# Enterprise Mock

> **LocalStack for enterprise SaaS knowledge APIs.** Point your RAG/search connectors at
> read-only mock **Slack, Gmail, Google Drive, GitHub, Jira, Confluence, Notion, Amazon S3,
> HubSpot, Linear, and Fireflies**
> APIs — real response shapes, real pagination, real per-document ACLs — entirely offline: no
> accounts, no OAuth, no rate limits.

[![tests](https://github.com/brekkylab/enterprise-mock/actions/workflows/ci.yml/badge.svg)](https://github.com/brekkylab/enterprise-mock/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A **read-only** mock server that stands in for ten enterprise SaaS knowledge sources at once.
It speaks each service's real read API — the exact response shapes, pagination schemes, auth,
and native permission endpoints their official SDKs expect — over a corpus **you** supply, so a
RAG/search connector built on those SDKs can be exercised **end-to-end** without the live
services.

## Quickstart (Docker)

```bash
docker build -t enterprise-mock .          # bakes a small corpus + ACLs into the image
docker run -p 8000:8000 enterprise-mock
curl -s localhost:8000/health
```

The image ships with a small corpus and generated ACLs already built in (no accounts, no data
download at runtime), so it's ready to crawl immediately.

## Why this exists

Testing a knowledge connector end-to-end normally needs live SaaS accounts, OAuth, seeded data,
and patience for rate limits. This server removes all of that: it serves whatever documents you
give it through the services' real read APIs, offline and deterministically.

You provide each document as `{title, content}` (plus optional structure). The server serves
`title` + `content` **verbatim** and **deterministically synthesizes** everything else a real
API response needs — ids, timestamps, users, channels/repos/spaces, keys, pagination cursors —
from `sha256(doc_id)`, so responses are stable and self-consistent across calls and paginated
fetches. It also generates a synthetic **org → group → user ACL** and both **exposes** it
(native permission endpoints per service) and **enforces** it (responses are filtered to the
calling user; an admin/service token sees everything).

## Setup (from source)

```bash
uv venv && source .venv/bin/activate     # or: python -m venv .venv
uv pip install -e ".[dev]"
```

Then prepare a corpus (below) and start the server:

```bash
python -m uvicorn backlot.main:app --port 8000
curl -s localhost:8000/health
```

## Preparing data

The server reads a corpus from `data/` (`mock.sqlite` + `tokens.yaml`). Build it either way:

### Import from EnterpriseRAG-Bench

[EnterpriseRAG-Bench](https://github.com/onyx-dot-app/EnterpriseRAG-Bench) ships ~500k
synthetic enterprise documents (flattened to `{doc_id, source_type, title, content}`). One
command downloads a slice, loads it, and generates the ACL:

```bash
python -m backlot.importer.erb     # small slice; --all for the full corpus, --augment for +α
```

The bench carries only `{title, content}` — no structure, no access control — so the mock
synthesizes the structural metadata and generates the ACL. Every import also **parses the real
conversations embedded in the content** (this is faithful representation, not synthesis, so it's
always on): Slack transcripts → threads, GitHub PR reviews and Jira comments → real comments,
Gmail threads → per-email messages. `--augment` then layers only the genuinely-absent,
*synthesized* structure on top: doc types, issue/PR split, status/labels, hierarchy, reactions.
A runnable walkthrough (import → serve → query) is in
[`examples/import-enterpriserag-bench/`](examples/import-enterpriserag-bench/).

### Bring your own corpus

Serve **any** document set: one JSONL document per line, validated against a per-service JSON
Schema (`backlot/schemas/`), then loaded.

```bash
python -m backlot.importer.byo mycorpus.jsonl              # validate + load -> data/
python -m backlot.importer.byo mycorpus.jsonl --dry-run    # validate only, no DB writes
python -m backlot.importer.byo mycorpus.jsonl --roster roster.yaml   # state the principals, don't derive them
python -m backlot.importer.byo corpus.jsonl.gz             # gzipped, read as a stream
python -m backlot.importer.byo artifact-dir/               # a sharded corpus + its manifest (below)
```

```json
{"source_type": "slack", "channel": "incidents", "author_email": "bob@acme.com", "content": "Anyone seeing 502s from the gateway?", "replies": [{"content": "Looking now.", "author_email": "ava@acme.com"}]}
{"source_type": "gmail", "mailbox": "ceo", "title": "Q1 board deck draft", "content": "Draft narrative for the Q1 board meeting.", "author_email": "ceo@acme.com", "to": "ava@acme.com", "readers": ["ceo@acme.com", "ava@acme.com"]}
```

The record format (fields, ACL, Slack/Gmail threads), a runnable walkthrough (`run.py`), and a
sample corpus are in [`examples/bring-your-own-corpus/`](examples/bring-your-own-corpus/); the
schemas are in [`schemas/README.md`](backlot/schemas/README.md).

The schema is expressive enough to hold an **entire existing dataset losslessly**, which is how the
bench is redistributed in it:

```bash
python -m backlot.importer.erb --export-byo out/     # ERB -> out/corpus.jsonl + out/roster.yaml
python -m backlot.importer.byo out/corpus.jsonl --roster out/roster.yaml
```

That produces a database *equivalent* to importing the bench directly — same rows, same column
values, same `doc_acl`, same `tokens.yaml`, asserted as a table-by-table diff in
`tests/test_importer_erb.py`. `roster.yaml` carries what the records cannot: display names (an
address does not round-trip a name) and which people are real accounts rather than just document
owners.

At bench scale one file is unwieldy — the whole of ERB is 581,294 records — so the export can shard:

```bash
python -m backlot.importer.erb --export-byo out/ --shard-records 50000
```

Each source becomes `out/data/<source>/part-NNNNN.jsonl.gz` alongside `out/manifest.json`, which
records every shard's path, record count, byte size, and SHA-256, plus the same for `roster.yaml`.
`python -m backlot.importer.byo out/` loads the whole thing in one command and checks every digest before
reading a record, so a damaged or swapped download fails up front instead of half-loading a database.
The roster is checked with the shards, since importing a directory picks it up automatically and it
decides who holds a token. Shards are gzipped with `mtime=0`, so the same input always produces the
same checksums.

A shard that is short but validly terminated — what a resumed or re-uploaded download looks like — is
the case this catches that nothing else would: the gzip stream reads cleanly to its end, so only the
digest tells you records are missing.

## Auth & tokens

`data/tokens.yaml` holds one bearer token per user plus an **admin/service token**
(`BACKLOT_ADMIN_TOKEN`, default `admin-service-token`). The admin token bypasses ACL filtering
(use it for a full crawl); a user token sees only documents that user's ACL permits.

- Slack: `Authorization: Bearer <token>` (also accepts `?token=` / form `token`)
- Gmail / Drive / GitHub / Notion / HubSpot / Fireflies: `Authorization: Bearer <token>`
- Linear: `Authorization: <token>` — the **bare** token, no `Bearer` prefix, which is how Linear
  carries a personal API key. `Authorization: Bearer <token>` is accepted too (Linear's OAuth
  shape); anything else, including a stray scheme like `Token <t>`, is a 401 rather than being
  quietly stripped — to the real API the whole header value *is* the key
- Jira / Confluence: HTTP Basic `email:<token>` (the token is the password)
- S3: AWS SigV4 — not the bearer token; use the `s3_access_key_id`/`s3_secret_access_key` pair from `GET /_mock/users` (derived from the token; per-user and an admin pair). See `examples/using-official-sdk/s3.py`

To discover the tokens without opening `data/tokens.yaml`, hit **`GET /_mock/users`** — a
mock-only directory of every user (email, name, token, groups) plus the `admin_token`. Pick a
token, use it against any of these APIs, and you get that user's ACL-filtered view — the easy way
to test per-user access. It hands out tokens in the clear (fine for a local test mock); disable
with `BACKLOT_EXPOSE_TOKENS=false`.

```bash
curl -s localhost:8000/_mock/users | jq '.users[0]'
# { "email": "ava@…", "name": "Ava Ng", "token": "usr-…", "groups": ["engineering"] }
```

### OAuth client config (Google-style)

Real Gmail/Drive connectors usually carry an OAuth **client config** — an `authorized_user`
bundle (client_id/secret + refresh_token) or a **service account** key that signs a JWT to
impersonate a user — rather than a raw access token. The mock supports that flow so those
connectors run unmodified: **`GET /_mock/credentials`** returns just the **shared** credentials —
the single `oauth_client` (client_id/secret) and the org `service_account` JSON. There's no
per-user data: a user's **refresh_token is simply their bearer token from `/_mock/users`**.
**`POST /oauth2/token`** honors the `refresh_token` and JWT-bearer (`sub` = impersonated user)
grants — returning that user's bearer token, so ACL enforcement is identical. `token_uri` points
back at the mock, so the client library's own refresh call lands here. A bare service account
(no `subject`) resolves to the admin/service token (a full-crawl identity). Same
`BACKLOT_EXPOSE_TOKENS` gate as `/_mock/users`. The Gmail/Drive SDK examples
([`gmail.py`](examples/using-official-sdk/gmail.py),
[`gdrive.py`](examples/using-official-sdk/gdrive.py)) authenticate this way.

```python
oc = requests.get(f"{BASE}/_mock/credentials").json()["oauth_client"]  # one shared client
rt = requests.get(f"{BASE}/_mock/users").json()["users"][0]["token"]   # a user's token = refresh_token
Credentials(None, refresh_token=rt, token_uri=f"{BASE}/oauth2/token",
            client_id=oc["client_id"], client_secret=oc["client_secret"])   # refreshes against the mock
```

## Using official SDKs with the mock

Point any official SDK at the mock's base URL — the only change from talking to the real
service:

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

## Using MCP with the mock

Point an MCP server at the mock's base URL and an agent retrieves through it — the mock enforces
the ACL for whatever token the MCP server authenticates with. Three servers are wired up in the
examples: the community-official [`mcp-atlassian`](https://github.com/sooperset/mcp-atlassian)
(Jira + Confluence, over Docker), the **official**
[`@notionhq/notion-mcp-server`](https://github.com/makenotion/notion-mcp-server) (Notion, over
`npx` — it takes a first-class `BASE_URL` override: `BASE_URL=http://localhost:8000/notion`), and
the **official** [`awslabs.aws-api-mcp-server`](https://github.com/awslabs/mcp/tree/main/src/aws-api-mcp-server)
(S3, over `uvx` — it shells the AWS CLI, whose boto3 client honors a first-class
`AWS_ENDPOINT_URL` override: `AWS_ENDPOINT_URL=http://localhost:8000/s3`). Sources with no
base-URL-switchable vendor server — GitHub, Slack, Gmail, Drive and HubSpot — go through a generic
**OpenAPI→MCP bridge** that turns the mock's own typed `/openapi.json` into MCP tools
(`GET /_mock/openapi/<source>` serves the per-source slice).
For example, connecting `mcp-atlassian` over stdio:

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(command="docker", args=[
    "run", "-i", "--rm", "--add-host=mock.atlassian.net:host-gateway",
    "-e", "MCP_ALLOWED_URL_DOMAINS=atlassian.net",
    "-e", "JIRA_URL=http://mock.atlassian.net:8000/atlassian",
    "-e", "JIRA_USERNAME=svc@x",
    "-e", "JIRA_API_TOKEN=<token from data/tokens.yaml>",   # resolved to a user; ACL enforced
    "ghcr.io/sooperset/mcp-atlassian", "--transport", "stdio",
])
async with stdio_client(params) as (reader, writer):
    async with ClientSession(reader, writer) as session:
        await session.initialize()
        tools = await session.list_tools()   # your agent calls these; they hit the mock
```

Runnable agents (Anthropic + OpenAI) and setup notes are in [`examples/using-mcp-with-agents/`](examples/using-mcp-with-agents/).

## Using mirage with the mock

[mirage](https://github.com/strukto-ai/mirage) mounts a SaaS backend as a **virtual
filesystem** an agent reads with bash (`ls`, `cat`, `grep`, `find`). Point its
Slack/Gmail/Drive/Notion/S3 resources at the mock and you can drive a mirage agent over your
corpus offline. Slack, Notion, and S3 expose `base_url`/`endpoint_url` config fields (point them
straight at the mock — S3's `S3Config` also takes `path_style=True`); Google hardcodes
`googleapis.com`, so a one-line helper redirects those constants at the mock:

```python
from mirage import MountMode, Workspace
from mirage.resource.slack import SlackConfig, SlackResource
from _mirage import point_mirage_at            # examples/using-mirage/_mirage.py

point_mirage_at("http://localhost:8000")       # slack.com / googleapis.com  ->  the mock
ws = Workspace({"/slack": SlackResource(SlackConfig(token=TOKEN))}, mode=MountMode.READ)
await ws.execute("ls /slack/channels/")         # then cat a channel's dated chat.jsonl
```

One runnable script per provider (Slack, Gmail, Drive, Notion, S3) plus a `unified.py` that greps
across Slack/Gmail/Drive at once are in [`examples/using-mirage/`](examples/using-mirage/); add `--fuse` to expose a
mount as a real OS filesystem (macFUSE/fuse3) that any tool can `cat`/`grep`. (Jira/Confluence
and GitHub are out of scope — mirage has no Jira/Confluence connector, and its GitHub connector
mirrors a repo's source-file tree rather than the issues/PRs the mock serves.)

## Using LlamaIndex readers with the mock

Point official [LlamaIndex readers](https://docs.llamaindex.ai/en/stable/module_guides/loading/connector/)
(`llama-index-readers-*`) at the mock and load an enterprise corpus as `Document` objects — the
first step of a LlamaIndex ingestion/RAG pipeline. GitHub, S3, Confluence, and Jira readers take a
host override directly; Slack, Notion, Gmail, and Drive hardcode their host, so a small shim in
`_llamaindex.py` redirects each:

```python
from llama_index.readers.github import GitHubIssuesClient
GitHubIssuesClient(github_token=TOKEN, base_url="http://localhost:8000/github")

from llama_index.readers.confluence import ConfluenceReader
ConfluenceReader(base_url="http://localhost:8000/atlassian/wiki", cloud=False, api_token=TOKEN)
```

One runnable script per source (GitHub, S3, Confluence, Jira, Slack, Notion, Gmail, Drive,
HubSpot, Linear) is in [`examples/using-llamaindex-readers/`](examples/using-llamaindex-readers/).
`LinearReader` needs a shim rather than a constructor argument — it hardcodes its endpoint as a
local variable inside `load_data`, so `patch_linear_at()` swaps the module's `requests` for a
URL-rewriting proxy.

## Endpoints (read-only)

| Prefix | Service | Endpoints |
|---|---|---|
| `/slack/api` | Slack | `conversations.list` (+`types`; this corpus has no DMs, so `im`/`mpim` select nothing, and an unknown value is `invalid_types`), `conversations.history` (+`oldest`/`latest`/`inclusive`), `conversations.replies`, `conversations.members` (per-channel, paginated), `users.list`, `users.info`, `auth.test`, `api.test` (auth-free connectivity check), `search.messages`. A channel's members are the people who have spoken in it — see the roster caveat below |
| `/gmail/v1` | Gmail | `users/{u}/messages` (+`q`: free text / `from:` `to:` `subject:` `after:` `before:` `newer_than:` `older_than:` `label:` `has:attachment`), `messages/{id}` (`format=full\|metadata\|minimal`), `messages/{id}/attachments/{id}`, `threads` (+`q`), `threads/{id}`, `labels`, `profile`. Message and thread ids are Gmail-shaped — 16 lowercase hex under 2^63, sharing one id space as the real API does — and map back to the corpus document; an id the real API could not parse is refused the same way |
| `/drive/v3` | Drive | `files` (`q`: `fullText contains`, `name contains`, `mimeType`, `… in parents` incl. `'root'`, `trashed`, `modifiedTime`, `sharedWithMe`, `… in owners`; `orderBy`: `name`/`name_natural`/`createdTime`/`modifiedTime`/`recency`/`folder`/`starred`/`quotaBytesUsed`/`sharedWithMeTime` (+` desc`); `fields` projection, validated), `files/{id}` (+`fields`), `files/{id}/export`, `files/{id}/permissions`, `drives`, `about` (`fields` **required**, as in real Drive; `storageQuota` is measured from the caller's visible corpus). Folders are files here: they match `mimeType='…folder'`, project, sort and resolve permissions like stored rows |
| `/docs/v1`, `/sheets/v4`, `/slides/v1` | Docs/Sheets/Slides | `documents/{id}`, `spreadsheets/{id}`, `presentations/{id}` — native-doc content for editor-aware clients (read structurally instead of via Drive export). `spreadsheets/{id}` returns structure only — cells need `includeGridData=true` (+ optional `ranges`), as in real Sheets. Sheets also serves `spreadsheets/{id}/values/{range}` and `spreadsheets/{id}/values:batchGet` (A1 ranges incl. `Sheet1!A1:B2`, `A:A`, `1:3`, `A2:B`, a bare sheet name quoted or not; `majorDimension`, `valueRenderOption`). A spreadsheet row is one stored **line**, held in a single cell verbatim — the mock picks no column delimiter, so splitting (CSV, pipes, …) stays the corpus owner's decision. Reading a file of the wrong type through any of the three APIs is refused, as real Google does, not reinterpreted |
| `/github` | GitHub | `search/issues` (`q`: free text + `repo:` `is:` `state:` `type:` `label:` `author:`), `orgs/{org}`, `orgs/{org}/repos`, `repos/{o}/{r}`, `.../issues[/{n}]`, `.../issues/{n}/comments`, `.../pulls[/{n}]`, `.../pulls/{n}/reviews`, `.../readme`, `.../collaborators`, `.../teams`, `orgs/{org}/teams` |
| `/atlassian/rest/api/3` | Jira | `search/jql` (JQL `project =`, `text\|summary\|description ~`), `issue/{key}`, `issue/{key}/comment`, `field`, `issueLinkType`, `project/search`, `project/{key}/role[/{id}]`, `serverInfo` (also under `rest/api/2`) |
| `/atlassian/wiki/rest/api` | Confluence | `content`, `content/{id}`, `content/{id}/restriction/byOperation`, `space`, `space/{key}/permission` |
| `/notion/v1` | Notion | `search`, `pages/{id}`, `blocks/{id}`, `blocks/{id}/children`, `databases/{id}` (version-aware), `data_sources/{id}`, `data_sources/{id}/query`, `databases/{id}/query` (legacy), `users[/{id}]`, `users/me`, `comments` |
| `/hubspot/crm/v3`, `/hubspot/crm/v4` | HubSpot | `objects/{objectType}` (+`limit` max 100, `after`, `properties`, `archived`), `objects/{objectType}/{id}`, `objects/{objectType}/search` (`filterGroups` OR-ed, `filters` AND-ed, 13 operators over any property), `objects/{objectType}/batch/read`, `v4/objects/{type}/{id}/associations/{toType}` |
| `/s3` | Amazon S3 | `ListBuckets`, `HeadBucket`, `GetBucketLocation`, `ListObjectsV2` (`prefix`/`delimiter`/`continuation-token`), `GetObject` (+`Range`), `HeadObject` |
| `/linear/graphql` | Linear | **GraphQL only** (one `POST`): `issues`, `issue(id:)` (UUID *or* `ENG-123`), `team(id:)` (UUID, key, or name), `teams`, `comments`, `users`, `viewer`, plus the `Team.issues` / `Issue.{comments,labels,children,relations,inverseRelations,attachments,releases}` connections and the by-id roots (`user`, `workflowState`, `project`, `issueLabel`, `cycle`, `release`, `attachment`, `issueRelation`) the official SDK's lazy relation accessors call. Relay pagination (`first`/`after`, `last`/`before` → `{nodes, pageInfo}`), server-side `filter` compiled into SQL, and full introspection |
| `/fireflies/graphql` | Fireflies | **GraphQL only** (one `POST`): `transcripts`, `transcript(id:)`, `user[(id:)]`, `users`. Offset pagination — `limit` (**max 50**, clamped) / `skip`, returning a **bare list**, not a Relay connection — plus the documented filters: `keyword` × `scope` (`title`\|`sentences`\|`all`), `fromDate`/`toDate`, `host_email`, `organizers`, `participants`, `user_id`, `mine`, `channel_id`. Field names are snake_case, as Fireflies' own schema has them. Full introspection |

### Known corpus limitation: Slack speakers are not in `users.list`

The bench corpus generates Slack transcript speakers independently of the employee directory, so the
two are largely disjoint: of **74,138** distinct message authors only **3,971 (5.4%)** are
registered
user principals, and all **70,167** of the rest are on the org's own domain. 74k speakers against an
11,913-person directory is not a headcount any real workspace has.

The mock does not paper over this. `users.list` serves the directory, so **an author outside it
resolves through `users.info` but never appears in `users.list`** — a combination real Slack cannot
produce, and the one place a client written against the mock will behave differently in production.

What is available instead: `conversations.members` pages the channel's own speakers, so every author
of a channel is discoverable there even when the roster omits them.

Reconciling the two sets means either inventing ~70k colleagues or discarding the transcripts' own
speakers, so it is a decision about the dataset rather than about this server.


## Tests

```bash
pytest              # unit (synth/pagination/acl/schema/erb-parsers) + HTTP endpoint tests
                    # (full-crawl completeness, content round-trip, ACL enforcement)
```

`tests/test_sdk.py` (needs `.[examples]`) and `tests/test_mcp.py` (needs Docker + `.[mcp]`)
each spin up their own server; they run when those are available and skip otherwise.

## Configuration

Env vars (prefix `BACKLOT_`): `BACKLOT_DATA_DIR`, `BACKLOT_RAW_DIR`, `BACKLOT_ADMIN_TOKEN`,
`BACKLOT_ENFORCE_ACL`, `BACKLOT_EXPOSE_TOKENS`, `BACKLOT_DEFAULT_PAGE_SIZE`, `BACKLOT_MAX_PAGE_SIZE`,
`BACKLOT_ORG_NAME`, `BACKLOT_ORG_DOMAIN`, `BACKLOT_ATLASSIAN_SITE`. See `backlot/config.py`.

Document visibility is **not** configurable: it comes from the corpus itself — each record's
`visibility` / `readers` for a BYO corpus, or the bench's own ownership fields for an ERB import
(see "Auth & tokens").
For a BYO corpus the org name/domain are inferred from the dominant author email domain unless
`BACKLOT_ORG_NAME` / `BACKLOT_ORG_DOMAIN` are set; the Atlassian site host and GitHub repo owner then
follow the org (`<org>.atlassian.net`, and the owner echoed from the request path).

## Limitations (by design)

- **Synthetic, deterministic data** — ids, timestamps, and URLs are derived from
  `sha256(doc_id)`: stable and self-consistent across calls, but fabricated (no real links).
- Google Drive doc type comes from a record's `subtype`
  (`document|spreadsheet|presentation|pdf`); unset, a document serves as a Google Doc
  (`text/plain` export).
- Notion is **BYO-only** (not in EnterpriseRAG-Bench). A record's `content` is served verbatim as
  a synthesized block tree; `databases.retrieve` returns the `2025-09-03` data-sources shape by
  default and the `2022-06-28` inline-`properties` shape when that `Notion-Version` header is sent.
- HubSpot is **polymorphic over `{objectType}`** — one set of routes serves contacts, companies,
  deals, notes and custom objects — so the object type is the grouping unit and a record's typed
  fields live in `properties`, searchable by name. EnterpriseRAG-Bench ships HubSpot as **company
  (account) records only**, whose CRM notes are imported as first-class `notes` objects associated
  with the company; contacts/deals/tickets arrive via BYO. The bench's `linked_*` fields are
  free-text stubs referencing other sources, so they stay properties rather than becoming
  associations. **A listing's last page omits `paging.next`** — the official client's `fetch_all`
  treats its absence as "done", so emitting it unconditionally would hang a real client.
- Linear is **GraphQL-only** (there is no REST surface to emulate) and the mock is **read-only**,
  so there is no `Mutation` type at all — introspection reports `mutationType: null` rather than
  advertising writes that would fail. The schema is generated from `@linear/sdk`'s own documents
  (`client.issues()` alone selects 171 field nodes across 11 fragments), so real SDK calls
  validate; fields no document corpus can back — reactions, SLA timestamps, board/sort orders,
  bot actors — resolve to `null`/`[]` rather than being invented. `IssueFilter` declares **only**
  what the mock actually evaluates, so an unsupported key is a validation error naming the field
  instead of a silently-dropped filter answered with a full, wrong result set. EnterpriseRAG-Bench
  ships Linear as its third-largest source (35,308 issues); its `P0`-`P3` priorities are mapped
  onto Linear's own 0-4 scale and its `status` onto `state`, so the served payload speaks Linear's
  vocabulary rather than the bench's. The bench's issue keys are **not unique** (5,055 repeat), so
  `issue(id: "ENG-123")` resolves a repeated identifier to the first match while the UUID form is
  always exact.
- **Linear's official SDK is TypeScript-only.** `@linear/sdk` is the only client Linear publishes;
  there is no official Python SDK, so `examples/using-official-sdk/linear/` is the one non-Python
  example in the repo and a dedicated CI job runs it. `LinearClient` has no base-URL option, so it
  is pointed at the mock by extending `LinearSdk` with a custom request function — Linear's own
  documented pattern. There is also no MCP story: Linear's official MCP server is remote-hosted at
  `https://mcp.linear.app/mcp` with no URL override, so no mock can substitute for it.
- Fireflies is **GraphQL-only** and, like Linear, read-only — no `Mutation` type at all. Two
  things set it apart from every other GraphQL source here and clients depend on both: pagination
  is **offset-based** (`limit`, capped at 50 and *clamped* rather than rejected, plus `skip`) and
  `transcripts` returns a **bare list**, not a `{nodes, pageInfo}` connection; and field names are
  **snake_case** (`host_email`, `audio_url`, `meeting_attendees`), which is Fireflies' own
  convention, not a translation. Note the units differ within one response, as they do in the real
  API: `duration` is **minutes**, sentence `start_time`/`end_time` are **seconds**.
  EnterpriseRAG-Bench ships Fireflies as 10,173 transcripts, but as **one flat text blob per
  meeting** — not structured per-sentence records — so the sentences the API serves are *parsed*
  from it (~619k of them) across the six line formats the corpus uses, gated on each meeting's
  declared attendees so a transcript's auto-notes header (`Date:`, `Duration:`) cannot mint a
  speaker. `content` is **defined as** the sentence concatenation and is an exact inverse of the
  sentence rows, so full-text search reads the meeting as one document that can never drift from
  its parts. Only *start* times are in the data (99.9% of lines) — end times are derived, wall-clock
  transcripts are rebased onto elapsed time, and a garbled reading is dropped rather than tearing a
  50-hour hole in a 60-minute meeting. The bench's `meeting_id` is **not unique**, so it is served
  as `calendar_id` and `Transcript.id` is synthesized (unique by construction, so `transcript(id:)`
  is never ambiguous). Transcripts are **org-visible** plus a per-user grant for everyone who
  resolves: the corpus names 1,104 distinct hosts of whom only the ~167 directory employees can
  authenticate, so an owner-or-channel scope would leave ~91% of meetings readable by admin alone
  (same arithmetic as HubSpot). `analytics.sentiments` and the classifier buckets are **synthesized
  or null, never derived from the text**; per-speaker talk time and word counts *are* computed from
  the sentences.
- **Fireflies has no SDK, no LlamaIndex reader, and no MCP server** — and raw HTTP is the
  *official* path, not a workaround. The vendor's own quickstart is four raw-HTTP examples (curl,
  Python `requests.post`, JS `axios.post`, Java `HttpClient`) posting to one endpoint with a Bearer
  key, so the base URL is just a variable in user code and there is nothing to shim:
  `examples/using-official-sdk/fireflies.py` uses `httpx` directly.
  `llama-index-readers-fireflies` does not exist on PyPI, so there is no reader script.
- S3 is **BYO-only** (not in EnterpriseRAG-Bench). Requests are XML (not JSON) and SigV4-signed;
  the mock verifies the signature against the access-key/secret derived from your bearer token
  and only supports path-style addressing (the bucket stays in the path, not the hostname).
  Read ops: `ListBuckets`, `HeadBucket`, `GetBucketLocation`, `ListObjectsV2`, `GetObject`
  (+`Range`), `HeadObject`.
- **Only read endpoints** are implemented.
