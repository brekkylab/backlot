"""The mutation overlay: a writable in-memory database attached to a read-only corpus."""

from __future__ import annotations

import json
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
    # The whole row is compared, DEFAULT (index 4) included: a mirror that keeps
    # name/type/notnull/pk but drops `thread_seq`'s `DEFAULT 0` fails every insert that omits
    # the column, and is not a mirror.
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
    (sheet,) = _drive_files(conn, "Q1 Revenue Model")
    store.replace_parts(
        conn,
        "google_drive",
        (sheet["id"],),
        {
            "gdrive_sheets": [
                {"sheet_index": 0, "sheet_id": 0, "title": "Sheet1", "grid": '[["w"]]'}
            ]
        },
        {"content": "written", "updated_ts": 4000000000},
    )
    conn.close()
    assert hashlib.sha256(sample_settings.db_path.read_bytes()).hexdigest() == digest


def _drive_files(conn, *titles):
    return [
        conn.execute("SELECT * FROM main.gdrive_files WHERE title = ?", (t,)).fetchone()
        for t in titles
    ]


def _write_sheet(conn, file_id, grid, content, updated_ts):
    store.replace_parts(
        conn,
        "google_drive",
        (file_id,),
        {
            "gdrive_sheets": [
                {"sheet_index": 0, "sheet_id": 0, "title": "Sheet1", "grid": json.dumps(grid)}
            ]
        },
        {"content": content, "updated_ts": updated_ts},
    )


def test_written_sheets_are_read_for_their_file_alone_until_reset(sample_settings):
    # A prose spreadsheet has no stored sheets; once written it has the overlay's, and the file
    # beside it in the same folder still has none.
    conn = store.connect_ro(sample_settings.db_path)
    name = overlay.name_for(object())
    overlay.attach(conn, name)
    model, ledger = _drive_files(conn, "Q1 Revenue Model", "Ledger With Gaps")
    _write_sheet(
        conn, model["id"], [["month", "revenue", "note"]], "month,revenue,note", 4000000000
    )
    got = [
        (s["sheet_id"], s["title"], s["grid"]) for s in store.gdrive_sheets_for(conn, model["id"])
    ]
    assert got == [(0, "Sheet1", '[["month", "revenue", "note"]]')]
    assert store.gdrive_sheets_for(conn, ledger["id"]) == []
    overlay.reset(conn, name)
    assert store.gdrive_sheets_for(conn, model["id"]) == []
    conn.close()


def test_a_drive_patch_reaches_every_drive_reader(sample_settings):
    # `content` and `updated_ts` are read by files.get, the folder listing's newest-first order,
    # the storage quota and search. The ledger is the oldest file in its folder, so leading the
    # listing afterwards also shows the patched `updated_ts` comparing as an integer. The written
    # text is past the floor a native file's size carries (`store.DRIVE_NATIVE_MIN_BYTES`), so the
    # quota moves with it.
    conn = store.connect_ro(sample_settings.db_path)
    overlay.attach(conn, overlay.name_for(object()))
    (ledger,) = _drive_files(conn, "Ledger With Gaps")
    assert store.list_drive_folder(conn, "finance")[0]["id"] != ledger["id"]
    used, _ = store.drive_usage_bytes(conn)
    text = "zebra " * 200
    assert len(text) > store.DRIVE_NATIVE_MIN_BYTES
    _write_sheet(conn, ledger["id"], [[text]], text, 4000000000)
    row = store.gdrive_by_id(conn, ledger["id"])
    assert (row["content"], row["updated_ts"]) == (text, 4000000000)
    assert store.list_drive_folder(conn, "finance")[0]["id"] == ledger["id"]
    before = store.drive_file_bytes(ledger["subtype"], ledger["content"])
    assert store.drive_usage_bytes(conn)[0] == used - before + len(text)
    assert [h["id"] for h in store.search_documents(conn, "zebra", "google_drive")] == [
        ledger["id"]
    ]
    assert ledger["id"] not in [
        h["id"] for h in store.search_documents(conn, "header", "google_drive")
    ]
    conn.close()


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

    data = yaml.safe_load(sample_settings.tokens_path.read_text(encoding="utf-8"))
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


def test_meta_overlay_names_every_table_but_the_index(oclient):
    from backlot import overlay, store

    j = oclient.get("/_meta/overlay").json()
    expected = set()
    for src in store.WRITABLE:
        expected |= {n for n in overlay.table_names(src).values() if not n.endswith("_fts_ov")}
        expected |= set(store.SATELLITES.get(src, {}))
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


def test_meta_overlay_reset_needs_the_admin_token(oclient, sample_settings):
    # The one `/_meta` route that DESTROYS something. An evaluation is graded from
    # `/_meta/overlay`, so a caller who could reach this without a credential could erase the
    # record of what it had just done — which is the one thing the oracle exists to hold.
    h = {"Authorization": f"Bearer {_tokens(sample_settings)['ava@acme.com']}"}
    assert oclient.post("/_meta/overlay/reset").status_code == 403
    assert oclient.post("/_meta/overlay/reset", headers=h).status_code == 403
    admin = {"Authorization": f"Bearer {sample_settings.admin_token}"}
    assert oclient.post("/_meta/overlay/reset", headers=admin).json() == {"ok": True}


