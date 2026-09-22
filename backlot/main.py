"""FastAPI app hosting every emulated vendor API under path prefixes.

Startup opens the read-only DB, loads the ACL/token map, and starts a background cache warm-up.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match
from starlette.types import Scope

from backlot import auth, errors, openapi, store, synth
from backlot.acl import Acl
from backlot.config import get_settings
from backlot.oauth import Oauth
from backlot.routers import (
    atlassian,
    fireflies,
    github,
    google,
    hubspot,
    linear,
    notion,
    oauth,
    s3,
    slack,
)


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
        data = yaml.safe_load(settings.tokens_path.read_text()) or {}
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
    app.state.acl = Acl.load(settings.tokens_path, settings.admin_token, settings.org_name)
    app.state.oauth = Oauth.load(settings.credentials_path)  # None if credentials.yaml absent

    _src = store.read_meta(conn, "source_documents")
    app.state.source_documents = int(_src) if _src is not None else None

    # Three caches the warm-up below fills, each too slow to compute per request on a large corpus:
    # per-source COUNT(*), channel -> principals granted on any of its docs, and channel -> member
    # count. Every consumer treats None as "not warm yet" and falls back to its own query.
    app.state.doc_counts = None
    app.state.channel_acl = None
    app.state.channel_members = None

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
                cacl: dict[str, set] = {}
                for ch, pid in c.execute(
                    f"SELECT DISTINCT a.channel, a.principal_id FROM {store.acl_table('slack')} a"
                ):
                    cacl.setdefault(ch, set()).add(pid)
                app.state.channel_acl = {k: frozenset(v) for k, v in cacl.items()}
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
    try:
        yield
    finally:
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
        vendor = errors.method_not_allowed(request.url.path, request.method)
        if vendor is not None:
            if vendor.headers is None:
                vendor.headers = getattr(exc, "headers", None)
            exc = vendor
    headers = getattr(exc, "headers", None)
    body = errors.http_body(request.url.path, exc, request.query_params)
    if body is None:
        body = {"detail": exc.detail}
    # A vendor may decide how the body reaches the wire as well as what is in it — Google's errors
    # are indented to the byte and a `callback` on a GET answers one at 200 as a script. Asking the
    # envelope keeps that where the rest of that vendor's error shape lives, and leaves Atlassian
    # and GitHub with exactly the JSONResponse they had. Measured 2026-09-15, that is right for
    # both and for different reasons: Jira and Confluence ignore `callback` outright, on an error
    # and on a success, while GitHub honours it through an envelope of its own — `/**/cb({"meta":
    # …, "data": …})` under `application/javascript; charset=utf-8`, with the status inside `meta`
    # and an unparseable name refused UNWRAPPED — which is a shape to build, not one to share.
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


def _some_github_route_matches(scope: Scope) -> bool:
    """Whether some mounted route matches this scope's path, whatever the method.

    A 404 for a path no route matches at all carries no version echo, and carries the five
    `x-ratelimit-*` headers only for a request that sent no `Authorization` header at all — the
    same line `refuse_a_trailing_slash_on_github` draws for the trailing-slash spelling of such a
    path, which real draws for every spelling. Measured against api.github.com 2026-09-21:
    `/repos/psf/requests/<unmatched>`, its trailing-slash spelling and a top-level `/<unmatched>`
    all answered 404 with no echo, where `/repos/psf/requests/issues/<missing>` — a route that DOES
    match, on a resource that does not exist — carried both for either caller. With no
    `Authorization` header the three carried the five (`limit` 60) and moved `used` by one apiece.
    Every header value carried none of them and moved no window: a valid token (whose `used` read 0
    before three of them and 0 after), a bad bearer, `Basic Zm9vOmJhcg==` and a scheme-less value —
    so what real reads here is that a credential arrived, not that one resolved, where the same
    unparseable `Basic` on a route that matches is served and counted.
    """
    return any(route.matches(scope)[0] is not Match.NONE for route in app.router.routes)


@app.middleware("http")
async def echo_github_api_version(request: Request, call_next):
    """Report which API version served the response, on the `/github` answers real echoes one for,
    200 and error alike.

    Middleware rather than a router dependency: a dependency that sets headers on its injected
    ``Response`` loses them whenever the route returns a ``Response`` itself, which the raw-content
    and diff media types do (see ``backlot.routers.github``) — so exactly the responses whose SHAPE
    the header describes would ship without it.

    A rejected version gets no echo, matching real: it selected nothing. That is `None` from
    ``selected_api_version``, the same call the router's 400 is raised from. Code search gets no
    echo either, whatever it pinned: real's code search backend does not read the header, and
    neither does `/rate_limit` asked with no `Authorization` header (see
    ``github.honours_api_version``). Nor does a 404 for a path no route matches at all, whatever
    the caller's credential — see ``_some_github_route_matches`` — nor real's "Bad credentials"
    401 (``github.refused_a_credential``).
    """
    response = await call_next(request)
    if (
        request.url.path.startswith("/github")
        and github.honours_api_version(request)
        and _some_github_route_matches(request.scope)
        and not github.refused_a_credential(request)
    ):
        version = github.selected_api_version(request)
        if version is not None:
            response.headers[github.SELECTED_VERSION_HEADER] = version
    return response


@app.middleware("http")
async def report_github_rate_limit(request: Request, call_next):
    """Put the five `x-ratelimit-*` headers on the `/github` answers real carries them on, 200 and
    error alike, and count each against the caller's window for the resource (see
    ``backlot.routers.github.rate_limit_headers``).

    Middleware for the reason the version echo is: the headers ride on answers no route handler
    builds, the exception handlers' 401s and 404s and the raw and diff media types' own responses
    among them, and a client paces by them on every one. Registered inside the `HEAD` middleware,
    so a `HEAD` runs through here as the GET it is rewritten to and counts once, as it does on real
    (`remaining` 46 → 45 across one `HEAD`, measured 2026-09-09), and the head copies the five
    with the rest of the GET's headers. A path outside `/github` gets nothing: the other vendors'
    rate-limit answers are not measured. A 404 for a path no route matches at all gets them only
    for a request that carried no `Authorization` header at all — see
    ``_some_github_route_matches`` — and a credential that did not resolve gets them on no path at
    all (``github.refused_a_credential``).
    """
    response = await call_next(request)
    if (
        request.url.path.startswith("/github")
        and not github.refused_a_credential(request)
        and (_some_github_route_matches(request.scope) or "authorization" not in request.headers)
    ):
        for name, value in github.rate_limit_headers(request, response.status_code).items():
            response.headers[name] = value
    return response


def _would_redirect_to_the_slash_free_path(request: Request) -> bool:
    """Whether the router's `redirect_slashes` would answer this path with a 307 to its slash-free
    spelling.

    That is the whole of what real has no equivalent for, so it is the whole of what gets
    intercepted. Starlette redirects only when the path AS SENT matches no route and the slash-free
    spelling matches one, so a route whose last segment is a `{path:path}` — `/contents/{path:path}`
    matches the empty string — answers its own trailing slash and never reaches the redirect, the
    same as real answers it.
    """
    scope = request.scope
    if _some_github_route_matches(scope):
        return False
    slash_free = {**scope, "path": scope["path"].rstrip("/")}
    return _some_github_route_matches(slash_free)


@app.middleware("http")
async def refuse_a_trailing_slash_on_github(request: Request, call_next):
    """A trailing slash on `/github` that matches no route is a 404, not the 307 to the slash-free
    path that Starlette's router answers by default.

    Real runs no slash redirect at all: a trailing slash is just part of the path, and what answers
    it is whichever route matches the path as sent. A route ending in a path parameter absorbs the
    slash as an empty segment — `GET /repos/{owner}/{repo}/contents/` is the root listing's own 200,
    like `/contents` beside it — and every other route simply does not match, so the request gets
    the same 404 a path with no route at all gets, ahead of a bad bearer's own 401. Measured against
    api.github.com on 2026-09-15, 2026-09-16 and 2026-09-17: 404 for `/repos/{owner}/{repo}/`,
    `/repos/{owner}/{repo}/pulls/`, `/orgs/{org}/`, `/orgs/{org}/repos/`, `/user/repos/`,
    `/rate_limit/`, and for the id-keyed spellings of the first four — `/repositories/{id}/`,
    `/repositories/{id}/pulls/`, `/organizations/{id}/` and `/organizations/{id}/repos/` — and 200
    for `/repos/{owner}/{repo}/contents/`. So this fires on the redirect alone — see
    :func:`_would_redirect_to_the_slash_free_path` — and leaves a trailing slash a route does match
    to that route.

    The five `x-ratelimit-*` headers ride on this 404, via `rate_limit_headers`, for a caller that
    sent no `Authorization` header at all, and on no other: the anonymous limit is counted by
    address, ahead of and independent of routing, where a credential's window only starts once a
    route is reached (anonymous `/repos/psf/requests/` answered `used` 45, 46 then 47 across a pair
    of bad-bearer 404s that carried no headers and moved no window between them, measured
    2026-09-17) — see `_some_github_route_matches` for the unparseable-credential and matched-route
    cases, which draw the same line.

    `redirect_slashes` is a setting of the whole app's `Router`, shared by every vendor mounted
    here, and no other vendor's own answer to a trailing slash has been measured — so this
    intercepts ahead of routing rather than turning the flag off for all of them. Registered inside
    `resolve_github_id_paths`, so an id-keyed path is already its login-keyed spelling by the time
    the question is asked and refuses its slash with the rest; and outside `report_github_rate_limit`
    and the version echo, so a refused path reaches the rate limiter only through the call below and
    never carries the echo.
    """
    path = request.url.path
    if (
        path.startswith("/github/")
        and path.endswith("/")
        and _would_redirect_to_the_slash_free_path(request)
    ):
        response = await _http_exception_handler(request, StarletteHTTPException(status_code=404))
        if "authorization" not in request.headers:
            headers = github.rate_limit_headers(request, response.status_code, count=True)
            for name, value in headers.items():
                response.headers[name] = value
        return response
    return await call_next(request)


@app.middleware("http")
async def serve_a_slashed_notion_path_as_the_path_without_it(request: Request, call_next):
    """One trailing slash on a `/notion` path is not part of the path: real serves what the
    slash-free spelling serves.

    Measured against api.notion.com on 2026-09-22: `GET /v1/users/me/` and `POST /v1/search/`
    answer the credential's 401, the same as without the slash, where `GET /v1/users/me//` is
    `invalid_request_url` at 400 — so exactly one slash is dropped and a second is a path segment
    like any other. The rewrite runs ahead of routing because the answer for a path no route
    matches is now a route of its own (``notion.unmatched_router``), which would otherwise claim
    every slashed spelling and answer 400 where real answers what the route answers; Starlette's
    own `redirect_slashes` no longer fires under `/notion/` for the same reason, and its 307 was
    not what real sends either.

    The vendor root is left alone: `/notion/` keeps the 400 every unserved URL gets, and `/notion`
    is Starlette's own 307 to that, where real's `/` is a 302 to its marketing site — a page this
    server does not serve at all.

    GitHub's trailing slash is the opposite rule and has its own middleware above; the two are
    measured separately because the vendors answer differently.
    """
    path = request.url.path
    if path.startswith("/notion/") and path.endswith("/") and len(path) > len("/notion/"):
        trimmed = path[:-1]
        request.scope["path"] = trimmed
        request.scope["raw_path"] = trimmed.encode()
    return await call_next(request)


@app.middleware("http")
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
    if path.startswith("/github/repositories/") or path.startswith("/github/organizations/"):
        acl = getattr(request.app.state, "acl", None)
        canonical = github.canonical_id_path(
            request.app.state.conn, getattr(acl, "org_name", None) or get_settings().org_name, path
        )
        if canonical is not None:
            request.scope["path"] = canonical
            request.scope["raw_path"] = canonical.encode()
    return await call_next(request)


# The path prefixes whose `HEAD` is the GET with the body left off. GitHub and Notion because each
# is measured to be, and a vendor is added here once its own is rather than by a rewrite that
# assumes they share GitHub's: api.notion.com answered `HEAD /v1/users/me` the credential's 401 and
# `HEAD /v1/nonexistent_thing/xyz` the URL's 400 on 2026-09-22, each with the `content-length` and
# `content-type` of the GET body beside it (178 and 145 bytes). `/health` and `/_meta` are Backlot's
# own routes, with no vendor to measure against: a `HEAD /health` is the shape a liveness probe
# takes, and `FastAPI`'s `APIRoute` refused it with the same 405 for the same reason.
_HEAD_IS_THE_GET_WITHOUT_ITS_BODY = ("/github", "/notion", "/health", "/_meta")


@app.middleware("http")
async def answer_head_as_the_get_without_its_body(request: Request, call_next):
    """Answer a `HEAD` as the `GET` with the body left off, which is how real GitHub answers one.

    Every GitHub route here is declared `GET` alone, and FastAPI's ``APIRoute`` does not add `HEAD`
    to a GET route the way Starlette's ``Route`` does, so a `HEAD` reached Starlette's 405 with
    `allow: GET` on every route, whatever the GET would have answered. Real answers the GET's own
    status and headers with nothing in the body: `content-length` of the body the GET would have
    carried and `Link` where the GET has one, on the 200s, the 404 for a repository that does not
    exist, the 401 for no credential, the 422 for a blank search `q` and code search's text/plain
    400 alike (measured against api.github.com on 2026-09-07 with `curl -I`, each `HEAD` beside its
    `GET` the same minute). An existence check, `requests.head(url)` or `curl -I`, is what a client
    sends a `HEAD` for, and a 405 for both the repository that exists and the one that does not
    cannot tell them apart.

    A middleware rather than `HEAD` in each route's ``methods``: FastAPI writes a `head` operation
    into `/openapi.json` for every method a route declares, where real's own description declares
    no `head` operation at all, so declaring it would hand `backlot diff --source github` operations
    real lacks and the MCP slice tools that answer nothing a GET does not. The method is rewritten
    on the scope before routing, so the GET runs in full: the router's dependencies, the handler and
    the three middlewares inside this one, the version echo, the rate-limit count and the id-path
    rewrite, see a GET and land on the answer by construction, and the charset middleware outside
    it rewrites the copied `content-type` as it does the GET's. The body is read to the end to be
    measured rather than sent, because the `content-length` a client reads a `HEAD` for is the GET
    body's length and computing the body is the only way to have that number; a `HEAD` costs what
    its GET costs, here as on real. The method goes back to `HEAD` on the scope once the GET has
    answered, because the
    server frames the response by it: uvicorn's httptools protocol reads ``scope["method"]`` when it
    writes the body, sends nothing for a `HEAD`, and for a `GET` holds the body to the declared
    `content-length`, so with the scope left saying `GET` the empty body this middleware sends was
    `RuntimeError: Response content shorter than Content-Length` in the server log and a reset
    connection for the client's next request (measured over uvicorn on a one-record corpus: a
    `requests.Session` that sent a `HEAD` had its following `GET /health` fail with
    `ConnectionResetError`; with the method restored that request is a 200 and the log is clean).
    """
    if request.method != "HEAD" or not request.url.path.startswith(
        _HEAD_IS_THE_GET_WITHOUT_ITS_BODY
    ):
        return await call_next(request)
    request.scope["method"] = "GET"
    response = await call_next(request)
    request.scope["method"] = "HEAD"
    length = 0
    async for chunk in response.body_iterator:
        length += len(chunk)
    head = Response(status_code=response.status_code, headers=response.headers)
    head.headers["content-length"] = str(length)
    return head


@app.middleware("http")
async def refuse_a_bearer_jira_cannot_read(request: Request, call_next):
    """Refuse a Jira read whose bearer the real gateway would not read, before the route runs.

    Unlike the Basic pair, which Jira serves anonymously, an unreadable bearer is refused — and
    refused ahead of everything, so `serverInfo` and `field` answer it too even though neither
    needs a credential. That is why this short-circuits rather than living in
    ``atlassian._jira_caller``. Confluence is not here: it answers its own 403 for any credential
    that fails, which ``atlassian._confluence_caller`` already gives. Measured against
    ecosystem.atlassian.net and brekkylab.atlassian.net on 2026-09-04.
    """
    if request.url.path.startswith("/atlassian/rest/") and auth.atlassian_bearer_unreadable(
        request
    ):
        return JSONResponse(status_code=403, content=errors.atlassian.connect_token_body())
    return await call_next(request)


@app.middleware("http")
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
    if request.url.path.startswith("/atlassian/rest/") and auth.basic_names_a_user(request):
        if auth.atlassian_caller(request).is_anonymous:
            response.headers["X-Seraph-LoginReason"] = "AUTHENTICATED_FAILED"
    return response


@app.middleware("http")
async def parse_slack_form(request: Request, call_next):
    """Slack SDK POSTs urlencoded params; stash them for the router's param lookup."""
    if request.url.path.startswith("/slack/") and request.method == "POST":
        ctype = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in ctype:
            request.state._form = dict(await request.form())
    return await call_next(request)


