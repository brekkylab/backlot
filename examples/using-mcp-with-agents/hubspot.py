#!/usr/bin/env python3
"""Drive the mock's HubSpot CRM API as MCP tools via the generic OpenAPI→MCP bridge. Self-contained.

The bridge (`_openapi_bridge.py`) fetches the mock's typed `/openapi.json`, slices it to `/hubspot`,
and serves those operations over stdio with a `Bearer <token>` header — so retrieval is ACL-scoped by
the token (default admin; per-user from GET /_mock/users). No vendor SDK and no vendor MCP server:
HubSpot has no base-URL-switchable one, so this bridge is its MCP path.

Because the CRM API is polymorphic over `{object_type}`, the agent gets *five* tools that each work
across every object type (list, read, search, batch-read, associations) rather than a set per type —
so "find the account, then its notes" is two calls with the object type as an argument.

Prereqs: `pip install -e ".[mcp]"` (installs fastmcp); an LLM key for --agent
(`ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` with `--agent openai`). Run from the repo root:
    ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/hubspot.py [--url … --token … --agent openai]
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
        "source_type": "hubspot",
        "object_type": "companies",
        "doc_id": "hs-co-acme",
        "title": "Acme Health",
        "content": "Mid-market healthcare provider, EU data residency required.",
        "properties": {
            "name": "Acme Health",
            "domain": "acme-health.com",
            "industry": "healthcare",
            "lifecyclestage": "evaluation",
        },
    },
    {
        "source_type": "hubspot",
        "object_type": "notes",
        "title": "",
        "content": "Security review scheduled; customer blocked on confirming EU data residency.",
        "properties": {
            "hs_note_body": "Security review scheduled; customer blocked on confirming "
            "EU data residency."
        },
        "associations": [{"to": "hs-co-acme"}],
    },
    {
        "source_type": "hubspot",
        "object_type": "notes",
        "title": "",
        "content": "Pricing pushback: wants per-query rather than reserved capacity.",
        "properties": {
            "hs_note_body": "Pricing pushback: wants per-query rather than reserved capacity."
        },
        "associations": [{"to": "hs-co-acme"}],
    },
    {
        "source_type": "hubspot",
        "object_type": "deals",
        "title": "Acme Health — renewal",
        "content": "12-month renewal, blocked on the security review.",
        "properties": {
            "dealname": "Acme Health — renewal",
            "amount": "50000",
            "dealstage": "contractsent",
        },
        "associations": [{"to": "hs-co-acme"}],
    },
]
QUESTION = (
    "Find the healthcare company in the CRM, then read the notes associated with it and the "
    "deal on the account. What is blocking the renewal? Cite the company name and the note "
    "text you relied on."
)

_BRIDGE = str(Path(__file__).with_name("_openapi_bridge.py"))


def build_params(base_url: str, token: str) -> StdioServerParameters:
    """Run `_openapi_bridge.py --source hubspot` as a stdio MCP server pointed at the mock."""
    return StdioServerParameters(
        command=sys.executable,
        args=[_BRIDGE, "--source", "hubspot", "--base-url", base_url.rstrip("/"), "--token", token],
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive the mock's HubSpot CRM API over MCP via the OpenAPI bridge."
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
