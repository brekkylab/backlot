"""Slack's Web API surface: conversations, users, and search.

One file per router, so a source's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import json
import re
import urllib.parse

import pytest
import yaml
from fastapi.responses import JSONResponse

from backlot import published, store, synth
from backlot.routers import slack
from tests._helpers import (
    client_for,
    corpus_client,
    crawl_slack,
    db_count,
    sent_as,
    tiny_corpus,
    tok,
)

_FORM = "application/x-www-form-urlencoded"
_MULTIPART = "multipart/form-data; boundary=b"


def _parts(fields) -> bytes:
    """A multipart body under :data:`_MULTIPART` with a part for each (name, value) of ``fields``,
    or an empty one for none: the closing delimiter alone is `invalid_form_data`."""
    parts = "".join(
        f'--b\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        for name, value in fields
    )
    return (parts + "--b--\r\n").encode() if parts else b""


# A part's lines after its delimiter, for a body that spells its delimiters itself.
_PART_A = b'Content-Disposition: form-data; name="a"\r\n\r\nb\r\n'
_PART_C = b'Content-Disposition: form-data; name="c"\r\n\r\nd\r\n'
_PART_E = b'Content-Disposition: form-data; name="e"\r\n\r\nf\r\n'
_PART_EMPTY = b'Content-Disposition: form-data; name=""\r\n\r\nx\r\n'


def _file(name: str, filename: str = "x", value: str = "f") -> bytes:
    """A file's part under :data:`_MULTIPART`, its delimiter included."""
    return (
        f'--b\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n\r\n{value}\r\n'
    ).encode()


def _field(name: str, value: str = "v") -> bytes:
    """:func:`_file` with no `filename`: a value's part."""
    return f'--b\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()


def _one_part(name: str, value: str) -> bytes:
    """:func:`_parts` with the one field ``name``."""
    return _parts([(name, value)])


_ONE_PART = _one_part("x", "y")


def _send(client, path: str, method: str, query: str, authorization, form, t: dict):
    """``method`` to ``/slack/api/<path>`` with ``query``, an `Authorization` header and a body,
    each formatted with ``t``: a ``form`` that is a (name, value) pair is a multipart body's one
    part, and a string an urlencoded body."""
    headers = {} if authorization is None else {"Authorization": authorization.format(**t)}
    body = {}
    if isinstance(form, tuple):
        headers["Content-Type"] = _MULTIPART
        body["content"] = _one_part(form[0], form[1].format(**t))
    elif form is not None:
        headers["Content-Type"] = _FORM
        body["content"] = form.format(**t)
    url = f"/slack/api/{path}?{query.format(**t)}" if query else f"/slack/api/{path}"
    return getattr(client, method)(url, headers=headers, **body)


# A user id Backlot mints, in the shape a client written against Slack will accept:
# `docs.slack.dev/enterprise#what` gives an enterprise user id as "a user ID beginning with `U` or
# `W`" and a legacy one as beginning with `U`. The prefix is Slack's; the two or more uppercase
# alphanumerics after it are what Backlot mints, as the message event reference's own example
# lists reaction users as short as `U1`.
USER_ID = re.compile(r"[UW][A-Z0-9]{2,}")


def test_admin_slack_crawls_all(client, admin_h, ro_conn):
    assert crawl_slack(client, admin_h) == db_count(ro_conn, "slack")


_TOKEN = "<the token>"


@pytest.mark.parametrize(
    "method, query, authorization, form, want",
    [
        # no credential, and one that is none
        ("get", "foo=1", None, None, {"foo": "1"}),
        ("post", "", None, "foo=bar", {"foo": "bar"}),
        ("get", "", "Bearer ", None, {}),
        ("get", "foo=1", "bearer {admin}", None, {"foo": "1"}),
        ("get", "foo=1", "Basic Zm9vOmJhcg==", None, {"foo": "1"}),
        ("get", "token=&foo=1", None, None, {"token": "", "foo": "1"}),
        # a credential, echoed as `token` where the body put it and last from the header
        ("get", "foo=1", "Bearer {admin}", None, {"foo": "1", "token": _TOKEN}),
        ("post", "foo=1", None, "token={admin}", {"foo": "1", "token": _TOKEN}),
        ("post", "", None, "token={admin}&a=b", {"token": _TOKEN, "a": "b"}),
        ("post", "c=d", "Bearer {admin}", "a=b", {"c": "d", "a": "b", "token": _TOKEN}),
        ("post", "", "Bearer {admin}", "token={admin}", {"token": _TOKEN}),
        ("post", "", "Bearer {admin}", "token=&a=b", {"token": _TOKEN, "a": "b"}),
        ("post", "", "Bearer {admin}", ("token", ""), {"token": _TOKEN}),
        ("post", "", None, ("token", "{admin}"), {"token": _TOKEN}),
        ("post", "", "Bearer {admin}", ("a", "b"), {"a": "b", "token": _TOKEN}),
        ("post", "", "Bearer {bogus}", "token={admin}", {"token": _TOKEN}),
        # a credential that does not resolve, and a `token` query parameter
        ("get", "foo=1", "Bearer {bogus}", None, "invalid_auth"),
        ("post", "foo=1", None, "token={bogus}", "invalid_auth"),
        ("post", "", None, ("token", "{bogus}"), "invalid_auth"),
        ("post", "", None, "token=%FF", "invalid_auth"),
        ("post", "", "Bearer {admin}", "token=%FF", "invalid_auth"),
        ("post", "", "Bearer {admin}", "token=%20", "invalid_auth"),
        ("get", "token={admin}&foo=1", None, None, "invalid_auth"),
        ("get", "token={bogus}&foo=1", None, None, "invalid_auth"),
        ("get", "a=1&token=2", None, None, "invalid_auth"),
        ("get", "token=&foo=1", "Bearer {admin}", None, "invalid_auth"),
        # a `token` form field in the query parameter's place (`auth.slack_query_token_refused`),
        # and a body with none
        ("post", "token={admin}", None, "token=", {"token": ""}),
        ("post", "token={admin}", None, ("token", ""), {"token": ""}),
        ("post", "token={admin}", None, "a=b", "invalid_auth"),
        # an error argument, answered after the credential
        ("get", "error=x", None, None, ("x", {"error": "x"})),
        ("post", "", None, "error=boom", ("boom", {"error": "boom"})),
        ("get", "error=x", "Bearer {admin}", None, ("x", {"error": "x", "token": _TOKEN})),
        ("get", "error=x&token=", None, None, ("x", {"error": "x", "token": ""})),
        ("get", "error=y", "Bearer ", None, ("y", {"error": "y"})),
        (
            "post",
            "",
            "Bearer {admin}",
            "error=x&a=b",
            ("x", {"error": "x", "a": "b", "token": _TOKEN}),
        ),
        ("post", "error=y", "Bearer {admin}", "error=x", ("x", {"error": "x", "token": _TOKEN})),
        ("post", "", None, "error=%FF", (None, {"error": None})),
        ("get", "error=", None, None, {"error": ""}),
        ("get", "error=&foo=1", "Bearer {admin}", None, {"error": "", "foo": "1", "token": _TOKEN}),
        ("get", "error=x", "Bearer {bogus}", None, "invalid_auth"),
        ("get", "token=x&error=y", None, None, "invalid_auth"),
    ],
)
def test_slack_api_test_reads_a_credential_and_echoes_it(
    client, tokens_yaml, method, query, authorization, form, want
):
    """`api.test` answers each row as slack.com/api answered it (``slack.api_test`` dates the
    measurement): ``want`` is what a served request echoes, a refused one's error, or a pair of
    the two for an error answer. ``_TOKEN`` stands for the admin token the row sent."""
    t = {"admin": tokens_yaml["admin_token"], "bogus": "xoxb-not-a-real-token"}
    r = _send(client, "api.test", method, query, authorization, form, t)

    def echo(args):
        return {k: t["admin"] if v == _TOKEN else v for k, v in args.items()}

    if isinstance(want, str):
        expected = {"ok": False, "error": want}
    elif isinstance(want, tuple):
        expected = {"ok": False, "error": want[0], "args": echo(want[1])}
    else:
        expected = {"ok": True, "args": echo(want)}
    assert r.content == JSONResponse(expected).body


def test_slack_accepts_form_field_token(client, tokens_yaml):
    # the official slack-go SDK posts the token as a form field (no bearer header); Backlot
    # must accept it exactly like a real Slack Web API.
    admin = tokens_yaml["admin_token"]
    ok = client.post("/slack/api/search.messages", data={"token": admin, "query": "the"}).json()
    assert ok["ok"] is True
    # no token anywhere -> not_authed
    none = client.post("/slack/api/search.messages", data={"query": "the"}).json()
    assert none == {"ok": False, "error": "not_authed"}


def test_slack_users_info_resolves_author(client, admin_h, ro_conn):
    # users.info must resolve a Slack message author's synthesized id (incl. display-only
    # speakers/bots, which aren't principals) — qst_0077's raw-ID bug.
    email = ro_conn.execute("SELECT DISTINCT author_email FROM slack_messages LIMIT 1").fetchone()[
        0
    ]
    uid = synth.slack_user_id(email)
    j = client.post("/slack/api/users.info", headers=admin_h, data={"user": uid}).json()
    assert j["ok"] is True
    assert j["user"]["id"] == uid and j["user"]["profile"]["email"] == email
    # a bogus id still 404s (clause honored, cache doesn't invent users)
    bad = client.post("/slack/api/users.info", headers=admin_h, data={"user": "UZZZZZZZZZZ"}).json()
    assert bad == {"ok": False, "error": "user_not_found"}


# --- Slack fidelity ---------------------------------------------------------------------
#
# Reported from building a filesystem-style Slack client against Backlot. Slack answers an
# application error as HTTP 200 with {"ok": false, "error": …}, which Backlot already does — these
# are about the cases where it answered something real Slack never would.
#
# NOTE: each expectation says where it comes from, because they do not all come from the same
# place. Some are transcribed from Slack's published reference (the channel object's field set, the
# documented `types` default). Others are MEASURED against slack.com: the not_authed/invalid_auth
# split, which needs no account at all, and — with a workspace user token — the error a by-id method
# answers for a private channel that token is not in, for an id that names nothing, and for a
# required argument that never arrived. A test that pins a measured answer says so.


# Measured against slack.com, which answers all of these without an account:
#   curl -H "Authorization: Bearer xoxb-not-a-real-token" .../conversations.list -> invalid_auth
#   curl                                                  .../conversations.list -> not_authed
# A merely UNKNOWN token answers the same as a malformed one, so the only thing that decides it is
# whether a credential was presented, which `slack._caller_or_error` decides. The `token` query
# parameter is the case `test_slack_reads_the_token_where_real_reads_it` pins. Every row is a live
# answer, including the ones that read as though they should go the other way.
@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({}, "not_authed"),
        ({"headers": {"Authorization": "Bearer"}}, "not_authed"),
        ({"headers": {"Authorization": "Basic abc"}}, "not_authed"),
        ({"data": {"token": ""}}, "not_authed"),
        ({"headers": {"Authorization": "Bearer xoxb-not-a-real-token"}}, "invalid_auth"),
        ({"headers": {"Authorization": "Bearer xoxb"}}, "invalid_auth"),
        ({"data": {"token": "bogus-token"}}, "invalid_auth"),
        # The scheme is case-SENSITIVE, which RFC 7235 does not require and Slack does anyway, so
        # any other casing is a credential that was never presented rather than a bad one.
        ({"headers": {"Authorization": "bearer xoxb-not-a-real-token"}}, "not_authed"),
        ({"headers": {"Authorization": "BEARER xoxb-not-a-real-token"}}, "not_authed"),
        ({"headers": {"Authorization": "BeArEr xoxb-not-a-real-token"}}, "not_authed"),
        # GitHub's legacy `token <t>` header is not a Slack scheme at all, in either casing.
        ({"headers": {"Authorization": "token xoxb-not-a-real-token"}}, "not_authed"),
        ({"headers": {"Authorization": "Token xoxb-not-a-real-token"}}, "not_authed"),
        # A tab does not separate scheme from token, but repeated spaces do, and either end of the
        # header value may carry slack.
        ({"headers": {"Authorization": "Bearer\txoxb-not-a-real-token"}}, "not_authed"),
        ({"headers": {"Authorization": "Bearerxoxb-not-a-real-token"}}, "not_authed"),
        ({"headers": {"Authorization": "Bearer  xoxb-not-a-real-token"}}, "invalid_auth"),
        ({"headers": {"Authorization": "Bearer xoxb-not-a-real-token "}}, "invalid_auth"),
        # The param and form names are case-sensitive too.
        ({"params": {"token": "bogus-token"}}, "invalid_auth"),
        ({"params": {"TOKEN": "bogus-token"}}, "not_authed"),
        ({"data": {"TOKEN": "bogus-token"}}, "not_authed"),
    ],
)
def test_slack_auth_errors_distinguish_a_missing_token_from_an_unusable_one(client, kwargs, error):
    """A connector branches on these two — invalid_auth means the credential is wrong and
    re-authenticating is the fix, not_authed means none was sent. Answering not_authed to both sends
    it down the wrong branch."""
    r = client.post("/slack/api/conversations.list", **kwargs)
    assert r.json() == {"ok": False, "error": error}


@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "BeArEr", "token", "Token"])
def test_slack_refuses_a_valid_token_under_an_unrecognised_scheme(client, tokens_yaml, scheme):
    """The error label is the visible half of the scheme split; this is the half that costs a
    deployment.

    A VALID token under any of these schemes must not authenticate, because live Slack answers
    `not_authed` to all five. The permissive `auth.bearer_token` accepts every one of them — for
    GitHub, which really does take `token <t>`, and per RFC 7235, which really does make the scheme
    case-insensitive — so Slack needs its own parser rather than that one. Sharing it would let six
    spellings work here and none of them work against Slack."""
    r = client.post(
        "/slack/api/auth.test",
        headers={"Authorization": f"{scheme} {tokens_yaml['admin_token']}"},
    )
    assert r.json() == {"ok": False, "error": "not_authed"}

    # ...while the one spelling Slack does recognise still authenticates, so this is a narrowing and
    # not a blanket refusal.
    ok = client.post(
        "/slack/api/auth.test",
        headers={"Authorization": f"Bearer {tokens_yaml['admin_token']}"},
    ).json()
    assert ok["ok"] is True


def test_slack_auth_error_split_is_uniform_across_methods(client, tokens_yaml):
    """The split is decided once, so no method is left answering the old single error. A token in
    the query string is refused on each of them too."""
    bogus = {"Authorization": "Bearer xoxb-not-a-real-token"}
    in_query = {"token": tokens_yaml["admin_token"]}
    for path in (
        "conversations.list",
        "conversations.info",
        "conversations.history",
        "conversations.members",
        "conversations.replies",
        "users.info",
        "users.list",
        "auth.test",
        "search.messages",
        "search.all",
        "search.files",
    ):
        assert client.post(f"/slack/api/{path}").json()["error"] == "not_authed", path
        assert client.post(f"/slack/api/{path}", headers=bogus).json()["error"] == (
            "invalid_auth"
        ), path
        assert client.get(f"/slack/api/{path}", params=in_query).json() == {
            "ok": False,
            "error": "invalid_auth",
        }, path


_AVA = synth.slack_user_id("ava@acme.com")


@pytest.mark.parametrize(
    "method,query,authorization,form,answer",
    [
        # the `token` query parameter, its last value read: a non-empty one is refused whatever
        # else the request carries but a `token` form field
        ("get", "token={admin}", None, None, "invalid_auth"),
        ("post", "token={admin}", None, None, "invalid_auth"),
        ("post", "token={admin}", None, "x=1", "invalid_auth"),
        ("get", "token=%20", None, None, "invalid_auth"),
        ("get", "token={admin}", "Bearer {admin}", None, "invalid_auth"),
        ("get", "token={bogus}", "Bearer {admin}", None, "invalid_auth"),
        ("get", "token={admin}", "Bearer {bogus}", None, "invalid_auth"),
        ("post", "token={bogus}", None, "token={admin}", "invalid_auth"),
        ("get", "token=&token={admin}", None, None, "invalid_auth"),
        ("get", "token={admin}&token=", None, None, "not_authed"),
        # an empty one, with and without a token elsewhere
        ("get", "token=", "Bearer {admin}", None, "invalid_auth"),
        ("get", "token", "Bearer {admin}", None, "invalid_auth"),
        ("post", "token=", None, "token={admin}", "invalid_auth"),
        ("get", "token=", None, None, "not_authed"),
        ("post", "token=", None, "token=", "not_authed"),
        ("get", "token=", "bearer {admin}", None, "not_authed"),
        ("get", "TOKEN={admin}", "Bearer {admin}", None, "USERVICE0"),
        # a `token` form field in the parameter's place (`auth.slack_query_token_refused`)
        ("post", "token={admin}", None, "token=", "not_authed"),
        ("post", "token={admin}", None, ("token", ""), "not_authed"),
        ("post", "token={bogus}", None, "token=", "not_authed"),
        ("post", "token={admin}", "Bearer {admin}", "token=", "invalid_auth"),
        ("post", "token={admin}", None, "token={admin}", "invalid_auth"),
        # the `token` form field, its last value read, ahead of the bearer header
        ("post", "", "Bearer {bogus}", "token={admin}", "USERVICE0"),
        ("post", "", "Bearer {admin}", "token={ava}", _AVA),
        ("post", "", "Bearer {ava}", "token={admin}", "USERVICE0"),
        ("post", "", "Bearer {admin}", "token={bogus}", "invalid_auth"),
        ("post", "", "Bearer {admin}", "token=", "USERVICE0"),
        ("post", "", "Bearer {admin}", "token=%20", "invalid_auth"),
        ("post", "", None, "token={ava}&token={bogus}", "invalid_auth"),
        ("post", "", None, "token={bogus}&token={ava}", _AVA),
        ("post", "", None, "token={ava}&token=", "not_authed"),
        ("post", "", None, "token={admin}", "USERVICE0"),
        ("post", "", None, "token=", "not_authed"),
        ("post", "", "Bearer {ava}", None, _AVA),
        # a multipart `token` part, read as the form field is
        ("post", "", None, ("token", "{admin}"), "USERVICE0"),
        ("post", "", "Bearer {bogus}", ("token", "{admin}"), "USERVICE0"),
        ("post", "", "Bearer {admin}", ("token", "{bogus}"), "invalid_auth"),
        ("post", "", "Bearer {admin}", ("token", ""), "USERVICE0"),
        ("post", "token=", None, ("token", "{admin}"), "invalid_auth"),
        # a field under the name real reads it by, and a value that is not UTF-8
        ("post", "", None, "to.ken={admin}", "not_authed"),
        ("post", "", None, "tok%65n={admin}", "USERVICE0"),
        ("post", "", None, "token=%FF", "invalid_auth"),
        ("post", "", "Bearer {admin}", "token=%FF", "invalid_auth"),
    ],
)
def test_slack_reads_the_token_where_real_reads_it(
    client, tokens_yaml, tokens, method, query, authorization, form, answer
):
    """Real's answers to `auth.test` for where a request carries its token, the rules
    `auth.slack_token` and `auth.slack_query_token_refused` record, and `slack.read_post_body` for
    the name and the value of a form field, urlencoded or multipart: ``answer`` is the `user_id` a
    served request names, or the error a refused one answers. The rows with a person's token in
    one place and the admin's in the other tell the form field's order from the header's."""
    t = {
        "admin": tokens_yaml["admin_token"],
        "ava": tokens["ava@acme.com"],
        "bogus": "xoxb-not-a-real-token",
    }
    j = _send(client, "auth.test", method, query, authorization, form, t).json()
    if answer in ("invalid_auth", "not_authed"):
        assert j == {"ok": False, "error": answer}
    else:
        assert j["ok"] is True and j["user_id"] == answer


def test_slack_auth_test_identifies_the_caller(client, admin_h, tokens):
    """`auth.test` answers "who am I", and both fields it answers with are the caller's own.

    As `auth_test` records, with a user token `auth.test` answers `user` as that person's handle and
    `user_id` as their own `U…`; the method's own reference fixes the same shape, its user-token
    success example being `"user": "grace"`, `"user_id": "W12345678"`. So `user` is a handle rather
    than an address, and `user_id` is that person's id.

    The id is asserted against `conversations.history`, not against `users.list`: the scenario the
    issue describes is a client matching `auth.test` to the author of a message to find its own, and
    both `auth.test` and `users.list` derive the id through `synth.slack_user_id`, so comparing
    those two would still pass if the derivation drifted away from what a message carries.

    The admin/service token keeps the service identity: it has no corpus email, and real Slack
    answers a bot token with the app's own id rather than a person's."""
    # each of these fixture users authors a message in the channel beside them
    for email, channel in (("ava@acme.com", "eng-announcements"), ("bob@acme.com", "incidents")):
        h = {"Authorization": f"Bearer {tokens[email]}"}
        me = client.post("/slack/api/auth.test", headers=h).json()
        assert me["ok"] is True
        assert me["user"] == email.split("@")[0]  # the handle, not the address
        assert "@" not in me["user"]

        chans = client.post(
            "/slack/api/conversations.list",
            headers=admin_h,
            params={"types": "public_channel,private_channel", "limit": 100},
        ).json()["channels"]
        cid = next(c["id"] for c in chans if c["name"] == channel)
        msgs = client.post(
            "/slack/api/conversations.history", headers=admin_h, params={"channel": cid}
        ).json()["messages"]
        mine = [m for m in msgs if m["user"] == me["user_id"]]
        assert mine, (
            f"{email} authors a message in #{channel}, but auth.test's user_id "
            f"{me['user_id']} matches none of {sorted({m['user'] for m in msgs})}"
        )

    # ...and it is per caller, not one id for the workspace
    ids = {
        e: client.post(
            "/slack/api/auth.test", headers={"Authorization": f"Bearer {tokens[e]}"}
        ).json()["user_id"]
        for e in ("ava@acme.com", "bob@acme.com", "hana@acme.com")
    }
    assert len(set(ids.values())) == 3, ids

    admin = client.post("/slack/api/auth.test", headers=admin_h).json()
    assert admin["user"] == "service-account" and admin["user_id"] == "USERVICE0"


@pytest.mark.parametrize("method", ["get", "post"])
def test_slack_auth_test_answers_reals_members_in_reals_order(client, admin_h, tokens, method):
    """A user token's `ok: true` answer has real's members in real's order (see the comment in
    `auth_test`), and the admin token's answer carries `is_enterprise_install: false` too."""
    user_h = {"Authorization": f"Bearer {tokens['ava@acme.com']}"}
    me = getattr(client, method)("/slack/api/auth.test", headers=user_h).json()
    assert list(me) == ["ok", "url", "team", "user", "team_id", "user_id", "is_enterprise_install"]
    assert me["is_enterprise_install"] is False
    admin = getattr(client, method)("/slack/api/auth.test", headers=admin_h).json()
    assert (admin["ok"], admin["is_enterprise_install"]) == (True, False)


def _a_channel_id(client, admin_h):
    return client.get("/slack/api/conversations.list", headers=admin_h, params={"limit": 1}).json()[
        "channels"
    ][0]["id"]


# SAMPLE's Slack channels, by the only distinction `types` draws between them.
_PUBLIC_CHANNELS = {"eng-announcements", "incidents"}
_PRIVATE_CHANNELS = {"people-confidential"}
_EVERY_CHANNEL = {"types": "public_channel,private_channel", "limit": 100}


@pytest.mark.parametrize(
    "types, expected",
    [
        ("public_channel", _PUBLIC_CHANNELS),
        ("private_channel", _PRIVATE_CHANNELS),
        ("public_channel,private_channel", _PUBLIC_CHANNELS | _PRIVATE_CHANNELS),
        ("im,public_channel", _PUBLIC_CHANNELS),
        ("im", set()),
        ("mpim", set()),
        ("im,mpim", set()),
    ],
)
def test_slack_conversations_list_honours_types(client, admin_h, types, expected):
    """`types` was parsed, and an unknown value rejected — then the parsed set was read as a
    boolean gate, so `public_channel` and `private_channel` never separated: either one returned
    every channel, and a client presenting `channels/` and `dms/` separately got each channel under
    both. This corpus has no DMs, so `im` must come back empty — which is exactly what real Slack
    answers for a DM-less workspace, making "no DMs here" indistinguishable from production instead
    of indistinguishable from a bug."""
    j = client.get(
        "/slack/api/conversations.list", headers=admin_h, params={"types": types, "limit": 5}
    ).json()
    assert j["ok"] is True
    assert {c["name"] for c in j["channels"]} == expected
    assert all(c["is_im"] is False and c["is_mpim"] is False for c in j["channels"])
    # the field the filter selects on, so a channel in the wrong bucket fails here too
    assert all(c["is_private"] is (c["name"] in _PRIVATE_CHANNELS) for c in j["channels"])


def test_slack_conversations_list_defaults_to_public_channels(client, admin_h):
    """Slack's documented default when `types` is omitted is `public_channel`, which `_slack_types`
    returned and the handler then ignored — so an omitted filter listed private channels."""
    omitted = client.get(
        "/slack/api/conversations.list", headers=admin_h, params={"limit": 5}
    ).json()
    explicit = client.get(
        "/slack/api/conversations.list",
        headers=admin_h,
        params={"limit": 5, "types": "public_channel"},
    ).json()
    assert omitted["channels"] == explicit["channels"]
    assert {c["name"] for c in omitted["channels"]} == _PUBLIC_CHANNELS


# Transcribed from Slack's documented example response for conversations.list:
# https://docs.slack.dev/reference/methods/conversations.list
#
# A FLOOR, not the exact shape, and transcribed from the .list page alone — which is why the five
# extra keys a live channel object carries (context_team_id, shared_team_ids,
# pending_connected_team_ids, parent_conversation, properties) are not in it: that page's example
# response omits all five. They are served, and pinned below against both methods.
#
# .list and .info do not agree with each other either — num_members is .list-only and last_read is
# .info-only — so this set is asserted against each with that one difference applied.
_DOCUMENTED_CHANNEL_KEYS = frozenset(
    {
        "id",
        "name",
        "is_channel",
        "is_group",
        "is_im",
        "created",
        "creator",
        "is_archived",
        "is_general",
        "unlinked",
        "name_normalized",
        "is_shared",
        "is_ext_shared",
        "is_org_shared",
        "pending_shared",
        "is_pending_ext_shared",
        "is_member",
        "is_private",
        "is_mpim",
        "updated",
        "topic",
        "purpose",
        "previous_names",
        "num_members",
    }
)


def test_slack_channel_object_carries_slacks_documented_field_set(client, admin_h):
    """Diffed against the transcription above rather than spot-checked, so a documented field that
    goes missing fails here instead of waiting to be noticed. `pending_shared` and
    `is_pending_ext_shared` are inert constants, but a client validating the object against a
    generated model still sees a shape real Slack never returns without them.

    A subset check, not an equality one: the live API sends more than the method page documents, so
    pinning an exact set would fail the day a real field is added (see #74)."""
    listed = client.get(
        "/slack/api/conversations.list", headers=admin_h, params={"limit": 1}
    ).json()["channels"][0]
    assert _DOCUMENTED_CHANNEL_KEYS <= set(listed)

    # .info answers the same core minus num_members, which is opt-in there.
    info = client.post(
        "/slack/api/conversations.info", headers=admin_h, data={"channel": listed["id"]}
    ).json()["channel"]
    assert _DOCUMENTED_CHANNEL_KEYS - {"num_members"} <= set(info)
    assert "num_members" not in info
    # and it carries what only .info does — for a service token, the zero ts (see _last_read)
    assert info["last_read"] == slack.ZERO_TS

    # Where these are documented differs by method, so the two pages are transcribed separately:
    # the `.info` page's own example response carries all six, while the `.list` page's example
    # carries none of them and only the conversation object reference describes them. Sent on every
    # live response either way — a single-workspace channel shared with nobody.
    for ch in (listed, info):
        assert ch["context_team_id"] == "T0000BKLT"
        assert ch["shared_team_ids"] == ["T0000BKLT"]
        assert ch["pending_connected_team_ids"] == []
        assert ch["parent_conversation"] is None
        # contextual channel configuration, which no corpus states — present but empty, never
        # furnished with settings this workspace does not have
        assert ch["properties"] == {}

    # the shared-channel family is answered in full, and consistently
    assert listed["pending_shared"] == [] and listed["is_pending_ext_shared"] is False
    assert listed["is_shared"] is False and listed["is_ext_shared"] is False
    assert listed["is_org_shared"] is False
    # nesting is part of the shape too
    assert set(listed["topic"]) == {"value", "creator", "last_set"}
    assert set(listed["purpose"]) == {"value", "creator", "last_set"}


def _private_channel(client, admin_h) -> dict:
    """SAMPLE's one private channel, as the service token sees it."""
    everything = client.get(
        "/slack/api/conversations.list", headers=admin_h, params=_EVERY_CHANNEL
    ).json()["channels"]
    private = next(c for c in everything if c["name"] == "people-confidential")
    assert private["is_private"] is True
    return private


