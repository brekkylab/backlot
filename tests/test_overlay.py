"""The mutation overlay: a writable in-memory database attached to a read-only corpus."""

from __future__ import annotations

import sqlite3

import pytest

from backlot import overlay, store


def test_overlay_is_writable_while_the_corpus_is_not(sample_settings):
    # SQLite, not convention, is what keeps the corpus immutable: mode=ro applies to `main`
    # alone, so an ATTACHed in-memory database on the same connection takes writes.
    conn = store.connect_ro(sample_settings.db_path)
    overlay.attach(conn, "test_ov_1")
    conn.execute(
        "INSERT INTO ov.slack_messages (channel, ts, author_email, content, created_ts) "
        "VALUES ('incidents', '9.000001', 'ava@acme.com', 'hello', 9)"
    )
    assert conn.execute("SELECT content FROM ov.slack_messages").fetchone()[0] == "hello"
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute(
            "INSERT INTO main.slack_messages (channel, ts, author_email, content, created_ts) "
            "VALUES ('incidents', '9.000002', 'ava@acme.com', 'nope', 9)"
        )
    conn.close()


def test_the_document_mirror_matches_the_corpus_table_exactly(sample_settings):
    # Generated from PRAGMA table_info, not restated here: a column a later import adds to the
    # corpus table appears in the overlay without anyone remembering to add it. Restating the
    # twelve columns by hand is the drift this asserts against.
    #
    # The whole row is compared, DEFAULT (index 4) included. Comparing only name/type/notnull/pk
    # passed while the mirror silently dropped `thread_seq`'s `DEFAULT 0`, which made every insert
    # that omitted the column fail — a mirror missing defaults is not a mirror.
    conn = store.connect_ro(sample_settings.db_path)
    overlay.attach(conn, "test_ov_mirror")
    for src in store.WRITABLE:
        tbl = store.table(src)
        corpus = [tuple(r)[1:] for r in conn.execute(f"PRAGMA main.table_info({tbl})")]
        mirror = [tuple(r)[1:] for r in conn.execute(f"PRAGMA ov.table_info({tbl})")]
        assert mirror == corpus, f"{src}: overlay mirror diverged from the corpus table"
    conn.close()


def test_only_writable_sources_get_overlay_tables(sample_settings):
    # A source not in WRITABLE must be untouched, which is what lets this ship without
    # re-validating ten other vendors.
    conn = store.connect_ro(sample_settings.db_path)
    overlay.attach(conn, "test_ov_writable")
    present = {r[0] for r in conn.execute("SELECT name FROM ov.sqlite_master WHERE type='table'")}
    for src in store.SOURCE_TABLE:
        tbl = store.table(src)
        assert (tbl in present) is (src in store.WRITABLE), f"{src}: wrong overlay presence"
    conn.close()


def test_every_writable_source_gets_all_five_tables(sample_settings):
    conn = store.connect_ro(sample_settings.db_path)
    overlay.attach(conn, "test_ov_five")
    for src in store.WRITABLE:
        names = overlay.table_names(src)
        assert set(names) == {"doc", "patch", "tombstone", "acl", "fts"}
        for kind, name in names.items():
            got = conn.execute("SELECT 1 FROM ov.sqlite_master WHERE name = ?", (name,)).fetchone()
            assert got is not None, f"{src}: overlay {kind} table {name} was not created"
    conn.close()


def test_the_patch_and_tombstone_tables_are_keyed_on_the_source_id_columns(sample_settings):
    conn = store.connect_ro(sample_settings.db_path)
    overlay.attach(conn, "test_ov_keys")
    for src in store.WRITABLE:
        names = overlay.table_names(src)
        key = list(store.id_columns(src))
        for kind in ("patch", "tombstone"):
            cols = [r[1] for r in conn.execute(f"PRAGMA ov.table_info({names[kind]})")]
            assert cols[: len(key)] == key, f"{src}.{kind} is not keyed on {key}"
    conn.close()


