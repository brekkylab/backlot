"""Microsoft Teams over Microsoft Graph: teams, channels, messages, members and the directory.

Every expected value below is one live Graph answered — ``backlot.routers.msteams`` records the
measurement.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone

import pytest

from backlot import store, synth
from backlot.errors.msgraph import WWW_AUTHENTICATE
from backlot.pagination import encode_cursor
from tests._helpers import complete, corpus_client, db_count, tok

V1 = "/msgraph/v1.0"


@pytest.fixture(scope="module")
def team_id(client, admin_h) -> str:
    return client.get(f"{V1}/me/joinedTeams", headers=admin_h).json()["value"][0]["id"]


def _channels(client, headers, team) -> dict[str, str]:
    """displayName -> channel id, for the channels this caller can see."""
    body = client.get(f"{V1}/teams/{team}/channels", headers=headers).json()
    return {c["displayName"]: c["id"] for c in body["value"]}


def _error(response, status: int) -> dict:
    assert response.status_code == status, response.text
    return response.json()["error"]


_NINES = "9" * 4301  # past the 4,300 digits Python's `int()` reads
_INCOMPATIBLE = (
    "Invalid filter clause: A binary operator with incompatible types was detected. Found operand "
    "types '{}' and '{}' for operator kind '{}'."
)
_NODES = (
    "The query specified in the URI is not valid. The node count limit of '100' has been "
    "exceeded. To increase the limit, set the 'MaxNodeCount' property on EnableQueryAttribute or "
    "ODataValidationSettings."
)
_DEPTH = "Invalid filter clause: Recursion depth exceeded allowed limit."
_NESTING = (
    "The query specified in the URI is not valid. The Any/All nesting limit of '1' has been "
    "exceeded. 'MaxAnyAllExpressionDepth' can be configured on ODataQuerySettings or "
    "EnableQueryAttribute."
)
_RAW_VALUE = "The argument 'rawValue' is null or empty. (Parameter 'rawValue')"
_INVALID = (
    "Invalid value '{}' for ${} query option found. The ${} query option requires a non-negative "
    "integer value."
)


def _invalid(option: str, value: str) -> str:
    return _INVALID.format(value, option, option)


def _limit(option: str, limit: int, value: str) -> str:
    return (
        f"The query specified in the URI is not valid. The limit of '{limit}' for {option} query "
        f"has been exceeded. The value from the incoming request is '{value}'."
    )


# --- auth ------------------------------------------------------------------------

_UNREADABLE = "Protocol 'Bearer' failed to validate because The token could not be read."
# A JWT whose header and payload parse, signed by nobody: Graph reads it and finds no application.
_FOREIGN_JWT = "eyJ0eXAiOiAiSldUIiwgImFsZyI6ICJSUzI1NiJ9.eyJhdWQiOiAieCJ9.c2ln"


@pytest.mark.parametrize(
    "header,message",
    [
        (None, "Access token is empty."),
        ("Bearer", "UnableToParseTokens"),
        ("Bearer ", "UnableToParseTokens"),
        ("Bearer nope", _UNREADABLE),
        # The scheme is not checked: each of these reaches the token parser and fails there, so
        # none of them may be reported as a missing credential.
        ("bearer nope", _UNREADABLE),
        ("BEARER nope", _UNREADABLE),
        ("token nope", _UNREADABLE),
        ("Basic bm9wZTpub3Bl", _UNREADABLE),
        ("nope", _UNREADABLE),
        (f"Bearer {_FOREIGN_JWT}", "NoAppIdInToken"),
    ],
)
def test_every_way_a_credential_fails_answers_the_message_live_graph_answers(
    client, header, message
):
    r = client.get(f"{V1}/me", headers={"Authorization": header} if header else {})
    err = _error(r, 401)
    assert err["code"] == "InvalidAuthenticationToken"
    assert err["message"] == message
    assert r.headers["www-authenticate"] == WWW_AUTHENTICATE
    # `innerError` is on every error: `date` with no zone and no fraction, and a fresh request id.
    inner = err["innerError"]
    assert list(inner) == ["date", "request-id", "client-request-id"]
    datetime.strptime(inner["date"], "%Y-%m-%dT%H:%M:%S")
    uuid.UUID(inner["request-id"])
    again = client.get(f"{V1}/me", headers={"Authorization": header} if header else {})
    assert again.json()["error"]["innerError"]["request-id"] != inner["request-id"]


_BEARER_SPELLINGS = [
    ("Bearer {T}", True),
    ("bearer {T}", True),
    ("BEARER {T}", True),
    ("{T}", True),
    ("Bearer  {T}", True),
    ("Bearer\t{T}", True),
    # A token that resolves is still read only after `Bearer` or with no scheme at all.
    ("token {T}", False),
    ("Basic {T}", False),
    ("Bearer {T} extra", False),
]


def test_a_token_that_resolves_is_read_only_where_graph_reads_one(client, tokens_yaml):
    token = tok(tokens_yaml, "bob@acme.com")
    answered = {}
    for header, _ in _BEARER_SPELLINGS:
        r = client.get(f"{V1}/me", headers={"Authorization": header.replace("{T}", token)})
        error = r.json().get("error") or {}
        answered[header] = (r.status_code, error.get("message"))
    assert answered == {
        header: (200, None) if served else (401, _UNREADABLE)
        for header, served in _BEARER_SPELLINGS
    }


def test_two_authorization_headers_are_refused_before_either_is_read(client, tokens_yaml):
    token = tok(tokens_yaml, "bob@acme.com")
    for pair in ((f"Bearer {token}", "Bearer nope"), ("Bearer nope", f"Bearer {token}")):
        r = client.get(f"{V1}/me", headers=[("Authorization", v) for v in pair])
        assert r.status_code == 400
        assert r.headers["content-type"] == "text/html; charset=us-ascii"
        assert "<h2>Bad Request - Invalid Header</h2>" in r.text


def test_every_answer_carries_its_request_id_and_a_200_is_odata(client, admin_h):
    ok = client.get(f"{V1}/me", headers=admin_h)
    assert ok.headers["content-type"] == (
        "application/json;odata.metadata=minimal;odata.streaming=true;IEEE754Compatible=false;"
        "charset=utf-8"
    )
    assert ok.headers["odata-version"] == "4.0"
    refused = client.get(f"{V1}/users", headers=admin_h, params={"$top": "0"})
    assert refused.headers["content-type"] == "application/json"
    assert "odata-version" not in refused.headers
    for r in (ok, refused):
        assert r.headers["request-id"] == r.headers["client-request-id"]
        uuid.UUID(r.headers["request-id"])
    assert refused.json()["error"]["innerError"]["request-id"] == refused.headers["request-id"]
    # A GUID the caller sends as `client-request-id` is echoed, beside a `request-id` of Graph's
    # own; anything else is ignored.
    mine = "11111111-2222-3333-4444-555555555555"
    echoed = client.get(f"{V1}/me?$filter=x", headers={**admin_h, "client-request-id": mine})
    assert echoed.headers["client-request-id"] == mine != echoed.headers["request-id"]
    assert echoed.json()["error"]["innerError"]["client-request-id"] == mine
    ignored = client.get(f"{V1}/me", headers={**admin_h, "client-request-id": "not-a-guid"})
    assert ignored.headers["client-request-id"] == ignored.headers["request-id"]


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/nonexistentsegment"),
        ("GET", "/teams/x/nonexistent"),
        ("POST", "/me"),
        ("DELETE", "/me"),
    ],
)
def test_the_credential_is_read_before_the_path_or_the_method(client, method, path):
    for headers in ({}, {"Authorization": "Bearer nope"}):
        r = client.request(method, f"{V1}{path}", headers=headers)
        assert _error(r, 401)["code"] == "InvalidAuthenticationToken"


@pytest.mark.parametrize(
    "method,path,headers,status,code,message",
    [
        (
            "POST",
            "/me",
            {},
            400,
            "BadRequest",
            "Write requests (excluding DELETE) must contain the Content-Type header declaration.",
        ),
        (
            "PATCH",
            "/me",
            {"Content-Type": ""},
            400,
            "BadRequest",
            "Write requests (excluding DELETE) must contain the Content-Type header declaration.",
        ),
        (
            "POST",
            "/me",
            {"Content-Type": "text/plain"},
            400,
            "BadRequest",
            "Entity only allows writes with a JSON Content-Type header.",
        ),
        (
            "POST",
            "/me",
            {"Content-Type": "application/json"},
            405,
            "Request_BadRequest",
            "Specified HTTP method is not allowed for the request target.",
        ),
        # The first segment is read before the content type.
        ("POST", "/bogus", {}, 400, "BadRequest", "Resource not found for the segment 'bogus'."),
    ],
)
def test_a_write_is_refused_as_graph_refuses_one(
    client, admin_h, method, path, headers, status, code, message
):
    r = client.request(method, f"{V1}{path}", headers={**admin_h, **headers}, content=b"")
    err = _error(r, status)
    assert (err["code"], err["message"]) == (code, message)


@pytest.mark.parametrize("method", ["OPTIONS", "HEAD"])
@pytest.mark.parametrize("path", ["/me", "/teams/{team}/channels", "/bogus"])
def test_a_method_graph_answers_on_no_route_is_an_empty_405(client, admin_h, team_id, method, path):
    """With a credential or none."""
    for headers in (admin_h, {}):
        r = client.request(method, f"{V1}{path.format(team=team_id)}", headers=headers)
        assert r.status_code == 405
        assert r.content == b"" and r.headers["content-length"] == "0"
        uuid.UUID(r.headers["request-id"])


_RESOURCE_MISSING = (
    "Resource 'bogus' does not exist or one of its queried reference-property objects are not "
    "present."
)


@pytest.mark.parametrize(
    "path,status,code,message",
    [
        (
            "/nonexistentsegment",
            400,
            "BadRequest",
            "Resource not found for the segment 'nonexistentsegment'.",
        ),
        (
            "/teams/{team}/nonexistent",
            404,
            "NotFound",
            "Requested API is not supported. Please check the path.",
        ),
        ("/me/bogus", 404, "Request_ResourceNotFound", _RESOURCE_MISSING),
        ("/users/{user}/bogus", 404, "Request_ResourceNotFound", _RESOURCE_MISSING),
        (
            "/teams/{team}/channels/{channel}/bogus",
            400,
            "BadRequest",
            "Resource not found for the segment 'bogus'.",
        ),
        ("/teams/abc/channels", 400, "BadRequest", "teamId needs to be a valid GUID."),
        ("/teams/abc/channels/{channel}", 400, "BadRequest", "teamId needs to be a valid GUID."),
        ("/teams/{team}/channels/abc", 400, "BadRequest", "channelId is not valid."),
        ("/teams/{team}/channels/abc/messages", 400, "BadRequest", "UnknownError"),
        ("/teams/{team}/channels/{channel}/messages/abc", 400, "BadRequest", "UnknownError"),
        (
            "/me/joinedTeams/{team}",
            404,
            "NotFound",
            "Requested API is not supported. Please check the path.",
        ),
        ("/msgraph/v2.0/me", 404, "ResourceNotFound", "Invalid version: v2.0"),
        ("/teams/abc", 400, "BadRequest", "teamId needs to be a valid GUID."),
        (
            "/teams/00000000-0000-0000-0000-000000000000",
            404,
            "NotFound",
            "No team found with Group Id 00000000-0000-0000-0000-000000000000",
        ),
    ],
)
def test_a_path_nothing_serves_is_refused_once_the_credential_holds(
    client, admin_h, team_id, path, status, code, message
):
    url = path.format(
        team=team_id,
        channel=_channels(client, admin_h, team_id)["Platform"],
        user=synth.msteams_user_id("ava@acme.com"),
    )
    err = _error(
        client.get(url if url.startswith("/msgraph") else f"{V1}{url}", headers=admin_h), status
    )
    assert (err["code"], err["message"]) == (code, message)
    if code == "NotFound" and message.startswith("No team"):
        assert err["innerError"]["code"] == "ItemNotFound"
        assert err["innerError"]["message"] == message
        assert list(err["innerError"])[:3] == ["code", "message", "details"]


_PATH_SPELLINGS = {
    "/ME": "/me",
    "/me/": "/me",
    "/me/joinedteams": "/me/joinedTeams",
    "/me/joinedTeams/": "/me/joinedTeams",
    "/Teams/{team}/Channels": "/teams/{team}/channels",
    "/teams('{team}')/channels": "/teams/{team}/channels",
    "/teams/{TEAM}/channels": "/teams/{team}/channels",
    "/users('{user}')": "/users/{user}",
    "/users/{USER}": "/users/{user}",
    "/users/{user}/": "/users/{user}",
    "/teams/{team}/channels/{channel}/": "/teams/{team}/channels/{channel}",
    "/teams('{team}')/channels('{channel}')/messages": "/teams/{team}/channels/{channel}/messages",
    "/teams/{team}/channels/{channel}/MESSAGES/": "/teams/{team}/channels/{channel}/messages",
    "/teams/{team}/channels/{channel}/members/": "/teams/{team}/channels/{channel}/members",
    "/me//": "/me",
    "/msgraph/V1.0/me": "/me",
    "/teams('{team}')": "/teams/{team}",
}


def test_a_path_is_read_in_every_spelling_graph_reads(client, admin_h, team_id):
    """The version and a resource name in any case, an id as an OData key segment or a GUID
    upper-cased, and trailing slashes are each the same path live."""
    user = synth.msteams_user_id("ava@acme.com")
    ids = dict(
        team=team_id,
        TEAM=team_id.upper(),
        channel=_channels(client, admin_h, team_id)["Platform"],
        user=user,
        USER=user.upper(),
    )
    strip = {"@odata.context", "@odata.nextLink"}

    def answer(path):
        r = client.get(path if path.startswith("/msgraph") else f"{V1}{path}", headers=admin_h)
        return r.status_code, {k: v for k, v in r.json().items() if k not in strip}

    answered = {spelled: answer(spelled.format(**ids)) for spelled in _PATH_SPELLINGS}
    canonical = {spelled: answer(path.format(**ids)) for spelled, path in _PATH_SPELLINGS.items()}
    assert all(status == 200 for status, _ in canonical.values())
    assert answered == canonical


# --- identity, the directory and the team ----------------------------------------


def test_me_answers_the_calling_user(client, tokens_yaml):
    me = client.get(
        f"{V1}/me", headers={"Authorization": f"Bearer {tok(tokens_yaml, 'bob@acme.com')}"}
    ).json()
    assert me["userPrincipalName"] == "bob@acme.com"
    assert me["id"] == synth.msteams_user_id("bob@acme.com")
    # The eleven properties Graph returns when no $select narrows the request.
    assert set(me) - {"@odata.context"} == {
        "businessPhones",
        "displayName",
        "givenName",
        "jobTitle",
        "mail",
        "mobilePhone",
        "officeLocation",
        "preferredLanguage",
        "surname",
        "userPrincipalName",
        "id",
    }


def test_select_keeps_only_what_was_named_and_the_directory_accepts_any_name(client, admin_h):
    """`id` is not kept unless asked for. The directory answers a name it does not have with an
    object that lacks it, where the Teams types refuse one (see the channel and message tests)."""
    body = client.get(f"{V1}/users", headers=admin_h, params={"$select": "displayName"}).json()
    assert body["value"], "the sample corpus has users"
    assert all(set(u) == {"displayName"} for u in body["value"])
    assert body["@odata.context"].endswith("#users(displayName)")
    bogus = client.get(f"{V1}/users", headers=admin_h, params={"$select": "bogus"}).json()
    assert all(u == {} for u in bogus["value"])
    one = client.get(f"{V1}/users/ava@acme.com", headers=admin_h, params={"$select": "bogus"})
    assert one.json() == {
        "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users(bogus)/$entity"
    }


def test_the_directory_counts_only_an_advanced_query(client, admin_h):
    plain = client.get(f"{V1}/users", headers=admin_h).json()
    assert "@odata.count" not in plain
    asked = client.get(f"{V1}/users", headers=admin_h, params={"$count": "true"}).json()
    assert "@odata.count" not in asked
    advanced = client.get(
        f"{V1}/users",
        headers={**admin_h, "ConsistencyLevel": "eventual"},
        params={"$count": "true"},
    ).json()
    assert advanced["@odata.count"] == len(plain["value"])


def test_the_directory_filters_without_regard_to_case(client, admin_h):
    ava = client.get(f"{V1}/users/ava@acme.com", headers=admin_h).json()
    for expr in (
        f"displayName eq '{ava['displayName'].lower()}'",
        f"startswith(displayName,'{ava['displayName'][:2].lower()}')",
    ):
        got = client.get(f"{V1}/users", headers=admin_h, params={"$filter": expr}).json()
        assert ava["id"] in {u["id"] for u in got["value"]}, expr


_INVALID_TOP = (
    "Invalid value '{}' for $top query option found. The $top query option requires a "
    "non-negative integer value."
)
_PAGE_SIZE = "Invalid page size specified: '{}'. Must be between 1 and 999 inclusive."
_ADVANCED = (
    "is not supported because the 'ConsistencyLevel:eventual' header is missing. Refer to "
    "https://aka.ms/graph-docs/advanced-queries for more information"
)
_EVENTUAL = {"ConsistencyLevel": "eventual"}
_DIRECTORY_EXPRESSION = "Invalid expression caused by operator '{}'."


@pytest.mark.parametrize(
    "query,headers,code,message",
    [
        ("$top=1000", {}, "Request_UnsupportedQuery", _PAGE_SIZE.format(1000)),
        ("$top=0", {}, "Request_UnsupportedQuery", _PAGE_SIZE.format(0)),
        ("$top=-0", {}, "Request_UnsupportedQuery", _PAGE_SIZE.format(0)),
        ("$top=abc", {}, "BadRequest", _INVALID_TOP.format("abc")),
        ("$top=-1", {}, "BadRequest", _INVALID_TOP.format("-1")),
        ("$top=1.0", {}, "BadRequest", _INVALID_TOP.format("1.0")),
        ("$top=%C2%B2", {}, "BadRequest", _INVALID_TOP.format("²")),
        ("$top=%EF%BC%92", {}, "BadRequest", _INVALID_TOP.format("２")),
        (
            "$skiptoken=garbage",
            {},
            "Request_UnsupportedQuery",
            "Unable to execute query; invalid page token specified.",
        ),
        (
            "$filter=contains(displayName,'K')",
            {},
            "Request_UnsupportedQuery",
            "Filter operator 'Contains' is not supported.",
        ),
        (
            "$filter=foo eq 'x'",
            {},
            "Request_UnsupportedQuery",
            "Property 'foo' does not exist as a declared property or extension property.",
        ),
        (
            "$filter=displayName eq",
            {},
            "BadRequest",
            "Invalid filter clause: Expression expected at position 14 in 'displayName eq'.",
        ),
        # The advanced operators want `ConsistencyLevel: eventual`, and `ne` `$count=true` too.
        (
            "$filter=displayName ne 'x'",
            {},
            "Request_UnsupportedQuery",
            "Filter operator 'NotEqualsMatch' is not supported.",
        ),
        (
            "$filter=displayName ne 'x'",
            _EVENTUAL,
            "Request_UnsupportedQuery",
            "Filter operator 'NotEqualsMatch' is not supported.",
        ),
        (
            "$filter=endswith(mail,'@acme.com')",
            {},
            "Request_UnsupportedQuery",
            f"Operator 'endsWith' {_ADVANCED}",
        ),
        (
            "$filter=not startswith(displayName,'A')",
            {},
            "Request_UnsupportedQuery",
            f"Operator 'not' {_ADVANCED}",
        ),
        (
            '$search="displayName:a"',
            {},
            "Request_UnsupportedQuery",
            "Request with $search query parameter only works through MSGraph with a special request "
            "header: 'ConsistencyLevel: eventual'",
        ),
        (
            "$search=displayName:Ava",
            _EVENTUAL,
            "BadRequest",
            "Syntax error: character ':' is not valid at position 11 in 'displayName:Ava'.",
        ),
        (
            '$search="foo:a"',
            _EVENTUAL,
            "Request_UnsupportedQuery",
            "Property 'foo' does not exist as a declared property or extension property.",
        ),
        (
            "$orderby=mail",
            {},
            "Request_UnsupportedQuery",
            "Unsupported sort property 'mail' for 'User'.",
        ),
        (
            "$orderby=displayName&$filter=startswith(displayName,'A')",
            {},
            "Request_UnsupportedQuery",
            "Sorting not supported for current query.",
        ),
        # Option names: the directory wants the `$`, and no option twice.
        ("top=1", {}, "Request_BadRequest", "Unrecognized query argument specified: 'top'."),
        (
            "select=displayName",
            {},
            "Request_BadRequest",
            "Unrecognized query argument specified: 'select'.",
        ),
        (
            "$top=1&$top=2",
            {},
            "BadRequest",
            "Query option '$top' was specified more than once, "
            "but it must be specified at most once.",
        ),
        ("$top=2147483648", {}, "Request_BadRequest", "Invalid value for '$top'."),
        ("$top=99999999999", {}, "Request_BadRequest", "Invalid value for '$top'."),
        ("$top=9223372036854775807", {}, "Request_BadRequest", "Invalid value for '$top'."),
        # Past Int64 a value is not one, and one past 1,024 characters is echoed cut.
        ("$top=9223372036854775808", {}, "BadRequest", _INVALID_TOP.format(9223372036854775808)),
        (f"$top={_NINES}", {}, "BadRequest", _INVALID_TOP.format("9" * 1021 + "...")),
        # .NET's blanks are read around a value, and the separators Python counts as space are not.
        ("$top=1%1C", {}, "BadRequest", _INVALID_TOP.format("1\x1c")),
        ("$top=%E2%80%8B1", {}, "BadRequest", _INVALID_TOP.format("\u200b1")),
        # `$count` is `true` or `false` exactly, read first.
        ("$count=TRUE", _EVENTUAL, "BadRequest", "'TRUE' is not a valid count option."),
        ("$count=yes", {}, "BadRequest", "'yes' is not a valid count option."),
        ("$count=", _EVENTUAL, "BadRequest", "'' is not a valid count option."),
        ("$count=TRUE&$top=abc", {}, "BadRequest", "'TRUE' is not a valid count option."),
        # A `ConsistencyLevel` that is not `eventual` is refused, after `$top`.
        (
            "",
            {"ConsistencyLevel": "bogus"},
            "Request_BadRequest",
            "One or more headers are invalid.",
        ),
        ("$top=abc", {"ConsistencyLevel": "bogus"}, "BadRequest", _INVALID_TOP.format("abc")),
        (
            "$filter=foo eq 'x'",
            {"ConsistencyLevel": "eventual,eventual"},
            "Request_BadRequest",
            "One or more headers are invalid.",
        ),
        (
            "$orderby=displayName,",
            {},
            "BadRequest",
            "Expression expected at position 12 in 'displayName,'.",
        ),
        (
            "$orderby=,displayName",
            {},
            "BadRequest",
            "Expression expected at position 0 in ',displayName'.",
        ),
        (
            "$orderby=displayName asc desc",
            {},
            "BadRequest",
            "Syntax error at position 20 in 'displayName asc desc'.",
        ),
        # `$orderby` and `$search` are read before the filter is bound.
        (
            "$orderby=displayName,&$filter=foo eq 'x'",
            {},
            "BadRequest",
            "Expression expected at position 12 in 'displayName,'.",
        ),
        (
            "$search=abc&$filter=foo eq 'x'",
            _EVENTUAL,
            "Request_UnsupportedQuery",
            "Clause 'abc' in $search is not of right format \"property:value\"",
        ),
        (
            '$search="abc"',
            {},
            "Request_UnsupportedQuery",
            "Clause 'abc' in $search is not of right format \"property:value\"",
        ),
        (
            '$search="displayName:"',
            _EVENTUAL,
            "Request_UnsupportedQuery",
            "Clause 'displayName:' in $search is of right format \"property:value\" but value is "
            "empty",
        ),
        (
            "$search=:abc",
            _EVENTUAL,
            "BadRequest",
            "Syntax error: character ':' is not valid at position 0 in ':abc'.",
        ),
        ("$search=", _EVENTUAL, "BadRequest", "Expression expected at position 0 in ''."),
        # U+001C–U+001F are no blanks, and no character a clause is made of.
        (
            "$search=a%1Cb",
            {},
            "BadRequest",
            "Syntax error: character '\x1c' is not valid at position 1 in 'a\x1cb'.",
        ),
        (
            "$search=%1Cabc",
            _EVENTUAL,
            "BadRequest",
            "Syntax error: character '\x1c' is not valid at position 0 in '\x1cabc'.",
        ),
        (
            '$search="displayName:a"%1F',
            _EVENTUAL,
            "BadRequest",
            "Syntax error: character '\x1f' is not valid at position 15 in '\"displayName:a\"\x1f'.",
        ),
        # A blank between clauses joins them, and each is then read.
        (
            "$search=a%C2%85b",
            _EVENTUAL,
            "Request_UnsupportedQuery",
            "Clause 'a' in $search is not of right format \"property:value\"",
        ),
        (
            "$filter=createdDateTime gt 0000-01-01",
            {},
            "BadRequest",
            "Invalid filter clause: Syntax error at position 26 in 'createdDateTime gt 0000-01-01'.",
        ),
        (
            "$filter=displayName eq " + "0" * 4301 + "1",
            {},
            "BadRequest",
            _INCOMPATIBLE.format("Edm.String", "Edm.Int32", "Equal"),
        ),
        (
            "$orderby=1",
            {},
            "Request_BadRequest",
            "Given expression for $orderby Microsoft.OData.UriParser.ConstantNode is not supported",
        ),
        (
            "$orderby=" + "0" * 4301 + "1",
            {},
            "Request_BadRequest",
            "Given expression for $orderby Microsoft.OData.UriParser.ConstantNode is not supported",
        ),
        ("$orderby=2020-13-45", {}, "BadRequest", "Syntax error at position 7 in '2020-13-45'."),
        # Outside an advanced query, `or` joins at most 15 children — an `in` list's values among
        # them — and `and` 25; a lambda wants the header too.
        (
            "$filter=" + " or ".join(["displayName eq 'x'"] * 16),
            {},
            "Request_UnsupportedQuery",
            "Too many child clauses specified in search filter expression containing 'OR' "
            "operators: 16. Max allowed: 15.",
        ),
        (
            "$filter=displayName in (" + ",".join(f"'{i}'" for i in range(16)) + ")",
            {},
            "Request_UnsupportedQuery",
            "Too many child clauses specified in search filter expression containing 'OR' "
            "operators: 16. Max allowed: 15.",
        ),
        (
            "$filter=" + " and ".join(["displayName eq 'x'"] * 26),
            {},
            "Request_UnsupportedQuery",
            "Too many child clauses specified in search filter expression containing 'AND' "
            "operators: 26. Max allowed: 25.",
        ),
        (
            "$filter=businessPhones/any(p:p eq '1')",
            {},
            "Request_UnsupportedQuery",
            "Unsupported or invalid query filter clause specified for property 'businessPhones' of "
            "resource 'User'.",
        ),
        (
            "$filter=displayName eq 5",
            {},
            "BadRequest",
            _INCOMPATIBLE.format("Edm.String", "Edm.Int32", "Equal"),
        ),
        (
            "$filter=accountEnabled eq 'x'",
            _EVENTUAL,
            "BadRequest",
            _INCOMPATIBLE.format("Edm.Boolean", "Edm.String", "Equal"),
        ),
        # The shape of every comparison first, in the order written: a property or a constant
        # against one, never two properties or two constants, and no bare property.
        (
            "$filter=length(displayName) div 0 eq 1",
            {},
            "Request_UnsupportedQuery",
            _DIRECTORY_EXPRESSION.format("EqualsMatch"),
        ),
        (
            "$filter=length(displayName) le 1",
            {},
            "Request_UnsupportedQuery",
            _DIRECTORY_EXPRESSION.format("LessOrEqual"),
        ),
        ("$filter=1 eq 1", _EVENTUAL, "Request_UnsupportedQuery", "Unsupported Query."),
        (
            "$filter=displayName eq displayName",
            {},
            "Request_UnsupportedQuery",
            "Unsupported Query.",
        ),
        ("$filter=accountEnabled", {}, "Request_UnsupportedQuery", "Unsupported Query."),
        (
            "$filter=length(displayName) in (1)",
            {},
            "Request_UnsupportedQuery",
            "Unsupported Query.",
        ),
        (
            "$filter=1 eq 1 and length(displayName) eq 1",
            {},
            "Request_UnsupportedQuery",
            "Unsupported Query.",
        ),
        (
            "$filter=length(displayName) eq 1 and 1 eq 1",
            {},
            "Request_UnsupportedQuery",
            _DIRECTORY_EXPRESSION.format("EqualsMatch"),
        ),
        (
            "$filter=foo eq 'x' and length(displayName) eq 1",
            {},
            "Request_UnsupportedQuery",
            _DIRECTORY_EXPRESSION.format("EqualsMatch"),
        ),
        (
            "$filter=businessPhones/any(p:p eq p)",
            _EVENTUAL,
            "Request_UnsupportedQuery",
            "Unsupported Query.",
        ),
        # Then, again in that order, a property it does not have, an operator it does not run
        # (worded per query), and a comparison with null outside an advanced query.
        (
            "$filter=displayName gt 'a'",
            {},
            "Request_UnsupportedQuery",
            "Filter operator 'Greater' is not supported. Use 'GreaterOrEqual' instead.",
        ),
        (
            "$filter=displayName lt 'z'",
            _EVENTUAL,
            "Request_UnsupportedQuery",
            "Operator: 'Less' is not supported.",
        ),
        (
            "$filter=contains(displayName,'a')",
            _EVENTUAL,
            "Request_UnsupportedQuery",
            "Operator: 'Contains' is not supported.",
        ),
        (
            "$filter=contains(displayName,'a') and foo eq 'x'",
            {},
            "Request_UnsupportedQuery",
            "Filter operator 'Contains' is not supported.",
        ),
        (
            "$filter=foo eq 'x' and contains(displayName,'a')",
            {},
            "Request_UnsupportedQuery",
            "Property 'foo' does not exist as a declared property or extension property.",
        ),
        # Within one comparison, a plain query reads the property first, an advanced one the
        # operator.
        (
            "$filter=foo gt 'a'",
            {},
            "Request_UnsupportedQuery",
            "Property 'foo' does not exist as a declared property or extension property.",
        ),
        (
            "$filter=foo gt 'a'",
            _EVENTUAL,
            "Request_UnsupportedQuery",
            "Operator: 'Greater' is not supported.",
        ),
        (
            "$filter=businessPhones/any(p:p gt '1')",
            _EVENTUAL,
            "Request_UnsupportedQuery",
            "Operator: 'Greater' is not supported.",
        ),
        (
            "$filter=displayName eq null",
            {},
            "Request_UnsupportedQuery",
            "Unsupported or invalid query filter clause specified for property 'displayName' of "
            "resource 'User'.",
        ),
        (
            "$filter=length(displayName) eq 'x'",
            {},
            "BadRequest",
            _INCOMPATIBLE.format("Edm.Int32", "Edm.String", "Equal"),
        ),
        (
            "$filter=businessPhones in ('1')",
            {},
            "BadRequest",
            "Invalid filter clause: The left operand for the IN operation is not a single value. "
            "IN operations require the left operand to be a single value and the right operand to "
            "be a collection value.",
        ),
        (
            "$filter=businessPhones eq '1'",
            {},
            "BadRequest",
            "Invalid filter clause: The operand for a binary operator 'Equal' is not a single "
            "value. Binary operators require both operands to be single values.",
        ),
        ("$skip=1", {}, "Request_BadRequest", "'$skip' is not supported by the service."),
        (
            "$expand=bogus",
            {},
            "BadRequest",
            "Parsing OData Select and Expand failed: Could not find a property named 'bogus' on "
            "type 'microsoft.graph.user'.",
        ),
    ],
)
def test_the_directory_refuses_a_query_it_cannot_run(
    client, admin_h, query, headers, code, message
):
    err = _error(client.get(f"{V1}/users?{query}", headers={**admin_h, **headers}), 400)
    assert (err["code"], err["message"]) == (code, message)


@pytest.mark.parametrize(
    "top",
    ["%2B1", "%201", "1%20", "%091", "1%09", "%C2%A01", "1%E3%80%80", "%E2%80%A81", "01", "$TOP=1"],
)
def test_a_top_graph_reads_is_one_however_it_is_spelled(client, admin_h, team_id, top):
    """ASCII digits with a sign and .NET's blanks around them — tabs, no-break and ideographic
    spaces and line separators among them — and the option's name in any case, on the directory
    and the message methods alike."""
    query = top if top.startswith("$") else f"$top={top}"
    users = client.get(f"{V1}/users?{query}", headers=admin_h).json()
    assert len(users["value"]) == 1
    assert query.split("=")[0] in users["@odata.nextLink"]
    platform = f"{V1}/teams/{team_id}/channels/{_channels(client, admin_h, team_id)['Platform']}"
    assert len(client.get(f"{platform}/messages?{query}", headers=admin_h).json()["value"]) == 1


@pytest.mark.parametrize("path", ["/users", "/me", "/users/ava@acme.com"])
def test_the_directory_answers_in_json_only(client, admin_h, path):
    for value in ("json", "JSON", "application/json"):
        assert client.get(f"{V1}{path}?$format={value}", headers=admin_h).status_code == 200
    for value, message in (
        ("xml", "The MIME type 'xml' is invalid or unspecified."),
        ("bogus", "The MIME type 'bogus' is invalid or unspecified."),
    ):
        err = _error(client.get(f"{V1}{path}?$format={value}", headers=admin_h), 415)
        assert (err["code"], err["message"]) == ("Request_BadRequest", message)
    err = _error(client.get(f"{V1}{path}?$format=application/xml", headers=admin_h), 415)
    assert err["message"].startswith(
        "A supported MIME type could not be found that matches the acceptable MIME types"
    )
    assert err["message"].endswith(
        "' do not match any of the acceptable MIME types 'application/xml'."
    )


def test_the_directory_answers_an_advanced_query(client, admin_h):
    h = {**admin_h, **_EVENTUAL}
    everyone = client.get(f"{V1}/users", headers=admin_h).json()["value"]
    ava = next(u for u in everyone if u["mail"] == "ava@acme.com")
    others = client.get(
        f"{V1}/users?$filter=displayName ne '{ava['displayName']}'&$count=true", headers=h
    ).json()
    assert others["@odata.count"] == len(everyone) - 1
    assert ava["id"] not in {u["id"] for u in others["value"]}
    first_word = ava["displayName"].split()[0][:2].lower()
    found = client.get(f'{V1}/users?$search="displayName:{first_word}"', headers=h).json()
    assert ava["id"] in {u["id"] for u in found["value"]}
    both = client.get(
        f'{V1}/users?$search="displayName:{first_word}" AND "mail:ava"', headers=h
    ).json()
    assert [u["id"] for u in both["value"]] == [ava["id"]]
    ends = client.get(f"{V1}/users?$filter=endswith(mail,'@acme.com')", headers=h).json()
    assert len(ends["value"]) == len(everyone)
    # The header in any case and with blanks around it; a blank one asks for nothing.
    for value in ("EVENTUAL", " eventual "):
        loose = {**admin_h, "ConsistencyLevel": value}
        assert client.get(f"{V1}/users?$count=true", headers=loose).json()["@odata.count"]
    blank = client.get(f"{V1}/users?$count=true", headers={**admin_h, "ConsistencyLevel": " "})
    assert "@odata.count" not in blank.json()
    twice = [*admin_h.items(), ("ConsistencyLevel", "eventual"), ("ConsistencyLevel", "eventual")]
    err = _error(client.get(f"{V1}/users", headers=twice), 400)
    assert (err["code"], err["message"]) == (
        "Request_BadRequest",
        "One or more headers are invalid.",
    )
    # A constant before a property is read as the property before it, the operator unchanged,
    # so `'a' le displayName` is `displayName le 'a'`; null is a value an advanced query compares.
    for expr, matched in (
        ("displayName ge 'a'", len(everyone)),
        ("'a' le displayName", 0),
        (f"'{ava['displayName']}' eq displayName", 1),
        ("startswith('a',displayName)", 0),
    ):
        assert len(client.get(f"{V1}/users?$filter={expr}", headers=admin_h).json()["value"]) == (
            matched
        ), expr
    assert client.get(f"{V1}/users?$filter=displayName eq null", headers=h).json()["value"] == []
    # An advanced query lifts the limits on `or` and on a lambda.
    many = " or ".join(["displayName eq 'x'"] * 16)
    assert client.get(f"{V1}/users?$filter={many}", headers=h).status_code == 200
    assert (
        client.get(f"{V1}/users?$filter=businessPhones/any(p:p eq '1')", headers=h).json()["value"]
        == []
    )


def test_a_user_resolves_by_object_id_and_by_principal_name(client, admin_h):
    by_upn = client.get(f"{V1}/users/ava@acme.com", headers=admin_h).json()
    by_id = client.get(f"{V1}/users/{by_upn['id']}", headers=admin_h).json()
    assert by_id == by_upn
    err = _error(client.get(f"{V1}/users/nobody@acme.com", headers=admin_h), 404)
    assert err["code"] == "Request_ResourceNotFound"
    assert err["message"] == (
        "Resource 'nobody@acme.com' does not exist or one of its queried reference-property "
        "objects are not present."
    )


def test_joined_teams_is_the_one_team_the_corpus_models(client, admin_h, tokens_yaml):
    body = client.get(f"{V1}/me/joinedTeams", headers=admin_h).json()
    assert body["@odata.count"] == len(body["value"]) == 1
    # Only id, displayName, description, isArchived and tenantId are populated; the rest are null.
    populated = {k for k, v in body["value"][0].items() if v is not None}
    assert populated == {"id", "displayName", "description", "isArchived", "tenantId"}
    # The same answer for a user by id, which is how a client that cannot ask `/me` lists teams.
    by_user = client.get(
        f"{V1}/users/{synth.msteams_user_id('ava@acme.com')}/joinedTeams", headers=admin_h
    ).json()
    assert by_user == body
    # A scoped caller lists its own only, by id or by address; anyone else's is a 403, whether or
    # not they exist, unless an address names nobody.
    bob = {"Authorization": f"Bearer {tok(tokens_yaml, 'bob@acme.com')}"}
    own = synth.msteams_user_id("bob@acme.com")
    for user in (own, own.upper(), "bob@acme.com"):
        assert client.get(f"{V1}/users/{user}/joinedTeams", headers=bob).json() == body, user
    for user in (
        synth.msteams_user_id("ava@acme.com"),
        "ava@acme.com",
        "00000000-0000-0000-0000-00000000000a",
    ):
        err = _error(client.get(f"{V1}/users/{user}/joinedTeams", headers=bob), 403)
        message = "Invalid UserId. UserId does not match Access token UserObjectId"
        assert (err["code"], err["message"]) == ("Forbidden", message), user
        inner = err["innerError"]
        assert (inner["code"], inner["message"], inner["details"]) == ("AccessDenied", message, [])
    err = _error(client.get(f"{V1}/users/nobody@acme.com/joinedTeams", headers=bob), 404)
    assert err["message"] == "Failed to find user with id 'nobody@acme.com' in the tenant"
    # A person the directory does not have, in the words of the lookup that failed.
    for user, status, message in (
        ("nobody@acme.com", 404, "Failed to find user with id 'nobody@acme.com' in the tenant"),
        ("bogus", 400, "userId must be a GUID."),
    ):
        err = _error(client.get(f"{V1}/users/{user}/joinedTeams", headers=admin_h), status)
        assert err["message"] == message
    unknown = "00000000-0000-0000-0000-00000000000A"
    err = _error(client.get(f"{V1}/users/{unknown}/joinedTeams", headers=admin_h), 404)
    assert err["message"].startswith(
        "Failed to execute MsGraph backend request GetUserGroupsS2SRequest. Workload "
        "MsGraph_DirectoryServices. Request Url: https://graph.microsoft.com/edu/users/"
        f"{unknown.lower()}/memberOf/"
    )
    assert (err["innerError"]["code"], err["innerError"]["details"]) == ("ItemNotFound", [])
    # `$select` projects it, keeping a name the type does not declare, and `$filter` is ignored;
    # the paging and shaping options are refused.
    selected = client.get(f"{V1}/me/joinedTeams?$select=displayName", headers=admin_h).json()
    assert selected["@odata.context"].endswith("#teams(displayName)")
    assert [set(t) for t in selected["value"]] == [{"displayName"}]
    bogus = client.get(f"{V1}/me/joinedTeams?$select=bogus", headers=admin_h).json()
    assert bogus["@odata.context"].endswith("#teams(bogus)") and bogus["value"] == [{}]
    ignored = client.get(f"{V1}/me/joinedTeams?$filter=displayName eq 'zzz'", headers=admin_h)
    assert len(ignored.json()["value"]) == 1
    for option, value in (
        ("Top", "1"),
        ("SkipToken", "x"),
        ("Count", "true"),
        ("OrderBy", "displayName"),
        ("Expand", "channels"),
        ("Skip", "1"),
        ("Search", "x"),
        ("Format", "json"),
    ):
        r = client.get(f"{V1}/me/joinedTeams?${option.lower()}={value}", headers=admin_h)
        assert _error(r, 400)["message"].startswith(
            f"The query specified in the URI is not valid. Query option '{option}' is not allowed."
        ), option


def test_a_team_read_by_id_is_shaped_as_graph_sends_one(client, admin_h, team_id, ro_conn):
    """Its first channel is the one it was created with, and its creation that channel's; nobody
    owns it here. `$select` keeps a name the type does not declare, as `joinedTeams` does."""
    team = client.get(f"{V1}/teams/{team_id}", headers=admin_h).json()
    assert list(team) == [
        "@odata.context",
        "id",
        "createdDateTime",
        "displayName",
        "description",
        "internalId",
        "classification",
        "specialization",
        "visibility",
        "webUrl",
        "isArchived",
        "isMembershipLimitedToOwners",
        "discoverySettings",
        "tagSettings",
        "memberSettings",
        "guestSettings",
        "messagingSettings",
        "funSettings",
        "summary",
    ]
    assert team["@odata.context"].endswith("#teams/$entity")
    channels = client.get(f"{V1}/teams/{team_id}/channels", headers=admin_h).json()["value"]
    first = min(channels, key=lambda c: c["createdDateTime"])
    assert (team["internalId"], team["createdDateTime"]) == (first["id"], first["createdDateTime"])
    assert team["webUrl"].startswith(
        f"https://teams.microsoft.com/l/team/{first['id'].replace(':', '%3a').replace('@', '%40')}/"
    )
    assert (team["specialization"], team["visibility"]) == ("none", "public")
    assert team["summary"] == {
        "ownersCount": 0,
        "membersCount": len(store.all_user_emails(ro_conn)),
        "guestsCount": 0,
    }
    selected = client.get(f"{V1}/teams/{team_id}?$select=displayName,id", headers=admin_h).json()
    assert list(selected) == ["@odata.context", "id", "displayName"]
    assert selected["@odata.context"].endswith("#teams(displayName,id)/$entity")
    bogus = client.get(f"{V1}/teams/{team_id}?$select=bogus", headers=admin_h).json()
    assert list(bogus) == ["@odata.context"]
    unechoed = client.get(f"{V1}/teams/{team_id}?select=displayName", headers=admin_h).json()
    assert unechoed["@odata.context"].endswith("#teams/$entity")
    # Every other option is refused, an `$expand` that binds too, and all before the id is read.
    for query, code, message in (
        ("$top=1", "", "Top"),
        ("$filter=isArchived", "", "Filter"),
        ("$format=json", "", "Format"),
        (
            "$expand=channels",
            "",
            "The query specified in the URI is not valid. The property 'channels' cannot be used "
            "in the $expand query option.",
        ),
        (
            "$expand=bogus",
            "BadRequest",
            "Parsing OData Select and Expand failed: Could not find a property named 'bogus' on "
            "type 'microsoft.graph.team'.",
        ),
        ("$top=abc", "BadRequest", _invalid("top", "abc")),
        ("$skiptoken=", "BadRequest", _RAW_VALUE),
    ):
        for team in (team_id, "bogus", "00000000-0000-0000-0000-000000000000"):
            err = _error(client.get(f"{V1}/teams/{team}?{query}", headers=admin_h), 400)
            if " " not in message:
                message = (
                    f"The query specified in the URI is not valid. Query option '{message}' is "
                    "not allowed. To allow it, set the 'AllowedQueryOptions' property on "
                    "EnableQueryAttribute or QueryValidationSettings."
                )
            assert (err["code"], err["message"]) == (code, message), (query, team)


@pytest.mark.parametrize("path", ["/me", "/users/{user}"])
def test_a_single_person_refuses_a_filter_and_ignores_top(client, admin_h, path):
    url = f"{V1}{path.format(user=synth.msteams_user_id('ava@acme.com'))}"
    single = (
        "Request_UnsupportedQuery",
        "The specified filter against a single entity is invalid.",
    )
    for query, want in (
        ("$filter=displayName eq 'x'", single),
        ("$search=x", single),
        ("$skiptoken=x", ("Request_BadRequest", "Specified $skiptoken is invalid.")),
        (
            "$expand=bogus",
            (
                "BadRequest",
                "Parsing OData Select and Expand failed: Could not find a property named 'bogus' "
                "on type 'microsoft.graph.user'.",
            ),
        ),
    ):
        err = _error(client.get(f"{url}?{query}", headers=admin_h), 400)
        assert (err["code"], err["message"]) == want, query
    for query in ("$top=1", "$orderby=displayName", "$count=true", "$expand=manager"):
        assert client.get(f"{url}?{query}", headers=admin_h).status_code == 200, query


# --- channels --------------------------------------------------------------------


def test_listing_and_getting_a_channel_differ_where_graph_does(client, admin_h, team_id):
    listed = client.get(f"{V1}/teams/{team_id}/channels", headers=admin_h).json()
    assert listed["@odata.count"] == len(listed["value"])
    platform = next(c for c in listed["value"] if c["displayName"] == "Platform")
    assert list(platform) == [
        "id",
        "createdDateTime",
        "displayName",
        "description",
        "isFavoriteByDefault",
        "email",
        "tenantId",
        "webUrl",
        "membershipType",
        "layoutType",
        "isArchived",
    ]
    assert platform["layoutType"] is None
    assert platform["description"] is None and platform["email"] is None
    # An option name in ASCII case only: with a Kelvin sign for its k, it is no option Graph reads.
    kelvin = client.get(f"{V1}/teams/{team_id}/channels?$s\u212aiptoken=abc", headers=admin_h)
    assert kelvin.status_code == 200
    assert platform["webUrl"].startswith("https://teams.cloud.microsoft/l/channel/")
    # `ngc=True` is on a private channel's address only.
    assert platform["webUrl"].endswith("&allowXTenantAccess=False")
    private = next(c for c in listed["value"] if c["membershipType"] == "private")
    assert private["webUrl"].endswith("&allowXTenantAccess=False&ngc=True")

    one = client.get(f"{V1}/teams/{team_id}/channels/{platform['id']}", headers=admin_h).json()
    assert one["@odata.context"].endswith(f"teams('{team_id}')/channels/$entity")
    assert one["layoutType"] == "chat"
    members = client.get(
        f"{V1}/teams/{team_id}/channels/{platform['id']}/members", headers=admin_h
    ).json()
    assert one["summary"] == {
        "ownersCount": 0,
        "membersCount": len(members["value"]),
        "guestsCount": 0,
        "hasMembersFromOtherTenants": False,
    }
    assert {
        k: v for k, v in one.items() if k not in ("@odata.context", "layoutType", "summary")
    } == {k: v for k, v in platform.items() if k != "layoutType"}


_ALL_CHANNELS = ["Incidents", "People Confidential", "Platform"]


@pytest.mark.parametrize(
    "expr",
    [
        "length(displayName) div 0 eq 1",
        "length(displayName) mod 0 eq 1",
        # A decimal divides as an integer does.
        "1.0000000000000000000000000001 div 0 eq 1",
    ],
)
@pytest.mark.parametrize("suffix", ["/channels", "/channels/{channel}/members"])
def test_a_filter_dotnet_fails_to_run_is_graphs_500(client, admin_h, team_id, expr, suffix):
    path = suffix.format(channel=_channels(client, admin_h, team_id)["Platform"])
    r = client.get(f"{V1}/teams/{team_id}{path}", headers=admin_h, params={"$filter": expr})
    err = _error(r, 500)
    assert (err["code"], err["message"]) == ("InternalServerError", "Failed to execute request.")


_CHANNEL_FILTERS = {
    "membershipType eq 'private'": ["People Confidential"],
    "membershipType eq 'shared'": [],
    "displayName eq 'Platform'": ["Platform"],
    "displayName eq 'PLATFORM'": [],  # case-sensitive here, unlike the directory
    "startswith(displayName,'Pl')": ["Platform"],
    "contains(displayName,'cid')": ["Incidents"],
    "displayName ne 'Platform' and membershipType eq 'standard'": ["Incidents"],
    # Property names and enum members match without regard to case.
    "MembershipType eq 'Private'": ["People Confidential"],
    "displayName EQ 'Platform' OR displayName eq 'Incidents'": ["Incidents", "Platform"],
    "startswith(DisplayName,'Pl')": ["Platform"],
    "displayName in ('Platform','Incidents')": ["Incidents", "Platform"],
    "displayName in ()": [],
    "": _ALL_CHANNELS,
    " ": _ALL_CHANNELS,
    # `not` binds tighter than a comparison, and a boolean property is a predicate on its own.
    "not isArchived eq true": _ALL_CHANNELS,
    "not (displayName eq 'Platform')": ["Incidents", "People Confidential"],
    "isArchived": [],
    "not isArchived and displayName eq 'Platform'": ["Platform"],
    "isArchived in (false)": _ALL_CHANNELS,
    # Typed literals: a date-time, a date, a qualified enum member and an enum's position.
    "createdDateTime gt 2026-02-06T00:00:00Z": ["Incidents", "People Confidential"],
    "createdDateTime lt 2026-02-06T13:00+01:00": ["People Confidential", "Platform"],
    "createdDateTime ge 2026-02-10": ["Incidents"],
    "membershipType eq microsoft.graph.channelMembershipType'private'": ["People Confidential"],
    "membershipType eq 1": ["People Confidential"],
    "displayName eq 'it''s'": [],
    "tolower(displayName) eq 'platform'": ["Platform"],
    "length(displayName) eq 8": ["Platform"],
    # Strings order with null below every one of them; literals compare with each other.
    "displayName gt null": _ALL_CHANNELS,
    "null eq displayName": [],
    "5 eq 5": _ALL_CHANNELS,
    "createdDateTime gt 2020-01-01T00:00:00z": _ALL_CHANNELS,
    # .NET's arithmetic and `substring`: an integer division truncates, a remainder takes the
    # dividend's sign, and a negative start or length is no characters skipped or taken.
    "length(displayName) div 2 eq 4": ["Incidents", "Platform"],
    "length(displayName) mod 4 eq 1": ["Incidents"],
    "substring(displayName,-1) eq displayName": _ALL_CHANNELS,
    "substring(displayName,1,-1) eq ''": _ALL_CHANNELS,
    "indexof(displayName,'zz') eq -1": _ALL_CHANNELS,
    "-7 div 2 eq -3": _ALL_CHANNELS,
    "-7 mod 3 eq -1": _ALL_CHANNELS,
    # A floating division by zero is an infinity or NaN, compared as a number; a `Single` is
    # computed as one, so 0.1 and 0.2 make 0.3.
    "length(displayName) div 0.0 eq 1": [],
    "length(displayName) div 0.0 gt 1": _ALL_CHANNELS,
    "length(displayName) div -0.0 lt 0": _ALL_CHANNELS,
    "length(displayName) mod 0.0 ne 1": _ALL_CHANNELS,
    "1.5 div 0 eq 1": [],
    "1.23456789 div 0 gt 1": _ALL_CHANNELS,
    "0.0 div 0.0 ne 0.0 div 0.0": _ALL_CHANNELS,
    "0.1 add 0.2 eq 0.3": _ALL_CHANNELS,
    # The limits, at the edge: 99 parentheses, 16 terms and 98 `not`s fit.
    "(" * 99 + "displayName eq 'Platform'" + ")" * 99: ["Platform"],
    " and ".join(["displayName ne 'x'"] * 16): _ALL_CHANNELS,
    "not " * 98 + "isArchived": [],
}


def test_a_channel_filter_selects_what_graph_selects(client, admin_h, team_id):
    selected = {}
    for expr in _CHANNEL_FILTERS:
        body = client.get(
            f"{V1}/teams/{team_id}/channels", headers=admin_h, params={"$filter": expr}
        ).json()
        selected[expr] = sorted(c["displayName"] for c in body["value"])
    assert selected == _CHANNEL_FILTERS


@pytest.mark.parametrize(
    "params,code,message",
    [
        (
            {"$filter": "foo eq 'x'"},
            "BadRequest",
            "Invalid filter clause: Could not find a "
            "property named 'foo' on type 'microsoft.graph.channel'.",
        ),
        (
            {"$filter": "membershipType eq 'bogus'"},
            "BadRequest",
            "Invalid filter clause: The string 'bogus' is not a valid enumeration type constant.",
        ),
        (
            {"$filter": "displayName eq"},
            "BadRequest",
            "Invalid filter clause: Expression expected at position 14 in 'displayName eq'.",
        ),
        (
            {"$select": "bogus"},
            "BadRequest",
            "Parsing OData Select and Expand failed: Could not "
            "find a property named 'bogus' on type 'microsoft.graph.channel'.",
        ),
        # The listing is not paged, and refuses what would page it.
        (
            {"$top": "1"},
            "BadRequest",
            "The query specified in the URI is not valid. Query option 'Top' is not allowed.",
        ),
        (
            {"$skiptoken": "x"},
            "BadRequest",
            "The query specified in the URI is not valid. Query option 'SkipToken' is not allowed.",
        ),
        (
            {"$orderby": "displayName"},
            "",
            "The query specified in the URI is not valid. Query "
            "option 'OrderBy' is not allowed. To allow it, set the 'AllowedQueryOptions' property "
            "on EnableQueryAttribute or QueryValidationSettings.",
        ),
        (
            {"$expand": "members"},
            "",
            "The query specified in the URI is not valid. Query option "
            "'Expand' is not allowed. To allow it, set the 'AllowedQueryOptions' property on "
            "EnableQueryAttribute or QueryValidationSettings.",
        ),
        (
            {"$count": "true"},
            "",
            "The query specified in the URI is not valid. Query option 'Count' is not allowed. To "
            "allow it, set the 'AllowedQueryOptions' property on EnableQueryAttribute or "
            "QueryValidationSettings.",
        ),
        # Option names match in any case, and with or without the `$`, on the Teams routes.
        (
            {"$TOP": "1"},
            "BadRequest",
            "The query specified in the URI is not valid. Query option 'Top' is not allowed.",
        ),
        (
            {"top": "1"},
            "BadRequest",
            "The query specified in the URI is not valid. Query option 'Top' is not allowed.",
        ),
        (
            {"$skip": "1"},
            "",
            "The query specified in the URI is not valid. Query option 'Skip' is not allowed. To "
            "allow it, set the 'AllowedQueryOptions' property on EnableQueryAttribute or "
            "QueryValidationSettings.",
        ),
        (
            {"$search": "x"},
            "",
            "The query specified in the URI is not valid. Query option 'Search' is not allowed. "
            "To allow it, set the 'AllowedQueryOptions' property on EnableQueryAttribute or "
            "QueryValidationSettings.",
        ),
        # The validator's refusal comes before the listing's own refusal of `$top`.
        (
            {"$orderby": "displayName", "$top": "1"},
            "",
            "The query specified in the URI is not valid. Query option 'OrderBy' is not allowed. "
            "To allow it, set the 'AllowedQueryOptions' property on EnableQueryAttribute or "
            "QueryValidationSettings.",
        ),
        (
            {"$format": "json"},
            "",
            "The query specified in the URI is not valid. Query option 'Format' is not allowed. To "
            "allow it, set the 'AllowedQueryOptions' property on EnableQueryAttribute or "
            "QueryValidationSettings.",
        ),
        ({"$skiptoken": ""}, "BadRequest", _RAW_VALUE),
        # ASCII case only: a long s is not an s.
        (
            {"$select": "diſplayName"},
            "BadRequest",
            "Parsing OData Select and Expand failed: Could not find a property named "
            "'diſplayName' on type 'microsoft.graph.channel'.",
        ),
        (
            {"$filter": "diſplayName eq 'x'"},
            "BadRequest",
            "Invalid filter clause: Could not find a property named 'diſplayName' on type "
            "'microsoft.graph.channel'.",
        ),
        # Operands are typed, each literal as the narrowest type that holds it.
        (
            {"$filter": "displayName gt 5"},
            "BadRequest",
            _INCOMPATIBLE.format("Edm.String", "Edm.Int32", "GreaterThan"),
        ),
        (
            {"$filter": "createdDateTime gt 5"},
            "BadRequest",
            _INCOMPATIBLE.format("Edm.DateTimeOffset", "Edm.Int32", "GreaterThan"),
        ),
        (
            {"$filter": "displayName eq 2147483648"},
            "BadRequest",
            _INCOMPATIBLE.format("Edm.String", "Edm.Int64", "Equal"),
        ),
        (
            {"$filter": "displayName eq 99999999999999999999"},
            "BadRequest",
            _INCOMPATIBLE.format("Edm.String", "Edm.Decimal", "Equal"),
        ),
        (
            {"$filter": f"displayName eq {_NINES}"},
            "BadRequest",
            _INCOMPATIBLE.format("Edm.String", "Edm.Single", "Equal"),
        ),
        (
            {"$filter": "displayName eq 1.5"},
            "BadRequest",
            _INCOMPATIBLE.format("Edm.String", "Edm.Single", "Equal"),
        ),
        # A fraction is the first floating or decimal type that holds it exactly; one no decimal
        # holds is the first floating type that does not overflow or underflow.
        *(
            (
                {"$filter": f"displayName eq {literal}"},
                "BadRequest",
                _INCOMPATIBLE.format("Edm.String", kind, "Equal"),
            )
            for literal, kind in (
                ("1.2345678", "Edm.Single"),
                ("1.23456789", "Edm.Double"),
                ("16777217.0", "Edm.Double"),
                ("0.30000000000000004", "Edm.Double"),
                ("0.3000000000000000444", "Edm.Decimal"),
                ("1.0000000000000000000000000001", "Edm.Decimal"),
                ("1e29", "Edm.Single"),
                ("1e39", "Edm.Double"),
                ("1e309", "Edm.Single"),
                ("1e-30", "Edm.Single"),
                ("1e-50", "Edm.Double"),
            )
        ),
        (
            {"$filter": "displayName eq 1.e5"},
            "BadRequest",
            "Invalid filter clause: A digit was expected at position 17 in 'displayName eq 1.e5'.",
        ),
        (
            {"$filter": "isArchived eq 'x'"},
            "BadRequest",
            _INCOMPATIBLE.format("Edm.Boolean", "Edm.String", "Equal"),
        ),
        (
            {"$filter": "displayName and isArchived"},
            "BadRequest",
            _INCOMPATIBLE.format("Edm.String", "Edm.Boolean", "And"),
        ),
        (
            {"$filter": "displayName add 1 eq 2"},
            "BadRequest",
            _INCOMPATIBLE.format("Edm.String", "Edm.Int32", "Add"),
        ),
        (
            {"$filter": "displayName eq 2020-01-01T00:00:00Z"},
            "BadRequest",
            _INCOMPATIBLE.format("Edm.String", "Edm.DateTimeOffset", "Equal"),
        ),
        (
            {"$filter": "not displayName eq 'x'"},
            "BadRequest",
            "Invalid filter clause: A unary operator with an incompatible type was detected. Found "
            "operand type 'Edm.String' for operator kind 'Not'.",
        ),
        (
            {"$filter": "displayName"},
            "BadRequest",
            "Invalid filter clause: The $filter expression must evaluate to a single boolean value.",
        ),
        (
            {"$filter": "startswith(displayName,5)"},
            "BadRequest",
            "Invalid filter clause: No function signature for the function with name 'startswith' "
            "matches the specified arguments. The function signatures considered are: "
            "startswith(Edm.String Nullable=true, Edm.String Nullable=true).",
        ),
        (
            {"$filter": "bogus(displayName)"},
            "BadRequest",
            "Invalid filter clause: An unknown function with name 'bogus' was found. This may also "
            "be a function import or a key lookup on a navigation property, which is not allowed.",
        ),
        (
            {"$filter": "displayName has 'x'"},
            "BadRequest",
            "Invalid filter clause: An internal error "
            "'TypePromotionUtils_GetFunctionSignatures_Binary_UnreachableCodepath' occurred.",
        ),
        (
            {"$filter": "isArchived eq True"},
            "BadRequest",
            "Invalid filter clause: Could not find a property named 'True' on type "
            "'microsoft.graph.channel'.",
        ),
        (
            {"$filter": "displayName in (1,2)"},
            "BadRequest",
            "Invalid filter clause: String item should be single/double quoted: '1'.",
        ),
        # What the parser refuses, and where it stops.
        (
            {"$filter": "displayName eq 'x' 'y'"},
            "BadRequest",
            "Invalid filter clause: Syntax error at position 22 in 'displayName eq 'x' 'y''.",
        ),
        (
            {"$filter": "displayName eqq 'x'"},
            "BadRequest",
            "Invalid filter clause: Syntax error at position 15 in 'displayName eqq 'x''.",
        ),
        (
            {"$filter": "id eq 19:abc"},
            "BadRequest",
            "Invalid filter clause: Syntax error at position 9 in 'id eq 19:abc'.",
        ),
        (
            {"$filter": "(displayName eq 'x'"},
            "BadRequest",
            "Invalid filter clause: ')' or operator expected at position 19 in "
            "'(displayName eq 'x''.",
        ),
        (
            {"$filter": 'displayName eq "x"'},
            "BadRequest",
            "Invalid filter clause: Syntax error: character '\"' is not valid at position 15 in "
            "'displayName eq \"x\"'.",
        ),
        (
            {"$filter": "displayName eq 'x''"},
            "BadRequest",
            "Invalid filter clause: There is an unterminated string literal at position 19 in "
            "'displayName eq 'x'''.",
        ),
        (
            {"$filter": "displayName in ('a',)"},
            "BadRequest",
            "Invalid filter clause: Invalid JSON. An unexpected comma was found in scope 'Array'. A "
            "comma is only valid between properties of an object or between elements of an array.",
        ),
        (
            {"$filter": "displayName in ('a' 'b')"},
            "BadRequest",
            "Invalid filter clause: Invalid JSON. A comma character ',' was expected in scope "
            "'Array'. Every two elements in an array and properties of an object must be "
            "separated by commas.",
        ),
        (
            {"$filter": "displayName IN ('a')"},
            "BadRequest",
            "Invalid filter clause: An unexpected 'PrimitiveValue' node was found when reading "
            "from the JSON reader. A 'StartArray' node was expected.",
        ),
        (
            {"$filter": "createdDateTime gt 2020-13-01T00:00:00Z"},
            "BadRequest",
            "Invalid filter clause: The DateTimeOffset text '2020-13-01T00:00:00Z' should be in "
            "format 'yyyy-mm-ddThh:mm:ss('.'s+)?(zzzzzz)?' and each field value is within valid "
            "range.",
        ),
        (
            {"$filter": "createdDateTime gt 2020-01-01T00:00:00"},
            "BadRequest",
            "Invalid filter clause: The DateTimeOffset text '2020-01-01T00:00:00' should be in "
            "format 'yyyy-mm-ddThh:mm:ss('.'s+)?(zzzzzz)?' and each field value is within valid "
            "range.",
        ),
        # Text that is not a date is read as the number in front of it.
        (
            {"$filter": "createdDateTime gt 2020-13-45"},
            "BadRequest",
            "Invalid filter clause: Syntax error at position 26 in 'createdDateTime gt 2020-13-45'.",
        ),
        (
            {"$filter": "createdDateTime gt 2020-02-30"},
            "BadRequest",
            "Invalid filter clause: Syntax error at position 26 in 'createdDateTime gt 2020-02-30'.",
        ),
        (
            {"$filter": "createdDateTime gt 2020-01-01t00:00:00z"},
            "BadRequest",
            "Invalid filter clause: Syntax error at position 26 in "
            "'createdDateTime gt 2020-01-01t00:00:00z'.",
        ),
        (
            {"$filter": "displayName eq " + "0" * 4301 + "1"},
            "BadRequest",
            _INCOMPATIBLE.format("Edm.String", "Edm.Int32", "Equal"),
        ),
        (
            {"$filter": "isArchived in (/)"},
            "BadRequest",
            "Invalid filter clause: Invalid JSON. A token was not recognized in the JSON content.",
        ),
        (
            {"$filter": "displayName in ('a',/)"},
            "BadRequest",
            "Invalid filter clause: String item should be single/double quoted: '/'.",
        ),
        (
            {"$filter": "isArchived in (()"},
            "BadRequest",
            "Invalid filter clause: Found an unbalanced bracket expression.",
        ),
        (
            {"$filter": "summary in ('x')"},
            "BadRequest",
            "Invalid filter clause: The property with name '' was found with a value node of type "
            "'PrimitiveValue'; however, a resource value of type 'microsoft.graph.channelSummary' "
            "was expected.",
        ),
        # An `$orderby` term is an expression: a constant binds, and the option is refused.
        (
            {"$orderby": "1"},
            "",
            "The query specified in the URI is not valid. Query option 'OrderBy' is not allowed. "
            "To allow it, set the 'AllowedQueryOptions' property on EnableQueryAttribute or "
            "QueryValidationSettings.",
        ),
        ({"$orderby": "2020-13-45"}, "BadRequest", "Syntax error at position 7 in '2020-13-45'."),
        # The parser's recursion limit, reached before the binder reads a property.
        ({"$filter": "(" * 100 + "bogus eq 'x'" + ")" * 100}, "BadRequest", _DEPTH),
        ({"$filter": "not " * 1200 + "isArchived"}, "BadRequest", _DEPTH),
        ({"$filter": "not(" * 89 + "isArchived" + ")" * 89}, "BadRequest", _DEPTH),
        # The validator's node count: a boolean property weighs 2 here, any other 3.
        ({"$filter": " and ".join(["displayName ne 'x'"] * 17)}, "", _NODES),
        ({"$filter": " or ".join(["isArchived"] * 34)}, "", _NODES),
        ({"$filter": "not " * 99 + "isArchived"}, "", _NODES),
        ({"$filter": " or ".join(["'x' eq 'x'"] * 26)}, "", _NODES),
        # The binder before the node count, and the count before the listing's own `$top`.
        (
            {"$filter": " or ".join(["displayName eq 'x'"] * 17) + " or displayName gt 5"},
            "BadRequest",
            _INCOMPATIBLE.format("Edm.String", "Edm.Int32", "GreaterThan"),
        ),
        ({"$filter": " or ".join(["displayName eq 'x'"] * 17), "$top": "1"}, "", _NODES),
        # A boolean ordered against another binds, and .NET cannot run it.
        (
            {"$filter": "isArchived gt true"},
            "",
            "The query specified in the URI is not valid. The binary operator GreaterThan is not "
            "defined for the types 'System.Nullable`1[System.Boolean]' and "
            "'System.Nullable`1[System.Boolean]'.",
        ),
        (
            {"$filter": "isArchived ge null"},
            "",
            "The query specified in the URI is not valid. The binary operator GreaterThanOrEqual "
            "is not defined for the types 'System.Nullable`1[System.Boolean]' and 'System.Object'.",
        ),
    ],
)
def test_a_channel_listing_refuses_a_query_it_cannot_run(
    client, admin_h, team_id, params, code, message
):
    err = _error(client.get(f"{V1}/teams/{team_id}/channels", headers=admin_h, params=params), 400)
    assert (err["code"], err["message"]) == (code, message)
    # The validator's own refusals carry no code, a `details` list and the reason again inside.
    if code == "":
        assert err["details"] == []
        assert message.endswith(err["innerError"]["message"])


@pytest.mark.parametrize(
    "route,prop",
    [
        ("/me/joinedTeams", "displayName"),
        ("/teams/{team}/channels", "displayName"),
        ("/teams/{team}/channels/{channel}", "displayName"),
        ("/teams/{team}/channels/{channel}/messages", "id"),
        ("/teams/{team}/channels/{channel}/members", "displayName"),
    ],
)
def test_select_is_read_in_every_spelling_and_only_dollar_select_is_echoed(
    client, admin_h, team_id, route, prop
):
    """The option name in any case, the property name in any case (answered in the declared
    spelling), and `select` without the `$` — which projects the same but leaves `@odata.context`
    without the list. `id` is not kept unless asked for. Twice is refused."""
    url = (
        f"{V1}{route.format(team=team_id, channel=_channels(client, admin_h, team_id)['Platform'])}"
    )
    for query, echoed in (
        (f"$select={prop}", True),
        (f"$SELECT={prop.upper()}", True),
        (f"select={prop}", False),
    ):
        body = client.get(f"{url}?{query}", headers=admin_h).json()
        items = body["value"] if "value" in body else [body]
        assert all(set(i) - {"@odata.context", "@odata.type"} == {prop} for i in items), query
        context = body["@odata.context"].removesuffix("/$entity")
        assert context.endswith(f"({prop})") == echoed, query
    err = _error(client.get(f"{url}?$select={prop}&$select=id", headers=admin_h), 400)
    assert err["message"] == (
        "Query option '$select' was specified more than once, but it must be specified at most "
        "once."
    )
    err = _error(client.get(f"{url}?$select={prop}&select=id", headers=admin_h), 400)
    assert err["message"] == "An item with the same key has already been added. Key: $select"


@pytest.mark.parametrize(
    "suffix,query,code,message",
    [
        ("", "$expand=members", "", "Expand"),
        ("", "$filter=displayName eq 'x'", "", "Filter"),
        ("", "$top=1", "", "Top"),
        ("", "$count=true", "", "Count"),
        ("/members", "$count=true", "", "Count"),
        ("/members", "$orderby=displayName", "", "OrderBy"),
        (
            "/members",
            "$expand=user",
            "BadRequest",
            "Parsing OData Select and Expand failed: Could "
            "not find a property named 'user' on type 'microsoft.graph.conversationMember'.",
        ),
        ("/messages", "$count=true", "", "Count"),
        ("/messages/{message}/replies", "$count=true", "", "Count"),
        ("/messages/{message}", "$filter=id eq 'x'", "", "Filter"),
        ("/messages/{message}", "$top=1", "", "Top"),
        (
            "/messages/{message}",
            "$expand=replies",
            "BadRequest",
            "Expand query parameter "
            "'replies' is not supported for this request. Use the /replies endpoint to get the "
            "replies for a message.",
        ),
        ("", "$orderby=displayName", "", "OrderBy"),
        ("", "$skiptoken=x", "", "SkipToken"),
        ("", "$search=x", "", "Search"),
        ("/messages", "$search=x", "", "Search"),
        ("/messages/{message}/replies", "$filter=messageType eq 'message'", "", "Filter"),
        ("/messages/{message}/replies", "$orderby=createdDateTime", "", "OrderBy"),
        ("/messages/{message}/replies", "$search=x", "", "Search"),
        (
            "/messages/{message}/replies",
            "$expand=bogus",
            "BadRequest",
            "Parsing OData Select and "
            "Expand failed: Could not find a property named 'bogus' on type "
            "'microsoft.graph.chatMessage'.",
        ),
        ("/messages/{message}", "$count=true", "", "Count"),
        ("/messages/{message}", "$orderby=createdDateTime", "", "OrderBy"),
        ("/messages/{message}", "$skiptoken=x", "", "SkipToken"),
        ("/messages/{message}", "$skip=1", "", "Skip"),
        ("/members", "$skip=1", "", "Skip"),
        ("/members", "$search=x", "", "Search"),
        # The binder reads property names before the validator reads which options are allowed,
        # and the `$filter` before the `$orderby`.
        (
            "/messages",
            "$filter=x eq 1",
            "BadRequest",
            "Invalid filter clause: Could not find a "
            "property named 'x' on type 'microsoft.graph.chatMessage'.",
        ),
        (
            "/messages",
            "$top=51&$filter=x eq 1",
            "BadRequest",
            "Invalid filter clause: Could not "
            "find a property named 'x' on type 'microsoft.graph.chatMessage'.",
        ),
        (
            "/messages",
            "$count=true&$orderby=x",
            "BadRequest",
            "Could not find a property named 'x' on type 'microsoft.graph.chatMessage'.",
        ),
        ("", "$top=1&$expand=members", "", "Top"),
        (
            "/messages",
            "$top=99999999999",
            "",
            "The query specified in the URI is not valid. The "
            "limit of '2147483647' for Top query has been exceeded. The value from the incoming "
            "request is '99999999999'.",
        ),
        # `$format` is refused on every channel method, and read last.
        ("", "$format=json", "", "Format"),
        ("/members", "$format=json", "", "Format"),
        ("/messages", "$format=json", "", "Format"),
        ("/messages/{message}", "$format=json", "", "Format"),
        ("/messages/{message}/replies", "$format=json", "", "Format"),
        ("", "$format=json&$expand=members", "", "Expand"),
        ("/messages", "$format=json&$top=51", "", _limit("Top", 50, "51")),
        ("", "$skip=1", "", "Skip"),
        # `$top` and `$skip` are 64-bit integers, read before the validator; past Int32 each has
        # the validator's limit.
        ("/messages", "$skip=abc", "BadRequest", _invalid("skip", "abc")),
        ("/messages", "$skip=-1", "BadRequest", _invalid("skip", "-1")),
        ("/messages", "$skip=", "BadRequest", _invalid("skip", "")),
        ("/messages", "$skip=2147483648", "", _limit("Skip", 2147483647, "2147483648")),
        (
            "/messages",
            "$skip=9223372036854775808",
            "BadRequest",
            _invalid("skip", "9223372036854775808"),
        ),
        ("/messages", f"$skip={_NINES}", "BadRequest", _invalid("skip", "9" * 1021 + "...")),
        ("/messages", f"$top={_NINES}", "BadRequest", _invalid("top", "9" * 1021 + "...")),
        (
            "/messages",
            "$top=9223372036854775807",
            "",
            _limit("Top", 2147483647, "9223372036854775807"),
        ),
        ("/messages/{message}/replies", "$skip=abc", "BadRequest", _invalid("skip", "abc")),
        (
            "/messages/{message}/replies",
            "$skip=2147483648",
            "",
            _limit("Skip", 2147483647, "2147483648"),
        ),
        (
            "/messages/{message}/replies",
            "$top=9223372036854775808",
            "BadRequest",
            _invalid("top", "9223372036854775808"),
        ),
        # A value that is not one is read before the option is refused.
        ("/messages/{message}", "$top=abc", "BadRequest", _invalid("top", "abc")),
        # An empty page token is refused before anything else.
        ("/messages", "$skiptoken=", "BadRequest", _RAW_VALUE),
        ("/messages", "$skiptoken=&$top=51", "BadRequest", _RAW_VALUE),
        ("/messages/{message}", "$skiptoken=", "BadRequest", _RAW_VALUE),
        ("/messages/{message}/replies", "$skiptoken=", "BadRequest", _RAW_VALUE),
        ("/members", "$skiptoken=", "BadRequest", _RAW_VALUE),
        # A filter over members: a collection is no single value, a lambda's variable is gone after
        # its parenthesis, lambdas do not nest, and every property weighs 2 in the node count.
        (
            "/members",
            "$filter=roles in ('owner')",
            "BadRequest",
            "Invalid filter clause: The left operand for the IN operation is not a single value. "
            "IN operations require the left operand to be a single value and the right operand to "
            "be a collection value.",
        ),
        (
            "/members",
            "$filter=roles eq 'owner'",
            "BadRequest",
            "Invalid filter clause: The operand for a binary operator 'Equal' is not a single "
            "value. Binary operators require both operands to be single values.",
        ),
        (
            "/members",
            "$filter=roles/any(r:r eq 'owner') or r eq null",
            "BadRequest",
            "Invalid filter clause: Could not find a property named 'r' on type "
            "'microsoft.graph.conversationMember'.",
        ),
        (
            "/members",
            "$filter=displayName/any(r:r eq 'x')",
            "BadRequest",
            "Invalid filter clause: Any/All may only be used following a collection.",
        ),
        (
            "/members",
            "$filter=roles/any(r:r)",
            "BadRequest",
            "Invalid filter clause: The Any/All query expression must evaluate to a single boolean "
            "value.",
        ),
        ("/members", "$filter=roles/any(r:roles/any(s:s eq r))", "", _NESTING),
        ("/members", "$skip=1&$filter=roles/any(r:roles/any(s:s eq r))", "", "Skip"),
        ("/members", "$filter=" + " or ".join(["displayName eq 'x'"] * 21), "", _NODES),
        (
            "/members",
            "$filter=visibleHistoryStartDateTime gt 2020-13-45",
            "BadRequest",
            "Invalid filter clause: Syntax error at position 38 in "
            "'visibleHistoryStartDateTime gt 2020-13-45'.",
        ),
        ("/messages", "$orderby=1", "", "OrderBy"),
        (
            "/messages",
            "$orderby=2020-13-45",
            "BadRequest",
            "Syntax error at position 7 in '2020-13-45'.",
        ),
        # A message listing binds a filter before refusing it.
        (
            "/messages",
            "$filter=subject gt 5",
            "BadRequest",
            _INCOMPATIBLE.format("Edm.String", "Edm.Int32", "GreaterThan"),
        ),
    ],
)
def test_a_channel_method_refuses_the_options_graph_refuses_on_it(
    client, admin_h, team_id, suffix, query, code, message
):
    """`code` empty is the OData validator's refusal; a one-word `message` names the option it
    refuses."""
    _, root = _incident(client, admin_h, team_id)
    incidents = _channels(client, admin_h, team_id)["Incidents"]
    url = f"{V1}/teams/{team_id}/channels/{incidents}{suffix.format(message=root['id'])}"
    err = _error(client.get(f"{url}?{query}", headers=admin_h), 400)
    assert err["code"] == code
    if code == "" and " " not in message:
        assert err["message"] == (
            f"The query specified in the URI is not valid. Query option '{message}' is not "
            "allowed. To allow it, set the 'AllowedQueryOptions' property on EnableQueryAttribute "
            "or QueryValidationSettings."
        )
    else:
        assert err["message"] == message


@pytest.mark.parametrize(
    "path,message",
    [
        (
            "/teams/{other}/channels?$top=1",
            "The query specified in the URI is not valid. Query option 'Top' is not allowed.",
        ),
        (
            "/teams/{team}/channels/{missing}?$top=1",
            "The query specified in the URI is not "
            "valid. Query option 'Top' is not allowed. To allow it, set the 'AllowedQueryOptions' "
            "property on EnableQueryAttribute or QueryValidationSettings.",
        ),
        (
            "/teams/{team}/channels/{missing}/messages?$top=51",
            "The query specified in the URI "
            "is not valid. The limit of '50' for Top query has been exceeded. The value from the "
            "incoming request is '51'.",
        ),
        (
            "/teams/{team}/channels/{channel}/messages/1111111111111?$top=1",
            "The query specified "
            "in the URI is not valid. Query option 'Top' is not allowed. To allow it, set the "
            "'AllowedQueryOptions' property on EnableQueryAttribute or QueryValidationSettings.",
        ),
        (
            "/teams/{team}/channels/{channel}/messages/abc?$top=1",
            "The query specified in the URI "
            "is not valid. Query option 'Top' is not allowed. To allow it, set the "
            "'AllowedQueryOptions' property on EnableQueryAttribute or QueryValidationSettings.",
        ),
        (
            "/users/nobody@acme.com/joinedTeams?$top=1",
            "The query specified in the URI is not "
            "valid. Query option 'Top' is not allowed. To allow it, set the 'AllowedQueryOptions' "
            "property on EnableQueryAttribute or QueryValidationSettings.",
        ),
        ("/teams/abc/channels?$top=1", "teamId needs to be a valid GUID."),
        ("/teams/abc/channels?$skiptoken=", _RAW_VALUE),
        ("/teams/abc/channels/{channel}/messages?$top=abc", _invalid("top", "abc")),
        (
            "/teams/abc/channels/{channel}/members?$filter=bogus eq 1",
            "Invalid filter clause: Could not find a property named 'bogus' on type "
            "'microsoft.graph.conversationMember'.",
        ),
        (
            "/teams/abc/channels/bad?$select=bogus",
            "Parsing OData Select and Expand failed: Could not find a property named 'bogus' on "
            "type 'microsoft.graph.channel'.",
        ),
        (
            "/teams/abc/channels/{channel}/messages/1111111111111?$top=1",
            "The query specified in the URI is not valid. Query option 'Top' is not allowed. To "
            "allow it, set the 'AllowedQueryOptions' property on EnableQueryAttribute or "
            "QueryValidationSettings.",
        ),
        ("/teams/abc/channels/{channel}/messages?$top=0", "teamId needs to be a valid GUID."),
        ("/teams/abc/channels/{channel}/messages/abc", "teamId needs to be a valid GUID."),
        ("/teams/{other}/channels?$filter=" + " or ".join(["displayName eq 'x'"] * 17), _NODES),
        (
            "/teams/{other}/channels/{channel}/members?$filter=roles/any(r:roles/any(s:s eq r))",
            _NESTING,
        ),
        ("/teams/{team}/channels/{missing}/messages?$skiptoken=", _RAW_VALUE),
        ("/teams/{team}/channels/{missing}/messages/abc/replies", "UnknownError"),
        ("/teams/{team}/channels/{channel}/messages/abc/replies?$top=51", _limit("Top", 50, "51")),
        (
            "/users/bogus/joinedTeams?$format=json",
            "The query specified in the URI is not valid. "
            "Query option 'Format' is not allowed. To allow it, set the 'AllowedQueryOptions' "
            "property on EnableQueryAttribute or QueryValidationSettings.",
        ),
    ],
)
def test_a_query_is_refused_before_what_it_names_is_looked_up(
    client, admin_h, team_id, path, message
):
    """Graph reads the query before the form of the team id, and that before the team, channel,
    message or person; a method's own refusals, such as the listing's `$top`, come after the id's
    form."""
    url = path.format(
        team=team_id,
        other="00000000-0000-0000-0000-000000000000",
        channel=_channels(client, admin_h, team_id)["Platform"],
        missing="19:00000000000000000000000000000000@thread.tacv2",
    )
    assert _error(client.get(f"{V1}{url}", headers=admin_h), 400)["message"] == message


def test_a_channel_granted_to_the_org_is_standard_and_a_narrower_one_is_private(
    client, admin_h, team_id
):
    body = client.get(f"{V1}/teams/{team_id}/channels", headers=admin_h).json()
    by_name = {c["displayName"]: c for c in body["value"]}
    assert by_name["Platform"]["membershipType"] == "standard"
    assert by_name["People Confidential"]["membershipType"] == "private"


@pytest.mark.parametrize(
    "suffix,status,code,message,inner",
    [
        ("", 404, "NotFound", "NotFound", "1"),
        ("/members", 404, "NotFound", "NotFound", "1"),
        ("/messages", 410, "Gone", "UnknownError", None),
        ("/messages/1791860130491", 410, "Gone", "UnknownError", None),
        ("/messages/1791860130491/replies", 410, "Gone", "UnknownError", None),
    ],
)
def test_a_channel_the_team_does_not_have_is_answered_per_method(
    client, admin_h, team_id, suffix, status, code, message, inner
):
    missing = "19:00000000000000000000000000000000@thread.tacv2"
    err = _error(
        client.get(f"{V1}/teams/{team_id}/channels/{missing}{suffix}", headers=admin_h), status
    )
    assert (err["code"], err["message"], err["innerError"].get("code")) == (code, message, inner)
    if inner == "1":
        assert err["innerError"]["message"] == (
            f"LocationLookupFailed-Location lookup failed for thread {missing}"
        )


@pytest.mark.parametrize(
    "suffix,message,inner",
    [
        ("/channels", "No threadId found for TeamId {team}", "Unknown"),
        # A filter .NET cannot run is refused only once the team is found.
        ("/channels?$filter=isArchived gt true", "No threadId found for TeamId {team}", "Unknown"),
        ("/channels/{channel}", "Channel does not belong to Team.", None),
        ("/channels/{channel}/messages", "Group ID '{team}' is not found.", None),
    ],
)
def test_a_team_the_corpus_does_not_model_is_answered_per_method(
    client, admin_h, team_id, suffix, message, inner
):
    other = "00000000-0000-0000-0000-000000000000"
    channel = _channels(client, admin_h, team_id)["Platform"]
    path = suffix.format(channel=channel)
    err = _error(client.get(f"{V1}/teams/{other}{path}", headers=admin_h), 404)
    assert err["code"] == "NotFound"
    assert err["message"] == message.format(team=other)
    assert err["innerError"].get("code") == inner


@pytest.mark.parametrize(
    "suffix,status,message,inner",
    [
        ("", 403, "InsufficientPrivileges", "1"),
        ("/members", 403, "InsufficientPrivileges", "1"),
        ("/messages", 403, "UnknownError", None),
        ("/messages/{message}", 403, "UnknownError", None),
        ("/messages/{message}/replies", 403, "UnknownError", None),
        ("/messages/{message}/replies/1111111111111", 403, "UnknownError", None),
        # A page token on members is refused for the channel before it is read as a token.
        ("/members?$skiptoken=x", 403, "InsufficientPrivileges", "1"),
        # A message id that is not one, and a refused option, are 400s before the channel is.
        ("/messages/abc", 400, "UnknownError", None),
        (
            "?$top=1",
            400,
            "The query specified in the URI is not valid. Query option 'Top' is not "
            "allowed. To allow it, set the 'AllowedQueryOptions' property on EnableQueryAttribute or "
            "QueryValidationSettings.",
            None,
        ),
    ],
)
def test_a_private_channel_the_caller_is_not_in_is_listed_to_nobody_and_refused_by_id(
    client, admin_h, tokens_yaml, team_id, suffix, status, message, inner
):
    """The listing leaves it out, and every method that names it by id answers 403 — measured with
    a delegated token for a person outside the channel. Read as a channel or for its members, the
    refusal names the caller and the channel's roster."""
    confidential = _channels(client, admin_h, team_id)["People Confidential"]
    root = client.get(
        f"{V1}/teams/{team_id}/channels/{confidential}/messages", headers=admin_h
    ).json()["value"][0]["id"]
    bob = {"Authorization": f"Bearer {tok(tokens_yaml, 'bob@acme.com')}"}
    assert "People Confidential" not in _channels(client, bob, team_id)
    path = f"{V1}/teams/{team_id}/channels/{confidential}{suffix.format(message=root)}"
    err = _error(client.get(path, headers=bob), status)
    assert (err["message"], err["innerError"].get("code")) == (message, inner)
    if status == 403:
        assert err["code"] == "Forbidden"
    if inner == "1":
        assert err["innerError"]["message"] == (
            f"AclCheckFailed-The initiator 8:orgid:{synth.msteams_user_id('bob@acme.com')} is not "
            f"a member of the roster 92:{confidential.removeprefix('19:')} in the generic thread "
            f"{confidential}"
        )


