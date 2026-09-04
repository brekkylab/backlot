#!/usr/bin/env python3
"""Drive mcp-atlassian (Jira + Confluence) over MCP, pointed at Backlot. Self-contained.

Runs the community-official `mcp-atlassian` server in **Docker** against a `--url` server (or
one it spins up), then lets an LLM agent answer a question by calling its MCP tools.

mcp-atlassian only classifies a host as Atlassian *Cloud* (the v3 + `/wiki` shape Backlot speaks)
when the hostname ends in `.atlassian.net`, so we always use a fake `backlot.atlassian.net` mapped
with Docker's `--add-host` — to the host machine (`host-gateway`) for a local server, or to a remote
deployment's resolved IP. Auth is HTTP Basic where the **api_token is a Backlot token** (`--token`,
default admin; per-user from GET /_meta/users); the **username** is required by mcp-atlassian but
matched by Backlot against the token's own account, as the real service does.

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
        "reporter": "bob@acme.com",
        "author_email": "bob@acme.com",
        "created": "2026-02-08T20:00:00Z",
        "source_type": "jira",
        "project": "payments",
        "title": "SEV2: checkout latency spike",
        "content": "p95 checkout latency jumped to 2.1s after the payments migration; rolling back.",
        "status": "In Progress",
        "issuetype": "Incident",
        "priority": "High",
    },
    {
        "author_email": "ava@acme.com",
        "created": "2025-09-10T11:00:00Z",
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
    """`docker run` args pointing mcp-atlassian at Backlot (Cloud shape via the fake host)."""
    u = urlparse(base_url)
    host = "backlot.atlassian.net"  # must end in .atlassian.net for Cloud detection
    if (u.hostname or "127.0.0.1") in _LOCAL_HOSTS:
        scheme, port, addhost, ssl_verify = "http", (u.port or 80), "host-gateway", True
        # The placeholder stands in only for the admin token, which has no address of its own; a
        # user's token needs that user's email, which is what the real service matches on.
        user = username or "svc@example.com"
    else:
        # remote deployment: alias the fake host to its IP, and require an explicit identity
        if not username:
            sys.exit(
                f"--url points at a remote deployment ({u.hostname}); also pass --username "
                "(and --token) — mcp-atlassian needs a Basic-auth username for Cloud detection "
                "and the token authenticates + scopes ACL (get one from GET /_meta/users)."
            )
        scheme = u.scheme
        port = u.port or (443 if u.scheme == "https" else 80)
        addhost = socket.gethostbyname(u.hostname)
        ssl_verify = False  # cert is for the real host, not backlot.atlassian.net
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
    p = argparse.ArgumentParser(description="Drive mcp-atlassian over MCP against Backlot.")
    p.add_argument(
        "--url", help="Backlot base URL to drive (default: spin up a local throwaway server)"
    )
    p.add_argument(
        "--token",
        help="Backlot bearer token from GET /_meta/users "
        "(default: the admin token, which sees everything)",
    )
    p.add_argument(
        "--username",
        help="Atlassian Basic-auth username: the address --token belongs to (required with "
        "--token, and for a remote --url)",
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
    # mcp-atlassian runs in a container and reaches a local server through host-gateway, which on
    # Linux is the docker bridge address — a loopback-only server never answers there. Docker
    # Desktop forwards host-gateway to the host's own loopback instead, so macOS keeps the narrower
    # bind and the firewall prompt that opening a port to the network raises.
    host = "0.0.0.0" if sys.platform == "linux" else "127.0.0.1"
    with serve_or_connect(CORPUS, url=args.url, host=host) as s:
        # Atlassian matches the pair, so either half alone is refused rather than sent: a token
        # under someone else's address is a 401, and an address with no token would carry the
        # admin's, which Backlot takes as the admin under any name.
        if args.token and not args.username:
            sys.exit(
                "--token names one user, so pass --username with that user's own email too: "
                f"Atlassian matches the pair, and {s.base_url}/_meta/users lists both"
            )
        if args.username and not args.token:
            sys.exit(
                f"--username alone would send the admin token under {args.username}, which "
                "Backlot takes as the admin: pass --token with that user's own token too "
                f"({s.base_url}/_meta/users lists both)"
            )
        if args.token:
            print(f"authenticating as {args.username} → retrieval is ACL-filtered to that user")
        params = build_params(s.base_url, args.token or s.token, args.username)
        run_agent(args.agent, params, QUESTION)
