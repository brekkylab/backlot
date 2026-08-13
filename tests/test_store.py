"""Tests for the read-only SQLite store layer (`backlot.store`).

The store is shared by every router, search, and the importers, so it gets its own file rather
than being verified incidentally through a load/route test. Registry wiring is checked across
every source; generic reads run against the shared SAMPLE corpus (the `db` fixture); connection
tuning uses hand-built / SAMPLE DBs.

ACL-filtered reads live in test_acl.py (the ACL is the subject there) and FTS search in
test_search.py (search is its own sub-domain); this file covers the plain store surface.
"""

import sqlite3

import pytest

from backlot import store, synth

ALL_SOURCES = [
    "slack",
    "gmail",
    "google_drive",
    "github",
    "jira",
    "confluence",
    "notion",
    "s3",
    "hubspot",
    "linear",
    "fireflies",
]


# --- registry wiring ------------------------------------------------------------


def test_registry_covers_every_source():
    assert set(store.SOURCE_TABLE) == set(ALL_SOURCES)
    for src in ALL_SOURCES:
        assert store.table(src)  # source -> table resolves
        assert store.grouping_table(src)  # source -> grouping table resolves
        assert store.grouping_col(src)  # source -> grouping column resolves


def test_unknown_source_type_raises():
    with pytest.raises(ValueError):
        store.table("nope")


def test_unknown_source_type_raises_from_acl_table():
    # Matches store.table(): a bare KeyError here would be a worse error for a caller than the
    # message ValueError gives, and _acl_clause relies on this to fail loudly for an unknown
    # source even on an admin (visible_ids=None) call.
    with pytest.raises(ValueError, match="nope"):
        store.acl_table("nope")


def test_grouping_cols_per_source():
    assert store.grouping_col("slack") == "channel"
    assert store.grouping_col("gmail") == "mailbox"
    assert store.grouping_col("google_drive") == "folder"
    assert store.grouping_col("github") == "repo"
    assert store.grouping_col("jira") == "project"
    assert store.grouping_col("confluence") == "space"
    assert store.grouping_col("notion") == "teamspace"
    assert store.grouping_col("s3") == "bucket"
    # HubSpot has no channel/space concept; the API is polymorphic over `{objectType}`, so the
    # object type is the grouping unit (see backlot/store.py GROUPING).
    assert store.grouping_col("hubspot") == "object_type"
    assert store.grouping_col("linear") == "team"
    # Fireflies groups meetings into channels, its own concept and a documented filter.
    assert store.grouping_col("fireflies") == "channel"


def test_comment_tables_only_where_supported():
    # jira/confluence/github/notion expose comments; slack/gmail/drive/s3 do not
    assert store.comment_table("jira") == "jira_comments"
    assert store.comment_table("confluence") == "confluence_comments"
    assert store.comment_table("github") == "github_comments"
    assert store.comment_table("notion") == "notion_comments"
    assert store.comment_table("linear") == "linear_comments"
    # Fireflies' child rows are SENTENCES, not comments; the slot is "the child rows of a doc in
    # this source", so it is reused rather than duplicated (see backlot/store.py COMMENT_TABLE).
    assert store.comment_table("fireflies") == "fireflies_sentences"
    # HubSpot models notes/emails/meetings as their own object types, not as comments on a record.
    for src in ("slack", "gmail", "google_drive", "s3", "hubspot"):
        assert store.comment_table(src) is None


def test_acl_table_registry_covers_every_source(tmp_path):
    """Each source owns its ACL, so the registry must be total over SOURCE_TABLE — a source
    missing here would silently fall back to no scoping at all."""
    assert set(store.ACL_TABLE) == set(store.SOURCE_TABLE)
    assert store.acl_table("github") == "github_acl"
    conn = store.connect_rw(tmp_path / "s.sqlite")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(store.ACL_TABLE.values()) <= names
    # the old shared table let two sources' documents merge their grants (doc_id is unique only
    # within a source) — it must not come back now that every source has its own table.
    assert "doc_acl" not in names
    conn.close()


def test_served_id_registry_covers_every_hashed_source():
    """This registry has to be total over every source that serves a HASHED id -- one resolved by
    reversing a hash back to a doc_id, whether that reversal still lives in `main._build_index`
    (hubspot, linear, github, jira) or already went through a stored column (confluence, gmail,
    notion, each in its own task) -- each gets its own column in its own task (#51), one source at
    a time, so the registry has to be total from the start or a later task's column goes
    unrecorded. `s3`'s id is `bucket/key`, stored already and never hashed; `slack` has no
    hash->doc_id map to replace; and fireflies' only hash (`fireflies_user_id`) reverses an EMAIL,
    not a doc_id -- none of the three belong here."""
    assert set(store.SERVED_ID) == {
        "confluence",
        "gmail",
        "notion",
        "hubspot",
        "linear",
        "github",
        "jira",
    }
    # Confluence's own entry -- column name, seed function, corpus-wide scope. (gmail and notion
    # read theirs too, by now; hubspot/linear/github/jira's are read only by main._build_index,
    # until their own task converts them.)
    assert store.SERVED_ID["confluence"] == ("served_id", synth.confluence_id, None)


def test_acl_clause_rejects_a_wrong_but_valid_table_pairing():
    """`_acl_clause`'s `tbl` defaults to the source's own table, but a caller may still pass one
    explicitly. When it does, a REAL table (one of SOURCE_TABLE's values) that isn't THIS
    source's table must raise rather than silently scoping to the wrong source: passing
    ("slack", "gmail_messages") is wrong in exactly the way that leaves a scoped Gmail listing
    silently reading Slack's ACL grants instead of Gmail's, with no observable failure short of
    the wrong rows coming back."""
    with pytest.raises(AssertionError):
        store._acl_clause("slack", "gmail_messages", {"p1"})
    # An alias (not a real table name) must not trip the check — the Linear relation readers
    # legitimately pass "i"/"a"/"b" as `tbl`.
    store._acl_clause("slack", "i", {"p1"})
    # tbl=None (the default) resolves to the source's own table and must not raise.
    store._acl_clause("slack", visible_ids={"p1"})


# --- generic reads over the SAMPLE corpus ---------------------------------------


def test_get_document(db):
    doc = store.get_document(db, "confluence", "cf-handbook")
    assert doc["title"] == "Engineering Handbook"
    assert store.get_document(db, "confluence", "no-such-doc") is None


def test_list_documents_container_scope(db):
    keys = {r["doc_id"] for r in store.list_documents(db, "jira", container="payments", limit=100)}
    assert {"jira-sev2", "jira-sub1", "jira-private"} <= keys
    assert store.list_documents(db, "jira", container="no-such-project", limit=100) == []


def test_list_documents_author_scope(db):
    rows = store.list_documents(db, "confluence", author_email="ava@acme.com", limit=100)
    assert rows and all(r["author_email"] == "ava@acme.com" for r in rows)


def test_count_documents(db):
    assert store.count_documents(db, "jira", container="payments") >= 3
    assert store.count_documents(db, "confluence") >= 3
    assert store.count_documents(db, "jira", container="no-such-project") == 0


