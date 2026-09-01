"""GitHub's REST surface: issues, PRs, comments, and the git tree/contents/blobs
codebase serving.

One file per router, so a source's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import base64
import hashlib
import json

import yaml

import pytest

from backlot import store
from tests._helpers import build_corpus, client_for, crawl_github_repo, db_count, tiny_corpus


def test_github_serves_a_comment_dated_at_the_epoch(tmp_path):
    """A comment id is an INTEGER and `synth.epoch` hashes a string, so a comment
    dated 1970-01-01T00:00:00Z (which stores as 0, and reached that fallback under truthiness)
    took both endpoints down with an AttributeError."""
    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "github",
                "doc_id": "gh-zero",
                "repo": "gw",
                "title": "Bug",
                "content": "x",
                "author_email": "a@x.com",
                "visibility": "public",
                "number": 7,
                "comments": [
                    {"content": "at the epoch", "author_email": "b@x.com", "created_ts": 0}
                ],
            }
        ],
    )
    with client_for(s, reload=True) as c:
        h = {"Authorization": f"Bearer {s.admin_token}"}
        org = c.get("/_meta/users", headers=h).json()["org"]
        (comment,) = c.get(f"/github/repos/{org}/gw/issues/7/comments", headers=h).json()
        assert comment["created_at"].startswith("1970-01-01T00:00:00")
        assert (
            c.get(f"/github/repos/{org}/gw/issues/comments/{comment['id']}", headers=h).json()["id"]
            == comment["id"]
        )


# Which fields belong to which object, from a key-set diff of api.github.com's issue and pull
# bodies. Only the ones Backlot has reason to serve: real's issue also carries
# `sub_issues_summary`, `type` and friends, which no corpus here has anything to fill in.
#
# `pull_request` is in the issue set because an issue that IS a pull carries that marker — the
# fixture below is exactly that case. A plain issue has the other seven.
ISSUE_ONLY_FIELDS = frozenset(
    {
        "closed_by",
        "events_url",
        "labels_url",
        "pull_request",
        "reactions",
        "repository_url",
        "state_reason",
        "timeline_url",
    }
)
PULL_ONLY_FIELDS = frozenset(
    {
        "_links",
        "auto_merge",
        "commits_url",
        "maintainer_can_modify",
        "review_comment_url",
        "review_comments_url",
        "statuses_url",
    }
)


def test_admin_github_crawls_all(client, admin_h, ro_conn, org):
    repos = client.get(
        f"/github/orgs/{org}/repos", headers=admin_h, params={"per_page": 100}
    ).json()
    seen = []
    for r in repos:
        seen += crawl_github_repo(client, admin_h, org, r["name"])
    assert len(seen) == db_count(ro_conn, "github")


def _gh_row(conn, title: str):
    """The github row a fixture record with this title became.

    A github number is assigned against the whole corpus, so it cannot be computed from the
    record's own identifier — which does not survive the import anyway. The row is found by
    something the fixture can still see, as any other client would have to."""
    return conn.execute("SELECT * FROM github_items WHERE title = ?", (title,)).fetchone()


def test_github_body_roundtrip(client, admin_h, ro_conn, org):
    doc = ro_conn.execute("SELECT * FROM github_items LIMIT 1").fetchone()

    num = doc["number"]
    issue = client.get(f"/github/repos/{org}/{doc['repo']}/issues/{num}", headers=admin_h).json()
    assert issue["body"] == doc["content"] and issue["title"] == doc["title"]


def test_github_issues_filtered_by_state(client, admin_h, org):
    # gateway repo: gh-issue-1 is open, gh-pr-1 is a closed PR (both surface via /issues)
    open_body = client.get(
        f"/github/repos/{org}/gateway/issues", headers=admin_h, params={"state": "open"}
    ).json()
    assert [i["title"] for i in open_body] == ["Rate limiter drops bursts under 50ms"]
    closed_body = client.get(
        f"/github/repos/{org}/gateway/issues", headers=admin_h, params={"state": "closed"}
    ).json()
    assert [i["title"] for i in closed_body] == ["Fix token-bucket refill off-by-one"]
    all_body = client.get(
        f"/github/repos/{org}/gateway/issues", headers=admin_h, params={"state": "all"}
    ).json()
    assert {i["title"] for i in all_body} == {
        "Rate limiter drops bursts under 50ms",
        "Fix token-bucket refill off-by-one",
    }
    # default (no state param) behaves like real GitHub: open only
    default_body = client.get(f"/github/repos/{org}/gateway/issues", headers=admin_h).json()
    assert default_body == open_body


def test_github_pulls_filtered_by_state(client, admin_h, org):
    # gateway repo's only PR (gh-pr-1) is closed
    open_body = client.get(
        f"/github/repos/{org}/gateway/pulls", headers=admin_h, params={"state": "open"}
    ).json()
    assert open_body == []
    closed_body = client.get(
        f"/github/repos/{org}/gateway/pulls", headers=admin_h, params={"state": "closed"}
    ).json()
    assert [p["title"] for p in closed_body] == ["Fix token-bucket refill off-by-one"]
    all_body = client.get(
        f"/github/repos/{org}/gateway/pulls", headers=admin_h, params={"state": "all"}
    ).json()
    assert [p["title"] for p in all_body] == ["Fix token-bucket refill off-by-one"]


# --- github codebase serving: git tree / contents / blobs / branches / readme ---------
#
# These need `github` `file` docs, which the shared SAMPLE corpus (built once, session-scoped,
# in conftest.py) doesn't carry. Rather than touch conftest.py, `gh_client` below builds its own
# small DB — SAMPLE plus a 'codebase' repo of file docs — the same way conftest._build() does.

_GH_FILE_DOCS = [
    {
        "source_type": "github",
        "doc_id": "gh-file-readme",
        "repo": "codebase",
        "subtype": "file",
        "path": "README.md",
        "title": "README.md",
        "content": "# codebase\n\nCore service source, browsable via the tree/contents API.\n",
        "group": "engineering",
        "visibility": "public",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
    },
    {
        "source_type": "github",
        "doc_id": "gh-file-main",
        "repo": "codebase",
        "subtype": "file",
        "path": "src/main.py",
        "title": "main.py",
        "content": "def main():\n    return 1\n",
        "group": "engineering",
        "visibility": "public",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
    },
    {
        "source_type": "github",
        "doc_id": "gh-file-utils",
        "repo": "codebase",
        "subtype": "file",
        "path": "src/pkg/utils.py",
        "title": "utils.py",
        "content": "def helper():\n    return 2\n",
        "group": "engineering",
        "visibility": "public",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
    },
    {
        "source_type": "github",
        "doc_id": "gh-file-secret",
        "repo": "codebase",
        "subtype": "file",
        "path": "config/secret.yaml",
        "title": "secret.yaml",
        "content": "api_key: shh\n",
        "group": "people",
        "visibility": "group",
        "author_email": "hana@acme.com",
        "author_groups": ["people"],
    },
    # a separate repo (not 'codebase') so this doesn't perturb the exact tree/contents sets the
    # 'codebase' tests assert against
    {
        "source_type": "github",
        "doc_id": "gh-file-unicode",
        "repo": "unicode-repo",
        "subtype": "file",
        "path": "docs/unicode.md",
        "title": "unicode.md",
        "content": "héllo wörld 世界\n",
        "group": "engineering",
        "visibility": "public",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
    },
    # Three snapshots of ONE path, in a repo of its own so the exact tree/contents sets asserted
    # for 'codebase' stay untouched. This is what a corpus recording a file's edits produces: a
    # file is addressed by (repo, path), so these are one file's history, not three files. `created`
    # is explicit on each because a synthesized one lands in 2023-2024 and HEAD would be a coin
    # toss; the middle one states no `ref`, which is the shape a corpus that just dates its
    # snapshots produces.
    {
        "source_type": "github",
        "doc_id": "gh-hist-v1",
        "repo": "history-repo",
        "subtype": "file",
        "path": "svc/rate.py",
        "ref": "pr-1",
        "title": "rate.py",
        "content": "LIMIT = 1\n",
        "created": "2026-01-01T00:00:00Z",
        "group": "engineering",
        "visibility": "public",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
    },
    {
        "source_type": "github",
        "doc_id": "gh-hist-v2",
        "repo": "history-repo",
        "subtype": "file",
        "path": "svc/rate.py",
        "title": "rate.py",
        "content": "LIMIT = 2\n",
        "created": "2026-02-01T00:00:00Z",
        "group": "engineering",
        "visibility": "public",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
    },
    {
        "source_type": "github",
        "doc_id": "gh-hist-v3",
        "repo": "history-repo",
        "subtype": "file",
        "path": "svc/rate.py",
        "ref": "pr-3",
        "title": "rate.py",
        "content": "LIMIT = 3\n",
        "created": "2026-03-01T00:00:00Z",
        "group": "engineering",
        "visibility": "public",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
    },
    {
        "source_type": "github",
        "doc_id": "gh-hist-readme-v1",
        "repo": "history-repo",
        "subtype": "file",
        "path": "README.md",
        "ref": "pr-1",
        "title": "README.md",
        "content": "# history-repo\n\nfirst\n",
        "created": "2026-01-01T00:00:00Z",
        "group": "engineering",
        "visibility": "public",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
    },
    {
        "source_type": "github",
        "doc_id": "gh-hist-readme-v2",
        "repo": "history-repo",
        "subtype": "file",
        "path": "README.md",
        "ref": "pr-3",
        "title": "README.md",
        "content": "# history-repo\n\nsecond\n",
        "created": "2026-03-01T00:00:00Z",
        "group": "engineering",
        "visibility": "public",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
    },
    # a second path in the same repo, so the tree has something to be a set OF
    {
        "source_type": "github",
        "doc_id": "gh-hist-other",
        "repo": "history-repo",
        "subtype": "file",
        "path": "svc/other.py",
        "title": "other.py",
        "content": "OTHER = 0\n",
        "created": "2026-01-15T00:00:00Z",
        "group": "engineering",
        "visibility": "public",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
    },
    # a file doc, deliberately chosen (by brute force over the doc_id) so its synthesized
    # `number` collides with gh-issue-1's in the SAME repo ('gateway') -- reproduces the
    # (repo, number) index-shadowing bug: a file's number must never be able to hide a
    # real issue/PR at that number.
    {
        "source_type": "github",
        "doc_id": "gh-file-collide-88814",
        "repo": "gateway",
        "subtype": "file",
        "path": "src/collide.py",
        "title": "collide.py",
        "content": "# unrelated file content\n",
        "group": "engineering",
        "visibility": "public",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
    },
]


# A repo carrying BOTH file docs and a pull request, which nothing above does: 'codebase' is
# files-only (test_github_file_excluded_from_issues_and_pulls asserts its pulls are empty) and
# 'gateway' has issues/PRs but only the one collision file. A PR's changeset is synthesized from
# the repo's own file set, so the diff endpoints need a repo where both exist.
_GH_DIFF_DOCS = (
    [
        {
            "source_type": "github",
            "doc_id": f"gh-diff-file-{name}",
            "repo": "diffable",
            "subtype": "file",
            "path": path,
            "title": path.rsplit("/", 1)[-1],
            "content": content,
            "group": "engineering",
            "visibility": "public",
            "author_email": "ava@acme.com",
            "author_groups": ["engineering"],
        }
        for name, path, content in [
            ("app", "app.py", "import sys\n\n\ndef run(argv):\n    return 0\n"),
            ("core", "pkg/core.py", "\n".join(f"line_{i} = {i}" for i in range(1, 31)) + "\n"),
            ("readme", "README.md", "# diffable\n\nA repo whose pulls have a changeset.\n"),
            ("conf", "pkg/conf.toml", '[tool]\nname = "diffable"\n'),
            # no trailing newline: a hunk touching the last line needs git's `\ No newline` marker,
            # and putting that marker mid-file is a diff git refuses to apply
            ("nonl", "pkg/no_newline.cfg", "alpha\nbeta\ngamma"),
        ]
    ]
    + [
        # people-only, so a pull can DECLARE a path the caller cannot see
        {
            "source_type": "github",
            "doc_id": "gh-diff-file-secret",
            "repo": "diffable",
            "subtype": "file",
            "path": "secret/keys.txt",
            "title": "keys.txt",
            "content": "rotate-me\nand-me\n",
            "group": "people",
            "visibility": "group",
            "author_email": "hana@acme.com",
            "author_groups": ["people"],
        }
    ]
    + [
        {
            "source_type": "github",
            "doc_id": "gh-diff-pr",
            "repo": "diffable",
            "subtype": "pull_request",
            "title": "Tighten the run() argv handling",
            "content": "Reworks argv parsing and drops the dead branch.",
            "group": "engineering",
            "visibility": "public",
            "author_email": "bob@acme.com",
            "author_groups": ["engineering"],
            "state": "open",
            "head": "fix/argv",
            "base": "main",
        },
        # The same repo's other pull, this one DECLARING its changeset and carrying both kinds of
        # comment. Four paths so a small per_page actually produces a second page — the synthesized
        # changeset caps at three and could never take the paging branch.
        {
            "source_type": "github",
            "doc_id": "gh-diff-pr-declared",
            "repo": "diffable",
            "subtype": "pull_request",
            "title": "Rename line_1 and document the config",
            "content": "Touches the four files it says it touches.",
            "group": "engineering",
            "visibility": "public",
            "author_email": "bob@acme.com",
            "author_groups": ["engineering"],
            "state": "open",
            "head": "chore/rename",
            "base": "main",
            "changed_paths": ["pkg/core.py", "app.py", "pkg/conf.toml", "README.md"],
            "comments": [
                {"content": "conversation, not anchored", "author_email": "ava@acme.com"},
                {
                    "content": "this line should be a constant",
                    "author_email": "ava@acme.com",
                    "path": "pkg/core.py",
                    "line": 4,
                },
                {
                    "content": "file-level note, no line",
                    "author_email": "ava@acme.com",
                    "path": "app.py",
                    "diff_hunk": "@@ -1,2 +1,3 @@\n import sys\n",
                },
            ],
        },
        # review comments anchored to paths that do NOT resolve: one to no file at all, one to a
        # people-only file. Both drop out of the list, so `review_comments` has to drop them too.
        {
            "source_type": "github",
            "doc_id": "gh-diff-pr-unresolvable",
            "repo": "diffable",
            "subtype": "pull_request",
            "title": "Comments anchored off the tree",
            "content": "body",
            "group": "engineering",
            "visibility": "public",
            "author_email": "bob@acme.com",
            "author_groups": ["engineering"],
            "changed_paths": ["app.py"],
            "comments": [
                {
                    "content": "resolvable",
                    "author_email": "ava@acme.com",
                    "path": "app.py",
                    "line": 1,
                },
                {
                    "content": "no such file",
                    "author_email": "ava@acme.com",
                    "path": "gone/x.py",
                    "line": 1,
                },
                {
                    "content": "people only",
                    "author_email": "hana@acme.com",
                    "path": "secret/keys.txt",
                    "line": 1,
                },
            ],
        },
        {
            "source_type": "github",
            "doc_id": "gh-diff-pr-restricted",
            "repo": "diffable",
            "subtype": "pull_request",
            "title": "Rotate the signing keys",
            "content": "Declares a path only the people group can read.",
            "group": "engineering",
            "visibility": "public",
            "author_email": "hana@acme.com",
            "author_groups": ["people"],
            "changed_paths": ["app.py", "secret/keys.txt"],
        },
    ]
)


@pytest.fixture(scope="module")
def gh_client(tmp_path_factory):
    from tests.conftest import SAMPLE

    settings = build_corpus(
        tmp_path_factory.mktemp("gh_sample"), SAMPLE + _GH_FILE_DOCS + _GH_DIFF_DOCS
    )
    with client_for(settings) as c:
        yield c, settings


@pytest.fixture(scope="module")
def gh_org(gh_client):
    c, _ = gh_client
    return c.get("/_meta/users").json()["org"]


@pytest.fixture(scope="module")
def gh_user_tokens(gh_client):
    _, settings = gh_client
    data = yaml.safe_load(settings.tokens_path.read_text())
    return {"admin": data["admin_token"], **{u["email"]: u["token"] for u in data["users"]}}


@pytest.fixture(scope="module")
def gh_admin_h(gh_user_tokens):
    return {"Authorization": f"Bearer {gh_user_tokens['admin']}"}


def test_github_tree_recursive(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    body = c.get(
        f"/github/repos/{gh_org}/codebase/git/trees/main",
        headers=gh_admin_h,
        params={"recursive": "1"},
    ).json()
    assert body["truncated"] is False
    paths = {e["path"] for e in body["tree"]}
    assert paths == {
        "README.md",
        "src",
        "src/main.py",
        "src/pkg",
        "src/pkg/utils.py",
        "config",
        "config/secret.yaml",
    }
    content = "def main():\n    return 1\n"
    blob = next(e for e in body["tree"] if e["path"] == "src/main.py")
    assert blob["mode"] == "100644" and blob["type"] == "blob"
    assert blob["sha"] == hashlib.sha1(content.encode()).hexdigest()
    assert blob["size"] == len(content)
    tree_dir = next(e for e in body["tree"] if e["path"] == "src/pkg")
    assert tree_dir["mode"] == "040000" and tree_dir["type"] == "tree"
    assert "size" not in tree_dir


def test_github_tree_non_recursive(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    body = c.get(f"/github/repos/{gh_org}/codebase/git/trees/main", headers=gh_admin_h).json()
    paths = {e["path"] for e in body["tree"]}
    assert paths == {"README.md", "src", "config"}  # top level only: root file + top dirs


@pytest.mark.parametrize(
    "params,expected",
    [({}, {"main.py", "pkg"}), ({"recursive": "1"}, {"main.py", "pkg", "pkg/utils.py"})],
    ids=["shallow", "recursive"],
)
def test_github_a_subtree_sha_resolves_to_that_subtree(
    gh_client, gh_admin_h, gh_org, params, expected
):
    """git/trees takes a TREE sha, not only a commit-ish, and answers that tree's own entries with
    paths relative to it. That is how a client walks a repo one directory at a time: it reads a
    subtree's sha out of the parent listing and asks for it — which is what fsspec's
    GithubFileSystem does.

    Answering the root for every ref does not fail loudly; it reports the root's entries under the
    child's name, so `ls("src")` yields `src/src` and `src/config` and a recursive walk descends
    until it runs out of stack.
    """
    c, _ = gh_client
    root = c.get(f"/github/repos/{gh_org}/codebase/git/trees/main", headers=gh_admin_h).json()
    src_sha = next(e["sha"] for e in root["tree"] if e["path"] == "src")

    body = c.get(
        f"/github/repos/{gh_org}/codebase/git/trees/{src_sha}", headers=gh_admin_h, params=params
    ).json()

    assert body["sha"] == src_sha, "the response names the tree that was asked for, not the root"
    assert {e["path"] for e in body["tree"]} == expected


def test_github_contents_dir(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    body = c.get(f"/github/repos/{gh_org}/codebase/contents/src", headers=gh_admin_h).json()
    assert {(e["name"], e["type"]) for e in body} == {("main.py", "file"), ("pkg", "dir")}


def test_github_contents_file(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    body = c.get(f"/github/repos/{gh_org}/codebase/contents/src/main.py", headers=gh_admin_h).json()
    content = "def main():\n    return 1\n"
    assert body["type"] == "file" and body["encoding"] == "base64"
    assert base64.b64decode(body["content"]).decode() == content
    assert body["sha"] == hashlib.sha1(content.encode()).hexdigest()
    assert body["name"] == "main.py" and body["path"] == "src/main.py"


def test_github_contents_root(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    body = c.get(f"/github/repos/{gh_org}/codebase/contents", headers=gh_admin_h).json()
    assert {e["name"] for e in body} == {"README.md", "src", "config"}


def test_github_blob_by_sha(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    content = "def main():\n    return 1\n"
    sha = hashlib.sha1(content.encode()).hexdigest()
    body = c.get(f"/github/repos/{gh_org}/codebase/git/blobs/{sha}", headers=gh_admin_h).json()
    assert body["sha"] == sha and body["encoding"] == "base64"
    assert base64.b64decode(body["content"]).decode() == content


def test_github_blob_unknown_sha_404(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    r = c.get(f"/github/repos/{gh_org}/codebase/git/blobs/{'0' * 40}", headers=gh_admin_h)
    assert r.status_code == 404
    # real's envelope, with this route's own documentation_url (see backlot.errors.github)
    assert r.json() == {
        "message": "Not Found",
        "documentation_url": "https://docs.github.com/rest/git/blobs#get-a-blob",
        "status": "404",
    }


def test_github_lists_the_refs_a_client_enumerates_before_it_reads(gh_client, gh_admin_h, gh_org):
    """The two ref LISTINGS, which a client that is handed a repo rather than a sha starts from —
    fsspec's `GithubFileSystem.branches`/`.tags`/`.refs` are these two routes and nothing else.

    A listed branch is real's SHORT branch, not the object `/branches/{branch}` serves: measured
    against api.github.com, an item carries `commit: {sha, url}` and stops there, where the
    single-branch route nests the whole commit under the same key. Serving the longer object here
    would hand a client a field real GitHub never sends.

    Tags are `[]`: a corpus states none, and `[]` is what real answers for a repo with no tags
    (measured against octocat/Hello-World) — a shape a client meets in production rather than a
    mock-only degenerate case, the same reasoning `/statuses/{sha}` is written on.
    """
    c, _ = gh_client
    repo = c.get(f"/github/repos/{gh_org}/codebase", headers=gh_admin_h).json()
    branches = c.get(f"/github/repos/{gh_org}/codebase/branches", headers=gh_admin_h)
    assert branches.status_code == 200
    body = branches.json()
    assert [b["name"] for b in body] == [repo["default_branch"]]
    assert body[0]["protected"] is False
    assert set(body[0]["commit"]) == {"sha", "url"}
    # the one branch is the one `/branches/{branch}` already serves, down to the commit
    single = c.get(f"/github/repos/{gh_org}/codebase/branches/main", headers=gh_admin_h).json()
    assert body[0]["commit"]["sha"] == single["commit"]["sha"]
    assert body[0]["commit"]["url"] == single["commit"]["url"]

    # `?protected=` selects: real answers only the protected branches for a true value, only the
    # unprotected ones for `false`/`0`, and all of them for an empty or omitted parameter —
    # measured on fastapi/fastapi, 22 branches with one protected, answering 1 / 21 / 22. The one
    # branch here is unprotected, so those last two coincide and `_truthy`'s split is the whole
    # rule, as it is for `?recursive=` on git/trees.
    for value, kept in (("true", 0), ("1", 0), ("yes", 0), ("false", 1), ("0", 1), ("", 1)):
        r = c.get(
            f"/github/repos/{gh_org}/codebase/branches",
            headers=gh_admin_h,
            params={"protected": value},
        )
        assert r.status_code == 200 and len(r.json()) == kept, f"?protected={value!r}"

    tags = c.get(f"/github/repos/{gh_org}/codebase/tags", headers=gh_admin_h)
    assert tags.status_code == 200 and tags.json() == []


def test_github_branch_and_commit_resolve_tree(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    branch = c.get(f"/github/repos/{gh_org}/codebase/branches/main", headers=gh_admin_h).json()
    tree_sha = branch["commit"]["commit"]["tree"]["sha"]
    commit_sha = branch["commit"]["sha"]
    commit = c.get(
        f"/github/repos/{gh_org}/codebase/commits/{commit_sha}", headers=gh_admin_h
    ).json()
    assert commit["commit"]["tree"]["sha"] == tree_sha
    # the tree sha resolved from branch/commit is itself a valid `ref` for git/trees
    tree = c.get(f"/github/repos/{gh_org}/codebase/git/trees/{tree_sha}", headers=gh_admin_h).json()
    assert tree["sha"] == tree_sha
    assert {e["path"] for e in tree["tree"]}


def test_github_readme_real_content(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    body = c.get(f"/github/repos/{gh_org}/codebase/readme", headers=gh_admin_h).json()
    text = "# codebase\n\nCore service source, browsable via the tree/contents API.\n"
    assert base64.b64decode(body["content"]).decode() == text
    assert body["sha"] == hashlib.sha1(text.encode()).hexdigest()


def test_github_readme_stub_when_no_readme_file(client, admin_h, org):
    # 'gateway' (base SAMPLE) has issues/PRs but no file docs -> falls back to the stub
    body = client.get(f"/github/repos/{org}/gateway/readme", headers=admin_h).json()
    assert base64.b64decode(body["content"]).decode().startswith("# gateway")


def test_github_file_excluded_from_issues_and_pulls(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    issues = c.get(
        f"/github/repos/{gh_org}/codebase/issues", headers=gh_admin_h, params={"state": "all"}
    ).json()
    assert issues == []  # 'codebase' has only file docs, no issues/PRs
    pulls = c.get(
        f"/github/repos/{gh_org}/codebase/pulls", headers=gh_admin_h, params={"state": "all"}
    ).json()
    assert pulls == []


def test_github_a_file_body_is_searchable_as_code_and_not_as_an_issue(
    gh_client, gh_admin_h, gh_org
):
    """A string only a file's body holds is reachable through `/search/code` and absent from
    `/search/issues`.

    The two endpoints share one FTS index and split it by `kind`: real GitHub's issue search does
    not return files, so filtering them out there is right. Until code search existed that filter
    left a file's body indexed and UNREACHABLE — the only route to it was to already know its path.
    """
    c, _ = gh_client
    # 'helper' appears only in codebase/src/pkg/utils.py's content
    issues = c.get("/github/search/issues", headers=gh_admin_h, params={"q": "helper"}).json()
    assert issues["total_count"] == 0
    assert issues["items"] == []

    body = c.get("/github/search/code", headers=gh_admin_h, params={"q": "helper"}).json()
    assert body["total_count"] == 1
    assert body["incomplete_results"] is False
    item = body["items"][0]
    assert (item["repository"]["name"], item["path"]) == ("codebase", "src/pkg/utils.py")
    assert item["name"] == "utils.py"
    assert item["sha"] == hashlib.sha1(b"def helper():\n    return 2\n").hexdigest()
    assert item["score"] == 1.0  # real reports a flat 1.0; the ORDER carries the relevance
    assert item["repository"]["full_name"] == f"{gh_org}/codebase"
    assert item["html_url"] == f"https://github.com/{gh_org}/codebase/blob/main/src/pkg/utils.py"
    # the links the item carries are ones Backlot serves
    for url in (item["url"], item["git_url"]):
        assert c.get(url.split("testserver", 1)[1], headers=gh_admin_h).status_code == 200

    # the owner-qualified `repo:` a real client sends, and a foreign owner
    def total(q):
        return c.get("/github/search/code", headers=gh_admin_h, params={"q": q}).json()[
            "total_count"
        ]

    assert total(f"repo:{gh_org}/codebase helper") == 1
    assert total("repo:other-org/codebase helper") == 0


def test_github_code_search_returns_only_a_paths_head_snapshot(gh_client, gh_admin_h):
    """Real code search indexes the DEFAULT BRANCH, so a path is one result however many snapshots
    the corpus holds — and a string surviving only in a superseded snapshot is not findable.
    """
    c, _ = gh_client

    def paths(q):
        body = c.get("/github/search/code", headers=gh_admin_h, params={"q": q}).json()
        return [(i["repository"]["name"], i["path"]) for i in body["items"]]

    # svc/rate.py is stored three times; 'LIMIT' matches all three rows and is ONE result
    assert paths("repo:history-repo LIMIT") == [("history-repo", "svc/rate.py")]
    # 'second' is HEAD's README body; 'first' only the superseded pr-1 snapshot's
    assert paths("second") == [("history-repo", "README.md")]
    assert paths("first") == []

    # the item's own url fetches the bytes that were searched
    item = c.get(
        "/github/search/code", headers=gh_admin_h, params={"q": "repo:history-repo LIMIT"}
    ).json()["items"][0]
    raw = c.get(
        item["url"].split("testserver", 1)[1],
        headers={**gh_admin_h, "Accept": "application/vnd.github.raw"},
    )
    assert raw.text == "LIMIT = 3\n"


@pytest.mark.parametrize(
    "q, expected",
    [
        # free text with no `in:` searches the content AND the path, as real does
        ("helper", [("codebase", "src/pkg/utils.py")]),
        ("in:file helper", [("codebase", "src/pkg/utils.py")]),
        ("in:path helper", []),
        ("in:path svc", [("history-repo", "svc/other.py"), ("history-repo", "svc/rate.py")]),
        # quoting groups a term; the quotes are not part of what is matched
        ('in:path "svc/other"', [("history-repo", "svc/other.py")]),
        (
            "repo:codebase in:path src",
            [("codebase", "src/main.py"), ("codebase", "src/pkg/utils.py")],
        ),
        ("filename:utils", [("codebase", "src/pkg/utils.py")]),
        ("filename:utils.py", [("codebase", "src/pkg/utils.py")]),
        ("path:src/pkg", [("codebase", "src/pkg/utils.py")]),
        # `path:/` is real's spelling for the ROOT, not for "any path"
        (
            "path:/",
            [
                ("codebase", "README.md"),
                ("diffable", "README.md"),
                ("diffable", "app.py"),
                ("history-repo", "README.md"),
            ],
        ),
        (
            "path:pkg",
            [
                ("codebase", "src/pkg/utils.py"),
                ("diffable", "pkg/conf.toml"),
                ("diffable", "pkg/core.py"),
                ("diffable", "pkg/no_newline.cfg"),
            ],
        ),
        ("extension:toml", [("diffable", "pkg/conf.toml")]),
        ("extension:.toml", [("diffable", "pkg/conf.toml")]),
        ("repo:no-such-repo helper", []),
        # a path fragment is a LITERAL: SQL LIKE's own wildcards are not a search syntax, so `_`
        # finds the one path holding an underscore rather than standing in for any character
        ("in:path %", []),
        ("in:path _", [("diffable", "pkg/no_newline.cfg")]),
        ("in:path no_newline", [("diffable", "pkg/no_newline.cfg")]),
    ],
)
def test_github_code_search_qualifiers(gh_client, gh_admin_h, q, expected):
    c, _ = gh_client
    body = c.get("/github/search/code", headers=gh_admin_h, params={"q": q}).json()
    assert sorted((i["repository"]["name"], i["path"]) for i in body["items"]) == sorted(expected)
    assert body["total_count"] == len(expected)


def test_github_code_search_is_acl_scoped(gh_client, gh_admin_h, gh_user_tokens):
    """A restricted file's body is not searchable by a caller who cannot read the file.

    The same corpus test_github_file_acl_scoped uses: codebase/config/secret.yaml is people-only.
    Search is the widest door onto a body, so it has to be the same door.
    """
    c, _ = gh_client
    member_h = {"Authorization": f"Bearer {gh_user_tokens['hana@acme.com']}"}  # in 'people'
    nonmember_h = {"Authorization": f"Bearer {gh_user_tokens['bob@acme.com']}"}  # not in 'people'

    def paths(headers, q="shh"):
        body = c.get("/github/search/code", headers=headers, params={"q": q}).json()
        return [i["path"] for i in body["items"]]

    assert paths(gh_admin_h) == ["config/secret.yaml"]
    assert paths(member_h) == ["config/secret.yaml"]
    assert paths(nonmember_h) == []
    # a qualifier-only listing is the same door, not a way around it
    assert paths(nonmember_h, "extension:yaml") == []
    assert paths(member_h, "extension:yaml") == ["config/secret.yaml"]


def test_github_code_search_text_matches_only_under_the_media_type(gh_client, gh_admin_h):
    """`text_matches` is what makes a hit useful rather than merely located, and real serves it only
    when `Accept` asks for it."""
    c, _ = gh_client
    plain = c.get("/github/search/code", headers=gh_admin_h, params={"q": "helper"}).json()
    assert "text_matches" not in plain["items"][0]

    h = {**gh_admin_h, "Accept": "application/vnd.github.text-match+json"}
    item = c.get("/github/search/code", headers=h, params={"q": "helper"}).json()["items"][0]
    (tm,) = item["text_matches"]
    assert tm["object_type"] == "FileContent"
    assert tm["property"] == "content"
    assert tm["object_url"] == item["url"]
    assert "def helper():" in tm["fragment"]
    (m,) = tm["matches"]
    start, end = m["indices"]
    assert tm["fragment"][start:end] == m["text"] == "helper"

    # a term repeated in the query is one occurrence in the file, so it is one match
    twice = c.get("/github/search/code", headers=h, params={"q": "helper helper"}).json()
    assert twice["items"][0]["text_matches"][0]["matches"] == [m]

    # a quoted phrase is ONE term spanning the space, not two terms carrying a stray quote
    quoted = c.get("/github/search/code", headers=h, params={"q": '"def helper"'}).json()
    (phrase,) = quoted["items"][0]["text_matches"][0]["matches"]
    assert phrase == {"text": "def helper", "indices": [0, 10]}

    # a hit matched on its PATH still carries the fragment real sends, with no match inside it
    only_path = c.get("/github/search/code", headers=h, params={"q": "in:path svc/other"}).json()
    (tm,) = only_path["items"][0]["text_matches"]
    assert tm["fragment"] == "OTHER = 0\n"
    assert tm["matches"] == []


def test_github_code_search_refuses_a_query_less_search(gh_client, gh_admin_h):
    """Real answers a `q`-less code search 422 in its own envelope, not FastAPI's `{"detail": …}`.

    `/search/issues` answers a blank `q` the same way; the test for that one sits beside it.
    """
    c, _ = gh_client
    r = c.get("/github/search/code", headers=gh_admin_h)
    assert r.status_code == 422
    assert r.json() == {
        "message": "Validation Failed",
        "documentation_url": "https://docs.github.com/v3/search",
        "errors": [{"resource": "Search", "field": "q", "code": "missing"}],
        "status": "422",
    }
    assert c.get("/github/search/code", headers=gh_admin_h, params={"q": " "}).status_code == 422


def test_github_issue_search_refuses_a_query_less_search(gh_client, gh_admin_h):
    """The expensive half of the same rule. A `q`-less issue search used to answer 200 with every
    issue and pull the caller could see, so a client that forgot its query got a plausible,
    ACL-scoped result set here and a hard 422 in production — nothing in between said so.

    Measured against api.github.com: `GET /search/issues` with no `q` at all is real's `Validation Failed`,
    resource `Search`, field `q`, code `missing`. (`/search/code` differs on the missing-parameter
    case only — real rejects that one at the query-string parser, `400 text/plain "Failed to
    deserialize query string: missing field q"` — and answers the same 422 for `?q=`. Backlot
    declares `q` with a default, so both routes see one case, and the 422 is the one to serve.)
    """
    c, _ = gh_client
    for params in ({}, {"q": ""}, {"q": "   "}):
        r = c.get("/github/search/issues", headers=gh_admin_h, params=params)
        assert r.status_code == 422, params
        assert r.json() == {
            "message": "Validation Failed",
            "documentation_url": "https://docs.github.com/v3/search",
            "errors": [{"resource": "Search", "field": "q", "code": "missing"}],
            "status": "422",
        }
    # ...and a real query still answers
    assert (
        c.get("/github/search/issues", headers=gh_admin_h, params={"q": "is:issue"}).status_code
        == 200
    )


def test_github_errors_answer_githubs_envelope(gh_client, gh_admin_h, gh_org):
    """`{"message", "documentation_url", "status"}`, not FastAPI's `{"detail": …}`.

    PyGithub picks its exception CLASS off `message`, so `detail` cost a client the difference
    between `BadCredentialsException` and a bare `GithubException` (pinned in `tests/test_sdk.py`).
    The wording was already real's; only the shape around it was not.
    """
    c, _ = gh_client
    r = c.get(f"/github/repos/{gh_org}/no-such-repo", headers=gh_admin_h)
    assert r.status_code == 404
    assert r.json() == {
        "message": "Not Found",
        "documentation_url": "https://docs.github.com/rest/repos/repos#get-a-repository",
        "status": "404",
    }
    assert "detail" not in r.json()
    # a repo the caller cannot see answers the same as one that is not there, as it did before
    assert c.get(f"/github/repos/{gh_org}/vault", headers=gh_admin_h).status_code == 200
    # non-github paths keep FastAPI's default envelope
    assert "detail" in c.get("/no-such-route").json()


def test_github_401_says_which_credential_failed(gh_client, gh_admin_h, gh_org):
    """Two causes, two messages, as real has them (measured against api.github.com):
    a credential that arrived and did not resolve is "Bad credentials", and a request carrying none
    is "Requires authentication". A client telling "I forgot the token" from "my token is wrong"
    read one answer for both before this.

    An `Authorization` real cannot parse is the second case, not the first: real ignores the header
    and serves the request anonymously (`Basic …` and a scheme-less value both answer 200 on a
    public repo), so the caller arrives with no credential rather than a rejected one.

    Both carry the bare `https://docs.github.com/rest`, which is what real answers on the routes a
    Backlot caller can meet a 401 on — a 404's route-specific anchor is not used here.
    """
    c, _ = gh_client
    url = f"/github/repos/{gh_org}/codebase"
    missing = c.get(url)
    assert missing.status_code == 401
    assert missing.json() == {
        "message": "Requires authentication",
        "documentation_url": "https://docs.github.com/rest",
        "status": "401",
    }
    for unparseable in ("Basic Zm9vOmJhcg==", "just-a-value", "Bearer"):
        r = c.get(url, headers={"Authorization": unparseable})
        assert r.status_code == 401 and r.json()["message"] == "Requires authentication", (
            unparseable
        )
    bad = c.get(url, headers={"Authorization": "Bearer usr-not-a-real-token"})
    assert bad.status_code == 401
    assert bad.json() == {
        "message": "Bad credentials",
        "documentation_url": "https://docs.github.com/rest",
        "status": "401",
    }


def test_github_documentation_url_names_the_route_that_failed(gh_client, gh_admin_h, gh_org):
    """Real's `documentation_url` is per-ENDPOINT — `/branches` names the list-branches anchor,
    `/tags` the list-tags one — so a single root URL would be a divergence on every 404. The table
    was measured by requesting each route shape against a repository that does not exist.

    Every route the app serves needs an entry, which is what the first assertion holds: a route
    added without one would answer the root and nobody would notice.
    """
    from backlot.errors import github as gh_errors

    c, _ = gh_client
    served = {p for p in c.get("/openapi.json").json()["paths"] if p.startswith("/github")}
    assert set(gh_errors.ROUTE_DOCS) == served, (
        "routes with no documentation_url: "
        f"{sorted(served - set(gh_errors.ROUTE_DOCS))}; entries for routes that are gone: "
        f"{sorted(set(gh_errors.ROUTE_DOCS) - served)}"
    )

    missing = f"/github/repos/{gh_org}/no-such-repo"
    for path, expected in (
        (f"{missing}/branches", "rest/branches/branches#list-branches"),
        (f"{missing}/tags", "rest/repos/repos#list-repository-tags"),
        (
            f"{missing}/collaborators",
            "rest/collaborators/collaborators#list-repository-collaborators",
        ),
        (f"{missing}/pulls/1/files", "rest/pulls/pulls#list-pull-requests-files"),
        # the two routes that take the rest of the path as one parameter still resolve
        (f"{missing}/git/ref/heads/release/2026-03", "rest/git/refs#get-a-reference"),
        (
            f"{missing}/contents/src/ingest/consumer.py",
            "rest/repos/contents#get-repository-content",
        ),
    ):
        r = c.get(path, headers=gh_admin_h)
        assert r.status_code == 404, path
        assert r.json()["documentation_url"] == f"https://docs.github.com/{expected}", path


def test_github_tolerates_the_pagination_values_real_tolerates(gh_client, gh_admin_h, gh_org):
    """Real refuses no pagination value at all. Measured on a public repository's issue listing:
    `per_page=0`, `per_page=abc`, `page=0`, `page=-1` and `page=abc` are each a 200 with the
    defaults applied, and a per_page above the cap is a 200 at the cap.

    Backlot declared `ge=1` and an `int` annotation, so FastAPI answered its 422 before
    `clamp_page` was reached and a paginator computing an edge value got a hard error where
    production absorbs it. Both are gone: the parameter absorbs a value it cannot parse, and the
    OpenAPI schema stays an integer, which is what real's own spec declares — the tolerance
    belongs in the runtime where real has it, not in the contract where real does not.
    """
    c, _ = gh_client
    base = f"/github/repos/{gh_org}/codebase/issues"
    full = c.get(base, headers=gh_admin_h).json()
    for params in (
        {"per_page": 0},
        {"page": 0},
        {"page": -1},
        {"per_page": -5},
        {"per_page": "abc"},
        {"page": "abc"},
        {"page": ""},
    ):
        r = c.get(base, headers=gh_admin_h, params=params)
        assert r.status_code == 200, params
        assert r.json() == full, params
    # over the cap is still the cap, not an error
    assert c.get(base, headers=gh_admin_h, params={"per_page": 100_000}).status_code == 200
    # ...and the parameter is still declared an integer, as real's spec declares it
    route = "/github/repos/{owner}/{repo}/issues"
    spec = c.get("/openapi.json").json()["paths"][route]["get"]
    page = next(p for p in spec["parameters"] if p["name"] == "page")
    assert {"type": "integer"} in page["schema"]["anyOf"]


def test_github_a_path_parameter_it_cannot_parse_is_the_route_s_404(gh_client, gh_admin_h, gh_org):
    """Real has no route for `/issues/notanint`, so it answers the 404 that route's own anchor
    names — measured: `Not Found`, `documentation_url: .../rest/issues/issues#get-an-issue`.

    Backlot declares `number: int` and so matches the route and fails after, which is a difference
    in how the two arrive rather than in what they answer. What it must not do is answer a 422:
    `{"detail": [...]}` announced itself as the mock's own default, and an envelope does not — a
    status real never sends would read as measured.
    """
    c, _ = gh_client
    r = c.get(f"/github/repos/{gh_org}/codebase/issues/notanint", headers=gh_admin_h)
    assert r.status_code == 404
    assert r.json() == {
        "message": "Not Found",
        "documentation_url": "https://docs.github.com/rest/issues/issues#get-an-issue",
        "status": "404",
    }


def test_github_a_wrong_method_is_not_dressed_as_a_measured_answer(gh_client, gh_admin_h, gh_org):
    """Every route here declares GET, so a wrong method is refused by Starlette with
    `http.HTTPStatus(405).phrase` — a string no measurement attributes to real, which answers a
    wrong method per endpoint rather than uniformly (an unauthenticated `POST /repos/{owner}/{repo}`
    is real's 401 Requires authentication, measured).

    So it keeps FastAPI's `detail`, which says plainly that the mock is answering. The envelope is
    for the errors whose wording was measured.
    """
    c, _ = gh_client
    r = c.post(f"/github/repos/{gh_org}/codebase", headers=gh_admin_h)
    assert r.status_code == 405
    assert r.json() == {"detail": "Method Not Allowed"}


def test_github_a_validation_failure_names_the_route_it_failed_on(gh_org):
    """One route answered two `documentation_url`s for its own 422: the hand-shaped q-less search
    named `v3/search` while anything reaching FastAPI's validator named the bare root, because the
    validation envelope was not given the path. It is now, so both halves agree.

    Called directly because the pagination parameters absorb rather than refuse, which leaves no
    query parameter on this surface that still reaches the validator.
    """
    from backlot.errors import github as gh_errors

    status, body = gh_errors.validation_body(
        "/github/search/issues", [{"loc": ("query", "per_page"), "msg": "nope"}]
    )
    assert status == 422
    assert body["documentation_url"] == "https://docs.github.com/v3/search"
    assert body["errors"] == [
        {"resource": "Request", "field": "per_page", "code": "invalid", "message": "nope"}
    ]


def _link_rels(header: str) -> dict:
    """The Link header's rel -> url map (RFC5988 `<url>; rel="name"`, comma-joined)."""
    rels = {}
    for part in header.split(", "):
        url, _, rel = part.partition("; ")
        rels[rel.removeprefix('rel="').rstrip('"')] = url.strip("<>")
    return rels


@pytest.mark.parametrize(
    "path, q",
    [("/github/search/code", "extension:md"), ("/github/search/issues", "repo:diffable is:pr")],
)
def test_github_search_pages_with_a_link_header(gh_client, gh_admin_h, path, q):
    """Real pages a search response with an RFC5988 `Link`, exactly as it pages a listing, and
    sends none at all when the results fit on one page.

    A search envelope reports `total_count`, so the header is not the only way to learn there is
    more — it is how a client that FOLLOWS links pages without composing a URL of its own, which is
    what every listing on this router already gives it.
    """
    c, _ = gh_client
    first = c.get(path, headers=gh_admin_h, params={"q": q, "per_page": 2, "page": 1})
    total = first.json()["total_count"]
    assert total > 2, "the fixture has to span more than one page for this to mean anything"
    assert set(_link_rels(first.headers["Link"])) == {"next", "last"}

    # following `next` lands on the same query's second page -- the round-trip the encoding is for
    nxt = _link_rels(first.headers["Link"])["next"]
    second = c.get(nxt.split("testserver", 1)[1], headers=gh_admin_h)
    assert second.json()["total_count"] == total
    ids = lambda r: {i["url"] for i in r.json()["items"]}  # noqa: E731
    assert ids(second) and not ids(second) & ids(first)
    assert {"prev", "first"} <= set(_link_rels(second.headers["Link"]))

    # one page of results carries no Link at all, as real sends none
    assert "Link" not in c.get(path, headers=gh_admin_h, params={"q": q, "per_page": 100}).headers


def test_github_code_search_paginates(gh_client, gh_admin_h):
    c, _ = gh_client

    def page(n):
        body = c.get(
            "/github/search/code",
            headers=gh_admin_h,
            params={"q": "extension:md", "page": n, "per_page": 2},
        ).json()
        return body["total_count"], [(i["repository"]["name"], i["path"]) for i in body["items"]]

    total, first = page(1)
    # three README.md (codebase, diffable, history-repo) + unicode-repo/docs/unicode.md
    assert total == 4
    _, second = page(2)
    assert len(first) == 2 and len(second) == 2
    assert not set(first) & set(second)


def test_github_a_files_snapshots_are_one_tree_entry_served_at_head(gh_client, gh_admin_h, gh_org):
    """A path the corpus states three times is ONE file in the tree and in a directory listing, and
    the version served is the newest.

    A file is addressed by (repo, path); the extra rows are that file's history. Listing a path
    once per snapshot would not be a git tree, and picking a snapshot by scan order (which is what
    an unordered lookup did) makes the served content arbitrary.
    """
    c, _ = gh_client
    tree = c.get(
        f"/github/repos/{gh_org}/history-repo/git/trees/main",
        headers=gh_admin_h,
        params={"recursive": "1"},
    ).json()
    blobs = [e for e in tree["tree"] if e["type"] == "blob"]
    # README.md and svc/rate.py each hold several snapshots; each is ONE entry
    assert sorted(e["path"] for e in blobs) == ["README.md", "svc/other.py", "svc/rate.py"]

    body = c.get(f"/github/repos/{gh_org}/history-repo/contents/svc", headers=gh_admin_h).json()
    assert sorted(e["path"] for e in body) == ["svc/other.py", "svc/rate.py"]

    raw = c.get(
        f"/github/repos/{gh_org}/history-repo/contents/svc/rate.py",
        headers={**gh_admin_h, "Accept": "application/vnd.github.raw"},
    )
    assert raw.text == "LIMIT = 3\n"  # newest by created, not whichever row was reached first


def test_github_contents_serves_a_snapshot_by_its_stated_ref(gh_client, gh_admin_h, gh_org):
    """`?ref=` reaches a snapshot the corpus named, which is the only way an older one is
    addressable by path.

    A ref the corpus does not name keeps the documented no-history tolerance and answers HEAD,
    rather than becoming a new 404 surface: real clients pass a branch name, and a corpus that
    dates its snapshots without naming them still has to answer `?ref=main`.
    """
    c, _ = gh_client
    url = f"/github/repos/{gh_org}/history-repo/contents/svc/rate.py"
    raw = {**gh_admin_h, "Accept": "application/vnd.github.raw"}
    assert c.get(url, headers=raw, params={"ref": "pr-1"}).text == "LIMIT = 1\n"
    assert c.get(url, headers=raw, params={"ref": "pr-3"}).text == "LIMIT = 3\n"
    assert c.get(url, headers=raw, params={"ref": "main"}).text == "LIMIT = 3\n"  # unknown -> HEAD

    body = c.get(url, headers=gh_admin_h, params={"ref": "pr-1"}).json()
    assert body["path"] == "svc/rate.py"  # the file's address, not the snapshot's


def test_github_a_ref_selected_file_carries_the_ref_in_its_own_links(gh_client, gh_admin_h, gh_org):
    """A `?ref=` response has to round-trip: following its own `url` must return the same bytes.

    Real GitHub carries the ref in `url`, `_links.self`, `html_url` and `download_url`. Without it
    the body is the older snapshot while every link on it fetches HEAD — a client that follows
    `_links.self` to re-read the file it was just handed gets different content and the same `path`.
    """
    c, _ = gh_client
    url = f"/github/repos/{gh_org}/history-repo/contents/svc/rate.py"
    body = c.get(url, headers=gh_admin_h, params={"ref": "pr-1"}).json()

    assert "ref=pr-1" in body["url"]
    assert "ref=pr-1" in body["_links"]["self"]
    assert "pr-1" in body["html_url"] and "pr-1" in body["download_url"]

    # the promise those links make, kept
    again = c.get(body["url"].split("testserver", 1)[1], headers=gh_admin_h).json()
    assert again["sha"] == body["sha"]
    assert again["content"] == body["content"]

    # HEAD's own response is unchanged -- no ref, no query string
    head = c.get(url, headers=gh_admin_h).json()
    assert "?" not in head["url"] and "?" not in head["_links"]["self"]

    # /readme serves the same underlying object, so it takes the ref too
    readme = f"/github/repos/{gh_org}/history-repo/readme"
    raw = {**gh_admin_h, "Accept": "application/vnd.github.raw"}
    assert "second" in c.get(readme, headers=raw).text  # HEAD
    assert "first" in c.get(readme, headers=raw, params={"ref": "pr-1"}).text


def test_github_every_snapshot_keeps_its_own_blob(gh_client, gh_admin_h, gh_org):
    """A blob sha is content-addressed, so each snapshot already has its own and stays fetchable
    even when it is not HEAD. Nothing about this route needed to change; it is pinned because it is
    the only route on which a superseded snapshot is reachable by id."""
    import hashlib

    c, _ = gh_client
    for content in ("LIMIT = 1\n", "LIMIT = 2\n", "LIMIT = 3\n"):
        sha = hashlib.sha1(content.encode()).hexdigest()
        got = c.get(
            f"/github/repos/{gh_org}/history-repo/git/blobs/{sha}",
            headers={**gh_admin_h, "Accept": "application/vnd.github.raw"},
        )
        assert got.status_code == 200, content
        assert got.text == content


def test_github_a_files_number_never_shadows_an_issue(gh_client, gh_admin_h, gh_org):
    """A file's number must never resolve as an issue or a pull. The hazard is real and pinned by
    the fixture: `gh-file-collide-88814` seeds to exactly `gh-issue-1`'s number.

    A file row DOES carry a number now. `github_items` holds two resources with different
    natural keys — an issue at (repo, number), a file at (repo, path) — and only one pair can be
    the PRIMARY KEY, so a file draws a number too rather than keeping a NULL that would leave it
    unaddressable. What protects the issue is the ASSIGNMENT ORDER, not an exclusion: every
    provided issue/PR number claims its spelling before anything probes, so a file can only ever
    take a number no issue asked for. Its number is never served — every route filters
    `kind='file'` — and (repo, path) is what a file is addressed by."""
    from backlot import store, synth

    c, settings = gh_client
    conn = store.connect_ro(settings.db_path)
    file_rows = conn.execute(
        "SELECT repo, number, path FROM github_items WHERE kind = 'file'"
    ).fetchall()
    assert len(file_rows) > 1
    # Every file has a number, and none of them is an issue's.
    assert all(r["number"] is not None for r in file_rows)
    issue_numbers = {
        (r["repo"], r["number"])
        for r in conn.execute(
            "SELECT repo, number FROM github_items WHERE kind IS NULL OR kind != 'file'"
        )
    }
    assert not issue_numbers & {(r["repo"], r["number"]) for r in file_rows}
    conn.close()

    # the real issue is still resolvable by number even though a file doc seeds onto it
    issue_num = synth.github_number("gh-issue-1")
    assert synth.github_number("gh-file-collide-88814") == issue_num  # sanity: collision is real
    r = c.get(f"/github/repos/{gh_org}/gateway/issues/{issue_num}", headers=gh_admin_h)
    assert r.status_code == 200
    assert r.json()["title"] == "Rate limiter drops bursts under 50ms"

    pr_num = synth.github_number("gh-pr-1")
    r2 = c.get(f"/github/repos/{gh_org}/gateway/pulls/{pr_num}", headers=gh_admin_h)
    assert r2.status_code == 200
    assert r2.json()["title"] == "Fix token-bucket refill off-by-one"


def test_github_size_is_utf8_byte_length(gh_client, gh_admin_h, gh_org):
    """Real GitHub's `size` is a UTF-8 byte count, not a character count -- must differ for a
    file whose content has multi-byte characters, across the tree, contents, and blob endpoints."""
    c, _ = gh_client
    content = "héllo wörld 世界\n"
    nbytes = len(content.encode())
    assert nbytes > len(content)  # sanity: the two would only coincidentally match otherwise

    tree = c.get(
        f"/github/repos/{gh_org}/unicode-repo/git/trees/main",
        headers=gh_admin_h,
        params={"recursive": "1"},
    ).json()
    entry = next(e for e in tree["tree"] if e["path"] == "docs/unicode.md")
    assert entry["size"] == nbytes

    body = c.get(
        f"/github/repos/{gh_org}/unicode-repo/contents/docs/unicode.md", headers=gh_admin_h
    ).json()
    assert body["size"] == nbytes

    sha = hashlib.sha1(content.encode()).hexdigest()
    blob = c.get(f"/github/repos/{gh_org}/unicode-repo/git/blobs/{sha}", headers=gh_admin_h).json()
    assert blob["size"] == nbytes


def test_github_file_acl_scoped(gh_client, gh_admin_h, gh_org, gh_user_tokens):
    c, _ = gh_client
    member_h = {"Authorization": f"Bearer {gh_user_tokens['hana@acme.com']}"}  # in 'people'
    nonmember_h = {"Authorization": f"Bearer {gh_user_tokens['bob@acme.com']}"}  # not in 'people'

    def has_secret(headers):
        body = c.get(
            f"/github/repos/{gh_org}/codebase/git/trees/main",
            headers=headers,
            params={"recursive": "1"},
        ).json()
        return any(e["path"] == "config/secret.yaml" for e in body["tree"])

    assert has_secret(gh_admin_h)
    assert has_secret(member_h)
    assert not has_secret(nonmember_h)

    secret = f"/github/repos/{gh_org}/codebase/contents/config/secret.yaml"
    assert c.get(secret, headers=member_h).status_code == 200
    assert c.get(secret, headers=nonmember_h).status_code == 404
    # ...and asking for the raw bytes is not a way around it
    raw = {"Accept": "application/vnd.github.raw"}
    assert c.get(secret, headers={**member_h, **raw}).status_code == 200
    assert c.get(secret, headers={**nonmember_h, **raw}).status_code == 404


# --- media-type negotiation: Accept: application/vnd.github.raw ----------

_MAIN_PY = "def main():\n    return 1\n"
_CODEBASE_README = "# codebase\n\nCore service source, browsable via the tree/contents API.\n"


@pytest.mark.parametrize(
    "accept",
    [
        "application/vnd.github.raw",
        "application/vnd.github.v3.raw",
        "application/vnd.github.raw+json",
        # GitHub's own docs spelled it `application/vnd.github.VERSION.raw+json`; missing this one
        # meant a caller using it got the base64 envelope with a 200 and no way to tell
        "application/vnd.github.v3.raw+json",
    ],
)
def test_github_raw_media_type_returns_the_bytes(gh_client, gh_admin_h, gh_org, accept):
    """Every content endpoint, in every spelling of the header GitHub honours.

    The tell that it isn't happening is the byte count disagreeing with the `size` the tree reported
    for the same blob, so that is what these assert against rather than "some content came back".
    Real GitHub answers git/blobs with text/plain and contents/readme with vnd.github.raw — the same
    bytes either way, but the difference is GitHub's own, so it is reproduced.
    """
    c, _ = gh_client
    sha = hashlib.sha1(_MAIN_PY.encode()).hexdigest()
    for url, ctype, body in [
        (f"/github/repos/{gh_org}/codebase/git/blobs/{sha}", "text/plain", _MAIN_PY),
        (
            f"/github/repos/{gh_org}/codebase/contents/src/main.py",
            "application/vnd.github.raw",
            _MAIN_PY,
        ),
        (
            f"/github/repos/{gh_org}/codebase/readme",
            "application/vnd.github.raw",
            _CODEBASE_README,
        ),
        # 'gateway' carries no README doc, so this one exercises the synthesized-stub branch
        (f"/github/repos/{gh_org}/gateway/readme", "application/vnd.github.raw", None),
    ]:
        r = c.get(url, headers={**gh_admin_h, "Accept": accept})
        assert r.status_code == 200, url
        assert r.headers["content-type"].startswith(ctype), url
        if body is None:
            assert r.text.startswith("# gateway")
        else:
            assert r.text == body and len(r.content) == len(body.encode()), url


def test_github_raw_accept_leaves_the_json_envelope_alone(gh_client, gh_admin_h, gh_org):
    """Only a `raw` request changes shape: the default and an explicit `+json` still get the
    base64 envelope, and a DIRECTORY listing has no raw form so it stays a JSON array."""
    c, _ = gh_client
    for accept in (None, "application/vnd.github+json", "*/*"):
        h = dict(gh_admin_h) if accept is None else {**gh_admin_h, "Accept": accept}
        body = c.get(f"/github/repos/{gh_org}/codebase/contents/src/main.py", headers=h).json()
        assert body["encoding"] == "base64"
        assert base64.b64decode(body["content"]).decode() == _MAIN_PY
    dirs = c.get(
        f"/github/repos/{gh_org}/codebase/contents/src",
        headers={**gh_admin_h, "Accept": "application/vnd.github.raw"},
    )
    assert isinstance(dirs.json(), list)


# --- GET /user/repos ------------------------------------------------------


def test_github_user_repos(gh_client, gh_admin_h, gh_user_tokens, gh_org):
    """The credential's own view of what it can reach: the same set `/orgs/{org}/repos` gives an
    admin, ACL-scoped per caller, and paginated."""
    c, _ = gh_client
    body = c.get("/github/user/repos", headers=gh_admin_h).json()
    org_repos = c.get(
        f"/github/orgs/{gh_org}/repos", headers=gh_admin_h, params={"per_page": 100}
    ).json()
    assert {x["name"] for x in body} == {x["name"] for x in org_repos}
    assert all(x["full_name"] == f"{gh_org}/{x['name']}" for x in body)

    # 'vault' holds one group-visible issue owned by 'people', which bob is not in
    bob_h = {"Authorization": f"Bearer {gh_user_tokens['bob@acme.com']}"}
    bob = {x["name"] for x in c.get("/github/user/repos", headers=bob_h).json()}
    assert "vault" in {x["name"] for x in body} and "vault" not in bob
    assert bob < {x["name"] for x in body}

    # ...and the repo's own routes agree with the listing. A repo every one of whose documents is
    # hidden from this caller is one they must not be able to confirm the existence of, so "does
    # this repo exist" is answered against the CALLER's view, not the corpus's.
    for path in (
        "",
        "/issues",
        "/pulls",
        "/readme",
        "/git/trees/main",
        "/git/ref/heads/main",
        "/branches",
        "/branches/main",
        "/tags",
        "/commits/deadbeef",
        "/contents",
        "/collaborators",
        "/teams",
    ):
        assert c.get(f"/github/repos/{gh_org}/vault{path}", headers=bob_h).status_code == 404, path
        assert c.get(f"/github/repos/{gh_org}/vault{path}", headers=gh_admin_h).status_code == 200

    page = c.get("/github/user/repos", headers=gh_admin_h, params={"per_page": 1, "page": 1})
    assert len(page.json()) == 1 and 'rel="next"' in page.headers.get("Link", "")


def test_github_repo_carries_a_url_template_for_each_resource_it_serves(
    gh_client, gh_admin_h, gh_org
):
    """An SDK completes a repository lazily by expanding these templates — PyGithub does it for the
    example this repo ships — so a repo object without them makes the client assemble URLs itself,
    which is the thing hypermedia is for. All derivable from owner/repo; no stored data.

    Values, not just keys: a template whose placeholder is wrong (`{sha}` where real says `{/sha}`)
    expands to a URL that 404s, which is the same dead end as omitting the field.

    THE RULE IS "a template iff the resource": real serves 42 of these and Backlot has routes for a
    third of them, so the rest stay absent rather than inviting a client to follow a link into a 404.
    A key set that lies about what can be fetched is worse for the caller than a short one — and the
    caller can tell the difference, which is the whole point of hypermedia. Adding a route later
    means adding its template here."""
    c, _ = gh_client
    repo = c.get(f"/github/repos/{gh_org}/gateway", headers=gh_admin_h).json()
    api = f"/github/repos/{gh_org}/gateway"
    for field, expected in {
        "pulls_url": f"{api}/pulls{{/number}}",
        "issues_url": f"{api}/issues{{/number}}",
        "issue_comment_url": f"{api}/issues/comments{{/number}}",
        "contents_url": f"{api}/contents/{{+path}}",
        "blobs_url": f"{api}/git/blobs{{/sha}}",
        "trees_url": f"{api}/git/trees{{/sha}}",
        "branches_url": f"{api}/branches{{/branch}}",
        "tags_url": f"{api}/tags",
        "commits_url": f"{api}/commits{{/sha}}",
        "statuses_url": f"{api}/statuses/{{sha}}",
        "collaborators_url": f"{api}/collaborators{{/collaborator}}",
        "teams_url": f"{api}/teams",
    }.items():
        assert repo[field].endswith(expected), f"{field}: {repo[field]}"
    # the git-protocol URLs name github.com, not Backlot, so they promise it nothing
    assert repo["clone_url"] == f"https://github.com/{gh_org}/gateway.git"
    assert repo["ssh_url"] == f"git@github.com:{gh_org}/gateway.git"
    assert repo["git_url"] == f"git://github.com/{gh_org}/gateway.git"
    assert repo["svn_url"] == f"https://github.com/{gh_org}/gateway"
    # real serves these; Backlot has no such route, so it does not advertise one
    unserved = {
        "archive_url",
        "assignees_url",
        "comments_url",
        "compare_url",
        "contributors_url",
        "deployments_url",
        "downloads_url",
        "events_url",
        "forks_url",
        "git_commits_url",
        "git_refs_url",  # Backlot serves `/git/ref/{ref}`, not real's plural `/git/refs{/sha}`
        "git_tags_url",
        "hooks_url",
        "issue_events_url",
        "keys_url",
        "labels_url",
        "languages_url",
        "merges_url",
        "milestones_url",
        "notifications_url",
        "releases_url",
        "stargazers_url",
        "subscribers_url",
        "subscription_url",
    }
    assert not unserved & set(repo), sorted(unserved & set(repo))
    # nothing invented either: the engagement counters real serves are absent, not made up
    assert not {"stargazers_count", "forks", "watchers", "language", "topics"} & set(repo)


def test_github_pull_sub_resources_the_new_links_point_at(gh_client, gh_admin_h, gh_org):
    """`_links.commits`/`statuses` name resources a client is invited to follow, so the routes have
    to exist — an emitted URL that 404s is a worse deal for the caller than an absent field.

    `commits` is the one commit the pull object already claims; `statuses` is empty because Backlot
    has no CI, which is what real answers for a sha nobody reported a status on."""
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-pr-1")
    base = f"/github/repos/{gh_org}/gateway"
    pull = c.get(f"{base}/pulls/{num}", headers=gh_admin_h).json()
    commits = c.get(f"{base}/pulls/{num}/commits", headers=gh_admin_h).json()
    assert len(commits) == pull["commits"] == 1
    # the pull's head IS that commit, so the sha a client follows here is the one it already has
    assert commits[0]["sha"] == pull["head"]["sha"]
    assert commits[0]["author"]["login"] == pull["user"]["login"]
    assert commits[0]["commit"]["message"] == pull["title"]
    # `commit.author` is a git author (name/email/date), which is not the GitHub user object
    assert set(commits[0]["commit"]["author"]) == {"name", "email", "date"}
    assert commits[0]["commit"]["author"]["date"] == pull["created_at"]
    statuses = c.get(f"{base}/statuses/{pull['head']['sha']}", headers=gh_admin_h)
    assert statuses.status_code == 200 and statuses.json() == []


# --- GET /repos/{o}/{r}/git/ref/{ref} -------------------------------------


def test_github_git_ref_resolves_a_ref_to_a_commit(gh_client, gh_admin_h, gh_org):
    """Resolving a branch to a commit sha, including one whose name contains a slash — the whole
    point of this route over `/branches/{branch}`, which cannot carry that in one path segment."""
    c, _ = gh_client
    r = c.get(f"/github/repos/{gh_org}/codebase/git/ref/heads/main", headers=gh_admin_h)
    assert r.status_code == 200
    body = r.json()
    assert body["ref"] == "refs/heads/main" and body["object"]["type"] == "commit"
    branch = c.get(f"/github/repos/{gh_org}/codebase/branches/main", headers=gh_admin_h).json()
    assert body["object"]["sha"] == branch["commit"]["sha"]  # the two must agree

    slashed = c.get(
        f"/github/repos/{gh_org}/codebase/git/ref/heads/release/2026-03", headers=gh_admin_h
    ).json()
    assert slashed["ref"] == "refs/heads/release/2026-03"
    # the sha it hands back is usable as a git/trees ref, which is what a pinning client does next
    tree = c.get(
        f"/github/repos/{gh_org}/codebase/git/trees/{slashed['object']['sha']}", headers=gh_admin_h
    )
    assert tree.status_code == 200

    unknown = c.get(f"/github/repos/{gh_org}/no-such-repo/git/ref/heads/main", headers=gh_admin_h)
    assert unknown.status_code == 404


# --- owner validation -----------------------------------------------------


def test_github_validates_the_owner_segment(gh_client, gh_admin_h, gh_org):
    """Real GitHub 404s on a wrong owner; echoing it back lets a client's owner-handling bug pass
    here and fail in production. Case-insensitive, as GitHub logins are, and the `{org}` segment
    is held to the same rule."""
    c, _ = gh_client
    for path in (
        "",
        "/issues",
        "/pulls",
        "/readme",
        "/contents/README.md",
        "/git/trees/main",
        "/branches",
        "/branches/main",
        "/tags",
        "/collaborators",
    ):
        r = c.get(f"/github/repos/not-the-owner/codebase{path}", headers=gh_admin_h)
        assert r.status_code == 404, f"wrong owner accepted at {path!r}"
        ok = c.get(f"/github/repos/{gh_org}/codebase{path}", headers=gh_admin_h)
        assert ok.status_code == 200, f"right owner rejected at {path!r}"

    assert c.get(f"/github/repos/{gh_org.upper()}/codebase", headers=gh_admin_h).status_code == 200
    assert c.get("/github/orgs/not-the-org", headers=gh_admin_h).status_code == 404
    assert c.get("/github/orgs/not-the-org/repos", headers=gh_admin_h).status_code == 404


# --- X-GitHub-Api-Version negotiation -----------------------------------------
#
# The two versions real GitHub currently supports, and the only field-level difference between them
# on this surface. Both were read off api.github.com rather than the docs: `2026-03-10` drops
# `assignee` (issues and pulls, superseded by `assignees`) and `merge_commit_sha` (pulls).


@pytest.mark.parametrize(
    "pinned, serves_removed_fields",
    [(None, True), ("2022-11-28", True), ("2026-03-10", False)],
    ids=["unpinned-defaults-to-2022-11-28", "2022-11-28", "2026-03-10"],
)
def test_github_api_version_selects_the_payload(
    gh_client, gh_admin_h, gh_org, pinned, serves_removed_fields
):
    """The header picks which body shape is served, and every response says which it chose.

    Accepting the header and ignoring it is worse than not supporting it: a client that pins a
    version gets a payload from another one with no way to tell. Unpinned is `2022-11-28`, which is
    what real GitHub defaults an unpinned request to."""
    c, _ = gh_client
    from backlot import synth

    h = {**gh_admin_h, **({"X-GitHub-Api-Version": pinned} if pinned else {})}
    num = synth.github_number("gh-pr-1")
    base = f"/github/repos/{gh_org}/gateway"
    for path, removed in (
        (f"{base}/pulls/{num}", ("assignee", "merge_commit_sha")),
        (f"{base}/issues/{num}", ("assignee",)),
    ):
        r = c.get(path, headers=h)
        assert r.status_code == 200, path
        assert r.headers["X-GitHub-Api-Version-Selected"] == (pinned or "2022-11-28")
        for field in removed:
            assert (field in r.json()) is serves_removed_fields, f"{path}: {field}"
    # the listings serve the same shape as the single-object routes they page over
    listing = c.get(f"{base}/pulls", headers=h, params={"state": "all"}).json()
    assert listing and all(("assignee" in p) is serves_removed_fields for p in listing)


def test_github_unsupported_api_version_is_refused_ahead_of_everything(gh_client, gh_org):
    """An unsupported version is a malformed request, so real answers it before authenticating and
    before routing — verified against api.github.com, which 400s a bad version on a nonexistent repo
    with no credentials at all. Running this check after either one would report a client's version
    typo as 401 or 404 and send them looking in the wrong place.

    Real sends no `Selected` echo on this 400 (it selected nothing), and does send one on a 404."""
    c, _ = gh_client
    bad = {"X-GitHub-Api-Version": "1999-01-01"}
    r = c.get(f"/github/repos/{gh_org}/gateway/pulls/1", headers=bad)
    assert r.status_code == 400
    body = r.json()
    assert body["message"] == "Bad Request" and body["status"] == "400"
    assert '"1999-01-01"' in body["errors"] and "is not a supported version" in body["errors"]
    assert '"2026-03-10" (most recent) and "2022-11-28"' in body["errors"]
    assert "X-GitHub-Api-Version-Selected" not in r.headers
    # no credentials, and an owner Backlot does not serve: still the version's 400
    assert c.get("/github/repos/nope/nope/pulls/1", headers=bad).status_code == 400
    assert c.get("/github/search/issues", headers=bad, params={"q": "x"}).status_code == 400


# --- a pull is a pull, not an issue with extra keys -------------


def test_github_pull_and_issue_views_are_distinct_objects(gh_client, gh_admin_h, gh_org):
    """Real GitHub models a PR's issue view and its pull view as two distinct nodes with two
    distinct field sets, so neither the id nor the body may be the other's.

    The pull carries hypermedia an issue does not (`_links`, `review_comments_url`, …) and carries
    none of the issue-only fields. `pull_request` is the clearest of those: that marker exists to
    tell a caller an ISSUE is really a pull, and a pull has no reason to point at itself. Both key
    sets were diffed against api.github.com."""
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-pr-1")
    pull = c.get(f"/github/repos/{gh_org}/gateway/pulls/{num}", headers=gh_admin_h).json()
    decoded = base64.b64decode(pull["node_id"] + "==").decode()
    assert "PullRequest" in decoded and "Issue" not in decoded

    issue = c.get(f"/github/repos/{gh_org}/gateway/issues/{num}", headers=gh_admin_h).json()
    assert "pull_request" in issue  # sanity: this is the PR seen as an issue
    assert "Issue" in base64.b64decode(issue["node_id"] + "==").decode()

    assert ISSUE_ONLY_FIELDS.isdisjoint(pull), sorted(ISSUE_ONLY_FIELDS & set(pull))
    assert ISSUE_ONLY_FIELDS <= set(issue), sorted(ISSUE_ONLY_FIELDS - set(issue))
    assert PULL_ONLY_FIELDS <= set(pull), sorted(PULL_ONLY_FIELDS - set(pull))
    assert PULL_ONLY_FIELDS.isdisjoint(issue), sorted(PULL_ONLY_FIELDS & set(issue))
    # ...and the listing serves the same object the single-pull route does
    listed = c.get(
        f"/github/repos/{gh_org}/gateway/pulls", headers=gh_admin_h, params={"state": "all"}
    ).json()
    assert set(listed[0]) == set(pull)

    # `_links` is the hypermedia the field set exists for: every href is one of the pull's own
    # sub-resources, and `review_comment` stays the template real serves.
    links = pull["_links"]
    assert links["self"]["href"] == pull["url"] and links["issue"]["href"] == pull["issue_url"]
    assert links["html"]["href"] == pull["html_url"]
    assert links["review_comments"]["href"] == pull["review_comments_url"]
    assert links["review_comment"]["href"] == pull["review_comment_url"]
    assert links["review_comment"]["href"].endswith("/pulls/comments{/number}")
    assert links["commits"]["href"] == pull["commits_url"]
    assert links["statuses"]["href"] == pull["statuses_url"]
    assert pull["statuses_url"].endswith("/statuses/" + pull["head"]["sha"])
    assert pull["auto_merge"] is None and pull["maintainer_can_modify"] is False


# --- pull changeset: /pulls/{n}/files and the diff media types --------


@pytest.fixture(scope="module")
def diff_pr(gh_client, gh_admin_h, gh_org):
    """(client, headers, org, pull_number) for the 'diffable' repo's synthesized-changeset PR."""
    c, _ = gh_client
    from backlot import synth

    return c, gh_admin_h, gh_org, synth.github_number("gh-diff-pr")


@pytest.fixture(scope="module")
def declared_pr(gh_client, gh_admin_h, gh_org):
    """Same, for the pull that declares its own changeset via `changed_paths`."""
    c, _ = gh_client
    from backlot import synth

    return c, gh_admin_h, gh_org, synth.github_number("gh-diff-pr-declared")


def test_github_pull_files_lists_the_changed_files(diff_pr):
    """The real API's shape, agreeing with the pull object's own counts — a contradiction between
    it — and stable across calls, since the whole changeset is derived from the pull's served key."""
    c, h, org, num = diff_pr
    r = c.get(f"/github/repos/{org}/diffable/pulls/{num}/files", headers=h)
    assert r.status_code == 200
    files = r.json()
    assert files, "a PR in a repo with file docs must report a changeset"
    tree_paths = {
        e["path"]
        for e in c.get(
            f"/github/repos/{org}/diffable/git/trees/main", headers=h, params={"recursive": "1"}
        ).json()["tree"]
    }
    for f in files:
        assert {"filename", "status", "additions", "deletions", "changes", "sha"} <= set(f)
        assert f["filename"] in tree_paths  # never invents a path the repo doesn't have
        # not "removed": the snapshot is the pull's head, and a file named as removed would still
        # be in the tree — see the changeset note in the router
        assert f["status"] in ("added", "modified")
        assert f["changes"] == f["additions"] + f["deletions"]
        assert f["blob_url"] and f["raw_url"] and f["contents_url"]

    pull = c.get(f"/github/repos/{org}/diffable/pulls/{num}", headers=h).json()
    assert pull["changed_files"] == len(files)
    assert pull["additions"] == sum(f["additions"] for f in files)
    assert pull["deletions"] == sum(f["deletions"] for f in files)
    assert c.get(f"/github/repos/{org}/diffable/pulls/{num}/files", headers=h).json() == files


@pytest.mark.parametrize("which", ["synthesized", "declared"])
def test_github_pull_diff_reverse_applies_with_real_git(
    gh_client, gh_admin_h, gh_org, tmp_path, which
):
    """The claim a synthesized diff has to earn: real `git apply` accepts it.

    The snapshot Backlot serves IS the pull's head, so `git apply --reverse` must be able to walk
    it back to the base. Nothing weaker proves it — a hunk header off by one line, or context that
    doesn't match the file, still looks like a diff and still passes a shape assertion. Run for both
    changeset kinds: declaring the files changes WHICH files are in the diff, not whether the hunks
    are real.
    """
    import shutil
    import subprocess

    from backlot import synth

    if shutil.which("git") is None:  # pragma: no cover - git is present everywhere this runs
        pytest.skip("git not available")
    c, _ = gh_client
    num = synth.github_number("gh-diff-pr" if which == "synthesized" else "gh-diff-pr-declared")
    diff = c.get(
        f"/github/repos/{gh_org}/diffable/pulls/{num}",
        headers={**gh_admin_h, "Accept": "application/vnd.github.diff"},
    ).text
    assert diff, "the changeset must not be empty for this repo"

    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
    tree = c.get(
        f"/github/repos/{gh_org}/diffable/git/trees/main",
        headers=gh_admin_h,
        params={"recursive": "1"},
    ).json()["tree"]
    for e in tree:
        if e["type"] != "blob":
            continue
        raw = c.get(
            f"/github/repos/{gh_org}/diffable/contents/{e['path']}",
            headers={**gh_admin_h, "Accept": "application/vnd.github.raw"},
        ).text
        dest = wt / e["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(raw)
    (wt / "pr.diff").write_text(diff)
    r = subprocess.run(
        ["git", "apply", "--reverse", "--check", "-v", "pr.diff"],
        cwd=wt,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"git rejected the diff:\n{r.stderr}\n---\n{diff}"


def test_github_pull_files_empty_when_the_repo_has_no_file_docs(tmp_path):
    """No file docs means no snapshot to diff against, so the changeset is empty rather than
    invented — and the pull object's counts follow it down to zero.

    Driven through the builders rather than a client: this file's two module-scoped clients share
    ``backlot.main.app``'s state (see ``client_for``), so "a corpus with no file docs" is not
    something an HTTP test here can rely on."""
    from backlot.routers.github import _pr_files, _pr_obj

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "github",
                "doc_id": "pr-nofiles",
                "repo": "bare",
                "title": "PR against a repo with no code",
                "content": "body",
                "author_email": "a@x.com",
                "subtype": "pull_request",
            }
        ],
    )
    conn = store.connect_ro(s.db_path)
    row = _gh_row(conn, "PR against a repo with no code")
    assert _pr_files(conn, "org", "bare", row, "http://m/github") == []
    pr = _pr_obj(conn, "org", "bare", row, "http://m/github")
    assert (pr["changed_files"], pr["additions"], pr["deletions"]) == (0, 0, 0)


