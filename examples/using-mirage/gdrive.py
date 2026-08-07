#!/usr/bin/env python3
"""Read Google Drive through mirage's virtual filesystem. Self-contained: run it directly.

Mirage mounts the mock's Drive API as a filesystem — folders and files you read with plain
``ls`` / ``cat`` (Google-native docs are exported to text on read). Auth is an ordinary Google
authorized-user credential; the only mirage-specific glue is ``point_google_at`` (mirage's
Google connectors have no host config, so we patch the module constants at the mock).

    pip install -e ".[examples,mirage]"
    python examples/using-mirage/gdrive.py                                 # first user, locally
    python examples/using-mirage/gdrive.py --url http://localhost:8000 --user mia@acme.com
    python examples/using-mirage/gdrive.py --url http://localhost:8000 --fuse   # real OS mount

With ``--fuse`` the Drive tree is exposed as an actual filesystem (needs macFUSE/fuse3) and read
with plain ``os``/shell tools; otherwise it's driven in-process via ``ws.execute``.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from mirage import MountMode, Workspace
from mirage.resource.gdrive import GoogleDriveConfig, GoogleDriveResource

from backlot import serve_or_connect
from backlot.integrations.mirage import point_google_at

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _common.google_creds import google_oauth_user

from _helpers import FUSE_HELP, lines, run_mirage

CORPUS = [
    {
        "source_type": "google_drive",
        "folder": "marketing",
        "title": "Brand guidelines v3",
        "content": "Logo usage, color palette, typography.",
        "subtype": "document",
        "author_email": "mia@acme.com",
    },
    {
        "source_type": "google_drive",
        "folder": "finance",
        "title": "Q1 Revenue Model",
        "content": "month,revenue\nJan,120000\nFeb,135000",
        "subtype": "spreadsheet",
        "author_email": "cfo@acme.com",
    },
]


def build(mock, user):
    point_google_at(mock.base_url)
    client_id, client_secret, refresh_token, _ = google_oauth_user(mock.base_url, user)
    return GoogleDriveResource(
        GoogleDriveConfig(
            client_id=client_id, client_secret=client_secret, refresh_token=refresh_token
        )
    )


async def main(resource) -> None:
    ws = Workspace({"/gdrive": resource}, mode=MountMode.READ)

    # Navigate top-down (bounded) rather than walking the whole tree — a corpus can be huge.
    print("=== ls -F /gdrive/ (folders marked with /) ===")
    root = lines(await (await ws.execute("ls -F /gdrive/")).stdout_str())
    print("\n".join(root) or "(empty)")
    folders = [e.rstrip("/") for e in root if e.endswith("/")]
    if not folders:
        print("\nno folders visible to this identity")
        return

    folder = folders[0]
    print(f"\n=== ls -F /gdrive/{folder}/ ===")
    entries = lines(await (await ws.execute(f'ls -F "/gdrive/{folder}/"')).stdout_str())
    print("\n".join(entries[:10]) or "(empty)")
    files = [e for e in entries if not e.endswith("/")]
    if not files:
        print("\n(folder holds only subfolders — descend further to reach files)")
        return

    # Google-native docs get a .gdoc/.gsheet/.gslide.json vfs name and are read structurally.
    path = f"/gdrive/{folder}/{files[0]}"
    print(f"\n=== cat {path} ===")
    print((await (await ws.execute(f'cat "{path}"')).stdout_str()).rstrip())


def main_fuse(resource) -> None:
    """--fuse: mount Drive as a *real* filesystem and read a file with ordinary tools. Needs
    macFUSE/fuse3. (Google-native docs read as their .gdoc/.gsheet/.gslide.json structure.)"""
    try:
        with Workspace({"/gdrive": resource}, mode=MountMode.READ) as ws:
            mnt = ws.add_fuse_mount("/gdrive")  # "/gdrive" is now a real directory on disk
            print(f"=== mounted at {mnt} — an ordinary filesystem now ===")
            folder = next(e for e in sorted(os.listdir(mnt)) if not e.startswith("."))
            name = next(
                e
                for e in sorted(os.listdir(f"{mnt}/{folder}"))
                if os.path.isfile(f"{mnt}/{folder}/{e}")
            )
            path = f"{mnt}/{folder}/{name}"
            print(f"\n$ head -c 200 {folder}/{name}")
            print("  " + open(path).read(200).replace("\n", " "))  # a genuine open() via FUSE
            count = subprocess.run(["grep", "-c", ".", path], capture_output=True, text=True)
            print(
                f"\n$ grep -c . <that file>   # a separate process reads the mount → {count.stdout.strip()}"
            )
            print(f"\nexplore it live in another terminal:  ls {mnt}/{folder}")
    except (ImportError, RuntimeError, OSError) as e:
        raise SystemExit(FUSE_HELP.format(err=e))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read Google Drive through mirage against the mock.")
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
