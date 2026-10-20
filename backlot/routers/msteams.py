"""Microsoft Teams over the Microsoft Graph API.

Base URL for a client: ``http://<host>/msgraph/v1.0`` — the path real Graph serves under
``https://graph.microsoft.com/v1.0``, so a client changes its base URL and nothing else.

Unlike Slack, Graph answers with real HTTP status codes and an ``{"error": {...}}`` body; the
envelope and the code strings live in :mod:`backlot.errors.msgraph`. Collections are OData:
``@odata.context``, ``@odata.count``, ``value`` and an absolute ``@odata.nextLink`` carrying a
``$skiptoken``.

Response shapes, status codes and query-option answers were measured against graph.microsoft.com
on 2026-10-13 and 2026-10-20, with app-only and delegated tokens on a Teams tenant, a member and a
non-member of a private channel among them. Where a shape below rests on the reference pages
instead, its comment says so.

What this corpus can and cannot model:

- ONE team, because a Backlot corpus has one org. Its id is derived from the org name, and a
  request naming any other team is answered as real Graph answers a team that does not exist.
- Channels only. Graph's chats (1:1 and group DMs) have no counterpart in a channel-based corpus,
  so ``chatId`` is null on every message, exactly as it is on every real CHANNEL message.
- No owners and no deletions: nothing in a corpus says who owns a channel or that a message was
  deleted, so every member's ``roles`` is empty and every ``deletedDateTime`` is null.
- Backlot synthesizes message ids from creation milliseconds, as real ids are; the ``etag`` is the
  last-modified time in the same units, as it is live.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from backlot import auth, store, synth
from backlot.acl import Caller
from backlot.config import get_settings
from backlot.errors import msgraph
from backlot.odata import FilterError, compile_filter
from backlot.openapi import qp
from backlot.pagination import decode_cursor_or_none, encode_cursor

router = APIRouter(prefix="/msgraph/v1.0", tags=["msteams"])
# After every route above, so only a path none of them match reaches it.
unmatched_router = APIRouter(prefix="/msgraph", include_in_schema=False)

SOURCE = "msteams"

# Page sizes, measured: messages and replies default to 20 and refuse a `$top` above 50; the
# directory defaults to 100 and refuses one outside 1..999. `$expand=replies` inlines 200.
_MESSAGES_DEFAULT, _MESSAGES_MAX = 20, 50
_USERS_DEFAULT, _USERS_MAX = 100, 999
_EXPAND_REPLIES = 200

# What each type declares, from Graph's v1.0 CSDL — the names `$select` and `$filter` may use.
_CHANNEL_PROPERTIES = frozenset(
    "createdDateTime description displayName email id isArchived isFavoriteByDefault layoutType "
    "membershipType migrationMode originalCreatedDateTime summary tenantId webUrl".split()
)
_CHANNEL_MEMBERSHIP = ("standard", "private", "unknownFutureValue", "shared")
_MESSAGE_PROPERTIES = frozenset(
    "attachments body channelIdentity chatId createdDateTime deletedDateTime etag eventDetail from "
    "id importance lastEditedDateTime lastModifiedDateTime locale mentions messageHistory "
    "messageType policyViolation reactions replyToId subject summary webUrl hostedContents "
    "replies".split()
)
# `conversationMember` is what a refusal names; its `aadUserConversationMember` subtype is what
# every member here is, so its properties are selectable too.
_MEMBER_PROPERTIES = frozenset(
    "displayName email id roles tenantId userId visibleHistoryStartDateTime".split()
)
_USER_PROPERTIES = frozenset(
    "aboutMe accountEnabled ageGroup assignedLicenses assignedPlans authorizationInfo birthday "
    "businessPhones city companyName consentProvidedForMinor country createdDateTime creationType "
    "customSecurityAttributes deletedDateTime department deviceEnrollmentLimit displayName "
    "employeeHireDate employeeId employeeLeaveDateTime employeeOrgData employeeType "
    "externalUserState externalUserStateChangeDateTime faxNumber givenName hireDate id identities "
    "identityParentId imAddresses interests isManagementRestricted isResourceAccount jobTitle "
    "lastPasswordChangeDateTime legalAgeGroupClassification licenseAssignmentStates mail "
    "mailNickname mailboxSettings mobilePhone mySite officeLocation onPremisesDistinguishedName "
    "onPremisesDomainName onPremisesExtensionAttributes onPremisesImmutableId "
    "onPremisesLastSyncDateTime onPremisesProvisioningErrors onPremisesSamAccountName "
    "onPremisesSecurityIdentifier onPremisesSyncEnabled onPremisesUserPrincipalName otherMails "
    "passwordPolicies passwordProfile pastProjects postalCode preferredDataLocation "
    "preferredLanguage preferredName print provisionedPlans proxyAddresses responsibilities "
    "schools securityIdentifier serviceProvisioningErrors showInAddressList signInActivity "
    "signInSessionsValidFromDateTime skills state streetAddress surname usageLocation "
    "userPrincipalName userType".split()
)

# A reaction's served `displayName`, keyed on the emoji `reactionType` carries — the pairs read off
# reactions made in the Teams client. `like` is the name Graph's older reactionType enum used for
# the first of them.
_REACTION_NAMES = {
    "👍": "Like",
    "😢": "Crying",
    "🎉": "Party popper",
    "😠": "Angry",
    "🔜": "Soon arrow",
    "😐": "Speechless",
}
_LEGACY_REACTIONS = {"like": "👍"}


# --- OpenAPI enrichment --------------------------------------------------
# Parameters are read off the raw request (an OData name starts with `$`, which is not a valid
# Python identifier), so they are declared here rather than in the handler signatures. The response
# models allow extra fields so the builders' full field set — `@odata.context` and friends included
# — passes through unfiltered.


class GraphResponse(BaseModel):
    """Every response here, collection and entity alike.

    No declared fields, deliberately: FastAPI serializes declared fields BEFORE extras, so naming
    ``value`` on the model would emit it ahead of the ``@odata.context`` real Graph opens with.
    """

    model_config = ConfigDict(extra="allow")


_P_TOP = qp("$top", "integer", description="Page size.")
_P_SKIP = qp("$skiptoken", description="Opaque page token, from a previous `@odata.nextLink`.")
_P_SELECT = qp("$select", description="Comma-separated properties to return.")
_P_FILTER = qp("$filter", description="An OData filter over the type's properties.")
_P_PAGED = [_P_TOP, _P_SKIP]
_P_USERS = _P_PAGED + [_P_SELECT, _P_FILTER, qp("$count", description="`true` to count.")]
_P_CHANNELS = [_P_SELECT, _P_FILTER]
_P_MESSAGES = _P_PAGED + [
    _P_SELECT,
    qp(
        "$expand",
        description=(
            "`replies` to include each message's replies inline, up to 200 per message, with "
            "`replies@odata.count` and `replies@odata.nextLink` beside them."
        ),
    ),
]


# --- identity ------------------------------------------------------------


def _org() -> tuple[str, str]:
    s = get_settings()
    return s.org_name, s.org_domain


def _team_id() -> str:
    return synth.msteams_team_id(get_settings().org_name)


def _tenant_id() -> str:
    return synth.msteams_tenant_id(get_settings().org_name)


def _is_jwt(token: str) -> bool:
    """Whether a token parses as a JWT: three segments, the first two base64url JSON objects."""
    parts = token.split(".")
    if len(parts) != 3:
        return False
    try:
        return all(
            isinstance(json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))), dict)
            for p in parts[:2]
        )
    except (ValueError, UnicodeDecodeError):
        return False


def _caller(request: Request) -> Caller:
    """The caller, or Graph's own 401 — which of FOUR, measured: no header at all, a header whose
    token part is blank, a JWT that names no application, and anything else that resolves to
    nobody. ``auth.msgraph_token`` draws the first lines (and documents why the scheme is not one
    of them); ``errors.msgraph`` carries the messages.
    """
    state, token = auth.msgraph_token(request)
    if state == "absent":
        raise msgraph.no_credentials()
    if state == "empty":
        raise msgraph.empty_credentials()
    caller = auth.acl(request).resolve(token)
    if caller is None:
        raise msgraph.foreign_jwt() if _is_jwt(token) else msgraph.bad_token()
    return caller


# --- OData plumbing ------------------------------------------------------


def _param(request: Request, *keys: str) -> str | None:
    for key in keys:
        if (value := request.query_params.get(key)) is not None:
            return value
    return None


def _top(request: Request, default: int, maximum: int) -> int:
    """A message or reply page's ``$top``, refused where Graph refuses it."""
    raw = _param(request, "$top")
    if raw is None:
        return default
    if not raw.isdigit():
        raise msgraph.top_not_integer(raw)
    value = int(raw)
    if value > maximum:
        raise msgraph.top_too_large(maximum, raw)
    if value == 0:
        raise msgraph.bad_request("PageSize needs to be more than 0. (Parameter 'pageSize')")
    return value


