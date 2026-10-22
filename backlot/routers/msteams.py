"""Microsoft Teams over the Microsoft Graph API.

Base URL for a client: ``http://<host>/msgraph/v1.0`` — the path real Graph serves under
``https://graph.microsoft.com/v1.0``, so a client changes its base URL and nothing else.

Unlike Slack, Graph answers with real HTTP status codes and an ``{"error": {...}}`` body; the
envelope and the code strings live in :mod:`backlot.errors.msgraph`. Collections are OData:
``@odata.context``, ``value`` and an absolute ``@odata.nextLink``, with a page's ``@odata.count``
on the Teams collections (see ``docs/supported-sources.md`` for the directory's).

Response shapes, status codes and query-option answers were measured against graph.microsoft.com on
2026-10-13, 2026-10-20 and 2026-10-21, with app-only and delegated tokens on a Teams tenant, a
member and a non-member of a private channel among them. Where a shape below rests on the reference
pages instead, its comment says so.


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
from typing import Callable, Mapping
from urllib.parse import quote

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from backlot import auth, store, synth
from backlot.acl import Caller
from backlot.config import get_settings
from backlot.errors import msgraph
from backlot.odata import (
    ORDERBY_NODES,
    ExecutionError,
    Filter,
    FilterError,
    Node,
    ascii_lower,
    compile_filter,
    is_blank,
    parse_orderby,
    strip_blanks,
)
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

# What each type declares, from Graph's v1.0 CSDL, by EDM type: the names `$select`, `$filter`
# and `$orderby` may use, and what a filter compares each one as.


def _typed(spec: str) -> dict[str, str]:
    """``"Edm.String: a b; Edm.Boolean: c"`` as ``{"a": "Edm.String", …}``."""
    out = {}
    for group in spec.split(";"):
        type_name, _, names = group.partition(":")
        out.update(dict.fromkeys(names.split(), type_name.strip()))
    return out


_G = "microsoft.graph"
_CHANNEL_TYPES = _typed(
    f"Edm.Boolean: isArchived isFavoriteByDefault; "
    f"Edm.DateTimeOffset: createdDateTime originalCreatedDateTime; "
    f"Edm.String: description displayName email id tenantId webUrl; "
    f"{_G}.channelLayoutType: layoutType; {_G}.channelMembershipType: membershipType; "
    f"{_G}.channelSummary: summary; {_G}.migrationMode: migrationMode"
)
_MESSAGE_TYPES = _typed(
    f"Collection({_G}.chatMessageAttachment): attachments; "
    f"Collection({_G}.chatMessageHistoryItem): messageHistory; "
    f"Collection({_G}.chatMessageMention): mentions; "
    f"Collection({_G}.chatMessageReaction): reactions; "
    f"Edm.DateTimeOffset: createdDateTime deletedDateTime lastEditedDateTime lastModifiedDateTime; "
    f"Edm.String: chatId etag id locale replyToId subject summary webUrl; "
    f"{_G}.channelIdentity: channelIdentity; {_G}.chatMessageFromIdentitySet: from; "
    f"{_G}.chatMessageImportance: importance; {_G}.chatMessagePolicyViolation: policyViolation; "
    f"{_G}.chatMessageType: messageType; {_G}.eventMessageDetail: eventDetail; {_G}.itemBody: body"
)
# `conversationMember` is what a refusal names; its `aadUserConversationMember` subtype is what
# every member here is, so its properties are selectable too.
_MEMBER_TYPES = _typed(
    "Collection(Edm.String): roles; Edm.DateTimeOffset: visibleHistoryStartDateTime; "
    "Edm.String: displayName email id tenantId userId"
)
_USER_TYPES = _typed(
    "Collection(Edm.String): businessPhones imAddresses interests otherMails pastProjects "
    "proxyAddresses responsibilities schools skills; "
    f"Collection({_G}.assignedLicense): assignedLicenses; "
    f"Collection({_G}.assignedPlan): assignedPlans; "
    f"Collection({_G}.licenseAssignmentState): licenseAssignmentStates; "
    f"Collection({_G}.objectIdentity): identities; "
    f"Collection({_G}.onPremisesProvisioningError): onPremisesProvisioningErrors; "
    f"Collection({_G}.provisionedPlan): provisionedPlans; "
    f"Collection({_G}.serviceProvisioningError): serviceProvisioningErrors; "
    "Edm.Boolean: accountEnabled isManagementRestricted isResourceAccount onPremisesSyncEnabled "
    "showInAddressList; "
    "Edm.DateTimeOffset: birthday createdDateTime deletedDateTime employeeHireDate "
    "employeeLeaveDateTime externalUserStateChangeDateTime hireDate lastPasswordChangeDateTime "
    "onPremisesLastSyncDateTime signInSessionsValidFromDateTime; "
    "Edm.Int32: deviceEnrollmentLimit; "
    "Edm.String: aboutMe ageGroup city companyName consentProvidedForMinor country creationType "
    "department displayName employeeId employeeType externalUserState faxNumber givenName id "
    "identityParentId jobTitle legalAgeGroupClassification mail mailNickname mobilePhone mySite "
    "officeLocation onPremisesDistinguishedName onPremisesDomainName onPremisesImmutableId "
    "onPremisesSamAccountName onPremisesSecurityIdentifier onPremisesUserPrincipalName "
    "passwordPolicies postalCode preferredDataLocation preferredLanguage preferredName "
    "securityIdentifier state streetAddress surname usageLocation userPrincipalName userType; "
    f"{_G}.authorizationInfo: authorizationInfo; "
    f"{_G}.customSecurityAttributeValue: customSecurityAttributes; "
    f"{_G}.employeeOrgData: employeeOrgData; {_G}.mailboxSettings: mailboxSettings; "
    f"{_G}.onPremisesExtensionAttributes: onPremisesExtensionAttributes; "
    f"{_G}.passwordProfile: passwordProfile; {_G}.signInActivity: signInActivity; "
    f"{_G}.userPrint: print"
)
_CHANNEL_PROPERTIES = frozenset(_CHANNEL_TYPES)
_MESSAGE_PROPERTIES = frozenset(_MESSAGE_TYPES)
_MEMBER_PROPERTIES = frozenset(_MEMBER_TYPES)
_USER_PROPERTIES = frozenset(_USER_TYPES)
_USER_KNOWN = frozenset(ascii_lower(p) for p in _USER_TYPES)
# Every enum a filter above can compare, with its members in declared order.
_ENUMS = {
    f"{_G}.channelMembershipType": ("standard", "private", "unknownFutureValue", "shared"),
    f"{_G}.channelLayoutType": ("post", "chat", "unknownFutureValue"),
    f"{_G}.migrationMode": ("inProgress", "completed", "unknownFutureValue"),
    f"{_G}.chatMessageType": (
        "message",
        "chatEvent",
        "typing",
        "unknownFutureValue",
        "systemEventMessage",
    ),
    f"{_G}.chatMessageImportance": ("normal", "high", "urgent", "unknownFutureValue"),
}

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
            "`replies@odata.context`, a count when there are any, and a nextLink past 200."
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

# The system query options Graph reads. Their names match without regard to ASCII case, and on the
# Teams routes without the `$` too; the directory's routes refuse a `$`-less one, and every route
# refuses one given twice.
_OPTIONS = frozenset("select filter top skip skiptoken count orderby expand search format".split())


@dataclass(frozen=True)
class _Option:
    value: str
    # Spelled with `$`. Only then does `@odata.context` echo a `select` list back.
    dollar: bool


def _options(request: Request, *, directory: bool = False) -> dict[str, _Option]:
    """The request's query options, keyed by lowercase name without the `$`. A Teams route refuses
    an empty `$skiptoken` before it reads anything else."""
    out: dict[str, _Option] = {}
    for key, value in request.query_params.multi_items():
        name = ascii_lower(key)
        dollar = name.startswith("$")
        base = name[1:] if dollar else name
        if base not in _OPTIONS:
            continue
        if directory and not dollar:
            raise msgraph.request_bad_request(f"Unrecognized query argument specified: '{key}'.")
        if base in out:
            if out[base].dollar != dollar:
                raise msgraph.bad_request(
                    f"An item with the same key has already been added. Key: ${base}"
                )
            raise msgraph.bad_request(
                f"Query option '${base}' was specified more than once, but it must be specified "
                "at most once."
            )
        out[base] = _Option(value, dollar)
    if not directory and (token := out.get("skiptoken")) is not None and token.value == "":
        raise msgraph.empty_option_value()
    return out


def _value(opts: dict[str, _Option], name: str) -> str | None:
    option = opts.get(name)
    return None if option is None else option.value


_INT32, _INT64 = 2**31 - 1, 2**63 - 1
_DIGITS = re.compile(r"([+-]?)([0-9]+)")


def _integer(raw: str, option: str = "top") -> int:
    """``$top`` or ``$skip`` as Graph reads it: a 64-bit integer, with a sign and .NET's blanks
    around it. ``-0`` is zero, and any other negative is not a value."""
    m = _DIGITS.fullmatch(strip_blanks(raw))
    digits = m.group(2).lstrip("0") if m else ""
    if (
        m is None
        or len(digits) > 19
        or int(digits or "0") > _INT64
        or (digits and m.group(1) == "-")
    ):
        raise msgraph.invalid_integer(option, raw)
    return int(digits or "0")


def _top(opts, default: int, maximum: int) -> int:
    """A message or reply page's ``$top``: past Int32 and then past the method's limit, the
    validator names the limit it hit."""
    raw = _value(opts, "top")
    if raw is None:
        return default
    value = _integer(raw)
    if value > _INT32:
        raise msgraph.top_too_large(_INT32, raw)
    if value > maximum:
        raise msgraph.top_too_large(maximum, raw)
    return value


def _offset(opts, refusal) -> int:
    """The offset a Backlot ``$skiptoken`` names, or ``refusal`` for one that names none. A blank
    one is the first page."""
    raw = _value(opts, "skiptoken")
    if raw is None or not strip_blanks(raw):
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
        if ascii_lower(k).lstrip("$") != "skiptoken"
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


def _select(opts, declared: frozenset[str], type_name: str, *, strict: bool = True):
    """``(names, echoed)``: the properties ``select`` names in their declared spelling (a name is
    matched without regard to ASCII case), and the list ``@odata.context`` echoes — the same names
    when the option was spelled ``$select``, None when it was ``select``. ``strict`` refuses a name
    the type does not declare; otherwise, as the directory and the team routes do, such a name is
    kept as given and answered with an object that lacks it."""
    option = opts.get("select")
    if option is None or not option.value:
        return None, None
    known = {ascii_lower(d): d for d in declared}
    names = []
    for raw in (f.strip() for f in option.value.split(",")):
        if not raw:
            continue
        name = known.get(ascii_lower(raw))
        if name is None:
            if strict:
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


def _filter(opts, types, *, refusal, **kwargs) -> Filter | None:
    """The ``$filter``, or the refusal Graph gives this method for one it cannot use. An empty or
    blank one is no filter."""
    raw = _value(opts, "filter")
    if raw is None or not strip_blanks(raw):
        return None
    try:
        return compile_filter(raw, types, enums=_ENUMS, **kwargs)
    except FilterError as e:
        raise refusal(e, raw)


def _filter_refusal(type_name: str):
    """The binder's refusal of a ``$filter`` over ``type_name``."""

    def refuse(e: FilterError, raw: str):
        if e.kind == "property":
            return msgraph.bad_request(
                f"Invalid filter clause: Could not find a property named '{e.name}' on type "
                f"'microsoft.graph.{type_name}'."
            )
        return msgraph.bad_request(f"Invalid filter clause: {e.message}")

    return refuse


