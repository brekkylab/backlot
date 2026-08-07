import pytest

from backlot import pagination as pg


def test_cursor_roundtrip():
    assert pg.decode_cursor(pg.encode_cursor(0)) == 0
    assert pg.decode_cursor(pg.encode_cursor(250)) == 250
    assert pg.decode_cursor(None) == 0
    assert pg.decode_cursor("garbage") == 0


def test_decode_cursor_is_the_or_none_variant_with_a_zero_floor():
    """The two differ only in what an undecodable token means — 0 for callers that restart the
    crawl, None for Slack, which answers `invalid_cursor` instead. Same decoding either way."""
    import base64

    for tok in (
        None,
        "",
        "garbage",
        base64.urlsafe_b64encode(b"x:1").decode(),
        base64.urlsafe_b64encode(b"o:nope").decode(),
        pg.encode_cursor(0),
        pg.encode_cursor(7),
        pg.encode_cursor(250),
    ):
        strict = pg.decode_cursor_or_none(tok)
        assert pg.decode_cursor(tok) == (0 if strict is None else strict), tok


def test_next_cursor_terminates():
    # 25 items, page of 10
    assert pg.next_cursor(0, 10, 25) != ""
    assert pg.next_cursor(10, 10, 25) != ""
    assert pg.next_cursor(20, 5, 25) == ""  # 20+5 == 25 -> done


def test_next_page_token_none_when_done():
    assert pg.next_page_token(0, 10, 25) is not None
    assert pg.next_page_token(20, 5, 25) is None


def test_cursor_walk_visits_every_item_once():
    total, page = 23, 10
    seen, offset, guard = [], 0, 0
    while True:
        guard += 1
        assert guard < 100
        page_len = min(page, total - offset)
        seen.extend(range(offset, offset + page_len))
        tok = pg.next_cursor(offset, page_len, total)
        if not tok:
            break
        offset = pg.decode_cursor(tok)
    assert seen == list(range(total))


def test_clamp_limit_falls_back_to_the_default():
    assert pg.clamp_limit(None, 10, 50) == 10
    assert pg.clamp_limit(0, 10, 50) == 10
    assert pg.clamp_limit(-5, 10, 50) == 10


def test_clamp_limit_caps_at_the_maximum():
    assert pg.clamp_limit(500, 10, 50) == 50
    assert pg.clamp_limit(25, 10, 50) == 25


def test_github_link_header():
    h = pg.github_link_header("http://x/repos/o/r/issues", {"state": "all"}, 1, 10, 25)
    assert 'rel="next"' in h and 'rel="last"' in h
    assert "page=2" in h and "page=3" in h
    # last page -> no next
    assert pg.github_link_header("http://x", {}, 3, 10, 25) is not None  # has prev/first
    assert pg.github_link_header("http://x", {}, 1, 10, 5) is None  # single page


def test_confluence_next_link():
    assert (
        pg.confluence_next_link("/wiki/rest/api/content", {"type": "page"}, 0, 25, 25, 60)
        is not None
    )
    assert pg.confluence_next_link("/wiki/rest/api/content", {}, 50, 25, 10, 60) is None


# --- Linear: Relay connections ----------------------------------------------------
# Linear pages a Relay connection (`first`/`after` -> `{nodes, pageInfo}`) rather than the
# offset/token schemes above. The cursor underneath is this module's opaque offset cursor, so
# what is Linear-specific is the slice arithmetic and the pageInfo flags.

from graphql import GraphQLError  # noqa: E402

from backlot.graphql.linear_resolvers import (  # noqa: E402
    PAGE_DEFAULT,
    PAGE_MAX,
    _connection,
    _from_end,
    _page,
    _slice,
)


def test_linear_forward_slice_defaults_to_linears_page_size():
    assert _slice(None, None, None, None) == (0, PAGE_DEFAULT, 0)


def test_linear_first_is_capped_at_linears_maximum():
    assert _slice(10_000, None, None, None) == (0, PAGE_MAX, 0)


def test_linear_after_cursor_becomes_the_offset():
    assert _slice(10, pg.encode_cursor(30), None, None) == (30, 10, 30)


def test_linear_backward_slice_ends_just_before_the_cursor():
    # `before` is the offset of the first row already seen; `last: 5` is the 5 rows before it.
    assert _slice(None, None, 5, pg.encode_cursor(20)) == (15, 5, 0)


def test_linear_backward_slice_clamps_at_the_start():
    """Asking for more rows than exist before the cursor must not produce a negative offset or
    re-read rows at or past the cursor."""
    assert _slice(None, None, 10, pg.encode_cursor(3)) == (0, 3, 0)


