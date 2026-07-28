"""Tests for the read-only SQLite store layer (`app.store`).

The store is shared by every router, search, and the importers, so it gets its own file rather
than being verified incidentally through a load/route test. Registry wiring is checked across
every source; generic reads run against the shared SAMPLE corpus (the `db` fixture); connection
tuning uses hand-built / SAMPLE DBs.

ACL-filtered reads live in test_acl.py (the ACL is the subject there) and FTS search in
test_search.py (search is its own sub-domain); this file covers the plain store surface.
"""
import sqlite3

import pytest

from app import store

ALL_SOURCES = ["slack", "gmail", "google_drive", "github", "jira", "confluence", "notion", "s3",
               "hubspot", "linear"]


# --- registry wiring ------------------------------------------------------------

def test_registry_covers_every_source():
    assert set(store.SOURCE_TABLE) == set(ALL_SOURCES)
    for src in ALL_SOURCES:
        assert store.table(src)            # source -> table resolves
        assert store.grouping_table(src)   # source -> grouping table resolves
        assert store.grouping_col(src)     # source -> grouping column resolves


def test_unknown_source_type_raises():
    with pytest.raises(ValueError):
        store.table("nope")


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
    # object type is the grouping unit (see app/store.py GROUPING).
    assert store.grouping_col("hubspot") == "object_type"


def test_comment_tables_only_where_supported():
    # jira/confluence/github/notion expose comments; slack/gmail/drive/s3 do not
    assert store.comment_table("jira") == "jira_comments"
    assert store.comment_table("confluence") == "confluence_comments"
    assert store.comment_table("github") == "github_comments"
    assert store.comment_table("notion") == "notion_comments"
    # HubSpot models notes/emails/meetings as their own object types, not as comments on a record.
    for src in ("slack", "gmail", "google_drive", "s3", "hubspot"):
        assert store.comment_table(src) is None


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
    open_ids = {r["doc_id"] for r in store.list_documents(db, "github", container="gateway",
                                                          limit=100, state="open")}
    closed_ids = {r["doc_id"] for r in store.list_documents(db, "github", container="gateway",
                                                            limit=100, state="closed")}
    assert "gh-issue-1" in open_ids and "gh-pr-1" not in open_ids
    assert "gh-pr-1" in closed_ids and "gh-issue-1" not in closed_ids
    # state=None (default) applies no filter -> both present
    all_ids = {r["doc_id"] for r in store.list_documents(db, "github", container="gateway", limit=100)}
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
    assert store.jcol(issue, "labels") == ["bug", "gateway"]      # JSON-valued TEXT column
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
        ("d5", "other", "logs/2026/01/a.json"),   # same key, different bucket
    ]
    for doc_id, bucket, key in rows:
        conn.execute(
            "INSERT INTO s3_objects(doc_id, bucket, author_email, title, content, key, "
            "created_ts) VALUES (?,?,?,?,?,?,1)",
            (doc_id, bucket, "a@x.com", key, "body", key))
    # d2 is ACL-restricted to group 'eng'; everything else is unrestricted (no doc_acl row ->
    # _acl_clause's EXISTS check only bites rows it has an entry for).
    conn.execute("INSERT INTO doc_acl VALUES ('d2','group','eng')")
    conn.commit()
    return conn