_ADVANCED = (
    "is not supported because the 'ConsistencyLevel:eventual' header is missing. Refer to "
    "https://aka.ms/graph-docs/advanced-queries for more information"
)


def _user_filter_refusal(e: FilterError, raw: str):
    """What the binder refuses on the directory. A name the type does not declare binds there,
    and `_directory_refusal` refuses it in the directory's own order."""
    return msgraph.bad_request(f"Invalid filter clause: {e.message}")


# The directory's names for the comparison operators.
_DIRECTORY_OPERATORS = {
    "eq": "EqualsMatch",
    "ne": "NotEqualsMatch",
    "gt": "Greater",
    "ge": "GreaterOrEqual",
    "lt": "Less",
    "le": "LessOrEqual",
}


def _directory_refusal(root: Node, *, eventual: bool, counted: bool) -> None:
    """The directory's refusal of a bound filter, as measured: first the shape of every
    comparison, in the order the filter is written, then — again in that order — a property it does
    not have, an operator it does not run, a comparison with null, and the advanced operators an
    advanced query is needed for. Within one comparison, a plain query reads the property before
    the operator, and an advanced one the operator first."""
    unsupported = msgraph.unsupported_query("Unsupported Query.")

    def is_property(node: Node) -> bool:
        return node.kind in ("name", "path")

    def shapes(node: Node) -> None:
        if node.kind in ("nary", "not"):
            for child in node.children:
                shapes(child)
        elif node.kind == "lambda":
            for child in node.children[1:]:
                shapes(child)
        elif node.kind == "in":
            if not is_property(node.children[0]):
                raise unsupported
        elif node.kind == "binary" and node.op in _DIRECTORY_OPERATORS:
            left, right = node.children
            if is_property(left) or (left.kind == "literal" and is_property(right)):
                if is_property(left) and is_property(right):
                    raise unsupported
                return
            if left.kind == "literal" and right.kind == "literal":
                raise unsupported
            raise msgraph.unsupported_query(
                f"Invalid expression caused by operator '{_DIRECTORY_OPERATORS[node.op]}'."
            )
        elif node.kind != "call":
            raise unsupported

    def declared(node: Node, variables: set[str]) -> None:
        name = node.token.text
        if node.kind == "name" and name not in variables and ascii_lower(name) not in _USER_KNOWN:
            raise msgraph.unsupported_query(
                f"Property '{name}' does not exist as a declared property or extension property."
            )

    def operator(op: str) -> None:
        if op in ("gt", "lt"):
            word = _DIRECTORY_OPERATORS[op]
            if eventual:
                raise msgraph.unsupported_query(f"Operator: '{word}' is not supported.")
            raise msgraph.unsupported_query(
                f"Filter operator '{word}' is not supported. Use '{word}OrEqual' instead."
            )
        if op == "ne" and not counted:
            raise msgraph.unsupported_query("Filter operator 'NotEqualsMatch' is not supported.")

    def refusals(node: Node, variables: set[str]) -> None:
        if node.kind == "nary":
            for child in node.children:
                refusals(child, variables)
        elif node.kind == "not":
            if not eventual:
                raise msgraph.unsupported_query(f"Operator 'not' {_ADVANCED}")
            refusals(node.children[0], variables)
        elif node.kind == "lambda":
            collection = node.children[0]
            declared(collection, variables)
            if not eventual:
                raise _null_clause(collection.token.text)
            if node.variable is not None:
                refusals(node.children[1], variables | {node.variable})
        elif node.kind == "call":
            name = ascii_lower(node.token.text)
            if name == "contains":
                raise msgraph.unsupported_query(
                    "Operator: 'Contains' is not supported."
                    if eventual
                    else "Filter operator 'Contains' is not supported."
                )
            if name == "endswith" and not eventual:
                raise msgraph.unsupported_query(f"Operator 'endsWith' {_ADVANCED}")
            for arg in node.children:
                declared(arg, variables)
        elif node.kind == "in":
            declared(node.children[0], variables)
        elif node.kind == "binary":
            left, right = node.children
            prop, other = (left, right) if is_property(left) else (right, left)
            if eventual:
                operator(node.op)
                declared(prop, variables)
            else:
                declared(prop, variables)
                operator(node.op)
                if other.kind == "literal" and other.token.text == "null":
                    raise _null_clause(prop.token.text)

    shapes(root)
    refusals(root, set())


