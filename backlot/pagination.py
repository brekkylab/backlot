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


def _page_url(url_no_query: str, params: dict) -> str:
    """One page url in a `Link` header, `<…>`-wrapped with its query percent-encoded."""
    q = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    return f"<{url_no_query}?{q}>"


def github_link_header(
    url_no_query: str,
    params: dict,
    page: int,
    per_page: int,
    total: int,
    *,
    per_page_param: str | None = None,
) -> str | None:
    """Build a Link header with rel=next/prev/first/last. ``params`` are extra query args.

    ``per_page_param`` is the page size the CALLER sent, verbatim, or ``None`` when it sent none.
    A size the handler defaulted is left out of the urls, the rule a listing's filters already
    follow, and one the caller sent goes back in the caller's own spelling rather than as the size
    the handler ended up applying. Both measured on api.github.com on 2026-09-04 against
    `psf/requests/tags` and its 161 tags: no query links `?page=2` and `?page=6` with no `per_page`
    anywhere, `?per_page=abc` links `per_page=abc` with `last` page 6 (it applied 30), and
    `?per_page=500` links `per_page=500` with `last` page 2 (it applied 100). `page` is not like
    this — every link recomputes it and real recomputes it too, answering `?page=abc` with `page=2`.

    `/search/issues` pages by these rules too. `/search/code` and the cursor-paged issue listing do
    not: see :func:`github_code_search_link_header` and :func:`github_cursor_link_header`.

    Returns ``None`` for a single page, which is what real sends there: no header at all, rather
    than one whose every rel points back at the page the caller is already holding. That holds
    however far past a one-page listing the caller asks.

    A page PAST the last one is the caller's way out of a listing it overshot, so ``prev`` names the
    last page that HOLDS rows rather than the page before the one asked for, and ``last`` comes back
    to say where the rows end. Measured on api.github.com on 2026-09-04: pages 7 and 99 of a
    six-page collaborator listing both answer prev=6, last=6, first=1, and a search whose 30 results
    end on page 3 answers prev=3, last=3, first=1 for page 9. On the last page that holds rows there
    is no ``last`` — a client already there needs no pointer to it.

    The rels come in one fixed order whichever of them a page has — prev, next, last, first — which
    is the order real emits across all four of those pages.

    Values are percent-encoded because a param value is arbitrary text — a search `q` carries
    spaces and colons — and a URI cannot hold them raw. A client that follows the link has to
    arrive at the query it was paging, not at a truncated one.
    """
    last_page = max(1, (total + per_page - 1) // per_page)
    if last_page <= 1:
        return None

    size = {} if per_page_param is None else {"per_page": per_page_param}

    def link(p: int) -> str:
        return _page_url(url_no_query, {**params, **size, "page": p})

    # `last_page >= 2` here, so one of the two page tests always holds and `parts` is never empty
    parts = []
    if page > 1:
        parts.append(f'{link(min(page - 1, last_page))}; rel="prev"')
    if page < last_page:
        parts.append(f'{link(page + 1)}; rel="next"')
    if page != last_page:  # short of the end, or past it
        parts.append(f'{link(last_page)}; rel="last"')
    if page > 1:
        parts.append(f'{link(1)}; rel="first"')
    return ", ".join(parts)


def github_cursor_offset(page: int, per_page: int, after: str | None, before: str | None) -> int:
    """Where a cursor-paged window starts: the cursor's offset, or the page's own when none came.

    A cursor OVERRIDES ``page`` rather than adding to it — measured on api.github.com on 2026-09-04,
    `/repos/psf/requests/issues?per_page=2&page=50` carrying page 1's `after` answers the rows
    `page=2` answers, where `page=50` alone answers a different window. ``before`` names the window
    that ENDS where the cursor points, which is how real's own `prev` walks back a page.
    """
    if after is not None:
        return decode_cursor(after)
    if before is not None:
        return max(0, decode_cursor(before) - per_page)
    return (page - 1) * per_page


def github_code_search_link_header(
    url_no_query: str, params: dict, page: int, per_page: int, total: int
) -> str | None:
    """Build a Link header for `/search/code`, which pages by none of the listings' rules.

    Measured at five positions on api.github.com on 2026-09-04, `repo:psf/requests def` at
    `per_page=5` (45 hits, nine pages), and again on `repo:kubernetes/kubernetes extension:md
    kubelet`:

    * `first` and `last` are on EVERY page — page 1 answers `next, first, last` and page 9, the last
      that holds rows, answers `prev, first, last`, where a listing sends neither rel there.
    * `prev` is the page before the one ASKED for even past the end: page 99 answers prev=98, where
      a listing clamps it to the last page that holds rows.
    * the rels come in the order next, prev, first, last.
    * the size written is the one APPLIED, whether or not the caller named one and whatever they
      spelt: no `per_page` in the query links `per_page=30`, `?per_page=500` on 1,808 hits links
      `per_page=100` (its cap) and `?per_page=0` links `per_page=30`, where a listing omits an
      unsent size and echoes a sent one verbatim. This cell it shares with the cursor listing.

    `/search/issues` shares none of that and pages by :func:`github_link_header`. A result set that
    fits one page carries no header, which is the one rule all three surfaces share.
    """
    last_page = max(1, (total + per_page - 1) // per_page)
    if last_page <= 1:
        return None

    def link(p: int) -> str:
        return _page_url(url_no_query, {**params, "per_page": per_page, "page": p})

    parts = []
    if page < last_page:
        parts.append(f'{link(page + 1)}; rel="next"')
    if page > 1:
        parts.append(f'{link(page - 1)}; rel="prev"')
    parts.append(f'{link(1)}; rel="first"')
    parts.append(f'{link(last_page)}; rel="last"')
    return ", ".join(parts)


def github_cursor_link_header(
    url_no_query: str,
    params: dict,
    page: int | None,
    per_page: int,
    total: int,
    offset: int,
) -> str | None:
    """Build a Link header for a listing real pages by CURSOR rather than by offset.

    `/repos/{owner}/{repo}/issues` is one; every other GitHub listing takes
    :func:`github_link_header`. Measured on api.github.com on 2026-09-04 against a repository with
    12 open issues at `per_page=5`: `next` and `prev` are the only rels — no `last`, no `first`,
    ever — page 1 answers `next` alone, page 2 `next, prev` in that order, page 3 `prev` alone, and
    pages 4 and 99 no header at all, as do those 12 rows at `per_page=100`.

    ``page`` is the number the caller ASSERTED, or ``None`` when it asserted none — real writes a
    page number only where the caller claimed a page of 2 or more, and drops `page` from both urls
    entirely otherwise. Measured the same day on `?per_page=2`: `?after=C`, `?page=1&after=C` and
    `?page=0&after=C` all answer `next` and `prev` with no `page` in either url, where
    `?page=2&after=C` answers next=3/prev=1 and `?page=50&after=C` next=51/prev=49. So the number
    is carried rather than computed, and the alternative to carrying one is writing none.

    The size is written whether or not the caller sent one, and as the size APPLIED rather than as
    the caller spelt it: `?per_page=abc` links `per_page=30` here, where `/tags` links `per_page=abc`
    (both measured). The cursor is what names the window (see :func:`github_cursor_offset`).
    """
    if offset >= total:  # a window past the rows carries no header at all, not even `prev`
        return None

    def link(step: int, **cursor) -> str:
        q = {**params, "per_page": per_page}
        if page is not None:
            q["page"] = page + step
        return _page_url(url_no_query, {**q, **cursor})

    parts = []
    if offset + per_page < total:
        parts.append(f'{link(1, after=encode_cursor(offset + per_page))}; rel="next"')
    if offset > 0:
        parts.append(f'{link(-1, before=encode_cursor(offset))}; rel="prev"')
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