def test_slack_info_answers_one_shape_whoever_asks(client, admin_h, tokens):
    """`last_read` is the caller's, so its value varies — its presence must not.

    Gating the key on what the caller can read gave `.info` two shapes, which defeats the point of
    pinning the object exactly, and made the key readable as an ACL oracle. A caller with nothing to
    have read gets the zero ts instead, so the shape is invariant and the answer is not there to be
    read off. A caller who may not see the channel at all is answered `channel_not_found` and so has
    no object to read anything off — the test below."""
    private = _private_channel(client, admin_h)

    shapes, values = set(), {}
    for who in ("admin", "hana@acme.com"):
        h = admin_h if who == "admin" else {"Authorization": f"Bearer {tokens[who]}"}
        ch = client.post(
            "/slack/api/conversations.info", headers=h, data={"channel": private["id"]}
        ).json()["channel"]
        shapes.add(frozenset(ch))
        values[who] = ch["last_read"]

    assert len(shapes) == 1, "conversations.info must answer one key set whoever asks"
    assert all("last_read" in shape for shape in shapes)
    # the service token is not a person and has read nothing, while the one person who can read the
    # channel is caught up on it — so this is neither a blanket zero nor a blanket ts
    assert values["admin"] == slack.ZERO_TS
    assert float(values["hana@acme.com"]) > 0


@pytest.mark.parametrize(
    "method, extra, when_visible",
    [
        ("conversations.info", {}, None),
        ("conversations.members", {}, None),
        ("conversations.history", {}, None),
        # replies takes a ts, and the channel is answered before it: for a channel the caller CAN
        # see, a ts that names nothing is `thread_not_found` — which is why it is the third value
        # here rather than an `ok`.
        ("conversations.replies", {"ts": "1700000000.000100"}, "thread_not_found"),
    ],
)
@pytest.mark.parametrize("sent_as", ["query", "form", "multipart"])
def test_slack_by_id_methods_refuse_a_channel_the_caller_cannot_see(
    client, admin_h, tokens, method, extra, when_visible, sent_as
):
    """`conversations.list` scopes by ACL; the methods that resolve a channel by id did not, so an
    id was enough for any authenticated principal to confirm a private room, read its name, topic,
    purpose and creation time, and enumerate who is in it — `conversations.members` being a
    projection of the very messages the ACL is withholding. `conversations.history` answered
    `ok:true` with an empty `messages`, which reads as "this channel exists and you may read it; it
    happens to be empty" — the opposite of what Backlot knows.

    All four answer `channel_not_found`, the same answer an id that names nothing gets, so a hidden
    channel is not distinguishable from one that was never there. Measured against slack.com with a
    workspace token, on a private channel that token is not in beside a well-formed id for no
    channel at all — every one of the eight answers is `channel_not_found`. NOT the `not_in_channel`
    that `conversations.history` also documents: that is the case where the token can SEE the
    channel and has not joined it, which is a public channel here and never refused, and the other
    three methods do not document the error at all. The arguments go in the query string, a form
    body or a multipart one (``sent_as``), and each answers the same."""
    private = _private_channel(client, admin_h)

    def ask(headers, params):
        if sent_as == "query":
            return client.get(f"/slack/api/{method}", headers=headers, params=params).json()
        if sent_as == "form":
            return client.post(f"/slack/api/{method}", headers=headers, data=params).json()
        h = {**headers, "Content-Type": _MULTIPART}
        return client.post(f"/slack/api/{method}", headers=h, content=_parts(params.items())).json()

    hidden_and_made_up = ({"channel": private["id"]}, {"channel": "C0000000000"})
    for email in ("ava@acme.com", "bob@acme.com"):
        h = {"Authorization": f"Bearer {tokens[email]}"}
        for params in hidden_and_made_up:
            j = ask(h, {**params, **extra})
            assert j == {"ok": False, "error": "channel_not_found"}, (email, params)

    def answer(headers, channel):
        j = ask(headers, {"channel": channel, **extra})
        return j.get("error") if not j["ok"] else None

    # The test is visibility, NOT membership: the one person who may read it is answered on the
    # channel's own terms...
    hana = {"Authorization": f"Bearer {tokens['hana@acme.com']}"}
    assert answer(hana, private["id"]) == when_visible
    # ...and so is a public channel the caller can see but has never posted in (bob has only ever
    # spoken in #incidents), which real Slack answers rather than refusing.
    bob = {"Authorization": f"Bearer {tokens['bob@acme.com']}"}
    public = client.get(
        "/slack/api/conversations.list", headers=bob, params={"types": "public_channel"}
    ).json()["channels"]
    quiet = next(c for c in public if c["name"] == "eng-announcements")
    assert quiet["is_member"] is False
    assert answer(bob, quiet["id"]) == when_visible


def test_slack_conversations_list_rejects_an_unknown_type(client, admin_h):
    """Real Slack answers `invalid_types`; Backlot accepted anything, so a typo'd filter silently
    returned the unfiltered list."""
    j = client.get(
        "/slack/api/conversations.list", headers=admin_h, params={"types": "bogus_type"}
    ).json()
    assert j == {"ok": False, "error": "invalid_types"}
    mixed = client.get(
        "/slack/api/conversations.list",
        headers=admin_h,
        params={"types": "public_channel,bogus_type"},
    ).json()
    assert mixed == {"ok": False, "error": "invalid_types"}


@pytest.mark.parametrize("path", ["conversations.list", "users.list"])
def test_slack_rejects_an_invalid_cursor(client, admin_h, path):
    """An undecodable cursor was treated as offset 0, so a client paginating with a corrupted
    cursor looped on page 1 forever instead of failing. Real Slack answers `invalid_cursor`."""
    for bad in ("bogus", "###"):
        j = client.get(f"/slack/api/{path}", headers=admin_h, params={"cursor": bad}).json()
        assert j == {"ok": False, "error": "invalid_cursor"}, (path, bad)


@pytest.mark.parametrize(
    "params, error",
    [
        # One unreadable argument.
        *(
            ({param: value}, error)
            for param, error in (("latest", "invalid_ts_latest"), ("oldest", "invalid_ts_oldest"))
            for value in ("not-a-ts", "inf", "-inf", "nan", "Infinity", "-Infinity", "NaN")
        ),
        ({"cursor": "zzz"}, "invalid_cursor"),
        # Two or more unreadable arguments.
        ({"oldest": "abc", "latest": "abc"}, "invalid_ts_latest"),
        ({"oldest": "abc", "cursor": "zzz"}, "invalid_ts_oldest"),
        ({"latest": "abc", "cursor": "zzz"}, "invalid_ts_latest"),
        ({"oldest": "abc", "latest": "abc", "cursor": "zzz"}, "invalid_ts_latest"),
        ({"cursor": "zzz", "latest": "abc", "oldest": "abc"}, "invalid_ts_latest"),
        ({"oldest": "inf", "latest": "inf"}, "invalid_ts_latest"),
        ({"oldest": "abc", "latest": "inf"}, "invalid_ts_latest"),
        ({"oldest": "inf", "latest": "abc"}, "invalid_ts_latest"),
        ({"oldest": "nan", "cursor": "zzz"}, "invalid_ts_oldest"),
        # An empty timestamp is none, and a channel that names nothing is answered first.
        ({"latest": "", "cursor": "zzz"}, "invalid_cursor"),
        ({"oldest": "", "cursor": "zzz"}, "invalid_cursor"),
        ({"channel": "C00000000", "latest": "abc", "cursor": "zzz"}, "channel_not_found"),
    ],
)
def test_slack_history_names_the_first_error_real_would(client, admin_h, params, error):
    """Real Slack answers 200 with the named error for one unreadable argument, and for two or
    more with the error of the argument it reads first. `_slack_ts` records the measured
    spellings, and the comment above the checks in `conversations_history` records the order."""
    r = client.get(
        "/slack/api/conversations.history",
        headers=admin_h,
        params={"channel": _a_channel_id(client, admin_h), **params},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": error}


@pytest.mark.parametrize(
    "params, echoed",
    [
        ({"oldest": "1700000000"}, {"oldest": "1700000000.000000"}),
        ({"oldest": "1700000000.5"}, {"oldest": "1700000000.000005"}),
        ({"oldest": "1700000000.1234567"}, {"oldest": "17000000001.234567"}),
        ({"oldest": "1.1.1"}, {"oldest": "0000000001.000001"}),
        ({"oldest": "a.1"}, {"oldest": "0000000000.000001"}),
        ({"oldest": "abc123"}, {"oldest": "0000000123.000000"}),
        ({"oldest": "1e999"}, {"oldest": "0000001999.000000"}),
        ({"oldest": "1\u00b2"}, {"oldest": "0000000001.000000"}),
        ({"oldest": "0.0000001"}, {"oldest": "0000000000.000001"}),
        ({"oldest": "1.0000001"}, {"oldest": "0000000001.000001"}),
        ({"oldest": "1.a"}, {"oldest": "0000000001.000000"}),
        ({"oldest": "1.\u00b2"}, {"oldest": "0000000001.000000"}),
        ({"oldest": "9" * 5000}, {"oldest": "9223372036854.775807"}),
        ({"latest": "1700000000.5"}, {"latest": "1700000000.000005"}),
        (
            {"oldest": "1700000000", "latest": "1800000000"},
            {"latest": "1800000000.000000", "oldest": "1700000000.000000"},
        ),
        ({"oldest": "0", "latest": "1800000000"}, {"latest": "1800000000.000000"}),
        ({"oldest": "0"}, {}),
        ({"latest": ""}, {}),
        ({"oldest": "\u0661\u0662\u0663"}, "invalid_ts_oldest"),
        ({"oldest": "\uff15"}, "invalid_ts_oldest"),
        ({"oldest": "\u00b2"}, "invalid_ts_oldest"),
        ({"oldest": "0.0"}, "invalid_ts_oldest"),
        ({"oldest": "-0"}, "invalid_ts_oldest"),
        ({"latest": "00"}, "invalid_ts_latest"),
    ],
)
def test_slack_history_reads_and_echoes_a_timestamp(client, admin_h, params, echoed):
    """`oldest` and `latest` as `_slack_ts` reads them, sent back right after `ok`, or the refusal
    of one it cannot read."""
    body = client.get(
        "/slack/api/conversations.history",
        headers=admin_h,
        params={"channel": _a_channel_id(client, admin_h), **params},
    ).json()
    if isinstance(echoed, str):
        assert body == {"ok": False, "error": echoed}
        return
    assert list(body.items())[: 1 + len(echoed)] == [("ok", True), *echoed.items()]
    assert {"oldest", "latest"} & body.keys() == echoed.keys()


@pytest.mark.parametrize(
    "bounds, inclusive, served",
    [
        ({"oldest": 0}, False, False),
        ({"oldest": 0}, True, True),
        ({"oldest": -1}, False, True),
        ({"oldest": 1}, False, False),
        ({"latest": 0}, False, False),
        ({"latest": 0}, True, True),
        ({"latest": 1}, False, True),
        ({"latest": -1}, False, False),
        ({"oldest": 0, "latest": 0}, False, False),
        ({"oldest": 0, "latest": 0}, True, True),
        ({"oldest": -1, "latest": 1}, False, True),
        ({"oldest": 1, "latest": -1}, False, False),
    ],
)
def test_slack_history_bounds_a_message_to_the_microsecond(
    client, admin_h, bounds, inclusive, served
):
    """The newest message, with each bound its row's offset in microseconds from the message's `ts`,
    is served or not as real serves it, and is the page's only message between two bounds that
    close."""
    cid = _a_channel_id(client, admin_h)
    history = "/slack/api/conversations.history"
    ts = client.get(history, headers=admin_h, params={"channel": cid}).json()["messages"][0]["ts"]
    seconds, micros = ts.split(".")
    at = int(seconds) * 1_000_000 + int(micros)
    params = {"channel": cid}
    for param, offset in bounds.items():
        params[param] = f"{(at + offset) // 1_000_000}.{(at + offset) % 1_000_000:06d}"
    if inclusive:
        params["inclusive"] = "true"
    page = [m["ts"] for m in client.get(history, headers=admin_h, params=params).json()["messages"]]
    assert (ts in page) is served
    if len(bounds) == 2:
        assert page == ([ts] if served else [])


#: Each measured value and whether the boundary message was served; the measurement lives in
#: conversations_history. These values were sent with oldest, latest and both bounds.
_INCLUSIVE_AT_EACH_BOUND = [
    (None, False),
    ("", False),
    ("0", False),
    ("false", False),
    ("False", False),
    *((value, True) for value in ("true", "True", "TRUE", "tRuE", "1", "01", "2", "-1", "1.0")),
    *((value, True) for value in ("yes", "on", "t", " true", "true ")),
]
#: These values, including the repeated parameters, were measured with both bounds set.
_INCLUSIVE_AT_BOTH_BOUNDS = [
    ("FALSE", False),
    ("fAlSe", False),
    *((value, True) for value in ("no", "off", "f", "n", "00", "000", "0.0", " 0", "0 ", "-0")),
    *((value, True) for value in ("+0", "null", "none", "nil", "false ", " false", "\x00")),
    (("true", "0"), False),
    (("0", "true"), True),
]


@pytest.mark.parametrize(
    "bounds, value, served",
    [
        (bounds, value, served)
        for bounds in (("oldest",), ("latest",), ("oldest", "latest"))
        for value, served in _INCLUSIVE_AT_EACH_BOUND
    ]
    + [(("oldest", "latest"), value, served) for value, served in _INCLUSIVE_AT_BOTH_BOUNDS],
)
def test_slack_history_inclusive_spellings(client, admin_h, bounds, value, served):
    """The `inclusive` values sent to conversations_history serve or omit each bound message."""
    history = "/slack/api/conversations.history"
    cid = _a_channel_id(client, admin_h)
    ts = client.get(history, headers=admin_h, params={"channel": cid}).json()["messages"][0]["ts"]
    params = [("channel", cid), *((bound, ts) for bound in bounds)]
    if value is not None:
        params += [
            ("inclusive", item) for item in (value if isinstance(value, tuple) else (value,))
        ]
    response = client.get(history, headers=admin_h, params=params).json()
    assert response["ok"] is True
    assert (ts in [m["ts"] for m in response["messages"]]) is served


def test_slack_members_are_the_channels_own_speakers(client, admin_h, tokens, ro_conn):
    """Every public channel reported the same membership — the entire roster — because the handler
    skipped membership for a public channel. Real Slack's membership differs per channel, and a
    workspace where every channel holds everybody is not a shape it produces.

    A public channel's membership is its own participants, which is what the corpus knows about a
    room everyone may read — and `is_member` is the caller's own place in that same set. It was the
    constant `True`, so every channel a caller could see claimed them as a member, including public
    ones they have never posted in; a client that stats a channel and then walks its members got two
    answers to one question.

    The invariant asserted at the end — `is_member` iff the caller is in `conversations.members` —
    holds for a private channel too, where both are its readers rather than its speakers (see
    test_slack_a_private_channels_members_are_its_readers)."""
    chans = client.get(
        "/slack/api/conversations.list", headers=admin_h, params=_EVERY_CHANNEL
    ).json()["channels"]
    seen = {}
    for c in chans[:4]:
        m = client.get(
            "/slack/api/conversations.members",
            headers=admin_h,
            params={"channel": c["id"], "limit": 1000},
        ).json()
        assert m["ok"] is True
        seen[c["name"]] = set(m["members"])
        expected = {
            r[0]
            for r in ro_conn.execute(
                "SELECT DISTINCT author_email FROM slack_messages WHERE channel = ?", (c["name"],)
            )
        }
        assert len(seen[c["name"]]) == len(expected), c["name"]
    assert len(set(map(frozenset, seen.values()))) > 1, (
        "different channels must not all report identical membership"
    )

    for email in ("ava@acme.com", "bob@acme.com", "hana@acme.com"):
        uid = synth.slack_user_id(email)
        listed = client.get(
            "/slack/api/conversations.list",
            headers={"Authorization": f"Bearer {tokens[email]}"},
            params=_EVERY_CHANNEL,
        ).json()["channels"]
        assert listed
        for c in listed:
            assert c["is_member"] is (uid in seen[c["name"]]), (email, c["name"])
    # ...and the service token, which is nobody, is a member of nothing
    assert all(c["is_member"] is False for c in chans)


def test_slack_members_paginate(client, admin_h):
    """`limit` and `cursor` were never read, so `limit=5` returned 16,034 members with an empty
    cursor. Real Slack paginates this method (default 100, cursor-based)."""
    cid = _a_channel_id(client, admin_h)
    first = client.get(
        "/slack/api/conversations.members", headers=admin_h, params={"channel": cid, "limit": 2}
    ).json()
    assert len(first["members"]) <= 2
    cursor = first["response_metadata"]["next_cursor"]
    everyone = client.get(
        "/slack/api/conversations.members", headers=admin_h, params={"channel": cid, "limit": 1000}
    ).json()["members"]
    if len(everyone) > 2:
        assert cursor, "a truncated page must hand back a cursor"
        second = client.get(
            "/slack/api/conversations.members",
            headers=admin_h,
            params={"channel": cid, "limit": 2, "cursor": cursor},
        ).json()
        assert not set(first["members"]) & set(second["members"]), "pages must not overlap"
        assert set(first["members"]) | set(second["members"]) <= set(everyone)
    else:
        assert cursor == ""


def test_slack_num_members_agrees_with_the_member_list(client, admin_h):
    """`conversations.info.num_members` counted the roster while `conversations.members` now pages
    the channel's own speakers. A client that stats a channel and then walks it must not get two
    different answers for the same question."""
    chans = client.get(
        "/slack/api/conversations.list", headers=admin_h, params={"limit": 100}
    ).json()["channels"]
    for c in chans[:4]:
        listed = client.get(
            "/slack/api/conversations.members",
            headers=admin_h,
            params={"channel": c["id"], "limit": 1000},
        ).json()["members"]
        assert c["num_members"] == len(listed), c["name"]
        # .info counts only when asked — the vendor's own include_num_members
        plain = client.get(
            "/slack/api/conversations.info", headers=admin_h, params={"channel": c["id"]}
        ).json()["channel"]
        assert "num_members" not in plain, c["name"]
        info = client.get(
            "/slack/api/conversations.info",
            headers=admin_h,
            params={"channel": c["id"], "include_num_members": "true"},
        ).json()["channel"]
        assert info["num_members"] == len(listed), c["name"]


_OWN_CHANNEL = "<the caller's own channel>"  # stands for an id only the test body can look up


@pytest.mark.parametrize(
    "method, params, error, missing",
    [
        # An ABSENT required argument — the request is malformed, and no channel was ever named.
        ("conversations.info", {}, "invalid_arguments", ["channel"]),
        ("conversations.members", {}, "invalid_arguments", ["channel"]),
        ("conversations.history", {}, "invalid_arguments", ["channel"]),
        ("conversations.replies", {}, "invalid_arguments", ["channel", "ts"]),
        ("conversations.replies", {"channel": "C_NOPE"}, "invalid_arguments", ["ts"]),
        ("conversations.replies", {"ts": "1.0"}, "invalid_arguments", ["channel"]),
        ("conversations.replies", {"channel": ""}, "invalid_arguments", ["ts"]),
        # PRESENT and empty, or present and naming nothing — the argument arrived, so what is
        # missing is the channel.
        ("conversations.info", {"channel": ""}, "channel_not_found", []),
        ("conversations.members", {"channel": ""}, "channel_not_found", []),
        ("conversations.history", {"channel": ""}, "channel_not_found", []),
        ("conversations.info", {"channel": "C_NOPE"}, "channel_not_found", []),
        ("conversations.members", {"channel": "C_NOPE"}, "channel_not_found", []),
        ("conversations.history", {"channel": "C_NOPE"}, "channel_not_found", []),
        ("conversations.replies", {"channel": "C_NOPE", "ts": "1.0"}, "channel_not_found", []),
        # ...and a ts that arrived empty is answered by the thread, the channel being readable.
        ("conversations.replies", {"channel": _OWN_CHANNEL, "ts": ""}, "thread_not_found", []),
        # The search family draws the line in the same place, and has its OWN name for the blank
        # half: `no_query`, the vendor's spelling for a query that arrived with nothing in it.
        ("search.messages", {}, "invalid_arguments", ["query"]),
        ("search.all", {}, "invalid_arguments", ["query"]),
        ("search.files", {}, "invalid_arguments", ["query"]),
        ("search.messages", {"query": ""}, "no_query", []),
        ("search.all", {"query": ""}, "no_query", []),
        ("search.files", {"query": ""}, "no_query", []),
        ("search.messages", {"query": "   "}, "no_query", []),  # whitespace is not a query either
        # what real strips before it reads a query as none is `slack._QUERY_BLANKS`
        *(
            (method, {"query": query}, error, [])
            for method in ("search.messages", "search.all", "search.files")
            for query, error in (("\x00 \x0b\r\n\t", "no_query"), ("\x0c", None), ("\xa0", None))
        ),
        # users.info is the exception, and is left one: live it answers `user_not_found` to all of
        # them, absent argument included. Pinned so the rule above is not "tidied" onto it.
        ("users.info", {}, "user_not_found", []),
        ("users.info", {"user": ""}, "user_not_found", []),
    ],
)
def test_slack_an_absent_argument_is_not_a_thing_that_was_not_found(
    client, admin_h, method, params, error, missing
):
    """Slack separates "you did not pass the argument" from "what you passed names nothing", and
    Backlot had only the second — so a client that omitted `channel` was told the thing it never
    named does not exist. The two send it down different branches: `channel_not_found` is about the
    workspace, `invalid_arguments` about the request it just built.

    Every row measured against slack.com with a workspace token, users.info's included — the rule
    is the vendor's, not a symmetry imposed on it. Auth is settled first (a bad token is
    `invalid_auth` with or without the arguments), which is why this table is all authenticated."""
    params = {
        k: (_a_channel_id(client, admin_h) if v == _OWN_CHANNEL else v) for k, v in params.items()
    }
    j = client.get(f"/slack/api/{method}", headers=admin_h, params=params).json()
    if error is None:
        assert j["ok"] is True
        return
    want = {"ok": False, "error": error}
    if missing:
        want["response_metadata"] = {
            "messages": [f"[ERROR] missing required field: {name}" for name in missing]
        }
    assert j == want


@pytest.mark.parametrize("method", ["search.messages", "search.all", "search.files"])
@pytest.mark.parametrize(
    "params, want",
    [
        ([("query", "the"), ("sort_dir", "ASC")], "enum"),
        ([("query", "the"), ("sort_dir", "bogus")], "enum"),
        ([("query", "the"), ("sort_dir", "Desc")], "enum"),
        ([("query", "the"), ("sort_dir", " asc")], "enum"),
        ([("query", "the"), ("sort_dir", "asc")], "ok"),
        ([("query", "the"), ("sort_dir", "desc")], "ok"),
        ([("query", "the"), ("sort_dir", "")], {"search.messages": "ok"}),
        ([("query", "the"), ("sort_dir", "asc"), ("sort_dir", "bogus")], "enum"),
        ([("query", "the"), ("sort_dir", "bogus"), ("sort_dir", "asc")], "ok"),
        ([("query", ""), ("sort_dir", "bogus")], "enum"),
        ([("sort_dir", "bogus")], "no query"),
    ],
)
def test_slack_search_sort_dir_outside_the_enum_is_invalid_arguments(
    client, admin_h, method, params, want
):
    """The rule the comment on `_SORT_DIRS` records, its order against `query` included."""
    if isinstance(want, dict):  # a row whose answer depends on the method
        want = want.get(method, "enum")
    j = client.get(f"/slack/api/{method}", headers=admin_h, params=[("count", 1), *params]).json()
    enum = {
        "ok": False,
        "error": "invalid_arguments",
        "response_metadata": {
            "messages": ["[ERROR] must be a valid enum value [json-pointer:/sort_dir]"]
        },
    }
    if want == "enum":
        assert j == enum
    elif want == "ok":
        assert j["ok"] is True
    else:
        assert j == {
            "ok": False,
            "error": "invalid_arguments",
            "response_metadata": {"messages": ["[ERROR] missing required field: query"]},
        }


_UNSET = "<the argument left out>"
_LIMIT_METHODS = [
    "conversations.list",
    "users.list",
    "conversations.members",
    "conversations.history",
]
_SEARCH_METHODS = ["search.messages", "search.all", "search.files"]
_CHANNEL_ARG = {m: [("channel", _OWN_CHANNEL)] for m in _LIMIT_METHODS[2:]}
_NUMBER_LINE = "[ERROR] must provide a number [json-pointer:/{}]".format
_SORT_DIR_LINE = "[ERROR] must be a valid enum value [json-pointer:/sort_dir]"
# Each value and the number real reads it as: `_UNSET` where real serves the page it serves with
# the argument left out, None where it answers `invalid_arguments` with one line naming the field.
# Of the four `limit` methods, users.list alone holds more than three items in the bundled corpus,
# so it is where 1, 2 and 3 differ from the page served without `limit`.
_LIMIT_VALUES = [
    *((v, _UNSET) for v in ("0", "00", "-0", ".5", "0.0", "1e-1", "1e400", "9" * 4301)),
    *((v, 2) for v in ("02", "002", "2.0", "2.9", "2e0", "2E0", "20e-1", "0.2e1", "2.5e0", "2\n")),
    ("1.5", 1),
    ("3.99999", 3),
    *(
        (v, None)
        for v in ("abc", "", "5abc", "0x10", " 2", "2 ", "\t2", "+2", "2.", "2e+0", "1_0")
        + ("\u0662", "\uff12", "Infinity", "NaN", "2\n\n", ".", "1e", "1.e1")
    ),
]
_INTEGER_VALUES = [
    *((v, int(v)) for v in ("0", "00", "02", "-1", "9223372036854775807", "-9223372036854775808")),
    *(
        (v, None)
        for v in ("abc", "", "5abc", "0x10", " 2", "2 ", "\t2", "+2", "-0", "-00", "-01", "2\n")
        + ("1_0", "\u0662", "\uff12", "2.0", "2.", "1.5", "1e1", "2e0", "9223372036854775808")
        + ("-9223372036854775809", "9" * 4301)
    ),
]
_PAGE_VALUES = [
    *((v, int(v)) for v in ("02", "00", "-1", "9223372036854775807")),
    *((v, None) for v in ("abc", "", "5abc", "0x10", "-0", "2\n", "+2", "2.0", "1.5", "1e2")),
    ("9223372036854775808", None),
]


def _number_rows():
    for m in _LIMIT_METHODS:
        for v, want in _LIMIT_VALUES:
            yield (
                m,
                [*_CHANNEL_ARG.get(m, []), ("limit", v)],
                [_NUMBER_LINE("limit")] if want is None else want,
            )
    for m in _SEARCH_METHODS:
        for v, n in _INTEGER_VALUES:
            yield m, [("query", "the"), ("count", v)], [_NUMBER_LINE("count")] if n is None else n
    for m in _SEARCH_METHODS:
        for v, n in _PAGE_VALUES:
            yield m, [("query", "the"), ("page", v)], [_NUMBER_LINE("page")] if n is None else n


_NUMERIC_ROWS = [
    *_number_rows(),
    # Real reads a repeated argument by its last value.
    ("users.list", [("limit", "1"), ("limit", "3")], 3),
    ("users.list", [("limit", "3"), ("limit", "1")], 1),
    # A missing required argument answers alone...
    ("conversations.history", [("limit", "abc")], ["[ERROR] missing required field: channel"]),
    ("search.messages", [("count", "x")], ["[ERROR] missing required field: query"]),
    # ...and a refused number answers ahead of the checks that follow it.
    (
        "conversations.history",
        [("channel", "C_NOPE"), ("limit", "abc")],
        [_NUMBER_LINE("limit")],
    ),
    (
        "conversations.history",
        [("channel", _OWN_CHANNEL), ("latest", "x"), ("limit", "abc")],
        [_NUMBER_LINE("limit")],
    ),
    (
        "conversations.history",
        [("channel", _OWN_CHANNEL), ("cursor", "zzz"), ("limit", "abc")],
        [_NUMBER_LINE("limit")],
    ),
    ("conversations.list", [("types", "bogus"), ("limit", "abc")], [_NUMBER_LINE("limit")]),
    ("conversations.list", [("cursor", "zzz"), ("limit", "abc")], [_NUMBER_LINE("limit")]),
    ("users.list", [("cursor", "zzz"), ("limit", "abc")], [_NUMBER_LINE("limit")]),
    ("users.list", [("sort_dir", "bogus"), ("limit", "abc")], [_NUMBER_LINE("limit")]),
    (
        "conversations.members",
        [("channel", "C_NOPE"), ("limit", "abc")],
        [_NUMBER_LINE("limit")],
    ),
    (
        "conversations.members",
        [("channel", _OWN_CHANNEL), ("cursor", "zzz"), ("limit", "abc")],
        [_NUMBER_LINE("limit")],
    ),
    *((m, [("query", " "), ("count", "abc")], [_NUMBER_LINE("count")]) for m in _SEARCH_METHODS),
    # Every refused number and an unknown `sort_dir` share one answer, in field order.
    *(
        (
            m,
            [("query", "the"), ("count", "abc"), ("page", "xyz"), ("sort_dir", "bogus")],
            [_NUMBER_LINE("count"), _NUMBER_LINE("page"), _SORT_DIR_LINE],
        )
        for m in _SEARCH_METHODS
    ),
    (
        "search.messages",
        [("sort_dir", "bogus"), ("page", "xyz"), ("count", "abc"), ("query", "the")],
        [_NUMBER_LINE("count"), _NUMBER_LINE("page"), _SORT_DIR_LINE],
    ),
]


def _numeric_cases():
    """Each row of :data:`_NUMERIC_ROWS` sent in the query, and the first number, refused and unset
    row of each method in a form and in a multipart body as well."""
    seen = set()
    for method, params, want in _NUMERIC_ROWS:
        yield method, params, want, "query"
        kind = "refused" if isinstance(want, list) else "unset" if want is _UNSET else "number"
        if (method, kind) not in seen:
            seen.add((method, kind))
            yield method, params, want, "form"
            yield method, params, want, "multipart"


@pytest.mark.parametrize("method, params, want, sent_as", list(_numeric_cases()))
def test_slack_numeric_arguments_are_read_as_real_reads_them(
    client, admin_h, method, params, want, sent_as
):
    """The rule on `_SLACK_INTEGER` and the order `_limit` and `_numbers` answer in, every row as
    slack.com/api answered it in the place ``sent_as`` puts the arguments. A row answered as a
    number is compared with the request that sends that number in its place, and an `_UNSET` row
    with the request that leaves the argument out, whose page is not empty: an empty page whose
    cursor names itself would keep a client that follows cursors on that page forever."""
    cid = _a_channel_id(client, admin_h)

    def get(ps):
        ps = [(k, cid if v == _OWN_CHANNEL else v) for k, v in ps]
        url = f"/slack/api/{method}"
        if sent_as == "query":
            r = client.get(url, headers=admin_h, params=ps)
        elif sent_as == "form":
            h = {**admin_h, "Content-Type": _FORM}
            r = client.post(url, headers=h, content=urllib.parse.urlencode(ps))
        else:
            r = client.post(
                url, headers={**admin_h, "Content-Type": _MULTIPART}, content=_parts(ps)
            )
        assert r.status_code == 200
        return r.json()

    got = get(params)
    if isinstance(want, list):
        assert got == {
            "ok": False,
            "error": "invalid_arguments",
            "response_metadata": {"messages": want},
        }
        return
    key = params[-1][0]
    rest = [p for p in params if p[0] != key]
    same = get(rest if want == _UNSET else [*rest, (key, str(want))])
    assert got["ok"] is True and got == same
    if key == "limit" and want == _UNSET:
        assert any(v for v in same.values() if isinstance(v, list)), same


_QUERY_FF_PART = b'--b\r\nContent-Disposition: form-data; name="query"\r\n\r\n\xff\r\n--b--\r\n'


@pytest.mark.parametrize(
    "method, params, content_type, body, credential, want",
    [
        # the body's value for a name ahead of the query string's
        ("conversations.list", [("limit", "abc")], _FORM, "limit=5", "admin", [("limit", "5")]),
        (
            "conversations.list",
            [("limit", "abc")],
            _MULTIPART,
            ("limit", "5"),
            "admin",
            [("limit", "5")],
        ),
        (
            "conversations.list",
            [("limit", "5")],
            _FORM,
            "limit=abc",
            "admin",
            [_NUMBER_LINE("limit")],
        ),
        # a name read as real reads it
        ("search.messages", [], _FORM, "query=the&sort.dir=bogus", "admin", [_SORT_DIR_LINE]),
        # a value that is not UTF-8
        ("conversations.history", [], _FORM, "channel=%FF", "admin", "channel_not_found"),
        ("conversations.list", [], _FORM, "limit=%FF", "admin", [_NUMBER_LINE("limit")]),
        ("users.info", [], _FORM, "user=%FF", "admin", "user_not_found"),
        *((m, [], _FORM, "query=%FF", "admin", "internal_error") for m in _SEARCH_METHODS),
        ("search.messages", [], _MULTIPART, _QUERY_FF_PART, "admin", "internal_error"),
        ("search.messages", [], _FORM, "query=%FF&count=abc", "admin", [_NUMBER_LINE("count")]),
        ("search.messages", [], _FORM, "query=%FF&sort_dir=bogus", "admin", [_SORT_DIR_LINE]),
        ("search.messages", [], _FORM, "query=%FF", None, "not_authed"),
        ("search.messages", [], _FORM, "query=%FF", "bogus", "invalid_auth"),
    ],
)
def test_slack_reads_an_argument_from_the_body_ahead_of_the_query(
    client, admin_h, method, params, content_type, body, credential, want
):
    """An argument sent in a POST body, urlencoded or as a multipart part (``body`` a (name, value)
    pair, or the body's bytes), under the name real reads it by, as real answered each row. ``want``
    is the error, the `invalid_arguments` lines, or the arguments a GET sends in the query string to
    be answered the same."""
    cid = _a_channel_id(client, admin_h)
    own = {_OWN_CHANNEL: cid}
    if isinstance(body, tuple):
        body = _one_part(body[0], own.get(body[1], body[1]))
    headers = {"content-type": content_type}
    if credential == "admin":
        headers.update(admin_h)
    elif credential == "bogus":
        headers["authorization"] = "Bearer xoxb-not-a-real-token"
    r = client.post(f"/slack/api/{method}", headers=headers, params=params, content=body)
    got = r.json()
    if isinstance(want, str):
        assert got == {"ok": False, "error": want}
    elif isinstance(want[0], str):
        assert got == {
            "ok": False,
            "error": "invalid_arguments",
            "response_metadata": {"messages": want},
        }
    else:
        query = [(k, own.get(v, v)) for k, v in want]
        assert got["ok"] is True
        assert got == client.get(f"/slack/api/{method}", headers=admin_h, params=query).json()


_EVERY = "<every item, as with no limit>"
_FATAL = "<fatal_error>"
# A channel of three messages and three members beside a second channel, so that one item, every
# item and none differ on each of the four methods.
_PAGED_SLACK = [
    *(
        {
            "source_type": "slack",
            "channel": "general",
            "content": f"message {n}",
            "author_email": f"{who}@x.com",
            "visibility": "public",
            "created": f"2026-03-0{n}T09:00:00Z",
        }
        for n, who in enumerate(("ava", "bo", "cy"), start=1)
    ),
    {
        "source_type": "slack",
        "channel": "random",
        "content": "hi",
        "author_email": "ava@x.com",
        "visibility": "public",
        "created": "2026-03-04T09:00:00Z",
    },
]


@pytest.fixture(scope="module")
def paged_slack(tmp_path_factory):
    s = tiny_corpus(tmp_path_factory.mktemp("paged-slack"), _PAGED_SLACK)
    with client_for(s, reload=True) as c:
        yield c, {"Authorization": f"Bearer {s.admin_token}"}


_NEGATIVE_PAST_A_FLOAT = ("-1e400", "-" + "9" * 4301)


@pytest.mark.parametrize(
    "method, limit, want",
    [
        *(
            (m, v, _EVERY)
            for m in ("conversations.list", "conversations.members")
            for v in ("-1", "-1.5", "-5", "-9223372036854775809", "-1e19", "1e19", "9.3e18")
            + _NEGATIVE_PAST_A_FLOAT
        ),
        *(
            ("conversations.history", v, 1)
            for v in ("-1", "-1.5", "-2", "-9223372036854775808", "-1e19", "1e19", "9.3e18")
            + _NEGATIVE_PAST_A_FLOAT
        ),
        *(("users.list", v, 0) for v in ("-1", "-1.5")),
        *(
            ("users.list", v, _FATAL)
            for v in ("-2", "-5", "-9223372036854775808", "-9223372036854775809", "-1e19")
            + ("9223372036854775808", "9.3e18", "1e19")
            + _NEGATIVE_PAST_A_FLOAT
        ),
        *(
            (m, v, _EVERY)
            for m in ("users.list", "conversations.history")
            for v in ("9223372036854775807", "9.2e18", "1e300")
        ),
        *((m, "1", 1) for m in ("conversations.list", "conversations.members", "users.list")),
    ],
)
def test_slack_negative_and_out_of_range_limits(paged_slack, method, limit, want):
    """A negative `limit`, and one past a signed 64-bit integer, as slack.com/api answered each:
    the rows of the measurement on `_limit`, with `limit=1` as the control."""
    client, admin_h = paged_slack
    channels = client.get("/slack/api/conversations.list", headers=admin_h).json()["channels"]
    general = next(ch["id"] for ch in channels if ch["name"] == "general")
    channel = (
        {"channel": general} if method in ("conversations.history", "conversations.members") else {}
    )

    def get(**ps):
        r = client.get(f"/slack/api/{method}", headers=admin_h, params={**channel, **ps})
        assert r.status_code == 200
        return r.json()

    got = get(limit=limit)
    if want == _FATAL:
        assert got == {"ok": False, "error": "fatal_error"}
        return
    assert got["ok"] is True
    if want == _EVERY:
        assert got == get()
    elif want == 0:  # no members, and the cursor `limit=1` would give
        assert got["members"] == []
        assert got["response_metadata"] == get(limit="1")["response_metadata"]
    else:  # one item, as `limit=1` serves, which is not every item
        assert got == get(limit="1") != get()


@pytest.mark.parametrize(
    "cursor, limit, error",
    [
        ("bogus", "-1", "invalid_cursor"),
        ("bogus", "-2", "invalid_cursor"),
        ("<the -1 page's>", "-1", "internal_error"),
        ("<the -1 page's>", "-1.5", "internal_error"),
        ("<the -1 page's>", "-5", "fatal_error"),
    ],
)
def test_slack_users_list_reads_the_cursor_before_a_negative_limit(
    client, admin_h, cursor, limit, error
):
    """The refusals `_limit` records for a negative `limit` beside a cursor on `users.list`."""
    users = "/slack/api/users.list"
    if cursor != "bogus":
        page = client.get(users, headers=admin_h, params={"limit": "-1"}).json()
        cursor = page["response_metadata"]["next_cursor"]
    r = client.get(users, headers=admin_h, params={"cursor": cursor, "limit": limit})
    assert r.json() == {"ok": False, "error": error}


def test_slack_search_all_answers_both_blocks_and_search_files_only_its_own(client, admin_h):
    # slack-go's Search()/SearchContext() hits search.all; it must return both messages + files.
    j = client.post("/slack/api/search.all", headers=admin_h, data={"query": "the"}).json()
    assert j["ok"] is True
    assert "messages" in j and "files" in j
    assert j["files"]["total"] == 0 and j["files"]["matches"] == []
    # search.files must not carry `messages`: a client reading it off a file search gets a
    # KeyError from the real API (see SlackFileSearch's docstring in backlot/routers/slack.py).
    f = client.post("/slack/api/search.files", headers=admin_h, data={"query": "the"}).json()
    assert set(f) == {"ok", "query", "files"}


def test_slack_replies_resolve_from_a_reply_ts(client, admin_h):
    # A search hit that lands on a REPLY yields that reply's ts; conversations.replies must return
    # the whole thread from it (Slack accepts any in-thread ts), not thread_not_found. The SAMPLE
    # 'incidents' 502 thread's replies include "Rolled back; 502s clearing." Without this,
    # replies resolved only thread ROOTS, so a search->replies chain broke whenever the hit was a
    # reply (the common case — real MCP clients pass the hit's own ts).
    sr = client.post(
        "/slack/api/search.messages", headers=admin_h, data={"query": "Rolled back"}
    ).json()
    matches = sr["messages"]["matches"]
    assert matches, "expected a slack search hit for the reply text"
    hit = next(m for m in matches if "Rolled back" in m["text"])
    assert "thread_ts" in hit, "a threaded search hit must carry its root thread_ts"
    rep = client.post(
        "/slack/api/conversations.replies",
        headers=admin_h,
        data={"channel": hit["channel"]["id"], "ts": hit["ts"]},
    ).json()
    assert rep.get("ok"), rep
    # No `latest` or `oldest` beside them, which `conversations.history` echoes.
    assert set(rep) == {"ok", "messages", "has_more"}
    texts = " ".join(m["text"] for m in rep["messages"])
    assert "Anyone else seeing 502s" in texts  # thread root is returned
    assert "Rolled back" in texts  # the reply we searched for is in the same thread


# --- Slack: enrichment did not change the responses ---------------------------------------


# Transcribed from live conversations.history responses, one per shape Slack builds. Backlot
# derives `blocks` from the same text a real client typed, so these are the payloads to match.
_LIVE_BLOCKS = {
    "test": [
        {
            "type": "rich_text",
            "elements": [
                {"type": "rich_text_section", "elements": [{"type": "text", "text": "test"}]}
            ],
        }
    ],
    "```code fence```": [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_preformatted",
                    "elements": [{"type": "text", "text": "code fence"}],
                    "border": 0,
                }
            ],
        }
    ],
    "\u2022 bullet 1\n\u2022 bullet 2": [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_list",
                    "style": "bullet",
                    "indent": 0,
                    "border": 0,
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [{"type": "text", "text": "bullet 1"}],
                        },
                        {
                            "type": "rich_text_section",
                            "elements": [{"type": "text", "text": "bullet 2"}],
                        },
                    ],
                }
            ],
        }
    ],
    "`inline` *bold*": [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "text", "text": "inline", "style": {"code": True}},
                        {"type": "text", "text": " "},
                        {"type": "text", "text": "bold", "style": {"bold": True}},
                    ],
                }
            ],
        }
    ],
}


