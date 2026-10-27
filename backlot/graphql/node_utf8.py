"""A JavaScript string as a vendor's Node server writes it to its database: as UTF-8, a lone
surrogate, which a JavaScript string can hold and UTF-8 cannot, written as U+FFFD, as
``Buffer.from`` writes it. Both vendors' GraphQL servers run on Node, and on 2026-10-27 (UTC)
api.linear.app answered ``issue(id:)``, ``team(id:)`` and an ``issues`` filter, and api.fireflies.ai
``transcript(id:)``, ``user(id:)`` and ``transcripts(keyword:)``, with ``a\\ud800b`` as with
``a\\ufffdb``. :class:`Written` binds a statement's strings so, as Python's ``sqlite3`` encodes
no lone surrogate.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def holds_a_lone_surrogate(value: str) -> bool:
    """Whether ``value`` holds a surrogate, which in a string the body reader made is a lone one:
    :func:`backlot.graphql.body.parse` joins a pair into the one character it encodes."""
    return _LONE_SURROGATE.search(value) is not None


def written(value: Any) -> Any:
    """``value`` as Node writes it: a string with each lone surrogate replaced by U+FFFD, and
    anything else as it is."""
    if isinstance(value, str):
        return _LONE_SURROGATE.sub("\ufffd", value)
    return value


class Written:
    """``conn`` with each parameter a statement binds :func:`written` first."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        if isinstance(parameters, dict):
            parameters = {name: written(value) for name, value in parameters.items()}
        else:
            parameters = [written(value) for value in parameters]
        return self._conn.execute(sql, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)