def _null_clause(name: str):
    return msgraph.unsupported_query(
        f"Unsupported or invalid query filter clause specified for property '{name}' of resource "
        "'User'."
    )


# `$search` on the directory: one or more `"property:term"` clauses joined by AND or OR, each
# matching a property any of whose words starts with the term, without regard to case. A clause is
# a quoted phrase or a bare word; a colon or an apostrophe outside quotes is not a character the
# lexer reads.
_SEARCH_WORD = re.compile(r"(?:(?![\x1c-\x1f])[^\s\"':])+")
_WORD = re.compile(r"[^\W_]+")


def _search_clauses(text: str) -> tuple[list[str], list[str]]:
    clauses: list[str] = []
    joins: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if is_blank(c):
            i += 1
            continue
        if c != '"' and (c in ":'" or not _SEARCH_WORD.match(text, i)):
            raise msgraph.bad_request(
                f"Syntax error: character '{c}' is not valid at position {i} in '{text}'."
            )
        if c == '"':
            close = text.find('"', i + 1)
            close = len(text) if close < 0 else close
            phrase, i = text[i + 1 : close], close + 1
        else:
            phrase = _SEARCH_WORD.match(text, i).group()
            i += len(phrase)
            if phrase in ("AND", "OR") and len(clauses) > len(joins):
                joins.append(phrase)
                continue
        # Two clauses side by side are joined by AND.
        if len(clauses) > len(joins):
            joins.append("AND")
        clauses.append(phrase)
    if not clauses or len(joins) == len(clauses):
        raise msgraph.bad_request(f"Expression expected at position {len(text)} in '{text}'.")
    return clauses, joins


def _search(raw: str):
    """A predicate over a served user for ``$search``, or Graph's refusal of it."""
    clause_texts, joins = _search_clauses(strip_blanks(raw))
    known = {ascii_lower(p): p for p in _USER_PROPERTIES}
    clauses = []
    for clause in clause_texts:
        name, colon, term = clause.partition(":")
        if not colon:
            raise msgraph.unsupported_query(
                f"Clause '{clause}' in $search is not of right format \"property:value\""
            )
        if not term:
            raise msgraph.unsupported_query(
                f"Clause '{clause}' in $search is of right format \"property:value\" but value is "
                "empty"
            )
        prop = known.get(ascii_lower(name))
        if prop is None:
            raise msgraph.unsupported_query(
                f"Property '{name}' does not exist as a declared property or extension property."
            )
        clauses.append((prop, term.casefold()))

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


# --- one query, read the way a Teams route reads it ------------------------
#
# Measured order, per request: an empty `$skiptoken` (`_options`), then the values of `$top` and
# `$skip`, then the OData binder resolves every property a `$filter`, `$orderby`, `$select` or
# `$expand` names (an `$expand` must name a navigation property), then the validator refuses the
# options the method allows none of, a `$top` or `$skip` past its limit and a filter past its
# limits, then the team id's form is checked, then the method's own checks run, and only then is
# the team, channel or message looked up. A comparison .NET cannot run is refused after that, where
# the query runs (`_Query.run`, `_kept`).

# The validator's order, where a request carries more than one option it refuses.
_VALIDATOR_ORDER = (
    "Top",
    "Skip",
    "SkipToken",
    "OrderBy",
    "Filter",
    "Count",
    "Search",
    "Expand",
    "Format",
)
_NODE_LIMIT = 100


@dataclass(frozen=True)
class _Method:
    type_name: str
    types: Mapping[str, str]
    navigations: frozenset[str]
    refused: frozenset[str] = frozenset()
    # The method's `$top` limit, None where the validator refuses `$top` outright.
    top_max: int | None = None
    # What a filter's property weighs in the validator's node count, by its type; None where the
    # count is not limited. Measured: the channel listing weighs a boolean 2 and any other
    # property 3, the member listing every property 2.
    node_weight: Callable[[str | None], int] | None = None
    # Whether `$select` refuses a name the type does not declare.
    strict_select: bool = True
    # Whether the validator refuses every `$expand` the binder resolves.
    expand_refused: bool = False

    @property
    def properties(self) -> frozenset[str]:
        return frozenset(self.types)


