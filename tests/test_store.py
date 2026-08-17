"""Tests for the read-only SQLite store layer (`backlot.store`).

The store is shared by every router, search, and the importers, so it gets its own file rather
than being verified incidentally through a load/route test. Registry wiring is checked across
every source; generic reads run against the shared SAMPLE corpus (the `db` fixture); connection
tuning uses hand-built / SAMPLE DBs.

ACL-filtered reads live in test_acl.py (the ACL is the subject there) and FTS search in
test_search.py (search is its own sub-domain); this file covers the plain store surface.
"""

import json
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
        assert store.id_columns(src)  # source -> the columns it is ADDRESSED by resolves


def test_id_columns_use_each_vendors_own_name():
    """The column holding a row's identity is spelled the way its VENDOR spells it, not forced to a
    uniform `id`: a real GitHub issue carries both an `id` and a `number` and the API addresses it
    by the number, a Jira issue by its `key`, a Slack message by its `ts`. Renaming any of those to
    `id` would serve a field the vendor's own client never asks for.

    Three are PAIRS, and that is the vendor's own rule rather than an over-constraint: an S3 key is
    unique only within its bucket (the same key in two buckets is ordinary), a Slack ts only within
    its channel, a GitHub number only within its repo. A Jira key, by contrast, is unique across a
    whole site, so it stands alone. Every entry is a tuple so the ACL tables, the FTS indexes and
    `get_document` stay n-ary uniformly instead of carrying a special case for the pairs."""
    assert store.id_columns("github") == ("repo", "number")
    assert store.id_columns("jira") == ("key",)
    assert store.id_columns("slack") == ("channel", "ts")
    assert store.id_columns("s3") == ("bucket", "key")
    # Everything else serves an opaque id and calls it `id`.
    for src in ("gmail", "google_drive", "confluence", "notion", "hubspot", "linear", "fireflies"):
        assert store.id_columns(src) == ("id",), src
    # The row-distinguishing column on its own — the last of the pair, and the whole key for the
    # nine sources whose id needs no container to be unambiguous.
    assert store.id_column("github") == "number"
    assert store.id_column("slack") == "ts"
    assert store.id_column("gmail") == "id"


def _pk_columns(conn, tbl: str) -> list[str]:
    """A table's PRIMARY KEY, in key order (`PRAGMA table_info`'s `pk` is a 1-based position)."""
    cols = list(conn.execute(f"PRAGMA table_info({tbl})"))
    return [c["name"] for c in sorted((c for c in cols if c["pk"]), key=lambda c: c["pk"])]


def test_no_table_stores_the_datasets_own_identifier(tmp_path):
    """The rule, at the schema level: the dataset's identifier scheme must not outlive the import.
    A `dsid_…` (or a corpus-authored `doc_id`) is an INPUT — it seeds a synthesized id and
    is then discarded — so after import a row is addressed by the id the API serves, and by nothing
    else.

    Renaming `doc_id` to `id` would keep the dataset's scheme, only spelled differently, so the
    check is textual as well as structural: a column named `doc_id` anywhere is the defect."""
    conn = store.connect_rw(tmp_path / "ids.sqlite")
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        # Sanity: the schema really was created, or every assertion below is vacuous.
        assert set(store.SOURCE_TABLE.values()) <= set(tables)

        assert [
            t
            for t in tables
            if any(c["name"] == "doc_id" for c in conn.execute(f"PRAGMA table_info({t})"))
        ] == []

        for src, tbl in store.SOURCE_TABLE.items():
            # Keyed on exactly the id it serves...
            assert _pk_columns(conn, tbl) == list(store.id_columns(src)), src
            # ...and that id comes FIRST. Today's columns sit in the order they were appended in;
            # a table rewritten from scratch leads with its identifier, then its container.
            idc = list(store.id_columns(src))
            declared = [c["name"] for c in conn.execute(f"PRAGMA table_info({tbl})")]
            assert declared[: len(idc)] == idc, src
            # ...then the container — unless the container IS part of the identifier, as it is for
            # the three sources whose id is only unique within one (slack, s3, github).
            assert store.grouping_col(src) in declared[: len(idc) + 1], src

        for src, tbl in store.ACL_TABLE.items():
            # A grant names the document by the same served id, so an ACL row and the row it
            # governs cannot be keyed on two different identifier schemes.
            idc = list(store.id_columns(src))
            assert _pk_columns(conn, tbl)[: len(idc)] == idc, src
    finally:
        conn.close()


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


def test_id_seed_registry_covers_every_source_whose_id_is_a_1_arity_hash():
    """`ID_SEED` holds the sources whose served id is a pure function of ONE value — the incoming
    record's own dataset identifier — so a single assignment method can seed and probe all of them.
    It is deliberately NOT total over `SOURCE_TABLE`; three sources cannot honour that contract and
    get their own assignment pass instead:

    - `jira`'s key is COMPOSED from its project's prefix, so its seed is (project, dataset id).
    - `slack`'s ts is a function of the row's `created_ts` and its thread root as well.
    - `s3`'s (bucket, key) is stated outright by the corpus; nothing synthesizes it at all.

    Widening the tuple to fit those would make every other source pay for their shape — the same
    call `fireflies_users` and `linear_teams` made when they were converted."""
    assert set(store.ID_SEED) == set(store.SOURCE_TABLE) - {"jira", "slack", "s3"}
    # Confluence's own entry -- seed function, then the columns its probe holds fixed (none:
    # confluence resolves an id corpus-wide). The column the seed FILLS is not in this registry:
    # it is the table's primary key, which `id_columns` already names.
    assert store.ID_SEED["confluence"] == (synth.confluence_id, None)
    assert store.id_seed("confluence") is synth.confluence_id
    # github is the one entry with a scope: a number is unique only within its repo.
    assert store.id_seed_scope("github") == "repo"
    assert store.id_seed_scope("gmail") is None


