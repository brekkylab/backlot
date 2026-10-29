"""Linear's GraphQL API, served at ``/linear/graphql``.

Linear is **GraphQL only** — there is no REST surface to emulate — so this router is one
endpoint, and what answers at any other path (:func:`express`). The schema and the resolvers live in
``backlot/graphql/`` (``linear.graphql`` + ``linear_resolvers.py``); everything here is HTTP: a
request and credentials in, a GraphQL envelope out, or the answer of a layer in front of Apollo
Server's run, in the order :func:`graphql` lists them.

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

import math
import re
import secrets
import string
import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from graphql import FieldNode, FragmentDefinitionNode, FragmentSpreadNode
from starlette.datastructures import MutableHeaders

from backlot import auth, express_http
from backlot.config import get_settings
from backlot.graphql import apollo_http, engine, node_utf8, v8_json
from backlot.graphql import body as request_body
from backlot.graphql.linear_resolvers import build_engine
from backlot.routers import sent_path

router = APIRouter(prefix="/linear", tags=["linear"])

# The media type of Express's `res.json`, in which `_api_key_as_bearer` and `_express` answer.
_JSON = "application/json; charset=utf-8"

# Linear's code for a body with no query, where Apollo Server's is `BAD_REQUEST`.
_INVALID_INPUT = "INVALID_INPUT"

# Built once at import: the SDL is parsed and validated and the resolver map bound, so a broken
# schema fails at startup instead of on the first request.
ENGINE = build_engine()


# --- the headers on an answer ----------------------------------------------------------------
#
# Read off the answers api.linear.app gave on 2026-10-25 to 2026-10-28 (UTC) to the requests the
# tests' tables hold and to each kind of answer with and without `Origin`. Backlot sends none of the
# headers Cloudflare, Google and the edge add (`via`, `cf-ray`, `cf-cache-status`, `alt-svc` and
# `age`), and its server's own `Server` where Cloudflare's says `cloudflare`.

#: The `Cache-Control` Express sends on an answer that sets none of its own.
_NO_STORE = "private, no-cache, no-store, must-revalidate"
#: The headers ``cors`` names to a page whose `Origin` it allows, on a preflight and on an answer.
_EXPOSED = (
    "Cache-Control,Content-Language,Content-Length,Content-Type,Expires,Last-Modified,Pragma,"
    "x-request-id,cf-ray,cf-cache-status,Retry-After"
)


def request_id() -> str:
    """An `x-request-id` as Linear writes one: 16 lower-case hex digits, a hyphen, and the code of
    the Cloudflare location that answered, `ICN` on each of the 940 api.linear.app sent on
    2026-10-25 and 2026-10-26 (UTC)."""
    return f"{secrets.token_hex(8)}-ICN"


def _origin(request: Request) -> str | None:
    """The `Origin` ``cors`` echoes: each copy without the spaces and tabs around it, the empty ones
    ahead of the first that is not empty left off, the rest joined with `, ` and the join's trailing
    spaces left off; or None where nothing is left, as api.linear.app echoed each `Origin` row of
    `_LINEAR_HTTP` in `tests/test_linear.py` on 2026-10-26 (UTC)."""
    copies = [value.strip(" \t") for value in request.headers.getlist("origin")]
    while copies and not copies[0]:
        copies.pop(0)
    return ", ".join(copies).rstrip(" ") or None


def _express_sent(
    request: Request,
    response: Response,
    *,
    etag: str | bool = True,
    allow_origin: str | None = None,
) -> Response:
    """``response`` with the headers Linear sends on an answer Express writes: :data:`_NO_STORE`
    where it has no `Cache-Control` of its own, Express's `ETag`
    (:func:`backlot.express_http.weak_etag`, as on 591 of the 618 answers that carried one with a
    body, the other 27 carrying :data:`_API_KEY_AS_BEARER_ETAG`) where ``etag`` is True, or ``etag``
    itself where it is one, `Vary: Accept-Encoding`, an `x-request-id` (:func:`request_id`), and
    where the request carries an `Origin` (:func:`_origin`), ``cors``'s answer to it:
    ``allow_origin`` where given, else that origin, with :data:`_EXPOSED`, and `Origin` in `Vary`. A
    `GET` or `HEAD` whose 2xx answer :func:`_fresh` finds fresh is Express's 304 instead, with no
    body, `Content-Type` or `Accept-Encoding` in `Vary`, and no other answer is, as api.linear.app
    answered on 2026-10-27 (UTC). A `HEAD`'s other 2xx answer carries no `ETag`, as api.linear.app
    answered `HEAD /` with and without the greeting's `ETag` in `If-None-Match` on 2026-10-26
    (UTC)."""
    headers: MutableHeaders = response.headers
    headers.setdefault("cache-control", _NO_STORE)
    if etag is True:
        headers["etag"] = express_http.weak_etag(response.body)
    elif etag:
        headers["etag"] = etag
    origin = _origin(request)
    if origin is None:
        headers["vary"] = "Accept-Encoding"
    else:
        headers["access-control-allow-origin"] = allow_origin or origin
        headers["access-control-expose-headers"] = _EXPOSED
        headers["vary"] = "Origin, Accept-Encoding"
    headers["x-request-id"] = request_id()
    if (
        request.method in ("GET", "HEAD")
        and 200 <= response.status_code < 300
        and _fresh(request, headers.get("etag"))
    ):
        response.status_code = 304
        response.body = b""
        del headers["content-type"]
        del headers["content-length"]
        vary = [name for name in headers["vary"].split(", ") if name != "Accept-Encoding"]
        if vary:
            headers["vary"] = ", ".join(vary)
        else:
            del headers["vary"]
    elif request.method == "HEAD" and 200 <= response.status_code < 300:
        del headers["etag"]
    return response


def _fresh(request: Request, etag: str | None) -> bool:
    """Whether ``fresh`` 2.0.0, which api.linear.app's answers match (Linear does not say what it
    runs), finds the answer carrying ``etag`` fresh to ``request``: an `If-None-Match` of `*`, or
    one naming ``etag``, weakly or not, and no `no-cache` (:data:`backlot.express_http.NO_CACHE`) in
    the request's `Cache-Control`, its copies joined with `, `. The `If-None-Match` is read without
    the spaces and tabs around it, and a list split at its commas with each tag stripped of spaces
    alone, as ``fresh``'s ``parseTokenList`` reads it; an `If-Modified-Since` is not read: 2.0.0
    reads one only where no `If-None-Match` is sent, and compares it with a `Last-Modified` no
    Linear answer carries. The rows of `_LINEAR_HTTP` in `tests/test_linear.py` that send either
    hold api.linear.app's answers of 2026-10-26 to 2026-10-28 (UTC)."""
    none_match = request.headers.get("if-none-match", "").strip(" \t")
    if not none_match:
        return False
    if express_http.NO_CACHE.search(", ".join(request.headers.getlist("cache-control"))):
        return False
    if none_match == "*":
        return True
    return any(
        match in (etag, f"W/{etag}") or f"W/{match}" == etag
        for match in (part.strip(" ") for part in none_match.split(","))
    )