@dataclass(frozen=True)
class _Query:
    select: list[str] | None
    echoed: list[str] | None
    keep: Filter | None
    expand: list[str]
    skip: int

    def run(self) -> None:
        """Refuse a filter that binds and that .NET cannot run — a boolean ordered against
        another — which Graph answers only once the team or channel is found."""
        if self.keep is not None and self.keep.deferred:
            raise msgraph.query_option_invalid(self.keep.deferred)


def _kept(keep: Filter | None, items: list[dict]) -> list[dict]:
    """The items ``keep`` keeps. A filter .NET fails to run over them — a division by zero — is
    Graph's 500."""
    if keep is None:
        return items
    try:
        return [item for item in items if keep(item)]
    except ExecutionError:
        raise msgraph.execution_failed() from None


def _validate_filter(keep: Filter, method: _Method) -> None:
    if keep.lambda_depth > 1:
        raise msgraph.query_option_invalid(
            "The Any/All nesting limit of '1' has been exceeded. 'MaxAnyAllExpressionDepth' can be "
            "configured on ODataQuerySettings or EnableQueryAttribute."
        )
    if method.node_weight is not None and keep.nodes(method.node_weight) > _NODE_LIMIT:
        raise msgraph.query_option_invalid(
            f"The node count limit of '{_NODE_LIMIT}' has been exceeded. To increase the limit, "
            "set the 'MaxNodeCount' property on EnableQueryAttribute or ODataValidationSettings."
        )


def _read_query(opts, method: _Method) -> _Query:
    """Read, bind, then validate, ``opts`` for ``method``."""
    if (raw_top := _value(opts, "top")) is not None:
        _integer(raw_top)
    skip = 0
    if (raw_skip := _value(opts, "skip")) is not None:
        skip = _integer(raw_skip, "skip")
    keep = _filter(opts, method.types, refusal=_filter_refusal(method.type_name))
    if (raw := _value(opts, "orderby")) is not None and strip_blanks(raw):
        try:
            terms = parse_orderby(raw)
        except FilterError as e:
            raise msgraph.bad_request(e.message) from None
        known = {ascii_lower(p) for p in method.properties}
        for name, _ in terms:
            if name not in ORDERBY_NODES and ascii_lower(name) not in known:
                raise msgraph.bad_request(
                    f"Could not find a property named '{name}' on type "
                    f"'microsoft.graph.{method.type_name}'."
                )
    select, echoed = _select(
        opts, method.properties | method.navigations, method.type_name, strict=method.strict_select
    )
    expand = []
    if raw := _value(opts, "expand"):
        navs = {ascii_lower(n): n for n in method.navigations}
        for name in (n.strip() for n in raw.split(",") if n.strip()):
            if ascii_lower(name) not in navs:
                raise msgraph.unknown_property(name, method.type_name)
            expand.append(navs[ascii_lower(name)])
    for option in _VALIDATOR_ORDER:
        if ascii_lower(option) not in opts:
            continue
        if option in method.refused:
            raise msgraph.option_not_allowed(option)
        if option == "Top" and method.top_max is not None:
            _top(opts, method.top_max, method.top_max)
        if option == "Skip" and skip > _INT32:
            raise msgraph.skip_too_large(raw_skip)
        if option == "Filter" and keep is not None:
            _validate_filter(keep, method)
        if option == "Expand" and method.expand_refused and expand:
            raise msgraph.query_option_invalid(
                f"The property '{expand[0]}' cannot be used in the $expand query option."
            )
    return _Query(select, echoed, keep, expand, skip)


_TEAM_NAVIGATIONS = frozenset(
    "allChannels channels group incomingChannels installedApps members operations "
    "permissionGrants photo primaryChannel schedule tags template".split()
)
# A live `team` carries three properties its v1.0 CSDL does not declare.
_TEAM_TYPES = _typed(
    "Edm.Boolean: isArchived isMembershipLimitedToOwners; Edm.DateTimeOffset: createdDateTime; "
    "Edm.String: classification description displayName firstChannelName id internalId tenantId "
    f"webUrl; {_G}.teamFunSettings: funSettings; {_G}.teamGuestSettings: guestSettings; "
    f"{_G}.teamMemberSettings: memberSettings; {_G}.teamMessagingSettings: messagingSettings; "
    f"{_G}.teamSpecialization: specialization; {_G}.teamSummary: summary; "
    f"{_G}.teamVisibilityType: visibility; {_G}.teamDiscoverySettings: discoverySettings; "
    f"{_G}.teamTagSettings: tagSettings"
)
_ALL = frozenset(_VALIDATOR_ORDER)
_JOINED_TEAMS = _Method(
    "team", _TEAM_TYPES, _TEAM_NAVIGATIONS, refused=_ALL - {"Filter"}, strict_select=False
)
_TEAM_GET = _Method(
    "team",
    _TEAM_TYPES,
    _TEAM_NAVIGATIONS,
    refused=_ALL - {"Expand"},
    strict_select=False,
    expand_refused=True,
)
_CHANNEL_NAVIGATIONS = frozenset(
    "allMembers enabledApps filesFolder members messages sharedWithTeams tabs".split()
)
# The listing's `$top` and `$skiptoken` are refused after the validator, in words of their own.
_CHANNEL_LIST = _Method(
    "channel",
    _CHANNEL_TYPES,
    _CHANNEL_NAVIGATIONS,
    refused=frozenset({"Skip", "Search", "OrderBy", "Expand", "Count", "Format"}),
    node_weight=lambda t: 2 if t == "Edm.Boolean" else 3,
)
_CHANNEL_GET = _Method(
    "channel",
    _CHANNEL_TYPES,
    _CHANNEL_NAVIGATIONS,
    refused=_ALL,
)
_MESSAGE_NAVIGATIONS = frozenset({"hostedContents", "replies"})
_MESSAGE_LIST = _Method(
    "chatMessage",
    _MESSAGE_TYPES,
    _MESSAGE_NAVIGATIONS,
    refused=frozenset({"OrderBy", "Count", "Search", "Format"}),
    top_max=_MESSAGES_MAX,
)
_REPLY_LIST = _Method(
    "chatMessage",
    _MESSAGE_TYPES,
    _MESSAGE_NAVIGATIONS,
    refused=frozenset({"OrderBy", "Filter", "Count", "Search", "Format"}),
    top_max=_MESSAGES_MAX,
)
_MESSAGE_GET = _Method(
    "chatMessage",
    _MESSAGE_TYPES,
    _MESSAGE_NAVIGATIONS,
    refused=frozenset({"Top", "Skip", "SkipToken", "OrderBy", "Filter", "Count", "Format"}),
)
_MEMBER_LIST = _Method(
    "conversationMember",
    _MEMBER_TYPES,
    frozenset(),
    refused=frozenset({"Skip", "OrderBy", "Count", "Search", "Format"}),
    node_weight=lambda t: 2,
)


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
    "channel": lambda cid, uid: msgraph.not_in_roster(uid, cid),
    "members": lambda cid, uid: msgraph.not_in_roster(uid, cid),
    "messages": lambda cid, uid: msgraph.forbidden("UnknownError"),
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


