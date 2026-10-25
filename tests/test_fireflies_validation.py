"""Measured person-filter refusals, exercised through a local listening HTTP server."""

from __future__ import annotations

import json
import re

import httpx
import pytest

import backlot


@pytest.fixture(scope="module")
def fireflies_http():
    with (
        backlot.serve() as server,
        httpx.Client(
            base_url=server.base_url, headers={"Authorization": f"Bearer {server.token}"}
        ) as client,
    ):
        response = client.post(
            "/fireflies/graphql", json={"query": "{ transcripts(limit: 1) { host_email } }"}
        )
        host = response.json()["data"]["transcripts"][0]["host_email"]
        yield client, host


def _query(fixture, arguments, selection="id"):
    client, host = fixture
    args = arguments.replace("HOST", json.dumps(host))
    return client.post(
        "/fireflies/graphql", json={"query": "{ transcripts(%s) { %s } }" % (args, selection)}
    )


_SINGULAR_MESSAGE = (
    "Only one of the following fields can be provided: "
    "mine, userId, hostEmail, organizerEmail, participantEmail."
)
_SCOPE_ENUM = ("isEnum", "scope must be one of the following values: title, sentences, all")
_SCOPE_KEYWORD = ("conditionalRequired", "keyword is required when scope is provided")
_DATE = "date must be a valid timestamp in milliseconds"
_NUMBER = ("isNumber", "date must be a number conforming to the specified constraints")


def _field(name, *constraints):
    return {
        "name": name,
        "message": constraints[0][1],
        "constraints": [{"type": kind, "message": message} for kind, message in constraints],
    }


def _exclusive(name):
    return _field(name, _excl(name))


def _excl(name):
    message = _SINGULAR_MESSAGE
    if name in ("organizers", "participants"):
        message = message[:-1] + f", {name}."
    return ("exclusiveFields", message)


def _email(name):
    return ("IsEmailOnly", f"{name} must be a valid email address")


def _assert_invalid(response, fields, path="transcripts"):
    assert response.status_code == 200
    body = response.json()
    assert body["data"][path] is None
    assert len(body["errors"]) == 1
    error = body["errors"][0]
    assert list(error) == ["friendly", "message", "locations", "path", "code", "extensions"]
    assert error["message"] == "Invalid argument(s) were provided"
    assert error["friendly"] is True
    assert error["code"] == "invalid_arguments"
    assert error["locations"] == [{"line": 1, "column": 3}]
    assert error["path"] == [path]
    extensions = error["extensions"]
    assert re.fullmatch(r"[A-Za-z0-9]{8}", extensions.pop("correlationId"))
    assert extensions == {
        "code": "invalid_arguments",
        "status": 400,
        "metadata": {"fields": fields},
        "helpUrls": ["https://docs.fireflies.ai/miscellaneous/error-codes#invalid_arguments"],
    }
    return body


@pytest.mark.parametrize(
    "arguments,names",
    [
        ('mine: true, user_id: "unknown"', ["userId", "mine"]),
        ('host_email: HOST, user_id: "unknown"', ["userId", "hostEmail"]),
        ("host_email: HOST, participant_email: HOST", ["hostEmail", "participantEmail"]),
        ("organizer_email: HOST, participant_email: HOST", ["organizerEmail", "participantEmail"]),
        (
            "host_email: HOST, organizer_email: HOST, participant_email: HOST",
            ["hostEmail", "organizerEmail", "participantEmail"],
        ),
        ("host_email: HOST, organizers: [HOST]", ["organizers"]),
        ("host_email: HOST, participants: [HOST]", ["participants"]),
        ("organizer_email: HOST, organizers: [HOST]", ["organizers"]),
        ("participant_email: HOST, participants: [HOST]", ["participants"]),
        ("mine: true, organizers: [HOST]", ["organizers"]),
        ('user_id: "unknown", participants: [HOST]', ["participants"]),
        (
            "host_email: HOST, organizers: [HOST], participants: [HOST]",
            ["organizers", "participants"],
        ),
        ("host_email: HOST, organizers: []", ["organizers"]),
        ('host_email: HOST, mine: false, user_id: "unknown"', ["userId", "mine", "hostEmail"]),
        ('host_email: "", mine: true, user_id: "unknown"', ["userId", "mine", "hostEmail"]),
        (
            'organizer_email: "", host_email: HOST, mine: true',
            ["mine", "hostEmail", "organizerEmail"],
        ),
        (
            'organizers: [HOST], participants: [HOST], user_id: "unknown"',
            ["organizers", "participants"],
        ),
    ],
)
def test_fireflies_measured_filter_collision(fireflies_http, arguments, names):
    _assert_invalid(
        _query(fireflies_http, arguments + ", limit: 50"), [_exclusive(n) for n in names]
    )