def test_list_documents_state_filter(db):
    # gateway repo: gh-issue-1 is open (state NULL/"open"), gh-pr-1 is closed
    open_ids = {
        r["doc_id"]
        for r in store.list_documents(db, "github", container="gateway", limit=100, state="open")
    }
    closed_ids = {
        r["doc_id"]
        for r in store.list_documents(db, "github", container="gateway", limit=100, state="closed")
    }
    assert "gh-issue-1" in open_ids and "gh-pr-1" not in open_ids
    assert "gh-pr-1" in closed_ids and "gh-issue-1" not in closed_ids
    # state=None (default) applies no filter -> both present
    all_ids = {
        r["doc_id"] for r in store.list_documents(db, "github", container="gateway", limit=100)
    }
    assert {"gh-issue-1", "gh-pr-1"} <= all_ids


def test_count_documents_state_filter(db):
    assert store.count_documents(db, "github", container="gateway", state="open") == 1
    assert store.count_documents(db, "github", container="gateway", state="closed") == 1
    assert store.count_documents(db, "github", container="gateway") >= 2


def test_children(db):
    # jira-sub1 is a subtask of jira-sev2; cf-oncall is a child page of cf-handbook
    assert "jira-sub1" in {r["doc_id"] for r in store.children(db, "jira", "jira-sev2")}
    assert "cf-oncall" in {r["doc_id"] for r in store.children(db, "confluence", "cf-handbook")}


def test_doc_comments(db):
    cmts = store.doc_comments(db, "confluence", "cf-oncall")
    assert len(cmts) == 1 and "rate-limiter" in cmts[0]["body"]
    # a source with no comment table returns [] rather than erroring
    assert store.doc_comments(db, "slack", "whatever") == []


def test_containers(db):
    names = {c["name"] for c in store.list_containers(db, "confluence")}
    assert {"handbook", "people-ops"} <= names
    assert store.get_container(db, "confluence", "handbook")["group_id"] == "engineering"
    assert store.get_container(db, "confluence", "no-such") is None
    # S3 buckets are the grouping unit; both eng-artifacts objects share the engineering group
    assert store.get_container(db, "s3", "eng-artifacts")["group_id"] == "engineering"


def test_users(db):
    emails = set(store.all_user_emails(db))
    assert "ava@acme.com" in emails
    assert store.get_user(db, "ava@acme.com") is not None
    assert store.get_user(db, "nobody@acme.com") is None


def test_jcol_parses_json_columns(db):
    issue = store.get_document(db, "github", "gh-issue-1")
    assert store.jcol(issue, "labels") == ["bug", "gateway"]  # JSON-valued TEXT column
    assert store.jcol(issue, "no_such_col", default=["x"]) == ["x"]


# --- S3: SQL-pushed prefix / keyset pagination / ACL scoping --------------------


def _s3_mini_db(tmp_path):
    """A hand-built DB with two buckets and a handful of objects — enough to exercise
    prefix/keyset/ACL without going through the full BYO importer."""
    conn = store.connect_rw(tmp_path / "s3.sqlite")
    rows = [
        # (doc_id, bucket, key)
        ("d1", "b", "logs/2026/01/a.json"),
        ("d2", "b", "logs/2026/01/b.json"),
        ("d3", "b", "logs/2026/02/a.json"),
        ("d4", "b", "notes/readme.md"),
        ("d5", "other", "logs/2026/01/a.json"),  # same key, different bucket
    ]
    for doc_id, bucket, key in rows:
        conn.execute(
            "INSERT INTO s3_objects(doc_id, bucket, author_email, title, content, key, "
            "created_ts) VALUES (?,?,?,?,?,?,1)",
            (doc_id, bucket, "a@x.com", key, "body", key),
        )
    # d2 is ACL-restricted to group 'eng'; everything else is unrestricted (no s3_acl row ->
    # _acl_clause's EXISTS check only bites rows it has an entry for).
    conn.execute("INSERT INTO s3_acl VALUES ('d2','group','eng')")
    conn.commit()
    return conn


def test_list_s3_objects_prefix_and_order(tmp_path):
    conn = _s3_mini_db(tmp_path)
    rows = store.list_s3_objects(conn, "b", prefix="logs/2026/01/")
    assert [r["key"] for r in rows] == ["logs/2026/01/a.json", "logs/2026/01/b.json"]
    # a bucket-only listing stays sorted and scoped to that bucket (d5 in "other" excluded)
    rows = store.list_s3_objects(conn, "b")
    assert [r["key"] for r in rows] == [
        "logs/2026/01/a.json",
        "logs/2026/01/b.json",
        "logs/2026/02/a.json",
        "notes/readme.md",
    ]


def test_list_s3_objects_prefix_no_like_wildcard_semantics(tmp_path):
    conn = _s3_mini_db(tmp_path)
    # the prefix filter is a byte range, not a LIKE pattern: '_'/'%' are ordinary bytes, not
    # wildcards, so a prefix containing them just fails to match (no escaping needed or done)
    assert store.list_s3_objects(conn, "b", prefix="logs_2026") == []
    assert store.list_s3_objects(conn, "b", prefix="logs%") == []


def test_list_s3_objects_prefix_uses_index_range_not_like(tmp_path):
    """Fix 1 (perf): the prefix filter must compile to an explicit key range on idx_s3_key —
    NOT a LIKE scan — since SQLite only range-optimizes a LIKE under case_sensitive_like=ON,
    which this repo must not set (list_drive_by_name needs the default case-insensitive LIKE)."""
    conn = _s3_mini_db(tmp_path)
    prefix = "logs/2026/01/"
    succ = store.key_successor(prefix)
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM s3_objects WHERE bucket = ? AND key >= ? AND key < ? "
        "ORDER BY key ASC",
        ("b", prefix, succ),
    ).fetchall()
    detail = " | ".join(row[-1] for row in plan)
    assert "idx_s3_key" in detail
    assert "LIKE" not in detail.upper()
    flat = detail.replace(" ", "")
    assert "key>" in flat and "key<" in flat


def test_list_s3_objects_prefix_case_sensitive(tmp_path):
    """Fix 2 (correctness): a direct consequence of the byte-range prefix filter (BINARY
    collation) — prefix matching is case-SENSITIVE, matching real S3's byte-exact semantics.
    Objects live only under the lowercase "logs/" prefix; an uppercase "LOGS/" prefix query must
    not match them (a case-insensitive LIKE would wrongly match)."""
    conn = store.connect_rw(tmp_path / "s3_case.sqlite")
    for doc_id, key in [("c1", "logs/a.json"), ("c2", "logs/b.json")]:
        conn.execute(
            "INSERT INTO s3_objects(doc_id, bucket, author_email, title, content, key, "
            "created_ts) VALUES (?,?,?,?,?,?,1)",
            (doc_id, "b", "a@x.com", key, "body", key),
        )
    conn.commit()
    assert [r["key"] for r in store.list_s3_objects(conn, "b", prefix="LOGS/")] == []
    assert [r["key"] for r in store.list_s3_objects(conn, "b", prefix="logs/")] == [
        "logs/a.json",
        "logs/b.json",
    ]


