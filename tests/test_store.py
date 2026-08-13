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
    reversing a hash back to a doc_id through a stored column assigned at import, each converted
    in its own task (#51) -- confluence, gmail, notion, hubspot, linear, github and, last, jira
    (task 8) -- so the registry has to be total from the start or a later task's column goes
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
    # Confluence's own entry -- column name, seed function, corpus-wide scope. Every entry in the
    # registry is read by its own stored-column reader now (github_by_served_number,
    # jira_by_served_number, etc.) -- none is left to main._build_index's reverse map.
    assert store.SERVED_ID["confluence"] == ("served_id", synth.confluence_id, None)


def test_no_blanket_replace_writes_a_table_with_a_non_pk_unique_index(tmp_path):
    """`INSERT OR REPLACE` has no scoped conflict target: on ANY unique-index violation, not only
    the PRIMARY KEY, it resolves the conflict by silently DELETING the other row rather than
    raising. For a table whose PRIMARY KEY is a real business key (a doc_id, a team, an email)
    that ALSO carries an unrelated unique-indexed served id, a served-id collision between two
    DIFFERENT rows then destroys the other row silently instead of failing the import loudly --
    exactly the failure mode #51 exists to remove (see write_containers' and fireflies_users' own
    upsert comments in backlot/importer/byo.py). This bug was independently reintroduced twice
    while converting `linear_teams` and `fireflies_users` alone.

    A pure `grep 'INSERT OR REPLACE INTO <table>'` cannot catch a THIRD occurrence here: every
    shared, multi-table write path in this codebase (the doc tables' `insert`, the comment
    tables' shared insert, `write_containers`' own `else` branch) names its target through an
    f-string variable (`{store.table(src)}`, `{ctable}`, `{gtable}`), never the literal table
    name -- so a naive text search for a guarded table's literal name never matches those sites
    regardless of whether the bug is present, which would make this guard a silent no-op for
    every one of them. This version checks each shared path on its own terms instead:

    - The doc tables (`SOURCE_TABLE`) and comment tables (`COMMENT_TABLE`) are structurally safe
      already: `insert` upserts on `ON CONFLICT(doc_id) DO UPDATE` (never `OR REPLACE`), and the
      comment insert deliberately excludes `served_id` from its column list (github_comments'
      own is set by a separate, non-`OR REPLACE`, `id`-scoped `UPDATE` -- see that insert's own
      comment). Neither is re-verified here; this guard is about tables NOT already covered by
      one of those two uniform mechanisms.
    - `write_containers`' `else` branch is where BOTH real occurrences of this bug lived: it runs
      one blanket `INSERT OR REPLACE` for every `GROUPING` table except the ones it special-cases
      (currently only `linear`, which gets its own scoped upsert). If any OTHER grouping table
      gains a non-PK unique index without also being added to that special-case set, this
      assertion catches it at the SCHEMA level -- no text search needed, since it reasons about
      which branch a table falls into, not what literal string appears near it.
    - A table with its OWN one-off write, outside both mechanisms above (currently just
      `fireflies_users`), is exactly the case a literal-name grep IS reliable for, since a
      one-off write has no reason to interpolate its own table name through a variable.

    What this guard deliberately does NOT re-verify: that `linear`'s OWN special-cased branch and
    `fireflies_users`' OWN write are themselves correct -- that is what
    `test_linear_team_served_ids_are_stored_and_resolve` and
    `test_fireflies_users_is_the_workspace_roster_not_every_named_person`'s trailing
    `pytest.raises(sqlite3.IntegrityError)` blocks are for; a behavioural collision test is the
    only way to verify a SPECIFIC write is right, while this guard's job is to catch a table that
    fell through every mechanism this schema currently trusts."""
    from pathlib import Path

    conn = store.connect_rw(tmp_path / "schema-only.sqlite")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    guarded = set()
    for t in tables:
        for idx in conn.execute(f"PRAGMA index_list({t})"):
            # (seq, name, unique, origin, partial) -- `origin='pk'` is the PRIMARY KEY's own
            # implicit unique constraint, which INSERT OR REPLACE is fine to fold into an upsert;
            # any OTHER unique index is the hazard this guard is for.
            if idx[2] == 1 and idx[3] != "pk":
                guarded.add(t)
    conn.close()
    # Sanity: the guard must find at least the tables this task's own bug lived in, or the
    # PRAGMA-based detection above is broken and every assertion below is vacuous.
    assert {"linear_teams", "fireflies_users"} <= guarded

    # `write_containers`: every grouping table except its declared special cases must have NO
    # non-PK unique index, or its generic `else` branch's blanket OR REPLACE reaches it.
    write_containers_special_cases = {"linear"}
    for src, (gtable, _gcol) in store.GROUPING.items():
        if src in write_containers_special_cases:
            continue
        assert gtable not in guarded, (
            f"{gtable} (source {src!r}) has a non-PK unique index but write_containers' generic "
            "`else` branch still writes it with a blanket INSERT OR REPLACE -- give it its own "
            "scoped upsert (the way linear_teams' has one) and add it to the special-case set "
            "above before this lands"
        )

    # What's left, once the two uniform doc/comment write paths and the grouping-table path
    # above are accounted for, is a table with its own bespoke write -- safe to name-match on,
    # since a one-off write has no reason to hide its target behind a variable.
    generic_tables = set(store.SOURCE_TABLE.values()) | set(store.COMMENT_TABLE.values())
    grouping_tables = {g for g, _ in store.GROUPING.values()}
    standalone_guarded = guarded - generic_tables - grouping_tables
    assert "fireflies_users" in standalone_guarded  # sanity: the guard reaches this table at all

    backlot_src = "\n".join(
        p.read_text() for p in Path(store.__file__).resolve().parent.rglob("*.py")
    )
    for t in sorted(standalone_guarded):
        assert f"INSERT OR REPLACE INTO {t}" not in backlot_src, (
            f"{t} has a non-PK unique index and its own bespoke write, but that write is a "
            "blanket INSERT OR REPLACE -- a served-id collision between two different rows "
            "would silently delete one instead of raising; use "
            "INSERT ... ON CONFLICT(<primary key>) DO UPDATE instead"
        )


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
    # `fireflies_users` is written at import, one row per user principal (#51) -- a unique-indexed
    # column lookup now, not the reverse map `main._build_index` used to build from `list_users`.
    assert store.fireflies_user_by_served_id(db, synth.fireflies_user_id("ava@acme.com")) == (
        "ava@acme.com"
    )
    assert store.fireflies_user_by_served_id(db, "not-a-real-served-id") is None
    # The two sides can no longer disagree by construction the way they could before this table
    # existed (the map was rebuilt from `principals` on every boot, so there was nothing to drift):
    # every `type='user'` principal must have EXACTLY one `fireflies_users` row, or `user(id:)`
    # either serves a fabricated user for an orphan row, or nulls out an id `users` just listed.
    user_principals = {r["id"] for r in db.execute("SELECT id FROM principals WHERE type = 'user'")}
    ff_users = {r[0] for r in db.execute("SELECT email FROM fireflies_users")}
    assert user_principals == ff_users


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


def test_s3_by_bucket_key(tmp_path):
    conn = _s3_mini_db(tmp_path)
    assert store.s3_by_bucket_key(conn, "b", "logs/2026/01/a.json")["doc_id"] == "d1"
    # the trap: key alone is not unique -- the same key in a different bucket is a different row
    assert store.s3_by_bucket_key(conn, "other", "logs/2026/01/a.json")["doc_id"] == "d5"
    assert store.s3_by_bucket_key(conn, "b", "nope") is None
    # d2 ("logs/2026/01/b.json") is ACL-restricted to group 'eng'; a non-empty visible_ids
    # granting nothing must still 404 it (runs _acl_clause's EXISTS branch, not its AND 0
    # short-circuit), while the granted group finds it.
    assert store.s3_by_bucket_key(conn, "b", "logs/2026/01/b.json", visible_ids={"nobody"}) is None
    found = store.s3_by_bucket_key(conn, "b", "logs/2026/01/b.json", visible_ids={"eng"})
    assert found["doc_id"] == "d2"


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
    (IF NOT EXISTS only guards the index name, not the referenced column).

    `served_number` is included in the hand-rolled table on purpose, even though this fixture
    predates it: unlike `path`/`changed_paths`/`number` (and unlike `github_comments.served_id`,
    an OLDER feature that predates this whole #51 series and IS self-healed below), it is NOT in
    connect_rw's self-heal ALTER list -- no back-compat for the per-DOCUMENT served-id-style
    columns tasks 3-7 added (confluence_pages/hubspot_objects/notion_pages/linear_issues/
    github_items's own `served_id`/`served_number`; see idx_github_served's schema comment), so a
    table missing it would fail at the UNIQUE index in SCHEMA for a reason this test isn't about.
    This test's own subject stays exactly `path`'s self-heal."""
    p = tmp_path / "old.sqlite"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE github_items ("
        "doc_id TEXT PRIMARY KEY, repo TEXT NOT NULL, author_email TEXT NOT NULL, "
        "title TEXT NOT NULL, content TEXT NOT NULL, kind TEXT, state TEXT, labels TEXT, "
        "assignees TEXT, merged_at TEXT, head_ref TEXT, base_ref TEXT, reviews TEXT, "
        "reactions TEXT, created_ts INTEGER NOT NULL, updated_ts INTEGER, closed_ts INTEGER, "
        "closed_by TEXT, merged_by TEXT, milestone TEXT, requested_reviewers TEXT, "
        "owner_display TEXT, served_number INTEGER"
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
    the module attribute reaches it just fine there.

    Also covers a regression the review round found: a database demoted to a non-database on a
    later import of the SAME doc_id (two records sharing one doc_id are both written, the later
    one winning -- see importer.byo's `seen.add` comment) must clear the stale
    served_data_source_id, not leave the earlier import's value in place for a row that is now a
    page."""
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
    # notion_by_data_source_id must apply visible_ids like notion_by_served_id does -- a
    # principal with no grant on db0 gets no row, not the unscoped one. A non-empty set, so
    # _acl_clause takes its EXISTS branch rather than the empty-set "AND 0" short-circuit.
    assert (
        store.notion_by_data_source_id(
            conn, synth.notion_data_source_id("db0"), visible_ids={"nobody"}
        )
        is None
    )
    conn.close()

    # Regression: a doc_id imported once as a database, then again (later in the same corpus) as
    # a non-database, must not keep answering to its old data-source id -- that would serve a page
    # as a data source (get_data_source relies on a served_data_source_id match implying
    # subtype='database'; see the comment on the importer's notion block).
    flip_docs = [
        {
            "source_type": "notion",
            "teamspace": "eng",
            "doc_id": "flip",
            "subtype": "database",
            "title": "Was a DB",
            "content": "x",
            "author_email": "a@acme.com",
        },
        {
            "source_type": "notion",
            "teamspace": "eng",
            "doc_id": "flip",
            "title": "Now a page",
            "content": "x",
            "author_email": "a@acme.com",
        },
    ]
    s = build_corpus(tmp_path / "flip", flip_docs)
    conn = store.connect_ro(s.db_path)
    row = conn.execute(
        "SELECT subtype, served_data_source_id FROM notion_pages WHERE doc_id = 'flip'"
    ).fetchone()
    assert row["subtype"] != "database"
    assert row["served_data_source_id"] is None
    assert store.notion_by_data_source_id(conn, synth.notion_data_source_id("flip")) is None
    conn.close()

    monkeypatch.setitem(store.SERVED_ID, "notion", ("served_id", lambda doc_id: "0" * 32, None))
    with pytest.raises(sqlite3.IntegrityError):
        build_corpus(tmp_path / "collide-served-id", docs)
    monkeypatch.undo()

    monkeypatch.setattr(synth, "notion_data_source_id", lambda doc_id: "1" * 32)
    with pytest.raises(sqlite3.IntegrityError):
        build_corpus(tmp_path / "collide-data-source-id", docs)


def test_hubspot_served_ids_probe_on_a_collision_and_stay_stable_across_a_reimport(
    tmp_path, monkeypatch
):
    """`synth.hubspot_record_id` draws from 9,000,000,000 values -- wide enough to look safe, but
    unlike gmail's 2**63 and notion's UUID space it is STILL not wide enough to skip a probe:
    measured over synthetic corpora it collides ~16 times at 500k documents (#51, see the task-5
    brief). So hubspot follows CONFLUENCE's probe-and-walk, not gmail's/notion's bare-seed shape.

    Forced here by collapsing the seed to a CONSTANT -- and the assertion is the OPPOSITE of
    gmail's forced-collision test: gmail's must ABORT the import (no probe -- a duplicate is a
    loud UNIQUE-index failure), while hubspot's must still produce N DISTINCT served_ids, each
    resolving to its own record, because the probe is what absorbs the collision instead of
    surfacing it.

    Patches `store.SERVED_ID` directly, NOT `synth.hubspot_record_id`: `store.served_id_seed
    ("hubspot")` -- what `_assign_hubspot_id` actually calls -- returns the tuple element
    `SERVED_ID` captured when `backlot.store` was first imported, a bound reference to the
    function object, not a live attribute lookup. `monkeypatch.setattr(synth, ...)` cannot reach
    it (see the confluence/gmail/notion tests above for the same defect); `monkeypatch.setitem`
    replaces the tuple the accessor actually reads.
    """
    from backlot.importer import byo
    from tests._helpers import build_corpus

    monkeypatch.setitem(
        store.SERVED_ID, "hubspot", ("served_id", lambda doc_id: "1000000000", None)
    )
    docs = [
        {
            "source_type": "hubspot",
            "doc_id": f"h{i}",
            "object_type": "companies",
            "title": f"Co {i}",
            "content": "x",
            "author_email": "a@acme.com",
            "properties": {"name": f"Co {i}"},
        }
        for i in range(5)
    ]
    s = build_corpus(tmp_path, docs)
    monkeypatch.undo()
    conn = store.connect_ro(s.db_path)
    served = [r["served_id"] for r in conn.execute("SELECT served_id FROM hubspot_objects")]
    assert len(served) == 5 and len(set(served)) == 5 and all(served)
    # The collapsed seed actually landed: h0 (the first record processed, before anything is
    # taken) is served exactly the forced value -- not some real hash. If this drifts back to a
    # real hash, the patch has gone inert again, silently, the same way it did before.
    assert store.hubspot_by_served_id(conn, "1000000000")["doc_id"] == "h0"
    for sid in served:
        row = store.hubspot_by_served_id(conn, sid)
        assert row is not None and row["served_id"] == sid
    conn.close()

    # A re-import (e.g. --append re-running over the same shard) must not renumber a record a
    # client may already hold a url for -- more load-bearing here than for confluence: a PROBED id
    # is not a pure function of doc_id, so without seed_tracker_ids' preload a replay could hand a
    # record a DIFFERENT id than the one already served.
    byo.load(s.data_dir / "_corpus.jsonl", s, reset=False)
    conn = store.connect_ro(s.db_path)
    assert sorted(
        r["served_id"] for r in conn.execute("SELECT served_id FROM hubspot_objects")
    ) == sorted(served)
    conn.close()


def test_hubspot_by_served_id_applies_the_acl(tmp_path):
    """A regression `notion_by_served_id` shipped without (#51 review round): a non-empty
    ``visible_ids`` that grants nothing must come back None, not the unscoped row -- otherwise the
    ACL clause could be deleted from `hubspot_by_served_id` invisibly. A non-empty set, so
    `_acl_clause` takes its EXISTS branch rather than the empty-set "AND 0" short-circuit."""
    from tests._helpers import tiny_corpus

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "hubspot",
                "doc_id": "h0",
                "object_type": "companies",
                "title": "Co 0",
                "content": "x",
                "author_email": "a@acme.com",
                "properties": {"name": "Co 0"},
            }
        ],
    )
    conn = store.connect_ro(s.db_path)
    served = conn.execute("SELECT served_id FROM hubspot_objects").fetchone()["served_id"]
    assert store.hubspot_by_served_id(conn, served)["doc_id"] == "h0"
    assert store.hubspot_by_served_id(conn, served, visible_ids={"nobody"}) is None


