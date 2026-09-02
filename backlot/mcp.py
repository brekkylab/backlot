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

S3 is the one source with no bridge: it is SigV4-signed, so a fixed ``Authorization`` header cannot
sign each request. ``examples/using-mcp-with-agents/s3.py`` drives it through awslabs' server.

Every tool call carries the caller's credential, so Backlot's per-token ACL decides what comes back —
pass ``--token`` a per-user token from ``GET /_meta/users`` to see what that user sees.

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
import contextlib
import json
import signal
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from typing import Any

from backlot.openapi import SOURCE_PREFIXES

OPENAPI_SOURCES: tuple[str, ...] = tuple(SOURCE_PREFIXES)
GRAPHQL_SOURCES: tuple[str, ...] = ("linear", "fireflies")
SOURCES: tuple[str, ...] = OPENAPI_SOURCES + GRAPHQL_SOURCES

# Where `backlot serve` listens by default, and so the one place worth probing before starting a
# server of our own.
DEFAULT_URL = "http://127.0.0.1:8000"

# The one per-source knob, measured (figures in examples/using-mcp-with-agents/README.md). Linear
# runs at 1: `Team`, `Project` and `Cycle` each carry dozens of configuration leaves that a second
# level would multiply across every issue. Fireflies needs the derivation's default of 2, because
# `Analytics` has no leaf fields of its own and a shallower selection drops the node whole. Sources
# absent here take that default.
DEPTH: dict[str, int] = {"linear": 1}


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _auth_header(token: str, username: str | None = None) -> dict[str, str]:
    # Basic `username:token` is Atlassian's scheme, where the api_token IS the Backlot token; Backlot
    # resolves it to a user and ignores the username once it has. Everything else is Bearer.
    if username:
        raw = f"{username}:{token}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    return {"Authorization": f"Bearer {token}"}


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


def is_backlot(url: str) -> bool:
    """Whether a *Backlot* server answers at ``url`` — not merely something with a ``/health``.

    The default port is shared with every other local dev server, and a 200 from one of those would
    send every tool call into 404s. Backlot's ``/health`` body always carries ``documents``
    (``null`` while the caches warm), which is enough to tell it apart."""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=2) as r:
            return r.status == 200 and "documents" in json.load(r)
    except Exception:  # noqa: BLE001 — refused, timed out, not JSON: all mean "not here"
        return False


# --------------------------------------------------------------------------- one source


def openapi_server(base_url: str, token: str, source: str, *, username: str | None = None):
    """A FastMCP server for one REST source: the MCP-ready spec, served over an authenticated
    client whose base URL is Backlot. ``username`` switches Atlassian to Basic auth."""
    import httpx2
    from fastmcp import FastMCP

    spec = _fetch_json(f"{base_url}/_meta/openapi/{source}")
    # httpx2, not httpx: `from_openapi` takes an httpx2 client. A legacy httpx one is still
    # accepted, under a deprecation warning that says it will stop being accepted.
    client = httpx2.AsyncClient(
        base_url=base_url,
        headers=_auth_header(token, username if source == "atlassian" else None),
        timeout=30,
    )
    # validate_output=False: Backlot's responses are the source of truth; a passthrough must never
    # reject a real server response for not matching a loose schema.
    return FastMCP.from_openapi(
        openapi_spec=spec, client=client, name=f"{source}-bridge", validate_output=False
    )