@pytest.mark.parametrize("text", list(_LIVE_BLOCKS))
def test_slack_blocks_match_what_live_slack_builds_from_the_same_text(text):
    """`text` keeps the original markup in a real response, so `blocks` is a second rendering of
    the same string — which is why it can be derived rather than stored."""
    from backlot import synth

    got = synth.slack_blocks(text, "chan:1.0")
    assert got is not None
    # block_id is seeded, so compare everything else
    assert [{k: v for k, v in b.items() if k != "block_id"} for b in got] == _LIVE_BLOCKS[text]
    assert len(got[0]["block_id"]) == 5


def test_slack_blocks_are_absent_where_slack_does_not_build_them(client, admin_h):
    """A message Slack itself generated carries neither `blocks` nor `client_msg_id` — measured on
    channel_join, which answers only type/user/text/ts/subtype."""
    from backlot import synth

    assert synth.slack_blocks("", "s") is None
    assert synth.slack_blocks("   ", "s") is None
    # the block_id is stable for a given message and differs between messages
    assert synth.slack_block_id("a:1") == synth.slack_block_id("a:1")
    assert synth.slack_block_id("a:1") != synth.slack_block_id("a:2")


def test_slack_history_messages_carry_blocks(client, admin_h):
    """Every human-typed message the live API returns carries `blocks`; a client that renders rich
    text, or validates against a generated model, sees a shape real Slack never returns without."""
    cid = _a_channel_id(client, admin_h)
    msgs = client.get(
        "/slack/api/conversations.history", headers=admin_h, params={"channel": cid, "limit": 5}
    ).json()["messages"]
    assert msgs
    for m in msgs:
        assert m["blocks"][0]["type"] == "rich_text"
        assert m["blocks"][0]["block_id"]
        # the text field still carries the original string, blocks being the second rendering
        assert m["text"]
        assert "client_msg_id" in m


def _incident_root(client, headers):
    """The `incidents` thread root, as whoever `headers` authenticates."""
    chans = client.get(
        "/slack/api/conversations.list", headers=headers, params={"limit": 100}
    ).json()["channels"]
    cid = next(c["id"] for c in chans if c["name"] == "incidents")
    msgs = client.get(
        "/slack/api/conversations.history", headers=headers, params={"channel": cid, "limit": 50}
    ).json()["messages"]
    return cid, next(m for m in msgs if m.get("reply_count"))


# The fixture thread: bob started it, ava and bob replied. Slack subscribes you to a thread you
# started or replied in, so who is asking decides the answer — measured only in the affirmative
# (the one live token authored both root and replies and was answered `true`), so the negative
# rests on Slack's description of when it notifies you rather than on an observation.
@pytest.mark.parametrize(
    "email, expected",
    [
        ("bob@acme.com", True),  # started the thread
        ("ava@acme.com", True),  # replied in it
        ("hana@acme.com", False),  # neither
    ],
)
def test_slack_subscribed_is_the_callers_own_thread_state(client, tokens, email, expected):
    _cid, root = _incident_root(client, {"Authorization": f"Bearer {tokens[email]}"})
    assert root["subscribed"] is expected, email


def test_slack_a_service_token_subscribes_to_nothing(client, admin_h):
    """An admin/service token is not a person, so it follows no thread — the same reasoning that
    makes fireflies' `mine` empty for one."""
    _cid, root = _incident_root(client, admin_h)
    assert root["subscribed"] is False
    # a thread property rather than a per-caller one, and nothing in a corpus locks a thread
    assert root["is_locked"] is False


def test_slack_thread_root_carries_what_live_slack_adds_for_replies(client, admin_h):
    """Measured: in one live conversations.history response a root with replies answered 15 keys
    where a plain message answered 7 — the thread fields are ADDED, never a substitution."""
    cid, root = _incident_root(client, admin_h)
    plain = [
        m
        for m in client.get(
            "/slack/api/conversations.history",
            headers=admin_h,
            params={"channel": cid, "limit": 50},
        ).json()["messages"]
        if not m.get("reply_count")
    ]
    added = {
        "thread_ts",
        "reply_count",
        "reply_users",
        "reply_users_count",
        "latest_reply",
        "subscribed",
        "is_locked",
    }
    assert added <= set(root)
    if plain:
        # the root is a superset of a plain message, which is what the live response showed
        assert set(plain[0]) <= set(root)
        assert added & set(plain[0]) == set()


def test_slack_history_envelope_carries_the_channel_action_counters(client, admin_h):
    """Live Slack sends both keys on every conversations.history call rather than omitting them,
    so a client projecting the envelope sees them. Neither is on the method page."""
    cid = _a_channel_id(client, admin_h)
    j = client.get("/slack/api/conversations.history", headers=admin_h, params={"channel": cid})
    body = j.json()
    assert body["ok"] is True
    assert body["channel_actions_ts"] is None and body["channel_actions_count"] == 0
    assert body["pin_count"] == 0


def test_slack_history_omits_response_metadata_on_a_last_page(client, admin_h):
    """Measured: a live conversations.history with `has_more: false` carries no `response_metadata`
    at all — not the key with an empty cursor. A client that subscripts it to decide whether to
    keep paging must terminate on the key's ABSENCE, so serving it always hid that bug.

    conversations.list is the other way round: it carries `{"next_cursor": ""}` with nothing more,
    so the two are not one convention."""
    cid = _a_channel_id(client, admin_h)
    last = client.get(
        "/slack/api/conversations.history", headers=admin_h, params={"channel": cid, "limit": 100}
    ).json()
    assert last["has_more"] is False
    assert "response_metadata" not in last

    # a page that IS followed by another still names the cursor
    first = client.get(
        "/slack/api/conversations.history", headers=admin_h, params={"channel": cid, "limit": 1}
    ).json()
    if first["has_more"]:
        assert first["response_metadata"]["next_cursor"]

    # conversations.list keeps the key even when exhausted, which is what live Slack does
    lst = client.get("/slack/api/conversations.list", headers=admin_h, params={"limit": 100}).json()
    assert lst["response_metadata"]["next_cursor"] == ""


def test_slack_client_msg_id_is_a_v4_uuid(client, admin_h):
    """Measured: every live value is canonical 8-4-4-4-12 with a v4's version nibble and variant
    bits, because the posting client generates it. A 16-hex token satisfied anything that only
    read the field and failed anything that validated it."""
    import re
    import uuid

    cid = _a_channel_id(client, admin_h)
    msgs = client.get(
        "/slack/api/conversations.history", headers=admin_h, params={"channel": cid, "limit": 20}
    ).json()["messages"]
    assert msgs
    seen = set()
    for m in msgs:
        got = m["client_msg_id"]
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", got
        ), got
        assert uuid.UUID(got).version == 4
        seen.add(got)
    # globally unique, which is what the (channel, ts) seed buys
    assert len(seen) == len(msgs)


def test_slack_responses_unchanged_by_enrichment(client, admin_h):
    lst = client.get("/slack/api/conversations.list", headers=admin_h).json()
    assert lst["ok"] and "channels" in lst and "response_metadata" in lst
    if lst["channels"]:
        ch = lst["channels"][0]
        for k in (
            "id",
            "name",
            "is_private",
            "is_member",
            "num_members",
            "topic",
            "purpose",
            "created",
            "creator",
        ):
            assert k in ch, f"slack channel missing {k} (fidelity regression)"
    srch = client.get(
        "/slack/api/search.messages", params={"query": "gateway"}, headers=admin_h
    ).json()
    assert srch["ok"] and "messages" in srch and "matches" in srch["messages"]


def test_slack_api_test_has_typed_response_schema(client):
    # api.test is a new endpoint (readers probe it on connect); enrich it like its siblings.
    op = client.get("/openapi.json").json()["paths"]["/slack/api/api.test"]["get"]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema != {}
    assert "$ref" in schema or schema.get("type") in ("object", "array")


# --- Slack -----------------------------------------------------------------------


def test_slack_reply_users_and_num_members(tmp_path):
    from backlot.routers.slack import _listed_channel, _message

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "doc_id": "s1",
                "channel": "inc",
                "content": "root",
                "author_email": "bob@x.com",
                "visibility": "public",
                "replies": [
                    {"content": "a", "author_email": "ava@x.com"},
                    {"content": "b", "author_email": "cid@x.com"},
                    {"content": "c", "author_email": "ava@x.com"},
                ],
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    # A thread is addressed by (channel, the root's ts) — a slack message's own key.
    channel, root_ts = conn.execute(
        "SELECT channel, ts FROM slack_messages WHERE thread_seq = 0"
    ).fetchone()
    thread = store.slack_thread(conn, channel, root_ts)
    root, first_reply = thread[0], thread[1]
    ru = store.slack_reply_authors(conn, channel, root_ts)
    ruids = [synth.slack_user_id(e) for e in ru]
    rootmsg = _message(root, reply_count=3, reply_users=ruids, reply_users_count=len(ru))
    # 3 replies but only 2 distinct repliers -> counts differ (real Slack distinguishes them)
    assert rootmsg["reply_count"] == 3 and rootmsg["reply_users_count"] == 2
    assert len(rootmsg["reply_users"]) == 2
    # a reply carries parent_user_id pointing at the root author
    rep = _message(first_reply, parent_user_id=synth.slack_user_id("bob@x.com"))
    assert rep["parent_user_id"] == synth.slack_user_id("bob@x.com")
    # conversations.list channel object reports a real member count (was hardcoded 0)
    import types

    from backlot.acl import Caller

    req = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace()))
    ch = _listed_channel(req, conn, "inc", Caller(email="ava@x.com", is_admin=False))
    assert ch["num_members"] > 0 and ch["creator"] == "USERVICE0"
    # is_member is the CALLER's own membership (was a constant True): ava replied in #inc, and
    # nobody at all speaks for a service token
    assert ch["is_member"] is True
    absent = _listed_channel(req, conn, "inc", Caller(email="dee@x.com", is_admin=False))
    assert absent["is_member"] is False
    assert (
        _listed_channel(req, conn, "inc", Caller(email=None, is_admin=True))["is_member"] is False
    )


def test_slack_channel_predicates_answer_the_same_warm_or_cold(tmp_path):
    """`_is_private` and `_channel_visibility` each read the warm channel_acl cache with a query
    behind them for the window before it lands, and conversations.list routes both `types` and ACL
    scoping through them. Two implementations of one predicate that disagreed would be a listing
    that changes shape when the background warm-up finishes, so they are pinned against each other:
    the cached `request` here holds what `main.lifespan` builds."""
    import types

    from backlot.acl import Acl, Caller
    from backlot.routers.slack import _channel_visibility, _is_private

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "channel": "open",
                "content": "morning all",
                "author_email": "ava@x.com",
                "visibility": "public",
            },
            {
                "source_type": "slack",
                "channel": "open",
                "content": "morning",
                "author_email": "bo@x.com",
                "visibility": "public",
            },
            {
                "source_type": "slack",
                "channel": "closed",
                "content": "the comp band lands at 240k",
                "author_email": "ava@x.com",
                "readers": ["user:ava@x.com"],
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    acl = Acl.load(s.tokens_path, s.admin_token, s.org_name)
    grants: dict[str, set] = {}
    for ch, pid in conn.execute("SELECT DISTINCT channel, principal_id FROM slack_acl"):
        grants.setdefault(ch, set()).add(pid)

    def request(channel_acl):
        state = types.SimpleNamespace(acl=acl, conn=conn)
        if channel_acl is not None:
            state.channel_acl = channel_acl
        return types.SimpleNamespace(app=types.SimpleNamespace(state=state))

    warm = request({k: frozenset(v) for k, v in grants.items()})
    cold = request(None)  # the warm-up has not landed yet, so every consumer queries
    for req in (warm, cold):
        assert _is_private(req, conn, "open") is False
        assert _is_private(req, conn, "closed") is True
        for email, sees_closed in (("ava@x.com", True), ("bo@x.com", False)):
            ids = acl.visible_ids(conn, Caller(email=email, is_admin=False))
            visible = _channel_visibility(req, conn, ids)
            assert visible("open") is True, email
            assert visible("closed") is sees_closed, email
        # an admin/service token is scoped by nothing at all
        assert all(_channel_visibility(req, conn, None)(n) for n in ("open", "closed"))


def test_slack_a_system_message_carries_no_client_composed_fields(tmp_path):
    """`client_msg_id` is minted by the posting client and `blocks` is what that client composed,
    so a message Slack itself generated has neither — measured on channel_join, which answers only
    type/user/text/ts/subtype. Reachable from a BYO corpus, which is what states a subtype: the ERB
    importer writes none."""
    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "doc_id": "sys",
                "channel": "inc",
                "subtype": "channel_join",
                "content": "<@U123> has joined the channel",
                "author_email": "bob@x.com",
                "visibility": "public",
            },
            {
                "source_type": "slack",
                "doc_id": "human",
                "channel": "inc",
                "content": "shipped it",
                "author_email": "ava@x.com",
                "visibility": "public",
            },
        ],
    )
    from backlot.routers.slack import _message

    conn = store.connect_ro(s.db_path)
    rows = {r["content"]: r for r in conn.execute("SELECT * FROM slack_messages")}

    sys_msg = _message(rows["<@U123> has joined the channel"])
    assert sys_msg["subtype"] == "channel_join"
    assert "client_msg_id" not in sys_msg and "blocks" not in sys_msg
    # `team` stays on both: it is absent from a live channel_join too, but a bot_message is a real
    # posted message and none was available to measure, so it is not dropped on the strength of one
    assert sys_msg["team"]

    human = _message(rows["shipped it"])
    assert human["client_msg_id"] and human["blocks"][0]["type"] == "rich_text"


def test_thread_ts_and_latest_reply_follow_a_replys_own_clock(tmp_path):
    """A reply may carry its own `created`, the treatment a gmail message gets. Its ts is that
    second, its thread_ts is the ROOT's ts, and the root's latest_reply names the last reply's
    real second."""
    from datetime import datetime

    from backlot.routers.slack import _message

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "doc_id": "s1",
                "channel": "inc",
                "content": "root",
                "author_email": "bob@x.com",
                "visibility": "public",
                "created": "2026-05-01T00:00:00Z",
                "replies": [
                    {"content": "ack", "author_email": "ava@x.com"},
                    {
                        "content": "the real answer, hours later",
                        "author_email": "cid@x.com",
                        "created": "2026-05-01T03:00:00Z",
                    },
                ],
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    channel, root_ts = conn.execute(
        "SELECT channel, ts FROM slack_messages WHERE thread_seq = 0"
    ).fetchone()
    thread = store.slack_thread(conn, channel, root_ts)
    root, late = thread[0], thread[2]
    base = int(datetime.fromisoformat("2026-05-01T00:00:00+00:00").timestamp())
    # the late reply's ts is its own second, not root + position
    assert late["ts"].split(".")[0] == str(base + 3 * 3600)
    # its thread_ts is the root's ts — position arithmetic would land 2s early
    assert _message(late)["thread_ts"] == root["ts"]
    # latest_reply is the late reply's real ts, not root + reply count
    assert store.slack_latest_reply_ts(conn, channel, root_ts) == late["ts"]
    # the replies endpoint's fast path (resolve a public ts by its second) finds it
    hits = store.slack_messages_at_created_ts(conn, "inc", base + 3 * 3600)
    assert any(r["ts"] == late["ts"] for r in hits)


