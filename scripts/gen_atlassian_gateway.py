#!/usr/bin/env python3
"""Regenerate backlot/fidelity/jira_gateway.json from Jira's own documents.

    python scripts/gen_atlassian_gateway.py            # rewrite the file
    python scripts/gen_atlassian_gateway.py --check    # exit 1 if the file is stale

The file lists the Jira operations the gateway refuses before Jira runs them when the caller has
no credential it resolves: `401 text/html` `Client must be authenticated to access this resource.`
Which operations those are is read off each operation's `security` in the documents
``backlot.fidelity.comparisons.COMPARISONS["jira"]`` names: an operation whose requirements do not
include the empty one, ``{}``, is refused; one that does is run, and what it answers then is the
operation's own. Measured on Jira Cloud on 2026-09-30, anonymously, over every GET
the Jira baseline lists as ``missing_operation``: all 300 without ``{}`` answered that 401, and all
226 with it reached the operation (a 200, a 404 for the key or id that names nothing, a 400, a 403,
a 410, or a 401 of the operation's own); of eighteen DELETEs, PUTs and POSTs, eight of the nine
without ``{}`` answered that 401 and the ninth, `PUT /rest/api/3/plans/plan/{}` with a JSON body,
a 415 from the operation, and the nine with it reached the operation.

An operation the documents give no ``security`` at all is listed too, as the one of those measured
with no credential, `GET /rest/atlassian-connect/1/app/module/dynamic`, answered the 401;
`GET /rest/atlassian-connect/1/service-registry`, which answered a 415 from the operation, is left
out by name.

Only the operations the baseline acknowledges as ``missing_operation`` are written, because those
are the ones no route here serves, which is where ``backlot.routers.atlassian.unmatched_path`` needs
the answer. Regenerate after the baseline gains or loses one.
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
from backlot.fidelity.fetch import fetch_json  # noqa: E402

OUT = REPO / "backlot" / "fidelity" / "jira_gateway.json"
METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
# measured reaching the operation with no credential although the documents give it no `security`
REACHED_WITHOUT_SECURITY = {"GET /rest/atlassian-connect/1/service-registry"}


def build() -> dict:
    security: dict[str, list | None] = {}
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
    baseline = json.loads(baseline_path("jira").read_text())["acknowledged"]
    unserved = sorted(r["path"] for r in baseline if r["kind"] == "missing_operation")
    refused = []
    for key in unserved:
        requirements = security.get(key)
        if requirements is None:
            if key not in REACHED_WITHOUT_SECURITY:
                refused.append(key)
        elif {} not in requirements:
            refused.append(key)
    return {
        "about": (
            "The Jira operations no route serves that the gateway refuses with 401 for a caller "
            "with no credential it resolves. Written by scripts/gen_atlassian_gateway.py, which "
            "says how it was measured; do not edit by hand."
        ),
        "documents": urls,
        "operations": refused,
    }


def render(content: dict) -> str:
    return json.dumps(content, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is stale")
    args = parser.parse_args()
    text = render(build())
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT.relative_to(REPO)} is stale; run scripts/gen_atlassian_gateway.py")
            return 1
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(REPO)}: {len(json.loads(text)['operations'])} operations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
