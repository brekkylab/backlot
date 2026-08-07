#!/usr/bin/env python3
"""Load Slack channels through the official llama-index Slack reader. Self-contained.

The reader has no base_url arg, but its underlying slack_sdk WebClient does. It's not enough to
set it *after* construction, though: `SlackReader.__init__` eagerly calls `client.api_test()`
before returning, using whatever base_url the client was built with. `slack_reader_at` (in
`backlot.integrations.llamaindex`) briefly swaps in a WebClient subclass that defaults to the
mock's base_url for just that one construction, so even the eager call lands on the mock.

    pip install -e ".[examples,llamaindex]"
    python examples/using-llamaindex-readers/slack.py            # or: --url http://localhost:8000
    python examples/using-llamaindex-readers/slack.py --url http://localhost:8000 --token <usr-token>
"""

import argparse

from backlot import serve_or_connect
from backlot.integrations.llamaindex import slack_reader_at

CORPUS = [
    {"source_type": "slack", "channel": "eng", "content": "Deploy freeze starts Friday 5pm."},
    {
        "source_type": "slack",
        "channel": "incidents",
        "content": "Anyone seeing 502s from the gateway?",
        "replies": [{"content": "Looking now."}, {"content": "Rolled back — clearing up."}],
    },
]


def build(mock, token):
    return slack_reader_at(mock.base_url, token)


def main(reader):
    channels = reader._client.conversations_list(limit=200)["channels"]
    docs = reader.load_data(channel_ids=[c["id"] for c in channels])
    print(f"loaded {len(docs)} Document(s) from {len(channels)} channel(s):")
    for c, d in zip(channels, docs):
        print(f"  - #{c['name']}: {d.text.splitlines()[0][:70]}")


def _parse_args():
    p = argparse.ArgumentParser(description="Load Slack channels via llama-index against the mock.")
    p.add_argument("--url", help="mock base URL (default: spin up a local throwaway mock)")
    p.add_argument("--token", help="mock bearer token from GET /_mock/users (default: admin)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        if args.token:
            print("authenticating with --token → responses are ACL-filtered to that user")
        main(build(mock, args.token or mock.token))
