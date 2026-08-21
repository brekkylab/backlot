#!/usr/bin/env python3
"""Generic GraphQL→MCP bridge — run as a stdio subprocess by the per-source launchers.

Introspects one source's GraphQL endpoint (``POST /<source>/graphql``) and serves each root
``Query`` field as its own MCP tool. ``backlot.graphql.mcp_tools`` owns the derivation — the tool
names, the argument JSON Schemas, and the generated selection sets, with every rule stated in that
module's docstring. Everything here is transport: post ``tool.document`` with the caller's
credential, so the mock's per-token ACL decides what comes back on every call.

This is the GraphQL counterpart to ``_openapi_bridge.py``, and it exists because the OpenAPI one
cannot serve these two sources at all: ``/linear/graphql`` and ``/fireflies/graphql`` are
``include_in_schema=False``, so ``/openapi.json`` describes nothing to slice — and describing one
POST route that accepts an arbitrary document would derive a single raw-document tool rather than a
usable toolset. Introspection is the schema description for a GraphQL endpoint, so that is what
this reads.

Auth is ``Authorization: Bearer <token>`` for both sources. Fireflies is the ordinary bearer path;
Linear accepts a bare API key *or* a ``Bearer`` token on the same header (``backlot.auth
.api_key_token``), exactly as the real API does, so one spelling covers both.

stdio only, matching ``_openapi_bridge.py``: FastMCP's streamable-HTTP mode has a known
Authorization-forwarding bug.

    python _graphql_bridge.py --source linear --base-url http://127.0.0.1:8000 --token <t> --depth 1
    python _graphql_bridge.py --source fireflies --base-url https://host --token <mock-token>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from backlot.graphql import mcp_tools

# The bridge runs as an MCP **stdio subprocess**, and that transport passes only a whitelisted
# environment — so an `SSL_CERT_FILE` exported by the caller does not reach us. On macOS Python has
# no CA bundle of its own, so introspecting an HTTPS deployment would fail with
# CERTIFICATE_VERIFY_FAILED and the subprocess would exit, surfacing to the client as an opaque
# "Connection closed". certifi ships with the [mcp] extra.
try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass


def _introspect(endpoint: str, token: str) -> dict:
    """Ask the endpoint what it serves, the way any GraphQL client would.

    This request carries the credential, so a bad ``--token`` fails here, before a single tool
    exists. Left to propagate it kills the subprocess and reaches the MCP client as an opaque
    "Connection closed", so exit with the endpoint's own error body instead.
    """
    request = urllib.request.Request(
        endpoint,
        data=json.dumps({"query": mcp_tools.INTROSPECTION_QUERY}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace").strip()
        sys.exit(f"introspecting {endpoint} failed with HTTP {exc.code}: {body[:500]}")
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach {endpoint}: {exc.reason}")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generic GraphQL→MCP bridge (stdio).")
    p.add_argument("--source", required=True)
    p.add_argument("--base-url", required=True)
    p.add_argument("--token", required=True)
    p.add_argument(
        "--depth",
        type=int,
        default=mcp_tools.DEFAULT_DEPTH,
        help="how many object levels the generated selection sets reach (default: %(default)s)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    endpoint = f"{args.base_url.rstrip('/')}/{args.source}/graphql"
    try:
        tools = mcp_tools.derive_tools(_introspect(endpoint, args.token), depth=args.depth)
    except ValueError as exc:  # a 200 carrying an error envelope rather than a schema
        sys.exit(str(exc))

    import httpx
    from fastmcp import FastMCP
    from fastmcp.tools import Tool
    from fastmcp.tools.tool import ToolResult

    client = httpx.AsyncClient(headers={"Authorization": f"Bearer {args.token}"}, timeout=30)

    class _Field(Tool):
        """One root field. The document is fixed; the caller's arguments are the variables."""

        spec: Any

        async def run(self, arguments: dict) -> ToolResult:
            # `with_page_default` bounds an unpaged list call, and leaves a caller that paged for
            # itself alone; the rule and its reason are in `mcp_tools`.
            variables = mcp_tools.with_page_default(self.spec, arguments)
            r = await client.post(
                endpoint, json={"query": self.spec.document, "variables": variables}
            )
            body = r.json()
            # The GraphQL envelope goes through untouched, on the same principle as
            # `_openapi_bridge.py`'s `validate_output=False`: the mock's response is the source of
            # truth and a passthrough must not reshape it. What the bridge does add is the MCP
            # error flag, so a caller can tell a refusal from an answer — raised only when there
            # is no `data` at all (a rejected document, or a non-null field the ACL hid), not for
            # a field error that came back beside partial data.
            failed = bool(body.get("errors")) and body.get("data") is None
            return ToolResult(content=json.dumps(body), is_error=failed)

    mcp = FastMCP(name=f"{args.source}-graphql-bridge")
    for tool in tools:
        mcp.add_tool(
            _Field(
                name=tool.name,
                description=tool.description,
                parameters=tool.input_schema,
                spec=tool,
            )
        )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
