"""Mock Slack Web API (read-only).

Base URL for a client: ``http://<host>/slack/api/`` (methods live under ``/api/``).
Slack always returns HTTP 200 with an ``{"ok": bool}`` envelope, so auth failures are
signalled as ``{"ok": false, "error": "not_authed"}`` rather than a 401 status.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backlot import auth, store, synth
from backlot.openapi import qp
from backlot.acl import Caller
from backlot.config import get_settings
from backlot.pagination import decode_cursor_or_none, next_cursor

router = APIRouter(prefix="/slack/api", tags=["slack"])


# --- OpenAPI enrichment (issue #4 bridge) --------------------------------------------------
# Params are read query-or-form via _param/_int, so we document them with openapi_extra instead
# of changing the handler signatures (which would break the form-body read path). Response models
# use extra="allow" so the builders' full field set passes through unfiltered.


class _SlackOk(BaseModel):
    model_config = ConfigDict(extra="allow")
    ok: bool


class SlackConversationsList(_SlackOk):
    channels: list[dict] = []


class SlackConversationInfo(_SlackOk):
    channel: dict = {}


class SlackHistory(_SlackOk):
    messages: list[dict] = []
    has_more: bool = False


class SlackMembers(_SlackOk):
    members: list[str] = []


class SlackUsersList(_SlackOk):
    members: list[dict] = []


class SlackUserInfo(_SlackOk):
    user: dict = {}


class SlackApiTest(_SlackOk):
    args: dict = {}


class SlackSearch(_SlackOk):
    messages: dict = {}


_P_LIST = [qp("limit", "integer"), qp("cursor")]
_P_CHANNEL = [qp("channel", required=True)]
_P_HISTORY = [
    qp("channel", required=True),
    qp("limit", "integer"),
    qp("cursor"),
    qp("oldest"),
    qp("latest"),
    qp("inclusive", "boolean"),
]
_P_REPLIES = [qp("channel", required=True), qp("ts", required=True)]
_P_USER = [qp("user", required=True)]
_P_SEARCH = [
    qp("query", required=True),
    qp("count", "integer"),
    qp("page", "integer"),
    qp("sort"),
    qp("sort_dir"),
]
_P_SEARCH_FILES = [qp("query", required=True), qp("count", "integer")]

# conversations.history page cap (thread roots). Slack recommends limit<=200; capping here bounds
# how many authors a client resolves per call so history stays fast even with a small users.list.
_HISTORY_MAX_ROOTS = 200


def _err(error: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error})


# Slack's conversation types. This corpus models channels only, so `im`/`mpim` select nothing —
# which is exactly what real Slack answers for a workspace with no DMs, so a client cannot tell the
# mock's "no DMs" from production's. An unknown value is `invalid_types`, as documented; accepting
# it silently returned the unfiltered list to a caller who had typo'd their filter.
_SLACK_TYPES = {"public_channel", "private_channel", "im", "mpim"}
_CHANNEL_TYPES = {"public_channel", "private_channel"}


def _slack_types(request: Request):
    """The requested conversation types, or ``None`` if the value is not a Slack type. Omitted
    defaults to ``public_channel``, which is Slack's documented default."""
    raw = _param(request, "types")
    if raw is None or not raw.strip():
        return {"public_channel"}
    want = {t.strip() for t in raw.split(",") if t.strip()}
    return want if want <= _SLACK_TYPES else None