def test_list_s3_objects_prefix_and_order(tmp_path):
    conn = _s3_mini_db(tmp_path)
    rows = store.list_s3_objects(conn, "b", prefix="logs/2026/01/")
    assert [r["key"] for r in rows] == ["logs/2026/01/a.json", "logs/2026/01/b.json"]
    # a bucket-only listing stays sorted and scoped to that bucket (d5 in "other" excluded)
    rows = store.list_s3_objects(conn, "b")
    assert [r["key"] for r in rows] == ["logs/2026/01/a.json", "logs/2026/01/b.json",
                                        "logs/2026/02/a.json", "notes/readme.md"]


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
        "ORDER BY key ASC", ("b", prefix, succ)).fetchall()
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
            (doc_id, "b", "a@x.com", key, "body", key))
    conn.commit()
    assert [r["key"] for r in store.list_s3_objects(conn, "b", prefix="LOGS/")] == []
    assert [r["key"] for r in store.list_s3_objects(conn, "b", prefix="logs/")] == \
        ["logs/a.json", "logs/b.json"]


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
    scoped_keys = {r["key"] for r in
                  store.list_s3_objects(conn, "b", prefix="logs/2026/01/", visible_ids={"eng"})}
    assert scoped_keys == {"logs/2026/01/b.json"}          # only d2, granted to group 'eng'
    none_visible = store.list_s3_objects(conn, "b", prefix="logs/2026/01/", visible_ids={"nobody"})
    assert none_visible == []


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
            (doc_id, object_type, "owner@x.com", title, title, properties))
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
            (from_doc, from_type, to_doc, to_type))
    # Every doc carries an ACL grant, as the importers write them: org-wide for most, and c2
    # restricted to group 'sales'.
    for doc_id, ptype, pid in [("c1", "group", "everyone"), ("c2", "group", "sales"),
                               ("co1", "group", "everyone"), ("d1", "group", "everyone")]:
        conn.execute("INSERT INTO doc_acl VALUES (?,?,?)", (doc_id, ptype, pid))
    conn.commit()
    return conn


def test_list_hubspot_objects_scoped_by_object_type(tmp_path):
    conn = _hubspot_mini_db(tmp_path)
    # the object type is the grouping unit, so the generic container scope selects by it
    assert [r["doc_id"] for r in store.list_documents(conn, "hubspot", container="contacts")] \
        == ["c1", "c2"]
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
    assert [r["to_doc_id"] for r in store.hubspot_associations(conn, "co1", "contacts")] \
        == ["c1", "c2"]
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
    conn.execute("INSERT INTO github_items(doc_id, repo, author_email, title, content, created_ts) "
                "VALUES ('i1', 'svc', 'a@x', 'a bug', '...', 1)")
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
        assert "path" in cols
    finally:
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
    conn.execute("INSERT INTO notion_pages(doc_id,teamspace,author_email,title,content,created_ts) "
                 "VALUES('n1','eng','a@x.com','Alpha runbook','deploy alpha service',1)")
    conn.commit()
    store.build_fts(conn)
    return conn


def test_fts_add_docs_indexes_new_without_dropping_old(tmp_path):
    conn = _mini_db(tmp_path)
    # a new page inserted AFTER the initial build is not searchable until indexed
    conn.execute("INSERT INTO notion_pages(doc_id,teamspace,author_email,title,content,created_ts) "
                 "VALUES('n2','eng','a@x.com','Beta guide','rotate beta credentials',2)")
    conn.commit()
    assert store.search_documents(conn, "beta", "notion") == []          # not yet indexed
    n = store.fts_add_docs(conn, "notion", ["n2"])
    assert n == 1
    got = {r["doc_id"] for r in store.search_documents(conn, "beta", "notion")}
    assert "n2" in got
    # the original doc is still searchable (index not clobbered)
    assert {r["doc_id"] for r in store.search_documents(conn, "alpha", "notion")} == {"n1"}


def test_fts_add_docs_is_idempotent(tmp_path):
    conn = _mini_db(tmp_path)
    store.fts_add_docs(conn, "notion", ["n1"])          # re-index existing doc
    assert len(store.search_documents(conn, "alpha", "notion")) == 1     # no duplicate row


def test_fts_add_docs_noop_without_index(tmp_path):
    conn = store.connect_rw(tmp_path / "n.sqlite")       # no build_fts called
    assert store.fts_add_docs(conn, "notion", ["x"]) == 0


