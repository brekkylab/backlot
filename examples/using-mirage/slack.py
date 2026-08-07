#!/usr/bin/env python3
"""Read Slack through mirage's virtual filesystem. Self-contained: run it directly.

Mirage mounts the mock's Slack API as a filesystem — channels, dates, and a ``chat.jsonl`` per
day — so an agent reads it with plain ``ls`` / ``cat``. Slack's API host is a config knob
(``SlackConfig(base_url=...)``), so we point it straight at the mock — no monkeypatch.

    pip install -e ".[examples,mirage]"
    python examples/using-mirage/slack.py                                  # local throwaway mock
    python examples/using-mirage/slack.py --url http://localhost:8000
    python examples/using-mirage/slack.py --url http://localhost:8000 --token <usr-token>
    python examples/using-mirage/slack.py --url http://localhost:8000 --fuse   # real OS mount

With ``--fuse`` the channel tree is exposed as an actual filesystem (needs macFUSE/fuse3) and
read with plain ``os``/shell tools; otherwise it's driven in-process via ``ws.execute``.
"""

import argparse
import os
import subprocess

from mirage import MountMode, Workspace
from mirage.resource.slack import SlackConfig, SlackResource

from backlot import serve_or_connect
from backlot.integrations.mirage import slack_base_url

from _helpers import FUSE_HELP, lines, run_mirage

CORPUS = [  # `created` keeps the throwaway channels' dates tight (one day) rather than synthesized
    {
        "source_type": "slack",
        "channel": "eng",
        "content": "Deploy freeze starts Friday 5pm.",
        "created": "2024-08-01T09:00:00Z",
    },
    {
        "source_type": "slack",
        "channel": "incidents",
        "content": "Anyone seeing 502s from the gateway?",
        "created": "2024-08-01T14:30:00Z",
        "replies": [{"content": "Looking now."}, {"content": "Rolled back — clearing up."}],
    },
]


def build(mock, token):
    # Slack's host is a config knob — point it at the mock (no monkeypatch needed).
    # --token <usr-token> (from /_mock/users) → ACL-filtered to that user; else admin sees all.
    return SlackResource(SlackConfig(token=token, base_url=slack_base_url(mock.base_url)))


async def main(resource) -> None:
    ws = Workspace({"/slack": resource}, mode=MountMode.READ)

    print("=== ls /slack/ ===")
    print((await (await ws.execute("ls /slack/")).stdout_str()).rstrip())

    r = await ws.execute("ls /slack/channels/")
    channels = lines(await r.stdout_str())
    if not channels:
        print("no channels visible to this identity")
        return
    print(f"\n=== {len(channels)} channel(s); reading #{channels[0]} ===")

    # channels/<name>/<date>/chat.jsonl — grab the most recent day's transcript.
    base = f"/slack/channels/{channels[0]}"
    dates = lines(await (await ws.execute(f'ls "{base}/"')).stdout_str())
    if not dates:
        print("  channel has no dated messages")
        return
    day = dates[-1].rstrip("/")
    chat = f"{base}/{day}/chat.jsonl"
    print(f"$ cat {chat}")
    print((await (await ws.execute(f'cat "{chat}"')).stdout_str()).rstrip()[:600])

    # grep is scoped to the one day's transcript (walking every channel/day would be huge).
    print(f"\n=== grep -c message {base}/{day}/ ===")
    r = await ws.execute(f'grep -rc message "{base}/{day}/"')
    print("  " + ((await r.stdout_str()).rstrip() or "(no matches)"))


def main_fuse(resource) -> None:
    """--fuse: mount the channel tree as a *real* filesystem, then read it with ordinary tools —
    any process (grep, an editor, an indexer) can open the files. Needs macFUSE/fuse3."""
    try:
        with Workspace({"/slack": resource}, mode=MountMode.READ) as ws:
            mnt = ws.add_fuse_mount("/slack")  # "/slack" is now a real directory on disk
            print(f"=== mounted at {mnt} — an ordinary filesystem now ===")
            channel = sorted(os.listdir(f"{mnt}/channels"))[0]
            day = sorted(os.listdir(f"{mnt}/channels/{channel}"))[-1]  # most recent day
            chat = f"{mnt}/channels/{channel}/{day}/chat.jsonl"
            print(f"\n$ head -c 160 channels/{channel}/{day}/chat.jsonl")
            print("  " + open(chat).read(160).replace("\n", " "))  # a genuine open() via FUSE
            count = subprocess.run(["grep", "-c", ".", chat], capture_output=True, text=True)
            print(
                f"\n$ grep -c . <that file>   # a separate process reads the mount → {count.stdout.strip()}"
            )
            print(f"\nexplore it live in another terminal:  ls {mnt}/channels")
    except (ImportError, RuntimeError, OSError) as e:
        raise SystemExit(FUSE_HELP.format(err=e))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read Slack through mirage against the mock.")
    p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
    p.add_argument(
        "--token",
        help="mock bearer token from GET /_mock/users "
        "(default: the admin token, which sees everything)",
    )
    p.add_argument(
        "--fuse", action="store_true", help="mount as a real FUSE filesystem (needs macFUSE/fuse3)"
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        if args.token:
            print("authenticating with --token → responses are ACL-filtered to that user")
        resource = build(mock, args.token or mock.token)
        if args.fuse:
            main_fuse(resource)
        else:
            run_mirage(main(resource))
