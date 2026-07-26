"""HubSpot CRM v3 read surface (+ v4 associations), served under ``/hubspot``.

The API is **polymorphic over ``{objectType}``** — one set of routes serves contacts, companies,
deals, notes, and any custom object — so this router dispatches on a path variable rather than
having a route per type, and the store keeps one table with the typed fields in a ``properties``
JSON column (see ``app/store.py``).

Paths and shapes follow what the official ``hubspot-api-client`` actually calls: **v3 for objects,
v4 for associations**. HubSpot also publishes a newer date-versioned scheme
(``/crm/objects/2026-03/…``); the SDK does not use it, so neither does this mock.

Read-only: ``search`` and ``batch/read`` are reads issued over POST and are served; create/update/
delete are not.

One contract deserves calling out because getting it wrong hangs clients rather than erroring: the
official client's ``fetch_all`` loops until a page has **no** ``paging.next``, so the last page must
omit it. :func:`_page` is the single place that decides this.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app import auth, store, synth

router = APIRouter(prefix="/hubspot", tags=["hubspot"])

# `hubspot/utils/objects.py` in the official client pages at 100 (PAGE_MAX_SIZE); a larger `limit`
# is clamped rather than rejected, matching how HubSpot itself caps a page.
_PAGE_MAX = 100
# HubSpot caps how deep a search can page; a bounded scan also keeps one request from walking a
# 15k-record object type when a filter matches almost everything.
_SEARCH_SCAN_MAX = 10_000


# --- OpenAPI enrichment (issue #4 bridge) --------------------------------------------------
# Query params are documented with openapi_extra (merges with path params, no signature change);
# POST bodies are read via _json_body, so they are declared as a requestBody the same way.

class _HLoose(BaseModel):
    model_config = ConfigDict(extra="allow")


class HubspotObject(_HLoose):
    id: str
    properties: dict = {}


class HubspotPage(_HLoose):
    results: list[dict] = []


def _qp(name: str, typ: str = "string") -> dict:
    return {"name": name, "in": "query", "schema": {"type": typ}}


_P_LIST = [_qp("limit", "integer"), _qp("after"), _qp("properties"),
           _qp("propertiesWithHistory"), _qp("associations"), _qp("archived", "boolean")]
_P_READ = [_qp("properties"), _qp("propertiesWithHistory"), _qp("associations"),
           _qp("archived", "boolean")]
_P_ASSOC = [_qp("limit", "integer"), _qp("after")]

_FILTER_SCHEMA = {"type": "object", "properties": {
    "propertyName": {"type": "string"}, "operator": {"type": "string"},
    "value": {"type": "string"}, "values": {"type": "array", "items": {"type": "string"}},
    "highValue": {"type": "string"}}}
_B_SEARCH = {"requestBody": {"content": {"application/json": {"schema": {
    "type": "object", "properties": {
        "filterGroups": {"type": "array", "items": {"type": "object", "properties": {
            "filters": {"type": "array", "items": _FILTER_SCHEMA}}}},
        "sorts": {"type": "array", "items": {"type": "object"}},
        "query": {"type": "string"},
        "properties": {"type": "array", "items": {"type": "string"}},
        "limit": {"type": "integer"}, "after": {"type": "string"}}}}}}}
_B_BATCH = {"requestBody": {"content": {"application/json": {"schema": {
    "type": "object", "properties": {
        "inputs": {"type": "array", "items": {"type": "object",
                                              "properties": {"id": {"type": "string"}}}},
        "properties": {"type": "array", "items": {"type": "string"}},
        "idProperty": {"type": "string"}}}}}}}


# --------------------------------------------------------------------------- helpers

def _error(status: int, message: str, category: str = "VALIDATION_ERROR") -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"status": "error", "message": message, "category": category})


def _caller(request: Request):
    return auth.resolve_bearer(request)


def _visible(request: Request, caller):
    return auth.visible_ids(request, caller)


def _doc_id_for(request: Request, record_id: str) -> str | None:
    return request.app.state.index["hubspot"].get(record_id)


def _clamp(raw, default: int, cap: int) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, cap))


def _props(row) -> dict:
    return store.jcol(row, "properties", {}) or {}


def _record(row, keep: list[str] | None = None) -> dict:
    """One CRM record in HubSpot's object shape. ``keep`` mirrors the ``properties`` query param:
    a projection, not a different record."""
    props = _props(row)
    if keep:
        props = {k: v for k, v in props.items() if k in keep}
    out = {
        "id": synth.hubspot_record_id(row["doc_id"]),
        "properties": props,
        "createdAt": synth.rfc3339_millis(row["created_ts"]),
        "updatedAt": synth.rfc3339_millis(row["updated_ts"] or row["created_ts"]),
        "archived": bool(row["archived"]),
    }
    return out


def _page(rows, limit: int, keep: list[str] | None) -> dict:
    """A paged listing. ``rows`` is limit+1 rows when a further page exists — the extra row is the
    only evidence needed, and it is dropped from the response. ``paging.next`` is emitted ONLY
    when it exists: the official client's fetch_all treats its absence as "done", so a mock that
    always emits it makes a real client loop forever."""
    has_more = len(rows) > limit
    rows = rows[:limit]
    out: dict = {"results": [_record(r, keep) for r in rows]}
    if has_more and rows:
        after = synth.hubspot_record_id(rows[-1]["doc_id"])
        out["paging"] = {"next": {"after": after, "link": f"?after={after}"}}
    return out


def _keep(raw) -> list[str] | None:
    """`properties` arrives comma-separated on GET and as a list on POST."""
    if raw is None:
        return None
    if isinstance(raw, list):
        return [str(p) for p in raw]
    return [p for p in str(raw).split(",") if p]


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — empty/invalid body → treat as no params
        return {}
    return body if isinstance(body, dict) else {}


# --------------------------------------------------------------------------- search filters

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _values_of(prop):
    """A property may hold a list (our custom CRM properties do); a filter matches if ANY element
    matches, which is how HubSpot treats multi-value properties."""
    if isinstance(prop, list):
        return [str(x) for x in prop]
    return [str(prop)]


def _tokens(s: str) -> set[str]:
    return {t for t in "".join(c.lower() if c.isalnum() else " " for c in s).split() if t}


def _match_one(prop, f: dict) -> bool:
    op = (f.get("operator") or "EQ").upper()
    present = prop is not None
    if op == "HAS_PROPERTY":
        return present
    if op == "NOT_HAS_PROPERTY":
        return not present
    if not present:
        return False
    target = f.get("value")
    cands = _values_of(prop)

    if op in ("EQ", "NEQ"):
        hit = any(c == str(target) for c in cands)
        return hit if op == "EQ" else not hit
    if op in ("IN", "NOT_IN"):
        wanted = {str(v) for v in (f.get("values") or [])}
        hit = any(c in wanted for c in cands)
        return hit if op == "IN" else not hit
    if op in ("CONTAINS_TOKEN", "NOT_CONTAINS_TOKEN"):
        want = _tokens(str(target or ""))
        hit = any(want and want <= _tokens(c) for c in cands)
        return hit if op == "CONTAINS_TOKEN" else not hit
    if op == "BETWEEN":
        lo, hi = _num(target), _num(f.get("highValue"))
        return any((n := _num(c)) is not None and lo is not None and hi is not None
                   and lo <= n <= hi for c in cands)
    if op in ("LT", "LTE", "GT", "GTE"):
        # numeric when both sides parse as numbers, else a string comparison — HubSpot property
        # types are not declared to this mock, so the values decide.
        t_num = _num(target)
        for c in cands:
            c_num = _num(c)
            a, b = ((c_num, t_num) if c_num is not None and t_num is not None
                    else (c, str(target)))
            if (op == "LT" and a < b) or (op == "LTE" and a <= b) \
                    or (op == "GT" and a > b) or (op == "GTE" and a >= b):
                return True
        return False
    return False


def _matches(row, body: dict) -> bool:
    """``filterGroups`` are OR-ed; the ``filters`` inside one group are AND-ed. A free-text
    ``query`` additionally has to hit the record's text."""
    q = (body.get("query") or "").strip().lower()
    if q and q not in f"{row['title']} {row['content']}".lower():
        return False
    groups = body.get("filterGroups") or []
    if not groups:
        return True
    props = _props(row)
    return any(all(_match_one(props.get(f.get("propertyName")), f)
                   for f in (g.get("filters") or []))
               for g in groups)


