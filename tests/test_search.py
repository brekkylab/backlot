"""Full-text search: store.build_fts + search_documents (FTS5) over the SAMPLE corpus.

The `db` fixture is built via backlot.importer.byo.load, which builds every source's own FTS
index, so
these exercise the real FTS path (search.messages / confluence CQL both sit on search_documents).
"""

import pytest

from backlot import store
from tests._helpers import served_id


def test_fts_index_built(db):
    assert all(store._has_fts(db, src) for src in store.SOURCE_TABLE), (
        "the importer should have built every source's full-text index"
    )


def test_fts_slack_search(db):
    rows = store.search_documents(db, "gateway", "slack")
    assert rows, "expected a slack message matching 'gateway'"
    assert any("gateway" in (r["content"] or "").lower() for r in rows)
    # token search, ACL admin (visible_ids=None) sees it; a nonsense term matches nothing
    assert store.search_documents(db, "zqxjkbrqznope", "slack") == []
    assert store.count_search(db, "gateway", "slack") >= 1


def test_fts_matches_title(db):
    # confluence page titled "On-call Runbook" — FTS indexes title too
    rows = store.search_documents(db, "runbook", "confluence")
    assert any("runbook" in (r["title"] or "").lower() for r in rows)


@pytest.mark.parametrize(
    "term, source, caller",
    [
        # each term appears only in a group-restricted document, and the caller is outside the group
        pytest.param("compensation", "confluence", "bob@acme.com", id="confluence"),
        pytest.param("confidential", "notion", "ava@acme.com", id="notion"),
        pytest.param("rotation", "linear", "ava@acme.com", id="linear"),
    ],
)
def test_fts_is_acl_scoped(db, acl, tokens, term, source, caller):
    """An index hit still has to pass the ACL: admin finds the restricted document, a caller outside
    its group finds nothing."""
    assert store.count_search(db, term, source, visible_ids=None) >= 1
    ids = acl.visible_ids(db, acl.resolve(tokens[caller]))
    assert store.count_search(db, term, source, visible_ids=ids) == 0


def test_fts_search_returns_only_its_own_source(db):
    # Was `test_fts_source_aware_index`, which asserted the shared index's `src:` intersection.
    # Each source has its own index rather than one shared, tagged one, but the PROPERTY is
    # the same and still worth pinning: a source-scoped search must never return another
    # source's rows, whatever enforces it.
    #
    # 'gateway' is in slack + confluence (SAMPLE); each source-scoped search returns only its
    # own rows, and a source whose title/content lacks the term returns nothing
    sl = store.search_documents(db, "gateway", "slack")
    cf = store.search_documents(db, "gateway", "confluence")
    assert sl and all("channel" in r.keys() for r in sl)
    assert cf and all("space" in r.keys() for r in cf)
    assert store.search_documents(db, "gateway", "github") == []


@pytest.mark.parametrize("src", sorted(store.SOURCE_TABLE))
def test_fts_every_source_has_its_own_index(db, src):
    """Each source searches its OWN FTS table.

    The split is about bm25's IDF, not scan cost: IDF is computed over whatever table the term is
    in, so a shared index made how a term ranks INSIDE one source depend on how often it appeared
    in the others.
    """
    fts = f"{src}_fts"
    assert store._fts_table(src) == fts
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert fts in names
    assert store._fts_match("latency spike") == '"latency" AND "spike"'
    # every doc of this source is in its own index and nothing else is
    n_src = db.execute(f"SELECT COUNT(*) FROM {store.table(src)}").fetchone()[0]
    n_fts = db.execute(f"SELECT COUNT(*) FROM {fts}").fetchone()[0]
    assert n_fts == n_src, f"{fts} should hold exactly {src}'s rows"


def test_fts_jira_search_survived_its_move(db):
    """The documents jira search returns did not change when it moved off the shared index —
    only their relative ORDER can, and only for a multi-term query (see the commit that added
    `jira_fts`: a single term's IDF is one constant factor across every match, so it cannot
    reorder)."""
    rows = store.search_documents(db, "latency", "jira")
    assert rows and all("project" in r.keys() for r in rows)
    assert store.search_documents(db, "gateway", "jira") == []
    assert store.count_search(db, "latency", "jira") >= 1


def test_fts_drive_source(db):
    # Drive has its own FTS index, so fullText search hits it via FTS (title + content)
    rows = store.search_documents(db, "palette", "google_drive")
    assert rows and any("palette" in (r["content"] or "").lower() for r in rows)
    # scoping is exact: a term absent from Drive title/content returns nothing for Drive
    assert store.search_documents(db, "postmortem", "google_drive") == []


def test_fts_container_scoped(db):
    # 'latency' is in the payments Jira project; container-scoping to a foreign project drops it
    assert store.search_documents(db, "latency", "jira", container="payments")
    assert store.count_search(db, "latency", "jira", container="payments") >= 1
    assert store.search_documents(db, "latency", "jira", container="no-such-project") == []
    assert store.count_search(db, "latency", "jira", container="no-such-project") == 0


def test_fts_phrase_match_is_adjacent():
    # phrase=True requires the tokens ADJACENT (an FTS5 phrase); the default ANDs them anywhere.
    # This is what a grep push-down needs so a literal pattern isn't buried under scattered hits.
    # No `src:` term: every source has its own index now, so there is nothing to intersect against.
    assert store._fts_match("upload csv") == '"upload" AND "csv"'
    assert store._fts_match("upload csv", phrase=True) == '"upload csv"'


def test_fts_phrase_boosts_literal_substring():
    # FTS tokenization drops punctuation, so "upload.csv" and "upload csv" tokenize identically and
    # bm25 can't tell them apart. A phrase search for "upload.csv" must still rank the doc that
    # LITERALLY contains "upload.csv" above the coincidental "upload csv" mentions.
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(store.SCHEMA)
    rows = [
        ("d_space", "please upload csv " + "filler " * 20),  # tokens adjacent, no literal dot
        ("d_literal", "the export is upload.csv exactly"),  # literal "upload.csv"
    ]
    for doc_id, content in rows:
        con.execute(
            "INSERT INTO slack_messages(ts, channel, author_email, content, thread_seq, "
            "created_ts) VALUES (?, 'eng', 'a@x.com', ?, 0, 1000)",
            (doc_id, content),
        )
    store.build_fts(con)
    hits = store.search_documents(con, "upload.csv", "slack", phrase=True)
    assert [h["ts"] for h in hits][:2] == ["d_literal", "d_space"], (
        "the doc literally containing 'upload.csv' must rank first"
    )
    # a plain (non-phrase) search does not reorder — it only ANDs the tokens
    plain = store.search_documents(con, "upload.csv", "slack")
    assert {h["ts"] for h in plain} == {"d_literal", "d_space"}


