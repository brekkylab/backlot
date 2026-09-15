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


def is_confluence(path: str) -> bool:
    """Which of the two products serves ``path``. They answer several things differently — the
    refusal envelope below, and how a query parameter is read — and one predicate keeps the
    callers from drifting apart on what counts as Confluence."""
    return path.startswith(WIKI)


# Java's `Character.isWhitespace` is documented to EXCLUDE the three non-breaking spaces and to
# include the other Unicode space separators. Measured on both products, 2026-09-15: a value
# holding U+00A0, U+2007 or U+202F between its digits is a 400 naming that value, while the same
# value holding U+2003, U+0020, a tab or a newline is thirty-four. Python's own `str.isspace()`
# calls all seven whitespace, so the three are put back by hand.
_JAVA_NON_WHITESPACE = "\u00a0\u2007\u202f"


def strip_java_whitespace(raw: str) -> str:
    """``raw`` with every character Java calls whitespace removed — not trimmed, removed: real
    reads `?maxResults=3%204` as thirty-four and names `?limit=a%20b` as ``"ab"``."""
    return "".join(ch for ch in raw if not ch.isspace() or ch in _JAVA_NON_WHITESPACE)


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
    it whitespace and all (`?maxResults=%20abc%20` names `' abc '`), Confluence names it with the
    whitespace REMOVED — the same cleaning it converts by, so `?limit=a%20b` names `"ab"` and a
    whitespace-only value names `""`. The cleaning is per value and the join comes after, which is
    the only order that matches all three of `?limit=%20abc%20&limit=2` naming `"abc,2"`,
    `?limit=abc&limit=%202%20` naming `"abc,2"` and `?limit=%20&limit=2` naming `",2"`.
    """
    if is_confluence(path):
        shown = ",".join(strip_java_whitespace(v) for v in values)
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
    # Masked to 32 bits: `Integer.toHexString(hashCode())` is at most eight digits, where CPython's
    # `id` is a heap address and renders nine here — which would also put a live address on the wire.
    shown = values[0] if len(values) == 1 else f"[Ljava.lang.String;@{id(values) & 0xFFFFFFFF:x}"
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


def unsupported_media_type(path: str, content_type: str | None) -> AtlassianError:
    """Jira's 415 for a POST body it will not read, measured 2026-09-15 on `POST search/jql`.

    RFC 7807 again, the same five keys and the same media type as
    :func:`integer_conversion_failure`. The ``detail`` names the type that arrived, and a request
    carrying no ``Content-Type`` at all is named ``'null'`` — the literal string, which is the
    header's absence rendered by a Java formatter rather than a JSON null.
    """
    return AtlassianError(
        415,
        {
            "type": "about:blank",
            "title": "Unsupported Media Type",
            "status": 415,
            "detail": f"Content-Type '{content_type or 'null'}' is not supported.",
            "instance": _instance(path),
        },
        media_type=PROBLEM_JSON,
    )


# The three sentences Jira answers a POST body it cannot turn into an object, measured 2026-09-15.
# Each arrives as `errorMessages` ALONE — no `errors`, where the refusals below it carry one — so
# they go through :class:`AtlassianError` rather than :func:`_body`.
BODY_EMPTY = "No content to map to Object due to end of input"
BODY_UNPARSEABLE = "There was an error parsing JSON. Check that your request body is valid."
BODY_NOT_AN_OBJECT = "Invalid request payload. Refer to the REST API documentation and try again."


def body_not_read(message: str) -> AtlassianError:
    """A 400 for a body that did not deserialize. ``message`` is one of the three above."""
    return AtlassianError(400, {"errorMessages": [message]})


def bad_page_token() -> AtlassianError:
    """Jira's 400 for a ``nextPageToken`` it cannot decode, measured 2026-09-15 on both methods —
    the query string's token and the body's are refused alike.

    The sentence is Backlot's own, not a transcription: real localises this one to the account's
    language, as it does the ``orderBy`` refusal (see ``routers.atlassian._jira_order_desc``), and
    the account it was measured against answers in Korean. The envelope is reproduced, the wording
    is not.
    """
    return AtlassianError(
        400,
        {
            "errorMessages": ["The provided nextPageToken is invalid or has expired."],
            "errors": {},
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
    """``path`` and ``query`` are both unused: one envelope covers every Atlassian route, unlike
    Google's per-family split, and no Atlassian refusal is selected by a query parameter the way
    Google's is by `$.xgafv`. They are in the signature so the dispatch in ``__init__`` can treat
    every module alike.

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
