"""Atlassian Cloud's error envelope.

Atlassian clients (atlassian-python-api, which mcp-atlassian uses) parse an error body as Atlassian
Cloud's shape — Confluence's ``raise_for_status`` does ``response.json()["message"]`` — so FastAPI's
default ``{"detail": …}`` turns every error into a cryptic ``KeyError: 'message'`` inside the client
rather than the error Backlot meant to report. Both Jira's ``errorMessages`` list and Confluence's
scalar ``message`` are emitted, since one envelope serves both APIs here.

One envelope, but not the ONLY one: a refusal real answers in a shape measured to differ carries
its own body on an :class:`AtlassianError` and is served verbatim. Reading a query parameter is
where the two products visibly part — see :func:`integer_conversion_failure`.
"""

from __future__ import annotations

import http

from fastapi import HTTPException

PREFIX = "/atlassian"
WIKI = f"{PREFIX}/wiki"

# Jira answers a type-conversion failure as RFC 7807, with a charset real puts on it and FastAPI
# does not. Measured on brekkylab.atlassian.net, 2026-09-14.
PROBLEM_JSON = "application/problem+json;charset=UTF-8"

# The two refusals Confluence gives a caller it will not serve, measured against
# ecosystem.atlassian.net and brekkylab.atlassian.net on 2026-09-04. A credential it read and
# rejected, and a request that carried none, both get the 403 below verbatim — the vendor reports
# its own exception class in the message, and a client that logs the body logs that. A Basic value
# it could not read gets a 401 instead, whose real body is Tomcat's HTML page titled "HTTP Status
# 401 - Unauthorized"; that title is the message here, because the envelope this module exists to
# emit is JSON and a client parsing `message` out of the page would find nothing.
CONFLUENCE_FORBIDDEN = (
    "com.atlassian.confluence.mvc.rest.common.exception.StacklessResponseStatusException: "
    '403 FORBIDDEN "Request rejected because caller cannot access Confluence"'
)
CONFLUENCE_UNAUTHORIZED = "Unauthorized"

# What a `<site>.atlassian.net` gateway answers a `Bearer` it cannot read as a Connect session
# JWT, on every Jira route — `serverInfo` and `field`, which need no credential, included. One
# `error` key and nothing else: not this module's envelope, and not Confluence's either, so it is
# returned as its own body rather than shaped. Measured on ecosystem.atlassian.net and
# brekkylab.atlassian.net on 2026-09-04.
CONNECT_TOKEN_UNREADABLE = "Failed to parse Connect Session Auth Token"


def connect_token_body() -> dict:
    return {"error": CONNECT_TOKEN_UNREADABLE}


def owns(path: str) -> bool:
    return path.startswith(PREFIX)


class AtlassianError(HTTPException):
    """A refusal whose body real sends verbatim, rather than through :func:`_body`.

    Carried on the exception the way Google's errors carry their own fields, so the router raises
    one expression and ``backlot.main``'s handler stays a dispatch. ``media_type`` rides along
    because the body and the header are one measurement: Jira's RFC 7807 body arrives on
    ``application/problem+json``, and serving the body under FastAPI's ``application/json`` would
    reproduce half of it.
    """

    def __init__(self, status_code: int, body: dict, *, media_type: str | None = None):
        super().__init__(status_code=status_code, detail=body.get("detail") or body.get("message"))
        self.body = body
        self.media_type = media_type


def _instance(path: str) -> str:
    """The path real puts in an RFC 7807 ``instance``, which is the vendor's own, not Backlot's."""
    return path[len(PREFIX) :] if path.startswith(PREFIX) else path


def integer_conversion_failure(path: str, name: str, values: list[str]) -> AtlassianError:
    """The 400 either product answers for a query parameter it cannot read as an ``int``.

    Measured on brekkylab.atlassian.net, 2026-09-14, on `GET /rest/api/3/issue/{key}/comment` and
    `GET /wiki/rest/api/space`. The two bodies share no key: Jira's is RFC 7807 naming the
    parameter, Confluence's is two keys carrying the Spring exception's own ``toString``, which
    names the value and the type it could not reach but never the parameter.

    ``values`` is every value the query string carried for ``name``, because what real reports for
    a REPEATED parameter is the whole array rather than the one value it tried: Confluence renders
    it as the comma-join, Jira as a Java array's ``toString``. The identity hash in Jira's differs
    per request on real, so a Python ``id`` stands in — equally arbitrary, equally not a promise.

    The products also disagree on whether the value they name is the one that ARRIVED: Jira echoes
    it whitespace and all (`?maxResults=%20abc%20` names `' abc '`), Confluence names the trimmed
    one (the same value names `"abc"`, and a whitespace-only value names `""`).
    """
    if path.startswith(WIKI):
        shown = ",".join(values).strip()
        java_type = "java.lang.String" if len(values) == 1 else "java.lang.String[]"
        return AtlassianError(
            400,
            {
                "statusCode": 400,
                "message": (
                    "org.springframework.web.method.annotation."
                    "MethodArgumentTypeMismatchException: Failed to convert value of type "
                    f"'{java_type}' to required type 'int'; nested exception is "
                    f'java.lang.NumberFormatException: For input string: "{shown}"'
                ),
            },
        )
    shown = values[0] if len(values) == 1 else f"[Ljava.lang.String;@{id(values):x}"
    return AtlassianError(
        400,
        {
            "type": "about:blank",
            "title": "Bad Request",
            "status": 400,
            "detail": f"Failed to convert '{name}' with value: '{shown}'",
            "instance": _instance(path),
        },
        media_type=PROBLEM_JSON,
    )


def negative_not_allowed(name: str) -> AtlassianError:
    """Confluence's refusal of a negative ``limit`` or ``start``, measured 2026-09-14 on both
    listings. Jira does NOT share it — a negative there clamps to the floor and answers 200 — so
    this is Confluence's alone, and its body is the bare exception string again."""
    return AtlassianError(
        400,
        {
            "statusCode": 400,
            "message": f"java.lang.IllegalArgumentException: {name} cannot be less than zero",
        },
    )


def _body(status_code: int, detail) -> dict:
    try:
        reason = http.HTTPStatus(status_code).phrase
    except ValueError:
        reason = "Error"
    message = detail if isinstance(detail, str) else str(detail)
    return {
        "statusCode": status_code,
        "message": message,
        "reason": reason,
        "errorMessages": [message],
        "errors": {},
    }


def http_body(path: str, exc, query=None) -> dict:
    """``path`` is unused: one envelope covers every Atlassian route, unlike Google's per-family
    split. It is in the signature so the dispatch in ``__init__`` can treat every module alike.

    An :class:`AtlassianError` is the exception to the one envelope and says so by carrying its
    own body, which is served as it stands."""
    body = getattr(exc, "body", None)
    return body if body is not None else _body(exc.status_code, exc.detail)


def validation_body(path: str, errors) -> tuple[int, dict]:
    """A 422 from FastAPI's own request validation, in the same envelope. The per-field detail
    collapses to one sentence because that is the only part a client reads.

    ``path`` is unused, as in :func:`http_body`: one envelope covers every Atlassian route."""
    message = "; ".join(e.get("msg", "invalid request") for e in errors) or "Invalid request"
    return 422, _body(422, message)