def _slack_ts(value: str | None) -> float | None | bool:
    """A Slack timestamp argument as a float; ``False`` marks one that does not parse. Unguarded
    ``float()`` made a bad argument a 500, and a 5xx is retried by clients that back off on it —
    so a request that can never succeed burned the whole retry budget."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return False


def _caller(request: Request) -> Caller | None:
    return auth.acl(request).resolve(auth.slack_token(request))


def _full_channel(request: Request, conn, name: str) -> dict:
    """A full conversation object (shared by conversations.list and .info)."""
    is_private = not store.container_has_public(conn, "slack", name)
    num = _member_count(request, conn, name)
    created = _channel_created(request, conn, name)
    return {
        "id": synth.slack_channel_id(name),
        "name": name,
        "name_normalized": name,
        "is_channel": True,
        "is_group": False,
        "is_im": False,
        "is_mpim": False,
        "is_private": is_private,
        "is_member": True,
        "is_archived": False,
        "is_general": name in ("general", "announcements"),
        "is_shared": False,
        "is_ext_shared": False,
        "is_org_shared": False,
        "unlinked": 0,
        "created": created,
        "updated": created * 1000,
        "creator": "USERVICE0",
        "topic": {"value": f"#{name}", "creator": "USERVICE0", "last_set": created},
        "purpose": {"value": f"Channel for {name}", "creator": "USERVICE0", "last_set": created},
        "previous_names": [],
        "num_members": num,
    }


def _channel_names(conn) -> list[str]:
    return [row["name"] for row in store.list_containers(conn, "slack")]


def _user_obj(conn, email: str) -> dict:
    u = store.get_user(conn, email)
    display = u["display_name"] if u else email.split("@")[0]
    parts = display.split()
    updated = synth.epoch("user:" + email)
    is_bot = not u and email.split("@")[0].endswith("bot")  # display-only "*bot" speakers
    return {
        "id": synth.slack_user_id(email),
        "team_id": "T0000MOCK",
        "name": email.split("@")[0].replace(".", ""),
        "real_name": display,
        "deleted": False,
        "is_bot": is_bot,
        "is_app_user": is_bot,
        "is_admin": False,
        "is_owner": False,
        "is_primary_owner": False,
        "is_restricted": False,
        "is_ultra_restricted": False,
        "has_2fa": False,
        "tz": "America/Los_Angeles",
        "tz_label": "Pacific Time",
        "tz_offset": -28800,
        "color": synth._digest(email)[:6],
        "updated": updated,
        "profile": {
            "real_name": display,
            "display_name": display,
            "real_name_normalized": display,
            "display_name_normalized": display,
            "first_name": parts[0] if parts else display,
            "last_name": parts[-1] if len(parts) > 1 else "",
            "email": email,
            "title": "",
            "phone": "",
            "skype": "",
            "status_text": "",
            "status_emoji": "",
            "avatar_hash": synth._digest(email)[:12],
        },
    }


@router.api_route("/api.test", methods=["GET", "POST"], response_model=SlackApiTest)
async def api_test(request: Request):
    """Real Slack's connectivity check — no auth required, echoes back any params other than
    `error` (which flips the response to an error envelope carrying that value). Several real
    clients call this at construction/connect time (e.g. llama-index's `SlackReader.__init__`),
    so the mock must answer it rather than 404 — a 404 there breaks reader construction entirely,
    before the caller ever gets a chance to point it at the mock."""
    error = _param(request, "error")
    if error:
        return _err(error)
    args = dict(request.query_params)
    form = getattr(request.state, "_form", None)
    if form:
        args.update(form)
    return {"ok": True, "args": args}


@router.api_route("/auth.test", methods=["GET", "POST"])
async def auth_test(request: Request):
    caller = _caller(request)
    if caller is None:
        return _err("not_authed")
    who = "service-account" if caller.is_admin else caller.email
    return {
        "ok": True,
        "url": f"https://{get_settings().org_name}.slack.com/",
        "team": get_settings().org_name,
        "user": who,
        "user_id": "USERVICE0",
        "team_id": "T0000MOCK",
    }


@router.api_route(
    "/conversations.list",
    methods=["GET", "POST"],
    response_model=SlackConversationsList,
    openapi_extra={"parameters": _P_LIST},
)
async def conversations_list(request: Request):
    conn = auth.conn(request)
    caller = _caller(request)
    if caller is None:
        return _err("not_authed")
    types = _slack_types(request)
    if types is None:
        return _err("invalid_types")
    offset = decode_cursor_or_none(_param(request, "cursor"))
    if offset is None:
        return _err("invalid_cursor")
    ids = auth.visible_ids(request, caller)

    # No conversation in this corpus is a DM, so a DM-only request selects nothing.
    names = _channel_names(conn) if types & _CHANNEL_TYPES else []
    if ids is not None:  # non-admin: only channels the caller can see a message in
        cache = getattr(request.app.state, "channel_acl", None)
        if cache is not None:  # O(channels): intersect each channel's grantees with the caller's
            idset = set(ids)
            names = [n for n in names if cache.get(n, frozenset()) & idset]
        else:  # cache not warm yet — principal-indexed query (public channels + granted ones)
            org = auth.acl(request).org_name
            granted = store.slack_channels_for_principals(conn, [p for p in ids if p != org])
            names = [
                n for n in names if store.container_has_public(conn, "slack", n) or n in granted
            ]

    limit = _int(request, "limit", get_settings().default_page_size)
    page = [_full_channel(request, conn, n) for n in names[offset : offset + limit]]
    cursor = next_cursor(offset, len(page), len(names))
    return {"ok": True, "channels": page, "response_metadata": {"next_cursor": cursor}}


@router.api_route(
    "/conversations.info",
    methods=["GET", "POST"],
    response_model=SlackConversationInfo,
    openapi_extra={"parameters": _P_CHANNEL},
)
async def conversations_info(request: Request):
    conn = auth.conn(request)
    caller = _caller(request)
    if caller is None:
        return _err("not_authed")
    name = _channel_name(conn, _param(request, "channel") or "")
    if name is None:
        return _err("channel_not_found")
    return {"ok": True, "channel": _full_channel(request, conn, name)}


@router.api_route(
    "/conversations.history",
    methods=["GET", "POST"],
    response_model=SlackHistory,
    openapi_extra={"parameters": _P_HISTORY},
)
async def conversations_history(request: Request):
    conn = auth.conn(request)
    caller = _caller(request)
    if caller is None:
        return _err("not_authed")
    channel_id = _param(request, "channel")
    if not channel_id:
        return _err("channel_not_found")
    name = _channel_name(conn, channel_id)
    if name is None:
        return _err("channel_not_found")
    ids = auth.visible_ids(request, caller)

    # Cap the page at a Slack-realistic size: the real API recommends limit<=200 and often returns
    # fewer than requested. A client that asks for 1000 (korotovsky) would make the client resolve
    # ~1000 roots x their authors/reply_users via users.info — slow. has_more/next_cursor still let
    # it paginate for more.
    limit = min(_int(request, "limit", get_settings().default_page_size), _HISTORY_MAX_ROOTS)
    offset = decode_cursor_or_none(_param(request, "cursor"))
    if offset is None:
        return _err("invalid_cursor")
    # Slack history returns only top-level messages (thread roots + standalone);
    # replies live under conversations.replies.
    lo, hi = _slack_ts(_param(request, "oldest")), _slack_ts(_param(request, "latest"))
    if lo is False:
        return _err("invalid_ts_oldest")
    if hi is False:
        return _err("invalid_ts_latest")
    if lo is not None or hi is not None:  # time-bounded (e.g. a single day): filter, then paginate
        inclusive = _param(request, "inclusive") in ("1", "true", "True")

        def _in_window(r) -> bool:
            ts = float(_msg_ts(r))
            # Slack: latest is inclusive; oldest is exclusive unless inclusive=true
            if lo is not None and (ts < lo if inclusive else ts <= lo):
                return False
            if hi is not None and ts > hi:
                return False
            return True

        # SQL-narrow by created_ts (±1s to cover the sub-second fraction in a public ts), then apply
        # the exact float window in Python — so a day window scans the day, not the whole channel.
        ts_lo = int(lo) - 1 if lo is not None else None
        ts_hi = int(hi) + 1 if hi is not None else None
        matched = [
            r
            for r in store.list_slack_top_level(
                conn, name, ids, limit=10**9, ts_lo=ts_lo, ts_hi=ts_hi
            )
            if _in_window(r)
        ]
        total = len(matched)
        rows = matched[offset : offset + limit]
    else:
        total = store.count_slack_top_level(conn, name, ids)
        rows = store.list_slack_top_level(conn, name, ids, limit=limit, offset=offset)
    messages = []
    for r in rows:
        rc = store.slack_reply_count(conn, r["doc_id"], ids) if r["thread_id"] else 0
        latest = _latest_reply(r, rc) if rc else None
        ru = store.slack_reply_authors(conn, r["doc_id"], ids) if rc else []
        ruids = [synth.slack_user_id(e) for e in ru[:5]]
        messages.append(
            _message(
                r, reply_count=rc, latest_reply=latest, reply_users=ruids, reply_users_count=len(ru)
            )
        )
    cursor = next_cursor(offset, len(rows), total)
    return {
        "ok": True,
        "messages": messages,
        "has_more": bool(cursor),
        "pin_count": 0,
        "response_metadata": {"next_cursor": cursor},
    }


@router.api_route(
    "/conversations.replies",
    methods=["GET", "POST"],
    response_model=SlackHistory,
    openapi_extra={"parameters": _P_REPLIES},
)
async def conversations_replies(request: Request):
    conn = auth.conn(request)
    caller = _caller(request)
    if caller is None:
        return _err("not_authed")
    ts = _param(request, "ts")
    name = _channel_name(conn, _param(request, "channel") or "")
    if name is None or not ts:
        return _err("thread_not_found")
    ids = auth.visible_ids(request, caller)
    # Resolve the ts against ALL messages in the channel, not just roots: Slack accepts any in-thread
    # ts here, and a client that got its ts from a search hit will often pass a REPLY's ts. Find the
    # matched message, then return the thread it belongs to (its root's).
    # Fast path: a public ts's integer part IS the row's created_ts, so narrow to that second instead
    # of loading the whole channel (eng-ml is ~340k rows → ~4s). Fall back to the full scan only for
    # a ts synthesized from a doc id (created_ts NULL), which the second query can't target.
    hit = None
    try:
        epoch = int(str(ts).split(".", 1)[0])
    except (TypeError, ValueError):
        epoch = None
    if epoch is not None:
        candidates = store.slack_messages_at_created_ts(conn, name, epoch, ids)
        hit = next((r for r in candidates if _msg_ts(r) == ts), None)
    if hit is None:
        msgs = store.list_slack_channel_messages(conn, name, ids)
        hit = next((r for r in msgs if _msg_ts(r) == ts), None)
    if hit is None:
        return _err("thread_not_found")
    if not hit["thread_id"]:  # standalone message (no thread)
        return {"ok": True, "messages": [_message(hit)], "has_more": False}
    # The root's own created_ts differs from a reply's ts, so don't re-scan the channel for it —
    # derive its doc_id from the hit (a root's doc_id == its thread_id) and pull the root out of the
    # ordered thread rows below.
    root_doc_id = hit["doc_id"] if hit["thread_seq"] == 0 else hit["thread_id"]
    rows = store.slack_thread(conn, root_doc_id, ids)  # root + replies, ordered
    root = next((r for r in rows if r["thread_seq"] == 0), None)
    if root is None:
        return _err("thread_not_found")
    rc = sum(1 for x in rows if x["thread_seq"] > 0)
    latest = _latest_reply(root, rc) if rc else None
    ru = store.slack_reply_authors(conn, root["doc_id"], ids)
    ruids = [synth.slack_user_id(e) for e in ru[:5]]
    parent_uid = synth.slack_user_id(root["author_email"])
    messages = [
        _message(
            x,
            reply_count=(rc if x["thread_seq"] == 0 else 0),
            latest_reply=(latest if x["thread_seq"] == 0 else None),
            reply_users=(ruids if x["thread_seq"] == 0 else None),
            reply_users_count=(len(ru) if x["thread_seq"] == 0 else 0),
            parent_user_id=(parent_uid if x["thread_seq"] > 0 else None),
        )
        for x in rows
    ]
    return {"ok": True, "messages": messages, "has_more": False}


@router.api_route(
    "/conversations.members",
    methods=["GET", "POST"],
    response_model=SlackMembers,
    openapi_extra={"parameters": _P_CHANNEL},
)
async def conversations_members(request: Request):
    conn = auth.conn(request)
    caller = _caller(request)
    if caller is None:
        return _err("not_authed")
    name = _channel_name(conn, _param(request, "channel") or "")
    if name is None:
        return _err("channel_not_found")
    offset = decode_cursor_or_none(_param(request, "cursor"))
    if offset is None:
        return _err("invalid_cursor")
    limit = _int(request, "limit", get_settings().default_page_size)
    # A channel's members are the people who have spoken in it — the only per-channel signal the
    # corpus carries. Never the whole roster: real Slack's membership differs per channel, and it
    # paginates this method.
    total = store.count_slack_channel_members(conn, name)
    emails = store.slack_channel_member_emails(conn, name, limit=limit, offset=offset)
    members = [synth.slack_user_id(e) for e in emails]
    cursor = next_cursor(offset, len(members), total)
    return {"ok": True, "members": members, "response_metadata": {"next_cursor": cursor}}


@router.api_route(
    "/users.list",
    methods=["GET", "POST"],
    response_model=SlackUsersList,
    openapi_extra={"parameters": _P_LIST},
)
async def users_list(request: Request):
    conn = auth.conn(request)
    caller = _caller(request)
    if caller is None:
        return _err("not_authed")
    # The roster is the registered user principals — the employee directory plus internal mail/doc
    # authors. Slack transcript speakers are NOT added, and that is a limitation of the upstream
    # dataset rather than a modelling choice, so it is stated plainly here and in the README (#33).
    #
    # The speakers are NOT "mostly external": measured on the bench corpus, of 74,138 distinct
    # speakers only 3,971 (5.4%) are principals and ALL 70,167 of the rest are on the org's own
    # domain. The two populations are generated independently upstream, and 74k speakers against an
    # 11,913-person directory is not a headcount any real workspace has — so neither set can be
    # made a subset of the other without inventing 70k colleagues or discarding the speakers.
    #
    # What it costs a client: `message.user` for a speaker outside the directory resolves through
    # users.info but never appears in users.list. conversations.members does now page the channel's
    # own speakers, so such an author is at least discoverable per channel.
    offset = decode_cursor_or_none(_param(request, "cursor"))
    if offset is None:
        return _err("invalid_cursor")
    emails = store.all_user_emails(conn)
    limit = _int(request, "limit", get_settings().default_page_size)
    page = emails[offset : offset + limit]
    members = [_user_obj(conn, e) for e in page]
    cursor = next_cursor(offset, len(page), len(emails))
    return {"ok": True, "members": members, "response_metadata": {"next_cursor": cursor}}


@router.api_route(
    "/users.info",
    methods=["GET", "POST"],
    response_model=SlackUserInfo,
    openapi_extra={"parameters": _P_USER},
)
async def users_info(request: Request):
    conn = auth.conn(request)
    caller = _caller(request)
    if caller is None:
        return _err("not_authed")
    uid = _param(request, "user")
    for e in store.all_user_emails(conn):
        if synth.slack_user_id(e) == uid:
            return {"ok": True, "user": _user_obj(conn, e)}
    # Display-only Slack speakers/bots (deploybot@…, payments-bot slugged to paymentsbot@…) aren't
    # principals; resolve them from the message authors so their IDs don't come back user_not_found.
    email = _slack_author_by_uid(request, conn, uid)
    if email:
        return {"ok": True, "user": _user_obj(conn, email)}
    return _err("user_not_found")


def _slack_author_by_uid(request: Request, conn, uid: str) -> str | None:
    """Reverse a synthesized Slack user id to a message-author email (synth is one-way, so build a
    map from the distinct authors and cache it on app.state — the DISTINCT scan runs once)."""
    cache = getattr(request.app.state, "_slack_uid_map", None)
    if cache is None:
        cache = {synth.slack_user_id(e): e for e in store.distinct_slack_author_emails(conn)}
        request.app.state._slack_uid_map = cache
    return cache.get(uid)


_SLACK_IN_RE = re.compile(r'\bin:(#|@)?([^\s"]+)')


def _parse_slack_query(raw: str) -> tuple[str, str | None, bool]:
    """Parse a Slack search query into (search_terms, channel_container, phrase), honouring the two
    operators real Slack search supports — without this they would be matched as literal text:

    - ``in:#channel`` (or ``in:channel``) scopes results to that channel — a container filter, not
      three stray search tokens (``in``, the ``#`` name...). ``in:@user`` (a DM) has no container in
      the mock's channel-based corpus, so it's stripped without scoping rather than mis-scoped.
    - a fully ``"quoted"`` query matches its tokens ADJACENTLY (an FTS phrase) instead of ANDed
      anywhere — Slack's quote semantics, and what a grep push-down needs so a literal pattern isn't
      buried under docs that merely contain the words scattered.

    Unrecognized operators stay in the term string (searched as text) to avoid silently dropping
    intent."""
    container = None

    def _grab(m: re.Match) -> str:
        nonlocal container
        sigil, val = m.group(1), m.group(2)
        if container is None and sigil != "@" and val:
            container = val.lstrip("#")
        return " "

    text = _SLACK_IN_RE.sub(_grab, raw).strip()
    phrase = len(text) >= 2 and text[0] == '"' and text[-1] == '"'
    text = text[1:-1] if phrase else text.replace('"', " ")
    return text.strip(), container, phrase


def _messages_block(request: Request):
    """Shared message-search core for search.messages and search.all. Returns (query, block) or
    (error_dict, None)."""
    conn = auth.conn(request)
    caller = _caller(request)
    if caller is None:
        return _err("not_authed"), None
    query = _param(request, "query") or ""
    if not query.strip():
        return _err("missing_query"), None
    terms, container, phrase = _parse_slack_query(query)
    ids = auth.visible_ids(request, caller)  # results are scoped to the caller's ACL
    count = _int(request, "count", 20)
    page = max(1, _int(request, "page", 1))
    offset = (page - 1) * count
    # Honor Slack's sort: "score" (default) = relevance; "timestamp" = by message time. sort_dir
    # defaults to desc (newest first). Previously the mock always ranked by relevance regardless.
    sort = (_param(request, "sort") or "score").lower()
    sort_dir = (_param(request, "sort_dir") or "desc").lower()
    order_by = None
    if sort == "timestamp":
        order_by = "recency_asc" if sort_dir == "asc" else "recency"
    rows = store.search_documents(
        conn,
        terms,
        "slack",
        ids,
        limit=count,
        offset=offset,
        container=container,
        phrase=phrase,
        order_by=order_by,
    )
    total = store.count_search(conn, terms, "slack", ids, container=container, phrase=phrase)
    matches = [_search_match(conn, r) for r in rows]
    pages = (total + count - 1) // count if count else 1
    block = {
        "total": total,
        "pagination": {
            "total_count": total,
            "page": page,
            "per_page": count,
            "page_count": pages,
            "first": offset + 1,
            "last": offset + len(matches),
        },
        "paging": {"count": count, "total": total, "page": page, "pages": pages},
        "matches": matches,
    }
    return query, block


@router.api_route(
    "/search.messages",
    methods=["GET", "POST"],
    response_model=SlackSearch,
    openapi_extra={"parameters": _P_SEARCH},
)
async def search_messages(request: Request):
    query, block = _messages_block(request)
    if block is None:
        return query  # error dict
    return {"ok": True, "query": query, "messages": block}


@router.api_route(
    "/search.files",
    methods=["GET", "POST"],
    response_model=SlackSearch,
    openapi_extra={"parameters": _P_SEARCH_FILES},
)
async def search_files(request: Request):
    """Slack file search. The mock has no uploaded-file corpus (files exist only as message
    attachments), so matches are always empty — but the endpoint must exist and return ok=True:
    real Slack has it, and mirage's grep push-down calls search.files for any file-inclusive scope;
    a 404 there reads as an error and forces a slow full-tree per-file fallback."""
    caller = _caller(request)
    if caller is None:
        return _err("not_authed")
    query = _param(request, "query") or ""
    if not query.strip():
        return _err("missing_query")
    count = _int(request, "count", 20)
    empty = {
        "total": 0,
        "matches": [],
        "pagination": {
            "total_count": 0,
            "page": 1,
            "per_page": count,
            "page_count": 0,
            "first": 0,
            "last": 0,
        },
        "paging": {"count": count, "total": 0, "page": 1, "pages": 0},
    }
    return {"ok": True, "query": query, "files": empty}


@router.api_route(
    "/search.all",
    methods=["GET", "POST"],
    response_model=SlackSearch,
    openapi_extra={"parameters": _P_SEARCH},
)
async def search_all(request: Request):
    """Slack's combined search (the slack-go SDK's Search()/SearchContext() hits this). The mock
    has no file corpus, so ``files`` is always empty; ``messages`` matches search.messages."""
    query, block = _messages_block(request)
    if block is None:
        return query  # error dict
    empty = {
        "total": 0,
        "matches": [],
        "pagination": {
            "total_count": 0,
            "page": 1,
            "per_page": block["paging"]["count"],
            "page_count": 0,
            "first": 0,
            "last": 0,
        },
        "paging": {"count": block["paging"]["count"], "total": 0, "page": 1, "pages": 0},
    }
    return {"ok": True, "query": query, "messages": block, "files": empty}


# --- helpers --------------------------------------------------------------------


def _search_match(conn, row) -> dict:
    """A search.messages `matches[]` entry for a slack row."""
    ch = row["channel"]
    cid = synth.slack_channel_id(ch)
    text = f"*{row['title']}*\n{row['content']}" if row["title"] else row["content"]
    ts = _msg_ts(row)
    m = {
        "type": "message",
        "team": "T0000MOCK",
        "channel": {
            "id": cid,
            "name": ch,
            "is_private": not store.container_has_public(conn, "slack", ch),
        },
        "user": synth.slack_user_id(row["author_email"]),
        "username": row["author_email"].split("@")[0],
        "ts": ts,
        "text": text,
        "permalink": f"https://{get_settings().org_name}.slack.com/archives/{cid}/p{ts.replace('.', '')}",
    }
    if row[
        "thread_id"
    ]:  # a hit inside a thread carries its root ts so the client can fetch replies
        m["thread_ts"] = synth.slack_fmt_ts(_root_epoch(row), row["thread_id"])
    return m


def _root_epoch(row) -> int:
    """The thread root's base second — the caller-supplied `created` (with the reply's
    position backed out) if present, else synthesized from the root doc_id."""
    if row["created_ts"]:
        return row["created_ts"] - row["thread_seq"]
    return synth.epoch(row["thread_id"] or row["doc_id"])


def _compute_channel_created(conn, name: str) -> int:
    """Channel creation second, pinned at/or before its earliest message so it never postdates
    one. The synthesized per-channel ``epoch(name)`` and per-message ``epoch(doc_id)`` are
    independent draws, so a message could otherwise predate its channel — which breaks clients
    (e.g. mirage) that list a day per date between creation and the latest message.

    Derived from a cheap aggregate rather than scanning every message: messages with an explicit
    ``created_ts`` contribute ``MIN(created_ts)``; messages without one synthesize their ts as
    ``epoch(doc_id)``, which is always ``>= BASE_EPOCH`` — so ``BASE_EPOCH`` is a safe lower
    bound for that group. ``created`` is the min of whichever groups are present."""
    b = store.slack_created_bounds(conn, name)
    if not b["total"]:
        return synth.epoch(name)
    candidates = []
    if b["have"]:  # rows with an explicit created_ts
        candidates.append(b["min_ts"])
    if b["have"] < b["total"]:  # rows whose ts is synthesized as epoch(doc_id)
        candidates.append(synth.BASE_EPOCH)
    return min(candidates)


def _channel_created(request: Request, conn, name: str) -> int:
    """Memoized ``_compute_channel_created`` — the corpus is read-only, and the aggregate scans
    a channel's messages, so cache it per app (conversations.list asks for every channel)."""
    cache = getattr(request.app.state, "channel_created", None)
    if cache is None:
        cache = request.app.state.channel_created = {}
    if name not in cache:
        cache[name] = _compute_channel_created(conn, name)
    return cache[name]


