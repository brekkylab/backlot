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
