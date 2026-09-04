"""GitHub REST API (read-only). Client base_url: ``http://<host>/github``.

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
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backlot import auth, store, synth
from backlot.acl import Caller
from backlot.config import get_settings
from backlot.pagination import PageParam, clamp_page, github_link_header

# Real GitHub caps a recursive tree at 100k entries / 7 MB and reports `truncated: true`. Module
# level rather than settings, so a test can lower them: a corpus big enough to hit the real cap is
# not a practical fixture, and a client's truncation-handling path is only reachable if Backlot
# can set the flag at all.
TREE_MAX_ENTRIES = 100_000
TREE_MAX_BYTES = 7 * 1024 * 1024


def _require(request: Request) -> Caller:
    """The caller, or real's 401 for the reason it failed.

    Two reasons, two messages, as real has them: "Bad credentials" is for a credential that arrived
    and did not resolve, and a request carrying none gets "Requires authentication" (measured
    against api.github.com: a bad bearer at `/repos/{owner}/{repo}/collaborators`, no header at the same route
    and at `/user`). A client that branches on which — retry versus re-authenticate — reads one
    answer for both otherwise.

    Header PRESENT but not a scheme this API takes is the second case, not the first: real ignores
    an `Authorization` it cannot parse and serves the request anonymously (measured: `Basic …` and a
    scheme-less value both answer 200 on a public repo), so the caller reaches an auth-required
    route with no credential rather than with a rejected one. Backlot serves nothing anonymously,
    which is why that lands here as the missing-credential 401 rather than as a public read.
    """
    if auth.bearer_token(request) is None:
        raise HTTPException(status_code=401, detail="Requires authentication")
    return auth.require_bearer(request, "Bad credentials")


def _org(request: Request) -> str:
    """The single org Backlot serves. ``tokens.yaml``'s ``org`` wins over the setting and lands
    on the ACL (see ``backlot.main``), so read it from there when there is one."""
    return getattr(getattr(request.app.state, "acl", None), "org_name", None) or (
        get_settings().org_name
    )


# --- API version negotiation ----------------------------------------------------
#
# `X-GitHub-Api-Version` selects a PAYLOAD, not an encoding, and real GitHub currently supports two
# values. On this surface they differ by three fields: `2026-03-10` dropped `assignee` from issues
# and pulls (superseded by the `assignees` array) and `merge_commit_sha` from pulls.
#
# The behaviour to avoid is accepting the header and ignoring it. A client that pins a version then
# reads fields from a different one with no way to tell it had no effect — which is worse than not
# supporting the header at all, because the failure is silent and the client's own version handling
# looks tested. So: an unsupported value is refused, the selected value is echoed on every response
# (see `backlot.main`), and the builders take it as an argument rather than reading a global.

# Most recent first, which is the order real's own error message lists them in.
API_VERSIONS = ("2026-03-10", "2022-11-28")
# What real serves an UNPINNED request — read off the API, not chosen here.
DEFAULT_API_VERSION = "2022-11-28"
API_VERSION_HEADER = "X-GitHub-Api-Version"
SELECTED_VERSION_HEADER = "X-GitHub-Api-Version-Selected"


def selected_api_version(request: Request) -> str | None:
    """The version this request selected, or ``None`` if it pinned one that does not exist."""
    pinned = request.headers.get(API_VERSION_HEADER)
    if pinned is None:
        return DEFAULT_API_VERSION
    return pinned if pinned in API_VERSIONS else None


def _unsupported_version_error(pinned: str) -> HTTPException:
    """Real's own 400, wording included — a client that matches on the message needs the real text.

    Composed from ``API_VERSIONS`` rather than pasted, so the sentence cannot fall out of step with
    the list it describes. The "X (most recent) and Y" phrasing is real's for the two versions it
    supports today; a third would need real's phrasing for three, not a guess at it.
    """
    newest, *rest = API_VERSIONS
    supported = ", ".join(f'"{v}"' for v in rest)
    exc = HTTPException(status_code=400, detail="Bad Request")
    # The github envelope, carried on the exception the way backlot.errors.google carries its extra
    # fields; backlot.errors.github renders it. `errors` is a STRING here, not the usual array.
    exc.github_body = {
        "message": "Bad Request",
        "errors": (
            f'The version you specified in the "X-GitHub-API-Version" request header, "{pinned}", '
            f"is not a supported version. The following versions are currently supported: "
            f'"{newest}" (most recent) and {supported}.'
        ),
        "documentation_url": "https://docs.github.com/rest",
        "status": "400",
    }
    return exc


def _version(request: Request) -> str:
    """The API version to build this response for. Never ``None``: ``_validate_api_version`` is a
    router-wide dependency, so an unsupported one never reaches a handler."""
    return selected_api_version(request) or DEFAULT_API_VERSION


async def _validate_api_version(request: Request) -> None:
    """400 a pinned version that does not exist — ahead of the credential and the owner check.

    Ordering is real's, and it is verified rather than assumed: api.github.com 400s a bad version on
    a repo that does not exist while sending no credentials at all. It matters to the caller — a
    version typo reported as 401 sends them to their token, and as 404 to their path, when the header
    is what is wrong. Declared before ``_validate_path_owner`` in the router's dependency list, which
    is what puts it first.
    """
    if selected_api_version(request) is None:
        # `None` is only reachable with the header present, so this read cannot miss.
        raise _unsupported_version_error(request.headers[API_VERSION_HEADER])


async def _validate_path_owner(request: Request) -> None:
    """404 a request whose ``{owner}``/``{org}`` segment is not the org we serve.

    Real GitHub 404s a wrong owner; echoing whatever was asked for back into the response lets a
    client's owner-handling bug pass against Backlot and fail in production. A router-wide
    dependency rather than a call in each handler so a route added later cannot forget it — routes
    with neither path param (``/search/issues``, ``/user/repos``) are unaffected. Credentials are
    checked first, so a bad token still reports 401 rather than the owner's 404.
    """
    _require(request)
    owner = request.path_params.get("owner") or request.path_params.get("org")
    if owner is not None and owner.lower() != _org(request).lower():
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(
    prefix="/github",
    tags=["github"],
    # Order is the answering order: an unsupported API version is a malformed request and real
    # refuses it before authenticating or routing, so it is declared first.
    dependencies=[Depends(_validate_api_version), Depends(_validate_path_owner)],
)


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
        f"application/vnd.github.{v}{name}{suffix}"
        for v in ("", "v3.")  # the legacy version segment GitHub's own docs used
        for suffix in ("", "+json")
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


class GitHubCodeHit(_Loose):
    name: str
    path: str
    sha: str
    url: str
    git_url: str
    html_url: str
    repository: dict


class GitHubCodeSearch(_Loose):
    total_count: int
    incomplete_results: bool
    items: list[GitHubCodeHit]


def _base_url(request: Request) -> str:
    host = request.headers.get("host", "localhost")
    return f"{request.url.scheme}://{host}{request.url.path}"


def _api_base(request: Request) -> str:
    """Backlot's GitHub API root (…/github), used for resource `url` fields so SDK
    clients (e.g. PyGithub) that lazily complete objects fetch back from Backlot."""
    host = request.headers.get("host", "localhost")
    return f"{request.url.scheme}://{host}/github"


def _paged(
    request: Request, rows_total: int, extra: dict, body: list, page: int, per_page: int
) -> Response:
    link = github_link_header(_base_url(request), extra, page, per_page, rows_total)
    headers = {"Link": link} if link else {}
    return JSONResponse(body, headers=headers)


def _echo(request: Request, **params) -> dict:
    """The FILTERS a next-page url spells out: the ones the caller sent, in `_paged`'s shape.

    Real echoes a listing's filter only when the request carried it — `/pulls?per_page=1` links
    `per_page` and `page` alone where `?state=open&per_page=1` links `state=open` too, and an empty
    `?protected=` is echoed as well (measured on psf/requests and fastapi/fastapi). A default the
    handler applied is not one the caller asked for: echoing `state`'s `open` narrows the url a
    paginator follows, dropping the rows a `state=all` walk asked for.

    Filters only. `per_page` is `github_link_header`'s to write, and it writes the effective size
    whether or not the caller named one, where real omits it for a caller who did not (#126).
    """
    return {k: v for k, v in params.items() if k in request.query_params}


_GH_OP = re.compile(r'(\w+):("[^"]*"|\S+)')
# The qualifiers each search endpoint honors. They differ because the resources do: an issue has a
# state and an author, a file has a path and an extension. A key absent from the set stays as free
# text, which is also what real does with a qualifier it does not know.
_GH_ISSUE_QUALS = {"repo", "is", "state", "type", "label", "author", "in", "org", "user"}
_GH_CODE_QUALS = {"repo", "path", "filename", "extension", "in"}


def _parse_q(q: str, keys: set[str]) -> tuple[str, dict]:
    """Split a GitHub search `q` into (free_text, qualifiers), honoring the keys in `keys`.
    Everything else is free text, matched full-text."""
    quals: dict[str, list[str]] = {}

    def _take(m):
        key = m.group(1).lower()
        if key in keys:
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
        if v in ("open", "closed") and row["state"] != v:
            return False
        if v == "merged" and not row["merged_at"]:
            return False
    for v in quals.get("state", []):
        if row["state"] != v.lower():
            return False
    for v in quals.get("label", []):
        if v.lower() not in [x.lower() for x in store.jcol(row, "labels")]:
            return False
    for v in quals.get("author", []):
        login = synth.github_login(row["author_email"]).lower()
        if v.lower() != login and v.lower() not in (row["author_email"] or "").lower():
            return False
    return True


def _search_paged(
    request: Request, response: Response, q: str, page: int, per_page: int, total: int
) -> None:
    """Carry the RFC5988 `Link` real sends on a search onto ``response``.

    A search envelope reports `total_count`, so the header is not the only way to learn there is
    more — it is how a client that FOLLOWS links pages without composing a URL of its own, which is
    what :func:`_paged` already gives every listing on this router. Set on the injected response
    rather than by returning a ``JSONResponse``, so the handler keeps its ``response_model`` and the
    operation keeps the typed schema the MCP bridge reads.
    """
    link = github_link_header(_base_url(request), {"q": q}, page, per_page, total)
    if link:
        response.headers["Link"] = link


@router.get("/search/issues", response_model=GitHubIssueSearch)
async def search_issues(
    request: Request,
    response: Response,
    q: str = Query("", description="Issues/PRs search query"),
    page: PageParam = None,
    per_page: PageParam = None,
):
    """Issues-and-PRs search (GitHub `GET /search/issues`): free text over title+body (FTS)
    plus repo:/is:/state:/type:/label:/author: qualifiers, ACL-scoped to the caller.

    A blank `q` is real's 422, the same envelope `/search/code` answers with. This used to be a
    listing of everything the caller can see, which is the expensive kind of divergence: a client
    that forgot its query got a plausible, ACL-scoped result set here and a hard 422 in production
    (measured against api.github.com — `GET /search/issues` with no `q` is `Validation Failed`, field `q`,
    code `missing`)."""
    caller = _require(request)
    if not q.strip():
        raise _search_validation_failed("q")
    conn = auth.conn(request)
    ids = auth.visible_ids(request, caller)
    free, quals = _parse_q(q, _GH_ISSUE_QUALS)
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
    # _org, not the setting directly: the owner in the URLs these items carry has to be the one the
    # repo routes accept, or every link a client follows out of a search hit 404s
    owner = _org(request)
    items = [
        _issue_obj(conn, owner, r["repo"], r, ab, _version(request))
        for r in matched[start : start + per_page]
    ]
    _search_paged(request, response, q, page, per_page, len(matched))
    return {"total_count": len(matched), "incomplete_results": False, "items": items}


# --- code search ----------------------------------------------------------------


def _code_path_match(path: str, value: str) -> bool:
    """`path:` — the value's segments as a contiguous run of WHOLE segments of `path`, anchored at
    the root when the value is (`path:/src`).

    Whole segments rather than a substring: `path:pkg` names a directory, and answering it with
    `mypkg/x.py` is the kind of hit that makes a result set untrustworthy. The run may reach the
    filename, so `path:src/pkg/utils.py` matches the one file it spells out.

    A bare `path:/` names the root itself and selects the files directly in it, which is real's own
    reading — matching everything there would make the qualifier a no-op at its most specific.
    """
    want = [s.lower() for s in value.split("/") if s]
    parts = [s.lower() for s in path.split("/")]
    if not want:
        return len(parts) == 1 if value.startswith("/") else True
    starts = [0] if value.startswith("/") else range(len(parts) - len(want) + 1)
    return any(parts[i : i + len(want)] == want for i in starts)


def _code_filename_match(path: str, value: str) -> bool:
    """`filename:` — the file's name, with or without its extension, so `filename:utils` and
    `filename:utils.py` both find `src/pkg/utils.py`."""
    name = path.rsplit("/", 1)[-1].lower()
    return value.lower() in (name, name.rsplit(".", 1)[0])


def _code_extension_match(path: str, value: str) -> bool:
    """`extension:` — the path's last extension. A leading dot is accepted; real's own docs spell
    the qualifier both ways."""
    return path.lower().endswith("." + value.lower().lstrip("."))


# A quoted run, or a bare run of non-space. Same quoting `_GH_OP` already honours on a qualifier's
# value, applied to what is left over as free text.
_GH_TERM = re.compile(r'"([^"]*)"|(\S+)')


def _code_terms(free: str) -> list[str]:
    """The free text as the terms a PATH and a `text_matches` fragment are searched for.

    Quotes group a term rather than being part of it: `"svc/other"` is how a caller spells one
    term containing a space, and hunting the path for a literal `"` finds nothing — so a quoted
    query answered zero where its bare form answered a file. FTS never saw the problem, since
    :func:`store._fts_match` tokenizes on word characters and drops the quotes on its own.

    Deduplicated, because a term the query repeats is still one occurrence of it in the file and
    reporting the same span twice would have a client highlight it twice.
    """
    terms = ((quoted or bare) for quoted, bare in _GH_TERM.findall(free))
    return list(dict.fromkeys(t for t in terms if t))


def _code_in_targets(quals: dict) -> set[str]:
    """Where `in:` says the free text has to match. Real searches the content AND the path when the
    qualifier is absent, and takes a comma-joined list (`in:file,path`).

    A value naming neither falls back to both rather than to nothing: `in:` is a NARROWING, and one
    the endpoint cannot honour is better answered too widely than with a silent zero.
    """
    named = {v.strip().lower() for val in quals.get("in", []) for v in val.split(",")}
    return (named & {"file", "path"}) or {"file", "path"}


def _code_repos(conn, quals: dict, org: str) -> set[str] | None:
    """The repos a `repo:` qualifier names, or ``None`` when it names none. Several of them OR, as
    on real.

    An EMPTY set is a restriction nothing satisfies, and that is the point: a `repo:` naming a repo
    Backlot does not serve — or one under another owner, which `_validate_path_owner` 404s
    everywhere else — has to match nothing rather than quietly widening back to the whole corpus and
    answering with files from a repo the caller did not ask about.
    """
    if "repo" not in quals:
        return None
    names = set()
    for v in quals["repo"]:
        owner, _, name = v.rpartition("/")
        if owner and owner.lower() != org.lower():
            continue
        if store.get_container(conn, "github", name) is not None:
            names.add(name)
    return names


# Real's fragment is a couple of hundred characters of the file around the match.
_TEXT_MATCH_CHARS = 200


def _text_matches(content: str, object_url: str, terms: list[str]) -> list[dict]:
    """Real's `text_matches`: one fragment of the file's content, with the indices of each search
    term inside it (`indices` are into the FRAGMENT, not the file).

    `property` is always `content` and `matches` may be EMPTY — real's own answer for a hit matched
    on its path, read off api.github.com rather than assumed. The fragment spans whole lines, so a
    code hit arrives readable.

    `terms` comes from :func:`_code_terms` — quoting grouped, duplicates dropped.
    """
    lowered = content.lower()
    found = [i for t in terms if (i := lowered.find(t.lower())) >= 0]
    start = content.rfind("\n", 0, max(0, min(found, default=0) - 60)) + 1
    end = content.find("\n", start + _TEXT_MATCH_CHARS)
    fragment = content[start : end + 1 if end >= 0 else len(content)]
    low = fragment.lower()
    matches = []
    for t in terms:
        at = low.find(t.lower())
        while at >= 0:
            matches.append({"text": fragment[at : at + len(t)], "indices": [at, at + len(t)]})
            at = low.find(t.lower(), at + 1)
    matches.sort(key=lambda m: m["indices"])
    return [
        {
            "object_url": object_url,
            "object_type": "FileContent",
            "property": "content",
            "fragment": fragment,
            "matches": matches,
        }
    ]


def _code_hit(conn, owner: str, row, api_base: str) -> dict:
    """One `/search/code` result — real's field set, which carries no body and no `_links`: a hit
    LOCATES a file, and `url` is where its content is then fetched from.

    Real's `url`/`git_url` name `/repositories/{id}/…` and pin `?ref=` to the commit it indexed.
    Backlot serves no `/repositories/{id}` route, so the links take the `/repos/{owner}/{repo}/…`
    form it does serve — a link the caller can follow beats one that matches real's spelling and
    404s, which is the rule :func:`_repo_obj` states for its url templates. The snapshot pin is
    kept, as the HEAD row's own `ref`, so following `url` returns the bytes that were searched.
    """
    repo, path, content = row["repo"], row["path"], row["content"]
    sha = _blob_sha(content)
    ref = row["ref"]
    rev = quote(ref, safe="") if ref else "main"
    return {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "sha": sha,
        "url": f"{api_base}/repos/{owner}/{repo}/contents/{path}" + (f"?ref={rev}" if ref else ""),
        "git_url": f"{api_base}/repos/{owner}/{repo}/git/blobs/{sha}",
        "html_url": f"https://github.com/{owner}/{repo}/blob/{rev}/{path}",
        "repository": _repo_obj(conn, owner, repo, api_base),
        # Real reports a flat 1.0 for every hit: its index exposes no per-hit score and the ORDER
        # carries the relevance. A bm25 value here would be a number real never sends.
        "score": 1.0,
    }


def _search_validation_failed(field: str) -> HTTPException:
    """Real's 422 for a search missing a required parameter, in its own envelope — here `errors` is
    the usual array, unlike the version 400's string (see :func:`_unsupported_version_error`)."""
    exc = HTTPException(status_code=422, detail="Validation Failed")
    exc.github_body = {
        "message": "Validation Failed",
        "documentation_url": "https://docs.github.com/v3/search",
        "errors": [{"resource": "Search", "field": field, "code": "missing"}],
        "status": "422",
    }
    return exc


@router.get("/search/code", response_model=GitHubCodeSearch)
async def search_code(
    request: Request,
    response: Response,
    q: str = Query(
        "",
        description=(
            "Required. Free text matched against a file's body and its path, plus "
            "repo:/path:/filename:/extension:/in:file/in:path qualifiers."
        ),
    ),
    page: PageParam = None,
    per_page: PageParam = None,
):
    """Code search (GitHub `GET /search/code`): free text over a file's body and its path, plus
    repo:/path:/filename:/extension:/in: qualifiers, ACL-scoped to the caller.

    ONE RESULT PER (repo, path) — the HEAD snapshot's. Real code search indexes the DEFAULT BRANCH
    only, while Backlot stores a row per snapshot of a path (see ``store._file_head_clause``), so
    without that restriction a path would answer once per revision it was ever recorded at and one
    repo's history would crowd the rest of the corpus out of the result set. The cost is deliberate,
    and it is real's cost too: a string surviving only in a SUPERSEDED snapshot is not findable
    here. It stays reachable by path at `/contents/{path}?ref=` and by digest at `/git/blobs/{sha}`.

    `Accept: application/vnd.github.text-match+json` adds `text_matches` — the part that makes a hit
    useful rather than merely located.

    A blank `q` is real's 422 rather than a listing: a code search with no term is a client bug
    better reported than answered with a corpus dump. `/search/issues` above answers a blank `q`
    the same way, and for the same measured reason.
    """
    caller = _require(request)
    if not q.strip():
        raise _search_validation_failed("q")
    conn = auth.conn(request)
    ids = auth.visible_ids(request, caller)
    free, quals = _parse_q(q, _GH_CODE_QUALS)
    org = _org(request)
    repos = _code_repos(conn, quals, org)
    # A `repo:` that resolved to nothing: no row can satisfy it, so there is nothing to read.
    if repos is not None and not repos:
        return {"total_count": 0, "incomplete_results": False, "items": []}
    one = next(iter(repos)) if repos and len(repos) == 1 else None
    targets = _code_in_targets(quals)
    terms = _code_terms(free)

    # Keyed by the address a file HAS, so the content search and the path search union rather than
    # double-count a file both of them found. Insertion order is the result order: FTS relevance
    # first, then the paths that matched on their name alone.
    rows: dict = {}
    if free and "file" in targets:
        for r in store.search_repo_files(conn, free, ids, repo=one):
            rows.setdefault((r["repo"], r["path"]), r)
    if free and "path" in targets:
        for r in store.search_repo_files(conn, None, ids, repo=one, path_like=terms):
            rows.setdefault((r["repo"], r["path"]), r)
    if not free:  # qualifier-only, which real also serves
        for r in store.search_repo_files(conn, None, ids, repo=one):
            rows.setdefault((r["repo"], r["path"]), r)

    matched = [
        r
        for r in rows.values()
        if (repos is None or r["repo"] in repos)
        and all(_code_path_match(r["path"], v) for v in quals.get("path", []))
        and all(_code_filename_match(r["path"], v) for v in quals.get("filename", []))
        and all(_code_extension_match(r["path"], v) for v in quals.get("extension", []))
    ]
    page, per_page = clamp_page(
        page, per_page, get_settings().default_page_size, get_settings().max_page_size
    )
    start = (page - 1) * per_page
    ab = _api_base(request)
    want_matches = _github_media(request, "text-match")
    items = []
    for row in matched[start : start + per_page]:
        hit = _code_hit(conn, org, row, ab)
        if want_matches:
            hit["text_matches"] = _text_matches(row["content"], hit["url"], terms)
        items.append(hit)
    _search_paged(request, response, q, page, per_page, len(matched))
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


def _repo_visible(conn, repo: str, ids) -> bool:
    """Whether the caller can see this repo at all. One with no visible document is not visible.

    ``store.get_container`` alone answers "does this repo exist", which is a different question: a
    repo every one of whose documents is hidden from a caller is one they must not be able to
    confirm the existence of. Every repo route resolves it through here so the answer cannot drift
    between them, and it is the same predicate :func:`_visible_repos` filters the listing by.
    """
    if store.get_container(conn, "github", repo) is None:
        return False
    return ids is None or store.count_documents(conn, "github", repo, ids) > 0


def _require_repo(conn, repo: str, ids) -> None:
    """404 unless the caller can see the repo (see :func:`_repo_visible`)."""
    if not _repo_visible(conn, repo, ids):
        raise HTTPException(status_code=404, detail="Not Found")


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
    page: PageParam = None,
    per_page: PageParam = None,
):
    conn = auth.conn(request)
    caller = _require(request)
    return _repo_page(request, conn, org, auth.visible_ids(request, caller), page, per_page)


