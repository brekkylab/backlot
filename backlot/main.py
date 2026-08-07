"""FastAPI app hosting every vendor mock under path prefixes.

Startup opens the read-only SQLite DB, loads the ACL/token map, and builds reverse
indexes (issue number / Jira key / Confluence id -> doc_id) for O(1) get-by-id.
"""

from __future__ import annotations

import http
import threading
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backlot import google_errors, openapi, store, synth
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


def _build_index(conn) -> dict:
    idx = {
        "github": {},
        "jira": {},
        "confluence": {},
        "notion": {},
        "s3": {},
        "hubspot": {},
        "gmail": {},
        "linear": {},
        "linear_teams": {},
        "linear_users": {},
        "linear_states": {},
        "linear_projects": {},
        "linear_cycles": {},
        "linear_labels": {},
        "linear_releases": {},
        "fireflies_users": {},
    }
    # Gmail ids are 16-hex integers, not dsids, so the served id has to be reversed back to a row.
    # ONE map covers messages AND threads: a thread key is the root message's doc_id (verified on
    # the bench corpus -- 0 of 121,390 thread keys is anything else), which is also why real Gmail
    # reports id == threadId for a lone message. Measured cost on the 556,238-message bench corpus:
    # +2.2s and +88 MiB, taking this whole function from 6.4s to ~8.6s, with 0 collisions.
    for r in conn.execute(f"SELECT doc_id FROM {store.table('gmail')}"):
        idx["gmail"][synth.gmail_message_id(r["doc_id"])] = r["doc_id"]
    # kind='file' rows (source-code docs) are never looked up by number -- excluding them keeps
    # a file's synthesized number from colliding with (and shadowing) a real issue/PR's.
    for r in conn.execute(
        f"SELECT doc_id, {store.grouping_col('github')} AS container "
        f"FROM {store.table('github')} WHERE kind IS NULL OR kind != 'file'"
    ):
        idx["github"][(r["container"], synth.github_number(r["doc_id"]))] = r["doc_id"]
    for r in conn.execute(
        f"SELECT doc_id, {store.grouping_col('jira')} AS container FROM {store.table('jira')}"
    ):
        idx["jira"][synth.jira_key(r["doc_id"], synth.jira_project_key(r["container"]))] = r[
            "doc_id"
        ]
    for r in conn.execute(f"SELECT doc_id FROM {store.table('confluence')}"):
        idx["confluence"][synth.confluence_id(r["doc_id"])] = r["doc_id"]
    # Notion ids are dashed UUIDs; key the index by the dashless form so a client sending either
    # dashed or dashless (both valid to real Notion) resolves — see routers.notion._norm.
    for r in conn.execute(f"SELECT doc_id FROM {store.table('notion')}"):
        idx["notion"][synth.notion_id(r["doc_id"]).replace("-", "")] = r["doc_id"]
    for r in conn.execute(f"SELECT doc_id, bucket, key FROM {store.table('s3')}"):
        idx["s3"][f"{r['bucket']}/{r['key']}"] = r["doc_id"]
    # HubSpot record ids are numeric strings; the CRM routes and the v4 association payload both
    # speak them, so one index resolves either back to a doc_id.
    for r in conn.execute(f"SELECT doc_id FROM {store.table('hubspot')}"):
        idx["hubspot"][synth.hubspot_record_id(r["doc_id"])] = r["doc_id"]
    # Linear's `issue(id:)` accepts the UUID *or* the human identifier (ENG-123), and `team(id:)`
    # the team UUID or its key — so one dict per entity resolves either spelling back to the row.
    # The bench's identifiers are NOT unique (5,055 keys repeat) and two containers can reduce to
    # one team key, so both use setdefault: the first row in doc_id/name order wins and the
    # mapping stays stable across restarts, while the UUID form always addresses a row exactly.
    # Exactly (doc_id, identifier), in that order: idx_linear_doc_ident covers it, so this is an
    # index-only scan and never touches the wide issue rows.
    for r in conn.execute(
        f"SELECT doc_id, identifier FROM {store.table('linear')} ORDER BY doc_id"
    ):
        idx["linear"][synth.linear_id(r["doc_id"])] = r["doc_id"]
        if r["identifier"]:
            idx["linear"].setdefault(r["identifier"], r["doc_id"])
    for r in store.list_containers(conn, "linear"):
        idx["linear_teams"][synth.linear_team_id(r["name"])] = r["name"]
        idx["linear_teams"].setdefault(synth.linear_team_key(r["name"]), r["name"])
        # A mock affordance on top of the two real spellings: the container's own name. Costs
        # nothing and saves a caller from having to derive `ENG` from `engineering` by hand.
        idx["linear_teams"].setdefault(r["name"], r["name"])
    # `@linear/sdk` resolves an issue's relations LAZILY: `await issue.state` issues a fresh
    # `workflowState(id: <uuid>)`. Those uuids are one-way hashes of a name, so the only way to
    # answer is a reverse map built here. Each source list is a DISTINCT over one column (see
    # store.linear_distinct_values), so this is a handful of scans of one table, not per-row work.
    distinct = store.linear_distinct_values(conn)
    for email, display in distinct["users"]:
        idx["linear_users"][synth.linear_user_id(email)] = (email, display)
    for team, name in distinct["states"]:
        idx["linear_states"][synth.linear_state_id(name, team)] = (team, name)
    for name in distinct["projects"]:
        idx["linear_projects"][synth.linear_project_id(name)] = name
    for team, name in distinct["cycles"]:
        idx["linear_cycles"][synth.linear_cycle_id(name, team)] = (team, name)
    for name in distinct["labels"]:
        idx["linear_labels"][synth.linear_label_id(name)] = name
    for name in distinct["releases"]:
        idx["linear_releases"][synth.linear_release_id(name)] = name
    # Fireflies needs NO transcript-id index: `transcript(id:)` resolves against the stored
    # `transcript_id` column (indexed), because unlike Linear's identifier that id is unique by
    # construction. Only `user_id` needs reversing — it is a one-way hash of an address, and both
    # `user(id:)` and the `transcripts(user_id:)` filter accept it.
    for r in store.list_users(conn):
        idx["fireflies_users"][synth.fireflies_user_id(r["email"])] = r["email"]
    return idx


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if not settings.db_path.exists():
        raise RuntimeError(
            f"DB not found at {settings.db_path}. Build it first: "
            "python -m backlot.importer.erb  (or: python -m backlot.importer.byo <corpus.jsonl>)"
        )
    # A BYO import records the corpus-derived org in tokens.yaml; adopt it so the routers
    # (which read get_settings().org_name/org_domain) stay consistent with the ACL. An erb
    # (bench) tokens.yaml has no org, so the settings defaults stand.
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
    app.state.index = _build_index(conn)

    # One indexed lookup, not a background warm-up like doc_counts below — the value can't
    # change while the server runs. None on a DB built before the meta table existed.
    _src = store.read_meta(conn, "source_documents")
    app.state.source_documents = int(_src) if _src is not None else None

    # Per-source COUNT(*) can be slow on a very large / cold DB, so compute it once in a
    # background thread (its own RO connection) and cache it — /health then stays O(1) and never
    # blocks the ALB health check, even right after a cold start.
    app.state.doc_counts = None
    # channel -> {principals granted on any of its docs}, so conversations.list can decide a
    # non-admin caller's visible channels by set-intersection (O(channels)) instead of a
    # per-request doc_acl⋈messages join that scales with the docs granted to the caller.
    app.state.channel_acl = None
    # channel -> its member count (its distinct speakers). conversations.info/.list report it
    # for every channel in a page, and a per-channel COUNT(DISTINCT) is far too slow for that.
    app.state.channel_members = None

    def _warm_caches():
        c = store.connect_ro(
            settings.db_path,
            mmap_mb=settings.sqlite_mmap_mb,
            cache_mb=settings.sqlite_cache_mb,
            temp_memory=True,
        )
        try:
            cacl: dict[str, set] = {}
            for ch, pid in c.execute(
                "SELECT DISTINCT d.channel, a.principal_id "
                "FROM doc_acl a JOIN slack_messages d ON d.doc_id = a.doc_id"
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

    # Kept on app.state (rather than fire-and-forget) so a caller — namely tests — can wait for
    # it deterministically instead of polling /health and hoping doc_counts landed in time.
    app.state.warm_thread = threading.Thread(target=_warm_caches, daemon=True)
    app.state.warm_thread.start()
    try:
        yield
    finally:
        conn.close()


app = FastAPI(
    title="EnterpriseRAG-Bench Mock Server",
    lifespan=lifespan,
    # NOT FastAPI's default, which derives the id's method suffix from a set and so
    # changes between restarts — see openapi.unique_operation_id.
    generate_unique_id_function=openapi.unique_operation_id,
)


# Atlassian clients (atlassian-python-api, used by mcp-atlassian) parse error bodies as Atlassian
# Cloud's envelope — Confluence's raise_for_status does ``response.json()["message"]`` — so
# FastAPI's default ``{"detail": ...}`` makes every error a cryptic ``KeyError: 'message'`` in the
# client. For ``/atlassian`` paths, shape errors like Atlassian (message + statusCode, plus Jira's
# errorMessages); every other prefix keeps FastAPI's default body.


def _atlassian_error_body(status_code: int, detail) -> dict:
    try:
        reason = http.HTTPStatus(status_code).phrase
    except ValueError:
        reason = "Error"
    message = detail if isinstance(detail, str) else str(detail)
    return {
        "statusCode": status_code,
        "message": message,
        "reason": reason,
        "errorMessages": [message],
        "errors": {},
    }


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    headers = getattr(exc, "headers", None)
    path = request.url.path
    if path.startswith("/atlassian"):
        return JSONResponse(
            status_code=exc.status_code,
            content=_atlassian_error_body(exc.status_code, exc.detail),
            headers=headers,
        )
    if google_errors.family(path) is not None:
        return JSONResponse(
            status_code=exc.status_code, content=google_errors.body(path, exc), headers=headers
        )
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.detail}, headers=headers
    )


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/atlassian"):
        msg = "; ".join(e.get("msg", "invalid request") for e in exc.errors()) or "Invalid request"
        return JSONResponse(status_code=422, content=_atlassian_error_body(422, msg))
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


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
    # O(1): return the cached per-source counts (see lifespan). `by_source` is {} for the brief
    # window after a cold start until the background count finishes.
    #
    # Two counts, deliberately. `documents` sums store.SOURCE_TABLE only — the 11 root-document
    # tables. It does NOT include store.COMMENT_TABLE (jira/confluence/github/notion/linear
    # comments, fireflies_sentences): those rows are served too, each with its own vendor
    # endpoint, but they're children of a root doc rather than documents themselves, so they
    # aren't counted here. `source_documents` is what the corpus offered, which is smaller than
    # `documents` because faithful parsing turns one Slack transcript into many message rows.
    # Publishing only the larger of the two reads as inflation, which is why both are reported.
    counts = getattr(app.state, "doc_counts", None)
    body = {"status": "ok", "source_documents": getattr(app.state, "source_documents", None)}
    if counts is not None:
        body["documents"] = sum(counts.values())
        body["by_source"] = counts
    else:
        body["documents"] = None
        body["by_source"] = {}
    return body


