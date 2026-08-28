"""Surface (a): a server from the installed package, with no arguments and no checkout."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.request

import pytest

import backlot
from tests._helpers import complete
from backlot.testing import _terminate


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def _routable_address() -> str | None:
    """This machine's own address on the interface a packet would leave by, or None when the only
    address it has is loopback (an offline or network-isolated host)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: a UDP connect() picks a route, sends nothing
            addr = s.getsockname()[0]
        except OSError:
            return None
    return None if addr.startswith("127.") else addr


def _ipv6_loopback() -> str | None:
    """``"::1"`` when this machine has a usable IPv6 stack, else None — some CI networks have none.
    Measured by binding it, not by ``socket.has_ipv6``, which only reports the build."""
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
    except OSError:
        return None
    return "::1"


def _health(url: str) -> dict:
    """``GET /health`` at ``url``, or ``{}`` when nothing there answers as Backlot — the connection
    refused, or some other program holding that port."""
    try:
        return _get(url)
    except Exception:  # noqa: BLE001
        return {}


def test_serve_with_no_arguments_serves_the_hello_corpus():
    with backlot.serve() as m:
        body = _get(f"{m.base_url}/health")
    assert body["status"] == "ok"
    assert body["source_documents"] > 0
    assert body["documents"] >= body["source_documents"]


def test_serve_accepts_records():
    with backlot.serve(
        [
            complete(
                source_type="confluence",
                space="handbook",
                title="Only Page",
                content="The only document.",
                author_email="ava@acme.com",
            ),
        ]
    ) as m:
        body = _get(f"{m.base_url}/health")
    assert body["source_documents"] == 1


def test_serve_token_authenticates():
    with backlot.serve() as m:
        req = urllib.request.Request(
            f"{m.base_url}/slack/api/auth.test", headers={"Authorization": f"Bearer {m.token}"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            assert json.load(r)["ok"] is True


def test_serve_token_reflects_a_custom_admin_token(monkeypatch):
    """Server.token must not be the hardcoded Settings default when the
    caller's environment configured a different admin token — serve() passes os.environ
    through to the subprocess, so the SERVER enforced "some-other-token" while the returned
    Server.token still said "admin-service-token". The failure mode isn't an exception:
    Slack fidelity means auth.test returns HTTP 200 with {"ok": false, "error": "not_authed"},
    which reads as the caller's own mistake rather than a serve() bug."""
    monkeypatch.setenv("BACKLOT_ADMIN_TOKEN", "some-other-token")
    with backlot.serve() as m:
        assert m.token == "some-other-token"
        req = urllib.request.Request(
            f"{m.base_url}/slack/api/auth.test", headers={"Authorization": f"Bearer {m.token}"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            assert json.load(r)["ok"] is True


def test_serve_or_connect_fetches_a_remote_servers_real_admin_token(monkeypatch):
    """serve_or_connect's remote branch must not return the hardcoded Settings
    default as a GUESS for any remote server, even though the server exposes its real
    admin_token at GET /_mock/users (the same endpoint examples/using-official-sdk/s3.py already
    points users at, for exactly this purpose). Start a server configured with a non-default
    token, then connect to it via --url and confirm the returned token is the real one fetched
    from the server, not the guess."""
    monkeypatch.setenv("BACKLOT_ADMIN_TOKEN", "remote-real-token")
    with backlot.serve() as server:
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


@pytest.mark.parametrize("host, answers_off_loopback", [("127.0.0.1", False), ("0.0.0.0", True)])
def test_serve_binds_where_host_says(host, answers_off_loopback):
    """A wildcard bind is what lets something that cannot reach this machine's loopback reach the
    mock: a Docker container on Linux, where ``--add-host=…:host-gateway`` resolves to the bridge
    address (tests/test_mcp.py runs the Atlassian MCP server that way). The narrow default is
    asserted alongside it, since one that quietly answered everywhere would pass either way.

    Both halves dial this machine's own routable address, because only a request settles where a
    server answers — and both ask whether BACKLOT answered, not whether the port accepted a
    connection, which is a fact about the machine rather than about this server."""
    addr = _routable_address()
    if addr is None:
        pytest.skip("this machine has no address but loopback to dial")

    with backlot.serve(host=host) as m:
        # A wildcard bind answers on every interface, loopback among them, so the URL a caller
        # gets stays loopback in both cases here.
        assert m.base_url.startswith("http://127.0.0.1:"), m.base_url
        assert _health(f"{m.base_url}/health")["status"] == "ok"

        port = int(m.base_url.rsplit(":", 1)[1])
        answered = _health(f"http://{addr}:{port}/health").get("status") == "ok"
        assert answered is answers_off_loopback


@pytest.mark.parametrize(
    "resolve, url_form",
    [(_routable_address, "http://{}:"), (_ipv6_loopback, "http://[{}]:")],
    ids=["one-interface", "ipv6-loopback"],
)
def test_a_narrow_bind_is_dialled_at_the_address_it_bound(resolve, url_form):
    """``host`` is an address, not a switch between loopback and everywhere: a server bound to one
    interface answers only there, so ``base_url`` has to name it. A ``base_url`` fixed at
    127.0.0.1 leaves a server that is up and a readiness poll knocking somewhere it never bound,
    which fails ten seconds later saying the server never came up."""
    host = resolve()
    if host is None:
        pytest.skip("this machine has no such address to bind")

    with backlot.serve(host=host) as m:
        assert m.base_url.startswith(url_form.format(host)), m.base_url
        assert _health(f"{m.base_url}/health")["status"] == "ok"


def test_two_servers_get_different_ports():
    with backlot.serve() as a, backlot.serve() as b:
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
