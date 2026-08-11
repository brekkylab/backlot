#!/usr/bin/env python3
"""Drive the awslabs aws-api MCP server against the mock's S3. Self-contained.

Runs `awslabs.aws-api-mcp-server` via **uvx** (Python) pointed at the mock's `/s3`, then lets an
LLM agent answer a question by calling its MCP tools (which shell the AWS CLI). The CLI's boto3
client honors a first-class `AWS_ENDPOINT_URL` override and SigV4-signs every call, so pointing it
at the mock is a handful of env vars — no Docker/host-gateway tricks.

NOTE: awslabs' server (an SSRF guard in its parser's `_validate_endpoint`) only accepts a
**loopback** endpoint — `localhost` / `127.0.0.1` / `::1`. A hostname is rejected ("Could not
resolve endpoint …") and even a non-loopback IP is rejected ("Local endpoint was not a loopback
address"). So to drive a remote deployment, tunnel it to loopback and point `--url` there:
    ssh -fN -L 18000:127.0.0.1:8000 user@host
    python examples/using-mcp-with-agents/s3.py --url http://127.0.0.1:18000 --access-key … --secret-key …

S3 authenticates with an AWS access-key/secret pair (not a bearer token). With `--url` (a running
server) `--access-key` / `--secret-key` are **required** — pass real AWS keys, or a pair from
`GET <url>/_mock/users` (each user, and the admin, has an `s3_access_key_id` / `s3_secret_access_key`
there). Without `--url` the local throwaway mock uses its own admin keypair.

Prereqs: uvx (Astral `uv`); `pip install -e ".[mcp]"`; an LLM key for `--agent` (`ANTHROPIC_API_KEY`,
or `OPENAI_API_KEY` with `--agent openai`). Run from the repo root:
    ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/s3.py            # local mock
    ANTHROPIC_API_KEY=… python examples/using-mcp-with-agents/s3.py \
        --url https://host --access-key <AKIA…> --secret-key <secret> [--agent openai]
"""

from __future__ import annotations

import argparse
import json
import urllib.request

from mcp import StdioServerParameters

from _agent import run_agent
from backlot import serve_or_connect

CORPUS = [
    {
        "source_type": "s3",
        "bucket": "payments",
        "key": "incidents/sev2.md",
        "title": "SEV2: checkout latency spike",
        "content": "p95 checkout latency jumped to 2.1s after the payments migration; rolling back.",
    },
    {
        "source_type": "s3",
        "bucket": "runbooks",
        "key": "oncall/checkout.md",
        "title": "On-call Runbook: checkout latency & bad deploys",
        "content": "When a deploy or migration spikes checkout latency: check the payments "
        "dashboards, roll back the last change, and page the on-call engineer.",
    },
]
QUESTION = (
    "The company's knowledge base is stored as objects in S3 buckets — use the S3 API only "
    "(no other AWS services). List the buckets, list a bucket's objects, and read an "
    "object's contents with `aws s3 cp s3://<bucket>/<key> -` (the trailing dash streams "
    "the body to stdout; `s3api get-object` only writes to a file and won't return the "
    "body). Find the incident about checkout latency and summarize it, then find the "
    "on-call runbook. Cite the object keys."
)


def build_params(base_url: str, access_key: str, secret_key: str) -> StdioServerParameters:
    """`uvx` args pointing the awslabs aws-api MCP server at the mock via AWS_ENDPOINT_URL.

    We deliberately do NOT set READ_OPERATIONS_ONLY: it blocks `aws s3 cp s3://… -`, which is the
    only way this server streams an object's *body* back to the model (a read-only `s3api
    get-object` just writes the bytes to a sandboxed file and returns metadata, so the agent can
    list objects but never read them). The mock has no write endpoints, so dropping the read-only
    guard is safe here; against real AWS you'd weigh read-only vs. being able to read object bodies."""
    return StdioServerParameters(
        command="uvx",
        args=["awslabs.aws-api-mcp-server@latest"],
        env={
            "AWS_ENDPOINT_URL": f"{base_url.rstrip('/')}/s3",
            "AWS_ACCESS_KEY_ID": access_key,
            "AWS_SECRET_ACCESS_KEY": secret_key,
            "AWS_REGION": "us-east-1",
        },
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive awslabs aws-api-mcp-server over MCP against the mock's S3."
    )
    p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
    p.add_argument(
        "--access-key",
        help="AWS access key id (S3 uses a keypair, not a token); "
        "required with --url — from GET <url>/_mock/users, or real AWS",
    )
    p.add_argument("--secret-key", help="AWS secret access key (required with --url)")
    p.add_argument(
        "--agent",
        choices=("anthropic", "openai"),
        default="anthropic",
        help="which LLM agent to run (default: anthropic)",
    )
    args = p.parse_args()
    if args.url and not (args.access_key and args.secret_key):
        p.error(
            "--access-key and --secret-key are required with --url "
            "(grab a pair from GET <url>/_mock/users)"
        )
    return args


def _admin_keys(base_url: str) -> tuple[str, str]:
    """The local throwaway mock's admin S3 keypair, read from its /_mock/users."""
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/_mock/users") as r:
        data = json.load(r)
    return data["admin_s3_access_key_id"], data["admin_s3_secret_access_key"]


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        # with --url you pass your own AWS keys; the local throwaway mock uses its admin keypair
        ak, sk = (args.access_key, args.secret_key) if args.url else _admin_keys(mock.base_url)
        run_agent(args.agent, build_params(mock.base_url, ak, sk), QUESTION)
