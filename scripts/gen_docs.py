#!/usr/bin/env python3
"""Regenerate the machine-maintained block of docs/supported-sources.md.

    python scripts/gen_docs.py            # rewrite the block
    python scripts/gen_docs.py --check    # exit 1 if stale, or if SOURCES is out of step

Why a mapping lives here instead of being derived: nothing in the app answers "which URL prefix
belongs to which source_type". ``jira`` and ``confluence`` both sit under ``/atlassian``; one
``google_drive`` spans ``/drive``, ``/docs``, ``/sheets`` and ``/slides``; and ``/batch``,
``/oauth2``, ``/health`` and ``/_meta`` are not sources at all. ``backlot.openapi.SOURCE_PREFIXES``
cannot stand in — it is scoped to the MCP bridge, so it omits S3 deliberately and merges Jira with
Confluence.

Route introspection is out too: FastAPI wraps an included router in a ``_IncludedRouter`` exposing
neither ``.path`` nor ``.routes``, so walking ``app.routes`` would depend on FastAPI internals.
REST prefixes are checked against ``/openapi.json`` instead — which needs no running server, only
``app.openapi()``.

The two GraphQL sources register their single POST with ``include_in_schema=False``, so they are
absent from the spec and cannot be checked that way. Proving those routes are mounted needs a
served corpus, which a docs generator has no business building; ``tests/test_docs.py`` does it
instead, against the paths this file wrote into the table.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from backlot.main import app
from backlot.validation import SERVICE_SCHEMAS

REPO = Path(__file__).resolve().parent.parent
SOURCES_DOC = REPO / "docs" / "supported-sources.md"

# source_type -> (display name, URL prefixes). The only place this mapping exists.
SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "confluence": ("Confluence", ("/atlassian/wiki/rest/api",)),
    "fireflies": ("Fireflies", ("/fireflies/graphql",)),
    "github": ("GitHub", ("/github",)),
    "gmail": ("Gmail", ("/gmail/v1",)),
    "google_drive": (
        "Google Drive, Docs, Sheets, Slides",
        ("/drive/v3", "/docs/v1", "/sheets/v4", "/slides/v1"),
    ),
    "hubspot": ("HubSpot", ("/hubspot",)),
    "jira": ("Jira", ("/atlassian/rest/api",)),
    "linear": ("Linear", ("/linear/graphql",)),
    "notion": ("Notion", ("/notion/v1",)),
    "s3": ("Amazon S3", ("/s3",)),
    "slack": ("Slack", ("/slack/api",)),
}

# One POST each, include_in_schema=False, so they contribute no /openapi.json paths.
GRAPHQL_ONLY = frozenset({"linear", "fireflies"})

_START = "<!-- generated:{name} start -->"
_END = "<!-- generated:{name} end -->"


def _first_sentence(text: str) -> str:
    """The first sentence of a schema description.

    Splits on a period followed by whitespace, so "A Fireflies.ai meeting transcript. `channel`
    is…" yields the whole first sentence instead of breaking at ".ai".
    """
    return re.split(r"(?<=\.)\s", text.strip(), maxsplit=1)[0]


def validate() -> list[str]:
    """Ways SOURCES disagrees with the code, as human-readable lines."""
    problems = []
    for source_type in sorted(SERVICE_SCHEMAS):
        if source_type not in SOURCES:
            problems.append(f"{source_type!r} has a schema but no SOURCES entry — add one")
    for source_type in sorted(SOURCES):
        if source_type not in SERVICE_SCHEMAS:
            problems.append(
                f"{source_type!r} is in SOURCES but has no backlot/schemas/{source_type}.schema.json"
            )

    spec_paths = sorted(app.openapi()["paths"])
    for source_type, (_, prefixes) in sorted(SOURCES.items()):
        if source_type in GRAPHQL_ONLY:
            continue
        for prefix in prefixes:
            if not any(path.startswith(prefix) for path in spec_paths):
                problems.append(
                    f"{source_type!r}: prefix {prefix!r} matches no path in /openapi.json"
                )

    for source_type in sorted(GRAPHQL_ONLY):
        prefixes = SOURCES[source_type][1]
        # These two are served as exactly one POST endpoint. More than one prefix, or one that is
        # not the GraphQL path, means the entry was edited without reading the note above — and
        # tests/test_docs.py POSTs whatever lands here, so a wrong path fails there loudly.
        if len(prefixes) != 1 or not prefixes[0].endswith("/graphql"):
            problems.append(
                f"{source_type!r} is GraphQL-only, so it needs exactly one /graphql prefix; "
                f"got {list(prefixes)}"
            )
    return problems


def render_sources() -> str:
    spec_paths = sorted(app.openapi()["paths"])
    rows = [
        "| `source_type` | Service | URL prefix | Endpoints | Record schema | What one record is |",
        "|---|---|---|---|---|---|",
    ]
    for source_type in sorted(SOURCES):
        name, prefixes = SOURCES[source_type]
        if source_type in GRAPHQL_ONLY:
            endpoints = "GraphQL (one `POST`)"
        else:
            endpoints = str(
                sum(1 for path in spec_paths if any(path.startswith(p) for p in prefixes))
            )
        prefix_cell = " ".join(f"`{p}`" for p in prefixes)
        schema_link = f"[`{source_type}.schema.json`](../backlot/schemas/{source_type}.schema.json)"
        summary = _first_sentence(SERVICE_SCHEMAS[source_type].get("description", ""))
        rows.append(
            f"| `{source_type}` | {name} | {prefix_cell} | {endpoints} | {schema_link} | {summary} |"
        )
    return "\n".join(rows)


def replace_block(text: str, name: str, body: str) -> str:
    start, end = _START.format(name=name), _END.format(name=name)
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if not pattern.search(text):
        raise SystemExit(f"no {name!r} marker pair in {SOURCES_DOC}")
    # A lambda, not a replacement string: the body is markdown full of backslashes and pipes that
    # re.sub would otherwise read as group references.
    return pattern.sub(lambda _: f"{start}\n{body}\n{end}", text)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate docs/supported-sources.md's generated block."
    )
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if stale or invalid; write nothing"
    )
    args = parser.parse_args()

    if problems := validate():
        print("SOURCES is out of step with the code:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    current = SOURCES_DOC.read_text()
    updated = replace_block(current, "sources", render_sources())
    if args.check:
        if current != updated:
            print(
                "docs/supported-sources.md is stale — run: python scripts/gen_docs.py",
                file=sys.stderr,
            )
            return 1
        return 0
    SOURCES_DOC.write_text(updated)
    print(f"wrote {SOURCES_DOC.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