def test_search_falls_back_to_like_without_an_fts_index():
    """The LIKE path the docstring promises, exercised for a source with no `title` column.

    `_fts5_ok` / `_has_fts` exist because a build without FTS5 has to keep answering, and slack is
    the one source whose doc table has no `title` — so the fallback's own SQL has to ask each source
    for its title the same way the index build does.
    """
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(store.SCHEMA)
    con.execute(
        "INSERT INTO slack_messages(channel, ts, author_email, content, thread_seq, created_ts)"
        " VALUES('eng','1.0','a@x.com','latency spike in the gateway',0,1000)"
    )
    # a titled source alongside it, so the fallback is covered on both shapes
    con.execute(
        "INSERT INTO jira_issues(key, project, author_email, title, content, created_ts)"
        " VALUES('PAY-1','PAY','a@x.com','Latency','p99 is up',1000)"
    )
    con.commit()
    # no build_fts: this is the no-FTS5 build the fallback exists for
    assert [r["ts"] for r in store.search_documents(con, "latency", "slack")] == ["1.0"]
    assert store.count_search(con, "latency", "slack") == 1
    assert [r["key"] for r in store.search_documents(con, "latency", "jira")] == ["PAY-1"]
    assert store.count_search(con, "latency", "jira") == 1
    # a jira hit in the TITLE still sorts ahead of one only in the body
    con.execute(
        "INSERT INTO jira_issues(key, project, author_email, title, content, created_ts)"
        " VALUES('PAY-2','PAY','a@x.com','Rollout','latency mentioned in the body',1000)"
    )
    con.commit()
    assert [r["key"] for r in store.search_documents(con, "latency", "jira")] == ["PAY-1", "PAY-2"]


def test_search_order_by_recency():
    # Slack sort=timestamp -> results ordered by the message's own ts (newest first), NOT relevance.
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(store.SCHEMA)
    for doc_id, ts in [("old", 1000), ("new", 2000), ("mid", 1500)]:
        con.execute(
            "INSERT INTO slack_messages(ts, channel, author_email, content, thread_seq, "
            "created_ts) VALUES (?, 'eng', 'a@x.com', 'quarterly planning notes', 0, ?)",
            (doc_id, ts),
        )
    store.build_fts(con)
    recency = store.search_documents(con, "planning", "slack", order_by="recency")
    assert [r["ts"] for r in recency] == ["new", "mid", "old"], "recency = newest ts first"
    asc = store.search_documents(con, "planning", "slack", order_by="recency_asc")
    assert [r["ts"] for r in asc] == ["old", "mid", "new"], "recency_asc = oldest first"


def test_parse_slack_query():
    from backlot.routers.slack import _parse_slack_query

    # bare terms: no scope, AND semantics
    assert _parse_slack_query("upload csv") == ("upload csv", None, False)
    # a fully quoted query -> phrase
    assert _parse_slack_query('"upload.csv"') == ("upload.csv", None, True)
    # in:#channel -> container scope (not three stray search tokens), and the quote -> phrase
    assert _parse_slack_query('in:#eng-ml "upload.csv"') == ("upload.csv", "eng-ml", True)
    # in:channel (no #) also scopes; order-independent
    assert _parse_slack_query("metadata source in:eng-ml") == ("metadata source", "eng-ml", False)
    # in:@user (a DM) has no container in the channel corpus -> stripped, not mis-scoped as a channel
    assert _parse_slack_query("in:@jonas retention") == ("retention", None, False)


def test_search_channel_scope_and_phrase(db):
    # in:#<channel> narrows to that channel exactly like the container arg; a foreign channel drops it
    from backlot.routers.slack import _parse_slack_query

    terms, container, phrase = _parse_slack_query("in:#general gateway")
    assert container == "general" and not phrase
    scoped = store.search_documents(db, terms, "slack", container=container)
    assert store.search_documents(db, terms, "slack", container="no-such-channel") == []
    # every scoped hit really is in that channel
    assert all(r["channel"] == "general" for r in scoped)


def test_jira_text_from_jql():
    from backlot.routers.atlassian import _text_from_jql

    assert _text_from_jql('project = PAY AND text ~ "latency spike"') == "latency spike"
    assert _text_from_jql("summary ~ 'postmortem'") == "postmortem"
    assert _text_from_jql("description ~ refill") == "refill"
    assert _text_from_jql("project = PAY ORDER BY created") is None


def test_gmail_q_parse_and_match():
    from backlot.routers.google import _parse_gmail_q

    free, ops = _parse_gmail_q("from:ceo@acme.com subject:board has:attachment quarterly")
    assert free == "quarterly"
    assert ops["from"] == ["ceo@acme.com"] and ops["subject"] == ["board"]
    assert ops["has"] == ["attachment"]
    # `in:` is an operator, not free text: unhandled, `in:anywhere` FTS-matched nothing and emptied
    # the mailbox it was meant to widen.
    free, ops = _parse_gmail_q("in:anywhere quarterly")
    assert free == "quarterly" and ops["in"] == ["anywhere"]


@pytest.mark.parametrize(
    "q, free, ops",
    [
        ("subject: rollout", "", {"subject": ["rollout"]}),
        ("subject:\trollout", "", {"subject": ["rollout"]}),
        ("label: inbox", "", {"label": ["inbox"]}),
        ("in:\tsent", "", {"in": ["sent"]}),
        ("after: 2026/10/01", "", {"after": ["1790812800"]}),
        ("before:   2026/10/01", "", {"before": ["1790812800"]}),
        ("newer: 2026/10/01", "", {"after": ["1790812800"]}),
        ("older: 2026/10/01", "", {"before": ["1790812800"]}),
        ("after:\t2026/10/01", "after: 2026/10/01", {}),
        ("has: attachment", "has: attachment", {}),
        ("newer_than: 7d", "newer_than: 7d", {}),
    ],
)
def test_gmail_operators_read_across_whitespace(q, free, ops):
    """Each row is how the comment on `_GMAIL_OP` reads its query."""
    from backlot.routers.google import _parse_gmail_q

    assert _parse_gmail_q(q) == (free, ops)


def test_gmail_folder_query_sees_the_label_a_message_is_served_under(db):
    """A message the corpus gives no labels is served `labelIds: ["INBOX"]`, so `in:inbox` and
    `label:inbox` have to find it — reading only the STATED labels answered "no such mail" about
    every message in Backlot. `in:anywhere` widens the search to spam and trash, which Backlot
    holds none of, so it restricts nothing."""
    from backlot.routers.google import _gmail_query

    total = len(_gmail_query(db, None, None, ""))
    assert total > 0
    assert len(_gmail_query(db, None, None, "in:anywhere")) == total
    assert len(_gmail_query(db, None, None, "in:inbox")) == total
    assert len(_gmail_query(db, None, None, "label:INBOX")) == total
    # A label no message carries is empty, not everything — the filter is still a filter.
    assert _gmail_query(db, None, None, "in:trash") == []


