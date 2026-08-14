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


def _gh_row(conn, title: str):
    """The github row a fixture record with this title became.

    A github number is assigned against the whole corpus (#51), so it cannot be computed from the
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


def test_github_a_files_number_never_shadows_an_issue(gh_client, gh_admin_h, gh_org):
    """A file's number must never resolve as an issue or a pull. The hazard is real and pinned by
    the fixture: `gh-file-collide-88814` seeds to exactly `gh-issue-1`'s number.

    A file row DOES carry a number now (#51). `github_items` holds two resources with different
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


# --- media-type negotiation: Accept: application/vnd.github.raw (issue #49 D1) ----------

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


# --- GET /user/repos (issue #49 D3) ------------------------------------------------------


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
        "/branches/main",
        "/commits/deadbeef",
        "/contents",
        "/collaborators",
        "/teams",
    ):
        assert c.get(f"/github/repos/{gh_org}/vault{path}", headers=bob_h).status_code == 404, path
        assert c.get(f"/github/repos/{gh_org}/vault{path}", headers=gh_admin_h).status_code == 200

    page = c.get("/github/user/repos", headers=gh_admin_h, params={"per_page": 1, "page": 1})
    assert len(page.json()) == 1 and 'rel="next"' in page.headers.get("Link", "")


# --- GET /repos/{o}/{r}/git/ref/{ref} (issue #49 D4) -------------------------------------


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


# --- owner validation (issue #49 D7) -----------------------------------------------------


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
        "/branches/main",
        "/collaborators",
    ):
        r = c.get(f"/github/repos/not-the-owner/codebase{path}", headers=gh_admin_h)
        assert r.status_code == 404, f"wrong owner accepted at {path!r}"
        ok = c.get(f"/github/repos/{gh_org}/codebase{path}", headers=gh_admin_h)
        assert ok.status_code == 200, f"right owner rejected at {path!r}"

    assert c.get(f"/github/repos/{gh_org.upper()}/codebase", headers=gh_admin_h).status_code == 200
    assert c.get("/github/orgs/not-the-org", headers=gh_admin_h).status_code == 404
    assert c.get("/github/orgs/not-the-org/repos", headers=gh_admin_h).status_code == 404


# --- a pull's node_id is PullRequest-typed (issue #49 D8) --------------------------------


def test_github_pull_and_issue_views_are_different_nodes(gh_client, gh_admin_h, gh_org):
    """Real GitHub models a PR's issue view and its pull view as two distinct nodes, so the pull
    is PullRequest-typed while /issues keeps the Issue-typed id for the same PR."""
    c, _ = gh_client
    from backlot import synth

    num = synth.github_number("gh-pr-1")
    pull = c.get(f"/github/repos/{gh_org}/gateway/pulls/{num}", headers=gh_admin_h).json()
    decoded = base64.b64decode(pull["node_id"] + "==").decode()
    assert "PullRequest" in decoded and "Issue" not in decoded

    issue = c.get(f"/github/repos/{gh_org}/gateway/issues/{num}", headers=gh_admin_h).json()
    assert "pull_request" in issue  # sanity: this is the PR seen as an issue
    assert "Issue" in base64.b64decode(issue["node_id"] + "==").decode()


# --- pull changeset: /pulls/{n}/files and the diff media types (issue #49 D6, D2) --------


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
    """The real API's shape, agreeing with the pull object's own counts — those used to contradict
    it — and stable across calls, since the whole changeset is derived from a hash of the doc_id."""
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

    The snapshot the mock serves IS the pull's head, so `git apply --reverse` must be able to walk
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
    exercisable if the mock emits the Link header — and a synthesized changeset caps at three files,
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


# --- line-anchored review comments (issue #49 D5) ---------------------------------------


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
    """A pull with none answers `[]` — the collection is a real resource, and the 404 it used to
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

    Re-importing the same corpus USED to keep the ids it had already assigned, so a url a client
    stored stayed valid. It cannot any more, and the second half of this test pins what replaced
    it: an append into a source whose keys are probed is REFUSED unless the record states its own
    identity (#51, Step 5). Nothing is left to recognise a row by, so the alternative was to add it
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
    """Every absolute URL the mock puts in a response has to be one the mock accepts back — SDK
    clients complete objects lazily by following them (see `_api_base`). Now that a wrong owner
    404s, an emitted URL built from a different notion of the org would be a dead link, and the
    builders do not all read the org from the same place.

    Includes a comment's own `url`, whose id is a hash of the stored comment id — it resolves back
    through the same startup reverse index that already serves gmail/jira/notion ids, and its route
    has to be registered ahead of `…/pulls/{number}/comments` or the literal `comments` is parsed as
    a pull number instead."""
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
    seen += [review.json()[0]["pull_request_url"], review.json()[0]["url"]]
    convo = c.get(f"/github/repos/{gh_org}/diffable/issues/{num}/comments", headers=gh_admin_h)
    seen.append(convo.json()[0]["url"])
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


def test_github_tree_truncates_at_the_real_caps(gh_client, gh_admin_h, gh_org, monkeypatch):
    """Real GitHub caps a recursive tree (100k entries / 7 MB) and sets `truncated: true`; a mock
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


def test_github_issue_number_asserts_rather_than_re_hash_a_null_number():
    """`_issue_number`'s `or` fallback used to silently re-hash a NULL number back to a
    plain `synth.github_number` -- exactly the shape the hubspot bug shipped as: a PROBED row
    (one whose actual served number came from a walk, not a pure hash) would then advertise a
    number nobody stored, unreachable at its own url (#51, task 11). An assertion is strictly
    better: every non-file row gets a number at import (`resolve_github_numbers` raises
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
    # assignee (singular) present alongside assignees[]
    assert iss["assignee"]["login"] == "a" and iss["assignees"][0]["login"] == "a"
    assert iss["closed_at"].startswith("2026-02-01") and iss["closed_by"]["login"] == "b"
    assert iss["milestone"]["title"] == "v2"
    assert iss["state_reason"] == "completed" and iss["author_association"] == "MEMBER"
    # reactions is the full 8-key rollup with total_count
    assert iss["reactions"]["total_count"] == 4 and iss["reactions"]["+1"] == 3
    assert iss["reactions"]["eyes"] == 0

    pr = _pr_obj(conn, "org", "gw", _gh_row(conn, "PR"), "http://m/github")
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
    # store.github_comments, not the shared doc_comments: only the github reader carries the
    # `id` the builder reports as the comment's `id`
    gh2 = _gh_row(conn, "T")
    c = store.github_comments(conn, gh2["repo"], gh2["number"])[0]
    obj = _gh_comment("org", "gw", 1, c, "http://m/github")
    assert obj["reactions"]["heart"] == 2 and obj["node_id"] and obj["url"]
    assert obj["reactions"]["total_count"] == 2
    assert obj["id"] == c["id"]
