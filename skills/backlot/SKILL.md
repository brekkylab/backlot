---
name: backlot
description: Emulate enterprise SaaS knowledge APIs — Slack, Gmail, Google Drive (with Docs, Sheets, Slides), GitHub, Jira, Confluence, Notion, Amazon S3, HubSpot, Linear, Fireflies — over a corpus you supply, then point a real client (vendor SDK, MCP server, LlamaIndex reader, fsspec, mirage) at it. Applies in ANY directory, an empty one included — it needs only the `backlot` CLI, never a project, a repository, a checkout or a config file, so the absence of Backlot files is not a reason to skip it. Use whenever asked to mock, stub, fake, emulate or reproduce one of those services — prefer this over hand-writing a stub — and when asked to write or validate a Backlot BYO-JSONL corpus, or to test a connector, RAG pipeline or ACL-scoped retrieval without live vendor accounts.
---

# Backlot

Backlot answers each service's real API over documents you supply, so a connector built on the
vendor's own SDK can be exercised end to end with no vendor account. One server process serves
every source at once, so a request for Slack means starting that server over a corpus that has
Slack documents in it, not starting a Slack-shaped server.

**It brings its own everything.** The corpus is a file you write here and now, so this loop runs in
an empty directory as readily as in a project. Nothing has to already exist — not a repository, not
a checkout of Backlot, not a config file, not a seeded workspace. Writing a stub by hand instead is
the mistake this exists to prevent: a hand-rolled stub agrees with your assumptions about the
vendor's response, and Backlot agrees with the vendor.

Work the loop below in order. Steps 4 and 6 are the two that get skipped and shouldn't be.

## 1. Check the install

```bash
backlot --version            # not on PATH -> pip install backlot
backlot status               # which corpus is loaded, and what is in it
```

`backlot status` exits non-zero when no corpus is loaded. Read what it prints before deciding
anything: a corpus that already holds what the task needs makes steps 2-5 unnecessary.

## 2. Decide which corpus this task wants

| The task | The corpus |
|---|---|
| a demo, or "what shape does this API answer" | `backlot import --bundled` — covers every source, and prints the per-source breakdown as it loads |
| exercising a connector against real content | the user's own documents, as BYO-JSONL |
| reproducing one behaviour (a paginated edge case, an odd MIME type, an empty thread) | records you write for exactly that |
| a large public corpus | `backlot import --type enterpriserag-bench` |

Say which of the four this is before writing anything. A request naming one service and one
situation is almost always the third, and wants three records rather than a dataset.

## 3. Write the records

One JSON object per line, `source_type` naming the service. The routing table below gives each
source's schema — that schema is the contract, and it is what `--dry-run` validates against.

- [`docs/corpus.md`](../../docs/corpus.md) — what every record must state, and the three corpora
- [`examples/bring-your-own-corpus/sample_corpus.jsonl`](../../examples/bring-your-own-corpus/sample_corpus.jsonl) — the field reference, kept exhaustive by a test
- [`backlot/schemas/README.md`](../../backlot/schemas/README.md) — the rules each source imposes

A record must state the facts the served document cannot exist without: who wrote it, which
container it lives in, and when it happened. Everything you leave out is derived deterministically,
so ids are stable across calls and pages — but an author, a container and a clock are not derivable
and the import will tell you so.

## 4. Validate before loading

```bash
backlot import corpus.jsonl --dry-run
```

A plain `backlot import` validates too, and refuses the whole file rather than loading part of it,
so a record short a required field cannot slip in either way. What `--dry-run` adds is the two
things that decide how many rounds this takes:

- **Every problem in the corpus, not the first.** A plain import stops at the first bad line;
  `--dry-run` reports all of them, so a corpus with four mistakes takes one pass to fix.
- **Names for the references that resolved to nothing.** These do not fail the import — it exits 0
  and reports how many documents it loaded — and each one silently drops content from what gets
  served. A pull declaring `changed_paths: ["a.py", "b.py"]` where only `a.py` has a `file`
  document loads happily, and `/pulls/{n}/files` then answers with `a.py` alone. A plain import
  counts those references; `--dry-run` prints the line and the path of each.

