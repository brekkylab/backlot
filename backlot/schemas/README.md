# BYO corpus JSON Schemas

One [Draft 2020-12](https://json-schema.org/) schema per served source type — **the source of
truth** for the JSONL record that `backlot/importer/byo.py` accepts:

| File | `source_type` | grouping-unit field |
|---|---|---|
| `slack.schema.json` | `slack` | `channel` |
| `gmail.schema.json` | `gmail` | `mailbox` |
| `google_drive.schema.json` | `google_drive` | `folder` |
| `github.schema.json` | `github` | `repo` |
| `jira.schema.json` | `jira` | `project` |
| `confluence.schema.json` | `confluence` | `space` |
| `notion.schema.json` | `notion` | `teamspace` |
| `s3.schema.json` | `s3` | `bucket` |
| `hubspot.schema.json` | `hubspot` | `object_type` |
| `linear.schema.json` | `linear` | `team` |
| `fireflies.schema.json` | `fireflies` | `channel` |

Edit these files directly to change the accepted record shape. `backlot/validation.py`
loads them at runtime (keyed by each schema's `properties.source_type.const`), so a new source
type is just a new `*.schema.json` file here.

## Child rows are named per source

Most sources' child rows are **comments** (`comments`). Three are not, and each uses the array a
reader of that source would expect:

- **Slack** uses `replies` — threaded replies to a message, carrying reactions and files.
- **Gmail** uses `messages` — the rest of the thread. Each is a full RFC822 message with its own
  sender, recipients and Message-ID, which a *reply* is not; and one may have an empty body, since
  a header-only auto-ack is still a message in the thread.
- **Fireflies** uses `sentences` — a transcript's utterances, each with a speaker and its
  timing. `replies` is deliberately *not* overloaded for any of these: writing a transcript should
  read like writing a transcript, and a `replies` array on a `fireflies` record is rejected rather
  than silently ignored.

In every case the record itself is the root (sequence 0), each child takes the next sequence
number, and children inherit the root's container and ACL.

Fireflies is also the one source where `content` and its child rows are two views of the **same**
text: supply `sentences` and `content` is derived from them, or supply only `content` and the
sentences are parsed back out of it. Either way the two round-trip exactly, so full-text search
and the per-sentence API can never disagree.

## Validate a corpus

```bash
python -m backlot.importer.byo path/to/corpus.jsonl --dry-run
```

Each JSONL line is dispatched to its `source_type` schema; problems are reported with a line
number and JSON path, and the exit code is non-zero on any failure (CI / pre-commit friendly).
`backlot.importer.byo` runs the same validation, so an invalid corpus never half-loads.

## Generating a dataset with an LLM

Hand the relevant service schema to a model as a structured-output / tool schema so generated
records conform to what the loader reads — then validate the output before loading.

Anthropic API (structured outputs), Python:

```python
import json
from pathlib import Path
import anthropic

schema = json.loads(Path("backlot/schemas/confluence.schema.json").read_text())
client = anthropic.Anthropic()

msg = client.messages.parse(
    model="claude-opus-4-8",
    max_tokens=2000,
    thinking={"type": "adaptive"},
    output_config={"format": {"type": "json_schema", "schema": schema}},
    messages=[{"role": "user",
               "content": "Generate one realistic Confluence on-call runbook page for an "
                          "infra team, visibility=group."}],
)
record = msg.content  # already conforms to the schema
```

Generate per service (one schema at a time), append each record to a `.jsonl`, then:

```bash
python -m backlot.importer.byo generated.jsonl --dry-run && python -m backlot.importer.byo generated.jsonl
```

## What the schemas enforce

- **Strict** — `source_type` (const), required `content` (+ `title` for every source except
  Slack), the `visibility` enum, per-service `subtype` enums (e.g. github `issue|pull_request`,
  drive `document|spreadsheet|presentation|pdf`, confluence `page|blogpost`, notion
  `page|database`), the child-row object shapes (see *Child rows are named per source* above —
  each array is accepted only on the source it belongs to), and `additionalProperties: false` (an
  unknown top-level key is almost always a typo). Gmail's `content` and its `messages[].content`
  are the one exception to non-empty content: a thread opened or continued by a header-only message
  (auto-ack, bare forward) is real, and dropping it would renumber the rest of the thread.
- **Permissive** — the free-form `meta` object and the loosely typed per-service extras
  (`reactions`, `attachments`, `issuelinks`, `reviews`, `changelog`, …), which the loader stores
  as JSON without a fixed shape.
- **Timestamps** — every source accepts `created` (epoch seconds or ISO 8601); drive/github/
  jira/confluence also accept `updated`. Both are optional — when omitted the router synthesizes
  a stable time from the `doc_id`. Slack `replies` are full messages (`reactions`/`files`/
  `subtype`/`edited`, not just `content`); gmail accepts an explicit `to`.
- **Per-service fidelity fields** (all optional; see each schema):
  gmail `html`; drive `trashed`; github `closed_at`/`closed_by`/`merged_by`/`milestone`/
  `requested_reviewers` (+ comment `reactions`); jira `assignee`/`reporter`/`resolution`/
  `resolutiondate`/`duedate`/`fix_versions`; confluence `version_number`/`version_message`/
  `minor_edit`; notion `properties` (database schema / row values), `icon`, `cover` (+ `subtype`
  `page|database`, `parent` for database rows); s3 `key` (**required** — the object's path within
  the bucket), `content_type` (MIME type, default `text/plain`), `size` (byte length, default:
  computed from `content`), `subtype` (storage-class label, default `STANDARD`). These map to the
  fields the real vendor APIs return; everything else on each response is synthesized
  deterministically from the `doc_id`.
- **Per-service people and scope** (all optional): confluence `confidentiality` (free text — a
  served label; ACL still comes from `visibility`/`readers`), `owner_team`, `reviewers`; drive
  `collaborators`; jira `severity` (a separate axis from `priority` — how bad, not when to fix)
  and `squad`; slack `participants` (thread-level, so root-only); gmail `mailbox_owner`. Plus
  `author_name` on every source whose table stores an owner display name, since a name is not
  recoverable from an address ("Tomás Rré" does not survive `<slug>@<domain>`).
- **Principals** — a `readers` entry may state its type: `user:<email>` / `group:<id>` /
  `org:<name>`. Unprefixed, an address is a user and anything else a group, as before. The typed
  form exists because the shorthand cannot name the org principal, so "org-readable *and* owned by
  these people" had no spelling.
- **Groups** — `group: null` means the container owns no ACL group. That is a state, not a missing
  value: a Gmail mailbox has no group scope (a thread is private to its participants), and
  inferring one from the mailbox name would invent a grantable principal. An *absent* `group`
  still defaults to the container slug.

## Stating the roster instead of deriving it

By default the roster is derived from the corpus: every `author_email` becomes a user with a bearer
token, named from its address. That is right for a hand-written corpus. A corpus converted from an
existing dataset already knows its people — and knows that only some of them are accounts — so it
ships a roster alongside:

```bash
python -m backlot.importer.byo corpus.jsonl --roster roster.yaml
```

```yaml
org: redwood                      # optional (default: inferred from the corpus)
org_domain: redwoodinference.com  # optional
departments:                      # authenticating users -> a bearer token each
  Engineering:
    - {name: Ava Chen, email: ava.chen@redwoodinference.com}
contacts:                         # principals with NO token (display-only)
  - {name: Zoe Newperson, email: zoe.newperson@redwoodinference.com, group: engineering}
```

With a roster, `principals` / `group_members` / `tokens.yaml` come from it **alone**: a record's
`author_email` and `readers` are references into it, and an address that is not in it — a Slack
display handle, an outside sender — stays a plain address on the document instead of silently
becoming an org account with a working token. `departments` is exactly the shape of
EnterpriseRAG-Bench's `employee_directory.yaml`, so that file works as a roster verbatim.

## Round-tripping an existing dataset

`backlot.importer.erb` can write a BYO artifact instead of a database, which is how the bench is
redistributed in this schema:

```bash
python -m backlot.importer.erb --export-byo out/   # -> out/corpus.jsonl + out/roster.yaml
python -m backlot.importer.byo out/corpus.jsonl --roster out/roster.yaml
```

The result is a database **equivalent** to importing the bench directly — same rows, same column
values, same `doc_acl`, same `tokens.yaml`, for all nine bench sources.
`tests/test_importer_erb.py` asserts exactly that, as a table-by-table diff, so expressiveness the
schemas lose fails a test instead of quietly producing a lossy artifact. It also asserts that every
source in `erb.SUPPORTED` has a converter and a fixture, because the conversion fails soft: a source
without one would be dropped from the artifact rather than raising.
