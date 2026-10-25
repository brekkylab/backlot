"""FastAPI app hosting every emulated vendor API under path prefixes.

Startup opens the read-only corpus DB, attaches the writable mutation overlay to it, loads the
ACL/token map, and starts a background cache warm-up. The corpus file is never opened writable —
see `backlot/overlay.py`.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.datastructures import URL, Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, DispatchFunction
from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Receive, Scope, Send

from backlot import auth, errors, openapi, overlay, store, synth, telemetry
from backlot.acl import Acl
from backlot.config import get_settings
from backlot.errors import msgraph as msgraph_errors
from backlot.oauth import Oauth
from backlot.routers import (
    atlassian,
    fireflies,
    github,
    google,
    hubspot,
    linear,
    msteams,
    notion,
    oauth,
    s3,
    slack,
)
from backlot.telemetry import naming, recorder
from backlot.telemetry.middleware import TelemetryMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if not settings.db_path.exists():
        raise RuntimeError(
            f"DB not found at {settings.db_path}. Build it first: "
            "backlot import <corpus.jsonl>  (see `backlot import --help` for the other sources)"
        )
    # Adopt the corpus-derived org a BYO import recorded, so the routers (which read these off
    # settings) agree with the ACL. A roster-built tokens.yaml has no org; the defaults then stand.
    if settings.tokens_path.exists():
        data = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8")) or {}
        if data.get("org"):
            settings.org_name = data["org"]
        if data.get("org_domain"):
            settings.org_domain = data["org_domain"]
    conn = store.connect_ro(
        settings.db_path,
        mmap_mb=settings.sqlite_mmap_mb,
        cache_mb=settings.sqlite_cache_mb,
        temp_memory=True,
        busy_ms=settings.sqlite_busy_ms,
    )
    # A DB older than a table or column this build reads would answer every request that touches
    # it with a bare OperationalError, one per read, with nothing saying why. Named once here.
    if stale := store.missing_schema(conn):
        raise RuntimeError(
            f"{settings.db_path} was built by an older Backlot and has no "
            f"{', '.join(stale)}. Re-import the corpus: backlot import <corpus.jsonl>"
        )
    app.state.conn = conn
    # The mutation overlay. Attached to the same connection as the corpus, which stays mode=ro:
    # served writes land in `ov` and the corpus file is never opened writable. A per-app name so
    # two servers in one process (every pytest run) do not share one overlay.
    app.state.overlay_name = overlay.name_for(app)
    overlay.attach(conn, app.state.overlay_name)
    app.state.acl = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
    app.state.oauth = Oauth.load(settings.credentials_path)  # None if credentials.yaml absent

    _src = store.read_meta(conn, "source_documents")
    app.state.source_documents = int(_src) if _src is not None else None

    # Caches the warm-up below fills, each too slow to compute per request on a large corpus:
    # per-source COUNT(*), channel -> principals granted on any of its docs, and channel -> member
    # count. Every consumer treats None as "not warm yet" and falls back to its own query.
    #
    # Teams gets its OWN channel -> principals map rather than sharing Slack's: a channel name is
    # unique within its source and nothing stops `#incidents` existing in both, so one map would
    # answer for a channel the caller asked nothing about.
    app.state.doc_counts = None
    app.state.channel_acl = None
    app.state.channel_members = None
    app.state.msteams_channel_acl = None

    app.state.warm_error = None

    def _warm_caches():
        """Fill the caches above, RECORDING a failure rather than dying quietly.

        The fallbacks mean a dead thread breaks nothing — which is exactly why it must be reported,
        or a broken warm-up looks like a slow one forever and /health stays `ok` with null counts.
        """
        try:
            c = store.connect_ro(
                settings.db_path,
                mmap_mb=settings.sqlite_mmap_mb,
                cache_mb=settings.sqlite_cache_mb,
                temp_memory=True,
            )
            try:

                def _channel_acl(src: str) -> dict:
                    cacl: dict[str, set] = {}
                    for ch, pid in c.execute(
                        f"SELECT DISTINCT a.{store.grouping_col(src)}, a.principal_id "
                        f"FROM {store.acl_table(src)} a"
                    ):
                        cacl.setdefault(ch, set()).add(pid)
                    return {k: frozenset(v) for k, v in cacl.items()}

                app.state.channel_acl = _channel_acl("slack")
                app.state.msteams_channel_acl = _channel_acl("msteams")
                app.state.doc_counts = {
                    src: c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                    for src, tbl in store.SOURCE_TABLE.items()
                }
                app.state.channel_members = store.slack_channel_member_counts(c)
            finally:
                c.close()
        except Exception as e:  # noqa: BLE001 — a warm-up must not be able to kill the server
            app.state.warm_error = f"{type(e).__name__}: {e}"
            logging.getLogger(__name__).exception("cache warm-up failed; /health will say degraded")

    # On app.state, not fire-and-forget, so tests can join it instead of polling /health.
    app.state.warm_thread = threading.Thread(target=_warm_caches, daemon=True)
    app.state.warm_thread.start()
    exporter = None
    if telemetry.enabled(settings):
        exporter = recorder.Exporter(
            recorder.RECORDER, recorder.ENDPOINT, recorder.INTERVAL, surface=naming.surface
        )
        exporter.start()
        print(telemetry.notice(recorder.ENDPOINT), file=sys.stderr)
    try:
        yield
    finally:
        if exporter is not None:
            exporter.stop()
        conn.close()


app = FastAPI(
    # The server's name, never a corpus's: it reaches /openapi.json and every generated client.
    title="Backlot",
    lifespan=lifespan,
    # FastAPI's default derives the method suffix from a set, so it changes between restarts.
    generate_unique_id_function=openapi.unique_operation_id,
)

# The served spec, with what FastAPI cannot be told to declare: real's `default: 30` / `default: 1`
# on GitHub's `per_page` / `page`, whose runtime default has to stay None, and real's description
# of each, which is the one place the `per_page` cap is stated (see
# :func:`backlot.openapi.github_page_parameters`). Both come from the router that applies the
# numbers (`github.PAGE_PARAMETERS`, built from its `PER_PAGE_DEFAULT` and `PER_PAGE_MAX`), so the
# document cannot declare one size while the route applies another. FastAPI caches its document on
# the app and hands the same dict back, so the edit is made in place and is the same edit each time.
_fastapi_openapi = app.openapi


def _openapi_with_vendor_parameters() -> dict:
    spec = openapi.github_page_parameters(_fastapi_openapi(), github.PAGE_PARAMETERS)
    return openapi.google_system_parameters(openapi.jira_search_placement(spec))


app.openapi = _openapi_with_vendor_parameters


# Per-vendor error envelopes live in ``backlot/errors/``. Both handlers ask that package and fall
# back to FastAPI's ``{"detail": ...}``.


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    # A wrong method is refused by the router before any vendor code runs, so the vendor's own 405
    # is asked for here rather than raised where the other refusals are. The exception is replaced
    # rather than edited: what follows reads the body, media type and headers off it either way.
    if exc.status_code == 405:
        # The path as a refusal echoes it, which is the routed one on any vendor but Atlassian.
        vendor = errors.method_not_allowed(atlassian._echoed_path(request), request.method)
        if vendor is not None:
            if vendor.headers is None:
                vendor.headers = getattr(exc, "headers", None)
            exc = vendor
    headers = getattr(exc, "headers", None)
    body = errors.http_body(request.url.path, exc, request.query_params)
    if body is None:
        body = {"detail": exc.detail}
    # A vendor may decide how the body reaches the wire as well as what is in it — Google's errors
    # are indented to the byte and a `callback` on a GET answers one at 200 as a script, and an
    # Atlassian refusal takes the shape `Accept` asks for (``errors.atlassian.rendered``).
    # Asking the envelope keeps that where the rest of that vendor's error shape lives, and leaves
    # most GitHub errors with FastAPI's `JSONResponse`. GitHub honours `callback` through an
    # envelope of its own — `/**/cb({"meta": …, "data": …})` under
    # `application/javascript; charset=utf-8`, with the status inside `meta` and an unparseable
    # name refused UNWRAPPED — which is a shape to build, not one to share.
    rendered = errors.rendered(request, exc.status_code, body, headers)
    if rendered is not None:
        return rendered
    # A vendor may answer one refusal under a media type of its own — Jira's type-conversion 400 is
    # RFC 7807 on `application/problem+json`, where its other 400s are plain JSON. The exception
    # carries it, because the path and status this is reached by are the same for both and
    # `errors.json_media_type` sees only those two. `None` leaves `JSONResponse`'s own type alone.
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        headers=headers,
        media_type=getattr(exc, "media_type", None),
    )


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    answer = errors.validation_body(request.url.path, exc.errors())
    if answer is None:
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
    status_code, body = answer
    return JSONResponse(status_code=status_code, content=body)


class _Layer:
    """One of the middlewares below, run on the requests ``runs_on`` picks; any other request goes
    past it to the app below.

    A middleware function runs as Starlette's ``BaseHTTPMiddleware``, which hands the request on in
    a task of its own and streams the answer back up through a channel, whether or not the function
    then does anything: measured over ``backlot.serve()`` on 2026-10-23 with 27 registered, they
    add 2.2 to 4.4 ms to an answer that goes through all of them. So ``runs_on(scope)`` is asked
    first, ahead of the ``BaseHTTPMiddleware``, and a request it does not pick is one the function
    would have handed straight on: it holds the function's own test for whether it has anything to
    do, or a part of that test that reads neither the answer nor the body. It is asked where the
    function stands in the order, so it sees the path and the method the middlewares outside it
    leave. Where the function reads ``request.url.path``, ``runs_on`` reads :func:`_url_path`.
    """

    def __init__(
        self, app: ASGIApp, dispatch: DispatchFunction, runs_on: Callable[[Scope], bool]
    ) -> None:
        self.app = app
        self.runs_on = runs_on
        self.layer = BaseHTTPMiddleware(app, dispatch=dispatch)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.runs_on(scope):
            await self.layer(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def _middleware(runs_on: Callable[[Scope], bool]):
    """Register the decorated function as a middleware run on the requests ``runs_on`` picks
    (:class:`_Layer`). Each one registered is outside those registered before it, so the last is
    the outermost."""

    def register(dispatch: DispatchFunction) -> DispatchFunction:
        app.add_middleware(_Layer, dispatch=dispatch, runs_on=runs_on)
        return dispatch

    return register


#: What ``request.url.path`` does not keep as ``scope["path"]`` holds it: Starlette builds that URL
#: by parsing the path again, where a `?` or a `#` ends it and a tab, CR or LF is dropped.
_REPARSED = re.compile(r"[?#\t\r\n]")


def _url_path(scope: Scope) -> str:
    """``request.url.path`` with no ``Request`` built: ``scope["path"]`` itself unless it holds one
    of :data:`_REPARSED` or the `Host` is in brackets, where the URL is built as ``request.url``
    builds it, and raises where that raises (`Host: [0:0]`, which Python's ``urlsplit`` refuses)."""
    path = scope["path"]
    if _REPARSED.search(path) or _bracketed_host(scope):
        return URL(scope=scope).path
    return path


def _bracketed_host(scope: Scope) -> bool:
    for name, value in scope["headers"]:
        if name == b"host":
            return value.startswith(b"[")
    return False


def _at_or_under(path: str, prefixes: tuple[str, ...]) -> bool:
    """Whether ``path`` is one of ``prefixes`` or a path under one."""
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def _some_github_route_matches(scope: Scope) -> bool:
    """Whether some mounted route matches this scope's path, whatever the method."""
    return any(route.matches(scope)[0] is not Match.NONE for route in app.router.routes)


def _github_routes(scope: Scope) -> bool:
    """Whether real routes this scope: some mounted route matches its path, whatever the method
    (:func:`_some_github_route_matches`), or its method and path are an operation GitHub publishes
    and no route here serves (``backlot.routers.github.is_an_unserved_operation``).

    A 404 for a path real does not route carries no version echo, and carries the five
    `x-ratelimit-*` headers only for a request that sent no `Authorization` header at all — the same
    line `refuse_a_trailing_slash_on_github` draws for the trailing-slash spelling of such a path,
    which real draws for every spelling. Measured against api.github.com 2026-09-21:
    `/repos/psf/requests/<unmatched>`, its trailing-slash spelling and a top-level `/<unmatched>`
    all answered 404 with no echo, where `/repos/psf/requests/issues/<missing>` — a route that DOES
    match, on a resource that does not exist — carried both for either caller. With no
    `Authorization` header the three carried the five (`limit` 60) and moved `used` by one apiece.
    Every header value carried none of them and moved no window: a valid token (whose `used` read 0
    before three of them and 0 after), a bad bearer, `Basic Zm9vOmJhcg==` and a scheme-less value —
    so what real reads here is that a credential arrived, not that one resolved, where the same
    `Basic` value, which names no token, on a route that matches is served and counted.
    """
    return _some_github_route_matches(scope) or github.is_an_unserved_operation(
        scope["method"], scope["path"]
    )


def _a_github_route_serves(scope: Scope) -> bool:
    """Whether a GitHub route serves this scope's method and path. Asked of
    ``backlot.routers.github.router``, which holds the GitHub routes and nothing else."""
    return any(route.matches(scope)[0] is Match.FULL for route in github.router.routes)


def _would_redirect_to_the_slashed_path(scope: Scope) -> bool:
    """Whether the router's `redirect_slashes` would answer this path with a 307 to its
    slash-terminated spelling."""
    if _some_github_route_matches(scope):
        return False
    slashed = {**scope, "path": f"{scope['path']}/"}
    return _some_github_route_matches(slashed)


@_middleware(
    lambda scope: (
        _url_path(scope).startswith("/github/")
        and not _url_path(scope).endswith("/")
        and _would_redirect_to_the_slashed_path(scope)
    )
)
async def refuse_a_missing_slash_on_github(request: Request, call_next):
    """A `/github` path no route matches, whose slash-terminated spelling one does, is a 404, not
    the 307 to that spelling Starlette's router answers.

    Real routes the path as sent, with no redirect: a `GET` of `.../statuses`, `.../git/ref` or
    `.../git/trees` was the 404 a path with no route gets, and `GET .../commits` the commit listing,
    which the baseline records as `repos/list-commits` `missing` (measured against api.github.com
    on 2026-10-19 and 2026-10-25). Registered first, which makes it the innermost of the GitHub
    middlewares, so its 404 leaves through them as the router's would: `GET .../commits` and
    `POST .../git/trees` are operations real refuses a credential on first ("Bad credentials" for
    `Bearer nope`, 2026-10-25), which ``backlot.routers.github.refuse_an_unserved_operation``
    answers, and of the paths this answers, only those two carry the version echo and the five
    `x-ratelimit-*` for a token (``_github_routes``).
    """
    return await _http_exception_handler(request, StarletteHTTPException(status_code=404))


@_middleware(
    lambda scope: (
        scope["path"].startswith("/github/")
        and not _a_github_route_serves(scope)
        and github.is_an_unserved_operation(scope["method"], scope["path"])
    )
)
async def refuse_an_unserved_github_operation(request: Request, call_next):
    """An operation GitHub publishes that no route here serves under the request's method is
    refused as a served route refuses before it runs, ahead of the router's 404 or 405
    (``backlot.routers.github.refuse_an_unserved_operation``).

    Registered second, after `refuse_a_missing_slash_on_github` and before the other GitHub
    middlewares: it reads the path ``resolve_github_id_paths`` rewrote and a `HEAD` as the GET it is
    rewritten to, and its refusal leaves through the version echo and the rate-limit headers as a
    served route's 401 does.
    """
    try:
        await github.refuse_an_unserved_operation(request)
    except StarletteHTTPException as exc:
        return await _http_exception_handler(request, exc)
    return await call_next(request)


@_middleware(
    lambda scope: (
        scope["path"].startswith("/hubspot/")
        and hubspot.is_an_unserved_operation(scope["method"], scope["path"])
    )
)
async def refuse_an_unserved_hubspot_operation(request: Request, call_next):
    """An operation HubSpot publishes that no route here serves is refused for its credential as a
    served route refuses one, ahead of the router's 404 or 405
    (``backlot.routers.hubspot.refuse_an_unserved_operation``)."""
    refusal = hubspot.refuse_an_unserved_operation(request)
    if refusal is not None:
        return refusal
    return await call_next(request)


def _hubspot_route(scope: Scope) -> BaseRoute | None:
    """The route the router matches this scope to: the first HubSpot route that matches it in
    full, else the first that matches its path alone (the router's 405). Asked of
    ``backlot.routers.hubspot.router``, which holds the HubSpot routes and nothing else."""
    partial = None
    for route in hubspot.router.routes:
        match = route.matches(scope)[0]
        if match is Match.FULL:
            return route
        if match is Match.PARTIAL and partial is None:
            partial = route
    return partial


@_middleware(
    lambda scope: (
        scope["path"].startswith("/hubspot/")
        and hubspot.answers_ahead_of_the_routes(
            scope["method"], scope["path"], Headers(scope=scope).get("accept")
        )
    )
)
async def answer_a_hubspot_method_or_accept_no_route_takes(request: Request, call_next):
    """`OPTIONS`, `GET` on `search` and `batch/read`, `PUT` on a record and an `Accept` that admits
    no JSON are answered on `/hubspot` as real answers them, ahead of the credential and the router
    (``backlot.routers.hubspot.answer_ahead_of_the_routes``). It leaves on the scope the route the
    router matches the request to (``_hubspot_route``), where telemetry names the call by it
    (``backlot.telemetry.naming.Namer.name``)."""
    if request.scope["path"].startswith("/hubspot/"):
        answer = hubspot.answer_ahead_of_the_routes(request)
        if answer is not None:
            request.scope["route"] = _hubspot_route(request.scope)
            return answer
    return await call_next(request)


@_middleware(lambda scope: _url_path(scope).startswith("/github"))
async def echo_github_api_version(request: Request, call_next):
    """Report which API version served the response, on the `/github` answers real echoes one for,
    200 and error alike.

    Middleware rather than a router dependency: a dependency that sets headers on its injected
    ``Response`` loses them whenever the route returns a ``Response`` itself, which the raw-content
    and diff media types do (see ``backlot.routers.github``) — so exactly the responses whose SHAPE
    the header describes would ship without it.

    A rejected version gets no added echo: ``selected_api_version`` returns `None`. The pull and
    contents backend's own empty echo is preserved (see ``github._validate_api_version``).
    Code search gets no echo either, whatever it pinned: real's code search backend does not read
    the header, and neither does `/rate_limit` asked with no `Authorization` header (see
    ``github.honours_api_version``). Nor does a 404 for a path real does not route, whatever the
    caller's credential — see ``_github_routes`` — nor real's "Bad credentials" 401
    (``github.refused_a_credential``), nor the 401s ``github.refused_an_unserved_operation`` names.
    ``DELETE /installation/token``'s 401 is the one of those that echoes, as a served route's does
    (:func:`github.refused_an_unserved_operation_reported`).
    """
    response = await call_next(request)
    if (
        github.honours_api_version(request)
        and _github_routes(request.scope)
        and not github.refused_a_credential(request)
        and (
            not github.refused_an_unserved_operation(request)
            or github.refused_an_unserved_operation_reported(request)
        )
    ):
        version = github.selected_api_version(request)
        if version is not None:
            response.headers[github.SELECTED_VERSION_HEADER] = version
    return response


@_middleware(lambda scope: errors.s3.owns(_url_path(scope)))
async def answer_s3_with_request_ids(request: Request, call_next):
    """Put `x-amz-request-id` and `x-amz-id-2` on every `/s3` answer, and hand the router the same
    pair for the body of an error.

    Middleware for the reason the GitHub rate-limit headers are: the pair rides answers no route
    handler builds, the exception handlers' refusals among them. The pair is set before the route
    runs so `backlot.routers.s3._error` writes the same one into `<RequestId>` and `<HostId>`, which
    real repeats there. Each header is set only where the answer has none, because the parse 400
    carries an id of its own shape (`backlot.errors.s3.method_not_allowed`).
    """
    ids = s3.request_ids(request.method, request.url.path, request.url.query)
    s3.REQUEST_IDS.set(ids)
    response = await call_next(request)
    response.headers.setdefault("x-amz-request-id", ids[0])
    response.headers.setdefault("x-amz-id-2", ids[1])
    return response


@_middleware(lambda scope: _url_path(scope).startswith("/github"))
async def report_github_rate_limit(request: Request, call_next):
    """Refuse a `/github` request whose window is already spent, real's 403 (see
    ``backlot.routers.github.rate_limit_refusal``), and otherwise put the five `x-ratelimit-*`
    headers on the answer real carries them on, 200 and error alike, counting each against the
    caller's window for the resource (``backlot.routers.github.rate_limit_headers``).

    Middleware for the reason the version echo is: the headers ride on answers no route handler
    builds, the exception handlers' 401s and 404s and the raw and diff media types' own responses
    among them, and a client paces by them on every one. Registered inside the `HEAD` middleware,
    so a `HEAD` runs through here as the GET it is rewritten to and counts once, as it does on real
    (`remaining` 46 → 45 across one `HEAD`, measured 2026-09-09), and the head copies the five
    with the rest of the GET's headers. A path outside `/github` gets nothing: the other vendors'
    rate-limit answers are not measured. A 404 for a path real does not route gets them only for a
    request that carried no `Authorization` header at all — see ``_github_routes`` — and a
    credential that did not resolve gets them on no path at all but those
    ``github.refused_without_a_window`` leaves out; the refusal check shares that same gate, so
    neither is ever refused by it. Of the 401s an operation no route here serves gets for
    its credential, the ones ``github.refused_an_unserved_operation_uncounted`` names get none
    either and count nothing.
    """
    gated = not github.refused_without_a_window(request) and (
        _github_routes(request.scope) or "authorization" not in request.headers
    )
    if gated:
        refusal = github.rate_limit_refusal(request)
        if refusal is not None:
            return refusal
    response = await call_next(request)
    if gated and not github.refused_an_unserved_operation_uncounted(request):
        for name, value in github.rate_limit_headers(
            request,
            response.status_code,
            count=not github.refused_an_unserved_operation_reported(request),
        ).items():
            response.headers[name] = value
    return response


def _would_redirect_to_the_slash_free_path(scope: Scope) -> bool:
    """Whether the router's `redirect_slashes` would answer this path with a 307 to its slash-free
    spelling.

    That is the whole of what real has no equivalent for, so it is the whole of what gets
    intercepted. Starlette redirects only when the path AS SENT matches no route and the slash-free
    spelling matches one, so a route whose last segment is a `{path:path}` — `/contents/{path:path}`
    matches the empty string — answers its own trailing slash and never reaches the redirect, the
    same as real answers it. What that route then makes of the slash is its own: `contents/{path}/`
    with a path in it is a 302 of real's own (:func:`backlot.routers.github.get_contents`), which is
    a route's answer rather than a routing rule, and the one redirect this server sends under
    `/github`.
    """
    if _some_github_route_matches(scope):
        return False
    slash_free = {**scope, "path": scope["path"].rstrip("/")}
    return _some_github_route_matches(slash_free)


def _a_slashed_github_path_the_router_redirects(scope: Scope) -> bool:
    path = _url_path(scope)
    return (
        path.startswith("/github/")
        and path.endswith("/")
        and _would_redirect_to_the_slash_free_path(scope)
    )


@_middleware(_a_slashed_github_path_the_router_redirects)
async def refuse_a_trailing_slash_on_github(request: Request, call_next):
    """A trailing slash on `/github` that matches no route is a 404, not the 307 to the slash-free
    path that Starlette's router answers by default.

    Real runs no routing-level slash redirect: a trailing slash is just part of the path, and what
    answers it is whichever route matches the path as sent. A route ending in a path parameter
    absorbs the slash as an empty segment — `GET /repos/{owner}/{repo}/contents/` is the root
    listing's own 200, like `/contents` beside it — and every other route simply does not match, so
    the request gets the same 404 a path with no route at all gets, ahead of a bad bearer's own 401.
    Measured against api.github.com on 2026-09-15, 2026-09-16 and 2026-09-17: 404 for
    `/repos/{owner}/{repo}/`, `/repos/{owner}/{repo}/pulls/`, `/orgs/{org}/`, `/orgs/{org}/repos/`,
    `/user/repos/`, `/rate_limit/`, and for the id-keyed spellings of the first four —
    `/repositories/{id}/`, `/repositories/{id}/pulls/`, `/organizations/{id}/` and
    `/organizations/{id}/repos/` — and 200 for `/repos/{owner}/{repo}/contents/`. So this fires on
    the redirect alone — see :func:`_would_redirect_to_the_slash_free_path` — and leaves a trailing
    slash a route does match to that route.

    The five `x-ratelimit-*` headers ride on this 404, via `rate_limit_headers`, for a caller that
    sent no `Authorization` header at all, and on no other: the anonymous limit is counted by
    address, ahead of and independent of routing, where a credential's window only starts once a
    route is reached (anonymous `/repos/psf/requests/` answered `used` 45, 46 then 47 across a pair
    of bad-bearer 404s that carried no headers and moved no window between them, measured
    2026-09-17) — see `_github_routes` for a header real reads as no credential and
    for a matched route, which draw the same line.

    `redirect_slashes` is a setting of the whole app's `Router`, shared by every vendor mounted
    here, and the vendors measured answer a trailing slash each their own way (Notion, Atlassian and
    Google have middlewares of their own below) — so this intercepts ahead of routing rather than
    turning the flag off for all of them. Registered inside `resolve_github_id_paths`, so an
    id-keyed path is already its login-keyed spelling by the time the question is asked and refuses
    its slash with the rest; and outside `report_github_rate_limit` and the version echo, so a
    refused path reaches the rate limiter only through the call below and never carries the echo.

    An anonymous caller's spent rate-limit window outranks this 404 too: `GET /repos/{owner}/{repo}/`
    answers real's 403 rather than this 404 once the window is spent, `server: Varnish` (measured
    against api.github.com 2026-09-23) — this middleware runs OUTSIDE `report_github_rate_limit`, so
    a path this middleware intercepts would otherwise never reach ``rate_limit_refusal`` at all. A
    token is unaffected: the same window spent under a token still answers this 404 (measured the
    same day, on `/search/issues/`), which is why the refusal is checked only for a caller with no
    `Authorization` header, matching the header-reporting branch below it.

    `/rate_limit/` with no `Authorization` header is neither: it is answered by the `server:
    fasthttp` front that answers `/rate_limit` itself, `404 Not Found` in `text/plain` with none of
    the five, never counted (`used` stayed put across two of them) and never refused, the anonymous
    `core` window spent or not (measured against api.github.com 2026-09-23). Any `Authorization`
    header — a valid token, a bad bearer or `Basic` — gets the JSON 404 below, from `server:
    github.com`.
    """
    anonymous = "authorization" not in request.headers
    if anonymous and request.url.path.rstrip("/") == github.RATE_LIMIT_PATH:
        return PlainTextResponse("404 Not Found", status_code=404)
    if anonymous:
        refusal = github.rate_limit_refusal(request)
        if refusal is not None:
            return refusal
    response = await _http_exception_handler(request, StarletteHTTPException(status_code=404))
    if anonymous:
        headers = github.rate_limit_headers(request, response.status_code)
        for name, value in headers.items():
            response.headers[name] = value
    return response


@_middleware(lambda scope: errors.notion.owns(_url_path(scope)))
async def read_a_notion_body_as_express_does(request: Request, call_next):
    """Read a `/notion` request's body the way Express reads one in front of every Notion route,
    and answer the refusal it draws there, ahead of the route, the credential and the version
    (``notion.front_door_body``). Registered before the trailing-slash rewrite below, which makes it
    the inner of the two, so the path it derives an answer's ids from is the one the route sees."""
    refusal = await notion.front_door_body(request)
    return refusal if refusal is not None else await call_next(request)


def _is_guid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


@_middleware(lambda scope: _url_path(scope).startswith("/msgraph/"))
async def read_a_graph_request_the_way_graph_does(request: Request, call_next):
    """A `/msgraph` request reaches the Teams routes as Graph reads it, and answers with the headers
    Graph sends.

    Two `Authorization` headers are refused by the front Graph sits behind before anything is read,
    with an HTML page rather than a Graph error, and an `OPTIONS` or `HEAD` with an empty 405,
    credential or none. Every other request has its path rewritten to the spelling the routes match
    (``msteams.normalize_path``), and every answer carries the request's id as `request-id` and
    `client-request-id` — the ids its `innerError` names, the second being the caller's own where it
    sent a GUID one — with a 200's JSON served as OData under `OData-Version: 4.0` (measured against
    graph.microsoft.com, 2026-10-21).
    """
    if len(request.headers.getlist("authorization")) > 1:
        return Response(
            msteams.DUPLICATE_HEADER_PAGE,
            status_code=400,
            media_type="text/html; charset=us-ascii",
            headers={"Connection": "close"},
        )
    msteams.normalize_path(request.scope)
    request_id = str(uuid.uuid4())
    sent = request.headers.get("client-request-id", "")
    client_id = sent if _is_guid(sent) else None
    reset = msgraph_errors.REQUEST_ID.set(request_id)
    reset_client = msgraph_errors.CLIENT_REQUEST_ID.set(client_id)
    try:
        if request.method in ("OPTIONS", "HEAD"):
            response = Response(status_code=405)
        else:
            response = await call_next(request)
    finally:
        msgraph_errors.REQUEST_ID.reset(reset)
        msgraph_errors.CLIENT_REQUEST_ID.reset(reset_client)
    response.headers["request-id"] = request_id
    response.headers["client-request-id"] = client_id or request_id
    if response.status_code == 200 and response.headers.get("content-type", "").startswith(
        "application/json"
    ):
        response.headers["content-type"] = msgraph_errors.JSON_MEDIA_TYPE
        response.headers["OData-Version"] = "4.0"
    return response


def _a_slashed_notion_path(scope: Scope) -> bool:
    path = _url_path(scope)
    return path.startswith("/notion/") and path.endswith("/") and len(path) > len("/notion/")


@_middleware(_a_slashed_notion_path)
async def serve_a_slashed_notion_path_as_the_path_without_it(request: Request, call_next):
    """One trailing slash on a `/notion` path is not part of the path: real serves what the
    slash-free spelling serves.

    Measured against api.notion.com on 2026-09-22: `GET /v1/users/me/` and `POST /v1/search/`
    answer the credential's 401, the same as without the slash, where `GET /v1/users/me//` is
    `invalid_request_url` at 400 — so exactly one slash is dropped and a second is a path segment
    like any other. The rewrite runs ahead of routing because ``notion.unmatched_router`` would
    otherwise claim every slashed spelling and answer 400 where real answers what the route
    answers.

    The vendor root is not rewritten: `/notion/` is the URL's 400, and `/notion` is Starlette's own
    307 to that, where real's `/` is a 302 to its marketing site — a page this server does not
    serve at all.

    GitHub's trailing slash is the opposite rule and has its own middleware above; the two are
    measured separately because the vendors answer differently.
    """
    trimmed = request.url.path[:-1]
    request.scope["path"] = trimmed
    request.scope["raw_path"] = trimmed.encode()
    return await call_next(request)


def _a_slashed_hubspot_path(scope: Scope) -> bool:
    path = _url_path(scope)
    return (
        path.startswith("/hubspot/")
        and path.endswith("/")
        and len(path.rstrip("/")) > len("/hubspot")
    )


@_middleware(_a_slashed_hubspot_path)
async def serve_a_slashed_hubspot_path_as_the_path_without_it(request: Request, call_next):
    """Trailing slashes on a `/hubspot` path are not part of the path: real serves what the
    slash-free spelling serves, where Starlette's router would answer a 307 to it.

    Measured against api.hubapi.com with a service key: on 2026-10-19, `GET` on the contacts
    listing, a contact and its v4 associations, and `POST` on `search` and `batch/read`, each with a
    slash, answered 200; the associations page and the search answer were byte for byte the
    slash-free ones, and the contact was the same contact. On 2026-10-22 `contacts//` and
    `contacts///` were the listing and `contacts/1//` the 404 page `contacts/1` gets, so every
    trailing slash is dropped."""
    trimmed = request.url.path.rstrip("/")
    request.scope["path"] = trimmed
    request.scope["raw_path"] = trimmed.encode()
    return await call_next(request)


def _google_route_match(scope: Scope) -> Match:
    """How the best Google route matches this scope: in full, by its path alone (``PARTIAL``, the
    router's 405), or not at all. Asked of ``backlot.routers.google.router``, which holds the Google
    routes and nothing else."""
    best = Match.NONE
    for route in google.router.routes:
        match = route.matches(scope)[0]
        if match is Match.FULL:
            return match
        if match is Match.PARTIAL:
            best = match
    return best


def _google_path_not_routed_as_sent(scope: Scope) -> bool:
    """Whether this scope's path is a Google family path whose raw path ends in a slash, or that no
    Google route serves under its method."""
    path = scope["path"]
    if not any(path.startswith(f"{p}/") for p in errors.google.PREFIXES):
        return False
    raw = scope.get("raw_path") or path.encode()
    return raw.endswith(b"/") or _google_route_match(scope) is not Match.FULL


@_middleware(_google_path_not_routed_as_sent)
async def route_a_google_path_the_way_google_does(request: Request, call_next):
    """Trailing slashes on a Google family path are not part of the path: real serves what the
    slash-free spelling serves, however many there are, refusals included. Measured 2026-10-11
    against the five hosts, on the spellings ``tests/test_google.py::_SLASHED_ROWS`` lists. And an
    operation Google publishes that no route here serves under the request's method is refused as a
    served route refuses before it runs, ahead of the router's 404 or 405
    (``backlot.routers.google.refuse_an_unserved_operation``).

    An escaped slash is not one of them — `values/A1%2F` is real's 400 `Unable to parse range: A1/`
    — so the slashes counted are the raw path's. Nor is a run of slashes inside a path:
    `/drive/v3//files` and `users/me//labels` are real's unrouted 404, and so are `values` and
    `values/`, whose range would be empty. With the slash gone no route here matches `values`, and
    the router's `redirect_slashes` would answer it with a 307 to `values/`, which the route reading
    `values/{a1_range:path}` takes; so a path that matches no route once its slashes are gone is the
    unrouted 404 here.

    The raw path keeps the slashes, because a Drive download sent as a batch part is redirected to
    its path as sent, slashes and all, which ``backlot.routers.google._drive_batch_redirect`` builds
    from the raw bytes. The path is the scope's rather than ``request.url``'s (:data:`_REPARSED`).
    """
    path = request.scope["path"]
    raw = request.scope.get("raw_path") or path.encode()
    slashes = len(raw) - len(raw.rstrip(b"/"))
    request.scope["path"] = path[: len(path) - slashes]
    match = _google_route_match(request.scope)
    if match is not Match.FULL:
        try:
            google.refuse_an_unserved_operation(request)
        except StarletteHTTPException as exc:
            return await _http_exception_handler(request, exc)
        if match is Match.NONE:
            return await _http_exception_handler(request, StarletteHTTPException(status_code=404))
    return await call_next(request)


@_middleware(lambda scope: scope["path"].startswith(f"{errors.atlassian.PREFIX}/"))
async def normalise_the_slashes_in_an_atlassian_path(request: Request, call_next):
    """Route an `/atlassian` path the way the real gateway does: runs of slashes are one, and a
    trailing slash is not part of the path.

    Measured on Atlassian Cloud, 2026-09-22: `/rest/api/3/serverInfo/`, `/rest/api/2/field/`,
    `/rest/api/3/issue/{key}/`, `/wiki/rest/api/space/`, `/wiki/rest/api/content/` and
    `/wiki/rest/api/space/{key}/` each answer 200, as do the same paths with the slash doubled and
    `/rest/api/3//serverInfo` with the run in the middle; a `HEAD` and an `OPTIONS` on the slashed
    spelling answer what the slash-free one answers.

    What the request ECHOES is not normalised the same way, which is why the path is kept: real
    collapses an interior run in `detail`, `instance` and Confluence's `null for uri:` message
    (`/api/3//nope` comes back `/api/3/nope`) but leaves a trailing slash in all three
    (`/nopesuchroute/` comes back `/nopesuchroute/`). Nor are its escapes: real echoes the path as
    it arrived, decoding only some of them (``errors.atlassian.echoed_spelling``), where routing
    reads the decoded path. That spelling is built from `raw_path` before the rewrite below
    replaces it, and stashed on the scope for ``backlot.routers.atlassian._echoed_path`` to read.

    Ahead of routing, because the answer for a path no route matches is a route of its own
    (`atlassian.unmatched_router`), which would otherwise claim every slashed spelling of a served
    one. GitHub's trailing slash is the opposite rule and has its own middleware above -- the two
    vendors are measured separately.
    """
    # the decoded path as the server parsed it rather than `request.url`'s (:data:`_REPARSED`): an
    # escaped `#` would hide a trailing slash after it (`POST /rest/api/3/issue/A%23B/` is real's
    # 405, 2026-10-17)
    path = request.scope["path"]
    collapsed = re.sub("/{2,}", "/", path)
    routed = collapsed.rstrip("/")
    # `/atlassian/` is the mount itself rather than a path under it: stripping its slash leaves a
    # path no route matches, which Starlette answers with a 307 back to the spelling that arrived.
    # It keeps its slash and reaches the catch-all like any other unserved path.
    if routed == errors.atlassian.PREFIX:
        routed = collapsed
    raw = request.scope.get("raw_path") or path.encode()
    request.scope["atlassian_echo_path"] = errors.atlassian.echoed_spelling(
        raw.decode("latin-1"), confluence=errors.atlassian.is_confluence(path)
    )
    if routed != path:
        request.scope["path"] = routed
        request.scope["raw_path"] = routed.encode()
    return await call_next(request)


@_middleware(
    lambda scope: _url_path(scope).startswith(("/github/repositories/", "/github/organizations/"))
)
async def resolve_github_id_paths(request: Request, call_next):
    """Serve `/github/repositories/{id}/…` and `/github/organizations/{id}/…` as what the
    login-keyed paths serve, because that is the form real's page urls take (see
    ``_page_base_url``) and a page url a client cannot follow is worse than no header at all.

    A rewrite rather than a redirect: real answers 200 on `/repositories/{id}/collaborators`
    directly, so a 302 would be a shape a client only meets here.

    The rewrite happens on the PREFIX, whatever the id turns out to name, so that these paths
    answer in the order the named ones do — see ``canonical_id_path`` for why resolving first would
    tell an unauthenticated caller which ids the corpus holds.
    """
    path = request.url.path
    acl = getattr(request.app.state, "acl", None)
    canonical = github.canonical_id_path(
        request.app.state.conn, getattr(acl, "org_name", None) or get_settings().org_name, path
    )
    if canonical is not None:
        request.scope["path"] = canonical
        request.scope["raw_path"] = canonical.encode()
    return await call_next(request)


@_middleware(
    lambda scope: (
        scope["method"] != "OPTIONS"
        and _url_path(scope).startswith("/atlassian/rest/")
        and auth.atlassian_bearer_unreadable(Request(scope))
    )
)
async def refuse_a_bearer_jira_cannot_read(request: Request, call_next):
    """Refuse a Jira read whose bearer the real gateway would not read, before the route runs.

    Unlike the Basic pair, which Jira serves anonymously, an unreadable bearer is refused — and
    refused ahead of every route, behind only the front door
    (:func:`answer_at_the_atlassian_front_door`), so `serverInfo` and `field` answer it too even
    though neither needs a credential. That is why this short-circuits rather than living in
    ``atlassian._jira_caller``. Confluence is not here: it answers its own 403 for any credential
    that fails, which ``atlassian._confluence_caller`` already gives. Measured on two Jira Cloud
    sites on 2026-09-04.

    The body is spelled the way real spells it, a space after the colon (fifteen of them on
    2026-09-30), or is the XML for the `Accept` values ``errors.atlassian.connect_token_answer``
    names, and this runs inside :func:`answer_head_as_the_get_without_its_body`: a
    `HEAD` is refused as its GET is and so carries no `content-length`, as real's did on
    `serverInfo` and on `nopesuchroute` the same day. An `OPTIONS` is not refused here: real answers
    one with an unreadable bearer as it answers one with none
    (``backlot.routers.atlassian._options_answer``).
    """
    media_type, body = errors.atlassian.connect_token_answer(request.headers.get("accept"))
    return Response(body, status_code=403, media_type=media_type)


@_middleware(
    lambda scope: scope["method"] == "HEAD" and errors.google.refuses_head(_url_path(scope))
)
async def refuse_a_head_on_the_google_editor_hosts(request: Request, call_next):
    """Refuse a `HEAD` on Docs, Sheets or Slides the way their host does, before anything else
    reads the request: ``errors.google.head_refusal``, where the measurement is.

    Inside the `HEAD` middleware below, whose tuple leaves these prefixes out, so the method is
    still `HEAD` when it gets here.
    """
    return errors.google.head_refusal()


# The path prefixes whose `HEAD` is the GET with the body left off. GitHub, Atlassian, Notion,
# Drive and Gmail because each is measured to be, and a vendor is added here once its own is rather
# than by a rewrite that assumes they share GitHub's: both Atlassian products answered a `HEAD` with
# the `GET`'s status and `content-type` and nothing in the body, on every route served here and on
# an unknown issue's and an unknown space's 404, measured 2026-09-22 and on Confluence's
# `child/attachment` 2026-10-10, and api.notion.com answered `HEAD /v1/users/me` the credential's
# 401 and `HEAD /v1/nonexistent_thing/xyz` the URL's 400 on 2026-09-22, each with the
# `content-length` and `content-type` of the GET body beside it (178 and 145 bytes). Drive and
# Gmail answered each route served here that way on 2026-10-11, refusals included
# (``errors.google.head_content_length`` lists them), and the other three Google hosts refuse the
# method (``errors.google.refuses_head``, asked by the middleware above). What the vendors do NOT
# share is the length — see ``errors.head_content_length``, asked below. `/health` and `/_meta` are
# Backlot's own routes, with no vendor to measure against: a `HEAD /health` is the shape a liveness
# probe takes, and with `/health` left out of this tuple it is the 405 with `allow: GET` a GitHub
# route gets with `/github` left out.
_HEAD_IS_THE_GET_WITHOUT_ITS_BODY = (
    "/github",
    "/atlassian",
    "/notion",
    "/drive/v3",
    "/gmail/v1",
    "/hubspot",
    "/health",
    "/_meta",
)


def _spring_failure(scope: Scope):
    """Which of ``atlassian.spring_sort_failure`` and ``atlassian.spring_write_failure``
    :func:`answer_an_atlassian_write_that_fails` hands this request's answer to, or ``None``
    where it hands the answer on as it is."""
    path = _url_path(scope)
    if not errors.atlassian.owns(path):
        return None
    # the product the gateway routes to, which reads each run of slashes as one
    # (``errors.atlassian.front_door``): under an `Accept` Spring cannot sort and under one it
    # cannot write its answer under, `GET` and `OPTIONS` on `//wiki/rest/api/space` and
    # `GET //wiki/rest/api/content` drew what each draws with one slash (Confluence Cloud,
    # 2026-10-25)
    product = re.sub("/{2,}", "/", path)
    reader = "spring" if errors.atlassian.is_confluence(product) else "jira"
    accept = Headers(scope=scope).get("accept")
    if errors.atlassian.spring_cannot_sort(accept, reader=reader):
        return atlassian.spring_sort_failure
    if errors.atlassian.quality_spring_cannot_copy(accept, reader=reader):
        return atlassian.spring_write_failure
    return None


@_middleware(lambda scope: _spring_failure(scope) is not None)
async def answer_an_atlassian_write_that_fails(request: Request, call_next):
    """Answer as real does where a Spring service could not sort `Accept`'s members
    (``errors.atlassian.spring_cannot_sort``) or could not write its answer under it
    (``errors.atlassian.quality_spring_cannot_copy``); what each answer becomes is
    ``backlot.routers.atlassian.spring_sort_failure``'s and ``spring_write_failure``'s.

    Middleware because what Spring would have answered decides what replaces it: this reads the
    answer the route or the refusal gave and replaces it. Registered before the `HEAD` rewrite
    below, so it reads a `HEAD`'s answer as its GET's, and the front door and the headers are put on
    what it gives as on any answer.
    """
    answer = _spring_failure(request.scope)
    response = await call_next(request)
    body = b"".join([chunk async for chunk in response.body_iterator])
    failed = answer(request, response.status_code, response.headers, body)
    if failed is None:
        failed = Response(body, status_code=response.status_code)
        failed.raw_headers = response.raw_headers
    return failed


@_middleware(
    lambda scope: (
        scope["method"] == "HEAD"
        and _at_or_under(_url_path(scope), _HEAD_IS_THE_GET_WITHOUT_ITS_BODY)
    )
)
async def answer_head_as_the_get_without_its_body(request: Request, call_next):
    """Answer a `HEAD` as the `GET` with the body left off, which is how real GitHub, both Atlassian
    products, Notion, Drive, Gmail and HubSpot answer one.

    FastAPI's ``APIRoute`` does not add `HEAD` to a GET route the way Starlette's ``Route`` does, so
    without this a `HEAD` on a route that serves the GET is a 405, whatever the GET answers. Real
    GitHub answers the GET's own status and headers with nothing in the body: `content-length` of
    the body the GET would have carried and `Link` where the GET has one, on the 200s, the 404 for a
    repository that does not exist, the 401 for no credential, the 422 for a blank search `q` and
    code search's text/plain 400 alike (measured against api.github.com on 2026-09-07 with
    `curl -I`, each `HEAD` beside its `GET` the same minute). An existence check,
    `requests.head(url)` or `curl -I`, is what a client sends a `HEAD` for, and a 405 for both the
    repository that exists and the one that does not cannot tell them apart.

    A middleware rather than `HEAD` in each route's ``methods``: FastAPI writes a `head` operation
    into `/openapi.json` for every method a route declares, where real's own description declares no
    `head` operation at all, so declaring it would hand `backlot diff --source github` operations
    real lacks and the MCP slice tools that answer nothing a GET does not. The method is rewritten
    on the scope before routing, so the GET runs in full: the router's dependencies, the handler and
    every middleware defined above this one — the version echo, the rate-limit count, the slash and
    id-path rewrites and the Connect-token refusal among them — see a GET and land on the answer by
    construction, and the charset middleware outside it rewrites the copied `content-type` as it
    does the GET's. The body is read to the end to be measured rather than sent, because the
    `content-length` a client reads a `HEAD` for is the GET body's length and computing the body is
    the only way to have that number; a `HEAD` costs what its GET costs, here as on real.

    What the vendors do NOT share is the `content-length`: GitHub, Notion and HubSpot declare the
    length of the body the `GET` would have carried (HubSpot's `HEAD` on `search` declared the 229
    of its `GET`'s 405 page, measured 2026-10-19), and Atlassian's and Google's answers split, so
    that header is asked for rather than assumed, through ``errors.head_content_length``. The
    request is marked ``answering_head`` on its state for ``errors.google.jsonp_callback`` and
    ``routers.github._validate_api_version``, which read it.

    The method goes back to `HEAD` on the scope once the GET has answered, because the server frames
    the response by it: uvicorn's httptools protocol reads ``scope["method"]`` when it writes the
    body, sends nothing for a `HEAD`, and for a `GET` holds the body to the declared
    `content-length`, so with the scope left saying `GET` the empty body this middleware sends was
    `RuntimeError: Response content shorter than Content-Length` in the server log and a reset
    connection for the client's next request (measured over uvicorn on a one-record corpus: a
    `requests.Session` that sent a `HEAD` had its following `GET /health` fail with
    `ConnectionResetError`; with the method restored that request is a 200 and the log is clean).
    """
    request.scope["method"] = "GET"
    request.state.answering_head = True
    response = await call_next(request)
    request.scope["method"] = "HEAD"
    length = 0
    async for chunk in response.body_iterator:
        length += len(chunk)
    head = Response(status_code=response.status_code, headers=response.headers)
    declares = errors.head_content_length(request, response.status_code)
    if declares is False:
        del head.headers["content-length"]
    else:
        head.headers["content-length"] = str(length)
    return head


@_middleware(
    lambda scope: (
        _url_path(scope).startswith(f"{errors.atlassian.PREFIX}/")
        and auth.basic_names_a_user(Request(scope))
    )
)
async def report_failed_jira_login(request: Request, call_next):
    """Say that a Jira read presented a credential Backlot could not resolve, as real Jira does.

    Jira serves those reads anonymously rather than refusing them (see
    ``backlot.routers.atlassian._jira_caller``) and reports the failure only in this header, on
    every answer it gives — the 200s included, which is why this is middleware rather than
    something a refusal path could carry. The header is keyed on the username alone, and
    Confluence sends it on nothing. Measured against ecosystem.atlassian.net and
    brekkylab.atlassian.net on 2026-09-04.
    """
    response = await call_next(request)
    # the path once the slash rewrite inside this has run
    if request.url.path.startswith("/atlassian/rest/"):
        if auth.atlassian_caller(request).is_anonymous:
            response.headers["X-Seraph-LoginReason"] = "AUTHENTICATED_FAILED"
    return response


def _atlassian_front_door(scope: Scope):
    """The page :func:`answer_at_the_atlassian_front_door` answers this request with, or ``None``
    for a request it hands on."""
    path = _url_path(scope)
    if not errors.atlassian.owns(path):
        return None
    # the decoded path stands in for a missing `raw_path`: the pages echo no path verbatim (the
    # CloudFront id is a hash of it, the rest are static)
    raw_path = scope.get("raw_path") or path.encode()
    return errors.atlassian.front_door(
        scope["method"],
        raw_path.decode("latin-1"),
        scope.get("query_string", b"").decode("latin-1"),
        Headers(scope=scope).get("accept"),
    )


@_middleware(lambda scope: _atlassian_front_door(scope) is not None)
async def answer_at_the_atlassian_front_door(request: Request, call_next):
    """Answer an `/atlassian` request the way a layer in front of Jira and Confluence does, where
    one answers it: the CDN, its firewall, the gateway and Confluence's Tomcat, each with its own
    page (which, and in what order, is ``errors.atlassian.front_door``).

    Registered after the slash rewrite, the Connect-token refusal, the `HEAD` rewrite and the
    Seraph header, so it runs ahead of each of them: a `HEAD` arrives here as a `HEAD` (real
    declares each page's length on one and sends no body), and none of the other three runs for a
    request this refuses, as none of them runs on real. Whose headers the answer carries
    (``errors.atlassian.FrontDoor``) is left on the scope for
    ``backlot.routers.atlassian.vendor_headers``.
    """
    answer = _atlassian_front_door(request.scope)
    request.scope["atlassian_front_door"] = answer.layer
    body = answer.body.encode()
    headers = {"Content-Type": answer.media_type, **answer.headers}
    if request.method == "HEAD":
        headers["Content-Length"] = str(len(body))
        body = b""
    return Response(body, status_code=answer.status, headers=headers)


@_middleware(lambda scope: errors.atlassian.owns(_url_path(scope)))
async def report_atlassian_headers(request: Request, call_next):
    """Put on every `/atlassian` answer the headers real sends beside the body: which ones, on
    which answers, and what each is measured from is ``backlot.routers.atlassian.vendor_headers``.

    Middleware for the reason GitHub's rate-limit headers are: they ride on answers no route
    handler builds — the exception handlers' refusals, the Connect-token 403 above, the catch-all's
    404 — and a client logging an id for a failed call needs the failed calls to carry one.

    Deliberately not served: `set-cookie`, which real sends as an XSRF token on an ANONYMOUS Jira
    200 alone (measured 2026-09-22; none of the authenticated answers carried one) and which would
    change what a browser-shaped client does next; `atl-confluence-via`, whose value names the
    Atlassian host that served the request and which Backlot has nothing to derive from;
    `strict-transport-security`, which real's CDN sends on every answer and which pins a host to
    HTTPS in a browser that reads it, so it has no place on a server reached over plain HTTP; and
    `vary`, which names the negotiation real's edge does on `Accept-Encoding` and this server does
    not do.
    """
    response = await call_next(request)
    for name, value in atlassian.vendor_headers(request, response.status_code).items():
        response.headers[name] = value
    return response


def _some_slack_route_matches(scope: Scope) -> bool:
    """Whether a Slack route matches this scope's path, whatever the method. Asked of
    ``backlot.routers.slack.router``, which holds the Slack routes and nothing else."""
    return any(route.matches(scope)[0] is not Match.NONE for route in slack.router.routes)


@_middleware(
    lambda scope: (
        scope["method"] in slack.UNSERVED_METHODS
        and scope["path"].startswith("/slack/api/")
        and not _some_slack_route_matches(scope)
    )
)
async def answer_an_unserved_slack_method(request: Request, call_next):
    """A request to a `/slack/api/` path no Slack route matches, by one of
    ``slack.UNSERVED_METHODS``, is answered as real answers it (``slack.unserved_answer``).
    Registered before ``parse_slack_form``, which makes it the inner of the two: a body real refuses
    is refused first, as real refuses it, and a token sent in the body is read here."""
    answer = slack.unserved_answer(request)
    if answer is not None:
        return answer
    return await call_next(request)


@_middleware(
    lambda scope: (
        scope["method"] != "GET"
        and scope["method"] in slack.UNSERVED_METHODS
        and slack.method_name(scope["path"]) is not None
    )
)
async def parse_slack_form(request: Request, call_next):
    """Read the body of a Slack request with one, any of ``slack.UNSERVED_METHODS`` but `GET`, to a
    path ``slack.method_name`` reads as a method's name, served here or not, before the method runs,
    as real does (``slack.read_body``): answer one real refuses with its error, or stash the
    arguments it carries for the router's param lookup and the token's (``slack._param``,
    ``auth.slack_token``)."""
    method = slack.method_name(request.scope["path"])
    content_type = slack.post_content_type(request)
    args, error = slack.read_body(request.method, content_type, await request.body(), method)
    if error is not None:
        request.state.slack_body_refused = True
        return JSONResponse({"ok": False, "error": error})
    request.state._form = args
    return await call_next(request)


@_middleware(lambda scope: _url_path(scope).startswith("/slack/") and scope["method"] == "POST")
async def warn_slack_post_charset(request: Request, call_next):
    """Add the charset warning ``slack.post_charset_warning`` decides to a Slack POST's answer.

    Middleware because the answers it goes on are built all over the router, the refusals of
    ``slack._caller_or_error`` and ``slack._missing_argument`` among them. The body is rendered
    again by ``JSONResponse``, which renders every Slack answer, so a warned answer is the unwarned
    one's bytes with the two keys added; ``slack.attach_warning`` says where they go. A body
    ``parse_slack_form`` refuses is answered with no warning, as real answers it.
    """
    warning = slack.post_charset_warning(
        slack.post_content_type(request), bool(await request.body())
    )
    response = await call_next(request)
    refused = getattr(request.state, "slack_body_refused", False)
    if warning is None or response.status_code != 200 or refused:
        return response

    body = b"".join([chunk async for chunk in response.body_iterator])
    headers = {k: v for k, v in response.headers.items() if k != "content-length"}
    return JSONResponse(
        slack.attach_warning(json.loads(body), warning),
        status_code=response.status_code,
        headers=headers,
    )


#: The page Cloudflare, in front of Linear and Fireflies, answers a request carrying `Authorization`
#: more than once with: `text/html` with no charset, CRLF line ends, 155 bytes.
_CLOUDFLARE_REPEATED_AUTHORIZATION = (
    "<html>\r\n<head><title>400 Bad Request</title></head>\r\n<body>\r\n"
    "<center><h1>400 Bad Request</h1></center>\r\n<hr><center>cloudflare</center>\r\n"
    "</body>\r\n</html>\r\n"
)


@_middleware(
    lambda scope: (
        _at_or_under(_url_path(scope), ("/linear", "/fireflies"))
        and auth.repeated_authorization(Request(scope))
    )
)
async def refuse_a_repeated_authorization_on_linear_and_fireflies(request: Request, call_next):
    """Answer a `/linear` or `/fireflies` request that carries `Authorization` more than once with
    :data:`_CLOUDFLARE_REPEATED_AUTHORIZATION`'s 400, ahead of the route, the body and the
    credential. api.linear.app and api.fireflies.ai answered it on 2026-10-19 (UTC) to two copies of
    a key, a key and `nope` in either order, a key and an empty value, two empty values, three
    copies and a second header named in lower case; on `POST /graphql` with a query, with no bytes,
    with `{` and with a body past each vendor's size limit, on `GET /graphql`, on `HEAD /graphql`,
    which declared the 155 bytes and sent none, and on `POST /nosuch` and `POST /`."""
    return Response(
        _CLOUDFLARE_REPEATED_AUTHORIZATION.encode(),
        status_code=400,
        headers={"Content-Type": "text/html"},
    )


@_middleware(lambda scope: _url_path(scope).startswith("/github"))
async def put_a_request_id_on_github_answers(request: Request, call_next):
    """Put an `x-github-request-id` on every `/github` answer, as real does on each one measured.

    Registered after the other `/github` middlewares so it wraps them, and the refusals they answer
    early carry it too. Made before the route runs (``backlot.routers.github.request_id`` has why)
    and left on ``request.state`` for the token's spent-window `message`, which quotes it
    (``github._rate_limit_exceeded_message``)."""
    rid = github.request_id(request.method, request.url.path, request.url.query)
    request.state.github_request_id = rid
    response = await call_next(request)
    response.headers.setdefault(github.REQUEST_ID_HEADER, rid)
    return response


# The Google paths whose reads real answers when they arrive as a POST that names one in
# `X-HTTP-Method-Override` (`answer_a_google_read_sent_as_a_post`), each with the overrides it reads
# as the GET once the spaces and tabs around one are gone. Measured on 2026-10-25: Drive's `about`
# and `files.list` read `GET` and `HEAD` in any case (`get`, `gET`, `hEAD`); Gmail's `labels.list`
# and `messages.list` read the two as spelt and not `get`, `Get` or `head`; and Docs'
# `documents.get` and Sheets' `spreadsheets.get` and `values.get` read `GET` as spelt and not `get`,
# `Get`, `HEAD` or `head`. On Drive's `about`, Gmail's `labels.list` and Sheets' `values.get`,
# `\xa0GET`, `\x85GET` and two `X-HTTP-Method-Override` headers, `GET` both times among them, were
# not read.
_GOOGLE_READ_OVERRIDES = {
    "/drive/v3": ("GET", "HEAD"),
    "/gmail/v1": ("GET", "HEAD"),
    "/docs/v1": ("GET",),
    "/sheets/v4": ("GET",),
}
_GOOGLE_OVERRIDE_IN_ANY_CASE = "/drive/v3"
_FORM_MEDIA_TYPE = "application/x-www-form-urlencoded"


@_middleware(
    lambda scope: (
        scope["method"] == "POST"
        and any(scope["path"].startswith(f"{prefix}/") for prefix in _GOOGLE_READ_OVERRIDES)
    )
)
async def answer_a_google_read_sent_as_a_post(request: Request, call_next):
    """A Google read sent as a POST that names it in `X-HTTP-Method-Override` is answered as the
    GET it stands for, its form body read as the query string after the URL's own. Which overrides
    each family reads is :data:`_GOOGLE_READ_OVERRIDES`.

    The Google API Python client sends a read this way on its own: `HttpRequest.execute`
    (google-api-python-client 2.201.0) turns a GET whose URI passes 2048 characters into a POST with
    `x-http-method-override: GET`, `content-type: application/x-www-form-urlencoded` and the query
    string as the body. Measured with a Workspace member's token: on www.googleapis.com/drive/v3 on
    2026-10-21, `files.list` with a 2,058-byte form body and `about` with `fields=kind` each
    answered 200 and what the same parameters get on a GET. On 2026-10-23 so did `files.get`,
    `drives.list` and `changes/startPageToken`, Docs' `documents.get` and Sheets' `values.get`, and
    on 2026-10-25, with a `gmail.readonly` token, Gmail's `labels.list`, `messages.list` and
    `getProfile`. A `HEAD` read there was answered as the GET, body and all. On 2026-10-25 the body
    was read on each of the four when its `Content-Type`, the first where two were sent, held
    `application/x-www-form-urlencoded` anywhere and in any case, as in
    `text/plain;application/x-www-form-urlencoded`, where `application/x-www-form-urlencode` was not
    read. It was not read under no `Content-Type` or `text/plain` on any of the four, nor under
    `application/json` or `multipart/form-data` on `about`, where `about` was the 400 a GET with no
    `fields` gets. A `fields` in the URL was read over one in the body, as the first of two in a
    GET's query is. A POST naming `PUT`, `PATCH`, `DELETE` or `GET, PUT` goes on as a POST: real
    answered each on `about` as it answers a `PUT` there, with its 404 page, and on 2026-10-25 so it
    answered a `PUT`, `PATCH` or `DELETE` naming `GET` there.

    Registered just before ``vendor_json_media_type``, outside every middleware that reads the
    method or the query, so they and the router see the GET.
    """
    path = request.scope["path"]
    prefix = next(p for p in _GOOGLE_READ_OVERRIDES if path.startswith(f"{p}/"))
    sent = request.headers.getlist("x-http-method-override")
    override = sent[0].strip(" \t") if len(sent) == 1 else ""
    if prefix == _GOOGLE_OVERRIDE_IN_ANY_CASE:
        override = override.upper()
    if override in _GOOGLE_READ_OVERRIDES[prefix]:
        form = _FORM_MEDIA_TYPE in request.headers.get("content-type", "").lower()
        body = await request.body() if form else b""
        query = request.scope.get("query_string", b"")
        request.scope["method"] = "GET"
        request.scope["query_string"] = b"&".join(part for part in (query, body) if part)
    return await call_next(request)


@_middleware(lambda scope: errors.has_json_media_type(_url_path(scope)))
async def vendor_json_media_type(request: Request, call_next):
    """Put the vendor's own `content-type` on a JSON body, where one is measured.

    Real GitHub answers `application/json; charset=utf-8` on every JSON response but code search's
    (see ``backlot.errors.github.json_media_type``), Jira `application/json;charset=UTF-8` on
    every `application/json` body but its gateway's 403
    (``backlot.errors.atlassian.json_media_type``), and Notion `application/json; charset=utf-8` on
    every JSON answer (``backlot.errors.notion.json_media_type``); FastAPI's ``JSONResponse``
    answers `application/json`. A middleware rather than a ``default_response_class`` on the router,
    because FastAPI writes a response class's media type into the OpenAPI document as the content
    key, and real's own spec says `application/json` there: the charset is a fact about the wire,
    not about the contract, and `backlot diff` compares the contract. Only a response that is
    exactly `application/json` is touched, so the raw, diff and text/plain answers keep their own
    types. Registered last of the middlewares, which makes it the outermost, so a JSON body another
    middleware returns (``refuse_a_bearer_jira_cannot_read``'s 403) passes through it too rather
    than around it; the route handlers and the two exception handlers above are covered from either
    position, those handlers because they run inside ``ExceptionMiddleware``. The status goes along
    with the path because the answer can depend on it: code search's own 200 and 422 carry no
    charset where the gateway's 401 on the same path does. An answer given in front of Jira is not
    Jira's, and keeps the type its layer gives (the edge's `;` 400,
    ``errors.atlassian.front_door``).
    """
    response = await call_next(request)
    if response.headers.get("content-type") == "application/json" and not request.scope.get(
        "atlassian_front_door"
    ):
        media_type = errors.json_media_type(request.url.path, response.status_code)
        if media_type is not None:
            response.headers["content-type"] = media_type
    return response


# Registered after every middleware above, which makes it the outermost: it sees every answer,
# including one a middleware gives without reaching a route.
app.add_middleware(TelemetryMiddleware)


@app.get("/health")
async def health(request: Request):
    # Two counts, deliberately. `documents` sums the root-document tables only (SOURCE_TABLE, not
    # COMMENT_TABLE); `source_documents` is what the corpus OFFERED, which is smaller because
    # parsing turns one Slack transcript into many messages. Reporting only the larger inflates.
    counts = getattr(request.app.state, "doc_counts", None)
    warm_error = getattr(request.app.state, "warm_error", None)
    # `degraded`, not a non-200: the corpus is still served correctly (see the fallbacks), so
    # failing the check would take down a working server. Only `ok` with null counts is wrong.
    body = {
        "status": "degraded" if warm_error else "ok",
        "source_documents": getattr(request.app.state, "source_documents", None),
    }
    if counts is not None:
        body["documents"] = sum(counts.values())
        body["by_source"] = counts
    else:
        body["documents"] = None
        body["by_source"] = {}
    if warm_error:
        # Reported even when the counts landed: the warm-up fills three caches in sequence, so it
        # can fail part-way.
        body["warm_error"] = warm_error
    return body


@app.get("/_meta/users")
async def meta_users(request: Request):
    """Directory of every generated user + their token, for testing per-user ACL.

    Not part of any emulated vendor API — a Backlot-only affordance. Present each user's
    token in the same shape as ``data/tokens.yaml`` plus the groups they belong to, so a
    caller can pick a token, send it to any of the APIs, and see the ACL-filtered view.
    S3 doesn't use bearer tokens — it uses AWS SigV4 — so each user (and the admin) also
    carries an ``s3_access_key_id`` / ``s3_secret_access_key`` pair (derived from the token,
    which is what the SigV4 verifier resolves) to hand straight to boto3 / the AWS CLI.
    The admin/service token bypasses all filtering. Always served: ``backlot mcp --user <email>``
    resolves a person to their whole credential set here.
    """
    conn = request.app.state.conn
    acl = request.app.state.acl
    tok = acl.email_to_token()
    # Only users with a token: everyone else the corpus names is display-only (an author or owner,
    # not an identity you can authenticate as).
    users = [
        {
            "email": u["email"],
            "name": u["display_name"],
            "token": tok[u["email"]],
            "s3_access_key_id": synth.s3_access_key_id(tok[u["email"]]),
            "s3_secret_access_key": synth.s3_secret_access_key(tok[u["email"]]),
            "groups": store.user_group_ids(conn, u["email"]),
        }
        for u in store.list_users(conn)
        if u["email"] in tok
    ]
    return {
        "org": acl.org_name,
        "admin_token": acl.admin_token,
        "admin_s3_access_key_id": synth.s3_access_key_id(acl.admin_token),
        "admin_s3_secret_access_key": synth.s3_secret_access_key(acl.admin_token),
        "count": len(users),
        "users": users,
    }


@app.get("/_meta/credentials")
async def meta_credentials(request: Request):
    """Directory of Google-style OAuth client credentials, for driving connectors that
    configure with an OAuth client / service account rather than a raw access token.

    Returns only the **shared** credentials: the single ``oauth_client`` (client_id/secret) and
    the org ``service_account`` JSON (with its private key). There is no per-user data here — a
    user's ``refresh_token`` is simply their bearer token from ``/_meta/users``, so build an
    ``authorized_user`` credential by combining ``oauth_client`` + a token from ``/_meta/users`` +
    ``token_uri``. ``token_uri`` points back at Backlot's ``/oauth2/token``, so the client's
    refresh / JWT-bearer exchange lands here. Impersonate a user with the service account by
    setting ``subject=<email>``; a bare service account (no subject) resolves to the
    admin/service token. Backlot-only affordance. See ``examples/using-official-sdk/gmail.py``.
    """
    o = getattr(request.app.state, "oauth", None)
    if o is None:
        raise HTTPException(status_code=404, detail="Not Found")
    token_uri = f"{request.url.scheme}://{request.headers.get('host', 'localhost')}/oauth2/token"
    return {
        "org": request.app.state.acl.org_name,
        "token_uri": token_uri,
        "oauth_client": o.client_config(),
        "service_account": o.service_account_json(token_uri),
    }


@app.get("/_meta/openapi/{source}")
async def meta_openapi(source: str, request: Request):
    """An MCP-ready OpenAPI spec for one source: the app's own ``/openapi.json`` sliced to that
    source and with its GET/POST and v2/v3 fidelity aliases collapsed to one operation each, so an
    OpenAPI→MCP bridge can feed it straight to ``FastMCP.from_openapi()`` (see ``backlot.openapi``)."""
    if source not in openapi.SOURCE_PREFIXES:
        raise HTTPException(
            status_code=404,
            detail=f"no MCP spec for {source!r}; one of {sorted(openapi.SOURCE_PREFIXES)}",
        )
    return openapi.build_mcp_spec(request.app.openapi(), source)


def _require_admin(request: Request) -> None:
    """Refuse a caller who is not the admin/service token.

    Used by the one `/_meta` route that destroys state. The rest of the namespace is open on
    purpose — `/_meta/users` hands out every user's token — but "this server is a fixture" is an
    argument about READS.
    """
    caller = app.state.acl.resolve(auth.bearer_token(request))
    if caller is None or not caller.is_admin:
        raise HTTPException(status_code=403, detail="admin token required")


@app.get("/_meta/overlay")
def meta_overlay():
    """Everything written since the last reset — the read an evaluation's grader makes.

    An ACL oracle asks what an agent did, and answering it by diffing the served surface against
    itself means knowing what the surface looked like before. The overlay already IS that
    difference, so it is reported directly: the documents written, the grants they carry, the
    columns overwritten, and the documents subtracted.

    The FTS index is left out. It holds no fact the document rows do not.

    Unauthenticated, like every other `/_meta` route: `/_meta/users` already hands out every
    user's token to any caller, so this namespace is a test fixture's control surface rather than
    a secured one, and a gate on this route alone would be the only one of its kind.
    """
    conn = app.state.conn
    out: dict[str, list[dict]] = {}
    for src in sorted(store.WRITABLE):
        names = [n for n in overlay.table_names(src).values() if not n.endswith("_fts_ov")]
        for name in names:
            out[name] = [dict(r) for r in conn.execute(f"SELECT * FROM ov.{name}")]
    return out


@app.post("/_meta/overlay/reset")
def meta_overlay_reset(request: Request):
    """Throw the overlay away — an evaluation run's teardown.

    Every write goes, including a tombstone, so a message deleted in one run is back for the next.
    That is what makes two runs over one server comparable. The corpus is untouched either way; it
    was never opened writable.

    Admin token only, unlike every other `/_meta` route — this is the one that DESTROYS
    something. An evaluation is graded from `/_meta/overlay`, so an agent that could reach this
    without a credential could erase the record of what it had just done, which is the one thing
    the oracle exists to hold.
    """
    _require_admin(request)
    overlay.reset(app.state.conn, app.state.overlay_name)
    # The member-count cache is left alone: it holds the corpus's own counts, which a write
    # never moved (`slack._member_count`), so a reset has nothing to put back.
    return {"ok": True}


app.include_router(oauth.router)
app.include_router(slack.router)
app.include_router(msteams.router)
# after the routes it serves, so only a path none of them match reaches it
app.include_router(msteams.unmatched_router)
app.include_router(google.router)
app.include_router(github.router)
app.include_router(atlassian.router)
# after the routes it serves, so only a path none of them match reaches it
app.include_router(atlassian.unmatched_router)
app.include_router(notion.router)
# after the routes it serves, so only a path none of them match reaches it
app.include_router(notion.unmatched_router)
app.include_router(s3.router)
app.include_router(hubspot.router)
app.include_router(linear.router)
app.include_router(fireflies.router)
