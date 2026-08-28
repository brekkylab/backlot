"""Slack Web API (read-only).

Base URL for a client: ``http://<host>/slack/api/`` (methods live under ``/api/``).
Slack always returns HTTP 200 with an ``{"ok": bool}`` envelope, so auth failures are
signalled in the body rather than as a 401 status. There are TWO of them and they are not
interchangeable: a MISSING credential is ``not_authed``, a credential that is present but does not
resolve is ``invalid_auth``. Connectors branch on exactly that split — re-authenticate versus give
up — so ``_caller_or_error`` decides it once for every method here.
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


# --- OpenAPI enrichment --------------------------------------------------
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


_P_PAGED = [qp("limit", "integer"), qp("cursor")]
# `types` decides what comes back, and its default excludes every private channel — so the spec
# says so rather than leaving a generated client to find out. `users.list` takes no such argument,
# which is why the two lists are not one.
_P_CONVERSATIONS_LIST = _P_PAGED + [
    qp(
        "types",
        description=(
            "Comma-separated conversation types to return: public_channel, private_channel, im, "
            "mpim. Defaults to public_channel alone, so private channels are only returned when "
            "asked for by name. An unrecognised value is rejected with `invalid_types`. This "
            "corpus models channels only, so `im`/`mpim` select nothing."
        ),
    )
]
_P_CHANNEL = [qp("channel", required=True)]
_P_INFO = [qp("channel", required=True), qp("include_num_members", "boolean")]

# The one workspace Backlot emulates. Every conversation is in it, shared with nobody, so the
# sharing ids below are all this team or empty.
TEAM_ID = "T0000BKLT"
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
# which is exactly what real Slack answers for a workspace with no DMs, so a client cannot tell
# Backlot's "no DMs" from production's. An unknown value is `invalid_types`, as documented;
# accepting it silently returned the list to a caller who had typo'd their filter.
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


def _caller_or_error(request: Request) -> tuple[Caller | None, dict | None]:
    """Resolve the caller, or say which of Slack's two auth errors applies.

    Measured against the live API: a token that is merely unknown answers the same as a malformed
    one, so "malformed" is not a category this has to recognise — what matters is only whether a
    credential was presented at all, and Slack recognises exactly three ways to present one:
    an `Authorization: Bearer <t>` header, a `token` query param, or a `token` form field.

        no header, no `token` param      -> not_authed
        `Authorization: Bearer ` (empty) -> not_authed
        `token=` (empty)                 -> not_authed
        any other header scheme          -> not_authed
        any non-empty token that fails   -> invalid_auth

    "any other header scheme" is the whole of the rest, not just the obvious `Basic`: the scheme is
    case-SENSITIVE live, so `bearer` and `BEARER` land here too, as does GitHub's legacy `token <t>`
    form. `auth.slack_bearer_token` is what draws that line, and is deliberately not the shared
    `auth.bearer_token` — see its docstring for why the two cannot be one parser.

    ``auth.slack_token`` returns None for every case in the first group: an unrecognised scheme
    parses to nothing, and an empty query or form value is falsy.
    """
    token = auth.slack_token(request)
    caller = auth.acl(request).resolve(token)
    if caller is not None:
        return caller, None
    return None, _err("invalid_auth" if token else "not_authed")


def _missing_argument(request: Request, *names: str) -> JSONResponse | None:
    """``invalid_arguments`` if one of these required arguments was not sent at all.

    Slack answers this before it resolves anything, and a client branches on the two differently:
    `channel_not_found` is about the workspace — retry with another id, or treat the channel as
    gone — while `invalid_arguments` is about the request the client just built. Collapsing them
    told a client its own malformed call had named a channel that does not exist.

    An argument that is PRESENT and empty is not this case: `channel=` resolves to nothing, which
    is `channel_not_found`, and `ts=` on a readable channel is `thread_not_found`. Both measured,
    along with the order — a bad token is `invalid_auth` whether or not the arguments are there,
    so this sits after `_caller_or_error` rather than before it.
    """
    if any(_param(request, n) is None for n in names):
        return _err("invalid_arguments")
    return None


def _channel_core(request: Request, conn, name: str, caller: Caller) -> dict:
    """The conversation object as BOTH conversations.list and .info answer it.

    What each adds is deliberately not here, because the real API does not agree between the two:
    `num_members` is a `.list` field that `.info` returns only for `include_num_members=true`, and
    `last_read` is `.info`-only (and the caller's). Building one object for both made Backlot the
    only place a client could rely on them matching.
    """
    is_private = _is_private(request, conn, name)
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
        "is_member": _is_member(conn, name, caller, is_private=is_private),
        "is_archived": False,
        "is_general": name in ("general", "announcements"),
        "is_shared": False,
        "is_ext_shared": False,
        "is_org_shared": False,
        # No BYO corpus expresses a pending external share, so these are constants — but the
        # shared-channel family is five fields in every response Slack documents, and a client
        # validating the object against a generated model sees a shape real Slack never returns.
        "is_pending_ext_shared": False,
        "pending_shared": [],
        # Single-workspace, nothing shared or nested: the ids are this team and the rest empty.
        # Absent here, a client generated from the vendor schema has no field to bind them to.
        "context_team_id": TEAM_ID,
        "shared_team_ids": [TEAM_ID],
        "pending_connected_team_ids": [],
        "parent_conversation": None,
        "unlinked": 0,
        "created": created,
        "updated": created * 1000,
        "creator": "USERVICE0",
        "topic": {"value": f"#{name}", "creator": "USERVICE0", "last_set": created},
        "purpose": {"value": f"Channel for {name}", "creator": "USERVICE0", "last_set": created},
        "previous_names": [],
        # Contextual channel configuration — tabs, a channel canvas, posting restrictions. The key
        # is always present in a real response, but no corpus states any of it, so it is served
        # empty rather than furnished with settings this workspace does not have.
        "properties": {},
    }


def _listed_channel(request: Request, conn, name: str, caller: Caller) -> dict:
    """conversations.list's channel: the core plus its member count."""
    return {
        **_channel_core(request, conn, name, caller),
        "num_members": _member_count(request, conn, name),
    }


# Slack's zero timestamp, its "nothing here yet" value for a ts-shaped field. Reasoned from the
# convention rather than measured: reading the live `last_read` of a channel nobody has opened needs
# a real token, which this environment has none of.
ZERO_TS = "0000000000.000000"


def _last_read(conn, name: str, caller: Caller, visible_ids) -> str:
    """The caller's `last_read` for a channel — always a ts, never absent.

    A service token gets the zero ts rather than the channel's newest message: it is not a person
    and has read nothing, the same reasoning `_subscribed` applies to a thread it cannot have
    followed. Otherwise Backlot models no unread state, so a channel the caller can read reads as
    caught up."""
    if not (caller.email or ""):
        return ZERO_TS
    return store.slack_latest_ts(conn, name, visible_ids) or ZERO_TS


def _info_channel(
    request: Request, conn, name: str, *, include_num_members: bool, visible_ids, caller: Caller
) -> dict:
    """conversations.info's channel. `num_members` is opt-in here (the vendor's own parameter), and
    `last_read` is the caller's.

    Being the caller's, its VALUE varies by who asks — that is what the field means. Its PRESENCE
    must not: a key that appears only when the caller can read the channel gives `.info` two shapes,
    and on a private channel `conversations.list` does not show this caller, the missing key is a
    straight answer to "can you read this?". A caller with nothing to have read gets `ZERO_TS`."""
    ch = _channel_core(request, conn, name, caller)
    if include_num_members:
        ch["num_members"] = _member_count(request, conn, name)
    ch["last_read"] = _last_read(conn, name, caller, visible_ids)
    return ch


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
        "team_id": TEAM_ID,
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
    so Backlot must answer it rather than 404 — a 404 there breaks reader construction entirely,
    before the caller ever gets a chance to point it at Backlot."""
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
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    who = "service-account" if caller.is_admin else caller.email
    return {
        "ok": True,
        "url": f"https://{get_settings().org_name}.slack.com/",
        "team": get_settings().org_name,
        "user": who,
        "user_id": "USERVICE0",
        "team_id": TEAM_ID,
    }


@router.api_route(
    "/conversations.list",
    methods=["GET", "POST"],
    response_model=SlackConversationsList,
    openapi_extra={"parameters": _P_CONVERSATIONS_LIST},
)
async def conversations_list(request: Request):
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    types = _slack_types(request)
    if types is None:
        return _err("invalid_types")
    offset = decode_cursor_or_none(_param(request, "cursor"))
    if offset is None:
        return _err("invalid_cursor")
    ids = auth.visible_ids(request, caller)

    # No conversation in this corpus is a DM, so a DM-only request selects nothing.
    names = _channel_names(conn) if types & _CHANNEL_TYPES else []
    visible = _channel_visibility(request, conn, ids)  # only channels the caller can see
    names = [n for n in names if visible(n)]
    if _CHANNEL_TYPES - types:  # one of the two channel types was asked for, not both
        want_private = "private_channel" in types
        names = [n for n in names if _is_private(request, conn, n) is want_private]

    limit = _int(request, "limit", get_settings().default_page_size)
    page = [_listed_channel(request, conn, n, caller) for n in names[offset : offset + limit]]
    cursor = next_cursor(offset, len(page), len(names))
    return {"ok": True, "channels": page, "response_metadata": {"next_cursor": cursor}}


@router.api_route(
    "/conversations.info",
    methods=["GET", "POST"],
    response_model=SlackConversationInfo,
    openapi_extra={"parameters": _P_INFO},
)
async def conversations_info(request: Request):
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    if err := _missing_argument(request, "channel"):
        return err
    name = _channel_name(conn, _param(request, "channel") or "")
    ids = auth.visible_ids(request, caller)
    # A channel this caller cannot see does not exist as far as they are concerned, so it answers
    # what an id resolving to nothing answers — `channel_not_found`, which is what the vendor
    # documents. The alternative describes the room (name, topic, purpose, creation time) to
    # someone who may not read a word of it.
    if name is None or not _channel_visibility(request, conn, ids)(name):
        return _err("channel_not_found")
    want_members = _param(request, "include_num_members") in ("1", "true", "True")
    ch = _info_channel(
        request,
        conn,
        name,
        include_num_members=want_members,
        visible_ids=ids,
        caller=caller,
    )
    return {"ok": True, "channel": ch}


@router.api_route(
    "/conversations.history",
    methods=["GET", "POST"],
    response_model=SlackHistory,
    openapi_extra={"parameters": _P_HISTORY},
)
async def conversations_history(request: Request):
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    if err := _missing_argument(request, "channel"):
        return err
    name = _channel_name(conn, _param(request, "channel") or "")
    ids = auth.visible_ids(request, caller)
    # A channel this caller cannot see is `channel_not_found`, exactly as in conversations.info —
    # NOT the `not_in_channel` the same page also documents, which is the other case: a channel the
    # token can see and has not joined, which a public channel here never is (its org grant admits
    # every principal). The ACL filter below cannot answer either one: an empty page reads as "you
    # may read this channel; it happens to be empty", which is the opposite of what is true.
    if name is None or not _channel_visibility(request, conn, ids)(name):
        return _err("channel_not_found")

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
            ts = float(r["ts"])
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
        rc = store.slack_reply_count(conn, name, r["ts"], ids) if r["thread_ts"] else 0
        latest = store.slack_latest_reply_ts(conn, name, r["ts"], ids) if rc else None
        ru = store.slack_reply_authors(conn, name, r["ts"], ids) if rc else []
        ruids = [synth.slack_user_id(e) for e in ru[:5]]
        messages.append(
            _message(
                r,
                reply_count=rc,
                latest_reply=latest,
                reply_users=ruids,
                reply_users_count=len(ru),
                subscribed=_subscribed(caller, r["author_email"], ru),
            )
        )
    cursor = next_cursor(offset, len(rows), total)
    body = {
        "ok": True,
        "messages": messages,
        "has_more": bool(cursor),
        "pin_count": 0,
        # Channel actions (the workflow/action bar) are not something a corpus expresses, but the
        # real API sends both keys on every call rather than omitting them.
        "channel_actions_ts": None,
        "channel_actions_count": 0,
    }
    # Omitted entirely on a last page, which is what the live API does here — NOT served with an
    # empty cursor. conversations.list is the other way round (it carries `{"next_cursor": ""}`
    # even with nothing more), so the two are not a shared convention to factor out.
    if cursor:
        body["response_metadata"] = {"next_cursor": cursor}
    return body


@router.api_route(
    "/conversations.replies",
    methods=["GET", "POST"],
    response_model=SlackHistory,
    openapi_extra={"parameters": _P_REPLIES},
)
async def conversations_replies(request: Request):
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    if err := _missing_argument(request, "channel", "ts"):
        return err
    ts = _param(request, "ts")
    name = _channel_name(conn, _param(request, "channel") or "")
    ids = auth.visible_ids(request, caller)
    # The channel is answered before the thread, and answered the same way its siblings answer it.
    # Measured live, against a private channel the token is not in beside an id that names nothing:
    # both are `channel_not_found` here, and `thread_not_found` is what a channel the caller CAN
    # see says about a ts it does not hold — so resolving the ts first would report a missing
    # thread for a room the caller cannot open.
    if name is None or not _channel_visibility(request, conn, ids)(name):
        return _err("channel_not_found")
    if not ts:
        return _err("thread_not_found")
    # Resolve the ts against ALL messages in the channel, not just roots: Slack accepts any in-thread
    # ts here, and a client that got its ts from a search hit will often pass a REPLY's ts. Find the
    # matched message, then return the thread it belongs to (its root's).
    # Fast path: a ts's integer part IS the row's created_ts (see the importer's `_slack_ts`), so
    # narrow to that second instead of loading the whole channel (eng-ml is ~340k rows → ~4s).
    # The full scan stays as a fallback for a row with no created_ts, which that query cannot
    # target.
    hit = None
    try:
        epoch = int(str(ts).split(".", 1)[0])
    except (TypeError, ValueError):
        epoch = None
    if epoch is not None:
        candidates = store.slack_messages_at_created_ts(conn, name, epoch, ids)
        hit = next((r for r in candidates if r["ts"] == ts), None)
    if hit is None:
        msgs = store.list_slack_channel_messages(conn, name, ids)
        hit = next((r for r in msgs if r["ts"] == ts), None)
    if hit is None:
        return _err("thread_not_found")
    if not hit["thread_ts"]:  # standalone message (no thread)
        return {"ok": True, "messages": [_message(hit)], "has_more": False}
    # `thread_ts` IS the root's ts, stored on every message of the thread — a root carries
    # its own, so this needs no case analysis and no re-derivation.
    rows = store.slack_thread(conn, name, hit["thread_ts"], ids)  # root + replies
    root = next((r for r in rows if r["thread_seq"] == 0), None)
    if root is None:
        return _err("thread_not_found")
    rc = sum(1 for x in rows if x["thread_seq"] > 0)
    latest = store.slack_latest_reply_ts(conn, name, root["ts"], ids) if rc else None
    ru = store.slack_reply_authors(conn, name, root["ts"], ids)
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
            subscribed=(
                _subscribed(caller, root["author_email"], ru) if x["thread_seq"] == 0 else False
            ),
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
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    if err := _missing_argument(request, "channel"):
        return err
    name = _channel_name(conn, _param(request, "channel") or "")
    # Membership is derived from who has spoken in the channel, so this list is a projection of the
    # very messages the ACL withholds — behind the same `channel_not_found` conversations.info
    # answers, and for the same reason.
    ids = auth.visible_ids(request, caller)
    if name is None or not _channel_visibility(request, conn, ids)(name):
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
    openapi_extra={"parameters": _P_PAGED},
)
async def users_list(request: Request):
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    # The roster is the registered user principals — the employee directory plus internal mail/doc
    # authors. Slack transcript speakers are NOT added, and that is a limitation of the upstream
    # dataset rather than a modelling choice, so it is stated plainly here and in the README.
    #
    # The speakers are NOT "mostly external": measured on a real corpus, of 74,138 distinct
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
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
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
      Backlot's channel-based corpus, so it's stripped without scoping rather than mis-scoped.
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
    caller, err = _caller_or_error(request)
    if err is not None:
        return err, None
    if err := _missing_argument(request, "query"):
        return err, None
    query = _param(request, "query") or ""
    # A query that arrived with nothing in it has its own error, measured live on all three search
    # methods: blank or whitespace-only is `no_query`, where absent is `invalid_arguments` above.
    if not query.strip():
        return _err("no_query"), None
    terms, container, phrase = _parse_slack_query(query)
    ids = auth.visible_ids(request, caller)  # results are scoped to the caller's ACL
    count = _int(request, "count", 20)
    page = max(1, _int(request, "page", 1))
    offset = (page - 1) * count
    # Honor Slack's sort: "score" (default) = relevance; "timestamp" = by message time. sort_dir
    # defaults to desc (newest first). Previously Backlot always ranked by relevance regardless.
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
    """Slack file search. Backlot has no uploaded-file corpus (files exist only as message
    attachments), so matches are always empty — but the endpoint must exist and return ok=True:
    real Slack has it, and mirage's grep push-down calls search.files for any file-inclusive scope;
    a 404 there reads as an error and forces a slow full-tree per-file fallback."""
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    if err := _missing_argument(request, "query"):
        return err
    query = _param(request, "query") or ""
    if not query.strip():
        return _err("no_query")
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
    """Slack's combined search (the slack-go SDK's Search()/SearchContext() hits this). Backlot
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
    text = row["content"]
    ts = row["ts"]
    m = {
        "type": "message",
        "team": TEAM_ID,
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
        "thread_ts"
    ]:  # a hit inside a thread carries its root ts so the client can fetch replies
        m["thread_ts"] = row["thread_ts"]
    return m


