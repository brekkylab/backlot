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
- ``validation_body(errors)`` — the body for a 422 from request validation, or ``None`` to keep
  FastAPI's own. Only Atlassian overrides it: Google's editor APIs answer a bad *parameter* through
  a router-raised ``GoogleError``, and nothing reaches FastAPI's validator with a Google path.

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


def validation_body(path: str, errors) -> dict | None:
    """The vendor-shaped body for a 422 on ``path``, or ``None`` for FastAPI's."""
    for envelope in _ENVELOPES:
        if envelope.owns(path):
            return envelope.validation_body(errors)
    return None


__all__ = ["atlassian", "github", "google", "http_body", "validation_body"]
