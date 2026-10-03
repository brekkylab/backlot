# AGENTS.md

Backlot serves enterprise SaaS APIs (Slack, Gmail, Google Drive, GitHub, Jira, and more) over a corpus the
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

- `backlot/routers/` — one module per vendor: response shapes, status codes, pagination, and for
  a vendor that takes writes, the write semantics and the refusal vocabulary.
- `backlot/schemas/*.schema.json` — the record schema per source. `docs/supported-sources.md` and
  the README source table are downstream of these.
- `backlot/acl.py` — which principal sees which document, in the vendor's own terms. Visibility now
  has two parts: the corpus's grants and the overlay's, unioned by a view `backlot/overlay.py`
  installs. Never in a router.
- `backlot/overlay.py` — served writes and the state they leave. Its tables are generated per
  source from `store.WRITABLE`; the merge that reads them is `store.merged_source`. A write must
  not put state anywhere else — a router that knows the overlay exists is in the wrong place.
- `backlot/importer/` — how a corpus gets in: the bundled set, BYO JSONL, `--id-map`, the roster.
  Not how a served write gets in; that is the overlay above.
- `tests/test_<source>.py` — written against measured vendor responses.
- `examples/` — one self-contained script per service per integration.

Adding a source is never one file. The full checklist lives in issue #89 and is the same every
time: schema, router, ACL mapping, BYO field mapping and ids, tests, SDK example, regenerated
docs, `pyproject.toml` keywords.

## Documentation rules (`tests/test_docs.py` enforces all of these)

- README.md stays within the line cap `tests/test_docs.py` enforces (`_README_MAX_LINES`).
  If a change needs more room, the content belongs in `docs/` — raising the cap is not the fix.
- Never state a source count in README.md. Counts go stale; the generated inventory carries the
  real one.
- Every relative link in every markdown file must resolve on disk. Do not link a path a stacked PR
  will add later.
- `docs/supported-sources.md` is generated. Never hand-edit between the generated markers — run
  `python scripts/gen_docs.py`.
- The served surface is not read-only, and user-facing docs should not call it that. Say what is
  true instead: the corpus is never written, and a served write lives in a per-server overlay. The
  endpoint tables already say which methods each source answers.

## Tests

- Run the suite before pushing: `pytest -q`. The docs tests above fail CI exactly like code tests.
- Vendor tests are written against a measurement where one is possible, and against the vendor's
  own published spec where it is not — a refusal that needs a write scope on a real workspace, say.
  Never against memory or this repo's earlier prose. Which of the two a claim rests on goes in the
  docstring, because a published spec lags and omits (see `docs/fidelity.md`). Bring a test that
  fails without your fix.
- Examples are self-contained: each script spins up its own throwaway server via
  `backlot.serve_or_connect` on a tiny in-code corpus. Keep new examples to that shape.

## A corpus flows one way

A **corpus** enters through `backlot import`, from the bundled set or a BYO JSONL, and never
through the vendor APIs. The serving connection is opened read-only, so this is enforced by SQLite
rather than agreed to.

Served **writes** exist, and they do not touch it. A write lands in `backlot/overlay.py`'s
in-memory database, attached to the same connection; reads merge the two, and the overlay dies with
the server. `/_meta/overlay` reports what is in it and `/_meta/overlay/reset` throws it away. See
[`docs/overlay.md`](docs/overlay.md).

Most served endpoints are still reads, including reads issued over POST — GraphQL queries, search,
batch read, token exchange. A POST is not evidence of a write either way; the endpoint tables in
`docs/supported-sources.md` say which is which.

## Commits and PRs

- Commit titles are single declarative sentences describing the new state. No trailers.
- PR and issue bodies reflow paragraphs to one line; no hard wrapping.
- Vendor names appear as plain text; brand assets are covered by `NOTICE.md`.