def test_github_pull_accept_diff_returns_a_unified_diff(diff_pr, gh_admin_h, gh_org):
    """`…diff` is a representation of the pull resource, so the default `Accept` still gets JSON and
    an ISSUE — which has no diff — gets its JSON too, as real GitHub does."""
    c, h, org, num = diff_pr
    r = c.get(
        f"/github/repos/{org}/diffable/pulls/{num}",
        headers={**h, "Accept": "application/vnd.github.diff"},
    )
    assert r.status_code == 200
    assert not r.text.startswith("{"), "a diff request must not get the pull's JSON"
    assert "diff" in r.headers["content-type"]
    files = c.get(f"/github/repos/{org}/diffable/pulls/{num}/files", headers=h).json()
    for f in files:
        assert f"diff --git a/{f['filename']} b/{f['filename']}" in r.text
        if f.get("patch"):
            assert f["patch"] in r.text

    body = c.get(f"/github/repos/{org}/diffable/pulls/{num}", headers=h).json()
    assert body["number"] == num and body["title"] == "Tighten the run() argv handling"

    from backlot import synth

    issue_num = synth.github_number("gh-issue-1")
    issue = c.get(
        f"/github/repos/{gh_org}/gateway/issues/{issue_num}",
        headers={**gh_admin_h, "Accept": "application/vnd.github.diff"},
    ).json()
    assert issue["number"] == issue_num


