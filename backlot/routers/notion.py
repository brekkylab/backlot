"""Notion API (read-only).

Base URL for a client: ``http://<host>/notion/v1/`` (the notion-client SDK appends ``/v1/`` to
its ``base_url``, so point it at ``http://<host>/notion``). Bearer auth
(``Authorization: Bearer <token>``, that scheme and no other); the admin/service token sees
everything, a user token is ACL-filtered. Errors use Notion's envelope:
``{"object":"error","status","code","message","request_id"}``, the last of which is also the
response's ``x-notion-request-id`` (see :func:`_error`). The OAuth client endpoints Notion puts
behind Basic auth refuse in their own shape, ``{"error","request_id"}`` (see ``unmatched_path``).

**Version-aware databases.** Notion moved database querying to the *data sources* model in
``2025-09-03``. This router keys off the ``Notion-Version`` request header:

- ``2025-09-03`` and later: ``databases.retrieve`` returns a ``data_sources: [{id,name}]`` array
  and rows are read via ``POST /data_sources/{id}/query``.
- before it (2022-06-28 and the four versions older than that): ``databases.retrieve`` returns
  ``properties`` (schema) inline and rows are read via ``POST /databases/{id}/query``.

One query path per version, as on the real API: the other one answers ``invalid_request_url``,
and so does ``GET /data_sources/{id}`` under a version older than the split, which mounts no data
source route at all (see ``_unmounted_here``). ``databases.retrieve`` stays mounted and answers
in whichever of the two shapes the caller's version reads.

Backlot has one data source per database, its id assigned at import alongside the database's own.

**The header is required** on every route here, as Notion requires it on every REST request: a
request that omits it -- or names a version Notion does not publish -- is answered
``missing_version``, behind both 401s a request with no usable credential gets -- the one that
names the bearer format and the one that says the token does not resolve (see ``_refusal``) --
and behind the 400 a URL Notion does not publish gets ahead of everything (see
``unmatched_path``). Every route declares it in the spec as well, so a client generated from that
spec -- ``backlot mcp``'s tools among them -- can send what the route asks for.

Object mapping: a Notion *page* is one doc (``subtype='page'``); a *database* is one doc
(``subtype='database'``, ``content`` → its description); a *database row* is a page whose
``parent`` is the database. Page ``content`` is served verbatim as the block tree
(``blocks.children.list``); the join of the blocks' plain_text reconstructs ``content`` exactly.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backlot import auth, published, store, synth
from backlot import notion_request as req
from backlot.graphql import body as request_body
from backlot.openapi import qp

router = APIRouter(prefix="/notion/v1", tags=["notion"])
# Everything under `/notion` that no route above answers. Mounted after `router` in
# `backlot.main`, so a real route wins and only what is left reaches this one.
unmatched_router = APIRouter(prefix="/notion", include_in_schema=False)

_PAGE_MAX = 100  # Notion caps page_size at 100
# The version that split databases into data sources, so the cut between the two models: a
# request at or after it reads rows through a data source, one before it through the database.
# Notion names a version for its release date and lists them in that order, which is what makes
# ``_data_sources_model`` a date comparison ("Changes by version", read 2026-09-15).
DATA_SOURCES_VERSION = "2025-09-03"
# Every version Notion publishes, oldest first. Not a list off the document -- "Changes by
# version" omits 2021-05-11 -- but the enumeration the API itself answers an unrecognised version
# with, read off that refusal on 2026-09-17.
PUBLISHED_VERSIONS = (
    "2021-05-11",
    "2021-05-13",
    "2021-08-16",
    "2022-02-22",
    "2022-06-28",
    "2025-09-03",
    "2026-03-11",
)
#: The headers real puts on an answer past its body reader (:func:`front_door_body`), one value
#: each wherever they were measured against api.notion.com on 2026-10-27: the answers of each route
#: here (200s, 400s, 401s, 404s and a 503, `HEAD`s among them), the OAuth client endpoints' 401 and
#: the root's 302. The refusals that body reader answers carried none of them, and neither did the
#: front door's `405 Not Allowed` page for `TRACE`: a method outside :data:`ROUTED_METHODS` is the
#: framework's 405 here (:func:`unmatched_path`), and carries none of them either.
#: `strict-transport-security` came beside them and is left off, a promise of HTTPS that a server
#: answering over plain HTTP cannot keep.
APPLICATION_HEADERS = {
    "content-security-policy": "default-src 'none'",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-content-type-options": "nosniff",
    "x-dns-prefetch-control": "off",
    "x-download-options": "noopen",
    "x-frame-options": "SAMEORIGIN",
    "x-permitted-cross-domain-policies": "none",
    "x-xss-protection": "0",
}


def weak_etag(body: bytes) -> str:
    """The `ETag` real puts on a Notion answer: a weak tag holding the body's length in bytes, in
    hex, and the first 27 characters of the base64 SHA-1 of the body, the form Express's `etag`
    writes.

    Measured against api.notion.com on 2026-10-27: the 200s of `users/me`, `pages/{id}` and
    `search`, the 401s (the OAuth client endpoints' among them), the 400s, a 404 and a 503, and
    the body reader's 413 and 415 each carried one over its own body, a `HEAD` the tag of the
    body its `GET` answers; the root's 302 and a CORS preflight's 204 carried none. The
    computation was checked against the bodies kept whole.
    """
    digest = base64.b64encode(hashlib.sha1(body).digest()).decode("ascii")[:27]
    return f'W/"{len(body):x}-{digest}"'


#: ``fresh``'s test for `no-cache` among the comma-separated parts of a request's `Cache-Control`,
#: its `\s` written out as the characters JavaScript's `\s` holds in latin-1, which a header value
#: is read as.
_NO_CACHE = re.compile(r"(?:^|,)[\t\n\v\f\r \xa0]*?no-cache[\t\n\v\f\r \xa0]*?(?:,|$)")


def not_modified(request: Request, status_code: int) -> bool:
    """Whether real answers ``request`` with a 304 in place of its answer of ``status_code``: the
    freshness check Express runs on a `GET`'s 2xx (``fresh`` 0.5.2), which finds `If-None-Match: *`
    fresh unless a non-empty `If-Modified-Since`, or `no-cache` in `Cache-Control`, goes with it. A
    `HEAD` reaches this as the `GET` it is rewritten to.

    Measured against api.notion.com on 2026-10-27: `If-None-Match: *`, with or without spaces or a
    tab around it, drew a 304 on a `GET` and a `HEAD` of `users/me`, and on a `GET` of `users`,
    `pages/{id}` and `blocks/{id}/children`, carrying the `ETag` and every header the 200 carries
    but `content-type`. Beside `If-Modified-Since` of a past or a future date, or
    `Cache-Control: max-age=0, no-cache` sent as one header or as two, it drew the 200, and beside
    an `If-Modified-Since` of U+00A0 alone or a `Cache-Control` of `no-cache` after a U+00A0 too;
    beside an empty `If-Modified-Since` or one of spaces, or a `Cache-Control` of `NO-CACHE`,
    `max-age=0`, `xno-cache` or `no-cache="set-cookie"`, the 304. A 400, a 404 and a `POST` were
    answered as without it, and so were `"*"`, `W/"0-x", *` and a U+00A0 beside the `*`. No other
    `If-None-Match` can match there, because each body carries a `request_id` minted for it; this
    server's comes from the request (:func:`_request_id`), so its tag repeats, and matching one
    would answer a 304 where real answers the 200. Only `*` is read.
    """
    if request.method != "GET" or not 200 <= status_code < 300:
        return False
    if request.headers.get("if-modified-since", "").strip(" \t"):
        return False
    if _NO_CACHE.search(", ".join(request.headers.getlist("cache-control"))):
        return False
    return request.headers.get("if-none-match", "").strip(" \t") == "*"


# --- OpenAPI enrichment --------------------------------------------------
# GET query params are documented with openapi_extra (merges with path params, no signature
# change); POST bodies (search/query) are read via _json_body, so their shape is documented as a
# requestBody the same way. Response models use extra="allow" to preserve the full field set.
# Error paths return JSONResponse (_error), which FastAPI passes through unfiltered.


class _NLoose(BaseModel):
    model_config = ConfigDict(extra="allow")


class NotionObject(_NLoose):
    object: str
    id: str


class NotionList(_NLoose):
    object: str
    results: list[dict] = []
    has_more: bool = False


_P_PAGINATE = [qp("start_cursor"), qp("page_size", "integer")]
_P_FILTER_PROPERTIES = qp(
    "filter_properties",
    description=(
        "A property id or name; repeat the parameter, or spell it `filter_properties[]` or "
        "`filter_properties[0]`, to list several. A page inside a database comes back with only "
        "the listed properties (none when the value is unknown or empty); a page outside a "
        "database keeps its title whatever is listed."
    ),
)
_P_COMMENTS = [qp("block_id", required=True), *_P_PAGINATE]


def _body(props: dict) -> dict:
    return {
        "requestBody": {
            "content": {"application/json": {"schema": {"type": "object", "properties": props}}}
        }
    }


_B_SEARCH = _body(
    {
        "query": {"type": "string"},
        "filter": {"type": "object"},
        "sort": {"type": "object"},
        "start_cursor": {"type": "string"},
        "page_size": {"type": "integer"},
    }
)


def _version_param(description: str) -> dict:
    """The ``Notion-Version`` header, declared on every operation and required, the way the
    vendor's OpenAPI declares it (all 64 of its operations point at one ``components/parameters``
    entry; read 2026-09-16). Here it is the contract each route enforces (see ``_refusal``), and
    it is the whole of what a generated client knows -- ``backlot mcp`` builds its tools off this
    spec and its bridge sends no version of its own.

    Declared route by route because the two query routes name the versions they serve (see
    ``_query_extra``). The schema is the seven versions the route accepts, not the vendor's enum
    of its current version alone."""
    return {
        "name": "Notion-Version",
        "in": "header",
        "required": True,
        "schema": {"type": "string", "enum": list(PUBLISHED_VERSIONS)},
        "description": description,
    }


_P_VERSION = _version_param(
    "The API version to read this request under. Notion requires it on every request, and refuses "
    "a request that omits it or names a version it does not publish. `2025-09-03` and later read "
    "a database's rows through "
    "`data_sources/{id}/query`; the versions before it read them through `databases/{id}/query`, "
    "and see a database's schema inline instead of a `data_sources` array."
)


def _params(*query_params: dict) -> dict:
    """``openapi_extra`` for a route: the version header every request carries, then whatever
    query parameters the route reads."""
    return {"parameters": [_P_VERSION, *query_params]}


def _query_extra(serves: str, refuses: str) -> dict:
    """``openapi_extra`` for one of the two query routes. ``serves`` names the versions it is the
    path for, ``refuses`` the ones that read rows through the other."""
    return {
        **_body(
            {
                "sorts": {"type": "array", "items": {"type": "object"}},
                "filter": {"type": "object"},
                "start_cursor": {"type": "string"},
                "page_size": {"type": "integer"},
                "result_type": {"type": "string", "enum": ["page", "data_source"]},
            }
        ),
        "parameters": [
            _version_param(
                f"The API version to read this request under. This path serves {serves}; "
                f"{refuses} read a database's rows through the other query path, and a request "
                "under one of them is refused here, as is a request that sends no version."
            ),
            qp(
                "filter_properties",
                description=(
                    "A property id or name; repeat the parameter, or spell it "
                    "`filter_properties[]` or `filter_properties[0]`, to list several. Each row "
                    "comes back with only the listed properties, and a name the data source does "
                    "not have is refused."
                ),
            ),
        ],
    }


_B_QUERY_DATA_SOURCE = _query_extra("2025-09-03 and later", "versions before it")
_B_QUERY_DATABASE = _query_extra("versions up to and including 2022-06-28", "2025-09-03 and later")


# --------------------------------------------------------------------------- helpers


_BEARER_REQUIRED = 'Authorization header must use the format "Bearer <token>".'
_TOKEN_INVALID = "API token is invalid."


def _request_id(request: Request) -> str:
    """The id every Notion answer names itself by, in the body and on the response.

    Measured 2026-09-22 with no credential, which is enough because the URL and the credential are
    checked before anything else: every refusal carries `"request_id": "<uuid>"` and
    `x-notion-request-id` with that same value. Real's is per response — three calls to one URL
    answered three ids — and this one is derived from the method, path and query string instead,
    the choice this repository makes for a synthesised id so that a corpus served twice answers the
    same thing and a test can assert one. Two POSTs that differ only in their body share an id
    here, where real would answer two; the body is left out because a request id is read from a
    log line beside a URL, not recomputed from what was sent.
    """
    raw = hashlib.shake_256(
        f"notion-req:{request.method} {request.url.path}?{request.url.query}".encode()
    )
    return str(uuid.UUID(bytes=raw.digest(16), version=4))


class _NodeJSON(JSONResponse):
    """JSON as Express writes it, which escapes a lone surrogate a refusal's quote can end on
    (``notion_request.quoted``); Starlette's encoder cannot write one at all."""

    def render(self, content) -> bytes:
        return request_body.node_json(content)


