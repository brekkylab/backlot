"""Serve Backlot's sources as MCP tools over stdio — what ``backlot mcp`` runs.

    pip install "backlot[mcp]" && claude mcp add backlot -- backlot mcp

One command, one process, every source. An MCP client starts ``backlot mcp`` as a stdio subprocess;
this module finds (or starts) a Backlot server and serves that server's APIs as tools:

- the REST sources through ``FastMCP.from_openapi()`` over the MCP-ready spec Backlot publishes at
  ``GET /_meta/openapi/<source>`` (``backlot.openapi`` slices the app's own ``/openapi.json`` to the
  source and collapses the GET/POST and v2/v3 fidelity aliases, so nothing is cleaned up here);
- the GraphQL sources, Linear and Fireflies, by introspecting ``POST /<source>/graphql`` and serving
  each root ``Query`` field as a typed tool. ``backlot.graphql.mcp_tools`` owns that derivation —
  tool names, argument schemas, generated selection sets — and this module is its transport.

S3 comes through the same OpenAPI path, but signed rather than bearer-authenticated: SigV4 signs
each request, so instead of a fixed ``Authorization`` header the client signs every call with
``backlot.sigv4`` — the module the S3 router verifies inbound signatures with, so the two cannot
drift apart. ``examples/using-mcp-with-agents/s3.py`` still drives awslabs' server, because
pointing a real vendor client at Backlot is its own thing to show.

**One credential input.** ``--user <email>`` names a person, and this module resolves that person's
whole credential set from the server's own ``GET /_meta/users`` — bearer token, and the S3
access-key pair. Each source is then spelled the way it authenticates: Bearer, Atlassian's Basic
``email:token``, S3's SigV4. Every tool call carries it, so Backlot's per-document ACL decides what
comes back. Without ``--user`` the admin sees everything.

**Which server.** ``--url`` when given; otherwise the default local port if a Backlot server already
answers there; otherwise one started here, as a subprocess over the data dir's corpus (or the
bundled hello-world corpus when the data dir holds none), and stopped when the client disconnects.
That last branch is what makes the install a single line.

**stdout is the transport.** Nothing here writes to it: diagnostics go to stderr, and the server
this may start is a subprocess whose output ``backlot.server.serve`` captures to a file. stdio only,
not streamable-HTTP — FastMCP's HTTP mode has a known bug forwarding the client's ``Authorization``
header.
"""

from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import hashlib
import json
import os
import signal
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backlot import sigv4
from backlot.openapi import SOURCE_PREFIXES

OPENAPI_SOURCES: tuple[str, ...] = tuple(SOURCE_PREFIXES)
GRAPHQL_SOURCES: tuple[str, ...] = ("linear", "fireflies")
SOURCES: tuple[str, ...] = OPENAPI_SOURCES + GRAPHQL_SOURCES

# Where `backlot serve` listens by default, and so the one place worth probing before starting a
# server of our own.
DEFAULT_URL = "http://127.0.0.1:8000"

# How long `is_backlot` waits for `/health`. The speculative probe of the default port gets 2s: it
# runs on every start and nothing is lost by a miss but a local server the caller did not name. A
# server the caller NAMED with --url gets the budget `backlot.server._healthy` reserves for a remote,
# possibly trans-continental hop; the hosted deployment answers `/health` in 0.8–1.0s, so 2s reads a
# merely slow server as absent. Measured: a `/health` that answers after 3s is False at 2s and True
# at 10s.
PROBE_TIMEOUT = 2
NAMED_SERVER_TIMEOUT = 10

# The prefix a source's route handlers carry to stay unique inside one module (`routers/google.py`
# holds Gmail and Drive together), which under the `<source>_` namespace would be said twice:
# `gmail_gmail_messages_list`, `gdrive_drive_files_list`. `mounted_name` folds it into the namespace.
_HANDLER_PREFIX: dict[str, str] = {"gmail": "gmail", "gdrive": "drive"}

# The one per-source knob, measured (figures in examples/using-mcp-with-agents/README.md). Linear
# runs at 1: `Team`, `Project` and `Cycle` each carry dozens of configuration leaves that a second
# level would multiply across every issue. Fireflies needs the derivation's default of 2, because
# `Analytics` has no leaf fields of its own and a shallower selection drops the node whole. Sources
# absent here take that default.
DEPTH: dict[str, int] = {"linear": 1}


