#!/usr/bin/env python3
"""Read a repo out of Backlot's GitHub through fsspec — the file tree as a filesystem.

fsspec ships its own `GithubFileSystem` (registered for ``github://``), which serves a repo's file
tree over the Contents and Git Trees APIs. It hardcodes api.github.com in six places and takes no
endpoint argument, so `backlot.integrations.fsspec.github_filesystem_at` subclasses it: two of the
six are class attributes, the other four are built inside method bodies and are replaced outright.

    pip install -e ".[fsspec]"
    python examples/using-fsspec/github.py
    python examples/using-fsspec/github.py --url http://localhost:8000 --token <usr-token> --repo pipeline
"""

import argparse
import json
import urllib.request

import pandas as pd

from backlot import serve_or_connect
from backlot.integrations.fsspec import github_filesystem_at

REPO = "platform"


def _file(path, content):
    return {
        "source_type": "github",
        "repo": REPO,
        "subtype": "file",
        "path": path,
        "title": path.rsplit("/", 1)[-1],
        "content": content,
        "author_email": "ava@acme.com",
        "created": "2026-02-01T09:00:00Z",
    }


CORPUS = [
    _file("README.md", "# platform\nIngest, transform, export.\n"),
    _file("src/ingest/consumer.py", "def consume():\n    return 'rows'\n"),
    _file("src/export/writer.py", "def write(rows):\n    return len(rows)\n"),
    _file("docs/runbook.md", "# Runbook\nRestart the consumer group.\n"),
    _file(
        "data/latency.csv",
        "service,p50_ms,p99_ms\ningest,12,180\nexport,31,240\ngateway,8,95\n",
    ),
]


def main(fs, repo: str) -> None:
    print(f"=== fs.ls('') — the root of {repo!r} ===")
    for entry in fs.ls("", detail=True):
        print(f"  {entry['type']:9} {entry['name']}")

    # A walk descends by the SUBTREE sha read out of each listing, one directory per request —
    # which is why `git/trees/{ref}` has to answer the tree it was asked for rather than the root.
    print("\n=== fs.walk('') ===")
    for root, dirs, files in fs.walk(""):
        print(f"  {root or '.':22} dirs={dirs} files={files}")

    print("\n=== cat a source file ===")
    source = next((p for p in fs.find("") if p.endswith(".py")), None)
    if source:
        print(f"  {source}:")
        print("    " + fs.cat_file(source).decode().rstrip().replace("\n", "\n    "))

    # Same trick as the other two scripts: pandas resolves nothing itself, it just asks fsspec for
    # a handle. A CSV committed to a repo is a table without a checkout.
    csv = next((p for p in fs.find("") if p.endswith(".csv")), None)
    if csv:
        print(f"\n=== {csv} as a DataFrame ===")
        df = pd.read_csv(fs.open(csv))
        print("  " + df.to_string(index=False).replace("\n", "\n  "))
    else:
        print("\n(no CSV committed in this repo, so no pandas leg)")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read a repo through fsspec against Backlot.")
    p.add_argument("--url", help="Backlot base URL (default: spin up a local throwaway server)")
    p.add_argument("--token", help="bearer token to read as (default: Backlot's admin token)")
    p.add_argument("--repo", default=REPO, help=f"repository to read (default: {REPO})")
    return p.parse_args()


def _org(base_url: str) -> str:
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/_meta/users") as r:
        return json.load(r)["org"]


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as s:
        token = args.token or s.token
        fs = github_filesystem_at(s.base_url, token, org=_org(s.base_url), repo=args.repo)
        main(fs, args.repo)