def _error(
    request: Request, status: int, code: str, message: str, additional_data: dict | None = None
) -> JSONResponse:
    """Notion's refusal: ``additional_data``, where real sends one, goes ahead of `request_id`, as
    the comments cursor's and the user list's 503 carried it on 2026-10-21."""
    request_id = _request_id(request)
    content = {"object": "error", "status": status, "code": code, "message": message}
    if additional_data is not None:
        content["additional_data"] = additional_data
    content["request_id"] = request_id
    return _NodeJSON(
        status_code=status, content=content, headers={"x-notion-request-id": request_id}
    )


def _bearer_credential(request: Request) -> str | None:
    """The token from `Authorization: Bearer <t>`, and from nothing else.

    `auth.bearer_token` also takes GitHub's legacy `token <t>`, which Notion does not: measured
    2026-09-22, `token <anything>`, `Basic …`, a bare value with no scheme, a `Bearer` with no
    token and no header at all are each refused as a header that is not a bearer credential, where
    `Bearer <a token that does not resolve>` is refused as a token. The header is exactly two
    words: on 2026-09-28 `Bearer a b`, `Bearer a  b` and `Bearer nope x` were told the format, and
    `Bearer  nope` with two spaces, `Bearer` and `nope` with a tab between them, `bearer nope` and
    `BEARER nope` were told the token: two spaces or a tab separate the words as one space does,
    and the scheme is read without its case.
    """
    parts = (request.headers.get("authorization") or "").split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def _version(request: Request) -> str:
    """The caller's ``Notion-Version``. The fallback is unreachable through a route -- every one
    of them runs ``_refusal`` first, which answers a request that sent no version -- and is here
    so a reader of ``_database_obj`` or ``search`` need not prove that to know what the version is
    when the shape is built."""
    return request.headers.get("notion-version") or DATA_SOURCES_VERSION


def _data_sources_model(version: str) -> bool:
    """Whether ``version`` reads a database's rows through a data source.

    2025-09-03 re-organised the ``/v1/databases`` APIs into ``/v1/data_sources`` (for a data
    source) and ``/v1/databases`` (for the container that holds them). Every version before it
    predates data sources entirely, so the comparison is against the date, not against the one
    legacy version a client happens to send: 2022-02-22 has no more idea what a data source is
    than 2022-06-28 does.

    A value Notion does not publish never reaches this comparison -- ``_refusal`` answers it
    ``missing_version``, as api.notion.com does (measured 2026-09-17), so the ordering here is
    total over the seven versions Notion publishes."""
    return version >= DATA_SOURCES_VERSION


def _unknown_version(value: str) -> str:
    """Notion's refusal for a version it does not publish: the enumeration, then the value it was
    handed, quoted as every refusal quotes one (``backlot.notion_request.quoted``)."""
    listed = ", ".join(f'`"{v}"`' for v in PUBLISHED_VERSIONS[:-1])
    return (
        f"Notion-Version header failed validation: Notion-Version header should be {listed}, "
        f'or `"{PUBLISHED_VERSIONS[-1]}"`, instead was `{req.quoted(value)}`.'
    )


