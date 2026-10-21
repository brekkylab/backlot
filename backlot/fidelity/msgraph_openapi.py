"""The operations of Microsoft Graph's Teams surface, read off the OpenAPI document Microsoft
generates its SDKs from.

The document declares every Graph operation — 17,885 in v1.0 — in about 44 MB of YAML. Only the
part a Teams client reaches is this source's contract: teams, chats and teamwork, and the people
they hang off (``/me`` and ``/users/{id}``, with their joined teams, chats and teamwork). A YAML
parser would build the whole document to read that part, so the paths, methods and
``operationId``s are read off its lines instead: Microsoft's generator writes each path at two
spaces, each method at four and each ``operationId`` at six.

Parameters are not read. Graph's are the OData system query options on every collection, which
the routes answer as measured rather than as declared; ``tests/test_msteams.py`` holds those.
"""

from __future__ import annotations

import re

import httpx

from backlot.fidelity.errors import FidelityError
from backlot.fidelity.operations import Operation, canonical

# The Teams surface, by vendor path.
SCOPE = re.compile(
    r"^/(?:teams|chats|teamwork)(?:/|$)"
    r"|^/users$"
    r"|^/(?:me|users/\{user-id\})(?:/(?:joinedTeams|chats|teamwork)(?:/.*)?)?$"
)
_PATH = re.compile(r"^  (['\"]?)(/.*?)\1:\s*$")
_METHOD = re.compile(r"^    (get|post|put|patch|delete):\s*$")
_OPERATION_ID = re.compile(r"^      operationId: (\S+)\s*$")


def operations(lines, scope: re.Pattern = SCOPE) -> dict[tuple[str, str], Operation]:
    """The in-scope operations ``lines`` (the document, line by line) declare."""
    out: dict[tuple[str, str], Operation] = {}
    in_paths, path, method = False, None, None
    for line in lines:
        if not in_paths:
            in_paths = line.startswith("paths:")
            continue
        if line[:1] not in (" ", "\n", ""):
            break
        if m := _PATH.match(line):
            path, method = m.group(2), None
        elif m := _METHOD.match(line):
            method = m.group(1)
        elif (m := _OPERATION_ID.match(line)) and path and method and scope.match(path):
            op = Operation(method, canonical(path), frozenset(), m.group(1))
            out[op.key] = op
    return out


def fetch(url: str, *, timeout: float = 120.0) -> dict[tuple[str, str], Operation]:
    """The in-scope operations of the document at ``url``, streamed rather than held."""
    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as response:
            if response.status_code != 200:
                raise FidelityError(f"{url} answered {response.status_code}")
            found = operations(response.iter_lines())
    except httpx.HTTPError as e:
        raise FidelityError(f"{url} unreachable: {e}") from e
    if not found:
        raise FidelityError(f"{url} declared no Teams operations; is it still Graph's OpenAPI?")
    return found
