#!/usr/bin/env python3
"""Read Slack through the official slack_sdk. Self-contained: run it directly.

pip install -e ".[examples]"
python examples/using-official-sdk/slack.py            # or: --url http://localhost:8000
python examples/using-official-sdk/slack.py --url http://localhost:8000 --token <usr-token>
"""

import argparse

from slack_sdk import WebClient

from backlot import serve_or_connect

CORPUS = [
    {"source_type": "slack", "channel": "eng", "content": "Deploy freeze starts Friday 5pm."},
    {
        "source_type": "slack",
        "channel": "incidents",
        "content": "Anyone seeing 502s from the gateway?",
        "replies": [{"content": "Looking now."}, {"content": "Rolled back — clearing up."}],
    },
]

_p = argparse.ArgumentParser(
    description="Read Slack through the official slack_sdk against the mock."
)
_p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
_p.add_argument(
    "--token",
    help="mock bearer token from GET /_mock/users "
    "(default: the admin token, which sees everything)",
)
args = _p.parse_args()

with serve_or_connect(CORPUS, url=args.url) as mock:
    if args.token:
        print("authenticating with --token → responses are ACL-filtered to that user")
    client = WebClient(token=args.token or mock.token, base_url=f"{mock.base_url}/slack/api/")

    channels = client.conversations_list()["channels"]
    if not channels:
        print("no channels visible to this identity")
    else:
        channel = channels[0]
        messages = client.conversations_history(channel=channel["id"], limit=10)["messages"]
        print(f"{len(channels)} channels; #{channel['name']} has these recent messages:")
        for m in messages:
            print(f"  - {m['text'].splitlines()[0]}")
