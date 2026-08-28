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


def test_the_sdist_prunes_the_readme_illustrations():
    """The mirror image of the test above: a file that ships and should not.

    ``setuptools-scm`` puts every git-tracked file in the sdist, so ``assets/`` rode along — the
    demo GIF and two architecture figures, 1.4 MB of a 2.3 MB sdist, that GitHub renders and
    nothing in the package reads. Asserted on the ``MANIFEST.in`` directive rather than on a built
    artifact, because nothing here runs a real build (see the module docstring).
    """
    directives = [
        line.split()
        for line in (REPO_ROOT / "MANIFEST.in").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert ["prune", "assets"] in directives, (
        "MANIFEST.in no longer prunes assets/, so the sdist ships the README illustrations again: "
        f"{directives}"
    )
    # Sanity check on the test itself: a prune of a directory that no longer exists proves nothing.
    assert (REPO_ROOT / "assets").is_dir(), "assets/ is gone — drop the prune and this test with it"


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

    HTML attributes are checked too. Markdown is not the only way in — ``<picture>`` and ``<img>``
    both survive the renderer, and a relative ``src``/``srcset`` in one is invisible to the
    ``](target)`` pattern above while breaking on PyPI just the same.
    """
    text = (REPO_ROOT / "README.pypi.md").read_text()
    targets = re.findall(r"\]\(([^)\s]+)\)", text)
    targets += [
        candidate.strip().split()[0]
        for attribute in re.findall(r"(?:src|srcset|href)=\"([^\"]+)\"", text)
        for candidate in attribute.split(",")
        if candidate.strip()
    ]
    relative = [t for t in targets if not t.startswith(("http://", "https://", "#"))]
    assert not relative, f"README.pypi.md links break on PyPI: {relative}"
