"""Mock GitHub REST API (read-only). Client base_url: ``http://<host>/github``.

Each dataset ``github`` document is modelled as an issue in its repo (= container).
Responses are bare JSON arrays with an RFC5988 ``Link`` header for pagination, as the
real API does. Auth: ``Authorization: Bearer <token>`` (or ``token <token>``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from email.utils import formatdate

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backlot import auth, store, synth
from backlot.acl import Caller
from backlot.config import get_settings
from backlot.pagination import clamp_page, github_link_header

# Real GitHub caps a recursive tree at 100k entries / 7 MB and reports `truncated: true`. Module
# level rather than settings, so a test can lower them: a corpus big enough to hit the real cap is
# not a practical fixture, and a client's truncation-handling path is only reachable if the mock
# can set the flag at all.
TREE_MAX_ENTRIES = 100_000
TREE_MAX_BYTES = 7 * 1024 * 1024


def _require(request: Request) -> Caller:
    return auth.require_bearer(request, "Bad credentials")


def _org(request: Request) -> str:
    """The single org this mock serves. ``tokens.yaml``'s ``org`` wins over the setting and lands
    on the ACL (see ``backlot.main``), so read it from there when there is one."""
    return getattr(getattr(request.app.state, "acl", None), "org_name", None) or (
        get_settings().org_name
    )


async def _validate_path_owner(request: Request) -> None:
    """404 a request whose ``{owner}``/``{org}`` segment is not the org we serve.

    Real GitHub 404s a wrong owner; echoing whatever was asked for back into the response lets a
    client's owner-handling bug pass against the mock and fail in production. A router-wide
    dependency rather than a call in each handler so a route added later cannot forget it — routes
    with neither path param (``/search/issues``, ``/user/repos``) are unaffected. Credentials are
    checked first, so a bad token still reports 401 rather than the owner's 404.
    """
    _require(request)
    owner = request.path_params.get("owner") or request.path_params.get("org")
    if owner is not None and owner.lower() != _org(request).lower():
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(prefix="/github", tags=["github"], dependencies=[Depends(_validate_path_owner)])


# --- media-type negotiation -----------------------------------------------------
#
# The `Accept` header selects a REPRESENTATION on GitHub, not just an encoding: a content endpoint
# asked for `raw` answers the file's bytes, and a pull asked for `diff`/`patch` answers a diff. A
# handler that ignores it returns a JSON envelope with a 200 and no way for the caller to tell,
# which is silent corruption rather than a missing feature.


def _accept_types(request: Request) -> list[str]:
    return [t.split(";")[0].strip().lower() for t in request.headers.get("accept", "").split(",")]


def _github_media(request: Request, name: str) -> bool:
    """True if `Accept` asks for GitHub's ``<name>`` representation, in any of the spellings the
    real API honours: ``application/vnd.github.<name>``, the legacy ``…github.v3.<name>``, and the
    ``…github.<name>+json`` form."""
    wanted = {
        f"application/vnd.github.{name}",
        f"application/vnd.github.v3.{name}",
        f"application/vnd.github.{name}+json",
    }
    return any(t in wanted for t in _accept_types(request))


def _raw_response(request: Request, content: str, media_type: str) -> Response | None:
    """The raw body when the caller asked for it, else ``None`` so the handler falls through to its
    JSON envelope."""
    if not _github_media(request, "raw"):
        return None
    return Response(content=content.encode(), media_type=media_type)


# Real GitHub answers git/blobs raw with text/plain and contents/readme with vnd.github.raw. Both
# are the same bytes; the difference is GitHub's own, so it is reproduced rather than unified.
_BLOB_RAW_TYPE = "text/plain; charset=utf-8"
_CONTENT_RAW_TYPE = "application/vnd.github.raw; charset=utf-8"


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


def _visible_repos(conn, ids) -> list[str]:
    """Repo names the caller can see at all — one with no visible document is not visible."""
    repos = [r["name"] for r in store.list_containers(conn, "github")]
    if ids is not None:
        repos = [n for n in repos if store.count_documents(conn, "github", n, ids) > 0]
    return repos


def _repo_page(request, conn, owner: str, ids, page, per_page) -> Response:
    repos = _visible_repos(conn, ids)
    page, per_page = clamp_page(
        page, per_page, get_settings().default_page_size, get_settings().max_page_size
    )
    start = (page - 1) * per_page
    ab = _api_base(request)
    body = [_repo_obj(conn, owner, n, ab) for n in repos[start : start + per_page]]
    return _paged(request, len(repos), {}, body, page, per_page)


@router.get("/orgs/{org}/repos")
async def list_repos(
    org: str,
    request: Request,
    page: int | None = Query(None, ge=1),
    per_page: int | None = Query(None, ge=1),
):
    conn = auth.conn(request)
    caller = _require(request)
    return _repo_page(request, conn, org, auth.visible_ids(request, caller), page, per_page)


@router.get("/user/repos")
async def list_user_repos(
    request: Request,
    page: int | None = Query(None, ge=1),
    per_page: int | None = Query(None, ge=1),
):
    """The repositories the CREDENTIAL can reach (real ``GET /user/repos``).

    ``/orgs/{org}/repos`` answers a different question and is not a substitute: a real fine-grained
    token may span several orgs or cover only personal repos, so an org listing is not the token's
    view of the world. This is the endpoint a credential uses to discover its own reach, and
    without it a client has to be configured with an explicit repo name per mount.

    Real GitHub also takes ``visibility``/``affiliation``/``type``/``sort``; the mock serves a
    single org whose repos are all owned by it, so those would have nothing to select between and
    are left out rather than accepted and ignored.
    """
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    return _repo_page(request, conn, _org(request), ids, page, per_page)


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
    # one file listing for the whole page: each PR's changeset chooses from it (see _pr_files)
    paths = store.list_repo_file_paths(conn, repo, ids)
    body = [
        _pr_obj(conn, owner, repo, r, ab, ids=ids, paths=paths)
        for r in prs[start : start + per_page]
    ]
    return _paged(request, len(prs), {"state": state}, body, page, per_page)


@router.get("/repos/{owner}/{repo}/pulls/{number}")
async def get_pull(owner: str, repo: str, number: int, request: Request):
    """The pull as JSON, or as its diff when `Accept` asks for one.

    ``application/vnd.github.diff`` and ``…patch`` are representations of this resource, not
    separate endpoints, so ignoring them meant a caller that piped the result to `git apply` or a
    diff viewer got the pull's JSON with a 200 and no way to notice."""
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _resolve(request, conn, repo, number, ids)
    if row is None or row["kind"] != "pull_request":
        raise HTTPException(status_code=404, detail="Not Found")
    ab = _api_base(request)
    wants_diff, wants_patch = _github_media(request, "diff"), _github_media(request, "patch")
    if not (wants_diff or wants_patch):
        return _pr_obj(conn, owner, repo, row, ab, ids=ids)
    files = _pr_files(conn, owner, repo, row, ab, ids)
    obj = _pr_obj(conn, owner, repo, row, ab, ids=ids, files=files)
    diff = _pr_diff(files, obj["base"]["sha"])
    if wants_patch:
        return Response(
            content=_pr_mbox(row, obj, diff).encode(),
            media_type="application/vnd.github.patch; charset=utf-8",
        )
    return Response(content=diff.encode(), media_type="application/vnd.github.diff; charset=utf-8")


