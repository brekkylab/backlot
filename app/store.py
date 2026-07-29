"""Read-only SQLite access layer.

Each service has its **own** table with columns natural to that service — no single
crammed `documents` table, so a column used by one service never lands on another's
rows. Each service also has its **own** grouping-unit table under its natural name
(``slack_channels``, ``gmail_mailboxes``, ``gdrive_folders``, ``github_repos``,
``jira_projects``, ``confluence_spaces``) mapping that unit to its owning ACL group.
Cross-cutting *relationship* tables (principals, group membership, ACL grants) stay
shared and are keyed by the globally-unique ``doc_id``.

Every doc table shares four core columns — ``doc_id, author_email, title, content`` —
plus a service-specific grouping column (``channel``/``mailbox``/``folder``/``repo``/
``project``/``space``); listing / ACL / pagination stay uniform via the ``GROUPING``
registry, and everything else is per-service.
Every listing/get takes an optional ``visible_ids`` set: ``None`` = admin (sees all),
otherwise results are filtered to docs whose ACL grants intersect that set. JSON-valued
columns (reactions, labels, …) are stored as TEXT; read them with :func:`jcol`.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

# source_type -> its dedicated table
SOURCE_TABLE = {
    "slack": "slack_messages",
    "gmail": "gmail_messages",
    "google_drive": "gdrive_files",
    "github": "github_items",
    "jira": "jira_issues",
    "confluence": "confluence_pages",
    "notion": "notion_pages",
    "s3": "s3_objects",
    "hubspot": "hubspot_objects",
    "linear": "linear_issues",
}


def table(source_type: str) -> str:
    try:
        return SOURCE_TABLE[source_type]
    except KeyError:
        raise ValueError(f"unknown source_type {source_type!r}")


# source_type -> its comment table (only services whose API exposes comments)
COMMENT_TABLE = {
    "jira": "jira_comments",
    "confluence": "confluence_comments",
    "github": "github_comments",
    "notion": "notion_comments",
    "linear": "linear_comments",
}


def comment_table(source_type: str) -> str | None:
    return COMMENT_TABLE.get(source_type)


# source_type -> (grouping table, grouping column) — the service's own name for its
# grouping unit (Slack channel, Gmail mailbox, Drive folder, GitHub repo, Jira project,
# Confluence space) instead of a vague generic "container".
GROUPING = {
    "slack": ("slack_channels", "channel"),
    "gmail": ("gmail_mailboxes", "mailbox"),
    "google_drive": ("gdrive_folders", "folder"),
    "github": ("github_repos", "repo"),
    "jira": ("jira_projects", "project"),
    "confluence": ("confluence_spaces", "space"),
    "notion": ("notion_teamspaces", "teamspace"),
    "s3": ("s3_buckets", "bucket"),
    # HubSpot has no channel/space/repo equivalent — its API is polymorphic over `{objectType}`
    # (contacts, companies, deals, …) and supports custom objects, so the object type *is* the
    # grouping unit and the thing an ACL group hangs off.
    "hubspot": ("hubspot_object_types", "object_type"),
    # Linear's own container is the team: `data.team.issues` is how both the API and the
    # official clients reach issues, and an issue's identifier prefix (ENG-123) is the team key.
    "linear": ("linear_teams", "team"),
}


def grouping_table(source_type: str) -> str:
    return GROUPING[source_type][0]


def grouping_col(source_type: str) -> str:
    return GROUPING[source_type][1]


SCHEMA = """
-- ── per-service document tables (core cols first, then service-specific) ──
CREATE TABLE IF NOT EXISTS slack_messages (
    doc_id TEXT PRIMARY KEY, channel TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    thread_id TEXT, thread_seq INTEGER NOT NULL DEFAULT 0, subtype TEXT,
    reactions TEXT, files TEXT, edited TEXT, created_ts INTEGER NOT NULL, participants TEXT
);
CREATE INDEX IF NOT EXISTS idx_slack_channel ON slack_messages(channel);
CREATE INDEX IF NOT EXISTS idx_slack_thread ON slack_messages(thread_id);
-- conversations.replies resolves a ts by (channel, created_ts); the composite index turns that from
-- a per-channel row scan (~340k rows in a big channel) into a direct lookup.
CREATE INDEX IF NOT EXISTS idx_slack_channel_ts ON slack_messages(channel, created_ts);

CREATE TABLE IF NOT EXISTS gmail_messages (
    doc_id TEXT PRIMARY KEY, mailbox TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    thread_id TEXT, thread_seq INTEGER NOT NULL DEFAULT 0,
    label_ids TEXT, to_addr TEXT, cc TEXT, bcc TEXT, reply_to TEXT,
    message_id TEXT, in_reply_to TEXT, refs TEXT, attachments TEXT, created_ts INTEGER NOT NULL,
    body_html TEXT, owner_display TEXT
);
CREATE INDEX IF NOT EXISTS idx_gmail_mailbox ON gmail_messages(mailbox);
CREATE INDEX IF NOT EXISTS idx_gmail_author ON gmail_messages(author_email);
-- date-scoped listing (ls /gmail/<label>/<date>) filters by a created_ts range; the index turns
-- that from a full-table scan into a range seek.
CREATE INDEX IF NOT EXISTS idx_gmail_created_ts ON gmail_messages(created_ts);

CREATE TABLE IF NOT EXISTS gdrive_files (
    doc_id TEXT PRIMARY KEY, folder TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    subtype TEXT, mime_type TEXT, parents TEXT, created_ts INTEGER NOT NULL, updated_ts INTEGER,
    trashed INTEGER, collaborators TEXT, owner_display TEXT
);
CREATE INDEX IF NOT EXISTS idx_gdrive_folder ON gdrive_files(folder);

CREATE TABLE IF NOT EXISTS github_items (
    doc_id TEXT PRIMARY KEY, repo TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    kind TEXT, state TEXT, labels TEXT, assignees TEXT,
    merged_at TEXT, head_ref TEXT, base_ref TEXT, reviews TEXT, reactions TEXT,
    created_ts INTEGER NOT NULL, updated_ts INTEGER,
    closed_ts INTEGER, closed_by TEXT, merged_by TEXT, milestone TEXT, requested_reviewers TEXT,
    owner_display TEXT, path TEXT
);
CREATE INDEX IF NOT EXISTS idx_github_repo ON github_items(repo);
CREATE INDEX IF NOT EXISTS idx_github_repo_path ON github_items(repo, path);

CREATE TABLE IF NOT EXISTS jira_issues (
    doc_id TEXT PRIMARY KEY, project TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    status TEXT, issuetype TEXT, priority TEXT, labels TEXT, components TEXT,
    issuelinks TEXT, parent_id TEXT, changelog TEXT, created_ts INTEGER NOT NULL, updated_ts INTEGER,
    assignee_email TEXT, reporter_email TEXT, resolution TEXT, resolution_ts INTEGER,
    duedate TEXT, fix_versions TEXT, severity TEXT, squad TEXT, owner_display TEXT
);
CREATE INDEX IF NOT EXISTS idx_jira_project ON jira_issues(project);
CREATE INDEX IF NOT EXISTS idx_jira_parent ON jira_issues(parent_id);

CREATE TABLE IF NOT EXISTS confluence_pages (
    doc_id TEXT PRIMARY KEY, space TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    subtype TEXT, parent_id TEXT, labels TEXT, created_ts INTEGER NOT NULL, updated_ts INTEGER,
    version_number INTEGER, version_message TEXT, minor_edit INTEGER,
    reviewers TEXT, confidentiality TEXT, owner_team TEXT, owner_display TEXT
);
CREATE INDEX IF NOT EXISTS idx_confluence_space ON confluence_pages(space);
CREATE INDEX IF NOT EXISTS idx_confluence_parent ON confluence_pages(parent_id);

-- ── per-service comment tables (only services whose API exposes comments) ──
CREATE TABLE IF NOT EXISTS jira_comments (
    id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, seq INTEGER NOT NULL,
    author_email TEXT, body TEXT NOT NULL, created_ts INTEGER NOT NULL, reactions TEXT
);
CREATE INDEX IF NOT EXISTS idx_jira_comments_doc ON jira_comments(doc_id);

CREATE TABLE IF NOT EXISTS confluence_comments (
    id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, seq INTEGER NOT NULL,
    author_email TEXT, body TEXT NOT NULL, created_ts INTEGER NOT NULL, reactions TEXT
);
CREATE INDEX IF NOT EXISTS idx_confluence_comments_doc ON confluence_comments(doc_id);

CREATE TABLE IF NOT EXISTS github_comments (
    id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, seq INTEGER NOT NULL,
    author_email TEXT, body TEXT NOT NULL, created_ts INTEGER NOT NULL, reactions TEXT
);
CREATE INDEX IF NOT EXISTS idx_github_comments_doc ON github_comments(doc_id);

CREATE TABLE IF NOT EXISTS notion_comments (
    id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, seq INTEGER NOT NULL,
    author_email TEXT, body TEXT NOT NULL, created_ts INTEGER NOT NULL, reactions TEXT
);
CREATE INDEX IF NOT EXISTS idx_notion_comments_doc ON notion_comments(doc_id);