def _refusal(request: Request, caller) -> JSONResponse | None:
    """What a request is answered before any route reads a row -- None when it may go on. Every
    route calls this, so the two checks are in one place and in the order the real API applies
    them. Called rather than declared as a router dependency: the credential half has to stay
    where the route resolves the caller it goes on to scope rows by.

    The credential first: on 2026-09-15 an invalid token answered ``unauthorized`` on both query
    paths under 2022-06-28, under 2025-09-03 and with no version header at all, so the version is
    never what a request without a usable credential hears about. Which of that 401's two messages
    it gets is :func:`_bearer_credential`: a header that is not a bearer credential is told the
    format, and a bearer the server cannot resolve is told the token.

    Then the version, which Notion requires on every request and refuses when it is one Notion
    does not publish. Both halves answer ``missing_version``, and each has its own message:
    measured against api.notion.com on 2026-09-17 with an integration token, a version-less
    request is refused on twelve routes out of twelve, and ``banana``, ``2024-01-01`` and
    ``2099-01-01`` are each answered with the enumeration of the seven versions it does publish.
    The absent-header message is a pair of backticks wider than the example Notion's status-code
    table prints for the code, so the table is stale against the service there.

    An empty header value is a value rather than a missing one, and real answers it the
    enumerating way -- so it is refused here too, which is what keeps it from sorting below
    2025-09-03 and quietly picking the legacy model for a caller that named nothing."""
    if _bearer_credential(request) is None:
        return _error(request, 401, "unauthorized", _BEARER_REQUIRED)
    if caller is None:
        # api.notion.com on 2026-10-22 answered a bearer it could not resolve with
        # `additional_data` `{"reason": "token_invalid"}` on users, users/me and search, under
        # 2025-09-03, 2022-06-28 and no version; the format's 401 above carries none.
        return _error(request, 401, "unauthorized", _TOKEN_INVALID, {"reason": "token_invalid"})
    version = request.headers.get("notion-version")
    if version is None:
        return _error(
            request,
            400,
            "missing_version",
            "Notion-Version header failed validation: Notion-Version header should be defined, "
            "instead was `undefined`.",
        )
    if version not in PUBLISHED_VERSIONS:
        return _error(request, 400, "missing_version", _unknown_version(version))
    return None


def _unmounted_here(request: Request, *, data_sources: bool) -> JSONResponse | None:
    """Notion's answer when the route the caller asked for is not mounted under the version it
    sent -- None when it is. Runs after ``_refusal``, so the version read here is one the caller
    sent.

    The two query paths, measured against api.notion.com on 2026-09-11 with an integration token,
    probing each with an id that exists but is a page: 2022-06-28 serves ``databases/{id}/query``
    and answers ``invalid_request_url`` for ``data_sources/{id}/query``, and 2025-09-03 the other
    way round. ``GET /data_sources/{id}`` is gated on the version the same way, measured on
    2026-09-17 with an id that exists and is not a data source: 2021-05-11 and 2022-06-28 answer
    ``invalid_request_url``, 2025-09-03 and 2026-03-11 answer ``object_not_found``, which is the
    route answering. Its counterpart ``GET /databases/{id}`` answered under each version probed
    there -- 2021-05-11, 2022-06-28 and 2026-03-11.

    The 09-11 probe recorded codes rather than bodies, and reported the refusal as "the same as a
    made-up path", so the message here is what api.notion.com answered on 2026-09-15 for a path no
    version mounts at all."""
    if _data_sources_model(_version(request)) is not data_sources:
        return _error(request, 400, "invalid_request_url", "Invalid request URL.")
    return None


def _norm(nid: str) -> str:
    """Notion accepts an id dashed or dashless, and does not fold its case: a dashed id in upper
    case was a 404 on ``pages/{id}`` (measured 2026-09-30) and on ``comments`` (2026-10-02), where
    a dashless one in upper case was not read as a uuid at all. Folding case here serves an id
    real does not. Canonicalize to the dashed lowercase form the stored ``id`` /
    ``data_source_id`` columns actually hold (see ``synth._uuid_from``) rather than to a dashless
    key: a UUID's dashes sit at fixed offsets (8-4-4-4-12), so reconstructing them is
    deterministic, and the column's job is to hold the value the API reports, not a lookup key a
    reader has to rebuild dashes from.

    Malformed input (the wrong length once dashes are stripped) comes back unchanged, which
    matches no stored id. Real refuses a malformed id with a 400 before it looks anything up;
    ``list_comments`` checks its ``block_id`` that way first, and the path routes answer it the
    404 an unknown id gets."""
    h = (nid or "").replace("-", "").lower()
    if len(h) != 32:
        return h
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _existing_id(request: Request, page_id: str) -> str | None:
    """The canonical form of a served page/database/block id, or None if it names nothing -- a
    PRIMARY KEY lookup (see store.notion_by_id). All it does is normalise the spelling and
    confirm a row holds that id.

    Unscoped by ACL on purpose: used only by _query_rows, for the ``databases/{id}/query`` half,
    which needs the id to drive a further, separately ACL-scoped query (store.children, a
    DIFFERENT table) rather than to serve this row itself -- see get_page and friends (and
    list_comments), which read the row directly instead of going through here, so as not to
    resolve it once and refetch it a second time for the ACL check."""
    row = store.notion_by_id(auth.conn(request), _norm(page_id))
    return row["id"] if row is not None else None


def _db_doc_for_data_source(request: Request, dsid: str) -> str | None:
    """The database `id` behind a data source id -- a unique-indexed column lookup (see
    store.notion_by_data_source_id), not the O(pages) scan -- hashing every notion row's data
    source id on every request. Unscoped by ACL for the same reason as
    _existing_id."""
    row = store.notion_by_data_source_id(auth.conn(request), _norm(dsid))
    return row["id"] if row is not None else None


def _list_obj(results: list, next_cursor: str | None, type_key: str) -> dict:
    """A page of a list: `has_more` is whether a cursor follows it."""
    return {
        "object": "list",
        "results": results,
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
        "type": type_key,
        type_key: {},
    }


# --------------------------------------------------------------------------- what a request carries


def _error_id(request: Request) -> str:
    """The `errorId` of a body Express refuses, derived as :func:`_request_id` is but apart from
    it: real's two differ on each such answer."""
    raw = hashlib.shake_256(
        f"notion-error:{request.method} {request.url.path}?{request.url.query}".encode()
    )
    return str(uuid.UUID(bytes=raw.digest(16), version=4))


async def front_door_body(request: Request) -> JSONResponse | None:
    """Read the body as Express reads one ahead of every route (``notion_request.read_body``), and
    answer the refusal it draws, or None with what it read left on the request for
    :func:`_json_body`.

    ``backlot.main`` calls this on every `/notion` request, the root included. A body Express
    refuses is answered in its error handler's shape, an `errorId` and no `request_id` member, with
    `x-notion-request-id` on the response as on every answer; one ``JSON.parse`` refuses is
    `invalid_json` in Notion's own envelope."""
    read = req.read_body(
        await request.body(),
        # Sent more than once, the type reaches Express joined (`application/json, text/plain`), a
        # type its parser cannot read: real served `{"foo":1}` under those two in either order and
        # under `application/json` twice (2026-10-21).
        request_body.joined_header(request.headers.getlist("content-type")),
        request_body.joined_header(request.headers.getlist("content-encoding")),
    )
    if isinstance(read, req.Unreadable):
        content = {
            "isNotionError": False,
            "errorId": _error_id(request),
            "name": read.name,
            "message": read.message,
        }
        if read.status == 413:
            content["code"] = 413
        return JSONResponse(
            status_code=read.status,
            content=content,
            headers={"x-notion-request-id": _request_id(request)},
        )
    if read == "invalid_json":
        return _error(request, 400, "invalid_json", "Error parsing JSON body.")
    request.state.notion_body = read
    return None