def test_every_in_thread_message_serves_the_roots_ts(tmp_path):
    """Both endpoints that serve in-thread rows — the thread itself and a page of search hits
    inside one — report the root's ts as `thread_ts`, so the value a client is handed is the one
    it fetches the thread with."""
    records = [
        {
            "source_type": "slack",
            "doc_id": "s-big",
            "channel": "inc",
            "content": "root of a long thread",
            "author_email": "bob@x.com",
            "visibility": "public",
            "created": "2026-02-10T18:00:00Z",
            "replies": [{"content": f"reply {i}", "author_email": "ava@x.com"} for i in range(30)],
        },
    ]
    with corpus_client(tmp_path, records) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        cid = synth.slack_channel_id("inc")
        hist = client.get(
            "/slack/api/conversations.history", headers=h, params={"channel": cid, "limit": 100}
        ).json()
        root_ts = hist["messages"][0]["ts"]

        rep = client.get(
            "/slack/api/conversations.replies",
            headers=h,
            params={"channel": cid, "ts": root_ts, "limit": 100},
        ).json()
        assert len(rep["messages"]) == 31
        assert {m["thread_ts"] for m in rep["messages"]} == {root_ts}

        found = client.get(
            "/slack/api/search.messages", headers=h, params={"query": "reply", "count": 100}
        ).json()["messages"]["matches"]
        assert len(found) >= 30
        assert {m["thread_ts"] for m in found} == {root_ts}


def test_thread_rooted_at_epoch_zero_stays_coherent(tmp_path):
    """1970-01-01T00:00:00Z is a second a corpus can write, and it stores as 0. The root serves
    that second and every message in the thread points at it, so a client can fetch the thread
    with the thread_ts it was handed."""
    from backlot.routers.slack import _message

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "doc_id": "s-zero",
                "channel": "inc",
                "content": "root",
                "author_email": "bob@x.com",
                "visibility": "public",
                "created": 0,
                "replies": [
                    {"content": "r1", "author_email": "ava@x.com", "created": 1},
                    {"content": "r2", "author_email": "ava@x.com", "created": 5000},
                ],
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    channel, root_ts = conn.execute(
        "SELECT channel, ts FROM slack_messages WHERE thread_seq = 0"
    ).fetchone()
    thread = store.slack_thread(conn, channel, root_ts)
    assert [r["created_ts"] for r in thread] == [0, 1, 5000]
    # the root serves its own second, not a hash of the id the corpus gave it
    assert thread[0]["ts"].split(".")[0] == "0"
    # and every message in the thread points at that same root ts
    assert {_message(r)["thread_ts"] for r in thread} == {thread[0]["ts"]}


def test_slack_chronology_is_numeric_not_lexicographic(tmp_path):
    """A ts is TEXT (`"<seconds>.<fraction>"`, the spelling Slack's own API uses), so ordering by
    it compares digit by digit and puts second 9 after second 10. Every slack listing orders by
    the integer second first, with the ts breaking ties so an offset page still cannot skip a row.

    Seconds 9 and 10 rather than something larger: a digit-count change is the whole failure, and
    the 1970 era where one happens is a corpus this project supports."""
    from backlot.routers.slack import _message

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "doc_id": "s-root",
                "channel": "inc",
                "content": "root",
                "author_email": "bob@x.com",
                "visibility": "public",
                "created": 0,
                "replies": [
                    {"content": "at nine", "author_email": "ava@x.com", "created": 9},
                    {"content": "at ten", "author_email": "ava@x.com", "created": 10},
                ],
            },
            {
                "source_type": "slack",
                "doc_id": "s-nine",
                "channel": "inc",
                "content": "standalone at nine",
                "author_email": "bob@x.com",
                "visibility": "public",
                "created": 9,
            },
            {
                "source_type": "slack",
                "doc_id": "s-ten",
                "channel": "inc",
                "content": "standalone at ten",
                "author_email": "bob@x.com",
                "visibility": "public",
                "created": 10,
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    listed = store.list_slack_top_level(conn, "inc")
    assert [r["created_ts"] for r in listed] == [0, 9, 10]
    # latest_reply names the LAST reply, which a lexicographic MAX(ts) misses for the same reason
    root = next(r for r in listed if r["thread_ts"] is not None)
    thread = store.slack_thread(conn, "inc", root["ts"])
    latest = store.slack_latest_reply_ts(conn, "inc", root["ts"])
    assert latest == thread[-1]["ts"] and thread[-1]["created_ts"] == 10
    assert _message(root, reply_count=2, latest_reply=latest)["latest_reply"] == latest


def test_slack_a_private_channels_members_are_its_readers(tmp_path):
    """Slack lists a private channel ONLY to the people in it, so there "may read it" and "is in
    it" are one fact: `is_private: true` beside `is_member: false`, with a full history behind it,
    is not a shape a client can be written against. A public channel is the other way round —
    everyone may read it and membership is who has posted, which the test above covers.

    A reader who has never posted is the ordinary case rather than a corner: on a 19,693-document
    corpus, 22 of its 27 private channels have at least one, and `num_members` under-reported by
    the same margin."""
    from backlot.acl import Acl

    records = [
        {
            "source_type": "slack",
            "channel": "board-comp",
            "content": "the comp band lands at 240k",
            "author_email": "ava@acme.com",
            "readers": ["user:ava@acme.com", "user:bo@acme.com"],  # bo reads, never posts
        },
        {
            "source_type": "slack",
            "channel": "general",
            "content": "morning all",
            "author_email": "ava@acme.com",
            "visibility": "public",
        },
    ]
    with corpus_client(tmp_path, records) as (client, settings):
        token = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
        bo = {"Authorization": f"Bearer {token.email_to_token()['bo@acme.com']}"}
        listed = {
            c["name"]: c
            for c in client.get(
                "/slack/api/conversations.list", headers=bo, params=_EVERY_CHANNEL
            ).json()["channels"]
        }
        assert set(listed) == {"board-comp", "general"}

        private, public = listed["board-comp"], listed["general"]
        assert private["is_private"] is True and private["is_member"] is True
        # ...while the public channel bo has never posted in is one bo is not in
        assert public["is_private"] is False and public["is_member"] is False

        for channel in (private, public):
            members = client.get(
                "/slack/api/conversations.members",
                headers=bo,
                params={"channel": channel["id"], "limit": 100},
            ).json()["members"]
            # num_members counts whatever that channel's membership is, so stat-then-walk agrees
            assert channel["num_members"] == len(members), channel["name"]
            # ...and is_member is the caller's own place in exactly that list
            assert channel["is_member"] is (synth.slack_user_id("bo@acme.com") in members)

        assert client.get(
            "/slack/api/conversations.members", headers=bo, params={"channel": private["id"]}
        ).json()["members"] == [synth.slack_user_id(e) for e in ("ava@acme.com", "bo@acme.com")]
        # the membership is not a courtesy: bo really does read the channel
        assert client.get(
            "/slack/api/conversations.history", headers=bo, params={"channel": private["id"]}
        ).json()["messages"], "a member of a private channel reads it"


def test_slack_one_person_has_one_handle_across_the_surface(tmp_path):
    """`users.list` names a person by `name` and `search.messages` names the same person by
    `username`, and Slack spells both the handle: this method's own reference example carries
    `"username": "roach"` beside `"user": "U2U85N1RV"`
    (`docs.slack.dev/reference/methods/search.messages`). A client that reads the
    id off `user` and the label off `username` gets one person, so the two must not answer two
    spellings of them.

    A dotted address is what separates the two derivations — the handle drops the dot, and the raw
    local part does not — and no address in the module's shared fixture has one, so the case is
    stated here rather than assumed.
    """
    records = [
        {
            "source_type": "slack",
            "channel": "incidents",
            "content": "gateway is throwing 502s",
            "author_email": "ava.chen@acme.com",
            "visibility": "public",
        }
    ]
    with corpus_client(tmp_path, records) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        member = next(
            m
            for m in client.post("/slack/api/users.list", headers=h).json()["members"]
            if m["profile"]["email"] == "ava.chen@acme.com"
        )
        (hit,) = client.post(
            "/slack/api/search.messages", headers=h, params={"query": "gateway"}
        ).json()["messages"]["matches"]

        assert hit["user"] == member["id"]  # the id already agreed
        assert hit["username"] == member["name"] == "avachen"
        assert "." not in hit["username"], "a handle drops the dot the address carries"


def test_slack_reaction_ids_and_count_are_derived_from_the_addresses(tmp_path):
    """The corpus names the reactors; the response carries what Slack carries.

    `count` is the length of the rendered list, which is the true count here because Backlot serves
    every reactor a record names; real Slack may send a larger one, truncating `users`. The schema
    refuses a stated count and a repeated address, so the addresses are the only thing written down
    and the number stays the number of people.

    The ids are `synth.slack_user_id` of each address, which is the same value `users.list` and a
    message's own `user` report for that person, so a client that groups a reaction's users against
    message authors gets one answer rather than two.
    """

    from backlot.routers.slack import _message

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "channel": "inc",
                "content": "gateway is flapping",
                "author_email": "ava@x.com",
                "visibility": "public",
                "reactions": [
                    # No record states a `count`; the schema refuses one. So every count below
                    # is one the response worked out from the addresses beside it.
                    {"name": "eyes", "users": ["ava@x.com", "bo@x.com"]},
                    {"name": "+1", "users": ["bo@x.com"]},
                ],
            }
        ],
    )
    conn = store.connect_ro(s.db_path)
    row = store.list_slack_top_level(conn, "inc")[0]
    reactions = _message(row)["reactions"]

    assert reactions == [
        {
            "name": "eyes",
            "users": [synth.slack_user_id("ava@x.com"), synth.slack_user_id("bo@x.com")],
            "count": 2,
        },
        {"name": "+1", "users": [synth.slack_user_id("bo@x.com")], "count": 1},
    ]
    assert all(USER_ID.fullmatch(u) for r in reactions for u in r["users"])


def test_slack_edited_renders_the_editors_id(tmp_path):
    """`edited.user` is rendered the same way `reactions.users` is: the corpus names the editor by
    address and `synth.slack_user_id` mints the id Slack answers there. `ts` is passed
    through unchanged — it is information only the corpus holds, not a value to derive.

    A reply carries the same block, and `_message` is the single renderer for history, replies and
    search hits, so both are read here off the rows each is served from. The editor is the author
    on both, which is the only pairing real Slack produces (see `_check_edited`).
    """

    from backlot.routers.slack import _message

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "channel": "inc",
                "content": "gateway is flapping",
                "author_email": "ava@x.com",
                "visibility": "public",
                "created": "2026-03-01T09:00:00Z",
                "edited": {"user": "ava@x.com", "ts": "1772614801.000000"},
                "replies": [
                    {
                        "content": "rolling back",
                        "author_email": "bo@x.com",
                        "created": "2026-03-01T09:01:00Z",
                        "edited": {"user": "bo@x.com", "ts": "1772614899.000000"},
                    }
                ],
            }
        ],
    )
    conn = store.connect_ro(s.db_path)
    root = store.list_slack_top_level(conn, "inc")[0]
    reply = store.slack_thread(conn, "inc", root["thread_ts"])[1]

    assert _message(root)["edited"] == {
        "user": synth.slack_user_id("ava@x.com"),
        "ts": "1772614801.000000",
    }
    assert _message(reply)["edited"] == {
        "user": synth.slack_user_id("bo@x.com"),
        "ts": "1772614899.000000",
    }
    for row in (root, reply):
        m = _message(row)
        assert USER_ID.fullmatch(m["edited"]["user"])
        # The editor is the author on the surface a client reads, which is the identity Slack's
        # own message-event example shows (`user` and `edited.user` are both `U123ABC456`).
        assert m["edited"]["user"] == m["user"]


def test_slack_file_renders_its_owner_id(tmp_path):
    """`user` on a file is rendered the same way `edited.user` and `reactions.users` are: the
    corpus names it by address and `synth.slack_user_id` mints the id. Slack's file object
    reference types `user` as "the ID of the user who created the object", and a live
    `search.files` call (2026-09-21) found one on every file the workspace served. Every other
    file field — `id`, `name`, `mimetype`, `title` — passes through unchanged.
    """

    from backlot.routers.slack import _message

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "channel": "inc",
                "content": "here's the graph",
                "author_email": "ava@x.com",
                "visibility": "public",
                "files": [
                    {
                        "id": "F1",
                        "name": "graph.png",
                        "mimetype": "image/png",
                        "user": "ava@x.com",
                    }
                ],
            }
        ],
    )
    conn = store.connect_ro(s.db_path)
    row = store.list_slack_top_level(conn, "inc")[0]
    files = _message(row)["files"]

    assert files == [
        {
            "id": "F1",
            "name": "graph.png",
            "mimetype": "image/png",
            "user": synth.slack_user_id("ava@x.com"),
        }
    ]
    assert USER_ID.fullmatch(files[0]["user"])


def test_slack_has_2fa_answers_an_admin_caller_or_the_callers_own_member_and_only_a_person(
    tmp_path,
):
    """`has_2fa` is gated by deactivation like the other ten fields, and within that carries two
    conditions of its own: the caller is an admin or is the member, and the member is not a bot —
    see `_user_obj` for the measurement behind both.

    It is not an ACL oracle: what it says about a member reaches an admin and that member, and it
    is the only key of the object that moves with the caller."""
    settings = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "channel": "incidents",
                "content": "rolling back the deploy now",
                "author_email": "ava@acme.com",
            },
            {
                "source_type": "slack",
                "channel": "incidents",
                "content": "thanks ava",
                "author_email": "bo@acme.com",
            },
            {
                "source_type": "slack",
                "channel": "incidents",
                "content": "deploy 41f2 is live",
                "author_email": "deploybot@acme.com",
            },
        ],
    )
    # BYO registers every author as a principal, and a speaker is only `is_bot` while they are not
    # one — the shape a corpus whose roster is narrower than its transcripts has, which `users.list`
    # describes. Dropping the principal is what leaves the speaker.
    conn = store.connect_rw(settings.db_path)
    conn.execute("DELETE FROM principals WHERE id = ?", ("deploybot@acme.com",))
    conn.commit()
    conn.close()

    # `reload=True`: resolving a speaker's id builds `app.state._slack_uid_map` once, and the
    # module-level app carries whichever corpus built it first.
    with client_for(settings, reload=True) as client:
        tokens = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))
        admin_h = {"Authorization": f"Bearer {tokens['admin_token']}"}
        ava_h = {"Authorization": f"Bearer {tok(tokens, 'ava@acme.com')}"}
        ava_uid, bo_uid = synth.slack_user_id("ava@acme.com"), synth.slack_user_id("bo@acme.com")

        def info(headers, uid):
            return client.get(
                "/slack/api/users.info", headers=headers, params={"user": uid}
            ).json()["user"]

        # the caller reading themselves and someone else, and the admin token reading both
        own, as_admin = info(ava_h, ava_uid), info(admin_h, ava_uid)
        other, other_as_admin = info(ava_h, bo_uid), info(admin_h, bo_uid)
        assert "has_2fa" in own and "has_2fa" in as_admin and own.keys() == as_admin.keys()
        assert "has_2fa" not in other and "has_2fa" in other_as_admin
        assert other_as_admin.keys() - {"has_2fa"} == other.keys()

        def listed(headers):
            members = client.get("/slack/api/users.list", headers=headers).json()["members"]
            return {u["profile"]["email"]: u for u in members}

        by_ava, by_admin = listed(ava_h), listed(admin_h)
        assert "has_2fa" in by_ava["ava@acme.com"] and "has_2fa" not in by_ava["bo@acme.com"]
        assert "has_2fa" in by_admin["ava@acme.com"] and "has_2fa" in by_admin["bo@acme.com"]

        bot = info(admin_h, synth.slack_user_id("deploybot@acme.com"))
        assert bot["is_bot"] is True
        assert "real_name" in bot and "has_2fa" not in bot


def test_slack_deactivation_changes_every_slack_answer_about_a_member_and_nothing_else(tmp_path):
    """A roster's `deactivated: true` (`backlot.importer.byo.load_roster`): the person is
    `deleted: true`, dropped from the membership of both kinds of channel though their messages
    stay in history, and refused `account_inactive` on their own Slack token while a non-Slack
    route still answers it. `is_forgotten` stays unserved either way, and their user object drops
    the fields `_user_obj` gates on deactivation. See `_user_obj` and
    `slack_private_channel_members` for the measurement behind each."""
    settings = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "channel": "incidents",
                "content": "rolling back the deploy now",
                "author_email": "ava@acme.com",
            },
            {
                "source_type": "slack",
                "channel": "incidents",
                "content": "thanks ava",
                "author_email": "bo@acme.com",
            },
            {
                "source_type": "slack",
                "channel": "board-comp",
                "content": "the comp band lands at 240k",
                "author_email": "ava@acme.com",
                "readers": ["user:ava@acme.com", "user:bo@acme.com"],
            },
        ],
    )
    conn = store.connect_rw(settings.db_path)
    conn.execute("INSERT INTO slack_deactivated_users VALUES (?)", ("ava@acme.com",))
    conn.commit()
    conn.close()

    with client_for(settings) as client:
        tokens = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))
        admin_h = {"Authorization": f"Bearer {tokens['admin_token']}"}
        ava_h = {"Authorization": f"Bearer {tok(tokens, 'ava@acme.com')}"}
        bo_h = {"Authorization": f"Bearer {tok(tokens, 'bo@acme.com')}"}
        ava_uid, bo_uid = synth.slack_user_id("ava@acme.com"), synth.slack_user_id("bo@acme.com")

        by_email = {
            u["profile"]["email"]: u
            for u in client.get("/slack/api/users.list", headers=admin_h).json()["members"]
        }
        assert by_email["ava@acme.com"]["deleted"] is True
        assert "is_forgotten" not in by_email["ava@acme.com"]
        assert by_email["bo@acme.com"]["deleted"] is False
        assert "is_forgotten" not in by_email["bo@acme.com"]

        # See _user_obj's comment for the measurement behind this set.
        active_only = {
            "real_name",
            "is_admin",
            "is_owner",
            "is_primary_owner",
            "is_restricted",
            "is_ultra_restricted",
            "tz",
            "tz_label",
            "tz_offset",
            "color",
        }
        assert active_only.isdisjoint(by_email["ava@acme.com"])
        assert active_only <= by_email["bo@acme.com"].keys()
        # has_2fa follows the same split here; see _user_obj for why it's asserted separately.
        assert "has_2fa" not in by_email["ava@acme.com"]
        assert "has_2fa" in by_email["bo@acme.com"]

        info = client.get(
            "/slack/api/users.info", headers=admin_h, params={"user": ava_uid}
        ).json()["user"]
        assert info["deleted"] is True
        assert active_only.isdisjoint(info) and "has_2fa" not in info

        listed = {
            c["name"]: c
            for c in client.get(
                "/slack/api/conversations.list", headers=admin_h, params=_EVERY_CHANNEL
            ).json()["channels"]
        }
        assert listed["board-comp"]["is_private"] is True
        assert listed["incidents"]["is_private"] is False
        # The two kinds of channel derive membership from different facts — speakers for a public
        # one, grantees for a private one — so deactivation has to be excluded from both.
        for name in ("incidents", "board-comp"):
            channel = listed[name]
            members = client.get(
                "/slack/api/conversations.members",
                headers=admin_h,
                params={"channel": channel["id"], "limit": 10},
            ).json()["members"]
            assert members == [bo_uid], name
            assert channel["num_members"] == len(members), name
            history = client.get(
                "/slack/api/conversations.history",
                headers=admin_h,
                params={"channel": channel["id"]},
            ).json()["messages"]
            assert any(m["user"] == ava_uid for m in history), (
                f"deactivation must not erase the person's messages from #{name}"
            )

        # `num_members` above came from the warm cache; the window before it lands counts each
        # channel on its own, and the two cannot answer differently.
        client.app.state.warm_thread.join()
        warm, client.app.state.channel_members = client.app.state.channel_members, None
        try:
            for name in ("incidents", "board-comp"):
                cold = client.get(
                    "/slack/api/conversations.info",
                    headers=admin_h,
                    params={"channel": listed[name]["id"], "include_num_members": "true"},
                ).json()["channel"]
                assert cold["num_members"] == warm[name] == 1, name
        finally:
            client.app.state.channel_members = warm

        refused = client.post("/slack/api/auth.test", headers=ava_h).json()
        assert refused == {"ok": False, "error": "account_inactive"}
        # ...before any handler, so a read refuses the same way rather than answering a page
        assert client.get("/slack/api/users.list", headers=ava_h).json()["error"] == (
            "account_inactive"
        )
        # ...and only Slack draws it: the roster field is Slack's own state, not a suspension
        assert client.get("/gmail/v1/users/me/profile", headers=ava_h).status_code == 200
        assert client.post("/slack/api/auth.test", headers=bo_h).json()["ok"] is True

    conn = store.connect_ro(settings.db_path)
    try:
        # `is_member` asks this one about the caller alone, where the refusal above reaches first,
        # so nothing served can tell it from the membership it claims to answer a person about.
        assert store.slack_channel_has_author(conn, "incidents", "ava@acme.com") is False
        assert store.slack_channel_has_author(conn, "incidents", "bo@acme.com") is True
    finally:
        conn.close()


@pytest.mark.parametrize(
    "verb, method, content_type, body, want_warning",
    [
        ("POST", "auth.test", "application/json", b"{}", "missing_charset"),
        ("POST", "auth.test", "application/json", b"", None),
        ("POST", "auth.test", "text/plain", b"{}", "missing_charset"),
        ("POST", "auth.test", "text/plain", b"", None),
        ("POST", "auth.test", "text/plain; charset=utf-8", b"{}", None),
        ("POST", "auth.test", "application/json; charset=", b"{}", "missing_charset"),
        ("POST", "auth.test", 'application/json; charset=""', b"{}", "missing_charset"),
        ("POST", "auth.test", "application/json; CHARSET=utf-8", b"{}", "missing_charset"),
        ("POST", "auth.test", "application/json; charset =utf-8", b"{}", "missing_charset"),
        ("POST", "auth.test", "application/json; charset= utf-8", b"{}", "missing_charset"),
        ("POST", "auth.test", "application/json; charset=utf-8", b"{}", None),
        ("POST", "auth.test", "application/json;charset=UTF-8", b"{}", None),
        ("POST", "auth.test", "application/json; charset=iso-8859-1", b"{}", None),
        ("POST", "auth.test", 'application/json; charset="utf-8"', b"{}", None),
        ("POST", "auth.test", _FORM, b"", None),
        ("POST", "auth.test", f"{_FORM}; charset=utf-8", b"", "superfluous_charset"),
        ("POST", "auth.test", f"{_FORM};charset=UTF-8", b"", "superfluous_charset"),
        ("POST", "auth.test", f"{_FORM}; charset=iso-8859-1", b"", "superfluous_charset"),
        ("POST", "auth.test", f'{_FORM}; charset="utf-8"', b"", "superfluous_charset"),
        ("POST", "auth.test", f"{_FORM}; charset=", b"", None),
        ("POST", "auth.test", f"{_FORM}; CHARSET=utf-8", b"", None),
        ("POST", "auth.test", f"{_FORM.upper()}; charset=utf-8", b"", None),
        ("POST", "auth.test", f"{_MULTIPART}; charset=utf-8", _ONE_PART, "superfluous_charset"),
        ("POST", "auth.test", _MULTIPART, _ONE_PART, None),
        ("POST", "auth.test", None, b"", None),
        ("GET", "auth.test", "application/json", None, None),
        ("GET", "auth.test", f"{_FORM}; charset=utf-8", None, None),
        ("POST", "api.test", "application/json", b"{}", "missing_charset"),
        ("POST", "api.test", f"{_FORM}; charset=utf-8", b"", "superfluous_charset"),
        ("POST", "conversations.list", "application/json", b"{}", "missing_charset"),
        ("POST", "conversations.history", "application/json", b"{}", "missing_charset"),
        ("POST", "conversations.history", f"{_FORM}; charset=utf-8", b"", "superfluous_charset"),
    ],
)
def test_slack_post_charset_warnings(
    client, admin_h, verb, method, content_type, body, want_warning
):
    """The warning ``slack.post_charset_warning`` decides, sent with a body each media type parses,
    where ``slack.attach_warning`` puts it: last, after whatever `response_metadata` holds. Rendered
    as every Slack answer is, warning or not, with a `content-length` that counts the bytes sent."""
    headers = dict(admin_h)
    if content_type is not None:
        headers["content-type"] = content_type
    r = client.request(verb, f"/slack/api/{method}", headers=headers, content=body)
    j = r.json()
    assert r.content == JSONResponse(j).body
    assert r.headers["content-length"] == str(len(r.content))
    if want_warning is None:
        assert "warning" not in j
        assert "warnings" not in j.get("response_metadata", {})
    else:
        assert list(j)[-2:] == ["warning", "response_metadata"]
        assert j["warning"] == want_warning
        assert list(j["response_metadata"].items())[-1] == ("warnings", [want_warning])


@pytest.mark.parametrize(
    "method, token, content_type, body, want",
    [
        (
            "conversations.history",
            "admin",
            "application/json",
            b"{}",
            {
                "ok": False,
                "error": "invalid_arguments",
                "warning": "missing_charset",
                "response_metadata": {
                    "messages": ["[ERROR] missing required field: channel"],
                    "warnings": ["missing_charset"],
                },
            },
        ),
        (
            "auth.test",
            None,
            "application/json",
            b"{}",
            {
                "ok": False,
                "error": "not_authed",
                "warning": "missing_charset",
                "response_metadata": {"warnings": ["missing_charset"]},
            },
        ),
        (
            "auth.test",
            "bad-token",
            f"{_FORM}; charset=utf-8",
            b"",
            {
                "ok": False,
                "error": "invalid_auth",
                "warning": "superfluous_charset",
                "response_metadata": {"warnings": ["superfluous_charset"]},
            },
        ),
    ],
)
def test_slack_post_charset_warning_on_a_refusal(
    client, admin_h, method, token, content_type, body, want
):
    """A refusal the method answers is warned as an answer is, where ``slack.attach_warning`` puts
    the two keys. The bytes are ``want`` rendered, so the order of its members is asserted as well
    as their values."""
    headers = {"content-type": content_type}
    if token == "admin":
        headers.update(admin_h)
    elif token is not None:
        headers["authorization"] = f"Bearer {token}"
    r = client.post(f"/slack/api/{method}", headers=headers, content=body)
    assert r.content == JSONResponse(want).body