def test_github_pull_accept_patch_returns_an_mbox_patch(diff_pr):
    """The `patch` media type is a git-am-able mail patch, not the same bytes as `diff`."""
    c, h, org, num = diff_pr
    r = c.get(
        f"/github/repos/{org}/diffable/pulls/{num}",
        headers={**h, "Accept": "application/vnd.github.patch"},
    )
    assert r.status_code == 200
    assert r.text.startswith("From ")
    assert "Subject: [PATCH] Tighten the run() argv handling" in r.text
    assert "diff --git " in r.text


def test_github_oversized_patch_is_omitted_from_json_but_kept_in_the_diff(diff_pr, monkeypatch):
    """`patch` being omitted is a limit on the JSON file object, not on the diff — real GitHub's
    `.diff` still carries the hunks. Applying the cap when the hunk is BUILT left the diff with a
    `diff --git` header and no body, which real git rejects as garbage rather than as a diff."""
    from backlot.routers import github as gh

    c, h, org, num = diff_pr
    monkeypatch.setattr(gh, "PATCH_MAX_BYTES", 1)  # every patch counts as oversized
    files = c.get(f"/github/repos/{org}/diffable/pulls/{num}/files", headers=h).json()
    assert files and all("patch" not in f for f in files)
    # ...but the counts still describe the change, and the diff still carries the hunks
    assert any(f["additions"] or f["deletions"] for f in files)
    diff = c.get(
        f"/github/repos/{org}/diffable/pulls/{num}",
        headers={**h, "Accept": "application/vnd.github.diff"},
    ).text
    assert "@@ " in diff


