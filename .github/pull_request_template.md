<!-- Describe the finished state: what Backlot does now, and why that is the right answer.
     Not the rounds of work that got here. Closes #<issue>. -->

## What changed

## Why this is what the real API does

<!-- The measurement behind it — the live call, the introspection, the generated client — and
     the date. Where a fix exists because someone diffed the two side by side, the comment beside
     it should say so. -->

## Verification

<!-- Paste the counts, don't summarize them. `pytest -n auto` on a `.[dev]` install, plus the
     extras covering what you touched: `.[examples]` for tests/test_sdk.py, `.[mcp]` + Docker for
     tests/test_mcp.py. A plain run skips those silently. Also
     `ruff check . && ruff format --check .`, which gates CI. -->

- [ ] A test fails without this change
- [ ] A new endpoint serving corpus content is ACL-scoped, proved for both an admin and a scoped token
- [ ] Comments state what was measured, not the history of the fix