@pytest.mark.parametrize(
    "verb, content_type, body, want",
    [
        # No Content-Type, or an empty one
        ("POST", None, b"{}", "missing_post_type"),
        ("POST", None, b"", {}),
        ("POST", "", b"{}", "missing_post_type"),
        # The media type, as sent; a form type read as one passes
        ("POST", "application/x-www-form-urlencoded\xa0", b"a=b", ({"a": "b"}, "missing_charset")),
        ("POST", "application/x-www-form-urlencoded\x85", b"a=b", ({"a": "b"}, "missing_charset")),
        ("POST", "\xa0application/x-www-form-urlencoded", b"a=b", "invalid_post_type"),
        (
            "POST",
            "text/plain; boundary=b",
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            ({}, "missing_charset"),
        ),
        ("POST", "text/html", b"{}", "invalid_post_type"),
        ("POST", "text/html", b"", {}),
        ("POST", "APPLICATION/JSON", b"{}", "invalid_post_type"),
        ("POST", "MULTIPART/FORM-DATA; boundary=b", b"{}", "invalid_post_type"),
        ("POST", "application/json; CHARSET=latin1", b"{}", ({}, "missing_charset")),
        ("GET", "text/html", b"{}", {}),
        (
            "POST",
            [("content-type", _FORM), ("content-type", "application/json")],
            b"a=b",
            ({"a": "b"}, "missing_charset"),
        ),
        (
            "POST",
            [("content-type", "application/json"), ("content-type", _FORM)],
            b"a=b",
            "invalid_post_type",
        ),
        # a header left empty or blank is dropped before the two are joined
        *(
            ("POST", [("content-type", first), ("content-type", second)], body, want)
            for first, second, body, want in (
                ("", _FORM, b"a=b", {"a": "b"}),
                (_FORM, "", b"a=b", {"a": "b"}),
                ("\t", _FORM, b"a=b", {"a": "b"}),
                ("   ", _FORM, b"a=b", {"a": "b"}),
                ("", "application/json", b"{}", ({}, "missing_charset")),
                ("application/json", "", b"{}", ({}, "missing_charset")),
                ("", _MULTIPART, _one_part("a", "b"), {"a": "b"}),
                ("", "", b"{}", "missing_post_type"),
            )
        ),
        ("POST", ", " + _FORM, b"a=b", "invalid_post_type"),
        ("POST", "  application/x-www-form-urlencoded  ", b"a=b", {"a": "b"}),
        ("POST", "application/json, text/html", b"{}", "invalid_post_type"),
        ("POST", "application/json; Charset=latin1", b"{}", ({}, "missing_charset")),
        ("POST", "APPLICATION/X-WWW-FORM-URLENCODED", b"a=b", ({"a": "b"}, "missing_charset")),
        ("POST", "application/x-www-form-urlencodedx", b"a=b", ({"a": "b"}, "missing_charset")),
        ("POST", "application/x-www-form-urlencoded, text/html", b"{}", "invalid_post_type"),
        ("POST", "application/x-www-form-urlencoded\t; charset=latin1", b"a=b", "invalid_charset"),
        ("POST", "application/x-www-form-urlencoded, text/html", b"a{=b", "invalid_arg_name"),
        (
            "POST",
            "MULTIPART/FORM-DATA; boundary=b",
            b"--b\r\nContent-Disposition: form-data\r\n\r\nhi\r\n--b--\r\n",
            "invalid_post_type",
        ),
        # The charset
        ("POST", "application/json;\xa0charset=latin1", b"{}", ({}, "missing_charset")),
        ("POST", "application/json;\tcharset=latin1", b"{}", ({}, "missing_charset")),
        ("POST", "application/json; charset=utf8", b"{}", {}),
        (
            "POST",
            "multipart/form-data; boundary=b; charset=latin1",
            b"",
            ({}, "superfluous_charset"),
        ),
        ("POST", "application/json ;charset=utf-8", b"{}", "invalid_post_type"),
        ("POST", "application/json; charset=latin1", b"{}", "invalid_charset"),
        ("POST", "application/json; charset=latin1", b"", "invalid_charset"),
        ("POST", "application/json; charset=ISO-8859-1", b"{}", {}),
        ("POST", 'application/json; charset="latin1"', b"{}", "invalid_charset"),
        ("POST", 'application/json; charset="utf-8"', b"{}", {}),
        ("POST", "text/html; charset=latin1", b"", "invalid_charset"),
        ("POST", "text/html; charset=latin1", b"{}", "invalid_post_type"),
        ("POST", "application/json; charset=latin1; charset=utf-8", b"{}", "invalid_charset"),
        ("POST", "application/json; charset=utf-8; charset=latin1", b"{}", {}),
        ("POST", "application/json; charset= latin1", b"{}", ({}, "missing_charset")),
        ("POST", "foo; charset=latin1", b"", "invalid_charset"),
        ("POST", "application/json; charset=; charset=latin1", b"{}", "invalid_charset"),
        # A form body
        ("POST", _FORM, b"9223372036854775807=c", {}),
        ("POST", _FORM, b"9223372036854775808=c", {"9223372036854775808": "c"}),
        pytest.param(
            "POST",
            _FORM,
            b"a=" + b"x" * 1572862,
            {"a": "x" * 1572862},
            id="form-value-1572862-bytes",
        ),
        pytest.param(
            "POST",
            _MULTIPART,
            _one_part("a", "x" * 1572864),
            {"a": "x" * 1572864},
            id="part-1572864-bytes",
        ),
        ("POST", _FORM, b"{}", "invalid_form_data"),
        ("POST", _FORM, b"a", "invalid_form_data"),
        ("POST", _FORM, b"=b", "invalid_form_data"),
        ("POST", _FORM, b"a{=b", "invalid_arg_name"),
        ("POST", _FORM, b"a[b]=c", "invalid_array_arg"),
        ("POST", _FORM, b"", {}),
        ("POST", _FORM, b"a=%zz", {"a": "%zz"}),
        ("POST", _FORM, b"a=%E2%82", {"a": None}),
        ("POST", "application/x-www-form-urlencoded; charset=latin1", b"{}", "invalid_form_data"),
        (
            "POST",
            "application/x-www-form-urlencoded; charset=bogus",
            b"a=b",
            ({"a": "b"}, "superfluous_charset"),
        ),
        ("POST", _FORM, b"a=b&c", {"a": "b"}),
        ("POST", _FORM, b"a==b", {"a": "=b"}),
        ("POST", _FORM, b"a=b&a=c", {"a": "c"}),
        ("POST", _FORM, b"a[]=c", "invalid_array_arg"),
        ("POST", _FORM, b"a]=c", "invalid_arg_name"),
        ("POST", _FORM, b"a[=c", {"a_": "c"}),
        ("POST", _FORM, b"[a]=c", "invalid_form_data"),
        ("POST", _FORM, b"a%5Bb%5D=c", "invalid_array_arg"),
        ("POST", _FORM, b"+=c", "invalid_form_data"),
        ("POST", _FORM, b"a+b=c", {"a_b": "c"}),
        ("POST", _FORM, b"a.b=c", {"a_b": "c"}),
        ("POST", _FORM, b"a-b=c", {"a-b": "c"}),
        ("POST", _FORM, b"1=c", {}),
        ("POST", _FORM, b"%C3%BC=c", "invalid_arg_name"),
        ("POST", _FORM, b"a=b+c", {"a": "b c"}),
        ("POST", _FORM, b"a=%00", {"a": "\x00"}),
        ("POST", _FORM, b"c[d]=e&a{=b", "invalid_arg_name"),
        ("POST", _FORM, b"=x&a[b]=c", "invalid_array_arg"),
        ("POST", _FORM, b"a=b\nc=d", {"a": "b\nc=d"}),
        ("POST", _FORM, b"a\x00b=c", {"a": "c"}),
        ("POST", _FORM, b"a%2Bb=c", "invalid_arg_name"),
        ("POST", _FORM, b" a=c", {"a": "c"}),
        (
            "POST",
            _FORM,
            b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=c",
            "invalid_arg_name",
        ),
        ("POST", _FORM, b"a[b]=c&a=d", {"a": "d"}),
        ("POST", _FORM, b"a=d&a[b]=c", "invalid_array_arg"),
        ("POST", _FORM, b"a[b.c=d", "invalid_arg_name"),
        ("POST", _FORM, b"%00=c", "invalid_form_data"),
        ("POST", _FORM, b"a%00[b]=c", {"a": "c"}),
        ("POST", _FORM, b"1[a]=b", "invalid_array_arg"),
        # A multipart body
        (
            "POST",
            "multipart/form-data;\xa0boundary=b",
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            "multipart/form-data;\tboundary=b",
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            "multipart/form-data; xboundary=b",
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            "multipart/form-data; foo=1boundary=b",
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            "multipart/form-data; boundary=b; boundary=c",
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            "multipart/form-data; boundary =b",
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            "multipart/form-data; boundary= b",
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            "invalid_form_data",
        ),
        (
            "POST",
            "multipart/form-data; boundary=b c",
            b'--b c\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b c--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            "multipart/form-data; boundary=b c",
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            "invalid_form_data",
        ),
        (
            "POST",
            "multipart/form-data; boundary=b,c",
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            "multipart/form-data; boundary=b,c",
            b'--b,c\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b,c--\r\n',
            "invalid_form_data",
        ),
        (
            "POST",
            "multipart/form-data; boundary=b;c",
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            'multipart/form-data; boundary="b;c"',
            b'--b;c\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b;c--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            "multipart/form-data; Boundary=b",
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            "invalid_form_data",
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\nContent-Disposition: form-data; name="a"\n\nb\n\n--b--\n',
            {"a": "b\n"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\nContent-Disposition: form-data; name="a"\n\nb\n\n--b\nContent-Disposition: form-data; name="c"\n\nd\n--b--\n',
            {"a": "b\n", "c": "d"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"\r\nContent-Type: text/plain; name="zz"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"; name\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"; foo="x"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"; filename\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"; filename=""\r\n\r\nb\r\n--b--\r\n',
            {},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"; FILENAME="x"\r\n\r\nb\r\n--b--\r\n',
            {},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; NAME="a"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nc\r\n--b--\r\n',
            {"a": "c"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="f"; filename="x.txt"\r\nContent-Type: text/plain\r\n\r\nhello\r\n--b--\r\n',
            {},
        ),
        (
            "POST",
            _MULTIPART,
            b"--b\r\nContent-Disposition: form-data\r\n\r\nb\r\n--b--\r\n",
            "invalid_form_data",
        ),
        (
            "POST",
            _MULTIPART,
            b"--b\r\nContent-Type: text/plain\r\n\r\nb\r\n--b--\r\n",
            "invalid_form_data",
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a[b]"\r\n\r\nc\r\n--b--\r\n',
            "invalid_array_arg",
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a.b"\r\n\r\nc\r\n--b--\r\n',
            {"a_b": "c"},
        ),
        (
            "POST",
            _MULTIPART,
            b"--b\r\nContent-Disposition: form-data; name=a\r\n\r\nb\r\n--b--\r\n",
            {"a": "b"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\ncontent-disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            'multipart/form-data; boundary="b"',
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\nContent-Disposition: form-data; name="a"\n\nb\n--b--\n',
            {"a": "b"},
        ),
        (
            "POST",
            _MULTIPART,
            b'junk\r\n--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\ntrailing junk',
            {"a": "b"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\n\xff\r\n--b--\r\n',
            {"a": None},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nx\r\ny\r\n--b--\r\n',
            {"a": "x\r\ny"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb+c%41\r\n--b--\r\n',
            {"a": "b+c%41"},
        ),
        ("POST", _MULTIPART, b"", {}),
        ("POST", _MULTIPART, b"--b--\r\n", "invalid_form_data"),
        (
            "POST",
            "multipart/form-data",
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            "invalid_form_data",
        ),
        (
            "POST",
            "multipart/form-data; boundary=",
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            "invalid_form_data",
        ),
        *(
            ("POST", content_type, body, want)
            for content_type, body, want in (
                ("multipart/form-data; boundary=", b"--\r\n" + _PART_A + b"----\r\n", {"a": "b"}),
                ('multipart/form-data; boundary=""', b"--\r\n" + _PART_A + b"----\r\n", {"a": "b"}),
                (
                    "multipart/form-data; boundary=; x=y",
                    b"--\r\n" + _PART_A + b"----\r\n",
                    {"a": "b"},
                ),
                (
                    "multipart/form-data; boundary=",
                    b'--\nContent-Disposition: form-data; name="a"\n\nb\n----\n',
                    {"a": "b"},
                ),
                ("multipart/form-data; boundary=", b"--\r\n" + _PART_A, {"a": "b"}),
                (
                    "multipart/form-data; boundary=",
                    b"--\r\n" + _PART_A + b"--\r\n" + _PART_C + b"----\r\n",
                    {"a": "b", "c": "d"},
                ),
                (
                    "multipart/form-data",
                    b"--multipart/form-data\r\n" + _PART_A + b"--multipart/form-data--\r\n",
                    "invalid_form_data",
                ),
                ("multipart/form-data; x=", b"--\r\n" + _PART_A + b"----\r\n", "invalid_form_data"),
            )
        ),
        # a line that starts with the delimiter and holds more (:func:`slack._multipart_parts`)
        *(
            ("POST", _MULTIPART, b"--b\r\n" + _PART_A + line + _PART_C + tail, want)
            for line, tail, want in (
                (b"--bx\r\n", b"--b\r\n" + _PART_E + b"--b--\r\n", {"a": "b", "e": "f"}),
                (b"--b  \r\n", b"--b\r\n" + _PART_E + b"--b--\r\n", {"a": "b", "e": "f"}),
                (b"--bx\r\n", b"--b--\r\n", {"a": "b"}),
            )
        ),
        # the headerless-part, first-line and end-of-body rules of ``slack._multipart_parts``
        *(
            ("POST", _MULTIPART, body, want)
            for body, want in (
                (b"--b\r\n\r\n--b\r\n" + _PART_A + b"--b--\r\n", {"a": "b"}),
                (b"--b\r\n\r\nvalue\r\n--b\r\n" + _PART_A + b"--b--\r\n", {"a": "b"}),
                (b"--b\r\n" + _PART_A + b"--b\r\n\r\n", {"a": "b"}),
                (b"--b\r\n" + _PART_A + b"--b", {"a": "b"}),
                (b"--b\r\n" + _PART_A + b"--b\n", {"a": "b"}),
                (b"--b", "invalid_form_data"),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\n--bz\r\n--b--\r\n',
                    {"a": "--bz"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\n--b--\r\n',
                    {"a": "--b--"},
                ),
                (b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\r', {"a": "v\r"}),
            )
        ),
        # the `Content-Disposition` rules ``slack._multipart_parts`` names, and the file rule
        # ``slack._register`` does
        *(
            ("POST", _MULTIPART, body, want)
            for body, want in (
                (b'--b\r\nContent-Disposition: name="a"\r\n\r\nv\r\n--b--\r\n', {"a": "v"}),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a;b"\r\n\r\nv\r\n--b--\r\n',
                    "invalid_arg_name",
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="c"; filename="x"\r\n\r\nf\r\n'
                    b'--b\r\nContent-Disposition: form-data; name="c"\r\n\r\nw\r\n--b--\r\n',
                    {},
                ),
            )
        ),
        # a part HHVM's `rfc1867PostHandler` names otherwise (``slack._multipart_parts``): an empty
        # name, no Content-Disposition, a filename alone, neither parameter, two of the header
        *(
            ("POST", _MULTIPART, body, want)
            for body, want in (
                (b"--b\r\n" + _PART_EMPTY + b"--b\r\n" + _PART_A + b"--b--\r\n", {"a": "b"}),
                (b"--b\r\n" + _PART_A + b"--b\r\n" + _PART_EMPTY + b"--b--\r\n", {"a": "b"}),
                (
                    b"--b\r\n"
                    + _PART_A
                    + b"--b\r\nX-Other: y\r\n\r\nz\r\n--b\r\n"
                    + _PART_C
                    + b"--b--\r\n",
                    {"a": "b", "c": "d"},
                ),
                (b'--b\r\nContent-Disposition: form-data; filename="f"\r\n\r\nx\r\n--b--\r\n', {}),
                (
                    b"--b\r\n"
                    + _PART_A
                    + b"--b\r\nContent-Disposition: form-data\r\n\r\nx\r\n--b\r\n"
                    + _PART_C
                    + b"--b--\r\n",
                    {"a": "b"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a"\r\nContent-Disposition: form-data; name="c"\r\n\r\nv\r\n--b--\r\n',
                    {"a": "v"},
                ),
            )
        ),
        # headers that end without a blank line, and a line with no `:`, which adds to the header
        # above it or, with none above, is ignored (``slack._Rfc1867Buffer.headers``)
        *(
            ("POST", _MULTIPART, body, want)
            for body, want in (
                (
                    b'--b\r\nContent-Disposition: form-data; name="a"\r\n--b--\r\n',
                    "invalid_arg_name",
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a"\r\nX: y\r\n--b--\r\n',
                    {"a": ""},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a"\r\nfoo\r\n--b--\r\n',
                    "invalid_arg_name",
                ),
                (b'--b\r\nContent-Disposition: form-data; name="a"\r\nfoo', {"a": "foo"}),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a"\r\nfoo\r\n\r\nb\r\n--b--\r\n',
                    "invalid_arg_name",
                ),
                (b"--b\r\n--b\r\n" + _PART_A + b"--b--\r\n", {"a": "b"}),
                (b"--b\r\n--bx\r\n" + _PART_A + b"--b--\r\n", {"a": "b"}),
                (
                    b'--b\r\n Content-Disposition: form-data; name="a"\r\n\r\nv\r\n--b--\r\n',
                    "invalid_form_data",
                ),
                (b"--b\r\nX:\r\n" + _PART_A + b"--b--\r\n", {"a": "b"}),
            )
        ),
        # a parameter's value as `php_ap_getword` and `php_ap_getword_conf` read it
        # (``slack._getword``, ``slack._getword_conf``)
        *(
            (
                "POST",
                _MULTIPART,
                b"--b\r\nContent-Disposition: form-data" + params + b"\r\n\r\nv\r\n--b--\r\n",
                want,
            )
            for params, want in (
                (b"; name='a'", {"a": "v"}),
                (b'; name="a\\"b"', "invalid_arg_name"),
                (b'; name="a\\\\b"', "invalid_arg_name"),
                (b"; name=a b", {"a": "v"}),
                (b'; name="a" x', "invalid_arg_name"),
                (b';; name="a"', {"a": "v"}),
            )
        ),
        # a file's part as HHVM takes or passes over one, and the names it protects
        # (``slack._multipart_parts``): the bracket rule, the twenty-upload cap, an empty filename,
        # a protected name's spelling
        *(
            ("POST", _MULTIPART, body, want)
            for body, want in (
                (_file("a") + _field("a[b]") + b"--b--\r\n", "invalid_array_arg"),
                (_file("a[b]") + _field("a") + b"--b--\r\n", {"a": "v"}),
                (_file("a]") + _field("a]") + b"--b--\r\n", "invalid_arg_name"),
                (_file("a]") + b"--b--\r\n", "invalid_form_data"),
                (_file("a[b") + _field("a_b") + b"--b--\r\n", {"a_b": "v"}),
                (_file("a[b]c") + _field("a[b]") + b"--b--\r\n", "invalid_array_arg"),
                (_file("a") + _field("a.b") + b"--b--\r\n", {"a_b": "v"}),
                (_file("a.b") + _field("a_b") + b"--b--\r\n", {}),
                (_file("a[tmp_name]") + _field("a") + b"--b--\r\n", {"a": "v"}),
                (
                    b"".join(_file(f"f{i}") for i in range(21))
                    + _field("f20")
                    + _field("f0", "w")
                    + b"--b--\r\n",
                    {"f20": "v"},
                ),
                (
                    b"".join(_file(f"f{i}") for i in range(20))
                    + _field("f19")
                    + _field("f0", "w")
                    + b"--b--\r\n",
                    {},
                ),
                (_file("f", "") + _field("f") + b"--b--\r\n", {}),
                (
                    b'--b\r\nContent-Disposition: form-data; name="f"; filename=""\r\n\r\n--b\r\n'
                    + _field("a")[5:]
                    + b"--b--\r\n",
                    {"a": "v"},
                ),
                (
                    _file("a]", "x", "v" * 5120)[:-2] + b"--b\r\n" + _field("a")[5:] + b"--b--\r\n",
                    {"a": "v"},
                ),
            )
        ),
        # the media types HHVM's `IsRfc1867` reads as multipart (``slack._rfc1867_boundary``), and
        # the tab Slack's own media-type check refuses
        *(
            ("POST", content_type, b"--b\r\n" + _PART_A + b"--b--\r\n", want)
            for content_type, want in (
                ("multipart/form; boundary=b", ({"a": "b"}, "missing_charset")),
                ("multipart/form-datax; boundary=b", "invalid_post_type"),
                ("; boundary=b", ({"a": "b"}, "missing_charset")),
                ("m; boundary=b", ({"a": "b"}, "missing_charset")),
                ("multipart/form-data ; boundary=b", ({"a": "b"}, "missing_charset")),
                ("multipart/form-data;boundary=b", {"a": "b"}),
                ('multipart/form-data; boundary="b', "invalid_form_data"),
                ("text/plain; boundary=b", ({}, "missing_charset")),
                ("multipart/form-data\t; boundary=b", "invalid_post_type"),
            )
        ),
        (
            "POST",
            "multipart/form-data; BOUNDARY=b",
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            "invalid_form_data",
        ),
        (
            "POST",
            _MULTIPART,
            b'--b  \r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            "invalid_form_data",
        ),
        (
            "POST",
            "multipart/form-data; boundary=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            b'--xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx--\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data\r\n\r\nx\r\n--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--\r\n',
            "invalid_form_data",
        ),
        ("POST", "multipart/form-data", b"", {}),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a{"; filename="x"\r\n\r\nhi\r\n--b--\r\n',
            {},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b--',
            {"a": "b"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n\r\n--b--\r\n',
            {"a": "b\r\n"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--b\r\n',
            {"a": "b"},
        ),
        (
            "POST",
            "multipart/form-data; boundary=b; boundary=c",
            b'--c\r\nContent-Disposition: form-data; name="a"\r\n\r\nb\r\n--c--\r\n',
            "invalid_form_data",
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: form-data; name="a"; name="c"\r\n\r\nb\r\n--b--\r\n',
            {"c": "b"},
        ),
        (
            "POST",
            _MULTIPART,
            b'--b\r\nContent-Disposition: attachment; name="a"\r\n\r\nb\r\n--b--\r\n',
            {"a": "b"},
        ),
        # A part's header lines, its quoting, and a form part named after a file part before it
        *(
            ("POST", _MULTIPART, body, want)
            for body, want in (
                (
                    b'--b\r\nContent-Disposition:\r\n\r\nz\r\n--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\r\n--b--\r\n',
                    {"a": "v"},
                ),
                (
                    b"--b\r\nContent-Disposition: form-data; name='a;b'\r\n\r\nv\r\n--b--\r\n",
                    "invalid_arg_name",
                ),
                (
                    b"--b\r\nContent-Disposition: form-data; name='a' x\r\n\r\nv\r\n--b--\r\n",
                    "invalid_arg_name",
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[b"; filename="x"\r\n\r\nf\r\n--b\r\nContent-Disposition: form-data; name="a[b"\r\n\r\nv\r\n--b--\r\n',
                    {},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[b]"; filename="x"\r\n\r\nf\r\n--b\r\nContent-Disposition: form-data; name="a[tmp_name][b]"\r\n\r\nv\r\n--b--\r\n',
                    {},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a"\r\n\tfoo:bar\r\n\r\nv\r\n--b--\r\n',
                    "invalid_arg_name",
                ),
                (
                    b'--b\r\nContent-Disposition:\r\nX: y\r\n\r\nz\r\n--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\r\n--b--\r\n',
                    {"a": "v"},
                ),
                (
                    b'--b\r\nContent-Disposition:\r\n form-data; name="z"\r\n\r\nq\r\n--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\r\n--b--\r\n',
                    {"a": "v"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\r\n--b\r\nContent-Disposition: form-data; name="c"\r\n\r\nw\r\n--',
                    {"a": "v", "c": "w"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\r\n--b\r\nContent-Disposition: form-data; name="c"\r\n\r\nw\r\n-',
                    {"a": "v", "c": "w"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\r\n--b\r\nContent-Disposition: form-data; name="c"\r\n\r\nw\r\n',
                    {"a": "v", "c": "w"},
                ),
                (b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\n-', {"a": "-"}),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a\r\n\r\nv\r\n--b--\r\n',
                    {"a": "v"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="x\\"; filename="f"\r\n\r\nv\r\n--b--\r\n',
                    "invalid_arg_name",
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a"\rb\r\n\r\nv\r\n--b--\r\n',
                    {"a": "v"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[b\\"\r]"\r\n\r\nv\r\n--b--\r\n',
                    "invalid_array_arg",
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a\\\r\n\r\nv\r\n--b--\r\n',
                    "invalid_arg_name",
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a";\r\n\r\nv\r\n--b--\r\n',
                    {"a": "v"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name= "a"\r\n\r\nv\r\n--b--\r\n',
                    {"a": "v"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name=\r\n\r\nq\r\n--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\r\n--b--\r\n',
                    {"a": "v"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a\\\\b"; filename="x"\r\n\r\nf\r\n--b\r\nContent-Disposition: form-data; name="a\\b"\r\n\r\nv\r\n--b--\r\n',
                    {},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a\\xb"\r\n\r\nv\r\n--b--\r\n',
                    "invalid_arg_name",
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a\\"b"; filename="x"\r\n\r\nf\r\n--b\r\nContent-Disposition: form-data; name=\'a"b\'\r\n\r\nv\r\n--b--\r\n',
                    {},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[b"; filename="x"\r\n\r\nf\r\n--b\r\nContent-Disposition: form-data; name="a[c"\r\n\r\nv\r\n--b--\r\n',
                    {"a_c": "v"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[b][ c]"; filename="x"\r\n\r\nf\r\n--b\r\nContent-Disposition: form-data; name="a[b][c]"\r\n\r\nv\r\n--b--\r\n',
                    {},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[b][c]"; filename="x"\r\n\r\nf\r\n--b\r\nContent-Disposition: form-data; name="a[b][d]"\r\n\r\nv\r\n--b--\r\n',
                    "invalid_array_arg",
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[b"; filename="x"\r\n\r\nf\r\n--b\r\nContent-Disposition: form-data; name="a[b[tmp_name]"\r\n\r\nv\r\n--b--\r\n',
                    {},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a"; filename="x"\r\n\r\nf\r\n--b\r\nContent-Disposition: form-data; name="a[tmp_name]"\r\n\r\nv\r\n--b--\r\n',
                    {},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[b]"; filename="x"\r\n\r\nf\r\n--b\r\nContent-Disposition: form-data; name="a[b]"\r\n\r\nv\r\n--b--\r\n',
                    {},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a"; filename="x"\r\n\r\n--b\x00x\r\nContent-Disposition: form-data; name="c"\r\n\r\nw\r\n--b--\r\n',
                    {},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a]"\r\n\r\nv\r\n--b--\r\n',
                    "invalid_arg_name",
                ),
            )
        ),
        (
            "POST",
            'multipart/form-data; boundary="b',
            b'--\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\r\n----\r\n',
            "invalid_form_data",
        ),
        ("POST", "text/html; charset=bogus", b"x", "invalid_post_type"),
        (
            "POST",
            "multipart/form-data; boundary=bbbb",
            b'--bbbb\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\r\n--bb',
            {"a": "v"},
        ),
        # a value's name as HHVM registers it, spelt as a protected name is, and the same names in a
        # form; a header line with no name before the part's Content-Disposition
        *(
            ("POST", _MULTIPART, body, want)
            for body, want in (
                (
                    b'--b\r\nContent-Disposition: form-data; name="a["\r\n\r\nv\r\n--b--\r\n',
                    {"a_": "v"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[ "\r\n\r\nv\r\n--b\r\nContent-Disposition: form-data; name="c"\r\n\r\nw\r\n--b--\r\n',
                    {"a_": "v", "c": "w"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a"; filename="x"\r\n\r\nf\r\n--b\r\nContent-Disposition: form-data; name="a["\r\n\r\nv\r\n--b--\r\n',
                    {"a_": "v"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a["; filename="x"\r\n\r\nf\r\n--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\r\n--b--\r\n',
                    {"a": "v"},
                ),
                (
                    b"--b\r\n:x; name=q\r\nContent-Disposition: form-data\r\n\r\nv\r\n--b--\r\n",
                    {"qform-data": "v"},
                ),
                (
                    b"--b\r\n:name=q\r\nContent-Disposition: form-data; x=y\r\n\r\nv\r\n--b--\r\n",
                    {"qform-data": "v"},
                ),
                (
                    b"--b\r\nContent-Disposition: form-data; name=a\\\r\n\r\nv\r\n--b--\r\n",
                    "invalid_arg_name",
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[ "\r\n\r\nv\r\n--b--\r\n',
                    {"a_": "v"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[b "\r\n\r\nv\r\n--b--\r\n',
                    "invalid_arg_name",
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[ b"\r\n\r\nv\r\n--b--\r\n',
                    {"a_b": "v"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[  "\r\n\r\nv\r\n--b--\r\n',
                    {"a_": "v"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[\t"\r\n\r\nv\r\n--b--\r\n',
                    {"a_": "v"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[b\t"\r\n\r\nv\r\n--b--\r\n',
                    "invalid_arg_name",
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[b. "\r\n\r\nv\r\n--b--\r\n',
                    "invalid_arg_name",
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[b c"\r\n\r\nv\r\n--b--\r\n',
                    "invalid_arg_name",
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[b[ "\r\n\r\nv\r\n--b--\r\n',
                    "invalid_arg_name",
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a "\r\n\r\nv\r\n--b--\r\n',
                    {"a_": "v"},
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[b] "\r\n\r\nv\r\n--b--\r\n',
                    "invalid_array_arg",
                ),
                (
                    b'--b\r\nContent-Disposition: form-data; name="a[]x "\r\n\r\nv\r\n--b--\r\n',
                    "invalid_array_arg",
                ),
            )
        ),
        (
            "POST",
            "multipart/form-data; boundary=bbbb",
            b'--bbbb\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\n--xy',
            {"a": "v\n--xy"},
        ),
        ("POST", _FORM, b"a[%20=v", "invalid_arg_name"),
        ("POST", _FORM, b"a[b%20=v", "invalid_arg_name"),
        ("POST", _FORM, b"a[%20b=v", "invalid_arg_name"),
        ("POST", _FORM, b"a[%09=v", "invalid_arg_name"),
        ("POST", _FORM, b"a%20=v", {"a_": "v"}),
        ("POST", _FORM, b"a[b%0B=v", "invalid_arg_name"),
        ("POST", _FORM, b"a[b%0C=v", "invalid_arg_name"),
        ("POST", _FORM, b"a[b%0D=v", "invalid_arg_name"),
        ("POST", _FORM, b"a[b%0A=v", "invalid_arg_name"),
        # Content-Encoding is not read
        (
            "POST",
            [("content-type", _FORM), ("content-encoding", "gzip")],
            b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xffK\xb4M\x02\x00\xcf0\x00\x7f\x03\x00\x00\x00",
            "invalid_form_data",
        ),
        ("POST", [("content-type", _FORM), ("content-encoding", "gzip")], b"a=b", {"a": "b"}),
    ],
)
def test_slack_reads_a_post_body_as_real_reads_it(client, verb, content_type, body, want):
    """`api.test`, sent no token, answers each row as real answered it (the POST bodies section of
    ``backlot.routers.slack`` dates the measurement): ``want`` is the error a refused body is
    answered with, or the arguments a served one echoes and the warning real adds beside them.
    ``content_type`` is one header's value, or the headers sent when there are several; one with a
    byte past ASCII goes as its latin-1 bytes (:func:`tests._helpers.sent_as`)."""
    if content_type is None:
        headers = []
    elif isinstance(content_type, str):
        headers = [("content-type", content_type)]
    else:
        headers = content_type
    if any(ord(c) > 0x7F for _, value in headers for c in value):
        r = sent_as(client, verb, "/slack/api/api.test", headers, body)
    else:
        r = client.request(verb, "/slack/api/api.test", headers=headers, content=body)
    if isinstance(want, str):
        expected = {"ok": False, "error": want}
    else:
        args, warning = want if isinstance(want, tuple) else (want, None)
        expected = {"ok": True, "args": args}
        if warning is not None:
            expected |= {"warning": warning, "response_metadata": {"warnings": [warning]}}
    assert r.content == JSONResponse(expected).body


_SLACK_METHODS = [
    "api.test",
    "auth.test",
    "conversations.list",
    "conversations.info",
    "conversations.history",
    "conversations.members",
    "conversations.replies",
    "users.info",
    "users.list",
    "search.messages",
    "search.all",
    "search.files",
]


_FOUR_BODIES = [
    (None, b"{}", "missing_post_type"),
    (_FORM, b"{}", "invalid_form_data"),
    ("text/html", b"{}", "invalid_post_type"),
    ("application/json; charset=latin1", b"{}", "invalid_charset"),
]
_NAME_BODIES = [
    (_FORM, b"a[b]=c", "invalid_array_arg"),
    (_FORM, b"a{=b", "invalid_arg_name"),
    (
        _MULTIPART,
        b"--b\r\nContent-Disposition: form-data\r\n\r\nx\r\n--b--\r\n",
        "invalid_form_data",
    ),
    ("application/json; charset=latin1", b"", "invalid_charset"),
]


@pytest.mark.parametrize(
    "method, credential, content_type, body, error",
    [
        *((m, "admin", *b) for m in _SLACK_METHODS for b in _FOUR_BODIES),
        *(("auth.test", c, *b) for c in (None, "bogus", "query") for b in _FOUR_BODIES),
        *(
            (m, c, *b)
            for m, c in (
                ("auth.test", None),
                ("auth.test", "bogus"),
                ("conversations.history", "admin"),
            )
            for b in _NAME_BODIES
        ),
    ],
)
def test_slack_refuses_a_body_before_the_method_runs(
    client, tokens_yaml, method, credential, content_type, body, error
):
    """Each body is refused ahead of the method's credential and of its arguments, as real refused
    it on each row, and with no `warning`. A GET with the same headers and body answers what one
    without them does, and the same POST to a path that names no method is a 404, as on real:
    neither reads the body."""
    headers = {}
    params = {}
    if credential == "admin":
        headers["authorization"] = f"Bearer {tokens_yaml['admin_token']}"
    elif credential == "bogus":
        headers["authorization"] = "Bearer xoxb-not-a-real-token"
    elif credential == "query":
        params["token"] = "xoxb-not-a-real-token"
    sent = dict(headers) if content_type is None else {**headers, "content-type": content_type}
    url = f"/slack/api/{method}"
    r = client.post(url, headers=sent, params=params, content=body)
    assert r.content == JSONResponse({"ok": False, "error": error}).body
    got = client.request("GET", url, headers=sent, params=params, content=body)
    assert got.json() == client.get(url, headers=headers, params=params).json()
    elsewhere = client.post("/slack/api/nonexistent-xyz", headers=sent, params=params, content=body)
    assert elsewhere.status_code == 404


_PAGE = "<real's 404 page>"
_ROUTED = "<the route's answer>"
_LEFT = "<the router's 404>"
_RETIRED_MESSAGE = (
    "[ERROR] This method is retired and can no longer be used. Please use {} instead. Learn more: "
    "https://api.slack.com/changelog/2020-01-deprecating-antecedents-to-the-conversations-api."
)
_MARK_RETIRED = {
    "ok": False,
    "error": "method_deprecated",
    "response_metadata": {"messages": [_RETIRED_MESSAGE.format("conversations.mark")]},
}
_LIST_RETIRED = {
    "ok": False,
    "error": "method_deprecated",
    "response_metadata": {
        "messages": [_RETIRED_MESSAGE.format("conversations.list or users.conversations")]
    },
}
_ROUTED_ROWS = (("PUT", "api.test"), ("PATCH", "conversations.list"), ("DELETE", "auth.test"))
_RETIRED = (
    "apps.permissions.info",
    "apps.permissions.request",
    "apps.permissions.resources.list",
    "apps.permissions.scopes.list",
    "apps.permissions.users.list",
    "apps.permissions.users.request",
    "oauth.token",
    "workflows.stepCompleted",
    "workflows.stepFailed",
    "workflows.updateStep",
)


@pytest.mark.parametrize(
    "verb, path, credential, content_type, body, want",
    [
        # a name Slack has no method by, before the credential
        *(
            (
                "GET",
                "nonexistent.method",
                cred,
                None,
                None,
                ("unknown_method", "nonexistent.method"),
            )
            for cred in (None, "bogus", "query", "admin")
        ),
        # names of each shape, with no token
        *(
            ("GET", name, None, None, None, ("unknown_method", name))
            for name in ("a.b", "A.B", "1.2", "a", "a..b", "api.test.x")
        ),
        ("GET", "chat.postmessage", None, None, None, ("unknown_method", "chat.postmessage")),
        ("GET", "a%2Eb", None, None, None, ("unknown_method", "a.b")),
        (
            "POST",
            "nonexistent.method",
            None,
            "application/json",
            b"{}",
            ("unknown_method", "nonexistent.method"),
        ),
        ("POST", "nonexistent.method", None, _FORM, b"{}", "invalid_form_data"),
        # a name real has retired
        *(
            ("GET", name, cred, None, None, ("unknown_method", name))
            for name in _RETIRED
            for cred in (None, "bogus", "admin")
        ),
        *(
            ("POST", name, cred, _FORM, b"", ("unknown_method", name))
            for name in _RETIRED
            for cred in (None, "bogus")
        ),
        # an unserved published method, with a `token` query parameter and with a body; the test
        # below sends each one the other two credentials
        ("GET", "emoji.list", "query", None, None, "invalid_auth"),
        ("POST", "emoji.list", "query", _FORM, b"token=", "not_authed"),
        ("POST", "emoji.list", "query", _FORM, b"a=b", "invalid_auth"),
        ("POST", "emoji.list", None, "application/json", b"{}", "not_authed"),
        ("POST", "emoji.list", None, None, b"{}", "missing_post_type"),
        ("POST", "emoji.list", None, "text/html", b"{}", "invalid_post_type"),
        # real's 404 page
        *(
            ("GET", path, None, None, None, _PAGE)
            for path in (
                "nonexistent-xyz",
                "a-b.c",
                "a_b.c",
                "%C3%A9.x",
                "a.b;c",
                "a/b.c",
                "/a/b.c",
                "/a-b",
                "a\\b.c",
            )
        ),
        ("POST", "nonexistent-xyz", None, "application/json", b"{}", _PAGE),
        ("POST", "nonexistent-xyz", None, None, b"{}", _PAGE),
        # the other methods of `slack.UNSERVED_METHODS`, answered as a `GET` is once a body is read
        *(
            row
            for verb in ("PUT", "PATCH", "DELETE")
            for row in (
                (
                    verb,
                    "nonexistent.method",
                    None,
                    None,
                    None,
                    ("unknown_method", "nonexistent.method"),
                ),
                (verb, "emoji.list", None, None, None, "not_authed"),
                (verb, "emoji.list", "bogus", None, None, "invalid_auth"),
                (verb, "nonexistent-xyz", None, None, None, _PAGE),
                (verb, "emoji.list", None, _FORM, b"token=xoxb-not-a-real-token", "invalid_auth"),
            )
        ),
        ("PUT", "emoji.list", None, _MULTIPART, _one_part("token", "xoxb-nope"), "invalid_auth"),
        ("PUT", "emoji.list", None, "text/html", b"token=xoxb-not-a-real-token", "not_authed"),
        ("PUT", "emoji.list", None, _FORM, b"{}", "not_authed"),
        ("PUT", "emoji.list", None, _FORM, b"a{=b", "invalid_arg_name"),
        ("PUT", "emoji.list", None, _FORM, b"a[b]=c", "invalid_array_arg"),
        ("PUT", "nonexistent.method", None, _FORM, b"a{=b", "invalid_arg_name"),
        ("PUT", "oauth.v2.access", None, _FORM, b"token=xoxb-not-a-real-token", "invalid_auth"),
        # the methods that answer `invalid_code`, by their credential: a refused one, and one that
        # resolves
        ("GET", "oauth.v2.access", "query", None, None, "invalid_auth"),
        ("GET", "oauth.v2.access", "admin", None, None, "invalid_code"),
        ("GET", "oauth.access", "admin", None, None, "invalid_code"),
        ("GET", "oauth.v2.user.access", "admin", None, None, "invalid_code"),
        ("GET", "openid.connect.token", "bogus", None, None, "invalid_auth"),
        # a method of `slack._ANSWERED_BY_ARGUMENTS`, answered before its arguments are read (its
        # comment)
        ("GET", "tooling.tokens.rotate", "bogus", None, None, "invalid_auth"),
        *(
            (verb, "blocks.validate", "bogus", None, None, "invalid_auth")
            for verb in ("PUT", "PATCH", "DELETE")
        ),
        ("GET", "blocks.validate", "admin", None, None, "unsupported_http_method"),
        # a name `slack_sdk` calls that the baseline does not list (`slack._OTHER_ANSWERS`)
        *(
            ("GET", name, None, None, None, _MARK_RETIRED)
            for name in ("channels.mark", "groups.mark", "im.mark", "mpim.mark")
        ),
        *(("GET", name, None, None, None, _LIST_RETIRED) for name in ("im.list", "mpim.list")),
        ("GET", "channels.mark", "admin", None, None, _MARK_RETIRED),
        ("GET", "channels.mark", "bogus", None, None, "invalid_auth"),
        ("POST", "im.list", None, _FORM, b"", _LIST_RETIRED),
        ("GET", "channels.list", None, None, None, ("unknown_method", "channels.list")),
        ("GET", "im.open", "bogus", None, None, ("unknown_method", "im.open")),
        # a method a route serves, for another HTTP method, is the route's to answer
        *((verb, m, None, None, None, _ROUTED) for verb, m in _ROUTED_ROWS),
        # left to the router: a token that resolves, a method of `slack._ANSWERED_BY_ARGUMENTS`, a
        # path ending in `/`, the dot segments, a name after two `/`s or a `\\`, and a method
        # outside `slack.UNSERVED_METHODS`, its body unread
        ("GET", "emoji.list", "admin", None, None, _LEFT),
        ("GET", "files.getUploadURLExternal", "admin", None, None, _LEFT),
        ("POST", "blocks.validate", None, _FORM, b"", _LEFT),
        *(
            (verb, "blocks.validate", None, None, None, _LEFT)
            for verb in ("PUT", "PATCH", "DELETE")
        ),
        ("POST", "tooling.tokens.rotate", None, _FORM, b"refresh_token=xoxe-1-bogus", _LEFT),
        ("GET", "oauth.v2.exchange", "bogus", None, None, _LEFT),
        ("GET", "emoji.list/", None, None, None, _LEFT),
        ("GET", "%2E", None, None, None, _LEFT),
        ("GET", "%2E%2E", None, None, None, _LEFT),
        ("GET", "/api.test", None, None, None, _LEFT),
        ("GET", "//emoji.list", "bogus", None, None, _LEFT),
        ("GET", "\\api.test", None, None, None, _LEFT),
        ("POST", "%2E", None, None, b"{}", _LEFT),
        ("POST", "%2E%2E", None, None, b"{}", _LEFT),
        ("OPTIONS", "nonexistent.method", None, None, None, _LEFT),
        ("OPTIONS", "nonexistent.method", None, _FORM, b"a{=b", _LEFT),
    ],
)
def test_slack_answers_a_path_no_route_serves_as_real_does(
    client, admin_h, verb, path, credential, content_type, body, want
):
    """A path under `/slack/api/` that none of the routes serves, answered as slack.com/api answered
    each row but a ``_LEFT`` one (the comment above ``slack.UNSERVED_METHODS`` dates the
    measurement): ``want`` is the error, (error, `req_method`) for `unknown_method`, the whole
    answer, or real's 404 page. A `POST` with a JSON body carries the `missing_charset` warning, as
    each of real's answers to one did but the page. A ``_ROUTED`` row only says this layer leaves it
    be: real runs the method. A ``_LEFT`` row is one this layer leaves to the router, which real
    answers otherwise."""
    headers = {}
    url = f"/slack/api/{path}"
    if credential == "admin":
        headers.update(admin_h)
    elif credential == "bogus":
        headers["Authorization"] = "Bearer xoxb-not-a-real-token"
    elif credential == "query":
        url += "?token=xoxb-not-a-real-token"
    if content_type is not None:
        headers["Content-Type"] = content_type
    r = client.request(verb, url, headers=headers, content=body)
    if want == _ROUTED:
        assert b"unknown_method" not in r.content
        return
    if want == _LEFT:
        assert (r.status_code, r.content) == (404, b'{"detail":"Not Found"}')
        return
    if want == _PAGE:
        assert r.status_code == 404
        assert r.headers["content-type"] == "text/html; charset=utf-8"
        assert r.content == slack._NOT_FOUND_PAGE
        return
    if isinstance(want, dict):
        expected = dict(want)
    elif isinstance(want, tuple):
        expected = {"ok": False, "error": want[0], "req_method": want[1]}
    else:
        expected = {"ok": False, "error": want}
    if content_type == "application/json" and body:
        expected |= {
            "warning": "missing_charset",
            "response_metadata": {"warnings": ["missing_charset"]},
        }
    assert r.status_code == 200
    assert r.content == JSONResponse(expected).body


# The two answers here where they are not `not_authed` and `invalid_auth`: real's, or `_LEFT` for
# those `slack._ANSWERED_BY_ARGUMENTS` leaves to the router.
_PUBLISHED_ANSWERS = {
    "oauth.access": ("invalid_code", "invalid_code"),
    **{
        name: ("invalid_code", "invalid_auth")
        for name in ("oauth.v2.access", "oauth.v2.user.access", "openid.connect.token")
    },
    "blocks.validate": {
        "GET": ("unsupported_http_method", "unsupported_http_method"),
        "POST": (_LEFT, "invalid_auth"),
    },
    "oauth.v2.exchange": (_LEFT, _LEFT),
    "tooling.tokens.rotate": (_LEFT, "invalid_auth"),
}


@pytest.mark.parametrize("name", sorted(published.missing_operations("slack")))
def test_slack_answers_each_published_method_no_route_serves(client, name):
    """Each method the baseline lists as published and not served, answered as real answered it but
    those ``slack._ANSWERED_BY_ARGUMENTS`` leaves to the router (``slack._PUBLISHED_NOT_SERVED``
    dates the measurement)."""
    answers = _PUBLISHED_ANSWERS.get(name, ("not_authed", "invalid_auth"))
    for verb in ("GET", "POST"):
        none, bogus = answers[verb] if isinstance(answers, dict) else answers
        for headers, error in (
            ({}, none),
            ({"Authorization": "Bearer xoxb-not-a-real-token"}, bogus),
        ):
            if verb == "POST":
                headers = {**headers, "Content-Type": _FORM}
            r = client.request(verb, f"/slack/api/{name}", headers=headers, content=b"")
            if error == _LEFT:
                assert (r.status_code, r.content) == (404, b'{"detail":"Not Found"}'), (verb, name)
                continue
            assert r.content == JSONResponse({"ok": False, "error": error}).body, (
                verb,
                name,
                headers,
            )


# --- writes -----------------------------------------------------------------------------------
#
# Every method answers both verbs; the measurement behind that is on the write block in
# `backlot/routers/slack.py`.


@pytest.fixture
def wclient(sample_settings):
    """A client of its own for each write test, over the SAMPLE corpus.

    Not the module-scoped `client`, for two reasons. The overlay lives for the life of a server, so
    sharing one would carry every test's writes into the next and make an assertion about a message
    count depend on file order. And `client_for` without `reload` starts a second lifespan on the
    module-level `app` object, so a `corpus_client` anywhere earlier in this file leaves the shared
    client's connection closed -- `reload=True` gives this one its own app and leaves that one
    alone.
    """
    from tests._helpers import client_for

    with client_for(sample_settings, reload=True) as c:
        yield c


def _uh(tokens, email):
    return {"Authorization": f"Bearer {tokens[email]}"}


def test_post_message_is_readable_back_by_the_poster(wclient, tokens):
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    posted = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "shipping now"}
    ).json()
    assert posted["ok"] is True
    assert posted["channel"] == cid
    assert posted["message"]["text"] == "shipping now"
    assert posted["ts"] == posted["message"]["ts"]
    hist = wclient.post(
        "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
    ).json()
    assert posted["ts"] in [m["ts"] for m in hist["messages"]]

    # `last_read` is `slack_latest_ts`, which asks each side of the merge separately and keeps
    # the newer. The channel written to now reads back the posted ts, and a channel the caller
    # can see but nothing was written to still answers — the overlay side has no row there, and
    # the merge has to carry that rather than subscript it.
    def info(channel):
        return wclient.post(
            "/slack/api/conversations.info", headers=h, data={"channel": channel}
        ).json()["channel"]

    assert info(cid)["last_read"] == posted["ts"]
    assert float(info(synth.slack_channel_id("eng-announcements"))["last_read"]) > 0


def test_only_post_message_takes_a_channel_by_name(wclient, tokens):
    # Naming a channel is a property of the method, not of the argument: `_writable_channel`
    # records which methods real takes a name on. A name nothing has is `channel_not_found`
    # wherever a name is read at all.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "by id"}
    ).json()["ts"]
    for channel in ("incidents", "#incidents"):
        assert (
            wclient.post(
                "/slack/api/chat.postMessage", headers=h, data={"channel": channel, "text": "x"}
            ).json()["ok"]
            is True
        )
    assert (
        wclient.post(
            "/slack/api/chat.postMessage", headers=h, data={"channel": "nope-xyz", "text": "x"}
        ).json()["error"]
        == "channel_not_found"
    )
    assert (
        wclient.post(
            "/slack/api/chat.update",
            headers=h,
            data={"channel": "incidents", "ts": ts, "text": "x"},
        ).json()["error"]
        == "channel_not_found"
    )
    assert (
        wclient.post(
            "/slack/api/reactions.add",
            headers=h,
            data={"channel": "incidents", "timestamp": ts, "name": "eyes"},
        ).json()["error"]
        == "channel_not_found"
    )


def test_post_message_to_an_unreadable_channel_is_channel_not_found(wclient, tokens):
    # Slack's non-leaking answer: a caller who cannot see the channel is told it does not exist
    # rather than that they lack permission. Quoted from the spec's chat.postMessage error enum.
    h = _uh(tokens, "bob@acme.com")
    cid = synth.slack_channel_id("people-confidential")
    j = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "hello"}
    ).json()
    assert j == {"ok": False, "error": "channel_not_found"}


@pytest.mark.parametrize("method", ["chat.postMessage", "chat.update"])
def test_a_text_that_is_absent_or_empty_is_no_text_and_a_blank_one_posts(wclient, tokens, method):
    # `text` and `channel` are read by different rules, which `chat_post_message` records: an
    # absent or empty `text` is `no_text` where an absent `channel` is `invalid_arguments`. A
    # `text` of spaces is a message, so the test is emptiness rather than blankness.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "to edit"}
    ).json()["ts"]
    args = {"channel": cid} | ({"ts": ts} if method == "chat.update" else {})
    assert wclient.post(f"/slack/api/{method}", headers=h, data=args).json() == {
        "ok": False,
        "error": "no_text",
    }
    assert wclient.post(f"/slack/api/{method}", headers=h, data=args | {"text": ""}).json() == {
        "ok": False,
        "error": "no_text",
    }
    assert (
        wclient.post(f"/slack/api/{method}", headers=h, data=args | {"text": "   "}).json()["ok"]
        is True
    )
    # `channel` is the other way round, which is why the two are read by different rules.
    assert wclient.post("/slack/api/chat.postMessage", headers=h, data={"text": "x"}).json() == {
        "ok": False,
        "error": "invalid_arguments",
        "response_metadata": {"messages": ["[ERROR] missing required field: channel"]},
    }


def test_post_message_answers_over_get_too(wclient, tokens):
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    j = wclient.get(
        "/slack/api/chat.postMessage", headers=h, params={"channel": cid, "text": "over get"}
    ).json()
    assert j["ok"] is True


def test_post_message_needs_a_credential(wclient):
    cid = synth.slack_channel_id("incidents")
    j = wclient.post("/slack/api/chat.postMessage", data={"channel": cid, "text": "anon"}).json()
    assert j == {"ok": False, "error": "not_authed"}


def test_posting_does_not_silently_join_a_public_channel(wclient, tokens):
    # Real Slack does not join you when you post through the API, and neither does this: public
    # membership is derived from `main.slack_messages`, which a written row never joins.
    h = _uh(tokens, "bob@acme.com")
    cid = synth.slack_channel_id("eng-announcements")
    before = wclient.post("/slack/api/conversations.info", headers=h, data={"channel": cid}).json()
    assert before["channel"]["is_member"] is False
    wclient.post("/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "hi"})
    after = wclient.post("/slack/api/conversations.info", headers=h, data={"channel": cid}).json()
    assert after["channel"]["is_member"] is False


def test_post_ephemeral_stores_nothing(wclient, tokens):
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    before = wclient.post(
        "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    j = wclient.post(
        "/slack/api/chat.postEphemeral",
        headers=h,
        data={"channel": cid, "user": synth.slack_user_id("bob@acme.com"), "text": "just you"},
    ).json()
    assert j["ok"] is True and j["message_ts"]
    after = wclient.post(
        "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    assert len(after) == len(before)


def test_the_service_token_posts_as_a_bot(wclient, admin_h):
    # auth.test already answers the service token as USERVICE0 by analogy to a bot token, and a
    # message it writes carries the same identity. No subtype: `_writer` records the measurement.
    # A client that calls auth.test and matches user_id against message authors -- the use case
    # auth.test's own docstring names -- has to find this message.
    cid = synth.slack_channel_id("incidents")
    j = wclient.post(
        "/slack/api/chat.postMessage", headers=admin_h, data={"channel": cid, "text": "from ci"}
    ).json()
    assert j["ok"] is True
    assert "subtype" not in j["message"]
    assert j["message"]["user"] == "USERVICE0"
    me = wclient.post("/slack/api/auth.test", headers=admin_h).json()
    assert me["user_id"] == j["message"]["user"]
    hist = wclient.post(
        "/slack/api/conversations.history", headers=admin_h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    posted = [m for m in hist if m["ts"] == j["ts"]][0]
    assert posted["user"] == "USERVICE0" and "subtype" not in posted
    # A message posted through this API carries no client_msg_id, whoever posted it: the client
    # that would mint one is the thing this API replaces. `_message` records the measurement.
    assert "client_msg_id" not in posted
    # The same identity wherever the service account is rendered from a stored address, rather
    # than the hash of its sentinel: a reaction it leaves, and the stamp on a message it edits.
    wclient.post(
        "/slack/api/reactions.add",
        headers=admin_h,
        data={"channel": cid, "timestamp": j["ts"], "name": "robot_face"},
    )
    got = wclient.post(
        "/slack/api/reactions.get", headers=admin_h, data={"channel": cid, "timestamp": j["ts"]}
    ).json()
    assert got["message"]["reactions"][0]["users"] == ["USERVICE0"]
    edited = wclient.post(
        "/slack/api/chat.update",
        headers=admin_h,
        data={"channel": cid, "ts": j["ts"], "text": "ci again"},
    ).json()
    assert edited["message"]["edited"]["user"] == "USERVICE0"
    # Every path that renders a stored author, not only the message builder: a search hit, and a
    # thread's parent and reply authors. A path that mints the id itself answers the same message
    # as USERVICE0 from history and as a hash of the sentinel from search.
    hit = [
        m
        for m in wclient.post(
            "/slack/api/search.messages", headers=admin_h, data={"query": "ci again"}
        ).json()["messages"]["matches"]
        if m["ts"] == j["ts"]
    ][0]
    assert hit["user"] == "USERVICE0"
    root = wclient.post(
        "/slack/api/chat.postMessage", headers=admin_h, data={"channel": cid, "text": "ci root"}
    ).json()["ts"]
    wclient.post(
        "/slack/api/chat.postMessage",
        headers=admin_h,
        data={"channel": cid, "text": "ci reply", "thread_ts": root},
    )
    thread = wclient.post(
        "/slack/api/conversations.replies", headers=admin_h, data={"channel": cid, "ts": root}
    ).json()["messages"]
    assert thread[0]["reply_users"] == ["USERVICE0"]
    assert thread[-1]["parent_user_id"] == "USERVICE0"


def test_a_posted_message_is_visible_only_where_the_channel_is(wclient, tokens):
    # The ACL rows a write copies are the channel's own, so a post into a private channel is as
    # private as the channel. Written by a member; read by somebody outside it.
    inside = _uh(tokens, "hana@acme.com")
    cid = synth.slack_channel_id("people-confidential")
    posted = wclient.post(
        "/slack/api/chat.postMessage",
        headers=inside,
        data={"channel": cid, "text": "confidential addendum"},
    ).json()
    assert posted["ok"] is True
    outside = _uh(tokens, "bob@acme.com")
    hits = wclient.post(
        "/slack/api/search.messages", headers=outside, data={"query": "confidential addendum"}
    ).json()
    assert hits["messages"]["matches"] == []
    mine = wclient.post(
        "/slack/api/search.messages", headers=inside, data={"query": "confidential addendum"}
    ).json()
    assert posted["ts"] in [m["ts"] for m in mine["messages"]["matches"]]


def test_update_own_message_changes_history_and_search(wclient, tokens):
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "zarquon one"}
    ).json()["ts"]
    j = wclient.post(
        "/slack/api/chat.update", headers=h, data={"channel": cid, "ts": ts, "text": "zarquon two"}
    ).json()
    assert j["ok"] is True and j["text"] == "zarquon two"
    # The message an edit echoes is a reduced one, as `chat_update` records: it carries neither
    # `ts` nor `reactions`, which a read of the same message answers. `ts` is on the answer
    # beside it.
    wclient.post(
        "/slack/api/reactions.add",
        headers=h,
        data={"channel": cid, "timestamp": ts, "name": "eyes"},
    )
    echoed = wclient.post(
        "/slack/api/chat.update", headers=h, data={"channel": cid, "ts": ts, "text": "zarquon two"}
    ).json()
    assert "ts" not in echoed["message"] and "reactions" not in echoed["message"]
    assert echoed["ts"] == ts
    read_back = [
        m
        for m in wclient.post(
            "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
        ).json()["messages"]
        if m["ts"] == ts
    ][0]
    assert read_back["ts"] == ts and read_back["reactions"]
    hist = wclient.post(
        "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    assert [m["text"] for m in hist if m["ts"] == ts] == ["zarquon two"]
    hits = wclient.post("/slack/api/search.messages", headers=h, data={"query": "zarquon"}).json()[
        "messages"
    ]["matches"]
    assert [m["text"] for m in hits if m["ts"] == ts] == ["zarquon two"]


def test_an_edited_message_carries_the_edited_stamp(wclient, tokens):
    # Real Slack marks an edited message with `edited: {user, ts}`, and a client renders "(edited)"
    # off it. `_message` already serves the column; the write has to fill it.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "before"}
    ).json()["ts"]
    wclient.post(
        "/slack/api/chat.update", headers=h, data={"channel": cid, "ts": ts, "text": "after"}
    )
    hist = wclient.post(
        "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    msg = [m for m in hist if m["ts"] == ts][0]
    assert msg["edited"]["user"] == synth.slack_user_id("ava@acme.com")
    assert msg["edited"]["ts"]
    # Stored as the ADDRESS, the way a corpus states one and the way `reactions.users` is —
    # `_edited` renders the id above from it, so storing the id would render it twice.
    stored = json.loads(
        wclient.app.state.conn.execute(
            "SELECT value FROM ov.slack_patch WHERE channel = ? AND ts = ? AND field = 'edited'",
            ("incidents", ts),
        ).fetchone()[0]
    )
    assert stored["user"] == "ava@acme.com"


def test_updating_someone_elses_message_is_cant_update_message(wclient, tokens):
    author = _uh(tokens, "ava@acme.com")
    other = _uh(tokens, "bob@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=author, data={"channel": cid, "text": "mine"}
    ).json()["ts"]
    j = wclient.post(
        "/slack/api/chat.update", headers=other, data={"channel": cid, "ts": ts, "text": "yours"}
    ).json()
    assert j == {"ok": False, "error": "cant_update_message"}
    # and the text is unchanged
    hist = wclient.post(
        "/slack/api/conversations.history", headers=author, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    assert [m["text"] for m in hist if m["ts"] == ts] == ["mine"]


def test_updating_a_message_in_an_unreadable_channel_is_channel_not_found(wclient, tokens, ro_conn):
    outsider = _uh(tokens, "bob@acme.com")
    ts = ro_conn.execute(
        "SELECT ts FROM slack_messages WHERE channel = 'people-confidential' LIMIT 1"
    ).fetchone()[0]
    cid = synth.slack_channel_id("people-confidential")
    j = wclient.post(
        "/slack/api/chat.update", headers=outsider, data={"channel": cid, "ts": ts, "text": "x"}
    ).json()
    assert j == {"ok": False, "error": "channel_not_found"}


def test_updating_a_ts_that_does_not_exist_is_message_not_found(wclient, tokens):
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    j = wclient.post(
        "/slack/api/chat.update",
        headers=h,
        data={"channel": cid, "ts": "1.000001", "text": "ghost"},
    ).json()
    assert j == {"ok": False, "error": "message_not_found"}


def test_deleting_someone_elses_message_is_cant_delete_message(wclient, tokens):
    author = _uh(tokens, "ava@acme.com")
    other = _uh(tokens, "bob@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=author, data={"channel": cid, "text": "mine too"}
    ).json()["ts"]
    j = wclient.post(
        "/slack/api/chat.delete", headers=other, data={"channel": cid, "ts": ts}
    ).json()
    assert j == {"ok": False, "error": "cant_delete_message"}


def test_a_deleted_message_leaves_every_read(wclient, tokens):
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "zarquon gone"}
    ).json()["ts"]
    assert (
        wclient.post("/slack/api/chat.delete", headers=h, data={"channel": cid, "ts": ts}).json()[
            "ok"
        ]
        is True
    )
    hist = wclient.post(
        "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    assert ts not in [m["ts"] for m in hist]
    hits = wclient.post(
        "/slack/api/search.messages", headers=h, data={"query": "zarquon gone"}
    ).json()["messages"]
    assert ts not in [m["ts"] for m in hits["matches"]]
    assert hits["total"] == len(hits["matches"])
    perma = wclient.post(
        "/slack/api/chat.getPermalink", headers=h, data={"channel": cid, "message_ts": ts}
    ).json()
    assert perma == {"ok": False, "error": "message_not_found"}


def test_a_corpus_message_can_be_deleted_by_its_author(wclient, tokens, ro_conn):
    # The tombstone has to work over a row that lives in the READ-ONLY corpus, not only over one
    # the overlay wrote. That is the case the corpus cannot express by deletion.
    #
    # Both shapes, because `chat_delete` answers them differently and the corpus holds both: a
    # message with no replies goes, and a thread root stays as the placeholder real leaves.
    alone = _a_corpus_message(ro_conn, "thread_ts IS NULL")
    root = _a_corpus_message(ro_conn, "thread_ts = ts")

    def history(email, channel):
        return {
            m["ts"]: m
            for m in wclient.post(
                "/slack/api/conversations.history",
                headers=_uh(tokens, email),
                data={"channel": synth.slack_channel_id(channel), "limit": 200},
            ).json()["messages"]
        }

    for email, channel, ts in (alone, root):
        assert ts in history(email, channel)
        assert (
            wclient.post(
                "/slack/api/chat.delete",
                headers=_uh(tokens, email),
                data={"channel": synth.slack_channel_id(channel), "ts": ts},
            ).json()["ok"]
            is True
        )

    assert alone[2] not in history(alone[0], alone[1])
    email, channel, ts = root
    left = history(email, channel)[ts]
    assert left["subtype"] == "tombstone" and left["hidden"] is True
    assert left["text"] == slack.TOMBSTONE_TEXT and "team" not in left
    # And the thread it rooted is still readable, which is the point of keeping it.
    thread = wclient.post(
        "/slack/api/conversations.replies",
        headers=_uh(tokens, email),
        data={"channel": synth.slack_channel_id(channel), "ts": ts},
    ).json()
    assert thread["ok"] is True and len(thread["messages"]) > 1


def test_a_deleted_thread_root_leaves_the_placeholder_real_leaves(wclient, tokens):
    # `chat_delete` and `_unthread_a_spent_root` record the measurement. Four things real does
    # that subtracting the row would not: it is attributed to Slackbot, it is found by no search
    # (neither the placeholder's words nor the message's own), it can no longer be updated or
    # deleted, and it still takes a reaction and answers a permalink.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")

    def post(text, **extra):
        return wclient.post(
            "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": text, **extra}
        ).json()["ts"]

    def history():
        return {
            m["ts"]: m
            for m in wclient.post(
                "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
            ).json()["messages"]
        }

    root = post("zarquonroot words")
    reply = post("a reply", thread_ts=root)
    wclient.post("/slack/api/chat.delete", headers=h, data={"channel": cid, "ts": root})

    stub = history()[root]
    assert stub["subtype"] == "tombstone" and stub["hidden"] is True
    assert stub["user"] == slack.SLACKBOT_ID and stub["parent_user_id"] == slack.SLACKBOT_ID
    assert stub["text"] == slack.TOMBSTONE_TEXT and "team" not in stub
    for query in ("deleted", "zarquonroot"):
        hits = wclient.post("/slack/api/search.messages", headers=h, data={"query": query}).json()[
            "messages"
        ]["matches"]
        assert root not in [m["ts"] for m in hits], query
    for method in ("chat.update", "chat.delete"):
        answered = wclient.post(
            f"/slack/api/{method}",
            headers=h,
            data={"channel": cid, "ts": root, "text": "x"},
        ).json()
        assert answered == {"ok": False, "error": "message_not_found"}, method
    assert (
        wclient.post(
            "/slack/api/reactions.add",
            headers=h,
            data={"channel": cid, "timestamp": root, "name": "eyes"},
        ).json()["ok"]
        is True
    )
    assert (
        wclient.post(
            "/slack/api/chat.getPermalink", headers=h, data={"channel": cid, "message_ts": root}
        ).json()["ok"]
        is True
    )

    # The last reply takes the placeholder with it.
    wclient.post("/slack/api/chat.delete", headers=h, data={"channel": cid, "ts": reply})
    assert root not in history()


def test_a_root_keeps_its_thread_while_a_reply_remains(wclient, tokens):
    # The other side of `_unthread_a_spent_root`: deleting one of two replies leaves the root a
    # thread root. Without its guard the first delete unthreads it.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    root = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "two replies"}
    ).json()["ts"]
    first, second = (
        wclient.post(
            "/slack/api/chat.postMessage",
            headers=h,
            data={"channel": cid, "text": text, "thread_ts": root},
        ).json()["ts"]
        for text in ("one", "two")
    )
    wclient.post("/slack/api/chat.delete", headers=h, data={"channel": cid, "ts": first})
    still = [
        m
        for m in wclient.post(
            "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
        ).json()["messages"]
        if m["ts"] == root
    ][0]
    assert still["thread_ts"] == root
    thread = wclient.post(
        "/slack/api/conversations.replies", headers=h, data={"channel": cid, "ts": root}
    ).json()["messages"]
    assert [m["ts"] for m in thread] == [root, second]


def test_a_root_whose_last_reply_goes_is_a_standalone_message_again(wclient, tokens):
    # Measured with the placeholder above: real leaves no `thread_ts` on it and its permalink
    # loses the thread query.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    root = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "live root"}
    ).json()["ts"]
    reply = wclient.post(
        "/slack/api/chat.postMessage",
        headers=h,
        data={"channel": cid, "text": "only reply", "thread_ts": root},
    ).json()["ts"]
    wclient.post("/slack/api/chat.delete", headers=h, data={"channel": cid, "ts": reply})
    back = [
        m
        for m in wclient.post(
            "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
        ).json()["messages"]
        if m["ts"] == root
    ][0]
    assert "thread_ts" not in back
    link = wclient.post(
        "/slack/api/chat.getPermalink", headers=h, data={"channel": cid, "message_ts": root}
    ).json()["permalink"]
    assert "thread_ts=" not in link


#: What each write and write-adjacent method DECLARES it reads. These are the arguments
#: `backlot mcp --source slack` turns into tool arguments, so an agent can only send what is
#: here — and a mutation run showed every one of them could be dropped with the suite green.
_DECLARED = {
    "chat.postMessage": {"channel", "text", "thread_ts"},
    "chat.postEphemeral": {"channel", "user", "text"},
    "chat.update": {"channel", "ts", "text"},
    "chat.delete": {"channel", "ts"},
    "chat.getPermalink": {"channel", "message_ts"},
    "reactions.add": {"channel", "timestamp", "name"},  # all three required; see _P_REACTION_ADD
    "reactions.remove": {"channel", "timestamp", "name"},
    "reactions.get": {"channel", "timestamp"},
    "reactions.list": {"count", "page", "limit", "cursor", "user"},
}


@pytest.mark.parametrize("method", sorted(_DECLARED))
def test_each_write_method_declares_the_arguments_it_reads(client, method):
    # Declared, not merely accepted: `_P_*` lists what Backlot actually reads, and the fidelity
    # baseline acknowledges every documented argument it does not. Dropping one here would take
    # it out of the MCP tool's signature with nothing else noticing.
    spec = client.get("/_meta/openapi/slack").json()
    names = {
        q["name"]
        for verb in spec["paths"][f"/slack/api/{method}"].values()
        if isinstance(verb, dict)
        for q in verb.get("parameters", [])
        if q.get("in") == "query"
    }
    assert names == _DECLARED[method], method


def test_a_json_token_is_not_a_credential(wclient, tokens):
    # `_json_args` records the measurement. Dropping the `k != "token"` guard left the suite
    # green, and with it a client is authenticated as whoever a stale field in its payload names.
    cid = synth.slack_channel_id("incidents")
    ava, bob = tokens["ava@acme.com"], tokens["bob@acme.com"]
    body = {"token": ava, "channel": cid, "text": "whose message?"}
    # With no header at all, a JSON `token` does not authenticate. (A JSON body with no charset
    # draws `missing_charset` beside the answer, which is its own measurement.)
    assert (
        wclient.post(
            "/slack/api/chat.postMessage", headers={"Content-Type": "application/json"}, json=body
        ).json()["error"]
        == "not_authed"
    )
    # Beside a header, the header is who posts.
    posted = wclient.post(
        "/slack/api/chat.postMessage",
        headers={"Authorization": f"Bearer {bob}", "Content-Type": "application/json"},
        json=body,
    ).json()
    assert posted["message"]["user"] == synth.slack_user_id("bob@acme.com")


@pytest.mark.parametrize(
    "sent, want",
    [
        ({}, "no_text"),
        ({"blocks": "[]"}, "no_text"),
        ({"attachments": "[]"}, "no_text"),
        ({"attachments": "[{}]"}, "no_text"),
        ({"attachments": "abc"}, "no_text"),
        ({"attachments": '{"text": "a"}'}, "no_text"),
        # Decoration, and shapes that are not an array of objects: each `no_text` on real,
        # none of them a refusal and none of them an error here.
        ({"attachments": '[{"color": "good"}]'}, "no_text"),
        ({"attachments": '[{"footer": "a"}]'}, "no_text"),
        ({"attachments": '[{"author_link": "a"}]'}, "no_text"),
        ({"attachments": '[{"title_link": "a"}]'}, "no_text"),
        ({"attachments": '[{"image_url": "a"}]'}, "no_text"),
        ({"attachments": '[{"mrkdwn_in": ["text"]}]'}, "no_text"),
        ({"attachments": '[{"ts": "1"}]'}, "no_text"),
        ({"attachments": '["text"]'}, "no_text"),
        ({"attachments": "5"}, "no_text"),
        ({"attachments": "true"}, "no_text"),
        ({"attachments": '"text"'}, "no_text"),
        ({"attachments": '[["text"]]'}, "no_text"),
        ({"attachments": "null"}, "no_text"),
        # A member's own `blocks` count only as a non-empty array, unlike its siblings.
        ({"attachments": '[{"blocks": []}]'}, "no_text"),
        ({"attachments": '[{"blocks": "abc"}]'}, "no_text"),
        ({"attachments": '[{"blocks": {}}]'}, "no_text"),
        (
            {
                "attachments": '[{"blocks": [{"type": "section", '
                '"text": {"type": "mrkdwn", "text": "a"}}]}]'
            },
            None,
        ),
        # The six keys that carry a message by being there, each with an empty value where one
        # is expressible, since the key is the test rather than what it says.
        ({"attachments": '[{"text": "a"}]'}, None),
        ({"attachments": '[{"fallback": "a"}]'}, None),
        ({"attachments": '[{"title": "a"}]'}, None),
        ({"attachments": '[{"pretext": "a"}]'}, None),
        ({"attachments": '[{"author_name": "a"}]'}, None),
        ({"attachments": '[{"fields": [{"title": "a", "value": "b", "short": true}]}]'}, None),
        ({"attachments": '[{"text": ""}]'}, None),
        ({"attachments": '[{"pretext": ""}]'}, None),
        ({"attachments": '[{"author_name": ""}]'}, None),
        ({"attachments": '[{"fields": []}]'}, None),
        ({"attachments": '[{}, {"text": "a"}]'}, None),
        ({"blocks": '[{"type": "section"}]'}, None),
        ({"blocks": "abc"}, "invalid_blocks_format"),
        ({"blocks": "{}"}, "invalid_blocks_format"),
        ({"blocks": ""}, "invalid_blocks_format"),
        ({"text": "x", "blocks": "abc"}, "invalid_blocks_format"),
    ],
)
@pytest.mark.parametrize("method", ["chat.postMessage", "chat.update"])
def test_blocks_is_read_as_a_value_not_looked_for(wclient, tokens, method, sent, want):
    # `chat_post_message` records the measurement: a non-empty `blocks` array stands in for
    # `text`, an `attachments` array does when one of its members carries a message, and a
    # `blocks` that is not an array is refused even beside a `text`. Dropping either operand
    # from the `no_text` test left the suite green, as did reading three of the six keys.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "editable"}
    ).json()["ts"]
    args = {"channel": cid, **sent} | ({"ts": ts} if method == "chat.update" else {})
    answered = wclient.post(f"/slack/api/{method}", headers=h, data=args).json()
    if want is None:
        assert answered["ok"] is True, (method, sent)
    else:
        assert answered == {"ok": False, "error": want}, (method, sent)


def test_post_ephemeral_reads_the_shape_of_user_then_its_membership(wclient, tokens):
    # `chat_post_ephemeral` records the measurement. Every mutation of this check survived.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    pattern = "[ERROR] input must match regex pattern: ^[UW][A-Z0-9]{2,}$ [json-pointer:/user]"
    for bad in ("", synth.slack_user_id("ava@acme.com").lower()):
        assert wclient.post(
            "/slack/api/chat.postEphemeral", headers=h, data={"channel": cid, "user": bad}
        ).json() == {
            "ok": False,
            "error": "invalid_arguments",
            "response_metadata": {"messages": [pattern]},
        }, bad
    # A missing `user` is named before a missing `channel`.
    assert wclient.post("/slack/api/chat.postEphemeral", headers=h, data={}).json()[
        "response_metadata"
    ]["messages"] == [
        "[ERROR] missing required field: user",
        "[ERROR] missing required field: channel",
    ]
    # A private channel's members are its grantees, so one who is not in it is refused.
    private = synth.slack_channel_id("people-confidential")
    insider = _uh(tokens, "hana@acme.com")
    assert wclient.post(
        "/slack/api/chat.postEphemeral",
        headers=insider,
        data={"channel": private, "user": synth.slack_user_id("bob@acme.com")},
    ).json() == {"ok": False, "error": "user_not_in_channel"}
    # And a member of it is served.
    assert (
        wclient.post(
            "/slack/api/chat.postEphemeral",
            headers=insider,
            data={"channel": private, "user": synth.slack_user_id("hana@acme.com")},
        ).json()["ok"]
        is True
    )
    # The service id is served too, and it is the one `user` no part of the lookup can refuse:
    # it is not a corpus author, so `all_user_emails` misses it and `_slack_author_by_uid`'s
    # own entry answers, and it is in no channel, so the membership check has to be skipped
    # rather than passed. Each of those three lines survived a mutation without this.
    assert (
        wclient.post(
            "/slack/api/chat.postEphemeral",
            headers=insider,
            data={"channel": private, "user": slack.SERVICE_USER_ID},
        ).json()["ok"]
        is True
    )


#: Requests where two refusals apply, and which one real answers. Swapping either guard
#: against its neighbour leaves the rest of the suite green, so the order is only stated here.
#: `BAD` is a channel id nothing has; `file` wins over `file_comment` whichever is written
#: first, so the pair is sent both ways round.
_REFUSAL_ORDER = [
    ("chat.postMessage", {"channel": "BAD", "blocks": "abc"}, "channel_not_found"),
    ("chat.update", {"channel": "GOOD", "ts": "1.000001", "blocks": "abc"}, "message_not_found"),
    ("chat.update", {"channel": "BAD", "ts": "1.000001", "blocks": "abc"}, "channel_not_found"),
    (
        "chat.postEphemeral",
        {"channel": "BAD", "user": "abc", "text": "x"},
        "invalid_arguments",
    ),
    (
        "reactions.remove",
        {"name": "eyes", "file": "F00000000", "file_comment": "Fc00000000"},
        "file_not_found",
    ),
    (
        "reactions.get",
        {"name": "eyes", "file_comment": "Fc00000000", "file": "F00000000"},
        "file_not_found",
    ),
]


@pytest.mark.parametrize("method, sent, want", _REFUSAL_ORDER)
def test_the_refusal_that_wins_is_the_one_real_answers(wclient, tokens, method, sent, want):
    h = _uh(tokens, "ava@acme.com")
    args = {
        k: {"GOOD": synth.slack_channel_id("incidents"), "BAD": "C00000000"}.get(v, v)
        for k, v in sent.items()
    }
    answered = wclient.post(f"/slack/api/{method}", headers=h, data=args).json()
    assert answered["ok"] is False, (method, sent)
    assert answered["error"] == want, (method, sent)


def test_post_ephemeral_finds_a_member_who_has_never_posted(tmp_path):
    """A private channel's grantee is a member whether or not they ever spoke, so `user` is
    resolved against the PRINCIPALS and not only against the message authors.

    SAMPLE cannot show it: every author there holds an account and its one private channel is
    granted to a group whose only member posted, so dropping the `all_user_emails` lookup left
    the suite green. A corpus of its own separates them -- zoe holds an account, is granted the
    channel, and has written nothing."""
    import yaml

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "channel": "closed",
                "content": "the comp band lands at 240k",
                "author_email": "ava@x.com",
                "readers": ["user:ava@x.com", "user:zoe@x.com"],
            }
        ],
    )
    conn = store.connect_ro(s.db_path)
    assert "zoe@x.com" in store.all_user_emails(conn)
    assert "zoe@x.com" not in store.distinct_slack_author_emails(conn)
    conn.close()
    issued = yaml.safe_load(s.tokens_path.read_text(encoding="utf-8"))
    tok = {u["email"]: u["token"] for u in issued["users"]}
    with client_for(s, reload=True) as c:
        answered = c.post(
            "/slack/api/chat.postEphemeral",
            headers={"Authorization": f"Bearer {tok['ava@x.com']}"},
            data={
                "channel": synth.slack_channel_id("closed"),
                "user": synth.slack_user_id("zoe@x.com"),
                "text": "only you can see this",
            },
        ).json()
    assert answered["ok"] is True, answered


def test_reactions_list_reads_an_empty_user_and_lists_each_message_once(wclient, tokens, admin_h):
    # Two survivors: `user=` read as absent, and the `seen` guard, without which a message the
    # caller reacted to through the API is listed twice — once from the patch and once from the
    # merged source.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    assert wclient.post("/slack/api/reactions.list", headers=h, data={"user": ""}).json() == {
        "ok": False,
        "error": "user_not_found",
    }
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "once only"}
    ).json()["ts"]
    wclient.post(
        "/slack/api/reactions.add",
        headers=h,
        data={"channel": cid, "timestamp": ts, "name": "eyes"},
    )
    listed = [
        i["message"]["ts"]
        for i in wclient.post("/slack/api/reactions.list", headers=h, data={"limit": "200"}).json()[
            "items"
        ]
    ]
    assert listed.count(ts) == 1, listed
    # The service id resolves too, and by its own branch: it is not a corpus author, so the
    # scan over `all_user_emails` cannot find it. Asked for it, the admin's own reaction is
    # what comes back, where dropping the branch answers `user_not_found`.
    admin_ts = wclient.post(
        "/slack/api/chat.postMessage", headers=admin_h, data={"channel": cid, "text": "by the app"}
    ).json()["ts"]
    wclient.post(
        "/slack/api/reactions.add",
        headers=admin_h,
        data={"channel": cid, "timestamp": admin_ts, "name": "tada"},
    )
    answered = wclient.post(
        "/slack/api/reactions.list", headers=admin_h, data={"user": slack.SERVICE_USER_ID}
    ).json()
    assert answered["ok"] is True
    assert admin_ts in [i["message"]["ts"] for i in answered["items"]]


def test_a_quoted_query_scores_a_written_message_against_the_corpus(wclient, tokens):
    # Every mutation of the phrase scoring survived, because nothing scored a quoted query with
    # an overlay hit. `_corpus_stats` records what the phrase path does and does not reproduce.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage",
        headers=h,
        data={"channel": cid, "text": "gateway throwing zarquonphrase"},
    ).json()["ts"]
    hits = wclient.post(
        "/slack/api/search.messages", headers=h, data={"query": '"throwing zarquonphrase"'}
    ).json()["messages"]["matches"]
    assert ts in [m["ts"] for m in hits]


def test_a_permalink_names_the_thread_only_for_a_message_in_one(wclient, tokens):
    # `_permalink` records the measurement. Dropping the `thread_ts` branch left the suite green.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")

    def link(ts):
        return wclient.post(
            "/slack/api/chat.getPermalink", headers=h, data={"channel": cid, "message_ts": ts}
        ).json()["permalink"]

    alone = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "alone"}
    ).json()["ts"]
    assert "?" not in link(alone)
    root = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "root"}
    ).json()["ts"]
    reply = wclient.post(
        "/slack/api/chat.postMessage",
        headers=h,
        data={"channel": cid, "text": "reply", "thread_ts": root},
    ).json()["ts"]
    for ts in (root, reply):
        assert f"?thread_ts={root}&cid={cid}" in link(ts)


@pytest.mark.parametrize(
    "method, reads_json",
    [
        ("chat.postMessage", True),
        ("chat.postEphemeral", True),
        ("chat.update", True),
        ("chat.delete", True),
        ("reactions.add", True),
        ("reactions.remove", True),
        ("chat.getPermalink", False),
        ("reactions.get", False),
    ],
)
def test_a_json_body_is_read_only_where_the_spec_declares_one(wclient, tokens, method, reads_json):
    # `JSON_BODY_METHODS` records the measurement, and dropping any member of it left the suite
    # green. The property is whether the body was READ: a method that does not read one answers
    # `invalid_arguments` for the arguments it never saw, where one that does gets past argument
    # validation to whatever it then makes of them.
    h = _uh(tokens, "ava@acme.com") | {"Content-Type": "application/json"}
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage",
        headers=_uh(tokens, "ava@acme.com"),
        data={"channel": cid, "text": "addressable"},
    ).json()["ts"]
    body = {
        "channel": cid,
        "text": "sent as json",
        "ts": ts,
        "message_ts": ts,
        "timestamp": ts,
        "name": "eyes",
        "user": synth.slack_user_id("ava@acme.com"),
    }
    answered = wclient.post(f"/slack/api/{method}", headers=h, json=body).json()
    # Method-agnostic: a method that does not read the body answers exactly what it answers
    # with no body at all, whatever that answer is.
    empty = wclient.post(f"/slack/api/{method}", headers=h, json={}).json()
    saw_the_arguments = answered != empty
    assert saw_the_arguments is reads_json, (method, answered, empty)


def test_reactions_remove_takes_off_the_name_it_was_given(wclient, tokens):
    # Matching any reaction rather than the one named left the suite green.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "two reactions"}
    ).json()["ts"]
    for name in ("eyes", "tada"):
        wclient.post(
            "/slack/api/reactions.add",
            headers=h,
            data={"channel": cid, "timestamp": ts, "name": name},
        )
    wclient.post(
        "/slack/api/reactions.remove",
        headers=h,
        data={"channel": cid, "timestamp": ts, "name": "eyes"},
    )
    got = wclient.post(
        "/slack/api/reactions.get", headers=h, data={"channel": cid, "timestamp": ts}
    ).json()
    assert [r["name"] for r in got["message"]["reactions"]] == ["tada"]


@pytest.mark.parametrize("sort", ["score", "timestamp"])
def test_a_written_message_is_in_a_search_under_either_sort_and_an_in_filter(wclient, tokens, sort):
    # The overlay merge has a branch per sort and a container filter, and neither was reached
    # with an overlay hit present.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "zarquonsearch"}
    ).json()["ts"]
    hits = wclient.post(
        "/slack/api/search.messages", headers=h, data={"query": "zarquonsearch", "sort": sort}
    ).json()["messages"]["matches"]
    assert ts in [m["ts"] for m in hits], sort
    scoped = wclient.post(
        "/slack/api/search.messages",
        headers=h,
        data={"query": "zarquonsearch in:#incidents", "sort": sort},
    ).json()["messages"]["matches"]
    assert ts in [m["ts"] for m in scoped], sort
    # And the filter narrows rather than decorates: the same term, asked of a channel the
    # message is not in, finds nothing. Without the overlay's own container clause the written
    # row answered every channel's search.
    elsewhere = wclient.post(
        "/slack/api/search.messages",
        headers=h,
        data={"query": "zarquonsearch in:#eng-announcements", "sort": sort},
    ).json()["messages"]
    assert elsewhere["matches"] == [], sort
    assert elsewhere["total"] == 0, sort


#: Every write and write-adjacent method, with arguments that would otherwise reach the handler,
#: and the arguments it requires. One table rather than a test apiece: these are the same two
#: refusals on nine methods, and a mutation run showed `return err` could be dropped after
#: `_caller_or_error`, `_missing_argument` or `_writable_channel` on eight of them with the suite
#: still green.
_WRITE_METHODS = {
    "chat.postMessage": ({"channel": "CID", "text": "x"}, ["channel"]),
    "chat.postEphemeral": ({"channel": "CID", "user": "UID", "text": "x"}, ["user", "channel"]),
    "chat.update": ({"channel": "CID", "ts": "TS", "text": "x"}, ["channel", "ts"]),
    "chat.delete": ({"channel": "CID", "ts": "TS"}, ["channel", "ts"]),
    "chat.getPermalink": ({"channel": "CID", "message_ts": "TS"}, ["channel", "message_ts"]),
    "reactions.add": (
        {"channel": "CID", "timestamp": "TS", "name": "eyes"},
        ["name", "timestamp", "channel"],
    ),
    "reactions.remove": ({"channel": "CID", "timestamp": "TS", "name": "eyes"}, ["name"]),
    "reactions.get": ({"channel": "CID", "timestamp": "TS"}, []),
    "reactions.list": ({}, []),
}


@pytest.mark.parametrize("method", sorted(_WRITE_METHODS))
def test_every_write_method_refuses_a_caller_with_no_credential(wclient, tokens, method):
    args, _ = _WRITE_METHODS[method]
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage",
        headers=_uh(tokens, "ava@acme.com"),
        data={"channel": cid, "text": "a message to address"},
    ).json()["ts"]
    sent = {
        k: {"CID": cid, "TS": ts, "UID": synth.slack_user_id("ava@acme.com")}.get(v, v)
        for k, v in args.items()
    }
    assert wclient.post(f"/slack/api/{method}", data=sent).json() == {
        "ok": False,
        "error": "not_authed",
    }
    # The credential is read BEFORE the arguments, so a call with neither is still about the
    # credential: a bogus bearer with an empty body answers `invalid_auth` on real, not the
    # missing arguments. Swapping the two calls left the suite green.
    assert wclient.post(f"/slack/api/{method}").json() == {"ok": False, "error": "not_authed"}


@pytest.mark.parametrize("method", sorted(m for m in _WRITE_METHODS if _WRITE_METHODS[m][1]))
def test_every_write_method_names_each_argument_it_requires(wclient, tokens, method):
    args, required = _WRITE_METHODS[method]
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "addressable"}
    ).json()["ts"]
    filled = {
        k: {"CID": cid, "TS": ts, "UID": synth.slack_user_id("ava@acme.com")}.get(v, v)
        for k, v in args.items()
    }
    for name in required:
        answered = wclient.post(
            f"/slack/api/{method}", headers=h, data={k: v for k, v in filled.items() if k != name}
        ).json()
        assert answered["ok"] is False, (method, name)
        assert answered["error"] == "invalid_arguments", (method, name)
        assert (
            f"[ERROR] missing required field: {name}" in (answered["response_metadata"]["messages"])
        ), (method, name)


@pytest.mark.parametrize(
    "method", sorted(m for m, (args, _) in _WRITE_METHODS.items() if "channel" in args)
)
def test_every_write_method_hides_a_channel_the_caller_cannot_see(wclient, tokens, method):
    args, _ = _WRITE_METHODS[method]
    # bob cannot see people-confidential, and must be told it does not exist rather than
    # anything about what is in it.
    sent = {
        k: {
            "CID": synth.slack_channel_id("people-confidential"),
            "TS": "1.000001",
            # Well-formed, so the channel check is what refuses: `chat.postEphemeral` reads
            # the SHAPE of `user` first (`_SLACK_USER_ID`).
            "UID": synth.slack_user_id("ava@acme.com"),
        }.get(v, v)
        for k, v in args.items()
    }
    answered = wclient.post(
        f"/slack/api/{method}", headers=_uh(tokens, "bob@acme.com"), data=sent
    ).json()
    assert answered == {"ok": False, "error": "channel_not_found"}, method


def _a_corpus_message(ro_conn, shape: str) -> tuple[str, str, str]:
    """``(author, channel, ts)`` of a corpus message of this shape. The sample corpus holds one
    threaded channel and two standalone messages, in different channels, so neither row can be
    asked for by naming a channel."""
    row = ro_conn.execute(
        f"SELECT author_email, channel, ts FROM slack_messages WHERE {shape} LIMIT 1"
    ).fetchone()
    assert row is not None, shape
    return row["author_email"], row["channel"], row["ts"]


def test_the_service_token_cannot_delete_a_persons_message(wclient, tokens, admin_h):
    # The admin token bypasses the ACL, which is about what it may SEE. Authorship is a different
    # question and it is not the author, so `cant_delete_message` is the honest answer.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "hers"}
    ).json()["ts"]
    j = wclient.post(
        "/slack/api/chat.delete", headers=admin_h, data={"channel": cid, "ts": ts}
    ).json()
    assert j == {"ok": False, "error": "cant_delete_message"}


def test_get_permalink_returns_an_archives_url(wclient, tokens):
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "link me"}
    ).json()["ts"]
    j = wclient.post(
        "/slack/api/chat.getPermalink", headers=h, data={"channel": cid, "message_ts": ts}
    ).json()
    assert j["ok"] is True and j["channel"] == cid
    assert j["permalink"].endswith(f"/archives/{cid}/p{ts.replace('.', '')}")


def test_delete_and_update_answer_over_get_too(wclient, tokens):
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "verb check"}
    ).json()["ts"]
    upd = wclient.get(
        "/slack/api/chat.update",
        params={"channel": cid, "ts": ts, "text": "verb checked"},
        headers=h,
    ).json()
    assert upd["ok"] is True
    perma = wclient.get(
        "/slack/api/chat.getPermalink", params={"channel": cid, "message_ts": ts}, headers=h
    ).json()
    assert perma["ok"] is True
    dele = wclient.get(
        "/slack/api/chat.delete", params={"channel": cid, "ts": ts}, headers=h
    ).json()
    assert dele["ok"] is True


def test_reaction_add_shows_up_wherever_the_message_is_served(wclient, tokens):
    # `reactions` is a column the corpus already carries and every message payload already serves,
    # so this is a patch to a served field rather than a new entity.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "react to me"}
    ).json()["ts"]
    assert wclient.post(
        "/slack/api/reactions.add",
        headers=h,
        data={"channel": cid, "timestamp": ts, "name": "tada"},
    ).json() == {"ok": True}
    got = wclient.post(
        "/slack/api/reactions.get", headers=h, data={"channel": cid, "timestamp": ts}
    ).json()
    assert got["ok"] is True and got["channel"] == cid
    assert [r["name"] for r in got["message"]["reactions"]] == ["tada"]
    assert got["message"]["reactions"][0]["users"] == [synth.slack_user_id("ava@acme.com")]
    assert got["message"]["reactions"][0]["count"] == 1
    hist = wclient.post(
        "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    msg = [m for m in hist if m["ts"] == ts][0]
    assert "tada" in [r["name"] for r in msg["reactions"]]
    # Stored as the ADDRESS, the way a corpus states one — `_reactions` hashes it into the id
    # above when it serves the message, so storing the id would hash it twice. No stored `count`
    # either: it is derived from the list, and a stated one is a second place to be wrong.
    stored = json.loads(
        wclient.app.state.conn.execute(
            "SELECT value FROM ov.slack_patch WHERE channel = ? AND ts = ? AND field = 'reactions'",
            ("incidents", ts),
        ).fetchone()[0]
    )
    assert stored == [{"name": "tada", "users": ["ava@acme.com"]}]


def test_a_second_person_joins_an_existing_reaction(wclient, tokens):
    ava = _uh(tokens, "ava@acme.com")
    bob = _uh(tokens, "bob@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=ava, data={"channel": cid, "text": "both of us"}
    ).json()["ts"]
    for h in (ava, bob):
        wclient.post(
            "/slack/api/reactions.add",
            headers=h,
            data={"channel": cid, "timestamp": ts, "name": "eyes"},
        )
    got = wclient.post(
        "/slack/api/reactions.get", headers=ava, data={"channel": cid, "timestamp": ts}
    ).json()
    reaction = got["message"]["reactions"][0]
    assert reaction["count"] == 2
    assert set(reaction["users"]) == {
        synth.slack_user_id("ava@acme.com"),
        synth.slack_user_id("bob@acme.com"),
    }


def test_reacting_to_an_existing_corpus_reaction_keeps_it(wclient, tokens, ro_conn):
    # The sample corpus ships a message with reactions already on it. A patch REPLACES the column,
    # so an add has to read what is there and put it back, not start from empty.
    channel, ts = ro_conn.execute(
        "SELECT channel, ts FROM slack_messages WHERE reactions IS NOT NULL AND reactions != '' "
        "AND channel = 'incidents' LIMIT 1"
    ).fetchone()
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id(channel)
    before = wclient.post(
        "/slack/api/reactions.get", headers=h, data={"channel": cid, "timestamp": ts}
    ).json()["message"]["reactions"]
    assert before, "sample corpus should carry a message with reactions"
    wclient.post(
        "/slack/api/reactions.add",
        headers=h,
        data={"channel": cid, "timestamp": ts, "name": "rocket"},
    )
    after = wclient.post(
        "/slack/api/reactions.get", headers=h, data={"channel": cid, "timestamp": ts}
    ).json()["message"]["reactions"]
    assert {r["name"] for r in before} < {r["name"] for r in after}
    assert "rocket" in {r["name"] for r in after}


def test_reacting_twice_is_already_reacted(wclient, tokens):
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "twice"}
    ).json()["ts"]
    wclient.post(
        "/slack/api/reactions.add",
        headers=h,
        data={"channel": cid, "timestamp": ts, "name": "eyes"},
    )
    again = wclient.post(
        "/slack/api/reactions.add",
        headers=h,
        data={"channel": cid, "timestamp": ts, "name": "eyes"},
    ).json()
    assert again == {"ok": False, "error": "already_reacted"}


def test_removing_a_reaction_takes_only_the_callers_own(wclient, tokens):
    ava = _uh(tokens, "ava@acme.com")
    bob = _uh(tokens, "bob@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=ava, data={"channel": cid, "text": "shared"}
    ).json()["ts"]
    for h in (ava, bob):
        wclient.post(
            "/slack/api/reactions.add",
            headers=h,
            data={"channel": cid, "timestamp": ts, "name": "eyes"},
        )
    assert wclient.post(
        "/slack/api/reactions.remove",
        headers=ava,
        data={"channel": cid, "timestamp": ts, "name": "eyes"},
    ).json() == {"ok": True}
    got = wclient.post(
        "/slack/api/reactions.get", headers=bob, data={"channel": cid, "timestamp": ts}
    ).json()
    reaction = got["message"]["reactions"][0]
    assert reaction["count"] == 1
    assert reaction["users"] == [synth.slack_user_id("bob@acme.com")]


def test_the_last_person_removing_a_reaction_removes_it_entirely(wclient, tokens):
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "solo"}
    ).json()["ts"]
    wclient.post(
        "/slack/api/reactions.add",
        headers=h,
        data={"channel": cid, "timestamp": ts, "name": "tada"},
    )
    wclient.post(
        "/slack/api/reactions.remove",
        headers=h,
        data={"channel": cid, "timestamp": ts, "name": "tada"},
    )
    got = wclient.post(
        "/slack/api/reactions.get", headers=h, data={"channel": cid, "timestamp": ts}
    ).json()
    # `_message` omits the key entirely when there are none, the way it always has for a message
    # nobody reacted to.
    assert "reactions" not in got["message"]


def test_removing_a_reaction_nobody_left_is_no_reaction(wclient, tokens):
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "nothing here"}
    ).json()["ts"]
    j = wclient.post(
        "/slack/api/reactions.remove",
        headers=h,
        data={"channel": cid, "timestamp": ts, "name": "tada"},
    ).json()
    assert j == {"ok": False, "error": "no_reaction"}


def test_reacting_to_an_unreadable_message_does_not_reveal_it(wclient, tokens, ro_conn):
    # The ACL decision. A caller who cannot see the channel is told it does not exist, before
    # anything about the message is looked up — so the error cannot confirm the ts is real.
    ts = ro_conn.execute(
        "SELECT ts FROM slack_messages WHERE channel = 'people-confidential' LIMIT 1"
    ).fetchone()[0]
    h = _uh(tokens, "bob@acme.com")
    cid = synth.slack_channel_id("people-confidential")
    real = wclient.post(
        "/slack/api/reactions.add",
        headers=h,
        data={"channel": cid, "timestamp": ts, "name": "tada"},
    ).json()
    invented = wclient.post(
        "/slack/api/reactions.add",
        headers=h,
        data={"channel": cid, "timestamp": "1.000001", "name": "tada"},
    ).json()
    assert real == invented == {"ok": False, "error": "channel_not_found"}


# Each row is one request and what `reactions.add`, `reactions.remove` and `reactions.get`
# answer it, with an emoji the workspace has and a `timestamp` naming no message. `GOOD` stands
# for a channel the caller can read, `BAD` for an id nothing has. `_reaction_target` records
# the measurement and the three rules the table encodes: the file forms win and
# `reactions.add` does not read them, `reactions.get` resolves the channel before the
# timestamp where the other two read the timestamp first, and an absent `channel` is
# `no_item_specified` however the `timestamp` beside it reads.
_ITEM_FORMS = [
    ({}, "invalid_arguments", "invalid_arguments", "no_item_specified"),
    ({"name": "tada"}, "invalid_arguments", "no_item_specified", "no_item_specified"),
    ({"name": "tada", "channel": "GOOD"}, "invalid_arguments", "bad_timestamp", "bad_timestamp"),
    (
        {"name": "tada", "channel": "BAD"},
        "invalid_arguments",
        "bad_timestamp",
        "channel_not_found",
    ),
    (
        {"name": "tada", "timestamp": "1.000001"},
        "invalid_arguments",
        "no_item_specified",
        "no_item_specified",
    ),
    (
        {"name": "tada", "channel": "GOOD", "timestamp": ""},
        "bad_timestamp",
        "bad_timestamp",
        "bad_timestamp",
    ),
    (
        {"name": "tada", "channel": "GOOD", "timestamp": "0"},
        "bad_timestamp",
        "bad_timestamp",
        "bad_timestamp",
    ),
    (
        {"name": "tada", "channel": "", "timestamp": "1.000001"},
        "channel_not_found",
        "channel_not_found",
        "channel_not_found",
    ),
    (
        {"name": "tada", "channel": "BAD", "timestamp": "1.000001"},
        "channel_not_found",
        "channel_not_found",
        "channel_not_found",
    ),
    (
        {"name": "tada", "channel": "GOOD", "timestamp": "abc"},
        "message_not_found",
        "message_not_found",
        "message_not_found",
    ),
    (
        {"name": "tada", "file": "F00000000"},
        "invalid_arguments",
        "file_not_found",
        "file_not_found",
    ),
    ({"name": "tada", "file": ""}, "invalid_arguments", "file_not_found", "file_not_found"),
    (
        {"name": "tada", "file_comment": "Fc00000000"},
        "invalid_arguments",
        "file_comment_not_found",
        "file_comment_not_found",
    ),
    (
        {"name": "tada", "channel": "GOOD", "timestamp": "1.000001", "file": "F00000000"},
        "message_not_found",
        "file_not_found",
        "file_not_found",
    ),
]


@pytest.mark.parametrize("sent, add, remove, get", _ITEM_FORMS)
def test_each_reaction_method_answers_an_unresolvable_item_its_own_way(
    wclient, tokens, sent, add, remove, get
):
    h = _uh(tokens, "ava@acme.com")
    args = {
        k: {"GOOD": synth.slack_channel_id("incidents"), "BAD": "C00000000"}.get(v, v)
        for k, v in sent.items()
    }
    for method, want in (
        ("reactions.add", add),
        ("reactions.remove", remove),
        ("reactions.get", get),
    ):
        answered = wclient.post(f"/slack/api/{method}", headers=h, data=args).json()
        assert answered["ok"] is False, (method, sent)
        assert answered["error"] == want, (method, sent)


def test_reacting_with_no_item_names_the_arguments_that_are_missing(wclient, tokens):
    # `_reaction_target` records the measurement: real names them rather than answering
    # `no_item_specified`, and `timestamp` comes before `channel`.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    assert wclient.post("/slack/api/reactions.add", headers=h, data={"name": "tada"}).json() == {
        "ok": False,
        "error": "invalid_arguments",
        "response_metadata": {
            "messages": [
                "[ERROR] missing required field: timestamp",
                "[ERROR] missing required field: channel",
            ]
        },
    }
    for sent, named in (
        ({"channel": cid}, "timestamp"),
        ({"timestamp": "1.000001"}, "channel"),
    ):
        j = wclient.post(
            "/slack/api/reactions.add", headers=h, data={"name": "tada", **sent}
        ).json()
        assert j["response_metadata"]["messages"] == [f"[ERROR] missing required field: {named}"]
    # A padded name is not normalised into the bare one real would have accepted. Real refuses
    # ` eyes ` outright, which needs the emoji set and stays a recorded gap; what must not happen
    # is silently reacting with `eyes`.
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "padded"}
    ).json()["ts"]
    wclient.post(
        "/slack/api/reactions.add",
        headers=h,
        data={"channel": cid, "timestamp": ts, "name": " eyes "},
    )
    got = wclient.post(
        "/slack/api/reactions.get", headers=h, data={"channel": cid, "timestamp": ts}
    ).json()
    assert [r["name"] for r in got["message"]["reactions"]] == [" eyes "]


def test_reactions_list_is_the_callers_own(wclient, tokens):
    ava = _uh(tokens, "ava@acme.com")
    bob = _uh(tokens, "bob@acme.com")
    cid = synth.slack_channel_id("incidents")
    mine = wclient.post(
        "/slack/api/chat.postMessage", headers=ava, data={"channel": cid, "text": "listed"}
    ).json()["ts"]
    theirs = wclient.post(
        "/slack/api/chat.postMessage", headers=ava, data={"channel": cid, "text": "not listed"}
    ).json()["ts"]
    wclient.post(
        "/slack/api/reactions.add",
        headers=ava,
        data={"channel": cid, "timestamp": mine, "name": "rocket"},
    )
    wclient.post(
        "/slack/api/reactions.add",
        headers=bob,
        data={"channel": cid, "timestamp": theirs, "name": "rocket"},
    )
    j = wclient.post("/slack/api/reactions.list", headers=ava).json()
    assert j["ok"] is True
    listed = [i["message"]["ts"] for i in j["items"] if i["type"] == "message"]
    assert mine in listed and theirs not in listed
    # A reaction the CORPUS states is this caller's too: ava reacted to a message in the sample.
    corpus_reacted = [t for t in listed if t not in (mine, theirs)]
    assert corpus_reacted, listed
    # The most recent is first, which the one just written is.
    assert listed[0] == mine
    # `user` names whose list to answer, and an id nobody has is `user_not_found`.
    as_bob = wclient.post(
        "/slack/api/reactions.list",
        headers=ava,
        data={"user": synth.slack_user_id("bob@acme.com")},
    ).json()
    assert theirs in [i["message"]["ts"] for i in as_bob["items"]]
    assert mine not in [i["message"]["ts"] for i in as_bob["items"]]
    assert wclient.post(
        "/slack/api/reactions.list", headers=ava, data={"user": "U00000000"}
    ).json() == {"ok": False, "error": "user_not_found"}
    # The answer carries a cursor and no `paging` object.
    assert "paging" not in j and j["response_metadata"]["next_cursor"] == ""
    # A number real cannot read is refused, and the refusal names each field in this order
    # whichever was sent first. `page` is validated without being honoured: refusing `page=abc`
    # and paging by `page=2` are different claims, and only the first is made.
    refused = wclient.post(
        "/slack/api/reactions.list", headers=ava, data={"page": "-0", "count": "abc"}
    ).json()
    assert refused["ok"] is False and refused["error"] == "invalid_arguments"
    assert refused["response_metadata"]["messages"] == [
        "[ERROR] must provide a number [json-pointer:/count]",
        "[ERROR] must provide a number [json-pointer:/page]",
    ]
    # `limit` is an integer on this method, where a paged reader would serve its integer part.
    assert wclient.post("/slack/api/reactions.list", headers=ava, data={"limit": "1.5"}).json()[
        "response_metadata"
    ]["messages"] == ["[ERROR] must provide a number [json-pointer:/limit]"]
    # `limit` sizes the page and `count` does not, and the cursor walks the rest.
    wclient.post(
        "/slack/api/reactions.add",
        headers=ava,
        data={"channel": cid, "timestamp": theirs, "name": "eyes"},
    )
    # The page is read against the whole list rather than against any assumed order.
    whole = [
        i["message"]["ts"]
        for i in wclient.post("/slack/api/reactions.list", headers=ava).json()["items"]
    ]
    assert {mine, theirs} <= set(whole)
    one = wclient.post(
        "/slack/api/reactions.list", headers=ava, data={"limit": "1", "count": "2"}
    ).json()
    assert [i["message"]["ts"] for i in one["items"]] == whole[:1]
    rest = wclient.post(
        "/slack/api/reactions.list",
        headers=ava,
        data={"limit": "1", "cursor": one["response_metadata"]["next_cursor"]},
    ).json()
    assert [i["message"]["ts"] for i in rest["items"]] == whole[1:2]
    # A `limit` at or below zero is the default page, not an empty one.
    for value in ("0", "-1"):
        zero = wclient.post("/slack/api/reactions.list", headers=ava, data={"limit": value}).json()
        assert [i["message"]["ts"] for i in zero["items"]] == whole
    assert (
        wclient.post("/slack/api/reactions.list", headers=ava, data={"cursor": "bogus"}).json()[
            "error"
        ]
        == "invalid_cursor"
    )


def test_reactions_answer_over_get_too(wclient, tokens):
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "verbs"}
    ).json()["ts"]
    assert (
        wclient.get(
            "/slack/api/reactions.add",
            params={"channel": cid, "timestamp": ts, "name": "tada"},
            headers=h,
        ).json()["ok"]
        is True
    )
    assert (
        wclient.get(
            "/slack/api/reactions.get", params={"channel": cid, "timestamp": ts}, headers=h
        ).json()["ok"]
        is True
    )
    assert wclient.get("/slack/api/reactions.list", headers=h).json()["ok"] is True
    assert (
        wclient.get(
            "/slack/api/reactions.remove",
            params={"channel": cid, "timestamp": ts, "name": "tada"},
            headers=h,
        ).json()["ok"]
        is True
    )


def test_posting_again_in_the_same_second_after_a_delete(wclient, tokens):
    # `slack_next_ts` probes for a free fraction, and a tombstoned message reads as absent — so
    # the ts it just freed came back, and the insert hit the primary key with a 500. A
    # post-delete-post inside one second is an ordinary agent sequence.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    first = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "one"}
    ).json()
    wclient.post("/slack/api/chat.delete", headers=h, data={"channel": cid, "ts": first["ts"]})
    second = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "two"}
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["ok"] is True
    assert body["ts"] != first["ts"]
    hist = wclient.post(
        "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    assert body["ts"] in [m["ts"] for m in hist]
    assert first["ts"] not in [m["ts"] for m in hist]


def test_editing_a_corpus_message_leaves_one_search_hit(wclient, tokens, ro_conn):
    # A patched CORPUS row is indexed in the overlay while its entry stays in the corpus index, so
    # search found the same document twice and count_search added it twice over. The old text must
    # also stop matching.
    email, channel, ts, content = ro_conn.execute(
        "SELECT author_email, channel, ts, content FROM slack_messages "
        "WHERE channel = 'incidents' AND thread_seq = 0 LIMIT 1"
    ).fetchone()
    h = _uh(tokens, email)
    cid = synth.slack_channel_id(channel)
    old_word = [w for w in content.split() if len(w) > 4][0].strip(".,?!")
    wclient.post(
        "/slack/api/chat.update",
        headers=h,
        data={"channel": cid, "ts": ts, "text": "zarquon replaced the body"},
    )
    hits = wclient.post("/slack/api/search.messages", headers=h, data={"query": "zarquon"}).json()[
        "messages"
    ]
    matched = [m["ts"] for m in hits["matches"]]
    assert matched.count(ts) == 1, f"duplicated in the page: {matched}"
    assert hits["total"] == len(hits["matches"]), "the total contradicts the page it describes"
    # the corpus index still holds the pre-edit text; a search for it must not return the message
    stale = wclient.post("/slack/api/search.messages", headers=h, data={"query": old_word}).json()[
        "messages"
    ]
    assert ts not in [m["ts"] for m in stale["matches"]], f"{old_word!r} still matches the old text"
    assert stale["total"] == len(stale["matches"])


def test_posting_with_thread_ts_replies_in_that_thread(wclient, tokens, ro_conn):
    # Ignoring `thread_ts` gave an agent told to reply in a thread a top-level message and a
    # `200 ok` — a wrong answer with no error in it.
    root_ts = ro_conn.execute(
        "SELECT ts FROM slack_messages WHERE channel = 'incidents' AND thread_seq = 0 "
        "AND thread_ts IS NOT NULL LIMIT 1"
    ).fetchone()[0]
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    before = wclient.post(
        "/slack/api/conversations.replies", headers=h, data={"channel": cid, "ts": root_ts}
    ).json()["messages"]
    posted = wclient.post(
        "/slack/api/chat.postMessage",
        headers=h,
        data={"channel": cid, "text": "threaded reply", "thread_ts": root_ts},
    ).json()
    assert posted["ok"] is True
    after = wclient.post(
        "/slack/api/conversations.replies", headers=h, data={"channel": cid, "ts": root_ts}
    ).json()["messages"]
    assert [m["ts"] for m in after][-1] == posted["ts"]
    assert len(after) == len(before) + 1
    # and it is NOT a top-level message
    hist = wclient.post(
        "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    assert posted["ts"] not in [m["ts"] for m in hist]
    # A reply to a STANDALONE message makes that message a root, which the corpus spells as the
    # message carrying its own ts. Without the stamp `conversations.replies` answers the parent
    # alone — a thread this API made and cannot show.
    alone = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "alone"}
    ).json()["ts"]
    answered = wclient.post(
        "/slack/api/chat.postMessage",
        headers=h,
        data={"channel": cid, "text": "answering it", "thread_ts": alone},
    ).json()["ts"]
    thread = wclient.post(
        "/slack/api/conversations.replies", headers=h, data={"channel": cid, "ts": alone}
    ).json()["messages"]
    assert [m["ts"] for m in thread] == [alone, answered]
    assert thread[0]["thread_ts"] == alone and thread[0]["reply_count"] == 1