def _query(request: Request, schema: req.Obj, *, reads_filter_properties: bool = False):
    """``(values, refusal)``: the query string's members as the validator reads them, or the 400 it
    answers. A key that is not one value is refused first (``notion_request.query_shape_refusal``),
    then the members against ``schema``, the route's own in its order and then each one it does
    not name. `filter_properties` is the list :func:`_filter_properties_keep` reads wherever it is
    sent: under its key in ``values`` on a route that reads it, and quoted back by ``schema`` on
    one that does not (`query.filter_properties should be not present, instead was `["title"]``,
    2026-10-21)."""
    members = req.query_members(request.url.query)
    keep, refused = _filter_properties_keep(request)
    own = {"filter_properties": _NOT_A_LIST if refused else None}
    shape = req.query_shape_refusal(members, own=own)
    if shape is not None:
        return None, _error(request, 400, "validation_error", shape)
    values = {k: req.coerce(m.value) for k, m in members.items() if k not in own}
    if keep is not None and not reads_filter_properties:
        values["filter_properties"] = keep
    problems = schema.check(values, ("query",))
    if problems:
        message = req.refusal_message("query", problems)
        return None, _error(request, 400, "validation_error", message)
    values["filter_properties"] = keep
    return values, None


def _get(request: Request, schema: req.Obj, *, reads_filter_properties: bool = False):
    """``(values, refusal)`` for a `GET`: its query string as :func:`_query` reads it, then a body,
    which no `GET` takes a member of (``notion_request.NOTHING``)."""
    query, refusal = _query(request, schema, reads_filter_properties=reads_filter_properties)
    if refusal is None:
        refusal = _json_body(request, req.NOTHING)[1]
    return query, refusal


def _json_body(request: Request, schema: req.Obj):
    """``(body, refusal)``: the body Express read, or the 400 for one that is not an object or that
    fails ``schema``."""
    body = getattr(request.state, "notion_body", {})
    problems = schema.check(body, ("body",))
    if problems:
        message = req.refusal_message("body", problems)
        return None, _error(request, 400, "validation_error", message)
    return body, None


def _cursor_refusal(request: Request, cursor: str, *, comments: bool = False) -> JSONResponse:
    """The 400 for a `start_cursor` that names no item of the list. `comments` adds the three
    `tool_error_*` members real carried there, and no other route did (2026-10-21)."""
    extra = None
    if comments:
        extra = {
            "tool_error_code": "invalid_cursor",
            "tool_error_class": "validation",
            "tool_error_retryable": "false",
        }
    message = f"The start_cursor provided is invalid: {cursor}"
    return _error(request, 400, "validation_error", message, extra)


def _start(ids: list[str], cursor) -> int | None:
    """Where a page that starts at ``cursor`` starts in ``ids``: at the item it names, which the
    page includes, the first item for no cursor, and None for a cursor naming none of them. Measured
    on `users`, `blocks/{id}/children`, `comments` and `search` (2026-10-21): the second user's,
    the third block's, the second comment's and the third result's id each start the page with that
    item. An id in upper case names nothing, and so does one without its dashes on `users`, the one
    route whose schema lets it through."""
    if cursor is None:
        return 0
    return ids.index(cursor) if cursor in ids else None


def _size(value) -> int | None:
    """The page size a list reads off a `page_size` its schema let through, or None for the empty
    page. Zero, and anything past 100 (`101` and `1e3`), are 100; a negative is the empty page
    (`-1`, `-2` and `-5`); and `search`'s body, which reads a fraction, cuts one toward zero, so
    `1.5` is 1, `2.5` is 2 and `0.5`, `0.9999` and `-0.5` an empty page (2026-10-21)."""
    if value is None or value == 0:
        return _PAGE_MAX
    if value < 0:
        return None
    return int(min(value, _PAGE_MAX))


def _paged(items: list, ids: list[str], start: int, size: int | None):
    """The slice of ``items`` a page shows from ``start``, and the cursor after it: the id of the
    next item, the cursor real hands back. The empty page (``size`` None) names the item it would
    have started with, which is the first item, or the one the cursor named
    (`users?page_size=-1&start_cursor=<the bot>` named the bot)."""
    if size is None:
        return [], ids[start] if start < len(ids) else None
    end = start + size
    return items[start:end], ids[end] if end < len(ids) else None


# The objects below carry real's members in real's order, as api.notion.com answered one object of
# each kind on 2026-10-20 under 2025-09-03, and under 2022-06-28 too for a page, a row, the block of
# each and a database: a child block, a data source, the bot and a person beside those.


def _partial_user(email: str) -> dict:
    """The user member real puts on `created_by` / `last_edited_by`: `object` and `id` only.

    Measured against api.notion.com on 2026-09-30: a page, a row, a block, a database and a data
    source answer this two-member user there, while `GET /v1/users/{id}` keeps the full person
    `_user_obj` renders.
    """
    return {"object": "user", "id": synth.notion_user_id(email or "")}


def _object_url(object_id: str) -> str:
    """The `url` real serves for a page or a database (measured 2026-09-30): `app.notion.com/p/`
    and the id without its dashes. A data source answers its database's."""
    return f"https://app.notion.com/p/{object_id.replace('-', '')}"


def _workspace_id(name: str) -> str:
    """A stable dashed uuid for one workspace, derived from its name the way the other served
    ids are (the same corpus answers the same id)."""
    raw = hashlib.shake_256(f"notion-workspace:{name}".encode()).digest(16)
    return str(uuid.UUID(bytes=raw, version=4))


def _answer(request: Request, payload: dict) -> JSONResponse:
    """A 200 in Notion's envelope: `request_id` as the last body member and on the header.

    Measured 2026-09-30 and 2026-10-20: the 200 of each route this router serves, list bodies
    included, carries one id in `request_id` and in `x-notion-request-id`, the way `_error`
    answers a refusal.
    """
    request_id = _request_id(request)
    body = dict(payload)
    body["request_id"] = request_id
    return JSONResponse(content=body, headers={"x-notion-request-id": request_id})


def _user_obj(conn, email: str) -> dict:
    u = store.get_user(conn, email)
    name = u["display_name"] if u else (email.split("@")[0] if email else "Unknown")
    return {
        "object": "user",
        "id": synth.notion_user_id(email or ""),
        "name": name,
        "avatar_url": None,
        "type": "person",
        # Real's person member is `email` then `email_verified` under each Notion-Version, the
        # latter `true` for the one person measured (2026-10-13). The corpus records no
        # verification, so every person is served `true`.
        "person": {"email": email, "email_verified": True},
    }


def _emoji_icon(icon: str | None) -> dict | None:
    if not icon:
        return None
    if icon.startswith("http"):
        return {"type": "external", "external": {"url": icon}}
    return {"type": "emoji", "emoji": icon}


def _cover(url: str | None) -> dict | None:
    return {"type": "external", "external": {"url": url}} if url else None


def _parent_field(conn, row, version: str | None = None) -> dict:
    """The `parent` a page and its block serve, in the model the caller's version reads.

    `parent_id` already HOLDS the parent's served id -- the importer resolved it there, and
    hashing it again would name a page nothing serves. On 2026-10-20 api.notion.com typed the parent
    the same way for the page and for its block: a page under a page `page_id` under 2022-06-28 and
    2025-09-03, and a row `database_id` under 2022-06-28 and `data_source_id`, with the database id
    beside it, under 2025-09-03.
    """
    if not row["parent_id"]:
        return {"type": "workspace", "workspace": True}
    parent = store.get_document(conn, "notion", row["parent_id"])
    if parent["subtype"] != "database":
        return {"type": "page_id", "page_id": row["parent_id"]}
    if version and _data_sources_model(version):
        return {
            "type": "data_source_id",
            "data_source_id": parent["data_source_id"],
            "database_id": row["parent_id"],
        }
    return {"type": "database_id", "database_id": row["parent_id"]}


def _database_parent(row) -> dict:
    """A database's own `parent`. This router serves each database at the top of the workspace."""
    return {"type": "workspace", "workspace": True}


def _title_prop(title: str) -> dict:
    return {"id": "title", "type": "title", "title": synth.notion_rich_text(title)}


def _prop_value(name: str, value) -> dict:
    """A Notion property *value* object (on a database row)."""
    if isinstance(value, bool):
        return {"id": name, "type": "checkbox", "checkbox": value}
    if isinstance(value, (int, float)):
        return {"id": name, "type": "number", "number": value}
    if isinstance(value, str):
        return {"id": name, "type": "select", "select": {"name": value, "color": "default"}}
    return {"id": name, "type": "rich_text", "rich_text": synth.notion_rich_text(str(value))}


