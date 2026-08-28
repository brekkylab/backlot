"""FastAPI app hosting every emulated vendor API under path prefixes.

Startup opens the read-only DB, loads the ACL/token map, and starts a background cache warm-up.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backlot import errors, openapi, store, synth
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
    body = errors.validation_body(request.url.path, exc.errors())
    if body is None:
        body = {"detail": jsonable_encoder(exc.errors())}
    return JSONResponse(status_code=422, content=body)


@app.middleware("http")
async def echo_github_api_version(request: Request, call_next):
    """Report which API version served the response, as real GitHub does on every github request.

    Middleware rather than a router dependency: a dependency that sets headers on its injected
    ``Response`` loses them whenever the route returns a ``Response`` itself, which the raw-content
    and diff media types do (see ``backlot.routers.github``) — so exactly the responses whose SHAPE
    the header describes would ship without it.

    A rejected version gets no echo, matching real: it selected nothing. That is `None` from
    ``selected_api_version``, the same call the router's 400 is raised from.
    """
    response = await call_next(request)
    if request.url.path.startswith("/github"):
        version = github.selected_api_version(request)
        if version is not None:
            response.headers[github.SELECTED_VERSION_HEADER] = version
    return response


@app.middleware("http")
async def parse_slack_form(request: Request, call_next):
    """Slack SDK POSTs urlencoded params; stash them for the router's param lookup."""
    if request.url.path.startswith("/slack/") and request.method == "POST":
        ctype = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in ctype:
            request.state._form = dict(await request.form())
    return await call_next(request)


@app.get("/health")
async def health():
    # Two counts, deliberately. `documents` sums the root-document tables only (SOURCE_TABLE, not
    # COMMENT_TABLE); `source_documents` is what the corpus OFFERED, which is smaller because
    # parsing turns one Slack transcript into many messages. Reporting only the larger inflates.
    counts = getattr(app.state, "doc_counts", None)
    warm_error = getattr(app.state, "warm_error", None)
    # `degraded`, not a non-200: the corpus is still served correctly (see the fallbacks), so
    # failing the check would take down a working server. Only `ok` with null counts is wrong.
    body = {
        "status": "degraded" if warm_error else "ok",
        "source_documents": getattr(app.state, "source_documents", None),
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
async def meta_users():
    """Directory of every generated user + their token, for testing per-user ACL.

    Not part of any emulated vendor API — a Backlot-only affordance. Present each user's
    token in the same shape as ``data/tokens.yaml`` plus the groups they belong to, so a
    caller can pick a token, send it to any of the APIs, and see the ACL-filtered view.
    S3 doesn't use bearer tokens — it uses AWS SigV4 — so each user (and the admin) also
    carries an ``s3_access_key_id`` / ``s3_secret_access_key`` pair (derived from the token,
    which is what the SigV4 verifier resolves) to hand straight to boto3 / the AWS CLI.
    Disable with ``BACKLOT_EXPOSE_TOKENS=false``. The admin/service token bypasses all filtering.
    """
    settings = get_settings()
    if not settings.expose_tokens:
        raise HTTPException(status_code=404, detail="Not Found")
    conn = app.state.conn
    acl = app.state.acl
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
    admin/service token. Backlot-only affordance; disable with ``BACKLOT_EXPOSE_TOKENS=false``. See
    ``examples/using-official-sdk/gmail.py``.
    """
    settings = get_settings()
    o = getattr(app.state, "oauth", None)
    if not settings.expose_tokens or o is None:
        raise HTTPException(status_code=404, detail="Not Found")
    token_uri = f"{request.url.scheme}://{request.headers.get('host', 'localhost')}/oauth2/token"
    return {
        "org": app.state.acl.org_name,
        "token_uri": token_uri,
        "oauth_client": o.client_config(),
        "service_account": o.service_account_json(token_uri),
    }


@app.get("/_meta/openapi/{source}")
async def meta_openapi(source: str):
    """An MCP-ready OpenAPI spec for one source: the app's own ``/openapi.json`` sliced to that
    source and with its GET/POST and v2/v3 fidelity aliases collapsed to one operation each, so an
    OpenAPI→MCP bridge can feed it straight to ``FastMCP.from_openapi()`` (see ``backlot.openapi``)."""
    if source not in openapi.SOURCE_PREFIXES:
        raise HTTPException(
            status_code=404,
            detail=f"no MCP spec for {source!r}; one of {sorted(openapi.SOURCE_PREFIXES)}",
        )
    return openapi.build_mcp_spec(app.openapi(), source)


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
