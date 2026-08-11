#!/usr/bin/env python3
"""Drive mcp-atlassian (Jira + Confluence) over MCP, pointed at the mock. Self-contained.

Runs the community-official `mcp-atlassian` server in **Docker** against a `--url` mock (or a local
one it spins up), then lets an LLM agent answer a question by calling its MCP tools.

mcp-atlassian only classifies a host as Atlassian *Cloud* (the v3 + `/wiki` shape the mock speaks)
when the hostname ends in `.atlassian.net`, so we always use a fake `mock.atlassian.net` mapped
with Docker's `--add-host` — to the host machine (`host-gateway`) for a local mock, or to a remote
deployment's resolved IP. Auth is HTTP Basic where the **api_token is a mock token** (`--token`,
default admin; per-user from GET /_mock/users); the **username** is required by mcp-atlassian but
ignored by the mock once the token resolves.

Prereqs: Docker; `pip install -e ".[mcp]"`; an LLM key for `--agent` (`ANTHROPIC_API_KEY`, or
`OPENAI_API_KEY` with `--agent openai`). Run from the repo root:
    ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/atlassian.py [--url … --token … --username … --agent openai]
"""

from __future__ import annotations

import argparse
import socket
import sys
from urllib.parse import urlparse

from mcp import StdioServerParameters

from _agent import run_agent
from backlot import serve_or_connect

CORPUS = [
    {
        "source_type": "jira",
        "project": "payments",
        "title": "SEV2: checkout latency spike",
        "content": "p95 checkout latency jumped to 2.1s after the payments migration; rolling back.",
        "status": "In Progress",
        "issuetype": "Incident",
        "priority": "High",
    },
    {
        "source_type": "confluence",
        "space": "runbooks",
        "title": "On-call Runbook: checkout latency & bad deploys",
        "content": "When a deploy or migration spikes checkout latency: check the payments "
        "dashboards, roll back the last change, and page the on-call engineer.",
    },
]
QUESTION = (
    "Find the incident about checkout latency and summarize it, then find the on-call "
    "runbook. Cite the titles."
)

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0")


def build_params(base_url: str, token: str, username: str | None) -> StdioServerParameters:
    """`docker run` args pointing mcp-atlassian at the mock (Cloud shape via mock.atlassian.net)."""
    u = urlparse(base_url)
    host = "mock.atlassian.net"  # must end in .atlassian.net for Cloud detection
    if (u.hostname or "127.0.0.1") in _LOCAL_HOSTS:
        scheme, port, addhost, ssl_verify = "http", (u.port or 80), "host-gateway", True
        user = username or "svc@example.com"  # placeholder; the mock ignores it once token resolves
    else:
        # remote deployment: alias the fake host to its IP, and require an explicit identity
        if not username:
            sys.exit(
                f"--url points at a remote deployment ({u.hostname}); also pass --username "
                "(and --token) — mcp-atlassian needs a Basic-auth username for Cloud detection "
                "and the token authenticates + scopes ACL (get one from GET /_mock/users)."
            )
        scheme = u.scheme
        port = u.port or (443 if u.scheme == "https" else 80)
        addhost = socket.gethostbyname(u.hostname)
        ssl_verify = False  # cert is for the real host, not mock.atlassian.net
        user = username
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    base = f"{scheme}://{host}" if default_port else f"{scheme}://{host}:{port}"
    args = [
        "run",
        "-i",
        "--rm",
        f"--add-host={host}:{addhost}",
        "-e",
        f"JIRA_URL={base}/atlassian",
        "-e",
        f"JIRA_USERNAME={user}",
        "-e",
        f"JIRA_API_TOKEN={token}",
        "-e",
        f"CONFLUENCE_URL={base}/atlassian/wiki",
        "-e",
        f"CONFLUENCE_USERNAME={user}",
        "-e",
        f"CONFLUENCE_API_TOKEN={token}",
        "-e",
        "MCP_ALLOWED_URL_DOMAINS=atlassian.net",
        "-e",
        "READ_ONLY_MODE=true",
    ]
    if not ssl_verify:
        args += ["-e", "JIRA_SSL_VERIFY=false", "-e", "CONFLUENCE_SSL_VERIFY=false"]
    args += ["ghcr.io/sooperset/mcp-atlassian:latest", "--transport", "stdio"]
    return StdioServerParameters(command="docker", args=args)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Drive mcp-atlassian over MCP against the mock.")
    p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
    p.add_argument(
        "--token",
        help="mock bearer token from GET /_mock/users "
        "(default: the admin token, which sees everything)",
    )
    p.add_argument("--username", help="Atlassian Basic-auth username (required for a remote --url)")
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
        params = build_params(mock.base_url, args.token or mock.token, args.username)
        run_agent(args.agent, params, QUESTION)