_API_KEY_AS_BEARER = (
    "It looks like you're trying to use an API key as a Bearer token. Remove the Bearer prefix "
    "from the Authorization header."
)


#: The `ETag` on :func:`_api_key_as_bearer`'s 400 in either envelope, which is Express's for the
#: other one's 249 bytes, not for the 240 of the GraphQL error: api.linear.app sent it on each at
#: every path measured on 2026-10-25 and 2026-10-26 (UTC).
_API_KEY_AS_BEARER_ETAG = 'W/"f9-BDIWTD84v19kyk2Xj/zxwnhRq74"'


def _api_key_as_bearer(request: Request) -> Response:
    """Linear's 400 for an API key under ``Bearer``, which
    :func:`backlot.auth.linear_api_key_under_bearer` finds, at the path a client sent
    (:func:`backlot.routers.sent_path`). Where that path starts `/graphql` it is a GraphQL error,
    as api.linear.app sent it on 2026-10-18; at any other it is the members of :func:`_express`'s
    envelope, as api.linear.app sent it at `/GRAPHQL`, `//graphql`, `/graph%71l`, `/nosuch` and `/`
    on 2026-10-25 (UTC), the first three of which it serves as `/graphql`, where `/graphqlx`,
    `/graphql;x` and `/graphql%2F` drew the GraphQL error. Its `Access-Control-Allow-Origin` is
    `https://linear.app` where :func:`_origin` finds an `Origin`, whatever it is: api.linear.app
    answered `Origin: https://example.com` with `https://linear.app` on 2026-10-26."""
    path = sent_path(request.scope)
    extensions = {
        "type": "invalid input",
        "code": "INPUT_ERROR",
        "statusCode": 400,
        "userError": True,
    }
    if path.startswith("/graphql"):
        body: dict = {"errors": [{"message": _API_KEY_AS_BEARER, "extensions": extensions}]}
    else:
        body = {"error": _API_KEY_AS_BEARER, "details": {}, "extensions": extensions, "code": 400}
    return _express_sent(
        request,
        Response(request_body.node_json(body), status_code=400, media_type=_JSON),
        etag=_API_KEY_AS_BEARER_ETAG,
        allow_origin="https://linear.app",
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


def _front(request: Request) -> Response | None:
    """The answer of a layer ahead of Express's routing, whatever the path: the load balancer's
    (:data:`_LOAD_BALANCER_501`), ``cors``'s (:func:`_preflight`) and :func:`_api_key_as_bearer`,
    in that order; or None."""
    method = request.method
    if method in _LOAD_BALANCER_501:
        return Response(status_code=501)
    if method == "OPTIONS" and "origin" in apollo_http.headers_seen(request.headers.raw):
        return _preflight(request)
    if method != "OPTIONS" and auth.linear_api_key_under_bearer(request):
        return _api_key_as_bearer(request)
    return None


@router.api_route("/graphql", methods=list(apollo_http.METHODS), include_in_schema=False)
async def graphql(request: Request):
    """Linear's endpoint, in front of which Linear runs Express and ``cors``, at `/graphql` and at
    each other path Express hands Apollo Server (:func:`routes_to_apollo`), which
    ``backlot.main`` routes here. Not in the OpenAPI document on purpose: this is a GraphQL
    service, and describing one route that accepts an arbitrary query would tell an OpenAPI→MCP
    bridge nothing useful (hence no ``SOURCE_PREFIXES`` entry either — see ``backlot/openapi.py``).
    Introspection is the schema description for this endpoint.

    The layers answer in this order, each measured against api.linear.app on 2026-10-25 (UTC) with
    the one after it wrong too: those of :func:`_front`, ``body-parser`` (:func:`_has_body`,
    :func:`~backlot.graphql.body.express_reads_json`), Apollo Server
    (:mod:`backlot.graphql.apollo_http`) up to the choice of operation, then the credential and the
    run (:func:`_answer`). Each answer of Express's carries what :func:`_express_sent` adds, and any
    answer at a path the load balancer reads as `/` carries `nosniff` (:func:`_at_the_root`)."""
    response = _front(request)
    if response is None:
        response = _express_sent(request, await _apollo_server(request))
    return _at_the_root(request, response)


async def _apollo_server(request: Request) -> Response:
    """The answer of ``body-parser`` and Apollo Server behind it, and the run."""
    method = request.method
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
    key = auth.api_key_token(request)
    state = request.app.state
    context = {
        "conn": node_utf8.Written(auth.conn(request)),
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
    return _answer(seen, ENGINE.run(resolved, context=context), payload, request, key)


def _has_body(request: Request) -> bool:
    """Whether ``body-parser`` finds a body to read, as ``type-is``'s ``hasBody`` decides behind
    Linear's front: under any method but `GET` and `HEAD`, which that front gives a length of 0
    where it has none. api.linear.app refused a `Content-Type` naming `charset=latin1` with no body
    and no length with its 415 under `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS` and `PROPFIND` on
    2026-10-25 (UTC), and read no body under `GET` and `HEAD`, a length of 0 among them."""
    return request.method not in ("GET", "HEAD")


_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_BARE = frozenset('"<>^`{|}')


def express_path(path: str) -> str:
    """``path``, as a client sent it (:func:`backlot.routers.sent_path`), as Express reads it behind
    Linear's front: a backslash is a slash, an escape of a character RFC 3986 leaves unreserved is
    that character, the dot segments are resolved (:func:`_dots_resolved`), a run of slashes is
    one, each character of :data:`_BARE` sent bare is escaped in upper case, and any other escape is
    kept as sent. Express's 404 quoted `/%6Eosuch` as `/nosuch`, `//nosuch` and `/nosuch//x` as
    `/nosuch` and `/nosuch/x`, `/a\\b` as `/a/b`, `/a%2fb` and `/a%3cb` as sent, each character of
    :data:`_BARE` escaped in upper case (`|` as `%7C`), and each other printable ASCII character but
    `%`, `?` and `#` as sent, on 2026-10-25 (UTC)."""
    path = re.sub("/{2,}", "/", _dots_resolved(_unreserved(path)))
    return "".join(f"%{ord(char):02X}" if char in _BARE else char for char in path)


def _unreserved(path: str) -> str:
    """``path`` with each backslash a slash and each escape of a character RFC 3986 leaves
    unreserved that character."""

    def one(match: re.Match[str]) -> str:
        char = chr(int(match[1], 16))
        return char if char in _UNRESERVED else match[0]

    return _ESCAPE.sub(one, path.replace("\\", "/"))


def routes_to_apollo(path: str) -> bool:
    """Whether Express hands ``path`` (:func:`express_path`) to Apollo Server: `/graphql` in any
    case, alone or with a slash after it and anything after that. api.linear.app served `/GRAPHQL`,
    `/GraphQL/`, `/graphql/`, `/graphql/x` and `/Graphql/X/Y` and answered `/graphqlx`,
    `/graphql.json`, `/graphql;x`, `/graphql%20` and `/graphql%2F` with Express's 404 on 2026-10-25
    (UTC)."""
    return re.fullmatch(r"/graphql(?:/.*)?", path, re.ASCII | re.IGNORECASE) is not None


def _dots_resolved(path: str) -> str:
    """``path`` with each `.` segment dropped and each `..` segment taking the segment before it
    away, an empty one included, as a URL's path is resolved: a `..` with none before it takes
    nothing, and a path that ends in either keeps the slash before it. Linear's front resolves them
    so ahead of folding a run of slashes: on 2026-10-29 (UTC) api.linear.app served `/..\\graphql`
    and `/graphql//..`, answered `/x//../graphql` and `/x/.//graphql` with Express's 404 quoting
    `/x/graphql`, and `/graphql/x/../..` and `/r/b/%2e%2e` quoting `/` and `/r/`, and answered a
    `GET` to `/r/..` with its greeting. Cloudflare refuses a `..` that climbs past the root with a
    segment after it first (``backlot.request_target.refused_path``)."""
    segments = path.split("/")[1:]
    out: list[str] = []
    for segment in segments:
        if segment == "..":
            out = out[:-1]
        elif segment != ".":
            out.append(segment)
    if segments[-1] in (".", ".."):
        out.append("")
    return "/" + "/".join(out)


_GREETING = b"Hello from the Linear GraphQL API."
_HTML = "text/html; charset=utf-8"


@router.api_route("/", methods=list(apollo_http.METHODS), include_in_schema=False)
async def express(request: Request):
    """Linear's answer at a path Express does not hand Apollo Server (:func:`routes_to_apollo`),
    each of which ``backlot.main`` routes here: after the layers of :func:`_front`, Express's own,
    as api.linear.app answered on 2026-10-25 (UTC), and `PATCH /` on 2026-10-26. At `/` that is
    :data:`_GREETING` to a `GET` and a `HEAD`, and the methods `/` takes, as `text/plain`, to an
    `OPTIONS`; at any other path, and to any other method, it is ``finalhandler``'s 404 page
    (:func:`_not_found`). The body is not read: `{` under `application/json` drew the 404.

    Each answer of Express's carries what :func:`_express_sent` adds, and the three carry
    `X-Content-Type-Options: nosniff`, as api.linear.app sent them on 2026-10-25 and 2026-10-26:
    the greeting with an `ETag` to a `GET` and none to a `HEAD`, the methods with none, `Allow` and
    the edge's `Cache-Control` and `Cache-Tag` for an `OPTIONS`, and the 404 page with none and with
    `Content-Security-Policy: default-src 'none'`. The 404 page goes without its `Content-Length`,
    which uvicorn sends in chunks, and with neither header to a `HEAD`, as api.linear.app sent it
    to each of `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS` and `PROPFIND` on
    2026-10-29 (UTC). Any answer at a path the load balancer reads as `/` carries `nosniff`
    (:func:`_at_the_root`)."""
    return _at_the_root(request, _front(request) or _express_route(request))


def reads_as_root(scope) -> bool:
    """Whether Linear's load balancer reads the path sent (:func:`backlot.routers.sent_path`) as
    `/`: as :func:`express_path` reads a backslash and an escape (:func:`_unreserved`), but with a
    run of slashes folded ahead of resolving the dot segments, where Express resolves them first."""
    return _dots_resolved(re.sub("/{2,}", "/", _unreserved(sent_path(scope)))) == "/"


def _at_the_root(request: Request, response: Response) -> Response:
    """``response`` with `X-Content-Type-Options: nosniff` where Linear's load balancer reads the
    path as `/` (:func:`reads_as_root`), whatever answered: api.linear.app put it on the load
    balancer's 501 at `//`, `///`, `/\\`, `/%2E` and `/x/..`, and not at `/%2F`, on 2026-10-26
    (UTC). On 2026-10-29 it put it on Apollo Server's 401 at `/graphql//..`, `/graphql///..` and
    `/graphql/x//../..` and not at `/graphql//x/..` or `/graphql/./`, paths Express hands Apollo
    Server alike; on the 200, 400 and 405, the preflight and the 501 at `/graphql//..`; and on
    Express's 404 at `/x//..`."""
    if reads_as_root(request.scope):
        response.headers["x-content-type-options"] = "nosniff"
    return response


def slashes_alone(scope) -> bool:
    """Whether the path sent (:func:`backlot.routers.sent_path`) is slashes alone, where
    Cloudflare's `CONNECT` 400 carries `X-Content-Type-Options: nosniff`: api.linear.app sent it at
    `/`, `//` and `///`, and not at `/%2E`, `/x/..`, `/x/%2E%2E`, `/./`, `/..`, `/%2E%2E`, `/\\` or
    `/nosuch`, on 2026-10-28 (UTC), where its load balancer reads each of them but `/nosuch` as `/`
    (:func:`reads_as_root`)."""
    return re.fullmatch("/+", sent_path(scope)) is not None


def _express_route(request: Request) -> Response:
    path = express_path(sent_path(request.scope))
    if path == "/" and request.method in ("GET", "HEAD"):
        response = Response(_GREETING, media_type=_HTML)
        _express_sent(request, response)
    elif path == "/" and request.method == "OPTIONS":
        response = _express_sent(
            request,
            Response(
                b"GET, HEAD",
                headers={
                    "content-type": "text/plain",
                    "allow": "GET, HEAD",
                    "cache-control": f"{_NO_STORE}, s-maxage=43200",
                    "cache-tag": "preflight",
                },
            ),
            etag=False,
        )
    else:
        response = Response(_not_found(request.method, path), status_code=404, media_type=_HTML)
        response.headers["content-security-policy"] = "default-src 'none'"
        _express_sent(request, response, etag=False)
        del response.headers["content-length"]
    response.headers["x-content-type-options"] = "nosniff"
    return response


def _not_found(method: str, path: str) -> bytes:
    """``finalhandler``'s 404 page, `Cannot` ``method`` ``path`` with `&` and `'` escaped for HTML:
    the other characters ``escape-html`` replaces are escaped in ``path`` by then
    (:func:`express_path`)."""
    message = f"Cannot {method} {path}".replace("&", "&amp;").replace("'", "&#39;")
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<title>Error</title>\n'
        f"</head>\n<body>\n<pre>{message}</pre>\n</body>\n</html>\n"
    ).encode()


#: The headers ``cors`` writes on each preflight, as api.linear.app answered every one sent with
#: `Origin` on 2026-10-25 (UTC), and the edge's ``Cache-Control`` and ``Cache-Tag`` beside them.
_PREFLIGHT = (
    ("cache-control", "s-maxage=43200"),
    ("vary", "Origin, Access-Control-Request-Headers"),
    ("cache-tag", "preflight"),
    ("access-control-allow-methods", "GET,HEAD,PUT,PATCH,POST,DELETE,OPTIONS"),
    ("access-control-expose-headers", _EXPOSED),
    ("access-control-max-age", "86400"),
)


def _preflight(request: Request) -> Response:
    """Linear's 204 to an `OPTIONS` carrying `Origin`, ahead of the credential and the body:
    `Origin` echoed back, :data:`_PREFLIGHT`, an `x-request-id` (:func:`request_id`), which each of
    the 55 preflights api.linear.app answered on 2026-10-25 and 2026-10-26 (UTC) carried, and each
    name `Access-Control-Request-Headers` lists, in each copy of it, without the spaces, tabs and
    U+00A0s around it: api.linear.app sent each byte from 0x80 to 0xFF before or after a name back
    as sent but 0xA0 on 2026-10-28 (UTC). It answered so with no credential, `nope` and an API key
    under `Bearer`, with `Access-Control-Request-Method` naming `POST`, `PUT` or none, with
    `Origin: null`, and with `Content-Type, X-Foo`, ` a , b `, `a,,b`, `a,  ,b`, a tab after `a` and
    `a` and `b` in two copies asked for, which came back `Content-Type,X-Foo`, `a,b`, `a,,b`,
    `a,,b`, `a` and `a,b`. An `OPTIONS` without `Origin` is Apollo Server's."""
    headers = [("access-control-allow-origin", request.headers["origin"].strip(" \t"))]
    asked = apollo_http.headers_seen(request.headers.raw).get("access-control-request-headers")
    if asked is not None:
        names = (name.strip(" \t\xa0") for name in asked.split(","))
        headers.append(("access-control-allow-headers", ",".join(names)))
    headers += [*_PREFLIGHT, ("x-request-id", request_id())]
    response = Response(status_code=204)
    response.raw_headers += [(k.encode(), v.encode("latin-1")) for k, v in headers]
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
    key: str | None = None,
) -> Response:
    """Apollo Server's answer to ``result`` in Linear's envelope (:func:`_envelope`), or Linear's
    401 where ``result`` is ``None``, in the media type
    :func:`~backlot.graphql.apollo_http.response_type` picks as Express writes it
    (:func:`_as_express_sends`), or Apollo Server's 406, the JSON ending in a newline as each of
    them did from api.linear.app on 2026-10-25 and 2026-10-26 (UTC). ``request``, passed where
    ``result`` is a run, is given ``result.raised`` as ``graphql_raised``, and the answer takes one
    from the :class:`RequestBudget` of ``key``, the API key that resolved, or of the address with
    none, and reports it, but for the 406, which carried no `x-ratelimit-requests-*` header on any
    of the 29 api.linear.app answered on 2026-10-25 and 2026-10-26 (UTC)."""
    if request is not None:
        request.state.graphql_raised = result.raised
    media_type = apollo_http.response_type(seen.get("accept"))
    thrown = result is not None and bool(result.refusals) and result.refusals[0].thrown
    not_acceptable = media_type is None and not thrown
    if not_acceptable:
        result = engine.refused(apollo_http.NOT_ACCEPTABLE)
    if result is None:
        body, status, headers = _UNAUTHENTICATED, 401, ()
    else:
        body, status, headers = _envelope(result, payload), result.status, result.headers
    response = Response(
        request_body.node_json(body) + b"\n",
        status_code=status,
        headers=dict(headers),
        media_type=_as_express_sends(media_type or apollo_http.JSON),
    )
    if request is not None and not not_acceptable:
        response.headers.update(RequestBudget.of(request.app).take(request, key))
    return response


