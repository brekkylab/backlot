"""Slack Web API.

Base URL for a client: ``http://<host>/slack/api/`` (methods live under ``/api/``).
Slack always returns HTTP 200 with an ``{"ok": bool}`` envelope, so auth failures are
signalled in the body rather than as a 401 status. There are TWO of them and they are not
interchangeable: a MISSING credential is ``not_authed``, a credential that is present but does not
resolve is ``invalid_auth``. Connectors branch on exactly that split — re-authenticate versus give
up — so ``_caller_or_error`` decides it once for every method here.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from backlot import auth, overlay, published, store, synth
from backlot.acl import Caller
from backlot.config import get_settings
from backlot.openapi import qp
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
    # Real sends `latest` and then `oldest` right after `ok`, each only when it read one
    # (slack.com/api, 2026-10-11 and 2026-10-16); the route leaves out the one not set.
    latest: str | None = None
    oldest: str | None = None
    messages: list[dict] = []
    has_more: bool = False


class SlackReplies(_SlackOk):
    # Real answers no `latest` or `oldest` here, sent or not (slack.com/api, 2026-10-21).
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


class SlackFileSearch(_SlackOk):
    """`search.files` answers a `files` block and nothing else.

    Its own model rather than :class:`SlackSearch`, because a declared field with a default is
    SERVED whether or not the handler returns it: measured 2026-09-22, `search.files` answers
    `{files, ok, query}` against slack.com. `search.all` is the method that answers both blocks.
    """

    files: dict = {}


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

# The identity a write by the admin/service token carries. `auth.test` already answers that caller
# `USERVICE0` -- "real Slack answers a bot token with the app's own id rather than a user's" -- and
# a message it posts has to agree, because matching auth.test's `user_id` against message authors
# is how a client finds its own messages (see auth.test's docstring). SERVICE_EMAIL is the row's
# stored author, chosen to be improbable rather than impossible: `author_email` is free text in a
# BYO record (display-only speakers already use that), so a corpus that stated this exact string
# would have those messages served as USERVICE0.
SERVICE_EMAIL = "service-account"
SERVICE_USER_ID = "USERVICE0"
BOT_ID = "B0000BKLT"
#: The shape real requires of a `user` argument, quoted from the refusal it answers for one that
#: does not match (measured 2026-10-22 on `chat.postEphemeral`).
_SLACK_USER_ID = re.compile(r"^[UW][A-Z0-9]{2,}$")
#: Slackbot, who the placeholder a deleted thread root leaves is attributed to.
SLACKBOT_ID = "USLACKBOT"
#: What real leaves in place of a deleted thread root (measured, see `chat_delete`).
TOMBSTONE_TEXT = "This message was deleted."
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

# The methods that read arguments from a JSON body as well as a form. Slack's own spec says which:
# `consumes` on these six lists `application/json` beside the urlencoded type, and on every other
# method here -- the reads, and the three write-adjacent methods the spec types GET
# (`chat.getPermalink`, `reactions.get`, `reactions.list`) -- it lists the form type alone.
#
# Both halves of the split are measured against slack.com. `POST chat.postMessage` carrying
# `channel` and `text` as JSON posted the message and echoed the text it was sent (2026-10-21),
# so real reads a JSON body where its spec declares one. `POST chat.getPermalink` with its
# arguments as JSON answers `invalid_arguments` ("missing required field: channel"), so it
# ignores one where its spec declines one. `slack_sdk` 3.44.1 sends `chat.postMessage`,
# `chat.update` and `chat.postEphemeral` as JSON (`WebClient.api_call(..., json=kwargs)`) and the
# rest as form, so a client that works against real works here only if this set is right.
JSON_BODY_METHODS = frozenset(
    {
        "chat.postMessage",
        "chat.postEphemeral",
        "chat.update",
        "chat.delete",
        "reactions.add",
        "reactions.remove",
    }
)

# The write surface. Each lists what Backlot actually READS, the way every other `_P_` here does:
# a param the vendor documents but Backlot ignores stays an acknowledged gap in the fidelity
# baseline rather than a declaration that promises behaviour it does not have.
_P_POST_MESSAGE = [qp("channel", required=True), qp("text", required=True), qp("thread_ts")]
# `text` is NOT required here, where `chat.postMessage` requires it (`chat_post_ephemeral`).
_P_POST_EPHEMERAL = [qp("channel", required=True), qp("user", required=True), qp("text")]
_P_UPDATE = [qp("channel", required=True), qp("ts", required=True), qp("text", required=True)]
_P_DELETE = [qp("channel", required=True), qp("ts", required=True)]
_P_PERMALINK = [qp("channel", required=True), qp("message_ts", required=True)]
# `reactions.add` requires all three; `reactions.remove` and `reactions.get` take `channel` and
# `timestamp` together or neither, answering `no_item_specified` for neither — see
# `_reaction_target`. A client generated from this reads the difference.
_P_REACTION_ADD = [
    qp("channel", required=True),
    qp("timestamp", required=True),
    qp("name", required=True),
]
_P_REACTION = [qp("channel"), qp("timestamp"), qp("name", required=True)]
_P_REACTION_GET = [qp("channel"), qp("timestamp")]
_P_REACTION_LIST = [
    qp("count", "integer"),
    qp("page", "integer"),
    qp("limit", "integer"),
    qp("cursor"),
    qp("user"),
]

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


# The largest timestamp real reads, 2^63-1 microseconds (`9223372036854.775807`).
_SLACK_TS_MAX = 2**63 - 1


def _slack_ts(value: str | None) -> str | None | bool:
    """A Slack timestamp argument as real reads it, in the form real echoes it back
    (`SSSSSSSSSS.MMMMMM`): ``None`` when it is absent, empty or exactly `0`, and ``False`` for one
    real answers with ``invalid_ts_oldest``/``invalid_ts_latest``.

    Measured against slack.com/api on 2026-10-11 and 2026-10-16; Slack does not document it, and
    this is a fit to what was measured. The ASCII digits before the first `.`, anything else there
    dropped, are seconds (`1e9` is 19, `abc123` is 123, `1²` is 1); the ASCII digits of the next
    part are a whole number of microseconds written after the seconds, so `.5` is 5 µs and a seventh
    digit moves into the seconds; a third part is ignored; and the result is capped at
    :data:`_SLACK_TS_MAX` however many digits it has. A value that reads as zero is refused, one
    with no ASCII digit included (`abc`, `inf`, `١٢٣`, `５`, `00`, `0.0`, `-0`), but `0` itself is
    no bound.
    """
    if value is None or value in ("", "0"):
        return None
    head, _, tail = value.partition(".")
    seconds = "".join(c for c in head if c in "0123456789")
    micros = "".join(c for c in tail.partition(".")[0] if c in "0123456789")
    digits = (seconds + micros.lstrip("0").zfill(6)).lstrip("0")
    total = min(int(digits[:20] or "0"), _SLACK_TS_MAX)
    if not total:
        return False
    return f"{total // 1_000_000:010d}.{total % 1_000_000:06d}"


def _caller_or_error(request: Request) -> tuple[Caller | None, dict | None]:
    """Resolve the caller, or say which of Slack's two auth errors applies.

    Measured against the live API: a token that is merely unknown answers the same as a malformed
    one, so "malformed" is not a category this has to recognise — what matters is only whether a
    credential was presented at all, and Slack recognises two ways to present one: a `token` form
    field, read first, and an `Authorization: Bearer <t>` header (`auth.slack_token`). A `token`
    query parameter is answered before either is resolved, by the rule
    `auth.slack_query_token_refused` records.

        no credential, no `token` query param       -> not_authed
        `Authorization: Bearer ` (empty)            -> not_authed
        `token=` (empty) on its own                 -> not_authed
        any other header scheme                     -> not_authed
        any non-empty token that fails              -> invalid_auth
        a non-empty `token` query param             -> invalid_auth
        an empty `token` query param + a credential -> invalid_auth

    "any other header scheme" is the whole of the rest, not just the obvious `Basic`: the scheme is
    case-SENSITIVE live, so `bearer` and `BEARER` land here too, as does GitHub's legacy `token <t>`
    form. `auth.slack_bearer_token` is what draws that line, and is deliberately not the shared
    `auth.bearer_token` — see its docstring for why the two cannot be one parser.

    ``auth.slack_token`` returns None for every case in the first group: an unrecognised scheme
    parses to nothing, and an empty form value is falsy.

    A resolved token whose person a roster marks ``deactivated: true`` is answered Slack's own
    ``account_inactive`` instead of the caller it would otherwise resolve to; this is the one place
    that refusal is drawn. The method reference lists that error for all twelve served methods
    (``docs.slack.dev/reference/methods/<method>``, the Errors table, read 2026-09-22) and
    qualifies it the same way on each: "Authentication token is for a deleted user or workspace
    when using a ``bot`` token", where Backlot's per-person tokens are user tokens. An admin or
    anonymous caller has no email, so it is never one of these people.
    """
    if auth.slack_query_token_refused(request):
        return None, _err("invalid_auth")
    token = auth.slack_token(request)
    caller = auth.acl(request).resolve(token)
    if caller is None:
        return None, _err("invalid_auth" if token else "not_authed")
    if store.slack_is_deactivated(auth.conn(request), caller.email):
        return None, _err("account_inactive")
    return caller, None


def _credential_sent(request: Request) -> bool:
    """Whether the request sends a credential ``_caller_or_error`` resolves or refuses: a token it
    reads (``auth.slack_token``), or a `token` query parameter it refuses."""
    return auth.slack_query_token_refused(request) or auth.slack_token(request) is not None


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

    Measured on 2026-10-05 and 2026-10-07: real adds `response_metadata.messages`, one
    `[ERROR] missing required field: <name>` per argument not sent, in the order `names` lists them
    (`channel` before `ts` on conversations.replies).
    """
    missing = [n for n in names if _param(request, n) is None]
    if missing:
        return JSONResponse(
            {
                "ok": False,
                "error": "invalid_arguments",
                "response_metadata": {
                    "messages": [f"[ERROR] missing required field: {n}" for n in missing]
                },
            }
        )
    return None


# --- POST bodies -----------------------------------------------------------
# What real reads from a POST body and what it refuses the body for, measured against slack.com/api
# on 2026-10-07, 2026-10-10, 2026-10-13 and 2026-10-20: on `api.test` sent no token, which echoes
# the arguments it read, and on the methods that take a token, with one and without.

_FORM = "application/x-www-form-urlencoded"
_MULTIPART = "multipart/form-data"
#: The media types real takes a POST body under, matched as sent (before the first `;`).
_POST_TYPES = ("application/json", "text/plain", _FORM, _MULTIPART)
#: The charsets real takes, matched in any case once `"` is stripped from either end.
_POST_CHARSETS = ("utf-8", "utf8", "iso-8859-1")
#: An argument name real takes, as :func:`_registered_name` reads it.
_ARG_NAME = re.compile(r"[-0-9:A-Za-z_]{1,100}")
#: An integer name, which is no argument within 64 bits (:func:`_register`): `api.test` echoes none
#: of `1`, `-1`, `0` and `9223372036854775807`, and echoes `01`, `-0` and `9223372036854775808`.
_INT_NAME = re.compile(r"0|-?[1-9][0-9]*")


class Undecodable(str):
    """A form value whose bytes are not UTF-8. Real reads it as an argument that is there and names
    nothing: `channel=%FF` is `channel_not_found`, `limit=%FF` the number refusal and `token=%FF`
    `invalid_auth`, beside a valid bearer header too. So it is a U+FFFD to the methods, and
    `api_test` echoes it as `null`, as real does. A search's `query` is the exception: real answers
    `internal_error` for one on each search method, after the credential and the number and
    `sort_dir` refusals."""

    def __new__(cls):
        return super().__new__(cls, "\ufffd")


def post_content_type(request: Request) -> str:
    """A request's Content-Type as real reads it: each header's value with the spaces and tabs
    around it stripped (` application/x-www-form-urlencoded ` is a form and `application/json<TAB>`
    JSON), a header left empty dropped, and two headers joined with `, `, or empty when there is
    none. A form type sent ahead of `application/json` is read as a form, and the other way round is
    `invalid_post_type`, as one header naming both is."""
    values = (v.strip(" \t") for v in request.headers.getlist("content-type"))
    return ", ".join(v for v in values if v)


def _named_charset(content_type: str) -> str | None:
    """The charset a Content-Type names, or None: the first parameter spelt `charset`, once the
    spaces around it are stripped, whose value does not start with a space and holds more than `"`
    and spaces. `CHARSET=utf-8`, `charset =utf-8`, `charset= utf-8`, `charset=""` and a tab or a
    U+00A0 byte before `charset` name none, and `charset=; charset=latin1` names `latin1`."""
    for param in content_type.split(";")[1:]:
        name, _, value = param.strip(" ").partition("=")
        if name == "charset" and value[:1] != " " and value.strip('" '):
            return value
    return None


def post_charset_warning(content_type: str | None, has_body: bool) -> str | None:
    """The warning real Slack adds to a POST's answer for the charset its Content-Type names, or
    None: `superfluous_charset` for a form or multipart media type that names one, an empty body
    included, and `missing_charset` for any other media type that names none and carries a body.

    The media type is matched as sent: `Application/x-www-form-urlencoded` with a form body and
    `application/x-www-form-urlencoded, text/html` both draw `missing_charset`, where the
    upper-case JSON and `text/html` bodies this would warn are refused (:func:`read_post_body`).
    """
    if not content_type:
        return None
    named = _named_charset(content_type) is not None
    if content_type.split(";", 1)[0] in (_FORM, _MULTIPART):
        return "superfluous_charset" if named else None
    return "missing_charset" if has_body and not named else None


