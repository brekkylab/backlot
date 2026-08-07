"""Mock Google APIs (read-only): Gmail (``/gmail/v1``), Drive (``/drive/v3``), and the
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
import re
from email.parser import BytesParser
from http import HTTPStatus

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict

from backlot import auth, google_errors as gerr, store, synth
from backlot.openapi import qp
from backlot.acl import Caller
from backlot.config import get_settings
from backlot.pagination import decode_cursor, next_page_token

router = APIRouter(tags=["google"])


# --- OpenAPI enrichment (issue #4 bridge) --------------------------------------------------
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
    async with httpx.AsyncClient(transport=transport, base_url="http://mock.batch") as client:
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


def _mailbox_slug(caller: Caller, user_id: str) -> str | None:
    """Resolve the requested mailbox to its container slug (the ``gmail_messages.mailbox`` value
    the importer derived from the owner's name). None = all mailboxes (admin ``me``). A concrete
    address (``me`` for a user, or an explicit email) maps its local-part to the slug so the WHOLE
    mailbox — received and sent — is scoped, not just messages that address happened to author."""
    email = caller.email if user_id == "me" else (user_id if "@" in user_id else None)
    if not email:
        return None
    return re.sub(r"[^a-z0-9]+", "_", email.split("@")[0].lower()).strip("_")


def _service_email(request: Request) -> str:
    """The identity to report for an admin/service caller that has no single mailbox
    (a bare service account / full-crawl token). Real Gmail always reports a concrete
    address here — never the literal ``me`` path segment — so we use the service account's
    email, falling back to a service address on the org domain."""
    oauth = getattr(request.app.state, "oauth", None)
    if oauth is not None and oauth.client_email:
        return oauth.client_email
    return f"service@{get_settings().org_domain}"


@router.get("/gmail/v1/users/{user_id}/profile")
async def gmail_profile(user_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    # A concrete mailbox (``me`` -> caller.email, or an explicit address) if we have one;
    # otherwise the admin/service identity — never echo the raw ``me`` path segment.
    email = _mailbox_email(caller, user_id) or caller.email or _service_email(request)
    ids = auth.visible_ids(request, caller)
    total = store.count_documents(
        conn, "gmail", container=_mailbox_slug(caller, user_id), visible_ids=ids
    )
    return {"emailAddress": email, "messagesTotal": total, "threadsTotal": total, "historyId": "1"}


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


def _label_obj(lid: str, total: int = 0) -> dict:
    hide = lid in ("SPAM", "TRASH", "CHAT")
    return {
        "id": lid,
        "name": lid,
        "type": "system",
        "messageListVisibility": "hide" if hide else "show",
        "labelListVisibility": "labelHide" if lid.startswith("CATEGORY_") else "labelShow",
        "messagesTotal": total,
        "messagesUnread": 0,
        "threadsTotal": total,
        "threadsUnread": 0,
    }


@router.get("/gmail/v1/users/{user_id}/labels")
async def gmail_labels(user_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    total = store.count_documents(
        conn, "gmail", container=_mailbox_slug(caller, user_id), visible_ids=ids
    )
    labels = [_label_obj(lid, total if lid == "INBOX" else 0) for lid in _SYSTEM_LABELS]
    return {"labels": labels}


@router.get("/gmail/v1/users/{user_id}/labels/{label_id}")
async def gmail_label_get(user_id: str, label_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    if label_id not in _SYSTEM_LABELS:
        raise gerr.not_found_entity()
    ids = auth.visible_ids(request, caller)
    total = store.count_documents(
        conn, "gmail", author_email=_mailbox_email(caller, user_id), visible_ids=ids
    )
    return _label_obj(label_id, total if label_id == "INBOX" else 0)


_GMAIL_OP = re.compile(r'(\w+):("[^"]*"|\S+)')
# operators we honor; anything else stays as free text
_GMAIL_KEYS = {
    "from",
    "to",
    "subject",
    "after",
    "before",
    "label",
    "has",
    "newer_than",
    "older_than",
}


def _parse_gmail_q(q: str) -> tuple[str, dict]:
    """Split a Gmail search `q` into (free_text, operators). Honors from:/to:/subject:/
    after:/before:/newer_than:/older_than:/label:/has: — the rest is free text matched
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
# enough approximation for a mock (the operators are otherwise honored exactly).
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
    for v in ops.get("label", []):
        if v.lower() not in [x.lower() for x in store.jcol(row, "label_ids")]:
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
        # created_ts DESC, doc_id order is the newest-first listing real Gmail returns — the plain
        # list_documents path ordered by doc_id (hash), scattering the listing by date.
        cand = store.list_gmail_in_range(conn, mailbox, lo, hi, ids, limit=100_000)
    return [r for r in cand if _gmail_op_match(r, ops)]


# --- Gmail ids ------------------------------------------------------------------------------
# Served ids are 16-hex integers (`synth.gmail_message_id`), not the corpus's dsids, so every route
# resolves an incoming id back to a row through the startup reverse index — the same shape the
# github / jira / confluence / notion / s3 routes already use. Threads share the map, because a
# thread key IS the root message's doc_id.

_GMAIL_HEX = re.compile(r"[0-9a-fA-F]+\Z")


def _gmail_resolve(request: Request, served_id: str) -> str | None:
    """The ``doc_id`` behind a served Gmail id, or ``None`` if it names nothing.

    An id that Gmail could not parse at all raises instead: measured, the real API answers 400
    INVALID_ARGUMENT "Invalid id value" for a non-hex id or one >= 2**63, and 404 only for a
    well-formed id it does not hold. `7fffffffffffffff` is well-formed; `8000000000000000` is
    not."""
    if not _GMAIL_HEX.fullmatch(served_id) or int(served_id, 16) >= synth.GMAIL_ID_MAX:
        raise gerr.invalid_id_value()
    return request.app.state.index["gmail"].get(served_id.lower())


def _gmail_doc(request: Request, conn, ids, served_id: str):
    """The visible row behind a served id. Resolution happens before the ACL read, so an id that
    resolves to a document the caller cannot see is still not-found, never a different answer."""
    doc_id = _gmail_resolve(request, served_id)
    if doc_id is None:
        return None
    return store.get_document(conn, "gmail", doc_id, visible_ids=ids)


def _gmail_ids(row) -> tuple[str, str]:
    """``(id, threadId)`` for a row. A message that is its own thread root reports the same value
    twice, as real Gmail does."""
    return (
        synth.gmail_message_id(row["doc_id"]),
        synth.gmail_message_id(row["thread_id"] or row["doc_id"]),
    )


@router.get(
    "/gmail/v1/users/{user_id}/messages",
    response_model=GmailMessageList,
    openapi_extra={"parameters": _P_GMAIL_LIST},
)
async def gmail_messages_list(user_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    mailbox = _mailbox_slug(caller, user_id)  # container slug (None = all mailboxes)
    limit = _int(request, "maxResults", get_settings().default_page_size)
    offset = decode_cursor(request.query_params.get("pageToken"))
    q = request.query_params.get("q", "") or ""
    if q.strip():  # search: filter the ACL-visible set by the query, then paginate
        matched = _gmail_query(conn, mailbox, ids, q)
        total = len(matched)
        rows = matched[offset : offset + limit]
    else:
        # newest-first by internalDate (created_ts), like real Gmail — NOT doc_id (hash) order, so a
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
    row = _gmail_doc(request, conn, ids, msg_id)
    if row is None:
        raise gerr.not_found_entity()
    return _gmail_message(row, request.query_params.get("format", "full"))


@router.get(
    "/gmail/v1/users/{user_id}/messages/{msg_id}/attachments/{att_id}",
    response_model=GmailAttachment,
)
async def gmail_attachment(user_id: str, msg_id: str, att_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _gmail_doc(request, conn, ids, msg_id)
    if row is None:
        raise gerr.not_found_entity()
    doc_id = row["doc_id"]
    found = next(
        (
            (i, a)
            for i, a in enumerate(store.jcol(row, "attachments"))
            if _att_id(doc_id, i) == att_id
        ),
        None,
    )
    body = _att_content(doc_id, found[0], found[1]) if found else f"attachment {att_id}"
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
    mailbox = _mailbox_email(caller, user_id)
    limit = _int(request, "maxResults", get_settings().default_page_size)
    offset = decode_cursor(request.query_params.get("pageToken"))
    q = request.query_params.get("q", "") or ""
    if q.strip():
        matched = _gmail_query(conn, mailbox, ids, q)
        total = len(matched)
        rows = matched[offset : offset + limit]
    else:
        total = store.count_documents(conn, "gmail", author_email=mailbox, visible_ids=ids)
        rows = store.list_documents(
            conn, "gmail", author_email=mailbox, visible_ids=ids, limit=limit, offset=offset
        )
    # a thread is keyed by its root; reply rows (thread_seq>0) aren't separate threads
    threads = [
        {"id": _gmail_ids(r)[1], "snippet": r["content"][:200], "historyId": "1"}
        for r in rows
        if (r["thread_seq"] or 0) == 0
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
    thread_key = _gmail_resolve(request, thread_id)
    msgs = store.gmail_thread(conn, thread_key, visible_ids=ids) if thread_key else []
    if not msgs:
        row = _gmail_doc(request, conn, ids, thread_id)
        if row is None:
            raise gerr.not_found_entity()
        msgs = [row]
    fmt = request.query_params.get("format", "full")
    return {
        "id": thread_id.lower(),
        "snippet": msgs[0]["content"][:200],
        "historyId": "1",
        "messages": [_gmail_message(m, fmt) for m in msgs],
    }


def _att_id(doc_id: str, i: int) -> str:
    return "ANGjdJ" + synth.gmail_id(doc_id, salt=f"att{i}")


def _att_content(doc_id: str, i: int, att: dict) -> str:
    """The exact bytes ``attachments.get`` serves for attachment ``i``, and therefore what
    ``messages.get`` reports as that part's ``body.size`` — real Gmail keeps the two equal so a
    client can stat from metadata alone. The corpus-declared ``size`` cannot be honoured with
    placeholder bytes, so the served content's length is the single source of truth."""
    return att.get("content", f"attachment {_att_id(doc_id, i)}")


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
    if row["created_ts"]:
        return row["created_ts"]
    return synth.epoch(row["thread_id"] or row["doc_id"]) + (row["thread_seq"] or 0) * 3600


def _gmail_message(row, fmt: str) -> dict:
    ts = _gmail_ts(row)
    author = row["author_email"]
    display = author.split("@")[0].replace(".", " ").title()
    msg_id = row["message_id"] or f"<{row['doc_id']}@{get_settings().org_domain}>"
    # a fetched (received) message carries transport/MIME headers but NOT Bcc (stripped in transit)
    headers = [
        {
            "name": "Delivered-To",
            "value": row["to_addr"] or f"{row['mailbox']}@{get_settings().org_domain}",
        },
        {"name": "MIME-Version", "value": "1.0"},
        {"name": "Subject", "value": row["title"]},
        {"name": "From", "value": f"{display} <{author}>"},
        {"name": "To", "value": row["to_addr"] or f"{row['mailbox']}@{get_settings().org_domain}"},
        {"name": "Date", "value": synth.rfc2822(ts)},
        {"name": "Message-ID", "value": msg_id},
    ]
    for hname, col in (
        ("Cc", "cc"),
        ("Reply-To", "reply_to"),
        ("In-Reply-To", "in_reply_to"),
        ("References", "refs"),
    ):
        if row[col]:
            headers.append({"name": hname, "value": row[col]})
    attachments = store.jcol(row, "attachments")
    top_mime = "multipart/mixed" if attachments else "multipart/alternative"
    boundary = f"b_{row['doc_id'][:12]}"
    headers.append({"name": "Content-Type", "value": f'{top_mime}; boundary="{boundary}"'})

    msg = {
        "id": _gmail_ids(row)[0],
        "threadId": _gmail_ids(row)[1],
        "labelIds": store.jcol(row, "label_ids") or ["INBOX"],
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
            b64 = base64.b64encode(_att_content(row["doc_id"], i, att).encode("utf-8")).decode(
                "ascii"
            )
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
                    "attachmentId": _att_id(row["doc_id"], i),
                    "size": len(_att_content(row["doc_id"], i, att)),
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

_DRIVE_FULLTEXT_RE = re.compile(r"fullText\s+contains\s+'([^']+)'")
# `sharedWithMe = true|false`, or the bare `sharedWithMe` Drive also accepts (meaning true).
_DRIVE_SHARED_RE = re.compile(r"sharedWithMe\b(?:\s*=\s*(true|false))?")
_DRIVE_MIME_RE = re.compile(r"mimeType\s*(=|!=)\s*'([^']+)'")


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


def _drive_facts(row) -> dict:
    """The values `q` clauses are evaluated against, taken from a stored row."""
    modified = row["updated_ts"] or (row["created_ts"] or synth.epoch(row["doc_id"])) + 3600
    return {
        "trashed": bool(row["trashed"]),
        "parents": store.jcol(row, "parents") or [synth.drive_folder_id(row["folder"])],
        "mime": _drive_mime(row),
        "name": row["title"] or "",
        "modified": synth.rfc3339(modified),
        "owner_email": row["author_email"],
        # real Drive keys `in owners` on the owner's email; the mock also accepts the owner
        # display name, since that's the only owner identifier some callers have.
        "owners": {(row["author_email"] or "").lower(), (row["owner_display"] or "").lower()},
    }


def _drive_obj_facts(obj: dict) -> dict:
    """The same values taken from an already-built file object — the synthesized folders, which
    exist only as objects, are matched through this so every clause treats them like a row."""
    return {
        "trashed": bool(obj.get("trashed")),
        "parents": obj.get("parents") or [],
        "mime": obj.get("mimeType") or "",
        "name": obj.get("name") or "",
        "modified": obj.get("modifiedTime") or "",
        "owner_email": (obj.get("owners") or [{}])[0].get("emailAddress"),
        "owners": {(o.get("emailAddress") or "").lower() for o in (obj.get("owners") or [])},
    }


def _drive_q_match_facts(f: dict, q: str, me: str | None = None) -> bool:
    """Honor the Drive `q` clauses connectors use (folder scoping, mimeType, name contains,
    modifiedTime, trashed, sharedWithMe, in owners). `fullText contains` is handled upstream via
    FTS (see drive_files_list), so it's stripped from `q` before this runs. Unrecognized clauses
    are ignored."""
    m = re.search(r"trashed\s*=\s*(true|false)", q)
    if m:
        if (m.group(1) == "true") != f["trashed"]:
            return False
    elif f["trashed"]:  # real API excludes trashed by default
        return False
    for fid in re.findall(r"'([^']+)'\s+in\s+parents", q):
        if fid not in f["parents"]:
            return False
    m = _DRIVE_MIME_RE.search(q)
    if m and (m.group(1) == "=") != (f["mime"] == m.group(2)):
        return False
    m = re.search(r"name\s+contains\s+'([^']+)'", q)
    if m and m.group(1).lower() not in f["name"].lower():
        return False
    m = re.search(r"modifiedTime\s*>\s*'([^']+)'", q)
    if m and f["modified"] <= m.group(1):
        return False
    # "Shared with me" = visible to the caller and not owned by them. Items shared with you carry
    # no My Drive parent on real Drive, so this clause is the only way to enumerate that section.
    m = _DRIVE_SHARED_RE.search(q)
    if m and ((m.group(1) or "true") == "true") == _drive_owned_by(f["owner_email"], me):
        return False
    for who in re.findall(r"'([^']+)'\s+in\s+owners", q):
        if who.strip().lower() not in f["owners"]:
            return False
    return True


def _drive_q_match(row, q: str, me: str | None = None) -> bool:
    return _drive_q_match_facts(_drive_facts(row), q, me)


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

    The mock models no folder owner, so a folder is never owned by the caller and carries
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
# documented set, not just the keys this mock synthesizes: real Drive accepts a documented field
# it has no value for (and omits it from the response) while rejecting anything unknown with 400.
# Validating against it is what makes a mock-backed test able to catch a typo'd or stale mask.
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
    mock-backed test could catch a mask that 400s in production."""
    for n in sorted(names):
        if n != "*" and n not in allowed:
            raise gerr.invalid_parameter("fields", f"Invalid field selection {n}")


def _drive_file_field_keys(fields: str | None) -> set[str] | None:
    """File keys a ``files.list`` caller selected — so the response carries only those, not the
    full ~30-field object. Google accepts both the group form (``files(id,name)``) and the path
    form (``files/id``); both are honored. ``None`` = no projection (an absent mask, or one that
    asks for everything with ``*``).

    Top-level names are validated but not projected: the mock always returns ``kind`` and
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
    grant on the folder id."""
    have = store.docs_with_grants(conn, [f["id"] for f in files if f["id"] in stored])
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
# by any signal"; the mock models exactly one modification timestamp, which stands in for it.
_DRIVE_ORDER_KEYS = {
    "createdTime": lambda f: f.get("createdTime") or "",
    "modifiedTime": lambda f: f.get("modifiedTime") or "",
    "recency": lambda f: f.get("modifiedTime") or "",
    "name": lambda f: (f.get("name") or "").casefold(),
    "name_natural": lambda f: _natural_key(f.get("name") or ""),
    "folder": lambda f: f.get("mimeType") != DRIVE_FOLDER_MIME,  # folders first
    "starred": lambda f: bool(f.get("starred")),
    "quotaBytesUsed": lambda f: int(f.get("quotaBytesUsed") or f.get("size") or 0),
    # Sortable because the mock DOES model the relation behind it — owner vs caller — even though it
    # records no share event (see _shared_with_me_time). Absent for the admin/service token, where
    # every key ties and the order falls back to the id, as it would on real Drive over nulls.
    "sharedWithMeTime": lambda f: f.get("sharedWithMeTime") or "",
}
# Documented by Drive, but derived from per-caller signals this mock does not model at all: nothing
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
                f"Sorting by '{key}' is not supported by this mock (it models no per-caller "
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


def _drive_q_plain_folder(q: str) -> bool:
    """True when ``q`` is just a folder scope (``'<id>' in parents``, optionally ``trashed=false``)
    with no other clause — the shape a tree-walking client sends, servable straight from SQL."""
    residual = re.sub(r"'[^']+'\s+in\s+parents", " ", q)
    residual = re.sub(r"trashed\s*=\s*false", " ", residual)
    residual = re.sub(r"\band\b", " ", residual, flags=re.IGNORECASE)
    return residual.strip() == ""


def _drive_q_excludes_folders(q: str) -> bool:
    """True when ``q`` carries a mimeType clause no folder can satisfy. Only an optimization —
    ``_drive_q_match_facts`` would reject them anyway — but it skips building the folder stream
    (and its per-folder ACL probes) for the common query that only wants files."""
    m = _DRIVE_MIME_RE.search(q)
    return bool(m) and ((m.group(1) == "=") != (m.group(2) == DRIVE_FOLDER_MIME))


def _drive_folder_candidates(conn, ids, q: str, me: str | None) -> list[dict]:
    """The caller's visible folders as file objects, filtered by ``q`` through the same clause
    matcher stored rows go through — so ``mimeType='…folder'`` finds them, not only
    ``'root' in parents``, and they honor the ``fields`` projection like any other row.

    Skipped for a ``fullText contains`` query: a folder's only text is its name (the mock's index
    covers document content, not container names), so it can't take part in an FTS match."""
    if _DRIVE_FULLTEXT_RE.search(q) or _drive_q_excludes_folders(q):
        return []
    return [
        f
        for f in (_drive_folder_obj(conn, n, me) for n in _visible_drive_folders(conn, ids))
        if _drive_q_match_facts(_drive_obj_facts(f), q, me)
    ]


def _drive_shared_with_me_scope(q: str, me: str | None) -> tuple[str | None, str | None]:
    """``sharedWithMe`` as an SQL owner filter — ``(author_email, not_author_email)``. Drive's
    "Shared with me" is a first-class listing a client pages through, so the half of the corpus it
    can never contain is excluded in SQL rather than materialized and dropped in Python."""
    m = _DRIVE_SHARED_RE.search(q)
    if m is None or not me:
        return None, None
    return (None, me) if (m.group(1) or "true") == "true" else (me, None)


def _drive_q_rows(conn, q: str, container: str | None, ids, me: str | None) -> list:
    """Rows matching a non-trivial ``q``: build the smallest candidate set SQL can produce, then
    apply the remaining clauses in Python."""
    ft = _DRIVE_FULLTEXT_RE.search(q)
    if ft:  # fullText contains → FTS candidates (ranked), then the other q clauses
        # Honor real Drive semantics: a quoted value (`fullText contains '"X Y"'`) is an exact
        # phrase (tokens adjacent); unquoted is separate terms. A grep push-down sends the quoted
        # form for a literal pattern, so the exact doc surfaces instead of being buried under
        # coincidental docs that merely contain the words scattered.
        ft_raw = ft.group(1)
        phrase = len(ft_raw) >= 2 and ft_raw[0] == '"' and ft_raw[-1] == '"'
        ft_term = ft_raw[1:-1] if phrase else ft_raw
        q_rest = _DRIVE_FULLTEXT_RE.sub(" ", q)  # FTS owns fullText; strip it from the rest
        candidates = store.search_documents(
            conn, ft_term, "google_drive", ids, limit=10_000, phrase=phrase
        )
    else:
        q_rest = q
        nm = re.search(r"name\s+contains\s+'([^']+)'", q)
        if nm:  # a name lookup (mirage resolves every gdrive file this way) — SQL title LIKE
            # instead of materializing the whole corpus (~25k rows, ~1.6s) to substring-match in
            # Python. The remaining q clauses still filter the (small) name-matched set below.
            candidates = store.list_drive_by_name(conn, nm.group(1), container, ids, limit=100_000)
        else:  # scope to the folder and/or the owner (if any) to shrink the set before the filter
            owner, not_owner = _drive_shared_with_me_scope(q, me)
            candidates = store.list_documents(
                conn,
                "google_drive",
                container=container,
                visible_ids=ids,
                limit=100_000,
                author_email=owner,
                not_author_email=not_owner,
            )
    return [r for r in candidates if _drive_q_match(r, q_rest, me)]


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
# Google's real values even though the mock is read-only: a client that reads them to decide what
# to ask for must branch the same way it would against real Drive.

# What `files.export` can turn each native type into. Kept to the three native types the mock
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
# this is the part that covers every format the mock's own corpus contains (native docs, Office
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
            # `usage` spans every Google service; the mock stores nothing outside Drive, so the two
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
    """Shared (Team) Drives — the mock's corpus lives entirely in My Drive, so this is empty.
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
    parent_ids = re.findall(r"'([^']+)'\s+in\s+parents", q)
    # A folder-scoped parent resolves to one container name (for the SQL-scoped paths below).
    scoped = [pid for pid in parent_ids if pid != "root"]
    container = next((n for pid in scoped if (n := _drive_folder_name_by_id(conn, pid))), None)
    # The mock's folders all hang directly under the root, so a query scoped inside one can only
    # match files — no folder stream to build.
    folders = [] if scoped else _drive_folder_candidates(conn, ids, q, me)

    # The row stream as (count, fetch) so the SQL paths stay SQL-paginated: a crawl costs one page
    # of rows per request, not a full-corpus scan re-run for every page.
    if "root" in parent_ids:
        total_rows, fetch = 0, lambda o, n: []  # every stored file lives in a folder
    elif container is not None and _drive_q_plain_folder(q):
        # The common case: a client walking the tree wants just this folder's files.
        total_rows = store.count_drive_folder(conn, container, ids)
        fetch = lambda o, n: store.list_drive_folder(conn, container, ids, limit=n, offset=o)  # noqa: E731
    elif q.strip():  # filter the visible set by the query, then paginate
        matched = _drive_q_rows(conn, q, container, ids, me)
        total_rows, fetch = len(matched), lambda o, n: matched[o : o + n]  # noqa: E731
    else:
        total_rows = store.count_documents(conn, "google_drive", visible_ids=ids)
        fetch = lambda o, n: store.list_documents(  # noqa: E731
            conn, "google_drive", visible_ids=ids, limit=n, offset=o
        )

    stored: set[str] = set()  # ids that came from the row stream (vs. a synthesized folder)

    def objects(o: int, n: int, *, with_shared: bool = True) -> list[dict]:
        rows = fetch(o, n) if n > 0 else []
        stored.update(r["doc_id"] for r in rows)
        shared = store.docs_with_grants(conn, [r["doc_id"] for r in rows]) if with_shared else ()
        return [_drive_file(conn, r, shared=r["doc_id"] in shared, me=me) for r in rows]

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
    row = store.get_document(conn, "google_drive", file_id, visible_ids=ids)
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
    row = store.get_document(conn, "google_drive", file_id, visible_ids=ids)
    if row is None:
        raise gerr.not_found_file(file_id)
    native = _native(row)
    if native is None or native[2] is None:  # binary or folder — not exportable
        raise gerr.not_exportable()
    requested = request.query_params.get("mimeType")
    if not requested:  # the real API requires an explicit target format
        raise gerr.required("mimeType")
    # honor the requested target format; CSV/TSV keep the raw content, others prefix the title
    plain = requested in ("text/csv", "text/tab-separated-values")
    body = row["content"] if plain else f"{row['title']}\n\n{row['content']}"
    return PlainTextResponse(body, media_type=requested)


@router.get("/drive/v3/files/{file_id}/permissions", response_model=DrivePermissionList)
async def drive_files_permissions(file_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = store.get_document(conn, "google_drive", file_id, visible_ids=ids)
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
    return {"kind": "drive#permissionList", "permissions": _drive_permissions(conn, file_id)}


# --- Google Workspace editors read APIs (Docs / Sheets / Slides) ------------------
#
# Drive `files.export` renders a native doc to text, but editor-aware clients (e.g. mirage)
# read the *structured* document straight from the Docs/Sheets/Slides APIs instead. These
# endpoints serve the corpus content shaped into each API's read response, keyed on the same
# Drive file id (the doc_id), and enforce the same ACL as Drive.

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
    row = store.get_document(conn, "google_drive", file_id, visible_ids=ids)
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

    NOT split on a delimiter. Measured over the bench's 1,875 ``doc_type: sheet`` records, none is
    delimiter-uniform CSV: 82.6% are prose, 17.4% prose wrapped around a PIPE-delimited table. So
    comma-splitting manufactures columns out of sentence punctuation. A line break is the only
    structure the stored text carries, so it is the only structure served — and choosing a column
    delimiter is a corpus-owner's decision, which a caller can still make without first undoing a
    guess made here.
    """
    return [[line] for line in (content or "").split("\n")]


_P_SHEETS_GET = [qp("includeGridData", "boolean"), qp("ranges")]


@router.get("/sheets/v4/spreadsheets/{spreadsheet_id}", openapi_extra={"parameters": _P_SHEETS_GET})
async def sheets_get(spreadsheet_id: str, request: Request):
    """The spreadsheet's structure, and its cells only if asked for.

    ``data`` is withheld unless ``includeGridData=true`` — measured: a real workbook answers 4 KB by
    default and 5.7 MB with the flag, and ``ranges`` alone does NOT unlock it. The mock used to
    volunteer the whole grid on every call, so a reader received cells here that the real API would
    never hand it, and the document it assembled had a different layout against the two backends.
    With the flag, ``ranges`` scopes the returned rows (measured: 5.7 MB -> 11 KB for ``A1:B2``)."""
    row = _editor_doc(request, spreadsheet_id, expect="spreadsheet")
    sheet = {
        "properties": {
            "sheetId": 0,
            "title": SHEETS_SHEET_TITLE,
            "index": 0,
            "sheetType": "GRID",
            # the grid, not the data extent — see SHEETS_GRID_ROWS
            "gridProperties": {"rowCount": SHEETS_GRID_ROWS, "columnCount": SHEETS_GRID_COLS},
        }
    }
    if (request.query_params.get("includeGridData") or "").lower() == "true":
        rows = _sheets_grid(row["content"])
        specs = request.query_params.getlist("ranges") or [SHEETS_SHEET_TITLE]
        sheet["data"] = [_sheets_grid_data(rows, s) for s in specs]
    return {
        "spreadsheetId": spreadsheet_id,
        "properties": {"title": row["title"], "locale": "en_US"},
        "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
        "sheets": [sheet],
    }


# --- Sheets `values` reads ------------------------------------------------------------------
# `spreadsheets.get` serves the whole structured grid; a client that wants a slice reads
# `values.get`, and one that wants several slices reads `values:batchGet`. Both resolve an A1
# range against the same grid `sheets_get` builds, so the three calls cannot disagree about what
# a cell holds.

SHEETS_SHEET_TITLE = "Sheet1"  # the mock shapes every spreadsheet as one sheet with this title

# A real sheet's GRID is larger than its data — Sheets creates one at 1000x26 — and every range
# behaviour below is defined against the grid rather than against the occupied cells. Measured on a
# real spreadsheet holding 14 rows: `values/<title>` echoes `A1:Z1000`, `A:A` echoes `A1:A1000`.
# So the mock declares the same grid. This is API scaffolding, like the synthesized `sheetId` and
# sheet title beside it — not invented cell data, which `_sheets_grid` still refuses to manufacture.
SHEETS_GRID_ROWS = 1000
SHEETS_GRID_COLS = 26

_A1_MAJOR = ("ROWS", "COLUMNS")
_A1_RENDER = ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")
_SHEETS_ENUM = "type.googleapis.com/google.apps.sheets.v4"
# One endpoint of an A1 range: a full cell (`B2`), a bare column (`B`) or a bare row (`2`).
_A1_END = re.compile(r"(?:(?P<col>[A-Za-z]{1,3})(?P<row>\d+)?|(?P<rowonly>\d+))\Z")


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


def _a1_endpoint(part: str, spec: str) -> tuple[int | None, int | None]:
    """``(row, col)`` 0-based for one side of a range; ``None`` means that axis is unbounded.

    ``spec`` is the whole requested range, because that — not the offending half — is what real
    Sheets names back: `A1:` reports "Unable to parse range: A1:", never a bare "".."""
    m = _A1_END.fullmatch(part.strip())
    if not m:
        raise gerr.invalid_argument(f"Unable to parse range: {spec}")
    if m.group("rowonly"):
        return int(m.group("rowonly")) - 1, None
    row = m.group("row")
    return (int(row) - 1 if row else None), _a1_col(m.group("col"))


def _a1_range(spec: str, rows: list[list[str]]) -> tuple[int, int, int, int]:
    """Resolve an A1 range to half-open ``(r0, c0, r1, c1)`` against this grid.

    Handles every form a client may send: ``Sheet1!A1:B2``, ``A1:B2`` (sheet omitted), ``Sheet1``
    (the whole sheet), ``B2`` (one cell), ``A:B`` / ``1:3`` (whole columns / rows), ``A2:B`` (one
    edge unbounded) and ``'Sheet1'!A1`` (quoted title). Everything resolves against the GRID, so a
    range may be wider than the data — the caller trims.

    Two boundary rules, measured against a real spreadsheet: the range's END may overflow and is
    CLAMPED (``A1:AA5`` on a 26-column sheet returns ``A1:Z5``), its START may not. Anything
    unparseable, or naming a sheet this spreadsheet lacks, 400s with Google's ``Unable to parse
    range`` — resolving to an empty grid instead would be indistinguishable from a genuinely empty
    range.
    """
    nrows, ncols = SHEETS_GRID_ROWS, SHEETS_GRID_COLS
    whole = (0, 0, nrows, ncols)
    if "!" not in spec:
        # A BARE name — no cell part at all — means every cell in that sheet. Measured: real Sheets
        # takes it quoted or unquoted and answers the full grid, and this is the only form that says
        # "the whole sheet" without naming bounds, so a client reading an unknown sheet sends it.
        bare = spec.strip()
        if bare[:1] == "'" and bare[-1:] == "'":
            # Quoting makes it unambiguously a sheet NAME, so there is no cell-reference fallback:
            # measured, `'A1'` 400s rather than resolving to cell A1, while bare `A1` IS cell A1.
            # That is exactly why a client cannot drop the quotes — unquoted, a tab named like a
            # cell reference would silently read the wrong tab's cells.
            if bare[1:-1] != SHEETS_SHEET_TITLE:
                raise gerr.invalid_argument(f"Unable to parse range: {spec}")
            return whole
        if bare == SHEETS_SHEET_TITLE:
            return whole
        body = bare
    else:
        title, _, body = spec.partition("!")
        title = title.strip()
        if title[:1] == "'" and title[-1:] == "'":
            title = title[1:-1]
        if title != SHEETS_SHEET_TITLE:
            raise gerr.invalid_argument(f"Unable to parse range: {spec}")
        body = body.strip()
        if not body:  # `Sheet1!` with nothing after it is malformed
            raise gerr.invalid_argument(f"Unable to parse range: {spec}")
    start, sep, end = body.partition(":")
    r0, c0 = _a1_endpoint(start, spec)
    if not sep:  # a single reference: one cell, one whole row, one column
        r0f, c0f = (0 if r0 is None else r0), (0 if c0 is None else c0)
        r1 = nrows if r0 is None else r0 + 1
        c1 = ncols if c0 is None else c0 + 1
    else:
        r1x, c1x = _a1_endpoint(end, spec)
        r0f = 0 if r0 is None else r0
        c0f = 0 if c0 is None else c0
        r1 = nrows if r1x is None else r1x + 1
        c1 = ncols if c1x is None else c1x + 1
        # A1 ranges are inclusive and may be written in either order (`B2:A1` == `A1:B2`).
        if r1 < r0f + 1:
            r0f, r1 = r1 - 1, r0f + 1
        if c1 < c0f + 1:
            c0f, c1 = c1 - 1, c0f + 1
    if r0f >= nrows or c0f >= ncols or r0f < 0 or c0f < 0:
        # The START is outside the grid — refused, with the range echoed back unclamped.
        raise gerr.invalid_argument(
            (
                f"Range ({_a1_name(r0f, c0f, r1, c1)}) exceeds grid limits. "
                f"Max rows: {nrows}, max columns: {ncols}"
            )
        )
    return r0f, c0f, min(r1, nrows), min(c1, ncols)


def _a1_name(r0: int, c0: int, r1: int, c1: int) -> str:
    """The resolved range in A1 form, which is what the response echoes.

    A single cell echoes as a bare reference (``Sheet1!A1``), not as ``A1:A1`` — measured: real
    Sheets collapses a 1x1 range even when the request spelled it out as ``A1:A1``."""

    def col(i: int) -> str:
        s = ""
        i += 1
        while i:
            i, rem = divmod(i - 1, 26)
            s = chr(65 + rem) + s
        return s

    start = f"{col(c0)}{r0 + 1}"
    if r1 - r0 == 1 and c1 - c0 == 1:
        return f"{SHEETS_SHEET_TITLE}!{start}"
    return f"{SHEETS_SHEET_TITLE}!{start}:{col(c1 - 1)}{r1}"


def _rstrip_empty(cells: list[str]) -> list[str]:
    while cells and cells[-1] == "":
        cells.pop()
    return cells


def _sheets_block(rows: list[list[str]], spec: str):
    """``(r0, c0, r1, c1, cells)`` for an A1 range: the range as resolved against the grid, plus the
    cells it covers with trailing empties trimmed off each row and off the block. The bounds are the
    RANGE's, not the data's — callers echo them, so they must not shrink to the occupied cells."""
    r0, c0, r1, c1 = _a1_range(spec, rows)
    block = [
        [(rows[r][c] if c < len(rows[r]) else "") for c in range(c0, c1)]
        for r in range(r0, min(r1, len(rows)))
    ]
    block = [_rstrip_empty(row) for row in block]
    while block and not block[-1]:
        block.pop()
    return r0, c0, r1, c1, block


def _sheets_grid_data(rows: list[list[str]], spec: str) -> dict:
    """One ``GridData`` block for ``spreadsheets.get?includeGridData=true``.

    Rows are padded to the range's width (real Sheets returns a cell object per column, empty ones
    carrying no value) and ``startRow``/``startColumn`` are omitted when zero, which is how the
    measured responses come back — proto3 drops defaults.

    Two divergences, stated rather than hidden: real Sheets pads ``rowData`` to the WHOLE 1000-row
    grid and this stops at the last row holding data; and real cells carry format objects plus
    ``rowMetadata``/``columnMetadata``, none of which this mock models."""
    r0, c0, _r1, c1, block = _sheets_block(rows, spec)
    width = c1 - c0
    out: dict = {}
    if r0:
        out["startRow"] = r0
    if c0:
        out["startColumn"] = c0
    out["rowData"] = [
        {
            "values": [
                (
                    {"formattedValue": row[i], "effectiveValue": {"stringValue": row[i]}}
                    if i < len(row) and row[i] != ""
                    else {}
                )
                for i in range(width)
            ]
        }
        for row in block
    ]
    return out


def _sheets_value_range(rows: list[list[str]], spec: str, major: str) -> dict:
    """One ``ValueRange``. Trailing empty cells and trailing empty rows are dropped rather than
    padded out to the requested bounds (real Sheets does the same), and a range holding nothing
    omits ``values`` entirely — a client tests for the key's presence, so an empty list would
    claim the range exists and is blank."""
    r0, c0, r1, c1, block = _sheets_block(rows, spec)
    out = {"range": _a1_name(r0, c0, r1, c1), "majorDimension": major}
    if major == "COLUMNS":
        width = max((len(r) for r in block), default=0)
        block = [_rstrip_empty([(r[i] if i < len(r) else "") for r in block]) for i in range(width)]
        while block and not block[-1]:
            block.pop()
    if block:
        out["values"] = block
    return out


def _sheets_rows(request: Request, spreadsheet_id: str) -> list[list[str]]:
    """The grid behind a values read — the same ``_sheets_grid`` ``spreadsheets.get`` serves, so a
    cell reads the same whichever of the three calls asked for it."""
    row = _editor_doc(request, spreadsheet_id, expect="spreadsheet")
    return _sheets_grid(row["content"])


def _sheets_options(request: Request) -> str:
    """Validate the read enums and return the major dimension. Real Sheets 400s on an unknown
    value; accepting one silently would hand back ROWS-shaped data to a client that asked for
    columns, and a silently unapplied option is worse than a refusal."""
    major = request.query_params.get("majorDimension") or "ROWS"
    render = request.query_params.get("valueRenderOption") or "FORMATTED_VALUE"
    if major not in _A1_MAJOR:
        raise gerr.invalid_argument(_a1_enum_error("major_dimension", "Dimension", major))
    # On a real spreadsheet these three genuinely differ — measured on one holding formulas and
    # currency: FORMATTED_VALUE gives "₩4,000,000", UNFORMATTED_VALUE gives the JSON number
    # 4000000, FORMULA gives "=B2/12". This corpus has none of that: a cell is one line of stored
    # text, so all three return the same string. The value is still validated, so a client's typo
    # fails here exactly as it would against real Sheets.
    if render not in _A1_RENDER:
        raise gerr.invalid_argument(
            _a1_enum_error("value_render_option", "ValueRenderOption", render)
        )
    return major


_P_SHEETS_VALUES = [qp("majorDimension"), qp("valueRenderOption"), qp("dateTimeRenderOption")]
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
    rows = _sheets_rows(request, spreadsheet_id)
    major = _sheets_options(request)
    ranges = request.query_params.getlist("ranges")
    body = {"spreadsheetId": spreadsheet_id}
    if ranges:
        body["valueRanges"] = [_sheets_value_range(rows, r, major) for r in ranges]
    return body


@router.get(
    "/sheets/v4/spreadsheets/{spreadsheet_id}/values/{a1_range:path}",
    openapi_extra={"parameters": _P_SHEETS_VALUES},
)
async def sheets_values_get(spreadsheet_id: str, a1_range: str, request: Request):
    """One range of a spreadsheet, ACL-enforced through the same lookup as ``spreadsheets.get``."""
    rows = _sheets_rows(request, spreadsheet_id)
    major = _sheets_options(request)
    return _sheets_value_range(rows, a1_range, major)


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
    created = row["created_ts"] or synth.epoch(row["doc_id"])
    modified = row["updated_ts"] or created + 3600
    author = row["author_email"]
    native = _native(row)
    mime = _drive_mime(row)
    if native is not None:
        seg = native[1]
        view = (
            f"https://docs.google.com/{seg}/d/{row['doc_id']}/edit"
            if seg
            else f"https://drive.google.com/drive/folders/{row['doc_id']}"
        )
    else:  # binary file (PDF, image, office doc)
        view = f"https://drive.google.com/file/d/{row['doc_id']}/view"
    is_folder = row["subtype"] == "folder"
    # "shared" = visible to anyone besides the owner — true for org/group/multi-reader docs.
    # In a list the caller passes it in (batch-computed); for a single get, look it up here.
    if shared is None:
        shared = bool(store.doc_grants(conn, row["doc_id"]))
    ext = row["title"].rsplit(".", 1)[-1] if (native is None and "." in row["title"]) else None
    nbytes = len((row["content"] or "").encode("utf-8"))
    f = {
        "kind": "drive#file",
        "id": row["doc_id"],
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
        f["webContentLink"] = f"https://drive.google.com/uc?id={row['doc_id']}&export=download"
        if ext:
            f["fileExtension"] = ext
            f["fullFileExtension"] = ext
    return f


def _drive_permissions(conn, doc_id: str, *, folder: str | None = None) -> list[dict]:
    """Build from the doc's ACL grants (preserving user/group/org identity) + an owner. For a
    synthesized folder, ``folder`` names the container and the grants come from its files (which is
    what makes the folder visible in the first place); the mock models no folder owner, so there is
    no owner permission to add."""
    grants = (
        store.container_grants(conn, "google_drive", folder)
        if folder
        else store.doc_grants(conn, doc_id)
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
    row = store.get_document(conn, "google_drive", doc_id)
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
