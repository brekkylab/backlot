"""Comparing Backlot against an OpenAPI document its vendor publishes.

Six vendors publish one, all of them public, which is what lets this run with no credential, no
quota and no account — and on a fork's pull request. Where a document is found is not this
module's business: a comparison hands over a URL, or a hook that produces one.

Response bodies are deliberately out of scope. A vendor's document describes them through deep
``$ref`` chains that Backlot's ``response_model`` set does not mirror shape-for-shape, so a body
diff would report how two documents are written rather than how two servers answer.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Protocol

from backlot.fidelity.errors import FidelityError
from backlot.fidelity.fetch import fetch_json
from backlot.fidelity.findings import Finding
from backlot.fidelity.operations import (
    Operation,
    _METHODS,
    canonical,
    diff_operations,
    from_backlot,
)


def _resolve(node: Any, root: Mapping[str, Any]) -> Any:
    """Follow a local ``$ref``. Vendor specs declare shared parameters once and point at them."""
    seen = 0
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            return {}
        seen += 1
        if seen > 20:  # a spec that points at itself must not hang the comparison
            return {}
        node = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                return {}
            node = node[part]
    return node


def _query_params(entries: Iterable[Any], root: Mapping[str, Any]) -> set[str]:
    out = set()
    for entry in entries or ():
        p = _resolve(entry, root)
        if isinstance(p, dict) and p.get("in") == "query" and p.get("name"):
            out.add(p["name"])
    return out


def from_openapi(spec: Mapping[str, Any]) -> dict[tuple[str, str], Operation]:
    """Operations declared by an OpenAPI 3 or Swagger 2 document."""
    ops: dict[tuple[str, str], Operation] = {}
    for path, item in (spec.get("paths") or {}).items():
        item = _resolve(item, spec)
        if not isinstance(item, dict):
            continue
        shared = _query_params(item.get("parameters"), spec)
        for method in _METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params = shared | _query_params(op.get("parameters"), spec)
            o = Operation(method, canonical(path), frozenset(params))
            ops[o.key] = o
    return ops


class OpenAPITarget(Protocol):
    """What this module needs of a comparison. Structural, so the registry can import this module
    without this module importing the registry."""

    spec_url: str
    mount: tuple[str, ...]
    strip: str
    resolve_url: "Callable[[Mapping[str, Any]], str] | None"


def fetch_spec(comparison: OpenAPITarget, *, timeout: float = 120.0) -> dict:
    """The vendor's published document, following an index when that is how it is addressed."""
    doc = fetch_json(comparison.spec_url, timeout=timeout)
    if comparison.resolve_url is not None:
        return fetch_json(comparison.resolve_url(doc), timeout=timeout)
    return doc


def divergences(comparison: OpenAPITarget, *, timeout: float = 120.0) -> list[Finding]:
    """This module's entry point: load both contracts and compare them."""
    from backlot.main import app

    vendor = from_openapi(fetch_spec(comparison, timeout=timeout))
    if not vendor:
        raise FidelityError(
            f"{comparison.spec_url} declared no operations; is it still an OpenAPI document?"
        )
    served = from_backlot(app.openapi(), comparison.mount, comparison.strip)
    return diff_operations(served, vendor)