def read_post_body(
    content_type: str | None, body: bytes, method: str | None = None
) -> tuple[dict[str, str], str | None]:
    """The arguments a POST body carries, and the error real answers it with instead of running the
    method, or None. Each error comes ahead of the credential, a `token` query parameter included,
    and of the arguments, in the order the checks here run.

    A body is read as multipart when :func:`_rfc1867_boundary` reads a boundary off its
    Content-Type, else as a form when the Content-Type starts with the form type in any case
    (`APPLICATION/X-WWW-FORM-URLENCODED`, `…-urlencodedx` and `…-urlencoded, text/html` are read
    too); what it carries passes the media-type check whatever the media type is. A `text/plain`
    body is not read. `Content-Encoding` is not read: a gzipped body is read as the bytes it is,
    and a plain one under `gzip`, `bogus` or an empty value is read as itself.

    A JSON body is read for the `method` in :data:`JSON_BODY_METHODS` alone, after every check
    above, so what a body is refused for does not turn on which method it was sent to. Whether a
    JSON body is READ does, and both answers are measured on slack.com: `POST chat.postMessage`
    sent one posted the message (2026-10-21), and `POST chat.getPermalink` sent one answers
    `invalid_arguments` ("missing required field: channel").
    """
    if not content_type:
        return {}, "missing_post_type" if body else None
    pairs = _multipart_parts(content_type, body)
    if pairs is None and content_type.lower().startswith(_FORM):
        pairs = _form_pairs(body)
    args, error = _register(pairs or [])
    if error is not None:
        return {}, error
    media_type = content_type.split(";", 1)[0]
    if args is None and body and media_type not in _POST_TYPES:
        return {}, "invalid_post_type"
    if media_type not in (_FORM, _MULTIPART):
        charset = _named_charset(content_type)
        if charset is not None and charset.strip('"').lower() not in _POST_CHARSETS:
            return {}, "invalid_charset"
    if args is None and body and media_type in (_FORM, _MULTIPART):
        return {}, "invalid_form_data"
    if args is None and body and media_type == "application/json" and method in JSON_BODY_METHODS:
        return _json_args(body), None
    return args or {}, None


def _json_args(body: bytes) -> dict[str, str]:
    """The arguments a JSON object body carries, as the text a form field would have carried: a
    client that sends `7` or `true` and one that sends `"7"` or `"true"` name the same argument,
    and a handler reads one type rather than two. A body that is not an object carries none.

    `token` is dropped. It is a credential in a FORM body and `auth.slack_token` reads it ahead
    of the header, which is right there and wrong here: measured against slack.com on 2026-10-22,
    a JSON `{"token": <valid>}` with no header answers `not_authed`, and one carrying a bogus
    `token` beside a valid header is answered for the header. Kept, it would authenticate a
    client as whoever a stale field in its own payload names.
    """
    try:
        parsed = json.loads(body)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        k: v if isinstance(v, str) else json.dumps(v)
        for k, v in parsed.items()
        if v is not None and k != "token"
    }


def read_body(
    verb: str, content_type: str | None, body: bytes, method: str | None = None
) -> tuple[dict[str, str], str | None]:
    """:func:`read_post_body` for a request's verb: a `PUT`, `PATCH` or `DELETE` body is read as a
    `POST`'s and refused only for a name, `invalid_arg_name` or `invalid_array_arg`. Measured
    against slack.com/api on 2026-10-20: a form `token=<bogus>` under each of them is
    `invalid_auth`, and a form `{}`, a body with no Content-Type and one under `text/html` are read
    as no body.

    ``method`` is the Slack method the path names, which decides whether a JSON body is read at
    all (:data:`JSON_BODY_METHODS`)."""
    args, error = read_post_body(content_type, body, method)
    if verb != "POST" and error not in ("invalid_arg_name", "invalid_array_arg"):
        return args, None
    return args, error


def _form_pairs(body: bytes) -> list[tuple[bytes, bytes]]:
    """The decoded (name, value) of each `&`-separated piece of a form body that holds a `=`: a
    piece without one is dropped (`a=b&c` reads `a` alone, and `a` alone is `invalid_form_data`).
    `+` is a space and `%XX` a byte, and any other `%` is kept as sent (`a=%zz` is `%zz`)."""
    pairs = []
    for piece in body.split(b"&"):
        name, eq, value = piece.partition(b"=")
        if eq:
            pairs.append((_unquote(name), _unquote(value)))
    return pairs


def _unquote(raw: bytes) -> bytes:
    return urllib.parse.unquote_to_bytes(raw.replace(b"+", b" "))


#: C's `isspace` on a header's bytes, which HHVM's reading of a part applies.
_C_SPACE = b" \t\n\v\f\r"
#: HHVM's `FILLUNIT`: how many bytes its multipart buffer holds, and asks for at a time.
_FILLUNIT = 5120
#: HHVM's `Server.Upload.MaxFileUploads` as built: file parts past this many are skipped.
_MAX_FILE_UPLOADS = 20


def _rfc1867_boundary(content_type: str) -> bytes | None:
    """The boundary HHVM's ``IsRfc1867`` reads off a Content-Type, or None where it reads the body
    as no multipart one. The header's first token, up to a `;`, `,` or space, is compared to
    `multipart/form-data` in any case over the token's own length, so `multipart/form`, `m` and
    an empty token pass and `multipart/form-datax` does not; the boundary is what follows the
    first `=` after the first `boundary` past that token, up to the closing `"` when it starts
    with one (none is no boundary) and up to a `,` or `;` otherwise, a space after the `=` and an
    empty value included."""
    content_type = content_type.split("\0", 1)[0]
    token = re.split("[;, ]", content_type, maxsplit=1)[0]
    if token.lower() != _MULTIPART[: len(token)]:
        return None
    at = content_type.find("boundary", len(token))
    eq = content_type.find("=", at) if at != -1 else -1
    if eq == -1:
        return None
    rest = content_type[eq + 1 :]
    if rest.startswith('"'):
        close = rest.find('"', 1)
        if close == -1:
            return None
        return rest[1:close].encode("latin-1")
    return re.split("[,;]", rest, maxsplit=1)[0].encode("latin-1")


class _Rfc1867Buffer:
    """HHVM's `multipart_buffer` over a body held whole: the buffer is the body's bytes from
    ``begin`` (`buf_begin`) for ``held`` of them (`bytes_in_buffer`), a fill extends ``held`` to
    ``bufsize`` or to the body's end, and each method is the function it is named after."""

    def __init__(self, body: bytes, boundary: bytes):
        self.body = body
        self.bufsize = max(len(boundary) + 6, _FILLUNIT)
        self.boundary = b"--" + boundary
        self.boundary_next = b"\n--" + boundary
        self.begin = 0
        self.held = 0

    def fill(self) -> int:
        held = min(self.bufsize, len(self.body) - self.begin)
        added, self.held = held - self.held, held
        return added

    def eof(self) -> bool:
        return self.held == 0 and self.fill() < 1

    def next_line(self) -> bytes | None:
        """The bytes up to the next LF, less a CR before it, as C reads them (to a NUL); None when
        the buffer holds no LF and is not full, else the whole buffer as a partial line."""
        end = self.begin + self.held
        lf = self.body.find(b"\n", self.begin, end)
        if lf != -1:
            line = self.body[self.begin : lf].removesuffix(b"\r")
            self.held -= lf + 1 - self.begin
            self.begin = lf + 1
        elif self.held < self.bufsize:
            return None
        else:
            line = self.body[self.begin : end]
            self.begin, self.held = end, 0
        return line.split(b"\0", 1)[0]

    def get_line(self) -> bytes | None:
        line = self.next_line()
        if line is None:
            self.fill()
            line = self.next_line()
        return line

    def find_boundary(self) -> bool:
        while (line := self.get_line()) is not None:
            if line == self.boundary:
                return True
        return False

    def headers(self) -> list[tuple[bytes, bytes]] | None:
        """A part's headers from the next delimiter to the first empty line, or None when no
        delimiter is left: a line with a `:` and no space in front starts one, a line without
        adds to the one above and, with none above, is ignored, and one left empty is dropped."""
        if not self.find_boundary():
            return None
        headers: list[tuple[bytes, bytes]] = []
        key, pieces = b"", []
        while line := self.get_line():
            colon = line.find(b":") if line[0] not in _C_SPACE else -1
            if colon != -1:
                if pieces and key:
                    headers.append((key, b"".join(pieces)))
                    pieces = []
                key = line[:colon]
                if rest := line[colon + 1 :].lstrip(_C_SPACE):
                    pieces.append(rest)
            elif pieces:
                pieces.append(line)
        if pieces and key:
            headers.append((key, b"".join(pieces)))
        return headers

    def read(self) -> bytes:
        """`multipart_buffer_read` asked for ``_FILLUNIT`` bytes: the buffer's bytes up to a `\\n--`
        and the boundary, or up to a `\\n` the buffer ends on with a prefix of that after it, at
        most 5119 of them, less a CR they end on while such a bound is in view, which stays in the
        buffer. A bound the buffer ends on and a later fill does not disprove ends the value there:
        with a boundary longer than the buffer's 5120 bytes, which no client sends, that cuts a
        value at a `\\n--` of its own."""
        if _FILLUNIT > self.held:
            self.fill()
        body, begin, end, bn = self.body, self.begin, self.begin + self.held, self.boundary_next
        bound = body.find(bn, begin, end)
        if bound == -1:
            at = body.find(b"\n--", max(begin, end - len(bn) + 1), end)
            while at != -1:
                if body[at:end] == bn[: end - at]:
                    bound = at
                    break
                at = body.find(b"\n--", at + 1, end)
            if bound == -1 and end - begin >= 2 and body[end - 2 : end] == b"\n-":
                bound = end - 2
            elif bound == -1 and end > begin and body[end - 1] == 0x0A:
                bound = end - 1
        n = min((end if bound == -1 else bound) - begin, _FILLUNIT - 1)
        chunk = body[begin : begin + n]
        if bound != -1 and chunk.endswith(b"\r"):
            chunk = chunk[:-1]
            n -= 1
        self.begin += n
        self.held -= n
        return chunk

    def read_body(self) -> bytes:
        """`multipart_buffer_read_body`: :meth:`read` until it reads nothing."""
        chunks = []
        while chunk := self.read():
            chunks.append(chunk)
        return b"".join(chunks)


def _getword(text: bytes, stop: int, start: int = 0) -> tuple[bytes, int]:
    """`php_ap_getword`: the text from ``start`` up to the first ``stop`` byte outside quotes, and
    the offset past the run of ``stop`` bytes there. A `"` or `'` opens a quote its own kind
    closes, a `\\` before that quote excepted."""
    pos = start
    while pos < len(text) and text[pos] != stop:
        quote = text[pos]
        if quote in (0x22, 0x27):
            pos += 1
            while pos < len(text) and text[pos] != quote:
                pos += (
                    2 if text[pos] == 0x5C and pos + 1 < len(text) and text[pos + 1] == quote else 1
                )
            if pos < len(text):
                pos += 1
        else:
            pos += 1
    if pos >= len(text):
        return text[start:], len(text)
    word = text[start:pos]
    while pos < len(text) and text[pos] == stop:
        pos += 1
    return word, pos


def _getword_conf(text: bytes) -> bytes:
    """`php_ap_getword_conf`: a parameter's value past the spaces in front of it. Unquoted, it runs
    to a space or the end; quoted (the quotes :func:`_getword` knows), to the closing quote, but a
    closing quote that is not the last byte or followed by a CR or LF is read as part of the value
    and the search goes on (`"a"foo` is `a"foo`)."""
    i = 0
    while i < len(text) and text[i] in _C_SPACE:
        i += 1
    if i == len(text):
        return b""
    quote = text[i]
    if quote not in (0x22, 0x27):
        end = i
        while end < len(text) and text[end] not in _C_SPACE:
            end += 1
        return _substring_conf(text[i:end], 0)
    end = i + 1
    while True:
        while end < len(text) and text[end] != quote:
            end += 2 if text[end] == 0x5C and end + 1 < len(text) and text[end + 1] == quote else 1
        if end + 1 < len(text) and text[end + 1] not in (0x0D, 0x0A):
            end += 1
            continue
        return _substring_conf(text[i + 1 : end], quote)


def _substring_conf(text: bytes, quote: int) -> bytes:
    """`substring_conf`: ``text`` with `\\\\` read as `\\` and, inside quotes, `\\` and the quote as
    the quote."""
    out = bytearray()
    i = 0
    while i < len(text):
        following = text[i + 1] if i + 1 < len(text) else 0
        if text[i] == 0x5C and (following == 0x5C or (quote and following == quote)):
            out.append(following)
            i += 2
        else:
            out.append(text[i])
            i += 1
    return bytes(out)


def _normalize_protected(name: bytes) -> bytes:
    """`normalize_protected_variable`: a name as the set of protected names holds it, mangled up
    to its first `[` as :func:`_registered_name` mangles one, each index less the spaces, tabs
    and line breaks in front of it, and nothing after the last `]` that no `[` follows."""
    head, bracket, rest = name.lstrip(b" ").partition(b"[")
    out = bytearray(head.replace(b" ", b"_").replace(b".", b"_"))
    at = 0
    while bracket:
        while at < len(rest) and rest[at] in b" \r\n\t":
            at += 1
        close = rest.find(b"]", at)
        if close == -1:
            out += b"[" + rest[at:]
            break
        out += b"[" + rest[at : close + 1]
        bracket = rest[close + 1 : close + 2] == b"["
        at = close + 2
    return bytes(out)


def _skips_upload(name: bytes) -> bool:
    """Whether the name of a file's part fails HHVM's bracket rule, which skips the upload: a `]`
    that closes none opened, or one followed by a byte other than `[`."""
    depth = 0
    for i, byte in enumerate(name):
        if byte == 0x5B:
            depth += 1
        elif byte == 0x5D:
            depth -= 1
            if i + 1 < len(name) and name[i + 1] != 0x5B:
                return True
        if depth < 0:
            return True
    return False