# Backlot's verifier reads the region out of the client's own credential scope, so any value
# validates; this is boto3's default.
S3_REGION = "us-east-1"


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Credentials:
    """One caller's whole credential set, as the server itself reports it.

    ``email`` is None for the admin, the one caller ``GET /_meta/users`` does not list as a person,
    which is also why Atlassian falls back to Bearer for them: Basic needs a username.
    """

    token: str
    email: str | None
    s3_access_key_id: str
    s3_secret_access_key: str


def resolve(base_url: str, user: str | None) -> Credentials:
    """The credentials for ``user`` (an email), or the admin's when ``user`` is None.

    An email the corpus does not know is refused, not silently promoted to the admin — a caller who
    named a person and got the admin's unfiltered view would have no way to tell.
    """
    from backlot import server as backlot_server

    try:
        directory = backlot_server.meta_users(base_url)
    except Exception as exc:  # noqa: BLE001 — unreachable, non-200, or not a directory
        sys.exit(f"cannot read {base_url}/_meta/users, so --user cannot be resolved: {exc}")
    # A field the directory does not carry is reported, not raised: an unhandled KeyError here
    # leaves the client with nothing but "Connection closed" (see `_fetch_json`).
    try:
        if user is None:
            return Credentials(
                token=directory["admin_token"],
                email=None,
                s3_access_key_id=directory["admin_s3_access_key_id"],
                s3_secret_access_key=directory["admin_s3_secret_access_key"],
            )
        # Matched case-insensitively, the way Atlassian and Google both treat an address: an
        # email copied out of the shape a Jira page displays it must not miss the person. The
        # entry's OWN spelling is what goes into Credentials, so Atlassian's Basic header carries
        # the corpus's.
        for entry in directory.get("users", ()):
            if str(entry.get("email", "")).casefold() == user.casefold():
                return Credentials(
                    token=entry["token"],
                    email=entry["email"],
                    s3_access_key_id=entry["s3_access_key_id"],
                    s3_secret_access_key=entry["s3_secret_access_key"],
                )
    except KeyError as exc:
        sys.exit(f"{base_url}/_meta/users is missing {exc} — it is not a Backlot user directory")
    known = len(directory.get("users", ()))
    sys.exit(
        f"{user!r} is not one of the {known} users this corpus has "
        f"(GET {base_url}/_meta/users lists them)"
    )


def _auth_header(creds: Credentials, source: str) -> dict[str, str]:
    # Basic `email:token` is Atlassian's scheme, where the api_token IS the Backlot token, and the
    # email is what mcp-atlassian sends. S3 has no header here at all: `_sign_sigv4` signs it.
    if source == "atlassian" and creds.email:
        raw = f"{creds.email}:{creds.token}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    return {"Authorization": f"Bearer {creds.token}"}


def _sign_sigv4(creds: Credentials, request) -> None:
    """Sign one httpx request for Backlot's S3 surface, in place, with :mod:`backlot.sigv4` — the
    module the S3 router verifies with, so the canonical request cannot be built two different
    ways. S3 signs the wire path VERBATIM, which is why the raw path is used, not the decoded one.
    """
    now = datetime.now(timezone.utc)
    amz_date = now.strftime(sigv4.AMZ_DATE_FORMAT)
    date_stamp = amz_date[:8]
    payload_hash = hashlib.sha256(request.content or b"").hexdigest()
    request.headers["x-amz-date"] = amz_date
    request.headers["x-amz-content-sha256"] = payload_hash
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    raw_path, _, _ = request.url.raw_path.partition(b"?")
    signature = sigv4.expected_signature(
        creds.s3_secret_access_key,
        request.method,
        raw_path.decode("ascii"),
        request.url.query.decode("ascii"),
        {k.lower(): v for k, v in request.headers.items()},
        signed_headers,
        payload_hash,
        amz_date,
        date_stamp,
        S3_REGION,
    )
    scope = f"{date_stamp}/{S3_REGION}/s3/aws4_request"
    request.headers["Authorization"] = (
        f"{sigv4.ALGORITHM} Credential={creds.s3_access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def _fetch_json(
    url: str, *, headers: dict[str, str] | None = None, body: dict | None = None
) -> Any:
    """GET (or POST ``body`` as JSON) and decode. A failure exits with the server's own error body:
    a stdio server that dies before the handshake reaches the client as an opaque
    ``Connection closed`` whatever the cause, so the reason has to land on stderr, where whoever
    holds the terminal can read it — not in a urllib traceback."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()
        sys.exit(f"{url} answered HTTP {exc.code}: {detail[:500]}")
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach {url}: {exc.reason}")


def is_backlot(url: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether a *Backlot* server answers at ``url`` — not merely something with a ``/health``.

    The default port is shared with every other local dev server, and a 200 from one of those would
    send every tool call into 404s. Backlot's ``/health`` body always carries ``documents``
    (``null`` while the caches warm), which is enough to tell it apart. ``timeout`` is the probe's
    budget: the default suits the speculative look at the default port, and a caller who named a
    server passes ``NAMED_SERVER_TIMEOUT``."""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=timeout) as r:
            return r.status == 200 and "documents" in json.load(r)
    except Exception:  # noqa: BLE001 — refused, timed out, not JSON: all mean "not here"
        return False


