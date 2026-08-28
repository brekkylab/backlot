import hashlib
import re
from urllib.parse import urlparse

import pytest

from backlot import synth

DOC = "dsid_00908a2dda4b4d359194a091019e8367"
DOC2 = "dsid_f9591843028149bdb47f7c3a70b3baa1"


def test_hnum_and_timestamps_are_deterministic():
    assert synth.hnum(DOC) == synth.hnum(DOC)
    assert synth.epoch(DOC) == synth.epoch(DOC)
    ts = synth.epoch(DOC)
    assert synth.BASE_EPOCH <= ts < synth.BASE_EPOCH + synth.TIME_RANGE


def test_distinct_docs_get_distinct_values():
    assert synth.github_number(DOC) != synth.github_number(DOC2)
    assert synth.confluence_id(DOC) != synth.confluence_id(DOC2)


def test_slack_ts_format():
    """`slack_fmt_ts` is the whole of Slack's ts shape now: the seconds come from the row's own
    `created_ts` and the fraction from a seed the importer picks, so there is no
    single-argument `slack_ts` left to test."""
    ts = synth.slack_fmt_ts(synth.epoch(DOC), DOC)
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


@pytest.mark.parametrize(
    "container",
    [
        "payments",
        "customer-support",
        "3d-printing",
        "1234-5678",
        "platform-infra-reliability-and-cost-ops",
        "a-b-c-d-e-f-g-h-i-j-k-l",
        "a",
        "!!!",
    ],
    ids=[
        "short",
        "two-words",
        "leading-digit",
        "all-digit-initials",
        "six-words",
        "twelve-words",
        "single-character",
        "no-word-characters",
    ],
)
def test_jira_project_key_is_a_shape_real_jira_can_issue(container):
    """`CreateProjectDetails.key` states the whole rule: a project key starts with an uppercase
    letter, continues in uppercase alphanumerics, and is at most 10 characters. `jira.schema.json`
    enforces exactly that on a corpus-PROVIDED key, so a DERIVED one has to satisfy it too or
    Backlot refuses as input the key it just served -- which it did, for any project name past four
    words and for one whose first word starts with a digit."""
    key = synth.jira_project_key(container)
    assert re.fullmatch(r"[A-Z][A-Z0-9]{1,9}", key), key
    assert len(key) <= synth.JIRA_PROJECT_KEY_MAX


def test_jira_project_key_leaves_an_already_valid_key_alone():
    """The cap moves only keys that were already invalid, and that is a property rather than a
    coincidence: a key satisfying the real rule is at most 10 characters and letter-led, so its
    readable half is at most 4 and starts with a letter, and both the trim and the digit-strip are
    no-ops. Pinned over the container names the bench actually carries, which is what makes the
    change safe to land without re-deriving anyone's stored keys."""
    for container in ("customer-support", "internal-support", "payments", "engineering"):
        key = synth.jira_project_key(container)
        assert key == synth._key(container, "PROJ") + synth._digest(container)[:6].upper()


def test_jira_key_number_matches_jira_keys_suffix():
    """`jira_key_number` is `jira_key`'s numeric suffix, split out so a served id can be assigned
    and probed on the suffix ALONE (see store.ID_SEED -- a key's prefix is under-constrained,
    since a corpus-provided key can claim any prefix for its project). `jira_key` calls
    `jira_key_number` rather than recomputing it, so the two cannot drift apart -- if they did, a
    served id assigned from the seed would stop matching the number the key itself carries."""
    for doc_id in (DOC, DOC2):
        for project_key in ("PAY", "ENG"):
            assert (
                synth.jira_key(doc_id, project_key)
                == f"{project_key}-{synth.jira_key_number(doc_id)}"
            )


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


_B64URL_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_gdrive_file_id_matches_the_real_id_shape():
    """Measured against real Drive: a modern file id is 33 characters, base64url alphabet
    (``[A-Za-z0-9_-]``), and always starts with ``1``. `synth.gdrive_file_id` has to match that
    shape, not `drive_folder_id`'s hex slice -- hex spans only 16 of the 64 available symbols and
    reads noticeably unlike a real one."""
    fid = synth.gdrive_file_id(DOC)
    assert len(fid) == 33
    assert fid[0] == "1"
    assert all(c in _B64URL_ALPHABET for c in fid)