@pytest.mark.parametrize(
    "arguments,reference",
    [
        ("organizers: [HOST], participants: [HOST]", "host_email: HOST"),
        ("host_email: HOST, mine: false", "host_email: HOST"),
        ("host_email: null, mine: true", "mine: true"),
        ('host_email: "", mine: true', "mine: true"),
        ('host_email: HOST, organizer_email: ""', "host_email: HOST"),
        ("mine: false, organizers: [HOST]", "organizers: [HOST]"),
        ('host_email: "", organizers: [HOST]', "organizers: [HOST]"),
        ('user_id: "", mine: true', "mine: true"),
    ],
)
def test_fireflies_measured_filter_controls(fireflies_http, arguments, reference):
    response = _query(fireflies_http, arguments + ", limit: 50")
    assert response.status_code == 200
    assert "errors" not in response.json()
    assert response.json() == _query(fireflies_http, reference + ", limit: 50").json()


_ALL_PERSON = (
    'organizers: ["nope"], participants: ["nope"], user_id: "u", mine: true, host_email: "nope", '
    'organizer_email: "nope", participant_email: "nope"'
)


@pytest.mark.parametrize(
    "arguments,fields",
    [
        (
            "host_email: HOST, mine: true, limit: 0",
            [
                _field("limit", ("isPositive", "limit must be a positive number")),
                _exclusive("mine"),
                _exclusive("hostEmail"),
            ],
        ),
        (
            'host_email: "nope", mine: true',
            [_exclusive("mine"), _field("hostEmail", _excl("hostEmail"), _email("hostEmail"))],
        ),
        (
            'toDate: "x", fromDate: "x", skip: -1, limit: 0',
            [
                _field("fromDate", ("isDate", "fromDate must be a Date instance")),
                _field("toDate", ("isDate", "toDate must be a Date instance")),
                _field("limit", ("isPositive", "limit must be a positive number")),
                _field("skip", ("min", "skip must not be less than 0")),
            ],
        ),
        (
            'host_email: "nope", organizers: [HOST]',
            [_exclusive("organizers"), _field("hostEmail", _email("hostEmail"))],
        ),
        (
            'organizers: ["nope"], host_email: HOST',
            [_field("organizers", _excl("organizers"), _email("organizers"))],
        ),
        (
            _ALL_PERSON,
            [_exclusive("userId"), _exclusive("mine")]
            + [
                _field(name, _excl(name), _email(name))
                for name in (
                    "organizers",
                    "participants",
                    "hostEmail",
                    "organizerEmail",
                    "participantEmail",
                )
            ],
        ),
        (
            'scope: "bogus", fromDate: "x", limit: 0, mine: true, user_id: "x"',
            [
                _field("scope", _SCOPE_KEYWORD, _SCOPE_ENUM),
                _field("fromDate", ("isDate", "fromDate must be a Date instance")),
                _field("limit", ("isPositive", "limit must be a positive number")),
                _exclusive("userId"),
                _exclusive("mine"),
            ],
        ),
        ("date: 1e400", [_field("date", ("max", _DATE), _NUMBER)]),
        ("date: -1e400", [_field("date", ("min", _DATE), _NUMBER)]),
        (
            "limit: 0, date: 1e308",
            [
                _field("date", ("max", _DATE)),
                _field("limit", ("isPositive", "limit must be a positive number")),
            ],
        ),
        (
            'date: 1e308, toDate: "x"',
            [
                _field("date", ("max", _DATE)),
                _field("toDate", ("isDate", "toDate must be a Date instance")),
            ],
        ),
        (
            'date: 1e400, mine: true, user_id: "u"',
            [_field("date", ("max", _DATE), _NUMBER), _exclusive("userId"), _exclusive("mine")],
        ),
        (
            'scope: "bogus", keyword: "k", fromDate: "x", toDate: "x", date: 1e308, limit: 0, skip: -1',
            [
                _field("scope", _SCOPE_ENUM),
                _field("date", ("max", _DATE)),
                _field("fromDate", ("isDate", "fromDate must be a Date instance")),
                _field("toDate", ("isDate", "toDate must be a Date instance")),
                _field("limit", ("isPositive", "limit must be a positive number")),
                _field("skip", ("min", "skip must not be less than 0")),
            ],
        ),
        (
            "date: 1e15, limit: 0",
            [_field("limit", ("isPositive", "limit must be a positive number"))],
        ),
    ],
)
def test_fireflies_measured_compound_refusal(fireflies_http, arguments, fields):
    _assert_invalid(_query(fireflies_http, arguments), fields)