# --- members ---------------------------------------------------------------------


def test_a_standard_channel_is_the_whole_team_and_a_private_one_its_readers(
    client, admin_h, tokens_yaml, team_id, ro_conn
):
    """A standard channel "inherits the list of members of the parent team" — everyone in the
    directory, whether or not they ever posted in it. A private one is the people granted it."""
    channels = _channels(client, admin_h, team_id)
    roster = set(store.all_user_emails(ro_conn))
    for name, expected in (
        ("Platform", roster),
        (
            "People Confidential",
            set(store.conversation_members_by_grant(ro_conn, "msteams", "People Confidential")),
        ),
    ):
        body = client.get(
            f"{V1}/teams/{team_id}/channels/{channels[name]}/members", headers=admin_h
        ).json()
        assert {m["email"] for m in body["value"]} == expected, name
        assert body["@odata.count"] == len(body["value"])
    # A scoped caller is a member of a standard channel whether or not they ever posted in it.
    bob = {"Authorization": f"Bearer {tok(tokens_yaml, 'bob@acme.com')}"}
    seen = client.get(
        f"{V1}/teams/{team_id}/channels/{channels['Platform']}/members", headers=bob
    ).json()
    assert "bob@acme.com" in {m["email"] for m in seen["value"]}


def test_a_member_is_shaped_and_identified_as_graph_sends_one(client, admin_h, team_id):
    platform = _channels(client, admin_h, team_id)["Platform"]
    base = f"{V1}/teams/{team_id}/channels/{platform}/members"
    member = client.get(base, headers=admin_h).json()["value"][0]
    assert list(member) == [
        "@odata.type",
        "id",
        "roles",
        "displayName",
        "visibleHistoryStartDateTime",
        "userId",
        "email",
        "tenantId",
    ]
    assert member["@odata.type"] == "#microsoft.graph.aadUserConversationMember"
    assert member["userId"] == synth.msteams_user_id(member["email"])
    assert base64.b64decode(member["id"]).decode() == (
        f"0##2##{member['tenantId']}##{platform}##{member['userId']}"
    )
    # `$select` keeps the polymorphic type a client deserializes by; `$top` pages nothing.
    selected = client.get(base, headers=admin_h, params={"$select": "displayName"}).json()
    assert all(set(m) == {"@odata.type", "displayName"} for m in selected["value"])
    for top in ("0", "1000"):
        assert len(client.get(base, headers=admin_h, params={"$top": top}).json()["value"]) == len(
            client.get(base, headers=admin_h).json()["value"]
        )
    filtered = client.get(
        base, headers=admin_h, params={"$filter": f"displayName eq '{member['displayName']}'"}
    ).json()
    assert [m["id"] for m in filtered["value"]] == [member["id"]]
    err = _error(client.get(base, headers=admin_h, params={"$select": "bogus"}), 400)
    assert err["message"] == (
        "Parsing OData Select and Expand failed: Could not find a property named 'bogus' on type "
        "'microsoft.graph.conversationMember'."
    )
    # A lambda over `roles` is read; nobody is an owner here, so nobody matches.
    owners = client.get(base, headers=admin_h, params={"$filter": "roles/any(r:r eq 'owner')"})
    assert owners.status_code == 200 and owners.json()["value"] == []


