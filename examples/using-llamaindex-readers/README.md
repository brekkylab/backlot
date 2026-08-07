# Using LlamaIndex readers with the mock

Point official [LlamaIndex readers](https://docs.llamaindex.ai/en/stable/module_guides/loading/connector/)
(`llama-index-readers-*`) at the mock and load your enterprise corpus as `Document` objects — the
first step of any LlamaIndex ingestion / RAG pipeline. Each script is self-contained:

    pip install -e ".[examples,llamaindex]"
    pip install --no-deps llama-index-readers-hubspot     # see the HubSpot note below
    python examples/using-llamaindex-readers/github.py            # local throwaway mock
    python examples/using-llamaindex-readers/github.py --url http://localhost:8000 --token <usr-token>

The only difference from talking to the real SaaS is where the reader points. Four readers take a
host argument directly; five hardcode the host, so the helpers in `backlot.integrations.llamaindex`
redirect them.

| Source | Reader class | How it's pointed at the mock |
|--------|--------------|------------------------------|
| GitHub | `GitHubRepositoryIssuesReader` | `GitHubIssuesClient(base_url=...)` |
| S3 | `S3Reader` | `S3Reader(s3_endpoint_url=...)` |
| Confluence | `ConfluenceReader` | `ConfluenceReader(base_url=".../atlassian/wiki", cloud=False, api_token=...)` |
| Jira | `JiraReader` | `JiraReader(PATauth={"server_url": ".../atlassian", "api_token": ...})` |
| Slack | `SlackReader` | `slack_reader_at()` swaps the `WebClient` class during construction |
| Notion | `NotionPageReader` | `patch_notion_at()` (rebinds hardcoded URL constants) |
| Gmail | `GmailReader` | `point_gmail_at()` (wraps `googleapiclient.discovery.build`) + patches `_get_credentials` |
| HubSpot | `HubspotReader` | `point_hubspot_at()` (rebinds `hubspot.HubSpot` to inject `host=`) |
| Drive | `GoogleDriveReader` | `point_drive_at()` (wraps `build`) + real `service_account_key=` injection hook |
| Linear | `LinearReader` | `patch_linear_at()` (swaps the module's `requests` for a URL-rewriting proxy) |
| Fireflies | **none exists** | n/a — `llama-index-readers-fireflies` is not on PyPI; see the note below |

All reads are ACL-scoped by the credential you pass (`--token`, or the admin token by default),
exactly as against the real API.

## Per-source notes

- **Fireflies** (no script): there is **no LlamaIndex reader for Fireflies** —
  `llama-index-readers-fireflies` does not exist on PyPI (checked). This is recorded here
  rather than left as a silent gap in the table above, so the absence reads as a fact about
  the ecosystem rather than as an omission in this directory. Nothing is lost: Fireflies
  publishes no SDK either, and its own quickstart is a raw HTTP POST, so
  `examples/using-official-sdk/fireflies.py` already shows the officially documented way to
  read it — pointing that at the mock is a one-line base-URL change with no shim at all.
  A reader, if one ever ships, would need no mock-side work.
- **GitHub** (`github.py`): `GitHubIssuesClient(base_url=...)` is a first-class constructor arg —
  no shim needed.
- **HubSpot** (`hubspot.py`): the reader is **not** in the `[llamaindex]` extra — it pins
  `hubspot-api-client<9`, which no resolver can reconcile with the `>=12` that `[examples]` needs, so
  declaring it would make `.[examples,llamaindex]` uninstallable. The pin is over-restrictive (the
  reader only calls `HubSpot(access_token=...)` and `crm.{deals,contacts,companies}.get_all()`, all
  present in 12.x and verified working), hence the `--no-deps` install above. It does
  `from hubspot import HubSpot` *inside* `load_data()`, so `point_hubspot_at()` only has to rebind
  the module attribute. Note the reader returns **three** Documents — one per object type, each the
  `str()` of a list of SDK objects — not one Document per record.
- **Linear** (`linear.py`): the hardest one to point. `LinearReader.load_data` sets
  `graphql_endpoint = "https://api.linear.app/graphql"` as a **local variable inside the method** —
  there is no constructor argument and no module-level constant, so the rebind-a-URL-constant
  approach Notion uses has nothing to rebind. The only seam is the module's `import requests`,
  which `patch_linear_at()` swaps for a proxy that rewrites Linear's host and forwards everything
  else untouched. The query is caller-supplied (`load_data(query)`), so the script carries the
  GraphQL document — which doubles as a readable statement of what the mock serves.
  **A client-side bug to know about:** the reader does `issue.get("assignee", {}).get("name", "")`,
  and a GraphQL response always *includes* a selected field — so an unassigned issue arrives as
  `assignee: null`, `.get`'s default never applies, and the reader raises
  `AttributeError: 'NoneType' object has no attribute 'get'`. The same holds for `project`,
  `state` and `creator`. Real Linear returns null for those too, so this reproduces against
  api.linear.app and no mock-side change can fix it; the example filters to issues that have an
  assignee and a project (`filter: {assignee: {null: false}, project: {null: false}}`, evaluated
  server-side), and `tests/test_llamaindex.py` pins the diagnosis so a fixed release is noticed.
- **S3** (`s3.py`): `S3Reader(s3_endpoint_url=...)` points the reader itself, but whole-bucket
  loads hit a client-side fsspec/s3fs bug: `SimpleDirectoryReader`'s directory walk passes a
  `topdown` kwarg into `S3FileSystem._ls()`, which doesn't accept it, raising `TypeError`. The bug
  has existed since at least the 2023.x releases of both libraries and reproduces on every
  version installable today (including fsspec/s3fs 2026.6.0) and against real AWS S3 too — it's
  independent of the mock, and no released version avoids it, so pinning to an older release isn't
  a workaround. `s3.py` calls `backlot.integrations.llamaindex.patch_s3fs_walk()` (a small,
  self-disabling monkeypatch scoped to `S3FileSystem._walk`) before constructing the reader;
  `tests/test_llamaindex.py` imports and exercises the same function.
- **Confluence** (`confluence.py`): the installed `atlassian-python-api` (4.0.7) does **not**
  append `/wiki` to the base URL itself, regardless of `cloud=` — `cloud` only toggles
  cloud-specific API shapes elsewhere — so `base_url` must spell out `.../atlassian/wiki`
  explicitly, with `cloud=False` (the mock speaks the on-prem/server API shape). Also,
  `load_data()` must be called with `max_num_results=` set: left at its default, the reader
  forwards a bare `limit=None` to `Confluence.get_all_pages_from_space`, which raises `TypeError`
  comparing against `None` — a client-side bug independent of the mock.
- **Jira** (`jira.py`): `JiraReader(PATauth={"server_url": ..., "api_token": ...})` is a
  first-class option. The `jira` PyPI client (used under the hood) probes
  `GET /rest/api/2/serverInfo` during auth; the mock gained that endpoint as an alias so the real
  client works unmodified.
- **Slack** (`slack.py`): `SlackReader.__init__` eagerly calls `client.api_test()` *during
  construction*, before the caller has a `reader._client` to set `base_url` on — so "construct,
  then set `_client.base_url`" isn't enough, since that eager call would hit the real
  `https://slack.com/api/` default first. `slack_reader_at()` swaps the `slack_sdk` module's
  `WebClient` for a subclass defaulting to the mock's `base_url`, for the duration of construction
  only (restored after), so even that first call lands on the mock — which gained an auth-free
  `/slack/api/api.test` endpoint for exactly this. `_client.base_url` is set again explicitly
  afterward for clarity.
- **Notion** (`notion.py`): `NotionPageReader` hardcodes the Notion host in module-level URL
  constants (no `base_url` arg at all). `patch_notion_at()` rebinds every constant that points at
  `api.notion.com` before the reader runs.
- **Gmail** (`gmail.py`): `point_gmail_at()` wraps `googleapiclient.discovery.build` (the reader
  does a *local* import of `build` on every call rather than importing it at module scope, so
  there's no module attribute to patch directly) to inject `client_options(api_endpoint=base)`.
  The installed `GmailReader` has no credential-injection hook — `_get_credentials()`
  unconditionally runs a local disk-based OAuth flow — so the example also patches
  `GmailReader._get_credentials` to hand back the mock-issued credential instead.
- **Drive** (`gdrive.py`): `point_drive_at()` wraps `build` the same way, with Drive's `/drive/v3`
  service path folded into the `api_endpoint` (Gmail's bundled discovery doc's rootUrl has no
  suffix; Drive's already carries `/drive/v3`). Unlike Gmail, `GoogleDriveReader` *does* accept
  `service_account_key=` directly, a real credential-injection hook — so the admin (non-
  impersonation) path needs no monkeypatch beyond `point_drive_at()`. Impersonating a user
  (`--user <email>`) still needs an instance-level `_get_credentials` override, since that
  constructor path drops any `subject`.

Gmail/Drive credentials (and the shared OAuth client config) come from the mock's
`GET /_mock/credentials`, exactly as in `examples/using-official-sdk/`.