# --------------------------------------------------------------------------- one source


def openapi_spec(base_url: str, source: str) -> dict:
    """The MCP-ready spec ``base_url`` serves for ``source`` — its operationIds are the tool names."""
    return _fetch_json(f"{base_url}/_meta/openapi/{source}")


def openapi_server(base_url: str, creds: Credentials, source: str, *, spec: dict | None = None):
    """A FastMCP server for one REST source: the MCP-ready spec, served over a client whose base
    URL is Backlot and which carries ``creds`` on every call — as a header for the bearer sources,
    as a per-request signature for S3. ``spec`` is that spec when the caller already fetched it."""
    import httpx2
    from fastmcp import FastMCP

    if spec is None:
        spec = openapi_spec(base_url, source)

    class _SigV4(httpx2.Auth):
        # An auth flow, not a header: it is the hook that sees each request in its final form.
        # `requires_request_body` is what guarantees `request.content` is readable by then.
        requires_request_body = True

        def auth_flow(self, request):
            _sign_sigv4(creds, request)
            yield request

    signed = source == "s3"
    # httpx2, not httpx: `from_openapi` takes an httpx2 client. A legacy httpx one is still
    # accepted, under a deprecation warning that says it will stop being accepted.
    client = httpx2.AsyncClient(
        base_url=base_url,
        headers={} if signed else _auth_header(creds, source),
        auth=_SigV4() if signed else None,
        timeout=30,
    )
    # validate_output=False: Backlot's responses are the source of truth; a passthrough must never
    # reject a real server response for not matching a loose schema.
    return FastMCP.from_openapi(
        openapi_spec=spec, client=client, name=f"{source}-bridge", validate_output=False
    )


def graphql_outcome(body: dict) -> tuple[str, bool]:
    """``(content, is_error)`` for one GraphQL response envelope.

    The envelope goes through untouched, on the same principle as ``validate_output=False`` in
    :func:`openapi_server`. What is added is the MCP error flag, so a caller can tell a refusal from
    an answer — raised only when there is no ``data`` at all (a rejected document, or a non-null
    field the ACL hid), not for a field error that came back beside partial data: under the wider
    rule an agent would throw away a mostly-complete answer."""
    failed = bool(body.get("errors")) and body.get("data") is None
    return json.dumps(body), failed


def graphql_server(base_url: str, creds: Credentials, source: str, *, depth: int | None = None):
    """A FastMCP server for one GraphQL source: one tool per root ``Query`` field, derived from the
    endpoint's own introspection, each posting a fixed document with the caller's arguments as
    variables. ``depth`` is how many object levels the generated selection sets reach."""
    import httpx2
    from fastmcp import FastMCP
    from fastmcp.tools import Tool, ToolResult

    from backlot.graphql import mcp_tools

    endpoint = f"{base_url}/{source}/graphql"
    headers = _auth_header(creds, source)
    # The introspection request carries the credential, so a bad token fails here, before a single
    # tool exists, with the endpoint's own error body.
    introspection = _fetch_json(
        endpoint, headers=headers, body={"query": mcp_tools.INTROSPECTION_QUERY}
    )
    if depth is None:
        depth = DEPTH.get(source, mcp_tools.DEFAULT_DEPTH)
    try:
        tools = mcp_tools.derive_tools(introspection, depth=depth)
    except ValueError as exc:  # a 200 carrying an error envelope rather than a schema
        sys.exit(str(exc))

    client = httpx2.AsyncClient(headers=headers, timeout=30)

    class _Field(Tool):
        """One root field. The document is fixed; the caller's arguments are the variables."""

        spec: Any

        async def run(self, arguments: dict) -> ToolResult:
            # `with_page_default` bounds an unpaged list call and leaves a caller that paged for
            # itself alone; the rule and its reason are in `mcp_tools`.
            variables = mcp_tools.with_page_default(self.spec, arguments)
            r = await client.post(
                endpoint, json={"query": self.spec.document, "variables": variables}
            )
            content, failed = graphql_outcome(r.json())
            return ToolResult(content=content, is_error=failed)

    server = FastMCP(name=f"{source}-graphql-bridge")
    for tool in tools:
        server.add_tool(
            _Field(
                name=tool.name,
                description=tool.description,
                parameters=tool.input_schema,
                spec=tool,
            )
        )
    return server