def test_hubspot_associations_carry_the_targets_own_served_id_through_a_collision(
    tmp_path, monkeypatch
):
    """`hubspot_associations` joins to `hubspot_objects` for the target's OWN stored `served_id`
    (as `to_served_id`) rather than leaving the v4 payload to recompute
    `synth.hubspot_record_id(to_doc_id)` the way gmail's `threadId` re-hashes a root row (see
    `routers.google._gmail_ids`). That shortcut is safe for gmail because its seed is never probed
    -- hash and stored column always agree -- but hubspot's IS probed (#51): a collision walk can
    move a record to an id different from its raw hash, and the association payload must report
    the one the target's own row actually resolves at.

    Forced the same way the served-id collision test above does: the seed collapsed to a
    constant, so of the two companies below, whichever is assigned second gets walked away from
    the forced value -- and `to_served_id` must track whichever one that turns out to be, not the
    raw hash.
    """
    from tests._helpers import tiny_corpus

    monkeypatch.setitem(
        store.SERVED_ID, "hubspot", ("served_id", lambda doc_id: "1000000000", None)
    )
    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "hubspot",
                "doc_id": "co0",
                "object_type": "companies",
                "title": "Co 0",
                "content": "x",
                "author_email": "a@acme.com",
                "properties": {"name": "Co 0"},
            },
            {
                "source_type": "hubspot",
                "doc_id": "co1",
                "object_type": "companies",
                "title": "Co 1",
                "content": "x",
                "author_email": "a@acme.com",
                "properties": {"name": "Co 1"},
                "associations": [{"to": "co0", "label": "Sibling"}],
            },
        ],
    )
    monkeypatch.undo()
    conn = store.connect_ro(s.db_path)
    actual = {
        r["doc_id"]: r["served_id"]
        for r in conn.execute("SELECT doc_id, served_id FROM hubspot_objects")
    }
    # The collision was actually forced (both start from "1000000000") AND resolved to two
    # distinct values -- otherwise the join below would trivially "match" against a value neither
    # side actually had to walk away from.
    assert len(set(actual.values())) == 2
    rows = store.hubspot_associations(conn, "co1", "companies")
    assert rows[0]["to_served_id"] == actual["co0"]
    back = store.hubspot_associations(conn, "co0", "companies")
    assert back[0]["to_served_id"] == actual["co1"]