def test_a_sources_extra_tables_are_created(sample_settings):
    # Slack membership is not one of the five generated tables — it is a concept only Slack has,
    # and EXTRA_DDL is where a vendor's own overlay table goes rather than the generator.
    conn = store.connect_ro(sample_settings.db_path)
    overlay.attach(conn, "test_ov_extra")
    cols = [r[1] for r in conn.execute("PRAGMA ov.table_info(slack_membership)")]
    assert cols == ["channel", "email", "state"]
    conn.close()


def test_two_overlays_do_not_share_rows(sample_settings):
    # Two servers in one process (which is every pytest run) must not see each other's writes.
    # A shared-cache URI is keyed on its NAME, so the name has to be per-app, not a constant.
    a = store.connect_ro(sample_settings.db_path)
    b = store.connect_ro(sample_settings.db_path)
    overlay.attach(a, "test_ov_a")
    overlay.attach(b, "test_ov_b")
    a.execute("INSERT INTO ov.slack_tombstone VALUES ('incidents', '1.000001')")
    assert a.execute("SELECT COUNT(*) FROM ov.slack_tombstone").fetchone()[0] == 1
    assert b.execute("SELECT COUNT(*) FROM ov.slack_tombstone").fetchone()[0] == 0
    a.close()
    b.close()


def test_name_for_never_repeats(sample_settings):
    # `id(app)` would: CPython reuses the address of a garbage-collected object, so a later app
    # could land on a freed one's address and attach to its overlay. A counter cannot.
    class _App:
        pass

    names = {overlay.name_for(_App()) for _ in range(1000)}
    assert len(names) == 1000


def test_reset_empties_the_overlay_and_keeps_it_usable(sample_settings):
    conn = store.connect_ro(sample_settings.db_path)
    overlay.attach(conn, "test_ov_reset")
    conn.execute("INSERT INTO ov.slack_tombstone VALUES ('incidents', '1.000001')")
    overlay.reset(conn, "test_ov_reset")
    assert overlay.is_attached(conn)
    assert conn.execute("SELECT COUNT(*) FROM ov.slack_tombstone").fetchone()[0] == 0
    conn.close()


def test_is_attached_is_false_before_attach(sample_settings):
    conn = store.connect_ro(sample_settings.db_path)
    assert overlay.is_attached(conn) is False
    conn.close()


def test_a_run_of_writes_leaves_the_corpus_file_identical(sample_settings):
    import hashlib

    digest = hashlib.sha256(sample_settings.db_path.read_bytes()).hexdigest()
    conn = store.connect_ro(sample_settings.db_path)
    overlay.attach(conn, overlay.name_for(object()))
    ts = store.slack_next_ts(conn, "incidents", 4000000000)
    store.insert_document(
        conn,
        "slack",
        {
            "channel": "incidents",
            "ts": ts,
            "author_email": "ava@acme.com",
            "content": "written",
            "created_ts": 4000000000,
        },
    )
    store.patch_document(conn, "slack", ("incidents", ts), "content", "edited")
    store.tombstone_document(conn, "slack", ("incidents", ts))
    conn.close()
    assert hashlib.sha256(sample_settings.db_path.read_bytes()).hexdigest() == digest


# --- the overlay's own control surface --------------------------------------------------------


@pytest.fixture
def oclient(sample_settings):
    """A server of its own per test, so one test's writes are not another's starting state.

    `reload=True` for the reason `test_slack.py`'s write fixture uses it: `client_for` without it
    starts a second lifespan on the module-level `app`, closing the connection any other live
    client is holding.
    """
    from tests._helpers import client_for

    with client_for(sample_settings, reload=True) as c:
        yield c


def _tokens(sample_settings):
    import yaml

    data = yaml.safe_load(sample_settings.tokens_path.read_text())
    return {u["email"]: u["token"] for u in data["users"]}


def test_meta_overlay_reports_what_was_written(oclient, sample_settings):
    from backlot import synth

    cid = synth.slack_channel_id("incidents")
    h = {"Authorization": f"Bearer {_tokens(sample_settings)['ava@acme.com']}"}
    ts = oclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "audited"}
    ).json()["ts"]
    j = oclient.get("/_meta/overlay").json()
    assert any(m["ts"] == ts and m["content"] == "audited" for m in j["slack_messages"])
    # the grants the write copied are reported too -- an ACL oracle reads what an agent could
    # make visible, not only what it said
    assert any(g["ts"] == ts for g in j["slack_acl"])


