"""Google APIs: Gmail (``/gmail/v1``), Drive (``/drive/v3``), and the Workspace editor APIs —
Docs (``/docs/v1``), Sheets (``/sheets/v4``, in `routers.google_sheets`), Slides (``/slides/v1``) —
for clients that read native docs structurally instead of via Drive export.

Client base-URL override: point the Gmail client at ``http://<host>/gmail`` and the
Drive client at ``http://<host>/drive`` (google-api-python-client ``api_endpoint``).
All authenticate with ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import base64
import datetime
import functools
import hashlib
import json
import quopri
import re
import string
import xml.parsers.expat
from contextvars import ContextVar
from email.parser import BytesParser
from email.utils import formataddr, getaddresses
from http import HTTPStatus
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import QueryParams
from starlette.routing import Match

from backlot import (
    auth,
    collation,
    gdocs,
    gsheets,
    protojson,
    store,
    synth,
)
from backlot.acl import Caller
from backlot.config import get_settings
from backlot.errors import google as gerr
from backlot.openapi import qp
from backlot.pagination import decode_cursor_or_none, next_page_token

# `$.xgafv` is checked before any route runs, and `callback` too but on a Drive download inside a
# batch — see `_system_parameters`. A router dependency runs only once a route has MATCHED, so a
# family path with no route 404s here rather than refusing either value — which is why the check
# records whether it looked at `callback` and `gerr.rendered` wraps nothing without it: an unrouted
# path must not be answered by calling a name nothing refused. Nothing to match there — measured
# 2026-09-16, real answers an unrouted family path from its front end, as HTML, 400 on Sheets, Docs
# and Slides and 404 on Drive and Gmail, with or without either parameter, so no JSON envelope of
# its own exists to compare against.


def _system_parameters(request: Request) -> None:
    """`gerr.validate_system_parameters`, with `callback` left alone on a Drive download: real
    answers an uncallable name there with its own 503, and a batch part with a 302, rather than
    refusing the name (see `gerr.refuse_download` and `_drive_batch_redirect`). Whether the request
    is a download is left on ``request.state`` for `gerr.head_content_length`, which is asked once
    the route has answered."""
    download = _drive_download_request(request)
    request.state.google_download = download
    gerr.validate_system_parameters(request, callback=not download)


router = APIRouter(tags=["google"], dependencies=[Depends(_system_parameters)])


# --- OpenAPI enrichment --------------------------------------------------
# Query params are read query-only (via request.query_params, a typed one through _typed_query).
# Documenting them with openapi_extra keeps the handler bodies untouched and merges cleanly with
# the auto-generated path params. Response models use extra="allow" so builders' full field set
# passes through.


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
    size: int
    data: str


_P_GMAIL_LIST = [qp("maxResults", "integer"), qp("pageToken"), qp("q")]
_P_GMAIL_FORMAT = [qp("format"), qp("metadataHeaders")]


class DriveFileList(_GLoose):
    kind: str = "drive#fileList"
    files: list[dict] = []


class DrivePermissionList(_GLoose):
    kind: str = "drive#permissionList"
    permissions: list[dict] = []


# drive_files_get / .export return a raw Response on some branches — they get openapi_extra params
# only (no JSON response_model, which would mis-serialize the raw body).
_P_DRIVE_LIST = [qp("pageSize", "integer"), qp("pageToken"), qp("q"), qp("fields"), qp("orderBy")]
_P_DRIVE_PERMISSIONS = [qp("pageSize", "integer"), qp("pageToken"), qp("fields")]
_P_DRIVE_DRIVES = [qp("q"), qp("fields")]
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


class _BatchOuter(NamedTuple):
    """The batch a sub-request came in."""

    base: str  # the batch request's base URL
    query: str  # the batch request's query string
    lone: bool  # whether exactly one of its parts is not a Drive download (`_drive_download`)


# The batch a sub-request came in, or ``None`` for a request sent on its own.
# ``httpx.ASGITransport`` runs the app in the task that `batch` dispatches from, so the routes a
# part reaches see what `batch` set.
_BATCH_OUTER: ContextVar[_BatchOuter | None] = ContextVar("google_batch_outer", default=None)
# The service account an admin/service caller stands for, which `_require` sets: what it creates
# is its own (`routers.google_sheets._sheets_creator`, `_drive_owned_by`).
_ADMIN_OWNER: ContextVar[str | None] = ContextVar("google_admin_owner", default=None)


def _batch_part_downloads(method: str, target: str) -> bool:
    """Whether a part's request line is a Drive download (`_drive_download`), asked of the route its
    path matches before any part is sent. Asked of this module's `router`, which owns the Drive
    routes: the app holds it wrapped, and the wrapper's match carries no endpoint."""
    import httpx

    url = httpx.URL(target)
    scope = {"type": "http", "method": method, "path": url.path, "root_path": ""}
    for route in router.routes:
        match, child = route.matches(scope)
        if match is Match.FULL:
            return _drive_download(child.get("endpoint"), QueryParams(url.query))
    return False


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


def _drive_batch_cut_path(target: str) -> str:
    """A Drive batch part's target with its path cut at the first ``;`` in it, its query kept.

    Measured 2026-10-05 against ``www.googleapis.com/batch/drive/v3``, and on 2026-10-22 with each
    part alone in its batch and all ten of ``_BATCH_CUT_ROWS`` in one: real ends a part's path
    there, so ``files/{id};x`` is that file, ``files/{id};x/export`` is ``files.get`` on that file
    with its ``/export`` dropped, and ``files;x``, ``about;x``, ``drives;x`` and
    ``files/{id}/permissions;x`` are those routes' answers. The cut is read off the target as
    sent, so an escaped ``%3B`` stays in the path and names the file whose id carries a ``;``.
    A request sent on its own is not cut: each of those paths was a 404 there on 2026-10-22, that
    same ``files/{id};x`` naming a file that does not exist."""
    path, sep, query = target.partition("?")
    cut = path.find(";")
    if cut != -1:
        path = path[:cut]
    return f"{path}?{query}" if sep else path


def _batch_sub_response(r, base: str) -> str:
    """One part's answer as the `application/http` payload a batch carries, `base` being the batch
    request's base URL. The download redirect (:func:`gerr.download_redirect`), the 302 whose
    `Location` is under `base`'s `/download`, carries real's three headers, measured 2026-10-04 in
    this order: `Content-Length: 0`, though its error body follows, `Content-Type` and `Location`.
    A part whose answer carries a `WWW-Authenticate`, a 401, passes it on ahead of `Content-Type`,
    where real's 401 parts put it on 2026-10-13, on Drive's batch and on Sheets', for a missing
    credential and for a bad one. Real's 200, 404 and 401 parts end their headers with a
    `Content-Length` too, and its 200s and 404s carry `Cache-Control`, `Date` and `Expires` ahead of
    `Content-Type`, in an order that changes from part to part, measured on Drive's batch on
    2026-10-13; this server sends none of those four on them. Any other header stays behind, so the
    `Location` of a redirect a route builds from the host the parts are dispatched to, which nothing
    answers, is not passed on."""
    location = r.headers.get("location", "")
    redirect = r.status_code == 302 and location.startswith(f"{base}download/")
    lines = [f"HTTP/1.1 {r.status_code} {_batch_reason(r.status_code)}"]
    if redirect:
        lines.append("Content-Length: 0")
    if "www-authenticate" in r.headers:
        lines.append(f"WWW-Authenticate: {r.headers['www-authenticate']}")
    lines.append(f"Content-Type: {r.headers.get('content-type', 'application/json')}")
    if redirect:
        lines.append(f"Location: {location}")
    return "\r\n".join(lines) + f"\r\n\r\n{r.text}"


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
    parts = [
        (part.get("Content-ID", ""), *_parse_batch_subrequest(part.get_payload(decode=False)))
        for part in parsed.get_payload()
    ]
    if api == "drive":
        # A Drive batch ends each part's path at a `;` it carries (`_drive_batch_cut_path`), ahead
        # of the download a part is read as, so the cut decides that too.
        parts = [
            (cid, method, _drive_batch_cut_path(target) if target else target, headers, body)
            for cid, method, target, headers, body in parts
        ]
    others = sum(
        1
        for _, method, target, _, _ in parts
        if not (method and target and _batch_part_downloads(method, target))
    )
    outer = _BATCH_OUTER.set(_BatchOuter(str(request.base_url), request.url.query, others == 1))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://backlot.batch"
        ) as client:
            for cid, method, target, sub_headers, sub_body in parts:
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
                    sub_resp = _batch_sub_response(r, str(request.base_url))
                out_parts.append((cid, sub_resp))
    finally:
        _BATCH_OUTER.reset(outer)

    body = ""
    for cid, sub_resp in out_parts:
        body += f"--{_BATCH_BOUNDARY}\r\nContent-Type: application/http\r\n"
        if cid:
            body += f"Content-ID: {cid}\r\n"
        body += "\r\n" + sub_resp + "\r\n"
    body += f"--{_BATCH_BOUNDARY}--\r\n"
    return Response(content=body, media_type=f'multipart/mixed; boundary="{_BATCH_BOUNDARY}"')


# The schemes real reads a token under, spelt exactly so, and what ends the token after one.
_SCHEMES = ("Bearer", "OAuth")
_TOKEN_END = re.compile(r"[ \t,]")


def _credential(request: Request) -> tuple[bool, str | None]:
    """Whether real Google reads a credential off the request, and the token it names, which is
    ``None`` for a credential that names none. ``Authorization`` is read first, and the query only
    where the header leaves it a say (`_query_credential`).

    The scheme is `Bearer` or `OAuth` in that case, and one space after it starts the token, which
    runs to the next space, tab or comma once any of those in front of it are passed. A token that
    starts with `"` runs to the next `"` instead, or to the end without one, and loses the spaces,
    tabs and commas at its two ends but none between; what follows the closing `"` is passed over,
    and `""` names no token. A scheme with something other than a space after it, and one whose
    space leads to nothing but those three, is a credential whose header names no token; a scheme
    with nothing but spaces and tabs after it is no credential at all, the query's included, and a
    scheme in another case and anything else are no credential in the header, which leaves the query
    to decide (below). Measured on 2026-10-11 against Drive (`files.list` and `about`), Sheets,
    Docs, Slides and Gmail, with a token that resolves and with `nope`, a quoted token only on
    2026-10-13, and every spelling ``tests/test_google.py::_CREDENTIAL_ROWS`` lists replayed on
    Gmail's `labels.get` and Drive's `files.list` on 2026-10-13.

    The query is read where there is no header, where the header is no credential but for a bare
    `Bearer` or `OAuth`, which keeps the query out too, and where it is a scheme with no space after
    it (`Bearer<t>`, `Bearer<TAB><t>`, `Bearers <t>`, `OAuth2 <t>`), which reads the query without
    the gate `_query_credential` sets on a value, and is a credential that names no token where
    the query names none.

    A Drive download and a batch part read the header this way too (`_require_download_bearer`,
    `_drive_batch_redirect`)."""
    value = request.headers.get("authorization") or ""
    for scheme in _SCHEMES:
        if value.startswith(scheme):
            rest = value[len(scheme) :]
            if not rest.strip():
                return False, None
            if not rest.startswith(" "):
                return _query_credential(request, gated=False) or (True, None)
            token = rest.lstrip(" \t,")
            if token.startswith('"'):
                quoted = token[1:].split('"', 1)[0]
                return True, quoted.strip(" \t,") if quoted else None
            return True, _TOKEN_END.split(token, maxsplit=1)[0] or None
    return _query_credential(request, gated=True) or (False, None)


# The prefixes a query value needs for real's front end to read it as a token, behind no header or
# one that is no credential (`_credential` says which header lifts the gate).
_QUERY_TOKEN_PREFIXES = ("ya29.", "1/")


def _query_credential(request: Request, *, gated: bool) -> tuple[bool, str | None] | None:
    """The credential the query names, as whether there is one and the token it names, or ``None``
    where the query names none.

    Three names carry a token, ``gerr.QUERY_TOKENS``, spelt exactly so. Taken in that order, the
    first the query carries is the gate and the last is the token, wherever each stands in the
    query. ``gated``, the gate's first value has to start `ya29.` or `1/` once decoded, or the query
    names no credential. Past the gate the token's value is resolved as it stands; an empty one, or
    a token whose name the query repeats, names no token. Inside a batch a part reads each of the
    three names from its own query where it carries that name and from the batch's where it does
    not, so a part's values for a name stand in for the batch's rather than join them.

    Measured on Drive and Gmail on 2026-10-13 and 2026-10-14, with the rows ``tests/test_google.py``
    keeps in `_QUERY_CREDENTIAL_ROWS`, and in `_BATCH_ROWS` for a batch. The two prefixes are what a
    byte sweep found, 282 values with each of the 94 visible ASCII characters (0x21 to 0x7E) after
    ya29 and after 1 and before a slash.
    """
    query = request.query_params
    outer = _BATCH_OUTER.get()
    if outer is not None:
        batch = QueryParams(outer.query)
        query = QueryParams(
            [
                (name, value)
                for name in gerr.QUERY_TOKENS
                for value in (query if name in query else batch).getlist(name)
            ]
        )
    named = [name for name in gerr.QUERY_TOKENS if name in query]
    if not named:
        return None
    if gated and not query.getlist(named[0])[0].startswith(_QUERY_TOKEN_PREFIXES):
        return None
    values = query.getlist(named[-1])
    return True, (values[0] or None) if len(values) == 1 else None


_ESCAPED_SLASH = re.compile(r"%2F", re.I)


def _operation_path(request: Request) -> str:
    """The path real chooses a Google operation by: the path as sent with an escaped `/` kept inside
    its segment, every other escape decoded, and the trailing slashes dropped. Measured 2026-10-13
    on Drive with no credential: `files/abc%2Fpermissions` is `files.get`'s 403, and a `POST` to
    `files/abc%2Fcopy` is the front end's HTML 404 whatever the credential, no operation being a
    `POST` on a file. The routes match the decoded path, so a credential that resolves reaches the
    route that path names."""
    raw = request.scope.get("raw_path")
    if not raw:
        return request.scope["path"]
    return unquote(_ESCAPED_SLASH.sub("%252F", raw.decode("latin-1"))).rstrip("/")


def _require(request: Request, *, download: bool = False) -> Caller:
    """The caller, or the error real Google gives — NOT the shared ``auth.require_bearer``, because
    Google's answer is not one status. A credential that does not resolve is 401 UNAUTHENTICATED
    everywhere (:func:`gerr.bad_token`), while no credential at all is a 403 or a 401 by operation
    (:func:`gerr.no_credentials`). What is a credential is `_credential`'s to say.

    ``download`` asks for a byte-stream read's answer instead: measured 2026-10-04 on
    `files.export` and 2026-10-05 on `files.get?alt=media`, a missing credential there names the
    missing API key (:func:`gerr.missing_api_key`) instead of the anonymous GET's unregistered
    caller, a header that is no credential is :func:`gerr.download_credential_refused`, and real
    puts both AFTER the download's own parameters — which is why the handlers resolve a download
    late."""
    present, token = _credential(request)
    if not present:
        if download and "authorization" in request.headers:
            raise gerr.download_credential_refused()
        if download:
            raise gerr.missing_api_key()
        raise gerr.no_credentials(_operation_path(request), request.method)
    caller = auth.acl(request).resolve(token) if token is not None else None
    if caller is None:
        raise gerr.bad_token(named=token is not None)
    if caller.email is None:
        _ADMIN_OWNER.set(_service_email(request))
    return caller


def _require_download_bearer(request: Request) -> None:
    """The 401 a Drive download answers ahead of its own refusals, for a credential (`_credential`)
    that does not resolve.

    Measured 2026-10-07 and 2026-10-08 on `files.get?alt=media` and `files.export` beside
    `callback=a b`: `Bearer nope` answers the 401, where `bearer nope`, `BEARER nope`,
    `token nope`, a bare `Bearer`, `bearer`, `nope` and `Basic YWJjOmRlZg==` each answer the
    callback's 503; on 2026-10-11 `OAuth nope`, `Bearer nope x`, `Bearer<t>` and `Bearer<TAB>nope`
    answered the 401 too. Every other value is left to :func:`gerr.refuse_download`, and without a
    `callback` is `_require`'s to answer."""
    if _credential(request)[0]:
        _require(request)


# The baselines `backlot diff` keeps for Google's two sources, read without importing
# ``backlot.fidelity``, whose ``__init__`` loads the comparison modules.
_BASELINES = Path(__file__).resolve().parent.parent / "fidelity" / "baseline"
# Where the paths the editor APIs' own documents spell from the version on are mounted here, as
# `backlot.fidelity.comparisons.GOOGLE_DISCOVERY` strips them.
_EDITOR_MOUNTS = (("/v4/", "/sheets"), ("/v1/documents", "/docs"), ("/v1/presentations", "/slides"))


def _published_not_served() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """The two Google baselines' `missing` operations as (method, path pattern), each at the
    path Backlot mounts it on and each `{}` one path segment."""
    operations = []
    for source in ("gmail", "google_drive"):
        entries = json.loads((_BASELINES / f"{source}.json").read_text(encoding="utf-8"))[
            "operations"
        ]
        for e in entries:
            if e["status"] != "missing":
                continue
            method, path = e["operation"].split(" ", 1)
            mount = next((m for spelled, m in _EDITOR_MOUNTS if path.startswith(spelled)), "")
            pattern = "[^/]+".join(re.escape(part) for part in (mount + path).split("{}"))
            operations.append((method, re.compile(pattern)))
    return tuple(operations)


_PUBLISHED_NOT_SERVED = _published_not_served()


def refuse_an_unserved_operation(request: Request) -> None:
    """Raise what real answers ahead of an operation in `_PUBLISHED_NOT_SERVED`, which is what a
    served route answers ahead of its own work: `$.xgafv`, then a `callback` on a GET, then the
    credential, by `_require`'s rule and the operation's own anonymous answer
    (:func:`gerr.no_credentials`). A caller that passes all three gets the router's 404 or 405, as
    Backlot has no operation to run.

    Measured 2026-10-11 on eight of the operations beside an unknown `$.xgafv`, `$.xgafv=1`, a
    callable `callback` and an uncallable one, with a trailing slash and as a `HEAD`: each refused
    as a served route of the same method does. The operation is chosen on
    :func:`_operation_path`."""
    path = _operation_path(request)
    if not any(m == request.method and p.fullmatch(path) for m, p in _PUBLISHED_NOT_SERVED):
        return
    gerr.validate_system_parameters(request)
    _require(request)


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


# ================================ Gmail =========================================


def _is_me(user_id: str) -> bool:
    """Whether `userId` is the caller's own alias, in any case: `ME` and `Me` read the caller's
    mailbox as `me` does, measured 2026-10-11."""
    return user_id.lower() == "me"


def _mailbox_caller(request: Request, user_id: str) -> Caller:
    """The caller, as `_require` gives it, refused by :func:`_refuse_another_mailbox`."""
    caller = _require(request)
    _refuse_another_mailbox(caller, user_id)
    return caller


def _refuse_another_mailbox(caller: Caller, user_id: str) -> None:
    """Refuse a user's token a `userId` that names a mailbox not its own.

    A user's token reads its own mailbox only, named `me` (:func:`_is_me`) or by its own address in
    any case. A `userId` with no `@`, or nothing on one side of it, is :func:`gerr.invalid_user_id`
    and any other is :func:`gerr.delegation_denied`. Real draws that line more narrowly: on
    2026-10-12 `a@b`, `a@b.c`, `a@b@c`, `a@x.com.`, `a@b_c.com`, a local part holding a space, a
    tab, `"`, `,` or `;`, and a literal `+`, which real reads as a space, were each the invalid id,
    where `a%2Bb@x.com`, `a@1.2` and `.a@x.com` were addresses; here those are the delegation
    refusal.

    Measured 2026-10-11 and 2026-10-12 on every Gmail route served here: the refusal comes after
    `$.xgafv`, the credential and a `maxResults` the proto layer cannot read as a uint32
    (:func:`_gmail_max_results`), and ahead of an unparseable message id or attachment token, a
    missing message or label, a `maxResults` of 0 and a page token that does not parse. The admin
    token stands for a domain-wide-delegated service account and reads any mailbox; real's answer
    to one was not measured, since only a domain's administrator can grant the delegation."""
    if caller.email is None or _is_me(user_id) or user_id.lower() == caller.email.lower():
        return
    local, at, domain = user_id.partition("@")
    if not (local and at and domain):
        raise gerr.invalid_user_id()
    raise gerr.delegation_denied(caller.email)


def _mailbox_email(caller: Caller, user_id: str) -> str | None:
    """Resolve the mailbox owner email; None means 'all mailboxes' (admin, 'me')."""
    if _is_me(user_id) or caller.email is not None:
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
    email = _mailbox_email(caller, user_id)
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
    caller = _mailbox_caller(request, user_id)
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

# The system labels Gmail always exposes (users.labels.list), in the order real `labels.list`
# returned them on one mailbox, twice, on 2026-10-01. Gmail documents no order, so this is
# Backlot's choice, taken from that measurement.
_SYSTEM_LABELS = [
    "CHAT",
    "SENT",
    "INBOX",
    "IMPORTANT",
    "TRASH",
    "DRAFT",
    "SPAM",
    "CATEGORY_FORUMS",
    "CATEGORY_UPDATES",
    "CATEGORY_PERSONAL",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "YELLOW_STAR",
    "STARRED",
    "UNREAD",
]

# Measured against gmail.googleapis.com on a Google Workspace account on 2026-09-30, and again on
# 2026-10-02: these four labels and the five `CATEGORY_` ones carry `messageListVisibility: "hide"`
# and `labelListVisibility: "labelHide"`, and the rest carry neither member. A personal account
# measured on 2026-10-01 also served INBOX with `messageListVisibility: "hide"` and
# `labelListVisibility: "labelShow"`; what makes the two differ was not measured.
_HIDDEN_LABELS = {"IMPORTANT", "CHAT", "SPAM", "TRASH"}


def _label_obj(lid: str, counts: tuple[int, int] | None = None) -> dict:
    """One system label. `labels.list` serves no counts and `labels.get` serves all four, as
    measured on 2026-09-30 — so `counts` is ``(messages, threads)`` on a get and None on a list."""
    obj = {"id": lid, "name": lid, "type": "system"}
    if lid in _HIDDEN_LABELS or lid.startswith("CATEGORY_"):
        obj["messageListVisibility"] = "hide"
        obj["labelListVisibility"] = "labelHide"
    if counts is not None:
        messages, threads = counts
        obj |= {
            "messagesTotal": messages,
            "messagesUnread": 0,
            "threadsTotal": threads,
            "threadsUnread": 0,
        }
    return obj


@router.get("/gmail/v1/users/{user_id}/labels")
async def gmail_labels(user_id: str, request: Request):
    _mailbox_caller(request, user_id)
    return {"labels": [_label_obj(lid) for lid in _SYSTEM_LABELS]}


@router.get("/gmail/v1/users/{user_id}/labels/{label_id}")
async def gmail_label_get(user_id: str, label_id: str, request: Request):
    conn = auth.conn(request)
    caller = _mailbox_caller(request, user_id)
    if label_id not in _SYSTEM_LABELS:
        raise gerr.not_found_entity()
    ids = auth.visible_ids(request, caller)
    messages, threads = _mailbox_totals(conn, caller, user_id, ids)
    return _label_obj(label_id, (messages, threads) if label_id == _GMAIL_DEFAULT_LABEL else (0, 0))


