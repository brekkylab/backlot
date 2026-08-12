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


def test_admin_github_crawls_all(client, admin_h, ro_conn, org):
    repos = client.get(
        f"/github/orgs/{org}/repos", headers=admin_h, params={"per_page": 100}
    ).json()
    seen = []
    for r in repos:
        seen += crawl_github_repo(client, admin_h, org, r["name"])
    assert len(seen) == db_count(ro_conn, "github")


def test_github_body_roundtrip(client, admin_h, ro_conn, org):
    doc = ro_conn.execute("SELECT * FROM github_items LIMIT 1").fetchone()
    from backlot import synth

    num = synth.github_number(doc["doc_id"])
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
            "meta": {"state": "open", "head": "fix/argv", "base": "main"},
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
            "meta": {"state": "open", "head": "chore/rename", "base": "main"},
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
    return c.get("/_mock/users").json()["org"]


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
    # matches the existing github 404 shape (backlot.main's shared exception handler wraps
    # HTTPException(detail=...) as {"detail": ...} for every non-atlassian router)
    assert r.json() == {"detail": "Not Found"}


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


def test_github_file_excluded_from_search_issues(gh_client, gh_admin_h):
    c, _ = gh_client
    # 'helper' only appears in a file's content (src/pkg/utils.py); it must not surface
    # as an issue/PR search hit even though the FTS index covers file content too.
    body = c.get("/github/search/issues", headers=gh_admin_h, params={"q": "helper"}).json()
    assert body["total_count"] == 0
    assert body["items"] == []


def test_github_file_number_index_excludes_files(gh_client, gh_admin_h, gh_org):
    """`kind='file'` rows must never populate app.state.index["github"] (the (repo, number)
    reverse index): a file's synthesized number can collide with a real issue/PR's number
    (see gh-file-collide-88814, which deliberately collides with gh-issue-1's), and if the
    file's doc_id ends up as the map value, a real issue/PR 404s."""
    c, _ = gh_client
    from backlot import synth

    file_doc_ids = {d["doc_id"] for d in _GH_FILE_DOCS}
    idx = c.app.state.index["github"]
    assert not (set(idx.values()) & file_doc_ids)

    # the real issue is still resolvable by number even though a file doc collides with it
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

    ok = c.get(f"/github/repos/{gh_org}/codebase/contents/config/secret.yaml", headers=member_h)
    assert ok.status_code == 200
    hidden = c.get(
        f"/github/repos/{gh_org}/codebase/contents/config/secret.yaml", headers=nonmember_h
    )
    assert hidden.status_code == 404


# --- media-type negotiation: Accept: application/vnd.github.raw (issue #49 D1) ----------
#
# Real GitHub serves the raw bytes when a content endpoint is asked for the `raw` media type; the
# tell that it isn't happening is the byte count disagreeing with the `size` the tree reported for
# the same blob, so that is what these assert against rather than just "some content came back".

_MAIN_PY = "def main():\n    return 1\n"


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
def test_github_blob_accept_raw_returns_the_bytes(gh_client, gh_admin_h, gh_org, accept):
    c, _ = gh_client
    sha = hashlib.sha1(_MAIN_PY.encode()).hexdigest()
    r = c.get(
        f"/github/repos/{gh_org}/codebase/git/blobs/{sha}",
        headers={**gh_admin_h, "Accept": accept},
    )
    assert r.status_code == 200
    assert r.text == _MAIN_PY
    assert len(r.content) == len(_MAIN_PY.encode())  # == the tree's `size` for this blob
    # real GitHub answers git/blobs raw with text/plain (contents/readme use vnd.github.raw)
    assert r.headers["content-type"].startswith("text/plain")


