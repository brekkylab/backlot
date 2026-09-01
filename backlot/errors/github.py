"""GitHub's error envelope.

Real GitHub answers an error as ``{"message", "documentation_url", "status"}``, plus an ``errors``
member on the kinds that carry one. That is not FastAPI's ``{"detail": …}``, and the difference is
not cosmetic: PyGithub — the SDK ``examples/using-official-sdk/github.py`` drives — picks its
exception CLASS off the body's ``message``, so the same wrong token raises
``BadCredentialsException`` against real and a bare ``GithubException`` against a server answering
``detail`` (measured against both). A client's error handling could not be written here.

``documentation_url`` is per-ENDPOINT and measured, not derived: requesting each route shape against
a repository that does not exist returns that route's own docs anchor, which is where
:data:`ROUTE_DOCS` comes from (every route below, none inferred from another).

An authentication failure is the exception to that: real answers those with the bare
``https://docs.github.com/rest``, measured on a bad bearer at
``/repos/{owner}/{repo}/collaborators`` and at ``/user/repos``. Real is not uniform there and
Backlot does not follow it all the way — ``/user/repos`` with NO Authorization header carries that
route's anchor, while a bad token on the same route carries the root. Backlot answers the root for
both 401s, which is this module's one knowing divergence.
"""

from __future__ import annotations

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
    "/github/repos/{owner}/{repo}/branches/{branch}": _DOCS + "branches/branches#get-a-branch",
    "/github/repos/{owner}/{repo}/commits/{sha}": _DOCS + "commits/commits#get-a-commit",
    "/github/repos/{owner}/{repo}/readme": _DOCS + "repos/contents#get-a-repository-readme",
    "/github/repos/{owner}/{repo}/collaborators": (
        _DOCS + "collaborators/collaborators#list-repository-collaborators"
    ),
    "/github/orgs/{org}/teams": _DOCS + "teams/teams#list-teams",
    "/github/repos/{owner}/{repo}/teams": _DOCS + "repos/repos#list-repository-teams",
}

# The two routes whose final parameter is a path converter, so it takes every remaining segment:
# `git/ref/{ref:path}` resolves a ref like `heads/release/2026-03`, and `contents/{path:path}` a
# nested file. The OpenAPI spec this table is keyed on writes both as an ordinary `{name}`, which is
# why they are named here instead of being inferred — letting any trailing placeholder run greedy
# would hand `/repos/{owner}/{repo}` every deeper path that failed to match something longer.
_TRAILING_PATH_ROUTES = frozenset(
    {
        "/github/repos/{owner}/{repo}/git/ref/{ref}",
        "/github/repos/{owner}/{repo}/contents/{path}",
    }
)


def owns(path: str) -> bool:
    return path.startswith(PREFIX)


def _matches(template: str, segments: list[str]) -> bool:
    """Whether a concrete path's ``segments`` fit ``template``."""
    parts = template.strip("/").split("/")
    greedy = template in _TRAILING_PATH_ROUTES
    if len(parts) > len(segments) or (not greedy and len(parts) != len(segments)):
        return False
    for i, part in enumerate(parts):
        placeholder = part.startswith("{") and part.endswith("}")
        if greedy and placeholder and i == len(parts) - 1:
            return True  # swallows segments[i:]
        if not placeholder and part != segments[i]:
            return False
    return len(parts) == len(segments)


def docs_url(path: str) -> str:
    """The anchor real names for the route ``path`` reaches, or the root for anything else.

    First match wins, in the router's own declaration order, because two templates can cover one
    path and FastAPI resolves those the same way.

    The root is also the honest answer for a path that matches no route: real has no endpoint
    documentation to point at for a URL it does not serve either.
    """
    segments = path.strip("/").split("/")
    for template, url in ROUTE_DOCS.items():
        if _matches(template, segments):
            return url
    return DOCS_ROOT


def http_body(path: str, exc) -> dict:
    """Render an exception into the envelope.

    An error the router shaped by hand carries its own body as ``github_body`` — the version 400
    and the search 422, both of which have an ``errors`` member no generic rendering could invent —
    and that wins. Everything else is the three-member envelope over the exception's own detail,
    whose wording is already real's ("Not Found", "Bad credentials") because the routers were
    written against measured responses; only the shape around it was FastAPI's.
    """
    body = getattr(exc, "github_body", None)
    if isinstance(body, dict):
        return body
    detail = exc.detail
    message = detail if isinstance(detail, str) else str(detail)
    return {
        "message": message,
        "documentation_url": DOCS_ROOT if exc.status_code == 401 else docs_url(path),
        "status": str(exc.status_code),
    }


def validation_body(errors) -> dict:
    """A 422 from FastAPI's own request validation, in real's Validation Failed envelope.

    Real reaches this case far less often than Backlot does — it refuses no pagination value at all
    (measured: ``per_page=0``, ``per_page=abc``, ``page=0``, ``page=-1`` are each a 200
    with the defaults applied), which is why the page parameters here no longer declare a floor and
    clamp instead. What is left arriving at FastAPI's validator is a parameter whose TYPE will not
    parse, and real answers those per route rather than uniformly — a non-integer issue number is
    its 404, not a 422. So this is the closest real shape for a case real does not have, not a
    measured reproduction of one: the envelope is the search 422's, whose ``errors`` entries name
    the offending field.
    """
    entries = [
        {
            "resource": "Request",
            "field": str(e.get("loc", ["?"])[-1]),
            "code": "invalid",
            "message": e.get("msg", "invalid request"),
        }
        for e in errors
    ]
    return {
        "message": "Validation Failed",
        "documentation_url": DOCS_ROOT,
        "errors": entries,
        "status": "422",
    }
