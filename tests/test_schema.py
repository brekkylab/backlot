"""BYO corpus JSON Schema validation (backlot/schemas/ + backlot.validation)."""

import json
from datetime import datetime

import pytest

from backlot import store, validation
from backlot.config import Settings
from backlot.validation import record_errors, validate_file
from backlot.importer.byo import load


def test_schema_per_service_matches_store():
    # one schema file per served source type, keyed identically to the store registry
    assert set(validation.SERVICE_SCHEMAS) == set(store.SOURCE_TABLE)


def test_sample_corpus_is_valid(sample_corpus_path):
    # the conftest SAMPLE corpus (written to a tempfile) passes validation end-to-end
    assert validate_file(sample_corpus_path) == []


def _first_error(rec):
    return record_errors(rec)


def test_unknown_source_type_rejected():
    errs = _first_error({"source_type": "drive", "content": "x", "title": "t"})
    assert errs and "source_type must be one of" in errs[0]


def test_bad_visibility_enum_rejected():
    errs = _first_error(
        {"source_type": "confluence", "title": "t", "content": "c", "visibility": "secret"}
    )
    assert any("visibility" in e for e in errs)


def test_bad_subtype_enum_rejected():
    errs = _first_error({"source_type": "github", "title": "t", "content": "c", "subtype": "task"})
    assert any("subtype" in e for e in errs)


def test_unknown_top_level_key_rejected():
    # a typo'd field is the most common corpus mistake
    errs = _first_error(
        {"source_type": "jira", "title": "t", "content": "c", "athor_email": "a@b.com"}
    )
    assert any("athor_email" in e for e in errs)


def test_title_required_except_slack():
    assert _first_error({"source_type": "gmail", "content": "c"})  # missing title -> error
    assert _first_error({"source_type": "slack", "content": "c"}) == []  # slack ok without title


def test_hubspot_record_accepted():
    # a CRM record: the object type is the grouping unit, typed properties are free-form, and
    # associations name the target record by doc_id
    assert (
        _first_error(
            {
                "source_type": "hubspot",
                "object_type": "contacts",
                "doc_id": "hs-c1",
                "title": "Ava Stone",
                "content": "Ava Stone — VP Platform at Acme Health",
                "author_email": "owner@acme.com",
                "properties": {
                    "firstname": "Ava",
                    "lastname": "Stone",
                    "email": "ava@acme-health.com",
                },
                "associations": [{"to": "hs-co1", "to_type": "companies", "label": "Primary"}],
            }
        )
        == []
    )


def test_hubspot_association_requires_a_target():
    errs = _first_error(
        {
            "source_type": "hubspot",
            "object_type": "contacts",
            "title": "t",
            "content": "c",
            "associations": [{"to_type": "companies"}],
        }
    )
    assert any("associations/0" in e for e in errs)


def test_comments_only_where_supported():
    # slack/gmail/drive have no comment API, and HubSpot models notes/emails/meetings as their own
    # object types rather than as comments on a record -> the comments key is unexpected
    for src in ("slack", "gmail", "google_drive", "hubspot"):
        rec = {"source_type": src, "content": "c", "comments": [{"content": "x"}]}
        if src != "slack":
            rec["title"] = "t"
        assert any("comments" in e for e in _first_error(rec)), src
    # jira/confluence/github accept them
    assert (
        _first_error(
            {"source_type": "jira", "title": "t", "content": "c", "comments": [{"content": "x"}]}
        )
        == []
    )


def test_comment_needs_content_or_body():
    errs = _first_error(
        {
            "source_type": "jira",
            "title": "t",
            "content": "c",
            "comments": [{"author_email": "a@b.com"}],
        }
    )
    assert any("comments/0" in e for e in errs)


def test_replies_only_on_slack():
    assert any(
        "replies" in e
        for e in _first_error(
            {
                "source_type": "confluence",
                "title": "t",
                "content": "c",
                "replies": [{"content": "x"}],
            }
        )
    )
    assert (
        _first_error({"source_type": "slack", "content": "c", "replies": [{"content": "x"}]}) == []
    )


def test_load_corpus_rejects_invalid_record(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"source_type": "confluence", "content": "c"}))  # no title
    with pytest.raises(SystemExit):
        load(bad, Settings(data_dir=tmp_path))


def test_schema_files_are_valid_json_schemas():
    # every committed schema is itself a well-formed Draft 2020-12 schema
    from jsonschema import Draft202012Validator

    for src, schema in validation.SERVICE_SCHEMAS.items():
        Draft202012Validator.check_schema(schema)
        assert schema["properties"]["source_type"]["const"] == src


