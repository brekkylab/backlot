"""Spin up a Backlot server in a subprocess — the package's primary entry point.

    import backlot

    with backlot.mock_server() as m:            # built-in hello-world corpus
        WebClient(token=m.token, base_url=f"{m.base_url}/slack/api/")

Pass ``records`` (BYO-JSONL dicts) to serve your own corpus instead. ``serve_or_connect`` prefers
an already-running server when one is reachable, which is what lets an example run against the
hosted deployment or a local one without changing code.

The packaged hello-world corpus (``HELLO_CORPUS``) covers all ten sources in 10 records; two of
them (github, jira) carry ``comments``, which exercise the real per-vendor comment APIs but are
not counted in ``/health``'s ``documents`` total — that number sums only the eleven root-document
tables, not their children.

Deliberately a subprocess and a real port, not an in-process ASGI transport: the official vendor
SDKs, the MCP servers, and the mirage mounts all make real HTTP calls, and a fake transport would
not exercise them. This module imports no FastAPI, so ``import backlot`` stays cheap.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HELLO_CORPUS = Path(__file__).resolve().parent / "data" / "hello.jsonl"
TOKEN = "admin-service-token"  # Settings default; per-user tokens are in <data_dir>/tokens.yaml

# Talking to an HTTPS url (a deployment behind an ACM cert) can fail with
# CERTIFICATE_VERIFY_FAILED on macOS, where Python's default SSL context has no CA bundle. certifi
# ships with the [examples] extra; point OpenSSL at it unless already configured.
try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass


@dataclass(frozen=True)
class MockServer:
    """A reachable Backlot server. ``data_dir`` is None when connected to a remote one."""

    base_url: str
    token: str
    data_dir: Path | None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _healthy(url: str) -> bool:
    # generous timeout: a remote deployment may be a trans-continental HTTPS hop
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=10) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


@contextlib.contextmanager
def mock_server(records: list[dict] | None = None):
    """Serve ``records`` (or the packaged hello-world corpus) on a free local port."""
    with tempfile.TemporaryDirectory() as data_dir:
        if records is None:
            corpus = HELLO_CORPUS
        else:
            corpus = Path(data_dir) / "corpus.jsonl"
            corpus.write_text("\n".join(json.dumps(r) for r in records))
        env = {**os.environ, "BACKLOT_DATA_DIR": data_dir}
        # No cwd= : the modules resolve through the installed package, so this works from
        # site-packages exactly as it does from a checkout.
        subprocess.run(
            [sys.executable, "-m", "backlot.importer.byo", str(corpus)],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        port = _free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backlot.main:app",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            env=env,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(100):
                if _healthy(base):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError(f"Backlot did not become ready on {base}")
            yield MockServer(base_url=base, token=TOKEN, data_dir=Path(data_dir))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


@contextlib.contextmanager
def serve_or_connect(records: list[dict] | None = None, url: str | None = None):
    """Use the server at ``url`` if reachable, else spin one up locally on ``records``.

    ``url`` defaults to a ``--url`` value on the command line, so a script that does not parse the
    flag itself still honours it."""
    url = (url or _url_from_argv() or "").strip()
    if url and _healthy(url):
        print(f"using Backlot at {url}")
        yield MockServer(base_url=url.rstrip("/"), token=TOKEN, data_dir=None)
        return
    if url:
        print(f"--url {url!r} is not reachable — falling back to a local server")
    with mock_server(records) as m:
        yield m


def _url_from_argv(argv: list[str] | None = None) -> str | None:
    argv = sys.argv[1:] if argv is None else argv
    for i, a in enumerate(argv):
        if a == "--url" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--url="):
            return a.split("=", 1)[1]
    return None
