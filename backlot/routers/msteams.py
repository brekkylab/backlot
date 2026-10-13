"""Microsoft Teams over the Microsoft Graph API (read-only).

Base URL for a client: ``http://<host>/msgraph/v1.0`` — the path real Graph serves under
``https://graph.microsoft.com/v1.0``, so a client changes its base URL and nothing else.

Unlike Slack, Graph answers with real HTTP status codes and an ``{"error": {...}}`` body; the
envelope and the code strings live in :mod:`backlot.errors.msgraph`, which is where their sourcing
is argued. Collections are OData: ``@odata.context``, ``@odata.count``, ``value`` and an absolute
``@odata.nextLink`` carrying a ``$skiptoken``.

Every response shape here is taken from the vendor's own v1.0 reference and its worked examples
(``chatMessage`` / ``channel`` / ``user`` / ``team`` resources, ``channel-list``,
``channel-list-messages``, ``channel-list-members``, ``user-list-joinedTeams``, ``user-get``) — no
Graph tenant was reachable from where this was written, so nothing here rests on a measurement and
anything the reference does not state is either omitted or called out below.

What this corpus can and cannot model:

- ONE team, because a Backlot corpus has one org. Its id is derived from the org name, and a
  request naming any other team is a 404 — which is what real Graph answers a caller for a team
  that does not exist.
- Channels only. Graph's chats (1:1 and group DMs) have no counterpart in a channel-based corpus,
  so ``chatId`` is null on every message, exactly as it is on every real CHANNEL message.
- Backlot synthesizes message ids from creation milliseconds and uses that id as the etag.
  This matches some published examples, not a Microsoft guarantee: the chatMessage reference
  declares an id string and an etag version string independently. Consumers must treat both as
  opaque. Authenticated tenant behavior remains unmeasured; see docs/supported-sources.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from backlot import auth, store, synth
from backlot.acl import Caller
from backlot.config import get_settings
from backlot.errors import msgraph
from backlot.openapi import qp
from backlot.pagination import decode_cursor_or_none, encode_cursor

router = APIRouter(prefix="/msgraph/v1.0", tags=["msteams"])

SOURCE = "msteams"

# Graph's own page sizes, from the endpoints' reference pages: channel messages default to 20 and
# cap at 50, channel members default to 100 and cap at 999. `$expand=replies` "can include up to
# 200 replies" by default, with `replies@odata.nextLink` for the rest.
_MESSAGES_DEFAULT, _MESSAGES_MAX = 20, 50
_MEMBERS_DEFAULT, _MEMBERS_MAX = 100, 999
_EXPAND_REPLIES = 200


# --- OpenAPI enrichment --------------------------------------------------
# Parameters are read off the raw request (an OData name starts with `$`, which is not a valid
# Python identifier), so they are declared here rather than in the handler signatures. The response
# models allow extra fields so the builders' full field set — `@odata.context` and friends included
# — passes through unfiltered.


class GraphResponse(BaseModel):
    """Every response here, collection and entity alike.

    No declared fields, deliberately: FastAPI serializes declared fields BEFORE extras, so naming
    ``value`` on the model would emit it ahead of the ``@odata.context`` real Graph opens with.
    JSON objects are unordered and no client can depend on that, but a recorded response is
    diffed by eye, and one whose keys are in the vendor's order is one less difference to explain.
    """

    model_config = ConfigDict(extra="allow")


_P_TOP = qp("$top", "integer", description="Page size.")
_P_SKIP = qp("$skiptoken", description="Opaque page token, from a previous `@odata.nextLink`.")
_P_SELECT = qp("$select", description="Comma-separated properties to return. `id` is always kept.")
_P_PAGED = [_P_TOP, _P_SKIP]
_P_CHANNELS = _P_PAGED + [
    _P_SELECT,
    qp(
        "$filter",
        description=(
            "Only `membershipType eq '<value>'` is honoured — the form the vendor documents for "
            "this method. Any other filter is rejected rather than ignored, so a caller cannot "
            "mistake an unfiltered list for a filtered one."
        ),
    ),
]
_P_MESSAGES = _P_PAGED + [
    qp(
        "$expand",
        description=(
            "`replies` to include each message's replies inline, up to 200 per message, with "
            "`replies@odata.count` and `replies@odata.nextLink` beside them."
        ),
    )
]


# --- identity ------------------------------------------------------------


def _org() -> tuple[str, str]:
    s = get_settings()
    return s.org_name, s.org_domain


def _team_id() -> str:
    return synth.msteams_team_id(get_settings().org_name)


def _tenant_id() -> str:
    return synth.msteams_tenant_id(get_settings().org_name)


def _caller(request: Request) -> Caller:
    """The caller, or Graph's own 401.

    Which of THREE 401s, measured live: no header at all, a header whose token part is blank, and a
    token that was presented and resolves to nobody. ``auth.msgraph_token`` draws those lines (and
    documents why the scheme is not one of them); ``errors.msgraph`` carries the messages.
    """
    state, token = auth.msgraph_token(request)
    if state == "absent":
        raise msgraph.no_credentials()
    if state == "empty":
        raise msgraph.empty_credentials()
    caller = auth.acl(request).resolve(token)
    if caller is None:
        raise msgraph.bad_token()
    return caller


def _require_team(team_id: str) -> None:
    """A corpus is one team, so any other id names a team that does not exist."""
    if team_id != _team_id():
        raise msgraph.not_found(f"No team found with Group Id {team_id}")


# --- OData plumbing ------------------------------------------------------


def _param(request: Request, key: str) -> str | None:
    return request.query_params.get(key)


def _int(request: Request, key: str, default: int, maximum: int) -> int:
    """An OData ``$top``, clamped to the method's own cap. A value Graph could not read falls back
    to the default rather than 400ing: nothing here was measured against the live service, and
    inventing a refusal is the larger divergence."""
    raw = _param(request, key)
    if not raw:
        return default
    try:
        return max(1, min(int(raw), maximum))
    except ValueError:
        return default


def _offset(request: Request) -> int:
    """The offset a Backlot ``$skiptoken`` names.

    Undecodable tokens and negative offsets restart at the first page here. This is an emulator
    fallback, not measured Graph behavior for stale or malformed tokens. Decodable offsets above
    SQLite's signed 64-bit range are refused before a query can overflow its integer binding.
    """
    offset = decode_cursor_or_none(_param(request, "$skiptoken")) or 0
    if offset > 2**63 - 1:
        raise msgraph.bad_request("$skiptoken offset exceeds the emulator's supported range.")
    return offset


def _next_link(request: Request, offset: int, page_len: int, total: int) -> str | None:
    """The absolute ``@odata.nextLink``, or None on the last page.

    Built from the REQUEST's own URL, so it points back at the host the caller actually reached
    — the same reasoning ``routers.atlassian`` applies to a Jira ``self`` link. Graph omits the
    key entirely when there is nothing more.
    """
    nxt = offset + page_len
    if nxt >= total or not page_len:
        return None
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "$skiptoken"]
    params.append(("$skiptoken", encode_cursor(nxt)))
    query = "&".join(f"{quote(k, safe='$')}={quote(v, safe='')}" for k, v in params)
    return str(request.url.replace(query=query))


def _context(fragment: str, select: list[str] | None = None) -> str:
    """An ``@odata.context``: the metadata URL plus the entity set the response describes.

    The host is graph.microsoft.com's, not this server's, and deliberately: the context names the
    ``$metadata`` document the payload conforms to, which is Microsoft's, and a client that
    resolves it must reach the real CSDL rather than a 404 here.
    """
    fields = f"({','.join(select)})" if select else ""
    return f"https://graph.microsoft.com/v1.0/$metadata#{fragment}{fields}"


def _select(request: Request) -> list[str] | None:
    raw = _param(request, "$select")
    if not raw:
        return None
    return [f.strip() for f in raw.split(",") if f.strip()] or None


def _project(entity: dict, select: list[str] | None) -> dict:
    """Apply ``$select``. ``id`` survives whether or not it was asked for, as it does live —
    a client that re-fetches an entity from a projected listing has nothing else to fetch it by."""
    if not select:
        return entity
    keep = set(select) | {"id"}
    return {k: v for k, v in entity.items() if k in keep or k.startswith("@odata.")}


def _collection(request: Request, fragment, items, offset, total, select=None) -> dict:
    """One OData collection response, in the key order real Graph sends."""
    body: dict = {
        "@odata.context": _context(fragment, select),
        "@odata.count": total,
    }
    if link := _next_link(request, offset, len(items), total):
        body["@odata.nextLink"] = link
    body["value"] = items
    return body


# --- entity builders -----------------------------------------------------


def _iso_millis(epoch_millis: int) -> str:
    """``2021-03-28T21:11:12.395Z`` — the spelling every Graph timestamp takes."""
    dt = datetime.fromtimestamp(epoch_millis / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{epoch_millis % 1000:03d}Z"


def _user(conn, email: str) -> dict:
    """A ``user`` with the properties Graph returns when no ``$select`` narrows it — that default
    set is documented on ``user-get`` and is exactly these eleven."""
    u = store.get_user(conn, email)
    display = u["display_name"] if u else email.split("@")[0]
    parts = display.split()
    return {
        # A corpus states a name and an address and nothing else, so the properties a directory
        # fills from HR data are null rather than synthesized — which is also what a real tenant
        # returns for a user nobody has filled them in for.
        "businessPhones": [],
        "displayName": display,
        "givenName": parts[0] if parts else display,
        "jobTitle": None,
        "mail": email,
        "mobilePhone": None,
        "officeLocation": None,
        "preferredLanguage": None,
        "surname": parts[-1] if len(parts) > 1 else None,
        "userPrincipalName": email,
        "id": synth.msteams_user_id(email),
    }


def _service_account_user() -> dict:
    """The identity ``/me`` answers for the admin/service token.

    Real Graph refuses ``/me`` on an app-only token, and this is not that: every token Backlot
    issues is a delegated one, the admin's included — it is a service ACCOUNT that bypasses ACL
    filtering, not an application. So ``/me`` names it, the same way Slack's ``auth.test`` answers
    ``user: "service-account"`` for the same token. It is not in the directory, so ``/users`` does
    not list it.
    """
    _, domain = _org()
    email = f"service-account@{domain}"
    return {
        "businessPhones": [],
        "displayName": "Service Account",
        "givenName": "Service",
        "jobTitle": None,
        "mail": None,
        "mobilePhone": None,
        "officeLocation": None,
        "preferredLanguage": None,
        "surname": "Account",
        "userPrincipalName": email,
        "id": synth.msteams_user_id(email),
    }


def _me(conn, caller: Caller) -> dict:
    return _service_account_user() if caller.is_admin else _user(conn, caller.email)


def _team() -> dict:
    """The one ``team``.

    ``joinedTeams`` is documented as returning every team property but populating only **id**,
    **displayName**, **description**, **isArchived** and **tenantId** — the rest null. That is
    reproduced rather than filled in: a client written against the real method already treats the
    nulls as "call Get team for these", and handing it values would train it out of that.
    """
    org, _ = _org()
    return {
        "id": _team_id(),
        "createdDateTime": None,
        "displayName": org,
        "description": f"{org} on Microsoft Teams",
        "internalId": None,
        "classification": None,
        "specialization": None,
        "visibility": None,
        "webUrl": None,
        "isArchived": False,
        "tenantId": _tenant_id(),
        "isMembershipLimitedToOwners": None,
        "memberSettings": None,
        "guestSettings": None,
        "messagingSettings": None,
        "funSettings": None,
        "discoverySettings": None,
        "tagSettings": None,
        "summary": None,
    }


def _channel(request: Request, conn, name: str, *, layout: str | None) -> dict:
    """One ``channel``.

    ``layout`` is passed in rather than decided here because the two methods genuinely disagree:
    the vendor documents a known issue where **layoutType returns null when listing all channels**,
    and says to use Get channel for it. Building one object for both would make Backlot the only
    place a client could rely on the listing carrying it.
    """
    cid = synth.msteams_channel_id(name)
    created = _channel_created(request, conn, name)
    return {
        "id": cid,
        "createdDateTime": synth.rfc3339_millis(created),
        "displayName": name,
        "description": f"Channel for {name}",
        # Not something a corpus states, and null is what a channel nobody marked as recommended
        # returns.
        "isFavoriteByDefault": None,
        # A channel gets an address only when one is provisioned for it, and the vendor's own
        # example shows the empty string for a channel with none.
        "email": "",
        "webUrl": (
            f"https://teams.microsoft.com/l/channel/{quote(cid, safe='')}/"
            f"{quote(name, safe='')}?groupId={_team_id()}&tenantId={_tenant_id()}"
        ),
        # A channel whose messages are granted to the org is one the whole team can read, which is
        # what `standard` means ("Channel inherits the list of members of the parent team"); any
        # narrower grant makes it `private` ("members that are a subset").
        "membershipType": "standard" if not _is_private(request, conn, name) else "private",
        "layoutType": layout,
        # `tenantId` is a documented channel property but appears in none of the vendor's example
        # responses, so it is left out rather than added to a shape real Graph does not send.
        # `summary` likewise — the reference says it is returned only when `$select` names it.
        "isArchived": False,
    }


def _message(
    request: Request, conn, row, *, replies: list[dict] | None = None, reply_total: int = 0
) -> dict:
    """One ``chatMessage``.

    Backlot's synthetic id includes creation milliseconds, so this local representation derives
    the timestamp from it. Microsoft documents id and createdDateTime separately; this relation
    must not be used to infer times from real Graph ids.
    """
    mid = row["id"]
    created_ms = int(mid)
    created = _iso_millis(created_ms)
    edited = row["last_edited_ts"]
    edited_iso = _iso_millis(int(edited) * 1000) if edited else None
    cid = synth.msteams_channel_id(row["channel"])
    root = row["reply_to_id"] or mid
    is_system = row["message_type"] == "systemEventMessage"
    m = {
        "id": mid,
        "replyToId": row["reply_to_id"],
        # A synthetic version for these immutable imported messages, not a Graph invariant.
        "etag": mid,
        "messageType": row["message_type"] or "message",
        "createdDateTime": created,
        # "when the chat message is created (initial setting) or modified": an unedited message
        # reports its creation time here, which is what the examples show.
        "lastModifiedDateTime": edited_iso or created,
        "lastEditedDateTime": edited_iso,
        "deletedDateTime": None,
        "subject": row["subject"],
        # Only ever set for a push notification's fallback view; no corpus states one.
        "summary": None,
        # Null on a CHANNEL message, always — it names the chat a message was sent in, and these
        # are not chat messages.
        "chatId": None,
        "importance": row["importance"] or "normal",
        # "Always set to en-us", per the property reference.
        "locale": "en-us",
        "webUrl": (
            f"https://teams.microsoft.com/l/message/{quote(cid, safe='')}/{mid}"
            f"?groupId={_team_id()}&tenantId={_tenant_id()}"
            f"&createdTime={mid}&parentMessageId={root}"
        ),
        "policyViolation": None,
        # A system event message carries no sender — the vendor's own example shows `from: null`
        # beside `messageType: systemEventMessage`.
        "from": None if is_system else _identity_set(conn, row["author_email"]),
        "body": {
            "contentType": row["content_type"] or "text",
            "content": row["content"],
        },
        "channelIdentity": {"teamId": _team_id(), "channelId": cid},
        "attachments": store.jcol(row, "attachments"),
        "mentions": store.jcol(row, "mentions"),
        "reactions": _reactions(conn, row, created),
        # The per-message audit trail Teams keeps of reaction and edit actions. Nothing in a corpus
        # states one, and the key is present and empty on every message the vendor shows.
        "messageHistory": [],
        "eventDetail": None,
    }
    if replies is not None:
        m["replies@odata.count"] = len(replies)
        if reply_total > len(replies):
            m["replies@odata.nextLink"] = str(
                request.url.replace(
                    path=f"{request.url.path.rstrip('/')}/{mid}/replies",
                    query=f"$skiptoken={encode_cursor(len(replies))}",
                )
            )
        m["replies"] = replies
    return m


def _reactions(conn, row, created: str) -> list[dict]:
    """The message's ``chatMessageReaction`` list, built from what the corpus stated.

    A corpus names the reactor by ADDRESS and this renders the vendor's identity set, the same way
    ``from`` is built from ``author_email``. It is not pass-through, and that is the point: Graph's
    metadata declares ``reactionType``, ``createdDateTime`` and ``user`` non-nullable on a
    ``chatMessageReaction``, so a reaction missing any of them is a shape the real service cannot
    send. Passing the corpus's own object through let exactly that out (a reaction with no
    ``user``), which is why the schema now takes an address and this assembles the rest.

    ``createdDateTime`` falls back to the message's own time: the field has to carry one, and
    nothing is gained by making a corpus repeat a second it already stated. What the corpus DID
    state arrives here as epoch seconds, parsed at import like every other time on the record.

    The two nullable members Graph declares beside these are only emitted when the corpus states
    them. ``chatMessageReactionIdentitySet`` also declares ``chatDisplayName``, ``chatId`` and
    ``initiator``; none appears in any published example, so none is invented here — the same call
    the channel object makes about ``tenantId``.
    """
    out = []
    for r in store.jcol(row, "reactions"):
        if not isinstance(r, dict):
            continue
        when = r.get("createdDateTime")
        reaction = {
            "reactionType": r.get("reactionType"),
            # Already epoch seconds: the importer parses a reaction's own time alongside every
            # other time on the record, so nothing here has to read the corpus's spelling.
            "createdDateTime": _iso_millis(int(when) * 1000) if when is not None else created,
            "user": _identity_set(conn, r["user"]),
        }
        for optional in ("displayName", "reactionContentUrl"):
            if optional in r:
                reaction[optional] = r[optional]
        out.append(reaction)
    return out


def _identity_set(conn, email: str) -> dict:
    """A ``chatMessageFromIdentitySet``. All three members are present on every real one, with the
    two this corpus cannot produce (an app, a device) null."""
    u = store.get_user(conn, email)
    return {
        "application": None,
        "device": None,
        "user": {
            "id": synth.msteams_user_id(email),
            "displayName": u["display_name"] if u else email.split("@")[0],
            "userIdentityType": "aadUser",
        },
    }


def _member(conn, email: str) -> dict:
    """An ``aadUserConversationMember``.

    ``roles`` is empty for an ordinary member; the vendor's example shows ``["owner"]`` for an
    owner, and nothing in a corpus says who owns a channel, so nobody is one. The ``@odata.type``
    is not decoration: ``conversationMember`` is a polymorphic base, and a client deserializing
    the collection picks its concrete type from that key.
    """
    u = store.get_user(conn, email)
    uid = synth.msteams_user_id(email)
    return {
        "@odata.type": "#microsoft.graph.aadUserConversationMember",
        "id": synth.msteams_member_id(_team_id(), uid),
        "roles": [],
        "displayName": u["display_name"] if u else email.split("@")[0],
        "userId": uid,
        "email": email,
    }


# --- channel helpers -----------------------------------------------------


def _channel_names(conn) -> list[str]:
    return [row["name"] for row in store.list_containers(conn, SOURCE)]


def _channel_name(conn, channel_id: str) -> str | None:
    for row in store.list_containers(conn, SOURCE):
        if synth.msteams_channel_id(row["name"]) == channel_id:
            return row["name"]
    return None


def _channel_acl_cache(request: Request):
    return getattr(request.app.state, "msteams_channel_acl", None)


def _channel_visibility(request: Request, conn, ids):
    """A predicate over channel names: may this caller see that the channel EXISTS?

    One predicate for every method, because a channel hidden from the listing has to be hidden
    from the methods that resolve one by id as well — otherwise a channel id is enough for any
    authenticated principal to read a private channel's name and description and enumerate who is
    in it, none of which the ACL on its messages allows them to know. The vendor's own note on
    ``channel-list`` says the same thing about the listing: "Teams members can't see private or
    shared channels that they aren't members of".
    """
    if ids is None:  # admin/service token: no filtering at all
        return lambda name: True
    cache = _channel_acl_cache(request)
    if cache is not None:  # O(1) per channel: intersect its grantees with the caller's principals
        idset = set(ids)
        return lambda name: bool(cache.get(name, frozenset()) & idset)
    # The cold path must intersect the SAME principal ids as the warm cache. An org grant
    # to some other org is not public to this caller merely because its type is 'org'.
    granted = store.conversation_channels_for_principals(conn, SOURCE, ids)
    return lambda name: name in granted


def _is_private(request: Request, conn, name: str) -> bool:
    """Whether a channel is private: nothing in it is granted to the org."""
    cache = _channel_acl_cache(request)
    if cache is not None:
        return auth.acl(request).org_name not in cache.get(name, frozenset())
    return not store.container_has_public(conn, SOURCE, name)


def _member_count(request: Request, conn, name: str) -> int:
    cache = getattr(request.app.state, "msteams_channel_members", None)
    if cache is not None:
        return cache.get(name, 0)
    return store.count_conversation_members(conn, SOURCE, name)


def _compute_channel_created(conn, name: str) -> int:
    """Channel creation second, pinned at or before its earliest message so it never postdates
    one. See ``routers.slack._compute_channel_created`` for why a synthesized per-channel epoch
    alone is not enough."""
    b = store.conversation_created_bounds(conn, SOURCE, name)
    if not b["total"]:
        return synth.epoch(name)
    candidates = []
    if b["have"]:
        candidates.append(b["min_ts"])
    if b["have"] < b["total"]:
        candidates.append(synth.BASE_EPOCH)
    return min(candidates)


def _channel_created(request: Request, conn, name: str) -> int:
    cache = getattr(request.app.state, "msteams_channel_created", None)
    if cache is None:
        cache = request.app.state.msteams_channel_created = {}
    if name not in cache:
        cache[name] = _compute_channel_created(conn, name)
    return cache[name]


def _resolve_channel(request: Request, conn, team_id: str, channel_id: str, ids):
    """The channel name behind an id, or Graph's 404.

    A channel this caller cannot see is answered exactly as one that does not exist. This is
    Backlot's concealment policy; a real tenant's 404 versus 403 has not been measured. No response
    may expose its name, description or membership to someone without corpus access.
    """
    _require_team(team_id)
    name = _channel_name(conn, channel_id)
    if name is None or not _channel_visibility(request, conn, ids)(name):
        raise msgraph.not_found(f"No channel found with id {channel_id}")
    return name


# --- endpoints -----------------------------------------------------------


@router.get("/me", response_model=GraphResponse, openapi_extra={"parameters": [_P_SELECT]})
async def me(request: Request):
    conn = auth.conn(request)
    caller = _caller(request)
    select = _select(request)
    return {
        "@odata.context": _context("users", select) + "/$entity",
        **_project(_me(conn, caller), select),
    }


@router.get("/me/joinedTeams", response_model=GraphResponse)
async def me_joined_teams(request: Request):
    """The teams the caller is in — the one this corpus models, for every caller.

    Not ACL-filtered: a team is not a document. Every principal in the org is in the workspace;
    which of its CHANNELS they may see is what the ACL decides, and that is answered by
    ``/teams/{id}/channels``.
    """
    _caller(request)
    return {"@odata.context": _context("teams"), "value": [_team()]}


@router.get(
    "/users", response_model=GraphResponse, openapi_extra={"parameters": _P_PAGED + [_P_SELECT]}
)
async def users_list(request: Request):
    """The directory: every registered user principal.

    Message senders who are not principals are NOT here — a corpus's transcript speakers can
    outnumber its directory, and neither set can be made a subset of the other without inventing
    colleagues or discarding speakers (the same limitation Slack's ``users.list`` states). Such a
    sender still resolves through ``/users/{id}`` and is listed among their channel's members.
    """
    conn = auth.conn(request)
    _caller(request)
    emails = store.all_user_emails(conn)
    offset = _offset(request)
    top = _int(request, "$top", get_settings().default_page_size, get_settings().max_page_size)
    select = _select(request)
    page = [_project(_user(conn, e), select) for e in emails[offset : offset + top]]
    return _collection(request, "users", page, offset, len(emails), select)


@router.get(
    "/users/{user_id:path}", response_model=GraphResponse, openapi_extra={"parameters": [_P_SELECT]}
)
async def users_get(request: Request, user_id: str):
    """One user by object id or userPrincipalName — Graph accepts either at this route.

    The ``:path`` converter is for the address form: a UPN is an email, and while nothing in one
    requires a slash, the converter costs nothing and keeps an unusual address resolving instead
    of 404ing on a route that never matched it.
    """
    conn = auth.conn(request)
    _caller(request)
    select = _select(request)
    email = _resolve_user(request, conn, user_id)
    if email is None:
        raise msgraph.user_not_found(user_id)
    return {
        "@odata.context": _context("users", select) + "/$entity",
        **_project(_user(conn, email), select),
    }


def _resolve_user(request: Request, conn, user_id: str) -> str | None:
    """An object id or a userPrincipalName -> the address a corpus knows the person by."""
    if "@" in user_id:
        lowered = user_id.lower()
        for e in store.all_user_emails(conn):
            if e.lower() == lowered:
                return e
    for e in store.all_user_emails(conn):
        if synth.msteams_user_id(e) == user_id:
            return e
    # Display-only senders — a bot or a speaker the directory does not carry — are not principals,
    # so they are resolved from the message authors instead of coming back not-found at an id the
    # API has just handed the caller on a message's `from`.
    return _sender_by_id(request, conn, user_id)


def _sender_by_id(request: Request, conn, user_id: str) -> str | None:
    """Reverse a synthesized user id to a message sender (synth is one-way, so build the map from
    the distinct senders once and cache it on app.state)."""
    cache = getattr(request.app.state, "_msteams_uid_map", None)
    if cache is None:
        cache = {
            synth.msteams_user_id(e): e
            for e in store.distinct_conversation_author_emails(conn, SOURCE)
        }
        request.app.state._msteams_uid_map = cache
    return cache.get(user_id)


@router.get(
    "/teams/{team_id}/channels",
    response_model=GraphResponse,
    openapi_extra={"parameters": _P_CHANNELS},
)
async def channels_list(request: Request, team_id: str):
    conn = auth.conn(request)
    caller = _caller(request)
    _require_team(team_id)
    ids = auth.visible_ids(request, caller)
    visible = _channel_visibility(request, conn, ids)
    names = [n for n in _channel_names(conn) if visible(n)]
    if want := _membership_filter(request):
        names = [
            n for n in names if ("private" if _is_private(request, conn, n) else "standard") == want
        ]
    offset = _offset(request)
    top = _int(request, "$top", get_settings().default_page_size, get_settings().max_page_size)
    select = _select(request)
    # `layoutType` is null here and only here — the vendor documents that as a known issue of this
    # method, and a client that works around it must keep having something to work around.
    page = [
        _project(_channel(request, conn, n, layout=None), select)
        for n in names[offset : offset + top]
    ]
    fragment = f"teams('{quote(team_id, safe='')}')/channels"
    return _collection(request, fragment, page, offset, len(names), select)


def _membership_filter(request: Request) -> str | None:
    """The value a ``$filter=membershipType eq '<x>'`` asks for, or None for no filter.

    Only that one form, because it is the only one the vendor's own reference shows for this
    method. Any other filter is refused rather than ignored: a caller who asked for a subset and
    silently got everything has no way to tell, which is the same failure Slack's ``invalid_types``
    exists to prevent.
    """
    raw = _param(request, "$filter")
    if not raw:
        return None
    head, _, value = raw.strip().partition(" eq ")
    value = value.strip().strip("'")
    if head.strip() != "membershipType" or value not in ("standard", "private", "shared"):
        raise msgraph.bad_request(
            f"Unsupported $filter: {raw!r}. This method supports "
            "membershipType eq 'standard' | 'private' | 'shared'."
        )
    return value


@router.get(
    "/teams/{team_id}/channels/{channel_id}",
    response_model=GraphResponse,
    openapi_extra={"parameters": [_P_SELECT]},
)
async def channels_get(request: Request, team_id: str, channel_id: str):
    conn = auth.conn(request)
    caller = _caller(request)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids)
    select = _select(request)
    fragment = f"teams('{quote(team_id, safe='')}')/channels"
    return {
        "@odata.context": _context(fragment, select) + "/$entity",
        # `post` is the documented default layout, and this is the method the vendor points at for
        # reading it.
        **_project(_channel(request, conn, name, layout="post"), select),
    }


@router.get(
    "/teams/{team_id}/channels/{channel_id}/messages",
    response_model=GraphResponse,
    openapi_extra={"parameters": _P_MESSAGES},
)
async def channel_messages(request: Request, team_id: str, channel_id: str):
    """The channel's root messages — "the list of messages (without the replies)".

    Ordered by the reply chain's newest activity, which is the order the vendor states this method
    returns: a post someone answered today outranks a newer post nobody has touched.
    """
    conn = auth.conn(request)
    caller = _caller(request)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids)
    offset = _offset(request)
    top = _int(request, "$top", _MESSAGES_DEFAULT, _MESSAGES_MAX)
    total = store.count_msteams_top_level(conn, name, ids)
    rows = store.list_msteams_top_level(conn, name, ids, limit=top, offset=offset)
    expand = (_param(request, "$expand") or "").strip().lower() == "replies"
    value = []
    for r in rows:
        if not expand:
            value.append(_message(request, conn, r))
            continue
        reply_total = store.count_msteams_replies(conn, name, r["id"], ids)
        replies = store.list_msteams_replies(conn, name, r["id"], ids, limit=_EXPAND_REPLIES)
        value.append(
            _message(
                request,
                conn,
                r,
                replies=[_message(request, conn, x) for x in replies],
                reply_total=reply_total,
            )
        )
    fragment = (
        f"teams('{quote(team_id, safe='')}')/channels('{quote(channel_id, safe='')}')/messages"
    )
    return _collection(request, fragment, value, offset, total)


@router.get(
    "/teams/{team_id}/channels/{channel_id}/messages/{message_id}",
    response_model=GraphResponse,
)
async def channel_message(request: Request, team_id: str, channel_id: str, message_id: str):
    """One message, root or reply alike: Graph resolves this route against the whole channel, and
    a client that got its id from a search hit or a `replyToId` will often pass a reply's."""
    conn = auth.conn(request)
    caller = _caller(request)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids)
    row = store.msteams_by_id(conn, name, message_id, ids)
    if row is None:
        raise msgraph.not_found(f"No message found with id {message_id}")
    fragment = (
        f"teams('{quote(team_id, safe='')}')/channels('{quote(channel_id, safe='')}')/messages"
    )
    return {"@odata.context": _context(fragment) + "/$entity", **_message(request, conn, row)}


