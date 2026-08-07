"""Fireflies.ai's GraphQL API, served at ``POST /fireflies/graphql``.

Fireflies is **GraphQL only** — there is no REST surface to emulate — so this router is one
endpoint. The schema and the resolvers live in ``backlot/graphql/`` (``fireflies.graphql`` +
``fireflies_resolvers.py``); everything here is HTTP: credentials in, a GraphQL envelope out.

Auth is the ordinary bearer path (``Authorization: Bearer <api_key>``), which is what the
vendor's own quickstart shows for all four of its raw-HTTP examples. Unlike Linear there is no
bare-header variant to accommodate.

Status codes follow the GraphQL-over-HTTP split the engine already draws: a **request** error
(unparseable document, failed validation, uncoercible variables) is a 400 with no ``data`` key,
while a **field** error mid-execution is a 200 carrying partial ``data`` alongside ``errors``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backlot import auth
from backlot.config import get_settings
from backlot.graphql.fireflies_resolvers import build_engine

router = APIRouter(prefix="/fireflies", tags=["fireflies"])

# Built once at import: the SDL is parsed and validated and the resolver map bound, so a broken
# schema fails at startup instead of on the first request.
ENGINE = build_engine()


@router.post("/graphql", include_in_schema=False)
async def graphql(request: Request):
    """The single Fireflies endpoint. Not in the OpenAPI document on purpose: this is a GraphQL
    service, and describing one POST route that accepts an arbitrary query would tell an
    OpenAPI→MCP bridge nothing useful (hence no ``SOURCE_PREFIXES`` entry either — see
    ``backlot/openapi.py``). Introspection is the schema description for this endpoint."""
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
        # user_id -> email, built once at startup (backlot.main), so `user(id:)` and the
        # `transcripts(user_id:)` filter can reverse a served id without scanning.
        "user_index": state.index.get("fireflies_users", {}),
        # The workspace roster: the addresses that can actually authenticate. `users` is scoped to
        # it, because the principals table holds every person the whole corpus names (16k on the
        # bench) and almost none of them have a Fireflies account. See resolve_users.
        "roster": frozenset(state.acl.email_to_token()) if getattr(state, "acl", None) else None,
    }
    result = ENGINE.execute_request(await request.body(), context=context)
    return JSONResponse(result.payload, status_code=400 if result.request_error else 200)
