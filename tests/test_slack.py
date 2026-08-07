"""Slack's Web API surface: conversations, users, and search.

One file per router, so a provider's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import pytest

from backlot import store, synth
from tests._helpers import crawl_slack, db_count, tiny_corpus


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


# --- Slack fidelity (#33) ---------------------------------------------------------------------
#
# Reported from building a filesystem-style Slack client against the mock. Slack answers an
# application error as HTTP 200 with {"ok": false, "error": …}, which the mock already does — these
# are about the cases where it answered something real Slack never would.
#
# NOTE: unlike the Google work in #37/#39, these expectations come from Slack's published reference
# rather than from probing the live API — there are no Slack credentials in this environment. Each
# one cites the documented behaviour it encodes.


def _a_channel_id(client, admin_h):
    return client.get("/slack/api/conversations.list", headers=admin_h, params={"limit": 1}).json()[
        "channels"
    ][0]["id"]


@pytest.mark.parametrize(
    "types, expect_channels",
    [
        ("public_channel", True),
        ("private_channel", True),
        ("public_channel,private_channel", True),
        ("im", False),
        ("mpim", False),
        ("im,mpim", False),
    ],
)
def test_slack_conversations_list_honours_types(client, admin_h, types, expect_channels):
    """`types` was ignored, so `im` returned every public channel and a client presenting
    `channels/` and `dms/` separately got each channel under both. This corpus has no DMs, so `im`
    must come back empty — which is exactly what real Slack answers for a DM-less workspace, making
    "no DMs here" indistinguishable from production instead of indistinguishable from a bug."""
    j = client.get(
        "/slack/api/conversations.list", headers=admin_h, params={"types": types, "limit": 5}
    ).json()
    assert j["ok"] is True
    assert bool(j["channels"]) is expect_channels, j["channels"][:1]
    assert all(c["is_im"] is False and c["is_mpim"] is False for c in j["channels"])


def test_slack_conversations_list_defaults_to_public_channels(client, admin_h):
    """Slack's documented default when `types` is omitted is `public_channel`."""
    omitted = client.get(
        "/slack/api/conversations.list", headers=admin_h, params={"limit": 5}
    ).json()
    explicit = client.get(
        "/slack/api/conversations.list",
        headers=admin_h,
        params={"limit": 5, "types": "public_channel"},
    ).json()
    assert omitted["channels"] == explicit["channels"]
    assert omitted["channels"]


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


def test_slack_members_are_the_channels_own_speakers(client, admin_h, ro_conn):
    """Every public channel reported the same membership — the entire roster — because the handler
    skipped membership for a public channel. Real Slack's membership differs per channel, and a
    workspace where every channel holds everybody is not a shape it produces.

    Membership is now the channel's own participants, which is what the corpus actually knows."""
    chans = client.get(
        "/slack/api/conversations.list", headers=admin_h, params={"limit": 100}
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
        info = client.get(
            "/slack/api/conversations.info", headers=admin_h, params={"channel": c["id"]}
        ).json()["channel"]
        assert info["num_members"] == len(listed), c["name"]


def test_slack_members_channel_not_found(client, admin_h):
    j = client.get(
        "/slack/api/conversations.members", headers=admin_h, params={"channel": "C_NOPE"}
    ).json()
    assert j == {"ok": False, "error": "channel_not_found"}


def test_slack_search_all(client, admin_h):
    # slack-go's Search()/SearchContext() hits search.all; it must return both messages + files.
    j = client.post("/slack/api/search.all", headers=admin_h, data={"query": "the"}).json()
    assert j["ok"] is True
    assert "messages" in j and "files" in j
    assert j["files"]["total"] == 0 and j["files"]["matches"] == []


def test_slack_replies_resolve_from_a_reply_ts(client, admin_h):
    # A search hit that lands on a REPLY yields that reply's ts; conversations.replies must return
    # the whole thread from it (Slack accepts any in-thread ts), not thread_not_found. The SAMPLE
    # 'incidents' 502 thread's replies include "Rolled back; 502s clearing." Regression: previously
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
    from backlot.routers.slack import _message, _full_channel

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
    thread = store.slack_thread(conn, "s1")
    root, first_reply = thread[0], thread[1]
    ru = store.slack_reply_authors(conn, "s1")
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

    req = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace()))
    ch = _full_channel(req, conn, "inc")
    assert ch["num_members"] > 0 and ch["creator"] == "USERVICE0"
