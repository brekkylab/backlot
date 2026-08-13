"""FastAPI app hosting every vendor mock under path prefixes.

Startup opens the read-only SQLite DB, loads the ACL/token map, and builds the reverse indexes
still needed for O(1) get-by-id: six of Linear's one-way-hashed entity ids (`linear_users`,
`linear_states`, `linear_projects`, `linear_cycles`, `linear_labels`, `linear_releases`), none of
which have a column of their own to be stored in -- they are `SELECT DISTINCT` projections over
`linear_issues` with no entity table to hold one. Every per-document served id (confluence/gmail/
notion/hubspot/linear/github/jira, #51) now resolves through a stored column instead — see
backlot.store's SERVED_ID -- and so, since task 9, do Linear's team id/key (`store.
linear_team_by_served_id`/`linear_team_by_served_key`, on `linear_teams`) and Fireflies' user id
(`store.fireflies_user_by_served_id`, on `fireflies_users`): both reverse a hash of a value
belonging to a real row (a team, a principal), not a doc_id, so they got their own small readers
instead of an entry in SERVED_ID. Jira's own project<->prefix maps (`jira_project_keys`/
`jira_project_containers`) also stay here: they are CONTAINER-level (one entry per project, not
per document), and the deferred assignment pass in backlot.importer.byo (`resolve_jira_numbers`)
needs the corpus-provided prefix to compose a key at serve time (see routers.atlassian).
"""

from __future__ import annotations

import logging
import sqlite3
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


def _build_index(conn) -> dict:
    idx = {
        "linear_users": {},
        "linear_states": {},
        "linear_projects": {},
        "linear_cycles": {},
        "linear_labels": {},
        "linear_releases": {},
        # container -> the project-key prefix its corpus-provided issue keys carry, and
        # the reverse — so `project = PAY` JQL, the project picker and every issue's
        # `fields.project.key` speak the spelling the documents cite. CONTAINER-level (one
        # entry per project, not per document) — unlike the per-document key resolution,
        # which is now a stored `served_number` column instead (#51, task 8; see
        # backlot.importer.byo's `resolve_jira_numbers` and store.jira_by_served_number).
        "jira_project_keys": {},
        "jira_project_containers": {},
    }

    # A DB from before the `key` column existed is opened READ-ONLY here, so no migration can
    # run; the column-less query below serves it exactly as that version did (no provided prefix
    # to register — every project falls back to its synthesized key, see routers.atlassian's
    # `_project_key`).
    def _scan(with_cols: str, without_cols: str, tail: str):
        try:
            return list(conn.execute(with_cols + tail))
        except sqlite3.OperationalError as e:
            # Only a missing column means "a DB from before these columns existed". Any
            # other OperationalError — a locked database, a corrupt page — must surface:
            # swallowed, it boots a server that ignores every corpus-provided id and 404s
            # at all of them, with nothing said. Same narrowing as `store.read_meta`.
            if "no such column" not in str(e).lower():
                raise
            return list(conn.execute(without_cols + tail))

    j_rows = _scan(
        f"SELECT doc_id, {store.grouping_col('jira')} AS container, key ",
        f"SELECT doc_id, {store.grouping_col('jira')} AS container, NULL AS key ",
        f"FROM {store.table('jira')} ORDER BY doc_id",
    )
    # Only the corpus-PROVIDED prefix is registered — a keyless project's synthesized prefix
    # (`synth.jira_project_key`) is derived at serve time instead, never written into this map.
    # One pass, doc_id order with setdefault (first row wins, stable across restarts): unlike the
    # old per-document resolution this replaced, nothing here depends on which row a collision
    # displaces, so there is no second pass to run.
    for r in j_rows:
        if not r["key"]:
            continue
        prefix = str(r["key"]).rsplit("-", 1)[0]
        if prefix:
            idx["jira_project_keys"].setdefault(r["container"], prefix)
            idx["jira_project_containers"].setdefault(prefix.upper(), r["container"])
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
    return idx


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if not settings.db_path.exists():
        raise RuntimeError(
            f"DB not found at {settings.db_path}. Build it first: "
            "backlot import <corpus.jsonl>  (see `backlot import --help` for the other sources)"
        )
    # A BYO import records the corpus-derived org in tokens.yaml; adopt it so the routers
    # (which read get_settings().org_name/org_domain) stay consistent with the ACL. A tokens.yaml
    # written from a stated roster instead of a corpus's own addresses has no org, so the settings
    # defaults stand.
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
    # per-request slack_acl⋈messages join that scales with the docs granted to the caller.
    app.state.channel_acl = None
    # channel -> its member count (its distinct speakers). conversations.info/.list report it
    # for every channel in a page, and a per-channel COUNT(DISTINCT) is far too slow for that.
    app.state.channel_members = None

    app.state.warm_error = None

    def _warm_caches():
        """Fill the caches above, and RECORD a failure rather than dying quietly.

        A daemon thread that raises takes its traceback with it: every cache stays None, which each
        consumer treats as "not warm yet" — the Slack routes fall back to their per-request queries
        and keep answering correctly, so nothing 500s and nothing retries. That is the right
        behaviour for a warm-up, and exactly why the failure has to be reported: without this, a
        broken warm-up is indistinguishable from a slow one forever, and /health goes on saying
        `status: "ok"` with `documents: null` while a load balancer stays green.
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
                    "SELECT DISTINCT d.channel, a.principal_id "
                    f"FROM {store.acl_table('slack')} a JOIN slack_messages d ON d.doc_id = a.doc_id"
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

    # Kept on app.state (rather than fire-and-forget) so a caller — namely tests — can wait for
    # it deterministically instead of polling /health and hoping doc_counts landed in time.
    app.state.warm_thread = threading.Thread(target=_warm_caches, daemon=True)
    app.state.warm_thread.start()
    try:
        yield
    finally:
        conn.close()


app = FastAPI(
    # The server's name, not a corpus's: it serves whatever was imported, so naming one dataset
    # here would put it in /openapi.json, every generated client, and every MCP bridge's tool
    # descriptions.
    title="Backlot Mock Server",
    lifespan=lifespan,
    # NOT FastAPI's default, which derives the id's method suffix from a set and so
    # changes between restarts — see openapi.unique_operation_id.
    generate_unique_id_function=openapi.unique_operation_id,
)


# A vendor whose clients parse error bodies gets its own envelope, and each lives in
# ``backlot/errors/`` with the reasoning for its shape. Both handlers below ask that package and fall
# back to FastAPI's ``{"detail": ...}`` — so neither carries a branch per vendor, and neither has to
# be edited to add one.


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
    # comments, fireflies_sentences): those rows are served too, each with its own
    # endpoint, but they're children of a root doc rather than documents themselves, so they
    # aren't counted here. `source_documents` is what the corpus offered, which is smaller than
    # `documents` because faithful parsing turns one Slack transcript into many message rows.
    # Publishing only the larger of the two reads as inflation, which is why both are reported.
    counts = getattr(app.state, "doc_counts", None)
    warm_error = getattr(app.state, "warm_error", None)
    # `degraded`, not a non-200: the corpus is still served correctly — every consumer of the warm
    # caches falls back to a per-request query — so failing the healthcheck would take down a server
    # that works. What must not happen is `ok` alongside counts that will never arrive.
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
        # Set even when the counts DID land: the warm-up fills three caches in sequence, so a
        # failure part-way leaves some of them populated. `degraded` without the reason is a worse
        # answer than either "ok" or a plain failure.
        body["warm_error"] = warm_error
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
