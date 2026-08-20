# Auth & tokens

[← README](../README.md)

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
[`examples/using-official-sdk/`](../examples/using-official-sdk/).
