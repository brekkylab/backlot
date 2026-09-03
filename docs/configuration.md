# Configuration

[← README](../README.md)

Every setting is an env var with a `BACKLOT_` prefix, and a `.env` file in the working directory is
read too — copy [`.env.example`](../.env.example) and edit. Defaults are what the server uses when
the var is unset. There are nine, and this page is all of them.

## Corpus and identity

| Env var | Default | What it does |
|---|---|---|
| `BACKLOT_DATA_DIR` | `./data` (resolved against the cwd, **not** the install location) | Where the corpus lives: `db.sqlite`, `tokens.yaml`, `credentials.yaml`. Both `backlot import` and `backlot serve` read it, which is how you keep several corpora side by side — `BACKLOT_DATA_DIR=/tmp/demo backlot import c.jsonl` |
| `BACKLOT_ADMIN_TOKEN` | `admin-service-token` | The token that bypasses ACL filtering — a full-crawl / service identity. Set it to anything for a shared deployment |
| `BACKLOT_ORG_NAME` | inferred from the corpus (fallback `example`) | The org slug that shows up in `auth.test`, synthesized emails and self-URLs. Inferred from the dominant author email domain — `@acme.com` documents serve as org `acme` — so set it only to override that |
| `BACKLOT_ORG_DOMAIN` | inferred from the corpus (fallback `example.com`) | The domain half of the same inference, e.g. `acme.com`. Used for addresses the corpus does not state |

## Paging

| Env var | Default | What it does |
|---|---|---|
| `BACKLOT_DEFAULT_PAGE_SIZE` | `100` | Page size when a request names none |
| `BACKLOT_MAX_PAGE_SIZE` | `1000` | Ceiling a request may ask for |

**A vendor's own cap still wins.** Where the real API documents a maximum, Backlot enforces that
one instead: Fireflies clamps `limit` to 50 rather than erroring, and HubSpot to 100 — the value its
official client pages at. Raising `BACKLOT_MAX_PAGE_SIZE` does not lift either, because a client
that gets 1000 rows from a call the real API caps at 50 is a client that breaks in production.

## SQLite

These are performance levers, not behaviour. Every one of them is safe to leave alone.

| Env var | Default | What it does |
|---|---|---|
| `BACKLOT_SQLITE_MMAP_MB` | `256` | Memory-maps the DB so reads come from the OS page cache instead of a syscall each — the main lever against a slow first request after idle. SQLite maps `min(this, db size)`; raise it to at or above your DB size to map a big corpus fully |
| `BACKLOT_SQLITE_CACHE_MB` | `64` | SQLite's own page cache, per serving connection |
| `BACKLOT_SQLITE_BUSY_MS` | `5000` | How long a read waits for a lock instead of erroring, so reads ride through an out-of-band write (e.g. an in-place FTS rebuild) rather than 500ing |

## Docker

The [`Dockerfile`](../Dockerfile) has three stages, and it bakes `BACKLOT_DATA_DIR=/app/data` in:

| Target | Carries | For |
|---|---|---|
| `full` (**default**) | The bundled corpus, already imported | `docker run` and it answers — nothing to mount, nothing to import |
| `serve` | No corpus | A deployment that mounts its own `/app/data` |
| `builder` | An intermediate that runs `backlot import --bundled` | Not a target you run |

```bash
docker build -t backlot .                       # full: corpus baked in
docker build --target serve -t backlot .        # empty: mount your own /app/data
docker build --build-arg VERSION=0.0.1 -t backlot .   # stamp the image with a version
```

`.git` stays out of the build context, so an image built without `--build-arg VERSION` reports the
fallback version `pyproject.toml` names rather than a release number. `backlot --version` inside the
container is the only place that shows.

[`docker-compose.yml`](../docker-compose.yml) builds the default target and passes the host's
`.env` through, so `docker compose up` comes up on the package defaults with no file to write. It
hardcodes no setting on purpose: what a deployment needs is a property of its own corpus and
exposure, and a multi-GB corpus wants different SQLite tuning than a laptop. Its commented-out
`volumes:` entry is the `--target serve` path above.

**The default image cannot do the OAuth-config path.** `full` copies only the two runtime files the
import produced — `db.sqlite` and `tokens.yaml` — and deliberately leaves `credentials.yaml`
behind in `builder`. So in that image `GET /_meta/credentials` is a 404 and `POST /oauth2/token`
answers `temporarily_unavailable: no OAuth credentials configured`. Bearer-token auth, `/_meta/users`
and every vendor API are unaffected; it is only the Google client-config exchange
([auth.md](auth.md)) that needs the file. Mount a data dir built by your own
`backlot import` if you need it.