def test_github_served_numbers_probe_on_a_collision_and_stay_stable_across_a_reimport(
    tmp_path, monkeypatch
):
    """github's served number, like hubspot's/confluence's, is PROBED against a collision, not
    stored as the bare hash: `synth.github_number`'s space is only 1..90,000 PER REPO (#51's
    tightest of the probed sources -- see store.SERVED_ID's `scope`), so a real corpus collides
    far short of the birthday bound. Forced here by collapsing the seed to a constant, the same
    shape as the hubspot test above.

    Also covers, in one corpus, the two things specific to github among the probed sources:
    - `kind='file'` rows are excluded from the number space entirely, not merely probed around --
      their served_number stays NULL, and several coexist under the UNIQUE (repo, served_number)
      index alongside the 5 resolved issue numbers IN THE SAME REPO, which this import succeeding
      at all already proves (SQLite exempts NULL from a UNIQUE constraint).
    - Stability across a re-import: unlike confluence's/hubspot's assignment (computed inline per
      record, memoized on the `_Loader` as it goes), github's is a DEFERRED pass
      (`resolve_github_numbers`) run once every record has loaded, and it is THAT method's own
      `_github_numbers` memo -- populated by `seed_tracker_ids` before this run's insert can reset
      the column to NULL -- that a replay actually depends on to reproduce the same numbers, not
      the corpus alone.

    Patches `store.SERVED_ID` directly, NOT `synth.github_number`: `store.served_id_seed
    ("github")` -- what `_assign_github_number` actually calls -- returns the tuple element
    `SERVED_ID` captured when `backlot.store` was first imported, a bound reference to the
    function object, not a live attribute lookup (see the confluence/gmail/notion/hubspot tests
    above for the same defect with `monkeypatch.setattr`).
    """
    from backlot.importer import byo
    from tests._helpers import build_corpus

    monkeypatch.setitem(store.SERVED_ID, "github", ("served_number", lambda doc_id: 7, "repo"))
    docs = [
        {
            "source_type": "github",
            "doc_id": f"g{i}",
            "repo": "core",
            "title": f"Issue {i}",
            "content": "x",
            "author_email": "a@acme.com",
        }
        for i in range(5)
    ] + [
        {
            "source_type": "github",
            "doc_id": f"g-file-{i}",
            "repo": "core",
            "subtype": "file",
            "path": f"src/f{i}.py",
            "title": f"f{i}.py",
            "content": "x",
            "author_email": "a@acme.com",
        }
        for i in range(3)
    ]
    s = build_corpus(tmp_path, docs)
    monkeypatch.undo()
    conn = store.connect_ro(s.db_path)
    issues = {
        r["doc_id"]: r["served_number"]
        for r in conn.execute("SELECT doc_id, served_number FROM github_items WHERE kind != 'file'")
    }
    files = [
        r["served_number"]
        for r in conn.execute("SELECT served_number FROM github_items WHERE kind = 'file'")
    ]
    # The collapsed seed actually landed -- g0, the first row processed (before anything is
    # taken), is served exactly the forced value, not some real hash -- AND resolved to 5
    # DISTINCT numbers, otherwise the collision below never actually happened and the rest of
    # this test passed for the wrong reason.
    assert issues["g0"] == 7
    assert len(issues) == 5 and len(set(issues.values())) == 5
    for doc_id, n in issues.items():
        assert store.github_by_served_number(conn, "core", n)["doc_id"] == doc_id
    # file rows stay NULL, several of them, coexisting under the UNIQUE index with the 5 real
    # numbers above IN THE SAME REPO -- proven by this import having succeeded at all.
    assert len(files) == 3 and all(n is None for n in files)
    conn.close()

    # A re-import (e.g. --append re-running over the same shard) must not renumber an issue a
    # client may already hold a url for -- more load-bearing here than for confluence's/
    # hubspot's own version of this check: a probed number is not a pure function of doc_id, and
    # unlike those two, github's assignment is not even memoized as it runs -- resolve_github_
    # numbers rebuilds its whole taken-set fresh every call, so ONLY seed_tracker_ids' preload
    # stands between a replay and a fully reshuffled repo.
    byo.load(s.data_dir / "_corpus.jsonl", s, reset=False)
    conn = store.connect_ro(s.db_path)
    replay = {
        r["doc_id"]: r["served_number"]
        for r in conn.execute("SELECT doc_id, served_number FROM github_items WHERE kind != 'file'")
    }
    assert replay == issues
    conn.close()