def test_replying_to_a_reply_lands_in_the_same_thread(wclient, tokens, ro_conn):
    # Slack threads under the ROOT, not under whichever message was named.
    root_ts, reply_ts = ro_conn.execute(
        "SELECT thread_ts, ts FROM slack_messages "
        "WHERE channel = 'incidents' AND thread_seq > 0 LIMIT 1"
    ).fetchone()
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    posted = wclient.post(
        "/slack/api/chat.postMessage",
        headers=h,
        data={"channel": cid, "text": "reply to a reply", "thread_ts": reply_ts},
    ).json()
    assert posted["ok"] is True
    thread = wclient.post(
        "/slack/api/conversations.replies", headers=h, data={"channel": cid, "ts": root_ts}
    ).json()["messages"]
    assert posted["ts"] in [m["ts"] for m in thread]


def test_a_thread_ts_naming_nothing_posts_top_level_and_an_unreadable_one_is_refused(
    wclient, tokens
):
    # `chat_post_message` records the measurement: a well-formed `thread_ts` that names no message
    # posts top-level, an empty one does the same, and one real cannot read is refused.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    for value in ("1700000000.000001", ""):
        j = wclient.post(
            "/slack/api/chat.postMessage",
            headers=h,
            data={"channel": cid, "text": "orphan", "thread_ts": value},
        ).json()
        assert j["ok"] is True, value
        assert "thread_ts" not in j["message"], value
    # Refused outside the window `_thread_ts_error` records, and the message carries the value
    # as real reads it, in microseconds.
    import time as _time

    for value, micros in (
        ("abc", 0),
        ("-1", 1_000_000),
        ("1.2.3", 1_000_002),
        ("1e9", 19_000_000),
        ("0000000000.000001", 1),
        ("631152000.000000", 631_152_000_000_000),
        (str(int(_time.time()) + 7200), None),
    ):
        answered = wclient.post(
            "/slack/api/chat.postMessage",
            headers=h,
            data={"channel": cid, "text": "orphan", "thread_ts": value},
        ).json()
        assert answered["ok"] is False, value
        assert answered["error"] == "invalid_thread_ts", value
        if micros is not None:
            assert answered["response_metadata"]["messages"] == [
                f"[ERROR] {micros} is not a valid thread_ts"
            ], value
    # And the floor is a second wide: one second above it posts.
    assert (
        wclient.post(
            "/slack/api/chat.postMessage",
            headers=h,
            data={"channel": cid, "text": "just inside", "thread_ts": "631152001.000000"},
        ).json()["ok"]
        is True
    )