def _resolve_channel(
    request: Request, conn, team_id: str, channel_id: str, ids, kind: str, caller: Caller
):
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
        raise _HIDDEN_CHANNEL[kind](channel_id, synth.msteams_user_id(caller.email or ""))
    return name


def _channel_fragment(team_id: str, channel_id: str) -> str:
    return f"teams('{quote(team_id, safe='')}')/channels('{quote(channel_id, safe='')}')"


# --- endpoints -----------------------------------------------------------

_USER_NAVIGATIONS = frozenset(
    "activities adhocCalls agreementAcceptances appRoleAssignments authentication calendar "
    "calendarGroups calendarView calendars chats cloudClipboard cloudPCs contactFolders contacts "
    "createdObjects dataSecurityAndGovernance deviceManagementTroubleshootingEvents directReports "
    "drive drives employeeExperience events extensions followedSites inferenceClassification "
    "insights joinedTeams licenseDetails mailFolders managedAppRegistrations managedDevices "
    "manager memberOf messages notes oauth2PermissionGrants onPremisesSyncBehavior onenote "
    "onlineMeetings outlook ownedDevices ownedObjects people permissionGrants photo photos planner "
    "presence registeredDevices scopedRoleMemberOf settings solutions sponsorOf sponsors teamwork "
    "todo transitiveMemberOf".split()
)


def _user_expand(opts) -> None:
    """The directory binds ``$expand`` to a user's navigation properties; it expands nothing here,
    since a corpus states none of them."""
    if raw := _value(opts, "expand"):
        navs = {ascii_lower(n) for n in _USER_NAVIGATIONS}
        for name in (n.strip() for n in raw.split(",") if n.strip()):
            if ascii_lower(name) not in navs:
                raise msgraph.unknown_property(name, "user")


def _consistency(request: Request) -> bool:
    """Whether a directory request asks for an advanced query. A ``ConsistencyLevel`` the directory
    cannot read — sent twice, or naming anything but ``eventual`` in any case — is refused; a
    blank one asks for nothing."""
    values = request.headers.getlist("consistencylevel")
    if not values:
        return False
    value = ascii_lower(strip_blanks(values[0]))
    if len(values) > 1 or value not in ("", "eventual"):
        raise msgraph.headers_invalid()
    return value == "eventual"


def _format(opts) -> None:
    """The directory answers ``$format`` in JSON only."""
    raw = _value(opts, "format")
    if raw is not None and ascii_lower(strip_blanks(raw)) not in ("json", "application/json"):
        raise msgraph.unsupported_format(strip_blanks(raw))


def _single_person(opts) -> None:
    """``/me`` and ``/users/{id}`` refuse a filter, a search and a page token, and ignore
    ``$top``, ``$orderby`` and ``$count``."""
    if "filter" in opts or "search" in opts:
        raise msgraph.unsupported_query("The specified filter against a single entity is invalid.")
    if "skiptoken" in opts:
        raise msgraph.request_bad_request("Specified $skiptoken is invalid.")
    _user_expand(opts)


@router.get("/me", response_model=GraphResponse, openapi_extra={"parameters": [_P_SELECT]})
async def me(request: Request):
    conn = auth.conn(request)
    caller = _caller(request)
    opts = _options(request, directory=True)
    _consistency(request)
    _format(opts)
    _single_person(opts)
    select, echoed = _select(opts, _USER_PROPERTIES, "user", strict=False)
    return {
        "@odata.context": _context("users", echoed) + "/$entity",
        **_project(_me(conn, caller), select),
    }


def _joined_teams(opts) -> dict:
    """The teams the caller is in — the one this corpus models, for every caller. ``$select``
    projects it (keeping a name the type does not declare, which leaves an empty object),
    ``$filter`` is ignored, and every other option is refused."""
    q = _read_query({k: v for k, v in opts.items() if k != "filter"}, _JOINED_TEAMS)
    return _collection("teams", [_project(_team(), q.select)], select=q.echoed)


@router.get(
    "/me/joinedTeams", response_model=GraphResponse, openapi_extra={"parameters": [_P_SELECT]}
)
async def me_joined_teams(request: Request):
    """Not ACL-filtered: a team is not a document. Every principal in the org is in the team; which
    of its CHANNELS they may see is what the ACL decides, and that is answered by
    ``/teams/{id}/channels``."""
    _caller(request)
    return _joined_teams(_options(request))