# --------------------------------------------------------------------------- routes

@router.get("/crm/v3/objects/{object_type}", response_model=HubspotPage,
            openapi_extra={"parameters": _P_LIST})
async def list_objects(object_type: str, request: Request):
    caller = _caller(request)
    if caller is None:
        return _error(401, "Authentication credentials not found.", "INVALID_AUTHENTICATION")
    qp = request.query_params
    limit = _clamp(qp.get("limit"), 10, _PAGE_MAX)
    after_doc = _doc_id_for(request, qp.get("after")) if qp.get("after") else None
    rows = store.list_hubspot_objects(
        auth.conn(request), object_type, after_doc_id=after_doc,
        visible_ids=_visible(request, caller), limit=limit + 1,
        archived=(qp.get("archived") or "").lower() == "true")
    return _page(rows, limit, _keep(qp.get("properties")))


@router.get("/crm/v3/objects/{object_type}/{record_id}", response_model=HubspotObject,
            openapi_extra={"parameters": _P_READ})
async def get_object(object_type: str, record_id: str, request: Request):
    caller = _caller(request)
    if caller is None:
        return _error(401, "Authentication credentials not found.", "INVALID_AUTHENTICATION")
    doc_id = _doc_id_for(request, record_id)
    row = (store.get_document(auth.conn(request), "hubspot", doc_id,
                              _visible(request, caller)) if doc_id else None)
    if row is None or row["object_type"] != object_type:
        return _error(404, "resource not found", "OBJECT_NOT_FOUND")
    return _record(row, _keep(request.query_params.get("properties")))