#: The requests an hour Linear allows an API key and a caller with no credential, as
#: `x-ratelimit-requests-limit` read on 2026-10-25 and 2026-10-26 (UTC). Linear's rate-limiting
#: page says 2,500 and 600.
_REQUESTS_AN_HOUR = {True: 2500, False: 1200}


class RequestBudget:
    """Linear's count of each caller's requests: a bucket of `limit` that refills at `limit` an
    hour, from which each answer to a document Linear ran, or whose variables it refused, takes
    one, `remaining` being its level rounded down. Measured against api.linear.app on 2026-10-26
    (UTC): thirty `{ __typename }` sent over 13 seconds took `x-ratelimit-requests-remaining` from
    2499 to 2478, and the bucket gives each of the 42 answers with a key that carried the header in
    that capture within 2 of real's, 30 of them exactly, where rounding to the nearest gives 12;
    twelve documents refused as they validated, sent between two that ran, took none; and
    `x-ratelimit-requests-reset` was an hour past the answer, in epoch milliseconds, 3599.9 to
    3600.9 seconds after its `Date`, on each. A caller with no credential is counted by its address,
    which the measurement did not vary. Past the limit api.linear.app answers a 429 that takes
    nothing: once the bucket of a caller with no credential was spent, ten more requests drew it,
    and one sent 13.8 seconds after the last served was served with `x-ratelimit-requests-remaining`
    3, on 2026-10-27 (UTC). This serves past the limit, so the level stops at 0."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}

    @staticmethod
    def of(app) -> "RequestBudget":
        """The app's budget, made on first use."""
        budget = getattr(app.state, "linear_requests", None)
        if budget is None:
            budget = app.state.linear_requests = RequestBudget()
        return budget

    def take(self, request: Request, key: str | None) -> dict[str, str]:
        """The three `x-ratelimit-requests-*` headers once one request is taken from the bucket of
        the API key ``key``, or where it is None, of the address ``request`` came from."""
        limit = _REQUESTS_AN_HOUR[key is not None]
        address = request.client.host if request.client else ""
        caller = key if key is not None else f"address:{address}"
        now = self.clock()
        level, then = self._buckets.get(caller, (float(limit), now))
        level = min(float(limit), level + (now - then) * limit / 3600) - 1
        self._buckets[caller] = (max(level, 0.0), now)
        return {
            "x-ratelimit-requests-limit": str(limit),
            "x-ratelimit-requests-remaining": str(max(math.floor(level), 0)),
            "x-ratelimit-requests-reset": str(int((now + 3600) * 1000)),
        }


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
