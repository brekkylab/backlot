"""Per-scheme pagination helpers.

Each vendor exposes a different native pagination contract. All of them reduce to an
integer offset over a stably-ordered result set; these helpers translate between that
offset and the vendor's token/header representation.
"""

from __future__ import annotations

import base64


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
    """Build a Link header with rel=next/prev/first/last. ``params`` are extra query args."""
    last_page = max(1, (total + per_page - 1) // per_page)
    if last_page <= 1:
        return None

    def link(p: int) -> str:
        q = "&".join(f"{k}={v}" for k, v in {**params, "per_page": per_page, "page": p}.items())
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