def test_gdrive_file_id_is_stable_and_distinct():
    assert synth.gdrive_file_id(DOC) == synth.gdrive_file_id(DOC)
    assert synth.gdrive_file_id(DOC) != synth.gdrive_file_id(DOC2)


def test_gdrive_file_id_never_collides_with_a_folder_id():
    """A file id and a folder id must be disjoint spaces: `routers.google._drive_folder_name_by_id`
    tries every container name's `drive_folder_id` against an incoming id, and a file id that also
    matched would resolve to the wrong kind of object. Every folder id starts ``0A``, every file id
    starts ``1``, so the two can never collide by construction, not merely by low probability.

    Both are base64url over the alphabet a real Drive id draws on, at the length real uses for each
    kind — a folder id was a hex slice, which reads noticeably unlike either."""
    import re

    assert synth.gdrive_file_id(DOC)[:1] == "1"
    folder = synth.drive_folder_id("some-folder")
    assert folder[:2] == "0A" and len(folder) == 19
    assert re.fullmatch(r"[A-Za-z0-9_-]+", folder)
    assert synth.gdrive_file_id(DOC) != synth.drive_folder_id(DOC)


def test_served_uuids_are_rfc_4122_version_4():
    """Real Notion and Linear ids are v4 UUIDs, and a strict validator says so — zod's `.uuid()`
    and class-validator's `IsUUID(4)` reject anything else. A raw digest slice satisfied them about
    1 time in 70, so ~93% of every served page, block, user, comment, issue and team id was
    rejected by a client that checks. Over a spread of seeds, not one: the version nibble and the
    variant bits are what the digest would otherwise vary."""
    import re

    v4 = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
    ids = [synth.notion_id(f"seed-{i}") for i in range(200)]
    ids += [synth.linear_id(f"seed-{i}") for i in range(200)]
    ids += [synth.notion_block_id(f"seed-{i}", i) for i in range(200)]
    assert all(v4.fullmatch(i) for i in ids), next(i for i in ids if not v4.fullmatch(i))
    # ...and still a pure function of the seed, so a stored id keeps resolving
    assert synth.notion_id(DOC) == synth.notion_id(DOC) != synth.notion_id(DOC2)


def test_gmail_message_ids_are_never_zero_padded():
    """Real Gmail renders the id as an integer, so an id whose top nibble is zero is 15 digits
    there — and the real API resolves that spelling while refusing the padded one. `:016x` padded
    roughly one id in 16, and Backlot served the spelling real 404s."""
    ids = [synth.gmail_message_id(f"seed-{i}") for i in range(3000)]
    assert not [i for i in ids if i.startswith("0")]
    assert all(int(i, 16) < synth.GMAIL_ID_MAX for i in ids)
    # the padded spelling of a served id is still the same message (store.gmail_id_spelling)
    from backlot import store

    assert store.gmail_id_spelling(ids[0].rjust(16, "0")) == ids[0]
    assert store.gmail_id_spelling(ids[0].upper()) == ids[0]


def test_atlassian_comment_ids_do_not_leak_the_child_spelling():
    """A stored comment id composes its PARENT's key with the comment's position (`PAY-7::c1`) —
    Backlot's own bookkeeping. Real Jira and Confluence report a numeric string, so a client that
    parses or pattern-matches the id rejected what this served, and the internal scheme leaked to
    the wire. notion and linear already wrapped theirs."""
    cid = synth.atlassian_comment_id("PAY-7::c1")
    assert cid.isdigit() and "::" not in cid
    assert cid == synth.atlassian_comment_id("PAY-7::c1")  # stable, or a stored url stops resolving
    assert cid != synth.atlassian_comment_id("PAY-7::c2")


def test_linear_url_is_the_real_vendor_domain():
    """A rename's blind substitution can turn this into `linear.backlot`. Asserted
    on the parsed host (no trailing slash) rather than a URL literal, because the vulnerable
    pattern is the literal characters `app` immediately followed by a slash — spelling that
    combination anywhere, even in a comment, makes a repeat of the bug rewrite it right alongside
    the code it guards. A bare `"linear.app"` with nothing appended has no slash for the pattern
    to land on, so it survives. The `"backlot" not in host` half is the one that actually
    matters: a rename can only ever INTRODUCE Backlot's own name into a vendor domain, never
    remove it, so no mechanical substitution can turn that assertion from failing into passing."""
    host = urlparse(synth.linear_url("ENG-1", "fix the thing", org="acme")).netloc
    assert host == "linear.app"
    assert "backlot" not in host


