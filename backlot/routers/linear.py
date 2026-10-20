"""Linear's GraphQL API, served at ``POST /linear/graphql``.

Linear is **GraphQL only** — there is no REST surface to emulate — so this router is one
endpoint. The schema and the resolvers live in ``backlot/graphql/`` (``linear.graphql`` +
``linear_resolvers.py``); everything here is HTTP: a body and credentials in, a GraphQL envelope
out, or Express's for the bodies :func:`_express` answers.

Auth is the one place Linear differs from every other source in this repo. A personal API key
travels as the **bare** header value (``Authorization: lin_api_…``, no scheme) while an OAuth
access token travels as ``Authorization: Bearer <token>`` — the same header, two shapes, both
accepted by the real API. :func:`backlot.auth.api_key_token` handles both.

Status codes follow the GraphQL-over-HTTP split :class:`backlot.graphql.engine.Result` marks; real
Linear draws the line in the same place, and generated clients branch on it.

The body is read before the credential is looked at, by Express's ``body-parser`` in front of the
GraphQL server: with no credential, a body that is not JSON drew its 400 from api.linear.app on
2026-10-18, not the 401. :func:`_express` is that 400, and each refusal ``body-parser`` makes before
it has the text (:func:`backlot.graphql.body.body_parser_text`); :func:`_read` names the bodies
answered in graphql-js's words instead.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from backlot import auth
from backlot.config import get_settings
from backlot.graphql import body as request_body
from backlot.graphql import engine
from backlot.graphql.linear_resolvers import build_engine

router = APIRouter(prefix="/linear", tags=["linear"])

# Built once at import: the SDL is parsed and validated and the resolver map bound, so a broken
# schema fails at startup instead of on the first request.
ENGINE = build_engine()


def _express(message: str, status: int = 400, length: int | None = None) -> Response:
    """Linear's answer to a body ``body-parser`` refuses: Express's envelope around ``message``,
    not a GraphQL one, as api.linear.app answered a 400 on 2026-10-18 with what
    :func:`~backlot.graphql.body.strict_violation` or
    :func:`~backlot.graphql.body.v8_constant_message` writes, and each
    :class:`~backlot.graphql.body.Unread` on 2026-10-19. A 413 for an uncompressed body carries
    ``extensions`` naming its ``length`` and the limit."""
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
    # In a worker thread: `_read` can take seconds over a body near the 10 MiB limit, which would
    # hold up every other request on the event loop.
    payload = await run_in_threadpool(
        _read,
        await request.body(),
        request.headers.get("content-type"),
        _content_encoding(request),
    )
    if isinstance(payload, Response):
        return payload
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
    return Response(
        request_body.node_json(result.payload),
        status_code=400 if result.request_error else 200,
        media_type="application/json",
    )


def _content_encoding(request: Request) -> str:
    """``Content-Encoding`` as Linear's ``body-parser`` reads it, the header sent once or more: each
    value without the spaces and tabs at its ends, which uvicorn keeps at the end, an empty one
    taken over by the value after it and any other joined to the next after ``", "``, and the whole
    without spaces and tabs at its end.

    api.linear.app answered each of 123 requests on 2026-10-20 (UTC), with one to four of the
    header, each empty, a space, a tab, a comma, ``gzip`` with or without spaces, tabs and commas
    beside it, ``gzip,gzip``, ``GZIP``, ``bogus`` or ``deflate``, as
    :func:`backlot.graphql.body.body_parser_text` reads the value this gives. The join is
    Cloudflare's, in front of Linear: Node 21 to 25 on their own join the values with ``", "``
    whatever they hold, so an empty value then ``gzip`` is ``", gzip"`` and its 415, where Notion,
    behind Cloudflare too, inflated that request as Linear did."""
    joined = ""
    for value in request.headers.getlist("content-encoding"):
        value = value.strip(" \t")
        joined = f"{joined}, {value}" if joined else value
    return joined.rstrip(" \t")


def _read(raw: bytes, content_type: str | None, content_encoding: str) -> Any:
    """The payload Linear reads from the body ``raw``, or the response this router refuses it with.

    Two refusals keep graphql-js's words here, before the credential as Linear's do: a body with no
    text once read, which Linear answers with Apollo Server's
    ``POST body missing, invalid Content-Type, or JSON object has no keys.``, and a body the reader
    refuses before it meets a ``NaN``, ``Infinity`` or ``-Infinity``, such as ``{`` or ``-NaN``,
    which Linear answers with V8's message."""
    try:
        text = request_body.body_parser_text(raw, content_type, content_encoding)
    except request_body.Unread as unread:
        return _express(unread.message, unread.status, unread.length)
    violation = request_body.strict_violation(text) if text else None
    if violation is not None:
        return _express(violation)
    try:
        return request_body.parse(text)
    except request_body.NotJson as exc:
        if exc.constant is not None:
            return _express(request_body.v8_constant_message(text, exc.constant))
        return JSONResponse(engine.not_json().payload, status_code=400)
