"""Whether a request's percent-escapes decode to UTF-8, and how Drive and the Docs, Sheets and
Slides APIs read a query value's escapes.

An escape that does not decode is a byte that starts no sequence (`%80`, `%FF`), a sequence cut
short (`%C3`, `%E2%82`), an overlong form (`%C0%80`), a surrogate (`%ED%A0%80`) or a code point past
U+10FFFF (`%F4%90%80%80`), which are the inputs Python's UTF-8 decoder refuses. What each vendor
answers a path holding one is at the place that answers it: Jira's in
``backlot.errors.atlassian.front_door``, Graph's in ``backlot.request_target.escape_answer``,
Notion's in ``backlot.routers.notion.undecodable_url``, S3's in ``backlot.routers.s3.path_refusal``
and Google's in ``backlot.routers.google._refuse_an_undecodable_path``.
"""

from __future__ import annotations

import re
import urllib.parse

#: A run of escapes, each two hex digits after its `%`.
_ESCAPES = re.compile(rb"(?:%[0-9A-Fa-f]{2})+")


def decodes(raw: str | bytes) -> bool:
    """Whether the bytes ``raw`` holds, with each escape read as its byte, are UTF-8."""
    try:
        urllib.parse.unquote_to_bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _as_google_reads(run: re.Match[bytes]) -> bytes:
    """A run of escapes spelled as Drive and the editors read it: each byte that is not part of a
    UTF-8 sequence as `%20`, `%00` as the three characters `%00` (`%2500`), and every other
    character as its own escapes."""
    out = []
    for char in urllib.parse.unquote_to_bytes(run[0]).decode("utf-8", "surrogateescape"):
        if "\udc80" <= char <= "\udcff":
            out.append(b"%20")
        elif char == "\x00":
            out.append(b"%2500")
        else:
            out.append(b"".join(b"%%%02X" % byte for byte in char.encode("utf-8")))
    return b"".join(out)


def google_query(query: bytes, credentials: tuple[str, ...]) -> bytes:
    """``query`` with each value's escapes spelled as :func:`_as_google_reads` spells them, the
    names as sent, and the value of each name ``credentials`` holds, its escapes decoded, as sent.

    Measured on values whose refusal quotes them back: on 2026-10-29 (UTC), Docs' `fields` with
    each escape from `%80` to `%FF`, and on 2026-10-30, Docs' and Drive's `fields` and Sheets'
    `majorDimension` with `%80`, `%C3`, `%e9`, `%FF`, `%C0%80`, `%E2%82`, `%ED%A0%80`,
    `%F4%90%80%80`, `%FF%FE`, `%00`, `%01`, `%7F` and `%C3%A9`; and the same three with `%80` and
    `%00` in the form of a POST naming `GET` in `X-HTTP-Method-Override`, read as the same value in
    the URL is. On 2026-10-29 `documents.create` stored the title of its query so too, and on
    2026-10-31 Slides' `fields` read the thirteen escapes sent on 2026-10-30 as Docs' did. On
    2026-11-03 Drive's `about` refused a token sent as `access_token`, `access%5Ftoken`,
    `oauth_token` or `bearer_token` with `%80`, `%C3%A9` or `%00` after it, as it refuses a token
    that does not resolve."""
    parts = []
    for part in query.split(b"&"):
        name, mark, value = part.partition(b"=")
        if urllib.parse.unquote_to_bytes(name).decode("latin-1") not in credentials:
            value = _ESCAPES.sub(_as_google_reads, value)
        parts.append(name + mark + value)
    return b"&".join(parts)
