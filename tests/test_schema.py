"""BYO corpus JSON Schema validation (backlot/schemas/ + backlot.validation)."""

import json
from datetime import datetime

import pytest

from backlot import store, validation
from tests._helpers import complete, served_id
from backlot.config import Settings
from backlot.validation import record_errors, validate_file
from backlot.importer.byo import load


def _unstated(rec: dict) -> list[str]:
    """Facts this record leaves for the loader to invent, according to its own schema.

    Derived from the schemas rather than from a list kept here: a second list of required fields is
    a second contract, and the point of the schemas is that there is one.
    """
    return [e for e in record_errors(rec) if "is a required property" in e]


def test_schema_per_service_matches_store():
    # one schema file per served source type, keyed identically to the store registry
    assert set(validation.SERVICE_SCHEMAS) == set(store.SOURCE_TABLE)


def test_sample_corpus_is_valid(sample_corpus_path):
    # the conftest SAMPLE corpus (written to a tempfile) passes validation end-to-end
    assert validate_file(sample_corpus_path) == []


def _first_error(rec):
    """Validate a record that states only what its test is about; the rest is completed for it.

    The tests ABOUT the contract call ``record_errors`` on a hand-built dict instead -- completing
    the record is exactly what they must not do.
    """
    return record_errors(complete(**rec))


def test_a_linear_comment_needs_a_body_like_every_other_sources():
    errors = _first_error(
        {
            "source_type": "linear",
            "team": "engineering",
            "title": "Guardbands",
            "content": "Body.",
            "comments": [{}],
        }
    )
    assert errors and "comments/0" in errors[0]


def test_a_fireflies_sentence_needs_its_text():
    """`content` IS the sentence concatenation, so a sentence with no text becomes a stored row the
    transcript's own text does not contain — which breaks the inverse the two are defined by."""
    errors = _first_error(
        {
            "source_type": "fireflies",
            "channel": "all-hands",
            "title": "April all-hands",
            "sentences": [{"speaker_name": "A", "text": "hello"}, {}],
        }
    )
    assert errors and "sentences/1" in errors[0]


@pytest.mark.parametrize("state,ok", [("open", True), ("closed", True), ("merged", False)])
def test_github_state_is_only_what_the_api_returns(state, ok):
    """A merge is `merged_at`, not a third state."""
    errors = _first_error(
        {
            "source_type": "github",
            "repo": "gateway",
            "title": "Fix the refill tick",
            "content": "Off by one.",
            "subtype": "pull_request",
            "state": state,
        }
    )
    assert (errors == []) is ok


@pytest.mark.parametrize(
    "repo,ok",
    [
        ("gateway", True),
        ("redwood-sdk-go", True),
        ("core_payments", True),
        ("api.v2", True),
        ("douro/core-payments", False),
        ("has space", False),
        # "must not exceed 100 characters" is the other half of the sentence GitHub states
        ("r" * 100, True),
        ("r" * 101, False),
    ],
)
def test_a_github_repo_is_a_name_not_an_owner_qualified_path(repo, ok):
    """`repo` is the API's bare `name`. The owner is a path segment of its own, which the router
    supplies from the org it serves, and `full_name` is composed from the two. A value carrying the
    owner is served as `name` verbatim and lands in `full_name` twice -- `douro/douro/core-payments`
    -- taking `html_url` and every URL template with it, and `/repos/{owner}/{repo}` never finds the
    row again. GitHub's own name charset is the check, so a space is refused for the same reason.
    """
    errors = _first_error({"source_type": "github", "repo": repo})
    assert (errors == []) is ok


@pytest.mark.parametrize(
    "channel,ok",
    [
        ("incidents", True),
        ("eng-platform", True),
        ("release_war_room", True),
        ("q4-2026", True),
        ("#incidents", False),
        ("Incidents", False),
        ("eng platform", False),
        ("slack/sports", False),
        ("eng.platform", False),
        # Measured, against what the docs say: a live workspace's conversations.list answers with
        # these three, so a name real Slack holds cannot be refused here either.
        ("일반", True),
        ("랜덤", True),
        ("식사", True),
        ("東京-eng", True),
        ("café", True),
        # "must be 80 characters or less" is the other half of the sentence Slack states
        ("c" * 80, True),
        ("c" * 81, False),
    ],
)
def test_a_slack_channel_is_named_the_way_slack_names_one(channel, ok):
    """`channel` is served verbatim as a conversation's `name` and `name_normalized`, so a name
    Slack would refuse is an object `conversations.list` could never answer with -- and a connector
    that round-trips the name back into a call fails against Slack while passing here. The leading
    `#` is the one an LLM writing a corpus reaches for, because it is the spelling humans use, but
    it is display sugar the client adds rather than part of the name: stored, it also doubles into
    a `##incidents` topic. Slack's own charset is the check, so uppercase and a space go with it.

    The charset is wider than the documented sentence, though: `conversations.create` names only
    lowercase letters, digits, hyphens and underscores, while real Slack also holds non-Latin
    names. Enforcing the sentence as written would refuse a corpus the live API could serve, which
    is the same bug pointing the other way.
    """
    errors = _first_error({"source_type": "slack", "channel": channel})
    assert (errors == []) is ok


