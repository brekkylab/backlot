#!/usr/bin/env python3
"""Drive Backlot's GitHub API as MCP tools via the generic OpenAPI→MCP bridge. Self-contained.

The bridge (`backlot mcp --source github`) fetches Backlot's typed `/openapi.json`, slices it to
`/github`, and serves those operations over stdio with a `Bearer <token>` header — so retrieval
is ACL-scoped by `--user` (default: the admin; any email from GET /_meta/users). No vendor SDK and
no vendor MCP server.

Prereqs: `pip install -e ".[mcp]"` (installs fastmcp); an LLM key for --agent
(`ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` with `--agent openai`). Run from the repo root:
    ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/github.py [--url … --user … --agent openai]
"""

from __future__ import annotations

import argparse
import sys

from mcp import StdioServerParameters

from _agent import run_agent
from backlot import serve_or_connect

CORPUS = [
    {
        "state": "open",
        "author_email": "bob@acme.com",
        "created": "2026-02-08T21:00:00Z",
        "source_type": "github",
        "repo": "payments",
        "subtype": "issue",
        "title": "Checkout latency spike after migration",
        "content": "p95 checkout latency jumped to 2.1s after the payments DB migration; rolling back.",
    },
    {
        "state": "open",
        "author_email": "ava@acme.com",
        "created": "2025-09-10T11:00:00Z",
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


def build_params(base_url: str, user: str | None = None) -> StdioServerParameters:
    """Run `backlot mcp --source github` as a stdio MCP server pointed at Backlot.

    `-m backlot` through this interpreter rather than the `backlot` script, so it works in an
    environment whose bin/ is not on PATH. Without `--user` the command answers as the admin, so
    there is nothing to pass for the default."""
    args = ["-m", "backlot", "mcp", "--source", "github", "--url", base_url]
    if user:
        args += ["--user", user]
    return StdioServerParameters(command=sys.executable, args=args)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive Backlot's GitHub API over MCP via the OpenAPI bridge."
    )
    p.add_argument(
        "--url", help="Backlot base URL to drive (default: spin up a local throwaway server)"
    )
    p.add_argument(
        "--user",
        metavar="EMAIL",
        help="answer as this person, from GET /_meta/users "
        "(default: the admin, who sees everything)",
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
    with serve_or_connect(CORPUS, url=args.url) as s:
        if args.user:
            print(f"answering as {args.user} → retrieval is ACL-filtered to that person")
        params = build_params(s.base_url, args.user)
        run_agent(args.agent, params, QUESTION)