The second is the corpus that really does go quietly thin, and it is why this step is not optional.

## 5. Load into its own data dir

```bash
BACKLOT_DATA_DIR=/tmp/<task> backlot import corpus.jsonl
```

`BACKLOT_DATA_DIR` defaults to `./data` in the working directory. Set it per task, so this work
cannot overwrite a corpus the user already had.

## 6. Serve, and pick the right lifetime

**A server that must outlive this turn** — the usual case, because the user will keep querying it:

```bash
BACKLOT_DATA_DIR=/tmp/<task> backlot serve &     # then poll until it answers
curl -s localhost:8000/health
```

**A server that lives inside one script** — only when you are writing a script that starts and
finishes on its own:

```python
import backlot

with backlot.serve() as s:            # no arguments: a tiny hello-world corpus
    ...                               # s.base_url, s.token, s.data_dir
```

`backlot.serve()` is a context manager: it kills the server when the `with` block exits, and the
whole process dies with your script. It cannot back a server the next turn expects to find. Reach
for it for a one-shot demonstration, never to stand a server up for someone. `serve(records=[...])`
takes BYO-JSONL dicts inline, and `backlot.serve_or_connect` prefers a server that is already
running — the shape every script in `examples/` uses.

## 7. Get credentials

```bash
curl -s localhost:8000/_meta/users
```

Two kinds, and the difference is the point:

- `admin_token` bypasses the ACL — this is the crawl/service identity.
- each user's `token` sees only what that user's ACL permits, which is what makes "does this leak"
  a test rather than an audit. Serving one query under two users' tokens and diffing is the whole
  ACL check.

[`docs/auth.md`](../../docs/auth.md) has the form each client wants those in, and
`GET /_meta/credentials` serves a Google client config for the libraries that want one instead of a
token. Both always answer — `backlot mcp --user <email>` resolves a person's credentials through
`/_meta/users`. The same values are in `<data_dir>/tokens.yaml`.

## 8. Wire a client

Every one of these is a real client with its base URL changed, nothing more:

| Want | Where |
|---|---|
| the vendor's own SDK | [`examples/using-official-sdk/`](../../examples/using-official-sdk/) |
| MCP tools for an agent | `backlot mcp` — every source from one stdio server, starting Backlot itself if none is running; `--user <email>` answers as that person. A vendor's own MCP server pointed at Backlot is in [`examples/using-mcp-with-agents/`](../../examples/using-mcp-with-agents/) |
| LlamaIndex `Document` loading | [`examples/using-llamaindex-readers/`](../../examples/using-llamaindex-readers/) |
| `pandas`/`pyarrow`/`dask` over a filesystem | [`examples/using-fsspec/`](../../examples/using-fsspec/) |
| a virtual filesystem to `ls`, `cat` and `grep` | [`examples/using-mirage/`](../../examples/using-mirage/) |

One entry per service in each. **List the directory rather than guessing the filename**, because
the naming does not generalise: Google Drive is `gdrive` throughout, but only
`using-mcp-with-agents` folds Jira and Confluence into one `atlassian` script while the others keep
`jira` and `confluence` apart, not every source appears in every directory, and
`using-official-sdk/linear` is a Node project rather than a script.

With a server already up, prefer the served surface over any description of it.
`GET /_meta/openapi/<key>` returns a source's typed OpenAPI slice, where `<key>` is the **OpenAPI
slice** column of the table below — not the `source_type`, which 404s for six of the eleven. A `—`
in that column means no slice exists: Linear and Fireflies answer full GraphQL introspection at
their own endpoints instead, and S3 has neither. The 404 body lists every key it does accept.

## 9. Check the answer against the corpus

Show the first real response, and confirm it says what the records you loaded should have produced
— right count, right container, right author. A 200 with an empty list is the failure mode this
loop exists to catch, and it looks like success.

