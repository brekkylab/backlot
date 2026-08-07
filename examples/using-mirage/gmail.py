#!/usr/bin/env python3
"""Read Gmail through mirage's virtual filesystem. Self-contained: run it directly.

Mirage mounts the mock's Gmail API as a filesystem — a directory per label, then per day, then
a file per message — so an agent reads mail with plain ``ls`` / ``cat``. Auth is an ordinary
Google authorized-user credential (client_id/secret + refresh token); the only mirage-specific
glue is ``point_google_at`` (mirage's Google connectors have no host config, so we patch the
module constants; the mock's ``/oauth2/token`` honors the refresh).

    pip install -e ".[examples,mirage]"
    python examples/using-mirage/gmail.py                                  # first user, locally
    python examples/using-mirage/gmail.py --url http://localhost:8000 --user ceo@acme.com
    python examples/using-mirage/gmail.py --url http://localhost:8000 --fuse   # real OS mount

With ``--fuse`` the mailbox is exposed as an actual filesystem (needs macFUSE/fuse3) and read
with plain ``os``/shell tools; otherwise it's driven in-process via ``ws.execute``.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from mirage import MountMode, Workspace
from mirage.resource.gmail import GmailConfig, GmailResource

from backlot import serve_or_connect
from backlot.integrations.mirage import point_google_at

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _common.google_creds import google_oauth_user

from _helpers import FUSE_HELP, lines, run_mirage

CORPUS = [
    {
        "source_type": "gmail",
        "mailbox": "ceo",
        "title": "Q1 board deck draft",
        "content": "Draft narrative for the Q1 board meeting. Please review before Thursday.",
        "author_email": "ceo@acme.com",
    },
    {
        "source_type": "gmail",
        "mailbox": "ceo",
        "title": "Re: Q1 board deck draft",
        "content": "Looks good — one tweak to the revenue slide.",
        "author_email": "ava@acme.com",
    },
]


def build(mock, user):
    point_google_at(mock.base_url)
    client_id, client_secret, refresh_token, _ = google_oauth_user(mock.base_url, user)
    return GmailResource(
        GmailConfig(client_id=client_id, client_secret=client_secret, refresh_token=refresh_token)
    )


async def main(resource) -> None:
    ws = Workspace({"/gmail": resource}, mode=MountMode.READ)

    print("=== ls /gmail/ (labels) ===")
    labels = lines(await (await ws.execute("ls /gmail/")).stdout_str())
    print("\n".join(labels) or "(none)")

    # Tree is /gmail/<label>/<date>/<message>.gmail.json. Descend one label → one day → one
    # message (bounded), rather than walking every label.
    for label in (["INBOX"] if "INBOX" in labels else []) + labels:
        dates = lines(await (await ws.execute(f'ls "/gmail/{label}/"')).stdout_str())
        if not dates:
            continue
        day = dates[0].rstrip("/")
        files = [
            f
            for f in lines(await (await ws.execute(f'ls "/gmail/{label}/{day}/"')).stdout_str())
            if f.endswith(".gmail.json")
        ]
        if not files:
            continue
        msg = f"/gmail/{label}/{day}/{files[0]}"
        print(f"\n=== cat {msg} ===")
        print((await (await ws.execute(f'cat "{msg}"')).stdout_str()).rstrip()[:600])
        print("\n=== jq .subject ===")
        print((await (await ws.execute(f'jq ".subject" "{msg}"')).stdout_str()).rstrip())
        return
    print("\nno messages visible to this identity")


def main_fuse(resource) -> None:
    """--fuse: mount the mailbox as a *real* filesystem and read a message with ordinary tools.
    Needs macFUSE/fuse3. (Listing a label makes mirage fetch every message in it — see the
    README's performance notes; keep the mailbox small when pointing at a large corpus.)"""
    try:
        with Workspace({"/gmail": resource}, mode=MountMode.READ) as ws:
            mnt = ws.add_fuse_mount("/gmail")  # "/gmail" is now a real directory on disk
            print(f"=== mounted at {mnt} — an ordinary filesystem now ===")
            label = (
                "INBOX"
                if "INBOX" in os.listdir(mnt)
                else next(e for e in sorted(os.listdir(mnt)) if not e.startswith("."))
            )
            day = sorted(os.listdir(f"{mnt}/{label}"))[0]
            msg = next(
                f for f in sorted(os.listdir(f"{mnt}/{label}/{day}")) if f.endswith(".gmail.json")
            )
            path = f"{mnt}/{label}/{day}/{msg}"
            print(f"\n$ head -c 200 {label}/{day}/{msg}")
            print("  " + open(path).read(200).replace("\n", " "))  # a genuine open() via FUSE
            hit = subprocess.run(
                ["grep", "-o", '"subject":"[^"]*"', path], capture_output=True, text=True
            )
            print(
                f"\n$ grep -o '\"subject\":…' <that file>   # external tool reads the mount\n  {hit.stdout.strip()}"
            )
            print(f"\nexplore it live in another terminal:  ls {mnt}/{label}")
    except (ImportError, RuntimeError, OSError) as e:
        raise SystemExit(FUSE_HELP.format(err=e))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read Gmail through mirage against the mock.")
    p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
    p.add_argument(
        "--user",
        help="which user's OAuth token to use, from GET /_mock/users (default: the first user)",
    )
    p.add_argument(
        "--fuse", action="store_true", help="mount as a real FUSE filesystem (needs macFUSE/fuse3)"
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        resource = build(mock, args.user)
        if args.fuse:
            main_fuse(resource)
        else:
            run_mirage(main(resource))