def _page_obj(conn, row, version: str | None = None) -> dict:
    pid = row["id"]
    created = row["created_ts"]
    updated = row["updated_ts"] or created
    props = {"title": _title_prop(row["title"])}
    for k, v in (json.loads(row["properties"]) if row["properties"] else {}).items():
        props[k] = _prop_value(k, v)
    # Both timestamps in the millisecond form real sends for the page and for its block.
    return {
        "object": "page",
        "id": pid,
        "created_time": synth.rfc3339_millis(created),
        "last_edited_time": synth.rfc3339_millis(updated),
        "created_by": _partial_user(row["author_email"]),
        "last_edited_by": _partial_user(row["author_email"]),
        "cover": _cover(row["cover"]),
        "icon": _emoji_icon(row["icon"]),
        "parent": _parent_field(conn, row, version),
        "in_trash": False,
        "is_archived": False,
        "is_locked": False,
        "properties": props,
        "url": _object_url(pid),
        "public_url": None,
        "archived": False,
    }


_FILTER_PROPERTIES_KEY = re.compile(r"filter_properties((?:\[[^\[\]]*\])*)")
_BRACKET = re.compile(r"\[([^\[\]]*)\]")


_NOT_A_LIST = (
    'The value of "filter_properties" in query parameters must be a string or list of strings'
)


def _filter_properties_keep(request: Request) -> tuple[list[str] | None, bool]:
    """``(names, refused)``: the ``filter_properties`` names, ``None`` when the parameter is
    absent, and whether real refuses it with :data:`_NOT_A_LIST`. Measured against api.notion.com
    (2022-06-28 and 2025-09-03) on 2026-10-15, which reads the query string the way Node's ``qs``
    does: the bare key, ``[]`` and an index from 0 to 20 all add to one list; any other single
    bracket (``[x]``, ``[21]``, ``[01]``) is a 400 unless the bare key came first, and then lists
    nothing; a deeper bracket (``[0][x]``) or an index given twice lists nothing. A bare
    ``[][]=Tags`` kept ``Tags`` on real and keeps nothing here."""
    keep: list[str] = []
    slots: dict[str, list[str]] = {}
    first = None
    refused = False
    for key, value in request.query_params.multi_items():
        if (m := _FILTER_PROPERTIES_KEY.fullmatch(key)) is None:
            continue
        brackets = _BRACKET.findall(m[1])
        first = brackets if first is None else first
        if brackets in ([], [""]):
            keep.append(value)
        elif len(brackets) > 1:
            continue
        elif (i := brackets[0]).isascii() and i.isdigit() and str(int(i)) == i and int(i) <= 20:
            slots.setdefault(i, []).append(value)
        else:
            refused = first != []
    names = keep + [values[0] for values in slots.values() if len(values) == 1]
    return (names if first is not None else None), refused


def _filter_properties(page: dict, keep: list[str] | None) -> dict:
    """Narrow ``properties`` to those whose id or name is in ``keep``; ``None`` leaves the page
    as it is."""
    if keep is None:
        return page
    wanted = set(keep)
    page["properties"] = {
        k: v for k, v in page["properties"].items() if k in wanted or v["id"] in wanted
    }
    return page


def _schema_props(row) -> dict:
    """A Notion database property *schema* (on a database / data source)."""
    schema = json.loads(row["properties"]) if row["properties"] else {}
    out = {"title": {"id": "title", "name": "Name", "type": "title", "title": {}}}
    for k, v in schema.items():
        t = v.get("type", "rich_text") if isinstance(v, dict) else "rich_text"
        out[k] = {"id": k, "name": k, "type": t, t: {}}
    return out


def _database_obj(conn, row, version: str) -> dict:
    """A database as 2025-09-03 and later answer it, with its data sources, `is_locked` and no
    author, or as earlier versions do, with the property schema, both authors and no
    `is_locked`."""
    did = row["id"]
    created = row["created_ts"]
    updated = row["updated_ts"] or created
    if _data_sources_model(version):
        return {
            "object": "database",
            "id": did,
            "title": synth.notion_rich_text(row["title"]),
            "description": synth.notion_rich_text(row["content"]),
            "parent": _database_parent(row),
            "is_inline": False,
            "database_type": None,
            "in_trash": False,
            "is_locked": False,
            "created_time": synth.rfc3339(created),
            "last_edited_time": synth.rfc3339(updated),
            "data_sources": [{"id": row["data_source_id"], "name": row["title"]}],
            "icon": _emoji_icon(row["icon"]),
            "cover": _cover(row["cover"]),
            "url": _object_url(did),
            "public_url": None,
            "archived": False,
        }
    return {
        "object": "database",
        "id": did,
        "cover": _cover(row["cover"]),
        "icon": _emoji_icon(row["icon"]),
        "created_time": synth.rfc3339(created),
        "created_by": _partial_user(row["author_email"]),
        "last_edited_by": _partial_user(row["author_email"]),
        "last_edited_time": synth.rfc3339(updated),
        "title": synth.notion_rich_text(row["title"]),
        "description": synth.notion_rich_text(row["content"]),
        "is_inline": False,
        "database_type": None,
        "properties": _schema_props(row),
        "parent": _database_parent(row),
        "url": _object_url(did),
        "public_url": None,
        "in_trash": False,
        "archived": False,
    }


def _data_source_obj(conn, row) -> dict:
    """The data source `GET /v1/data_sources/{id}` answers: the database as `parent`, the
    database's own parent as `database_parent`, the database's URL as `url`, and the title as
    `title`, with no `name`."""
    created = row["created_ts"]
    updated = row["updated_ts"] or created
    return {
        "object": "data_source",
        "id": row["data_source_id"],
        "cover": _cover(row["cover"]),
        "icon": _emoji_icon(row["icon"]),
        "created_time": synth.rfc3339_millis(created),
        "created_by": _partial_user(row["author_email"]),
        "last_edited_by": _partial_user(row["author_email"]),
        "last_edited_time": synth.rfc3339_millis(updated),
        "title": synth.notion_rich_text(row["title"]),
        "description": synth.notion_rich_text(row["content"]),
        "is_inline": False,
        "database_type": None,
        "properties": _schema_props(row),
        "parent": {"type": "database_id", "database_id": row["id"]},
        "database_parent": _database_parent(row),
        "url": _object_url(row["id"]),
        "public_url": None,
        "in_trash": False,
        "archived": False,
    }


# --------------------------------------------------------------------------- pages / blocks


def _block_child(conn, page_row, block: dict) -> dict:
    """A block of `blocks.children` in the twelve members real serves (measured 2026-09-30): the
    seven `synth.notion_blocks` emits plus the page parent, both timestamps in the millisecond form
    and the two partial authors."""
    created = page_row["created_ts"]
    updated = page_row["updated_ts"] or created
    kind = block["type"]
    return {
        "object": "block",
        "id": block["id"],
        "parent": {"type": "page_id", "page_id": page_row["id"]},
        "created_time": synth.rfc3339_millis(created),
        "last_edited_time": synth.rfc3339_millis(updated),
        "created_by": _partial_user(page_row["author_email"]),
        "last_edited_by": _partial_user(page_row["author_email"]),
        "has_children": block["has_children"],
        "in_trash": block["in_trash"],
        "type": kind,
        kind: block[kind],
        "archived": block["archived"],
    }


