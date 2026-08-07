#!/usr/bin/env python3
"""Load Notion pages through the official llama-index Notion reader. Self-contained.

NotionPageReader hardcodes the Notion host in module constants; patch_notion_at() rebinds them at
the mock before the reader runs.

    pip install -e ".[examples,llamaindex]"
    python examples/using-llamaindex-readers/notion.py            # or: --url http://localhost:8000
    python examples/using-llamaindex-readers/notion.py --url http://localhost:8000 --token <usr-token>
"""

import argparse

from llama_index.readers.notion import NotionPageReader

from backlot import serve_or_connect
from backlot.integrations.llamaindex import notion_base_url, patch_notion_at

CORPUS = [
    {
        "source_type": "notion",
        "teamspace": "engineering",
        "doc_id": "runbook",
        "title": "On-call Runbook",
        "content": "# On-call\n\nCheck dashboards, roll back, page on-call.",
    },
    {
        "source_type": "notion",
        "teamspace": "engineering",
        "doc_id": "howto",
        "title": "Deploy How-to",
        "content": "Merge to main, wait for CI, promote the build.",
    },
]


def build(mock, token):
    patch_notion_at(notion_base_url(mock.base_url))
    return NotionPageReader(integration_token=token)


def main(reader):
    # Discover page ids via the reader's own search (patched at the mock), then load them. The
    # installed reader's `search()` returns a flat list of ids (not result dicts), and an empty
    # query returns everything visible on the mock (pages and databases alike — no object-type
    # filter is applied client-side here).
    page_ids = reader.search("")
    docs = reader.load_data(page_ids=page_ids)
    print(f"loaded {len(docs)} Document(s):")
    for d in docs:
        print(f"  - {d.doc_id}: {d.text.splitlines()[0][:70]}")


def _parse_args():
    p = argparse.ArgumentParser(description="Load Notion pages via llama-index against the mock.")
    p.add_argument("--url", help="mock base URL (default: spin up a local throwaway mock)")
    p.add_argument("--token", help="mock bearer token from GET /_mock/users (default: admin)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        if args.token:
            print("authenticating with --token → responses are ACL-filtered to that user")
        main(build(mock, args.token or mock.token))