# The operators that read the word after whitespace as their value. Measured on a Workspace
# mailbox: on 2026-10-16, `from: <word>`, `bcc: <word>`, `to:<tab><word>`, `From: <word>` and
# `TO: <word>` answered the ids the operator answers with no whitespace, and `to: from: bcc:`
# answered nothing; on 2026-10-17, so did `subject:`, `label:` and `in:` after a space or a tab
# (`subject:` after two spaces or a no-break space too), and `after:`, `before:`, `newer:` and
# `older:` after one space or three. A tab after those four leaves the operator empty:
# `after:<tab>1` answered the messages holding both search terms `after` and `1`. `has:`,
# `newer_than:`, `older_than:` and `larger:` read nothing after a space: `has: attachment` answered
# nothing where `has:attachment` answered 160 messages. On 2026-10-19 a pair of quotes holding
# nothing or only spaces read as whitespace after each operator `_GMAIL_SPACED` names:
# `label:"" probe`, `subject:" " probe`, `from:"" <word>` and `cc:""<word>` answered what
# `label: probe`, `subject: probe`, `from: <word>` and `cc: <word>` answer, and `<word> to:""` what
# `<word>` alone answers.
_GMAIL_SPACED = (
    r"(?<=\bfrom:)|(?<=\bto:)|(?<=\bcc:)|(?<=\bbcc:)|(?<=\bsubject:)|(?<=\blabel:)|(?<=\bin:)"
)
# In the whitespace `_GMAIL_SPACED` reads after an operator, a no-break space counts only after
# `subject:`: measured on 2026-10-22, `subject:<no-break space>probe` answered the ids of
# `subject:probe`, where `from:<no-break space>me` answered nothing — after the other operators
# a value holding a no-break space matches nothing. On 2026-10-25 `from:me<no-break space>`,
# `label:inbox<no-break space>`, `has:<no-break space>attachment` and
# `has:attachment<no-break space>` answered nothing too, and with a `-` or a `NOT` in front
# (`-label:inbox<no-break space>`, `-has:<no-break space>attachment`,
# `NOT label:inbox<no-break space>`) every message; `subject:probe<no-break space>` answered the ids
# of `subject:probe`.
_GMAIL_SPACED_NBSP = r"(?<=\bsubject:)"
_GMAIL_DATED = r"(?<=\bafter:)|(?<=\bbefore:)|(?<=\bnewer:)|(?<=\bolder:)"
# A `"` after `subject:`, `label:` or `in:` that no later `"` closes. What follows it is the
# operator's value, read as if a `"` closed it at the end of `q`, with the whitespace around it and
# any `-` in front of it left out; after whitespace, one `from:`, `to:`, `cc:`, `bcc:` or `subject:`
# operator is read as itself instead, its value whole and with no `-` in front of it or of its
# value, and the first operator and the quote are left out. Measured on a Workspace mailbox,
# comparing `messages.list` id sets: on 2026-10-19, `label:"`, `in:"` and `subject:"` answered every
# message, `probe label:"` what `probe` answers, and `subject:" probe` and `label:" probe` what
# `subject:probe` and `label:probe` answer. On 2026-10-21, `label: " inbox`, `label:"inbox`,
# `label:" -inbox`, `in:" sent` and `IN:" sent` answered what `label:inbox` and `in:sent` answer,
# `-label:" inbox` and `-in:" sent` their negations, and `subject:" backlot probe` what
# `subject:"backlot probe"` answers; `subject:" probe backlot`, `subject:"probe backlot`,
# `label:"inbox probe`, `label:" inbox probe`, `subject:" label:inbox`, `in:" in:sent` and
# `subject:" has:attachment` answered nothing, and `label: "`, `-label:"` and `-subject:"` every
# message. `subject:" from:me`, `subject:" FROM:me`, `label:" from:me`, `in:" from:me`,
# `subject: " from:me`, `subject:" from: me`, `subject:" -from:me` and `subject:" from:-me` answered
# what `from:me` answers, `-subject:" from:me` its negation, and `subject:" to:me` and
# `in:" subject:probe` what `to:me` and `subject:probe` answer; `subject:"from:me`,
# `subject:" from:me to:me` and `subject:" from:me+probe` answered nothing. `threads.list` answered
# `label:"`, `subject:" probe`, `subject:" from:me` and `subject:" label:inbox` alike.
_GMAIL_LONE_QUOTE = re.compile(
    r'(?<!\S)(-*)((?:subject|label|in|from|to|cc|bcc):)\s*(?:"[ \t]*"\s*)*"(?![^"]*")(\s*)(.*)',
    re.IGNORECASE,
)
_GMAIL_LONE_QUOTE_OPERATOR = re.compile(r"-*((?:from|to|cc|bcc|subject):)\s*-*(\S*)", re.IGNORECASE)
# `from:`, `to:`, `cc:` and `bcc:` read a lone quote as `subject:`, `label:` and `in:` do, and a
# pair of quotes holding nothing in front of the lone quote is left out. Measured on a Workspace
# mailbox over 639 to 645 messages on 2026-10-21 and 2026-10-22, comparing `messages.list` id
# sets: `from:" me` and `to:" me` answered what `from:me` and `to:me` answer; `from:" probe`,
# `from:"me probe`, `to:" probe` and `from:" label:inbox` nothing; `from:" from:me`,
# `to:" from:me`, `cc:" from:me`, `from:" FROM:me` and `to:" -from:me` what `from:me` answers,
# `from:" to:me` and `from:" -to:me` what `to:me` answers, `from:" subject:probe` what
# `subject:probe` answers, and `-from:" to:me` and `-to:" from:me` the negations of `to:me` and
# `from:me`. `from:"` answered every message, `probe from:"` what `probe` answers and `from:"me`
# what `from:me` answers. `label:"" "` and `label:"""` answered every message, `label:"" " inbox`
# and `label:""" inbox` what `label:inbox` answers, `subject:"" " from:me` and
# `subject:"" " -from:me` what `from:me` answers, `in:"" " to:me` what `to:me` answers, and
# `subject:"" " label:inbox` nothing.
#
# A quote after `subject:`, `label:`, `in:` or an address operator that whitespace follows and a
# later quote closes. Measured on the same mailboxes: `subject:" from:me"` and `subject:" from:"me"`
# answered what `from:me` answers, `label:" inbox "` and `subject:" probe "` what `label:inbox` and
# `subject:probe` answer, and `label:" inbox subject:"probe"`, `label:" inbox -subject:"probe"` and
# `label:" inbox --subject:"probe"` what `label:inbox probe` answers; on 2026-10-22,
# `subject:" from:" probe` answered nothing and `subject:" probe<no-break space>"` what
# `subject:probe` answers.
_GMAIL_CLOSED_QUOTE = re.compile(
    r'(?<!\S)(-*)((?:subject|label|in|from|to|cc|bcc):)\s*"\s+(?=[^\s"])([^"]*)"', re.IGNORECASE
)
_GMAIL_BARE_OPERATOR = re.compile(r"-*(?:from|to|cc|bcc|subject):", re.IGNORECASE)
_GMAIL_TRAILING_OPERATOR = re.compile(
    r"\s+-*(?:from|to|cc|bcc|subject|label|in|has|after|before|newer|older|newer_than|older_than):$",
    re.IGNORECASE,
)
# A word joined by a hyphen to an operator is one search term no message holds, and so is a word
# joined by a hyphen to a bracket holding an address operator or `subject:`, negated or not; a word
# of digits makes such a term only with those five. A `-` touching the word, or a `NOT` in front of
# it, flips the term as `_GMAIL_NOT` flips an operator, and a flipped term is left out. Measured on
# a Workspace mailbox on 2026-10-21 and 2026-10-22, comparing `messages.list` id sets:
# `probe-label:inbox`, `probe-subject:" from:me`, `probe-has:attachment`, `probe-after:2020/01/01`,
# `a1-label:inbox`, `probe-(from:me)`, `probe-( from:me)`, `probe-(to:me)`, `probe-(-from:me)`,
# `2026-from:me` and `2026-(from:me)` answered nothing; `-probe-label:inbox`, `--probe-label:inbox`,
# `NOT probe-label:inbox`, `NOT - probe-label:inbox`, `-probe-(from:me)`, `-probe-subject:" from:me`
# and `-2026-from:me` every message; `NOT NOT probe-label:inbox` and `- probe-label:inbox` nothing;
# and `-probe-label:inbox from:me` and `NOT probe-label:inbox from:me` what `from:me` answers.
# `2026-label:inbox`, `2026-in:inbox` and `2026--label:inbox` answered what `2026 label:inbox`
# answers, and `probe-(label:inbox)`, `probe-(in:inbox)` and `probe-(label:inbox` what
# `label:inbox probe` answers, where `probe-(label:inbox from:me)`, `probe-(label:inbox to:me)`,
# `probe-(label:inbox -subject:probe)` and `probe-(label:inbox NOT from:me)` answered nothing.
_GMAIL_HYPHENED = re.compile(
    r"(?<!\S)((?:NOT[ \t]+(?:-+[ \t]+)*)*)(-*)"
    r"(?:\w+-+\((?=[^)]*(?<![^\s(])-*(?i:from|to|cc|bcc|subject):)[^)]*\)?"
    r"|(?:\w+-+(?i:from|to|cc|bcc|subject)"
    r"|\w*[^\W\d]\w*-+(?i:label|in|has|after|before|newer|older|newer_than|older_than))"
    r':(?:"[^"]*"?|\S*))'
)
# A lone quote at the start of a search with one operator after it, and nothing more, stands for
# that operator, with any `-` in front of the operator left out. Measured on a Workspace mailbox on
# 2026-10-21 and 2026-10-22, comparing `messages.list` id sets: `" label:inbox`, `" -label:inbox`
# and `" --label:inbox` answered what `label:inbox` answers, `" from:me` and `" -from:me` what
# `from:me` answers, and `" has:attachment` and `" in:sent` what `has:attachment` and `in:sent`
# answer; `" label:inbox -from:me`, `" -label:inbox -from:me`, `" label:`, `" -label:`, `" from:`
# and `"` alone answered nothing.
_GMAIL_BARE_LONE_QUOTE = re.compile(
    r'^[ \t]*"[ \t]+-*(?=(?i:from|to|cc|bcc|subject|label|in|has|after|before|newer|older'
    r"|newer_than|older_than):\S+[ \t]*$)"
)
# A lone quote after `subject:`, `label:`, `in:` or an address operator, then whitespace and a
# `(` that no `)` closes. On 2026-10-25 (the record at `_GMAIL_NOT`): `subject:" (from:me`,
# `subject:" ( from:me`, `subject:" (probe`, `subject:" ((from:me)`, `subject:" (from:me) (to:me`,
# `label:" (from:me`, `from:" (from:me`, `to:" (from:me` and `probe subject:" (from:me` answered
# nothing, and `-subject:" (from:me`, `-subject:" (probe`, `-label:" (from:me` and
# `NOT subject:" (from:me` every message, where `subject:" (from:me)`, with the `)`, answered the
# ids of `from:me`. `_parse_gmail_q` reads it before `_gmail_groups` can read the group.
_GMAIL_LONE_QUOTE_UNCLOSED = re.compile(
    r'(?<!\S)((?:NOT\s+)*)(-*)(?:subject|label|in|from|to|cc|bcc):\s*(?:"[ \t]*"\s*)*'
    r'"(?![^"]*")\s+(.*)',
    re.IGNORECASE,
)
# A quoted run holding one operator and its value, with nothing around them but whitespace,
# brackets, a leading `-` or `#` and a trailing `.`, `,`, `;`, `/` or `-`, reads as that operator.
# With the 2026-10-25 record at `_GMAIL_NOT`: `"from:me"`, `"from: me"`, `"(from:me)"`,
# `"( from:me )"`, `" (from:me)"`, `"(from:me"`, `"(((from:me)))"`, `"[from:me]"`, `"-from:me"`,
# `"(-from:me)"`, `"-(from:me)"`, `"#from:me"`, `"from:me."` and `"(from:me);"` answered what
# `from:me` answers; `"(label:inbox)"`, `"has:attachment"` and `"in:sent"` what the operator
# answers; `-"(from:me)"` and `NOT "from:me"` the negation, `probe "(from:me)"` the ids both `probe`
# and the operator answer, `"(from:)"` every message and `"!from:me"` and `"from:me+"` nothing. Two
# terms in the quotes are no operator: `"(from:me to:me)"` and `"(from:me)(to:me)"` answered
# nothing. The operator's name is read in any case: on 2026-10-26 `"(FROM:me)"`, `"(From:me)"` and
# `"FROM:me"` answered what `from:me` answers, `"IN:sent"` and `"HAS:attachment"` what their
# operators answer, and `-"(FROM:me)"` and `NOT "FROM:me"` the negation.
_GMAIL_QUOTED_OPERATOR = re.compile(
    r'(?<![^\s(])(-*)"[\s(\[{#-]*'
    r"((?:from|to|cc|bcc|subject|label|in|has|after|before|newer|older|newer_than|older_than):"
    r'[ \t]*[^\s"()\[\]{}]*?)[\s)\]}.,;/-]*"',
    re.IGNORECASE,
)
# An empty operator `_GMAIL_OP` drops, so that the operator after it applies. Measured on a
# Workspace mailbox of 625 messages on 2026-10-19, comparing `messages.list` id sets
# (`threads.list` answered the ten queries sent to it alike):
#
#   dropped   an empty `subject:`, `from:`, `to:`, `cc:` or `bcc:` with spaces, a tab, a pair of
#             quotes holding nothing or spaces, or nothing at all after it, and an empty `label:` or
#             `in:` with any of those except nothing, in any letter case, before `label:`, `in:`,
#             `has:`, `after:`, `before:`, `newer:`, `older:`, `newer_than:` or `older_than:` whose
#             value starts right after its colon, or after spaces for the four date operators:
#             `subject:label:inbox`, `bcc:""in:sent`, `label:" "label:inbox`,
#             `SUBJECT: NEWER_THAN:30d` and `in: before: 2026/10/01` answered the ids of the
#             operator after the empty one
#   kept      before `subject:`, `from:`, `to:`, `cc:` or `bcc:`, before one of those nine whose
#             value is empty, quoted or (for the five that are not dates) after a space, before a
#             date operator whose value is `-`, and with a no-break space after it:
#             `subject: from:me`, `label: label:""`, `in: label:"inbox"`, `from: label: inbox`,
#             `subject: after:-` and `subject:<no-break space>label:inbox` answered nothing, and
#             `_GMAIL_OP` reads the next word as the empty operator's value
_GMAIL_EMPTY_BEFORE = (
    r'(?:(?:subject|from|to|cc|bcc):|(?:label|in):(?=[ \t"]))(?:[ \t]|"[ \t]*")*'
    r'(?=(?:label|in|has|newer_than|older_than):[^\s"]|(?:after|before|newer|older): *[^\s"-])'
)
# An operator does not start in the middle of a term: a no-break space glues the word before it
# to what follows, so `(?<!\xa0)` keeps `probe<no-break space>from:me` one free-text term
# no message holds, where a space in that place would leave `from:me` an operator.
_GMAIL_OP = re.compile(
    rf"(?<!\xa0)(?:{_GMAIL_EMPTY_BEFORE})?(?:(?<!\S)-+)?(\w+):"
    rf'(?:(?:{_GMAIL_SPACED_NBSP})(?:\s|"\s*")*|(?:{_GMAIL_SPACED})(?:[ \t]|"[ \t]*")*'
    rf'|(?:{_GMAIL_DATED}) *)?("[^"]*"|(?:\S|\xa0)*)',
    re.IGNORECASE,
)
# operators we honor; anything else stays as free text
_GMAIL_KEYS = {
    "from",
    "to",
    "cc",
    "bcc",
    "subject",
    "after",
    "before",
    "label",
    "in",
    "has",
    "newer_than",
    "older_than",
    "newer",
    "older",
}
# Real reads `newer:` as `after:` and `older:` as `before:`, so each is folded into the operator
# it is another name for. Measured on a Workspace mailbox on 2026-10-15: messages.list and
# threads.list answered `newer:X` with the ids `after:X` answers, and `older:X` with those of
# `before:X`, for each value X tried, read as a date or not. An empty `newer:` is the search term
# `newer`, as an empty `after:` is `after`, and `_GMAIL_OP` leaves both in the free text.
_GMAIL_KEY_ALIASES = {"newer": "after", "older": "before"}
# The address operators. A Gmail search ignores one with no value rather than searching it as free
# text. Measured on a Workspace mailbox: `to:`, `from:` and `bcc:` with no value answered every
# message, as no `q` does (2026-10-14), and so did `cc:`, `from:""` and `to:""` (2026-10-16).
_GMAIL_ADDRESS_KEYS = {"from", "to", "cc", "bcc"}
# The operators a search ignores when they have no value, as it ignores an address operator with
# none, where a bare `after:`, `before:`, `newer:` or `older:` is the word it spells. Measured on a
# Workspace mailbox, comparing `messages.list` id sets with those of no `q`: on 2026-10-17,
# `subject:`, `label:` and `in:` answered every message, with a space after the colon or none,
# where `subject`, `label` and `in` alone did not; on 2026-10-18 so did `subject:""`, `label:""`
# and `in:""`, a tab or two spaces after the colon, and `SUBJECT:`. `probe label:` answered as
# `probe` does, and `threads.list` answered each of the three with every thread.
_GMAIL_DROPPED_WHEN_EMPTY = {"subject", "label", "in"}
# The operators whose value a `+` ends. Measured on a Workspace mailbox on 2026-10-15:
# `cc:<local>+<word>`, `from:<local>+update` and `to:<local>+update` answered the ids of the
# operator with `+<word>` sent as a search term of its own; `cc:<local>+`, `cc:<local>++` and
# `from:<local>+   to:<local>+` answered nothing; `cc:+<local>` answered as `cc:<local>` does,
# `+` alone with every message, and `cc:<local> +` as `cc:<local>`. On 2026-10-16, `bcc:` and
# `subject:` answered alike (`subject:backlot+probe` as `subject:backlot +probe`, `bcc:<local>+`
# and `subject:backlot+` nothing, `bcc:+<local>` as `bcc:<local>`), and so did a word outside an
# operator (`+update` as `update`, `update+` and `+update+` nothing). A value with a character
# before an `@` keeps what follows a `+`: `from:<address>+x`, `cc:<address>+x` and
# `from:<local>@+x` answered nothing and `from:<local>+<tag>@<domain>` that address's messages,
# while `from:<address>+` and `from:<address>++` answered as `from:<address>` and `from:@<domain>+x`
# as `from:@<domain> +x`. `label:`, `in:` and `has:` keep a `+` (`label:inbox+x` answered nothing).
_GMAIL_PLUS_ENDS = {"from", "to", "cc", "bcc", "subject"}
# The `ops` key of a value that ends in `+`: real answers such a search with nothing. No operator
# can be named this, since `_GMAIL_OP` reads a name of at least one word character.
_GMAIL_NOTHING = ""
# How real reads the value of `after:` and `before:` (and so of `newer:` and `older:`), as
# `_gmail_q_date` reads it. Measured on a Workspace mailbox, comparing the ids each query returned
# with those of a date written `YYYY/MM/DD` (595 messages on 2026-10-15, 603 on 2026-10-17):
#
#   read as seconds   ASCII digits whose value is above 0 and at most `_GMAIL_SECONDS_MAX`,
#                     leading zeros and all: `1`, `01`, `1790812800`, `01790812800`,
#                     `253402300800`; `0`, `000`, `-1`, `+1790812800`, `1e9`, `9223372036854` and
#                     `99999999999999999999` nothing
#   read as a date    three runs of ASCII digits split by one `/` or one `-`, tried as year, month,
#                     day, then month, day, year, then day, month, year, the first that is a date:
#                     `10/1/2026` is 1 October, `01/10/2026` 10 January, `15/10/2026` 15 October;
#                     a year above 0 and below 100 is in the 2000s (`26/10/05`, `9/30/26`), so
#                     `10/05/26` is 26 May 2010; a first run of three digits or more is the year
#                     (`026/10/05`, `02026/10/01`); a month or day may carry leading zeros
#                     (`2026/010/01`, `2026/10/005`)
#   either            after the trailing `/`, `.`, `,` and `:` are dropped (`2026/10/01//`,
#                     `2026/10/01.`, `1790812800,`, `2026/10/01:`)
#   nothing           anything else: `abc`, `2026/13/01`, `2026/02/30`, `2026.10.01`,
#                     `2026-10-01T00:00`, `2026/10-01`, `"2026/10/01"`, `(2026/10/01)`, digits
#                     that are not ASCII (`２０２６/１０/０１`), and a query holding one of them
#                     beside a value real reads
#
# A `-` in front negates the operator when its value reads: `-after:2026/10/01` answered as
# `before:2026/10/01`, and `-before:X` as `after:X`; `-after:abc`, `-after:0` and `-before:0`
# answered every message. `after:-` is the search term `after`, as an empty `after:` is. Not
# modelled here, each answered nothing: a year past 9999 (real read `10000/01/01` as a date after
# every message), `00` (real read it), a count of seconds between `_GMAIL_SECONDS_MAX` and
# `9223372036854`, and values real answered with ids no reading above gives (`2026/10`, `10/01`,
# `2026/10/01x`, `1790812800.5`).
_GMAIL_DATE_KEYS = {"after", "before"}
# The largest count of seconds measured to read, 10000-01-01T00:00:00Z.
_GMAIL_SECONDS_MAX = 253402300800
_GMAIL_DATE = re.compile(r"([0-9]+)([/-])([0-9]+)\2([0-9]+)")
_GMAIL_NEGATED_DATE = re.compile(r"(?<!\S)-(after|before|newer|older): *(\S+)", re.IGNORECASE)
# A `-` in front of an address operator, or in front of its value, negates it. Measured on a
# Workspace mailbox, comparing `messages.list` id sets: on 2026-10-17, `-from:<word>` and
# `from:-<word>` each answered the messages `from:<word>` does not, and so did `to:` and `cc:`, for
# three words on each operator and for 15 hyphenated local parts sent as `from:-<local part>`. On
# 2026-10-18, so did `bcc:`, and on all four operators `-from: <word>`, `from: -<word>`,
# `-from:-<word>`, `--from:<word>`, `from:--<word>`, `-FROM:<word>`, `-from:"<word>"`, a whole
# address, an `@<domain>` and `me`; `from:"-<word>"` answered as `from:<word>`, and `-from:`,
# `from:-`, `-from:-`, `from:--` and `-from:""` every message. `label:` keeps the `-` in its value:
# `label:-` answered nothing. Not modelled: `from:-<word>+update` answered every message, where
# `-from:<word>+update` answered the messages of `+update` that `from:<word>` leaves out. A negated
# value is stored under its key with a `-` in front.
_GMAIL_NEGATED_ADDRESS = re.compile(r"(?<!\S)-+(from|to|cc|bcc):\s*", re.IGNORECASE)
# A `-` in front of `subject:`, `label:` or `in:` negates it, and so does a `-` in front of its
# value after whitespace or a pair of quotes; a `-` touching the colon negates `subject:` and stays
# in the value of `label:` and `in:`. A `-` standing alone is no search term. Measured on a
# Workspace mailbox, comparing `messages.list` id sets: on 2026-10-18, `-subject:probe`,
# `-label:inbox`, `label: -inbox`, `-in:sent` and `in: -sent` answered the messages the operator
# alone does not, `- from:<word>` the ids of `from:<word>`, and `NOT from:<word>` and
# `-(from:<word>)` the ids `from:<word>` does not answer, for three words on each of `from:`, `to:`,
# `cc:` and `bcc:`; on 2026-10-19 `label:"" -probe` answered every message. On 2026-10-20 (637
# messages), `-label: inbox`, `-label:<tab>inbox`, `-label: "inbox"`, `-label:"" inbox`,
# `-label: -inbox`, `-subject: probe`, `subject:-probe`, `-in: sent`, `NOT label:inbox`,
# `NOT subject:probe`, `NOT in:sent`, `-(label:inbox)` and `-( from:me )` answered the messages the
# operator alone does not; `NOT -from:me` and `NOT -label:inbox` the ids of the operator alone;
# `not from:me` those of `from:me not`; `-subject:<word>+probe`, for a word in no subject, those of
# `probe`; `label:-inbox`, `in:-sent` and `-in:anywhere` nothing; and `-label:-inbox`,
# `-subject:""`, `-label:" "`, `subject:-`, `subject: -`, `label: -` and `subject: -label:inbox`
# every message. Not modelled: `subject:-<word>+probe` answered every message. `NOT <word>` and
# `-(<word>)` are :func:`_gmail_take_negated_words`.
_GMAIL_NEGATABLE = {"subject", "label", "in"}
# `NOT` in capitals negates the operator after it as a `-` does, and each `NOT`, and a `-` touching
# the operator, in a run in front of it flips it again; a `-` standing alone in the run flips
# nothing. Measured on a Workspace mailbox of 642 messages on 2026-10-21, comparing `messages.list`
# id sets: `NOT has:attachment`, `NOT  has:attachment` and `NOT<tab>has:attachment` answered the
# messages `has:attachment` does not, `NOT after:2026/10/01` and `NOT newer:2026/10/01` the ids of
# `before:2026/10/01`, `NOT before:2026/10/01` and `NOT older:2026/10/01` those of
# `after:2026/10/01`, `NOT newer_than:30d` those of `older_than:30d` and `NOT older_than:30d` those
# of `newer_than:30d`; `NOT NOT from:me`, `NOT NOT NOT NOT from:me`, `NOT -from:me` and
# `NOT NOT NOT -from:me` the ids of `from:me`, and `NOT NOT NOT from:me`, `NOT NOT -from:me`,
# `NOT - from:me` and `- NOT from:me` those of `-from:me`; `NOT -has:attachment` and
# `NOT NOT has:attachment` those of `has:attachment`. In small capitals `not` is a word:
# `not has:attachment` and `Not has:attachment` answered the ids of `has:attachment not`.
# Measured again on a Workspace mailbox of 664 messages on 2026-10-25: `NOT -from:me`,
# `NOT NOT NOT -from:me`, `NOT -(from:me)`, `NOT -label:inbox`, `NOT -subject:probe`, `NOT -in:sent`
# and `NOT -has:attachment` answered the ids of the operator without its `-`, and
# `probe NOT -from:me` those of `probe from:me`. A `-` touching a `NOT` flips as another `NOT`
# would: `-NOT from:me` answered as `NOT NOT from:me` does and `NOT -NOT from:me` as
# `NOT NOT NOT from:me`. An operator with no value leaves the run no term to fall on (the comment at
# `_gmail_not_words`): `NOT subject:"`, `NOT label:""` and `NOT -NOT label:` answered nothing, and
# `NOT NOT subject:"`, `-NOT subject:` and `-NOT subject:"` every message. A no-break space is no
# separator in the run: `NOT<no-break space>from:me` answered nothing, as one term no message holds.
_GMAIL_NOT = re.compile(
    r"(?:^|(?<=[^\S\xa0]))((?:(?:-*)NOT[^\S\xa0]+(?:-+[^\S\xa0]+)*)+)(-*)"
    r"(?=(?i:from|to|cc|bcc|subject|label|in|has|after|before|newer|older|newer_than|older_than):)"
)
_GMAIL_NOT_DASH = re.compile(r"-(?=NOT)")
# A `NOT` in a run of them, with any `-` touching it.
_GMAIL_NOT_TERM = re.compile(r"-*NOT")
_GMAIL_TRAILING_NOTS = re.compile(r"\s*-*NOT\s*\Z")
# `NOT`s glued by no-break spaces are one term, and the term reads as the word `not`: measured
# on 2026-10-22, `NOT<no-break space>NOT from:me` answered what `not from:me` answers.
_GMAIL_NOT_GLUE = re.compile(r"NOT(?:\xa0NOT)+")
_GMAIL_NEGATION = {"after": "before", "before": "after", "newer": "older", "older": "newer"}
# A `-` in front of an empty operator `_GMAIL_OP` drops is moved onto the operator after it.
# Measured on a Workspace mailbox, comparing `messages.list` id sets: on 2026-10-19,
# `-subject: label:inbox`, `-label: label:inbox`, `-from: label:inbox` and `-from:label:inbox`
# answered the ids of `-label:inbox`; on 2026-10-20, so did `-in: label:inbox`, `-to: label:inbox`,
# `-subject:label:inbox`, `-subject:<tab>label:inbox`, `-SUBJECT: LABEL:inbox`,
# `-subject:"" label:inbox`, `-from:"" label:inbox` and `-label:" " label:inbox`, and
# `-subject: in:sent`, `-cc: has:attachment`, `-from: in:anywhere`, `-from: after:2026/10/01` and
# `-to: newer_than:30d` answered the ids of the operator after the empty one with a `-` in front.
# Where the empty operator is kept the `-` stays on it: `-from: from:me`, `-subject: subject:probe`,
# `-label:label:inbox` and `-in:label:inbox` answered every message, and `-has: label:inbox`,
# `-after: label:inbox` and `-newer: label:inbox` the ids of `label:inbox -has`,
# `label:inbox -after` and `label:inbox -newer`.
_GMAIL_NEGATED_EMPTY = re.compile(rf"(?<!\S)-+{_GMAIL_EMPTY_BEFORE}", re.IGNORECASE)
# A `-` flips an age operator and negates `has:`: on 2026-10-20 `-newer_than:30d` answered the ids
# of `older_than:30d`, `-older_than:30d` those of `newer_than:30d`, and `-has:attachment` the
# messages `has:attachment` does not.
_GMAIL_AGE_NEGATION = {"newer_than": "older_than", "older_than": "newer_than"}
# A `(…)` group, read before the operators (`_gmail_groups`), measured with the queries at
# `_GMAIL_NOT`: `(from:me)`, `((from:me))`, `( from:me )`, `(from:me` and `from:me)` answered the
# ids of `from:me`, and `probe(from:me)` and `(from:me) probe` those of `from:me probe`;
# `-(from:me to:me)`, `-((from:me) to:me)`, `-(from:me (to:me))`, `-( from:me to:me )` and
# `-(from:me to:me`, with no `)`, answered the messages `from:me to:me` does not, `-(probe from:me)`
# those `from:me probe` does not, and `-(NOT from:me to:me)` and `-(-from:me to:me)` those
# `-from:me to:me` does not; `-(from:me)`, `-((from:me))`, `-(from:me`, `-(-from:me)`, `--(from:me)`
# and `-(NOT NOT from:me)` answered the ids of `-from:me`, and `-(-(from:me to:me))` and
# `--(from:me to:me)` those of `-(from:me to:me)`; `-( )`, `-()`, `(` and `)` every message. After
# `NOT` the brackets are no group, and `NOT` falls on the first term inside them:
# `NOT (from:me to:me)`, `NOT((from:me to:me))` and `NOT (from:me) to:me` answered the ids of
# `-from:me to:me`, `NOT (-from:me to:me)` those of `from:me to:me`, and `NOT -(from:me to:me)`
# those of `from:me to:me` as well. `threads.list` answered `-(from:me to:me)`, `-(probe from:me)`
# and `NOT (from:me to:me)` alike. A `)` with a `-` and more after it, and a `-(` after a word, are
# no brackets: `-(from:me)-(to:me)` and `-(from:me)-to:me` answered every message, and
# `(from:me)-(to:me)`, `from:me-(to:me)` and `probe-(from:me)` nothing, where `-(from:me)- (to:me)`
# answered the ids of `-from:me to:me` and `-(from:me)-` those of `-from:me`. A lone quote
# before a group no `)` closes is read at `_GMAIL_LONE_QUOTE_UNCLOSED`. A no-break space is no
# separator here or anywhere in `_parse_gmail_q`: measured on 2026-10-22,
# `probe<no-break space>from:me` and `from:me<no-break space>to:me` answered nothing,
# `NOT<no-break space>from:me` nothing, and `-(from:me<no-break space>to:me)` every message — the
# no-break space is part of the term it touches, where after `subject:` it reads as whitespace (the
# measurements at `_GMAIL_SPACED`).
_GMAIL_GROUP_PIECE = re.compile(
    r'[ \t\n\r\f\v]+|"[^"]*"|'
    r'(?:(?<=[^ \t\n\r\f\v(-])-+\(|\)(?=-+[^ \t\n\r\f\v])|[^ \t\n\r\f\v()"])+|[()]|"'
)
# The `ops` key holding the inner text of each `-(…)` group of more than one term, whose messages
# `_gmail_query` leaves out. No operator can be named this, since `_GMAIL_OP` reads a name of word
# characters.
_GMAIL_NOT_ALL = "-()"
# The `ops` key holding each word a leading `-` or a capital `NOT` leaves out
# (:func:`_gmail_take_negated_words`). No operator can be named this either.
_GMAIL_NOT_WORD = "-w"
_GMAIL_DASHED_WORD = re.compile(r'-+(?:"([^"]*)"|([^ ]+))\Z')
# A free-text term: a quoted stretch, spaces and all, with what touches it, or a run of other
# characters between spaces.
_GMAIL_FREE_TERM = re.compile(r'[^ "]*"[^"]*"[^ "]*|[^ ]+')


