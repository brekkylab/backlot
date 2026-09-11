"""GitHub's error envelope.

Real GitHub answers an error as ``{"message", "documentation_url", "status"}``, plus an ``errors``
member on the kinds that carry one. That is not FastAPI's ``{"detail": …}``, and the difference is
not cosmetic: PyGithub — the SDK ``examples/using-official-sdk/github.py`` drives — picks its
exception CLASS off the body's ``message``, so the same wrong token raises
``BadCredentialsException`` against real and a bare ``GithubException`` against a server answering
``detail`` (measured against both). A client's error handling could not be written here.

``documentation_url`` is per-ENDPOINT and measured, not derived: requesting each route shape against
a repository that does not exist returns that route's own docs anchor, which is where
:data:`ROUTE_DOCS` comes from (every route below but one, none inferred from another). The one is
``/rate_limit``, which names no repository and whose only error on the wire is the credential 401
with the root below; its entry is the ``externalDocs`` url real's description gives the operation,
and no answer Backlot gives on that route reaches it.

An authentication failure is the exception to that: real answers those with the bare
``https://docs.github.com/rest``, measured on a bad bearer at
``/repos/{owner}/{repo}/collaborators`` and at ``/user/repos``. Real is not uniform there and
Backlot does not follow it all the way — ``/user/repos`` with NO Authorization header carries that
route's anchor, while a bad token on the same route carries the root. Backlot answers the root for
both 401s, which is this module's one knowing divergence.
"""

from __future__ import annotations

import re
from functools import lru_cache

PREFIX = "/github"

# What real sends when the failure is about the credential rather than the resource.
DOCS_ROOT = "https://docs.github.com/rest"

_DOCS = "https://docs.github.com/rest/"

# Route template -> the anchor real names in an error on that route. Ordered as the router declares
# them, and read first-match, because two templates can cover one path and FastAPI resolves those by
# declaration order too (`issues/comments/{comment_id}` before `issues/{number}/comments`).
# ``tests/test_github.py`` holds this table to the routes the app actually serves.
ROUTE_DOCS: dict[str, str] = {
    "/github/search/issues": "https://docs.github.com/v3/search",
    "/github/search/code": "https://docs.github.com/v3/search",
    "/github/orgs/{org}": _DOCS + "orgs/orgs#get-an-organization",
    "/github/rate_limit": (
        _DOCS + "rate-limit/rate-limit#get-rate-limit-status-for-the-authenticated-user"
    ),
    "/github/orgs/{org}/repos": _DOCS + "repos/repos#list-organization-repositories",
    "/github/user/repos": _DOCS + "repos/repos#list-repositories-for-the-authenticated-user",
    "/github/repos/{owner}/{repo}": _DOCS + "repos/repos#get-a-repository",
    "/github/repos/{owner}/{repo}/issues": _DOCS + "issues/issues#list-repository-issues",
    "/github/repos/{owner}/{repo}/issues/comments/{comment_id}": (
        _DOCS + "issues/comments#get-an-issue-comment"
    ),
    "/github/repos/{owner}/{repo}/pulls/comments/{comment_id}": (
        _DOCS + "pulls/comments#get-a-review-comment-for-a-pull-request"
    ),
    "/github/repos/{owner}/{repo}/issues/{number}": _DOCS + "issues/issues#get-an-issue",
    "/github/repos/{owner}/{repo}/issues/{number}/comments": (
        _DOCS + "issues/comments#list-issue-comments"
    ),
    "/github/repos/{owner}/{repo}/pulls": _DOCS + "pulls/pulls#list-pull-requests",
    "/github/repos/{owner}/{repo}/pulls/{number}": _DOCS + "pulls/pulls#get-a-pull-request",
    "/github/repos/{owner}/{repo}/pulls/{number}/reviews": (
        _DOCS + "pulls/reviews#list-reviews-for-a-pull-request"
    ),
    "/github/repos/{owner}/{repo}/pulls/{number}/comments": (
        _DOCS + "pulls/comments#list-review-comments-on-a-pull-request"
    ),
    "/github/repos/{owner}/{repo}/pulls/{number}/commits": (
        _DOCS + "pulls/pulls#list-commits-on-a-pull-request"
    ),
    "/github/repos/{owner}/{repo}/statuses/{sha}": (
        _DOCS + "commits/statuses#list-commit-statuses-for-a-reference"
    ),
    "/github/repos/{owner}/{repo}/pulls/{number}/files": (
        _DOCS + "pulls/pulls#list-pull-requests-files"
    ),
    "/github/repos/{owner}/{repo}/git/ref/{ref}": _DOCS + "git/refs#get-a-reference",
    "/github/repos/{owner}/{repo}/git/trees/{ref}": _DOCS + "git/trees#get-a-tree",
    "/github/repos/{owner}/{repo}/contents": _DOCS + "repos/contents#get-repository-content",
    "/github/repos/{owner}/{repo}/contents/{path}": (
        _DOCS + "repos/contents#get-repository-content"
    ),
    "/github/repos/{owner}/{repo}/git/blobs/{sha}": _DOCS + "git/blobs#get-a-blob",
    "/github/repos/{owner}/{repo}/branches": _DOCS + "branches/branches#list-branches",
    "/github/repos/{owner}/{repo}/tags": _DOCS + "repos/repos#list-repository-tags",
    "/github/repos/{owner}/{repo}/branches/{branch}/protection": (
        _DOCS + "branches/branch-protection#get-branch-protection"
    ),
    "/github/repos/{owner}/{repo}/branches/{branch}": _DOCS + "branches/branches#get-a-branch",
    "/github/repos/{owner}/{repo}/commits/{sha}": _DOCS + "commits/commits#get-a-commit",
    "/github/repos/{owner}/{repo}/readme": _DOCS + "repos/contents#get-a-repository-readme",
    "/github/repos/{owner}/{repo}/collaborators": (
        _DOCS + "collaborators/collaborators#list-repository-collaborators"
    ),
    "/github/orgs/{org}/teams": _DOCS + "teams/teams#list-teams",
    "/github/repos/{owner}/{repo}/teams": _DOCS + "repos/repos#list-repository-teams",
}


