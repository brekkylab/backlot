"""Surface (a): a server from the installed package, with no arguments and no checkout."""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from types import SimpleNamespace

import pytest

import backlot
import backlot.server
from backlot.server import _terminate
from tests._helpers import POSIX_SIGNALS, complete, readline_within


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


# A process that holds `serve()` and nothing else, as a library caller does: it prints the server's
# URL and pid, and with `FORK` set the pid of a child it forked that only sleeps, then waits for its
# stdin to close.
_HOLDS_A_SERVER = """
import os, sys, time
import backlot
import backlot.server

with backlot.serve() as s:
    forked = ""
    if os.environ.get("FORK"):
        forked = os.fork()
        if forked == 0:
            time.sleep(60)
            os._exit(0)
    print(s.base_url, backlot.server._CHILDREN[-1].pid, forked, flush=True)
    sys.stdin.read()
"""

_FORKS = pytest.mark.skipif(not hasattr(os, "fork"), reason="forks, which Windows cannot")


@pytest.mark.windows
@pytest.mark.parametrize(
    "end, forks",
    [
        pytest.param("kill", False, id="kill"),
        pytest.param("SIGTERM", False, marks=POSIX_SIGNALS, id="SIGTERM"),
        pytest.param("SIGHUP", False, marks=POSIX_SIGNALS, id="SIGHUP"),
        pytest.param("kill", True, marks=_FORKS, id="kill with a forked child alive"),
    ],
)
def test_the_server_ends_with_the_process_that_started_it(end, forks):
    """A process holding `serve()` that ends without unwinding it takes its server with it, through
    the stdin pipe `serve()` starts the server on, and a child it forked that is still running does
    not hold the server up. `kill()` is SIGKILL on POSIX and `TerminateProcess` on Windows; SIGTERM
    and SIGHUP end a POSIX process by their default disposition."""
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDS_A_SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={**os.environ, "FORK": "1"} if forks else None,
    )
    forked = []
    try:
        line = readline_within(holder.stdout, 120)
        assert line, "the process holding serve() printed no URL within 120s"
        url, pid, *forked = line.decode().split()
        if end == "kill":
            holder.kill()
        else:
            holder.send_signal(getattr(signal, end))
        holder.wait(timeout=30)
    finally:
        if holder.poll() is None:
            holder.kill()
    port = int(url.rsplit(":", 1)[1])
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            except OSError:
                break
            time.sleep(0.2)
        else:
            os.kill(int(pid), signal.SIGTERM)
            pytest.fail(
                f"the server on port {port} (pid {pid}) outlived the process that started it"
            )
    finally:
        for child in forked:
            os.kill(int(child), signal.SIGKILL)


# --------------------------------------------------------------------------- data_dir


def test_serve_owns_its_childrens_stdin(monkeypatch):
    """Neither child `serve` starts inherits the caller's stdin: the `import` that builds the corpus
    gets `stdin=DEVNULL`, and the `serve` that answers a pipe of `serve()`'s own, closed once that
    server has stopped. Under `backlot mcp` the inherited handle would be the MCP protocol pipe,
    and a child reading from it corrupts a live session while every test that only watches the
    server's answers stays green. Asserted on the call, since that is the only place the choice is
    visible from outside the child; the `subprocess.run` that starts the `import` reaches `Popen`
    too."""
    import backlot.server as server_mod

    stdin_of, started = {}, {}
    real_popen = server_mod.subprocess.Popen

    class Popen(real_popen):
        def __init__(self, args, *rest, **kwargs):
            command = args[args.index("backlot") + 1]
            stdin_of[command], started[command] = kwargs.get("stdin"), self
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(server_mod.subprocess, "Popen", Popen)
    with backlot.serve() as s:
        assert s.base_url
        assert not started["serve"].stdin.closed
    assert stdin_of == {"import": subprocess.DEVNULL, "serve": subprocess.PIPE}, stdin_of
    assert started["serve"].stdin.closed


@pytest.mark.parametrize(
    "platform, path, executable, base, starts_on_base",
    [
        # a venv on Windows: its python.exe is a launcher
        ("win32", ntpath, r"C:\venv\Scripts\python.exe", r"C:\Python\python.exe", True),
        # the venv's own python.exe by Windows' rules, in another case and with forward slashes
        ("win32", ntpath, r"C:\venv\Scripts\python.exe", "c:/VENV/scripts/PYTHON.EXE", False),
        # no venv: the base interpreter is sys.executable itself
        ("win32", ntpath, r"C:\Python\python.exe", r"C:\Python\python.exe", False),
        # a venv elsewhere: its python is no launcher
        ("linux", posixpath, "/venv/bin/python", "/usr/bin/python3", False),
    ],
)
def test_the_server_runs_on_the_interpreter_that_holds_its_files(
    platform, path, executable, base, starts_on_base, monkeypatch
):
    """Per row, the interpreter the server starts on and the environment it gets: the base
    interpreter, and the built environment plus the launcher variable, only in a venv on Windows
    (`backlot.server._interpreter` says why). Asserted on the call that starts it, which is all a
    run off Windows can see, with the platform, both paths and the path rules swapped on that
    module's own view of `sys` and `os`."""
    import backlot.server as server_mod

    view = SimpleNamespace(platform=platform, executable=executable, _base_executable=base)
    monkeypatch.setattr(server_mod, "sys", view)
    monkeypatch.setattr(server_mod, "os", SimpleNamespace(path=path, environ=os.environ))
    built, seen = [], []

    class Started(Exception):
        pass

    def run(argv, **kwargs):
        built.append(kwargs["env"])

    def Popen(argv, **kwargs):
        seen.append((argv[0], kwargs["env"]))
        raise Started

    # `subprocess.run` starts its process through `subprocess.Popen`, so the import is faked too: a
    # real one would reach the `Popen` fake first.
    monkeypatch.setattr(server_mod.subprocess, "run", run)
    monkeypatch.setattr(server_mod.subprocess, "Popen", Popen)
    with pytest.raises(Started), backlot.serve():
        pass
    if starts_on_base:
        assert seen == [(base, {**built[0], "__PYVENV_LAUNCHER__": executable})]
    else:
        assert seen == [(executable, built[0])]


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


