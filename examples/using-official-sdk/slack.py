#!/usr/bin/env python3
"""Read and write Slack through the official slack_sdk. Self-contained: run it directly.

One run exercises the whole surface: the reads, then the six methods that write and the
three that read what they wrote, then the two
`/_meta/overlay` routes an evaluation uses. Nothing reaches the corpus file — writes land in an
in-memory overlay that dies with the server, and the reset at the end puts the corpus back, which
is what lets one evaluation run start where the last one did. See docs/write-overlay.md.

pip install -e ".[official-sdk]"
python examples/using-official-sdk/slack.py            # or: --url http://localhost:8000
python examples/using-official-sdk/slack.py --url http://localhost:8000 --token <usr-token>
"""

import argparse
import json
import urllib.request

from slack_sdk import WebClient

from backlot import serve_or_connect

CORPUS = [
    {
        "author_email": "ava@acme.com",
        "created": "2026-02-05T17:00:00Z",
        "source_type": "slack",
        "channel": "eng",
        "content": "Deploy freeze starts Friday 5pm.",
    },
    {
        "author_email": "bob@acme.com",
        "created": "2026-02-10T18:00:00Z",
        "source_type": "slack",
        "channel": "incidents",
        "content": "Anyone seeing 502s from the gateway?",
        "replies": [
            {
                "author_email": "ava@acme.com",
                "created": "2026-02-10T18:00:40Z",
                "content": "Looking now.",
            },
            {
                "author_email": "bob@acme.com",
                "created": "2026-02-10T18:06:00Z",
                "content": "Rolled back — clearing up.",
            },
        ],
    },
]

_p = argparse.ArgumentParser(
    description="Read and write Slack through the official slack_sdk against Backlot."
)
_p.add_argument(
    "--url", help="Backlot base URL to drive (default: spin up a local throwaway server)"
)
_p.add_argument(
    "--token",
    help="Backlot bearer token from GET /_meta/users "
    "(default: the admin token, which sees everything)",
)
args = _p.parse_args()


def meta(base_url: str, path: str, token: str | None = None) -> dict:
    """GET or POST a `/_meta` route. Reset is the one that takes the admin token, so it is passed
    explicitly rather than taken from the client, which may be answering as somebody else."""
    request = urllib.request.Request(f"{base_url}/_meta/{path}")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
        request.data = b""
    with urllib.request.urlopen(request) as response:
        return json.load(response)


with serve_or_connect(CORPUS, url=args.url) as s:
    if args.token:
        print("authenticating with --token → responses are ACL-filtered to that user")
    client = WebClient(token=args.token or s.token, base_url=f"{s.base_url}/slack/api/")

    channels = client.conversations_list()["channels"]
    if not channels:
        print("no channels visible to this identity")
        raise SystemExit(0)

    channel = channels[0]["id"]
    name = channels[0]["name"]

    def history() -> list[dict]:
        return client.conversations_history(channel=channel, limit=50)["messages"]

    before = history()
    print(f"{len(channels)} channels; #{name} has {len(before)} message(s):")
    for m in before:
        print(f"  - {m['text'].splitlines()[0]}")

    print(f"\nwriting to #{name}")
    root = client.chat_postMessage(channel=channel, text="Deploy freeze lifted.")["ts"]
    reply = client.chat_postMessage(channel=channel, text="Shipping now.", thread_ts=root)["ts"]
    thread = client.conversations_replies(channel=channel, ts=root)["messages"]
    print(f"  posted {root}, replied in its thread → conversations.replies returns {len(thread)}")

    # A reply's permalink names the thread it is in, as real's does.
    print(
        f"  permalink: {client.chat_getPermalink(channel=channel, message_ts=reply)['permalink']}"
    )

    # A written message is a message: the reads find it under the same ACL as the corpus around
    # it, search included -- which is the part a mock usually gets wrong.
    matches = client.search_messages(query="freeze")["messages"]["matches"]
    print(f"  search 'freeze' finds the new message: {root in [m['ts'] for m in matches]}")

    client.reactions_add(channel=channel, timestamp=root, name="tada")
    reacted = client.reactions_get(channel=channel, timestamp=root)["message"]
    listed = client.reactions_list()["items"]
    print(
        f"  reacted → {[(r['name'], r['count']) for r in reacted['reactions']]}, "
        f"reactions.list has {len(listed)} item(s) of this caller's own"
    )
    client.reactions_remove(channel=channel, timestamp=root, name="tada")
    cleared = client.reactions_get(channel=channel, timestamp=root)["message"]
    print(f"  removed → reactions: {cleared.get('reactions', [])}")

    client.chat_update(channel=channel, ts=root, text="Deploy freeze lifted - ship away.")
    edited = next(m for m in history() if m["ts"] == root)
    print(f"  edited to {edited['text']!r}, stamped by {edited['edited']['user']}")

    # An ephemeral is in no channel's history, so nothing is stored for it.
    depth = len(history())
    client.chat_postEphemeral(channel=channel, user=edited["user"], text="only you see this")
    print(f"  posted an ephemeral; history is still {depth} message(s): {len(history()) == depth}")

    print(f"  /_meta/overlay: { {k: len(v) for k, v in meta(s.base_url, 'overlay').items() if v} }")

    # Deleting a thread ROOT leaves the placeholder real leaves, so the thread stays readable;
    # deleting its last reply takes the placeholder with it.
    client.chat_delete(channel=channel, ts=root)
    stub = next(m for m in history() if m["ts"] == root)
    print(
        f"  deleted the root → subtype={stub['subtype']!r}, thread still returns "
        f"{len(client.conversations_replies(channel=channel, ts=root)['messages'])}"
    )
    client.chat_delete(channel=channel, ts=reply)
    print(f"  deleted the reply → root gone too: {root not in [m['ts'] for m in history()]}")

    meta(s.base_url, "overlay/reset", token=s.token)
    empty = not any(meta(s.base_url, "overlay").values())
    print(f"\nreset → overlay empty: {empty}, #{name} back to {len(history())} message(s)")