@router.get(
    "/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies",
    response_model=GraphResponse,
    openapi_extra={"parameters": _P_PAGED},
)
async def message_replies(request: Request, team_id: str, channel_id: str, message_id: str):
    """A message's replies, newest first — the order the vendor's own example returns them in
    (Reply3, Reply2, Reply1).

    A message id that names a REPLY has no replies of its own: Teams threads are one level deep,
    so an empty collection is the honest answer rather than the parent's replies.
    """
    conn = auth.conn(request)
    caller = _caller(request)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids)
    if store.msteams_by_id(conn, name, message_id, ids) is None:
        raise msgraph.not_found(f"No message found with id {message_id}")
    offset = _offset(request)
    top = _int(request, "$top", _MESSAGES_DEFAULT, _MESSAGES_MAX)
    total = store.count_msteams_replies(conn, name, message_id, ids)
    rows = store.list_msteams_replies(conn, name, message_id, ids, limit=top, offset=offset)
    fragment = (
        f"teams('{quote(team_id, safe='')}')/channels('{quote(channel_id, safe='')}')/"
        f"messages('{message_id}')/replies"
    )
    value = [_message(request, conn, r) for r in rows]
    return _collection(request, fragment, value, offset, total)


@router.get(
    "/teams/{team_id}/channels/{channel_id}/members",
    response_model=GraphResponse,
    openapi_extra={"parameters": _P_PAGED + [_P_SELECT]},
)
async def channel_members(request: Request, team_id: str, channel_id: str):
    """The channel's members.

    Membership is derived from who has posted in the channel (or, for a private one, from who may
    read it), so this list is a projection of the very messages the ACL withholds — and it sits
    behind the same 404 the other methods answer, for the same reason.
    """
    conn = auth.conn(request)
    caller = _caller(request)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids)
    offset = _offset(request)
    top = _int(request, "$top", _MEMBERS_DEFAULT, _MEMBERS_MAX)
    total = _member_count(request, conn, name)
    emails = store.conversation_member_emails(conn, SOURCE, name, limit=top, offset=offset)
    select = _select(request)
    value = [_project(_member(conn, e), select) for e in emails]
    fragment = (
        f"teams('{quote(team_id, safe='')}')/channels('{quote(channel_id, safe='')}')/members"
    )
    return _collection(request, fragment, value, offset, total, select)
