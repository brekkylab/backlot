"""Google APIs (read-only): Gmail (``/gmail/v1``), Drive (``/drive/v3``), and the
Workspace editor read APIs — Docs (``/docs/v1``), Sheets (``/sheets/v4``), Slides
(``/slides/v1``) — for clients that read native docs structurally instead of via Drive export.

Client base-URL override: point the Gmail client at ``http://<host>/gmail`` and the
Drive client at ``http://<host>/drive`` (google-api-python-client ``api_endpoint``).
All authenticate with ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import re
from email.parser import BytesParser
from http import HTTPStatus
from typing import NamedTuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict

from backlot import auth, sheets_grid, store, synth
from backlot.acl import Caller
from backlot.config import get_settings
from backlot.errors import google as gerr
from backlot.openapi import qp
from backlot.pagination import decode_cursor, next_page_token

# `$.xgafv` is checked before any route runs — see `gerr.validate_system_parameters`.
router = APIRouter(tags=["google"], dependencies=[Depends(gerr.validate_system_parameters)])


# --- OpenAPI enrichment --------------------------------------------------
# Query params are read query-only (via _int/request.query_params); documenting them with
# openapi_extra keeps the handler bodies untouched and merges cleanly with the auto-generated
# path params. Response models use extra="allow" so builders' full field set passes through.


class _GLoose(BaseModel):
    model_config = ConfigDict(extra="allow")


class GmailMessageList(_GLoose):
    messages: list[dict] = []
    resultSizeEstimate: int = 0


class GmailThreadList(_GLoose):
    threads: list[dict] = []
    resultSizeEstimate: int = 0


class GmailMessage(_GLoose):
    id: str


class GmailThread(_GLoose):
    id: str
    messages: list[dict] = []


class GmailAttachment(_GLoose):
    attachmentId: str
    size: int
    data: str


_P_GMAIL_LIST = [qp("maxResults", "integer"), qp("pageToken"), qp("q")]
_P_GMAIL_FORMAT = [qp("format")]


class DriveFileList(_GLoose):
    kind: str = "drive#fileList"
    files: list[dict] = []


class DrivePermissionList(_GLoose):
    kind: str = "drive#permissionList"
    permissions: list[dict] = []


# drive_files_get / .export return raw Response/PlainTextResponse on some branches — they get
# openapi_extra params only (no JSON response_model, which would mis-serialize the raw body).
_P_DRIVE_LIST = [qp("pageSize", "integer"), qp("pageToken"), qp("q"), qp("fields"), qp("orderBy")]
_P_DRIVE_ALT = [qp("alt"), qp("fields")]
_P_DRIVE_EXPORT = [qp("mimeType", required=True)]
_P_DRIVE_ABOUT = [qp("fields", required=True)]

DRIVE_DOC_MIME = "application/vnd.google-apps.document"
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"

# --- Google-style multipart/mixed batch (google-api-python-client BatchHttpRequest) -------------
# The client POSTs one multipart/mixed body to a single batch_uri; each part is an application/http
# sub-request (which carries, or inherits from the outer request, its own Authorization). Google
# runs each and returns a multipart/mixed of application/http sub-responses matched by Content-ID.
# We emulate that by dispatching each sub-request in-process through this app (normal auth + routers)
# and reassembling the response, echoing each Content-ID so the client can pair them.
_BATCH_BOUNDARY = "erb_batch_boundary_9f2a7c"
_BATCH_DROP_HEADERS = {"host", "content-length", "content-transfer-encoding", "connection"}


def _batch_reason(code: int) -> str:
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return "Status"


def _parse_batch_subrequest(payload: str):
    """An application/http payload -> (method, target, headers, body)."""
    head, sep, body = payload.partition("\r\n\r\n")
    if not sep:
        head, sep, body = payload.partition("\n\n")
    lines = head.strip().splitlines()
    first = (lines[0].split(" ") + ["", ""])[:3] if lines else ["", "", ""]
    method, target = first[0], first[1]
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            if k.strip().lower() not in _BATCH_DROP_HEADERS:
                headers[k.strip()] = v.strip()
    return method, target, headers, body


@router.post("/batch")
@router.post("/batch/{api}/{version}")
async def batch(request: Request, api: str = "", version: str = "") -> Response:
    raw = await request.body()
    ctype = request.headers.get("content-type", "")
    if "multipart/mixed" not in ctype:
        return Response("expected multipart/mixed", status_code=400)
    # the email parser needs the Content-Type (with the boundary) as a header to split the parts
    parsed = BytesParser().parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
    if not parsed.is_multipart():
        return Response("not multipart/mixed", status_code=400)

    import httpx  # lazy: keep httpx out of app-import so a runtime image lacking it degrades only
    #               /batch, not the whole server (it's a test-time dep, not baked into the image)

    # Google applies the outer credential to any sub-request without its own; do the same so a batch
    # authenticates whether the client set per-sub-request auth or only the outer request.
    outer_auth = request.headers.get("authorization")
    transport = httpx.ASGITransport(app=request.app, raise_app_exceptions=False)
    out_parts: list[tuple[str, str]] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://backlot.batch") as client:
        for part in parsed.get_payload():
            cid = part.get("Content-ID", "")
            method, target, sub_headers, sub_body = _parse_batch_subrequest(
                part.get_payload(decode=False)
            )
            if outer_auth and not any(k.lower() == "authorization" for k in sub_headers):
                sub_headers["Authorization"] = outer_auth
            if not method or not target:
                sub_resp = "HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\n\r\nmalformed sub-request"
            else:
                r = await client.request(
                    method,
                    target,
                    headers=sub_headers,
                    content=sub_body.encode() if sub_body else None,
                )
                sub_resp = (
                    f"HTTP/1.1 {r.status_code} {_batch_reason(r.status_code)}\r\n"
                    f"Content-Type: {r.headers.get('content-type', 'application/json')}\r\n"
                    f"\r\n{r.text}"
                )
            out_parts.append((cid, sub_resp))

    body = ""
    for cid, sub_resp in out_parts:
        body += f"--{_BATCH_BOUNDARY}\r\nContent-Type: application/http\r\n"
        if cid:
            body += f"Content-ID: {cid}\r\n"
        body += "\r\n" + sub_resp + "\r\n"
    body += f"--{_BATCH_BOUNDARY}--\r\n"
    return Response(content=body, media_type=f'multipart/mixed; boundary="{_BATCH_BOUNDARY}"')


def _require(request: Request) -> Caller:
    """The caller, or the error real Google gives — NOT the shared ``auth.require_bearer``, because
    Google's answer is not one status. Measured: a present-but-invalid bearer is 401 UNAUTHENTICATED
    everywhere, while NO Authorization header at all is 403 PERMISSION_DENIED on Drive and Sheets
    (they accept API keys, so an anonymous request is a caller with no established identity) and 401
    on the OAuth-only Gmail/Docs/Slides."""
    caller = auth.resolve_bearer(request)
    if caller is None:
        if not request.headers.get("authorization"):
            raise gerr.no_credentials(request.url.path)
        raise gerr.bad_token()
    return caller


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


# ================================ Gmail =========================================


def _mailbox_email(caller: Caller, user_id: str) -> str | None:
    """Resolve the mailbox owner email; None means 'all mailboxes' (admin, 'me')."""
    if user_id == "me":
        return caller.email  # None for admin
    return user_id if "@" in user_id else None


def _mailbox_address(mailbox: str) -> str:
    """A mailbox's own address, for the ``Delivered-To`` a receiving MTA would have added.
    A corpus that states the mailbox AS an address already carries its domain."""
    return mailbox if "@" in mailbox else f"{mailbox}@{get_settings().org_domain}"


def _mailbox_container(conn, caller: Caller, user_id: str) -> str | None:
    """Resolve the requested mailbox to the ``gmail_messages.mailbox`` value it is stored under.
    None = all mailboxes (admin ``me``). A concrete address (``me`` for a user, or an explicit
    email) resolves to the WHOLE mailbox — received and sent — rather than to the messages that
    address happened to author; ``store.mailbox_for`` is what knows how the corpus spelled it."""
    email = caller.email if user_id == "me" else (user_id if "@" in user_id else None)
    return store.mailbox_for(conn, email) if email else None


def _service_email(request: Request) -> str:
    """The identity to report for an admin/service caller that has no single mailbox
    (a bare service account / full-crawl token). Real Gmail always reports a concrete
    address here — never the literal ``me`` path segment — so we use the service account's
    email, falling back to a service address on the org domain."""
    oauth = getattr(request.app.state, "oauth", None)
    if oauth is not None and oauth.client_email:
        return oauth.client_email
    return f"service@{get_settings().org_domain}"


def _mailbox_totals(conn, caller: Caller, user_id: str, ids) -> tuple[int, int]:
    """``(messages, threads)`` in the requested mailbox. Two counts, because they are two numbers:
    a thread of five messages is one thread, and reporting the message count as both made every
    threaded mailbox claim more threads than it holds."""
    mailbox = _mailbox_container(conn, caller, user_id)
    return (
        store.count_documents(conn, "gmail", container=mailbox, visible_ids=ids),
        store.count_documents(conn, "gmail", container=mailbox, visible_ids=ids, roots_only=True),
    )


@router.get("/gmail/v1/users/{user_id}/profile")
async def gmail_profile(user_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    # A concrete mailbox (``me`` -> caller.email, or an explicit address) if we have one;
    # otherwise the admin/service identity — never echo the raw ``me`` path segment.
    email = _mailbox_email(caller, user_id) or caller.email or _service_email(request)
    ids = auth.visible_ids(request, caller)
    messages, threads = _mailbox_totals(conn, caller, user_id, ids)
    return {
        "emailAddress": email,
        "messagesTotal": messages,
        "threadsTotal": threads,
        "historyId": "1",
    }


# The label a message carries when the corpus states none — the one `messages.get` reports, so a
# query for it has to agree with the message it comes back with.
_GMAIL_DEFAULT_LABEL = "INBOX"

# The system labels Gmail always exposes (users.labels.list).
_SYSTEM_LABELS = [
    "INBOX",
    "SENT",
    "DRAFT",
    "SPAM",
    "TRASH",
    "UNREAD",
    "STARRED",
    "IMPORTANT",
    "CHAT",
    "CATEGORY_PERSONAL",
    "CATEGORY_SOCIAL",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
    "CATEGORY_PROMOTIONS",
]


def _label_obj(lid: str, messages: int = 0, threads: int = 0) -> dict:
    hide = lid in ("SPAM", "TRASH", "CHAT")
    return {
        "id": lid,
        "name": lid,
        "type": "system",
        "messageListVisibility": "hide" if hide else "show",
        "labelListVisibility": "labelHide" if lid.startswith("CATEGORY_") else "labelShow",
        "messagesTotal": messages,
        "messagesUnread": 0,
        "threadsTotal": threads,
        "threadsUnread": 0,
    }


@router.get("/gmail/v1/users/{user_id}/labels")
async def gmail_labels(user_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    messages, threads = _mailbox_totals(conn, caller, user_id, ids)
    labels = [
        _label_obj(lid, *((messages, threads) if lid == _GMAIL_DEFAULT_LABEL else (0, 0)))
        for lid in _SYSTEM_LABELS
    ]
    return {"labels": labels}


@router.get("/gmail/v1/users/{user_id}/labels/{label_id}")
async def gmail_label_get(user_id: str, label_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    if label_id not in _SYSTEM_LABELS:
        raise gerr.not_found_entity()
    ids = auth.visible_ids(request, caller)
    messages, threads = _mailbox_totals(conn, caller, user_id, ids)
    return _label_obj(
        label_id, *((messages, threads) if label_id == _GMAIL_DEFAULT_LABEL else (0, 0))
    )


_GMAIL_OP = re.compile(r'(\w+):("[^"]*"|\S+)')
# operators we honor; anything else stays as free text
_GMAIL_KEYS = {
    "from",
    "to",
    "subject",
    "after",
    "before",
    "label",
    "in",
    "has",
    "newer_than",
    "older_than",
}


def _parse_gmail_q(q: str) -> tuple[str, dict]:
    """Split a Gmail search `q` into (free_text, operators). Honors from:/to:/subject:/
    after:/before:/newer_than:/older_than:/label:/in:/has: — the rest is free text matched
    full-text."""
    ops: dict[str, list[str]] = {}

    def _take(m):
        key = m.group(1).lower()
        if key in _GMAIL_KEYS:
            ops.setdefault(key, []).append(m.group(2).strip('"'))
            return " "
        return m.group(0)

    free = re.sub(r"\s+", " ", _GMAIL_OP.sub(_take, q)).strip()
    return free, ops


def _gmail_date(v: str) -> int | None:
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return int(
                datetime.datetime.strptime(v, fmt).replace(tzinfo=datetime.timezone.utc).timestamp()
            )
        except ValueError:
            continue
    try:
        return int(v)  # epoch seconds
    except ValueError:
        return None


# Gmail relative-age units for newer_than:/older_than:. Real Gmail counts calendar months/years,
# which we can't reproduce without the query's wall-clock calendar; days-per-unit is a faithful-
# enough approximation here (the operators are otherwise honored exactly).
_GMAIL_REL_UNIT = {"d": 1, "m": 30, "y": 365}
_GMAIL_REL = re.compile(r"(\d+)([dmy])")


def _gmail_rel_secs(v: str) -> int | None:
    """Seconds for a Gmail relative-age token like ``5d`` / ``2m`` / ``1y`` (newer_than:/older_than:).
    None if it isn't a recognized relative token, so callers can ignore it rather than zero out."""
    m = _GMAIL_REL.fullmatch(v.strip().lower())
    return int(m.group(1)) * _GMAIL_REL_UNIT[m.group(2)] * 86400 if m else None


def _resolve_relative_dates(ops: dict) -> dict:
    """Fold Gmail's relative-age operators into the absolute after:/before: bounds the rest of the
    pipeline already understands (SQL range push-down + `_gmail_op_match`), anchored to *now* — so
    newer_than:5d becomes ``after`` (ts >= now-5d) and older_than:5d becomes ``before`` (ts < now-5d).
    Returns ``ops`` unchanged when no relative operator is present."""
    new_secs = [s for v in ops.get("newer_than", []) if (s := _gmail_rel_secs(v)) is not None]
    old_secs = [s for v in ops.get("older_than", []) if (s := _gmail_rel_secs(v)) is not None]
    if not new_secs and not old_secs:
        return ops
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    ops = {k: list(vs) for k, vs in ops.items()}
    ops.pop("newer_than", None)
    ops.pop("older_than", None)
    # as epoch-second strings: both the SQL push-down and _gmail_op_match parse these via _gmail_date
    ops.setdefault("after", []).extend(str(now - s) for s in new_secs)
    ops.setdefault("before", []).extend(str(now - s) for s in old_secs)
    return ops


def _gmail_op_match(row, ops: dict) -> bool:
    for v in ops.get("from", []):
        if v.lower() not in (row["author_email"] or "").lower():
            return False
    for v in ops.get("to", []):
        if v.lower() not in (row["to_addr"] or "").lower():
            return False
    for v in ops.get("subject", []):
        if v.lower() not in (row["title"] or "").lower():
            return False
    # `in:` and `label:` ask the same question of a message — Gmail's folders ARE labels — and both
    # have to see the label a message is served under rather than only a stated one. `in:anywhere`
    # is the exception: it widens the search to spam and trash, which Backlot holds none of, so
    # it restricts nothing.
    labels = [x.lower() for x in store.jcol(row, "label_ids")] or [_GMAIL_DEFAULT_LABEL.lower()]
    for v in ops.get("label", []) + [x for x in ops.get("in", []) if x.lower() != "anywhere"]:
        if v.lower() not in labels:
            return False
    if any(v.lower() == "attachment" for v in ops.get("has", [])) and not store.jcol(
        row, "attachments"
    ):
        return False
    ts = _gmail_ts(row)
    for v in ops.get("after", []):
        d = _gmail_date(v)
        if d is not None and ts < d:
            return False
    for v in ops.get("before", []):
        d = _gmail_date(v)
        if d is not None and ts >= d:
            return False
    return True


def _gmail_query(conn, mailbox, ids, q: str) -> list:
    """Full ACL+mailbox-filtered match set for a Gmail `q` (FTS-ranked when free text is
    present; otherwise the mailbox listing). The caller paginates the returned rows."""
    free, ops = _parse_gmail_q(q)
    ops = _resolve_relative_dates(ops)  # newer_than:/older_than: -> absolute after:/before: bounds
    if free:
        # Honor a fully "quoted" free-text term as a phrase (Gmail's quote semantics): match the
        # tokens adjacently AND rank docs literally containing the phrase first, so a grep push-down
        # for e.g. "upload.csv" surfaces the one doc that contains it instead of burying it under
        # coincidental "upload csv" mentions. Unquoted free text stays an AND of terms.
        phrase = len(free) >= 2 and free[0] == '"' and free[-1] == '"'
        term = free[1:-1] if phrase else free
        cand = store.search_documents(
            conn, term, "gmail", ids, limit=10_000, offset=0, container=mailbox, phrase=phrase
        )
    else:
        # No free text. If the query pins a date range (after:/before:), filter created_ts in SQL —
        # a date-dir listing otherwise materialized the whole mailbox (~100k rows) then date-filtered
        # in Python. after: -> ts >= d (inclusive lo), before: -> ts < d (exclusive hi), matching
        # _gmail_op_match; the remaining ops still filter the (now small) candidate set below.
        lo = max(
            (d for v in ops.get("after", []) if (d := _gmail_date(v)) is not None), default=None
        )
        hi = min(
            (d for v in ops.get("before", []) if (d := _gmail_date(v)) is not None), default=None
        )
        # list_gmail_in_range for BOTH the date-pinned and the open-ended case (lo=hi=None): its
        # created_ts DESC, id order is the newest-first listing real Gmail returns — the plain
        # list_documents path ordered by id (a hash), scattering the listing by date.
        cand = store.list_gmail_in_range(conn, mailbox, lo, hi, ids, limit=100_000)
    return [r for r in cand if _gmail_op_match(r, ops)]


# --- Gmail ids ------------------------------------------------------------------------------
# A gmail id is a 16-hex integer (`synth.gmail_message_id`) and it IS the row's primary key
# (`gmail_messages.id`, assigned at import — see `backlot.importer.byo`), so resolution is a point
# lookup rather than a map rebuilt on every boot. `thread_id` holds the ROOT MESSAGE'S id,
# already resolved at import, so a thread resolves through the same key with no re-derivation.

_GMAIL_HEX = re.compile(r"[0-9a-fA-F]+\Z")


