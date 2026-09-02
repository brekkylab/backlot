#!/usr/bin/env python3
"""Drive Backlot's Fireflies GraphQL API as MCP tools via the GraphQL→MCP bridge. Self-contained.

**Fireflies' official MCP server is remote-only** — vendor-hosted, no base-URL override — so, as
with Linear, nothing local can stand in for it. The community side is thinner here than anywhere
else in this directory: barely-adopted servers, and the maintained one pins its vendor endpoint as a
module constant with the API key its sole configurable. There is no consensus server to be faithful
to and none that can be redirected, so carrying a patched fork of one would buy nothing.

The tools therefore come from Backlot's own schema: `backlot mcp --source fireflies` introspects
`POST /fireflies/graphql` and serves its four root fields as typed tools — `transcripts` (with
Fireflies' own `keyword` / `scope` / `fromDate` / `host_email` / `limit` / `skip` arguments),
`transcript`, `user`, `users`. Stated plainly, since it is the reason this file exists: that
exercises *our* tool surface rather than production tooling.

The bridge's default depth of 2 is what this schema needs. `Analytics` has no leaf fields of its
own, so a shallower selection would drop the node entirely and with it the sentiment split the
corpus computes — see `backlot/graphql/mcp_tools.py` for the rules.

Note the division of labour between the two transcript tools, which the size rule there creates:
`transcripts` returns the metadata and the summary of each match, and `transcript(id:)` returns one
meeting's utterances. So the agent searches, then reads the transcript it picked — the same two-step
`examples/using-official-sdk/fireflies.py` performs by hand.

Prereqs: `pip install -e ".[mcp]"` (installs fastmcp); an LLM key for --agent
(`ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` with `--agent openai`). Run from the repo root:
    ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/fireflies.py [--url … --token … --agent openai]
"""

from __future__ import annotations

import argparse
import sys

from mcp import StdioServerParameters

from _agent import run_agent
from backlot import serve_or_connect

CORPUS = [
    {
        "source_type": "fireflies",
        "channel": "sales-calls",
        "doc_id": "ff-discovery",
        "title": "Acme Health — latency discovery",
        "host_email": "rep@acme.com",
        "host_name": "Dana Rep",
        "duration": 34.0,
        "created": "2026-04-02T15:00:00Z",
        "summary": {
            "overview": "Acme Health needs sub-300ms p95 before they will pilot.",
            "topics_discussed": ["latency budget", "batching", "EU residency"],
            "action_items": [
                "Dana: send the batching benchmark",
                "Ivan: confirm EU data residency",
            ],
            "keywords": ["latency", "batching", "residency"],
            "meeting_type": "discovery",
        },
        "meeting_attendees": [
            {"displayName": "Dana Rep", "email": "rep@acme.com"},
            {"displayName": "Ivan Ortiz", "email": "ivan@acme-health.example"},
        ],
        "sentences": [
            {
                "speaker_name": "Dana Rep",
                "author_email": "rep@acme.com",
                "start_time": 0,
                "text": "Thanks for making time — let's start with the latency budget.",
            },
            {
                "speaker_name": "Ivan Ortiz",
                "start_time": 18,
                "text": "Our p95 sits around 300 milliseconds and batching is the suspect.",
            },
            {
                "speaker_name": "Dana Rep",
                "author_email": "rep@acme.com",
                "start_time": 41,
                "text": "Understood. I'll send the batching benchmark right after this.",
            },
        ],
    },
    {
        "source_type": "fireflies",
        "channel": "sales-calls",
        "doc_id": "ff-checkin",
        "title": "Acme Health — pilot check-in",
        "host_email": "rep@acme.com",
        "duration": 21.0,
        "created": "2026-04-16T15:00:00Z",
        "content": "[00:00] Dana: quick check-in on the pilot numbers.\n"
        "[00:24] Ivan: p95 is down to 240 milliseconds with batching on.\n"
        "We're comfortable moving to the security review.",
    },
]
QUESTION = (
    "Search the Fireflies transcripts for the Acme Health latency discussion. What did they need "
    "on p95, what were the action items, and what changed by the follow-up call? Cite the "
    "meeting titles."
)


def build_params(base_url: str, token: str, depth: int | None = None) -> StdioServerParameters:
    """Run `backlot mcp --source fireflies` as a stdio MCP server pointed at Backlot.

    `-m backlot` through this interpreter rather than the `backlot` script, so it works in an
    environment whose bin/ is not on PATH."""
    args = ["-m", "backlot", "mcp", "--source", "fireflies", "--url", base_url, "--token", token]
    if depth is not None:
        args += ["--depth", str(depth)]
    return StdioServerParameters(command=sys.executable, args=args)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive Backlot's Fireflies GraphQL API over MCP via the GraphQL bridge."
    )
    p.add_argument(
        "--url", help="Backlot base URL to drive (default: spin up a local throwaway server)"
    )
    p.add_argument(
        "--token",
        help="Backlot bearer token from GET /_meta/users "
        "(default: the admin token, which sees everything)",
    )
    p.add_argument(
        "--depth",
        type=int,
        default=None,
        help="object levels the generated selection sets reach (default: the bridge's own 2, "
        "which is what this schema's analytics node needs)",
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
        if args.token:
            print("authenticating with --token → retrieval is ACL-filtered to that user")
        params = build_params(s.base_url, args.token or s.token, args.depth)
        run_agent(args.agent, params, QUESTION)
