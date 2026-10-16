"""The operations a vendor publishes and no route here serves.

`backlot diff` keeps one baseline per source, the file ``backlot.fidelity.baseline_path`` names, and
each operation in it whose status is ``missing`` is one the vendor's description publishes that
Backlot does not serve, written ``METHOD /path`` with each path parameter as ``{}``. Notion's and
GitHub's routers read the list from here to answer such an operation the way the vendor answers
ahead of running one, so an operation the vendor adds gets that answer once the baseline
acknowledges it.

It reads the file itself: importing ``backlot.fidelity`` would load the comparison modules, which
its ``__init__`` imports.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

#: (method, pattern over the vendor's own path) for each operation.
Operations = tuple[tuple[str, re.Pattern[str]], ...]

_BASELINES = Path(__file__).resolve().parent / "fidelity" / "baseline"


def operations(rows: Iterable[str]) -> Operations:
    """(method, vendor-path pattern) for each row, each ``{}`` one path segment."""
    ops = []
    for row in rows:
        method, path = row.split(" ", 1)
        pattern = "[^/]+".join(re.escape(part) for part in path.split("{}"))
        ops.append((method, re.compile(pattern)))
    return tuple(ops)


def missing_operations(source: str) -> list[str]:
    """The ``missing`` operations of ``source``'s baseline, each ``METHOD /path``."""
    baseline = _BASELINES / f"{source}.json"
    entries = json.loads(baseline.read_text(encoding="utf-8"))["operations"]
    return [e["operation"] for e in entries if e["status"] == "missing"]


def not_served(source: str) -> Operations:
    """:func:`operations` over :func:`missing_operations`."""
    return operations(missing_operations(source))


def includes(ops: Operations, method: str, path: str) -> bool:
    """Whether ``method`` on ``path`` is one of ``ops``."""
    return any(verb == method and pattern.fullmatch(path) for verb, pattern in ops)
