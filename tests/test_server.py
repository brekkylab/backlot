"""Surface (a): a server from the installed package, with no arguments and no checkout."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.request
from types import SimpleNamespace

import pytest

import backlot
import backlot.server
from tests._helpers import complete
from backlot.server import _terminate


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
    """The counts are asserted, not just the status, and that is what makes this a real check:
    ``/health`` answers 200 with ``documents: null`` for as long as the warm-up thread is still
    filling its caches, so a ``serve()`` that returned on the status code alone would hand back a
    server whose counts are not computed yet. It did, and this failed on one interpreter and not
    the others. ``_warm`` is the readiness poll's condition now; the parametrized test below pins
    the three answers it has to distinguish.
    """
    with backlot.serve() as s:
        body = _get(f"{s.base_url}/health")
    assert body["status"] == "ok"
    assert body["source_documents"] > 0
    assert body["documents"] >= body["source_documents"]


@pytest.mark.parametrize(
    "body, warm",
    [
        pytest.param({"status": "ok", "documents": 41}, True, id="counts-landed"),
        pytest.param({"status": "ok", "documents": None}, False, id="still-warming"),
        # The caches will never fill and the server serves correctly on the fallbacks, so waiting
        # longer would turn a reported failure into `did not become ready`.
        pytest.param(
            {"status": "degraded", "documents": None, "warm_error": "OperationalError: x"},
            True,
            id="warm-up-failed",
        ),
        pytest.param({"status": "ok", "documents": 0}, True, id="an-empty-corpus-is-warm"),
    ],
)
def test_readiness_waits_for_the_warm_up_but_not_for_a_failed_one(body, warm, monkeypatch):
    import contextlib as ctx
    import json as json_mod

    import backlot.server as server_mod

    @ctx.contextmanager
    def fake_urlopen(url, timeout=None):
        yield SimpleNamespace(status=200, read=lambda: json_mod.dumps(body).encode())

    monkeypatch.setattr(server_mod.urllib.request, "urlopen", fake_urlopen)
    assert server_mod._warm("http://x", timeout=0.5) is warm


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
    ) as s:
        body = _get(f"{s.base_url}/health")
    assert body["source_documents"] == 1


def test_serve_token_authenticates():
    with backlot.serve() as s:
        req = urllib.request.Request(
            f"{s.base_url}/slack/api/auth.test", headers={"Authorization": f"Bearer {s.token}"}
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
    with backlot.serve() as s:
        assert s.token == "some-other-token"
        req = urllib.request.Request(
            f"{s.base_url}/slack/api/auth.test", headers={"Authorization": f"Bearer {s.token}"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            assert json.load(r)["ok"] is True


def test_serve_or_connect_fetches_a_remote_servers_real_admin_token(monkeypatch):
    """serve_or_connect's remote branch must not return the hardcoded Settings
    default as a GUESS for any remote server, even though the server exposes its real
    admin_token at GET /_meta/users (the same endpoint examples/using-official-sdk/s3.py already
    points users at, for exactly this purpose). Start a server configured with a non-default
    token, then connect to it via --url and confirm the returned token is the real one fetched
    from the server, not the guess."""
    monkeypatch.setenv("BACKLOT_ADMIN_TOKEN", "remote-real-token")
    with backlot.serve() as remote:
        with backlot.serve_or_connect(url=remote.base_url) as s:
            assert s.token == "remote-real-token"
            assert s.token != "admin-service-token"  # the old guess would have returned this
            req = urllib.request.Request(
                f"{s.base_url}/slack/api/auth.test",
                headers={"Authorization": f"Bearer {s.token}"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                assert json.load(r)["ok"] is True


def test_meta_users_is_strict_where_admin_token_for_is_lenient():
    """The two readers of ``GET /_meta/users`` answer different questions, so they fail
    differently. ``meta_users`` backs ``backlot mcp --user``, which names a person and cannot be
    satisfied by a guess — a silent fallback would serve the admin's unfiltered view of the corpus
    under that person's name. ``admin_token_for`` only asks who the admin is, and a server that
    won't say still serves every fallback an example needs, so it guesses rather than refusing to
    connect."""
    import backlot.server as server_mod

    dead = "http://127.0.0.1:1"
    with pytest.raises(Exception):  # noqa: B017 — urllib's own error, whatever it is
        server_mod.meta_users(dead, timeout=2)
    assert server_mod.admin_token_for(dead) == server_mod.TOKEN


def test_meta_users_reports_a_real_servers_own_credentials():
    """Against a live server it is the directory ``backlot mcp --user`` resolves through: the
    admin's token and access-key pair, and one entry per user carrying the same."""
    with backlot.serve() as s:
        directory = backlot.server.meta_users(s.base_url)
    assert directory["admin_token"] == s.token
    assert directory["admin_s3_access_key_id"].startswith("AKIA")
    assert directory["users"], directory
    one = directory["users"][0]
    assert {"email", "token", "s3_access_key_id", "s3_secret_access_key"} <= one.keys()


