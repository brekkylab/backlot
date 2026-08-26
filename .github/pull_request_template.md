<!-- Describe the finished state: what Backlot does now, and why that is the right answer.
     Not the rounds of work that got here. Closes #<issue>. -->

## What changed

## Why this is what the real API does

<!-- The measurement behind it — the live call, the introspection, the generated client — and
     the date. Where a fix exists because someone diffed the two side by side, the comment beside
     it should say so. -->

## Verification

<!-- Fill in the lines below rather than summarizing them: a skip count means nothing without the
     install that produced it. `.[dev]` alone leaves whole test files sitting out (all of
     `tests/test_s3.py`, `tests/test_sdk.py`, `tests/test_mcp.py`, `tests/test_llamaindex.py`,
     and the reader, Google and mirage tests in `tests/test_integrations.py`), so install what
     covers the surface you touched — CONTRIBUTING.md maps each extra to what it gates. -->

- **Tests** — `pytest` on a `.[…]` install:
- **Optional surfaces that ran rather than skipped** —
- **Lint** — `ruff check . && ruff format --check .`:

- [ ] A test fails without this change
- [ ] A new endpoint serving corpus content is ACL-scoped, proved for both an admin and a scoped token
- [ ] Comments state what was measured, not the history of the fix
