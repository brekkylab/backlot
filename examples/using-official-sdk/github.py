#!/usr/bin/env python3
"""Read GitHub through the official PyGithub. Self-contained: run it directly.

Finds its repo via `get_user().get_repos()`, reads a pull request (`get_files()`,
`get_review_comments()` vs `get_issue_comments()`), then crawls the code: `get_git_ref()`,
`get_git_tree(..., recursive=True)`, `get_contents()` and `get_readme()`.

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
        "state": "open",
        "author_email": "bob@acme.com",
        "created": "2026-02-09T09:00:00Z",
        "source_type": "github",
        "repo": "gateway",
        "title": "Rate limiter drops bursts under 50ms",
        "content": "The token-bucket refill is off by one tick.",
        "subtype": "issue",
    },
    {
        "state": "open",
        "author_email": "bob@acme.com",
        "created": "2026-02-09T14:00:00Z",
        "source_type": "github",
        "repo": "gateway",
        "title": "Fix token-bucket refill off-by-one",
        "content": "Corrects the refill tick; adds a regression test.",
        "subtype": "pull_request",
        # The files this pull touched, named as `path` values of this repo's subtype='file' records.
        # Only WHICH files: the hunks are derived from each file's own content, so the diff applies
        # with real git. Leave it out and the mock picks deterministically instead — a well-formed
        # diff, but unrelated to what the pull is about.
        "changed_paths": ["src/ratelimiter.py"],
        "comments": [
            # No `path` -> a conversation comment (GET /issues/{n}/comments)
            {
                "created_ts": "2026-02-09T16:00:00Z",
                "content": "Can we add a metric for dropped bursts?",
                "author_email": "ava@acme.com",
            },
            # `path` -> a line-anchored REVIEW comment (GET /pulls/{n}/comments). Real GitHub keeps
            # the two apart and counts them separately, and so does the mock.
            {
                "created_ts": "2026-02-09T16:20:00Z",
                "content": "This clamps against `tokens`, so the bucket can never refill.",
                "author_email": "ava@acme.com",
                "path": "src/ratelimiter.py",
                "line": 8,
            },
        ],
    },
    {
        "author_email": "ava@acme.com",
        "created": "2026-01-15T09:00:00Z",
        "source_type": "github",
        "repo": "gateway",
        "subtype": "file",
        "path": "README.md",
        "title": "README.md",
        "content": "# gateway\n\nToken-bucket rate limiter for inbound requests.\n",
    },
    {
        "author_email": "bob@acme.com",
        "created": "2026-01-20T09:00:00Z",
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
        "author_email": "bob@acme.com",
        "created": "2026-01-20T09:05:00Z",
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

    # `get_user().get_repos()` is GET /user/repos — the credential's own view of what it can reach.
    # Nothing here names the org: the mock derives it from the corpus's email domain and 404s any
    # other owner, as real GitHub does, so `full_name` is the only safe way to address a repo.
    repos = list(gh.get_user().get_repos())
    if not repos:
        print("no repos visible to this identity")
    else:
        repo = gh.get_repo(repos[0].full_name)
        issues = list(repo.get_issues(state="all")[:5])
        print(f"{len(repos)} repos; {repo.name} has these issues/PRs:")
        for issue in issues:
            kind = "PR" if issue.pull_request else "issue"
            print(f"  - #{issue.number} ({kind}) {issue.title}")

        # --- pull requests: the changeset, and the two kinds of comment ---------------------
        pulls = list(repo.get_pulls(state="all"))
        for pr in pulls[:1]:
            print(f"\n#{pr.number} {pr.title}")
            print(
                f"  {pr.changed_files} changed file(s), +{pr.additions}/-{pr.deletions}; "
                f"{pr.comments} conversation comment(s), {pr.review_comments} review comment(s)"
            )
            # get_files() is GET /pulls/{n}/files. `patch` is a real unified diff hunk built from
            # the file's own content — `git apply` accepts it.
            for f in pr.get_files():
                print(f"  - {f.status:8} {f.filename}  +{f.additions}/-{f.deletions}")
                for line in (f.patch or "").splitlines()[:4]:
                    print(f"      {line}")
            # get_review_comments() is GET /pulls/{n}/comments — the line-anchored ones only.
            # PyGithub's own docstring points at get_issue_comments() for the conversation, which
            # is the split the corpus made with `path` above.
            for rc in pr.get_review_comments():
                where = f"{rc.path}:{rc.line}" if rc.line else f"{rc.path} (file-level)"
                print(f"  review comment @ {where}: {rc.body}")
            for ic in pr.get_issue_comments():
                print(f"  conversation: {ic.body}")

        # --- code crawl: pin the branch to a commit, then read the tree, a file, the README ----
        # get_git_ref() is GET /git/ref/{ref}. It takes the ref as a trailing PATH, which is why it
        # resolves a branch whose name contains a slash (`heads/release/2026-03`) where
        # /branches/{branch} cannot.
        head = repo.get_git_ref(f"heads/{repo.default_branch}")
        print(f"\n$ get_git_ref('heads/{repo.default_branch}') -> {head.object.sha[:12]}")

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