# --- messages --------------------------------------------------------------------


def _incident(client, admin_h, team_id, **params) -> tuple[str, dict]:
    incidents = _channels(client, admin_h, team_id)["Incidents"]
    base = f"{V1}/teams/{team_id}/channels/{incidents}/messages"
    roots = client.get(base, headers=admin_h, params=params).json()["value"]
    return base, next(m for m in roots if "502s" in m["body"]["content"])


def test_a_message_is_shaped_as_graph_sends_one(client, admin_h, team_id):
    base, root = _incident(client, admin_h, team_id)
    assert list(root) == [
        "id",
        "replyToId",
        "etag",
        "messageType",
        "createdDateTime",
        "lastModifiedDateTime",
        "lastEditedDateTime",
        "deletedDateTime",
        "subject",
        "summary",
        "chatId",
        "importance",
        "locale",
        "webUrl",
        "policyViolation",
        "eventDetail",
        "from",
        "body",
        "channelIdentity",
        "attachments",
        "mentions",
        "reactions",
    ]
    assert root["importance"] == "high"
    # A person's message without a subject carries the empty string, not null.
    assert root["subject"] == ""
    assert root["chatId"] is None  # a channel message is not a chat message
    assert root["from"]["user"] == {
        "@odata.type": "#microsoft.graph.teamworkUserIdentity",
        "id": synth.msteams_user_id("bob@acme.com"),
        "displayName": root["from"]["user"]["displayName"],
        "userIdentityType": "aadUser",
        "tenantId": root["from"]["user"]["tenantId"],
    }
    assert root["channelIdentity"]["teamId"] == team_id
    # The id is the creation time in epoch milliseconds, as it is live.
    stamp = datetime.strptime(root["createdDateTime"], "%Y-%m-%dT%H:%M:%S.%f%z")
    assert round(stamp.timestamp() * 1000) == int(root["id"])