def test_github_by_served_number_applies_the_acl(tmp_path):
    """A regression `notion_by_served_id` shipped without (#51 review round), and every reader
    since has had to guard against: a non-empty ``visible_ids`` that grants nothing must come back
    None, not the unscoped row -- otherwise the ACL clause could be deleted from
    `github_by_served_number` invisibly. A non-empty set, so `_acl_clause` takes its EXISTS branch
    rather than the empty-set "AND 0" short-circuit."""
    from tests._helpers import tiny_corpus

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "github",
                "doc_id": "g0",
                "repo": "core",
                "title": "Issue 0",
                "content": "x",
                "author_email": "a@acme.com",
            }
        ],
    )
    conn = store.connect_ro(s.db_path)
    served = conn.execute("SELECT served_number FROM github_items").fetchone()["served_number"]
    assert store.github_by_served_number(conn, "core", served)["doc_id"] == "g0"
    assert store.github_by_served_number(conn, "core", served, visible_ids={"nobody"}) is None
    conn.close()


def test_jira_served_numbers_probe_on_a_collision_and_stay_stable_across_a_reimport(
    tmp_path, monkeypatch
):
    """jira's served suffix, like github's served number, is PROBED against a collision, not
    stored as the bare hash: `synth.jira_key_number`'s space is only 1..9,000 PER PROJECT (#51's
    task 8 -- see store.SERVED_ID's `scope`), so a real project collides far short of the birthday
    bound. Forced here by collapsing the seed to a constant, the same shape as the github test
    above (no `kind='file'` exclusion to also cover -- jira has no file-shaped row).

    Also covers stability across a re-import: like github's assignment (and unlike confluence's/
    hubspot's inline probe), jira's is a DEFERRED pass (`resolve_jira_numbers`) run once every
    record has loaded, and it is THAT method's own `_jira_numbers` memo -- populated by
    `seed_tracker_ids` before this run's insert can reset the column to NULL -- that a replay
    actually depends on to reproduce the same suffixes, not the corpus alone.

    Patches `store.SERVED_ID` directly, NOT `synth.jira_key_number`: `store.served_id_seed
    ("jira")` -- what `_assign_jira_number` actually calls -- returns the tuple element
    `SERVED_ID` captured when `backlot.store` was first imported, a bound reference to the
    function object, not a live attribute lookup (see the github/hubspot/confluence tests above
    for the same defect with `monkeypatch.setattr`).
    """
    from backlot.importer import byo
    from tests._helpers import build_corpus

    monkeypatch.setitem(store.SERVED_ID, "jira", ("served_number", lambda doc_id: 7, "project"))
    docs = [
        {
            "source_type": "jira",
            "doc_id": f"j{i}",
            "project": "payments",
            "title": f"Issue {i}",
            "content": "x",
            "author_email": "a@acme.com",
        }
        for i in range(5)
    ]
    s = build_corpus(tmp_path, docs)
    monkeypatch.undo()
    conn = store.connect_ro(s.db_path)
    issues = {
        r["doc_id"]: r["served_number"]
        for r in conn.execute("SELECT doc_id, served_number FROM jira_issues")
    }
    # The collapsed seed actually landed -- j0, the first row processed (before anything is
    # taken), is served exactly the forced value, not some real hash -- AND resolved to 5 DISTINCT
    # suffixes, otherwise the collision below never actually happened and the rest of this test
    # passed for the wrong reason.
    assert issues["j0"] == 7
    assert len(issues) == 5 and len(set(issues.values())) == 5
    for doc_id, n in issues.items():
        assert store.jira_by_served_number(conn, "payments", n)["doc_id"] == doc_id
    conn.close()

    # A re-import (e.g. --append re-running over the same shard) must not renumber an issue a
    # client may already hold a url for -- more load-bearing here than for confluence's/hubspot's
    # own version of this check: a probed suffix is not a pure function of doc_id, and jira's
    # assignment is not even memoized as it runs -- resolve_jira_numbers rebuilds its whole
    # taken-set fresh every call, so ONLY seed_tracker_ids' preload stands between a replay and a
    # fully reshuffled project.
    byo.load(s.data_dir / "_corpus.jsonl", s, reset=False)
    conn = store.connect_ro(s.db_path)
    replay = {
        r["doc_id"]: r["served_number"]
        for r in conn.execute("SELECT doc_id, served_number FROM jira_issues")
    }
    assert replay == issues
    conn.close()


