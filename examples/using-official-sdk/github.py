#!/usr/bin/env python3
"""Read GitHub through the official PyGithub. Self-contained: run it directly.

Lists a repo's issues/PRs, then crawls its code: the git tree (`get_git_tree(...,
recursive=True)`), a file read via `get_contents`, and the README via `get_readme`.

    pip install -e ".[examples]"
    python examples/using-official-sdk/github.py            # or: --url http://localhost:8000
    python examples/using-official-sdk/github.py --url http://localhost:8000 --token <usr-token>
"""

import argparse
import sys
from pathlib import Path

from backlot import serve_or_connect

# This file is named github.py, so its own directory would shadow PyGithub's `github`
# package. Drop that directory now that the local helper is imported.
_here = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != Path(_here)]

from github import Auth, Github  # noqa: E402

CORPUS = [
    {
        "source_type": "github",
        "repo": "gateway",
        "title": "Rate limiter drops bursts under 50ms",
        "content": "The token-bucket refill is off by one tick.",
        "subtype": "issue",
    },
    {
        "source_type": "github",
        "repo": "gateway",
        "title": "Fix token-bucket refill off-by-one",
        "content": "Corrects the refill tick; adds a regression test.",
        "subtype": "pull_request",
    },
    {
        "source_type": "github",
        "repo": "gateway",
        "subtype": "file",
        "path": "README.md",
        "title": "README.md",
        "content": "# gateway\n\nToken-bucket rate limiter for inbound requests.\n",
    },
    {
        "source_type": "github",
        "repo": "gateway",
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
        "source_type": "github",
        "repo": "gateway",
        "subtype": "file",
        "path": "src/utils/tokens.py",
        "title": "tokens.py",
        "content": "def clamp(value, low, high):\n    return max(low, min(value, high))\n",
    },
]

_p = argparse.ArgumentParser(
    description="Read GitHub through the official PyGithub against the mock."
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
    gh = Github(auth=Auth.Token(args.token or mock.token), base_url=f"{mock.base_url}/github")

    # the repo owner is echoed back by the mock (it doesn't own an org concept), so any org works
    repos = list(gh.get_organization("acme").get_repos())
    if not repos:
        print("no repos visible to this identity")
    else:
        repo = gh.get_repo(f"acme/{repos[0].name}")
        issues = list(repo.get_issues(state="all")[:5])
        print(f"{len(repos)} repos; {repo.name} has these issues/PRs:")
        for issue in issues:
            kind = "PR" if issue.pull_request else "issue"
            print(f"  - #{issue.number} ({kind}) {issue.title}")

        # code crawl: the repo's file tree, then read one file + the README
        tree = repo.get_git_tree(repo.default_branch, recursive=True)
        print(f"\n{repo.name}@{repo.default_branch} tree ({len(tree.tree)} entries), a few paths:")
        for entry in tree.tree[:5]:
            print(f"  - {entry.type:4s} {entry.path}")

        file_paths = [e.path for e in tree.tree if e.type == "blob"]
        if file_paths:
            path = next((p for p in file_paths if p.endswith(".py")), file_paths[0])
            content_file = repo.get_contents(path)
            snippet = content_file.decoded_content.decode()[:200]
            print(f"\n$ get_contents({path!r}):")
            print("  " + snippet.replace("\n", "\n  "))

        readme = repo.get_readme()
        print(f"\n$ get_readme() -> {readme.path} ({readme.size} bytes):")
        print("  " + readme.decoded_content.decode()[:200].replace("\n", "\n  "))