def test_s3_schema_registered():
    from backlot.validation import record_errors

    assert (
        record_errors(
            {"source_type": "s3", "bucket": "b", "key": "k", "title": "t", "content": "c"}
        )
        == []
    )
    errs = record_errors({"source_type": "s3", "bucket": "b", "title": "t", "content": "c"})
    assert errs and any("key" in e for e in errs)


# --- Linear -----------------------------------------------------------------------


def test_linear_record_accepts_linears_own_field_names():
    """A corpus written against the Linear API should need no renaming: `state` (not status),
    camelCase `branchName`/`dueDate`, `assignee` as an email."""
    assert (
        record_errors(
            {
                "source_type": "linear",
                "title": "t",
                "content": "c",
                "team": "engineering",
                "identifier": "ENG-1",
                "state": "In Progress",
                "priority": 2,
                "estimate": 3,
                "labels": ["bug"],
                "project": "p",
                "cycle": "2025-W08",
                "branchName": "ava/eng-1-t",
                "dueDate": "2026-03-15",
                "assignee": "ava@acme.com",
                "assigneeName": "Ava",
                "completedAt": "2026-03-01T00:00:00Z",
            }
        )
        == []
    )


def test_linear_priority_accepts_a_label_or_the_numeric_scale():
    for value in (0, 4, "P0", "Urgent"):
        assert (
            record_errors(
                {"source_type": "linear", "title": "t", "content": "c", "priority": value}
            )
            == []
        )


def test_linear_rejects_jiras_vocabulary():
    """`status` is Jira's word for it; accepting it silently would drop the value on load."""
    errs = record_errors(
        {"source_type": "linear", "title": "t", "content": "c", "status": "In Progress"}
    )
    assert any("status" in e for e in errs)


def test_linear_rejects_an_unknown_key():
    errs = record_errors({"source_type": "linear", "title": "t", "content": "c", "nope": 1})
    assert any("nope" in e for e in errs)


def test_linear_byo_round_trip_serves_what_it_loaded(tmp_path):
    """The whole BYO contract for a new source: a `source_type: "linear"` corpus imports,
    validates, and comes back out of the store on the API's own columns."""
    corpus = tmp_path / "linear.jsonl"
    corpus.write_text(
        json.dumps(
            {
                "source_type": "linear",
                "doc_id": "byo-1",
                "team": "platform",
                "group": "platform",
                "title": "Ship the cache",
                "content": "Two-tier cache for the gateway.",
                "author_email": "ava@acme.com",
                "identifier": "PLA-7",
                "state": "Done",
                "priority": "Urgent",
                "estimate": 8,
                "labels": ["cache"],
                "project": "gateway",
                "cycle": "Cycle 41",
                "dueDate": "2026-04-01",
                "assignee": "bob@acme.com",
                "assigneeName": "Bob Stone",
                "completedAt": "2026-03-20T00:00:00Z",
                "comments": [{"content": "Rolled out.", "author_email": "bob@acme.com"}],
            }
        )
        + "\n"
    )
    assert validate_file(corpus) == []
    settings = Settings(data_dir=tmp_path)
    assert load(corpus, settings)["counts"] == {"linear": 1}

    conn = store.connect_ro(settings.db_path)
    try:
        row = store.get_document(conn, "linear", "byo-1")
        assert row["team"] == "platform"
        assert row["identifier"] == "PLA-7"
        assert row["state"] == "Done"
        assert row["priority"] == 1  # "Urgent" -> Linear's scale
        assert row["estimate"] == 8
        assert row["due_date"] == "2026-04-01"
        assert row["assignee_email"] == "bob@acme.com"
        assert row["assignee_display"] == "Bob Stone"
        # `completedAt` is stored as epoch seconds, not left to a derivation.
        assert row["completed_ts"] == int(
            datetime.fromisoformat("2026-03-20T00:00:00+00:00").timestamp()
        )
        assert json.loads(row["labels"]) == ["cache"]
        assert store.get_container(conn, "linear", "platform")["group_id"] == "platform"
        assert [c["body"] for c in store.doc_comments(conn, "linear", "byo-1")] == ["Rolled out."]
    finally:
        conn.close()


