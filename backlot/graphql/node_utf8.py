"""A string as the vendors' Node servers write it in UTF-8 to look it up or hash it: a lone
surrogate, which a JavaScript string can hold and UTF-8 cannot, as U+FFFD, the character
``Buffer.from`` writes for one.

Measured on 2026-10-27 (UTC), both vendors answered each of these string arguments, sent with
``\\ud800`` in it, as with ``\\ufffd`` in its place: ``issue(id:)``, ``team(id:)`` and ``issues``
title filters on Linear, ``transcript(id:)``, ``user(id:)`` and ``transcripts(keyword:)`` and
``(title:)`` on Fireflies, each with the surrogate after a value the workspace holds, and Linear's
``containsIgnoreCase`` title filter and Fireflies' keyword with it alone. Neither drops it: an id it
follows names nothing, and alone it matches nothing where an empty string matches each.
:class:`Written` binds a statement's strings so, as Python's ``sqlite3`` encodes no lone surrogate.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def holds_a_lone_surrogate(value: str) -> bool:
    """Whether ``value`` holds a surrogate, which in a string the body reader made is a lone one:
    :func:`backlot.graphql.body.parse` joins a pair into the one character it encodes."""
    return LONE_SURROGATE.search(value) is not None


def written(value: Any) -> Any:
    """``value`` as Node writes it: a string with each lone surrogate replaced by U+FFFD, and
    anything else as it is."""
    if isinstance(value, str):
        return LONE_SURROGATE.sub("\ufffd", value)
    return value


class Written:
    """``conn`` with each parameter a statement binds :func:`written` first. It offers ``execute``
    alone, the one method the GraphQL resolvers call on it, so a call that would bind through
    another fails where it is made."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        if isinstance(parameters, dict):
            parameters = {name: written(value) for name, value in parameters.items()}
        else:
            parameters = [written(value) for value in parameters]
        return self._conn.execute(sql, parameters)
