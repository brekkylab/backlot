"""Notion API (read-only).

Base URL for a client: ``http://<host>/notion/v1/`` (the notion-client SDK appends ``/v1/`` to
its ``base_url``, so point it at ``http://<host>/notion``). Bearer auth
(``Authorization: Bearer <token>``); the admin/service token sees everything, a user token is
ACL-filtered. Errors use Notion's envelope: ``{"object":"error","status","code","message"}``.

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
request that omits it is answered ``missing_version``, behind the 401 that a request with no
usable credential gets (see ``_refusal``). Every route declares it in the spec as well, so a
client generated from that spec -- ``backlot mcp``'s tools among them -- can send what the route
asks for.

Object mapping: a Notion *page* is one doc (``subtype='page'``); a *database* is one doc
(``subtype='database'``, ``content`` → its description); a *database row* is a page whose
``parent`` is the database. Page ``content`` is served verbatim as the block tree
(``blocks.children.list``); the join of the blocks' plain_text reconstructs ``content`` exactly.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backlot import auth, pagination, store, synth
from backlot.openapi import qp
from backlot.routers import json_body

router = APIRouter(prefix="/notion/v1", tags=["notion"])

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
_P_COMMENTS = [qp("block_id"), *_P_PAGINATE]


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
        **_body({"start_cursor": {"type": "string"}, "page_size": {"type": "integer"}}),
        "parameters": [
            _version_param(
                f"The API version to read this request under. This path serves {serves}; "
                f"{refuses} read a database's rows through the other query path, and a request "
                "under one of them is refused here, as is a request that sends no version."
            )
        ],
    }


_B_QUERY_DATA_SOURCE = _query_extra("2025-09-03 and later", "versions before it")
_B_QUERY_DATABASE = _query_extra("versions up to and including 2022-06-28", "2025-09-03 and later")


# --------------------------------------------------------------------------- helpers


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"object": "error", "status": status, "code": code, "message": message},
    )


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
    handed, both quoted the way the live API quotes them (measured 2026-09-17)."""
    listed = ", ".join(f'`"{v}"`' for v in PUBLISHED_VERSIONS[:-1])
    return (
        f"Notion-Version header failed validation: Notion-Version header should be {listed}, "
        f'or `"{PUBLISHED_VERSIONS[-1]}"`, instead was `"{value}"`.'
    )