def test_gmail_relative_date_parse():
    # newer_than:/older_than: are operators (relative age), NOT free text — so they neither leak
    # into the FTS term nor drop the real free-text term beside them.
    from backlot.routers.google import _gmail_rel_secs, _parse_gmail_q

    free, ops = _parse_gmail_q("quarterly newer_than:5d older_than:1y")
    assert free == "quarterly"
    assert ops["newer_than"] == ["5d"] and ops["older_than"] == ["1y"]
    assert _gmail_rel_secs("2d") == 2 * 86400
    assert _gmail_rel_secs("3m") == 3 * 30 * 86400
    assert _gmail_rel_secs("1y") == 365 * 86400
    assert _gmail_rel_secs("bogus") is None


def test_gmail_relative_date_query_not_zeroed(db):
    # Unhandled, newer_than:/older_than: fall through as free text, FTS-match nothing, and
    # zero out ANY relative-date query. They must filter by age (anchored to now) like real Gmail.
    from backlot.routers.google import _gmail_query

    total = len(_gmail_query(db, None, None, ""))  # whole (admin-visible) mailbox
    assert total > 0
    # a window wide enough to contain every message == the full set (parsed + honored, not zeroed)
    assert len(_gmail_query(db, None, None, "newer_than:3650000d")) == total
    # "older than ~10000 years ago" excludes everything, but is a real filter — not a parse failure
    assert len(_gmail_query(db, None, None, "older_than:3650000d")) == 0


def test_gmail_newer_older_are_after_before(db):
    """The cases the comment on `_GMAIL_KEY_ALIASES` measures, with a value and empty."""
    from backlot.routers.google import _gmail_q_date, _gmail_query, _parse_gmail_q

    free, ops = _parse_gmail_q("quarterly newer:2026/03/02 older:2026/03/04")
    assert free == "quarterly"
    assert ops == {"after": [_gmail_q_date("2026/03/02")], "before": [_gmail_q_date("2026/03/04")]}
    assert _parse_gmail_q("newer:") == ("newer:", {})
    assert _parse_gmail_q("older:") == ("older:", {})
    total = len(_gmail_query(db, None, None, ""))
    for alias, op in (("newer", "after"), ("older", "before")):
        for day in ("1970/01/02", "2999/01/01"):
            got = _gmail_query(db, None, None, f"{alias}:{day}")
            want = _gmail_query(db, None, None, f"{op}:{day}")
            assert [r["id"] for r in got] == [r["id"] for r in want]
    # Not zeroed: the far-past `newer:` keeps the whole mailbox.
    assert len(_gmail_query(db, None, None, "newer:1970/01/02")) == total


def test_gmail_plus_ends_an_address_or_subject_value():
    """How `_parse_gmail_q` reads a `+` in an operator's value and in free text, row by row as the
    comment on `_GMAIL_PLUS_ENDS` records it."""
    from backlot.routers.google import _GMAIL_NOTHING, _parse_gmail_q

    assert _parse_gmail_q("cc:lena+update") == ("+update", {"cc": ["lena"]})
    assert _parse_gmail_q("from:bob+update") == ("+update", {"from": ["bob"]})
    assert _parse_gmail_q("to:ava+update") == ("+update", {"to": ["ava"]})
    assert _parse_gmail_q("cc:+lena") == ("", {"cc": ["lena"]})
    assert _parse_gmail_q("cc:lena +") == ("", {"cc": ["lena"]})
    assert _parse_gmail_q("+") == ("", {})
    for q in ("cc:lena+", "cc:lena++", "cc:lena+ the", "from:bob+   to:ava+"):
        assert _GMAIL_NOTHING in _parse_gmail_q(q)[1], q
    assert _parse_gmail_q("bcc:lena+news") == ("+news", {"bcc": ["lena"]})
    assert _parse_gmail_q("subject:plan+rollout") == ("+rollout", {"subject": ["plan"]})
    for q in ("bcc:lena+", "subject:c+", "update+", "+update+"):
        assert _GMAIL_NOTHING in _parse_gmail_q(q)[1], q
    assert _parse_gmail_q("cc:lena@acme.com+x") == ("", {"cc": ["lena@acme.com+x"]})
    assert _parse_gmail_q("from:lena+news@acme.com") == ("", {"from": ["lena+news@acme.com"]})
    assert _parse_gmail_q("cc:lena@acme.com++") == ("", {"cc": ["lena@acme.com"]})
    assert _parse_gmail_q("cc:@acme.com+x") == ("+x", {"cc": ["@acme.com"]})
    # A quoted address value keeps its `+` and its quotes, which tell the matcher to read it alone;
    # `label:` keeps its `+` too.
    assert _parse_gmail_q('cc:"lena+x"') == ("", {"cc": ['"lena+x"']})
    assert _parse_gmail_q("label:inbox+x") == ("", {"label": ["inbox+x"]})


def test_gmail_plus_over_the_wire(tmp_path):
    """The rows of the issue's reproduction, over a served mailbox of two messages."""
    from tests._helpers import client_for, tiny_corpus

    base = {"source_type": "gmail", "mailbox": "ava", "author_email": "bob@acme.com"}
    records = [
        {
            **base,
            "doc_id": "plan",
            "title": "Rollout plan",
            "content": "the rollout update",
            "to": "ava@acme.com",
            "cc": "Lena Park <lena@acme.com>",
            "readers": ["ava@acme.com"],
            "created": "2026-03-01T09:00:00Z",
        },
        {
            **base,
            "doc_id": "lunch",
            "title": "Lunch",
            "content": "Friday?",
            "to": "ava@acme.com",
            "cc": "lena@acme.com",
            "readers": ["ava@acme.com"],
            "created": "2026-03-02T09:00:00Z",
        },
    ]
    settings = tiny_corpus(tmp_path, records)
    with client_for(settings, reload=True) as c:
        h = {"Authorization": f"Bearer {settings.admin_token}"}

        def count(q):
            r = c.get("/gmail/v1/users/ava@acme.com/messages", headers=h, params={"q": q})
            return len(r.json().get("messages", []))

        assert count("cc:lena") == 2
        assert count("cc:lena+update") == count("cc:lena +update") == 1
        assert count("from:bob+update") == count("from:bob +update") == 1
        assert count("cc:lena+") == count("cc:lena++") == count("cc:lena+ the") == 0
        assert count("cc:+lena") == count("cc:lena +") == 2
        assert count("cc:lena@acme.com+update") == count("update+") == 0
        assert count("cc:lena@acme.com+") == 2


_OCT_1 = "1790812800"  # 2026-10-01T00:00:00Z


