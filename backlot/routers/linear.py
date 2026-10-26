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

Express's ``body-parser``, in front of the GraphQL server, reads the body before the credential is
looked at, for any credential but an API key under ``Bearer``: with no credential, a body that is
not JSON drew its 400 from api.linear.app on 2026-10-18, not the 401. :func:`_express` is that 400,
and each refusal ``body-parser`` makes before it has the text
(:func:`backlot.graphql.body.body_parser_text`). Apollo Server's checks come ahead of the credential
too, up to the choice of operation (:meth:`backlot.graphql.engine.Engine.resolve_payload`): with no
credential and with ``nope``, an empty body, ``{}``, an array, a ``query`` of ``5`` or ``null``, a
string ``variables``, an ``extensions`` or ``operationName`` of ``5``, a persisted query's hash
alone or one that does not match, a document that does not parse or validate and one of two
operations with no ``operationName`` drew what the key drew on 2026-10-25 (UTC), and a document
missing a variable the 401. An API key under ``Bearer`` is refused ahead of the body: with
``Bearer <key>``, ``{``, an empty body and a body holding ``NaN`` drew
:func:`_api_key_as_bearer`'s 400 on 2026-10-18, and ``charset=latin1``, ``Content-Encoding: bogus``
and a body past the size limit did on 2026-10-19, when a key in an API key's shape that Linear never
issued drew it under ``Bearer`` with ``{``, ``NaN``, ``5``, ``charset=latin1`` and an empty body.
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

# Linear's code for a body with no query, where Apollo Server's is `BAD_REQUEST`.
_INVALID_INPUT = "INVALID_INPUT"

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
    resolved = ENGINE.resolve_payload(_variables_read(payload))
    if isinstance(resolved, engine.Result):
        return _answer(request, resolved, payload)
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
    return _answer(request, ENGINE.run(resolved, context=context), payload)


def _answer(request: Request, result: engine.Result, payload: Any) -> Response:
    request.state.graphql_raised = result.raised
    return Response(
        request_body.node_json(_envelope(result, payload)),
        status_code=result.status,
        media_type="application/json",
    )


def _variables_read(payload: Any) -> Any:
    """``payload`` with a string ``variables`` read as JSON where ``JSON.parse`` reads it: Linear
    served ``{"variables": "{}"}``, ``" {} "``, ``"null"`` and a string holding an object its
    operation used on 2026-10-25 (UTC), and answered one ``JSON.parse`` refuses (``"{"``,
    ``""``, ``"NaN"``) or reads as a string with Apollo Server's refusal of a string, and one it
    reads as an array or a number with the refusal of a value that is not an object."""
    if isinstance(payload, dict) and isinstance(payload.get("variables"), str):
        try:
            return {**payload, "variables": request_body.parse(payload["variables"])}
        except request_body.NotJson:
            pass
    return payload


# The `type` beside each code in Linear's errors of 2026-10-25 (UTC), which also carry Apollo
# Server's `http` member on each request error but a variable's refusal, and `userError`.
_TYPE = {engine.INTERNAL_SERVER_ERROR: "internal error", _INVALID_INPUT: "invalid input"}


def _apollo(message: str, code: str, status: int, locations: Any = ()) -> dict:
    entry: dict = {"message": message}
    if locations:
        entry["locations"] = list(locations)
    extensions: dict = (
        {} if code == engine.BAD_USER_INPUT else {"http": {"status": status, "headers": {}}}
    )
    extensions["code"] = code
    extensions["type"] = _TYPE.get(code, "graphql error")
    extensions["userError"] = code != engine.INTERNAL_SERVER_ERROR
    entry["extensions"] = extensions
    return entry


def _internal(error: dict) -> dict:
    """Linear's ``Internal server error`` in place of ``error``, at its ``path`` and ``locations``:
    api.linear.app's answer to a ``null`` variable a non-null argument refused, on 2026-10-21
    (UTC)."""
    entry: dict = {"message": "Internal server error"}
    for key in ("path", "locations"):
        if key in error:
            entry[key] = error[key]
    entry["extensions"] = {
        "code": engine.INTERNAL_SERVER_ERROR,
        "type": "internal error",
        "userError": False,
    }
    return entry


def _operation_missing(name: Any) -> dict:
    """Linear's own refusal of a document whose operation cannot be chosen, two operations and no
    ``operationName`` or a name no operation has, in the words api.linear.app used on 2026-10-25
    (UTC): an empty name in those for no name."""
    presentable = (
        f"The operation '{name}' does not exist on the query."
        if name
        else "The operation does not exist on the query."
    )
    return {
        "message": "operation does not exist",
        "extensions": {
            "type": "invalid input",
            "code": "INPUT_ERROR",
            "statusCode": 400,
            "userError": True,
            "userPresentableMessage": presentable,
            "http": {"status": 400},
        },
    }


def _envelope(result: engine.Result, payload: Any) -> dict:
    """``result`` as Linear writes it: a request error in Linear's members (:func:`_apollo`), but a
    body with no query under Linear's ``INVALID_INPUT``, a persisted query refused for its version
    or its hash as Linear's ``Internal server error``, an operation that cannot be chosen as
    :func:`_operation_missing`, and the request error after fifty refused variables, to which
    Apollo Server gives no code of its own, as :func:`_internal`, as api.linear.app answered it
    on 2026-10-26 (UTC); and each error in ``result.assigned`` as :func:`_internal`."""
    if result.refusals:
        errors = []
        for refusal in result.refusals:
            if refusal.code == engine.OPERATION_RESOLUTION_FAILURE:
                errors.append(_operation_missing(payload.get("operationName")))
            elif refusal.message in (
                engine.PERSISTED_QUERY_VERSION,
                engine.PERSISTED_QUERY_MISMATCH,
            ):
                errors.append(
                    _apollo("Internal server error", engine.INTERNAL_SERVER_ERROR, refusal.status)
                )
            elif refusal.message == engine.NO_QUERY:
                errors.append(_apollo(refusal.message, _INVALID_INPUT, refusal.status))
            elif refusal.code == engine.INTERNAL_SERVER_ERROR:
                errors.append(_internal({}))
            else:
                errors.append(
                    _apollo(refusal.message, refusal.code, refusal.status, refusal.locations)
                )
        return {"errors": errors}
    if not result.assigned:
        return result.payload
    errors = [
        _internal(error) if i in result.assigned else error
        for i, error in enumerate(result.payload["errors"])
    ]
    return {**result.payload, "errors": errors}


def _read(raw: bytes, content_type: str | None, content_encoding: str) -> Any:
    """The payload Linear reads from the body ``raw``, or the response this router refuses it with.

    A body with no text once read is read as an empty object, which Apollo Server refuses as it
    refuses one sent: Linear answered both with ``POST body missing, invalid Content-Type, or JSON
    object has no keys.`` on 2026-10-25 (UTC)."""
    try:
        text = request_body.body_parser_text(raw, content_type, content_encoding)
    except request_body.Unread as unread:
        return _express(unread.message, unread.status, unread.length)
    if not text:
        return {}
    violation = request_body.strict_violation(text)
    if violation is not None:
        return _express(violation)
    try:
        return request_body.parse(text)
    except request_body.NotJson:
        return _express(v8_json.error(text))
