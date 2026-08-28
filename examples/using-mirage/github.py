#!/usr/bin/env python3
"""Read a GitHub repo's code through mirage's virtual filesystem. Self-contained: run it directly.

Mirage mounts a repo's git file tree as a filesystem — read it with plain ``ls`` / ``cat`` /
``grep``, same as the S3/Notion examples. Unlike Slack/Notion/S3, ``GitHubConfig`` (mirage 0.0.3)
has no ``base_url`` knob: the connector hardcodes ``mirage.core.github._client.API_BASE =
"https://api.github.com"``, so ``point_github_at`` monkeypatches that module constant before the
resource is built (mirrors ``point_google_at``'s approach for Google — see
``backlot.integrations.mirage``).

mirage's GitHub connector only mirrors the *file tree* (git ``trees``/``blobs``), not issues/PRs —
use `examples/using-official-sdk/github.py` for those.

    pip install -e ".[examples,mirage]"
    python examples/using-mirage/github.py                                  # local throwaway mock
    python examples/using-mirage/github.py --url http://localhost:8000
    python examples/using-mirage/github.py --url http://localhost:8000 --token <usr-token>
    python examples/using-mirage/github.py --url http://localhost:8000 --fuse   # real OS mount

With ``--fuse`` the tree is exposed as an actual filesystem (needs macFUSE/fuse3) and read with
plain ``os``/shell tools; otherwise it's driven in-process via ``ws.execute``.
"""

import argparse
import json
import os
import subprocess
import urllib.request

from mirage import MountMode, Workspace
from mirage.resource.github import GitHubConfig, GitHubResource

from backlot import serve_or_connect
from backlot.integrations.mirage import point_github_at

from _helpers import FUSE_HELP, lines, run_mirage

REPO = "gateway"  # the throwaway CORPUS's repo; a --url mock's own repos are discovered below
CORPUS = [
    {
        "state": "open",
        "author_email": "bob@acme.com",
        "created": "2026-02-09T09:00:00Z",
        "source_type": "github",
        "repo": REPO,
        "title": "Rate limiter drops bursts under 50ms",
        "content": "The token-bucket refill is off by one tick.",
        "subtype": "issue",
    },
    {
        "author_email": "bob@acme.com",
        "created": "2026-01-20T09:00:00Z",
        "source_type": "github",
        "repo": REPO,
        "subtype": "file",
        "path": "README.md",
        "title": "README.md",
        "content": "# gateway\n\nToken-bucket rate limiter for inbound requests.\n",
    },
    {
        "author_email": "bob@acme.com",
        "created": "2026-01-20T09:05:00Z",
        "source_type": "github",
        "repo": REPO,
        "subtype": "file",
        "path": "src/ratelimiter.py",
        "title": "ratelimiter.py",
        "content": "class TokenBucket:\n"
        "    def __init__(self, rate, burst):\n"
        "        self.rate = rate\n"
        "        self.tokens = burst\n\n"
        "    def refill(self, elapsed):\n"
        "        # BUG: off-by-one tick drops the last burst token\n"
        "        self.tokens = min(self.tokens + elapsed * self.rate, self.tokens)\n",
    },
    {
        "author_email": "bob@acme.com",
        "created": "2026-02-10T12:00:00Z",
        "source_type": "github",
        "repo": REPO,
        "subtype": "file",
        "path": "src/utils/tokens.py",
        "title": "tokens.py",
        "content": "def clamp(value, low, high):\n    return max(low, min(value, high))\n",
    },
]


