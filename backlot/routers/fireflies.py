"""Fireflies.ai's GraphQL API, served at ``/fireflies/graphql``.

Fireflies is **GraphQL only** — there is no REST surface to emulate — so this router is one
endpoint, and what answers at any other path (:func:`unrouted`). The schema and the resolvers live
in ``backlot/graphql/`` (``fireflies.graphql`` + ``fireflies_resolvers.py``); everything here is
HTTP: a request and credentials in, a GraphQL envelope out, or the answer of a layer in front of
Apollo Server's run, in the order :func:`graphql` lists them.

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

import email.utils
import math
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    OperationDefinitionNode,
)
from graphql.execution.collect_fields import collect_fields
from graphql.execution.values import get_argument_values, get_variable_values

from backlot import auth
from backlot.config import get_settings
from backlot.graphql import apollo_http, engine, js_words, node_utf8
from backlot.graphql import body as request_body
from backlot.graphql.fireflies_resolvers import build_engine
from backlot.routers import sent_path

router = APIRouter(prefix="/fireflies", tags=["fireflies"])

# Built once at import: the SDL is parsed and validated and the resolver map bound, so a broken
# schema fails at startup instead of on the first request.
ENGINE = build_engine()

# The media type api.fireflies.ai answered each envelope of `_body_refusal` and `_not_found` in.
_JSON = "application/json; charset=utf-8"
_EMPTY = "Body cannot be empty when content-type is set to 'application/json'"
_UNSUPPORTED = "Unsupported Media Type"
_NOT_JSON = "Body is not valid JSON but content-type is set to 'application/json'"
_TOO_LARGE = "Request body is too large"
_SIZE_MISMATCH = "Request body size did not match Content-Length"
#: The most bytes Fireflies reads: api.fireflies.ai served a body of 1048576 bytes and answered one
#: of 1048577 with :data:`_TOO_LARGE`'s 413 on 2026-10-19, with a credential, with none and with
#: ``Authorization: nope``.
_LIMIT = 1024 * 1024


def _body_refusal(message: str, status: int = 400, code: str = "http") -> Response:
    """Fireflies' answer to a body it cannot read: an envelope of its HTTP layer, not a GraphQL
    one, with the message twice. :data:`_EMPTY` is the 400 for a body of no bytes and
    :data:`_NOT_JSON` the 400 for every other one :mod:`backlot.graphql.body` cannot read or finds
    :func:`~backlot.graphql.body.prototype_poisoned`, as api.fireflies.ai answered on 2026-10-18;
    :data:`_TOO_LARGE` is the 413 for one over :data:`_LIMIT` and :data:`_UNSUPPORTED` the 415
    for one of a type it does not read. :func:`_not_found` is the same envelope."""
    envelope = {
        "statusCode": status,
        "status": False,
        "code": code,
        "message": message,
        "errorMessage": message,
    }
    return Response(request_body.node_json(envelope), status_code=status, media_type=_JSON)


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


def _auth_failed(seen: dict[str, str], message: str) -> Response:
    """Fireflies' answer to a credential it does not take: a 500 carrying an ``auth_failed``
    entry, as api.fireflies.ai answered on 2026-10-18 with no ``Authorization``, with ``nope``,
    ``Basic …``, ``token <key>``, the bare key, ``Bearer nope`` and ``Bearer <key> extra``. The
    document is not read first: ``{"query":"{"}``, ``[]`` and ``5`` drew the same 500. Apollo
    Server makes it from the error its context function throws, so it takes a thrown refusal's
    media type (:mod:`backlot.graphql.apollo_http`), and ends in a newline."""
    return Response(
        request_body.node_json(
            {
                "errors": [
                    {
                        "friendly": True,
                        "code": "auth_failed",
                        "message": message,
                        "extensions": {"code": "auth_failed"},
                    }
                ]
            }
        )
        + b"\n",
        status_code=500,
        media_type=apollo_http.response_type(seen.get("accept")) or apollo_http.JSON,
    )


_FRIENDLY = {
    "invalid_arguments": True,
    "INTERNAL_SERVER_ERROR": False,
    "not_in_team": True,
    "object_not_found": True,
}

# The methods Fastify reads a body for but `OPTIONS`, which `@fastify/cors` answers first: at a path
# or under a method no route takes, it reads the body, then answers its not-found handler's 404
# (:func:`_not_found`). Any other method's body goes unread: a `PROPFIND` to `/graphql/` under
# `a/b/c` or with `{` as JSON drew the 404 on 2026-10-26 (UTC).
_BODIED = ("DELETE", "PATCH", "POST", "PUT")


@router.api_route("/graphql", methods=list(apollo_http.METHODS), include_in_schema=False)
async def graphql(request: Request):
    """Fireflies' endpoint, the one path Fastify routes, in front of which Fireflies runs nginx
    (:func:`nginx_passes`), Fastify and ``@fastify/cors``. Not in the OpenAPI document on purpose:
    this is a GraphQL service, and describing one route that accepts an arbitrary query would
    tell an OpenAPI→MCP bridge nothing useful (hence no ``SOURCE_PREFIXES`` entry either — see
    ``backlot/openapi.py``). Introspection is the schema description for this endpoint.

    The layers answer in this order, each measured against api.fireflies.ai on 2026-10-25 (UTC)
    with the one after it wrong too: ``@fastify/cors`` (:func:`_preflight`), Fastify
    (:func:`_fastify_read`, :func:`_not_found`), then Apollo Server
    (:mod:`backlot.graphql.apollo_http`): its page for a browser and its CSRF check ahead of the
    credential (:func:`_auth_failed`), and the rest after it (:func:`_answer`). Each answer carries
    what :func:`_from_fastify` adds."""
    return _from_fastify(await _fastify(request))


async def _fastify(request: Request) -> Response:
    """The answer of Fastify, Apollo Server behind it, and the run."""
    method = request.method
    if method == "OPTIONS":
        return _preflight(request)
    if method not in ("GET", "HEAD", "POST"):
        if method in _BODIED:
            read = await _fastify_read(request, routed=False)
            if isinstance(read, Response):
                return read
        return _not_found(request)
    payload: Any = None
    if method == "POST":
        payload = await _fastify_read(request, routed=True)
        if isinstance(payload, Response):
            return payload
        # The telemetry middleware names the call by this document (backlot.telemetry.naming).
        request.state.graphql_payload = payload
    seen = apollo_http.headers_seen(request.headers.raw)
    if apollo_http.prefers_html(method, seen.get("accept")):
        return Response(apollo_http.landing_page(), headers={"content-type": "text/html"})
    refusal = apollo_http.csrf_refusal(seen)
    if refusal is not None:
        return _answer(seen, engine.refused(refusal))
    token = auth.fireflies_bearer_token(request)
    caller = auth.acl(request).resolve(token)
    if caller is None:
        return _auth_failed(seen, _NO_PREFIX if token is None else _BAD_KEY)
    if method == "HEAD":
        return _answer(seen, engine.refused(apollo_http.method_refusal()))
    if method == "GET":
        refusal = apollo_http.get_content_type_refusal(seen)
        payload = refusal or apollo_http.search_payload(request.scope["query_string"])
        if isinstance(payload, engine.Refusal):
            return _answer(seen, engine.refused(payload))
        request.state.graphql_payload = payload
    state = request.app.state
    context = {
        "conn": node_utf8.Written(auth.conn(request)),
        "visible_ids": auth.visible_ids(request, caller),
        "caller_email": None if caller.is_admin else caller.email,
        "org": getattr(state.acl, "org_name", None),
        "org_domain": get_settings().org_domain,
        # The workspace roster: the addresses that can actually authenticate. `users` is scoped to
        # it, because the principals table holds every person the whole corpus names (16k at the
        # scale measured) and almost none of them have a Fireflies account. See resolve_users.
        "roster": frozenset(state.acl.email_to_token()) if getattr(state, "acl", None) else None,
    }
    resolved = ENGINE.resolve_payload(payload, on_document=_depth_limit, get=method == "GET")
    if isinstance(resolved, engine.Result):
        return _answer(seen, resolved)
    result = ENGINE.run(resolved, context=context)
    request.state.graphql_raised = result.raised
    spent = None
    if not result.refusals:
        spent = Budget.of(request.app).spend(token, _resolver_fields(resolved))
    return _answer(seen, result, spent)


#: The `Strict-Transport-Security` on each answer past Cloudflare, nginx's 404 page among them, as
#: api.fireflies.ai sent it on 2026-10-25 and 2026-10-26 (UTC).
_HSTS = "max-age=31536000; includeSubDomains"


def _from_fastify(response: Response) -> Response:
    """``response`` with the headers on each answer Fastify sends: ``@fastify/cors``'s
    `Access-Control-Allow-Origin: *`, whatever the `Origin`, and :data:`_HSTS`. api.fireflies.ai
    sent the first on each such answer on 2026-10-25 and 2026-10-26 (UTC), its 404, its 415 and
    its 500 for a credential among them, with `Origin: https://example.com` and without one."""
    response.headers.setdefault("access-control-allow-origin", "*")
    response.headers["strict-transport-security"] = _HSTS
    return response


def _answer(seen: dict[str, str], result: engine.Result, spent: Spent | None = None) -> Response:
    """Apollo Server's answer to ``result`` in Fireflies' envelope (:func:`_envelope`), in the
    media type :func:`~backlot.graphql.apollo_http.response_type` picks, or Apollo Server's 406,
    the JSON ending in a newline, and ``spent``'s refusals and headers where a run spent the budget
    (:class:`Budget`). An answer of the request pipeline carries `Cache-Control: no-store` unless it
    sets its own, as a persisted query's hash alone does, where a refusal thrown ahead of it and the
    406 carry none, as api.fireflies.ai answered on 2026-10-25 and 2026-10-26 (UTC): `no-store` on
    each run, a document that does not parse or validate, one with no query, an operation it cannot
    choose, a variable it refuses and a `GET` of a mutation, and none on the CSRF refusal, a body it
    cannot read, a `GET`'s query string it refuses, a `HEAD`, the 406 and the 500
    graphql-depth-limit throws (:func:`_depth_limit`)."""
    media_type = apollo_http.response_type(seen.get("accept"))
    thrown = bool(result.refusals) and result.refusals[0].thrown
    if media_type is None and not thrown:
        result = engine.refused(apollo_http.NOT_ACCEPTABLE)
        thrown = True
    body = _envelope(result)
    headers = dict(result.headers)
    if not thrown and result.status != 500:
        headers.setdefault("cache-control", "no-store")
    if spent is not None:
        body = spent.refuse(body, result.assigned)
        headers.update(spent.headers)
    return Response(
        request_body.node_json(body) + b"\n",
        status_code=result.status,
        headers=headers,
        media_type=media_type or apollo_http.JSON,
    )


def _resolver_fields(operation: engine.Operation) -> list[tuple[str, FieldNode]]:
    """The root fields ``operation`` selects whose resolvers run, in order, through fragments and as
    `@skip` and `@include` leave them with the variables as the run reads them: each response key,
    an alias or a field's name, once, and not `__typename`, `__schema` or `__type`, nor a field
    whose arguments graphql-js refuses as it starts the field, which api.fireflies.ai answered with
    that refusal and not the budget's, its minute spent, on 2026-10-26 (UTC)."""
    root = ENGINE.schema.get_root_type(operation.operation.operation)
    fragments = {
        d.name.value: d
        for d in operation.document.definitions
        if isinstance(d, FragmentDefinitionNode)
    }
    sent = operation.variables if isinstance(operation.variables, dict) else {}
    # The run this follows coerced the same variables, so they coerce here too.
    variables = get_variable_values(
        ENGINE.schema, list(operation.operation.variable_definitions), sent
    )
    try:
        fields = collect_fields(
            ENGINE.schema, fragments, variables, root, operation.operation.selection_set
        )
    except GraphQLError:
        # A root directive refusing its argument: the operation ran no field, its data null.
        return []
    taken = []
    for key, nodes in fields.items():
        if nodes[0].name.value.startswith("__"):
            continue
        try:
            get_argument_values(root.fields[nodes[0].name.value], nodes[0], variables)
        except GraphQLError:
            continue
        taken.append((key, nodes[0]))
    return taken