def _refusal(request: Request, caller) -> JSONResponse | None:
    """What a request is answered before any route reads a row -- None when it may go on. Every
    route calls this, so the two checks are in one place and in the order the real API applies
    them. Called rather than declared as a router dependency: the credential half has to stay
    where the route resolves the caller it goes on to scope rows by.

    The credential first: on 2026-09-15 an invalid token answered ``unauthorized`` on both query
    paths under 2022-06-28, under 2025-09-03 and with no version header at all, so the version is
    never what a request without a usable credential hears about.

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
    if caller is None:
        return _error(401, "unauthorized", "API token is invalid.")
    version = request.headers.get("notion-version")
    if version is None:
        return _error(
            400,
            "missing_version",
            "Notion-Version header failed validation: Notion-Version header should be defined, "
            "instead was `undefined`.",
        )
    if version not in PUBLISHED_VERSIONS:
        return _error(400, "missing_version", _unknown_version(version))
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
        return _error(400, "invalid_request_url", "Invalid request URL.")
    return None


def _norm(nid: str) -> str:
    """Notion accepts an id dashed or dashless, any case. Canonicalize to the dashed lowercase
    form the stored ``id`` / ``data_source_id`` columns actually hold (see
    ``synth._uuid_from``) rather than to a dashless key: a UUID's dashes sit at fixed offsets
    (8-4-4-4-12), so reconstructing them is deterministic, and the column's job is to hold the
    value the API reports, not a lookup key a reader has to rebuild dashes from.

    Malformed input (the wrong length once dashes are stripped) comes back unchanged, which just
    won't match any stored id -- the same 404 every unknown-but-well-formed id already gets.
    Notion draws no 400-vs-404 shape distinction the way gmail does (see
    routers.google._gmail_check_shape); there is nothing to preserve here."""
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


def _page_size(raw, default: int = _PAGE_MAX) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, _PAGE_MAX))


def _list_obj(results: list, offset: int, page_len: int, total: int, type_key: str) -> dict:
    nxt = offset + page_len
    has_more = nxt < total
    return {
        "object": "list",
        "results": results,
        "next_cursor": pagination.encode_cursor(nxt) if has_more else None,
        "has_more": has_more,
        "type": type_key,
        type_key: {},
    }


def _user_obj(conn, email: str) -> dict:
    u = store.get_user(conn, email)
    name = u["display_name"] if u else (email.split("@")[0] if email else "Unknown")
    return {
        "object": "user",
        "id": synth.notion_user_id(email or ""),
        "type": "person",
        "name": name,
        "avatar_url": None,
        "person": {"email": email},
    }


def _emoji_icon(icon: str | None) -> dict | None:
    if not icon:
        return None
    if icon.startswith("http"):
        return {"type": "external", "external": {"url": icon}}
    return {"type": "emoji", "emoji": icon}


def _cover(url: str | None) -> dict | None:
    return {"type": "external", "external": {"url": url}} if url else None


def _parent_field(row) -> dict:
    # `parent_id` already HOLDS the parent's served id — the importer resolved it there.
    # Hashing it again would name a page nothing serves.
    if row["parent_id"]:
        return {"type": "database_id", "database_id": row["parent_id"]}
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


def _page_obj(conn, row) -> dict:
    pid = row["id"]
    created = row["created_ts"]
    updated = row["updated_ts"] or created
    props = {"title": _title_prop(row["title"])}
    for k, v in (json.loads(row["properties"]) if row["properties"] else {}).items():
        props[k] = _prop_value(k, v)
    return {
        "object": "page",
        "id": pid,
        "created_time": synth.rfc3339(created),
        "last_edited_time": synth.rfc3339(updated),
        "created_by": _user_obj(conn, row["author_email"]),
        "last_edited_by": _user_obj(conn, row["author_email"]),
        "cover": _cover(row["cover"]),
        "icon": _emoji_icon(row["icon"]),
        "parent": _parent_field(row),
        "archived": False,
        "in_trash": False,
        "properties": props,
        "url": f"https://www.notion.so/{pid.replace('-', '')}",
        "public_url": None,
    }


def _schema_props(row) -> dict:
    """A Notion database property *schema* (on a database / data source)."""
    schema = json.loads(row["properties"]) if row["properties"] else {}
    out = {"title": {"id": "title", "name": "Name", "type": "title", "title": {}}}
    for k, v in schema.items():
        t = v.get("type", "rich_text") if isinstance(v, dict) else "rich_text"
        out[k] = {"id": k, "name": k, "type": t, t: {}}
    return out


def _database_obj(conn, row, version: str) -> dict:
    did = row["id"]
    created = row["created_ts"]
    updated = row["updated_ts"] or created
    obj = {
        "object": "database",
        "id": did,
        "created_time": synth.rfc3339(created),
        "last_edited_time": synth.rfc3339(updated),
        "created_by": _user_obj(conn, row["author_email"]),
        "last_edited_by": _user_obj(conn, row["author_email"]),
        "title": synth.notion_rich_text(row["title"]),
        "description": synth.notion_rich_text(row["content"]),
        "icon": _emoji_icon(row["icon"]),
        "cover": _cover(row["cover"]),
        "parent": {"type": "workspace", "workspace": True},
        "archived": False,
        "in_trash": False,
        "is_inline": False,
        "url": f"https://www.notion.so/{did.replace('-', '')}",
        "public_url": None,
    }
    if _data_sources_model(version):
        obj["data_sources"] = [{"id": row["data_source_id"], "name": row["title"]}]
    else:
        obj["properties"] = _schema_props(row)
    return obj


def _data_source_obj(conn, row) -> dict:
    return {
        "object": "data_source",
        "id": row["data_source_id"],
        "name": row["title"],
        "parent": {"type": "database_id", "database_id": row["id"]},
        "database_parent": {"type": "database_id", "database_id": row["id"]},
        "properties": _schema_props(row),
    }


# --------------------------------------------------------------------------- pages / blocks


@router.get("/pages/{page_id}", response_model=NotionObject, openapi_extra=_params())
async def get_page(page_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    conn = auth.conn(request)
    # One ACL-scoped query, not a resolve followed by a get_document refetch of the same row:
    # `id` is the PRIMARY KEY, so the ACL clause can only narrow "found" to "not found", never
    # redirect to a different row (see store.notion_by_id).
    row = store.notion_by_id(conn, _norm(page_id), auth.visible_ids(request, caller))
    if row is None or row["subtype"] == "database":
        return _error(404, "object_not_found", f"Could not find page with ID: {page_id}.")
    return _page_obj(conn, row)


@router.get("/blocks/{block_id}", response_model=NotionObject, openapi_extra=_params())
async def get_block(block_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    conn = auth.conn(request)
    row = store.notion_by_id(conn, _norm(block_id), auth.visible_ids(request, caller))
    if row is None:
        return _error(404, "object_not_found", f"Could not find block with ID: {block_id}.")
    bid = row["id"]
    kind = "child_database" if row["subtype"] == "database" else "child_page"
    return {
        "object": "block",
        "id": bid,
        "type": kind,
        "has_children": True,
        "archived": False,
        "in_trash": False,
        "created_time": synth.rfc3339(row["created_ts"]),
        "last_edited_time": synth.rfc3339(row["updated_ts"] or row["created_ts"]),
        "parent": _parent_field(row),
        kind: {"title": row["title"]},
    }


@router.get(
    "/blocks/{block_id}/children",
    response_model=NotionList,
    openapi_extra=_params(*_P_PAGINATE),
)
async def get_block_children(block_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    conn = auth.conn(request)
    row = store.notion_by_id(conn, _norm(block_id), auth.visible_ids(request, caller))
    if row is None:
        return _error(404, "object_not_found", f"Could not find block with ID: {block_id}.")
    blocks = synth.notion_blocks(row["id"], row["content"])
    offset = pagination.decode_cursor(request.query_params.get("start_cursor"))
    limit = _page_size(request.query_params.get("page_size"))
    page = blocks[offset : offset + limit]
    return _list_obj(page, offset, len(page), len(blocks), "block")


# --------------------------------------------------------------------------- databases / data sources


@router.get("/databases/{database_id}", response_model=NotionObject, openapi_extra=_params())
async def get_database(database_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    conn = auth.conn(request)
    row = store.notion_by_id(conn, _norm(database_id), auth.visible_ids(request, caller))
    if row is None or row["subtype"] != "database":
        return _error(404, "object_not_found", f"Could not find database with ID: {database_id}.")
    return _database_obj(conn, row, _version(request))


@router.get("/data_sources/{data_source_id}", response_model=NotionObject, openapi_extra=_params())
async def get_data_source(data_source_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    if (refusal := _unmounted_here(request, data_sources=True)) is not None:
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
            404, "object_not_found", f"Could not find data source with ID: {data_source_id}."
        )
    return _data_source_obj(conn, row)


async def _query_rows(request: Request, row_id: str, *, data_sources: bool):
    """The rows of one database, reached by whichever of the two query paths the caller's version
    serves. ``row_id`` is the id as spelled in the path -- a data source's under ``data_sources``,
    the database's own under ``databases`` -- and is resolved only once the credential and the
    version have both been accepted, so a refused request never runs a lookup."""
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    if (refusal := _unmounted_here(request, data_sources=data_sources)) is not None:
        return refusal
    db_id = (
        _db_doc_for_data_source(request, row_id) if data_sources else _existing_id(request, row_id)
    )
    conn = auth.conn(request)
    visible = auth.visible_ids(request, caller)
    db = store.get_document(conn, "notion", db_id, visible_ids=visible) if db_id else None
    if db is None or db["subtype"] != "database":
        return _error(404, "object_not_found", "Could not find the requested database.")
    body = await json_body(request)
    offset = pagination.decode_cursor(body.get("start_cursor"))
    limit = _page_size(body.get("page_size"))
    rows = store.children(conn, "notion", db_id, visible, limit=limit + 1, offset=offset)
    page = rows[:limit]
    total = offset + len(rows)  # +1 probe tells us whether there's a next page
    results = [_page_obj(conn, r) for r in page]
    return _list_obj(results, offset, len(page), total, "page_or_database")


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


@router.post("/search", response_model=NotionList, openapi_extra={**_B_SEARCH, **_params()})
async def search(request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    conn = auth.conn(request)
    visible = auth.visible_ids(request, caller)
    body = await json_body(request)
    query = body.get("query") or ""
    want = (body.get("filter") or {}).get("value")  # 'page' | 'database' | None
    offset = pagination.decode_cursor(body.get("start_cursor"))
    limit = _page_size(body.get("page_size"))
    # Over-fetch so object-type filtering still fills a page; cap keeps it bounded. An empty query
    # means "everything the integration can see" (real Notion behavior), so list instead of FTS.
    window = limit * 4 + offset
    if query.strip():
        rows = store.search_documents(conn, query, "notion", visible, limit=window, offset=0)
    else:
        rows = store.list_documents(conn, "notion", visible_ids=visible, limit=window, offset=0)
    picked = []
    for r in rows:
        is_db = r["subtype"] == "database"
        if want == "database" and not is_db:
            continue
        if want == "page" and is_db:
            continue
        picked.append(r)
    window = picked[offset : offset + limit]
    results = [
        _database_obj(conn, r, _version(request))
        if r["subtype"] == "database"
        else _page_obj(conn, r)
        for r in window
    ]
    return _list_obj(results, offset, len(window), len(picked), "page_or_database")


# --------------------------------------------------------------------------- users


@router.get("/users", response_model=NotionList, openapi_extra=_params(*_P_PAGINATE))
async def list_users(request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    conn = auth.conn(request)
    users = store.list_users(conn)
    offset = pagination.decode_cursor(request.query_params.get("start_cursor"))
    limit = _page_size(request.query_params.get("page_size"))
    page = users[offset : offset + limit]
    results = [_user_obj(conn, u["email"]) for u in page]
    return _list_obj(results, offset, len(page), len(users), "user")


@router.get("/users/me", response_model=NotionObject, openapi_extra=_params())
async def get_me(request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    conn = auth.conn(request)
    if caller.email:  # a user token → that person
        return _user_obj(conn, caller.email)
    # admin/service token → the integration's bot user
    return {
        "object": "user",
        "id": synth.notion_user_id("bot:backlot"),
        "type": "bot",
        "name": "backlot",
        "avatar_url": None,
        "bot": {
            "owner": {"type": "workspace", "workspace": True},
            "workspace_name": auth.acl(request).org_name,
        },
    }


@router.get("/users/{user_id}", response_model=NotionObject, openapi_extra=_params())
async def get_user(user_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    conn = auth.conn(request)
    key = _norm(user_id)
    for u in store.list_users(conn):
        if _norm(synth.notion_user_id(u["email"])) == key:
            return _user_obj(conn, u["email"])
    return _error(404, "object_not_found", f"Could not find user with ID: {user_id}.")


# --------------------------------------------------------------------------- comments


@router.get("/comments", response_model=NotionList, openapi_extra=_params(*_P_COMMENTS))
async def list_comments(request: Request):
    caller = auth.resolve_bearer(request)
    if (refusal := _refusal(request, caller)) is not None:
        return refusal
    conn = auth.conn(request)
    block_id = request.query_params.get("block_id")
    # One ACL-scoped query (the parent must itself be visible to the caller), not
    # a resolve followed by a get_document refetch of the same row -- see get_page and friends.
    row = (
        store.notion_by_id(conn, _norm(block_id), auth.visible_ids(request, caller))
        if block_id
        else None
    )
    if row is None:
        return _list_obj([], 0, 0, 0, "comment")
    parent_id = row["id"]
    comments = store.doc_comments(conn, "notion", parent_id)
    offset = pagination.decode_cursor(request.query_params.get("start_cursor"))
    limit = _page_size(request.query_params.get("page_size"))
    page = comments[offset : offset + limit]
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
    return _list_obj(results, offset, len(page), len(comments), "comment")


# --------------------------------------------------------------------------- misc
