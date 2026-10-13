"""Regressions for the independently reproduced Teams integration review findings."""

from __future__ import annotations

import json
from contextlib import closing

import pytest

from backlot import store, synth
from backlot.importer.byo import load
from backlot.pagination import encode_cursor
from tests._helpers import build_corpus, client_for, complete, corpus_client

V1 = "/msgraph/v1.0"


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.parametrize(
    "reader,visible", [("org:other", False), ("org:acme", True), ("user:bob@acme.com", True)]
)
def test_channel_visibility_intersects_actual_principals(tmp_path, warm, reader, visible):
    records = [
        {
            "source_type": "msteams",
            "channel": "restricted",
            "author_email": "bob@acme.com",
            "readers": [reader],
        }
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
        listed = client.get(base, headers=headers)
        assert listed.status_code == 200, listed.text
        assert [row["id"] for row in listed.json()["value"]] == ([cid] if visible else [])
        for suffix in ("", "/members", "/messages"):
            response = client.get(f"{base}/{cid}{suffix}", headers=headers)
            assert response.status_code == (200 if visible else 404), response.text
            if not visible:
                assert response.json()["error"]["code"] == "NotFound"


@pytest.mark.parametrize("source", ["msteams", "slack"])
def test_append_empty_readers_revokes_existing_thread_grants(tmp_path, source):
    record = complete(
        source,
        doc_id="reimported-thread",
        channel="restricted",
        author_email="ava@acme.com",
        readers=["user:ava@acme.com"],
        replies=[{"content": "A reply.", "author_email": "ava@acme.com"}],
    )
    settings = build_corpus(tmp_path, [record])

    def read_as_ava(client):
        client.app.state.warm_thread.join()
        token = client.app.state.acl.email_to_token()["ava@acme.com"]
        headers = {"Authorization": f"Bearer {token}"}
        if source == "msteams":
            team = client.get(f"{V1}/me/joinedTeams", headers=headers).json()["value"][0]["id"]
            cid = synth.msteams_channel_id("restricted")
            return client.get(f"{V1}/teams/{team}/channels/{cid}/messages", headers=headers)
        return client.post(
            "/slack/api/conversations.history",
            headers=headers,
            data={"channel": synth.slack_channel_id("restricted")},
        )

    with client_for(settings) as client:
        response = read_as_ava(client)
        assert response.status_code == 200, response.text
        assert response.json().get("ok", True)
    with closing(store.connect_ro(settings.db_path)) as conn:
        ids = [
            tuple(row)
            for row in conn.execute(
                f"SELECT {', '.join(store.id_columns(source))} FROM {store.table(source)}"
            )
        ]
        assert len(ids) == 2
        assert all(store.doc_grants(conn, source, *key) for key in ids)

    replacement = tmp_path / "admin-only.jsonl"
    replacement.write_text(json.dumps({**record, "readers": []}))
    load(replacement, settings, reset=False)

    with client_for(settings) as client:
        response = read_as_ava(client)
        if source == "msteams":
            assert response.status_code == 404, response.text
            assert response.json()["error"]["code"] == "NotFound"
        else:
            assert response.json()["ok"] is False, response.text
            assert response.json()["error"] == "channel_not_found"
    with closing(store.connect_ro(settings.db_path)) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {store.table(source)}").fetchone()[0] == 2
        assert all(store.doc_grants(conn, source, *key) == [] for key in ids)


@pytest.mark.parametrize("edited", ["root", "root-with-reply", "reply"])
def test_chain_order_includes_root_and_reply_edits(tmp_path, edited):
    active = complete("msteams", doc_id="active", channel="Platform", content="Active", created=100)
    if edited != "root":
        active["replies"] = [{"content": "Reply", "author_email": "ava@acme.com", "created": 150}]
    target = active["replies"][0] if edited == "reply" else active
    target["lastEditedDateTime"] = 300
    quiet = complete("msteams", doc_id="quiet", channel="Platform", content="Quiet", created=200)
    with corpus_client(tmp_path, [active, quiet]) as (client, settings):
        headers = {"Authorization": f"Bearer {settings.admin_token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=headers).json()["value"][0]["id"]
        cid = synth.msteams_channel_id("Platform")
        response = client.get(f"{V1}/teams/{team}/channels/{cid}/messages", headers=headers)
        assert response.status_code == 200, response.text
        assert [m["body"]["content"] for m in response.json()["value"]] == ["Active", "Quiet"]


@pytest.mark.parametrize("new_activity", ["root", "reply"])
def test_chain_order_preserves_same_second_creation_precision(tmp_path, new_activity):
    """Use served timestamps, not a timing guarantee about real Graph's opaque ids."""
    # The root case crosses an id digit boundary so a false tie cannot accidentally pass.
    edited_created, activity_second = (9, 10) if new_activity == "root" else (200, 300)
    edited = complete(
        "msteams",
        doc_id="edited-root",
        channel="Platform",
        content="Edited root",
        created=edited_created,
        lastEditedDateTime=activity_second,
    )
    newer = complete(
        "msteams",
        doc_id="newer-activity",
        channel="Platform",
        content="Newer activity",
        created=activity_second if new_activity == "root" else 100,
    )
    if new_activity == "reply":
        newer["replies"] = [{"content": "New reply", "created": activity_second}]
    with corpus_client(tmp_path, [edited, newer]) as (client, settings):
        headers = {"Authorization": f"Bearer {settings.admin_token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=headers).json()["value"][0]["id"]
        cid = synth.msteams_channel_id("Platform")
        base = f"{V1}/teams/{team}/channels/{cid}/messages"
        response = client.get(base, headers=headers, params={"$expand": "replies"})
        assert response.status_code == 200, response.text
        roots = response.json()["value"]
        by_content = {message["body"]["content"]: message for message in roots}
        edited_message = by_content["Edited root"]
        activity = by_content["Newer activity"]
        if new_activity == "reply":
            activity = activity["replies"][0]
        edited_time = edited_message["lastModifiedDateTime"]
        created_time = activity["createdDateTime"]
        assert edited_time.endswith(".000Z")
        assert created_time.split(".")[0] == edited_time.split(".")[0]
        assert created_time > edited_time  # Same second, but genuinely later served activity.
        assert activity["lastModifiedDateTime"] == created_time
        assert [message["body"]["content"] for message in roots] == [
            "Newer activity",
            "Edited root",
        ]
        first = client.get(base, headers=headers, params={"$top": 1})
        assert first.status_code == 200, first.text
        assert first.json()["value"][0]["id"] == by_content["Newer activity"]["id"]
        second = client.get(first.json()["@odata.nextLink"], headers=headers)
        assert second.status_code == 200, second.text
        assert [message["id"] for message in second.json()["value"]] == [edited_message["id"]]
        assert "@odata.nextLink" not in second.json()


def test_chain_order_breaks_equal_activity_ties_by_root_id(tmp_path):
    records = [
        complete(
            "msteams",
            doc_id="reply-edited",
            channel="Platform",
            content="Reply edited",
            created=100,
            replies=[{"content": "Reply", "created": 150, "lastEditedDateTime": 300}],
        ),
        complete(
            "msteams",
            doc_id="root-edited",
            channel="Platform",
            content="Root edited",
            created=200,
            lastEditedDateTime=300,
        ),
    ]
    with corpus_client(tmp_path, records) as (client, settings):
        headers = {"Authorization": f"Bearer {settings.admin_token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=headers).json()["value"][0]["id"]
        cid = synth.msteams_channel_id("Platform")
        response = client.get(
            f"{V1}/teams/{team}/channels/{cid}/messages",
            headers=headers,
            params={"$expand": "replies"},
        )
        assert response.status_code == 200, response.text
        roots = response.json()["value"]
        by_content = {message["body"]["content"]: message for message in roots}
        assert (
            by_content["Root edited"]["lastModifiedDateTime"]
            == (by_content["Reply edited"]["replies"][0]["lastModifiedDateTime"])
            == "1970-01-01T00:05:00.000Z"
        )
        assert [message["body"]["content"] for message in roots] == [
            "Root edited",
            "Reply edited",
        ]
        assert [message["id"] for message in roots] == sorted(
            (message["id"] for message in roots), reverse=True
        )


@pytest.mark.parametrize(
    "token,expected",
    [
        (encode_cursor(2**64), "reject"),
        (encode_cursor(2**63), "reject"),
        (encode_cursor(2**63 - 1), "empty"),
        (encode_cursor(-1), "first"),
        (encode_cursor(-(2**64)), "first"),
        (encode_cursor(0), "first"),
        ("not-a-cursor", "first"),
        ("", "first"),
    ],
)
def test_skiptoken_is_bounded_without_changing_fallbacks(tmp_path, token, expected):
    record = complete("msteams", channel="Platform", replies=[{"content": "Reply"}])
    with corpus_client(tmp_path, [record]) as (client, settings):
        headers = {"Authorization": f"Bearer {settings.admin_token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=headers).json()["value"][0]["id"]
        channels = f"{V1}/teams/{team}/channels"
        channel = f"{channels}/{synth.msteams_channel_id('Platform')}"
        messages = f"{channel}/messages"
        root = client.get(messages, headers=headers).json()["value"][0]["id"]
        for path in (
            messages,
            f"{messages}/{root}/replies",
            f"{channel}/members",
            channels,
            f"{V1}/users",
        ):
            response = client.get(path, headers=headers, params={"$skiptoken": token})
            if expected == "reject":
                assert response.status_code == 400, response.text
                assert response.json()["error"]["code"] == "badRequest"
                assert "innerError" in response.json()["error"]
                assert "detail" not in response.json()
            else:
                assert response.status_code == 200, response.text
                want = (
                    [] if expected == "empty" else client.get(path, headers=headers).json()["value"]
                )
                assert response.json()["value"] == want
                assert "@odata.nextLink" not in response.json()


def test_message_id_probe_uses_all_1000_slots_and_reimports_stably(tmp_path):
    records = [
        complete("msteams", doc_id=f"d{i}", channel="busy", content=f"Message {i}", created=100)
        for i in range(1000)
    ]
    settings = build_corpus(tmp_path / "first", records)

    def assigned(settings):
        with closing(store.connect_ro(settings.db_path)) as conn:
            return dict(conn.execute("SELECT content, id FROM msteams_messages"))

    first = assigned(settings)
    assert len(first) == 1000
    assert {int(mid) for mid in first.values()} == set(range(100000, 101000))
    fresh = build_corpus(tmp_path / "fresh", records)
    assert assigned(fresh) == first
    # Reimport both an initial candidate and a heavily probed one into the full space.
    reimport = settings.data_dir / "reimport.jsonl"
    reimport.write_text("\n".join(json.dumps(r) for r in (records[0], records[-1])))
    load(reimport, settings, reset=False)
    assert assigned(settings) == first

    overflow = settings.data_dir / "overflow.jsonl"
    overflow.write_text(
        json.dumps(
            complete(
                "msteams", doc_id="overflow", channel="busy", content="One too many", created=100
            )
        )
    )
    with pytest.raises(SystemExit, match="1000 sub-second values"):
        load(overflow, settings, reset=False)
    assert assigned(settings) == first


@pytest.mark.parametrize("part", ["root", "reply"])
@pytest.mark.parametrize("edited", ["not-a-date", "", 99, 100])
def test_invalid_or_precreation_edit_is_refused(tmp_path, part, edited):
    record = complete("msteams", doc_id="edited", created=100)
    target = record
    if part == "reply":
        record["created"] = 50
        record["replies"] = [{"content": "Reply", "author_email": "ava@acme.com", "created": 100}]
        target = record["replies"][0]
    target["lastEditedDateTime"] = edited
    with pytest.raises(SystemExit, match="lastEditedDateTime"):
        build_corpus(tmp_path, [record])


def test_expand_205_replies_follows_continuation_without_loss(tmp_path):
    """Pin the emulator's count convention; do not imply live-tenant count verification."""
    record = complete(
        "msteams",
        channel="Platform",
        created=100,
        replies=[{"content": f"Reply {i}", "created": 101 + i} for i in range(205)],
    )
    with corpus_client(tmp_path, [record]) as (client, settings):
        headers = {"Authorization": f"Bearer {settings.admin_token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=headers).json()["value"][0]["id"]
        cid = synth.msteams_channel_id("Platform")
        base = f"{V1}/teams/{team}/channels/{cid}/messages"
        response = client.get(base, headers=headers, params={"$expand": "replies"})
        assert response.status_code == 200, response.text
        root = response.json()["value"][0]
        assert root["replies@odata.count"] == len(root["replies"]) == 200
        link = root["replies@odata.nextLink"]
        assert link.startswith(f"http://testserver{base}/{root['id']}/replies?")
        continued = client.get(link, headers=headers)
        assert continued.status_code == 200, continued.text
        page = continued.json()
        assert page["@odata.count"] == 205
        assert len(page["value"]) == 5
        assert "@odata.nextLink" not in page
        replies = root["replies"] + page["value"]
        assert len({reply["id"] for reply in replies}) == 205
        assert {reply["replyToId"] for reply in replies} == {root["id"]}
        assert [reply["body"]["content"] for reply in replies] == [
            f"Reply {i}" for i in reversed(range(205))
        ]