def test_jira_by_served_number_applies_the_acl(tmp_path):
    """A regression `notion_by_served_id` shipped without (#51 review round), and every reader
    since has had to guard against: a non-empty ``visible_ids`` that grants nothing must come back
    None, not the unscoped row -- otherwise the ACL clause could be deleted from
    `jira_by_served_number` invisibly. A non-empty set, so `_acl_clause` takes its EXISTS branch
    rather than the empty-set "AND 0" short-circuit."""
    from tests._helpers import tiny_corpus

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "jira",
                "doc_id": "j0",
                "project": "payments",
                "title": "Issue 0",
                "content": "x",
                "author_email": "a@acme.com",
            }
        ],
    )
    conn = store.connect_ro(s.db_path)
    served = conn.execute("SELECT served_number FROM jira_issues").fetchone()["served_number"]
    assert store.jira_by_served_number(conn, "payments", served)["doc_id"] == "j0"
    assert store.jira_by_served_number(conn, "payments", served, visible_ids={"nobody"}) is None
    conn.close()


def test_jira_by_served_number_is_scoped_to_its_project(tmp_path, monkeypatch):
    """(review round 1, I-2) The SAME suffix in two different projects is the NORMAL case here,
    not an exotic one -- `(project, served_number)` is the whole UNIQUE index's scope, precisely
    because a suffix is unique only WITHIN its project (see store.SERVED_ID's `scope` for jira).
    `jira_by_served_number`'s `WHERE project = ? AND {col} = ?` is therefore the ONLY thing that
    keeps two same-numbered issues in different projects from resolving to each other -- there is
    no other predicate backing it up.

    Forces the collision directly via a collapsed seed (`monkeypatch.setitem`, not
    `monkeypatch.setattr` -- see the collision test above for why), rather than hoping two
    independent hashes coincide."""
    from tests._helpers import build_corpus

    monkeypatch.setitem(store.SERVED_ID, "jira", ("served_number", lambda doc_id: 7, "project"))
    s = build_corpus(
        tmp_path,
        [
            {
                "source_type": "jira",
                "doc_id": "pay0",
                "project": "payments",
                "title": "Payments issue",
                "content": "x",
                "author_email": "a@acme.com",
            },
            {
                "source_type": "jira",
                "doc_id": "dev0",
                "project": "devtools",
                "title": "Devtools issue",
                "content": "x",
                "author_email": "a@acme.com",
            },
        ],
    )
    monkeypatch.undo()
    conn = store.connect_ro(s.db_path)
    served = {
        r["doc_id"]: r["served_number"]
        for r in conn.execute("SELECT doc_id, served_number FROM jira_issues")
    }
    # Sanity: the collision was actually forced -- both rows share the same suffix, in DIFFERENT
    # projects, otherwise the assertions below would pass even with the scope missing entirely.
    assert served["pay0"] == served["dev0"] == 7
    assert store.jira_by_served_number(conn, "payments", 7)["doc_id"] == "pay0"
    assert store.jira_by_served_number(conn, "devtools", 7)["doc_id"] == "dev0"
    conn.close()


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