def test_a_reaction_moves_the_messages_modified_time_and_etag(client, admin_h, team_id):
    """Live, the etag is the last-modified time in epoch ms: the id on an untouched message, and
    later once an edit or a reaction lands."""
    base, root = _incident(client, admin_h, team_id, **{"$expand": "replies"})
    assert root["reactions"] == [
        {
            "reactionType": "👍",
            "displayName": "Like",
            "reactionContentUrl": None,
            "createdDateTime": "2026-02-10T18:01:00.000Z",
            "user": {
                "application": None,
                "device": None,
                "user": {
                    "@odata.type": "#microsoft.graph.teamworkUserIdentity",
                    "id": synth.msteams_user_id("ava@acme.com"),
                    "displayName": None,  # a reactor's name is not sent
                    "userIdentityType": "aadUser",
                    "tenantId": root["from"]["user"]["tenantId"],
                },
            },
        }
    ]
    assert root["lastModifiedDateTime"] == "2026-02-10T18:01:00.000Z"
    assert root["etag"] == str(
        int(datetime(2026, 2, 10, 18, 1, tzinfo=timezone.utc).timestamp()) * 1000
    )
    edited = next(r for r in root["replies"] if r["body"]["contentType"] == "html")
    assert edited["lastModifiedDateTime"] == edited["lastEditedDateTime"] is not None
    untouched = next(r for r in root["replies"] if r["body"]["contentType"] == "text")
    assert untouched["lastEditedDateTime"] is None
    assert untouched["lastModifiedDateTime"] == untouched["createdDateTime"]
    assert untouched["etag"] == untouched["id"]


