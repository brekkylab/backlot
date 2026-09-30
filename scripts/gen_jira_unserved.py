#!/usr/bin/env python3
"""Regenerate backlot/data/jira_unserved.json from Jira's own documents.

    python scripts/gen_jira_unserved.py            # rewrite the file
    python scripts/gen_jira_unserved.py --check    # exit 1 if the file is stale, 2 if unreadable

The file sorts every operation the Jira baseline acknowledges as ``missing_operation`` into
``refused`` and ``run``, because those are the ones no route here serves, which is where
``backlot.routers.atlassian.unmatched_path`` needs to know what real answers before the operation
runs. Regenerate after the baseline gains or loses one; ``tests/test_atlassian.py`` fails until
then, and `.github/workflows/fidelity.yml` runs ``--check`` on its schedule, which is where a change
to the documents shows.

``refused`` are the operations that refuse a caller with no credential they resolve with Jira's 401
(``backlot.errors.atlassian.JIRA_UNAUTHENTICATED``), and ``run`` the rest, which run for that
caller. Which is which is read off each operation's ``security`` in the documents
``backlot.fidelity.comparisons.COMPARISONS["jira"]`` names: one whose requirements do not include
the empty one, ``{}``, is refused, and so is one the documents give no ``security`` at all. Measured
on Jira Cloud on 2026-09-30 with no credential, over every operation the baseline lists: all 300
GETs without ``{}`` answered that 401, as did the two with no ``security`` (`service-registry` once
its `Content-Type` passed, see below), and all 226 with it reached the operation (a 200, a 404 for
the key or id that names nothing, a 400, a 403, a 410, or a 401 of the operation's own); all 437
DELETEs, PUTs and POSTs without it answered the 401 when they carried a body of a type the operation
takes, and none of the 232 with it did.

A refused operation's entry says what it takes, which it checks first, for any caller the gateway
lets through: ``consumes`` lists the media types, concrete ones before a wildcard, the order Jira
names them in the 415's `Accept` (`multipart/form-data, */*` for `settings/columns`, whose document
lists `*/*` first); ``body`` is ``"optional"`` where a request with no body is not checked. The
types are the documents' ``requestBody``, but where the same sweep measured otherwise, the names
below say so. How the check reads a `Content-Type` is
``backlot.errors.atlassian.refuse_a_media_type``'s.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backlot.fidelity.comparisons import COMPARISONS, baseline_path  # noqa: E402
from backlot.fidelity.errors import FidelityError  # noqa: E402
from backlot.fidelity.fetch import fetch_json  # noqa: E402

OUT = REPO / "backlot" / "data" / "jira_unserved.json"
METHODS = ("get", "put", "post", "delete", "patch", "head", "options")


def _both(*paths: str) -> set[str]:
    """Each `METHOD /rest/api/{}/…` once under `/rest/api/2` and once under `/rest/api/3`."""
    return {p.replace("{v}", v) for p in paths for v in ("2", "3")}


# Documented with a JSON body, and answered the 401 with none and with one typed `application/xml`
# or `foo`, which does not parse: no check at all.
UNCHECKED = _both(
    "DELETE /rest/api/{v}/config/fieldschemes/fields",
    "DELETE /rest/api/{v}/field/association",
    "POST /rest/api/{v}/jql/function/computation/search",
    "POST /rest/api/{v}/plans/plan",
    "POST /rest/api/{v}/plans/plan/{}/duplicate",
    "POST /rest/api/{v}/plans/plan/{}/team/atlassian",
    "POST /rest/api/{v}/plans/plan/{}/team/planonly",
    "POST /rest/api/{v}/priorityscheme/mappings",
    "PUT /rest/api/{v}/user/properties/{}",
) | {
    "PUT /rest/atlassian-connect/1/addons/{}/properties/{}",
    "PUT /rest/forge/1/app/properties/{}",
}
# Answered the 401 with no body and a 415 for a `text/plain` one.
BODY_OPTIONAL = _both(
    "POST /rest/api/{v}/uiModifications",
    "PUT /rest/api/{v}/uiModifications/{}",
    "PUT /rest/api/{v}/issues/archive/export",
)
# Documented with no body, and answered a 415 naming `'null'` with no Content-Type and the 401 with
# `application/json`.
UNDOCUMENTED = {
    op: ["application/json"]
    for op in _both("DELETE /rest/api/{v}/project-template/remove-template")
    | {"GET /rest/atlassian-connect/1/service-registry"}
}


def _entry(key: str, content: list[str] | None) -> dict:
    types = UNDOCUMENTED.get(key, content)
    if not types or key in UNCHECKED:
        return {}
    entry: dict = {"consumes": sorted(types, key=lambda t: "*" in t)}
    if key in BODY_OPTIONAL:
        entry["body"] = "optional"
    return entry


def build() -> dict:
    security: dict[str, list | None] = {}
    content: dict[str, list[str] | None] = {}
    urls = []
    for spec in COMPARISONS["jira"].specs:
        urls.append(spec.spec_url)
        document = fetch_json(spec.spec_url)
        default = document.get("security")
        for path, item in document["paths"].items():
            for method, operation in item.items():
                if method in METHODS:
                    key = f"{method.upper()} {re.sub(r'{[^}]+}', '{}', path)}"
                    security[key] = operation.get("security", default)
                    body = operation.get("requestBody")
                    content[key] = None if body is None else list(body.get("content", {}))
    baseline = json.loads(baseline_path("jira").read_text())["acknowledged"]
    unserved = sorted(r["path"] for r in baseline if r["kind"] == "missing_operation")
    refused, run = {}, []
    for key in unserved:
        requirements = security.get(key)
        if requirements is None or {} not in requirements:
            refused[key] = _entry(key, content.get(key))
        else:
            run.append(key)
    named = UNCHECKED | BODY_OPTIONAL | set(UNDOCUMENTED)
    if named - set(refused):
        raise SystemExit(f"measured by name but no longer refused: {sorted(named - set(refused))}")
    return {
        "about": (
            "What real answers before each Jira operation no route serves runs. Written by "
            "scripts/gen_jira_unserved.py, which says how it was measured; do not edit by hand."
        ),
        "documents": urls,
        "refused": refused,
        "run": run,
    }


def render(content: dict) -> str:
    return json.dumps(content, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is stale")
    args = parser.parse_args()
    try:
        text = render(build())
    except FidelityError as e:
        # 2, as `backlot diff` exits for a document it could not ask for: not a finding
        print(e, file=sys.stderr)
        return 2
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT.relative_to(REPO)} is stale; run scripts/gen_jira_unserved.py")
            return 1
        return 0
    OUT.write_text(text)
    table = json.loads(text)
    print(
        f"wrote {OUT.relative_to(REPO)}: {len(table['refused'])} refused, {len(table['run'])} run"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
