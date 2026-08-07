"""Mock Notion API (read-only).

Base URL for a client: ``http://<host>/notion/v1/`` (the notion-client SDK appends ``/v1/`` to
its ``base_url``, so point it at ``http://<host>/notion``). Bearer auth
(``Authorization: Bearer <token>``); the admin/service token sees everything, a user token is
ACL-filtered. Errors use Notion's envelope: ``{"object":"error","status","code","message"}``.

**Version-aware databases.** Notion moved database querying to the *data sources* model in
``2025-09-03``. This router keys off the ``Notion-Version`` request header:

- ``2025-09-03`` (default): ``databases.retrieve`` returns a ``data_sources: [{id,name}]`` array
  and rows are read via ``POST /data_sources/{id}/query``.
- ``2022-06-28`` (e.g. mirage): ``databases.retrieve`` returns ``properties`` (schema) inline and
  rows are read via ``POST /databases/{id}/query``.

Both query paths and both retrieve shapes are always served; the mock has one data source per
database, its id derived deterministically from the database's doc_id.

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
from backlot.routers import json_body
from backlot.openapi import qp

router = APIRouter(prefix="/notion/v1", tags=["notion"])

_PAGE_MAX = 100  # Notion caps page_size at 100
DEFAULT_VERSION = "2025-09-03"
LEGACY_VERSION = "2022-06-28"


# --- OpenAPI enrichment (issue #4 bridge) --------------------------------------------------
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
_B_QUERY = _body({"start_cursor": {"type": "string"}, "page_size": {"type": "integer"}})


# --------------------------------------------------------------------------- helpers


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"object": "error", "status": status, "code": code, "message": message},
    )


def _version(request: Request) -> str:
    return request.headers.get("notion-version") or DEFAULT_VERSION


def _norm(nid: str) -> str:
    """Notion accepts ids with or without dashes; normalize to the 32-hex index key."""
    return (nid or "").replace("-", "").lower()


def _doc_id_for(request: Request, page_id: str) -> str | None:
    return request.app.state.index["notion"].get(_norm(page_id))


def _db_doc_for_data_source(request: Request, dsid: str) -> str | None:
    key = _norm(dsid)
    for doc_id in request.app.state.index["notion"].values():
        if _norm(synth.notion_data_source_id(doc_id)) == key:
            return doc_id
    return None


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
    if row["parent_id"]:
        return {"type": "database_id", "database_id": synth.notion_id(row["parent_id"])}
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
    pid = synth.notion_id(row["doc_id"])
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
    did = synth.notion_id(row["doc_id"])
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
    if version == LEGACY_VERSION:
        obj["properties"] = _schema_props(row)
    else:
        obj["data_sources"] = [
            {"id": synth.notion_data_source_id(row["doc_id"]), "name": row["title"]}
        ]
    return obj


def _data_source_obj(conn, row) -> dict:
    return {
        "object": "data_source",
        "id": synth.notion_data_source_id(row["doc_id"]),
        "name": row["title"],
        "parent": {"type": "database_id", "database_id": synth.notion_id(row["doc_id"])},
        "database_parent": {"type": "database_id", "database_id": synth.notion_id(row["doc_id"])},
        "properties": _schema_props(row),
    }


# --------------------------------------------------------------------------- pages / blocks


@router.get("/pages/{page_id}", response_model=NotionObject)
async def get_page(page_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "unauthorized", "API token is invalid.")
    conn = auth.conn(request)
    doc_id = _doc_id_for(request, page_id)
    row = (
        store.get_document(conn, "notion", doc_id, auth.visible_ids(request, caller))
        if doc_id
        else None
    )
    if row is None or row["subtype"] == "database":
        return _error(404, "object_not_found", f"Could not find page with ID: {page_id}.")
    return _page_obj(conn, row)


@router.get("/blocks/{block_id}", response_model=NotionObject)
async def get_block(block_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "unauthorized", "API token is invalid.")
    conn = auth.conn(request)
    doc_id = _doc_id_for(request, block_id)
    row = (
        store.get_document(conn, "notion", doc_id, auth.visible_ids(request, caller))
        if doc_id
        else None
    )
    if row is None:
        return _error(404, "object_not_found", f"Could not find block with ID: {block_id}.")
    bid = synth.notion_id(row["doc_id"])
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
    openapi_extra={"parameters": _P_PAGINATE},
)
async def get_block_children(block_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "unauthorized", "API token is invalid.")
    conn = auth.conn(request)
    doc_id = _doc_id_for(request, block_id)
    row = (
        store.get_document(conn, "notion", doc_id, auth.visible_ids(request, caller))
        if doc_id
        else None
    )
    if row is None:
        return _error(404, "object_not_found", f"Could not find block with ID: {block_id}.")
    blocks = synth.notion_blocks(row["doc_id"], row["content"])
    offset = pagination.decode_cursor(request.query_params.get("start_cursor"))
    limit = _page_size(request.query_params.get("page_size"))
    page = blocks[offset : offset + limit]
    return _list_obj(page, offset, len(page), len(blocks), "block")


# --------------------------------------------------------------------------- databases / data sources


@router.get("/databases/{database_id}", response_model=NotionObject)
async def get_database(database_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "unauthorized", "API token is invalid.")
    conn = auth.conn(request)
    doc_id = _doc_id_for(request, database_id)
    row = (
        store.get_document(conn, "notion", doc_id, auth.visible_ids(request, caller))
        if doc_id
        else None
    )
    if row is None or row["subtype"] != "database":
        return _error(404, "object_not_found", f"Could not find database with ID: {database_id}.")
    return _database_obj(conn, row, _version(request))


@router.get("/data_sources/{data_source_id}", response_model=NotionObject)
async def get_data_source(data_source_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "unauthorized", "API token is invalid.")
    conn = auth.conn(request)
    doc_id = _db_doc_for_data_source(request, data_source_id)
    row = (
        store.get_document(conn, "notion", doc_id, auth.visible_ids(request, caller))
        if doc_id
        else None
    )
    if row is None or row["subtype"] != "database":
        return _error(
            404, "object_not_found", f"Could not find data source with ID: {data_source_id}."
        )
    return _data_source_obj(conn, row)


async def _query_rows(request: Request, db_doc_id: str | None):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "unauthorized", "API token is invalid.")
    conn = auth.conn(request)
    visible = auth.visible_ids(request, caller)
    db = store.get_document(conn, "notion", db_doc_id, visible) if db_doc_id else None
    if db is None or db["subtype"] != "database":
        return _error(404, "object_not_found", "Could not find the requested database.")
    body = await json_body(request)
    offset = pagination.decode_cursor(body.get("start_cursor"))
    limit = _page_size(body.get("page_size"))
    rows = store.children(conn, "notion", db_doc_id, visible, limit=limit + 1, offset=offset)
    page = rows[:limit]
    total = offset + len(rows)  # +1 probe tells us whether there's a next page
    results = [_page_obj(conn, r) for r in page]
    return _list_obj(results, offset, len(page), total, "page_or_database")


@router.post(
    "/data_sources/{data_source_id}/query", response_model=NotionList, openapi_extra=_B_QUERY
)
async def query_data_source(data_source_id: str, request: Request):
    return await _query_rows(request, _db_doc_for_data_source(request, data_source_id))


@router.post("/databases/{database_id}/query", response_model=NotionList, openapi_extra=_B_QUERY)
async def query_database(database_id: str, request: Request):
    return await _query_rows(request, _doc_id_for(request, database_id))


# --------------------------------------------------------------------------- search


@router.post("/search", response_model=NotionList, openapi_extra=_B_SEARCH)
async def search(request: Request):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "unauthorized", "API token is invalid.")
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


@router.get("/users", response_model=NotionList, openapi_extra={"parameters": _P_PAGINATE})
async def list_users(request: Request):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "unauthorized", "API token is invalid.")
    conn = auth.conn(request)
    users = store.list_users(conn)
    offset = pagination.decode_cursor(request.query_params.get("start_cursor"))
    limit = _page_size(request.query_params.get("page_size"))
    page = users[offset : offset + limit]
    results = [_user_obj(conn, u["email"]) for u in page]
    return _list_obj(results, offset, len(page), len(users), "user")


@router.get("/users/me", response_model=NotionObject)
async def get_me(request: Request):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "unauthorized", "API token is invalid.")
    conn = auth.conn(request)
    if caller.email:  # a user token → that person
        return _user_obj(conn, caller.email)
    # admin/service token → the integration's bot user
    return {
        "object": "user",
        "id": synth.notion_user_id("bot:enterprise-mock"),
        "type": "bot",
        "name": "enterprise-mock",
        "avatar_url": None,
        "bot": {
            "owner": {"type": "workspace", "workspace": True},
            "workspace_name": auth.acl(request).org_name,
        },
    }


@router.get("/users/{user_id}", response_model=NotionObject)
async def get_user(user_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "unauthorized", "API token is invalid.")
    conn = auth.conn(request)
    key = _norm(user_id)
    for u in store.list_users(conn):
        if _norm(synth.notion_user_id(u["email"])) == key:
            return _user_obj(conn, u["email"])
    return _error(404, "object_not_found", f"Could not find user with ID: {user_id}.")


# --------------------------------------------------------------------------- comments


@router.get("/comments", response_model=NotionList, openapi_extra={"parameters": _P_COMMENTS})
async def list_comments(request: Request):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "unauthorized", "API token is invalid.")
    conn = auth.conn(request)
    block_id = request.query_params.get("block_id")
    doc_id = _doc_id_for(request, block_id) if block_id else None
    # the parent must itself be visible to the caller
    row = (
        store.get_document(conn, "notion", doc_id, auth.visible_ids(request, caller))
        if doc_id
        else None
    )
    if row is None:
        return _list_obj([], 0, 0, 0, "comment")
    parent_id = synth.notion_id(row["doc_id"])
    comments = store.doc_comments(conn, "notion", doc_id)
    offset = pagination.decode_cursor(request.query_params.get("start_cursor"))
    limit = _page_size(request.query_params.get("page_size"))
    page = comments[offset : offset + limit]
    results = [
        {
            "object": "comment",
            "id": synth.notion_id(c["id"]),
            "parent": {"type": "page_id", "page_id": parent_id},
            "discussion_id": synth.notion_id(f"disc:{row['doc_id']}"),
            "created_time": synth.rfc3339(c["created_ts"]),
            "last_edited_time": synth.rfc3339(c["created_ts"]),
            "created_by": _user_obj(conn, c["author_email"]),
            "rich_text": synth.notion_rich_text(c["body"]),
        }
        for c in page
    ]
    return _list_obj(results, offset, len(page), len(comments), "comment")


# --------------------------------------------------------------------------- misc
