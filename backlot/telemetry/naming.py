"""Which source, vendor operation and support state a request is, read off Backlot's own baselines.

Each fidelity baseline (``backlot/fidelity/baseline/<source>.json``) records every operation of the
vendor's contract and every one Backlot serves, with the vendor's id for it and a status. A call is
named by that id, so a name is always one the vendor's contract or Backlot's routes define and never
anything the request itself spelled. The mounts and strips that place a served path in a vendor
document are the fidelity comparisons' own (``backlot.fidelity.comparisons``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from graphql import (
    FieldNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    OperationDefinitionNode,
    parse,
)

from backlot.fidelity.baselines import Baseline
from backlot.fidelity.comparisons import (
    CSDL,
    GOOGLE_DISCOVERY,
    GRAPHQL,
    OPENAPI,
    PROBE,
    SLACK,
    UNCOMPARED,
    baseline_path,
)
from backlot.fidelity.operations import canonical

IMPLEMENTED, EXTRA, REFUSED, MISSING, UNKNOWN = (
    "implemented",
    "extra",
    "refused",
    "missing",
    "unknown",
)
# When two baseline rows fit a request equally well, the one Backlot serves is the one it answered.
_PREFERENCE = {IMPLEMENTED: 0, EXTRA: 1, REFUSED: 2, MISSING: 3}
# FastAPI's own documentation routes, matched exactly: Google Docs is mounted at `/docs/v1`.
_FASTAPI_DOCS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})
# The shape the collector keeps an operation name in; a template outside it (Sheets'
# `values:batchGet`) is left `unmatched` rather than replaced there with `invalid`.
_OPERATION = re.compile(r"^[A-Za-z_][A-Za-z0-9_.,/+{}-]{0,127}$")
# Introspection is answered by the GraphQL engine for every schema; no baseline lists it.
_INTROSPECTION = {"__schema", "__type", "__typename"}
_S3_ROW = re.compile(
    r"^(?P<name>\w+): (?P<method>[A-Z]+) /\{(?P<target>\w+)\}(?:\?(?P<query>.*))?$"
)


@dataclass(frozen=True)
class Named:
    source: str
    operation: str
    support: str


@dataclass(frozen=True)
class _Template:
    segments: tuple[str, ...]
    id: str
    status: str

    @property
    def literals(self) -> int:
        return sum(seg != "{}" for seg in self.segments)


@dataclass(frozen=True)
class _S3Row:
    id: str
    status: str
    method: str
    target: str
    selectors: tuple[tuple[str, str | None], ...]


def own_route(path: str) -> bool:
    """Backlot's own surface: health, the user directory, the token exchange, the API docs."""
    return path in _FASTAPI_DOCS or any(
        path == p or path.startswith(p.rstrip("/") + "/") for p in UNCOMPARED
    )


