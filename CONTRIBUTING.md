# Contributing

Thanks for your interest in improving **Backlot**! Its essence is to serve each vendor's
**read-only API** — Slack, Gmail, Google Drive, GitHub, Jira, Confluence, Notion, Amazon S3,
HubSpot, Linear, Fireflies — with the **smallest possible gap** from the real thing, so clients
built against the real APIs work unchanged against the mock. The corpus is yours to supply;
EnterpriseRAG-Bench is just one dataset you can load into that surface. Contributions that shrink
the gap between the mock and the real APIs — request/response shapes, status codes, pagination,
error formats — are especially welcome.

## Development setup

Requires Python **3.11+**.

```bash
git clone https://github.com/brekkylab/backlot.git
cd backlot

uv venv && source .venv/bin/activate     # or: python -m venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]"               # or: pip install -e ".[dev]"
```

That puts the `backlot` command on PATH. The server needs no API keys, but it does need a
corpus — the bundled hello-world one (136 records covering every source) is enough to get a
live server:

```bash
backlot import backlot/data/hello.jsonl   # -> data/mock.sqlite + data/tokens.yaml
backlot serve                             # serves on http://127.0.0.1:8000
curl -s localhost:8000/health
```

For a real corpus use `backlot import --type enterpriserag-bench` (the bench) or
`backlot import mycorpus.jsonl` (your own) — see the README.

Every command and every option lives in `backlot/cli.py`, declared as Typer parameters; the
importers expose a plain `run(**kwargs)` and parse nothing. So `backlot import --help` is the
complete list, and adding a flag means touching one file. `python -m backlot.importer.{byo,erb}`
still work — they re-enter the same CLI, so they cannot take a different set.

## Terminology

Two words, and they are not interchangeable:

- **source** — one of the things Backlot serves: `slack`, `gmail`, `jira`, … This is the codebase's
  own word for it (`source_type` on every record, `store.SOURCE_TABLE`, `/_mock/openapi/<source>`),
  so use it for anything on our side of the line: a corpus record, a router, a table, an example
  script, a test file.
- **vendor** — the real company whose API a source imitates, and the artifacts that belong to them:
  a *vendor SDK*, a *vendor MCP server*, "the real vendor APIs", "per-vendor page caps". Never a
  synonym for source — "the real source API" would name the wrong owner.

## Running tests and lint

```bash
pytest                    # unit + HTTP endpoint tests; needs no data
ruff check . && ruff format --check .    # both gate CI; ruff comes from the `dev` extra
```

- **Unit + endpoint tests** run with no data and no network — these must pass for every change.
  The unit half covers synth, pagination, ACL, the schemas and the importer parsers; the endpoint
  half covers full-crawl completeness, content round-trip and ACL enforcement.
- `tests/test_sdk.py` needs the `.[examples]` extra and `tests/test_mcp.py` needs Docker +
  the `.[mcp]` extra; both spin up their own server and **self-skip** when their prerequisites
  are absent. Run them when touching the relevant surface.

CI runs `pytest -q` on every push to `main` and every pull request (see
`.github/workflows/ci.yml`).

## Pull requests

1. Fork and create a topic branch off `main`.
2. Keep changes focused; one logical change per PR.
3. Add or update tests — a bug fix should come with a test that fails without it.
4. Make sure `pytest` passes locally before opening the PR.
5. Write a clear description of *what* changed and *why*.

## Adding or changing API behavior

The whole point of this project is **fidelity to the real APIs**, so:

- When you add or change an endpoint, mirror the real service's request/response shape,
  status codes, pagination, and error format as closely as practical.
- Response shapes are pinned by each route's `response_model` and by the endpoint tests — not by
  [`backlot/schemas/`](backlot/schemas/), which is the **ingest** contract (the BYO-JSONL record a
  corpus may carry). Touch a schema when you change what a corpus can express, and a
  `response_model` when you change what a client receives.
- A divergence from the real API is a bug, even when the mock's answer is reasonable. Measure
  against the real service where you can, and record the measurement in the comment beside the fix —
  several of them exist precisely because someone diffed the two side by side.
- ACL scoping is enforced per bearer token (the admin token bypasses). New endpoints that
  expose corpus content must respect the same ACL rules — add a test proving an
  ACL-restricted item is readable by the admin token and blocked for a scoped user token.

### Adding a source

A new source is a new `backlot/schemas/<source_type>.schema.json`. That file alone makes
`<source_type>` something the importer accepts, so `tests/test_docs.py` fails until the docs catch
up. To get it green:

1. add the source to `SOURCES` in [`scripts/gen_docs.py`](scripts/gen_docs.py) — display name and
   URL prefixes;
2. run `python scripts/gen_docs.py` to rewrite the generated table;
3. write its row in the per-service table in [`docs/endpoints.md`](docs/endpoints.md), where the
   fidelity notes live.

The `README.md` needs no change — it names no source count and lists no sources, by design, so it
does not go stale when this list grows.

## Reporting bugs & requesting features

Open an issue at https://github.com/brekkylab/backlot/issues. For a bug, include
the endpoint, the request you made, what you got, and what a real API would have returned.

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
