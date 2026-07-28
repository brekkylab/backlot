# Enterprise Mock

> **LocalStack for enterprise SaaS knowledge APIs.** Point your RAG/search connectors at
> read-only mock **Slack, Gmail, Google Drive, GitHub, Jira, Confluence, Notion, Amazon S3,
> HubSpot, and Linear**
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
python -m uvicorn app.main:app --port 8000
curl -s localhost:8000/health
```

## Preparing data

The server reads a corpus from `data/` (`mock.sqlite` + `tokens.yaml`). Build it either way:

### Import from EnterpriseRAG-Bench

[EnterpriseRAG-Bench](https://github.com/onyx-dot-app/EnterpriseRAG-Bench) ships ~500k
synthetic enterprise documents (flattened to `{doc_id, source_type, title, content}`). One
command downloads a slice, loads it, and generates the ACL:

```bash
python -m app.importer.erb     # small slice; --all for the full corpus, --augment for +α
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
Schema (`schemas/`), then loaded.

```bash
python -m app.importer.byo mycorpus.jsonl              # validate + load -> data/
python -m app.importer.byo mycorpus.jsonl --dry-run    # validate only, no DB writes
```

```json
{"source_type": "slack", "channel": "incidents", "author_email": "bob@acme.com", "content": "Anyone seeing 502s from the gateway?", "replies": [{"content": "Looking now.", "author_email": "ava@acme.com"}]}
{"source_type": "gmail", "mailbox": "ceo", "title": "Q1 board deck draft", "content": "Draft narrative for the Q1 board meeting.", "author_email": "ceo@acme.com", "to": "ava@acme.com", "readers": ["ceo@acme.com", "ava@acme.com"]}
```

The record format (fields, ACL, Slack threads), a runnable walkthrough (`run.py`), and a sample
corpus are in [`examples/bring-your-own-corpus/`](examples/bring-your-own-corpus/); the schemas are in
[`schemas/README.md`](schemas/README.md).

## Auth & tokens

`data/tokens.yaml` holds one bearer token per user plus an **admin/service token**
(`MOCK_ADMIN_TOKEN`, default `admin-service-token`). The admin token bypasses ACL filtering
(use it for a full crawl); a user token sees only documents that user's ACL permits.

