# Backlot

[![tests](https://github.com/brekkylab/backlot/actions/workflows/ci.yml/badge.svg)](https://github.com/brekkylab/backlot/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A **read-only** mock server that stands in for a whole stack of enterprise SaaS knowledge sources
at once.
It speaks each service's real read API — the exact response shapes, pagination schemes, auth,
and native permission endpoints their official SDKs expect — over a corpus **you** supply, so a
RAG/search connector built on those SDKs can be exercised **end-to-end** without the live
services.

## Quickstart

```bash
pip install backlot
```

That puts the `backlot` command on PATH. A server needs a corpus, and one is bundled with the
package — 136 documents covering every source it serves — so there is nothing to fetch or write:

```bash
backlot import --bundled    # the bundled corpus -> data/mock.sqlite + data/tokens.yaml
backlot serve               # http://127.0.0.1:8000
curl -s localhost:8000/health
```

Or skip the CLI entirely and let a test spin one up on a free port, serving that same corpus
(`pip install "backlot[examples]"` for the vendor SDKs the first snippet uses):

```python
import backlot
from slack_sdk import WebClient

with backlot.mock_server() as m:                       # no arguments: the bundled corpus
    slack = WebClient(token=m.token, base_url=f"{m.base_url}/slack/api/")
    print(slack.conversations_list()["channels"])

with backlot.mock_server(records=[                     # or your own records, inline
    {"source_type": "confluence", "space": "handbook", "title": "On-call",
     "content": "Page for Sev1 and Sev2 only.", "author_email": "ava@acme.com"},
]) as m:
    ...
```

`python -m backlot` is the same CLI as the `backlot` script, for when the venv is not activated.
Working on Backlot itself instead of with it? [CONTRIBUTING.md](CONTRIBUTING.md) covers the
from-source install.

### Docker

```bash
docker build -t backlot .                  # server + the bundled corpus baked in; no download
docker run -p 8000:8000 backlot
curl -s localhost:8000/health
```

That image answers every endpoint of every source out of the box. Use
`--target serve` for a server with **no** corpus, for a deployment that mounts its own
`/app/data`.

## Why you need this

Connectors are easy to write and hard to trust. Proving one works means an account with every
vendor, an OAuth app per vendor, seeded data in each, and rate limits between you and every retry —
so most of it
gets tested against fixtures that agree with your assumptions instead of with the API. Backlot is
the API: same shapes, same pagination, same permission endpoints, over documents you control.

It is the right tool when you are:

- **building or upgrading a connector** — crawl a source to exhaustion, then diff what you got
  against what you loaded, on a corpus small enough to reason about
- **testing ACL-scoped retrieval** — every document carries its own readers, and each user's token
  sees only theirs, so "does this leak" is a test rather than an audit
- **running that suite in CI** — no accounts, no secrets, no network, no flakes from someone else's
  outage; a server starts in a second and dies with the test
- **evaluating RAG or agents** — point an SDK, an MCP server, or a LlamaIndex reader at it and get
  the same answers on every run, because ids and timestamps are derived, not random
- **reproducing a bug you cannot reach** — a paginated edge case, an odd MIME type, an empty thread:
  write the document that causes it and serve it in seconds

It is **not** a sandbox for writes, a rate-limit or latency simulator, or a source of realistic
*content* — the documents are yours.

## Preparing a corpus

The server reads a corpus from `data/` (`mock.sqlite` + `tokens.yaml`). Build it from your own
documents, or load a public dataset.

You describe each document the way its own service would, and a per-source JSON Schema says what
that record may carry. `title` and `content` are served verbatim; so is every other field you set —
authors, timestamps, threads, comments, labels, states, ACLs — so no part of a response *has* to be
synthesized. What you leave out is filled in **deterministically**, each value hashed from the
stable key it belongs to (a document's `doc_id`, a container's name, an author's address), so ids
never move between calls or pages.

### Bring your own corpus

One JSONL document per line, validated against a per-service JSON Schema
([`backlot/schemas/`](backlot/schemas/)), then loaded:

```bash
backlot import mycorpus.jsonl              # validate + load -> data/
backlot import mycorpus.jsonl --dry-run    # validate only, no DB writes
backlot import mycorpus.jsonl --roster roster.yaml   # state the principals, don't derive them
backlot import corpus.jsonl.gz             # gzipped, read as a stream
backlot import artifact-dir/               # a sharded corpus + its manifest, digests verified
```

```json
{"source_type": "slack", "channel": "incidents", "author_email": "bob@acme.com", "content": "Anyone seeing 502s from the gateway?", "replies": [{"content": "Looking now.", "author_email": "ava@acme.com"}]}
{"source_type": "gmail", "mailbox": "ceo", "title": "Q1 board deck draft", "content": "Draft narrative for the Q1 board meeting.", "author_email": "ceo@acme.com", "to": "ava@acme.com", "readers": ["ceo@acme.com", "ava@acme.com"]}
```

Only `source_type` and `content` are required (`title` too, for every source except Slack). Where
to look next:

| To learn | Read |
|---|---|
| every field a record may carry | [`examples/bring-your-own-corpus/sample_corpus.jsonl`](examples/bring-your-own-corpus/sample_corpus.jsonl) — the field reference, and a test keeps it exhaustive |
| the rules each source imposes | [`backlot/schemas/README.md`](backlot/schemas/README.md) |
| import → serve → query, runnable | [`examples/bring-your-own-corpus/run.py`](examples/bring-your-own-corpus/run.py) |

The schemas double as the contract for **LLM dataset generation**: hand one to a model as a
structured-output schema, generate records, then `--dry-run` before loading. See
[`backlot/schemas/README.md`](backlot/schemas/README.md).

### Load a public dataset

[EnterpriseRAG-Bench](https://github.com/onyx-dot-app/EnterpriseRAG-Bench) is ~500k synthetic
enterprise documents across nine of the supported sources. One command downloads, loads and
ACL-derives it:

```bash
backlot import --type enterpriserag-bench    # -t erb for short
```

What that dataset does and does not carry, and how to redistribute it as BYO-JSONL, is in
[`examples/import-enterpriserag-bench/`](examples/import-enterpriserag-bench/) — it is one corpus
you can load, not part of this server's contract.

## Auth & tokens

Each service authenticates its own way, and the mock expects what the real one does: a bearer token
for most, HTTP Basic for Jira/Confluence, a **bare** `Authorization` value for Linear's personal API
keys, SigV4 for S3, and Google's OAuth token exchange for a connector carrying a client config.

You construct none of it. Two mock-only endpoints hand out every credential the corpus generated —
an admin/service token that bypasses the ACL (use it to crawl), plus one identity per person, each
seeing only what their own ACL permits:

```bash
curl -s localhost:8000/_mock/users
```
```json
{ "org": "acme", "admin_token": "admin-service-token", "count": 10,
  "admin_s3_access_key_id": "AKIA732S…", "admin_s3_secret_access_key": "l4sz5sXT…",
  "users": [{ "email": "ava.chen@acme.com", "name": "Ava Chen", "token": "usr-29b84da570…",
              "s3_access_key_id": "AKIADNLO…", "s3_secret_access_key": "0FdhlUUQ…",
              "groups": ["engineering", "handbook", "product", "design"] }] }
```

```bash
curl -s localhost:8000/_mock/credentials    # for a Google client that wants a config, not a token
```
```json
{ "org": "acme", "token_uri": "http://localhost:8000/oauth2/token",
  "oauth_client": { "client_id": "e8ae7a….apps.googleusercontent.com", "client_secret": "GOCSPX-…" },
  "service_account": { "type": "service_account", "client_email": "…", "private_key": "…" } }
```

Use a user's `token` and every API filters to that user, which is what makes per-user access a test
rather than an audit. `token_uri` points back at the mock, so a client library's own refresh lands
here and resolves to the same ACL — a user's refresh_token is just their bearer token. Both
endpoints serve credentials in the clear, so `BACKLOT_EXPOSE_TOKENS=false` closes them; the same
values are in `data/tokens.yaml`. Per-service detail:
[`examples/using-official-sdk/`](examples/using-official-sdk/).

## Example Usages

Every one of these points a real client at the mock's base URL — that is the only change from
talking to the live service.

### Official SDKs

```python
from slack_sdk import WebClient
WebClient(token=TOKEN, base_url="http://localhost:8000/slack/api/")

from github import Github, Auth
Github(auth=Auth.Token(TOKEN), base_url="http://localhost:8000/github")

from atlassian import Jira, Confluence
Jira(url="http://localhost:8000/atlassian", username="svc@x", password=TOKEN)
Confluence(url="http://localhost:8000/atlassian/wiki", username="svc@x", password=TOKEN)

from googleapiclient.discovery import build
from google.api_core.client_options import ClientOptions
from google.oauth2.credentials import Credentials
creds = Credentials(token=TOKEN)
build("gmail", "v1", credentials=creds, client_options=ClientOptions(api_endpoint="http://localhost:8000"))
build("drive", "v3", credentials=creds, client_options=ClientOptions(api_endpoint="http://localhost:8000/drive/v3"))

from notion_client import Client
Client(auth=TOKEN, base_url="http://localhost:8000/notion")   # SDK appends /v1/ itself

import boto3
from botocore.config import Config
boto3.client("s3", endpoint_url="http://localhost:8000/s3", aws_access_key_id=AK, aws_secret_access_key=SK,
             region_name="us-east-1", config=Config(s3={"addressing_style": "path"}))
```

A runnable, self-contained script per service is in [`examples/using-official-sdk/`](examples/using-official-sdk/).

### MCP

An MCP server pointed at the mock retrieves through it, ACL-scoped to whatever token it
authenticates with. Some vendors publish a server that takes a base URL — use it directly:

```python
# examples/using-mcp-with-agents/atlassian.py — the community-official mcp-atlassian, over Docker.
# The host must end in .atlassian.net for the server's Cloud detection, so alias it at the mock.
params = StdioServerParameters(command="docker", args=[
    "run", "-i", "--rm", "--add-host=mock.atlassian.net:host-gateway",
    "-e", "JIRA_URL=http://mock.atlassian.net:8000/atlassian",
    "-e", "CONFLUENCE_URL=http://mock.atlassian.net:8000/atlassian/wiki",
    "-e", "JIRA_USERNAME=svc@example.com", "-e", "CONFLUENCE_USERNAME=svc@example.com",
    "-e", f"JIRA_API_TOKEN={token}", "-e", f"CONFLUENCE_API_TOKEN={token}",  # a user token -> its ACL
    "-e", "MCP_ALLOWED_URL_DOMAINS=atlassian.net", "-e", "READ_ONLY_MODE=true",
    "ghcr.io/sooperset/mcp-atlassian:latest", "--transport", "stdio",
])
```

For the ones that don't — GitHub, Slack, Gmail, Drive, HubSpot — a generic **OpenAPI→MCP bridge**
turns the mock's own typed `/openapi.json` into tools instead (`GET /_mock/openapi/<source>` serves
the per-source slice):

```python
# examples/using-mcp-with-agents/github.py — no vendor SDK, no vendor MCP server
params = StdioServerParameters(command=sys.executable, args=[
    "examples/using-mcp-with-agents/_openapi_bridge.py",
    "--source", "github", "--base-url", mock.base_url, "--token", mock.token,
])
```

Either way an agent then calls `session.list_tools()` and retrieves. Runnable agents for both LLM backends (Anthropic + OpenAI), one file per service, are in [`examples/using-mcp-with-agents/`](examples/using-mcp-with-agents/).

### LlamaIndex readers

Point official [LlamaIndex readers](https://docs.llamaindex.ai/en/stable/module_guides/loading/connector/)
(`llama-index-readers-*`) at the mock and load an enterprise corpus as `Document` objects — the
first step of a LlamaIndex ingestion/RAG pipeline.

```python
from llama_index.readers.github import GitHubIssuesClient
GitHubIssuesClient(github_token=TOKEN, base_url="http://localhost:8000/github")

from llama_index.readers.confluence import ConfluenceReader
ConfluenceReader(base_url="http://localhost:8000/atlassian/wiki", cloud=False, api_token=TOKEN)
```

One runnable script per source is in [`examples/using-llamaindex-readers/`](examples/using-llamaindex-readers/).


### Mirage

[mirage](https://github.com/strukto-ai/mirage) mounts a SaaS backend as a **virtual
filesystem** an agent reads with shell commands (`ls`, `cat`, `grep`, `find`). Point its resources at the mock and you can drive a mirage agent over your corpus offline.

```python
from mirage import MountMode, Workspace
from mirage.resource.slack import SlackConfig, SlackResource

resource = SlackResource(SlackConfig(token=TOKEN, base_url="http://localhost:8000/slack/api"))
ws = Workspace({"/slack": resource}, mode=MountMode.READ)
await ws.execute("ls /slack/channels/")        # then cat a channel's dated chat.jsonl
```

One runnable script per source plus a `unified.py` that greps
across Slack/Gmail/Google Drive at once are in [`examples/using-mirage/`](examples/using-mirage/).


## Endpoints (read-only)

| Prefix | Service | Endpoints |
|---|---|---|
| `/slack/api` | Slack | `conversations.list` (+`types`; this corpus has no DMs, so `im`/`mpim` select nothing, and an unknown value is `invalid_types`), `conversations.history` (+`oldest`/`latest`/`inclusive`), `conversations.replies`, `conversations.members` (per-channel, paginated), `users.list`, `users.info`, `auth.test`, `api.test` (auth-free connectivity check), `search.messages` |
| `/gmail/v1` | Gmail | `users/{u}/messages` (+`q`: free text / `from:` `to:` `subject:` `after:` `before:` `newer_than:` `older_than:` `label:` `has:attachment`), `messages/{id}` (`format=full\|metadata\|minimal`), `messages/{id}/attachments/{id}`, `threads` (+`q`), `threads/{id}`, `labels`, `profile`. Message and thread ids are Gmail-shaped — 16 lowercase hex under 2^63, sharing one id space as the real API does — and map back to the corpus document; an id the real API could not parse is refused the same way |
| `/drive/v3` | Drive | `files` (`q`: `fullText contains`, `name contains`, `mimeType`, `… in parents` incl. `'root'`, `trashed`, `modifiedTime`, `sharedWithMe`, `… in owners`; `orderBy`: `name`/`name_natural`/`createdTime`/`modifiedTime`/`recency`/`folder`/`starred`/`quotaBytesUsed`/`sharedWithMeTime` (+` desc`); `fields` projection, validated), `files/{id}` (+`fields`), `files/{id}/export`, `files/{id}/permissions`, `drives`, `about` (`fields` **required**, as in real Drive; `storageQuota` is measured from the caller's visible corpus). Folders are files here: they match `mimeType='…folder'`, project, sort and resolve permissions like stored rows. Trashed files are excluded unless `trashed = true` asks for them |
| `/docs/v1`, `/sheets/v4`, `/slides/v1` | Docs/Sheets/Slides | `documents/{id}`, `spreadsheets/{id}`, `presentations/{id}` — native-doc content for editor-aware clients (read structurally instead of via Drive export). `spreadsheets/{id}` returns structure only — cells need `includeGridData=true` (+ optional `ranges`), as in real Sheets. Sheets also serves `spreadsheets/{id}/values/{range}` and `spreadsheets/{id}/values:batchGet` (A1 ranges incl. `Sheet1!A1:B2`, `A:A`, `1:3`, `A2:B`, a bare sheet name quoted or not; `majorDimension`, `valueRenderOption`). A spreadsheet row is one stored **line**, held in a single cell verbatim — the mock picks no column delimiter, so splitting (CSV, pipes, …) stays the corpus owner's decision. Reading a file of the wrong type through any of the three APIs is refused, as real Google does, not reinterpreted |
| `/github` | GitHub | `search/issues` (`q`: free text + `repo:` `is:` `state:` `type:` `label:` `author:`), `orgs/{org}`, `orgs/{org}/repos`, `user/repos` (the token's own reach), `repos/{o}/{r}`, `.../issues[/{n}]`, `.../issues/{n}/comments`, `.../pulls[/{n}]`, `.../pulls/{n}/reviews`, `.../pulls/{n}/comments`, `.../pulls/{n}/files`, `.../issues/comments/{id}`, `.../pulls/comments/{id}`, `.../readme`, `.../contents[/{path}]`, `.../git/trees/{ref}`, `.../git/blobs/{sha}`, `.../git/ref/{ref}` (takes the ref as a trailing path, so `heads/release/2026-03` resolves), `.../branches/{branch}`, `.../commits/{sha}`, `.../collaborators`, `.../teams`, `orgs/{org}/teams`. **Media types are honoured**: `Accept: application/vnd.github.raw` on `contents`/`readme`/`git/blobs` returns the file's bytes, and `…diff`/`…patch` on a pull returns a real unified diff / `git am` mbox. The `{owner}` segment is validated against the served org and 404s otherwise, as GitHub does. A pull's changed-file list comes from its corpus `changed_paths` when declared and is chosen deterministically otherwise; either way the hunks are derived from each file's own snapshot, so the diff applies with real `git` and `additions`/`deletions`/`changed_files` agree with `/files`. A comment carrying a `path` is served as a line-anchored review comment, kept apart from the conversation as GitHub keeps them |
| `/atlassian/rest/api/3` | Jira | `search/jql` (JQL `project =`, `text\|summary\|description ~`), `issue/{key}`, `issue/{key}/comment`, `field`, `issueLinkType`, `project/search`, `project/{key}/role[/{id}]`, `serverInfo` (also under `rest/api/2`) |
| `/atlassian/wiki/rest/api` | Confluence | `content`, `content/{id}`, `content/{id}/child/comment`, `content/{id}/restriction/byOperation`, `search` (CQL), `space`, `space/{key}`, `space/{key}/permission` |
| `/notion/v1` | Notion | `search`, `pages/{id}`, `blocks/{id}`, `blocks/{id}/children`, `databases/{id}` (version-aware), `data_sources/{id}`, `data_sources/{id}/query`, `databases/{id}/query` (legacy), `users[/{id}]`, `users/me`, `comments` |
| `/hubspot/crm/v3`, `/hubspot/crm/v4` | HubSpot | `objects/{objectType}` (+`limit` max 100, `after`, `properties`, `archived`), `objects/{objectType}/{id}`, `objects/{objectType}/search` (`filterGroups` OR-ed, `filters` AND-ed, 13 operators over any property), `objects/{objectType}/batch/read`, `v4/objects/{type}/{id}/associations/{toType}` |
| `/s3` | Amazon S3 | `ListBuckets`, `HeadBucket`, `GetBucketLocation`, `ListObjectsV2` (`prefix`/`delimiter`/`continuation-token`), `GetObject` (+`Range`), `HeadObject` |
| `/linear/graphql` | Linear | **GraphQL only** (one `POST`): `issues`, `issue(id:)` (UUID *or* `ENG-123`), `team(id:)` (UUID, key, or name), `teams`, `comments`, `users`, `viewer`, plus the `Team.issues` / `Issue.{comments,labels,children,relations,inverseRelations,attachments,releases}` connections and the by-id roots (`user`, `workflowState`, `project`, `issueLabel`, `cycle`, `release`, `attachment`, `issueRelation`) the official SDK's lazy relation accessors call. Relay pagination (`first`/`after`, `last`/`before` → `{nodes, pageInfo}`), server-side `filter` compiled into SQL, and full introspection |
| `/fireflies/graphql` | Fireflies | **GraphQL only** (one `POST`): `transcripts`, `transcript(id:)`, `user[(id:)]`, `users`. Offset pagination — `limit` (**max 50**, clamped) / `skip`, returning a **bare list**, not a Relay connection — plus the documented filters: `keyword` × `scope` (`title`\|`sentences`\|`all`), `fromDate`/`toDate`, `host_email`, `organizers`, `participants`, `user_id`, `mine`, `channel_id`. Field names are snake_case, as Fireflies' own schema has them. Full introspection |

Mock-only endpoints: `/health`, `/_mock/users`, `/_mock/credentials`, `/_mock/openapi/<source>`,
`/openapi.json`.

## Tests

```bash
pytest              # unit (synth/pagination/acl/schema/importer parsers) + HTTP endpoint tests
                    # (full-crawl completeness, content round-trip, ACL enforcement)
ruff check . && ruff format --check .
```

## Configuration

Every setting is an env var with a `BACKLOT_` prefix, and a `.env` file in the working directory is
read too. Defaults are what the server uses when the var is unset.

| Env var | Default | What it does |
|---|---|---|
| `BACKLOT_DATA_DIR` | `./data` (resolved against the cwd, **not** the install location) | Where the corpus lives: `mock.sqlite`, `tokens.yaml`, `credentials.yaml`. Both `backlot import` and `backlot serve` read it, which is how you keep several corpora side by side — `BACKLOT_DATA_DIR=/tmp/demo backlot import c.jsonl` |
| `BACKLOT_ADMIN_TOKEN` | `admin-service-token` | The token that bypasses ACL filtering — a full-crawl / service identity. Set it to anything for a shared deployment |
| `BACKLOT_ENFORCE_ACL` | `true` | When `false`, any well-formed token is treated as admin. The ACL is still *exposed* through each vendor's permission endpoints, just not *enforced* — useful for isolating whether a connector's gaps are permissions or parsing |
| `BACKLOT_EXPOSE_TOKENS` | `true` | Serves `GET /_mock/users` and `GET /_mock/credentials`, which hand out every user's token in the clear. Fine for a local mock; set `false` to close both |
| `BACKLOT_ORG_NAME` | inferred from the corpus (fallback `example`) | The org slug that shows up in `auth.test`, synthesized emails and self-URLs. Inferred from the dominant author email domain — `@acme.com` documents serve as org `acme` — so set it only to override that |
| `BACKLOT_ORG_DOMAIN` | inferred from the corpus (fallback `example.com`) | The domain half of the same inference, e.g. `acme.com`. Used for addresses the corpus does not state |
| `BACKLOT_DEFAULT_PAGE_SIZE` | `100` | Page size when a request names none |
| `BACKLOT_MAX_PAGE_SIZE` | `1000` | Ceiling a request may ask for. Per-vendor caps still win where the real API has one (Fireflies clamps to 50, HubSpot to 100) |
| `BACKLOT_SQLITE_MMAP_MB` | `256` | Memory-maps the DB so reads come from the OS page cache instead of a syscall each — the main lever against a slow first request after idle. SQLite maps `min(this, db size)`; raise it to at or above your DB size to map a big corpus fully |
| `BACKLOT_SQLITE_CACHE_MB` | `64` | SQLite's own page cache, per serving connection |
| `BACKLOT_SQLITE_BUSY_MS` | `5000` | How long a read waits for a lock instead of erroring, so reads ride through an out-of-band write (e.g. an in-place FTS rebuild) rather than 500ing |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: fidelity to the real APIs is the point,
so a divergence is a bug — measure against the real service, and bring a test that fails without
your fix.

## License

[MIT](LICENSE)
