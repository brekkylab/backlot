"""HubSpot CRM v3 read surface (+ v4 associations), served under ``/hubspot``.

The API is **polymorphic over ``{objectType}``** — one set of routes serves contacts, companies,
deals, notes, and any custom object — so this router dispatches on a path variable rather than
having a route per type, and the store keeps one table with the typed fields in a ``properties``
JSON column (see ``backlot/store.py``).

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

import re
from functools import lru_cache

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backlot import auth, store, synth
from backlot.routers import json_body
from backlot.openapi import qp

router = APIRouter(prefix="/hubspot", tags=["hubspot"])

# `hubspot/utils/objects.py` in the official client pages at 100 (PAGE_MAX_SIZE); a larger `limit`
# is clamped rather than rejected, matching how HubSpot itself caps a page.
_PAGE_MAX = 100
# The associations endpoint pages at 500 per request, like the vendor's.
_ASSOC_PAGE_MAX = 500


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


# Only parameters the mock actually honours are advertised: `propertiesWithHistory` and inline
# `associations` expansion are not implemented, and declaring them would have clients ask for data
# that silently never arrives (worse for `propertiesWithHistory`, which also makes the official
# client drop its page size to 50).
_P_LIST = [qp("limit", "integer"), qp("after"), qp("properties"), qp("archived", "boolean")]
_P_READ = [qp("properties"), qp("archived", "boolean")]
_P_ASSOC = [qp("limit", "integer"), qp("after")]

_FILTER_SCHEMA = {
    "type": "object",
    "properties": {
        "propertyName": {"type": "string"},
        "operator": {"type": "string"},
        "value": {"type": "string"},
        "values": {"type": "array", "items": {"type": "string"}},
        "highValue": {"type": "string"},
    },
}
_B_SEARCH = {
    "requestBody": {
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "filterGroups": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "filters": {"type": "array", "items": _FILTER_SCHEMA}
                                },
                            },
                        },
                        "sorts": {"type": "array", "items": {"type": "object"}},
                        "query": {"type": "string"},
                        "properties": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer"},
                        "after": {"type": "string"},
                    },
                }
            }
        }
    }
}
_B_BATCH = {
    "requestBody": {
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "inputs": {
                            "type": "array",
                            "items": {"type": "object", "properties": {"id": {"type": "string"}}},
                        },
                        "properties": {"type": "array", "items": {"type": "string"}},
                        "idProperty": {"type": "string"},
                    },
                }
            }
        }
    }
}


# --------------------------------------------------------------------------- helpers


def _error(status: int, message: str, category: str = "VALIDATION_ERROR") -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"status": "error", "message": message, "category": category}
    )


def _doc_id_for(request: Request, record_id: str) -> str | None:
    return request.app.state.index["hubspot"].get(record_id)


def _clamp(raw, default: int, cap: int) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, cap))


def _flag(raw) -> bool:
    """`archived=1` must not silently serve the un-archived view; accept the spellings a raw caller
    plausibly sends (the official client always sends `true`/`false`)."""
    return str(raw or "").strip().lower() in {"true", "1", "yes"}


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
    if has_more:
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


# HubSpot's standard CRM objects exist in every portal whether or not any records do, so an empty
# `deals` is an empty listing rather than an unknown type. Custom objects exist only where defined,
# which for this mock means present in the corpus.
_STANDARD_OBJECT_TYPES = frozenset(
    {
        "contacts",
        "companies",
        "deals",
        "tickets",
        "line_items",
        "products",
        "quotes",
        "notes",
        "emails",
        "meetings",
        "calls",
        "tasks",
        "feedback_submissions",
    }
)


def _known_type(request: Request, object_type: str) -> bool:
    """Whether this object type exists at all — a standard CRM object, or a custom one the corpus
    defines. Deliberately independent of the caller's ACL and of whether any record is visible: a
    type whose every record the caller cannot read still exists and still returns an empty page."""
    return (
        object_type in _STANDARD_OBJECT_TYPES
        or store.get_container(auth.conn(request), "hubspot", object_type) is not None
    )


def _resolve_cursor(request: Request, after: str | None):
    """(doc_id, error) for an ``after`` cursor. A cursor that names no record is an error rather
    than a silent restart — a client resuming with a stale cursor would otherwise re-read the whole
    object type as though it were the first page."""
    if not after:
        return None, None
    doc_id = _doc_id_for(request, after)
    if doc_id is None:
        return None, _error(400, f"Invalid 'after' cursor: {after}")
    return doc_id, None


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


# A token is a run of alphanumerics; `_` separates, so "audit_logging" holds the token "audit".
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Same class as a lookaround pair, so a needle matches only on token boundaries — equivalent to
# testing membership in the haystack's token set, without having to build that set.
_TOK = r"[^\W_]"


def _tokens(s: str) -> set[str]:
    return set(_TOKEN_RE.findall(s.lower()))


@lru_cache(maxsize=512)
def _token_patterns(target: str) -> tuple:
    """One compiled boundary-anchored pattern per token in the needle.

    Scanning a large object type called this once per row with the same needle, and tokenizing the
    whole haystack to test a couple of needle tokens: both are wasted. Compiling per needle (cached)
    and searching the haystack lets a miss bail on the first absent token instead of building a full
    token set for every row."""
    return tuple(re.compile(f"(?<!{_TOK}){re.escape(t)}(?!{_TOK})") for t in _tokens(target))


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
        pats = _token_patterns(str(target or ""))
        hit = bool(pats) and any(all(p.search(c.lower()) for p in pats) for c in cands)
        return hit if op == "CONTAINS_TOKEN" else not hit
    if op == "BETWEEN":
        # Numeric when all three parse as numbers, else lexicographic — the same fallback LT/GT
        # use. Without it an ISO-8601 range returns nothing while `GT` on the same property works,
        # which bites the mock's own `hs_timestamp` values.
        hi_raw = f.get("highValue")
        lo_n, hi_n = _num(target), _num(hi_raw)
        for c in cands:
            c_n = _num(c)
            if None not in (lo_n, hi_n, c_n):
                if lo_n <= c_n <= hi_n:
                    return True
            elif str(target) <= c <= str(hi_raw):
                return True
        return False
    if op in ("LT", "LTE", "GT", "GTE"):
        # numeric when both sides parse as numbers, else a string comparison — HubSpot property
        # types are not declared to this mock, so the values decide.
        t_num = _num(target)
        for c in cands:
            c_num = _num(c)
            a, b = (c_num, t_num) if c_num is not None and t_num is not None else (c, str(target))
            if (
                (op == "LT" and a < b)
                or (op == "LTE" and a <= b)
                or (op == "GT" and a > b)
                or (op == "GTE" and a >= b)
            ):
                return True
        return False
    return False


def _sorted(rows, sorts):
    """Apply `sorts` (first entry wins, as HubSpot documents). Numeric when every value on that
    property parses as a number, else lexicographic — the mock is not told property types."""
    if not sorts:
        return rows
    spec = sorts[0] if isinstance(sorts, list) and sorts else None
    if not isinstance(spec, dict) or not spec.get("propertyName"):
        return rows
    name = spec["propertyName"]
    # Decorate once: the properties JSON is parsed per row here, and re-parsing it inside the sort
    # key would double that over the whole match set (15k+ rows on the bench corpus).
    decorated = [(_props(r).get(name), r) for r in rows]
    vals = [v for v, _ in decorated]
    numeric = any(v is not None for v in vals) and all(
        _num(v) is not None for v in vals if v is not None
    )

    def key(pair):
        v = pair[0]
        if v is None:  # absent sorts last in both directions
            return (1, 0.0 if numeric else "")
        return (0, _num(v) if numeric else str(v))

    decorated.sort(key=key, reverse=str(spec.get("direction", "ASCENDING")).upper() == "DESCENDING")
    return [r for _, r in decorated]


def _ascii_scalar(v) -> str | None:
    """A value usable in a substring pre-filter: stored `properties` JSON is written with
    `json.dumps` defaults, so non-ASCII lands escaped as \\uXXXX and a raw needle would not be
    found — which for a *necessary* condition would wrongly drop real matches."""
    if not isinstance(v, str) or not v or not v.isascii():
        return None
    return v if '"' not in v and "\\" not in v else None


def _sql_prefilter(body: dict):
    """A SQL condition every match must satisfy, or None.

    Only a single filter group qualifies: within one group the filters are AND-ed, so each one is
    individually necessary. Across groups they are OR-ed and no single filter has to hold. Python
    stays the authority on what actually matches — this only shrinks what Python has to look at.
    """
    groups = body.get("filterGroups") or []
    if len(groups) != 1 or (body.get("query") or "").strip():
        return None
    frags, params = [], []
    for f in groups[0].get("filters") or []:
        if not isinstance(f, dict) or not f.get("propertyName"):
            return None
        name, op = f["propertyName"], (f.get("operator") or "EQ").upper()
        if not name.isascii() or not name.replace("_", "").isalnum():
            return None  # keep the JSON path a literal we can trust
        path = f"$.{name}"
        if op == "HAS_PROPERTY":
            frags.append("json_extract(properties, ?) IS NOT NULL")
            params.append(path)
        elif op in ("EQ", "IN", "CONTAINS_TOKEN"):
            # The value (or every needle token) must appear somewhere in the properties text. True
            # whether the property holds a scalar or a list, which is why this is a substring test
            # rather than an equality one.
            needles = [f.get("value")] if op != "IN" else list(f.get("values") or [])
            if op == "CONTAINS_TOKEN":
                needles = list(_tokens(str(f.get("value") or "")))
            vals = [_ascii_scalar(v) for v in needles]
            if not vals or any(v is None for v in vals):
                return None
            if op == "IN":
                frags.append(
                    "(" + " OR ".join(["instr(lower(properties), lower(?)) > 0"] * len(vals)) + ")"
                )
            else:
                frags += ["instr(lower(properties), lower(?)) > 0"] * len(vals)
            params += vals
        # every other operator (NEQ/NOT_*/comparisons/BETWEEN) has no safe necessary condition here
    return (" AND ".join(frags), params) if frags else None


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
    return any(
        all(_match_one(props.get(f.get("propertyName")), f) for f in (g.get("filters") or []))
        for g in groups
    )


# --------------------------------------------------------------------------- routes


@router.get(
    "/crm/v3/objects/{object_type}",
    response_model=HubspotPage,
    openapi_extra={"parameters": _P_LIST},
)
async def list_objects(object_type: str, request: Request):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "Authentication credentials not found.", "INVALID_AUTHENTICATION")
    if not _known_type(request, object_type):
        return _error(404, f"Unable to infer object type from: {object_type}", "OBJECT_NOT_FOUND")
    qp = request.query_params
    limit = _clamp(qp.get("limit"), 10, _PAGE_MAX)
    after_doc, err = _resolve_cursor(request, qp.get("after"))
    if err is not None:
        return err
    rows = store.list_hubspot_objects(
        auth.conn(request),
        object_type,
        after_doc_id=after_doc,
        visible_ids=auth.visible_ids(request, caller),
        limit=limit + 1,
        archived=_flag(qp.get("archived")),
    )
    return _page(rows, limit, _keep(qp.get("properties")))


@router.get(
    "/crm/v3/objects/{object_type}/{record_id}",
    response_model=HubspotObject,
    openapi_extra={"parameters": _P_READ},
)
async def get_object(object_type: str, record_id: str, request: Request):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "Authentication credentials not found.", "INVALID_AUTHENTICATION")
    doc_id = _doc_id_for(request, record_id)
    row = (
        store.get_document(auth.conn(request), "hubspot", doc_id, auth.visible_ids(request, caller))
        if doc_id
        else None
    )
    if row is None or row["object_type"] != object_type:
        return _error(404, "resource not found", "OBJECT_NOT_FOUND")
    return _record(row, _keep(request.query_params.get("properties")))


@router.post(
    "/crm/v3/objects/{object_type}/search", response_model=HubspotPage, openapi_extra=_B_SEARCH
)
async def search_objects(object_type: str, request: Request):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "Authentication credentials not found.", "INVALID_AUTHENTICATION")
    if not _known_type(request, object_type):
        return _error(404, f"Unable to infer object type from: {object_type}", "OBJECT_NOT_FOUND")
    body = await json_body(request)
    limit = _clamp(body.get("limit"), 10, _PAGE_MAX)
    visible = auth.visible_ids(request, caller)
    conn = auth.conn(request)
    after_doc, err = _resolve_cursor(request, body.get("after"))
    if err is not None:
        return err

    # Filters may name ANY property, so they are evaluated over the JSON column rather than compiled
    # to SQL; the object-type and ACL predicates stay in SQL. The whole object type is matched on
    # every request, NOT just the rows past the cursor: `total` is a property of the query, so it
    # must not shrink as the caller pages. `sorts` then orders the full match set, which is the only
    # place a stable order can be established.
    # Read only the columns the scan will actually use. `title`/`content` are needed solely to match
    # a free-text `query`, and `content` is the widest column there is (a note's whole body), so
    # pulling it for every row of a 69k-row object type dwarfs the filtering itself.
    cols = "doc_id, object_type, properties, archived, created_ts, updated_ts, owner_display" + (
        ", title, content" if (body.get("query") or "").strip() else ""
    )
    pre = _sql_prefilter(body)
    hits: list = []
    cursor = None
    while True:
        batch = store.list_hubspot_objects(
            conn,
            object_type,
            after_doc_id=cursor,
            visible_ids=visible,
            limit=2000,
            columns=cols,
            prefilter=pre,
        )
        if not batch:
            break
        cursor = batch[-1]["doc_id"]
        hits += [r for r in batch if _matches(r, body)]
    total = len(hits)
    hits = _sorted(hits, body.get("sorts"))

    if after_doc is not None:
        ids = [r["doc_id"] for r in hits]
        if after_doc not in ids:
            return _error(400, f"Invalid 'after' cursor: {body.get('after')}")
        hits = hits[ids.index(after_doc) + 1 :]
    out = _page(hits, limit, _keep(body.get("properties")))
    out["total"] = total
    return out


@router.post(
    "/crm/v3/objects/{object_type}/batch/read", response_model=HubspotPage, openapi_extra=_B_BATCH
)
async def batch_read(object_type: str, request: Request):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "Authentication credentials not found.", "INVALID_AUTHENTICATION")
    if not _known_type(request, object_type):
        return _error(404, f"Unable to infer object type from: {object_type}", "OBJECT_NOT_FOUND")
    body = await json_body(request)
    conn, visible = auth.conn(request), auth.visible_ids(request, caller)
    keep = _keep(body.get("properties"))
    results, errors = [], []
    for item in body.get("inputs") or []:
        rid = str(item.get("id"))
        doc_id = _doc_id_for(request, rid)
        row = store.get_document(conn, "hubspot", doc_id, visible) if doc_id else None
        if row is None or row["object_type"] != object_type:
            errors.append(
                {
                    "status": "error",
                    "category": "OBJECT_NOT_FOUND",
                    "message": f"Could not get some {object_type}. Some of the ids provided "
                    f"were not found.",
                    "context": {"id": [rid]},
                }
            )
            continue
        results.append(_record(row, keep))
    # A partial batch is **207** with `numErrors` + `errors`, and `status` stays COMPLETE — its
    # allowed values are PENDING/PROCESSING/CANCELED/COMPLETE, so inventing "PARTIAL" would make the
    # official client deserialize into the no-errors model and drop the error detail on the floor.
    out: dict = {"status": "COMPLETE", "results": results}
    if not errors:
        return out
    out["numErrors"] = len(errors)
    out["errors"] = errors
    return JSONResponse(status_code=207, content=out)


@router.get(
    "/crm/v4/objects/{object_type}/{record_id}/associations/{to_object_type}",
    response_model=HubspotPage,
    openapi_extra={"parameters": _P_ASSOC},
)
async def list_associations(
    object_type: str, record_id: str, to_object_type: str, request: Request
):
    caller = auth.resolve_bearer(request)
    if caller is None:
        return _error(401, "Authentication credentials not found.", "INVALID_AUTHENTICATION")
    doc_id = _doc_id_for(request, record_id)
    conn, visible = auth.conn(request), auth.visible_ids(request, caller)
    row = store.get_document(conn, "hubspot", doc_id, visible) if doc_id else None
    if row is None or row["object_type"] != object_type:
        return _error(404, "resource not found", "OBJECT_NOT_FOUND")
    limit = _clamp(request.query_params.get("limit"), _ASSOC_PAGE_MAX, _ASSOC_PAGE_MAX)
    after_to, err = _resolve_cursor(request, request.query_params.get("after"))
    if err is not None:
        return err
    # limit+1 for the same reason listings do it: the extra row is the only evidence of a further
    # page, and without paging here every association past the first page would be unreachable.
    rows = store.hubspot_associations(
        conn, doc_id, to_object_type, after_to_doc_id=after_to, visible_ids=visible, limit=limit + 1
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    out: dict = {
        "results": [
            {
                "toObjectId": synth.hubspot_record_id(r["to_doc_id"]),
                "associationTypes": [
                    {
                        "category": r["assoc_category"],
                        "typeId": r["assoc_type_id"],
                        "label": r["label"],
                    }
                ],
            }
            for r in rows
        ]
    }
    if has_more:
        after = out["results"][-1]["toObjectId"]
        out["paging"] = {"next": {"after": after, "link": f"?after={after}"}}
    return out
