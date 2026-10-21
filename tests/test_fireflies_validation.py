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


def _field(name, *constraints):
    return {
        "name": name,
        "message": constraints[0][1],
        "constraints": [{"type": kind, "message": message} for kind, message in constraints],
    }


def _exclusive(name):
    message = _SINGULAR_MESSAGE
    if name in ("organizers", "participants"):
        message = message[:-1] + f", {name}."
    return _field(name, ("exclusiveFields", message))


def _assert_invalid(response, fields, path="transcripts"):
    assert response.status_code == 200
    body = response.json()
    assert body["data"][path] is None
    assert len(body["errors"]) == 1
    error = body["errors"][0]
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
    ],
)
def test_fireflies_measured_filter_controls(fireflies_http, arguments, reference):
    response = _query(fireflies_http, arguments + ", limit: 50")
    assert response.status_code == 200
    assert "errors" not in response.json()
    assert response.json() == _query(fireflies_http, reference + ", limit: 50").json()


def test_fireflies_measured_compound_limit_collision(fireflies_http):
    fields = [
        _field("limit", ("isPositive", "limit must be a positive number")),
        _exclusive("mine"),
        _exclusive("hostEmail"),
    ]
    _assert_invalid(_query(fireflies_http, "host_email: HOST, mine: true, limit: 0"), fields)


def test_fireflies_measured_compound_email_collision(fireflies_http):
    host = _exclusive("hostEmail")
    host["constraints"].append(
        {"type": "IsEmailOnly", "message": "hostEmail must be a valid email address"}
    )
    _assert_invalid(
        _query(fireflies_http, 'host_email: "nope", mine: true'), [_exclusive("mine"), host]
    )


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
    ],
)
def test_fireflies_supporting_measured_validation(fireflies_http, arguments, name, kind, message):
    _assert_invalid(_query(fireflies_http, arguments), [_field(name, (kind, message))])


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
        'host_email: "someone@example.com"',
    ],
)
def test_fireflies_measured_validation_boundaries(fireflies_http, arguments):
    response = _query(fireflies_http, arguments)
    assert response.status_code == 200
    body = response.json()
    assert "errors" not in body
    if arguments in ("skip: 5000", 'host_email: "someone@example.com"'):
        assert body["data"]["transcripts"] == []
    else:
        assert isinstance(body["data"]["transcripts"], list)


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
