"""Linear's GraphQL API, served at ``/linear/graphql``.

Linear is **GraphQL only** — there is no REST surface to emulate — so this router is one
endpoint. The schema and the resolvers live in ``backlot/graphql/`` (``linear.graphql`` +
``linear_resolvers.py``); everything here is HTTP: a request and credentials in, a GraphQL
envelope out, or the answer of a layer in front of Apollo Server's run, in the order :func:`graphql`
lists them.

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
missing a variable the 401, but for one that selects only ``__schema`` and ``__type``
(:func:`_introspection_only`), which drew the variable's refusal as with the key. An API key under
``Bearer`` is refused ahead of the body on any method but ``OPTIONS``: with ``Bearer <key>``,
``{``, an empty body and a body holding ``NaN`` drew :func:`_api_key_as_bearer`'s 400 on
2026-10-18, and ``charset=latin1``, ``Content-Encoding: bogus`` and a body past the size limit
did on 2026-10-19, when a key in an API key's shape that Linear never issued drew it under
``Bearer`` with ``{``, ``NaN``, ``5``, ``charset=latin1`` and an empty body.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from graphql import FieldNode, FragmentDefinitionNode, FragmentSpreadNode

from backlot import auth
from backlot.config import get_settings
from backlot.graphql import apollo_http, engine, v8_json
from backlot.graphql import body as request_body
from backlot.graphql.linear_resolvers import build_engine

router = APIRouter(prefix="/linear", tags=["linear"])

# The media type of Express's `res.json`, in which `_api_key_as_bearer` and `_express` answer.
_JSON = "application/json; charset=utf-8"

# Linear's code for a body with no query, where Apollo Server's is `BAD_REQUEST`.
_INVALID_INPUT = "INVALID_INPUT"

# Built once at import: the SDL is parsed and validated and the resolver map bound, so a broken
# schema fails at startup instead of on the first request.
ENGINE = build_engine()


def _api_key_as_bearer() -> Response:
    """Linear's 400 for an API key under ``Bearer``, which
    :func:`backlot.auth.linear_api_key_under_bearer` finds, as api.linear.app sent it on
    2026-10-18."""
    return Response(
        request_body.node_json(
            {
                "errors": [
                    {
                        "message": "It looks like you're trying to use an API key as a Bearer "
                        "token. Remove the Bearer prefix from the Authorization header.",
                        "extensions": {
                            "type": "invalid input",
                            "code": "INPUT_ERROR",
                            "statusCode": 400,
                            "userError": True,
                        },
                    }
                ]
            }
        ),
        status_code=400,
        media_type=_JSON,
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
    return Response(request_body.node_json(envelope), status_code=status, media_type=_JSON)


#: The methods Linear's load balancer answers with an empty 501 before Linear sees them, as
#: api.linear.app answered each on 2026-10-25 (UTC). Of the rest of :data:`apollo_http.METHODS`,
#: Cloudflare answers those ``backlot.main._CLOUDFLARE_METHOD`` holds and the layers :func:`graphql`
#: lists the others.
_LOAD_BALANCER_501 = frozenset(
    {"BIND", "LINK", "M-SEARCH", "MKCALENDAR", "REBIND", "SOURCE", "UNBIND", "UNLINK"}
)


@router.api_route("/graphql", methods=list(apollo_http.METHODS), include_in_schema=False)
async def graphql(request: Request):
    """The single Linear endpoint, in front of which Linear runs Express and ``cors``. Not in the
    OpenAPI document on purpose: this is a GraphQL service, and describing one route that accepts an
    arbitrary query would tell an OpenAPI→MCP bridge nothing useful (hence no ``SOURCE_PREFIXES``
    entry either — see ``backlot/openapi.py``). Introspection is the schema description for this
    endpoint.

    The layers answer in this order, each measured against api.linear.app on 2026-10-25 (UTC) with
    the one after it wrong too: the load balancer (:data:`_LOAD_BALANCER_501`), ``cors``
    (:func:`_preflight`), :func:`_api_key_as_bearer`, ``body-parser`` (:func:`_has_body`,
    :func:`~backlot.graphql.body.express_reads_json`), Apollo Server
    (:mod:`backlot.graphql.apollo_http`) up to the choice of operation, then the credential and the
    run (:func:`_answer`)."""
    method = request.method
    if method in _LOAD_BALANCER_501:
        return Response(status_code=501)
    if method == "OPTIONS" and "origin" in apollo_http.headers_seen(request.headers.raw):
        return _preflight(request)
    if method != "OPTIONS" and auth.linear_api_key_under_bearer(request):
        return _api_key_as_bearer()
    payload: Any = None
    content_type = request_body.joined_header(request.headers.getlist("content-type"))
    if _has_body(request) and request_body.express_reads_json(content_type):
        # In a worker thread: `_read` can take seconds over a body near the 10 MiB limit, which
        # would hold up every other request on the event loop.
        payload = await run_in_threadpool(
            _read,
            await request.body(),
            content_type,
            request_body.joined_header(request.headers.getlist("content-encoding")),
        )
        if isinstance(payload, Response):
            return payload
    if method == "POST":
        # The telemetry middleware names the call by this document (backlot.telemetry.naming).
        request.state.graphql_payload = payload
    seen = apollo_http.headers_seen(request.headers.raw)
    seen.pop("content-type", None)
    if content_type:
        seen["content-type"] = content_type
    refusal = apollo_http.csrf_refusal(seen)
    if refusal is None and method not in ("GET", "POST"):
        refusal = apollo_http.method_refusal()
    if refusal is None and method == "GET":
        payload = apollo_http.search_payload(request.scope["query_string"])
        if isinstance(payload, engine.Refusal):
            refusal = payload
        else:
            request.state.graphql_payload = payload
    if refusal is not None:
        return _answer(seen, engine.refused(refusal), None)
    if method == "POST":
        resolved = ENGINE.resolve_payload(_variables_read(payload))
    else:
        resolved = ENGINE.resolve_payload(payload, get=True)
    if isinstance(resolved, engine.Result):
        return _answer(seen, resolved, payload)
    caller = auth.resolve_api_key(request)
    if caller is None:
        if _introspection_only(resolved):
            return _answer(seen, ENGINE.run(resolved), payload, request)
        return _answer(seen, None, payload)
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
    return _answer(seen, ENGINE.run(resolved, context=context), payload, request)


def _has_body(request: Request) -> bool:
    """Whether ``body-parser`` finds a body to read, as ``type-is``'s ``hasBody`` decides behind
    Linear's front: under any method but `GET` and `HEAD`, which that front gives a length of 0
    where it has none. api.linear.app refused a `Content-Type` naming `charset=latin1` with no body
    and no length with its 415 under `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS` and `PROPFIND` on
    2026-10-25 (UTC), and read no body under `GET` and `HEAD`, a length of 0 among them."""
    return request.method not in ("GET", "HEAD")


#: The headers ``cors`` writes on each preflight, as api.linear.app answered every one sent with
#: `Origin` on 2026-10-25 (UTC), and the edge's ``Cache-Control`` and ``Cache-Tag`` beside them.
_PREFLIGHT = (
    ("cache-control", "s-maxage=43200"),
    ("vary", "Origin, Access-Control-Request-Headers"),
    ("cache-tag", "preflight"),
    ("access-control-allow-methods", "GET,HEAD,PUT,PATCH,POST,DELETE,OPTIONS"),
    (
        "access-control-expose-headers",
        "Cache-Control,Content-Language,Content-Length,Content-Type,Expires,Last-Modified,Pragma,"
        "x-request-id,cf-ray,cf-cache-status,Retry-After",
    ),
    ("access-control-max-age", "86400"),
)


def _preflight(request: Request) -> Response:
    """Linear's 204 to an `OPTIONS` carrying `Origin`, ahead of the credential and the body:
    `Origin` echoed back, :data:`_PREFLIGHT`, and each name `Access-Control-Request-Headers` lists,
    in each copy of it, without the spaces and tabs around it. api.linear.app answered so with no
    credential, `nope` and an API key under `Bearer`, with `Access-Control-Request-Method` naming
    `POST`, `PUT` or none, with `Origin: null`, and with `Content-Type, X-Foo`, ` a , b `, `a,,b`,
    `a,  ,b`, a tab after `a` and `a` and `b` in two copies asked for, which came back
    `Content-Type,X-Foo`, `a,b`, `a,,b`, `a,,b`, `a` and `a,b`. An `OPTIONS` without `Origin` is
    Apollo Server's."""
    headers = [("access-control-allow-origin", request.headers["origin"].strip(" \t"))]
    asked = apollo_http.headers_seen(request.headers.raw).get("access-control-request-headers")
    if asked is not None:
        names = (name.strip(" \t") for name in asked.split(","))
        headers.append(("access-control-allow-headers", ",".join(names)))
    response = Response(status_code=204)
    response.raw_headers += [(k.encode(), v.encode("latin-1")) for k, v in (*headers, *_PREFLIGHT)]
    return response


