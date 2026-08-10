#!/usr/bin/env python3
"""End-to-end checks against an INSTALLED Backlot wheel.

The suite in ``tests/`` runs against the source tree, so it cannot see anything that depends on what
the wheel actually contains: a data file no glob shipped, an entry point that was never declared, a
path default that resolves inside ``site-packages``. Those only surface from an install. This script
is what the ``wheel-install`` CI job runs, and it is a plain script rather than a pytest module on
purpose — the venv under test holds the wheel and its runtime dependencies and nothing else, so
adding pytest there would change the thing being measured.

    python -m build --wheel
    python -m venv /tmp/wheel-venv
    /tmp/wheel-venv/bin/pip install dist/*.whl
    cd /tmp && /tmp/wheel-venv/bin/python <checkout>/tests/e2e/wheel_smoke.py

The working directory must be outside the checkout. ``check_import_resolves_to_the_install`` asserts
that rather than trusting it: run from the repo root, every other check here would pass while
testing the source tree, which is exactly the blind spot this file exists to close.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BIN = Path(sys.executable).parent  # the venv under test; `backlot` is beside its python
CORPUS_RECORD = {
    "source_type": "slack",
    "channel": "general",
    "author_email": "ci@example.com",
    "content": "Served through the CLI.",
}


def _get_json(url: str, token: str | None = None, timeout: float = 10) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return json.load(r)


def _run(*args: str, **kw) -> subprocess.CompletedProcess:
    """Run one of the venv's own executables, failing loudly on a non-zero exit."""
    return subprocess.run([str(BIN / args[0]), *args[1:]], check=True, text=True, **kw)


def _wait_for_health(url: str, attempts: int = 60, delay: float = 0.5) -> dict:
    for _ in range(attempts):
        time.sleep(delay)
        try:
            return _get_json(url)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            continue
    raise AssertionError(f"{url} never became healthy")


# --- checks ---------------------------------------------------------------------------------


def check_import_resolves_to_the_install() -> str:
    """`backlot` must come from site-packages, not from the checkout.

    Everything below is meaningless otherwise, and the failure is silent: `python -c "import
    backlot"` from the repo root succeeds either way. `pip install -e` leaves the source tree
    importable, which is why CI installing editable could never answer a question about the wheel.
    """
    import backlot

    origin = Path(backlot.__file__).resolve()
    assert not origin.is_relative_to(REPO_ROOT), (
        f"backlot resolved to the checkout ({origin}), not an install — run this from a working "
        f"directory outside {REPO_ROOT}"
    )
    return str(origin)


def check_main_imports() -> None:
    """A bare import of the ASGI module.

    This is what a missing `graphql/*.graphql` broke: `routers/linear.py` and `routers/fireflies.py`
    build their engine at import time, so `import backlot.main` raised FileNotFoundError outright on
    a clean install while the source tree was perfectly happy.
    """
    import backlot.main  # noqa: F401


def check_mock_server_authenticates() -> dict:
    """`backlot.mock_server()` with no arguments — the package's primary entry point.

    No arguments means it serves the bundled corpus, so this also proves `data/*.jsonl` shipped. The
    token is read off the returned object rather than assumed, since that is what a caller does.
    """
    import backlot

    with backlot.mock_server() as m:
        assert _get_json(f"{m.base_url}/slack/api/auth.test", m.token)["ok"] is True, (
            "auth.test did not authenticate with MockServer.token"
        )
        health = _get_json(f"{m.base_url}/health")
    for key in ("source_documents", "documents"):
        assert key in health, f"/health is missing {key}: {health}"
    assert health["documents"] > 0, health
    return health


def check_packaged_schemas_validate() -> None:
    """A BYO record through the validator, which loads `schemas/*.json` from the package.

    Without those files `SERVICE_SCHEMAS` is empty and every record fails with a message that points
    at the data instead of the packaging, so this asserts the valid case rather than a rejection.
    """
    from backlot.importer.byo import record_errors

    errors = record_errors(
        {
            "source_type": "confluence",
            "space": "handbook",
            "title": "CI smoke record",
            "content": "Exercises the packaged JSON Schemas.",
            "author_email": "ci@example.com",
        }
    )
    assert errors == [], errors


def check_console_script(tmp: Path, port: int = 8123) -> dict:
    """The `backlot` console script, end to end: import a corpus, then serve it.

    An entry point exists only in an installed distribution, so `tests/test_cli.py` — which calls
    `cli.main` in-process — cannot catch a missing or misspelled `[project.scripts]` target. This is
    the only place that can.
    """
    _run("backlot", "--version")
    _run("backlot", "import", "--help", stdout=subprocess.DEVNULL)

    corpus = tmp / "cli-corpus.jsonl"
    corpus.write_text(json.dumps(CORPUS_RECORD) + "\n")
    env = {**_environ(), "BACKLOT_DATA_DIR": str(tmp / "cli-data")}
    _run("backlot", "import", str(corpus), "--dry-run", env=env)
    _run("backlot", "import", str(corpus), env=env)

    server = subprocess.Popen([str(BIN / "backlot"), "serve", "--port", str(port)], env=env)
    try:
        health = _wait_for_health(f"http://127.0.0.1:{port}/health")
    finally:
        server.terminate()
        server.wait(timeout=30)
    assert health["documents"] == 1, health
    return health


def check_console_script_serves_the_bundled_corpus(tmp: Path, port: int = 8124) -> dict:
    """`backlot import --bundled` — the quickstart the README leads with.

    A path into the checkout is exactly what a pip-installed user does not have, so this is the only
    corpus the CLI can load with no data to hand.
    """
    env = {**_environ(), "BACKLOT_DATA_DIR": str(tmp / "bundled-data")}
    _run("backlot", "import", "--bundled", env=env, stdout=subprocess.DEVNULL)
    server = subprocess.Popen([str(BIN / "backlot"), "serve", "--port", str(port)], env=env)
    try:
        health = _wait_for_health(f"http://127.0.0.1:{port}/health")
    finally:
        server.terminate()
        server.wait(timeout=30)
    assert health["documents"] > 1, health
    assert all(health["by_source"].values()), f"a source served nothing: {health['by_source']}"
    return health


def _environ() -> dict:
    import os

    return dict(os.environ)


def main() -> int:
    import tempfile

    origin = check_import_resolves_to_the_install()
    print(f"backlot resolved to {origin}")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        checks = [
            ("import backlot.main", check_main_imports),
            ("mock_server() authenticates", check_mock_server_authenticates),
            ("packaged schemas validate", check_packaged_schemas_validate),
            ("console script: import + serve", lambda: check_console_script(tmp)),
            (
                "console script: --bundled covers every source",
                lambda: check_console_script_serves_the_bundled_corpus(tmp),
            ),
        ]
        for label, fn in checks:
            result = fn()
            detail = f" -> {result}" if result is not None else ""
            print(f"OK  {label}{detail}")
    print(f"\n{len(checks)} wheel checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
