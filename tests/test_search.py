"""Full-text search: store.build_fts + search_documents (FTS5) over the SAMPLE corpus.

The `db` fixture is built via backlot.importer.byo.load, which now builds the docs_fts index, so
these exercise the real FTS path (search.messages / confluence CQL both sit on search_documents).
"""

from backlot import store


def test_fts_index_built(db):
    assert store._has_fts(db), "importer should have built the docs_fts full-text index"


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


def test_fts_acl_scoped(db, acl, tokens):
    # 'compensation' appears only in the group-restricted People page; a non-member can't find it
    admin_hits = store.count_search(db, "compensation", "confluence", visible_ids=None)
    bob_ids = acl.visible_ids(db, acl.resolve(tokens["bob@acme.com"]))  # not in 'people'
    assert admin_hits >= 1
    assert store.count_search(db, "compensation", "confluence", visible_ids=bob_ids) == 0


def test_fts_source_aware_index(db):
    # the importer rebuilds docs_fts with the indexed `src` column → fast source-intersection
    assert store._fts_has_src(db)
    assert (
        store._fts_match("latency spike", "jira", has_src=True)
        == 'src:srcjira AND ("latency" AND "spike")'
    )
    # 'gateway' is in slack + confluence (SAMPLE); each source-scoped search returns only its
    # own rows, and a source whose title/content lacks the term returns nothing
    sl = store.search_documents(db, "gateway", "slack")
    cf = store.search_documents(db, "gateway", "confluence")
    assert sl and all("channel" in r.keys() for r in sl)
    assert cf and all("space" in r.keys() for r in cf)
    assert store.search_documents(db, "gateway", "github") == []


def test_fts_drive_source(db):
    # Drive is in the source-aware index, so fullText search hits it via FTS (title + content)
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
    assert (
        store._fts_match("upload csv", "slack", has_src=True)
        == 'src:srcslack AND ("upload" AND "csv")'
    )
    assert (
        store._fts_match("upload csv", "slack", has_src=True, phrase=True)
        == 'src:srcslack AND ("upload csv")'
    )


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
            "INSERT INTO slack_messages(doc_id, channel, author_email, title, content, thread_seq, "
            "created_ts) VALUES (?, 'eng', 'a@x.com', '', ?, 0, 1000)",
            (doc_id, content),
        )
    store.build_fts(con)
    hits = store.search_documents(con, "upload.csv", "slack", phrase=True)
    assert [h["doc_id"] for h in hits][:2] == ["d_literal", "d_space"], (
        "the doc literally containing 'upload.csv' must rank first"
    )
    # a plain (non-phrase) search does not reorder — it only ANDs the tokens
    plain = store.search_documents(con, "upload.csv", "slack")
    assert {h["doc_id"] for h in plain} == {"d_literal", "d_space"}


def test_search_order_by_recency():
    # Slack sort=timestamp -> results ordered by the message's own ts (newest first), NOT relevance.
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(store.SCHEMA)
    for doc_id, ts in [("old", 1000), ("new", 2000), ("mid", 1500)]:
        con.execute(
            "INSERT INTO slack_messages(doc_id, channel, author_email, title, content, thread_seq, "
            "created_ts) VALUES (?, 'eng', 'a@x.com', '', 'quarterly planning notes', 0, ?)",
            (doc_id, ts),
        )
    store.build_fts(con)
    recency = store.search_documents(con, "planning", "slack", order_by="recency")
    assert [r["doc_id"] for r in recency] == ["new", "mid", "old"], "recency = newest ts first"
    asc = store.search_documents(con, "planning", "slack", order_by="recency_asc")
    assert [r["doc_id"] for r in asc] == ["old", "mid", "new"], "recency_asc = oldest first"


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


def test_gmail_relative_date_parse():
    # newer_than:/older_than: are operators (relative age), NOT free text — so they neither leak
    # into the FTS term nor drop the real free-text term beside them.
    from backlot.routers.google import _parse_gmail_q, _gmail_rel_secs

    free, ops = _parse_gmail_q("quarterly newer_than:5d older_than:1y")
    assert free == "quarterly"
    assert ops["newer_than"] == ["5d"] and ops["older_than"] == ["1y"]
    assert _gmail_rel_secs("2d") == 2 * 86400
    assert _gmail_rel_secs("3m") == 3 * 30 * 86400
    assert _gmail_rel_secs("1y") == 365 * 86400
    assert _gmail_rel_secs("bogus") is None


def test_gmail_relative_date_query_not_zeroed(db):
    # Regression: newer_than:/older_than: used to fall through as free text, FTS-match nothing, and
    # zero out ANY relative-date query. They must filter by age (anchored to now) like real Gmail.
    from backlot.routers.google import _gmail_query

    total = len(_gmail_query(db, None, None, ""))  # whole (admin-visible) mailbox
    assert total > 0
    # a window wide enough to contain every message == the full set (parsed + honored, not zeroed)
    assert len(_gmail_query(db, None, None, "newer_than:3650000d")) == total
    # "older than ~10000 years ago" excludes everything, but is a real filter — not a parse failure
    assert len(_gmail_query(db, None, None, "older_than:3650000d")) == 0