def _as_express_sends(media_type: str) -> str:
    """``media_type`` as Express's ``res.send`` rewrites it through the ``content-type`` package:
    parameter names in lower case, in the order of their names. api.linear.app answered
    ``Accept: application/json; callbackSpec=1.0`` in
    ``application/json; callbackspec=1.0; charset=utf-8`` on 2026-10-25 (UTC)."""
    type_, *parameters = (part.strip() for part in media_type.split(";"))
    pairs = sorted(
        (name.lower(), value) for name, _, value in (p.partition("=") for p in parameters)
    )
    return "; ".join([type_, *(f"{name}={value}" for name, value in pairs)])


# Linear's 401, as api.linear.app answered `{ viewer { id } }` with no `Authorization`, with
# `Bearer nope`, `nope`, `Bearer` and `Basic Zm9vOmJhcg==` on 2026-10-25 (UTC).
_UNAUTHENTICATED = {
    "errors": [
        {
            "message": "Authentication required, not authenticated",
            "extensions": {
                "type": "authentication error",
                "code": "AUTHENTICATION_ERROR",
                "statusCode": 401,
                "userError": True,
                "userPresentableMessage": "You need to authenticate to access this operation.",
                "meta": {},
                "http": {"status": 401},
            },
        }
    ]
}
_INTROSPECTION = frozenset({"__schema", "__type"})