def test_github_diff_never_emits_a_file_header_with_no_body(tmp_path):
    """A `diff --git` header with nothing after it is not an empty diff — real git calls it garbage
    and refuses the whole patch, so a file with no hunk at all is left out instead."""
    import shutil
    import subprocess

    from backlot.routers.github import _pr_diff

    diff = _pr_diff(
        [
            {"sha": "a" * 40, "filename": "nohunk.bin", "status": "modified"},
            {
                "sha": "b" * 40,
                "filename": "ok.txt",
                "status": "added",
                "patch": "@@ -0,0 +1,1 @@\n+hello\n",
            },
        ],
        "c" * 40,
    )
    assert "nohunk.bin" not in diff and "ok.txt" in diff
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git not available")
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
    (wt / "ok.txt").write_text("hello\n")
    (wt / "pr.diff").write_text(diff)
    r = subprocess.run(
        ["git", "apply", "--reverse", "--check", "pr.diff"], cwd=wt, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr


# --- a corpus-declared changeset: `changed_paths` ---------------------------------------


def test_github_declared_changeset_is_what_the_corpus_said(declared_pr):
    """With `changed_paths` the changeset is what the corpus says it is — same order, all of them,
    not the deterministic pick and not capped at three — and the pull object's counts follow it.

    Every declared file is `modified`: a corpus naming a path says the pull CHANGED a file the repo
    already has, and reporting it as `added` would claim the pull created it, which is more than the
    corpus said. A synthesized changeset still varies, so `added` stays exercisable."""
    c, h, org, num = declared_pr
    files = c.get(f"/github/repos/{org}/diffable/pulls/{num}/files", headers=h).json()
    assert [f["filename"] for f in files] == [
        "pkg/core.py",
        "app.py",
        "pkg/conf.toml",
        "README.md",
    ]
    assert {f["status"] for f in files} == {"modified"}

    pull = c.get(f"/github/repos/{org}/diffable/pulls/{num}", headers=h).json()
    assert pull["changed_files"] == len(files) == 4
    assert pull["additions"] == sum(f["additions"] for f in files)
    assert pull["deletions"] == sum(f["deletions"] for f in files)


def test_github_declared_path_the_caller_cannot_see_is_dropped(
    gh_client, gh_admin_h, gh_user_tokens, gh_org
):
    """A declared path is still ACL-resolved: declaring a file does not publish its name. Stronger
    than the same check on a synthesized changeset, which might not have picked the restricted file
    at all."""
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-diff-pr-restricted")
    bob = {"Authorization": f"Bearer {gh_user_tokens['bob@acme.com']}"}  # not in 'people'
    url = f"/github/repos/{gh_org}/diffable/pulls/{num}/files"
    assert [f["filename"] for f in c.get(url, headers=gh_admin_h).json()] == [
        "app.py",
        "secret/keys.txt",
    ]
    assert [f["filename"] for f in c.get(url, headers=bob).json()] == ["app.py"]
    # the pull object's counts follow the CALLER's view, not the corpus's declaration
    pull = c.get(f"/github/repos/{gh_org}/diffable/pulls/{num}", headers=bob).json()
    assert pull["changed_files"] == 1


def test_github_declared_paths_are_resolved_and_deduplicated(tmp_path):
    """A path naming no file in the repo cannot be diffed, so it is skipped rather than emitted as a
    file with no content — indistinguishable from an ACL-hidden one at this layer, which is why both
    behave the same way. A path named twice would put the same file in the diff twice, which
    `git apply` refuses outright."""
    from backlot.routers.github import _pr_files

    def files_for(changed_paths):
        s = tiny_corpus(
            tmp_path / str(abs(hash(tuple(changed_paths)))),
            [
                {
                    "source_type": "github",
                    "doc_id": "f1",
                    "repo": "r",
                    "subtype": "file",
                    "path": "real.py",
                    "title": "real.py",
                    "content": "a\nb\nc\n",
                    "author_email": "a@x.com",
                },
                {
                    "source_type": "github",
                    "doc_id": "p1",
                    "repo": "r",
                    "subtype": "pull_request",
                    "title": "PR",
                    "content": "body",
                    "author_email": "a@x.com",
                    "changed_paths": changed_paths,
                },
            ],
        )
        conn = store.connect_ro(s.db_path)
        row = _gh_row(conn, "PR")
        return [f["filename"] for f in _pr_files(conn, "org", "r", row, "http://m/github")]

    assert files_for(["real.py", "typo.py"]) == ["real.py"]
    assert files_for(["real.py", "real.py"]) == ["real.py"]


def test_github_pull_files_paginates(declared_pr):
    """Real GitHub paginates the changed-file list; a client's paging loop over it is only
    exercisable if Backlot emits the Link header — and a synthesized changeset caps at three files,
    so a declared one is what makes a second page reachable at all."""
    c, h, org, num = declared_pr
    url = f"/github/repos/{org}/diffable/pulls/{num}/files"
    first = c.get(url, headers=h, params={"per_page": 2, "page": 1})
    assert [f["filename"] for f in first.json()] == ["pkg/core.py", "app.py"]
    assert 'rel="next"' in first.headers.get("Link", "")
    second = c.get(url, headers=h, params={"per_page": 2, "page": 2})
    assert [f["filename"] for f in second.json()] == ["pkg/conf.toml", "README.md"]
    assert 'rel="next"' not in second.headers.get("Link", "")

    unpaged = c.get(url, headers=h).json()
    walked, page = [], 1
    while True:
        r = c.get(url, headers=h, params={"per_page": 3, "page": page})
        walked += r.json()
        if 'rel="next"' not in r.headers.get("Link", ""):
            break
        page += 1
    assert walked == unpaged


# --- line-anchored review comments ---------------------------------------


def test_github_pull_review_comments_are_served(declared_pr):
    """The real API's shape, anchored where the corpus said. `diff_hunk` comes from the corpus when
    it supplied one and is otherwise derived from the file's own snapshot, the same principle as the
    changeset; `position` indexes INTO that hunk, so the row it selects is the commented line."""
    c, h, org, num = declared_pr
    body = c.get(f"/github/repos/{org}/diffable/pulls/{num}/comments", headers=h).json()
    assert [(x["path"], x["line"]) for x in body] == [("pkg/core.py", 4), ("app.py", None)]

    derived = body[0]
    assert derived["body"] == "this line should be a constant"
    assert derived["side"] == "RIGHT" and derived["commit_id"]
    assert derived["user"]["login"] == "ava"
    assert derived["pull_request_url"].endswith(f"/pulls/{num}")
    assert derived["html_url"].endswith(f"#discussion_r{derived['id']}")
    assert derived["diff_hunk"].startswith("@@ ")
    assert "line_4 = 4" in derived["diff_hunk"]  # real content from the file, around line 4
    rows = derived["diff_hunk"].split("\n")
    assert rows[derived["position"]].lstrip(" +") == "line_4 = 4"

    explicit = body[1]
    assert explicit["diff_hunk"] == "@@ -1,2 +1,3 @@\n import sys\n"  # corpus wins
    # a file-level comment has no line to index into the hunk with
    assert explicit["position"] is None and explicit["subject_type"] == "file"


def test_github_pull_review_comments_are_a_separate_resource(gh_client, gh_admin_h, gh_org):
    """A pull with none answers `[]` — the collection is a real resource, and a 404
    return aborts any client that renders a pull from its four sub-resources. A non-pull has no such
    resource at all, and the anchored comments must never leak into the conversation endpoint."""
    c, _ = gh_client
    from backlot import synth

    pr_num = synth.github_number("gh-pr-1")  # a PR with no anchored comments
    r = c.get(f"/github/repos/{gh_org}/gateway/pulls/{pr_num}/comments", headers=gh_admin_h)
    assert r.status_code == 200 and r.json() == []

    issue_num = synth.github_number("gh-issue-1")  # an issue, not a PR
    assert (
        c.get(
            f"/github/repos/{gh_org}/gateway/pulls/{issue_num}/comments", headers=gh_admin_h
        ).status_code
        == 404
    )

    declared = synth.github_number("gh-diff-pr-declared")
    convo = c.get(
        f"/github/repos/{gh_org}/diffable/issues/{declared}/comments", headers=gh_admin_h
    ).json()
    assert [x["body"] for x in convo] == ["conversation, not anchored"]
    assert all("path" not in x for x in convo)


def test_github_comment_by_id_refuses_what_its_collection_would_not_serve(
    gh_client, gh_admin_h, gh_user_tokens, gh_org
):
    """A comment's own `url` is a second way to reach it, so it has to refuse everything the
    collection refuses — otherwise it is a way around the list's ACL and its two-resource split.

    404 for all of it: a comment of the other kind, one under the wrong repo, and one anchored to a
    file the caller cannot read must be indistinguishable from one that does not exist."""
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-diff-pr-declared")
    review = c.get(f"/github/repos/{gh_org}/diffable/pulls/{num}/comments", headers=gh_admin_h)
    rc_id = review.json()[0]["id"]
    convo = c.get(f"/github/repos/{gh_org}/diffable/issues/{num}/comments", headers=gh_admin_h)
    ic_id = convo.json()[0]["id"]

    # each id resolves under its own kind...
    assert (
        c.get(f"/github/repos/{gh_org}/diffable/pulls/comments/{rc_id}", headers=gh_admin_h).json()[
            "path"
        ]
        == "pkg/core.py"
    )
    assert (
        c.get(
            f"/github/repos/{gh_org}/diffable/issues/comments/{ic_id}", headers=gh_admin_h
        ).json()["body"]
        == "conversation, not anchored"
    )
    # ...and nowhere else: not as the other kind, not under another repo
    for url in (
        f"/github/repos/{gh_org}/diffable/issues/comments/{rc_id}",
        f"/github/repos/{gh_org}/diffable/pulls/comments/{ic_id}",
        f"/github/repos/{gh_org}/codebase/pulls/comments/{rc_id}",
        f"/github/repos/{gh_org}/diffable/pulls/comments/999999",
    ):
        assert c.get(url, headers=gh_admin_h).status_code == 404, url

    # a review comment anchored to a people-only file: the collection drops it for bob, so by-id
    # must too
    unres = synth.github_number("gh-diff-pr-unresolvable")
    hidden = next(
        x
        for x in c.get(
            f"/github/repos/{gh_org}/diffable/pulls/{unres}/comments", headers=gh_admin_h
        ).json()
        if x["path"] == "secret/keys.txt"
    )
    bob = {"Authorization": f"Bearer {gh_user_tokens['bob@acme.com']}"}
    by_id = f"/github/repos/{gh_org}/diffable/pulls/comments/{hidden['id']}"
    assert c.get(by_id, headers=gh_admin_h).status_code == 200
    assert c.get(by_id, headers=bob).status_code == 404


def test_github_comment_ids_are_unique_even_when_the_seed_collides(tmp_path, monkeypatch):
    """A comment's `id` is ASSIGNED at import, not hashed at serve time.

    A hash alone collides by the birthday bound — ~4% at 27k comments, certain by 500k — and two
    comments sharing an id means one comment's `url` returns the other's body. The seed is probed
    until free, so the ids are unique however badly it collides. Forced here by collapsing the seed
    to a single value, since a real collision needs ~100k comments to be likely.

    Re-importing the same corpus does NOT keep the ids it assigned, and the second half pins what
    happens instead: an append into a source whose keys are probed is REFUSED unless the record
    states its own identity. Nothing is left to recognise a row by, so the alternative is adding it
    a second time in silence."""
    from backlot.importer import byo

    monkeypatch.setattr(byo.synth, "github_comment_id", lambda cid: 7)  # every seed collides
    settings = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "github",
                "doc_id": "p1",
                "repo": "r",
                "subtype": "pull_request",
                "title": "PR",
                "content": "body",
                "author_email": "a@x.com",
                "comments": [{"content": f"c{i}", "author_email": "a@x.com"} for i in range(5)],
            }
        ],
    )
    monkeypatch.undo()

    conn = store.connect_ro(settings.db_path)
    served = [r["id"] for r in conn.execute("SELECT id FROM github_comments")]
    assert len(served) == 5 and len(set(served)) == 5 and all(served)
    for sid in served:  # each still resolves to its own comment
        assert store.get_github_comment(conn, sid)["id"] == sid
    conn.close()

    with pytest.raises(SystemExit) as e:
        byo.load(settings.data_dir / "corpus.jsonl", settings, reset=False)
    assert "must carry `number`" in str(e.value)
    # ...and nothing was written by the refused append.
    conn = store.connect_ro(settings.db_path)
    assert sorted(r["id"] for r in conn.execute("SELECT id FROM github_comments")) == sorted(served)
    conn.close()


