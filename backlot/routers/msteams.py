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
import re
from dataclasses import dataclass
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
    nobody or is spelled as Graph cannot read. ``auth.msgraph_token`` draws the spelling lines;
    ``errors.msgraph`` carries the messages.
    """
    state, token = auth.msgraph_token(request)
    if state == "absent":
        raise msgraph.no_credentials()
    if state == "empty":
        raise msgraph.empty_credentials()
    if state == "unreadable":
        raise msgraph.bad_token()
    caller = auth.acl(request).resolve(token)
    if caller is None:
        raise msgraph.foreign_jwt() if _is_jwt(token) else msgraph.bad_token()
    return caller


# --- OData plumbing ------------------------------------------------------

# The system query options Graph reads. Their names match without regard to case, and on the Teams
# routes without the `$` too; the directory's routes refuse a `$`-less one, and every route refuses
# one given twice.
_OPTIONS = frozenset("select filter top skip skiptoken count orderby expand search format".split())


@dataclass(frozen=True)
class _Option:
    value: str
    # Spelled with `$`. Only then does `@odata.context` echo a `select` list back.
    dollar: bool


def _options(request: Request, *, directory: bool = False) -> dict[str, _Option]:
    """The request's query options, keyed by lowercase name without the `$`."""
    out: dict[str, _Option] = {}
    for key, value in request.query_params.multi_items():
        name = key.lower()
        dollar = name.startswith("$")
        base = name[1:] if dollar else name
        if base not in _OPTIONS:
            continue
        if directory and not dollar:
            raise msgraph.request_bad_request(f"Unrecognized query argument specified: '{key}'.")
        if base in out:
            raise msgraph.bad_request(
                f"Query option '${base}' was specified more than once, but it must be specified "
                "at most once."
            )
        out[base] = _Option(value, dollar)
    return out


def _value(opts: dict[str, _Option], name: str) -> str | None:
    option = opts.get(name)
    return None if option is None else option.value


# A `$top` Graph reads: ASCII digits, an optional sign, spaces or tabs before and spaces after.
_INTEGER = re.compile(r"[ \t]*([+-]?[0-9]+) *")


def _integer(raw: str) -> int:
    """``$top`` as Graph reads it: ``-0`` is zero, and any other negative is not an integer."""
    m = _INTEGER.fullmatch(raw)
    if m is None or int(m.group(1)) < 0:
        raise msgraph.top_not_integer(raw)
    return int(m.group(1))


def _top(opts, default: int, maximum: int) -> int:
    """A message or reply page's ``$top``, refused where Graph refuses it."""
    raw = _value(opts, "top")
    if raw is None:
        return default
    value = _integer(raw)
    if value > maximum:
        raise msgraph.top_too_large(maximum, raw)
    if value == 0:
        raise msgraph.bad_request("PageSize needs to be more than 0. (Parameter 'pageSize')")
    return value


def _offset(opts, refusal) -> int:
    """The offset a Backlot ``$skiptoken`` names, or ``refusal`` for one that names none."""
    raw = _value(opts, "skiptoken")
    if raw is None:
        return 0
    offset = decode_cursor_or_none(raw)
    if offset is None or offset > 2**63 - 1:
        raise refusal()
    return offset


