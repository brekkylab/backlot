#!/usr/bin/env python3
"""Generic OpenAPI→MCP bridge — run as a stdio subprocess by the per-source launchers.

Fetches Backlot's **MCP-ready** spec for one source (``GET /_meta/openapi/<source>`` — Backlot
slices its own ``/openapi.json`` to that source and collapses the GET/POST and v2/v3 fidelity
aliases server-side, so there's nothing to clean up here) and serves those operations as MCP tools
via ``FastMCP.from_openapi()`` over an ``httpx.AsyncClient`` whose ``Authorization`` header is the
caller's credential — so Backlot's per-token ACL is enforced on every tool call.

Auth: ``--username`` present → HTTP Basic (``username:token``, used by Atlassian); otherwise Bearer.
stdio only: FastMCP's streamable-HTTP mode has a known Authorization-forwarding bug.

    python _openapi_bridge.py --source github --base-url http://127.0.0.1:8000 --token <token>
    python _openapi_bridge.py --source atlassian --base-url https://host --token <t> --username svc@example.com
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.request

# The bridge runs as an MCP **stdio subprocess**, and that transport passes only a whitelisted
# environment — so an `SSL_CERT_FILE` exported by the caller does not reach us. On macOS Python has
# no CA bundle of its own (`ssl.get_default_verify_paths().cafile` is None), so fetching the spec
# from an HTTPS deployment would fail with CERTIFICATE_VERIFY_FAILED and the subprocess would exit,
# surfacing to the client as an opaque "Connection closed". certifi ships with the [mcp] extra.
try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass


def _auth_header(token: str, username: str | None) -> dict[str, str]:
    if username:  # Atlassian resolves Basic email:api_token, where the api_token is Backlot token
        raw = f"{username}:{token}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    return {"Authorization": f"Bearer {token}"}


def _fetch_mcp_spec(base_url: str, source: str) -> dict:
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/_meta/openapi/{source}", timeout=10) as r:
        return json.load(r)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generic OpenAPI→MCP bridge (stdio).")
    p.add_argument("--source", required=True)
    p.add_argument("--base-url", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--username", default=None, help="basic-auth username (Atlassian)")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    spec = _fetch_mcp_spec(args.base_url, args.source)

    import httpx
    from fastmcp import FastMCP

    client = httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=_auth_header(args.token, args.username),
        timeout=30,
    )
    # validate_output=False: Backlot's responses are the source of truth; a passthrough bridge
    # must never reject a real server response for not matching a loose schema.
    mcp = FastMCP.from_openapi(
        openapi_spec=spec, client=client, name=f"{args.source}-bridge", validate_output=False
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
