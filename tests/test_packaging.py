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

import ast
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


# The same drift class, one layer up: an OPTIONAL-dependency gate. `pytest.importorskip` makes a
# test self-skip when its module is absent, so a gate whose distribution no extra carries never
# fails anywhere — a contributor who installed every extra still skips it, believing they ran it,
# and CI's `-rs` reports it without failing (kept that way on purpose; these tests read sources,
# not runs). CONTRIBUTING maps each extra to what sits out without it, and that table is prose:
# nothing held it to `pyproject.toml` or to what the tests actually gate on. The two tests below do.

# importorskip module -> the pyproject dependency that provides it, for the names PEP 503
# normalization cannot reach. Everything else maps by convention (`llama_index.readers.slack` <->
# `llama-index-readers-slack`); a new gate that lands here with neither breaks the test, which is
# the point — the map is then updated in the same diff that adds the gate.
_IMPORT_TO_DIST_EXCEPTIONS = {
    "googleapiclient": "google-api-python-client",
    "github": "PyGithub",
    "atlassian": "atlassian-python-api",
    "botocore": "boto3",  # carried transitively: boto3 pins it
    "hubspot": "hubspot-api-client",
    "mirage.core.github._client": "mirage-ai",
    "mirage.core.google._client": "mirage-ai",
}

# Gates whose distribution deliberately has no extra — CONTRIBUTING's "which no extra carries"
# row. A module leaves this set by an extra gaining its distribution, not by editing the set.
_DOCUMENTED_ABSENT = {"llama_index.readers.hubspot": "llama-index-readers-hubspot"}


def _norm(dist: str) -> str:
    return re.sub(r"[-_.]+", "-", dist).lower()


def pyproject_optional_dependencies() -> dict[str, list[str]]:
    """`[project.optional-dependencies]` verbatim — the requirement strings, not the parsed names,
    which is what the aggregate has to be read from."""
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["optional-dependencies"]


def _extras() -> dict[str, set[str]]:
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        pyproject = tomllib.load(f)
    deps = pyproject["project"]["optional-dependencies"]
    return {
        extra: {_norm(re.match(r"[A-Za-z0-9_.-]+", d).group()) for d in reqs}
        for extra, reqs in deps.items()
    }


