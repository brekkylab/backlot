"""Read-only SQLite access layer.

One table per service, with that service's own columns and its own grouping-unit table
(``slack_channels``, ``github_repos``, …) — never one crammed ``documents`` table, so a column
one service needs never lands on another's rows. The principal / group-membership relationship
tables are shared, keyed by names that ARE globally unique. ACL grants are not: each source has
its own ``<source>_acl`` table (see ``ACL_TABLE``), because a served id is unique only *within* a
source — a shared table keyed on one let two documents in different sources that happened to share
an id merge their grants.

**A row is identified by the id the API serves it under, and by nothing else**. The dataset's
own identifier scheme does not survive the import: a corpus's ``doc_id`` seeds a synthesized value
and is then discarded, so there is no ``doc_id`` column anywhere and no map from one. Each table's
PRIMARY KEY is that served id — see :data:`ID_COLUMNS`, which also names it, since the column is
spelled the way its vendor spells it (``number``, ``key``, ``ts``, otherwise ``id``).

Every doc table carries ``author_email`` and ``content`` after its identifier and its grouping
column, plus ``title`` on the ten sources whose documents have one (see ``TITLELESS``), which is
what keeps listing / ACL / pagination uniform via the ``GROUPING`` registry. Every listing takes
``visible_ids``: ``None`` = admin, otherwise results are filtered to docs whose ACL grants intersect
it. JSON columns are TEXT — read with :func:`jcol`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
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
# (id, <parent's id column>, seq, author_email, body, created_ts, reactions) that
# :func:`doc_comments` reads, and adds its own columns beside it (see fireflies_sentences).
# The parent reference is named after the parent's OWN id column (`jira_comments.key`,
# `github_comments.number`), not a uniform `doc_id`: a child row points at a served id, and
# spelling it the way the parent's table spells it is what keeps the two from drifting into
# separate namespaces.
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


# source_type -> the columns in its child-rows table that point at the parent, positionally
# matching :data:`ID_COLUMNS`. The parent's OWN column name is reused, so a child row and the row
# it hangs off are named the same way — except where that bare name would collide with the child's
# own `id`, in which case it is qualified by the parent's noun (`page_id`, `issue_id`). Explicit
# rather than derived, because that qualification is a judgement about readability that a rule
# cannot make on its own.
COMMENT_PARENT = {
    "jira": ("key",),
    "confluence": ("page_id",),
    "github": ("repo", "number"),
    "notion": ("page_id",),
    "linear": ("issue_id",),
    "fireflies": ("transcript_id",),
}


def comment_parent_columns(source_type: str) -> tuple[str, ...]:
    return COMMENT_PARENT[source_type]


# Each source's ACL. Per source, not one shared table, because a served id is per-source too (see
# test_two_sources_may_share_an_id): keyed corpus-wide, two documents that merely share an id
# shared their grants and the union was enforced. Each table keys on its source's own
# :data:`ID_COLUMNS`, so a grant and the row it governs are named the same way.
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


def mailbox_for(conn, email: str) -> str:
    """The value the corpus spelled this address's Gmail mailbox as.

    A mailbox is the one container a client never names: every other source takes its container
    from the request path, while ``users/me/messages`` derives it from the caller's own address.
    A corpus is free to state that mailbox as the address, as its local part, or as a slug of
    either, so the address is resolved against the mailboxes the corpus DID state instead of
    assuming one spelling. Falls back to the underscore slug — the spelling a name-derived
    mailbox takes — so an address the corpus holds no mailbox for still scopes to nothing.
    """
    local = email.split("@")[0].lower()
    candidates = [
        email.lower(),
        re.sub(r"[^a-z0-9]+", "_", local).strip("_"),
        re.sub(r"[^a-z0-9]+", "-", local).strip("-"),
        local,
    ]
    stated = {
        row[0].lower(): row[0]
        for row in conn.execute(
            f"SELECT mailbox FROM gmail_mailboxes WHERE lower(mailbox) IN "
            f"({','.join('?' * len(candidates))})",
            candidates,
        )
    }
    return next((stated[c] for c in candidates if c in stated), candidates[1])


# source_type -> the column(s) a row is ADDRESSED by after import: its PRIMARY KEY, the key its
# `<source>_acl` table and its FTS index carry, and what every cross-row reference points at.
# There is no `doc_id` beside them — the dataset's own identifier seeds the value and is then
# discarded, so this registry is the whole answer to "what identifies a row".
#
# Each column is spelled the way its VENDOR spells it rather than forced to a uniform `id`: a real
# GitHub issue carries both an `id` and a `number` and its API addresses it by the number, a Jira
# issue by its `key`, a Slack message by its `ts`. Serving `id` for any of those would be a field
# the vendor's own client never asks for.
#
# A PAIR is the vendor's own uniqueness rule, not caution: a GitHub number is unique within its
# repo, a Slack ts within its channel, an S3 key within its bucket — the same key in two buckets is
# ordinary S3. A Jira key is unique across a whole site, so it stands alone. Every value is a tuple
# regardless, so `_acl_clause`, the ACL tables, the FTS indexes and `get_document` are n-ary
# uniformly and no source needs a special case.
ID_COLUMNS = {
    "slack": ("channel", "ts"),
    "gmail": ("id",),
    "google_drive": ("id",),
    "github": ("repo", "number"),
    "jira": ("key",),
    "confluence": ("id",),
    "notion": ("id",),
    "s3": ("bucket", "key"),
    "hubspot": ("id",),
    "linear": ("id",),
    "fireflies": ("id",),
}


# source_type -> the columns a listing is ORDERED by, where that is not the key itself. Only
# slack differs, and it has to: its `ts` is TEXT (`"<seconds>.<fraction>"`, the spelling Slack's
# own API uses), so ordering by it compares digit by digit — `"9.5"` after `"10.5"`. Ordering by
# the integer second first restores chronology, with the ts breaking ties so the order stays
# total and an offset page still cannot skip or repeat a row.
ORDER_COLUMNS = {"slack": ("created_ts", "ts")}


def order_columns(source_type: str) -> tuple[str, ...]:
    """The columns a listing of this source is ordered by."""
    return ORDER_COLUMNS.get(source_type) or id_columns(source_type)


def id_columns(source_type: str) -> tuple[str, ...]:
    """The full key a row of this source is addressed by — its PRIMARY KEY, in key order."""
    try:
        return ID_COLUMNS[source_type]
    except KeyError:
        raise ValueError(f"unknown source_type {source_type!r}")


def id_column(source_type: str) -> str:
    """Just the row-distinguishing column, without the container that scopes it — ``number`` for
    github, ``ts`` for slack, and the whole key for the sources whose id needs no container."""
    return id_columns(source_type)[-1]


# The SQL type each identifier column is declared with, for the tables that REFER to a document
# rather than hold it (the ACL tables, generated below). Affinity is not cosmetic here: store an
# integer github number under a TEXT-affinity column and SQLite writes '7', after which
# `_acl.number = github_items.number` compares '7' against 7 and matches nothing — an ACL that
# silently grants no one. Only the two non-text ids need an entry; everything else is TEXT.
# `INT` rather than `INTEGER` for confluence, for the rowid-alias reason its own table explains.
_ID_COLUMN_TYPES = {("github", "number"): "INTEGER", ("confluence", "id"): "INT"}


def id_column_type(source_type: str, column: str) -> str:
    return _ID_COLUMN_TYPES.get((source_type, column), "TEXT")


# source_type -> (seed function, uniqueness scope), for the sources whose served id is a pure
# 1-arity function of the incoming record's dataset id. The value is ASSIGNED at import (see
# backlot.importer.byo) rather than hashed at serve time: a hash into any fixed range collides by
# the birthday bound, and last-writer-wins leaves one document unreachable at its own id. The
# uniform `(dataset_id) -> candidate` shape is what lets one assignment method probe every source.
#
# `scope` is the columns the assignment probe must hold fixed for the served value to be
# unambiguous in BACKLOT'S OWN lookup — not necessarily the vendor's own uniqueness rule. It is
# `None` when Backlot resolves the id with no container (a flat column/index lookup), or the
# source's own GROUPING column when Backlot's own lookup is already scoped to one container (a
# GitHub number is looked up within its repo). gmail and hubspot are
# real-world per-container ids (per-mailbox; per-object-type, which is why every HubSpot route
# carries `{objectType}`) that Backlot nonetheless resolves flat (`routers.google`/`routers.hubspot`
# look an id up with no container, hubspot only checking `object_type` afterwards) — `None` there
# is the safe over-constraint Backlot's own lookup needs, not a claim about the vendor.
#
# Absent here, and deliberately: `jira`, whose key is COMPOSED from its project's prefix rather
# than seeded 1-arity; `slack`, whose ts is a function of the row's `created_ts` and its thread
# root as well as its dataset id; and `s3`, whose (bucket, key) the corpus states outright and
# nothing synthesizes. Each gets its own assignment pass instead of widening this tuple to fit it.
ID_SEED = {
    "confluence": (synth.confluence_id, None),
    "gmail": (synth.gmail_message_id, None),
    "notion": (synth.notion_id, None),
    "hubspot": (synth.hubspot_record_id, None),
    # Linear's hashed uuid, kept distinct from `identifier` (already stored, and which must keep
    # winning — see GROUPING's linear comment) — this column is the OTHER spelling `issue(id:)`
    # accepts.
    "linear": (synth.linear_id, None),
    "github": (synth.github_number, grouping_col("github")),
    # Drive's own file id -- unprobed, like gmail/notion/linear; see
    # synth.gdrive_file_id's docstring for the digest-bytes reasoning and idx_gdrive_id's
    # schema comment for the collision argument and the folder-id disjointness.
    "google_drive": (synth.gdrive_file_id, None),
    # A transcript's own 24-hex id. A BYO record may still provide one (its `transcript_id`), which
    # wins the way a provided github number does; the seed fills every row that gives none.
    "fireflies": (synth.fireflies_id, None),
}


def id_seed(source_type: str) -> Callable[[str], object]:
    return ID_SEED[source_type][0]


def id_seed_scope(source_type: str) -> str | None:
    return ID_SEED[source_type][1]


SCHEMA = """
-- ── per-service document tables (core cols first, then service-specific) ──
-- `ts` IS Slack's message id, and it is STORED rather than derived per request. Deriving it from
-- `(created_ts, thread-root key)` collides outright: every message of a thread hashes the same root
-- key into the same micro-fraction, so two replies in one second share a ts and one is reachable
-- only at the other's. Assigned once at import and probed within its channel, it cannot.
--
-- Storing it is also what lets `thread_ts` below hold the root's own `ts` (Slack's own name for
-- that field), so a reply reports its thread by reading one stored value instead of two
-- derivations having to agree.
-- A root carries its own ts here (Slack does too, so `thread_ts == ts` marks the root); a
-- standalone message carries NULL.
CREATE TABLE IF NOT EXISTS slack_messages (
    channel TEXT NOT NULL, ts TEXT NOT NULL, author_email TEXT NOT NULL,
    content TEXT NOT NULL,
    thread_ts TEXT, thread_seq INTEGER NOT NULL DEFAULT 0, subtype TEXT,
    reactions TEXT, files TEXT, edited TEXT, created_ts INTEGER NOT NULL, participants TEXT,
    PRIMARY KEY (channel, ts)
);
-- No `idx_slack_channel`: the PRIMARY KEY's own implicit index leads with `channel`, so a
-- per-channel scan already seeks. It was dropped rather than kept as a narrower duplicate.
DROP INDEX IF EXISTS idx_slack_channel;
-- A thread is read within its channel, so the thread index carries it — `thread_id` alone was
-- enough only while a doc_id was corpus-wide unique.
CREATE INDEX IF NOT EXISTS idx_slack_thread ON slack_messages(channel, thread_ts, thread_seq);
-- conversations.replies resolves a ts by (channel, created_ts); the composite index turns that from
-- a per-channel row scan (~340k rows in a big channel) into a direct lookup.
CREATE INDEX IF NOT EXISTS idx_slack_channel_ts ON slack_messages(channel, created_ts);
-- conversations.members pages a channel's distinct speakers; without this the DISTINCT
-- is a per-channel row scan (768k rows in the biggest channel) on every request.
CREATE INDEX IF NOT EXISTS idx_slack_channel_author ON slack_messages(channel, author_email);