def test_linear_last_without_before_defers_to_the_total():
    """`last:` with no `before:` means "the final n rows", which cannot be known without the
    total — so the slice defers rather than guessing. Guessing offset 0 (what an earlier version
    did) served the FIRST n rows to every client asking for the last n."""
    offset, limit, floor = _slice(None, None, 5, None)
    assert offset is None and limit == 5 and floor == 0
    assert _from_end(None, 5, 0, total=21) == 16  # the last 5 of 21
    assert _from_end(None, 5, 0, total=3) == 0  # fewer rows than asked for
    assert _from_end(7, 5, 0, total=21) == 7  # an explicit offset is left alone


def test_linear_after_still_applies_when_combined_with_last():
    """Relay applies `after` first, then takes the last n of what remains — so the tail may not
    reach back past the cursor."""
    offset, limit, floor = _slice(None, pg.encode_cursor(18), 5, None)
    assert (offset, limit, floor) == (None, 5, 18)
    assert _from_end(None, 5, 18, total=21) == 18  # floor wins over total-limit (=16)


def test_linear_both_directions_at_once_is_rejected():
    with pytest.raises(GraphQLError):
        _slice(5, None, 5, None)


def test_linear_page_info_flags_the_middle_of_a_result_set():
    page = _connection([{"id": 1}, {"id": 2}], offset=2, has_next=True)
    assert page["pageInfo"]["hasNextPage"] is True
    assert page["pageInfo"]["hasPreviousPage"] is True
    assert pg.decode_cursor(page["pageInfo"]["startCursor"]) == 2
    # endCursor is where the NEXT page starts, so it feeds straight back in as `after`.
    assert pg.decode_cursor(page["pageInfo"]["endCursor"]) == 4


def test_linear_page_info_terminates_on_the_last_page():
    page = _connection([{"id": 9}], offset=9, has_next=False)
    assert page["pageInfo"]["hasNextPage"] is False
    assert page["pageInfo"]["hasPreviousPage"] is True


def test_linear_limit_plus_one_probe_decides_has_next():
    """`hasNextPage` comes from reading ONE row past the page, not from a COUNT — the schema has
    no `totalCount`, so a count would be a full scan computed only to derive a boolean."""
    assert _page([1, 2, 3], 2) == ([1, 2], True)  # the extra row is dropped, never served
    assert _page([1, 2], 2) == ([1, 2], False)
    assert _page([], 2) == ([], False)


def test_linear_empty_page_has_no_cursors():
    page = _connection([], offset=0, has_next=False)
    assert page["pageInfo"] == {
        "hasNextPage": False,
        "hasPreviousPage": False,
        "startCursor": None,
        "endCursor": None,
    }


# --- fireflies: offset pagination, not a Relay connection ------------------------
# Fireflies pages with `limit`/`skip` and documents `limit` as "max 50". It CLAMPS rather than
# erroring, so these pin the clamp and the offset walk (the cursor helpers above do not apply).


def test_fireflies_limit_clamps_at_fifty():
    from backlot.graphql.fireflies_resolvers import PAGE_DEFAULT, PAGE_MAX, clamp_limit

    assert PAGE_MAX == 50
    assert clamp_limit(1) == 1
    assert clamp_limit(50) == 50
    assert clamp_limit(51) == 50  # clamped, not rejected
    assert clamp_limit(10_000) == 50
    assert clamp_limit(None) == PAGE_DEFAULT
    # a value that cannot be a page size falls back to the default rather than returning nothing
    for bad in (0, -1, "abc", 2.5):
        assert clamp_limit(bad) in (PAGE_DEFAULT, 2)


def test_fireflies_skip_is_never_negative():
    from backlot.graphql.fireflies_resolvers import clamp_skip

    assert clamp_skip(0) == 0
    assert clamp_skip(7) == 7
    assert clamp_skip(-5) == 0  # a negative offset would be a SQL error
    assert clamp_skip(None) == 0
    assert clamp_skip("nope") == 0


def test_fireflies_datetime_arguments_accept_iso_and_epoch():
    """Fireflies documents these as ISO 8601 but returns `date` as epoch MILLISECONDS, and
    clients pass back what they were handed — so both have to coerce to the same instant."""
    from backlot.graphql.fireflies_resolvers import to_epoch_seconds

    assert to_epoch_seconds("2026-04-02T15:00:00Z") == 1775142000
    assert to_epoch_seconds("2026-04-02T15:00:00+00:00") == 1775142000
    assert to_epoch_seconds(1775142000) == 1775142000  # seconds
    assert to_epoch_seconds(1775142000000) == 1775142000  # milliseconds
    assert to_epoch_seconds("1775142000") == 1775142000
    assert to_epoch_seconds(None) is None
    assert to_epoch_seconds("not a date") is None


def test_fireflies_offset_walk_visits_every_transcript_once(db):
    from backlot import store

    total = store.count_fireflies_transcripts(db)
    seen = []
    skip = 0
    while True:
        page = store.list_fireflies_transcripts(db, limit=2, offset=skip)
        if not page:
            break
        seen += [r["doc_id"] for r in page]
        skip += 2
    assert len(seen) == total == len(set(seen))
