"""Keep a same-named script from shadowing the third-party package it imports.

A script named ``github.py`` or ``jira.py`` sits on ``sys.path`` (as ``sys.path[0]``, the
running script's own directory) right where Python looks first — so its own transitive
``import github`` / ``import jira`` would resolve to itself instead of the real package.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["drop_self_from_syspath"]


def drop_self_from_syspath(file: str) -> None:
    """Remove a script's own directory from sys.path so a file named `jira.py` / `github.py`
    doesn't shadow the third-party `jira` / `github` package it (transitively) imports."""
    here = Path(file).resolve().parent
    sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != here]