@pytest.mark.parametrize(
    "source_type,container",
    sorted((src, store.grouping_col(src)) for src in store.SOURCE_TABLE),
)
def test_a_record_must_state_its_container_author_and_time(source_type, container):
    """The three facts a served document cannot exist without, and which the loader used to invent:
    who wrote it, where it lives, and when it happened."""
    minimal = {"source_type": source_type, "content": "Body."}
    if source_type != "slack":  # a slack message has no title, and unknown keys are rejected
        minimal["title"] = "T"
    if source_type == "fireflies":
        minimal["duration"] = 30
    joined = " ".join(record_errors(minimal))
    assert container in joined
    assert "created" in joined
    assert "author_email" in joined or "host_email" in joined
    # And the requirement does not stop at the key: `str(rec[container])` is taken verbatim now, so
    # a corpus that states the container as "" gets a container whose name is the empty string,
    # where the old `rec.get(col) or src` coerced it to the source name.
    blank = record_errors(complete(source_type, **{container: "", "doc_id": "blank"}))
    assert blank and container in blank[0], blank


def test_a_fireflies_record_may_name_its_owner_as_host_email():
    """`host_email` is the API's own name for it, so a corpus written against Fireflies needs no
    renaming."""
    assert (
        record_errors(
            {
                "source_type": "fireflies",
                "channel": "all-hands",
                "title": "April all-hands",
                "host_email": "ceo@acme.com",
                "created": "2026-04-10T16:00:00Z",
                "duration": 25.0,
                "content": "Ada Chief: welcome everyone.",
            }
        )
        == []
    )


def test_an_empty_title_is_not_a_title():
    errors = record_errors(
        {
            "source_type": "confluence",
            "space": "handbook",
            "title": "",
            "content": "Body.",
            "author_email": "ava@acme.com",
            "created": "2026-02-01T09:00:00Z",
        }
    )
    assert errors and "title" in errors[0]


def test_a_hubspot_note_needs_no_name():
    """HubSpot serves no name on a note, and notes are most of a real portal."""
    assert (
        record_errors(
            {
                "source_type": "hubspot",
                "object_type": "notes",
                "content": "Security review scheduled.",
                "author_email": "rep@acme.com",
                "created": "2026-03-05T14:00:00Z",
                "properties": {"hs_note_body": "Security review scheduled."},
            }
        )
        == []
    )


def test_a_github_file_needs_no_state_but_a_pull_does():
    common = {
        "source_type": "github",
        "repo": "gateway",
        "title": "gateway/limiter.py",
        "content": "class TokenBucket: ...",
        "author_email": "bob@acme.com",
        "created": "2026-01-20T09:00:00Z",
    }
    assert record_errors({**common, "subtype": "file", "path": "gateway/limiter.py"}) == []
    errors = record_errors({**common, "subtype": "pull_request", "state": "open"})
    assert errors == []
    errors = record_errors({**common, "subtype": "pull_request"})
    assert errors and "state" in errors[0]


def test_a_child_row_states_its_author_and_its_time():
    errors = record_errors(
        {
            "source_type": "jira",
            "project": "payments",
            "title": "SEV2",
            "content": "Body.",
            "author_email": "bob@acme.com",
            "created": "2026-02-08T20:00:00Z",
            "status": "In Progress",
            "issuetype": "Incident",
            "reporter": "bob@acme.com",
            "comments": [{"content": "Rolled back."}],
        }
    )
    joined = " ".join(errors)
    assert "author_email" in joined and "created_ts" in joined


def test_a_fireflies_sentence_still_needs_no_author():
    """The vendor's `Sentence` carries no email at all, and an unnamed speaker is what diarization
    produces — so Backlot must not be stricter than the API it stands in for."""
    assert (
        record_errors(
            {
                "source_type": "fireflies",
                "channel": "all-hands",
                "title": "April all-hands",
                "host_email": "ceo@acme.com",
                "created": "2026-04-10T16:00:00Z",
                "duration": 25.0,
                "sentences": [{"text": "(crosstalk)", "start_time": 12}],
            }
        )
        == []
    )