def test_list_s3_objects_keyset_pagination(tmp_path):
    conn = _s3_mini_db(tmp_path)
    page1 = store.list_s3_objects(conn, "b", limit=2)
    assert [r["key"] for r in page1] == ["logs/2026/01/a.json", "logs/2026/01/b.json"]
    page2 = store.list_s3_objects(conn, "b", start_after=page1[-1]["key"], limit=2)
    assert [r["key"] for r in page2] == ["logs/2026/02/a.json", "notes/readme.md"]
    # keyset pagination never re-returns the boundary key, and pages don't overlap
    assert not {r["key"] for r in page1} & {r["key"] for r in page2}


def test_list_s3_objects_acl_scoped(tmp_path):
    conn = _s3_mini_db(tmp_path)
    all_keys = {r["key"] for r in store.list_s3_objects(conn, "b", prefix="logs/2026/01/")}
    assert all_keys == {"logs/2026/01/a.json", "logs/2026/01/b.json"}
    scoped_keys = {
        r["key"]
        for r in store.list_s3_objects(conn, "b", prefix="logs/2026/01/", visible_ids={"eng"})
    }
    assert scoped_keys == {"logs/2026/01/b.json"}  # only d2, granted to group 'eng'
    none_visible = store.list_s3_objects(conn, "b", prefix="logs/2026/01/", visible_ids={"nobody"})
    assert none_visible == []


# --- Drive: storage usage -------------------------------------------------------


def _drive_usage_db(tmp_path):
    """A hand-built Drive corpus whose byte count differs from its character count — the ASCII
    SAMPLE cannot tell the two apart, and that difference is the whole point of the helper."""
    conn = store.connect_rw(tmp_path / "gdrive.sqlite")
    rows = [
        # (doc_id, content, trashed) — "안녕" is 2 characters but 6 UTF-8 bytes
        ("g1", "abc", 0),  # 3 bytes
        ("g2", "안녕", 0),  # 6 bytes
        ("g3", "trash", 1),  # 5 bytes, in the trash
    ]
    for doc_id, content, trashed in rows:
        conn.execute(
            "INSERT INTO gdrive_files(doc_id, folder, author_email, title, content, subtype, "
            "created_ts, trashed) VALUES (?,?,?,?,?,?,1,?)",
            (doc_id, "f", "a@x.com", doc_id, content, "document", trashed),
        )
    # Every doc gets a grant, as the importers write one: a `visible_ids` filter passes only rows
    # that HAVE a matching google_drive_acl row, so a doc with no grant is invisible to any scoped
    # caller.
    for doc_id, pid in [("g1", "acme"), ("g2", "eng"), ("g3", "acme")]:
        conn.execute("INSERT INTO google_drive_acl VALUES (?,?,?)", (doc_id, "group", pid))
    conn.commit()
    return conn


def test_drive_usage_bytes_counts_utf8_bytes_not_characters(tmp_path):
    """``about.get``'s storageQuota has to agree with the ``size`` files.list serves, which is
    ``len(content.encode("utf-8"))``. SQLite's ``length()`` on a TEXT column counts CHARACTERS, so
    a non-ASCII doc would under-report unless the sum casts to BLOB first."""
    conn = _drive_usage_db(tmp_path)
    total, trashed = store.drive_usage_bytes(conn)
    assert total == 3 + 6 + 5  # not 3 + 2 + 5, which is what length(content) would give
    assert trashed == 5


def test_drive_usage_bytes_is_acl_scoped(tmp_path):
    """Usage is per-caller: a token that cannot read a file must not be told its size, or the
    quota number would leak the weight of a corpus the caller has no access to."""
    conn = _drive_usage_db(tmp_path)
    assert store.drive_usage_bytes(conn, visible_ids={"acme", "eng"}) == (3 + 6 + 5, 5)
    assert store.drive_usage_bytes(conn, visible_ids={"acme"}) == (3 + 5, 5)  # g2 invisible
    assert store.drive_usage_bytes(conn, visible_ids={"eng"}) == (6, 0)  # g2 only, untrashed
    assert store.drive_usage_bytes(conn, visible_ids=set()) == (0, 0)  # sees nothing


# --- HubSpot: polymorphic objects + associations --------------------------------


def _hubspot_mini_db(tmp_path):
    """A hand-built CRM: two contacts, one company, one deal, plus associations — enough to
    exercise object-type scoping and association lookup without the BYO importer."""
    conn = store.connect_rw(tmp_path / "hs.sqlite")
    rows = [
        # (doc_id, object_type, title, properties)
        ("c1", "contacts", "Ava Stone", '{"firstname": "Ava", "lastname": "Stone"}'),
        ("c2", "contacts", "Bob Reyes", '{"firstname": "Bob", "lastname": "Reyes"}'),
        ("co1", "companies", "Acme Health", '{"name": "Acme Health"}'),
        ("d1", "deals", "Acme renewal", '{"dealname": "Acme renewal", "amount": "50000"}'),
    ]
    for doc_id, object_type, title, properties in rows:
        conn.execute(
            "INSERT INTO hubspot_objects(doc_id, object_type, author_email, title, content, "
            "properties, created_ts) VALUES (?,?,?,?,?,?,1)",
            (doc_id, object_type, "owner@x.com", title, title, properties),
        )
    # Both contacts belong to the company; the deal is associated with the company too.
    # Real HubSpot associations are bidirectional with a distinct type id per direction, so a row
    # is stored per direction and a lookup is a plain (from_doc_id, to_type) match.
    for from_doc, from_type, to_doc, to_type in [
        ("c1", "contacts", "co1", "companies"),
        ("c2", "contacts", "co1", "companies"),
        ("d1", "deals", "co1", "companies"),
        ("co1", "companies", "c1", "contacts"),
        ("co1", "companies", "c2", "contacts"),
        ("co1", "companies", "d1", "deals"),
    ]:
        conn.execute(
            "INSERT INTO hubspot_associations(from_doc_id, from_type, to_doc_id, to_type, "
            "assoc_category, assoc_type_id, label) VALUES (?,?,?,?,'HUBSPOT_DEFINED',1,NULL)",
            (from_doc, from_type, to_doc, to_type),
        )
    # Every doc carries an ACL grant, as the importers write them: org-wide for most, and c2
    # restricted to group 'sales'.
    for doc_id, ptype, pid in [
        ("c1", "group", "everyone"),
        ("c2", "group", "sales"),
        ("co1", "group", "everyone"),
        ("d1", "group", "everyone"),
    ]:
        conn.execute("INSERT INTO hubspot_acl VALUES (?,?,?)", (doc_id, ptype, pid))
    conn.commit()
    return conn


