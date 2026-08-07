"""Derive an MCP-ready OpenAPI spec from the mock's own ``/openapi.json``.

For vendor fidelity several routes accept **more than one HTTP method on a single route** — every
Slack Web API method takes GET and POST (``api_route(methods=["GET","POST"])``), Jira's
``search/jql`` likewise on both its ``/rest/api/2`` and ``/rest/api/3`` aliases, S3's object route
GET and HEAD. FastAPI derives one operationId *per route* and reuses it for each method, so the raw
``/openapi.json`` ends up with ~14 *duplicate* operationIds. An OpenAPI→MCP bridge keys its tools by
operationId, so a raw spec would collide (one tool silently overwriting the other).

This can't be fixed with FastAPI's operationId hooks (``operation_id=`` / ``generate_unique_id_function``):
those run *per route* and return a single id for all of a route's methods — there is no per-method
hook, so a GET+POST route is inherently one id. Splitting every such route into single-method routes
would be invasive and still leave a redundant GET-tool + POST-tool pair. Instead this module keeps
the fidelity-shaped routes as-is and, for MCP consumers, slices the spec to one source's paths and
collapses each route's methods to one callable operation (prefer GET, then fewest path params, then
the lexicographically greatest path). Served at ``GET /_mock/openapi/{source}`` so a bridge consumes
it directly — no client-side spec surgery. S3 is intentionally absent: it is SigV4-signed, which a
static bridge auth header can't produce.
"""

from __future__ import annotations

import re
import warnings

# Building the app's /openapi.json (FastAPI) warns "Duplicate Operation ID" once per multi-method
# route described above. Those duplicates are expected and are collapsed by build_mcp_spec, so the
# warning is pure noise — silence just that message. Lives here (imported by backlot.main before any
# spec is built) beside the rationale for the duplicates it concerns.
warnings.filterwarnings("ignore", message="Duplicate Operation ID", category=UserWarning)

# source -> the path prefix(es) whose operations that source's MCP server should expose.
SOURCE_PREFIXES: dict[str, list[str]] = {
    "github": ["/github"],
    "slack": ["/slack/api"],
    "gmail": ["/gmail"],
    "gdrive": ["/drive"],
    "notion": ["/notion/v1"],
    "atlassian": ["/atlassian", "/wiki"],
    "hubspot": ["/hubspot"],
}

_METHODS = ("get", "post", "put", "delete", "patch")
_METHOD_RANK = {m: i for i, m in enumerate(_METHODS)}


def unique_operation_id(route) -> str:
    """A route's operationId — replaces FastAPI's default, which is not deterministic.

    The default suffixes the id with ``list(route.methods)[0]``, and ``route.methods`` is a SET, so
    every route declared with more than one method (each Slack method, Jira's ``search/jql`` on both
    its v2 and v3 aliases, S3's object route) got a suffix that depended on ``PYTHONHASHSEED`` and
    changed between restarts. That is not cosmetic here: an OpenAPI->MCP bridge keys its tools by
    operationId (see :func:`build_mcp_spec`), so a bridge that caches tool names saw them move under
    it whenever the server restarted.

    The method is chosen by ``_METHOD_RANK`` — GET first, the same preference
    :func:`dedupe_operations` applies — so the operation that survives the collapse is also the one
    whose id names its own method. A method the rank does not know sorts after the known ones, by
    name, so it is stable too.
    """
    ident = re.sub(r"\W", "_", f"{route.name}{route.path_format}")
    method = min(
        route.methods, key=lambda m: (_METHOD_RANK.get(m.lower(), len(_METHODS)), m.lower())
    )
    return f"{ident}_{method.lower()}"


def qp(name: str, typ: str = "string", required: bool = False) -> dict:
    """One OpenAPI query parameter, for a router's ``openapi_extra``.

    The routers read their query params off the raw request rather than through FastAPI signatures
    (a vendor's parameter names are not always valid Python, and several are conditional), so each
    one has to declare what it honours by hand. This is that declaration — and only for parameters
    the mock actually honours: advertising one it ignores makes a client ask for data that never
    arrives, which is worse than not offering it.
    """
    return {"name": name, "in": "query", "required": required, "schema": {"type": typ}}


def slice_spec(spec: dict, prefixes: list[str]) -> dict:
    """Copy ``spec`` keeping only paths under one of ``prefixes``."""
    paths = {
        p: item
        for p, item in spec.get("paths", {}).items()
        if any(p == pre or p.startswith(pre + "/") for pre in prefixes)
    }
    if not paths:
        raise ValueError(f"no paths matched {prefixes} — is the router enriched/mounted?")
    return {**spec, "paths": paths}


def dedupe_operations(spec: dict) -> dict:
    """Keep one operation per operationId (the mock aliases the same op for fidelity).

    Preference: GET before POST/…, then fewest path params, then lexicographically greatest path
    (so ``/rest/api/3`` beats ``/rest/api/2``; ``/batch`` beats ``/batch/{api}/{version}``)."""
    cand: dict[str, list[tuple[str, str]]] = {}
    for path, item in spec.get("paths", {}).items():
        for method, op in item.items():
            if method in _METHODS and isinstance(op, dict) and "operationId" in op:
                cand.setdefault(op["operationId"], []).append((path, method))
    keep: set[tuple[str, str]] = set()
    for entries in cand.values():
        best_rank = min(_METHOD_RANK[m] for _, m in entries)
        finalists = [(p, m) for p, m in entries if _METHOD_RANK[m] == best_rank]
        fewest = min(p.count("{") for p, _ in finalists)
        finalists = [(p, m) for p, m in finalists if p.count("{") == fewest]
        keep.add(max(finalists, key=lambda pm: pm[0]))
    new_paths: dict[str, dict] = {}
    for path, item in spec.get("paths", {}).items():
        kept = {k: v for k, v in item.items() if k not in _METHODS or (path, k) in keep}
        if any(k in _METHODS for k in kept):
            new_paths[path] = kept
    return {**spec, "paths": new_paths}


def _duplicate_operation_ids(spec: dict) -> list[str]:
    seen: dict[str, int] = {}
    for item in spec.get("paths", {}).values():
        for method, op in item.items():
            if method in _METHODS and isinstance(op, dict) and "operationId" in op:
                seen[op["operationId"]] = seen.get(op["operationId"], 0) + 1
    return sorted(k for k, n in seen.items() if n > 1)


def build_mcp_spec(full_spec: dict, source: str) -> dict:
    """The MCP-ready spec for ``source``: sliced to its paths, fidelity aliases collapsed.

    Raises ``KeyError`` for an unknown source and ``ValueError`` if any operationId collision
    survives (an invariant — dedupe should always resolve them)."""
    prefixes = SOURCE_PREFIXES[source]
    spec = dedupe_operations(slice_spec(full_spec, prefixes))
    dupes = _duplicate_operation_ids(spec)
    if dupes:
        raise ValueError(f"unresolved duplicate operationIds for {source!r}: {dupes}")
    return spec
