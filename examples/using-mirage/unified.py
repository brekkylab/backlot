#!/usr/bin/env python3
"""Mount Slack, Gmail, and Drive as ONE filesystem and search across them — mirage's core
value: many backends, one set of bash commands. Self-contained: run it directly.

    pip install -e ".[examples,mirage]"
    python examples/using-mirage/unified.py                                # local throwaway mock
    python examples/using-mirage/unified.py --url http://localhost:8000 --token xoxb-... --user ava@acme.com
    python examples/using-mirage/unified.py --fuse                          # all three as one OS mount

Slack is ACL-filtered by ``--token``; Gmail/Drive by ``--user`` (they share one Google
authorized-user credential). Point them all at the same mock.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from mirage import MountMode, Workspace
from mirage.resource.gdrive import GoogleDriveConfig, GoogleDriveResource
from mirage.resource.gmail import GmailConfig, GmailResource
from mirage.resource.slack import SlackConfig, SlackResource

from backlot import serve_or_connect
from backlot.integrations.mirage import point_google_at, slack_base_url

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _common.google_creds import google_oauth_user

from _helpers import FUSE_HELP, lines, run_mirage

# One term — "Q1" — deliberately threads through all three sources.
CORPUS = [
    {
        "source_type": "slack",
        "channel": "finance",
        "created": "2024-08-01T10:00:00Z",
        "content": "Q1 revenue landed 12% over plan — great work team.",
    },
    {
        "source_type": "gmail",
        "mailbox": "ceo",
        "title": "Q1 board deck draft",
        "content": "Draft narrative for the Q1 board meeting.",
        "author_email": "ceo@acme.com",
    },
    {
        "source_type": "google_drive",
        "folder": "finance",
        "title": "Q1 Revenue Model",
        "content": "quarter,revenue\nQ1,1200000",
        "subtype": "spreadsheet",
        "author_email": "ceo@acme.com",
    },
]


async def _ls(ws, path):
    return lines(await (await ws.execute(f'ls "{path}"')).stdout_str())


async def _first_slack_chat(ws):
    chans = await _ls(ws, "/slack/channels/")
    if not chans:
        return None
    dates = await _ls(ws, f"/slack/channels/{chans[0]}/")
    return f"/slack/channels/{chans[0]}/{dates[-1].rstrip('/')}/chat.jsonl" if dates else None


async def _first_gmail_msg(ws):
    labels = await _ls(ws, "/gmail/")
    for label in (["INBOX"] if "INBOX" in labels else []) + labels:
        dates = await _ls(ws, f"/gmail/{label}/")
        if not dates:
            continue
        day = dates[0].rstrip("/")
        files = [f for f in await _ls(ws, f"/gmail/{label}/{day}/") if f.endswith(".gmail.json")]
        if files:
            return f"/gmail/{label}/{day}/{files[0]}"
    return None


async def _first_drive_file(ws):
    root = lines(await (await ws.execute("ls -F /gdrive/")).stdout_str())
    folders = [e.rstrip("/") for e in root if e.endswith("/")]
    if not folders:
        return None
    entries = lines(await (await ws.execute(f'ls -F "/gdrive/{folders[0]}/"')).stdout_str())
    files = [e for e in entries if not e.endswith("/")]
    return f"/gdrive/{folders[0]}/{files[0]}" if files else None


def build(mock, token, user) -> dict:
    point_google_at(mock.base_url)  # Google has no host config; Slack takes base_url below
    client_id, client_secret, refresh_token, _ = google_oauth_user(mock.base_url, user)
    google = dict(client_id=client_id, client_secret=client_secret, refresh_token=refresh_token)
    return {  # three backends, one filesystem
        "/slack": SlackResource(SlackConfig(token=token, base_url=slack_base_url(mock.base_url))),
        "/gmail": GmailResource(GmailConfig(**google)),
        "/gdrive": GoogleDriveResource(GoogleDriveConfig(**google)),
    }


async def main(resources: dict) -> None:
    ws = Workspace(resources, mode=MountMode.READ)

    print("=== ls -F / (all backends mounted side by side) ===")
    print((await (await ws.execute("ls -F /")).stdout_str()).rstrip())

    # Read one file from each backend with the SAME commands — the point of mirage. We navigate
    # top-down (bounded) instead of grepping whole mounts, so this stays fast on a big corpus.
    for backend, path in (
        ("slack", await _first_slack_chat(ws)),
        ("gmail", await _first_gmail_msg(ws)),
        ("gdrive", await _first_drive_file(ws)),
    ):
        print(f"\n=== [{backend}] cat {path or '(nothing visible)'} ===")
        if not path:
            continue
        head = (await (await ws.execute(f'cat "{path}"')).stdout_str()).strip().splitlines()
        print("  " + (head[0][:160] if head else "(empty)"))


def main_fuse(resources: dict) -> None:
    """--fuse: mount all three backends under ONE filesystem root (so slack/, gmail/, gdrive/
    are subdirectories — a single mountpoint, which works on macOS too, since macFUSE limits the
    number of mountpoints per process, not the number of sources behind one), then run a single
    real command across them. Needs macFUSE/fuse3.

    ``grep -r`` walks the whole mount, so it's meant for the small default corpus; against a
    large ``--url`` use the per-provider scripts (their bounded navigation)."""
    try:
        with Workspace(resources, mode=MountMode.READ) as ws:
            mnt = ws.add_fuse_mount("/")  # the workspace root is now one real directory
            backends = [p.strip("/") for p in resources]
            print(f"=== mounted at {mnt}: {', '.join(backends)} — one filesystem ===")
            print(f"\n$ grep -rl Q1 {' '.join(backends)}   # one command, all three backends")
            hits = subprocess.run(
                ["grep", "-rl", "Q1", *(f"{mnt}/{b}" for b in backends)],
                capture_output=True,
                text=True,
            )
            seen = set()  # one hit per backend (a message can appear under many labels)
            for h in hits.stdout.splitlines():
                rel = os.path.relpath(h, mnt)
                if rel.split("/")[0] not in seen:
                    seen.add(rel.split("/")[0])
                    print(f"  [{rel.split('/')[0]}] {rel}")
            print(f"\nexplore it live in another terminal:  ls {mnt}")
    except (ImportError, RuntimeError, OSError) as e:
        raise SystemExit(FUSE_HELP.format(err=e))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Read Slack + Gmail + Drive through mirage against the mock."
    )
    p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
    p.add_argument(
        "--token", help="Slack: mock bearer token from GET /_mock/users (default: admin)"
    )
    p.add_argument(
        "--user",
        help="Gmail/Drive: which user's Google OAuth token to use (default: the first user)",
    )
    p.add_argument(
        "--fuse", action="store_true", help="mount as a real FUSE filesystem (needs macFUSE/fuse3)"
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        resources = build(mock, args.token or mock.token, args.user)
        if args.fuse:
            main_fuse(resources)
        else:
            run_mirage(main(resources))