@router.get("/user/repos")
async def list_user_repos(
    request: Request,
    page: PageParam = None,
    per_page: PageParam = None,
):
    """The repositories the CREDENTIAL can reach (real ``GET /user/repos``).

    ``/orgs/{org}/repos`` answers a different question and is not a substitute: a real fine-grained
    token may span several orgs or cover only personal repos, so an org listing is not the token's
    view of the world. This is the endpoint a credential uses to discover its own reach, and
    without it a client has to be configured with an explicit repo name per mount.

    Real GitHub also takes ``visibility``/``affiliation``/``type``/``sort``; Backlot serves a
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
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    _require_repo(conn, repo, ids)
    return _repo_obj(conn, owner, repo, _api_base(request))


@router.get("/repos/{owner}/{repo}/issues", response_model=list[GitHubIssue])
async def list_issues(
    owner: str,
    repo: str,
    request: Request,
    state: str = Query("open"),
    page: PageParam = None,
    per_page: PageParam = None,
):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    _require_repo(conn, repo, ids)
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
    body = [_issue_obj(conn, owner, repo, r, ab, _version(request)) for r in rows]
    return _paged(request, len(all_rows), _echo(request, state=state), body, page, per_page)


# --- a comment by its own id ----------------------------------------------------
#
# Both routes MUST be declared ahead of `…/issues/{number}/comments` and `…/pulls/{number}/comments`:
# FastAPI matches in declaration order, and the literal `comments` would otherwise be parsed as the
# `{number}` path param and rejected as a non-integer.


def _comment_by_id(request, conn, repo: str, cid: int, ids, *, anchored: bool):
    """Resolve a served comment id back to (comment, document), or raise 404.

    404 rather than 403 for every failure — a comment the caller cannot read, one belonging to
    another repo, and one of the other kind all have to be indistinguishable from a comment that
    does not exist, or the response confirms what it is refusing to serve.
    """
    row = store.get_github_comment(conn, cid)
    if row is None or (row["path"] is not None) != anchored:
        raise HTTPException(status_code=404, detail="Not Found")
    doc = store.get_document(conn, "github", row["repo"], row["number"], visible_ids=ids)
    if doc is None or doc["repo"] != repo:
        raise HTTPException(status_code=404, detail="Not Found")
    return row, doc


@router.get("/repos/{owner}/{repo}/issues/comments/{comment_id}")
async def get_issue_comment(owner: str, repo: str, comment_id: int, request: Request):
    """One conversation comment, the resource its own `url` points at."""
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row, doc = _comment_by_id(request, conn, repo, comment_id, ids, anchored=False)
    number = _issue_number(doc)
    return _gh_comment(owner, repo, number, row, _api_base(request))


@router.get("/repos/{owner}/{repo}/pulls/comments/{comment_id}")
async def get_pull_review_comment(owner: str, repo: str, comment_id: int, request: Request):
    """One line-anchored review comment. The file it is anchored to has to be readable by this
    caller too — the collection drops such a comment, so serving it by id would be a way around
    that."""
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row, doc = _comment_by_id(request, conn, repo, comment_id, ids, anchored=True)
    src = _RepoFiles(conn, repo, ids)
    f = src.get(row["path"])
    if f is None:
        raise HTTPException(status_code=404, detail="Not Found")
    ab = _api_base(request)
    number = _issue_number(doc)
    patches = {
        x["filename"]: x.get("patch") for x in _pr_files(conn, owner, repo, doc, ab, ids, src)
    }
    return _gh_review_comment(owner, repo, number, doc, row, f, patches, ab)


@router.get("/repos/{owner}/{repo}/issues/{number}", response_model=GitHubIssue)
async def get_issue(owner: str, repo: str, number: int, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _resolve(conn, repo, number, ids)
    if row is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return _issue_obj(conn, owner, repo, row, _api_base(request), _version(request))


@router.get("/repos/{owner}/{repo}/issues/{number}/comments")
async def issue_comments(owner: str, repo: str, number: int, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _resolve(conn, repo, number, ids)
    if row is None:
        raise HTTPException(status_code=404, detail="Not Found")
    ab = _api_base(request)
    return [
        _gh_comment(owner, repo, number, c, ab)
        # anchored=False: a line-anchored review comment belongs to /pulls/{n}/comments, and
        # serving it here too would duplicate it under a resource that means something else
        for c in store.github_comments(conn, row["repo"], row["number"], anchored=False)
    ]


@router.get("/repos/{owner}/{repo}/pulls")
async def list_pulls(
    owner: str,
    repo: str,
    request: Request,
    state: str = Query("open"),
    page: PageParam = None,
    per_page: PageParam = None,
):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    _require_repo(conn, repo, ids)
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
    # one _RepoFiles for the whole page: every PR's changeset reads through it (see _pr_files)
    repo_files = _RepoFiles(conn, repo, ids)
    body = [
        _pr_obj(conn, owner, repo, r, ab, ids=ids, repo_files=repo_files, version=_version(request))
        for r in prs[start : start + per_page]
    ]
    return _paged(request, len(prs), _echo(request, state=state), body, page, per_page)


@router.get("/repos/{owner}/{repo}/pulls/{number}")
async def get_pull(owner: str, repo: str, number: int, request: Request):
    """The pull as JSON, or as its diff when `Accept` asks for one.

    ``application/vnd.github.diff`` and ``…patch`` are representations of this resource, not
    separate endpoints, so ignoring them meant a caller that piped the result to `git apply` or a
    diff viewer got the pull's JSON with a 200 and no way to notice."""
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _resolve(conn, repo, number, ids)
    if row is None or row["kind"] != "pull_request":
        raise HTTPException(status_code=404, detail="Not Found")
    ab = _api_base(request)
    wants_diff, wants_patch = _github_media(request, "diff"), _github_media(request, "patch")
    if not (wants_diff or wants_patch):
        return _pr_obj(conn, owner, repo, row, ab, ids=ids, version=_version(request))
    files = _pr_files(conn, owner, repo, row, ab, ids)
    obj = _pr_obj(conn, owner, repo, row, ab, ids=ids, files=files, version=_version(request))
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
    row = _resolve(conn, repo, number, ids)
    if row is None:
        raise HTTPException(status_code=404, detail="Not Found")
    ab = _api_base(request)
    number = _issue_number(row)
    sha = hashlib.sha1(_seed(row).encode()).hexdigest()[:40]
    out = []
    for i, rv in enumerate(store.jcol(row, "reviews"), start=1):
        rid = synth.github_number(_seed(row) + str(i))
        pr_url = f"{ab}/repos/{owner}/{repo}/pulls/{number}"
        out.append(
            {
                "id": rid,
                "node_id": synth.node_id("PullRequestReview", rid),
                "user": _gh_user(rv.get("author_email", "reviewer@x"), ab),
                "body": rv.get("body", ""),
                "state": rv.get("state", "COMMENTED"),
                "submitted_at": synth.rfc3339(synth.epoch(_seed(row)) + i * 60),
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
    """A pull's REVIEW comments — the line-anchored ones. A different resource from both
    ``/issues/{n}/comments`` (the conversation) and ``/pulls/{n}/reviews`` (approve/request-changes
    events): a corpus marks a comment as this one by giving it a ``path``.

    A pull with none answers ``[]``, which is also what real GitHub does — and what this returned for
    every pull before ``github_comments`` could hold the anchoring. A 404 (the behaviour before that)
    aborts any client that renders a pull from its metadata, conversation, review comments and files.
    """
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _resolve(conn, repo, number, ids)
    if row is None or row["kind"] != "pull_request":
        raise HTTPException(status_code=404, detail="Not Found")
    src = _RepoFiles(conn, repo, ids)
    resolved = _resolved_review_comments(conn, row, src)
    if not resolved:
        return []
    ab = _api_base(request)
    # The same changeset this pull's diff serves, so a comment's `diff_hunk` and the diff agree.
    patches = {
        f["filename"]: f.get("patch") for f in _pr_files(conn, owner, repo, row, ab, ids, src)
    }
    return [_gh_review_comment(owner, repo, number, row, c, f, patches, ab) for c, f in resolved]


@router.get("/repos/{owner}/{repo}/pulls/{number}/commits")
async def pull_commits(owner: str, repo: str, number: int, request: Request):
    """The pull's commits — one, the head, which is what the pull object already claims.

    Here because ``_links.commits``/``commits_url`` name it: a field whose whole purpose is to be
    followed must lead somewhere, and a 404 at the end of an advertised link is a worse answer for
    the caller than no link at all. Nothing is invented — the sha, the message and the author are the
    pull's own, and Backlot keeps no history to draw a second commit from (see ``get_tree``).
    """
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _resolve(conn, repo, number, ids)
    if row is None or row["kind"] != "pull_request":
        raise HTTPException(status_code=404, detail="Not Found")
    ab = _api_base(request)
    sha = hashlib.sha1(_seed(row).encode()).hexdigest()
    author = _gh_user(row["author_email"], ab)
    created = synth.rfc3339(row["created_ts"] or synth.epoch(_seed(row)))
    # `commit.author` is a GIT author — name/email/date — and not the GitHub user beside it. Real
    # serves both because they are different things: one signed the commit, one has an account.
    git_author = {"name": author["login"], "email": row["author_email"], "date": created}
    tree_sha = _repo_tree_sha(repo)
    return [
        {
            "sha": sha,
            "node_id": synth.node_id("Commit", sha[:12]),
            "commit": {
                "author": git_author,
                "committer": git_author,
                "message": row["title"],
                "tree": {"sha": tree_sha, "url": f"{ab}/repos/{owner}/{repo}/git/trees/{tree_sha}"},
                "url": f"{ab}/repos/{owner}/{repo}/git/commits/{sha}",
                "comment_count": 0,
            },
            "url": f"{ab}/repos/{owner}/{repo}/commits/{sha}",
            "html_url": f"https://github.com/{owner}/{repo}/commit/{sha}",
            "author": author,
            "committer": author,
            "parents": [],  # no history is kept, so the head has no parent to name
        }
    ]


@router.get("/repos/{owner}/{repo}/statuses/{sha:path}")
async def commit_statuses(owner: str, repo: str, sha: str, request: Request):
    """Statuses for a commit: always empty, and empty is the honest answer rather than a stub.

    A corpus records no CI, and real GitHub answers `[]` for a sha nobody reported a status on — so
    this is a shape a client will meet in production, not a Backlot-only degenerate case. It exists
    because a pull's ``statuses_url`` and ``_links.statuses`` name it.

    Empty is the answer for a ref that EXISTS. One that names nothing is a 404, as on real, where
    `/statuses/main` answers `[]` and `/statuses/totally-made-up` 404s (measured on psf/requests) —
    the same question :func:`_commit_ish` answers for `/commits` and `git/trees`. The ref is the
    trailing path for the same reason it is there: `/statuses/bug/5671` resolves on real.
    """
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    _require_repo(conn, repo, ids)  # a repo this caller cannot see must not answer for its shas
    if sha.strip("/") not in _commit_ish(conn, owner, repo, ids):
        raise HTTPException(status_code=404, detail="Not Found")
    return []


@router.get("/repos/{owner}/{repo}/pulls/{number}/files")
async def pull_files(
    owner: str,
    repo: str,
    number: int,
    request: Request,
    page: PageParam = None,
    per_page: PageParam = None,
):
    """The pull's changed-file list (``filename``/``status``/``additions``/``deletions``/``patch``),
    paginated as the real API paginates it. See the changeset note below for where it comes from."""
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    row = _resolve(conn, repo, number, ids)
    if row is None or row["kind"] != "pull_request":
        raise HTTPException(status_code=404, detail="Not Found")
    files = _json_file_objects(_pr_files(conn, owner, repo, row, _api_base(request), ids))
    page, per_page = clamp_page(
        page, per_page, get_settings().default_page_size, get_settings().max_page_size
    )
    start = (page - 1) * per_page
    return _paged(request, len(files), {}, files[start : start + per_page], page, per_page)


@router.get("/repos/{owner}/{repo}/git/ref/{ref:path}")
async def get_git_ref(owner: str, repo: str, ref: str, request: Request):
    """Resolve a ref (``heads/main``, ``heads/release/2026-03``, ``tags/v1``) to a commit.

    The ref is the trailing PATH, which is the whole reason a client reaches for this instead of
    ``/branches/{branch}``: a branch name containing a slash does not fit in one path segment, so
    without this route such a branch cannot be pinned to a commit at all.

    A ref that exists resolves to the repo's snapshot commit, since Backlot keeps no commit
    history (see :func:`get_tree`). WHICH refs exist is knowable, and real 404s the rest:

    - ``heads/{name}`` for a name :func:`_branch_names` holds. `heads/totally-made-up` is a 404 on
      psf/requests, and answering it here made "which branches does this repo have" resolve one
      way through ``/branches`` and another through this route.
    - ``pull/{n}/head`` for a pull that exists, and ``pull/{n}/merge`` only while it is still
      OPEN. Real drops the merge ref when the pull closes: on psf/requests, #7616 (merged) and
      #7589 (closed, unmerged) answer 404 on ``…/merge`` and 200 on ``…/head``, where open #7586
      answers 200 on both. A number that names no pull is a 404 either way (#999999 on
      pydantic/pydantic). Restricting this route to branches would 404 a ref real resolves.
    - ``tags/{name}`` for a tag the repo states. One it does not state has nothing to resolve to,
      which is the same answer ``/tags`` gives for it.
    - Nothing else — ``refs/heads/main``, the fully-qualified git spelling, included. Real takes
      ``heads/main`` here and 404s the qualified form with the get-a-reference endpoint's own body
      (measured on psf/requests), so it is a missing ref rather than a missing route. This used to
      be stripped and accepted, which let a client sending the git spelling pass here and 404 in
      production. The ref this route ANSWERS with is still fully qualified, as real's is.
    """
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    _require_repo(conn, repo, ids)
    ref = ref.strip("/")
    if not _ref_exists(conn, owner, repo, ref, ids):
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


def _ref_exists(conn, owner: str, repo: str, ref: str, ids) -> bool:
    """Whether `ref` — already stripped of any `refs/` prefix — names something in this repo. The
    namespaces are :func:`get_git_ref`'s; a pull's number is checked against the pulls the CALLER
    can see, so a ref is not a way to learn a restricted pull exists."""
    head, _, name = ref.partition("/")
    if head == "heads":
        return bool(name) and name in _branch_names(conn, owner, repo, ids)
    if head == "tags":
        return bool(name) and name in _repo_tags(conn, repo)
    if head == "pull":
        number, _, kind = name.partition("/")
        if kind not in ("head", "merge") or not number.isdigit():
            return False
        row = store.github_by_number(conn, repo, int(number), ids)
        if row is None or row["kind"] != "pull_request":
            return False
        # A merge ref is git's preview of the pull applied to its base, and it stops existing when
        # there is nothing left to merge — so it is the pull's state, not its existence, that
        # decides. `state` is COALESCEd because a corpus that states none means open.
        open_pull = (row["state"] or "open") == "open" and not row["merged_at"]
        return kind == "head" or open_pull
    return False


@router.get("/repos/{owner}/{repo}/git/trees/{ref:path}")
async def get_tree(
    owner: str, repo: str, ref: str, request: Request, recursive: str | None = Query(None)
):
    """The repo's file set as a git tree (real API shape). `recursive` (any truthy value,
    GitHub-style) returns every blob/tree entry; otherwise only the entries directly under root.

    `ref` selects WHICH tree, exactly as on real GitHub: a SUBTREE's own sha — the one a client
    reads out of a parent listing's `tree` entry — answers that directory's entries, with paths
    relative to it. That is how a client walks a repo one level at a time (fsspec's
    `GithubFileSystem` does), and answering the root instead does not fail loudly: it reports the
    root's entries under the child's name, so listing `src` yields `src/src` and `src/config` and a
    recursive walk descends until it runs out of stack.

    Everything else `ref` may be is a commit-ish resolving to the ROOT tree — a branch name, or a
    sha this server hands out as a commit (see :func:`_commit_ish`). A ref that is none of those is
    a 404, as on real, where both `totally-made-up` and a 40-hex sha naming no object answer 404
    (measured on psf/requests). This route used to answer the root for any of them, which made a
    name no branch listing held resolve to a tree.

    A subtree sha resolves PER CALLER, because the entries it is matched against are already
    `visible_ids`-scoped: a token that can see nothing inside a folder does not find that folder's
    sha, and gets a 404 where a wider token gets the folder. No content crosses the ACL.

    **A tree keeps no history**, so every other `ref` — a branch name, or a sha from /branches,
    /commits or git/ref — resolves to the repo's CURRENT root. FILES themselves do have history: a
    corpus may state several snapshots of one path, and `/contents/{path}?ref=` reaches them (see
    :func:`get_contents`). What has no per-ref shape is the tree — there is no mapping from a ref to
    the set of snapshots that were current at it — so this route answers HEAD for every ref, and a
    path appears exactly once however many snapshots it holds. Two consequences a client author will
    otherwise assume away:

    - Pinning to a commit sha gives no immutability guarantee. A blob sha is content-addressed and
      stable, but the tree a commit sha resolves to moves whenever the corpus changes, so caching a
      snapshot against a commit sha caches a moving target.
    - Because there is no before/after state, a pull's changeset is synthesized out of this same
      snapshot rather than diffed from it (see :func:`_pr_files`) — which is also why a file the
      changeset reports as `removed` is still present here.

    The ref is the trailing PATH, as on `git/ref`: a branch name may contain a slash and real
    resolves one here (`git/trees/bug/5671` on psf/requests answers that branch's tree).

    `truncated` follows the real caps (:data:`TREE_MAX_ENTRIES` / :data:`TREE_MAX_BYTES`)."""
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    _require_repo(conn, repo, ids)
    ref = ref.strip("/")
    ab = _api_base(request)
    rows = store.list_repo_files(conn, repo, ids)
    entries = _tree_from_paths(owner, repo, rows, ab)
    subtree = _subtree_path(repo, ref, entries)
    if (
        subtree is None
        and ref != _repo_tree_sha(repo)
        and ref not in _commit_ish(conn, owner, repo, ids)
    ):
        raise HTTPException(status_code=404, detail="Not Found")
    if subtree is not None:
        prefix = subtree + "/"
        entries = [
            {**e, "path": e["path"][len(prefix) :]} for e in entries if e["path"].startswith(prefix)
        ]
    if not _truthy(recursive):
        entries = [e for e in entries if "/" not in e["path"]]
    entries, truncated = _cap_tree(entries)
    tree_sha = _repo_tree_sha(repo) if subtree is None else _dir_sha(repo, subtree)
    return {
        "sha": tree_sha,
        "url": f"{ab}/repos/{owner}/{repo}/git/trees/{tree_sha}",
        "tree": entries,
        "truncated": truncated,
    }


def _subtree_path(repo: str, ref: str, entries: list[dict]) -> str | None:
    """The directory `ref` names, if `ref` is one of this repo's subtree shas — else None.

    Reversed off the directory entries already built rather than stored: `_dir_sha` is a pure
    function of (repo, path), so the shas handed out in a listing are exactly the ones recomputed
    here. A repo with no directories can only ever answer None, which is the root.
    """
    for entry in entries:
        if entry["type"] == "tree" and _dir_sha(repo, entry["path"]) == ref:
            return entry["path"]
    return None


def _cap_tree(entries: list[dict]) -> tuple[list[dict], bool]:
    """Apply the real API's tree caps, returning ``(entries, truncated)``.

    Real GitHub caps a recursive tree at 100k entries / 7 MB and sets `truncated: true`; a server
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


async def _contents_response(
    owner: str, repo: str, path: str, request: Request, ref: str | None = None
):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    _require_repo(conn, repo, ids)
    _require_ref(conn, owner, repo, ref, ids)
    ab = _api_base(request)
    path = path.strip("/")
    if path:
        row = store.get_repo_file(conn, repo, path, ids, ref=ref)
        if row is not None:
            return _raw_response(request, row["content"], _CONTENT_RAW_TYPE) or _file_obj(
                owner, repo, row, ab, ref
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


def _require_ref(conn, owner: str, repo: str, ref: str | None, ids) -> None:
    """404 unless `ref` names something on this repo, which real does for a ref it has no object
    for (`?ref=totally-made-up` on psf/requests).

    Wider than the branch listing by one set: a corpus NAMES the snapshots of a file it states
    (`store.github_file_refs`), and those refs are how an older revision is addressable at all.
    They are refs without being branches, so they resolve here and are absent from `/branches`.

    An EMPTY value is an absent one — real answers `contents/README.md?ref=` with the file — the
    same reading `list_branches` gives an empty `?protected=`.

    The 404 carries real's own body, which is not the generic one every other ref route sends:
    `No commit found for the ref {ref}`, against the contents documentation (measured). A client
    matching on the message needs the real text, the reason :func:`_no_commit_for_sha` exists for
    `/commits`.
    """
    if not ref or ref in _commit_ish(conn, owner, repo, ids):
        return
    if ref not in store.github_file_refs(conn, repo, ids):
        exc = HTTPException(status_code=404, detail="Not Found")
        exc.github_body = {
            "message": f"No commit found for the ref {ref}",
            "documentation_url": "https://docs.github.com/v3/repos/contents/",
            "status": "404",
        }
        raise exc


@router.get("/repos/{owner}/{repo}/contents")
async def get_contents_root(owner: str, repo: str, request: Request, ref: str | None = Query(None)):
    return await _contents_response(owner, repo, "", request, ref)


@router.get("/repos/{owner}/{repo}/contents/{path:path}")
async def get_contents(
    owner: str, repo: str, path: str, request: Request, ref: str | None = Query(None)
):
    """`ref` selects a SNAPSHOT of the file when the corpus named one; see store.get_repo_file for
    why an unnamed ref answers HEAD instead of 404. A directory listing ignores it — the tree has
    no per-ref shape here (the no-history simplification in :func:`get_tree`)."""
    return await _contents_response(owner, repo, path, request, ref)


@router.get("/repos/{owner}/{repo}/git/blobs/{sha}")
async def get_blob(owner: str, repo: str, sha: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    _require_repo(conn, repo, ids)
    # every snapshot, not just HEAD: a blob sha is content-addressed, so a superseded snapshot
    # keeps its own and stays fetchable at it (see store.iter_repo_file_snapshots). Streamed, so a
    # match stops the scan rather than reading the repo's every file first.
    row = next(
        (
            r
            for r in store.iter_repo_file_snapshots(conn, repo, ids)
            if _blob_sha(r["content"]) == sha
        ),
        None,
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


@router.get("/repos/{owner}/{repo}/branches")
async def list_branches(
    owner: str,
    repo: str,
    request: Request,
    protected: str | None = Query(None),
    page: PageParam = None,
    per_page: PageParam = None,
):
    """The repo's branches: the default branch, plus the refs its pulls advertise.

    Which refs those are, and why, is :func:`_branch_names` — including the caller scoping, since
    the listing is built from pulls and pulls are ACL-filtered.

    A listed branch is real's SHORT branch, not the object :func:`get_branch` serves below: on
    api.github.com an item's `commit` carries `sha` and `url` and stops there, where the
    single-branch route nests the whole commit under the same key. Serving the longer object from
    both would hand a client a field real GitHub never sends here.

    `?protected=` selects, so it is honoured rather than ignored: a client that asked for the
    protected branches and got an unprotected one back would read that branch as push-guarded.
    Real has three answers — only protected branches for a true value, only unprotected ones for
    `false`, and all of them when the parameter is omitted — and parses the value the way
    `?recursive=` is parsed, every non-empty value but `false`/`0` reading true. Measured on
    fastapi/fastapi (22 branches, one of them protected): `true`/`1`/`TRUE`/`yes`/`banana` answer
    1, `false`/`0` answer 21, an empty value and an omitted one answer 22.

    All three answers are distinct for a repo whose `subtype: "repo"` record states which branches
    are protected. For one that does not, every branch is unprotected and real's last two coincide
    — a pull cannot imply protection, so an inferred listing has none (see :func:`_branch_rows`).

    `?protected=` selects AHEAD of the page cut, so the `Link` counts the pages of the selection:
    `?protected=false&per_page=1` on fastapi/fastapi answers one of the 21 unprotected branches and
    reports `rel="last"` page 21, not the 22 of the whole listing.
    """
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    _require_repo(conn, repo, ids)
    rows = _branch_rows(conn, owner, repo, ids)
    if protected:  # an EMPTY value selects nothing, as an absent one does: real answers all 22
        rows = [b for b in rows if b["protected"] is _truthy(protected)]
    page, per_page = clamp_page(
        page, per_page, get_settings().default_page_size, get_settings().max_page_size
    )
    start = (page - 1) * per_page
    sha = _repo_commit_sha(repo)
    url = f"{_api_base(request)}/repos/{owner}/{repo}/commits/{sha}"
    body = [
        {"name": b["name"], "commit": {"sha": sha, "url": url}, "protected": b["protected"]}
        for b in rows[start : start + per_page]
    ]
    return _paged(request, len(rows), _echo(request, protected=protected), body, page, per_page)


@router.get("/repos/{owner}/{repo}/tags")
async def list_tags(
    owner: str,
    repo: str,
    request: Request,
    page: PageParam = None,
    per_page: PageParam = None,
):
    """The tags a `subtype: "repo"` record stated, or `[]` for a repo that stated none.

    Empty rather than absent for a repo with no tags. Real GitHub answers `[]` there —
    octocat/Hello-World does — so this is a shape a client meets in production, where a 404 would
    be one it only ever meets here. Same reasoning as `/statuses/{sha}`, empty because Backlot has
    no CI.

    The item is real's, measured on psf/requests: `name`, `commit: {sha, url}`, `zipball_url`,
    `tarball_url` and `node_id`, with the archive urls spelling the ref out in full as
    `refs/tags/{name}`. Every tag resolves to the repo's one snapshot commit, as every branch does
    — Backlot keeps no commit history (see :func:`get_tree`), so a tag cannot point at a different
    one.

    Paged like the rest of the router, and real pages it too: `?per_page=2` there answers two tags
    with a `rel="last"` counting all 161.
    """
    conn = auth.conn(request)
    caller = _require(request)
    _require_repo(conn, repo, auth.visible_ids(request, caller))
    names = _repo_tags(conn, repo)
    page, per_page = clamp_page(
        page, per_page, get_settings().default_page_size, get_settings().max_page_size
    )
    start = (page - 1) * per_page
    ab, sha = _api_base(request), _repo_commit_sha(repo)
    body = [
        {
            "name": name,
            "commit": {"sha": sha, "url": f"{ab}/repos/{owner}/{repo}/commits/{sha}"},
            "zipball_url": f"{ab}/repos/{owner}/{repo}/zipball/refs/tags/{name}",
            "tarball_url": f"{ab}/repos/{owner}/{repo}/tarball/refs/tags/{name}",
            "node_id": synth.node_id("Ref", synth.github_number(f"{repo}:tags/{name}")),
        }
        for name in names[start : start + per_page]
    ]
    return _paged(request, len(names), {}, body, page, per_page)


@router.get("/repos/{owner}/{repo}/branches/{branch:path}")
async def get_branch(owner: str, repo: str, branch: str, request: Request):
    """One branch, if the listing holds it — 404 otherwise, as real answers for a name no branch
    has (measured on psf/requests).

    The name is the trailing PATH because a branch name may contain a slash and real serves it
    whole: `/branches/bug/5671` on psf/requests answers that branch, where a single path segment
    could only 404 it.
    """
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    _require_repo(conn, repo, ids)
    branch = branch.strip("/")
    found = next((b for b in _branch_rows(conn, owner, repo, ids) if b["name"] == branch), None)
    if found is None:
        raise HTTPException(status_code=404, detail="Branch not found")
    ab = _api_base(request)
    commit_sha, tree_sha = _repo_commit_sha(repo), _repo_tree_sha(repo)
    return {
        "name": branch,
        "protected": found["protected"],
        "commit": {
            "sha": commit_sha,
            "commit": {"tree": {"sha": tree_sha}},
            "url": f"{ab}/repos/{owner}/{repo}/commits/{commit_sha}",
        },
    }


@router.get("/repos/{owner}/{repo}/commits/{sha:path}")
async def get_commit(owner: str, repo: str, sha: str, request: Request):
    """One commit, by sha or by a ref standing for one — real takes a branch name here and answers
    that branch's head (`/commits/main` and `git/trees/main` report the same sha on psf/requests),
    a slashed one included, which is why the ref is the trailing path.

    A ref naming no commit is real's 422 rather than a 404 (see :func:`_no_commit_for_sha`). This
    route used to echo any string back as a sha with a 200, so a client that pinned to a name it
    had misspelled read a commit that does not exist.
    """
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    _require_repo(conn, repo, ids)
    sha = sha.strip("/")
    if sha not in _commit_ish(conn, owner, repo, ids):
        raise _no_commit_for_sha(sha)
    # A NAME resolves to the commit it stands for rather than being echoed back as one: real
    # answers `/commits/main` with the branch's own sha, the value `/branches/main` reports under
    # `commit.sha` (both `dae7ef63…` on psf/requests), and carries it in `url` and `node_id` too.
    # Every ref of this repo stands for the one snapshot commit (see :func:`get_tree`), so a
    # branch and a tag resolve alike; a pull's head or base sha arrives already 40-hex and is its
    # own answer.
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        sha = _repo_commit_sha(repo)
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
async def get_readme(owner: str, repo: str, request: Request, ref: str | None = Query(None)):
    """`ref` selects a snapshot, as on `/contents` — real GitHub takes it here too, and both serve
    the same underlying object (see :func:`_file_obj`), so a README the corpus snapshots would
    otherwise be reachable at an older revision through one route and not the other."""
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    _require_repo(conn, repo, ids)
    _require_ref(conn, owner, repo, ref, ids)
    ab = _api_base(request)
    row = store.get_repo_file(conn, repo, "README.md", ids, ref=ref) or store.get_repo_file(
        conn, repo, "readme.md", ids, ref=ref
    )
    if row is not None:
        return _raw_response(request, row["content"], _CONTENT_RAW_TYPE) or _file_obj(
            owner, repo, row, ab, ref
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
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    _require_repo(conn, repo, ids)
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
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    _require_repo(conn, repo, ids)
    c = store.get_container(conn, "github", repo)
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


# The refs a pull advertises when the corpus states neither. `_pr_obj` serves these and
# `_branch_names` lists them: a corpus that names no branches must still not contradict itself.
# The branch is `store`'s, because the importer checks an open pull's base against the same
# fallback and a second copy of it here is a second answer.
_DEFAULT_BRANCH = store.GITHUB_DEFAULT_BRANCH
_UNSTATED_HEAD_REF = "feature"


def _truthy(v: str | None) -> bool:
    """GitHub's `?recursive=` accepts any non-empty, non-'0'/'false' value as true."""
    return v is not None and v.lower() not in ("", "0", "false")


def _blob_sha(content: str) -> str:
    return hashlib.sha1(content.encode()).hexdigest()


def _repo_tree_sha(repo: str) -> str:
    return hashlib.sha1(f"tree:{repo}".encode()).hexdigest()


def _repo_commit_sha(repo: str) -> str:
    return hashlib.sha1(f"commit:{repo}".encode()).hexdigest()


def _default_branch(conn, repo: str) -> str:
    """The branch a repo object reports and every unstated ref falls back to — the corpus's
    `default_branch` if a `subtype: "repo"` record stated one, else `main`."""
    meta = store.github_repo_meta(conn, repo)
    return (meta["default_branch"] if meta else None) or _DEFAULT_BRANCH


def _repo_tags(conn, repo: str) -> list[str]:
    """The tags a `subtype: "repo"` record stated, in the order it stated them. `[]` for a repo
    that stated none, which is also what a repo record's absence means: nothing in a corpus but
    that record can imply a tag."""
    meta = store.github_repo_meta(conn, repo)
    return json.loads(meta["tags"]) if meta and meta["tags"] else []


def _branch_rows(conn, owner: str, repo: str, ids, pulls=None) -> list[dict]:
    """The repo's branches, each with its `protected` flag, ascending by name.

    **Stated wins.** A `subtype: "repo"` record naming `branches` is the repo answering for itself,
    and the pulls are not consulted — so an open pull heading a branch the record omits does not
    add it back, exactly as real behaves for a pull whose branch was deleted under it. The default
    branch is listed either way; a repo record that omits it from `branches` has still named it.

    **Inferred otherwise**, from the pulls, measured on api.github.com (2026-09-03):

    - An OPEN pull's same-repo head ref is a branch — 27 of 27 across seven repos. Omitting it told
      a client that reads a pull and then resolves its head here that the branch was deleted or
      lives in a fork, contradicting `head.repo.full_name` in the same pull.
    - A MERGED pull's head ref is not — 0 of 54 across ten repos, GitHub's "automatically delete
      head branches" at work. This is the one place the listing must not hold what a pull says.
    - A CLOSED-but-unmerged pull's head ref is left out with it, and that one is a coin toss rather
      than a rule: 6 of 28 such heads still exist. Neither answer is right often enough to be
      right, which is what a repo record exists to settle — a corpus that knows says so, and this
      inference stops guessing for it.
    - A FORK's head ref is not either: it is a branch of the fork. `head_repo` is how a corpus says
      so — a `head_repo` naming THIS repo is not a fork — and without it every pull looked
      same-repo.
    - A `base.ref` is a branch, always, whether or not it is the default branch (pydantic/pydantic
      lists its non-default base `pure-annotation-schema-cache`). A base outlives the pull on it.

    A pull the corpus states no refs for still advertises the fallbacks used here, so they are
    listed for the same reason a stated ref is: the listing has to hold what the pulls say.

    `protected` is false throughout an inferred listing — a pull cannot imply protection — which is
    why `?protected=` only really selects for a repo that states its branches.

    Scoped to `ids` when inferred: pulls are ACL-filtered, so the branches drawn from them are too.
    Deliberate, and the same answer `/user/repos` already gives — a caller who cannot see the pull
    is not told its branch exists — rather than a side effect of where the material happens to
    live. A STATED listing is the same for every caller, because the record it comes from carries
    no ACL of its own (see `store.github_repo_meta`).
    """
    meta = store.github_repo_meta(conn, repo)
    default = (meta["default_branch"] if meta else None) or _DEFAULT_BRANCH
    if meta is not None and meta["branches"] is not None:  # stated: the pulls are not read at all
        protection = {b["name"]: bool(b.get("protected")) for b in json.loads(meta["branches"])}
        protection.setdefault(default, False)
        return [{"name": n, "protected": protection[n]} for n in sorted(protection)]
    names = {default}
    here = f"{owner}/{repo}"
    for row in store.github_pull_refs(conn, repo, ids) if pulls is None else pulls:
        names.add(row["base_ref"] or default)
        # `head_repo` is compared, not merely tested for presence: a corpus may write this repo's
        # own `owner/name` there, and reading that as a fork dropped a head the pull advertises —
        # `_pr_obj` resolves the same field the same way, so the two cannot disagree.
        if row["state"] == "open" and not row["merged_at"] and (row["head_repo"] or here) == here:
            names.add(row["head_ref"] or _UNSTATED_HEAD_REF)
    return [{"name": n, "protected": False} for n in sorted(names)]


def _branch_names(conn, owner: str, repo: str, ids) -> list[str]:
    return [b["name"] for b in _branch_rows(conn, owner, repo, ids)]


def _commit_shas(repo: str, pulls) -> set[str]:
    """Every sha this server hands the caller AS A COMMIT of `repo`.

    The repo's one snapshot commit, plus the head and base shas each visible pull reports — those
    are advertised in the pull object, in `/pulls/{n}/commits` and in the `commits_url` a client
    follows, so a `/commits/{sha}` that rejected them would 422 a link this same server printed.

    Takes the pull rows rather than reading them, so :func:`_commit_ish` can derive both halves of
    its answer from one scan.
    """
    shas = {_repo_commit_sha(repo)}
    for row in pulls:
        head = hashlib.sha1(_seed(row).encode()).hexdigest()
        shas.update((head, head[::-1]))  # the pull's head and its base, as `_pr_obj` derives them
    return shas


def _commit_ish(conn, owner: str, repo: str, ids) -> set[str]:
    """What may stand in for a commit: a branch name, a TAG, or a commit sha — real GitHub's own
    rule for the `{ref}` of `/commits`, `git/trees`, `/statuses` and `?ref=`.

    A tag counts wherever a branch does, measured on psf/requests with `v2.34.2`: all four answer
    200 for it. A tag that resolved on `/tags` and `git/ref/tags/{name}` alone was a ref a client
    is offered and cannot then read at — `GithubFileSystem.refs` lists it and `fs.ls("", sha=…)`
    raises on it.

    One read of the repo's pulls, shared by every half: the branch names and the commit shas come
    from the same rows, and fetching them separately ran that ACL-joined scan twice on every route
    that asks whether a ref exists. The tags are a primary-key lookup on the repo's own row, not
    that scan.
    """
    pulls = store.github_pull_refs(conn, repo, ids)
    branches = _branch_rows(conn, owner, repo, ids, pulls=pulls)
    names = {b["name"] for b in branches} | set(_repo_tags(conn, repo))
    return names | _commit_shas(repo, pulls)


def _no_commit_for_sha(sha: str) -> HTTPException:
    """Real's answer for a `/commits/{ref}` naming nothing — a 422, not the 404 every other ref
    route gives, and with the ref echoed in the message (measured on psf/requests for both a
    made-up name and a 40-hex sha naming no commit)."""
    exc = HTTPException(status_code=422, detail="Unprocessable Entity")
    exc.github_body = {
        "message": f"No commit found for SHA: {sha}",
        "documentation_url": "https://docs.github.com/rest/commits/commits#get-a-commit",
        "status": "422",
    }
    return exc


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


def _file_obj(owner: str, repo: str, row, api_base: str = "", ref: str | None = None) -> dict:
    """The Contents API's file-object shape (base64 body), used by both /contents/{path}
    and /readme — real GitHub serves both from the same underlying object.

    `ref` is carried into every link, as real GitHub does. Without it a `?ref=`-selected response
    holds one snapshot's bytes while `url`/`_links.self` fetch HEAD, so a client re-reading the file
    it was just handed gets different content under the same `path`. It is the SELECTED ref rather
    than the row's own, so a ref that named no snapshot (and fell back to HEAD) does not invent a
    revision the corpus never stated.
    """
    content = row["content"]
    path = row["path"]
    name = path.rsplit("/", 1)[-1]
    sha = _blob_sha(content)
    q = f"?ref={quote(ref, safe='')}" if ref else ""
    rev = quote(ref, safe="") if ref else "main"
    url = f"{api_base}/repos/{owner}/{repo}/contents/{path}{q}"
    git_url = f"{api_base}/repos/{owner}/{repo}/git/blobs/{sha}"
    html_url = f"https://github.com/{owner}/{repo}/blob/{rev}/{path}"
    download_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{rev}/{path}"
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
    """A repository, carrying a URL template for each sub-resource Backlot serves.

    The templates are how an SDK completes a repository lazily — PyGithub expands them for the
    example this repo ships — so without them the client assembles the URLs from parts, which is the
    work hypermedia exists to remove. All of them derive from owner/repo; none needs stored data.

    THE RULE IS "a template iff the resource". Real serves 42 of these and Backlot has routes for a
    third, so the rest are absent rather than inviting a caller into a 404: a key set that lies about
    what can be fetched is a worse deal than a short one, and short is something the caller can
    detect and work around. Adding a route later means adding its template here. (`git_refs_url` is
    absent for a subtler version of the same reason — Backlot serves `/git/ref/{ref}`, singular,
    not real's plural `/git/refs{/sha}`.)

    The git-protocol URLs are the exception: they name github.com rather than Backlot, so they
    promise it nothing and cost nothing to state.
    """
    private = not store.container_has_public(conn, "github", name)
    rid = synth.github_user_id(name)
    ts = synth.epoch("repo:" + name)
    repo_url = f"{api_base}/repos/{owner}/{name}"
    return {
        "id": rid,
        "node_id": synth.node_id("Repository", rid),
        "name": name,
        "full_name": f"{owner}/{name}",
        "private": private,
        "visibility": "private" if private else "public",
        "owner": {**_gh_user(f"{owner}@org", api_base), "login": owner, "type": "Organization"},
        "html_url": f"https://github.com/{owner}/{name}",
        "url": repo_url,
        "description": f"{name} service repository.",
        "fork": False,
        "archived": False,
        "disabled": False,
        "created_at": synth.rfc3339(ts),
        "updated_at": synth.rfc3339(ts + 3600),
        "pushed_at": synth.rfc3339(ts + 7200),
        "default_branch": _default_branch(conn, name),
        "issues_url": f"{repo_url}/issues{{/number}}",
        "pulls_url": f"{repo_url}/pulls{{/number}}",
        "issue_comment_url": f"{repo_url}/issues/comments{{/number}}",
        "contents_url": f"{repo_url}/contents/{{+path}}",
        "blobs_url": f"{repo_url}/git/blobs{{/sha}}",
        "trees_url": f"{repo_url}/git/trees{{/sha}}",
        "branches_url": f"{repo_url}/branches{{/branch}}",
        "tags_url": f"{repo_url}/tags",
        "commits_url": f"{repo_url}/commits{{/sha}}",
        "statuses_url": f"{repo_url}/statuses/{{sha}}",
        "collaborators_url": f"{repo_url}/collaborators{{/collaborator}}",
        "teams_url": f"{repo_url}/teams",
        "clone_url": f"https://github.com/{owner}/{name}.git",
        "ssh_url": f"git@github.com:{owner}/{name}.git",
        "git_url": f"git://github.com/{owner}/{name}.git",
        "svn_url": f"https://github.com/{owner}/{name}",
    }


def _resolve(conn, repo: str, number: int, ids):
    """One issue/PR by its served number, ACL-scoped — a PRIMARY KEY lookup (see
    store.github_by_number).

    A `kind='file'` row carries a number too (the table holds two resources and only one key can be
    primary — see the schema), so this guard is load-bearing: a file has no title/body/state in the
    issue sense and is addressed by (repo, path)."""
    row = store.github_by_number(conn, repo, number, visible_ids=ids)
    return row if row is not None and row["kind"] != "file" else None


def _milestone(row, owner, repo, api_base):
    title = row["milestone"]
    if not title:
        return None
    num = synth.github_number(_seed(row) + ":ms") % 100
    return {
        "number": num,
        "title": title,
        "state": "open",
        "url": f"{api_base}/repos/{owner}/{repo}/milestones/{num}",
        "html_url": f"https://github.com/{owner}/{repo}/milestone/{num}",
    }


def _issue_number(row) -> int:
    """The number this document answers to — its own stored `number`, assigned at import (see
    backlot.importer.byo's `resolve_github_numbers`).

    Deriving it here would disagree with that assignment whenever a row's plain hash was already
    claimed by a record that stated it outright: this row would advertise a number that fetches
    somebody else, and be reachable at nothing. Asserted rather than re-derived, because every
    github row carries a number by the time it is served."""
    assert row["number"] is not None, "github: a row reached the serializer with no number"
    return row["number"]


# Which versions still carry a field a later one removed. Stated as "who HAS it" rather than "who
# dropped it" so that adding a third version has to answer the question instead of inheriting an
# answer from whichever side the condition happened to be written on.
_HAS_SINGULAR_ASSIGNEE = frozenset({"2022-11-28"})  # 2026-03-10: superseded by `assignees`
_HAS_MERGE_COMMIT_SHA = frozenset({"2022-11-28"})  # 2026-03-10: removed from every pull body


def _shared_obj(conn, owner: str, repo: str, row, api_base: str, version: str) -> dict:
    """The fields real GitHub's issue body and its pull body BOTH carry.

    Split out because a pull is not an issue with extra keys — real serves each its own field set,
    and building the pull as ``_issue_obj`` plus additions leaked ten issue-only fields onto it,
    ``pull_request`` included. That marker exists to tell a caller an ISSUE is really a pull, so a
    pull carrying one points at itself and tells a client the opposite of the truth.

    ``comments_url`` is shared and shared in VALUE too: a pull's conversation comments are the
    issue's, and real points both objects at the same collection.
    """
    created = row["created_ts"] or synth.epoch(_seed(row))
    updated = row["updated_ts"] or created + 3600
    number = _issue_number(row)
    iid = synth.jira_numeric_id(_seed(row))  # a stable large numeric db id (≠ number)
    is_pr = row["kind"] == "pull_request"
    state = row["state"]
    assignees = [_gh_user(a, api_base) for a in store.jcol(row, "assignees")]
    issue_url = f"{api_base}/repos/{owner}/{repo}/issues/{number}"
    obj = {
        "id": iid,
        "node_id": synth.node_id("Issue", iid),
        "number": number,
        "title": row["title"],
        "body": row["content"],
        "state": state,
        "locked": False,
        "active_lock_reason": None,
        "user": _gh_user(row["author_email"], api_base),
        "labels": [
            {
                "id": synth.github_number(_seed(row) + lbl),
                "name": lbl,
                "color": "ededed",
                "default": False,
                "description": None,
            }
            for lbl in store.jcol(row, "labels")
        ],
        "assignees": assignees,
        "milestone": _milestone(row, owner, repo, api_base),
        "comments": len(store.github_comments(conn, row["repo"], row["number"], anchored=False)),
        "author_association": "MEMBER",
        "created_at": synth.rfc3339(created),
        "updated_at": synth.rfc3339(updated),
        "closed_at": (
            synth.rfc3339(row["closed_ts"])
            if row["closed_ts"]
            else synth.rfc3339(updated)
            if state == "closed"
            else None
        ),
        "comments_url": f"{issue_url}/comments",
        "html_url": f"https://github.com/{owner}/{repo}/{'pull' if is_pr else 'issues'}/{number}",
    }
    if version in _HAS_SINGULAR_ASSIGNEE:
        # Both objects carried it and both lost it in the same version, so the gate is here rather
        # than repeated in each builder.
        obj["assignee"] = assignees[0] if assignees else None
    return obj


def _issue_obj(
    conn,
    owner: str,
    repo: str,
    row,
    api_base: str = "",
    version: str = DEFAULT_API_VERSION,
) -> dict:
    """An issue. A pull seen through ``/issues`` is one of these too — plus the marker saying so."""
    obj = _shared_obj(conn, owner, repo, row, api_base, version)
    self_url = f"{api_base}/repos/{owner}/{repo}/issues/{obj['number']}"
    obj.update(
        {
            "url": self_url,
            "state_reason": ("completed" if obj["state"] == "closed" else None),
            "reactions": _reactions(store.jcol(row, "reactions", {}), self_url),
            "closed_by": _gh_user(row["closed_by"], api_base) if row["closed_by"] else None,
            "repository_url": f"{api_base}/repos/{owner}/{repo}",
            "labels_url": f"{self_url}/labels{{/name}}",
            "events_url": f"{self_url}/events",
            "timeline_url": f"{self_url}/timeline",
        }
    )
    if row["kind"] == "pull_request":  # what connectors read to tell PRs apart in the stream
        number = obj["number"]
        obj["pull_request"] = {
            "url": f"{api_base}/repos/{owner}/{repo}/pulls/{number}",
            "html_url": f"https://github.com/{owner}/{repo}/pull/{number}",
            "diff_url": f"https://github.com/{owner}/{repo}/pull/{number}.diff",
            "patch_url": f"https://github.com/{owner}/{repo}/pull/{number}.patch",
            "merged_at": row["merged_at"],
        }
    return obj


def _pr_obj(
    conn,
    owner: str,
    repo: str,
    row,
    api_base: str = "",
    ids=None,
    files=None,
    repo_files=None,
    version: str = DEFAULT_API_VERSION,
) -> dict:
    """A pull, in real GitHub's PULL shape — see ``_shared_obj`` for why that is not the issue's.

    The hypermedia half (``_links`` and the ``*_url`` siblings) is the point of the field set: it is
    how a caller reaches a pull's sub-resources without assembling URLs from parts, which is what
    hypermedia is for. Every href here names a route Backlot serves, so following one gets an
    answer rather than a 404 — ``review_comment`` excepted, which real serves as a template too.
    """
    obj = _shared_obj(conn, owner, repo, row, api_base, version)
    sha = hashlib.sha1(_seed(row).encode()).hexdigest()
    number = obj["number"]
    repo_url = f"{api_base}/repos/{owner}/{repo}"
    self_url = f"{repo_url}/pulls/{number}"
    issue_url = f"{repo_url}/issues/{number}"
    commits_url = f"{self_url}/commits"
    review_comments_url = f"{self_url}/comments"
    # A template, as real serves it: one comment id is not knowable from the pull alone.
    review_comment_url = f"{repo_url}/pulls/comments{{/number}}"
    # Real anchors this on the HEAD sha rather than templating it — the pull knows its own head.
    statuses_url = f"{repo_url}/statuses/{sha}"
    reviewers = [_gh_user(e, api_base) for e in store.jcol(row, "requested_reviewers")]
    n_comments = obj["comments"]
    # one _RepoFiles for both the changeset and the review-comment resolution below, so a file
    # either of them touches is read once
    src = repo_files if repo_files is not None else _RepoFiles(conn, repo, ids)
    if files is None:
        files = _pr_files(conn, owner, repo, row, api_base, ids, src)
    # `head_repo` is a full `owner/name` precisely because a fork's owner is what differs; an
    # unstated one means the head is a branch of this repo, under this owner.
    head_repo = row["head_repo"] or f"{owner}/{repo}"
    head_owner = head_repo.split("/", 1)[0]
    base_branch = _default_branch(conn, repo)
    obj.update(
        {
            # A PR's issue view and its pull view are two DISTINCT nodes on real GitHub, so this
            # overrides the Issue-typed id _shared_obj built. /issues keeps the Issue one on purpose.
            "node_id": synth.node_id("PullRequest", obj["id"]),
            "draft": False,
            "merged": bool(row["merged_at"]),
            "merged_at": row["merged_at"],
            "merged_by": _gh_user(row["merged_by"], api_base) if row["merged_by"] else None,
            "mergeable": None,
            "mergeable_state": "unknown",
            "rebaseable": None,
            "auto_merge": None,  # nothing in a corpus says a pull was queued to auto-merge
            "maintainer_can_modify": False,
            "requested_reviewers": reviewers,
            "requested_teams": [],
            # `head_repo` is the fork the head branch lives in, when the corpus states one. Real
            # spells the difference in both fields: an outside pull on pydantic/pydantic reports
            # `head.repo.full_name` `chenlichao/pydantic` and `head.label`
            # `chenlichao:fix/…` — the FORK's owner, not the base repo's. The base is always this
            # repo, so its label keeps this owner.
            "head": {
                "ref": row["head_ref"] or _UNSTATED_HEAD_REF,
                "sha": sha,
                "label": f"{head_owner}:{row['head_ref'] or _UNSTATED_HEAD_REF}",
                "user": obj["user"],
                "repo": {"full_name": head_repo},
            },
            "base": {
                "ref": row["base_ref"] or base_branch,
                "sha": sha[::-1],
                "label": f"{owner}:{row['base_ref'] or base_branch}",
                "user": obj["user"],
                "repo": {"full_name": f"{owner}/{repo}"},
            },
            "commits": 1,
            # Summed over the synthesized changeset rather than guessed from the body length, so
            # these agree with what /pulls/{n}/files reports. They used to contradict it.
            "additions": sum(f["additions"] for f in files),
            "deletions": sum(f["deletions"] for f in files),
            "changed_files": len(files),
            # The anchored half of github_comments; `comments` above is the conversation half. Real
            # GitHub reports the two separately, and one number covering both would contradict
            # whichever list the caller then fetched. Resolved the same way /pulls/{n}/comments
            # resolves it, so the count describes the list the caller actually gets.
            "review_comments": len(_resolved_review_comments(conn, row, src)),
            "comments": n_comments,
            "url": self_url,
            "diff_url": f"https://github.com/{owner}/{repo}/pull/{number}.diff",
            "patch_url": f"https://github.com/{owner}/{repo}/pull/{number}.patch",
            "issue_url": issue_url,
            "commits_url": commits_url,
            "review_comments_url": review_comments_url,
            "review_comment_url": review_comment_url,
            "statuses_url": statuses_url,
            # The same eight targets, in the envelope a client walks instead of reading the flat
            # fields. Real serves both; a caller following `_links` and a caller reading
            # `review_comments_url` must land on the same resource, so they are built from one set of
            # locals rather than assembled twice.
            "_links": {
                "self": {"href": self_url},
                "html": {"href": obj["html_url"]},
                "issue": {"href": issue_url},
                "comments": {"href": obj["comments_url"]},
                "review_comments": {"href": review_comments_url},
                "review_comment": {"href": review_comment_url},
                "commits": {"href": commits_url},
                "statuses": {"href": statuses_url},
            },
        }
    )
    if version in _HAS_MERGE_COMMIT_SHA:
        obj["merge_commit_sha"] = sha[:40] if row["merged_at"] else None
    return obj


# --- pull request changesets ----------------------------------------------------
#
# WHICH files a pull changed comes from its `changed_paths` when the corpus declared it. Nothing
# records the HUNKS, so those are always synthesized — deterministically, seeded on the pull's
# served key — but they invent no content: every line on either side is a line of that file's own
# snapshot. A `modified` file's "before" state is the snapshot with one real block either taken out
# or duplicated (see _patch_modified), so the hunk is a well-formed diff against real bytes in both
# directions. Without `changed_paths` the file list is chosen deterministically too, which is
# well-formed but unrelated to what the pull is about.
#
# What that buys, none of which was reachable before: a client's diff path, its handling of an
# omitted `patch`, and — for a corpus that declares more paths than one page holds — its paging over
# a changed-file list. A synthesized list caps at _MAX_CHANGED_FILES and will not fill a page.
#
# THE SNAPSHOT IS THE PULL'S HEAD. Every hunk here is expressed against that one convention, which
# is what makes the diff actually apply: `git apply --reverse` walks the snapshot back to the base
# (tests/test_github.py checks exactly that with real git). Two consequences:
#   - A repo with no `file` docs has an EMPTY changeset, and the pull object's counts follow it to
#     zero. There is no snapshot to diff, and naming a file the repo does not contain would
#     contradict its own tree.
#   - `status: "removed"` is NOT produced. A deleted file is absent from the head, but the snapshot
#     is the only state Backlot has, so a file it names as removed would still be in the tree — and
#     a diff mixing that with a `modified` hunk claims the snapshot is base and head at once, which
#     no client can apply. Deletions come from the `dedup` flavour below instead.

_MAX_CHANGED_FILES = 3  # only bounds a SYNTHESIZED changeset; `changed_paths` is taken in full
_MAX_BLOCK_LINES = 3  # how many lines one hunk adds or removes
_PATCH_CONTEXT = 3
# Real GitHub omits `patch` from the JSON file object for a binary file or a diff too large to
# inline. That is a limit on THAT representation only — the `.diff` media type still carries the
# hunks — so it is applied where the JSON is built (see _json_file_objects), never when the hunk is.
PATCH_MAX_BYTES = 1024 * 1024
_CHANGE_STATUSES = ("modified", "added")


class _RepoFiles:
    """One repo's files for the life of one request: the path listing, fetched lazily, plus a memo
    of the rows actually read.

    Both halves matter on a list endpoint. A ``/pulls`` page builds a changeset per row and those
    changesets overlap heavily — a synthesized one draws from the same pool, and a declared
    ``changed_paths`` repeats across pulls — so without the memo the same file's content is read once
    per pull. Lazy because a pull that declares its paths never needs the listing at all.
    """

    def __init__(self, conn, repo: str, ids):
        self._conn, self._repo, self._ids = conn, repo, ids
        self._paths: list[str] | None = None
        self._rows: dict[str, object] = {}

    @property
    def paths(self) -> list[str]:
        if self._paths is None:
            self._paths = store.list_repo_file_paths(self._conn, self._repo, self._ids)
        return self._paths

    def get(self, path: str):
        """The file row, or None when the caller cannot see it or the repo has no such file. The two
        are deliberately indistinguishable to callers: a path the corpus named but this caller may
        not read must behave exactly like one that was a typo, or the response reveals which."""
        if path not in self._rows:
            self._rows[path] = store.get_repo_file(self._conn, self._repo, path, self._ids)
        return self._rows[path]


def _pr_files(
    conn, owner: str, repo: str, row, api_base: str = "", ids=None, repo_files=None
) -> list[dict]:
    """The pull's changed files in the real API's shape. See the changeset note above.

    ``changed_paths`` on the pull wins when the corpus set it — in full and in its declared order,
    uncapped, because the corpus is stating a fact rather than asking for a plausible one. Otherwise
    up to :data:`_MAX_CHANGED_FILES` paths are chosen deterministically.

    Either way only the chosen files are read, never the whole repo — pass ``repo_files`` to share
    one :class:`_RepoFiles` across a page of pulls.
    """
    src = repo_files if repo_files is not None else _RepoFiles(conn, repo, ids)
    seed = _seed(row)
    declared = store.jcol(row, "changed_paths")
    if declared:
        # dict.fromkeys: declared order, minus repeats. A path named twice would put the same file
        # in the diff twice, which `git apply` refuses outright.
        chosen = list(dict.fromkeys(declared))
        # A corpus naming a path says the pull CHANGED a file the repo already has, so every
        # declared file is `modified` (bar the ones _changed_file has to downgrade for want of a
        # hunk). Varying it would have Backlot claim the pull CREATED a file, which is more than
        # the corpus said. A synthesized changeset still varies, so `added` stays exercisable.
        statuses = dict.fromkeys(chosen, "modified")
    else:
        paths = src.paths
        if not paths:
            return []
        n = 1 + synth.hnum(seed, salt="pr-nfiles") % min(_MAX_CHANGED_FILES, len(paths))
        start = synth.hnum(seed, salt="pr-offset") % len(paths)
        chosen = [paths[(start + i) % len(paths)] for i in range(n)]
        # Seeded on (pull, path) rather than the file's position, so a file's status does not shift
        # when an ACL-hidden sibling drops out of the list.
        statuses = {
            p: _CHANGE_STATUSES[synth.hnum(f"{seed}:{p}", salt="pr-status") % 2] for p in chosen
        }
    head_sha = hashlib.sha1(seed.encode()).hexdigest()
    out = []
    for path in chosen:
        f = src.get(path)
        if f is None:  # not visible to this caller, or no such file — see _RepoFiles.get
            continue
        out.append(_changed_file(seed, owner, repo, f, statuses[path], head_sha, api_base))
    return out


def _seed(row) -> str:
    """A stable seed for the values GitHub derives rather than stores — a commit sha, a review id,
     a milestone number. It was the corpus's own document id; it is the row's OWN identity now
    , which is equally stable and is what the row is actually addressed by."""
    return f"{row['repo']}#{row['number']}"


def _changed_file(
    seed: str, owner: str, repo: str, row, status: str, head_sha: str, api_base: str
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
        added, deleted, patch = _patch_modified(f"{seed}:{path}", lines, selectable)
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
    does not already contain. A replacement would need "before" text that is nowhere in the corpus,
    and inventing a line is the fabrication this module exists to avoid:

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


def _json_file_objects(files: list[dict]) -> list[dict]:
    """The file objects as the JSON endpoint serves them: `patch` dropped when it is too large to
    inline, which is what real GitHub does for that field. See :data:`PATCH_MAX_BYTES` — the cap
    belongs to this representation, so the diff built from the same list keeps its hunks."""
    return [
        {k: v for k, v in f.items() if k != "patch"}
        if f.get("patch") and len(f["patch"].encode()) > PATCH_MAX_BYTES
        else f
        for f in files
    ]


def _pr_diff(files: list[dict], base_sha: str) -> str:
    """The pull's unified diff (`Accept: application/vnd.github.diff`), git-apply-able.

    A file with no hunk at all is left out rather than given a header: `diff --git` followed by
    nothing is not an empty diff, it is what real git reports as "patch with only garbage", and it
    would make the WHOLE diff unapplyable rather than that one file."""
    out = []
    for f in files:
        if not f.get("patch"):
            continue
        a, b = f"a/{f['filename']}", f"b/{f['filename']}"
        out.append(f"diff --git {a} {b}\n")
        short = f["sha"][:7]
        if f["status"] == "added":
            out.append(f"new file mode 100644\nindex 0000000..{short}\n--- /dev/null\n+++ {b}\n")
        else:  # `removed` is never synthesized — see the changeset note above
            out.append(f"index {base_sha[:7]}..{short} 100644\n--- {a}\n+++ {b}\n")
        out.append(f["patch"] if f["patch"].endswith("\n") else f["patch"] + "\n")
    return "".join(out)


def _pr_mbox(row, obj: dict, diff: str) -> str:
    """The pull as a mail patch (`Accept: application/vnd.github.patch`). Real GitHub's `patch`
    media type is a `git am`-able mbox, NOT the same bytes as `diff` — a client that pipes one to
    the wrong tool has to be able to tell them apart here too."""
    ts = row["created_ts"] or synth.epoch(_seed(row))
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


def _resolved_review_comments(conn, row, repo_files) -> list[tuple]:
    """This pull's anchored comments paired with the file each one resolves to, dropping any whose
    ``path`` names no file the caller can read.

    One resolution, used by both the list endpoint and the ``review_comments`` count on the pull.
    Counting the raw rows instead made the two contradict each other — a client paging until it had
    ``review_comments`` items never finished — and leaked that a hidden file carries a comment.
    """
    out = []
    for c in store.github_comments(conn, row["repo"], row["number"], anchored=True):
        f = repo_files.get(c["path"])
        if f is not None:  # else: hidden from this caller, or no such file — see _RepoFiles.get
            out.append((c, f))
    return out


def _gh_review_comment(
    owner: str, repo: str, number: int, pr_row, c, file_row, patches: dict, api_base: str = ""
) -> dict:
    """One line-anchored review comment, in the real API's shape.

    ``diff_hunk`` prefers what the corpus supplied, then the hunk this pull's own diff carries for
    that file, so the comment and the diff agree. It falls back to a context window from the
    snapshot when the comment anchors to a file the changeset does not touch — real GitHub cannot
    produce that, but there is no reason to drop the comment over it.
    """
    ts = c["created_ts"] or synth.epoch(c["id"])
    email = c["author_email"] or "unknown@x"
    cid = c["id"]
    head = hashlib.sha1(_seed(pr_row).encode()).hexdigest()
    self_url = f"{api_base}/repos/{owner}/{repo}/pulls/comments/{cid}"
    pr_url = f"{api_base}/repos/{owner}/{repo}/pulls/{number}"
    html_url = f"https://github.com/{owner}/{repo}/pull/{number}#discussion_r{cid}"
    hunk = _comment_hunk(c, patches.get(c["path"]), file_row)
    return {
        "id": cid,
        "node_id": synth.node_id("PullRequestReviewComment", cid),
        "pull_request_review_id": synth.github_number(_seed(pr_row) + ":review"),
        "path": c["path"],
        "line": c["line"],
        "original_line": c["line"],
        "start_line": None,
        "original_start_line": None,
        "side": "RIGHT",
        "start_side": None,
        # no history here, so the commit a comment is "original" to is the pull's head
        "commit_id": head,
        "original_commit_id": head,
        "diff_hunk": hunk,
        "position": _hunk_position(hunk, c["line"]),
        "original_position": _hunk_position(hunk, c["line"]),
        # real GitHub's own discriminator for a comment on a whole file rather than one line
        "subject_type": "line" if c["line"] else "file",
        "body": c["body"],
        "user": _gh_user(email, api_base),
        "created_at": synth.rfc3339(ts),
        "updated_at": synth.rfc3339(ts),
        "author_association": "MEMBER",
        "reactions": _reactions(store.jcol(c, "reactions", {}), self_url),
        "url": self_url,
        "pull_request_url": pr_url,
        "html_url": html_url,
        "_links": {
            "self": {"href": self_url},
            "html": {"href": html_url},
            "pull_request": {"href": pr_url},
        },
    }


_HUNK_HEADER = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _hunk_position(hunk: str, line: int | None) -> int | None:
    """``position`` is the row's offset INSIDE the diff hunk — 1-based, counting every row after the
    ``@@`` header — not the line number in the file, which is what ``line`` reports. Real GitHub
    returns both, and a client resolving a comment against a diff uses this one, so reporting
    ``line`` for it would look right and point at the wrong row.

    None when the comment has no line (a file-level comment) or the hunk does not cover it, which is
    a state the real API has too."""
    if not hunk or line is None:
        return None
    rows = hunk.split("\n")
    m = _HUNK_HEADER.match(rows[0])
    if m is None:
        return None
    cur = int(m.group(1))
    for offset, row in enumerate(rows[1:], start=1):
        # A removed row occupies no line on the new side, and `\ No newline at end of file` is a
        # hunk row but not a line of the file at all — counting it lets a line past the end of the
        # file resolve to the marker's own offset.
        if not row or row[0] in "-\\":
            continue
        if cur == line:
            return offset
        cur += 1
    return None


def _comment_hunk(c, patch: str | None, file_row) -> str:
    """The hunk a review comment is anchored to: what the corpus supplied, else this pull's own hunk
    for that file, else a context window from the snapshot.

    The middle rung is taken only when the pull's hunk actually COVERS the commented line — a pull
    changes one part of a file and a comment may sit anywhere in it, so handing back a hunk the
    comment's own `line` is nowhere inside would leave `position` null against a `diff_hunk` that
    looks authoritative."""
    if c["diff_hunk"]:
        return c["diff_hunk"]
    if patch and (c["line"] is None or _hunk_position(patch, c["line"]) is not None):
        return patch
    return _hunk_around(file_row, c["line"])


def _hunk_around(file_row, line: int | None) -> str:
    """A context-only hunk covering ``line`` of the file's snapshot — the fallback for a review
    comment on a file this pull's changeset does not touch. All-context because nothing changed on
    that file: inventing +/- rows to look more like a diff would claim an edit that is not in the
    changeset the same pull serves."""
    lines = (file_row["content"] or "").splitlines(keepends=True)
    if not lines:
        return ""
    idx = max(0, min((line - 1) if line else 0, len(lines) - 1))
    lo = max(0, idx - _PATCH_CONTEXT)
    window = lines[lo : idx + _PATCH_CONTEXT + 1]
    header = f"@@ -{lo + 1},{len(window)} +{lo + 1},{len(window)} @@"
    return header + "\n" + "".join(" " + ln for ln in _nl_terminated(window))


def _gh_comment(owner: str, repo: str, number: int, c, api_base: str = "") -> dict:
    # `is not None`, and str(): a comment's id is an INTEGER, and 0 is a second a corpus can write,
    # so under truthiness a comment dated 1970-01-01T00:00:00Z reaches `synth.epoch` — which hashes
    # a STRING.
    ts = c["created_ts"] if c["created_ts"] is not None else synth.epoch(str(c["id"]))
    email = c["author_email"] or "unknown@x"
    cid = c["id"]
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
