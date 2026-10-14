"""`scripts/invariance.py`'s comparisons, over recordings written by hand."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _invariance():
    """`scripts/invariance.py` as a module, since `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("invariance", REPO / "scripts" / "invariance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


invariance = _invariance()


def _run(root: Path, *, body: dict, reset: str, outcome="passed", lines=(1, 2)) -> Path:
    for part, rows in {
        "traffic": [
            {
                "test": "t.py::a",
                "i": 0,
                "request": {"method": "GET", "url": "/x", "headers": [], "body": {"text": ""}},
                "status": 200,
                "headers": [["x-ratelimit-reset", reset]],
                "body": {"text": json.dumps(body)},
            }
        ],
        "outcomes": [{"test": "t.py::a", "when": "call", "outcome": outcome}],
    }.items():
        (root / part).mkdir(parents=True)
        (root / part / "main.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    files = {"backlot/x.py": {"executed_lines": list(lines), "executed_branches": [[1, 2]]}}
    (root / "coverage.json").write_text(json.dumps({"files": files}))
    return root


@pytest.mark.parametrize(
    ("head", "mode", "found"),
    [
        pytest.param({"body": {"a": 1, "at": 9}, "reset": "8"}, "code", None, id="calibrated"),
        pytest.param({"body": {"a": 2, "at": 7}, "reset": "7"}, "code", "$.a", id="body-leaf"),
        pytest.param(
            {"body": {"a": 1, "at": 7}, "reset": "7", "outcome": "failed"},
            "code",
            "passed on the base, failed",
            id="outcome",
        ),
        pytest.param(
            {"body": {}, "reset": "", "lines": (1,)},
            "tests",
            "lines no longer executed: [2]",
            id="lost-line",
        ),
    ],
)
def test_compare_flags_a_difference_and_only_one_two_base_runs_agree_on(
    tmp_path, capsys, head, mode, found
):
    """`at` and the reset header differ between the two base runs, so they are noise; `a` does
    not, so a head that changes it has changed behaviour."""
    base = _run(tmp_path / "base", body={"a": 1, "at": 7}, reset="7")
    base2 = _run(tmp_path / "base2", body={"a": 1, "at": 8}, reset="8")
    code = invariance._compare(base, _run(tmp_path / "head", **head), mode, base2, limit=5)
    out = capsys.readouterr().out
    assert code == (found is not None)
    assert (found or "invariant") in out


def test_prose_tree_ignores_comments_and_docstrings_only():
    before = 'def f():\n    """Old."""\n    return 1  # one\n'
    assert invariance._code_tree(before) == invariance._code_tree(
        'def f():\n    """New, longer."""\n    # a comment\n    return 1\n'
    )
    assert invariance._code_tree(before) != invariance._code_tree("def f():\n    return 2\n")