def test_a_channel_listing_returns_roots_only_and_replies_newest_first(client, admin_h, team_id):
    base, root = _incident(client, admin_h, team_id)
    roots = client.get(base, headers=admin_h).json()["value"]
    assert all(m["replyToId"] is None for m in roots)
    # An empty filter is none, and a blank page token the first page.
    for params in ({"$filter": ""}, {"$skiptoken": " "}):
        assert client.get(base, headers=admin_h, params=params).json()["value"] == roots
    replies = client.get(f"{base}/{root['id']}/replies", headers=admin_h).json()
    assert replies["@odata.count"] == len(replies["value"]) == 2
    assert all(r["replyToId"] == root["id"] for r in replies["value"])
    assert [r["id"] for r in replies["value"]] == sorted(
        (r["id"] for r in replies["value"]), reverse=True
    )


def test_a_reply_is_read_under_its_root_and_not_at_the_root_route(client, admin_h, team_id):
    base, root = _incident(client, admin_h, team_id)
    reply = client.get(f"{base}/{root['id']}/replies", headers=admin_h).json()["value"][0]
    one = client.get(f"{base}/{root['id']}/replies/{reply['id']}", headers=admin_h)
    assert one.status_code == 200, one.text
    assert {k: v for k, v in one.json().items() if k != "@odata.context"} == reply
    err = _error(client.get(f"{base}/{reply['id']}", headers=admin_h), 404)
    assert (err["code"], err["message"]) == ("NotFound", "UnknownError")
    # Under its root, a reply id that names nothing is a 403, one that is not an id a 400, and a
    # reply of another root a 404; `expand=replies` is refused without the root route's pointer.
    thread = f"{base}/{root['id']}/replies"
    for path, status, code, message in (
        (f"{thread}/1111111111111", 403, "Forbidden", "UnknownError"),
        (f"{thread}/abc", 400, "BadRequest", "UnknownError"),
        (f"{base}/1111111111111/replies/{reply['id']}", 404, "NotFound", "UnknownError"),
        # A root id that is not an unsigned 64-bit number is a 400 under `/replies` too.
        (f"{base}/abc/replies", 400, "BadRequest", "UnknownError"),
        (f"{base}/abc/replies/{reply['id']}", 400, "BadRequest", "UnknownError"),
        (f"{base}/18446744073709551616/replies", 400, "BadRequest", "UnknownError"),
        (f"{base}/99999999999999999999/replies/{reply['id']}", 400, "BadRequest", "UnknownError"),
        (
            f"{thread}/{reply['id']}?$expand=replies",
            400,
            "BadRequest",
            "Expand query parameter 'replies' is not supported for this request.",
        ),
    ):
        err = _error(client.get(path, headers=admin_h), status)
        assert (err["code"], err["message"]) == (code, message), path
    # An id that names no message at all is refused rather than not found, to a delegated caller.
    err = _error(client.get(f"{base}/9999999999999", headers=admin_h), 403)
    assert (err["code"], err["message"]) == ("Forbidden", "UnknownError")
    assert "code" not in err["innerError"]
    # Teams threads are one level deep: a reply's own replies are an empty collection.
    nested = client.get(f"{base}/{reply['id']}/replies", headers=admin_h).json()
    assert (nested["@odata.count"], nested["value"]) == (0, [])
    # A root id is read as a number, so leading zeros name the same root; past it, an empty thread.
    padded = client.get(f"{base}/000{root['id']}/replies", headers=admin_h).json()
    assert [r["id"] for r in padded["value"]] == [
        r["id"] for r in client.get(thread, headers=admin_h).json()["value"]
    ]
    empty = client.get(f"{base}/18446744073709551615/replies", headers=admin_h).json()
    assert empty["value"] == []
    zeros = "0" * 4301 + "1"
    assert client.get(f"{base}/{zeros}/replies", headers=admin_h).json()["value"] == []
    for path in (f"{base}/{zeros}", f"{thread}/{zeros}"):
        assert _error(client.get(path, headers=admin_h), 403)["message"] == "UnknownError"