@router.get("/pages/{page_id}", response_model=NotionObject, openapi_extra=_params())
async def get_page(page_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    if (refusal := _get(request, req.PAGE_QUERY, reads_filter_properties=True)[1]) is not None:
        return refusal
    conn = auth.conn(request)
    # One ACL-scoped query, not a resolve followed by a get_document refetch of the same row:
    # `id` is the PRIMARY KEY, so the ACL clause can only narrow "found" to "not found", never
    # redirect to a different row (see store.notion_by_id).
    row = store.notion_by_id(conn, _norm(page_id), auth.visible_ids(request, caller))
    if row is None or row["subtype"] == "database":
        return _error(request, 404, "object_not_found", f"Could not find page with ID: {page_id}.")
    return _answer(request, _page_obj(conn, row, _version(request)))


@router.get("/blocks/{block_id}", response_model=NotionObject, openapi_extra=_params())
async def get_block(block_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    if (refusal := _get(request, req.NOTHING)[1]) is not None:
        return refusal
    conn = auth.conn(request)
    row = store.notion_by_id(conn, _norm(block_id), auth.visible_ids(request, caller))
    if row is None:
        return _error(
            request, 404, "object_not_found", f"Could not find block with ID: {block_id}."
        )
    bid = row["id"]
    kind = "child_database" if row["subtype"] == "database" else "child_page"
    return _answer(
        request,
        {
            "object": "block",
            "id": bid,
            "parent": _parent_field(conn, row, _version(request)),
            "created_time": synth.rfc3339_millis(row["created_ts"]),
            "last_edited_time": synth.rfc3339_millis(row["updated_ts"] or row["created_ts"]),
            "created_by": _partial_user(row["author_email"]),
            "last_edited_by": _partial_user(row["author_email"]),
            "has_children": row["subtype"] != "database",
            "in_trash": False,
            "type": kind,
            kind: {"title": row["title"]},
            "archived": False,
        },
    )


@router.get(
    "/blocks/{block_id}/children",
    response_model=NotionList,
    openapi_extra=_params(*_P_PAGINATE),
)
async def get_block_children(block_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    # The query string is read before the block is looked up, a cursor that names no block after
    # (2026-10-21: an unknown block with `start_cursor=bogus` is the shape's 400, with a uuid
    # that names nothing the 404).
    query, refusal = _get(request, req.CHILDREN_QUERY)
    if refusal is not None:
        return refusal
    conn = auth.conn(request)
    row = store.notion_by_id(conn, _norm(block_id), auth.visible_ids(request, caller))
    if row is None:
        return _error(
            request, 404, "object_not_found", f"Could not find block with ID: {block_id}."
        )
    # A database's rows are pages reached through the query route, not blocks: on api.notion.com
    # (2026-10-04, three databases, Notion-Version 2022-06-28 and 2025-09-03) its children are the
    # empty list, and its block reports `has_children` false.
    blocks = [] if row["subtype"] == "database" else synth.notion_blocks(row["id"], row["content"])
    ids = [b["id"] for b in blocks]
    start = _start(ids, query.get("start_cursor"))
    if start is None:
        return _cursor_refusal(request, query["start_cursor"])
    page, after = _paged(blocks, ids, start, _size(query.get("page_size")))
    return _answer(request, _list_obj([_block_child(conn, row, b) for b in page], after, "block"))


# --------------------------------------------------------------------------- databases / data sources


@router.get("/databases/{database_id}", response_model=NotionObject, openapi_extra=_params())
async def get_database(database_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    if (refusal := _get(request, req.NOTHING)[1]) is not None:
        return refusal
    conn = auth.conn(request)
    row = store.notion_by_id(conn, _norm(database_id), auth.visible_ids(request, caller))
    if row is None or row["subtype"] != "database":
        return _error(
            request, 404, "object_not_found", f"Could not find database with ID: {database_id}."
        )
    return _answer(request, _database_obj(conn, row, _version(request)))


@router.get("/data_sources/{data_source_id}", response_model=NotionObject, openapi_extra=_params())
async def get_data_source(data_source_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    if (refusal := _unmounted_here(request, data_sources=True)) is not None:
        return refusal
    if (refusal := _get(request, req.NOTHING)[1]) is not None:
        return refusal
    conn = auth.conn(request)
    # No subtype check needed here, unlike get_database's lookup by `id` (which spans both pages
    # and databases): served_data_source_id is populated only for subtype='database' rows, and the
    # importer writes it or NULL on every import (see the notion block in importer.byo._Loader.add).
    # So a match here already implies the right kind.
    row = store.notion_by_data_source_id(
        conn, _norm(data_source_id), auth.visible_ids(request, caller)
    )
    if row is None:
        return _error(
            request,
            404,
            "object_not_found",
            f"Could not find data source with ID: {data_source_id}.",
        )
    return _answer(request, _data_source_obj(conn, row))


def _query_session(db) -> str:
    """The session a first page's `next_cursor` names, `s:<session>:<the next row's id>`. Real's
    is random per first page and a later page carries the one it was handed; this one is derived
    from the data source, so a corpus served twice pages the same way."""
    raw = hashlib.shake_256(f"notion-query-session:{db['data_source_id']}".encode())
    return str(uuid.UUID(bytes=raw.digest(16), version=4))


_SESSION_CURSOR = re.compile(r"s:([^:]+):(.+)", re.DOTALL)


def _row_start(ids: list[str], cursor: str | None, issued: str):
    """``(start, session, named)`` for a data source query's `start_cursor`: the index the page
    starts at (None to refuse it), the session the next cursor carries (None for a cursor that was
    a row's id, whose next cursor is the next row's id), and what a refusal names.

    Measured 2026-10-21. No cursor and an empty one are the first page. `s:<session>:<row>` starts
    at that row and carries the session. A session real handed out reads only a row's dashed id,
    and names anything else (`s:<issued>:y` is `invalid: y`); one it never did also reads the id
    without dashes in lower case, and is the first page for anything else (`s:<fresh>:y`,
    `s:<fresh>:b:<row>`). `s:`, `s:x:` and `s::<row>` are not that shape, and with any other text
    the cursor is a row's dashed id or refused whole."""
    if not cursor:
        return 0, issued, None
    shaped = _SESSION_CURSOR.fullmatch(cursor)
    if shaped is None:
        return _start(ids, cursor), None, cursor
    session, row = shaped.groups()
    start = _start(ids, row)
    if session != issued and re.fullmatch("[0-9a-f]{32}", row):
        start = _start(ids, _norm(row))
    if start is None and session != issued:
        start = 0
    return start, session, row


def _property_names(db) -> set[str]:
    """What a query may name a property by: its name or its id."""
    return {name for k, v in _schema_props(db).items() for name in (k, v["id"], v["name"])}


def _unknown_filter_property(flt, names: set[str]) -> str | None:
    """The first `property` a filter names that the data source does not have, depth first and
    `and` before `or` whichever is sent first; a compound's own `property` is read by nothing
    (`{"and":[],"property":"Nope"}` is served). Measured 2026-10-21 with an unknown property under
    each of `and` and `or`, sent in both orders: the one under `and` is named."""
    if not isinstance(flt, dict):
        return None
    if "and" in flt or "or" in flt:
        for each in [*(flt.get("and") or []), *(flt.get("or") or [])]:
            if (found := _unknown_filter_property(each, names)) is not None:
                return found
        return None
    prop = flt.get("property")
    return None if prop in names else prop


async def _query_rows(request: Request, row_id: str, *, data_sources: bool):
    """The rows of one database, reached by whichever of the two query paths the caller's version
    serves. ``row_id`` is the id as spelled in the path -- a data source's under ``data_sources``,
    the database's own under ``databases`` -- and is resolved only once the credential and the
    version have both been accepted, so a refused request never runs a lookup.

    The query string and the body are refused ahead of the lookup, and what names a property or a
    row after it, in this order (2026-10-21): an unknown data source with `{"sorts":[{}]}` or
    `{"page_size":0}` is the 400, with `{"start_cursor":"bogus"}` or a filter on `Nope` the 404;
    a sort on an unknown property comes ahead of a filter on one, a filter's ahead of a cursor,
    and a cursor ahead of a `filter_properties` naming nothing. Neither `filter` nor `sorts` is
    applied to the rows."""
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    if (refusal := _unmounted_here(request, data_sources=data_sources)) is not None:
        return refusal
    query, refusal = _query(request, req.NOTHING, reads_filter_properties=True)
    if refusal is not None:
        return refusal
    keep = query.get("filter_properties")
    body, refusal = _json_body(request, req.QUERY_BODY)
    if refusal is not None:
        return refusal
    db_id = (
        _db_doc_for_data_source(request, row_id) if data_sources else _existing_id(request, row_id)
    )
    conn = auth.conn(request)
    visible = auth.visible_ids(request, caller)
    db = store.get_document(conn, "notion", db_id, visible_ids=visible) if db_id else None
    if db is None or db["subtype"] != "database":
        return _error(request, 404, "object_not_found", "Could not find the requested database.")
    names = _property_names(db)
    for sort in body.get("sorts", []):
        if "property" in sort and sort["property"] not in names:
            message = f"Could not find sort property with name or id: {sort['property']}"
            return _error(request, 400, "validation_error", message)
    if (missing := _unknown_filter_property(body.get("filter"), names)) is not None:
        message = f"Could not find property with name or id: {missing}"
        return _error(request, 400, "validation_error", message)
    rows = store.children(conn, "notion", db_id, visible, limit=-1)
    if body.get("result_type") == "data_source":
        rows = []  # a database here holds pages, never a data source
    ids = [r["id"] for r in rows]
    start, session, named = _row_start(ids, body.get("start_cursor"), _query_session(db))
    if start is None:
        return _cursor_refusal(request, named)
    # An empty name is no property's, and refused by none: `filter_properties=` lists nothing.
    for name in keep or []:
        if name and name not in names:
            return _error(
                request,
                400,
                "validation_error",
                "The schema for the data source with the following ID is malformed: "
                f"{db['data_source_id']}. The provided `filter_properties` contains an invalid "
                f"attribute: {name}.",
            )
    page, after = _paged(rows, ids, start, _size(body.get("page_size")))
    if after is not None and session is not None:
        after = f"s:{session}:{after}"
    version = _version(request)
    results = [_filter_properties(_page_obj(conn, r, version), keep) for r in page]
    trailer = "page_or_data_source" if _data_sources_model(version) else "page_or_database"
    return _answer(request, _list_obj(results, after, trailer))


@router.post(
    "/data_sources/{data_source_id}/query",
    response_model=NotionList,
    openapi_extra=_B_QUERY_DATA_SOURCE,
)
async def query_data_source(data_source_id: str, request: Request):
    return await _query_rows(request, data_source_id, data_sources=True)


@router.post(
    "/databases/{database_id}/query", response_model=NotionList, openapi_extra=_B_QUERY_DATABASE
)
async def query_database(database_id: str, request: Request):
    return await _query_rows(request, database_id, data_sources=False)


# --------------------------------------------------------------------------- search

#: How many documents a page reads past its own size before it reads them all, where a filter on
#: the object type can leave a window of the corpus short of a page.
_OVERFETCH = 4


@router.post(
    "/search",
    response_model=NotionList,
    openapi_extra={**_B_SEARCH, **_params(_P_FILTER_PROPERTIES)},
)
async def search(request: Request):
    """Pages and data sources (databases before 2025-09-03) the caller can see."""
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    query, refusal = _query(request, req.NOTHING, reads_filter_properties=True)
    if refusal is not None:
        return refusal
    keep = query.get("filter_properties")
    version = _version(request)
    body, refusal = _json_body(request, req.search_body(legacy=not _data_sources_model(version)))
    if refusal is not None:
        return refusal
    if "sort" in body and (said := req.search_sort_refusal(body["sort"])) is not None:
        return _error(request, 400, "validation_error", said)
    conn = auth.conn(request)
    visible = auth.visible_ids(request, caller)
    # Trimmed as JavaScript trims: a `query` of U+3000, U+2028 or U+FEFF was every result, and one
    # of U+001C, U+0085 or U+200B none (2026-10-21).
    text = body.get("query", "").strip(req.JS_SPACE)
    flt = body.get("filter", {})
    # `page`, `data_source` or None; 2022-06-28's `database` asks for what `data_source` does.
    want = {"database": "data_source"}.get(flt.get("value"), flt.get("value"))
    # A `data_source` filter keeps the database rows. That filter, an unfiltered search and a
    # `query` list each database as a database before 2025-09-03 and as its data source from it,
    # in the shape `GET /v1/data_sources/{id}` answers less its `request_id`. Real answered the
    # filter with the three databases under 2022-06-28 and their three data sources under
    # 2025-09-03 and 2026-03-11 (2026-10-14), `{}` the same beside the pages (2026-10-14 and
    # 2026-10-17), and a `query` naming one database's title with that database's data source
    # under 2025-09-03 (2026-10-17).
    render_data_source = _data_sources_model(version) and want in (None, "data_source")
    cursor = body.get("start_cursor")
    size = _size(body.get("page_size"))

    def read(window: int) -> tuple[list, bool]:
        """The results among the first ``window`` documents, and whether there were that many."""
        if text:
            rows = store.search_documents(conn, text, "notion", visible, limit=window, offset=0)
        else:
            rows = store.list_documents(conn, "notion", visible_ids=visible, limit=window, offset=0)
        picked = [
            r
            for r in rows
            if not flt.get("in_trash")
            and not (want == "data_source" and r["subtype"] != "database")
            and not (want == "page" and r["subtype"] == "database")
        ]
        return picked, window != -1 and len(rows) == window

    # A page and the result after it, read off a window of the corpus where that is enough; a
    # cursor, which can name any result, reads the whole of it.
    picked, cut = read(-1 if cursor else ((size or 1) + 1) * _OVERFETCH)
    if cut and len(picked) <= (size or 0):
        picked, cut = read(-1)
    served = [
        r["data_source_id"] if r["subtype"] == "database" and render_data_source else r["id"]
        for r in picked
    ]
    start = _start(served, cursor)
    if start is None:
        # 2022-06-28 starts at a database named by its data source's id too, the id 2025-09-03
        # serves it by (2026-10-21).
        start = _start([r["data_source_id"] for r in picked], cursor)
    if start is None:
        return _cursor_refusal(request, cursor)
    shown, after = _paged(picked, served, start, size)
    # A database row is narrowed under every Notion-Version, its parent typed `database_id` or
    # `data_source_id`; a page outside a database keeps its title whatever is listed.
    results = [
        _data_source_obj(conn, r)
        if r["subtype"] == "database" and render_data_source
        else _database_obj(conn, r, version)
        if r["subtype"] == "database"
        else _filter_properties(_page_obj(conn, r, version), keep if r["parent_id"] else None)
        for r in shown
    ]
    trailer = "page_or_data_source" if _data_sources_model(version) else "page_or_database"
    return _answer(request, _list_obj(results, after, trailer))


# --------------------------------------------------------------------------- users


@router.get("/users", response_model=NotionList, openapi_extra=_params(*_P_PAGINATE))
async def list_users(request: Request):
    """The workspace's users. A `page_size` of -2 or below is real's 503, which it answered after
    reading the cursor (`page_size=-2&start_cursor=bogus` is the cursor's 400) and on each of
    -2, -5 and -100 (2026-09-30 and 2026-10-21); -1 is the empty page, as on every list."""
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    query, refusal = _get(request, req.USERS_QUERY)
    if refusal is not None:
        return refusal
    conn = auth.conn(request)
    users = store.list_users(conn)
    ids = [synth.notion_user_id(u["email"]) for u in users]
    start = _start(ids, query.get("start_cursor"))
    if start is None:
        return _cursor_refusal(request, query["start_cursor"])
    if query.get("page_size", 0) <= -2:
        return _error(
            request,
            503,
            "service_unavailable",
            "Public API service is temporarily unavailable, please try again later.",
            {"endpoint_name": "public_listUsers", "notion_error_name": "DatastoreInfraError"},
        )
    page, after = _paged(users, ids, start, _size(query.get("page_size")))
    results = [_user_obj(conn, u["email"]) for u in page]
    return _answer(request, _list_obj(results, after, "user"))


@router.get("/users/me", response_model=NotionObject, openapi_extra=_params())
async def get_me(request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    if (refusal := _get(request, req.NOTHING)[1]) is not None:
        return refusal
    conn = auth.conn(request)
    if caller.email:  # a user token → that person
        return _answer(request, _user_obj(conn, caller.email))
    # admin/service token → the integration's bot user
    return _answer(
        request,
        {
            "object": "user",
            "id": synth.notion_user_id("bot:backlot"),
            "name": "backlot",
            "avatar_url": None,
            "type": "bot",
            "bot": {
                "owner": {"type": "workspace", "workspace": True},
                "workspace_name": auth.acl(request).org_name,
                "workspace_id": _workspace_id(auth.acl(request).org_name),
                # 5242880 on the free workspace measured on 2026-10-20.
                "workspace_limits": {"max_file_upload_size_in_bytes": 5 * 1024 * 1024},
            },
        },
    )


@router.get("/users/{user_id}", response_model=NotionObject, openapi_extra=_params())
async def get_user(user_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    if (refusal := _get(request, req.NOTHING)[1]) is not None:
        return refusal
    conn = auth.conn(request)
    key = _norm(user_id)
    for u in store.list_users(conn):
        if _norm(synth.notion_user_id(u["email"])) == key:
            return _answer(request, _user_obj(conn, u["email"]))
    return _error(request, 404, "object_not_found", f"Could not find user with ID: {user_id}.")


# --------------------------------------------------------------------------- comments


@router.get("/comments", response_model=NotionList, openapi_extra=_params(*_P_COMMENTS))
async def list_comments(request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    # Real reads `block_id`, then the cursor's shape and the page size, before it looks the id up
    # (``notion_request.COMMENTS_QUERY``), answers a database's id as restricted, and serves a key
    # it does not read. A cursor that names no comment is refused after the lookup.
    query, refusal = _get(request, req.COMMENTS_QUERY)
    if refusal is not None:
        return refusal
    conn = auth.conn(request)
    block_id = query["block_id"]
    # One ACL-scoped query (the parent must itself be visible to the caller), not
    # a resolve followed by a get_document refetch of the same row -- see get_page and friends.
    row = store.notion_by_id(conn, _norm(block_id), auth.visible_ids(request, caller))
    if row is None:
        # A block is not a row: it is one of the blocks `blocks/{id}/children` derives for a page,
        # and Backlot keeps no comment on one, so a block of a page the caller can see is the empty
        # list real answered on 2026-10-02.
        visible = auth.visible_ids(request, caller)
        wanted = _norm(block_id)
        for page in store.list_documents(conn, "notion", visible_ids=visible, limit=-1):
            if page["subtype"] != "database" and any(
                b["id"] == wanted for b in synth.notion_blocks(page["id"], page["content"])
            ):
                if query.get("start_cursor") is not None:
                    return _cursor_refusal(request, query["start_cursor"], comments=True)
                return _answer(request, _list_obj([], None, "comment"))
        return _error(
            request, 404, "object_not_found", f"Could not find block with ID: {block_id}."
        )
    if row["subtype"] == "database":
        return _error(
            request,
            403,
            "restricted_resource",
            "This object is managed by Notion and isn’t accessible via MCP",
        )
    parent_id = row["id"]
    comments = store.doc_comments(conn, "notion", parent_id)
    ids = [synth.notion_id(c["id"]) for c in comments]
    start = _start(ids, query.get("start_cursor"))
    if start is None:
        return _cursor_refusal(request, query["start_cursor"], comments=True)
    page, after = _paged(comments, ids, start, _size(query.get("page_size")))
    results = [
        {
            "object": "comment",
            "id": synth.notion_id(c["id"]),
            "parent": {"type": "page_id", "page_id": parent_id},
            "discussion_id": synth.notion_id(f"disc:{row['id']}"),
            "created_time": synth.rfc3339(c["created_ts"]),
            "last_edited_time": synth.rfc3339(c["created_ts"]),
            "created_by": _user_obj(conn, c["author_email"]),
            "rich_text": synth.notion_rich_text(c["body"]),
        }
        for c in page
    ]
    return _answer(request, _list_obj(results, after, "comment"))


# --------------------------------------------------------------------------- misc


# The operations Notion publishes and no route here serves (see ``backlot.published``); a route
# added here wins over ``unmatched_path`` whatever the baseline says.
_PUBLISHED_NOT_SERVED = published.not_served("notion")
# The three operations Notion's document puts behind `basicAuth` rather than the `bearerAuth`
# every other operation inherits (read 2026-09-28): its OAuth client endpoints.
_CLIENT_AUTH_OPERATIONS = frozenset(
    {("POST", "/v1/oauth/token"), ("POST", "/v1/oauth/introspect"), ("POST", "/v1/oauth/revoke")}
)


def _invalid_client(request: Request) -> JSONResponse:
    """An OAuth client endpoint's refusal, in its own shape rather than Notion's envelope.

    Measured 2026-09-28: `POST /v1/oauth/token`, `/v1/oauth/introspect` and `/v1/oauth/revoke`
    each answered 401 `{"error":"invalid_client","request_id":"<uuid>"}` with the id in
    `x-notion-request-id` too and `WWW-Authenticate: Basic realm="OAuth"`, with no header, with
    `Bearer nope` and with `Basic YTpi`. Backlot registers no OAuth client, so there is no header
    this answers any other way."""
    request_id = _request_id(request)
    return JSONResponse(
        status_code=401,
        content={"error": "invalid_client", "request_id": request_id},
        headers={"x-notion-request-id": request_id, "www-authenticate": 'Basic realm="OAuth"'},
    )


#: The methods the catch-all below takes; :func:`unmatched_path` has why these six.
ROUTED_METHODS = ("GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS")


@unmatched_router.api_route("/{rest:path}", methods=list(ROUTED_METHODS))
async def unmatched_path(request: Request, rest: str) -> JSONResponse:
    """Notion's answer for a URL no route here serves: the credential's refusal where Notion
    publishes the operation, and otherwise the URL's 400, which it gives before it reads anything
    else.

    Measured 2026-09-22 with no credential at all: `/v1/nonexistent_thing/xyz` answers 400
    `invalid_request_url` with `Invalid request URL.`, and so do the same path carrying a token
    that cannot resolve, the same path with no `Notion-Version`, a path outside `/v1` entirely, and
    a POST to an unknown `/v1` path. A path that does exist answers the credential's 401 instead,
    so the URL is the first thing that API checks.

    A published operation exists for that check whether or not Backlot serves it. Of the
    operations the baseline lists as ``missing_operation`` (see ``backlot.published``), the 53 it
    listed on 2026-09-28 each answered the format's 401 with no header and the token's 401 with
    `Bearer nope`, the two a served route answers, apart from the three OAuth client endpoints and
    their own refusal (see :func:`_invalid_client`); `POST /v1/ai/skills/{id}/upload` answered the
    same two on 2026-10-22, where `GET` on that path was the URL's 400. A request that clears the
    credential and the version on one of them but those three gets the URL's 400 here, as Backlot
    has no operation to run: that is the gap each of those operations acknowledges.

    The method is part of the URL that check reads: `GET /v1/search` and `GET /v1/pages`, both POST
    routes, `DELETE`, `PUT`, `PATCH` and `OPTIONS` on `/v1/users/me`, a GET route, and `PUT` on
    `/v1/pages/{id}`, which Notion publishes for `GET` and `PATCH`, are each that same 400 rather
    than a 405 — which is why this route takes the six methods below rather than the ones the
    routes above happen to declare, and why no `method_not_allowed` envelope is needed for this
    vendor. `TRACE` is left off: real refuses it at the front door with Cloudflare's own `405 Not
    Allowed` HTML page, never reaching the API, so leaving it unrouted keeps the framework's 405
    here rather than trading it for a 400. An invented method such as `FOO` is a bare 501 there,
    and over uvicorn it never reaches this app: the server's parser answers it 400 `Invalid HTTP
    request received.` (both measured 2026-09-28). A `HEAD` arrives as the GET it is rewritten to
    (`backlot.main.answer_head_as_the_get_without_its_body`), and one trailing slash is dropped
    before routing, so a slashed spelling of a served path does not land here
    (`backlot.main.serve_a_slashed_notion_path_as_the_path_without_it`).

    Not reproduced here: real's own root, `/`, a 302 to the marketing site, a page Backlot does not
    serve; a path in another case, which real routes as the lower-case one (`GET /V1/USERS/ME`
    and `PATCH /v1/Pages/{id}` were each the format's 401 on 2026-09-28) and this answers the URL's
    400; and a method outside the six that the front door passes on to the API, `PROPFIND` and
    `QUERY` among them, which real answered the URL's 400 on 2026-09-28 and this answers the
    framework's 405. `/notion/` gets the URL's 400, and `/notion` is Starlette's 307 to it — the one
    path under `/notion` where its slash redirect fires, since this route matches every other.
    """
    path = request.url.path.removeprefix("/notion")
    if published.includes(_PUBLISHED_NOT_SERVED, request.method, path):
        if (request.method, path) in _CLIENT_AUTH_OPERATIONS:
            return _invalid_client(request)
        if (refusal := _refusal(request, auth.resolve_bearer(request))) is not None:
            return refusal
    return _error(request, 400, "invalid_request_url", "Invalid request URL.")
