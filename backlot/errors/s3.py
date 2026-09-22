"""S3's error envelope for the refusals no route handler builds.

`backlot.routers.s3` builds its own `<Error>` documents, so this module answers the one refusal the
router never sees: a method Starlette rejects before any route runs. The five methods real defines
an answer for at a bucket, a key or the service root are declared as routes there and answered
there. What is left is a method S3 defines nothing for at all, and real answers every one of those
the same way — measured 2026-09-22 with `TRACE`, `LINK` and `PROPFIND` at the service root: 400
`BadRequest`, "An error occurred when parsing the HTTP request.", `application/xml`, and no `Allow`,
where Starlette's own 405 sends a JSON body and an `Allow` naming whichever route it matched first.
"""

from __future__ import annotations

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

_XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'
_UNPARSEABLE = "An error occurred when parsing the HTTP request."


class S3Error(HTTPException):
    """A refusal whose body is the XML real sends, carried on the exception so ``backlot.main``'s
    handler stays a dispatch. ``detail`` is that document, which :func:`rendered` puts on the wire
    as XML rather than letting ``JSONResponse`` quote it."""

    def __init__(self, status_code: int, document: str, headers: dict | None = None):
        super().__init__(status_code=status_code, detail=document, headers=headers)


def owns(path: str) -> bool:
    return path == "/s3" or path.startswith("/s3/")


def http_body(path: str, exc, query=None) -> dict | None:
    """No opinion: every other S3 refusal is a ``Response`` the router already built."""
    return None


def rendered(request: Request, status_code: int, body: dict, headers=None) -> Response | None:
    """The XML :class:`S3Error` carries, or ``None`` for anything else under ``/s3``.

    The handler hands a body of ``{"detail": exc.detail}`` here when no envelope shaped one, so the
    document this module built arrives as that one string, and nothing else under ``/s3`` reaches
    the handler with an `<Error>` document in it."""
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail.startswith("<Error>"):
        return Response(
            content=_XML_DECL + detail,
            media_type="application/xml",
            status_code=status_code,
            headers=headers,
        )
    return None


def method_not_allowed(path: str, method: str) -> S3Error:
    """Real's answer to a method S3 defines nothing for: the parse 400, not a 405.

    ``headers`` carries the request id pair rather than being empty, because this is the one answer
    whose extended id real widens: sampled three times each on 2026-09-22, the parse 400's
    `x-amz-id-2` is 128 characters where the 405, the 404 and a 200 all carry 96. No `Allow` rides
    it — real sends none, where Starlette would compute one from the routes this server happens to
    declare — and the router's middleware leaves a header this sets alone.
    """
    from backlot.routers.s3 import REQUEST_IDS, wide_extended_id

    ids = REQUEST_IDS.get()
    if ids is None:
        return S3Error(
            400, f"<Error><Code>BadRequest</Code><Message>{_UNPARSEABLE}</Message></Error>", {}
        )
    request_id, extended = ids[0], wide_extended_id(ids)
    tail = f"<RequestId>{request_id}</RequestId><HostId>{extended}</HostId>"
    return S3Error(
        400,
        f"<Error><Code>BadRequest</Code><Message>{_UNPARSEABLE}</Message>{tail}</Error>",
        {"x-amz-request-id": request_id, "x-amz-id-2": extended},
    )