def test_expand_replies_inlines_every_thread_and_counts_only_a_nonempty_one(
    client, admin_h, team_id
):
    base, root = _incident(client, admin_h, team_id, **{"$expand": "replies"})
    assert root["replies@odata.count"] == len(root["replies"]) == 2
    assert root["replies@odata.context"].endswith(f"/messages('{root['id']}')/replies")
    platform = _channels(client, admin_h, team_id)["Platform"]
    quiet = client.get(
        f"{V1}/teams/{team_id}/channels/{platform}/messages",
        headers=admin_h,
        params={"$expand": "Replies"},  # the option's value is read without regard to case
    ).json()["value"]
    assert quiet, "the sample corpus has a root with no replies"
    for m in quiet:
        assert m["replies"] == [] and "replies@odata.count" not in m
        assert "replies@odata.context" in m
    plain = client.get(base, headers=admin_h).json()["value"]
    assert all("replies" not in m for m in plain)


@pytest.mark.parametrize(
    "params,status,code,message",
    [
        (
            {"$top": "51"},
            400,
            "",
            "The query specified in the URI is not valid. The limit of "
            "'50' for Top query has been exceeded. The value from the incoming request is '51'.",
        ),
        (
            {"$top": "0"},
            400,
            "BadRequest",
            "PageSize needs to be more than 0. (Parameter 'pageSize')",
        ),
        (
            {"$top": "abc"},
            400,
            "BadRequest",
            "Invalid value 'abc' for $top query option found. "
            "The $top query option requires a non-negative integer value.",
        ),
        ({"$skiptoken": "garbage"}, 400, "BadRequest", "UnknownError"),
        (
            {"$filter": "messageType eq 'message'"},
            400,
            "BadRequest",
            "Parameter 'Filter' not supported for this request.",
        ),
        (
            {"$orderby": "createdDateTime"},
            400,
            "",
            "The query specified in the URI is not valid. "
            "Query option 'OrderBy' is not allowed. To allow it, set the 'AllowedQueryOptions' "
            "property on EnableQueryAttribute or QueryValidationSettings.",
        ),
        (
            {"$expand": "bogus"},
            400,
            "BadRequest",
            "Parsing OData Select and Expand failed: Could "
            "not find a property named 'bogus' on type 'microsoft.graph.chatMessage'.",
        ),
        (
            {"$select": "bogus"},
            400,
            "BadRequest",
            "Parsing OData Select and Expand failed: Could "
            "not find a property named 'bogus' on type 'microsoft.graph.chatMessage'.",
        ),
        (
            {"$top": "²"},
            400,
            "BadRequest",
            "Invalid value '²' for $top query option found. The $top query option requires a "
            "non-negative integer value.",
        ),
        (
            {"$top": "-0"},
            400,
            "BadRequest",
            "PageSize needs to be more than 0. (Parameter 'pageSize')",
        ),
        (
            {"$TOP": "51"},
            400,
            "",
            "The query specified in the URI is not valid. The limit of '50' for Top query has "
            "been exceeded. The value from the incoming request is '51'.",
        ),
    ],
)
def test_a_message_listing_refuses_a_query_it_cannot_run(
    client, admin_h, team_id, params, status, code, message
):
    base, _ = _incident(client, admin_h, team_id)
    err = _error(client.get(base, headers=admin_h, params=params), status)
    assert (err["code"], err["message"]) == (code, message)