@pytest.mark.parametrize(
    "value, want",
    [
        *((v, _OCT_1) for v in ("2026/10/01", "2026-10-01", "2026/10/1", "2026-10-1")),
        *((v, _OCT_1) for v in ("10/01/2026", "10/1/2026", "10-01-2026", "2026/10/01/")),
        (_OCT_1, _OCT_1),
        ("01/10/2026", "1768003200"),  # 10 January
        ("1", "1"),
        ("01", "1"),
        ("01790812800", _OCT_1),
        ("253402300800", "253402300800"),
        *((v, _OCT_1) for v in ("2026/10/01//", "2026/10/01.", "1790812800,", "2026/10/01:")),
        *((v, _OCT_1) for v in ("02026/10/01", "2026/010/01")),
        ("15/10/2026", "1792022400"),  # 15 October
        ("26/10/05", "1791158400"),
        ("026/10/05", "1791158400"),
        ("2026/10/005", "1791158400"),
        ("9/30/26", "1790726400"),
        ("10/05/26", "1274832000"),  # 26 May 2010
        ("30/9/26", "1916611200"),
        *(
            (v, None)
            for v in ("abc", "2026/13/01", "2026/02/30", "2026/10/32", "2026.10.01")
            + ("2026-10-01T00:00", "0", "-1", '"2026/10/01"', "'2026/10/01'", "(2026/10/01)")
            + ("000", "+1790812800", "1e9", "2026/10-01", "0010/05/2026", "00/01/2026")
            + ("9223372036854", "99999999999999999999")
            + ("\uff12\uff10\uff12\uff16/\uff11\uff10/\uff10\uff11",)
        ),
    ],
)
def test_gmail_after_before_value(value, want):
    """Each value the comment on `_GMAIL_DATE_KEYS` measures, as `_gmail_q_date` reads it."""
    from backlot.routers.google import _gmail_q_date

    assert _gmail_q_date(value) == want


def test_gmail_after_before_parse():
    """A value real cannot read leaves nothing to match, beside a readable one too; a `-` negates
    the operator, or drops it when its value does not read; `after:-` stays a search term."""
    from backlot.routers.google import _GMAIL_NOTHING, _parse_gmail_q

    for q in ("after:abc", "before:2026/02/30", "after:0", 'after:"2026/10/01"'):
        assert _GMAIL_NOTHING in _parse_gmail_q(q)[1], q
    assert _GMAIL_NOTHING in _parse_gmail_q("after:2026/10/01 after:abc")[1]
    assert _parse_gmail_q("-after:2026/10/01") == ("", {"before": [_OCT_1]})
    assert _parse_gmail_q("-after: 2026/10/01") == ("", {"before": [_OCT_1]})
    assert _parse_gmail_q("-after:abc") == _parse_gmail_q("-before:0") == ("", {})
    assert _parse_gmail_q("-before:2026/10/01") == ("", {"after": [_OCT_1]})
    assert _parse_gmail_q("-newer:2026/10/01") == ("", {"before": [_OCT_1]})
    assert _parse_gmail_q("after:-") == ("after:-", {})


def test_gmail_after_before_over_the_wire(tmp_path):
    """The issue's reproduction: three messages, around 1 October."""
    from tests._helpers import client_for, tiny_corpus

    records = [
        {
            "source_type": "gmail",
            "doc_id": d,
            "mailbox": "ava",
            "title": t,
            "content": "Status notes.",
            "author_email": "bob@acme.com",
            "to": "ava@acme.com",
            "label_ids": ["INBOX"],
            "readers": ["ava@acme.com"],
            "created": c,
        }
        for d, t, c in [
            ("sep", "September", "2026-09-15T09:00:00Z"),
            ("oct1", "October 1", "2026-10-01T12:00:00Z"),
            ("oct5", "October 5", "2026-10-05T09:00:00Z"),
        ]
    ]
    settings = tiny_corpus(tmp_path, records)
    with client_for(settings, reload=True) as c:
        h = {"Authorization": f"Bearer {settings.admin_token}"}

        def count(q):
            r = c.get("/gmail/v1/users/ava@acme.com/messages", headers=h, params={"q": q})
            return len(r.json().get("messages", []))

        for q in ("after:2026/10/01", "after:10/01/2026", "after:10-01-2026", "after:2026/10/01/"):
            assert count(q) == 2, q
        for q in ("after:abc", "after:2026/13/01", "after:0", 'after:"2026/10/01"'):
            assert count(q) == 0, q
        assert count("after:2026/10/01 after:abc") == 0
        assert count("-after:2026/10/01") == count("before:2026/10/01") == 1
        assert count("-after:abc") == 3
        assert count("before:99999999999999999999") == 0


@pytest.mark.parametrize(
    "q, free, ops",
    [
        ("-from:bob", "", {"-from": ["bob"]}),
        ("from:-bob", "", {"-from": ["bob"]}),
        ("-FROM:bob", "", {"-from": ["bob"]}),
        ("-from: bob", "", {"-from": ["bob"]}),
        ("from: -bob", "", {"-from": ["bob"]}),
        ("-from:-bob", "", {"-from": ["bob"]}),
        ("--from:bob", "", {"-from": ["bob"]}),
        ("from:--bob", "", {"-from": ["bob"]}),
        ('-from:"bob"', "", {"-from": ['"bob"']}),
        ("-to:ava", "", {"-to": ["ava"]}),
        ("cc:-lena", "", {"-cc": ["lena"]}),
        ("-bcc:lena", "", {"-bcc": ["lena"]}),
        ("-cc:lena+update", "+update", {"-cc": ["lena"]}),
        ('from:"-bob"', "", {"from": ['"-bob"']}),
        ("from:mary-jane", "", {"from": ["mary-jane"]}),
        ("-from:", "", {}),
        ("from:-", "", {}),
        ("-from:-", "", {}),
        ("from:--", "", {}),
        ('-from:""', "", {}),
        ("label:-", "", {"label": ["-"]}),
    ],
)
def test_gmail_a_minus_negates_an_address_operator(q, free, ops):
    """Each row is how the comment on `_GMAIL_NEGATED_ADDRESS` reads its query."""
    from backlot.routers.google import _parse_gmail_q

    assert _parse_gmail_q(q) == (free, ops)


