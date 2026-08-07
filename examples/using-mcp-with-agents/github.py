#!/usr/bin/env python3
"""Drive the mock's GitHub API as MCP tools via the generic OpenAPI→MCP bridge. Self-contained.

The bridge (`_openapi_bridge.py`) fetches the mock's typed `/openapi.json`, slices it to `/github`, and
serves those operations over stdio with a `Bearer <token>` header — so retrieval is ACL-scoped by
the token (default admin; per-user from GET /_mock/users). No vendor SDK and no vendor MCP server.

Prereqs: `pip install -e ".[mcp]"` (installs fastmcp); an LLM key for --agent
(`ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` with `--agent openai`). Run from the repo root:
    ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/github.py [--url … --token … --agent openai]
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
        "source_type": "github",
        "repo": "payments",
        "subtype": "issue",
        "title": "Checkout latency spike after migration",
        "content": "p95 checkout latency jumped to 2.1s after the payments DB migration; rolling back.",
    },
    {
        "source_type": "github",
        "repo": "runbooks",
        "subtype": "issue",
        "title": "Runbook: latency spikes & bad deploys",
        "content": "When a deploy or migration spikes checkout latency: check dashboards, roll back, "
        "page on-call.",
    },
]
QUESTION = (
    "Search GitHub issues for the checkout latency incident and summarize it, then find the "
    "runbook issue. Cite the titles."
)

_BRIDGE = str(Path(__file__).with_name("_openapi_bridge.py"))


def build_params(base_url: str, token: str) -> StdioServerParameters:
    """Run `_openapi_bridge.py --source github` as a stdio MCP server pointed at the mock."""
    return StdioServerParameters(
        command=sys.executable,
        args=[_BRIDGE, "--source", "github", "--base-url", base_url.rstrip("/"), "--token", token],
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive the mock's GitHub API over MCP via the OpenAPI bridge."
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
