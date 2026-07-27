# Bring your own corpus

Serve **any** document set through the nine mock APIs — provide a JSONL where each line is one
document, validate it, and load it:

```bash
python -m app.importer.byo mycorpus.jsonl              # validate + load -> data/
python -m app.importer.byo mycorpus.jsonl --dry-run    # validate only, no DB writes
python -m uvicorn app.main:app --port 8000
```

`run.py` here is a self-contained walkthrough — it validates `sample_corpus.jsonl`, starts a
real mock server backed by it, and reads it back over HTTP (ACL enforced):

```bash
python examples/bring-your-own-corpus/run.py
```

`sample_corpus.jsonl` is a runnable sample for a fictional "Acme". It deliberately fills in
**every** field the schemas expose — `created`/`updated` on all records, plus the per-service
fidelity fields (slack rich replies with reactions/files/edited; gmail `to`/`html`/threaded
`in_reply_to`; drive `trashed`/`parents`; github `closed_at`/`merged_by`/`milestone`/
`requested_reviewers` + comment reactions; jira `assignee`/`resolution`/`resolutiondate`/
`duedate`; confluence `version_number`/`version_message`/`minor_edit`) — so you can see that
none of the response structure has to be synthesized: it can all be set directly from the corpus.

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

- `source_type` ∈ `slack | gmail | google_drive | github | jira | confluence | notion | s3 | hubspot`.
- The grouping unit is named per service — `channel` (slack), `mailbox` (gmail),
  `folder` (google_drive), `repo` (github), `project` (jira), `space` (confluence),
  `teamspace` (notion), `bucket` (s3).
- **ACL per doc:** `readers` (emails → users, other ids → groups) win; else `visibility`
  `public | group | private` (default `public`). Group membership is derived from each author's
  `author_groups` plus the grouping unit they wrote in.
- Groups, users, and a per-user token for each are derived from the corpus and written to
  `data/tokens.yaml` — the same token-scoped ACL then applies across all eight APIs and MCP.
- **Org:** the org name + domain are inferred from the corpus's dominant author email domain
  (a `@acme.com` corpus serves as org `acme`, so Slack `auth.test`, `/_mock/users`, and default
  emails all say `acme` — not a hardcoded default). Override with `MOCK_ORG_NAME` /
  `MOCK_ORG_DOMAIN`. The chosen values are persisted to `data/tokens.yaml`.
- **Slack threads:** a slack record may carry a `replies` array. Each reply is a full message
  (`content`, optional `author_email`/`author_name`/`subtype`/`reactions`/`files`/`edited`), not
  just text. It becomes a thread — the record is the root, each reply a threaded reply. Only the
  root appears in `conversations.history`; the full thread comes back from `conversations.replies`
  (shared `thread_ts`, increasing `ts`, `reply_count` on the root). Reply times follow the root's
  `created` + position, so the thread stays ordered.
- **Timestamps:** every record accepts `created` (epoch seconds or ISO 8601) — it drives the
  Slack `ts` / Gmail `Date`+`internalDate` / Drive `createdTime` / GitHub `created_at` / Jira
  `created` / Confluence version time. Drive/GitHub/Jira/Confluence also accept `updated`
  (default: `created` + 1h). Omit either and it's synthesized deterministically from the `doc_id`.
- **Gmail recipients:** `to` sets the `To` header (default `<mailbox>@<org_domain>`).

Per-service extras (`subtype`, `labels`, `reactions`, `comments`, `issuelinks`, …) are
described by the per-service JSON Schemas — see [`schemas/README.md`](../../schemas/README.md).
Each record is validated against its schema before loading, so typos and shape errors fail fast
with a line number; the schemas double as the contract for LLM dataset generation.