def test_a_replies_page_token_that_does_not_decode_is_a_backend_failure(client, admin_h, team_id):
    base, root = _incident(client, admin_h, team_id)
    members = base.removesuffix("/messages") + "/members"
    err = _error(client.get(members, headers=admin_h, params={"$skiptoken": "x"}), 502)
    assert err["code"] == "BadGateway"
    for key in ("$skiptoken", "$skipToken"):
        err = _error(
            client.get(f"{base}/{root['id']}/replies", headers=admin_h, params={key: "garbage"}),
            502,
        )
        assert (err["code"], err["message"]) == ("BadGateway", "Failed to execute backend request.")


def test_select_projects_a_message_to_what_was_named(client, admin_h, team_id):
    base, root = _incident(client, admin_h, team_id)
    body = client.get(base, headers=admin_h, params={"$select": "id"}).json()
    assert all(set(m) == {"id"} for m in body["value"])
    one = client.get(f"{base}/{root['id']}", headers=admin_h, params={"$select": "id"}).json()
    assert one["@odata.context"].endswith("/messages(id)/$entity")
    assert set(one) == {"@odata.context", "id"}


# --- paging and ordering ---------------------------------------------------------


def test_skip_skips_roots_and_drops_replies_from_the_page_top_reads(client, admin_h, team_id):
    """On a reply listing, `$skip` drops that many from the `$top` page and never reaches past it,
    so a skip as long as the page leaves it empty, with no link onward."""
    base, root = _incident(client, admin_h, team_id)
    platform = f"{V1}/teams/{team_id}/channels/{_channels(client, admin_h, team_id)['Platform']}"
    roots = client.get(f"{platform}/messages", headers=admin_h).json()["value"]
    skipped = client.get(f"{platform}/messages?$skip=1", headers=admin_h).json()["value"]
    assert [m["id"] for m in skipped] == [m["id"] for m in roots[1:]]
    last = encode_cursor(2**63 - 1)
    past = client.get(f"{platform}/messages?$skiptoken={last}&$skip=1", headers=admin_h).json()
    assert past["value"] == []
    thread = f"{base}/{root['id']}/replies"
    every = client.get(thread, headers=admin_h).json()["value"]
    replies = client.get(f"{thread}?$skip=1", headers=admin_h).json()
    assert [r["id"] for r in replies["value"]] == [r["id"] for r in every[1:]]
    assert replies["@odata.count"] == len(every) - 1 and "@odata.nextLink" not in replies
    first = client.get(f"{thread}?$top=1", headers=admin_h).json()
    assert "@odata.nextLink" in first
    none = client.get(f"{thread}?$top=1&$skip=1", headers=admin_h).json()
    assert none["value"] == [] and "@odata.nextLink" not in none


