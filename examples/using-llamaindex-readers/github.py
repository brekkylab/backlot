#!/usr/bin/env python3
"""Load GitHub issues/PRs through the official llama-index GitHub reader. Self-contained.

pip install -e ".[examples,llamaindex]"
python examples/using-llamaindex-readers/github.py            # or: --url http://localhost:8000
python examples/using-llamaindex-readers/github.py --url http://localhost:8000 --token <usr-token>
"""

import argparse
import sys
from pathlib import Path

from backlot import serve_or_connect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _common.syspath import drop_self_from_syspath

# This file is named github.py; drop its own dir so the local helper import above wins but any
# `import github` inside the reader's deps resolves to the real package.
drop_self_from_syspath(__file__)

from llama_index.readers.github import (  # noqa: E402
    GitHubIssuesClient,
    GitHubRepositoryIssuesReader,
)

CORPUS = [
    {
        "state": "open",
        "author_email": "bob@acme.com",
        "created": "2026-02-09T09:00:00Z",
        "source_type": "github",
        "repo": "gateway",
        "title": "Rate limiter drops bursts under 50ms",
        "content": "The token-bucket refill is off by one tick.",
        "subtype": "issue",
    },
    {
        "state": "open",
        "author_email": "bob@acme.com",
        "created": "2026-02-09T14:00:00Z",
        "source_type": "github",
        "repo": "gateway",
        "title": "Fix token-bucket refill off-by-one",
        "content": "Corrects the refill tick; adds a regression test.",
        "subtype": "pull_request",
    },
]


def build(mock, token):
    client = GitHubIssuesClient(github_token=token, base_url=f"{s.base_url}/github", verbose=False)
    return GitHubRepositoryIssuesReader(client, owner="acme", repo="gateway", verbose=False)


def main(reader):
    docs = reader.load_data(state=GitHubRepositoryIssuesReader.IssueState.ALL)
    print(f"loaded {len(docs)} Document(s):")
    for d in docs:
        print(f"  - {d.metadata.get('title', d.doc_id)}: {d.text.splitlines()[0][:70]}")


def _parse_args():
    p = argparse.ArgumentParser(description="Load GitHub issues via llama-index against Backlot.")
    p.add_argument("--url", help="Backlot base URL (default: spin up a local throwaway server)")
    p.add_argument("--token", help="Backlot bearer token from GET /_meta/users (default: admin)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as s:
        if args.token:
            print("authenticating with --token → responses are ACL-filtered to that user")
        main(build(s, args.token or s.token))
