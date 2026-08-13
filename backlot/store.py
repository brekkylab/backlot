"""Read-only SQLite access layer.

One table per service, with that service's own columns and its own grouping-unit table
(``slack_channels``, ``github_repos``, …) — never one crammed ``documents`` table, so a column
one service needs never lands on another's rows. The principal / group-membership relationship
tables are shared, keyed by names that ARE globally unique. ACL grants are not: each source has
its own ``<source>_acl`` table (see ``ACL_TABLE``), because ``doc_id`` is unique only *within* a
source — a shared table keyed on it let two documents in different sources that happened to share
an id merge their grants.

Every doc table carries the same four core columns (``doc_id, author_email, title, content``)
plus its grouping column, which is what keeps listing / ACL / pagination uniform via the
``GROUPING`` registry. Every listing takes ``visible_ids``: ``None`` = admin, otherwise results
are filtered to docs whose ACL grants intersect it. JSON columns are TEXT — read with :func:`jcol`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path

from backlot import synth

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
    "fireflies": "fireflies_transcripts",
}


def table(source_type: str) -> str:
    try:
        return SOURCE_TABLE[source_type]
    except KeyError:
        raise ValueError(f"unknown source_type {source_type!r}")


# source_type -> its child-rows table. For most services those child rows ARE comments; for
# Fireflies they are the transcript's sentences, which are not comments but are exactly "the
# child rows of a doc in this source" — so they reuse this slot rather than adding a parallel
# mechanism. Every table here therefore shares the child-row column contract
# (id, doc_id, seq, author_email, body, created_ts, reactions) that :func:`doc_comments` reads,
# and adds its own columns beside it (see fireflies_sentences).
COMMENT_TABLE = {
    "jira": "jira_comments",
    "confluence": "confluence_comments",
    "github": "github_comments",
    "notion": "notion_comments",
    "linear": "linear_comments",
    "fireflies": "fireflies_sentences",
}


def comment_table(source_type: str) -> str | None:
    return COMMENT_TABLE.get(source_type)


# Each source's ACL. Per source, not one shared table, because `doc_id` is per-source too (see
# test_two_sources_may_share_a_doc_id): keyed corpus-wide, two documents that merely share an id
# shared their grants and the union was enforced.
ACL_TABLE = {src: f"{src}_acl" for src in SOURCE_TABLE}


def acl_table(source_type: str) -> str:
    try:
        return ACL_TABLE[source_type]
    except KeyError:
        raise ValueError(f"unknown source_type {source_type!r}")


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
    # Fireflies groups transcripts by `channel` — its own grouping concept, and one of the
    # documented `transcripts(channel_id:)` filters — so container->group needs no per-source code.
    "fireflies": ("fireflies_channels", "channel"),
}


def grouping_table(source_type: str) -> str:
    return GROUPING[source_type][0]


def grouping_col(source_type: str) -> str:
    return GROUPING[source_type][1]


# source_type -> (served-id column, seed function, uniqueness scope). The column holds an id
# ASSIGNED at import (see backlot.importer.byo) rather than derived by hashing at serve time: a
# hash into any fixed range collides by the birthday bound, and the reverse map this replaces
# resolved a collision last-writer-wins, leaving one document unreachable at its own id (#51). The
# seed function is always `(doc_id) -> a candidate value`, so an assignment method can probe it
# uniformly regardless of source.
#
# `scope` is the columns the assignment probe must hold fixed for the served value to be
# unambiguous in BACKLOT'S OWN lookup — not necessarily the vendor's own uniqueness rule. It is
# `None` when Backlot resolves the id with no container (a flat column/index lookup), or the
# source's own GROUPING column when Backlot's own lookup is already scoped to one container (a
# GitHub number is looked up within its repo; a Jira key's numeric suffix within its project — see
# `synth.jira_key_number`, since the key's PREFIX is a fact about the container that a corpus-
# provided key can still override, so the probe must never include it). gmail and hubspot are
# real-world per-container ids (per-mailbox; per-object-type, which is why every HubSpot route
# carries `{objectType}`) that Backlot nonetheless resolves flat (`routers.google`/`routers.hubspot`
# look an id up with no container, hubspot only checking `object_type` afterwards) — `None` there
# is the safe over-constraint Backlot's own lookup needs, not a claim about the vendor.
SERVED_ID = {
    "confluence": ("served_id", synth.confluence_id, None),
    "gmail": ("served_id", synth.gmail_message_id, None),
    "notion": ("served_id", synth.notion_id, None),
    "hubspot": ("served_id", synth.hubspot_record_id, None),
    # Linear's hashed uuid, kept distinct from `identifier` (already stored, and which must keep
    # winning — see GROUPING's linear comment) — this column is the OTHER spelling `issue(id:)`
    # accepts.
    "linear": ("served_id", synth.linear_id, None),
    "github": ("served_number", synth.github_number, grouping_col("github")),
    "jira": ("served_number", synth.jira_key_number, grouping_col("jira")),
}


def served_id_column(source_type: str) -> str:
    return SERVED_ID[source_type][0]


def served_id_seed(source_type: str) -> Callable[[str], object]:
    return SERVED_ID[source_type][1]


def served_id_scope(source_type: str) -> str | None:
    return SERVED_ID[source_type][2]


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
-- conversations.members pages a channel's distinct speakers; without this the DISTINCT
-- is a per-channel row scan (768k rows in the biggest channel) on every request.
CREATE INDEX IF NOT EXISTS idx_slack_channel_author ON slack_messages(channel, author_email);

CREATE TABLE IF NOT EXISTS gmail_messages (
    doc_id TEXT PRIMARY KEY, mailbox TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    thread_id TEXT, thread_seq INTEGER NOT NULL DEFAULT 0,
    label_ids TEXT, to_addr TEXT, cc TEXT, bcc TEXT, reply_to TEXT,
    message_id TEXT, in_reply_to TEXT, refs TEXT, attachments TEXT, created_ts INTEGER NOT NULL,
    body_html TEXT, owner_display TEXT, served_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_gmail_mailbox ON gmail_messages(mailbox);
CREATE INDEX IF NOT EXISTS idx_gmail_author ON gmail_messages(author_email);
-- date-scoped listing (ls /gmail/<label>/<date>) filters by a created_ts range; the index turns
-- that from a full-table scan into a range seek.
CREATE INDEX IF NOT EXISTS idx_gmail_created_ts ON gmail_messages(created_ts);
-- The id the API reports, assigned at import (see backlot.importer.byo) rather than hashed at
-- serve time, so a get-by-id is a column lookup instead of a reverse map rebuilt on every boot.
-- Unlike confluence this is the raw seed, never probed: `synth.gmail_message_id` draws from 2**63,
-- and keeping it a pure hash is what lets a reply derive its `threadId` by re-hashing the root's
-- key instead of reading the root's row. UNIQUE turns the (vanishingly unlikely) collision into a
-- loud import failure -- the shared write path's upsert (`ON CONFLICT(doc_id) DO UPDATE`) is
-- scoped to doc_id only, so a served_id collision (a different doc_id) falls through to this
-- index and raises, rather than silently replacing the earlier message.
CREATE UNIQUE INDEX IF NOT EXISTS idx_gmail_served ON gmail_messages(served_id);

CREATE TABLE IF NOT EXISTS gdrive_files (
    doc_id TEXT PRIMARY KEY, folder TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    subtype TEXT, mime_type TEXT, parents TEXT, created_ts INTEGER NOT NULL, updated_ts INTEGER,
    trashed INTEGER, collaborators TEXT, owner_display TEXT
);
CREATE INDEX IF NOT EXISTS idx_gdrive_folder ON gdrive_files(folder);

-- `path` names the file THIS row is (only kind='file' rows have one). `changed_paths` is the other
-- direction: a JSON list of the paths a PULL touched, so a corpus can state which files a pull
-- changed instead of leaving the router to pick deterministically. See backlot.routers.github's
-- changeset note. Comments stay OUTSIDE the parens: SQLite persists the statement verbatim, and a
-- trailing in-body comment makes a later `ALTER TABLE ... DROP COLUMN` fail to re-parse it.
CREATE TABLE IF NOT EXISTS github_items (
    doc_id TEXT PRIMARY KEY, repo TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    kind TEXT, state TEXT, labels TEXT, assignees TEXT,
    merged_at TEXT, head_ref TEXT, base_ref TEXT, reviews TEXT, reactions TEXT,
    created_ts INTEGER NOT NULL, updated_ts INTEGER,
    closed_ts INTEGER, closed_by TEXT, merged_by TEXT, milestone TEXT, requested_reviewers TEXT,
    owner_display TEXT, path TEXT, changed_paths TEXT, number INTEGER, served_number INTEGER
);
CREATE INDEX IF NOT EXISTS idx_github_repo ON github_items(repo);
CREATE INDEX IF NOT EXISTS idx_github_repo_path ON github_items(repo, path);
-- (doc_id, kind, repo, number, served_number) in that order: importer.byo's resolve_github_numbers
-- deferred assignment pass (which replaced main._build_index's boot-time reverse map, #51) scans
-- exactly these five, so the pass stays an index-only scan and never touches the wide rows.
-- Partial, because that scan excludes kind='file' rows — in a code-heavy corpus they dominate the
-- table, and indexing them would only pay write and disk cost for rows the one consumer filters
-- out. `kind` must stay among the columns or the plan loses COVERING.
CREATE INDEX IF NOT EXISTS idx_github_doc_number
    ON github_items(doc_id, kind, repo, number, served_number)
    WHERE kind IS NULL OR kind != 'file';
-- The number the API reports, assigned at import (see backlot.importer.byo) rather than derived
-- at serve time from a startup reverse map: `synth.github_number`'s 90,000-value-per-repo space
-- collides by the birthday bound long before a real repo runs out of issues/PRs, and the map this
-- replaces resolved a collision by MOVING the loser to a fresh number on every boot -- stable
-- while the server ran, but "free to move when --append changes the set" (the old `_free_number`
-- docstring's own words), so a client that had already saved a link could have it renumbered out
-- from under it by a later append. A stored column no longer moves (#51).
--
-- `number` (the corpus-provided value, untouched by this column) and `served_number` (what the
-- API actually reports) are kept as TWO columns on purpose: a synthesized value written into
-- `number` would be indistinguishable from a provided one, and "provided" has to mean exactly
-- "number IS NOT NULL" everywhere downstream (main.py's old comment on this table said so; see
-- importer.byo's `insert` for where that reasoning lives now). UNIQUE is scoped to (repo,
-- served_number), not served_number alone, because a GitHub number is unique only within its
-- repo (see store.SERVED_ID's `scope` for github) -- and a `kind='file'` row's served_number
-- stays NULL (see idx_github_doc_number above), which SQLite's UNIQUE treats as no claim rather
-- than a collision, so any number of file rows coexist under it. Uniqueness is primarily enforced
-- by the assignment itself -- resolve_github_numbers' two-phase pass (provided numbers claim
-- first, corpus-wide, before anything probes) -- so a genuine collision essentially never reaches
-- this index; if one somehow did, the shared write path's upsert (`ON CONFLICT(doc_id) DO
-- UPDATE`) is scoped to the doc_id conflict target only, so a served_number collision (a
-- DIFFERENT doc_id) falls through to this index and raises IntegrityError rather than being
-- silently resolved.
CREATE UNIQUE INDEX IF NOT EXISTS idx_github_served ON github_items(repo, served_number);

CREATE TABLE IF NOT EXISTS jira_issues (
    doc_id TEXT PRIMARY KEY, project TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    status TEXT, issuetype TEXT, priority TEXT, labels TEXT, components TEXT,
    issuelinks TEXT, parent_id TEXT, changelog TEXT, created_ts INTEGER NOT NULL, updated_ts INTEGER,
    assignee_email TEXT, reporter_email TEXT, resolution TEXT, resolution_ts INTEGER,
    duedate TEXT, fix_versions TEXT, severity TEXT, squad TEXT, owner_display TEXT, key TEXT,
    served_number INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jira_project ON jira_issues(project);
CREATE INDEX IF NOT EXISTS idx_jira_parent ON jira_issues(parent_id);
-- (doc_id, project, key, served_number) in that order: importer.byo's resolve_jira_numbers
-- deferred assignment pass (which replaced main._build_index's boot-time reverse map for the KEY,
-- #51's task 8) scans exactly these four, so the pass stays an index-only scan and never touches
-- the wide rows.
CREATE INDEX IF NOT EXISTS idx_jira_doc_key ON jira_issues(doc_id, project, key, served_number);
-- The numeric SUFFIX the API reports (composed with the project's own prefix at serve time --
-- see synth.jira_key, routers.atlassian's `_issue_key`), assigned at import (see
-- backlot.importer.byo) rather than derived at serve time from a startup reverse map:
-- `synth.jira_key_number`'s 9,000-value-per-project space collides by the birthday bound long
-- before a real project runs out of issues, and the map this replaces resolved a collision by
-- MOVING the loser to a fresh suffix on every boot — stable while the server ran, but free to
-- move when `--append` changes the set, so a client that had already saved a link could have it
-- renumbered out from under it by a later append. A stored column no longer moves (#51, task 8).
--
-- `key` (the corpus-provided value, untouched by this column) and `served_number` (the suffix
-- actually served) are kept as TWO things on purpose, same reasoning as github's `number`/
-- `served_number` split: a synthesized value written into `key` would be indistinguishable from a
-- provided one, and "provided" has to mean exactly "key IS NOT NULL" everywhere downstream. UNIQUE
-- is scoped to (project, served_number), not served_number alone, because a jira key's suffix is
-- unique only within its project (see store.SERVED_ID's `scope` for jira) — the PREFIX is not part
-- of this index at all, since a corpus-provided key can still override it (synth.jira_key_number's
-- own docstring). Uniqueness is primarily enforced by the assignment itself —
-- resolve_jira_numbers' two-phase pass (provided suffixes claim first, corpus-wide, before
-- anything probes) — so a genuine collision essentially never reaches this index; if one somehow
-- did, the shared write path's upsert (`ON CONFLICT(doc_id) DO UPDATE`) is scoped to the doc_id
-- conflict target only, so a served_number collision (a DIFFERENT doc_id) falls through to this
-- index and raises IntegrityError rather than being silently resolved.
--
-- Residual NOT closed by this index (documented, not fixed — see backlot.importer.byo's `insert`
-- and resolve_jira_numbers, and synth.jira_project_key's own docstring, for the same note): a
-- project with no provided keys at all is never registered in `jira_prefix_holders`, so its
-- SYNTHESIZED prefix (`synth.jira_project_key`, initials + 6 hex of the digest) could in
-- principle equal another project's PROVIDED prefix -- and, symmetrically, two KEYLESS projects'
-- own synthesized prefixes could collide with EACH OTHER, at the identical order (both draw from
-- the same 6-hex digest space; only the provided-vs-synthesized case above involves a corpus
-- writing anything at all). Either way, two documents in different projects would then serve the
-- exact same key while this index (whose scope is `project`, not the prefix string) is perfectly
-- satisfied. ~1 in 16.7M per such pair; closing it is a different change.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jira_served ON jira_issues(project, served_number);

CREATE TABLE IF NOT EXISTS confluence_pages (
    doc_id TEXT PRIMARY KEY, space TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    subtype TEXT, parent_id TEXT, labels TEXT, created_ts INTEGER NOT NULL, updated_ts INTEGER,
    version_number INTEGER, version_message TEXT, minor_edit INTEGER,
    reviewers TEXT, confidentiality TEXT, owner_team TEXT, owner_display TEXT,
    served_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_confluence_space ON confluence_pages(space);
CREATE INDEX IF NOT EXISTS idx_confluence_parent ON confluence_pages(parent_id);
-- The id the API reports, assigned at import (see backlot.importer.byo) rather than hashed at
-- serve time: a hash into `synth.confluence_id`'s 9,000,000 values collides by the birthday
-- bound, and the reverse map this replaces was last-writer-wins, so a collision made a page
-- unreachable by its own id. Uniqueness is primarily enforced by the assignment itself --
-- _assign_confluence_id's in-run memo plus seed_tracker_ids' cross-run preload, both probed
-- against every id already taken -- so a genuine collision essentially never reaches this index.
-- If one somehow did, the shared write path's upsert (`ON CONFLICT(doc_id) DO UPDATE`, in
-- backlot.importer.byo) is scoped to the doc_id conflict target only, so a served_id collision
-- (a DIFFERENT doc_id) falls through to this index and raises IntegrityError rather than being
-- silently resolved -- the index is a real backstop, not just a point-read lookup.
CREATE UNIQUE INDEX IF NOT EXISTS idx_confluence_served ON confluence_pages(served_id);

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

-- Two resources real GitHub keeps apart, discriminated by `path`: a row WITH one is a
-- line-anchored review comment (/pulls/{n}/comments), one without is a conversation comment
-- (/issues/{n}/comments). `line` may be NULL for a file-level review comment, as on real GitHub;
-- `diff_hunk` is optional and derived from the file's snapshot when the corpus omits it.
-- `served_id` is the `id` the API reports, assigned at import (see backlot.importer.byo) rather
-- than hashed at serve time: a comment's own `url` resolves through it, and a hash into any fixed
-- range collides by the birthday bound long before a real corpus runs out of comments — two
-- comments sharing one served id means one comment's url returns the other's body.
CREATE TABLE IF NOT EXISTS github_comments (
    id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, seq INTEGER NOT NULL,
    author_email TEXT, body TEXT NOT NULL, created_ts INTEGER NOT NULL, reactions TEXT,
    path TEXT, line INTEGER, diff_hunk TEXT, served_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_github_comments_doc ON github_comments(doc_id);
-- UNIQUE is the guarantee, not just an index: the assignment probes against it, so a duplicate is
-- an import error rather than a comment that silently shadows another at serve time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_github_comments_served ON github_comments(served_id);

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
    created_ts INTEGER NOT NULL, updated_ts INTEGER,
    served_id TEXT, served_data_source_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_notion_teamspace ON notion_pages(teamspace);
CREATE INDEX IF NOT EXISTS idx_notion_parent ON notion_pages(parent_id);
-- Notion has TWO synthesized id spaces per row, both assigned at import (see
-- backlot.importer.byo) rather than derived by hashing at serve time. `served_id` is the
-- page/database id (synth.notion_id), populated for every row. `served_data_source_id` is the
-- 2025-09-03 API's data-source (query target) id for a DATABASE (synth.notion_data_source_id) --
-- real Notion has no such id for a page, so it is populated only when subtype='database' and
-- stays NULL elsewhere, which a UNIQUE index treats as no claim rather than a collision (SQLite
-- allows any number of NULLs under UNIQUE). Neither column is probed the way confluence's is:
-- both draw from synth._uuid_from's full digest space, not a bounded range, so a collision is
-- vanishingly unlikely -- and the shared write path's upsert (`ON CONFLICT(doc_id) DO UPDATE`) is
-- scoped to doc_id only, so a served-column conflict (a DIFFERENT doc_id) falls through to these
-- indexes and raises IntegrityError instead of silently replacing the row that held the value.
CREATE UNIQUE INDEX IF NOT EXISTS idx_notion_served ON notion_pages(served_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notion_served_ds ON notion_pages(served_data_source_id);

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
-- `{objectType}` is a path variable and custom object types exist, so a table per type would make
-- each new type a migration and break table()'s one-table-per-source contract. Typed properties live
-- in a JSON column because a search filter may name any property (-> json_extract).
CREATE TABLE IF NOT EXISTS hubspot_objects (
    doc_id TEXT PRIMARY KEY, object_type TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    properties TEXT, archived INTEGER, created_ts INTEGER NOT NULL, updated_ts INTEGER,
    owner_display TEXT, served_id TEXT
);
-- (object_type, doc_id), not object_type alone: every read is "one type, ordered by doc_id", so
-- carrying the ordering column makes a page a range seek instead of a temp-b-tree re-sort.
CREATE INDEX IF NOT EXISTS idx_hubspot_type_doc ON hubspot_objects(object_type, doc_id);
-- The id the API reports, assigned at import (see backlot.importer.byo) rather than hashed at
-- serve time: `synth.hubspot_record_id`'s space is 9,000,000,000 values -- wide enough to look
-- safe, but a corpus this project actually generates (500k documents) still collides ~16 times by
-- the birthday bound (#51), and the reverse map this replaces was last-writer-wins, so a collision
-- made a record unreachable at its own id. Uniqueness is primarily enforced by the assignment
-- itself -- _assign_hubspot_id's in-run memo plus seed_tracker_ids' cross-run preload, both probed
-- against every id already taken, the same shape confluence's own served_id follows -- so a
-- genuine collision essentially never reaches this index. If one somehow did, the shared write
-- path's upsert (`ON CONFLICT(doc_id) DO UPDATE`, in backlot.importer.byo) is scoped to the doc_id
-- conflict target only, so a served_id collision (a DIFFERENT doc_id) falls through to this index
-- and raises IntegrityError rather than being silently resolved.
CREATE UNIQUE INDEX IF NOT EXISTS idx_hubspot_served ON hubspot_objects(served_id);

-- Associations are bidirectional in real HubSpot, with a distinct type id per direction, so a row
-- is stored per direction and a lookup stays a plain (from_doc_id, to_type) index match.
CREATE TABLE IF NOT EXISTS hubspot_associations (
    from_doc_id TEXT NOT NULL, from_type TEXT NOT NULL,
    to_doc_id TEXT NOT NULL, to_type TEXT NOT NULL,
    assoc_category TEXT, assoc_type_id INTEGER NOT NULL, label TEXT,
    PRIMARY KEY (from_doc_id, to_doc_id, assoc_type_id)
);
CREATE INDEX IF NOT EXISTS idx_hubspot_assoc_from ON hubspot_associations(from_doc_id, to_type);

-- ── Linear: issues + their comments. Columns keep LINEAR's vocabulary, not Jira's (`state` not
-- status, `estimate` not story points, `branch_name`), so the payload cannot drift toward the wrong
-- vendor's model. `priority` is Linear's own 0-4 integer (0 none, 1 urgent … 4 low), not the corpus's
-- "P1"; `priorityLabel` is derived from it at serve time.
CREATE TABLE IF NOT EXISTS linear_issues (
    doc_id TEXT PRIMARY KEY, team TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    identifier TEXT, state TEXT, priority INTEGER, estimate INTEGER, labels TEXT,
    project TEXT, cycle TEXT, branch_name TEXT, due_date TEXT,
    created_ts INTEGER NOT NULL, updated_ts INTEGER,
    archived_ts INTEGER, auto_archived_ts INTEGER, auto_closed_ts INTEGER,
    canceled_ts INTEGER, completed_ts INTEGER, started_ts INTEGER,
    assignee_email TEXT, assignee_display TEXT, owner_display TEXT,
    -- The parent's identifier as the corpus wrote it, plus the doc_id it RESOLVED to at import. Both,
    -- because identifiers are NOT required to be unique (measured: one key is the identifier of
    -- 107 issues), so a
    -- serve-time join on `identifier` would invent edges. Resolving once — first match by doc_id, the
    -- rule linear_issue_by_identifier applies — makes Issue.parent and Issue.children exact inverses.
    parent_key TEXT, parent_doc_id TEXT,
    -- Release name as the corpus writes it (`runtime-1.19`); served as `Issue.releases`.
    release TEXT,
    served_id TEXT
);
-- (team, doc_id): the Relay connection pages one team ordered by doc_id, so carrying the ordering
-- column makes a page a range seek rather than a re-sort of the whole team.
CREATE INDEX IF NOT EXISTS idx_linear_team_doc ON linear_issues(team, doc_id);
-- The ORDER BY is always TOTAL (sort key + doc_id), so an index on the sort key alone does not
-- satisfy it — SQLite falls back to a temp b-tree over the whole table for every page. These carry
-- the tiebreak, so the ORDER BY is read straight off the index.
CREATE INDEX IF NOT EXISTS idx_linear_created_doc ON linear_issues(created_ts, doc_id);
CREATE INDEX IF NOT EXISTS idx_linear_team_created ON linear_issues(team, created_ts, doc_id);
-- `orderBy: updatedAt` sorts on the same COALESCE the field is served with, so the index has to
-- be on the expression, not the bare column.
CREATE INDEX IF NOT EXISTS idx_linear_updated_doc
    ON linear_issues(COALESCE(updated_ts, created_ts), doc_id);
-- Superseded by idx_linear_created_doc (which has it as a prefix). Dropped EXPLICITLY: `CREATE INDEX
-- IF NOT EXISTS` matches on NAME, so it would never replace this on an already-built DB.
DROP INDEX IF EXISTS idx_linear_created_ts;
CREATE INDEX IF NOT EXISTS idx_linear_state ON linear_issues(state);
-- The by-id roots probe "can this caller see any issue carrying X" (linear_entity_has_visible), so
-- these are indexed to seek rather than scan until the first readable row. Labels get no index (JSON
-- column; json_each cannot be indexed) — only a MISS pays a full scan, and a miss is exactly the
-- enumeration attempt.
CREATE INDEX IF NOT EXISTS idx_linear_project ON linear_issues(project);
CREATE INDEX IF NOT EXISTS idx_linear_cycle ON linear_issues(cycle);
CREATE INDEX IF NOT EXISTS idx_linear_assignee ON linear_issues(assignee_email);
CREATE INDEX IF NOT EXISTS idx_linear_author ON linear_issues(author_email);
-- `Issue.children` is "every issue whose parent_doc_id is me" — an indexed equality, not a join
-- on the non-unique identifier text.
CREATE INDEX IF NOT EXISTS idx_linear_parent_doc ON linear_issues(parent_doc_id);
CREATE INDEX IF NOT EXISTS idx_linear_release ON linear_issues(release);
-- `issue(id: "ENG-123")` resolves an identifier straight to its row; identifiers are NOT unique
-- (5,055 of them repeat in one real corpus), so this is a lookup index, never a unique constraint.
CREATE INDEX IF NOT EXISTS idx_linear_identifier ON linear_issues(identifier);
-- COVERING index for the importer's parent-resolution pass (backlot.importer.byo, `parent_key` ->
-- `parent_doc_id`), which reads (doc_id, identifier) for every issue that provides one. Without it
-- each wide row is fetched from a scattered page and the scan dominates that pass; as an
-- index-only scan it is negligible. (Formerly also covered main._build_index's startup reverse
-- map, since replaced by `served_id` below — see idx_linear_served.)
CREATE INDEX IF NOT EXISTS idx_linear_doc_ident ON linear_issues(doc_id, identifier);
-- The UUID the API reports (`synth.linear_id`), assigned at import (see backlot.importer.byo)
-- rather than hashed at serve time -- a get-by-id is a column lookup instead of a reverse map
-- rebuilt on every boot. Kept distinct from `identifier` on purpose: identifiers are NOT unique
-- (see idx_linear_identifier above), so they stay a lookup index, never a candidate for this
-- column or this constraint (see the SERVED_ID comment on why `identifier` is excluded).
-- No probe, like gmail's: `synth.linear_id` draws from `_uuid_from`'s full digest space, not a
-- bounded range, so a collision is vanishingly unlikely -- and the shared write path's upsert
-- (`ON CONFLICT(doc_id) DO UPDATE`) is scoped to doc_id only, so a served_id collision (a
-- DIFFERENT doc_id) falls through to this index and raises IntegrityError, rather than silently
-- replacing the issue already holding that value.
CREATE UNIQUE INDEX IF NOT EXISTS idx_linear_served ON linear_issues(served_id);

CREATE TABLE IF NOT EXISTS linear_comments (
    id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, seq INTEGER NOT NULL,
    author_email TEXT, body TEXT NOT NULL, created_ts INTEGER NOT NULL, reactions TEXT
);
CREATE INDEX IF NOT EXISTS idx_linear_comments_doc ON linear_comments(doc_id, seq);
-- `Query.comments` pages the whole corpus ordered by time; without this the ORDER BY
-- re-sorts every comment in a temp b-tree on every page (165k of them at the scale measured).
CREATE INDEX IF NOT EXISTS idx_linear_comments_ts ON linear_comments(created_ts, id);

-- Linear's IssueRelation, `type` in (blocks | duplicate | related). ONE row per relation, not per
-- direction: Issue.relations and Issue.inverseRelations are the two ends of the same row.
-- `to_doc_id` is resolved at import, so a dangling key never becomes a relation.
CREATE TABLE IF NOT EXISTS linear_relations (
    id TEXT PRIMARY KEY, from_doc_id TEXT NOT NULL, to_doc_id TEXT NOT NULL,
    type TEXT NOT NULL, created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_linear_rel_from ON linear_relations(from_doc_id);
CREATE INDEX IF NOT EXISTS idx_linear_rel_to ON linear_relations(to_doc_id);

-- Linear's model for any external link on an issue (a corpus's `links` and `attachments` alike).
-- `title` is non-null in Linear, so a bare URL gets one derived from its last path segment.
CREATE TABLE IF NOT EXISTS linear_attachments (
    id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, seq INTEGER NOT NULL,
    title TEXT NOT NULL, url TEXT NOT NULL, subtitle TEXT, source_type TEXT,
    created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_linear_attach_doc ON linear_attachments(doc_id, seq);

-- One root document per meeting plus its ordered sentences below. `content` is the sentences
-- concatenated (synth.fireflies_transcript_text) so search and any RAG consumer see one document; it
-- is an EXACT inverse of fireflies_sentences, not a second copy that can drift. `author_email` is the
-- HOST (the API's `host_email`); `organizer_email` is separate because the real API exposes both and
-- they legitimately differ, and is NULL when they coincide.
CREATE TABLE IF NOT EXISTS fireflies_transcripts (
    doc_id TEXT PRIMARY KEY, channel TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    -- The API-facing id, synthesized rather than reused from a corpus's own meeting id, which is NOT
    -- required to be unique — `transcript(id:)` looks a meeting up by it, so a duplicate would make
    -- that ambiguous.
    -- The corpus's own value is kept as `calendar_id`, where a real transcript carries it.
    transcript_id TEXT, calendar_id TEXT, calendar_type TEXT,
    organizer_email TEXT, duration REAL,
    created_ts INTEGER NOT NULL,
    -- JSON: the API's nested objects, stored whole because that is the shape served.
    summary TEXT, analytics TEXT, participants TEXT, meeting_attendees TEXT,
    audio_url TEXT, video_url TEXT, transcript_url TEXT, meeting_link TEXT,
    owner_display TEXT
);
CREATE INDEX IF NOT EXISTS idx_fireflies_channel ON fireflies_transcripts(channel);
-- `transcripts(fromDate:/toDate:)` is a date range and the default order is newest-first, so the
-- ordering column carries its doc_id tiebreak (same lesson as idx_linear_created_doc).
CREATE INDEX IF NOT EXISTS idx_fireflies_created_doc
    ON fireflies_transcripts(created_ts, doc_id);
CREATE INDEX IF NOT EXISTS idx_fireflies_channel_created
    ON fireflies_transcripts(channel, created_ts, doc_id);
-- `transcript(id:)` resolves a synthesized transcript id straight to its row.
CREATE INDEX IF NOT EXISTS idx_fireflies_transcript_id
    ON fireflies_transcripts(transcript_id);
-- `transcripts(host_email:)` / `organizers:` filter on these directly.
CREATE INDEX IF NOT EXISTS idx_fireflies_host ON fireflies_transcripts(author_email);

-- The transcript's sentences. Carries the shared child-row contract that doc_comments reads, so it
-- fits the COMMENT_TABLE slot, plus the per-sentence fields the API serves. `body` IS the sentence
-- text; `author_email` is the speaker resolved to an identity, NULL for an anonymous label
-- ("Speaker 3") which both the corpus and the real API leave unattributed.
CREATE TABLE IF NOT EXISTS fireflies_sentences (
    id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, seq INTEGER NOT NULL,
    author_email TEXT, body TEXT NOT NULL, created_ts INTEGER NOT NULL, reactions TEXT,
    speaker_name TEXT, speaker_id INTEGER, start_time REAL, end_time REAL
);
CREATE INDEX IF NOT EXISTS idx_fireflies_sentences_doc ON fireflies_sentences(doc_id, seq);

-- ── shared relationship tables (keyed by names, not doc_id — ACL grants live in the per-source
-- ── tables appended below instead, since doc_id is unique only within a source) ──
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
-- `served_id` (`synth.linear_team_id`, a UUID) and `served_key` (`synth.linear_team_key`, "ENG")
-- are the OTHER two spellings real `team(id:)` accepts, alongside this table's own primary key
-- (the raw container name -- a mock affordance, see resolve_team's docstring). Both are written
-- unconditionally at import (backlot.importer.byo's write_containers), replacing the reverse map
-- `main._build_index` used to rebuild on every boot (#51). `served_key` carries NO unique index:
-- `linear_team_key` is not injective -- two containers can reduce to one key -- so a lookup
-- breaks the tie by team NAME order instead (see linear_team_by_served_key).
CREATE TABLE IF NOT EXISTS linear_teams (
    team TEXT PRIMARY KEY, group_id TEXT, served_id TEXT, served_key TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_linear_teams_served ON linear_teams(served_id);
CREATE INDEX IF NOT EXISTS idx_linear_teams_served_key ON linear_teams(served_key);
CREATE TABLE IF NOT EXISTS fireflies_channels (channel TEXT PRIMARY KEY, group_id TEXT);

CREATE TABLE IF NOT EXISTS principals (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, display_name TEXT, email TEXT
);

-- Fireflies' own per-user id (`synth.fireflies_user_id`, a one-way hash of the address) needs a
-- row to live on, but `principals` also holds org/group rows the id is meaningless for (only
-- `type = 'user'` rows have a Fireflies account -- see list_users), and #51's whole point is
-- keeping vendor concerns off the deliberately central roster. A dedicated table sidesteps both:
-- every row here IS a user, written unconditionally (backlot.importer.byo), so there is no NULL
-- branch and no column that stays empty for every non-user principal.
CREATE TABLE IF NOT EXISTS fireflies_users (
    email TEXT PRIMARY KEY REFERENCES principals(id), served_id TEXT NOT NULL
);
-- `synth.fireflies_user_id` is 24 hex characters (96 bits) -- like gmail/notion's own served ids,
-- the raw seed is stored as-is and a collision (vanishingly unlikely at any real scale) fails the
-- import loudly through this index rather than resolving silently.
CREATE UNIQUE INDEX IF NOT EXISTS idx_fireflies_user_served ON fireflies_users(served_id);

CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL, user_id TEXT NOT NULL, PRIMARY KEY (group_id, user_id)
);

-- Build-time facts that cannot be recomputed from the rows. `source_documents` is the count of
-- documents the corpus OFFERED, which differs from COUNT(*) because faithful parsing promotes
-- structure inside a document to first-class rows (one Slack transcript -> many messages).
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# One ACL table per source, appended rather than written out eleven times: they differ only in name,
# and a hand-written block per source is eleven places for them to drift apart.
SCHEMA += "".join(
    f"\nCREATE TABLE IF NOT EXISTS {t} (\n"
    "    doc_id TEXT NOT NULL, principal_type TEXT NOT NULL,\n"
    "    principal_id TEXT NOT NULL REFERENCES principals(id),\n"
    "    PRIMARY KEY (doc_id, principal_type, principal_id)\n"
    ");\n"
    f"CREATE INDEX IF NOT EXISTS idx_{t}_pid ON {t}(principal_id);\n"
    for t in ACL_TABLE.values()
)


def connect_rw(path: Path, *, busy_ms: int = 60_000) -> sqlite3.Connection:
    path = Path(path)  # accept a str path too
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Wait for a lock rather than erroring, so an in-place rebuild (build_fts) against a DB the
    # live server is reading rides through the reader's lock instead of a spurious "locked".
    if busy_ms:
        conn.execute(f"PRAGMA busy_timeout={busy_ms}")
    # A DB built before this branch has one shared `doc_acl` table, keyed corpus-wide by `doc_id`
    # rather than a table per source. An append onto it would write the new source's grants into
    # the per-source tables SCHEMA creates below while every pre-existing grant stays behind in
    # `doc_acl`, which nothing reads any more -- every document from before the append silently
    # becomes invisible to every scoped token. There is deliberately no backfill here: `doc_acl`
    # has no source column, so it cannot say which source a colliding `doc_id`'s grant belonged
    # to, and copying its rows into the per-source tables blind would silently re-create exactly
    # the cross-source union this branch was written to remove (see the `ACL_TABLE` comment
    # above). The only correct move is a fresh re-import.
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'doc_acl'"
    ).fetchone():
        raise ValueError(
            f"{path} predates per-source ACL tables (it still has a `doc_acl` table) -- "
            "re-import this corpus from scratch instead of appending to it; the old grants "
            "cannot be safely migrated"
        )
    # Self-heal tables built before a column was added. `CREATE TABLE IF NOT EXISTS` in SCHEMA
    # below does NOT alter an existing table, so a DB created by an earlier version keeps the old
    # column set -- and then every INSERT naming the new column fails. (For github_items the
    # symptom was different but the cause identical: `CREATE INDEX IF NOT EXISTS
    # idx_github_repo_path ON github_items(repo, path)` guards only the index NAME and still
    # raises if the referenced column is missing.) Each ALTER is idempotent: it no-ops on a fresh
    # DB (table absent) and on a DB that already has the column.
    for table, column, decl in (
        ("github_items", "path", "TEXT"),
        ("github_items", "changed_paths", "TEXT"),
        ("github_items", "number", "INTEGER"),
        ("github_comments", "path", "TEXT"),
        ("github_comments", "line", "INTEGER"),
        ("github_comments", "diff_hunk", "TEXT"),
        ("github_comments", "served_id", "INTEGER"),
        ("linear_issues", "parent_key", "TEXT"),
        ("linear_issues", "parent_doc_id", "TEXT"),
        ("linear_issues", "release", "TEXT"),
        ("jira_issues", "key", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError:
            pass  # table absent (fresh DB) or column already present
    conn.executescript(SCHEMA)
    return conn


def write_meta(conn: sqlite3.Connection, key: str, value) -> None:
    """Persist a build-time fact. Values are stored as TEXT; the caller casts on read.

    Commits the entire pending transaction on the connection, not just the meta row.
    Matches the contract of build_fts and fts_add_docs, which also commit.
    """
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
    conn.commit()


def read_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """A build-time fact, or None when absent — including on a DB built before the meta table
    existed. Only a missing-table error is swallowed; other OperationalErrors (e.g. database
    locked) must surface, not masquerade as absent metadata."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError as e:
        # Only "no such table" means the meta table doesn't exist. A different OperationalError
        # (e.g. "database is locked") must surface, not masquerade as metadata absence.
        if "no such table" not in str(e).lower():
            raise
        return None
    return row[0] if row else None