def test_github_issue_q_parse():
    from backlot.routers.github import _parse_issue_q

    free, quals = _parse_issue_q("repo:acme/gateway is:pr state:closed refill bug")
    assert free == "refill bug"
    assert quals["repo"] == ["acme/gateway"] and quals["is"] == ["pr"]
    assert quals["state"] == ["closed"]


def test_fts_notion_search(db):
    # the SAMPLE 'Notion On-call Runbook' body mentions dashboards
    rows = store.search_documents(db, "dashboards", "notion")
    assert rows and any(r["doc_id"] == "nt-runbook" for r in rows)
    assert all("teamspace" in r.keys() for r in rows)  # source-scoped to notion's own table


def test_fts_notion_acl_scoped(db, acl, tokens):
    # 'confidential' appears only in the group-restricted people-ops page (nt-secret);
    # an engineer (ava) can't find it, admin can
    assert store.count_search(db, "confidential", "notion", visible_ids=None) >= 1
    ava_ids = acl.visible_ids(db, acl.resolve(tokens["ava@acme.com"]))  # not in 'people'
    assert store.count_search(db, "confidential", "notion", visible_ids=ava_ids) == 0


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
    assert rows and any(r["doc_id"] == "lin-rl" for r in rows)
    assert all("team" in r.keys() for r in rows)  # source-scoped to linear's own table


def test_fts_linear_acl_scoped(db, acl, tokens):
    # 'rotation' appears only in the reader-restricted issue; assert it IS findable unfiltered
    # first, so the ==0 below proves the ACL rather than a vacuous no-match.
    assert store.count_search(db, "rotation", "linear", visible_ids=None) >= 1
    ava_ids = acl.visible_ids(db, acl.resolve(tokens["ava@acme.com"]))
    assert store.count_search(db, "rotation", "linear", visible_ids=ava_ids) == 0


def test_fts_linear_scoped_to_one_team(db):
    """`container` on a Linear search is the team, so a term present in both teams narrows."""
    assert store.count_search(db, "states", "linear", container="design") >= 1
    assert store.count_search(db, "states", "linear", container="engineering") == 0


# --- fireflies: keyword x scope --------------------------------------------------
# Fireflies' `keyword` is scoped by `scope` (title | sentences | all) rather than being one
# index over everything, so it does not go through search_documents; these pin that the three
# values really do search different text. `content` is the sentences concatenated, which is what
# makes "sentences" a column match and keeps FTS (title+content) meaningful for this source too.


def test_fireflies_transcripts_are_in_the_shared_fts_index(db):
    rows = store.search_documents(db, "batching", "fireflies")
    assert {r["doc_id"] for r in rows} == {"ff-discovery"}
    assert store.search_documents(db, "zqxjkbrqznope", "fireflies") == []


def test_fireflies_fts_indexes_the_sentence_text(db):
    """The transcript is indexed through `content`, i.e. through its sentences — so a word only
    ever spoken (never in a title or a summary) is still findable."""
    rows = store.search_documents(db, "crosstalk", "fireflies")
    assert {r["doc_id"] for r in rows} == {"ff-discovery"}


def test_fireflies_scope_title_ignores_the_transcript_body(db):
    # "selects" is spoken in ff-allhands but appears in no title
    assert store.list_fireflies_transcripts(db, keyword="selects", scope="title") == []
    assert [
        r["doc_id"]
        for r in store.list_fireflies_transcripts(db, keyword="selects", scope="sentences")
    ] == ["ff-allhands"]


def test_fireflies_scope_sentences_ignores_the_title(db):
    # "all-hands" is the title of ff-allhands and is never spoken in it
    assert store.list_fireflies_transcripts(db, keyword="all-hands", scope="sentences") == []
    assert [
        r["doc_id"]
        for r in store.list_fireflies_transcripts(db, keyword="all-hands", scope="title")
    ] == ["ff-allhands"]


def test_fireflies_scope_all_is_the_union(db):
    title_hits = {
        r["doc_id"] for r in store.list_fireflies_transcripts(db, keyword="latency", scope="title")
    }
    sent_hits = {
        r["doc_id"]
        for r in store.list_fireflies_transcripts(db, keyword="latency", scope="sentences")
    }
    all_hits = {
        r["doc_id"] for r in store.list_fireflies_transcripts(db, keyword="latency", scope="all")
    }
    assert all_hits == title_hits | sent_hits
    assert title_hits and sent_hits  # both sides actually contribute


def test_fireflies_scope_defaults_to_all(db):
    assert {r["doc_id"] for r in store.list_fireflies_transcripts(db, keyword="latency")} == {
        r["doc_id"] for r in store.list_fireflies_transcripts(db, keyword="latency", scope="all")
    }


def test_fireflies_summary_prose_is_not_keyword_searchable_as_a_sentence(db):
    """The summary is auto-notes, not speech. `scope: sentences` must not match words that only
    appear there, or a RAG consumer would attribute them to a speaker."""
    assert "sub-300ms" in (store.get_document(db, "fireflies", "ff-discovery")["summary"] or "")
    assert store.list_fireflies_transcripts(db, keyword="sub-300ms", scope="sentences") == []
