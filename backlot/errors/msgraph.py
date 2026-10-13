"""Microsoft Graph's error envelope.

The stable machine-readable contract is ``error.code``. Microsoft explicitly warns callers not
to depend on the human-readable message (https://learn.microsoft.com/graph/errors).

Anonymous GET https://graph.microsoft.com/v1.0/me was measured again on 2026-10-08. No real token
or tenant was used. Every case answered 401 / InvalidAuthenticationToken:

- no Authorization header: ``Access token is empty.``
- ``Bearer`` or ``Bearer ``: ``UnableToParseTokens``
- ``Bearer nope`` (also lower/uppercase bearer, ``token nope``, ``Basic bm9wZTpub3Bl`` and bare
  ``nope``): ``Protocol 'Bearer' failed to validate because The token could not be read.``

Those last two messages differ from the patch author's 2026-09-02 observation (ArgumentNull and
IDX14100). Tests pin the fresh measurement, not those older strings. All nine responses contained
innerError with date, request-id and client-request-id, and no inner code. Backlot generates its
own correlation values, and does not yet echo a caller-supplied client-request-id.

The other status/code combinations are emulator choices informed by published examples, not
measurements against an authenticated tenant. In particular, hidden resources use the same 404
as missing resources for concealment; whether a real tenant returns 403 or 404 is unmeasured.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException


def owns(path: str) -> bool:
    """Whether this module shapes errors for ``path`` — the question ``backlot.errors`` asks every
    envelope."""
    return path.startswith("/msgraph")


class GraphError(HTTPException):
    """An error carrying the ``code`` a client branches on, and the inner code where Graph sends
    one. The message is still the ``detail``, so a plain ``HTTPException`` raised on a Graph path
    renders too — it just falls back to the status's generic code."""

    def __init__(self, status_code: int, code: str, message: str, *, inner: str | None = None):
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.inner = inner


# The code a bare HTTPException on a Graph path renders as, so a route that raises one still
# produces a body a client can branch on rather than a null code.
_GENERIC_CODE = {
    400: "badRequest",
    401: "InvalidAuthenticationToken",
    403: "accessDenied",
    404: "itemNotFound",
    405: "methodNotAllowed",
    422: "badRequest",
}


# GET https://graph.microsoft.com/v1.0/me, measured 2026-10-08 without a tenant.
# All nonempty malformed credentials in the table above returned this text.
JWT_MALFORMED_MESSAGE = "Protocol 'Bearer' failed to validate because The token could not be read."


def no_credentials() -> GraphError:
    """No Authorization header at all."""
    return GraphError(401, "InvalidAuthenticationToken", "Access token is empty.")


def empty_credentials() -> GraphError:
    """An Authorization header whose token part is blank (``Bearer`` and nothing after it).

    Its own message, because it is its own case live: a header that carried nothing is not the same
    as no header, and a client that distinguishes them is distinguishing something real."""
    return GraphError(401, "InvalidAuthenticationToken", "UnableToParseTokens")


def bad_token() -> GraphError:
    """A token that was presented and resolves to nobody."""
    return GraphError(401, "InvalidAuthenticationToken", JWT_MALFORMED_MESSAGE)


def not_found(message: str) -> GraphError:
    """The Teams 404: a team, channel or message that does not resolve — or that this caller may
    not see, which is answered the same way for the reason ``routers.msteams`` states."""
    return GraphError(404, "NotFound", message, inner="ItemNotFound")


def user_not_found(user_id: str) -> GraphError:
    """The directory's own 404, which names the id it could not resolve."""
    return GraphError(
        404,
        "Request_ResourceNotFound",
        f"Resource '{user_id}' does not exist or one of its queried reference-property objects "
        "are not present.",
    )


def bad_request(message: str) -> GraphError:
    return GraphError(400, "badRequest", message)


def _inner(code: str | None) -> dict:
    """The ``innerError`` real Graph sends on every error.

    ``date`` has no timezone suffix and no fractional seconds live ("2026-09-02T11:24:27"), so it
    is formatted rather than handed to ``isoformat()``. The two ids are equal here: live they
    differ only when the CLIENT supplied its own ``client-request-id``, which nothing in Backlot
    does yet.
    """
    request_id = str(uuid.uuid4())
    body = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "request-id": request_id,
        "client-request-id": request_id,
    }
    # Measured absent on the 401s; the Teams 404s are reported with one.
    return {"code": code, **body} if code else body


def http_body(path: str, exc: HTTPException, query=None) -> dict:
    detail = exc.detail
    message = detail if isinstance(detail, str) else str(detail)
    return {
        "error": {
            "code": getattr(exc, "code", None)
            or _GENERIC_CODE.get(exc.status_code, "generalException"),
            "message": message,
            "innerError": _inner(getattr(exc, "inner", None)),
        }
    }


def validation_body(path: str, errors) -> tuple[int, dict]:
    """The answer to a request FastAPI's own validator rejected.

    **400, not 422.** Graph has no 422 for a read: a parameter it cannot use is a ``badRequest``,
    and answering 422 would send a client down a branch the real service never takes. The status is
    part of the contract precisely so a vendor can say this.

    The routes here take their parameters off the raw request rather than through typed signatures,
    so nothing should reach the validator — but a body is shaped rather than left as
    ``{"detail": …}`` so that if one ever does, a Graph client still sees a Graph error.
    """
    return 400, {
        "error": {
            "code": "badRequest",
            "message": "The request is malformed or incorrect.",
            "innerError": _inner(None),
        }
    }