@pytest.mark.parametrize(
    "q, free, ops",
    [
        ("subject: label:inbox", "", {"label": ["inbox"]}),
        ("label: in:sent", "", {"in": ["sent"]}),
        ("in: has:attachment", "", {"has": ["attachment"]}),
        ("from: after:2026/10/01", "", {"after": [_OCT_1]}),
        ("to: before:2026/10/01", "", {"before": [_OCT_1]}),
        ("cc: newer:2026/10/01", "", {"after": [_OCT_1]}),
        ("bcc: older:2026/10/01", "", {"before": [_OCT_1]}),
        ("subject: newer_than:30d", "", {"newer_than": ["30d"]}),
        ("label: older_than:30d", "", {"older_than": ["30d"]}),
        ("subject:label:inbox", "", {"label": ["inbox"]}),
        ("bcc:in:sent", "", {"in": ["sent"]}),
        ("subject:\tlabel:inbox", "", {"label": ["inbox"]}),
        ("in:\tin:sent", "", {"in": ["sent"]}),
        ("subject:  label:inbox", "", {"label": ["inbox"]}),
        ('subject:"" label:inbox', "", {"label": ["inbox"]}),
        ('label:""label:inbox', "", {"label": ["inbox"]}),
        ('label:" "label:inbox', "", {"label": ["inbox"]}),
        ("SUBJECT:LABEL:inbox", "", {"label": ["inbox"]}),
        ("in: before: 2026/10/01", "", {"before": [_OCT_1]}),
        ("subject:older: 2026/10/01", "", {"before": [_OCT_1]}),
        ("subject: subject:probe", "", {"subject": ["subject:probe"]}),
        ("label: from:me", "", {"label": ["from:me"]}),
        ("in: to:me", "", {"in": ["to:me"]}),
        ("from: cc:me", "", {"from": ["cc:me"]}),
        ("subject: label:", "", {"subject": ["label:"]}),
        ('label: label:""', "", {"label": ["label:"]}),
        ('in: label:"inbox"', "", {"in": ['label:"inbox']}),
        ("from: label: inbox", "inbox", {"from": ["label:"]}),
        ("label:label:inbox", "", {"label": ["label:inbox"]}),
        ("subject:\xa0label:inbox", "", {"subject": ["label:inbox"]}),
        ("subject: after:-", "", {"subject": ["after:-"]}),
        ('subject: after:"2026/10/01"', "", {"subject": ['after:"2026/10/01']}),
    ],
)
def test_gmail_an_empty_operator_before_another(q, free, ops):
    """Each row is how the comment on `_GMAIL_EMPTY_BEFORE` reads its query."""
    from backlot.routers.google import _parse_gmail_q

    assert _parse_gmail_q(q) == (free, ops)


@pytest.mark.parametrize(
    "q, ops",
    [
        ("-subject: label:inbox", {"-label": ["inbox"]}),
        ("-label: label:inbox", {"-label": ["inbox"]}),
        ("-in: label:inbox", {"-label": ["inbox"]}),
        ("-from: label:inbox", {"-label": ["inbox"]}),
        ("-from:label:inbox", {"-label": ["inbox"]}),
        ("-subject:\tlabel:inbox", {"-label": ["inbox"]}),
        ("-SUBJECT: LABEL:inbox", {"-label": ["inbox"]}),
        ('-subject:"" label:inbox', {"-label": ["inbox"]}),
        ('-label:" " label:inbox', {"-label": ["inbox"]}),
        ("-cc: has:attachment", {"-has": ["attachment"]}),
        ("-from: in:anywhere", {"-in": ["anywhere"]}),
        ("-from: after:2026/10/01", {"before": [_OCT_1]}),
        ("-to: newer_than:30d", {"older_than": ["30d"]}),
        ("-newer_than:30d", {"older_than": ["30d"]}),
        ("-older_than:30d", {"newer_than": ["30d"]}),
        ("-has:attachment", {"-has": ["attachment"]}),
        # Kept, so the `-` stays on the empty operator.
        ("-from: from:bob", {"-from": ["from:bob"]}),
        ("-subject: subject:plan", {"-subject": ["subject:plan"]}),
        ("-label:label:inbox", {"-label": ["label:inbox"]}),
    ],
)
def test_gmail_a_minus_before_an_empty_operator_has_or_an_age_operator(q, ops):
    """Each row is how the comments on `_GMAIL_NEGATED_EMPTY` and `_GMAIL_AGE_NEGATION` read its
    query."""
    from backlot.routers.google import _parse_gmail_q

    assert _parse_gmail_q(q) == ("", ops)


def test_gmail_empty_operator_over_the_wire(tmp_path):
    """The issue's reproduction: one message in the inbox, one sent."""
    from tests._helpers import client_for, tiny_corpus

    base = {"source_type": "gmail", "mailbox": "ava", "to": "ava@acme.com"}
    records = [
        {**base, "doc_id": "plan", "title": "Rollout plan", "content": "attached",
         "author_email": "bob@acme.com", "label_ids": ["INBOX"], "readers": ["ava@acme.com"],
         "created": "2026-03-01T09:00:00Z",
         "attachments": [{"filename": "plan.pdf", "mime_type": "application/pdf", "content": "x"}]},
        {**base, "doc_id": "lunch", "title": "Lunch", "content": "Friday?",
         "author_email": "mia@acme.com", "label_ids": ["SENT"], "readers": ["ava@acme.com"],
         "created": "2026-03-02T09:00:00Z"},
    ]  # fmt: skip
    settings = tiny_corpus(tmp_path, records)
    with client_for(settings, reload=True) as c:
        h = {"Authorization": f"Bearer {settings.admin_token}"}

        def ids(q):
            r = c.get("/gmail/v1/users/ava@acme.com/messages", headers=h, params={"q": q})
            return {m["id"] for m in r.json().get("messages", [])}

        inbox, sent = ids("label:inbox"), ids("in:sent")
        assert inbox and sent and inbox != sent
        for q in ("subject: label:inbox", "label: label:inbox", "in: label:inbox"):
            assert ids(q) == inbox, q
        assert ids('subject:"" label:inbox') == inbox
        assert ids("label: in:sent") == ids("from: in:sent") == sent
        assert ids("subject:plan") == ids("from:bob") == inbox
        assert ids("subject: subject:plan") == ids("subject: from:bob") == set()
        assert ids('subject: label:"inbox"') == ids('subject: label:""') == set()
        # The readings the comments on `_GMAIL_NEGATED_EMPTY` and `_GMAIL_AGE_NEGATION` measure.
        for q in (
            "-subject: label:inbox",
            "-label: label:inbox",
            "-from: label:inbox",
            "-from:label:inbox",
            '-subject:"" label:inbox',
            "-in:inbox",
            "-has:attachment",
            "-cc: has:attachment",
        ):
            assert ids(q) == sent, q
        assert ids("has:attachment") == inbox
        assert ids("-from: from:bob") == ids("-label:label:inbox") == inbox | sent
        assert ids("-from: in:anywhere") == set()


