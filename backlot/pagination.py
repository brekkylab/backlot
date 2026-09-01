"""Per-scheme pagination helpers.

Each vendor exposes a different native pagination contract. All of them reduce to an
integer offset over a stably-ordered result set; these helpers translate between that
offset and the vendor's token/header representation.
"""

from __future__ import annotations

import base64
from typing import Annotated
from urllib.parse import quote

from fastapi import Query
from pydantic import BeforeValidator


# --- opaque offset cursor (Slack next_cursor, Gmail/Drive pageToken, Jira token) ---


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode()


def decode_cursor(token: str | None) -> int:
    """The offset a page token names, or 0 for anything that does not decode — for the sources
    whose API restarts the crawl rather than reporting a bad cursor. Slack does report one, hence
    :func:`decode_cursor_or_none`; the decoding itself is shared."""
    offset = decode_cursor_or_none(token)
    return 0 if offset is None else offset


def decode_cursor_or_none(token: str | None) -> int | None:
    """Like ``decode_cursor``, but ``None`` for a token that does not decode. Slack answers
    ``invalid_cursor``; silently restarting at page 0 makes a client with a corrupted cursor loop
    on the first page instead of failing."""
    if not token:
        return 0
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if not raw.startswith("o:"):
        return None
    try:
        return max(0, int(raw[2:]))
    except ValueError:
        return None


def next_cursor(offset: int, page_len: int, total: int) -> str:
    """Slack-style next_cursor: empty string when there are no more results."""
    nxt = offset + page_len
    return encode_cursor(nxt) if nxt < total else ""


def next_page_token(offset: int, page_len: int, total: int) -> str | None:
    """Google/Jira-style token: omitted (None) when exhausted."""
    nxt = offset + page_len
    return encode_cursor(nxt) if nxt < total else None


# --- GraphQL sources ------------------------------------------------------------
# The two GraphQL vendors disagree on pagination shape — Linear pages a Relay connection
# (``first``/``after`` -> ``{nodes, pageInfo}``) while Fireflies is plain offset paging
# (``limit``/``skip``) — so only the parts that genuinely coincide live here: the opaque
# cursor above (Linear's ``after`` is an offset cursor like every other source's token) and
# the page-size clamp below. The response shapes stay in each vendor's resolvers.


def clamp_limit(limit: int | None, default: int, maximum: int) -> int:
    """Page size with the vendor's default and hard cap applied (Fireflies caps at 50)."""
    n = limit if limit and limit > 0 else default
    return min(n, maximum)


# --- GitHub: page/per_page + RFC5988 Link header --------------------------------


def _absorb_page(v):
    """A page parameter's value, or ``None`` for one that will not parse.

    Real GitHub refuses no value here: `per_page=0`, `per_page=abc`, `page=0`, `page=-1` and
    `page=abc` are each a 200 with the defaults applied, and a `per_page` over the cap is a 200 at
    the cap (measured against a public repository's issue listing). Refusing them would hand a
    paginator computing an edge value a hard error where production absorbs it.

    A `BeforeValidator` rather than a `str` annotation: the parameter stays an integer in the
    OpenAPI schema, which is what real's own spec declares, so the tolerance is in the runtime
    where real has it and not in the contract where real does not.
    """
    if v is None or isinstance(v, int):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


#: A GitHub `page`/`per_page` query parameter. Declared per route as `page: PageParam = None`.
#: Only the GitHub surface uses it — the other vendors' answers to an unparseable page value are
#: not measured, and a helper that spread this tolerance to them would be asserting they share it.
PageParam = Annotated[int | None, BeforeValidator(_absorb_page), Query()]


def clamp_page(
    page: int | None, per_page: int | None, default: int, maximum: int
) -> tuple[int, int]:
    p = page if page and page > 0 else 1
    pp = per_page if per_page and per_page > 0 else default
    pp = min(pp, maximum)
    return p, pp


def github_link_header(
    url_no_query: str, params: dict, page: int, per_page: int, total: int
) -> str | None:
    """Build a Link header with rel=next/prev/first/last. ``params`` are extra query args.

    Returns ``None`` for a single page, which is what real sends there: no header at all, rather
    than one whose every rel points back at the page the caller is already holding.

    Values are percent-encoded because a param value is arbitrary text — a search `q` carries
    spaces and colons — and a URI cannot hold them raw. A client that follows the link has to
    arrive at the query it was paging, not at a truncated one.
    """
    last_page = max(1, (total + per_page - 1) // per_page)
    if last_page <= 1:
        return None

    def link(p: int) -> str:
        q = "&".join(
            f"{k}={quote(str(v), safe='')}"
            for k, v in {**params, "per_page": per_page, "page": p}.items()
        )
        return f"<{url_no_query}?{q}>"

    parts = []
    if page < last_page:
        parts.append(f'{link(page + 1)}; rel="next"')
        parts.append(f'{link(last_page)}; rel="last"')
    if page > 1:
        parts.append(f'{link(page - 1)}; rel="prev"')
        parts.append(f'{link(1)}; rel="first"')
    return ", ".join(parts) if parts else None


# --- Confluence: start/limit + relative _links.next -----------------------------


def confluence_next_link(
    path: str, params: dict, start: int, limit: int, size: int, total: int
) -> str | None:
    nxt = start + size
    if nxt >= total or size == 0:
        return None
    q = "&".join(f"{k}={v}" for k, v in {**params, "start": nxt, "limit": limit}.items())
    return f"{path}?{q}"
