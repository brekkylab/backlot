# Using MCP tools with agents

Drive an LLM agent that retrieves corpus data through a **real MCP server** pointed at the
mock, with retrieval **ACL-scoped** by the credentials you give it. One self-contained file per
service (like the other `examples/` dirs) — run the one you want:

- **`atlassian.py`** (Jira + Confluence) via the community-official
  [`mcp-atlassian`](https://github.com/sooperset/mcp-atlassian) (Docker).
- **`notion.py`** via the **official**
  [`@notionhq/notion-mcp-server`](https://github.com/makenotion/notion-mcp-server) (npx/Node) —
  it takes a first-class `BASE_URL` override, so pointing it at the mock is one env var.
- **`s3.py`** via the **official**
  [`awslabs.aws-api-mcp-server`](https://github.com/awslabs/mcp/tree/main/src/aws-api-mcp-server)
  (uvx/Python) — it shells the AWS CLI, whose boto3 client honors a first-class
  `AWS_ENDPOINT_URL` override and SigV4-signs every call; a broad AWS-CLI wrapper, so the agent
  runs `aws s3api …` commands.
- **`github.py`** via the **generic OpenAPI→MCP bridge** (`_openapi_bridge.py`, Python/FastMCP) — no vendor
  MCP server exists that can be pointed at a self-hosted mock, so instead the bridge turns the
  mock's own typed `/openapi.json` into MCP tools. See "How the OpenAPI→MCP bridge connects" below.
  This unlocks the sources with no base-URL-switchable vendor server; more sources
  (Gmail/Drive) are being added the same way.
- **`slack.py`** via the same **OpenAPI→MCP bridge** — no maintained Slack MCP server accepts a
  base-URL override (they hard-wire `slack.com`), so the bridge serves the mock's Slack Web API
  (`/slack/api/*`) as tools instead.
- **`gmail.py`** via the same **OpenAPI→MCP bridge** — Gmail MCP servers hard-wire `googleapis.com`
  and need real Google OAuth, so the bridge serves the mock's Gmail API (`/gmail/*`) as tools.
- **`gdrive.py`** via the same **OpenAPI→MCP bridge** — likewise for Google Drive (`/drive/*`).
- **`hubspot.py`** via the same **OpenAPI→MCP bridge** — no HubSpot MCP server takes a base-URL
  override. Because the CRM API is polymorphic over `{object_type}`, the agent gets five tools that
  each work across every object type (list, read, search, batch-read, associations) rather than a set
  per type, so "find the account, then its notes" is two calls with the object type as an argument.
- **`linear.py`** and **`fireflies.py`** via the **generic GraphQL→MCP bridge**
  (`_graphql_bridge.py`, Python/FastMCP). Both sources are GraphQL-only, so the OpenAPI bridge
  cannot serve them at all — `/openapi.json` describes one `POST /<source>/graphql` operation,
  which would derive a single raw-document tool rather than a usable toolset. This bridge reads the
  endpoint's own **introspection** instead and turns each root `Query` field into a typed tool. See
  "How the GraphQL→MCP bridge connects" below.

Every source now has an MCP path. The two GraphQL ones use a bridge rather than a vendor server
because **both vendors' official MCP servers are remote-hosted** — `https://mcp.linear.app/mcp` and
Fireflies' equivalent, neither with a base-URL override — so nothing local can substitute for them,
and the community servers hard-wire `api.linear.app` / `api.fireflies.ai` in source (details under
"Why these need the bridge"). Both are of course still reachable the ordinary way too: hand an agent
the GraphQL endpoint and a token (see `examples/using-official-sdk/`).

Each service file builds its own MCP `StdioServerParameters` and calls `run_agent(...)`. Two shared
helpers:

| File | What it is |
|---|---|
| `_agent.py` | The agent loop for both backends: `--agent anthropic` (default, Anthropic SDK + its beta MCP tool runner) or `--agent openai` (OpenAI Agents SDK) |
| `backlot.serve_or_connect` | Starts the mock (`backlot.main`) on a small corpus, or connects to a `--url` one |

Each service file declares its own CLI options with `argparse` — run `python <file> --help` to see
exactly what that source takes (e.g. `s3.py` takes `--access-key`/`--secret-key`, required with
`--url`; `atlassian.py` takes `--token`/`--username`). All accept `--url` and `--agent {anthropic,openai}`.

Each example spins up its own small mock by default, or pass `--url` to use an already-running one
(unreachable → it falls back to spinning up its own). Note the demo question is tuned to each
example's own seed corpus; against a `--url` server holding *different* data it may have no exact
match, so the agent answers from the closest documents and notes what's missing (it's told to be
decisive rather than exhaustively hunt).

## Run

```bash
pip install -e ".[mcp]"          # mcp + openai-agents + anthropic[mcp]
                                 # Atlassian needs Docker; Notion needs Node (npx); S3 needs uvx

# prove retrieval + ACL end-to-end through the real MCP servers — no API key needed.
# One test per service (each skips if its runtime — Docker / npx / uvx — is absent):
python -m pytest tests/test_mcp.py
#   Atlassian: admin reads an ACL-restricted Jira issue, a user token is blocked
#   Notion:    admin reads an ACL-restricted page, an outsider is blocked
#   S3:        admin lists bucket objects through a signed AWS CLI call
#   GitHub:    admin reads an ACL-restricted issue via the bridge, a user token is blocked
#   Slack:     admin search surfaces a restricted-channel message via the bridge, a user can't
#   HubSpot:   admin reads an ACL-restricted CRM record via the bridge, a user token is blocked;
#              the polymorphic search tool round-trips filterGroups and returns `total`
#   Linear/Fireflies: same via the GraphQL bridge — admin reads an ACL-restricted issue/transcript
#              and a user token cannot; a nested `IssueFilter` round-trips and `pageInfo` comes back

# drive it with an LLM agent (needs an API key). --agent defaults to anthropic; add --agent openai.
ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/atlassian.py
ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/notion.py
OPENAI_API_KEY=…    python examples/using-mcp-with-agents/notion.py --agent openai
ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/s3.py
ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/github.py   # via the OpenAPI→MCP bridge
ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/slack.py    # via the OpenAPI→MCP bridge
ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/hubspot.py  # via the OpenAPI→MCP bridge
ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/linear.py    # via the GraphQL→MCP bridge
ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/fireflies.py # via the GraphQL→MCP bridge
```

**Auth is per-service.** Retrieval is ACL-scoped by the identity you pass:

- **Atlassian / Notion** use a mock **token**: default is the admin token (sees everything); pass
  `--token` a per-user token from `GET /_mock/users` to scope it (the token, not the username,
  authenticates).
- **S3** uses an AWS **access-key/secret pair** (not a token): pass `--access-key` / `--secret-key`
  — **required with `--url`** (real AWS keys, or a pair from `GET <url>/_mock/users`, where each
  user and the admin has an `s3_access_key_id` / `s3_secret_access_key`). Without `--url` the local
  throwaway mock uses its own admin keypair.

- **Local** — `--url http://localhost:PORT`.
- **Remote** — `--url https://host` plus the service's credentials. Grab them from
  `GET /_mock/users` (don't reuse the built-in admin token/keys against someone else's server).
  `atlassian.py` additionally **requires** `--username` for a remote target (see below).

## How `atlassian.py` connects

`mcp-atlassian` runs in Docker and only classifies a host as Atlassian **Cloud** (the v3 + `/wiki`
API shape the mock speaks) when the hostname ends in `.atlassian.net`. So the example:

- uses a fake host `mock.atlassian.net`, mapped with Docker's `--add-host` — to the host machine
  (`host-gateway`) for a local mock, or to a **remote** deployment's resolved IP;
- sets `MCP_ALLOWED_URL_DOMAINS=atlassian.net` to pass the server's SSRF guard;
- authenticates with HTTP Basic where the **api-token is a mock token** — the mock resolves it to a
  user and enforces that user's ACL. The Basic-auth **username** is required by mcp-atlassian but
  ignored by the mock once the token resolves, so a placeholder (`svc@example.com`) works for a
  local mock. For a **remote** target it must be explicit (`--username`), and because the
  deployment's TLS cert is for its own name (not `mock.atlassian.net`), cert verification is
  disabled for that hop (`*_SSL_VERIFY=false`) — fine for a test mock.

## How `notion.py` connects

Much simpler — the official `notion-mcp-server` reads a **`BASE_URL`** env var and propagates it
straight to its HTTP client, so the example just sets:

- `BASE_URL=<mock>/notion` — the server appends the `/v1/...` paths from its bundled OpenAPI spec,
  landing on the mock's `/notion/v1/...` routes. It runs on the host via `npx`, so a local
  `localhost` mock is reached directly (no Docker/host-gateway aliasing).
- `NOTION_TOKEN=<mock token>` — sent as `Authorization: Bearer …`; the mock resolves it to a user
  and enforces that user's ACL.
- `NOTION_VERSION=2025-09-03` — the mock's default (data-sources model).

## How `s3.py` connects

`awslabs.aws-api-mcp-server` shells the AWS CLI (botocore underneath), which takes a first-class
endpoint override, so the example just sets:

- `AWS_ENDPOINT_URL=<mock>/s3` — every AWS CLI call the server runs is routed at the mock instead
  of real AWS.
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — the required `--access-key` / `--secret-key` (the
  keys the mock's SigV4 verifier accepts; grab a pair from `GET /_mock/users`), so botocore's
  signature resolves back to that identity and the mock enforces its ACL.
- `AWS_REGION=us-east-1` — any region works (the mock's verifier reads the region back out of the
  client's own credential scope); this just has to be *some* valid region.

We intentionally do **not** set `READ_OPERATIONS_ONLY`. It sounds right for a read-only mock, but
it blocks `aws s3 cp s3://<bucket>/<key> -` — the one command that streams an object's **body**
back to the model. (A read-only `s3api get-object` writes the bytes to a sandboxed file and returns
only metadata; `… /dev/stdout` is path-blocked/deadlocks.) So with it on, the agent can *list*
objects but never *read* them, and it thrashes. The mock has no write endpoints, so dropping the
guard is safe here. The example's question therefore tells the agent to read via `s3 cp … -`.

Note this server is a **broad AWS-CLI wrapper**, not S3-specific — under the hood the agent runs
`aws s3api …` commands (e.g. `list-objects-v2`, `get-object`) via the server's `call_aws` tool.
Because it exposes *all* of the AWS CLI (not a domain search tool like the Notion/Atlassian
servers), the agent has no way to know the corpus lives in S3 — left unguided it wanders off to
AWS's actual search/config services (Kendra, SSM, …). So `s3.py`'s question **explicitly tells it
to search S3 only** (list buckets → list objects → get object). This steering is the price of
using a generic AWS-CLI MCP for retrieval.

**Gotcha — loopback-only endpoint:** awslabs' server has an SSRF guard (`_validate_endpoint` in
its command parser) that only accepts a **loopback** endpoint — `localhost` / `127.0.0.1` / `::1`.
A hostname `--url` (e.g. an ALB-fronted `https://…` deployment) is rejected with `Could not resolve
endpoint …`, and even a non-loopback IP is rejected with `Local endpoint was not a loopback
address`. To drive a remote deployment, tunnel it to loopback and point `--url` there:
`ssh -fN -L 18000:127.0.0.1:8000 user@host`, then
`--url http://127.0.0.1:18000 --access-key … --secret-key …`. (boto3 and mirage have no such
restriction — they take the hostname directly.)

## How the OpenAPI→MCP bridge connects (`github.py` / `slack.py` / `gmail.py` / `gdrive.py`)

These four sources have no vendor MCP server that accepts a base-URL override (see "Why these need
the bridge" below). Instead of a vendor server, each launcher runs the **generic bridge**
`_openapi_bridge.py` (Python, [FastMCP](https://gofastmcp.com)) as a stdio subprocess. The bridge is
deliberately thin — the mock does the spec work:

- the mock serves an **MCP-ready spec per source** at **`GET /_mock/openapi/<source>`** — its own
  typed `/openapi.json` (the routers declare query params and response models) sliced to that
  source and with the GET/POST and Jira v2/v3 fidelity aliases collapsed to one operation each
  (the raw spec carries ~14 duplicate operationIds, which an MCP tool set can't have). This lives
  in `backlot/openapi.py`, so there is nothing to clean up client-side;
- the bridge just fetches that spec and serves it over stdio via `FastMCP.from_openapi()` on an
  `httpx.AsyncClient` whose base URL is the mock and whose **`Authorization`** header is the mock
  token — so the mock resolves the token to a user and **enforces that user's ACL** on every call.

stdio (not streamable-HTTP): FastMCP's HTTP mode has a known bug forwarding the client's
`Authorization` header downstream. Auth: `--username` present → HTTP Basic (Atlassian); otherwise
`Bearer` (`--token`, default admin; per-user from `GET /_mock/users`). Adding a source is one entry
in `backlot/openapi.py`'s `SOURCE_PREFIXES` plus a thin launcher.

**Notion and Atlassian** already have vendor-server launchers above, but they also work through the
generic bridge (no vendor server) — run `_openapi_bridge.py` directly:

```bash
python examples/using-mcp-with-agents/_openapi_bridge.py --source notion    --base-url <mock> --token <t>
python examples/using-mcp-with-agents/_openapi_bridge.py --source atlassian --base-url <mock> --token <t> --username svc@example.com
```

Atlassian authenticates with HTTP Basic (`--username` + the mock token as the password), and its
`SOURCE_PREFIXES` entry covers both the `/atlassian` (Jira) and `/wiki` (Confluence) path roots.
**S3 is the one source with no bridge** — it is SigV4-signed (a static `Authorization` header can't
sign each request), so it is absent from `/_mock/openapi/*`; use the vendor `s3.py` example.

## How the GraphQL→MCP bridge connects (`linear.py` / `fireflies.py`)

Linear and Fireflies are **GraphQL-only**, served at `POST /<source>/graphql` with
`include_in_schema=False` — so there is no OpenAPI operation for the bridge above to slice, and
`GET /_mock/openapi/linear` is a 404 by construction. What a GraphQL endpoint *does* publish is its
schema, over standard introspection, so that is what `_graphql_bridge.py` reads. Same split as the
OpenAPI bridge — the app does the schema work, the bridge is transport:

- **`backlot/graphql/mcp_tools.py`** turns the introspection result into one tool per root `Query`
  field: 15 for Linear (`issues`, `issue`, `teams`, `comments`, `viewer`, the by-id relation
  roots …), 4 for Fireflies (`transcripts`, `transcript`, `user`, `users`). It derives each tool's
  argument JSON Schema *and* its GraphQL document, which is the one thing OpenAPI never has to
  decide: a REST operation returns a fixed body, a GraphQL field returns whatever was asked for.
- **the bridge** posts `tool.document` with the caller's `Authorization: Bearer <token>` and passes
  the GraphQL envelope back untouched, so the mock's per-token ACL decides every result. One header
  spelling serves both: Fireflies is the ordinary bearer path, and Linear accepts a bare API key or
  a `Bearer` token on the same header exactly as the real API does.

**Typed tools, not a `graphql(query:)` passthrough.** A passthrough is three lines and turns the
exercise into "can the model write GraphQL against a schema it has not seen"; no other example here
works that way.

**How the selection set is chosen** (full rules in `mcp_tools`): leaf fields always; a Relay
connection is transparent wherever it appears (`nodes { … } pageInfo { … }` — free at the root,
costing a level below it, and `pageInfo` always present so a page an agent cannot advance never
reads as a complete answer); object fields are followed while a depth budget lasts; a type already
on the path is not re-entered; a field with a required argument is skipped. Input objects expand
under that same path guard, so `IssueFilter` arrives with its real comparator keys —
`{"filter": {"title": {"containsIgnoreCase": "latency"}}}` — rather than as an opaque blob.

Depth is the one per-source knob, and both values are measured. Fireflies uses the default **2**:
`Analytics` has no leaf fields of its own, so anything shallower drops the sentiment split and
per-speaker talk time entirely. Linear runs at **1**, because `Team` / `Project` / `Cycle` each
carry dozens of configuration leaves and a second level takes one issue from 516 selected fields to
1,446 — for data no agent asks about. Depth 1 still returns `state`, `assignee`, `team`, `project` and
`labels` inline. Both launchers take `--depth` if you want to see the difference.

**What this trades away**, stated plainly because it is the argument for the vendor servers above:
the bridge exercises *our* tool surface, not the community tooling an agent meets in production.
That is accepted here — for Fireflies especially, where the most-adopted community server has 5
stars, there is no consensus tooling to be faithful to.

## Why these need the bridge (no base-URL-switchable vendor server)

These services' vendor MCP servers **cannot** be pointed at a self-hosted mock — that is exactly why
the OpenAPI→MCP bridge above exists (it needs no vendor server at all); each is now driven through
it:

- **GitHub** — the official `github/github-mcp-server` has `GITHUB_HOST`, but it strips the
  port (so needs port 80), forces GitHub-Enterprise paths (`/api/v3`, `/api/graphql`), and
  relies on GraphQL the mock doesn't implement. **→ driven via the bridge (`github.py`) instead.**
- **Slack** — no API-base override in any maintained server (hard-wired to `slack.com`).
  **→ driven via the bridge (`slack.py`) instead.**
- **Gmail / Google Drive** — official and community servers hard-wire `googleapis.com` and
  require real Google OAuth; no endpoint override. **→ driven via the bridge (`gmail.py`, `gdrive.py`).**
- **Linear** — the official server is **remote-hosted** (`https://mcp.linear.app/mcp`), so there is
  no local process to redirect. Of the community servers, `tacticlaunch/mcp-linear` (current) and
  `jerhadf/linear-mcp-server` (most-starred, untouched since 2025) both hard-wire
  `https://api.linear.app` and document only a token; because they run as `npx` subprocesses, the
  in-process URL rewrite backlot uses for the LlamaIndex Linear reader cannot reach them either.
  **→ driven via the GraphQL bridge (`linear.py`).**
- **Fireflies** — the official server is likewise remote-only, and the community side is thinner
  than anywhere else here: the most-adopted server has 5 stars, and `johntoups/mcp-fireflies`, the
  only maintained one, pins `GRAPHQL_ENDPOINT = "https://api.fireflies.ai/graphql"` as a module
  constant with the API key its sole configurable. **→ driven via the GraphQL bridge
  (`fireflies.py`).**