def _offset(request: Request, refusal) -> int:
    """The offset a Backlot ``$skiptoken`` names, or ``refusal`` for one that names none. Graph's
    continuation links spell the option either way, so both are read."""
    raw = _param(request, "$skiptoken", "$skipToken")
    if raw is None:
        return 0
    offset = decode_cursor_or_none(raw)
    if offset is None or offset > 2**63 - 1:
        raise refusal()
    return offset


def _next_link(request: Request, offset: int, page_len: int, total: int) -> str | None:
    """The absolute ``@odata.nextLink``, or None on the last page.

    Built from the REQUEST's own URL, so it points back at the host the caller actually reached.
    Graph omits the key entirely when there is nothing more.
    """
    nxt = offset + page_len
    if nxt >= total or not page_len:
        return None
    params = [
        (k, v)
        for k, v in request.query_params.multi_items()
        if k not in ("$skiptoken", "$skipToken")
    ]
    params.append(("$skiptoken", encode_cursor(nxt)))
    query = "&".join(f"{quote(k, safe='$')}={quote(v, safe='')}" for k, v in params)
    return str(request.url.replace(query=query))


def _context(fragment: str, select: list[str] | None = None) -> str:
    """An ``@odata.context``: the metadata URL plus the entity set the response describes.

    The host is graph.microsoft.com's, not this server's, and deliberately: the context names the
    ``$metadata`` document the payload conforms to, which is Microsoft's.
    """
    fields = f"({','.join(select)})" if select else ""
    return f"https://graph.microsoft.com/v1.0/$metadata#{fragment}{fields}"


