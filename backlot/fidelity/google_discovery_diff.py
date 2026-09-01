"""Comparing Backlot against a Google API Discovery document.

Google does not publish OpenAPI. Its own format describes the same thing — which operations exist
and what each accepts — but says it differently enough that sharing a parser with OpenAPI would
mean a parser that understands neither well:

* methods hang off nested ``resources`` rather than off a flat path map;
* a method's ``path`` is relative to ``servicePath`` — empty for Gmail, ``drive/v3/`` for Drive;
* the standard parameters (``fields``, ``alt``, ``prettyPrint``) are declared ONCE at the top of
  the document rather than on each method. Reading only the per-method block reports every one of
  them as surface Backlot invented, which is how this comparison first accused Drive's ``fields``.

Like OpenAPI, these documents are public: no credential, no quota, no account.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from backlot.fidelity.errors import FidelityError
from backlot.fidelity.fetch import fetch_json
from backlot.fidelity.findings import Finding
from backlot.fidelity.operations import Operation, canonical, diff_operations, from_backlot


def from_google_discovery(doc: Mapping[str, Any]) -> dict[tuple[str, str], Operation]:
    """Operations declared by a Google API Discovery document.

    Methods hang off nested ``resources`` rather than off a flat path map, and a method's ``path``
    is relative to ``servicePath`` — empty for Gmail, ``drive/v3/`` for Drive — so the two have to
    be joined before anything lines up with what Backlot serves.
    """
    service_path = doc.get("servicePath") or ""
    # Google declares `fields`, `alt`, `prettyPrint` and the rest ONCE at the top of the document,
    # not on each method. Reading only the per-method block reports every one of them as surface
    # Backlot invented, which is how this comparison first accused Drive's `fields` projection.
    common = {
        name
        for name, p in (doc.get("parameters") or {}).items()
        if isinstance(p, dict) and p.get("location") == "query"
    }
    ops: dict[tuple[str, str], Operation] = {}

    def visit(node: Mapping[str, Any]) -> None:
        for method in (node.get("methods") or {}).values():
            if not isinstance(method, dict) or "path" not in method:
                continue
            params = {
                name
                for name, p in (method.get("parameters") or {}).items()
                if isinstance(p, dict) and p.get("location") == "query"
            }
            o = Operation(
                method.get("httpMethod", "GET").lower(),
                canonical(service_path + method["path"]),
                frozenset(params | common),
            )
            ops[o.key] = o
        for child in (node.get("resources") or {}).values():
            if isinstance(child, dict):
                visit(child)

    visit(doc)
    return ops


class GoogleDiscoveryTarget(Protocol):
    """What this module needs of a comparison. Structural, so the registry can import this module
    without this module importing the registry."""

    spec_url: str
    mount: tuple[str, ...]
    strip: str


def divergences(comparison: GoogleDiscoveryTarget, *, timeout: float = 120.0) -> list[Finding]:
    """This module's entry point: load both contracts and compare them."""
    from backlot.main import app

    vendor = from_google_discovery(fetch_json(comparison.spec_url, timeout=timeout))
    if not vendor:
        raise FidelityError(
            f"{comparison.spec_url} declared no operations; is it still a Discovery document?"
        )
    served = from_backlot(app.openapi(), comparison.mount, comparison.strip)
    return diff_operations(served, vendor)