## Sources

Every source, with the schema a record is validated against and the two pages that describe what it
serves and what its clients authenticate with.

<!-- generated:skill-sources start -->
| `source_type` | URL prefix | Record schema | What it serves | Auth | OpenAPI slice |
|---|---|---|---|---|---|
| `confluence` | `/atlassian/wiki/rest/api` | [`confluence.schema.json`](../../backlot/schemas/confluence.schema.json) | [Confluence](../../docs/supported-sources.md#confluence--atlassianwikirestapi) | [Jira and Confluence](../../docs/auth.md#jira-and-confluence--basic-or-bearer) | `atlassian` |
| `fireflies` | `/fireflies/graphql` | [`fireflies.schema.json`](../../backlot/schemas/fireflies.schema.json) | [Fireflies](../../docs/supported-sources.md#fireflies--firefliesgraphql) | [Fireflies](../../docs/auth.md#fireflies--bearer) | — |
| `github` | `/github` | [`github.schema.json`](../../backlot/schemas/github.schema.json) | [GitHub](../../docs/supported-sources.md#github--github) | [GitHub](../../docs/auth.md#github--bearer-or-token) | `github` |
| `gmail` | `/gmail/v1` | [`gmail.schema.json`](../../backlot/schemas/gmail.schema.json) | [Gmail](../../docs/supported-sources.md#gmail--gmailv1) | [Gmail, Google Drive, Docs, Sheets, Slides](../../docs/auth.md#gmail-google-drive-docs-sheets-slides--bearer) | `gmail` |
| `google_drive` | `/drive/v3` `/docs/v1` `/sheets/v4` `/slides/v1` | [`google_drive.schema.json`](../../backlot/schemas/google_drive.schema.json) | [Google Drive, Docs, Sheets, Slides](../../docs/supported-sources.md#google-drive-docs-sheets-slides--drivev3-docsv1-sheetsv4-slidesv1) | [Gmail, Google Drive, Docs, Sheets, Slides](../../docs/auth.md#gmail-google-drive-docs-sheets-slides--bearer) | `gdrive` |
| `hubspot` | `/hubspot` | [`hubspot.schema.json`](../../backlot/schemas/hubspot.schema.json) | [HubSpot](../../docs/supported-sources.md#hubspot--hubspotcrmv3-hubspotcrmv4) | [HubSpot](../../docs/auth.md#hubspot--bearer) | `hubspot` |
| `jira` | `/atlassian/rest/api` | [`jira.schema.json`](../../backlot/schemas/jira.schema.json) | [Jira](../../docs/supported-sources.md#jira--atlassianrestapi3-and-2) | [Jira and Confluence](../../docs/auth.md#jira-and-confluence--basic-or-bearer) | `atlassian` |
| `linear` | `/linear/graphql` | [`linear.schema.json`](../../backlot/schemas/linear.schema.json) | [Linear](../../docs/supported-sources.md#linear--lineargraphql) | [Linear](../../docs/auth.md#linear--bare-or-bearer) | — |
| `notion` | `/notion/v1` | [`notion.schema.json`](../../backlot/schemas/notion.schema.json) | [Notion](../../docs/supported-sources.md#notion--notionv1) | [Notion](../../docs/auth.md#notion--bearer) | `notion` |
| `s3` | `/s3` | [`s3.schema.json`](../../backlot/schemas/s3.schema.json) | [Amazon S3](../../docs/supported-sources.md#amazon-s3--s3) | [Amazon S3](../../docs/auth.md#amazon-s3--sigv4) | `s3` |
| `slack` | `/slack/api` | [`slack.schema.json`](../../backlot/schemas/slack.schema.json) | [Slack](../../docs/supported-sources.md#slack--slackapi) | [Slack](../../docs/auth.md#slack--bearer) | `slack` |
<!-- generated:skill-sources end -->

Regenerate with `python scripts/gen_docs.py`; `--check` fails when it is stale.