def _compute_channel_created(conn, name: str) -> int:
    """Channel creation second, pinned at/or before its earliest message so it never postdates
    one. The synthesized per-channel ``epoch(name)`` and a message's own clock are independent
    draws, so a message could otherwise predate its channel — which breaks clients (e.g. mirage)
    that list a day per date between creation and the latest message.

    Derived from a cheap aggregate rather than scanning every message: messages with an explicit
    ``created_ts`` contribute ``MIN(created_ts)``; any without one were synthesized at or after
    ``BASE_EPOCH``, so that is a safe lower bound for the group. ``created`` is the min of
    whichever groups are present."""
    b = store.slack_created_bounds(conn, name)
    if not b["total"]:
        return synth.epoch(name)
    candidates = []
    if b["have"]:  # rows with an explicit created_ts
        candidates.append(b["min_ts"])
    if b["have"] < b["total"]:  # rows whose clock was synthesized rather than given
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


def _channel_visibility(request: Request, conn, ids):
    """A predicate over channel names: may this caller see that the conversation EXISTS?

    One predicate for every method, because a channel hidden from conversations.list has to be
    hidden from the methods that resolve one by id as well — otherwise a channel id is enough for
    any authenticated principal to read a private room's name, topic and purpose and enumerate who
    is in it, none of which the ACL on its messages allows them to know.

    Costs what a listing pays: the warm cache when it is filled, and otherwise a principal-indexed
    query, which reads the caller's own grants rather than the channel's.
    """
    if ids is None:  # admin/service token: no filtering at all
        return lambda name: True
    cache = getattr(request.app.state, "channel_acl", None)
    if cache is not None:  # O(1) per channel: intersect its grantees with the caller's principals
        idset = set(ids)
        return lambda name: bool(cache.get(name, frozenset()) & idset)
    org = auth.acl(request).org_name  # cache not warm yet — public channels + granted ones
    granted = store.slack_channels_for_principals(conn, [p for p in ids if p != org])
    return lambda name: store.container_has_public(conn, "slack", name) or name in granted