def test_list_hubspot_objects_scoped_by_object_type(tmp_path):
    conn = _hubspot_mini_db(tmp_path)
    # the object type is the grouping unit, so the generic container scope selects by it
    assert [r["doc_id"] for r in store.list_documents(conn, "hubspot", container="contacts")] == [
        "c1",
        "c2",
    ]
    assert [r["doc_id"] for r in store.list_documents(conn, "hubspot", container="deals")] == ["d1"]


def test_hubspot_associations_from_a_record(tmp_path):
    conn = _hubspot_mini_db(tmp_path)
    rows = store.hubspot_associations(conn, "c1", "companies")
    assert [r["to_doc_id"] for r in rows] == ["co1"]
    assert rows[0]["assoc_category"] == "HUBSPOT_DEFINED"
    assert rows[0]["assoc_type_id"] == 1


def test_hubspot_associations_filtered_to_the_target_type(tmp_path):
    conn = _hubspot_mini_db(tmp_path)
    # c1 has no association to deals, only to companies
    assert store.hubspot_associations(conn, "c1", "deals") == []


def test_hubspot_associations_acl_scoped_on_the_target(tmp_path):
    """An association must not leak a record the caller cannot read — the ACL applies to the
    *target*, since that is the record whose existence the response reveals."""
    conn = _hubspot_mini_db(tmp_path)
    # admin (visible_ids=None) sees both contacts of the company
    assert [r["to_doc_id"] for r in store.hubspot_associations(conn, "co1", "contacts")] == [
        "c1",
        "c2",
    ]
    # a caller with only 'everyone' cannot read c2, so that association is not revealed
    rows = store.hubspot_associations(conn, "co1", "contacts", visible_ids={"everyone"})
    assert [r["to_doc_id"] for r in rows] == ["c1"]
    # a caller in 'sales' reads c2 but not c1
    rows = store.hubspot_associations(conn, "co1", "contacts", visible_ids={"sales"})
    assert [r["to_doc_id"] for r in rows] == ["c2"]


# --- connection tuning ----------------------------------------------------------


def test_connect_rw_busy_timeout(tmp_path):
    c = store.connect_rw(tmp_path / "rw.sqlite", busy_ms=12345)
    try:
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 12345
    finally:
        c.close()


def test_connect_rw_self_heals_missing_path_column(tmp_path):
    """A pre-existing github_items table built before the `path` column existed must not make
    connect_rw's executescript(SCHEMA) blow up on `CREATE INDEX ... ON github_items(repo, path)`
    (IF NOT EXISTS only guards the index name, not the referenced column)."""
    p = tmp_path / "old.sqlite"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE github_items ("
        "doc_id TEXT PRIMARY KEY, repo TEXT NOT NULL, author_email TEXT NOT NULL, "
        "title TEXT NOT NULL, content TEXT NOT NULL, kind TEXT, state TEXT, labels TEXT, "
        "assignees TEXT, merged_at TEXT, head_ref TEXT, base_ref TEXT, reviews TEXT, "
        "reactions TEXT, created_ts INTEGER NOT NULL, updated_ts INTEGER, closed_ts INTEGER, "
        "closed_by TEXT, merged_by TEXT, milestone TEXT, requested_reviewers TEXT, owner_display TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO github_items(doc_id, repo, author_email, title, content, created_ts) "
        "VALUES ('i1', 'svc', 'a@x', 'a bug', '...', 1)"
    )
    conn.commit()
    conn.close()

    reconn = store.connect_rw(p)  # must not raise
    try:
        cols = {r[1] for r in reconn.execute("PRAGMA table_info(github_items)")}
        assert "path" in cols
        # pre-existing row survives the migration
        assert reconn.execute("SELECT doc_id FROM github_items WHERE doc_id = 'i1'").fetchone()
    finally:
        reconn.close()


def test_connect_rw_fresh_db_still_works(tmp_path):
    conn = store.connect_rw(tmp_path / "fresh.sqlite")
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(github_items)")}
        assert {"path", "changed_paths"} <= cols
        ccols = {r[1] for r in conn.execute("PRAGMA table_info(github_comments)")}
        assert {"path", "line", "diff_hunk"} <= ccols
    finally:
        conn.close()


def test_connect_rw_refuses_a_pre_per_source_acl_db(tmp_path):
    """A DB built before this branch has one shared `doc_acl` table. Appending to it would write
    the new source's grants into the empty per-source tables SCHEMA creates while every
    pre-existing grant stays behind in `doc_acl`, which nothing reads any more — every
    pre-existing document would silently become invisible to every scoped token. There is no
    backfill: `doc_acl` cannot say which source a colliding `doc_id`'s grant belonged to, so
    connect_rw must refuse outright rather than quietly leaving the old grants orphaned."""
    p = tmp_path / "old.sqlite"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE doc_acl (doc_id TEXT NOT NULL, principal_type TEXT NOT NULL, "
        "principal_id TEXT NOT NULL, PRIMARY KEY (doc_id, principal_type, principal_id))"
    )
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="re-import"):
        store.connect_rw(p)


def test_github_comments_splits_review_from_conversation(tmp_path):
    """github_comments holds two resources real GitHub keeps apart, discriminated by `path`: a row
    WITH one is a line-anchored review comment (/pulls/{n}/comments), one without is a conversation
    comment (/issues/{n}/comments). Serving either from an unsplit read duplicates it under a
    resource that means something else.

    A github-specific reader because the shared doc_comments SELECT is one column list for six
    tables and cannot carry github-only columns."""
    conn = store.connect_rw(tmp_path / "c.sqlite")
    rows = [
        ("c1", 1, "looks good overall", None, None),
        ("c2", 2, "this branch is dead", "src/a.py", 12),
        ("c3", 3, "and here too", "src/a.py", 40),
    ]
    for cid, seq, body, path, line in rows:
        conn.execute(
            "INSERT INTO github_comments(id,doc_id,seq,author_email,body,created_ts,path,line) "
            "VALUES(?,'p1',?,'a@x',?,1,?,?)",
            (cid, seq, body, path, line),
        )
    conn.commit()
    assert [c["id"] for c in store.github_comments(conn, "p1")] == ["c1", "c2", "c3"]
    assert [c["id"] for c in store.github_comments(conn, "p1", anchored=False)] == ["c1"]
    anchored = store.github_comments(conn, "p1", anchored=True)
    assert [c["id"] for c in anchored] == ["c2", "c3"]
    assert (anchored[0]["path"], anchored[0]["line"]) == ("src/a.py", 12)


