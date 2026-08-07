#!/usr/bin/env python3
"""Drive the official notion-mcp-server over MCP, pointed at the mock. Self-contained.

Runs `@notionhq/notion-mcp-server` via **npx** (Node) against a `--url` mock (or a local one it
spins up), then lets an LLM agent answer a question by calling its MCP tools. The server takes a
first-class `BASE_URL` override, so pointing it at the mock is one env var and a local `localhost`
mock is reached directly (no Docker/host-gateway tricks). Auth is a mock token (`--token`, default
admin; per-user from GET /_mock/users).

Prereqs: Node/npx; `pip install -e ".[mcp]"`; an LLM key for `--agent` (`ANTHROPIC_API_KEY`, or
`OPENAI_API_KEY` with `--agent openai`). Run from the repo root:
    ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/notion.py [--url … --token … --agent openai]
"""

from __future__ import annotations

import argparse

from mcp import StdioServerParameters

from _agent import run_agent
from backlot import serve_or_connect

CORPUS = [
    {
        "source_type": "notion",
        "teamspace": "payments",
        "title": "SEV2: checkout latency spike",
        "content": "# SEV2\n\np95 checkout latency jumped to 2.1s after the payments migration; "
        "rolling back.",
    },
    {
        "source_type": "notion",
        "teamspace": "runbooks",
        "title": "On-call Runbook: checkout latency & bad deploys",
        "content": "# On-call\n\nWhen a deploy or migration spikes checkout latency: check the "
        "payments dashboards, roll back the last change, and page the on-call engineer.",
    },
]
QUESTION = (
    "Find the incident about checkout latency and summarize it, then find the on-call "
    "runbook. Cite the titles."
)


def build_params(base_url: str, token: str) -> StdioServerParameters:
    """`npx` args pointing the official notion-mcp-server at the mock via BASE_URL."""
    return StdioServerParameters(
        command="npx",
        args=["-y", "@notionhq/notion-mcp-server"],
        env={
            "BASE_URL": f"{base_url.rstrip('/')}/notion",
            "NOTION_TOKEN": token,
            "NOTION_VERSION": "2025-09-03",
        },
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Drive notion-mcp-server over MCP against the mock.")
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