def connect_ro(
    path: Path, *, mmap_mb: int = 0, cache_mb: int = 0, temp_memory: bool = False, busy_ms: int = 0
) -> sqlite3.Connection:
    """Open a read-only connection. The tuning knobs default to off, so tests and small corpora are
    unaffected; the serving path passes config values to keep the big DB warm.

    ``mmap_mb`` memory-maps the DB (the main lever against cold reads; set >= DB size to map it
    fully), ``cache_mb`` sizes SQLite's page cache, ``temp_memory`` keeps sorts in RAM (helps FTS
    ``ORDER BY rank``), and ``busy_ms`` waits for a lock instead of erroring — so a read rides
    through an out-of-band writer's commit (an in-place ``build_fts``) rather than 500ing.
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


def _like_escape(needle: str | None) -> str:
    """Neutralize LIKE wildcards in a user-supplied needle so they match literally. Use with
    ``LIKE ? ESCAPE '\\'``; without it a search for ``100%`` matches everything."""
    return (needle or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _acl_clause(
    source_type: str,
    tbl: str | None = None,
    visible_ids: set[str] | None = None,
    col: str = "doc_id",
) -> tuple[str, list]:
    """``col`` names the column holding the doc whose ACL decides visibility — normally the row's
    own ``doc_id``, but for a HubSpot association it is the *target* (``to_doc_id``), since the
    target is the record whose existence the response would reveal.

    ``source_type`` selects the ACL table. Per source because `doc_id` is per source: one table
    keyed corpus-wide let two documents that merely share an id share their grants.

    ``tbl`` is the SQL name the caller's query knows the doc table by. It defaults to
    ``table(source_type)`` — most callers query that table under its own name and would
    otherwise restate it. Pass it explicitly only for a SQL alias (``"i"``, ``"a"``, ``"b"``,
    ``"t"``, ``"s"`` — see linear_relation_by_id for why the subquery avoids colliding with one)
    or for a genuinely different real table, e.g. ``hubspot_associations``. A real table name
    (one of :data:`SOURCE_TABLE`'s values) that does NOT match ``table(source_type)`` is always a
    mistake — a valid-but-wrong pairing like ``("slack", "gmail_messages")`` would otherwise scope
    silently to the wrong source instead of failing, so that combination raises."""
    # Resolved before the `visible_ids is None` early return, so an unknown source_type fails loudly
    # on an admin call too, rather than lying dormant until a scoped caller happens to hit it.
    acl_tbl = acl_table(source_type)
    if tbl is None:
        tbl = table(source_type)
    elif tbl in SOURCE_TABLE.values():
        real_table = table(source_type)
        assert tbl == real_table, (
            f"_acl_clause: tbl {tbl!r} is a real table but doesn't match source_type "
            f"{source_type!r}'s own table {real_table!r} — this would scope the ACL check to "
            f"the wrong source"
        )
    if visible_ids is None:
        return "", []
    ids = list(visible_ids)
    if not ids:
        return " AND 0", []
    marks = ",".join("?" for _ in ids)
    # The subquery's own alias must not collide with a caller's outer alias — the Linear relation
    # readers pass "a"/"b" as `tbl` (see linear_relation_by_id), and a shared name there would
    # shadow the outer table, turning `_acl.doc_id = {tbl}.{col}` into a tautological
    # self-comparison that admits any row with ANY grant in this table.
    return (
        f" AND EXISTS (SELECT 1 FROM {acl_tbl} _acl WHERE _acl.doc_id = {tbl}.{col} "
        f"AND _acl.principal_id IN ({marks}))",
        ids,
    )


def _scope(
    sql: str,
    params: list,
    gcol: str,
    container: str | None,
    author_email: str | None,
    not_author_email: str | None = None,
) -> str:
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


def list_documents(
    conn,
    source_type,
    container=None,
    visible_ids=None,
    limit=100,
    offset=0,
    author_email=None,
    state=None,
    not_author_email=None,
    exclude_trashed=False,
) -> list[sqlite3.Row]:
    # state: only valid for source_type="github" — it's the only items table with a `state`
    # column; passing it for any other source_type raises sqlite3.OperationalError. Likewise
    # exclude_trashed, which only gdrive_files has a column for.
    tbl = table(source_type)
    sql = f"SELECT * FROM {tbl} WHERE 1=1"
    params: list = []
    sql = _scope(sql, params, grouping_col(source_type), container, author_email, not_author_email)
    if state is not None:
        sql += " AND COALESCE(state, 'open') = ?"
        params.append(state)
    if exclude_trashed:
        sql += " AND COALESCE(trashed, 0) = 0"
    clause, cparams = _acl_clause(source_type, visible_ids=visible_ids)
    sql += clause + " ORDER BY doc_id LIMIT ? OFFSET ?"
    params += cparams + [limit, offset]
    return conn.execute(sql, params).fetchall()


def key_successor(s: str) -> str:
    """The smallest string greater than every string with prefix ``s`` (increments its last
    character), so an S3 prefix becomes the half-open range ``key >= s AND key < key_successor(s)``.
    The ListObjectsV2 router also uses it to skip a whole CommonPrefixes group in one bound.
    Undefined for an empty string — callers guard that case."""
    return s[:-1] + chr(ord(s[-1]) + 1)


def list_s3_objects(
    conn, bucket, *, prefix="", start_after=None, start_at=None, visible_ids=None, limit=1000
) -> list[sqlite3.Row]:
    """One page of ListObjectsV2: prefix filter, keyset pagination and ACL scoping, all in SQL.

    The prefix is a half-open byte range (``key >= prefix AND key < key_successor(prefix)``), NOT a
    ``LIKE prefix||'%'``: SQLite only turns a LIKE's leading literal into an index range when
    ``case_sensitive_like`` is ON, which this repo must not set (``list_drive_by_name`` needs the
    default case-insensitive LIKE). The byte range hits ``idx_s3_key(bucket, key)`` for both the
    WHERE and the ORDER BY, and is byte-exact like real S3.

    ``start_after`` (exclusive) and ``start_at`` (inclusive — the router uses it to resume past a
    whole rolled-up CommonPrefixes group) are independent bounds."""
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
    clause, cparams = _acl_clause("s3", visible_ids=visible_ids)
    sql += clause + " ORDER BY key ASC LIMIT ?"
    params += cparams + [limit]
    return conn.execute(sql, params).fetchall()


def s3_by_bucket_key(conn, bucket, key, visible_ids=None) -> sqlite3.Row | None:
    """One object by the (bucket, key) address real S3 itself keys on. `idx_s3_key(bucket, key)`
    already covers this exactly, so this is a plain indexed lookup — there is nothing to
    synthesize and no entry in `SERVED_ID` for s3, unlike confluence/gmail/notion/hubspot.

    Unlike those served-id columns, (bucket, key) is unique only by convention here, not by a DB
    constraint: `idx_s3_key` is a plain index, and the doc_id the importer assigns (a content
    hash by default) doesn't depend on (bucket, key) at all, so nothing stops two rows from
    sharing an address. On that unsupported duplicate shape this behaves DIFFERENTLY from the
    map it replaces, not identically: the old map picked one winning row at boot (whichever the
    unordered scan saw last) and every caller got that same row-or-404 regardless of their ACL;
    this reads per-CALL, with the ACL clause folded into the same query, so which of the
    duplicate rows (if any) a given caller sees now depends on which one THEY are granted —
    two callers hitting the same duplicate address can get two different rows, where the old map
    gave everyone the same row and just 404'd whoever lacked a grant on it. Neither version
    promises a stable winner across the whole corpus; the difference is that resolution is now
    per-caller and ACL-shaped rather than address-stable. No leak either way — a caller only ever
    sees a row it holds a grant on — but a duplicate address was never a supported shape, and
    nothing here is designed to make it behave sensibly, only to not leak while it doesn't."""
    clause, cp = _acl_clause("s3", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM s3_objects WHERE bucket = ? AND key = ?{clause}", [bucket, key, *cp]
    ).fetchone()


def hubspot_by_served_id(conn, served_id, visible_ids=None, *, columns="*") -> sqlite3.Row | None:
    """One CRM record by the id the API reports. A unique-indexed column lookup, not a reverse map
    built at startup: the id is assigned at import (see backlot.importer.byo), so it needs neither
    the memory nor the per-boot scan, and it cannot be ambiguous.

    Unlike confluence's/notion's/gmail's ``served_id``, HubSpot's is PROBED against a collision
    (#51: ``synth.hubspot_record_id``'s 9,000,000,000-value space still collides at the corpus
    sizes this project generates) -- which is exactly why a plain equality lookup against a UNIQUE
    column is still correct here: the probe (``_assign_hubspot_id``) is what makes the column
    unique, this reader just trusts that it did.

    ``columns`` narrows the projection: ``routers.hubspot._doc_id_for`` only ever needs the
    doc_id, and pulling ``content`` (a note's whole body, the widest column on this table) for
    every ``after`` cursor on every paged listing/association request would dwarf the lookup it
    is resolving on the way to a query that is separately ACL-scoped anyway."""
    col = served_id_column("hubspot")
    clause, cp = _acl_clause("hubspot", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT {columns} FROM hubspot_objects WHERE {col} = ?{clause}", [served_id, *cp]
    ).fetchone()


def list_hubspot_objects(
    conn,
    object_type,
    *,
    after_doc_id=None,
    visible_ids=None,
    limit=100,
    archived=False,
    columns="*",
    prefilter=None,
) -> list[sqlite3.Row]:
    """One page of a CRM object type, keyset-paginated by ``doc_id``.

    HubSpot's ``after`` cursor is a record id, which the router maps back to a doc_id, so the bound
    is a keyset rather than an OFFSET. ``archived`` splits the two views the API exposes.

    ``prefilter`` is a ``(sql_fragment, params)`` the caller has established as a *necessary*
    condition, so pushing it down can only remove rows that would have been rejected anyway.
    ``columns`` narrows the projection: search walks the whole object type to report an honest
    ``total``, and ``content`` (a note's body) dominates that scan if it is read needlessly."""
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
    clause, cparams = _acl_clause("hubspot", visible_ids=visible_ids)
    sql += clause + " ORDER BY doc_id LIMIT ?"
    params += cparams + [limit]
    return conn.execute(sql, params).fetchall()


# --- Linear: issues, their comments, and the identifier lookup ---------------------
# Linear pages a Relay connection, and the mock's `after` is the same opaque offset cursor every
# other source's page token is (see backlot/pagination.py), so these take an offset. The ORDER BY is
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
LINEAR_ORDER_COLUMNS = {"createdAt": "created_ts", "updatedAt": "COALESCE(updated_ts, created_ts)"}


# `IssueSortInput` key -> the column it sorts on. `updatedAt` uses the same COALESCE the field
# itself is served with (an issue with no recorded edit reports its creation time), so a client
# crawling "newest first until older than X" sees a monotonic sequence rather than one that
# disagrees with the `updatedAt` it is reading.
LINEAR_SORT_COLUMNS = {
    "title": "title",
    "priority": "priority",
    "estimate": "estimate",
    "createdAt": "created_ts",
    "updatedAt": "COALESCE(updated_ts, created_ts)",
}


def _linear_order(order_by: str | None, descending: bool, sort=None) -> str:
    """The ORDER BY, always TOTAL (sort keys + ``doc_id``) — an offset page over a non-total order
    can silently repeat or skip a row between pages. ``sort`` (Linear's ``IssueSortInput``) wins over
    ``orderBy`` when both are given, matching the real API, where it is the richer multi-key form."""
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


def list_linear_issues(
    conn,
    team=None,
    *,
    visible_ids=None,
    limit=50,
    offset=0,
    order_by=None,
    descending=False,
    prefilter=None,
    sort=None,
    archived=False,
) -> list[sqlite3.Row]:
    """One page of Linear issues, optionally scoped to a team. ``prefilter`` is a necessary
    condition pushed into SQL, so an ``issues(filter:)`` query is an indexed scan rather than a
    full materialize-then-filter in Python."""
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
    clause, cparams = _acl_clause("linear", visible_ids=visible_ids)
    sql += clause + f" ORDER BY {_linear_order(order_by, descending, sort)} LIMIT ? OFFSET ?"
    params += cparams + [limit, offset]
    return conn.execute(sql, params).fetchall()


def count_linear_issues(
    conn, team=None, *, visible_ids=None, prefilter=None, archived=False
) -> int:
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
    clause, cparams = _acl_clause("linear", visible_ids=visible_ids)
    return conn.execute(sql + clause, params + cparams).fetchone()[0]


def linear_by_served_id(conn, served_id, visible_ids=None) -> sqlite3.Row | None:
    """One issue by the UUID the API reports (``synth.linear_id``), distinct from ``identifier``
    (see ``linear_issue_by_identifier``) -- unique-indexed, so this is a plain column lookup, not
    a reverse map built at startup."""
    col = served_id_column("linear")
    clause, cp = _acl_clause("linear", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM linear_issues WHERE {col} = ?{clause}", [served_id, *cp]
    ).fetchone()


def linear_issue_by_identifier(conn, identifier, visible_ids=None) -> sqlite3.Row | None:
    """Resolve a human identifier (``ENG-123``) to its issue. Identifiers are not unique (5,055
    repeat in one real corpus), so this deliberately returns the first by ``doc_id`` rather than pretending
    the lookup is unambiguous — the UUID form of ``issue(id:)`` is the exact one."""
    sql = "SELECT * FROM linear_issues WHERE identifier = ?"
    params: list = [identifier]
    clause, cparams = _acl_clause("linear", visible_ids=visible_ids)
    return conn.execute(sql + clause + " ORDER BY doc_id LIMIT 1", params + cparams).fetchone()


def list_linear_comments(
    conn, *, doc_id=None, visible_ids=None, limit=50, offset=0, prefilter=None
) -> list[sqlite3.Row]:
    """Comments on one issue, or across the corpus when ``doc_id`` is None (``Query.comments``).

    A comment row carries no ACL grant of its own; visibility is the parent issue's, so the ACL
    is applied to ``linear_issues`` through a join rather than to the comment table."""
    # The join exists ONLY to reach the parent issue's ACL, so an admin read (visible_ids None)
    # skips it: measured over 165k comments, the join cost ~40ms per page for nothing.
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
    clause, cparams = _acl_clause("linear", "i", visible_ids)
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
    clause, cparams = _acl_clause("linear", "i", visible_ids)
    return conn.execute(sql + clause, params + cparams).fetchone()[0]


def linear_children(
    conn, parent_doc_id, *, visible_ids=None, limit=50, offset=0, prefilter=None
) -> list[sqlite3.Row]:
    """Sub-issues of an issue — every row whose resolved ``parent_doc_id`` is this one.

    An indexed equality on a doc_id, NOT a join on ``identifier``: identifiers repeat, so a
    join would attach one issue's children to every issue sharing its key. Resolved once at import,
    which is what makes this the exact inverse of ``Issue.parent``."""
    sql = "SELECT * FROM linear_issues WHERE parent_doc_id = ?"
    params: list = [parent_doc_id]
    if prefilter:
        frag, fparams = prefilter
        sql += f" AND {frag}"
        params += fparams
    clause, cparams = _acl_clause("linear", visible_ids=visible_ids)
    sql += clause + " ORDER BY created_ts, doc_id LIMIT ? OFFSET ?"
    return conn.execute(sql, params + cparams + [limit, offset]).fetchall()


def linear_relations(
    conn, doc_id, *, inverse=False, visible_ids=None, limit=50, offset=0
) -> list[sqlite3.Row]:
    """One page of an issue's relations: ``Issue.relations`` (rows it declared) or, with
    ``inverse``, ``Issue.inverseRelations`` (rows pointing at it) — two ends of one stored row.

    ACL-scoped on the OTHER end: a relation whose counterpart the caller cannot read is omitted
    entirely, since surfacing its id would disclose that issue."""
    mine, other = ("to_doc_id", "from_doc_id") if inverse else ("from_doc_id", "to_doc_id")
    clause, cparams = _acl_clause("linear", "i", visible_ids)
    sql = (
        f"SELECT r.* FROM linear_relations r JOIN linear_issues i ON i.doc_id = r.{other} "
        f"WHERE r.{mine} = ?{clause} ORDER BY r.created_ts, r.id LIMIT ? OFFSET ?"
    )
    return conn.execute(sql, [doc_id, *cparams, limit, offset]).fetchall()


def linear_attachments(
    conn, doc_id, *, visible_ids=None, limit=50, offset=0, url=None, prefilter=None
) -> list[sqlite3.Row]:
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
    clause, cparams = _acl_clause("linear", "i", visible_ids)
    sql += clause + " ORDER BY a.seq LIMIT ? OFFSET ?"
    return conn.execute(sql, params + cparams + [limit, offset]).fetchall()


def linear_attachment_by_id(conn, served_id, visible_ids=None) -> sqlite3.Row | None:
    """Resolve a SERVED attachment uuid back to its row, scoped on the parent issue's ACL.

    No reverse index (attachments are only reached through their issue), so the id is matched by
    re-deriving it over visible rows — an attachment on a hidden issue is simply not found."""
    from backlot import synth

    join = "" if visible_ids is None else " JOIN linear_issues i ON i.doc_id = a.doc_id"
    clause, cparams = _acl_clause("linear", "i", visible_ids)
    rows = conn.execute(f"SELECT a.* FROM linear_attachments a{join} WHERE 1=1{clause}", cparams)
    for row in rows:
        if synth.linear_attachment_id(row["id"]) == served_id:
            return row
    return None


def linear_relation_by_id(conn, served_id, visible_ids=None) -> sqlite3.Row | None:
    """Same for a relation, scoped on BOTH ends: a relation is only visible to a caller who can
    read the issues at each side of it."""
    from backlot import synth

    if visible_ids is None:
        rows = conn.execute("SELECT * FROM linear_relations")
    else:
        clause_a, pa = _acl_clause("linear", "a", visible_ids)
        clause_b, pb = _acl_clause("linear", "b", visible_ids)
        rows = conn.execute(
            f"SELECT r.* FROM linear_relations r "
            f"JOIN linear_issues a ON a.doc_id = r.from_doc_id "
            f"JOIN linear_issues b ON b.doc_id = r.to_doc_id WHERE 1=1{clause_a}{clause_b}",
            [*pa, *pb],
        )
    for row in rows:
        if synth.linear_relation_id(row["id"]) == served_id:
            return row
    return None


def linear_distinct_values(conn) -> dict[str, list]:
    """The distinct entity names Linear's by-id roots have to resolve back to.

    ``@linear/sdk`` resolves relations lazily (``await issue.state`` fires a fresh
    ``workflowState(id:)``) and those uuids are one-way hashes of a name, so the app builds a reverse
    index at startup — see ``backlot.main._build_index``. Each entry is a DISTINCT over one column.
    Users come back as ``(email, display_name)`` so a user reached by id is named like one reached
    inline on an issue.
    """

    def col(name):
        return [
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT {name} FROM linear_issues WHERE {name} IS NOT NULL AND {name} != ''"
            )
        ]

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
        return [
            tuple(r)
            for r in conn.execute(f"SELECT DISTINCT team, {expr} FROM linear_issues{where}", params)
        ]

    people: dict[str, str | None] = {}
    for email_col, name_col in (
        ("author_email", "owner_display"),
        ("assignee_email", "assignee_display"),
    ):
        for email, display in conn.execute(
            f"SELECT DISTINCT {email_col}, {name_col} FROM linear_issues "
            f"WHERE {email_col} IS NOT NULL AND {email_col} != ''"
        ):
            # Keep the first NON-EMPTY display name: a person can appear as an author with no
            # recorded name and as an assignee with one, and the named form must win whichever
            # order the two passes see them in.
            people[email] = display or people.get(email)
    return {
        "states": per_team("state", default=LINEAR_DEFAULT_STATE),
        "projects": col("project"),
        "cycles": per_team("cycle"),
        "labels": [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT value FROM linear_issues, json_each(COALESCE(labels, '[]'))"
            )
        ],
        "releases": col("release"),
        "users": sorted(people.items()),
    }


