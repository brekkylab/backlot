"""Per-vendor error envelopes, and the dispatch that picks one by request path.

FastAPI's default error body is ``{"detail": …}``, which no real client parses: an Atlassian client
reads ``message``, ``google-api-python-client`` builds its ``HttpError`` from ``error.message``, and
both branch on fields that body does not have. So the vendors whose clients we drive get their own
envelope, one module each (:mod:`backlot.errors.atlassian`, :mod:`backlot.errors.google`), and
``backlot.main``'s exception handlers ask here rather than carrying a branch per vendor.

A module in ``_ENVELOPES`` provides:

- ``owns(path)`` — whether this vendor shapes errors for that path.
- ``http_body(path, exc, query)`` — the body for an ``HTTPException``. The exception itself,
  because a vendor may carry its extra fields as attributes on it (Google does), and the request's
  query parameters, because one vendor's envelope depends on one (Google's `$.xgafv`).
- ``validation_body(path, errors)`` — the status and body for a request-validation failure, or
  ``None`` to keep FastAPI's own 422. Google is the one that keeps it: its editor APIs answer a bad
  *parameter* through a router-raised ``GoogleError``, so the validator is not the path that
  reports one.
- ``method_not_allowed(path, method)``, optional — the vendor's own 405, as an exception carrying
  its body, media type and headers. The router raises a 405 before any vendor code runs, so a
  vendor whose 405 differs from the shape its other refusals take says so here. Atlassian is the
  one that implements it; what its two products answer is in
  :func:`backlot.errors.atlassian.method_not_allowed`. A vendor without it keeps the shared
  envelope.
- ``json_media_type(path, status_code)``, optional — the `content-type` the vendor puts on a JSON
  body answered at that path with that status, when it is measured to differ from FastAPI's bare
  `application/json`. GitHub is the one that implements it; a vendor without it keeps the default,
  which is not a claim about what real sends.
- ``rendered(request, status_code, body, headers)``, optional — the whole ``Response``, for a
  vendor whose error bodies are measured to the byte or whose status the body does not decide. Only
  Google has one: its errors are indented, its `content-type` carries a charset, and a `callback`
  on a GET turns one into a 200 JSONP script. The whole REQUEST, because that rendering reads the
  method as well as the query. A vendor without it gets FastAPI's ``JSONResponse`` around the dict,
  which is what Atlassian and GitHub keep — measured, Atlassian ignores `callback` on every route
  asked, and GitHub's JSONP is a different envelope under a different media type, so neither wants
  Google's renderer.

A media type that varies by REFUSAL rather than by path and status cannot come from
``json_media_type``, which sees only those two. It rides on the exception instead, as a
``media_type`` attribute ``backlot.main``'s handler reads: Atlassian's
:class:`~backlot.errors.atlassian.AtlassianError` carries `application/problem+json` for Jira's
type-conversion 400, where Jira's other 400s on the same path are plain JSON. A vendor that sets no
such attribute is unaffected.

Adding a vendor is a module plus one entry below — not an edit to the handler.
"""

from __future__ import annotations

from fastapi import Request, Response

from backlot.errors import atlassian, github, google

_ENVELOPES = (atlassian, github, google)


def http_body(path: str, exc, query=None) -> dict | None:
    """The vendor-shaped body for an ``HTTPException`` on ``path``, or ``None`` for FastAPI's.
    ``query`` is the request's query parameters, read by the envelope that needs them."""
    for envelope in _ENVELOPES:
        if envelope.owns(path):
            return envelope.http_body(path, exc, query)
    return None


def method_not_allowed(path: str, method: str):
    """The vendor's own 405 for ``method`` on ``path``, as an exception whose body, media type and
    headers ``backlot.main``'s handler reads, or ``None`` to keep the shared envelope.

    Its ``headers`` is three-valued: a mapping replaces what the router computed, ``{}`` sends none
    where the vendor sends none, and ``None`` keeps the router's `Allow`.
    """
    for envelope in _ENVELOPES:
        if envelope.owns(path):
            answer = getattr(envelope, "method_not_allowed", None)
            return answer(path, method) if answer is not None else None
    return None


def json_media_type(path: str, status_code: int) -> str | None:
    """The vendor's measured `content-type` for a JSON body answered on ``path`` with
    ``status_code``, or ``None`` for FastAPI's."""
    for envelope in _ENVELOPES:
        if envelope.owns(path):
            pick = getattr(envelope, "json_media_type", None)
            return pick(path, status_code) if pick is not None else None
    return None


def rendered(request: Request, status_code: int, body: dict, headers=None) -> Response | None:
    """The vendor's own rendering of an error on this request's path — the whole response rather
    than the body alone — or ``None`` to let the handler answer with a ``JSONResponse`` around
    ``body``.

    The whole response, because a vendor can decide more about an error than its dict: Google
    indents every error body and ends it with a newline whatever `prettyPrint` says, names a charset
    on the type, and answers a GET carrying `callback` at 200 as a script with the error inside it.
    The whole request, because that last one is decided by the method and the query together, where
    the other entry points here need only a path.
    """
    for envelope in _ENVELOPES:
        if envelope.owns(request.url.path):
            render = getattr(envelope, "rendered", None)
            return render(request, status_code, body, headers) if render is not None else None
    return None


def validation_body(path: str, errors) -> tuple[int, dict] | None:
    """The status and vendor-shaped body for a request-validation failure on ``path``, or ``None``
    to keep FastAPI's own 422.

    The STATUS as well as the body, because a vendor may answer this case with something other than
    422: a path parameter GitHub has no route for is its 404, not a validation failure.
    """
    for envelope in _ENVELOPES:
        if envelope.owns(path):
            return envelope.validation_body(path, errors)
    return None


__all__ = [
    "atlassian",
    "github",
    "google",
    "http_body",
    "json_media_type",
    "method_not_allowed",
    "rendered",
    "validation_body",
]
