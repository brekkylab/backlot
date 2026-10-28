"""Fireflies.ai's GraphQL API, served at ``POST /fireflies/graphql``.

Fireflies is **GraphQL only** — there is no REST surface to emulate — so this router is one
endpoint. The schema and the resolvers live in ``backlot/graphql/`` (``fireflies.graphql`` +
``fireflies_resolvers.py``); everything here is HTTP: a body and credentials in, a GraphQL
envelope out, or its HTTP layer's for a body it cannot read (:func:`_body_refusal`).

Auth is the bearer path (``Authorization: Bearer <api_key>``), which is what the vendor's own
quickstart shows for all four of its raw-HTTP examples, read by
:func:`backlot.auth.fireflies_bearer_token`. A credential Fireflies does not take is
:func:`_auth_failed`'s 500, which comes after the body is read.

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
from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    OperationDefinitionNode,
)

from backlot import auth
from backlot.config import get_settings
from backlot.graphql import body as request_body
from backlot.graphql import engine
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


# The two messages Fireflies refuses a credential with: a value with no `Bearer` prefix, and the
# prefix with a key that does not resolve, which is also the answer to no `Authorization` or an
# empty one.
_NO_PREFIX = (
    "Authorization header is missing the 'Bearer' prefix. Please use the format: "
    "'Authorization: Bearer <api_key>'. If this persists, please contact Fireflies support."
)
_BAD_KEY = (
    "An error occurred while authenticating your request. Please recheck your API key and try "
    "again. If this persists, please contact Fireflies support."
)


def _auth_failed(message: str) -> JSONResponse:
    """Fireflies' answer to a credential it does not take: a 500 carrying an ``auth_failed``
    entry, as api.fireflies.ai answered on 2026-10-18 with no ``Authorization``, with ``nope``,
    ``Basic …``, ``token <key>``, the bare key, ``Bearer nope`` and ``Bearer <key> extra``. The
    document is not read first: ``{"query":"{"}``, ``[]`` and ``5`` drew the same 500."""
    return JSONResponse(
        {
            "errors": [
                {
                    "friendly": True,
                    "code": "auth_failed",
                    "message": message,
                    "extensions": {"code": "auth_failed"},
                }
            ]
        },
        status_code=500,
    )


_FRIENDLY = {
    "invalid_arguments": True,
    "INTERNAL_SERVER_ERROR": False,
    "not_in_team": True,
    "object_not_found": True,
}


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
    # The telemetry middleware names the call by this document (backlot.telemetry.naming).
    request.state.graphql_payload = payload
    token = auth.fireflies_bearer_token(request)
    if token is None:
        return _auth_failed(_NO_PREFIX)
    caller = auth.acl(request).resolve(token)
    if caller is None:
        return _auth_failed(_BAD_KEY)
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
    result = ENGINE.execute_payload(payload, context=context, on_document=_depth_limit)
    request.state.graphql_raised = result.raised
    return Response(
        request_body.node_json(_envelope(result)),
        status_code=result.status,
        media_type="application/json",
    )


def _entry(error: dict, code: str, friendly: bool, extensions: dict) -> dict:
    """``error`` in Fireflies' members, in this order: ``friendly`` first, its own members, then
    ``code`` and ``extensions``."""
    return {
        "friendly": friendly,
        **{key: value for key, value in error.items() if key != "extensions"},
        "code": code,
        "extensions": extensions,
    }


def _envelope(result: engine.Result) -> dict:
    """``result`` as Fireflies writes it, as api.fireflies.ai answered on 2026-10-25 (UTC): each
    request error, and each error in ``result.assigned``, with ``friendly: false``, Apollo Server's
    code beside the message and ``extensions`` empty. The errors a resolver raises with a code in
    ``_FRIENDLY`` keep the extensions it gave them, ``friendly`` as ``_FRIENDLY`` has it, as
    api.fireflies.ai answered ``invalid_arguments`` on 2026-10-21, ``INTERNAL_SERVER_ERROR`` on
    2026-10-22, and ``not_in_team`` and ``object_not_found`` on 2026-10-27."""
    if result.refusals:
        errors = []
        for refusal in result.refusals:
            error: dict = {"message": refusal.message}
            if refusal.locations:
                error["locations"] = list(refusal.locations)
            errors.append(_entry(error, refusal.code, False, {}))
        return {"errors": errors}
    errors = result.payload.get("errors")
    if not errors:
        return result.payload
    shaped = []
    for i, error in enumerate(errors):
        code = error.get("extensions", {}).get("code")
        if i in result.assigned:
            shaped.append(_entry(error, code, False, {}))
        elif code in _FRIENDLY:
            shaped.append(_entry(error, code, _FRIENDLY[code], error["extensions"]))
        else:
            shaped.append(error)
    return {**result.payload, "errors": shaped}


#: graphql-depth-limit's ``maxDepth`` on api.fireflies.ai, which on 2026-10-26 (UTC) refused a
#: selection eleven fields deep and crawled one ten deep.
_MAX_DEPTH = 10


class _Thrown(Exception):
    """graphql-depth-limit throwing out of graphql-js's validation, which Apollo Server answers with
    its 500."""


def _depth_limit(doc: DocumentNode) -> engine.Result | list[GraphQLError]:
    """graphql-depth-limit's rule with a ``maxDepth`` of :data:`_MAX_DEPTH`, which Fireflies runs
    as graphql-js starts validating ``doc``, for :meth:`backlot.graphql.engine.Engine.resolve` to
    take as ``on_document``: the depth errors, or Fireflies' 500 where the rule throws.

    It crawls each operation, the last of each name and the unnamed ones as the last one named
    ``''``: a field's selections one deeper, but none of a field whose name starts ``__``, and a
    spread's fragment, the last of its name, at the spread's depth. A selection deeper than
    :data:`_MAX_DEPTH` is reported and not crawled. A spread of a fragment the document does not
    define, a spread back to a fragment with no field between, and a report past the
    :data:`backlot.graphql.engine.MAX_VALIDATION_ERRORS` graphql-js's ``validate`` takes throw.
    api.fireflies.ai answered so on 2026-10-26 (UTC), and answered ``{ ...F }`` with ``F`` undefined
    inside a field ten deep with the 500 and eleven deep with the depth error.

    A document with an operation named ``__proto__`` throws too, whichever operation is chosen:
    graphql-depth-limit keeps the operations in a plain object, which takes an operation of that
    name as its prototype, and its ``for…in`` crawls the operation's own members. api.fireflies.ai
    answered it with the 500 on 2026-10-28 (UTC), and served an operation named ``constructor`` or
    ``__defineGetter__`` and a spread of a fragment named ``__proto__``."""
    fragments: dict[str, FragmentDefinitionNode] = {}
    operations: dict[str, OperationDefinitionNode] = {}
    for definition in doc.definitions:
        if isinstance(definition, FragmentDefinitionNode):
            fragments[definition.name.value] = definition
        elif isinstance(definition, OperationDefinitionNode):
            operations[definition.name.value if definition.name else ""] = definition
    # The selections a fragment's crawl from a depth reports, which each of its spreads at that
    # depth reports again; None while it is being crawled.
    crawled: dict[tuple[str, int], list | None] = {}

    def crawl(selections, depth: int) -> list:
        reported: list = []
        for node in selections:
            if depth > _MAX_DEPTH:
                reported.append(node)
            elif isinstance(node, FieldNode):
                if not node.name.value.startswith("__") and node.selection_set is not None:
                    reported += crawl(node.selection_set.selections, depth + 1)
            elif isinstance(node, FragmentSpreadNode):
                reported += spread(node.name.value, depth)
            else:
                reported += crawl(node.selection_set.selections, depth)
            if len(reported) > engine.MAX_VALIDATION_ERRORS:
                raise _Thrown
        return reported

    def spread(name: str, depth: int) -> list:
        if name not in fragments or crawled.get((name, depth), []) is None:
            raise _Thrown
        if (name, depth) not in crawled:
            crawled[name, depth] = None
            crawled[name, depth] = crawl(fragments[name].selection_set.selections, depth)
        return crawled[name, depth]

    errors: list[GraphQLError] = []
    try:
        if "__proto__" in operations:
            raise _Thrown
        for name, operation in operations.items():
            for node in crawl(operation.selection_set.selections, 0):
                if len(errors) == engine.MAX_VALIDATION_ERRORS:
                    raise _Thrown
                message = f"'{name}' exceeds maximum operation depth of {_MAX_DEPTH}"
                errors.append(GraphQLError(message, node))
    except _Thrown:
        return engine.refused(
            engine.Refusal("Internal server error", engine.INTERNAL_SERVER_ERROR, status=500)
        )
    return errors


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