@router.get("/repos/{owner}/{repo}/pulls/{number}/reviews")
async def pull_reviews(owner: str, repo: str, number: int, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _resolve(request, conn, repo, number, ids)
    if row is None:
        raise HTTPException(status_code=404, detail="Not Found")
    ab = _api_base(request)
    number = synth.github_number(row["doc_id"])
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


@router.get("/repos/{owner}/{repo}/pulls/{number}/comments")
async def pull_review_comments(owner: str, repo: str, number: int, request: Request):
    """A pull's REVIEW comments — the line-anchored ones, a different resource from both
    ``/issues/{n}/comments`` (the conversation) and ``/pulls/{n}/reviews`` (approve/request-changes
    events, which this mock does carry).

    ``github_comments`` has no ``path``/``line``/``diff_hunk``, so a line-anchored comment cannot be
    represented and this is always empty. That is still the right answer rather than the 404 it used
    to be: the collection is a real resource on every pull, a pull with no review comments answers
    ``[]`` on real GitHub too, and a 404 aborts any client that renders a pull by combining its
    metadata, conversation, review comments and files. Returning the conversation here instead would
    be worse — it would duplicate ``/issues/{n}/comments`` under a resource that means something
    else.
    """
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _resolve(request, conn, repo, number, ids)
    if row is None or row["kind"] != "pull_request":
        raise HTTPException(status_code=404, detail="Not Found")
    return []


@router.get("/repos/{owner}/{repo}/pulls/{number}/files")
async def pull_files(owner: str, repo: str, number: int, request: Request):
    """The pull's changed-file list (``filename``/``status``/``additions``/``deletions``/``patch``).
    Synthesized from the repo's own snapshot — see the changeset note below."""
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _resolve(request, conn, repo, number, ids)
    if row is None or row["kind"] != "pull_request":
        raise HTTPException(status_code=404, detail="Not Found")
    return _pr_files(conn, owner, repo, row, _api_base(request), ids)


@router.get("/repos/{owner}/{repo}/git/ref/{ref:path}")
async def get_git_ref(owner: str, repo: str, ref: str, request: Request):
    """Resolve a ref (``heads/main``, ``heads/release/2026-03``, ``tags/v1``) to a commit.

    The ref is the trailing PATH, which is the whole reason a client reaches for this instead of
    ``/branches/{branch}``: a branch name containing a slash does not fit in one path segment, so
    without this route such a branch cannot be pinned to a commit at all.

    Any ref resolves to the repo's snapshot commit, as ``/branches`` and ``git/trees`` already do —
    the mock keeps no history and the schema carries no branch list, so ref EXISTENCE is not
    knowable here. That is the documented no-history simplification (see :func:`get_tree`), not the
    unvalidated-segment bug that the owner check fixes: an owner IS knowable.
    """
    conn = auth.conn(request)
    _require(request)
    if store.get_container(conn, "github", repo) is None:
        raise HTTPException(status_code=404, detail="Not Found")
    ref = ref.strip("/")
    if ref.startswith("refs/"):  # real API takes `heads/main`; tolerate the fully-qualified form
        ref = ref[len("refs/") :]
    if not ref:
        raise HTTPException(status_code=404, detail="Not Found")
    ab = _api_base(request)
    sha = _repo_commit_sha(repo)
    return {
        "ref": f"refs/{ref}",
        "node_id": synth.node_id("Ref", synth.github_number(f"{repo}:{ref}")),
        "url": f"{ab}/repos/{owner}/{repo}/git/ref/{ref}",
        "object": {
            "type": "commit",
            "sha": sha,
            "url": f"{ab}/repos/{owner}/{repo}/git/commits/{sha}",
        },
    }


@router.get("/repos/{owner}/{repo}/git/trees/{ref}")
async def get_tree(
    owner: str, repo: str, ref: str, request: Request, recursive: str | None = Query(None)
):
    """The repo's file set as a git tree (real API shape). `recursive` (any truthy value,
    GitHub-style) returns every blob/tree entry; otherwise only the entries directly under root.

    **We keep no history**, so any `ref` — a branch name, or a sha from /branches, /commits or
    git/ref — resolves to the repo's CURRENT files. Two consequences a client author will otherwise
    assume away:

    - Pinning to a commit sha gives no immutability guarantee. A blob sha is content-addressed and
      stable, but the tree a commit sha resolves to moves whenever the corpus changes, so caching a
      snapshot against a commit sha caches a moving target.
    - Because there is no before/after state, a pull's changeset is synthesized out of this same
      snapshot rather than diffed from it (see :func:`_pr_files`) — which is also why a file the
      changeset reports as `removed` is still present here.

    `truncated` follows the real caps (:data:`TREE_MAX_ENTRIES` / :data:`TREE_MAX_BYTES`)."""
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
    entries, truncated = _cap_tree(entries)
    tree_sha = _repo_tree_sha(repo)
    return {
        "sha": tree_sha,
        "url": f"{ab}/repos/{owner}/{repo}/git/trees/{tree_sha}",
        "tree": entries,
        "truncated": truncated,
    }


def _cap_tree(entries: list[dict]) -> tuple[list[dict], bool]:
    """Apply the real API's tree caps, returning ``(entries, truncated)``.

    Real GitHub caps a recursive tree at 100k entries / 7 MB and sets `truncated: true`; a mock that
    reports `false` unconditionally means a client's truncation-handling path — the fallback where
    it walks the tree one level at a time instead — is never exercised. The whole-list `dumps` is
    the common case and runs once; the per-entry loop only runs for a tree that actually overflows.
    """
    if len(entries) > TREE_MAX_ENTRIES:
        return entries[:TREE_MAX_ENTRIES], True
    if len(json.dumps(entries)) <= TREE_MAX_BYTES:
        return entries, False
    kept, size = [], 2  # the enclosing brackets
    for e in entries:
        size += len(json.dumps(e)) + 1
        if size > TREE_MAX_BYTES:
            break
        kept.append(e)
    return kept, True


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
            return _raw_response(request, row["content"], _CONTENT_RAW_TYPE) or _file_obj(
                owner, repo, row, ab
            )
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
    raw = _raw_response(request, content, _BLOB_RAW_TYPE)
    if raw is not None:
        return raw
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
        return _raw_response(request, row["content"], _CONTENT_RAW_TYPE) or _file_obj(
            owner, repo, row, ab
        )
    text = f"# {repo}\n\nRepository `{owner}/{repo}`.\n"
    raw = _raw_response(request, text, _CONTENT_RAW_TYPE)
    if raw is not None:
        return raw
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
    number = synth.github_number(row["doc_id"])
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


def _pr_obj(
    conn, owner: str, repo: str, row, api_base: str = "", ids=None, files=None, paths=None
) -> dict:
    obj = _issue_obj(conn, owner, repo, row, api_base)
    sha = hashlib.sha1(row["doc_id"].encode()).hexdigest()
    number = obj["number"]
    reviewers = [_gh_user(e, api_base) for e in store.jcol(row, "requested_reviewers")]
    n_comments = obj["comments"]
    if files is None:
        files = _pr_files(conn, owner, repo, row, api_base, ids, paths)
    obj.update(
        {
            # A PR's issue view and its pull view are two DISTINCT nodes on real GitHub, so this
            # overrides the Issue-typed id _issue_obj built. /issues keeps the Issue one on purpose.
            "node_id": synth.node_id("PullRequest", obj["id"]),
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
            # Summed over the synthesized changeset rather than guessed from the body length, so
            # these agree with what /pulls/{n}/files reports. They used to contradict it.
            "additions": sum(f["additions"] for f in files),
            "deletions": sum(f["deletions"] for f in files),
            "changed_files": len(files),
            "review_comments": 0,
            "comments": n_comments,
            "url": f"{api_base}/repos/{owner}/{repo}/pulls/{number}",
            "diff_url": f"https://github.com/{owner}/{repo}/pull/{number}.diff",
            "patch_url": f"https://github.com/{owner}/{repo}/pull/{number}.patch",
            "issue_url": f"{api_base}/repos/{owner}/{repo}/issues/{number}",
        }
    )
    return obj


# --- pull request changesets ----------------------------------------------------
#
# `github_items` carries nothing about what a pull CHANGED: no files, no patch, no additions/
# deletions. The changeset below is therefore synthesized — deterministically, seeded on the pull's
# doc_id — but it invents no content: it only ever names files the repo actually has, and every line
# on either side of a hunk is a line of that file's own snapshot. A `modified` file's "before" state
# is the snapshot with one real block either taken out or duplicated (see _patch_modified), so the
# hunk is a well-formed diff against real bytes in both directions.
#
# What that buys, none of which was reachable before: a client's diff path, its handling of an
# omitted `patch`, and its pagination over a changed-file list.
#
# THE SNAPSHOT IS THE PULL'S HEAD. Every hunk here is expressed against that one convention, which
# is what makes the diff actually apply: `git apply --reverse` walks the snapshot back to the base
# (tests/test_github.py checks exactly that with real git). Two consequences:
#   - A repo with no `file` docs has an EMPTY changeset, and the pull object's counts follow it to
#     zero. There is no snapshot to diff, and naming a file the repo does not contain would
#     contradict its own tree.
#   - `status: "removed"` is NOT produced. A deleted file is absent from the head, but the snapshot
#     is the only state the mock has, so a file it names as removed would still be in the tree — and
#     a diff mixing that with a `modified` hunk claims the snapshot is base and head at once, which
#     no client can apply. Deletions come from the `dedup` flavour below instead.

_MAX_CHANGED_FILES = 3
_MAX_BLOCK_LINES = 3  # how many lines one hunk adds or removes
_PATCH_CONTEXT = 3
# Real GitHub omits `patch` for a binary file or a diff too large to inline.
PATCH_MAX_BYTES = 1024 * 1024
_CHANGE_STATUSES = ("modified", "added")


def _pr_files(
    conn, owner: str, repo: str, row, api_base: str = "", ids=None, paths: list[str] | None = None
) -> list[dict]:
    """The pull's changed files in the real API's shape. See the changeset note above.

    Chooses among PATHS and then reads only the few files it picked, so the cost is one cheap
    listing plus <=3 point lookups rather than every file's content. ``paths`` lets a list endpoint
    hoist that one listing out of its per-row loop (see :func:`list_pulls`) — without it, a
    ``/pulls`` page repeats it once per pull.
    """
    if paths is None:
        paths = store.list_repo_file_paths(conn, repo, ids)
    if not paths:
        return []
    doc_id = row["doc_id"]
    n = 1 + synth.hnum(doc_id, salt="pr-nfiles") % min(_MAX_CHANGED_FILES, len(paths))
    start = synth.hnum(doc_id, salt="pr-offset") % len(paths)
    head_sha = hashlib.sha1(doc_id.encode()).hexdigest()
    out = []
    for i in range(n):
        f = store.get_repo_file(conn, repo, paths[(start + i) % len(paths)], ids)
        if f is None:  # the listing and the read raced a writer; drop it rather than half-report it
            continue
        # the first file is always `modified`, so a changeset always carries one hunk with context
        status = (
            "modified" if i == 0 else _CHANGE_STATUSES[synth.hnum(doc_id, salt=f"pr-st{i}") % 2]
        )
        out.append(_changed_file(doc_id, owner, repo, f, status, head_sha, api_base))
    return out


def _changed_file(
    doc_id: str, owner: str, repo: str, row, status: str, head_sha: str, api_base: str
) -> dict:
    content = row["content"] or ""
    path = row["path"]
    lines = content.splitlines(keepends=True)
    # A final line with its own newline missing can be hunk CONTEXT (git's own `\ No newline`
    # marker covers that) but never part of a chosen block: duplicating or inserting it mid-file
    # would put the marker somewhere git rejects.
    selectable = len(lines) if content.endswith("\n") else len(lines) - 1
    if status == "modified" and selectable < 1:
        status = "added"  # nothing to build a hunk out of; the whole file is the change
    if not lines:
        added, deleted, patch = 0, 0, None
    elif status == "added":
        added, deleted, patch = len(lines), 0, _patch_new_file(lines)
    else:
        added, deleted, patch = _patch_modified(f"{doc_id}:{path}", lines, selectable)
    if patch is not None and len(patch.encode()) > PATCH_MAX_BYTES:
        patch = None  # as real GitHub does for an oversized diff
    sha = _blob_sha(content)
    obj = {
        "sha": sha,
        "filename": path,
        "status": status,
        "additions": added,
        "deletions": deleted,
        "changes": added + deleted,
        "blob_url": f"https://github.com/{owner}/{repo}/blob/{head_sha}/{path}",
        "raw_url": f"https://github.com/{owner}/{repo}/raw/{head_sha}/{path}",
        "contents_url": f"{api_base}/repos/{owner}/{repo}/contents/{path}?ref={head_sha}",
    }
    if patch is not None:
        obj["patch"] = patch
    return obj


def _patch_new_file(lines: list[str]) -> str:
    """The hunk for a file the pull ADDED: the old side is empty, so there is no "before" to
    reconstruct at all and every line is a real line of the snapshot."""
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "".join("+" + ln for ln in _nl_terminated(lines))


def _patch_modified(seed: str, lines: list[str], selectable: int) -> tuple[int, int, str]:
    """A hunk for a file the pull MODIFIED, in one of two flavours. Returns
    ``(additions, deletions, patch)``.

    Both express the change against the snapshot as the HEAD, and neither writes a line the file
    does not already contain — a replacement would need "before" text that is nowhere in the corpus,
    and inventing a line the file never held is the fabrication this module exists to avoid:

    - ``insertion`` — the pull added a real block of the file; the base is the snapshot with that
      block taken out. Pure additions.
    - ``dedup`` — the pull removed a duplicated copy of a real block; the base is the snapshot with
      that block appearing twice. Pure deletions, and a realistic change to have made.
    """
    k = 1 + synth.hnum(seed, salt="pr-block") % min(_MAX_BLOCK_LINES, max(1, selectable - 1))
    at = synth.hnum(seed, salt="pr-at") % (selectable - k + 1)
    pre = lines[max(0, at - _PATCH_CONTEXT) : at]
    block = lines[at : at + k]
    post = lines[at + k : at + k + _PATCH_CONTEXT]
    start = max(0, at - _PATCH_CONTEXT) + 1  # the pre-context sits at the same offset on both sides
    if synth.hnum(seed, salt="pr-flavour") % 2:
        old_n, new_n = len(pre) + len(post), len(pre) + k + len(post)
        body = [" " + ln for ln in pre] + ["+" + ln for ln in block]
        added, deleted = k, 0
    else:
        old_n, new_n = len(pre) + 2 * k + len(post), len(pre) + k + len(post)
        body = [" " + ln for ln in pre] + ["-" + ln for ln in block] + [" " + ln for ln in block]
        added, deleted = 0, k
    body += [" " + ln for ln in _nl_terminated(post)]
    return added, deleted, f"@@ -{start},{old_n} +{start},{new_n} @@\n" + "".join(body)


def _nl_terminated(lines: list[str]) -> list[str]:
    """Each line newline-terminated, so a hunk's rows cannot run together. A final line with no
    newline of its own gets git's own marker."""
    out = []
    for ln in lines:
        out.append(ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n")
    return out


def _pr_diff(files: list[dict], base_sha: str) -> str:
    """The pull's unified diff (`Accept: application/vnd.github.diff`), git-apply-able."""
    out = []
    for f in files:
        a, b = f"a/{f['filename']}", f"b/{f['filename']}"
        out.append(f"diff --git {a} {b}\n")
        short = f["sha"][:7]
        if f["status"] == "added":
            out.append(f"new file mode 100644\nindex 0000000..{short}\n--- /dev/null\n+++ {b}\n")
        else:  # `removed` is never synthesized — see the changeset note above
            out.append(f"index {base_sha[:7]}..{short} 100644\n--- {a}\n+++ {b}\n")
        if f.get("patch"):
            out.append(f["patch"] if f["patch"].endswith("\n") else f["patch"] + "\n")
    return "".join(out)


def _pr_mbox(row, obj: dict, diff: str) -> str:
    """The pull as a mail patch (`Accept: application/vnd.github.patch`). Real GitHub's `patch`
    media type is a `git am`-able mbox, NOT the same bytes as `diff` — a client that pipes one to
    the wrong tool has to be able to tell them apart here too."""
    ts = row["created_ts"] or synth.epoch(row["doc_id"])
    email_addr = row["author_email"] or "unknown@users.noreply.github.com"
    login = synth.github_login(email_addr)
    head = obj["head"]["sha"]
    body = (row["content"] or "").rstrip("\n")
    return (
        f"From {head} Mon Sep 17 00:00:00 2001\n"
        f"From: {login} <{email_addr}>\n"
        f"Date: {formatdate(ts, usegmt=True)}\n"
        f"Subject: [PATCH] {obj['title']}\n\n"
        f"{body}\n---\n{diff}-- \n2.45.0\n"
    )


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