# --------------------------------------------------------------------------- every source


def build_server(
    base_url: str,
    creds: Credentials,
    sources: Sequence[str],
    *,
    depth: int | None = None,
):
    """One FastMCP server over ``sources``.

    A single source is served as itself, so its tools keep the names Backlot's own routes give them
    (``search_messages``, ``jira_get_issue``). Several are mounted under the source's own name as a
    namespace — ``slack_search_messages``, ``github_get_issue`` — because two vendors can and do
    name an operation the same way."""
    from fastmcp import FastMCP

    def build(source: str):
        if source in GRAPHQL_SOURCES:
            return graphql_server(base_url, creds, source, depth=depth), None
        spec = openapi_spec(base_url, source)
        return openapi_server(base_url, creds, source, spec=spec), spec

    # Each source costs one round trip to the server before its tools exist (a spec fetch or an
    # introspection), and the default run has ten of them. Serially that is ten round trips before
    # the MCP handshake completes — about 10s against a hosted deployment answering in ~1s — and
    # some MCP clients give a server less than that to come up. Built concurrently it is about one.
    # A failure inside a worker (`_fetch_json` exits with the server's own error) surfaces here on
    # iteration, as it would have inline.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sources)) as pool:
        built = dict(zip(sources, pool.map(build, sources)))
    servers = {source: server for source, (server, _) in built.items()}
    specs = {source: spec for source, (_, spec) in built.items() if spec is not None}
    if len(servers) == 1:
        return next(iter(servers.values()))
    # Listed from what was mounted: a client feeds `instructions` to the model as the server's
    # description, and a fixed list would advertise the sources a `--source` run left out.
    namespaces = ", ".join(f"{source}_*" for source in servers)
    instructions = (
        "Backlot serves these enterprise SaaS APIs over a local corpus. Tools are namespaced "
        f"by source: {namespaces}."
    )
    if "atlassian" in servers:
        instructions += " atlassian_* covers both Jira and Confluence."
    root = FastMCP(name="backlot", instructions=instructions)
    for source, server in servers.items():
        # `tool_names` renames a tool before the namespace is prefixed, so what goes in is the
        # tail `mounted_name` wants after `<source>_`. Only the tools whose default name would
        # stutter need an entry; their operationIds are the spec's.
        renamed = {
            op["operationId"]: mounted_name(source, op["operationId"])[len(source) + 1 :]
            for item in specs.get(source, {}).get("paths", {}).values()
            for method, op in item.items()
            if isinstance(op, dict)
            and "operationId" in op
            and mounted_name(source, op["operationId"]) != f"{source}_{op['operationId']}"
        }
        root.mount(server, namespace=source, tool_names=renamed or None)
    return root


def mounted_name(source: str, tool: str) -> str:
    """``tool``'s name under the ``<source>_`` namespace of the composite server.

    ``<source>_<tool>``, except that a prefix the route name already carries to stay unique inside
    its module is folded into the namespace rather than repeated: Gmail's ``gmail_messages_list``
    stays ``gmail_messages_list`` and Drive's ``drive_files_list`` becomes ``gdrive_files_list``,
    where mounting alone would say ``gmail_gmail_messages_list`` and ``gdrive_drive_files_list`` to a
    model that reads the name on every call."""
    prefix = _HANDLER_PREFIX.get(source)
    if prefix and tool.startswith(prefix + "_"):
        return f"{source}_{tool[len(prefix) + 1 :]}"
    return f"{source}_{tool}"


# --------------------------------------------------------------------------- which server