-- ── Notion: pages + databases share one table (subtype), rows are pages parented to a database ──
CREATE TABLE IF NOT EXISTS notion_pages (
    doc_id TEXT PRIMARY KEY, teamspace TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    subtype TEXT, parent_id TEXT, properties TEXT, icon TEXT, cover TEXT,
    created_ts INTEGER NOT NULL, updated_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_notion_teamspace ON notion_pages(teamspace);
CREATE INDEX IF NOT EXISTS idx_notion_parent ON notion_pages(parent_id);

-- ── S3: objects live in buckets (flat key namespace); no comments ──
CREATE TABLE IF NOT EXISTS s3_objects (
    doc_id TEXT PRIMARY KEY, bucket TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    key TEXT NOT NULL, subtype TEXT, content_type TEXT, size INTEGER,
    created_ts INTEGER NOT NULL, updated_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_s3_bucket ON s3_objects(bucket);
CREATE INDEX IF NOT EXISTS idx_s3_key ON s3_objects(bucket, key);

-- ── HubSpot: ONE polymorphic table, because the CRM API is polymorphic ──
-- `{objectType}` is a path variable (/crm/v3/objects/{objectType}) and HubSpot supports custom
-- objects, so a table per type would make every new object type a schema migration and would put
-- a source's docs in more than one table, breaking `table()`'s one-table-per-source contract.
-- HubSpot's typed properties therefore live in a JSON column: search filters can name ANY
-- property, so they compile to json_extract() rather than to fixed columns.
CREATE TABLE IF NOT EXISTS hubspot_objects (
    doc_id TEXT PRIMARY KEY, object_type TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    properties TEXT, archived INTEGER, created_ts INTEGER NOT NULL, updated_ts INTEGER,
    owner_display TEXT
);
-- (object_type, doc_id), not object_type alone: every read of this table is "one object type,
-- ordered by doc_id" — keyset paging and the search scan both are — and a single-column index
-- leaves ORDER BY to a temp b-tree that re-sorts every matching row on each page. Same lesson as
-- idx_s3_key(bucket, key): put the ordering column in the index so a page is a range seek.
CREATE INDEX IF NOT EXISTS idx_hubspot_type_doc ON hubspot_objects(object_type, doc_id);

-- Associations are bidirectional in real HubSpot, with a distinct type id per direction, so a row
-- is stored per direction and a lookup stays a plain (from_doc_id, to_type) index match.
CREATE TABLE IF NOT EXISTS hubspot_associations (
    from_doc_id TEXT NOT NULL, from_type TEXT NOT NULL,
    to_doc_id TEXT NOT NULL, to_type TEXT NOT NULL,
    assoc_category TEXT, assoc_type_id INTEGER NOT NULL, label TEXT,
    PRIMARY KEY (from_doc_id, to_doc_id, assoc_type_id)
);
CREATE INDEX IF NOT EXISTS idx_hubspot_assoc_from ON hubspot_associations(from_doc_id, to_type);

-- ── Linear: issues + their comments. Columns keep LINEAR's vocabulary, not Jira's — `state`
-- (not status), `estimate` (not story points), `branch_name` (branchName), `identifier` — so the
-- served payload can't drift toward the wrong vendor's model even though the two are close.
-- `priority` is Linear's own 0-4 integer (0 none, 1 urgent … 4 low), NOT the corpus's "P1"
-- string: the importer maps onto the API's scale, the way load_hubspot maps onto real HubSpot
-- property names. `priorityLabel` is derived from it at serve time.
CREATE TABLE IF NOT EXISTS linear_issues (
    doc_id TEXT PRIMARY KEY, team TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    identifier TEXT, state TEXT, priority INTEGER, estimate INTEGER, labels TEXT,
    project TEXT, cycle TEXT, branch_name TEXT, due_date TEXT,
    created_ts INTEGER NOT NULL, updated_ts INTEGER,
    archived_ts INTEGER, auto_archived_ts INTEGER, auto_closed_ts INTEGER,
    canceled_ts INTEGER, completed_ts INTEGER, started_ts INTEGER,
    assignee_email TEXT, assignee_display TEXT, owner_display TEXT,
    -- The parent's human identifier (ENG-123), as the corpus wrote it, and the doc_id that key
    -- was RESOLVED to at import. Both, because they answer different questions: `parent_key` is
    -- the corpus's own value, `parent_doc_id` is what the API serves.
    --
    -- The resolution has to happen once, at import, because bench identifiers are not unique:
    -- 45.4% of parent references point at a key that matches more than one issue, and `ENG-314159`
    -- alone is claimed by 218 children while being the identifier of 107 issues — a serve-time
    -- join on `identifier` would invent 23,326 edges from that key. Resolving once (first match by
    -- doc_id, the same rule `linear_issue_by_identifier` applies) makes `Issue.parent` and
    -- `Issue.children` EXACT inverses by construction rather than two lookups kept in step, and
    -- drops the 24.8% dangling references at import instead of on every request.
    parent_key TEXT, parent_doc_id TEXT,
    -- Release name as the corpus writes it (`runtime-1.19`); served as `Issue.releases`.
    release TEXT
);
-- (team, doc_id) rather than team alone: every read here is "one team, ordered by doc_id" —
-- the Relay connection pages that way — so putting the ordering column in the index makes a
-- page a range seek instead of a temp b-tree re-sort of the whole team (same lesson as
-- idx_hubspot_type_doc). ENG alone is ~23k rows.
CREATE INDEX IF NOT EXISTS idx_linear_team_doc ON linear_issues(team, doc_id);
-- The ORDER BY is always TOTAL — the sort key plus `doc_id` — so an index on the sort key ALONE
-- does not satisfy it: SQLite reports "USE TEMP B-TREE FOR LAST TERM OF ORDER BY" and sorts the
-- whole table for every page. That is not academic. When the default ordering moved from `doc_id`
-- (the primary key, already an index) to Linear's documented `createdAt`, a page at offset 35,000
-- went from 5ms to 699ms on the deployed corpus. These carry the tiebreak, so the ORDER BY is
-- read straight off the index.
CREATE INDEX IF NOT EXISTS idx_linear_created_doc ON linear_issues(created_ts, doc_id);
CREATE INDEX IF NOT EXISTS idx_linear_team_created ON linear_issues(team, created_ts, doc_id);
-- `orderBy: updatedAt` sorts on the same COALESCE the field is served with, so the index has to
-- be on the expression, not the bare column.
CREATE INDEX IF NOT EXISTS idx_linear_updated_doc
    ON linear_issues(COALESCE(updated_ts, created_ts), doc_id);
-- Superseded by idx_linear_created_doc, which has it as a prefix. Dropped explicitly because
-- `CREATE INDEX IF NOT EXISTS` matches on NAME — it would never replace a differently-defined
-- index of the same name on an already-built DB.
DROP INDEX IF EXISTS idx_linear_created_ts;
CREATE INDEX IF NOT EXISTS idx_linear_state ON linear_issues(state);
-- The by-id roots probe "can this caller see any issue carrying X" (linear_entity_has_visible).
-- Indexed so the probe seeks to that entity's issues instead of scanning all 35k until it finds
-- one the caller may read — the miss case (which is exactly the leak attempt) is the worst case.
-- Labels have no index: they live in a JSON column and json_each cannot be indexed. Measured on
-- the deployed 35k-issue corpus, that is the right trade rather than a gap — a legitimate HIT is
-- 3.7-7.6ms (it stops at the first visible carrier) and an absent id never reaches the probe at
-- all (4.1ms), while only a MISS, i.e. a caller probing a label they cannot see, pays the full
-- 81ms scan. The slow path is exactly the enumeration path.
CREATE INDEX IF NOT EXISTS idx_linear_project ON linear_issues(project);
CREATE INDEX IF NOT EXISTS idx_linear_cycle ON linear_issues(cycle);
CREATE INDEX IF NOT EXISTS idx_linear_assignee ON linear_issues(assignee_email);
CREATE INDEX IF NOT EXISTS idx_linear_author ON linear_issues(author_email);
-- `Issue.children` is "every issue whose parent_doc_id is me" — an indexed equality, not a join
-- on the non-unique identifier text.
CREATE INDEX IF NOT EXISTS idx_linear_parent_doc ON linear_issues(parent_doc_id);
CREATE INDEX IF NOT EXISTS idx_linear_release ON linear_issues(release);
-- `issue(id: "ENG-123")` resolves an identifier straight to its row; the bench's keys are NOT
-- unique (5,055 of them repeat), so this is a lookup index, never a unique constraint.
CREATE INDEX IF NOT EXISTS idx_linear_identifier ON linear_issues(identifier);
-- COVERING index for the startup reverse-index build (app.main._build_index), which reads
-- (doc_id, identifier) for every issue. Without it that query walks the doc_id primary-key index
-- and then fetches each wide row (the `content` column is a full issue description) from a
-- scattered table page: 5.6s for 35k rows on the 8GB bench DB, i.e. essentially the whole server
-- startup. As an index-only scan it is ~40ms.
CREATE INDEX IF NOT EXISTS idx_linear_doc_ident ON linear_issues(doc_id, identifier);

CREATE TABLE IF NOT EXISTS linear_comments (
    id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, seq INTEGER NOT NULL,
    author_email TEXT, body TEXT NOT NULL, created_ts INTEGER NOT NULL, reactions TEXT
);
CREATE INDEX IF NOT EXISTS idx_linear_comments_doc ON linear_comments(doc_id, seq);
-- `Query.comments` pages the whole corpus ordered by time; without this the ORDER BY
-- re-sorts all 165k bench comments in a temp b-tree on every page.
CREATE INDEX IF NOT EXISTS idx_linear_comments_ts ON linear_comments(created_ts, id);

-- Linear models a dependency as an IssueRelation with a `type` (blocks | duplicate | related).
-- One row per DIRECTION is NOT stored: `Issue.relations` and `Issue.inverseRelations` are the two
-- ends of the same row, so the row is written once from the declaring issue and read from either
-- side. `to_doc_id` is resolved at import for the same reason `parent_doc_id` is — a dangling or
-- ambiguous key must never become a relation pointing at an issue that does not exist.
CREATE TABLE IF NOT EXISTS linear_relations (
    id TEXT PRIMARY KEY, from_doc_id TEXT NOT NULL, to_doc_id TEXT NOT NULL,
    type TEXT NOT NULL, created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_linear_rel_from ON linear_relations(from_doc_id);
CREATE INDEX IF NOT EXISTS idx_linear_rel_to ON linear_relations(to_doc_id);

-- An attachment is Linear's model for any external link on an issue, which is what the bench's
-- `links` and `attachments` both are. `title` is non-null in Linear, so a bare URL gets one
-- derived from its last path segment rather than an empty string.
CREATE TABLE IF NOT EXISTS linear_attachments (
    id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, seq INTEGER NOT NULL,
    title TEXT NOT NULL, url TEXT NOT NULL, subtitle TEXT, source_type TEXT,
    created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_linear_attach_doc ON linear_attachments(doc_id, seq);

-- ── shared relationship tables (keyed by doc_id / names) ──
-- ── per-service grouping tables (name of the grouping unit + its owning ACL group) ──
CREATE TABLE IF NOT EXISTS slack_channels    (channel TEXT PRIMARY KEY, group_id TEXT);
CREATE TABLE IF NOT EXISTS gmail_mailboxes   (mailbox TEXT PRIMARY KEY, group_id TEXT);
CREATE TABLE IF NOT EXISTS gdrive_folders    (folder  TEXT PRIMARY KEY, group_id TEXT);
CREATE TABLE IF NOT EXISTS github_repos      (repo    TEXT PRIMARY KEY, group_id TEXT);
CREATE TABLE IF NOT EXISTS jira_projects     (project TEXT PRIMARY KEY, group_id TEXT);
CREATE TABLE IF NOT EXISTS confluence_spaces (space   TEXT PRIMARY KEY, group_id TEXT);
CREATE TABLE IF NOT EXISTS notion_teamspaces (teamspace TEXT PRIMARY KEY, group_id TEXT);
CREATE TABLE IF NOT EXISTS s3_buckets        (bucket  TEXT PRIMARY KEY, group_id TEXT);
CREATE TABLE IF NOT EXISTS hubspot_object_types (object_type TEXT PRIMARY KEY, group_id TEXT);
CREATE TABLE IF NOT EXISTS linear_teams      (team    TEXT PRIMARY KEY, group_id TEXT);

CREATE TABLE IF NOT EXISTS principals (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, display_name TEXT, email TEXT
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL, user_id TEXT NOT NULL, PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS doc_acl (
    doc_id TEXT NOT NULL, principal_type TEXT NOT NULL, principal_id TEXT NOT NULL,
    PRIMARY KEY (doc_id, principal_type, principal_id)
);
CREATE INDEX IF NOT EXISTS idx_acl_doc ON doc_acl(doc_id);
CREATE INDEX IF NOT EXISTS idx_acl_pid ON doc_acl(principal_id);
"""


def connect_rw(path: Path, *, busy_ms: int = 60_000) -> sqlite3.Connection:
    path = Path(path)  # accept a str path too
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Wait for a lock rather than erroring, so an in-place rebuild (build_fts) against a DB the
    # live server is reading rides through the reader's lock instead of a spurious "locked".
    if busy_ms:
        conn.execute(f"PRAGMA busy_timeout={busy_ms}")
    # Self-heal tables built before a column was added. `CREATE TABLE IF NOT EXISTS` in SCHEMA
    # below does NOT alter an existing table, so a DB created by an earlier version keeps the old
    # column set -- and then every INSERT naming the new column fails. (For github_items the
    # symptom was different but the cause identical: `CREATE INDEX IF NOT EXISTS
    # idx_github_repo_path ON github_items(repo, path)` guards only the index NAME and still
    # raises if the referenced column is missing.) Each ALTER is idempotent: it no-ops on a fresh
    # DB (table absent) and on a DB that already has the column.
    for table, column, decl in (("github_items", "path", "TEXT"),
                                ("linear_issues", "parent_key", "TEXT"),
                                ("linear_issues", "parent_doc_id", "TEXT"),
                                ("linear_issues", "release", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError:
            pass  # table absent (fresh DB) or column already present
    conn.executescript(SCHEMA)
    return conn


def connect_ro(path: Path, *, mmap_mb: int = 0, cache_mb: int = 0,
               temp_memory: bool = False, busy_ms: int = 0) -> sqlite3.Connection:
    """Open a read-only connection. The tuning knobs default to off (so tests and small
    corpora are unaffected); the serving path passes config values to keep the big DB warm:

    - ``mmap_mb``: memory-map up to this many MiB of the DB, so reads go through the OS page
      cache without per-read syscalls or a duplicated pager buffer (set >= DB size to map it
      fully; SQLite silently caps to its compile-time max). The main lever against cold reads.
    - ``cache_mb``: SQLite's own page cache size (MiB).
    - ``temp_memory``: keep transient sorts/temp b-trees in RAM (helps FTS ``ORDER BY rank``).
    - ``busy_ms``: wait this long for a lock instead of erroring — lets reads ride through an
      out-of-band writer's commit (e.g. an in-place ``build_fts`` rebuild) rather than 500ing.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if busy_ms:
        conn.execute(f"PRAGMA busy_timeout={busy_ms}")
    if cache_mb:
        conn.execute(f"PRAGMA cache_size=-{cache_mb * 1024}")  # negative => KiB, not pages
    if temp_memory:
        conn.execute("PRAGMA temp_store=MEMORY")
    if mmap_mb:
        conn.execute(f"PRAGMA mmap_size={mmap_mb * 1024 * 1024}")
    return conn


def jcol(row: sqlite3.Row, key: str, default=None):
    """Parse a JSON-valued column; returns ``default`` (or []) if empty/invalid."""
    default = [] if default is None else default
    if key not in row.keys() or not row[key]:
        return default
    try:
        return json.loads(row[key])
    except (ValueError, TypeError):
        return default


# --- ACL-aware document queries -------------------------------------------------

def _acl_clause(tbl: str, visible_ids: set[str] | None, col: str = "doc_id") -> tuple[str, list]:
    """``col`` names the column holding the doc whose ACL decides visibility — normally the row's
    own ``doc_id``, but for a HubSpot association it is the *target* (``to_doc_id``), since the
    target is the record whose existence the response would reveal."""
    if visible_ids is None:
        return "", []
    ids = list(visible_ids)
    if not ids:
        return " AND 0", []
    marks = ",".join("?" for _ in ids)
    return (f" AND EXISTS (SELECT 1 FROM doc_acl a WHERE a.doc_id = {tbl}.{col} "
            f"AND a.principal_id IN ({marks}))", ids)


def _scope(sql: str, params: list, gcol: str, container: str | None, author_email: str | None,
           not_author_email: str | None = None) -> str:
    if container is not None:
        sql += f" AND {gcol} = ?"
        params.append(container)
    if author_email is not None:
        sql += " AND author_email = ?"
        params.append(author_email)
    # The complement of an author filter — Drive's `sharedWithMe` partitions the visible set on
    # "owned by the caller" vs not, and pushing the negative half down keeps a Shared-with-me
    # listing from materializing the whole corpus to filter it in Python.
    if not_author_email is not None:
        sql += " AND author_email <> ?"
        params.append(not_author_email)
    return sql


def list_documents(conn, source_type, container=None, visible_ids=None, limit=100,
                   offset=0, author_email=None, state=None,
                   not_author_email=None) -> list[sqlite3.Row]:
    # state: only valid for source_type="github" — it's the only items table with a `state`
    # column; passing it for any other source_type raises sqlite3.OperationalError.
    tbl = table(source_type)
    sql = f"SELECT * FROM {tbl} WHERE 1=1"
    params: list = []
    sql = _scope(sql, params, grouping_col(source_type), container, author_email, not_author_email)
    if state is not None:
        sql += " AND COALESCE(state, 'open') = ?"
        params.append(state)
    clause, cparams = _acl_clause(tbl, visible_ids)
    sql += clause + " ORDER BY doc_id LIMIT ? OFFSET ?"
    params += cparams + [limit, offset]
    return conn.execute(sql, params).fetchall()


def key_successor(s: str) -> str:
    """The lexicographically-smallest string that is greater than every string with prefix
    ``s`` (increments ``s``'s last character). Used both to turn an S3 prefix filter into a
    half-open byte range (``key >= s AND key < key_successor(s)``) and, in the ListObjectsV2
    router, to build a keyset cursor that skips an entire rolled-up CommonPrefixes group in one
    bound. Undefined for an empty string — callers guard the empty-prefix case separately."""
    return s[:-1] + chr(ord(s[-1]) + 1)


def list_s3_objects(conn, bucket, *, prefix="", start_after=None, start_at=None, visible_ids=None,
                    limit=1000) -> list[sqlite3.Row]:
    """S3 ListObjectsV2's data-access path: prefix filter, keyset pagination, and ACL scoping,
    all pushed into SQL and ordered by key — so listing a big bucket costs one indexed range
    scan per page, not a whole-bucket materialize + Python sort/filter.

    The prefix filter is an explicit half-open byte range (``key >= prefix AND key <
    key_successor(prefix)``), NOT a ``LIKE prefix||'%'``: SQLite only turns a LIKE's leading
    literal into an index range when ``case_sensitive_like`` is ON, which this repo must not set
    (``list_drive_by_name`` relies on the default case-insensitive LIKE) — so a plain LIKE here
    would silently fall back to a full index/table scan regardless of match position. The byte
    range hits ``idx_s3_key(bucket, key)`` directly (leading ``bucket = ?`` equality + an
    ascending range on ``key``), giving both the WHERE and the ORDER BY a single indexed range
    scan, no full scan and no separate sort step. It's also case-SENSITIVE (the column has the
    default BINARY collation), matching real S3's byte-exact prefix matching — and needs no
    wildcard escaping, since a byte range has no wildcards to escape.

    ``start_after`` (keyset lower bound, exclusive — the last key already returned) and
    ``start_at`` (inclusive — used by the router to resume past an entire rolled-up
    CommonPrefixes group) are independent bounds and can both be combined with the prefix range."""
    sql = "SELECT * FROM s3_objects WHERE bucket = ?"
    params: list = [bucket]
    if prefix:
        sql += " AND key >= ? AND key < ?"
        params += [prefix, key_successor(prefix)]
    if start_after:
        sql += " AND key > ?"
        params.append(start_after)
    if start_at:
        sql += " AND key >= ?"
        params.append(start_at)
    clause, cparams = _acl_clause("s3_objects", visible_ids)
    sql += clause + " ORDER BY key ASC LIMIT ?"
    params += cparams + [limit]
    return conn.execute(sql, params).fetchall()


def list_hubspot_objects(conn, object_type, *, after_doc_id=None, visible_ids=None, limit=100,
                         archived=False, columns="*", prefilter=None) -> list[sqlite3.Row]:
    """One page of a CRM object type, keyset-paginated by ``doc_id``.

    HubSpot's ``after`` cursor is a *record id*, which the router maps back to a doc_id — so the
    bound is a keyset (``doc_id > ?``) rather than an OFFSET, and a page costs one indexed range
    scan regardless of how deep into the type it sits. ``archived`` splits the two views the API
    exposes: archived records are hidden unless explicitly asked for.

    ``prefilter`` is an optional ``(sql_fragment, params)`` the caller has established as a
    *necessary* condition for a match, so pushing it down can only remove rows that would have been
    rejected anyway. It exists because search must walk the whole object type to report an honest
    total, and doing that row-by-row in Python is far slower than letting SQLite reject the bulk.

    ``columns`` narrows the projection. Search has to walk the whole object type to report an
    honest ``total``, and ``content`` is by far the widest column (a note's body), so reading it for
    rows that are only being *filtered* dominates that scan — the caller passes just the columns it
    will actually read."""
    sql = f"SELECT {columns} FROM hubspot_objects WHERE object_type = ?"
    params: list = [object_type]
    if prefilter:
        frag, fparams = prefilter
        sql += f" AND {frag}"
        params += fparams
    sql += " AND archived IS NOT NULL" if archived else " AND archived IS NULL"
    if after_doc_id:
        sql += " AND doc_id > ?"
        params.append(after_doc_id)
    clause, cparams = _acl_clause("hubspot_objects", visible_ids)
    sql += clause + " ORDER BY doc_id LIMIT ?"
    params += cparams + [limit]
    return conn.execute(sql, params).fetchall()


# --- Linear: issues, their comments, and the identifier lookup ---------------------
# Linear pages a Relay connection, and the mock's `after` is the same opaque offset cursor every
# other source's page token is (see app/pagination.py), so these take an offset. The ORDER BY is
# always total — the sort column plus `doc_id` as the tiebreak — because an offset page over a
# non-total order can silently repeat or skip a row between pages.

# GraphQL `orderBy` value -> the column it sorts on.
#
# Linear's pagination docs state "By default results are ordered by createdAt field", and its
# `PaginationOrderBy` enum carries a FIELD ONLY — no direction — so the server fixes the
# direction and a client that wants the other one uses the richer `sort:` input instead.
# The direction is not documented; ASCENDING is the choice here because it is the only one that
# makes an `after` cursor stable: with newest-first, creating an issue shifts every existing
# offset by one and a mid-crawl cursor silently re-reads a row. `doc_id` breaks ties into a
# total order either way, which offset paging requires.
LINEAR_DEFAULT_ORDER_BY = "createdAt"
LINEAR_ORDER_COLUMNS = {"createdAt": "created_ts",
                        "updatedAt": "COALESCE(updated_ts, created_ts)"}


# `IssueSortInput` key -> the column it sorts on. `updatedAt` uses the same COALESCE the field
# itself is served with (an issue with no recorded edit reports its creation time), so a client
# crawling "newest first until older than X" sees a monotonic sequence rather than one that
# disagrees with the `updatedAt` it is reading.
LINEAR_SORT_COLUMNS = {"title": "title", "priority": "priority", "estimate": "estimate",
                       "createdAt": "created_ts",
                       "updatedAt": "COALESCE(updated_ts, created_ts)"}


def _linear_order(order_by: str | None, descending: bool, sort=None) -> str:
    """The ORDER BY. Always TOTAL — the sort keys plus `doc_id` — because an offset page over a
    non-total order can silently repeat or skip a row between pages.

    `sort` (Linear's `IssueSortInput`) wins over `orderBy` when both are given, matching the real
    API, where `sort` is the richer multi-key form. Accepting it and ignoring it, which an earlier
    version did, answered every sorted query in insertion order."""
    terms = []
    for entry in sort or []:
        for key, opts in (entry or {}).items():
            col = LINEAR_SORT_COLUMNS.get(key)
            if col is None:
                continue
            direction = "DESC" if (opts or {}).get("order") == "Descending" else "ASC"
            nulls = (opts or {}).get("nulls")
            tail = f" NULLS {'FIRST' if nulls == 'first' else 'LAST'}" if nulls else ""
            terms.append(f"{col} {direction}{tail}")
    if terms:
        return ", ".join(terms) + ", doc_id"
    # An ABSENT orderBy is not "unordered": Linear documents createdAt as the default, so falling
    # through to raw insertion order (`doc_id`) was a real divergence — `issues(first: 10)`
    # returned an arbitrary ten rather than the first ten by creation.
    col = LINEAR_ORDER_COLUMNS[order_by or LINEAR_DEFAULT_ORDER_BY]
    direction = "DESC" if descending else "ASC"
    # NULL updated_ts sorts last on DESC, which is where an issue with no recorded edit belongs.
    return f"{col} {direction}, doc_id"


def _linear_archived(archived: bool) -> str:
    """Linear EXCLUDES archived issues unless `includeArchived: true` is asked for. Accepting the
    argument and never applying it served archived issues to every caller who explicitly asked
    not to see them."""
    return "" if archived else " AND archived_ts IS NULL"


def list_linear_issues(conn, team=None, *, visible_ids=None, limit=50, offset=0,
                       order_by=None, descending=False, prefilter=None, sort=None,
                       archived=False) -> list[sqlite3.Row]:
    """One page of Linear issues, optionally scoped to a team.

    ``prefilter`` is an optional ``(sql_fragment, params)`` the resolver has established as a
    *necessary* condition for a match, so pushing it into SQL can only remove rows the caller
    would have rejected anyway — it exists so an `issues(filter:)` query is an indexed scan
    rather than a full materialize-then-filter in Python.
    """
    sql = "SELECT * FROM linear_issues WHERE 1=1"
    params: list = []
    if team is not None:
        sql += " AND team = ?"
        params.append(team)
    if prefilter:
        frag, fparams = prefilter
        sql += f" AND {frag}"
        params += fparams
    sql += _linear_archived(archived)
    clause, cparams = _acl_clause("linear_issues", visible_ids)
    sql += clause + f" ORDER BY {_linear_order(order_by, descending, sort)} LIMIT ? OFFSET ?"
    params += cparams + [limit, offset]
    return conn.execute(sql, params).fetchall()


def count_linear_issues(conn, team=None, *, visible_ids=None, prefilter=None,
                        archived=False) -> int:
    """Total matching issues — what ``pageInfo.hasNextPage`` is decided against."""
    sql = "SELECT COUNT(*) FROM linear_issues WHERE 1=1"
    params: list = []
    if team is not None:
        sql += " AND team = ?"
        params.append(team)
    if prefilter:
        frag, fparams = prefilter
        sql += f" AND {frag}"
        params += fparams
    sql += _linear_archived(archived)
    clause, cparams = _acl_clause("linear_issues", visible_ids)
    return conn.execute(sql + clause, params + cparams).fetchone()[0]


def linear_issue_by_identifier(conn, identifier, visible_ids=None) -> sqlite3.Row | None:
    """Resolve a human identifier (``ENG-123``) to its issue. The bench's keys are not unique
    (5,055 repeat), so this deliberately returns the first by ``doc_id`` rather than pretending
    the lookup is unambiguous — the UUID form of ``issue(id:)`` is the exact one."""
    sql = "SELECT * FROM linear_issues WHERE identifier = ?"
    params: list = [identifier]
    clause, cparams = _acl_clause("linear_issues", visible_ids)
    return conn.execute(sql + clause + " ORDER BY doc_id LIMIT 1", params + cparams).fetchone()


def list_linear_comments(conn, *, doc_id=None, visible_ids=None, limit=50, offset=0,
                         prefilter=None) -> list[sqlite3.Row]:
    """Comments on one issue, or across the corpus when ``doc_id`` is None (``Query.comments``).

    A comment row carries no ACL grant of its own; visibility is the parent issue's, so the ACL
    is applied to ``linear_issues`` through a join rather than to the comment table."""
    # The join exists ONLY to reach the parent issue's ACL, so an admin read (visible_ids None)
    # skips it: over 165k bench comments the join cost ~40ms per page for nothing.
    join = "" if visible_ids is None else " JOIN linear_issues i ON i.doc_id = c.doc_id"
    sql = f"SELECT c.* FROM linear_comments c{join} WHERE 1=1"
    params: list = []
    if doc_id is not None:
        sql += " AND c.doc_id = ?"
        params.append(doc_id)
    if prefilter:
        frag, fparams = prefilter
        sql += f" AND {frag}"
        params += fparams
    clause, cparams = _acl_clause("i", visible_ids)
    sql += clause + " ORDER BY c.created_ts, c.id LIMIT ? OFFSET ?"
    return conn.execute(sql, params + cparams + [limit, offset]).fetchall()


def count_linear_comments(conn, *, doc_id=None, visible_ids=None, prefilter=None) -> int:
    join = "" if visible_ids is None else " JOIN linear_issues i ON i.doc_id = c.doc_id"
    sql = f"SELECT COUNT(*) FROM linear_comments c{join} WHERE 1=1"
    params: list = []
    if doc_id is not None:
        sql += " AND c.doc_id = ?"
        params.append(doc_id)
    if prefilter:
        frag, fparams = prefilter
        sql += f" AND {frag}"
        params += fparams
    clause, cparams = _acl_clause("i", visible_ids)
    return conn.execute(sql + clause, params + cparams).fetchone()[0]


def linear_children(conn, parent_doc_id, *, visible_ids=None, limit=50, offset=0,
                    prefilter=None) -> list[sqlite3.Row]:
    """Sub-issues of an issue — every row whose resolved ``parent_doc_id`` is this one.

    An indexed equality on a doc_id, NOT a join on ``identifier``: bench identifiers repeat, and
    joining on them would attach one issue's children to all 107 issues that happen to share its
    key. Resolution happened once at import (see the ``parent_doc_id`` column), which is also what
    makes this the exact inverse of ``Issue.parent``."""
    sql = "SELECT * FROM linear_issues WHERE parent_doc_id = ?"
    params: list = [parent_doc_id]
    if prefilter:
        frag, fparams = prefilter
        sql += f" AND {frag}"
        params += fparams
    clause, cparams = _acl_clause("linear_issues", visible_ids)
    sql += clause + " ORDER BY created_ts, doc_id LIMIT ? OFFSET ?"
    return conn.execute(sql, params + cparams + [limit, offset]).fetchall()


def linear_relations(conn, doc_id, *, inverse=False, visible_ids=None, limit=50,
                     offset=0) -> list[sqlite3.Row]:
    """One page of an issue's relations. ``inverse=False`` is ``Issue.relations`` (rows this issue
    declared), ``inverse=True`` is ``Issue.inverseRelations`` (rows pointing AT it) — the two ends
    of the same stored row, which is why a relation is written once rather than per direction.

    ACL-scoped on the OTHER end: a relation whose counterpart the caller cannot read is omitted
    entirely rather than surfaced as an id, since its existence would disclose that issue."""
    mine, other = ("to_doc_id", "from_doc_id") if inverse else ("from_doc_id", "to_doc_id")
    clause, cparams = _acl_clause("i", visible_ids)
    sql = (f"SELECT r.* FROM linear_relations r JOIN linear_issues i ON i.doc_id = r.{other} "
           f"WHERE r.{mine} = ?{clause} ORDER BY r.created_ts, r.id LIMIT ? OFFSET ?")
    return conn.execute(sql, [doc_id, *cparams, limit, offset]).fetchall()


def linear_attachments(conn, doc_id, *, visible_ids=None, limit=50, offset=0,
                       url=None, prefilter=None) -> list[sqlite3.Row]:
    """An issue's attachments. Visibility is the parent issue's — an attachment carries no grant
    of its own — so the ACL is applied through a join, as it is for comments. ``url`` is Linear's
    own exact-match argument on this connection."""
    join = "" if visible_ids is None else " JOIN linear_issues i ON i.doc_id = a.doc_id"
    sql = f"SELECT a.* FROM linear_attachments a{join} WHERE a.doc_id = ?"
    params: list = [doc_id]
    if url is not None:
        sql += " AND a.url = ?"
        params.append(url)
    if prefilter:
        frag, fparams = prefilter
        sql += f" AND {frag}"
        params += fparams
    clause, cparams = _acl_clause("i", visible_ids)
    sql += clause + " ORDER BY a.seq LIMIT ? OFFSET ?"
    return conn.execute(sql, params + cparams + [limit, offset]).fetchall()


def linear_attachment_by_id(conn, served_id, visible_ids=None) -> sqlite3.Row | None:
    """Resolve a SERVED attachment uuid back to its row, scoped on the parent issue's ACL.

    Attachments have no app-level reverse index (they are only ever reached through their issue),
    so the served id is matched by re-deriving it over the rows of issues the caller can see —
    which also means an attachment on a hidden issue is simply not found."""
    from app import synth
    join = "" if visible_ids is None else " JOIN linear_issues i ON i.doc_id = a.doc_id"
    clause, cparams = _acl_clause("i", visible_ids)
    rows = conn.execute(f"SELECT a.* FROM linear_attachments a{join} WHERE 1=1{clause}", cparams)
    for row in rows:
        if synth.linear_attachment_id(row["id"]) == served_id:
            return row
    return None


def linear_relation_by_id(conn, served_id, visible_ids=None) -> sqlite3.Row | None:
    """Same for a relation, scoped on BOTH ends: a relation is only visible to a caller who can
    read the issues at each side of it."""
    from app import synth
    if visible_ids is None:
        rows = conn.execute("SELECT * FROM linear_relations")
    else:
        clause_a, pa = _acl_clause("a", visible_ids)
        clause_b, pb = _acl_clause("b", visible_ids)
        rows = conn.execute(
            f"SELECT r.* FROM linear_relations r "
            f"JOIN linear_issues a ON a.doc_id = r.from_doc_id "
            f"JOIN linear_issues b ON b.doc_id = r.to_doc_id WHERE 1=1{clause_a}{clause_b}",
            [*pa, *pb])
    for row in rows:
        if synth.linear_relation_id(row["id"]) == served_id:
            return row
    return None


def linear_distinct_values(conn) -> dict[str, list]:
    """The distinct entity names Linear's by-id roots have to resolve back to.

    ``@linear/sdk``'s relation accessors are lazy — ``await issue.state`` fires a fresh
    ``workflowState(id: <uuid>)`` query — and those uuids are one-way hashes of a name, so the app
    builds a reverse index at startup (see ``app.main._build_index``). Everything here is a
    ``DISTINCT`` over one column of ``linear_issues``: a handful of states and teams, a few
    thousand projects and people. Labels come out of the JSON column via ``json_each``.

    Users are returned as ``(email, display_name)`` pairs so a user reached by id shows the same
    name as one reached inline on an issue.
    """
    def col(name):
        return [r[0] for r in conn.execute(
            f"SELECT DISTINCT {name} FROM linear_issues WHERE {name} IS NOT NULL AND {name} != ''")]

    def per_team(name, default=None):
        # Workflow states and cycles are per-team entities in Linear, so their reverse map is
        # keyed on the (team, name) pair the id was derived from.
        #
        # `default` matters: the resolver SYNTHESIZES a state name for a row that has none
        # (`_state` falls back to "Todo", since Linear declares the relation non-null). That id
        # is served, so it must be resolvable — filtering NULLs out here left `workflowState(id:)`
        # answering "Entity not found" for an id the API had just handed the caller, even as
        # admin. The rule is: index exactly what is served.
        expr = f"COALESCE({name}, ?)" if default is not None else name
        params = [default] if default is not None else []
        where = "" if default is not None else f" WHERE {name} IS NOT NULL AND {name} != ''"
        return [tuple(r) for r in conn.execute(
            f"SELECT DISTINCT team, {expr} FROM linear_issues{where}", params)]

    people: dict[str, str | None] = {}
    for email_col, name_col in (("author_email", "owner_display"),
                                ("assignee_email", "assignee_display")):
        for email, display in conn.execute(
                f"SELECT DISTINCT {email_col}, {name_col} FROM linear_issues "
                f"WHERE {email_col} IS NOT NULL AND {email_col} != ''"):
            # Keep the first NON-EMPTY display name: a person can appear as an author with no
            # recorded name and as an assignee with one, and the named form must win whichever
            # order the two passes see them in.
            people[email] = display or people.get(email)
    return {
        "states": per_team("state", default=LINEAR_DEFAULT_STATE),
        "projects": col("project"),
        "cycles": per_team("cycle"),
        "labels": [r[0] for r in conn.execute(
            "SELECT DISTINCT value FROM linear_issues, json_each(COALESCE(labels, '[]'))")],
        "releases": col("release"),
        "users": sorted(people.items()),
    }


def linear_team_has_visible(conn, team, visible_ids=None) -> bool:
    """Whether the caller can see ANY issue in a team — a ``LIMIT 1`` existence check that stops
    at the first visible row, so deciding which teams to surface costs a few cheap probes instead
    of an ACL-filtered ``GROUP BY`` over every issue in the corpus. Same shape as
    :func:`drive_folder_has_visible`."""
    clause, params = _acl_clause("linear_issues", visible_ids)
    return conn.execute(f"SELECT 1 FROM linear_issues WHERE team = ?{clause} LIMIT 1",
                        [team, *params]).fetchone() is not None


# `Issue.state` is non-null in Linear, so a row with no recorded state is served this name (see
# linear_resolvers._state). It lives here because the reverse index and the visibility probe must
# agree with the resolver on it, or an id the API served becomes unresolvable.
LINEAR_DEFAULT_STATE = "Todo"


# The by-id roots (`project(id:)`, `workflowState(id:)`, …) resolve an entity that has no table
# of its own: it exists only as a column value on some issue. So "can the caller see it" means
# "can the caller see any issue carrying it", and each kind names the predicate that asks.
# Keyed exactly as app.main._build_index keys its reverse maps.
_LINEAR_ENTITY_PREDICATES = {
    "project": lambda v: ("project = ?", [v]),
    "cycle": lambda v: ("cycle = ? AND team = ?", [v[1], v[0]]),          # v = (team, name)
    # COALESCE, to match the synthesized default above: a caller reading an issue with no state
    # must be able to resolve the state id that issue served them.
    "state": lambda v: (f"COALESCE(state, '{LINEAR_DEFAULT_STATE}') = ? AND team = ?",
                        [v[1], v[0]]),                                    # v = (team, name)
    # A person is reachable as either end of an issue.
    "user": lambda v: ("(author_email = ? OR assignee_email = ?)", [v[0], v[0]]),  # v = (email, _)
    "label": lambda v: (
        "EXISTS (SELECT 1 FROM json_each(COALESCE(labels, '[]')) WHERE value = ?)", [v]),
    "release": lambda v: ("release = ?", [v]),
}


def linear_entity_has_visible(conn, kind: str, value, visible_ids=None) -> bool:
    """Whether the caller can see ANY issue carrying this project / cycle / state / person / label.

    Without this the by-id roots are an existence oracle over the whole corpus: their reverse
    index is an unfiltered ``DISTINCT`` built at startup, so a caller who cannot read an issue
    could still resolve that issue's project name, label, cycle, workflow state, and assignee —
    column values on a row they are denied. ``teams`` already hides a team the caller sees no
    issue in; this applies the same rule to the other five roots.

    A ``LIMIT 1`` probe, so it stops at the first visible carrier rather than counting."""
    build = _LINEAR_ENTITY_PREDICATES.get(kind)
    if build is None:
        raise ValueError(f"unknown linear entity kind {kind!r}")
    frag, params = build(value)
    clause, cparams = _acl_clause("linear_issues", visible_ids)
    return conn.execute(f"SELECT 1 FROM linear_issues WHERE {frag}{clause} LIMIT 1",
                        [*params, *cparams]).fetchone() is not None


def linear_team_issue_counts(conn, visible_ids=None) -> dict[str, int]:
    """team -> visible issue count, in one grouped scan — ``Team.issueCount`` for a whole page of
    teams without a COUNT(*) per team."""
    clause, cparams = _acl_clause("linear_issues", visible_ids)
    rows = conn.execute(
        f"SELECT team, COUNT(*) FROM linear_issues WHERE 1=1{clause} GROUP BY team", cparams)
    return {r[0]: r[1] for r in rows}


def hubspot_associations(conn, from_doc_id, to_type, *, after_to_doc_id=None, visible_ids=None,
                         limit=500) -> list[sqlite3.Row]:
    """One page of associations from a CRM record to records of ``to_type``, ACL-scoped on the
    target. Keyset-paginated by ``to_doc_id`` for the same reason the listings are: the API's
    cursor is the last record id the caller saw, and a record past the first page must stay
    reachable."""
    sql = "SELECT * FROM hubspot_associations WHERE from_doc_id = ? AND to_type = ?"
    params: list = [from_doc_id, to_type]
    if after_to_doc_id:
        sql += " AND to_doc_id > ?"
        params.append(after_to_doc_id)
    clause, cparams = _acl_clause("hubspot_associations", visible_ids, col="to_doc_id")
    sql += clause + " ORDER BY to_doc_id LIMIT ?"
    params += cparams + [limit]
    return conn.execute(sql, params).fetchall()


def list_drive_folder(conn, folder, visible_ids=None, limit=100, offset=0) -> list[sqlite3.Row]:
    """Non-trashed files directly in a Drive folder — SQL-scoped + SQL-paginated, so listing a
    big folder costs one page of rows per request, not a full-corpus scan on every page."""
    sql = "SELECT * FROM gdrive_files WHERE folder = ? AND COALESCE(trashed, 0) = 0"
    params: list = [folder]
    clause, cparams = _acl_clause("gdrive_files", visible_ids)
    # No ORDER BY: the folder index already yields a stable order for pagination, and adding
    # ORDER BY doc_id forces a per-page sort of the whole folder (≈30x slower on a big folder).
    sql += clause + " LIMIT ? OFFSET ?"
    params += cparams + [limit, offset]
    return conn.execute(sql, params).fetchall()


def list_drive_by_name(conn, name_substr, container=None, visible_ids=None,
                       limit=100_000, offset=0) -> list[sqlite3.Row]:
    """Non-trashed Drive files whose title contains ``name_substr`` (Drive's ``name contains 'X'``),
    optionally within a folder — the SQL path for a name lookup. Without it the endpoint listed the
    WHOLE corpus (~25k rows, ~1.6s) then substring-matched in Python; a title LIKE builds only the
    matches (~14ms). LIKE wildcards in the needle are escaped so they stay literal."""
    needle = (name_substr or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    # SQLite LIKE is case-insensitive for ASCII by default (matching Drive's case-insensitive
    # `name contains`); no lower() wrapper, which would force a per-row scan.
    sql = ("SELECT * FROM gdrive_files WHERE COALESCE(trashed, 0) = 0 "
           "AND title LIKE ? ESCAPE '\\'")
    params: list = [f"%{needle}%"]
    if container is not None:
        sql += " AND folder = ?"
        params.append(container)
    clause, cparams = _acl_clause("gdrive_files", visible_ids)
    sql += clause + " LIMIT ? OFFSET ?"
    params += cparams + [limit, offset]
    return conn.execute(sql, params).fetchall()


def count_drive_folder(conn, folder, visible_ids=None) -> int:
    sql = "SELECT COUNT(*) FROM gdrive_files WHERE folder = ? AND COALESCE(trashed, 0) = 0"
    params: list = [folder]
    clause, cparams = _acl_clause("gdrive_files", visible_ids)
    sql += clause
    params += cparams
    return conn.execute(sql, params).fetchone()[0]


def drive_folder_has_visible(conn, folder, visible_ids=None) -> bool:
    """Whether the caller can see any file in a folder — a ``LIMIT 1`` existence check (stops at
    the first visible file), so deciding which folders to surface is a couple of cheap probes."""
    clause, params = _acl_clause("gdrive_files", visible_ids)
    sql = f"SELECT 1 FROM gdrive_files WHERE folder = ?{clause} LIMIT 1"
    return conn.execute(sql, [folder, *params]).fetchone() is not None


def count_documents(conn, source_type, container=None, visible_ids=None, author_email=None,
                    state=None) -> int:
    # state: only valid for source_type="github" — it's the only items table with a `state`
    # column; passing it for any other source_type raises sqlite3.OperationalError.
    tbl = table(source_type)
    sql = f"SELECT COUNT(*) FROM {tbl} WHERE 1=1"
    params: list = []
    sql = _scope(sql, params, grouping_col(source_type), container, author_email)
    if state is not None:
        sql += " AND COALESCE(state, 'open') = ?"
        params.append(state)
    clause, cparams = _acl_clause(tbl, visible_ids)
    sql += clause
    params += cparams
    return conn.execute(sql, params).fetchone()[0]


def get_document(conn, source_type, doc_id, visible_ids=None) -> sqlite3.Row | None:
    tbl = table(source_type)
    sql = f"SELECT * FROM {tbl} WHERE doc_id = ?"
    params: list = [doc_id]
    clause, cparams = _acl_clause(tbl, visible_ids)
    sql += clause
    params += cparams
    return conn.execute(sql, params).fetchone()


# --- full-text search (FTS5) ----------------------------------------------------
# A single FTS5 index over every source's title+content, so search is fast even on the
# millions-of-rows augmented corpus (a LIKE scan would be a full-table scan). Built by the
# importers via build_fts(); search falls back to LIKE if the index/FTS5 isn't present.

def _fts5_ok(conn) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError as e:
        # Only "FTS5 not compiled in" means genuinely-unsupported → LIKE fallback. A different
        # OperationalError (e.g. "database is locked") must surface, not masquerade as no-FTS5
        # and make build_fts a silent no-op.
        if "no such module" in str(e).lower() or "fts5" in str(e).lower():
            return False
        raise


def _has_fts(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='docs_fts'").fetchone() is not None


def _src_tag(source_type: str) -> str:
    """A single collision-free token for the indexed ``src`` column. unicode61 splits on
    non-alphanumerics, so strip underscores (``google_drive`` -> ``srcgoogledrive``)."""
    return "src" + source_type.replace("_", "")


def build_fts(conn) -> bool:
    """(Re)build the docs_fts full-text index over all source tables. No-op (returns False) if
    the SQLite build lacks FTS5 — search then uses the LIKE fallback.

    ``src`` is an **indexed** column holding a per-source tag token, so a search can intersect
    the source's posting list with the term posting lists (``src:srcjira AND "latency"``)
    instead of ranking every source's matches and post-filtering — the latter made a
    minority-source search (e.g. Jira for a term common in Slack) scan far past other sources."""
    if not _fts5_ok(conn):
        return False
    conn.execute("DROP TABLE IF EXISTS docs_fts")
    # porter stemming (over unicode61) so a search matches morphological variants the way real
    # Slack/Gmail search do — "deletion" finds "deletions", "embedding" finds "embeddings". The
    # tokenizer applies to every column including the src tag, but that is safe: the stored tag and
    # the src: query term stem identically, and the 6 tags don't collide under porter.
    conn.execute("CREATE VIRTUAL TABLE docs_fts USING fts5("
                 "doc_id UNINDEXED, src, title, content, tokenize='porter unicode61')")
    # Commit per source rather than once at the end: on an in-place rebuild of a large DB this
    # keeps each writer lock window to one source's index, so a concurrent reader (the live
    # server, with a busy_timeout) rides through instead of blocking on a single multi-GB commit.
    for src, tbl in SOURCE_TABLE.items():
        conn.execute(f"INSERT INTO docs_fts(doc_id, src, title, content) "
                     f"SELECT doc_id, '{_src_tag(src)}', title, content FROM {tbl}")
        conn.commit()
    return True


def fts_add_docs(conn, source_type: str, doc_ids: list[str]) -> int:
    """Incrementally (re)index specific docs in ``docs_fts`` — delete-then-insert per doc_id so it is
    idempotent (an upsert). Used by append imports so a small add doesn't trigger a full rebuild over
    the whole corpus. No-op (returns 0) if the FTS index isn't present or ``doc_ids`` is empty."""
    if not doc_ids or not _has_fts(conn):
        return 0
    tbl, tag = table(source_type), _src_tag(source_type)
    n = 0
    for i in range(0, len(doc_ids), 900):
        chunk = doc_ids[i:i + 900]
        marks = ",".join("?" for _ in chunk)
        conn.execute(f"DELETE FROM docs_fts WHERE doc_id IN ({marks})", chunk)
        conn.execute(f"INSERT INTO docs_fts(doc_id, src, title, content) "
                     f"SELECT doc_id, '{tag}', title, content FROM {tbl} WHERE doc_id IN ({marks})",
                     chunk)
        n += len(chunk)
    conn.commit()
    return n


def _fts_has_src(conn) -> bool:
    """True if docs_fts carries the indexed ``src`` column (new schema). Lets the query layer
    use the fast source-intersection path when the index has been rebuilt, and fall back to the
    legacy ``source_type`` post-filter otherwise — so new code runs against an old index too."""
    try:
        return any(r[1] == "src" for r in conn.execute("PRAGMA table_info(docs_fts)"))
    except sqlite3.OperationalError:
        return False


def _fts_match(query: str, source_type: str | None, has_src: bool, phrase: bool = False) -> str:
    """A safe FTS5 MATCH string: alnum tokens, each quoted and ANDed. When the index is
    source-aware, prefix an indexed ``src:`` filter so only that source's postings are scanned
    (the term tokens still match title/content — the src column holds only the tag token).

    ``phrase=True`` requires the tokens ADJACENT (an FTS5 phrase) instead of ANDed anywhere — used
    by grep-style callers where the pattern is a literal string: an AND over the tokens buries the
    exact match under coincidental docs that merely contain all the words scattered."""
    toks = re.findall(r"\w+", (query or "").lower())
    if not toks:
        return ""
    body = ('"' + " ".join(toks) + '"') if (phrase and len(toks) > 1) \
        else " AND ".join(f'"{t}"' for t in toks)
    if has_src and source_type:
        return f"src:{_src_tag(source_type)} AND ({body})"
    return body


def search_documents(conn, query, source_type=None, visible_ids=None, limit=25, offset=0,
                     container=None, phrase=False, order_by=None) -> list[sqlite3.Row]:
    """Keyword search over title + content within one source (FTS5-ranked; LIKE fallback),
    optionally scoped to one grouping unit (``container``, e.g. a Jira project / GitHub repo).
    ``phrase=True`` matches the query tokens adjacently (for literal grep-style lookups) and ranks
    docs that contain the query as a literal substring ABOVE coincidental token-adjacency hits.
    ``order_by`` selects result ordering: ``None`` = relevance (bm25, the Slack ``sort=score``
    default); ``"recency"`` / ``"recency_asc"`` = by the doc's own timestamp (Slack ``sort=timestamp``),
    newest- or oldest-first — the query still filters, only the ORDER differs."""
    tbl = table(source_type)
    cont_sql, cont_p = "", []
    if container is not None:
        cont_sql, cont_p = f" AND {{a}}.{grouping_col(source_type)} = ?", [container]
    if _has_fts(conn):
        has_src = _fts_has_src(conn)
        m = _fts_match(query, source_type, has_src, phrase=phrase)
        if not m:
            return []
        clause, cparams = _acl_clause("t", visible_ids)
        src_sql = "" if has_src else " AND docs_fts.source_type = ?"
        src_p = [] if has_src else [source_type]
        # For a phrase search, tier the results: docs literally containing the query string first
        # (bm25 next as the tiebreak). FTS tokenization drops punctuation, so "upload.csv" and
        # "upload csv" tokenize identically and bm25 can't tell them apart — the one doc that
        # actually contains "upload.csv" would otherwise sink beneath hundreds of "upload csv"
        # mentions. instr runs only over the (already phrase-narrowed) matches, so it's cheap.
        order_sql, order_p = "docs_fts.rank", []
        lit = (query or "").strip()
        if order_by in ("recency", "recency_asc"):
            # Slack sort=timestamp: order matches by the message's own ts, not relevance. NULL
            # created_ts (a synthesized ts) sorts last on desc / first on asc — an acceptable edge.
            direction = "ASC" if order_by == "recency_asc" else "DESC"
            order_sql = f"t.created_ts {direction}, docs_fts.rank"
        # Boost docs containing the query as a literal substring, but ONLY when the query has
        # punctuation joining word chars (upload.csv, DOCS-210, a/b): that's exactly when the
        # tokenizer splits one literal into pieces and the exact match sinks under coincidental
        # "upload csv"/"upload-csv" hits. This surfaces it first whether the client quoted the query
        # (mirage's grep push-down) or not (the MCP slack/gmail search sends bare terms). Plain
        # multi-word queries ("the meeting") gain nothing from it and would pay a full instr scan
        # over tens of thousands of matches, so the punctuation test gates them out. Only for
        # relevance ordering — sort=timestamp is a pure recency order.
        elif lit and re.search(r"\w[^\w\s]\w", lit):
            order_sql = ("(instr(lower(t.content), lower(?)) > 0 "
                         "OR instr(lower(t.title), lower(?)) > 0) DESC, docs_fts.rank")
            order_p = [lit, lit]
        sql = (f"SELECT t.* FROM docs_fts JOIN {tbl} t ON t.doc_id = docs_fts.doc_id "
               f"WHERE docs_fts MATCH ?{src_sql}{cont_sql.format(a='t')}{clause} "
               f"ORDER BY {order_sql} LIMIT ? OFFSET ?")
        return conn.execute(
            sql, [m, *src_p, *cont_p, *cparams, *order_p, limit, offset]).fetchall()
    like = f"%{query}%"
    sql = f"SELECT * FROM {tbl} WHERE (title LIKE ? OR content LIKE ?){cont_sql.format(a=tbl)}"
    params: list = [like, like, *cont_p]
    clause, cparams = _acl_clause(tbl, visible_ids)
    sql += clause + " ORDER BY (CASE WHEN title LIKE ? THEN 0 ELSE 1 END), doc_id LIMIT ? OFFSET ?"
    params += cparams + [like, limit, offset]
    return conn.execute(sql, params).fetchall()


def count_search(conn, query, source_type, visible_ids=None, cap=1000, container=None,
                 phrase=False) -> int:
    """Count matches for a search (ACL-filtered), bounded by ``cap`` so a very common term
    doesn't scan the whole corpus — mirrors real search APIs capping the reported total.
    ``phrase`` must match the corresponding ``search_documents`` call so the reported total is
    consistent with the rows returned (an AND-count would overstate a phrase search)."""
    tbl = table(source_type)
    cont_sql, cont_p = "", []
    if container is not None:
        cont_sql, cont_p = f" AND {{a}}.{grouping_col(source_type)} = ?", [container]
    if _has_fts(conn):
        has_src = _fts_has_src(conn)
        m = _fts_match(query, source_type, has_src, phrase=phrase)
        if not m:
            return 0
        clause, cparams = _acl_clause("t", visible_ids)
        src_sql = "" if has_src else " AND docs_fts.source_type = ?"
        src_p = [] if has_src else [source_type]
        sql = (f"SELECT COUNT(*) FROM (SELECT t.doc_id FROM docs_fts JOIN {tbl} t "
               f"ON t.doc_id = docs_fts.doc_id WHERE docs_fts MATCH ?{src_sql}"
               f"{cont_sql.format(a='t')}{clause} LIMIT ?)")
        return conn.execute(sql, [m, *src_p, *cont_p, *cparams, cap]).fetchone()[0]
    like = f"%{query}%"
    clause, cparams = _acl_clause(tbl, visible_ids)
    sql = (f"SELECT COUNT(*) FROM (SELECT doc_id FROM {tbl} WHERE (title LIKE ? OR content LIKE ?)"
           f"{cont_sql.format(a=tbl)}{clause} LIMIT ?)")
    return conn.execute(sql, [like, like, *cont_p, *cparams, cap]).fetchone()[0]


def children(conn, source_type, parent_id, visible_ids=None, limit=1000, offset=0) -> list[sqlite3.Row]:
    """Child documents (jira subtasks / confluence child pages) of a parent doc."""
    tbl = table(source_type)
    sql = f"SELECT * FROM {tbl} WHERE parent_id = ?"
    params: list = [parent_id]
    clause, cparams = _acl_clause(tbl, visible_ids)
    sql += clause + " ORDER BY doc_id LIMIT ? OFFSET ?"
    params += cparams + [limit, offset]
    return conn.execute(sql, params).fetchall()


# --- slack threading ------------------------------------------------------------

def slack_created_bounds(conn, channel) -> sqlite3.Row:
    """Cheap aggregate for a channel's ``created`` (see routers.slack._channel_created): the
    earliest explicit ``created_ts``, the row count, and how many rows carry a ``created_ts``.
    A single indexed aggregate — no per-row transfer — so it stays fast on huge channels."""
    return conn.execute(
        "SELECT MIN(created_ts) AS min_ts, COUNT(*) AS total, COUNT(created_ts) AS have "
        "FROM slack_messages WHERE channel = ?", (channel,)).fetchone()


def list_slack_top_level(conn, channel, visible_ids=None, limit=100, offset=0,
                         ts_lo=None, ts_hi=None) -> list[sqlite3.Row]:
    """Top-level (thread-root/standalone) messages in a channel. ``ts_lo``/``ts_hi`` bound
    ``created_ts`` (seconds) for a time-windowed conversations.history — the SQL date filter so a
    day window doesn't materialize the whole channel (~17k rows) then filter in Python. Bounds
    should be widened by ±1s (the public ts carries a sub-second fraction); the caller re-checks the
    exact float window. created_ts is NOT NULL (guaranteed at import), so this is a plain indexed
    range — no NULL branch, which keeps the idx_slack_channel_ts range-seek."""
    sql = "SELECT * FROM slack_messages WHERE channel = ? AND thread_seq = 0"
    params: list = [channel]
    if ts_lo is not None or ts_hi is not None:
        lo = ts_lo if ts_lo is not None else -(1 << 62)
        hi = ts_hi if ts_hi is not None else (1 << 62)
        sql += " AND created_ts >= ? AND created_ts <= ?"
        params += [lo, hi]
    clause, cparams = _acl_clause("slack_messages", visible_ids)
    sql += clause + " ORDER BY doc_id LIMIT ? OFFSET ?"
    params += cparams + [limit, offset]
    return conn.execute(sql, params).fetchall()


def count_slack_top_level(conn, channel, visible_ids=None) -> int:
    sql = "SELECT COUNT(*) FROM slack_messages WHERE channel = ? AND thread_seq = 0"
    params: list = [channel]
    clause, cparams = _acl_clause("slack_messages", visible_ids)
    sql += clause
    params += cparams
    return conn.execute(sql, params).fetchone()[0]


def list_slack_channel_messages(conn, channel, visible_ids=None) -> list[sqlite3.Row]:
    """Every visible message in a channel (roots AND replies). Used by conversations.replies to
    resolve a ts that may belong to a reply (e.g. a search hit landed on one), since ts is
    synthesized and can't be queried directly."""
    sql = "SELECT * FROM slack_messages WHERE channel = ?"
    params: list = [channel]
    clause, cparams = _acl_clause("slack_messages", visible_ids)
    sql += clause + " ORDER BY thread_id, thread_seq"
    params += cparams
    return conn.execute(sql, params).fetchall()


def list_gmail_in_range(conn, mailbox, ts_lo, ts_hi, visible_ids=None,
                        limit=100_000, offset=0) -> list[sqlite3.Row]:
    """Gmail messages whose ``created_ts`` is in ``[ts_lo, ts_hi)`` (either bound may be None for
    open-ended), newest first. The SQL date filter for a date-scoped listing (``ls /gmail/<label>/
    <date>``): without it the endpoint materialized the WHOLE mailbox (~100k rows) and filtered in
    Python. gmail ``created_ts`` is fully populated, so this covers every message."""
    sql = "SELECT * FROM gmail_messages WHERE 1=1"
    params: list = []
    if ts_lo is not None:
        sql += " AND created_ts >= ?"
        params.append(ts_lo)
    if ts_hi is not None:
        sql += " AND created_ts < ?"
        params.append(ts_hi)
    if mailbox is not None:
        sql += " AND mailbox = ?"
        params.append(mailbox)
    clause, cparams = _acl_clause("gmail_messages", visible_ids)
    # created_ts DESC = newest-first (real Gmail's messages.list order); doc_id breaks ties into a
    # stable TOTAL order so keyset-free offset pagination can't dupe/skip rows across pages.
    sql += clause + " ORDER BY created_ts DESC, doc_id LIMIT ? OFFSET ?"
    params += cparams + [limit, offset]
    return conn.execute(sql, params).fetchall()


def slack_messages_at_created_ts(conn, channel, created_ts, visible_ids=None) -> list[sqlite3.Row]:
    """Visible channel messages whose stored ``created_ts`` equals ``created_ts`` — the fast path for
    conversations.replies resolving a ts. A message's public ts has ``created_ts`` as its integer
    part (see the router's ``_msg_ts``), so a client-supplied ts narrows to the handful of rows at
    that second instead of loading the whole channel (eng-ml alone is ~340k rows → seconds). Only
    messages with a NULL ``created_ts`` (ts synthesized from the doc id) miss this; the caller falls
    back to the full scan for those."""
    sql = "SELECT * FROM slack_messages WHERE channel = ? AND created_ts = ?"
    params: list = [channel, created_ts]
    clause, cparams = _acl_clause("slack_messages", visible_ids)
    sql += clause + " ORDER BY thread_id, thread_seq"
    params += cparams
    return conn.execute(sql, params).fetchall()


def slack_reply_count(conn, root_doc_id, visible_ids=None) -> int:
    sql = "SELECT COUNT(*) FROM slack_messages WHERE thread_id = ? AND thread_seq > 0"
    params: list = [root_doc_id]
    clause, cparams = _acl_clause("slack_messages", visible_ids)
    sql += clause
    params += cparams
    return conn.execute(sql, params).fetchone()[0]


def slack_channels_for_principals(conn, principals) -> set[str]:
    """Channels with at least one doc granted to any of ``principals``. Starts from the
    principal-indexed ``doc_acl`` (idx_acl_pid) instead of scanning the whole slack table, so
    it's cheap even at millions of rows — used to list a non-admin caller's visible channels."""
    principals = list(principals)
    if not principals:
        return set()
    marks = ",".join("?" for _ in principals)
    rows = conn.execute(
        f"SELECT DISTINCT d.channel FROM doc_acl a JOIN slack_messages d ON d.doc_id = a.doc_id "
        f"WHERE a.principal_id IN ({marks})", principals)
    return {r[0] for r in rows}


def slack_reply_authors(conn, root_doc_id, visible_ids=None) -> list[str]:
    """Distinct reply-author emails in a thread, in reply order (for reply_users)."""
    sql = "SELECT author_email FROM slack_messages WHERE thread_id = ? AND thread_seq > 0"
    params: list = [root_doc_id]
    clause, cparams = _acl_clause("slack_messages", visible_ids)
    sql += clause + " ORDER BY thread_seq"
    params += cparams
    seen: list[str] = []
    for r in conn.execute(sql, params):
        if r[0] and r[0] not in seen:
            seen.append(r[0])
    return seen


def slack_thread(conn, root_doc_id, visible_ids=None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM slack_messages WHERE thread_id = ?"
    params: list = [root_doc_id]
    clause, cparams = _acl_clause("slack_messages", visible_ids)
    sql += clause + " ORDER BY thread_seq"
    params += cparams
    return conn.execute(sql, params).fetchall()


def gmail_thread(conn, thread_id, visible_ids=None) -> list[sqlite3.Row]:
    """All messages in a Gmail thread (root + replies), ordered, ACL-filtered."""
    sql = "SELECT * FROM gmail_messages WHERE thread_id = ?"
    params: list = [thread_id]
    clause, cparams = _acl_clause("gmail_messages", visible_ids)
    sql += clause + " ORDER BY thread_seq"
    params += cparams
    return conn.execute(sql, params).fetchall()


# --- GitHub file items (kind='file') ----------------------------------------

def list_repo_files(conn, repo, visible_ids=None, limit=10_000, offset=0) -> list[sqlite3.Row]:
    clause, cp = _acl_clause("github_items", visible_ids)
    sql = ("SELECT * FROM github_items WHERE repo = ? AND kind = 'file'" + clause +
           " ORDER BY path LIMIT ? OFFSET ?")
    return conn.execute(sql, [repo, *cp, limit, offset]).fetchall()


def count_repo_files(conn, repo, visible_ids=None) -> int:
    clause, cp = _acl_clause("github_items", visible_ids)
    return conn.execute("SELECT COUNT(*) FROM github_items WHERE repo = ? AND kind = 'file'" + clause,
                        [repo, *cp]).fetchone()[0]


def get_repo_file(conn, repo, path, visible_ids=None) -> sqlite3.Row | None:
    clause, cp = _acl_clause("github_items", visible_ids)
    return conn.execute("SELECT * FROM github_items WHERE repo = ? AND kind = 'file' AND path = ?" + clause,
                        [repo, path, *cp]).fetchone()


# --- grouping units (channels/mailboxes/folders/repos/projects/spaces) & principals ---

def list_containers(conn, source_type) -> list[sqlite3.Row]:
    """List a service's grouping units as rows with `name` + `group_id` (uniform API)."""
    gtable, gcol = GROUPING[source_type]
    return conn.execute(
        f"SELECT {gcol} AS name, group_id FROM {gtable} ORDER BY {gcol}").fetchall()


def get_container(conn, source_type, name) -> sqlite3.Row | None:
    gtable, gcol = GROUPING[source_type]
    return conn.execute(
        f"SELECT {gcol} AS name, group_id FROM {gtable} WHERE {gcol} = ?", (name,)).fetchone()


def list_users(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, display_name, email FROM principals WHERE type = 'user' ORDER BY id").fetchall()


def get_user(conn, email) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, display_name, email FROM principals WHERE type = 'user' AND id = ?",
        (email,)).fetchone()


def user_group_ids(conn, email) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT group_id FROM group_members WHERE user_id = ?", (email,)).fetchall()]


def group_members(conn, group_id) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.id, p.display_name, p.email FROM group_members gm "
        "JOIN principals p ON p.id = gm.user_id WHERE gm.group_id = ? ORDER BY p.id",
        (group_id,)).fetchall()


def all_user_emails(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT id FROM principals WHERE type = 'user' ORDER BY id")]


def distinct_slack_author_emails(conn) -> list[str]:
    """Every author on a Slack message — the display-only speakers/bots (e.g. deploybot@…) that
    aren't org principals but still need to resolve via users.info. Scanned once and cached by
    the caller (a full-table DISTINCT)."""
    return [r[0] for r in conn.execute("SELECT DISTINCT author_email FROM slack_messages")]


# --- ACL grants (container/doc scoped) ------------------------------------------

def container_grants(conn, source_type, container) -> list[sqlite3.Row]:
    tbl, gcol = table(source_type), grouping_col(source_type)
    return conn.execute(
        f"SELECT DISTINCT a.principal_type, a.principal_id FROM doc_acl a "
        f"JOIN {tbl} d ON d.doc_id = a.doc_id WHERE d.{gcol} = ?", (container,)).fetchall()


def container_has_public(conn, source_type, container) -> bool:
    tbl, gcol = table(source_type), grouping_col(source_type)
    return conn.execute(
        f"SELECT 1 FROM doc_acl a JOIN {tbl} d ON d.doc_id = a.doc_id "
        f"WHERE d.{gcol} = ? AND a.principal_type = 'org' LIMIT 1", (container,)).fetchone() is not None


def doc_grants(conn, doc_id) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT principal_type, principal_id FROM doc_acl WHERE doc_id = ? "
        "ORDER BY principal_type, principal_id", (doc_id,)).fetchall()


def docs_with_grants(conn, doc_ids: list[str]) -> set[str]:
    """The subset of ``doc_ids`` that have at least one ACL grant — one query (chunked to stay
    under SQLite's variable limit) instead of a per-doc ``doc_grants`` call when building a list."""
    out: set[str] = set()
    for i in range(0, len(doc_ids), 900):
        chunk = doc_ids[i:i + 900]
        marks = ",".join("?" for _ in chunk)
        out.update(r[0] for r in conn.execute(
            f"SELECT DISTINCT doc_id FROM doc_acl WHERE doc_id IN ({marks})", chunk).fetchall())
    return out


def _expand_grants(conn, grants) -> set[str] | None:
    emails: set[str] = set()
    for g in grants:
        ptype, pid = g["principal_type"], g["principal_id"]
        if ptype == "org":
            return None
        if ptype == "group":
            emails.update(m["email"] for m in group_members(conn, pid))
        elif ptype == "user":
            emails.add(pid)
    return emails


def container_member_emails(conn, source_type, container) -> set[str] | None:
    return _expand_grants(conn, container_grants(conn, source_type, container))


def doc_member_emails(conn, doc_id) -> set[str] | None:
    return _expand_grants(conn, doc_grants(conn, doc_id))


# --- comments -------------------------------------------------------------------

def doc_comments(conn, source_type, doc_id) -> list[sqlite3.Row]:
    tbl = COMMENT_TABLE.get(source_type)
    if tbl is None:
        return []
    return conn.execute(
        f"SELECT id, seq, author_email, body, created_ts, reactions FROM {tbl} "
        "WHERE doc_id = ? ORDER BY seq", (doc_id,)).fetchall()
