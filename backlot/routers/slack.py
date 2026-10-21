"""Slack Web API (read-only).

Base URL for a client: ``http://<host>/slack/api/`` (methods live under ``/api/``).
Slack always returns HTTP 200 with an ``{"ok": bool}`` envelope, so auth failures are
signalled in the body rather than as a 401 status. There are TWO of them and they are not
interchangeable: a MISSING credential is ``not_authed``, a credential that is present but does not
resolve is ``invalid_auth``. Connectors branch on exactly that split — re-authenticate versus give
up — so ``_caller_or_error`` decides it once for every method here.
"""

from __future__ import annotations

import math
import re
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backlot import auth, store, synth
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
    that refusal is drawn. Slack's spec declares that error for every served method that declares
    an error enum at all — 8 of the 12, ``api.test`` and ``search.messages`` declaring none and
    ``search.all``/``search.files`` being absent from the spec — and declares it unqualified, which
    the method reference's own sentence for it does not ("...for a deleted user or workspace when
    using a bot token", where Backlot's per-person tokens are user tokens). An admin or anonymous
    caller has no email, so it is never one of these people.
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


def read_post_body(content_type: str | None, body: bytes) -> tuple[dict[str, str], str | None]:
    """The arguments a POST body carries, and the error real answers it with instead of running the
    method, or None. Each error comes ahead of the credential, a `token` query parameter included,
    and of the arguments, in the order the checks here run.

    A body is read as multipart when :func:`_rfc1867_boundary` reads a boundary off its
    Content-Type, else as a form when the Content-Type starts with the form type in any case
    (`APPLICATION/X-WWW-FORM-URLENCODED`, `…-urlencodedx` and `…-urlencoded, text/html` are read
    too); what it carries passes the media-type check whatever the media type is. A JSON or
    `text/plain` body is not read here. `Content-Encoding` is not read: a gzipped body is read as
    the bytes it is, and a plain one under `gzip`, `bogus` or an empty value is read as itself.
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
    return args or {}, None


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
        # would show if there were one. Slack documents nothing about what it does -- the
        # conversation object shows only an ASCII example, and the vendor's OpenAPI carries no
        # description for the field -- so the response is the only source there is.
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


def _handle(email: str) -> str:
    """A corpus email as Slack's handle — the `name` a user object carries and the `user`
    `auth.test` reports for the same person."""
    return email.split("@")[0].replace(".", "")


def _user_obj(conn, email: str, caller: Caller) -> dict:
    u = store.get_user(conn, email)
    display = u["display_name"] if u else email.split("@")[0]
    parts = display.split()
    updated = synth.epoch("user:" + email)
    is_bot = not u and email.split("@")[0].endswith("bot")  # display-only "*bot" speakers
    # A roster entry's `deactivated: true`. Measured against a live workspace on 2026-09-17:
    # `deleted` is present on all 19 members and true on the 9 deactivated ones, so it is served
    # unconditionally where docs.slack.dev's user object allows either ("Otherwise the value is
    # false, or the field may not appear at all"). `is_forgotten` is a different state, not a
    # spelling of this one: the same page types it Boolean and gives it as "Whether the user has
    # been GDPR-forgotten", and on that workspace 5 of the 9 carried it against 0 of the 10
    # active. A roster states no GDPR erasure, so there is nothing here to derive it from.
    deactivated = bool(u) and store.slack_is_deactivated(conn, email)
    obj = {
        "id": synth.slack_user_id(email),
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
    if auth.slack_query_token_refused(request) or auth.slack_token(request) is not None:
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

    Slack's spec pins both fields at once: `slack_web_openapi_v2.json`,
    `paths./auth.test.get.responses.200`, whose user-token example is `"user": "grace"` with
    `"user_id": "W12345678"`. So `user` is the HANDLE, not an address — the same `name` a user
    object carries for that person — and `user_id` is that caller's own id, the `U…` that
    `users.list` reports and that a message in `conversations.history` names as its author.

    A workspace constant can never match: "call auth.test, then match `user_id` against message
    authors" is how a client finds its own messages, so the id is the field that costs.

    The admin/service token has no corpus email, so it keeps the service identity: that caller is
    not a person, and real Slack answers a bot token with the app's own id rather than a user's."""
    caller, err = _caller_or_error(request)
    if err is not None:
        return err
    who = "service-account" if caller.is_admin else _handle(caller.email)
    return {
        "ok": True,
        "url": f"https://{get_settings().org_name}.slack.com/",
        "team": get_settings().org_name,
        "user": who,
        "team_id": TEAM_ID,
        "user_id": "USERVICE0" if caller.is_admin else synth.slack_user_id(caller.email),
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
                conn, name, ids, limit=10**9, ts_lo=lo_sec, ts_hi=hi_sec
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


# --- helpers --------------------------------------------------------------------


def _search_match(conn, row) -> dict:
    """A search.messages `matches[]` entry for a slack row.

    `username` is the author's handle, the same string `users.list` gives that person as `name` —
    Slack's spec spells it that way in this method's own example (`slack_web_openapi_v2.json`,
    `paths./search.messages.get.responses.200`, whose matches carry `"username": "roach"`). It goes
    through `_handle` for that reason: a hit and a user object name one person, and a client that
    reads the id off `user` and the handle off `username` must not get two spellings of them.
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
        "user": synth.slack_user_id(row["author_email"]),
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

    Not pass-through, and the vendor's own spec is why. `objs_reaction` requires `name`, `users`
    and `count` together, and each entry of `users` is a `defs_user_id` (`^[UW][A-Z0-9]{2,}$`) —
    which is `synth.slack_user_id` of a person, a value no corpus author can know. Writing it by
    hand meant inventing ids for people the corpus already names by address everywhere else.

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
                "users": [synth.slack_user_id(e) for e in users],
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
    """Renders `edited.user` via `synth.slack_user_id`, the same lift `_reactions` gives
    `reactions.users` (see `backlot/schemas/slack.schema.json` for the vendor shape behind it);
    `ts` passes through unchanged."""
    return {"user": synth.slack_user_id(edited["user"]), "ts": edited["ts"]}


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


def _slack_number(key: str, raw: str) -> int | None:
    """The value of one numeric argument by the rule on `_SLACK_INTEGER`, or None where real
    refuses it. A `limit` is its integer part (`1.5` is 1, `3.99999` is 3, `-1.5` is -1). One too
    large for a float is zero when it is positive (`1e400`), which `_limit` serves as no `limit`, as
    real served every item, and -2**63 when it is negative, which each method serves as it serves
    `-2`. An integer part at or past 2**63 wraps around 2**64, so `9223372036854775808`
    and `1e19` are negative and `1e300` is zero, and one below -2**63 stays negative: the reading
    that every row of the measurement on `_limit` agrees with."""
    if key == "limit":
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
    request: Request, *params: tuple[str, int], sort_dir_ok: bool | None = None
) -> tuple[dict[str, int], JSONResponse | None]:
    """The numeric arguments `params` names as `(key, default)`, each read by `_slack_number` and
    capped at `max_page_size`, and the `invalid_arguments` that names every one real refuses.

    Real puts one line per refused field in one answer, in the order `params` lists them
    whichever was sent first, and on the search methods an unknown `sort_dir` adds its line after
    them, all of it ahead of an empty query (measured 2026-10-11 and 2026-10-12 on search.messages
    with the query sent first or last, search.all and search.files). `sort_dir_ok` is the
    `empty_ok` half of the rule on `_SORT_DIRS`; None leaves `sort_dir` unread."""
    values: dict[str, int] = {}
    messages: list[str] = []
    for key, default in params:
        raw = _param(request, key)
        n = default if raw is None else _slack_number(key, raw)
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