@pytest.mark.parametrize(
    "arguments,name,kind,message",
    [
        ("limit: 0", "limit", "isPositive", "limit must be a positive number"),
        ("limit: -1", "limit", "isPositive", "limit must be a positive number"),
        ("limit: 51", "limit", "max", "limit must not be greater than 50"),
        ("skip: -1", "skip", "min", "skip must not be less than 0"),
        ("skip: 5001", "skip", "max", "skip must not be greater than 5000"),
        ('fromDate: "not-a-date"', "fromDate", "isDate", "fromDate must be a Date instance"),
        ('fromDate: "1700000000000"', "fromDate", "isDate", "fromDate must be a Date instance"),
        ('fromDate: "13"', "fromDate", "isDate", "fromDate must be a Date instance"),
        ('fromDate: "275761"', "fromDate", "isDate", "fromDate must be a Date instance"),
        ('fromDate: ""', "fromDate", "isDate", "fromDate must be a Date instance"),
        ('fromDate: "Jan"', "fromDate", "isDate", "fromDate must be a Date instance"),
        ('fromDate: "\\uff11\\uff12"', "fromDate", "isDate", "fromDate must be a Date instance"),
        pytest.param(
            f'fromDate: "{"9" * 4301}"',
            "fromDate",
            "isDate",
            "fromDate must be a Date instance",
            id="fromDate-4301-digits",
        ),
        ('toDate: "not-a-date"', "toDate", "isDate", "toDate must be a Date instance"),
        (
            'organizers: ["nope"]',
            "organizers",
            "IsEmailOnly",
            "organizers must be a valid email address",
        ),
        (
            'participants: ["a@b.co", "nope"]',
            "participants",
            "IsEmailOnly",
            "participants must be a valid email address",
        ),
        (
            'host_email: "nope"',
            "hostEmail",
            "IsEmailOnly",
            "hostEmail must be a valid email address",
        ),
        (
            'organizer_email: "nope"',
            "organizerEmail",
            "IsEmailOnly",
            "organizerEmail must be a valid email address",
        ),
        (
            'participant_email: "nope"',
            "participantEmail",
            "IsEmailOnly",
            "participantEmail must be a valid email address",
        ),
        ("date: 8.64e15", "date", "max", _DATE),
        ("date: 8640000000000001", "date", "max", _DATE),
        ("date: 1e308", "date", "max", _DATE),
        ("date: -8640000000000001", "date", "min", _DATE),
        ("date: -1e308", "date", "min", _DATE),
    ],
)
def test_fireflies_supporting_measured_validation(fireflies_http, arguments, name, kind, message):
    _assert_invalid(_query(fireflies_http, arguments), [_field(name, (kind, message))])