def _multipart_parts(content_type: str, body: bytes) -> list[tuple[bytes, bytes | None]] | None:
    """The (name, value) of each part of a multipart body as real reads them, the value None for a
    file's part, or None for a Content-Type :func:`_rfc1867_boundary` reads no boundary off.

    Real's reading is HHVM's `rfc1867PostHandler` (`hphp/runtime/server/upload.cpp`, with
    `IsRfc1867` in `hphp/runtime/server/http-protocol.cpp`), the same at tags `HHVM-3.30.12` and
    `HHVM-4.172.3`, read off its answers: Slack does not say. The functions above port that
    code's and :class:`_Rfc1867Buffer` its buffer, so what they do is theirs; in short, a part's
    headers run from a line that is `--` and the boundary exactly to the first empty line, the
    first `Content-Disposition` names the part by its last `name` and makes it a file's by a
    `filename`, each parameter's name read in any case, a part with no `Content-Disposition` is
    no part, one with neither parameter ends the reading, one with a `filename` alone is a file's,
    and a value runs from the line after the headers to the cut :meth:`_Rfc1867Buffer.read_body`
    makes. A file's part past the twentieth, or one whose name fails :func:`_skips_upload`, is
    passed over with its body unread, so the next delimiter is found line by line; one with an
    empty `filename` is taken with its body unread too. A file's name, as
    :func:`_normalize_protected` spells it, and its `tmp_name` key (:func:`_tmp_name_key`) are
    protected: a later value under a protected name registers nothing. A value's name is looked
    up, and registered, as :func:`_normalize_protected` spells it, which HHVM does to the name in
    place for the lookup: `a[ b` is `a_b` here, and `invalid_arg_name` in a urlencoded form.
    Measured against slack.com/api on 2026-10-20: each multipart `POST` to `api.test` captured is
    answered as this reads it."""
    boundary = _rfc1867_boundary(content_type)
    if boundary is None:
        return None
    buffer = _Rfc1867Buffer(body, boundary)
    parts: list[tuple[bytes, bytes | None]] = []
    protected: set[bytes] = set()
    anonymous = uploads = 0
    while not buffer.eof():
        headers = buffer.headers()
        if headers is None:
            break
        disposition = next((v for k, v in headers if k.lower() == b"content-disposition"), None)
        if disposition is None:
            continue
        name = filename = None
        at = len(disposition) - len(disposition.lstrip(_C_SPACE))
        while at < len(disposition):
            pair, at = _getword(disposition, 0x3B, at)
            while at < len(disposition) and disposition[at] in _C_SPACE:
                at += 1
            if b"=" in pair:
                key, eq = _getword(pair, 0x3D)
                if key.lower() == b"name":
                    name = _getword_conf(pair[eq:])
                elif key.lower() == b"filename":
                    filename = _getword_conf(pair[eq:])
        if filename is None and name is not None:
            value = buffer.read_body()
            name = _normalize_protected(name)
            if name not in protected:
                parts.append((name, value))
            continue
        if name is None and filename is None:
            break
        if name is None:
            name = b"%d" % anonymous
            anonymous += 1
        if uploads >= _MAX_FILE_UPLOADS or _skips_upload(name):
            continue
        uploads += 1
        if filename:
            buffer.read_body()
        protected.add(_normalize_protected(name))
        protected.add(_normalize_protected(_tmp_name_key(name)))
        parts.append((name, None))
    return parts


def _tmp_name_key(name: bytes) -> bytes:
    """The `$_FILES` key HHVM protects beside a file's name: `name[tmp_name]`, or for a name that
    holds a `[` and ends in `]`, `head[tmp_name][index]` with `index` what lies between its first
    `[` and its last `]`."""
    first = name.find(b"[")
    if first != -1 and name.endswith(b"]"):
        return name[:first] + b"[tmp_name][" + name[first + 1 : -1] + b"]"
    return name + b"[tmp_name]"


def _registered_name(name: bytes) -> tuple[str, bool] | None:
    """The argument name real reads for a decoded one, and whether its value is an array, or None
    for a name that registers nothing: it ends at a NUL, leading spaces are dropped, and a space or
    `.` is `_`. A `[` closed by a `]` later on starts an array index; a `[` that is not ends in `_`
    and the rest is kept as sent (`a[b.c` is `a_b.c`). A name that is empty before its `[` (`[a]`,
    ` `, `=x`) registers nothing."""
    text = name.split(b"\0", 1)[0].lstrip(b" ").decode("latin-1")
    bracket = text.find("[")
    base = (text if bracket == -1 else text[:bracket]).replace(" ", "_").replace(".", "_")
    if not base:
        return None
    if bracket == -1:
        return base, False
    if text.find("]", bracket + 1) != -1:
        return base, True
    return base + "_" + text[bracket + 1 :], False


_ARRAY = object()


def _register(pairs: list[tuple[bytes, bytes | None]]) -> tuple[dict | None, str | None]:
    """The arguments ``pairs`` register, the last value of a name read, or None for none at all, and
    the error real refuses them with: `invalid_arg_name` for any name :data:`_ARG_NAME` does not
    take, ahead of `invalid_array_arg` for any whose last value is an array, wherever each comes
    in the body. A file's part registers without being an argument, and an integer name
    (:data:`_INT_NAME`, within 64 bits) is no argument either. A file's name is not checked:
    `name="a{"` and `name="a[b]"` with a `filename` are served."""
    args: dict = {}
    registered = False
    for raw_name, raw_value in pairs:
        reg = _registered_name(raw_name)
        if reg is None:
            continue
        registered = True
        name, is_array = reg
        if raw_value is None:
            continue
        if is_array:
            args[name] = _ARRAY
            continue
        try:
            args[name] = raw_value.decode("utf-8")
        except UnicodeDecodeError:
            args[name] = Undecodable()
    if not registered:
        return None, None
    if any(not _ARG_NAME.fullmatch(name) for name in args):
        return {}, "invalid_arg_name"
    if any(value is _ARRAY for value in args.values()):
        return {}, "invalid_array_arg"
    return {
        name: value
        for name, value in args.items()
        if not (_INT_NAME.fullmatch(name) and -(2**63) <= int(name) < 2**63)
    }, None


# --- Paths no route here serves -----------------------------------------------
# How real answers a request for a path under `/api/` that none of the methods routed below is,
# measured against slack.com/api on 2026-09-28 and 2026-10-20.

#: The HTTP methods real answers such a path for by the rules below, each as it answers a `GET` once
#: its body is read (:func:`read_body`), but for `blocks.validate` (:data:`_ANSWERED_BY_ARGUMENTS`).
UNSERVED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

#: A path segment this server reads as a method's name once it is decoded (`a%2Eb` is `a.b`): ASCII
#: letters, digits and `.`, but not `.` or `..` alone (:func:`method_name`); any other byte in it,
#: where :func:`unserved_answer` does not leave the path to the router, is answered with real's 404
#: page. Real answers `-`, `_` and the bytes past ASCII so, but refuses `%00`, `%2F`, `%3F`, `%5C`
#: and a `%` that starts no escape at its front end, and `%0A` but at a name's end, where it drops
#: `%09`, `%0A`, `%0B`, `%0D`, `%20` and `%2B`.
_METHOD_NAME = re.compile(r"[0-9A-Za-z.]+")
#: The Slack baseline's ``missing`` operations (``backlot.published``): the methods Slack's
#: reference index lists that no route here serves, by name. Measured on 2026-10-20 with no token
#: and with a bogus one, by `GET` and by `POST`, each answered the credential's refusal
#: (`not_authed`, `invalid_auth`) but those :data:`_OTHER_ANSWERS` and
#: :data:`_ANSWERED_BY_ARGUMENTS` hold. Ten that Slack's archived OpenAPI document lists and the
#: index does not, `oauth.token`, three `workflows` ones and six `apps.permissions` ones, real
#: answered `unknown_method` for, as for a name it never had, whatever the credential, one that
#: resolves included.
_PUBLISHED_NOT_SERVED = frozenset(published.missing_operations("slack"))
#: The answer, where it is not the credential's refusal, of a method no route here serves, and
#: whether a credential sent that does not resolve is refused ahead of it, each also answered so to
#: a token that resolves. The `.mark` and `.list` ones are among the names `slack_sdk` 3.45.0 calls
#: that the baseline does not list, sent by `GET` with each credential and by `POST` with none; the
#: rest of those names, `channels.list` and `im.open` among them, were `unknown_method`.
_OTHER_ANSWERS: dict[str, tuple[dict, bool]] = {
    "oauth.access": ({"ok": False, "error": "invalid_code"}, False),
    **{
        name: ({"ok": False, "error": "invalid_code"}, True)
        for name in ("oauth.v2.access", "oauth.v2.user.access", "openid.connect.token")
    },
    **{
        name: (
            {
                "ok": False,
                "error": "method_deprecated",
                "response_metadata": {
                    "messages": [
                        "[ERROR] This method is retired and can no longer be used. Please use "
                        f"{instead} instead. Learn more: https://api.slack.com/changelog/"
                        "2020-01-deprecating-antecedents-to-the-conversations-api."
                    ]
                },
            },
            True,
        )
        for name, instead in (
            ("channels.mark", "conversations.mark"),
            ("groups.mark", "conversations.mark"),
            ("im.mark", "conversations.mark"),
            ("mpim.mark", "conversations.mark"),
            ("im.list", "conversations.list or users.conversations"),
            ("mpim.list", "conversations.list or users.conversations"),
        )
    },
}
#: Methods whose answer to a caller real lets through turns on the arguments sent, which is left to
#: the router, each with whether it refuses a credential that does not resolve before reading them
#: (`invalid_auth`, as on any method). Real answered `oauth.v2.exchange` `invalid_arguments` naming
#: the arguments missing whatever the credential, `tooling.tokens.rotate` `invalid_arguments`
#: without a `refresh_token` and `invalid_refresh_token` with a bogus one, and `blocks.validate`
#: `ok` to a `POST` of `blocks=[]` and `invalid_arguments` to a `POST`, `PUT`, `PATCH` or `DELETE`
#: of nothing; a `GET` of `blocks.validate` was `unsupported_http_method` whatever the credential,
#: which :func:`unserved_answer` answers (2026-10-20, the `PATCH` on 2026-10-21). The rest is left
#: to the router: an answer for one set of arguments does not hold for another.
_ANSWERED_BY_ARGUMENTS = {
    "blocks.validate": True,
    "oauth.v2.exchange": False,
    "tooling.tokens.rotate": True,
}
#: Real's answer to a path that names no method, where :func:`unserved_answer` gives it, but for the
#: bytes :data:`_METHOD_NAME` says real refuses at its front end: this page, 1736 bytes under
#: `text/html; charset=utf-8`, the same on each of the 611 answers that carried it on 2026-10-20.
_NOT_FOUND_PAGE = (
    b"<!DOCTYPE html>\n"
    b'<html lang="en-US">\n'
    b"<head>\n"
    b'<meta charset="utf-8">\n'
    b'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    b'<meta name="robots" content="noindex">\n'
    b"<title>There\xe2\x80\x99s been a glitch\xe2\x80\xa6 | Slack</title>\n"
    b"<style>\n"
    b"html, body { height: 100%; margin: 0; }\n"
    b"body {\n"
    b"\tbackground: #c6f5ff url('https://a.slack-edge.com/80588/img/404/marrakesh-meadow-80.j"
    b"pg') no-repeat 50% 100% / cover;\n"
    b"\tcolor: #1d1c1d;\n"
    b"\tfont: 17px/1.5 Slack-Lato, Lato, -apple-system, BlinkMacSystemFont, 'Segoe UI', Robot"
    b"o, Helvetica, Arial, sans-serif;\n"
    b"}\n"
    b"header { background: rgba(255, 255, 255, 0.95); border-bottom: 1px solid #ddd; padding:"
    b" 20px 24px; }\n"
    b"header img { display: block; height: 28px; }\n"
    b"main {\n"
    b"\tbackground: #fff; border: 1px solid #ddd; border-radius: 4px; box-shadow: 0 1px 0 rgb"
    b"a(0, 0, 0, 0.25);\n"
    b"\tmargin: 56px auto 0; max-width: 390px; padding: 32px 36px;\n"
    b"}\n"
    b"h1 { font-size: 32px; font-weight: 900; letter-spacing: -0.5px; line-height: 1.2; margi"
    b"n: 0 0 20px; }\n"
    b"h1 svg { height: 32px; margin-right: 8px; vertical-align: -5px; width: 32px; }\n"
    b"p { margin: 0 0 12px; }\n"
    b"a { color: #1264a3; }\n"
    b"@media (max-width: 480px) { main { margin: 24px 16px 0; } }\n"
    b"</style>\n"
    b"</head>\n"
    b"<body>\n"
    b'<header><a href="https://slack.com/"><img src="https://a.slack-edge.com/9cc0056/img/sla'
    b'ck_logo.svg" alt="Slack"></a></header>\n'
    b"<main>\n"
    b'<h1><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#de4e2b" stro'
    b'ke-width="2" stroke-linejoin="round" d="M12 3 2 21h20L12 3z"/><path fill="#de4e2b" d="M'
    b'11 10h2v6h-2zM11 17.5h2v2h-2z"/></svg>There\xe2\x80\x99s been a glitch\xe2\x80\xa6</h1>'
    b"\n"
    b"<p>We\xe2\x80\x99re not quite sure what went wrong. You can go back, or try looking on "
    b'our <a href="http://get.slack.help" target="_blank">Help Center</a> if you need a hand.'
    b"</p>\n"
    b"</main>\n"
    b"</body>\n"
    b"</html>"
)


def method_name(path: str) -> str | None:
    """The method a `/slack/api/` path names, as :data:`_METHOD_NAME` reads it, or None for one it
    reads as none, for `.` and `..`, and for a path elsewhere."""
    name = path.removeprefix("/slack/api/")
    if name in (".", "..") or not _METHOD_NAME.fullmatch(name):
        return None
    return name