def linear_team_has_visible(conn, team, visible_ids=None) -> bool:
    """Whether the caller can see ANY issue in a team — a ``LIMIT 1`` existence check that stops
    at the first visible row, so deciding which teams to surface costs a few cheap probes instead
    of an ACL-filtered ``GROUP BY`` over every issue in the corpus. Same shape as
    :func:`drive_folder_has_visible`."""
    clause, params = _acl_clause("linear", visible_ids=visible_ids)
    return (
        conn.execute(
            f"SELECT 1 FROM linear_issues WHERE team = ?{clause} LIMIT 1", [team, *params]
        ).fetchone()
        is not None
    )


# `Issue.state` is non-null in Linear, so a row with no recorded state is served this name (see
# linear_resolvers._state). It lives here because the reverse index and the visibility probe must
# agree with the resolver on it, or an id the API served becomes unresolvable.
LINEAR_DEFAULT_STATE = "Todo"


# The by-id roots (`project(id:)`, `workflowState(id:)`, …) resolve an entity that has no table
# of its own: it exists only as a column value on some issue. So "can the caller see it" means
# "can the caller see any issue carrying it", and each kind names the predicate that asks.
# Keyed exactly as backlot.main._build_index keys its reverse maps.
_LINEAR_ENTITY_PREDICATES = {
    "project": lambda v: ("project = ?", [v]),
    "cycle": lambda v: ("cycle = ? AND team = ?", [v[1], v[0]]),  # v = (team, name)
    # COALESCE, to match the synthesized default above: a caller reading an issue with no state
    # must be able to resolve the state id that issue served them.
    "state": lambda v: (
        f"COALESCE(state, '{LINEAR_DEFAULT_STATE}') = ? AND team = ?",
        [v[1], v[0]],
    ),  # v = (team, name)
    # A person is reachable as either end of an issue.
    "user": lambda v: ("(author_email = ? OR assignee_email = ?)", [v[0], v[0]]),  # v = (email, _)
    "label": lambda v: (
        "EXISTS (SELECT 1 FROM json_each(COALESCE(labels, '[]')) WHERE value = ?)",
        [v],
    ),
    "release": lambda v: ("release = ?", [v]),
}