# --- the budget --------------------------------------------------------------------------------
#
# Fireflies counts each root field a document runs a resolver for, an alias of one as one more:
# api.fireflies.ai took 1 from `x-ratelimit-remaining-api` for `{ user { user_id } }`, 2 for the
# same field under two aliases, and 1 for a `transcript` it did not find, and none for
# `{ __typename }`, a document it refused or a credential it did not take, on 2026-10-26 (UTC).

_TOO_MANY = "https://docs.fireflies.ai/miscellaneous/error-codes#too_many_requests"


@dataclass
class Spent:
    """What one run took from a key's :class:`Budget`: the root fields refused, which the answer
    writes as ``too_many_requests`` with a null in its data (:meth:`refuse`), the headers on the
    answer, and the moment a refused field may be asked for again, in epoch seconds."""

    refused: list[tuple[str, FieldNode]]
    headers: dict[str, str]
    retry_at: float

    def refuse(self, body: dict, assigned: frozenset[int] = frozenset()) -> dict:
        """``body`` with each refused field's data null and its error, as api.fireflies.ai answered
        the refused fields of :class:`Budget`'s run: `"friendly": true`, the moment in the message
        to the second and in `metadata.retryAfter` to the millisecond. The refused fields are the
        last the document runs, as a budget once spent stays spent for the rest of it, and an error
        the run made at one of them is left out, the refusal standing for the field's whole answer.

        graphql-js's refusals of a root field's arguments, those of ``assigned`` at a root field,
        come first, as it makes them before Fireflies counts a field; then the refusals; then every
        other error, which a resolver makes once Fireflies has counted its field. api.fireflies.ai
        answered so a `null` refused at a non-null argument before and after a refused field, a
        refusal ahead of a `not_in_team` 30 fields before it, and refusals ahead of
        `transcripts(limit: 0)` and `transcripts(date: 1e20)` at the last field the minute had room
        for, on 2026-10-26 (UTC). Which comes first is a race: with `transcripts(limit: 0)` or
        `transcripts(date: 1e20)` 30 fields before the refusals, it answered the error first in
        five of six, on 2026-10-26 and 2026-10-27, and between two refusals in the sixth; this does
        not follow those six."""
        if not self.refused:
            return body
        when = email.utils.formatdate(self.retry_at, usegmt=True)
        keys = {key for key, _ in self.refused}
        started: list[dict] = []
        refusals: list[dict] = []
        errors: list[dict] = []
        for i, error in enumerate(body.get("errors", [])):
            path = error.get("path") or [None]
            if path[0] in keys:
                continue
            (started if len(path) == 1 and i in assigned else errors).append(error)
        for key, node in self.refused:
            refusals.append(
                {
                    "friendly": True,
                    "message": f"Too many requests. Please retry after {when} (UTC)",
                    "locations": list(js_words.locations(GraphQLError("", node))),
                    "path": [key],
                    "code": "too_many_requests",
                    "extensions": {
                        "code": "too_many_requests",
                        "status": 429,
                        "metadata": {"retryAfter": int(self.retry_at * 1000)},
                        "helpUrls": [_TOO_MANY],
                    },
                }
            )
        data = dict(body["data"])
        for key in keys:
            data[key] = None
        return {"errors": started + refusals + errors, "data": data}