@pytest.mark.parametrize(
    "q, want",
    [
        ("-subject:plan", ("", {"-subject": ["plan"]})),
        ("-subject: plan", ("", {"-subject": ["plan"]})),
        ("subject:-plan", ("", {"-subject": ["plan"]})),
        ("-label:inbox", ("", {"-label": ["inbox"]})),
        ("-label: inbox", ("", {"-label": ["inbox"]})),
        ("-label:\tinbox", ("", {"-label": ["inbox"]})),
        ('-label: "inbox"', ("", {"-label": ["inbox"]})),
        ('-label:"" inbox', ("", {"-label": ["inbox"]})),
        ("label: -inbox", ("", {"-label": ["inbox"]})),
        ("-label: -inbox", ("", {"-label": ["inbox"]})),
        ('label:"" -probe', ("", {"-label": ["probe"]})),
        ("label:-inbox", ("", {"label": ["-inbox"]})),
        ("-label:-inbox", ("", {"-label": ["-inbox"]})),
        ("-in:sent", ("", {"-in": ["sent"]})),
        ("-in: sent", ("", {"-in": ["sent"]})),
        ("in: -sent", ("", {"-in": ["sent"]})),
        ("in:-sent", ("", {"in": ["-sent"]})),
        ('-subject:""', ("", {})),
        ('-label:" "', ("", {})),
        ("subject:-", ("", {})),
        ("label: -", ("", {})),
        ("-subject:plan+probe", ("+probe", {"-subject": ["plan"]})),
        ("subject: -label:inbox", ("", {"-subject": ["label:inbox"]})),
        ("- from:bob", ("", {"from": ["bob"]})),
        ("NOT from:bob", ("", {"-from": ["bob"]})),
        ("NOT bcc:lena", ("", {"-bcc": ["lena"]})),
        ("NOT label:inbox", ("", {"-label": ["inbox"]})),
        ("NOT -from:bob", ("", {"from": ["bob"]})),
        ("NOT -label:inbox", ("", {"label": ["inbox"]})),
        ("-(from:bob)", ("", {"-from": ["bob"]})),
        ("-( subject:plan )", ("", {"-subject": ["plan"]})),
        ("not from:bob", ("not", {"from": ["bob"]})),
        ("NOT has:attachment", ("", {"-has": ["attachment"]})),
        ("NOT  has:attachment", ("", {"-has": ["attachment"]})),
        ("not has:attachment", ("not", {"has": ["attachment"]})),
        ("NOT after:2026/10/01", ("", {"before": [_OCT_1]})),
        ("NOT newer:2026/10/01", ("", {"before": [_OCT_1]})),
        ("NOT before:2026/10/01", ("", {"after": [_OCT_1]})),
        ("NOT newer_than:30d", ("", {"older_than": ["30d"]})),
        ("NOT older_than:30d", ("", {"newer_than": ["30d"]})),
        ("NOT NOT from:bob", ("", {"from": ["bob"]})),
        ("NOT NOT NOT from:bob", ("", {"-from": ["bob"]})),
        ("NOT NOT NOT NOT from:bob", ("", {"from": ["bob"]})),
        ("NOT NOT -from:bob", ("", {"-from": ["bob"]})),
        ("NOT NOT NOT -from:bob", ("", {"from": ["bob"]})),
        ("NOT - from:bob", ("", {"-from": ["bob"]})),
        ("NOT -has:attachment", ("", {"has": ["attachment"]})),
        ("-(has:attachment)", ("", {"-has": ["attachment"]})),
        ("-(newer_than:30d)", ("", {"older_than": ["30d"]})),
        ("-(-after:2026/10/01)", ("", {"before": [_OCT_1]})),
        ("-((from:bob))", ("", {"-from": ["bob"]})),
        ("-(from:bob", ("", {"-from": ["bob"]})),
        ("-(-from:bob)", ("", {"-from": ["bob"]})),
        ("--(from:bob)", ("", {"-from": ["bob"]})),
        ("NOT(from:bob)", ("", {"-from": ["bob"]})),
        ("NOT ((from:bob))", ("", {"-from": ["bob"]})),
        ("(from:bob)", ("", {"from": ["bob"]})),
        ("( from:bob )", ("", {"from": ["bob"]})),
        ("(from:bob", ("", {"from": ["bob"]})),
        ("from:bob)", ("", {"from": ["bob"]})),
        ("probe(from:bob)", ("probe", {"from": ["bob"]})),
        ("-(from:bob)to:ava", ("", {"-from": ["bob"], "to": ["ava"]})),
        ("-()", ("", {})),
        ("-( )", ("", {})),
        ("(", ("", {})),
        ("-(from:bob to:ava)", ("", {"-()": ["from:bob to:ava"]})),
        ("-( from:bob to:ava )", ("", {"-()": [" from:bob to:ava "]})),
        ("-(from:bob to:ava", ("", {"-()": ["from:bob to:ava"]})),
        ("-(probe from:bob)", ("", {"-()": ["probe from:bob"]})),
        ("probe -(from:bob to:ava)", ("probe", {"-()": ["from:bob to:ava"]})),
        ("-(-(from:bob to:ava))", ("", {"-()": ["from:bob to:ava"]})),
        ("--(from:bob to:ava)", ("", {"-()": ["from:bob to:ava"]})),
        ("-(NOT from:bob to:ava)", ("", {"-()": ["NOT from:bob to:ava"]})),
        ("NOT NOT -(from:bob to:ava)", ("", {"-()": ["from:bob to:ava"]})),
        ("NOT (from:bob to:ava)", ("", {"-from": ["bob"], "to": ["ava"]})),
        ("NOT ((from:bob to:ava))", ("", {"-from": ["bob"], "to": ["ava"]})),
        ("NOT (-from:bob to:ava)", ("", {"from": ["bob"], "to": ["ava"]})),
        ("NOT -(from:bob to:ava)", ("", {"from": ["bob"], "to": ["ava"]})),
        ("label:-", ("", {"label": ["-"]})),
        ("mary-jane", ("mary-jane", {})),
        # A capital `NOT` or a leading `-` in front of a word is `_GMAIL_NOT_WORD`.
        ("NOT probe", ("", {"-w": ["probe"]})),
        ("-probe", ("", {"-w": ["probe"]})),
        ("--probe", ("", {"-w": ["probe"]})),
        ('-"probe"', ("", {"-w": ["probe"]})),
        ("-(probe)", ("", {"-w": ["probe"]})),
        ("not probe", ("not probe", {})),
        # The runs of `NOT`s `_gmail_not_words` reads.
        ("NOT", ("", {"": ["NOT"]})),
        ("NOT NOT", ("", {})),
        ("NOT NOT NOT", ("", {"": ["NOT NOT NOT"]})),
        ("NOT ()", ("", {"": ["NOT"]})),
        ("(NOT)", ("", {"": ["NOT"]})),
        ("-(NOT)", ("", {})),
        ("-(NOT NOT)", ("", {})),
        ("NOT -", ("", {"": ["NOT -"]})),
        ('NOT ""', ("", {"": ['NOT ""']})),
        ('NOT NOT ""', ("", {})),
        ("probe NOT", ("probe", {"": ["probe NOT"]})),
        ("NOT NOT probe", ("probe", {})),
        ("-NOT probe", ("probe", {})),
        ('NOT subject:"', ("", {"": ["NOT"]})),
        ("-NOT subject:", ("", {})),
        ('-NOT subject:"', ("", {})),
        ("NOT -NOT label:", ("", {"": ["NOT -NOT"]})),
        ("probe NOT -from:bob", ("probe", {"from": ["bob"]})),
        # The no-break spaces the comments at `_GMAIL_SPACED_NBSP` and `_GMAIL_GROUP_PIECE` measure.
        ("probe\u00a0from:bob", ("probe\u00a0from:bob", {})),
        ("from:bob\u00a0to:ava", ("", {"": ["bob\u00a0to:ava"]})),
        ("from:\u00a0bob", ("", {"": ["\u00a0bob"]})),
        ("from:bob\u00a0", ("", {"": ["bob\u00a0"]})),
        ("from:bob \u00a0", ("", {"from": ["bob"]})),
        ("has:\u00a0attachment", ("", {"": ["\u00a0attachment"]})),
        ("-from:bob\u00a0to:ava", ("", {})),
        ("-label:inbox\u00a0", ("", {})),
        ("subject:plan\u00a0", ("", {"subject": ["plan"]})),
        ("NOT\u00a0", ("NOT\u00a0", {})),
        ("NOT\u00a0from:bob", ("NOT\u00a0from:bob", {})),
        ("NOT\u00a0NOT from:bob", ("not", {"from": ["bob"]})),
        # A quoted operator (`_GMAIL_QUOTED_OPERATOR`) and a lone quote before an unclosed group
        # (`_GMAIL_LONE_QUOTE_UNCLOSED`).
        ('"(from:bob)"', ("", {"from": ["bob"]})),
        ('"(-from:bob)"', ("", {"from": ["bob"]})),
        ('-"(from:bob)"', ("", {"-from": ["bob"]})),
        ('probe "(from:bob)"', ("probe", {"from": ["bob"]})),
        ('"(FROM:bob)"', ("", {"from": ["bob"]})),
        ('-"(FROM:bob)"', ("", {"-from": ["bob"]})),
        ('NOT "FROM:bob"', ("", {"-from": ["bob"]})),
        ('"IN:sent"', ("", {"in": ["sent"]})),
        ('subject:" (from:bob', ("", {"": ['subject:" (from:bob']})),
        ('probe subject:" (from:bob', ("", {"": ['subject:" (from:bob']})),
        ('-subject:" (from:bob', ("", {})),
        ("-(probe NOT)", ("", {"-w": ["probe"]})),
        ('NOT "" probe', ("", {"-w": ["probe"]})),
        ("-\u00a0probe", ("", {"-w": ["\u00a0probe"]})),
    ],
)
def test_gmail_negation_and_groups(q, want):
    """The readings the comments on `_GMAIL_NEGATABLE`, `_GMAIL_NOT` and `_GMAIL_GROUP_PIECE`
    measure; `-()` holds a negated group of several terms, which `_gmail_query` reads."""
    from backlot.routers.google import _parse_gmail_q

    assert _parse_gmail_q(q) == want