def test_linear_synthesized_identifier_is_resolvable(tmp_path):
    """An omitted `identifier` is synthesized — and the synthesized value must be MATERIALIZED,
    not produced per request. The app's reverse index is built from stored columns, so a
    serve-time-only identifier came back "Entity not found" from `issue(id:)` even though the API
    had just served that exact string to the caller."""
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(
        json.dumps(
            {
                "source_type": "linear",
                "doc_id": "no-ident",
                "team": "engineering",
                "title": "No identifier given",
                "content": "body",
                "author_email": "ava@acme.com",
            }
        )
        + "\n"
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    try:
        row = store.get_document(conn, "linear", "no-ident")
        assert row["identifier"], "identifier must be stored, not left to serve time"
        assert row["identifier"].startswith("ENG-")
        # ...and it resolves through the same lookup `issue(id:)` uses.
        assert store.linear_issue_by_identifier(conn, row["identifier"])["doc_id"] == "no-ident"
    finally:
        conn.close()


# --- fireflies -------------------------------------------------------------------


def test_fireflies_schema_accepts_sentences_or_content(tmp_path):
    """Either view of the transcript is a complete record: `sentences` (the structured form) or
    `content` (a plain body the loader parses)."""
    assert (
        record_errors(
            {
                "source_type": "fireflies",
                "title": "T",
                "sentences": [{"speaker_name": "A", "text": "hi"}],
            }
        )
        == []
    )
    assert record_errors({"source_type": "fireflies", "title": "T", "content": "A: hi"}) == []


def test_fireflies_record_with_neither_sentences_nor_content_is_rejected(tmp_path):
    """A schema `anyOf` would report "not valid under any of the given schemas", naming neither
    field, so the loader states it instead."""
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(json.dumps({"source_type": "fireflies", "title": "Empty"}) + "\n")
    with pytest.raises(SystemExit) as e:
        load(corpus, Settings(data_dir=tmp_path))
    assert "'sentences' or 'content'" in str(e.value)


def test_fireflies_schema_rejects_the_slack_replies_array():
    """`replies` is Slack's child-row array. A transcript's child rows are `sentences`, so writing
    `replies` on a transcript is a mistake worth catching rather than silently ignoring."""
    errs = record_errors(
        {
            "source_type": "fireflies",
            "title": "T",
            "content": "A: hi",
            "replies": [{"content": "x"}],
        }
    )
    assert errs and any("replies" in e for e in errs)


def test_fireflies_schema_sentence_shape_is_checked():
    ok = record_errors(
        {
            "source_type": "fireflies",
            "title": "T",
            "sentences": [
                {
                    "speaker_name": "A",
                    "speaker_id": 0,
                    "author_email": "a@x.com",
                    "text": "hi",
                    "start_time": 0,
                    "end_time": 1.5,
                }
            ],
        }
    )
    assert ok == []
    # a null speaker is legal (the API returns one when diarization produced no label)
    assert (
        record_errors(
            {
                "source_type": "fireflies",
                "title": "T",
                "sentences": [{"speaker_name": None, "text": "(crosstalk)"}],
            }
        )
        == []
    )
    bad = record_errors(
        {
            "source_type": "fireflies",
            "title": "T",
            "sentences": [{"text": "hi", "speaker_nam": "typo"}],
        }
    )
    assert bad and any("speaker_nam" in e for e in bad)


def test_fireflies_schema_uses_the_apis_own_field_names():
    """A corpus written against the real Fireflies API should need no renaming."""
    assert (
        record_errors(
            {
                "source_type": "fireflies",
                "title": "T",
                "content": "A: hi",
                "host_email": "h@x.com",
                "organizer_email": "o@x.com",
                "duration": 42,
                "participants": ["a@x.com"],
                "transcript_id": "abc",
                "calendar_id": "c",
                "calendar_type": "google_calendar",
                "meeting_link": "https://meet.example/x",
                "audio_url": "https://a",
                "video_url": "https://v",
                "transcript_url": "https://t",
                "meeting_attendees": [{"displayName": "A", "email": "a@x.com", "location": None}],
                "summary": {
                    "overview": "o",
                    "topics_discussed": ["t"],
                    "action_items": ["a"],
                    "keywords": ["k"],
                    "meeting_type": "discovery",
                },
                "analytics": {"sentiments": {"positive_pct": 50}},
            }
        )
        == []
    )


def test_fireflies_schema_duration_is_minutes_not_seconds():
    """The API's unit is MINUTES; the description has to say so, because a corpus author guessing
    seconds would silently serve 60x-long meetings."""
    desc = validation.SERVICE_SCHEMAS["fireflies"]["properties"]["duration"]["description"]
    assert "MINUTES" in desc


# --- fields that make an ERB import expressible (#17) ------------------------------


def test_confluence_accepts_confidentiality_ownership_and_reviewers():
    assert (
        record_errors(
            {
                "source_type": "confluence",
                "space": "ENG",
                "title": "Runbook",
                "content": "c",
                "author_email": "ava@a.com",
                "author_name": "Tom\u00e1s Rr\u00e9",
                # free text, not an enum: the bench writes "restricted (finance/customer-sensitive)" too
                "confidentiality": "restricted (customer-sensitive)",
                "owner_team": "engineering",
                "reviewers": ["bob@a.com"],
            }
        )
        == []
    )


def test_drive_collaborators_and_jira_severity_squad_accepted():
    assert (
        record_errors(
            {
                "source_type": "google_drive",
                "folder": "research",
                "title": "t",
                "content": "c",
                "collaborators": ["bob@a.com"],
                "author_name": "Ava Chen",
            }
        )
        == []
    )
    assert (
        record_errors(
            {
                "source_type": "jira",
                "project": "PAY",
                "title": "t",
                "content": "c",
                "severity": "Sev1",
                "squad": "payments-core",
            }
        )
        == []
    )


def test_slack_participants_accepted():
    assert (
        record_errors(
            {
                "source_type": "slack",
                "channel": "incidents",
                "content": "c",
                "participants": ["ava", "infra-bot"],
            }
        )
        == []
    )


def test_gmail_messages_array_is_the_thread_not_slack_replies():
    """A Gmail thread's later messages get their own array: `replies` stays Slack-only, since a
    reply (with reactions and files) is not what a further email in a thread is."""
    assert (
        record_errors(
            {
                "source_type": "gmail",
                "mailbox": "ava",
                "title": "Retry storm",
                "content": "c",
                "mailbox_owner": "Ava Chen",
                "messages": [
                    {
                        "content": "On it.",
                        "author_email": "bob@a.com",
                        "to": "ava@a.com",
                        "message_id": "<b@a>",
                        "created": "2026-01-04T10:00:00Z",
                    },
                    # a header-only message: empty is allowed here, unlike a slack reply
                    {"content": ""},
                ],
            }
        )
        == []
    )
    # the key is still required
    assert any(
        "messages/0" in e
        for e in record_errors(
            {"source_type": "gmail", "title": "t", "content": "c", "messages": [{"to": "x@a.com"}]}
        )
    )
    # and `messages` is gmail's alone
    assert any(
        "messages" in e
        for e in record_errors(
            {"source_type": "slack", "content": "c", "messages": [{"content": "x"}]}
        )
    )


def test_gmail_thread_content_may_be_empty_but_no_other_source():
    """A thread opened by a header-only message still carries its content in `messages`; every
    other source's `content` is the document itself, so it stays non-empty."""
    assert (
        record_errors(
            {"source_type": "gmail", "title": "t", "content": "", "messages": [{"content": "body"}]}
        )
        == []
    )
    assert any(
        "content" in e
        for e in record_errors({"source_type": "confluence", "title": "t", "content": ""})
    )


def test_group_may_be_null_to_mean_no_group_owns_the_container():
    for src, extra in (
        ("gmail", {"mailbox": "ava", "title": "t"}),
        ("google_drive", {"folder": "scratch", "title": "t"}),
        ("slack", {"channel": "incidents"}),
    ):
        assert record_errors({"source_type": src, "content": "c", "group": None, **extra}) == [], (
            src
        )


def test_readers_accept_typed_principal_ids():
    assert (
        record_errors(
            {
                "source_type": "jira",
                "project": "PAY",
                "title": "t",
                "content": "c",
                "readers": ["user:ava@a.com", "group:eng", "org:acme"],
            }
        )
        == []
    )


# --- the shipped example corpus ---------------------------------------------------


def _example_corpus():
    from tests.conftest import REPO_ROOT

    return REPO_ROOT / "examples" / "bring-your-own-corpus" / "sample_corpus.jsonl"


def test_example_corpus_is_valid():
    """`examples/bring-your-own-corpus/sample_corpus.jsonl` is the file the walkthrough loads and
    the one a reader copies from, and nothing validated it — `sample_corpus_path` above is the
    in-code conftest SAMPLE, a different corpus. Read the shipped file itself."""
    assert validate_file(_example_corpus()) == []


def test_example_corpus_covers_every_served_source():
    """It is documented as "a fully-populated record of every source type", so a source missing
    from it is a broken promise — and one that goes unnoticed: `linear` was absent for two
    releases after its loader landed, because only a human running `run.py` ever read this file."""
    import json

    records = [
        json.loads(line) for line in _example_corpus().read_text().split("\n") if line.strip()
    ]
    assert {r["source_type"] for r in records} == set(store.SOURCE_TABLE)
