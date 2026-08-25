"""Slack's Web API surface: conversations, users, and search.

One file per router, so a source's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import pytest

from backlot import store, synth
from backlot.routers import slack
from tests._helpers import corpus_client, crawl_slack, db_count, tiny_corpus


def test_admin_slack_crawls_all(client, admin_h, ro_conn):
    assert crawl_slack(client, admin_h) == db_count(ro_conn, "slack")


def test_slack_api_test_requires_no_auth(client):
    # real Slack's api.test needs no token at all (it's a bare connectivity check); several real
    # clients call it at construction/connect time (e.g. llama-index's SlackReader.__init__), so
    # the mock must answer 200 without auth rather than 404/not_authed.
    ok = client.post("/slack/api/api.test", data={"foo": "bar"}).json()
    assert ok == {"ok": True, "args": {"foo": "bar"}}
    err = client.post("/slack/api/api.test", data={"error": "boom"}).json()
    assert err == {"ok": False, "error": "boom"}


def test_slack_accepts_form_field_token(client, tokens_yaml):
    # the official slack-go SDK posts the token as a form field (no bearer header); the mock
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
# Reported from building a filesystem-style Slack client against the mock. Slack answers an
# application error as HTTP 200 with {"ok": false, "error": …}, which the mock already does — these
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
# whether a credential was presented — and Slack recognises exactly three ways to present one: an
# `Authorization: Bearer` header, a `token` query param, or a `token` form field. Every row is a
# live answer, including the ones that read as though they should go the other way.
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


def test_slack_auth_error_split_is_uniform_across_methods(client):
    """The split is decided once, so no method is left answering the old single error."""
    bogus = {"Authorization": "Bearer xoxb-not-a-real-token"}
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
        assert ch["context_team_id"] == "T0000MOCK"
        assert ch["shared_team_ids"] == ["T0000MOCK"]
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
def test_slack_by_id_methods_refuse_a_channel_the_caller_cannot_see(
    client, admin_h, tokens, method, extra, when_visible
):
    """`conversations.list` scopes by ACL; the methods that resolve a channel by id did not, so an
    id was enough for any authenticated principal to confirm a private room, read its name, topic,
    purpose and creation time, and enumerate who is in it — `conversations.members` being a
    projection of the very messages the ACL is withholding. `conversations.history` answered
    `ok:true` with an empty `messages`, which reads as "this channel exists and you may read it; it
    happens to be empty" — the opposite of what the mock knows.

    All four answer `channel_not_found`, the same answer an id that names nothing gets, so a hidden
    channel is not distinguishable from one that was never there. Measured against slack.com with a
    workspace token, on a private channel that token is not in beside a well-formed id for no
    channel at all — every one of the eight answers is `channel_not_found`. NOT the `not_in_channel`
    that `conversations.history` also documents: that is the case where the token can SEE the
    channel and has not joined it, which is a public channel here and never refused, and the other
    three methods do not document the error at all."""
    private = _private_channel(client, admin_h)
    hidden_and_made_up = ({"channel": private["id"]}, {"channel": "C0000000000"})
    for email in ("ava@acme.com", "bob@acme.com"):
        h = {"Authorization": f"Bearer {tokens[email]}"}
        for params in hidden_and_made_up:
            j = client.get(f"/slack/api/{method}", headers=h, params={**params, **extra}).json()
            assert j == {"ok": False, "error": "channel_not_found"}, (email, params)

    def answer(headers, channel):
        j = client.get(
            f"/slack/api/{method}", headers=headers, params={"channel": channel, **extra}
        ).json()
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
    """Real Slack answers `invalid_types`; the mock accepted anything, so a typo'd filter silently
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


