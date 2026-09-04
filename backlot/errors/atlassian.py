"""Atlassian Cloud's error envelope.

Atlassian clients (atlassian-python-api, which mcp-atlassian uses) parse an error body as Atlassian
Cloud's shape — Confluence's ``raise_for_status`` does ``response.json()["message"]`` — so FastAPI's
default ``{"detail": …}`` turns every error into a cryptic ``KeyError: 'message'`` inside the client
rather than the error Backlot meant to report. Both Jira's ``errorMessages`` list and Confluence's
scalar ``message`` are emitted, since one envelope serves both APIs here.
"""

from __future__ import annotations

import http

PREFIX = "/atlassian"

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


def owns(path: str) -> bool:
    return path.startswith(PREFIX)


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


def http_body(path: str, exc) -> dict:
    """``path`` is unused: one envelope covers every Atlassian route, unlike Google's per-family
    split. It is in the signature so the dispatch in ``__init__`` can treat every module alike."""
    return _body(exc.status_code, exc.detail)


def validation_body(path: str, errors) -> tuple[int, dict]:
    """A 422 from FastAPI's own request validation, in the same envelope. The per-field detail
    collapses to one sentence because that is the only part a client reads.

    ``path`` is unused, as in :func:`http_body`: one envelope covers every Atlassian route."""
    message = "; ".join(e.get("msg", "invalid request") for e in errors) or "Invalid request"
    return 422, _body(422, message)