def test_no_blanket_replace_writes_a_table_with_a_non_pk_unique_index(tmp_path):
    """`INSERT OR REPLACE` has no scoped conflict target: on ANY unique-index violation, not only the
    PRIMARY KEY, it resolves the conflict by silently DELETING the other row. So for a table whose
    PRIMARY KEY is a business key (a doc_id, a team, an email) but which ALSO carries an unrelated
    unique-indexed served id, a served-id collision destroys the other row instead of failing the
    import — the failure this whole scheme exists to remove.

    A `grep 'INSERT OR REPLACE INTO <table>'` cannot catch this. Every shared write path here names
    its target through an f-string variable (`{store.table(src)}`, `{ctable}`, `{gtable}`), so a
    search for a literal table name never matches them whether the bug is present or not. This
    checks each path on its own terms:

    - The doc tables (`SOURCE_TABLE`) and comment tables (`COMMENT_TABLE`) are structurally safe:
      `insert` upserts on `ON CONFLICT(<the served key>) DO UPDATE`, and the comment insert excludes
      `served_id` from its column list. Not re-verified here.
    - `write_containers`' `else` branch runs one blanket `INSERT OR REPLACE` for every `GROUPING`
      table it does not special-case (currently only `linear`). A grouping table that gains a non-PK
      unique index without joining that set is caught here at the SCHEMA level.
    - A table with its own one-off write (currently just `fireflies_users`) is the one case a
      literal-name grep is reliable for, since such a write has no reason to interpolate its name.

    Whether `linear`'s and `fireflies_users`' own writes are CORRECT is a different question, left to
    the `pytest.raises(sqlite3.IntegrityError)` blocks in
    `test_linear_team_served_ids_are_stored_and_resolve` and
    `test_fireflies_users_is_the_workspace_roster_not_every_named_person`. This guard's job is to
    catch a table that fell through every mechanism the schema trusts."""
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
    # Sanity: the guard must find at least these two, or the PRAGMA-based detection above is
    # broken and every assertion below is vacuous.
    assert {"linear_teams", "fireflies_users"} <= guarded

    # `write_containers`: every grouping table except its declared special cases must have NO
    # non-PK unique index, or its generic `else` branch's blanket OR REPLACE reaches it.
    # `jira` joined `linear` here when jira_projects gained its own `key` column and its UNIQUE
    # index -- this guard is what flagged that the generic branch
    # would have reached it, and it now has its own scoped upsert in write_containers.
    write_containers_special_cases = {"linear", "jira"}
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
    the wrong rows coming back.

    A raise, not an assert: `python -O` drops an assert, and this guard has no observable failure
    to fall back on."""
    with pytest.raises(ValueError):
        store._acl_clause("slack", "gmail_messages", {"p1"})
    # An alias (not a real table name) must not trip the check — the Linear relation readers
    # legitimately pass "i"/"a"/"b" as `tbl`.
    store._acl_clause("slack", "i", {"p1"})
    # tbl=None (the default) resolves to the source's own table and must not raise.
    store._acl_clause("slack", visible_ids={"p1"})


# --- generic reads over the SAMPLE corpus ---------------------------------------


def test_get_document(db, keys):
    doc = store.get_document(db, "confluence", *keys["cf-handbook"])
    assert doc["title"] == "Engineering Handbook"
    assert store.get_document(db, "confluence", 1) is None


def test_list_documents_container_scope(db, keys):
    got = {(r["key"],) for r in store.list_documents(db, "jira", container="payments", limit=100)}
    assert {keys["jira-sev2"], keys["jira-sub1"], keys["jira-private"]} <= got
    assert store.list_documents(db, "jira", container="no-such-project", limit=100) == []


def test_list_documents_author_scope(db):
    rows = store.list_documents(db, "confluence", author_email="ava@acme.com", limit=100)
    assert rows and all(r["author_email"] == "ava@acme.com" for r in rows)


def test_count_documents(db):
    assert store.count_documents(db, "jira", container="payments") >= 3
    assert store.count_documents(db, "confluence") >= 3
    assert store.count_documents(db, "jira", container="no-such-project") == 0


def test_list_documents_state_filter(db, keys):
    # gateway repo: gh-issue-1 is open (state NULL/"open"), gh-pr-1 is closed
    def listed(**kw):
        return {
            (r["repo"], r["number"])
            for r in store.list_documents(db, "github", container="gateway", limit=100, **kw)
        }

    issue, pull = keys["gh-issue-1"], keys["gh-pr-1"]
    assert issue in listed(state="open") and pull not in listed(state="open")
    assert pull in listed(state="closed") and issue not in listed(state="closed")
    # state=None (default) applies no filter -> both present
    assert {issue, pull} <= listed()


def test_count_documents_state_filter(db):
    assert store.count_documents(db, "github", container="gateway", state="open") == 1
    assert store.count_documents(db, "github", container="gateway", state="closed") == 1
    assert store.count_documents(db, "github", container="gateway") >= 2


def test_children(db, keys):
    # jira-sub1 is a subtask of jira-sev2; cf-oncall is a child page of cf-handbook
    assert keys["jira-sub1"] in {
        (r["key"],) for r in store.children(db, "jira", *keys["jira-sev2"])
    }
    assert keys["cf-oncall"] in {
        (r["id"],) for r in store.children(db, "confluence", *keys["cf-handbook"])
    }


def test_doc_comments(db, keys):
    cmts = store.doc_comments(db, "confluence", *keys["cf-oncall"])
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
    # `fireflies_users` is written at import, one row per user principal -- a unique-indexed lookup.
    assert store.fireflies_user_by_served_id(db, synth.fireflies_user_id("ava@acme.com")) == (
        "ava@acme.com"
    )
    assert store.fireflies_user_by_served_id(db, "not-a-real-served-id") is None
    # The table can drift from `principals`, which a map rebuilt from it per boot could not: every
    # `type='user'` principal must have EXACTLY one `fireflies_users` row, or `user(id:)` either
    # serves a fabricated user for an orphan row, or nulls out an id `users` just listed.
    user_principals = {r["id"] for r in db.execute("SELECT id FROM principals WHERE type = 'user'")}
    ff_users = {r[0] for r in db.execute("SELECT email FROM fireflies_users")}
    assert user_principals == ff_users


def test_jcol_parses_json_columns(db, keys):
    issue = store.get_document(db, "github", *keys["gh-issue-1"])
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
    for _label, bucket, key in rows:
        conn.execute(
            "INSERT INTO s3_objects(bucket, key, author_email, title, content, created_ts) "
            "VALUES (?,?,?,?,?,1)",
            (bucket, key, "a@x.com", key, "body"),
        )
    # One object is ACL-restricted to group 'eng'; everything else is unrestricted (no s3_acl row
    # -> _acl_clause's EXISTS check only bites rows it has an entry for). An s3 grant names its
    # object by (bucket, key), the same pair the table is keyed on.
    conn.execute("INSERT INTO s3_acl VALUES ('b','logs/2026/01/b.json','group','eng')")
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
    # The PRIMARY KEY's own index IS (bucket, key) now, so the range seek rides it
    # directly rather than a separate idx_s3_key.
    assert "USING INDEX sqlite_autoindex_s3_objects_1" in detail
    assert "LIKE" not in detail.upper()
    flat = detail.replace(" ", "")
    assert "key>" in flat and "key<" in flat


def test_list_s3_objects_prefix_case_sensitive(tmp_path):
    """Fix 2 (correctness): a direct consequence of the byte-range prefix filter (BINARY
    collation) — prefix matching is case-SENSITIVE, matching real S3's byte-exact semantics.
    Objects live only under the lowercase "logs/" prefix; an uppercase "LOGS/" prefix query must
    not match them (a case-insensitive LIKE would wrongly match)."""
    conn = store.connect_rw(tmp_path / "s3_case.sqlite")
    for key in ("logs/a.json", "logs/b.json"):
        conn.execute(
            "INSERT INTO s3_objects(bucket, key, author_email, title, content, created_ts) "
            "VALUES (?,?,?,?,?,1)",
            ("b", key, "a@x.com", key, "body"),
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
    assert store.s3_by_bucket_key(conn, "b", "logs/2026/01/a.json")["key"] == "logs/2026/01/a.json"
    # the trap: key alone is not unique -- the same key in a different bucket is a different row
    assert store.s3_by_bucket_key(conn, "other", "logs/2026/01/a.json")["bucket"] == "other"
    assert store.s3_by_bucket_key(conn, "b", "nope") is None
    # d2 ("logs/2026/01/b.json") is ACL-restricted to group 'eng'; a non-empty visible_ids
    # granting nothing must still 404 it (runs _acl_clause's EXISTS branch, not its AND 0
    # short-circuit), while the granted group finds it.
    assert store.s3_by_bucket_key(conn, "b", "logs/2026/01/b.json", visible_ids={"nobody"}) is None
    found = store.s3_by_bucket_key(conn, "b", "logs/2026/01/b.json", visible_ids={"eng"})
    assert found["key"] == "logs/2026/01/b.json"


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
            "INSERT INTO gdrive_files(id, folder, author_email, title, content, subtype, "
            "created_ts, trashed) VALUES (?,?,?,?,?,?,1,?)",
            # `id` is the primary key; nothing here reads it back, so the fixture's own
            # label serves as one rather than the real synth.gdrive_file_id — this DB is
            # hand-built to bypass the importer.
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
    for record_id, object_type, title, properties in rows:
        conn.execute(
            "INSERT INTO hubspot_objects(id, object_type, author_email, title, content, "
            "properties, created_ts) VALUES (?,?,?,?,?,?,1)",
            # `id` is the primary key; the fixture's own label serves as one rather than the
            # real synth.hubspot_record_id, since this DB is hand-built to bypass the importer.
            (record_id, object_type, "owner@x.com", title, title, properties),
        )
    # Both contacts belong to the company; the deal is associated with the company too.
    # Real HubSpot associations are bidirectional with a distinct type id per direction, so a row
    # is stored per direction and a lookup is a plain (from_id, to_type) match.
    for from_doc, from_type, to_doc, to_type in [
        ("c1", "contacts", "co1", "companies"),
        ("c2", "contacts", "co1", "companies"),
        ("d1", "deals", "co1", "companies"),
        ("co1", "companies", "c1", "contacts"),
        ("co1", "companies", "c2", "contacts"),
        ("co1", "companies", "d1", "deals"),
    ]:
        conn.execute(
            "INSERT INTO hubspot_associations(from_id, from_type, to_id, to_type, "
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
    assert [r["id"] for r in store.list_documents(conn, "hubspot", container="contacts")] == [
        "c1",
        "c2",
    ]
    assert [r["id"] for r in store.list_documents(conn, "hubspot", container="deals")] == ["d1"]


def test_hubspot_associations_from_a_record(tmp_path):
    conn = _hubspot_mini_db(tmp_path)
    rows = store.hubspot_associations(conn, "c1", "companies")
    assert [r["to_id"] for r in rows] == ["co1"]
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
    assert [r["to_id"] for r in store.hubspot_associations(conn, "co1", "contacts")] == [
        "c1",
        "c2",
    ]
    # a caller with only 'everyone' cannot read c2, so that association is not revealed
    rows = store.hubspot_associations(conn, "co1", "contacts", visible_ids={"everyone"})
    assert [r["to_id"] for r in rows] == ["c1"]
    # a caller in 'sales' reads c2 but not c1
    rows = store.hubspot_associations(conn, "co1", "contacts", visible_ids={"sales"})
    assert [r["to_id"] for r in rows] == ["c2"]


# --- connection tuning ----------------------------------------------------------


def test_connect_rw_busy_timeout(tmp_path):
    c = store.connect_rw(tmp_path / "rw.sqlite", busy_ms=12345)
    try:
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 12345
    finally:
        c.close()


def test_connect_rw_fresh_db_still_works(tmp_path):
    conn = store.connect_rw(tmp_path / "fresh.sqlite")
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(github_items)")}
        assert {"path", "changed_paths"} <= cols
        ccols = {r[1] for r in conn.execute("PRAGMA table_info(github_comments)")}
        assert {"path", "line", "diff_hunk"} <= ccols
    finally:
        conn.close()


def _write_pre_acl_db(p):
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE doc_acl (doc_id TEXT NOT NULL, principal_type TEXT NOT NULL, "
        "principal_id TEXT NOT NULL, PRIMARY KEY (doc_id, principal_type, principal_id))"
    )
    conn.commit()
    conn.close()


def _write_pre_served_columns_db(p):
    conn = sqlite3.connect(p)
    # A hand-rolled DB keyed on the dataset's own `doc_id`. That column IS the signal, so one
    # check covers every table.
    conn.execute(
        "CREATE TABLE jira_issues (doc_id TEXT PRIMARY KEY, project TEXT NOT NULL, "
        "author_email TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL, "
        "status TEXT, issuetype TEXT, priority TEXT, labels TEXT, components TEXT, "
        "issuelinks TEXT, parent_id TEXT, changelog TEXT, created_ts INTEGER NOT NULL, "
        "updated_ts INTEGER, assignee_email TEXT, reporter_email TEXT, resolution TEXT, "
        "resolution_ts INTEGER, duedate TEXT, fix_versions TEXT, severity TEXT, squad TEXT, "
        "owner_display TEXT, key TEXT)"
    )
    conn.execute("CREATE TABLE linear_teams (team TEXT PRIMARY KEY, group_id TEXT)")
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    "setup, match",
    [
        (_write_pre_acl_db, "doc_acl"),
        (_write_pre_served_columns_db, "doc_id"),
    ],
)
def test_connect_rw_refuses_a_pre_served_db(tmp_path, setup, match):
    """Two DB shapes connect_rw must refuse outright rather than let SCHEMA fail on, or migrate:

    - built before per-source ACL tables (still has one shared `doc_acl`) — appending would
      write a new source's grants into the empty per-source tables SCHEMA creates while every
      pre-existing grant stays behind in `doc_acl`, which nothing reads any more, silently
      hiding every pre-existing document from every scoped token.
    - built before the served-id primary keys (its documents still carry a `doc_id`) —
      `CREATE TABLE IF NOT EXISTS` would not alter the old table at all and `CREATE INDEX IF NOT
      EXISTS` guards only the index's own name, so it raises a bare `OperationalError: no such
      column` naming whichever table SCHEMA's text happens to reach first, saying nothing about
      why. Neither case has a backfill (see connect_rw's own comments on both checks), so the
      only correct move for either is a fresh re-import — which is what both readable errors say,
      instead of a raw SQLite one or a silent migration of ids that were never meant to move."""
    p = tmp_path / "old.sqlite"
    setup(p)

    with pytest.raises(ValueError, match=match):
        store.connect_rw(p)
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
            "INSERT INTO github_comments(id,repo,number,seq,author_email,body,created_ts,"
            "path,line) VALUES(?,'gw',1,?,'a@x',?,1,?,?)",
            (cid, seq, body, path, line),
        )
    conn.commit()
    assert [c["id"] for c in store.github_comments(conn, "gw", 1)] == ["c1", "c2", "c3"]
    assert [c["id"] for c in store.github_comments(conn, "gw", 1, anchored=False)] == ["c1"]
    anchored = store.github_comments(conn, "gw", 1, anchored=True)
    assert [c["id"] for c in anchored] == ["c2", "c3"]
    assert (anchored[0]["path"], anchored[0]["line"]) == ("src/a.py", 12)


def test_confluence_served_ids_are_unique_even_when_the_seed_collides(tmp_path, monkeypatch):
    """`synth.confluence_id` draws from 9,000,000 values, so a large space collides by the birthday
    bound — ~222 in a 60,000-page corpus, and a collision leaves one page unreachable by its own
    id. Forced here by collapsing the seed.

    Patches `store.ID_SEED` directly, NOT `synth.confluence_id` (`monkeypatch.setattr(byo.synth,
    "confluence_id", ...)`, the original shape of this test): `store.id_seed("confluence")`
    — what `_assign_confluence_id` actually calls — returns the tuple element `ID_SEED` captured
    when `backlot.store` was first imported, a bound reference to the function object, not a live
    attribute lookup. Patching the module attribute afterward cannot reach it — verified: it left
    every page with a distinct, real hash, so the collision below never actually happened and the
    assertions passed for the wrong reason. `monkeypatch.setitem` replaces the tuple the accessor
    actually reads.
    """
    from backlot.importer import byo
    from tests._helpers import build_corpus

    monkeypatch.setitem(store.ID_SEED, "confluence", (lambda doc_id: 7, None))
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
    served = [r["id"] for r in conn.execute("SELECT id FROM confluence_pages")]
    assert len(served) == 5 and len(set(served)) == 5 and all(served)
    # The collapsed seed actually landed: p0 (the first page processed, before anything is taken)
    # is served exactly 7, the forced value -- not some real hash. If this drifts back to a real
    # hash, the patch has gone inert again, silently, the same way it did before.
    assert store.confluence_by_id(conn, 7)["title"] == "Page 0"
    for sid in served:
        assert store.confluence_by_id(conn, sid)["id"] == sid
    conn.close()

    # A re-import is REFUSED. confluence and hubspot are probed like github and jira, so a
    # re-imported row that stated no identity would be recomputed, land on a different id and
    # duplicate in silence -- an append that omits `content_id` / `record_id` is refused instead,
    # the same rule the other two probed sources follow.
    with pytest.raises(SystemExit, match="must carry"):
        byo.load(s.data_dir / "_corpus.jsonl", s, reset=False)
    conn = store.connect_ro(s.db_path)
    assert sorted(r["id"] for r in conn.execute("SELECT id FROM confluence_pages")) == sorted(
        served
    ), "the refused append left every id exactly as the first import assigned it"
    conn.close()


def test_confluence_by_id_applies_the_acl(tmp_path):
    """A regression `notion_by_id` shipped without, and every reader
    since has had to guard against: a non-empty ``visible_ids`` that grants nothing must come back
    None, not the unscoped row -- otherwise the ACL clause could be deleted from
    `confluence_by_id` invisibly. One of a family, with `test_hubspot_by_id_applies_the_acl`,
    `test_github_by_number_applies_the_acl` and `test_jira_by_served_number_applies_the_acl`.
    A non-empty set, so `_acl_clause` takes its EXISTS branch rather than the empty-set "AND 0"
    short-circuit."""
    from tests._helpers import tiny_corpus

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "space": "wiki",
                "doc_id": "p0",
                "title": "Page 0",
                "content": "x",
                "author_email": "a@acme.com",
            }
        ],
    )
    conn = store.connect_ro(s.db_path)
    served = conn.execute("SELECT id FROM confluence_pages").fetchone()["id"]
    assert store.confluence_by_id(conn, served)["title"] == "Page 0"
    assert store.confluence_by_id(conn, served, visible_ids={"nobody"}) is None
    conn.close()


def test_gmail_served_ids_are_stored_and_resolve(tmp_path, monkeypatch):
    """Gmail's id space is 2**63, so unlike confluence it does not probe: the seed is stored as-is.
    Keeping it a pure hash is what lets `_gmail_ids` derive `threadId` by re-hashing the root's key
    instead of reading the root's row.

    The second half forces the collision gmail's design accepts as the tradeoff for staying
    unprobed: a duplicate `served_id` MUST fail the import loudly (a UNIQUE index violation), not
    resolve through the shared write path's upsert, which was `INSERT OR REPLACE` and resolved a
    conflict on ANY unique index by silently DELETING the row already holding that value -- a
    message would vanish with no error. `monkeypatch.setitem` on `store.ID_SEED`, not
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
    rows = conn.execute("SELECT id, title FROM gmail_messages").fetchall()
    assert len(rows) == 5
    # the stored key IS the seed of the corpus's own id for that record
    assert {r["id"] for r in rows} == {synth.gmail_message_id(f"m{i}") for i in range(5)}
    for r in rows:
        assert store.gmail_by_id(conn, r["id"])["title"] == r["title"]
    conn.close()

    monkeypatch.setitem(store.ID_SEED, "gmail", (lambda key: "00000000deadbeef", None))
    with pytest.raises(SystemExit, match="already resolves to"):
        build_corpus(tmp_path / "collide", docs)


def test_gdrive_served_ids_are_stored_and_resolve(tmp_path, monkeypatch):
    """Drive was the one document source with no served-id column at all -- it
    served the corpus's own `doc_id` straight through as the file id. Like gmail/notion/linear it
    does not probe: `synth.gdrive_file_id` draws from a 192-bit digest, so the seed is stored as-is
    and a collision is left to the UNIQUE index rather than walked around.

    The second half forces that collision the same way `test_gmail_served_ids_are_stored_and_
    resolve` does: `monkeypatch.setitem` on `store.ID_SEED`, not `monkeypatch.setattr(synth,
    "gdrive_file_id", ...)` -- the registry captures the seed function object at `backlot.store`
    import time, so patching the module attribute afterward cannot reach it (see the confluence
    test above for the same defect). Before this fix there was no served_id column and no index to
    violate -- Drive just served whichever doc_id the URL named, so this collision could not even
    be expressed."""
    from tests._helpers import build_corpus

    docs = [
        {
            "source_type": "google_drive",
            "folder": "eng",
            "doc_id": f"d{i}",
            "title": f"Doc {i}",
            "content": "x",
            "author_email": "a@acme.com",
        }
        for i in range(5)
    ]
    s = build_corpus(tmp_path / "ok", docs)
    conn = store.connect_ro(s.db_path)
    rows = conn.execute("SELECT id, title FROM gdrive_files").fetchall()
    assert len(rows) == 5
    assert {r["id"] for r in rows} == {synth.gdrive_file_id(f"d{i}") for i in range(5)}
    for r in rows:
        assert store.gdrive_by_id(conn, r["id"])["title"] == r["title"]
    conn.close()

    monkeypatch.setitem(store.ID_SEED, "google_drive", (lambda key: "1" + "a" * 32, None))
    with pytest.raises(SystemExit, match="already resolves to"):
        build_corpus(tmp_path / "collide", docs)


def test_notion_served_ids_are_stored_and_resolve(tmp_path, monkeypatch):
    """Notion has TWO synthesized id spaces per row: `served_id` (synth.notion_id), every
    row, like gmail unprobed since synth._uuid_from draws from the full digest space; and
    `served_data_source_id` (synth.notion_data_source_id), the 2025-09-03 API's data source id for
    a DATABASE row only -- real Notion has no data source for a page, so a page's column stays
    NULL rather than colliding with anything.

    `served_data_source_id` is assigned directly from `synth.notion_data_source_id` in
    importer.byo, not through `store.ID_SEED` (that registry stays one column per source, see
    its own comment) -- so forcing ITS collision needs `monkeypatch.setattr(synth, ...)`, the
    opposite of `served_id`'s: `store.ID_SEED`'s tuple captures the seed function object at
    `backlot.store` import time (see the confluence/gmail tests above), so `served_id`'s collision
    needs `monkeypatch.setitem` on the registry, while `synth.notion_data_source_id` is looked up
    live off the module at call time in importer.byo (`from backlot import ... synth`), so patching
    the module attribute reaches it just fine there.

    Also covers: a database demoted to a non-database on a later import of the SAME doc_id (two records sharing one doc_id are both written, the later
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
    rows = {r["id"]: r for r in conn.execute("SELECT id, title, data_source_id FROM notion_pages")}
    page, database = synth.notion_id("p0"), synth.notion_id("db0")
    assert set(rows) >= {page, database}
    assert rows[database]["data_source_id"] == synth.notion_data_source_id("db0")
    # a page never gets a data source id -- real Notion has no query target for a page.
    assert rows[page]["data_source_id"] is None
    assert store.notion_by_id(conn, page)["title"] == rows[page]["title"]
    assert (
        store.notion_by_data_source_id(conn, synth.notion_data_source_id("db0"))["id"] == database
    )
    # the two id spaces don't alias each other -- a page's own id must not also resolve as SOME
    # row's data source id (the reader must query data_source_id, not id).
    assert store.notion_by_data_source_id(conn, page) is None
    # notion_by_data_source_id must apply visible_ids like notion_by_id does -- a
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
        "SELECT subtype, data_source_id FROM notion_pages WHERE title = 'Now a page'"
    ).fetchone()
    assert row["subtype"] != "database"
    assert row["data_source_id"] is None
    assert store.notion_by_data_source_id(conn, synth.notion_data_source_id("flip")) is None
    conn.close()

    monkeypatch.setitem(store.ID_SEED, "notion", (lambda seed: "0" * 32, None))
    with pytest.raises(SystemExit, match="already resolves to"):
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
    measured over synthetic corpora it collides ~16 times at 500k documents. So hubspot follows
    CONFLUENCE's probe-and-walk, not gmail's/notion's bare-seed shape.

    Forced here by collapsing the seed to a CONSTANT -- and the assertion is the OPPOSITE of
    gmail's forced-collision test: gmail's must ABORT the import (no probe -- a duplicate is a
    loud UNIQUE-index failure), while hubspot's must still produce N DISTINCT served_ids, each
    resolving to its own record, because the probe is what absorbs the collision instead of
    surfacing it.

    Patches `store.ID_SEED` directly, NOT `synth.hubspot_record_id`: `store.served_id_seed
    ("hubspot")` -- what `_assign_hubspot_id` actually calls -- returns the tuple element
    `ID_SEED` captured when `backlot.store` was first imported, a bound reference to the
    function object, not a live attribute lookup. `monkeypatch.setattr(synth, ...)` cannot reach
    it (see the confluence/gmail/notion tests above for the same defect); `monkeypatch.setitem`
    replaces the tuple the accessor actually reads.
    """
    from backlot.importer import byo
    from tests._helpers import build_corpus

    monkeypatch.setitem(store.ID_SEED, "hubspot", (lambda doc_id: "1000000000", None))
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
    served = [r["id"] for r in conn.execute("SELECT id FROM hubspot_objects")]
    assert len(served) == 5 and len(set(served)) == 5 and all(served)
    # The collapsed seed actually landed: h0 (the first record processed, before anything is
    # taken) is served exactly the forced value -- not some real hash. If this drifts back to a
    # real hash, the patch has gone inert again, silently, the same way it did before.
    assert store.hubspot_by_id(conn, "1000000000")["title"] == "Co 0"
    for sid in served:
        row = store.hubspot_by_id(conn, sid)
        assert row is not None and row["id"] == sid
    conn.close()

    # A re-import is REFUSED. confluence and hubspot are probed like github and jira, so a
    # re-imported row that stated no identity would be recomputed, land on a different id and
    # duplicate in silence -- an append that omits `content_id` / `record_id` is refused instead,
    # the same rule the other two probed sources follow.
    with pytest.raises(SystemExit, match="must carry"):
        byo.load(s.data_dir / "_corpus.jsonl", s, reset=False)
    conn = store.connect_ro(s.db_path)
    assert sorted(r["id"] for r in conn.execute("SELECT id FROM hubspot_objects")) == sorted(
        served
    ), "the refused append left every id exactly as the first import assigned it"
    conn.close()


def test_hubspot_by_id_applies_the_acl(tmp_path):
    """A regression `notion_by_id` shipped without: a non-empty
    ``visible_ids`` that grants nothing must come back None, not the unscoped row -- otherwise the
    ACL clause could be deleted from `hubspot_by_id` invisibly. A non-empty set, so
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
    served = conn.execute("SELECT id FROM hubspot_objects").fetchone()["id"]
    assert store.hubspot_by_id(conn, served)["title"] == "Co 0"
    assert store.hubspot_by_id(conn, served, visible_ids={"nobody"}) is None


def test_hubspot_associations_carry_the_targets_own_served_id_through_a_collision(
    tmp_path, monkeypatch
):
    """`hubspot_associations` joins to `hubspot_objects` for the target's OWN stored `served_id`
    (as `to_served_id`) rather than leaving the v4 payload to recompute
    `synth.hubspot_record_id(to_doc_id)` the way gmail's `threadId` re-hashes a root row (see
    `routers.google._gmail_ids`). That shortcut is safe for gmail because its seed is never probed
    -- hash and stored column always agree -- but hubspot's IS probed: a collision walk can
    move a record to an id different from its raw hash, and the association payload must report
    the one the target's own row actually resolves at.

    Forced the same way the served-id collision test above does: the seed collapsed to a
    constant, so of the two companies below, whichever is assigned second gets walked away from
    the forced value -- and `to_served_id` must track whichever one that turns out to be, not the
    raw hash.
    """
    from tests._helpers import tiny_corpus

    monkeypatch.setitem(store.ID_SEED, "hubspot", (lambda doc_id: "1000000000", None))
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
    actual = {r["title"]: r["id"] for r in conn.execute("SELECT id, title FROM hubspot_objects")}
    # The collision was actually forced (both start from "1000000000") AND resolved to two
    # distinct values -- otherwise the join below would trivially "match" against a value neither
    # side actually had to walk away from.
    assert len(set(actual.values())) == 2
    rows = store.hubspot_associations(conn, actual["Co 1"], "companies")
    assert rows[0]["to_id"] == actual["Co 0"]
    back = store.hubspot_associations(conn, actual["Co 0"], "companies")
    assert back[0]["to_id"] == actual["Co 1"]


def test_github_numbers_probe_on_a_collision_and_stay_stable_across_a_reimport(
    tmp_path, monkeypatch
):
    """github's served number, like hubspot's/confluence's, is PROBED against a collision, not
    stored as the bare hash: `synth.github_number`'s space is only 1..90,000 PER REPO -- the
    tightest of the probed sources (see store.ID_SEED's `scope`) -- so a real corpus collides far
    short of the birthday bound. Forced here by collapsing the seed to a constant, the same
    shape as the hubspot test above.

    Also covers, in one corpus, the two things specific to github among the probed sources:
    - `kind='file'` rows are excluded from the number space entirely, not merely probed around --
      their number stays NULL, and several coexist under the UNIQUE (repo, number)
      index alongside the 5 resolved issue numbers IN THE SAME REPO, which this import succeeding
      at all already proves (SQLite exempts NULL from a UNIQUE constraint).
    - Stability across a re-import: unlike confluence's/hubspot's assignment (computed inline per
      record, memoized on the `_Loader` as it goes), github's is a DEFERRED pass
      (`resolve_github_numbers`) run once every record has loaded, and it is THAT method's own
      `_github_numbers` memo -- populated by `seed_tracker_ids` before this run's insert can reset
      the column to NULL -- that a replay actually depends on to reproduce the same numbers, not
      the corpus alone.

    Patches `store.ID_SEED` directly, NOT `synth.github_number`: `store.served_id_seed
    ("github")` -- what `_assign_github_number` actually calls -- returns the tuple element
    `ID_SEED` captured when `backlot.store` was first imported, a bound reference to the
    function object, not a live attribute lookup (see the confluence/gmail/notion/hubspot tests
    above for the same defect with `monkeypatch.setattr`).
    """
    from backlot.importer import byo
    from tests._helpers import build_corpus

    monkeypatch.setitem(store.ID_SEED, "github", (lambda seed: 7, "repo"))
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
        r["title"]: r["number"]
        for r in conn.execute("SELECT title, number FROM github_items WHERE kind != 'file'")
    }
    files = [
        r["number"] for r in conn.execute("SELECT number FROM github_items WHERE kind = 'file'")
    ]
    # The collapsed seed actually landed -- g0, the first row processed (before anything is
    # taken), is served exactly the forced value, not some real hash -- AND resolved to 5
    # DISTINCT numbers, otherwise the collision below never actually happened and the rest of
    # this test passed for the wrong reason.
    assert issues["Issue 0"] == 7
    assert len(issues) == 5 and len(set(issues.values())) == 5
    for title, n in issues.items():
        assert store.github_by_number(conn, "core", n)["title"] == title
    # Every file carries a number too, since (repo, number) is the primary key -- assigned AFTER
    # every issue, so none of them took a number an issue wanted.
    assert len(files) == 3 and all(n is not None for n in files)
    assert not set(files) & set(issues.values())
    conn.close()

    # A re-import is REFUSED, and github is the source where that can be enforced: a
    # record may state its own `number`, so an append that omits one is refused rather than
    # adding the row a second time under a freshly probed number. Contrast the confluence and
    # hubspot versions of this check, whose records have no such field and therefore duplicate.
    with pytest.raises(SystemExit, match="must carry `number`"):
        byo.load(s.data_dir / "_corpus.jsonl", s, reset=False)
    conn = store.connect_ro(s.db_path)
    replay = {
        r["title"]: r["number"]
        for r in conn.execute("SELECT title, number FROM github_items WHERE kind != 'file'")
    }
    assert replay == issues, "the refused append left every number as it was"
    conn.close()


def test_github_serves_one_number_column(tmp_path, monkeypatch):
    """`number` holds the number the API serves, whether the corpus wrote it or the import assigned
    it. There is no second column.

    The two were separate so "provided" could mean exactly `number IS NOT NULL` — a distinction the
    old boot-time resolver needed on every start. Assignment moved to import time, where provenance
    comes from the incoming record rather than from a column, so at the moment the deferred pass
    runs "provided" means precisely "already non-NULL" and the second column has nothing left to
    say. See `docs/superpowers/plans/2026-08-14-identifier-consolidation.md`.

    The seed is collapsed onto the number a sibling PROVIDED, so the probe has to move the keyless
    row off it — proving the provided value wins its spelling and both kinds share one column.
    """
    from tests._helpers import build_corpus

    cols = {
        r[1]
        for r in store.connect_rw(tmp_path / "s.sqlite").execute("PRAGMA table_info(github_items)")
    }
    assert "number" in cols and "served_number" not in cols and "doc_id" not in cols
    assert store.id_column("github") == "number"

    monkeypatch.setitem(store.ID_SEED, "github", (lambda seed: 7, "repo"))
    s = build_corpus(
        tmp_path / "c",
        [
            {
                "source_type": "github",
                "doc_id": "g-provided",
                "repo": "core",
                "number": 7,
                "title": "provided",
                "content": "x",
                "author_email": "a@acme.com",
            },
            {
                "source_type": "github",
                "doc_id": "g-derived",
                "repo": "core",
                "title": "derived",
                "content": "x",
                "author_email": "a@acme.com",
            },
        ],
    )
    monkeypatch.undo()
    conn = store.connect_ro(s.db_path)
    got = {r["title"]: r["number"] for r in conn.execute("SELECT title, number FROM github_items")}
    assert got["provided"] == 7, "the corpus's number is served verbatim"
    assert got["derived"] != 7, "the keyless row must move off the taken spelling"
    for title, num in got.items():
        assert store.github_by_number(conn, "core", num)["title"] == title
    conn.close()


def test_github_by_number_applies_the_acl(tmp_path):
    """A regression `notion_by_id` shipped without, and every reader
    since has had to guard against: a non-empty ``visible_ids`` that grants nothing must come back
    None, not the unscoped row -- otherwise the ACL clause could be deleted from
    `github_by_number` invisibly. A non-empty set, so `_acl_clause` takes its EXISTS branch
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
    served = conn.execute("SELECT number FROM github_items").fetchone()["number"]
    assert store.github_by_number(conn, "core", served)["title"] == "Issue 0"
    assert store.github_by_number(conn, "core", served, visible_ids={"nobody"}) is None
    conn.close()


def test_github_by_number_is_scoped_to_its_repo(tmp_path, monkeypatch):
    """(final review, I-2) The SAME number in two different repos is the NORMAL case here, not an
    exotic one -- GitHub numbers restart per repo, and `(repo, number)` is the whole UNIQUE
    index's scope (see store.ID_SEED's `scope` for github, and idx_github_served).
    `github_by_number`'s `WHERE repo = ? AND {col} = ?` is therefore the ONLY thing that
    keeps two same-numbered issues in different repos from resolving to each other -- there is no
    other predicate backing it up. jira's byte-identical predicate already has this sibling
    (`test_jira_by_served_number_is_scoped_to_its_project`); github never got its own.

    Forces the collision directly via a collapsed seed (`monkeypatch.setitem`, not
    `monkeypatch.setattr` -- see the collision test above for why), rather than hoping two
    independent hashes coincide."""
    from tests._helpers import build_corpus

    monkeypatch.setitem(store.ID_SEED, "github", (lambda seed: 7, "repo"))
    s = build_corpus(
        tmp_path,
        [
            {
                "source_type": "github",
                "doc_id": "a0",
                "repo": "alpha",
                "title": "gh-a in alpha",
                "content": "x",
                "author_email": "a@acme.com",
            },
            {
                "source_type": "github",
                "doc_id": "b0",
                "repo": "bravo",
                "title": "gh-b in bravo",
                "content": "x",
                "author_email": "a@acme.com",
            },
        ],
    )
    monkeypatch.undo()
    conn = store.connect_ro(s.db_path)
    served = {
        (r["repo"], r["title"]): r["number"]
        for r in conn.execute("SELECT repo, title, number FROM github_items")
    }
    # Sanity: the collision was actually forced -- both rows share the same number, in DIFFERENT
    # repos, otherwise the assertions below would pass even with the scope missing entirely.
    assert served[("alpha", "gh-a in alpha")] == served[("bravo", "gh-b in bravo")] == 7
    assert store.github_by_number(conn, "alpha", 7)["title"] == "gh-a in alpha"
    assert store.github_by_number(conn, "bravo", 7)["title"] == "gh-b in bravo"
    conn.close()


def test_jira_served_numbers_probe_on_a_collision_and_stay_stable_across_a_reimport(
    tmp_path, monkeypatch
):
    """jira's served suffix, like github's served number, is PROBED against a collision, not
    stored as the bare hash: `synth.jira_key_number`'s space is only 1..9,000 PER PROJECT (see
    store.ID_SEED's `scope`), so a real project collides far short of the birthday bound. Forced here by collapsing the seed to a constant, the same shape as the github test
    above (no `kind='file'` exclusion to also cover -- jira has no file-shaped row).

    Also covers stability across a re-import: like github's assignment (and unlike confluence's/
    hubspot's inline probe), jira's is a DEFERRED pass (`resolve_jira_numbers`) run once every
    record has loaded, and it is THAT method's own `_jira_numbers` memo -- populated by
    `seed_tracker_ids` before this run's insert can reset the column to NULL -- that a replay
    actually depends on to reproduce the same suffixes, not the corpus alone.

    Patches `store.ID_SEED` directly, NOT `synth.jira_key_number`: `store.served_id_seed
    ("jira")` -- what `_assign_jira_number` actually calls -- returns the tuple element
    `ID_SEED` captured when `backlot.store` was first imported, a bound reference to the
    function object, not a live attribute lookup (see the github/hubspot/confluence tests above
    for the same defect with `monkeypatch.setattr`).
    """
    from backlot.importer import byo
    from tests._helpers import build_corpus

    # `synth.jira_key_number` directly, NOT `monkeypatch.setitem(store.ID_SEED, "jira", ...)`:
    # jira is not in that registry -- its served value is the composed KEY -- so a setitem patch
    # is silently INERT, since `_assign_jira_number` reads the synth attribute at call time. The
    # assertion below pins a specific value so a dead patch cannot go unnoticed.
    monkeypatch.setattr(synth, "jira_key_number", lambda doc_id: 7)
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
    issues = {r["title"]: r["key"] for r in conn.execute("SELECT title, key FROM jira_issues")}
    # The collapsed seed actually landed -- j0, the first row processed (before anything is
    # taken), is served exactly the forced value, not some real hash -- AND resolved to 5 DISTINCT
    # suffixes, otherwise the collision below never actually happened and the rest of this test
    # passed for the wrong reason.
    assert issues["Issue 0"].endswith("-7")
    assert len(issues) == 5 and len(set(issues.values())) == 5
    for title, key in issues.items():
        assert store.jira_by_key(conn, key)["title"] == title
    conn.close()

    # A re-import is REFUSED. A probed id is a function of the whole corpus, and with the
    # dataset's own identifier gone there is nothing left to recognise a re-stated row by, so it
    # would be ADDED a second time at a second id rather than keeping the one a client may already
    # hold a url for. The import fails instead of duplicating in silence.
    with pytest.raises(SystemExit):
        byo.load(s.data_dir / "_corpus.jsonl", s, reset=False)
    conn = store.connect_ro(s.db_path)
    replay = {r["title"]: r["key"] for r in conn.execute("SELECT title, key FROM jira_issues")}
    assert replay == issues
    conn.close()


def test_jira_serves_one_key_column(tmp_path, monkeypatch):
    """`key` holds the whole key the API serves, whether the corpus wrote it or the import composed
    it. There is no separate suffix column.

    Storing only the numeric suffix meant the key had to be taken apart to look one up and put back
    together to serve it, and that round trip is what let `_jira_container_for_key`'s three-way
    tolerance — correct for the JQL project TOKEN — leak into the ISSUE-KEY namespace twice, once as
    `payments-7` resolving and once as case-insensitivity. Storing the key whole makes the lookup
    `WHERE key = ?` and removes the round trip that caused both.

    The UNIQUE is global, matching real Jira where a key is unique site-wide. That also closes the
    residual where two projects synthesizing the same prefix could serve the same key while a
    per-project uniqueness rule was perfectly satisfied.
    """
    from tests._helpers import build_corpus

    cols = {
        r[1]
        for r in store.connect_rw(tmp_path / "s.sqlite").execute("PRAGMA table_info(jira_issues)")
    }
    assert "key" in cols and "served_number" not in cols
    assert "jira" not in store.ID_SEED, (
        "jira's served value is composed from its project's prefix, so it cannot be a 1-arity "
        "seed over doc_id — it gets its own reader instead (as fireflies_users/linear_teams do)"
    )

    s = build_corpus(
        tmp_path / "c",
        [
            {
                "source_type": "jira",
                "doc_id": "j-provided",
                "project": "payments",
                "key": "PAY-7",
                "title": "provided",
                "content": "x",
                "author_email": "a@acme.com",
            },
            {
                "source_type": "jira",
                "doc_id": "j-derived",
                "project": "payments",
                "title": "derived",
                "content": "x",
                "author_email": "a@acme.com",
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    got = {r["title"]: r["key"] for r in conn.execute("SELECT title, key FROM jira_issues")}
    assert got["provided"] == "PAY-7", "the corpus's key is served verbatim"
    # the keyless sibling composes from the SAME prefix its provided sibling established
    assert got["derived"].startswith("PAY-") and got["derived"] != "PAY-7"
    for title, key in got.items():
        assert store.jira_by_key(conn, key)["title"] == title
    conn.close()


def test_jira_by_key_applies_the_acl(tmp_path):
    """A regression `notion_by_id` shipped without, and every reader
    since has had to guard against: a non-empty ``visible_ids`` that grants nothing must come back
    None, not the unscoped row -- otherwise the ACL clause could be deleted from
    `jira_by_key` invisibly. A non-empty set, so `_acl_clause` takes its EXISTS branch
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
    key = conn.execute("SELECT key FROM jira_issues").fetchone()["key"]
    assert store.jira_by_key(conn, key)["title"] == "Issue 0"
    assert store.jira_by_key(conn, key, visible_ids={"nobody"}) is None
    conn.close()


def test_jira_keys_are_unique_across_projects(tmp_path, monkeypatch):
    """A key is unique across a whole Jira site, so nothing scopes this lookup to a project — and
    that is the point: `WHERE key = ?` cannot resolve two issues to each other the way a
    project-scoped suffix lookup could.

    Storing the whole key makes the question different rather than smaller. A suffix is still
    unique only within a project, but a KEY is unique site-wide -- which is what real Jira
    guarantees -- so two projects that would serve the identical key is an IMPORT ERROR now, not a
    state the reader has to be careful around. That also closes the residual the per-project index
    could not: a keyless project's synthesized prefix colliding with another project's, leaving two
    documents serving one key while a (project, suffix) index was perfectly satisfied.

    Forced with a collapsed suffix seed AND a collapsed project prefix, so both projects compose
    the very same key.
    """
    from backlot import synth
    from tests._helpers import build_corpus

    monkeypatch.setattr(synth, "jira_key_number", lambda doc_id: 7)
    monkeypatch.setattr(synth, "jira_project_key", lambda container: "SAME")
    docs = [
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
    ]
    with pytest.raises(sqlite3.IntegrityError):
        build_corpus(tmp_path, docs)


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
    # `id` is the primary key; no test here reads it back, so a literal serves rather than
    # synth.notion_id, since this DB bypasses the importer on purpose.
    conn.execute(
        "INSERT INTO notion_pages(id,teamspace,author_email,title,content,created_ts) "
        "VALUES('n1','eng','a@x.com','Alpha runbook','deploy alpha service',1)"
    )
    conn.commit()
    store.build_fts(conn)
    return conn


def test_fts_add_docs_indexes_new_without_dropping_old(tmp_path):
    conn = _mini_db(tmp_path)
    # a new page inserted AFTER the initial build is not searchable until indexed
    conn.execute(
        "INSERT INTO notion_pages(id,teamspace,author_email,title,content,created_ts) "
        "VALUES('n2','eng','a@x.com','Beta guide','rotate beta credentials',2)"
    )
    conn.commit()
    assert store.search_documents(conn, "beta", "notion") == []  # not yet indexed
    n = store.fts_add_docs(conn, "notion", ["n2"])
    assert n == 1
    got = {r["id"] for r in store.search_documents(conn, "beta", "notion")}
    assert "n2" in got
    # the original doc is still searchable (index not clobbered)
    assert {r["id"] for r in store.search_documents(conn, "alpha", "notion")} == {"n1"}


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
        "INSERT INTO github_items(number,repo,author_email,title,content,kind,path,created_ts) "
        "VALUES(1,'svc','a@x','a.py','print(1)','file','src/a.py',1)"
    )
    conn.execute(
        "INSERT INTO github_items(number,repo,author_email,title,content,kind,path,created_ts) "
        "VALUES(2,'svc','a@x','b.py','print(2)','file','src/b.py',1)"
    )
    conn.execute(
        "INSERT INTO github_items(number,repo,author_email,title,content,kind,created_ts) "
        "VALUES(3,'svc','a@x','a bug','...', 'issue',1)"
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
        (1, "src/b.py", "everyone"),
        (2, "src/a.py", "everyone"),
        (3, "s.py", "people"),
    ]
    for number, path, principal in rows:
        conn.execute(
            "INSERT INTO github_items(number,repo,author_email,title,content,kind,path,created_ts)"
            " VALUES(?,'svc','a@x',?,'body','file',?,1)",
            (number, path.rsplit("/", 1)[-1], path),
        )
        # a github grant names its document by (repo, number) — see store.ID_COLUMNS
        conn.execute(
            "INSERT INTO github_acl(repo, number, principal_id, principal_type) "
            "VALUES('svc',?,?,'group')",
            (number, principal),
        )
    conn.execute(
        "INSERT INTO github_items(number,repo,author_email,title,content,kind,created_ts) "
        "VALUES(4,'svc','a@x','a bug','...','issue',1)"
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
            "EXPLAIN QUERY PLAN SELECT * FROM hubspot_objects WHERE object_type = ? AND id > ? "
            "ORDER BY id LIMIT 10",
            ("contacts", "c0"),
        )
    )
    assert "idx_hubspot_type_doc" in plan
    assert "TEMP B-TREE" not in plan.upper(), f"ORDER BY is being sorted, not seeked: {plan}"


# --- Linear -----------------------------------------------------------------------
# Linear's store surface is its own set of functions (a Relay connection needs a total as well
# as a page, and comments are ACL-scoped through their parent issue), so it gets its own block.


def test_linear_list_and_count_agree(db, keys):
    rows = store.list_linear_issues(db, limit=100)
    assert store.count_linear_issues(db) == len(rows) == 5
    assert {r["id"] for r in rows} == {
        keys["lin-rl"][0],
        keys["lin-batch"][0],
        keys["lin-des"][0],
        keys["lin-secret"][0],
        keys["lin-blackops"][0],
    }


def test_linear_list_scopes_to_a_team(db, keys):
    rows = store.list_linear_issues(db, "design", limit=100)
    assert [r["id"] for r in rows] == [keys["lin-des"][0]]
    assert store.count_linear_issues(db, "design") == 1


def test_linear_default_order_is_createdAt_ascending(db):
    """Linear documents createdAt as the default ordering, and its `PaginationOrderBy` carries no
    direction — so an absent `orderBy` must still order by creation, not by insertion order."""
    default = [r["id"] for r in store.list_linear_issues(db, "engineering", limit=100)]
    explicit = [
        r["id"]
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
        seen.append(page[0]["id"])
    assert len(seen) == len(set(seen)) == 5


def test_linear_identifier_lookup(db, keys):
    assert store.linear_issue_by_identifier(db, "ENG-101")["id"] == keys["lin-rl"][0]
    assert store.linear_issue_by_identifier(db, "NOPE-1") is None


def test_linear_served_ids_are_stored_and_resolve(tmp_path, monkeypatch):
    """`served_id` is the UUID half of `issue(id:)`, distinct from `identifier` -- the OTHER
    half, which stays a plain lookup index because it is not unique (see the schema comment on
    idx_linear_doc_ident / idx_linear_identifier). Like gmail's, no probe: synth.linear_id draws
    from `_uuid_from`'s full digest space, so the seed is stored as-is, and a forced collision
    must fail the import loudly rather than resolve through the shared upsert.

    `monkeypatch.setitem` on `store.ID_SEED`, not `monkeypatch.setattr(synth, ...)` -- the
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
    rows = conn.execute("SELECT id, title FROM linear_issues").fetchall()
    assert len(rows) == 3
    assert {r["id"] for r in rows} == {synth.linear_id(f"i{i}") for i in range(3)}
    for r in rows:
        assert store.linear_by_id(conn, r["id"])["title"] == r["title"]
    # A non-empty, non-matching visible_ids must still exclude the row -- proving _acl_clause
    # takes its EXISTS branch here rather than the empty-set "AND 0" short-circuit (which every
    # one of these public rows would otherwise pass regardless of who's asking).
    served = rows[0]["id"]
    assert store.linear_by_id(conn, served, visible_ids={"nobody"}) is None
    assert store.linear_by_id(conn, served, visible_ids=None) is not None  # admin bypass
    conn.close()

    monkeypatch.setitem(store.ID_SEED, "linear", (lambda doc_id: "dup-uuid", None))
    with pytest.raises(SystemExit, match="already resolves to"):
        build_corpus(tmp_path / "collide", docs)


def test_linear_prefilter_is_pushed_into_sql(db, keys):
    flt = ("state = ?", ["Done"])
    assert store.count_linear_issues(db, prefilter=flt) == 1
    assert store.list_linear_issues(db, prefilter=flt)[0]["id"] == keys["lin-batch"][0]


def test_linear_comments_scoped_to_one_issue(db, keys):
    rows = store.list_linear_comments(db, issue_id=keys["lin-rl"][0])
    assert [r["body"] for r in rows] == ["Reproduced with a burst test.", "Fix is in review."]
    assert store.count_linear_comments(db, issue_id=keys["lin-rl"][0]) == 2


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
    primary-key lookup (`get_container`). Both are written at import.

    `synth.linear_team_key` is NOT injective: "night-shift" and "north-star" both reduce to "NS".
    `served_key` therefore carries no UNIQUE index, and the tie must break by team NAME order --
    the same order `store.list_containers` returns -- so the key resolves to one fixed team.

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

    # No probe: `synth.linear_team_id` draws from `_uuid_from`'s full digest space, so the
    # raw seed is stored as-is -- a forced collision must fail the import loudly through
    # `idx_linear_teams_served`, not silently let one team's row overwrite the other's.
    monkeypatch.setattr(synth, "linear_team_id", lambda name: "dup-team-uuid")
    with pytest.raises(sqlite3.IntegrityError):
        build_corpus(tmp_path / ("collide-" + "-".join(order)), docs)


def test_linear_distinct_values_feed_the_entity_table(db):
    d = store.linear_distinct_values(db)
    assert ("engineering", "In Progress") in d["states"]
    assert "runtime-stability" in d["projects"]
    assert ("engineering", "2025-W08") in d["cycles"]
    assert {"bug", "gateway", "latency", "tokens"} <= set(d["labels"])
    assert ("bob@acme.com", "Bob Stone") in d["users"]


def test_linear_entity_by_id_resolves_every_kind_from_the_stored_table(db):
    """None of these six DISTINCT values has a document row to be stored on -- a Linear project or
    label exists only as a column value on some issue. `linear_entities` gives them one, written at
    import.

    Every kind is asserted, and each in the SHAPE its resolver serves: a bare name for the three
    corpus-wide entities, `(team, name)` for the two Linear scopes to a team, `(email, display)` for
    a person. Getting a shape wrong is not a type error anywhere -- the resolver would just unpack a
    tuple into the wrong fields -- so the shape is the assertion.
    """
    cases = {
        "project": ("runtime-stability", synth.linear_project_id("runtime-stability")),
        "label": ("gateway", synth.linear_label_id("gateway")),
        "state": (
            ("engineering", "In Progress"),
            synth.linear_state_id("In Progress", "engineering"),
        ),
        "cycle": (("engineering", "2025-W08"), synth.linear_cycle_id("2025-W08", "engineering")),
        "user": (("bob@acme.com", "Bob Stone"), synth.linear_user_id("bob@acme.com")),
    }
    assert set(cases) <= set(store.LINEAR_ENTITY_VALUE)
    for kind, (want, served) in cases.items():
        assert store.linear_entity_by_id(db, kind, served) == want, kind
    # An id no row holds is absent, not an error -- the by-id root turns that into "not found".
    assert store.linear_entity_by_id(db, "project", "00000000-0000-0000-0000-000000000000") is None
    # An unknown KIND is a programming error and raises, the way store.table() does for a source.
    with pytest.raises(ValueError, match="nope"):
        store.linear_entity_by_id(db, "nope", "x")


def test_linear_entities_are_rebuilt_whole_so_an_append_is_resolvable(tmp_path):
    """The table's contents are a DISTINCT over the live issues, so after an `--append` the right
    answer is a function of every issue now present -- not of the ones that run happened to add.
    Rebuilding whole is what makes a project introduced by an appended issue resolvable, and what
    keeps one that no issue carries any more from lingering.

    Safe to rebuild because these ids are pure hashes of a NAME and never probed: a row deleted and
    re-inserted comes back with the same id, so no id a client holds can move.
    """
    from backlot.importer.byo import load
    from backlot.config import Settings

    def issue(doc_id, project):
        return {
            "source_type": "linear",
            "team": "engineering",
            "doc_id": doc_id,
            "title": doc_id,
            "content": "c",
            "author_email": "a@acme.com",
            "project": project,
        }

    settings = Settings(data_dir=tmp_path)
    first = tmp_path / "a.jsonl"
    first.write_text(json.dumps(issue("l1", "alpha")) + "\n")
    load(first, settings, reset=True)
    second = tmp_path / "b.jsonl"
    second.write_text(json.dumps(issue("l2", "beta")) + "\n")
    load(second, settings, reset=False)

    conn = store.connect_ro(settings.db_path)
    try:
        # Both the original project and the appended one resolve.
        assert (
            store.linear_entity_by_id(conn, "project", synth.linear_project_id("alpha")) == "alpha"
        )
        assert store.linear_entity_by_id(conn, "project", synth.linear_project_id("beta")) == "beta"
        assert (
            conn.execute("SELECT COUNT(*) FROM linear_entities WHERE kind = 'project'").fetchone()[
                0
            ]
            == 2
        )
    finally:
        conn.close()

    # The other half, and the one only a REBUILD gives: an entity no issue carries any more has to
    # STOP resolving. Re-stating `l1` under a different project is how that arises -- linear's id is
    # a pure hash of the seed, so a re-import upserts the issue in place (unlike a probed source,
    # which refuses one) and nothing is left carrying `alpha`. Appending to the table instead of
    # rebuilding it would leave `alpha` answering for an entity that exists nowhere.
    third = tmp_path / "c.jsonl"
    third.write_text(json.dumps(issue("l1", "gamma")) + "\n")
    load(third, settings, reset=False)
    conn = store.connect_ro(settings.db_path)
    try:
        assert (
            store.linear_entity_by_id(conn, "project", synth.linear_project_id("gamma")) == "gamma"
        )
        assert (
            store.linear_entity_by_id(conn, "project", synth.linear_project_id("alpha")) is None
        ), (
            "a project no issue carries any more still resolves -- the table was appended to, "
            "not rebuilt"
        )
    finally:
        conn.close()


def test_linear_comment_table_is_registered(db):
    assert store.comment_table("linear") == "linear_comments"


def test_linear_relation_by_id_scopes_the_from_end_too(tmp_path):
    """The docstring claims a relation is "scoped on BOTH ends". A relation whose `to` issue is
    granted to the caller but whose `from` issue is NOT must still be hidden — this pins the alias
    collision where `_acl_clause`'s inner alias shadowed the caller's outer "a"/"b" alias and
    turned the from-end predicate into a tautology (`a.id = a.id`), which admitted a
    relation off an unreadable issue to any caller holding ANY grant at all in `linear_acl`."""
    from backlot import synth

    conn = store.connect_rw(tmp_path / "rel.sqlite")
    for issue_id in ("i1", "i2"):
        conn.execute(
            # `id` is the primary key; a literal serves rather than synth.linear_id, since this DB
            # bypasses the importer on purpose.
            "INSERT INTO linear_issues(id, team, author_email, title, content, created_ts) "
            "VALUES (?,'eng','a@x.com','issue','body',1)",
            (issue_id,),
        )
    conn.execute(
        "INSERT INTO linear_relations(id, from_id, to_id, type, created_ts) "
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


def test_fireflies_child_rows_use_the_shared_comment_slot(db, keys):
    # Not comments, but the same "child rows of a doc" mapping — and therefore the same column
    # contract, which is what makes doc_comments work against it unchanged.
    assert store.comment_table("fireflies") == "fireflies_sentences"
    rows = store.doc_comments(db, "fireflies", keys["ff-discovery"][0])
    assert [r["seq"] for r in rows] == [1, 2, 3, 4]
    assert rows[0]["body"].startswith("Thanks for joining")


def test_fireflies_scope_columns_are_the_api_vocabulary():
    assert store.fireflies_scope_columns("title") == ("title",)
    assert store.fireflies_scope_columns("sentences") == ("content",)
    assert store.fireflies_scope_columns("all") == ("title", "content")
    assert store.fireflies_scope_columns(None) == ("title", "content")  # default
    assert store.fireflies_scope_columns("ALL") == ("title", "content")  # case-insensitive
    assert store.fireflies_scope_columns("body") is None  # not an API value


def test_fireflies_transcripts_are_newest_first(db, keys):
    rows = store.list_fireflies_transcripts(db, limit=50)
    tss = [r["created_ts"] for r in rows]
    assert tss == sorted(tss, reverse=True)
    assert {r["id"] for r in rows} == {
        keys["ff-discovery"][0],
        keys["ff-allhands"][0],
        keys["ff-secret"][0],
    }


def test_fireflies_limit_and_skip_walk_the_corpus_without_gaps(db):
    everything = [r["id"] for r in store.list_fireflies_transcripts(db, limit=50)]
    walked = []
    for skip in range(0, len(everything)):
        page = store.list_fireflies_transcripts(db, limit=1, offset=skip)
        walked += [r["id"] for r in page]
    assert walked == everything
    # past the end is an empty page, not an error
    assert store.list_fireflies_transcripts(db, limit=5, offset=999) == []


def test_fireflies_keyword_honours_every_scope(db, keys):
    # "selects" appears only in a SENTENCE of ff-allhands, never in any title.
    assert [
        r["id"] for r in store.list_fireflies_transcripts(db, keyword="selects", scope="sentences")
    ] == [keys["ff-allhands"][0]]
    assert store.list_fireflies_transcripts(db, keyword="selects", scope="title") == []
    assert [
        r["id"] for r in store.list_fireflies_transcripts(db, keyword="selects", scope="all")
    ] == [keys["ff-allhands"][0]]
    # "all-hands" appears only in a TITLE.
    assert [
        r["id"] for r in store.list_fireflies_transcripts(db, keyword="all-hands", scope="title")
    ] == [keys["ff-allhands"][0]]
    assert store.list_fireflies_transcripts(db, keyword="all-hands", scope="sentences") == []


def test_fireflies_keyword_wildcards_stay_literal(db):
    # A LIKE metacharacter in the needle must not turn into a match-everything pattern.
    assert store.list_fireflies_transcripts(db, keyword="%") == []
    assert store.list_fireflies_transcripts(db, keyword="_atency") == []
    assert [r["id"] for r in store.list_fireflies_transcripts(db, keyword="latency")]


def test_fireflies_filters_narrow_by_channel_host_and_date(db, keys):
    assert [r["id"] for r in store.list_fireflies_transcripts(db, channel="all-hands")] == [
        keys["ff-allhands"][0]
    ]
    assert [r["id"] for r in store.list_fireflies_transcripts(db, host_email="AVA@acme.com")] == [
        keys["ff-discovery"][0]
    ]  # case-insensitive
    ts = store.list_fireflies_transcripts(db, channel="sales-calls")[0]["created_ts"]
    assert [r["id"] for r in store.list_fireflies_transcripts(db, to_ts=ts)] == [
        keys["ff-discovery"][0]
    ]
    assert keys["ff-discovery"][0] not in [
        r["id"] for r in store.list_fireflies_transcripts(db, from_ts=ts + 1)
    ]


def test_fireflies_organizer_filter_falls_back_to_the_host(db, keys):
    # organizer_email is NULL when the organizer IS the host, so filtering by the host's address
    # must still match — otherwise organizing your own meeting would not find it.
    assert (
        db.execute(
            "SELECT organizer_email FROM fireflies_transcripts WHERE id = ?",
            (keys["ff-discovery"][0],),
        ).fetchone()[0]
        is None
    )
    assert [r["id"] for r in store.list_fireflies_transcripts(db, organizers=["ava@acme.com"])] == [
        keys["ff-discovery"][0]
    ]


def test_fireflies_participant_filter_is_exact_membership(db, keys):
    assert [
        r["id"] for r in store.list_fireflies_transcripts(db, participants=["ava@acme.com"])
    ] == [keys["ff-discovery"][0]]
    # a substring of a stored address must NOT match (json_each, not a LIKE on the JSON text)
    assert store.list_fireflies_transcripts(db, participants=["ava@acme.co"]) == []


def test_fireflies_transcript_by_id_is_unambiguous(db):
    row = store.list_fireflies_transcripts(db, limit=1)[0]
    assert store.fireflies_transcript_by_id(db, row["id"])["id"] == row["id"]
    assert store.fireflies_transcript_by_id(db, "deadbeefdeadbeefdeadbeef") is None
    # unique by CONSTRAINT now -- it is the table's primary key, not merely unique by
    # construction the way a value derived from a corpus identifier was.
    n, distinct = db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT id) FROM fireflies_transcripts"
    ).fetchone()
    assert n == distinct


def test_fireflies_sentences_come_back_in_spoken_order(db, keys):
    rows = store.fireflies_sentences(db, keys["ff-discovery"][0])
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
    # Insert unrelated data without committing. `id` is the primary key; a literal serves rather
    # than synth.notion_id, since this DB bypasses the importer on purpose.
    conn.execute(
        "INSERT INTO notion_pages(id,teamspace,author_email,title,content,created_ts) "
        "VALUES('n1','eng','a@x.com','Test','content',1)"
    )
    # write_meta commits the entire pending transaction
    store.write_meta(conn, "test_key", "test_value")
    conn.close()
    # From a separate connection, verify both the unrelated row and the meta row are visible
    check_conn = store.connect_ro(path)
    assert (
        check_conn.execute("SELECT COUNT(*) FROM notion_pages WHERE id = 'n1'").fetchone()[0] == 1
    )
    assert store.read_meta(check_conn, "test_key") == "test_value"
    check_conn.close()