def test_confluence_served_ids_are_unique_even_when_the_seed_collides(tmp_path, monkeypatch):
    """`synth.confluence_id` draws from 9,000,000 values, so a large space collides by the birthday
    bound — ~222 in a 60,000-page corpus. The old reverse map was last-writer-wins, so a collision
    made a page unreachable by its own id. Forced here by collapsing the seed.

    Patches `store.SERVED_ID` directly, NOT `synth.confluence_id` (`monkeypatch.setattr(byo.synth,
    "confluence_id", ...)`, the original shape of this test): `store.served_id_seed("confluence")`
    — what `_assign_confluence_id` actually calls — returns the tuple element `SERVED_ID` captured
    when `backlot.store` was first imported, a bound reference to the function object, not a live
    attribute lookup. Patching the module attribute afterward cannot reach it — verified: it left
    every page with a distinct, real hash, so the collision below never actually happened and the
    assertions passed for the wrong reason. `monkeypatch.setitem` replaces the tuple the accessor
    actually reads.
    """
    from backlot.importer import byo
    from tests._helpers import build_corpus

    monkeypatch.setitem(store.SERVED_ID, "confluence", ("served_id", lambda doc_id: 7, None))
    s = build_corpus(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "space": "wiki",
                "doc_id": f"p{i}",
                "title": f"Page {i}",
                "content": "x",
                "author_email": "a@acme.com",
            }
            for i in range(5)
        ],
    )
    monkeypatch.undo()
    conn = store.connect_ro(s.db_path)
    served = [r["served_id"] for r in conn.execute("SELECT served_id FROM confluence_pages")]
    assert len(served) == 5 and len(set(served)) == 5 and all(served)
    # The collapsed seed actually landed: p0 (the first page processed, before anything is taken)
    # is served exactly 7, the forced value -- not some real hash. If this drifts back to a real
    # hash, the patch has gone inert again, silently, the same way it did before.
    assert store.confluence_by_served_id(conn, 7)["doc_id"] == "p0"
    for sid in served:
        assert store.confluence_by_served_id(conn, sid)["served_id"] == sid
    conn.close()

    # A re-import (e.g. --append re-running over the same shard) must not renumber a page a
    # client may already hold a url for -- seed_tracker_ids preloads the ids already assigned so
    # the same collision, replayed, resolves to the same served_ids.
    byo.load(s.data_dir / "_corpus.jsonl", s, reset=False)
    conn = store.connect_ro(s.db_path)
    assert sorted(
        r["served_id"] for r in conn.execute("SELECT served_id FROM confluence_pages")
    ) == sorted(served)
    conn.close()


def test_gmail_served_ids_are_stored_and_resolve(tmp_path, monkeypatch):
    """Gmail's id space is 2**63, so unlike confluence it does not probe: the seed is stored as-is.
    Keeping it a pure hash is what lets `_gmail_ids` derive `threadId` by re-hashing the root's key
    instead of reading the root's row.

    The second half forces the collision gmail's design accepts as the tradeoff for staying
    unprobed: a duplicate `served_id` MUST fail the import loudly (a UNIQUE index violation), not
    resolve through the shared write path's upsert, which was `INSERT OR REPLACE` and resolved a
    conflict on ANY unique index by silently DELETING the row already holding that value -- a
    message would vanish with no error. `monkeypatch.setitem` on `store.SERVED_ID`, not
    `monkeypatch.setattr(synth, "gmail_message_id", ...)`: the registry captures the seed function
    object at `backlot.store` import time, so patching the module attribute afterward cannot reach
    it (see the confluence test above for the same defect)."""
    from tests._helpers import build_corpus

    docs = [
        {
            "source_type": "gmail",
            "mailbox": "a@acme.com",
            "doc_id": f"m{i}",
            "title": f"Mail {i}",
            "content": "x",
            "author_email": "a@acme.com",
        }
        for i in range(5)
    ]
    s = build_corpus(tmp_path / "ok", docs)
    conn = store.connect_ro(s.db_path)
    rows = conn.execute("SELECT doc_id, served_id FROM gmail_messages").fetchall()
    assert len(rows) == 5
    assert {r["served_id"] for r in rows} == {synth.gmail_message_id(r["doc_id"]) for r in rows}
    for r in rows:
        assert store.gmail_by_served_id(conn, r["served_id"])["doc_id"] == r["doc_id"]
    conn.close()

    monkeypatch.setitem(
        store.SERVED_ID, "gmail", ("served_id", lambda key: "00000000deadbeef", None)
    )
    with pytest.raises(sqlite3.IntegrityError):
        build_corpus(tmp_path / "collide", docs)


def test_notion_served_ids_are_stored_and_resolve(tmp_path, monkeypatch):
    """Notion has TWO synthesized id spaces per row (#51): `served_id` (synth.notion_id), every
    row, like gmail unprobed since synth._uuid_from draws from the full digest space; and
    `served_data_source_id` (synth.notion_data_source_id), the 2025-09-03 API's data source id for
    a DATABASE row only -- real Notion has no data source for a page, so a page's column stays
    NULL rather than colliding with anything.

    `served_data_source_id` is assigned directly from `synth.notion_data_source_id` in
    importer.byo, not through `store.SERVED_ID` (that registry stays one column per source, see
    its own comment) -- so forcing ITS collision needs `monkeypatch.setattr(synth, ...)`, the
    opposite of `served_id`'s: `store.SERVED_ID`'s tuple captures the seed function object at
    `backlot.store` import time (see the confluence/gmail tests above), so `served_id`'s collision
    needs `monkeypatch.setitem` on the registry, while `synth.notion_data_source_id` is looked up
    live off the module at call time in importer.byo (`from backlot import ... synth`), so patching
    the module attribute reaches it just fine there."""
    from tests._helpers import build_corpus

    docs = [
        {
            "source_type": "notion",
            "teamspace": "eng",
            "doc_id": "p0",
            "title": "Page 0",
            "content": "x",
            "author_email": "a@acme.com",
        },
        {
            "source_type": "notion",
            "teamspace": "eng",
            "doc_id": "db0",
            "subtype": "database",
            "title": "DB 0",
            "content": "x",
            "author_email": "a@acme.com",
        },
        {
            "source_type": "notion",
            "teamspace": "eng",
            "doc_id": "db1",
            "subtype": "database",
            "title": "DB 1",
            "content": "x",
            "author_email": "a@acme.com",
        },
    ]
    s = build_corpus(tmp_path / "ok", docs)
    conn = store.connect_ro(s.db_path)
    rows = {
        r["doc_id"]: r
        for r in conn.execute("SELECT doc_id, served_id, served_data_source_id FROM notion_pages")
    }
    assert rows["p0"]["served_id"] == synth.notion_id("p0")
    assert rows["db0"]["served_id"] == synth.notion_id("db0")
    assert rows["db0"]["served_data_source_id"] == synth.notion_data_source_id("db0")
    # a page never gets a data source id -- real Notion has no query target for a page.
    assert rows["p0"]["served_data_source_id"] is None
    assert store.notion_by_served_id(conn, synth.notion_id("p0"))["doc_id"] == "p0"
    assert (
        store.notion_by_data_source_id(conn, synth.notion_data_source_id("db0"))["doc_id"] == "db0"
    )
    # the two id spaces don't alias each other -- a page's own served_id must not also resolve as
    # SOME row's data source id (the reader must query served_data_source_id, not served_id).
    assert store.notion_by_data_source_id(conn, synth.notion_id("p0")) is None
    conn.close()

    monkeypatch.setitem(store.SERVED_ID, "notion", ("served_id", lambda doc_id: "0" * 32, None))
    with pytest.raises(sqlite3.IntegrityError):
        build_corpus(tmp_path / "collide-served-id", docs)
    monkeypatch.undo()

    monkeypatch.setattr(synth, "notion_data_source_id", lambda doc_id: "1" * 32)
    with pytest.raises(sqlite3.IntegrityError):
        build_corpus(tmp_path / "collide-data-source-id", docs)


