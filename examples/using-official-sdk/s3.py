#!/usr/bin/env python3
"""Read S3 through the official AWS SDK (boto3). Self-contained: run it directly.

    pip install -e ".[examples]"
    python examples/using-official-sdk/s3.py            # local mock, uses its admin keypair
    python examples/using-official-sdk/s3.py --url http://localhost:8000 \
        --access-key <AKIA...> --secret-key <secret>    # AWS keys, e.g. from GET /_mock/users

The only changes from talking to real S3 are ``endpoint_url`` (point it at the mock's ``/s3``) and
path-style addressing (so the bucket stays in the path, not the hostname). boto3 SigV4-signs every
request. S3 uses an AWS access-key/secret pair (not a bearer token). With ``--url`` (a running
server) ``--access-key`` / ``--secret-key`` are **required** — pass real AWS keys, or a pair from
``GET <url>/_mock/users`` (each user, and the admin, has an ``s3_access_key_id`` /
``s3_secret_access_key`` there). Without ``--url`` the local throwaway mock uses its own admin
keypair.
"""

import argparse
import json
import urllib.request

import boto3
from botocore.config import Config

from backlot import serve_or_connect

CORPUS = [
    {
        "source_type": "s3",
        "bucket": "eng-artifacts",
        "key": "runbooks/oncall.md",
        "title": "On-call Runbook",
        "content": "# On-call\nCheck dashboards, roll back, page on-call.",
        "content_type": "text/markdown",
    },
    {
        "source_type": "s3",
        "bucket": "eng-artifacts",
        "key": "design/architecture.md",
        "title": "Architecture",
        "content": "Gateway, workers, and the token bucket.",
        "content_type": "text/markdown",
    },
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read S3 through boto3 against the mock's S3.")
    p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
    p.add_argument(
        "--access-key",
        help="AWS access key id (S3 uses a keypair, not a token); "
        "required with --url — from GET <url>/_mock/users, or real AWS",
    )
    p.add_argument("--secret-key", help="AWS secret access key (required with --url)")
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
        s3 = boto3.client(
            "s3",
            endpoint_url=f"{mock.base_url}/s3",
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
            region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"}),
        )

        buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
        print(f"buckets → {buckets}")

        for b in buckets:
            objs = s3.list_objects_v2(Bucket=b).get("Contents", [])
            print(f"\ns3://{b}/ → {len(objs)} object(s):")
            for o in objs:
                print(f"  - {o['Key']}  ({o['Size']} bytes, {o['ETag']})")

        if buckets:
            first = s3.list_objects_v2(Bucket=buckets[0]).get("Contents", [])
            if first:
                key = first[0]["Key"]
                body = s3.get_object(Bucket=buckets[0], Key=key)["Body"].read().decode()
                print(f"\nget s3://{buckets[0]}/{key}:\n  " + body.replace("\n", "\n  "))
                head = s3.head_object(Bucket=buckets[0], Key=key)
                print(f"\nhead → {head['ContentType']}, {head['ContentLength']} bytes")
                part = s3.get_object(Bucket=buckets[0], Key=key, Range="bytes=0-6")["Body"].read()
                print(f"range bytes=0-6 → {part!r}")