def _is_private(request: Request, conn, name: str) -> bool:
    """Whether a channel is private: nothing in it is granted to the org.

    Read from the warm channel_acl cache, the org principal in a channel's grantee set being the
    same fact `store.container_has_public` asks the ACL table for. conversations.list applies this
    to every channel to honour `types`, not just to the page it shapes, so the per-channel query is
    worth avoiding whenever the cache can answer.
    """
    cache = getattr(request.app.state, "channel_acl", None)
    if cache is not None:
        return auth.acl(request).org_name not in cache.get(name, frozenset())
    return not store.container_has_public(conn, "slack", name)


def _is_member(conn, name: str, caller: Caller, *, is_private: bool) -> bool:
    """Whether the CALLER is in the channel — the conversation object's own definition of
    `is_member`, and the same membership conversations.members pages, so a client that stats a
    channel and then walks its members cannot get two answers to one question.

    The two kinds of channel answer it from different facts, because Slack shows them differently.
    A PUBLIC channel is shown to everybody, so seeing it is not being in it: real Slack lists the
    public channels a user is not in and answers `is_member: false` for them, and membership is who
    has posted. A PRIVATE channel is listed to its members only, so there the two are one fact —
    being shown it IS being in it, and every path here gates on `_channel_visibility` before it
    builds a channel object. `is_private: true` beside `is_member: false` and a full history is not
    a shape a client can be written against.

    A service token is not a person: it bypasses the ACL rather than belonging to anything, so it
    is a member of nothing — the reasoning `_subscribed` and `_last_read` apply to a thread and a
    read cursor.
    """
    if not caller.email:
        return False
    if is_private:
        return True
    return store.slack_channel_has_author(conn, name, caller.email)