class Budget:
    """Each key's minute and day: the minute opens at the first field it counts and closes 60
    seconds on, the day is the calendar day in UTC, each holds the fields
    :attr:`backlot.config.Settings.fireflies_per_minute` and ``fireflies_per_day`` give, and a field
    past either is refused while ``fireflies_enforce_rate_limits`` is on. Measured on 2026-10-26
    (UTC): thirty `{ user { user_id } }` in 11 seconds were served with
    `x-ratelimit-remaining-api_burst` falling from 29 to 0 and `x-ratelimit-reset-api_burst` from
    60 to 50, the next three refused, `{ __typename }` beside them served, and a document of 33
    aliases sent 65 seconds on served 30 and refused 3.

    A key the minute refuses is held from its first refused field to 60 seconds on, past its
    minute's own close: each field it sends meanwhile is refused, counted in the minute it falls in
    and not in the day, with the seconds left in `retry-after-api_burst`, and, while the day has
    room, in `retry-after` and the message. api.fireflies.ai refused one 51 seconds into a hold
    opened 10 seconds into a minute, with 9, then served one 11 seconds later as the second of a new
    minute, the day down only by that one; and refused fields 32 and 34 seconds into a hold opened
    30 seconds into a minute with 28 and 26, and one 5 seconds in with 56, a second over what the
    hold gives, on 2026-10-26 (UTC).

    A field past the day is refused until midnight UTC and counted in the minute all the same. An
    answer refusing a field carries the `-api_burst` window where a field passed the minute,
    `retry-after-api_burst` where the minute refused one, the `-api` window where one was served,
    and `retry-after-api` where the day refused one. Once the day is spent, `retry-after`, the bare
    `x-ratelimit-*` and the moment in the message are the day's; before, they are the minute's
    where it refuses every field, and the day's numbers beside the minute's wait where it serves
    one. Measured on 2026-10-29 (UTC) on a key with 15 fields left of its day: 17 fields at once,
    then 1, 15 past the minute's 30, 3 and then 1 in the hold that opened, and 1 after it."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._minutes: dict[str, list[float]] = {}
        self._days: dict[str, list[int]] = {}
        self._held: dict[str | None, float] = {}

    @staticmethod
    def of(app) -> "Budget":
        """The app's budget, made on first use."""
        budget = getattr(app.state, "fireflies_budget", None)
        if budget is None:
            budget = app.state.fireflies_budget = Budget()
        return budget

    def spend(self, key: str | None, fields: list[tuple[str, FieldNode]]) -> Spent | None:
        """One count from ``key``'s minute and day for each of ``fields``, or a refusal of it once
        either is spent; None where there is no field to count."""
        if not fields:
            return None
        now = self.clock()
        minute = self._minutes.get(key)
        if minute is None or now >= minute[0] + 60:
            minute = self._minutes[key] = [now, 0]
        today = self._days.get(key)
        if today is None or today[0] != int(now // 86400):
            today = self._days[key] = [int(now // 86400), 0]
        settings = get_settings()
        per_minute, per_day = settings.fireflies_per_minute, settings.fireflies_per_day
        enforce = settings.fireflies_enforce_rate_limits
        held = enforce and now < self._held.get(key, now)
        refused: list[tuple[str, FieldNode]] = []
        by_minute = by_day = passed = served = False
        for field in fields:
            minute[1] += 1
            if held or (enforce and minute[1] > per_minute):
                refused.append(field)
                by_minute = True
                continue
            passed = True
            if enforce and today[1] >= per_day:
                refused.append(field)
                by_day = True
            else:
                today[1] += 1
                served = True
        midnight = (today[0] + 1) * 86400
        burst = _window("api_burst", per_minute, minute[1], math.ceil(minute[0] + 60 - now))
        day = _window("api", per_day, today[1], math.ceil(midnight - now))
        plain = {name.removesuffix("-api"): value for name, value in day.items()}
        if not refused:
            return Spent([], burst | day | plain, now)
        if by_minute and not held:
            self._held[key] = now + 60
        headers = burst if passed else {}
        if by_minute:
            headers["retry-after-api_burst"] = str(math.ceil(self._held[key] - now))
        if served:
            headers |= day
        if by_day:
            headers["retry-after-api"] = day["x-ratelimit-reset-api"]
        if today[1] >= per_day:
            wait = day["x-ratelimit-reset-api"]
            bare = plain
            retry_at = now + int(wait)
        else:
            wait = headers["retry-after-api_burst"]
            bare = plain
            if not served:
                bare = {
                    "x-ratelimit-limit": str(per_minute),
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": wait,
                }
            retry_at = self._held[key]
        if served:
            headers |= bare | {"retry-after": wait}
        else:
            headers |= {"retry-after": wait} | bare
        return Spent(refused, headers, retry_at)


def _window(name: str, limit: int, used: int, reset: int) -> dict[str, str]:
    return {
        f"x-ratelimit-limit-{name}": str(limit),
        f"x-ratelimit-remaining-{name}": str(max(limit - used, 0)),
        f"x-ratelimit-reset-{name}": str(reset),
    }


#: The headers ``@fastify/cors`` writes on each preflight, as api.fireflies.ai answered every one
#: sent with `Origin` and `Access-Control-Request-Method` on 2026-10-25 (UTC).
_PREFLIGHT = (
    ("access-control-allow-origin", "*"),
    ("access-control-allow-methods", "GET,HEAD,POST"),
    ("vary", "Access-Control-Request-Headers"),
)


def _preflight(request: Request) -> Response:
    """``@fastify/cors``'s answer to an `OPTIONS`, ahead of the body and the credential: its 204,
    with :data:`_PREFLIGHT` and `Access-Control-Request-Headers` echoed back as Node reads it, to
    one carrying both `Origin` and `Access-Control-Request-Method` with more than spaces and tabs in
    them, and its 400 to any other. api.fireflies.ai answered so with no credential and
    `Bearer nope`, with `Access-Control-Request-Method` naming `POST` or `PUT`, with `Origin: null`,
    and with a JSON body or `{`; it echoed ` a , b ` as `a , b`, `a` and `b` in two copies as
    `a, b`, and a value with a space, a tab, U+0085 or U+00A0 inside it, U+00A0 at either end or
    U+0085 after it, as sent. It answered an empty or blank `Origin` and an empty
    `Access-Control-Request-Method` with the 400 on 2026-10-26 (UTC)."""
    seen = apollo_http.headers_seen(request.headers.raw)
    if "origin" not in seen or "access-control-request-method" not in seen:
        return Response(
            b"Invalid Preflight Request", status_code=400, headers={"content-type": "text/plain"}
        )
    response = Response(status_code=204)
    asked = seen.get("access-control-request-headers")
    echoed = () if asked is None else (("access-control-allow-headers", asked),)
    response.raw_headers += [(k.encode(), v.encode("latin-1")) for k, v in (*_PREFLIGHT, *echoed)]
    return response


def _not_found(request: Request) -> Response:
    """Fireflies' 404 for a method or a path no route takes, the method, the path as a client sent
    it (:func:`~backlot.routers.sent_path`) and the query string as sent quoted in it.
    api.fireflies.ai answered so each of the 28 methods of :data:`apollo_http.METHODS` but GET,
    HEAD, POST, OPTIONS and Cloudflare's three at `/graphql` on 2026-10-25 (UTC), `PUT`, `DELETE`
    and `PATCH` once Fastify had read their body (:func:`_fastify_read`), and quoted
    `/graphql?x=%7B%20y&z&a=b+c` as sent; and `POST`, `GET`, `HEAD`, `PUT`, `PROPFIND` and `BIND`
    at `/GRAPHQL`, `/graphql/x`, `//graphql` and `/graphql%2F`, `POST` at `/%2Fgraphql` and
    `/graphql%0A`, and `PUT` at `/graph%71l`, quoting each path as sent."""
    query = request.scope["query_string"].decode("latin-1")
    message = f"Cannot {request.method} {sent_path(request.scope)}{'?' if query else ''}{query}"
    return _body_refusal(message, 404, "not_found")


_NGINX_PAGE = (
    b"<html>\r\n<head><title>%s</title></head>\r\n<body>\r\n<center><h1>%s</h1></center>\r\n"
    b"<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n"
)
_NGINX_404 = _NGINX_PAGE % ((b"404 Not Found",) * 2)
_NGINX_400 = _NGINX_PAGE % ((b"400 Bad Request",) * 2)
_NGINX_LOCATION = re.compile(rb"/graphql(/|$)", re.IGNORECASE)


def nginx_path(path: str) -> bytes | None:
    """``path``, as a client sent it (:func:`~backlot.routers.sent_path`), as nginx in front of
    Fastify reads it to pick a location: each escape decoded, each run of slashes one, then each
    `.` segment dropped and each `..` segment taking the segment before it away; or ``None`` where a
    `..` has no segment before it to take, which nginx refuses with :data:`_NGINX_400`. On
    2026-10-29 (UTC) `/x/../graphql`, `/x//../graphql`, `/a/b/../../graphql`, `/x/..%2Fgraphql` and
    `/graphql/x/..` drew Fastify's 404 from api.fireflies.ai, `/graphql/..`, `/graphql//..` and
    `/x/.//graphql` :data:`_NGINX_404`, and `/..`, `/x/../..`, `/x/..%2F..` and `/%2e%2e`
    :data:`_NGINX_400`."""
    segments = re.sub(rb"/{2,}", b"/", urllib.parse.unquote_to_bytes(path)).split(b"/")[1:]
    out: list[bytes] = []
    for segment in segments:
        if segment == b"..":
            if not out:
                return None
            out.pop()
        elif segment != b".":
            out.append(segment)
    return b"/" + b"/".join(out)


def nginx_passes(path: bytes) -> bool:
    """Whether nginx passes on ``path``, as :func:`nginx_path` reads it, to Fastify: it starts with
    `/graphql`, in any case, followed by a slash, by nothing or by a newline that ends it.
    api.fireflies.ai answered with Fastify's 404 at each path :func:`_not_found` names, and with
    :data:`_NGINX_404` at `/graphqlx`, `/graphql.json`, `/graphql;x`, `/graphql%20`, `/graphql%7E`,
    `/x/graphql` and `/`, on 2026-10-25 (UTC)."""
    return _NGINX_LOCATION.match(path) is not None


@router.api_route("/", methods=list(apollo_http.METHODS), include_in_schema=False)
async def unrouted(request: Request):
    """Fireflies' answer at a path Fastify has no route for, each of which ``backlot.main`` routes
    here: nginx's page where nginx keeps the request (:func:`nginx_path`, :func:`nginx_passes`), its
    400 without :data:`_HSTS` and its 404 with it alone, and Fastify's where it passes it on:
    :func:`_preflight` to an `OPTIONS`, and to any other method, after reading the body of one of
    :data:`_BODIED` (:func:`_fastify_read`), its not-found handler's 404 (:func:`_not_found`), each
    with what :func:`_from_fastify` adds."""
    path = nginx_path(sent_path(request.scope))
    if path is None:
        return Response(_NGINX_400, status_code=400, headers={"content-type": "text/html"})
    if not nginx_passes(path):
        return Response(
            _NGINX_404,
            status_code=404,
            headers={"content-type": "text/html", "strict-transport-security": _HSTS},
        )
    if request.method == "OPTIONS":
        return _from_fastify(_preflight(request))
    if request.method in _BODIED:
        read = await _fastify_read(request, routed=False)
        if isinstance(read, Response):
            return _from_fastify(read)
    return _from_fastify(_not_found(request))


_FASTIFY_PARSED = ("application/json", "text/plain", "application/x-www-form-urlencoded")


async def _fastify_read(request: Request, routed: bool) -> Any:
    """The body Fastify 5.10.0 hands a request's handler, or :func:`_body_refusal`'s refusal of it,
    read off ``lib/handle-request.js`` and ``lib/content-type-parser.js``. With no
    ``Content-Type``, a request with no ``Content-Length`` or one of 0, and no
    ``Transfer-Encoding``, has none, and any other is the 415; a type it cannot parse
    (:func:`~backlot.graphql.body.fastify_media_type`) is the 415. It reads JSON as :func:`_read`
    does, plain text and a form (:func:`~backlot.graphql.body.form_fields`). On a request its route
    does not take, ``routed`` false, any other type and no type are left unread, where a routed
    request gets the 415.

    api.fireflies.ai answered so each body sent on 2026-10-25 (UTC) to `POST`, `PUT`, `DELETE`
    and `PATCH`: no type with a body was the 415 by `POST` and the 404 by `PUT`, `a/b/c` and an
    empty type the 415 by both, `application/graphql` the 415 by `POST`, past the limit too, and
    the 404 by `PUT`, and a form or plain text past the limit the 413 by both. A `POST` to
    `/graphql/`, which no route takes, was read as a `PUT` to `/graphql` is: `{` and an empty body
    drew the 400s, `a/b/c` the 415, and plain text, `application/graphql` and no type the 404."""
    content_type = request.headers.get("content-type")
    if content_type is None:
        length = request.headers.get("content-length")
        if "transfer-encoding" not in request.headers and length in (None, "0"):
            return None
        return _body_refusal(_UNSUPPORTED, 415) if routed else None
    media_type = request_body.fastify_media_type(content_type)
    if media_type is None:
        return _body_refusal(_UNSUPPORTED, 415)
    if media_type not in _FASTIFY_PARSED:
        return _body_refusal(_UNSUPPORTED, 415) if routed else None
    raw = await request.body()
    if media_type == "application/json":
        # Off the event loop, as a body of `_LIMIT` bytes can keep `_read` busy a third of a second.
        return await run_in_threadpool(_read, raw)
    if len(raw) > _LIMIT:
        return _body_refusal(_TOO_LARGE, 413)
    text = raw.decode("utf-8", "replace")
    if media_type != "text/plain":
        return request_body.form_fields(text)
    # Fastify reads plain text as UTF-8 and counts the text's own UTF-8 bytes against the body's:
    # `text/plain` holding the byte 0xE9, with a charset or none, by POST and by PUT, was the 400 on
    # 2026-10-26 (UTC), where a form holding it was served and 0xE9 past the limit was the 413.
    return text if len(text.encode("utf-8")) == len(raw) else _body_refusal(_SIZE_MISMATCH)


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
    """The payload Fireflies reads from the JSON body ``raw``, or :func:`_body_refusal`'s refusal
    of it."""
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
