"""`scripts/pr_gate.py`'s two rules, without the network.

The titles are ones pull requests from forks were opened with on 2026-10-03 and 2026-10-04.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _gate():
    """The script, loaded by path because scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("pr_gate", REPO / "scripts" / "pr_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _gate()

TEMPLATE = (REPO / ".github" / "pull_request_template.md").read_text()
FILLED = TEMPLATE + "\nCloses #412.\n"
OTHER_HEADINGS = "## Summary\n\nServes it.\n\n## Changes\n\n- one\n\n## Testing\n\n- pytest\n"

HEADING_PROBLEMS = [
    f"Add the template's `## {h}` heading to the description." for h in gate.HEADINGS
]
CHECKBOX_PROBLEM = "Keep the template's three checkboxes in the description."
NUMBER_PROBLEM = "Move the issue number from the title to the description, as `Closes #N`."
FIX_PROBLEM = "Drop the `fix:`-style prefix from the title."


@pytest.mark.parametrize(
    ("body", "numbers"),
    [
        ("Closes #412", [412]),
        ("closes #412 and fixes #413", [412, 413]),
        ("Resolves: #7, then resolved #7", [7]),
        ("Found while measuring for #387.", []),
        ("Measured in #417 on 2026-10-03", []),
    ],
)
def test_closed_issues(body, numbers):
    assert gate.closed_issues(body) == numbers


@pytest.mark.parametrize(
    ("title", "body", "prefixes", "problems"),
    [
        (
            "notion: validate search query and filter types like the real API",
            FILLED,
            {"notion"},
            [],
        ),
        (
            "jira: serverInfo answers fifteen members (closes #412)",
            FILLED,
            {"jira"},
            [NUMBER_PROBLEM],
        ),
        (
            "GitHub tree recursion follows parameter presence",
            FILLED,
            {"github"},
            ["Open the title with the closed issue's prefix, `github: `."],
        ),
        (
            "fix(gmail): normalize padded Gmail thread ids",
            OTHER_HEADINGS,
            {"gmail"},
            ["Open the title with the closed issue's prefix, `gmail: `.", FIX_PROBLEM]
            + HEADING_PROBLEMS
            + [CHECKBOX_PROBLEM],
        ),
        (
            "fix: match real Sheets gridData format (Closes #441)",
            FILLED,
            {"drive"},
            [
                "Open the title with the closed issue's prefix, `drive: `.",
                NUMBER_PROBLEM,
                FIX_PROBLEM,
            ],
        ),
        # A pull request that closes no fidelity issue has no prefix to open with.
        ("Fix loop-doorbell workflow to use the gh token correctly", FILLED, set(), []),
        # Closing two issues from two sources, either prefix opens the title.
        ("slack: limit=0 serves the default page", FILLED, {"gmail", "slack"}, []),
        # A body saved from the web editor ends its lines in CRLF.
        ("slack: limit=0 serves the default page", FILLED.replace("\n", "\r\n"), {"slack"}, []),
        (
            "slack: limit=0 serves the default page",
            FILLED.replace("- [ ] A new endpoint", "A new endpoint"),
            {"slack"},
            [CHECKBOX_PROBLEM],
        ),
    ],
)
def test_format_problems(title, body, prefixes, problems):
    assert gate.format_problems(title, body, prefixes) == problems


FOUR_IN_A_MINUTE = [
    ("2026-10-04T07:14:27Z", 433),
    ("2026-10-04T07:14:32Z", 434),
    ("2026-10-04T07:14:36Z", 435),
    ("2026-10-04T07:14:41Z", 436),
]


@pytest.mark.parametrize(
    ("number", "open_prs", "merged", "kept"),
    [
        (433, FOUR_IN_A_MINUTE[:1], 0, None),
        (433, FOUR_IN_A_MINUTE, 0, None),
        (434, FOUR_IN_A_MINUTE, 0, [433]),
        (436, FOUR_IN_A_MINUTE, 0, [433]),
        # Listed newest first, the oldest is still the one kept.
        (433, FOUR_IN_A_MINUTE[::-1], 0, None),
        (435, FOUR_IN_A_MINUTE[:3], 1, None),
        (436, FOUR_IN_A_MINUTE, 1, [433, 434, 435]),
        (436, FOUR_IN_A_MINUTE, 5, [433, 434, 435]),
    ],
)
def test_kept_if_over(number, open_prs, merged, kept):
    assert gate.kept_if_over(number, open_prs, merged) == kept