def owns(path: str) -> bool:
    return path.startswith(PREFIX)


#: What real's JSON responses declare, 200 and error alike, on every GitHub route measured but one.
JSON_MEDIA_TYPE = "application/json; charset=utf-8"

#: The statuses the code search backend answers itself, and so without the charset. Its 400 is
#: text/plain and never a JSON body; the 401 and the 405 this server answers on the path are the
#: gateway's kind and keep the charset.
_CODE_SEARCH_BACKEND_STATUSES = frozenset({200, 422})


def json_media_type(path: str, status_code: int) -> str:
    """The `content-type` real puts on a JSON body answered at ``path`` with ``status_code``.

    `application/json; charset=utf-8` everywhere: measured 2026-09-06 on api.github.com over
    thirteen responses, the 200s of `/repos/{o}/{r}/issues`, `/repos/{o}/{r}`, `/branches`,
    `/commits`, `/contents/{path}`, `/orgs/{org}`, `/user/repos` and `/search/issues`, the 404 for
    a repository that does not exist, the 422 for a blank search `q`, the version 400, the 401 for
    no credential, and the 200 `/repos/{o}/{r}` answers with no credential at all (the thirteen
    unchanged on 2026-09-08). Code search is the exception, served by a backend that is not the
    rest of the API's: `/search/code` answers `application/json` with no charset on its 200 and on
    its 422s alike, the same backend that reads no version header (see
    ``routers.github.honours_api_version``).
    The exception is the backend's and not the path's: the 401 for no credential or a bad one on
    `/search/code` is the gateway's answer and carries the charset like every other 401 (measured
    2026-09-07), so on this path only the statuses the backend answers, 200 and 422, go without it.
    A wrong method on the path is Starlette's 405 here, the gateway's 404 on real, and both are
    the gateway's shape, charset included.

    The parameter changes no byte of the body, JSON being UTF-8 by definition; what it changes is
    the header a client or a recorded fixture compares as a string. Imported lazily, as
    :func:`_routes` is, so the dependency between this package and the router stays one-way.
    """
    from backlot.routers.github import CODE_SEARCH_PATH

    if path == CODE_SEARCH_PATH and status_code in _CODE_SEARCH_BACKEND_STATUSES:
        return "application/json"
    return JSON_MEDIA_TYPE


@lru_cache(maxsize=1)
def _routes() -> list[tuple[object, str]]:
    """The router's own routes, each paired with the key :data:`ROUTE_DOCS` uses.

    Asking Starlette which route a path reaches, rather than re-implementing the match here: a
    hand-rolled scanner has to know that `git/ref/{ref:path}` swallows the rest of the path while
    `branches/{branch}` does not, and that `issues/comments/{id}` is tried before
    `issues/{number}/comments` — rules that are already true of the router, and that a restatement
    would get wrong the first time a route arrived with a converter the restatement had not met.

    Imported inside the function: ``backlot.errors`` is imported before the routers are, and the
    router imports nothing from this package, so the dependency stays one-way.
    """
    from starlette.routing import Route

    from backlot.routers.github import router as github_router

    return [
        (r, re.sub(r"\{(\w+):path\}", r"{\1}", r.path))
        for r in github_router.routes
        if isinstance(r, Route)
    ]