def linear_entity_has_visible(conn, kind: str, value, visible_ids=None) -> bool:
    """Whether the caller can see ANY issue carrying this project / cycle / state / person / label.

    Without it the by-id roots are an existence oracle: the reverse index is an unfiltered DISTINCT
    built at startup, so a caller denied an issue could still resolve that issue's project, label,
    cycle, state and assignee. A ``LIMIT 1`` probe, so it stops at the first visible carrier."""
    build = _LINEAR_ENTITY_PREDICATES.get(kind)
    if build is None:
        raise ValueError(f"unknown linear entity kind {kind!r}")
    frag, params = build(value)
    clause, cparams = _acl_clause("linear", visible_ids=visible_ids)
    return (
        conn.execute(
            f"SELECT 1 FROM linear_issues WHERE {frag}{clause} LIMIT 1", [*params, *cparams]
        ).fetchone()
        is not None
    )


def linear_team_issue_counts(conn, visible_ids=None) -> dict[str, int]:
    """team -> visible issue count, in one grouped scan — ``Team.issueCount`` for a whole page of
    teams without a COUNT(*) per team."""
    clause, cparams = _acl_clause("linear", visible_ids=visible_ids)
    rows = conn.execute(
        f"SELECT team, COUNT(*) FROM linear_issues WHERE 1=1{clause} GROUP BY team", cparams
    )
    return {r[0]: r[1] for r in rows}


