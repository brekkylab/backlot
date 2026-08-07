# Bring your own corpus

Serve **any** document set through all eleven mock APIs — provide a JSONL where each line is one
document, validate it, and load it:

```bash
python -m backlot.importer.byo mycorpus.jsonl              # validate + load -> data/
python -m backlot.importer.byo mycorpus.jsonl --dry-run    # validate only, no DB writes
python -m uvicorn backlot.main:app --port 8000
```

`run.py` here is a self-contained walkthrough — it validates `sample_corpus.jsonl`, starts a
real mock server backed by it, and reads it back over HTTP (ACL enforced):

```bash
python examples/bring-your-own-corpus/run.py
```

`sample_corpus.jsonl` is a runnable sample for a fictional "Acme". It deliberately fills in
**every** field the schemas expose — `created`/`updated` on all records, plus the per-service
fidelity fields (slack rich replies with reactions/files/edited and `participants`; gmail
`to`/`html`/`mailbox_owner` and a `messages` thread with `in_reply_to`; drive
`trashed`/`parents`/`collaborators`; github `closed_at`/`merged_by`/`milestone`/
`requested_reviewers` + comment reactions; jira `assignee`/`resolution`/`resolutiondate`/
`duedate`/`severity`/`squad`; confluence `version_number`/`version_message`/`minor_edit`/
`confidentiality`/`owner_team`/`reviewers`; linear a parent/child pair with `relations`,
`attachments`, `estimate`/`cycle`/`project`/`release` and lifecycle timestamps) — so you can see
that none of the response structure has to be synthesized: it can all be set directly from the
corpus. `tests/test_schema.py` asserts it stays valid and keeps covering every served source.

## Record format

Only `source_type` and `content` are required; `title` is required for every source **except
Slack** (Slack messages have no title). One JSON object per line (JSONL) — for example:

```json
{"source_type": "slack", "channel": "incidents", "author_email": "bob@acme.com", "content": "Anyone seeing 502s from the gateway?", "reactions": [{"name": "eyes", "count": 2}], "replies": [{"content": "Looking now.", "author_email": "ava@acme.com"}, {"content": "Rolled back — clearing up.", "author_email": "bob@acme.com"}]}
{"source_type": "gmail", "mailbox": "ceo", "title": "Q1 board deck draft", "content": "Draft narrative for the Q1 board meeting.", "author_email": "ceo@acme.com", "to": "ava@acme.com", "cc": "cfo@acme.com", "readers": ["ceo@acme.com", "ava@acme.com"]}
{"source_type": "github", "repo": "gateway", "subtype": "pull_request", "title": "Fix token-bucket refill off-by-one", "content": "Corrects the refill tick; adds a test.", "author_email": "bob@acme.com", "state": "closed", "merged_at": "2026-02-10T12:00:00Z", "reviews": [{"author_email": "ava@acme.com", "state": "APPROVED", "body": "LGTM"}]}
{"source_type": "jira", "project": "payments", "title": "SEV2: checkout latency spike", "content": "p95 checkout latency jumped to 2.1s.", "author_email": "bob@acme.com", "author_groups": ["payments"], "visibility": "group", "status": "In Progress", "issuetype": "Incident", "assignee": "ava@acme.com"}
{"source_type": "google_drive", "folder": "marketing", "subtype": "spreadsheet", "title": "Q1 Revenue Model", "content": "month,revenue\nJan,120000\nFeb,135000", "author_email": "cfo@acme.com", "author_groups": ["finance"], "visibility": "group"}
{"source_type": "confluence", "space": "handbook", "title": "On-call Runbook", "content": "Respond to gateway 502s: check dashboards, roll back, page on-call.", "author_email": "ava@acme.com", "author_groups": ["engineering"], "labels": ["oncall", "runbook"]}
{"source_type": "notion", "teamspace": "engineering", "subtype": "database", "title": "Eng Tasks", "content": "Engineering task tracker.", "doc_id": "nt-tasks-db", "properties": {"Status": {"type": "select"}}}
{"source_type": "notion", "teamspace": "engineering", "title": "Fix gateway 502s", "content": "Investigate token-bucket refill.", "parent": "nt-tasks-db", "properties": {"Status": "In Progress"}, "icon": "🐛"}
{"source_type": "hubspot", "object_type": "contacts", "title": "Ava Stone", "content": "Ava Stone — VP Platform at Acme Health.", "properties": {"firstname": "Ava", "lastname": "Stone", "email": "ava@acme-health.com"}, "associations": [{"to": "hs-co-acme", "label": "Primary"}]}
```

See `sample_corpus.jsonl` for a fully-populated record of every source type.

- `source_type` ∈ `slack | gmail | google_drive | github | jira | confluence | notion | s3 |
  hubspot | linear | fireflies`.