def test_linear_priority_normalisation():
    """Both importers normalize through this, so however a corpus spells a priority — a label, a
    `P0`-style key, or Linear's own number — it reaches the same stored value."""
    assert [synth.linear_priority(v) for v in ("P0", "P1", "P2", "P3")] == [1, 2, 3, 4]
    assert [synth.linear_priority(v) for v in ("Urgent", "High", "Medium", "Low")] == [1, 2, 3, 4]
    assert synth.linear_priority(3) == 3  # already Linear's scale
    assert synth.linear_priority("unrecognised") == 0  # Linear's "No priority"
    assert synth.linear_priority(None) is None


def test_parse_transcript_text_is_the_inverse_of_the_writer():
    """`content` is DEFINED as the sentence concatenation, so this pair has to be a fixed point —
    the same relationship notion_blocks / notion_blocks_to_text have."""
    text = "Hana: numbers first.\nMia: design shipped.\nAnd cleared the backlog."
    sentences = synth.parse_transcript_text(text)
    assert [s["speaker_name"] for s in sentences] == ["Hana", "Mia"]
    assert sentences[1]["text"] == "design shipped.\nAnd cleared the backlog."
    assert synth.fireflies_transcript_text(sentences) == text


def test_parse_transcript_text_reads_a_leading_clock_as_the_start_time():
    sentences = synth.parse_transcript_text("[00:00] A: one\n(01:30) B: two\n02:00 - C: three")
    assert [s["start_time"] for s in sentences] == [0.0, 90.0, 120.0]
    assert [s["speaker_name"] for s in sentences] == ["A", "B", "C"]


def test_an_unattributed_opening_line_becomes_a_sentence_rather_than_vanishing():
    """Dropping it would break the fixed point above: the stored content is re-derived from the
    sentences, so a line no sentence holds is a line the transcript loses."""
    sentences = synth.parse_transcript_text("(recording starts)\nA: hello")
    assert [(s["speaker_name"], s["text"]) for s in sentences] == [
        (None, "(recording starts)"),
        ("A", "hello"),
    ]
    assert synth.fireflies_transcript_text(sentences) == "(recording starts)\nA: hello"


def test_a_colon_inside_a_sentence_does_not_mint_a_speaker():
    sentences = synth.parse_transcript_text("A: the dashboard\nsee https://example.com for detail")
    assert len(sentences) == 1
    assert sentences[0]["text"] == "the dashboard\nsee https://example.com for detail"


def test_an_auto_notes_label_line_is_read_as_a_speaker_here():
    """A BYO transcript body is read by the conventions the record format documents and no others,
    so a line shaped like `Label: value` IS a speaker line — `Date:` and `Duration:` included.

    That is the intended reading, not an oversight. `backlot.importer.erb` suppresses those labels
    because its dataset demonstrably leads with an auto-notes header AND declares the attendees to
    gate them against; a BYO record has no attendee list, leaving "Host: welcome everyone" and
    "Date: 2025-02-20" indistinguishable. Given the ambiguity this parser keeps the property that
    matters — the fixed point below, which a deny list breaks by dropping the line — and a corpus
    that wants a header excluded supplies `sentences` explicitly instead.
    """
    text = "Date: 2025-02-20\nDuration: ~52 min\nMaya: numbers first."
    sentences = synth.parse_transcript_text(text)
    assert [s["speaker_name"] for s in sentences] == ["Date", "Duration", "Maya"]
    # The consequence to know about: these names reach the served speaker analytics.
    assert [s["name"] for s in synth.fireflies_speaker_stats(sentences)] == [
        "Date",
        "Duration",
        "Maya",
    ]


def test_a_label_shaped_opening_line_keeps_the_fixed_point():
    """The regression a ported-back deny list would cause. Suppressing a label-shaped speaker also
    strands its line — nothing holds it, and `content` is re-derived from the sentences — so the
    transcript silently loses its first line from both the served body and full-text search.
    Every plausible speaker label that is also a header word has to survive this.
    """
    for opening in ("Host: welcome everyone", "Notes: kickoff", "Date: 2025-02-20"):
        text = f"{opening}\nMaya: numbers first."
        assert synth.fireflies_transcript_text(synth.parse_transcript_text(text)) == text, opening


def test_parse_transcript_text_of_an_empty_body_is_empty():
    assert synth.parse_transcript_text("") == []
    assert synth.parse_transcript_text("   \n  ") == []
