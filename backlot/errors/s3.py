"""S3's error envelope for the refusals no route handler builds.

`backlot.routers.s3` builds its own `<Error>` documents, so this module answers the one refusal the
router never sees: a method Starlette rejects before any route runs. The seven methods real
defines an answer for are declared as routes there, at a bucket, a key and the service root alike,
and answered there. What is left is a method S3 defines nothing for at all, and real answers each
one measured the same way — measured 2026-09-22 with `TRACE`, `LINK` and `PROPFIND` at the service
root, and 2026-09-23 against `s3.us-east-1.amazonaws.com` with `TRACE` at all three, `LINK` and a
made-up `FOOBAR` at a bucket and `PROPFIND` at a key: 400 `BadRequest`, "An error occurred when
parsing the HTTP request.", `application/xml`, and no `Allow`, where Starlette's own 405 sends a
JSON body and an `Allow` naming whichever route it matched first.
"""

from __future__ import annotations

import hashlib

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

    ``headers`` carries this answer's request id and nothing else, so that no `Allow` rides it —
    real sends none, where Starlette would compute one from the routes this server happens to
    declare. The id is not the shape ``backlot.routers.s3.request_ids`` gives S3's own answers: all
    200 measured 2026-09-23 (`TRACE` at a bucket in us-east-1 and ap-northeast-2, and in us-east-1
    `LINK` and `FOOBAR` at a bucket and `PROPFIND` at a key) were uppercase hex and none began with
    `0`, 190 of them at 16 characters, nine at 15 and one at 14, so this writes a 64-bit value in
    hex without padding. The middleware adds the extended id, as on every other answer, and leaves
    this id alone; the body names the same pair.
    """
    from backlot.routers.s3 import REQUEST_IDS

    ids = REQUEST_IDS.get()
    headers, tail = {}, ""
    if ids is not None:
        request_id = f"{int.from_bytes(hashlib.shake_256(ids[0].encode()).digest(8)):X}"
        headers = {"x-amz-request-id": request_id}
        tail = f"<RequestId>{request_id}</RequestId><HostId>{ids[1]}</HostId>"
    return S3Error(
        400,
        f"<Error><Code>BadRequest</Code><Message>{_UNPARSEABLE}</Message>{tail}</Error>",
        headers,
    )