def test_github_comment_counts_match_the_lists_they_describe(
    gh_client, gh_admin_h, gh_user_tokens, gh_org
):
    """`comments` counts the conversation and `review_comments` the anchored ones, as real GitHub
    reports them — one number covering both would contradict whichever list you fetched.

    `review_comments` counts what the LIST returns, which drops a comment anchored to a file the
    caller cannot read: counting the raw rows made the two contradict each other, never terminated a
    client paging until it had that many, and leaked that a hidden file carries a comment."""
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-diff-pr-declared")
    pull = c.get(f"/github/repos/{gh_org}/diffable/pulls/{num}", headers=gh_admin_h).json()
    assert pull["comments"] == 1 and pull["review_comments"] == 2
    issue = c.get(f"/github/repos/{gh_org}/diffable/issues/{num}", headers=gh_admin_h).json()
    assert issue["comments"] == 1  # the issue view counts the conversation only

    # 'gone/x.py' is in no tree and 'secret/keys.txt' is people-only, so the count has to shrink
    # with the list — differently for each caller
    unres = synth.github_number("gh-diff-pr-unresolvable")
    bob = {"Authorization": f"Bearer {gh_user_tokens['bob@acme.com']}"}
    for headers, expected in ((gh_admin_h, ["app.py", "secret/keys.txt"]), (bob, ["app.py"])):
        body = c.get(f"/github/repos/{gh_org}/diffable/pulls/{unres}/comments", headers=headers)
        obj = c.get(f"/github/repos/{gh_org}/diffable/pulls/{unres}", headers=headers).json()
        assert [x["path"] for x in body.json()] == expected
        assert obj["review_comments"] == len(expected)