def unserved_answer(request: Request) -> Response | None:
    """Real's answer to a request for a path no route here matches, or None for the router's 404:
    the method's credential refusal (``_caller_or_error``) for a method Slack has, ahead of whatever
    the method would read, or the answer :data:`_OTHER_ANSWERS` holds for it; `unknown_method` with
    the name for one it has none by, whatever the credential; and its 404 page for a path that names
    no method. A credential that resolves, on a method :data:`_OTHER_ANSWERS` does not hold, is left
    to the router, and so is a method of :data:`_ANSWERED_BY_ARGUMENTS` but for the answers its
    comment gives here, and so are an empty name, the names `.` and `..`, a path ending in `/`, and
    a name after an escaped run, which ``backlot.main.merge_slashes_before_a_slack_method_name``
    does not merge."""
    path = request.scope["path"]
    rest = path.removeprefix("/slack/api/")
    if rest in ("", ".", "..") or path.endswith("/"):
        return None
    lead = rest.replace("\\", "/")
    if lead.startswith("/") and _METHOD_NAME.fullmatch(lead.lstrip("/")):
        return None
    name = method_name(path)
    if name is None:
        return Response(_NOT_FOUND_PAGE, status_code=404, media_type="text/html; charset=utf-8")
    if name in _ANSWERED_BY_ARGUMENTS:
        if name == "blocks.validate" and request.method == "GET":
            return _err("unsupported_http_method")
        if not (_ANSWERED_BY_ARGUMENTS[name] and _credential_sent(request)):
            return None
    other = _OTHER_ANSWERS.get(name)
    if other is None and name not in _PUBLISHED_NOT_SERVED:
        return JSONResponse({"ok": False, "error": "unknown_method", "req_method": name})
    refusal = _caller_or_error(request)[1]
    if other is None:
        return refusal
    answer, refused_first = other
    if refused_first and refusal is not None and _credential_sent(request):
        return refusal
    return JSONResponse(answer)


def attach_warning(payload: dict, warning: str) -> dict:
    """Add `warning` and `response_metadata.warnings` to a Slack answer where real puts them:
    `warning` after the method's own keys, `response_metadata` after it, and `warnings` after
    whatever `response_metadata` already holds (`next_cursor`, `messages`). Real does so on each
    of the twelve methods Backlot serves (slack.com/api, 2026-10-10)."""
    meta = payload.pop("response_metadata", None)
    payload["warning"] = warning
    if meta is not None:
        warnings = meta.setdefault("warnings", [])
        if warning not in warnings:
            warnings.append(warning)
        payload["response_metadata"] = meta
    else:
        payload["response_metadata"] = {"warnings": [warning]}
    return payload


# `sort_dir` is an enum on search.messages, search.all and search.files, matched as sent: measured
# on 2026-10-03 and 2026-10-05, `ASC` and `bogus` answer this on all three and `Desc`, ` asc` and
# `asc ` on search.messages, while `asc` and `desc` are served. An empty `sort_dir` is served on
# search.messages and answers this on search.all and search.files. A repeated `sort_dir` is read
# by its last value. A missing `query` is answered before this and an empty one after it. `sort`
# has no such check, `sort=bogus` is served.
_SORT_DIRS = {"asc", "desc"}
_SORT_DIR_LINE = "[ERROR] must be a valid enum value [json-pointer:/sort_dir]"
# A search's `query` made of these characters alone is `no_query` on all three search methods,
# where one of `\x0c` or U+00A0 alone is searched for (slack.com/api, 2026-10-20).
_QUERY_BLANKS = " \t\n\r\0\x0b"


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
        # The same string as `name`, measured rather than assumed: every channel a live
        # workspace's conversations.list answers with carries a `name_normalized` identical to
        # its `name`, and that holds for the non-Latin ones too, which is where a normalization
        # would show if there were one. The conversation object reference names the field and
        # says only "the normalized version of the channel name", never what normalizing does,
        # and its example is ASCII -- so the response is the only source for the value.
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


def _uid(email: str) -> str:
    """A stored author's Slack user id.

    Every author but one is a corpus address hashed by :func:`synth.slack_user_id`. The exception
    is the service account, which is not a person and whose id `auth.test` already fixes at
    ``USERVICE0``; hashing its sentinel address instead would give a client two different ids for
    the same caller.

    Used wherever a STORED author becomes an id — the message, a search hit, a thread's reply
    authors and its parent, a reaction, an edit stamp, a user object. A site that hashes directly
    would answer a different id for the same person than the one beside it.
    """
    return SERVICE_USER_ID if email == SERVICE_EMAIL else synth.slack_user_id(email)


def _handle(email: str) -> str:
    """A corpus email as Slack's handle — the `name` a user object carries and the `user`
    `auth.test` reports for the same person."""
    return email.split("@")[0].replace(".", "")