def test_unknown_source_type_rejected():
    # As written: `complete` has no values for a source type that does not exist.
    errs = record_errors({"source_type": "drive", "content": "x", "title": "t"})
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


def test_an_either_or_child_row_names_both_fields():
    """`anyOf` is the accurate statement of "state either of these" and the useless message at the
    same time: jsonschema reports `is not valid under any of the given schemas`, which names neither
    alternative and quotes the child row instead of the missing key -- the string #47 existed to
    remove. The branches ARE the alternatives, so the message lists them."""
    assert record_errors(
        complete("linear", doc_id="ln-1", comments=[{"author_email": "a@b.com", "created_ts": 1}])
    ) == ["ln-1 [comments/0]: 'content' or 'body' is a required property"]
    assert record_errors(complete("fireflies", doc_id="ff-2", sentences=[{"start_time": 0}])) == [
        "ff-2 [sentences/0]: 'text' or 'content' is a required property"
    ]
    # An `anyOf` that is a union of TYPES names no fields, and keeps jsonschema's own message.
    assert record_errors(complete("linear", doc_id="ln-2", attachments=[7])) == [
        "ln-2 [attachments/0]: 7 is not valid under any of the given schemas"
    ]


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


@pytest.mark.parametrize(
    "bucket,ok",
    [
        ("eng-artifacts", True),
        ("douro-core-payments", True),
        ("logs.2026", True),
        ("douro/core-payments", False),
        ("Eng-Artifacts", False),
        ("eng_artifacts", False),
        ("-eng", False),
        ("b", False),
        # "Bucket names must not contain two adjacent periods", and "must not be formatted as an
        # IP address" -- both on the same AWS list as the three rules above, and both accepted by
        # a charset-only pattern.
        ("logs..2026", False),
        ("192.168.1.1", False),
        ("192.168.1.1.eng", True),
    ],
)
def test_an_s3_bucket_is_named_the_way_s3_names_one(bucket, ok):
    """A bucket is the first half of a served object's identity and the host label in the
    virtual-hosted URL S3 hands back, so a name S3 would refuse cannot be served either: a slash
    splits the address, an uppercase letter or an underscore cannot appear in a host label, a
    one-character name is below the three S3 requires, and S3 refuses both adjacent periods and a
    name shaped like an IP address. A dot itself S3 allows, so this does too.
    """
    errors = _first_error({"source_type": "s3", "bucket": bucket, "key": "docs/readme.md"})
    assert (errors == []) is ok


def test_s3_schema_registered():
    from backlot.validation import record_errors

    assert (
        _first_error(
            {"source_type": "s3", "bucket": "bkt", "key": "k", "title": "t", "content": "c"}
        )
        == []
    )
    # As written: the point is that the missing `key` is refused.
    errs = record_errors({"source_type": "s3", "bucket": "bkt", "title": "t", "content": "c"})
    assert errs and any("key" in e for e in errs)


# --- Linear -----------------------------------------------------------------------


def test_linear_record_accepts_linears_own_field_names():
    """A corpus written against the Linear API should need no renaming: `state` (not status),
    camelCase `branchName`/`dueDate`, `assignee` as an email."""
    assert (
        _first_error(
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
            _first_error({"source_type": "linear", "title": "t", "content": "c", "priority": value})
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
                "created": "2026-02-18T09:00:00Z",
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
                "comments": [
                    {
                        "content": "Rolled out.",
                        "author_email": "bob@acme.com",
                        "created_ts": "2026-02-18T10:00:00Z",
                    }
                ],
            }
        )
        + "\n"
    )
    assert validate_file(corpus) == []
    settings = Settings(data_dir=tmp_path)
    assert load(corpus, settings)["counts"] == {"linear": 1}

    conn = store.connect_ro(settings.db_path)
    try:
        row = store.get_document(conn, "linear", served_id("linear", "byo-1"))
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
        assert [
            c["body"] for c in store.doc_comments(conn, "linear", served_id("linear", "byo-1"))
        ] == ["Rolled out."]
    finally:
        conn.close()