def test_meta_overlay_reset_empties_it(oclient, sample_settings):
    from backlot import synth

    cid = synth.slack_channel_id("incidents")
    h = {"Authorization": f"Bearer {_tokens(sample_settings)['ava@acme.com']}"}
    oclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "gone soon"}
    )
    admin = {"Authorization": f"Bearer {sample_settings.admin_token}"}
    assert oclient.post("/_meta/overlay/reset", headers=admin).json() == {"ok": True}
    assert oclient.get("/_meta/overlay").json()["slack_messages"] == []
    hist = oclient.post(
        "/slack/api/conversations.history", headers=h, data={"channel": cid, "limit": 200}
    ).json()["messages"]
    assert "gone soon" not in [m["text"] for m in hist]


def test_reset_restores_a_deleted_corpus_message(oclient, sample_settings, ro_conn):
    # A tombstone is the overlay's, not the corpus's, so throwing the overlay away has to bring
    # the message back. That is the property that makes one eval run repeatable after another.
    from backlot import synth

    rows = {
        shape: ro_conn.execute(
            f"SELECT author_email, channel, ts FROM slack_messages WHERE {shape} LIMIT 1"
        ).fetchone()
        for shape in ("thread_ts IS NULL", "thread_ts = ts")
    }
    tokens = _tokens(sample_settings)

    def history(row):
        return {
            m["ts"]: m
            for m in oclient.post(
                "/slack/api/conversations.history",
                headers={"Authorization": f"Bearer {tokens[row['author_email']]}"},
                data={"channel": synth.slack_channel_id(row["channel"]), "limit": 200},
            ).json()["messages"]
        }

    alone, root = rows["thread_ts IS NULL"], rows["thread_ts = ts"]
    original = history(root)[root["ts"]]["text"]
    for row in (alone, root):
        oclient.post(
            "/slack/api/chat.delete",
            headers={"Authorization": f"Bearer {tokens[row['author_email']]}"},
            data={"channel": synth.slack_channel_id(row["channel"]), "ts": row["ts"]},
        )
    # A root is deleted by a PATCH into a tombstone rather than by a tombstone row, so the reset
    # has to undo both kinds of write to bring the corpus back.
    assert alone["ts"] not in history(alone)
    assert history(root)[root["ts"]]["subtype"] == "tombstone"
    admin = {"Authorization": f"Bearer {sample_settings.admin_token}"}
    oclient.post("/_meta/overlay/reset", headers=admin)
    assert alone["ts"] in history(alone)
    back = history(root)[root["ts"]]
    assert back["text"] == original and "subtype" not in back


def test_a_write_does_not_move_the_member_count(oclient, sample_settings):
    # `num_members` is answered from a warm cache in preference to a query, and nothing
    # invalidates it: `slack._member_count` records why no write moves what it holds. This is
    # the assertion that would catch a reader starting to count written rows.
    from backlot import synth

    tokens = _tokens(sample_settings)
    admin = {"Authorization": f"Bearer {sample_settings.admin_token}"}
    cid = synth.slack_channel_id("eng-announcements")
    # Published, so this is about the cache rather than about the window before it exists, which
    # is its own test above.
    oclient.app.state.warm_thread.join(10)

    def num_members():
        page = oclient.post("/slack/api/conversations.list", headers=admin, data={"limit": 200})
        return [c["num_members"] for c in page.json()["channels"] if c["id"] == cid][0]

    before = num_members()
    h = {"Authorization": f"Bearer {tokens['hana@acme.com']}"}
    oclient.post("/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "new"})
    assert num_members() == before
    # And a delete does not either, which is the other half of the reason nothing invalidates.
    ts = oclient.post(
        "/slack/api/chat.postMessage", headers=h, data={"channel": cid, "text": "and gone"}
    ).json()["ts"]
    oclient.post("/slack/api/chat.delete", headers=h, data={"channel": cid, "ts": ts})
    assert num_members() == before


def test_the_warm_up_answers_the_same_count_before_and_after_it_publishes(
    sample_settings, monkeypatch
):
    """A write while the warm-up is still building must not change what `conversations.list`
    answers, before or after the map lands.

    Held open rather than raced: `slack_channel_member_counts` is swapped for one that blocks, so
    the write happens with the cache still unpublished every run. The count is the corpus's
    either way — `slack._member_count` records why no write moves it — and this is what would
    catch a reader that started taking written rows into the number without the cache following.
    """
    import threading

    from backlot import synth

    release, entered = threading.Event(), threading.Event()
    real = store.slack_channel_member_counts

    def slow(conn):
        entered.set()
        release.wait(10)
        return real(conn)

    monkeypatch.setattr(store, "slack_channel_member_counts", slow)

    from tests._helpers import client_for

    with client_for(sample_settings, reload=True) as client:
        app = client.app
        assert entered.wait(10), "the warm-up never reached the member counts"
        assert app.state.channel_members is None

        cid = synth.slack_channel_id("eng-announcements")
        admin = {"Authorization": f"Bearer {sample_settings.admin_token}"}

        def num_members():
            page = client.post("/slack/api/conversations.list", headers=admin, data={"limit": 200})
            return [c["num_members"] for c in page.json()["channels"] if c["id"] == cid][0]

        before = num_members()
        client.post(
            "/slack/api/chat.postMessage",
            headers=admin,
            data={"channel": cid, "text": "while the warm-up runs"},
        )
        assert num_members() == before

        release.set()
        app.state.warm_thread.join(10)
        assert num_members() == before
