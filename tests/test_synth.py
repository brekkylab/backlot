import hashlib
from urllib.parse import urlparse

from backlot import synth

DOC = "dsid_00908a2dda4b4d359194a091019e8367"
DOC2 = "dsid_f9591843028149bdb47f7c3a70b3baa1"


def test_hnum_and_timestamps_are_deterministic():
    assert synth.hnum(DOC) == synth.hnum(DOC)
    assert synth.epoch(DOC) == synth.epoch(DOC)
    ts = synth.epoch(DOC)
    assert synth.BASE_EPOCH <= ts < synth.BASE_EPOCH + synth.TIME_RANGE


def test_distinct_docs_get_distinct_values():
    assert synth.slack_ts(DOC) != synth.slack_ts(DOC2)
    assert synth.github_number(DOC) != synth.github_number(DOC2)
    assert synth.confluence_id(DOC) != synth.confluence_id(DOC2)


def test_slack_ts_format():
    ts = synth.slack_ts(DOC)
    secs, micro = ts.split(".")
    assert secs.isdigit() and len(micro) == 6


def test_channel_id_stable_per_name():
    assert synth.slack_channel_id("general") == synth.slack_channel_id("general")
    assert synth.slack_channel_id("general") != synth.slack_channel_id("random")
    assert synth.slack_channel_id("general").startswith("C")


def test_time_formats():
    ts = 1712343600  # 2024-04-05T19:00:00Z
    assert synth.rfc3339(ts) == "2024-04-05T19:00:00Z"
    assert synth.rfc3339_millis(ts) == "2024-04-05T19:00:00.000Z"
    assert synth.jira_datetime(ts).endswith("+0000")
    assert synth.rfc2822(ts).endswith("+0000")


def test_account_id_and_login():
    assert synth.atlassian_account_id("ava.chen@x.com").startswith("5b")
    assert synth.github_login("ava.chen@x.com") == "ava-chen"


def test_notion_id_is_stable_uuid():
    assert synth.notion_id("n-page") == synth.notion_id("n-page")
    assert synth.notion_id("n-page") != synth.notion_id("n-other")
    a = synth.notion_id("n-page")
    assert len(a) == 36 and a.count("-") == 4


def test_notion_blocks_roundtrip_content_verbatim():
    content = "# Title\n\nA paragraph.\n\n- one\n- two"
    blocks = synth.notion_blocks("n-page", content)
    assert blocks and all(b["object"] == "block" and "id" in b for b in blocks)
    assert synth.notion_blocks_to_text(blocks) == content
    # block ids are deterministic and per-position
    assert blocks[0]["id"] == synth.notion_block_id("n-page", 0)
    assert blocks[0]["type"] == "heading_1"


# --- S3 tests ---


def test_s3_access_key_id_is_stable_and_shaped():
    ak = synth.s3_access_key_id("usr-abc")
    assert ak.startswith("AKIA") and len(ak) == 20 and ak.isalnum() and ak.upper() == ak
    assert synth.s3_access_key_id("usr-abc") == ak  # stable
    assert synth.s3_access_key_id("usr-xyz") != ak  # per-token


def test_s3_secret_access_key_is_stable_and_shaped():
    sk = synth.s3_secret_access_key("usr-abc")
    assert len(sk) == 40 and synth.s3_secret_access_key("usr-abc") == sk
    assert synth.s3_secret_access_key("usr-xyz") != sk


def test_s3_etag_is_quoted_md5_of_content():
    etag = synth.s3_etag("o1", "hello")
    assert etag == '"' + hashlib.md5(b"hello").hexdigest() + '"'


def test_s3_timestamps():
    assert synth.s3_iso(1_700_000_000).endswith("Z") and "T" in synth.s3_iso(1_700_000_000)
    assert synth.s3_http_date(1_700_000_000).endswith(" GMT")


def test_confluence_space_key_unique_for_colliding_names():
    # initials alone collide; the hash suffix must disambiguate
    a = synth.confluence_space_key("eng-serving-runtime")
    b = synth.confluence_space_key("eng-sre/runbooks")
    assert a != b
    assert synth.confluence_space_key("eng-serving-runtime") == a  # deterministic


def test_jira_project_key_unique_for_colliding_names():
    a = synth.jira_project_key("eng-serving-runtime")
    b = synth.jira_project_key("eng-sre/runbooks")
    assert a != b
    assert synth.jira_project_key("eng-serving-runtime") == a  # deterministic


def test_gmail_message_id_matches_the_real_id_shape():
    """Measured against the live API: Gmail hands out 16 lowercase hex digits, and it rejects an id
    whose integer value is >= 2**63 with 400 "Invalid id value" (`7fffffffffffffff` resolves,
    `8000000000000000` does not). So the derivation has to stay inside 63 bits — the pre-existing
    `gmail_id` does not, and 50.0% of the bench corpus's 556,238 messages hash above the line."""
    mid = synth.gmail_message_id(DOC)
    assert len(mid) == 16
    assert all(c in "0123456789abcdef" for c in mid)
    assert int(mid, 16) < 2**63


def test_gmail_message_id_is_stable_and_distinct():
    assert synth.gmail_message_id(DOC) == synth.gmail_message_id(DOC)
    assert synth.gmail_message_id(DOC) != synth.gmail_message_id(DOC2)


def test_gmail_message_id_stays_in_range_across_many_docs():
    """The 63-bit ceiling is the whole point, so it is asserted over enough ids that a missing mask
    could not slip through: unmasked, about half of these would be over."""
    ids = [synth.gmail_message_id(f"dsid_{i:032x}") for i in range(2000)]
    assert all(int(m, 16) < 2**63 for m in ids)
    assert len(set(ids)) == len(ids)


def test_linear_url_is_the_real_vendor_domain():
    """Regression: a rename's blind substitution once turned this into `linear.backlot`. Asserted
    on the parsed host (no trailing slash) rather than a URL literal, because the vulnerable
    pattern is the literal characters `app` immediately followed by a slash — spelling that
    combination anywhere, even in a comment, makes a repeat of the bug rewrite it right alongside
    the code it guards. A bare `"linear.app"` with nothing appended has no slash for the pattern
    to land on, so it survives. The `"backlot" not in host` half is the one that actually
    matters: a rename can only ever INTRODUCE the mock's own name into a vendor domain, never
    remove it, so no mechanical substitution can turn that assertion from failing into passing."""
    host = urlparse(synth.linear_url("ENG-1", "fix the thing", org="acme")).netloc
    assert host == "linear.app"
    assert "backlot" not in host
