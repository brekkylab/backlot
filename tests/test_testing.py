"""Surface (a): a server from the installed package, with no arguments and no checkout."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

import backlot
from backlot.testing import _terminate


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def test_mock_server_with_no_arguments_serves_the_hello_corpus():
    with backlot.mock_server() as m:
        body = _get(f"{m.base_url}/health")
    assert body["status"] == "ok"
    assert body["source_documents"] > 0
    assert body["documents"] >= body["source_documents"]


def test_mock_server_accepts_records():
    with backlot.mock_server(
        [
            {
                "source_type": "confluence",
                "space": "handbook",
                "title": "Only Page",
                "content": "The only document.",
                "author_email": "ava@acme.com",
            },
        ]
    ) as m:
        body = _get(f"{m.base_url}/health")
    assert body["source_documents"] == 1


def test_mock_server_token_authenticates():
    with backlot.mock_server() as m:
        req = urllib.request.Request(
            f"{m.base_url}/slack/api/auth.test", headers={"Authorization": f"Bearer {m.token}"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            assert json.load(r)["ok"] is True


def test_mock_server_token_reflects_a_custom_admin_token(monkeypatch):
    """Regression: MockServer.token used to be the hardcoded Settings default even when the
    caller's environment configured a different admin token — mock_server() passes os.environ
    through to the subprocess, so the SERVER enforced "some-other-token" while the returned
    MockServer.token still said "admin-service-token". The failure mode isn't an exception:
    Slack fidelity means auth.test returns HTTP 200 with {"ok": false, "error": "not_authed"},
    which reads as the caller's own mistake rather than a mock_server() bug."""
    monkeypatch.setenv("BACKLOT_ADMIN_TOKEN", "some-other-token")
    with backlot.mock_server() as m:
        assert m.token == "some-other-token"
        req = urllib.request.Request(
            f"{m.base_url}/slack/api/auth.test", headers={"Authorization": f"Bearer {m.token}"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            assert json.load(r)["ok"] is True


def test_serve_or_connect_fetches_a_remote_servers_real_admin_token(monkeypatch):
    """Regression: serve_or_connect's remote branch used to return the hardcoded Settings
    default as a GUESS for any remote server, even though the server exposes its real
    admin_token at GET /_mock/users (the same endpoint examples/using-official-sdk/s3.py already
    points users at, for exactly this purpose). Start a server configured with a non-default
    token, then connect to it via --url and confirm the returned token is the real one fetched
    from the server, not the guess."""
    monkeypatch.setenv("BACKLOT_ADMIN_TOKEN", "remote-real-token")
    with backlot.mock_server() as server:
        with backlot.serve_or_connect(url=server.base_url) as m:
            assert m.token == "remote-real-token"
            assert m.token != "admin-service-token"  # the old guess would have returned this
            req = urllib.request.Request(
                f"{m.base_url}/slack/api/auth.test",
                headers={"Authorization": f"Bearer {m.token}"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                assert json.load(r)["ok"] is True


def test_serve_or_connect_does_not_fetch_the_token_over_plain_http_to_a_non_loopback_host(
    monkeypatch,
):
    """Hardening: fetching a credential from an unauthenticated plaintext response is the wrong
    default once the host isn't loopback. Only https or loopback should trigger the
    GET /_mock/users fetch at all — a plain-http non-loopback URL must fall back to the guess
    WITHOUT the fetch ever being attempted. Asserted by spying on
    `_admin_token_from_mock_users` and requiring it was never called, which is a stronger claim
    than just checking the returned token (that could coincidentally match)."""
    import backlot.testing as testing_mod

    monkeypatch.setattr(testing_mod, "_healthy", lambda url, timeout=10: True)

    calls = []
    monkeypatch.setattr(
        testing_mod,
        "_admin_token_from_mock_users",
        lambda url, timeout=10: calls.append(url) or "should-never-be-used",
    )

    with backlot.serve_or_connect(url="http://example.com:8000") as m:
        assert m.token == testing_mod.TOKEN

    assert calls == [], f"token fetch must not run against a plain-http non-loopback host: {calls}"


def test_two_servers_get_different_ports():
    with backlot.mock_server() as a, backlot.mock_server() as b:
        assert a.base_url != b.base_url


def test_serve_or_connect_falls_back_when_the_url_is_unreachable():
    with backlot.serve_or_connect(url="http://127.0.0.1:1/") as m:
        assert _get(f"{m.base_url}/health")["status"] == "ok"


def test_teardown_reaps_a_process_that_ignores_sigterm():
    """A bare kill() with no following wait() can leave a zombie — this would pass silently
    without a test that checks the process was actually reaped, not just signalled."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ]
    )
    _terminate(proc, timeout=0.2)
    assert proc.poll() is not None