def _gmail_q_date(v: str) -> str | None:
    """The epoch seconds an `after:` or `before:` value in a search names, or None for a value real
    answers with nothing (the reading the comment at `_GMAIL_DATE_KEYS` measures)."""
    v = v.rstrip("/.,:")
    if re.fullmatch(r"[0-9]+", v):
        return str(int(v)) if 0 < int(v) <= _GMAIL_SECONDS_MAX else None
    m = _GMAIL_DATE.fullmatch(v)
    if m is None:
        return None
    a, b, c = m.group(1), m.group(3), m.group(4)
    for y, mo, d in [(a, b, c)] if len(a) > 2 else [(a, b, c), (c, a, b), (c, b, a)]:
        year = int(y) + 2000 if 0 < int(y) < 100 else int(y)
        try:
            day = datetime.datetime(year, int(mo), int(d), tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        return str(int(day.timestamp()))
    return None


def _gmail_negated_date(m: re.Match) -> str:
    """A negated date operator as the one it flips to, or nothing at all when its value does not
    read (the comment at `_GMAIL_DATE_KEYS`)."""
    if _gmail_q_date(m.group(2)) is None:
        return " "
    return f"{_GMAIL_NEGATION[m.group(1).lower()]}:{m.group(2)}"


def _gmail_lone_quote(m: re.Match) -> str:
    """A lone quote's operator with the rest of the search as its quoted value, or the one operator
    after it on its own (the comment at `_GMAIL_LONE_QUOTE`)."""
    dashes, key, gap, rest = m.group(1), m.group(2), m.group(3), m.group(4).rstrip()
    alone = _GMAIL_LONE_QUOTE_OPERATOR.fullmatch(rest) if gap else None
    if alone:
        return f'{dashes}{alone.group(1)}"{alone.group(2)}"'
    return f'{dashes}{key}"{rest.lstrip("-")}"'


def _gmail_not(m: re.Match) -> str:
    """A run of `NOT`s in front of an operator as the `-` it leaves on it, or nothing: each `NOT`
    in the run, each `-` touching one and a `-` touching the operator flip it once more (the
    measurements at `_GMAIL_NOT`). In front of an operator with no value the run stays as it is,
    for `_gmail_not_words`: on 2026-10-25 `NOT in:` and `NOT from:` answered nothing,
    `NOT NOT from:` every message, and `NOT has:` and `NOT after:` what `-has` and `-after`
    answer, an empty `has:` or `after:` being the word it spells."""
    if not _GMAIL_OP.match(m.string, m.end()).group(2):
        return m.group(0)
    run = m.group(1)
    flips = run.count("NOT") + len(_GMAIL_NOT_DASH.findall(run)) + bool(m.group(2))
    return "-" if flips % 2 else ""


def _gmail_not_words(words: list[str]) -> tuple[list[str], bool]:
    """The free-text words with each run of `NOT`s read, and whether the search answers nothing.

    Each `NOT` in a run and each `-` touching one flips what follows it. A run in front of a word
    becomes one `NOT` when it flips an odd number of times and nothing when even, for
    :func:`_gmail_take_negated_words`; a run with no term after it makes the search answer nothing
    when it flips an odd number of times and is left out when even. Measured with the other
    2026-10-25 queries at `_GMAIL_NOT`: `NOT`, `NOT ()`, `NOT -`, `(NOT)`, `NOT NOT NOT`,
    `NOT -NOT`, `-NOT NOT`, `NOT ""`, `probe NOT`, `from:me NOT`, `probe NOT NOT NOT`,
    `NOT probe NOT` and `(probe) NOT` answered nothing; `NOT NOT`, `NOT NOT NOT NOT`, `-NOT`,
    `-NOT -NOT`, `-(NOT)`, `NOT NOT -` and `NOT NOT ""` every message; `NOT NOT probe`,
    `-NOT probe`, `probe NOT NOT` and `probe -NOT` what `probe` answers.
    """
    out: list[str] = []
    i = 0
    while i < len(words):
        j, flips = i, 0
        while j < len(words) and _GMAIL_NOT_TERM.fullmatch(words[j]):
            flips += 1 + words[j].startswith("-")
            j += 1
        if j == i:
            out.append(words[i])
            i += 1
            continue
        if j == len(words):
            return out, bool(flips % 2)
        out += ["NOT"] * (flips % 2)
        i = j
    return out, False


def _gmail_group_items(text: str) -> list:
    """The top-level items of ``text``: each run of characters between whitespace and brackets, and
    each `(…)` group as ``(negated, inner text)``, a group running to its `)` or to the end. A group
    is negated when a run of `-` touches its `(`; a quoted run is one item, brackets and all."""
    pieces = _GMAIL_GROUP_PIECE.findall(text)
    items: list = []
    run, i = "", 0
    while i < len(pieces):
        piece = pieces[i]
        if piece == "(":
            depth, j = 1, i + 1
            while j < len(pieces) and depth:
                depth += {"(": 1, ")": -1}.get(pieces[j], 0)
                j += 1
            negated = bool(run) and not run.strip("-")
            if run and not negated:
                items.append(run)
            items.append((negated, "".join(pieces[i + 1 : j - 1 if depth == 0 else j])))
            run, i = "", j
            continue
        if piece.isspace() or piece == ")":
            if run:
                items.append(run)
            run = ""
        else:
            run += piece
        i += 1
    if run:
        items.append(run)
    return items


def _gmail_flatten(items: list, out: list, negated: list) -> None:
    """Write ``items`` into ``out`` as terms, as the comment at `_GMAIL_GROUP_PIECE` measures: a
    group's brackets drop, a `-(…)` of one term puts a `-` on that term, and one of several terms
    goes into ``negated`` whole, each `NOT` in front of it flipping it."""
    for item in items:
        if isinstance(item, str):
            out.append(item)
            continue
        is_negated, inner = item
        sub = _gmail_group_items(inner)
        if not is_negated:
            _gmail_flatten(sub, out, negated)
            continue
        # Inside `-(…)` a run of `NOT`s with no term after it is left out: `-(NOT NOT)` answered
        # every message and `-(probe NOT)` the messages `probe` does not, on 2026-10-25.
        while sub and isinstance(sub[-1], str) and _GMAIL_NOT_TERM.fullmatch(sub[-1]):
            sub.pop()
            inner = _GMAIL_TRAILING_NOTS.sub("", inner)
        terms = [s for s in sub if not (isinstance(s, str) and not s.strip("-"))]
        if len(terms) == 1 and isinstance(terms[0], str):
            out.append("-" + terms[0].lstrip("-"))
            continue
        if len(terms) == 1:  # `-((…))` and `-(-(…))` are `-(…)`
            _gmail_flatten([(True, terms[0][1])], out, negated)
            continue
        if not terms:
            continue
        nots = 0
        while out and out[-1] == "NOT":
            out.pop()
            nots += 1
        if nots % 2:
            _gmail_flatten(sub, out, negated)
        else:
            negated.append(inner)


def _gmail_groups(q: str) -> tuple[str, list[str]]:
    """``q`` with its `(…)` groups read (`_gmail_flatten`), and the inner text of each `-(…)` of
    several terms."""
    if "(" not in q and ")" not in q:
        return q, []
    out: list[str] = []
    negated: list[str] = []
    _gmail_flatten(_gmail_group_items(q), out, negated)
    return " ".join(out), negated


def _gmail_take_negated_words(free: str) -> tuple[str, list[str]]:
    """``free`` without the words a leading `-` or a capital `NOT` leaves out, and those words.

    Measured on a Workspace mailbox of 635 messages on 2026-10-20, comparing `messages.list` id
    sets with a baseline taken the same minute: `-probe`, `--probe`, `-PROBE` and `-"probe"` each
    answered the messages `probe` does not, and so did `NOT probe` and `-( probe )`.
    `label:inbox -probe` answered the inbox messages `probe` leaves out, `probe -probe` answered
    nothing, and `-attachment`, a word no message holds, answered every message. Only `NOT` in
    capitals reads this way: `not from:me` answered as `from:me not`. On 2026-10-22 (655
    messages), `-"backlot probe"`, `NOT "backlot probe"` and `-("backlot probe")` answered the
    messages the phrase `"backlot probe"` does not, and `-"probe backlot"`, a phrase no message
    holds, every message; `NOT -probe`, `NOT --probe`, `NOT NOT probe` and `NOT NOT NOT -probe`
    answered what `probe` answers, and `NOT NOT NOT probe` what `-probe` answers, each `NOT` and a
    `-` touching the word flipping it; and `-probe+` and `NOT probe+` answered every message, where
    `probe+` answers nothing. A `-(…)` of one word arrives here as that word with a `-` in front
    (:func:`_gmail_flatten`); one of several terms is :data:`_GMAIL_NOT_ALL`. The word is then
    matched by the same search the word alone runs, and those messages are left out, so `-plan` on
    its own has no free text left and the listing path answers.
    """
    kept: list[str] = []
    negated: list[str] = []
    terms = _GMAIL_FREE_TERM.findall(free)
    i = 0
    while i < len(terms):
        j = i
        while j < len(terms) and terms[j] == "NOT":
            j += 1
        dashed = _GMAIL_DASHED_WORD.fullmatch(terms[j]) if j < len(terms) else None
        if dashed:
            word = (dashed.group(1) if dashed.group(1) is not None else dashed.group(2)).strip('"')
        else:
            word = terms[j].strip('"') if i < j < len(terms) else None
        if not word:
            kept += terms[i : j + 1]
        elif (j - i + bool(dashed)) % 2 == 0:
            kept.append(terms[j].lstrip("-"))
        elif not word.endswith("+"):
            negated.append(word)
        i = j + 1
    return " ".join(kept), negated


def _gmail_closed_quote(m: re.Match) -> str:
    """The operator a quote that whitespace follows and a later quote closes stands for: the one
    operator inside it on its own, the operator the closing quote opens a value for, or the
    operator with what is inside the quotes as its value (the comment at `_GMAIL_CLOSED_QUOTE`)."""
    dashes, key, content = m.group(1), m.group(2), m.group(3).rstrip()
    if _GMAIL_BARE_OPERATOR.fullmatch(content):
        return f'{dashes}{content.lstrip("-")}"'
    alone = _GMAIL_LONE_QUOTE_OPERATOR.fullmatch(content)
    if alone:
        return f'{dashes}{alone.group(1)}"{alone.group(2)}"'
    return f'{dashes}{key}"{_GMAIL_TRAILING_OPERATOR.sub("", content)}"'


def _parse_gmail_q(q: str) -> tuple[str, dict]:
    """Split a Gmail search `q` into (free_text, operators). Honors from:/to:/cc:/bcc:/subject:/
    after:/before:/newer:/older:/newer_than:/older_than:/label:/in:/has: — the rest is free text
    matched full-text."""
    q = _GMAIL_QUOTED_OPERATOR.sub(r"\1\2", q)
    lone = _GMAIL_LONE_QUOTE_UNCLOSED.search(q)
    if lone and lone.group(3).count("(") > lone.group(3).count(")"):
        if (lone.group(1).count("NOT") + bool(lone.group(2))) % 2 == 0:
            return "", {_GMAIL_NOTHING: [lone.group(0)]}
        q = q[: lone.start()]
    ops: dict[str, list[str]] = {}

    def _take(m):
        key = m.group(1).lower()
        if key in _GMAIL_KEYS:
            key = _GMAIL_KEY_ALIASES.get(key, key)
            value = m.group(2)
            if not value and key not in _GMAIL_ADDRESS_KEYS:
                if key in _GMAIL_DROPPED_WHEN_EMPTY:
                    return " "
                return m.group(0)
            # A value is no age when it starts with a `"` or has a `"` followed by anything but
            # quotes, and with a `-` in front it is left out; quotes that end an age are left out of
            # it. Measured on a Workspace mailbox on 2026-10-21 and 2026-10-22: `newer_than:"`,
            # `older_than:"`, `newer_than:""`, `older_than:""`, `newer_than:"30d"`,
            # `newer_than:" 30d`, `newer_than:"probe` and `newer_than:30"d` answered nothing, and
            # `-newer_than:"`, `NOT newer_than:"`, `-older_than:""` and `-newer_than:"30d"` every
            # message; `newer_than:30d"`, `newer_than:30d""` and `newer_than:30d"""` answered what
            # `newer_than:30d` answers, and `older_than:1d"`, `older_than:30d"` and
            # `older_than:30d"" "` what `older_than:1d` and `older_than:30d` answer.
            if key in _GMAIL_AGE_NEGATION and (value[:1] == '"' or '"' in value.rstrip('"')):
                if not m.group(0).startswith("-"):
                    ops.setdefault(_GMAIL_NOTHING, []).append(value)
                return " "
            if key in _GMAIL_DATE_KEYS:
                if value == "-":
                    return m.group(0)
                read = _gmail_q_date(value)
                ops.setdefault(_GMAIL_NOTHING if read is None else key, []).append(read or value)
                return " "
            # A no-break space in a value: the measurements at `_GMAIL_SPACED_NBSP`.
            if key == "subject":
                value = value.strip("\xa0")
            elif "\xa0" in value:
                if not (m.group(0).startswith("-") or value.startswith("-")):
                    ops.setdefault(_GMAIL_NOTHING, []).append(value)
                return " "
            stored = key
            if key in _GMAIL_ADDRESS_KEYS and value.startswith("-"):
                value, stored = value.lstrip("-"), f"-{key}"
                if not _GMAIL_ADDRESS_WORD.search(value):
                    return " "
            if key in _GMAIL_NEGATABLE:
                spaced = m.start(2) > m.end(1) + 1
                if value[:1] == "-" and (key == "subject" or spaced):
                    value, stored = value.lstrip("-"), f"-{key}"
                    if not value:
                        return " "
                elif m.group(0).startswith("-"):
                    stored = f"-{key}"
            elif m.group(0).startswith("-"):
                if key in _GMAIL_AGE_NEGATION:
                    stored = _GMAIL_AGE_NEGATION[key]
                elif key == "has":
                    stored = "-has"
                else:
                    return m.group(0)
            if key in _GMAIL_PLUS_ENDS and not value.startswith('"'):
                value = value.lstrip("+")
                if re.search(r"[^@]@", value):
                    value = value.rstrip("+")
                else:
                    value, plus, term = value.partition("+")
                    if plus and not term.strip("+"):
                        ops.setdefault(_GMAIL_NOTHING, []).append(value)
                        return " "
                    if plus:
                        ops.setdefault(stored, []).append(value)
                        return f" +{term} "
            # A quoted address value keeps its quotes, which `_gmail_address_match` reads.
            quoted = (
                key in _GMAIL_ADDRESS_KEYS and len(value) > 1 and value[:1] == value[-1:] == '"'
            )
            ops.setdefault(stored, []).append(value if quoted else value.strip('"'))
            return " "
        return m.group(0)

    trailing = re.search(r'\S[ \t]+"[ \t]*$', q)
    hyphened = []

    def _hyphened(m):
        if m.string.count('"', 0, m.start()) % 2:
            return m.group(0)
        if (m[1].count("NOT") + bool(m[2])) % 2 == 0:
            hyphened.append(m.group(0))
        return " "

    q = _GMAIL_HYPHENED.sub(_hyphened, _GMAIL_BARE_LONE_QUOTE.sub("", q))
    if hyphened:
        return "", {_GMAIL_NOTHING: hyphened}
    q, groups = _gmail_groups(q)
    if groups:
        ops[_GMAIL_NOT_ALL] = groups
    q = _GMAIL_CLOSED_QUOTE.sub(_gmail_closed_quote, q)
    q = _GMAIL_LONE_QUOTE.sub(_gmail_lone_quote, q)
    q = _GMAIL_NOT.sub(_gmail_not, q)
    q = _GMAIL_NEGATED_EMPTY.sub("-", q)
    q = _GMAIL_NEGATED_DATE.sub(_gmail_negated_date, q)
    q = _GMAIL_NEGATED_ADDRESS.sub(lambda m: f"{m.group(1)}:-", q)
    free = re.sub(r"[^\S\xa0]+", " ", _GMAIL_OP.sub(_take, q)).strip(" \t\n\r\f\v")
    # A `+` or a `-` standing alone is no search term, and a word ending in a `+` matches nothing
    # (the measurements at `_GMAIL_PLUS_ENDS` and `_GMAIL_NEGATABLE`). Nor is a pair of quotes
    # holding nothing, or a no-break space alone: on 2026-10-25 `probe ""` and `"" probe` answered
    # what `probe` answers, `NOT "" probe` what `-probe` answers, and `from:me <no-break space>`
    # what `from:me` answers. Negated words come out first, so a `-` on a word ending in a `+`
    # does not mark the whole search as matching nothing.
    if not (len(free) >= 2 and free[0] == free[-1] == '"'):
        words = [t for t in free.split(" ") if t.strip("\xa0") not in ("", "+", "-", '""')]
        words = ["not" if _GMAIL_NOT_GLUE.fullmatch(t) else t for t in words]
        words, nothing = _gmail_not_words(words)
        if nothing:
            ops.setdefault(_GMAIL_NOTHING, []).append(free)
        free, negated = _gmail_take_negated_words(" ".join(words))
        if negated:
            ops[_GMAIL_NOT_WORD] = negated
        if any(t.endswith("+") for t in free.split(" ")):
            ops.setdefault(_GMAIL_NOTHING, []).append(free)
    # A lone quote that no search term follows is no term beside the operators read; one in front of
    # the operator is read at `_GMAIL_BARE_LONE_QUOTE`, where a search with two answers nothing, and
    # an operator left with no value is not counted (`" from:` there). Measured on a Workspace
    # mailbox on 2026-10-22: `label:inbox "` and `"label:inbox` answered what `label:inbox` answers,
    # `from:me "` what `from:me` answers, and `label:inbox -from:me "` and `from:me to:me "` what
    # `label:inbox -from:me` and `from:me to:me` answer.
    valued = sum(1 for values in ops.values() for value in values if value)
    if free == '"' and valued and (trailing or valued == 1):
        free = ""
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


# What `_gmail_address_match` reads: a word is a run of letters, digits, `_` and `&` in any
# script, and a value shaped like one address is compared with each address the header holds.
_GMAIL_ADDRESS_WORD = re.compile(r"[\w&]+")
_GMAIL_ONE_ADDRESS = re.compile(r"[^@\s]+@[^@\s]+")
_GMAIL_ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
# What keeps a hyphenated value from being read joined as well (`_gmail_address_match`).
_GMAIL_UNJOINED = re.compile(r"--|[\w&]\.[\w&]")


def _gmail_address_match(header: str | None, value: str) -> bool:
    """Whether an address header answers an address search value as Gmail answers it, in any case.
    A value shaped like one address matches an address in the header exactly: `lena@acme.com`
    does, `lena@acme` names no address and matches nothing. Any other value matches when its words
    come one after another among the header's words, display names included: `lena`, `acme`,
    `acme.com`, `@acme.com` and `lena@` match `Lena Park <lena@acme.com>`, and so does `park`,
    where `len`, `ena`, `par` and `com.acme` do not. `_` joins the characters around it into one
    word, so `ops` does not match `ops_team@corp.example`, and `&` counts as a word character too,
    where the rest of ASCII punctuation does not. A value with no word in it, such as `.`, `@` or
    `""`, is ignored and matches every message, a message without the header included. A value
    with a hyphen matches its words joined as well (`no-reply` finds `noreply@` beside
    `no-reply@`, `jt-snowball` finds `@jtsnowball.com`) unless it holds `--` or a `.` between two
    word characters (`jt-snowball.com` is its three words alone); a joined value does not find the
    hyphenated spelling. A quoted value is matched without its quotes and with no joined spelling:
    `from:"no-reply"` finds `no-reply@` and not `noreply@`.

    Measured on a Workspace mailbox of 595 messages on 2026-10-15: 113 searches on `cc:` and
    `bcc:`, compared with the headers of every message, all match this rule, and of the 32 ASCII
    punctuation marks sent alone as a `cc:` value, `_` and `&` match nothing and the other 30 match
    every message. On the same mailbox at 601 messages on 2026-10-16, 122 of 124 `from:` and `to:`
    searches built from its own addresses, local parts, domains and display-name words match this
    rule over `From`, and over `To`, `Cc` and `Bcc` together; `to:me` counts the four messages
    where the caller is in `Bcc` alone. The other two are a hyphenated value, read joined as above,
    and part of a Korean display name. At 603 messages on 2026-10-17, the 311 `from:`, `to:` and
    `cc:` searches with a hyphen in them (a header's words split by one hyphen and by two, into two
    and three parts, beside an `@` or a `.`, and each hyphenated local part whole and in pairs),
    none quoted and none led by `-`, match this rule, and at 604 on 2026-10-18 so did `bcc:` on a
    word of the one `Bcc` address there, split by one hyphen and by two and beside a `.`. At 624
    on 2026-10-19 so did 444 searches on the four operators, compared with the headers of every
    message in `messages.list` and `threads.list`: 27 words of five letters or more from the
    mailbox's local parts, domain labels and display names, each whole, split by a hyphen, both of
    those quoted, and the quoted two negated; the 7 domain labels among them split after an `@`,
    quoted and not; and the 37 local parts the mailbox holds that are letters and digits joined by
    `-`, each whole, joined, both of those quoted, negated quoted and not, and quoted with its
    domain and as `@<domain>`. A value led by `-` is the negation the comment at
    `_GMAIL_NEGATED_ADDRESS` measures. On 2026-10-14, `cc:<a local part>+x` read `+x` as a search
    term of its own."""
    header = (header or "").lower()
    value = value.lower()
    # A value the parser kept its quotes around is read alone: no joined spelling beside its words.
    quoted = len(value) > 1 and value[:1] == value[-1:] == '"'
    if quoted:
        value = value[1:-1]
    if _GMAIL_ONE_ADDRESS.fullmatch(value):
        return value in _GMAIL_ADDRESS.findall(header)
    words, have = _GMAIL_ADDRESS_WORD.findall(value), _GMAIL_ADDRESS_WORD.findall(header)
    if not words:
        return True
    alternatives = [words]
    if not quoted and "-" in value and not _GMAIL_UNJOINED.search(value):
        alternatives.append(_GMAIL_ADDRESS_WORD.findall(value.replace("-", "")))
    return any(
        have[i : i + len(option)] == option
        for option in alternatives
        for i in range(len(have) - len(option) + 1)
    )


def _gmail_op_match(row, ops: dict) -> bool:
    if _GMAIL_NOTHING in ops:
        return False
    for v in ops.get("from", []):
        if not _gmail_address_match(row["author_email"], v):
            return False
    for v in ops.get("to", []):
        if not any(_gmail_address_match(row[k], v) for k in ("to_addr", "cc", "bcc")):
            return False
    for key in ("cc", "bcc"):
        for v in ops.get(key, []):
            if not _gmail_address_match(row[key], v):
                return False
    for v in ops.get("-from", []):
        if _gmail_address_match(row["author_email"], v):
            return False
    for v in ops.get("-to", []):
        if any(_gmail_address_match(row[k], v) for k in ("to_addr", "cc", "bcc")):
            return False
    for key in ("cc", "bcc"):
        for v in ops.get(f"-{key}", []):
            if _gmail_address_match(row[key], v):
                return False
    for v in ops.get("subject", []):
        if v.lower() not in (row["title"] or "").lower():
            return False
    for v in ops.get("-subject", []):
        if v.lower() in (row["title"] or "").lower():
            return False
    # `in:` and `label:` ask the same question of a message — Gmail's folders ARE labels — and both
    # have to see the label a message is served under rather than only a stated one. `in:anywhere`
    # is the exception: it widens the search to spam and trash, which Backlot holds none of, so
    # it restricts nothing.
    labels = [x.lower() for x in store.jcol(row, "label_ids")] or [_GMAIL_DEFAULT_LABEL.lower()]
    for v in ops.get("label", []) + [x for x in ops.get("in", []) if x.lower() != "anywhere"]:
        if v.lower() not in labels:
            return False
    for v in ops.get("-label", []) + ops.get("-in", []):
        if v.lower() in labels:
            return False
    if any(v.lower() == "anywhere" for v in ops.get("-in", [])):
        return False
    if any(v.lower() == "attachment" for v in ops.get("has", [])) and not store.jcol(
        row, "attachments"
    ):
        return False
    if any(v.lower() == "attachment" for v in ops.get("-has", [])) and store.jcol(
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


def _gmail_query(conn, mailbox, ids, q: str, caller_email: str | None = None) -> list:
    """Full ACL+mailbox-filtered match set for a Gmail `q` (FTS-ranked when free text is
    present; otherwise the mailbox listing). The caller paginates the returned rows."""
    free, ops = _parse_gmail_q(q)
    groups = ops.pop(_GMAIL_NOT_ALL, [])
    negated_words = ops.pop(_GMAIL_NOT_WORD, [])
    if caller_email:
        # `me` reads through quotes: on 2026-10-19, `op:"me"` and `-op:"me"` answered what `op:me`
        # and `-op:me` answer on each address operator.
        for key in ("from", "to", "cc", "bcc", "-from", "-to", "-cc", "-bcc"):
            if key in ops:
                ops[key] = [caller_email if v.strip('"').lower() == "me" else v for v in ops[key]]
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
    rows = [r for r in cand if _gmail_op_match(r, ops)]
    for word in negated_words:
        # The same search the word alone runs, so `-probe` leaves out what `probe` answers.
        # A quoted phrase of several words is matched adjacently, as a quoted free-text term is.
        phrase = " " in word
        excluded = {
            row["id"]
            for row in store.search_documents(
                conn,
                word,
                "gmail",
                ids,
                limit=100_000,
                offset=0,
                container=mailbox,
                phrase=phrase,
            )
        }
        rows = [row for row in rows if row["id"] not in excluded]
    for group in groups:
        out = {r["id"] for r in _gmail_query(conn, mailbox, ids, group, caller_email)}
        rows = [r for r in rows if r["id"] not in out]
    return rows


# --- Gmail ids ------------------------------------------------------------------------------
# A gmail id is a 16-hex integer (`synth.gmail_message_id`) and it IS the row's primary key
# (`gmail_messages.id`, assigned at import — see `backlot.importer.byo`), so resolution is a point
# lookup rather than a map rebuilt on every boot. `thread_id` holds the thread's id, computed at
# import the same way (see `gmail_messages` in `store.SCHEMA`), so a thread resolves through a
# stored column too, with no re-derivation.

_GMAIL_HEX = re.compile(r"[0-9a-fA-F]+\Z")


def _gmail_check_shape(served_id: str) -> None:
    """Raises if ``served_id`` isn't a parsable, in-range hex id — the check every gmail
    id-resolving path but ``attachments.get`` must run BEFORE any lookup, so an unparsable id is
    400 INVALID_ARGUMENT regardless of whether it would otherwise resolve.

    Measured against the real API: 400 INVALID_ARGUMENT "Invalid id value" for a non-hex id or one
    >= 2**63, 404 only for a well-formed id it does not hold. `7fffffffffffffff` is well-formed;
    `8000000000000000` is not."""
    if not _GMAIL_HEX.fullmatch(served_id) or int(served_id, 16) >= synth.GMAIL_ID_MAX:
        raise gerr.invalid_id_value()


def _gmail_resolve(served_id: str) -> str:
    """Validate a served Gmail id's SHAPE and return its stored spelling
    (`store.gmail_id_spelling`), the key `store.gmail_thread` matches against `thread_id`.

    Kept as a named step rather than inlined because the shape check must run BEFORE any lookup:
    an unparsable id is 400 INVALID_ARGUMENT whether or not it would have resolved. No
    ``visible_ids``: the ACL read stays in the caller (`store.gmail_thread`), so an id naming a
    thread the caller cannot see is not-found, never a different answer."""
    _gmail_check_shape(served_id)
    return store.gmail_id_spelling(served_id)


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


def _gmail_max_results(request: Request) -> list[int]:
    """Every `maxResults` the request sends, each read as Gmail's proto layer reads it, a uint32
    (:func:`_gmail_uint32`). Where that refusal stands is :func:`_refuse_another_mailbox`'s to say.
    What the method makes of the values is :func:`_gmail_page`'s."""
    return _typed_query(request, {"maxResults": _gmail_uint32})["maxResults"]


def _gmail_page(request: Request, listing: str, sizes: list[int]) -> tuple[int, int]:
    """The page size and offset `messages.list` and `threads.list` serve from the `maxResults`
    values in ``sizes``, refused in the order real refuses them on each.

    Measured on gmail.googleapis.com on 2026-10-07 with a Workspace user's `gmail.readonly` token,
    one request per row; `threads.list` answered every row with the same status and error, on
    2026-10-07 or 2026-10-08. The proto layer parses every repeat as a uint32 and names every repeat
    it cannot read in one 400, as `Invalid value at 'max_results' (TYPE_UINT32), "<value>"`, the
    value quoted as sent. `+2` and `02` are numbers. A leading `-` is refused even on `-0`, and so
    are an empty value, `1.5`, the Arabic-Indic digit `٣` and a value past 2**32 - 1 (`4294967296`).
    The method reads the last repeat (the pair is in `gerr.first_repeat`'s table): `0` (`+0`, `00`)
    and anything from 2**31 up (`2147483648`, `+2147483648`, `4294967295`) are `Invalid maxResults`,
    while `2147483647` is served; `0&3` is 3 and `3&0` is refused. Below 2**31 a value is capped at
    500, not refused: on 2026-10-03, `501` and `1000` each answered 500 messages with a
    `nextPageToken`, and the reference gives both methods "The maximum allowed value for this field
    is 500". With no `maxResults`, the page is the default size capped at 500. A sent value is also
    capped at the deployment's `max_page_size`.

    The uint32 refusal (:func:`_gmail_max_results`) comes first on both listings. After it
    `messages.list` refuses a `pageToken` that does not parse (:func:`_gmail_page_token`) before a
    `maxResults` out of range, and `threads.list` the range before the token: measured on 2026-10-09
    with `maxResults` of `0` and `2147483648` beside a token that does not parse, each request sent
    four times with the same answer.
    """
    offset = _gmail_page_token(request) if listing == "messages" else None
    if not sizes:
        limit = min(get_settings().default_page_size, 500)
    elif sizes[-1] == 0 or sizes[-1] >= 2**31:
        raise gerr.invalid_max_results()
    else:
        limit = min(sizes[-1], 500, get_settings().max_page_size)
    if offset is None:
        offset = _gmail_page_token(request)
    return limit, offset


def _gmail_page_token(request: Request) -> int:
    """The offset a Gmail `pageToken` names, read from its last repeat as real reads it. An empty
    token is the first page, and one that does not decode is :func:`gerr.invalid_page_token`."""
    offset = decode_cursor_or_none(request.query_params.get("pageToken"))
    if offset is None:
        raise gerr.invalid_page_token()
    return offset


# `maxResults` as Gmail's proto layer reads it: a uint32, where Drive's `pageSize` is an int32
# (`_INT32`). The spellings and the bound are `_gmail_page`'s.
_UINT32 = re.compile(r"\+?[0-9]+")


def _gmail_uint32(raw: str) -> int:
    if _UINT32.fullmatch(raw) and int(raw) < 2**32:
        return int(raw)
    raise gerr.invalid_field_value(
        "max_results", f"Invalid value at 'max_results' (TYPE_UINT32), \"{raw}\""
    )


def _gmail_ids(row) -> tuple[str, str]:
    """``(id, threadId)`` for a row. A message that is its own thread root reports the same value
    twice, as real Gmail does.

    Both halves are read straight off the row: `thread_id` is computed once at import (see
    `gmail_messages` in `store.SCHEMA`), so `threadId` reads one stored value rather than
    re-hashing the thread's key and hoping the two agree."""
    return (row["id"], row["thread_id"] or row["id"])


@router.get(
    "/gmail/v1/users/{user_id}/messages",
    response_model=GmailMessageList,
    response_model_exclude_unset=True,
    openapi_extra={"parameters": _P_GMAIL_LIST},
)
async def gmail_messages_list(user_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    sizes = _gmail_max_results(request)
    _refuse_another_mailbox(caller, user_id)
    ids = auth.visible_ids(request, caller)
    mailbox = _mailbox_container(conn, caller, user_id)  # None = all mailboxes
    limit, offset = _gmail_page(request, "messages", sizes)
    q = request.query_params.get("q", "") or ""
    if q.strip():  # search: filter the ACL-visible set by the query, then paginate
        matched = _gmail_query(conn, mailbox, ids, q, caller.email or _service_email(request))
        total = len(matched)
        rows = matched[offset : offset + limit]
    else:
        # newest-first by internalDate (created_ts), like real Gmail — NOT id (hash) order, so a
        # capped "newest N" crawl is deterministic by date, not random. Open-ended range = whole box.
        total = store.count_documents(conn, "gmail", container=mailbox, visible_ids=ids)
        rows = store.list_gmail_in_range(conn, mailbox, None, None, ids, limit=limit, offset=offset)
    # threadId must agree with messages.get (a reply belongs to its root's thread)
    messages = [dict(zip(("id", "threadId"), _gmail_ids(r))) for r in rows]
    # A list with no match leaves `messages` out rather than sending `[]` — measured on 2026-09-30,
    # so a client that reads `response["messages"]` meets the KeyError it meets there.
    body = {"messages": messages} if messages else {}
    body["resultSizeEstimate"] = total
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
    caller = _mailbox_caller(request, user_id)
    ids = auth.visible_ids(request, caller)
    row = _gmail_doc(conn, ids, msg_id)
    if row is None:
        raise gerr.not_found_entity()
    return _gmail_message(
        row,
        request.query_params.get("format", "full"),
        caller.email,
        request.query_params.getlist("metadataHeaders"),
    )


def _byte_len(text: str) -> int:
    """The length Gmail reports for `text`, in the UTF-8 bytes `_b64url` serves. Real counts bytes,
    not characters: a part's `size` is its decoded `data`'s, on a text part, an attachment part and
    `attachments.get` alike (measured on 2026-10-02), and a message's `sizeEstimate` is its decoded
    `raw`'s, under `minimal`, `metadata` and `raw` and in `threads.get` (12 messages, measured on
    2026-10-04)."""
    return len(text.encode("utf-8"))


@router.get(
    "/gmail/v1/users/{user_id}/messages/{msg_id}/attachments/{att_id}",
    response_model=GmailAttachment,
)
async def gmail_attachment(user_id: str, msg_id: str, att_id: str, request: Request):
    conn = auth.conn(request)
    caller = _mailbox_caller(request, user_id)
    ids = auth.visible_ids(request, caller)
    # The path's message id plays no part in the answer, in real Gmail or here: an attachment id
    # is served under the message it belongs to, under another message's id, under a well-formed
    # id no message has and under a non-hex id alike, with the same bytes, measured against
    # gmail.googleapis.com on 2026-10-01 and again on 2026-10-07. The message the path names is
    # one point lookup, so it is tried first; only an attachment id it does not hold pays for
    # scanning every attachment-bearing message the caller can see.
    named = store.gmail_by_id(conn, msg_id, visible_ids=ids)
    found = _attachment([] if named is None else [named], att_id) or _attachment(
        store.gmail_rows_with_attachments(conn, visible_ids=ids), att_id
    )
    if found is None:
        raise gerr.invalid_attachment_token()
    message_id, i, att = found
    body = _att_content(message_id, i, att)
    # `{size, data}` alone: real names no `attachmentId` here, measured on 2026-09-30
    return {"size": _byte_len(body), "data": _b64url(body)}


def _attachment(rows, att_id: str) -> tuple[str, int, dict] | None:
    """The message id, index and attachment among ``rows`` whose ``_att_id`` is ``att_id``."""
    return next(
        (
            (row["id"], i, a)
            for row in rows
            for i, a in enumerate(store.jcol(row, "attachments"))
            if _att_id(row["id"], i) == att_id
        ),
        None,
    )


@router.get(
    "/gmail/v1/users/{user_id}/threads",
    response_model=GmailThreadList,
    response_model_exclude_unset=True,
    openapi_extra={"parameters": _P_GMAIL_LIST},
)
async def gmail_threads_list(user_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    sizes = _gmail_max_results(request)
    _refuse_another_mailbox(caller, user_id)
    ids = auth.visible_ids(request, caller)
    # The MAILBOX's threads, like messages.list and like real Gmail — not the threads the caller
    # happens to have written in. Scoping by author served a mailbox owner none of the mail they
    # received, and `q` was already scoping by container, so the two halves of this one listing
    # disagreed about what a thread list is.
    mailbox = _mailbox_container(conn, caller, user_id)
    limit, offset = _gmail_page(request, "threads", sizes)
    q = request.query_params.get("q", "") or ""
    if q.strip():
        # A search returns the THREADS its matches are in: Gmail lists a thread whose match is in a
        # reply, and lists it once even when several of its messages match.
        matched = _by_thread(
            _gmail_query(conn, mailbox, ids, q, caller.email or _service_email(request))
        )
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
    threads = [{"id": _gmail_ids(r)[1], "snippet": _snippet(r), "historyId": "1"} for r in rows]
    # A list with no match leaves `threads` out rather than sending `[]`, measured on 2026-09-30.
    body = {"threads": threads} if threads else {}
    body["resultSizeEstimate"] = total
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
    caller = _mailbox_caller(request, user_id)
    ids = auth.visible_ids(request, caller)
    thread_key = _gmail_resolve(thread_id)
    msgs = store.gmail_thread(conn, thread_key, visible_ids=ids)
    if not msgs:
        row = _gmail_doc(conn, ids, thread_id)
        # Measured against gmail.googleapis.com on 2026-09-30 and 2026-10-01: `threads.get` on a
        # reply's message id returns the same 404 body as a well-formed unknown id, while the
        # thread's own id serves the thread. So the fallback serves a message only when it is its
        # own thread root.
        if row is None or _gmail_ids(row)[0] != _gmail_ids(row)[1]:
            raise gerr.not_found_entity()
        msgs = [row]
    fmt = request.query_params.get("format", "full")
    named = request.query_params.getlist("metadataHeaders")
    # No `snippet`: real serves one on a `threads.list` entry and not on `threads.get`, with or
    # without `format=minimal` — measured on 2026-09-30. An id in uppercase, with zeros in front, or
    # both gets the answer the lowercase id without them gets, `id` included — measured on
    # 2026-10-03 and 2026-10-05.
    return {
        "id": _gmail_ids(msgs[0])[1],
        "historyId": "1",
        "messages": [_gmail_message(m, fmt, caller.email, named) for m in msgs],
    }


def _att_id(message_id: str, i: int) -> str:
    return "ANGjdJ" + synth.gmail_id(message_id, salt=f"att{i}")


def _att_content(message_id: str, i: int, att: dict) -> str:
    """The exact bytes ``attachments.get`` serves for attachment ``i``, and therefore what
    ``messages.get`` reports as that part's ``body.size`` — real Gmail keeps the two equal so a
    client can stat from metadata alone. The corpus-declared ``size`` cannot be honoured with
    placeholder bytes, so the served content's length is the single source of truth."""
    return att.get("content", f"attachment {_att_id(message_id, i)}")


def _header(name: str, value: str) -> dict:
    return {"name": name, "value": value}


# RFC 2047 encoded-word budget is 75 octets. `=?UTF-8?B?` + `?=` leaves 63, and base64 length
# must be a multiple of 4, so 60 chars / 45 UTF-8 bytes per word. RFC 2047 §5 keeps a multi-octet
# character within one word, so a word ends at the last whole character inside those 45 bytes.
# Adjacent words are separated by a space, which a decoder discards (RFC 2047 §6.2).
_ENCODED_WORD_BYTES = 45


def _encoded_words(text: str) -> str:
    """`text` as UTF-8 `B` encoded-words, as many as `_ENCODED_WORD_BYTES` needs."""
    chunks = [b""]
    for char in text:
        encoded = char.encode("utf-8")
        if len(chunks[-1]) + len(encoded) > _ENCODED_WORD_BYTES:
            chunks.append(b"")
        chunks[-1] += encoded
    return " ".join(f"=?UTF-8?B?{base64.b64encode(c).decode('ascii')}?=" for c in chunks)


def _raw_mailbox(display: str, address: str) -> str:
    """One mailbox of an address header, for `_raw_header_value`: only the display name is
    encoded, since RFC 2047 §5 keeps encoded-words out of an addr-spec."""
    if not display.isascii():
        return f"{_encoded_words(display)} <{address}>"
    if not address.isascii():
        # `formataddr` refuses a non-ASCII address
        return f"{display} <{address}>" if display else address
    return formataddr((display, address))


def _raw_header_value(name: str, value: str) -> str:
    """One header's value as `format=raw` writes it. `payload.headers` under `full` and
    `metadata` serve the value as it is.

    Measured on 2026-10-05, on a message composed in the Gmail web client: real's `raw` is ASCII. It
    writes the subject as a UTF-8 `B` encoded-word, a Hangul display name in `From` and `To` as one
    encoded-word before the ASCII `<address>`, with no quotes, and the attachment's `name=` and
    `filename=` as encoded-words inside their quotes. `Cc`, `Bcc` and `Reply-To` were not measured
    and are written the way `To` is; any other header is encoded whole, as the subject is. A
    non-ASCII address is written as it is (see `_raw_mailbox`), so it stays non-ASCII in `raw`.
    """
    if value.isascii():
        return value
    if name.lower() in {"content-type", "content-disposition"}:

        def quoted(match: re.Match) -> str:
            inner = match.group(1)
            if inner.isascii():
                return match.group(0)
            return f'"{_encoded_words(inner)}"'

        return re.sub(r'"([^"]*)"', quoted, value)
    if name.lower() in {"from", "to", "cc", "bcc", "reply-to", "delivered-to"}:
        mailboxes = getaddresses([value])
        if all("@" in address for _, address in mailboxes):
            return ", ".join(_raw_mailbox(display, address) for display, address in mailboxes)
    return _encoded_words(value)


def _raw_header_block(headers: list[dict]) -> str:
    return "\r\n".join(f"{h['name']}: {_raw_header_value(h['name'], h['value'])}" for h in headers)


def _text_node(mime: str, data: str, encoding: str | None) -> dict:
    """A text leaf, sent in `encoding` (None: as it is, with no `Content-Transfer-Encoding`)."""
    headers = [_header("Content-Type", f'{mime}; charset="UTF-8"')]
    if encoding:
        headers.append(_header("Content-Transfer-Encoding", encoding))
    return {"mimeType": mime, "filename": "", "headers": headers, "data": data, "cte": encoding}


# Gmail's web composer quoted-printables an ASCII text/html part once a line is longer than this. Its
# own choice, not an API rule: `messages.send` stores and serves whatever the sender wrote. Measured
# on 2026-10-02: lines of 80, 81 and 175 characters went with no `Content-Transfer-Encoding` and
# lines of 325 and 425 went quoted-printable, so the limit lies somewhere in 175..324; that range is
# all that was measured, and 250 is a pick inside it.
_HTML_QP_LINE = 250


def _mime_tree(row, html: str, attachments: list) -> list[dict]:
    """The payload's parts, which `full` serves as JSON and `raw` as MIME, so the two describe one
    message. As measured on 2026-09-30: with no attachment the payload is `multipart/alternative`
    over the text and HTML parts; with one it is `multipart/mixed` over a `multipart/alternative`
    part holding those two, then one part per attachment."""
    # The part headers are what Gmail's web composer writes, which the API passes through. Measured
    # on web-composed messages on 2026-09-30, 2026-10-01 and 2026-10-02: text/plain is base64 when
    # its text is non-ASCII and carries no `Content-Transfer-Encoding` otherwise (the composer wraps
    # ASCII text at 74 characters, so no sample had a long ASCII text/plain line); text/html is
    # quoted-printable when it is non-ASCII or has a line longer than `_HTML_QP_LINE`, and carries
    # no `Content-Transfer-Encoding` otherwise.
    html_qp = not html.isascii() or any(len(line) > _HTML_QP_LINE for line in html.splitlines())
    texts = [
        _text_node("text/plain", row["content"], None if row["content"].isascii() else "base64"),
        _text_node("text/html", html, "quoted-printable" if html_qp else None),
    ]
    if not attachments:
        return texts
    alt_boundary = f"a_{row['id'][:12]}"
    nodes = [
        {
            "mimeType": "multipart/alternative",
            "filename": "",
            "headers": [
                _header("Content-Type", f'multipart/alternative; boundary="{alt_boundary}"')
            ],
            "boundary": alt_boundary,
            "parts": texts,
        }
    ]
    for i, att in enumerate(attachments):
        filename = att.get("filename", "attachment.bin")
        mime = att.get("mime", "application/octet-stream")
        # A text attachment names its charset: US-ASCII for ASCII content and UTF-8 otherwise, as an
        # ASCII one (2026-10-01) and a Korean one (2026-09-30) were served. A binary type has none
        # to name.
        ascii_att = _att_content(row["id"], i, att).isascii()
        charset = (
            f'; charset="{"US-ASCII" if ascii_att else "UTF-8"}"'
            if mime.startswith("text/")
            else ""
        )
        # `f_` and nine lowercase alphanumerics, the one attachment measured on 2026-09-30
        x_id = f"f_{synth.gmail_id(row['id'], salt=f'att{i}')[:9]}"
        nodes.append(
            {
                "mimeType": mime,
                "filename": filename,
                "headers": [
                    _header("Content-Type", f'{mime}{charset}; name="{filename}"'),
                    _header("Content-Disposition", f'attachment; filename="{filename}"'),
                    _header("Content-Transfer-Encoding", "base64"),
                    _header("X-Attachment-Id", x_id),
                    _header("Content-ID", f"<{x_id}>"),
                ],
                "attachment": (i, att),
            }
        )
    return nodes


def _json_part(node: dict, part_id: str, message_id: str) -> dict:
    """One node of `_mime_tree` as a `full` payload part."""
    part = {
        "partId": part_id,
        "mimeType": node["mimeType"],
        "filename": node["filename"],
        "headers": node["headers"],
    }
    if "parts" in node:
        part["body"] = {"size": 0}
        part["parts"] = [
            _json_part(child, f"{part_id}.{j}", message_id) for j, child in enumerate(node["parts"])
        ]
    elif "attachment" in node:
        i, att = node["attachment"]
        # size = the exact byte length attachments.get serves (see _att_content), so a client can
        # stat the attachment from this metadata without a second call — real Gmail's contract.
        part["body"] = {
            "attachmentId": _att_id(message_id, i),
            "size": _byte_len(_att_content(message_id, i, att)),
        }
    else:
        part["body"] = {"size": _byte_len(node["data"]), "data": _b64url(node["data"])}
    return part


def _mime_part(node: dict, message_id: str) -> str:
    """One node of `_mime_tree` as a MIME entity, encoded as its own headers declare."""
    head = _raw_header_block(node["headers"])
    if "parts" in node:
        body = _mime_multipart(node["parts"], node["boundary"], message_id)
    elif "attachment" in node:
        i, att = node["attachment"]
        # same bytes attachments.get serves, so raw MIME and the attachment endpoint agree
        body = base64.b64encode(_att_content(message_id, i, att).encode("utf-8")).decode("ascii")
    elif node["cte"] == "quoted-printable":
        body = quopri.encodestring(node["data"].encode("utf-8")).decode("ascii")
    elif node["cte"] == "base64":
        body = base64.encodebytes(node["data"].encode("utf-8")).decode("ascii")
    else:
        body = node["data"]
    return f"{head}\r\n\r\n{body}"


def _mime_multipart(nodes: list[dict], boundary: str, message_id: str) -> str:
    parts = "".join(f"--{boundary}\r\n{_mime_part(n, message_id)}\r\n" for n in nodes)
    return parts + f"--{boundary}--"


def _snippet(row) -> str:
    """The message's first 200 characters, with `<` and `>` as `&lt;` and `&gt;`: real's snippet of
    a quoted `Name <address>` line reads `Name &lt;address&gt;`, measured on 2026-09-30. Only those
    two were in the text measured, so `&` and quotes are sent as they are."""
    return row["content"][:200].replace("<", "&lt;").replace(">", "&gt;")


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


def _gmail_message(
    row, fmt: str, caller_email: str | None = None, metadata_headers: list[str] | None = None
) -> dict:
    """One message in the API's shape.

    `caller_email` decides Bcc. Real Gmail keeps the Bcc header only on the sender's own copy — a
    recipient's is stripped in transit — so a reader who is not the author must not learn who was
    blind-copied. An admin/service caller has no email and is not the sender either.

    `metadata_headers` is every `metadataHeaders` value sent, and narrows a `metadata` payload to
    the headers it names.
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
        "snippet": _snippet(row),
        "historyId": "1",
        "internalDate": str(ts * 1000),
    }
    html = row["body_html"] or f"<html><body><p>{row['content']}</p></body></html>"
    nodes = _mime_tree(row, html, attachments)
    mime_body = _mime_multipart(nodes, boundary, row["id"])
    raw = _raw_header_block(headers) + "\r\n\r\n" + mime_body
    msg["sizeEstimate"] = _byte_len(raw)
    if fmt == "minimal":
        return msg
    if fmt == "metadata":
        # `mimeType` and `headers` alone: real sends no `partId`, `filename` or `body` on a
        # metadata payload, measured on 2026-09-30.
        msg["payload"] = {"mimeType": top_mime}
        if metadata_headers:
            # Measured on 2026-10-03 and 2026-10-05, on `messages.get` and on each message of a
            # `threads.get`: each value names one header, matched without regard to case and
            # served under the message's own spelling and order. A comma or a space is part of the
            # name, and a value no header has (or an empty one) matches nothing; when nothing is
            # matched, `headers` is left out of the payload.
            named = {n.lower() for n in metadata_headers}
            headers = [h for h in headers if h["name"].lower() in named]
        if headers:
            msg["payload"]["headers"] = headers
        return msg
    if fmt == "raw":
        # RFC 2822 message, base64url — a genuine boundary-delimited MIME body matching the
        # declared multipart Content-Type above. It has to be real MIME: a plain-text body under a
        # `multipart/...` header with no boundary makes Python's `email` parser raise
        # StartBoundaryNotFoundDefect/MultipartInvariantViolationDefect, and readers built on it
        # (llama-index's GmailReader) choke because `get_payload()` degrades to a bare
        # string instead of a list of sub-messages). Built from the same parts `full` serves.
        msg["raw"] = _b64url(raw)
        return msg

    msg["payload"] = {
        "partId": "",
        "mimeType": top_mime,
        "filename": "",
        "headers": headers,
        "body": {"size": 0},
        "parts": [_json_part(n, str(i), row["id"]) for i, n in enumerate(nodes)],
    }
    return msg


# ================================ Drive =========================================

# --- `q` ---------------------------------------------------------------------------------------
# Drive's query language, parsed as the reference's grammar and evaluated term by term, so a clause
# is either evaluated or refused — never dropped from the filter while the listing answers 200.
# mirage sends two of its shapes: its folder listing resolves a file with `name='…'` and bounds a
# sync with `modifiedTime >= '…'` and `modifiedTime < '…'`.
#
# The grammar is the reference's (developers.google.com/workspace/drive/api/guides/ref-search-terms):
# a term is `<field> <operator> <value>` or `'<value>' in <collection>`, terms join with `and` and
# `or`, `not` negates, and a string value is single-quoted with an apostrophe escaped as `\'` —
# "Escape single quotes in queries with \'". Parentheses group. Whether `and` binds before `or` the
# reference does not say; Backlot reads it as SQL does, `and` first.
#
# A term Backlot cannot evaluate is a 400 on `q`, never a silence. Two kinds: a term the listing
# does not take (`bogusField = 'x'`), which real Drive refuses too with the bare `Invalid Value` of
# the parameter envelope, and a term `_FILES_Q.unmodelled` names, which `files.list` documents and
# Backlot holds no fact for, refused with a message that says so.

_TIME_OPS = frozenset({"<=", "<", "=", "!=", ">", ">="})
_TIME_FIELDS = frozenset({"modifiedTime", "createdTime", "viewedByMeTime"})


class _DriveQTerms(NamedTuple):
    """The terms one listing's `q` takes: each field with the operators it takes, the fields whose
    value is `true` or `false` (and which alone mean `= true`), those of them taken only as `true`
    and never under a `not`, the fields `'x' in <field>` takes, and the documented fields refused
    with a sentence saying Backlot does not evaluate them."""

    operators: dict[str, frozenset[str]]
    boolean: frozenset[str]
    collections: frozenset[str]
    unmodelled: frozenset[str]
    only_true: frozenset[str] = frozenset()


_FILES_Q = _DriveQTerms(
    operators={
        "name": frozenset({"contains", "=", "!="}),
        "fullText": frozenset({"contains"}),
        "mimeType": frozenset({"contains", "=", "!="}),
        "modifiedTime": _TIME_OPS,
        "createdTime": _TIME_OPS,
        "viewedByMeTime": _TIME_OPS,
        "trashed": frozenset({"=", "!=", "<", "<=", ">", ">="}),
        "starred": frozenset({"=", "!="}),
        "sharedWithMe": frozenset({"="}),
    },
    boolean=frozenset({"trashed", "starred", "sharedWithMe"}),
    collections=frozenset({"parents", "owners"}),
    unmodelled=frozenset({"writers", "readers", "properties", "appProperties", "visibility"}),
    # Real answered `sharedWithMe`, `= true`, `= TRUE` and `(sharedWithMe)`, and refused with the
    # 400 at `q` `= false`, `!= true`, `not sharedWithMe` and
    # `not (name contains 'a' and sharedWithMe)`, measured 2026-10-20.
    only_true=frozenset({"sharedWithMe"}),
)
#: What `trashed` with `<`, `<=`, `>` or `>=` lists: the files `trashed = <value>` lists, or none
#: (None). No order of `false` and `true` gives all eight. Measured on Drive v3 on 2026-10-21 over
#: a Workspace member's files with two trashed ones among them; `sharedWithMe` with any of the four
#: is the 400.
_DRIVE_Q_TRASHED_ORDERED = {
    ("<", "true"): None,
    ("<", "false"): None,
    ("<=", "true"): "true",
    ("<=", "false"): None,
    (">", "true"): None,
    (">", "false"): "false",
    (">=", "true"): "true",
    (">=", "false"): "false",
}
# What `drives.list` takes from a caller who is not the domain's administrator, measured 2026-10-20
# as a Google Workspace member of two shared drives: `name` with `<`, `<=`, `>` and `>=` too, which
# the reference does not list for it. The reference's other four terms for it, `memberCount`,
# `organizerCount`, `orgUnitId` and `createdTime`, were each the 400 at `q` with a value of their
# own type. On 2026-10-22 `drives.list` served `()`, `not ()`, a trailing `or` and a value in double
# quotes, and refused `( )`, a trailing `and`, `not()` and `()()` with the 400 at `q`.
_DRIVES_Q = _DriveQTerms(
    operators={
        "name": frozenset({"contains", "=", "!=", "<", "<=", ">", ">="}),
        "hidden": frozenset({"=", "!="}),
    },
    boolean=frozenset({"hidden"}),
    collections=frozenset(),
    unmodelled=frozenset(),
)
# `{` and `}` are tokens so that `properties has { key='x' and value='y' }`, the reference's own
# spelling, reaches `term()` with the field read — the refusal then names `properties` instead of
# the bare `Invalid Value` an unlexable character gets. `()` with nothing between is a token of its
# own, the empty group real lists every file for, where `( )` is the 400 (2026-10-21); a value in
# double quotes is read as the same value in single quotes (2026-10-20). Tokens stand apart by a
# space, a tab, a line feed, a form feed or a carriage return: real served each of those five
# between two tokens and before the first, and refused U+000B, U+001C, U+0085, U+00A0, U+2003 and
# U+3000 in either place with the 400 at `q`, measured 2026-10-21.
_DRIVE_Q_SPACE = " \t\n\f\r"
_DRIVE_Q_TOKEN = re.compile(
    r"[ \t\n\f\r]*(?:(?P<empty>\(\))|(?P<paren>[()])|(?P<brace>[{}])|(?P<op>!=|<=|>=|=|<|>)"
    r"|(?P<str>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_]*))"
)
# Nesting past this is refused here and read by real: 2,000 parentheses around a term, and 400
# groups each inside the last one's `and`, were served on 2026-10-21, where 4,000 and 800 overran
# the URL. The parser is recursive, so an unbounded query answered 500 where this section's contract
# is a 400: 320 parentheses exhausted the interpreter's frame limit.
_DRIVE_Q_MAX_DEPTH = 128


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


class _QAll(NamedTuple):
    """The empty group `()`, and the missing operand of a trailing `or`: every file."""


def _drive_q_refused(message: str | None = None) -> gerr.GoogleError:
    return gerr.invalid_value("q", message)


def _drive_q_tokens(q: str) -> list[tuple[str, str]]:
    """``(kind, text)`` pairs; a quoted value arrives unescaped."""
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(q):
        m = _DRIVE_Q_TOKEN.match(q, pos)
        if m is None:
            if q[pos:].strip(_DRIVE_Q_SPACE):
                raise _drive_q_refused()
            break
        pos = m.end()
        kind = m.lastgroup or ""
        text = m.group(kind)
        # Real refused `not(trashed)`, `NOT(trashed)` and `not(hidden)`, a `not` its group's
        # parenthesis touches, and served `not (trashed)`, `not\t(trashed)` and
        # `trashed and(starred)`, measured 2026-10-21.
        if kind == "word" and text.lower() == "not" and q.startswith("(", pos):
            raise _drive_q_refused()
        if kind == "str":
            text = re.sub(r"\\(.)", r"\1", text[1:-1])  # strip surrounding quotes
        out.append((kind, text))
    return out


def _drive_q_parse(q: str, terms: _DriveQTerms = _FILES_Q):
    """The parsed query, or ``None`` for an empty one; a query of the separators `_DRIVE_Q_TOKEN`
    reads alone is the 400, as real answered a space, a tab and a newline on `files.list` and a
    space and a tab on `drives.list`. A clause ``terms`` does not take is a 400. A query that names
    no ``trashed`` term matches a trashed file, as `drive_files_list` lists the caller's trash with
    no ``q``."""
    tokens = _drive_q_tokens(q)
    if not tokens:
        if q:
            raise _drive_q_refused()
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
            # at the end or before a `)`, every file, where a trailing `and` is the 400 (2026-10-21)
            ended = peek() in ((None, None), ("paren", ")"))
            parts.append(_QAll() if ended else conjunction())
        return parts[0] if len(parts) == 1 else _QOr(tuple(parts))

    def conjunction():
        parts = [unary()]
        while is_word("and"):
            take()
            parts.append(unary())
        return parts[0] if len(parts) == 1 else _QAnd(tuple(parts))

    depth = 0

    def unary():
        nonlocal depth
        depth += 1
        if depth > _DRIVE_Q_MAX_DEPTH:
            raise _drive_q_refused()
        try:
            if is_word("not"):
                take()
                # Real refused `not not hidden` and `not not name contains 'a'` on `drives.list`,
                # and `not not starred` on `files.list`, with the 400 at `q`, and served
                # `not (not hidden)`, measured 2026-10-20.
                if is_word("not"):
                    raise _drive_q_refused()
                inner = unary()
                if any(t.field in terms.only_true for t in _drive_q_terms(inner)):
                    raise _drive_q_refused()
                return _QNot(inner)
            if peek() == ("empty", "()"):
                take()
                return _QAll()
            if peek() == ("paren", "("):
                take()
                node = disjunction()
                if take() != ("paren", ")"):
                    raise _drive_q_refused()
                return node
            return term()
        finally:
            depth -= 1

    def unmodelled(field: str) -> gerr.GoogleError:
        evaluated = ", ".join(sorted(terms.operators))
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
            if field in terms.unmodelled:
                raise unmodelled(field)
            if field not in terms.collections:
                raise _drive_q_refused()
            return _QTerm(field, "in", text or "")
        if kind != "word" or text is None:
            raise _drive_q_refused()
        field = text
        if field in terms.unmodelled:
            raise unmodelled(field)
        if field not in terms.operators:
            raise _drive_q_refused()
        okind, op = peek()
        if okind == "op":
            take()
        elif okind == "word" and (op or "").lower() in ("contains", "has", "in"):
            take()
            op = (op or "").lower()
        elif field in terms.boolean:
            # The bare `sharedWithMe`, `trashed`, `starred` or `hidden` Drive also accepts,
            # meaning true.
            return _QTerm(field, "=", "true")
        else:
            raise _drive_q_refused()
        if op not in terms.operators[field]:
            raise _drive_q_refused()
        vkind, value = take()
        if field in terms.boolean:
            value = (value or "").lower()
            if vkind != "word" or value not in ("true", "false"):
                raise _drive_q_refused()
            if field in terms.only_true and value != "true":
                raise _drive_q_refused()
            return _QTerm(field, op or "", value)
        if vkind != "str":
            raise _drive_q_refused()
        if field in _TIME_FIELDS and _drive_q_time(value or "") is None:
            # The reference wants RFC 3339 here. A value that is not one would otherwise compare
            # as a string against the file's timestamp and answer something for every file; real
            # answered `modifiedTime < 'yesterday'`, `createdTime < 'yesterday'` and
            # `viewedByMeTime > 'garbage'` with this 400, measured 2026-10-20.
            raise _drive_q_refused()
        return _QTerm(field, op or "", value or "")

    node = disjunction()
    if pos != len(tokens):
        raise _drive_q_refused()
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
    if isinstance(node, _QAll):
        return True
    field, op, value = node
    if field == "trashed":
        if op in ("=", "!="):
            return ((value == "true") == f["trashed"]) == (op == "=")
        same = _DRIVE_Q_TRASHED_ORDERED[op, value]
        return same is not None and (same == "true") == f["trashed"]
    if field == "starred":
        # Nothing here is starred, as every file's `starred` says.
        return (value == "false") == (op == "=")
    if field == "viewedByMeTime":
        # No file has one: real matched a file its caller never viewed under `!=` and under no
        # other comparison, measured 2026-10-20, and `not` around any of them matches the rest.
        return op == "!="
    if field == "sharedWithMe":
        # "Shared with me" = visible to the caller and not owned by them. Items shared with you
        # carry no My Drive parent on real Drive, so this clause is the only way to enumerate
        # that section.
        shared = not _drive_owned_by(f["owner_email"], me)
        return ((value == "true") == shared) == (op == "=")
    if field == "name":
        # Two comparisons, both measured 2026-09-14 against real Drive and each the one
        # `store.list_drive_by_name` builds its candidate set with, so SQL and this agree. `=` and
        # `!=` fold case for ASCII letters only (`'ÉLAN VITAL'` is `Élan Vital`, `'élan vital'` and
        # `'strasse plan'` are not); `contains` folds as `store.drive_name_fold` says.
        if op == "contains":
            return store.drive_name_fold(value) in store.drive_name_fold(f["name"])
        have, want = _ascii_lower(f["name"]), _ascii_lower(value)
        return (have == want) == (op == "=")
    if field == "mimeType":
        return value in f["mime"] if op == "contains" else (f["mime"] == value) == (op == "=")
    if field == "fullText":
        return f["id"] in fulltext.get(value, ())
    if field == "modifiedTime":
        return _drive_q_compare(op, f["modified"], value)
    if field == "createdTime":
        return _drive_q_compare(op, f["created"], value)
    if field == "parents":
        # `root` is the caller's My Drive root by its alias, and so is its id: a file created at
        # the root carries the id (`synth.drive_root_id`), a folder here the alias.
        roots = _drive_roots(me)
        if value in roots:
            return any(p in roots for p in f["parents"])
        return value in f["parents"]
    if field == "owners":
        # `me` is the reference's alias for the caller (`'me' in owners`), resolved through the
        # identity `sharedWithMe` reads (`_drive_owned_by`).
        who = value.strip().lower()
        if who == "me":
            return any(_drive_owned_by(owner, me) for owner in f["owners"])
        return who in f["owners"]
    raise AssertionError(f"unevaluated q term {field!r}")  # every field the parser admits is above


_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _ascii_lower(text: str) -> str:
    """Lower-case the ASCII letters and nothing else — SQLite's LIKE fold, and real Drive's `name =`."""
    return text.translate(_ASCII_LOWER)


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


def _drive_listed_trash(ids: set[str] | None, me: str | None) -> bool | str:
    """The trashed files `files.list` lists to this caller, as the ``trash`` the store's Drive
    listings take: those it owns. Real listed a trashed file to its owner, with no `q` and under
    each `q` it was sent that named no `trashed` term, on two accounts' Drives, the folder scope on
    one, and to no one else: a Google Workspace member's `files.get` answered two files another
    user owns and trashed, and `files.list` listed neither with no `q` or under `trashed = true`,
    `trashed`, the file's name, its folder or `not 'me' in owners`, measured 2026-10-20. The admin
    token, ``ids`` ``None``, sees every file and lists them all."""
    return True if ids is None else (me or False)


# The `folder` a file created at a user's My Drive root is stored under: no folder at all, which
# no corpus file has (every one lives in a folder), so the two never meet.
_DRIVE_ROOT_FOLDER = ""


def _drive_roots(me: str | None) -> set[str]:
    """What names the caller's My Drive root in a `parents` term: the alias `root`, and the id."""
    return {"root", synth.drive_root_id(me)} if me else {"root"}


def _drive_owned_by(owner_email: str | None, me: str | None) -> bool:
    """Whether the caller owns this file. The admin/service token is not a Drive user (its
    ``caller.email`` is None): it owns what the service account it stands for created
    (`_ADMIN_OWNER`), and every other file reads as shared to it. ASCII letters fold, as SQLite's
    ``lower()`` folds them in the trash rule `_drive_listed_trash` hands the store, so a listing
    and ``ownedByMe`` agree on whose a file is."""
    who = me or _ADMIN_OWNER.get()
    return bool(who) and _ascii_lower(owner_email or "") == _ascii_lower(who)


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


def _modified_by_me(owner_email: str | None, me: str | None, modified: int) -> dict:
    """``modifiedByMe``, and ``modifiedByMeTime`` when it is true, as a ``**``-mergeable fragment.
    A row's author is its owner and the one user ``lastModifyingUser`` names, so the caller
    modified a file exactly when it owns it, last at ``modifiedTime``. Real sends ``modifiedByMe``
    on every file and the time only when it is true, and on 21,074 of the 21,085 files two
    accounts owned and had modified the time was ``modifiedTime``, measured 2026-10-20."""
    if not _drive_owned_by(owner_email, me):
        return {"modifiedByMe": False}
    return {"modifiedByMe": True, "modifiedByMeTime": synth.rfc3339(modified)}


def _drive_created(row) -> int:
    """A file's creation second — its own, else one seeded from its id. `is not None`, because
    1970-01-01T00:00:00Z stores as 0 and a file that HAS a second must serve it rather than a
    seeded one (which would also make the `q` time filters disagree with the served body)."""
    return row["created_ts"] if row["created_ts"] is not None else synth.epoch(row["id"])


def _drive_modified(row) -> int:
    """A file's last-modified second, an hour after creation when the corpus states none
    (``store.DRIVE_MODIFIED`` is the same in SQL)."""
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
        "quotaBytesUsed": "0",  # as real's folders answered it, 2026-10-25
        **_shared_with_me_time(None, me, ts),
        "trashed": False,
        "explicitlyTrashed": False,
        "starred": False,
        "shared": True,
        "ownedByMe": False,
        "modifiedByMe": False,
        "viewedByMe": False,
        "version": "1",
        "spaces": ["drive"],
        "webViewLink": f"https://drive.google.com/drive/folders/{fid}",
        "iconLink": "https://drive.google.com/icons/folder.png",
        "capabilities": {
            "canDownload": True,
            "canListChildren": True,
            "canComment": False,
            "canEdit": False,
            "canCopy": False,
            "canShare": False,
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
    appProperties capabilities clientEncryptionDetails contentHints contentRestrictions
    copyRequiresWriterPermission createdTime description downloadRestrictions driveId
    explicitlyTrashed exportLinks fileExtension folderColorRgb
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


# `permissions.list`'s and `drives.list`'s entries, each field the reference lists for them.
_DRIVE_PERMISSION_FIELDS = frozenset(
    """
    allowFileDiscovery deleted displayName domain emailAddress expirationTime id
    inheritedPermissionsDisabled kind pendingOwner permissionDetails photoLink role
    teamDrivePermissionDetails type view
""".split()
)
_DRIVE_DRIVE_FIELDS = frozenset(
    """
    backgroundImageFile backgroundImageLink capabilities colorRgb createdTime hidden id kind name
    orgUnitId restrictions themeId
""".split()
)
# What each object field of a Drive resource holds, as Drive v3's discovery document declares it:
# names that hold none in turn as a set, names some of which do as a dict, and `None` where any name
# is taken (`properties` and `appProperties`). A field missing from its table holds no name, so a
# name below it is refused, `id/zzz` and `exportLinks/zzz` as `owners/zzz` are. On 2026-10-25 real
# served each name these tables declare below a file, a permission, a drive and `about`, and
# answered `zzz` below each of their 84 objects, and below `exportLinks`, `exportFormats`,
# `importFormats` and `maxImportSizes`, with the 400 naming `zzz`; it served `properties/zzz` and
# `appProperties/zzz`.
_DRIVE_USER_KEYS = frozenset("displayName emailAddress kind me permissionId photoLink".split())
_DRIVE_PERMISSION_SUBFIELDS = {
    "permissionDetails": frozenset("inherited inheritedFrom permissionType role".split()),
    "teamDrivePermissionDetails": frozenset(
        "inherited inheritedFrom role teamDrivePermissionType".split()
    ),
}
_DRIVE_DOWNLOAD_RESTRICTION = frozenset({"restrictedForReaders", "restrictedForWriters"})
_DRIVE_PERMISSION_ENTRY = {
    **dict.fromkeys(_DRIVE_PERMISSION_FIELDS, frozenset()),
    **_DRIVE_PERMISSION_SUBFIELDS,
}
_DRIVE_FILE_SUBFIELDS = {
    "appProperties": None,
    "capabilities": frozenset(
        """
        canAcceptOwnership canAccessViaGenAi canAddChildren canAddFolderFromAnotherDrive
        canAddMyDriveParent canChangeCopyRequiresWriterPermission canChangeItemDownloadRestriction
        canChangeSecurityUpdateEnabled canChangeViewersCanCopyContent canComment canCopy canDelete
        canDeleteChildren canDisableInheritedPermissions canDownload canEdit
        canEnableInheritedPermissions canListChildren canModifyContent canModifyContentRestriction
        canModifyEditorContentRestriction canModifyLabels canModifyOwnerContentRestriction
        canMoveChildrenOutOfDrive canMoveChildrenOutOfTeamDrive canMoveChildrenWithinDrive
        canMoveChildrenWithinTeamDrive canMoveItemIntoTeamDrive canMoveItemOutOfDrive
        canMoveItemOutOfTeamDrive canMoveItemWithinDrive canMoveItemWithinTeamDrive
        canMoveTeamDriveItem canReadDrive canReadLabels canReadRevisions canReadTeamDrive
        canRemoveChildren canRemoveContentRestriction canRemoveMyDriveParent canRename canShare
        canStartApproval canTrash canTrashChildren canUntrash
    """.split()
    ),
    "clientEncryptionDetails": {
        "decryptionMetadata": frozenset(
            """
            aes256GcmChunkSize encryptionResourceKeyHash jwt kaclsId kaclsName keyFormat wrappedKey
        """.split()
        ),
        "encryptionState": frozenset(),
    },
    "contentHints": {"indexableText": frozenset(), "thumbnail": frozenset({"image", "mimeType"})},
    "contentRestrictions": {
        **dict.fromkeys(
            """
            ownerRestricted readOnly reason restrictionTime systemRestricted type
        """.split(),
            frozenset(),
        ),
        "restrictingUser": _DRIVE_USER_KEYS,
    },
    "downloadRestrictions": dict.fromkeys(
        ("effectiveDownloadRestrictionWithContext", "itemDownloadRestriction"),
        _DRIVE_DOWNLOAD_RESTRICTION,
    ),
    "imageMediaMetadata": {
        **dict.fromkeys(
            """
            aperture cameraMake cameraModel colorSpace exposureBias exposureMode exposureTime
            flashUsed focalLength height isoSpeed lens maxApertureValue meteringMode rotation sensor
            subjectDistance time whiteBalance width
        """.split(),
            frozenset(),
        ),
        "location": frozenset({"altitude", "latitude", "longitude"}),
    },
    "labelInfo": {"labels": frozenset({"fields", "id", "kind", "revisionId"})},
    "lastModifyingUser": _DRIVE_USER_KEYS,
    "linkShareMetadata": frozenset({"securityUpdateEligible", "securityUpdateEnabled"}),
    "owners": _DRIVE_USER_KEYS,
    "permissions": _DRIVE_PERMISSION_ENTRY,
    "properties": None,
    "sharingUser": _DRIVE_USER_KEYS,
    "shortcutDetails": frozenset({"targetId", "targetMimeType", "targetResourceKey"}),
    "trashingUser": _DRIVE_USER_KEYS,
    "videoMediaMetadata": frozenset({"durationMillis", "height", "width"}),
}
_DRIVE_DRIVE_SUBFIELDS = {
    "backgroundImageFile": frozenset({"id", "width", "xCoordinate", "yCoordinate"}),
    "capabilities": frozenset(
        """
        canAddChildren canChangeCopyRequiresWriterPermissionRestriction
        canChangeDomainUsersOnlyRestriction canChangeDownloadRestriction canChangeDriveBackground
        canChangeDriveMembersOnlyRestriction
        canChangeSharingFoldersRequiresOrganizerPermissionRestriction canComment canCopy
        canDeleteChildren canDeleteDrive canDownload canEdit canListChildren canManageMembers
        canReadRevisions canRename canRenameDrive canResetDriveRestrictions canShare
        canTrashChildren
    """.split()
    ),
    "restrictions": {
        **dict.fromkeys(
            """
            adminManagedRestrictions copyRequiresWriterPermission domainUsersOnly driveMembersOnly
            sharingFoldersRequiresOrganizerPermission
        """.split(),
            frozenset(),
        ),
        "downloadRestriction": _DRIVE_DOWNLOAD_RESTRICTION,
    },
}
# The fields a resource's default answer holds, which an empty group selects: real answered
# `id()`, `name()`, `kind()` and `mimeType()` on a file, `id()`, `type()`, `kind()` and `role()` on
# a permission, `id()`, `name()` and `kind()` on a drive, and a listing's `kind()` and
# `nextPageToken()` with those keys, and `createdTime()`, `owners()`, `trashed()`,
# `permissions(emailAddress())` and `drives(hidden())` with nothing, measured 2026-10-20.
_DRIVE_FILE_DEFAULT = frozenset({"kind", "id", "name", "mimeType"})
_DRIVE_PERMISSION_DEFAULT = frozenset({"id", "type", "kind", "role"})
_DRIVE_DRIVE_DEFAULT = frozenset({"id", "name", "kind"})
_DRIVE_LIST_DEFAULT = frozenset({"kind", "nextPageToken", "incompleteSearch"})
# Documented fields real gave each of a Google Workspace member's 228 files, measured 2026-10-20,
# that no file here carries: a mask that names one is refused, saying so, rather than answered
# without it.
_DRIVE_FILE_UNSERVED = frozenset(
    """
    clientEncryptionDetails copyRequiresWriterPermission downloadRestrictions hasThumbnail
    inheritedPermissionsDisabled isAppAuthorized linkShareMetadata thumbnailVersion
    viewersCanCopyContent writersCanShare
""".split()
)
# What a name in a `fields` mask is made of, as real read each character between `id,b` and `x` on
# 2026-10-20: the ASCII below, and past ASCII the name characters of XML 1.0's fourth edition
# (Appendix B), whose table expat's is. None of the 1,345 code points past ASCII it was sent, every
# one from U+0080 to U+02FF and the rest chosen by category, was read otherwise. Every other
# character it was sent, white space other than a space or a tab among them, made the whole mask the
# 400's subject, and so did a name ending in `-` or holding `:` other than once between two other
# characters (`x-`, `:x`, `x:`, `x::y`, `x:y:z`), where `-x`, `x--y` and `x:y` were names.
_MASK_NAME = frozenset("$+-.:_" + string.ascii_letters + string.digits)
# The last name of a path an empty group, `owners()`, ends.
_MASK_EMPTY = "()"
# How many names of a path `_mask_paths` keeps: a listing, an entry's field, the two levels the
# deepest object below a field holds (`permissions/permissionDetails/role`) and one name past them,
# which `_check_mask` refuses, so a mask nested a thousand deep costs its length rather than the
# square of its depth.
_MASK_DEPTH = 5


def _mask_paths(mask: str) -> list[tuple[str, ...]]:
    """The paths a `fields` mask selects, in its order: ``files(id,owners(kind)),kind`` is
    ``files/id``, ``files/owners/kind`` and ``kind``, and ``files()`` is ``files`` then
    ``_MASK_EMPTY``, each path to its first ``_MASK_DEPTH`` names. A space or a tab may stand beside
    a name, a ``/`` or a parenthesis. A mask that is not one — an empty entry, a name `_mask_name`
    does not take, a group never opened or never closed — is the 400 naming the whole mask, as real
    answered ``id,,name``, ``files(id,)``, ``id,`` and ``owners(``; that refusal comes before any
    name's.

    Read without recursion: real answered a group nested a thousand deep with the name it did not
    take, not a refusal of the mask."""
    paths: list[tuple[str, ...]] = []
    prefixes: list[tuple[str, ...]] = [()]
    i = 0

    def refused() -> gerr.GoogleError:
        return gerr.invalid_parameter("fields", f"Invalid field selection {mask}")

    def blank() -> None:
        nonlocal i
        while i < len(mask) and mask[i] in " \t":
            i += 1

    def name() -> str:
        nonlocal i
        blank()
        start = i
        if mask.startswith("*", i):
            i += 1
        while i < len(mask) and mask[start] != "*" and _mask_name_char(mask[i]):
            i += 1
        end = i
        if not _mask_name(mask[start:end]):
            raise refused()
        blank()
        return mask[start:end]

    while True:
        path = [name()]
        while mask.startswith("/", i):
            i += 1
            path.append(name())
        full = (prefixes[-1] + tuple(path))[:_MASK_DEPTH]
        if mask.startswith("(", i):
            i += 1
            blank()
            if not mask.startswith(")", i):
                prefixes.append(full)
                continue
            i += 1
            paths.append((*full, _MASK_EMPTY)[:_MASK_DEPTH])
        else:
            paths.append(full)
        blank()
        while mask.startswith(")", i) and len(prefixes) > 1:
            i += 1
            prefixes.pop()
            blank()
        if i == len(mask) and len(prefixes) == 1:
            return paths
        if not mask.startswith(",", i):
            raise refused()
        i += 1


def _mask_name(text: str) -> bool:
    """Whether ``text``, read of the characters `_mask_name_char` takes, is a name, as the comment
    above ``_MASK_NAME`` says."""
    if text == "*":
        return True
    colons = text.count(":")
    if not text or text.endswith("-") or colons > 1:
        return False
    return not colons or (text[0] != ":" and text[-1] != ":")


@functools.lru_cache(maxsize=4096)
def _mask_name_char(ch: str) -> bool:
    if ch.isascii():
        return ch in _MASK_NAME
    parser = xml.parsers.expat.ParserCreate()
    try:
        parser.Parse(f"<a{ch}/>", True)
    except (xml.parsers.expat.ExpatError, ValueError):
        return False
    return True


def _check_mask(
    paths,
    top: frozenset,
    under: str | None = None,
    entries: frozenset = frozenset(),
    unserved: frozenset = frozenset(),
    subfields: dict | None = None,
):
    """Refuse the first name, in the mask's order, the resource does not have, as real names it:
    a top-level name outside ``top``, under the list ``under`` an entry's name outside ``entries``,
    and a name below a field that does not hold it, as ``subfields`` gives what the resource's
    object fields hold; a field it leaves out, and each name of a listing but ``under``, holds none.
    Then refuse the first of the ``unserved`` names of a file, the top level's on `files.get` and
    an entry's on `files.list`, with a sentence saying Backlot does not carry it."""
    subfields = subfields or {}
    served: list[str] = []
    for path in paths:
        names = [n for n in path if n != _MASK_EMPTY]
        checks = [(names[0], top)]
        held, below = frozenset(), names[1:]
        if under is not None and names[0] == under and len(names) > 1:
            checks.append((names[1], entries))
            held, below = subfields.get(names[1], frozenset()), names[2:]
        elif under is None:
            held = subfields.get(names[0], frozenset())
        for name, allowed in checks:
            if name != "*" and name not in allowed:
                raise gerr.invalid_parameter("fields", f"Invalid field selection {name}")
        for name in below:
            if held is None or name == "*":
                break
            if name not in held:
                raise gerr.invalid_parameter("fields", f"Invalid field selection {name}")
            held = held[name] if isinstance(held, dict) else frozenset()
        served.append(checks[-1][0] if under is None or path[0] == under else "")
    for name in served:
        if name in unserved:
            raise gerr.invalid_parameter(
                "fields",
                f"'{name}' is not served by Backlot: real Drive answers it, and nothing here "
                f"carries it.",
            )


def _mask_keys(paths, defaults: frozenset = frozenset()) -> set[str] | None:
    """The names a mask's paths select at their top level; ``None`` selects every one, as no mask
    and a top-level ``*`` do. A key an empty group names is selected only when it is one of the
    ``defaults``, as the comment above ``_DRIVE_FILE_DEFAULT`` says."""
    if paths is None:
        return None
    keys = {p[0] for p in paths if not (len(p) > 1 and p[1] == _MASK_EMPTY) or p[0] in defaults}
    return None if "*" in keys else keys


def _drive_refuse_a_trailing_comma(request: Request) -> None:
    """Refuse a `fields` mask whose last character but white space (`string.whitespace`) is a comma,
    with the 400 naming the whole mask, on a metadata read. Real answered it after the typed and
    range refusals and ahead of the rest, measured 2026-10-20 beside a bad `pageToken`, `q` and
    `orderBy`, an `orderBy` naming a key twice or `starred` after another, `fullText` beside an
    `orderBy`, `includeItemsFromAllDrives` alone, `acknowledgeAbuse`, `useDomainAdminAccess`, a
    repeated `pageSize` and a file that does not exist. A download read no mask."""
    mask = gerr.first_repeat(request.query_params, "fields")
    if mask is not None and mask.rstrip(string.whitespace).endswith(","):
        raise gerr.invalid_parameter("fields", f"Invalid field selection {mask}")


def _drive_mask(fields: str | None) -> list[tuple[str, ...]] | None:
    """A mask's paths; ``None`` for an absent one, and no paths (``[]``) for one of spaces and tabs
    alone, the blank mask `_drive_project_one` describes."""
    if fields is None:
        return None
    return _mask_paths(fields) if fields.strip(" \t") else []


def _drive_project_listing(
    body: dict,
    fields: str | None,
    under: str,
    entries: frozenset,
    defaults: frozenset,
):
    """``permissions.list`` or ``drives.list`` trimmed to its mask: the top-level keys it names,
    and the entries of the list ``under`` trimmed to theirs by `_drive_project_paths`. The list
    stays when only its entries are emptied. ``None`` for no mask."""
    paths = _drive_mask(fields)
    if paths is None:
        return None
    _check_mask(
        paths,
        frozenset({"kind", "nextPageToken", under}),
        under,
        entries,
        subfields=_DRIVE_PERMISSION_ENTRY if under == "permissions" else _DRIVE_DRIVE_SUBFIELDS,
    )
    tops = _mask_keys(paths, defaults=_DRIVE_LIST_DEFAULT)
    if tops is None:
        return body
    if any(p[:2] == (under, _MASK_EMPTY) for p in paths):
        tops.add(under)
    out = {k: v for k, v in body.items() if k in tops}
    if under in out:
        out[under] = _drive_project_paths(out[under], paths, under, defaults)
    return out


def _drive_project_entries(entries: list[dict], keys: set[str] | None) -> list[dict]:
    """A list's entries trimmed to ``keys``, an entry left with none dropped: real answered
    `files(trashingUser)` and `permissions(teamDrivePermissionDetails)` with an empty list on
    listings none of whose entries had the field, measured 2026-10-20."""
    return [e for e in _drive_project(entries, keys) if e]


def _drive_project(files: list[dict], keys: set[str] | None) -> list[dict]:
    return files if keys is None else [{k: v for k, v in f.items() if k in keys} for f in files]


def _drive_entry_keys(paths, under: str, defaults: frozenset) -> set[str] | None:
    """The keys each entry of a masked list carries: ``None`` for the whole entry object, a set for
    a projection, and an empty set when the wrapper is not named at all. A bare wrapper, an empty
    group on a default key, a `*` at entry level, or a top-level `*` that reaches in each answer
    the whole entry."""
    if paths is None:
        return None
    if any(len(p) == 1 and p[0] == "*" for p in paths):
        return None
    under_paths = [p[1:] for p in paths if p[0] == under]
    if not under_paths:
        return set()
    if any(not p for p in under_paths):
        return None
    keys = {
        p[0] for p in under_paths if not (len(p) > 1 and p[1] == _MASK_EMPTY) or p[0] in defaults
    }
    return None if "*" in keys else keys


def _drive_trim_object(obj, paths, under: str | None, defaults: frozenset):
    """One resource object trimmed to a mask's paths. ``under`` strips a list wrapper's name
    (`files`, `permissions` or `drives`); for a single object it is None. Nested paths trim
    object-valued fields (`owners(kind)`) instead of leaving the whole object. A path naming a key
    the object does not carry omits it, the way real omits an absent key; name validity is
    `_check_mask`'s job."""
    chains = []
    bare = False
    entry_empty = False
    empty_groups: set[str] = set()
    for p in paths:
        raw = list(p)
        if under is not None:
            if not raw or raw[0] != under:
                continue
            raw = raw[1:]
        if not raw:  # a bare `files`/`permissions` names the whole entry object
            bare = True
            continue
        if raw == [_MASK_EMPTY]:  # `files()`/`permissions()` answers an empty entry
            entry_empty = True
            continue
        if len(raw) >= 2 and raw[1] == _MASK_EMPTY and raw[0] not in defaults:
            empty_groups.add(raw[0])
        chains.append([n for n in raw if n != _MASK_EMPTY])
    if entry_empty:
        return []
    if bare:
        return obj
    selected = _mask_keys(chains, defaults)
    if selected is None:
        return obj
    nested: dict[str, set] = {}
    whole: set[str] = set()
    for chain in chains:
        if len(chain) > 1:
            if chain[1] == "*":
                whole.add(chain[0])
            else:
                nested.setdefault(chain[0], set()).add(chain[1])
    # Measured 2026-10-25, in either order: `*` below a field keeps it whole beside a narrower group
    # (`owners`, `capabilities`, `permissionDetails` and `about`'s `user`), an empty group drops it
    # whatever else names it (`owners`, `capabilities`, `lastModifyingUser`, `permissionDetails`),
    # and a narrower group trims it beside its bare name (`owners`, `capabilities`, `user`).
    for field in whole:
        nested.pop(field, None)
    nested.update(dict.fromkeys(empty_groups, set()))
    out = {}
    for key, value in obj.items():
        if key not in selected:
            continue
        if key in nested:
            subkeys = nested[key]
            if not subkeys:
                continue
            if isinstance(value, dict):
                value = {k: v for k, v in value.items() if k in subkeys}
            elif isinstance(value, list):
                value = [
                    {k: v for k, v in item.items() if k in subkeys}
                    if isinstance(item, dict)
                    else item
                    for item in value
                ]
        out[key] = value
    return out


def _drive_project_paths(entries: list[dict], paths, under: str, defaults: frozenset) -> list[dict]:
    """A listing's entries each trimmed to the mask, an entry left with none dropped."""
    return [
        e for e in (_drive_trim_object(entry, paths, under, defaults) for entry in entries) if e
    ]


def _drive_project_one(obj: dict, fields: str | None) -> dict:
    """A single file resource trimmed to a ``files.get`` mask; the mask is validated (400 on a name
    real refuses) before trimming, including nested object fields. Trimming a file read by id as
    a listing's entries are trimmed is what makes one file look the same either way.

    No mask answers the `_DRIVE_FILE_DEFAULT` keys alone, measured 2026-10-25. A mask that is empty
    or of spaces and tabs alone selects nothing, which is not the same as no mask: measured
    2026-09-23, `fields=` and `fields=%20` answer ``{}``, and `fields=%09` did too on 2026-10-20."""
    paths = _drive_mask(fields)
    if paths is None:
        return {k: v for k, v in obj.items() if k in _DRIVE_FILE_DEFAULT}
    _check_mask(
        paths,
        _DRIVE_FILE_FIELDS,
        unserved=_DRIVE_FILE_UNSERVED,
        subfields=_DRIVE_FILE_SUBFIELDS,
    )
    return _drive_trim_object(obj, paths, None, _DRIVE_FILE_DEFAULT)


def _drive_fill_shared(conn, files: list[dict], stored: set[str]) -> None:
    """Resolve ``shared`` for one page of stored files, in one query. Objects not in ``stored`` are
    the synthesized folders, left alone: their sharing comes from the files they hold, not from a
    grant on the folder id.

    ``stored`` is the SET of served file ids that came from a row. An ACL grant names a file by
    that same id, so plain set membership is exact."""
    have = store.gdrive_shared(conn, [f["id"] for f in files if f["id"] in stored])
    for f in files:
        if f["id"] in stored:
            f["shared"] = f["id"] in have


# --- `orderBy` -----------------------------------------------------------------------------


# Real Drive's documented `orderBy` keys -> the sort key each takes from the served file object
# (sorting what the client actually sees, so folders and stored rows order together). `name`
# compares names case-insensitively, code point by code point; `name_natural` is
# `backlot.collation`'s.
_DRIVE_ORDER_KEYS = {
    "createdTime": lambda f: f.get("createdTime") or "",
    "modifiedTime": lambda f: f.get("modifiedTime") or "",
    "name": lambda f: (f.get("name") or "").casefold(),
    "name_natural": lambda f: collation.sort_key(f.get("name") or ""),
    "folder": lambda f: f.get("mimeType") != DRIVE_FOLDER_MIME,  # folders first
    "starred": lambda f: bool(f.get("starred")),
    "quotaBytesUsed": lambda f: int(f.get("quotaBytesUsed") or f.get("size") or 0),
    # The per-caller keys read the served object, so a file the caller never modified or was
    # shared sorts as real sorts a file without the time: ahead of every time, and after every one
    # under `desc`.
    "sharedWithMeTime": lambda f: f.get("sharedWithMeTime") or "",
    "modifiedByMeTime": lambda f: f.get("modifiedByMeTime") or "",
    # Real sorts a file shared with the caller that the caller never viewed after every file
    # without a view time and ahead of every view time, one of 2001 included, measured 2026-10-21;
    # a file reached through a shared folder carries no `sharedWithMeTime` and sorts with the
    # first. Nothing here is viewed, as `viewedByMe` says on every file, so whether the caller was
    # shared the file is all the key reads.
    "viewedByMeTime": lambda f: bool(f.get("sharedWithMeTime")),
    # The later of the caller's last view and last modification; a file with neither after every
    # file with either.
    "recency": lambda f: max(f.get("viewedByMeTime") or "", f.get("modifiedByMeTime") or ""),
}
# Listed newest first whichever direction is asked for: real answered `recency` and `recency asc`
# newest first, as it answers `recency desc`, measured 2026-10-20.
_DRIVE_ORDER_NEWEST_FIRST = frozenset({"recency"})
# What separates an `orderBy` key from its direction, and what may stand around either: the 25
# code points of Unicode's White_Space property, each a 200 between `name` and `desc`, ahead of
# `name` and after it, measured 2026-10-20. `str.split()` also splits on U+001C-U+001F, which real
# refuses as part of the key.
_DRIVE_ORDER_SPACE = re.compile(
    "[\t-\r \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+"
)


def _drive_order_words(order_by: str | None) -> list[list[str]]:
    """``orderBy``'s comma-separated keys, each as its words. A key with no words is no key:
    ``,name``, ``name,`` and ``name,,modifiedTime`` are each served."""
    words = ([w for w in _DRIVE_ORDER_SPACE.split(tok) if w] for tok in (order_by or "").split(","))
    return [w for w in words if w]


def _drive_order_specs(order_by: str | None) -> list[tuple]:
    """Parse ``orderBy`` — comma-separated keys, each optionally followed by ``asc`` or ``desc`` in
    lower case — into ``(key, reverse)`` pairs. Anything else is real's 400 ``Invalid Value``: an
    unknown key, a key or direction in another case, a third word. A key named twice is a 403."""
    specs = []
    seen: set[str] = set()
    for parts in _drive_order_words(order_by):
        key = parts[0]
        if parts[1:] not in ([], ["asc"], ["desc"]):
            raise gerr.invalid_value("orderBy")
        if key not in _DRIVE_ORDER_KEYS:
            raise gerr.invalid_value("orderBy")
        # Real Drive (measured 2026-10-04) 403s a key named twice whatever either direction is, and
        # reads `name_natural` as `name` but `recency` and `modifiedTime` as two keys — so this
        # compares names, not the key functions. It reads left to right and answers the first
        # problem it meets, so the repeat is checked only once the token passes the checks above.
        name = "name" if key == "name_natural" else key
        if name in seen:
            raise gerr.duplicate_sort_keys()
        seen.add(name)
        specs.append((key, parts[1:] == ["desc"]))
    return specs


def _drive_sort(files: list[dict], specs: list[tuple]) -> list[dict]:
    """Apply the keys last-first: Python's sort is stable, so the first key wins. Files the keys
    tie stay newest modification first under either direction, as real lists them: of the tied
    pairs in its `name`, `name_natural`, `folder,name`, `starred` and `quotaBytesUsed` listings of
    two accounts' files on 2026-10-20, and in its `viewedByMeTime`, `sharedWithMeTime`,
    `modifiedByMeTime` and `recency` listings of folders made for them on 2026-10-21, none had the
    older first. The id under that keeps a sorted walk from repeating or skipping a row across
    pages."""
    files.sort(key=lambda f: f.get("id") or "")
    files.sort(key=lambda f: f.get("modifiedTime") or "", reverse=True)
    for key, reverse in reversed(specs):
        files.sort(key=_DRIVE_ORDER_KEYS[key], reverse=reverse or key in _DRIVE_ORDER_NEWEST_FIRST)
    return files


def _drive_newest_first(objects, folders: list[dict], offset: int, limit: int) -> list[dict]:
    """The page at ``offset`` of a listing whose request names no `orderBy` and whose `q` has no
    `fullText` term: newest `modifiedTime` first, ties by id, each synthesized folder among the
    stored files where its own time puts it. Measured on Drive v3: a Workspace member's 170 files
    and a consumer account's 21,702 with no `q` (2026-10-20), and on 2026-10-21 the member's 228
    with `orderBy=` or `q=` empty, under `trashed = false` and under a `modifiedTime` bound, and
    both accounts' under `createdTime` and `viewedByMeTime` bounds, `starred = false`,
    `sharedWithMe = true`, `'me' in owners` and `trashed = true`, in the order
    `orderBy=modifiedTime desc` gives, id for id, and in descending `modifiedTime` under `mimeType`
    and `name contains` terms on both accounts and `'root' in parents` on the member's. Five
    `fullText contains` queries on the two accounts came back with 16 to 144 of their adjacent pairs
    out of time order.

    ``objects(o, n)`` reads stored rows ``o`` to ``o + n`` already in that order. A row on the page
    has at most ``len(folders)`` folders ahead of it, so the page holds no row before row
    ``offset - len(folders)``, and a folder ahead of every row from there sorts before the page."""
    start = max(0, offset - len(folders))
    window = objects(start, offset + limit - start, with_shared=False)
    newest = [("modifiedTime", True)]
    return _drive_sort(window + folders, newest)[offset - start : offset - start + limit]


def _drive_starred_after_another_key(order_by: str | None) -> bool:
    """Whether ``starred`` follows another key in an ``orderBy`` that ``_drive_order_specs`` passed,
    the case `gerr.drive_internal_error` answers: `,starred` is served and `name,,starred` is the
    500."""
    keys = [parts[0] for parts in _drive_order_words(order_by)]
    return "starred" in keys[1:]


def _drive_q_plain_folder(query) -> str | None:
    """Whether the query is just a folder scope, the shape a tree-walking client sends, servable
    straight from SQL: ``"live"`` for ``'<id>' in parents and trashed = false``, whose listing
    leaves the trash out, ``"all"`` for ``'<id>' in parents`` alone, as LlamaIndex's Google Drive
    reader sends it, whose listing holds the caller's own trash too, and ``None`` for any other
    clause."""
    conjuncts = _drive_q_conjuncts(query) if _drive_q_is_conjunction(query) else []
    trashed_false = _QTerm("trashed", "=", "false")
    if not conjuncts or not all(t.field == "parents" or t == trashed_false for t in conjuncts):
        return None
    return "live" if trashed_false in conjuncts else "all"


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
    # `contains` or `=`: `list_drive_by_name` makes the comparison real Drive makes for each, the
    # same one `_drive_q_eval` makes, so the candidate set is the answer for that term and the
    # evaluator only applies the others. Nothing the evaluator accepts is outside it.
    name = next((t for t in conjuncts if t.field == "name" and t.op in ("contains", "=")), None)
    trash = _drive_listed_trash(ids, me)
    if fulltext is not None:  # the index's candidates, in rank order, then the other terms
        candidates = [
            r
            for r in hits[fulltext.value]
            if not r["trashed"] or trash is True or _drive_owned_by(r["author_email"], me)
        ]
    elif name is not None:
        # A name lookup (mirage resolves every gdrive file with `name='…'`) — SQL title LIKE
        # instead of materializing the whole corpus (~25k rows, ~1.6s) to match in Python. The
        # remaining terms, `trashed` among them, still filter the (small) name-matched set below.
        candidates = store.list_drive_by_name(
            conn, name.value, container, ids, limit=100_000, exact=name.op == "=", trash=trash
        )
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
            trash=trash,
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
# What `about`'s object fields hold, as `_DRIVE_FILE_SUBFIELDS` gives a file's.
_DRIVE_ABOUT_SUBFIELDS = {
    "driveThemes": frozenset({"backgroundImageLink", "colorRgb", "id"}),
    "storageQuota": frozenset({"limit", "usage", "usageInDrive", "usageInDriveTrash"}),
    "teamDriveThemes": frozenset({"backgroundImageLink", "colorRgb", "id"}),
    "user": _DRIVE_USER_KEYS,
}

_MASK_SPACED_NAMES = re.compile(r"(?<=[^ \t,()/])[ \t]+(?=[^ \t,()/])")


def _drive_about_mask_is_required(mask: str) -> bool:
    """Whether `about` answers a mask `_mask_paths` refuses whole with the missing-mask 400,
    `required`, rather than with that refusal: it does for each one but a mask ending in a comma
    and one that would be a mask if two names with only spaces or tabs between them were one.

    Measured 2026-10-20 and 2026-10-25: `user,`, `user,,`, `,,`, `user(,`, `*,`, `(,`,
    `user/,`, `user(kind),`, `user, ` and `user,<TAB>` were `Invalid field selection` and the mask,
    and so were `user name`, `user<TAB>name`, `user name(kind)`, `user(kind name)` and
    `kind, user name`; `user,,kind`, `,user`, ` ,user`, `user(`, `user)(`, `user(kind,)`,
    `user//kind`, `user;kind`, `user*`, `*name`, `user *`, `user(kind) name`, `user name)` and
    `kind,user(kind name` were `required`."""
    if mask.rstrip(" \t").endswith(","):
        return False
    try:
        _mask_paths(_MASK_SPACED_NAMES.sub("_", mask))
    except gerr.GoogleError:
        return True
    return False


def _drive_about_field_keys(fields: str | None) -> set[str] | None:
    """``about.get`` is the one Drive read whose ``fields`` mask is MANDATORY — the resource has no
    default projection, and real Drive 400s without one. ``None`` = serve everything (``*``).

    A mask that selects nothing is that 400 too: real answered it to white space alone, any one of
    the 29 code points `str.isspace` takes, and to `user()`, `user( )` and `kind()`, measured
    2026-10-20, and to an empty group beside another key, `user(),kind` and `kind,user()`, on
    2026-10-25. So does each mask `_drive_about_mask_is_required` takes."""
    if fields is None:
        raise gerr.required("fields", "The 'fields' parameter is required for this method.")
    if _drive_about_mask_is_required(fields):
        raise gerr.required("fields", "The 'fields' parameter is required for this method.")
    paths = _mask_paths(fields)
    _check_mask(paths, _DRIVE_ABOUT_FIELDS, subfields=_DRIVE_ABOUT_SUBFIELDS)
    if any(len(p) > 1 and p[-1] == _MASK_EMPTY for p in paths):
        raise gerr.required("fields", "The 'fields' parameter is required for this method.")
    keys = _mask_keys(paths)
    if keys is not None and not keys:
        raise gerr.required("fields", "The 'fields' parameter is required for this method.")
    return keys


# The conversion tables below describe the *API's* capabilities, not this account's, so they carry
# Google's real values even though Backlot is read-only: a client that reads them to decide what
# to ask for must branch the same way it would against real Drive.

# What `files.export` can turn each native type into, in the order real `about.exportFormats`
# lists them (measured 2026-09-23), and what `drive_files_export` accepts. Kept to the three
# native types Backlot actually stores (`_NATIVE` minus the folder, which is not exportable
# anywhere).
_DRIVE_EXPORT_FORMATS = {
    DRIVE_DOC_MIME: [
        "application/rtf",
        "application/vnd.oasis.opendocument.text",
        "text/html",
        "application/pdf",
        "text/x-markdown",
        "text/markdown",
        "application/epub+zip",
        "application/zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
    ],
    "application/vnd.google-apps.spreadsheet": [
        "application/x-vnd.oasis.opendocument.spreadsheet",
        "text/tab-separated-values",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
        "application/zip",
        "application/vnd.oasis.opendocument.spreadsheet",
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
    # 400s on an absent or unknown mask
    mask = gerr.first_repeat(request.query_params, "fields")
    _drive_about_field_keys(mask)
    ids = auth.visible_ids(request, caller)
    # A caller with no mailbox of their own is the admin/service token; real Drive reports a
    # concrete address here either way, as gmail.users.getProfile already does.
    email = caller.email or _service_email(request)
    used, trashed = store.drive_usage_bytes(conn, ids)
    about = {
        "kind": "drive#about",
        "user": _drive_user(email, True),  # `about.user` IS the caller
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
    return _drive_trim_object(
        about, _mask_paths(mask), None, frozenset({"kind", "storageQuota", "user"})
    )


@router.get("/drive/v3/drives", openapi_extra={"parameters": _P_DRIVE_DRIVES})
async def drive_shared_drives(request: Request):
    """Shared (Team) Drives — Backlot's corpus lives entirely in My Drive, so this is empty.
    Present so shared-drive-aware clients don't 404 while enumerating."""
    _require(request)
    _drive_page_size_in_range(
        _drive_typed(request, "useDomainAdminAccess", page_size=True)["pageSize"], 100
    )
    _drive_refuse_a_trailing_comma(request)
    # A `q` it does not take comes after the typed and range refusals and ahead of a bad
    # `pageToken` and the 403 below, measured 2026-10-20 by sending one of each beside it.
    _drive_q_parse(gerr.first_repeat(request.query_params, "q") or "", _DRIVES_Q)
    _drive_listing_page_token(request)
    # Every caller here is a member of one Workspace domain and none is its administrator: a
    # member's 403, with a `q` sent and without one, on its own and in a batch, measured 2026-10-06.
    # Real answers a domain administrator 200 and a consumer account 400 at `q`.
    if _drive_true(request, "useDomainAdminAccess"):
        raise gerr.domain_admin_privilege_required()
    body = {"kind": "drive#driveList", "drives": []}
    # The rest of the mask's refusals last: real answered a name the list does not have after a bad
    # `q`, a bad `pageToken` and the 403, each sent beside it, measured 2026-10-20.
    mask = gerr.first_repeat(request.query_params, "fields")
    projected = _drive_project_listing(
        body, mask, "drives", _DRIVE_DRIVE_FIELDS, _DRIVE_DRIVE_DEFAULT
    )
    return body if projected is None else projected


@router.get(
    "/drive/v3/files", response_model=DriveFileList, openapi_extra={"parameters": _P_DRIVE_LIST}
)
async def drive_files_list(request: Request):
    """A listing is the union of two streams — the stored files and the synthesized folders — put
    through one matcher, one sort and one projection, so a query that should match a folder does
    and every row comes back shaped the way the caller asked for."""
    conn = auth.conn(request)
    caller = _require(request)
    me = caller.email
    ids = auth.visible_ids(request, caller)
    # Each read off the first repeat, as real reads them -- see `gerr.first_repeat`. Refused in
    # real's order, measured 2026-09-23 by sending two bad values at once: `pageSize` first, then
    # `orderBy`, `q`, `pageToken` and `fields`, whichever order the query names them in. The 403 for
    # an `orderBy` naming a key twice comes at the same point, measured 2026-10-05, the shared-drive
    # 403 between `orderBy` and `q`, measured 2026-10-04, the 403 for an `orderBy` on a `q` with a
    # `fullText` term between `q` and `pageToken`, measured 2026-10-05 and 2026-10-07, and the 500
    # for `starred` after another key between `pageToken` and `fields`, measured 2026-10-04 and
    # 2026-10-07. A `fields` ending in a comma comes right after `pageSize`, as
    # `_drive_refuse_a_trailing_comma` says.
    params = request.query_params
    typed = _drive_typed(
        request,
        "supportsAllDrives",
        "supportsTeamDrives",
        "includeItemsFromAllDrives",
        "includeTeamDriveItems",
        page_size=True,
    )
    limit = _drive_page_size(typed["pageSize"])
    _drive_refuse_a_trailing_comma(request)
    # 400 on an unusable key, 403 on a key named twice
    order = _drive_order_specs(gerr.first_repeat(params, "orderBy"))
    shared_items = _drive_true(request, "includeItemsFromAllDrives") or _drive_true(
        request, "includeTeamDriveItems"
    )
    if shared_items and not (
        _drive_true(request, "supportsAllDrives") or _drive_true(request, "supportsTeamDrives")
    ):
        raise gerr.supports_all_drives_required()
    q = gerr.first_repeat(params, "q") or ""
    query = _drive_q_parse(q)  # 400 on a clause Backlot cannot evaluate; None when there is no q
    fulltext = any(t.field == "fullText" for t in _drive_q_terms(query))
    if order and fulltext:
        raise gerr.sorting_not_supported_fulltext()
    # Measured 2026-09-23: a token the API did not issue is 400 `Invalid Value`, where an empty one
    # is the first page.
    offset = decode_cursor_or_none(gerr.first_repeat(params, "pageToken"))
    if offset is None:
        raise gerr.invalid_value("pageToken")
    if _drive_starred_after_another_key(gerr.first_repeat(params, "orderBy")):
        raise gerr.drive_internal_error()
    mask = gerr.first_repeat(params, "fields")
    if mask is not None and not mask.strip(" \t"):
        # The blank mask `_drive_project_one` describes; on a listing it drops `kind` and
        # `files` too, so the typed response model is bypassed.
        return JSONResponse({})
    list_paths = _drive_mask(mask)
    if list_paths is not None:  # 400 on a name the listing or its files do not have
        _check_mask(
            list_paths,
            _DRIVE_LIST_FIELDS,
            "files",
            _DRIVE_FILE_FIELDS,
            _DRIVE_FILE_UNSERVED,
            _DRIVE_FILE_SUBFIELDS,
        )
    conjuncts = _drive_q_conjuncts(query)
    hits = _drive_q_fulltext(conn, query, ids)
    # A parent every match has to be under; one under an `or` scopes nothing.
    parent_ids = [t.value for t in conjuncts if t.field == "parents"]
    roots = _drive_roots(me)
    in_root = any(pid in roots for pid in parent_ids)
    # A folder-scoped parent resolves to one container name (for the SQL-scoped paths below).
    scoped = [pid for pid in parent_ids if pid not in roots]
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
    if in_root and scoped:
        total_rows, fetch = 0, lambda o, n: []  # a file is at the root or in a folder, not both
    elif in_root and (scope := _drive_q_plain_folder(query)):
        # The files at the root are the ones a write created there (`_DRIVE_ROOT_FOLDER`): every
        # corpus file lives in a folder.
        trash = _drive_listed_trash(ids, me) if scope == "all" else False
        total_rows = store.count_drive_folder(conn, _DRIVE_ROOT_FOLDER, ids, trash)
        fetch = lambda o, n: store.list_drive_folder(  # noqa: E731
            conn, _DRIVE_ROOT_FOLDER, ids, limit=n, offset=o, trash=trash
        )
    elif in_root:
        matched = _drive_q_rows(conn, query, _DRIVE_ROOT_FOLDER, ids, me, hits)
        if not fulltext:
            matched.sort(key=lambda r: r["id"])
            matched.sort(key=_drive_modified, reverse=True)
        total_rows, fetch = len(matched), lambda o, n: matched[o : o + n]  # noqa: E731
    elif container is not None and (scope := _drive_q_plain_folder(query)):
        # The common case: a client walking the tree wants just this folder's files.
        trash = _drive_listed_trash(ids, me) if scope == "all" else False
        total_rows = store.count_drive_folder(conn, container, ids, trash)
        fetch = lambda o, n: store.list_drive_folder(  # noqa: E731
            conn, container, ids, limit=n, offset=o, trash=trash
        )
    elif query is not None:  # filter the visible set by the query, then paginate
        matched = _drive_q_rows(conn, query, container, ids, me, hits)
        if not fulltext:  # in the order `_drive_newest_first` reads rows in
            matched.sort(key=lambda r: r["id"])
            matched.sort(key=_drive_modified, reverse=True)
        total_rows, fetch = len(matched), lambda o, n: matched[o : o + n]  # noqa: E731
    else:
        trash = _drive_listed_trash(ids, me)  # the caller's own trash with the rest
        total_rows = store.count_documents(conn, "google_drive", visible_ids=ids, trash=trash)
        fetch = lambda o, n: store.list_documents(  # noqa: E731
            conn, "google_drive", visible_ids=ids, limit=n, offset=o, trash=trash, newest_first=True
        )

    # The file ids that came from the row stream, as opposed to a synthesized folder -- see
    # _drive_fill_shared, which marks only those.
    stored: set[str] = set()

    def objects(o: int, n: int, *, with_shared: bool = True) -> list[dict]:
        rows = fetch(o, n) if n > 0 else []
        stored.update(r["id"] for r in rows)
        shared = store.gdrive_shared(conn, [r["id"] for r in rows]) if with_shared else ()
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
    elif fulltext:
        # The index's rank order (`_drive_q_rows`), the folder objects as the tail: real lists a
        # `fullText` match in an order of its own, not by time (`_drive_newest_first`).
        files = objects(offset, min(limit, max(0, total_rows - offset)))
        if len(files) < limit:
            start = max(0, offset - total_rows)
            files += folders[start : start + limit - len(files)]
    else:
        files = _drive_newest_first(objects, folders, offset, limit)
        _drive_fill_shared(conn, files, stored)
    # No mask answers each entry with the `_DRIVE_FILE_DEFAULT` keys alone, as `_drive_project_one`
    # answers a file read by id.
    entry_keys = (
        set(_DRIVE_FILE_DEFAULT)
        if list_paths is None
        else _drive_entry_keys(list_paths, "files", _DRIVE_FILE_DEFAULT)
    )
    body = {
        "kind": "drive#fileList",
        "incompleteSearch": False,
        "files": _drive_project_entries(files, entry_keys),
    }
    # Nested object sub-selection (`files(owners(kind))`) the flat `_drive_project` pass cannot
    # express, but only when entries are projected and no top-level `*` names the whole entry.
    if (
        list_paths is not None
        and entry_keys is not None
        and not any(p == ("*",) for p in list_paths)
        and any(len(p) > 1 and p[0] == "files" and p[1] != "*" for p in list_paths)
    ):
        body["files"] = _drive_project_paths(
            body["files"], list_paths, "files", _DRIVE_FILE_DEFAULT
        )
    token = next_page_token(offset, len(files), total)
    if token:
        body["nextPageToken"] = token
    # Trim the listing's own top-level keys to the mask, the way real answers `fields=kind` with
    # only kind, `fields=nextPageToken` without files and `files(id)` without kind/incompleteSearch.
    tops = _mask_keys(list_paths, _DRIVE_LIST_DEFAULT)
    if tops is not None:
        if any(p[0] == "files" for p in list_paths):
            tops.add("files")
        body = {k: v for k, v in body.items() if k in tops}
    if not body:
        return JSONResponse({})
    return JSONResponse(body)


@router.get("/drive/v3/files/{file_id}", openapi_extra={"parameters": _P_DRIVE_ALT})
async def drive_files_get(file_id: str, request: Request):
    if _drive_batch_download(request):
        _drive_batch_redirect(request)
    # A byte-stream read answers in real's measured order: a `Bearer` that does not resolve is its
    # 401 (2026-10-07), a `callback` is the 503 an uncallable name answers whatever the download
    # would have done (2026-10-04, 2026-10-05), and the absent credential is named afterwards, once
    # `_drive_typed` has had its say (2026-10-07) -- `gerr.refuse_download` and the late `_require`.
    # The metadata read below keeps its own order: its credential first, then the typed parameters.
    # Inside a batch the redirect above answers first, whatever the part carries.
    download = _drive_download_request(request)
    conn = auth.conn(request)
    if download:
        _require_download_bearer(request)
        gerr.refuse_download(request)
    else:
        caller = _require(request)
    _drive_typed(request, "acknowledgeAbuse", "supportsAllDrives", "supportsTeamDrives")
    if download:
        caller = _require(request, download=True)
    else:
        _drive_refuse_a_trailing_comma(request)
    # Measured 2026-10-04: before the lookup, so a file that does not exist is refused alike, and
    # before each `fields` refusal but `_drive_refuse_a_trailing_comma`'s. Inside a batch real
    # checks a part's own flag only when the part is the batch's one part that is not a download,
    # measured 2026-10-05 beside downloads, other reads and a second flagged part.
    outer = _BATCH_OUTER.get()
    if not download and _drive_true(request, "acknowledgeAbuse") and (outer is None or outer.lone):
        raise gerr.abuse_acknowledgment_not_applicable()
    ids = auth.visible_ids(request, caller)
    row = store.gdrive_by_id(conn, file_id, visible_ids=ids)
    if row is None:
        # Folders aren't stored as rows. Measured 2026-10-15 and 2026-10-16 with a Workspace
        # member's token: real serves a folder the caller can see with 200, and answers one it
        # cannot see with the 404 notFound an id that names nothing gets, `fields` mask or not.
        name = _drive_folder_name_by_id(conn, file_id)
        if name is not None and store.drive_folder_has_visible(conn, name, ids):
            # Measured 2026-10-16: a visible folder cannot be downloaded, just as a native
            # Docs Editors file cannot; a folder the caller cannot see remains a 404.
            if gerr.alt_format(request.query_params) == "media":
                raise gerr.not_downloadable()
            return _drive_project_one(
                _drive_folder_obj(conn, name, caller.email),
                gerr.first_repeat(request.query_params, "fields"),
            )
        raise gerr.not_found_file(file_id)
    # `gerr.alt_format`, not `.get`: measured 2026-09-17 against this same route, `alt=MEDIA` and
    # `alt=Media` download the content just as `alt=media` does, and `alt=media&alt=json` downloads
    # it where `alt=json&alt=media` answers the metadata — the first repeat decides, and
    # ``QueryParams.get`` answers the last.
    if gerr.alt_format(request.query_params) == "media":
        # raw download — real API errors on native Docs-editors types (use export)
        if _native(row) is not None:
            raise gerr.not_downloadable()
        mime = row["mime_type"] or "application/octet-stream"
        return Response(row["content"].encode("utf-8"), media_type=mime)
    # Same projection as files.list: a file resolved by id and the same file read out of a listing
    # must come back identical, or caching/diffing behaves differently depending on which call
    # produced the row.
    return _drive_project_one(
        _drive_file(conn, row, me=caller.email),
        gerr.first_repeat(request.query_params, "fields"),
    )


@router.get("/drive/v3/files/{file_id}/export", openapi_extra={"parameters": _P_DRIVE_EXPORT})
async def drive_files_export(file_id: str, request: Request):
    """Measured 2026-09-23, the refusals come in this order: an absent `mimeType` ahead of the file
    lookup (a file that does not exist is still `Required parameter: mimeType`), then the 404,
    then a file that is not a Docs Editors one, then a format its type does not export to. That
    last one is matched without regard to case -- `TEXT/CSV` exports -- and an empty `mimeType=`
    is one of them rather than an absent parameter.

    A byte-stream read layers real's three download refusals before those, measured 2026-10-04,
    2026-10-05 and 2026-10-07: a `Bearer` that does not resolve is its 401, a `callback` is the 503
    an uncallable name answers and the shape every later error takes, and no credential is refused
    only after `mimeType` (`gerr.missing_api_key` with no `Authorization`,
    `gerr.download_credential_refused` with one that is no credential). An export asking for
    `alt=json` is none of that -- it is an ordinary read, which `_drive_download` decides.

    The export's `Content-Type` is the `mimeType` as sent and nothing more, measured 2026-09-30 on
    eleven formats of a spreadsheet and a document under five `Accept` values each (none, `*/*`,
    `application/json`, `text/html`, `application/xml`): `text/csv`, `TEXT/CSV`, `Text/Csv`,
    `text/markdown` and `text/html` each came back as exactly that, with no `charset`. So the
    header is set whole, where a ``media_type`` would have Starlette append `; charset=utf-8` to a
    lower-case `text/` type."""
    if _drive_batch_download(request):
        _drive_batch_redirect(request)
    download = _drive_download_request(request)
    conn = auth.conn(request)
    if download:
        _require_download_bearer(request)
        gerr.refuse_download(request)
    else:
        caller = _require(request)
    requested = gerr.first_repeat(request.query_params, "mimeType")
    if requested is None:
        raise gerr.required("mimeType")
    if download:
        caller = _require(request, download=True)
    ids = auth.visible_ids(request, caller)
    row = store.gdrive_by_id(conn, file_id, visible_ids=ids)
    if row is None:
        # A folder is synthesized rather than stored, so the lookup misses it. Measured 2026-10-09
        # and 2026-10-15: real refuses the export of a folder the caller can see with the 403 a PDF
        # gets, and of one it cannot see with the 404 an id that names nothing gets.
        name = _drive_folder_name_by_id(conn, file_id)
        if name is not None and store.drive_folder_has_visible(conn, name, ids):
            raise gerr.not_exportable()
        raise gerr.not_found_file(file_id)
    native = _native(row)
    if native is None or native[2] is None:  # binary or folder — not exportable
        raise gerr.not_exportable()
    target = requested.casefold()
    if target not in {f.casefold() for f in _DRIVE_EXPORT_FORMATS.get(native[0], ())}:
        raise gerr.unsupported_conversion()
    # honor the requested target format; CSV/TSV serve the cells, others prefix the title.
    #
    # CSV needs no branch for either kind of document: a document that STATES a grid has its
    # `content` derived from the first sheet's CSV at import, and one that does not has the text it
    # always had. TSV is a DIFFERENT serialisation of the same cells — measured, it has no quoting
    # mechanism and collapses an embedded newline or tab to a single space — so a stated grid
    # re-serialises for it. A prose document exports verbatim either way: its cells ARE its lines,
    # so there is nothing to re-serialise.
    if target == "text/tab-separated-values":
        stored = store.gdrive_sheets_for(conn, file_id)
        if stored:
            grid = json.loads(stored[0]["grid"])
            return Response(gsheets.to_tsv(grid), headers={"content-type": requested})
    plain = target in ("text/csv", "text/tab-separated-values")
    body = row["content"] if plain else f"{row['title']}\n\n{row['content']}"
    return Response(body, headers={"content-type": requested})


@router.get(
    "/drive/v3/files/{file_id}/permissions",
    response_model=DrivePermissionList,
    openapi_extra={"parameters": _P_DRIVE_PERMISSIONS},
)
async def drive_files_permissions(file_id: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    sizes = _drive_typed(
        request, "supportsAllDrives", "supportsTeamDrives", "useDomainAdminAccess", page_size=True
    )["pageSize"]
    _drive_page_size_in_range(sizes, 100)
    _drive_refuse_a_trailing_comma(request)
    # Refused in real's order, measured by sending two bad values at once: the `pageToken`
    # refusals, a repeated `pageSize`'s 500, the domain-administrator refusal and the file lookup
    # below, and a token issued for another file after those, in `_drive_permission_page`. The
    # places of the 500 and of the other-file token were measured 2026-10-08.
    token = _drive_permissions_page_token(request)
    size = _drive_permissions_page_size(sizes)
    # No caller here is a domain administrator: 404 for the file, even one the caller owns,
    # measured 2026-10-04 on a consumer account and 2026-10-06 on a Workspace member.
    if _drive_true(request, "useDomainAdminAccess"):
        raise gerr.not_found_file(file_id)
    ids = auth.visible_ids(request, caller)
    row = store.gdrive_by_id(conn, file_id, visible_ids=ids)
    if row is None:
        # A folder id is a first-class file id on real Drive — files.get answers for one, so
        # permissions.list has to as well. Folders aren't stored as rows, so their sharing comes
        # from the grants on the files they hold. Measured 2026-10-15 and 2026-10-16 with a
        # Workspace member's token, real answers a folder the caller cannot see with the 404
        # notFound an id that names nothing gets.
        name = _drive_folder_name_by_id(conn, file_id)
        if name is None or not store.drive_folder_has_visible(conn, name, ids):
            raise gerr.not_found_file(file_id)
        permissions = _drive_permissions(conn, file_id, folder=name)
    else:
        permissions = _drive_permissions(conn, row["id"])
    mask = gerr.first_repeat(request.query_params, "fields")
    if mask is None:
        permissions = [_drive_permission_view(p) for p in permissions]
    body = _drive_permission_page(permissions, file_id, size, token)
    # Real answered a name the list does not have after the `pageToken` refusals, the
    # domain-administrator 404 and the file lookup, each sent beside it, measured 2026-10-20.
    projected = _drive_project_listing(
        body, mask, "permissions", _DRIVE_PERMISSION_FIELDS, _DRIVE_PERMISSION_DEFAULT
    )
    return body if projected is None else JSONResponse(projected)


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
# invalid-argument one. `.pptx -> Slides` gives the Office message too, measured on 2026-10-10.
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

    Sheets, Docs and Slides resolve the stored type before visibility: a hidden file of their own
    type is permission-denied, while a hidden file of another type gets that type's usual answer.
    Measured on 2026-10-06, for an Office file on 2026-10-09, and through Slides on 2026-10-10 and
    2026-10-17."""
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    # A native Doc/Sheet/Slides id is the SAME id space as Drive's own file id --
    # real Google resolves docs.googleapis.com/etc. off the identical Drive file id, so this has
    # to resolve the file's own id, unscoped, to distinguish a missing id from a stored file that
    # this caller cannot see.
    row = store.gdrive_by_id(conn, file_id, visible_ids=None)
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
        if store.gdrive_by_id(conn, file_id, visible_ids=ids) is None:
            raise gerr.permission_denied()
        return row
    if subtype in _EDITOR_NATIVE:  # a different Workspace type: not this API's entity at all
        raise gerr.not_found_entity()
    if subtype in _EDITOR_OFFICE_FAMILY[expect]:
        raise gerr.failed_precondition(EDITOR_OFFICE)
    raise gerr.invalid_argument(EDITOR_INVALID_ARG)


def _a1_enum_error(field: str, enum: protojson.Enum, value: str) -> str:
    """Google's own wording for a bad read enum — it names the proto field and message type, e.g.
    ``Invalid value at 'major_dimension' (…sheets.v4.Dimension), "DIAGONAL"``. Measured, because a
    client that matches on the message needs the real one."""
    return f"Invalid value at '{field}' ({enum.type_url}), \"{value}\""


def _sheets_bool_value(raw: str | None, field: str) -> bool:
    """One of the boolean query parameters, read as a JSON body's string is (``protojson.to_bool``).
    Measured: `1`, `t`, `y` and `yes` mean true and `0`, `f`, `n` and `no` mean false, whatever the
    case of their ASCII letters, while `on`/`off`, a padded `" true"`, `2`, `01` and `1.0` are
    refused, as ``Invalid value at '<field>' (TYPE_BOOL), "<value>"``, naming the proto TYPE rather
    than a message. An absent flag is false; an EMPTY one is not absent and 400s."""
    if raw is None:
        return False
    try:
        return protojson.to_bool(("string", raw))
    except protojson.ConversionError:
        raise gerr.invalid_field_value(
            field, f"Invalid value at '{field}' (TYPE_BOOL), \"{raw}\""
        ) from None


def _sheets_enum_value(raw: str, field: str, enum: protojson.Enum) -> str:
    """One of the read enums from the query string, as its canonical name.

    Read the way a JSON body's string is (``protojson.enum_from_string``), measured on the query
    string too: a number names the value it is the number of and nothing past the enum's last, so on
    `majorDimension` ``+2`` and ``02`` are `COLUMNS` while ``3``, ``-1``, ``2.0`` and a space-padded
    `` 2`` are refused. The response echoes the canonical name whatever the request used,
    `DIMENSION_UNSPECIFIED` as `ROWS` (:func:`google_sheets._sheets_major`). Anything else 400s
    naming the proto field and type and quoting the value as sent, and an EMPTY value is not an
    absent one — it 400s rather than falling back to the default."""
    try:
        return enum.names[protojson.enum_from_string(raw, enum)]
    except protojson.ConversionError:
        raise gerr.invalid_field_value(field, _a1_enum_error(field, enum, raw)) from None


# `documents.get`'s two enums, names in number order. Measured 2026-10-09: `suggestionsViewMode`
# reads `0` through `3`, `+1` and `01` (the value numbered 1) and a name in any ASCII case, and
# refuses `4`, `-1`, `1.0`, a padded `" 1"`, empty and `NOPE`. `commentsViewMode` reads the same
# way (2026-10-14: `3`, `+1` and `comments_view_mode_omitted` read, `4` and `-1` refused).
# `includeTabsContent` is the boolean `_sheets_bool_value` reads (2026-10-14: `no`, `n`, `y`, `T`
# and `F` read, `on`, `off`, `2` and `tru` refused). A value that cannot be read is a 400 before
# the document is looked up.
_DOCS_VIEW = "type.googleapis.com/google.apps.docs.v1"
_DOCS_SUGGESTIONS_VIEW = protojson.Enum(
    f"{_DOCS_VIEW}.SuggestionsViewMode",
    (
        "DEFAULT_FOR_CURRENT_ACCESS",
        "SUGGESTIONS_INLINE",
        "PREVIEW_SUGGESTIONS_ACCEPTED",
        "PREVIEW_WITHOUT_SUGGESTIONS",
    ),
)
_DOCS_COMMENTS_VIEW = protojson.Enum(
    f"{_DOCS_VIEW}.CommentsViewMode",
    (
        "COMMENTS_VIEW_MODE_UNSPECIFIED",
        "COMMENTS_VIEW_MODE_DEFAULT_FOR_CURRENT_ACCESS",
        "COMMENTS_VIEW_MODE_OMITTED",
        "COMMENTS_VIEW_MODE_INCLUDED",
    ),
)


@router.get("/docs/v1/documents/{document_id}")
async def docs_get(document_id: str, request: Request):
    """The document as Docs structural elements: its stored body, or one ``paragraph`` per line of
    its text when it states none (`backlot.gdocs`)."""
    # The credential, then the typed values, then the lookup -- `_typed_query` records the order.
    _require(request)
    # Validated and then unused: the body is the same whatever a value that is read selects, where
    # real's changes with it.
    _typed_query(
        request,
        {
            "suggestionsViewMode": lambda raw: _sheets_enum_value(
                raw, "suggestions_view_mode", _DOCS_SUGGESTIONS_VIEW
            ),
            "includeTabsContent": lambda raw: _sheets_bool_value(raw, "include_tabs_content"),
            "commentsViewMode": lambda raw: _sheets_enum_value(
                raw, "comments_view_mode", _DOCS_COMMENTS_VIEW
            ),
        },
    )
    row = _editor_doc(request, document_id, expect="document")
    body = store.gdrive_doc_body_for(auth.conn(request), document_id)
    if body is None:
        body = gdocs.from_text(row["content"] or "")
    # Real's order for an untabbed read (docs.googleapis.com, 2026-10-16, twelve Docs, six owned by
    # a Workspace member and six by a consumer account), the same under each `suggestionsViewMode`,
    # `includeTabsContent=false` and a `fields` mask naming the members in another order
    # (`documentId,title`, `revisionId,body,title`). Real also serves `headers` and `footers` after
    # `body`, `lists` after `namedStyles` and `inlineObjects` before `documentId` when a Doc has
    # them; this server serves none of the four.
    return {
        "title": row["title"],
        "body": {"content": gdocs.indexed(body)},
        "documentStyle": gdocs.document_style(),
        "namedStyles": gdocs.named_styles(),
        "revisionId": synth._digest(document_id)[:24],
        "suggestionsViewMode": "SUGGESTIONS_INLINE",
        "documentId": document_id,
        # Last, where real serves it. Real's value for a read that asks for no comments, whatever
        # `suggestionsViewMode` and `includeTabsContent` select (measured 2026-10-09 and
        # 2026-10-15); `commentsViewMode=COMMENTS_VIEW_MODE_INCLUDED` beside the tabs and inline
        # suggestions is answered `COMMENTS_VIEW_MODE_INCLUDED` there and this value here.
        "commentsViewMode": "COMMENTS_VIEW_MODE_OMITTED",
    }


@router.get("/slides/v1/presentations/{presentation_id}")
async def slides_get(presentation_id: str, request: Request):
    """The presentation as slides: one ``TEXT_BOX`` slide per blank-line-separated block of text."""
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
    return _NATIVE.get(row["subtype"] or store.DRIVE_DEFAULT_SUBTYPE)


def _drive_user(email: str, me: bool = False) -> dict:
    """A `User`, ``me`` when it is the caller: measured 2026-10-27, the owner reading its own file
    gets ``me: true`` on ``owners[]`` and on ``lastModifyingUser``."""
    return {
        "kind": "drive#user",
        "displayName": email.split("@")[0].replace(".", " ").title(),
        "emailAddress": email,
        "me": me,
        "permissionId": str(synth.github_user_id(email)),
        "photoLink": synth.github_avatar(synth.github_user_id(email)),
    }


# Where real's `exportLinks` point for a native file of each type, and the short name each type it
# exports to is named by there, read off a document, a spreadsheet and a presentation on
# 2026-10-25.
_DRIVE_EXPORT_LINK = {
    DRIVE_DOC_MIME: "https://docs.google.com/feeds/download/documents/export/Export",
    "application/vnd.google-apps.spreadsheet": "https://docs.google.com/spreadsheets/export",
    "application/vnd.google-apps.presentation": (
        "https://docs.google.com/feeds/download/presentations/Export"
    ),
}
_DRIVE_EXPORT_NAME = {
    "application/epub+zip": "epub",
    "application/pdf": "pdf",
    "application/rtf": "rtf",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/x-vnd.oasis.opendocument.spreadsheet": "ods",
    "application/zip": "zip",
    "text/csv": "csv",
    "text/html": "html",
    "text/markdown": "markdown",
    "text/plain": "txt",
    "text/tab-separated-values": "tsv",
    "text/x-markdown": "markdown",
}


def _drive_export_links(file_id: str, mime: str) -> dict[str, str]:
    """The `exportLinks` a native Docs Editors file carries, one per MIME `about.exportFormats`
    gives its type, in that order, at the address real's use with the format's short name."""
    base = _DRIVE_EXPORT_LINK.get(mime)
    if base is None:
        return {}
    return {
        out: f"{base}?id={file_id}&exportFormat={_DRIVE_EXPORT_NAME[out]}"
        for out in _DRIVE_EXPORT_FORMATS[mime]
    }


def _drive_mime(row) -> str:
    """The mimeType this row serves: a native Workspace type from its subtype, else its own
    declared type (and only a type-less binary falls back to an opaque blob)."""
    native = _native(row)
    return native[0] if native else (row["mime_type"] or "application/octet-stream")


def _drive_file_capabilities(is_folder: bool, owned: bool) -> dict:
    """A stored file's `capabilities`, for its owner or for anyone else, whom every grant here makes
    a reader. Measured 2026-10-22 on a spreadsheet, a plain-text file and a folder, each read by its
    owner and by a user it was shared with as a reader: the owner may comment, edit, rename, trash,
    delete, share and modify content, and the reader may do none of those; both may download. A
    file can be copied and its owner may read its revisions; a folder lists its children to both
    and takes new ones from its owner."""
    return {
        "canDownload": True,
        "canListChildren": is_folder,
        "canComment": owned,
        "canEdit": owned,
        "canCopy": not is_folder,
        "canShare": owned,
        "canRename": owned,
        "canTrash": owned,
        "canDelete": owned,
        "canReadRevisions": owned and not is_folder,
        "canAddChildren": owned and is_folder,
        "canModifyContent": owned,
    }


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
        shared = bool(store.gdrive_shared(conn, [row["id"]]))
    ext = row["title"].rsplit(".", 1)[-1] if (native is None and "." in row["title"]) else None
    owned = _drive_owned_by(author, me)
    f = {
        "kind": "drive#file",
        "id": row["id"],
        "name": row["title"],
        "mimeType": mime,
        "parents": store.jcol(row, "parents") or [synth.drive_folder_id(row["folder"])],
        "createdTime": synth.rfc3339(created),
        "modifiedTime": synth.rfc3339(modified),
        "owners": [_drive_user(author, owned)],
        "lastModifyingUser": _drive_user(author, owned),
        "trashed": bool(row["trashed"]),
        "explicitlyTrashed": bool(row["trashed"]),
        "starred": False,
        "shared": bool(shared),
        "viewedByMe": False,
        "ownedByMe": owned,
        **_modified_by_me(author, me, modified),
        **_shared_with_me_time(author, me, created),
        "version": str(2 if row["updated_ts"] else 1),
        "spaces": ["drive"],
        "webViewLink": view,
        "iconLink": f"https://drive.google.com/icons/{(row['subtype'] or 'document')}.png",
        "capabilities": _drive_file_capabilities(is_folder, owned),
    }
    # Per Google's reference, `size` "is populated for files with binary content stored in Google
    # Drive AND for Docs Editors files; it is not populated for shortcuts or folders" — so a native
    # Doc/Sheet/Slides carries it too. Checksums, a download link and the file-extension pair stay
    # binary-only, which is also what real Drive does for the Docs-editors types. A non-folder file
    # always answers `quotaBytesUsed` (its own `size`), and a native file answers `exportLinks`,
    # both measured 2026-10-21/25. A native file's `size` never goes under
    # `store.DRIVE_NATIVE_MIN_BYTES`, where the measurement is recorded.
    if not is_folder:
        nbytes = store.drive_file_bytes(row["subtype"], row["content"])
        f["size"] = str(nbytes)
        f["quotaBytesUsed"] = str(nbytes)
    if native is not None and not is_folder:
        f["exportLinks"] = _drive_export_links(row["id"], mime)
    if native is None:
        f["md5Checksum"] = hashlib.md5(row["content"].encode()).hexdigest()
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

    def full(
        kind_id: str,
        ptype: str,
        role: str,
        email: str | None = None,
        name: str | None = None,
        *,
        anyone: bool = False,
    ) -> dict:
        """A permission as real answers it under `permissions` or `*`: the four default keys and
        `permissionDetails`, a direct entry for its role and, on an owner's, an inherited `writer`
        entry before it; an `anyone` permission adds `allowFileDiscovery`, any other `deleted`
        (false), and a user's `pendingOwner` (false) and `photoLink`. Measured 2026-10-25 on the
        permissions of a Workspace member's 193 files: 192 owners', 45 writers', a user commenter's,
        two groups' and two `anyone` permissions, where one owner's and nine writers' carried a
        single inherited entry instead. No user reader's was among them; a reader here takes the
        commenter's shape."""
        details = [{"permissionType": "file", "role": role, "inherited": False}]
        if role == "owner":
            details.insert(0, {"permissionType": "file", "role": "writer", "inherited": True})
        p = {
            "kind": "drive#permission",
            "id": kind_id,
            "type": ptype,
            "role": role,
            "permissionDetails": details,
        }
        if anyone:
            p["allowFileDiscovery"] = True
        else:
            p["deleted"] = False
        if ptype == "user":
            p["pendingOwner"] = False
        if email is not None:
            p["emailAddress"] = email
        if name is not None:
            p["displayName"] = name
        if ptype == "user":
            p["photoLink"] = synth.github_avatar(synth.github_user_id(email))
        return p

    row = None if folder else store.get_document(conn, "google_drive", file_id)
    owner_email = row["author_email"].lower() if row is not None else None
    perms = []
    for g in grants:
        ptype, pid = g["principal_type"], g["principal_id"]
        if ptype == "user" and pid.lower() == owner_email:
            # The owner's own grant is the owner permission below, not a reader's beside it:
            # measured 2026-10-26, a file whose only grant is its owner's lists one permission.
            continue
        if ptype == "org":  # anyone-in-org / anyone-with-link
            perms.append(full("anyoneWithLink", "anyone", "reader", anyone=True))
        elif ptype == "group":
            perms.append(
                full(
                    str(synth.github_user_id(pid)),
                    "group",
                    "reader",
                    email=f"{pid}@{domain}",
                    name=pid,
                )
            )
        else:  # user
            perms.append(
                full(
                    str(synth.github_user_id(pid)),
                    "user",
                    "reader",
                    email=pid,
                    name=pid.split("@")[0].replace(".", " ").title(),
                )
            )
    # every file has an owner
    if row is not None:
        owner = row["author_email"]
        perms.insert(
            0,
            full(
                str(synth.github_user_id(owner)),
                "user",
                "owner",
                email=owner,
                name=owner.split("@")[0].replace(".", " ").title(),
            ),
        )
    return perms


def _drive_permission_view(p: dict) -> dict:
    """The default `permissions.list` projection: real answers id, type, kind and role with no
    mask; emailAddress, displayName and the rest appear only under an explicit mask (`*`)."""
    return {k: p[k] for k in ("id", "type", "kind", "role") if k in p}


def _typed_query(request: Request, readers: dict) -> dict[str, list]:
    """Every repeat of each typed query parameter in ``readers``, parsed and keyed by name, in the
    order the query sent them.

    Real parses every repeat of a typed parameter, not only the one it reads, and refuses all it
    cannot parse in one 400 (:func:`gerr.invalid_field_values`). :func:`gerr.first_repeat` names
    the parameters a bad value at either end was measured on, and the end each is READ from, which
    is the caller's to pick.

    The refusals are grouped by parameter, each parameter's in query order, and the groups come in
    the order their parameters first appear. Measured 2026-09-23, eight identical requests each:
    `majorDimension=NOPE1&valueRenderOption=NOPE2&majorDimension=NOPE3` kept the two
    `major_dimension` refusals together and in that order every time, while which parameter came
    first varied from one request to the next (`valueRenderOption=NOPE&majorDimension=NOPE`
    answered each order four times), so first appearance is one of the orders real gives.

    Its callers run it after the credential and before the lookup, measured 2026-09-30 on Drive
    `files.get` and `permissions.list` and on Sheets `values.get`, `values:batchGet` and
    `spreadsheets.get`: with no credential or a bad one, a bad value is answered with the
    credential's refusal, and with a good one it is this 400 for an id that does not exist, where
    the same request without the bad value is the 404. `permissions.list` checks its `pageSize`
    range at the same point, as real does: `pageSize=0` on a missing file is the range refusal."""
    parsed: dict[str, list] = {name: [] for name in readers}
    refused: dict[str, list[tuple[str, str]]] = {}
    for name, raw in request.query_params.multi_items():
        if name not in readers:
            continue
        try:
            parsed[name].append(readers[name](raw))
        except gerr.GoogleError as exc:
            violations = gerr.field_violations(exc)
            if not violations:
                raise
            refused.setdefault(name, []).extend(violations)
    if refused:
        raise gerr.invalid_field_values([v for group in refused.values() for v in group])
    return parsed


# The typed booleans each Drive method Backlot serves declares, as the proto field its refusal
# names. Each is parsed and the parsed value never read. Measured 2026-09-23 on each of them: the
# Sheets boolean spellings (`_sheets_bool_value`), 30 of them swept on `supportsAllDrives`; and on
# 2026-10-06 `yeſ`, `YEſ`, `falſe` and `FALſE`, each refused, since only ASCII letters are folded
# (`protojson.to_bool`). Spelled `true`, four of them run a check of their own and two lift one --
# see `_drive_true`. `files.export` and `about.get` declare none, and real ignores
# `supportsAllDrives=NOPE` on both.
_DRIVE_BOOLS = {
    "supportsAllDrives": "supports_all_drives",
    "supportsTeamDrives": "supports_team_drives",
    "includeItemsFromAllDrives": "include_items_from_all_drives",
    "includeTeamDriveItems": "include_team_drive_items",
    "acknowledgeAbuse": "acknowledge_abuse",
    "useDomainAdminAccess": "use_domain_admin_access",
}


def _drive_typed(request: Request, *bools: str, page_size: bool = False) -> dict[str, list]:
    """`_typed_query` over the booleans a Drive method declares, and its `pageSize` if it has
    one."""
    readers = {
        name: (lambda raw, field=_DRIVE_BOOLS[name]: _sheets_bool_value(raw, field))
        for name in bools
    }
    if page_size:
        readers["pageSize"] = _drive_int32
    return _typed_query(request, readers)


def _drive_true(request: Request, name: str) -> bool:
    """Whether a Drive flag's first repeat is the word `true`, in any case.

    Four flags run a check of their own when spelled `true`, and the check reads the spelling rather
    than the boolean `_drive_typed` parses. Measured 2026-10-04 on `files.list`'s
    `includeItemsFromAllDrives`: `true`, `TRUE` and `tRuE` run it, while `t`, `1`, `y` and `yes`,
    which parse as true, do not; and `supportsAllDrives` lifts it at `true` and not at `t`, `1` or
    `yes`. `true&false` runs it and `false&true` does not."""
    return (gerr.first_repeat(request.query_params, name) or "").casefold() == "true"


def _drive_download(endpoint, query) -> bool:
    """Whether a request to `endpoint` with `query` is a Drive download: `files.get` with
    `alt=media`, or `files.export` with no `alt`, an empty one or `alt=media`."""
    alt = gerr.alt_format(query)
    return (endpoint is drive_files_get and alt == "media") or (
        endpoint is drive_files_export and alt in ("", "media")
    )


def _drive_download_request(request: Request) -> bool:
    """Whether this request is a Drive download (`_drive_download`), read off the route it matched.

    The one question ``_system_parameters`` and the two ``files.get``/``files.export`` handlers ask,
    so a download sent on its own and one sent as a batch part are answered alike, and so a request
    that merely LOOKS like one is not: measured 2026-10-07, an export asking for `alt=json` and a
    file whose id is literally `export` are ordinary reads to real."""
    return _drive_download(request.scope.get("endpoint"), request.query_params)


def _drive_batch_download(request: Request) -> bool:
    """Whether this request is a Drive download sent as a part of a batch. Read off the route the
    request matched, so the router's dependency can ask before the route runs."""
    return _BATCH_OUTER.get() is not None and _drive_download_request(request)


_URL_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")


def _batch_escapes(text: str, decoded: str) -> str:
    """`text` with each escape of an ASCII letter or digit, or of a character in `decoded`, decoded,
    and each other escape kept with its hex in upper case. A `%` that two hex digits do not follow
    stays as sent."""

    def one(match: "re.Match[str]") -> str:
        char = chr(int(match.group(1), 16))
        if char.isascii() and (char.isalnum() or char in decoded):
            return char
        return "%" + match.group(1).upper()

    return _URL_ESCAPE.sub(one, text)


def _batch_query_pairs(query: str) -> list[tuple[str, str]]:
    """A query string's pairs as real writes them into a batch redirect's `Location`. Measured
    2026-10-04, in a name and a value alike: an empty pair is dropped, a name with no `=` gains one,
    and an escape of an ASCII letter or digit is decoded, where any other escape keeps its byte and
    has its hex written in upper case (`%7e` is `%7E`, `%2d` is `%2D`); `+` and `%20` stay as
    sent."""
    return [
        (_batch_escapes(name, ""), _batch_escapes(value, ""))
        for name, _, value in (pair.partition("=") for pair in query.split("&") if pair)
    ]


def _drive_batch_redirect(request: Request) -> None:
    """A Drive download inside a batch, answered with real's redirect: a 302 to the same path under
    `/download`.

    Measured 2026-10-04 on `files.get` with `alt=media` and on `files.export`: the redirect comes
    after `$.xgafv` and after a credential the part carries (`_credential`), a bad one being the
    401, while a part with no credential at all is redirected: measured 2026-10-05,
    `Basic YWJjOmRlZg==`, `bearer nope`, a bare `Bearer` and `nope` are each redirected, though each
    is :func:`gerr.download_credential_refused` on a download sent on its own, and on 2026-10-11
    `OAuth nope`, `Bearer nope x`, `Bearer<t>`, `Bearer<TAB>nope` and `Bearer ,nope` were each the
    401 in a part. A token named in the part's query or in the batch's is
    :func:`gerr.token_in_query` instead of the redirect, after that 401, measured 2026-10-13. The
    redirect comes ahead of everything else measured beside it: a file that does not exist,
    `supportsAllDrives=NOPE`, `fields=bogus`, an absent or empty `mimeType`, a Docs file read with
    `alt=media`, and a `callback`, which neither wraps the redirect (`cb`) nor refuses it (`a b`).

    `Location` is the request's path under `/download`, as sent but for its escapes: measured
    2026-10-05 on `files/a%XXb` for each byte 0x20-0x7E, real decodes an escape of an ASCII letter,
    digit, `-`, `.`, `_` or `~` and keeps any other with its hex in upper case, `%23`, `%2F` and
    `%E2%82%AC` among them. Then come the request's query pairs, then each of the batch request's
    whose name the request's pairs do not carry, in the batch's order, all as `_batch_query_pairs`
    writes them. A name is matched as written there, so `fo%6F=` keeps the batch's `foo` out and
    `Foo` does not, and a name the batch repeats is carried every time. The path and the query are
    read from the request's raw bytes, not its decoded URL, in which `%23` would start a
    fragment."""
    if _credential(request)[0]:
        _require(request)
    outer = _BATCH_OUTER.get()
    if gerr.names_a_query_token(request.query_params) or gerr.names_a_query_token(
        QueryParams(outer.query)
    ):
        raise gerr.token_in_query()
    path = _batch_escapes(request.scope["raw_path"].decode("latin-1"), "-._~")
    pairs = _batch_query_pairs(request.scope["query_string"].decode("latin-1"))
    named = {name for name, _ in pairs}
    pairs += [(name, value) for name, value in _batch_query_pairs(outer.query) if name not in named]
    query = "&".join(f"{name}={value}" for name, value in pairs)
    raise gerr.download_redirect(f"{outer.base}download{path}" + (f"?{query}" if query else ""))


# An int32 as the Drive query parser takes one. Measured on `pageSize`, on `files.list` 2026-09-23
# and on `permissions.list` and `drives.list` 2026-09-30: `+2` and `02` are 2 and `-0` is 0, while
# a padded ` 2` or `2 `, `1_0`, `2.0`, `0x10`, `1e2`, an empty value and a value past 2**31 - 1 are
# the `TYPE_INT32` refusal.
_INT32 = re.compile(r"[+-]?[0-9]+")


def _drive_int32(raw: str) -> int:
    if _INT32.fullmatch(raw) and -(2**31) <= int(raw) < 2**31:
        return int(raw)
    raise gerr.invalid_field_value(
        "page_size", f"Invalid value at 'page_size' (TYPE_INT32), \"{raw}\""
    )


def _drive_page_size_in_range(sizes: list[int], top: int) -> None:
    """Refuse one `pageSize` outside 1 to ``top`` with real's range sentence, which names the value
    as an int. Measured 2026-09-23, and on each of the three routes again 2026-09-30: ``top`` is
    1000 on `files.list` and 100 on `permissions.list` and `drives.list`; `0`, `-1`, `-0` (named
    `0`), ``top + 1`` and `2147483647` are refused alike; and two or more values get no range
    sentence, the first of them deciding instead, in `_drive_page_size` on `files.list` and
    `_drive_permissions_page_size` on `permissions.list`."""
    if len(sizes) == 1 and not 1 <= sizes[0] <= top:
        raise gerr.invalid_parameter(
            "page_size",
            f"Invalid value '{sizes[0]}'. Values must be within the range: [value: 1\n, value: "
            f"{top}\n]",
        )


def _drive_permissions_token(file_id: str, offset: int) -> str:
    """The `nextPageToken` `permissions.list` issues: the offset of the next page and the file it
    pages, so that `_drive_permission_page` can refuse a token sent back to another file."""
    return base64.urlsafe_b64encode(f"p:{offset}:{file_id}".encode()).decode()


def _drive_permissions_page_token(request: Request) -> tuple[int, str] | None:
    """The `pageToken` sent to `permissions.list`, as the offset and the file it was issued for, or
    ``None`` when none was sent. Read from the first repeat. Measured against Drive v3 on 2026-10-04
    and 2026-10-05, the empty, `bad`, `bzow`, `BOGUS` and `files.list`-token cells again on
    2026-10-07 as a Workspace member, and `bzox` and the last two rows on 2026-10-08 on a consumer
    account::

        pageToken                         permissions.list          drives.list
        --------------------------------|-------------------------|-------------------------
        empty                           | 403 `pageTokenExpired`  | the first page
        `bad`, `bzow`, `bzox`, `AAAA`   | 400 `Invalid Value`     | 400 `Invalid Value`
        `BOGUS`, `0`, `a`, a space, a   | 500 `Unknown Error.`    | 400 `Invalid Value`
        token `files.list` issued       |                         |
        one issued for another file     | 403 `pageTokenExpired`  |
        one issued for this file        | the next page           |

    An issued token with a character added, changed or dropped was the 500, the 400, the 403 or the
    next page by which character it was, measured 2026-10-08. A token here names the offset, from 1,
    and the file (`_drive_permissions_token`), and only the exact spelling issued is read, so the
    spellings the table lists against the 400 and the 500, and every altered token, are the 400
    here: the 500, and an altered token's 403 and next page, are not modelled. A token naming
    another file does decode, and its 403 comes after the file lookup, in `_drive_permission_page`.
    `drives.list` issues none, and `_drive_listing_page_token` reads its column."""
    token = gerr.first_repeat(request.query_params, "pageToken")
    if token is None:
        return None
    if not token:
        raise gerr.page_token_expired()
    try:
        _, offset, file_id = base64.urlsafe_b64decode(token).decode().split(":", 2)
        issued = int(offset), file_id
    except ValueError:
        raise gerr.invalid_value("pageToken") from None
    if issued[0] < 1 or _drive_permissions_token(file_id, issued[0]) != token:
        raise gerr.invalid_value("pageToken")
    return issued


def _drive_permissions_page_size(sizes: list[int]) -> int | None:
    """The `pageSize` `permissions.list` pages by, or ``None`` for the whole list. One value outside
    1-100 is the range refusal `_drive_page_size_in_range` raises; two or more are read from the
    first, and real answers :func:`gerr.unknown_error` when that one is outside 1-100. Measured
    against Drive v3 on 2026-10-08 on a consumer account: `0&2`, `-0&2`, `-1&2`, `0&0`, `0&101`,
    `101&2`, `101&101`, `2147483647&2`, `-2147483648&2` and `0&2&2` each answered the 500, where
    `100&2` and `2&0&0` served two permissions and `1&0`, `+1&0`, `1&2` and `1&2&3` one and a
    token."""
    if not sizes:
        return None
    if not 1 <= sizes[0] <= 100:
        raise gerr.unknown_error()
    return sizes[0]


def _drive_permission_page(
    permissions: list[dict], file_id: str, size: int | None, token: tuple[int, str] | None
) -> dict:
    """One page of a file's permissions, in the unpaged list's order, and the token for the next.

    Measured against Drive v3 on two-permission My Drive files on 2026-10-05, and on a file and a
    folder on 2026-10-08: no `pageSize` serves the whole list and no `nextPageToken`; `pageSize=1`
    the first permission and a token; that token, sent back with a `pageSize` of 1 or 2 or with
    none, the other permission and no token; and a `pageSize` of 2, 3 or 100 both and no token. A
    token issued for another file is 403 `pageTokenExpired` (`_drive_permissions_page_token`)."""
    offset = 0
    if token is not None:
        offset, issued_for = token
        if issued_for != file_id:
            raise gerr.page_token_expired()
    end = len(permissions) if size is None else offset + size
    body = {"kind": "drive#permissionList", "permissions": permissions[offset:end]}
    if end < len(permissions):
        body["nextPageToken"] = _drive_permissions_token(file_id, end)
    return body


def _drive_listing_page_token(request: Request) -> None:
    """Refuse a `pageToken` sent to `drives.list`, which is empty and issues no `nextPageToken`,
    so every token is one it did not issue. Presence is the whole test: `decode_cursor_or_none`,
    which `files.list` calls, reads `bzow` and that route's own tokens as offsets. Read from the
    first repeat, ahead of the `useDomainAdminAccess=true` refusal; the route comment says what
    precedes it. What real answers is the `drives.list` column of the table
    `_drive_permissions_page_token` records."""
    token = gerr.first_repeat(request.query_params, "pageToken")
    if token:
        raise gerr.invalid_value("pageToken")


def _drive_page_size(sizes: list[int]) -> int:
    """The page size a `files.list` asks for, from the `pageSize` values `_drive_typed` parsed.

    Measured 2026-09-23: two or more values are read from the first, one past 1000 answering 1000
    files and one at or below 0 answering 500 (`0&2`, `0&0`, `-1&2` and `0&1001` all did, with
    more than 500 files to list), where `2&0` is 2 and `3&0` is 3. Absent is the default of 100."""
    _drive_page_size_in_range(sizes, 1000)
    if not sizes:
        return get_settings().default_page_size
    first = sizes[0]
    size = 1000 if first > 1000 else 500 if first < 1 else first
    return min(size, get_settings().max_page_size)


# The Sheets routes, registered on `router` by the module that holds them -- which imports this
# one's helpers, so it is imported last.
from backlot.routers import google_sheets  # noqa: E402, F401