def test_connect_ro_tuning(sample_settings):
    # tuned connection applies the pragmas; a plain one keeps sqlite defaults (tests unaffected)
    c = store.connect_ro(sample_settings.db_path, mmap_mb=64, cache_mb=16, temp_memory=True)
    try:
        assert c.execute("PRAGMA cache_size").fetchone()[0] == -16 * 1024
        assert c.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY
        assert c.execute("PRAGMA mmap_size").fetchone()[0] > 0
    finally:
        c.close()
    d = store.connect_ro(sample_settings.db_path)
    try:
        assert d.execute("PRAGMA mmap_size").fetchone()[0] == 0
    finally:
        d.close()


# --- incremental FTS indexing --------------------------------------------------


def _mini_db(tmp_path):
    conn = store.connect_rw(tmp_path / "m.sqlite")
    conn.execute(
        "INSERT INTO notion_pages(doc_id,teamspace,author_email,title,content,created_ts) "
        "VALUES('n1','eng','a@x.com','Alpha runbook','deploy alpha service',1)"
    )
    conn.commit()
    store.build_fts(conn)
    return conn


def test_fts_add_docs_indexes_new_without_dropping_old(tmp_path):
    conn = _mini_db(tmp_path)
    # a new page inserted AFTER the initial build is not searchable until indexed
    conn.execute(
        "INSERT INTO notion_pages(doc_id,teamspace,author_email,title,content,created_ts) "
        "VALUES('n2','eng','a@x.com','Beta guide','rotate beta credentials',2)"
    )
    conn.commit()
    assert store.search_documents(conn, "beta", "notion") == []  # not yet indexed
    n = store.fts_add_docs(conn, "notion", ["n2"])
    assert n == 1
    got = {r["doc_id"] for r in store.search_documents(conn, "beta", "notion")}
    assert "n2" in got
    # the original doc is still searchable (index not clobbered)
    assert {r["doc_id"] for r in store.search_documents(conn, "alpha", "notion")} == {"n1"}


def test_fts_add_docs_is_idempotent(tmp_path):
    conn = _mini_db(tmp_path)
    store.fts_add_docs(conn, "notion", ["n1"])  # re-index existing doc
    assert len(store.search_documents(conn, "alpha", "notion")) == 1  # no duplicate row


def test_fts_add_docs_noop_without_index(tmp_path):
    conn = store.connect_rw(tmp_path / "n.sqlite")  # no build_fts called
    assert store.fts_add_docs(conn, "notion", ["x"]) == 0


def test_repo_files_listing_and_kind_isolation(tmp_path):
    conn = store.connect_rw(tmp_path / "g.sqlite")
    # two files + one issue in the same repo
    conn.execute(
        "INSERT INTO github_items(doc_id,repo,author_email,title,content,kind,path,created_ts) "
        "VALUES('f1','svc','a@x','a.py','print(1)','file','src/a.py',1)"
    )
    conn.execute(
        "INSERT INTO github_items(doc_id,repo,author_email,title,content,kind,path,created_ts) "
        "VALUES('f2','svc','a@x','b.py','print(2)','file','src/b.py',1)"
    )
    conn.execute(
        "INSERT INTO github_items(doc_id,repo,author_email,title,content,kind,created_ts) "
        "VALUES('i1','svc','a@x','a bug','...', 'issue',1)"
    )
    conn.commit()
    files = store.list_repo_files(conn, "svc")
    assert [f["path"] for f in files] == ["src/a.py", "src/b.py"]  # only files, sorted, no issue
    assert store.count_repo_files(conn, "svc") == 2
    got = store.get_repo_file(conn, "svc", "src/b.py")
    assert got["content"] == "print(2)"
    assert store.get_repo_file(conn, "svc", "nope.py") is None


def test_list_repo_file_paths_agrees_with_the_full_listing(tmp_path):
    """The paths-only listing must select exactly what list_repo_files does — same repo, same
    kind='file' filter, same order, same ACL scoping. It exists because a caller that only needs to
    CHOOSE among a repo's files (github's pull changeset) would otherwise pull every file's content
    to do it, which on a 3000-file repo is a ~1.4 MB read per pull."""
    conn = store.connect_rw(tmp_path / "g.sqlite")
    rows = [
        ("f1", "src/b.py", "everyone"),
        ("f2", "src/a.py", "everyone"),
        ("f3", "s.py", "people"),
    ]
    for doc_id, path, principal in rows:
        conn.execute(
            "INSERT INTO github_items(doc_id,repo,author_email,title,content,kind,path,created_ts)"
            " VALUES(?,'svc','a@x',?,'body','file',?,1)",
            (doc_id, path.rsplit("/", 1)[-1], path),
        )
        conn.execute(
            "INSERT INTO github_acl(doc_id, principal_id, principal_type) VALUES(?,?,'group')",
            (doc_id, principal),
        )
    conn.execute(
        "INSERT INTO github_items(doc_id,repo,author_email,title,content,kind,created_ts) "
        "VALUES('i1','svc','a@x','a bug','...','issue',1)"
    )
    conn.commit()
    assert store.list_repo_file_paths(conn, "svc") == [
        f["path"] for f in store.list_repo_files(conn, "svc")
    ]
    for ids in (None, {"everyone"}, {"everyone", "people"}, set()):
        assert store.list_repo_file_paths(conn, "svc", ids) == [
            f["path"] for f in store.list_repo_files(conn, "svc", ids)
        ]
    assert store.list_repo_file_paths(conn, "svc", {"everyone"}) == ["src/a.py", "src/b.py"]


def test_hubspot_listing_is_an_index_range_seek(tmp_path):
    """Every read of hubspot_objects is "one object type, ordered by doc_id" — keyset paging and the
    search scan both. With only object_type indexed, ORDER BY falls to a temp b-tree that re-sorts
    every matching row on each page, so paging a large type costs O(rows) per page instead of
    O(log rows + page). Measured on 69k notes that was ~1s per search page; the composite index made
    it ~1ms. Same guard as test_list_s3_objects_prefix_uses_index_range_not_like."""
    conn = _hubspot_mini_db(tmp_path)
    plan = " ".join(
        r[-1]
        for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM hubspot_objects WHERE object_type = ? AND doc_id > ? "
            "ORDER BY doc_id LIMIT 10",
            ("contacts", "c0"),
        )
    )
    assert "idx_hubspot_type_doc" in plan
    assert "TEMP B-TREE" not in plan.upper(), f"ORDER BY is being sorted, not seeked: {plan}"


# --- Linear -----------------------------------------------------------------------
# Linear's store surface is its own set of functions (a Relay connection needs a total as well
# as a page, and comments are ACL-scoped through their parent issue), so it gets its own block.


