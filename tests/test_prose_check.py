"""`scripts/prose_check.py`'s two checks, and the command CI runs."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

REPO = Path(__file__).resolve().parent.parent


def _check():
    """`scripts/prose_check.py` as a module, since `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location(
        "prose_check", REPO / "scripts" / "prose_check.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _check()


def _message(number: str) -> str:
    return (
        f"{number} names an issue or pull request: say what was measured and when, and put "
        f"{number} in the pull request's description"
    )


# (source, added lines or None for every line, names at the repository's root, lines reported)
@pytest.mark.parametrize(
    ("src", "added", "local", "lines"),
    [
        pytest.param("x = 1\n# Measured on 2026-10-05 (#90473).\n", None, (), [2], id="comment"),
        pytest.param("x = 1  # see #90074\n", None, (), [1], id="comment-after-code"),
        pytest.param('def f():\n    """#90188: both are 200."""\n', None, (), [2], id="docstring"),
        pytest.param(
            '''\
            def f():
                """Real drops the merge ref when the pull closes: on psf/requests, #97616 and
                #97589 answer 404."""
            ''',
            None,
            (),
            [],
            id="another-repository-names-it",
        ),
        pytest.param(
            "# Read and echoed (#90178). boto3 sends it, as psf/requests does.\n",
            None,
            (),
            [1],
            id="repository-in-the-next-sentence",
        ),
        pytest.param(
            '''\
            def f():
                """#90178: read and echoed

                the way psf/requests sends it."""
            ''',
            None,
            (),
            [2],
            id="repository-in-the-next-paragraph",
        ),
        pytest.param(
            "# Pinned in tests/test_google.py::MEASURED (#90174).\n",
            None,
            (),
            [1],
            id="file-path",
        ),
        pytest.param(
            "# backlot/routers reads it (#90174).\n",
            None,
            ("backlot",),
            [1],
            id="path-in-this-repo",
        ),
        pytest.param(
            "# backlot/routers reads it (#90174).\n", None, (), [], id="path-elsewhere-is-a-repo"
        ),
        pytest.param("# One (#90011).\n# Two (#90012).\n", {2}, (), [2], id="only-added-lines"),
        pytest.param('x = "#90123"\n', None, (), [], id="string-literal"),
        pytest.param(
            "# &#90123; is an entity, #9 a digit and #000000 a colour.\n",
            None,
            (),
            [],
            id="entity-digit-and-colour",
        ),
        pytest.param("# See backlot#90469.\n", None, (), [1], id="repository-prefixed-number"),
        pytest.param(
            "# On psf/requests#7616 it answers 404.\n",
            None,
            (),
            [],
            id="another-repository-prefixed",
        ),
        pytest.param(
            "# Same on brekkylab/backlot (#90469).\n", None, (), [1], id="this-repository"
        ),
        pytest.param(
            "# Answers application/json (#90469).\n", None, (), [1], id="media-type-is-not-a-repo"
        ),
        pytest.param("# GET/POST both 405 (#90469).\n", None, (), [1], id="methods-are-not-a-repo"),
        pytest.param(
            "# On NVIDIA/cutlass#1234 it answers 404.\n", None, (), [], id="upper-case-owner"
        ),
        pytest.param("# An admin and/or scoped token (#90469).\n", None, (), [1], id="and-or"),
        pytest.param("# Measured (#90473).\nx = (\n", None, (), [1], id="does-not-tokenize"),
    ],
)
def test_issue_numbers(src, added, local, lines):
    src = dedent(src)
    added = set(range(1, src.count("\n") + 2)) if added is None else added
    found = check.issue_numbers(src, added, frozenset(local))
    assert [n for n, _ in found] == lines
    assert all(msg == _message(msg.split()[0]) for _, msg in found)


CODE = "backlot/x.py"
TEST = "tests/test_x.py"


# (sources by path, added lines by path, where a path left out adds every line, and the
#  (path, line) pairs reported)
@pytest.mark.parametrize(
    ("sources", "added", "found"),
    [
        pytest.param(
            {CODE: "# Measured 2026-10-05: 400.\n", TEST: 'def t():\n    """2026-10-05: 400."""\n'},
            {},
            [(TEST, 2)],
            id="test-dates-code-measurement",
        ),
        pytest.param(
            {
                CODE: "x = 1\n# Measured 2026-10-05: 400.\n",
                TEST: "assert x  # measured 2026-10-05\n",
            },
            {},
            [(TEST, 1)],
            id="comment-after-code",
        ),
        pytest.param(
            {CODE: "# Measured 2026-10-04.\n", TEST: "# Measured 2026-10-05.\n"},
            {},
            [],
            id="date-only-the-test-has",
        ),
        pytest.param(
            {CODE: "# Measured 2026-10-05.\n", TEST: "# Measured 2026-10-05.\n"},
            {CODE: set()},
            [],
            id="code-prose-not-in-the-change",
        ),
        pytest.param(
            {
                CODE: 'VERSION = "2022-11-28"\n# The 2022-11-28 bodies carry it.\n',
                TEST: "# The 2022-11-28 bodies carry it.\n",
            },
            {},
            [],
            id="api-version-the-code-uses",
        ),
        pytest.param(
            {
                CODE: 'URL = f"/{x}?version=2022-11-28"\n# The 2022-11-28 bodies carry it.\n',
                TEST: "# The 2022-11-28 bodies carry it.\n",
            },
            {},
            [],
            id="api-version-in-an-f-string",
        ),
        pytest.param(
            {
                CODE: 'def v():\n    """2026-10-05 is when."""\n# Measured 2026-10-05.\n',
                TEST: "# Measured 2026-10-05.\n",
            },
            {},
            [(TEST, 1)],
            id="docstring-date-is-not-a-value",
        ),
        pytest.param(
            {CODE: "# Measured 2026-10-05.\n", TEST: 'DAY = "2026-10-05"\n'},
            {},
            [],
            id="date-in-test-code",
        ),
    ],
)
def test_redated_tests(sources, added, found):
    added = {p: added.get(p, set(range(1, s.count("\n") + 2))) for p, s in sources.items()}
    values = check.value_dates({p: s for p, s in sources.items() if not p.startswith("tests/")})
    assert [(p, n) for p, n, _ in check.redated_tests(sources, added, values)] == found


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_the_command_reads_the_change_from_its_merge_base(tmp_path, monkeypatch, capsys):
    """A line from before the merge base with BASE is left alone, also where BASE has since dropped
    it. A line committed after it, an uncommitted line and untracked files are read, with
    `diff.noprefix` and `diff.external` set too, and both checks run on them; the test's date that
    is an API version the code uses is left alone. `--github` writes warnings, escapes the path,
    fills the job summary and exits 0."""
    for name, value in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    a = tmp_path / "a.py"

    def git(*args):
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    git("config", "diff.noprefix", "true")
    git("config", "diff.external", "false")
    a.write_text("# Measured on 2026-10-05 (#90012).\n")
    git("add", "a.py")
    git("commit", "-q", "-m", "fork point")
    git("checkout", "-q", "-b", "upstream")
    a.write_text("")
    git("commit", "-q", "-am", "upstream drops the line")
    git("checkout", "-q", "-")
    a.write_text(a.read_text() + "# Committed (#90015).\n")
    git("commit", "-q", "-am", "change")
    a.write_text(a.read_text() + "# Read first (#90013).\n")
    (tmp_path / "odd,name.py").write_text("x = 1  # see #90014\n")
    (tmp_path / "backlot").mkdir()
    (tmp_path / "backlot" / "x.py").write_text(
        'V = "2022-11-28"\n# Measured on 2026-10-05 under 2022-11-28: 400.\n'
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("# 2026-10-05, under 2022-11-28: 400.\n")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    found = [
        ("a.py", 2, _message("#90015")),
        ("a.py", 3, _message("#90013")),
        ("odd,name.py", 1, _message("#90014")),
        (
            "tests/test_x.py",
            1,
            "2026-10-05 dates a measurement backlot/x.py:2 records: point at it rather than dating "
            "it again",
        ),
    ]

    assert check.main(["upstream"]) == 1
    assert capsys.readouterr().out == "".join(f"{p}:{n}: {m}\n" for p, n, m in found)
    assert check.main(["--github", "upstream"]) == 0
    assert capsys.readouterr().out == "".join(
        f"::warning file={p.replace(',', '%2C')},line={n},title=prose::{m}\n" for p, n, m in found
    )
    assert summary.read_text() == "### Prose checks\n\n" + "".join(
        f"- `{p}:{n}`: {m}\n" for p, n, m in found
    )