def test_a_server_answers_for_its_corpus_after_one_opened_inside_it_ends(tmp_path):
    """`/health`, `/_meta/users`, `/_meta/credentials` and `/_meta/openapi/{source}` read the app
    off the request, not the module-level ``app``. The difference shows when ``backlot.main`` is
    re-imported while a first server is still up, which ``client_for`` does: a reload
    re-executes the module in the same namespace, so the first app's handlers found ``app`` rebound
    to the second one and read its state, and once the second lifespan had ended and closed its
    connection, the first server's `/_meta/users` was `sqlite3.ProgrammingError: Cannot operate on
    a closed database` while its vendor routes, which read ``request.app``, kept answering. In the
    suite this bit a module-scoped client whose first `/_meta/users` call came after a reload.
    Slack's `auth.test` reads the org off the settings `client_for` puts back on exit."""
    from tests._helpers import build_corpus, client_for

    def record(domain: str) -> dict:
        return {
            "source_type": "github",
            "doc_id": "gh-1",
            "repo": "one",
            "title": "Issue",
            "content": "body",
            "author_email": f"ava@{domain}",
            "visibility": "public",
        }

    first_settings = build_corpus(tmp_path / "first", [record("acme.com")])
    second_settings = build_corpus(tmp_path / "second", [record("other.com")])
    with client_for(first_settings) as first:
        assert first.get("/_meta/users").json()["org"] == "acme"
        with client_for(second_settings) as second:
            assert second.app is not first.app
            assert second.get("/_meta/users").json()["org"] == "other"
        # the second lifespan has ended and closed its connection; the first server is still up
        assert first.get("/health").json()["status"] == "ok"
        assert first.get("/_meta/users").json()["org"] == "acme"
        assert first.get("/_meta/credentials").json()["org"] == "acme"
        assert "/github/search/code" in first.get("/_meta/openapi/github").json()["paths"]
        h = {"Authorization": f"Bearer {first_settings.admin_token}"}
        assert first.post("/slack/api/auth.test", headers=h).json()["team"] == "acme"


def test_backlots_own_routes_answer_head_as_the_get(client, admin_h):
    """`HEAD /health` is the shape a liveness probe takes, and FastAPI's ``APIRoute`` refused it with
    the 405 the GitHub routes earned for the same reason (see
    ``backlot.main.answer_head_as_the_get_without_its_body``). These are Backlot's own routes, with
    no vendor to measure against, so the answer is Backlot's to choose: the GET's status and
    headers, the GET body's length, and no body, the same rule as GitHub's."""
    client.app.state.warm_thread.join()  # `/health` grows once the counts land; hold it still
    for path in ("/health", "/_meta/users", "/_meta/openapi/github", "/_meta/openapi/nope"):
        get, head = client.get(path, headers=admin_h), client.head(path, headers=admin_h)
        assert head.status_code == get.status_code, path
        assert head.headers["content-length"] == str(len(get.content)), path
        assert head.content == b"", path
    assert client.get("/_meta/openapi/nope").status_code == 404  # the 404 cell was a 404


def test_serving_a_db_older_than_the_schema_says_so_and_says_to_re_import(tmp_path):
    """A table this build reads that the DB does not have, then a column one of its tables does
    not have -- rather than an OperationalError per read either way. There is no migration -- a
    corpus is re-imported, not upgraded in place -- so naming the gap is the whole remedy. `serve()`
    over that data dir reports the same refusal, with the code uvicorn exits with when the app does
    not start (3) and nothing after it."""
    import sqlite3

    import pytest

    from backlot import store
    from tests._helpers import build_corpus, client_for

    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "google_drive",
                "doc_id": "gd-old",
                "subtype": "spreadsheet",
                "title": "Prose",
                "folder": "sales",
                "content": "a\nb",
                "author_email": "d@acme.com",
            }
        ],
    )
    conn = sqlite3.connect(settings.db_path)
    conn.execute("DROP TABLE gdrive_sheets")
    conn.commit()
    conn.close()
    assert store.missing_schema(sqlite3.connect(settings.db_path)) == ["gdrive_sheets"]

    with pytest.raises(RuntimeError) as e:
        with client_for(settings, reload=True):
            pass
    assert "gdrive_sheets" in str(e.value)
    assert "backlot import" in str(e.value)
    with pytest.raises(RuntimeError) as e:
        with backlot.serve(data_dir=settings.data_dir):
            pass
    assert "exited with code 3 " in str(e.value)
    assert "gdrive_sheets" in str(e.value)
    assert "Fatal Python error" not in str(e.value)

    # A column this build reads that the table predates is the same refusal, named as
    # `table.column` — before the check knew columns, `name contains` was a bare 500 per request
    # on such a DB while the plain listing and `name =` answered 200.
    conn = sqlite3.connect(settings.db_path)
    conn.executescript(store.SCHEMA)  # the table comes back; the column is then dropped
    conn.execute("ALTER TABLE gdrive_files DROP COLUMN title_fold")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError) as e:
        with client_for(settings, reload=True):
            pass
    assert "gdrive_files.title_fold" in str(e.value)