- The grouping unit is named per service — `channel` (slack), `mailbox` (gmail),
  `folder` (google_drive), `repo` (github), `project` (jira), `space` (confluence),
  `teamspace` (notion), `bucket` (s3), `object_type` (hubspot), `team` (linear),
  `channel` (fireflies).
- **ACL per doc:** `readers` (emails → users, other ids → groups) win; else `visibility`
  `public | group | private` (default `public`). Group membership is derived from each author's
  `author_groups` plus the grouping unit they wrote in.
- Groups, users, and a per-user token for each are derived from the corpus and written to
  `data/tokens.yaml` — the same token-scoped ACL then applies across every one of them and MCP.
- **Org:** the org name + domain are inferred from the corpus's dominant author email domain
  (a `@acme.com` corpus serves as org `acme`, so Slack `auth.test`, `/_mock/users`, and default
  emails all say `acme` — not a hardcoded default). Override with `BACKLOT_ORG_NAME` /
  `BACKLOT_ORG_DOMAIN`. The chosen values are persisted to `data/tokens.yaml`.
- **Slack threads:** a slack record may carry a `replies` array. Each reply is a full message
  (`content`, optional `author_email`/`author_name`/`subtype`/`reactions`/`files`/`edited`), not
  just text. It becomes a thread — the record is the root, each reply a threaded reply. Only the
  root appears in `conversations.history`; the full thread comes back from `conversations.replies`
  (shared `thread_ts`, increasing `ts`, `reply_count` on the root). Reply times follow the root's
  `created` + position, so the thread stays ordered.
- **Fireflies transcripts:** a fireflies record's child rows are `sentences`, not `replies`
  — a transcript should read like a transcript, so `replies` on a `fireflies` record is
  rejected rather than ignored. Each sentence carries `text`, an optional `speaker_name`
  (null for an unattributed utterance), an optional `author_email` resolving the speaker to
  an identity, and optional `start_time`/`end_time` in **seconds** (`duration` on the record
  is in **minutes** — Fireflies' own units). `content` and `sentences` are two views of the
  same text: supply `sentences` and `content` is derived from them, or supply only `content`
  (a plain `Speaker: text` body) and the sentences are parsed back out of it. A line that
  names no speaker folds into the sentence above it. Either way the two round-trip exactly,
  so full-text search and the per-sentence API can never disagree; a record with neither is
  a load error, because one of the two IS the transcript.
- **Gmail threads:** a gmail record may carry a `messages` array — the thread's later messages,
  this record being the first. Each is a full message with its own `author_email`, `to`/`cc`,
  `message_id` and `created`, sharing the root's thread id and ACL. It is a separate array from
  slack's `replies` on purpose: a threaded reply and a further email in a thread are different
  things, and only the latter has recipients and a Message-ID of its own. A message's `content`
  may be empty — a header-only auto-ack is still a message, and dropping it would renumber the
  rest of the thread.
- **Owner display name:** `author_name` is served as the document's owner, under each service's
  own name for it — gmail uses `mailbox_owner` (a mailbox's owner is usually not the sender of a
  given message in it) and fireflies `host_name` (the meeting's host).
  It is stored rather than derived because a name does not survive an email address — "Tomás Rré"
  slugs to `tomas.rre`, and there is no way back.
- **Typed reader principals:** a `readers` entry may say what it is — `user:<email>`,
  `group:<id>`, `org:<name>`. Unprefixed, an address is a user and anything else a group. Use the
  typed form when a document is org-readable *and* names its owners; the shorthand cannot name the
  org principal at all.
- **`group: null`:** the container owns no ACL group. A real state, not a missing value — a Gmail
  mailbox has no group scope, so inferring one from its name would invent a grantable principal.
  An *absent* `group` still defaults to the container slug.
- **Stating the roster:** pass `--roster roster.yaml` and `principals`/`group_members`/
  `tokens.yaml` come from that file alone, instead of every `author_email` becoming a token-holding
  user. That is how a corpus converted from an existing dataset carries the people it already knows
  — including which of them are real accounts. See
  [`schemas/README.md`](../../backlot/schemas/README.md).
- **Timestamps:** every record accepts `created` (epoch seconds or ISO 8601) — it drives the
  Slack `ts` / Gmail `Date`+`internalDate` / Drive `createdTime` / GitHub `created_at` / Jira
  `created` / Confluence version time. Drive/GitHub/Jira/Confluence also accept `updated`
  (default: `created` + 1h). Omit either and it's synthesized deterministically from the `doc_id`.
- **Gmail recipients:** `to` sets the `To` header (default `<mailbox>@<org_domain>`).

Per-service extras (`subtype`, `labels`, `reactions`, `comments`, `issuelinks`, …) are
described by the per-service JSON Schemas — see [`schemas/README.md`](../../backlot/schemas/README.md).
Each record is validated against its schema before loading, so typos and shape errors fail fast
with a line number; the schemas double as the contract for LLM dataset generation.