def _member_count(request: Request, conn, name: str) -> int:
    """A channel's member count — the same set conversations.members pages, so `num_members` and
    walking the members cannot disagree. Read from the warm cache; if the background thread has not
    finished, count this one channel directly rather than blocking on all of them."""
    cache = getattr(request.app.state, "channel_members", None)
    if cache is not None:
        return cache.get(name, 0)
    return store.count_slack_channel_members(conn, name)


def _msg_ts(row) -> str:
    if row["created_ts"]:
        return synth.slack_fmt_ts(row["created_ts"], row["thread_id"] or row["doc_id"])
    if row["thread_id"]:
        return synth.slack_thread_ts(row["thread_id"], row["thread_seq"])
    return synth.slack_ts(row["doc_id"])


def _latest_reply(root_row, reply_count: int) -> str:
    """ts of the last reply in a thread (root base + reply_count)."""
    return synth.slack_fmt_ts(_root_epoch(root_row) + reply_count, root_row["doc_id"])


def _message(
    row,
    reply_count: int = 0,
    latest_reply: str | None = None,
    reply_users: list[str] | None = None,
    reply_users_count: int = 0,
    parent_user_id: str | None = None,
) -> dict:
    # Slack messages have no title; only prepend one as a lead line when present
    # (bench docs carry a title, BYO slack records typically don't).
    title = row["title"]
    text = f"*{title}*\n{row['content']}" if title else row["content"]
    m = {
        "type": "message",
        "user": synth.slack_user_id(row["author_email"]),
        "text": text,
        "ts": _msg_ts(row),
        "team": "T0000MOCK",
        "client_msg_id": synth.gmail_id(row["doc_id"], salt="cmid"),
    }
    reactions = store.jcol(row, "reactions")
    if reactions:
        m["reactions"] = reactions
    files = store.jcol(row, "files")
    if files:
        m["files"] = files
    edited = store.jcol(row, "edited", {})
    if edited:
        m["edited"] = edited
    if row["subtype"]:
        m["subtype"] = row["subtype"]
    if row["thread_id"]:  # part of a thread
        m["thread_ts"] = synth.slack_fmt_ts(_root_epoch(row), row["thread_id"])
        if row["thread_seq"] == 0 and reply_count > 0:  # thread root
            m.update(
                {
                    "reply_count": reply_count,
                    "reply_users_count": reply_users_count or len(reply_users or []),
                    "reply_users": reply_users or [],
                    "latest_reply": latest_reply,
                    "subscribed": False,
                }
            )
        elif row["thread_seq"] > 0 and parent_user_id:  # a reply
            m["parent_user_id"] = parent_user_id
    return m


def _channel_name(conn, channel_id: str) -> str | None:
    for row in store.list_containers(conn, "slack"):
        if synth.slack_channel_id(row["name"]) == channel_id:
            return row["name"]
    return None


def _param(request: Request, key: str) -> str | None:
    v = request.query_params.get(key)
    if v is not None:
        return v
    form = getattr(request.state, "_form", None)
    return form.get(key) if form else None


def _int(request: Request, key: str, default: int) -> int:
    v = _param(request, key)
    try:
        return min(int(v), get_settings().max_page_size) if v else default
    except ValueError:
        return default
