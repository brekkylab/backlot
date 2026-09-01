"""Per-vendor error envelopes, and the dispatch that picks one by request path.

FastAPI's default error body is ``{"detail": …}``, which no real client parses: an Atlassian client
reads ``message``, ``google-api-python-client`` builds its ``HttpError`` from ``error.message``, and
both branch on fields that body does not have. So the vendors whose clients we drive get their own
envelope, one module each (:mod:`backlot.errors.atlassian`, :mod:`backlot.errors.google`), and
``backlot.main``'s exception handlers ask here rather than carrying a branch per vendor.

A module in ``_ENVELOPES`` provides:

- ``owns(path)`` — whether this vendor shapes errors for that path.
- ``http_body(path, exc)`` — the body for an ``HTTPException``. The exception itself, because a
  vendor may carry its extra fields as attributes on it (Google does).
- ``validation_body(path, errors)`` — the status and body for a request-validation failure, or
  ``None`` to keep FastAPI's own 422. Google is the one that keeps it: its editor APIs answer a bad
  *parameter* through a router-raised ``GoogleError``, so the validator is not the path that
  reports one.

Adding a vendor is a module plus one entry below — not an edit to the handler.
"""

from __future__ import annotations

from backlot.errors import atlassian, github, google

_ENVELOPES = (atlassian, github, google)


def http_body(path: str, exc) -> dict | None:
    """The vendor-shaped body for an ``HTTPException`` on ``path``, or ``None`` for FastAPI's."""
    for envelope in _ENVELOPES:
        if envelope.owns(path):
            return envelope.http_body(path, exc)
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


__all__ = ["atlassian", "github", "google", "http_body", "validation_body"]