def test_hunk_position_indexes_into_the_hunk():
    """`position` and `line` are different numbers on real GitHub — the row's offset within the diff
    hunk versus the line in the file — and a client resolving a comment against a diff uses the
    former. They coincide only for a hunk starting at line 1 with nothing removed above, so this
    pins the distinction on a hunk where they cannot."""
    from backlot.routers.github import _hunk_around, _hunk_position

    hunk = "@@ -8,4 +10,5 @@\n ctx_a\n-gone\n+added\n ctx_b\n ctx_c\n"
    #        new-side lines:      10       (none)  11      12      13
    assert _hunk_position(hunk, 10) == 1
    assert _hunk_position(hunk, 11) == 3  # offset 2 is the removed row, which has no new-side line
    assert _hunk_position(hunk, 13) == 5
    assert _hunk_position(hunk, 99) is None  # outside the hunk
    assert _hunk_position(hunk, None) is None  # a file-level comment
    assert _hunk_position("not a hunk", 10) is None

    # git's `\ No newline at end of file` is a hunk row but NOT a line of the file. Counting it as
    # one let a line past the end of the file resolve to the marker's own offset.
    unterminated = _hunk_around({"content": "alpha\nbeta\ngamma"}, 3)
    assert "\\ No newline at end of file" in unterminated
    assert _hunk_position(unterminated, 3) == 3  # the real last line
    assert _hunk_position(unterminated, 4) is None  # past the end — must not land on the marker


