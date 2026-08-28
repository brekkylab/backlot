#!/usr/bin/env python3
"""Read Backlot's S3 through fsspec — and then through pandas, which speaks fsspec. Self-contained.

``s3fs`` is the fsspec implementation registered for ``s3://``, and its endpoint is an ordinary
constructor argument, so pointing it at Backlot is one ``client_kwargs``. Everything downstream —
``fsspec.open``, ``pandas.read_csv("s3://…")``, pyarrow, dask — then works against Backlot with no
further change, because they all resolve the URL through the same registry.

S3 uses an AWS access-key/secret pair (not a bearer token). With ``--url`` (a running server)
``--access-key``/``--secret-key`` are **required** — grab a pair from ``GET <url>/_meta/users``.
Without ``--url`` the local throwaway server's admin keypair is used.

    pip install -e ".[fsspec]"
    python examples/using-fsspec/s3.py
    python examples/using-fsspec/s3.py --url http://localhost:8000 --access-key <AKIA...> --secret-key <secret>
"""

import argparse
import json
import urllib.request

import fsspec
import pandas as pd

from backlot import serve_or_connect
from backlot.integrations.llamaindex import patch_s3fs_walk

BUCKET = "eng-artifacts"
_REVENUE = "region,quarter,revenue\nEMEA,{q},120\nAMER,{q},240\nAPAC,{q},90\n"
CORPUS = [
    {
        "author_email": "ava@acme.com",
        "created": "2026-02-11T09:00:00Z",
        "source_type": "s3",
        "bucket": BUCKET,
        "key": "metrics/q1.csv",
        "title": "Q1 revenue by region",
        "content": _REVENUE.format(q="Q1"),
        "content_type": "text/csv",
    },
    {
        "author_email": "ava@acme.com",
        "created": "2026-05-11T09:00:00Z",
        "source_type": "s3",
        "bucket": BUCKET,
        "key": "metrics/q2.csv",
        "title": "Q2 revenue by region",
        "content": _REVENUE.format(q="Q2"),
        "content_type": "text/csv",
    },
    {
        "author_email": "ava@acme.com",
        "created": "2025-12-05T09:00:00Z",
        "source_type": "s3",
        "bucket": BUCKET,
        "key": "runbooks/oncall.md",
        "title": "On-call Runbook",
        "content": "# On-call\nCheck dashboards, roll back, page on-call.",
        "content_type": "text/markdown",
    },
]


def storage_options(s, access_key, secret_key) -> dict:
    """The whole redirect. Anything that takes fsspec `storage_options` takes these."""
    return {
        "key": access_key,
        "secret": secret_key,
        "client_kwargs": {
            "endpoint_url": f"{s.base_url}/s3",
            "region_name": "us-east-1",
        },
    }


def main(opts: dict) -> None:
    patch_s3fs_walk()  # fsspec/s3fs `walk` bug; see backlot.integrations.llamaindex
    fs = fsspec.filesystem("s3", **opts)

    # Discovered, not assumed: with --url this runs against whatever corpus that server was built
    # from, not the one at the top of this file.
    buckets = fs.ls("/", detail=False)
    with_csv = [b for b in buckets if any(k.endswith(".csv") for k in fs.find(b))]
    bucket = BUCKET if BUCKET in buckets else next(iter(with_csv or buckets))
    keys = fs.find(bucket)

    print(f"=== fs.ls('/') -> {buckets} ===")
    print(f"\n=== fs.find({bucket!r}) ===")
    for key in keys:
        print(f"  {key}  ({fs.info(key)['size']} bytes)")

    print("\n=== fsspec.open() — a plain file handle over an s3:// URL ===")
    prefer = [k for k in keys if k.endswith((".md", ".txt", ".tf", ".json"))]
    text = (prefer or [k for k in keys if not k.endswith((".csv", ".pdf"))] or keys)[0]
    with fsspec.open(f"s3://{text}", "rt", **opts) as fh:
        print(f"  {text}: {fh.read().splitlines()[0]}")

    csvs = [k for k in keys if k.endswith(".csv")]
    if not csvs:
        print(f"\n(no CSV objects in {bucket!r}, so no pandas leg — the reads above are the point)")
        return

    # The point of the whole example: pandas never learns about Backlot. It hands the URL to
    # fsspec, fsspec hands it to s3fs, and s3fs is the one holding the endpoint.
    print(f"\n=== pandas.read_csv('s3://{csvs[0]}') ===")
    first = pd.read_csv(f"s3://{csvs[0]}", storage_options=opts)
    print(f"  {first.shape[0]} rows x {first.shape[1]} columns")
    print("  " + first.head().to_string(index=False).replace("\n", "\n  "))

    # Every CSV in the bucket that agrees with the first one's columns, as one frame. Real buckets
    # hold more than one table, so the shape is the grouping key rather than an assumption.
    same = [
        k for k in csvs if tuple(pd.read_csv(f"s3://{k}", storage_options=opts)) == tuple(first)
    ]
    print(f"\n=== {len(same)} of {len(csvs)} CSVs share those columns — concatenated ===")
    everything = pd.concat(
        [pd.read_csv(f"s3://{k}", storage_options=opts) for k in same], ignore_index=True
    )
    print(f"  {len(everything)} rows from {[k.rsplit('/', 1)[-1] for k in same]}")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read Backlot's S3 through fsspec and pandas.")
    p.add_argument("--url", help="Backlot base URL (default: spin up a local throwaway server)")
    p.add_argument(
        "--access-key",
        help="AWS access key id (S3 uses a keypair, not a token); "
        "required with --url — from GET <url>/_meta/users",
    )
    p.add_argument("--secret-key", help="AWS secret access key (required with --url)")
    args = p.parse_args()
    if args.url and not (args.access_key and args.secret_key):
        p.error(
            "--access-key and --secret-key are required with --url (from GET <url>/_meta/users)"
        )
    return args


def _admin_keys(base_url: str) -> tuple[str, str]:
    """The local throwaway server's admin S3 keypair, read from its /_meta/users."""
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/_meta/users") as r:
        data = json.load(r)
    return data["admin_s3_access_key_id"], data["admin_s3_secret_access_key"]


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as s:
        ak, sk = (args.access_key, args.secret_key) if args.url else _admin_keys(s.base_url)
        main(storage_options(s, ak, sk))