def test_repo_files_listing_and_kind_isolation(tmp_path):
    conn = store.connect_rw(tmp_path / "g.sqlite")
    # two files + one issue in the same repo
    conn.execute("INSERT INTO github_items(doc_id,repo,author_email,title,content,kind,path,created_ts) "
                 "VALUES('f1','svc','a@x','a.py','print(1)','file','src/a.py',1)")
    conn.execute("INSERT INTO github_items(doc_id,repo,author_email,title,content,kind,path,created_ts) "
                 "VALUES('f2','svc','a@x','b.py','print(2)','file','src/b.py',1)")
    conn.execute("INSERT INTO github_items(doc_id,repo,author_email,title,content,kind,created_ts) "
                 "VALUES('i1','svc','a@x','a bug','...', 'issue',1)")
    conn.commit()
    files = store.list_repo_files(conn, "svc")
    assert [f["path"] for f in files] == ["src/a.py", "src/b.py"]     # only files, sorted, no issue
    assert store.count_repo_files(conn, "svc") == 2
    got = store.get_repo_file(conn, "svc", "src/b.py")
    assert got["content"] == "print(2)"
    assert store.get_repo_file(conn, "svc", "nope.py") is None


def test_hubspot_listing_is_an_index_range_seek(tmp_path):
    """Every read of hubspot_objects is "one object type, ordered by doc_id" — keyset paging and the
    search scan both. With only object_type indexed, ORDER BY falls to a temp b-tree that re-sorts
    every matching row on each page, so paging a large type costs O(rows) per page instead of
    O(log rows + page). Measured on 69k notes that was ~1s per search page; the composite index made
    it ~1ms. Same guard as test_list_s3_objects_prefix_uses_index_range_not_like."""
    conn = _hubspot_mini_db(tmp_path)
    plan = " ".join(r[-1] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM hubspot_objects WHERE object_type = ? AND doc_id > ? "
        "ORDER BY doc_id LIMIT 10", ("contacts", "c0")))
    assert "idx_hubspot_type_doc" in plan
    assert "TEMP B-TREE" not in plan.upper(), f"ORDER BY is being sorted, not seeked: {plan}"


# --- Linear -----------------------------------------------------------------------
# Linear's store surface is its own set of functions (a Relay connection needs a total as well
# as a page, and comments are ACL-scoped through their parent issue), so it gets its own block.

def test_linear_list_and_count_agree(db):
    rows = store.list_linear_issues(db, limit=100)
    assert store.count_linear_issues(db) == len(rows) == 5
    assert {r["doc_id"] for r in rows} == {"lin-rl", "lin-batch", "lin-des", "lin-secret",
                                           "lin-blackops"}


def test_linear_list_scopes_to_a_team(db):
    rows = store.list_linear_issues(db, "design", limit=100)
    assert [r["doc_id"] for r in rows] == ["lin-des"]
    assert store.count_linear_issues(db, "design") == 1


def test_linear_list_orders_newest_first_by_created(db):
    rows = store.list_linear_issues(db, "engineering", limit=100, order_by="createdAt")
    stamps = [r["created_ts"] for r in rows]
    assert stamps == sorted(stamps, reverse=True)


def test_linear_list_orders_oldest_first_when_ascending(db):
    rows = store.list_linear_issues(db, "engineering", limit=100, order_by="createdAt",
                                    descending=False)
    stamps = [r["created_ts"] for r in rows]
    assert stamps == sorted(stamps)


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
    assert store.linear_team_issue_counts(db) == {"engineering": 3, "design": 1,
                                                 "blackops": 1}


def test_linear_distinct_values_feed_the_reverse_index(db):
    d = store.linear_distinct_values(db)
    assert ("engineering", "In Progress") in d["states"]
    assert "runtime-stability" in d["projects"]
    assert ("engineering", "2025-W08") in d["cycles"]
    assert {"bug", "gateway", "latency", "tokens"} <= set(d["labels"])
    assert ("bob@acme.com", "Bob Stone") in d["users"]


def test_linear_comment_table_is_registered(db):
    assert store.comment_table("linear") == "linear_comments"
