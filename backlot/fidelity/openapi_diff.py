"""Comparing Backlot against an OpenAPI document its vendor publishes.

Six vendors publish one, all of them public, which is what lets this run with no credential, no
quota and no account, and to be reproduced locally by anyone. Where a document is found is not this
module's business: a comparison hands over a URL, or a hook that produces one.

Response bodies are deliberately out of scope. A vendor's document describes them through deep
``$ref`` chains that Backlot's ``response_model`` set does not mirror shape-for-shape, so a body
diff would report how two documents are written rather than how two servers answer.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol

from backlot.fidelity.errors import FidelityError
from backlot.fidelity.fetch import fetch_json
from backlot.fidelity.findings import Finding
from backlot.fidelity.operations import (
    _METHODS,
    Operation,
    _query_params,
    _resolve,
    canonical,
    diff_operations,
    from_backlot,
)


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


class SpecTarget(Protocol):
    """What this module needs of ONE document. Structural, so the registry can import this module
    without this module importing the registry."""

    spec_url: str
    mount: tuple[str, ...]
    strip: str
    resolve_url: "Callable[[Mapping[str, Any]], str] | None"


def fetch_spec(spec: SpecTarget, *, timeout: float = 120.0) -> dict:
    """The vendor's published document, following an index when that is how it is addressed."""
    doc = fetch_json(spec.spec_url, timeout=timeout)
    if spec.resolve_url is not None:
        return fetch_json(spec.resolve_url(doc), timeout=timeout)
    return doc


def divergences(spec: SpecTarget, *, timeout: float = 120.0) -> list[Finding]:
    """This module's entry point: load both contracts and compare them.

    ONE document. A source published as several — Jira's v2 and v3 — is the caller's
    concatenation, not this module's: the two contracts here are a document and the paths it speaks
    for, and a second document is a second pair with its own mount and its own strip."""
    from backlot.main import app

    vendor = from_openapi(fetch_spec(spec, timeout=timeout))
    if not vendor:
        raise FidelityError(
            f"{spec.spec_url} declared no operations; is it still an OpenAPI document?"
        )
    served = from_backlot(app.openapi(), spec.mount, spec.strip)
    return diff_operations(served, vendor)
