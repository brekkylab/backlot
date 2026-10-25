#!/usr/bin/env python3
"""Read Linear through mirage's virtual filesystem. Self-contained: run it directly.

Mirage mounts Backlot's Linear GraphQL API as a filesystem — a directory per team holding
``team.json`` (its workflow states included), ``issues/`` with an ``issue.json`` and a
``comments.jsonl`` per issue, and ``members/``, ``projects/``, ``cycles/`` and ``documents/`` — so
an agent reads it with plain ``ls`` / ``cat`` / ``jq``. Linear's endpoint is a config knob
(``LinearConfig(base_url=...)``), so we point it straight at Backlot — no monkeypatch. mirage sends
the key bare in ``Authorization``, as Linear takes a personal API key, and Backlot reads it the
same way.

    uv sync --all-extras --locked
    python examples/using-mirage/linear.py                                  # local throwaway server
    python examples/using-mirage/linear.py --url http://localhost:8000
    python examples/using-mirage/linear.py --url http://localhost:8000 --token <usr-token>
    python examples/using-mirage/linear.py --url http://localhost:8000 --fuse   # real OS mount

With ``--fuse`` the team tree is exposed as an actual filesystem (needs macFUSE/fuse3) and read
with plain ``os``/shell tools; otherwise it's driven in-process via ``ws.shell``.
"""

import argparse
import os
import subprocess

from _helpers import FUSE_HELP, lines, run_mirage
from mirage import MountMode, Workspace
from mirage.vfs.linear import LinearConfig, LinearVFS

from backlot import serve_or_connect

CORPUS = [
    {
        "source_type": "linear",
        "doc_id": "lin-kv",
        "team": "engineering",
        "group": "engineering",
        "title": "Variant-aware GPU allocation and KV residency",
        "content": "Long-context configs push peak GPU memory into fragile regions.",
        "author_email": "amaya.chen@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "identifier": "ENG-49121",
        "state": "In Progress",
        "priority": "P1",
        "labels": ["kv-cache", "memory-optimization"],
        "project": "runtime-memory-2025",
        "cycle": "2025-W08",
        "assignee": "diego.martinez@acme.com",
        "assigneeName": "Diego Martinez",
        "created": "2025-02-18T00:00:00Z",
        "updated": "2025-03-04T00:00:00Z",
        "comments": [
            {
                "content": "Baseline traces captured; peak 98.2 GB on the quantized variant.",
                "author_email": "amaya.chen@acme.com",
                "created_ts": "2025-02-19T09:00:00Z",
            },
            {
                "content": "Residency bands cut peak memory 20%.",
                "author_email": "diego.martinez@acme.com",
                "created_ts": "2025-02-25T14:00:00Z",
            },
        ],
    },
    {
        "source_type": "linear",
        "doc_id": "lin-pager",
        "team": "operations",
        "group": "operations",
        "title": "Pager rotation skips the weekend handoff",
        "content": "Saturday's shift starts with nobody paged when the rotation crosses midnight UTC.",
        "author_email": "bob@acme.com",
        "author_groups": ["operations"],
        "visibility": "public",
        "identifier": "OPS-311",
        "state": "Todo",
        "priority": "P2",
        "labels": ["on-call"],
        "created": "2025-03-05T00:00:00Z",
    },
]


def build(s, token):
    # Linear's endpoint is a config knob — point it at Backlot (no monkeypatch needed).
    # --token <usr-token> (from /_meta/users) → ACL-filtered to that user; else admin sees all.
    return LinearVFS(LinearConfig(api_key=token, base_url=f"{s.base_url}/linear/graphql"))


async def main(vfs) -> None:
    ws = Workspace({"/linear": vfs}, mode=MountMode.READ)

    teams = lines(await (await ws.shell("ls /linear/teams/")).stdout_str())
    print("=== ls /linear/teams/ ===")
    print("\n".join(teams) or "no teams visible to this identity")
    if not teams:
        return

    team = f"/linear/teams/{teams[0]}"
    print(f"\n=== ls {team}/ ===")
    print((await (await ws.shell(f'ls "{team}/"')).stdout_str()).rstrip())

    print("\n=== the team's workflow states, from team.json ===")
    r = await ws.shell(f'jq -r ".states[].state_name" "{team}/team.json"')
    print((await r.stdout_str()).rstrip())

    issues = lines(await (await ws.shell(f'ls "{team}/issues/"')).stdout_str())
    if not issues:
        print("\n  team has no issues visible to this identity")
        return
    issue = f"{team}/issues/{issues[0]}"
    print(f"\n$ cat {issue}/issue.json")
    print((await (await ws.shell(f'cat "{issue}/issue.json"')).stdout_str()).rstrip()[:600])

    # comments.jsonl is one comment per line, so jq reads it a comment at a time.
    print(f"\n$ jq -r '.user_name + \": \" + .body' {issue}/comments.jsonl")
    r = await ws.shell(f'jq -r \'.user_name + ": " + .body\' "{issue}/comments.jsonl"')
    print("  " + ((await r.stdout_str()).rstrip().replace("\n", "\n  ") or "(no comments)"))


def main_fuse(vfs) -> None:
    """--fuse: mount the team tree as a *real* filesystem, then read it with ordinary tools —
    any process (grep, an editor, an indexer) can open the files. Needs macFUSE/fuse3."""
    try:
        with Workspace({"/linear": vfs}, mode=MountMode.READ) as ws:
            mnt = ws.add_fuse_mount("/linear")  # "/linear" is now a real directory on disk
            print(f"=== mounted at {mnt} — an ordinary filesystem now ===")
            team = sorted(os.listdir(f"{mnt}/teams"))[0]
            team_json = f"{mnt}/teams/{team}/team.json"
            print(f"\n$ head -c 200 teams/{team}/team.json")
            print(
                "  " + open(team_json, encoding="utf-8").read(200).replace("\n", " ")
            )  # a genuine open() via FUSE
            count = subprocess.run(
                ["grep", "-c", "state_name", team_json],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            print(
                f"\n$ grep -c state_name <that file>   # a separate process reads the mount → {count.stdout.strip()}"
            )
            print(f"\nexplore it live in another terminal:  ls {mnt}/teams")
    except (ImportError, RuntimeError, OSError) as e:
        raise SystemExit(FUSE_HELP.format(err=e))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read Linear through mirage against Backlot.")
    p.add_argument(
        "--url", help="Backlot base URL to drive (default: spin up a local throwaway server)"
    )
    p.add_argument(
        "--token",
        help="Backlot bearer token from GET /_meta/users "
        "(default: the admin token, which sees everything)",
    )
    p.add_argument(
        "--fuse", action="store_true", help="mount as a real FUSE filesystem (needs macFUSE/fuse3)"
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as s:
        if args.token:
            print("authenticating with --token → responses are ACL-filtered to that user")
        vfs = build(s, args.token or s.token)
        if args.fuse:
            main_fuse(vfs)
        else:
            run_mirage(main(vfs))
