"""Linear's GraphQL API, served at ``POST /linear/graphql``.

Linear is **GraphQL only** — there is no REST surface to emulate — so this router is one
endpoint. The schema and the resolvers live in ``backlot/graphql/`` (``linear.graphql`` +
``linear_resolvers.py``); everything here is HTTP: credentials in, a GraphQL envelope out.

Auth is the one place Linear differs from every other source in this repo. A personal API key
travels as the **bare** header value (``Authorization: lin_api_…``, no scheme) while an OAuth
access token travels as ``Authorization: Bearer <token>`` — the same header, two shapes, both
accepted by the real API. :func:`backlot.auth.api_key_token` handles both.

Status codes follow the GraphQL-over-HTTP split the engine already draws: a **request** error
(unparseable document, failed validation, uncoercible variables) is a 400 with no ``data`` key,
while a **field** error mid-execution is a 200 carrying partial ``data`` alongside ``errors``.
Real Linear draws the line in the same place, and generated clients branch on it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backlot import auth
from backlot.config import get_settings
from backlot.graphql.linear_resolvers import build_engine

router = APIRouter(prefix="/linear", tags=["linear"])

# Built once at import: the SDL is parsed and validated and the resolver map bound, so a broken
# schema fails at startup instead of on the first request.
ENGINE = build_engine()


@router.post("/graphql", include_in_schema=False)
async def graphql(request: Request):
    """The single Linear endpoint. Not in the OpenAPI document on purpose: this is a GraphQL
    service, and describing one POST route that accepts an arbitrary query would tell an
    OpenAPI→MCP bridge nothing useful (hence no ``SOURCE_PREFIXES`` entry either — see
    ``backlot/openapi.py``). Introspection is the schema description for this endpoint."""
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
        # uuid/identifier -> doc_id and uuid/key -> team, built once at startup (backlot.main).
        "index": state.index.get("linear", {}),
        "team_index": state.index.get("linear_teams", {}),
        # Reverse maps for the by-id roots the SDK's lazy relation accessors call.
        "user_index": state.index.get("linear_users", {}),
        "state_index": state.index.get("linear_states", {}),
        "project_index": state.index.get("linear_projects", {}),
        "cycle_index": state.index.get("linear_cycles", {}),
        "label_index": state.index.get("linear_labels", {}),
        "release_index": state.index.get("linear_releases", {}),
    }
    result = ENGINE.execute_request(await request.body(), context=context)
    return JSONResponse(result.payload, status_code=400 if result.request_error else 200)