def _select(request: Request, declared: frozenset[str] | None, type_name: str) -> list[str] | None:
    """The ``$select`` list, each name checked against ``declared``. ``None`` for the directory,
    which answers an undeclared name with an object that lacks it rather than a refusal."""
    raw = _param(request, "$select")
    if not raw:
        return None
    names = [f.strip() for f in raw.split(",") if f.strip()]
    if declared is not None:
        for name in names:
            if name not in declared:
                raise msgraph.unknown_property(name, type_name)
    return names or None


def _project(entity: dict, select: list[str] | None) -> dict:
    """Apply ``$select``: the named properties and the ``@odata.type`` a polymorphic one carries.
    ``id`` is not kept unless it was asked for, as it is not live."""
    if not select:
        return entity
    keep = set(select)
    return {k: v for k, v in entity.items() if k in keep or k == "@odata.type"}


def _collection(fragment, items, *, select=None, count=True, link=None) -> dict:
    """One OData collection response, in the key order real Graph sends. ``@odata.count`` is the
    number of items on THIS page, as it is live on every Teams collection."""
    body: dict = {"@odata.context": _context(fragment, select)}
    if count:
        body["@odata.count"] = len(items)
    if link:
        body["@odata.nextLink"] = link
    body["value"] = items
    return body


def _refuse_options(request: Request, *options: str) -> None:
    """The validator's refusal of an option this method allows none of, named as Graph names it."""
    for option in options:
        if _param(request, f"${option.lower()}") is not None:
            raise msgraph.option_not_allowed(option)


def _filter(request: Request, properties, *, refusal, **kwargs):
    """The ``$filter`` predicate, or the refusal Graph gives this method for one it cannot use."""
    raw = _param(request, "$filter")
    if raw is None:
        return None
    try:
        return compile_filter(raw, properties, **kwargs)
    except FilterError as e:
        raise refusal(e, raw)


def _channel_filter_refusal(e: FilterError, raw: str):
    if e.kind == "property":
        message = f"Could not find a property named '{e.name}' on type 'microsoft.graph.channel'."
    elif e.kind == "enum":
        message = f"The string '{e.name}' is not a valid enumeration type constant."
    elif e.kind == "expected":
        message = f"Expression expected at position {e.position} in '{raw}'."
    else:
        message = f"Syntax error at position {e.position} in '{raw}'."
    return msgraph.bad_request(f"Invalid filter clause: {message}")


def _user_filter_refusal(e: FilterError, raw: str):
    if e.kind == "property":
        return msgraph.unsupported_query(
            f"Property '{e.name}' does not exist as a declared property or extension property."
        )
    if e.kind == "function":
        return msgraph.unsupported_query(
            f"Filter operator '{e.name.capitalize()}' is not supported."
        )
    return _channel_filter_refusal(e, raw)


def _member_filter_refusal(e: FilterError, raw: str):
    if e.kind == "property":
        return msgraph.bad_request(
            f"Invalid filter clause: Could not find a property named '{e.name}' on type "
            "'microsoft.graph.conversationMember'."
        )
    return _channel_filter_refusal(e, raw)


# --- entity builders -----------------------------------------------------


