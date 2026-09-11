"""FastAPI app hosting every emulated vendor API under path prefixes.

Startup opens the read-only corpus DB, attaches the writable mutation overlay to it, loads the
ACL/token map, and starts a background cache warm-up. The corpus file is never opened writable —
see `backlot/overlay.py`.
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

from backlot import auth, errors, openapi, overlay, store, synth
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
    # A DB older than a table this build reads would answer every request that touches it with a
    # bare OperationalError, one per read, with nothing saying why. Named once here instead.
    if stale := store.missing_tables(conn):
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


def _openapi_with_github_page_parameters() -> dict:
    return openapi.github_page_parameters(_fastapi_openapi(), github.PAGE_PARAMETERS)


app.openapi = _openapi_with_github_page_parameters


# Per-vendor error envelopes live in ``backlot/errors/``. Both handlers ask that package and fall
# back to FastAPI's ``{"detail": ...}``.


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    headers = getattr(exc, "headers", None)
    body = errors.http_body(request.url.path, exc)
    if body is None:
        body = {"detail": exc.detail}
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    answer = errors.validation_body(request.url.path, exc.errors())
    if answer is None:
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
    status_code, body = answer
    return JSONResponse(status_code=status_code, content=body)


@app.middleware("http")
async def echo_github_api_version(request: Request, call_next):
    """Report which API version served the response, as real GitHub does on every github request.

    Middleware rather than a router dependency: a dependency that sets headers on its injected
    ``Response`` loses them whenever the route returns a ``Response`` itself, which the raw-content
    and diff media types do (see ``backlot.routers.github``) — so exactly the responses whose SHAPE
    the header describes would ship without it.

    A rejected version gets no echo, matching real: it selected nothing. That is `None` from
    ``selected_api_version``, the same call the router's 400 is raised from. Code search gets no
    echo either, whatever it pinned: real's code search backend does not read the header (see
    ``github.honours_api_version``).
    """
    response = await call_next(request)
    if request.url.path.startswith("/github") and github.honours_api_version(request):
        version = github.selected_api_version(request)
        if version is not None:
            response.headers[github.SELECTED_VERSION_HEADER] = version
    return response


@app.middleware("http")
async def report_github_rate_limit(request: Request, call_next):
    """Put the five `x-ratelimit-*` headers on every `/github` answer and count it against the
    caller's hourly window, as real does on every response it gives, 200 and error alike (see
    ``backlot.routers.github.rate_limit_headers``).

    Middleware for the reason the version echo is: the headers ride on answers no route handler
    builds, the exception handlers' 401s and 404s and the raw and diff media types' own responses
    among them, and a client paces by them on every one. Registered inside the `HEAD` middleware,
    so a `HEAD` runs through here as the GET it is rewritten to and counts once, as it does on real
    (`remaining` 46 → 45 across one `HEAD`, measured 2026-09-09), and the head copies the five
    with the rest of the GET's headers. A path outside `/github` gets nothing: the other vendors'
    rate-limit answers are not measured.
    """
    response = await call_next(request)
    if request.url.path.startswith("/github"):
        for name, value in github.rate_limit_headers(request, response.status_code).items():
            response.headers[name] = value
    return response


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


# The path prefixes whose `HEAD` is the GET with the body left off. GitHub because it is measured to
# be, and none of the other vendors' `HEAD` answers is, so a vendor is added here once its own is
# rather than by a rewrite that assumes they share GitHub's. `/health` and `/_meta` are Backlot's
# own routes, with no vendor to measure against: a `HEAD /health` is the shape a liveness probe
# takes, and `FastAPI`'s `APIRoute` refused it with the same 405 for the same reason.
_HEAD_IS_THE_GET_WITHOUT_ITS_BODY = ("/github", "/health", "/_meta")


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
    (see ``backlot.errors.github.json_media_type``); FastAPI's ``JSONResponse`` answers
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
    columns overwritten, the documents subtracted, and the memberships changed.

    The FTS index is left out. It holds no fact the document rows do not.

    Unauthenticated, like every other `/_meta` route: `/_meta/users` already hands out every
    user's token to any caller, so this namespace is a test fixture's control surface rather than
    a secured one, and a gate on this route alone would be the only one of its kind.
    """
    conn = app.state.conn
    out: dict[str, list[dict]] = {}
    for src in sorted(store.WRITABLE):
        names = [n for n in overlay.table_names(src).values() if not n.endswith("_fts_ov")]
        names += list(overlay.EXTRA_DDL.get(src, {}))
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
    # The per-channel member counts were computed against the pre-write corpus and are correct for
    # it again, so the cache is rebuilt rather than left holding a written channel's number.
    app.state.channel_members = None
    return {"ok": True}


app.include_router(oauth.router)
app.include_router(slack.router)
app.include_router(google.router)
app.include_router(github.router)
app.include_router(atlassian.router)
app.include_router(notion.router)
app.include_router(s3.router)
app.include_router(hubspot.router)
app.include_router(linear.router)
app.include_router(fireflies.router)