- Slack: `Authorization: Bearer <token>` (also accepts `?token=` / form `token`)
- Gmail / Drive / GitHub / Notion / HubSpot: `Authorization: Bearer <token>`
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
with `MOCK_EXPOSE_TOKENS=false`.

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
`MOCK_EXPOSE_TOKENS` gate as `/_mock/users`. The Gmail/Drive SDK examples
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
| `/slack/api` | Slack | `conversations.list`, `conversations.history` (+`oldest`/`latest`/`inclusive`), `conversations.replies`, `conversations.members`, `users.list`, `users.info`, `auth.test`, `api.test` (auth-free connectivity check), `search.messages` |
| `/gmail/v1` | Gmail | `users/{u}/messages` (+`q`: free text / `from:` `to:` `subject:` `after:` `before:` `newer_than:` `older_than:` `label:` `has:attachment`), `messages/{id}` (`format=full\|metadata\|minimal`), `messages/{id}/attachments/{id}`, `threads` (+`q`), `threads/{id}`, `labels`, `profile` |
| `/drive/v3` | Drive | `files` (`q`: `fullText contains`, `name contains`, `mimeType`, `… in parents` incl. `'root'` → folders, `trashed`, `modifiedTime`), `files/{id}`, `files/{id}/export`, `files/{id}/permissions`, `drives` |
| `/docs/v1`, `/sheets/v4`, `/slides/v1` | Docs/Sheets/Slides | `documents/{id}`, `spreadsheets/{id}`, `presentations/{id}` — native-doc content for editor-aware clients (read structurally instead of via Drive export) |
| `/github` | GitHub | `search/issues` (`q`: free text + `repo:` `is:` `state:` `type:` `label:` `author:`), `orgs/{org}`, `orgs/{org}/repos`, `repos/{o}/{r}`, `.../issues[/{n}]`, `.../issues/{n}/comments`, `.../pulls[/{n}]`, `.../pulls/{n}/reviews`, `.../readme`, `.../collaborators`, `.../teams`, `orgs/{org}/teams` |
| `/atlassian/rest/api/3` | Jira | `search/jql` (JQL `project =`, `text\|summary\|description ~`), `issue/{key}`, `issue/{key}/comment`, `field`, `issueLinkType`, `project/search`, `project/{key}/role[/{id}]`, `serverInfo` (also under `rest/api/2`) |
| `/atlassian/wiki/rest/api` | Confluence | `content`, `content/{id}`, `content/{id}/restriction/byOperation`, `space`, `space/{key}/permission` |
| `/notion/v1` | Notion | `search`, `pages/{id}`, `blocks/{id}`, `blocks/{id}/children`, `databases/{id}` (version-aware), `data_sources/{id}`, `data_sources/{id}/query`, `databases/{id}/query` (legacy), `users[/{id}]`, `users/me`, `comments` |
| `/hubspot/crm/v3`, `/hubspot/crm/v4` | HubSpot | `objects/{objectType}` (+`limit` max 100, `after`, `properties`, `archived`), `objects/{objectType}/{id}`, `objects/{objectType}/search` (`filterGroups` OR-ed, `filters` AND-ed, 13 operators over any property), `objects/{objectType}/batch/read`, `v4/objects/{type}/{id}/associations/{toType}` |
| `/s3` | Amazon S3 | `ListBuckets`, `HeadBucket`, `GetBucketLocation`, `ListObjectsV2` (`prefix`/`delimiter`/`continuation-token`), `GetObject` (+`Range`), `HeadObject` |
| `/linear/graphql` | Linear | **GraphQL only** (one `POST`): `issues`, `issue(id:)` (UUID *or* `ENG-123`), `team(id:)` (UUID, key, or name), `teams`, `comments`, `users`, `viewer`, plus the `Team.issues` / `Issue.{comments,labels,children,relations,inverseRelations,attachments,releases}` connections and the by-id roots (`user`, `workflowState`, `project`, `issueLabel`, `cycle`, `release`, `attachment`, `issueRelation`) the official SDK's lazy relation accessors call. Relay pagination (`first`/`after`, `last`/`before` → `{nodes, pageInfo}`), server-side `filter` compiled into SQL, and full introspection |

## Tests

```bash
pytest              # unit (synth/pagination/acl/schema/erb-parsers) + HTTP endpoint tests
                    # (full-crawl completeness, content round-trip, ACL enforcement)
```

`tests/test_sdk.py` (needs `.[examples]`) and `tests/test_mcp.py` (needs Docker + `.[mcp]`)
each spin up their own server; they run when those are available and skip otherwise.

## Configuration

Env vars (prefix `MOCK_`): `MOCK_DATA_DIR`, `MOCK_ADMIN_TOKEN`, `MOCK_ENFORCE_ACL`,
`MOCK_EXPOSE_TOKENS`, `MOCK_ACL_PUBLIC_RATIO`, `MOCK_ACL_GROUP_RATIO`, `MOCK_DEFAULT_PAGE_SIZE`,
`MOCK_ORG_NAME`, `MOCK_ORG_DOMAIN`, `MOCK_ATLASSIAN_SITE`. See `app/config.py`.
For a BYO corpus the org name/domain are inferred from the dominant author email domain unless
`MOCK_ORG_NAME` / `MOCK_ORG_DOMAIN` are set; the Atlassian site host and GitHub repo owner then
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
- S3 is **BYO-only** (not in EnterpriseRAG-Bench). Requests are XML (not JSON) and SigV4-signed;
  the mock verifies the signature against the access-key/secret derived from your bearer token
  and only supports path-style addressing (the bucket stays in the path, not the hostname).
  Read ops: `ListBuckets`, `HeadBucket`, `GetBucketLocation`, `ListObjectsV2`, `GetObject`
  (+`Range`), `HeadObject`.
- **Only read endpoints** are implemented.