def _next_link(request: Request, offset: int, page_len: int, total: int) -> str | None:
    """The absolute ``@odata.nextLink``, or None on the last page.

    Built from the REQUEST's own URL, so it points back at the host the caller actually reached,
    with every option spelled as the caller spelled it. Graph omits the key entirely when there is
    nothing more.
    """
    nxt = offset + page_len
    if nxt >= total or not page_len:
        return None
    params = [
        (k, v)
        for k, v in request.query_params.multi_items()
        if k.lower().lstrip("$") != "skiptoken"
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


def _select(opts, declared: frozenset[str] | None, type_name: str):
    """``(names, echoed)``: the properties ``select`` names in their declared spelling (a name is
    matched without regard to case), and the list ``@odata.context`` echoes — the same names when
    the option was spelled ``$select``, None when it was ``select``. ``declared`` is None for the
    directory, which answers an undeclared name with an object that lacks it rather than refusing
    it, and keeps such a name as given."""
    option = opts.get("select")
    if option is None or not option.value:
        return None, None
    known = {d.casefold(): d for d in (declared if declared is not None else _USER_PROPERTIES)}
    names = []
    for raw in (f.strip() for f in option.value.split(",")):
        if not raw:
            continue
        name = known.get(raw.casefold())
        if name is None:
            if declared is not None:
                raise msgraph.unknown_property(raw, type_name)
            name = raw
        names.append(name)
    return (names or None), (names or None) if option.dollar else None


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


def _refuse_options(opts, *options: str) -> None:
    """The validator's refusal of an option this method allows none of, named as Graph names it."""
    for option in options:
        if option.lower() in opts:
            raise msgraph.option_not_allowed(option)


def _filter(opts, properties, *, refusal, **kwargs):
    """The ``$filter`` predicate, or the refusal Graph gives this method for one it cannot use."""
    raw = _value(opts, "filter")
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


_ADVANCED = (
    "is not supported because the 'ConsistencyLevel:eventual' header is missing. Refer to "
    "https://aka.ms/graph-docs/advanced-queries for more information"
)


def _user_filter_refusal(e: FilterError, raw: str):
    if e.kind == "property":
        return msgraph.unsupported_query(
            f"Property '{e.name}' does not exist as a declared property or extension property."
        )
    if e.kind == "function" and e.name == "contains":
        return msgraph.unsupported_query("Filter operator 'Contains' is not supported.")
    if e.kind == "function":
        return msgraph.unsupported_query(f"Operator 'endsWith' {_ADVANCED}")
    if e.kind == "operator" and e.name == "ne":
        return msgraph.unsupported_query("Filter operator 'NotEqualsMatch' is not supported.")
    if e.kind == "operator":
        return msgraph.unsupported_query(f"Operator 'not' {_ADVANCED}")
    return _channel_filter_refusal(e, raw)


def _member_filter_refusal(e: FilterError, raw: str):
    if e.kind == "property":
        return msgraph.bad_request(
            f"Invalid filter clause: Could not find a property named '{e.name}' on type "
            "'microsoft.graph.conversationMember'."
        )
    return _channel_filter_refusal(e, raw)


# `$search` on the directory: one or more `"property:term"` clauses joined by AND or OR, each
# matching a property any of whose words starts with the term, without regard to case.
_SEARCH_CLAUSE = re.compile(r'"([^":]+):([^"]*)"')
_WORD = re.compile(r"[^\W_]+")


def _search(raw: str):
    """A predicate over a served user for ``$search``, or Graph's refusal of it."""
    clauses, joins, pos = [], [], 0
    text = raw.strip()
    while True:
        m = _SEARCH_CLAUSE.match(text, pos)
        if m is None:
            colon = text.find(":")
            raise msgraph.bad_request(
                f"Syntax error: character ':' is not valid at position {colon} in '{text}'."
            )
        prop = {p.casefold(): p for p in _USER_PROPERTIES}.get(m.group(1).casefold())
        if prop is None:
            raise msgraph.unsupported_query(
                f"Property '{m.group(1)}' does not exist as a declared property or extension "
                "property."
            )
        clauses.append((prop, m.group(2).casefold()))
        pos = m.end()
        rest = text[pos:]
        if not rest.strip():
            break
        join = re.match(r"\s+(AND|OR)\s+", rest)
        if join is None:
            raise msgraph.bad_request(f"Syntax error at position {pos} in '{text}'.")
        joins.append(join.group(1))
        pos += join.end()

    def hit(user, prop, term):
        value = user.get(prop)
        return isinstance(value, str) and any(
            w.casefold().startswith(term) for w in _WORD.findall(value)
        )

    def keep(user):
        result = hit(user, *clauses[0])
        for join, clause in zip(joins, clauses[1:]):
            result = (
                (result and hit(user, *clause)) if join == "AND" else (result or hit(user, *clause))
            )
        return result

    return keep


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
            "&allowXTenantAccess=False" + ("&ngc=True" if private else "")
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


_GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_CHANNEL_ID = re.compile(r"19:[^@]+@thread\.\w+")
# What a channel id that is not one gets, per method.
_MALFORMED_CHANNEL = {
    "channel": lambda: msgraph.bad_request("channelId is not valid."),
    "members": lambda: msgraph.bad_request("channelId is not valid."),
    "messages": lambda: msgraph.bad_request("UnknownError"),
}


def _check_team_id(team_id: str) -> None:
    if not _GUID.fullmatch(team_id):
        raise msgraph.bad_request("teamId needs to be a valid GUID.")


def _resolve_channel(request: Request, conn, team_id: str, channel_id: str, ids, kind: str):
    """The channel name behind an id, or the refusal Graph gives ``kind`` of method for it."""
    _check_team_id(team_id)
    if not _CHANNEL_ID.fullmatch(channel_id):
        raise _MALFORMED_CHANNEL[kind]()
    if team_id.lower() != _team_id():
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


def _single_entity(opts) -> None:
    """``/me`` and ``/users/{id}`` refuse a filter, and ignore ``$top``."""
    if "filter" in opts:
        raise msgraph.unsupported_query("The specified filter against a single entity is invalid.")


@router.get("/me", response_model=GraphResponse, openapi_extra={"parameters": [_P_SELECT]})
async def me(request: Request):
    conn = auth.conn(request)
    caller = _caller(request)
    opts = _options(request, directory=True)
    _single_entity(opts)
    select, echoed = _select(opts, None, "user")
    return {
        "@odata.context": _context("users", echoed) + "/$entity",
        **_project(_me(conn, caller), select),
    }


def _joined_teams(request: Request) -> dict:
    """The teams the caller is in — the one this corpus models, for every caller. ``$select``
    projects it, ``$filter`` is ignored, and the paging and shaping options are refused."""
    opts = _options(request)
    _refuse_options(opts, "Top", "SkipToken", "Count", "OrderBy", "Expand")
    select, echoed = _select(opts, frozenset(_team()), "team")
    return _collection("teams", [_project(_team(), select)], select=echoed)


@router.get(
    "/me/joinedTeams", response_model=GraphResponse, openapi_extra={"parameters": [_P_SELECT]}
)
async def me_joined_teams(request: Request):
    """Not ACL-filtered: a team is not a document. Every principal in the org is in the team; which
    of its CHANNELS they may see is what the ACL decides, and that is answered by
    ``/teams/{id}/channels``."""
    _caller(request)
    return _joined_teams(request)


@router.get("/users", response_model=GraphResponse, openapi_extra={"parameters": _P_USERS})
async def users_list(request: Request):
    """The directory: every registered user principal, in userPrincipalName order.

    Message senders who are not principals are NOT here — a corpus's transcript speakers can
    outnumber its directory (the same limitation Slack's ``users.list`` states). Such a sender
    still resolves through ``/users/{id}``.

    ``ne``, ``not``, ``endswith``, ``$search`` and a count are advanced queries, answered only with
    ``ConsistencyLevel: eventual`` — ``ne`` with ``$count=true`` beside it as well. Live, an advanced
    query's results come back in an order of the index's own, which is not modelled: they are in
    userPrincipalName order here too.
    """
    conn = auth.conn(request)
    _caller(request)
    opts = _options(request, directory=True)
    top = _USERS_DEFAULT
    if (raw_top := _value(opts, "top")) is not None:
        top = _integer(raw_top)
        if not 1 <= top <= _USERS_MAX:
            raise msgraph.unsupported_query(
                f"Invalid page size specified: '{top}'. Must be between 1 and 999 inclusive."
            )
    offset = _offset(
        opts,
        lambda: msgraph.unsupported_query("Unable to execute query; invalid page token specified."),
    )
    eventual = request.headers.get("consistencylevel", "").lower() == "eventual"
    counted = eventual and (_value(opts, "count") or "").lower() == "true"
    refuse = {"contains"}
    if not eventual:
        refuse |= {"endswith", "not"}
    if not counted:
        refuse.add("ne")
    keep = _filter(
        opts, _USER_PROPERTIES, refusal=_user_filter_refusal, fold_case=True, refuse=refuse
    )
    users = [_user(conn, e) for e in store.all_user_emails(conn)]
    if keep is not None:
        users = [u for u in users if keep(u)]
    if (raw_search := _value(opts, "search")) is not None:
        if not eventual:
            raise msgraph.unsupported_query(
                "Request with $search query parameter only works through MSGraph with a special "
                "request header: 'ConsistencyLevel: eventual'"
            )
        matches = _search(raw_search)
        users = [u for u in users if matches(u)]
    if (raw_order := _value(opts, "orderby")) is not None:
        users = _order_users(users, raw_order, filtered=keep is not None and not eventual)
    select, echoed = _select(opts, None, "user")
    page = [_project(u, select) for u in users[offset : offset + top]]
    body: dict = {"@odata.context": _context("users", echoed)}
    if counted:
        body["@odata.count"] = len(users)
    if link := _next_link(request, offset, len(page), len(users)):
        body["@odata.nextLink"] = link
    body["value"] = page
    return body


# The properties the directory sorts by; any other is refused.
_USER_SORTS = {"displayname": "displayName", "userprincipalname": "userPrincipalName"}


def _order_users(users: list[dict], raw: str, *, filtered: bool) -> list[dict]:
    """``$orderby`` over the directory: one or more ``property [asc|desc]`` terms, each property
    matched without regard to case. A sort beside a filter is refused, unless the query is an
    advanced one."""
    if filtered:
        raise msgraph.unsupported_query("Sorting not supported for current query.")
    terms = []
    for term in (t.strip() for t in raw.split(",")):
        name, _, direction = term.partition(" ")
        prop = _USER_SORTS.get(name.casefold())
        if prop is None:
            raise msgraph.unsupported_query(f"Unsupported sort property '{name}' for 'User'.")
        terms.append((prop, direction.strip().lower() == "desc"))
    for prop, descending in reversed(terms):
        users = sorted(users, key=lambda u: (u.get(prop) or "").casefold(), reverse=descending)
    return users


def _user_or_404(request: Request, conn, user_id: str) -> str:
    email = _resolve_user(request, conn, user_id)
    if email is None:
        raise msgraph.user_not_found(user_id)
    return email


@router.get(
    "/users/{user_id}", response_model=GraphResponse, openapi_extra={"parameters": [_P_SELECT]}
)
async def users_get(request: Request, user_id: str):
    """One user by object id or userPrincipalName — Graph accepts either at this route."""
    conn = auth.conn(request)
    _caller(request)
    opts = _options(request, directory=True)
    _single_entity(opts)
    select, echoed = _select(opts, None, "user")
    email = _user_or_404(request, conn, user_id)
    return {
        "@odata.context": _context("users", echoed) + "/$entity",
        **_project(_user(conn, email), select),
    }


@router.get(
    "/users/{user_id}/joinedTeams",
    response_model=GraphResponse,
    openapi_extra={"parameters": [_P_SELECT]},
)
async def users_joined_teams(request: Request, user_id: str):
    """A user's teams — how a client that cannot ask ``/me`` lists them. Everyone in the directory
    is in the one team."""
    conn = auth.conn(request)
    _caller(request)
    _user_or_404(request, conn, user_id)
    return _joined_teams(request)


def _resolve_user(request: Request, conn, user_id: str) -> str | None:
    """An object id or a userPrincipalName -> the address a corpus knows the person by."""
    if "@" in user_id:
        lowered = user_id.lower()
        for e in store.all_user_emails(conn):
            if e.lower() == lowered:
                return e
    lowered = user_id.lower()
    for e in store.all_user_emails(conn):
        if synth.msteams_user_id(e) == lowered:
            return e
    # Display-only senders — a bot or a speaker the directory does not carry — are not principals,
    # so they are resolved from the message authors instead of coming back not-found at an id the
    # API has just handed the caller on a message's `from`.
    return _sender_by_id(request, conn, lowered)


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
    _check_team_id(team_id)
    if team_id.lower() != _team_id():
        raise msgraph.team_not_found(team_id)
    opts = _options(request)
    for option, name in (("top", "Top"), ("skiptoken", "SkipToken")):
        if option in opts:
            raise msgraph.bad_request(
                f"The query specified in the URI is not valid. Query option '{name}' is not "
                "allowed."
            )
    _refuse_options(opts, "OrderBy", "Expand", "Count")
    select, echoed = _select(opts, _CHANNEL_PROPERTIES, "channel")
    keep = _filter(
        opts,
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
    return _collection(f"teams('{quote(_team_id(), safe='')}')/channels", page, select=echoed)


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
    opts = _options(request)
    _refuse_options(opts, "Expand", "Filter", "Top", "Count")
    select, echoed = _select(opts, _CHANNEL_PROPERTIES, "channel")
    return {
        "@odata.context": _context(f"teams('{quote(_team_id(), safe='')}')/channels", echoed)
        + "/$entity",
        **_project(_channel(request, conn, name, single=True), select),
    }


def _message_query(opts) -> tuple[list[str] | None, list[str] | None, bool]:
    """What a message listing was asked for: its ``select`` (and the echoed list), and whether
    ``expand=replies``. Every other option is refused the way Graph refuses it on this method."""
    if "filter" in opts:
        raise msgraph.bad_request("Parameter 'Filter' not supported for this request.")
    _refuse_options(opts, "OrderBy", "Count")
    expand = False
    if raw := _value(opts, "expand"):
        for name in (n.strip() for n in raw.split(",")):
            if name.lower() != "replies":
                raise msgraph.unknown_property(name, "chatMessage")
            expand = True
    select, echoed = _select(opts, _MESSAGE_PROPERTIES, "chatMessage")
    return select, echoed, expand


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
    opts = _options(request)
    select, echoed, expand = _message_query(opts)
    top = _top(opts, _MESSAGES_DEFAULT, _MESSAGES_MAX)
    offset = _offset(opts, lambda: msgraph.bad_request("UnknownError"))
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
        select=echoed,
        link=_next_link(request, offset, len(value), total),
    )


def _single_message_query(opts) -> tuple[list[str] | None, list[str] | None]:
    """A single message refuses ``$filter`` and ``$top``, and sends ``expand=replies`` to the
    replies route."""
    _refuse_options(opts, "Filter", "Top")
    if raw := _value(opts, "expand"):
        name = raw.split(",")[0].strip()
        if name.lower() == "replies":
            raise msgraph.bad_request(
                "Expand query parameter 'replies' is not supported for this request. Use the "
                "/replies endpoint to get the replies for a message."
            )
        raise msgraph.unknown_property(name, "chatMessage")
    return _select(opts, _MESSAGE_PROPERTIES, "chatMessage")


@router.get(
    "/teams/{team_id}/channels/{channel_id}/messages/{message_id}",
    response_model=GraphResponse,
    openapi_extra={"parameters": [_P_SELECT]},
)
async def channel_message(request: Request, team_id: str, channel_id: str, message_id: str):
    """One ROOT message. A reply's id is not resolved here — live answers it 404 — but at
    ``…/messages/{rootId}/replies/{replyId}``; an id that names no message is a 403 to a delegated
    caller, which is what every Backlot token is, and one that is not a message id at all a 400."""
    conn = auth.conn(request)
    caller = _caller(request)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids, "messages")
    if not message_id.isascii() or not message_id.isdigit():
        raise msgraph.bad_request("UnknownError")
    select, echoed = _single_message_query(_options(request))
    row = store.msteams_by_id(conn, name, message_id, ids)
    if row is None:
        raise msgraph.forbidden("UnknownError")
    if row["reply_to_id"] is not None:
        raise msgraph.message_not_found()
    return {
        "@odata.context": _context(f"{_channel_fragment(team_id, channel_id)}/messages", echoed)
        + "/$entity",
        **_project(_message(request, conn, row), select),
    }


