#!/usr/bin/env python3
"""Check a built wheel's METADATA for things that block a PyPI upload.

Separate from ``wheel_smoke.py`` because it inspects the ARTIFACT rather than an install: it reads
the zip, so it runs with the build interpreter and needs no venv.

    python -m build --wheel
    python tests/e2e/wheel_metadata.py            # newest wheel in ./dist
    python tests/e2e/wheel_metadata.py dist/backlot-0.0.1-py3-none-any.whl

**Direct-URL requirements** (``name @ https://…``, which is what a git dependency becomes) are
rejected by PyPI on upload. ``twine check`` reports PASSED on a wheel carrying one — it validates
metadata well-formedness and README rendering, not this policy — so a release would fail at the last
step with nothing having warned. That gap is the whole reason this file exists.
"""

from __future__ import annotations

import email
import glob
import subprocess
import sys
import zipfile
from pathlib import Path


def read_metadata(wheel: Path) -> email.message.Message:
    with zipfile.ZipFile(wheel) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
        return email.message_from_string(zf.read(name).decode())


def check_no_direct_url_requirements(metadata: email.message.Message) -> None:
    bad = [r for r in metadata.get_all("Requires-Dist") or [] if " @ " in r]
    if bad:
        print("PyPI rejects direct-URL dependencies (twine check does not catch this):")
        for line in bad:
            print(f"  Requires-Dist: {line}")
        raise SystemExit(1)
    print("OK  no direct-URL Requires-Dist entries")


def check_summary_is_present_and_short(metadata: email.message.Message) -> None:
    """``Summary`` is the one line PyPI shows in search results, and it comes from
    ``project.description``. Absent, the listing has no subtitle; long, it is truncated mid-word."""
    summary = (metadata.get("Summary") or "").strip()
    assert summary, "no Summary in the wheel metadata (project.description is empty)"
    assert len(summary) <= 120, f"Summary is {len(summary)} chars, long enough to be truncated"
    print(f"OK  summary ({len(summary)} chars): {summary}")


def check_runtime_assets_shipped(wheel: Path, repo_root: Path) -> None:
    """Every non-``.py`` file under ``backlot/`` in the checkout must be in the wheel.

    Compares the artifact against the source tree rather than against ``[tool.setuptools.
    package-data]``, because that table is not what decides the outcome any more: setuptools
    defaults ``include_package_data`` to true for a pyproject-based build, so the assets ship even
    with the globs deleted. Asserting the *declaration* is therefore no longer the same as asserting
    the *result* — this checks the result, and stays honest if a future setuptools flips that default
    back or someone adds an ``exclude-package-data`` entry.

    Each of these is read at runtime: no ``schemas/*.json`` and every BYO record fails validation, no
    ``graphql/*.graphql`` and ``import backlot.main`` raises, no ``data/*.jsonl`` and
    ``backlot import --bundled`` has nothing to load.
    """
    # `git ls-files`, not a directory walk: an untracked `backlot/.DS_Store` on someone's machine
    # would otherwise fail this for a wheel that is perfectly correct. tests/test_packaging.py reads
    # the index for the same reason.
    tracked = subprocess.run(
        ["git", "ls-files", "backlot"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    expected = {f for f in tracked if not f.endswith(".py")}
    with zipfile.ZipFile(wheel) as zf:
        shipped = set(zf.namelist())
    missing = sorted(expected - shipped)
    assert not missing, f"the wheel is missing {len(missing)} asset(s) the checkout has: {missing}"
    print(f"OK  all {len(expected)} runtime assets shipped")


def check_entry_point_is_declared(wheel: Path) -> None:
    """The ``backlot`` console script. Declared only in the distribution, so a broken
    ``[project.scripts]`` target is invisible to the test suite — ``wheel_smoke.py`` runs the script
    itself, and this asserts the wheel even carries one to run."""
    with zipfile.ZipFile(wheel) as zf:
        name = next((n for n in zf.namelist() if n.endswith(".dist-info/entry_points.txt")), None)
        assert name, "the wheel declares no entry points, so `backlot` will not be on PATH"
        text = zf.read(name).decode()
    assert "backlot = backlot.cli:main" in text, text
    print("OK  console script declared: backlot = backlot.cli:main")


def main(argv: list[str]) -> int:
    if argv:
        wheel = Path(argv[0])
    else:
        found = sorted(glob.glob("dist/*.whl"))
        assert found, "no wheel in ./dist — run `python -m build --wheel` first"
        wheel = Path(found[-1])
    print(f"inspecting {wheel}")
    metadata = read_metadata(wheel)
    check_no_direct_url_requirements(metadata)
    check_summary_is_present_and_short(metadata)
    check_entry_point_is_declared(wheel)
    check_runtime_assets_shipped(wheel, Path(__file__).resolve().parents[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
