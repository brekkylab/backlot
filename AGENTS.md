# AGENTS.md

Backlot serves enterprise SaaS APIs (Slack, Gmail, Drive, GitHub, Jira, and more) over a corpus the
user supplies, with per-document ACLs. Fidelity to the real APIs is the point of the project — read
this before changing anything.

## Fidelity is measured, never assumed

- A divergence from the real vendor API is a bug. A claim about what the real API does needs a
  measurement against the real service or a quote from the vendor's spec — never memory, never
  another mock, never this repo's own earlier prose.
- When a validation rule disagrees with a value the code produces, do not assume the rule is wrong.
  Find which side has an external source first. Widening a pattern to make a test pass has shipped
  a real bug here before.
- Docstrings that attribute a shape to the real vendor are source attributions. Do not delete or
  invert them without checking the vendor's spec.

## Where a change goes

- `backlot/routers/` — one module per vendor: response shapes, status codes, pagination.
- `backlot/schemas/*.schema.json` — the record schema per source. `docs/supported-sources.md` and
  the README source table are downstream of these.
- `backlot/acl.py` — which principal sees which document, in the vendor's own terms.
- `backlot/importer/` — how a corpus gets in: the bundled set, BYO JSONL, `--id-map`, the roster.
- `tests/test_<source>.py` — written against measured vendor responses.
- `examples/` — one self-contained script per service per integration.

Adding a source is never one file. The full checklist lives in issue #89 and is the same every
time: schema, router, ACL mapping, BYO field mapping and ids, tests, SDK example, regenerated
docs, `pyproject.toml` keywords.

## Documentation rules (`tests/test_docs.py` enforces all of these)

- README.md stays ≤ 130 lines. If a change needs more room, the content belongs in `docs/`.
- Never state a source count in README.md. Counts go stale; the generated inventory carries the
  real one.
- Every relative link in every markdown file must resolve on disk. Do not link a path a stacked PR
  will add later.
- `docs/supported-sources.md` is generated. Never hand-edit between the generated markers — run
  `python scripts/gen_docs.py`.
- Do not describe the served surface as "read-only" in user-facing docs. The wording was removed
  deliberately.

## Tests

- Run the suite before pushing: `pytest -q`. The docs tests above fail CI exactly like code tests.
- Vendor tests are written against measured responses, not vendor docs. Bring a test that fails
  without your fix.
- Examples are self-contained: each script spins up its own throwaway mock via
  `backlot.serve_or_connect` on a tiny in-code corpus. Keep new examples to that shape.

## Data flows one way

Served endpoints are reads, including reads issued over POST — GraphQL queries, search, batch read,
token exchange. Data enters through `backlot import`, from the bundled corpus or a BYO JSONL, never
through the vendor APIs.

## Commits and PRs

- Commit titles are single declarative sentences describing the new state. No trailers.
- PR and issue bodies reflow paragraphs to one line; no hard wrapping.
- Vendor names appear as plain text; brand assets are covered by `NOTICE.md`.