def _loop_values(tree: ast.AST, call: ast.Call, target: str) -> list[str] | None:
    """The strings ``target`` takes, if ``call`` sits inside a ``for`` that binds it to a literal
    sequence of them. A tuple and a list are the same loop, so both are read.

    Scoped to the enclosing loop rather than to the file: the answer depends on WHERE the call is,
    and a name is free to mean something else in the next loop down.
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.For) and isinstance(node.target, ast.Name)):
            continue
        if node.target.id != target or not isinstance(node.iter, ast.Tuple | ast.List):
            continue
        if not all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.iter.elts
        ):
            continue
        if any(child is call for child in ast.walk(node)):
            return [e.value for e in node.iter.elts]
    return None


def _importorskip_modules() -> dict[str, list[str]]:
    """Every module the suite gates on, module -> the test files that gate on it.

    Read with ``ast`` rather than a regex because one gate is spelled through a loop variable
    (``for _mod in (...): pytest.importorskip(_mod)`` in ``tests/test_sdk.py``), which a string
    scan for literals silently drops — and a collector that misses gates proves nothing.

    The loop variable is resolved INSIDE the loop that binds it, not from a file-wide map: one
    map per file is last-write-wins, so a second ``for`` over the same name would drop the first
    loop's gates, and a second ``for`` that is ordinary iteration would contribute names that gate
    nothing. Both leave the collector reporting confidently on the wrong set.
    """
    found: dict[str, list[str]] = {}
    for path in sorted((REPO_ROOT / "tests").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name != "importorskip":
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                mods = [arg.value]
            elif isinstance(arg, ast.Name) and (bound := _loop_values(tree, node, arg.id)):
                mods = bound
            else:  # pragma: no cover — a shape this collector cannot read must fail, not skip
                raise AssertionError(
                    f"unreadable importorskip argument in {path.name}: {ast.dump(arg)}"
                )
            for m in mods:
                found.setdefault(m, []).append(path.name)
    return found


def test_the_all_extra_aggregates_every_other_one():
    """`.[all]` is what CI installs, so it is what decides whether a gated test runs at all.

    It is one of three places that enumerate the extras — `pyproject.toml` defines them,
    CONTRIBUTING's table names them, this aggregate installs them — and the one that silently
    costs coverage: an extra added to the other two but left out here installs nowhere, so every
    test behind it skips forever and `-rs` reports that without failing. The table and the
    definitions are held to each other by the test below; this holds the copy CI acts on.

    An aggregate rather than a flag because pip has no `--all-extras` (measured on pip 26.2.1),
    and a self-reference rather than a duplicated list because pip resolves `backlot[...]` to the
    directory being installed: the resolution report names a `file://` url for it and marks it
    direct, so the release on PyPI is never consulted.
    """
    extras = _extras()
    assert "all" in extras, "the aggregate CI installs is gone; ci.yml still names `.[all]`"
    (aggregate,) = pyproject_optional_dependencies()["all"]
    named = set(
        re.search(r"backlot\[([\w,\s-]+)\]", aggregate).group(1).replace(" ", "").split(",")
    )
    assert named == set(extras) - {"all"}, (
        f"`all` installs {sorted(named)} but the extras are {sorted(set(extras) - {'all'})} — "
        "an extra missing here is one CI never installs"
    )


def test_the_contributing_extras_are_the_pyproject_extras():
    """CONTRIBUTING's gate table is prose over `pyproject.toml`, and prose drifts: an extra renamed
    or added in pyproject leaves the table telling contributors to install something that does not
    exist, or not naming something they need. Both directions are asserted, plus that every test
    file the table points at is still there."""
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text()
    # `[\w,\s-]`, not `[\w,]`: an extra may be hyphenated and a list may be spaced (`.[dev, mcp]`),
    # and a pattern that cannot match those reports a correctly documented extra as missing.
    named = {
        e.strip()
        for group in re.findall(r"\.\[([\w,\s-]+)\]", text)
        for e in group.split(",")
        if e.strip()
    }
    extras = set(_extras())
    assert named == extras, (
        f"CONTRIBUTING names extras pyproject does not define: {sorted(named - extras)}; "
        f"pyproject defines extras CONTRIBUTING never names: {sorted(extras - named)}"
    )
    missing = [
        f for f in set(re.findall(r"`(tests/[\w./]+\.py)`", text)) if not (REPO_ROOT / f).is_file()
    ]
    assert missing == [], f"CONTRIBUTING points at test files that are gone: {missing}"


def test_every_optional_import_gate_is_reachable_from_an_extra():
    """A gate nobody can install is a test nobody runs. Every ``importorskip``'d module must map to
    a distribution some extra (or the core dependencies) carries, or sit in the one documented
    exception — so the next optional test arrives either with its extra or with its absence written
    down, instead of skipping forever on machines that installed everything."""
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        pyproject = tomllib.load(f)
    provided = {
        _norm(re.match(r"[A-Za-z0-9_.-]+", d).group()) for d in pyproject["project"]["dependencies"]
    }
    for dists in _extras().values():
        provided |= dists

    gates = _importorskip_modules()
    # Sanity check on the collector itself: the loop-variable spelling in test_sdk.py must be seen,
    # or this test walks a subset and proves nothing.
    assert "slack_sdk" in gates and "test_sdk.py" in gates["slack_sdk"]

    unreachable = {}
    for module, files in gates.items():
        if module in _DOCUMENTED_ABSENT:
            continue
        dist = _norm(_IMPORT_TO_DIST_EXCEPTIONS.get(module, module))
        if dist not in provided:
            unreachable[module] = files
    assert unreachable == {}, (
        "these importorskip gates name modules no extra's distribution provides, so the tests "
        "behind them skip even on a full install — add the extra, or document the absence: "
        f"{unreachable}"
    )

    # ...and the documented absence stays true: the day an extra carries the distribution, the
    # exception row here and in CONTRIBUTING comes out rather than shadowing a gate that now runs.
    for module, dist in _DOCUMENTED_ABSENT.items():
        assert module in gates, f"nothing gates on {module} any more — retire its absence row"
        assert _norm(dist) not in provided, (
            f"{dist} is now carried by an extra; {module} is no longer a documented absence"
        )