class Namer:
    """Reads every baseline once, on the first request that needs it."""

    def __init__(self) -> None:
        self._loaded = False
        # (prefix, strip, source), longest prefix first.
        self._mounts: list[tuple[str, str, str]] = []
        self._by_key: dict[str, dict[tuple[str, str], tuple[str, str]]] = {}
        self._templates: dict[str, dict[str, list[_Template]]] = {}
        self._s3: list[_S3Row] = []
        self._graphql: dict[str, dict[str, str]] = {}
        # Slack's methods by name: a method is one operation whatever the verb.
        self._slack: dict[str, str] = {}
        self._graphql_paths = {f"/{name}/graphql": name for name in GRAPHQL}
        # The API a batch path names (`/batch/drive/v3`), read off each Google document's mount.
        self._batch_api: dict[str, str] = {}

    # ---------------------------------------------------------------------------------- loading

    def _load(self) -> None:
        if self._loaded:
            return
        for name, comparison in {**OPENAPI, **GOOGLE_DISCOVERY}.items():
            for spec in comparison.specs:
                for prefix in spec.mount:
                    self._mounts.append((prefix, spec.strip, name))
            self._index(name, Baseline.load(baseline_path(name)).operations or ())
        for name, comparison in GOOGLE_DISCOVERY.items():
            for spec in comparison.specs:
                for prefix in spec.mount:
                    self._batch_api.setdefault(prefix.strip("/").split("/")[0], name)
        for prefix in SLACK["slack"].mount:
            self._mounts.append((prefix, prefix, "slack"))
        for entry in Baseline.load(baseline_path("slack")).operations or ():
            self._slack[entry.operation] = entry.status
        # A CSDL source's operations are its baseline's path half, under the mount the vendor's
        # paths start below (`/msgraph/v1.0/teams/...` is Graph's `/teams/...`).
        for name, comparison in CSDL.items():
            for prefix in comparison.mount:
                self._mounts.append((prefix, prefix, name))
            self._index(name, Baseline.load(baseline_path(name)).operations or ())
        for prefix in PROBE["s3"].mount:
            self._mounts.append((prefix, prefix, "s3"))
        for entry in Baseline.load(baseline_path("s3")).operations or ():
            m = _S3_ROW.match(entry.operation)
            if m and entry.id:
                pairs = [p for p in (m["query"] or "").split("&") if p]
                selectors = tuple((k, v or None) for k, _, v in (p.partition("=") for p in pairs))
                self._s3.append(_S3Row(entry.id, entry.status, m["method"], m["target"], selectors))
        for name in GRAPHQL:
            raw = json.loads(baseline_path(name).read_text(encoding="utf-8"))
            fields = raw["types"][raw["root"]["query"]]["fields"]
            self._graphql[name] = {field: entry["status"] for field, entry in fields.items()}
        self._mounts.sort(key=lambda m: len(m[0]), reverse=True)
        self._loaded = True

    def _index(self, source: str, entries: Iterable[Any]) -> None:
        by_key = self._by_key.setdefault(source, {})
        templates = self._templates.setdefault(source, {})
        for entry in entries:
            method, _, path = entry.operation.partition(" ")
            key = (method.upper(), canonical(path))
            by_key[key] = (entry.id, entry.status)
            if entry.id:
                segments = tuple(key[1].split("/")) if key[1] else ()
                templates.setdefault(key[0], []).append(_Template(segments, entry.id, entry.status))

    # ---------------------------------------------------------------------------------- naming

    def source_of(self, path: str) -> tuple[str, str] | None:
        """The source whose mount ``path`` falls under, and the prefix its document omits."""
        self._load()
        if path in self._graphql_paths:
            return self._graphql_paths[path], ""
        if _under(path, "/batch"):
            # `/batch/{api}/{version}` names its API; the bare `/batch` is Gmail's batch path.
            if path.count("/") < 2:
                return "gmail", ""
            api = self._batch_api.get(path.split("/")[2])
            return (api, "") if api else None
        for prefix, strip, name in self._mounts:
            if _under(path, prefix):
                return name, strip
        return None

    def name(
        self,
        *,
        method: str,
        path: str,
        route: Any,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        graphql_payload: Any = None,
    ) -> Named:
        self._load()
        placed = self.source_of(path)
        if placed is None:
            return Named("other", "unmatched", UNKNOWN)
        source, strip = placed
        if source in self._graphql:
            return self._graphql_call(source, graphql_payload)
        if source == "s3":
            return self._s3_call(method, path[len(strip) :], query, headers)
        if path.startswith("/batch"):
            return Named(source, "batch", IMPLEMENTED)
        # A catch-all route (`/{rest:path}`) answers what no route serves; a 405 matched the path
        # under another method. Neither is a served operation.
        methods = getattr(route, "methods", None) or set() if route is not None else set()
        rest = path[len(strip) :] if path.startswith(strip) else path
        if not _catch_all(route) and (method in methods or (method == "HEAD" and "GET" in methods)):
            # A route's `{param}` can take a literal segment of an operation it does not serve
            # (`issues/{issue_number}` takes `issues/comments`). The vendor's router matches the
            # literal, so a baseline row with more literals than the route names the call.
            best = self._best(source, rest, method)
            if best is not None and best.literals > _literals(_route_key(route.path, strip)):
                return Named(source, best.id, best.status)
            return self._served(source, strip, method, route)
        if source == "slack":
            name = canonical(rest)
            if name in self._slack:
                return Named(source, name, self._slack[name])
            return Named(source, "unmatched", UNKNOWN)
        best = self._best(source, rest, method)
        if best is not None:
            return Named(source, best.id, best.status)
        # A path a route serves under other methods (a 405) is named by a template, never by what
        # the request spelled: the route's, or a vendor template under any method with more
        # literal segments, since the first route whose `{param}` takes the path is the one that
        # answers (`…/{}/{}` takes `…/{}/search`), as with a served call.
        if not _catch_all(route):
            template = _route_key(route.path, strip)
            literal = self._best(source, rest, None)
            if literal is not None and literal.literals > _literals(template):
                template = "/".join(literal.segments)
            if _OPERATION.match(template):
                return Named(source, template, UNKNOWN)
        return Named(source, "unmatched", UNKNOWN)

    def _served(self, source: str, strip: str, method: str, route: Any) -> Named:
        key = _route_key(route.path, strip)
        if source == "slack":
            return Named(source, key, IMPLEMENTED)
        # An operation the vendor's contract does not describe has no vendor id, and is named by
        # its path template (`wiki/rest/api/content/{}`), which a refactor of the route leaves be.
        for m in (method, "GET") if method == "HEAD" else (method,):
            hit = self._by_key.get(source, {}).get((m, key))
            if hit:
                op_id, status = hit
                return Named(source, op_id or key, status)
        return Named(source, key, EXTRA)

    def _best(self, source: str, rest: str, method: str | None) -> _Template | None:
        """The vendor template ``rest`` fits with the most literal segments, the served one first
        among equals; under any method where ``method`` is None."""
        segments = tuple(canonical(rest).split("/")) if canonical(rest) else ()
        methods = self._templates.get(source, {})
        if method is None:
            pool = [t for templates in methods.values() for t in templates]
        else:
            pool = methods.get(method, []) + (methods.get("GET", []) if method == "HEAD" else [])
        best: _Template | None = None
        for t in pool:
            if len(t.segments) != len(segments):
                continue
            if all(a == "{}" or a == b for a, b in zip(t.segments, segments)):
                if best is None or (t.literals, -_PREFERENCE[t.status]) > (
                    best.literals,
                    -_PREFERENCE[best.status],
                ):
                    best = t
        return best

    def _s3_call(
        self, method: str, rest: str, query: Mapping[str, str], headers: Mapping[str, str]
    ) -> Named:
        bucket, _, key = rest.lstrip("/").partition("/")
        target = "object" if key else "bucket" if bucket else "service"
        copying = "x-amz-copy-source" in headers
        best: _S3Row | None = None
        for row in self._s3:
            if row.method != method or row.target != target:
                continue
            if not all(k in query and (v is None or query[k] == v) for k, v in row.selectors):
                continue
            rank = (len(row.selectors), ("Copy" in row.id) == copying, -_PREFERENCE[row.status])
            if best is None or rank > (
                len(best.selectors),
                ("Copy" in best.id) == copying,
                -_PREFERENCE[best.status],
            ):
                best = row
        if best is None:
            return Named("s3", "unmatched", UNKNOWN)
        return Named("s3", best.id, best.status)

    def _graphql_call(self, source: str, payload: Any) -> Named:
        """``payload`` is the request body as the route read it, None where it read none."""
        fields = self._graphql[source]
        query = payload.get("query") if isinstance(payload, dict) else None
        try:
            document = parse(query) if isinstance(query, str) else None
        except Exception:  # noqa: BLE001 — anything unreadable is one answer
            document = None
        if document is None:
            return Named(source, "unparsed", UNKNOWN)
        operations = [d for d in document.definitions if isinstance(d, OperationDefinitionNode)]
        wanted = payload.get("operationName")
        chosen = next((o for o in operations if o.name and o.name.value == wanted), None) or (
            operations[0] if len(operations) == 1 or not wanted and operations else None
        )
        if chosen is None:
            return Named(source, "unparsed", UNKNOWN)
        kind = chosen.operation.value
        if kind != "query":
            return Named(source, kind, UNKNOWN)
        fragments = {
            d.name.value: d for d in document.definitions if hasattr(d, "type_condition") and d.name
        }
        roots = sorted(set(_root_fields(chosen.selection_set.selections, fragments)))
        if not roots or any(r not in fields and r not in _INTROSPECTION for r in roots):
            return Named(source, "query.unmatched", UNKNOWN)
        states = {fields.get(r, IMPLEMENTED) for r in roots}
        support = IMPLEMENTED if states <= {IMPLEMENTED} else MISSING
        return Named(source, "query." + ",".join(roots), support)


