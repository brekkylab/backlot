"""GitHub's error envelope, for the errors this mock shapes on purpose.

Real GitHub answers an error as ``{"message", "documentation_url", "status"}`` — plus a
``"errors"`` member on a few, where it is sometimes a string rather than the usual array. That is
not FastAPI's ``{"detail": …}``, so converting the WHOLE github surface is a real fidelity
improvement; it is also its own change, touching every 404 the router raises and the tests that
assert them. It is deliberately not bundled here.

What this module carries is narrower: an envelope for an error the router builds deliberately and
would otherwise have to emit in a shape real never sends — today, the 400 that refuses an
unsupported ``X-GitHub-Api-Version``. The exception carries its own body as ``github_body``, the
same way :mod:`backlot.errors.google` carries its extra fields on the exception, and anything
without one falls through to FastAPI's default: ``http_body`` returning ``None`` is what
:mod:`backlot.errors` reads as "this vendor has nothing to say about that one".

So ``owns`` claiming every ``/github`` path costs nothing today and is the right seam for the
broader conversion when it happens.
"""

from __future__ import annotations


def owns(path: str) -> bool:
    return path.startswith("/github")


def http_body(path: str, exc) -> dict | None:
    """The github-shaped body the router attached to ``exc``, or ``None`` for FastAPI's default."""
    body = getattr(exc, "github_body", None)
    return body if isinstance(body, dict) else None


def validation_body(errors) -> dict | None:
    """Nothing to add: no github route answers a bad *parameter* through FastAPI's validator."""
    return None
