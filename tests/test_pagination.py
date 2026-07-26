from app import pagination as pg


def test_cursor_roundtrip():
    assert pg.decode_cursor(pg.encode_cursor(0)) == 0
    assert pg.decode_cursor(pg.encode_cursor(250)) == 250
    assert pg.decode_cursor(None) == 0
    assert pg.decode_cursor("garbage") == 0


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
    assert pg.confluence_next_link("/wiki/rest/api/content", {"type": "page"}, 0, 25, 25, 60) is not None
    assert pg.confluence_next_link("/wiki/rest/api/content", {}, 50, 25, 10, 60) is None
