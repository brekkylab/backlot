"""Which Graph requests to make, and which declared type each answer is.

The knowledge here is the pairing, and it cannot be derived. A CSDL says what a ``chatMessage`` is;
it does not say that ``GET /teams/{id}/channels/{id}/messages`` answers with a collection of them,
because it declares types rather than routes. So the source names its own walk, the way S3 names
its own prober, and a renamed endpoint is something a reader can follow rather than a convention
that silently stops covering anything.

This is a shape sample: first pages only, plus replies returned inline by ``$expand``. Collection
and reply continuation links are not followed; a successful comparison is not an exhaustive crawl.
The enumerated nested objects are checked too, against their own declared types. An explicitly
null non-nullable property is a schema mismatch; an omitted property is not reported. Neither
classification claims that every generated client will refuse to deserialize the payload.
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping
from urllib.parse import quote

import httpx

from backlot.fidelity.csdl_diff import Schema, check
from backlot.fidelity.errors import FidelityError
from backlot.fidelity.findings import Finding

_G = "microsoft.graph"
V1 = "/msgraph/v1.0"


def _get(client: httpx.Client, path: str) -> dict:
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


def _objects(client: httpx.Client) -> Iterator[tuple[str, Mapping, str]]:
    """Sample first-page objects and inline replies, paired with their declared types."""
    yield f"{_G}.user", _get(client, f"{V1}/me"), "GET /me"
    for user in _get(client, f"{V1}/users")["value"]:
        yield f"{_G}.user", user, "GET /users"

    teams = _get(client, f"{V1}/me/joinedTeams")["value"]
    for team in teams:
        yield f"{_G}.team", team, "GET /me/joinedTeams"
    if not teams:
        raise FidelityError("the corpus serves no team, so nothing below it can be compared")
    team_id = quote(teams[0]["id"], safe="")

    channels = _get(client, f"{V1}/teams/{team_id}/channels")["value"]
    for channel in channels:
        yield f"{_G}.channel", channel, "GET /channels"
    for channel in channels:
        base = f"{V1}/teams/{team_id}/channels/{quote(channel['id'], safe='')}"
        yield f"{_G}.channel", _get(client, base), "GET /channels/{id}"
        for member in _get(client, f"{base}/members")["value"]:
            yield f"{_G}.aadUserConversationMember", member, "GET /channels/{id}/members"
        # `$expand=replies` in one request rather than a second walk: it is the shape a crawler
        # actually asks for, and it puts a reply through the same checks as a root.
        for message in _get(client, f"{base}/messages?$expand=replies")["value"]:
            yield from _message_parts(message, "GET /messages")
            for reply in message.get("replies") or []:
                yield from _message_parts(reply, "GET /messages?$expand=replies replies[]")


def run(base_url: str, token: str, schema: Schema, timeout: float) -> list[Finding]:
    """Compare sampled Teams objects against the vendor declarations, without following pages."""
    found: list[Finding] = []
    with httpx.Client(
        base_url=base_url, timeout=timeout, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        seen: set[str] = set()
        for type_name, obj, where in _objects(client):
            for finding in check(schema, type_name, obj, where):
                # One row per divergence, not per object that carries it: every message in a
                # channel would otherwise report the same undeclared property.
                if finding.key not in seen:
                    seen.add(finding.key)
                    found.append(finding)
    return found
