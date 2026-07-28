# BYO corpus JSON Schemas

One [Draft 2020-12](https://json-schema.org/) schema per served source type — **the source of
truth** for the JSONL record that `app/importer/byo.py` accepts:

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

Edit these files directly to change the accepted record shape. `app/validation.py`
loads them at runtime (keyed by each schema's `properties.source_type.const`), so a new source
type is just a new `*.schema.json` file here.

## Validate a corpus

```bash
python -m app.importer.byo path/to/corpus.jsonl --dry-run
```

Each JSONL line is dispatched to its `source_type` schema; problems are reported with a line
number and JSON path, and the exit code is non-zero on any failure (CI / pre-commit friendly).
`app.importer.byo` runs the same validation, so an invalid corpus never half-loads.

## Generating a dataset with an LLM

Hand the relevant service schema to a model as a structured-output / tool schema so generated
records conform to what the loader reads — then validate the output before loading.

Anthropic API (structured outputs), Python:

```python
import json
from pathlib import Path
import anthropic

schema = json.loads(Path("schemas/confluence.schema.json").read_text())
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
python -m app.importer.byo generated.jsonl --dry-run && python -m app.importer.byo generated.jsonl
```

## What the schemas enforce

- **Strict** — `source_type` (const), required `content` (+ `title` for every source except
  Slack), the `visibility` enum, per-service `subtype` enums (e.g. github `issue|pull_request`,
  drive `document|spreadsheet|presentation|pdf`, confluence `page|blogpost`, notion
  `page|database`), comment/reply object shapes, and `additionalProperties: false` (an unknown
  top-level key is almost always a typo). `comments` are allowed on jira/confluence/github/notion;
  `replies` only on slack.
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