def _iso_millis(epoch_millis: int) -> str:
    """``2021-03-28T21:11:12.395Z`` — the spelling every Graph timestamp takes."""
    dt = datetime.fromtimestamp(epoch_millis / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{epoch_millis % 1000:03d}Z"


def _user(conn, email: str) -> dict:
    """A ``user`` with the eleven properties Graph returns when no ``$select`` narrows it."""
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

    ``joinedTeams`` populates only **id**, **displayName**, **description**, **isArchived** and
    **tenantId**, and sends every other property as null — the reference says so and a live
    response does so.
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


def _channel(request: Request, conn, name: str, *, single: bool) -> dict:
    """One ``channel``. Listing and getting one differ live: the listing sends ``layoutType`` as
    null (a known issue the reference documents) and no ``summary``, which getting one sends
    unasked."""
    cid = synth.msteams_channel_id(name)
    created = _channel_created(request, conn, name)
    private = _is_private(request, conn, name)
    channel = {
        "id": cid,
        "createdDateTime": synth.rfc3339_millis(created),
        "displayName": name,
        # Null on every channel that does not set them; only a team's General channel carries a
        # description and an address, and a corpus channel is never that one.
        "description": None,
        "isFavoriteByDefault": None,
        "email": None,
        "tenantId": _tenant_id(),
        "webUrl": (
            f"https://teams.cloud.microsoft/l/channel/{quote(cid, safe='')}/"
            f"{quote(name, safe='')}?groupId={_team_id()}&tenantId={_tenant_id()}"
            "&allowXTenantAccess=False&ngc=True"
        ),
        # A channel whose messages are granted to the org is one the whole team can read, which is
        # what `standard` means ("Channel inherits the list of members of the parent team"); any
        # narrower grant makes it `private` ("members that are a subset").
        "membershipType": "private" if private else "standard",
        # `chat` is what every channel created in the Teams client answers; `post` is the older
        # layout a channel has only when it was created with it, which a corpus cannot say.
        "layoutType": "chat" if single else None,
        "isArchived": False,
    }
    if single:
        channel["summary"] = {
            "ownersCount": 0,
            "membersCount": len(_member_emails(request, conn, name)),
            "guestsCount": 0,
            "hasMembersFromOtherTenants": False,
        }
    return channel


def _identity(conn, email: str, *, named: bool = True) -> dict:
    """A ``teamworkUserIdentity``. A reactor's is sent without its name (``displayName`` null),
    a sender's and a mentioned person's with it."""
    u = store.get_user(conn, email)
    return {
        "@odata.type": "#microsoft.graph.teamworkUserIdentity",
        "id": synth.msteams_user_id(email),
        "displayName": (u["display_name"] if u else email.split("@")[0]) if named else None,
        "userIdentityType": "aadUser",
        "tenantId": _tenant_id(),
    }


def _identity_set(conn, email: str, *, named: bool = True) -> dict:
    """A ``chatMessageFromIdentitySet``: the user, with an application and a device this corpus
    cannot produce."""
    return {"application": None, "device": None, "user": _identity(conn, email, named=named)}


def _reaction_millis(row) -> list[int]:
    created = int(row["id"])
    return [
        int(r["createdDateTime"]) * 1000 if r.get("createdDateTime") is not None else created
        for r in store.jcol(row, "reactions")
        if isinstance(r, dict)
    ]


def _last_modified_millis(row) -> int:
    """Creation, the last edit, and every reaction move a message's ``lastModifiedDateTime`` and
    its ``etag`` live; a reply does not move its root's."""
    edited = row["last_edited_ts"]
    return max([int(row["id"]), int(edited) * 1000 if edited else 0, *_reaction_millis(row)])


def _reactions(conn, row, created: str) -> list[dict]:
    """The message's ``chatMessageReaction`` list, built from what the corpus stated: an emoji and
    the reactor's ADDRESS, from which the identity set is assembled — Graph's metadata declares
    ``reactionType``, ``createdDateTime`` and ``user`` non-nullable, so a reaction missing any of
    them is a shape the real service cannot send.

    ``createdDateTime`` falls back to the message's own time; what the corpus did state arrives
    here as epoch seconds, parsed at import like every other time on the record.
    """
    out = []
    for r in store.jcol(row, "reactions"):
        if not isinstance(r, dict):
            continue
        emoji = _LEGACY_REACTIONS.get(r.get("reactionType"), r.get("reactionType"))
        when = r.get("createdDateTime")
        out.append(
            {
                "reactionType": emoji,
                "displayName": r.get("displayName") or _REACTION_NAMES.get(emoji),
                "reactionContentUrl": None,
                "createdDateTime": _iso_millis(int(when) * 1000) if when is not None else created,
                "user": _identity_set(conn, r["user"], named=False),
            }
        )
    return out


def _mentions(conn, row) -> list[dict]:
    """A ``chatMessageMention`` per person the corpus says the message mentions. Its ``id`` is the
    one the body's ``<at id="…">`` names."""
    out = []
    for i, m in enumerate(store.jcol(row, "mentions")):
        u = store.get_user(conn, m["user"])
        out.append(
            {
                "id": i,
                "mentionText": m.get("mentionText")
                or (u["display_name"] if u else m["user"].split("@")[0]),
                "mentioned": {
                    "application": None,
                    "device": None,
                    "conversation": None,
                    "tag": None,
                    "user": _identity(conn, m["user"]),
                },
            }
        )
    return out


def _attachments(row) -> list[dict]:
    """A ``chatMessageAttachment`` per file the corpus says the message carries: a reference to
    where the file lives, which is the shape a file shared in a channel takes."""
    out = []
    for i, a in enumerate(store.jcol(row, "attachments")):
        out.append(
            {
                "id": a.get("id") or synth.msteams_attachment_id(row["id"], i),
                "contentType": "reference",
                "contentUrl": a["contentUrl"],
                "content": None,
                "name": a["name"],
                "thumbnailUrl": None,
                "teamsAppId": None,
            }
        )
    return out


def _message(
    request: Request, conn, row, *, replies: list[dict] | None = None, reply_total: int = 0
) -> dict:
    """One ``chatMessage``, in the key order live sends."""
    mid = row["id"]
    created = _iso_millis(int(mid))
    modified = _last_modified_millis(row)
    edited = row["last_edited_ts"]
    cid = synth.msteams_channel_id(row["channel"])
    root = row["reply_to_id"] or mid
    is_system = row["message_type"] == "systemEventMessage"
    # A system event is `unknownFutureValue` unless the caller opted into the evolvable members.
    prefer = request.headers.get("prefer", "")
    message_type = (
        ("systemEventMessage" if "include-unknown-enum-members" in prefer else "unknownFutureValue")
        if is_system
        else "message"
    )
    m = {
        "id": mid,
        "replyToId": row["reply_to_id"],
        "etag": str(modified),
        "messageType": message_type,
        "createdDateTime": created,
        "lastModifiedDateTime": _iso_millis(modified),
        "lastEditedDateTime": _iso_millis(int(edited) * 1000) if edited else None,
        "deletedDateTime": None,
        # A person's message without one carries the empty string; a system event carries null.
        "subject": None if is_system else (row["subject"] or ""),
        "summary": None,
        # Null on a CHANNEL message, always — it names the chat a message was sent in.
        "chatId": None,
        "importance": row["importance"] or "normal",
        "locale": "en-us",
        "webUrl": (
            f"https://teams.microsoft.com/l/message/{quote(cid, safe='')}/{mid}"
            f"?groupId={_team_id()}&tenantId={_tenant_id()}"
            f"&createdTime={mid}&parentMessageId={root}"
        ),
        "policyViolation": None,
        # Live, a system event's detail is a typed object naming what happened; a corpus states
        # no such thing, so it stays null.
        "eventDetail": None,
        "from": None if is_system else _identity_set(conn, row["author_email"]),
        "body": (
            {"contentType": "html", "content": "<systemEventMessage/>"}
            if is_system
            else {"contentType": row["content_type"] or "text", "content": row["content"]}
        ),
        "channelIdentity": {"teamId": _team_id(), "channelId": cid},
        "attachments": _attachments(row),
        "mentions": _mentions(conn, row),
        "reactions": _reactions(conn, row, created),
    }
    if replies is not None:
        thread = (
            f"teams('{_team_id()}')/channels('{quote(cid, safe='')}')/messages('{mid}')/replies"
        )
        m["replies@odata.context"] = _context(thread)
        if replies:
            m["replies@odata.count"] = len(replies)
        if reply_total > len(replies):
            base = str(request.base_url).rstrip("/")
            m["replies@odata.nextLink"] = (
                f"{base}{router.prefix}/teams('{_team_id()}')/channels('{cid}')/messages('{mid}')"
                f"/replies?$skipToken={encode_cursor(len(replies))}"
            )
        m["replies"] = replies
    return m


def _member(conn, email: str, channel_id: str) -> dict:
    """An ``aadUserConversationMember``.

    ``roles`` is empty for an ordinary member and ``["owner"]`` for an owner; nothing in a corpus
    says who owns a channel, so nobody is one. The ``@odata.type`` is not decoration:
    ``conversationMember`` is a polymorphic base, and a client deserializing the collection picks
    its concrete type from that key.
    """
    u = store.get_user(conn, email)
    uid = synth.msteams_user_id(email)
    return {
        "@odata.type": "#microsoft.graph.aadUserConversationMember",
        "id": synth.msteams_member_id(_tenant_id(), channel_id, uid),
        "roles": [],
        "displayName": u["display_name"] if u else email.split("@")[0],
        "visibleHistoryStartDateTime": None,
        "userId": uid,
        "email": email,
        "tenantId": _tenant_id(),
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

    The listing leaves out a private channel the caller is not in ("Teams members can't see
    private or shared channels that they aren't members of"), and the methods that resolve one by
    id refuse it with a 403 rather than serving its name, members or messages.
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


def _member_emails(request: Request, conn, name: str) -> list[str]:
    """A channel's members, in address order: the team — every person in the directory — for a
    standard channel, which "inherits the list of members of the parent team", and the people
    granted it for a private one."""
    if _is_private(request, conn, name):
        return store.conversation_members_by_grant(conn, SOURCE, name) or []
    return store.all_user_emails(conn)


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


# Which answer each method gives a channel id the team does not have, and one it has but the caller
# may not read — measured per method, since Graph answers each differently.
_MISSING_CHANNEL = {
    "channel": lambda cid: msgraph.channel_not_found(cid),
    "members": lambda cid: msgraph.channel_not_found(cid),
    "messages": lambda cid: msgraph.channel_gone(),
}
_HIDDEN_CHANNEL = {
    "channel": lambda: msgraph.forbidden("Failed to execute GetThreadAsync.", inner="Unknown"),
    "members": lambda: msgraph.forbidden("InsufficientPrivileges", inner="1"),
    "messages": lambda: msgraph.forbidden("UnknownError"),
}
_FOREIGN_TEAM = {
    "channel": lambda tid: msgraph.not_found("Channel does not belong to Team."),
    "members": lambda tid: msgraph.not_found("Channel does not belong to Team."),
    "messages": lambda tid: msgraph.not_found(f"Group ID '{tid}' is not found."),
}


def _resolve_channel(request: Request, conn, team_id: str, channel_id: str, ids, kind: str):
    """The channel name behind an id, or the refusal Graph gives ``kind`` of method for it."""
    if team_id != _team_id():
        raise _FOREIGN_TEAM[kind](team_id)
    name = _channel_name(conn, channel_id)
    if name is None:
        raise _MISSING_CHANNEL[kind](channel_id)
    if not _channel_visibility(request, conn, ids)(name):
        raise _HIDDEN_CHANNEL[kind]()
    return name


def _channel_fragment(team_id: str, channel_id: str) -> str:
    return f"teams('{quote(team_id, safe='')}')/channels('{quote(channel_id, safe='')}')"


# --- endpoints -----------------------------------------------------------


@router.get("/me", response_model=GraphResponse, openapi_extra={"parameters": [_P_SELECT]})
async def me(request: Request):
    conn = auth.conn(request)
    caller = _caller(request)
    select = _select(request, None, "user")
    return {
        "@odata.context": _context("users", select) + "/$entity",
        **_project(_me(conn, caller), select),
    }


@router.get("/me/joinedTeams", response_model=GraphResponse)
async def me_joined_teams(request: Request):
    """The teams the caller is in — the one this corpus models, for every caller.

    Not ACL-filtered: a team is not a document. Every principal in the org is in the team; which of
    its CHANNELS they may see is what the ACL decides, and that is answered by
    ``/teams/{id}/channels``.
    """
    _caller(request)
    return _collection("teams", [_team()])


@router.get("/users", response_model=GraphResponse, openapi_extra={"parameters": _P_USERS})
async def users_list(request: Request):
    """The directory: every registered user principal.

    Message senders who are not principals are NOT here — a corpus's transcript speakers can
    outnumber its directory (the same limitation Slack's ``users.list`` states). Such a sender
    still resolves through ``/users/{id}``.
    """
    conn = auth.conn(request)
    _caller(request)
    raw_top = _param(request, "$top")
    top = _USERS_DEFAULT
    if raw_top is not None:
        if not raw_top.isdigit():
            raise msgraph.top_not_integer(raw_top)
        top = int(raw_top)
        if not 1 <= top <= _USERS_MAX:
            raise msgraph.unsupported_query(
                f"Invalid page size specified: '{raw_top}'. Must be between 1 and 999 inclusive."
            )
    offset = _offset(
        request,
        lambda: msgraph.unsupported_query("Unable to execute query; invalid page token specified."),
    )
    # The directory compares strings without regard to case, and evaluates no `contains`.
    keep = _filter(
        request,
        _USER_PROPERTIES,
        refusal=_user_filter_refusal,
        fold_case=True,
        refuse=("contains",),
    )
    users = [_user(conn, e) for e in store.all_user_emails(conn)]
    if keep is not None:
        users = [u for u in users if keep(u)]
    select = _select(request, None, "user")
    page = [_project(u, select) for u in users[offset : offset + top]]
    body: dict = {"@odata.context": _context("users", select)}
    # Only an advanced query counts: `$count=true` together with `ConsistencyLevel: eventual`.
    if (_param(request, "$count") or "").lower() == "true" and request.headers.get(
        "consistencylevel", ""
    ).lower() == "eventual":
        body["@odata.count"] = len(users)
    if link := _next_link(request, offset, len(page), len(users)):
        body["@odata.nextLink"] = link
    body["value"] = page
    return body


@router.get(
    "/users/{user_id:path}", response_model=GraphResponse, openapi_extra={"parameters": [_P_SELECT]}
)
async def users_get(request: Request, user_id: str):
    """One user by object id or userPrincipalName — Graph accepts either at this route."""
    conn = auth.conn(request)
    _caller(request)
    select = _select(request, None, "user")
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
    """Every channel the caller can see, in one response: this method is not paged, and refuses the
    options that would page it."""
    conn = auth.conn(request)
    caller = _caller(request)
    if team_id != _team_id():
        raise msgraph.team_not_found(team_id)
    for option, name in (("$top", "Top"), ("$skiptoken", "SkipToken"), ("$skipToken", "SkipToken")):
        if _param(request, option) is not None:
            raise msgraph.bad_request(
                f"The query specified in the URI is not valid. Query option '{name}' is not "
                "allowed."
            )
    _refuse_options(request, "OrderBy", "Expand")
    select = _select(request, _CHANNEL_PROPERTIES, "channel")
    keep = _filter(
        request,
        _CHANNEL_PROPERTIES,
        refusal=_channel_filter_refusal,
        enums={"membershipType": _CHANNEL_MEMBERSHIP},
    )
    ids = auth.visible_ids(request, caller)
    visible = _channel_visibility(request, conn, ids)
    channels = [
        _channel(request, conn, n, single=False) for n in _channel_names(conn) if visible(n)
    ]
    if keep is not None:
        channels = [c for c in channels if keep(c)]
    page = [_project(c, select) for c in channels]
    return _collection(f"teams('{quote(team_id, safe='')}')/channels", page, select=select)


@router.get(
    "/teams/{team_id}/channels/{channel_id}",
    response_model=GraphResponse,
    openapi_extra={"parameters": [_P_SELECT]},
)
async def channels_get(request: Request, team_id: str, channel_id: str):
    conn = auth.conn(request)
    caller = _caller(request)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids, "channel")
    select = _select(request, _CHANNEL_PROPERTIES, "channel")
    return {
        "@odata.context": _context(f"teams('{quote(team_id, safe='')}')/channels", select)
        + "/$entity",
        **_project(_channel(request, conn, name, single=True), select),
    }


def _message_query(request: Request) -> tuple[list[str] | None, bool]:
    """What a message listing was asked for: its ``$select``, and whether ``$expand=replies``.
    Every other option is refused the way Graph refuses it on this method."""
    if _param(request, "$filter") is not None:
        raise msgraph.bad_request("Parameter 'Filter' not supported for this request.")
    _refuse_options(request, "OrderBy")
    expand = False
    if raw := _param(request, "$expand"):
        for name in (n.strip() for n in raw.split(",")):
            if name.lower() != "replies":
                raise msgraph.unknown_property(name, "chatMessage")
            expand = True
    return _select(request, _MESSAGE_PROPERTIES, "chatMessage"), expand


@router.get(
    "/teams/{team_id}/channels/{channel_id}/messages",
    response_model=GraphResponse,
    openapi_extra={"parameters": _P_MESSAGES},
)
async def channel_messages(request: Request, team_id: str, channel_id: str):
    """The channel's root messages — "the list of messages (without the replies)".

    Ordered by the reply chain's newest activity, measured: a new reply to the oldest root moves it
    to the top of the listing, though the root's own ``lastModifiedDateTime`` does not change.
    """
    conn = auth.conn(request)
    caller = _caller(request)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids, "messages")
    select, expand = _message_query(request)
    top = _top(request, _MESSAGES_DEFAULT, _MESSAGES_MAX)
    offset = _offset(request, lambda: msgraph.bad_request("UnknownError"))
    total = store.count_msteams_top_level(conn, name, ids)
    rows = store.list_msteams_top_level(conn, name, ids, limit=top, offset=offset)
    value = []
    for r in rows:
        if not expand:
            value.append(_project(_message(request, conn, r), select))
            continue
        reply_total = store.count_msteams_replies(conn, name, r["id"], ids)
        replies = store.list_msteams_replies(conn, name, r["id"], ids, limit=_EXPAND_REPLIES)
        message = _message(
            request,
            conn,
            r,
            replies=[_message(request, conn, x) for x in replies],
            reply_total=reply_total,
        )
        value.append(_project(message, select))
    return _collection(
        f"{_channel_fragment(team_id, channel_id)}/messages",
        value,
        select=select,
        link=_next_link(request, offset, len(value), total),
    )


@router.get(
    "/teams/{team_id}/channels/{channel_id}/messages/{message_id}",
    response_model=GraphResponse,
    openapi_extra={"parameters": [_P_SELECT]},
)
async def channel_message(request: Request, team_id: str, channel_id: str, message_id: str):
    """One ROOT message. A reply's id is not resolved here — live answers it 404 — but at
    ``…/messages/{rootId}/replies/{replyId}``; an id that names no message is a 403 to a delegated
    caller, which is what every Backlot token is."""
    conn = auth.conn(request)
    caller = _caller(request)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids, "messages")
    select = _select(request, _MESSAGE_PROPERTIES, "chatMessage")
    row = store.msteams_by_id(conn, name, message_id, ids)
    if row is None:
        raise msgraph.forbidden("UnknownError")
    if row["reply_to_id"] is not None:
        raise msgraph.message_not_found()
    return {
        "@odata.context": _context(f"{_channel_fragment(team_id, channel_id)}/messages", select)
        + "/$entity",
        **_project(_message(request, conn, row), select),
    }