def hubspot_associations(
    conn, from_doc_id, to_type, *, after_to_doc_id=None, visible_ids=None, limit=500
) -> list[sqlite3.Row]:
    """One page of associations from a CRM record to records of ``to_type``, ACL-scoped on the
    target. Keyset-paginated by ``to_doc_id`` for the same reason the listings are: the API's
    cursor is the last record id the caller saw, and a record past the first page must stay
    reachable.

    Joined to ``hubspot_objects`` for the target's own ``served_id`` (as ``to_served_id``), rather
    than leaving the v4 payload's ``toObjectId`` to recompute ``synth.hubspot_record_id(to_doc_id)``
    the way gmail's ``threadId`` re-hashes a root row (see ``routers.google._gmail_ids``): gmail's
    seed is never probed, so the hash and the stored column always agree, but hubspot's IS probed
    (#51) -- a collision walk can hand a record an id different from its raw hash, and the v4
    payload has to report the one the target's own row actually resolves at. The join is safe
    (INNER, not LEFT): ``resolve_cross_references`` refuses an association whose target object type
    cannot be resolved, so every ``to_doc_id`` here already has a row in ``hubspot_objects``."""
    sql = (
        "SELECT hubspot_associations.*, o.served_id AS to_served_id FROM hubspot_associations "
        "JOIN hubspot_objects o ON o.doc_id = hubspot_associations.to_doc_id "
        "WHERE from_doc_id = ? AND to_type = ?"
    )
    params: list = [from_doc_id, to_type]
    if after_to_doc_id:
        sql += " AND to_doc_id > ?"
        params.append(after_to_doc_id)
    clause, cparams = _acl_clause("hubspot", "hubspot_associations", visible_ids, col="to_doc_id")
    sql += clause + " ORDER BY to_doc_id LIMIT ?"
    params += cparams + [limit]
    return conn.execute(sql, params).fetchall()