@router.post("/crm/v3/objects/{object_type}/search", response_model=HubspotPage,
             openapi_extra=_B_SEARCH)
async def search_objects(object_type: str, request: Request):
    caller = _caller(request)
    if caller is None:
        return _error(401, "Authentication credentials not found.", "INVALID_AUTHENTICATION")
    body = await _json_body(request)
    limit = _clamp(body.get("limit"), 10, _PAGE_MAX)
    visible = _visible(request, caller)
    conn = auth.conn(request)
    # Filters may name ANY property, so they are evaluated over the JSON column rather than
    # compiled to SQL; the object-type and ACL predicates stay in SQL, and the scan is bounded the
    # way HubSpot bounds search depth.
    after_doc = _doc_id_for(request, body["after"]) if body.get("after") else None
    hits: list = []
    cursor = after_doc
    scanned = 0
    # Scan to the bound rather than stopping at `limit` matches: `total` is the number of matching
    # records, not the size of this page, so an early exit would under-report it.
    while scanned < _SEARCH_SCAN_MAX:
        batch = store.list_hubspot_objects(conn, object_type, after_doc_id=cursor,
                                           visible_ids=visible, limit=500)
        if not batch:
            break
        scanned += len(batch)
        cursor = batch[-1]["doc_id"]
        hits += [r for r in batch if _matches(r, body)]
    total = len(hits)
    out = _page(hits, limit, _keep(body.get("properties")))
    out["total"] = total
    return out


@router.post("/crm/v3/objects/{object_type}/batch/read", response_model=HubspotPage,
             openapi_extra=_B_BATCH)
async def batch_read(object_type: str, request: Request):
    caller = _caller(request)
    if caller is None:
        return _error(401, "Authentication credentials not found.", "INVALID_AUTHENTICATION")
    body = await _json_body(request)
    conn, visible = auth.conn(request), _visible(request, caller)
    keep = _keep(body.get("properties"))
    results, errors = [], 0
    for item in body.get("inputs") or []:
        doc_id = _doc_id_for(request, str(item.get("id")))
        row = store.get_document(conn, "hubspot", doc_id, visible) if doc_id else None
        if row is None or row["object_type"] != object_type:
            errors += 1
            continue
        results.append(_record(row, keep))
    out = {"status": "COMPLETE" if not errors else "PARTIAL", "results": results}
    if errors:
        out["numErrors"] = errors
    return out


@router.get("/crm/v4/objects/{object_type}/{record_id}/associations/{to_object_type}",
            response_model=HubspotPage, openapi_extra={"parameters": _P_ASSOC})
async def list_associations(object_type: str, record_id: str, to_object_type: str,
                            request: Request):
    caller = _caller(request)
    if caller is None:
        return _error(401, "Authentication credentials not found.", "INVALID_AUTHENTICATION")
    doc_id = _doc_id_for(request, record_id)
    conn, visible = auth.conn(request), _visible(request, caller)
    row = store.get_document(conn, "hubspot", doc_id, visible) if doc_id else None
    if row is None or row["object_type"] != object_type:
        return _error(404, "resource not found", "OBJECT_NOT_FOUND")
    limit = _clamp(request.query_params.get("limit"), 500, 500)
    rows = store.hubspot_associations(conn, doc_id, to_object_type, visible_ids=visible,
                                     limit=limit)
    return {"results": [{
        "toObjectId": synth.hubspot_record_id(r["to_doc_id"]),
        "associationTypes": [{"category": r["assoc_category"], "typeId": r["assoc_type_id"],
                              "label": r["label"]}],
    } for r in rows]}