def test_linear_served_ids_are_stored_and_resolve(tmp_path, monkeypatch):
    """`served_id` is the UUID half of `issue(id:)` (#51), distinct from `identifier` -- the OTHER
    half, which stays a plain lookup index because it is not unique (see the schema comment on
    idx_linear_doc_ident / idx_linear_identifier). Like gmail's, no probe: synth.linear_id draws
    from `_uuid_from`'s full digest space, so the seed is stored as-is, and a forced collision
    must fail the import loudly rather than resolve through the shared upsert.

    `monkeypatch.setitem` on `store.SERVED_ID`, not `monkeypatch.setattr(synth, ...)` -- the
    registry captures the seed function object at `backlot.store` import time, so patching the
    module attribute afterward cannot reach it (see the confluence/gmail tests above)."""
    from tests._helpers import build_corpus

    docs = [
        {
            "source_type": "linear",
            "team": "engineering",
            "doc_id": f"i{i}",
            "title": f"Issue {i}",
            "content": "x",
            "author_email": "a@acme.com",
        }
        for i in range(3)
    ]
    s = build_corpus(tmp_path / "ok", docs)
    conn = store.connect_ro(s.db_path)
    rows = conn.execute("SELECT doc_id, served_id FROM linear_issues").fetchall()
    assert len(rows) == 3
    assert {r["served_id"] for r in rows} == {synth.linear_id(r["doc_id"]) for r in rows}
    for r in rows:
        assert store.linear_by_served_id(conn, r["served_id"])["doc_id"] == r["doc_id"]
    # A non-empty, non-matching visible_ids must still exclude the row -- proving _acl_clause
    # takes its EXISTS branch here rather than the empty-set "AND 0" short-circuit (which every
    # one of these public rows would otherwise pass regardless of who's asking).
    served = rows[0]["served_id"]
    assert store.linear_by_served_id(conn, served, visible_ids={"nobody"}) is None
    assert store.linear_by_served_id(conn, served, visible_ids=None) is not None  # admin bypass
    conn.close()

    monkeypatch.setitem(store.SERVED_ID, "linear", ("served_id", lambda doc_id: "dup-uuid", None))
    with pytest.raises(sqlite3.IntegrityError):
        build_corpus(tmp_path / "collide", docs)


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


