"""Validate BYO corpus records against the per-service JSON Schemas in ``backlot/schemas/``.

The schemas (``backlot/schemas/<source_type>.schema.json``, Draft 2020-12) are the source of
truth for the record shape the loader accepts — they define the app's ingest contract, so this
lives on the application side. ``backlot/importer/byo.py`` calls :func:`record_errors` to fail
fast on load, and its ``--dry-run`` validates a whole file via :func:`validate_file` without
touching the DB.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

# __file__-relative, not cwd-relative: these are resources the package SHIPS (see the
# [tool.setuptools.package-data] entry in pyproject.toml), unlike backlot.config's data_dir/
# raw_dir, which are user data and must resolve against the cwd instead. `.parent`, not
# `.parent.parent` — schemas/ lives inside the backlot/ package, not the repo root, precisely so
# it is included in the wheel.
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


def _load_schemas() -> dict[str, dict]:
    """Load every ``*.schema.json`` in ``SCHEMA_DIR``, keyed by its ``source_type`` const."""
    schemas: dict[str, dict] = {}
    for p in sorted(SCHEMA_DIR.glob("*.schema.json")):
        schema = json.loads(p.read_text())
        const = schema.get("properties", {}).get("source_type", {}).get("const")
        schemas[const or p.name.split(".")[0]] = schema
    if not schemas:
        # Loading zero schemas is a packaging bug, not empty user data — every BYO record would
        # then fail validation with a confusing "source_type must be one of []" that points at the
        # data instead of the missing schemas/. Announce it here instead.
        raise RuntimeError(
            f"no *.schema.json files found under {SCHEMA_DIR} — the package is missing its "
            "bundled schemas (check [tool.setuptools.package-data] in pyproject.toml)"
        )
    return schemas


SERVICE_SCHEMAS: dict[str, dict] = _load_schemas()


@lru_cache(maxsize=None)
def _validator(source_type: str) -> Draft202012Validator:
    return Draft202012Validator(SERVICE_SCHEMAS[source_type], format_checker=FormatChecker())


def record_errors(rec: dict) -> list[str]:
    """Return human-readable validation errors for one BYO record ([] if valid)."""
    if not isinstance(rec, dict):
        return ["record must be a JSON object"]
    st = rec.get("source_type")
    if st not in SERVICE_SCHEMAS:
        return [f"source_type must be one of {list(SERVICE_SCHEMAS)}, got {st!r}"]
    msgs: list[str] = []
    for err in sorted(_validator(st).iter_errors(rec), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "<root>"
        msgs.append(f"{loc}: {err.message}")
    return msgs


def jsonl_lines(text: str) -> list[str]:
    """Split a JSONL document into records on ``\\n`` — and ONLY on ``\\n``.

    Not ``str.splitlines()``, which also breaks on U+2028/U+2029, U+0085 and the vertical tab.
    Those are ordinary characters inside a JSON string, and JSON Lines separates records by ``\\n``,
    so splitting on them tears one valid record into two invalid halves. Real text contains them:
    one U+2028 shows up in the bench corpus, and it was enough to make a converted artifact fail
    to load with "Unterminated string"."""
    return text.split("\n")


def validate_file(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, message), ...] for every problem in a JSONL corpus ([] == all valid)."""
    problems: list[tuple[int, str]] = []
    for lineno, raw in enumerate(jsonl_lines(Path(path).read_text()), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            problems.append((lineno, f"invalid JSON: {e}"))
            continue
        for msg in record_errors(rec):
            problems.append((lineno, msg))
    return problems