def list_drive_folder(conn, folder, visible_ids=None, limit=100, offset=0) -> list[sqlite3.Row]:
    """Non-trashed files directly in a Drive folder — SQL-scoped + SQL-paginated, so listing a
    big folder costs one page of rows per request, not a full-corpus scan on every page."""
    sql = "SELECT * FROM gdrive_files WHERE folder = ? AND COALESCE(trashed, 0) = 0"
    params: list = [folder]
    clause, cparams = _acl_clause("google_drive", visible_ids=visible_ids)
    # No ORDER BY: the folder index already yields a stable order for pagination, and adding
    # ORDER BY doc_id forces a per-page sort of the whole folder (≈30x slower on a big folder).
    sql += clause + " LIMIT ? OFFSET ?"
    params += cparams + [limit, offset]
    return conn.execute(sql, params).fetchall()


def list_drive_by_name(
    conn, name_substr, container=None, visible_ids=None, limit=100_000, offset=0
) -> list[sqlite3.Row]:
    """Non-trashed Drive files whose title contains ``name_substr`` (Drive's ``name contains 'X'``),
    optionally within a folder — the SQL path for a name lookup. Without it the endpoint listed the
    WHOLE corpus (~25k rows, ~1.6s) then substring-matched in Python; a title LIKE builds only the
    matches (~14ms). LIKE wildcards in the needle are escaped so they stay literal."""
    needle = _like_escape(name_substr)
    # SQLite LIKE is case-insensitive for ASCII by default (matching Drive's case-insensitive
    # `name contains`); no lower() wrapper, which would force a per-row scan.
    sql = "SELECT * FROM gdrive_files WHERE COALESCE(trashed, 0) = 0 AND title LIKE ? ESCAPE '\\'"
    params: list = [f"%{needle}%"]
    if container is not None:
        sql += " AND folder = ?"
        params.append(container)
    clause, cparams = _acl_clause("google_drive", visible_ids=visible_ids)
    sql += clause + " LIMIT ? OFFSET ?"
    params += cparams + [limit, offset]
    return conn.execute(sql, params).fetchall()


def count_drive_folder(conn, folder, visible_ids=None) -> int:
    sql = "SELECT COUNT(*) FROM gdrive_files WHERE folder = ? AND COALESCE(trashed, 0) = 0"
    params: list = [folder]
    clause, cparams = _acl_clause("google_drive", visible_ids=visible_ids)
    sql += clause
    params += cparams
    return conn.execute(sql, params).fetchone()[0]


def drive_folder_has_visible(conn, folder, visible_ids=None) -> bool:
    """Whether the caller can see any file in a folder — a ``LIMIT 1`` existence check (stops at
    the first visible file), so deciding which folders to surface is a couple of cheap probes."""
    clause, params = _acl_clause("google_drive", visible_ids=visible_ids)
    sql = f"SELECT 1 FROM gdrive_files WHERE folder = ?{clause} LIMIT 1"
    return conn.execute(sql, [folder, *params]).fetchone() is not None


def drive_usage_bytes(conn, visible_ids=None) -> tuple[int, int]:
    """``(bytes stored, bytes in the trash)`` over the Drive files this caller can see — what
    ``about.get`` serves as ``storageQuota``. One query, so the quota costs a single scan.

    ``length(CAST(content AS BLOB))`` is deliberate: SQLite's ``length()`` on a TEXT column counts
    CHARACTERS, while the ``size`` every file resource carries is ``len(content.encode("utf-8"))``.
    Without the cast a corpus holding any non-ASCII text reports a quota smaller than the sum of
    the sizes the same caller reads out of ``files.list`` — a divergence no client could explain."""
    nbytes = "length(CAST(content AS BLOB))"
    sql = (
        f"SELECT COALESCE(SUM({nbytes}), 0), "
        f"COALESCE(SUM(CASE WHEN COALESCE(trashed, 0) = 1 THEN {nbytes} ELSE 0 END), 0) "
        "FROM gdrive_files WHERE 1=1"
    )
    clause, params = _acl_clause("google_drive", visible_ids=visible_ids)
    total, trashed = conn.execute(sql + clause, params).fetchone()
    return int(total), int(trashed)


def count_documents(
    conn,
    source_type,
    container=None,
    visible_ids=None,
    author_email=None,
    state=None,
    exclude_trashed=False,
) -> int:
    # state: only valid for source_type="github" — it's the only items table with a `state`
    # column; passing it for any other source_type raises sqlite3.OperationalError. Likewise
    # exclude_trashed, which only gdrive_files has a column for. It has to track
    # list_documents': a count that includes rows the listing drops makes nextPageToken lie.
    tbl = table(source_type)
    sql = f"SELECT COUNT(*) FROM {tbl} WHERE 1=1"
    params: list = []
    sql = _scope(sql, params, grouping_col(source_type), container, author_email)
    if state is not None:
        sql += " AND COALESCE(state, 'open') = ?"
        params.append(state)
    if exclude_trashed:
        sql += " AND COALESCE(trashed, 0) = 0"
    clause, cparams = _acl_clause(source_type, visible_ids=visible_ids)
    sql += clause
    params += cparams
    return conn.execute(sql, params).fetchone()[0]


def get_document(conn, source_type, doc_id, visible_ids=None) -> sqlite3.Row | None:
    tbl = table(source_type)
    sql = f"SELECT * FROM {tbl} WHERE doc_id = ?"
    params: list = [doc_id]
    clause, cparams = _acl_clause(source_type, visible_ids=visible_ids)
    sql += clause
    params += cparams
    return conn.execute(sql, params).fetchone()


# --- fireflies ------------------------------------------------------------------
# Fireflies pages with `limit`/`skip` (offset-based, capped at 50 by the API) rather than a Relay
# connection, so these take a plain limit/offset and there is no cursor to keep stable.

# The API's `scope` decides WHICH text a `keyword` is matched against. `content` is the
# transcript's sentences concatenated, so "sentences" is a match on content and needs no join.
_FF_SCOPE_COLS = {"title": ("title",), "sentences": ("content",), "all": ("title", "content")}


def fireflies_scope_columns(scope: str | None) -> tuple[str, ...] | None:
    """The columns a `scope` searches, or None if the value isn't one Fireflies accepts."""
    return _FF_SCOPE_COLS.get((scope or "all").lower())


def _fireflies_where(
    *,
    channel=None,
    host_email=None,
    organizers=None,
    participants=None,
    from_ts=None,
    to_ts=None,
    keyword=None,
    scope=None,
    visible_ids=None,
) -> tuple[str, list]:
    sql = " WHERE 1=1"
    params: list = []
    if channel is not None:
        sql += " AND channel = ?"
        params.append(channel)
    if host_email:
        sql += " AND lower(author_email) = ?"
        params.append(host_email.lower())
    if organizers:
        # `organizer_email` is null when the organizer IS the host, which is the common case, so
        # the filter has to consider both — otherwise organizing a meeting you also hosted would
        # not match your own address.
        marks = ", ".join("?" for _ in organizers)
        sql += f" AND lower(COALESCE(organizer_email, author_email)) IN ({marks})"
        params += [o.lower() for o in organizers]
    for email in participants or []:
        # `participants` is a JSON array column; json_each is the exact membership test (a LIKE on
        # the serialized text would match an address that is merely a substring of another).
        sql += (
            " AND EXISTS (SELECT 1 FROM json_each(fireflies_transcripts.participants) "
            "WHERE lower(json_each.value) = ?)"
        )
        params.append(email.lower())
    if from_ts is not None:
        sql += " AND created_ts >= ?"
        params.append(from_ts)
    if to_ts is not None:
        sql += " AND created_ts <= ?"
        params.append(to_ts)
    if keyword:
        cols = fireflies_scope_columns(scope) or ("title", "content")
        sql += " AND (" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in cols) + ")"
        params += [f"%{_like_escape(keyword)}%" for _ in cols]
    clause, cparams = _acl_clause("fireflies", visible_ids=visible_ids)
    return sql + clause, params + cparams


def list_fireflies_transcripts(
    conn,
    *,
    channel=None,
    host_email=None,
    organizers=None,
    participants=None,
    from_ts=None,
    to_ts=None,
    keyword=None,
    scope=None,
    visible_ids=None,
    limit=50,
    offset=0,
) -> list[sqlite3.Row]:
    """One page of transcripts, newest first — the order the real API returns them in.

    The ORDER BY carries its ``doc_id`` tiebreak so it is TOTAL, and the tiebreak runs DESC WITH the
    sort key rather than against it: either direction is valid for an arbitrary tiebreak, but a
    uniform one is a backwards index scan while a mixed one is a temp b-tree over the whole table.
    """
    where, params = _fireflies_where(
        channel=channel,
        host_email=host_email,
        organizers=organizers,
        participants=participants,
        from_ts=from_ts,
        to_ts=to_ts,
        keyword=keyword,
        scope=scope,
        visible_ids=visible_ids,
    )
    return conn.execute(
        f"SELECT * FROM fireflies_transcripts{where} ORDER BY created_ts DESC, doc_id DESC "
        f"LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()


def count_fireflies_transcripts(conn, **kw) -> int:
    where, params = _fireflies_where(**kw)
    return conn.execute(f"SELECT COUNT(*) FROM fireflies_transcripts{where}", params).fetchone()[0]


def fireflies_transcript_by_id(conn, transcript_id, visible_ids=None) -> sqlite3.Row | None:
    """Resolve the API-facing transcript id to its row. Unlike Linear's identifier this IS
    unique — it is derived from the doc_id — so there is no first-match ambiguity."""
    sql = "SELECT * FROM fireflies_transcripts WHERE transcript_id = ?"
    clause, cparams = _acl_clause("fireflies", visible_ids=visible_ids)
    return conn.execute(sql + clause, [transcript_id] + cparams).fetchone()


def fireflies_sentences(conn, doc_id) -> list[sqlite3.Row]:
    """A transcript's sentences in order. No ACL clause: the caller has already been cleared for
    the parent transcript, and a sentence is not independently addressable."""
    return conn.execute(
        "SELECT * FROM fireflies_sentences WHERE doc_id = ? ORDER BY seq", (doc_id,)
    ).fetchall()


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
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='docs_fts'"
        ).fetchone()
        is not None
    )


def _src_tag(source_type: str) -> str:
    """A single collision-free token for the indexed ``src`` column. unicode61 splits on
    non-alphanumerics, so strip underscores (``google_drive`` -> ``srcgoogledrive``)."""
    return "src" + source_type.replace("_", "")