@app.get("/_mock/users")
async def mock_users():
    """Directory of every generated user + their token, for testing per-user ACL.

    Not part of any emulated vendor API — a mock-only affordance. Present each user's
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
    # Only authenticating users (those with a bearer token) are listed — the org's real roster.
    # Other people the corpus references are display-only: they appear as owners/authors on
    # documents, but aren't identities you can pick a token for here.
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


@app.get("/_mock/credentials")
async def mock_credentials(request: Request):
    """Directory of Google-style OAuth client credentials, for driving connectors that
    configure with an OAuth client / service account rather than a raw access token.

    Returns only the **shared** credentials: the single ``oauth_client`` (client_id/secret) and
    the org ``service_account`` JSON (with its private key). There is no per-user data here — a
    user's ``refresh_token`` is simply their bearer token from ``/_mock/users``, so build an
    ``authorized_user`` credential by combining ``oauth_client`` + a token from ``/_mock/users`` +
    ``token_uri``. ``token_uri`` points back at this mock's ``/oauth2/token``, so the client's
    refresh / JWT-bearer exchange lands here. Impersonate a user with the service account by
    setting ``subject=<email>``; a bare service account (no subject) resolves to the
    admin/service token. Mock-only affordance; disable with ``BACKLOT_EXPOSE_TOKENS=false``. See
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


@app.get("/_mock/openapi/{source}")
async def mock_openapi(source: str):
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
