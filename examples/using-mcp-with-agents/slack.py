#!/usr/bin/env python3
"""Drive Backlot's Slack Web API as MCP tools via the generic OpenAPI→MCP bridge. Self-contained.

No maintained Slack MCP server accepts a base-URL override (they hard-wire slack.com), so instead
`backlot mcp --source slack` turns Backlot's typed `/openapi.json` into MCP tools: it slices to
`/slack/api`, dedupes the GET/POST operation aliases, and serves them over stdio with a
`Bearer <token>` header — so retrieval is ACL-scoped by `--user` (default: the admin; any email
from GET /_meta/users).

The bridge serves the WRITE methods too, so the agent is given a task it has to act to finish
rather than a question it can only answer: it finds the incident, finds the runbook, replies in
the thread and reacts — retrieval included, since it cannot write the reply without reading both
messages first. What it did is then read back from `/_meta/overlay`, the record an evaluation
grades and the reason the writes land in an overlay instead of the corpus (see docs/write-overlay.md).

Prereqs: `pip install -e ".[mcp]"` (installs fastmcp); an LLM key for --agent
(`ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` with `--agent openai`). Run from the repo root:
    ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/slack.py [--url … --user … --agent openai]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from _agent import run_agent
from mcp import StdioServerParameters

from backlot import serve_or_connect

CORPUS = [
    {
        "author_email": "ava@acme.com",
        "created": "2026-02-05T17:00:00Z",
        "source_type": "slack",
        "channel": "incidents",
        "content": "checkout p95 latency hit 2.1s after the payments migration; rolling back now.",
    },
    {
        "author_email": "bob@acme.com",
        "created": "2026-02-10T18:00:00Z",
        "source_type": "slack",
        "channel": "runbooks",
        "content": "on-call runbook — latency spike after a deploy: check dashboards, roll back, page on-call.",
    },
]
TASK = (
    "Search Slack for the checkout latency incident. Reply in that message's thread with a "
    "one-line summary and the next step from the on-call runbook, and add an :eyes: reaction to "
    "the incident message itself. Cite the channels you read."
)


def build_params(base_url: str, user: str | None = None) -> StdioServerParameters:
    """Run `backlot mcp --source slack` as a stdio MCP server pointed at Backlot.

    `-m backlot` through this interpreter rather than the `backlot` script, so it works in an
    environment whose bin/ is not on PATH. Without `--user` the command answers as the admin, so
    there is nothing to pass for the default."""
    args = ["-m", "backlot", "mcp", "--source", "slack", "--url", base_url]
    if user:
        args += ["--user", user]
    return StdioServerParameters(command=sys.executable, args=args)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive Backlot's Slack API over MCP via the OpenAPI bridge."
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


def report_writes(base_url: str) -> None:
    """What the agent actually did, read back the way an evaluation's grader reads it.

    `/_meta/overlay` holds exactly the writes since the last reset, so grading needs no snapshot
    of the surface from before the run -- the overlay already is the difference.
    """
    with urllib.request.urlopen(f"{base_url}/_meta/overlay") as response:
        overlay = json.load(response)
    print("\n--- what the agent wrote, from /_meta/overlay ---")
    wrote = False
    for message in overlay.get("slack_messages") or ():
        wrote = True
        print(f"  message in #{message['channel']} at {message['ts']}: {message['content']!r}")
    for patch in overlay.get("slack_patch") or ():
        wrote = True
        print(f"  {patch['field']} on #{patch['channel']} {patch['ts']}: {patch['value']!r}")
    for stone in overlay.get("slack_tombstone") or ():
        wrote = True
        print(f"  deleted #{stone['channel']} {stone['ts']}")
    if not wrote:
        print("  nothing — the agent answered without acting")


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as s:
        if args.user:
            print(f"answering as {args.user} → retrieval is ACL-filtered to that person")
        params = build_params(s.base_url, args.user)
        run_agent(args.agent, params, TASK)
        report_writes(s.base_url)