@pytest.mark.parametrize(
    "value,refused",
    [
        ("a@b.co", False),
        ('"a b"@b.co', False),
        ("ü@b.co", False),
        ("a@ü.co", False),
        ("a@b.xn--p1ai", False),
        ("x" * 64 + "@b.co", False),
        ("x" * 65 + "@b.co", True),
        ("a@" + "b" * 63 + ".co", False),
        ("a@" + "b" * 64 + ".co", True),
        ("a@b.c", True),
        ("a@b.12", True),
        ("someone@example.com", False),
        ("a@b.co1", True),
        ("a..b@b.co", True),
        (".a@b.co", True),
        ("a@b.co.", True),
        ("a@-b.co", True),
        ("a@b_c.co", True),
        ("a@127.0.0.1", True),
        ("a@localhost", True),
        ("a@b!c.co", True),
        ("a@b-.co", True),
        ('"a@b.co', True),
        ('a"@b.co', True),
        ('"a b"@b.c', True),
        ("a@b.co\u3000m", True),
        ("a@b.co\ufeffm", True),
        ("a@\uff42.co", True),
        pytest.param(
            "x" * 64 + "@" + ".".join(["b" * 63, "b" * 63, "b" * 58, "co"]), False, id="254"
        ),
        pytest.param(
            "x" * 64 + "@" + ".".join(["b" * 63, "b" * 63, "b" * 60, "co"]), True, id="256"
        ),
        pytest.param("a@" + ".".join(["ü" * 40] * 3) + ".co", False, id="domain-245-bytes"),
        pytest.param("a@" + ".".join(["ü" * 60] * 3) + ".co", True, id="domain-365-bytes"),
    ],
)
def test_fireflies_measured_email_spelling(fireflies_http, value, refused):
    response = _query(fireflies_http, f"host_email: {json.dumps(value)}")
    if refused:
        _assert_invalid(response, [_field("hostEmail", _email("hostEmail"))])
    else:
        assert response.json() == {"data": {"transcripts": []}}


def test_fireflies_alias_partial_data(fireflies_http):
    client, host = fireflies_http
    response = client.post(
        "/fireflies/graphql",
        json={"query": "{ bad: transcripts(limit: 0) { id } good: transcripts(limit: 1) { id } }"},
    )
    body = _assert_invalid(
        response, [_field("limit", ("isPositive", "limit must be a positive number"))], "bad"
    )
    assert len(body["data"]["good"]) == 1


@pytest.mark.parametrize(
    "key,side",
    [
        ("host_email", "before"),
        ("host_email", "after"),
        ("organizer_email", "before"),
        ("participant_email", "before"),
        ("participant_email", "after"),
    ],
)
def test_fireflies_measured_email_whitespace(fireflies_http, key, side):
    _client, host = fireflies_http
    value = " " + host if side == "before" else host + " "
    name = {
        "host_email": "hostEmail",
        "organizer_email": "organizerEmail",
        "participant_email": "participantEmail",
    }[key]
    _assert_invalid(
        _query(fireflies_http, f"{key}: {json.dumps(value)}"),
        [_field(name, ("IsEmailOnly", f"{name} must be a valid email address"))],
    )


@pytest.mark.parametrize(
    "arguments",
    [
        "limit: 50",
        "skip: 5000",
        'fromDate: "2020-01-01"',
        'fromDate: "2020-01-01T00:00:00Z"',
        "fromDate: 1700000000000",
        'fromDate: "2020"',
        'fromDate: "0"',
        'fromDate: "00"',
        'fromDate: "0013"',
        'fromDate: "32"',
        'fromDate: "Jan 1 2020"',
        'fromDate: "Wed, 01 Jan 2020 00:00:00 GMT"',
        "fromDate: true",
        'toDate: "2020-01-01"',
        'organizers: ["\\"a b\\"@b.co"]',
        "date: 253402300799999",
        "date: 253402300799999.9",
        "date: -62167219200000",
        "date: -62167219200000.9",
    ],
)
def test_fireflies_measured_validation_boundaries(fireflies_http, arguments):
    response = _query(fireflies_http, arguments)
    assert response.status_code == 200
    body = response.json()
    assert "errors" not in body
    if arguments == "skip: 5000":
        assert body["data"]["transcripts"] == []
    else:
        assert isinstance(body["data"]["transcripts"], list)