@router.get(
    "/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies",
    response_model=GraphResponse,
    openapi_extra={"parameters": _P_PAGED + [_P_SELECT]},
)
async def message_replies(request: Request, team_id: str, channel_id: str, message_id: str):
    """A message's replies, newest first. An id that names no message, or names a reply (Teams
    threads are one level deep), has none: live answers both with an empty collection."""
    conn = auth.conn(request)
    caller = _caller(request)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids, "messages")
    opts = _options(request)
    _refuse_options(opts, "Count")
    select, echoed = _select(opts, _MESSAGE_PROPERTIES, "chatMessage")
    top = _top(opts, _MESSAGES_DEFAULT, _MESSAGES_MAX)
    offset = _offset(opts, msgraph.backend_failure)
    total = store.count_msteams_replies(conn, name, message_id, ids)
    rows = store.list_msteams_replies(conn, name, message_id, ids, limit=top, offset=offset)
    value = [_project(_message(request, conn, r), select) for r in rows]
    return _collection(
        f"{_channel_fragment(team_id, channel_id)}/messages('{message_id}')/replies",
        value,
        select=echoed,
        link=_next_link(request, offset, len(value), total),
    )


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
    select, echoed = _single_message_query(_options(request))
    row = store.msteams_by_id(conn, name, reply_id, ids)
    if row is None or row["reply_to_id"] != message_id:
        raise msgraph.message_not_found()
    fragment = f"{_channel_fragment(team_id, channel_id)}/messages('{message_id}')/replies"
    return {
        "@odata.context": _context(fragment, echoed) + "/$entity",
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
    opts = _options(request)
    _refuse_options(opts, "Count", "OrderBy")
    if raw := _value(opts, "expand"):
        raise msgraph.unknown_property(raw.split(",")[0].strip(), "conversationMember")
    select, echoed = _select(opts, _MEMBER_PROPERTIES, "conversationMember")
    keep = _filter(opts, _MEMBER_PROPERTIES, refusal=_member_filter_refusal)
    members = [_member(conn, e, channel_id) for e in _member_emails(request, conn, name)]
    if keep is not None:
        members = [m for m in members if keep(m)]
    return _collection(
        f"{_channel_fragment(team_id, channel_id)}/members",
        [_project(m, select) for m in members],
        select=echoed,
    )


# --- the path as Graph reads it ------------------------------------------

_PREFIX = "/msgraph/v1.0/"
_SEGMENTS = {
    s.lower(): s
    for s in ("me", "users", "teams", "channels", "messages", "replies", "members", "joinedTeams")
}
_KEY_SEGMENT = re.compile(r"([A-Za-z]+)\('(.*)'\)")

# Sent by the front Graph sits behind when a request carries `Authorization` twice, before the
# credential or the path is read.
DUPLICATE_HEADER_PAGE = (
    b'<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN""http://www.w3.org/TR/html4/strict.dtd">\r\n'
    b"<HTML><HEAD><TITLE>Bad Request</TITLE>\r\n"
    b'<META HTTP-EQUIV="Content-Type" Content="text/html; charset=us-ascii"></HEAD>\r\n'
    b"<BODY><h2>Bad Request - Invalid Header</h2>\r\n"
    b"<hr><p>HTTP Error 400. The request has an invalid header name.</p>\r\n</BODY></HTML>\r\n"
)


def normalize_path(scope) -> None:
    """Rewrite a Graph path to the one spelling the routes above match, as Graph reads it: a
    resource name in any case, an id as an OData key segment (``teams('{id}')``), a GUID in either
    case, and one trailing slash are each the same path live."""
    path = scope["path"]
    if not path.startswith(_PREFIX):
        return
    rest = path[len(_PREFIX) :]
    if rest.endswith("/"):
        rest = rest[:-1]
    out: list[str] = []
    for segment in rest.split("/"):
        key = _KEY_SEGMENT.fullmatch(segment)
        parts = (
            [key.group(1), key.group(2)] if key and key.group(1).lower() in _SEGMENTS else [segment]
        )
        for part in parts:
            if part.lower() in _SEGMENTS:
                part = _SEGMENTS[part.lower()]
            elif _GUID.fullmatch(part):
                part = part.lower()
            out.append(part)
    new = _PREFIX + "/".join(out)
    if new != path:
        scope["path"] = new
        scope["raw_path"] = quote(new, safe="/:@!$&'()*+,;=-._~").encode()


# --- everything else -----------------------------------------------------

_RESOURCES = ("me", "users", "teams")


@unmatched_router.api_route(
    "/{rest:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"], include_in_schema=False
)
async def unmatched_path(request: Request, rest: str):
    """A path or method no route above serves. Graph reads the credential before the path: with
    none, or one that resolves to nobody, every such request is the credential's 401 (measured on
    an unknown first segment, an unknown path under a team, and ``POST``/``DELETE`` on ``/me``).
    Past that, an unknown first segment is a 400 naming it; under a person (``/me``,
    ``/users/{id}``) the unknown property is a 404 naming it; under a channel, a 400 naming it; and
    under a team, a 404."""
    _caller(request)
    segments = rest.removeprefix("v1.0/").split("/")
    if segments[0] not in _RESOURCES:
        raise msgraph.unknown_segment(segments[0])
    person = {"me": 1, "users": 2}.get(segments[0])
    if person is not None and len(segments) > person:
        raise msgraph.user_not_found(segments[person])
    if segments[0] == "teams" and len(segments) > 4 and segments[2] == "channels":
        raise msgraph.unknown_segment(segments[-1])
    raise msgraph.unsupported_path()