-- `id` is the id the API reports, assigned at import (see backlot.importer.byo) rather than hashed
-- at serve time, so a get-by-id is a PRIMARY KEY lookup. Unlike confluence it is the raw seed,
-- never probed: `synth.gmail_message_id` draws from
-- 2**63, so a collision is vanishingly unlikely, and as the PRIMARY KEY one fails the import
-- loudly rather than silently replacing the earlier message.
--
-- `thread_id` is another message's `id` (the thread root's), not a dataset identifier: it is
-- resolved at import along with every other cross-row reference.
CREATE TABLE IF NOT EXISTS gmail_messages (
    id TEXT PRIMARY KEY, mailbox TEXT NOT NULL, author_email TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_gmail_thread ON gmail_messages(thread_id, thread_seq);
-- Superseded by the PRIMARY KEY, which IS this column now. Dropped EXPLICITLY: `CREATE INDEX IF
-- NOT EXISTS` matches on NAME, so it would never be removed from an already-built DB otherwise.
DROP INDEX IF EXISTS idx_gmail_served;

-- `id` is what `files.get`/`.export`/`.permissions` resolve and every listing/`_drive_facts`
-- emits: assigned at import (see backlot.importer.byo), never the corpus's own
-- identifier served straight through -- Drive was the one document source with no served-id column
-- at all, so a client kept seeing the dataset's own identifier scheme instead of an opaque
-- Drive-shaped id. No probe, like gmail/notion/linear: `synth.gdrive_file_id` draws from a 192-bit
-- digest (see its own docstring), so a collision is vanishingly unlikely, and the PRIMARY KEY
-- turns one into a loud import failure instead of a silent shadow. A SEPARATE id space from the
-- folder id `synth.drive_folder_id` computes at serve time (every folder id starts "0A", every one
-- of these starts "1"), so the two can never collide.
CREATE TABLE IF NOT EXISTS gdrive_files (
    id TEXT PRIMARY KEY, folder TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    subtype TEXT, mime_type TEXT, parents TEXT, created_ts INTEGER NOT NULL, updated_ts INTEGER,
    trashed INTEGER, owner_display TEXT
);
CREATE INDEX IF NOT EXISTS idx_gdrive_folder ON gdrive_files(folder);
DROP INDEX IF EXISTS idx_gdrive_served;

-- `path` names the file THIS row is (only kind='file' rows have one). `changed_paths` is the other
-- direction: a JSON list of the paths a PULL touched, so a corpus can state which files a pull
-- changed instead of leaving the router to pick deterministically. See backlot.routers.github's
-- changeset note. Comments stay OUTSIDE the parens: SQLite persists the statement verbatim, and a
-- trailing in-body comment makes a later `ALTER TABLE ... DROP COLUMN` fail to re-parse it.
-- The number the API reports, assigned at import (see backlot.importer.byo) rather than derived
-- at serve time: `synth.github_number`'s 90,000-value-per-repo space collides by the birthday
-- bound long before a real repo runs out of issues/PRs, and resolving a collision by MOVING the
-- loser to a fresh number leaves a number free to change whenever `--append` changes the set --
-- renumbering a link a client had already saved. A stored PRIMARY KEY cannot move.
--
-- Keyed on (repo, number) because a GitHub number is unique only within its repo (see
-- store.ID_COLUMNS). Uniqueness is enforced by the assignment itself -- resolve_github_numbers'
-- two-phase pass, where corpus-provided numbers claim their spelling first, corpus-wide, before
-- anything probes -- so a genuine collision essentially never reaches the key; if one did, the
-- deferred pass's own UPDATE raises IntegrityError there.
--
-- EVERY row carries a number, including `kind='file'`. A file's identity in the GitHub API is
-- (repo, path), not a number, and its number is never served: `github_by_number`'s caller, the
-- issue search and every listing already exclude `kind='file'` (see routers.github). But this
-- table holds two resources with different natural keys, and only one of them can be the PRIMARY
-- KEY -- so files draw a number from the same per-repo space rather than keeping the NULL that
-- left them unaddressable once `doc_id` was gone. Provided numbers on file rows are still
-- ignored, so a corpus's real issue numbers keep winning the claim.
CREATE TABLE IF NOT EXISTS github_items (
    repo TEXT NOT NULL, number INTEGER NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    kind TEXT, state TEXT, labels TEXT, assignees TEXT,
    merged_at TEXT, head_ref TEXT, base_ref TEXT, head_repo TEXT, reviews TEXT, reactions TEXT,
    created_ts INTEGER NOT NULL, updated_ts INTEGER,
    closed_ts INTEGER, closed_by TEXT, merged_by TEXT, milestone TEXT, requested_reviewers TEXT,
    owner_display TEXT, path TEXT, changed_paths TEXT, ref TEXT,
    PRIMARY KEY (repo, number)
);
-- No `idx_github_repo`: the PRIMARY KEY leads with `repo`, so a per-repo scan already seeks.
DROP INDEX IF EXISTS idx_github_repo;
-- A file is addressed by (repo, path) even though it is keyed by (repo, number) — this is the
-- index that lookup rides, and it is what makes the number a purely internal handle for a file.
CREATE INDEX IF NOT EXISTS idx_github_repo_path ON github_items(repo, path);
-- Several rows MAY state one (repo, path): they are that file's snapshots, and each is a document
-- in its own right with its own ACL. What must be unique is (repo, path, snapshot) — otherwise two
-- documents are the same file at the same moment and no order makes one of them the served one.
--
-- COALESCE, not a plain (repo, path, ref): SQLite treats NULLs in a UNIQUE index as DISTINCT, so a
-- three-column index over a nullable `ref` enforces nothing for exactly the rows that omit it —
-- which is every row in a corpus that does not use refs. `created_ts` is the fallback because it is
-- NOT NULL on every row and is stable across re-imports (the importer fills it from the corpus, or
-- from synth.epoch of the dataset id), so it makes the key total without asking a corpus for a
-- field it may have no use for. The cost is that with `ref` omitted a file's identity is
-- time-dependent: re-importing the same content under a NEW `created` adds a snapshot rather than
-- replacing one, which is the same "no update path" the stated ids already have.
CREATE UNIQUE INDEX IF NOT EXISTS idx_github_file_snapshot
    ON github_items(repo, path, COALESCE(ref, created_ts)) WHERE kind = 'file';
-- Superseded: the assignment pass reads (repo, number, kind), which the PRIMARY KEY's own index
-- covers apart from `kind`. Dropped EXPLICITLY, since `IF NOT EXISTS` matches only on name.
DROP INDEX IF EXISTS idx_github_doc_number;
DROP INDEX IF EXISTS idx_github_served;

-- The key the API reports, whole, and the row's PRIMARY KEY. Assigned at import (see
-- backlot.importer.byo's resolve_jira_keys) rather than composed at serve time from a stored
-- suffix plus the project's prefix: that round trip is what let routers.atlassian's
-- `_jira_container_for_key` -- whose three-way tolerance is a deliberate affordance for the JQL
-- project TOKEN -- leak into the ISSUE-KEY namespace twice (`payments-7` resolving, and
-- case-insensitivity), each time needing its own guard. `WHERE key = ?` has no seam for either to
-- enter.
--
-- The key is GLOBAL, not per project -- a real Jira key is unique across a site. That also closes
-- a residual a per-project index could not: a project with no provided keys is never registered
-- in `jira_prefix_holders`, so its SYNTHESIZED prefix could equal another project's provided one
-- (or another keyless project's own), and two documents in different projects would then serve the
-- identical key while a (project, suffix) index stayed perfectly satisfied. Now that is an import
-- error, which is what it always should have been.
--
-- `parent_id` holds the PARENT'S KEY, the same value this table is keyed on -- a subtask points at
-- a served id, never at a dataset identifier. It keeps the generic name because
-- :func:`children` reads it uniformly across jira, confluence and notion.
CREATE TABLE IF NOT EXISTS jira_issues (
    key TEXT PRIMARY KEY, project TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    status TEXT, issuetype TEXT, priority TEXT, labels TEXT, components TEXT,
    issuelinks TEXT, parent_id TEXT, changelog TEXT, created_ts INTEGER NOT NULL, updated_ts INTEGER,
    assignee_email TEXT, reporter_email TEXT, resolution TEXT, resolution_ts INTEGER,
    duedate TEXT, fix_versions TEXT, owner_display TEXT
);
CREATE INDEX IF NOT EXISTS idx_jira_project ON jira_issues(project);
CREATE INDEX IF NOT EXISTS idx_jira_parent ON jira_issues(parent_id);
-- Both superseded by the PRIMARY KEY. Dropped EXPLICITLY: `IF NOT EXISTS` matches on NAME, so
-- neither would ever be removed from an already-built DB.
DROP INDEX IF EXISTS idx_jira_doc_key;
DROP INDEX IF EXISTS idx_jira_served;

-- `id` is what the API reports, assigned at import (see backlot.importer.byo) rather than hashed
-- at serve time: a hash into `synth.confluence_id`'s 9,000,000 values collides by the birthday
-- bound, and a collision resolved last-writer-wins makes a page unreachable by its own id.
-- Uniqueness is enforced by the assignment itself --
-- _assign_confluence_id's in-run memo plus seed_tracker_ids' cross-run preload, both probed
-- against every id already taken -- so a genuine collision essentially never reaches the PRIMARY
-- KEY; if one did, the insert raises there rather than resolving silently.
-- `parent_id` holds the parent page's `id`, the same served value.
--
-- `INT`, not `INTEGER`, and that spelling is load-bearing: a column declared exactly `INTEGER`
-- and made the PRIMARY KEY becomes an alias for the rowid, and SQLite then AUTO-ASSIGNS a value
-- when one is inserted as NULL -- silently inventing an id for a page whose assignment pass
-- failed to give it one, which is precisely the quiet-shadow failure this whole change exists to
-- remove. `INT` has identical integer affinity (`typeof()` still reports "integer") but is not a
-- rowid alias, so a NULL raises NOT NULL instead.
CREATE TABLE IF NOT EXISTS confluence_pages (
    id INT NOT NULL, space TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    subtype TEXT, parent_id INT, labels TEXT, created_ts INTEGER NOT NULL, updated_ts INTEGER,
    version_number INTEGER, version_message TEXT, minor_edit INTEGER,
    owner_display TEXT,
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_confluence_space ON confluence_pages(space);
CREATE INDEX IF NOT EXISTS idx_confluence_parent ON confluence_pages(parent_id);
DROP INDEX IF EXISTS idx_confluence_served;

-- ── per-service comment tables (only services whose API exposes comments) ──
-- The parent reference is the ISSUE'S KEY -- the served id its own table is keyed on -- and it is
-- spelled the way that table spells it. A child row points at a served id; naming the column
-- after the parent's own is what keeps a comment and its issue in one namespace rather than two.
CREATE TABLE IF NOT EXISTS jira_comments (
    id TEXT PRIMARY KEY, key TEXT NOT NULL, seq INTEGER NOT NULL,
    author_email TEXT, body TEXT NOT NULL, created_ts INTEGER NOT NULL, reactions TEXT
);
CREATE INDEX IF NOT EXISTS idx_jira_comments_doc ON jira_comments(key);

CREATE TABLE IF NOT EXISTS confluence_comments (
    id TEXT PRIMARY KEY, page_id INT NOT NULL, seq INTEGER NOT NULL,
    author_email TEXT, body TEXT NOT NULL, created_ts INTEGER NOT NULL, reactions TEXT
);
CREATE INDEX IF NOT EXISTS idx_confluence_comments_doc ON confluence_comments(page_id);

-- Two resources real GitHub keeps apart, discriminated by `path`: a row WITH one is a
-- line-anchored review comment (/pulls/{n}/comments), one without is a conversation comment
-- (/issues/{n}/comments). `line` may be NULL for a file-level review comment, as on real GitHub;
-- `diff_hunk` is optional and derived from the file's snapshot when the corpus omits it.
-- `id` is the id the API reports, assigned at import (see backlot.importer.byo) rather than hashed
-- at serve time: a comment's own `url` resolves through it, and a hash into any fixed range
-- collides by the birthday bound long before a real corpus runs out of comments — two comments
-- sharing one id means one comment's url returns the other's body.
--
-- ONE column, not the corpus's own comment identifier plus a served one. The pair existed so
-- the assignment could key its memo on the value the corpus wrote; that value is an INPUT — it
-- seeds `synth.github_comment_id` and is then discarded — so keeping a column for it stored the
-- dataset's identifier scheme for no one to read. Uniqueness is enforced by the assignment itself
-- (_assign_github_comment_id's in-run taken-set, probed against every id already taken), so a
-- genuine collision essentially never reaches the PRIMARY KEY; if one did, the insert raises there
-- rather than resolving silently.
CREATE TABLE IF NOT EXISTS github_comments (
    id INT NOT NULL, repo TEXT NOT NULL, number INTEGER NOT NULL, seq INTEGER NOT NULL,
    author_email TEXT, body TEXT NOT NULL, created_ts INTEGER NOT NULL, reactions TEXT,
    path TEXT, line INTEGER, diff_hunk TEXT,
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_github_comments_doc ON github_comments(repo, number, seq);
DROP INDEX IF EXISTS idx_github_comments_served;

CREATE TABLE IF NOT EXISTS notion_comments (
    id TEXT PRIMARY KEY, page_id TEXT NOT NULL, seq INTEGER NOT NULL,
    author_email TEXT, body TEXT NOT NULL, created_ts INTEGER NOT NULL, reactions TEXT
);
CREATE INDEX IF NOT EXISTS idx_notion_comments_doc ON notion_comments(page_id);

-- ── Notion: pages + databases share one table (subtype), rows are pages parented to a database ──
-- `id` is the page/database id (synth.notion_id) the API reports, assigned at import (see
-- backlot.importer.byo) rather than derived by hashing at serve time. `data_source_id` is the
-- 2025-09-03 API's data-source (query target) id for a DATABASE (synth.notion_data_source_id) --
-- real Notion has no such id for a page, so it is populated only when subtype='database' and
-- stays NULL elsewhere, which a UNIQUE index treats as no claim rather than a collision (SQLite
-- allows any number of NULLs under UNIQUE). Neither is probed the way confluence's is: both draw
-- from synth._uuid_from's full digest space, not a bounded range, so a collision is vanishingly
-- unlikely -- and one raises on the PRIMARY KEY (or on idx_notion_ds) rather than silently
-- replacing the row that held the value.
-- `parent_id` holds the parent page's or database's `id`, the same served value.
CREATE TABLE IF NOT EXISTS notion_pages (
    id TEXT PRIMARY KEY, teamspace TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    subtype TEXT, parent_id TEXT, properties TEXT, icon TEXT, cover TEXT,
    created_ts INTEGER NOT NULL, updated_ts INTEGER,
    data_source_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_notion_teamspace ON notion_pages(teamspace);
CREATE INDEX IF NOT EXISTS idx_notion_parent ON notion_pages(parent_id);
DROP INDEX IF EXISTS idx_notion_served;
CREATE UNIQUE INDEX IF NOT EXISTS idx_notion_ds ON notion_pages(data_source_id);
DROP INDEX IF EXISTS idx_notion_served_ds;

-- ── S3: objects live in buckets (flat key namespace); no comments ──
-- (bucket, key) IS an object's address in S3, so it is the PRIMARY KEY. It was already the
-- only way in — `s3_by_bucket_key` is the one reader — but as a plain index nothing stopped two
-- rows from sharing an address, and which of them a caller got then depended on that caller's own
-- ACL grants (see s3_by_bucket_key's docstring for how far that went). A duplicate address is now
-- an import error. The pair is required rather than cautious: the same key in two buckets is
-- ordinary S3, so `key` alone could never have been unique.
CREATE TABLE IF NOT EXISTS s3_objects (
    bucket TEXT NOT NULL, key TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    subtype TEXT, content_type TEXT, size INTEGER,
    created_ts INTEGER NOT NULL, updated_ts INTEGER,
    PRIMARY KEY (bucket, key)
);
-- Both superseded by the PRIMARY KEY, whose implicit index IS (bucket, key) — the prefix range
-- scan and the per-bucket seek both ride it. Dropped EXPLICITLY, since `IF NOT EXISTS` matches
-- only on name.
DROP INDEX IF EXISTS idx_s3_bucket;
DROP INDEX IF EXISTS idx_s3_key;

-- ── HubSpot: ONE polymorphic table, because the CRM API is polymorphic ──
-- `{objectType}` is a path variable and custom object types exist, so a table per type would make
-- each new type a migration and break table()'s one-table-per-source contract. Typed properties live
-- in a JSON column because a search filter may name any property (-> json_extract).
-- `id` is what the API reports, assigned at import (see backlot.importer.byo) rather than hashed
-- at serve time: `synth.hubspot_record_id`'s space is 9,000,000,000 values -- wide enough to look
-- safe, but a corpus this project actually generates (500k documents) still collides ~16 times by
-- the birthday bound, and a collision resolved last-writer-wins makes a record unreachable at its
-- own id. Uniqueness is enforced by the assignment itself --
-- _assign_hubspot_id's in-run memo plus seed_tracker_ids' cross-run preload, both probed against
-- every id already taken, the same shape confluence's follows -- so a genuine collision
-- essentially never reaches the PRIMARY KEY; if one did, the insert raises there.
CREATE TABLE IF NOT EXISTS hubspot_objects (
    id TEXT PRIMARY KEY, object_type TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    properties TEXT, archived INTEGER, created_ts INTEGER NOT NULL, updated_ts INTEGER,
    owner_display TEXT
);
-- (object_type, id), not object_type alone: every read is "one type, ordered by id", so carrying
-- the ordering column makes a page a range seek instead of a temp-b-tree re-sort.
CREATE INDEX IF NOT EXISTS idx_hubspot_type_doc ON hubspot_objects(object_type, id);
DROP INDEX IF EXISTS idx_hubspot_served;

-- Associations are bidirectional in real HubSpot, with a distinct type id per direction, so a row
-- is stored per direction and a lookup stays a plain (from_id, to_type) index match. Both ends
-- name a record by its SERVED id, which is also what the v4 payload's `toObjectId` reports —
-- so that field is read straight off this row rather than re-derived from anything.
CREATE TABLE IF NOT EXISTS hubspot_associations (
    from_id TEXT NOT NULL, from_type TEXT NOT NULL,
    to_id TEXT NOT NULL, to_type TEXT NOT NULL,
    assoc_category TEXT, assoc_type_id INTEGER NOT NULL, label TEXT,
    PRIMARY KEY (from_id, to_id, assoc_type_id)
);
CREATE INDEX IF NOT EXISTS idx_hubspot_assoc_from ON hubspot_associations(from_id, to_type);

-- ── Linear: issues + their comments. Columns keep LINEAR's vocabulary, not Jira's (`state` not
-- status, `estimate` not story points, `branch_name`), so the payload cannot drift toward the wrong
-- vendor's model. `priority` is Linear's own 0-4 integer (0 none, 1 urgent … 4 low), not the corpus's
-- "P1"; `priorityLabel` is derived from it at serve time.
CREATE TABLE IF NOT EXISTS linear_issues (
    id TEXT PRIMARY KEY, team TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    identifier TEXT, state TEXT, priority INTEGER, estimate INTEGER, labels TEXT,
    project TEXT, cycle TEXT, branch_name TEXT, due_date TEXT,
    created_ts INTEGER NOT NULL, updated_ts INTEGER,
    archived_ts INTEGER, auto_archived_ts INTEGER, auto_closed_ts INTEGER,
    canceled_ts INTEGER, completed_ts INTEGER, started_ts INTEGER,
    assignee_email TEXT, assignee_display TEXT, owner_display TEXT,
    -- The parent's identifier as the corpus wrote it, plus the issue `id` it RESOLVED to at import.
    -- Both, because identifiers are NOT required to be unique (measured: one key is the identifier
    -- of 107 issues), so a serve-time join on `identifier` would invent edges. Resolving once —
    -- first match by `id`, the rule linear_issue_by_identifier applies — makes Issue.parent and
    -- Issue.children exact inverses.
    parent_key TEXT, parent_id TEXT,
    -- Release name as the corpus writes it (`runtime-1.19`); served as `Issue.releases`.
    release TEXT
);
-- (team, id): the Relay connection pages one team ordered by id, so carrying the ordering
-- column makes a page a range seek rather than a re-sort of the whole team.
CREATE INDEX IF NOT EXISTS idx_linear_team_doc ON linear_issues(team, id);
-- The ORDER BY is always TOTAL (sort key + id), so an index on the sort key alone does not
-- satisfy it — SQLite falls back to a temp b-tree over the whole table for every page. These carry
-- the tiebreak, so the ORDER BY is read straight off the index.
CREATE INDEX IF NOT EXISTS idx_linear_created_doc ON linear_issues(created_ts, id);
CREATE INDEX IF NOT EXISTS idx_linear_team_created ON linear_issues(team, created_ts, id);
-- `orderBy: updatedAt` sorts on the same COALESCE the field is served with, so the index has to
-- be on the expression, not the bare column.
CREATE INDEX IF NOT EXISTS idx_linear_updated_doc
    ON linear_issues(COALESCE(updated_ts, created_ts), id);
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
-- `Issue.children` is "every issue whose parent_id is me" — an indexed equality, not a join
-- on the non-unique identifier text.
CREATE INDEX IF NOT EXISTS idx_linear_parent_doc ON linear_issues(parent_id);
CREATE INDEX IF NOT EXISTS idx_linear_release ON linear_issues(release);
-- `issue(id: "ENG-123")` resolves an identifier straight to its row; identifiers are NOT unique
-- (5,055 of them repeat in one real corpus), so this is a lookup index, never a unique constraint.
CREATE INDEX IF NOT EXISTS idx_linear_identifier ON linear_issues(identifier);
-- COVERING index for the importer's parent-resolution pass (backlot.importer.byo, `parent_key` ->
-- `parent_id`), which reads (id, identifier) for every issue that provides one. Without it each
-- wide row is fetched from a scattered page and the scan dominates that pass; as an index-only
-- scan it is negligible.
CREATE INDEX IF NOT EXISTS idx_linear_doc_ident ON linear_issues(id, identifier);
-- `id` is the UUID the API reports (`synth.linear_id`), assigned at import (see
-- backlot.importer.byo) rather than hashed at serve time, so a get-by-id is a PRIMARY KEY lookup.
-- Kept distinct from `identifier` on purpose:
-- identifiers are NOT unique (see idx_linear_identifier above), so they stay a lookup index, never
-- a candidate for this column or this constraint (see ID_SEED's comment on why `identifier` is
-- excluded). No probe, like gmail's: `synth.linear_id` draws from `_uuid_from`'s full digest
-- space, not a bounded range, so a collision is vanishingly unlikely -- and one raises on the
-- PRIMARY KEY rather than silently replacing the issue already holding that value.
DROP INDEX IF EXISTS idx_linear_served;

CREATE TABLE IF NOT EXISTS linear_comments (
    id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, seq INTEGER NOT NULL,
    author_email TEXT, body TEXT NOT NULL, created_ts INTEGER NOT NULL, reactions TEXT
);
CREATE INDEX IF NOT EXISTS idx_linear_comments_doc ON linear_comments(issue_id, seq);
-- `Query.comments` pages the whole corpus ordered by time; without this the ORDER BY
-- re-sorts every comment in a temp b-tree on every page (165k of them at the scale measured).
CREATE INDEX IF NOT EXISTS idx_linear_comments_ts ON linear_comments(created_ts, id);

-- Linear's IssueRelation, `type` in (blocks | duplicate | related). ONE row per relation, not per
-- direction: Issue.relations and Issue.inverseRelations are the two ends of the same row.
-- `to_id` is resolved at import, so a dangling key never becomes a relation. Both ends name an
-- issue by its served `id`.
CREATE TABLE IF NOT EXISTS linear_relations (
    id TEXT PRIMARY KEY, from_id TEXT NOT NULL, to_id TEXT NOT NULL,
    type TEXT NOT NULL, created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_linear_rel_from ON linear_relations(from_id);
CREATE INDEX IF NOT EXISTS idx_linear_rel_to ON linear_relations(to_id);

-- Linear's model for any external link on an issue (a corpus's `links` and `attachments` alike).
-- `title` is non-null in Linear, so a bare URL gets one derived from its last path segment.
CREATE TABLE IF NOT EXISTS linear_attachments (
    id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, seq INTEGER NOT NULL,
    title TEXT NOT NULL, url TEXT NOT NULL, subtitle TEXT, source_type TEXT,
    created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_linear_attach_doc ON linear_attachments(issue_id, seq);

-- One root document per meeting plus its ordered sentences below. `content` is the sentences
-- concatenated (synth.fireflies_transcript_text) so search and any RAG consumer see one document; it
-- is an EXACT inverse of fireflies_sentences, not a second copy that can drift. `author_email` is the
-- HOST (the API's `host_email`); `organizer_email` is separate because the real API exposes both and
-- they legitimately differ, and is NULL when they coincide.
-- `id` is the API-facing transcript id: `synth.fireflies_id` seeded on the incoming record's own
-- identifier when the corpus is silent, but a BYO record may still provide its own value
-- (its schema-declared `transcript_id`) -- `transcript(id:)` looks a meeting up by it, so a
-- duplicate (whichever source it came from) would leave one transcript unreachable at its own id.
-- As the PRIMARY KEY it fails the import loudly instead, and it cannot be NULL: nothing else on
-- the row identifies a transcript.
-- The corpus's own meeting id is kept as `calendar_id`, where a real transcript carries it.
CREATE TABLE IF NOT EXISTS fireflies_transcripts (
    id TEXT PRIMARY KEY, channel TEXT NOT NULL, author_email TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL,
    calendar_id TEXT, calendar_type TEXT,
    organizer_email TEXT, duration REAL,
    created_ts INTEGER NOT NULL,
    -- JSON: the API's nested objects, stored whole because that is the shape served.
    summary TEXT, analytics TEXT, participants TEXT, meeting_attendees TEXT,
    audio_url TEXT, video_url TEXT, transcript_url TEXT, meeting_link TEXT,
    owner_display TEXT
);
CREATE INDEX IF NOT EXISTS idx_fireflies_channel ON fireflies_transcripts(channel);
-- `transcripts(fromDate:/toDate:)` is a date range and the default order is newest-first, so the
-- ordering column carries its `id` tiebreak (same lesson as idx_linear_created_doc).
CREATE INDEX IF NOT EXISTS idx_fireflies_created_doc
    ON fireflies_transcripts(created_ts, id);
CREATE INDEX IF NOT EXISTS idx_fireflies_channel_created
    ON fireflies_transcripts(channel, created_ts, id);
-- Superseded by the PRIMARY KEY, which IS the transcript id now.
DROP INDEX IF EXISTS idx_fireflies_transcript_id;
-- `transcripts(host_email:)` / `organizers:` filter on these directly.
CREATE INDEX IF NOT EXISTS idx_fireflies_host ON fireflies_transcripts(author_email);

-- The transcript's sentences. Carries the shared child-row contract that doc_comments reads, so it
-- fits the COMMENT_TABLE slot, plus the per-sentence fields the API serves. `body` IS the sentence
-- text; `author_email` is the speaker resolved to an identity, NULL for an anonymous label
-- ("Speaker 3") which both the corpus and the real API leave unattributed.
CREATE TABLE IF NOT EXISTS fireflies_sentences (
    id TEXT PRIMARY KEY, transcript_id TEXT NOT NULL, seq INTEGER NOT NULL,
    author_email TEXT, body TEXT NOT NULL, created_ts INTEGER NOT NULL, reactions TEXT,
    speaker_name TEXT, speaker_id INTEGER, start_time REAL, end_time REAL
);
CREATE INDEX IF NOT EXISTS idx_fireflies_sentences_doc ON fireflies_sentences(transcript_id, seq);

-- ── shared relationship tables (keyed by names — ACL grants live in the per-source tables
-- ── appended below instead, since a served id is unique only within a source) ──
-- ── per-service grouping tables (name of the grouping unit + its owning ACL group) ──
CREATE TABLE IF NOT EXISTS slack_channels    (channel TEXT PRIMARY KEY, group_id TEXT);
CREATE TABLE IF NOT EXISTS gmail_mailboxes   (mailbox TEXT PRIMARY KEY, group_id TEXT);
CREATE TABLE IF NOT EXISTS gdrive_folders    (folder  TEXT PRIMARY KEY, group_id TEXT);
-- `default_branch`, `branches` and `tags` are the repo's own refs, stated by a
-- `subtype: "repo"` record. Container-level facts on the container's own row, for the same reason
-- `jira_projects.key` and `linear_teams.served_key` are: one row per repo, and no document owns
-- them. NULL is "the corpus did not say", which is not the same as an empty list — an unstated
-- branch set is inferred from the repo's pulls, a stated empty one is a repo with no branches.
CREATE TABLE IF NOT EXISTS github_repos (
    repo TEXT PRIMARY KEY, group_id TEXT,
    default_branch TEXT, branches TEXT, tags TEXT
);
-- `key` is the project's own key (`PAY`) -- the prefix every one of its issue keys carries, which
-- real Jira guarantees IS the project's key. A container-level fact, one row per project, so it
-- belongs on the container's own row for the same reason `linear_teams.served_key` does.
CREATE TABLE IF NOT EXISTS jira_projects (
    project TEXT PRIMARY KEY, key TEXT, group_id TEXT
);
-- A project key is unique across a Jira site, the same rule idx_jira_served enforces for the full
-- issue key. Two projects claiming one prefix is an import error, not a last-writer-wins race.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jira_projects_key ON jira_projects(key);
CREATE TABLE IF NOT EXISTS confluence_spaces (space   TEXT PRIMARY KEY, group_id TEXT);
CREATE TABLE IF NOT EXISTS notion_teamspaces (teamspace TEXT PRIMARY KEY, group_id TEXT);
CREATE TABLE IF NOT EXISTS s3_buckets        (bucket  TEXT PRIMARY KEY, group_id TEXT);
CREATE TABLE IF NOT EXISTS hubspot_object_types (object_type TEXT PRIMARY KEY, group_id TEXT);
-- `served_id` (`synth.linear_team_id`, a UUID) and `served_key` (`synth.linear_team_key`, "ENG")
-- are the OTHER two spellings real `team(id:)` accepts, alongside this table's own primary key
-- (the raw container name -- a Backlot affordance, see resolve_team's docstring). Both are written
-- unconditionally at import (backlot.importer.byo's write_containers). `served_key` carries NO
-- unique index:
-- `linear_team_key` is not injective -- two containers can reduce to one key -- so a lookup
-- breaks the tie by team NAME order instead (see linear_team_by_served_key).
CREATE TABLE IF NOT EXISTS linear_teams (
    team TEXT PRIMARY KEY, group_id TEXT, served_id TEXT, served_key TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_linear_teams_served ON linear_teams(served_id);
CREATE INDEX IF NOT EXISTS idx_linear_teams_served_key ON linear_teams(served_key);
CREATE TABLE IF NOT EXISTS fireflies_channels (channel TEXT PRIMARY KEY, group_id TEXT);

-- Linear's by-id roots (`project(id:)`, `workflowState(id:)`, `issueLabel(id:)`, `cycle(id:)`,
-- `user(id:)`, `release(id:)`) resolve an entity that has no table of its own: it exists only as a
-- column VALUE on some issue. `@linear/sdk` reaches them lazily (`await issue.state` fires a fresh
-- `workflowState(id:)`) and the ids are one-way hashes of a name, so serving one means reversing
-- the hash. This table holds that reversal, written once at import (backlot.importer.byo's
-- `write_linear_entities`) rather than rebuilt per boot.
--
-- ONE table, not six, because these are not six kinds of thing: each row is a distinct value of one
-- issue column, and they share a shape and a single access pattern (`kind` + id -> the value). The
-- code already dispatched on `kind` before the table existed -- see `_LINEAR_ENTITY_PREDICATES` and
-- `linear_entity_has_visible`, which decide VISIBILITY by the same key. Six near-identical DDL
-- blocks would be six places to drift apart, the argument the `<source>_acl` tables' generated DDL
-- already makes.
--
-- `team` is set only for the two entities Linear scopes to one (a workflow state and a cycle);
-- `display` only for a user, whose value is the (email, display name) pair the resolver serves.
-- Which columns make up a kind's value is declared in :data:`LINEAR_ENTITY_VALUE`, so the reader
-- returns exactly the shape the resolver expects rather than a row for it to unpack.
CREATE TABLE IF NOT EXISTS linear_entities (
    kind TEXT NOT NULL, served_id TEXT NOT NULL, team TEXT, name TEXT, display TEXT,
    PRIMARY KEY (kind, served_id)
);

CREATE TABLE IF NOT EXISTS principals (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, display_name TEXT, email TEXT
);

-- Fireflies' own per-user id (`synth.fireflies_user_id`, a one-way hash of the address) needs a
-- row to live on, but `principals` also holds org/group rows the id is meaningless for (only
-- `type = 'user'` rows have a Fireflies account -- see list_users), and vendor concerns stay off
-- the deliberately central roster. A dedicated table sidesteps both:
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

# One ACL table per source, appended rather than written out eleven times: they differ only in the
# name and in which columns identify the document, and a hand-written block per source is eleven
# places for them to drift apart.
#
# A grant names its document by that source's OWN served id (:data:`ID_COLUMNS`) -- the same
# columns, in the same order, that the document table is keyed on -- so an ACL row and the row it
# governs can never be addressed two different ways. For the three sources whose id is a pair that
# means a two-column grant, which is why the DDL is generated from the registry rather than
# assuming one `doc_id` column.
SCHEMA += "".join(
    f"\nCREATE TABLE IF NOT EXISTS {t} (\n"
    + "".join(f"    {c} {id_column_type(src, c)} NOT NULL,\n" for c in ID_COLUMNS[src])
    + "    principal_type TEXT NOT NULL,\n"
    "    principal_id TEXT NOT NULL REFERENCES principals(id),\n"
    f"    PRIMARY KEY ({', '.join(ID_COLUMNS[src])}, principal_type, principal_id)\n"
    ");\n"
    f"CREATE INDEX IF NOT EXISTS idx_{t}_pid ON {t}(principal_id);\n"
    for src, t in ACL_TABLE.items()
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
    """A build-time fact, or None when this import did not write that key."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
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
    cols: tuple[str, ...] | None = None,
) -> tuple[str, list]:
    """``cols`` names the columns in the CALLER'S table that hold the document whose ACL decides
    visibility. It defaults to the source's own :func:`id_columns` — the row is normally its own
    subject — and is passed explicitly only when the subject is a different row, as for a HubSpot
    association, where it is the *target* (``to_id``), since the target is the record whose
    existence the response would reveal. It is positional against ``ID_COLUMNS``, so the n-th
    column here is matched to the n-th column of the ACL table.

    ``source_type`` selects the ACL table. Per source because a served id is per source: one table
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
    key = id_columns(source_type)
    if cols is None:
        cols = key
    elif len(cols) != len(key):
        raise ValueError(
            f"_acl_clause: {source_type!r} is identified by {len(key)} column(s) {key} but "
            f"{len(cols)} were given ({cols}) — the two are matched positionally"
        )
    if tbl is None:
        tbl = table(source_type)
    elif tbl in SOURCE_TABLE.values():
        real_table = table(source_type)
        # Raised, not asserted: `python -O` drops an assert, and the two guards in this function
        # protect the same thing — an ACL clause scoped to the wrong source or the wrong columns.
        if tbl != real_table:
            raise ValueError(
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
    # shadow the outer table, turning `_acl.id = {tbl}.{col}` into a tautological
    # self-comparison that admits any row with ANY grant in this table.
    on = " AND ".join(f"_acl.{a} = {tbl}.{b}" for a, b in zip(key, cols))
    return (
        f" AND EXISTS (SELECT 1 FROM {acl_tbl} _acl WHERE {on} AND _acl.principal_id IN ({marks}))",
        ids,
    )


def _acl_join(source_type: str, acl_alias: str, doc_alias: str) -> str:
    """The ON clause tying a source's ACL table to its doc table — every identifier column, since
    a grant names its document by exactly the key the document is stored under."""
    return " AND ".join(f"{doc_alias}.{c} = {acl_alias}.{c}" for c in id_columns(source_type))


def _order_by(source_type: str, alias: str = "", *, desc: bool = False) -> str:
    """The source's ordering columns as an ORDER BY term — the stable total order every offset
    page needs. A PAIR has to name both parts, or the order is not total within a container."""
    p = f"{alias}." if alias else ""
    d = " DESC" if desc else ""
    return ", ".join(f"{p}{c}{d}" for c in order_columns(source_type))


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
    sql += clause + f" ORDER BY {_order_by(source_type)} LIMIT ? OFFSET ?"
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
    """One object by the (bucket, key) address real S3 itself keys on — which is also this table's
    PRIMARY KEY, so this is a point lookup on it. There is nothing to synthesize and no entry in
    `ID_SEED` for s3: the corpus states the address outright.

    Unique by CONSTRAINT, which matters: two rows sharing an address would be resolved by the ACL
    clause in the same query, so which object a caller got would depend on that caller's own
    grants. A duplicate address is an import error instead."""
    clause, cp = _acl_clause("s3", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM s3_objects WHERE bucket = ? AND key = ?{clause}", [bucket, key, *cp]
    ).fetchone()


def hubspot_by_id(conn, record_id, visible_ids=None, *, columns="*") -> sqlite3.Row | None:
    """One CRM record by the id the API reports — this table's PRIMARY KEY, so the lookup cannot be
    ambiguous.

    Unlike confluence's/notion's/gmail's, HubSpot's id is PROBED against a collision
    (``synth.hubspot_record_id``'s 9,000,000,000-value space still collides at the corpus sizes this
    project generates) -- which is why a plain equality lookup is correct here: the probe
    (``_assign_hubspot_id``) is what makes the id unique, and this reader trusts it.

    ``columns`` narrows the projection: a caller resolving an ``after`` cursor only needs to know
    the record exists, and pulling ``content`` (a note's whole body, the widest column on this
    table) for every paged listing/association request would dwarf the lookup it is resolving on
    the way to a query that is separately ACL-scoped anyway."""
    clause, cp = _acl_clause("hubspot", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT {columns} FROM hubspot_objects WHERE id = ?{clause}", [record_id, *cp]
    ).fetchone()


def list_hubspot_objects(
    conn,
    object_type,
    *,
    after_id=None,
    visible_ids=None,
    limit=100,
    archived=False,
    columns="*",
    prefilter=None,
) -> list[sqlite3.Row]:
    """One page of a CRM object type, keyset-paginated by ``id``.

    HubSpot's ``after`` cursor IS the record id this table is keyed on, so the bound is a keyset
    rather than an OFFSET and needs no translation on the way in. ``archived`` splits the two views
    the API exposes.

    ``object_type`` may be several spellings of ONE type — HubSpot answers a standard object under
    its singular as well as its plural, and a corpus states whichever it likes, so the listing
    covers every spelling rather than the one the caller happened to type.

    ``prefilter`` is a ``(sql_fragment, params)`` the caller has established as a *necessary*
    condition, so pushing it down can only remove rows that would have been rejected anyway.
    ``columns`` narrows the projection: search walks the whole object type to report an honest
    ``total``, and ``content`` (a note's body) dominates that scan if it is read needlessly."""
    types = [object_type] if isinstance(object_type, str) else list(object_type)
    sql = (
        f"SELECT {columns} FROM hubspot_objects WHERE object_type IN ({','.join('?' * len(types))})"
    )
    params: list = list(types)
    if prefilter:
        frag, fparams = prefilter
        sql += f" AND {frag}"
        params += fparams
    sql += " AND archived IS NOT NULL" if archived else " AND archived IS NULL"
    if after_id:
        sql += " AND id > ?"
        params.append(after_id)
    clause, cparams = _acl_clause("hubspot", visible_ids=visible_ids)
    sql += clause + " ORDER BY id LIMIT ?"
    params += cparams + [limit]
    return conn.execute(sql, params).fetchall()


# --- Linear: issues, their comments, and the identifier lookup ---------------------
# Linear pages a Relay connection, and Backlot's `after` is the same opaque offset cursor every
# other source's page token is (see backlot/pagination.py), so these take an offset. The ORDER BY is
# always total — the sort column plus `id` as the tiebreak — because an offset page over a
# non-total order can silently repeat or skip a row between pages.

# GraphQL `orderBy` value -> the column it sorts on.
#
# Linear's pagination docs state "By default results are ordered by createdAt field", and its
# `PaginationOrderBy` enum carries a FIELD ONLY — no direction — so the server fixes the
# direction and a client that wants the other one uses the richer `sort:` input instead.
# The direction is not documented; ASCENDING is the choice here because it is the only one that
# makes an `after` cursor stable: with newest-first, creating an issue shifts every existing
# offset by one and a mid-crawl cursor silently re-reads a row. `id` breaks ties into a
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
    """The ORDER BY, always TOTAL (sort keys + ``id``) — an offset page over a non-total order
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
        return ", ".join(terms) + ", id"
    # An ABSENT orderBy is not "unordered": Linear documents createdAt as the default, so falling
    # through to raw insertion order (`id`) was a real divergence — `issues(first: 10)`
    # returned an arbitrary ten rather than the first ten by creation.
    col = LINEAR_ORDER_COLUMNS[order_by or LINEAR_DEFAULT_ORDER_BY]
    direction = "DESC" if descending else "ASC"
    # NULL updated_ts sorts last on DESC, which is where an issue with no recorded edit belongs.
    return f"{col} {direction}, id"


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


def linear_by_id(conn, issue_id, visible_ids=None) -> sqlite3.Row | None:
    """One issue by the UUID the API reports (``synth.linear_id``), distinct from ``identifier``
    (see ``linear_issue_by_identifier``) -- this table's PRIMARY KEY, so a point lookup."""
    clause, cp = _acl_clause("linear", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM linear_issues WHERE id = ?{clause}", [issue_id, *cp]
    ).fetchone()


def linear_issue_by_identifier(conn, identifier, visible_ids=None) -> sqlite3.Row | None:
    """Resolve a human identifier (``ENG-123``) to its issue. Identifiers are not unique (5,055
    repeat in one real corpus), so this deliberately returns the first by ``id`` rather than pretending
    the lookup is unambiguous — the UUID form of ``issue(id:)`` is the exact one."""
    sql = "SELECT * FROM linear_issues WHERE identifier = ?"
    params: list = [identifier]
    clause, cparams = _acl_clause("linear", visible_ids=visible_ids)
    return conn.execute(sql + clause + " ORDER BY id LIMIT 1", params + cparams).fetchone()


def list_linear_comments(
    conn, *, issue_id=None, visible_ids=None, limit=50, offset=0, prefilter=None
) -> list[sqlite3.Row]:
    """Comments on one issue, or across the corpus when ``issue_id`` is None (``Query.comments``).

    A comment row carries no ACL grant of its own; visibility is the parent issue's, so the ACL
    is applied to ``linear_issues`` through a join rather than to the comment table."""
    # The join exists ONLY to reach the parent issue's ACL, so an admin read (visible_ids None)
    # skips it: measured over 165k comments, the join cost ~40ms per page for nothing.
    join = "" if visible_ids is None else " JOIN linear_issues i ON i.id = c.issue_id"
    sql = f"SELECT c.* FROM linear_comments c{join} WHERE 1=1"
    params: list = []
    if issue_id is not None:
        sql += " AND c.issue_id = ?"
        params.append(issue_id)
    if prefilter:
        frag, fparams = prefilter
        sql += f" AND {frag}"
        params += fparams
    clause, cparams = _acl_clause("linear", "i", visible_ids)
    sql += clause + " ORDER BY c.created_ts, c.id LIMIT ? OFFSET ?"
    return conn.execute(sql, params + cparams + [limit, offset]).fetchall()


def count_linear_comments(conn, *, issue_id=None, visible_ids=None, prefilter=None) -> int:
    join = "" if visible_ids is None else " JOIN linear_issues i ON i.id = c.issue_id"
    sql = f"SELECT COUNT(*) FROM linear_comments c{join} WHERE 1=1"
    params: list = []
    if issue_id is not None:
        sql += " AND c.issue_id = ?"
        params.append(issue_id)
    if prefilter:
        frag, fparams = prefilter
        sql += f" AND {frag}"
        params += fparams
    clause, cparams = _acl_clause("linear", "i", visible_ids)
    return conn.execute(sql + clause, params + cparams).fetchone()[0]


def linear_children(
    conn, parent_id, *, visible_ids=None, limit=50, offset=0, prefilter=None
) -> list[sqlite3.Row]:
    """Sub-issues of an issue — every row whose resolved ``parent_id`` is this one.

    An indexed equality on an issue id, NOT a join on ``identifier``: identifiers repeat, so a
    join would attach one issue's children to every issue sharing its key. Resolved once at import,
    which is what makes this the exact inverse of ``Issue.parent``."""
    sql = "SELECT * FROM linear_issues WHERE parent_id = ?"
    params: list = [parent_id]
    if prefilter:
        frag, fparams = prefilter
        sql += f" AND {frag}"
        params += fparams
    clause, cparams = _acl_clause("linear", visible_ids=visible_ids)
    sql += clause + " ORDER BY created_ts, id LIMIT ? OFFSET ?"
    return conn.execute(sql, params + cparams + [limit, offset]).fetchall()


def linear_relations(
    conn, issue_id, *, inverse=False, visible_ids=None, limit=50, offset=0
) -> list[sqlite3.Row]:
    """One page of an issue's relations: ``Issue.relations`` (rows it declared) or, with
    ``inverse``, ``Issue.inverseRelations`` (rows pointing at it) — two ends of one stored row.

    ACL-scoped on the OTHER end: a relation whose counterpart the caller cannot read is omitted
    entirely, since surfacing its id would disclose that issue."""
    mine, other = ("to_id", "from_id") if inverse else ("from_id", "to_id")
    clause, cparams = _acl_clause("linear", "i", visible_ids)
    sql = (
        f"SELECT r.* FROM linear_relations r JOIN linear_issues i ON i.id = r.{other} "
        f"WHERE r.{mine} = ?{clause} ORDER BY r.created_ts, r.id LIMIT ? OFFSET ?"
    )
    return conn.execute(sql, [issue_id, *cparams, limit, offset]).fetchall()


def linear_attachments(
    conn, issue_id, *, visible_ids=None, limit=50, offset=0, url=None, prefilter=None
) -> list[sqlite3.Row]:
    """An issue's attachments. Visibility is the parent issue's — an attachment carries no grant
    of its own — so the ACL is applied through a join, as it is for comments. ``url`` is Linear's
    own exact-match argument on this connection."""
    join = "" if visible_ids is None else " JOIN linear_issues i ON i.id = a.issue_id"
    sql = f"SELECT a.* FROM linear_attachments a{join} WHERE a.issue_id = ?"
    params: list = [issue_id]
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

    An attachment is only ever reached through its issue, so the id is matched by re-deriving it
    over visible rows — one on a hidden issue is simply not found."""
    from backlot import synth

    join = "" if visible_ids is None else " JOIN linear_issues i ON i.id = a.issue_id"
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
            f"JOIN linear_issues a ON a.id = r.from_id "
            f"JOIN linear_issues b ON b.id = r.to_id WHERE 1=1{clause_a}{clause_b}",
            [*pa, *pb],
        )
    for row in rows:
        if synth.linear_relation_id(row["id"]) == served_id:
            return row
    return None


def linear_distinct_values(conn) -> dict[str, list]:
    """The distinct entity names Linear's by-id roots have to resolve back to.

    ``@linear/sdk`` resolves relations lazily (``await issue.state`` fires a fresh
    ``workflowState(id:)``) and those uuids are one-way hashes of a name, so each entity needs a row
    to resolve through. Each entry is a DISTINCT over one column. Users come back as
    ``(email, display_name)`` so a user reached by id is named like one reached inline on an
    issue.
    """

    def col(name):
        return [
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT {name} FROM linear_issues WHERE {name} IS NOT NULL AND {name} != ''"
            )
        ]

    def per_team(name, default=None):
        # Workflow states and cycles are per-team entities in Linear, so they are keyed on the
        # (team, name) pair the id was derived from.
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
# linear_resolvers._state). It lives here because the entity table and the visibility probe must
# agree with the resolver on it, or an id the API served becomes unresolvable.
LINEAR_DEFAULT_STATE = "Todo"


# The by-id roots (`project(id:)`, `workflowState(id:)`, …) resolve an entity that has no table
# of its own: it exists only as a column value on some issue. So "can the caller see it" means
# "can the caller see any issue carrying it", and each kind names the predicate that asks.
# Keyed exactly as `linear_entities` keys its rows.
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


# kind -> the `linear_entities` columns that make up its value, in the order the resolver wants
# them. A one-column kind resolves to a bare value, a two-column kind to a tuple -- which is
# exactly what `_LINEAR_ENTITY_PREDICATES` below takes, so a value read out of the table can be
# handed straight to the visibility check.
LINEAR_ENTITY_VALUE = {
    "project": ("name",),
    "cycle": ("team", "name"),
    "state": ("team", "name"),
    "user": ("name", "display"),  # (email, display name)
    "label": ("name",),
    "release": ("name",),
}


def linear_entity_by_id(conn, kind: str, served_id):
    """The project / state / label / cycle / user / release a served id names, or None.

    A PRIMARY KEY lookup on `linear_entities`, written at import. Returns the value in the shape
    the resolver serves it in (see :data:`LINEAR_ENTITY_VALUE`): a bare name for the corpus-wide entities, a
    ``(team, name)`` pair for the two Linear scopes to a team, ``(email, display)`` for a user.

    No ``visible_ids``: whether the CALLER may see the entity is a different question, answered by
    :func:`linear_entity_has_visible` against the issues carrying it — an entity has no ACL of its
    own, so folding the two together would have to decide visibility from a row that holds no
    grant.
    """
    cols = LINEAR_ENTITY_VALUE.get(kind)
    if cols is None:
        raise ValueError(f"unknown linear entity kind {kind!r}")
    row = conn.execute(
        f"SELECT {', '.join(cols)} FROM linear_entities WHERE kind = ? AND served_id = ?",
        (kind, str(served_id)),
    ).fetchone()
    if row is None:
        return None
    return tuple(row) if len(cols) > 1 else row[0]


def linear_entity_has_visible(conn, kind: str, value, visible_ids=None) -> bool:
    """Whether the caller can see ANY issue carrying this project / cycle / state / person / label.

    Without it the by-id roots are an existence oracle: `linear_entities` is an unfiltered DISTINCT
    over every issue, so a caller denied an issue could still resolve that issue's project, label,
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
    conn, from_id, to_type, *, after_to_id=None, visible_ids=None, limit=500
) -> list[sqlite3.Row]:
    """One page of associations from a CRM record to records of ``to_type``, ACL-scoped on the
    target. Keyset-paginated by ``to_id`` for the same reason the listings are: the API's cursor is
    the last record id the caller saw, and a record past the first page must stay reachable.

    No join to ``hubspot_objects``: ``to_id`` IS the served id the v4 payload's ``toObjectId``
    reports, so it is read straight off this row. Recomputing it from a hash would not do —
    hubspot's id is PROBED, so the raw hash can disagree with what the target resolves at."""
    sql = "SELECT * FROM hubspot_associations WHERE from_id = ? AND to_type = ?"
    params: list = [from_id, to_type]
    if after_to_id:
        sql += " AND to_id > ?"
        params.append(after_to_id)
    clause, cparams = _acl_clause("hubspot", "hubspot_associations", visible_ids, cols=("to_id",))
    sql += clause + " ORDER BY to_id LIMIT ?"
    params += cparams + [limit]
    return conn.execute(sql, params).fetchall()


def list_drive_folder(conn, folder, visible_ids=None, limit=100, offset=0) -> list[sqlite3.Row]:
    """Non-trashed files directly in a Drive folder — SQL-scoped + SQL-paginated, so listing a
    big folder costs one page of rows per request, not a full-corpus scan on every page."""
    sql = "SELECT * FROM gdrive_files WHERE folder = ? AND COALESCE(trashed, 0) = 0"
    params: list = [folder]
    clause, cparams = _acl_clause("google_drive", visible_ids=visible_ids)
    # No ORDER BY: the folder index already yields a stable order for pagination, and adding
    # ORDER BY id forces a per-page sort of the whole folder (≈30x slower on a big folder).
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


def gdrive_by_id(conn, file_id, visible_ids=None) -> sqlite3.Row | None:
    """One Drive file by the id `files.get` / `.export` / `.permissions` resolve --
    the model is gmail_by_id/notion_by_id: this table's PRIMARY KEY, assigned at import and read
    straight through the ACL clause."""
    clause, cp = _acl_clause("google_drive", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM gdrive_files WHERE id = ?{clause}", [file_id, *cp]
    ).fetchone()


# A Gmail thread is listed once, under its root. ``thread_id`` holds the ROOT'S OWN served id (see
# the google router's ``_gmail_ids``), so a root is the row that is its own thread — or states no
# thread at all, which a lone message does.
_GMAIL_ROOT = " AND (thread_id IS NULL OR thread_id = id)"


def count_documents(
    conn,
    source_type,
    container=None,
    visible_ids=None,
    author_email=None,
    state=None,
    exclude_trashed=False,
    roots_only=False,
) -> int:
    # state: only valid for source_type="github" — it's the only items table with a `state`
    # column; passing it for any other source_type raises sqlite3.OperationalError. Likewise
    # exclude_trashed, which only gdrive_files has a column for, and roots_only, which counts
    # gmail THREADS rather than messages. It has to track list_documents': a count that includes
    # rows the listing drops makes nextPageToken lie.
    tbl = table(source_type)
    sql = f"SELECT COUNT(*) FROM {tbl} WHERE 1=1"
    params: list = []
    sql = _scope(sql, params, grouping_col(source_type), container, author_email)
    if state is not None:
        sql += " AND COALESCE(state, 'open') = ?"
        params.append(state)
    if exclude_trashed:
        sql += " AND COALESCE(trashed, 0) = 0"
    if roots_only:
        sql += _GMAIL_ROOT
    clause, cparams = _acl_clause(source_type, visible_ids=visible_ids)
    sql += clause
    params += cparams
    return conn.execute(sql, params).fetchone()[0]


def get_document(conn, source_type, *ident, visible_ids=None) -> sqlite3.Row | None:
    """One document by the id it is served under. ``ident`` is positional against the source's
    :func:`id_columns`, so it is one value for the nine sources keyed on a single column and two
    for slack (channel, ts), s3 (bucket, key) and github (repo, number). A wrong arity raises
    rather than silently matching on a prefix of the key, which would return an arbitrary row of
    the right container."""
    key = id_columns(source_type)
    if len(ident) != len(key):
        raise ValueError(
            f"get_document: {source_type!r} is identified by {key}, so it takes "
            f"{len(key)} value(s), not {len(ident)}"
        )
    tbl = table(source_type)
    sql = f"SELECT * FROM {tbl} WHERE " + " AND ".join(f"{c} = ?" for c in key)
    params: list = list(ident)
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
    title=None,
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
    if title:
        # `transcripts(title:)` matches a case-insensitive SUBSTRING, which the live API
        # demonstrates: "tes" and "TEST" both return the meeting titled "test". Distinct from
        # `keyword`, which `scope` can point at sentences.
        #
        # LIKE and nothing around it, the same expression list_gdrive_files_by_name documents:
        # SQLite folds ASCII case here and SQLite's own `lower()` folds no more, so wrapping both
        # sides in it is a measured no-op. A cased non-ASCII letter therefore matches
        # case-sensitively; the SDL says so rather than leaving a client to find out. Folding it
        # too would mean a Python callback per row, which is 40% on the content scan `keyword`
        # runs, for one title in the 10,173-transcript bench corpus — and that one matches either
        # way.
        sql += " AND title LIKE ? ESCAPE '\\'"
        params.append(f"%{_like_escape(title)}%")
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
    title=None,
    from_ts=None,
    to_ts=None,
    keyword=None,
    scope=None,
    visible_ids=None,
    limit=50,
    offset=0,
) -> list[sqlite3.Row]:
    """One page of transcripts, newest first — the order the real API returns them in.

    The ORDER BY carries its ``id`` tiebreak so it is TOTAL, and the tiebreak runs DESC WITH the
    sort key rather than against it: either direction is valid for an arbitrary tiebreak, but a
    uniform one is a backwards index scan while a mixed one is a temp b-tree over the whole table.
    """
    where, params = _fireflies_where(
        channel=channel,
        host_email=host_email,
        organizers=organizers,
        participants=participants,
        title=title,
        from_ts=from_ts,
        to_ts=to_ts,
        keyword=keyword,
        scope=scope,
        visible_ids=visible_ids,
    )
    return conn.execute(
        f"SELECT * FROM fireflies_transcripts{where} ORDER BY created_ts DESC, id DESC "
        f"LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()


def count_fireflies_transcripts(conn, **kw) -> int:
    where, params = _fireflies_where(**kw)
    return conn.execute(f"SELECT COUNT(*) FROM fireflies_transcripts{where}", params).fetchone()[0]


def fireflies_transcript_by_id(conn, transcript_id, visible_ids=None) -> sqlite3.Row | None:
    """Resolve the API-facing transcript id to its row. Unlike Linear's identifier this IS
    unique — it is this table's PRIMARY KEY — so there is no first-match ambiguity."""
    sql = "SELECT * FROM fireflies_transcripts WHERE id = ?"
    clause, cparams = _acl_clause("fireflies", visible_ids=visible_ids)
    return conn.execute(sql + clause, [transcript_id] + cparams).fetchone()


def fireflies_speakers(conn, transcript_ids) -> dict[str, list[sqlite3.Row]]:
    """The numbered speakers of each transcript, as ``{transcript_id: [row, ...]}`` in number
    order — each row carrying ``speaker_id`` and ``speaker_name``.

    Fireflies numbers speakers WITHIN a meeting and reports that number on both `Sentence` and
    `AnalyticsSpeaker`, so the roster comes from the rows that already state it rather than from a
    position in the analytics JSON, which carries no number of its own.

    One entry per NUMBER, which is what Fireflies assigns: two runs diarization gave no label are
    two speakers, and keying the roster on the name instead would collapse them into one and leave
    `Sentence.speaker_id` naming a speaker the roster does not list. Where one number does carry
    several labels, the first the transcript uses is served — with exactly one ``min()`` aggregate,
    SQLite takes the bare ``speaker_name`` from the row that minimum came from.

    Batched over a page of ids: `Transcript.speakers` and `analytics.speakers` are resolved per
    transcript, so a per-transcript read is one statement per row of the page and two when both
    fields are selected. Every requested id gets an entry, so a transcript whose sentences carry no
    number reads as an empty roster rather than as a missing key.
    """
    ids = list(transcript_ids)
    out: dict[str, list[sqlite3.Row]] = {t: [] for t in ids}
    if not ids:
        return out
    marks = ", ".join("?" for _ in ids)
    # Seeks each transcript over idx_fireflies_sentences_doc rather than scanning the table.
    rows = conn.execute(
        "SELECT transcript_id, speaker_id, speaker_name, MIN(seq) AS first_seq "
        f"FROM fireflies_sentences WHERE transcript_id IN ({marks}) AND speaker_id IS NOT NULL "
        "GROUP BY transcript_id, speaker_id ORDER BY transcript_id, speaker_id",
        ids,
    )
    for r in rows:
        out[r["transcript_id"]].append(r)
    return out


def fireflies_channel_members(conn, channel, visible_ids=None) -> list[sqlite3.Row]:
    """A channel's roster: everyone who took part in a meeting in it the caller may read.

    Membership is the per-channel signal the corpus actually carries — the same choice
    slack_channel_member_emails documents. Answering with the channel's ACL group instead would
    give every channel sharing a group the same members, which the real API cannot produce.

    ACL'd from a different position than that function, though: a Slack channel's membership is
    org-visible by construction, while a fireflies transcript is granted per document and
    `transcripts` honours that. An aggregate over the channel's meetings therefore needs the same
    clause, or the roster names the participants of meetings the caller was denied — and, by
    carrying an address no visible meeting mentions, states that such a meeting exists."""
    sql = (
        "SELECT DISTINCT je.value AS email, p.display_name AS display_name "
        "FROM fireflies_transcripts t, json_each(t.participants) je "
        "LEFT JOIN principals p ON lower(p.email) = lower(je.value) "
        "WHERE t.channel = ?"
    )
    clause, cparams = _acl_clause("fireflies", "t", visible_ids=visible_ids)
    return conn.execute(sql + clause + " ORDER BY je.value", [channel] + cparams).fetchall()


def fireflies_sentences(conn, transcript_id) -> list[sqlite3.Row]:
    """A transcript's sentences in order. No ACL clause: the caller has already been cleared for
    the parent transcript, and a sentence is not independently addressable."""
    return conn.execute(
        "SELECT * FROM fireflies_sentences WHERE transcript_id = ? ORDER BY seq", (transcript_id,)
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


def _has_fts(conn, source_type: str) -> bool:
    """Whether THIS source's FTS index exists. Per-source rather than corpus-wide: one missing
    index now falls only its own source back to the LIKE path, instead of every source."""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (_fts_table(source_type),),
        ).fetchone()
        is not None
    )


# Each source's terms live in their OWN FTS index. The split exists for one substantive reason:
# bm25's IDF is computed over whatever table the term is in, so a shared index makes how a term
# ranks INSIDE one source depend on how often it appears in the others. Per-source indexes make
# each vendor's ranking a function of that vendor's corpus alone, which is what the real products
# do — measured, a two-term query reorders (see the commit that added `jira_fts`).
#
# It was deliberately NOT justified by scan cost. The shared `docs_fts` this replaced carried an
# INDEXED `src` tag, so a scoped search already intersected posting lists rather than ranking
# every source and post-filtering; that had been solved when the tag was added.
FTS_TABLE = {src: f"{src}_fts" for src in SOURCE_TABLE}


def _fts_table(source_type: str) -> str:
    """The FTS table a source's terms live in. Raises for an unknown source rather than defaulting,
    the same way ``table()`` does — a silent default here would search the wrong corpus."""
    try:
        return FTS_TABLE[source_type]
    except KeyError:
        raise ValueError(f"unknown source_type {source_type!r}")


# Sources with no `title` column. The FTS index keeps a `title` column for every source so its shape
# and the queries over it stay uniform; for these it is fed a constant.
TITLELESS = frozenset({"slack"})


def title_expr(source_type: str, alias: str = "") -> str:
    """SQL for a source's title, `''` where the table has no such column — for the LIKE fallback,
    whose one statement covers every source."""
    if source_type in TITLELESS:
        return "''"
    return f"{alias}.title" if alias else "title"


def _fts_text_columns(source_type: str) -> tuple[str, ...]:
    """The text columns a source's index holds — `content`, and `title` where the table has one."""
    return ("content",) if source_type in TITLELESS else ("title", "content")


def build_fts(conn) -> bool:
    """(Re)build every source's FTS index. No-op (False) without FTS5 — search then uses the LIKE
    fallback.

    One index per source, each holding only that source's rows: no `src` tag column, because there
    is nothing else in the table to tell apart and the tag's own token would contribute to every
    doc's length and bm25 score for no purpose."""
    if not _fts5_ok(conn):
        return False
    # porter stemming (over unicode61) so a search matches morphological variants the way real
    # Slack/Gmail search do — "deletion" finds "deletions", "embedding" finds "embeddings".
    #
    # Commit per source rather than once at the end: on an in-place rebuild of a large DB this
    # keeps each writer lock window to one source's index, so a concurrent reader (the live
    # server, with a busy_timeout) rides through instead of blocking on a single multi-GB commit.
    for src, tbl in SOURCE_TABLE.items():
        fts = _fts_table(src)
        # The index carries the source's OWN identifier columns, so the join back to the doc
        # table is on the same key that table is now stored under. They are UNINDEXED: FTS5 must
        # not tokenize an id, but it DOES preserve the value's type, so an integer github number
        # comes back an integer and matches its INTEGER column rather than the string '7'.
        key = ", ".join(id_columns(src))
        decl = ", ".join(f"{c} UNINDEXED" for c in id_columns(src))
        cols = ", ".join(_fts_text_columns(src))
        conn.execute(f"DROP TABLE IF EXISTS {fts}")
        conn.execute(
            f"CREATE VIRTUAL TABLE {fts} USING fts5({decl}, {cols}, tokenize='porter unicode61')"
        )
        conn.execute(f"INSERT INTO {fts}({key}, {cols}) SELECT {key}, {cols} FROM {tbl}")
        conn.commit()
    return True


def fts_add_docs(conn, source_type: str, doc_keys: list) -> int:
    """Incrementally (re)index specific docs in this source's FTS index — delete-then-insert per
    document so it is idempotent (an upsert). Used by append imports so a small add doesn't trigger
    a full rebuild. No-op (returns 0) if that index isn't present or ``doc_keys`` is empty.

    Each entry is one document's key, positional against :func:`id_columns`: a bare value for the
    nine single-column sources, a tuple for slack / s3 / github. The match is a row-value
    ``IN (VALUES ...)``, which is one lookup per document at either arity."""
    if not doc_keys or not _has_fts(conn, source_type):
        return 0
    tbl, fts = table(source_type), _fts_table(source_type)
    cols = id_columns(source_type)
    key = ", ".join(cols)
    n = 0
    # Chunked to stay under SQLite's variable limit — which counts VALUES, not rows, so the chunk
    # shrinks with the arity of the key rather than staying at a flat 900.
    per_chunk = max(1, 900 // len(cols))
    rows = [k if isinstance(k, tuple) else (k,) for k in doc_keys]
    for i in range(0, len(rows), per_chunk):
        chunk = rows[i : i + per_chunk]
        values = ",".join("(" + ",".join("?" for _ in cols) + ")" for _ in chunk)
        flat = [v for row in chunk for v in row]
        conn.execute(f"DELETE FROM {fts} WHERE ({key}) IN (VALUES {values})", flat)
        cols = ", ".join(_fts_text_columns(source_type))
        conn.execute(
            f"INSERT INTO {fts}({key}, {cols}) "
            f"SELECT {key}, {cols} FROM {tbl} WHERE ({key}) IN (VALUES {values})",
            flat,
        )
        n += len(chunk)
    conn.commit()
    return n


def _fts_join(source_type: str, alias: str) -> str:
    """The ON clause tying a source's FTS index back to its doc table — every identifier column,
    since the index carries the same key the table is stored under."""
    fts = _fts_table(source_type)
    return " AND ".join(f"{alias}.{c} = {fts}.{c}" for c in id_columns(source_type))


def _fts_match(query: str, phrase: bool = False) -> str:
    """A safe FTS5 MATCH string: alnum tokens, each quoted and ANDed. ``phrase=True`` requires the
    tokens ADJACENT, for grep-style callers whose pattern is a literal — an AND would bury the exact
    match under docs that merely contain all the words scattered.

    Takes no source: each source has its own index, so the caller picks the table (`_fts_table`) and
    there is nothing left for the match string to exclude."""
    toks = re.findall(r"\w+", (query or "").lower())
    if not toks:
        return ""
    return (
        ('"' + " ".join(toks) + '"')
        if (phrase and len(toks) > 1)
        else " AND ".join(f'"{t}"' for t in toks)
    )


def _fts_relevance_order(source_type: str, query, tbl: str = "t") -> tuple[str, list]:
    """Relevance ordering for an FTS query — bm25 rank, behind a literal-substring tier when the
    query joins word characters with punctuation.

    Boost docs containing the query as a literal substring, but ONLY for such a query (upload.csv,
    DOCS-210, a/b): that's exactly when the tokenizer splits one literal into pieces and the exact
    match sinks under coincidental "upload csv"/"upload-csv" hits. This surfaces it first whether
    the client quoted the query (mirage's grep push-down) or not (the MCP slack/gmail search sends
    bare terms). Plain multi-word queries ("the meeting") gain nothing from it and would pay a full
    instr scan over tens of thousands of matches, so the punctuation test gates them out. instr
    runs only over the already-matched rows, so it is cheap.

    Shared by :func:`search_documents` and :func:`search_repo_files`: the tier is a property of
    FTS5 tokenization, not of either caller's endpoint, so both orders have to agree on it.
    """
    fts = _fts_table(source_type)
    lit = (query or "").strip()
    if lit and re.search(r"\w[^\w\s]\w", lit):
        return (
            f"(instr(lower({tbl}.content), lower(?)) > 0 "
            f"OR instr(lower({title_expr(source_type, tbl)}), lower(?)) > 0) DESC, {fts}.rank",
            [lit, lit],
        )
    return f"{fts}.rank", []


def search_documents(
    conn,
    query,
    source_type,
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
    if _has_fts(conn, source_type):
        m = _fts_match(query, phrase=phrase)
        if not m:
            return []
        clause, cparams = _acl_clause(source_type, "t", visible_ids)
        fts = _fts_table(source_type)
        on = _fts_join(source_type, "t")
        # A recency order wins outright: sort=timestamp asks for the doc's own clock, and the
        # literal-substring tier _fts_relevance_order applies would reorder by relevance under it.
        if order_by in ("recency", "recency_asc"):
            # Slack sort=timestamp: order matches by the message's own ts, not relevance. NULL
            # created_ts (a synthesized ts) sorts last on desc / first on asc — an acceptable edge.
            direction = "ASC" if order_by == "recency_asc" else "DESC"
            order_sql, order_p = f"t.created_ts {direction}, {fts}.rank", []
        else:
            order_sql, order_p = _fts_relevance_order(source_type, query, "t")
        sql = (
            f"SELECT t.* FROM {fts} JOIN {tbl} t ON {on} "
            f"WHERE {fts} MATCH ?{cont_sql.format(a='t')}{clause} "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?"
        )
        return conn.execute(sql, [m, *cont_p, *cparams, *order_p, limit, offset]).fetchall()
    like = f"%{query}%"
    ttl = title_expr(source_type)
    sql = f"SELECT * FROM {tbl} WHERE ({ttl} LIKE ? OR content LIKE ?){cont_sql.format(a=tbl)}"
    params: list = [like, like, *cont_p]
    clause, cparams = _acl_clause(source_type, visible_ids=visible_ids)
    sql += (
        clause + f" ORDER BY (CASE WHEN {ttl} LIKE ? THEN 0 ELSE 1 END), "
        f"{_order_by(source_type)} LIMIT ? OFFSET ?"
    )
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
    if _has_fts(conn, source_type):
        m = _fts_match(query, phrase=phrase)
        if not m:
            return 0
        clause, cparams = _acl_clause(source_type, "t", visible_ids)
        fts = _fts_table(source_type)
        on = _fts_join(source_type, "t")
        sql = (
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {fts} JOIN {tbl} t "
            f"ON {on} WHERE {fts} MATCH ?"
            f"{cont_sql.format(a='t')}{clause} LIMIT ?)"
        )
        return conn.execute(sql, [m, *cont_p, *cparams, cap]).fetchone()[0]
    like = f"%{query}%"
    clause, cparams = _acl_clause(source_type, visible_ids=visible_ids)
    sql = (
        f"SELECT COUNT(*) FROM (SELECT 1 FROM {tbl} WHERE "
        f"({title_expr(source_type)} LIKE ? OR content LIKE ?)"
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
    sql += clause + f" ORDER BY {_order_by(source_type)} LIMIT ? OFFSET ?"
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
    sql += clause + f" ORDER BY {_order_by('slack')} LIMIT ? OFFSET ?"
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
    sql += clause + " ORDER BY thread_ts, thread_seq"
    params += cparams
    return conn.execute(sql, params).fetchall()


def list_gmail_in_range(
    conn, mailbox, ts_lo, ts_hi, visible_ids=None, limit=100_000, offset=0, roots_only=False
) -> list[sqlite3.Row]:
    """Gmail messages whose ``created_ts`` is in ``[ts_lo, ts_hi)`` (either bound may be None for
    open-ended), newest first. The SQL date filter for a date-scoped listing (``ls /gmail/<label>/
    <date>``): without it the endpoint materialized the WHOLE mailbox (~100k rows) and filtered in
    Python. gmail ``created_ts`` is fully populated, so this covers every message.

    ``roots_only`` narrows it to one row per thread, which is what ``threads.list`` lists."""
    sql = "SELECT * FROM gmail_messages WHERE 1=1"
    params: list = []
    if roots_only:
        sql += _GMAIL_ROOT
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
    # created_ts DESC = newest-first (real Gmail's messages.list order); `id` breaks ties into a
    # stable TOTAL order so keyset-free offset pagination can't dupe/skip rows across pages.
    sql += clause + " ORDER BY created_ts DESC, id LIMIT ? OFFSET ?"
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
    sql += clause + " ORDER BY thread_ts, thread_seq"
    params += cparams
    return conn.execute(sql, params).fetchall()


def slack_reply_count(conn, channel, thread_ts, visible_ids=None) -> int:
    """Replies below a thread root. Scoped to the channel as well as the root's ts, because a ts
    is unique only within its channel (see store.ID_COLUMNS) — a bare `thread_ts = ?` would count
    another channel's thread too."""
    sql = (
        "SELECT COUNT(*) FROM slack_messages WHERE channel = ? AND thread_ts = ? AND thread_seq > 0"
    )
    params: list = [channel, thread_ts]
    clause, cparams = _acl_clause("slack", visible_ids=visible_ids)
    sql += clause
    params += cparams
    return conn.execute(sql, params).fetchone()[0]


def slack_channels_for_principals(conn, principals) -> set[str]:
    """Channels with at least one doc granted to any of ``principals``. Reads the
    principal-indexed ``slack_acl`` (idx_slack_acl_pid) and nothing else, so it's cheap even at
    millions of rows — used to list a non-admin caller's visible channels.

    No join to ``slack_messages``: a Slack message is identified by (channel, ts), so the grant
    row carries the channel itself and the answer is a DISTINCT over the ACL table alone."""
    principals = list(principals)
    if not principals:
        return set()
    marks = ",".join("?" for _ in principals)
    rows = conn.execute(
        f"SELECT DISTINCT a.channel FROM {acl_table('slack')} a WHERE a.principal_id IN ({marks})",
        principals,
    )
    return {r[0] for r in rows}


def slack_latest_ts(conn, channel, visible_ids=None) -> str | None:
    """The ts of the newest message in a channel — what conversations.info reports as the caller's
    ``last_read``, Backlot modelling no unread state of its own.

    Ordered by ``created_ts`` rather than ``MAX(ts)`` for the reason slack_latest_reply_ts gives:
    ts is TEXT, so a max over it is lexicographic and picks the wrong row when a channel straddles
    a digit-count change in the epoch second."""
    sql = "SELECT ts FROM slack_messages WHERE channel = ?"
    params: list = [channel]
    clause, cparams = _acl_clause("slack", visible_ids=visible_ids)
    row = conn.execute(
        sql + clause + " ORDER BY created_ts DESC, ts DESC LIMIT 1", params + cparams
    ).fetchone()
    return row[0] if row else None


def slack_latest_reply_ts(conn, channel, thread_ts, visible_ids=None) -> str | None:
    """The ts of a thread's last reply — Slack's ``latest_reply``.

    The last reply's own stored ts, which costs one indexed lookup. Synthesizing it as "the root's
    base second plus the reply count" would report a ts no message need actually have.

    Ordered rather than ``MAX(ts)``: ts is TEXT, so a max over it is lexicographic and a thread
    whose replies straddle a digit-count change (second 9 to second 10) names the wrong reply."""
    sql = "SELECT ts FROM slack_messages WHERE channel = ? AND thread_ts = ? AND thread_seq > 0"
    params: list = [channel, thread_ts]
    clause, cparams = _acl_clause("slack", visible_ids=visible_ids)
    row = conn.execute(
        sql + clause + " ORDER BY created_ts DESC, thread_seq DESC LIMIT 1", params + cparams
    ).fetchone()
    return row["ts"] if row else None


def slack_reply_authors(conn, channel, thread_ts, visible_ids=None) -> list[str]:
    """Distinct reply-author emails in a thread, in reply order (for reply_users)."""
    sql = (
        "SELECT author_email FROM slack_messages "
        "WHERE channel = ? AND thread_ts = ? AND thread_seq > 0"
    )
    params: list = [channel, thread_ts]
    clause, cparams = _acl_clause("slack", visible_ids=visible_ids)
    sql += clause + " ORDER BY thread_seq"
    params += cparams
    seen: list[str] = []
    for r in conn.execute(sql, params):
        if r[0] and r[0] not in seen:
            seen.append(r[0])
    return seen


def slack_thread(conn, channel, thread_ts, visible_ids=None) -> list[sqlite3.Row]:
    """A thread's root and replies in order. Scoped to the channel: a ts identifies a message only
    within one (see store.ID_COLUMNS)."""
    sql = "SELECT * FROM slack_messages WHERE channel = ? AND thread_ts = ?"
    params: list = [channel, thread_ts]
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


def gmail_by_id(conn, message_id, visible_ids=None) -> sqlite3.Row | None:
    """One message by the id the API reports. The stored key is unpadded lowercase hex; callers
    pass the id as the client spelled it, so both the case and any leading zeros are normalized
    here rather than at each call site — real Gmail parses the id as an integer, so `0abc` and
    `abc` are the same message there and must be here."""
    clause, cp = _acl_clause("gmail", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM gmail_messages WHERE id = ?{clause}", [gmail_id_spelling(message_id), *cp]
    ).fetchone()


def gmail_id_spelling(message_id) -> str:
    """A gmail id as it is STORED, from any spelling a client may send it in. Non-hex is handed
    back untouched: rejecting it is the router's job (a 400, ahead of any lookup)."""
    try:
        return f"{int(str(message_id), 16):x}"
    except ValueError:
        return str(message_id).lower()


def github_by_number(conn, repo, number, visible_ids=None) -> sqlite3.Row | None:
    """One issue/PR by the number the API reports, scoped to its repo — github's own uniqueness
    rule (see store.ID_COLUMNS for github). A PRIMARY KEY lookup: the number is assigned at import
    (see :mod:`backlot.importer.byo`'s ``resolve_github_numbers``), so it
    cannot be ambiguous.

    A `kind='file'` row DOES carry a number now — the table holds two resources and only one key
    can be primary (see the schema) — so this CAN return a file row, and the caller must reject
    one. `routers.github._issue_row` is where that happens; a file is addressed by (repo, path)
    and its number is never served."""
    clause, cp = _acl_clause("github", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM github_items WHERE repo = ? AND number = ?{clause}",
        [repo, number, *cp],
    ).fetchone()


def github_pull_refs(conn, repo, visible_ids=None) -> list[sqlite3.Row]:
    """The refs every pull in `repo` names, with what the caller needs to tell a live branch from a
    deleted one — `state` and `merged_at` — and the `number` its synthesized shas are seeded from.

    `kind='pull_request'` only: a `file` row shares this table and carries neither ref, and an
    issue is not a pull. ACL-scoped like any other read, so the branch listing built from this is
    the branch listing for THIS caller.
    """
    clause, cp = _acl_clause("github", visible_ids=visible_ids)
    return conn.execute(
        "SELECT repo, number, COALESCE(state, 'open') AS state, merged_at,"
        " head_ref, base_ref, head_repo"
        f" FROM github_items WHERE repo = ? AND kind = 'pull_request'{clause}",
        [repo, *cp],
    ).fetchall()


# The branch a repo has when its record states no `default_branch` — a fact about the column
# below, and read by both the router that serves it and the importer that checks against it, so
# the two cannot drift into disagreeing about what an unstated default means.
GITHUB_DEFAULT_BRANCH = "main"


def github_repo_meta(conn, repo) -> sqlite3.Row | None:
    """The repo's own row — its stated `default_branch`, `branches` and `tags`, any of which may
    be NULL for a corpus that stated none.

    Not ACL-scoped, and it does not need to be: a `subtype: "repo"` record carries no document and
    no grant, so this row reveals nothing a caller could not already see. Whether the repo exists
    FOR a caller is `routers.github._repo_visible`, which this does not change — but note what that
    predicate already says: a SCOPED caller needs a visible document, and the admin needs only the
    container row. A repo record creates that row without a document, which no record could do
    before, so a repo stated with nothing in it is served to the admin and 404s for everyone else.
    """
    return conn.execute("SELECT * FROM github_repos WHERE repo = ?", [repo]).fetchone()


def github_file_refs(conn, repo, visible_ids=None) -> list[str]:
    """The refs a corpus NAMED on this repo's file snapshots (see :func:`_file_head_clause`).

    These are refs without being branches — a corpus states one to identify a snapshot, not to
    claim the repo has a branch by that name — so they are addressable on `?ref=` and absent from
    the branch listing.
    """
    clause, cp = _acl_clause("github", visible_ids=visible_ids)
    rows = conn.execute(
        "SELECT DISTINCT ref FROM github_items"
        f" WHERE repo = ? AND kind = 'file' AND ref IS NOT NULL{clause}",
        [repo, *cp],
    ).fetchall()
    return [r[0] for r in rows]


def jira_by_key(conn, key, visible_ids=None) -> sqlite3.Row | None:
    """One issue by the key the API reports, whole. A unique-indexed column lookup — the key is
    composed at import (see importer.byo's ``resolve_jira_keys``) and stored, so serving it needs
    neither the project's prefix nor a suffix to recombine.

    Case-SENSITIVE, deliberately: real Jira keys are uppercase and the schema enforces it, and the
    one time this resolved case-insensitively it was an accident of reusing the JQL project
    token's tolerance. There is no prefix resolution here for that tolerance to leak through.
    """
    clause, cp = _acl_clause("jira", visible_ids=visible_ids)
    return conn.execute(f"SELECT * FROM jira_issues WHERE key = ?{clause}", [key, *cp]).fetchone()


def _file_head_clause(visible_ids=None, tbl: str = "t") -> tuple[str, list]:
    """SQL restricting `tbl` to the HEAD of its `(repo, path)` — no snapshot the caller can also
    see is newer.

    A github file is ADDRESSED by `(repo, path)` but STORED one row per snapshot (see the
    `idx_github_file_snapshot` comment), so every read of a file has to choose one. HEAD is the
    newest `created_ts`, and `ref` breaks a tie.

    `ref` and not `number`: two snapshots may share an instant, which is precisely when the schema
    tells an author to state a `ref`, and `number` is an internal handle probed from a hash of the
    dataset id. Tie-breaking on it made the served content a property of a doc_id — renaming a
    document flipped which snapshot the repo answered with — so stating a `ref` bought identity and
    left serving undefined. Ordering on `ref` gives that advice something to settle. The snapshot
    index makes `(created_ts, ref)` total for a path: they cannot both be equal.

    Resolved among the rows the CALLER can see, not corpus-wide. A snapshot hidden from them is
    not the file they are served — otherwise a path whose newest snapshot is restricted answers
    404 for a caller who can read an older one, which reveals that a newer one exists.

    """
    inner, ip = _acl_clause("github", tbl="x", visible_ids=visible_ids)
    return (
        f" AND NOT EXISTS (SELECT 1 FROM github_items x WHERE x.repo = {tbl}.repo"
        f" AND x.path = {tbl}.path AND x.kind = 'file'{inner}"
        f" AND (x.created_ts > {tbl}.created_ts"
        # COALESCE so a stated ref still orders against an unstated one rather than dropping out of
        # the comparison, as any NULL operand would.
        f" OR (x.created_ts = {tbl}.created_ts"
        f" AND COALESCE(x.ref, '') > COALESCE({tbl}.ref, ''))))",
        ip,
    )


def list_repo_files(conn, repo, visible_ids=None, limit=10_000, offset=0) -> list[sqlite3.Row]:
    clause, cp = _acl_clause("github", tbl="t", visible_ids=visible_ids)
    head, hp = _file_head_clause(visible_ids)
    sql = (
        "SELECT t.* FROM github_items t WHERE t.repo = ? AND t.kind = 'file'"
        + clause
        + head
        + " ORDER BY t.path LIMIT ? OFFSET ?"
    )
    return conn.execute(sql, [repo, *cp, *hp, limit, offset]).fetchall()


def list_repo_file_paths(conn, repo, visible_ids=None, limit=10_000, offset=0) -> list[str]:
    """Just the paths :func:`list_repo_files` would return, in the same order.

    For a caller that only needs to CHOOSE among a repo's files rather than read them — github's
    pull-request changeset picks a few paths per pull — and would otherwise drag every file's
    content along to do it. On a 3000-file repo that is ~4 MB of content read per pull, and a
    ``/pulls`` page synthesizes a changeset per row.
    """
    clause, cp = _acl_clause("github", tbl="t", visible_ids=visible_ids)
    head, hp = _file_head_clause(visible_ids)
    sql = (
        "SELECT t.path FROM github_items t WHERE t.repo = ? AND t.kind = 'file'"
        + clause
        + head
        + " ORDER BY t.path LIMIT ? OFFSET ?"
    )
    return [r[0] for r in conn.execute(sql, [repo, *cp, *hp, limit, offset])]


def get_github_comment(conn, comment_id: int) -> sqlite3.Row | None:
    """One comment by the id the API reports, carrying `(repo, number)` so the caller can ACL-check
    the issue or pull it belongs to.

    A PRIMARY KEY lookup: the id is assigned at import (see :mod:`backlot.importer.byo`), so it
    cannot be ambiguous.
    """
    return conn.execute(
        "SELECT id, repo, number, seq, author_email, body, created_ts, reactions, path, line, "
        "diff_hunk FROM github_comments WHERE id = ?",
        (comment_id,),
    ).fetchone()


def confluence_by_id(conn, page_id, visible_ids=None) -> sqlite3.Row | None:
    """One page by the id the API reports — a PRIMARY KEY lookup, so it cannot be ambiguous."""
    clause, cp = _acl_clause("confluence", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM confluence_pages WHERE id = ?{clause}", [page_id, *cp]
    ).fetchone()


def notion_by_id(conn, page_id, visible_ids=None) -> sqlite3.Row | None:
    """One page or database by the id the API reports -- a dashed lowercase UUID. The caller
    canonicalizes a dashless or mixed-case spelling to that same form before calling (see
    routers.notion._norm); this is a plain equality lookup, not a case- or dash-insensitive one."""
    clause, cp = _acl_clause("notion", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM notion_pages WHERE id = ?{clause}", [page_id, *cp]
    ).fetchone()


def notion_by_data_source_id(conn, data_source_id, visible_ids=None) -> sqlite3.Row | None:
    """One DATABASE by its data source id -- the 2025-09-03 API's query target, a second and
    unrelated served-id space for the same row (see the schema comment on idx_notion_served_ds).
    Populated only for subtype='database' rows, so a page's NULL column never matches -- unlike
    notion_by_id (which spans both kinds), a match here already implies the right kind and
    the caller needs no subtype check of its own."""
    clause, cp = _acl_clause("notion", visible_ids=visible_ids)
    return conn.execute(
        f"SELECT * FROM notion_pages WHERE data_source_id = ?{clause}",
        [data_source_id, *cp],
    ).fetchone()


def github_comments(conn, repo, number, *, anchored: bool | None = None) -> list[sqlite3.Row]:
    """``github_comments`` rows for one document, carrying the review-comment columns.

    A github-specific reader because :func:`doc_comments` selects one fixed column list for six
    tables and so cannot carry ``path``/``line``/``diff_hunk``.

    ``anchored`` splits the two resources this one table holds and real GitHub keeps apart — see the
    table's own comment in ``SCHEMA``. ``True`` returns only line-anchored review comments, ``False``
    only conversation comments, ``None`` both.
    """
    where = {None: "", True: " AND path IS NOT NULL", False: " AND path IS NULL"}[anchored]
    return conn.execute(
        "SELECT id, repo, number, seq, author_email, body, created_ts, reactions, path, line, "
        "diff_hunk FROM github_comments WHERE repo = ? AND number = ?" + where + " ORDER BY seq",
        (repo, number),
    ).fetchall()


def count_repo_files(conn, repo, visible_ids=None) -> int:
    clause, cp = _acl_clause("github", tbl="t", visible_ids=visible_ids)
    head, hp = _file_head_clause(visible_ids)
    return conn.execute(
        "SELECT COUNT(*) FROM github_items t WHERE t.repo = ? AND t.kind = 'file'" + clause + head,
        [repo, *cp, *hp],
    ).fetchone()[0]


def get_repo_file(conn, repo, path, visible_ids=None, ref=None) -> sqlite3.Row | None:
    """One file at `(repo, path)` — HEAD, or the snapshot `ref` names.

    `ref` matches the column a corpus states, and falls back to HEAD when no snapshot answers to
    it. The fallback is deliberate: a ref is also a git ref, and Backlot keeps no branch list, so
    `?ref=main` from a real client has to resolve to the current file rather than 404 (the
    no-history tolerance `routers.github.get_tree` documents). A ref a corpus DID name is knowable,
    so it wins.
    """
    clause, cp = _acl_clause("github", tbl="t", visible_ids=visible_ids)
    if ref is not None:
        named = conn.execute(
            "SELECT t.* FROM github_items t WHERE t.repo = ? AND t.kind = 'file' AND t.path = ?"
            " AND t.ref = ?" + clause,
            [repo, path, ref, *cp],
        ).fetchone()
        if named is not None:
            return named
    head, hp = _file_head_clause(visible_ids)
    return conn.execute(
        "SELECT t.* FROM github_items t WHERE t.repo = ? AND t.kind = 'file' AND t.path = ?"
        + clause
        + head,
        [repo, path, *cp, *hp],
    ).fetchone()


def search_repo_files(
    conn, query=None, visible_ids=None, repo=None, path_like=None, limit=10_000, offset=0
) -> list[sqlite3.Row]:
    """HEAD-snapshot `kind='file'` rows — corpus-wide or in one repo, narrowed by an FTS match over
    title+content (`query`) and by paths containing every string in `path_like`.

    Neither existing read answers this and neither should be bent into it: :func:`search_documents`
    spans every kind and every snapshot (it is what `/search/issues` filters files back OUT of),
    and :func:`list_repo_files` is one repo with no text filter. Code search is the inverse of both
    — files only, HEAD only, across repos.

    `path_like` is a substring test rather than a second FTS clause because `path` is not in the
    index (see :func:`_fts_text_columns`), and because a caller matching a path is matching a
    fragment of one, not a stemmed word. It ANDs with `query` where both are given.

    Ordered by relevance under `query` (see :func:`_fts_relevance_order`) and by `(repo, path)`
    without one, so a listing is stable and a search leads with its best hit.
    """
    clause, cp = _acl_clause("github", tbl="t", visible_ids=visible_ids)
    head, hp = _file_head_clause(visible_ids)
    narrow, np = "", []
    if repo is not None:
        narrow, np = " AND t.repo = ?", [repo]
    for frag in path_like or ():
        # ESCAPE, because a path fragment is a LITERAL: `_` is common in filenames and is also
        # LIKE's single-character wildcard, so `mod_7` would otherwise answer with `mod-7` too.
        narrow += " AND lower(t.path) LIKE ? ESCAPE '\\'"
        np.append(f"%{_like_escape(frag.lower())}%")
    if query is not None and _has_fts(conn, "github"):
        m = _fts_match(query)
        if not m:
            return []
        fts = _fts_table("github")
        order_sql, order_p = _fts_relevance_order("github", query, "t")
        sql = (
            f"SELECT t.* FROM {fts} JOIN github_items t ON {_fts_join('github', 't')}"
            f" WHERE {fts} MATCH ? AND t.kind = 'file'{narrow}{clause}{head}"
            f" ORDER BY {order_sql} LIMIT ? OFFSET ?"
        )
        return conn.execute(sql, [m, *np, *cp, *hp, *order_p, limit, offset]).fetchall()
    text, tp = "", []
    if query is not None:  # no FTS5: the same LIKE fallback search_documents uses
        # Escaped like the path fragment above, and for the same reason: a wildcard the CALLER
        # typed is text they are searching for. Without it `100%` matches every file in the repo.
        needle = f"%{_like_escape(query)}%"
        text, tp = (
            " AND (t.title LIKE ? ESCAPE '\\' OR t.content LIKE ? ESCAPE '\\')",
            [needle, needle],
        )
    sql = (
        "SELECT t.* FROM github_items t WHERE t.kind = 'file'"
        + text
        + narrow
        + clause
        + head
        + " ORDER BY t.repo, t.path LIMIT ? OFFSET ?"
    )
    return conn.execute(sql, [*tp, *np, *cp, *hp, limit, offset]).fetchall()


def iter_repo_file_snapshots(conn, repo, visible_ids=None) -> Iterator[sqlite3.Row]:
    """EVERY file row in the repo, superseded snapshots included — the one read that does not
    collapse to HEAD.

    For content-addressed lookup: a blob sha is `sha1(content)`, so each snapshot has its own and
    stays fetchable after a newer one supersedes it. SQLite cannot compute the digest, so the
    caller has to scan candidates, and scanning :func:`list_repo_files` would 404 every blob but
    HEAD's.

    A cursor rather than a list, and uncapped where its siblings take a `limit`. Both follow from
    what the caller does: it compares a digest and stops at the first match, so streaming means the
    common fetch reads a few rows instead of materialising a repo's every file (`SELECT *` here is
    content included — the ~4 MB `list_repo_file_paths` exists to avoid). A cap would instead make
    a blob past it a false 404, which is the bug this replaced.
    """
    clause, cp = _acl_clause("github", visible_ids=visible_ids)
    return conn.execute(
        "SELECT * FROM github_items WHERE repo = ? AND kind = 'file'" + clause + " ORDER BY path",
        [repo, *cp],
    )


# --- grouping units (channels/mailboxes/folders/repos/projects/spaces) & principals ---


def jira_project_key(conn, project: str) -> str | None:
    """The key a jira project is served under (`PAY`) — its own stored column, not a prefix
    reversed at boot out of the issues. One row per project, so this is a PK lookup."""
    row = conn.execute("SELECT key FROM jira_projects WHERE project = ?", (project,)).fetchone()
    return row["key"] if row else None


def jira_project_by_key(conn, key: str) -> str | None:
    """The project a key names, the reverse of :func:`jira_project_key`. Case-insensitive, because
    this answers the JQL project TOKEN (`project = pay`), which real Jira's pickers accept in any
    case — deliberately unlike issue-key resolution, which is exact (see routers.atlassian's
    `_resolve_jira_key` for what mixing the two cost)."""
    row = conn.execute(
        "SELECT project FROM jira_projects WHERE UPPER(key) = ?", (key.upper(),)
    ).fetchone()
    return row["project"] if row else None


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
    plain column lookup."""
    row = conn.execute("SELECT team FROM linear_teams WHERE served_id = ?", (served_id,)).fetchone()
    return row["team"] if row else None


def linear_team_keys(conn) -> dict[str, str]:
    """team -> the key its issues' identifiers are prefixed with, for every linear team.

    One scan of a table with one row per team, so callers that need the key of more than one team
    (the ``teams`` filter, the compiled issue filter, a page of issues each selecting
    ``team { key }``) read it once instead of per row. A team's key is
    :func:`synth.linear_team_key` of its name UNLESS its own issues spell a different prefix out,
    in which case the importer stored that -- which is exactly why this reads the column rather
    than deriving."""
    return {
        r["team"]: r["served_key"]
        for r in conn.execute("SELECT team, served_key FROM linear_teams")
    }


def linear_team_by_served_key(conn, served_key) -> str | None:
    """Resolve a team KEY (`synth.linear_team_key`, e.g. "ENG") to its container name.

    The key is NOT injective -- two containers can reduce to the same one -- so `served_key`
    carries no UNIQUE index. A key the corpus SPELLED OUT wins the tie: it is a fact about that
    team, where a colliding one is only the shape another team's name happens to shorten to, and
    `team(id: "ENG")` answering with the latter left the team the corpus called ENG unreachable at
    its own key. Told apart by re-deriving: a served key that is not its team's derived key is one
    the importer was told (see `byo._Loader._claim_linear_prefix`). Among equals the tie goes to
    team NAME order, keeping the first team a key is seen on -- change either half of that without
    the other and a key silently resolves to a different team."""
    rows = conn.execute(
        "SELECT team FROM linear_teams WHERE served_key = ? ORDER BY team", (str(served_key),)
    ).fetchall()
    if not rows:
        return None
    for row in rows:
        if synth.linear_team_key(row["team"]) != str(served_key):
            return row["team"]
    return rows[0]["team"]


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
    Unique-indexed column lookup."""
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


def slack_private_channel_members(conn, channel) -> list[str] | None:
    """The user principals who may read a private channel — its membership — in email order.
    ``None`` for a public channel, which has no such list.

    A private channel and a public one derive membership from different facts, because Slack shows
    them differently. Slack lists a private channel to its members ONLY, so being able to read one
    IS being in it: the grants ARE the membership, and a group grant is expanded to the people in
    it because the membership a client walks is a list of people. A public channel is shown to
    everyone in the org, so its org grant says nothing about who is in it — see
    :func:`slack_channel_member_emails` for what answers there.

    An empty list is a private channel nobody may read (``"readers": []``, or a grant to a group
    with no members): it has no membership rather than a membership of everyone.
    """
    if container_has_public(conn, "slack", channel):
        return None
    members: set[str] = set()
    for ptype, pid in conn.execute(
        "SELECT DISTINCT principal_type, principal_id FROM slack_acl WHERE channel = ?",
        (channel,),
    ):
        if ptype == "user":
            members.add(pid)
        elif ptype == "group":
            members.update(r["id"] for r in group_members(conn, pid))
    return sorted(members)


def slack_channel_member_emails(conn, channel, limit=100, offset=0) -> list[str]:
    """One page of a channel's members, in email order.

    A private channel's members are the people who may read it
    (:func:`slack_private_channel_members`). A public channel's are the people who have spoken in
    it — everyone in the org may read a public channel, and real Slack lists public channels to
    people who are not in them, so readership cannot be the membership there. Speaking is the only
    other per-channel signal a corpus carries, and answering with the whole roster instead would
    give every public channel the same members, which real Slack cannot produce.

    The public path is index-only on idx_slack_channel_author, so a page costs a seek rather than a
    scan of the channel; the private path pages a set small enough to hold (a channel's grantees).
    """
    members = slack_private_channel_members(conn, channel)
    if members is not None:
        return members[offset : offset + limit]
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT author_email FROM slack_messages WHERE channel = ? "
            "ORDER BY author_email LIMIT ? OFFSET ?",
            (channel, limit, offset),
        )
    ]


def slack_membership_violations(conn) -> list[tuple[str, str]]:
    """``(channel, email)`` for every speaker who cannot read the private channel they spoke in.

    Speaking in a channel means being in it, and a member of a private channel can read it — so a
    speaker outside that channel's grantees is two facts that cannot both hold: the same person is
    served by :func:`slack_channel_member_emails` and told `channel_not_found` by
    conversations.info. The corpus has no way to say "left the channel", which is the one state
    real Slack reaches this from, so within this model it is a corpus that cannot be true.

    Only PRIVATE channels can produce it — a public channel's org grant covers every principal —
    and only speakers who are principals: a display-only speaker has no identity to authenticate
    with, so there is nobody for the two answers to disagree about.
    """
    out: list[tuple[str, str]] = []
    for row in list_containers(conn, "slack"):
        channel = row["name"]
        # `None` is a public channel, whose org grant admits every principal, and an empty list is
        # a channel nobody may read — neither has a membership a speaker can fall outside of.
        allowed = slack_private_channel_members(conn, channel)
        if not allowed:
            continue
        for (email,) in conn.execute(
            "SELECT DISTINCT m.author_email FROM slack_messages m "
            "JOIN principals p ON p.id = m.author_email AND p.type = 'user' "
            "WHERE m.channel = ? ORDER BY m.author_email",
            (channel,),
        ):
            if email not in allowed:
                out.append((channel, email))
    return out


def slack_channel_has_author(conn, channel, email) -> bool:
    """Whether ``email`` has spoken in a channel — which is being a member of a PUBLIC one, the
    same set :func:`slack_channel_member_emails` pages there, asked about one person. Index-only on
    idx_slack_channel_author with equality on both columns, so it is a seek rather than the DISTINCT
    scan that counting the members is."""
    return (
        conn.execute(
            "SELECT 1 FROM slack_messages WHERE channel = ? AND author_email = ? LIMIT 1",
            (channel, email),
        ).fetchone()
        is not None
    )


def slack_channel_member_counts(conn) -> dict[str, int]:
    """Every channel's member count in one pass. Per-channel COUNT(DISTINCT) is ~1.9s on the
    biggest channel measured, and conversations.list shapes every channel in the page, so counting
    them one at a time would be minutes per request; this is 12.2s once.

    Counted from whatever membership that channel has, so `num_members` and walking
    :func:`slack_channel_member_emails` cannot disagree — the speakers for a public channel, the
    grantees for a private one. Every channel is keyed, including one with no messages at all,
    which the GROUP BY alone cannot reach.
    """
    counts = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT channel, COUNT(DISTINCT author_email) FROM slack_messages GROUP BY channel"
        )
    }
    return {
        channel: (len(members) if members is not None else counts.get(channel, 0))
        for channel, members in (
            (row["name"], slack_private_channel_members(conn, row["name"]))
            for row in list_containers(conn, "slack")
        )
    }


def count_slack_channel_members(conn, channel) -> int:
    """One channel's member count — :func:`slack_channel_member_counts` for a single channel, for
    the window before that cache is warm."""
    members = slack_private_channel_members(conn, channel)
    if members is not None:
        return len(members)
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
        f"JOIN {tbl} d ON {_acl_join(source_type, 'a', 'd')} WHERE d.{gcol} = ?",
        (container,),
    ).fetchall()


def container_has_public(conn, source_type, container) -> bool:
    tbl, gcol = table(source_type), grouping_col(source_type)
    return (
        conn.execute(
            f"SELECT 1 FROM {acl_table(source_type)} a JOIN {tbl} d "
            f"ON {_acl_join(source_type, 'a', 'd')} "
            f"WHERE d.{gcol} = ? AND a.principal_type = 'org' LIMIT 1",
            (container,),
        ).fetchone()
        is not None
    )


def doc_grants(conn, source_type, *ident) -> list[sqlite3.Row]:
    """Every grant on one document. ``ident`` is positional against :func:`id_columns`, the same
    contract :func:`get_document` follows."""
    key = id_columns(source_type)
    if len(ident) != len(key):
        raise ValueError(
            f"doc_grants: {source_type!r} is identified by {key}, so it takes "
            f"{len(key)} value(s), not {len(ident)}"
        )
    where = " AND ".join(f"{c} = ?" for c in key)
    return conn.execute(
        f"SELECT principal_type, principal_id FROM {acl_table(source_type)} WHERE {where} "
        "ORDER BY principal_type, principal_id",
        ident,
    ).fetchall()


def docs_with_grants(conn, source_type, doc_keys: list) -> set:
    """The subset of ``doc_keys`` that have at least one ACL grant — one query (chunked to stay
    under SQLite's variable limit) instead of a per-doc ``doc_grants`` call when building a list.

    Each entry is one document's key, positional against :func:`id_columns`: a bare value for a
    single-column source, a tuple for slack / s3 / github. The returned set is spelled the same
    way the input was, so a caller can test membership with the value it passed in."""
    cols = id_columns(source_type)
    key = ", ".join(cols)
    rows = [k if isinstance(k, tuple) else (k,) for k in doc_keys]
    # The variable limit counts VALUES, not rows, so the chunk shrinks with the key's arity.
    per_chunk = max(1, 900 // len(cols))
    out: set = set()
    for i in range(0, len(rows), per_chunk):
        chunk = rows[i : i + per_chunk]
        values = ",".join("(" + ",".join("?" for _ in cols) + ")" for _ in chunk)
        flat = [v for row in chunk for v in row]
        found = conn.execute(
            f"SELECT DISTINCT {key} FROM {acl_table(source_type)} "
            f"WHERE ({key}) IN (VALUES {values})",
            flat,
        ).fetchall()
        out.update(tuple(r) if len(cols) > 1 else r[0] for r in found)
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


def doc_member_emails(conn, source_type, *ident) -> set[str] | None:
    return _expand_grants(conn, doc_grants(conn, source_type, *ident))


# --- comments -------------------------------------------------------------------


def doc_comments(conn, source_type, *ident) -> list[sqlite3.Row]:
    """A document's child rows in order. ``ident`` is positional against :func:`id_columns` — the
    parent is named by its served id, and the comment table's own columns for it come from
    :data:`COMMENT_PARENT`."""
    tbl = COMMENT_TABLE.get(source_type)
    if tbl is None:
        return []
    cols = comment_parent_columns(source_type)
    if len(ident) != len(cols):
        raise ValueError(
            f"doc_comments: {source_type!r} comments hang off {cols}, so this takes "
            f"{len(cols)} value(s), not {len(ident)}"
        )
    where = " AND ".join(f"{c} = ?" for c in cols)
    return conn.execute(
        f"SELECT id, seq, author_email, body, created_ts, reactions FROM {tbl} "
        f"WHERE {where} ORDER BY seq",
        ident,
    ).fetchall()
