# Configuration

[← README](../README.md)

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

## Docker without a corpus

The [`Dockerfile`](../Dockerfile)'s default target bakes a corpus into the image. Build the
`serve` target instead for a server with **no** corpus, for a deployment that mounts its own
`/app/data` — pointed at by `BACKLOT_DATA_DIR` above:

```bash
docker build --target serve -t backlot .
```