def build_fts(conn) -> bool:
    """(Re)build the docs_fts index over all source tables. No-op (False) without FTS5 — search
    then uses the LIKE fallback.

    ``src`` is an INDEXED column holding a per-source tag, so a search intersects that source's
    posting list with the term's (``src:srcjira AND "latency"``) instead of ranking every source's
    matches and post-filtering, which made a minority-source search scan past the others."""
    if not _fts5_ok(conn):
        return False
    conn.execute("DROP TABLE IF EXISTS docs_fts")
    # porter stemming (over unicode61) so a search matches morphological variants the way real
    # Slack/Gmail search do — "deletion" finds "deletions", "embedding" finds "embeddings". The
    # tokenizer applies to every column including the src tag, but that is safe: the stored tag and
    # the src: query term stem identically, and the 6 tags don't collide under porter.
    conn.execute(
        "CREATE VIRTUAL TABLE docs_fts USING fts5("
        "doc_id UNINDEXED, src, title, content, tokenize='porter unicode61')"
    )
    # Commit per source rather than once at the end: on an in-place rebuild of a large DB this
    # keeps each writer lock window to one source's index, so a concurrent reader (the live
    # server, with a busy_timeout) rides through instead of blocking on a single multi-GB commit.
    for src, tbl in SOURCE_TABLE.items():
        conn.execute(
            f"INSERT INTO docs_fts(doc_id, src, title, content) "
            f"SELECT doc_id, '{_src_tag(src)}', title, content FROM {tbl}"
        )
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
        chunk = doc_ids[i : i + 900]
        marks = ",".join("?" for _ in chunk)
        conn.execute(f"DELETE FROM docs_fts WHERE doc_id IN ({marks})", chunk)
        conn.execute(
            f"INSERT INTO docs_fts(doc_id, src, title, content) "
            f"SELECT doc_id, '{tag}', title, content FROM {tbl} WHERE doc_id IN ({marks})",
            chunk,
        )
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
    """A safe FTS5 MATCH string: alnum tokens, each quoted and ANDed, with an indexed ``src:``
    filter when the index is source-aware. ``phrase=True`` requires the tokens ADJACENT, for
    grep-style callers whose pattern is a literal — an AND would bury the exact match under docs
    that merely contain all the words scattered."""
    toks = re.findall(r"\w+", (query or "").lower())
    if not toks:
        return ""
    body = (
        ('"' + " ".join(toks) + '"')
        if (phrase and len(toks) > 1)
        else " AND ".join(f'"{t}"' for t in toks)
    )
    if has_src and source_type:
        return f"src:{_src_tag(source_type)} AND ({body})"
    return body


def search_documents(
    conn,
    query,
    source_type=None,
    visible_ids=None,
    limit=25,
    offset=0,
    container=None,
    phrase=False,
    order_by=None,
) -> list[sqlite3.Row]:
    """Keyword search over title + content within one source (FTS5-ranked; LIKE fallback), optionally
    scoped to one grouping unit. ``phrase=True`` matches the tokens adjacently and ranks a literal
    substring hit above a coincidental one. ``order_by``: ``None`` = relevance (bm25, Slack's
    ``sort=score``), ``"recency"``/``"recency_asc"`` = the doc's own timestamp
    (``sort=timestamp``)."""
    tbl = table(source_type)
    cont_sql, cont_p = "", []
    if container is not None:
        cont_sql, cont_p = f" AND {{a}}.{grouping_col(source_type)} = ?", [container]
    if _has_fts(conn):
        has_src = _fts_has_src(conn)
        m = _fts_match(query, source_type, has_src, phrase=phrase)
        if not m:
            return []
        clause, cparams = _acl_clause(source_type, "t", visible_ids)
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
            order_sql = (
                "(instr(lower(t.content), lower(?)) > 0 "
                "OR instr(lower(t.title), lower(?)) > 0) DESC, docs_fts.rank"
            )
            order_p = [lit, lit]
        sql = (
            f"SELECT t.* FROM docs_fts JOIN {tbl} t ON t.doc_id = docs_fts.doc_id "
            f"WHERE docs_fts MATCH ?{src_sql}{cont_sql.format(a='t')}{clause} "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?"
        )
        return conn.execute(sql, [m, *src_p, *cont_p, *cparams, *order_p, limit, offset]).fetchall()
    like = f"%{query}%"
    sql = f"SELECT * FROM {tbl} WHERE (title LIKE ? OR content LIKE ?){cont_sql.format(a=tbl)}"
    params: list = [like, like, *cont_p]
    clause, cparams = _acl_clause(source_type, visible_ids=visible_ids)
    sql += clause + " ORDER BY (CASE WHEN title LIKE ? THEN 0 ELSE 1 END), doc_id LIMIT ? OFFSET ?"
    params += cparams + [like, limit, offset]
    return conn.execute(sql, params).fetchall()


def count_search(
    conn, query, source_type, visible_ids=None, cap=1000, container=None, phrase=False
) -> int:
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
        clause, cparams = _acl_clause(source_type, "t", visible_ids)
        src_sql = "" if has_src else " AND docs_fts.source_type = ?"
        src_p = [] if has_src else [source_type]
        sql = (
            f"SELECT COUNT(*) FROM (SELECT t.doc_id FROM docs_fts JOIN {tbl} t "
            f"ON t.doc_id = docs_fts.doc_id WHERE docs_fts MATCH ?{src_sql}"
            f"{cont_sql.format(a='t')}{clause} LIMIT ?)"
        )
        return conn.execute(sql, [m, *src_p, *cont_p, *cparams, cap]).fetchone()[0]
    like = f"%{query}%"
    clause, cparams = _acl_clause(source_type, visible_ids=visible_ids)
    sql = (
        f"SELECT COUNT(*) FROM (SELECT doc_id FROM {tbl} WHERE (title LIKE ? OR content LIKE ?)"
        f"{cont_sql.format(a=tbl)}{clause} LIMIT ?)"
    )
    return conn.execute(sql, [like, like, *cont_p, *cparams, cap]).fetchone()[0]


def children(
    conn, source_type, parent_id, visible_ids=None, limit=1000, offset=0
) -> list[sqlite3.Row]:
    """Child documents (jira subtasks / confluence child pages) of a parent doc."""
    tbl = table(source_type)
    sql = f"SELECT * FROM {tbl} WHERE parent_id = ?"
    params: list = [parent_id]
    clause, cparams = _acl_clause(source_type, visible_ids=visible_ids)
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
        "FROM slack_messages WHERE channel = ?",
        (channel,),
    ).fetchone()


def list_slack_top_level(
    conn, channel, visible_ids=None, limit=100, offset=0, ts_lo=None, ts_hi=None
) -> list[sqlite3.Row]:
    """Top-level (thread-root/standalone) messages in a channel. ``ts_lo``/``ts_hi`` bound
    ``created_ts`` for a time-windowed conversations.history, so a day window is an indexed range
    rather than the whole channel filtered in Python. Widen the bounds by ±1s — the public ts
    carries a sub-second fraction — and re-check the exact float window in the caller."""
    sql = "SELECT * FROM slack_messages WHERE channel = ? AND thread_seq = 0"
    params: list = [channel]
    if ts_lo is not None or ts_hi is not None:
        lo = ts_lo if ts_lo is not None else -(1 << 62)
        hi = ts_hi if ts_hi is not None else (1 << 62)
        sql += " AND created_ts >= ? AND created_ts <= ?"
        params += [lo, hi]
    clause, cparams = _acl_clause("slack", visible_ids=visible_ids)
    sql += clause + " ORDER BY doc_id LIMIT ? OFFSET ?"
    params += cparams + [limit, offset]
    return conn.execute(sql, params).fetchall()


def count_slack_top_level(conn, channel, visible_ids=None) -> int:
    sql = "SELECT COUNT(*) FROM slack_messages WHERE channel = ? AND thread_seq = 0"
    params: list = [channel]
    clause, cparams = _acl_clause("slack", visible_ids=visible_ids)
    sql += clause
    params += cparams
    return conn.execute(sql, params).fetchone()[0]


def list_slack_channel_messages(conn, channel, visible_ids=None) -> list[sqlite3.Row]:
    """Every visible message in a channel (roots AND replies). Used by conversations.replies to
    resolve a ts that may belong to a reply (e.g. a search hit landed on one), since ts is
    synthesized and can't be queried directly."""
    sql = "SELECT * FROM slack_messages WHERE channel = ?"
    params: list = [channel]
    clause, cparams = _acl_clause("slack", visible_ids=visible_ids)
    sql += clause + " ORDER BY thread_id, thread_seq"
    params += cparams
    return conn.execute(sql, params).fetchall()


def list_gmail_in_range(
    conn, mailbox, ts_lo, ts_hi, visible_ids=None, limit=100_000, offset=0
) -> list[sqlite3.Row]:
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
    clause, cparams = _acl_clause("gmail", visible_ids=visible_ids)
    # created_ts DESC = newest-first (real Gmail's messages.list order); doc_id breaks ties into a
    # stable TOTAL order so keyset-free offset pagination can't dupe/skip rows across pages.
    sql += clause + " ORDER BY created_ts DESC, doc_id LIMIT ? OFFSET ?"
    params += cparams + [limit, offset]
    return conn.execute(sql, params).fetchall()


def slack_messages_at_created_ts(conn, channel, created_ts, visible_ids=None) -> list[sqlite3.Row]:
    """Visible channel messages at exactly this ``created_ts`` — the fast path for
    conversations.replies resolving a ts, whose integer part IS ``created_ts`` (see the router's
    ``_msg_ts``). Narrows to the handful of rows at that second instead of the whole channel. A row
    with a NULL ``created_ts`` misses this; the caller falls back to a full scan for those."""
    sql = "SELECT * FROM slack_messages WHERE channel = ? AND created_ts = ?"
    params: list = [channel, created_ts]
    clause, cparams = _acl_clause("slack", visible_ids=visible_ids)
    sql += clause + " ORDER BY thread_id, thread_seq"
    params += cparams
    return conn.execute(sql, params).fetchall()


def slack_reply_count(conn, root_doc_id, visible_ids=None) -> int:
    sql = "SELECT COUNT(*) FROM slack_messages WHERE thread_id = ? AND thread_seq > 0"
    params: list = [root_doc_id]
    clause, cparams = _acl_clause("slack", visible_ids=visible_ids)
    sql += clause
    params += cparams
    return conn.execute(sql, params).fetchone()[0]


def slack_channels_for_principals(conn, principals) -> set[str]:
    """Channels with at least one doc granted to any of ``principals``. Starts from the
    principal-indexed ``slack_acl`` (idx_slack_acl_pid) instead of scanning the whole slack table,
    so it's cheap even at millions of rows — used to list a non-admin caller's visible channels."""
    principals = list(principals)
    if not principals:
        return set()
    marks = ",".join("?" for _ in principals)
    rows = conn.execute(
        f"SELECT DISTINCT d.channel FROM {acl_table('slack')} a "
        f"JOIN slack_messages d ON d.doc_id = a.doc_id WHERE a.principal_id IN ({marks})",
        principals,
    )
    return {r[0] for r in rows}


def slack_reply_authors(conn, root_doc_id, visible_ids=None) -> list[str]:
    """Distinct reply-author emails in a thread, in reply order (for reply_users)."""
    sql = "SELECT author_email FROM slack_messages WHERE thread_id = ? AND thread_seq > 0"
    params: list = [root_doc_id]
    clause, cparams = _acl_clause("slack", visible_ids=visible_ids)
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
    clause, cparams = _acl_clause("slack", visible_ids=visible_ids)
    sql += clause + " ORDER BY thread_seq"
    params += cparams
    return conn.execute(sql, params).fetchall()


def gmail_thread(conn, thread_id, visible_ids=None) -> list[sqlite3.Row]:
    """All messages in a Gmail thread (root + replies), ordered, ACL-filtered."""
    sql = "SELECT * FROM gmail_messages WHERE thread_id = ?"
    params: list = [thread_id]
    clause, cparams = _acl_clause("gmail", visible_ids=visible_ids)
    sql += clause + " ORDER BY thread_seq"
    params += cparams
    return conn.execute(sql, params).fetchall()


def gmail_by_served_id(conn, served_id, visible_ids=None) -> sqlite3.Row | None:
    """One message by the id the API reports. The stored column is lowercase hex; callers pass the
    id as the client spelled it, so fold case here rather than at each call site."""
    clause, cp = _acl_clause("gmail", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM gmail_messages WHERE served_id = ?{clause}", [served_id.lower(), *cp]
    ).fetchone()


def github_by_served_number(conn, repo, served_number, visible_ids=None) -> sqlite3.Row | None:
    """One issue/PR by the number the API reports, scoped to its repo — github's own uniqueness
    rule (see store.SERVED_ID's `scope` for github, and idx_github_served). A unique-indexed
    column lookup, not a reverse map built at startup: the number is assigned at import (see
    :mod:`backlot.importer.byo`'s ``resolve_github_numbers``), so it needs neither the memory nor
    the per-boot scan, and it cannot be ambiguous.

    A `kind='file'` row's served_number is always NULL (see idx_github_doc_number's comment on the
    schema), so this can never resolve to a file even though the column it queries is shared with
    issues/PRs."""
    col = served_id_column("github")
    clause, cp = _acl_clause("github", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM github_items WHERE repo = ? AND {col} = ?{clause}",
        [repo, served_number, *cp],
    ).fetchone()


