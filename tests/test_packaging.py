"""Guard one shape of the packaging-data class of bug: a file inside the package with no matching
package-data glob.

``backlot/validation.py``'s ``SCHEMA_DIR`` and ``backlot/graphql/{linear,fireflies}.graphql`` both
resolved to a location that a wheel never actually contained — each one shipped fine from a
checkout and broke only once installed, because nothing here runs a real build. This test
enumerates every non-``.py`` file ``git`` tracks under ``backlot/`` and asserts ``pyproject.toml``'s
``[tool.setuptools.package-data]`` covers it, so the next asset added under ``backlot/`` without a
matching glob fails in the normal suite instead of waiting for someone to install a wheel outside
this repo. It deliberately reads tracked files, not a directory walk: ``backlot/.DS_Store`` (editor
cruft, untracked, ``.gitignore``d) must not fail this on a colleague's machine.

A third instance from the same bug hunt, ``backlot/config.py``'s ``data_dir`` default, is a
different shape — a path resolving *outside* the package entirely — which enumerating files
*inside* the package cannot catch. ``tests/test_config.py`` guards that one.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from fnmatch import fnmatch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _tracked_non_py_files_under_backlot() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "backlot"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        line[len("backlot/") :] for line in out.splitlines() if line and not line.endswith(".py")
    ]


def test_every_shipped_non_py_file_is_covered_by_package_data():
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        pyproject = tomllib.load(f)
    patterns = pyproject["tool"]["setuptools"]["package-data"]["backlot"]

    files = _tracked_non_py_files_under_backlot()
    # Sanity check on the test itself: if this ever comes back empty, the glob below matches
    # trivially and the assertion proves nothing.
    assert files, "expected at least one non-.py file under backlot/ (schemas/, graphql/, ...)"

    uncovered = [f for f in files if not any(fnmatch(f, pat) for pat in patterns)]
    assert uncovered == [], (
        "these files live under backlot/ in the checkout but no glob in "
        "[tool.setuptools.package-data] covers them, so a wheel build silently omits them: "
        f"{uncovered}"
    )


# README.md carries relative image and link paths that only resolve on GitHub. PyPI passes them
# through unchanged (measured 2026-08-14 against readme_renderer[md]: `<picture>` and `<details>`
# survive, relative targets are left alone and therefore 404 under pypi.org), so the long
# description points at a short absolute-URL-only page instead. The two tests below keep that
# pair from drifting.


def test_long_description_is_the_pypi_readme():
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        pyproject = tomllib.load(f)
    assert pyproject["project"]["readme"] == "README.pypi.md"


def test_pypi_readme_has_no_relative_links():
    """A relative target resolves against pypi.org, where it is a 404.

    Every ``](target)`` is checked, not just the ones a leading ``[`` precedes: a badge is nested
    (``[![alt](image)](href)``) and the pattern that matches its inner image cannot also reach the
    outer href, so a linked badge is exactly where a relative target hides.
    """
    text = (REPO_ROOT / "README.pypi.md").read_text()
    relative = [
        target
        for target in re.findall(r"\]\(([^)\s]+)\)", text)
        if not target.startswith(("http://", "https://", "#"))
    ]
    assert not relative, f"README.pypi.md links break on PyPI: {relative}"
