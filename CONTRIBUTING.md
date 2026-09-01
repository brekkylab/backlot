# Contributing

Thanks for your interest in improving **Backlot**! Its essence is to serve each vendor's
**API** — Slack, Gmail, Google Drive, GitHub, Jira, Confluence, Notion, Amazon S3,
HubSpot, Linear, Fireflies — with the **smallest possible gap** from the real thing, so clients
built against the real APIs work unchanged against Backlot. The corpus is yours to supply;
EnterpriseRAG-Bench is just one dataset you can load into that surface. Contributions that shrink
the gap between Backlot and the real APIs — request/response shapes, status codes, pagination,
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
backlot import backlot/data/hello.jsonl   # -> data/db.sqlite + data/tokens.yaml
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
  own word for it (`source_type` on every record, `store.SOURCE_TABLE`, `/_meta/openapi/<source>`),
  so use it for anything on our side of the line: a corpus record, a router, a table, an example
  script, a test file.
- **vendor** — the real company whose API a source imitates, and the artifacts that belong to them:
  a *vendor SDK*, a *vendor MCP server*, "the real vendor APIs", "per-vendor page caps". Never a
  synonym for source — "the real source API" would name the wrong owner.

## Running tests and lint

```bash
pytest                    # unit + HTTP endpoint tests; needs no data and no network
pytest -rs                # the same, and it names every test that skipped, with the reason
ruff check . && ruff format --check .    # both gate CI; ruff comes from the `dev` extra
```

Everything that runs on a `dev` install must pass for every change, and it needs no data and no
network: the unit half covers synth, pagination, ACL, the schemas and the importer parsers, and the
endpoint half covers full-crawl completeness, content round-trip and ACL enforcement. Tests that
drive something outside the package **self-skip** when what they need is absent, so a `dev`-only run
is quietly narrower than the suite — three of those gates sit at module level and take the whole
file with them:

| Needs | Sits out without it |
|---|---|
| `.[examples]` — the vendor SDKs | all of `tests/test_sdk.py` and all of `tests/test_s3.py`, plus the `googleapiclient` tests in `tests/test_integrations.py` |
| `.[llamaindex]` — the official readers | all of `tests/test_llamaindex.py`, plus the reader tests in `tests/test_integrations.py` |
| `llama-index-readers-hubspot`, which no extra carries | the HubSpot reader test in `tests/test_llamaindex.py` |
| `.[mcp]` | all of `tests/test_mcp.py` |
| `.[mirage]` | the shim tests in `tests/test_integrations.py` |
| `.[fsspec]` — `fsspec` and `gdrive-fsspec` | the Google Drive and GitHub filesystem tests in `tests/test_integrations.py` |
| Docker, `npx`, `uvx` | one `tests/test_mcp.py` test each — the Atlassian, Notion and AWS MCP servers |
| `git` | the tests in `tests/test_github.py` that build a real repo |

Install what covers the surface you touched, or all of it at once, and let `-rs` confirm nothing
you meant to run skipped. The zero-skip install is two commands, the way CI's is — `.[all]`
is every extra above, kept in step with them by `tests/test_packaging.py`, and the HubSpot
reader pins `hubspot-api-client<9` against the `>=12` that `examples` needs — over-restrictive
rather than a real incompatibility, so it goes in past its own dependencies, at the version CI
installs:

```bash
uv pip install -e ".[all]"
uv pip install --no-deps "llama-index-readers-hubspot<0.6"
```

CI runs the suite, ruff, and the Linear example on every push to `main` and every pull request
(see `.github/workflows/ci.yml`).

### Testing the agent skill

The repository is its own plugin marketplace, so a working tree can be installed as one. Point the
harness at a **git URL with your branch as the fragment**, not at the checkout's path:

```bash
claude plugin marketplace add "https://github.com/brekkylab/backlot.git#<branch>"
claude plugin install backlot@brekkylab
```

A directory source is copied verbatim, gitignored files included, which pulls your `.venv` into
`~/.claude/plugins/cache/` and costs hundreds of megabytes. Cloning a ref takes the tracked tree
alone, a few megabytes, and is what a user installing from `main` gets. Remove both the plugin and
the marketplace when you are done — uninstalling leaves the cache directory behind.

## Pull requests

1. Fork and create a topic branch off `main`.
2. Keep changes focused; one logical change per PR.
3. Add or update tests — a bug fix should come with a test that fails without it.
4. Make sure `pytest` passes locally before opening the PR.
5. Fill in the pull request template: what changed, the measurement that says it is right, and
   the test output — describing the finished state rather than the rounds of work behind it.

## Adding or changing API behavior

The whole point of this project is **fidelity to the real APIs**, so:

- When you add or change an endpoint, mirror the real service's request/response shape,
  status codes, pagination, and error format as closely as practical.
- Response shapes are pinned by each route's `response_model` and by the endpoint tests — not by
  [`backlot/schemas/`](backlot/schemas/), which is the **ingest** contract (the BYO-JSONL record a
  corpus may carry). Touch a schema when you change what a corpus can express, and a
  `response_model` when you change what a client receives.
- A divergence from the real API is a bug, even when Backlot's answer is reasonable. Measure
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
3. add its subsection to [`docs/supported-sources.md`](docs/supported-sources.md) — a table of the
   endpoints it serves, and the fidelity notes under it.

The `README.md` needs no change — it names no source count and lists no sources, by design, so it
does not go stale when this list grows.

## Reporting bugs & requesting features

Open an issue at https://github.com/brekkylab/backlot/issues. The **fidelity gap** template
is the one most issues here want: the endpoint, the request you made, what Backlot served, what
the real API served, and how you measured the difference.

## Code of Conduct

Taking part in this project means following our [Code of Conduct](CODE_OF_CONDUCT.md).

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
