"""Spin up a Backlot server in a subprocess — the package's primary entry point.

    import backlot

    with backlot.serve() as s:            # built-in hello-world corpus
        WebClient(token=s.token, base_url=f"{s.base_url}/slack/api/")

Pass ``records`` (BYO-JSONL dicts) to serve your own corpus instead. ``serve_or_connect`` prefers
an already-running server when one is reachable, which is what lets an example run against the
hosted deployment or a local one without changing code.

The bundled hello-world corpus (``HELLO_CORPUS``) is 136 records covering EVERY served source, with
several containers each (4 Slack channels, 3 Gmail mailboxes, 3 Drive folders, 2 repos, 2 Jira
projects, 2 Confluence spaces, 2 Notion teamspaces, 2 buckets, 2 Linear teams, 2 Fireflies
channels, 4 HubSpot object types) so a listing has more than one of anything to page through.

Two counts, and they differ on purpose. ``source_documents`` is 136 — what the corpus offered.
``/health``'s ``documents`` is 167, because parsing promotes structure into rows of the same table:
Slack ``replies`` and Gmail ``messages`` are messages in their own right. Child rows in the
per-vendor comment tables (Jira/Confluence/GitHub/Notion/Linear comments, and Fireflies sentences)
are in neither number — ``documents`` sums root documents only.

It is a demo corpus, not a fixture: assert on shapes and relationships, not on these totals. It also
leaves optional fields out on purpose — to see what a record MAY carry, read
``examples/bring-your-own-corpus/sample_corpus.jsonl`` instead ("Which corpus is which" in
``backlot/schemas/README.md`` lays out the three and what each is for).

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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from backlot.config import Settings

HELLO_CORPUS = Path(__file__).resolve().parent / "data" / "hello.jsonl"
# Settings default; per-user tokens are in <data_dir>/tokens.yaml. Used only as the LAST-RESORT
# fallback in serve_or_connect()'s remote branch, when the real token can't be fetched — either
# GET /_meta/users is disabled (BACKLOT_EXPOSE_TOKENS=false, a legitimate configuration) or the
# URL doesn't clear _trusted_for_token_fetch's bar (plain HTTP to a non-loopback host: treating an
# unauthenticated plaintext response as a credential there is the wrong default, not merely
# unavailable). serve() itself never falls back to this: it reads the real value via
# Settings() below.
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
    get credentials from that endpoint for a remote server they didn't start) — but only when
    ``url`` clears ``_trusted_for_token_fetch`` (``https``, or a loopback host): a plaintext
    response from an arbitrary non-loopback host is never treated as a credential, independent of
    whether the endpoint would have answered. In either case the fetch doesn't happen — the
    endpoint disabled (``BACKLOT_EXPOSE_TOKENS=false``) or the URL not meeting that bar — ``token``
    falls back to the ``Settings`` default as a GUESS, wrong if that server's operator also
    overrode ``BACKLOT_ADMIN_TOKEN``.
    """

    base_url: str
    token: str
    data_dir: Path | None


def _ensure_cert_bundle() -> None:
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


