"""`scripts/pr_gate.py`'s rules, without the network.

The titles are taken, or edited, from ones pull requests here were opened with.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _gate():
    """The script as a module, loaded the way `_gen_docs` in `tests/test_skills.py` is."""
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
        ("Fixes: #9", [9]),
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
        (
            "s3 ListObjectsV2 honours fetch-owner",
            FILLED,
            {"s3"},
            ["Open the title with the closed issue's prefix, `s3: `."],
        ),
        # A pull request that closes no fidelity issue has no prefix to open with.
        ("Fix loop-doorbell workflow to use the gh token correctly", FILLED, set(), []),
        # Closing two issues from two sources, either prefix opens the title.
        ("slack: limit=0 serves the default page", FILLED, {"gmail", "slack"}, []),
        # Some pull request bodies here end their lines in CRLF.
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


# Two pull requests that close one issue, the second opened two hours after the first.
FIRST = ("2026-10-05T17:29:30Z", 492, [479])
SECOND = ("2026-10-05T19:33:30Z", 497, [479])


@pytest.mark.parametrize(
    ("number", "open_prs", "held"),
    [
        (492, [FIRST, SECOND], {}),
        (497, [FIRST, SECOND], {479: 492}),
        # Listed newest first, the oldest still holds.
        (497, [SECOND, FIRST], {479: 492}),
        # Opened in the same second, the lower number holds.
        (434, [(FOUR_IN_A_MINUTE[0][0], 434, [9]), (FOUR_IN_A_MINUTE[0][0], 433, [9])], {9: 433}),
        (500, [FIRST, ("2026-10-06T00:00:00Z", 500, [480, 479])], {479: 492}),
        (
            500,
            [
                FIRST,
                ("2026-10-05T18:00:00Z", 498, [480]),
                ("2026-10-06T00:00:00Z", 500, [479, 480]),
            ],
            {479: 492, 480: 498},
        ),
        (500, [FIRST, ("2026-10-06T00:00:00Z", 500, [])], {}),
    ],
)
def test_held_elsewhere(number, open_prs, held):
    assert gate.held_elsewhere(number, open_prs) == held


def _pr(created_at: str, number: int, issues: list[int], login: str) -> dict:
    """A pull request as the API and the event list it, closing ``issues``."""
    return {
        "number": number,
        "created_at": created_at,
        "user": {"login": login},
        "state": "open",
        "author_association": "CONTRIBUTOR",
        "title": "hubspot: NEQ, NOT_IN and NOT_CONTAINS_TOKEN include a record without the property",
        "body": FILLED.replace("Closes #412.", " ".join(f"Closes #{i}." for i in issues)),
    }


@pytest.mark.parametrize(
    ("number", "action", "sender", "closed"),
    [
        (492, "opened", "first-author", False),
        (497, "opened", "second-author", True),
        (497, "reopened", "second-author", True),
        # A maintainer's reopening is left alone.
        (497, "reopened", "a-maintainer", False),
    ],
)
def test_a_pull_request_for_a_held_issue_is_closed(
    monkeypatch, capsys, number, action, sender, closed
):
    """A dry run, with the open pull requests and the API's answers served from here."""
    listed = [_pr(*FIRST, "first-author"), _pr(*SECOND, "second-author")]
    monkeypatch.setattr(gate, "_pages", lambda path: listed if "/pulls?" in path else [])
    monkeypatch.setattr(
        gate,
        "_api",
        lambda method, path, payload=None: (
            {"total_count": 0}
            if path.startswith("/search/")
            else {"title": "hubspot: NEQ …", "labels": [{"name": "fidelity"}]}
        ),
    )
    pr = next(p for p in listed if p["number"] == number)
    gate.run("brekkylab/backlot", action, pr, sender, write=False)
    out = capsys.readouterr().out
    assert (f"PATCH /repos/brekkylab/backlot/pulls/{number}" in out) is closed
    assert ("#492, opened before it, already closes #479," in out) is closed
