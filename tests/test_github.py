"""GitHub's REST surface: issues, PRs, comments, and the git tree/contents/blobs
codebase serving.

One file per router, so a source's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from urllib.parse import quote

import pytest
import yaml

from backlot import store, synth
from backlot.config import Settings
from backlot.pagination import encode_cursor
from tests._helpers import (
    build_corpus,
    client_for,
    crawl_github_repo,
    db_count,
    tiny_corpus,
    tok,
)


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


def test_github_state_is_reals_enum_refused_on_issues_and_absorbed_on_pulls(tmp_path):
    """GitHub's OpenAPI description declares `state` on the issue and the pull listing as
    `{type: string, enum: [open, closed, all], default: open}`, described "Indicates the state of
    the issues to return." on the one and "Either `open`, `closed`, or `all` to filter by state." on
    the other (read 2026-09-09). Backlot's document declared `{type: string, default: open}` with
    no enum and no description, so a generated client or an agent reading the MCP slice had the
    default and nothing on what else the parameter takes.

    Outside the enum the two routes part, measured on api.github.com on 2026-09-09 against
    `psf/requests`. The issue listing refuses: `state=bogus`, `state=OPEN` and `state=` (empty) are
    each a 422, `Validation Failed`, one `errors` entry carrying the value sent with
    `resource: Issue`, `field: state`, `code: invalid`, `documentation_url`
    `https://docs.github.com/v3/issues/#list-issues`, `content-type: application/json; charset=utf-8`;
    the repository comes first, so the same value on `psf/ghost-zz-9876` is the 404 with the route's
    own anchor. The pull listing absorbs: `pulls?state=bogus` and `?state=OPEN` answer the 87 rows
    `state=open` answers, where `closed` and `all` answer other, larger sets. Backlot filtered on the
    value as sent and answered an empty 200 on both routes, which is neither answer. The corpus is
    built here because no repository in the sample holds an open pull beside a closed one, which is
    what tells the open set from an empty one.
    """
    pulls = [
        {
            "source_type": "github",
            "doc_id": f"gh-pr-{state}",
            "repo": "wide",
            "subtype": "pull_request",
            "state": state,
            "title": f"PR {state}",
            "content": "body",
            "author_email": "ava@acme.com",
            "visibility": "public",
            "head": f"feat/{state}",
            "base": "main",
        }
        for state in ("open", "closed")
    ]
    settings = build_corpus(tmp_path, pulls, name="state.jsonl")
    with client_for(settings, reload=True) as c:
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        org = c.get("/_meta/users").json()["org"]
        spec = c.get("/openapi.json").json()
        for route, description in (
            ("issues", "Indicates the state of the issues to return."),
            ("pulls", "Either `open`, `closed`, or `all` to filter by state."),
        ):
            params = spec["paths"][f"/github/repos/{{owner}}/{{repo}}/{route}"]["get"]["parameters"]
            state = next(p for p in params if p["name"] == "state")
            assert state["description"] == description, route
            assert state["schema"]["enum"] == ["open", "closed", "all"], route
            assert state["schema"]["default"] == "open", route
            assert state["schema"]["type"] == "string", route
        base = f"/github/repos/{org}/wide"
        open_rows = c.get(f"{base}/pulls", headers=h, params={"state": "open"}).json()
        assert [r["title"] for r in open_rows] == ["PR open"]
        assert len(c.get(f"{base}/pulls", headers=h, params={"state": "all"}).json()) == 2
        for value in ("bogus", "OPEN", ""):
            issues = c.get(f"{base}/issues", headers=h, params={"state": value})
            assert issues.status_code == 422, value
            assert issues.headers["content-type"] == "application/json; charset=utf-8"
            assert issues.json() == {
                "message": "Validation Failed",
                "errors": [
                    {"value": value, "resource": "Issue", "field": "state", "code": "invalid"}
                ],
                "documentation_url": "https://docs.github.com/v3/issues/#list-issues",
                "status": "422",
            }, value
            absorbed = c.get(f"{base}/pulls", headers=h, params={"state": value})
            assert absorbed.status_code == 200, value
            assert absorbed.json() == open_rows, value
        # the repository is checked first
        ghost = c.get(
            f"/github/repos/{org}/ghost-zz-9876/issues", headers=h, params={"state": "bogus"}
        )
        assert ghost.status_code == 404
        assert ghost.json()["documentation_url"].endswith("issues/issues#list-repository-issues")


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
        # people-only and OPEN, so the branch listing a caller is served depends on which pulls
        # they can see: its head ref is a branch for hana and not one for bob.
        {
            "source_type": "github",
            "doc_id": "gh-diff-pr-people-only",
            "repo": "diffable",
            "subtype": "pull_request",
            "title": "Move the signing keys behind a rotation job",
            "content": "body",
            "group": "people",
            "visibility": "group",
            "author_email": "hana@acme.com",
            "author_groups": ["people"],
            "state": "open",
            "head": "hana/key-rotation",
            "base": "main",
        },
    ]
)


# A repo that STATES its own refs, and a pull whose head lives in a fork. Both are facts about a
# repository rather than about a document in it, which is what `subtype: "repo"` exists to carry.
_GH_REPO_DOCS = [
    {
        "source_type": "github",
        "subtype": "repo",
        "repo": "stated-repo",
        "default_branch": "trunk",
        # One protected and one not, so `?protected=` has all three of real's answers to give.
        "branches": [{"name": "trunk", "protected": True}, {"name": "release/2026-03"}],
        "tags": ["v1.0", "v1.1"],
    },
    {
        "source_type": "github",
        "doc_id": "gh-stated-file",
        "repo": "stated-repo",
        "subtype": "file",
        "path": "main.py",
        "title": "main.py",
        "content": "print('hi')\n",
        "group": "engineering",
        "visibility": "public",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
    },
    {
        # OPEN, and its head is a name the repo record does not list: a stated listing is the
        # repo's own answer, so this head does not add itself to it.
        "source_type": "github",
        "doc_id": "gh-stated-pr",
        "repo": "stated-repo",
        "subtype": "pull_request",
        "title": "Work on a branch the repo record does not list",
        "content": "body",
        "group": "engineering",
        "visibility": "public",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
        "state": "open",
        "head": "never-listed",
        "base": "trunk",
    },
    {
        # `head_repo` naming THIS repo, which is what a corpus writing its own org as the owner
        # produces. It is not a fork, so its head is a branch here like any other.
        "source_type": "github",
        "doc_id": "gh-diff-pr-own-head-repo",
        "repo": "diffable",
        "subtype": "pull_request",
        "title": "Spell out the head repo",
        "content": "body",
        "group": "engineering",
        "visibility": "public",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
        "state": "open",
        "head": "chore/own-head-repo",
        "head_repo": "acme/diffable",
        "base": "main",
    },
    {
        # A pull from a FORK of `diffable`: its head is a branch of someone else's repo, so it is
        # not a branch of this one however open the pull is.
        "source_type": "github",
        "doc_id": "gh-diff-pr-forked",
        "repo": "diffable",
        "subtype": "pull_request",
        "title": "Fix the argv handling, from a fork",
        "content": "body",
        "group": "engineering",
        "visibility": "public",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
        "state": "open",
        "head": "fix/from-a-fork",
        "head_repo": "outsider/diffable",
        "base": "main",
    },
]


@pytest.fixture(scope="module")
def gh_client(tmp_path_factory):
    from tests.conftest import SAMPLE

    settings = build_corpus(
        tmp_path_factory.mktemp("gh_sample"),
        SAMPLE + _GH_FILE_DOCS + _GH_DIFF_DOCS + _GH_REPO_DOCS,
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
    # those three and nothing else: real's item carries none of the `_links`, `protection` and
    # `protection_url` the single-branch route does (psf/requests, `?per_page=1` against `/3.0`)
    assert set(body[0]) == {"name", "commit", "protected"}
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


def test_github_branch_listing_holds_the_refs_its_pulls_advertise(gh_client, gh_admin_h, gh_org):
    """Every ref the repo's own pulls name is a branch of it.

    Measured on api.github.com (2026-09-03): across pydantic/pydantic, fastapi/fastapi,
    psf/requests, pallets/flask, sqlalchemy/alembic, encode/httpx and jupyter/notebook, all 27 open
    pulls whose head is a branch of the same repo have that head in `/branches`, and every
    `base.ref` any pull names is listed too — including a base that is not the default branch
    (pydantic's `pure-annotation-schema-cache`). A client that resolves a pull's head against the
    listing — to build a ref picker, or to check the ref still exists before reading files at it —
    reads an absent head as a deleted or forked branch, while `head.repo.full_name` in the same
    response says the branch is right here.

    Ordering is real's, ascending by name: `3.0`, `bug/5671`, `init_level_types`, `main`, … on
    psf/requests.
    """
    c, _ = gh_client
    base = f"/github/repos/{gh_org}/diffable"
    pulls = c.get(f"{base}/pulls", headers=gh_admin_h, params={"state": "all"}).json()
    assert pulls, "the listing is built from this repo's pulls; without one this asserts nothing"
    listing = c.get(f"{base}/branches", headers=gh_admin_h)
    assert listing.status_code == 200
    body = listing.json()
    names = [b["name"] for b in body]
    # A fork's head is a branch of the fork, not of this repo, so only a same-repo head counts.
    same_repo = [p for p in pulls if p["head"]["repo"]["full_name"] == f"{gh_org}/diffable"]
    advertised = (
        {"main"} | {p["head"]["ref"] for p in same_repo} | {p["base"]["ref"] for p in pulls}
    )
    assert names == sorted(advertised)
    # an entry stays real's SHORT branch, whatever the listing now holds
    assert all(set(b["commit"]) == {"sha", "url"} and b["protected"] is False for b in body)

    # a name with a slash is a branch like any other, and every route that takes a ref has to
    # carry it: measured on psf/requests, `/branches/bug/5671`, `git/trees/bug/5671`,
    # `/commits/bug/5671` and `?ref=bug/5671` all answer 200. None of those names fit in one path
    # segment, so a route that takes one 404s a branch it holds.
    single = c.get(f"{base}/branches/chore/rename", headers=gh_admin_h)
    assert single.status_code == 200 and single.json()["name"] == "chore/rename"
    for path, params in (
        ("/git/ref/heads/chore/rename", None),
        ("/git/trees/chore/rename", None),
        ("/commits/chore/rename", None),
        ("/statuses/chore/rename", None),
        ("/contents/app.py", {"ref": "chore/rename"}),
    ):
        r = c.get(base + path, headers=gh_admin_h, params=params)
        assert r.status_code == 200, path

    # `?protected=` splits real's three answers, now that there is more than one entry to split:
    # every branch here is unprotected, so a true value selects none and `false` selects all.
    for value, kept in (("true", 0), ("1", 0), ("false", len(names)), ("", len(names))):
        r = c.get(f"{base}/branches", headers=gh_admin_h, params={"protected": value})
        assert r.status_code == 200 and len(r.json()) == kept, f"?protected={value!r}"


def test_github_a_stated_branch_listing_replaces_the_inferred_one(gh_client, gh_admin_h, gh_org):
    """A `subtype: "repo"` record answers for the repo's refs, and the pulls stop being consulted.

    Which branches a repo has is a fact about the repository, and inferring it from pulls is only
    the best available answer when nothing states it — it is right for an open pull's head and
    wrong about 1 time in 5 for a closed one (6 of 28 closed-unmerged heads still exist, measured
    on api.github.com 2026-09-03). A corpus that states the set is not overruled by that guess:
    the open pull below heads a branch the record omits, and the listing omits it too, exactly as
    real does for a pull whose branch was deleted under it.
    """
    c, _ = gh_client
    base = f"/github/repos/{gh_org}/stated-repo"
    listing = c.get(f"{base}/branches", headers=gh_admin_h).json()
    assert [b["name"] for b in listing] == ["release/2026-03", "trunk"]

    pull = c.get(f"{base}/pulls", headers=gh_admin_h, params={"state": "all"}).json()[0]
    assert pull["head"]["ref"] == "never-listed"
    assert c.get(f"{base}/branches/never-listed", headers=gh_admin_h).status_code == 404

    # the repo object reports the stated default branch, not the hardcoded `main`
    assert c.get(base, headers=gh_admin_h).json()["default_branch"] == "trunk"
    assert c.get(f"{base}/branches/trunk", headers=gh_admin_h).status_code == 200


def test_github_stated_protection_decides_the_filter_and_the_protection_object(
    gh_client, gh_admin_h, gh_org
):
    """`?protected=` selects for real once a corpus states which branches are protected, and the
    same stated bit decides `protection.enabled` on the branch object.

    Real is three-valued — a truthy value selects the protected branches, `false`/`0` the
    unprotected ones, an absent or empty parameter all of them (measured on fastapi/fastapi: 22
    branches, one protected, answering 1 / 21 / 22). Until a corpus could say so, every branch was
    unprotected and the last two answers coincided; they no longer have to.

    `protection.enabled` reports CLASSIC protection where `protected` covers any mechanism, and a
    stated bit is read as the classic one — measured 2026-09-03, see
    :func:`backlot.routers.github.get_branch`.
    """
    c, _ = gh_client
    url = f"/github/repos/{gh_org}/stated-repo/branches"

    def names(params):
        return [b["name"] for b in c.get(url, headers=gh_admin_h, params=params).json()]

    def next_url(params):
        return _link_rels(c.get(url, headers=gh_admin_h, params=params).headers["Link"])["next"]

    assert names({"protected": "true"}) == ["trunk"]
    assert names({"protected": "1"}) == ["trunk"]
    assert names({"protected": "false"}) == ["release/2026-03"]
    assert names({"protected": "0"}) == ["release/2026-03"]
    assert names({"protected": ""}) == ["release/2026-03", "trunk"]
    assert names(None) == ["release/2026-03", "trunk"]
    # and the flag rides on the entry itself, in both listings
    assert [b["protected"] for b in c.get(url, headers=gh_admin_h).json()] == [False, True]

    # real's six members on EVERY branch, protected or not, and PyGithub's `Branch` declares the
    # three this used to omit
    single = c.get(f"{url}/trunk", headers=gh_admin_h).json()
    slashed = c.get(f"{url}/release/2026-03", headers=gh_admin_h).json()
    keys = {"name", "commit", "_links", "protected", "protection", "protection_url"}
    assert set(single) == keys and set(slashed) == keys
    assert single["protected"] is True and single["protection"]["enabled"] is True
    assert slashed["protected"] is False and slashed["protection"]["enabled"] is False
    # real's empty block either way: a corpus records no CI
    empty = {"enforcement_level": "off", "contexts": [], "checks": []}
    assert single["protection"] == {"enabled": True, "required_status_checks": empty}
    assert slashed["protection"] == {"enabled": False, "required_status_checks": empty}

    # a slash stays a slash in all three urls, unescaped, as real spells them
    self_url = slashed["_links"]["self"]
    assert self_url.split("testserver", 1)[1] == f"{url}/release/2026-03"
    assert slashed["protection_url"] == f"{self_url}/protection"
    assert slashed["_links"]["html"] == (
        f"https://github.com/{gh_org}/stated-repo/tree/release/2026-03"
    )
    # `self` resolves to this same object, which is the point of serving it
    assert c.get(self_url.split("testserver", 1)[1], headers=gh_admin_h).json() == slashed

    # `protection_url` reaches a route, and that route answers real's 404 for a caller without
    # repo-admin rights (psf/requests `main` and `3.0`). The slashed name pins the route ORDER:
    # real reads the trailing `/protection` as the route even after a slashed name
    # (`/branches/bug/5671/protection` answers this anchor, and `bug/5671` is a branch).
    for name in ("trunk", "release/2026-03"):
        r = c.get(f"{url}/{name}/protection", headers=gh_admin_h)
        assert r.status_code == 404, name
        assert r.json() == {
            "message": "Not Found",
            "documentation_url": (
                "https://docs.github.com/rest/branches/branch-protection#get-branch-protection"
            ),
            "status": "404",
        }, name

    # that handler resolves nothing, so the router-wide dependencies are what answer for the
    # credential, the owner and the version — an unauthenticated 404 would confirm the route
    prot = f"{url}/trunk/protection"
    bad_token = {"Authorization": "Bearer usr-not-a-real-token"}
    old_version = {**gh_admin_h, "X-GitHub-Api-Version": "1999-01-01"}
    wrong_owner = "/github/repos/not-the-owner/stated-repo/branches/trunk/protection"
    assert c.get(prot).status_code == 401
    assert c.get(prot, headers=bad_token).status_code == 401
    assert c.get(prot, headers=old_version).status_code == 400
    assert c.get(wrong_owner, headers=gh_admin_h).status_code == 404

    # the selection happens ahead of the page cut, and a page url spells out only a parameter the
    # caller sent, an empty value included (`list_branches` and `_echo` carry the measurements)
    paged = c.get(url, headers=gh_admin_h, params={"protected": "false", "per_page": 1})
    assert [b["name"] for b in paged.json()] == ["release/2026-03"]
    assert "Link" not in paged.headers, "one unprotected branch here is a single page"
    assert "protected=&" in next_url({"protected": "", "per_page": 1})
    assert "protected" not in next_url({"per_page": 1})


@pytest.mark.parametrize(
    "route, names",
    [("branches", ["release/2026-03", "trunk"]), ("tags", ["v1.0", "v1.1"])],
)
def test_github_ref_listings_page_like_every_other_listing(
    gh_client, gh_admin_h, gh_org, route, names
):
    """`/branches` and `/tags` honour `per_page` and send the `Link` the rest of the router sends.

    Neither listing reports a total, so `next` is the only way a client learns there is a second
    page at all — the reason the header is not optional here.
    """
    c, _ = gh_client
    url = f"/github/repos/{gh_org}/stated-repo/{route}"
    first = c.get(url, headers=gh_admin_h, params={"per_page": 1})
    assert [x["name"] for x in first.json()] == names[:1]
    assert set(_link_rels(first.headers["Link"])) == {"next", "last"}

    nxt = _link_rels(first.headers["Link"])["next"]
    second = c.get(nxt.split("testserver", 1)[1], headers=gh_admin_h)
    assert [x["name"] for x in second.json()] == names[1:]
    assert {"prev", "first"} <= set(_link_rels(second.headers["Link"]))

    # one page carries no Link at all, as real sends none
    assert "Link" not in c.get(url, headers=gh_admin_h).headers


@pytest.mark.parametrize(
    "route, total",
    [
        ("repos/{org}/gateway/issues/{gw}/comments", 1),
        ("repos/{org}/diffable/pulls/{declared}/comments", 2),
        ("repos/{org}/gateway/pulls/{gw}/reviews", 1),
        ("repos/{org}/gateway/pulls/{gw}/commits", 1),
        ("repos/{org}/codebase/collaborators", 8),
        ("orgs/{org}/teams", 12),
        ("repos/{org}/codebase/teams", 1),
        ("repos/{org}/gateway/statuses/{sha}", 0),
    ],
)
def test_github_sub_resource_listings_page_and_answer_an_empty_page(
    gh_client, gh_admin_h, gh_org, route, total
):
    """Every list route here honours `page`/`per_page` and stops at the end of the listing.

    A listing does not have to span a page for a client to see the difference: real answers `[]`
    past the last page where a route that ignores `page` serves its first page forever. Measured on
    api.github.com on 2026-09-04 — `psf/requests/pulls/7616/commits` holds one commit, and
    `?per_page=1&page=1` answers it where `page=99` answers nothing; the six collaborators of a
    repository the author administers answer the same way, and `kubernetes/kubernetes`'s legacy
    `/statuses/{sha}` pages at `per_page=1` with a `Link`. So a walk that increments `page` until
    the answer is empty — the way a listing that reports no total is paged — terminates on real. It
    did not here, and `/pulls/{n}/commits` and `/repos/{o}/{r}/teams` were the worst of it: their
    single item repeated without end.
    """
    c, _ = gh_client
    gw = synth.github_number("gh-pr-1")
    sha = c.get(f"/github/repos/{gh_org}/gateway/pulls/{gw}", headers=gh_admin_h).json()["head"][
        "sha"
    ]
    url = "/github/" + route.format(
        org=gh_org, gw=gw, declared=synth.github_number("gh-diff-pr-declared"), sha=sha
    )
    full = c.get(url, headers=gh_admin_h).json()
    assert len(full) == total, "the listing this walk is measured against"

    walked, page = [], 1
    while page <= total + 2:
        body = c.get(url, headers=gh_admin_h, params={"per_page": 1, "page": page}).json()
        if not body:
            break
        assert len(body) == 1, f"page {page} served more than the per_page asked for"
        walked += body
        page += 1
    else:
        pytest.fail("no page of this listing is empty, so a walk of it never terminates")
    assert walked == full  # every item once, in the order the unpaged listing has them

    first = c.get(url, headers=gh_admin_h, params={"per_page": 1})
    if total > 1:
        rels = _link_rels(first.headers["Link"])
        assert set(rels) == {"next", "last"}
        assert rels["last"].endswith(f"page={total}")
    else:
        assert "Link" not in first.headers  # a single page carries none, as real sends none


@pytest.mark.parametrize("route", ["pulls", "issues"])
def test_github_a_page_url_echoes_only_the_filters_the_caller_sent(
    gh_client, gh_admin_h, gh_org, route
):
    """A next-page url spells out a listing's filter only when the request carried it (`_echo`).

    `state` defaults to `open`, so a url echoing it is narrower than the one the caller called: a
    client walking `state=all` by following `next` would lose the closed rows from page 2 on.

    Both listings that take a `state`, since each applies the default itself: `/issues` serves the
    repo's pulls as rows too, the way real does, so `diffable` pages it without a fixture of its own.
    """
    c, _ = gh_client
    url = f"/github/repos/{gh_org}/diffable/{route}"

    def next_url(params):
        r = c.get(url, headers=gh_admin_h, params={"per_page": 1, **params})
        return _link_rels(r.headers["Link"])["next"]

    assert "state" not in next_url({})
    assert "state=open" in next_url({"state": "open"})
    assert "state=all" in next_url({"state": "all"})


def test_github_a_stated_repo_serves_its_tags(gh_client, gh_admin_h, gh_org):
    """`/tags` answers what the corpus states rather than `[]`.

    Real's item carries `name`, `commit: {sha, url}`, `zipball_url`, `tarball_url` and `node_id`,
    with the archive urls spelling the ref out as `refs/tags/{name}` (measured on psf/requests).
    A tag also becomes a ref: `git/ref/tags/{name}` resolves one that exists.
    """
    c, _ = gh_client
    base = f"/github/repos/{gh_org}/stated-repo"
    tags = c.get(f"{base}/tags", headers=gh_admin_h).json()
    assert [t["name"] for t in tags] == ["v1.0", "v1.1"]
    assert set(tags[0]) == {"name", "commit", "zipball_url", "tarball_url", "node_id"}
    assert set(tags[0]["commit"]) == {"sha", "url"}
    assert tags[0]["zipball_url"].endswith("/zipball/refs/tags/v1.0")
    assert c.get(f"{base}/git/ref/tags/v1.0", headers=gh_admin_h).status_code == 200
    assert c.get(f"{base}/git/ref/tags/v9.9", headers=gh_admin_h).status_code == 404

    # Real takes a tag wherever it takes a branch, measured on psf/requests with `v2.34.2`:
    # `/commits/{tag}`, `git/trees/{tag}`, `/statuses/{tag}` and `/contents/{path}?ref={tag}` all
    # answer 200. A tag that resolved on `/tags` and nowhere else is a ref a client is offered and
    # cannot read at — `GithubFileSystem.refs` offers it and `fs.ls("", sha=<tag>)` then raises.
    for path, params in (
        ("/commits/v1.0", None),
        ("/git/trees/v1.0", None),
        ("/statuses/v1.0", None),
        ("/contents/main.py", {"ref": "v1.0"}),
    ):
        assert c.get(base + path, headers=gh_admin_h, params=params).status_code == 200, path
    # and it resolves to the same commit every other ref of this repo does
    head = c.get(f"{base}/branches/trunk", headers=gh_admin_h).json()["commit"]["sha"]
    assert c.get(f"{base}/commits/v1.0", headers=gh_admin_h).json()["sha"] == head
    # a repo that states none still answers [], which is what real gives a repo with no tags
    assert c.get(f"/github/repos/{gh_org}/diffable/tags", headers=gh_admin_h).json() == []


def test_github_a_forked_pulls_head_is_not_a_branch_of_the_base_repo(gh_client, gh_admin_h, gh_org):
    """A pull from a fork names a branch of the FORK, so the base repo does not list it.

    Real spells the difference in the pull itself: `head.repo.full_name` is the fork and
    `head.label` carries the fork's owner, not the base repo's — measured on pydantic/pydantic,
    where an outside pull reports `chenlichao:fix/…` against `head.repo.full_name`
    `chenlichao/pydantic`. Without a way to state that, every pull looked same-repo and its head
    became a branch here.
    """
    c, _ = gh_client
    base = f"/github/repos/{gh_org}/diffable"
    pulls = c.get(f"{base}/pulls", headers=gh_admin_h, params={"state": "all"}).json()
    forked = next(p for p in pulls if p["head"]["ref"] == "fix/from-a-fork")
    assert forked["head"]["repo"]["full_name"] == "outsider/diffable"
    assert forked["head"]["label"] == "outsider:fix/from-a-fork"
    assert forked["base"]["label"] == f"{gh_org}:main"  # the base is still this repo's

    names = [b["name"] for b in c.get(f"{base}/branches", headers=gh_admin_h).json()]
    assert "fix/from-a-fork" not in names
    assert c.get(f"{base}/branches/fix/from-a-fork", headers=gh_admin_h).status_code == 404

    # `head_repo` naming THIS repo is not a fork — a corpus reaches it by writing its own org as
    # the owner, and reading any stated `head_repo` as a fork made that pull advertise a head the
    # listing then 404d: the contradiction this whole change set removes, from a record the schema
    # accepts.
    own = next(p for p in pulls if p["head"]["ref"] == "chore/own-head-repo")
    assert own["head"]["repo"]["full_name"] == f"{gh_org}/diffable"
    assert own["head"]["label"] == f"{gh_org}:chore/own-head-repo"
    assert "chore/own-head-repo" in names
    assert c.get(f"{base}/branches/chore/own-head-repo", headers=gh_admin_h).status_code == 200


def test_github_branch_listing_omits_a_merged_pulls_head_ref(client, admin_h, org):
    """A merged pull's head branch is gone; its base branch is not.

    The one place a listing built from pulls must NOT hold what a pull advertises. Measured on
    api.github.com (2026-09-03): 0 of 54 merged same-repo head refs across ten repos appear in
    `/branches`, while every base ref does. Listing them would trade the divergence this listing
    closes for a rarer one.
    """
    pulls = client.get(
        f"/github/repos/{org}/gateway/pulls", headers=admin_h, params={"state": "all"}
    ).json()
    merged = [p for p in pulls if p["merged"]]
    assert merged, "gateway's pull is merged; without one this asserts nothing"
    listing = client.get(f"/github/repos/{org}/gateway/branches", headers=admin_h).json()
    names = [b["name"] for b in listing]
    for pull in merged:
        assert pull["head"]["ref"] not in names
        assert pull["base"]["ref"] in names


def test_github_branch_listing_is_scoped_to_the_caller(gh_client, gh_user_tokens, gh_org):
    """Built from the pulls the caller can see, so a branch only a restricted pull names is not one
    they are told about — the rule `/user/repos` already answers repo existence by."""
    c, _ = gh_client
    bob = {"Authorization": f"Bearer {gh_user_tokens['bob@acme.com']}"}  # not in 'people'
    hana = {"Authorization": f"Bearer {gh_user_tokens['hana@acme.com']}"}
    url = f"/github/repos/{gh_org}/diffable/branches"
    assert "hana/key-rotation" in [b["name"] for b in c.get(url, headers=hana).json()]
    assert "hana/key-rotation" not in [b["name"] for b in c.get(url, headers=bob).json()]
    assert c.get(f"{url}/hana/key-rotation", headers=hana).status_code == 200
    assert c.get(f"{url}/hana/key-rotation", headers=bob).status_code == 404


def test_github_a_name_the_branch_listing_omits_is_not_a_ref(gh_client, gh_admin_h, gh_org):
    """ "Does this name exist" is one answer across every route that takes a ref.

    Measured on psf/requests (2026-09-03) for a name no branch listing holds: `/branches/{name}`
    404, `git/ref/heads/{name}` 404, `git/trees/{name}` 404, `/statuses/{name}` 404,
    `/contents/{path}?ref=` 404, and `/commits/{name}` **422** carrying
    `No commit found for SHA: {name}` — not the 404 the other five give. A 40-hex sha naming
    nothing gets those same answers, so a well-formed sha is not a way in.

    `/statuses/{ref}` still answers `[]` for a ref that DOES exist (measured: `/statuses/main` on
    psf/requests) — a corpus records no CI, and that empty list is a shape a client meets in
    production. What changes is only whether the ref was ever named.

    Answering 200 for a name nobody stated made "which branches does this repo have" resolve three
    different ways depending on which route was asked.
    """
    c, _ = gh_client
    base = f"/github/repos/{gh_org}/diffable"
    for ghost in ("totally-made-up", "0" * 40):
        assert c.get(f"{base}/branches/{ghost}", headers=gh_admin_h).status_code == 404, ghost
        assert c.get(f"{base}/git/ref/heads/{ghost}", headers=gh_admin_h).status_code == 404, ghost
        assert c.get(f"{base}/git/trees/{ghost}", headers=gh_admin_h).status_code == 404, ghost
        assert c.get(f"{base}/statuses/{ghost}", headers=gh_admin_h).status_code == 404, ghost
        r = c.get(f"{base}/contents/app.py", headers=gh_admin_h, params={"ref": ghost})
        assert r.status_code == 404, ghost
        # real's own body for a ref it has no object for, which is NOT the generic "Not Found"
        # every other 404 here carries (measured on psf/requests)
        assert r.json()["message"] == f"No commit found for the ref {ghost}"
        assert r.json()["documentation_url"] == "https://docs.github.com/v3/repos/contents/"
        commit = c.get(f"{base}/commits/{ghost}", headers=gh_admin_h)
        assert commit.status_code == 422, ghost
        assert commit.json()["message"] == f"No commit found for SHA: {ghost}"


def test_github_git_ref_resolves_a_pulls_own_ref(gh_client, gh_admin_h, gh_org):
    """`refs/pull/{n}/head` and `…/merge` are refs real GitHub serves for a pull that exists, and
    404s for a number that does not — measured on pydantic/pydantic, where #13686 answers both and
    #999999 answers neither. Holding this route to branches alone would 404 a ref real resolves."""
    c, _ = gh_client
    base = f"/github/repos/{gh_org}/diffable"
    num = c.get(f"{base}/pulls", headers=gh_admin_h, params={"state": "all"}).json()[0]["number"]
    for suffix in ("head", "merge"):
        r = c.get(f"{base}/git/ref/pull/{num}/{suffix}", headers=gh_admin_h)
        assert r.status_code == 200, suffix
        assert r.json()["ref"] == f"refs/pull/{num}/{suffix}"
    assert c.get(f"{base}/git/ref/pull/999999/head", headers=gh_admin_h).status_code == 404
    # this repo states no tags and `/tags` says so, so a tag ref cannot resolve either
    assert c.get(f"{base}/git/ref/tags/v1", headers=gh_admin_h).status_code == 404

    # `…/merge` exists only while the pull is OPEN — real drops the merge ref once the pull is
    # closed: on psf/requests #7616 (merged) and #7589 (closed, unmerged) answer 404 there and 200
    # on `…/head`, where open #7586 answers 200 on both. The pull above is open, which is why the
    # merged one below has to be asked separately.
    merged = f"/github/repos/{gh_org}/gateway"
    n = c.get(merged + "/pulls", headers=gh_admin_h, params={"state": "all"}).json()[0]
    assert n["merged"], "gateway's pull is merged; without one this asserts nothing"
    assert c.get(f"{merged}/git/ref/pull/{n['number']}/head", headers=gh_admin_h).status_code == 200
    assert (
        c.get(f"{merged}/git/ref/pull/{n['number']}/merge", headers=gh_admin_h).status_code == 404
    )


def test_github_a_ref_check_reads_the_repos_pulls_once(gh_client, gh_admin_h, gh_org):
    """The routes that ask whether a ref exists read the pulls behind that answer once.

    `_commit_ish` is the branch names and the commit shas together, and both are derived from the
    same rows — so fetching them separately ran the ACL-joined pull scan twice for every
    `/commits`, `git/trees`, `/statuses` and `?ref=` request. Counted rather than described,
    because nothing else in the suite fails when it doubles.

    The pull scan only; the repo's own row is a primary-key lookup and is not what this counts.
    """
    c, _ = gh_client
    conn = c.app.state.conn
    base = f"/github/repos/{gh_org}/diffable"

    def pull_scans(path, **kw) -> int:
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            assert c.get(base + path, headers=gh_admin_h, **kw).status_code == 200
        finally:
            conn.set_trace_callback(None)
        return sum("kind = 'pull_request'" in q for q in statements)

    assert pull_scans("/branches") == 1
    assert pull_scans("/commits/main") == 1
    assert pull_scans("/git/trees/main") == 1
    assert pull_scans("/statuses/main") == 1
    assert pull_scans("/contents/app.py", params={"ref": "main"}) == 1


def test_github_branch_and_commit_resolve_tree(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    branch = c.get(f"/github/repos/{gh_org}/codebase/branches/main", headers=gh_admin_h).json()
    tree_sha = branch["commit"]["commit"]["tree"]["sha"]
    commit_sha = branch["commit"]["sha"]
    # A ref standing for a commit RESOLVES to it. Real answers `/commits/main` with the branch's
    # own commit sha — the value `/branches/main` reports under `commit.sha` (psf/requests:
    # `dae7ef63…` from both) — and carries that sha in `url` and `node_id`. Echoing the name back
    # as the sha handed a client pinning to `/commits/main` a sha no other route would accept.
    by_name = c.get(f"/github/repos/{gh_org}/codebase/commits/main", headers=gh_admin_h).json()
    assert by_name["sha"] == commit_sha
    assert by_name["url"].endswith(f"/commits/{commit_sha}")
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
                ("stated-repo", "main.py"),
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
    """Real's listings refuse no pagination value. Measured on a public repository's issue listing:
    `per_page=0`, `per_page=abc`, `page=0`, `page=-1` and `page=abc` are each a 200 with the
    defaults applied, and a per_page above the cap is a 200 at the cap. (Of the ten surfaces
    measured `/search/code` is the one that refuses, and refuses in text/plain; see the code search
    tests.)

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
    # over the cap is still the cap (real's 100), not an error
    assert c.get(base, headers=gh_admin_h, params={"per_page": 100_000}).status_code == 200
    # ...and the parameter is still declared an integer, as real's spec declares it
    route = "/github/repos/{owner}/{repo}/issues"
    spec = c.get("/openapi.json").json()["paths"][route]["get"]
    page = next(p for p in spec["parameters"] if p["name"] == "page")
    assert {"type": "integer"} in page["schema"]["anyOf"]


#: GitHub's OpenAPI description, `components/parameters` `per-page` and `page`, read 2026-09-09
#: (github/rest-api-description, `descriptions/api.github.com/api.github.com.json`), verbatim.
_REAL_PAGE_PARAMETER_DESCRIPTIONS = {
    "per_page": (
        "The number of results per page (max 100). For more information, see "
        '"[Using pagination in the REST API]'
        '(https://docs.github.com/rest/using-the-rest-api/using-pagination-in-the-rest-api)."'
    ),
    "page": (
        "The page number of the results to fetch. For more information, see "
        '"[Using pagination in the REST API]'
        '(https://docs.github.com/rest/using-the-rest-api/using-pagination-in-the-rest-api)."'
    ),
}


def _schema_bounds(schema: dict) -> set[str]:
    """The upper-bound keywords a parameter schema declares, at its top level or in any `anyOf`
    branch, which is where FastAPI puts the integer half of an `int | None` parameter."""
    keys = set(schema) | set().union(*(set(branch) for branch in schema.get("anyOf", [])))
    return keys & {"maximum", "exclusiveMaximum"}


def test_github_the_spec_declares_reals_page_parameters(gh_client):
    """GitHub's OpenAPI description declares the shared `per-page` parameter as `{type: integer,
    default: 30}` and `page` as `{type: integer, default: 1}` (github/rest-api-description,
    `components/parameters`, read 2026-09-07 and again 2026-09-09), each under a description on the
    parameter itself. Sixteen of the seventeen routes served here that page reference the two; the
    seventeenth, `GET /repos/{owner}/{repo}/statuses/{sha}`, is the legacy alias the description
    names only in the prose of `/commits/{ref}/statuses`, which references them. The three routes
    whose inline `per_page` default differs — `/notifications` at 50,
    `/orgs/{org}/copilot/billing/seats` at 50 and `/organizations/{org}/settings/billing/budgets`
    at 10 — are indeed unserved here; `/zen` declares no parameters at all, so it is not in that
    set. Backlot's slice declared neither default and neither description: FastAPI writes no default
    for a parameter whose runtime default is None, and the handlers keep None to tell an unsent size
    from a sent one. The spec is what `backlot mcp` hands an agent as a tool, so a default the
    document does not state is one the agent cannot know.

    The description matters for one number: "(max 100)" is the ONLY place real states `per_page`'s
    cap. Its schema is a bare `{type: integer}` with no `maximum`, and that absence is a
    declaration, not an oversight: real serves `per_page=101` at the cap rather than refusing it
    (`test_github_pages_at_reals_thirty_and_caps_at_its_hundred`), so a schema bound would have a
    generated client refuse what the server accepts. A served document that declared `default: 30`
    with no ceiling left 500 looking legal when 500 comes back as 100. So this test holds the
    description to real's text and the schema to no bound, and it holds the 100 in the prose to the
    100 the route applies, since the text is built from that constant rather than spelled out twice.

    Both are written onto the served document after FastAPI builds it, on GitHub's operations
    alone: a Slack `page` keeps the schema its router declared by hand.
    """
    from backlot.routers import github as gh

    c, _ = gh_client
    spec = c.get("/openapi.json").json()
    seen = 0
    for path, item in spec["paths"].items():
        if not path.startswith("/github/"):
            continue
        for op in item.values():
            for p in op.get("parameters", []):
                if p["name"] in ("page", "per_page"):
                    assert p["schema"]["default"] == {"per_page": 30, "page": 1}[p["name"]], path
                    assert {"type": "integer"} in p["schema"]["anyOf"], path  # still an integer
                    assert p["description"] == _REAL_PAGE_PARAMETER_DESCRIPTIONS[p["name"]], path
                    assert not _schema_bounds(p["schema"]), path  # the cap is prose, as on real
                    seen += 1
    assert seen == 2 * 17  # the seventeen routes that page, both parameters each
    # the cap the prose states is the cap the route applies, read from the one constant
    assert f"(max {gh.PER_PAGE_MAX})" in gh.PAGE_PARAMETERS["per_page"][1]
    assert gh.PAGE_PARAMETERS["per_page"][0] == gh.PER_PAGE_DEFAULT
    slack = spec["paths"]["/slack/api/search.messages"]["get"]["parameters"]
    assert "default" not in next(p for p in slack if p["name"] == "page")["schema"]
    # ...and the MCP slice, built from the same document, carries them to an agent
    mcp = c.get("/_meta/openapi/github").json()
    code = mcp["paths"]["/github/search/code"]["get"]["parameters"]
    per_page = next(p for p in code if p["name"] == "per_page")
    assert per_page["schema"]["default"] == 30
    assert per_page["description"] == _REAL_PAGE_PARAMETER_DESCRIPTIONS["per_page"]


def test_github_pages_at_reals_thirty_and_caps_at_its_hundred(tmp_path):
    """Real serves 30 items when `per_page` is not sent and 100 for any sent size at or above it,
    on every listing and search measured. On api.github.com on 2026-09-06, `psf/requests/issues?state=all`,
    `psf/requests/tags`, `psf/requests/pulls?state=all` and `/search/issues?q=repo:psf/requests+timeout`
    each answer 30 items with no `per_page` and 100 for `per_page=100`, `101` and `500` alike;
    `/user/repos` answers 30 unsent and its full 94 for each of the three, which is under the cap.
    GitHub's OpenAPI description declares the shared `per-page` parameter "The number of results per
    page (max 100)." with `default: 30`, the two numbers measured.

    Backlot sized every GitHub page from the server's `default_page_size` (100) and `max_page_size`
    (1000), the two numbers every other router reads: a client walking a 120-issue repository without
    naming a size took two pages here and five on real, and one asking for 500 got five times real's
    page. The `Link` header already paged the way real's does (#131: an unsent size omitted, `last`
    computed from the size applied), so it described the wrong page length faithfully. The corpus is
    built here because no repository in the bundled one holds more than 30 documents.
    """
    from backlot.routers import github as gh

    issues = [
        {
            "source_type": "github",
            "doc_id": f"gh-wide-{i}",
            "repo": "wide",
            "subtype": "issue",
            "title": f"Issue {i}",
            "content": "body",
            "visibility": "public",
            "author_email": "ava@acme.com",
        }
        for i in range(120)
    ]
    files = [
        {
            "source_type": "github",
            "doc_id": f"gh-wide-file-{i}",
            "repo": "wide",
            "subtype": "file",
            "path": f"src/module_{i}.py",
            "title": f"module_{i}.py",
            "content": "print('hi')\n",
            "visibility": "public",
            "author_email": "ava@acme.com",
        }
        for i in range(120)
    ]
    settings = build_corpus(tmp_path, issues + files, name="wide.jsonl")
    with client_for(settings, reload=True) as c:
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        org = c.get("/_meta/users").json()["org"]
        # the issue listing pages by cursor and links `next` alone; the searches link `last`, the
        # page the size APPLIED reaches over 120 rows
        surfaces = (
            (f"/github/repos/{org}/wide/issues", {"state": "all"}, lambda b: b, "next"),
            ("/github/search/issues", {"q": f"repo:{org}/wide"}, lambda b: b["items"], "last"),
            (
                "/github/search/code",
                {"q": f"repo:{org}/wide extension:py"},
                lambda b: b["items"],
                "last",
            ),
        )
        for path, base, items, rel in surfaces:
            for sent, served in (
                (None, 30),  # unsent: real's default
                (30, 30),
                (100, 100),  # at the cap
                (101, 100),  # one over: the cap, not an error
                (500, 100),
            ):
                params = dict(base) if sent is None else {**base, "per_page": sent}
                r = c.get(path, headers=h, params=params)
                assert r.status_code == 200, (path, params)
                assert len(items(r.json())) == served, (path, params)
                expected = 2 if rel == "next" else -(-120 // served)
                assert f"page={expected}" in _link_rels(r.headers["Link"])[rel], (path, params)
        # ...and the two numbers are real's, not the server's settings
        assert (gh.PER_PAGE_DEFAULT, gh.PER_PAGE_MAX) == (30, 100)
        assert Settings.model_fields["default_page_size"].default == 100
        assert Settings.model_fields["max_page_size"].default == 1000


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
    for the errors whose wording was measured. `HEAD` is not a wrong method here: real answers it
    on each of the seven routes measured, and so does Backlot on all of its own, see
    `test_github_a_head_is_the_get_with_the_body_left_off`.
    """
    c, _ = gh_client
    r = c.post(f"/github/repos/{gh_org}/codebase", headers=gh_admin_h)
    assert r.status_code == 405
    assert r.json() == {"detail": "Method Not Allowed"}


def test_github_a_head_is_the_get_with_the_body_left_off(gh_client, gh_admin_h, gh_org):
    """Real answers a `HEAD` on each GitHub route measured as the `GET` with nothing in the body: the GET's
    status, its headers, `content-length` of the body the GET would have carried and `Link` where
    the GET has one. Measured against api.github.com on 2026-09-07 with `curl -I`, each `HEAD`
    beside its `GET` the same minute: `/repos/psf/requests` and `/repos/psf/requests/issues?per_page=2`
    200, the listing at `content-length: 9038` with its `Link`; `/search/code?q=…&per_page=1` 200
    with `Link` and code search's charset-less `application/json`; `/repos/psf/ghost-zz-9876` 404 at
    `content-length: 132`, the length of the GET's Not Found envelope; `/user` with no credential
    401 at 120; `/search/issues?q=` 422 at 219; `/search/code?q=…&per_page=abc` 400
    `text/plain; charset=utf-8` at 75, the length of the deserializer's own line. Seven endpoints,
    one rule, the errors included: they answer `HEAD` exactly as they answer `GET`, body length
    included.

    Every route here is declared `GET` alone, and FastAPI's ``APIRoute`` does not add `HEAD` to a
    GET route the way Starlette's ``Route`` does, so a `HEAD` was Starlette's 405 with `allow: GET`
    on all of them, whatever the GET would have answered: an existence check, `requests.head(url)`
    or `curl -I`, could not tell the repository that exists from the one that does not. It is
    answered by ``backlot.main.answer_head_as_the_get_without_its_body``, which runs the GET and
    keeps its headers, so the version echo, the charset and the id-path rewrite land on a `HEAD` by
    construction; each is asserted below so that the construction is not the only thing saying so.
    The OpenAPI document is untouched: real's description declares no `head` operation (none in
    the 2026-09-09 read) and neither does Backlot's, so `backlot diff` and the MCP slice see what
    they saw. The other vendors' `HEAD` answers are not measured and stay the 405 they were.
    """
    c, _ = gh_client
    codebase = f"/github/repos/{gh_org}/codebase"
    raw = {**gh_admin_h, "Accept": "application/vnd.github.raw"}
    repo_id = c.get(codebase, headers=gh_admin_h).json()["id"]
    rows = (
        (codebase, gh_admin_h, {}),
        (f"/github/repos/{gh_org}/diffable/issues", gh_admin_h, {"state": "all", "per_page": 1}),
        (f"{codebase}/contents/README.md", raw, {}),
        (f"/github/repositories/{repo_id}", gh_admin_h, {}),
        (f"/github/repos/{gh_org}/ghost-zz-9876", gh_admin_h, {}),
        ("/github/user/repos", {}, {}),
        ("/github/search/issues", gh_admin_h, {"q": ""}),
        ("/github/search/code", gh_admin_h, {"q": "extension:md", "per_page": 1}),
        ("/github/search/code", gh_admin_h, {"q": "extension:md", "per_page": "abc"}),
        (codebase, {**gh_admin_h, "X-GitHub-Api-Version": "1999-01-01"}, {}),
    )
    statuses = []
    for path, headers, params in rows:
        get = c.get(path, headers=headers, params=params)
        head = c.head(path, headers=headers, params=params)
        assert head.status_code == get.status_code, (path, params)
        assert head.content == b"", (path, params)
        assert head.headers["content-length"] == get.headers["content-length"], (path, params)
        assert head.headers["content-length"] == str(len(get.content)), (path, params)
        for name in ("content-type", "link", "x-github-api-version-selected"):
            assert head.headers.get(name) == get.headers.get(name), (path, params, name)
        statuses.append(head.status_code)
    assert statuses == [200, 200, 200, 200, 404, 401, 422, 200, 400, 400]
    # ...and the headers the loop compared were there to compare: the listing's `Link` and version
    # echo, the raw representation's own type, code search's text/plain refusal at real's length
    listing = c.head(rows[1][0], headers=gh_admin_h, params=rows[1][2])
    assert "next" in _link_rels(listing.headers["Link"])
    assert listing.headers["X-GitHub-Api-Version-Selected"] == "2022-11-28"
    assert c.head(f"{codebase}/contents/README.md", headers=raw).headers["content-type"] == (
        "application/vnd.github.raw; charset=utf-8"
    )
    refused = c.head("/github/search/code", headers=gh_admin_h, params=rows[8][2])
    assert refused.headers["content-type"] == "text/plain; charset=utf-8"
    assert refused.headers["content-length"] == "75"
    assert "X-GitHub-Api-Version-Selected" not in refused.headers
    # no `head` operation was declared to get there
    spec = c.get("/openapi.json").json()
    assert not [
        p for p, item in spec["paths"].items() if p.startswith("/github") and "head" in item
    ]
    # ...and at the ASGI layer, where the test client cannot stand in for a server: Starlette's
    # TestClient drops a HEAD response's body itself (`testclient.py`, `if request.method != "HEAD"`),
    # so every `head.content == b""` above holds whether or not the middleware sent one, and it also
    # never frames a response by the scope's method the way uvicorn does (see the middleware's
    # docstring for what that framing did while the scope was left saying `GET`). So the messages
    # the app sends are read directly: the headers carry the GET's length, and no body byte follows
    # them.
    from starlette.testclient import TestClient

    sent = []

    async def recording(scope, receive, send):
        async def record(message):
            sent.append(message)
            await send(message)

        await c.app(scope, receive, record)

    # No `with`: a second lifespan on the app would overwrite the state gh_client started.
    assert TestClient(recording).head(codebase, headers=gh_admin_h).status_code == 200
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = c.get(codebase, headers=gh_admin_h).content
    assert dict(start["headers"])[b"content-length"] == str(len(body)).encode()
    assert sum(len(m.get("body", b"")) for m in sent if m["type"] == "http.response.body") == 0
    # a vendor whose `HEAD` is not measured is refused as before
    assert c.head("/slack/api/auth.test", headers=gh_admin_h).status_code == 405


def test_github_a_path_failure_decides_the_answer_whatever_order_it_is_reported_in():
    """A request can fail on a path and a query parameter at once, and it has one answer: the 404
    the path failure earns. Reading the first error reported would make that answer depend on the
    order FastAPI happens to list them in, which is not a rule anyone could rely on.

    Unreachable on this surface today — the page parameters absorb and every other query parameter
    is a `str` — so the function is called directly, with the pair in both orders.
    """
    from backlot.errors import github as gh_errors

    path_err = {"loc": ("path", "number"), "msg": "not an integer"}
    query_err = {"loc": ("query", "per_page"), "msg": "not an integer"}
    route = "/github/repos/acme/codebase/issues/notanint"
    for errors in ([path_err, query_err], [query_err, path_err]):
        status, body = gh_errors.validation_body(route, errors)
        assert status == 404, [e["loc"] for e in errors]
        assert body["message"] == "Not Found"
    # ...and a query failure on its own is still the 422
    assert gh_errors.validation_body(route, [query_err])[0] == 422


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


def test_github_the_issue_listing_pages_by_cursor_not_by_offset(gh_client, gh_admin_h, gh_org):
    """`/repos/{o}/{r}/issues` is the one listing real pages by CURSOR, and its header says so: only
    `next` and `prev`, each url carrying an opaque `after`/`before` beside the page number, and no
    header at all past the rows.

    Measured on api.github.com on 2026-09-04 against a repository with 12 open issues at
    `per_page=5` — page 1 answers `next` alone, page 2 `next, prev`, page 3 `prev` alone, pages 4
    and 99 nothing — where `/pulls` on the same server answers all four rels from `page=2`. The
    cursor is what selects the window: `?page=50` carrying page 1's `after` answers page 2's rows.
    So `rel="last"`, which every other listing here sends, would tell a client the size of a
    listing real never sizes for it.
    """
    c, _ = gh_client
    url = f"/github/repos/{gh_org}/diffable/issues"
    args = {"state": "all", "per_page": 1}
    full = c.get(url, headers=gh_admin_h, params={"state": "all"}).json()
    assert len(full) > 2, "the walk this test measures needs more rows than a page holds"

    first = c.get(url, headers=gh_admin_h, params=args)
    assert set(_link_rels(first.headers["Link"])) == {"next"}
    assert f"after={quote(encode_cursor(1), safe='')}" in first.headers["Link"]

    second = c.get(
        _link_rels(first.headers["Link"])["next"].split("testserver", 1)[1], headers=gh_admin_h
    )
    assert second.json() == full[1:2]
    assert set(_link_rels(second.headers["Link"])) == {"next", "prev"}

    # the cursor decides the window, not the page carried beside it
    ahead = c.get(url, headers=gh_admin_h, params={**args, "page": 50, "after": encode_cursor(1)})
    assert ahead.json() == full[1:2]

    # the last row's page keeps `prev` alone, and the page after it carries no header
    last = c.get(url, headers=gh_admin_h, params={**args, "page": len(full)})
    assert set(_link_rels(last.headers["Link"])) == {"prev"}
    beyond = c.get(url, headers=gh_admin_h, params={**args, "page": len(full) + 1})
    assert beyond.json() == [] and "Link" not in beyond.headers


def test_github_a_page_url_names_the_repository_and_the_org_by_id(gh_client, gh_admin_h, gh_org):
    """Real's page urls name a repository and an organization by ID, where the resource urls in the
    very same response keep the login form.

    Measured on api.github.com on 2026-09-04: `/repos/brekkylab/enterprise-mock/collaborators` at
    `per_page=1` links `/repositories/1287077005/collaborators?per_page=1&page=2`,
    `/orgs/brekkylab/repos` links `/organizations/130592615/repos?…`, and a comment fetched in the
    same breath still carries `url: …/repos/psf/requests/issues/comments/…`. `/user/repos` keeps its
    own path — it names no owner to swap for an id.

    The id form has to RESOLVE, or the header hands a client a url it cannot follow: real answers
    200 on `/repositories/{id}/collaborators` directly, and so does Backlot (see
    ``resolve_github_id_paths`` in ``backlot.main``).
    """
    c, _ = gh_client
    rid, oid = synth.github_user_id("diffable"), synth.github_user_id(gh_org)
    url = f"/github/repos/{gh_org}/diffable/pulls"
    args = {"state": "all", "per_page": 1}

    nxt = _link_rels(c.get(url, headers=gh_admin_h, params=args).headers["Link"])["next"]
    assert f"/github/repositories/{rid}/pulls?" in nxt
    second = c.get(nxt.split("testserver", 1)[1], headers=gh_admin_h)
    assert second.json() == c.get(url, headers=gh_admin_h, params={**args, "page": 2}).json()
    # a resource url in that same body keeps the owner/repo form
    assert f"/repos/{gh_org}/diffable/" in second.json()[0]["url"]

    org_next = _link_rels(
        c.get(f"/github/orgs/{gh_org}/repos", headers=gh_admin_h, params={"per_page": 1}).headers[
            "Link"
        ]
    )["next"]
    assert f"/github/organizations/{oid}/repos?" in org_next
    assert c.get(org_next.split("testserver", 1)[1], headers=gh_admin_h).status_code == 200

    # the listing with no owner in its path is left alone, and an id that names nothing 404s
    user_next = _link_rels(
        c.get("/github/user/repos", headers=gh_admin_h, params={"per_page": 1}).headers["Link"]
    )["next"]
    assert "/github/user/repos?" in user_next
    assert c.get(f"/github/repositories/{rid + 1}/pulls", headers=gh_admin_h).status_code == 404

    # The id is the CORPUS's, not one minted from the spelling the caller used. Both segments
    # resolve in any case, so a page url built from the request path would name an id nothing
    # holds — and 404 the client that followed it.
    for path in (
        f"/github/repos/{gh_org}/DIFFABLE/pulls",
        f"/github/repos/{gh_org.upper()}/diffable/pulls",
    ):
        loud = _link_rels(c.get(path, headers=gh_admin_h, params=args).headers["Link"])["next"]
        assert f"/github/repositories/{rid}/pulls?" in loud, path
        assert c.get(loud.split("testserver", 1)[1], headers=gh_admin_h).status_code == 200, path
    loud_org = _link_rels(
        c.get(
            f"/github/orgs/{gh_org.upper()}/repos", headers=gh_admin_h, params={"per_page": 1}
        ).headers["Link"]
    )["next"]
    assert f"/github/organizations/{oid}/repos?" in loud_org
    assert c.get(loud_org.split("testserver", 1)[1], headers=gh_admin_h).status_code == 200


def test_github_an_id_path_answers_in_the_order_the_named_path_does(gh_client, gh_admin_h, gh_org):
    """An id that names nothing is refused the way a name that names nothing is, and in the same
    order: credentials first, existence second, so that no answer here tells an unauthenticated
    caller which ids the corpus holds (`canonical_id_path` says why that matters)."""
    c, _ = gh_client
    held, absent = synth.github_user_id("diffable"), synth.github_user_id("diffable") + 1
    for path in (
        f"/github/repositories/{held}/pulls",
        f"/github/repositories/{absent}/pulls",
        f"/github/repos/{gh_org}/diffable/pulls",
        f"/github/repos/{gh_org}/nosuchrepo/pulls",
    ):
        assert c.get(path).status_code == 401, path
    assert c.get(f"/github/repositories/{held}/pulls", headers=gh_admin_h).status_code == 200
    assert c.get(f"/github/repositories/{absent}/pulls", headers=gh_admin_h).status_code == 404


def test_github_an_id_two_repos_share_names_neither(tmp_path):
    """A corpus that holds both halves of a `github_user_id` collision — the ids are not unique,
    see `canonical_id_path` — answers for each name and 404s the id they share, rather
    than walking a client onto whichever of the two sorts first."""
    assert synth.github_user_id("repo485") == synth.github_user_id("repo4107")
    shared = synth.github_user_id("repo485")
    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "github",
                "doc_id": f"gh-{name}",
                "repo": name,
                "subtype": "issue",
                "title": "hi",
                "content": "body",
                "visibility": "public",
                "author_email": "ava@acme.com",
            }
            for name in ("repo485", "repo4107")
        ],
        name="collide.jsonl",
    )
    with client_for(settings, reload=True) as c:
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        org = c.get("/_meta/users").json()["org"]
        for name in ("repo485", "repo4107"):
            assert c.get(f"/github/repos/{org}/{name}/issues", headers=h).status_code == 200, name
        assert c.get(f"/github/repositories/{shared}/issues", headers=h).status_code == 404


def test_github_only_the_offset_surfaces_write_the_size_the_caller_spelt(
    gh_client, gh_admin_h, monkeypatch
):
    """Which surfaces carry the caller's own spelling of `per_page` and which carry the size they
    applied. `/search/issues` and the listings carry the caller's; `/search/code` carries the size
    it applied, as the cursor listing does.

    Measured on api.github.com on 2026-09-04: `/search/code?q=…&per_page=500` on 1,808 hits links
    `per_page=100`, its own cap, and `?per_page=0` links `per_page=30`, where
    `/search/issues?…&per_page=500` links `per_page=500` and `/tags?per_page=abc` links
    `per_page=abc`. The cap is lowered here because no listing in the corpus spans a page of 100,
    so without it neither surface links a `next` to read the size off at all.
    """
    from backlot.routers import github as gh

    c, _ = gh_client
    monkeypatch.setattr(gh, "PER_PAGE_MAX", 2)
    code = c.get(
        "/github/search/code", headers=gh_admin_h, params={"q": "extension:md", "per_page": 500}
    )
    issues = c.get(
        "/github/search/issues",
        headers=gh_admin_h,
        params={"q": "repo:diffable is:pr", "per_page": 500},
    )
    assert "per_page=2" in _link_rels(code.headers["Link"])["next"]
    assert "per_page=500" in _link_rels(issues.headers["Link"])["next"]


def test_github_the_issue_listing_writes_a_page_number_only_where_one_was_claimed(
    gh_client, gh_admin_h, gh_org
):
    """Which requests get a page number in their cursor urls, which is the branch the route decides
    and the helper only carries out.

    Measured on api.github.com on 2026-09-04 at `per_page=2`: `?after=C` and `?page=1&after=C` both
    answer `next` and `prev` with no `page` in either url, where `?page=3&after=C` answers next=4
    and prev=2 and a request with no cursor at all answers next=2.
    """
    c, _ = gh_client
    url = f"/github/repos/{gh_org}/diffable/issues"
    args = {"state": "all", "per_page": 1, "after": encode_cursor(1)}

    def pages(params):
        rels = _link_rels(c.get(url, headers=gh_admin_h, params=params).headers["Link"])
        return {
            rel: re.search(r"[?&]page=(\d+)", link).group(1) if "&page=" in link else None
            for rel, link in rels.items()
        }

    assert pages(args) == {"next": None, "prev": None}
    assert pages({**args, "page": 1}) == {"next": None, "prev": None}
    assert pages({**args, "page": 3}) == {"next": "4", "prev": "2"}
    assert pages({"state": "all", "per_page": 1}) == {"next": "2"}


def test_github_a_page_url_omits_a_page_size_the_caller_did_not_send(
    gh_client, gh_admin_h, gh_org, monkeypatch
):
    """A `per_page` the handler defaulted is not one the caller sent, so the page urls leave it out
    — the rule `_paged` already applies to a listing's filters, reaching the one parameter it
    builds itself.

    Real omits it: `psf/requests/tags` with no query links `?page=2` and `?page=6`, six pages of
    161 tags at its own default of 30, with no `per_page` anywhere, where `?per_page=1` links
    `per_page=1&page=2`. Search omits it the same way (measured on api.github.com on 2026-09-04).

    The default page size is lowered here because no listing in the corpus is longer than the 30
    rows a GitHub page defaults to, so nothing in it spans a page without a `per_page` to make it.
    """
    from backlot.routers import github as gh

    c, _ = gh_client
    url = f"/github/repos/{gh_org}/diffable/pulls"
    monkeypatch.setattr(gh, "PER_PAGE_DEFAULT", 1)
    unsent = c.get(url, headers=gh_admin_h, params={"state": "all"})
    nxt = _link_rels(unsent.headers["Link"])["next"]
    assert nxt.endswith("?state=all&page=2") and "per_page" not in nxt
    sent = c.get(url, headers=gh_admin_h, params={"state": "all", "per_page": 1})
    assert "per_page=1&page=2" in _link_rels(sent.headers["Link"])["next"]


@pytest.mark.parametrize(
    "path, q, page_one",
    [
        ("/github/search/code", "extension:md", {"next", "first", "last"}),
        ("/github/search/issues", "repo:diffable is:pr", {"next", "last"}),
    ],
)
def test_github_search_pages_with_a_link_header(gh_client, gh_admin_h, path, q, page_one):
    """Real pages a search response with an RFC5988 `Link` and sends none at all when the results
    fit on one page — but the two search routes do not page alike, and page 1 is where it shows:
    `/search/issues` answers `next, last` as every listing here does, `/search/code` `next, first,
    last` (:func:`backlot.pagination.github_code_search_link_header` for the rest of its rules).

    A search envelope reports `total_count`, so the header is not the only way to learn there is
    more — it is how a client that FOLLOWS links pages without composing a URL of its own, which is
    what every listing on this router already gives it.
    """
    c, _ = gh_client
    first = c.get(path, headers=gh_admin_h, params={"q": q, "per_page": 2, "page": 1})
    total = first.json()["total_count"]
    assert total > 2, "the fixture has to span more than one page for this to mean anything"
    assert set(_link_rels(first.headers["Link"])) == page_one

    # following `next` lands on the same query's second page -- the round-trip the encoding is for
    nxt = _link_rels(first.headers["Link"])["next"]
    second = c.get(nxt.split("testserver", 1)[1], headers=gh_admin_h)
    assert second.json()["total_count"] == total

    def ids(r):
        return {i["url"] for i in r.json()["items"]}

    assert ids(second) and not ids(second) & ids(first)
    assert {"prev", "first"} <= set(_link_rels(second.headers["Link"]))

    # one page of results carries no Link at all, as real sends none
    assert "Link" not in c.get(path, headers=gh_admin_h, params={"q": q, "per_page": 100}).headers


_INVALID_DIGIT = "Failed to deserialize query string: {}: invalid digit found in string"
_EMPTY = "Failed to deserialize query string: {}: cannot parse integer from empty string"
_TOO_LARGE = "Failed to deserialize query string: {}: number too large to fit in target type"
_DUPLICATE = "Failed to deserialize query string: duplicate field `{}`"
# Each row is one answer api.github.com gave on 2026-09-06 or 2026-09-07 to
# `/search/code?q=repo:psf/requests+def` with the query string below appended. The number is parsed
# as Rust parses a u32: an optional single `+` then ASCII digits, so an unencoded `+5` (which
# arrives as ` 5`), a `+` alone and `++5` are invalid digits where `%2B5` is a 200 (tested below).
_CODE_SEARCH_PAGE_REFUSALS = [
    ("per_page=abc", _INVALID_DIGIT.format("per_page")),
    ("per_page=-1", _INVALID_DIGIT.format("per_page")),
    ("per_page=1.5", _INVALID_DIGIT.format("per_page")),
    ("per_page=+5", _INVALID_DIGIT.format("per_page")),
    ("per_page=%2B", _INVALID_DIGIT.format("per_page")),
    ("per_page=%2B%2B5", _INVALID_DIGIT.format("per_page")),
    ("per_page=5abc", _INVALID_DIGIT.format("per_page")),
    ("per_page=1e2", _INVALID_DIGIT.format("per_page")),
    # digits that are not ASCII digits: Python's int() reads each of these as 1, real does not
    ("per_page=%D9%A1", _INVALID_DIGIT.format("per_page")),
    ("page=%D9%A1", _INVALID_DIGIT.format("page")),
    ("per_page=%EF%BC%91", _INVALID_DIGIT.format("per_page")),
    ("per_page=", _EMPTY.format("per_page")),
    ("per_page=4294967296", _TOO_LARGE.format("per_page")),
    ("per_page=99999999999999999999", _TOO_LARGE.format("per_page")),
    # 5000 digits: real's answer is still the too-large line, where Python's int() refuses to
    # convert a string that long at all
    ("per_page=" + "1" * 5000, _TOO_LARGE.format("per_page")),
    ("page=abc", _INVALID_DIGIT.format("page")),
    ("page=-1", _INVALID_DIGIT.format("page")),
    ("page=1.5", _INVALID_DIGIT.format("page")),
    ("page=", _EMPTY.format("page")),
    ("page=99999999999999999999", _TOO_LARGE.format("page")),
    # the first failure in query-string order is the one named
    ("page=abc&per_page=abc", _INVALID_DIGIT.format("page")),
    ("per_page=abc&page=abc", _INVALID_DIGIT.format("per_page")),
    # a repeated parameter is refused at its second occurrence, so order decides which line
    ("per_page=5&per_page=abc", _DUPLICATE.format("per_page")),
    ("per_page=abc&per_page=5", _INVALID_DIGIT.format("per_page")),
    # `q` repeated is the same deserializer's duplicate (the test's own `q` comes first), in
    # query-string order with the page failures; `sort` repeated is a 200 (tested below)
    ("q=x", _DUPLICATE.format("q")),
    ("q=x&per_page=abc", _DUPLICATE.format("q")),
    ("per_page=abc&q=x", _INVALID_DIGIT.format("per_page")),
    # a good page value beside an unknown or a bad other parameter is not what is refused
    ("per_page=abc&sort=abc", _INVALID_DIGIT.format("per_page")),
]


@pytest.mark.parametrize(
    "qs,body", _CODE_SEARCH_PAGE_REFUSALS, ids=[r[0] for r in _CODE_SEARCH_PAGE_REFUSALS]
)
def test_github_code_search_refuses_an_unparseable_page_value_in_text_plain(
    gh_client, gh_admin_h, qs, body
):
    """Of the ten GitHub surfaces measured `/search/code` is the one that refuses a `page` /
    `per_page` it cannot parse, or a `q`, `page` or `per_page` given twice, and it refuses in a
    shape no other GitHub error has: 400, `text/plain; charset=utf-8`, no envelope, a Rust
    deserializer's own line. The other nine absorb the same values (see
    `test_github_tolerates_the_pagination_values_real_tolerates`), which this route did too, so a
    client's error path for the 400 was never reached against Backlot and `.json()` on it would
    have parsed where real's answer raises."""
    c, _ = gh_client
    r = c.get(f"/github/search/code?q=extension:md&{qs}", headers=gh_admin_h)
    assert r.status_code == 400
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    assert r.text == body
    with pytest.raises(ValueError):
        r.json()


def test_github_code_search_page_refusal_is_the_parse_and_comes_before_q(gh_client, gh_admin_h):
    """What is refused is the parse, not the range or the query: `0`, `01` and 4294967295 (the
    largest value real's unsigned 32-bit parameter holds) are each a 200, `01` served as 1, and so
    are an encoded `+` before the digits (`%2B5`, `page=%2B2`), 5000 leading zeros and `sort` given
    twice; a blank `q` beside `per_page=abc` is this 400 and not the blank-query 422, as is a blank
    `q` given twice; a bad `per_page` on `/search/issues` is still absorbed, and so is a repeated
    `q` there; and the OpenAPI slice still declares the parameter an integer, since the refusal is
    the route's and not the validator's (all measured 2026-09-06 and 2026-09-07)."""
    c, _ = gh_client
    full = c.get("/github/search/code?q=extension:md", headers=gh_admin_h).json()
    assert full["total_count"] >= 2
    for qs in (
        "per_page=0",
        "page=0",
        "per_page=4294967295",
        "page=01",
        "page=%2B1",
        "per_page=%2B100",
        "page=" + "0" * 5000,
        "per_page=" + "0" * 5000 + "100",
        "foo=abc&per_page=100",
        "sort=indexed&sort=indexed",
    ):
        r = c.get(f"/github/search/code?q=extension:md&{qs}", headers=gh_admin_h)
        assert r.status_code == 200, qs
        assert r.json() == full, qs
    for qs in ("per_page=01", "per_page=%2B1", "per_page=" + "0" * 5000 + "1"):
        one = c.get(f"/github/search/code?q=extension:md&{qs}", headers=gh_admin_h).json()
        assert one["total_count"] == full["total_count"] and len(one["items"]) == 1, qs
    two = c.get("/github/search/code?q=extension:md&per_page=1&page=%2B2", headers=gh_admin_h)
    assert two.json()["items"] == [full["items"][1]]
    r = c.get("/github/search/code?q=&per_page=abc", headers=gh_admin_h)
    assert (r.status_code, r.headers["content-type"]) == (400, "text/plain; charset=utf-8")
    assert r.text == _INVALID_DIGIT.format("per_page")
    r = c.get("/github/search/code?q=&q=", headers=gh_admin_h)
    assert (r.status_code, r.text) == (400, _DUPLICATE.format("q"))
    assert c.get("/github/search/code?q=", headers=gh_admin_h).status_code == 422
    assert (
        c.get("/github/search/issues?q=is:open&q=is:closed", headers=gh_admin_h).status_code == 200
    )
    assert (
        c.get("/github/search/issues?q=is:open&per_page=abc", headers=gh_admin_h).status_code == 200
    )
    spec = c.get("/openapi.json").json()["paths"]["/github/search/code"]["get"]
    for name in ("page", "per_page"):
        param = next(p for p in spec["parameters"] if p["name"] == name)
        assert {"type": "integer"} in param["schema"]["anyOf"], name


_CODE_CAP = {
    "message": "Cannot access beyond the first 1000 results",
    "documentation_url": "https://docs.github.com/rest/search/search#search-code",
    "status": "422",
}
_ISSUES_CAP = {
    "message": "Only the first 1000 search results are available",
    "documentation_url": "https://docs.github.com/v3/search/",
    "status": "422",
}


@pytest.mark.parametrize(
    "qs,served",
    [
        # page * per_page against 1000, whatever the total (the fixture has four hits)
        ("per_page=100&page=10", True),
        ("per_page=100&page=11", False),
        ("per_page=30&page=33", True),
        ("per_page=30&page=34", False),
        ("per_page=7&page=142", True),
        ("per_page=7&page=143", False),
        ("per_page=1&page=1000", True),
        ("per_page=1&page=1001", False),
    ],
)
def test_github_code_search_refuses_a_page_reaching_past_the_first_1000_results(
    gh_client, gh_admin_h, qs, served
):
    """Code search serves the first 1000 results and refuses a page that would reach past them,
    `page * per_page > 1000`, with a 422 of its own wording and its own route's anchor and no
    `errors` array; the total does not enter, so a four-hit search refuses page 34 at 30 a page
    after serving pages 1 to 33, the empty ones included (measured 2026-09-06 on api.github.com,
    44 hits and 294 million). Backlot served every page of every search."""
    c, _ = gh_client
    r = c.get(f"/github/search/code?q=extension:md&{qs}", headers=gh_admin_h)
    if served:
        assert r.status_code == 200, qs
        assert r.json()["total_count"] == 4 and r.json()["items"] == [], qs
    else:
        assert r.status_code == 422, qs
        assert r.json() == _CODE_CAP, qs


def test_github_code_search_answers_its_refusals_in_reals_order(gh_client, gh_admin_h, gh_org):
    """Measured 2026-09-06: no credential is the 401 before anything else, a page that will not
    parse is the text/plain 400 before a blank `q`, a blank `q` is its 422 before the depth, and the
    depth is refused before the `repo:` qualifier is read (`repo:psf/ghost-zz-9876` at page 11 of
    100 is the depth 422, where the same qualifier on page 1 is the empty `incomplete_results`
    200)."""
    c, _ = gh_client
    assert c.get("/github/search/code?q=extension:md&per_page=abc&page=11").status_code == 401
    r = c.get("/github/search/code?q=extension:md&per_page=abc&page=11", headers=gh_admin_h)
    assert (r.status_code, r.headers["content-type"]) == (400, "text/plain; charset=utf-8")
    r = c.get("/github/search/code?q=&per_page=100&page=11", headers=gh_admin_h)
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "q"
    ghost = f"/github/search/code?q=repo:{gh_org}/ghost-zz-9876+def"
    r = c.get(f"{ghost}&per_page=100&page=11", headers=gh_admin_h)
    assert (r.status_code, r.json()) == (422, _CODE_CAP)
    r = c.get(ghost, headers=gh_admin_h)
    assert r.status_code == 200 and r.json()["incomplete_results"] is True
    # the size APPLIED is what the depth is measured in: an unsent or zero size is served at 30 and
    # one over the cap at 100, real's numbers, so the boundary pages are real's (measured 2026-09-06)
    for qs, served in (
        ("page=33", True),
        ("page=34", False),
        ("per_page=0&page=33", True),
        ("per_page=101&page=10", True),
        ("per_page=101&page=11", False),
    ):
        r = c.get(f"/github/search/code?q=extension:md&{qs}", headers=gh_admin_h)
        assert r.status_code == (200 if served else 422), qs


@pytest.mark.parametrize(
    "qs,served",
    [
        # the page's START against 1000: a page straddling it is served in full
        ("per_page=100&page=10", True),
        ("per_page=100&page=11", False),
        ("per_page=30&page=34", True),
        ("per_page=30&page=35", False),
        ("per_page=7&page=143", True),
        ("per_page=7&page=144", False),
        ("per_page=1&page=1000", True),
        ("per_page=1&page=1001", False),
    ],
)
def test_github_issue_search_refuses_a_page_starting_past_the_first_1000_results(
    gh_client, gh_admin_h, qs, served
):
    """Issue search draws the depth line elsewhere than code search: the page's first result
    against 1000, so at 30 a page, page 34 (results 991 to 1020) is served in full and page 35
    refused, at 7 a page, page 143 (995 to 1001) is served and 144 refused; the 422 is its own
    wording with the bare `/v3/search/` anchor, and an 846-result search refuses page 11 at 100 a
    page just the same after serving page 10 empty (measured 2026-09-06 on api.github.com,
    `/search/repositories` answering the same). Backlot served every page."""
    c, _ = gh_client
    r = c.get(f"/github/search/issues?q=is:open&{qs}", headers=gh_admin_h)
    if served:
        assert r.status_code == 200 and r.json()["items"] == [], qs
    else:
        assert (r.status_code, r.json()) == (422, _ISSUES_CAP), qs


def test_github_issue_search_refuses_the_query_before_the_depth(gh_client, gh_admin_h, gh_org):
    """On `/search/issues` the blank-`q` and unsearchable-`repo:` 422s come before the depth's,
    the reverse of code search's order for the qualifier (measured 2026-09-06)."""
    c, _ = gh_client
    r = c.get("/github/search/issues?q=&per_page=100&page=11", headers=gh_admin_h)
    assert r.status_code == 422 and r.json()["errors"][0]["code"] == "missing"
    r = c.get(
        f"/github/search/issues?q=repo:{gh_org}/ghost-zz-9876&per_page=100&page=11",
        headers=gh_admin_h,
    )
    assert r.status_code == 422 and r.json()["errors"][0]["code"] == "invalid"
    # an unsent size is served at 30, real's default, so page 34 (results 991 to 1020) is the last
    # served and 35 the first refused, as on real (measured 2026-09-06)
    for page, served in ((34, True), (35, False)):
        r = c.get(f"/github/search/issues?q=is:open&page={page}", headers=gh_admin_h)
        assert r.status_code == (200 if served else 422), page


def test_github_code_search_neither_refuses_nor_echoes_the_api_version(gh_client, gh_admin_h):
    """Code search is served by a backend that does not read `X-GitHub-Api-Version`: a pinned
    `1999-01-01` or `garbage` is a 200 where every other route answers the version 400, a pinned
    `2026-03-10` is a 200, and no response from it, 200, 400 or 422, carries
    `X-GitHub-Api-Version-Selected` (measured 2026-09-06; `/search/issues` beside it 400s the bad
    version, see `test_github_unsupported_api_version_is_refused_ahead_of_everything`). Backlot
    refused the bad version and echoed the good one here as everywhere else."""
    c, _ = gh_client
    for pinned in ("1999-01-01", "garbage", "2026-03-10", None):
        h = {**gh_admin_h, **({"X-GitHub-Api-Version": pinned} if pinned else {})}
        r = c.get("/github/search/code?q=extension:md", headers=h)
        assert r.status_code == 200, pinned
        assert "X-GitHub-Api-Version-Selected" not in r.headers, pinned
    for qs, status in (("q=extension:md&per_page=abc", 400), ("q=", 422), ("q=x&page=34", 422)):
        r = c.get(f"/github/search/code?{qs}", headers=gh_admin_h)
        assert r.status_code == status and "X-GitHub-Api-Version-Selected" not in r.headers, qs
    # ...and the route beside it still does both
    r = c.get("/github/search/issues?q=is:open", headers=gh_admin_h)
    assert r.headers["X-GitHub-Api-Version-Selected"] == "2022-11-28"


def _rel_pages(link: str | None) -> list[tuple[str, int]]:
    """`Link` as `(rel, page)` pairs in header order, for asserting against real's."""
    from urllib.parse import parse_qs, urlparse

    out = []
    for part in (link or "").split(", "):
        url, rel = part.split("; rel=")
        out.append((rel.strip('"'), int(parse_qs(urlparse(url.strip("<>")).query)["page"][0])))
    return out


def test_github_search_link_headers_stop_at_the_first_1000_results():
    """Measured 2026-09-06 on api.github.com. An issue search of 2,813,432 results links last=10 at
    100 a page, 34 at 30 and 143 at 7, `ceil(1000 / per_page)` each time, and on that page carries
    `prev, first` alone as on any last page. A code search of 294,649,856 results links last=10,
    34, 143 and, at 1 a page, 1000 — and at 30 a page, page 33 links next=34 and last=34, a page the
    route refuses (`test_github_code_search_refuses_a_page_reaching_past_the_first_1000_results`):
    real's `last` names a page real does not serve, and so does this one."""
    from backlot.pagination import github_code_search_link_header, github_link_header

    def issues(page, per_page):
        return _rel_pages(
            github_link_header(
                "https://api.github.com/search/issues",
                {"q": "is:issue label:bug is:open"},
                page,
                per_page,
                2_813_432,
                per_page_param=str(per_page),
                max_page=-(-1000 // per_page),
            )
        )

    assert issues(1, 100) == [("next", 2), ("last", 10)]
    assert issues(9, 100) == [("prev", 8), ("next", 10), ("last", 10), ("first", 1)]
    assert issues(10, 100) == [("prev", 9), ("first", 1)]
    assert issues(1, 30) == [("next", 2), ("last", 34)]
    assert issues(33, 30) == [("prev", 32), ("next", 34), ("last", 34), ("first", 1)]
    assert issues(34, 30) == [("prev", 33), ("first", 1)]
    assert issues(1, 7) == [("next", 2), ("last", 143)]
    assert issues(143, 7) == [("prev", 142), ("first", 1)]
    # under the depth nothing changes: 846 results at 100 a page still end at 9
    small = github_link_header("u", {"q": "x"}, 1, 100, 846, per_page_param="100", max_page=10)
    assert _rel_pages(small) == [("next", 2), ("last", 9)]

    def code(page, per_page):
        return _rel_pages(
            github_code_search_link_header(
                "https://api.github.com/search/code", {"q": "def"}, page, per_page, 294_649_856
            )
        )

    assert code(1, 100) == [("next", 2), ("first", 1), ("last", 10)]
    assert code(1, 30) == [("next", 2), ("first", 1), ("last", 34)]
    assert code(1, 7) == [("next", 2), ("first", 1), ("last", 143)]
    assert code(1, 1) == [("next", 2), ("first", 1), ("last", 1000)]
    assert code(33, 30) == [("next", 34), ("prev", 32), ("first", 1), ("last", 34)]
    # 44 hits at 1 a page: last=44, under the depth
    small = github_code_search_link_header("u", {"q": "def"}, 1, 1, 44)
    assert _rel_pages(small) == [("next", 2), ("first", 1), ("last", 44)]


def test_github_search_routes_wire_the_depth_into_their_link_and_their_422(
    gh_client, gh_admin_h, monkeypatch
):
    """The fixture cannot hold a thousand results, so the depth is lowered to 2 to see that each
    route hands it to its Link builder and its 422: at 1 a page both searches link last=2 where the
    total would say 4, and page 3 is the route's own 422 (`test_github_search_link_headers_stop_at_
    the_first_1000_results` pins the builders' arithmetic at the real depth)."""
    from backlot import pagination

    c, _ = gh_client
    # the one name both the 422 and the Link read: the router holds no copy of the number
    monkeypatch.setattr(pagination, "GITHUB_SEARCH_RESULT_CAP", 2)
    for path, q, cap in (
        ("/github/search/code", "extension:md", _CODE_CAP),
        ("/github/search/issues", "is:open", _ISSUES_CAP),
    ):
        r = c.get(path, headers=gh_admin_h, params={"q": q, "per_page": 1})
        assert r.json()["total_count"] >= 3, path
        assert dict(_rel_pages(r.headers["Link"]))["last"] == 2, path
        r = c.get(path, headers=gh_admin_h, params={"q": q, "per_page": 1, "page": 3})
        assert (r.status_code, r.json()) == (422, cap), path


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

    Two kinds of ref answer here, and they are not the same set the branch listing holds. A
    snapshot ref (`pr-1`) is a name the corpus gave one revision of a file — a ref without being a
    branch — and selects it. A branch selects HEAD, since Backlot keeps no per-ref tree (see
    `get_tree`), so `?ref=main` is the file as it stands.

    A ref that is neither is a 404, as on real (`?ref=totally-made-up` on psf/requests). Answering
    HEAD for it meant a client that misspelled a branch read the current file and could not tell.
    """
    c, _ = gh_client
    url = f"/github/repos/{gh_org}/history-repo/contents/svc/rate.py"
    raw = {**gh_admin_h, "Accept": "application/vnd.github.raw"}
    assert c.get(url, headers=raw, params={"ref": "pr-1"}).text == "LIMIT = 1\n"
    assert c.get(url, headers=raw, params={"ref": "pr-3"}).text == "LIMIT = 3\n"
    assert c.get(url, headers=raw, params={"ref": "main"}).text == "LIMIT = 3\n"  # a branch -> HEAD
    # an EMPTY value is an absent one, as it is for `?protected=`: real answers
    # `contents/README.md?ref=` with the file (measured on psf/requests)
    assert c.get(url, headers=raw, params={"ref": ""}).text == "LIMIT = 3\n"
    assert (
        c.get(url, headers=gh_admin_h, params={"ref": "pr-2"}).status_code == 404
    )  # named by no one

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


# A repo that exists only as a `subtype: repo` record, beside a repo with one readable document.
# `github.schema.json` says the record-only repo "stays visible to a scoped caller exactly when one of
# its documents is, and to the admin as soon as the record itself exists"; this is what pins it.
_GH_CONTAINER_ONLY_DOCS = [
    {"source_type": "github", "subtype": "repo", "repo": "pipeline"},
    {
        "source_type": "github",
        "doc_id": "gh-docs-1",
        "repo": "docs-site",
        "group": "engineering",
        "title": "Docs build is red",
        "content": "The nightly docs build fails on the API reference page.",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "state": "open",
    },
]


def test_github_a_container_only_repo_reaches_the_admin_and_no_scoped_caller(tmp_path):
    """The repo visibility checks ask "can this caller see anything in the repo?" with an existence
    read (`store.has_visible_document`) rather than by counting every document, which was #134's
    swap on the Jira side. The one thing the swap could have lost is the admin's view of a repo
    that holds no document at all: `has_visible_document` is False for it under every ACL, the
    admin's included, so the `ids is None` short-circuit in `_repo_visible` and `_visible_repos` is
    load-bearing here where it was a no-op on Jira, whose records cannot create an empty container.
    Each route that resolves a repo goes through `_require_repo`, so one of them stands for all."""
    from backlot.acl import Acl, Caller

    settings = build_corpus(tmp_path, _GH_CONTAINER_ONLY_DOCS)
    with client_for(settings, reload=True) as c:
        org = c.get("/_meta/users").json()["org"]
        tokens = yaml.safe_load(settings.tokens_path.read_text())
        admin = {"Authorization": f"Bearer {tokens['admin_token']}"}
        ava = {"Authorization": f"Bearer {tok(tokens, 'ava@acme.com')}"}

        def names(h, path):
            # sorted: the two listings order differently with nothing sent (see `_ORG_REPO_ORDERING`
            # and `_USER_REPO_ORDERING`), and the question here is who sees what, not in what order
            return sorted(r["name"] for r in c.get(path, headers=h).json())

        for listing in ("/github/user/repos", f"/github/orgs/{org}/repos"):
            assert names(admin, listing) == ["docs-site", "pipeline"], listing
            assert names(ava, listing) == ["docs-site"], listing
        assert c.get(f"/github/repos/{org}/pipeline", headers=admin).status_code == 200
        assert c.get(f"/github/repos/{org}/pipeline", headers=ava).status_code == 404
        assert c.get(f"/github/repos/{org}/pipeline/issues", headers=admin).json() == []
        assert c.get(f"/github/repos/{org}/docs-site", headers=ava).status_code == 200
        # the `repo:` qualifier on an issue search resolves the repo by the same rule: the admin
        # searches the empty repo and gets nothing, a scoped caller gets the 422 a repo they cannot
        # see shares with one that does not exist, so the record's existence is not confirmed
        r = c.get(f"/github/search/issues?q=repo:{org}/pipeline", headers=admin)
        assert (r.status_code, r.json()["total_count"]) == (200, 0)
        assert c.get(f"/github/search/issues?q=repo:{org}/pipeline", headers=ava).status_code == 422
        assert c.get(f"/github/search/issues?q=repo:{org}/nosuch", headers=ava).status_code == 422

        # ...and the existence read is why the short-circuit has to stay: on the container-only
        # repo it says False to everyone, while it agrees with the count wherever a document exists
        conn = store.connect_ro(settings.db_path)
        try:
            acl = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
            scoped = acl.visible_ids(conn, Caller(email="ava@acme.com", is_admin=False))
            for ids in (None, scoped, set()):
                assert store.has_visible_document(conn, "github", "pipeline", ids) is False, ids
                assert store.has_visible_document(conn, "github", "docs-site", ids) is (
                    store.count_documents(conn, "github", "docs-site", ids) > 0
                ), ids
            assert store.has_visible_document(conn, "github", "docs-site", set()) is False
        finally:
            conn.close()


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
        "/commits/main",
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
    point of this route over `/branches/{branch}`, which cannot carry that in one path segment.

    `diffable`, because the slashed branch has to be a branch the repo HAS: a pull of this repo
    heads `chore/rename`, which is why the listing holds it.
    """
    c, _ = gh_client
    base = f"/github/repos/{gh_org}/diffable"
    r = c.get(f"{base}/git/ref/heads/main", headers=gh_admin_h)
    assert r.status_code == 200
    body = r.json()
    assert body["ref"] == "refs/heads/main" and body["object"]["type"] == "commit"
    branch = c.get(f"{base}/branches/main", headers=gh_admin_h).json()
    assert body["object"]["sha"] == branch["commit"]["sha"]  # the two must agree

    slashed = c.get(f"{base}/git/ref/heads/chore/rename", headers=gh_admin_h).json()
    assert slashed["ref"] == "refs/heads/chore/rename"
    # the sha it hands back is usable as a git/trees ref, which is what a pinning client does next
    tree = c.get(f"{base}/git/trees/{slashed['object']['sha']}", headers=gh_admin_h)
    assert tree.status_code == 200

    # `refs/heads/main` is the FULLY-QUALIFIED spelling, and this route does not take it: real
    # answers 404 with the get-a-reference endpoint's own body (measured on psf/requests —
    # `{"message": "Not Found", "documentation_url": ".../git/refs#get-a-reference"}`, a missing
    # ref rather than a missing route). Accepting it let a client that sends the git spelling pass
    # here and 404 in production. The ref this route ANSWERS with is still fully qualified, which
    # is what the assertions above pin.
    assert c.get(f"{base}/git/ref/refs/heads/main", headers=gh_admin_h).status_code == 404

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


def test_github_answers_the_corpus_spelling_whatever_case_was_asked_for(
    gh_client, gh_admin_h, gh_org, gh_user_tokens
):
    """A url Backlot answers names the org and the repo as the corpus spells them, not as the
    caller typed them — and a repo asked for in another case resolves rather than 404ing.

    Real normalizes both segments and 404s neither: `GET /repos/PSF/REQUESTS` answers
    `name: requests`, `full_name: psf/requests` and every url, template and git url lowercase,
    `/orgs/PSF` answers `login: psf` and `url: .../orgs/psf`, and an issue item's `url`,
    `repository_url` and `html_url` are lowercase too — measured on api.github.com 2026-09-03,
    200 each with no redirect, so the normalization is in the body rather than in a `Location`.
    Echoing the caller's spelling gives one resource two identities: a client keying a cache on the
    url it got back stores both, and `/orgs/ACME` and `/orgs/acme` reported different `id`s, since
    the id is synthesized from the name it was asked with.

    Resolving the repo case-insensitively must not widen what a scoped token sees, which is the
    last two assertions: the name is canonicalized before the visibility check, never instead of
    it.
    """
    c, _ = gh_client
    shout, shout_repo = gh_org.upper(), "CODEBASE"
    assert shout != gh_org, "this asserts nothing unless the org has a case to get wrong"

    def shouted(body):
        """Every string in the response carrying a spelling the corpus does not use."""
        if isinstance(body, dict):
            return [u for v in body.values() for u in shouted(v)]
        if isinstance(body, list):
            return [u for v in body for u in shouted(v)]
        if not isinstance(body, str):
            return []
        return [body] if shout in body or shout_repo in body else []

    for path in ("", "/branches/main", "/issues", "/pulls", "/readme", "/tags", "/collaborators"):
        r = c.get(f"/github/repos/{shout}/{shout_repo}{path}", headers=gh_admin_h)
        assert r.status_code == 200, path
        assert shouted(r.json()) == [], f"{path} echoed the caller's: {shouted(r.json())}"

    base = f"/github/repos/{shout}/{shout_repo}"
    branch = c.get(f"{base}/branches/main", headers=gh_admin_h).json()
    assert branch["_links"]["self"].endswith(f"/repos/{gh_org}/codebase/branches/main")
    assert branch["_links"]["html"] == f"https://github.com/{gh_org}/codebase/tree/main"
    assert branch["protection_url"] == f"{branch['_links']['self']}/protection"
    assert f"/repos/{gh_org}/codebase/" in branch["commit"]["url"]

    repo = c.get(base, headers=gh_admin_h).json()
    assert repo["name"] == "codebase" and repo["full_name"] == f"{gh_org}/codebase"
    assert repo["html_url"] == f"https://github.com/{gh_org}/codebase"
    assert repo["owner"]["login"] == gh_org
    # the same object either spelling reaches it by, ids and templates included
    assert repo == c.get(f"/github/repos/{gh_org}/codebase", headers=gh_admin_h).json()

    # `/orgs/{org}` is held to the same rule, and the synthesized `id` is the sharp end of it:
    # derived from the name, it forked into two values for one org.
    org = c.get(f"/github/orgs/{shout}", headers=gh_admin_h).json()
    assert org == c.get(f"/github/orgs/{gh_org}", headers=gh_admin_h).json()
    assert org["login"] == gh_org and org["html_url"] == f"https://github.com/{gh_org}"
    assert org["url"].endswith(f"/orgs/{gh_org}") and org["repos_url"].endswith("/repos")

    # A `repo:` qualifier resolves the same way, on both search routes: real answers the same
    # 4,173 for `repo:PSF/Requests is:issue` as for the lowercase spelling, with `repository_url`
    # canonical (measured 2026-09-04). A qualifier that resolved in only one case would read as
    # "that repo has no matches" rather than as a spelling this server would not take.
    for route, named in (("issues", "gateway"), ("code", "codebase")):
        url = f"/github/search/{route}"
        loud = c.get(url, headers=gh_admin_h, params={"q": f"repo:{named.upper()}"})
        quiet = c.get(url, headers=gh_admin_h, params={"q": f"repo:{named}"})
        assert loud.status_code == 200 and quiet.json()["total_count"] > 0, route
        assert loud.json() == quiet.json(), route

    # 'vault' is invisible to bob, in whatever case he asks for it
    bob = {"Authorization": f"Bearer {gh_user_tokens['bob@acme.com']}"}
    assert c.get(f"/github/repos/{gh_org}/VAULT", headers=bob).status_code == 404
    assert c.get(f"/github/repos/{gh_org}/VAULT", headers=gh_admin_h).status_code == 200


def test_github_a_repo_qualifier_naming_nothing_is_refused_rather_than_widened(
    gh_client, gh_admin_h, gh_org, gh_user_tokens
):
    """A `repo:` that resolves to no repository is real's 422 on `/search/issues`, not the whole
    corpus's issues — and `incomplete_results` on `/search/code`, not a silent zero.

    Measured on api.github.com on 2026-09-04. `search/issues?q=repo:psf/ghost-zz-9876` and
    `repo:someone-else-zz/requests` both answer 422 with `code: "invalid"` and "The listed users and
    repositories cannot be searched…", which is also the answer for a repository the token cannot
    see — so the body never says which of the two it was, and neither does this. The refusal is for
    a qualifier that resolves to NOTHING: `repo:psf/requests repo:psf/ghost-zz-9876 timeout` is a
    200 answering psf/requests' 846 items.

    `search/code` answers those same queries 200 with `total_count: 0` and
    `incomplete_results: true`, and `false` for the mix — the one field its answer differs in.
    """
    c, _ = gh_client
    issues, code = "/github/search/issues", "/github/search/code"

    def ask(path, q, headers=gh_admin_h):
        r = c.get(path, headers=headers, params={"q": q})
        return r.status_code, r.json()

    assert ask(issues, "repo:gateway")[1]["total_count"] > 0  # a name that resolves still narrows

    for q in ("repo:ghost", f"repo:{gh_org}/ghost", "repo:someone-else/gateway"):
        status, body = ask(issues, q)
        assert status == 422, q
        assert body["errors"][0]["code"] == "invalid" and body["errors"][0]["field"] == "q"
        assert body["errors"][0]["message"].startswith("The listed users and repositories")
        assert body["documentation_url"] == "https://docs.github.com/v3/search/"
    # one that resolves beside one that does not is not refused
    assert ask(issues, "repo:gateway repo:ghost")[1]["total_count"] > 0

    # a repository the caller cannot see answers the refusal too, so the 422 does not confirm it
    bob = {"Authorization": f"Bearer {gh_user_tokens['bob@acme.com']}"}
    assert ask(issues, "repo:vault", bob)[0] == 422
    assert ask(issues, "repo:vault")[0] == 200

    for q in ("repo:ghost line", "repo:someone-else/codebase line"):
        status, body = ask(code, q)
        assert (status, body["total_count"], body["incomplete_results"]) == (200, 0, True), q
    assert ask(code, "repo:codebase repo:ghost line")[1]["incomplete_results"] is False
    assert ask(code, "repo:vault line", bob)[1]["incomplete_results"] is True


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


def test_github_json_carries_the_charset_real_sends_except_on_code_search(
    gh_client, gh_admin_h, gh_org
):
    """Real answers `application/json; charset=utf-8` on every GitHub JSON response measured
    (2026-09-06: nine 200s, one of them with no credential, a 404, a 422, the version 400 and a 401)
    and `application/json` on
    `/search/code`, whose backend is not the rest of the API's, on its 200 and its 422s alike; the
    401 on that path is the gateway's and carries the charset (2026-09-07), as does the answer to a
    wrong method. Backlot answered FastAPI's bare `application/json` everywhere, so a client or a
    recorded fixture comparing the header as a string agreed with real on code search alone. The
    other media types this router answers are not JSON and are not touched."""
    c, _ = gh_client
    from backlot import synth

    utf8, bare = "application/json; charset=utf-8", "application/json"
    pr = synth.github_number("gh-pr-1")
    repo_id = c.get(f"/github/repos/{gh_org}/gateway", headers=gh_admin_h).json()["id"]
    cells = [
        (f"/github/repos/{gh_org}/gateway/issues?per_page=1", gh_admin_h, 200, utf8),
        (f"/github/repos/{gh_org}/gateway", gh_admin_h, 200, utf8),
        (f"/github/orgs/{gh_org}", gh_admin_h, 200, utf8),
        ("/github/user/repos?per_page=1", gh_admin_h, 200, utf8),
        ("/github/search/issues?q=is:open&per_page=1", gh_admin_h, 200, utf8),
        (f"/github/repos/{gh_org}/ghost-zz-9876", gh_admin_h, 404, utf8),
        ("/github/search/issues?q=", gh_admin_h, 422, utf8),
        (
            f"/github/repos/{gh_org}/gateway",
            {**gh_admin_h, "X-GitHub-Api-Version": "1999-01-01"},
            400,
            utf8,
        ),
        ("/github/user/repos", {}, 401, utf8),
        ("/github/user/repos", {"Authorization": "Bearer nope"}, 401, utf8),
        # the same repository asked for by id, which the middleware rewrites onto the login path
        (f"/github/repositories/{repo_id}", gh_admin_h, 200, utf8),
        # code search: the one route where real sends no charset, on the statuses its own backend
        # answers, the 200 and the 422s
        ("/github/search/code?q=extension:md", gh_admin_h, 200, bare),
        ("/github/search/code?q=", gh_admin_h, 422, bare),
        ("/github/search/code?q=extension:md&per_page=1&page=1001", gh_admin_h, 422, bare),
        # ...while the 401 on the same path is the gateway's, charset and all
        ("/github/search/code?q=extension:md", {}, 401, utf8),
        ("/github/search/code?q=extension:md", {"Authorization": "Bearer nope"}, 401, utf8),
        # and its parse 400 is text, as before
        (
            "/github/search/code?q=extension:md&per_page=abc",
            gh_admin_h,
            400,
            "text/plain; charset=utf-8",
        ),
        # non-JSON answers on this router keep their own types
        (
            f"/github/repos/{gh_org}/gateway/pulls/{pr}",
            {**gh_admin_h, "Accept": "application/vnd.github.diff"},
            200,
            "application/vnd.github.diff; charset=utf-8",
        ),
    ]
    for path, headers, status, ctype in cells:
        r = c.get(path, headers=headers)
        assert (r.status_code, r.headers["content-type"]) == (status, ctype), path
    # a wrong method is Starlette's 405 (real answers a 404 there, see `errors.github.http_body`),
    # and it is the gateway's kind of answer on code search too: the charset stays
    for path in ("/github/search/code?q=extension:md", f"/github/repos/{gh_org}/gateway"):
        r = c.post(path, headers=gh_admin_h)
        assert (r.status_code, r.headers["content-type"]) == (405, utf8), path
    # the OpenAPI document still keys the JSON body as `application/json`, as real's spec does: the
    # charset is on the wire, not in the contract `backlot diff` compares
    op = c.get("/openapi.json").json()["paths"]["/github/repos/{owner}/{repo}/issues"]["get"]
    assert list(op["responses"]["200"]["content"]) == ["application/json"]
    # ...and a JSON response outside `/github` is untouched, the app's own routes and another
    # vendor's alike: not a claim about what Atlassian sends, only that this rule is GitHub's alone
    assert c.get("/openapi.json").headers["content-type"] == bare
    server_info = c.get("/atlassian/rest/api/2/serverInfo", headers=gh_admin_h)
    assert (server_info.status_code, server_info.headers["content-type"]) == (200, bare)


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
        check=False,
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
        ["git", "apply", "--reverse", "--check", "pr.diff"],
        cwd=wt,
        capture_output=True,
        text=True,
        check=False,
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
    client paging until it had that many, and leaked that a hidden file carries a comment.

    `sort=comments` orders by that same served count, so a row sits where its own numbers put it."""
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

    # The listing orders by the count the caller is SERVED. When the key counted the raw rows, the
    # comment on `secret/keys.txt` counted for everyone: bob's `sort=comments&direction=asc` put
    # the unresolvable pull ahead of the declared one, two rows whose own counts then DESCENDED
    # 3, 1, and that position was the one place a hidden file's comment still showed.
    for headers, tail in ((gh_admin_h, [(unres, 2), (num, 3)]), (bob, [(unres, 1), (num, 3)])):
        rows = c.get(
            f"/github/repos/{gh_org}/diffable/issues",
            params={"sort": "comments", "direction": "asc", "per_page": 100},
            headers=headers,
        ).json()
        served = [
            (
                r["number"],
                r["comments"]
                + c.get(f"/github/repos/{gh_org}/diffable/pulls/{r['number']}", headers=headers)
                .json()
                .get("review_comments", 0),
            )
            for r in rows
        ]
        counts = [n for _, n in served]
        assert counts == sorted(counts), counts
        assert served[-2:] == tail


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


def test_github_the_statuses_listing_declares_the_page_parameters_real_accepts(client):
    """`/statuses/{sha}` answers `[]` on every page on both sides, so its page parameters change no
    body a client can read — they change the contract, which is what `backlot mcp` hands an agent as
    a tool. Real accepts them: measured on api.github.com on 2026-09-04, a `kubernetes/kubernetes`
    pull head at `?per_page=1` answers one status with a `Link` carrying `rel="next"`. GitHub's
    published OpenAPI declares this legacy route not at all, so no baseline entry covers it and the
    contract is the only place the divergence shows.
    """
    path = "/github/repos/{owner}/{repo}/statuses/{sha}"
    op = client.get("/openapi.json").json()["paths"][path]["get"]
    assert {"page", "per_page"} <= {p["name"] for p in op.get("parameters", [])}


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


# --- sort, direction and the repository filters ---------------------------------------


def _ratelimit(response) -> dict[str, str]:
    """The five `x-ratelimit-*` headers of a response, keyed without the prefix."""
    names = ("limit", "remaining", "used", "reset", "resource")
    return {n: response.headers[f"x-ratelimit-{n}"] for n in names}


def _numbers(c, path: str, h: dict, **params) -> list[int]:
    r = c.get(path, headers=h, params=params)
    assert r.status_code == 200, (path, params, r.text)
    return [x["number"] for x in r.json()]


def _sort_param(spec: dict, path: str, name: str) -> dict:
    params = spec["paths"][path]["get"]["parameters"]
    return next(p for p in params if p["name"] == name)


def test_github_sort_and_direction_order_the_issue_and_pull_listings_as_measured(tmp_path):
    """GitHub's OpenAPI description declares `sort` and `direction` on both listings with an enum
    and a default (issues `[created, updated, comments]` / `created` and `[asc, desc]` / `desc`;
    pulls `[created, updated, popularity, long-running]` / `created` and `[asc, desc]` with no
    default, read 2026-09-10); Backlot declared neither, read neither, and answered every request
    in the store's number order, so `?sort=updated` fetched the same rows as nothing at all.

    Each cell below is what api.github.com answered on 2026-09-09 and 2026-09-10 against
    `psf/requests` (the numbers are in `_ISSUE_ORDERING` and `_PULL_ORDERING`); the corpus here
    is six documents whose creation, update and comment orders all differ, so every cell tells the
    key it names from the others. The wire departs from the description in three places this
    corpus shows: a pull listing with `sort=created` sent is the REVERSE of one without (oldest
    first, where the prose says `desc`); `long-running` orders by creation and filters nothing
    (closed pulls from 2016 answered it); and `comments` and `popularity` order by a pull's
    conversation and review comments added together, a number no served member carries (5797's
    `comments: 105` sat between 211 and 122). A value outside either enum is absorbed and answers
    200 everywhere, in a different order per listing and per parameter, each measured.
    """
    when = {1: "01-10", 2: "02-10", 3: "03-10", 4: "04-10", 5: "05-10", 6: "06-10"}
    updated = {1: "06-20", 2: "02-11", 3: "03-11", 4: "07-01", 5: "05-11", 6: "06-12"}
    pulls = {2, 4, 6}
    ava = {"content": "c", "author_email": "ava@acme.com"}
    comments = {
        2: [ava, {**ava, "path": "src/a.py", "line": 1}, {**ava, "path": "src/b.py", "line": 2}],
        3: [ava, ava],
        5: [ava],
        6: [ava],
    }
    docs = [
        {
            "source_type": "github",
            "doc_id": f"gh-sorted-{n}",
            "repo": "sorted",
            "subtype": "pull_request" if n in pulls else "issue",
            "number": n,
            "title": f"Document {n}",
            "content": "body",
            "author_email": "bob@acme.com",
            "visibility": "public",
            "state": "open",
            "created": f"2026-{when[n]}T00:00:00Z",
            "updated": f"2026-{updated[n]}T00:00:00Z",
            "comments": comments.get(n, []),
            **({"head": f"feat/{n}", "base": "main"} if n in pulls else {}),
        }
        for n in range(1, 7)
    ]
    # The two paths document 2's review comments anchor to are files of this repo, so the comments
    # resolve and are served. `sort=comments` orders by the count the caller is served, and a
    # comment on a path no file answers is not one of them (see `store.github_comment_counts`).
    docs += [
        {
            "source_type": "github",
            "doc_id": f"gh-sorted-file-{path.replace('/', '-')}",
            "repo": "sorted",
            "subtype": "file",
            "path": path,
            "title": path,
            "content": "x = 1\n",
            "author_email": "bob@acme.com",
            "visibility": "public",
        }
        for path in ("src/a.py", "src/b.py")
    ]
    settings = build_corpus(tmp_path, docs, name="sorted.jsonl")
    with client_for(settings, reload=True) as c:
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        org = c.get("/_meta/users").json()["org"]
        spec = c.get("/openapi.json").json()
        issues_path = "/github/repos/{owner}/{repo}/issues"
        pulls_path = "/github/repos/{owner}/{repo}/pulls"
        sort = _sort_param(spec, issues_path, "sort")
        assert sort["description"] == "What to sort results by."
        assert sort["schema"]["enum"] == ["created", "updated", "comments"]
        assert sort["schema"]["default"] == "created" and sort["schema"]["type"] == "string"
        direction = _sort_param(spec, issues_path, "direction")
        assert direction["description"] == "The direction to sort the results by."
        assert direction["schema"]["enum"] == ["asc", "desc"]
        assert direction["schema"]["default"] == "desc"
        sort = _sort_param(spec, pulls_path, "sort")
        assert sort["description"].startswith("What to sort results by. `popularity` will sort by")
        assert sort["schema"]["enum"] == ["created", "updated", "popularity", "long-running"]
        assert sort["schema"]["default"] == "created"
        direction = _sort_param(spec, pulls_path, "direction")
        assert direction["description"] == (
            "The direction of the sort. Default: `desc` when sort is `created` or sort is not "
            "specified, otherwise `asc`."
        )
        assert direction["schema"]["enum"] == ["asc", "desc"]
        assert "default" not in direction["schema"]

        issues = f"/github/repos/{org}/sorted/issues"
        # the unsent order is `sort=created`'s, newest first, and every sort descends by default
        assert _numbers(c, issues, h) == [6, 5, 4, 3, 2, 1]
        assert _numbers(c, issues, h, sort="created") == [6, 5, 4, 3, 2, 1]
        assert _numbers(c, issues, h, sort="updated") == [4, 1, 6, 5, 3, 2]
        # 2 has one conversation comment and two review comments, and orders as three; a tie
        # keeps the unsent order whatever the direction (6 before 5 at one comment each, 4 before
        # 1 at none)
        assert _numbers(c, issues, h, sort="comments") == [2, 3, 6, 5, 4, 1]
        assert _numbers(c, issues, h, sort="comments", direction="asc") == [4, 1, 6, 5, 3, 2]
        (two,) = [x for x in c.get(issues, headers=h).json() if x["number"] == 2]
        assert two["comments"] == 1  # the served member counts the conversation alone
        assert _numbers(c, issues, h, direction="asc") == [1, 2, 3, 4, 5, 6]
        assert _numbers(c, issues, h, sort="updated", direction="asc") == [2, 3, 5, 6, 1, 4]
        # a value outside either enum drops the other parameter too, on this listing
        for absorbed in (
            {"sort": "bogus"},
            {"sort": "bogus", "direction": "asc"},
            {"direction": "bogus"},
            {"sort": "updated", "direction": "bogus"},
        ):
            assert _numbers(c, issues, h, **absorbed) == [6, 5, 4, 3, 2, 1], absorbed

        pulls = f"/github/repos/{org}/sorted/pulls"
        # newest first unsent, oldest first the moment a sort is sent, `created` included
        assert _numbers(c, pulls, h) == [6, 4, 2]
        assert _numbers(c, pulls, h, sort="created") == [2, 4, 6]
        assert _numbers(c, pulls, h, sort="updated") == [2, 6, 4]
        assert _numbers(c, pulls, h, sort="updated", direction="desc") == [4, 6, 2]
        assert _numbers(c, pulls, h, sort="popularity") == [4, 6, 2]
        assert _numbers(c, pulls, h, sort="popularity", direction="desc") == [2, 6, 4]
        assert _numbers(c, pulls, h, sort="long-running") == [2, 4, 6]  # no filter, see above
        assert _numbers(c, pulls, h, direction="asc") == [2, 4, 6]
        assert _numbers(c, pulls, h, direction="desc") == [6, 4, 2]
        # an unknown sort is read as `created` with the direction honoured; an unknown direction
        # is `desc` with the sort kept
        assert _numbers(c, pulls, h, sort="bogus") == [2, 4, 6]
        assert _numbers(c, pulls, h, sort="bogus", direction="desc") == [6, 4, 2]
        assert _numbers(c, pulls, h, direction="bogus") == [6, 4, 2]
        assert _numbers(c, pulls, h, sort="updated", direction="bogus") == [4, 6, 2]

        # the page urls carry the caller's own parameters and none the handler applied
        link = c.get(issues, headers=h, params={"sort": "updated", "per_page": 2}).headers["Link"]
        assert "sort=updated" in link and "direction=" not in link
        assert _numbers(c, issues, h, sort="updated", per_page=2, page=2) == [6, 5]
        link = c.get(pulls, headers=h, params={"per_page": 1}).headers["Link"]
        assert "sort=" not in link and "direction=" not in link
        assert _numbers(c, pulls, h, sort="created", per_page=1, page=2) == [4]


def test_github_type_visibility_sort_and_direction_on_the_repository_listings(tmp_path):
    """`GET /orgs/{org}/repos` takes `type`, `sort` and `direction` and `GET /user/repos`
    `visibility`, `sort` and `direction`, each declared in GitHub's OpenAPI description with an
    enum and a default (`type` `[all, public, private, forks, sources, member]` / `all`;
    `visibility` `[all, public, private]` / `all`; `sort` `[created, updated, pushed, full_name]`
    with `created` the default on the one and `full_name` on the other; `direction` with none),
    read 2026-09-10. Backlot declared none of the six and answered every request in name order.

    The orders are the cells `_ORG_REPO_ORDERING` and `_USER_REPO_ORDERING` carry, measured on
    api.github.com on 2026-09-09 and 2026-09-10 against the `psf` organization and the token's own
    repositories: an organization's repositories come OLDEST first with no sort and newest first
    with `sort=created` sent; `full_name` is the one sort that ascends by default; an unknown
    direction is `asc` on the organization listing and `desc` on the token's own. `type` selects
    on the one fact a corpus states about a repository's kind, its ACL: `public` and `private` are
    the org-wide grant or its absence, `forks` and `member` answer nothing (every repository here is
    the organization's own and `fork: false`, as `type=member` answered `[]` for `psf`), `sources`
    and a value outside the enum answer every repository. `type` and `affiliation` on
    `/user/repos` select on what the caller is to each repository, which no corpus states, and stay
    undeclared. The three names are chosen so the derived creation order is not the name order,
    which the precondition below holds.
    """
    names = ("gateway", "ledger", "portal")
    by_created = sorted(names, key=lambda n: synth.epoch("repo:" + n))
    assert by_created != sorted(names), "pick names whose derived order differs from name order"
    docs = [
        {
            "source_type": "github",
            "doc_id": f"gh-{name}-issue",
            "repo": name,
            "title": f"{name} issue",
            "content": "body",
            "author_email": "bob@acme.com",
            "author_groups": ["engineering"],
            "group": "engineering",
            # `ledger` has no org-wide grant on any document, so it is the private repository
            "visibility": "group" if name == "ledger" else "public",
        }
        for name in names
    ]
    settings = build_corpus(tmp_path, docs, name="repos.jsonl")
    with client_for(settings, reload=True) as c:
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        org = c.get("/_meta/users").json()["org"]
        spec = c.get("/openapi.json").json()
        org_path, user_path = "/github/orgs/{org}/repos", "/github/user/repos"
        kind = _sort_param(spec, org_path, "type")
        assert kind["description"] == "Specifies the types of repositories you want returned."
        assert kind["schema"]["enum"] == ["all", "public", "private", "forks", "sources", "member"]
        assert kind["schema"]["default"] == "all"
        visibility = _sort_param(spec, user_path, "visibility")
        assert visibility["description"] == (
            "Limit results to repositories with the specified visibility."
        )
        assert visibility["schema"]["enum"] == ["all", "public", "private"]
        assert visibility["schema"]["default"] == "all"
        for path, default in ((org_path, "created"), (user_path, "full_name")):
            sort = _sort_param(spec, path, "sort")
            assert sort["description"] == "The property to sort the results by.", path
            assert sort["schema"]["enum"] == ["created", "updated", "pushed", "full_name"], path
            assert sort["schema"]["default"] == default, path
            direction = _sort_param(spec, path, "direction")
            assert direction["description"] == (
                "The order to sort by. Default: `asc` when using `full_name`, otherwise `desc`."
            ), path
            assert direction["schema"]["enum"] == ["asc", "desc"] and (
                "default" not in direction["schema"]
            ), path
        declared = {p["name"] for p in spec["paths"][user_path]["get"]["parameters"]}
        assert declared == {"visibility", "sort", "direction", "page", "per_page"}

        def listed(path, **params):
            r = c.get(path, headers=h, params=params)
            assert r.status_code == 200, (path, params, r.text)
            return [x["name"] for x in r.json()]

        by_name = sorted(names)
        newest_first = list(reversed(by_created))
        org_repos = f"/github/orgs/{org}/repos"
        assert listed(org_repos) == by_created
        assert listed(org_repos, sort="created") == newest_first
        assert listed(org_repos, sort="updated") == newest_first
        assert listed(org_repos, sort="pushed") == newest_first
        assert listed(org_repos, sort="full_name") == by_name
        assert listed(org_repos, sort="full_name", direction="desc") == by_name[::-1]
        assert listed(org_repos, direction="desc") == newest_first
        assert listed(org_repos, direction="asc") == by_created
        assert listed(org_repos, sort="bogus") == newest_first
        assert listed(org_repos, direction="bogus") == by_created
        assert listed(org_repos, sort="pushed", direction="bogus") == by_created
        assert listed(org_repos, sort="full_name", direction="bogus") == by_name
        assert listed(org_repos, type="private") == ["ledger"]
        assert listed(org_repos, type="public") == [n for n in by_created if n != "ledger"]
        assert listed(org_repos, type="forks") == [] and listed(org_repos, type="member") == []
        for every in ("all", "sources", "bogus"):
            assert listed(org_repos, type=every) == by_created, every
        (ledger,) = [x for x in c.get(org_repos, headers=h).json() if x["name"] == "ledger"]
        assert ledger["private"] is True and ledger["visibility"] == "private"

        user_repos = "/github/user/repos"
        assert listed(user_repos) == by_name
        assert listed(user_repos, sort="full_name") == by_name
        assert listed(user_repos, sort="created") == newest_first
        assert listed(user_repos, sort="created", direction="asc") == by_created
        assert listed(user_repos, direction="desc") == by_name[::-1]
        assert listed(user_repos, sort="bogus") == newest_first
        assert listed(user_repos, sort="created", direction="bogus") == newest_first
        assert listed(user_repos, visibility="private") == ["ledger"]
        assert listed(user_repos, visibility="public") == ["gateway", "portal"]
        assert listed(user_repos, visibility="bogus") == by_name
        # `type` is not declared on this listing and does not select either
        assert listed(user_repos, type="private") == by_name

        # the page urls carry the caller's own filters
        link = c.get(org_repos, headers=h, params={"type": "public", "per_page": 1}).headers["Link"]
        assert "type=public" in link and "sort=" not in link
        assert (
            listed(org_repos, type="public", per_page=1, page=2)
            == [n for n in by_created if n != "ledger"][1:]
        )
        link = c.get(user_repos, headers=h, params={"sort": "created", "per_page": 1}).headers[
            "Link"
        ]
        assert "sort=created" in link and "visibility=" not in link


# --- rate limits ------------------------------------------------------------------


def test_github_every_response_carries_the_five_ratelimit_headers_and_rate_limit_reports_them(
    tmp_path, monkeypatch
):
    """Real puts five `x-ratelimit-*` headers on every response it gives and serves
    `GET /rate_limit`; Backlot sent none of the five on any response and answered the route 404,
    so a client that paces by `remaining` and sleeps until `reset` never ran that path here, and
    PyGithub's `get_rate_limit()` raised before a crawl's first request.

    Measured against api.github.com on 2026-09-09 and 2026-09-10 (curl unauthenticated, `gh api`
    authenticated): `limit: 60` on `core` and `10` on `search` for a caller with no credential,
    `5000`, `30` and `10` on `core`, `search` and `code_search` for a token; the five on the 200s,
    on the 404 for a repository that does not exist, on the 401s, on the blank-`q` 422 (against
    `search`) and on the version 400; a `HEAD` counted once (`remaining` 46 → 45); the 401 an
    anonymous code search gets counted against `core`, not `code_search`; `reset` in epoch
    seconds, the same on every answer inside a window; `GET /rate_limit` 200 with
    `resources.core`, `.search`, `.code_search` each `{limit, used, remaining, reset}` and `rate`
    beside them under `2022-11-28` and not under `2026-03-10`, carrying the five itself and not
    counting (two reads in a row both `used: 0`); a bad bearer on it the 401. What real's route
    reports is a fresh window rather than the headers' (see the route's docstring); Backlot reports
    the headers'. Exhaustion is not measured and nothing here refuses: `remaining` stops at 0 and
    `used` keeps counting.
    """
    from backlot.routers import github as gh

    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "github",
                "doc_id": "gh-rl-issue",
                "repo": "rl",
                "title": "Paced",
                "content": "body",
                "author_email": "bob@acme.com",
                "visibility": "public",
            },
            {
                "source_type": "github",
                "doc_id": "gh-rl-file",
                "repo": "rl",
                "subtype": "file",
                "path": "README.md",
                "title": "README.md",
                "content": "# rl\n",
                "author_email": "bob@acme.com",
                "visibility": "public",
            },
        ],
        name="rl.jsonl",
    )
    with client_for(settings, reload=True) as c:
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        users = c.get("/_meta/users").json()
        org, bob = users["org"], {"Authorization": f"Bearer {users['users'][0]['token']}"}
        repo = f"/github/repos/{org}/rl"

        first = c.get(repo, headers=h)
        assert first.status_code == 200
        five = _ratelimit(first)
        assert five == {
            "limit": "5000",
            "remaining": "4999",
            "used": "1",
            "reset": five["reset"],
            "resource": "core",
        }
        reset = int(five["reset"])
        # the HEAD counts once and carries the five with the rest of the GET's headers
        head = c.head(repo, headers=h)
        assert head.content == b"" and _ratelimit(head)["used"] == "2"
        # the errors count against `core` too: the 404, the version 400
        ghost = c.get(f"/github/repos/{org}/ghost-zz-9876", headers=h)
        assert (ghost.status_code, _ratelimit(ghost)["used"]) == (404, "3")
        bad_version = c.get(repo, headers={**h, "X-GitHub-Api-Version": "1999-01-01"})
        assert (bad_version.status_code, _ratelimit(bad_version)["used"]) == (400, "4")
        assert _ratelimit(bad_version)["reset"] == str(reset)  # the window stays put
        # search and code search are their own resources at their own limits
        found = c.get("/github/search/issues", headers=h, params={"q": f"repo:{org}/rl"})
        assert found.status_code == 200
        assert _ratelimit(found) == {
            "limit": "30",
            "remaining": "29",
            "used": "1",
            "reset": _ratelimit(found)["reset"],
            "resource": "search",
        }
        blank = c.get("/github/search/issues", headers=h, params={"q": ""})
        assert (blank.status_code, _ratelimit(blank)["used"]) == (422, "2")
        code = c.get("/github/search/code", headers=h, params={"q": "extension:md"})
        assert code.status_code == 200
        assert (_ratelimit(code)["limit"], _ratelimit(code)["resource"]) == ("10", "code_search")
        # a caller with no credential is counted by address at 60, the anonymous code search's
        # 401 against `core`
        anonymous = c.get("/github/user/repos")
        assert anonymous.status_code == 401
        assert _ratelimit(anonymous) == {
            "limit": "60",
            "remaining": "59",
            "used": "1",
            "reset": _ratelimit(anonymous)["reset"],
            "resource": "core",
        }
        anonymous_code = c.get("/github/search/code", params={"q": "extension:md"})
        assert anonymous_code.status_code == 401
        assert (_ratelimit(anonymous_code)["resource"], _ratelimit(anonymous_code)["used"]) == (
            "core",
            "2",
        )
        # another token is another window
        assert _ratelimit(c.get(repo, headers=bob))["used"] == "1"

        # the route reports the windows the headers report, and does not count
        status = c.get("/github/rate_limit", headers=h)
        assert status.status_code == 200
        assert status.headers["content-type"] == "application/json; charset=utf-8"
        core = {"limit": 5000, "used": 4, "remaining": 4996, "reset": reset}
        assert status.json() == {
            "resources": {
                "core": core,
                "search": {
                    "limit": 30,
                    "used": 2,
                    "remaining": 28,
                    "reset": int(_ratelimit(found)["reset"]),
                },
                "code_search": {
                    "limit": 10,
                    "used": 1,
                    "remaining": 9,
                    "reset": int(_ratelimit(code)["reset"]),
                },
            },
            "rate": core,
        }
        assert _ratelimit(status) == {**five, "remaining": "4996", "used": "4"}
        again = c.get("/github/rate_limit", headers=h)
        assert again.json() == status.json() and _ratelimit(again)["used"] == "4"
        assert (
            "rate"
            not in c.get(
                "/github/rate_limit", headers={**h, "X-GitHub-Api-Version": "2026-03-10"}
            ).json()
        )
        # no credential is answered at the anonymous limits; a bad one is the 401
        unauthenticated = c.get("/github/rate_limit")
        assert unauthenticated.status_code == 200
        resources = unauthenticated.json()["resources"]
        assert (resources["core"]["limit"], resources["core"]["used"]) == (60, 2)
        assert (resources["search"]["limit"], resources["code_search"]["limit"]) == (10, 10)
        assert _ratelimit(unauthenticated)["limit"] == "60"
        refused = c.get("/github/rate_limit", headers={"Authorization": "Bearer nope"})
        assert refused.status_code == 401
        assert refused.json() == {
            "message": "Bad credentials",
            "documentation_url": "https://docs.github.com/rest",
            "status": "401",
        }
        assert _ratelimit(refused)["limit"] == "60"  # counted with the anonymous callers

        # an hour on, the window is a new one: `used` starts over and `reset` moves by the hour
        windows = c.app.state.github_rate_limits
        now = windows.clock()
        windows.clock = lambda: now + gh.RATE_LIMIT_WINDOW + 1
        rolled = _ratelimit(c.get(repo, headers=h))
        assert (rolled["used"], rolled["remaining"]) == ("1", "4999")
        assert int(rolled["reset"]) == int(now) + gh.RATE_LIMIT_WINDOW + 1 + gh.RATE_LIMIT_WINDOW
        # past the limit nothing is refused: `remaining` stops at 0 and `used` keeps counting
        monkeypatch.setitem(gh.RATE_LIMITS, "core", gh._HourlyLimit(60, 2))
        assert _ratelimit(c.get(repo, headers=h)) == {
            **rolled,
            "limit": "2",
            "remaining": "0",
            "used": "2",
        }
        over = c.get(repo, headers=h)
        assert over.status_code == 200
        assert (_ratelimit(over)["remaining"], _ratelimit(over)["used"]) == ("0", "3")
