#!/usr/bin/env python3
"""Read Notion through mirage's virtual filesystem. Self-contained: run it directly.

Mirage mounts the mock's Notion API as a filesystem — ``pages/`` and ``databases/`` at the root,
each entry a directory with a ``page.json`` / ``database.json`` — so an agent reads it with plain
``ls`` / ``cat``. Notion's API host is a config knob (``NotionConfig(base_url=...)``), so we point
it straight at the mock — no monkeypatch (unlike Google). mirage sends ``Notion-Version:
2022-06-28``, which the mock's version-aware router serves.

    pip install -e ".[examples,mirage]"
    python examples/using-mirage/notion.py                                  # local throwaway mock
    python examples/using-mirage/notion.py --url http://localhost:8000
    python examples/using-mirage/notion.py --url http://localhost:8000 --token <usr-token>
    python examples/using-mirage/notion.py --url http://localhost:8000 --fuse   # real OS mount

With ``--fuse`` the tree is exposed as an actual filesystem (needs macFUSE/fuse3) and read with
plain ``os``/shell tools; otherwise it's driven in-process via ``ws.execute``.
"""

import argparse
import os
import subprocess

from mirage import MountMode, Workspace
from mirage.resource.notion import NotionConfig, NotionResource

from backlot import serve_or_connect
from backlot.integrations.mirage import notion_base_url

from _helpers import FUSE_HELP, lines, run_mirage

CORPUS = [
    {
        "source_type": "notion",
        "teamspace": "engineering",
        "title": "On-call Runbook",
        "content": "# On-call\n\nCheck dashboards, roll back, page on-call.",
        "comments": [{"content": "add the rate-limiter rollback step"}],
    },
    {
        "source_type": "notion",
        "teamspace": "engineering",
        "subtype": "database",
        "title": "Eng Tasks",
        "content": "Team task tracker.",
        "doc_id": "eng-tasks-db",
        "properties": {"Status": {"type": "select"}, "Priority": {"type": "select"}},
    },
    {
        "source_type": "notion",
        "teamspace": "engineering",
        "title": "Fix gateway 502s",
        "content": "Investigate token-bucket refill.",
        "parent": "eng-tasks-db",
        "properties": {"Status": "In Progress", "Priority": "High"},
    },
]


def build(mock, token):
    # Notion's host is a config knob — point it at the mock (no monkeypatch needed).
    # --token <usr-token> (from /_mock/users) → ACL-filtered to that user; else admin sees all.
    return NotionResource(NotionConfig(api_key=token, base_url=notion_base_url(mock.base_url)))


async def main(resource) -> None:
    ws = Workspace({"/notion": resource}, mode=MountMode.READ)

    print("=== ls /notion/ ===")
    print((await (await ws.execute("ls /notion/")).stdout_str()).rstrip())

    pages = lines(await (await ws.execute("ls /notion/pages/")).stdout_str())
    print(f"\n=== {len(pages)} page(s) ===")
    for p in pages:
        print(f"  {p}")
    if pages:
        page_json = f"/notion/pages/{pages[0].rstrip('/')}/page.json"
        print(f"\n$ cat {page_json}")
        print((await (await ws.execute(f'cat "{page_json}"')).stdout_str()).rstrip()[:600])

    dbs = lines(await (await ws.execute("ls /notion/databases/")).stdout_str())
    print(f"\n=== {len(dbs)} database(s) ===")
    for d in dbs:
        print(f"  {d}")
    if dbs:
        db_json = f"/notion/databases/{dbs[0].rstrip('/')}/database.json"
        print(f"\n$ cat {db_json}")
        print((await (await ws.execute(f'cat "{db_json}"')).stdout_str()).rstrip()[:600])


def main_fuse(resource) -> None:
    """--fuse: mount the Notion tree as a *real* filesystem, then read it with ordinary tools."""
    try:
        with Workspace({"/notion": resource}, mode=MountMode.READ) as ws:
            mnt = ws.add_fuse_mount("/notion")  # "/notion" is now a real directory on disk
            print(f"=== mounted at {mnt} — an ordinary filesystem now ===")
            pages = sorted(os.listdir(f"{mnt}/pages"))
            if pages:
                page_json = f"{mnt}/pages/{pages[0]}/page.json"
                print(f"\n$ head -c 200 pages/{pages[0]}/page.json")
                print(
                    "  " + open(page_json).read(200).replace("\n", " ")
                )  # a genuine open() via FUSE
                count = subprocess.run(
                    ["grep", "-c", ".", page_json], capture_output=True, text=True
                )
                print(
                    f"\n$ grep -c . <that file>   # a separate process reads the mount → {count.stdout.strip()}"
                )
            print(f"\nexplore it live in another terminal:  ls {mnt}/pages {mnt}/databases")
    except (ImportError, RuntimeError, OSError) as e:
        raise SystemExit(FUSE_HELP.format(err=e))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read Notion through mirage against the mock.")
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