def _api_get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def discover_repo(base_url: str, token: str) -> tuple[str, str] | None:
    """Find a repo the token can reach that actually has files, instead of assuming the throwaway
    CORPUS's own ``REPO`` exists — against a ``--url`` serving a different corpus,
    ``GitHubResource.__init__`` 404s immediately (it eagerly fetches the repo + its tree).

    Goes through ``GET /user/repos`` — the CREDENTIAL's own view of what it can reach — so nothing
    here has to guess the owner: the mock derives its org from the corpus's email domain and 404s
    any other owner, exactly as real GitHub does, and each entry's ``full_name`` already carries
    the right ``owner/repo`` pair.

    Returns the first repo whose recursive git tree has at least one ``blob`` entry (a repo of only
    issues/PRs would give the ls/cat walk below nothing to read), falling back to the first visible
    repo, or ``None`` if the token can reach none at all (or the mock can't be reached).
    """
    base = f"{base_url.rstrip('/')}/github"
    try:
        repos = _api_get(f"{base}/user/repos?per_page=100", token)
    except Exception:  # noqa: BLE001 — any failure just means "discovery found nothing"
        return None
    pairs = [
        tuple(r["full_name"].split("/", 1)) for r in repos if "/" in (r.get("full_name") or "")
    ]
    for owner, name in pairs:
        try:
            tree = _api_get(f"{base}/repos/{owner}/{name}/git/trees/main?recursive=1", token)
        except Exception:  # noqa: BLE001
            continue
        if any(entry.get("type") == "blob" for entry in tree.get("tree", [])):
            return owner, name
    return pairs[0] if pairs else None


def build(mock, token, owner, repo):
    # GitHubConfig has no base_url field — redirect the hardcoded API_BASE constant first, then
    # construct the resource (its __init__ makes synchronous HTTP calls to fetch the default
    # branch and the recursive tree).
    point_github_at(mock.base_url)
    return GitHubResource(GitHubConfig(token=token, owner=owner, repo=repo, ref="main"))


async def main(resource) -> None:
    ws = Workspace({"/github": resource}, mode=MountMode.READ)

    print("=== ls /github/ ===")
    print((await (await ws.execute("ls /github/")).stdout_str()).rstrip())

    # Don't assume a fixed layout (e.g. "src/") — a discovered repo's file tree is unknown ahead
    # of time, so `find` the actual files and read whichever one turns up.
    files = lines(await (await ws.execute("find /github/ -type f")).stdout_str())
    print(f"\n=== find /github/ -type f  ({len(files)} file(s)) ===")
    for f in files[:5]:
        print(f)

    if files:
        cat_path = files[0]
        print(f"\n$ cat {cat_path}")
        print((await (await ws.execute(f'cat "{cat_path}"')).stdout_str()).rstrip()[:600])

    print("\n$ grep -r BUG /github/")
    print((await (await ws.execute("grep -r BUG /github/")).stdout_str()).rstrip())


def main_fuse(resource) -> None:
    """--fuse: mount the repo tree as a *real* filesystem, then read it with ordinary tools."""
    try:
        with Workspace({"/github": resource}, mode=MountMode.READ) as ws:
            mnt = ws.add_fuse_mount("/github")  # "/github" is now a real directory on disk
            print(f"=== mounted at {mnt} — an ordinary filesystem now ===")
            first_file = None
            for root, _dirs, fnames in os.walk(mnt):
                if fnames:
                    first_file = os.path.join(root, fnames[0])
                    break
            if first_file:
                rel = os.path.relpath(first_file, mnt)
                print(f"\n$ head -c 200 {rel}")
                print(
                    "  " + open(first_file).read(200).replace("\n", " ")
                )  # a genuine open() via FUSE
            count = subprocess.run(["grep", "-rc", "BUG", mnt], capture_output=True, text=True)
            print(
                f"\n$ grep -rc BUG {mnt}   # a separate process reads the mount → {count.stdout.strip()}"
            )
            print(f"\nexplore it live in another terminal:  ls -R {mnt}")
    except (ImportError, RuntimeError, OSError) as e:
        raise SystemExit(FUSE_HELP.format(err=e))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Read a GitHub repo's code through mirage against the mock."
    )
    p.add_argument("--url", help="mock base URL to drive (default: spin up a local throwaway mock)")
    p.add_argument(
        "--token",
        help="mock bearer token from GET /_meta/users "
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
        token = args.token or mock.token

        # Discover a repo that actually has files rather than assuming this script's own CORPUS
        # repo exists on whatever's behind --url. The owner comes from the mock too — it 404s a
        # wrong one, so there is nothing to hardcode.
        discovered = discover_repo(mock.base_url, token)
        if discovered is None:
            raise SystemExit("no repositories visible to this token — nothing to mount")
        owner, repo = discovered
        print(f"mounting {owner}/{repo}")

        resource = build(mock, token, owner, repo)
        if args.fuse:
            main_fuse(resource)
        else:
            run_mirage(main(resource))