def graphql_server(base_url: str, token: str, source: str, *, depth: int | None = None):
    """A FastMCP server for one GraphQL source: one tool per root ``Query`` field, derived from the
    endpoint's own introspection, each posting a fixed document with the caller's arguments as
    variables. ``depth`` is how many object levels the generated selection sets reach."""
    import httpx2
    from fastmcp import FastMCP
    from fastmcp.tools import Tool, ToolResult

    from backlot.graphql import mcp_tools

    endpoint = f"{base_url}/{source}/graphql"
    headers = _auth_header(token)
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
            body = r.json()
            # The GraphQL envelope goes through untouched, on the same principle as
            # `validate_output=False` above. What is added is the MCP error flag, so a caller can
            # tell a refusal from an answer — raised only when there is no `data` at all (a
            # rejected document, or a non-null field the ACL hid), not for a field error that came
            # back beside partial data.
            failed = bool(body.get("errors")) and body.get("data") is None
            return ToolResult(content=json.dumps(body), is_error=failed)

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
    token: str,
    sources: Sequence[str],
    *,
    username: str | None = None,
    depth: int | None = None,
):
    """One FastMCP server over ``sources``.

    A single source is served as itself, so its tools keep the names the vendor's spec gives them
    (``search_messages``, ``get_issue``). Several are mounted under the source's own name as a
    namespace — ``slack_search_messages``, ``github_get_issue`` — because two vendors can and do
    name an operation the same way."""
    from fastmcp import FastMCP

    servers = {
        source: (
            graphql_server(base_url, token, source, depth=depth)
            if source in GRAPHQL_SOURCES
            else openapi_server(base_url, token, source, username=username)
        )
        for source in sources
    }
    if len(servers) == 1:
        return next(iter(servers.values()))
    root = FastMCP(
        name="backlot",
        instructions="Backlot serves these enterprise SaaS APIs over a local corpus. Tools are "
        "namespaced by source: slack_*, github_*, gmail_*, gdrive_*, notion_*, atlassian_* "
        "(Jira and Confluence), hubspot_*, linear_*, fireflies_*.",
    )
    for source, server in servers.items():
        root.mount(server, namespace=source)
    return root


# --------------------------------------------------------------------------- which server


@contextlib.contextmanager
def attach(url: str | None = None, token: str | None = None) -> Iterator[tuple[str, str]]:
    """Yield ``(base_url, token)`` for the server to bridge: the one at ``url``, else the one on the
    default local port, else one started here for as long as the block runs.

    An explicit ``url`` that does not answer is an error rather than a fallback — the caller named
    a server, and quietly serving a different corpus in its place is the wrong kind of helpful.
    The token, when not given, is the server's real admin token where that can be learned
    (``backlot.server.admin_token_for``) and the package default otherwise.
    """
    from backlot import server as backlot_server

    url = (url or "").strip().rstrip("/")
    if url:
        backlot_server.ensure_cert_bundle()
        if not is_backlot(url):
            sys.exit(f"no Backlot server answers at {url}")
        yield url, token or backlot_server.admin_token_for(url)
        return
    if is_backlot(DEFAULT_URL):
        _stderr(f"using the Backlot server at {DEFAULT_URL}")
        yield DEFAULT_URL, token or backlot_server.admin_token_for(DEFAULT_URL)
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
    with started as s:
        _stderr(f"Backlot is up at {s.base_url}")
        yield s.base_url, token or s.token


def _exit_on_signal(signum: int, frame: object) -> None:
    # SIGTERM's default disposition ends the process without unwinding, which would leave the server
    # `attach` started running with no client. Raising instead runs the context managers' cleanup,
    # the same path a closed stdin takes.
    raise SystemExit(128 + signum)


def run(
    sources: Sequence[str] | None = None,
    *,
    url: str | None = None,
    token: str | None = None,
    username: str | None = None,
    depth: int | None = None,
) -> None:
    """Serve ``sources`` (default: all of them) as MCP tools over stdio until the client hangs up."""
    try:
        import fastmcp  # noqa: F401 — the check is the import
    except ImportError:
        sys.exit('backlot mcp needs the [mcp] extra:  pip install "backlot[mcp]"')

    signal.signal(signal.SIGTERM, _exit_on_signal)
    try:
        with attach(url, token) as (base_url, resolved_token):
            server = build_server(
                base_url, resolved_token, sources or SOURCES, username=username, depth=depth
            )
            # No banner: fastmcp prints one to stderr, which is harmless to the protocol but noise
            # in every MCP client's log.
            server.run(transport="stdio", show_banner=False)
    except RuntimeError as exc:
        # `serve` raises this when the server it started died before answering — a corrupt corpus
        # in the data dir is the common cause — carrying that server's own last words. They are
        # the diagnosis; a traceback through contextlib would bury them.
        sys.exit(str(exc))
