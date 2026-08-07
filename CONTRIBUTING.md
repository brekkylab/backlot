# Contributing

Thanks for your interest in improving **enterprise-mock**! Its essence is to serve each
provider's **read-only API** — Slack, Gmail, Google Drive, GitHub, Jira, Confluence — with
the **smallest possible gap** from the real thing, so clients built against the real APIs
work unchanged against the mock. EnterpriseRAG-Bench is simply one corpus you can load into
that surface (bring your own works too). Contributions that shrink the gap between the mock
and the real APIs — request/response shapes, status codes, pagination, error formats — are
especially welcome.

## Development setup

Requires Python **3.11+**.

```bash
git clone https://github.com/brekkylab/enterprise-mock.git
cd enterprise-mock

uv venv && source .venv/bin/activate     # or: python -m venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]"               # or: pip install -e ".[dev]"
```

The server itself needs no data or API keys to start:

```bash
python -m backlot.main                   # serves on http://localhost:8000
curl -s localhost:8000/health
```

To exercise it against a real corpus, build one first (`python -m backlot.importer.erb`
for a bench slice, or `python -m backlot.importer.byo mycorpus.jsonl` for your own — see
the README).

## Running tests

```bash
pytest                    # unit + HTTP endpoint tests; needs no data
```

- **Unit + endpoint tests** run with no data and no network — these must pass for every change.
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
- Response shapes are validated against the JSON Schemas in [`backlot/schemas/`](backlot/schemas/); update
  the relevant schema alongside any response-shape change.
- ACL scoping is enforced per bearer token (the admin token bypasses). New endpoints that
  expose corpus content must respect the same ACL rules — add a test proving an
  ACL-restricted item is readable by the admin token and blocked for a scoped user token.

## Reporting bugs & requesting features

Open an issue at https://github.com/brekkylab/enterprise-mock/issues. For a bug, include
the endpoint, the request you made, what you got, and what a real API would have returned.

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
