"""The unit a path-shaped contract is compared in, and the comparison itself.

A vendor that publishes its contract as a document — OpenAPI, or Google's Discovery format —
enumerates operations as paths. Reduced to a method, a path and the query parameters it accepts,
those line up against what Backlot serves regardless of which format they were read out of, so the
two parsers live apart from this and hand back the same thing.

Path templates are compared with their placeholders flattened: the vendor calling a segment
``{userId}`` and Backlot calling it ``{user_id}`` is not a divergence, and reporting it as one
would bury the operations that genuinely do not line up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from backlot.fidelity.findings import BREAKING, GAP, Finding
from backlot.fidelity.surface import EXTRA, IMPLEMENTED, MISSING, OperationEntry, OperationSurface

_PLACEHOLDER = re.compile(r"\{[^}]*\}")
_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def canonical(path: str) -> str:
    """A path both sides can be compared on: no leading slash, placeholders flattened."""
    return _PLACEHOLDER.sub("{}", path).strip("/")


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    params: frozenset[str]
    # What the vendor's document calls the operation; empty on Backlot's side.
    id: str = ""
    # The message it answers with, where the document names one: a discovery method's `response`.
    response: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)

    def __str__(self) -> str:
        return f"{self.method.upper()} /{self.path}"


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


def from_backlot(
    spec: Mapping[str, Any], mount: Iterable[str], strip: str = ""
) -> dict[tuple[str, str], Operation]:
    """The operations Backlot serves for one source, taken from the app's own ``/openapi.json``.

    ``mount`` selects the paths belonging to this source; ``strip`` is the part of them the vendor's
    own document does not repeat. The two differ per source and are not guessable: Jira is mounted
    at ``/atlassian/rest/api/3`` and its document spells ``/rest/api/3/...``, so ``/atlassian``
    comes off, while Gmail is mounted at ``/gmail`` and Google's document already spells
    ``gmail/v1/...``, so nothing does. Guessing here silently pairs the wrong operations.
    """
    mount = tuple(mount)
    ops: dict[tuple[str, str], Operation] = {}
    for path, item in (spec.get("paths") or {}).items():
        if not any(path.startswith(m) for m in mount):
            continue
        if strip and path.startswith(strip):
            path = path[len(strip) :] or "/"
        for method in _METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            o = Operation(
                method, canonical(path), frozenset(_query_params(op.get("parameters"), spec))
            )
            ops[o.key] = o
    return ops


def diff_operations(
    backlot: Mapping[tuple[str, str], Operation],
    real: Mapping[tuple[str, str], Operation],
) -> list[Finding]:
    """Every divergence in the request surface, both directions, operations and parameters."""
    out: list[Finding] = []
    for key in sorted(set(backlot) - set(real)):
        out.append(
            Finding(
                "extra_operation",
                BREAKING,
                str(backlot[key]),
                "the vendor's spec declares no such operation",
            )
        )
    for key in sorted(set(real) - set(backlot)):
        out.append(
            Finding(
                "missing_operation", GAP, str(real[key]), "the vendor serves it; Backlot does not"
            )
        )
    for key in sorted(set(backlot) & set(real)):
        m, r = backlot[key], real[key]
        for name in sorted(m.params - r.params):
            out.append(
                Finding(
                    "extra_param",
                    BREAKING,
                    f"{m}?{name}",
                    "Backlot accepts it; the vendor's spec declares no such parameter",
                )
            )
        for name in sorted(r.params - m.params):
            out.append(
                Finding(
                    "missing_param", GAP, f"{m}?{name}", "the vendor accepts it; Backlot does not"
                )
            )
    return sorted(out, key=lambda f: (f.severity != BREAKING, f.path, f.kind))


def inventory(
    backlot: Mapping[tuple[str, str], Operation],
    real: Mapping[tuple[str, str], Operation],
) -> OperationSurface:
    """One entry per operation either side has: the vendor's id for it, and which side has it.

    The id is only what the document declares, so a served route with no path item in the document
    has none.
    """
    out = []
    for key in sorted(set(backlot) | set(real)):
        op = backlot.get(key) or real[key]
        name = real[key].id if key in real else ""
        if key not in backlot:
            status = MISSING
        else:
            status = IMPLEMENTED if key in real else EXTRA
        response = real[key].response if key in real else ""
        out.append(OperationEntry(str(op), status, name, response))
    return OperationSurface(tuple(out))