def test_gmail_negation_and_groups_over_the_wire(tmp_path):
    """The issue's reproduction: one message in the inbox from bob, one sent by mia."""
    from tests._helpers import client_for, tiny_corpus

    base = {"source_type": "gmail", "mailbox": "ava", "to": "ava@acme.com"}
    records = [
        {**base, "doc_id": "plan", "title": "Rollout plan", "content": "attached",
         "author_email": "bob@acme.com", "label_ids": ["INBOX"], "readers": ["ava@acme.com"],
         "created": "2026-03-01T09:00:00Z"},
        {**base, "doc_id": "lunch", "title": "Lunch", "content": "Friday?",
         "author_email": "mia@acme.com", "label_ids": ["SENT"], "readers": ["ava@acme.com"],
         "created": "2026-03-02T09:00:00Z"},
    ]  # fmt: skip
    settings = tiny_corpus(tmp_path, records)
    with client_for(settings, reload=True) as c:
        h = {"Authorization": f"Bearer {settings.admin_token}"}

        def ids(q, listing="messages"):
            r = c.get(f"/gmail/v1/users/ava@acme.com/{listing}", headers=h, params={"q": q})
            return {m["id"] for m in r.json().get(listing, [])}

        every = ids("")
        for plain, negated in (
            ("subject:plan", ("-subject:plan", "-subject: plan", "subject:-plan")),
            ("label:inbox", ("-label:inbox", "-label: inbox", "label: -inbox", "NOT label:inbox")),
            ("in:sent", ("-in:sent", "-in: sent", "in: -sent", "-(in:sent)")),
            ("from:bob", ("NOT from:bob", "-(from:bob)")),
        ):
            matched = ids(plain)
            assert matched and matched != every, plain
            for q in negated:
                assert ids(q) == every - matched, q
        assert ids("- from:bob") == ids("NOT -from:bob") == ids("from:bob")
        assert ids("in:anywhere") == every
        assert ids("label:-inbox") == ids("in:-sent") == ids("-in:anywhere") == set()
        both = ids("from:bob label:inbox")
        assert both and both != every
        for q in ("-(from:bob label:inbox)", "-((from:bob) label:inbox)", "-(from:bob label:inbox"):
            assert ids(q) == every - both, q
        assert ids("-(attached from:bob)") == every - ids("attached from:bob")
        assert ids("NOT -(from:bob label:inbox)") == both
        threads = ids("", "threads") - ids("from:bob label:inbox", "threads")
        assert ids("-(from:bob label:inbox)", "threads") == threads
        assert ids("NOT (from:bob label:sent)") == ids("-from:bob label:sent")
        assert ids("-(from:bob)-(label:inbox)") == every
        assert ids("(from:bob)-(label:inbox)") == set()


def test_gmail_bare_not_and_no_break_space_over_the_wire(tmp_path):
    """The issue's searches on its three messages, as the comments at `_gmail_not_words`,
    `_GMAIL_QUOTED_OPERATOR`, `_GMAIL_LONE_QUOTE_UNCLOSED` and `_GMAIL_GROUP_PIECE` measure them."""
    from tests._helpers import client_for, tiny_corpus

    base = {
        "source_type": "gmail",
        "mailbox": "ava",
        "readers": ["ava@acme.com"],
        "visibility": "public",
    }
    records = [
        {**base, "doc_id": "plan", "title": "Rollout plan", "content": "probe final",
         "author_email": "bob@acme.com", "to": "ava@acme.com", "label_ids": ["INBOX"],
         "created": "2026-03-01T09:00:00Z"},
        {**base, "doc_id": "deck", "title": "Q4 deck", "content": "not final",
         "author_email": "bob@acme.com", "to": "ava@acme.com", "label_ids": ["INBOX"],
         "created": "2026-03-02T09:00:00Z"},
        {**base, "doc_id": "lunch", "title": "Lunch", "content": "probe Friday?",
         "author_email": "ava@acme.com", "to": "bob@acme.com", "label_ids": ["SENT"],
         "created": "2026-10-15T09:00:00Z"},
    ]  # fmt: skip
    settings = tiny_corpus(tmp_path, records)
    with client_for(settings, reload=True) as c:
        h = {"Authorization": f"Bearer {settings.admin_token}"}

        def ids(q):
            r = c.get("/gmail/v1/users/ava@acme.com/messages", headers=h, params={"q": q})
            return {m["id"] for m in r.json().get("messages", [])}

        every = ids("")
        from_bob = ids("from:bob")
        probe = ids("probe")
        assert from_bob and probe and from_bob != every and probe != every
        not_from_bob = every - from_bob
        for q in ("NOT", "NOT ()", "NOT -", "(NOT)", "probe NOT", 'NOT subject:"'):
            assert ids(q) == set(), q
        for q in ("NOT NOT", "-(NOT)", '-NOT subject:"'):
            assert ids(q) == every, q
        assert ids("NOT NOT probe") == ids("-NOT probe") == probe
        assert ids("NOT -from:bob") == from_bob
        assert ids("probe NOT -from:bob") == probe & from_bob
        assert ids("NOT -label:inbox") == ids("label:inbox")
        nbsp = "\u00a0"
        for q in (
            f"probe{nbsp}from:bob",
            f"from:bob{nbsp}to:ava",
            f"NOT{nbsp}from:bob",
            f"from:{nbsp}bob",
        ):
            assert ids(q) == set(), q
        assert ids(f"-(from:bob{nbsp}to:ava)") == every
        assert ids(f"NOT{nbsp}NOT from:bob") == ids("not from:bob")
        assert ids('"(from:bob)"') == ids('"(-from:bob)"') == from_bob
        assert ids('subject:" (from:bob') == set()
        assert ids('subject:" (from:bob)') == from_bob
        assert ids('-"(from:bob)"') == not_from_bob
        assert ids('"(from:bob to:ava)"') == set()
        assert ids('-subject:" (from:bob') == every