@router.get("/users", response_model=GraphResponse, openapi_extra={"parameters": _P_USERS})
async def users_list(request: Request):
    """The directory: every registered user principal, in userPrincipalName order.

    Message senders who are not principals are NOT here — a corpus's transcript speakers can
    outnumber its directory (the same limitation Slack's ``users.list`` states). Such a sender
    still resolves through ``/users/{id}``.

    ``ne``, ``not``, ``endswith``, a lambda, ``$search`` and a count are advanced queries,
    answered only with ``ConsistencyLevel: eventual`` — ``ne`` with ``$count=true`` beside it as
    well. Outside one, a filter joins at most 15 children with ``or`` (an ``in`` list's values among
    them) and 25 with ``and``. Live, an advanced query's results come back in an order of the
    index's own, which is not modelled: they are in userPrincipalName order here too.

    Measured order: ``$count``'s value, ``$top``, the ``ConsistencyLevel`` header, then ``$search``
    and ``$orderby`` as written, then the filter.
    """
    conn = auth.conn(request)
    _caller(request)
    opts = _options(request, directory=True)
    raw_count = _value(opts, "count")
    if raw_count is not None and strip_blanks(raw_count) not in ("true", "false"):
        raise msgraph.invalid_count(raw_count)
    top = _USERS_DEFAULT
    if (raw_top := _value(opts, "top")) is not None:
        top = _integer(raw_top)
        if top > _INT32:
            raise msgraph.request_bad_request("Invalid value for '$top'.")
        if not 1 <= top <= _USERS_MAX:
            raise msgraph.unsupported_query(
                f"Invalid page size specified: '{top}'. Must be between 1 and 999 inclusive."
            )
    eventual = _consistency(request)
    _format(opts)
    if "skip" in opts:
        raise msgraph.request_bad_request("'$skip' is not supported by the service.")
    _user_expand(opts)
    offset = _offset(
        opts,
        lambda: msgraph.unsupported_query("Unable to execute query; invalid page token specified."),
    )
    counted = eventual and strip_blanks(raw_count or "") == "true"
    raw_search = _value(opts, "search")
    matches = _search(raw_search) if raw_search is not None else None
    raw_order = _value(opts, "orderby")
    terms: list[tuple[str, bool]] = []
    if raw_order is not None and strip_blanks(raw_order):
        try:
            terms = parse_orderby(raw_order)
        except FilterError as e:
            raise msgraph.bad_request(e.message) from None
    # The directory reads `'a' le displayName` as `displayName le 'a'`, measured.
    keep = _filter(
        opts,
        _USER_TYPES,
        refusal=_user_filter_refusal,
        fold_case=True,
        open_type=True,
        literal_first=True,
    )
    if keep is not None:
        _directory_refusal(keep.root, eventual=eventual, counted=counted)
    if keep is not None and not eventual:
        for op, limit in (("or", 15), ("and", 25)):
            if (n := keep.clauses(op)) > limit:
                raise msgraph.unsupported_query(
                    "Too many child clauses specified in search filter expression containing "
                    f"'{op.upper()}' operators: {n}. Max allowed: {limit}."
                )
    users = _kept(keep, [_user(conn, e) for e in store.all_user_emails(conn)])
    if matches is not None:
        if not eventual:
            raise msgraph.unsupported_query(
                "Request with $search query parameter only works through MSGraph with a special "
                "request header: 'ConsistencyLevel: eventual'"
            )
        users = [u for u in users if matches(u)]
    if terms:
        users = _order_users(users, terms, filtered=keep is not None and not eventual)
    select, echoed = _select(opts, _USER_PROPERTIES, "user", strict=False)
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


def _order_users(users: list[dict], terms: list[tuple[str, bool]], *, filtered: bool) -> list[dict]:
    """``$orderby`` over the directory, each property matched without regard to ASCII case. A sort
    beside a filter is refused, unless the query is an advanced one."""
    if filtered:
        raise msgraph.unsupported_query("Sorting not supported for current query.")
    resolved = []
    for name, descending in terms:
        if name in ORDERBY_NODES:
            raise msgraph.request_bad_request(
                f"Given expression for $orderby Microsoft.OData.UriParser.{name} is not supported"
            )
        prop = _USER_SORTS.get(ascii_lower(name))
        if prop is None:
            raise msgraph.unsupported_query(f"Unsupported sort property '{name}' for 'User'.")
        resolved.append((prop, descending))
    for prop, descending in reversed(resolved):
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
    _consistency(request)
    _format(opts)
    _single_person(opts)
    select, echoed = _select(opts, _USER_PROPERTIES, "user", strict=False)
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
    is in the one team. The id is an object id or an address. A caller who is no administrator lists
    only its own, and asking for anyone else's — whether or not they exist, unless an address names
    nobody — is a 403, as it is live for a member's token with ``Team.ReadBasic.All``. The admin
    token, which a tenant administrator's stands for, lists anyone's, and an id the directory does
    not have is refused in the words of the lookup that failed."""
    conn = auth.conn(request)
    caller = _caller(request)
    body = _joined_teams(_options(request))
    own = synth.msteams_user_id(caller.email or "")
    if "@" in user_id:
        email = _resolve_user(request, conn, user_id)
        if email is None:
            raise msgraph.no_such_person(user_id)
        if not caller.is_admin and synth.msteams_user_id(email) != own:
            raise msgraph.not_the_caller()
    elif not _GUID.fullmatch(user_id):
        raise msgraph.bad_request("userId must be a GUID.")
    elif not caller.is_admin and user_id.lower() != own:
        raise msgraph.not_the_caller()
    elif _resolve_user(request, conn, user_id) is None:
        raise msgraph.no_such_member_of(user_id.lower())
    return body


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


# A new team's settings, as live sends them for one nobody has changed.
_TEAM_SETTINGS = {
    "discoverySettings": {"showInTeamsSearchAndSuggestions": True},
    "tagSettings": None,
    "memberSettings": {
        "allowCreateUpdateChannels": True,
        "allowCreatePrivateChannels": True,
        "allowDeleteChannels": True,
        "allowAddRemoveApps": True,
        "allowCreateUpdateRemoveTabs": True,
        "allowCreateUpdateRemoveConnectors": True,
    },
    "guestSettings": {"allowCreateUpdateChannels": False, "allowDeleteChannels": False},
    "messagingSettings": {
        "allowUserEditMessages": True,
        "allowUserDeleteMessages": True,
        "allowOwnerDeleteMessages": True,
        "allowTeamMentions": True,
        "allowChannelMentions": True,
    },
    "funSettings": {
        "allowGiphy": True,
        "giphyContentRating": "moderate",
        "allowStickersAndMemes": True,
        "allowCustomMemes": True,
    },
}


def _team_entity(request: Request, conn) -> dict:
    """The one ``team``, read by id: what ``joinedTeams`` leaves null, filled. Its first channel
    is the one it was created with — General where the corpus has one — and its creation is that
    channel's. Nobody owns it, as nobody owns a channel here."""
    org, _ = _org()
    names = _channel_names(conn)
    created = {n: _channel_created(request, conn, n) for n in names}
    first = "General" if "General" in created else min(created, key=created.get, default=None)
    internal = synth.msteams_channel_id(first) if first is not None else None
    web = None
    if internal is not None:
        escaped = re.sub(r"%[0-9A-F]{2}", lambda m: m.group().lower(), quote(internal, safe=""))
        web = (
            f"https://teams.microsoft.com/l/team/{escaped}/conversations?groupId={_team_id()}"
            f"&tenantId={_tenant_id()}"
        )
    return {
        "id": _team_id(),
        "createdDateTime": synth.rfc3339_millis(min(created.values(), default=synth.BASE_EPOCH)),
        "displayName": org,
        "description": f"{org} on Microsoft Teams",
        "internalId": internal,
        "classification": None,
        "specialization": "none",
        "visibility": "public",
        "webUrl": web,
        "isArchived": False,
        "isMembershipLimitedToOwners": False,
        **_TEAM_SETTINGS,
        "summary": {
            "ownersCount": 0,
            "membersCount": len(store.all_user_emails(conn)),
            "guestsCount": 0,
        },
    }