@pytest.mark.parametrize("who", ["admin", "person"])
def test_a_posted_message_carries_bot_id_and_blocks_and_no_subtype(wclient, tokens, admin_h, who):
    # `_message` records the measurement: real carries `bot_id` and `blocks` on every message
    # posted over the Web API, under a user token as well as a bot one, in history as well as in
    # the echo, and no `subtype` on either. A corpus message carries `client_msg_id` and no
    # `bot_id`, since it stands for one a Slack client posted.
    h = admin_h if who == "admin" else _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "ci says"}
    ).json()["ts"]
    hist = wclient.post(
        "/slack/api/conversations.history", headers=admin_h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    msg = [m for m in hist if m["ts"] == ts][0]
    assert "bot_id" in msg and "blocks" in msg
    assert "subtype" not in msg and "client_msg_id" not in msg
    corpus = [m for m in hist if m["ts"] != ts and "client_msg_id" in m]
    assert corpus, "the sample corpus has no plain message to contrast with"
    assert "bot_id" not in corpus[0]


def test_an_author_with_a_differently_cased_address_can_edit_their_own_message(wclient, tokens):
    # `_subscribed` already case-folds the same addresses; authorship has to agree, or a corpus
    # that wrote an address in mixed case locks its own author out of their message.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "mine"}
    ).json()["ts"]
    conn = wclient.app.state.conn
    conn.execute(
        "UPDATE ov.slack_messages SET author_email = ? WHERE channel = ? AND ts = ?",
        ("Ava@Acme.com", "incidents", ts),
    )
    conn.commit()
    j = wclient.post(
        "/slack/api/chat.update", headers=h, data={"channel": cid, "ts": ts, "text": "edited"}
    ).json()
    assert j["ok"] is True


def test_a_bot_messages_author_resolves_through_users_info(wclient, admin_h):
    # `auth.test`'s docstring says a client finds its own messages by matching `user_id` against
    # message authors; resolving that id then has to work, or the flow stops one step later.
    cid = synth.slack_channel_id("incidents")
    posted = wclient.post(
        "/slack/api/chat.postMessage", headers=admin_h, data={"channel": cid, "text": "ci"}
    ).json()
    uid = posted["message"]["user"]
    j = wclient.post("/slack/api/users.info", headers=admin_h, data={"user": uid}).json()
    assert j["ok"] is True
    assert j["user"]["id"] == uid
    assert j["user"]["is_bot"] is True