def test_github_contents_accept_raw_returns_the_bytes(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    r = c.get(
        f"/github/repos/{gh_org}/codebase/contents/src/main.py",
        headers={**gh_admin_h, "Accept": "application/vnd.github.raw"},
    )
    assert r.status_code == 200
    assert r.text == _MAIN_PY
    assert r.headers["content-type"].startswith("application/vnd.github.raw")


def test_github_readme_accept_raw_returns_the_bytes(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    text = "# codebase\n\nCore service source, browsable via the tree/contents API.\n"
    r = c.get(
        f"/github/repos/{gh_org}/codebase/readme",
        headers={**gh_admin_h, "Accept": "application/vnd.github.raw"},
    )
    assert r.status_code == 200
    assert r.text == text
    assert r.headers["content-type"].startswith("application/vnd.github.raw")


def test_github_readme_stub_honours_accept_raw(client, admin_h, org):
    # 'gateway' has no README file doc, so this exercises the synthesized-stub branch too
    r = client.get(
        f"/github/repos/{org}/gateway/readme",
        headers={**admin_h, "Accept": "application/vnd.github.raw"},
    )
    assert r.status_code == 200 and r.text.startswith("# gateway")


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


def test_github_raw_accept_still_acl_scoped(gh_client, gh_user_tokens, gh_org):
    c, _ = gh_client
    nonmember_h = {
        "Authorization": f"Bearer {gh_user_tokens['bob@acme.com']}",
        "Accept": "application/vnd.github.raw",
    }
    r = c.get(f"/github/repos/{gh_org}/codebase/contents/config/secret.yaml", headers=nonmember_h)
    assert r.status_code == 404


# --- GET /user/repos (issue #49 D3) ------------------------------------------------------


def test_github_user_repos_lists_the_callers_repos(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    r = c.get("/github/user/repos", headers=gh_admin_h)
    assert r.status_code == 200
    body = r.json()
    org_repos = c.get(
        f"/github/orgs/{gh_org}/repos", headers=gh_admin_h, params={"per_page": 100}
    ).json()
    assert {x["name"] for x in body} == {x["name"] for x in org_repos}
    assert all(x["full_name"] == f"{gh_org}/{x['name']}" for x in body)


def test_github_user_repos_is_acl_scoped(gh_client, gh_user_tokens, gh_admin_h):
    c, _ = gh_client
    # 'vault' holds one group-visible issue owned by 'people', which bob is not in
    bob_h = {"Authorization": f"Bearer {gh_user_tokens['bob@acme.com']}"}
    admin = {x["name"] for x in c.get("/github/user/repos", headers=gh_admin_h).json()}
    bob = {x["name"] for x in c.get("/github/user/repos", headers=bob_h).json()}
    assert "vault" in admin and "vault" not in bob
    assert bob < admin


def test_github_user_repos_paginates(gh_client, gh_admin_h):
    c, _ = gh_client
    r = c.get("/github/user/repos", headers=gh_admin_h, params={"per_page": 1, "page": 1})
    assert len(r.json()) == 1
    assert 'rel="next"' in r.headers.get("Link", "")


# --- GET /repos/{o}/{r}/git/ref/{ref} (issue #49 D4) -------------------------------------


def test_github_git_ref_resolves_a_branch_to_a_commit(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    r = c.get(f"/github/repos/{gh_org}/codebase/git/ref/heads/main", headers=gh_admin_h)
    assert r.status_code == 200
    body = r.json()
    assert body["ref"] == "refs/heads/main"
    assert body["object"]["type"] == "commit"
    branch = c.get(f"/github/repos/{gh_org}/codebase/branches/main", headers=gh_admin_h).json()
    assert body["object"]["sha"] == branch["commit"]["sha"]  # the two must agree


def test_github_git_ref_handles_a_slash_in_the_branch_name(gh_client, gh_admin_h, gh_org):
    """The whole point of the git-refs route over /branches/{branch}: the ref is the trailing
    path, so `release/2026-03` resolves where a single path segment cannot carry it."""
    c, _ = gh_client
    r = c.get(f"/github/repos/{gh_org}/codebase/git/ref/heads/release/2026-03", headers=gh_admin_h)
    assert r.status_code == 200
    assert r.json()["ref"] == "refs/heads/release/2026-03"
    tree_sha = r.json()["object"]["sha"]
    # the sha it hands back is usable as a git/trees ref, which is what a pinning client does next
    assert (
        c.get(
            f"/github/repos/{gh_org}/codebase/git/trees/{tree_sha}", headers=gh_admin_h
        ).status_code
        == 200
    )


def test_github_git_ref_unknown_repo_404s(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    r = c.get(f"/github/repos/{gh_org}/no-such-repo/git/ref/heads/main", headers=gh_admin_h)
    assert r.status_code == 404


# --- owner validation (issue #49 D7) -----------------------------------------------------


def test_github_wrong_owner_404s(gh_client, gh_admin_h, gh_org):
    """Real GitHub 404s on a wrong owner; echoing it back lets a client's owner-handling bug pass
    here and fail in production."""
    c, _ = gh_client
    for path in (
        "",
        "/issues",
        "/pulls",
        "/readme",
        "/contents/README.md",
        "/git/trees/main",
        "/branches/main",
        "/collaborators",
    ):
        r = c.get(f"/github/repos/not-the-owner/codebase{path}", headers=gh_admin_h)
        assert r.status_code == 404, f"wrong owner accepted at {path!r}"
        ok = c.get(f"/github/repos/{gh_org}/codebase{path}", headers=gh_admin_h)
        assert ok.status_code == 200, f"right owner rejected at {path!r}"


def test_github_owner_match_is_case_insensitive(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    r = c.get(f"/github/repos/{gh_org.upper()}/codebase", headers=gh_admin_h)
    assert r.status_code == 200


def test_github_wrong_org_404s(gh_client, gh_admin_h):
    c, _ = gh_client
    assert c.get("/github/orgs/not-the-org", headers=gh_admin_h).status_code == 404
    assert c.get("/github/orgs/not-the-org/repos", headers=gh_admin_h).status_code == 404


# --- a pull's node_id is PullRequest-typed (issue #49 D8) --------------------------------


def test_github_pull_node_id_is_pullrequest_typed(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-pr-1")
    pull = c.get(f"/github/repos/{gh_org}/gateway/pulls/{num}", headers=gh_admin_h).json()
    decoded = base64.b64decode(pull["node_id"] + "==").decode()
    assert "PullRequest" in decoded and "Issue" not in decoded


def test_github_pr_keeps_its_issue_node_id_in_the_issues_stream(gh_client, gh_admin_h, gh_org):
    """Real GitHub models a PR's issue view and pull view as two distinct nodes, so /issues must
    keep the Issue-typed id even for a PR — this is not a bug to fix alongside D8."""
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-pr-1")
    issue = c.get(f"/github/repos/{gh_org}/gateway/issues/{num}", headers=gh_admin_h).json()
    assert "pull_request" in issue  # sanity: this is the PR seen as an issue
    assert "Issue" in base64.b64decode(issue["node_id"] + "==").decode()


# --- pull review comments (issue #49 D5) ------------------------------------------------


def test_github_pull_review_comments_route_exists(gh_client, gh_admin_h, gh_org):
    """A pull's review-comment collection is a real resource, so a PR with none must answer `[]`
    rather than 404 — a 404 aborts any client that renders a pull from its four sub-resources."""
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-pr-1")
    r = c.get(f"/github/repos/{gh_org}/gateway/pulls/{num}/comments", headers=gh_admin_h)
    assert r.status_code == 200
    assert r.json() == []


def test_github_pull_review_comments_404_for_a_non_pull(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-issue-1")  # an issue, not a PR
    r = c.get(f"/github/repos/{gh_org}/gateway/pulls/{num}/comments", headers=gh_admin_h)
    assert r.status_code == 404


# --- pull changeset: /pulls/{n}/files and the diff media types (issue #49 D6, D2) --------


def _hunk_is_wellformed(patch: str) -> bool:
    """Every `@@ -a,b +c,d @@` header's counts must match the lines that follow it."""
    import re as _re

    hunks = _re.findall(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*$", patch, _re.M)
    if not hunks:
        return False
    bodies = _re.split(r"^@@ .*$\n?", patch, flags=_re.M)[1:]
    if len(bodies) != len(hunks):
        return False
    for (_, old_n, _, new_n), body in zip(hunks, bodies):
        old_n = int(old_n) if old_n is not None else 1
        new_n = int(new_n) if new_n is not None else 1
        lines = [ln for ln in body.split("\n") if ln != ""]
        old_seen = sum(1 for ln in lines if ln[0] in " -")
        new_seen = sum(1 for ln in lines if ln[0] in " +")
        if old_seen != old_n or new_seen != new_n:
            return False
    return True


@pytest.fixture(scope="module")
def diff_pr(gh_client, gh_admin_h, gh_org):
    """(client, headers, org, pull_number) for the 'diffable' repo's one PR."""
    c, _ = gh_client
    from backlot import synth

    return c, gh_admin_h, gh_org, synth.github_number("gh-diff-pr")


def test_github_pull_files_lists_the_changed_files(diff_pr):
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


def test_github_pull_files_patches_are_wellformed_hunks(diff_pr):
    c, h, org, num = diff_pr
    files = c.get(f"/github/repos/{org}/diffable/pulls/{num}/files", headers=h).json()
    patched = [f for f in files if f.get("patch")]
    assert patched, "at least one changed file must carry a patch"
    for f in patched:
        assert _hunk_is_wellformed(f["patch"]), (
            f"malformed patch for {f['filename']}:\n{f['patch']}"
        )


def test_github_pull_object_counts_agree_with_its_files(diff_pr):
    """The pull object already advertised additions/deletions/changed_files; they have to be the
    sum over what /files reports or the two answers contradict each other."""
    c, h, org, num = diff_pr
    pull = c.get(f"/github/repos/{org}/diffable/pulls/{num}", headers=h).json()
    files = c.get(f"/github/repos/{org}/diffable/pulls/{num}/files", headers=h).json()
    assert pull["changed_files"] == len(files)
    assert pull["additions"] == sum(f["additions"] for f in files)
    assert pull["deletions"] == sum(f["deletions"] for f in files)


def test_github_pull_files_are_deterministic(diff_pr):
    c, h, org, num = diff_pr
    first = c.get(f"/github/repos/{org}/diffable/pulls/{num}/files", headers=h).json()
    second = c.get(f"/github/repos/{org}/diffable/pulls/{num}/files", headers=h).json()
    assert first == second


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
    row = store.get_document(conn, "github", "pr-nofiles")
    assert _pr_files(conn, "org", "bare", row, "http://m/github") == []
    pr = _pr_obj(conn, "org", "bare", row, "http://m/github")
    assert (pr["changed_files"], pr["additions"], pr["deletions"]) == (0, 0, 0)


def test_github_pull_files_is_acl_scoped(gh_client, gh_user_tokens, gh_org):
    """A file the caller can't see must not appear in a changeset either."""
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-diff-pr")
    nonmember_h = {"Authorization": f"Bearer {gh_user_tokens['bob@acme.com']}"}
    files = c.get(f"/github/repos/{gh_org}/diffable/pulls/{num}/files", headers=nonmember_h).json()
    assert all(f["filename"] != "config/secret.yaml" for f in files)


def test_github_pull_accept_diff_returns_a_unified_diff(diff_pr):
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


def test_github_pull_diff_reverse_applies_with_real_git(diff_pr, tmp_path):
    """The claim a synthesized diff has to earn: real `git apply` accepts it.

    The snapshot the mock serves IS the pull's head, so `git apply --reverse` must be able to walk
    it back to the base. Nothing weaker proves it — a hunk header off by one line, or context that
    doesn't match the file, still looks like a diff and still passes a shape assertion. Reverse
    rather than forward because the base is what the mock doesn't have on disk.
    """
    import shutil
    import subprocess

    if shutil.which("git") is None:  # pragma: no cover - git is present everywhere this runs
        pytest.skip("git not available")
    c, h, org, num = diff_pr
    diff = c.get(
        f"/github/repos/{org}/diffable/pulls/{num}",
        headers={**h, "Accept": "application/vnd.github.diff"},
    ).text
    assert diff, "the changeset must not be empty for this repo"

    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
    tree = c.get(
        f"/github/repos/{org}/diffable/git/trees/main", headers=h, params={"recursive": "1"}
    ).json()["tree"]
    for e in tree:
        if e["type"] != "blob":
            continue
        raw = c.get(
            f"/github/repos/{org}/diffable/contents/{e['path']}",
            headers={**h, "Accept": "application/vnd.github.raw"},
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


def test_github_pull_default_accept_is_still_json(diff_pr):
    c, h, org, num = diff_pr
    body = c.get(f"/github/repos/{org}/diffable/pulls/{num}", headers=h).json()
    assert body["number"] == num and body["title"] == "Tighten the run() argv handling"


def test_github_issue_ignores_the_diff_media_type(gh_client, gh_admin_h, gh_org):
    """Only a pull has a diff; an issue asked for one gets its JSON, as real GitHub does."""
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-issue-1")
    body = c.get(
        f"/github/repos/{gh_org}/gateway/issues/{num}",
        headers={**gh_admin_h, "Accept": "application/vnd.github.diff"},
    ).json()
    assert body["number"] == num


# --- a corpus-declared changeset: `changed_paths` ---------------------------------------


@pytest.fixture(scope="module")
def declared_pr(gh_client, gh_admin_h, gh_org):
    """(client, headers, org, number) for 'diffable''s pull that declares its own changeset."""
    c, _ = gh_client
    from backlot import synth

    return c, gh_admin_h, gh_org, synth.github_number("gh-diff-pr-declared")


def test_github_pull_files_are_the_declared_paths(declared_pr):
    """With `changed_paths` the changeset is what the corpus says it is — same order, all of them,
    not the deterministic pick and not capped at three."""
    c, h, org, num = declared_pr
    files = c.get(f"/github/repos/{org}/diffable/pulls/{num}/files", headers=h).json()
    assert [f["filename"] for f in files] == [
        "pkg/core.py",
        "app.py",
        "pkg/conf.toml",
        "README.md",
    ]


def test_github_declared_changeset_still_agrees_with_the_pull_object(declared_pr):
    c, h, org, num = declared_pr
    pull = c.get(f"/github/repos/{org}/diffable/pulls/{num}", headers=h).json()
    files = c.get(f"/github/repos/{org}/diffable/pulls/{num}/files", headers=h).json()
    assert pull["changed_files"] == len(files) == 4
    assert pull["additions"] == sum(f["additions"] for f in files)
    assert pull["deletions"] == sum(f["deletions"] for f in files)


def test_github_declared_changeset_diff_reverse_applies_with_real_git(declared_pr, tmp_path):
    """Same proof as for the synthesized changeset: declaring the files changes WHICH files are in
    the diff, not whether the hunks are real."""
    import shutil
    import subprocess

    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git not available")
    c, h, org, num = declared_pr
    diff = c.get(
        f"/github/repos/{org}/diffable/pulls/{num}",
        headers={**h, "Accept": "application/vnd.github.diff"},
    ).text
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
    tree = c.get(
        f"/github/repos/{org}/diffable/git/trees/main", headers=h, params={"recursive": "1"}
    ).json()["tree"]
    for e in tree:
        if e["type"] != "blob":
            continue
        raw = c.get(
            f"/github/repos/{org}/diffable/contents/{e['path']}",
            headers={**h, "Accept": "application/vnd.github.raw"},
        ).text
        dest = wt / e["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(raw)
    (wt / "pr.diff").write_text(diff)
    r = subprocess.run(
        ["git", "apply", "--reverse", "--check", "pr.diff"], cwd=wt, capture_output=True, text=True
    )
    assert r.returncode == 0, f"git rejected the declared diff:\n{r.stderr}\n---\n{diff}"


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


def test_github_declared_path_that_does_not_exist_is_dropped(tmp_path):
    """A path naming no file in the repo cannot be diffed, so it is skipped rather than emitted as a
    file with no content. Indistinguishable from an ACL-hidden one at this layer, which is why both
    behave the same way."""
    from backlot.routers.github import _pr_files

    s = tiny_corpus(
        tmp_path,
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
                "changed_paths": ["real.py", "typo.py"],
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    row = store.get_document(conn, "github", "p1")
    assert [f["filename"] for f in _pr_files(conn, "org", "r", row, "http://m/github")] == [
        "real.py"
    ]


def test_github_declared_paths_are_deduplicated(tmp_path):
    """A repeated path would put the same file in the diff twice, which `git apply` refuses — so a
    corpus typo would produce a diff no client can use."""
    from backlot.routers.github import _pr_files

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "github",
                "doc_id": "f1",
                "repo": "r",
                "subtype": "file",
                "path": "a.py",
                "title": "a.py",
                "content": "x\ny\nz\n",
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
                "changed_paths": ["a.py", "a.py"],
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    row = store.get_document(conn, "github", "p1")
    assert [f["filename"] for f in _pr_files(conn, "org", "r", row, "http://m/github")] == ["a.py"]


# --- /pulls/{n}/files pagination --------------------------------------------------------


def test_github_pull_files_paginates(declared_pr):
    """Real GitHub paginates the changed-file list; a client's paging loop over it is only
    exercisable if the mock emits the Link header."""
    c, h, org, num = declared_pr
    url = f"/github/repos/{org}/diffable/pulls/{num}/files"
    first = c.get(url, headers=h, params={"per_page": 2, "page": 1})
    assert [f["filename"] for f in first.json()] == ["pkg/core.py", "app.py"]
    assert 'rel="next"' in first.headers.get("Link", "")
    second = c.get(url, headers=h, params={"per_page": 2, "page": 2})
    assert [f["filename"] for f in second.json()] == ["pkg/conf.toml", "README.md"]
    assert 'rel="next"' not in second.headers.get("Link", "")


def test_github_pull_files_pagination_covers_every_file_once(declared_pr):
    c, h, org, num = declared_pr
    url = f"/github/repos/{org}/diffable/pulls/{num}/files"
    unpaged = c.get(url, headers=h).json()
    walked, page = [], 1
    while True:
        r = c.get(url, headers=h, params={"per_page": 3, "page": page})
        walked += r.json()
        if 'rel="next"' not in r.headers.get("Link", ""):
            break
        page += 1
    assert walked == unpaged


# --- line-anchored review comments ------------------------------------------------------


def test_github_pull_review_comments_are_served(declared_pr):
    c, h, org, num = declared_pr
    body = c.get(f"/github/repos/{org}/diffable/pulls/{num}/comments", headers=h).json()
    assert [(x["path"], x["line"]) for x in body] == [("pkg/core.py", 4), ("app.py", None)]
    first = body[0]
    assert first["body"] == "this line should be a constant"
    assert first["side"] == "RIGHT" and first["commit_id"]
    assert first["user"]["login"] == "ava"
    assert first["pull_request_url"].endswith(f"/pulls/{num}")
    assert first["html_url"].endswith(f"#discussion_r{first['id']}")


def test_github_review_comment_position_points_at_the_commented_row(declared_pr):
    """`position` indexes INTO the diff hunk, so the row it selects must be the commented line."""
    c, h, org, num = declared_pr
    comment = next(
        x
        for x in c.get(f"/github/repos/{org}/diffable/pulls/{num}/comments", headers=h).json()
        if x["path"] == "pkg/core.py"
    )
    rows = comment["diff_hunk"].split("\n")
    pos = comment["position"]
    assert pos is not None
    assert rows[pos].lstrip(" +") == f"line_{comment['line']} = {comment['line']}"


def test_hunk_position_is_not_the_file_line():
    """`position` and `line` are different numbers on real GitHub — the row's offset within the diff
    hunk versus the line in the file — and a client resolving a comment against a diff uses the
    former. They coincide only for a hunk that starts at line 1 with nothing removed above, so this
    pins the distinction on a hunk where they cannot."""
    from backlot.routers.github import _hunk_position

    hunk = "@@ -8,4 +10,5 @@\n ctx_a\n-gone\n+added\n ctx_b\n ctx_c\n"
    #        new-side lines:      10       (none)  11      12      13
    assert _hunk_position(hunk, 10) == 1
    assert _hunk_position(hunk, 11) == 3  # offset 2 is the removed row, which has no new-side line
    assert _hunk_position(hunk, 13) == 5
    assert _hunk_position(hunk, 99) is None  # outside the hunk
    assert _hunk_position(hunk, None) is None  # a file-level comment
    assert _hunk_position("not a hunk", 10) is None


def test_hunk_position_ignores_the_no_newline_marker():
    """git's `\\ No newline at end of file` is a hunk row but NOT a line of the file. Counting it as
    one lets a line past the end of the file resolve to the marker's own offset."""
    from backlot.routers.github import _hunk_around, _hunk_position

    hunk = _hunk_around({"content": "alpha\nbeta\ngamma"}, 3)  # no trailing newline
    assert "\\ No newline at end of file" in hunk
    assert _hunk_position(hunk, 3) == 3  # the real last line
    assert _hunk_position(hunk, 4) is None  # past the end — must not land on the marker


def test_github_file_level_review_comment_has_no_position(declared_pr):
    c, h, org, num = declared_pr
    comment = next(
        x
        for x in c.get(f"/github/repos/{org}/diffable/pulls/{num}/comments", headers=h).json()
        if x["line"] is None
    )
    assert comment["position"] is None and comment["subject_type"] == "file"


def test_github_review_comment_diff_hunk_is_derived_when_absent(declared_pr):
    """The corpus need only say where the comment is anchored; the hunk around it comes from the
    file's own snapshot, the same principle as the changeset."""
    c, h, org, num = declared_pr
    body = c.get(f"/github/repos/{org}/diffable/pulls/{num}/comments", headers=h).json()
    derived = next(x for x in body if x["path"] == "pkg/core.py")
    assert derived["diff_hunk"].startswith("@@ ")
    assert "line_4 = 4" in derived["diff_hunk"]  # real content from the file, around line 4
    explicit = next(x for x in body if x["path"] == "app.py")
    assert explicit["diff_hunk"] == "@@ -1,2 +1,3 @@\n import sys\n"  # corpus wins


def test_github_review_comments_do_not_leak_into_the_conversation(declared_pr):
    """The two resources real GitHub keeps apart must stay apart: serving review comments from
    /issues/{n}/comments would duplicate them under a resource that means something else."""
    c, h, org, num = declared_pr
    convo = c.get(f"/github/repos/{org}/diffable/issues/{num}/comments", headers=h).json()
    assert [x["body"] for x in convo] == ["conversation, not anchored"]
    assert all("path" not in x for x in convo)


def test_github_comment_counts_split_the_two_kinds(declared_pr):
    """`comments` counts the conversation and `review_comments` the anchored ones, as real GitHub
    reports them — one number covering both would contradict whichever list you fetched."""
    c, h, org, num = declared_pr
    pull = c.get(f"/github/repos/{org}/diffable/pulls/{num}", headers=h).json()
    assert pull["comments"] == 1
    assert pull["review_comments"] == 2
    issue = c.get(f"/github/repos/{org}/diffable/issues/{num}", headers=h).json()
    assert issue["comments"] == 1  # the issue view counts the conversation only


def test_github_review_comment_count_matches_the_list_it_describes(gh_client, gh_admin_h, gh_org):
    """`review_comments` has to be the number the list endpoint actually returns.

    The list drops a comment whose `path` resolves to no file the caller can read; counting every
    anchored row instead meant the two answers contradicted each other, a client paging until it had
    `review_comments` items never terminated, and the count LEAKED that a hidden file carries a
    comment."""
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-diff-pr-unresolvable")
    body = c.get(f"/github/repos/{gh_org}/diffable/pulls/{num}/comments", headers=gh_admin_h).json()
    pull = c.get(f"/github/repos/{gh_org}/diffable/pulls/{num}", headers=gh_admin_h).json()
    # 'gone/x.py' is in no tree, so even the admin sees one fewer than the corpus declared
    assert [x["path"] for x in body] == ["app.py", "secret/keys.txt"]
    assert pull["review_comments"] == len(body) == 2


def test_github_review_comment_count_is_acl_scoped(gh_client, gh_user_tokens, gh_org):
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-diff-pr-unresolvable")
    bob = {"Authorization": f"Bearer {gh_user_tokens['bob@acme.com']}"}  # not in 'people'
    body = c.get(f"/github/repos/{gh_org}/diffable/pulls/{num}/comments", headers=bob).json()
    pull = c.get(f"/github/repos/{gh_org}/diffable/pulls/{num}", headers=bob).json()
    assert [x["path"] for x in body] == ["app.py"]
    assert pull["review_comments"] == len(body) == 1


def test_github_pull_with_no_review_comments_still_answers_empty(diff_pr):
    c, h, org, num = diff_pr
    r = c.get(f"/github/repos/{org}/diffable/pulls/{num}/comments", headers=h)
    assert r.status_code == 200 and r.json() == []


def test_github_emitted_urls_are_fetchable(gh_client, gh_admin_h, gh_org):
    """Every absolute URL the mock puts in a response has to be one the mock accepts back — SDK
    clients complete objects lazily by following them (see `_api_base`). Now that a wrong owner
    404s, an emitted URL built from a different notion of the org would be a dead link, and the
    builders do not all read the org from the same place.

    NOT covered, deliberately: a comment's own `url`/`_links.self`
    (`…/pulls/comments/{id}`, `…/issues/comments/{id}`). No route serves a comment by id — the id is
    a hash of the stored comment id, so resolving one back would need a reverse index the app does
    not build. The field is kept because it is part of the real object's shape, but following it
    404s. Pre-existing for issue comments; review comments inherit it."""
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-diff-pr-declared")
    seen = []
    search = c.get("/github/search/issues", headers=gh_admin_h, params={"q": "argv"}).json()
    assert search["items"], "need a search hit to check the URLs it emits"
    seen += [search["items"][0][k] for k in ("url", "repository_url", "comments_url")]
    pull = c.get(f"/github/repos/{gh_org}/diffable/pulls/{num}", headers=gh_admin_h).json()
    seen += [pull["url"], pull["issue_url"], pull["repository_url"]]
    files = c.get(f"/github/repos/{gh_org}/diffable/pulls/{num}/files", headers=gh_admin_h).json()
    seen.append(files[0]["contents_url"])
    review = c.get(f"/github/repos/{gh_org}/diffable/pulls/{num}/comments", headers=gh_admin_h)
    seen.append(review.json()[0]["pull_request_url"])
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


# --- git trees: the real truncation cap (issue #49 D9) ----------------------------------


def test_github_tree_truncates_at_the_entry_cap(gh_client, gh_admin_h, gh_org, monkeypatch):
    """Real GitHub caps a recursive tree and sets `truncated: true`; a mock that can never set it
    leaves a client's truncation-handling path untested."""
    from backlot.routers import github as gh

    c, _ = gh_client
    monkeypatch.setattr(gh, "TREE_MAX_ENTRIES", 2)
    body = c.get(
        f"/github/repos/{gh_org}/codebase/git/trees/main",
        headers=gh_admin_h,
        params={"recursive": "1"},
    ).json()
    assert body["truncated"] is True
    assert len(body["tree"]) == 2


def test_github_tree_truncates_at_the_byte_cap(gh_client, gh_admin_h, gh_org, monkeypatch):
    """The other half of the real cap (7 MB), and the only branch that trims entry by entry."""
    from backlot.routers import github as gh

    c, _ = gh_client
    full = c.get(
        f"/github/repos/{gh_org}/codebase/git/trees/main",
        headers=gh_admin_h,
        params={"recursive": "1"},
    ).json()["tree"]
    monkeypatch.setattr(gh, "TREE_MAX_BYTES", len(json.dumps(full)) // 2)
    body = c.get(
        f"/github/repos/{gh_org}/codebase/git/trees/main",
        headers=gh_admin_h,
        params={"recursive": "1"},
    ).json()
    assert body["truncated"] is True
    assert 0 < len(body["tree"]) < len(full)
    assert body["tree"] == full[: len(body["tree"])]  # a prefix, not a resampling


def test_github_tree_not_truncated_under_the_cap(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    body = c.get(
        f"/github/repos/{gh_org}/codebase/git/trees/main",
        headers=gh_admin_h,
        params={"recursive": "1"},
    ).json()
    assert body["truncated"] is False


# --- OpenAPI enrichment: github response fidelity ------------------------------------------


def test_github_list_issues_documents_state_param(client):
    op = client.get("/openapi.json").json()["paths"]["/github/repos/{owner}/{repo}/issues"]["get"]
    params = {p["name"]: p for p in op.get("parameters", [])}
    assert "state" in params and {"page", "per_page"} <= set(params)
    assert params["state"]["schema"].get("default") == "open"


def test_github_search_still_filters_by_q(client, admin_h):
    body = client.get("/github/search/issues", params={"q": ""}, headers=admin_h).json()
    assert "items" in body and "total_count" in body


def test_github_responses_unchanged_by_enrichment(client, admin_h):
    # Fidelity guard: the rich issue field set must survive query-param + response_model enrichment.
    body = client.get("/github/search/issues", params={"q": ""}, headers=admin_h).json()
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


def test_github_issue_search_has_typed_response_schema(client):
    op = client.get("/openapi.json").json()["paths"]["/github/search/issues"]["get"]
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
    iss = _issue_obj(
        conn, "org", "gw", store.get_document(conn, "github", "gh1"), "http://m/github"
    )
    # numeric id present and distinct from number (real connectors dedupe on id)
    assert iss["id"] != iss["number"] and isinstance(iss["id"], int)
    assert iss["node_id"]
    # assignee (singular) present alongside assignees[]
    assert iss["assignee"]["login"] == "a" and iss["assignees"][0]["login"] == "a"
    assert iss["closed_at"].startswith("2026-02-01") and iss["closed_by"]["login"] == "b"
    assert iss["milestone"]["title"] == "v2"
    assert iss["state_reason"] == "completed" and iss["author_association"] == "MEMBER"
    # reactions is the full 8-key rollup with total_count
    assert iss["reactions"]["total_count"] == 4 and iss["reactions"]["+1"] == 3
    assert iss["reactions"]["eyes"] == 0

    pr = _pr_obj(conn, "org", "gw", store.get_document(conn, "github", "pr1"), "http://m/github")
    assert pr["merged"] is True and pr["merged_by"]["login"] == "b"
    assert pr["requested_reviewers"][0]["login"] == "c"


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
    c = store.doc_comments(conn, "github", "gh2")[0]
    obj = _gh_comment("org", "gw", 1, c, "http://m/github")
    assert obj["reactions"]["heart"] == 2 and obj["node_id"] and obj["url"]
    assert obj["reactions"]["total_count"] == 2
