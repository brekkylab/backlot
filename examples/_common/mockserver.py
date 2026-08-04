"""Point an example at a mock server — a given ``--url`` if reachable, else a local one.

``serve_or_connect(records)`` is the entry point: if ``--url <URL>`` is on the command line
and that server's ``/health`` responds, the example talks to it directly; otherwise it falls
back to ``mock_server``, which builds a throwaway DB from ``records`` and runs ``uvicorn``
against it — so the example stays self-contained with no separate process to launch.

    from _mockserver import serve_or_connect

    CORPUS = [{"source_type": "slack", "content": "hi"}]
    with serve_or_connect(CORPUS) as mock:
        ...  # point an SDK at mock.base_url, using mock.token (admin: sees everything)
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
import types
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve()
while not (ROOT / "pyproject.toml").exists():
    ROOT = ROOT.parent
TOKEN = "admin-service-token"  # Settings default; a per-user token is in <data>/tokens.yaml

# When talking to an HTTPS `--url` (e.g. a real deployment behind an ACM cert), Python's
# default SSL context may have no CA bundle on macOS (`ssl.get_default_verify_paths().cafile`
# is None) — so urllib and the SDKs would raise CERTIFICATE_VERIFY_FAILED even for a valid
# cert. certifi ships with the [examples] extra; point OpenSSL at it unless already configured.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def google_service_account_info(base_url: str, subject: str | None = None) -> tuple[dict, str | None]:
    """Fetch the mock's service-account key from ``/_mock/credentials`` — the mock-specific glue,
    standing in for the JSON you'd download from the Cloud Console. Returns ``(sa_info, subject)``
    where ``subject`` is the user to impersonate via domain-wide delegation (ACL-filtered to them)
    or None (bare service account → admin, sees everything). The caller turns ``sa_info`` into a
    credential with the official google-auth library (see the examples). ``token_uri`` inside
    ``sa_info`` already points at the mock's ``/oauth2/token``."""
    import json
    import urllib.request

    with urllib.request.urlopen(f"{base_url.rstrip('/')}/_mock/credentials") as r:
        sa = json.load(r)["service_account"]
    if subject:
        print(f"impersonating {subject} → responses are ACL-filtered to that user")
    return sa, subject


def google_oauth_user(base_url: str, user: str | None = None) -> tuple[str, str, str, str]:
    """Mock glue for the authorized-user (3LO) flow. Returns ``(client_id, client_secret,
    refresh_token, token_uri)``: the shared OAuth client's id/secret and ``token_uri`` from
    ``/_mock/credentials``, plus a user's bearer token (from ``/_mock/users`` — ``user`` if given,
    else the first) used as the ``refresh_token``. The caller builds the Credentials with the
    official google-auth library (see gmail.py); the library then refreshes against ``token_uri``
    (the mock's ``/oauth2/token``)."""
    import json
    import urllib.request

    with urllib.request.urlopen(f"{base_url.rstrip('/')}/_mock/credentials") as r:
        creds = json.load(r)
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/_mock/users") as r:
        users = json.load(r)["users"]
    who = next((u for u in users if u["email"] == user), None) if user else (users[0] if users else None)
    if who is None:
        raise SystemExit(f"--user {user!r} not found in /_mock/users" if user else "no users on the mock")
    print(f"authenticating as {who['email']} (authorized_user — client_id/secret + refresh token)")
    client = creds["oauth_client"]
    return client["client_id"], client["client_secret"], who["token"], creds["token_uri"]


def _url_from_argv(argv: list[str] | None = None) -> str | None:
    """The value of ``--url`` / ``--url=…`` on the command line, or None.

    ``serve_or_connect`` falls back to this so a script that does not parse ``--url`` itself still
    honours it — which is what ``bring-your-own-corpus/run.py`` relies on.
    """
    argv = sys.argv[1:] if argv is None else argv
    for i, a in enumerate(argv):
        if a == "--url" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--url="):
            return a.split("=", 1)[1]
    return None


def _healthy(url: str) -> bool:
    # generous timeout: a remote deployment may be a trans-continental HTTPS hop
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=10) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


@contextlib.contextmanager
def serve_or_connect(records: list[dict], url: str | None = None):
    """Use the given ``url`` mock if reachable; otherwise spin up a local one on ``records``.

    ``url`` defaults to ``--url`` on the command line, so a script that does not parse it still
    honours the flag."""
    url = (url or _url_from_argv() or "").strip()
    if url and _healthy(url):
        print(f"using mock server at {url}")
        yield types.SimpleNamespace(base_url=url.rstrip("/"), token=TOKEN, data_dir=None)
        return
    if url:
        print(f"--url {url!r} is not reachable — falling back to a local mock")
    with mock_server(records) as mock:
        yield mock


@contextlib.contextmanager
def mock_server(records: list[dict]):
    with tempfile.TemporaryDirectory() as data_dir:
        corpus = Path(data_dir) / "corpus.jsonl"
        corpus.write_text("\n".join(json.dumps(r) for r in records))
        env = {**os.environ, "MOCK_DATA_DIR": data_dir}
        subprocess.run([sys.executable, "-m", "app.importer.byo", str(corpus)],
                       cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
            cwd=ROOT, env=env)
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(100):
                try:
                    with urllib.request.urlopen(f"{base}/health", timeout=0.5) as r:
                        if r.status == 200:
                            break
                except Exception:  # noqa: BLE001
                    time.sleep(0.1)
            else:
                raise RuntimeError("mock server did not become ready")
            yield types.SimpleNamespace(base_url=base, token=TOKEN, data_dir=Path(data_dir))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