def _route_key(route_path: str, strip: str) -> str:
    """A route's path as its source's documents spell it: the mount's strip off, `{}` for each
    parameter."""
    return canonical(
        route_path[len(strip) :] if strip and route_path.startswith(strip) else route_path
    )


def _literals(key: str) -> int:
    return sum(seg != "{}" for seg in key.split("/")) if key else 0


def _catch_all(route: Any) -> bool:
    """No route, or one (`/{rest:path}`) that answers whatever no other route serves."""
    return "{rest" in getattr(route, "path", "{rest")


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


# Nodes visited per document. Fragments that each spread the next k times expand to k**depth
# visits: 41 million for four levels at k=80, which a 2 KB body spells.
_ROOT_FIELD_BUDGET = 10_000


def _root_fields(
    selections: Iterable[Any],
    fragments: Mapping[str, Any],
    depth: int = 0,
    budget: list[int] | None = None,
) -> list[str]:
    budget = [_ROOT_FIELD_BUDGET] if budget is None else budget
    out: list[str] = []
    for node in selections:
        if budget[0] <= 0:
            break
        budget[0] -= 1
        if isinstance(node, FieldNode):
            out.append(node.name.value)
        elif isinstance(node, InlineFragmentNode) and depth < 5:
            out += _root_fields(node.selection_set.selections, fragments, depth + 1, budget)
        elif isinstance(node, FragmentSpreadNode) and depth < 5 and node.name.value in fragments:
            out += _root_fields(
                fragments[node.name.value].selection_set.selections, fragments, depth + 1, budget
            )
    return out


def surface() -> dict[tuple[str, str], int]:
    """How many vendor operations each source's baseline records, by support state: the
    denominator of coverage, as this Backlot knows it. An operation named under several rows (Jira's
    v2 and v3) counts once, in its best state; a served route with no vendor id
    is not one of the vendor's operations and is left out."""
    best: dict[tuple[str, str], str] = {}
    for name in (*OPENAPI, *SLACK, *GOOGLE_DISCOVERY, *CSDL, "s3"):
        for entry in Baseline.load(baseline_path(name)).operations or ():
            key = (name, entry.id)
            if entry.id and (key not in best or _PREFERENCE[entry.status] < _PREFERENCE[best[key]]):
                best[key] = entry.status
    for name in GRAPHQL:
        raw = json.loads(baseline_path(name).read_text(encoding="utf-8"))
        for field, entry in raw["types"][raw["root"]["query"]]["fields"].items():
            best[(name, field)] = entry["status"]
    counts: dict[tuple[str, str], int] = {}
    for (name, _), status in best.items():
        counts[(name, status)] = counts.get((name, status), 0) + 1
    return counts