def test_meta_overlay_is_empty_on_a_fresh_server(oclient):
    j = oclient.get("/_meta/overlay").json()
    assert j["slack_messages"] == []
    assert j["slack_patch"] == []
    assert j["slack_tombstone"] == []
    assert j["slack_membership"] == []


def test_meta_overlay_names_every_table_but_the_index(oclient):
    from backlot import overlay, store

    j = oclient.get("/_meta/overlay").json()
    expected = set()
    for src in store.WRITABLE:
        expected |= {n for n in overlay.table_names(src).values() if not n.endswith("_fts_ov")}
        expected |= set(overlay.EXTRA_DDL.get(src, {}))
    assert set(j) == expected


def test_meta_overlay_reports_a_patch_and_a_tombstone(oclient, sample_settings):
    from backlot import synth

    cid = synth.slack_channel_id("incidents")
    h = {"Authorization": f"Bearer {_tokens(sample_settings)['ava@acme.com']}"}
    ts = oclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "first"}
    ).json()["ts"]
    oclient.post(
        "/slack/api/chat.update", headers=h, data={"channel": cid, "ts": ts, "text": "second"}
    )
    oclient.post("/slack/api/chat.delete", headers=h, data={"channel": cid, "ts": ts})
    j = oclient.get("/_meta/overlay").json()
    assert {p["field"] for p in j["slack_patch"] if p["ts"] == ts} == {"content", "edited"}
    assert any(d["ts"] == ts for d in j["slack_tombstone"])


def test_meta_overlay_reset_empties_it(oclient, sample_settings):
    from backlot import synth

    cid = synth.slack_channel_id("incidents")
    h = {"Authorization": f"Bearer {_tokens(sample_settings)['ava@acme.com']}"}
    oclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "gone soon"}
    )
    assert oclient.post("/_meta/overlay/reset").json() == {"ok": True}
    assert oclient.get("/_meta/overlay").json()["slack_messages"] == []
    hist = oclient.post(
        "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    assert "gone soon" not in [m["text"] for m in hist]


def test_reset_restores_a_deleted_corpus_message(oclient, sample_settings, ro_conn):
    # A tombstone is the overlay's, not the corpus's, so throwing the overlay away has to bring
    # the message back. That is the property that makes one eval run repeatable after another.
    from backlot import synth

    email, channel, ts = ro_conn.execute(
        "SELECT author_email, channel, ts FROM slack_messages WHERE channel = 'incidents' LIMIT 1"
    ).fetchone()
    h = {"Authorization": f"Bearer {_tokens(sample_settings)[email]}"}
    cid = synth.slack_channel_id(channel)
    oclient.post("/slack/api/chat.delete", headers=h, data={"channel": cid, "ts": ts})
    gone = oclient.post(
        "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    assert ts not in [m["ts"] for m in gone]
    oclient.post("/_meta/overlay/reset")
    back = oclient.post(
        "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    assert ts in [m["ts"] for m in back]


def test_a_write_drops_the_stale_member_count(oclient, sample_settings):
    # `num_members` is answered from a warm cache in preference to a query, so a write has to drop
    # the entry or conversations.list keeps reporting the count from before it.
    from backlot import synth

    tokens = _tokens(sample_settings)
    admin = {"Authorization": f"Bearer {sample_settings.admin_token}"}
    cid = synth.slack_channel_id("eng-announcements")

    def num_members():
        page = oclient.post("/slack/api/conversations.list", headers=admin, data={"limit": 200})
        return [c["num_members"] for c in page.json()["channels"] if c["id"] == cid][0]

    before = num_members()
    h = {"Authorization": f"Bearer {tokens['hana@acme.com']}"}
    oclient.post("/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "new"})
    # The poster is written `out`, so the count must NOT move -- but it has to be recomputed to
    # know that, rather than served from a cache that happens to agree.
    assert num_members() == before
    # A member who leaves is the case where the cached number would be wrong.
    from backlot import store

    conn = oclient.app.state.conn
    store.slack_set_membership(conn, "eng-announcements", "ava@acme.com", "out")
    oclient.app.state.channel_members = None
    assert num_members() == before - 1
