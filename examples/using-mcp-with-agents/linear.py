#!/usr/bin/env python3
"""Drive the mock's Linear GraphQL API as MCP tools via the GraphQL→MCP bridge. Self-contained.

**Linear's official MCP server is remote-only.** It is vendor-hosted at
`https://mcp.linear.app/mcp` with no base-URL override, so there is nothing local to point at a
mock — the official route is closed here, not merely inconvenient. The community servers do not
rescue it either: `tacticlaunch/mcp-linear` (the current one) and `jerhadf/linear-mcp-server` (the
most-starred, untouched since 2025) both hard-wire `https://api.linear.app` in source and document
only a token, and because they run as `npx` subprocesses the in-process URL rewrite backlot uses
for the LlamaIndex Linear reader cannot reach them.

So the tools come from the mock's own schema instead: `_graphql_bridge.py` introspects
`POST /linear/graphql` and serves each root `Query` field as a typed tool (`issues`, `issue`,
`teams`, `comments`, `viewer`, the by-id relation roots …). That trade is worth stating plainly —
this exercises *our* tool surface rather than the community tooling an agent meets in production,
which is why `atlassian` / `notion` / `s3` use vendor servers where one can be redirected at all.

**Depth 1, deliberately.** The bridge generates each tool's selection set (rules in
`backlot/graphql/mcp_tools.py`) and Linear is the schema where the default of 2 costs too much:
`Team`, `Project` and `Cycle` each carry dozens of configuration leaves, so a second level takes
an issue from 507 selected fields to 1,313 — for data no agent asks about. Depth 1 still returns
`state`, `assignee`, `team`, `project` and `labels` inline. Pass `--depth 2` to see the difference.

Prereqs: `pip install -e ".[mcp]"` (installs fastmcp); an LLM key for --agent
(`ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` with `--agent openai`). Run from the repo root:
    ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/linear.py [--url … --token … --agent openai]
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
        "source_type": "linear",
        "doc_id": "lin-checkout",
        "team": "engineering",
        "group": "engineering",
        "title": "Checkout p95 latency regression after the payments migration",
        "content": "p95 hit 2.1s once the payments migration shipped. Rolling back while we trace it.",
        "author_email": "amaya.chen@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "identifier": "ENG-4912",
        "state": "In Progress",
        "priority": "P0",
        "estimate": 5,
        "labels": ["latency", "payments"],
        "project": "checkout-reliability",
        "assignee": "diego.martinez@acme.com",
        "assigneeName": "Diego Martinez",
        "comments": [
            {
                "content": "Traces point at the new settlement call being serial, not batched.",
                "author_email": "diego.martinez@acme.com",
            },
        ],
    },
    {
        "source_type": "linear",
        "doc_id": "lin-alert",
        "team": "engineering",
        "group": "engineering",
        "title": "Alert when checkout p95 crosses 800ms",
        "content": "No alert fired during the latency regression. Add one at 800ms for 5 minutes.",
        "author_email": "diego.martinez@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "identifier": "ENG-4930",
        "state": "Todo",
        "priority": "P2",
        "labels": ["observability"],
    },
]
QUESTION = (
    "Find the Linear issue about the checkout latency regression. What state is it in, who is "
    "assigned, and what did the comments say the cause was? Cite the issue identifiers."
)

_BRIDGE = str(Path(__file__).with_name("_graphql_bridge.py"))


def build_params(base_url: str, token: str, depth: int = 1) -> StdioServerParameters:
    """Run `_graphql_bridge.py --source linear` as a stdio MCP server pointed at the mock."""
    return StdioServerParameters(
        command=sys.executable,
        args=[
            _BRIDGE,
            "--source",
            "linear",
            "--base-url",
            base_url.rstrip("/"),
            "--token",
            token,
            "--depth",
            str(depth),
        ],
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive the mock's Linear GraphQL API over MCP via the GraphQL bridge."
    )
    p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
    p.add_argument(
        "--token",
        help="mock token from GET /_mock/users "
        "(default: the admin token, which sees everything). Linear accepts it bare or as Bearer",
    )
    p.add_argument(
        "--depth",
        type=int,
        default=1,
        help="object levels the generated selection sets reach (default: %(default)s — see the "
        "module docstring for why Linear is shallower than the bridge default)",
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
        params = build_params(mock.base_url, args.token or mock.token, args.depth)
        run_agent(args.agent, params, QUESTION)
