#!/usr/bin/env python3
"""Drive Backlot's Gmail API as MCP tools via the generic OpenAPI→MCP bridge. Self-contained.

Official and community Gmail MCP servers hard-wire `googleapis.com` and require real Google OAuth,
so none can be pointed at a self-hosted server. Instead `backlot mcp --source gmail` turns Backlot's
typed `/openapi.json` into MCP tools: it slices to `/gmail`, dedupes operation aliases, and serves
them over stdio with a `Bearer <token>` header — retrieval is ACL-scoped by `--user` (default:
the admin; any email from GET /_meta/users).

Prereqs: `pip install -e ".[mcp]"` (installs fastmcp); an LLM key for --agent
(`ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` with `--agent openai`). Run from the repo root:
    ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/gmail.py [--url … --user … --agent openai]
"""

from __future__ import annotations

import argparse
import sys

from mcp import StdioServerParameters

from _agent import run_agent
from backlot import serve_or_connect

CORPUS = [
    {
        "author_email": "bob@acme.com",
        "created": "2026-02-08T20:30:00Z",
        "source_type": "gmail",
        "mailbox": "ops@acme.test",
        "title": "Checkout latency incident",
        "content": "p95 checkout latency 2.1s after the payments migration; rolling back.",
    },
    {
        "author_email": "ava@acme.com",
        "created": "2025-09-10T11:00:00Z",
        "source_type": "gmail",
        "mailbox": "ops@acme.test",
        "title": "On-call runbook",
        "content": "latency spike after a deploy → check dashboards, roll back, page on-call.",
    },
]
QUESTION = (
    "Search Gmail for the checkout latency incident and summarize it, then find the on-call "
    "runbook email. Cite the subjects."
)


def build_params(base_url: str, user: str | None = None) -> StdioServerParameters:
    """Run `backlot mcp --source gmail` as a stdio MCP server pointed at Backlot.

    `-m backlot` through this interpreter rather than the `backlot` script, so it works in an
    environment whose bin/ is not on PATH. Without `--user` the command answers as the admin, so
    there is nothing to pass for the default."""
    args = ["-m", "backlot", "mcp", "--source", "gmail", "--url", base_url]
    if user:
        args += ["--user", user]
    return StdioServerParameters(command=sys.executable, args=args)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive Backlot's Gmail API over MCP via the OpenAPI bridge."
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
