"""Mock GitHub REST API (read-only). Client base_url: ``http://<host>/github``.

Each dataset ``github`` document is modelled as an issue in its repo (= container).
Responses are bare JSON arrays with an RFC5988 ``Link`` header for pagination, as the
real API does. Auth: ``Authorization: Bearer <token>`` (or ``token <token>``).
"""

from __future__ import annotations

import base64
import hashlib
import re

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backlot import auth, store, synth
from backlot.acl import Caller
from backlot.config import get_settings
from backlot.pagination import clamp_page, github_link_header

router = APIRouter(prefix="/github", tags=["github"])


class _Loose(BaseModel):
    """Documents the fields the bridge/agent rely on while ``extra='allow'`` lets the
    builders' full (real-API-shaped) field set pass through unfiltered — the OpenAPI schema
    gains structure with zero fidelity loss."""

    model_config = ConfigDict(extra="allow")


class GitHubIssue(_Loose):
    id: int
    number: int
    title: str | None = None
    body: str | None = None
    state: str
    html_url: str
    url: str


class GitHubIssueSearch(_Loose):
    total_count: int
    incomplete_results: bool
    items: list[GitHubIssue]


def _require(request: Request) -> Caller:
    return auth.require_bearer(request, "Bad credentials")


def _base_url(request: Request) -> str:
    host = request.headers.get("host", "localhost")
    return f"{request.url.scheme}://{host}{request.url.path}"


def _api_base(request: Request) -> str:
    """The mock's GitHub API root (…/github), used for resource `url` fields so SDK
    clients (e.g. PyGithub) that lazily complete objects fetch back from the mock."""
    host = request.headers.get("host", "localhost")
    return f"{request.url.scheme}://{host}/github"


def _paged(
    request: Request, rows_total: int, extra: dict, body: list, page: int, per_page: int
) -> Response:
    link = github_link_header(_base_url(request), extra, page, per_page, rows_total)
    headers = {"Link": link} if link else {}
    return JSONResponse(body, headers=headers)


_GH_OP = re.compile(r'(\w+):("[^"]*"|\S+)')
# search qualifiers we honor; everything else stays as free text
_GH_QUAL_KEYS = {"repo", "is", "state", "type", "label", "author", "in", "org", "user"}


def _parse_issue_q(q: str) -> tuple[str, dict]:
    """Split a GitHub issues-search `q` into (free_text, qualifiers). Honors
    repo:/is:/state:/type:/label:/author: — the rest is free text matched full-text."""
    quals: dict[str, list[str]] = {}

    def _take(m):
        key = m.group(1).lower()
        if key in _GH_QUAL_KEYS:
            quals.setdefault(key, []).append(m.group(2).strip('"'))
            return " "
        return m.group(0)

    free = re.sub(r"\s+", " ", _GH_OP.sub(_take, q)).strip()
    return free, quals


def _issue_qual_match(row, quals: dict) -> bool:
    for v in quals.get("is", []) + quals.get("type", []):
        v = v.lower()
        if v == "issue" and row["kind"] == "pull_request":
            return False
        if v == "pr" and row["kind"] != "pull_request":
            return False
        if v in ("open", "closed") and (row["state"] or "open") != v:
            return False
        if v == "merged" and not row["merged_at"]:
            return False
    for v in quals.get("state", []):
        if (row["state"] or "open") != v.lower():
            return False
    for v in quals.get("label", []):
        if v.lower() not in [x.lower() for x in store.jcol(row, "labels")]:
            return False
    for v in quals.get("author", []):
        login = synth.github_login(row["author_email"]).lower()
        if v.lower() != login and v.lower() not in (row["author_email"] or "").lower():
            return False
    return True