def _gmail_check_shape(served_id: str) -> None:
    """Raises if ``served_id`` isn't a parsable, in-range hex id — the check every gmail
    id-resolving path must run BEFORE any lookup, so an unparsable id is 400 INVALID_ARGUMENT
    regardless of whether it would otherwise resolve.

    Measured against the real API: 400 INVALID_ARGUMENT "Invalid id value" for a non-hex id or one
    >= 2**63, 404 only for a well-formed id it does not hold. `7fffffffffffffff` is well-formed;
    `8000000000000000` is not."""
    if not _GMAIL_HEX.fullmatch(served_id) or int(served_id, 16) >= synth.GMAIL_ID_MAX:
        raise gerr.invalid_id_value()


def _gmail_resolve(served_id: str) -> str | None:
    """Validate a served Gmail id's SHAPE and hand it back — a thread is keyed on the root
    message's own id, so there is nothing left to translate, only to reject.

    Kept as a named step rather than inlined because the shape check must run BEFORE any lookup:
    an unparsable id is 400 INVALID_ARGUMENT whether or not it would have resolved. No
    ``visible_ids``: the ACL read stays in the caller (`store.gmail_thread`), so an id naming a
    thread the caller cannot see is not-found, never a different answer.

    Lowercased, because the id is hex and real Gmail resolves either spelling: `store.gmail_by_id`
    folds case, so returning the spelling as given made `threads.get` the one route that did not —
    an uppercase id missed the exact `thread_id = ?` lookup and fell through to a single message."""
    _gmail_check_shape(served_id)
    return served_id.lower()


def _gmail_doc(conn, ids, served_id: str):
    """The visible row behind a served id, one query: shape validation first (400 before any
    lookup), then a single ACL-scoped column lookup — not a resolve-then-refetch, which would cost
    two full-row reads of the same wide table per call. Resolution and the ACL read still can't be
    pulled apart: the query is scoped to `visible_ids` from the start, so a served_id that names a
    document the caller cannot see comes back as no row, i.e. not-found, never a different answer
    (one WHERE clause holds that invariant; it needs no second round trip)."""
    _gmail_check_shape(served_id)
    return store.gmail_by_id(conn, served_id, visible_ids=ids)


def _by_thread(rows) -> list:
    """One row per thread, first occurrence kept — so a thread listing reports a thread once,
    whichever of its messages the search matched, in the order the match set arrived."""
    seen, out = set(), []
    for row in rows:
        thread = _gmail_ids(row)[1]
        if thread not in seen:
            seen.add(thread)
            out.append(row)
    return out


def _gmail_ids(row) -> tuple[str, str]:
    """``(id, threadId)`` for a row. A message that is its own thread root reports the same value
    twice, as real Gmail does.

    Both halves are read straight off the row. `thread_id` holds the ROOT'S OWN id,
    resolved once at import, so `threadId` reads one stored value rather than re-hashing the
    root's key and hoping the two agree."""
    return (row["id"], row["thread_id"] or row["id"])