@pytest.mark.parametrize("host, answers_off_loopback", [("127.0.0.1", False), ("0.0.0.0", True)])
def test_serve_binds_where_host_says(host, answers_off_loopback):
    """A wildcard bind is what lets something that cannot reach this machine's loopback reach
    Backlot: a Docker container on Linux, where ``--add-host=…:host-gateway`` resolves to the bridge
    address (tests/test_mcp.py runs the Atlassian MCP server that way). The narrow default is
    asserted alongside it, since one that quietly answered everywhere would pass either way.

    Both halves dial this machine's own routable address, because only a request settles where a
    server answers — and both ask whether BACKLOT answered, not whether the port accepted a
    connection, which is a fact about the machine rather than about this server."""
    addr = _routable_address()
    if addr is None:
        pytest.skip("this machine has no address but loopback to dial")

    with backlot.serve(host=host) as s:
        # A wildcard bind answers on every interface, loopback among them, so the URL a caller
        # gets stays loopback in both cases here.
        assert s.base_url.startswith("http://127.0.0.1:"), s.base_url
        assert _health(f"{s.base_url}/health")["status"] == "ok"

        port = int(s.base_url.rsplit(":", 1)[1])
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

    with backlot.serve(host=host) as s:
        assert s.base_url.startswith(url_form.format(host)), s.base_url
        assert _health(f"{s.base_url}/health")["status"] == "ok"


def test_two_servers_get_different_ports():
    with backlot.serve() as a, backlot.serve() as b:
        assert a.base_url != b.base_url


def test_serve_or_connect_falls_back_when_the_url_is_unreachable():
    with backlot.serve_or_connect(url="http://127.0.0.1:1/") as s:
        assert _get(f"{s.base_url}/health")["status"] == "ok"


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


# --------------------------------------------------------------------------- data_dir


def test_serve_data_dir_serves_an_existing_build_as_it_stands(tmp_path):
    """``backlot mcp`` puts the user's own corpus behind the tools this way: a data dir one
    ``backlot import`` already built is served without re-importing, and nothing is written into
    it — the server log goes to scratch, not beside the user's database."""
    import json
    import urllib.request

    from backlot import cli

    assert cli.main(["import", "--bundled", "--data-dir", str(tmp_path)]) == 0
    before = sorted(p.name for p in tmp_path.iterdir())
    with backlot.serve(data_dir=tmp_path) as s:
        with urllib.request.urlopen(f"{s.base_url}/health") as r:
            body = json.load(r)
        assert body["documents"] and body["documents"] > 0
        assert s.data_dir == tmp_path
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_serve_refuses_records_together_with_a_data_dir(tmp_path):
    with pytest.raises(ValueError, match="not both"):
        with backlot.serve([{"source_type": "slack"}], data_dir=tmp_path):
            pass


def test_serve_or_connect_reports_on_stderr_not_stdout(capsys):
    """A caller may be an MCP stdio server whose stdout is the protocol stream; one stray line
    there ends the session. Both messages — attached, and falling back — go to stderr."""
    with backlot.serve() as remote:
        with backlot.serve_or_connect(url=remote.base_url):
            pass
    with backlot.serve_or_connect(url="http://127.0.0.1:1"):
        pass
    out, err = capsys.readouterr()
    assert out == ""
    assert "using Backlot at" in err and "falling back to a local server" in err