@router.get(
    "/teams/{team_id}", response_model=GraphResponse, openapi_extra={"parameters": [_P_SELECT]}
)
async def teams_get(request: Request, team_id: str):
    """The team. Every option but ``$select`` and ``$expand`` is refused, an ``$expand`` that binds
    is refused too, and the id is checked only after."""
    conn = auth.conn(request)
    _caller(request)
    q = _read_query(_options(request), _TEAM_GET)
    _check_team_id(team_id)
    if team_id.lower() != _team_id():
        raise msgraph.no_such_team(team_id)
    return {
        "@odata.context": _context("teams", q.echoed) + "/$entity",
        **_project(_team_entity(request, conn), q.select),
    }


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
    opts = _options(request)
    q = _read_query(opts, _CHANNEL_LIST)
    _check_team_id(team_id)
    for option, name in (("top", "Top"), ("skiptoken", "SkipToken")):
        if option in opts:
            raise msgraph.bad_request(
                f"The query specified in the URI is not valid. Query option '{name}' is not "
                "allowed."
            )
    if team_id.lower() != _team_id():
        raise msgraph.team_not_found(team_id)
    q.run()
    ids = auth.visible_ids(request, caller)
    visible = _channel_visibility(request, conn, ids)
    channels = [
        _channel(request, conn, n, single=False) for n in _channel_names(conn) if visible(n)
    ]
    channels = _kept(q.keep, channels)
    page = [_project(c, q.select) for c in channels]
    return _collection(f"teams('{quote(_team_id(), safe='')}')/channels", page, select=q.echoed)


@router.get(
    "/teams/{team_id}/channels/{channel_id}",
    response_model=GraphResponse,
    openapi_extra={"parameters": [_P_SELECT]},
)
async def channels_get(request: Request, team_id: str, channel_id: str):
    conn = auth.conn(request)
    caller = _caller(request)
    q = _read_query(_options(request), _CHANNEL_GET)
    _check_team_id(team_id)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids, "channel", caller)
    return {
        "@odata.context": _context(f"teams('{quote(_team_id(), safe='')}')/channels", q.echoed)
        + "/$entity",
        **_project(_channel(request, conn, name, single=True), q.select),
    }


@router.get(
    "/teams/{team_id}/channels/{channel_id}/messages",
    response_model=GraphResponse,
    openapi_extra={"parameters": _P_MESSAGES},
)
async def channel_messages(request: Request, team_id: str, channel_id: str):
    """The channel's root messages — "the list of messages (without the replies)".

    Ordered by the reply chain's newest activity, measured: a new reply to the oldest root moves it
    to the top of the listing, though the root's own ``lastModifiedDateTime`` does not change.
    ``$skip`` skips that many roots; a ``$filter`` that binds is refused all the same, and an
    empty one is no filter.
    """
    conn = auth.conn(request)
    caller = _caller(request)
    opts = _options(request)
    q = _read_query(opts, _MESSAGE_LIST)
    _check_team_id(team_id)
    if q.keep is not None:
        raise msgraph.bad_request("Parameter 'Filter' not supported for this request.")
    top = _top(opts, _MESSAGES_DEFAULT, _MESSAGES_MAX)
    if top == 0:
        raise msgraph.bad_request("PageSize needs to be more than 0. (Parameter 'pageSize')")
    offset = min(_offset(opts, lambda: msgraph.bad_request("UnknownError")) + q.skip, _INT64)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids, "messages", caller)
    expand = "replies" in q.expand
    total = store.count_msteams_top_level(conn, name, ids)
    rows = store.list_msteams_top_level(conn, name, ids, limit=top, offset=offset)
    value = []
    for r in rows:
        if not expand:
            value.append(_project(_message(request, conn, r), q.select))
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
        value.append(_project(message, q.select))
    return _collection(
        f"{_channel_fragment(team_id, channel_id)}/messages",
        value,
        select=q.echoed,
        link=_next_link(request, offset, len(value), total),
    )


def _single_message_query(opts, *, refer: bool) -> _Query:
    """A single message's query. ``expand=replies`` is refused, pointing at the replies route
    (``refer``) on a root and not on a reply, as live words them."""
    q = _read_query(opts, _MESSAGE_GET)
    if "replies" in q.expand:
        raise msgraph.bad_request(
            "Expand query parameter 'replies' is not supported for this request."
            + (" Use the /replies endpoint to get the replies for a message." if refer else "")
        )
    return q


_UINT64 = 2**64 - 1


def _check_message_id(message_id: str) -> str:
    """The id a message or reply route names, as a number reads: one that is not an unsigned
    64-bit number is a 400, before the channel is checked at all. Which unknown ids live answers
    403, 404 or 400 depends on the number's range, which is not modelled."""
    digits = message_id.lstrip("0") or "0"
    if not message_id.isascii() or not message_id.isdigit() or len(digits) > 20:
        raise msgraph.bad_request("UnknownError")
    if int(digits) > _UINT64:
        raise msgraph.bad_request("UnknownError")
    return digits


def _message_row(conn, name: str, message_id: str, ids):
    """The row a message id names; one that names nothing is a 403 to a delegated caller, which is
    what every Backlot token is."""
    row = store.msteams_by_id(conn, name, message_id, ids)
    if row is None:
        raise msgraph.forbidden("UnknownError")
    return row


@router.get(
    "/teams/{team_id}/channels/{channel_id}/messages/{message_id}",
    response_model=GraphResponse,
    openapi_extra={"parameters": [_P_SELECT]},
)
async def channel_message(request: Request, team_id: str, channel_id: str, message_id: str):
    """One ROOT message. A reply's id is a 404 here; a reply is read at
    ``…/messages/{rootId}/replies/{replyId}``."""
    conn = auth.conn(request)
    caller = _caller(request)
    q = _single_message_query(_options(request), refer=True)
    _check_team_id(team_id)
    message_id = _check_message_id(message_id)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids, "messages", caller)
    row = _message_row(conn, name, message_id, ids)
    if row["reply_to_id"] is not None:
        raise msgraph.message_not_found()
    return {
        "@odata.context": _context(f"{_channel_fragment(team_id, channel_id)}/messages", q.echoed)
        + "/$entity",
        **_project(_message(request, conn, row), q.select),
    }