@router.get(
    "/gmail/v1/users/{user_id}/messages",
    response_model=GmailMessageList,
    openapi_extra={"parameters": _P_GMAIL_LIST},
)
async def gmail_messages_list(user_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    mailbox = _mailbox_container(conn, caller, user_id)  # None = all mailboxes
    limit = _int(request, "maxResults", get_settings().default_page_size)
    offset = decode_cursor(request.query_params.get("pageToken"))
    q = request.query_params.get("q", "") or ""
    if q.strip():  # search: filter the ACL-visible set by the query, then paginate
        matched = _gmail_query(conn, mailbox, ids, q)
        total = len(matched)
        rows = matched[offset : offset + limit]
    else:
        # newest-first by internalDate (created_ts), like real Gmail — NOT id (hash) order, so a
        # capped "newest N" crawl is deterministic by date, not random. Open-ended range = whole box.
        total = store.count_documents(conn, "gmail", container=mailbox, visible_ids=ids)
        rows = store.list_gmail_in_range(conn, mailbox, None, None, ids, limit=limit, offset=offset)
    # threadId must agree with messages.get (a reply belongs to its root's thread)
    messages = [dict(zip(("id", "threadId"), _gmail_ids(r))) for r in rows]
    body = {"messages": messages, "resultSizeEstimate": total}
    token = next_page_token(offset, len(rows), total)
    if token:
        body["nextPageToken"] = token
    return body


@router.get(
    "/gmail/v1/users/{user_id}/messages/{msg_id}",
    response_model=GmailMessage,
    openapi_extra={"parameters": _P_GMAIL_FORMAT},
)
async def gmail_messages_get(user_id: str, msg_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _gmail_doc(conn, ids, msg_id)
    if row is None:
        raise gerr.not_found_entity()
    return _gmail_message(row, request.query_params.get("format", "full"), caller.email)


@router.get(
    "/gmail/v1/users/{user_id}/messages/{msg_id}/attachments/{att_id}",
    response_model=GmailAttachment,
)
async def gmail_attachment(user_id: str, msg_id: str, att_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _gmail_doc(conn, ids, msg_id)
    if row is None:
        raise gerr.not_found_entity()
    message_id = row["id"]
    found = next(
        (
            (i, a)
            for i, a in enumerate(store.jcol(row, "attachments"))
            if _att_id(message_id, i) == att_id
        ),
        None,
    )
    body = _att_content(message_id, found[0], found[1]) if found else f"attachment {att_id}"
    return {"attachmentId": att_id, "size": len(body), "data": _b64url(body)}


@router.get(
    "/gmail/v1/users/{user_id}/threads",
    response_model=GmailThreadList,
    openapi_extra={"parameters": _P_GMAIL_LIST},
)
async def gmail_threads_list(user_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    # The MAILBOX's threads, like messages.list and like real Gmail — not the threads the caller
    # happens to have written in. Scoping by author served a mailbox owner none of the mail they
    # received, and `q` was already scoping by container, so the two halves of this one listing
    # disagreed about what a thread list is.
    mailbox = _mailbox_container(conn, caller, user_id)
    limit = _int(request, "maxResults", get_settings().default_page_size)
    offset = decode_cursor(request.query_params.get("pageToken"))
    q = request.query_params.get("q", "") or ""
    if q.strip():
        # A search returns the THREADS its matches are in: Gmail lists a thread whose match is in a
        # reply, and lists it once even when several of its messages match.
        matched = _by_thread(_gmail_query(conn, mailbox, ids, q))
        total = len(matched)
        rows = matched[offset : offset + limit]
    else:
        total = store.count_documents(
            conn, "gmail", container=mailbox, visible_ids=ids, roots_only=True
        )
        # newest-first by internalDate, the order messages.list already serves and the order a
        # capped crawl of a mailbox has to be stable under.
        rows = store.list_gmail_in_range(
            conn, mailbox, None, None, ids, limit=limit, offset=offset, roots_only=True
        )
    threads = [
        {"id": _gmail_ids(r)[1], "snippet": r["content"][:200], "historyId": "1"} for r in rows
    ]
    body = {"threads": threads, "resultSizeEstimate": total}
    token = next_page_token(offset, len(rows), total)
    if token:
        body["nextPageToken"] = token
    return body


@router.get(
    "/gmail/v1/users/{user_id}/threads/{thread_id}",
    response_model=GmailThread,
    openapi_extra={"parameters": _P_GMAIL_FORMAT},
)
async def gmail_thread_get(user_id: str, thread_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    thread_key = _gmail_resolve(thread_id)
    msgs = store.gmail_thread(conn, thread_key, visible_ids=ids) if thread_key else []
    if not msgs:
        row = _gmail_doc(conn, ids, thread_id)
        if row is None:
            raise gerr.not_found_entity()
        msgs = [row]
    fmt = request.query_params.get("format", "full")
    return {
        "id": thread_id.lower(),
        "snippet": msgs[0]["content"][:200],
        "historyId": "1",
        "messages": [_gmail_message(m, fmt, caller.email) for m in msgs],
    }


def _att_id(message_id: str, i: int) -> str:
    return "ANGjdJ" + synth.gmail_id(message_id, salt=f"att{i}")


def _att_content(message_id: str, i: int, att: dict) -> str:
    """The exact bytes ``attachments.get`` serves for attachment ``i``, and therefore what
    ``messages.get`` reports as that part's ``body.size`` — real Gmail keeps the two equal so a
    client can stat from metadata alone. The corpus-declared ``size`` cannot be honoured with
    placeholder bytes, so the served content's length is the single source of truth."""
    return att.get("content", f"attachment {_att_id(message_id, i)}")


def _leaf(mime: str, part_id: str, data: str) -> dict:
    return {
        "partId": part_id,
        "mimeType": mime,
        "filename": "",
        "body": {"size": len(data), "data": _b64url(data)},
    }


def _gmail_ts(row) -> int:
    """A message's unix ts. A real per-message created_ts (its parsed Date header) is used
    verbatim; only when it's missing do we synthesize a thread base and spread replies an hour
    apart so a thread still reads in order. Both the served Date and the after/before filter use
    this, so they agree."""
    # `is not None`: 1970-01-01T00:00:00Z stores as 0, and a message that HAS a second must serve
    # it rather than a synthesized one.
    if row["created_ts"] is not None:
        return row["created_ts"]
    return synth.epoch(row["thread_id"] or row["id"]) + (row["thread_seq"] or 0) * 3600


def _gmail_message(row, fmt: str, caller_email: str | None = None) -> dict:
    """One message in the API's shape.

    `caller_email` decides Bcc. Real Gmail keeps the Bcc header only on the sender's own copy — a
    recipient's is stripped in transit — so a reader who is not the author must not learn who was
    blind-copied. An admin/service caller has no email and is not the sender either.
    """
    ts = _gmail_ts(row)
    author = row["author_email"]
    display = author.split("@")[0].replace(".", " ").title()
    msg_id = row["message_id"] or f"<{row['id']}@{get_settings().org_domain}>"
    headers = [
        {
            "name": "Delivered-To",
            "value": row["to_addr"] or _mailbox_address(row["mailbox"]),
        },
        {"name": "MIME-Version", "value": "1.0"},
    ]
    # `Subject` sits with `To`, not with the mandatory headers: RFC 5322 §3.6 gives both 0..1, and
    # this corpus format says an empty subject IS the absence of one ("a message with no Subject
    # header is legal"). Emitting `Subject: ""` served a header real Gmail would have left out.
    # Placed here rather than appended with the others so the header order stays as it was.
    if row["title"]:
        headers.append({"name": "Subject", "value": row["title"]})
    headers += [
        {"name": "From", "value": f"{display} <{author}>"},
        {"name": "Date", "value": synth.rfc2822(ts)},
        {"name": "Message-ID", "value": msg_id},
    ]
    optional = [
        # `To` among them: RFC 5322 allows a message with no destination field, and real Gmail
        # returns the headers the message has. `Delivered-To` above keeps its default, since a
        # receiving MTA really does add one.
        ("To", "to_addr"),
        ("Cc", "cc"),
        ("Reply-To", "reply_to"),
        ("In-Reply-To", "in_reply_to"),
        ("References", "refs"),
    ]
    if caller_email and caller_email == author:
        optional.insert(1, ("Bcc", "bcc"))
    for hname, col in optional:
        if row[col]:
            headers.append({"name": hname, "value": row[col]})
    attachments = store.jcol(row, "attachments")
    top_mime = "multipart/mixed" if attachments else "multipart/alternative"
    boundary = f"b_{row['id'][:12]}"
    headers.append({"name": "Content-Type", "value": f'{top_mime}; boundary="{boundary}"'})

    msg = {
        "id": _gmail_ids(row)[0],
        "threadId": _gmail_ids(row)[1],
        "labelIds": store.jcol(row, "label_ids") or [_GMAIL_DEFAULT_LABEL],
        "snippet": row["content"][:200],
        "historyId": "1",
        "internalDate": str(ts * 1000),
        "sizeEstimate": len(row["content"]) + 400,
    }
    html = row["body_html"] or f"<html><body><p>{row['content']}</p></body></html>"
    if fmt == "raw":
        # RFC 2822 message, base64url — a genuine boundary-delimited MIME body matching the
        # declared multipart Content-Type above. It has to be real MIME: a plain-text body under a
        # `multipart/...` header with no boundary makes Python's `email` parser raise
        # StartBoundaryNotFoundDefect/MultipartInvariantViolationDefect, and readers built on it
        # (llama-index's GmailReader) choke because `get_payload()` degrades to a bare
        # string instead of a list of sub-messages). Mirrors the same flat text/plain + text/html
        # (+ attachment) leaves the `full` format exposes via `parts` below.
        leaves = [
            f'Content-Type: text/plain; charset="UTF-8"\r\n\r\n{row["content"]}',
            f'Content-Type: text/html; charset="UTF-8"\r\n\r\n{html}',
        ]
        for i, att in enumerate(attachments):
            filename = att.get("filename", "attachment.bin")
            mime = att.get("mime", "application/octet-stream")
            # same bytes attachments.get serves, so raw MIME and the attachment endpoint agree
            b64 = base64.b64encode(_att_content(row["id"], i, att).encode("utf-8")).decode("ascii")
            leaves.append(
                f'Content-Type: {mime}; name="{filename}"\r\n'
                f'Content-Disposition: attachment; filename="{filename}"\r\n'
                f"Content-Transfer-Encoding: base64\r\n\r\n{b64}"
            )
        mime_body = "".join(f"--{boundary}\r\n{leaf}\r\n" for leaf in leaves) + f"--{boundary}--"
        raw = "\r\n".join(f"{h['name']}: {h['value']}" for h in headers) + "\r\n\r\n" + mime_body
        msg["raw"] = _b64url(raw)
        return msg
    if fmt == "minimal":
        return msg
    if fmt == "metadata":
        msg["payload"] = {
            "partId": "",
            "mimeType": top_mime,
            "filename": "",
            "headers": headers,
            "body": {"size": 0},
        }
        return msg

    # full: multipart with text/plain + text/html leaves, plus attachment leaves
    parts = [_leaf("text/plain", "0", row["content"]), _leaf("text/html", "1", html)]
    for i, att in enumerate(attachments):
        parts.append(
            {
                "partId": str(i + 2),
                "mimeType": att.get("mime", "application/octet-stream"),
                "filename": att.get("filename", "attachment.bin"),
                "headers": [
                    {
                        "name": "Content-Disposition",
                        "value": f'attachment; filename="{att.get("filename", "attachment.bin")}"',
                    }
                ],
                # size = the exact byte length attachments.get serves (see _att_content), so a client can
                # stat the attachment from this metadata without a second call — real Gmail's contract.
                "body": {
                    "attachmentId": _att_id(row["id"], i),
                    "size": len(_att_content(row["id"], i, att)),
                },
            }
        )
    msg["payload"] = {
        "partId": "",
        "mimeType": top_mime,
        "filename": "",
        "headers": headers,
        "body": {"size": 0},
        "parts": parts,
    }
    return msg


# ================================ Drive =========================================

# --- `q` ---------------------------------------------------------------------------------------
# Drive's query language, PARSED rather than pattern-matched. One regex per known clause let any
# clause the regexes did not match — `name = '…'`, `modifiedTime < '…'`, `mimeType contains '…'`,
# an `or`, a `not`, a mistyped term — drop out of the FILTER instead of out of the result, so the
# caller got the unfiltered listing under a 200. Measured 2026-09-12 at 6490f3b: `name = 'First
# Week Checklist'` answered every visible file, 16, where `name contains` answered 1. mirage sends
# two of those shapes: its folder listing resolves a file with `name='…'` and bounds a sync with
# `modifiedTime >= '…'` and `modifiedTime < '…'`.
#
# The grammar is the reference's (developers.google.com/workspace/drive/api/guides/ref-search-terms):
# a term is `<field> <operator> <value>` or `'<value>' in <collection>`, terms join with `and` and
# `or`, `not` negates, and a string value is single-quoted with an apostrophe escaped as `\'` —
# "Escape single quotes in queries with \'". Parentheses group. Whether `and` binds before `or` the
# reference does not say; Backlot reads it as SQL does, `and` first.
#
# A term Backlot cannot evaluate is a 400 on `q`, never a silence. Two kinds: a term the reference
# does not list (`bogusField = 'x'`), which real Drive refuses too — its wording is unmeasured, so
# the bare `Invalid Value` of the parameter envelope stands — and a documented term Backlot holds no
# fact for, refused with a message that says so, the way an unmodelled `orderBy` key is. Honouring
# `starred = true` as "everything" would be the listing this section exists to stop.

# Every term Backlot evaluates, with the operators the reference lists for it.
_DRIVE_Q_OPERATORS: dict[str, frozenset[str]] = {
    "name": frozenset({"contains", "=", "!="}),
    "fullText": frozenset({"contains"}),
    "mimeType": frozenset({"contains", "=", "!="}),
    "modifiedTime": frozenset({"<=", "<", "=", "!=", ">", ">="}),
    "createdTime": frozenset({"<=", "<", "=", "!=", ">", ">="}),
    "trashed": frozenset({"=", "!="}),
    "sharedWithMe": frozenset({"=", "!="}),
}
_DRIVE_Q_COLLECTIONS = frozenset({"parents", "owners"})
_DRIVE_Q_BOOLEAN = frozenset({"trashed", "sharedWithMe"})
# Documented on the reference, and nothing in a corpus record to evaluate them against.
_DRIVE_Q_UNMODELLED = frozenset(
    {"starred", "viewedByMeTime", "writers", "readers", "properties", "appProperties", "visibility"}
)
_DRIVE_Q_TOKEN = re.compile(
    r"\s*(?:(?P<paren>[()])|(?P<op>!=|<=|>=|=|<|>)|'(?P<str>(?:[^'\\]|\\.)*)'"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_]*))"
)


class _QTerm(NamedTuple):
    field: str
    op: str
    value: str


class _QNot(NamedTuple):
    inner: object


class _QAnd(NamedTuple):
    parts: tuple


class _QOr(NamedTuple):
    parts: tuple


def _drive_q_refused(message: str | None = None) -> gerr.GoogleError:
    return gerr.invalid_value("q", message)


def _drive_q_tokens(q: str) -> list[tuple[str, str]]:
    """``(kind, text)`` pairs; a quoted value arrives unescaped."""
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(q):
        m = _DRIVE_Q_TOKEN.match(q, pos)
        if m is None:
            if q[pos:].strip():
                raise _drive_q_refused()
            break
        pos = m.end()
        kind = m.lastgroup or ""
        text = m.group(kind)
        out.append((kind, re.sub(r"\\(.)", r"\1", text) if kind == "str" else text))
    return out


def _drive_q_parse(q: str):
    """The parsed query, or ``None`` for an empty one. A clause Backlot cannot evaluate is a 400.

    A query that names no ``trashed`` term gets ``and trashed = false`` appended: real Drive leaves
    trashed files out of a listing unless a clause asks for them, which the matcher used to apply as
    its default branch and the plain listing applies as ``exclude_trashed``."""
    tokens = _drive_q_tokens(q)
    if not tokens:
        return None
    pos = 0

    def peek() -> tuple[str | None, str | None]:
        return tokens[pos] if pos < len(tokens) else (None, None)

    def take() -> tuple[str | None, str | None]:
        nonlocal pos
        tok = peek()
        pos += 1
        return tok

    def is_word(text: str) -> bool:
        kind, tok = peek()
        return kind == "word" and (tok or "").lower() == text

    def disjunction():
        parts = [conjunction()]
        while is_word("or"):
            take()
            parts.append(conjunction())
        return parts[0] if len(parts) == 1 else _QOr(tuple(parts))

    def conjunction():
        parts = [unary()]
        while is_word("and"):
            take()
            parts.append(unary())
        return parts[0] if len(parts) == 1 else _QAnd(tuple(parts))

    def unary():
        if is_word("not"):
            take()
            return _QNot(unary())
        if peek() == ("paren", "("):
            take()
            node = disjunction()
            if take() != ("paren", ")"):
                raise _drive_q_refused()
            return node
        return term()

    def unmodelled(field: str) -> gerr.GoogleError:
        evaluated = ", ".join(sorted(_DRIVE_Q_OPERATORS))
        return _drive_q_refused(
            f"'{field}' is not evaluated by Backlot: a corpus record carries nothing to answer it "
            f"from. Terms it evaluates: {evaluated}, and 'x' in parents / owners."
        )

    def term():
        kind, text = take()
        if kind == "str":  # `'<value>' in <collection>`
            if not is_word("in"):
                raise _drive_q_refused()
            take()
            fkind, field = take()
            if fkind != "word":
                raise _drive_q_refused()
            if field in _DRIVE_Q_UNMODELLED:
                raise unmodelled(field)
            if field not in _DRIVE_Q_COLLECTIONS:
                raise _drive_q_refused()
            return _QTerm(field, "in", text or "")
        if kind != "word" or text is None:
            raise _drive_q_refused()
        field = text
        if field in _DRIVE_Q_UNMODELLED:
            raise unmodelled(field)
        if field not in _DRIVE_Q_OPERATORS:
            raise _drive_q_refused()
        okind, op = peek()
        if okind == "op":
            take()
        elif okind == "word" and (op or "").lower() in ("contains", "has", "in"):
            take()
            op = (op or "").lower()
        elif field == "sharedWithMe":
            # The bare `sharedWithMe` Drive also accepts, meaning true.
            return _QTerm(field, "=", "true")
        else:
            raise _drive_q_refused()
        if op not in _DRIVE_Q_OPERATORS[field]:
            raise _drive_q_refused()
        vkind, value = take()
        if field in _DRIVE_Q_BOOLEAN:
            if vkind != "word" or (value or "").lower() not in ("true", "false"):
                raise _drive_q_refused()
            return _QTerm(field, op or "", (value or "").lower())
        if vkind != "str":
            raise _drive_q_refused()
        if field in ("modifiedTime", "createdTime") and _drive_q_time(value or "") is None:
            # The reference wants RFC 3339 here. A value that is not one would otherwise compare
            # as a string against the file's timestamp and answer something for every file; what
            # real Drive answers for it is unmeasured, so this is a refusal rather than its wording.
            raise _drive_q_refused()
        return _QTerm(field, op or "", value or "")

    node = disjunction()
    if pos != len(tokens):
        raise _drive_q_refused()
    if not any(t.field == "trashed" for t in _drive_q_terms(node)):
        node = _QAnd((node, _QTerm("trashed", "=", "false")))
    return node


def _drive_q_terms(node):
    """Every term in the tree, whatever it sits under."""
    if isinstance(node, _QTerm):
        yield node
    elif isinstance(node, _QNot):
        yield from _drive_q_terms(node.inner)
    elif isinstance(node, (_QAnd, _QOr)):
        for part in node.parts:
            yield from _drive_q_terms(part)


def _drive_q_conjuncts(node) -> list[_QTerm]:
    """The terms every match has to satisfy — those joined by `and` at the top, with nothing
    under an `or` or a `not`. What the SQL paths may narrow the candidate set by."""
    if isinstance(node, _QTerm):
        return [node]
    if isinstance(node, _QAnd):
        return [t for part in node.parts for t in _drive_q_conjuncts(part)]
    return []


def _drive_q_is_conjunction(node) -> bool:
    """Whether the whole tree is terms joined by `and`, so its conjuncts are the whole query."""
    if isinstance(node, _QTerm):
        return True
    return isinstance(node, _QAnd) and all(_drive_q_is_conjunction(p) for p in node.parts)


def _drive_q_time(value: str) -> datetime.datetime | None:
    """An RFC 3339 value as a datetime, UTC when it names no zone; ``None`` if it is not one."""
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def _drive_q_compare(op: str, have: str, want: str) -> bool:
    """A time term, compared as instants: `'2026-01-10T00:00:00'` equals `'…Z'`. The value was
    checked at parse; a fact that is no timestamp (an object with the field unset) matches
    nothing."""
    left, right = _drive_q_time(have), _drive_q_time(want)
    if left is None or right is None:
        return False
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == "=":
        return left == right
    if op == "!=":
        return left != right
    if op == ">":
        return left > right
    return left >= right


def _drive_q_eval(node, f: dict, me: str | None, fulltext: dict[str, set[str]]) -> bool:
    """Whether one file's facts satisfy the query. ``fulltext`` maps each `fullText contains`
    value to the ids the index answered for it, so the term is a membership test here."""
    if isinstance(node, _QNot):
        return not _drive_q_eval(node.inner, f, me, fulltext)
    if isinstance(node, _QAnd):
        return all(_drive_q_eval(p, f, me, fulltext) for p in node.parts)
    if isinstance(node, _QOr):
        return any(_drive_q_eval(p, f, me, fulltext) for p in node.parts)
    field, op, value = node
    if field == "trashed":
        return ((value == "true") == f["trashed"]) == (op == "=")
    if field == "sharedWithMe":
        # "Shared with me" = visible to the caller and not owned by them. Items shared with you
        # carry no My Drive parent on real Drive, so this clause is the only way to enumerate
        # that section.
        shared = not _drive_owned_by(f["owner_email"], me)
        return ((value == "true") == shared) == (op == "=")
    if field == "name":
        # Case-insensitive on every operator, as the `contains` this served before was. Whether
        # real Drive compares `name =` case-sensitively is unmeasured.
        have, want = f["name"].casefold(), value.casefold()
        return want in have if op == "contains" else (have == want) == (op == "=")
    if field == "mimeType":
        return value in f["mime"] if op == "contains" else (f["mime"] == value) == (op == "=")
    if field == "fullText":
        return f["id"] in fulltext.get(value, ())
    if field == "modifiedTime":
        return _drive_q_compare(op, f["modified"], value)
    if field == "createdTime":
        return _drive_q_compare(op, f["created"], value)
    if field == "parents":
        return value in f["parents"]
    if field == "owners":
        return value.strip().lower() in f["owners"]
    raise AssertionError(f"unevaluated q term {field!r}")  # every field the parser admits is above


def _drive_q_fulltext(conn, node, ids) -> dict[str, list]:
    """The index's answer for each `fullText contains` value in the query, as rows in rank order.

    Real Drive semantics: a quoted value (`fullText contains '"X Y"'`) is an exact phrase (tokens
    adjacent); unquoted is separate terms. A grep push-down sends the quoted form for a literal
    pattern, so the exact doc surfaces instead of being buried under coincidental docs that merely
    contain the words scattered."""
    out: dict[str, list] = {}
    for term in _drive_q_terms(node):
        if term.field != "fullText" or term.value in out:
            continue
        raw = term.value
        phrase = len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"'
        out[raw] = store.search_documents(
            conn, raw[1:-1] if phrase else raw, "google_drive", ids, limit=10_000, phrase=phrase
        )
    return out


def _drive_q_fulltext_ids(hits: dict[str, list]) -> dict[str, set[str]]:
    return {value: {r["id"] for r in rows} for value, rows in hits.items()}


def _drive_owned_by(owner_email: str | None, me: str | None) -> bool:
    """Whether the caller owns this file. The admin/service token is not a Drive user (its
    ``caller.email`` is None), so it owns nothing — for it, everything reads as shared."""
    return bool(me) and (owner_email or "").lower() == me.lower()


def _shared_with_me_time(owner_email: str | None, me: str | None, created: int) -> dict:
    """``sharedWithMeTime`` as a ``**``-mergeable fragment. Real Drive sets it only on items shared
    WITH the caller, so its presence is how a client tells a shared item from its own — the same
    partition ``q: sharedWithMe`` filters on, which is why the two must agree.

    Empty for an unknown caller (the admin token: nothing was shared with it) or an owned item. No
    share event is recorded, so the creation time stands in; ``modifiedTime`` would reorder
    ``orderBy=sharedWithMeTime`` every time the document was edited."""
    if not me or _drive_owned_by(owner_email, me):
        return {}
    return {"sharedWithMeTime": synth.rfc3339(created)}


def _drive_created(row) -> int:
    """A file's creation second — its own, else one seeded from its id. `is not None`, because
    1970-01-01T00:00:00Z stores as 0 and a file that HAS a second must serve it rather than a
    seeded one (which would also make the `q` time filters disagree with the served body)."""
    return row["created_ts"] if row["created_ts"] is not None else synth.epoch(row["id"])


def _drive_modified(row) -> int:
    """A file's last-modified second, an hour after creation when the corpus states none."""
    return row["updated_ts"] if row["updated_ts"] is not None else _drive_created(row) + 3600


def _drive_facts(row) -> dict:
    """The values `q` clauses are evaluated against, taken from a stored row."""
    modified = _drive_modified(row)
    return {
        "id": row["id"],
        "trashed": bool(row["trashed"]),
        "parents": store.jcol(row, "parents") or [synth.drive_folder_id(row["folder"])],
        "mime": _drive_mime(row),
        "name": row["title"] or "",
        "modified": synth.rfc3339(modified),
        "created": synth.rfc3339(_drive_created(row)),
        "owner_email": row["author_email"],
        # real Drive keys `in owners` on the owner's email; Backlot also accepts the owner
        # display name, since that's the only owner identifier some callers have.
        "owners": {(row["author_email"] or "").lower(), (row["owner_display"] or "").lower()},
    }


def _drive_obj_facts(obj: dict) -> dict:
    """The same values taken from an already-built file object — the synthesized folders, which
    exist only as objects, are matched through this so every clause treats them like a row."""
    return {
        "id": obj.get("id") or "",
        "trashed": bool(obj.get("trashed")),
        "parents": obj.get("parents") or [],
        "mime": obj.get("mimeType") or "",
        "name": obj.get("name") or "",
        "modified": obj.get("modifiedTime") or "",
        "created": obj.get("createdTime") or "",
        "owner_email": (obj.get("owners") or [{}])[0].get("emailAddress"),
        "owners": {(o.get("emailAddress") or "").lower() for o in (obj.get("owners") or [])},
    }


def _drive_q_match(row, query, me: str | None = None, fulltext: dict | None = None) -> bool:
    """Whether a stored row satisfies ``query`` — the string, or the tree ``_drive_q_parse`` built
    from it. Without ``fulltext`` a `fullText contains` term matches nothing."""
    node = _drive_q_parse(query) if isinstance(query, str) else query
    return node is None or _drive_q_eval(node, _drive_facts(row), me, fulltext or {})


def _visible_drive_folders(conn, ids) -> list[str]:
    """Folder names the caller can see a file in — the containers to surface as folders."""
    folders = [r["name"] for r in store.list_containers(conn, "google_drive")]
    if ids is None:  # admin sees every folder
        return sorted(folders)
    return sorted(f for f in folders if store.drive_folder_has_visible(conn, f, ids))


def _drive_folder_obj(conn, name: str, me: str | None = None) -> dict:
    """A Drive file object for a folder container. Its id matches what files in it report as
    their parent (``synth.drive_folder_id``), and it hangs under ``root`` so a client that
    navigates from My Drive root (e.g. mirage) can discover and descend into it.

    Backlot models no folder owner, so a folder is never owned by the caller and carries
    ``sharedWithMeTime`` like any other item the ``sharedWithMe`` filter returns — the folder stream
    has to answer a clause the same way the row stream does."""
    fid = synth.drive_folder_id(name)
    ts = synth.epoch("folder:" + name)
    return {
        "kind": "drive#file",
        "id": fid,
        "name": name,
        "mimeType": DRIVE_FOLDER_MIME,
        "parents": ["root"],
        "createdTime": synth.rfc3339(ts),
        "modifiedTime": synth.rfc3339(ts),
        **_shared_with_me_time(None, me, ts),
        "trashed": False,
        "explicitlyTrashed": False,
        "starred": False,
        "shared": True,
        "ownedByMe": False,
        "viewedByMe": False,
        "version": "1",
        "spaces": ["drive"],
        "webViewLink": f"https://drive.google.com/drive/folders/{fid}",
        "iconLink": "https://drive.google.com/icons/folder.png",
        "capabilities": {
            "canDownload": False,
            "canListChildren": True,
            "canComment": False,
            "canEdit": False,
            "canCopy": False,
            "canShare": True,
            "canRename": False,
            "canTrash": False,
            "canDelete": False,
            "canReadRevisions": False,
            "canAddChildren": False,
            "canModifyContent": False,
        },
    }


def _drive_folder_name_by_id(conn, file_id: str) -> str | None:
    """Reverse a synthesized folder id back to its container name. Uses the small folder table
    (no ACL/no per-row scan) — the caller's ACL is enforced when its files are then listed."""
    for row in store.list_containers(conn, "google_drive"):
        if synth.drive_folder_id(row["name"]) == file_id:
            return row["name"]
    return None


# --- `fields` projection -------------------------------------------------------------------
# Every field of the Drive v3 `files` resource per Google's reference — deliberately the whole
# documented set, not just the keys Backlot synthesizes: real Drive accepts a documented field
# it has no value for (and omits it from the response) while rejecting anything unknown with 400.
# Validating against it is what makes a Backlot-backed test able to catch a typo'd or stale mask.
_DRIVE_FILE_FIELDS = frozenset(
    """
    appProperties capabilities contentHints contentRestrictions copyRequiresWriterPermission
    createdTime description driveId explicitlyTrashed exportLinks fileExtension folderColorRgb
    fullFileExtension hasAugmentedPermissions hasThumbnail headRevisionId iconLink id
    imageMediaMetadata inheritedPermissionsDisabled isAppAuthorized kind labelInfo
    lastModifyingUser linkShareMetadata md5Checksum mimeType modifiedByMe modifiedByMeTime
    modifiedTime name originalFilename ownedByMe owners parents permissionIds permissions
    properties quotaBytesUsed resourceKey sha1Checksum sha256Checksum shared sharedWithMeTime
    sharingUser shortcutDetails size spaces starred teamDriveId thumbnailLink thumbnailVersion
    trashed trashedTime trashingUser version videoMediaMetadata viewedByMe viewedByMeTime
    viewersCanCopyContent webContentLink webViewLink writersCanShare
""".split()
)
_DRIVE_LIST_FIELDS = frozenset({"kind", "nextPageToken", "incompleteSearch", "files"})


def _split_mask(mask: str) -> list[str]:
    """Split a `fields` mask on its top-level commas, so a nested group stays whole
    (``files(id,name),nextPageToken`` -> ``['files(id,name)', 'nextPageToken']``)."""
    out, depth, cur = [], 0, ""
    for ch in mask:
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        depth += (ch == "(") - (ch == ")")
        depth = max(depth, 0)
        cur += ch
    out.append(cur)
    return [t.strip() for t in out if t.strip()]


def _mask_names(mask: str) -> set[str]:
    """The leading key of each comma-separated entry: a nested mask (``capabilities/canEdit``,
    ``owners(emailAddress)``) selects — and is validated as — its parent key."""
    return {t.split("/")[0].split("(")[0].strip() for t in _split_mask(mask)}


def _check_mask(names, allowed: frozenset) -> None:
    """Reject an unknown field name the way real Drive does. Without this a bogus name simply
    matched nothing and vanished, so the response was a 200 full of empty objects and no
    Backlot-backed test could catch a mask that 400s in production."""
    for n in sorted(names):
        if n != "*" and n not in allowed:
            raise gerr.invalid_parameter("fields", f"Invalid field selection {n}")


def _drive_file_field_keys(fields: str | None) -> set[str] | None:
    """File keys a ``files.list`` caller selected — so the response carries only those, not the
    full ~30-field object. Google accepts both the group form (``files(id,name)``) and the path
    form (``files/id``); both are honored. ``None`` = no projection (an absent mask, or one that
    asks for everything with ``*``).

    Top-level names are validated but not projected: Backlot always returns ``kind`` and
    ``incompleteSearch``, because its typed response model (``DriveFileList``, which the OpenAPI
    schema is built from) declares them."""
    if not (fields or "").strip():
        return None
    top, keys = set(), set()
    for tok in _split_mask(fields):
        if tok == "*":
            return None
        group = re.fullmatch(r"files\s*\((.*)\)", tok, re.DOTALL)
        if group:
            top.add("files")
            keys |= _mask_names(group.group(1))
        elif tok.startswith("files/"):
            top.add("files")
            keys |= _mask_names(tok[len("files/") :])
        else:
            top.add(tok.split("/")[0].split("(")[0])
    _check_mask(top, _DRIVE_LIST_FIELDS)
    _check_mask(keys, _DRIVE_FILE_FIELDS)
    return None if "*" in keys else (keys or None)


def _drive_get_field_keys(fields: str | None) -> set[str] | None:
    """The same projection for ``files.get``, whose mask names file fields directly
    (``fields=id,name,size``). Applying it is what makes one file look the same whether a client
    read it out of a listing or resolved it by id."""
    if not (fields or "").strip():
        return None
    keys = _mask_names(fields)
    _check_mask(keys, _DRIVE_FILE_FIELDS)
    return None if "*" in keys else (keys or None)


def _drive_project(files: list[dict], keys: set[str] | None) -> list[dict]:
    return files if not keys else [{k: v for k, v in f.items() if k in keys} for f in files]


def _drive_fill_shared(conn, files: list[dict], stored: set[str]) -> None:
    """Resolve ``shared`` for one page of stored files, in one query. Objects not in ``stored`` are
    the synthesized folders, left alone: their sharing comes from the files they hold, not from a
    grant on the folder id.

    ``stored`` is the SET of served file ids that came from a row. An ACL grant names a file by
    that same id, so plain set membership is exact."""
    have = store.docs_with_grants(
        conn, "google_drive", [f["id"] for f in files if f["id"] in stored]
    )
    for f in files:
        if f["id"] in stored:
            f["shared"] = f["id"] in have


# --- `orderBy` -----------------------------------------------------------------------------


def _natural_key(name: str) -> list[tuple]:
    """Drive's ``name_natural``: digit runs compare numerically, so ``v2`` sorts before ``v10``."""
    return [
        (0, int(t), "") if t.isdigit() else (1, 0, t.casefold())
        for t in re.split(r"(\d+)", name)
        if t
    ]


# Real Drive's documented `orderBy` keys -> the sort key each takes from the served file object
# (sorting what the client actually sees, so folders and stored rows order together). Names sort
# case-insensitively, the way Drive's collation presents them. `recency` is Drive's "most recent
# by any signal"; Backlot models exactly one modification timestamp, which stands in for it.
_DRIVE_ORDER_KEYS = {
    "createdTime": lambda f: f.get("createdTime") or "",
    "modifiedTime": lambda f: f.get("modifiedTime") or "",
    "recency": lambda f: f.get("modifiedTime") or "",
    "name": lambda f: (f.get("name") or "").casefold(),
    "name_natural": lambda f: _natural_key(f.get("name") or ""),
    "folder": lambda f: f.get("mimeType") != DRIVE_FOLDER_MIME,  # folders first
    "starred": lambda f: bool(f.get("starred")),
    "quotaBytesUsed": lambda f: int(f.get("quotaBytesUsed") or f.get("size") or 0),
    # Sortable because Backlot DOES model the relation behind it — owner vs caller — even though it
    # records no share event (see _shared_with_me_time). Absent for the admin/service token, where
    # every key ties and the order falls back to the id, as it would on real Drive over nulls.
    "sharedWithMeTime": lambda f: f.get("sharedWithMeTime") or "",
}
# Documented by Drive, but derived from per-caller signals Backlot does not model at all: nothing
# here is ever viewed or modified *by* anyone in particular. Sorting by one of these could only be a
# no-op, and a silently unapplied sort is the very failure this fix is about — so they 400, which
# tells a consumer "verify this against real Drive" instead of quietly agreeing.
_DRIVE_ORDER_UNMODELLED = ("viewedByMeTime", "modifiedByMeTime")


def _drive_order_specs(order_by: str | None) -> list[tuple]:
    """Parse ``orderBy`` — comma-separated keys, each optionally suffixed ``desc`` — into
    ``(key function, reverse)`` pairs. An unusable key is a 400, as on the real API — accepting one
    and not applying it would let a client relying on server-side ordering pass here and misbehave
    against the real thing."""
    specs = []
    for tok in (order_by or "").split(","):
        parts = tok.split()
        if not parts:
            continue
        key = parts[0]
        if len(parts) > 2 or (len(parts) == 2 and parts[1] != "desc"):
            raise gerr.invalid_value("orderBy", f"Invalid sort key: {tok.strip()}")
        if key in _DRIVE_ORDER_UNMODELLED:
            raise gerr.invalid_value(
                "orderBy",
                f"Sorting by '{key}' is not supported by Backlot (it models no per-caller "
                f"view/share timestamps). Supported: {', '.join(sorted(_DRIVE_ORDER_KEYS))}.",
            )
        if key not in _DRIVE_ORDER_KEYS:
            raise gerr.invalid_value("orderBy", f"Invalid sort key: {tok.strip()}")
        specs.append((_DRIVE_ORDER_KEYS[key], len(parts) == 2))
    return specs


def _drive_sort(files: list[dict], specs: list[tuple]) -> list[dict]:
    """Apply the keys last-first: Python's sort is stable, so the first key wins. The id pre-sort
    makes ties deterministic, which is what keeps a sorted walk from repeating or skipping a row
    across pages."""
    files.sort(key=lambda f: f.get("id") or "")
    for keyfn, reverse in reversed(specs):
        files.sort(key=keyfn, reverse=reverse)
    return files


def _drive_q_plain_folder(query) -> bool:
    """True when the query is just a folder scope (``'<id>' in parents``, and the ``trashed =
    false`` every query carries) with no other clause — the shape a tree-walking client sends,
    servable straight from SQL."""
    return _drive_q_is_conjunction(query) and all(
        t.field == "parents" or t == _QTerm("trashed", "=", "false")
        for t in _drive_q_conjuncts(query)
    )


def _drive_q_excludes_folders(conjuncts: list[_QTerm]) -> bool:
    """True when a term every match has to satisfy is a mimeType no folder can satisfy. Only an
    optimization — ``_drive_q_eval`` would reject them anyway — but it skips building the folder
    stream (and its per-folder ACL probes) for the common query that only wants files."""
    for t in conjuncts:
        if t.field != "mimeType":
            continue
        if t.op == "contains":
            if t.value not in DRIVE_FOLDER_MIME:
                return True
        elif (t.op == "=") != (t.value == DRIVE_FOLDER_MIME):
            return True
    return False


def _drive_folder_candidates(conn, ids, query, me: str | None, fulltext: dict) -> list[dict]:
    """The caller's visible folders as file objects, filtered by the query through the same
    evaluator stored rows go through — so ``mimeType='…folder'`` finds them, not only
    ``'root' in parents``, and they honor the ``fields`` projection like any other row.

    A ``fullText contains`` term matches no folder: a folder's only text is its name (Backlot's
    index covers document content, not container names), so its id is never among the index's
    answers."""
    if _drive_q_excludes_folders(_drive_q_conjuncts(query)):
        return []
    return [
        f
        for f in (_drive_folder_obj(conn, n, me) for n in _visible_drive_folders(conn, ids))
        if query is None or _drive_q_eval(query, _drive_obj_facts(f), me, fulltext)
    ]


def _drive_shared_with_me_scope(
    conjuncts: list[_QTerm], me: str | None
) -> tuple[str | None, str | None]:
    """``sharedWithMe`` as an SQL owner filter — ``(author_email, not_author_email)``. Drive's
    "Shared with me" is a first-class listing a client pages through, so the half of the corpus it
    can never contain is excluded in SQL rather than materialized and dropped in Python."""
    term = next((t for t in conjuncts if t.field == "sharedWithMe"), None)
    if term is None or not me:
        return None, None
    return (None, me) if (term.value == "true") == (term.op == "=") else (me, None)


def _drive_q_rows(conn, query, container: str | None, ids, me: str | None, hits: dict) -> list:
    """Rows matching a non-trivial query: build the smallest candidate set SQL can produce from the
    terms every match has to satisfy, then evaluate the whole query in Python.

    ``hits`` is ``_drive_q_fulltext``'s answer for this query, so the index is asked once."""
    conjuncts = _drive_q_conjuncts(query)
    fulltext = next((t for t in conjuncts if t.field == "fullText"), None)
    name = next((t for t in conjuncts if t.field == "name" and t.op == "contains"), None)
    # `list_drive_by_name` answers non-trashed rows only, so it can be the candidate set only when
    # every match is non-trashed — `trashed = false` a conjunct, not merely present: under a `not`
    # it asks for the trash.
    non_trashed = _QTerm("trashed", "=", "false") in conjuncts or (
        _QTerm("trashed", "!=", "true") in conjuncts
    )
    if fulltext is not None:  # the index's candidates, in rank order, then the other terms
        candidates = hits[fulltext.value]
    elif name is not None and non_trashed:
        # A name lookup (mirage resolves every gdrive file this way) — SQL title LIKE instead of
        # materializing the whole corpus (~25k rows, ~1.6s) to substring-match in Python. The
        # remaining terms still filter the (small) name-matched set below.
        candidates = store.list_drive_by_name(conn, name.value, container, ids, limit=100_000)
    else:  # scope to the folder and/or the owner (if any) to shrink the set before the filter
        owner, not_owner = _drive_shared_with_me_scope(conjuncts, me)
        candidates = store.list_documents(
            conn,
            "google_drive",
            container=container,
            visible_ids=ids,
            limit=100_000,
            author_email=owner,
            not_author_email=not_owner,
        )
    ids_by_value = _drive_q_fulltext_ids(hits)
    return [r for r in candidates if _drive_q_eval(query, _drive_facts(r), me, ids_by_value)]


# --- about ---------------------------------------------------------------------------------

# Every field of the Drive v3 `about` resource, for the same reason `_DRIVE_FILE_FIELDS` is the
# whole documented set: real Drive accepts a documented name it has no value for and rejects an
# unknown one with 400, so validating against it is what lets a test catch a typo'd mask.
_DRIVE_ABOUT_FIELDS = frozenset(
    """
    appInstalled canCreateDrives canCreateTeamDrives driveThemes exportFormats folderColorPalette
    importFormats kind maxImportSizes maxUploadSize storageQuota teamDriveThemes user
""".split()
)


def _drive_about_field_keys(fields: str | None) -> set[str] | None:
    """``about.get`` is the one Drive read whose ``fields`` mask is MANDATORY — the resource has no
    default projection, and real Drive 400s without one. ``None`` = serve everything (``*``).

    A mask that parses to no names at all (``fields=,``) 400s rather than falling through to "no
    projection": on a resource where the mask is required, answering a request for nothing with
    everything is the one outcome the caller certainly did not ask for."""
    if not (fields or "").strip():
        raise gerr.required("fields", "The 'fields' parameter is required for this method.")
    keys = _mask_names(fields)
    _check_mask(keys, _DRIVE_ABOUT_FIELDS)
    if not keys:
        raise gerr.invalid_parameter("fields", f"Invalid field selection {fields}")
    return None if "*" in keys else keys


# The conversion tables below describe the *API's* capabilities, not this account's, so they carry
# Google's real values even though Backlot is read-only: a client that reads them to decide what
# to ask for must branch the same way it would against real Drive.

# What `files.export` can turn each native type into. Kept to the three native types Backlot
# actually stores (`_NATIVE` minus the folder, which is not exportable anywhere).
_DRIVE_EXPORT_FORMATS = {
    DRIVE_DOC_MIME: [
        "application/rtf",
        "application/vnd.oasis.opendocument.text",
        "text/html",
        "application/pdf",
        "application/epub+zip",
        "application/zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
    ],
    "application/vnd.google-apps.spreadsheet": [
        "application/x-vnd.oasis.opendocument.spreadsheet",
        "text/tab-separated-values",
        "application/pdf",
        "application/vnd.oasis.opendocument.spreadsheet",
        "text/csv",
        "application/zip",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ],
    "application/vnd.google-apps.presentation": [
        "application/vnd.oasis.opendocument.presentation",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/plain",
    ],
}

# Source type -> the native types Drive can convert it into on upload. Google's map is longer;
# this is the part that covers every format Backlot's own corpus contains (native docs, Office
# files, PDFs, delimited text, images), so a client's lookup for a real file resolves.
_DRIVE_IMPORT_FORMATS = {
    "application/pdf": [DRIVE_DOC_MIME],
    "application/rtf": [DRIVE_DOC_MIME],
    "text/html": [DRIVE_DOC_MIME],
    "text/plain": [DRIVE_DOC_MIME],
    "application/vnd.oasis.opendocument.text": [DRIVE_DOC_MIME],
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [DRIVE_DOC_MIME],
    "application/msword": [DRIVE_DOC_MIME],
    "image/jpeg": [DRIVE_DOC_MIME],
    "image/png": [DRIVE_DOC_MIME],
    "image/gif": [DRIVE_DOC_MIME],
    "text/csv": ["application/vnd.google-apps.spreadsheet"],
    "text/tab-separated-values": ["application/vnd.google-apps.spreadsheet"],
    "application/vnd.ms-excel": ["application/vnd.google-apps.spreadsheet"],
    "application/vnd.oasis.opendocument.spreadsheet": ["application/vnd.google-apps.spreadsheet"],
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [
        "application/vnd.google-apps.spreadsheet"
    ],
    "application/vnd.ms-powerpoint": ["application/vnd.google-apps.presentation"],
    "application/vnd.oasis.opendocument.presentation": ["application/vnd.google-apps.presentation"],
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": [
        "application/vnd.google-apps.presentation"
    ],
}

_DRIVE_MAX_IMPORT_SIZES = {
    DRIVE_DOC_MIME: "10485760",
    "application/vnd.google-apps.spreadsheet": "104857600",
    "application/vnd.google-apps.presentation": "104857600",
    "application/vnd.google-apps.drawing": "2097152",
}
_DRIVE_MAX_UPLOAD_SIZE = "5242880000000"

# The colors `files.folderColorRgb` may be set to — a documented file field, so the palette a
# client picks from has to be the real one.
_DRIVE_FOLDER_COLORS = [
    "#ac725e",
    "#d06b64",
    "#f83a22",
    "#fa573c",
    "#ff7537",
    "#ffad46",
    "#42d692",
    "#16a765",
    "#7bd148",
    "#b3dc6c",
    "#fbe983",
    "#fad165",
    "#92e1c0",
    "#9fe1e7",
    "#9fc6e7",
    "#4986e7",
    "#9a9cff",
    "#b99aff",
    "#c2c2c2",
    "#cabdbf",
    "#cca6ac",
    "#f691b2",
    "#cd74e6",
    "#a47ae2",
]

# 2 TiB — a fixed plan size. The usage beside it is measured from the corpus, so the pair reads
# like a real account rather than a made-up ratio.
_DRIVE_STORAGE_LIMIT = 2 * 1024**4


@router.get("/drive/v3/about", openapi_extra={"parameters": _P_DRIVE_ABOUT})
async def drive_about(request: Request):
    """Who the caller is and how much space they use — the first call most Drive clients make.

    No ``response_model`` on purpose: real Drive returns strictly what the mask selected, down to
    omitting ``kind``, and a typed model's defaults would put the unasked-for keys back."""
    conn = auth.conn(request)
    caller = _require(request)
    keys = _drive_about_field_keys(request.query_params.get("fields"))  # 400s on absent/unknown
    ids = auth.visible_ids(request, caller)
    # A caller with no mailbox of their own is the admin/service token; real Drive reports a
    # concrete address here either way, as gmail.users.getProfile already does.
    email = caller.email or _service_email(request)
    used, trashed = store.drive_usage_bytes(conn, ids)
    about = {
        "kind": "drive#about",
        "user": _drive_user(email) | {"me": True},  # `about.user` IS the caller
        "storageQuota": {
            "limit": str(_DRIVE_STORAGE_LIMIT),
            # `usage` spans every Google service; Backlot stores nothing outside Drive, so the two
            # are equal. Both include the trash, which is the subset `usageInDriveTrash` reports.
            "usage": str(used),
            "usageInDrive": str(used),
            "usageInDriveTrash": str(trashed),
        },
        "importFormats": _DRIVE_IMPORT_FORMATS,
        "exportFormats": _DRIVE_EXPORT_FORMATS,
        "maxImportSizes": _DRIVE_MAX_IMPORT_SIZES,
        "maxUploadSize": _DRIVE_MAX_UPLOAD_SIZE,
        "appInstalled": False,
        "folderColorPalette": _DRIVE_FOLDER_COLORS,
        # The corpus is all My Drive and /drive/v3/drives is empty, so every shared-drive field
        # says so rather than hinting at a capability that isn't there.
        "canCreateDrives": False,
        "canCreateTeamDrives": False,
        "driveThemes": [],
        "teamDriveThemes": [],
    }
    return _drive_project([about], keys)[0]


@router.get("/drive/v3/drives")
async def drive_shared_drives(request: Request):
    """Shared (Team) Drives — Backlot's corpus lives entirely in My Drive, so this is empty.
    Present so shared-drive-aware clients don't 404 while enumerating."""
    _require(request)
    return {"kind": "drive#driveList", "drives": []}


@router.get(
    "/drive/v3/files", response_model=DriveFileList, openapi_extra={"parameters": _P_DRIVE_LIST}
)
async def drive_files_list(request: Request):
    """A listing is the union of two streams — the stored files and the synthesized folders — put
    through one matcher, one sort and one projection, so a query that should match a folder does
    and every row comes back shaped the way the caller asked for."""
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    me = caller.email
    limit = _int(request, "pageSize", get_settings().default_page_size)
    offset = decode_cursor(request.query_params.get("pageToken"))
    q = request.query_params.get("q", "") or ""
    keys = _drive_file_field_keys(request.query_params.get("fields"))  # 400 on an unknown field
    order = _drive_order_specs(request.query_params.get("orderBy"))  # 400 on an unusable key
    query = _drive_q_parse(q)  # 400 on a clause Backlot cannot evaluate; None when there is no q
    conjuncts = _drive_q_conjuncts(query)
    hits = _drive_q_fulltext(conn, query, ids)
    # A parent every match has to be under; one under an `or` scopes nothing.
    parent_ids = [t.value for t in conjuncts if t.field == "parents"]
    # A folder-scoped parent resolves to one container name (for the SQL-scoped paths below).
    scoped = [pid for pid in parent_ids if pid != "root"]
    container = next((n for pid in scoped if (n := _drive_folder_name_by_id(conn, pid))), None)
    # Backlot's folders all hang directly under the root, so a query scoped inside one can only
    # match files — no folder stream to build.
    folders = (
        []
        if scoped
        else _drive_folder_candidates(conn, ids, query, me, _drive_q_fulltext_ids(hits))
    )

    # The row stream as (count, fetch) so the SQL paths stay SQL-paginated: a crawl costs one page
    # of rows per request, not a full-corpus scan re-run for every page.
    if "root" in parent_ids:
        total_rows, fetch = 0, lambda o, n: []  # every stored file lives in a folder
    elif container is not None and _drive_q_plain_folder(query):
        # The common case: a client walking the tree wants just this folder's files.
        total_rows = store.count_drive_folder(conn, container, ids)
        fetch = lambda o, n: store.list_drive_folder(conn, container, ids, limit=n, offset=o)  # noqa: E731
    elif query is not None:  # filter the visible set by the query, then paginate
        matched = _drive_q_rows(conn, query, container, ids, me, hits)
        total_rows, fetch = len(matched), lambda o, n: matched[o : o + n]  # noqa: E731
    else:
        # exclude_trashed: with no `q` at all there is no query to carry `trashed = false`, and
        # real Drive leaves trashed files out of files.list unless `trashed = true` asks for them.
        # The q-bearing paths already do this (the `trashed = false` _drive_q_parse appends,
        # store.list_drive_folder's WHERE), so without it the DEFAULT listing was the one call that
        # returned trash.
        total_rows = store.count_documents(
            conn, "google_drive", visible_ids=ids, exclude_trashed=True
        )
        fetch = lambda o, n: store.list_documents(  # noqa: E731
            conn, "google_drive", visible_ids=ids, limit=n, offset=o, exclude_trashed=True
        )

    # The file ids that came from the row stream, as opposed to a synthesized folder -- see
    # _drive_fill_shared, which marks only those.
    stored: set[str] = set()

    def objects(o: int, n: int, *, with_shared: bool = True) -> list[dict]:
        rows = fetch(o, n) if n > 0 else []
        stored.update(r["id"] for r in rows)
        shared = (
            store.docs_with_grants(conn, "google_drive", [r["id"] for r in rows])
            if with_shared
            else ()
        )
        return [_drive_file(conn, r, shared=r["id"] in shared, me=me) for r in rows]

    total = total_rows + len(folders)
    if order:
        # A sort spans the whole result set, so it needs the whole set: paging in SQL would order
        # each page in isolation. Materializing the corpus costs more than a paged listing, which
        # is why it happens only when a sort is actually asked for — and `shared`, the one field
        # that costs a query per page and that no sort key reads, is deferred to the page below.
        files = _drive_sort(objects(0, total_rows, with_shared=False) + folders, order)[
            offset : offset + limit
        ]
        _drive_fill_shared(conn, files, stored)
    else:
        # No sort: the stored rows first (SQL-paginated), the folder objects as the tail. Real
        # Drive leaves the default order unspecified, and keeping folders last means a client that
        # reads files[0] out of an unfiltered listing still gets a file.
        files = objects(offset, min(limit, max(0, total_rows - offset)))
        if len(files) < limit:
            start = max(0, offset - total_rows)
            files += folders[start : start + limit - len(files)]
    body = {
        "kind": "drive#fileList",
        "incompleteSearch": False,
        "files": _drive_project(files, keys),
    }
    token = next_page_token(offset, len(files), total)
    if token:
        body["nextPageToken"] = token
    return body


@router.get("/drive/v3/files/{file_id}", openapi_extra={"parameters": _P_DRIVE_ALT})
async def drive_files_get(file_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = store.gdrive_by_id(conn, file_id, visible_ids=ids)
    if row is None:
        name = _drive_folder_name_by_id(conn, file_id)  # folders aren't stored as rows
        if name is not None:
            keys = _drive_get_field_keys(request.query_params.get("fields"))
            return _drive_project([_drive_folder_obj(conn, name, caller.email)], keys)[0]
        raise gerr.not_found_file(file_id)
    if request.query_params.get("alt") == "media":
        # raw download — real API errors on native Docs-editors types (use export)
        if _native(row) is not None:
            raise gerr.not_downloadable()
        mime = row["mime_type"] or "application/octet-stream"
        return Response(row["content"].encode("utf-8"), media_type=mime)
    # Same projection as files.list: a file resolved by id and the same file read out of a listing
    # must come back identical, or caching/diffing behaves differently depending on which call
    # produced the row.
    keys = _drive_get_field_keys(request.query_params.get("fields"))
    return _drive_project([_drive_file(conn, row, me=caller.email)], keys)[0]


@router.get("/drive/v3/files/{file_id}/export", openapi_extra={"parameters": _P_DRIVE_EXPORT})
async def drive_files_export(file_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = store.gdrive_by_id(conn, file_id, visible_ids=ids)
    if row is None:
        raise gerr.not_found_file(file_id)
    native = _native(row)
    if native is None or native[2] is None:  # binary or folder — not exportable
        raise gerr.not_exportable()
    requested = request.query_params.get("mimeType")
    if not requested:  # the real API requires an explicit target format
        raise gerr.required("mimeType")
    # honor the requested target format; CSV/TSV serve the cells, others prefix the title.
    #
    # CSV needs no branch for either kind of document: a document that STATES a grid has its
    # `content` derived from the first sheet's CSV at import, and one that does not has the text it
    # always had. TSV is a DIFFERENT serialisation of the same cells — measured, it has no quoting
    # mechanism and collapses an embedded newline or tab to a single space — so a stated grid
    # re-serialises for it. A prose document exports verbatim either way: its cells ARE its lines,
    # so there is nothing to re-serialise.
    if requested == "text/tab-separated-values":
        stored = store.gdrive_sheets_for(conn, file_id)
        if stored:
            grid = json.loads(stored[0]["grid"])
            return PlainTextResponse(sheets_grid.to_tsv(grid), media_type=requested)
    plain = requested in ("text/csv", "text/tab-separated-values")
    body = row["content"] if plain else f"{row['title']}\n\n{row['content']}"
    return PlainTextResponse(body, media_type=requested)


@router.get("/drive/v3/files/{file_id}/permissions", response_model=DrivePermissionList)
async def drive_files_permissions(file_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = store.gdrive_by_id(conn, file_id, visible_ids=ids)
    if row is None:
        # A folder id is a first-class file id on real Drive — files.get answers for one, so
        # permissions.list has to as well. Folders aren't stored as rows, so their sharing comes
        # from the grants on the files they hold.
        name = _drive_folder_name_by_id(conn, file_id)
        if name is None:
            raise gerr.not_found_file(file_id)
        return {
            "kind": "drive#permissionList",
            "permissions": _drive_permissions(conn, file_id, folder=name),
        }
    return {"kind": "drive#permissionList", "permissions": _drive_permissions(conn, row["id"])}


# --- Google Workspace editors read APIs (Docs / Sheets / Slides) ------------------
#
# Drive `files.export` renders a native doc to text, but editor-aware clients (e.g. mirage)
# read the *structured* document straight from the Docs/Sheets/Slides APIs instead. These
# endpoints serve the corpus content shaped into each API's read response, keyed on the same
# Drive file id, and enforce the same ACL as Drive.

# How the real Docs / Sheets / Slides APIs answer an id that is not their own kind of document.
# MEASURED against docs.googleapis.com, sheets.googleapis.com and slides.googleapis.com with real
# OAuth credentials, one call per case:
#
#   target passed to API X                  | response
#   ----------------------------------------|-----------------------------------------------------
#   a DIFFERENT native Workspace type       | 404 NOT_FOUND  "Requested entity was not found."
#   an Office file of X's own family        | 400 FAILED_PRECONDITION  EDITOR_OFFICE
#   any other non-native (pdf/txt/folder/…) | 400 INVALID_ARGUMENT  "Request contains an invalid…"
#   an id that does not exist               | 404 NOT_FOUND  (identical to the first row)
#
# The first row is the counter-intuitive one, and it is why the earlier guess here was wrong: a Doc
# id is not a malformed spreadsheet to the Sheets API, it is simply not an entity that API knows,
# and the response is indistinguishable from an id that never existed.
#
# The Office row is narrower than the widely-cited bug reports (googlesheets4#275 and friends)
# suggest — they only ever show the family that matches. Measured both ways: .xlsx -> Sheets and
# .docx -> Docs give the Office message, while .xlsx -> Docs and .docx -> Sheets give the plain
# invalid-argument one. `.pptx -> Slides` follows the confirmed pattern but was not itself
# measured; no .pptx was available in the probed account.
EDITOR_NOT_FOUND = "Requested entity was not found."
EDITOR_INVALID_ARG = "Request contains an invalid argument."
EDITOR_OFFICE = (
    "This operation is not supported for this document. The document must not be an Office file."
)

# The binary subtypes (importer `_ATT_MIME` keys) each editor API considers its own family.
_EDITOR_OFFICE_FAMILY = {
    "document": {"doc", "docx"},
    "spreadsheet": {"xls", "xlsx"},
    "presentation": {"ppt", "pptx"},
}
_EDITOR_NATIVE = frozenset(_EDITOR_OFFICE_FAMILY)


def _editor_doc(request: Request, file_id: str, *, expect: str):
    """The Drive row behind an editor read, or the error real Google gives for a mismatch.

    ``expect`` is the native subtype this API serves, and every caller names its own — otherwise
    reading a Doc through the Sheets API answers 200 with prose sliced into a "grid", plausible
    enough that a client trusts it rather than noticing the id was wrong.

    Visibility resolves FIRST, so a caller who cannot see the file gets not-found and never a type
    error: the type of a document you cannot access is not something the API should confirm."""
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    # A native Doc/Sheet/Slides id is the SAME id space as Drive's own file id --
    # real Google resolves docs.googleapis.com/etc. off the identical Drive file id, so this has
    # to resolve the file's own id.
    row = store.gdrive_by_id(conn, file_id, visible_ids=ids)
    if row is None:
        # Folders are synthesized rather than stored, so they miss the lookup above. Real Google
        # calls a folder an invalid argument, not a missing entity, so resolve it before giving up.
        if _drive_folder_name_by_id(conn, file_id) is not None:
            raise gerr.invalid_argument(EDITOR_INVALID_ARG)
        raise gerr.not_found_entity()
    # A row with no stored subtype is a document elsewhere in this module (`_native`), so it is one
    # here too — the fallback stays in one place rather than being decided per route.
    subtype = row["subtype"] or "document"
    if subtype == expect:
        return row
    if subtype in _EDITOR_NATIVE:  # a different Workspace type: not this API's entity at all
        raise gerr.not_found_entity()
    if subtype in _EDITOR_OFFICE_FAMILY[expect]:
        raise gerr.failed_precondition(EDITOR_OFFICE)
    raise gerr.invalid_argument(EDITOR_INVALID_ARG)


@router.get("/docs/v1/documents/{document_id}")
async def docs_get(document_id: str, request: Request):
    row = _editor_doc(request, document_id, expect="document")
    # Docs body is an ordered list of structural elements; one paragraph per line.
    content = [{"sectionBreak": {"sectionStyle": {}}}]
    for line in (row["content"] or "").split("\n"):
        content.append(
            {"paragraph": {"elements": [{"textRun": {"content": line + "\n", "textStyle": {}}}]}}
        )
    return {
        "documentId": document_id,
        "title": row["title"],
        "revisionId": synth._digest(document_id)[:24],
        "suggestionsViewMode": "SUGGESTIONS_INLINE",
        "body": {"content": content},
        "documentStyle": {},
        "namedStyles": {"styles": []},
    }


def _sheets_grid(content: str | None) -> list[list[str]]:
    """The stored text as a grid: one row per line, each row a SINGLE cell holding that line
    verbatim. Joined back with ``\\n`` this reproduces the stored content byte-for-byte, which is
    also what ``files.export`` serves — so the two cannot disagree.

    NOT split on a delimiter. Measured over 1,875 real spreadsheet records, none is
    delimiter-uniform CSV: 82.6% are prose, 17.4% prose wrapped around a PIPE-delimited table. So
    comma-splitting manufactures columns out of sentence punctuation. A line break is the only
    structure the stored text carries, so it is the only structure served — and choosing a column
    delimiter is a corpus-owner's decision, which a caller can still make without first undoing a
    guess made here.
    """
    return [[line] for line in (content or "").split("\n")]


# --- the standard query parameters every Sheets read accepts ---------------------------------
#
# Measured on the live API, all on `values.get` unless noted:
#
#   fields           a partial-response mask; see `_gmask`
#   prettyPrint      DEFAULT TRUE -- the body is 2-space indented unless `false` says otherwise,
#                    and an unparseable value is treated as true rather than refused
#   alt              `json` only; `media` is 400 "Unsupported alt type ... for non byte stream
#                    request." and anything else 400 "Invalid value ... for query parameter 'alt'"
#   callback         JSONP: the body is wrapped and the type becomes text/javascript
#   quotaUser        a rate-limit bucket label; any string, including empty, and no effect on the
#                    response -- Backlot enforces no quota, so there is nothing for it to select
#   upload_protocol  accepted and ignored on a read
#
# `key`, `access_token` and `oauth_token` are NOT here: each is an alternative way to authenticate,
# and honouring one means a second credential path through `backlot.auth` rather than a parameter
# this module can read. `uploadType` is not here either -- measured, the real API REFUSES it on a
# read ("Cannot bind query parameter"), so the baseline's "the vendor accepts it" is not what the
# vendor does.
_P_SHEETS_STD = [
    qp("fields"),
    qp("prettyPrint", "boolean"),
    qp("alt"),
    qp("callback"),
    qp("quotaUser"),
    qp("upload_protocol"),
]


def _gmask_parse(mask: str) -> dict:
    """A ``fields`` mask as a nested selection tree; ``{}`` at a leaf means "this whole subtree".

    Measured grammar: ``.`` and ``/`` both descend, ``a(b,c)`` groups a sub-selection, ``,``
    separates siblings, ``*`` selects everything, and a trailing comma is tolerated. A name is
    matched case-sensitively and is not trimmed -- `` spreadsheetId`` with a leading space 400s."""
    tree: dict = {}
    # (node, key-so-far) as the parser descends into a group
    stack, cur, token = [], tree, ""

    def land(node, name):
        if not name:
            return node
        head, _, rest = name.replace("/", ".").partition(".")
        child = node.setdefault(head, {})
        while rest:
            head, _, rest = rest.partition(".")
            child = child.setdefault(head, {})
        return child

    for ch in mask:
        if ch == "(":
            stack.append((cur, token))
            cur, token = land(cur, token), ""
        elif ch == ")":
            if not stack:
                raise gerr.bad_field_mask(mask)
            land(cur, token)
            cur, token = stack.pop()[0], ""
        elif ch == ",":
            land(cur, token)
            token = ""
        else:
            token += ch
    if stack:
        raise gerr.bad_field_mask(mask)
    land(cur, token)
    return tree


def _gmask_check(tree: dict, allowed: dict, path: str = "") -> None:
    """Refuse a name the response has no field for, naming the full path as the real API does.

    Validated against the fields Backlot CAN emit rather than against the whole Sheets schema:
    a mask naming a real field this module does not model -- a cell's `userEnteredFormat`, say --
    400s here where the real API answers 200. Stated rather than hidden; the alternative is to
    accept any name at all, which is how a typo becomes a silently empty response."""
    for name, sub in tree.items():
        if name == "*":
            continue
        full = f"{path}.{name}" if path else name
        if name not in allowed:
            raise gerr.bad_field_mask(full)
        if sub:
            _gmask_check(sub, allowed[name], full)


def _gmask_wants_grid(mask: str | None) -> bool:
    """Whether a `fields` mask reaches the cells, which is what decides the grid when one is set.

    The discovery document says of `includeGridData`, on both methods that take it: "This parameter
    is ignored if a field mask was set in the request." Measured, that is narrower than it reads --
    the mask has to reach `sheets.data`. `fields=sheets` and `fields=sheets.data...` build the
    grid; `fields=*` and `fields=sheets.properties.title` do not, even though the first of those
    selects everything."""
    if not mask:
        return False
    tree = _gmask_parse(mask)
    under = tree.get("sheets")
    return under is not None and (not under or "data" in under)


def _gmask_apply(tree: dict, value):
    """Project ``value`` through a selection tree, mapping over a list rather than indexing it —
    which is what lets ``sheets.properties.title`` reach into every sheet."""
    if not tree or "*" in tree:
        return value
    if isinstance(value, list):
        return [_gmask_apply(tree, v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for name, sub in tree.items():
        if name in value:
            out[name] = _gmask_apply(sub, value[name])
    return out


def _sheets_respond(request: Request, body: dict, allowed: dict) -> Response:
    """One Sheets response, with the standard query parameters applied.

    Order matters and is measured: `fields` narrows the body, then `prettyPrint` decides the
    indentation, then `callback` wraps what is left."""
    alt = request.query_params.get("alt")
    if alt is not None and alt != "json":
        # Measured: `media` gets its own sentence, everything else the generic one.
        raise gerr.invalid_argument(
            f'Unsupported alt type "{alt}" for non byte stream request.'
            if alt == "media"
            else f"Invalid value \"{alt}\" for query parameter 'alt'"
        )
    mask = request.query_params.get("fields")
    if mask:
        tree = _gmask_parse(mask)
        _gmask_check(tree, allowed)
        body = _gmask_apply(tree, body)
    # Measured: indented by default, and only the literal `false` spellings turn it off -- an
    # unparseable value is treated as true rather than refused, unlike the other booleans.
    compact = (request.query_params.get("prettyPrint") or "").casefold() in _SHEETS_FALSE
    # Measured to the byte: compact puts no space after `:` or `,` and ends without a newline,
    # while the indented form is two spaces deep and DOES end with one. Non-ASCII stays raw.
    text = (
        json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        if compact
        else json.dumps(body, ensure_ascii=False, indent=2) + "\n"
    )
    callback = request.query_params.get("callback")
    if callback:
        return Response(
            f"// API callback\n{callback}({text});", media_type="text/javascript; charset=UTF-8"
        )
    return Response(text, media_type="application/json; charset=UTF-8")


# What a `fields` mask may name, per response — the fields these routes actually build. A cell's
# value objects are leaves: their members are the one-of `stringValue`/`numberValue`/`boolValue`,
# which a mask reaches by naming the value itself.
# Google's Color, whose members a mask may name. Spelled out rather than left a leaf: an empty
# subtree would make `...rgbColor.red` a refusal where the real API answers it.
_F_COLOR = {"red": {}, "green": {}, "blue": {}, "alpha": {}}
_F_TEXT_FORMAT = {
    "foregroundColor": _F_COLOR,
    "fontFamily": {},
    "fontSize": {},
    "bold": {},
    "italic": {},
    "strikethrough": {},
    "underline": {},
    "foregroundColorStyle": {"rgbColor": _F_COLOR},
}
_F_CELL = {
    "userEnteredValue": {"stringValue": {}, "numberValue": {}, "boolValue": {}},
    "effectiveValue": {"stringValue": {}, "numberValue": {}, "boolValue": {}},
    "formattedValue": {},
    "effectiveFormat": {
        "backgroundColor": _F_COLOR,
        "padding": {"top": {}, "right": {}, "bottom": {}, "left": {}},
        "horizontalAlignment": {},
        "verticalAlignment": {},
        "wrapStrategy": {},
        "textFormat": _F_TEXT_FORMAT,
        "hyperlinkDisplayType": {},
        "backgroundColorStyle": {"rgbColor": _F_COLOR},
    },
}
_F_GRID_DATA = {
    "startRow": {},
    "startColumn": {},
    "rowData": {"values": _F_CELL},
    "rowMetadata": {"pixelSize": {}},
    "columnMetadata": {"pixelSize": {}},
}
_F_SPREADSHEET = {
    "spreadsheetId": {},
    "spreadsheetUrl": {},
    "properties": {
        "title": {},
        "locale": {},
        "autoRecalc": {},
        "timeZone": {},
        "defaultFormat": {
            "backgroundColor": _F_COLOR,
            "padding": {"top": {}, "right": {}, "bottom": {}, "left": {}},
            "verticalAlignment": {},
            "wrapStrategy": {},
            "textFormat": _F_TEXT_FORMAT,
            "backgroundColorStyle": {"rgbColor": _F_COLOR},
        },
        "spreadsheetTheme": {
            "primaryFontFamily": {},
            "themeColors": {"colorType": {}, "color": {"rgbColor": _F_COLOR}},
        },
    },
    "sheets": {
        "properties": {
            "sheetId": {},
            "title": {},
            "index": {},
            "sheetType": {},
            "gridProperties": {"rowCount": {}, "columnCount": {}},
        },
        "data": _F_GRID_DATA,
    },
}
_F_VALUE_RANGE = {"range": {}, "majorDimension": {}, "values": {}}
_F_BATCH_BY_FILTER = {
    "spreadsheetId": {},
    "valueRanges": {
        "valueRange": {"range": {}, "majorDimension": {}, "values": {}},
        "dataFilters": {"a1Range": {}, "gridRange": {}},
    },
}
_F_BATCH_VALUES = {"spreadsheetId": {}, "valueRanges": _F_VALUE_RANGE}

_P_SHEETS_GET = [
    qp("includeGridData", "boolean"),
    qp("ranges"),
    qp("excludeTablesInBandedRanges", "boolean"),
    *_P_SHEETS_STD,
]


@router.get("/sheets/v4/spreadsheets/{spreadsheet_id}", openapi_extra={"parameters": _P_SHEETS_GET})
async def sheets_get(spreadsheet_id: str, request: Request):
    """The spreadsheet's structure, and its cells only if asked for.

    ``data`` is withheld unless ``includeGridData=true`` — measured: a real workbook answers 4 KB by
    default and 5.7 MB with the flag, and ``ranges`` alone does NOT unlock it. Volunteering the
    whole grid would hand a reader cells the real API never would, so the document it assembles
    would differ between the two backends. With the flag, ``ranges`` scopes the returned rows
    (measured: 5.7 MB -> 11 KB for ``A1:B2``)."""
    row, sheets = _workbook(request, spreadsheet_id)
    mask = request.query_params.get("fields")
    # A mask that reaches the cells decides the grid, and `includeGridData` is then ignored rather
    # than consulted -- the vendor's own wording. Still parsed, so a bad value is still refused.
    grid = _sheets_bool(request, "includeGridData", "include_grid_data")
    if mask:
        grid = _gmask_wants_grid(mask)
    # Validated and then unused, deliberately: it drops the tables that sit inside a banded range,
    # and a corpus states neither tables nor banded ranges, so there is nothing here to exclude.
    # Leaving it unvalidated instead would accept the one thing a client can get wrong about it.
    _sheets_bool(request, "excludeTablesInBandedRanges", "exclude_tables_in_banded_ranges")
    return _sheets_respond(
        request,
        _sheets_book(spreadsheet_id, row, sheets, request.query_params.getlist("ranges"), grid),
        _F_SPREADSHEET,
    )


def _sheets_book(spreadsheet_id: str, row, sheets: list[_Sheet], specs: list[str], grid: bool):
    """The `Spreadsheet` body both `spreadsheets.get` and `:getByDataFilter` answer with.

    ``specs`` is the A1 ranges selecting what to serve — from `ranges` for one and from the data
    filters for the other; empty means every sheet, whole."""
    # Each sheet paired with the cell parts to serve for it: `("", title)` is the whole grid, which
    # is what a sheet nobody named gets. Resolved ONCE, here — a sheet is never re-derived from its
    # own title further down, or one titled like a cell reference (`A1`, `AB`) would come back
    # holding the first sheet's cells, or overflow the grid and fail the whole call.
    wanted: list[tuple[_Sheet, list[tuple[str, str]]]] = [(sh, [("", sh.title)]) for sh in sheets]
    if specs:
        # A range filters the SHEETS ARRAY, not merely the cells: measured, a sheet none touches is
        # absent from the response entirely, and a sheet several touch gets one `data` block each.
        # That holds with or without `includeGridData` — without it the sheet list is still
        # filtered and no block is served.
        per_sheet: dict[int, list[tuple[str, str]]] = {}
        for spec in specs:
            sheet, body = _a1_sheet(spec, sheets)
            per_sheet.setdefault(sheet.index, []).append((body, spec))
        wanted = [(sh, per_sheet[sh.index]) for sh in sheets if sh.index in per_sheet]
    out = []
    for sh, parts in wanted:
        entry = {
            "properties": {
                "sheetId": sh.sheet_id,
                "title": sh.title,
                "index": sh.index,
                "sheetType": "GRID",
                # the grid, not the data extent — see SHEETS_GRID_ROWS
                "gridProperties": {"rowCount": sh.rows, "columnCount": sh.cols},
            }
        }
        if grid:
            entry["data"] = [_sheets_grid_data(sh, body, spec) for body, spec in parts]
        out.append(entry)
    return {
        "spreadsheetId": spreadsheet_id,
        "properties": {
            "title": row["title"],
            "locale": "en_US",
            "autoRecalc": SHEETS_AUTO_RECALC,
            "timeZone": SHEETS_TIME_ZONE,
            "defaultFormat": SHEETS_DEFAULT_FORMAT,
            "spreadsheetTheme": SHEETS_THEME,
        },
        "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
        "sheets": out,
    }


# --- Sheets `values` reads ------------------------------------------------------------------
# `spreadsheets.get` serves the whole structured grid; a client that wants a slice reads
# `values.get`, and one that wants several slices reads `values:batchGet`. Both resolve an A1
# range against the same grid `sheets_get` builds, so the three calls cannot disagree about what
# a cell holds.

SHEETS_SHEET_TITLE = "Sheet1"  # Backlot shapes every spreadsheet as one sheet with this title

# A real sheet's GRID is larger than its data — Sheets creates one at 1000x26 — and every range
# behaviour below is defined against the grid rather than against the occupied cells. Measured on a
# real spreadsheet holding 14 rows: `values/<title>` echoes `A1:Z1000`, `A:A` echoes `A1:A1000`.
# So Backlot declares the same grid. This is API scaffolding, like the synthesized `sheetId` and
# sheet title beside it — not invented cell data, which `_sheets_grid` still refuses to manufacture.
SHEETS_GRID_ROWS = 1000
SHEETS_GRID_COLS = 26

# A cell's `effectiveFormat`, in the order real emits it. Measured: across every cell type a
# corpus can state it varies in ONE field, `horizontalAlignment` -- a string sits left, a number
# right, a boolean centred -- and the rest is the spreadsheet's default format, which nothing in a
# corpus can change. So the whole object is derived rather than stored, the way `formattedValue`
# is.
#
# `userEnteredFormat` is the other half of the pair and is NOT emitted. Measured, it carries only
# what was explicitly set on that cell -- a strict subset of `effectiveFormat`, absent entirely
# from a cell nobody formatted. Of 17 typed cells the only ones that had one were the percent,
# date, datetime, time and scientific cells, each carrying a lone `numberFormat` that Sheets
# INFERRED from what was typed. A corpus states no formatting and none of those value types, so no
# cell it can describe has anything to put there -- not because the field resists derivation, but
# because nothing a corpus says would trigger one.
_CELL_FORMAT_ALIGN = {"str": "LEFT", "num": "RIGHT", "bool": "CENTER"}
_CELL_FORMAT_REST = {
    "verticalAlignment": "BOTTOM",
    "wrapStrategy": "OVERFLOW_CELL",
    "textFormat": {
        "foregroundColor": {},
        "fontFamily": "Arial",
        "fontSize": 10,
        "bold": False,
        "italic": False,
        "strikethrough": False,
        "underline": False,
        "foregroundColorStyle": {"rgbColor": {}},
    },
    "hyperlinkDisplayType": "PLAIN_TEXT",
    "backgroundColorStyle": {"rgbColor": {"red": 1, "green": 1, "blue": 1}},
}


def _sheets_format(cell) -> dict:
    """The `effectiveFormat` a cell of this type carries."""
    if isinstance(cell, bool):
        align = _CELL_FORMAT_ALIGN["bool"]
    elif isinstance(cell, (int, float)):
        align = _CELL_FORMAT_ALIGN["num"]
    else:
        align = _CELL_FORMAT_ALIGN["str"]
    return {
        "backgroundColor": {"red": 1, "green": 1, "blue": 1},
        "padding": {"top": 2, "right": 3, "bottom": 2, "left": 3},
        "horizontalAlignment": align,
        **_CELL_FORMAT_REST,
    }


# A track's default size in pixels, carried by every `rowMetadata`/`columnMetadata` entry a
# `GridData` block holds. Measured on a real workbook: every row entry is `{"pixelSize": 21}` and
# every column entry `{"pixelSize": 100}`. A corpus states no track size, so nothing varies.
SHEETS_ROW_PIXELS = 21
SHEETS_COL_PIXELS = 100

# The spreadsheet-level format a freshly created workbook carries. Unlike a cell's
# `effectiveFormat`, which the cell's own type decides, these two are SETTINGS: measured across six
# real workbooks they came in three variants, differing where someone had applied a theme or the
# document had been imported from .xlsx (Malgun Gothic, 11pt, MIDDLE alignment). A corpus states
# none of that, so what is served is the variant a new spreadsheet has -- the same footing as
# `locale`, `autoRecalc`, `timeZone` and the 1000x26 grid beside them.
#
# `foregroundColor: {}` and the TEXT theme colour's `rgbColor: {}` are black: proto3 drops a zero,
# so an all-zero colour is the empty object rather than three zeroes.
SHEETS_DEFAULT_FORMAT = {
    "backgroundColor": {"red": 1, "green": 1, "blue": 1},
    "padding": {"top": 2, "right": 3, "bottom": 2, "left": 3},
    "verticalAlignment": "BOTTOM",
    "wrapStrategy": "OVERFLOW_CELL",
    "textFormat": {
        "foregroundColor": {},
        # the CSS stack, where a CELL's textFormat resolves to the single family "Arial"
        "fontFamily": "arial,sans,sans-serif",
        "fontSize": 10,
        "bold": False,
        "italic": False,
        "strikethrough": False,
        "underline": False,
        "foregroundColorStyle": {"rgbColor": {}},
    },
    "backgroundColorStyle": {"rgbColor": {"red": 1, "green": 1, "blue": 1}},
}
SHEETS_THEME = {
    "primaryFontFamily": "Arial",
    "themeColors": [
        {"colorType": "TEXT", "color": {"rgbColor": {}}},
        {"colorType": "BACKGROUND", "color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}},
        {
            "colorType": "ACCENT1",
            "color": {"rgbColor": {"red": 0.25882354, "green": 0.52156866, "blue": 0.95686275}},
        },
        {
            "colorType": "ACCENT2",
            "color": {"rgbColor": {"red": 0.91764706, "green": 0.2627451, "blue": 0.20784314}},
        },
        {
            "colorType": "ACCENT3",
            "color": {"rgbColor": {"red": 0.9843137, "green": 0.7372549, "blue": 0.015686275}},
        },
        {
            "colorType": "ACCENT4",
            "color": {"rgbColor": {"red": 0.20392157, "green": 0.65882355, "blue": 0.3254902}},
        },
        {
            "colorType": "ACCENT5",
            "color": {"rgbColor": {"red": 1, "green": 0.42745098, "blue": 0.003921569}},
        },
        {
            "colorType": "ACCENT6",
            "color": {"rgbColor": {"red": 0.27450982, "green": 0.7411765, "blue": 0.7764706}},
        },
        {
            "colorType": "LINK",
            "color": {"rgbColor": {"red": 0.06666667, "green": 0.33333334, "blue": 0.8}},
        },
    ],
}

# `properties` fields real Sheets always carries beside `title` and `locale`. `ON_CHANGE` is the
# recalculation setting a spreadsheet has unless someone changes it; `Etc/GMT` is the neutral zone,
# and matches what a freshly created spreadsheet answered with.
SHEETS_AUTO_RECALC = "ON_CHANGE"
SHEETS_TIME_ZONE = "Etc/GMT"

_A1_MAJOR = ("ROWS", "COLUMNS")
_A1_RENDER = ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")
_A1_DATETIME = ("SERIAL_NUMBER", "FORMATTED_STRING")
_SHEETS_ENUM = "type.googleapis.com/google.apps.sheets.v4"
# One endpoint of an A1 range: a full cell (`B2`), a bare column (`B`) or a bare row (`2`). A bare
# column or row is an endpoint only INSIDE a range — measured 2026-09-12, `Sheet1!B`, `Sheet1!2` and
# a bang-less `Z` are all "Unable to parse range" while `A:A` and `1:1` answer — and row 0 is not
# one at all (`A0`, `A1:A0` are unparseable too).
_A1_END = re.compile(r"(?:(?P<col>[A-Za-z]{1,3})(?P<row>\d+)?|(?P<rowonly>\d+))\Z")
# One endpoint in R1C1 notation, which the discovery document names beside A1 for `values.get`'s
# `range` ("The A1 notation or R1C1 notation of the range to retrieve values from"), and which
# the LlamaIndex `GoogleSheetsReader` sends for every sheet as `R1C1:R{rowCount}C{columnCount}`.
#
# The grammar below is measured, not read off a document: 217 requests against a real workbook on
# 2026-09-12 (#174), every one pinned in `tests/test_google.py::MEASURED_R1C1`.
#
# * `R` and `C` each take an ABSOLUTE 1-based number (`R1C1`), a BRACKETED 0-based offset from A1
#   (`R[1]C[1]` is B2, `R[0]C[0]` is A1 — the concepts guide's "relative to the current cell" has
#   A1 as the current cell on a read), or no number at all (`R1C` is A1, `RC` is A1). Absolute 0
#   and a negative or `+`-signed offset are unparseable; a leading zero is fine (`R01C01` is A1).
# * either letter may be absent. Beside an R1C1 half a numbered `R2` is row 2 with the columns
#   unbounded (`R1C1:R2` is A1:Z2) and `C[1]` alone is column B whole (`B1:B1000`); a bare `R`, `C`
#   or `RC` is the cell A1.
# * both halves of a range are read in ONE notation: `A1:R2C2` and `R1C1:B2` are unparseable. A
#   token is read as A1 first (`R1` is the cell R1, `RC1` the cell RC1, `RC:RC` the columns RC),
#   then as R1C1 (`RC` alone is A1, and so is `R1C1` even in a workbook with a sheet named `R1C1`),
#   then — bang-less — as a sheet name (`A` is the sheet `A` when there is one).
# * a reversed R1C1 range is swapped the way an A1 one is (`R3C3:R1C1` is A1:C3), except that on
#   an axis where both halves carry a number OF THE SAME KIND — both absolute, or both offsets —
#   start == end + 1 on the numbers AS WRITTEN is unparseable: `R2C2:R1C1`, `R1C2:R1C1`,
#   `R[1]C[1]:R[0]C[0]` and `R2:R1C1` 400 while `R3C3:R1C1` and `R[2]C[2]:R[0]C[0]` answer. An
#   axis that mixes the kinds swaps freely: `R[1]C[1]:R1C1`, `R[2]C[2]:R1C1` and `R1C1:R[0]C[0]`
#   all answer. That reads as a half-open interval on the raw numbers coming out empty, checked
#   before offsets are resolved; the rule was fitted on 22 reversed ranges, the 17 predictions made
#   from it before they were sent all held, and the same-kind clause was then added for the 4
#   mixed-kind rows the first version got wrong.
# * the echo is the A1 equivalent, and the grid rules are A1's: an end past the grid is clamped
#   (`R1C1:R2000C50` is A1:Z1000, `1:1001` is A1:Z1000), a start past it is refused (`R[1000]C[0]`
#   names `A1001`), and a refused whole column or row is named by its letters or numbers alone
#   (`C[26]` names `AA`, `R[1000]:R[1000]` names `1001`, `1001:1002` names `1001:1002`).
# * whitespace is refused wherever it was tried — around the whole, around the bang, around the
#   colon, inside a token, inside a quoted title, a tab as much as a space: ` A1`, `Sheet1! A1`,
#   `A1: B2`, `R1 C1`, `R[ 1]C[1]`, `'Sheet1 '!A1` — all 36 forms sent — are unparseable. An EMPTY title
#   before the bang is the first sheet (`!A1`, `''!A1`, `!R[1]C[1]`), a whitespace one is not.
_R1C1_END = re.compile(
    r"(?P<r>R(?:(?P<rabs>\d+)|\[(?P<rrel>\d+)\])?)?(?P<c>C(?:(?P<cabs>\d+)|\[(?P<crel>\d+)\])?)?\Z",
    re.IGNORECASE,
)


class _End(NamedTuple):
    """One side of a range, resolved: 0-based, ``None`` on an axis left unbounded. ``raw_row`` and
    ``raw_col`` are the number as written and its kind — ``(2, "abs")`` for `R2`, ``(2, "rel")``
    for `R[2]` — which is what real's reversed-range check reads; ``None`` where the axis carries no
    number."""

    row: int | None
    col: int | None
    raw_row: tuple[int, str] | None = None
    raw_col: tuple[int, str] | None = None


def _a1_end(part: str) -> _End | None:
    """An A1 endpoint, or ``None`` when the string is not one."""
    m = _A1_END.fullmatch(part)
    if not m:
        return None
    if m.group("rowonly"):
        n = int(m.group("rowonly"))
        return _End(n - 1, None) if n else None
    row = m.group("row")
    if row is not None and int(row) == 0:
        return None
    return _End(int(row) - 1 if row else None, _a1_col(m.group("col")))


def _r1c1_end(part: str) -> _End | None:
    """An R1C1 endpoint, or ``None`` when the string is not one — the grammar above."""
    m = _R1C1_END.fullmatch(part)
    if not m or not (m.group("r") or m.group("c")):
        return None

    def numbered(letter: str) -> tuple[int, tuple[int, str]] | None:
        """``(index, (raw, kind))`` for a letter that carries a number; ``None`` for one that does
        not."""
        absolute, relative = m.group(letter + "abs"), m.group(letter + "rel")
        if absolute is not None:
            n = int(absolute)
            if n == 0:
                raise ValueError(part)
            return n - 1, (n, "abs")
        if relative is not None:
            n = int(relative)
            return n, (n, "rel")
        return None

    try:
        rnum, cnum = numbered("r"), numbered("c")
    except ValueError:
        return None

    def index(letter: str, own, other) -> int | None:
        if own is not None:
            return own[0]
        if m.group(letter):  # present without a number: offset 0
            return 0
        return None if other is not None else 0  # absent: unbounded beside a numbered letter

    return _End(
        index("r", rnum, cnum),
        index("c", cnum, rnum),
        rnum[1] if rnum else None,
        cnum[1] if cnum else None,
    )


def _a1_classify(body: str) -> tuple[str, list[_End]] | None:
    """Which notation a cell part is in, with its endpoints: ``("a1", ends)``, ``("r1c1", ends)``,
    or ``None`` when it is neither. A1 first, then R1C1 — the order measured — and a lone A1 token
    has to be a full cell, since a bare column or row is an endpoint only inside a range."""
    halves = body.split(":")
    if len(halves) > 2:
        return None
    a1 = [_a1_end(h) for h in halves]
    if all(a1) and (len(a1) == 2 or (a1[0].row is not None and a1[0].col is not None)):
        return "a1", a1  # type: ignore[return-value]
    r1c1 = [_r1c1_end(h) for h in halves]
    if all(r1c1):
        return "r1c1", r1c1  # type: ignore[return-value]
    return None


def _a1_col(letters: str) -> int:
    """Column letters to a 0-based index, base-26 with no zero digit (``A``->0, ``Z``->25,
    ``AA``->26)."""
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _a1_enum_error(field: str, enum: str, value: str) -> str:
    """Google's own wording for a bad read enum — it names the proto field and message type, e.g.
    ``Invalid value at 'major_dimension' (…sheets.v4.Dimension), "DIAGONAL"``. Measured, because a
    client that matches on the message needs the real one."""
    return f"Invalid value at '{field}' ({_SHEETS_ENUM}.{enum}), \"{value}\""


# A protobuf JSON boolean, as the Sheets query parser takes one. Measured: case-insensitive, and
# `on`/`off`, a padded `" true"`, `2`, `01` and `1.0` are all refused.
_SHEETS_TRUE = frozenset({"1", "t", "true", "y", "yes"})
_SHEETS_FALSE = frozenset({"0", "f", "false", "n", "no"})


def _sheets_bool(request: Request, param: str, field: str) -> bool:
    """One of the boolean query params, parsed the way the real one is."""
    return _sheets_bool_value(request.query_params.get(param), field)


def _sheets_bool_value(raw, field: str) -> bool:
    """The rule itself, so a read that carries the flag in a JSON BODY applies the same one.

    Measured: `1`, `t`, `y` and `yes` mean true and `0`, `f`, `n` and `no` mean false, matched
    case-insensitively; anything else 400s as ``Invalid value at '<field>' (TYPE_BOOL), "<value>"``,
    naming the proto TYPE rather than a message. An absent flag is false; an EMPTY one is not
    absent and 400s.

    A JSON body may carry a real boolean, which is taken as itself -- `bool()` on the raw value
    would otherwise make the STRING "false" true, which under no reading it is."""
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    folded = str(raw).casefold()
    if folded in _SHEETS_TRUE:
        return True
    if folded in _SHEETS_FALSE:
        return False
    raise gerr.invalid_field_value(f"Invalid value at '{field}' (TYPE_BOOL), \"{raw}\"")


def _a1_find(title: str, sheets: list[_Sheet]) -> _Sheet | None:
    """The sheet a title names, or None.

    Measured: lookup is CASE-INSENSITIVE (`data!B1:B3` answers from `Data`), quoting is optional
    even for a title holding a space, a colon, brackets or a bang, and inside quotes a doubled
    apostrophe is one apostrophe."""
    if title[:1] == "'" and title[-1:] == "'":
        title = title[1:-1].replace("''", "'")
    fold = title.casefold()
    return next((s for s in sheets if s.title.casefold() == fold), None)


def _a1_looks_like_a_range(body: str) -> bool:
    return _a1_classify(body) is not None


def _a1_sheet(spec: str, sheets: list[_Sheet]) -> tuple[_Sheet, str]:
    """``(sheet, body)`` — which sheet a spec addresses, and the cell part left over (``""`` for a
    bare sheet name, meaning the whole grid).

    Measured against a real multi-sheet workbook:

    * the separator is the LAST bang, not the first — `has!bang!A1` addresses the sheet `has!bang`,
      and splitting at the first would leave `bang!A1` as the cell part
    * a spec with NO bang is parsed as a range FIRST and only then as a sheet name, so bare `A1` is
      cell A1 of the first sheet even in a workbook that has a sheet named `A1`, while bare `Data`
      is the sheet because four letters cannot be a cell reference. R1C1 before a sheet name too:
      bare `R1C1` and `RC` are the cell A1 in a workbook with sheets so named, and bare `A` is the
      sheet `A`, a lone column being no range (measured 2026-09-12)
    * an unqualified range answers from the sheet at index 0
    * a name no sheet has 400s with the same `Unable to parse range` message unparseable garbage
      gets — resolving to an empty grid instead would be indistinguishable from an empty range
    """
    # Not stripped anywhere: measured, ` A1`, `A1 `, ` R1C1 `, `Sheet1! A1`, `Sheet1 !A1` and
    # `'Data' !A1` are all "Unable to parse range", spaces and all.
    bare = spec
    if "!" in bare:
        title, _, body = bare.rpartition("!")
        if title in ("", "''") and _a1_looks_like_a_range(body):
            # An EMPTY title is the first sheet, as an unqualified range is: measured, `!A1`,
            # `''!A1`, `!A1:B2` and `!R[1]C[1]` answer from `Sheet1`. Only for a range — `!Data`
            # and a bare `!` are unparseable, as is a title that is only whitespace (` !A1`).
            return sheets[0], body
        found = _a1_find(title, sheets)
        if found is not None and body:
            return found, body
        # A title that itself holds a bang, named bare: `has!bang` is the WHOLE sheet, so the
        # split above leaves `has` (no such sheet) over `bang` (no such range). Measured.
        # `Sheet1!` with nothing after it falls here too, and is malformed either way.
        whole = _a1_find(bare, sheets)
        if whole is None:
            raise gerr.invalid_argument(f"Unable to parse range: {spec}")
        return whole, ""
    if _a1_looks_like_a_range(bare):
        return sheets[0], bare
    found = _a1_find(bare, sheets)
    if found is None:
        raise gerr.invalid_argument(f"Unable to parse range: {spec}")
    return found, ""


def _a1_range(spec: str, body: str, sheet: _Sheet) -> tuple[int, int, int, int]:
    """Resolve the cell part of an A1 range to half-open ``(r0, c0, r1, c1)`` against this sheet.

    Handles every form a client may send: ``A1:B2``, ``B2`` (one cell), ``A:B`` / ``1:3`` (whole
    columns / rows), ``A2:B`` (one edge unbounded), ``R1C1:R2C2`` (either half in R1C1) and ``""``
    (the whole sheet, which is what a bare sheet name resolves to). Everything resolves against the
    GRID, so a range may be wider than the data — the caller trims.

    Two boundary rules, measured against a real spreadsheet: the range's END may overflow and is
    CLAMPED (``A1:AA5`` on a 26-column sheet returns ``A1:Z5``), its START may not.

    ``spec`` is the whole requested range, because that — not the offending half — is what real
    Sheets names back: `A1:` reports "Unable to parse range: A1:", never a bare "".
    """
    nrows, ncols = sheet.rows, sheet.cols
    if not body:
        return 0, 0, nrows, ncols
    classified = _a1_classify(body)
    if classified is None:
        raise gerr.invalid_argument(f"Unable to parse range: {spec}")
    notation, ends = classified
    if len(ends) == 1:  # a single reference: one cell, one whole row, one column
        (start,) = ends
        r0f, c0f = (0 if start.row is None else start.row), (0 if start.col is None else start.col)
        r1 = nrows if start.row is None else start.row + 1
        c1 = ncols if start.col is None else start.col + 1
    else:
        start, end = ends
        if notation == "r1c1":
            # Real's reversed-range rule, on the numbers as written — see `_R1C1_END`.
            for a, b in ((start.raw_row, end.raw_row), (start.raw_col, end.raw_col)):
                if a is not None and b is not None and a[1] == b[1] and a[0] == b[0] + 1:
                    raise gerr.invalid_argument(f"Unable to parse range: {spec}")
        r0f = 0 if start.row is None else start.row
        c0f = 0 if start.col is None else start.col
        r1 = nrows if end.row is None else end.row + 1
        c1 = ncols if end.col is None else end.col + 1
        # Ranges are inclusive and may be written in either order (`B2:A1` == `A1:B2`), in R1C1
        # as in A1 (`R3C3:R1C1` == `A1:C3`), once past the rule above.
        if r1 < r0f + 1:
            r0f, r1 = r1 - 1, r0f + 1
        if c1 < c0f + 1:
            c0f, c1 = c1 - 1, c0f + 1
    if r0f >= nrows or c0f >= ncols or r0f < 0 or c0f < 0:
        # The START is outside the grid — refused, with the range echoed back unclamped; whole
        # columns by their letters alone and whole rows by their numbers alone (`_a1_axis_name`).
        if all(e.row is None for e in ends):
            named = _a1_axis_name(sheet, _a1_col_letters(c0f), _a1_col_letters(c1 - 1))
        elif all(e.col is None for e in ends):
            named = _a1_axis_name(sheet, str(r0f + 1), str(r1))
        else:
            named = _a1_name(sheet, r0f, c0f, r1, c1)
        raise gerr.invalid_argument(
            f"Range ({named}) exceeds grid limits. Max rows: {nrows}, max columns: {ncols}"
        )
    return r0f, c0f, min(r1, nrows), min(c1, ncols)


# A title the echo may spell without quotes. Measured: `Data` echoes bare while `'Second Sheet'`,
# `'2024'`, `'Bob''s Sheet'`, `'a:b'`, `'a[b]'`, `'has!bang'` and `'A1'` echo quoted. This pattern
# (plus the A1-reference exclusion below) is INFERRED from that sample rather than measured: it
# accounts for every title measured, but a title the sample does not cover could contradict it.
_A1_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _a1_title(title: str) -> str:
    """A sheet title as the echoed ``range`` spells it, quoted unless it is a plain identifier that
    is not itself a cell reference. An embedded apostrophe doubles.

    Measured 2026-09-12 on sheets so named: `R1C1` and `RC` echo quoted (`'RC'!A1`), as `A1` does,
    while `A` — a bare column, which is no reference on its own — echoes bare (`A!A1:Z1000`)."""
    if _A1_PLAIN.fullmatch(title) and _a1_classify(title) is None:
        return title
    return "'" + title.replace("'", "''") + "'"


def _a1_col_letters(i: int) -> str:
    """A 0-based column index as A1 letters — the inverse of :func:`_a1_col`."""
    s = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s


def _a1_axis_name(sheet: _Sheet, start: str, end: str) -> str:
    """Whole columns by their letters alone, or whole rows by their numbers alone, the way real
    names them when refusing a range past the grid: measured, `ZZ:ZZ` reports ``Sheet1!ZZ``,
    `AA:AB` ``Sheet1!AA:AB``, `1001:1001` ``Sheet1!1001`` and `1001:1002` ``Sheet1!1001:1002``, never
    ``ZZ1:ZZ1000`` or ``A1001:Z1001``. One column or row collapses as one cell does; a lone `C[26]`
    reports ``Sheet1!AA`` the same way."""
    title = _a1_title(sheet.title)
    return f"{title}!{start}" if start == end else f"{title}!{start}:{end}"


def _a1_name(sheet: _Sheet, r0: int, c0: int, r1: int, c1: int) -> str:
    """The resolved range in A1 form, which is what the response echoes.

    A single cell echoes as a bare reference (``Sheet1!A1``), not as ``A1:A1`` — measured: real
    Sheets collapses a 1x1 range even when the request spelled it out as ``A1:A1``."""
    title = _a1_title(sheet.title)
    start = f"{_a1_col_letters(c0)}{r0 + 1}"
    if r1 - r0 == 1 and c1 - c0 == 1:
        return f"{title}!{start}"
    return f"{title}!{start}:{_a1_col_letters(c1 - 1)}{r1}"


def _rstrip_empty(cells: list[str]) -> list[str]:
    while cells and cells[-1] == "":
        cells.pop()
    return cells


def _sheets_block(sheet: _Sheet, body: str, spec: str):
    """``(r0, c0, r1, c1, cells)`` for the cell part ``body`` against ``sheet``: the range as
    resolved against that sheet's grid, and the cells it covers as STORED — trimming happens in the
    caller, which knows how it is rendering them. ``spec`` is only what an error echoes.

    Takes an already-resolved sheet rather than re-parsing one out of a spec, so a caller that
    knows which sheet it wants cannot have it reinterpreted: a title reading as a cell or column
    reference (``A1``, ``AB``) would resolve against the FIRST sheet instead of itself.

    The bounds are the RANGE's, not the data's: callers echo them, so they must not shrink to the
    occupied cells."""
    rows = sheet.grid
    r0, c0, r1, c1 = _a1_range(spec, body, sheet)
    block = [
        [(rows[r][c] if c < len(rows[r]) else None) for c in range(c0, c1)]
        for r in range(r0, min(r1, len(rows)))
    ]
    return r0, c0, r1, c1, block


def _sheets_value(cell) -> dict:
    """A cell's ``ExtendedValue``.

    Measured: a string is ``stringValue``, a number ``numberValue``, a boolean ``boolValue``, and
    an empty cell carries no value object at all.

    ``bool`` is tested BEFORE the numeric branch because it is a subclass of ``int`` in Python —
    unguarded, TRUE would serve as ``numberValue: 1``.

    A real cell may also hold ``formulaValue`` (in ``userEnteredValue``) or ``errorValue`` (in
    ``effectiveValue``). A corpus states neither, so neither is emitted."""
    if cell is None or cell == "":
        return {}
    if isinstance(cell, bool):
        return {"boolValue": cell}
    if isinstance(cell, (int, float)):
        return {"numberValue": cell}
    return {"stringValue": cell}


def _sheets_grid_data(sheet: _Sheet, body: str, spec: str) -> dict:
    """One ``GridData`` block for ``spreadsheets.get?includeGridData=true``.

    Measured: a cell object per column of the range, an empty one carrying no value;
    ``startRow``/``startColumn`` omitted when zero, proto3 dropping its defaults; and no
    ``rowData`` key at all on an empty sheet, whose block is metadata alone.

    ``userEnteredValue`` and ``effectiveValue`` are equal here and both absent from an empty cell.
    Measured, they differ on real Sheets only for a FORMULA cell — the formula in the first, its
    result in the second — and a corpus cannot state a formula, so there is nothing to differ over.

    ``rowMetadata``/``columnMetadata`` cover the RANGE, one entry per row and column of it —
    measured, 2 and 2 for ``Data!A1:B2`` against the same sheet whose unscoped block carries 1000
    and 26. Every entry is identical (``pixelSize`` 21 for a row, 100 for a column), those being
    the default track sizes; a corpus states no track size, so there is nothing to vary.

    One divergence, stated rather than hidden: real Sheets pads ``rowData`` out to the WHOLE
    1000-row grid where this stops at the last row holding data."""
    r0, c0, r1x, c1, block = _sheets_block(sheet, body, spec)
    width = c1 - c0
    while block and all(sheets_grid.formatted(c) == "" for c in block[-1]):
        block.pop()
    out: dict = {}
    if r0:
        out["startRow"] = r0
    if c0:
        out["startColumn"] = c0
    out["rowMetadata"] = [{"pixelSize": SHEETS_ROW_PIXELS} for _ in range(r1x - r0)]
    out["columnMetadata"] = [{"pixelSize": SHEETS_COL_PIXELS} for _ in range(width)]
    if block:
        out["rowData"] = [
            {
                "values": [
                    (
                        {
                            "userEnteredValue": v,
                            "effectiveValue": v,
                            "formattedValue": sheets_grid.formatted(row[i]),
                            "effectiveFormat": _sheets_format(row[i]),
                        }
                        if i < len(row) and (v := _sheets_value(row[i]))
                        else {}
                    )
                    for i in range(width)
                ]
            }
            for row in block
        ]
    return out


def _sheets_render(cell, render: str):
    """One cell as ``values.get`` returns it under ``render``.

    Measured: ``FORMATTED_VALUE`` gives the display string, while ``UNFORMATTED_VALUE`` and
    ``FORMULA`` both give the raw typed value — a JSON number for a number, a JSON boolean for a
    boolean. The two agree for every NON-FORMULA cell on the real API, and a corpus states no
    formulas, so they agree here for every cell.

    An empty cell is ``""`` under every option, measured — a JSON string even under
    ``UNFORMATTED_VALUE``, never null."""
    if cell is None:
        return ""
    return sheets_grid.formatted(cell) if render == "FORMATTED_VALUE" else cell


def _sheets_value_range(spec: str, sheets: list[_Sheet], major: str, render: str) -> dict:
    """One ``ValueRange``.

    Trailing empty cells are dropped PER ROW rather than padded out to the requested bounds, so
    rows come back ragged; an interior gap stays ``""``; trailing empty rows are dropped
    altogether. Under ``COLUMNS`` the same rule applies per column, and a fully empty INTERIOR
    column comes back as ``[]`` rather than being dropped. All measured.

    A range holding nothing omits ``values`` entirely — a client tests for the key's presence, so
    an empty list would claim the range exists and is blank."""
    sheet, body = _a1_sheet(spec, sheets)
    r0, c0, r1, c1, raw = _sheets_block(sheet, body, spec)
    block = [[_sheets_render(c, render) for c in row] for row in raw]
    out = {"range": _a1_name(sheet, r0, c0, r1, c1), "majorDimension": major}
    if major == "COLUMNS":
        width = max((len(r) for r in block), default=0)
        block = [_rstrip_empty([(r[i] if i < len(r) else "") for r in block]) for i in range(width)]
    else:
        block = [_rstrip_empty(row) for row in block]
    while block and not block[-1]:
        block.pop()
    if block:
        out["values"] = block
    return out


class _Sheet(NamedTuple):
    """One sheet of a workbook, however the corpus stated it."""

    sheet_id: int
    index: int
    title: str
    grid: list[list]

    @property
    def rows(self) -> int:
        """The declared grid's height — never smaller than the data, and at least the default."""
        return max(SHEETS_GRID_ROWS, len(self.grid))

    @property
    def cols(self) -> int:
        return max(SHEETS_GRID_COLS, max((len(r) for r in self.grid), default=0))


def _workbook(request: Request, spreadsheet_id: str) -> tuple:
    """``(row, sheets)`` — the Drive row behind a spreadsheet and the sheets it serves.

    A document that STATES a grid answers with its stored sheets; one that does not answers with a
    SINGLE synthesized sheet holding ``_sheets_grid``'s line-per-cell reading. Prose is therefore
    one more grid, which is what leaves a single serving path below and stops the three Sheets
    calls disagreeing about a cell.

    A prose sheet's cells are strings and STAY strings. Nothing here sniffs a line for a number or
    a boolean: a cell's type is something a corpus states, never something this module infers."""
    row = _editor_doc(request, spreadsheet_id, expect="spreadsheet")
    stored = store.gdrive_sheets_for(auth.conn(request), spreadsheet_id)
    if not stored:
        return row, [_Sheet(0, 0, SHEETS_SHEET_TITLE, _sheets_grid(row["content"]))]
    return row, [
        _Sheet(s["sheet_id"], s["sheet_index"], s["title"], json.loads(s["grid"])) for s in stored
    ]


def _sheets_enum(request: Request, param: str, field: str, enum: str, allowed, default: str) -> str:
    """One of the read enums, validated and canonicalised.

    Measured, and identical for all three: the match is CASE-INSENSITIVE (``majorDimension=rows``
    answers 200) and the response echoes the canonical upper-case spelling whatever the request
    used; an unknown value 400s, naming the proto field and type and quoting the value as the
    client sent it; and an EMPTY value is not an absent one — it 400s rather than falling back to
    the default."""
    return _sheets_enum_value(request.query_params.get(param), field, enum, allowed, default)


def _sheets_enum_value(raw, field: str, enum: str, allowed, default: str) -> str:
    """The rule itself, so a read that carries its enums in a JSON BODY applies the same one.

    Absent means the default; present means validated, and an empty string is present."""
    if raw is None:
        return default
    value = str(raw).upper()
    if value not in allowed:
        raise gerr.invalid_field_value(_a1_enum_error(field, enum, raw))
    return value


def _sheets_options(request: Request) -> tuple[str, str]:
    """Validate the read enums and return ``(majorDimension, valueRenderOption)``. Real Sheets 400s
    on an unknown value; accepting one silently would hand back ROWS-shaped data to a client that
    asked for columns, and a silently unapplied option is worse than a refusal.
    """
    major = _sheets_enum(
        request, "majorDimension", "major_dimension", "Dimension", _A1_MAJOR, "ROWS"
    )
    # Measured over typed cells: FORMATTED_VALUE gives the display string "12", UNFORMATTED_VALUE
    # the JSON number 12, and FORMULA the same raw value as UNFORMATTED_VALUE for every cell that
    # is not a formula. A spreadsheet whose cells are lines of stored text has only strings, so
    # all three agree on one; a spreadsheet that STATES its grid does not.
    render = _sheets_enum(
        request,
        "valueRenderOption",
        "value_render_option",
        "ValueRenderOption",
        _A1_RENDER,
        "FORMATTED_VALUE",
    )
    # Validated and then unused, deliberately. It selects between a date cell's serial number and
    # its formatted string, and a corpus states no date cells — every cell is a string, a number, a
    # boolean or empty — so the two renderings coincide here. Leaving it unvalidated instead would
    # accept the one thing a client can get wrong about it.
    _sheets_enum(
        request,
        "dateTimeRenderOption",
        "date_time_render_option",
        "DateTimeRenderOption",
        _A1_DATETIME,
        "SERIAL_NUMBER",
    )
    return major, render


_P_SHEETS_VALUES = [
    qp("majorDimension"),
    qp("valueRenderOption"),
    qp("dateTimeRenderOption"),
    *_P_SHEETS_STD,
]
_P_SHEETS_BATCH = [qp("ranges"), *_P_SHEETS_VALUES]


@router.get(
    "/sheets/v4/spreadsheets/{spreadsheet_id}/values:batchGet",
    openapi_extra={"parameters": _P_SHEETS_BATCH},
)
async def sheets_values_batch_get(spreadsheet_id: str, request: Request):
    """Several ranges in one round trip. Declared before ``values/{range}`` for clarity only —
    ``values:batchGet`` is a single path segment, so the two cannot collide.

    One unusable range fails the whole call rather than yielding a short ``valueRanges`` list: a
    partial batch leaves the caller unable to say which range it is missing.

    With no ``ranges`` at all, nothing is selected and ``valueRanges`` is omitted. NOTE: that is
    the natural reading of a parameter with no default, NOT a response diffed against real
    Sheets — unlike the rest of this module's behaviour, it is unverified."""
    _row, sheets = _workbook(request, spreadsheet_id)
    major, render = _sheets_options(request)
    ranges = request.query_params.getlist("ranges")
    body = {"spreadsheetId": spreadsheet_id}
    if ranges:
        body["valueRanges"] = [_sheets_value_range(r, sheets, major, render) for r in ranges]
    return _sheets_respond(request, body, _F_BATCH_VALUES)


@router.get(
    "/sheets/v4/spreadsheets/{spreadsheet_id}/values/{a1_range:path}",
    openapi_extra={"parameters": _P_SHEETS_VALUES},
)
async def sheets_values_get(spreadsheet_id: str, a1_range: str, request: Request):
    """One range of a spreadsheet, ACL-enforced through the same lookup as ``spreadsheets.get``."""
    _row, sheets = _workbook(request, spreadsheet_id)
    major, render = _sheets_options(request)
    return _sheets_respond(
        request, _sheets_value_range(a1_range, sheets, major, render), _F_VALUE_RANGE
    )


# --- the two reads issued over POST ------------------------------------------------------------
#
# A DataFilter selects the same cells an A1 range does, by range or by grid indices. Measured, the
# two endpoints disagree about an ABSENT filter list: `values:batchGetByDataFilter` refuses it
# ("Must specify at least one dataFilter.") while `spreadsheets:getByDataFilter` treats it as
# "every sheet". They also word a bad range differently — only the values one prefixes
# `Invalid dataFilter[N]: `.


def _sheets_filter_spec(f: dict, sheets: list[_Sheet]) -> str:
    """One DataFilter as an A1 spec.

    A `gridRange` is turned into A1 through :func:`_a1_name`, whose output is quoted where the
    title needs it — which is what makes handing it back to the parser safe, unlike a bare title.
    Half-open indices, and an omitted bound means that edge of the grid."""
    if isinstance(f.get("a1Range"), str):
        return f["a1Range"]
    grid = f.get("gridRange")
    if not isinstance(grid, dict):
        raise gerr.invalid_argument("dataFilter.filter must be specified.")
    sheet_id = _sheets_int32(grid.get("sheetId"), "sheetId", 0)
    sheet = next((s for s in sheets if s.sheet_id == sheet_id), None)
    if sheet is None:
        raise gerr.invalid_argument(f"No sheet with id: {sheet_id}")
    r0 = _sheets_int32(grid.get("startRowIndex"), "startRowIndex", 0)
    c0 = _sheets_int32(grid.get("startColumnIndex"), "startColumnIndex", 0)
    r1 = _sheets_int32(grid.get("endRowIndex"), "endRowIndex", sheet.rows)
    c1 = _sheets_int32(grid.get("endColumnIndex"), "endColumnIndex", sheet.cols)
    # Half-open and ascending. An end at or before its start selects nothing, and letting it
    # through builds an A1 name with a row 0 in it -- `Data!A1:Z0` -- which the parser then reads
    # back as a start row of -1 and answers a range nobody asked for.
    if r1 <= r0 or c1 <= c0:
        raise gerr.invalid_argument(
            f"Invalid gridRange: end must be greater than start, got rows [{r0}, {r1}) "
            f"and columns [{c0}, {c1})"
        )
    return _a1_name(sheet, r0, c0, r1, c1)


def _sheets_int32(raw, field: str, default: int) -> int:
    """One int32 member of a `gridRange`.

    The discovery document declares these `int32`, and proto3's JSON mapping takes a number or a
    decimal string for one, so `"0"` resolves like `0`. What it does not take is a float with a
    fraction, a non-numeric string or a negative index -- each of which reached `_a1_name`
    unchecked before, turning a client's typo into a 500 or into a silently truncated index.

    Backlot's own wording: the real API's message for these was not measured."""
    if raw is None:
        return default
    if isinstance(raw, bool):
        raise gerr.invalid_field_value(f"Invalid value at '{field}' (TYPE_INT32), \"{raw}\"")
    value = raw
    if isinstance(value, str):
        try:
            value = int(value, 10)
        except ValueError:
            raise gerr.invalid_field_value(
                f"Invalid value at '{field}' (TYPE_INT32), \"{raw}\""
            ) from None
    if isinstance(value, float):
        if not value.is_integer():
            raise gerr.invalid_field_value(f"Invalid value at '{field}' (TYPE_INT32), \"{raw}\"")
        value = int(value)
    if not isinstance(value, int) or value < 0:
        raise gerr.invalid_field_value(f"Invalid value at '{field}' (TYPE_INT32), \"{raw}\"")
    return value


async def _sheets_filters(request: Request, sheets: list[_Sheet], *, required: bool, indexed: bool):
    """``(body, specs)`` for a by-data-filter read: the parsed request body and one A1 spec per
    filter, in the order they were sent.

    ``indexed`` says whether a bad filter is reported behind an ``Invalid dataFilter[N]: `` prefix.
    Measured, the two endpoints differ: the values-level one names the index, the spreadsheet-level
    one gives the bare parse error."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    filters = body.get("dataFilters") or []
    if not filters:
        if required:
            raise gerr.invalid_argument("Must specify at least one dataFilter.")
        return body, []
    specs = []
    for i, f in enumerate(filters):
        try:
            spec = _sheets_filter_spec(f if isinstance(f, dict) else {}, sheets)
            # Resolved HERE, not left to the read below, so a range that names no sheet is
            # reported against the filter that carried it — measured, the message is the usual
            # `Unable to parse range` behind an `Invalid dataFilter[N]: ` prefix.
            _a1_sheet(spec, sheets)
        except Exception as exc:  # noqa: BLE001 — re-raised with the index the API names
            message = getattr(exc, "message", None)
            if message is None:
                raise
            raise gerr.invalid_argument(
                f"Invalid dataFilter[{i}]: {message}" if indexed else message
            ) from None
        specs.append(spec)
    return body, specs


@router.post(
    "/sheets/v4/spreadsheets/{spreadsheet_id}/values:batchGetByDataFilter",
    openapi_extra={"parameters": _P_SHEETS_STD},
)
async def sheets_values_batch_get_by_data_filter(spreadsheet_id: str, request: Request):
    """``values:batchGet`` addressed by DataFilter rather than by A1 string.

    A read, issued over POST because the filters do not fit in a query string. Each entry carries
    the ``valueRange`` AND the filter that selected it — measured — so a caller that sent several
    can tell which answer belongs to which."""
    _row, sheets = _workbook(request, spreadsheet_id)
    body, specs = await _sheets_filters(request, sheets, required=True, indexed=True)
    # The same three enums the query-string reads take, and the same rule for them — including
    # that an empty value is not an absent one, and that `dateTimeRenderOption` is validated even
    # though a corpus states no date cell for it to render.
    major = _sheets_enum_value(
        body.get("majorDimension"), "major_dimension", "Dimension", _A1_MAJOR, "ROWS"
    )
    render = _sheets_enum_value(
        body.get("valueRenderOption"),
        "value_render_option",
        "ValueRenderOption",
        _A1_RENDER,
        "FORMATTED_VALUE",
    )
    _sheets_enum_value(
        body.get("dateTimeRenderOption"),
        "date_time_render_option",
        "DateTimeRenderOption",
        _A1_DATETIME,
        "SERIAL_NUMBER",
    )

    # NOT the order the filters arrived in. Measured: the answers come back sorted by where each
    # range starts, column before row — `Data!A2` precedes `Data!B1`, `Data!B9` precedes
    # `Data!B10`, a shorter range precedes the one that extends it, and a sheet earlier in the
    # workbook comes first. Each entry still carries the filter that selected it, so a caller pairs
    # by that rather than by position.
    def where(i: int):
        sheet, part = _a1_sheet(specs[i], sheets)
        r0, c0, r1, c1 = _a1_range(specs[i], part, sheet)
        return (sheet.index, c0, r0, c1, r1)

    out = {
        "spreadsheetId": spreadsheet_id,
        "valueRanges": [
            {
                "valueRange": _sheets_value_range(specs[i], sheets, major, render),
                "dataFilters": [body["dataFilters"][i]],
            }
            for i in sorted(range(len(specs)), key=where)
        ],
    }
    return _sheets_respond(request, out, _F_BATCH_BY_FILTER)


@router.post(
    "/sheets/v4/spreadsheets/{spreadsheet_id}:getByDataFilter",
    openapi_extra={"parameters": _P_SHEETS_STD},
)
async def sheets_get_by_data_filter(spreadsheet_id: str, request: Request):
    """``spreadsheets.get`` addressed by DataFilter. Same response, and the filters scope the
    ``sheets`` array exactly as ``ranges`` does — measured, including that NO filter means every
    sheet rather than the refusal its values-level sibling gives."""
    row, sheets = _workbook(request, spreadsheet_id)
    body, specs = await _sheets_filters(request, sheets, required=False, indexed=False)
    grid = _sheets_bool_value(body.get("includeGridData"), "include_grid_data")
    if mask := request.query_params.get("fields"):
        grid = _gmask_wants_grid(mask)
    return _sheets_respond(
        request, _sheets_book(spreadsheet_id, row, sheets, specs, grid), _F_SPREADSHEET
    )


@router.get("/slides/v1/presentations/{presentation_id}")
async def slides_get(presentation_id: str, request: Request):
    row = _editor_doc(request, presentation_id, expect="presentation")
    chunks = [c for c in (row["content"] or "").split("\n\n") if c.strip()] or [
        row["content"] or ""
    ]
    slides = []
    for i, chunk in enumerate(chunks):
        slides.append(
            {
                "objectId": f"p{i}",
                "pageType": "SLIDE",
                "pageElements": [
                    {
                        "objectId": f"p{i}_t",
                        "shape": {
                            "shapeType": "TEXT_BOX",
                            "text": {
                                "textElements": [
                                    {"textRun": {"content": chunk + "\n", "style": {}}}
                                ]
                            },
                        },
                    }
                ],
            }
        )
    return {
        "presentationId": presentation_id,
        "title": row["title"],
        "pageSize": {
            "width": {"magnitude": 9144000, "unit": "EMU"},
            "height": {"magnitude": 6858000, "unit": "EMU"},
        },
        "slides": slides,
    }


# Google Workspace native types: subtype -> (mimeType, webView path segment, export content-type)
_NATIVE = {
    "document": ("application/vnd.google-apps.document", "document", "text/plain"),
    "spreadsheet": ("application/vnd.google-apps.spreadsheet", "spreadsheets", "text/csv"),
    "presentation": ("application/vnd.google-apps.presentation", "presentation", "text/plain"),
    "folder": ("application/vnd.google-apps.folder", None, None),
}


def _native(row):
    """Return the _NATIVE tuple for this doc, or None if it's a binary (non-native) file."""
    return _NATIVE.get(row["subtype"] or "document")


def _drive_user(email: str) -> dict:
    return {
        "kind": "drive#user",
        "displayName": email.split("@")[0].replace(".", " ").title(),
        "emailAddress": email,
        "me": False,
        "permissionId": str(synth.github_user_id(email)),
        "photoLink": synth.github_avatar(synth.github_user_id(email)),
    }


def _drive_mime(row) -> str:
    """The mimeType this row serves: a native Workspace type from its subtype, else its own
    declared type (and only a type-less binary falls back to an opaque blob)."""
    native = _native(row)
    return native[0] if native else (row["mime_type"] or "application/octet-stream")


def _drive_file(conn, row, shared: bool | None = None, me: str | None = None) -> dict:
    """The served ``files`` resource for a stored row. ``me`` is the caller's email, which decides
    the per-caller ``ownedByMe`` (None for the admin/service token, which owns nothing)."""
    created = _drive_created(row)
    modified = _drive_modified(row)
    author = row["author_email"]
    native = _native(row)
    mime = _drive_mime(row)
    if native is not None:
        seg = native[1]
        view = (
            f"https://docs.google.com/{seg}/d/{row['id']}/edit"
            if seg
            else f"https://drive.google.com/drive/folders/{row['id']}"
        )
    else:  # binary file (PDF, image, office doc)
        view = f"https://drive.google.com/file/d/{row['id']}/view"
    is_folder = row["subtype"] == "folder"
    # "shared" = visible to anyone besides the owner — true for org/group/multi-reader docs.
    # In a list the caller passes it in (batch-computed); for a single get, look it up here.
    if shared is None:
        shared = bool(store.doc_grants(conn, "google_drive", row["id"]))
    ext = row["title"].rsplit(".", 1)[-1] if (native is None and "." in row["title"]) else None
    nbytes = len((row["content"] or "").encode("utf-8"))
    f = {
        "kind": "drive#file",
        "id": row["id"],
        "name": row["title"],
        "mimeType": mime,
        "parents": store.jcol(row, "parents") or [synth.drive_folder_id(row["folder"])],
        "createdTime": synth.rfc3339(created),
        "modifiedTime": synth.rfc3339(modified),
        "owners": [_drive_user(author)],
        "lastModifyingUser": _drive_user(author),
        "trashed": bool(row["trashed"]),
        "explicitlyTrashed": bool(row["trashed"]),
        "starred": False,
        "shared": bool(shared),
        "viewedByMe": False,
        "ownedByMe": _drive_owned_by(author, me),
        **_shared_with_me_time(author, me, created),
        "version": str(2 if row["updated_ts"] else 1),
        "spaces": ["drive"],
        "webViewLink": view,
        "iconLink": f"https://drive.google.com/icons/{(row['subtype'] or 'document')}.png",
        "capabilities": {
            "canDownload": not is_folder,
            "canListChildren": is_folder,
            "canComment": not is_folder,
            "canEdit": False,
            "canCopy": not is_folder,
            "canShare": True,
            "canRename": False,
            "canTrash": False,
            "canDelete": False,
            "canReadRevisions": not is_folder,
            "canAddChildren": is_folder,
            "canModifyContent": False,
        },
    }
    # Per Google's reference, `size` "is populated for files with binary content stored in Google
    # Drive AND for Docs Editors files; it is not populated for shortcuts or folders" — so a native
    # Doc/Sheet/Slides carries it too. Checksums, a download link and the file-extension pair stay
    # binary-only, which is also what real Drive does for the Docs-editors types.
    if not is_folder:
        f["size"] = str(nbytes)
    if native is None:
        f["md5Checksum"] = hashlib.md5(row["content"].encode()).hexdigest()
        f["quotaBytesUsed"] = str(nbytes)
        f["webContentLink"] = f"https://drive.google.com/uc?id={row['id']}&export=download"
        if ext:
            f["fileExtension"] = ext
            f["fullFileExtension"] = ext
    return f


def _drive_permissions(conn, file_id: str, *, folder: str | None = None) -> list[dict]:
    """Build from the doc's ACL grants (preserving user/group/org identity) + an owner. For a
    synthesized folder, ``folder`` names the container and the grants come from its files (which is
    what makes the folder visible in the first place); Backlot models no folder owner, so there is
    no owner permission to add."""
    grants = (
        store.container_grants(conn, "google_drive", folder)
        if folder
        else store.doc_grants(conn, "google_drive", file_id)
    )
    domain = get_settings().org_domain
    perms = []
    for g in grants:
        ptype, pid = g["principal_type"], g["principal_id"]
        if ptype == "org":  # anyone-in-org / anyone-with-link
            perms.append(
                {
                    "kind": "drive#permission",
                    "id": "anyoneWithLink",
                    "type": "anyone",
                    "role": "reader",
                    "allowFileDiscovery": True,
                }
            )
        elif ptype == "group":
            perms.append(
                {
                    "kind": "drive#permission",
                    "id": str(synth.github_user_id(pid)),
                    "type": "group",
                    "role": "reader",
                    "emailAddress": f"{pid}@{domain}",
                    "displayName": pid,
                }
            )
        else:  # user
            perms.append(
                {
                    "kind": "drive#permission",
                    "id": str(synth.github_user_id(pid)),
                    "type": "user",
                    "role": "reader",
                    "emailAddress": pid,
                    "displayName": pid.split("@")[0].replace(".", " ").title(),
                }
            )
    # every file has an owner
    row = store.get_document(conn, "google_drive", file_id)
    if row is not None:
        owner = row["author_email"]
        perms.insert(
            0,
            {
                "kind": "drive#permission",
                "id": str(synth.github_user_id(owner)),
                "type": "user",
                "role": "owner",
                "emailAddress": owner,
                "displayName": owner.split("@")[0].replace(".", " ").title(),
            },
        )
    return perms


def _int(request: Request, key: str, default: int) -> int:
    v = request.query_params.get(key)
    try:
        return min(int(v), get_settings().max_page_size) if v else default
    except ValueError:
        return default