def _introspection_only(operation: engine.Operation) -> bool:
    """Whether each root field ``operation`` selects, through its fragments and whatever their
    directives say, is ``__schema`` or ``__type``. Linear answered such a document with no
    credential on 2026-10-25 (UTC), aliased, in a named or an inline fragment, under ``@skip(if:
    false)`` and as the operation ``operationName`` chose, and answered the 401 to one that also
    selects ``__typename`` or a field it skips."""
    fragments = {
        d.name.value: d
        for d in operation.document.definitions
        if isinstance(d, FragmentDefinitionNode)
    }

    def only(selections) -> bool:
        for selection in selections:
            if isinstance(selection, FieldNode):
                if selection.name.value not in _INTROSPECTION:
                    return False
            elif isinstance(selection, FragmentSpreadNode):
                if not only(fragments[selection.name.value].selection_set.selections):
                    return False
            elif not only(selection.selection_set.selections):
                return False
        return True

    return only(operation.operation.selection_set.selections)


def _answer(
    seen: dict[str, str],
    result: engine.Result | None,
    payload: Any,
    request: Request | None = None,
) -> Response:
    """Apollo Server's answer to ``result`` in Linear's envelope (:func:`_envelope`), or Linear's
    401 where ``result`` is ``None``, in the media type
    :func:`~backlot.graphql.apollo_http.response_type` picks as Express writes it
    (:func:`_as_express_sends`), or Apollo Server's 406. ``request``, passed where ``result`` is a
    run, is given ``result.raised`` as ``graphql_raised``."""
    if request is not None:
        request.state.graphql_raised = result.raised
    media_type = apollo_http.response_type(seen.get("accept"))
    thrown = result is not None and bool(result.refusals) and result.refusals[0].thrown
    if media_type is None and not thrown:
        result = engine.refused(apollo_http.NOT_ACCEPTABLE)
    if result is None:
        body, status, headers = _UNAUTHENTICATED, 401, ()
    else:
        body, status, headers = _envelope(result, payload), result.status, result.headers
    return Response(
        request_body.node_json(body),
        status_code=status,
        headers=dict(headers),
        media_type=_as_express_sends(media_type or apollo_http.JSON),
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


def _apollo(
    message: str, code: str, status: int, locations: Any = (), http_headers: bool = True
) -> dict:
    entry: dict = {"message": message}
    if locations:
        entry["locations"] = list(locations)
    http: dict = {"status": status, "headers": {}} if http_headers else {"status": status}
    extensions: dict = {} if code == engine.BAD_USER_INPUT else {"http": http}
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
    Apollo Server gives no code of its own, as :func:`_internal` (api.linear.app, 2026-10-26
    UTC); each error in ``result.assigned`` as :func:`_internal`, and each other field error in
    Linear's member order (:func:`_in_linears_order`)."""
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
                    _apollo(
                        refusal.message,
                        refusal.code,
                        refusal.status,
                        refusal.locations,
                        refusal.http_headers,
                    )
                )
        return {"errors": errors}
    if "errors" not in result.payload:
        return result.payload
    errors = [
        _internal(error) if i in result.assigned else _in_linears_order(error)
        for i, error in enumerate(result.payload["errors"])
    ]
    return {**result.payload, "errors": errors}


def _in_linears_order(error: dict) -> dict:
    """``error``'s members in the order Linear writes a field error's: api.linear.app put ``path``
    before ``locations`` in each by-id root's not-found and in an ``Argument Validation Error`` on
    2026-10-25 (UTC)."""
    return {
        key: error[key] for key in ("message", "path", "locations", "extensions") if key in error
    }


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
