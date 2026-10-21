"""`scripts/invariance.py`'s comparisons, over recordings written by hand, and its mutations."""

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


def _run(
    root: Path,
    *,
    body: dict,
    reset: str,
    outcome="passed",
    lines=(1, 2),
    branches=((1, 2),),
    test="t.py::a",
    teardown=None,
) -> Path:
    for part, rows in {
        "traffic": [
            {
                "test": test,
                "i": 0,
                "request": {"method": "GET", "url": "/x", "headers": [], "body": {"text": ""}},
                "status": 200,
                "headers": [["x-ratelimit-reset", reset]],
                "body": {"text": json.dumps(body)},
            }
        ],
        "outcomes": [{"test": test, "when": "call", "outcome": outcome}]
        + ([{"test": test, "when": "teardown", "outcome": teardown}] if teardown else []),
    }.items():
        (root / part).mkdir(parents=True)
        (root / part / "main.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
    files = {"backlot/x.py": {"executed_lines": list(lines), "executed_branches": branches}}
    (root / "coverage.json").write_text(json.dumps({"files": files}), encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("head", "mode", "found"),
    [
        pytest.param({"body": {"a": 1, "at": 9}, "reset": "8"}, "code", None, id="calibrated"),
        pytest.param({"body": {"a": 2, "at": 7}, "reset": "7"}, "code", "$.a", id="body-leaf"),
        pytest.param(
            {"body": {"a": 1, "at": 7}, "reset": "7s"}, "code", "x-ratelimit-reset", id="header"
        ),
        pytest.param({"body": {"at": 7, "a": 1}, "reset": "7"}, "code", "$ keys", id="key-order"),
        pytest.param(
            {"body": {"a": 1, "at": 7, "e": []}, "reset": "7"}, "code", "$.e", id="empty-list"
        ),
        pytest.param(
            {"body": {"a": 1, "at": 7}, "reset": "7", "outcome": "failed"},
            "code",
            "passed on the base, failed",
            id="outcome",
        ),
        pytest.param(
            {"body": {"a": 1, "at": 7}, "reset": "7", "teardown": "failed"},
            "code",
            "passed on the base, teardown failed on the head",
            id="teardown",
        ),
        pytest.param(
            {"body": {"a": 1, "at": 7}, "reset": "7", "test": "t.py::b"},
            "code",
            "test t.py::a ran on the base and not on the head",
            id="code-test-gone",
        ),
        pytest.param(
            {"body": {"a": 1, "at": 7}, "reset": "7", "test": "t.py::b"},
            "code",
            "test t.py::b ran on the head and not on the base",
            id="code-test-new",
        ),
        pytest.param(
            {"body": {}, "reset": "", "lines": (1,)},
            "tests",
            "lines no longer executed: [2]",
            id="lost-line",
        ),
        pytest.param(
            {"body": {}, "reset": "", "branches": ()},
            "tests",
            "branches no longer taken: [(1, 2)]",
            id="lost-branch",
        ),
        pytest.param({"body": {}, "reset": "", "test": "t.py::b"}, "tests", None, id="renamed"),
        pytest.param(
            {"body": {}, "reset": "", "test": "t.py::b", "outcome": "failed"},
            "tests",
            "test t.py::b is new and failed",
            id="new-failing",
        ),
    ],
)
def test_compare_flags_a_difference_and_only_one_two_base_runs_agree_on(
    tmp_path, capsys, head, mode, found
):
    """`at` and the reset header's digits differ between the two base runs, so they are noise; `a`
    does not, so a head that changes it has changed behaviour."""
    base = _run(tmp_path / "base", body={"a": 1, "at": 7}, reset="7")
    base2 = _run(tmp_path / "base2", body={"a": 1, "at": 8}, reset="8")
    code = invariance._compare(base, _run(tmp_path / "head", **head), mode, base2, limit=5)
    out = capsys.readouterr().out
    assert code == (found is not None)
    assert (found or "invariant") in out


@pytest.mark.parametrize(
    "written", ["backlot/x.py", "backlot\\x.py"], ids=["written-slash", "written-backslash"]
)
def test_coverage_names_a_recordings_files_with_slashes(tmp_path, written):
    files = {written: {"executed_lines": [1]}}
    (tmp_path / "coverage.json").write_text(json.dumps({"files": files}), encoding="utf-8")
    assert invariance._coverage(tmp_path) == {"backlot/x.py": ({1}, set())}


@pytest.mark.parametrize(
    "written", ["backlot/x.py", "backlot\\x.py"], ids=["written-slash", "written-backslash"]
)
@pytest.mark.parametrize(
    ("path", "listed"),
    [
        pytest.param("backlot/x.py", True, id="file"),
        pytest.param("backlot\\x.py", True, id="file-backslash"),
        pytest.param("backlot/", True, id="directory"),
        pytest.param("backlot", True, id="directory-bare"),
        pytest.param("backlot\\", True, id="directory-backslash"),
        pytest.param("backlot/y.py", False, id="another-file"),
        pytest.param("backl", False, id="a-prefix-of-the-directory"),
    ],
)
def test_a_recordings_files_are_named_with_slashes_and_found_by_either_separator(
    tmp_path, capsys, written, path, listed
):
    files = {written: {"executed_lines": [1], "missing_lines": [2]}}
    (tmp_path / "coverage.json").write_text(json.dumps({"files": files}), encoding="utf-8")
    assert invariance._uncovered(tmp_path, [path]) == 0
    listing = "backlot/x.py: 1 lines no test executes: [2]\n"
    assert capsys.readouterr().out == (listing if listed else "")


def test_prose_tree_ignores_comments_and_unserved_docstrings_only():
    before = 'def f():\n    """Old."""\n    return 1  # one\n'
    assert invariance._code_tree(before) == invariance._code_tree(
        'def f():\n    """New, longer."""\n    # a comment\n    return 1\n'
    )
    assert invariance._code_tree(before) != invariance._code_tree("def f():\n    return 2\n")
    route = '@router.get("/x")\ndef f():\n    """{}"""\n'
    assert invariance._code_tree(route.format("Old.")) != invariance._code_tree(
        route.format("New.")
    )


@pytest.mark.parametrize(
    ("new", "select", "verdict", "code"),
    [
        pytest.param("return 2", "test_f", "caught", 0, id="caught"),
        pytest.param("return 1  # same", "test_f", "missed", 1, id="missed"),
        pytest.param("return 2", "test_nothing", "no verdict", 2, id="none-selected"),
        pytest.param("return (", "test_f", "no verdict", 2, id="import-broken"),
    ],
)
def test_mutate_answers_only_from_tests_that_ran(tmp_path, capsys, new, select, verdict, code):
    source = tmp_path / "mod.py"
    source.write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "test_mod.py").write_text(
        "from mod import f\n\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8"
    )
    args = [
        str(tmp_path),
        "-k",
        select,
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
        "--rootdir",
        str(tmp_path),
    ]
    assert invariance._mutate(source, "return 1", new, args) == code
    assert verdict in capsys.readouterr().out
    assert source.read_text(encoding="utf-8") == "def f():\n    return 1\n"


def test_record_keeps_a_tests_exchanges_outcome_and_coverage(tmp_path, monkeypatch):
    """Through the real plugin, on one test of this suite: its plugin refuses any project that does
    not import `backlot` from its own root, so a fixture project could not stand in."""
    monkeypatch.chdir(REPO)
    test = "tests/test_cross_vendor.py::test_meta_users_directory"
    out = tmp_path / "rec"
    assert invariance.main(["record", str(out), test, "-o", "addopts=", "-p", "no:warnings"]) == 0
    traffic = invariance._traffic(out)
    # the `org` fixture's directory read, the test's own, then the two repo listings it compares
    assert [
        (t, i, r["request"]["url"].split("?")[0], r["status"]) for (t, i), r in traffic.items()
    ] == [
        (test, 0, "http://testserver/_meta/users", 200),
        (test, 1, "http://testserver/_meta/users", 200),
        (test, 2, "http://testserver/github/orgs/acme/repos", 200),
        (test, 3, "http://testserver/github/orgs/acme/repos", 200),
    ]
    assert invariance._outcomes(out) == {test: "passed"}
    assert json.loads((out / "meta.json").read_text(encoding="utf-8"))["passed"] == 1
    assert any(f.startswith("backlot/") for f in invariance._coverage(out))