def test_github_emitted_urls_are_fetchable(gh_client, gh_admin_h, gh_org):
    """Every absolute URL Backlot puts in a response has to be one Backlot accepts back — SDK
    clients complete objects lazily by following them (see `_api_base`). Now that a wrong owner
    404s, an emitted URL built from a different notion of the org would be a dead link, and the
    builders do not all read the org from the same place.

    Includes a comment's own `url`, whose route has to be registered ahead of
    `…/pulls/{number}/comments` or the literal `comments` is parsed as a pull number instead."""
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-diff-pr-declared")
    seen = []
    search = c.get("/github/search/issues", headers=gh_admin_h, params={"q": "argv"}).json()
    assert search["items"], "need a search hit to check the URLs it emits"
    seen += [search["items"][0][k] for k in ("url", "repository_url", "comments_url")]
    pull = c.get(f"/github/repos/{gh_org}/diffable/pulls/{num}", headers=gh_admin_h).json()
    # `repository_url` is not among these: it is an issue field, and a pull does not carry it.
    seen += [pull[k] for k in ("url", "issue_url", "comments_url")]
    # the hypermedia a pull gained with its own field set — a template is not a URL, so
    # `review_comment_url` is expanded the way a client would rather than fetched verbatim.
    seen += [pull[k] for k in ("commits_url", "review_comments_url", "statuses_url")]
    # `_links` minus the two forms this check cannot make: `html` names github.com, which Backlot
    # does not serve at all (like `diff_url`/`clone_url`), and `review_comment` is a template.
    # Everything else is a route here, and is expected to answer.
    api_prefix = pull["url"].split("/repos/")[0]
    followable = [
        v["href"]
        for v in pull["_links"].values()
        if v["href"].startswith(api_prefix) and "{" not in v["href"]
    ]
    assert len(followable) == len(pull["_links"]) - 2, sorted(pull["_links"])
    seen += followable
    files = c.get(f"/github/repos/{gh_org}/diffable/pulls/{num}/files", headers=gh_admin_h).json()
    seen.append(files[0]["contents_url"])
    review = c.get(f"/github/repos/{gh_org}/diffable/pulls/{num}/comments", headers=gh_admin_h)
    seen += [review.json()[0]["pull_request_url"], review.json()[0]["url"]]
    convo = c.get(f"/github/repos/{gh_org}/diffable/issues/{num}/comments", headers=gh_admin_h)
    seen.append(convo.json()[0]["url"])
    # A template is not a URL, so each is expanded the way a client would — that is the only form a
    # caller can actually follow, and an unexpanded one would pass this check while helping nobody.
    repo = c.get(f"/github/repos/{gh_org}/diffable", headers=gh_admin_h).json()
    seen += [
        pull["review_comment_url"].replace("{/number}", f"/{review.json()[0]['id']}"),
        repo["pulls_url"].replace("{/number}", f"/{num}"),
        repo["issues_url"].replace("{/number}", f"/{num}"),
        repo["issue_comment_url"].replace("{/number}", f"/{convo.json()[0]['id']}"),
        repo["contents_url"].replace("{+path}", "app.py"),
        repo["trees_url"].replace("{/sha}", "/main"),
        repo["blobs_url"].replace("{/sha}", "/" + files[0]["sha"]),
        repo["branches_url"].replace("{/branch}", "/main"),
        repo["branches_url"].replace("{/branch}", ""),
        repo["tags_url"],
        repo["commits_url"].replace("{/sha}", "/" + pull["head"]["sha"]),
        repo["statuses_url"].replace("{sha}", pull["head"]["sha"]),
        repo["collaborators_url"].replace("{/collaborator}", ""),
        repo["teams_url"],
    ]
    tree = c.get(
        f"/github/repos/{gh_org}/diffable/git/trees/main",
        headers=gh_admin_h,
        params={"recursive": 1},
    ).json()
    seen += [tree["url"], next(e["url"] for e in tree["tree"] if e["type"] == "blob")]

    for url in seen:
        path = url.split("/github", 1)[1]
        r = c.get(f"/github{path}", headers=gh_admin_h)
        assert r.status_code == 200, f"emitted a dead URL: {url} -> {r.status_code}"