def test_linear_list_and_count_agree(db):
    rows = store.list_linear_issues(db, limit=100)
    assert store.count_linear_issues(db) == len(rows) == 5
    assert {r["doc_id"] for r in rows} == {
        "lin-rl",
        "lin-batch",
        "lin-des",
        "lin-secret",
        "lin-blackops",
    }


def test_linear_list_scopes_to_a_team(db):
    rows = store.list_linear_issues(db, "design", limit=100)
    assert [r["doc_id"] for r in rows] == ["lin-des"]
    assert store.count_linear_issues(db, "design") == 1


def test_linear_default_order_is_createdAt_ascending(db):
    """Linear documents createdAt as the default ordering, and its `PaginationOrderBy` carries no
    direction — so an absent `orderBy` must still order by creation, not by insertion order."""
    default = [r["doc_id"] for r in store.list_linear_issues(db, "engineering", limit=100)]
    explicit = [
        r["doc_id"]
        for r in store.list_linear_issues(db, "engineering", limit=100, order_by="createdAt")
    ]
    assert default == explicit
    stamps = [r["created_ts"] for r in store.list_linear_issues(db, "engineering", limit=100)]
    assert stamps == sorted(stamps)


def test_linear_list_can_be_ordered_newest_first(db):
    rows = store.list_linear_issues(
        db, "engineering", limit=100, order_by="createdAt", descending=True
    )
    stamps = [r["created_ts"] for r in rows]
    assert stamps == sorted(stamps, reverse=True)


def test_linear_default_order_is_stable_across_pages(db):
    """An offset page over a non-total order can repeat or skip a row; walking one row at a time
    must therefore visit each issue exactly once."""
    seen = []
    for offset in range(store.count_linear_issues(db)):
        page = store.list_linear_issues(db, limit=1, offset=offset)
        seen.append(page[0]["doc_id"])
    assert len(seen) == len(set(seen)) == 5


def test_linear_identifier_lookup(db):
    assert store.linear_issue_by_identifier(db, "ENG-101")["doc_id"] == "lin-rl"
    assert store.linear_issue_by_identifier(db, "NOPE-1") is None


def test_linear_prefilter_is_pushed_into_sql(db):
    flt = ("state = ?", ["Done"])
    assert store.count_linear_issues(db, prefilter=flt) == 1
    assert store.list_linear_issues(db, prefilter=flt)[0]["doc_id"] == "lin-batch"


def test_linear_comments_scoped_to_one_issue(db):
    rows = store.list_linear_comments(db, doc_id="lin-rl")
    assert [r["body"] for r in rows] == ["Reproduced with a burst test.", "Fix is in review."]
    assert store.count_linear_comments(db, doc_id="lin-rl") == 2


def test_linear_comments_across_the_corpus(db):
    assert store.count_linear_comments(db) == 3


def test_linear_team_issue_counts(db):
    assert store.linear_team_issue_counts(db) == {"engineering": 3, "design": 1, "blackops": 1}


def test_linear_distinct_values_feed_the_reverse_index(db):
    d = store.linear_distinct_values(db)
    assert ("engineering", "In Progress") in d["states"]
    assert "runtime-stability" in d["projects"]
    assert ("engineering", "2025-W08") in d["cycles"]
    assert {"bug", "gateway", "latency", "tokens"} <= set(d["labels"])
    assert ("bob@acme.com", "Bob Stone") in d["users"]


def test_linear_comment_table_is_registered(db):
    assert store.comment_table("linear") == "linear_comments"


def test_linear_relation_by_id_scopes_the_from_end_too(tmp_path):
    """The docstring claims a relation is "scoped on BOTH ends". A relation whose `to` issue is
    granted to the caller but whose `from` issue is NOT must still be hidden — this pins the alias
    collision where `_acl_clause`'s inner alias shadowed the caller's outer "a"/"b" alias and
    turned the from-end predicate into a tautology (`a.doc_id = a.doc_id`), which admitted a
    relation off an unreadable issue to any caller holding ANY grant at all in `linear_acl`."""
    from backlot import synth

    conn = store.connect_rw(tmp_path / "rel.sqlite")
    for doc_id in ("i1", "i2"):
        conn.execute(
            "INSERT INTO linear_issues(doc_id, team, author_email, title, content, created_ts) "
            "VALUES (?,'eng','a@x.com','issue','body',1)",
            (doc_id,),
        )
    conn.execute(
        "INSERT INTO linear_relations(id, from_doc_id, to_doc_id, type, created_ts) "
        "VALUES ('r1','i1','i2','blocks',1)"
    )
    # Only i2 (the `to` end) is granted; i1 (the `from` end) carries no grant at all.
    conn.execute("INSERT INTO linear_acl VALUES ('i2','group','bob')")
    conn.commit()
    served = synth.linear_relation_id("r1")
    assert store.linear_relation_by_id(conn, served, {"bob"}) is None
    assert store.linear_relation_by_id(conn, served, None) is not None  # admin bypass still works
    # Granting i1 too makes it visible — proving the hiding above was the ACL, not a broken query.
    conn.execute("INSERT INTO linear_acl VALUES ('i1','group','bob')")
    conn.commit()
    assert store.linear_relation_by_id(conn, served, {"bob"}) is not None


# --- fireflies ------------------------------------------------------------------
# Sentences occupy the per-source child-rows slot, so the registry assertion above already
# covers the table wiring; these cover the reads the GraphQL resolvers are built on.


def test_fireflies_child_rows_use_the_shared_comment_slot(db):
    # Not comments, but the same "child rows of a doc" mapping — and therefore the same column
    # contract, which is what makes doc_comments work against it unchanged.
    assert store.comment_table("fireflies") == "fireflies_sentences"
    rows = store.doc_comments(db, "fireflies", "ff-discovery")
    assert [r["seq"] for r in rows] == [1, 2, 3, 4]
    assert rows[0]["body"].startswith("Thanks for joining")


def test_fireflies_scope_columns_are_the_api_vocabulary():
    assert store.fireflies_scope_columns("title") == ("title",)
    assert store.fireflies_scope_columns("sentences") == ("content",)
    assert store.fireflies_scope_columns("all") == ("title", "content")
    assert store.fireflies_scope_columns(None) == ("title", "content")  # default
    assert store.fireflies_scope_columns("ALL") == ("title", "content")  # case-insensitive
    assert store.fireflies_scope_columns("body") is None  # not an API value


def test_fireflies_transcripts_are_newest_first(db):
    rows = store.list_fireflies_transcripts(db, limit=50)
    tss = [r["created_ts"] for r in rows]
    assert tss == sorted(tss, reverse=True)
    assert {r["doc_id"] for r in rows} == {"ff-discovery", "ff-allhands", "ff-secret"}


def test_fireflies_limit_and_skip_walk_the_corpus_without_gaps(db):
    everything = [r["doc_id"] for r in store.list_fireflies_transcripts(db, limit=50)]
    walked = []
    for skip in range(0, len(everything)):
        page = store.list_fireflies_transcripts(db, limit=1, offset=skip)
        walked += [r["doc_id"] for r in page]
    assert walked == everything
    # past the end is an empty page, not an error
    assert store.list_fireflies_transcripts(db, limit=5, offset=999) == []


