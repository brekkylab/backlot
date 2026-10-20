"""Fireflies.ai's GraphQL API, served at ``POST /fireflies/graphql``.

Fireflies is **GraphQL only** — there is no REST surface to emulate — so this router is one
endpoint. The schema and the resolvers live in ``backlot/graphql/`` (``fireflies.graphql`` +
``fireflies_resolvers.py``); everything here is HTTP: a body and credentials in, a GraphQL
envelope out, or its HTTP layer's for a body it cannot read (:func:`_body_refusal`).

Auth is the ordinary bearer path (``Authorization: Bearer <api_key>``), which is what the
vendor's own quickstart shows for all four of its raw-HTTP examples. Unlike Linear there is no
bare-header variant to accommodate.

Status codes follow the GraphQL-over-HTTP split :class:`backlot.graphql.engine.Result` marks.

A body Fireflies cannot read is refused ahead of anything about the credential: with no
credential, and with ``Authorization: nope``, an empty body and a body that is not JSON drew
:func:`_body_refusal`'s 400 from api.fireflies.ai on 2026-10-18. ``Content-Encoding`` and the
charset go unread: on 2026-10-19 a gzipped body drew the not-JSON 400, and ``charset=latin1``,
``charset=utf-nope`` and ``Content-Encoding: bogus`` were served.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from backlot import auth
from backlot.config import get_settings
from backlot.graphql import body as request_body
from backlot.graphql.fireflies_resolvers import build_engine

router = APIRouter(prefix="/fireflies", tags=["fireflies"])

# Built once at import: the SDL is parsed and validated and the resolver map bound, so a broken
# schema fails at startup instead of on the first request.
ENGINE = build_engine()

_EMPTY = "Body cannot be empty when content-type is set to 'application/json'"
_NOT_JSON = "Body is not valid JSON but content-type is set to 'application/json'"
_TOO_LARGE = "Request body is too large"
#: The most bytes Fireflies reads: api.fireflies.ai served a body of 1048576 bytes and answered one
#: of 1048577 with :data:`_TOO_LARGE`'s 413 on 2026-10-19, with a credential, with none and with
#: ``Authorization: nope``.
_LIMIT = 1024 * 1024


def _body_refusal(message: str, status: int = 400) -> JSONResponse:
    """Fireflies' answer to a body it cannot read: an envelope of its HTTP layer, not a GraphQL
    one, with the message twice. :data:`_EMPTY` is the 400 for a body of no bytes and
    :data:`_NOT_JSON` the 400 for every other one :mod:`backlot.graphql.body` cannot read or finds
    :func:`~backlot.graphql.body.prototype_poisoned`, as api.fireflies.ai answered on 2026-10-18;
    :data:`_TOO_LARGE` is the 413 for one over :data:`_LIMIT`."""
    return JSONResponse(
        {
            "statusCode": status,
            "status": False,
            "code": "http",
            "message": message,
            "errorMessage": message,
        },
        status_code=status,
    )


@router.post("/graphql", include_in_schema=False)
async def graphql(request: Request):
    """The single Fireflies endpoint. Not in the OpenAPI document on purpose: this is a GraphQL
    service, and describing one POST route that accepts an arbitrary query would tell an
    OpenAPI→MCP bridge nothing useful (hence no ``SOURCE_PREFIXES`` entry either — see
    ``backlot/openapi.py``). Introspection is the schema description for this endpoint."""
    # Off the event loop, as a body of `_LIMIT` bytes can keep `_read` busy a third of a second.
    payload = await run_in_threadpool(_read, await request.body())
    if isinstance(payload, JSONResponse):
        return payload
    caller = auth.resolve_bearer(request)
    if caller is None:
        # Real Fireflies answers a bad credential with a GraphQL error envelope and a 401, not a
        # framework 403 — clients parse `errors[0].message`.
        return JSONResponse(
            {
                "errors": [
                    {
                        "message": "Please provide a valid API key",
                        "extensions": {"code": "unauthorized"},
                    }
                ]
            },
            status_code=401,
        )
    state = request.app.state
    context = {
        "conn": auth.conn(request),
        "visible_ids": auth.visible_ids(request, caller),
        "caller_email": None if caller.is_admin else caller.email,
        "org": getattr(state.acl, "org_name", None),
        "org_domain": get_settings().org_domain,
        # The workspace roster: the addresses that can actually authenticate. `users` is scoped to
        # it, because the principals table holds every person the whole corpus names (16k at the
        # scale measured) and almost none of them have a Fireflies account. See resolve_users.
        "roster": frozenset(state.acl.email_to_token()) if getattr(state, "acl", None) else None,
    }
    result = ENGINE.execute_payload(payload, context=context)
    return Response(
        request_body.node_json(result.payload),
        status_code=400 if result.request_error else 200,
        media_type="application/json",
    )


def _read(raw: bytes) -> Any:
    """The payload Fireflies reads from the body ``raw``, or :func:`_body_refusal`'s refusal of
    it."""
    if len(raw) > _LIMIT:
        return _body_refusal(_TOO_LARGE, 413)
    if not raw:
        return _body_refusal(_EMPTY)
    try:
        payload = request_body.parse(request_body.decode(raw))
    except request_body.NotJson:
        return _body_refusal(_NOT_JSON)
    if request_body.prototype_poisoned(payload):
        return _body_refusal(_NOT_JSON)
    return payload
