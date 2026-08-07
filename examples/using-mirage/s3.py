#!/usr/bin/env python3
"""Read S3 through mirage's virtual filesystem. Self-contained: run it directly.

Mirage mounts an S3 bucket as a filesystem under ``/s3`` — read it with plain ``ls`` / ``cat`` /
``grep``. S3's endpoint is a config knob (``S3Config(endpoint_url=..., path_style=True)``), so we
point it straight at the mock — no monkeypatch (unlike Google). mirage/aioboto3 SigV4-signs every
request. S3 uses an AWS access-key/secret pair (not a bearer token): with ``--url`` (a running
server) ``--access-key``/``--secret-key`` are **required** — pass real AWS keys, or a pair from
``GET <url>/_mock/users`` (each user, and the admin, has an ``s3_access_key_id`` /
``s3_secret_access_key`` there). Without ``--url`` the local throwaway mock uses its own admin
keypair.

    pip install -e ".[examples,mirage]"
    python examples/using-mirage/s3.py                              # local throwaway mock
    python examples/using-mirage/s3.py --url http://localhost:8000 --access-key <AKIA...> --secret-key <secret>
    python examples/using-mirage/s3.py --url http://localhost:8000 --access-key <AKIA...> --secret-key <secret> --fuse   # real OS mount

With ``--fuse`` the tree is exposed as an actual filesystem (needs macFUSE/fuse3) and read with
plain ``os``/shell tools; otherwise it's driven in-process via ``ws.execute``.
"""

import argparse
import json
import os
import subprocess
import urllib.request

from mirage import MountMode, Workspace
from mirage.resource.s3 import S3Config, S3Resource

from backlot import serve_or_connect
from backlot.integrations.mirage import s3_base_url

from _helpers import FUSE_HELP, lines, run_mirage

BUCKET = "eng-artifacts"
CORPUS = [
    {
        "source_type": "s3",
        "bucket": BUCKET,
        "key": "runbooks/oncall.md",
        "title": "On-call Runbook",
        "content": "# On-call\nCheck dashboards, roll back, page on-call.",
        "content_type": "text/markdown",
    },
    {
        "source_type": "s3",
        "bucket": BUCKET,
        "key": "design/architecture.md",
        "title": "Architecture",
        "content": "Gateway, workers, and the token bucket.",
        "content_type": "text/markdown",
    },
]


def build(mock, access_key, secret_key):
    return S3Resource(
        S3Config(
            bucket=BUCKET,
            endpoint_url=s3_base_url(mock.base_url),
            path_style=True,
            region="us-east-1",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
    )


async def main(resource) -> None:
    ws = Workspace({"/s3": resource}, mode=MountMode.READ)
    print("=== ls /s3/ (recursive) ===")
    print((await (await ws.execute("ls /s3/")).stdout_str()).rstrip())
    for entry in lines(await (await ws.execute("ls /s3/runbooks/")).stdout_str()):
        path = f"/s3/runbooks/{entry.rstrip('/')}"
        print(f"\n$ cat {path}")
        print((await (await ws.execute(f'cat "{path}"')).stdout_str()).rstrip()[:400])
    print("\n$ grep -r dashboards /s3/")
    print((await (await ws.execute("grep -r dashboards /s3/")).stdout_str()).rstrip())


def main_fuse(resource) -> None:
    """--fuse: mount the S3 bucket as a *real* filesystem, then read it with ordinary tools."""
    try:
        with Workspace({"/s3": resource}, mode=MountMode.READ) as ws:
            mnt = ws.add_fuse_mount("/s3")  # "/s3" is now a real directory on disk
            print(f"=== mounted at {mnt} — an ordinary filesystem now ===")
            objs = [os.path.join(r, f) for r, _dirs, files in os.walk(mnt) for f in files]
            for p in objs:
                print("  " + p.replace(mnt, "/s3"))
            if objs:
                # a genuinely separate process reads the FUSE mount like any real directory
                hit = subprocess.run(
                    ["grep", "-rc", "dashboards", mnt], capture_output=True, text=True
                )
                print(
                    f"\n$ grep -rc dashboards {mnt}   # a separate process reads the mount → "
                    f"{hit.stdout.strip()}"
                )
            print(f"\nexplore it live in another terminal:  ls -R {mnt}")
    except (ImportError, RuntimeError, OSError) as e:
        raise SystemExit(FUSE_HELP.format(err=e))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read S3 through mirage against the mock's S3.")
    p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
    p.add_argument(
        "--access-key",
        help="AWS access key id (S3 uses a keypair, not a token); "
        "required with --url — from GET <url>/_mock/users, or real AWS",
    )
    p.add_argument("--secret-key", help="AWS secret access key (required with --url)")
    p.add_argument(
        "--fuse", action="store_true", help="mount as a real FUSE filesystem (needs macFUSE/fuse3)"
    )
    args = p.parse_args()
    if args.url and not (args.access_key and args.secret_key):
        p.error(
            "--access-key and --secret-key are required with --url (grab a pair from GET <url>/_mock/users)"
        )
    return args


def _admin_keys(base_url: str) -> tuple[str, str]:
    """The local throwaway mock's admin S3 keypair, read from its /_mock/users."""
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/_mock/users") as r:
        data = json.load(r)
    return data["admin_s3_access_key_id"], data["admin_s3_secret_access_key"]


if __name__ == "__main__":
    args = _parse_args()
    with serve_or_connect(CORPUS, url=args.url) as mock:
        ak, sk = (args.access_key, args.secret_key) if args.url else _admin_keys(mock.base_url)
        resource = build(mock, ak, sk)
        if args.fuse:
            main_fuse(resource)
        else:
            run_mirage(main(resource))