def test_fireflies_keyword_honours_every_scope(db):
    # "selects" appears only in a SENTENCE of ff-allhands, never in any title.
    assert [
        r["doc_id"]
        for r in store.list_fireflies_transcripts(db, keyword="selects", scope="sentences")
    ] == ["ff-allhands"]
    assert store.list_fireflies_transcripts(db, keyword="selects", scope="title") == []
    assert [
        r["doc_id"] for r in store.list_fireflies_transcripts(db, keyword="selects", scope="all")
    ] == ["ff-allhands"]
    # "all-hands" appears only in a TITLE.
    assert [
        r["doc_id"]
        for r in store.list_fireflies_transcripts(db, keyword="all-hands", scope="title")
    ] == ["ff-allhands"]
    assert store.list_fireflies_transcripts(db, keyword="all-hands", scope="sentences") == []


def test_fireflies_keyword_wildcards_stay_literal(db):
    # A LIKE metacharacter in the needle must not turn into a match-everything pattern.
    assert store.list_fireflies_transcripts(db, keyword="%") == []
    assert store.list_fireflies_transcripts(db, keyword="_atency") == []
    assert [r["doc_id"] for r in store.list_fireflies_transcripts(db, keyword="latency")]


def test_fireflies_filters_narrow_by_channel_host_and_date(db):
    assert [r["doc_id"] for r in store.list_fireflies_transcripts(db, channel="all-hands")] == [
        "ff-allhands"
    ]
    assert [
        r["doc_id"] for r in store.list_fireflies_transcripts(db, host_email="AVA@acme.com")
    ] == ["ff-discovery"]  # case-insensitive
    ts = store.list_fireflies_transcripts(db, channel="sales-calls")[0]["created_ts"]
    assert [r["doc_id"] for r in store.list_fireflies_transcripts(db, to_ts=ts)] == ["ff-discovery"]
    assert "ff-discovery" not in [
        r["doc_id"] for r in store.list_fireflies_transcripts(db, from_ts=ts + 1)
    ]


def test_fireflies_organizer_filter_falls_back_to_the_host(db):
    # organizer_email is NULL when the organizer IS the host, so filtering by the host's address
    # must still match — otherwise organizing your own meeting would not find it.
    assert (
        db.execute(
            "SELECT organizer_email FROM fireflies_transcripts WHERE doc_id = 'ff-discovery'"
        ).fetchone()[0]
        is None
    )
    assert [
        r["doc_id"] for r in store.list_fireflies_transcripts(db, organizers=["ava@acme.com"])
    ] == ["ff-discovery"]


def test_fireflies_participant_filter_is_exact_membership(db):
    assert [
        r["doc_id"] for r in store.list_fireflies_transcripts(db, participants=["ava@acme.com"])
    ] == ["ff-discovery"]
    # a substring of a stored address must NOT match (json_each, not a LIKE on the JSON text)
    assert store.list_fireflies_transcripts(db, participants=["ava@acme.co"]) == []


def test_fireflies_transcript_by_id_is_unambiguous(db):
    row = store.list_fireflies_transcripts(db, limit=1)[0]
    assert store.fireflies_transcript_by_id(db, row["transcript_id"])["doc_id"] == row["doc_id"]
    assert store.fireflies_transcript_by_id(db, "deadbeefdeadbeefdeadbeef") is None
    # unique by construction (derived from the doc_id), unlike Linear's identifier
    n, distinct = db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT transcript_id) FROM fireflies_transcripts"
    ).fetchone()
    assert n == distinct


def test_fireflies_sentences_come_back_in_spoken_order(db):
    rows = store.fireflies_sentences(db, "ff-discovery")
    assert [r["seq"] for r in rows] == [1, 2, 3, 4]
    assert [r["start_time"] for r in rows] == sorted(r["start_time"] for r in rows)
    # every window is forward-going and contiguous with the next
    for a, b in zip(rows, rows[1:]):
        assert a["start_time"] < a["end_time"] <= b["start_time"]


def test_fireflies_counts_agree_with_the_pages(db):
    for kw, scope in [
        (None, None),
        ("latency", "all"),
        ("selects", "sentences"),
        ("all-hands", "title"),
    ]:
        total = store.count_fireflies_transcripts(db, keyword=kw, scope=scope)
        assert total == len(store.list_fireflies_transcripts(db, keyword=kw, scope=scope, limit=50))


# --- meta table (build-time facts) -----------------------------------------------


def test_meta_round_trips(tmp_path):
    conn = store.connect_rw(tmp_path / "meta.sqlite")
    store.write_meta(conn, "source_documents", 531248)
    assert store.read_meta(conn, "source_documents") == "531248"
    conn.close()


def test_meta_absent_key_is_none(tmp_path):
    conn = store.connect_rw(tmp_path / "meta.sqlite")
    assert store.read_meta(conn, "never_written") is None
    conn.close()


def test_meta_overwrites(tmp_path):
    conn = store.connect_rw(tmp_path / "meta.sqlite")
    store.write_meta(conn, "source_documents", 1)
    store.write_meta(conn, "source_documents", 2)
    assert store.read_meta(conn, "source_documents") == "2"
    conn.close()


def test_read_meta_tolerates_a_db_without_the_table(tmp_path):
    """A DB built before this change has no meta table; /health must still answer.

    Simulates the deployed box's DB: created with the full pre-Task-3 schema, then the meta
    table dropped to represent a pre-meta-table version."""
    path = tmp_path / "old.sqlite"
    # Create a DB with the full schema, then drop the meta table to simulate the deployed box
    conn = store.connect_rw(path)
    conn.execute("DROP TABLE IF EXISTS meta")
    conn.commit()
    conn.close()
    # Re-open and verify read_meta tolerates the missing table
    conn = sqlite3.connect(path)
    assert store.read_meta(conn, "source_documents") is None
    conn.close()


def test_write_meta_commits_the_entire_transaction(tmp_path):
    """write_meta commits the entire pending transaction, not just its own row.

    This test verifies the contract documented in the docstring: when called from the importers
    (Task 4), a write_meta call flushes any unrelated pending inserts."""
    path = tmp_path / "committed.sqlite"
    conn = store.connect_rw(path)
    # Insert unrelated data without committing
    conn.execute(
        "INSERT INTO notion_pages(doc_id,teamspace,author_email,title,content,created_ts) "
        "VALUES('n1','eng','a@x.com','Test','content',1)"
    )
    # write_meta commits the entire pending transaction
    store.write_meta(conn, "test_key", "test_value")
    conn.close()
    # From a separate connection, verify both the unrelated row and the meta row are visible
    check_conn = store.connect_ro(path)
    assert (
        check_conn.execute("SELECT COUNT(*) FROM notion_pages WHERE doc_id = 'n1'").fetchone()[0]
        == 1
    )
    assert store.read_meta(check_conn, "test_key") == "test_value"
    check_conn.close()