@router.get("/search/issues", response_model=GitHubIssueSearch)
async def search_issues(
    request: Request,
    q: str = Query("", description="Issues/PRs search query"),
    page: int | None = Query(None, ge=1),
    per_page: int | None = Query(None, ge=1),
):
    """Issues-and-PRs search (GitHub `GET /search/issues`): free text over title+body (FTS)
    plus repo:/is:/state:/type:/label:/author: qualifiers, ACL-scoped to the caller."""
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    free, quals = _parse_issue_q(q)
    container = None  # a repo: qualifier narrows to one repo
    for v in quals.get("repo", []):
        name = v.split("/")[-1]
        if store.get_container(conn, "github", name) is not None:
            container = name
    if free:
        cand = store.search_documents(conn, free, "github", ids, limit=10_000, container=container)
    else:
        cand = store.list_documents(conn, "github", container, ids, limit=10_000)
    matched = [r for r in cand if r["kind"] != "file" and _issue_qual_match(r, quals)]
    page, per_page = clamp_page(
        page, per_page, get_settings().default_page_size, get_settings().max_page_size
    )
    start = (page - 1) * per_page
    ab = _api_base(request)
    owner = get_settings().org_name
    items = [_issue_obj(conn, owner, r["repo"], r, ab) for r in matched[start : start + per_page]]
    return {"total_count": len(matched), "incomplete_results": False, "items": items}


@router.get("/orgs/{org}")
async def get_org(org: str, request: Request):
    _require(request)
    return {
        "login": org,
        "id": synth.github_user_id(org),
        "type": "Organization",
        "url": f"{_base_url(request)}",
        "repos_url": f"{_base_url(request)}/repos",
        "html_url": f"https://github.com/{org}",
    }


@router.get("/orgs/{org}/repos")
async def list_repos(
    org: str,
    request: Request,
    page: int | None = Query(None, ge=1),
    per_page: int | None = Query(None, ge=1),
):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    repos = [r["name"] for r in store.list_containers(conn, "github")]
    if ids is not None:
        repos = [n for n in repos if store.count_documents(conn, "github", n, ids) > 0]
    page, per_page = clamp_page(
        page, per_page, get_settings().default_page_size, get_settings().max_page_size
    )
    start = (page - 1) * per_page
    ab = _api_base(request)
    body = [_repo_obj(conn, org, n, ab) for n in repos[start : start + per_page]]
    return _paged(request, len(repos), {}, body, page, per_page)


@router.get("/repos/{owner}/{repo}")
async def get_repo(owner: str, repo: str, request: Request):
    conn = auth.conn(request)
    _require(request)
    if store.get_container(conn, "github", repo) is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return _repo_obj(conn, owner, repo, _api_base(request))


@router.get("/repos/{owner}/{repo}/issues", response_model=list[GitHubIssue])
async def list_issues(
    owner: str,
    repo: str,
    request: Request,
    state: str = Query("open"),
    page: int | None = Query(None, ge=1),
    per_page: int | None = Query(None, ge=1),
):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    if store.get_container(conn, "github", repo) is None:
        raise HTTPException(status_code=404, detail="Not Found")
    state_filter = state if state != "all" else None
    # kind='file' docs (source code, not issues/PRs) never appear here — fetch generously and
    # filter+paginate in Python, mirroring list_pulls below.
    all_rows = [
        r
        for r in store.list_documents(conn, "github", repo, ids, limit=10_000, state=state_filter)
        if r["kind"] != "file"
    ]
    page, per_page = clamp_page(
        page, per_page, get_settings().default_page_size, get_settings().max_page_size
    )
    start = (page - 1) * per_page
    rows = all_rows[start : start + per_page]
    # like the real API, /issues returns issues AND PRs (PRs carry a pull_request marker)
    ab = _api_base(request)
    body = [_issue_obj(conn, owner, repo, r, ab) for r in rows]
    return _paged(request, len(all_rows), {"state": state}, body, page, per_page)


