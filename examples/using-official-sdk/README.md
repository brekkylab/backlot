# Using official SDKs against the mock

One runnable, **self-contained** script per service — each spins up its own mock (via
`_mockserver`) on a tiny in-code corpus, points the official SDK at it, and prints what it read.
The only change from talking to the real service is the base URL.

```bash
pip install -e ".[examples]"
python examples/using-official-sdk/slack.py     # or gmail.py, gdrive.py, github.py, jira.py, confluence.py, notion.py, s3.py, hubspot.py
```

**HubSpot** (`hubspot.py`) points the official client with a plain `host=` kwarg — no shim and no
first-class base-URL arg needed. It requires `hubspot-api-client>=12`: on 8.x that kwarg is silently
**ignored** and the client talks to api.hubapi.com, so the script asserts its configured host before
reading anything rather than letting a "mock" run hit production.

`github.py` lists a repo's issues/PRs, then crawls its **code**: `repo.get_git_tree(...,
recursive=True)` for the file tree, `repo.get_contents(path)` to read one file, and
`repo.get_readme()` — the mock serves the real git `trees`/`contents`/`blobs`/`readme` shapes.

Pass `--url http://host:port` to point a script at an already-running mock instead; if it's
omitted or unreachable, the script falls back to spinning up its own.

### Testing per-user ACL

To see a **specific user's ACL-filtered view**, each example takes the credential its service
uses — a token, Google `--user`, Atlassian Basic auth, or an S3 keypair:

```bash
# Google: gmail.py (authorized_user) & gdrive.py (service account) both take --user <email>
python examples/using-official-sdk/gmail.py --url http://localhost:8000 --user ava@acme.com

# bearer-token services: slack.py, github.py, notion.py — grab a token from GET /_mock/users:
python examples/using-official-sdk/github.py --url http://localhost:8000 --token <usr-token>

# Atlassian Basic auth: jira.py, confluence.py take --username and --password
python examples/using-official-sdk/jira.py --url http://localhost:8000 \
    --username ava@acme.com --password <usr-token>

# S3: boto3 SigV4 uses an AWS keypair (required with --url; grab a pair from GET /_mock/users)
python examples/using-official-sdk/s3.py --url http://localhost:8000 \
    --access-key <AKIA...> --secret-key <secret>
```

The response then contains only what that identity is allowed to read. Grab tokens / emails /
S3 keypairs from the running server's [`GET /_mock/users`](../../README.md#auth--tokens) directory.
For Jira/Confluence either `--password <token>` or `--username <email>` alone identifies the user
(the mock resolves by the api token, falling back to the username email). Pair
`--user`/`--token`/`--password`/`--access-key`+`--secret-key` with `--url` so the identity exists
on the server you're querying. (Each example declares its own options — see `python <file> --help`.)

### How Google auth works here

The two Google examples show the **two credential shapes** real connectors use — the official
library's own token exchange runs against the mock's `POST /oauth2/token` in both:

- **`gmail.py` → authorized-user (3-legged OAuth)**: `client_id`/`client_secret` + a
  `refresh_token`. The shared `oauth_client` comes from
  [`GET /_mock/credentials`](../../README.md#oauth-client-config-google-style); the
  `refresh_token` is a user's token from `GET /_mock/users`. `--user <email>` picks the user
  (default: the first); there is no admin in this flow.
- **`gdrive.py` → service account**: the key from `/_mock/credentials` (standing in for the JSON
  you'd download from the Cloud Console) signs a JWT. `--user <email>` sets the impersonation
  subject (domain-wide delegation); without it the bare service account maps to the admin
  identity (sees everything).

## Base URL per SDK

| Service | SDK | How to point it at the mock |
|---|---|---|
| Slack | `slack_sdk` | `WebClient(token=T, base_url="http://localhost:8000/slack/api/")` |
| GitHub | `PyGithub` | `Github(auth=Auth.Token(T), base_url="http://localhost:8000/github")` |
| Jira | `atlassian-python-api` | `Jira(url="http://localhost:8000/atlassian", username="svc@x", password=T)` |
| Confluence | `atlassian-python-api` | `Confluence(url="http://localhost:8000/atlassian/wiki", username="svc@x", password=T)` |
| Gmail | `google-api-python-client` | `build("gmail","v1", …, client_options=ClientOptions(api_endpoint="http://localhost:8000"))` |
| Drive | `google-api-python-client` | `build("drive","v3", …, client_options=ClientOptions(api_endpoint="http://localhost:8000/drive/v3"))` |
| Notion | `notion-client` | `Client(auth=T, base_url="http://localhost:8000/notion")` (SDK appends `/v1/`) |
| S3 | `boto3` | `client("s3", endpoint_url="http://localhost:8000/s3", config=Config(s3={"addressing_style":"path"}))` |

(`T` is a token from `data/tokens.yaml` — the admin token sees everything; a per-user token is
scoped to that user's ACL. For Google, credentials come from a service account issued by
`/_mock/credentials`; pass `static_discovery=True`. A raw `Credentials(token=T)` also still works.)

## Coverage

[`tests/test_sdk.py`](../../tests/test_sdk.py) drives every SDK's read methods — including the
real-world +α (threads, comments, reactions, attachments, doc types, hierarchy, PR reviews) —
against a mock it starts itself, asserting all 39 checks pass across the 6 SDKs:

```bash
python -m pytest tests/test_sdk.py
```
