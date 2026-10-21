"""Microsoft Graph's half of the Teams comparison: which requests to make and which declared type
each answer is, and which of the vendor's operations are the Teams surface.

The pairing of a request with its type cannot be derived. A CSDL says what a ``chatMessage`` is; it
does not say that ``GET /teams/{id}/channels/{id}/messages`` answers with a collection of them,
because it declares types rather than routes. So the source names its own walk, the way S3 names
its own prober, and a renamed endpoint is something a reader can follow rather than a convention
that silently stops covering anything.

The walk is a shape sample: first pages only, plus replies returned inline by ``$expand``.
Collection and reply continuation links are not followed; a successful comparison is not an
exhaustive crawl. The enumerated nested objects are checked too, against their own declared types.
An explicitly null non-nullable property is a schema mismatch; an omitted property is not reported.
Neither classification claims that every generated client will refuse to deserialize the payload.

The operations come from the OpenAPI document Microsoft generates its SDKs from. It declares every
Graph operation — 17,885 in v1.0 — in about 44 MB of YAML, and only the part a Teams client reaches
is this source's contract: teams, chats and teamwork, and the people they hang off (``/me`` and
``/users/{id}``, with their joined teams, chats and teamwork). A YAML parser would build the whole
document to read that part, so the paths, methods and ``operationId``s are read off its lines
instead: Microsoft's generator writes each path at two spaces, each method at four and each
``operationId`` at six. Parameters are not read. Graph's are the OData system query options on
every collection, which the routes answer as measured rather than as declared;
``tests/test_msteams.py`` holds those.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import quote

import httpx

from backlot.fidelity.csdl_diff import Schema, check, surface
from backlot.fidelity.errors import FidelityError
from backlot.fidelity.findings import Finding
from backlot.fidelity.operations import Operation, canonical
from backlot.fidelity.surface import Measurement

_G = "microsoft.graph"
V1 = "/msgraph/v1.0"
# The Teams surface, by vendor path.
SCOPE = re.compile(
    r"^/(?:teams|chats|teamwork)(?:/|$)"
    r"|^/users$"
    r"|^/(?:me|users/\{user-id\})(?:/(?:joinedTeams|chats|teamwork)(?:/.*)?)?$"
)
_PATH = re.compile(r"^  (['\"]?)(/.*?)\1:\s*$")
_METHOD = re.compile(r"^    (get|post|put|patch|delete):\s*$")
_OPERATION_ID = re.compile(r"^      operationId: (\S+)\s*$")


def _get(client: httpx.Client, path: str, roots: set[str] | None = None) -> dict:
    """One request's answer. ``roots`` collects the container member each path starts from."""
    if roots is not None:
        roots.add(path.removeprefix(f"{V1}/").split("/")[0].split("?")[0])
    response = client.get(path)
    if response.status_code != 200:
        raise FidelityError(f"Backlot answered {response.status_code} for {path}")
    return response.json()