@router.get("/repos/{owner}/{repo}/issues/{number}", response_model=GitHubIssue)
async def get_issue(owner: str, repo: str, number: int, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _resolve(request, conn, repo, number, ids)
    if row is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return _issue_obj(conn, owner, repo, row, _api_base(request))


@router.get("/repos/{owner}/{repo}/issues/{number}/comments")
async def issue_comments(owner: str, repo: str, number: int, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _resolve(request, conn, repo, number, ids)
    if row is None:
        raise HTTPException(status_code=404, detail="Not Found")
    ab = _api_base(request)
    return [
        _gh_comment(owner, repo, number, c, ab)
        for c in store.doc_comments(conn, "github", row["doc_id"])
    ]


@router.get("/repos/{owner}/{repo}/pulls")
async def list_pulls(
    owner: str,
    repo: str,
    request: Request,
    state: str = Query("open"),
    page: int | None = Query(None, ge=1),
    per_page: int | None = Query(None, ge=1),
):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    if store.get_container(conn, "github", repo) is None:
        raise HTTPException(status_code=404, detail="Not Found")
    state_filter = state if state != "all" else None
    prs = [
        r
        for r in store.list_documents(conn, "github", repo, ids, limit=10_000, state=state_filter)
        if r["kind"] == "pull_request"
    ]
    page, per_page = clamp_page(
        page, per_page, get_settings().default_page_size, get_settings().max_page_size
    )
    start = (page - 1) * per_page
    ab = _api_base(request)
    body = [_pr_obj(conn, owner, repo, r, ab) for r in prs[start : start + per_page]]
    return _paged(request, len(prs), {"state": state}, body, page, per_page)


@router.get("/repos/{owner}/{repo}/pulls/{number}")
async def get_pull(owner: str, repo: str, number: int, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _resolve(request, conn, repo, number, ids)
    if row is None or row["kind"] != "pull_request":
        raise HTTPException(status_code=404, detail="Not Found")
    return _pr_obj(conn, owner, repo, row, _api_base(request))


@router.get("/repos/{owner}/{repo}/pulls/{number}/reviews")
async def pull_reviews(owner: str, repo: str, number: int, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _resolve(request, conn, repo, number, ids)
    if row is None:
        raise HTTPException(status_code=404, detail="Not Found")
    ab = _api_base(request)
    number = row["number"] or synth.github_number(row["doc_id"])
    sha = hashlib.sha1(row["doc_id"].encode()).hexdigest()[:40]
    out = []
    for i, rv in enumerate(store.jcol(row, "reviews"), start=1):
        rid = synth.github_number(row["doc_id"] + str(i))
        pr_url = f"{ab}/repos/{owner}/{repo}/pulls/{number}"
        out.append(
            {
                "id": rid,
                "node_id": synth.node_id("PullRequestReview", rid),
                "user": _gh_user(rv.get("author_email", "reviewer@x"), ab),
                "body": rv.get("body", ""),
                "state": rv.get("state", "COMMENTED"),
                "submitted_at": synth.rfc3339(synth.epoch(row["doc_id"]) + i * 60),
                "commit_id": sha,
                "author_association": "MEMBER",
                "html_url": f"https://github.com/{owner}/{repo}/pull/{number}#pullrequestreview-{rid}",
                "pull_request_url": pr_url,
                "_links": {
                    "html": {
                        "href": f"https://github.com/{owner}/{repo}/pull/{number}#pullrequestreview-{rid}"
                    },
                    "pull_request": {"href": pr_url},
                },
            }
        )
    return out


@router.get("/repos/{owner}/{repo}/git/trees/{ref}")
async def get_tree(
    owner: str, repo: str, ref: str, request: Request, recursive: str | None = Query(None)
):
    """The repo's file set as a git tree (real API shape). We keep no history, so any
    `ref` — a branch name or a sha from /branches or /commits — resolves to the repo's
    current files. `recursive` (any truthy value, GitHub-style) returns every blob/tree
    entry; otherwise only the entries directly under root."""
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    if store.get_container(conn, "github", repo) is None:
        raise HTTPException(status_code=404, detail="Not Found")
    ab = _api_base(request)
    rows = store.list_repo_files(conn, repo, ids)
    entries = _tree_from_paths(owner, repo, rows, ab)
    if not _truthy(recursive):
        entries = [e for e in entries if "/" not in e["path"]]
    tree_sha = _repo_tree_sha(repo)
    return {
        "sha": tree_sha,
        "url": f"{ab}/repos/{owner}/{repo}/git/trees/{tree_sha}",
        "tree": entries,
        "truncated": False,
    }


async def _contents_response(owner: str, repo: str, path: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    if store.get_container(conn, "github", repo) is None:
        raise HTTPException(status_code=404, detail="Not Found")
    ab = _api_base(request)
    path = path.strip("/")
    if path:
        row = store.get_repo_file(conn, repo, path, ids)
        if row is not None:
            return _file_obj(owner, repo, row, ab)
    rows = store.list_repo_files(conn, repo, ids)
    entries = _tree_from_paths(owner, repo, rows, ab)
    is_dir = path == "" or any(
        e["path"] == path or e["path"].startswith(path + "/") for e in entries
    )
    if not is_dir:
        raise HTTPException(status_code=404, detail="Not Found")
    children = [e for e in entries if _dirname(e["path"]) == path]
    return [_contents_child(owner, repo, e, ab) for e in children]


@router.get("/repos/{owner}/{repo}/contents")
async def get_contents_root(owner: str, repo: str, request: Request):
    return await _contents_response(owner, repo, "", request)


@router.get("/repos/{owner}/{repo}/contents/{path:path}")
async def get_contents(owner: str, repo: str, path: str, request: Request):
    return await _contents_response(owner, repo, path, request)


@router.get("/repos/{owner}/{repo}/git/blobs/{sha}")
async def get_blob(owner: str, repo: str, sha: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    if store.get_container(conn, "github", repo) is None:
        raise HTTPException(status_code=404, detail="Not Found")
    row = next(
        (r for r in store.list_repo_files(conn, repo, ids) if _blob_sha(r["content"]) == sha), None
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Not Found")
    content = row["content"]
    ab = _api_base(request)
    return {
        "sha": sha,
        "node_id": synth.node_id("Blob", sha[:12]),
        "size": len(content.encode()),
        "encoding": "base64",
        "content": base64.b64encode(content.encode()).decode(),
        "url": f"{ab}/repos/{owner}/{repo}/git/blobs/{sha}",
    }


@router.get("/repos/{owner}/{repo}/branches/{branch}")
async def get_branch(owner: str, repo: str, branch: str, request: Request):
    conn = auth.conn(request)
    _require(request)
    if store.get_container(conn, "github", repo) is None:
        raise HTTPException(status_code=404, detail="Not Found")
    ab = _api_base(request)
    commit_sha, tree_sha = _repo_commit_sha(repo), _repo_tree_sha(repo)
    return {
        "name": branch,
        "protected": False,
        "commit": {
            "sha": commit_sha,
            "commit": {"tree": {"sha": tree_sha}},
            "url": f"{ab}/repos/{owner}/{repo}/commits/{commit_sha}",
        },
    }


@router.get("/repos/{owner}/{repo}/commits/{sha}")
async def get_commit(owner: str, repo: str, sha: str, request: Request):
    conn = auth.conn(request)
    _require(request)
    if store.get_container(conn, "github", repo) is None:
        raise HTTPException(status_code=404, detail="Not Found")
    ab = _api_base(request)
    tree_sha = _repo_tree_sha(repo)
    return {
        "sha": sha,
        "node_id": synth.node_id("Commit", sha[:12]),
        "commit": {
            "tree": {"sha": tree_sha, "url": f"{ab}/repos/{owner}/{repo}/git/trees/{tree_sha}"},
            "message": f"Snapshot of {repo}",
            "url": f"{ab}/repos/{owner}/{repo}/git/commits/{sha}",
        },
        "url": f"{ab}/repos/{owner}/{repo}/commits/{sha}",
        "html_url": f"https://github.com/{owner}/{repo}/commit/{sha}",
    }


@router.get("/repos/{owner}/{repo}/readme")
async def get_readme(owner: str, repo: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    if store.get_container(conn, "github", repo) is None:
        raise HTTPException(status_code=404, detail="Not Found")
    ab = _api_base(request)
    row = store.get_repo_file(conn, repo, "README.md", ids) or store.get_repo_file(
        conn, repo, "readme.md", ids
    )
    if row is not None:
        return _file_obj(owner, repo, row, ab)
    text = f"# {repo}\n\nRepository `{owner}/{repo}`.\n"
    sha = hashlib.sha1(text.encode()).hexdigest()
    url = f"{ab}/repos/{owner}/{repo}/contents/README.md"
    return {
        "type": "file",
        "name": "README.md",
        "path": "README.md",
        "encoding": "base64",
        "content": base64.b64encode(text.encode()).decode(),
        "size": len(text),
        "sha": sha,
        "node_id": synth.node_id("Blob", sha[:12]),
        "url": url,
        "git_url": f"{ab}/repos/{owner}/{repo}/git/blobs/{sha}",
        "html_url": f"https://github.com/{owner}/{repo}/blob/main/README.md",
        "download_url": f"https://raw.githubusercontent.com/{owner}/{repo}/main/README.md",
        "_links": {
            "self": url,
            "git": f"{ab}/repos/{owner}/{repo}/git/blobs/{sha}",
            "html": f"https://github.com/{owner}/{repo}/blob/main/README.md",
        },
    }


@router.get("/repos/{owner}/{repo}/collaborators")
async def list_collaborators(owner: str, repo: str, request: Request):
    conn = auth.conn(request)
    _require(request)
    if store.get_container(conn, "github", repo) is None:
        raise HTTPException(status_code=404, detail="Not Found")
    emails = store.container_member_emails(conn, "github", repo)
    if emails is None:
        emails = store.all_user_emails(conn)
    ab = _api_base(request)
    return [
        {
            **_gh_user(e, ab),
            "role_name": "read",
            "permissions": {
                "admin": False,
                "maintain": False,
                "push": False,
                "triage": False,
                "pull": True,
            },
        }
        for e in sorted(emails)
    ]


@router.get("/orgs/{org}/teams")
async def list_teams(org: str, request: Request):
    conn = auth.conn(request)
    _require(request)
    rows = conn.execute(
        "SELECT id, display_name FROM principals WHERE type = 'group' ORDER BY id"
    ).fetchall()
    ab = _api_base(request)
    return [
        {
            "id": synth.github_user_id(r["id"]),
            "node_id": synth.node_id("Team", synth.github_user_id(r["id"])),
            "name": r["display_name"],
            "slug": r["id"],
            "description": f"{r['display_name']} team",
            "privacy": "closed",
            "permission": "pull",
            "parent": None,
            "url": f"{ab}/orgs/{org}/teams/{r['id']}",
            "html_url": f"https://github.com/orgs/{org}/teams/{r['id']}",
        }
        for r in rows
    ]


@router.get("/repos/{owner}/{repo}/teams")
async def list_repo_teams(owner: str, repo: str, request: Request):
    conn = auth.conn(request)
    _require(request)
    c = store.get_container(conn, "github", repo)
    if c is None:
        raise HTTPException(status_code=404, detail="Not Found")
    if not c["group_id"]:
        return []
    return [
        {
            "id": synth.github_user_id(c["group_id"]),
            "name": c["group_id"],
            "slug": c["group_id"],
            "permission": "pull",
        }
    ]


# --- repo file tree / contents / blobs ------------------------------------------


def _truthy(v: str | None) -> bool:
    """GitHub's `?recursive=` accepts any non-empty, non-'0'/'false' value as true."""
    return v is not None and v.lower() not in ("", "0", "false")


def _blob_sha(content: str) -> str:
    return hashlib.sha1(content.encode()).hexdigest()


def _repo_tree_sha(repo: str) -> str:
    return hashlib.sha1(f"tree:{repo}".encode()).hexdigest()


def _repo_commit_sha(repo: str) -> str:
    return hashlib.sha1(f"commit:{repo}".encode()).hexdigest()


def _dir_sha(repo: str, dirpath: str) -> str:
    return hashlib.sha1(f"tree:{repo}:{dirpath}".encode()).hexdigest()


def _dirname(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _tree_from_paths(owner: str, repo: str, files, api_base: str = "") -> list[dict]:
    """Flat repo file rows -> full recursive git-tree entries: a blob per file plus an
    inferred `tree` (directory) entry for every distinct path prefix. Deterministic and
    sorted by path; callers needing only the top level filter out any path containing '/'."""
    entries: dict[str, dict] = {}
    dirs: set[str] = set()
    for row in files:
        path, content = row["path"], row["content"]
        sha = _blob_sha(content)
        entries[path] = {
            "path": path,
            "mode": "100644",
            "type": "blob",
            "sha": sha,
            "size": len(content.encode()),
            "url": f"{api_base}/repos/{owner}/{repo}/git/blobs/{sha}",
        }
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    for d in dirs:
        dsha = _dir_sha(repo, d)
        entries[d] = {
            "path": d,
            "mode": "040000",
            "type": "tree",
            "sha": dsha,
            "url": f"{api_base}/repos/{owner}/{repo}/git/trees/{dsha}",
        }
    return [entries[p] for p in sorted(entries)]


def _file_obj(owner: str, repo: str, row, api_base: str = "") -> dict:
    """The Contents API's file-object shape (base64 body), used by both /contents/{path}
    and /readme — real GitHub serves both from the same underlying object."""
    content = row["content"]
    path = row["path"]
    name = path.rsplit("/", 1)[-1]
    sha = _blob_sha(content)
    url = f"{api_base}/repos/{owner}/{repo}/contents/{path}"
    git_url = f"{api_base}/repos/{owner}/{repo}/git/blobs/{sha}"
    html_url = f"https://github.com/{owner}/{repo}/blob/main/{path}"
    download_url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/{path}"
    return {
        "type": "file",
        "name": name,
        "path": path,
        "encoding": "base64",
        "content": base64.b64encode(content.encode()).decode(),
        "size": len(content.encode()),
        "sha": sha,
        "node_id": synth.node_id("Blob", sha[:12]),
        "url": url,
        "git_url": git_url,
        "html_url": html_url,
        "download_url": download_url,
        "_links": {"self": url, "git": git_url, "html": html_url},
    }


def _contents_child(owner: str, repo: str, entry: dict, api_base: str = "") -> dict:
    """One element of a directory listing (real Contents API array shape)."""
    is_file = entry["type"] == "blob"
    name = entry["path"].rsplit("/", 1)[-1]
    self_url = f"{api_base}/repos/{owner}/{repo}/contents/{entry['path']}"
    git_url = (
        f"{api_base}/repos/{owner}/{repo}/git/blobs/{entry['sha']}"
        if is_file
        else f"{api_base}/repos/{owner}/{repo}/git/trees/{entry['sha']}"
    )
    html_url = (
        f"https://github.com/{owner}/{repo}/{'blob' if is_file else 'tree'}/main/{entry['path']}"
    )
    return {
        "name": name,
        "path": entry["path"],
        "sha": entry["sha"],
        "size": entry.get("size", 0),
        "url": self_url,
        "html_url": html_url,
        "git_url": git_url,
        "download_url": (
            f"https://raw.githubusercontent.com/{owner}/{repo}/main/{entry['path']}"
            if is_file
            else None
        ),
        "type": "file" if is_file else "dir",
        "_links": {"self": self_url, "git": git_url, "html": html_url},
    }


# --- object builders ------------------------------------------------------------


def _gh_user(email: str, api_base: str = "") -> dict:
    """A full Simple User object (login/id/node_id/avatar/urls/type/site_admin)."""
    login = synth.github_login(email)
    uid = synth.github_user_id(email)
    return {
        "login": login,
        "id": uid,
        "node_id": synth.node_id("User", uid),
        "avatar_url": synth.github_avatar(uid),
        "gravatar_id": "",
        "url": f"{api_base}/users/{login}",
        "html_url": f"https://github.com/{login}",
        "type": "User",
        "site_admin": False,
    }


def _reactions(val, api_url: str = "") -> dict:
    """Normalize a stored reactions blob into the real GitHub rollup shape (all 8 keys)."""
    roll = {
        "+1": 0,
        "-1": 0,
        "laugh": 0,
        "hooray": 0,
        "confused": 0,
        "heart": 0,
        "rocket": 0,
        "eyes": 0,
    }
    if isinstance(val, dict):
        for k, v in val.items():
            if k in roll and isinstance(v, int):
                roll[k] = v
    total = sum(roll.values())
    return {"url": f"{api_url}/reactions", "total_count": total, **roll}


def _repo_obj(conn, owner: str, name: str, api_base: str = "") -> dict:
    private = not store.container_has_public(conn, "github", name)
    rid = synth.github_user_id(name)
    ts = synth.epoch("repo:" + name)
    return {
        "id": rid,
        "node_id": synth.node_id("Repository", rid),
        "name": name,
        "full_name": f"{owner}/{name}",
        "private": private,
        "visibility": "private" if private else "public",
        "owner": {**_gh_user(f"{owner}@org", api_base), "login": owner, "type": "Organization"},
        "html_url": f"https://github.com/{owner}/{name}",
        "url": f"{api_base}/repos/{owner}/{name}",
        "description": f"{name} service repository.",
        "fork": False,
        "archived": False,
        "disabled": False,
        "created_at": synth.rfc3339(ts),
        "updated_at": synth.rfc3339(ts + 3600),
        "pushed_at": synth.rfc3339(ts + 7200),
        "default_branch": "main",
    }


def _resolve(request: Request, conn, repo: str, number: int, ids):
    doc_id = request.app.state.index["github"].get((repo, number))
    row = store.get_document(conn, "github", doc_id, visible_ids=ids) if doc_id else None
    # a 'file' doc's synthesized number lives in the same index but must never surface as
    # an issue/PR (it has no title/body/state in the issue sense).
    return row if row is not None and row["kind"] != "file" else None


def _milestone(row, owner, repo, api_base):
    title = row["milestone"]
    if not title:
        return None
    num = synth.github_number(row["doc_id"] + ":ms") % 100
    return {
        "number": num,
        "title": title,
        "state": "open",
        "url": f"{api_base}/repos/{owner}/{repo}/milestones/{num}",
        "html_url": f"https://github.com/{owner}/{repo}/milestone/{num}",
    }


def _issue_obj(conn, owner: str, repo: str, row, api_base: str = "") -> dict:
    created = row["created_ts"] or synth.epoch(row["doc_id"])
    updated = row["updated_ts"] or created + 3600
    number = row["number"] or synth.github_number(row["doc_id"])
    iid = synth.jira_numeric_id(row["doc_id"])  # a stable large numeric db id (≠ number)
    is_pr = row["kind"] == "pull_request"
    kind = "pull" if is_pr else "issues"
    state = row["state"] or "open"
    assignees = [_gh_user(a, api_base) for a in store.jcol(row, "assignees")]
    self_url = f"{api_base}/repos/{owner}/{repo}/issues/{number}"
    closed_at = (
        synth.rfc3339(row["closed_ts"])
        if row["closed_ts"]
        else synth.rfc3339(updated)
        if state == "closed"
        else None
    )
    obj = {
        "id": iid,
        "node_id": synth.node_id("Issue", iid),
        "number": number,
        "title": row["title"],
        "body": row["content"],
        "state": state,
        "state_reason": ("completed" if state == "closed" else None),
        "locked": False,
        "active_lock_reason": None,
        "user": _gh_user(row["author_email"], api_base),
        "labels": [
            {
                "id": synth.github_number(row["doc_id"] + lbl),
                "name": lbl,
                "color": "ededed",
                "default": False,
                "description": None,
            }
            for lbl in store.jcol(row, "labels")
        ],
        "assignee": assignees[0] if assignees else None,
        "assignees": assignees,
        "milestone": _milestone(row, owner, repo, api_base),
        "comments": len(store.doc_comments(conn, "github", row["doc_id"])),
        "reactions": _reactions(store.jcol(row, "reactions", {}), self_url),
        "author_association": "MEMBER",
        "created_at": synth.rfc3339(created),
        "updated_at": synth.rfc3339(updated),
        "closed_at": closed_at,
        "closed_by": _gh_user(row["closed_by"], api_base) if row["closed_by"] else None,
        "url": self_url,
        "repository_url": f"{api_base}/repos/{owner}/{repo}",
        "labels_url": f"{self_url}/labels{{/name}}",
        "comments_url": f"{self_url}/comments",
        "events_url": f"{self_url}/events",
        "html_url": f"https://github.com/{owner}/{repo}/{kind}/{number}",
        "timeline_url": f"{self_url}/timeline",
    }
    if is_pr:  # the marker connectors use to tell PRs apart in the /issues stream
        obj["pull_request"] = {
            "url": f"{api_base}/repos/{owner}/{repo}/pulls/{number}",
            "html_url": f"https://github.com/{owner}/{repo}/pull/{number}",
            "diff_url": f"https://github.com/{owner}/{repo}/pull/{number}.diff",
            "patch_url": f"https://github.com/{owner}/{repo}/pull/{number}.patch",
            "merged_at": row["merged_at"],
        }
    return obj


def _pr_obj(conn, owner: str, repo: str, row, api_base: str = "") -> dict:
    obj = _issue_obj(conn, owner, repo, row, api_base)
    sha = hashlib.sha1(row["doc_id"].encode()).hexdigest()
    number = obj["number"]
    reviewers = [_gh_user(e, api_base) for e in store.jcol(row, "requested_reviewers")]
    n_comments = obj["comments"]
    obj.update(
        {
            "draft": False,
            "merged": bool(row["merged_at"]),
            "merged_at": row["merged_at"],
            "merged_by": _gh_user(row["merged_by"], api_base) if row["merged_by"] else None,
            "mergeable": None,
            "mergeable_state": "unknown",
            "rebaseable": None,
            "merge_commit_sha": sha[:40] if row["merged_at"] else None,
            "requested_reviewers": reviewers,
            "requested_teams": [],
            "head": {
                "ref": row["head_ref"] or "feature",
                "sha": sha,
                "label": f"{owner}:{row['head_ref'] or 'feature'}",
                "user": obj["user"],
                "repo": {"full_name": f"{owner}/{repo}"},
            },
            "base": {
                "ref": row["base_ref"] or "main",
                "sha": sha[::-1],
                "label": f"{owner}:{row['base_ref'] or 'main'}",
                "user": obj["user"],
                "repo": {"full_name": f"{owner}/{repo}"},
            },
            "commits": 1,
            "additions": len(row["content"]) // 20,
            "deletions": 0,
            "changed_files": 1,
            "review_comments": 0,
            "comments": n_comments,
            "url": f"{api_base}/repos/{owner}/{repo}/pulls/{number}",
            "diff_url": f"https://github.com/{owner}/{repo}/pull/{number}.diff",
            "patch_url": f"https://github.com/{owner}/{repo}/pull/{number}.patch",
            "issue_url": f"{api_base}/repos/{owner}/{repo}/issues/{number}",
        }
    )
    return obj


def _gh_comment(owner: str, repo: str, number: int, c, api_base: str = "") -> dict:
    ts = c["created_ts"] or synth.epoch(c["id"])
    email = c["author_email"] or "unknown@x"
    cid = synth.github_number(c["id"])
    self_url = f"{api_base}/repos/{owner}/{repo}/issues/comments/{cid}"
    return {
        "id": cid,
        "node_id": synth.node_id("IssueComment", cid),
        "body": c["body"],
        "user": _gh_user(email, api_base),
        "created_at": synth.rfc3339(ts),
        "updated_at": synth.rfc3339(ts),
        "author_association": "MEMBER",
        "reactions": _reactions(store.jcol(c, "reactions", {}), self_url),
        "url": self_url,
        "issue_url": f"{api_base}/repos/{owner}/{repo}/issues/{number}",
        "html_url": f"https://github.com/{owner}/{repo}/issues/{number}#issuecomment-{cid}",
    }