@pytest.mark.parametrize(
    "arguments",
    [
        "date: 253402300800000",
        "date: 8639999999999999",
        "date: -62167219200001",
        "date: -8.64e15",
        'date: 1e15, fromDate: "2020-01-01"',
    ],
)
def test_fireflies_date_outside_the_years_0_to_9999_is_an_internal_error(fireflies_http, arguments):
    response = _query(fireflies_http, arguments)
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["transcripts"] is None
    (error,) = body["errors"]
    assert list(error) == ["friendly", "message", "locations", "path", "code", "extensions"]
    assert list(error["extensions"]) == ["code", "correlationId", "status"]
    assert re.fullmatch(r"[A-Za-z0-9]{8}", error["extensions"].pop("correlationId"))
    assert error == {
        "friendly": False,
        "message": "Something unexpected happened. Please try again",
        "locations": [{"line": 1, "column": 3}],
        "path": ["transcripts"],
        "code": "INTERNAL_SERVER_ERROR",
        "extensions": {"code": "INTERNAL_SERVER_ERROR", "status": 500},
    }


def test_fireflies_validation_preserves_scoped_acl(live_server, tokens):
    base_url, _settings = live_server
    with httpx.Client(
        base_url=base_url, headers={"Authorization": f"Bearer {tokens['ava@acme.com']}"}
    ) as client:
        fixture = client, "ava@acme.com"
        baseline = _query(fixture, "limit: 50").json()["data"]["transcripts"]
        assert baseline
        response = client.post(
            "/fireflies/graphql",
            json={
                "query": '{ bad: transcripts(host_email: "ava@acme.com", mine: true) { id } good: transcripts(limit: 50) { id } }'
            },
        )
        body = _assert_invalid(response, [_exclusive("mine"), _exclusive("hostEmail")], "bad")
        assert body["data"]["good"] == baseline


@pytest.mark.parametrize(
    "query",
    [
        "{ a: transcripts(limit: 0) { id } b: transcripts(skip: -1) { id } }",
        "{ a: transcripts(limit: 0) { id } b: transcripts(date: 1e15) { id } }",
    ],
)
def test_fireflies_errors_in_one_response_share_one_correlation_id(fireflies_http, query):
    client, _host = fireflies_http
    body = client.post("/fireflies/graphql", json={"query": query}).json()
    assert len(body["errors"]) == 2
    assert len({error["extensions"]["correlationId"] for error in body["errors"]}) == 1


@pytest.mark.parametrize(
    "arguments,constraints",
    [
        ('scope: "title"', [_SCOPE_KEYWORD]),
        ('scope: "sentences"', [_SCOPE_KEYWORD]),
        ('scope: "all"', [_SCOPE_KEYWORD]),
        ('scope: "title", keyword: null', [_SCOPE_KEYWORD]),
        ('scope: "title", keyword: ""', [_SCOPE_KEYWORD]),
        ('scope: "title", keyword: " \\t\\n"', [_SCOPE_KEYWORD]),
        ('scope: "title", keyword: "\\u00a0"', [_SCOPE_KEYWORD]),
        ('scope: "title", keyword: "\\ufeff"', [_SCOPE_KEYWORD]),
        ('scope: "title", keyword: "\\u001c"', []),
        ('scope: "title", keyword: "\\u0085"', []),
        ('scope: "all", keyword: "k"', []),
        ('scope: "TITLE", keyword: "k"', [_SCOPE_ENUM]),
        ('scope: "all ", keyword: "k"', [_SCOPE_ENUM]),
        ('scope: "", keyword: "k"', [_SCOPE_ENUM]),
        ('scope: ""', [_SCOPE_ENUM]),
        ('scope: "bogus", keyword: "k"', [_SCOPE_ENUM]),
        ('scope: "bogus"', [_SCOPE_KEYWORD, _SCOPE_ENUM]),
        ('scope: " "', [_SCOPE_KEYWORD, _SCOPE_ENUM]),
        ("scope: null", []),
        ('keyword: ""', []),
    ],
)
def test_fireflies_measured_scope_and_keyword(fireflies_http, arguments, constraints):
    response = _query(fireflies_http, arguments)
    if constraints:
        _assert_invalid(response, [_field("scope", *constraints)])
    else:
        assert "errors" not in response.json()
        assert isinstance(response.json()["data"]["transcripts"], list)