def _member_count(request: Request, conn, name: str) -> int:
    """A channel's member count — the same set conversations.members pages, so `num_members` and
    walking the members cannot disagree. Read from the warm cache; if the background thread has not
    finished, count this one channel directly rather than blocking on all of them."""
    cache = getattr(request.app.state, "channel_members", None)
    if cache is not None:
        return cache.get(name, 0)
    return store.count_slack_channel_members(conn, name)


def _subscribed(caller: Caller, root_author: str | None, reply_authors) -> bool:
    """Whether the CALLER follows this thread — Slack subscribes you to one you started or replied
    in. An admin/service token is not a person and follows nothing, the same reasoning that makes
    fireflies' `mine` empty for one."""
    email = (caller.email or "").lower()
    if not email:
        return False
    if (root_author or "").lower() == email:
        return True
    return any((e or "").lower() == email for e in reply_authors or ())


def _message(
    row,
    reply_count: int = 0,
    latest_reply: str | None = None,
    reply_users: list[str] | None = None,
    reply_users_count: int = 0,
    parent_user_id: str | None = None,
    subscribed: bool = False,
) -> dict:
    text = row["content"]
    seed = f"{row['channel']}:{row['ts']}"
    m = {
        "type": "message",
        "user": synth.slack_user_id(row["author_email"]),
        "text": text,
        "ts": row["ts"],
        "team": TEAM_ID,
    }
    if not row["subtype"]:
        # `client_msg_id` is minted by the CLIENT that posted, and `blocks` is what that client
        # composed, so neither belongs on a message Slack itself generated — measured: a
        # channel_join carries only type/user/text/ts/subtype. `team` is left on both: it is
        # absent from a channel_join too, but a bot_message is a real posted message rather than a
        # system notice and there was none to measure, so dropping it everywhere would generalise
        # past the evidence.
        #
        # Seeded on (channel, ts), not ts alone: a ts is unique within its channel (see
        # store.ID_COLUMNS), so the same second in two channels produced one client_msg_id — a
        # value real Slack makes globally unique.
        m["client_msg_id"] = synth.slack_client_msg_id(seed)
        blocks = synth.slack_blocks(text, seed)
        if blocks:
            m["blocks"] = blocks
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
    if row["thread_ts"]:  # part of a thread
        m["thread_ts"] = row["thread_ts"]
        if row["thread_seq"] == 0 and reply_count > 0:  # thread root
            m.update(
                {
                    "reply_count": reply_count,
                    "reply_users_count": reply_users_count or len(reply_users or []),
                    "reply_users": reply_users or [],
                    "latest_reply": latest_reply,
                    # Per-CALLER state, which is why it is passed in rather than decided here:
                    # Slack subscribes you to a thread you started or replied in. Measured only in
                    # the affirmative -- the one token available authored both the root and the
                    # replies and was answered `true` -- so the negative case rests on Slack's own
                    # description of when it notifies you, not on an observation.
                    "subscribed": subscribed,
                    # A thread property rather than a per-caller one, and nothing in a corpus locks
                    # a thread.
                    "is_locked": False,
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