def jira_by_served_number(conn, project, served_number, visible_ids=None) -> sqlite3.Row | None:
    """One issue by the numeric SUFFIX the API reports, scoped to its project — jira's own
    uniqueness rule (see store.SERVED_ID's `scope` for jira, and idx_jira_served). A
    unique-indexed column lookup, not a reverse map built at startup: the suffix is assigned at
    import (see :mod:`backlot.importer.byo`'s ``resolve_jira_numbers``), so it needs neither the
    memory nor the per-boot scan, and it cannot be ambiguous.

    Callers compose the served KEY themselves (``f"{project_key}-{served_number}"``, see
    ``synth.jira_key`` and ``routers.atlassian``'s ``_issue_key``/``_resolve_jira_key``) — the
    project's own prefix is a container-level fact (``main._build_index``'s
    ``jira_project_keys``/``jira_project_containers``), not something this row-scoped lookup
    resolves."""
    col = served_id_column("jira")
    clause, cp = _acl_clause("jira", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM jira_issues WHERE project = ? AND {col} = ?{clause}",
        [project, served_number, *cp],
    ).fetchone()


# --- GitHub file items (kind='file') ----------------------------------------


def list_repo_files(conn, repo, visible_ids=None, limit=10_000, offset=0) -> list[sqlite3.Row]:
    clause, cp = _acl_clause("github", visible_ids=visible_ids)
    sql = (
        "SELECT * FROM github_items WHERE repo = ? AND kind = 'file'"
        + clause
        + " ORDER BY path LIMIT ? OFFSET ?"
    )
    return conn.execute(sql, [repo, *cp, limit, offset]).fetchall()


def list_repo_file_paths(conn, repo, visible_ids=None, limit=10_000, offset=0) -> list[str]:
    """Just the paths :func:`list_repo_files` would return, in the same order.

    For a caller that only needs to CHOOSE among a repo's files rather than read them — github's
    pull-request changeset picks a few paths per pull — and would otherwise drag every file's
    content along to do it. On a 3000-file repo that is ~4 MB of content read per pull, and a
    ``/pulls`` page synthesizes a changeset per row.
    """
    clause, cp = _acl_clause("github", visible_ids=visible_ids)
    sql = (
        "SELECT path FROM github_items WHERE repo = ? AND kind = 'file'"
        + clause
        + " ORDER BY path LIMIT ? OFFSET ?"
    )
    return [r[0] for r in conn.execute(sql, [repo, *cp, limit, offset])]


def get_github_comment(conn, served_id: int) -> sqlite3.Row | None:
    """One comment by the id the API reports, carrying `doc_id` so the caller can ACL-check the
    document it belongs to.

    A unique-indexed column lookup, not a reverse map built at startup: the id is assigned at import
    (see :mod:`backlot.importer.byo`), so it needs neither the memory nor the per-boot scan, and it
    cannot be ambiguous.
    """
    return conn.execute(
        "SELECT id, doc_id, seq, author_email, body, created_ts, reactions, path, line, diff_hunk, "
        "served_id "
        "FROM github_comments WHERE served_id = ?",
        (served_id,),
    ).fetchone()


def confluence_by_served_id(conn, served_id, visible_ids=None) -> sqlite3.Row | None:
    """One page by the id the API reports. A unique-indexed column lookup, not a reverse map built
    at startup: the id is assigned at import, so it costs neither the per-boot scan nor the memory,
    and it cannot be ambiguous."""
    col = served_id_column("confluence")
    clause, cp = _acl_clause("confluence", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM confluence_pages WHERE {col} = ?{clause}", [served_id, *cp]
    ).fetchone()


def notion_by_served_id(conn, served_id, visible_ids=None) -> sqlite3.Row | None:
    """One page or database by the id the API reports -- a dashed lowercase UUID. The caller
    canonicalizes a dashless or mixed-case spelling to that same form before calling (see
    routers.notion._norm); this is a plain equality lookup, not a case- or dash-insensitive one. A
    unique-indexed column lookup, not a reverse map built at startup."""
    col = served_id_column("notion")
    clause, cp = _acl_clause("notion", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM notion_pages WHERE {col} = ?{clause}", [served_id, *cp]
    ).fetchone()


def notion_by_data_source_id(conn, data_source_id, visible_ids=None) -> sqlite3.Row | None:
    """One DATABASE by its data source id -- the 2025-09-03 API's query target, a second and
    unrelated served-id space for the same row (see the schema comment on idx_notion_served_ds).
    Populated only for subtype='database' rows, so a page's NULL column never matches -- unlike
    notion_by_served_id (which spans both kinds), a match here already implies the right kind and
    the caller needs no subtype check of its own."""
    clause, cp = _acl_clause("notion", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM notion_pages WHERE served_data_source_id = ?{clause}",
        [data_source_id, *cp],
    ).fetchone()


def github_comments(conn, doc_id, *, anchored: bool | None = None) -> list[sqlite3.Row]:
    """``github_comments`` rows for one document, carrying the review-comment columns.

    A github-specific reader because :func:`doc_comments` selects one fixed column list for six
    tables and so cannot carry ``path``/``line``/``diff_hunk``.

    ``anchored`` splits the two resources this one table holds and real GitHub keeps apart — see the
    table's own comment in ``SCHEMA``. ``True`` returns only line-anchored review comments, ``False``
    only conversation comments, ``None`` both.
    """
    where = {None: "", True: " AND path IS NOT NULL", False: " AND path IS NULL"}[anchored]
    return conn.execute(
        "SELECT id, doc_id, seq, author_email, body, created_ts, reactions, path, line, diff_hunk, "
        "served_id "
        "FROM github_comments WHERE doc_id = ?" + where + " ORDER BY seq",
        (doc_id,),
    ).fetchall()


def count_repo_files(conn, repo, visible_ids=None) -> int:
    clause, cp = _acl_clause("github", visible_ids=visible_ids)
    return conn.execute(
        "SELECT COUNT(*) FROM github_items WHERE repo = ? AND kind = 'file'" + clause, [repo, *cp]
    ).fetchone()[0]


def get_repo_file(conn, repo, path, visible_ids=None) -> sqlite3.Row | None:
    clause, cp = _acl_clause("github", visible_ids=visible_ids)
    return conn.execute(
        "SELECT * FROM github_items WHERE repo = ? AND kind = 'file' AND path = ?" + clause,
        [repo, path, *cp],
    ).fetchone()


# --- grouping units (channels/mailboxes/folders/repos/projects/spaces) & principals ---


def list_containers(conn, source_type) -> list[sqlite3.Row]:
    """List a service's grouping units as rows with `name` + `group_id` (uniform API)."""
    gtable, gcol = grouping_table(source_type), grouping_col(source_type)
    return conn.execute(f"SELECT {gcol} AS name, group_id FROM {gtable} ORDER BY {gcol}").fetchall()


def get_container(conn, source_type, name) -> sqlite3.Row | None:
    gtable, gcol = grouping_table(source_type), grouping_col(source_type)
    return conn.execute(
        f"SELECT {gcol} AS name, group_id FROM {gtable} WHERE {gcol} = ?", (name,)
    ).fetchone()


def linear_team_by_served_id(conn, served_id) -> str | None:
    """Resolve a team UUID (`synth.linear_team_id`) to its container name. Unique-indexed, so a
    plain column lookup rather than the reverse map `main._build_index` used to build (#51)."""
    row = conn.execute("SELECT team FROM linear_teams WHERE served_id = ?", (served_id,)).fetchone()
    return row["team"] if row else None


def linear_team_by_served_key(conn, served_key) -> str | None:
    """Resolve a team KEY (`synth.linear_team_key`, e.g. "ENG") to its container name.

    The key is NOT injective -- two containers can reduce to the same one -- so `served_key`
    carries no UNIQUE index, and this breaks the tie exactly as the reverse map it replaced did:
    `main._build_index`'s old loop walked `list_containers`'s own team-NAME order and kept the
    FIRST team a key was seen on (`setdefault`). `ORDER BY team LIMIT 1` reproduces that -- change
    either without the other and a key that used to resolve to one team silently resolves to a
    different one."""
    row = conn.execute(
        "SELECT team FROM linear_teams WHERE served_key = ? ORDER BY team LIMIT 1", (served_key,)
    ).fetchone()
    return row["team"] if row else None


def list_users(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, display_name, email FROM principals WHERE type = 'user' ORDER BY id"
    ).fetchall()


def get_user(conn, email) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, display_name, email FROM principals WHERE type = 'user' AND id = ?", (email,)
    ).fetchone()


def fireflies_user_by_served_id(conn, served_id) -> str | None:
    """Reverse a served Fireflies `user_id` (`synth.fireflies_user_id`) to the address it hashes.
    Unique-indexed column lookup, not the reverse map `main._build_index` used to build (#51)."""
    row = conn.execute(
        "SELECT email FROM fireflies_users WHERE served_id = ?", (served_id,)
    ).fetchone()
    return row["email"] if row else None


def user_group_ids(conn, email) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT group_id FROM group_members WHERE user_id = ?", (email,)
        ).fetchall()
    ]


def group_members(conn, group_id) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.id, p.display_name, p.email FROM group_members gm "
        "JOIN principals p ON p.id = gm.user_id WHERE gm.group_id = ? ORDER BY p.id",
        (group_id,),
    ).fetchall()


def slack_channel_member_emails(conn, channel, limit=100, offset=0) -> list[str]:
    """One page of a channel's members, in email order.

    Membership is the set of people who have spoken in the channel — the only per-channel signal
    the corpus carries. It replaces answering every public channel with the whole roster, which is
    a shape real Slack cannot produce (its membership differs per channel). Index-only on
    idx_slack_channel_author, so a page costs a seek rather than a scan of the channel."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT author_email FROM slack_messages WHERE channel = ? "
            "ORDER BY author_email LIMIT ? OFFSET ?",
            (channel, limit, offset),
        )
    ]


def slack_channel_member_counts(conn) -> dict[str, int]:
    """Every channel's member count in one pass. Per-channel COUNT(DISTINCT) is ~1.9s on the
    biggest channel measured, and conversations.list shapes every channel in the page, so counting
    them one at a time would be minutes per request; this is 12.2s once."""
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT channel, COUNT(DISTINCT author_email) FROM slack_messages GROUP BY channel"
        )
    }


def count_slack_channel_members(conn, channel) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT author_email) FROM slack_messages WHERE channel = ?", (channel,)
    ).fetchone()[0]


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
        f"SELECT DISTINCT a.principal_type, a.principal_id FROM {acl_table(source_type)} a "
        f"JOIN {tbl} d ON d.doc_id = a.doc_id WHERE d.{gcol} = ?",
        (container,),
    ).fetchall()


def container_has_public(conn, source_type, container) -> bool:
    tbl, gcol = table(source_type), grouping_col(source_type)
    return (
        conn.execute(
            f"SELECT 1 FROM {acl_table(source_type)} a JOIN {tbl} d ON d.doc_id = a.doc_id "
            f"WHERE d.{gcol} = ? AND a.principal_type = 'org' LIMIT 1",
            (container,),
        ).fetchone()
        is not None
    )


def doc_grants(conn, source_type, doc_id) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT principal_type, principal_id FROM {acl_table(source_type)} WHERE doc_id = ? "
        "ORDER BY principal_type, principal_id",
        (doc_id,),
    ).fetchall()


def docs_with_grants(conn, source_type, doc_ids: list[str]) -> set[str]:
    """The subset of ``doc_ids`` that have at least one ACL grant — one query (chunked to stay
    under SQLite's variable limit) instead of a per-doc ``doc_grants`` call when building a list."""
    out: set[str] = set()
    for i in range(0, len(doc_ids), 900):
        chunk = doc_ids[i : i + 900]
        marks = ",".join("?" for _ in chunk)
        out.update(
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT doc_id FROM {acl_table(source_type)} WHERE doc_id IN ({marks})",
                chunk,
            ).fetchall()
        )
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


def doc_member_emails(conn, source_type, doc_id) -> set[str] | None:
    return _expand_grants(conn, doc_grants(conn, source_type, doc_id))


# --- comments -------------------------------------------------------------------


def doc_comments(conn, source_type, doc_id) -> list[sqlite3.Row]:
    tbl = COMMENT_TABLE.get(source_type)
    if tbl is None:
        return []
    return conn.execute(
        f"SELECT id, seq, author_email, body, created_ts, reactions FROM {tbl} "
        "WHERE doc_id = ? ORDER BY seq",
        (doc_id,),
    ).fetchall()