def _user_obj(conn, email: str, caller: Caller) -> dict:
    u = store.get_user(conn, email)
    display = u["display_name"] if u else email.split("@")[0]
    parts = display.split()
    updated = synth.epoch("user:" + email)
    # The service account is a bot in the sense every client cares about: it posts as an app, and
    # `auth.test` reports it with the app-shaped id `_uid` returns.
    is_bot = email == SERVICE_EMAIL or (not u and email.split("@")[0].endswith("bot"))
    # A roster entry's `deactivated: true`. Measured against a live workspace on 2026-09-17:
    # `deleted` is present on all 19 members and true on the 9 deactivated ones, so it is served
    # unconditionally where docs.slack.dev's user object allows either ("Otherwise the value is
    # false, or the field may not appear at all"). `is_forgotten` is a different state, not a
    # spelling of this one: the same page types it Boolean and gives it as "Whether the user has
    # been GDPR-forgotten", and on that workspace 5 of the 9 carried it against 0 of the 10
    # active. A roster states no GDPR erasure, so there is nothing here to derive it from.
    deactivated = bool(u) and store.slack_is_deactivated(conn, email)
    obj = {
        "id": _uid(email),
        "team_id": TEAM_ID,
        "name": _handle(email),
        "deleted": deactivated,
        "is_bot": is_bot,
        "is_app_user": is_bot,
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
    # Measured live against a real workspace, 2026-09-21 (`users.list`, `users.info`): a
    # deactivated member drops `real_name`, `color`, the admin/ownership/restriction flags and the
    # tz fields entirely — not `false`/empty, absent. All 9 deactivated members carried none of
    # those ten, and all 11 active ones (8 people, 3 bot-shaped) carried all ten.
    if not deactivated:
        obj.update(
            {
                "real_name": display,
                "is_admin": False,
                "is_owner": False,
                "is_primary_owner": False,
                "is_restricted": False,
                "is_ultra_restricted": False,
                "tz": "America/Los_Angeles",
                "tz_label": "Pacific Time",
                "tz_offset": -28800,
                "color": synth._digest(email)[:6],
            }
        )
        # `has_2fa` has two conditions of its own, measured over both methods. The caller: an
        # admin user token carries it for all 8 active people and a bot token for none of them
        # (2026-09-21), which is docs.slack.dev's "Only visible if the user executing the call is
        # an admin". The docs do not name the caller's own member: a user token that is not an
        # admin carries it on that member and on no other (2026-09-23: on 1 of 20 members in
        # `users.list`, the caller's, and in `users.info` for the caller and for none of the 7
        # other active people). The member: no `is_bot` one carries it under the admin user token
        # or the bot token. Backlot answers every active member `is_admin: false`, so its
        # admin/service token is the only caller here that is an admin.
        if (caller.is_admin or email == caller.email) and not is_bot:
            obj["has_2fa"] = False
    return obj


@router.api_route("/api.test", methods=["GET", "POST"], response_model=SlackApiTest)
async def api_test(request: Request):
    """Real Slack's connectivity check: served with no credential at all, and an echo of the
    arguments it was sent, as `args`. Several real clients call it at construction/connect time
    (e.g. llama-index's `SlackReader.__init__`), so Backlot must answer it rather than 404.

    A credential sent is read as on every other method (``_caller_or_error``) and refused ahead of
    an `error` argument when it does not resolve, and so is a `token` query parameter
    ``auth.slack_query_token_refused`` refuses, one that resolves included; with none, the request
    is served. The query's arguments come first in `args`, then the body's, and the token read is
    `token`: in the place of the body's `token` field, an empty one included, and last when it came
    in the bearer header alone. A non-empty `error` answers `ok: false` with that error, `null` for
    a body's that is not UTF-8, and `args` beside it. Measured against slack.com/api on 2026-10-13
    and 2026-10-20."""
    if _credential_sent(request):
        _, refusal = _caller_or_error(request)
        if refusal is not None:
            return refusal
    args: dict[str, str | None] = dict(request.query_params)
    form = getattr(request.state, "_form", None)
    if form:
        args.update({k: None if isinstance(v, Undecodable) else v for k, v in form.items()})
    token = auth.slack_token(request)
    if token is not None:
        args["token"] = token
    error = _param(request, "error")
    if error:
        error = None if isinstance(error, Undecodable) else error
        return JSONResponse({"ok": False, "error": error, "args": args})
    return {"ok": True, "args": args}


@router.api_route("/auth.test", methods=["GET", "POST"])
async def auth_test(request: Request):
    """Who the token is, in the caller's own terms.

    Measured 2026-09-22 with a user token: `auth.test` answered `"user"` as that person's handle
    and `"user_id"` as their own `U…`, the same id `users.list` reports for them. The method's own
    reference pins both fields the same way — its user-token success example is `"user": "grace"`
    with `"user_id": "W12345678"`. So `user` is the HANDLE, not an address — the same `name` a
    user object carries for that person — and `user_id` is that caller's own id, the one a message
    in `conversations.history` names as its author.

    A workspace constant can never match: "call auth.test, then match `user_id` against message
    authors" is how a client finds its own messages, so the id is the field that costs.

    The admin/service token has no corpus email, so it keeps the service identity: that caller is
    not a person, and real Slack answers a bot token with the app's own id rather than a user's."""
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    who = SERVICE_EMAIL if caller.is_admin else _handle(caller.email)
    return {
        "ok": True,
        "url": f"https://{get_settings().org_name}.slack.com/",
        "team": get_settings().org_name,
        "user": who,
        "team_id": TEAM_ID,
        "user_id": SERVICE_USER_ID if caller.is_admin else synth.slack_user_id(caller.email),
        # Measured against slack.com on 2026-10-09 and 2026-10-11 with a user token, on a workspace
        # outside any Enterprise organization: every `ok: true` answer, over GET and POST, has these
        # members in this order and ends with `"is_enterprise_install": false`.
        "is_enterprise_install": False,
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
    limit, err = _limit(request)
    if err is not None:
        return err
    if limit < 0:  # every item, as for no `limit` (see `_limit`)
        limit = get_settings().default_page_size
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
    response_model_exclude_unset=True,
    openapi_extra={"parameters": _P_HISTORY},
)
async def conversations_history(request: Request):
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    if err := _missing_argument(request, "channel"):
        return err
    limit, err = _limit(request)
    if err is not None:
        return err
    if limit < 0:  # one message (see `_limit`)
        limit = 1
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
    limit = min(limit, _HISTORY_MAX_ROOTS)
    # Slack history returns only top-level messages (thread roots + standalone);
    # replies live under conversations.replies.
    lo, hi = _slack_ts(_param(request, "oldest")), _slack_ts(_param(request, "latest"))
    # Real reads `latest` before `oldest`, and either timestamp before the cursor, so the errors
    # answer in that order: measured against slack.com/api on 2026-10-10, `oldest=abc&latest=abc`
    # answers `invalid_ts_latest`, and an unreadable timestamp beside an undecodable cursor answers
    # the timestamp's error, never `invalid_cursor`. On 2026-10-12 the same answers came back with
    # the three in every query order, and in a POST form body; an empty `latest` or `oldest` was
    # read as none, leaving `invalid_cursor` beside an undecodable cursor; and a channel that names
    # nothing answered `channel_not_found` ahead of all three.
    if hi is False:
        return _err("invalid_ts_latest")
    if lo is False:
        return _err("invalid_ts_oldest")
    offset = decode_cursor_or_none(_param(request, "cursor"))
    if offset is None:
        return _err("invalid_cursor")
    # Each page is newest first. The pages walk back in time from the newest message or from
    # `latest`, or forward from `oldest` when it is the only bound. Measured against slack.com/api
    # on 2026-10-23 over a 330-message channel with limit=3: no bound, `oldest=0` and a `latest`
    # past the newest message served the newest three and then the three before those; `latest`,
    # alone or beside `oldest`, the three just before it and then the three before those; `oldest`
    # alone, or beside a `latest` of `0` or an empty one, the three just after it and then the
    # next three.
    from_oldest = lo is not None and hi is None
    if lo is not None or hi is not None:  # time-bounded (e.g. a single day): filter, then paginate
        # Measured against slack.com/api on 2026-10-18: only absent, empty, "0" and
        # case-insensitive "false" exclude the bound. Other strings, including "00", "no"
        # and whitespace-padded "false", include it. Repeated query values use the last one.
        inclusive = (_param(request, "inclusive") or "").lower() not in ("", "0", "false")

        def _ts_to_us(ts_str: str) -> int:
            """Convert a Slack ts string "SSSSSSSSSS.MMMMMM" to total microseconds."""
            s, m = ts_str.split(".")
            return int(s) * 1_000_000 + int(m)

        def _in_window(r) -> bool:
            ts_us = _ts_to_us(r["ts"])
            # Both bounds are exclusive unless the `inclusive` read above includes them: measured
            # against slack.com/api on 2026-10-16, a message whose ts equals `latest` is left out,
            # as one at `oldest` is, and a page bounded by one message's ts on both sides is empty —
            # `inclusive=true` puts that message back, alone.
            if lo is not None:
                lo_us = _ts_to_us(lo)
                if ts_us < lo_us if inclusive else ts_us <= lo_us:
                    return False
            if hi is not None:
                hi_us = _ts_to_us(hi)
                if ts_us > hi_us if inclusive else ts_us >= hi_us:
                    return False
            return True

        # SQL-narrow by created_ts (±1s to cover the sub-second fraction in a public ts), then apply
        # the exact window in Python — so a day window scans the day, not the whole channel.
        lo_sec = int(lo.split(".")[0]) - 1 if lo is not None else None
        hi_sec = int(hi.split(".")[0]) + 1 if hi is not None else None
        matched = [
            r
            for r in store.list_slack_top_level(
                conn,
                name,
                ids,
                limit=10**9,
                ts_lo=lo_sec,
                ts_hi=hi_sec,
                newest_first=not from_oldest,
            )
            if _in_window(r)
        ]
        total = len(matched)
        rows = matched[offset : offset + limit]
        if from_oldest:
            rows.reverse()
    else:
        total = store.count_slack_top_level(conn, name, ids)
        rows = store.list_slack_top_level(
            conn, name, ids, limit=limit, offset=offset, newest_first=True
        )
    messages = []
    for r in rows:
        rc = store.slack_reply_count(conn, name, r["ts"], ids) if r["thread_ts"] else 0
        latest = store.slack_latest_reply_ts(conn, name, r["ts"], ids) if rc else None
        ru = store.slack_reply_authors(conn, name, r["ts"], ids) if rc else []
        ruids = [_uid(e) for e in ru[:5]]
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
        **({"latest": hi} if hi is not None else {}),
        **({"oldest": lo} if lo is not None else {}),
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
    response_model=SlackReplies,
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
    ruids = [_uid(e) for e in ru[:5]]
    parent_uid = _uid(root["author_email"])
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
    limit, err = _limit(request)
    if err is not None:
        return err
    if limit < 0:  # every item, as for no `limit` (see `_limit`)
        limit = get_settings().default_page_size
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
    limit, err = _limit(request)
    if err is not None:
        return err
    if limit < 0:  # see `_limit`
        sent = _param(request, "cursor")
        if decode_cursor_or_none(sent) is None:
            return _err("invalid_cursor")
        if limit < -1:
            return _err("fatal_error")
        if sent:
            return _err("internal_error")
        total = len(store.all_user_emails(conn))
        return {
            "ok": True,
            "members": [],
            "response_metadata": {"next_cursor": next_cursor(0, 1, total)},
        }
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
    page = emails[offset : offset + limit]
    members = [_user_obj(conn, e, caller) for e in page]
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
            return {"ok": True, "user": _user_obj(conn, e, caller)}
    # Display-only Slack speakers/bots (deploybot@…, payments-bot slugged to paymentsbot@…) aren't
    # principals; resolve them from the message authors so their IDs don't come back user_not_found.
    email = _slack_author_by_uid(request, conn, uid)
    if email:
        return {"ok": True, "user": _user_obj(conn, email, caller)}
    return _err("user_not_found")


def _slack_author_by_uid(request: Request, conn, uid: str) -> str | None:
    """Reverse a synthesized Slack user id to a message-author email (synth is one-way, so build a
    map from the distinct authors and cache it on app.state — the DISTINCT scan runs once)."""
    cache = getattr(request.app.state, "_slack_uid_map", None)
    if cache is None:
        cache = {synth.slack_user_id(e): e for e in store.distinct_slack_author_emails(conn)}
        # The service account is not a corpus author, so the DISTINCT scan cannot find it — but a
        # message it posted names it, and `auth.test` tells a client to match that id against
        # message authors. Resolving to nothing there breaks the one flow that docstring describes.
        cache[SERVICE_USER_ID] = SERVICE_EMAIL
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


def _messages_block(request: Request, *, empty_sort_dir_ok: bool):
    """Shared message-search core for search.messages and search.all. Returns (query, block) or
    (error_dict, None). `empty_sort_dir_ok` is search.messages' half of the rule on `_SORT_DIRS`."""
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err, None
    if err := _missing_argument(request, "query"):
        return err, None
    counts, err = _numbers(request, ("count", 20), ("page", 1), sort_dir_ok=empty_sort_dir_ok)
    if err is not None:
        return err, None
    query = _param(request, "query") or ""
    if isinstance(query, Undecodable):
        return _err("internal_error"), None
    # Absent is `invalid_arguments` above, and blank is `no_query` (`_QUERY_BLANKS`).
    if not query.strip(_QUERY_BLANKS):
        return _err("no_query"), None
    terms, container, phrase = _parse_slack_query(query)
    ids = auth.visible_ids(request, caller)  # results are scoped to the caller's ACL
    count = counts["count"]
    page = max(1, counts["page"])
    offset = (page - 1) * count
    # Honor Slack's sort: "score" (default) = relevance; "timestamp" = by message time. sort_dir
    # defaults to desc (newest first). Previously Backlot always ranked by relevance regardless.
    sort = (_param(request, "sort") or "score").lower()
    sort_dir = _param(request, "sort_dir") or "desc"
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
    query, block = _messages_block(request, empty_sort_dir_ok=True)
    if block is None:
        return query  # error dict
    return {"ok": True, "query": query, "messages": block}


@router.api_route(
    "/search.files",
    methods=["GET", "POST"],
    response_model=SlackFileSearch,
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
    counts, err = _numbers(request, ("count", 20), ("page", 1), sort_dir_ok=False)
    if err is not None:
        return err
    query = _param(request, "query") or ""
    if isinstance(query, Undecodable):
        return _err("internal_error")
    if not query.strip(_QUERY_BLANKS):
        return _err("no_query")
    count = counts["count"]
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
    query, block = _messages_block(request, empty_sort_dir_ok=False)
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


# --- writes ---------------------------------------------------------------------------------
#
# Every method here answers BOTH verbs. Measured against slack.com on 2026-09-06 with a read-only
# token: each answers `missing_scope` -- not `unknown_method` -- over GET and POST alike, so the
# method and the verb are recognised and the request reaches the scope check.
#
# Backlot models no OAuth scopes, so wherever real Slack splits an answer on one it takes the
# permissive branch. `chat.postMessage` into a public channel the caller has not joined is the
# case that matters: real Slack answers `not_in_channel` without `chat:write.public`. The
# divergence is an acknowledged entry in the Slack fidelity baseline rather than a silent one.


#: The earliest second a `thread_ts` may name: 1990-01-01T00:00:00Z. Measured against slack.com
#: on 2026-10-22 — `631152000.000001` is refused and `631152001.000000` posts.
_THREAD_TS_FLOOR = 631152000
#: How far ahead of the request one may name. Bisected the same day: 3,649 s past the clock read
#: when the bisection started posted, 3,650 s was refused, the bisection taking about 30 s.
_THREAD_TS_AHEAD = 3600


def _thread_ts_error(wanted) -> JSONResponse | None:
    """`invalid_thread_ts` for a `thread_ts` outside the window real accepts, else None.

    `_slack_ts` has already read the value the way real reads it (`1e9` is 19 seconds, `x5` is
    5, `1.2.3` is 1,000,002 µs). What real adds is bounds: the seconds have to be above
    :data:`_THREAD_TS_FLOOR` and no more than :data:`_THREAD_TS_AHEAD` past the request. A value
    inside the window that names no message is not refused — it posts top-level.

    The message carries the value as real read it, in MICROSECONDS: `abc` is
    `0 is not a valid thread_ts`, `-1` is `1000000 is not`, `1e9` is `19000000 is not`.
    """
    if wanted is None:
        return None
    micros = 0 if wanted is False else int(float(wanted) * 1_000_000 + 0.5)
    seconds = micros // 1_000_000
    if _THREAD_TS_FLOOR < seconds <= int(time.time()) + _THREAD_TS_AHEAD:
        return None
    return JSONResponse(
        {
            "ok": False,
            "error": "invalid_thread_ts",
            "response_metadata": {"messages": [f"[ERROR] {micros} is not a valid thread_ts"]},
        }
    )


def _blocks_error(request: Request) -> JSONResponse | None:
    """`invalid_blocks_format` for a `blocks` that is not a JSON array.

    Measured against slack.com on 2026-10-22: `blocks=abc`, `blocks={}` and an empty `blocks`
    are each refused, with or without a `text` beside them, where `blocks=[]` is read as an
    empty list. Only `blocks` has the format error — an `attachments` of `abc` or of an object
    answers `no_text`, so a malformed one is simply a value carrying no message.
    """
    raw = _param(request, "blocks")
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return _err("invalid_blocks_format")
    return None if isinstance(parsed, list) else _err("invalid_blocks_format")


def _json_array(request: Request, name: str) -> list:
    """The argument read as a JSON array, or empty for anything else."""
    raw = _param(request, name)
    if raw is None:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


#: The `attachments` keys that make a member carry the message by being there at all. Measured
#: against slack.com on 2026-10-23 with no `text`, twenty values on `chat.postMessage` and the
#: same on `chat.update`: each of these posts with an empty value as with a filled one
#: (`"pretext": ""` and `"fields": []` post), where `footer`, `color`, `author_link`,
#: `title_link`, `image_url`, `mrkdwn_in` and `ts` answer `no_text`.
_ATTACHMENT_TEXT_KEYS = ("text", "fallback", "title", "pretext", "author_name", "fields")


def _attachment_carries_text(a: object) -> bool:
    """Whether one `attachments` member is a message rather than decoration.

    Its own `blocks` count the way the top-level argument does and not the way its siblings do:
    only a NON-EMPTY array, where `[]`, `"abc"` and `{}` are each `no_text`.
    """
    if not isinstance(a, dict):
        return False
    if any(k in a for k in _ATTACHMENT_TEXT_KEYS):
        return True
    blocks = a.get("blocks")
    return isinstance(blocks, list) and bool(blocks)


def _has_content(request: Request) -> bool:
    """Whether `blocks` or `attachments` carries the message in place of `text`.

    For `blocks` that is a non-empty JSON array, and only that. For `attachments` it is an array
    with a member :func:`_attachment_carries_text` accepts, so `[]`, `[{}]`, a member carrying
    decoration alone, and anything that is not an array of objects -- `5`, `true`, `"text"`,
    `["text"]`, `[["text"]]`, `null` -- all leave the message textless. `chat.postMessage` and
    `chat.update` answered identically on every value measured.
    """
    if _json_array(request, "blocks"):
        return True
    return any(_attachment_carries_text(a) for a in _json_array(request, "attachments"))


def _writer(caller: Caller) -> str:
    """The corpus address a write is attributed to.

    The service token has no corpus email -- `_is_member` says outright that it "is not a person"
    -- and `auth.test` already answers it as ``USERVICE0`` by analogy to a bot token, on the
    grounds that "real Slack answers a bot token with the app's own id rather than a user's".

    What it does NOT carry is a subtype. Measured against slack.com on 2026-10-22 with an app's
    bot token: the echo and the history both answered `app_id`, `blocks`, `bot_id`,
    `bot_profile`, `team`, `text`, `ts`, `type` and the bot user's `user`, and no `subtype`. A
    `bot_message` here would also drop `blocks`, which real serves.
    """
    return SERVICE_EMAIL if caller.is_admin else caller.email


def _writable_channel(request: Request, conn, caller: Caller, *, by_name: bool = False):
    """``(channel_name, None)`` for a channel this caller may write to, else ``(None, error)``.

    `channel_not_found` for one they cannot see, which is Slack's own non-leaking answer: a caller
    outside a private channel is told it does not exist rather than that they lack permission, so
    the error cannot be used to enumerate channels.

    `by_name` also reads the argument as a channel's name, with or without a leading `#`. It is a
    property of the method, not of the argument: measured against slack.com on 2026-10-21 in one
    workspace, `chat.postMessage` posted for the id, the name and `#name` alike, where
    `chat.update` and `reactions.add` answered `channel_not_found` for the name and took the id
    alone. A name nothing has is `channel_not_found`, as an id nothing has is.
    """
    raw = _param(request, "channel") or ""
    name = _channel_name(conn, raw)
    if name is None and by_name:
        wanted = raw[1:] if raw.startswith("#") else raw
        name = wanted if wanted in _channel_names(conn) else None
    ids = auth.visible_ids(request, caller)
    if name is None or not _channel_visibility(request, conn, ids)(name):
        return None, _err("channel_not_found")
    return name, None


@router.api_route(
    "/chat.postMessage", methods=["GET", "POST"], openapi_extra={"parameters": _P_POST_MESSAGE}
)
async def chat_post_message(request: Request):
    """Post a message to a channel.

    Errors quoted from `slack_web_openapi_v2.json`,
    ``paths./chat.postMessage.post.responses.default``: `channel_not_found`, `not_in_channel`,
    `is_archived`, `msg_too_long`, `no_text`, `too_many_attachments`. Backlot answers
    `channel_not_found` and `no_text`; `not_in_channel` needs a scope model, `is_archived` needs a
    channel that can be archived, and `too_many_attachments` is a limit no corpus states.

    `msg_too_long` is none of those: docs.slack.dev puts the number in the platform's own docs
    ("Slack will truncate messages containing more than 40,000 characters"), so it is reproducible
    where a workspace setting is not. Backlot does not truncate yet, and a 50,000-character text
    posts and reads back whole.

    When `no_text` fires is measured, not chosen: against slack.com on 2026-10-21, `text`
    absent and `text=` both answer `no_text`, where `channel` absent answers `invalid_arguments`
    and `channel=` `channel_not_found`. A `text` of spaces posts, so the test is emptiness rather
    than blankness. `chat.update` reads `ts` by `channel`'s rules.

    `blocks` and `attachments` are READ, not merely looked for, and each by its own rule --
    see :func:`_has_content`. Measured with no `text`: a `blocks` naming one section posts and
    `blocks=[]` answers `no_text`, where a `blocks` that is not a JSON array -- `abc`, `{}`,
    empty -- answers `invalid_blocks_format`, which it does even beside a `text`. An
    `attachments` array posts when one of its members carries a message -- see
    :func:`_attachment_carries_text` for the keys that say so -- and answers `no_text`
    otherwise, a member carrying only decoration included; it has no format error of its own,
    so a value that is not an array of objects is `no_text` rather than a refusal.
    `chat.update` reads `text`, `blocks` and `attachments` by these same rules, every value
    re-measured against it.

    Posting does not join a public channel the caller is not in, and nothing here has to arrange
    that: membership there is derived from `main.slack_messages`, which a written row never joins.

    `thread_ts` posts the message as a reply, under the ROOT of the thread it names, so replying
    to a reply lands in the same thread. A value that names no message posts the message
    TOP-LEVEL and carries no `thread_ts` back, as an empty one does. What is refused is a value
    outside the window real accepts — see :func:`_thread_ts_error`.

    `blocks` and `attachments` decide whether the message has content, and nothing more: the
    `blocks` the message reads back with are composed from its `text` (`synth.slack_blocks`), as
    a corpus message's are, rather than being the ones the caller sent. Arguments the vendor
    documents and Backlot ignores outright: `as_user`, `username`, `icon_emoji`, `icon_url`,
    `reply_broadcast`, `unfurl_links`, `unfurl_media`, `parse`, `link_names`, `mrkdwn`.
    `backlot diff` cannot see them -- it compares query parameters and Slack's spec declares
    these as form fields (see `backlot/fidelity/operations.py`) -- so this list is the record
    until that gap is closed.
    """
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    if err := _missing_argument(request, "channel"):
        return err
    name, err = _writable_channel(request, conn, caller, by_name=True)
    if err is not None:
        return err
    if err := _blocks_error(request):
        return err
    if not (_param(request, "text") or "") and not _has_content(request):
        return _err("no_text")
    text = _param(request, "text") or ""
    author = _writer(caller)
    ids = auth.visible_ids(request, caller)
    thread_ts, thread_seq = None, 0
    wanted = _slack_ts(_param(request, "thread_ts"))
    if err := _thread_ts_error(wanted):
        return err
    parent = (
        store.document_by_key(conn, "slack", (name, wanted), ids) if wanted is not None else None
    )
    if parent is not None:
        # Slack threads a reply under the ROOT, not under whichever message was named: replying to
        # a reply puts it in the same thread. The corpus stores that as the root's own ts, which is
        # what `thread_ts` holds on every row of a thread, the root included.
        thread_ts = parent["thread_ts"] or parent["ts"]
        thread_seq = store.slack_next_thread_seq(conn, name, thread_ts)
        if not parent["thread_ts"]:
            # The first reply turns a standalone message into a root, which the corpus spells as
            # the message carrying its own ts. Without the stamp the parent stays NULL, and then
            # `conversations.replies` answers the root alone and the message reads back with no
            # thread at all — a thread this API made and cannot show.
            store.patch_document(conn, "slack", (name, parent["ts"]), "thread_ts", thread_ts)
    now = int(time.time())
    ts = store.slack_next_ts(conn, name, now)
    store.insert_document(
        conn,
        "slack",
        {
            "channel": name,
            "ts": ts,
            "author_email": author,
            "content": text,
            "created_ts": now,
            "thread_ts": thread_ts,
            "thread_seq": thread_seq,
            "subtype": None,
        },
    )
    row = store.document_by_key(conn, "slack", (name, ts))
    return {
        "ok": True,
        "channel": synth.slack_channel_id(name),
        "ts": ts,
        "message": _message(row),
    }


@router.api_route(
    "/chat.postEphemeral", methods=["GET", "POST"], openapi_extra={"parameters": _P_POST_EPHEMERAL}
)
async def chat_post_ephemeral(request: Request):
    """A message only one user sees.

    Nothing is stored: an ephemeral message is not in the channel's history in real Slack either,
    so there is no state for a corpus to hold and nothing for a later read to disagree with.

    Errors quoted from the spec's ``default`` response: `channel_not_found`, `is_archived`,
    `msg_too_long`, `no_text`, `restricted_action`, `too_many_attachments`, `user_not_in_channel`.
    Backlot answers `channel_not_found` and `user_not_in_channel`.

    `text` is not required here, where `chat.postMessage` requires it: measured against
    slack.com on 2026-10-22, this method posts with `text` absent, empty or blank. Reading
    `text` by `chat.postMessage`'s rule is the mistake a shared name invites.

    `user` is read in two steps, both measured the same day. Its SHAPE first: a value that does
    not match ``^[UW][A-Z0-9]{2,}$`` — an empty one, a lower-cased id — is `invalid_arguments`
    carrying that pattern, ahead of anything about the channel. Then its membership: an id
    nobody has, and an active member who is not in the channel, are each `user_not_in_channel`.
    A missing `user` is named before a missing `channel`.
    """
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    if err := _missing_argument(request, "user", "channel"):
        return err
    uid = _param(request, "user") or ""
    if not _SLACK_USER_ID.fullmatch(uid):
        return JSONResponse(
            {
                "ok": False,
                "error": "invalid_arguments",
                "response_metadata": {
                    "messages": [
                        "[ERROR] input must match regex pattern: "
                        f"{_SLACK_USER_ID.pattern} [json-pointer:/user]"
                    ]
                },
            }
        )
    name, err = _writable_channel(request, conn, caller)
    if err is not None:
        return err
    # Two lookups because they cover different people, and neither is a superset.
    # `all_user_emails` has the principals, including one who holds an account and has never
    # posted: a private channel's grantee is a member whether or not they spoke.
    # `_slack_author_by_uid` has the message authors, including the service id and the
    # display-only speakers that hold no account (deploybot@...). Only a generated corpus has
    # those -- the BYO importer makes a principal of every author it reads -- so the fallback
    # is the one of the two no test here distinguishes.
    email = next(
        (e for e in store.all_user_emails(conn) if synth.slack_user_id(e) == uid), None
    ) or _slack_author_by_uid(request, conn, uid)
    if uid != SERVICE_USER_ID and not _in_channel(conn, name, email):
        return _err("user_not_in_channel")
    return {"ok": True, "message_ts": synth.slack_fmt_ts(int(time.time()), f"ephemeral:{name}")}


def _in_channel(conn, name: str, email: str | None) -> bool:
    """Whether this person is in the channel, for the one method that refuses on it.

    A PRIVATE channel's members are its grantees, which the corpus states outright. A public
    one's are its speakers, which a written message deliberately does not join — so a caller who
    has posted here but never spoke in the corpus would be refused their own ephemeral, and the
    public side answers yes for anyone the corpus knows instead. The private side is the half
    real was measured on.
    """
    if not email:
        return False
    members = store.slack_private_channel_members(conn, name)
    return email in members if members is not None else True


def _own_message(request: Request, conn, caller: Caller, ts_param: str, refusal: str):
    """``(channel_name, ts, row, None)`` for a message this caller wrote, else an error in the
    last slot.

    The three refusals are ordered the way Slack answers them, and the order is the point: a
    caller outside the channel is told `channel_not_found` before anything about the message is
    looked up, so the message-level errors cannot be used to probe a channel they cannot see.
    """
    name, err = _writable_channel(request, conn, caller)
    if err is not None:
        return None, None, None, err
    ts = _param(request, ts_param) or ""
    row = store.document_by_key(conn, "slack", (name, ts), auth.visible_ids(request, caller))
    if row is None or row["subtype"] == "tombstone":
        # The placeholder a deleted root leaves is served but is no longer a message anyone can
        # address: measured 2026-10-22, `chat.update` and `chat.delete` on it answer
        # `message_not_found`, where reacting to it and asking its permalink still answer.
        return None, None, None, _err("message_not_found")
    author = _writer(caller)
    # Case-folded, the way `_subscribed` compares the same addresses: a corpus states an author's
    # address however it was written, and a mixed-case one could not edit their own message.
    if (row["author_email"] or "").lower() != (author or "").lower():
        return None, None, None, _err(refusal)
    return name, ts, row, None


@router.api_route("/chat.update", methods=["GET", "POST"], openapi_extra={"parameters": _P_UPDATE})
async def chat_update(request: Request):
    """Edit a message's text.

    Errors quoted from `slack_web_openapi_v2.json`,
    ``paths./chat.update.post.responses.default``: `message_not_found`, `cant_update_message`,
    `channel_not_found`, `edit_window_closed`, `msg_too_long`, `too_many_attachments`, `no_text`,
    `is_inactive`. Backlot answers the first three and `no_text`; `edit_window_closed` is a
    workspace retention setting no corpus states, and the rest are limits it does not model.

    The condition is the vendor's own: docs.slack.dev's error table for chat.update gives
    `cant_update_message` as "Authenticated user does not have permission to update this message."
    The admin/service token is refused under it too — bypassing the ACL is about what a caller may
    SEE, and authorship is a different question it does not answer.
    """
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    if err := _missing_argument(request, "channel", "ts"):
        return err
    name, ts, row, err = _own_message(request, conn, caller, "ts", "cant_update_message")
    if err is not None:
        return err
    if err := _blocks_error(request):
        return err
    text = _param(request, "text") or ""
    if not text and not _has_content(request):
        return _err("no_text")
    author = _writer(caller)
    store.patch_document(conn, "slack", (name, ts), "content", text)
    # Real Slack stamps an edited message with `edited: {user, ts}`, which is what a client renders
    # "(edited)" from. `_message` already serves the column, so filling it is the whole of it.
    #
    # The ADDRESS is stored, not the id: `_edited` renders one from the other when it serves the
    # message, so an id here would be rendered a second time — the same representation a corpus
    # states and `reactions.users` keeps.
    store.patch_document(
        conn,
        "slack",
        (name, ts),
        "edited",
        json.dumps(
            {"user": author, "ts": synth.slack_fmt_ts(int(time.time()), f"edit:{name}:{ts}")}
        ),
    )
    updated = store.document_by_key(conn, "slack", (name, ts))
    # The message an edit echoes is not the message a read answers: measured against slack.com on
    # 2026-10-21, `chat.update` carried neither `ts` nor `reactions` for a threaded message with a
    # reaction, where `conversations.replies` answered the same message carrying both. `ts` is on
    # the answer beside it either way.
    echoed = {k: v for k, v in _message(updated).items() if k not in ("ts", "reactions")}
    return {
        "ok": True,
        "channel": synth.slack_channel_id(name),
        "ts": ts,
        "text": text,
        "message": echoed,
    }


@router.api_route("/chat.delete", methods=["GET", "POST"], openapi_extra={"parameters": _P_DELETE})
async def chat_delete(request: Request):
    """Delete a message.

    Errors quoted from the spec's ``default`` response: `message_not_found`, `channel_not_found`,
    `cant_delete_message`, `compliance_exports_prevent_deletion`. The last is a workspace
    compliance setting no corpus states. `cant_delete_message` carries the vendor's own condition:
    docs.slack.dev gives it as "Authenticated user does not have permission to delete this
    message."

    A CORPUS message can be deleted. The row stays in the read-only database and is subtracted at
    read time by the overlay's tombstone, which is the only way this can work over a corpus that
    is never written.

    A thread ROOT with replies still under it is not subtracted: real leaves it as a placeholder
    so the thread stays readable. Measured against slack.com on 2026-10-21 — a deleted root came
    back from `conversations.history` and `conversations.replies` as
    ``subtype: "tombstone"``, ``text: "This message was deleted."``, ``hidden: true``, keeping its
    `thread_ts`, `reply_count`, `reply_users` and `latest_reply`, with both replies intact beside
    it; a deleted STANDALONE message came back from neither; and deleting the last reply took the
    placeholder with it.
    """
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    if err := _missing_argument(request, "channel", "ts"):
        return err
    name, ts, _row, err = _own_message(request, conn, caller, "ts", "cant_delete_message")
    if err is not None:
        return err
    ids = auth.visible_ids(request, caller)
    if _row["thread_seq"] == 0 and store.slack_reply_count(conn, name, ts, ids) > 0:
        store.patch_document(conn, "slack", (name, ts), "content", TOMBSTONE_TEXT)
        store.patch_document(conn, "slack", (name, ts), "subtype", "tombstone")
        # Served, but found by nothing: real answers no search hit for the placeholder's words
        # or for the message's own.
        store.blank_index(conn, "slack", (name, ts))
    else:
        store.tombstone_document(conn, "slack", (name, ts))
        _unthread_a_spent_root(conn, name, _row, ids)
    return {"ok": True, "channel": synth.slack_channel_id(name), "ts": ts}


def _unthread_a_spent_root(conn, channel: str, deleted, ids) -> None:
    """What a thread's root becomes once ``deleted`` was its last reply.

    Measured against slack.com on 2026-10-22: a placeholder goes with the last reply under it,
    and a live root goes back to being a standalone message — no `thread_ts` on it in history,
    and `chat.getPermalink` answers it without the thread query.
    """
    root_ts = deleted["thread_ts"]
    if not root_ts or deleted["thread_seq"] == 0:
        return
    root = store.document_by_key(conn, "slack", (channel, root_ts))
    if root is None or store.slack_reply_count(conn, channel, root_ts, ids) > 0:
        return
    if root["subtype"] == "tombstone":
        store.tombstone_document(conn, "slack", (channel, root_ts))
    else:
        store.patch_document(conn, "slack", (channel, root_ts), "thread_ts", None)


@router.api_route(
    "/chat.getPermalink", methods=["GET", "POST"], openapi_extra={"parameters": _P_PERMALINK}
)
async def chat_get_permalink(request: Request):
    """A message's archive URL.

    Errors quoted from the spec's ``default`` response: `channel_not_found`, `message_not_found`.

    The link itself is `_permalink`'s, which also names the thread a threaded message is in.
    """
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    if err := _missing_argument(request, "channel", "message_ts"):
        return err
    name, err = _writable_channel(request, conn, caller)
    if err is not None:
        return err
    ts = _param(request, "message_ts") or ""
    row = store.document_by_key(conn, "slack", (name, ts), auth.visible_ids(request, caller))
    if row is None:
        return _err("message_not_found")
    return {
        "ok": True,
        "channel": synth.slack_channel_id(name),
        "permalink": _permalink(name, row),
    }


def _permalink(channel_name: str, row) -> str:
    """A message's archive URL.

    The path is Slack's own shape -- ``/archives/<channel id>/p<ts with the dot removed>`` -- so
    a permalink Backlot mints parses the way a real one does. A message in a THREAD also carries
    ``?thread_ts=<root>&cid=<channel id>``, which is how a client opens the thread rather than
    the channel: measured against slack.com on 2026-10-21, a thread root and a reply both carried
    the query and a standalone message carried none.
    """
    cid = synth.slack_channel_id(channel_name)
    link = (
        f"https://{get_settings().org_name}.slack.com/archives/{cid}/p{row['ts'].replace('.', '')}"
    )
    if row["thread_ts"]:
        link += f"?thread_ts={row['thread_ts']}&cid={cid}"
    return link


def _reacted_by(conn, address: str) -> list[tuple[str, str]]:
    """``(channel, ts)`` of every message this person has reacted to, most recent first.

    Two sources, because a reaction can come from either: the overlay's `reactions` patches,
    ordered by the rowid their last write gave them, and then the corpus's own column. The
    corpus side is prefiltered in SQL on the address appearing in the stored JSON, so the scan
    is one pass over a column rather than a decode of every message.

    Two ways this is not real's order, both measured 2026-10-22 and both wanting a per-reaction
    time the corpus does not carry. Real orders by when THIS person reacted, where a patch's
    rowid moves whenever anyone reacts to that message; and real lists one item per REACTION, so
    a message the caller put two emoji on appears twice. Recorded rather than invented: a corpus
    states a message's reactions as a set with no times in it.
    """
    patch = overlay.table_names("slack")["patch"]
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in conn.execute(
        f"SELECT channel, ts, value FROM ov.{patch} WHERE field = 'reactions' ORDER BY rowid DESC"
    ):
        try:
            reactions = json.loads(row["value"] or "[]")
        except ValueError:
            continue
        key = (row["channel"], row["ts"])
        seen.add(key)
        if any(_has_reacted(r, address) for r in reactions):
            out.append(key)
    like = f'%"{address}"%'
    for row in conn.execute(
        f"SELECT channel, ts, reactions FROM {store.merged_source(conn, 'slack')} "
        "WHERE reactions IS NOT NULL AND reactions LIKE ? ORDER BY created_ts DESC",
        (like,),
    ):
        key = (row["channel"], row["ts"])
        if key in seen:  # the overlay already answered for this message
            continue
        try:
            reactions = json.loads(row["reactions"] or "[]")
        except ValueError:
            continue
        if any(_has_reacted(r, address) for r in reactions):
            out.append(key)
    return out


def _same_address(a: str, b: str) -> bool:
    """Whether two stored reactor addresses are the same person.

    Case-folded, the way `_subscribed` and the authorship check compare the same addresses: a
    corpus states an address however it was written.
    """
    return (a or "").lower() == (b or "").lower()


def _has_reacted(reaction: dict, address: str) -> bool:
    return any(_same_address(u, address) for u in reaction.get("users") or [])


class _AlreadyReacted(Exception):
    """The caller already left this reaction. Raised from inside the edit callback so the write is
    abandoned with the overlay lock still held, rather than decided on a value read before it."""


class _NoReaction(Exception):
    """The caller has no such reaction to remove. See :class:`_AlreadyReacted`."""


#: The `timestamp` values real refuses rather than reads. Measured on `reactions.get`
#: 2026-10-23: of `0`, `0.0`, `00`, `-1`, `1.5`, `x5`, `abc`, two spaces, `1e9`, `inf` and
#: `0.000001`, only `0` answers `bad_timestamp` — every other value is read as a ts that names
#: no message. An absent or empty one is refused with it.
_BAD_TIMESTAMPS = ("", "0")


def _reaction_target(
    request: Request, conn, caller: Caller, *, item_forms: bool, channel_first: bool
):
    """``(channel_name, ts, row, None)`` for a reaction's message, else an error in the last slot.

    Slack addresses a reaction target three ways — `channel` + `timestamp`, `file`, or
    `file_comment`. Backlot has no file corpus (see search.files), so the two file forms name
    nothing and are answered `file_not_found` and `file_comment_not_found`. Naming one wins over
    a `channel` and `timestamp` beside it, and an empty `file=` is refused as a present one is.

    The two flags are the three methods disagreeing, each measured against slack.com on
    2026-10-23 with an emoji nothing has:

    - ``item_forms`` — `reactions.add` reads neither file argument: given all of `name`,
      `channel`, `timestamp` and `file` it answers `message_not_found` where the other two
      answer `file_not_found`. It is also the method that NAMES its missing arguments, so the
      `no_item_specified` an absent `channel` draws here is not its answer; `_missing_argument`
      has already spoken for it.
    - ``channel_first`` — `reactions.get` resolves the channel before reading the timestamp, and
      the other two read the timestamp first. A channel nothing has, with no timestamp, is
      `channel_not_found` on `reactions.get` and `bad_timestamp` on the other two.

    An absent `channel` is `no_item_specified` whatever the `timestamp` beside it says, so a
    `timestamp` alone draws it too.

    The channel is resolved before the message either way, so `channel_not_found` is the answer
    for a caller outside it whether or not the ts they named is real. A ts that resolved
    differently would let them confirm a message exists in a channel they cannot read.
    """

    def bad_timestamp():
        if (_param(request, "timestamp") or "") in _BAD_TIMESTAMPS:
            return _err("bad_timestamp")
        return None

    if item_forms:
        if _param(request, "file") is not None:
            return None, None, None, _err("file_not_found")
        if _param(request, "file_comment") is not None:
            return None, None, None, _err("file_comment_not_found")
        if _param(request, "channel") is None:
            return None, None, None, _err("no_item_specified")
    if not channel_first and (err := bad_timestamp()):
        return None, None, None, err
    name, err = _writable_channel(request, conn, caller)
    if err is not None:
        return None, None, None, err
    if channel_first and (err := bad_timestamp()):
        return None, None, None, err
    ts = _param(request, "timestamp") or ""
    row = store.document_by_key(conn, "slack", (name, ts), auth.visible_ids(request, caller))
    if row is None:
        return None, None, None, _err("message_not_found")
    return name, ts, row, None


@router.api_route(
    "/reactions.add", methods=["GET", "POST"], openapi_extra={"parameters": _P_REACTION_ADD}
)
async def reactions_add(request: Request):
    """Add an emoji reaction to a message.

    Errors quoted from `slack_web_openapi_v2.json`,
    ``paths./reactions.add.post.responses.default``: `bad_timestamp`, `message_not_found`,
    `no_item_specified`, `invalid_name`, `already_reacted`, `too_many_emoji`,
    `too_many_reactions`. Backlot answers `message_not_found` and `invalid_name`;
    `no_item_specified` is in the enum but is not what real answers THIS method for a missing
    argument; it names them. The two ceilings are workspace limits no corpus states.
    `bad_timestamp` is answered for the values real refuses outright (see
    :data:`_BAD_TIMESTAMPS`); a `timestamp` real reads and finds nothing under is
    `message_not_found`.

    `reactions` is a column the corpus already carries and every message payload already serves,
    so this is a patch to a served field rather than a new entity. The patch REPLACES the column,
    so the existing value is read and put back — a corpus message that already carries reactions
    keeps them.

    What is stored is the reactor's ADDRESS, which is what a corpus states and what `_reactions`
    hashes into a Slack id when it serves the message. Storing the id instead would be hashed a
    second time. No `count` is stored either: `_reactions` derives it from the list, and the record
    schema refuses a stated one for the same reason — a second place for it to be wrong.
    """
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    # `reactions.add` NAMES the arguments it is missing, where `reactions.remove` and
    # `reactions.get` answer `no_item_specified` for the same request: measured 2026-10-22, an
    # empty call lists `name`, `timestamp`, `channel` in that order, `channel` alone names
    # `timestamp`, and `timestamp` alone names `channel`. The rule is this method's, so it is
    # applied here rather than in the helper the three share.
    if err := _missing_argument(request, "name", "timestamp", "channel"):
        return err
    name, ts, row, err = _reaction_target(
        request, conn, caller, item_forms=False, channel_first=False
    )
    if err is not None:
        return err
    # Not stripped: measured 2026-10-22, ` eyes ` is `invalid_name` on real where stripping it
    # reacts with `eyes`. `:eyes:` and `EYES` are refused there too, which needs the emoji set
    # Backlot does not have and stays a recorded gap.
    emoji = _param(request, "name") or ""
    if not emoji:
        return _err("invalid_name")
    author = _writer(caller)

    def add(fresh):
        # Re-read inside the lock: `row` above was read before it, and two callers reacting at
        # once would otherwise each write back a list missing the other's user.
        current = store.jcol(fresh, "reactions")
        for r in current:
            if r["name"] != emoji:
                continue
            if _has_reacted(r, author):
                raise _AlreadyReacted
            r["users"] = [*r.get("users", []), author]
            break
        else:
            current.append({"name": emoji, "users": [author]})
        return json.dumps(current)

    try:
        store.edit_document(
            conn, "slack", (name, ts), "reactions", add, auth.visible_ids(request, caller)
        )
    except _AlreadyReacted:
        return _err("already_reacted")
    return {"ok": True}


@router.api_route(
    "/reactions.remove", methods=["GET", "POST"], openapi_extra={"parameters": _P_REACTION}
)
async def reactions_remove(request: Request):
    """Remove one of the caller's own reactions.

    Errors quoted from the spec's ``default`` response: `bad_timestamp`, `file_not_found`,
    `file_comment_not_found`, `message_not_found`, `no_item_specified`, `invalid_name`,
    `no_reaction`.

    A reaction nobody is left on is dropped from the column entirely rather than kept at count 0,
    so a message returns to the shape `_message` gives one nobody reacted to.
    """
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    if err := _missing_argument(request, "name"):
        return err
    name, ts, row, err = _reaction_target(
        request, conn, caller, item_forms=True, channel_first=False
    )
    if err is not None:
        return err
    emoji = _param(request, "name") or ""
    author = _writer(caller)

    def drop(fresh):
        current = store.jcol(fresh, "reactions")
        for r in current:
            if r["name"] == emoji and _has_reacted(r, author):
                r["users"] = [u for u in r.get("users", []) if not _same_address(u, author)]
                break
        else:
            raise _NoReaction
        return json.dumps([r for r in current if r.get("users")])

    try:
        store.edit_document(
            conn, "slack", (name, ts), "reactions", drop, auth.visible_ids(request, caller)
        )
    except _NoReaction:
        return _err("no_reaction")
    return {"ok": True}


@router.api_route(
    "/reactions.get", methods=["GET", "POST"], openapi_extra={"parameters": _P_REACTION_GET}
)
async def reactions_get(request: Request):
    """A message with its reactions.

    Errors quoted from the spec's ``default`` response: `bad_timestamp`, `file_not_found`,
    `file_comment_not_found`, `message_not_found`, `no_item_specified`.

    The message is built by `_message`, the same builder conversations.history uses, so a client
    that reads a reaction here and the message there cannot get two shapes for one message.
    """
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    name, _ts, row, err = _reaction_target(
        request, conn, caller, item_forms=True, channel_first=True
    )
    if err is not None:
        return err
    return {
        "ok": True,
        "type": "message",
        "channel": synth.slack_channel_id(name),
        # `permalink` rides on the message here and on `reactions.list`, and on no other method
        # that answers one: measured against slack.com on 2026-10-21, `conversations.history` and
        # `conversations.replies` carry none. So it goes on at these two call sites rather than
        # in `_message`, which all of them share.
        "message": _message(row) | {"permalink": _permalink(name, row)},
    }


@router.api_route(
    "/reactions.list", methods=["GET", "POST"], openapi_extra={"parameters": _P_REACTION_LIST}
)
async def reactions_list(request: Request):
    """Items the caller has reacted to.

    Errors quoted from the spec's ``default`` response: `user_not_found`.

    A reaction the CORPUS states is listed too: measured against slack.com on 2026-10-22, 10 of
    the 13 items real answered were reactions left long before the probe. Only reactions this
    API wrote would answer a fresh server with an empty list where real answers a history.

    The most recent is first. Real listed Y, Z, X for reactions left to X, then Z, then Y, so an
    overlay patch is ordered by the rowid its last write gave it (`INSERT OR REPLACE` moves it),
    and the corpus's own come after, in the order the corpus states.

    `user` names whose reactions to list, and an id nobody has is `user_not_found`.

    Measured 2026-10-21: the page is `limit` items long and carries `response_metadata`, with no
    `paging` object. `count` and `page` are read and refused by the same rule but change neither
    the page nor its size — `count=1` and `count=2` each answered all 100 of the default page,
    where `limit=2` answered two. Reading an argument is not honouring it: real refuses
    `page=abc` whatever it does with `page=2`.

    `count`, `page` and `limit` are refused in that order whichever was sent first, and ahead of
    both an undecodable `cursor` and an unknown `user`. `limit` takes the integer rule here, so
    `1.5` is refused where a paged read method would serve one item, and a `limit` at or below
    zero is the default page rather than every item: `0`, `00`, `-1` and `-2` each answered the
    same 100 items and the same cursor as no `limit`, where `1000` answered all 247 and an empty
    cursor. `full` is a recorded gap.
    """
    conn = auth.conn(request)
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    counts, err = _numbers(
        request,
        ("count", 100),
        ("page", 1),
        ("limit", get_settings().default_page_size, False),
    )
    if err is not None:
        return err
    offset = decode_cursor_or_none(_param(request, "cursor"))
    if offset is None:
        return _err("invalid_cursor")
    author = _writer(caller)
    wanted = _param(request, "user")
    if wanted is not None:
        # Read as SENT, not as absent-when-empty: measured 2026-10-22, `user=` is
        # `user_not_found` on real, as a lower-cased id is.
        author = next(
            (e for e in store.all_user_emails(conn) if synth.slack_user_id(e) == wanted), None
        ) or (SERVICE_EMAIL if wanted == SERVICE_USER_ID else None)
        if author is None:
            return _err("user_not_found")
    ids = auth.visible_ids(request, caller)
    limit = counts["limit"] if counts["limit"] > 0 else get_settings().default_page_size
    items = []
    # One document fetched per item SERVED, plus one to see whether a cursor is wanted — not one
    # per message the caller has ever reacted to. A caller with 2,000 of them paid 90 ms a call
    # to build a list of which twenty were then kept.
    for channel, ts in _reacted_by(conn, author):
        if len(items) > offset + limit:
            break
        row = store.document_by_key(conn, "slack", (channel, ts), ids)
        if row is None:  # deleted, or no longer visible to this caller
            continue
        items.append(
            {
                "type": "message",
                "channel": synth.slack_channel_id(channel),
                "message": _message(row) | {"permalink": _permalink(channel, row)},
            }
        )
    page = items[offset : offset + limit]
    # `len(items)` is the page plus at most one, which is all `next_cursor` needs to know.
    cursor = next_cursor(offset, len(page), offset + len(items[offset:]))
    return {"ok": True, "items": page, "response_metadata": {"next_cursor": cursor}}


# --- helpers --------------------------------------------------------------------


def _search_match(conn, row) -> dict:
    """A search.messages `matches[]` entry for a slack row.

    `username` is the author's handle, the same string `users.list` gives that person as `name` —
    the method's own reference spells it that way, its success example carrying
    `"username": "roach"` beside `"user": "U2U85N1RV"`
    (`docs.slack.dev/reference/methods/search.messages`, read 2026-09-22). It goes through
    `_handle` for that reason: a hit and a user object name one person, and a client that reads
    the id off `user` and the handle off `username` must not get two spellings of them.
    """
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
        "user": _uid(row["author_email"]),
        "username": _handle(row["author_email"]),
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
    finished, count this one channel directly rather than blocking on all of them.

    A MISSING key is counted, not answered as zero. `slack_channel_member_counts` keys every
    channel, including one with no messages at all, so a key is absent only while the cache is
    still being built; reading an absent key as zero would answer `num_members: 0` for a channel
    that has members.

    Nothing invalidates this cache, because no write moves what it holds: a public channel's
    members are the speakers in `main.slack_messages`, which a written row never joins, and a
    private channel's are the grantees a written row's own grants were copied from. Measured
    both ways, over a post and over a delete, on a public channel and a private one.
    """
    cache = getattr(request.app.state, "channel_members", None)
    if cache is not None and name in cache:
        return cache[name]
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


def _is_written(row) -> bool:
    """Whether this row was written through the API rather than imported — `merged_source`'s
    `_written`, absent from the bare-table shape it returns while nothing has been written."""
    return "_written" in row.keys() and bool(row["_written"])


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
        "user": _uid(row["author_email"]),
        "text": text,
        "ts": row["ts"],
        "team": TEAM_ID,
    }
    if row["author_email"] == SERVICE_EMAIL or _is_written(row):
        # Real carries `bot_id` on every message posted through the Web API, over a user token
        # as well as a bot one, in history as well as in the echo (measured 2026-10-22) — the
        # app is what posted it either way. It is not a subtype that says so: a bot-token post
        # has none. A corpus message stands for one a Slack client posted and carries none.
        m["bot_id"] = BOT_ID
    if not row["subtype"]:
        # `client_msg_id` is minted by the CLIENT that posted, and `blocks` is what that client
        # composed, so neither belongs on a message Slack itself generated — measured: a
        # channel_join carries only type/user/text/ts/subtype. `team` is left on both: it is
        # absent from a channel_join too, but a posted message is a real one rather than a
        # system notice and there was none to measure, so dropping it everywhere would generalise
        # past the evidence.
        #
        # `blocks` stays on a message posted through this API: measured against slack.com on
        # 2026-10-22, a plain-text post over a user token and over a bot token, in the echo and
        # in history, carried `blocks`. Only `client_msg_id` turns on where the row came from —
        # a post over the Web API carries none, and a corpus message stands for one a Slack
        # client posted, so it keeps its.
        #
        # Seeded on (channel, ts), not ts alone: a ts is unique within its channel (see
        # store.ID_COLUMNS), so the same second in two channels produced one client_msg_id — a
        # value real Slack makes globally unique.
        if not _is_written(row):
            m["client_msg_id"] = synth.slack_client_msg_id(seed)
        blocks = synth.slack_blocks(text, seed)
        if blocks:
            m["blocks"] = blocks
    reactions = _reactions(row)
    if reactions:
        m["reactions"] = reactions
    files = _files(row)
    if files:
        m["files"] = files
    edited = store.jcol(row, "edited", {})
    if edited:
        m["edited"] = _edited(edited)
    if row["subtype"]:
        m["subtype"] = row["subtype"]
    if row["subtype"] == "tombstone":
        # The placeholder a deleted root leaves is not attributed to the poster: measured
        # 2026-10-22, real answers `user` and `parent_user_id` as `USLACKBOT`, carries `hidden`,
        # and carries none of the author's own fields.
        m["hidden"] = True
        m["user"] = SLACKBOT_ID
        m["parent_user_id"] = SLACKBOT_ID
        m.pop("team", None)
        m.pop("bot_id", None)
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


def _reactions(row) -> list[dict]:
    """A message's ``reactions``, built from the addresses the corpus stated.

    Not pass-through, and the shape Slack answers with is why. `reactions.get`'s own success
    example carries `name`, `users` and `count` on every reaction, and each entry of `users` is a
    user id (`"users": ["W222222"]`) — which is `synth.slack_user_id` of a person, a value no
    corpus author can know. Writing it by hand meant inventing ids for people the corpus already
    names by address everywhere else.

    `count` is DERIVED rather than stated, and NOT because a mismatch would be unreal: real Slack
    sends one larger than `users` whenever it truncates, which `reactions.get` documents as `users`
    "might not always contain all users that have reacted" while `count` "will always represent the
    count of all users who made that reaction". Backlot truncates nothing, so every reactor the
    corpus names is rendered and `len(users)` IS that count; stating it in a record would add a
    second place for it to be wrong. The schema refuses one, and a REPEATED address too, since
    `reactions.add` answers a repeat with `already_reacted` and deriving from one would render a
    single id under a count of two.

    The address is not required to belong to anyone the corpus places. `users.info` resolves a
    PRINCIPAL's id first and falls back to a message author's, so a reactor who is neither is served
    as an id that resolves to nobody — the same limitation `users.list` already carries for
    display-only speakers. Being a principal is enough on its own: a reactor who never posted here
    still resolves, which is why the sample corpus's reactors need not be Slack authors.

    Field order is Slack's own: the `reactions.get` example response shows
    `{"name": …, "users": [...], "count": N}`.
    """
    out = []
    for reaction in store.jcol(row, "reactions"):
        users = list(reaction.get("users") or [])
        out.append(
            {
                "name": reaction.get("name"),
                # `_uid`, not `synth.slack_user_id`: the service account is not a corpus address
                # and its id is fixed at USERVICE0, the same value `auth.test` and a message it
                # posted report. Hashing its sentinel would give a client two ids for one reactor.
                "users": [_uid(e) for e in users],
                "count": len(users),
            }
        )
    return out


def _files(row) -> list[dict]:
    """Renders `user` via `synth.slack_user_id`, the same lift `_reactions` gives
    `reactions.users` and `_edited` gives `edited.user` (see `backlot/schemas/slack.schema.json`
    for the vendor shape behind it). Every other field passes through unchanged."""
    out = []
    for f in store.jcol(row, "files"):
        f = dict(f)
        if "user" in f:
            f["user"] = synth.slack_user_id(f["user"])
        out.append(f)
    return out


def _edited(edited: dict) -> dict:
    """Renders `edited.user` from the address it is stored as, the same lift `_reactions` gives
    `reactions.users` (see `backlot/schemas/slack.schema.json` for the vendor shape behind it);
    `ts` passes through unchanged.

    Through `_uid` rather than `synth.slack_user_id`: an edit made by the service account is stored
    against a sentinel that is not a corpus address, and its id is fixed at USERVICE0.
    """
    return {"user": _uid(edited["user"]), "ts": edited["ts"]}


def _channel_name(conn, channel_id: str) -> str | None:
    for row in store.list_containers(conn, "slack"):
        if synth.slack_channel_id(row["name"]) == channel_id:
            return row["name"]
    return None


def _param(request: Request, key: str) -> str | None:
    """An argument's value: the body's (``read_post_body``), else the query string's. Real reads the
    body's when both send one (slack.com/api, 2026-10-20): on `api.test`, on `limit` on
    `conversations.list`, `users.list` and `conversations.history`, on `count` on `search.messages`
    and on `types` on `conversations.list`."""
    form = getattr(request.state, "_form", None)
    if form and key in form:
        return form[key]
    return request.query_params.get(key)


# How real reads the numbers a paged method takes, measured against slack.com/api from 2026-10-10
# to 2026-10-12 on each method that reads them. `count` and `page` take ASCII digits, a leading
# zero included, and a minus sign only before a digit other than zero (`-1` is read, `-0` and
# `-01` are refused), within the signed 64-bit range. `limit` takes a decimal with an optional
# fraction and exponent (`2.9`, `.5`, `2e0`, `20e-1`), but no `+` before it or before the
# exponent, no `2.` and no `1.e1`, and serves its integer part. One trailing newline is read on
# `limit` and on neither of the others. Anything else, padding, an empty value, `0x10`, `1_0` and
# digits outside ASCII included, is refused with one line naming the field. What a negative
# `limit` or one past 2**63 serves is outside this rule: real's answer to it differs by method.
_SLACK_INTEGER = re.compile(r"[0-9]+|-[1-9][0-9]*")
_SLACK_NUMBER = re.compile(r"-?(?:[0-9]+(?:[.][0-9]+)?|[.][0-9]+)(?:[eE]-?[0-9]+)?\n?")


def _slack_number(key: str, raw: str, *, decimal: bool | None = None) -> int | None:
    """The value of one numeric argument by the rule on `_SLACK_INTEGER`, or None where real
    refuses it. A decimal argument is its integer part (`1.5` is 1, `3.99999` is 3, `-1.5` is -1).
    One too large for a float is zero when it is positive (`1e400`), which `_limit` serves as no
    `limit`, as real served every item, and -2**63 when it is negative, which each method serves
    as it serves `-2`. An integer part at or past 2**63 wraps around 2**64, so
    `9223372036854775808` and `1e19` are negative and `1e300` is zero, and one below -2**63 stays
    negative: the reading that every row of the measurement on `_limit` agrees with.

    Which of the two rules a key takes is a property of the method, not of the name. `limit` is
    decimal on the paged read methods and an integer on `reactions.list`, where `1.5`, `2e0` and
    `1.e1` are each refused (measured 2026-10-21). `decimal` names the rule; left None it follows
    the name, which is what every paged method wants."""
    if key == "limit" if decimal is None else decimal:
        if not _SLACK_NUMBER.fullmatch(raw):
            return None
        if _SLACK_INTEGER.fullmatch(raw) and (n := _within_64_bits(raw)) is not None:
            return n
        x = float(raw)
        if math.isinf(x):
            return 0 if x > 0 else -(2**63)
        n = int(x)
        return (n + 2**63) % 2**64 - 2**63 if n >= 2**63 else n
    if not _SLACK_INTEGER.fullmatch(raw):
        return None
    return _within_64_bits(raw)


def _within_64_bits(raw: str) -> int | None:
    """``raw``, which `_SLACK_INTEGER` matches, as an int within the signed 64-bit range, or None.
    Its digits, its zeros in front dropped, are counted before `int` sees them, which raises past
    4300 of them; real refuses a `count` of 4301 nines as one past 2**63 and serves such a `limit`
    as one too large for a float, on each of the seven methods that read one, by `GET`, and on
    `conversations.list` and `search.messages` by a form `POST` (slack.com/api, 2026-10-20). Those
    nines after a `-` are a `limit` served as a negative one too large for a float, on the four
    methods `_limit` names (2026-10-21)."""
    digits = raw.lstrip("-").lstrip("0") or "0"
    if len(digits) > 19:
        return None
    n = -int(digits) if raw.startswith("-") else int(digits)
    return n if -(2**63) <= n < 2**63 else None


def _numbers(
    request: Request,
    *params: tuple[str, int] | tuple[str, int, bool],
    sort_dir_ok: bool | None = None,
) -> tuple[dict[str, int], JSONResponse | None]:
    """The numeric arguments `params` names as `(key, default)`, each read by `_slack_number` and
    capped at `max_page_size`, and the `invalid_arguments` that names every one real refuses. A
    third element is that key's `decimal`, for a method that reads it by the other rule.

    Real puts one line per refused field in one answer, in the order `params` lists them
    whichever was sent first, and on the search methods an unknown `sort_dir` adds its line after
    them, all of it ahead of an empty query (measured 2026-10-11 and 2026-10-12 on search.messages
    with the query sent first or last, search.all and search.files). `sort_dir_ok` is the
    `empty_ok` half of the rule on `_SORT_DIRS`; None leaves `sort_dir` unread."""
    values: dict[str, int] = {}
    messages: list[str] = []
    for key, default, *rule in params:
        raw = _param(request, key)
        n = default if raw is None else _slack_number(key, raw, decimal=rule[0] if rule else None)
        if n is None:
            messages.append(f"[ERROR] must provide a number [json-pointer:/{key}]")
            n = default
        values[key] = min(n, get_settings().max_page_size)
    if sort_dir_ok is not None:
        value = _param(request, "sort_dir")
        if value is not None and value not in _SORT_DIRS and not (sort_dir_ok and value == ""):
            messages.append(_SORT_DIR_LINE)
    if not messages:
        return values, None
    return values, JSONResponse(
        {"ok": False, "error": "invalid_arguments", "response_metadata": {"messages": messages}}
    )


def _limit(request: Request) -> tuple[int, JSONResponse | None]:
    """The page size a paged method serves, and the refusal for a `limit` real does not read.

    Each handler answers the refusal after its missing-argument check and before it resolves the
    channel, `types`, `latest` or cursor, where real answers it (measured 2026-10-10 and
    2026-10-11). A `limit` read as zero is read as no `limit` at all: on `conversations.list`,
    `users.list`, `conversations.members` and `conversations.history`, `0` (measured 2026-10-03
    and 2026-10-05) and `00` (2026-10-05) answered what the same request without `limit`
    answers, every item and an empty `next_cursor`, on collections of 8 to 21.

    A negative `limit` is returned as it is, for each method to serve as real does (measured
    2026-10-12, one workspace of 21 members and 8 public channels, `-1` to `-1e19`, and 2026-10-21,
    `-1e400`): `conversations.list` and `conversations.members` serve every item, as for no `limit`;
    `conversations.history` serves one message; `users.list` serves no members and a cursor for
    `-1`, and answers `fatal_error` for anything lower. On `users.list`, measured 2026-10-16 on the
    same workspace, the cursor `-1` serves goes on from the second member, as the one `limit=1`
    serves does; `-1` beside a cursor that decodes is `internal_error`; and a cursor that does not
    decode is `invalid_cursor` ahead of the other two (`cursor=bogus&limit=-2`)."""
    default = get_settings().default_page_size
    values, err = _numbers(request, ("limit", default))
    return values["limit"] or default, err
