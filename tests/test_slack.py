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
    # Backlot must answer 200 without auth rather than 404/not_authed.
    ok = client.post("/slack/api/api.test", data={"foo": "bar"}).json()
    assert ok == {"ok": True, "args": {"foo": "bar"}}
    err = client.post("/slack/api/api.test", data={"error": "boom"}).json()
    assert err == {"ok": False, "error": "boom"}


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


def test_slack_auth_test_identifies_the_caller(client, admin_h, tokens):
    """`auth.test` answers "who am I", and both fields it answers with are the caller's own.

    Slack's spec fixes their shape — `slack_web_openapi_v2.json`,
    `paths./auth.test.get.responses.200` is `"user": "grace"`, `"user_id": "W12345678"` — so `user`
    is a handle rather than an address, and `user_id` is that person's id.

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
def test_slack_by_id_methods_refuse_a_channel_the_caller_cannot_see(
    client, admin_h, tokens, method, extra, when_visible
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


# Placed after every `client`-fixture test in this module: it opens a SECOND app over a different DB
# via `corpus_client`, which overwrites the module-scoped fixture's shared `app.state` (see
# `tests._helpers.client_for`'s docstring).


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
    `username`, and Slack spells both the handle: this method's own spec example carries
    `"username": "roach"` (`slack_web_openapi_v2.json`,
    `paths./search.messages.get.responses.200`). A client that reads the id off `user` and the
    label off `username` gets one person, so the two must not answer two spellings of them.

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


# --- writes -----------------------------------------------------------------------------------
#
# Every method answers both verbs. Measured against slack.com on 2026-09-06 with a read-only
# token: all nine answer `missing_scope` -- not `unknown_method` -- over GET and POST alike, so
# the method and the verb are recognised and the request reaches the scope check.


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


def test_post_message_to_an_unreadable_channel_is_channel_not_found(wclient, tokens):
    # Slack's non-leaking answer: a caller who cannot see the channel is told it does not exist
    # rather than that they lack permission. Quoted from the spec's chat.postMessage error enum.
    h = _uh(tokens, "bob@acme.com")
    cid = synth.slack_channel_id("people-confidential")
    j = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "hello"}
    ).json()
    assert j == {"ok": False, "error": "channel_not_found"}


def test_post_message_with_no_text_is_no_text(wclient, tokens):
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    j = wclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": ""}
    ).json()
    assert j == {"ok": False, "error": "no_text"}


def test_post_message_without_text_at_all_is_invalid_arguments(wclient, tokens):
    # `_missing_argument`'s existing split: an absent argument is about the request the client
    # built, an empty one is about the workspace. Slack answers them differently and clients
    # branch on it.
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    j = wclient.post("/slack/api/chat.postMessage", headers=h, data={"channel": cid}).json()
    assert j == {"ok": False, "error": "invalid_arguments"}


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
    # Membership is derived from having spoken, so the row just written would join the poster.
    # Real Slack does not join you when you post through the API.
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
    # auth.test already answers the service token as USERVICE0 by analogy to a bot token; a
    # message it writes carries the same identity and the subtype real Slack gives an app post.
    # A client that calls auth.test and matches user_id against message authors -- the use case
    # auth.test's own docstring names -- has to find this message.
    cid = synth.slack_channel_id("incidents")
    j = wclient.post(
        "/slack/api/chat.postMessage", headers=admin_h, data={"channel": cid, "text": "from ci"}
    ).json()
    assert j["ok"] is True
    assert j["message"]["subtype"] == "bot_message"
    assert j["message"]["user"] == "USERVICE0"
    me = wclient.post("/slack/api/auth.test", headers=admin_h).json()
    assert me["user_id"] == j["message"]["user"]
    hist = wclient.post(
        "/slack/api/conversations.history", headers=admin_h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    posted = [m for m in hist if m["ts"] == j["ts"]][0]
    assert posted["user"] == "USERVICE0" and posted["subtype"] == "bot_message"
    # A bot message carries no client_msg_id or blocks -- the client that would have minted them
    # is Slack itself. `_message` already draws that line off `subtype`.
    assert "client_msg_id" not in posted


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
    email, channel, ts = ro_conn.execute(
        "SELECT author_email, channel, ts FROM slack_messages WHERE channel = 'incidents' LIMIT 1"
    ).fetchone()
    h = _uh(tokens, email)
    cid = synth.slack_channel_id(channel)
    before = wclient.post(
        "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    assert ts in [m["ts"] for m in before]
    assert (
        wclient.post("/slack/api/chat.delete", headers=h, data={"channel": cid, "ts": ts}).json()[
            "ok"
        ]
        is True
    )
    after = wclient.post(
        "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    assert ts not in [m["ts"] for m in after]


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


def test_reacting_with_no_item_is_no_item_specified(wclient, tokens):
    h = _uh(tokens, "ava@acme.com")
    j = wclient.post("/slack/api/reactions.add", headers=h, data={"name": "tada"}).json()
    assert j == {"ok": False, "error": "no_item_specified"}


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


def test_posting_into_a_thread_that_does_not_exist_is_message_not_found(wclient, tokens):
    h = _uh(tokens, "ava@acme.com")
    cid = synth.slack_channel_id("incidents")
    j = wclient.post(
        "/slack/api/chat.postMessage",
        headers=h,
        data={"channel": cid, "text": "orphan", "thread_ts": "1.000001"},
    ).json()
    assert j == {"ok": False, "error": "message_not_found"}


def test_a_bot_message_carries_bot_id_in_history(wclient, admin_h):
    cid = synth.slack_channel_id("incidents")
    ts = wclient.post(
        "/slack/api/chat.postMessage", headers=admin_h, data={"channel": cid, "text": "ci says"}
    ).json()["ts"]
    hist = wclient.post(
        "/slack/api/conversations.history", headers=admin_h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    msg = [m for m in hist if m["ts"] == ts][0]
    assert msg["bot_id"] and msg["subtype"] == "bot_message"


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