def test_linear_synthesized_identifier_is_resolvable(tmp_path):
    """An omitted `identifier` is synthesized — and the synthesized value must be MATERIALIZED,
    not produced per request: every lookup reads a stored column, so a serve-time-only identifier
    came back "Entity not found" from `issue(id:)` even though the API had just served that exact
    string to the caller."""
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(
        json.dumps(
            complete(
                "linear",
                doc_id="no-ident",
                team="engineering",
                title="No identifier given",
                content="body",
                author_email="ava@acme.com",
            )
        )
        + "\n"
    )
    settings = Settings(data_dir=tmp_path)
    load(corpus, settings)
    conn = store.connect_ro(settings.db_path)
    try:
        row = store.get_document(conn, "linear", served_id("linear", "no-ident"))
        assert row["identifier"], "identifier must be stored, not left to serve time"
        assert row["identifier"].startswith("ENG-")
        # ...and it resolves through the same lookup `issue(id:)` uses.
        assert store.linear_issue_by_identifier(conn, row["identifier"])["id"] == served_id(
            "linear", "no-ident"
        )
    finally:
        conn.close()


# --- fireflies -------------------------------------------------------------------


def test_fireflies_schema_accepts_sentences_or_content(tmp_path):
    """Either view of the transcript is a complete record: `sentences` (the structured form) or
    `content` (a plain body the loader parses)."""
    assert (
        _first_error(
            {
                "source_type": "fireflies",
                "title": "T",
                "sentences": [{"speaker_name": "A", "text": "hi"}],
            }
        )
        == []
    )
    assert _first_error({"source_type": "fireflies", "title": "T", "content": "A: hi"}) == []