@pytest.mark.parametrize(
    "param, error", [("latest", "invalid_ts_latest"), ("oldest", "invalid_ts_oldest")]
)
def test_slack_history_rejects_a_malformed_timestamp(client, admin_h, param, error):
    """`float(oldest)` was unguarded, so a bad argument was a 500 — which clients that back off on
    5xx will retry, burning the whole budget on a request that can never succeed. Real Slack
    answers 200 with the named error."""
    r = client.get(
        "/slack/api/conversations.history",
        headers=admin_h,
        params={"channel": _a_channel_id(client, admin_h), param: "not-a-ts"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": error}


@pytest.mark.parametrize("path", ["conversations.list", "users.list"])
def test_slack_rejects_an_invalid_cursor(client, admin_h, path):
    """An undecodable cursor was treated as offset 0, so a client paginating with a corrupted
    cursor looped on page 1 forever instead of failing. Real Slack answers `invalid_cursor`."""
    for bad in ("bogus", "###"):
        j = client.get(f"/slack/api/{path}", headers=admin_h, params={"cursor": bad}).json()
        assert j == {"ok": False, "error": "invalid_cursor"}, (path, bad)


def test_slack_history_rejects_an_invalid_cursor(client, admin_h):
    j = client.get(
        "/slack/api/conversations.history",
        headers=admin_h,
        params={"channel": _a_channel_id(client, admin_h), "cursor": "bogus"},
    ).json()
    assert j == {"ok": False, "error": "invalid_cursor"}


def test_slack_members_are_the_channels_own_speakers(client, admin_h, tokens, ro_conn):
    """Every public channel reported the same membership — the entire roster — because the handler
    skipped membership for a public channel. Real Slack's membership differs per channel, and a
    workspace where every channel holds everybody is not a shape it produces.

    Membership is now the channel's own participants, which is what the corpus actually knows — and
    `is_member` is the caller's own place in that same set. It was the constant `True`, so every
    channel a caller could see claimed them as a member, including public ones they have never
    posted in; a client that stats a channel and then walks its members got two answers to one
    question."""
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
    "method, params, error",
    [
        # An ABSENT required argument — the request is malformed, and no channel was ever named.
        ("conversations.info", {}, "invalid_arguments"),
        ("conversations.members", {}, "invalid_arguments"),
        ("conversations.history", {}, "invalid_arguments"),
        ("conversations.replies", {}, "invalid_arguments"),
        ("conversations.replies", {"channel": "C_NOPE"}, "invalid_arguments"),  # ts absent
        # PRESENT and empty, or present and naming nothing — the argument arrived, so what is
        # missing is the channel.
        ("conversations.info", {"channel": ""}, "channel_not_found"),
        ("conversations.members", {"channel": ""}, "channel_not_found"),
        ("conversations.history", {"channel": ""}, "channel_not_found"),
        ("conversations.info", {"channel": "C_NOPE"}, "channel_not_found"),
        ("conversations.members", {"channel": "C_NOPE"}, "channel_not_found"),
        ("conversations.history", {"channel": "C_NOPE"}, "channel_not_found"),
        ("conversations.replies", {"channel": "C_NOPE", "ts": "1.0"}, "channel_not_found"),
        # ...and a ts that arrived empty is answered by the thread, the channel being readable.
        ("conversations.replies", {"channel": _OWN_CHANNEL, "ts": ""}, "thread_not_found"),
        # The search family draws the line in the same place, and has its OWN name for the blank
        # half: `no_query`, the vendor's spelling for a query that arrived with nothing in it.
        ("search.messages", {}, "invalid_arguments"),
        ("search.all", {}, "invalid_arguments"),
        ("search.files", {}, "invalid_arguments"),
        ("search.messages", {"query": ""}, "no_query"),
        ("search.all", {"query": ""}, "no_query"),
        ("search.files", {"query": ""}, "no_query"),
        ("search.messages", {"query": "   "}, "no_query"),  # whitespace is not a query either
        # users.info is the exception, and is left one: live it answers `user_not_found` to all of
        # them, absent argument included. Pinned so the rule above is not "tidied" onto it.
        ("users.info", {}, "user_not_found"),
        ("users.info", {"user": ""}, "user_not_found"),
    ],
)
def test_slack_an_absent_argument_is_not_a_thing_that_was_not_found(
    client, admin_h, method, params, error
):
    """Slack separates "you did not pass the argument" from "what you passed names nothing", and
    the mock had only the second — so a client that omitted `channel` was told the thing it never
    named does not exist. The two send it down different branches: `channel_not_found` is about the
    workspace, `invalid_arguments` about the request it just built.

    Every row measured against slack.com with a workspace token, users.info's included — the rule
    is the vendor's, not a symmetry imposed on it. Auth is settled first (a bad token is
    `invalid_auth` with or without the arguments), which is why this table is all authenticated."""
    params = {
        k: (_a_channel_id(client, admin_h) if v == _OWN_CHANNEL else v) for k, v in params.items()
    }
    j = client.get(f"/slack/api/{method}", headers=admin_h, params=params).json()
    assert j == {"ok": False, "error": error}


def test_slack_search_all(client, admin_h):
    # slack-go's Search()/SearchContext() hits search.all; it must return both messages + files.
    j = client.post("/slack/api/search.all", headers=admin_h, data={"query": "the"}).json()
    assert j["ok"] is True
    assert "messages" in j and "files" in j
    assert j["files"]["total"] == 0 and j["files"]["matches"] == []


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
    texts = " ".join(m["text"] for m in rep["messages"])
    assert "Anyone else seeing 502s" in texts  # thread root is returned
    assert "Rolled back" in texts  # the reply we searched for is in the same thread


# --- Slack: enrichment did not change the responses ---------------------------------------


# Transcribed from live conversations.history responses, one per shape Slack builds. The mock
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
    from backlot.routers.slack import _message, _listed_channel

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