def _message_parts(message: Mapping[str, Any], where: str) -> Iterator[tuple[str, Mapping, str]]:
    """A chatMessage and every declared type hanging off it."""
    yield f"{_G}.chatMessage", message, where
    if isinstance(message.get("body"), dict):
        yield f"{_G}.itemBody", message["body"], f"{where} body"
    if isinstance(message.get("channelIdentity"), dict):
        yield f"{_G}.channelIdentity", message["channelIdentity"], f"{where} channelIdentity"
    sender = message.get("from")
    if isinstance(sender, dict):
        yield f"{_G}.chatMessageFromIdentitySet", sender, f"{where} from"
        if isinstance(sender.get("user"), dict):
            yield f"{_G}.teamworkUserIdentity", sender["user"], f"{where} from.user"
    for kind, type_name in (
        ("reactions", f"{_G}.chatMessageReaction"),
        ("attachments", f"{_G}.chatMessageAttachment"),
        ("mentions", f"{_G}.chatMessageMention"),
    ):
        for entry in message.get(kind) or []:
            if isinstance(entry, dict):
                yield type_name, entry, f"{where} {kind}[]"
                # A mention's `mentioned` is a third identity-set type of its own.
                if kind == "mentions" and isinstance(entry.get("mentioned"), dict):
                    mentioned = entry["mentioned"]
                    yield (
                        f"{_G}.chatMessageMentionedIdentitySet",
                        mentioned,
                        f"{where} mentions[].mentioned",
                    )
                    if isinstance(mentioned.get("user"), dict):
                        yield (
                            f"{_G}.teamworkUserIdentity",
                            mentioned["user"],
                            f"{where} mentions[].mentioned.user",
                        )
                # A reactor's identity, which is a different declared type from a sender's and is
                # built from a different corpus field, so it gets its own check rather than
                # inheriting the sender's.
                if kind == "reactions" and isinstance(entry.get("user"), dict):
                    reactor = entry["user"]
                    yield (
                        f"{_G}.chatMessageReactionIdentitySet",
                        reactor,
                        f"{where} reactions[].user",
                    )
                    if isinstance(reactor.get("user"), dict):
                        yield (
                            f"{_G}.teamworkUserIdentity",
                            reactor["user"],
                            f"{where} reactions[].user.user",
                        )


def _objects(
    client: httpx.Client, roots: set[str] | None = None
) -> Iterator[tuple[str, Mapping, str]]:
    """Sample first-page objects and inline replies, paired with their declared types."""
    yield f"{_G}.user", _get(client, f"{V1}/me", roots), "GET /me"
    for user in _get(client, f"{V1}/users", roots)["value"]:
        yield f"{_G}.user", user, "GET /users"

    teams = _get(client, f"{V1}/me/joinedTeams", roots)["value"]
    for team in teams:
        yield f"{_G}.team", team, "GET /me/joinedTeams"
    if not teams:
        raise FidelityError("the corpus serves no team, so nothing below it can be compared")
    team_id = quote(teams[0]["id"], safe="")
    yield f"{_G}.team", _get(client, f"{V1}/teams/{team_id}", roots), "GET /teams/{id}"

    channels = _get(client, f"{V1}/teams/{team_id}/channels", roots)["value"]
    for channel in channels:
        yield f"{_G}.channel", channel, "GET /channels"
    for channel in channels:
        base = f"{V1}/teams/{team_id}/channels/{quote(channel['id'], safe='')}"
        yield f"{_G}.channel", _get(client, base, roots), "GET /channels/{id}"
        for member in _get(client, f"{base}/members", roots)["value"]:
            yield f"{_G}.aadUserConversationMember", member, "GET /channels/{id}/members"
        # `$expand=replies` in one request rather than a second walk: it is the shape a crawler
        # actually asks for, and it puts a reply through the same checks as a root.
        for message in _get(client, f"{base}/messages?$expand=replies", roots)["value"]:
            yield from _message_parts(message, "GET /messages")
            for reply in message.get("replies") or []:
                yield from _message_parts(reply, "GET /messages?$expand=replies replies[]")


def run(base_url: str, token: str, schema: Schema, timeout: float) -> Measurement:
    """Compare sampled Teams objects against the vendor declarations, without following pages."""
    found: list[Finding] = []
    served: dict[str, set[str]] = {}
    roots: set[str] = set()
    with httpx.Client(
        base_url=base_url, timeout=timeout, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        seen: set[str] = set()
        for type_name, obj, where in _objects(client, roots):
            served.setdefault(type_name, set()).update(k for k in obj if "@odata." not in k)
            for finding in check(schema, type_name, obj, where):
                # One row per divergence, not per object that carries it: every message in a
                # channel would otherwise report the same undeclared property.
                if finding.key not in seen:
                    seen.add(finding.key)
                    found.append(finding)
    return Measurement(
        sorted(found, key=lambda f: (f.path, f.kind)), surface(schema, served, roots)
    )


def operations(lines: Iterable[str], scope: re.Pattern = SCOPE) -> dict[tuple[str, str], Operation]:
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