async def _replies(request: Request, team_id: str, channel_id: str, message_id: str):
    conn = auth.conn(request)
    caller = _caller(request)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids, "messages")
    select = _select(request, _MESSAGE_PROPERTIES, "chatMessage")
    top = _top(request, _MESSAGES_DEFAULT, _MESSAGES_MAX)
    offset = _offset(request, msgraph.backend_failure)
    # An id that names no message, or names a reply (Teams threads are one level deep), has no
    # replies: live answers both with an empty collection.
    total = store.count_msteams_replies(conn, name, message_id, ids)
    rows = store.list_msteams_replies(conn, name, message_id, ids, limit=top, offset=offset)
    value = [_project(_message(request, conn, r), select) for r in rows]
    return _collection(
        f"{_channel_fragment(team_id, channel_id)}/messages('{message_id}')/replies",
        value,
        select=select,
        link=_next_link(request, offset, len(value), total),
    )


@router.get(
    "/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies",
    response_model=GraphResponse,
    openapi_extra={"parameters": _P_PAGED + [_P_SELECT]},
)
async def message_replies(request: Request, team_id: str, channel_id: str, message_id: str):
    """A message's replies, newest first."""
    return await _replies(request, team_id, channel_id, message_id)


@router.get(
    "/teams('{team_id}')/channels('{channel_id}')/messages('{message_id}')/replies",
    response_model=GraphResponse,
    include_in_schema=False,
)
async def message_replies_by_key(request: Request, team_id: str, channel_id: str, message_id: str):
    """The same collection addressed by OData key segments, the form ``replies@odata.nextLink``
    takes live, so a client following that link reaches the rest of the thread."""
    return await _replies(request, team_id, channel_id, message_id)


