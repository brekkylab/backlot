"""Linear's GraphQL API, served at ``POST /linear/graphql``.

Linear is **GraphQL only** — there is no REST surface to emulate — so this router is one
endpoint. The schema and the resolvers live in ``backlot/graphql/`` (``linear.graphql`` +
``linear_resolvers.py``); everything here is HTTP: a body and credentials in, a GraphQL envelope
out, or Express's for the bodies :func:`_not_json` answers.

Auth is the one place Linear differs from every other source in this repo. A personal API key
travels as the **bare** header value (``Authorization: lin_api_…``, no scheme) while an OAuth
access token travels as ``Authorization: Bearer <token>`` — the same header, two shapes, both
accepted by the real API. :func:`backlot.auth.api_key_token` handles both.

Status codes follow the GraphQL-over-HTTP split :class:`backlot.graphql.engine.Result` marks; real
Linear draws the line in the same place, and generated clients branch on it.

The body is read before the credential is looked at, by Express's ``body-parser`` in front of the
GraphQL server: with no credential, a body that is not JSON drew its 400 from api.linear.app on
2026-10-18, not the 401. :func:`_not_json` is that 400; :func:`graphql` names the bodies answered in
graphql-js's words instead.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
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


def _not_json(message: str) -> Response:
    """Linear's 400 for a body V8 refuses, ``message`` being what
    :func:`~backlot.graphql.body.strict_violation` or
    :func:`~backlot.graphql.body.v8_constant_message` writes: Express's envelope around it, not a
    GraphQL one, as api.linear.app answered on 2026-10-18."""
    return Response(
        request_body.node_json({"error": message, "details": {}, "code": 400}),
        status_code=400,
        media_type="application/json",
    )


@router.post("/graphql", include_in_schema=False)
async def graphql(request: Request):
    """The single Linear endpoint. Not in the OpenAPI document on purpose: this is a GraphQL
    service, and describing one POST route that accepts an arbitrary query would tell an
    OpenAPI→MCP bridge nothing useful (hence no ``SOURCE_PREFIXES`` entry either — see
    ``backlot/openapi.py``). Introspection is the schema description for this endpoint.

    Two refusals keep graphql-js's words here, before the credential as Linear's do: an empty
    body, which Linear answers with Apollo Server's ``POST body missing, invalid Content-Type, or
    JSON object has no keys.``, and a body the reader refuses before it meets a ``NaN``,
    ``Infinity`` or ``-Infinity``, such as ``{`` or ``-NaN``, which Linear answers with V8's
    message."""
    text = request_body.decode(await request.body())
    violation = request_body.strict_violation(text) if text else None
    if violation is not None:
        return _not_json(violation)
    try:
        payload = request_body.parse(text)
    except request_body.NotJson as exc:
        if exc.constant is not None:
            return _not_json(request_body.v8_constant_message(text, exc.constant))
        return JSONResponse(engine.not_json().payload, status_code=400)
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
    return JSONResponse(result.payload, status_code=400 if result.request_error else 200)
