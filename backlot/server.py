"""Spin up a Backlot server in a subprocess — the package's primary entry point.

    import backlot

    with backlot.serve() as s:            # built-in hello-world corpus
        WebClient(token=s.token, base_url=f"{s.base_url}/slack/api/")

Pass ``records`` (BYO-JSONL dicts) to serve your own corpus instead. ``serve_or_connect`` prefers
an already-running server when one is reachable, which is what lets an example run against the
hosted deployment or a local one without changing code.

The bundled hello-world corpus (``HELLO_CORPUS``) covers EVERY served source, with several
containers each — channels, mailboxes, folders, repos, projects, spaces, teamspaces, buckets, teams
and object types — so a listing always has more than one of anything to page through. ``backlot
import`` prints the per-source breakdown as it loads; no count is written down here, because the
corpus grows whenever a source or a behaviour needs a record and a written one goes stale on that
commit.

Two counts, and they differ on purpose. ``source_documents`` is what the corpus OFFERED;
``/health``'s ``documents`` is what is SERVED, and it is larger for two reasons. Parsing promotes
structure into rows of the same table: Slack ``replies`` and Gmail ``messages`` are messages in
their own right. And a record is not always a document — a ``subtype: "repo"`` record states a
repository's refs and becomes no row in the document table at all, so it is counted as offered and
never as served. Child rows in the per-vendor comment tables (Jira/Confluence/GitHub/Notion/Linear
comments, and Fireflies sentences) are in neither number — ``documents`` sums root documents only.

It is a demo corpus, not a fixture: assert on shapes and relationships, not on these totals. It also
leaves optional fields out on purpose — to see what a record MAY carry, read
``examples/bring-your-own-corpus/sample_corpus.jsonl`` instead ("Which corpus is which" in
``backlot/schemas/README.md`` lays out the three and what each is for).

Deliberately a subprocess and a real port, not an in-process ASGI transport: the official vendor
SDKs, the MCP servers, and the mirage mounts all make real HTTP calls, and a fake transport would
not exercise them. This module imports no FastAPI, so ``import backlot`` stays cheap.

``backlot.main`` DEFINES the ASGI app; this module holds the handle to a RUNNING one — the
``Server`` dataclass and the two ways to get one. ``serve`` starts a subprocess.
``serve_or_connect`` starts one or attaches to a deployment already up, which is what lets an
example run against the hosted server without changing code. Neither is test-only: ``backlot.cli``
reads ``HELLO_CORPUS`` from here to serve ``backlot import --bundled``.
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

from backlot.config import Settings

HELLO_CORPUS = Path(__file__).resolve().parent / "data" / "hello.jsonl"
# Settings default; per-user tokens are in <data_dir>/tokens.yaml. Used only as the LAST-RESORT
# fallback in serve_or_connect()'s remote branch, when GET /_meta/users can't be read at all — a
# network failure, or a server too old to serve it. serve() itself never falls back to this: it
# reads the real value via Settings() below.
TOKEN = "admin-service-token"

# How long serve()'s local readiness poll waits for each attempt, and how many attempts it
# makes. A subprocess that hasn't bound its port yet refuses the connection almost instantly, so
# this is deliberately tight — unlike _healthy()'s own default, which has to allow for a remote,
# possibly trans-continental HTTPS hop.
_LOCAL_HEALTH_TIMEOUT = 0.5
_LOCAL_HEALTH_ATTEMPTS = 100


@dataclass(frozen=True)
class Server:
    """A reachable Backlot server. ``data_dir`` is None when connected to a remote one.

    ``token`` is MEASURED, not assumed, in both cases where that's possible. For a server this
    process started (``serve()``), it reads ``Settings().admin_token`` with the same
    environment / cwd ``.env`` the subprocess inherits, so a caller's ``BACKLOT_ADMIN_TOKEN``
    override is reflected correctly. For an already-running remote server
    (``serve_or_connect``'s remote-``url`` branch), it is fetched from that server's own
    ``GET /_meta/users`` — a Backlot-only affordance (``backlot/main.py``) that already serves
    ``admin_token`` for exactly this purpose (``examples/using-official-sdk/s3.py`` tells users to
    get credentials from that endpoint for a remote server they didn't start). When that read
    fails, ``token`` falls back to the ``Settings`` default as a GUESS, wrong if that server's
    operator also overrode ``BACKLOT_ADMIN_TOKEN``.
    """

    base_url: str
    token: str
    data_dir: Path | None


def ensure_cert_bundle() -> None:
    # Talking to an HTTPS url (a deployment behind an ACM cert) can fail with
    # CERTIFICATE_VERIFY_FAILED on macOS, where Python's default SSL context has no CA bundle.
    # certifi ships with the [examples] extra; point OpenSSL at it unless already configured.
    # Called only from the code paths that may hit a remote https:// url — not at import time,
    # so a bare `import backlot` never mutates process-global environment as a side effect.
    try:
        import certifi

        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    except ImportError:
        pass


def _free_port(host: str = "127.0.0.1") -> int:
    # Probed on the address the server will actually bind, in that address's own family: a port
    # free on loopback can still be taken on another interface, and the server would then die on
    # startup instead of serving.
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _dialable(host: str) -> str:
    """The address a client on THIS machine dials to reach a server bound to ``host``.

    A wildcard bind answers on every interface, loopback among them, and loopback is the one that
    stays right whatever the machine's addresses are; any narrower bind is dialled where it is
    bound, because that is the only place it answers. IPv6 comes back bracketed, as a URL needs."""
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    return f"[{host}]" if ":" in host else host


def _terminate(proc: subprocess.Popen, timeout: float = 10) -> None:
    """Stop ``proc`` and reap it. A bare ``kill()`` can still leave a zombie if nothing calls
    ``wait()`` afterward — this always does, even on the SIGTERM-ignoring fallback path."""
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# Every server `serve()` has started and not yet stopped. Between `Popen` returning and the context
# manager yielding, the child is alive but nothing above this module can reach it: the context is
# not entered yet, so there is no `finally` for a `with` (or an ExitStack) to unwind, and that is
# exactly where a client that gives up on a slow start sends its signal. `stop_children` is the
# way out of that window; the normal path stays the context manager's own `finally`.
_CHILDREN: list[subprocess.Popen] = []


def stop_children() -> None:
    """Terminate and reap every server ``serve()`` started that is still running.

    For a caller ending the process without unwinding — ``backlot mcp``'s signal handler — where a
    server may have been spawned but not yet yielded. Safe to call when ``serve()``'s own
    ``finally`` is already at work on the same child: terminating an exited process is a no-op."""
    for proc in list(_CHILDREN):
        _terminate(proc)
        if proc in _CHILDREN:
            _CHILDREN.remove(proc)


def meta_users(url: str, timeout: float = 10) -> dict:
    """The directory a Backlot server publishes at ``GET /_meta/users``: every user's email and
    token, each one's S3 access-key pair, and the admin's own.

    Raises when it cannot be read or is not a directory at all; a directory that is one but lacks
    the individual fields a caller needs is :func:`backlot.mcp.resolve`'s to report. Strict because
    a caller naming a user (``backlot mcp --user``) cannot be satisfied by a guess — a resolution
    that silently became the admin would answer with the admin's unfiltered view.
    :func:`admin_token_for` is the lenient wrapper ``serve_or_connect`` wants instead.
    """
    with urllib.request.urlopen(f"{url.rstrip('/')}/_meta/users", timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"{url}/_meta/users answered HTTP {r.status}")
        data = json.loads(r.read())
    if not isinstance(data, dict) or not isinstance(data.get("admin_token"), str):
        raise RuntimeError(f"{url}/_meta/users did not answer a user directory")
    return data


def admin_token_for(url: str) -> str:
    """The admin token for the already-running server at ``url``: fetched from its own
    ``GET /_meta/users``, and the package default as a GUESS when that read fails — see
    ``Server.token``'s docstring for when the guess is wrong.

    Lenient where :func:`meta_users` is strict: a server that won't say who its admin is still
    serves every fallback an example needs, so refusing to connect would break a working session
    over a credential the caller may never use.
    """
    try:
        return meta_users(url)["admin_token"]
    except Exception:  # noqa: BLE001 — unreachable, 404, or not a directory: all mean "guess"
        return TOKEN


def _healthy(url: str, timeout: float = 10) -> bool:
    # Default timeout suits a remote deployment; serve()'s local readiness poll passes a
    # much smaller one (see _LOCAL_HEALTH_TIMEOUT) since a local subprocess either answers almost
    # immediately or hasn't bound the port yet, in which case the connection is refused, not slow.
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _warm(url: str, timeout: float) -> bool:
    """Whether the server has finished warming its caches, not merely bound its port.

    ``/health`` answers 200 with ``documents: null`` while the warm-up thread is still filling
    (see ``backlot.main``'s lifespan), so a readiness poll that only checks the status code hands
    back a server whose counts are not computed yet. Every consumer falls back to its own query,
    which is why this is invisible until something reads the counts themselves: the caller sees a
    null where a number belongs, once in a hundred runs, on whichever interpreter happened to be
    slower that day.

    Only ``serve()`` uses this. ``_healthy`` stays as it is for the remote branch of
    ``serve_or_connect``, where a deployment that is serving correctly on the fallbacks must not
    be refused for being mid-warm-up or permanently degraded.

    A recorded ``warm_error`` counts as warm: the caches will never fill, the server serves
    correctly without them, and waiting longer would only turn a reported failure into a timeout.
    """
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=timeout) as r:
            if r.status != 200:
                return False
            body = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return False
    return body.get("documents") is not None or body.get("warm_error") is not None


@contextlib.contextmanager
def serve(
    records: list[dict] | None = None, host: str = "127.0.0.1", *, data_dir: Path | None = None
):
    """Serve ``records`` (or the bundled hello-world corpus) on a free local port.

    ``data_dir`` serves an existing data dir as it stands — one ``backlot import`` already built —
    instead of importing anything; it is what ``backlot mcp`` uses to put the user's own corpus
    behind the tools. It cannot be combined with ``records``, which describe a corpus that does not
    exist yet.

    ``host`` is the bind address. The default keeps the server off every interface but loopback;
    pass ``"0.0.0.0"`` (or ``"::"``) to reach it from somewhere the loopback address does not
    resolve to this machine — a Docker container on Linux, where ``--add-host=…:host-gateway``
    resolves to the bridge address. A single interface's address works too. ``base_url`` follows:
    loopback for a wildcard bind, otherwise the address bound, since that is where the server
    answers."""
    if records is not None and data_dir is not None:
        raise ValueError("serve() takes records to import or a data_dir already built, not both")
    with tempfile.TemporaryDirectory() as scratch:
        if data_dir is None:
            data_dir = Path(scratch)
            if records is None:
                corpus = HELLO_CORPUS
            else:
                corpus = data_dir / "corpus.jsonl"
                corpus.write_text("\n".join(json.dumps(r) for r in records))
        env = {**os.environ, "BACKLOT_DATA_DIR": str(data_dir)}
        # Read with the SAME env + cwd the subprocess below inherits (only BACKLOT_DATA_DIR
        # differs, and that doesn't affect admin_token), so this resolves to whatever token the
        # server will actually enforce — a caller's BACKLOT_ADMIN_TOKEN env var or a .env in
        # their cwd both change it (Settings declares env_file=".env"), and the returned
        # Server.token must track that: otherwise a caller's first authenticated call fails
        # with HTTP 200 / {"ok": false} (Slack fidelity), which reads as the caller's own mistake.
        token = Settings().admin_token
        if data_dir == Path(scratch):
            # `-m backlot` rather than the `backlot` script: same CLI, but resolved through THIS
            # interpreter, so it works in an environment whose bin/ is not on PATH. No cwd= either —
            # the package resolves from site-packages exactly as it does from a checkout.
            subprocess.run(
                [sys.executable, "-m", "backlot", "import", str(corpus)],
                env=env,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )
        port = _free_port(host)
        base = f"http://{_dialable(host)}:{port}"
        # Captured to a file rather than subprocess.PIPE: a live pipe nobody drains can fill and
        # deadlock the child, and we only need the contents if uvicorn dies before serving.
        # In the scratch dir, not the data dir: a caller's own data dir is theirs to keep clean.
        log_path = Path(scratch) / "server.log"
        with open(log_path, "wb") as log_f:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "backlot",
                    "serve",
                    "--host",
                    host,
                    "--port",
                    str(port),
                    "--log-level",
                    "warning",
                ],
                env=env,
                # The server reads nothing from stdin, and must not inherit the caller's: when
                # the caller is `backlot mcp`, that is the MCP protocol pipe.
                stdin=subprocess.DEVNULL,
                stdout=log_f,
                stderr=subprocess.STDOUT,
            )
        _CHILDREN.append(proc)
        try:
            for _ in range(_LOCAL_HEALTH_ATTEMPTS):
                if proc.poll() is not None:
                    tail = log_path.read_text(errors="replace")[-4000:]
                    raise RuntimeError(
                        f"Backlot exited with code {proc.returncode} before becoming ready on "
                        f"{base}:\n{tail}"
                    )
                if _warm(base, timeout=_LOCAL_HEALTH_TIMEOUT):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError(f"Backlot did not become ready on {base}")
            yield Server(base_url=base, token=token, data_dir=Path(data_dir))
        finally:
            _terminate(proc)
            if proc in _CHILDREN:
                _CHILDREN.remove(proc)


@contextlib.contextmanager
def serve_or_connect(
    records: list[dict] | None = None, url: str | None = None, host: str = "127.0.0.1"
):
    """Use the server at ``url`` if reachable, else spin one up locally on ``records``.

    ``url`` is used only when given — this does not read ``sys.argv``. Pass
    ``url=backlot.url_from_argv()`` to honour a ``--url`` flag the way the bundled examples do.

    ``host`` is ``serve``'s bind address, and so applies only when this falls back to a
    local server; a reachable ``url`` is already bound however its own operator bound it."""
    url = (url or "").strip()
    if url:
        ensure_cert_bundle()
        if _healthy(url):
            # stderr, here and below: a caller may be an MCP stdio server whose stdout IS the
            # protocol stream, and one stray line there ends the session.
            print(f"using Backlot at {url}", file=sys.stderr)
            # Fetched, not guessed, when the server will say (see Server.token's docstring): GET
            # /_meta/users on the remote server reports its real admin_token. Falls back to the
            # Settings-default GUESS when that read fails at all.
            yield Server(base_url=url.rstrip("/"), token=admin_token_for(url), data_dir=None)
            return
        print(f"--url {url!r} is not reachable — falling back to a local server", file=sys.stderr)
    with serve(records, host=host) as s:
        yield s


def url_from_argv(argv: list[str] | None = None) -> str | None:
    """The value of ``--url`` / ``--url=…`` on ``argv`` (default ``sys.argv[1:]``), or None.

    Not read implicitly by ``serve_or_connect`` — a library reading the host program's command
    line on its own would silently intercept a consumer's own ``--url`` flag. Call this explicitly
    and pass the result along instead."""
    argv = sys.argv[1:] if argv is None else argv
    for i, a in enumerate(argv):
        if a == "--url" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--url="):
            return a.split("=", 1)[1]
    return None