@router.get(
    "/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies/{reply_id}",
    response_model=GraphResponse,
    openapi_extra={"parameters": [_P_SELECT]},
)
async def message_reply(
    request: Request, team_id: str, channel_id: str, message_id: str, reply_id: str
):
    """One reply, addressed under its root."""
    conn = auth.conn(request)
    caller = _caller(request)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids, "messages")
    select = _select(request, _MESSAGE_PROPERTIES, "chatMessage")
    row = store.msteams_by_id(conn, name, reply_id, ids)
    if row is None or row["reply_to_id"] != message_id:
        raise msgraph.message_not_found()
    fragment = f"{_channel_fragment(team_id, channel_id)}/messages('{message_id}')/replies"
    return {
        "@odata.context": _context(fragment, select) + "/$entity",
        **_project(_message(request, conn, row), select),
    }


@router.get(
    "/teams/{team_id}/channels/{channel_id}/members",
    response_model=GraphResponse,
    openapi_extra={"parameters": [_P_SELECT, _P_FILTER]},
)
async def channel_members(request: Request, team_id: str, channel_id: str):
    """The channel's members, in one response: live answers ``$top`` with the whole list."""
    conn = auth.conn(request)
    caller = _caller(request)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids, "members")
    select = _select(request, _MEMBER_PROPERTIES, "conversationMember")
    keep = _filter(request, _MEMBER_PROPERTIES, refusal=_member_filter_refusal)
    members = [_member(conn, e, channel_id) for e in _member_emails(request, conn, name)]
    if keep is not None:
        members = [m for m in members if keep(m)]
    return _collection(
        f"{_channel_fragment(team_id, channel_id)}/members",
        [_project(m, select) for m in members],
    )


# --- everything else -----------------------------------------------------

_RESOURCES = ("me", "users", "teams")


@unmatched_router.api_route(
    "/{rest:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"], include_in_schema=False
)
async def unmatched_path(request: Request, rest: str):
    """A path or method no route above serves. Graph reads the credential before the path: with
    none, or one that resolves to nobody, every such request is the credential's 401 (measured on
    an unknown first segment, an unknown path under a team, and ``POST``/``DELETE`` on ``/me``).
    Past that, an unknown first segment is a 400 naming it, and an unknown path under a resource
    Graph has is a 404."""
    _caller(request)
    segment = rest.removeprefix("v1.0/").split("/")[0]
    if segment.split("(")[0] not in _RESOURCES:
        raise msgraph.unknown_segment(segment)
    raise msgraph.unsupported_path()