def test_github_search_q_parse():
    """One parser, a qualifier vocabulary per endpoint: a key the endpoint does not honor stays
    free text, so the FTS still gets a chance at it."""
    from backlot.routers.github import _GH_CODE_QUALS, _GH_ISSUE_QUALS, _parse_q

    free, quals = _parse_q("repo:acme/gateway is:pr state:closed refill bug", _GH_ISSUE_QUALS)
    assert free == "refill bug"
    assert quals["repo"] == ["acme/gateway"] and quals["is"] == ["pr"]
    assert quals["state"] == ["closed"]

    free, quals = _parse_q("repo:acme/core extension:py in:path INC-0E0C46", _GH_CODE_QUALS)
    assert free == "INC-0E0C46"
    assert quals == {"repo": ["acme/core"], "extension": ["py"], "in": ["path"]}

    # `state:` says nothing about a file, so code search leaves it as text rather than swallowing it
    assert _parse_q("state:closed thing", _GH_CODE_QUALS) == ("state:closed thing", {})


def test_fts_notion_search(db):
    # the SAMPLE 'Notion On-call Runbook' body mentions dashboards
    rows = store.search_documents(db, "dashboards", "notion")
    assert rows and any(r["id"] == served_id("notion", "nt-runbook") for r in rows)
    assert all("teamspace" in r.keys() for r in rows)  # source-scoped to notion's own table


def test_fts_s3_search(db):
    rows = store.search_documents(db, "dashboards", source_type="s3")
    assert any(r["key"] == "runbooks/oncall.md" for r in rows)


def test_fts_s3_acl_scoped(db, acl, tokens):
    # the group-restricted object is invisible to a non-member (ava is engineering, not people).
    # First assert it IS findable unfiltered, so the ==0 below proves ACL — not a vacuous no-match.
    assert store.count_search(db, "band", source_type="s3", visible_ids=None) >= 1
    ava_ids = acl.visible_ids(db, acl.resolve(tokens["ava@acme.com"]))
    assert store.count_search(db, "band", source_type="s3", visible_ids=ava_ids) == 0


# --- Linear ----------------------------------------------------------------------


def test_fts_linear_search(db):
    rows = store.search_documents(db, "token-bucket", "linear")
    assert rows and any(r["id"] == served_id("linear", "lin-rl") for r in rows)
    assert all("team" in r.keys() for r in rows)  # source-scoped to linear's own table


def test_fts_linear_scoped_to_one_team(db):
    """`container` on a Linear search is the team, so a term present in both teams narrows."""
    assert store.count_search(db, "states", "linear", container="design") >= 1
    assert store.count_search(db, "states", "linear", container="engineering") == 0


# --- fireflies: keyword x scope --------------------------------------------------
# Fireflies' `keyword` is scoped by `scope` (title | sentences | all) rather than being one
# index over everything, so it does not go through search_documents; these pin that the three
# values really do search different text. `content` is the sentences concatenated, which is what
# makes "sentences" a column match and keeps FTS (title+content) meaningful for this source too.


def test_fireflies_transcripts_are_in_their_own_fts_index(db):
    rows = store.search_documents(db, "batching", "fireflies")
    assert {r["id"] for r in rows} == {served_id("fireflies", "ff-discovery")}
    assert store.search_documents(db, "zqxjkbrqznope", "fireflies") == []


def test_fireflies_fts_indexes_the_sentence_text(db):
    """The transcript is indexed through `content`, i.e. through its sentences — so a word only
    ever spoken (never in a title or a summary) is still findable."""
    rows = store.search_documents(db, "crosstalk", "fireflies")
    assert {r["id"] for r in rows} == {served_id("fireflies", "ff-discovery")}


def test_fireflies_scope_title_ignores_the_transcript_body(db):
    # "selects" is spoken in ff-allhands but appears in no title
    assert store.list_fireflies_transcripts(db, keyword="selects", scope="title") == []
    assert [
        r["id"] for r in store.list_fireflies_transcripts(db, keyword="selects", scope="sentences")
    ] == [served_id("fireflies", "ff-allhands")]


def test_fireflies_scope_sentences_ignores_the_title(db):
    # "all-hands" is the title of ff-allhands and is never spoken in it
    assert store.list_fireflies_transcripts(db, keyword="all-hands", scope="sentences") == []
    assert [
        r["id"] for r in store.list_fireflies_transcripts(db, keyword="all-hands", scope="title")
    ] == [served_id("fireflies", "ff-allhands")]


def test_fireflies_scope_all_is_the_union(db):
    title_hits = {
        r["id"] for r in store.list_fireflies_transcripts(db, keyword="latency", scope="title")
    }
    sent_hits = {
        r["id"] for r in store.list_fireflies_transcripts(db, keyword="latency", scope="sentences")
    }
    all_hits = {
        r["id"] for r in store.list_fireflies_transcripts(db, keyword="latency", scope="all")
    }
    assert all_hits == title_hits | sent_hits
    assert title_hits and sent_hits  # both sides actually contribute


def test_fireflies_scope_defaults_to_all(db):
    assert {r["id"] for r in store.list_fireflies_transcripts(db, keyword="latency")} == {
        r["id"] for r in store.list_fireflies_transcripts(db, keyword="latency", scope="all")
    }


def test_fireflies_summary_prose_is_not_keyword_searchable_as_a_sentence(db):
    """The summary is auto-notes, not speech. `scope: sentences` must not match words that only
    appear there, or a RAG consumer would attribute them to a speaker."""
    row = store.get_document(db, "fireflies", served_id("fireflies", "ff-discovery"))
    assert "sub-300ms" in (row["summary"] or "")
    assert store.list_fireflies_transcripts(db, keyword="sub-300ms", scope="sentences") == []
