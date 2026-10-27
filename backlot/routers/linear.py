"""Linear's GraphQL API, served at ``POST /linear/graphql``.

Linear is **GraphQL only** — there is no REST surface to emulate — so this router is one
endpoint. The schema and the resolvers live in ``backlot/graphql/`` (``linear.graphql`` +
``linear_resolvers.py``); everything here is HTTP: a body and credentials in, a GraphQL envelope
out, or Express's for the bodies :func:`_express` answers.

Auth is the one place Linear differs from every other source in this repo. A personal API key
travels as the **bare** header value (``Authorization: lin_api_…``, no scheme), read by
:func:`backlot.auth.api_key_token`, and a Backlot token is one. ``Bearer <token>`` is how an OAuth
access token travels, and what Linear takes for an API key there
(:func:`backlot.auth.linear_api_key_under_bearer`) is :func:`_api_key_as_bearer`'s 400.

Status codes follow the GraphQL-over-HTTP split :class:`backlot.graphql.engine.Result` marks; real
Linear draws the line in the same place, and generated clients branch on it.

Express's ``body-parser``, in front of the GraphQL server, reads the body before the credential is
looked at, for any credential but an API key under ``Bearer``: with no credential, a body that is
not JSON drew its 400 from api.linear.app on 2026-10-18, not the 401. :func:`_express` is that 400,
and each refusal ``body-parser`` makes before it has the text
(:func:`backlot.graphql.body.body_parser_text`); :func:`_read` names the bodies answered in
graphql-js's words instead. An API key under ``Bearer`` is refused ahead of the body: with
``Bearer <key>``, ``{``, an empty body and a body holding ``NaN`` drew :func:`_api_key_as_bearer`'s
400 the same day, and ``charset=latin1``, ``Content-Encoding: bogus`` and a body past the size
limit did on 2026-10-19, when a key in an API key's shape that Linear never issued drew it under
``Bearer`` with ``{``, ``NaN``, ``5``, ``charset=latin1`` and an empty body.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from backlot import auth
from backlot.config import get_settings
from backlot.graphql import body as request_body
from backlot.graphql import engine, v8_json
from backlot.graphql.linear_resolvers import build_engine

router = APIRouter(prefix="/linear", tags=["linear"])

# Built once at import: the SDL is parsed and validated and the resolver map bound, so a broken
# schema fails at startup instead of on the first request.
ENGINE = build_engine()


def _api_key_as_bearer() -> JSONResponse:
    """Linear's 400 for an API key under ``Bearer``, which
    :func:`backlot.auth.linear_api_key_under_bearer` finds, as api.linear.app sent it on
    2026-10-18."""
    return JSONResponse(
        {
            "errors": [
                {
                    "message": "It looks like you're trying to use an API key as a Bearer token. "
                    "Remove the Bearer prefix from the Authorization header.",
                    "extensions": {
                        "type": "invalid input",
                        "code": "INPUT_ERROR",
                        "statusCode": 400,
                        "userError": True,
                    },
                }
            ]
        },
        status_code=400,
    )


def _express(message: str, status: int = 400, length: int | None = None) -> Response:
    """Linear's answer to a body ``body-parser`` refuses: Express's envelope around ``message``,
    not a GraphQL one, as api.linear.app answered a 400 on 2026-10-18 with what
    :func:`~backlot.graphql.body.strict_violation` or :func:`backlot.graphql.v8_json.error` writes,
    and each :class:`~backlot.graphql.body.Unread` on 2026-10-19. A 413 for an uncompressed body
    carries ``extensions`` naming its ``length`` and the limit."""
    envelope: dict = {"error": message, "details": {}}
    if length is not None:
        envelope["extensions"] = {
            "code": "PAYLOAD_TOO_LARGE",
            "type": "payload too large",
            "statusCode": 413,
            "userError": True,
            "limit": request_body.LINEAR_LIMIT,
            "length": length,
        }
    envelope["code"] = status
    return Response(
        request_body.node_json(envelope), status_code=status, media_type="application/json"
    )


@router.post("/graphql", include_in_schema=False)
async def graphql(request: Request):
    """The single Linear endpoint. Not in the OpenAPI document on purpose: this is a GraphQL
    service, and describing one POST route that accepts an arbitrary query would tell an
    OpenAPI→MCP bridge nothing useful (hence no ``SOURCE_PREFIXES`` entry either — see
    ``backlot/openapi.py``). Introspection is the schema description for this endpoint."""
    if auth.linear_api_key_under_bearer(request):
        return _api_key_as_bearer()
    # In a worker thread: `_read` can take seconds over a body near the 10 MiB limit, which would
    # hold up every other request on the event loop.
    payload = await run_in_threadpool(
        _read,
        await request.body(),
        request.headers.get("content-type"),
        request_body.joined_header(request.headers.getlist("content-encoding")),
    )
    if isinstance(payload, Response):
        return payload
    # The telemetry middleware names the call by this document (backlot.telemetry.naming).
    request.state.graphql_payload = payload
    caller = auth.resolve_api_key(request)
    if caller is None:
        # Real Linear answers a bad credential with a GraphQL error envelope and a 401, not a
        # framework 403 — clients parse `errors[0].message`.
        return JSONResponse(
            {
                "errors": [
                    {
                        "message": "Authentication required",
                        "extensions": {
                            "type": "authentication error",
                            "userPresentableMessage": "Invalid API key",
                        },
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
        # `org` is the workspace slug in a linear.app URL; `org_domain` is what the
        # service identity's address is built from (an org NAME is not a domain).
        "org": getattr(state.acl, "org_name", None),
        "org_domain": get_settings().org_domain,
        # Every id the SDK's lazy relation accessors ask for resolves through a stored table: a
        # team through store.linear_team_by_served_id / linear_team_by_served_key, and the six
        # entities with no table of their own through `linear_entities`
        # (store.linear_entity_by_id, called by the resolvers' own `_by_id`).
    }
    result = ENGINE.execute_payload(payload, context=context)
    request.state.graphql_raised = result.raised
    return Response(
        request_body.node_json(result.payload),
        status_code=400 if result.request_error else 200,
        media_type="application/json",
    )


def _read(raw: bytes, content_type: str | None, content_encoding: str) -> Any:
    """The payload Linear reads from the body ``raw``, or the response this router refuses it with.

    A body with no text once read keeps graphql-js's words here, after the ``Bearer`` check and
    before the key is resolved, as Linear's refusal comes: Linear answers it with Apollo Server's
    ``POST body missing, invalid Content-Type, or JSON object has no keys.``"""
    try:
        text = request_body.body_parser_text(raw, content_type, content_encoding)
    except request_body.Unread as unread:
        return _express(unread.message, unread.status, unread.length)
    violation = request_body.strict_violation(text) if text else None
    if violation is not None:
        return _express(violation)
    try:
        return request_body.parse(text)
    except request_body.NotJson:
        if not text:
            return JSONResponse(engine.not_json().payload, status_code=400)
        return _express(v8_json.error(text))
