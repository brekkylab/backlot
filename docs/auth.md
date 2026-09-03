# Auth & tokens

[← README](../README.md)

Each service authenticates the way the real one does, and you construct none of it: the corpus
generates every credential, and two of Backlot's own endpoints hand them out.

Every token on this page is what `backlot import --bundled` produces. They are **derived, not
random** — hashed from the corpus's own email addresses — so these exact values are the ones on your
machine too, and the commands below are copy-pasteable against `backlot serve`.

## Where credentials come from

```bash
curl -s localhost:8000/_meta/users
```
```json
{ "org": "acme", "admin_token": "admin-service-token",
  "admin_s3_access_key_id": "AKIA732S…", "admin_s3_secret_access_key": "l4sz5sXT…",
  "users": [{ "email": "ava.chen@acme.com", "name": "Ava Chen",
              "token": "usr-29b84da5703116c2a832",
              "s3_access_key_id": "AKIADNLO…", "s3_secret_access_key": "0FdhlUUQ…",
              "groups": ["handbook", "design", "product", "engineering"] }] }
```

```bash
curl -s localhost:8000/_meta/credentials   # for a Google client that wants a config, not a token
```
```json
{ "org": "acme", "token_uri": "http://localhost:8000/oauth2/token",
  "oauth_client": { "client_id": "e8ae7a3acfbfffb01ddab8df6961c15d.apps.googleusercontent.com",
                    "client_secret": "GOCSPX-…" },
  "service_account": { "type": "service_account", "client_email": "…", "private_key": "…" } }
```

Both serve working credentials in the clear, and always answer: `backlot mcp --user <email>`
resolves a person to their credentials through `/_meta/users`, so a switch that closed it would
take that command's only credential input with it. The same values are in `data/tokens.yaml`.

## The three identities used below

| Identity | Token | Sees |
|---|---|---|
| admin / service | `admin-service-token` | Everything — ACL filtering is bypassed. Use it to crawl |
| `ava.chen@acme.com` | `usr-29b84da5703116c2a832` | `handbook`, `design`, `product`, `engineering` |
| `dana.whitfield@acme.com` | `usr-94ef7da374a581b973c0` | `sales` |

The token is what authenticates. Atlassian's Basic is the one scheme that also checks the
username, because the real service does: it has to be the token's own address (below). Send a
user's token and every API filters to that user, which is what makes per-user access a test rather
than an audit. Send the same request three times, changing only the token, and count what comes
back:

```bash
for t in admin-service-token usr-29b84da5703116c2a832 usr-94ef7da374a581b973c0; do
  curl -s localhost:8000/drive/v3/files -H "Authorization: Bearer $t" | jq '.files | length'
done
```

The admin sees the whole corpus, ava sees what her four groups reach, and dana — in `sales` alone —
sees least. Nothing about the request changes but the identity.

## What each service expects

| Service | Header |
|---|---|
| Slack, Gmail, Google Drive/Docs/Sheets/Slides, Notion, HubSpot, Fireflies | `Authorization: Bearer <token>` |
| GitHub | `Authorization: Bearer <token>`, or the legacy `token <token>` |
| Jira, Confluence | HTTP Basic with the token as the **password**, or a plain `Bearer` |
| Linear | A **bare** `Authorization: <token>`, or `Bearer` |
| Amazon S3 | AWS SigV4, signed with the identity's key pair |
| A Google client with a config | Exchange it at `POST /oauth2/token` — see the Google section |

### Slack — `Bearer`

```bash
curl -s localhost:8000/slack/api/auth.test \
  -H "Authorization: Bearer usr-29b84da5703116c2a832"
```

The scheme is required: a bare token answers `not_authed`. As with the real Web API, an auth failure
is **HTTP 200 with `ok: false`** — `not_authed` when the header is missing or unscheme'd,
`invalid_auth` when the token resolves to nobody. The token is also accepted as a `token` query
parameter or form field, which is where Slack's own clients put it.

**It is a user token, not a bot token.** Slack has two kinds and they are not interchangeable; a
Backlot token behaves as the first:

| | Real Slack | Backlot |
|---|---|---|
| User token `xoxp-…` | Acts as a person — sees every channel that person is in | What a `usr-…` token is |
| Bot token `xoxb-…` | Acts as the app — sees only channels the bot was **invited** to, and carries a `bot_id` | Not modelled |

Two consequences worth knowing before you trust a passing test here:

- **`search.messages`, `search.all` and `search.files` are user-token-only in real Slack.** A bot
  token calling them gets `not_allowed_token_type`. Backlot serves all three, so search working here
  says nothing about whether it will work for a connector that authenticates as a bot in production.
- **There is no "invite the bot to the channel" step.** Backlot scopes by per-document readers — the
  user model — so a caller reaches every channel their ACL allows. A bot in production starts with
  access to nothing until it is invited, which is a failure mode Backlot cannot reproduce.

So if the connector under test uses a bot token in production, Backlot is the more permissive of the
two on both counts. `auth.test` reflects the user shape it emulates: `user`, `user_id`, `team`,
`team_id`, and no `bot_id`.

### Gmail, Google Drive, Docs, Sheets, Slides — `Bearer`

```bash
curl -s "localhost:8000/gmail/v1/users/me/profile" \
  -H "Authorization: Bearer usr-29b84da5703116c2a832"

curl -s "localhost:8000/drive/v3/files?fields=files(id,name)" \
  -H "Authorization: Bearer usr-29b84da5703116c2a832"
```