@app.middleware("http")
async def vendor_json_media_type(request: Request, call_next):
    """Put the vendor's own `content-type` on a JSON body, where one is measured.

    Real GitHub answers `application/json; charset=utf-8` on every JSON response but code search's
    (see ``backlot.errors.github.json_media_type``), and Jira `application/json;charset=UTF-8` on
    every `application/json` body but its gateway's 403
    (``backlot.errors.atlassian.json_media_type``); FastAPI's ``JSONResponse`` answers
    `application/json`. A middleware rather than a ``default_response_class`` on the router, because
    FastAPI writes a response class's media type into the OpenAPI document as the content key, and
    real's own spec says `application/json` there: the charset is a fact about the wire, not about
    the contract, and `backlot diff` compares the contract. Only a response that is exactly
    `application/json` is touched, so the raw, diff and text/plain answers keep their own types.
    Registered last of the middlewares, which makes it the outermost, so a JSON body another
    middleware returns (``refuse_a_bearer_jira_cannot_read``'s 403) passes through it too rather
    than around it; the route handlers and the two exception handlers above are covered from either
    position, those handlers because they run inside ``ExceptionMiddleware``. The status goes along
    with the path because the answer can depend on it: code search's own 200 and 422 carry no
    charset where the gateway's 401 on the same path does.
    """
    response = await call_next(request)
    if response.headers.get("content-type") == "application/json":
        media_type = errors.json_media_type(request.url.path, response.status_code)
        if media_type is not None:
            response.headers["content-type"] = media_type
    return response


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


app.include_router(oauth.router)
app.include_router(slack.router)
app.include_router(google.router)
app.include_router(github.router)
app.include_router(atlassian.router)
app.include_router(notion.router)
# after the routes it serves, so only a path none of them match reaches it
app.include_router(notion.unmatched_router)
app.include_router(s3.router)
app.include_router(hubspot.router)
app.include_router(linear.router)
app.include_router(fireflies.router)