@pytest.mark.parametrize(
    "order",
    [("night-shift", "north-star"), ("north-star", "night-shift")],
    ids=["insertion-agrees-with-name-order", "insertion-disagrees-with-name-order"],
)
def test_linear_team_served_ids_are_stored_and_resolve(tmp_path, monkeypatch, order):
    """`served_id` (the team UUID) and `served_key` (its short key, e.g. "ENG") are the OTHER two
    spellings `team(id:)` accepts, alongside the container's own raw name -- already a plain
    primary-key lookup (`get_container`). Both are written at import now (#51), not rebuilt into a
    startup reverse map by `main._build_index`.

    `synth.linear_team_key` is NOT injective: "night-shift" and "north-star" both reduce to "NS".
    `served_key` therefore carries no UNIQUE index, and the tie must break by team NAME order --
    the same order `store.list_containers` returns and `main._build_index`'s old `setdefault` loop
    walked -- so a key that used to resolve to one team keeps resolving to that same team.

    Parametrized over BOTH insertion orders on purpose: with a fixed insertion order the two
    happen to coincide (rowid order already matches name order), so a query with no `ORDER BY`
    at all would still pass by accident. Reversing insertion order breaks that coincidence and
    would have caught it."""
    from tests._helpers import build_corpus

    assert synth.linear_team_key("night-shift") == synth.linear_team_key("north-star") == "NS"
    docs = [
        {
            "source_type": "linear",
            "team": t,
            "doc_id": f"{t}-1",
            "title": "x",
            "content": "x",
            "author_email": "a@acme.com",
        }
        for t in order
    ]
    s = build_corpus(tmp_path / "-".join(order), docs)
    conn = store.connect_ro(s.db_path)
    rows = {
        r["team"]: r for r in conn.execute("SELECT team, served_id, served_key FROM linear_teams")
    }
    assert rows["night-shift"]["served_id"] == synth.linear_team_id("night-shift")
    assert rows["north-star"]["served_id"] == synth.linear_team_id("north-star")
    assert rows["night-shift"]["served_key"] == rows["north-star"]["served_key"] == "NS"

    assert store.linear_team_by_served_id(conn, rows["night-shift"]["served_id"]) == "night-shift"
    assert store.linear_team_by_served_id(conn, rows["north-star"]["served_id"]) == "north-star"
    # The collision: both reduce to "NS", and the FIRST by name ("night-shift" < "north-star")
    # must keep winning, REGARDLESS of which one was inserted first.
    assert store.linear_team_by_served_key(conn, "NS") == "night-shift"
    assert store.linear_team_by_served_key(conn, "nope") is None
    conn.close()

    # No probe (#51): `synth.linear_team_id` draws from `_uuid_from`'s full digest space, so the
    # raw seed is stored as-is -- a forced collision must fail the import loudly through
    # `idx_linear_teams_served`, not silently let one team's row overwrite the other's.
    monkeypatch.setattr(synth, "linear_team_id", lambda name: "dup-team-uuid")
    with pytest.raises(sqlite3.IntegrityError):
        build_corpus(tmp_path / ("collide-" + "-".join(order)), docs)


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