`me` works as the mailbox id, resolving to whoever the token is.

#### A client carrying a config, not a token — `POST /oauth2/token`

For a connector that configures with an OAuth client or a service account rather than a raw token,
`token_uri` from `/_meta/credentials` points back at Backlot, so the client's own refresh lands
here and resolves to the same ACL:

```bash
curl -s localhost:8000/oauth2/token \
  -d grant_type=refresh_token \
  -d refresh_token=usr-29b84da5703116c2a832 \
  -d client_id=e8ae7a3acfbfffb01ddab8df6961c15d.apps.googleusercontent.com \
  -d client_secret=GOCSPX-…
```

A user's refresh token **is** their bearer token, so the exchange hands the same value straight back
as the `access_token`. Expiry is cosmetic — a re-refresh returns the same token, so a long crawl
never breaks. A signed service-account assertion works too, with its `sub` claim selecting the
impersonated user; a bare service account with no `sub` resolves to the admin identity. See
[supported-sources.md](supported-sources.md#oauth-and-batch).

### GitHub — `Bearer` or `token`

```bash
curl -s localhost:8000/github/user/repos \
  -H "Authorization: Bearer usr-29b84da5703116c2a832"
```

Both schemes are accepted, as GitHub accepts both. `user/repos` is the token's own reach, so it is
the quickest check that an identity resolves.

### Jira and Confluence — Basic, or `Bearer`

Atlassian clients send HTTP Basic with an API token as the password, so that is what Backlot takes
— and the **username has to be that token's own address**, which is what the real service matches
on. Measured against a real Atlassian Cloud site (`GET /rest/api/3/myself`) with a user API token:
`email:token` answers 200, while an empty password, a wrong password, and a valid token under
someone else's address all answer 401.

```bash
curl -s -u "ava.chen@acme.com:usr-29b84da5703116c2a832" \
  localhost:8000/atlassian/rest/api/3/project/search

curl -s -u "ava.chen@acme.com:usr-29b84da5703116c2a832" \
  localhost:8000/atlassian/wiki/rest/api/space
```

The admin/service token is the exception, and the only one: it resolves to no address, so there is
nothing to match and any username goes through. That is what lets an Atlassian client send the
placeholder its config demands — `svc@example.com:admin-service-token` works.

A plain `Authorization: Bearer <token>` is accepted too, which is easier when you are driving the
API by hand rather than through an Atlassian SDK. Sending neither is a `401` — with one exception,
`rest/api/3/serverInfo`, which answers unauthenticated as real Jira's does, so it is the one
Atlassian endpoint that cannot tell you whether your credential works.

### Notion — `Bearer`

```bash
curl -s localhost:8000/notion/v1/users/me \
  -H "Authorization: Bearer usr-29b84da5703116c2a832" \
  -H "Notion-Version: 2025-09-03"
```

`Notion-Version` is optional here and defaults to `2025-09-03`, the data-sources model. Real Notion
requires it, so send it if you are exercising a client's own version handling.

### HubSpot — `Bearer`

```bash
curl -s "localhost:8000/hubspot/crm/v3/objects/contacts" \
  -H "Authorization: Bearer usr-29b84da5703116c2a832"
```

### Linear — bare, or `Bearer`

Linear's personal API keys go in the header with **no scheme**, and Backlot accepts that spelling
as well as `Bearer`, exactly as the real API does:

```bash
curl -s localhost:8000/linear/graphql \
  -H "Authorization: usr-29b84da5703116c2a832" \
  -H "Content-Type: application/json" \
  -d '{"query":"{ viewer { id name } }"}'
```

### Fireflies — `Bearer`

```bash
curl -s localhost:8000/fireflies/graphql \
  -H "Authorization: Bearer usr-29b84da5703116c2a832" \
  -H "Content-Type: application/json" \
  -d '{"query":"{ users { name } }"}'
```

### Amazon S3 — SigV4

S3 does not take a bearer token. Each identity carries an `s3_access_key_id` /
`s3_secret_access_key` pair, derived from that identity's token — which is what Backlot's SigV4
verifier resolves back to a user — so hand them to any AWS client:

```python
import boto3, httpx
from botocore.config import Config

ava = next(u for u in httpx.get("http://localhost:8000/_meta/users").json()["users"]
           if u["email"] == "ava.chen@acme.com")

s3 = boto3.client(
    "s3",
    endpoint_url="http://localhost:8000/s3",
    aws_access_key_id=ava["s3_access_key_id"],
    aws_secret_access_key=ava["s3_secret_access_key"],
    region_name="us-east-1",                       # any region; it is read back out of the scope
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)
print([b["Name"] for b in s3.list_buckets()["Buckets"]])
```

**Path addressing is not optional.** Backlot serves `/s3/{bucket}/{key}`, so virtual-hosted
addressing — which puts the bucket in the host, `acme-artifacts.localhost:8000` — reaches nothing.
boto3's default picks path for this endpoint today, so the setting looks redundant right up until
someone carries a virtual-hosted config over from real S3.

The pair is read at runtime rather than pasted here, and that is worth knowing rather than a
formality: these keys are **AWS-shaped on purpose** — `AKIA` + 16, and a 40-character secret —
because botocore validates the shape before it signs. Secret scanners cannot tell them from live
credentials, so GitHub push protection rejects a commit containing one. Keep them out of anything
you commit, yours included.

## Per-service, runnable

One script per service, driving the vendor's own SDK against Backlot:
[`examples/using-official-sdk/`](../examples/using-official-sdk/).