def docs_url(path: str) -> str:
    """The anchor real names for the route ``path`` reaches, or the root for anything else.

    The root is also the honest answer for a path that matches no route: real has no endpoint
    documentation to point at for a URL it does not serve either.
    """
    from starlette.routing import Match

    scope = {"type": "http", "path": path, "method": "GET", "path_params": {}, "headers": []}
    for route, template in _routes():
        if route.matches(scope)[0] is not Match.NONE:
            return ROUTE_DOCS.get(template, DOCS_ROOT)
    return DOCS_ROOT


def http_body(path: str, exc) -> dict | None:
    """Render an exception into the envelope.

    An error the router shaped by hand carries its own body as ``github_body``, and that wins. Seven
    sites do, each for one of three reasons no generic rendering could supply: an ``errors`` member
    (the version 400, the two Validation Failed search 422s and the issue listing's ``state`` 422);
    a ``documentation_url`` that is not the route's anchor in :data:`ROUTE_DOCS` (the two depth
    422s, the issue search's the bare ``/v3/search/`` with its trailing slash and code search's the
    ``#search-code`` anchor, the ref 404 the contents routes raise, whose is ``/v3/repos/contents/``,
    and that ``state`` 422 again, whose is ``/v3/issues/#list-issues``); or a ``message`` the
    exception's ``detail`` does not carry, the ref echoed back (that same ref 404, and the
    ``/commits/{sha}`` 422 of ``_no_commit_for_sha``, whose ``documentation_url`` is the route's
    own). Everything else is the three-member envelope over the exception's own detail, whose
    wording is already real's ("Not Found", "Bad credentials") because the routers were written
    against measured responses; only the shape around it was FastAPI's.

    A 405 is the exception, and keeps FastAPI's ``detail``. Every route here declares GET, so a
    wrong method is refused by Starlette with ``http.HTTPStatus(405).phrase`` — a string no
    measurement attributes to real, which answers a wrong method per endpoint rather than
    uniformly (an unauthenticated ``POST /repos/{owner}/{repo}`` is its 401 Requires
    authentication; an authenticated ``POST`` to `/search/code` or `/search/issues` is its 404 Not
    Found, measured 2026-09-07). Dressing Starlette's phrase in real's envelope would read as
    measured.
    """
    body = getattr(exc, "github_body", None)
    if isinstance(body, dict):
        return body
    if exc.status_code == 405:
        return None
    detail = exc.detail
    message = detail if isinstance(detail, str) else str(detail)
    return {
        "message": message,
        "documentation_url": DOCS_ROOT if exc.status_code == 401 else docs_url(path),
        "status": str(exc.status_code),
    }


def validation_body(path: str, errors) -> tuple[int, dict]:
    """A 422 from FastAPI's own request validation, as the status and body real would answer.

    A PATH parameter that will not parse is real's 404, not a 422: `/repos/{o}/{r}/issues/notanint`
    is `Not Found` with that route's own anchor, because real has no route for it to reach in the
    first place (measured). Backlot declares `number: int`, so it matches the route and fails after,
    which is a difference in how the two arrive at the answer rather than in the answer.

    Any path error decides it, not the first error reported: a request that fails on a path and a
    query parameter at once has one answer, and which of the two FastAPI happens to list first is
    not it.

    A QUERY parameter is the residue. Real's listings refuse no pagination value, which is why
    those absorb (see :func:`backlot.pagination._absorb_page`) and never reach here; the one
    measured route that does refuse one, `/search/code`, answers in `text/plain` with no envelope,
    and does so from the route itself, reading the raw query string after the validator has
    absorbed the value (see :func:`backlot.pagination.github_code_search_query_refusal`), so
    validation never fails there and this is never reached either. What is left is a shape real has
    no measured answer for, so it keeps the Validation Failed envelope real uses for a parameter it
    does refuse, with this route's anchor rather than the bare root.
    """
    if any(tuple(e.get("loc", ()))[:1] == ("path",) for e in errors):
        return 404, {
            "message": "Not Found",
            "documentation_url": docs_url(path),
            "status": "404",
        }
    entries = [
        {
            "resource": "Request",
            "field": str(e.get("loc", ["?"])[-1]),
            "code": "invalid",
            "message": e.get("msg", "invalid request"),
        }
        for e in errors
    ]
    return 422, {
        "message": "Validation Failed",
        "documentation_url": docs_url(path),
        "errors": entries,
        "status": "422",
    }