def test_fireflies_record_with_neither_sentences_nor_content_is_rejected(tmp_path):
    """One of the two IS the transcript, so a record carrying neither has nothing to serve.

    Stated as a condition rather than an `anyOf`, which would report "not valid under any of the
    given schemas" and name neither field.
    """
    assert record_errors(complete("fireflies", _omit={"content"}, title="Empty")) == [
        "Empty: 'content' is a required property when sentences is absent"
    ]

    corpus = tmp_path / "c.jsonl"
    corpus.write_text(json.dumps({"source_type": "fireflies", "title": "Empty"}) + "\n")
    with pytest.raises(SystemExit) as e:
        load(corpus, Settings(data_dir=tmp_path))
    assert "'content' is a required property" in str(e.value)


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
    ok = _first_error(
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
        _first_error(
            {
                "source_type": "fireflies",
                "title": "T",
                "sentences": [{"speaker_name": None, "text": "(crosstalk)"}],
            }
        )
        == []
    )
    bad = _first_error(
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
        _first_error(
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


# --- fields that make an ERB import expressible ------------------------------


def test_slack_participants_accepted():
    assert (
        _first_error(
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
        _first_error(
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
        for e in _first_error(
            {"source_type": "gmail", "title": "t", "content": "c", "messages": [{"to": "x@a.com"}]}
        )
    )
    # and `messages` is gmail's alone
    assert any(
        "messages" in e
        for e in _first_error(
            {"source_type": "slack", "content": "c", "messages": [{"content": "x"}]}
        )
    )


def test_gmail_thread_content_may_be_empty_but_no_other_source():
    """A thread opened by a header-only message still carries its content in `messages`; every
    other source's `content` is the document itself, so it stays non-empty."""
    assert (
        _first_error(
            {"source_type": "gmail", "title": "t", "content": "", "messages": [{"content": "body"}]}
        )
        == []
    )
    assert any(
        "content" in e
        for e in _first_error({"source_type": "confluence", "title": "t", "content": ""})
    )


def test_group_may_be_null_to_mean_no_group_owns_the_container():
    for src, extra in (
        ("gmail", {"mailbox": "ava", "title": "t"}),
        ("google_drive", {"folder": "scratch", "title": "t"}),
        ("slack", {"channel": "incidents"}),
    ):
        assert _first_error({"source_type": src, "content": "c", "group": None, **extra}) == [], src


def test_readers_accept_typed_principal_ids():
    assert (
        _first_error(
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


# --- the BYO example corpus: the FIELD REFERENCE ----------------------------------
#
# Three corpora live in this repo and each answers a different question (see
# backlot/schemas/README.md, "Which corpus is which"). This one's job is field coverage: every
# field the schemas declare, populated at least once, so a reader can see that none of a response
# has to be synthesized. `backlot/data/hello.jsonl` is the DEMO corpus (volume and containers, and
# it deliberately leaves optional fields out); `tests/conftest.py::SAMPLE` is the test fixture.
#
# Until the test below existed, the only property pinned here was "covers every source" — which
# hello.jsonl also satisfies, so nothing distinguished the two and the README's "fills in every
# field" was a claim rather than a fact (68 declared fields were unused when it was written).


def test_the_sample_corpus_states_every_required_fact():
    gaps = {
        i: _unstated(rec) for i, rec in enumerate(_example_records(), start=1) if _unstated(rec)
    }
    assert gaps == {}


def test_the_test_corpus_states_every_required_fact():
    from tests.conftest import SAMPLE

    assert [(i, _unstated(r)) for i, r in enumerate(SAMPLE) if _unstated(r)] == []


def test_the_typescript_examples_corpus_would_load():
    """The one example whose corpus is not Python, so the sweep above cannot see it. CI catches it
    by running the example, which is a whole round trip away; this catches it here."""
    import re

    from tests.conftest import REPO_ROOT

    path = REPO_ROOT / "examples/using-official-sdk/linear/index.ts"
    text = path.read_text()
    assert "const CORPUS = [" in text, f"{path.name}: no CORPUS literal -- fix this extractor"
    body = text[text.index("const CORPUS = [") + len("const CORPUS = ") :]
    depth = 0
    end = None
    for i, ch in enumerate(body):
        depth += ch == "["
        depth -= ch == "]"
        if depth == 0 and ch == "]":
            end = i + 1
            break
    assert end, f"{path.name}: unbalanced CORPUS literal"
    # object-literal keys to JSON keys, then drop the trailing commas JSON does not allow
    as_json = re.sub(r"(\n\s*|\{\s*|,\s*)([A-Za-z_][A-Za-z_0-9]*):", r'\1"\2":', body[:end])
    records = json.loads(re.sub(r",(\s*[}\]])", r"\1", as_json))
    assert records, f"{path.name}: parsed no records -- fix this extractor"
    assert {r.get("doc_id"): record_errors(r) for r in records if record_errors(r)} == {}


def test_every_record_the_docs_show_would_load():
    """A README snippet is the first corpus anybody writes, so one that no longer validates teaches
    a shape the importer refuses."""
    from tests.conftest import REPO_ROOT

    shown = {}
    for rel in (
        "README.md",
        "examples/bring-your-own-corpus/README.md",
        "backlot/schemas/README.md",
    ):
        for line in (REPO_ROOT / rel).read_text().split("\n"):
            text = line.strip()
            if text.startswith('{"source_type"') and text.endswith("}"):
                shown.setdefault(rel, []).append(json.loads(text))
    assert shown, "no record snippets found in the docs"

    bad = {
        f"{rel}: {rec.get('doc_id') or rec.get('title') or rec['source_type']}": errors
        for rel, records in shown.items()
        for rec in records
        if (errors := record_errors(rec))
    }
    assert bad == {}


def _module_constants(tree, names) -> dict:
    """The module-level literal assignments among `names`, so a corpus is read with the values its
    own example uses. Anything not a literal -- a call, an f-string, a value built at import --
    stays absent and keeps its placeholder."""
    import ast

    out = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if getattr(target, "id", None) in names:
                try:
                    out[target.id] = ast.literal_eval(stmt.value)
                except (ValueError, SyntaxError):
                    pass
    return out


def _inline_corpora():
    """Every inline `CORPUS` literal under examples/, as (path, records).

    Read with `ast` rather than imported: an example's module body spins up a server.
    """
    import ast

    from tests.conftest import REPO_ROOT

    out = []
    for path in sorted((REPO_ROOT / "examples").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(getattr(t, "id", None) == "CORPUS" for t in node.targets):
                continue
            names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            # The names an example interpolates are its own module constants -- a bucket, a repo, a
            # mailbox owner's address -- so read them off the module. Standing one address-shaped
            # string in for all of them satisfied `format: email` but not the constraints a
            # container carries: it put an address in `repo`, where the charset refuses one, and a
            # value no example writes is a value no example is checked on.
            scope = dict.fromkeys(names, "placeholder@example.com") | _module_constants(tree, names)
            try:
                corpus = eval(  # noqa: S307
                    compile(ast.Expression(node.value), "<corpus>", "eval"), {}, scope
                )
            except Exception:
                continue
            if isinstance(corpus, list):
                out.append((path.relative_to(REPO_ROOT).as_posix(), corpus))
    return out


def test_every_example_corpus_states_every_required_fact():
    gaps = {}
    for name, corpus in _inline_corpora():
        found = [_unstated(r) for r in corpus if isinstance(r, dict) and _unstated(r)]
        if found:
            gaps[name] = found
    assert gaps == {}


def test_every_example_corpus_validates():
    for name, corpus in _inline_corpora():
        for rec in corpus:
            if isinstance(rec, dict):
                assert record_errors(rec) == [], f"{name}: {rec.get('doc_id') or rec.get('title')}"


def _example_corpus():
    from tests.conftest import REPO_ROOT

    return REPO_ROOT / "examples" / "bring-your-own-corpus" / "sample_corpus.jsonl"


def _example_records():
    import json

    return [json.loads(line) for line in _example_corpus().read_text().split("\n") if line.strip()]


def test_example_corpus_is_valid():
    """`examples/bring-your-own-corpus/sample_corpus.jsonl` is the file the walkthrough loads and
    the one a reader copies from, and nothing validated it — `sample_corpus_path` above is the
    in-code conftest SAMPLE, a different corpus. Read the shipped file itself."""
    assert validate_file(_example_corpus()) == []


def test_example_corpus_covers_every_served_source():
    """It is documented as "a fully-populated record of every source type", so a source missing
    from it is a broken promise — and one that goes unnoticed: `linear` was absent for two
    releases after its loader landed, because only a human running `run.py` ever read this file."""
    assert {r["source_type"] for r in _example_records()} == set(store.SOURCE_TABLE)


def test_example_corpus_populates_every_field_the_schemas_declare():
    """This corpus's whole job: show every field a record may carry, so a reader can see that none
    of a response has to be synthesized.

    Derived from the schemas rather than a list kept here, so adding a field to a schema fails this
    test until the example carries it — the same way a new SOURCE already fails the test above.
    Coverage is the UNION over one source's records, because some fields are alternatives (a
    `visibility` record and a `readers` record; a fireflies transcript written as `sentences` and
    one written as `content`) and no single record can hold both sides.
    """
    used: dict[str, set[str]] = {}
    for r in _example_records():
        used.setdefault(r["source_type"], set()).update(r)
    missing = {
        src: sorted((set(schema["properties"]) - {"source_type"}) - used.get(src, set()))
        for src, schema in validation.SERVICE_SCHEMAS.items()
    }
    assert {s: m for s, m in missing.items() if m} == {}


def test_a_conditional_requirement_says_which_records_it_applies_to():
    """`path` is required of source files and of nothing else, but the rule is an
    `if`/`then`, so jsonschema reports it at the document root with a bare message. A
    23,000-record corpus was rejected with dozens of identical
    "<root>: 'path' is a required property" lines, and none of them said that only
    subtype 'file' rows needed one — the condition sat in the schema for the reader to
    go and find."""
    errs = record_errors(
        complete(
            "github",
            _omit={"path"},
            title="settlement.py",
            content="code",
            doc_id="gh-1",
            subtype="file",
        )
    )
    assert errs == ["gh-1: 'path' is a required property when subtype is \"file\""]

    # An issue is not a file, so the same schema asks nothing of it.
    assert (
        record_errors(complete("github", title="t", content="c", doc_id="gh-2", subtype="issue"))
        == []
    )


def test_a_validation_error_names_the_record_it_is_about():
    """A line number is not an identifier: a sharded artifact numbers every shard from
    one, and the id is what the author's own build wrote down and can grep for. Absent an
    id, the title serves; absent both, the root placeholder is all there is."""
    by_id = record_errors(complete("github", title="t", content="c", doc_id="d1", subtype="nope"))
    assert by_id and by_id[0].startswith("d1 [subtype]: ")

    by_title = record_errors({"source_type": "linear", "title": "Cutover plan", "nope": 1})
    assert by_title and all(e.startswith("Cutover plan") for e in by_title)

    anonymous = record_errors({"source_type": "github", "content": "c"})
    assert anonymous and all(e.startswith("<root>: ") for e in anonymous)
    assert "<root>: 'title' is a required property" in anonymous


def test_a_label_cannot_be_mistaken_for_the_field_path():
    """A label is free text and a space does not close it. A record titled `subtype` whose
    `subtype` field is wrong read "subtype subtype: ...", naming the field twice and marking
    neither as the record — so the path is bracketed. The path's own spelling is untouched:
    the slash form is what the rest of this suite pins."""
    assert record_errors(complete("github", title="subtype", content="c", subtype=9)) == [
        "subtype [subtype]: 9 is not one of ['issue', 'pull_request', 'file']"
    ]

    # A record-wide error has no path, so it gains no brackets either.
    assert record_errors(complete("github", _omit={"title"}, content="c", doc_id="d")) == [
        "d: 'title' is a required property"
    ]

    # A nameless record marks its path as a path, so an unbracketed head is always the record:
    # bare, `subtype: ` was both this and the labelled record-wide line just above it.
    nameless = record_errors(complete("github", title="", content="c", subtype=9))
    assert nameless and any(e.startswith("<root> [subtype]: ") for e in nameless)
    assert all(e.startswith("<root> ") for e in nameless)


def test_the_condition_clause_is_omitted_rather_than_guessed():
    """An unconditional rule gets no clause, and a condition shape the renderer does not
    recognise gets none either — a wrong "when" is worse than no "when"."""
    plain = record_errors(complete("github", _omit={"title"}, content="c", doc_id="d"))
    assert plain == ["d: 'title' is a required property"]
    assert (
        validation._when_clause(
            {"allOf": [{"then": {"required": ["x"]}}]},
            ["allOf", 0, "then", "required"],
            {"required": ["x"]},
        )
        == ""
    )
    assert validation._when_clause({}, ["required"], {}) == ""

    # `then` under `properties` is a field name a corpus author picked, not the keyword, and
    # the `if` beside it is another field — a schema that happens to hold both fields must not
    # be read as a condition over them.
    assert (
        validation._when_clause(
            {"properties": {"if": {"properties": {"mode": {"const": "fast"}}}, "then": {}}},
            ["properties", "then", "type"],
            {},
        )
        == ""
    )


def test_a_multi_value_predicate_is_bracketed_against_the_and():
    """Values within a field were joined by " or " and the fields by " and ", with no grouping
    between the two -- so `a is "x" or "y" and b is "z"` reads as `x or (y and z)`, which is not
    what the schema says. A single-value `enum` is unaffected."""
    two = {
        "allOf": [
            {
                "if": {
                    "properties": {"a": {"enum": ["x", "y"]}, "b": {"const": "z"}},
                    "required": ["a", "b"],
                },
                "then": {"required": ["q"]},
            }
        ]
    }
    assert validation._when_clause(
        two, ["allOf", 0, "then", "required"], two["allOf"][0]["then"]
    ) == (' when a is one of ["x", "y"] and b is "z"')

    one = {"allOf": [{"if": {"properties": {"a": {"enum": ["x"]}}, "required": ["a"]}, "then": {}}]}
    assert validation._when_clause(
        one, ["allOf", 0, "then", "required"], one["allOf"][0]["then"]
    ) == (' when a is "x"')


def test_a_negated_predicate_still_names_its_condition():
    """Both shapes a schema states an exclusion in. `{"not": {"const": …}}` predicates on a VALUE
    being anything else; `{"not": {"required": […]}}` predicates on a field's ABSENCE, which is how
    two fields say "one of us". Rendering neither left the rule reporting as a bare
    `'state' is a required property` — the line #47 was written about — and left a record that
    states neither of the two fields never learning that stating the other would do."""
    value = {
        "allOf": [
            {
                "if": {"properties": {"subtype": {"not": {"const": "file"}}}},
                "then": {"required": ["state"]},
            }
        ]
    }
    assert (
        validation._when_clause(value, ["allOf", 0, "then", "required"], value["allOf"][0]["then"])
        == ' when subtype is not "file" (or absent)'
    )
    presence = {
        "allOf": [
            {"if": {"not": {"required": ["host_email"]}}, "then": {"required": ["author_email"]}}
        ]
    }
    assert (
        validation._when_clause(
            presence, ["allOf", 0, "then", "required"], presence["allOf"][0]["then"]
        )
        == " when host_email is absent"
    )
    # and the rules the shipped schemas state in those shapes, end to end
    assert record_errors(complete("github", doc_id="gh-1", subtype="issue", _omit={"state"})) == [
        "gh-1: 'state' is a required property when subtype is not \"file\" (or absent)"
    ]
    assert record_errors(complete("fireflies", doc_id="ff-1", _omit={"author_email"})) == [
        "ff-1: 'author_email' is a required property when host_email is absent"
    ]


def test_a_malformed_condition_reports_rather_than_raises():
    """A schema is only `json.loads`ed at import -- `check_schema` runs in a test, not on load --
    so a hand-edited file can reach the renderer with a non-object where a subschema belongs. The
    diagnostics path must degrade to "no clause" there, because raising turns every record into a
    traceback, including on the `--dry-run` that exists to report problems."""
    for broken in (
        {"if": {"properties": ["a"]}, "then": {}},
        {"if": {"properties": "a"}, "then": {}},
        {"if": {"properties": 1}, "then": {}},
    ):
        assert validation._when_clause(broken, ["then", "required"], broken["then"]) == ""


def test_a_predicate_field_the_condition_does_not_require_says_so():
    """`properties` constrains a value; it does not require the field. An `if` with no sibling
    `required` succeeds for a record that omits the field, so `then` binds that record too --
    and a bare `when subtype is "file"` would send its author hunting for a field they never
    wrote. The shipped github condition DOES carry `required`, so its clause is unchanged."""
    from jsonschema import Draft202012Validator

    unguarded = {
        "type": "object",
        "allOf": [
            {"if": {"properties": {"subtype": {"const": "file"}}}, "then": {"required": ["path"]}}
        ],
    }
    # No `subtype` anywhere in the record, yet the requirement binds it.
    (err,) = Draft202012Validator(unguarded).iter_errors({})
    assert (
        validation._when_clause(unguarded, err.schema_path, err.schema)
        == ' when subtype is "file" (or absent)'
    )

    guarded = {
        "type": "object",
        "allOf": [
            {
                "if": {"properties": {"subtype": {"const": "file"}}, "required": ["subtype"]},
                "then": {"required": ["path"]},
            }
        ],
    }
    assert not list(Draft202012Validator(guarded).iter_errors({}))
    (bound,) = Draft202012Validator(guarded).iter_errors({"subtype": "file"})
    assert validation._when_clause(guarded, bound.schema_path, bound.schema) == (
        ' when subtype is "file"'
    )

    # The real schema is the guarded form, so the headline diagnostic keeps its plain clause.
    assert record_errors(
        complete("github", _omit={"path"}, subtype="file", title="t", content="c", doc_id="gh-1")
    ) == ["gh-1: 'path' is a required property when subtype is \"file\""]


def test_a_clause_is_not_read_across_a_ref_hop():
    """`err.schema_path` omits the `$ref` keyword it passed through, so a path from inside a
    referenced subschema reads as a root-relative one. Walking the root by it can land on a
    different conditional and name a field the record never mentions, so the walk is only trusted
    when it arrives at the subschema that actually reported the error."""
    from jsonschema import Draft202012Validator

    root = {
        "type": "object",
        "$ref": "#/$defs/Sub",
        "allOf": [
            {
                "if": {"properties": {"mode": {"const": "fast"}}, "required": ["mode"]},
                "then": {"required": ["speed"]},
            }
        ],
        "$defs": {
            "Sub": {
                "allOf": [
                    {
                        "if": {"properties": {"kind": {"const": "blob"}}, "required": ["kind"]},
                        "then": {"required": ["sha"]},
                    }
                ]
            }
        },
    }
    (err,) = Draft202012Validator(root).iter_errors({"kind": "blob"})
    # The hop is gone from both spellings of the path, so neither is a way to tell them apart.
    assert list(err.schema_path) == ["allOf", 0, "then", "required"]
    assert list(err.absolute_schema_path) == ["allOf", 0, "then", "required"]
    # That path also addresses the ROOT's conditional, whose `if` is over `mode` -- a field this
    # record does not carry. No clause is better than that one.
    assert validation._when_clause(root, err.schema_path, err.schema) == ""

    # A path that does arrive at the failing subschema still gets its clause, including when the
    # error sits deeper inside `then` than the branch itself.
    nested = {
        "type": "object",
        "allOf": [
            {
                "if": {"properties": {"subtype": {"const": "file"}}, "required": ["subtype"]},
                "then": {"properties": {"path": {"type": "string"}}},
            }
        ],
    }
    (deep,) = Draft202012Validator(nested).iter_errors({"subtype": "file", "path": 9})
    assert list(deep.schema_path) == ["allOf", 0, "then", "properties", "path", "type"]
    assert (
        validation._when_clause(nested, deep.schema_path, deep.schema) == ' when subtype is "file"'
    )


def test_an_else_branch_rules_out_the_condition_whole():
    """`else` is in force when the predicate fails, so a two-field condition is ruled out as a
    pair. Negating each field on its own would read as "a is not x AND b is not y", which the
    schema never says — a record with a == x and b != y takes the `else` too."""
    two_field = {
        "allOf": [{"if": {"properties": {"a": {"const": "x"}, "b": {"const": "y"}}}, "else": {}}]
    }
    assert (
        validation._when_clause(
            two_field, ["allOf", 0, "else", "required"], two_field["allOf"][0]["else"]
        )
        == ' unless a is "x" (or absent) and b is "y" (or absent)'
    )


def test_a_label_stays_on_one_line_and_admits_being_cut():
    """`--dry-run` prints one problem per line, and a title is free text. A silently cut label
    also reads as a shorter id — one the author would search the corpus for and never find."""
    multiline = record_errors(
        {"source_type": "github", "content": "c", "title": "two\nlines", "nope": 1}
    )
    assert multiline and all("\n" not in m for m in multiline)
    assert multiline[0].startswith("two lines: ")

    cut = record_errors({"source_type": "github", "content": "c", "doc_id": "d" * 80})
    label = cut[0].split(":")[0]
    assert len(label) == 60 and label.endswith("…")
