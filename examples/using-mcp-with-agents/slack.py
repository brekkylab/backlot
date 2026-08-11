#!/usr/bin/env python3
"""Drive the mock's Slack Web API as MCP tools via the generic OpenAPI→MCP bridge. Self-contained.

No maintained Slack MCP server accepts a base-URL override (they hard-wire slack.com), so instead
`_openapi_bridge.py` turns the mock's typed `/openapi.json` into MCP tools: it slices to `/slack/api`,
dedupes the GET/POST operation aliases, and serves them over stdio with a `Bearer <token>` header —
so retrieval is ACL-scoped by the token (default admin; per-user from GET /_mock/users).

Prereqs: `pip install -e ".[mcp]"` (installs fastmcp); an LLM key for --agent
(`ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` with `--agent openai`). Run from the repo root:
    ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/slack.py [--url … --token … --agent openai]
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
        "source_type": "slack",
        "channel": "incidents",
        "content": "checkout p95 latency hit 2.1s after the payments migration; rolling back now.",
    },
    {
        "source_type": "slack",
        "channel": "runbooks",
        "content": "on-call: latency spike after a deploy → check dashboards, roll back, page on-call.",
    },
]
QUESTION = (
    "Search Slack for the checkout latency incident and summarize it, then find the on-call "
    "runbook message. Cite the channels."
)

_BRIDGE = str(Path(__file__).with_name("_openapi_bridge.py"))


def build_params(base_url: str, token: str) -> StdioServerParameters:
    """Run `_openapi_bridge.py --source slack` as a stdio MCP server pointed at the mock."""
    return StdioServerParameters(
        command=sys.executable,
        args=[_BRIDGE, "--source", "slack", "--base-url", base_url.rstrip("/"), "--token", token],
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive the mock's Slack API over MCP via the OpenAPI bridge."
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