# --- git trees: the real truncation cap ----------------------------------


def test_github_tree_truncates_at_the_real_caps(gh_client, gh_admin_h, gh_org, monkeypatch):
    """Real GitHub caps a recursive tree (100k entries / 7 MB) and sets `truncated: true`; a server
    that can never set it leaves a client's truncation-handling path untested.

    (The un-truncated case is already asserted by test_github_tree_recursive.)"""
    from backlot.routers import github as gh

    c, _ = gh_client
    url = f"/github/repos/{gh_org}/codebase/git/trees/main"
    full = c.get(url, headers=gh_admin_h, params={"recursive": "1"}).json()["tree"]

    monkeypatch.setattr(gh, "TREE_MAX_ENTRIES", 2)
    body = c.get(url, headers=gh_admin_h, params={"recursive": "1"}).json()
    assert body["truncated"] is True and len(body["tree"]) == 2
    monkeypatch.undo()

    # the byte cap is the only branch that trims entry by entry
    monkeypatch.setattr(gh, "TREE_MAX_BYTES", len(json.dumps(full)) // 2)
    body = c.get(url, headers=gh_admin_h, params={"recursive": "1"}).json()
    assert body["truncated"] is True
    assert 0 < len(body["tree"]) < len(full)
    assert body["tree"] == full[: len(body["tree"])]  # a prefix, not a resampling


# --- OpenAPI enrichment: github response fidelity ------------------------------------------


def test_github_list_issues_documents_state_param(client):
    op = client.get("/openapi.json").json()["paths"]["/github/repos/{owner}/{repo}/issues"]["get"]
    params = {p["name"]: p for p in op.get("parameters", [])}
    assert "state" in params and {"page", "per_page"} <= set(params)
    assert params["state"]["schema"].get("default") == "open"


def test_github_search_still_filters_by_q(client, admin_h):
    body = client.get("/github/search/issues", params={"q": "is:issue"}, headers=admin_h).json()
    assert "items" in body and "total_count" in body


def test_github_responses_unchanged_by_enrichment(client, admin_h):
    # Fidelity guard: the rich issue field set must survive query-param + response_model enrichment.
    body = client.get("/github/search/issues", params={"q": "is:issue"}, headers=admin_h).json()
    assert body["items"], "SAMPLE should have github issues"
    item = body["items"][0]
    for key in (
        "id",
        "node_id",
        "number",
        "title",
        "body",
        "state",
        "user",
        "labels",
        "assignees",
        "milestone",
        "comments",
        "reactions",
        "author_association",
        "created_at",
        "updated_at",
        "html_url",
        "url",
        "repository_url",
    ):
        assert key in item, f"missing {key} (fidelity regression)"


@pytest.mark.parametrize("path", ["/github/search/issues", "/github/search/code"])
def test_github_search_has_typed_response_schema(client, path):
    op = client.get("/openapi.json").json()["paths"][path]["get"]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema != {}
    assert "$ref" in schema or schema.get("type") in ("object", "array")


def test_github_operation_ids_unique(client):
    spec = client.get("/openapi.json").json()
    ids = [
        op["operationId"]
        for p, item in spec["paths"].items()
        if p.startswith("/github")
        for m, op in item.items()
        if isinstance(op, dict) and "operationId" in op
    ]
    assert len(ids) == len(set(ids))


# --- GitHub ---------------------------------------------------------------------


def test_github_issue_number_asserts_rather_than_re_hash_a_null_number():
    """`_issue_number` must not silently re-hash a NULL number back to a plain
    `synth.github_number`: a PROBED row (one whose served number came from a walk, not a pure hash)
    would then advertise a number nobody stored, unreachable at its own url. An assertion is
    strictly better: every non-file row gets a number at import (`resolve_github_numbers` raises
    rather than leave one NULL), so reaching here with one is a bug upstream, and failing loudly
    beats silently serving the wrong number."""
    from backlot.routers.github import _issue_number

    with pytest.raises(AssertionError, match="no number"):
        _issue_number({"number": None})


def test_github_issue_shape(tmp_path):
    from backlot.routers.github import _issue_obj, _pr_obj

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "github",
                "doc_id": "gh1",
                "repo": "gw",
                "title": "Bug",
                "content": "x",
                "author_email": "a@x.com",
                "state": "closed",
                "closed_at": "2026-02-01T00:00:00Z",
                "closed_by": "b@x.com",
                "assignees": ["a@x.com"],
                "milestone": "v2",
                "reactions": {"+1": 3, "heart": 1},
                "comments": [{"content": "c", "author_email": "b@x.com", "reactions": {"+1": 1}}],
            },
            {
                "source_type": "github",
                "doc_id": "pr1",
                "repo": "gw",
                "title": "PR",
                "content": "y",
                "author_email": "a@x.com",
                "subtype": "pull_request",
                "merged_at": "2026-02-02T00:00:00Z",
                "merged_by": "b@x.com",
                "requested_reviewers": ["c@x.com"],
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    iss = _issue_obj(conn, "org", "gw", _gh_row(conn, "Bug"), "http://m/github")
    # numeric id present and distinct from number (real connectors dedupe on id)
    assert iss["id"] != iss["number"] and isinstance(iss["id"], int)
    assert iss["node_id"]
    # `assignee` (singular) is 2022-11-28's; `2026-03-10` removed it in favour of `assignees[]`,
    # which every version has. The builders take the version so the two shapes come from one place.
    assert iss["assignee"]["login"] == "a" and iss["assignees"][0]["login"] == "a"
    assert iss["closed_at"].startswith("2026-02-01") and iss["closed_by"]["login"] == "b"
    assert iss["milestone"]["title"] == "v2"
    assert iss["state_reason"] == "completed" and iss["author_association"] == "MEMBER"
    # reactions is the full 8-key rollup with total_count
    assert iss["reactions"]["total_count"] == 4 and iss["reactions"]["+1"] == 3
    assert iss["reactions"]["eyes"] == 0

    row = _gh_row(conn, "Bug")
    newer = _issue_obj(conn, "org", "gw", row, "http://m/github", version="2026-03-10")
    assert "assignee" not in newer and newer["assignees"][0]["login"] == "a"
    assert set(iss) - set(newer) == {"assignee"}  # and nothing else moved with it

    pr_row = _gh_row(conn, "PR")
    pr = _pr_obj(conn, "org", "gw", pr_row, "http://m/github")
    assert pr["merged"] is True and pr["merged_by"]["login"] == "b"
    assert pr["requested_reviewers"][0]["login"] == "c"
    # a pull is not an issue with extra keys: the issue-only fields are absent at the builder, not
    # stripped by a route, so every caller of _pr_obj gets the same object
    assert ISSUE_ONLY_FIELDS.isdisjoint(pr), sorted(ISSUE_ONLY_FIELDS & set(pr))
    assert PULL_ONLY_FIELDS <= set(pr), sorted(PULL_ONLY_FIELDS - set(pr))
    pr_new = _pr_obj(conn, "org", "gw", pr_row, "http://m/github", version="2026-03-10")
    assert set(pr) - set(pr_new) == {"assignee", "merge_commit_sha"}


def test_github_comment_reactions(tmp_path):
    from backlot.routers.github import _gh_comment

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "github",
                "doc_id": "gh2",
                "repo": "gw",
                "title": "T",
                "content": "x",
                "comments": [
                    {"content": "hi", "author_email": "a@x.com", "reactions": {"heart": 2}}
                ],
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    # store.github_comments, not the shared doc_comments: only the github reader carries the
    # `id` the builder reports as the comment's `id`
    gh2 = _gh_row(conn, "T")
    c = store.github_comments(conn, gh2["repo"], gh2["number"])[0]
    obj = _gh_comment("org", "gw", 1, c, "http://m/github")
    assert obj["reactions"]["heart"] == 2 and obj["node_id"] and obj["url"]
    assert obj["reactions"]["total_count"] == 2
    assert obj["id"] == c["id"]
