#!/usr/bin/env python3
"""Drive the mock's Google Drive API as MCP tools via the generic OpenAPI→MCP bridge. Self-contained.

Official and community Drive MCP servers hard-wire `googleapis.com` and require real Google OAuth,
so none can be pointed at a self-hosted mock. Instead `_openapi_bridge.py` turns the mock's typed
`/openapi.json` into MCP tools: it slices to `/drive`, dedupes operation aliases, and serves them
over stdio with a `Bearer <token>` header — retrieval is ACL-scoped by the token (default admin;
per-user from GET /_mock/users).

Prereqs: `pip install -e ".[mcp]"` (installs fastmcp); an LLM key for --agent
(`ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` with `--agent openai`). Run from the repo root:
    ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/gdrive.py [--url … --token … --agent openai]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mcp import StdioServerParameters

from _agent import run_agent
from backlot import serve_or_connect

CORPUS = [
    {
        "source_type": "google_drive",
        "folder": "Incidents",
        "subtype": "document",
        "title": "Checkout latency postmortem",
        "content": "p95 checkout latency 2.1s after the payments migration; rolled back.",
    },
    {
        "source_type": "google_drive",
        "folder": "Runbooks",
        "subtype": "document",
        "title": "On-call runbook",
        "content": "latency spike after a deploy → check dashboards, roll back, page on-call.",
    },
]
QUESTION = (
    "Search Drive for the checkout latency postmortem and summarize it, then find the "
    "on-call runbook doc. Cite the titles."
)

_BRIDGE = str(Path(__file__).with_name("_openapi_bridge.py"))


def build_params(base_url: str, token: str) -> StdioServerParameters:
    """Run `_openapi_bridge.py --source gdrive` as a stdio MCP server pointed at the mock."""
    return StdioServerParameters(
        command=sys.executable,
        args=[_BRIDGE, "--source", "gdrive", "--base-url", base_url.rstrip("/"), "--token", token],
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive the mock's Google Drive API over MCP via the OpenAPI bridge."
    )
    p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
    p.add_argument(
        "--token",
        help="mock bearer token from GET /_mock/users "
        "(default: the admin token, which sees everything)",
    )
    p.add_argument(
        "--agent",
        choices=("anthropic", "openai"),
        default="anthropic",
        help="which LLM agent to run (default: anthropic)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        if args.token:
            print("authenticating with --token → retrieval is ACL-filtered to that user")
        params = build_params(mock.base_url, args.token or mock.token)
        run_agent(args.agent, params, QUESTION)
