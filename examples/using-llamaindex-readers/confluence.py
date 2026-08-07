#!/usr/bin/env python3
"""Load Confluence pages through the official llama-index Confluence reader. Self-contained.

pip install -e ".[examples,llamaindex]"
python examples/using-llamaindex-readers/confluence.py            # or: --url http://localhost:8000
python examples/using-llamaindex-readers/confluence.py --url http://localhost:8000 --token <usr-token>
"""

import argparse

from llama_index.readers.confluence import ConfluenceReader

from backlot import serve_or_connect
from backlot.integrations.llamaindex import atlassian_base_url

CORPUS = [
    {
        "source_type": "confluence",
        "space": "handbook",
        "title": "Engineering Handbook",
        "content": "How we build software: coding standards, review process, on-call.",
    },
    {
        "source_type": "confluence",
        "space": "handbook",
        "title": "On-call Runbook",
        "content": "Respond to gateway 502s: check dashboards, roll back, page on-call.",
    },
]


def build(mock, token):
    # atlassian-python-api 4.0.7 does not append `/wiki` itself regardless of `cloud` (`cloud`
    # only toggles cloud-specific API shapes elsewhere, not the URL), so the mock's
    # `/atlassian/wiki/rest/api` root must be spelled out in `base_url`.
    return ConfluenceReader(
        base_url=f"{atlassian_base_url(mock.base_url)}/wiki", cloud=False, api_token=token
    )


def main(reader):
    # `max_num_results` must be passed explicitly: llama-index-readers-confluence 0.7.0's
    # `load_data` otherwise forwards a bare `limit=None` to `Confluence.get_all_pages_from_space`,
    # which raises `TypeError` comparing `None` — a client-side bug independent of the mock.
    docs = reader.load_data(space_key="handbook", max_num_results=50)
    print(f"loaded {len(docs)} Document(s):")
    for d in docs:
        print(f"  - {d.metadata.get('title', d.doc_id)}: {d.text.splitlines()[0][:70]}")


def _parse_args():
    p = argparse.ArgumentParser(
        description="Load Confluence pages via llama-index against the mock."
    )
    p.add_argument("--url", help="mock base URL (default: spin up a local throwaway mock)")
    p.add_argument("--token", help="mock bearer token from GET /_mock/users (default: admin)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        if args.token:
            print("authenticating with --token → responses are ACL-filtered to that user")
        main(build(mock, args.token or mock.token))