@router.get(
    "/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies",
    response_model=GraphResponse,
    openapi_extra={"parameters": _P_PAGED + [_P_SELECT]},
)
async def message_replies(request: Request, team_id: str, channel_id: str, message_id: str):
    """A message's replies, newest first. An id that names no message, or names a reply (Teams
    threads are one level deep), has none: live answers both with an empty collection. ``$skip``
    drops that many from the page ``$top`` reads, so it never reaches past it."""
    conn = auth.conn(request)
    caller = _caller(request)
    opts = _options(request)
    q = _read_query(opts, _REPLY_LIST)
    _check_team_id(team_id)
    top = _top(opts, _MESSAGES_DEFAULT, _MESSAGES_MAX)
    if top == 0:
        raise msgraph.bad_request("PageSize needs to be more than 0. (Parameter 'pageSize')")
    offset = _offset(opts, msgraph.backend_failure)
    root = _check_message_id(message_id)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids, "messages", caller)
    total = store.count_msteams_replies(conn, name, root, ids)
    rows = store.list_msteams_replies(conn, name, root, ids, limit=top, offset=offset)
    value = [_project(_message(request, conn, r), q.select) for r in rows][q.skip :]
    return _collection(
        f"{_channel_fragment(team_id, channel_id)}/messages('{message_id}')/replies",
        value,
        select=q.echoed,
        link=_next_link(request, offset, len(rows) if value else 0, total),
    )


@router.get(
    "/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies/{reply_id}",
    response_model=GraphResponse,
    openapi_extra={"parameters": [_P_SELECT]},
)
async def message_reply(
    request: Request, team_id: str, channel_id: str, message_id: str, reply_id: str
):
    """One reply, addressed under its root. One that is under another root is a 404."""
    conn = auth.conn(request)
    caller = _caller(request)
    q = _single_message_query(_options(request), refer=False)
    _check_team_id(team_id)
    root = _check_message_id(message_id)
    reply = _check_message_id(reply_id)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids, "messages", caller)
    row = _message_row(conn, name, reply, ids)
    if row["reply_to_id"] != root:
        raise msgraph.message_not_found()
    fragment = f"{_channel_fragment(team_id, channel_id)}/messages('{message_id}')/replies"
    return {
        "@odata.context": _context(fragment, q.echoed) + "/$entity",
        **_project(_message(request, conn, row), q.select),
    }


@router.get(
    "/teams/{team_id}/channels/{channel_id}/members",
    response_model=GraphResponse,
    openapi_extra={"parameters": [_P_SELECT, _P_FILTER]},
)
async def channel_members(request: Request, team_id: str, channel_id: str):
    """The channel's members, in one response, and a ``$skiptoken`` on a channel the caller may
    read answered with a backend failure. A private channel answers ``$top`` with the whole list
    live; a standard channel pages its members in pages shorter than ``$top``, which is not
    modelled."""
    conn = auth.conn(request)
    caller = _caller(request)
    opts = _options(request)
    q = _read_query(opts, _MEMBER_LIST)
    _check_team_id(team_id)
    ids = auth.visible_ids(request, caller)
    name = _resolve_channel(request, conn, team_id, channel_id, ids, "members", caller)
    if strip_blanks(_value(opts, "skiptoken") or ""):
        raise msgraph.backend_failure()
    q.run()
    members = [_member(conn, e, channel_id) for e in _member_emails(request, conn, name)]
    members = _kept(q.keep, members)
    return _collection(
        f"{_channel_fragment(team_id, channel_id)}/members",
        [_project(m, q.select) for m in members],
        select=q.echoed,
    )


# --- the path as Graph reads it ------------------------------------------

_PREFIX = "/msgraph/v1.0/"
_SEGMENTS = {
    ascii_lower(s): s
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
    """Rewrite a Graph path to the one spelling the routes above match, as Graph reads it: the
    version and a resource name in any ASCII case, an id as an OData key segment
    (``teams('{id}')``), a GUID in either case, and trailing slashes are each the same path live."""
    path = scope["path"]
    if ascii_lower(path[: len(_PREFIX)]) != _PREFIX:
        return
    rest = path[len(_PREFIX) :].rstrip("/")
    out: list[str] = []
    for segment in rest.split("/"):
        key = _KEY_SEGMENT.fullmatch(segment)
        parts = (
            [key.group(1), key.group(2)]
            if key and ascii_lower(key.group(1)) in _SEGMENTS
            else [segment]
        )
        for part in parts:
            if ascii_lower(part) in _SEGMENTS:
                part = _SEGMENTS[ascii_lower(part)]
            elif _GUID.fullmatch(part):
                part = part.lower()
            out.append(part)
    new = _PREFIX + "/".join(out)
    if new != path:
        scope["path"] = new
        scope["raw_path"] = quote(new, safe="/:@!$&'()*+,;=-._~").encode()


# --- everything else -----------------------------------------------------

_RESOURCES = ("me", "users", "teams")
_VERSION = re.compile(r"v\d+(?:\.\d+)*", re.IGNORECASE)


@unmatched_router.api_route(
    "/{rest:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"], include_in_schema=False
)
async def unmatched_path(request: Request, rest: str):
    """A path or method no route above serves. Graph reads the credential before the path: with
    none, or one that resolves to nobody, every such request is the credential's 401 (measured on
    an unknown first segment, an unknown path under a team, and ``POST``/``DELETE`` on ``/me``).
    Past that, a version other than v1.0 is a 404 naming it; an unknown first segment is a 400
    naming it; a write with no ``Content-Type`` is a 400, and a ``POST`` to ``/me`` a 405; under a
    person (``/me``, ``/users/{id}``) the unknown property is a 404 naming it, and anything below
    ``joinedTeams`` an API that is not supported; under a channel, a 400 naming it; and under a
    team, a 404. The beta version and the service document at ``/v1.0`` are not modelled."""
    _caller(request)
    first, _, after = rest.partition("/")
    if ascii_lower(first) == "v1.0":
        segments = after.split("/")
    elif _VERSION.fullmatch(first):
        raise msgraph.invalid_version(first)
    else:
        segments = rest.split("/")
    if segments[0] not in _RESOURCES:
        raise msgraph.unknown_segment(segments[0])
    if request.method in ("POST", "PUT", "PATCH"):
        content_type = strip_blanks(request.headers.get("content-type", ""))
        if not content_type:
            raise msgraph.write_without_content_type()
        if segments == ["me"] and request.method == "POST":
            if not ascii_lower(content_type).startswith("application/json"):
                raise msgraph.write_not_json()
            raise msgraph.post_not_allowed()
    person = {"me": 1, "users": 2}.get(segments[0])
    if person is not None and len(segments) > person:
        if segments[person] == "joinedTeams":
            raise msgraph.unsupported_path()
        raise msgraph.user_not_found(segments[person])
    if segments[0] == "teams" and len(segments) > 4 and segments[2] == "channels":
        raise msgraph.unknown_segment(segments[-1])
    raise msgraph.unsupported_path()