def test_a_page_counts_itself_and_the_next_link_walks_the_channel(
    client, admin_h, team_id, ro_conn
):
    base, _ = _incident(client, admin_h, team_id)
    url, seen, pages = f"{base}?$top=1", [], 0
    while url and pages < 10:
        body = client.get(url, headers=admin_h).json()
        assert body["@odata.count"] == len(body["value"])
        seen += [m["id"] for m in body["value"]]
        url = body.get("@odata.nextLink")
        pages += 1
    assert url is None, "paging did not terminate"
    assert len(seen) == len(set(seen)) == store.count_msteams_top_level(ro_conn, "Incidents", None)


def test_a_crawl_reaches_every_stored_message(client, admin_h, team_id, ro_conn):
    """Roots come from the channel listing and replies from the relationship, so the two together
    have to account for every row — a message reachable by neither is one no client can read."""
    found = set()
    for channel_id in _channels(client, admin_h, team_id).values():
        base = f"{V1}/teams/{team_id}/channels/{channel_id}/messages"
        url = f"{base}?$top=50"
        while url:
            body = client.get(url, headers=admin_h).json()
            for m in body["value"]:
                found.add(m["id"])
                replies = client.get(f"{base}/{m['id']}/replies", headers=admin_h).json()
                found.update(r["id"] for r in replies["value"])
            url = body.get("@odata.nextLink")
    assert len(found) == db_count(ro_conn, "msteams")


@pytest.mark.parametrize(
    "activity",
    ["reply", "edited reply", "edited root", "reaction", "reaction on a reply"],
)
def test_a_channel_is_ordered_by_its_reply_chains_newest_activity(tmp_path, activity):
    """A new reply to the oldest root moved it to the top live, while the root's own
    `lastModifiedDateTime` did not change; an edit or a reaction on any message of the chain moves
    that message's, and so the chain's."""
    old = complete("msteams", doc_id="old", channel="Platform", content="Old", created=100)
    reply = {"content": "Reply", "author_email": "bo@x.com", "created": 150}
    if activity == "reply":
        reply["created"] = 300
    if activity == "edited reply":
        reply["edited"] = 300
    if activity == "edited root":
        old["edited"] = 300
    if activity == "reaction":
        old["reactions"] = [{"reactionType": "👍", "user": "bo@x.com", "createdDateTime": 300}]
    if activity == "reaction on a reply":
        reply["reactions"] = [{"reactionType": "👍", "user": "ava@x.com", "createdDateTime": 300}]
    old["replies"] = [reply]
    newer = complete("msteams", doc_id="newer", channel="Platform", content="Newer", created=200)
    with corpus_client(tmp_path, [old, newer]) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=h).json()["value"][0]["id"]
        cid = synth.msteams_channel_id("Platform")
        roots = client.get(f"{V1}/teams/{team}/channels/{cid}/messages", headers=h).json()["value"]
    assert [m["body"]["content"] for m in roots] == ["Old", "Newer"]
    first = roots[0]
    moved = activity in ("edited root", "reaction")
    assert (first["lastModifiedDateTime"] == "1970-01-01T00:05:00.000Z") == moved


def test_a_sender_id_from_a_message_resolves_at_the_users_route(tmp_path):
    """An id the API hands out has to resolve, a bot's that the directory does not carry included."""
    record = complete(
        "msteams",
        channel="Platform",
        author_email="ava@x.com",
        replies=[{"content": "Artifacts uploaded.", "author_email": "buildbot@x.com"}],
    )
    with corpus_client(tmp_path, [record]) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=h).json()["value"][0]["id"]
        base = f"{V1}/teams/{team}/channels/{_channels(client, h, team)['Platform']}/messages"
        root = client.get(base, headers=h).json()["value"][0]
        reply = client.get(f"{base}/{root['id']}/replies", headers=h).json()["value"][0]
        resolved = client.get(f"{V1}/users/{reply['from']['user']['id']}", headers=h)
    assert resolved.status_code == 200
    assert resolved.json()["userPrincipalName"] == "buildbot@x.com"


def test_a_reaction_without_its_own_time_takes_the_messages(tmp_path):
    record = complete(
        "msteams",
        channel="Platform",
        author_email="ava@x.com",
        reactions=[{"reactionType": "🎉", "user": "bo@x.com"}],
    )
    with corpus_client(tmp_path, [record]) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=h).json()["value"][0]["id"]
        url = f"{V1}/teams/{team}/channels/{_channels(client, h, team)['Platform']}/messages"
        m = client.get(url, headers=h).json()["value"][0]
    reaction = m["reactions"][0]
    assert reaction["createdDateTime"] == m["createdDateTime"] == m["lastModifiedDateTime"]
    assert (reaction["reactionType"], reaction["displayName"]) == ("🎉", "Party popper")


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.parametrize(
    "reader,visible", [("org:other", False), ("org:acme", True), ("user:bob@acme.com", True)]
)
def test_channel_visibility_intersects_the_callers_own_principals(tmp_path, warm, reader, visible):
    """An org grant to some OTHER org does not make a channel public to this caller, whether the
    channel ACL cache is warm or not."""
    records = [
        complete("msteams", channel="restricted", author_email="bob@acme.com", readers=[reader])
    ]
    with corpus_client(tmp_path, records) as (client, settings):
        client.app.state.warm_thread.join()
        assert client.app.state.warm_error is None
        if not warm:
            client.app.state.msteams_channel_acl = None
        token = client.app.state.acl.email_to_token()["bob@acme.com"]
        headers = {"Authorization": f"Bearer {token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=headers).json()["value"][0]["id"]
        cid = synth.msteams_channel_id("restricted")
        base = f"{V1}/teams/{team}/channels"
        listed = client.get(base, headers=headers).json()
        assert [row["id"] for row in listed["value"]] == ([cid] if visible else [])
        for suffix in ("", "/members", "/messages"):
            r = client.get(f"{base}/{cid}{suffix}", headers=headers)
            assert r.status_code == (200 if visible else 403), r.text


def test_mentions_and_attachments_are_built_from_what_the_corpus_names(tmp_path):
    record = complete(
        "msteams",
        channel="Platform",
        content='<p><at id="0">Bo</at> see <attachment id="a1"></attachment></p>',
        content_type="html",
        author_email="ava@x.com",
        mentions=[{"user": "bo@x.com", "mentionText": "Bo"}],
        attachments=[{"id": "a1", "name": "plan.pdf", "contentUrl": "https://x.sharepoint.com/p"}],
    )
    with corpus_client(tmp_path, [record]) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=h).json()["value"][0]["id"]
        channel = _channels(client, h, team)["Platform"]
        m = client.get(f"{V1}/teams/{team}/channels/{channel}/messages", headers=h).json()["value"][
            0
        ]
    assert m["mentions"] == [
        {
            "id": 0,
            "mentionText": "Bo",
            "mentioned": {
                "application": None,
                "device": None,
                "conversation": None,
                "tag": None,
                "user": {
                    "@odata.type": "#microsoft.graph.teamworkUserIdentity",
                    "id": synth.msteams_user_id("bo@x.com"),
                    "displayName": m["mentions"][0]["mentioned"]["user"]["displayName"],
                    "userIdentityType": "aadUser",
                    "tenantId": m["from"]["user"]["tenantId"],
                },
            },
        }
    ]
    assert m["attachments"] == [
        {
            "id": "a1",
            "contentType": "reference",
            "contentUrl": "https://x.sharepoint.com/p",
            "content": None,
            "name": "plan.pdf",
            "thumbnailUrl": None,
            "teamsAppId": None,
        }
    ]


def test_a_system_event_is_an_unknown_future_value_unless_the_caller_opts_in(tmp_path):
    record = complete(
        "msteams",
        channel="Platform",
        message_type="systemEventMessage",
        content="Raj joined the team.",
        author_email="ava@x.com",
    )
    with corpus_client(tmp_path, [record]) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=h).json()["value"][0]["id"]
        url = f"{V1}/teams/{team}/channels/{_channels(client, h, team)['Platform']}/messages"
        plain = client.get(url, headers=h).json()["value"][0]
        opted = client.get(url, headers={**h, "Prefer": "include-unknown-enum-members"}).json()[
            "value"
        ][0]
    assert plain["messageType"] == "unknownFutureValue"
    assert opted["messageType"] == "systemEventMessage"
    for m in (plain, opted):
        assert m["from"] is None
        assert m["subject"] is None
        assert m["body"] == {"contentType": "html", "content": "<systemEventMessage/>"}


def test_an_expanded_thread_past_200_continues_at_the_link_graph_gives(tmp_path):
    record = complete(
        "msteams",
        channel="Platform",
        created=100,
        replies=[{"content": f"Reply {i}", "created": 101 + i} for i in range(205)],
    )
    with corpus_client(tmp_path, [record]) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=h).json()["value"][0]["id"]
        cid = synth.msteams_channel_id("Platform")
        root = client.get(
            f"{V1}/teams/{team}/channels/{cid}/messages", headers=h, params={"$expand": "replies"}
        ).json()["value"][0]
        assert root["replies@odata.count"] == len(root["replies"]) == 200
        link = root["replies@odata.nextLink"]
        assert link.startswith(
            f"http://testserver{V1}/teams('{team}')/channels('{cid}')/messages('{root['id']}')"
            "/replies?$skipToken="
        )
        page = client.get(link, headers=h).json()
        assert page["@odata.count"] == len(page["value"]) == 5
        assert "@odata.nextLink" not in page
    replies = root["replies"] + page["value"]
    assert [r["body"]["content"] for r in replies] == [f"Reply {i}" for i in reversed(range(205))]


def test_the_directory_sorts_by_name_or_address_in_either_direction(tmp_path):
    """The default order is by userPrincipalName; `$orderby` sorts by `displayName` or
    `userPrincipalName`, named in any case, ascending or descending."""
    records = [
        complete("msteams", channel="Platform", author_email="zed@x.com", author_name="Abe Zed"),
        complete("msteams", channel="Platform", author_email="amy@x.com", author_name="Zoe Amy"),
    ]
    with corpus_client(tmp_path, records) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}

        def order(query=""):
            body = client.get(f"{V1}/users{query}", headers=h).json()
            return [u["userPrincipalName"] for u in body["value"]]

        assert order() == ["amy@x.com", "zed@x.com"]
        assert order("?$orderby=displayName") == ["zed@x.com", "amy@x.com"]
        assert order("?$orderby=DisplayName desc") == ["amy@x.com", "zed@x.com"]
        assert order("?$orderby=userPrincipalName desc") == ["zed@x.com", "amy@x.com"]