@contextlib.contextmanager
def attach(url: str | None = None) -> Iterator[str]:
    """Yield the ``base_url`` of the server to bridge: the one at ``url``, else the one on the
    default local port, else one started here for as long as the block runs.

    An explicit ``url`` that does not answer is an error rather than a fallback — the caller named
    a server, and quietly serving a different corpus in its place is the wrong kind of helpful.
    """
    from backlot import server as backlot_server

    url = (url or "").strip().rstrip("/")
    if url:
        backlot_server.ensure_cert_bundle()
        if not is_backlot(url, timeout=NAMED_SERVER_TIMEOUT):
            sys.exit(f"no Backlot server answers at {url}")
        yield url
        return
    if is_backlot(DEFAULT_URL):
        _stderr(f"using the Backlot server at {DEFAULT_URL}")
        # A data dir names a corpus the way --url names a server, and this branch cannot honour
        # it: whatever is on the default port serves the corpus its own operator gave it. Said
        # rather than silently dropped — the caller would otherwise read that server's answers as
        # their own corpus's.
        if os.environ.get("BACKLOT_DATA_DIR"):
            _stderr(
                f"(--data-dir is not used: that server serves whatever corpus its own operator gave "
                f"it; pass --url to name a server, or free the port to have one started over "
                f"{os.environ['BACKLOT_DATA_DIR']})"
            )
        yield DEFAULT_URL
        return

    from backlot.config import get_settings

    settings = get_settings()  # honours BACKLOT_DATA_DIR, which `backlot mcp --data-dir` sets
    if settings.db_path.exists():
        _stderr(f"starting Backlot over the corpus in {settings.data_dir}")
        started = backlot_server.serve(data_dir=settings.data_dir)
    else:
        _stderr(
            f"no corpus in {settings.data_dir}; starting Backlot over the bundled hello-world "
            "corpus (`backlot import <corpus.jsonl>` to serve your own)"
        )
        started = backlot_server.serve()
    global _started
    with started as s:
        _stderr(f"Backlot is up at {s.base_url}")
        _started = started
        try:
            yield s.base_url
        finally:
            _started = None


# The `serve()` context `attach` entered when it started a server itself, while that server is up.
# The signal handler closes it directly rather than by raising through the stack: an exception
# raised out of a signal handler has to cross fastmcp's stdio transport on the way up, and there it
# can be held waiting on the stdin reader thread — non-daemon, blocked in a read nobody will end —
# so the exit is late or never (measured: with stdin held open, two of four SIGHUP runs in CI were
# still alive 20s later; SIGTERM alone used to wait until stdin closed). Closing the context here
# and leaving with `os._exit` depends on nothing above the handler.
_started: contextlib.AbstractContextManager | None = None


def _exit_on_signal(signum: int, frame: object) -> None:
    # A terminating signal's default disposition ends the process without unwinding, which would
    # leave the server `attach` started running with no client. Installed for SIGINT and SIGHUP as
    # well as SIGTERM: a terminal's Ctrl-C or hangup reaches the whole process group, uvicorn
    # included, but a supervisor that signals only its direct child does not — measured, `kill -INT`
    # alone left the server listening, and `kill -HUP` ended this process by default and orphaned it.
    started = _started
    if started is not None:
        started.__exit__(None, None, None)  # `serve()`'s finally: SIGTERM the server, then wait
    sys.stderr.flush()
    os._exit(128 + signum)


# The signals a supervisor or terminal sends to end a stdio server. SIGHUP does not exist on Windows.
_TERMINATING_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGINT", "SIGHUP") if hasattr(signal, name)
)


def run(
    sources: Sequence[str] | None = None,
    *,
    url: str | None = None,
    user: str | None = None,
    depth: int | None = None,
) -> None:
    """Serve ``sources`` (default: all of them) as MCP tools over stdio until the client hangs up."""
    try:
        import fastmcp  # noqa: F401 — the check is the import
    except ImportError:
        sys.exit('backlot mcp needs the [mcp] extra:  pip install "backlot[mcp]"')

    for sig in _TERMINATING_SIGNALS:
        signal.signal(sig, _exit_on_signal)
    try:
        with attach(url) as base_url:
            creds = resolve(base_url, user)
            server = build_server(base_url, creds, sources or SOURCES, depth=depth)
            # No banner: fastmcp prints one to stderr, which is harmless to the protocol but noise
            # in every MCP client's log.
            server.run(transport="stdio", show_banner=False)
    except RuntimeError as exc:
        # `serve` raises this when the server it started died before answering — a corrupt corpus
        # in the data dir is the common cause — carrying that server's own last words. They are
        # the diagnosis; a traceback through contextlib would bury them.
        sys.exit(str(exc))
