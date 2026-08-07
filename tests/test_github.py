"""GitHub's REST surface: issues, PRs, comments, and the git tree/contents/blobs
codebase serving.

One file per router, so a provider's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import base64
import hashlib
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


@pytest.fixture(scope="module")
def gh_client(tmp_path_factory):
    from tests.conftest import SAMPLE

    settings = build_corpus(tmp_path_factory.mktemp("gh_sample"), SAMPLE + _GH_FILE_DOCS)
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
