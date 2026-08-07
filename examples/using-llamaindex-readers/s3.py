#!/usr/bin/env python3
"""Load S3 objects through the official llama-index S3 reader. Self-contained.

S3 uses an AWS access-key/secret pair (not a bearer token). With `--url` (a running server),
`--access-key`/`--secret-key` are required — grab a pair from GET <url>/_mock/users. Without
`--url` the local throwaway mock's admin keypair is used.

    pip install -e ".[examples,llamaindex]"
    python examples/using-llamaindex-readers/s3.py
    python examples/using-llamaindex-readers/s3.py --url http://localhost:8000 --access-key <AK> --secret-key <sk>
"""

import argparse
import json
import urllib.request

from llama_index.readers.s3 import S3Reader

from backlot import serve_or_connect
from backlot.integrations.llamaindex import patch_s3fs_walk, s3_base_url

BUCKET = "eng-artifacts"
CORPUS = [
    {
        "source_type": "s3",
        "bucket": BUCKET,
        "key": "runbooks/oncall.md",
        "title": "On-call Runbook",
        "content": "# On-call\nCheck dashboards, roll back, page on-call.",
        "content_type": "text/markdown",
    },
    {
        "source_type": "s3",
        "bucket": BUCKET,
        "key": "design/architecture.md",
        "title": "Architecture",
        "content": "Gateway, workers, and the token bucket.",
        "content_type": "text/markdown",
    },
]


def build(mock, access_key, secret_key):
    patch_s3fs_walk()  # fsspec/s3fs compat bug workaround; see backlot.integrations.llamaindex
    return S3Reader(
        bucket=BUCKET,
        s3_endpoint_url=s3_base_url(mock.base_url),
        aws_access_id=access_key,
        aws_access_secret=secret_key,
        region_name="us-east-1",
    )


def main(reader):
    docs = reader.load_data()
    print(f"loaded {len(docs)} Document(s):")
    for d in docs:
        print(f"  - {d.metadata.get('file_name', d.doc_id)}: {d.text.splitlines()[0][:70]}")


def _admin_keys(base_url):
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/_mock/users") as r:
        data = json.load(r)
    return data["admin_s3_access_key_id"], data["admin_s3_secret_access_key"]


def _parse_args():
    p = argparse.ArgumentParser(description="Load S3 objects via llama-index against the mock.")
    p.add_argument("--url", help="mock base URL (default: spin up a local throwaway mock)")
    p.add_argument("--access-key", help="AWS access key id (required with --url)")
    p.add_argument("--secret-key", help="AWS secret access key (required with --url)")
    args = p.parse_args()
    if args.url and not (args.access_key and args.secret_key):
        p.error(
            "--access-key and --secret-key are required with --url (from GET <url>/_mock/users)"
        )
    return args


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        ak, sk = (args.access_key, args.secret_key) if args.url else _admin_keys(mock.base_url)
        main(build(mock, ak, sk))