def _trusted_for_token_fetch(url: str) -> bool:
    """Whether it's safe to treat a plaintext ``GET /_meta/users`` response from ``url`` as a
    credential.

    Gates the token FETCH in ``serve_or_connect``'s remote branch only — not the connection
    itself; we already talk to whatever ``--url`` the caller passes, that's the whole feature. But
    fetching a token turns that response into this process's own credential, so it gets a higher
    bar than merely being reachable: ``https`` (transport-protected) or a loopback host (this
    project's two real remote callers, ``http://localhost:8000`` and
    ``https://backlot.brekkylab.com``, both already clear it). Parses the host with
    :mod:`urllib.parse` rather than string-matching, so ``https://evil.example/?x=localhost``
    can't slip through by merely containing the substring.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https":
        return True
    return parsed.hostname in ("localhost", "127.0.0.1", "::1")


def _admin_token_from_meta_users(url: str, timeout: float = 10) -> str | None:
    """Fetch the real admin token from a remote server's own ``GET /_meta/users`` — the same
    affordance ``examples/using-official-sdk/s3.py`` already points users at for a remote
    server's credentials. Returns None (not raises) if the endpoint 404s
    (``BACKLOT_EXPOSE_TOKENS=false``, a legitimate configuration, not an error) or the response
    is otherwise unusable, so the caller can fall back to a guess rather than fail the connect."""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/_meta/users", timeout=timeout) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read())
        token = data.get("admin_token")
        return token if isinstance(token, str) else None
    except Exception:  # noqa: BLE001
        return None


def _healthy(url: str, timeout: float = 10) -> bool:
    # Default timeout suits a remote deployment; serve()'s local readiness poll passes a
    # much smaller one (see _LOCAL_HEALTH_TIMEOUT) since a local subprocess either answers almost
    # immediately or hasn't bound the port yet, in which case the connection is refused, not slow.
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


@contextlib.contextmanager
def serve(records: list[dict] | None = None, host: str = "127.0.0.1"):
    """Serve ``records`` (or the bundled hello-world corpus) on a free local port.

    ``host`` is the bind address. The default keeps the server off every interface but loopback;
    pass ``"0.0.0.0"`` (or ``"::"``) to reach it from somewhere the loopback address does not
    resolve to this machine — a Docker container on Linux, where ``--add-host=…:host-gateway``
    resolves to the bridge address. A single interface's address works too. ``base_url`` follows:
    loopback for a wildcard bind, otherwise the address bound, since that is where the server
    answers."""
    with tempfile.TemporaryDirectory() as data_dir:
        if records is None:
            corpus = HELLO_CORPUS
        else:
            corpus = Path(data_dir) / "corpus.jsonl"
            corpus.write_text("\n".join(json.dumps(r) for r in records))
        env = {**os.environ, "BACKLOT_DATA_DIR": data_dir}
        # Read with the SAME env + cwd the subprocess below inherits (only BACKLOT_DATA_DIR
        # differs, and that doesn't affect admin_token), so this resolves to whatever token the
        # server will actually enforce — a caller's BACKLOT_ADMIN_TOKEN env var or a .env in
        # their cwd both change it (Settings declares env_file=".env"), and the returned
        # Server.token must track that: otherwise a caller's first authenticated call fails
        # with HTTP 200 / {"ok": false} (Slack fidelity), which reads as the caller's own mistake.
        token = Settings().admin_token
        # `-m backlot` rather than the `backlot` script: same CLI, but resolved through THIS
        # interpreter, so it works in an environment whose bin/ is not on PATH. No cwd= either —
        # the package resolves from site-packages exactly as it does from a checkout.
        subprocess.run(
            [sys.executable, "-m", "backlot", "import", str(corpus)],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        port = _free_port(host)
        base = f"http://{_dialable(host)}:{port}"
        # Captured to a file rather than subprocess.PIPE: a live pipe nobody drains can fill and
        # deadlock the child, and we only need the contents if uvicorn dies before serving.
        log_path = Path(data_dir) / "server.log"
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
                stdout=log_f,
                stderr=subprocess.STDOUT,
            )
        try:
            for _ in range(_LOCAL_HEALTH_ATTEMPTS):
                if proc.poll() is not None:
                    tail = log_path.read_text(errors="replace")[-4000:]
                    raise RuntimeError(
                        f"Backlot exited with code {proc.returncode} before becoming ready on "
                        f"{base}:\n{tail}"
                    )
                if _healthy(base, timeout=_LOCAL_HEALTH_TIMEOUT):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError(f"Backlot did not become ready on {base}")
            yield Server(base_url=base, token=token, data_dir=Path(data_dir))
        finally:
            _terminate(proc)


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
        _ensure_cert_bundle()
        if _healthy(url):
            print(f"using Backlot at {url}")
            # Fetched, not guessed, when possible AND safe (see Server.token's docstring): GET
            # /_meta/users on the remote server reports its real admin_token. Gated by
            # _trusted_for_token_fetch — this is about not treating an unauthenticated plaintext
            # response from an arbitrary host as a credential, NOT about `url`'s server being
            # untrustworthy in general (we already talk to it either way). Falls back to the
            # Settings-default GUESS whenever the fetch doesn't happen at all: the endpoint
            # disabled (BACKLOT_EXPOSE_TOKENS=false) or the URL not clearing that bar.
            token = TOKEN
            if _trusted_for_token_fetch(url):
                token = _admin_token_from_meta_users(url) or TOKEN
            yield Server(base_url=url.rstrip("/"), token=token, data_dir=None)
            return
        print(f"--url {url!r} is not reachable — falling back to a local server")
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
