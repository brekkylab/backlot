"""BYO corpus JSON Schema validation (schemas/ + app.validation)."""
import json
from datetime import datetime

import pytest

from app import store, validation
from app.config import Settings
from app.validation import record_errors, validate_file
from app.importer.byo import load


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
    errs = _first_error({"source_type": "confluence", "title": "t", "content": "c",
                         "visibility": "secret"})
    assert any("visibility" in e for e in errs)


def test_bad_subtype_enum_rejected():
    errs = _first_error({"source_type": "github", "title": "t", "content": "c",
                         "subtype": "task"})
    assert any("subtype" in e for e in errs)


def test_unknown_top_level_key_rejected():
    # a typo'd field is the most common corpus mistake
    errs = _first_error({"source_type": "jira", "title": "t", "content": "c",
                         "athor_email": "a@b.com"})
    assert any("athor_email" in e for e in errs)


def test_title_required_except_slack():
    assert _first_error({"source_type": "gmail", "content": "c"})  # missing title -> error
    assert _first_error({"source_type": "slack", "content": "c"}) == []  # slack ok without title


def test_hubspot_record_accepted():
    # a CRM record: the object type is the grouping unit, typed properties are free-form, and
    # associations name the target record by doc_id
    assert _first_error({
        "source_type": "hubspot", "object_type": "contacts", "doc_id": "hs-c1",
        "title": "Ava Stone", "content": "Ava Stone — VP Platform at Acme Health",
        "author_email": "owner@acme.com",
        "properties": {"firstname": "Ava", "lastname": "Stone", "email": "ava@acme-health.com"},
        "associations": [{"to": "hs-co1", "to_type": "companies", "label": "Primary"}],
    }) == []


def test_hubspot_association_requires_a_target():
    errs = _first_error({"source_type": "hubspot", "object_type": "contacts", "title": "t",
                         "content": "c", "associations": [{"to_type": "companies"}]})
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
    assert _first_error({"source_type": "jira", "title": "t", "content": "c",
                         "comments": [{"content": "x"}]}) == []


def test_comment_needs_content_or_body():
    errs = _first_error({"source_type": "jira", "title": "t", "content": "c",
                         "comments": [{"author_email": "a@b.com"}]})
    assert any("comments/0" in e for e in errs)


def test_replies_only_on_slack():
    assert any("replies" in e for e in _first_error(
        {"source_type": "confluence", "title": "t", "content": "c", "replies": [{"content": "x"}]}))
    assert _first_error({"source_type": "slack", "content": "c",
                         "replies": [{"content": "x"}]}) == []


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
    from app.validation import record_errors
    assert record_errors({"source_type": "s3", "bucket": "b", "key": "k",
                          "title": "t", "content": "c"}) == []
    errs = record_errors({"source_type": "s3", "bucket": "b", "title": "t", "content": "c"})
    assert errs and any("key" in e for e in errs)


# --- Linear -----------------------------------------------------------------------

def test_linear_record_accepts_linears_own_field_names():
    """A corpus written against the Linear API should need no renaming: `state` (not status),
    camelCase `branchName`/`dueDate`, `assignee` as an email."""
    assert record_errors({
        "source_type": "linear", "title": "t", "content": "c", "team": "engineering",
        "identifier": "ENG-1", "state": "In Progress", "priority": 2, "estimate": 3,
        "labels": ["bug"], "project": "p", "cycle": "2025-W08", "branchName": "ava/eng-1-t",
        "dueDate": "2026-03-15", "assignee": "ava@acme.com", "assigneeName": "Ava",
        "completedAt": "2026-03-01T00:00:00Z",
    }) == []


def test_linear_priority_accepts_a_label_or_the_numeric_scale():
    for value in (0, 4, "P0", "Urgent"):
        assert record_errors({"source_type": "linear", "title": "t", "content": "c",
                              "priority": value}) == []


def test_linear_rejects_jiras_vocabulary():
    """`status` is Jira's word for it; accepting it silently would drop the value on load."""
    errs = record_errors({"source_type": "linear", "title": "t", "content": "c",
                          "status": "In Progress"})
    assert any("status" in e for e in errs)


def test_linear_rejects_an_unknown_key():
    errs = record_errors({"source_type": "linear", "title": "t", "content": "c", "nope": 1})
    assert any("nope" in e for e in errs)


def test_linear_byo_round_trip_serves_what_it_loaded(tmp_path):
    """The whole BYO contract for a new source: a `source_type: "linear"` corpus imports,
    validates, and comes back out of the store on the API's own columns."""
    corpus = tmp_path / "linear.jsonl"
    corpus.write_text(json.dumps({
        "source_type": "linear", "doc_id": "byo-1", "team": "platform", "group": "platform",
        "title": "Ship the cache", "content": "Two-tier cache for the gateway.",
        "author_email": "ava@acme.com", "identifier": "PLA-7", "state": "Done",
        "priority": "Urgent", "estimate": 8, "labels": ["cache"], "project": "gateway",
        "cycle": "Cycle 41", "dueDate": "2026-04-01", "assignee": "bob@acme.com",
        "assigneeName": "Bob Stone", "completedAt": "2026-03-20T00:00:00Z",
        "comments": [{"content": "Rolled out.", "author_email": "bob@acme.com"}],
    }) + "\n")
    assert validate_file(corpus) == []
    settings = Settings(data_dir=tmp_path)
    assert load(corpus, settings)["counts"] == {"linear": 1}

    conn = store.connect_ro(settings.db_path)
    try:
        row = store.get_document(conn, "linear", "byo-1")
        assert row["team"] == "platform"
        assert row["identifier"] == "PLA-7"
        assert row["state"] == "Done"
        assert row["priority"] == 1                      # "Urgent" -> Linear's scale
        assert row["estimate"] == 8
        assert row["due_date"] == "2026-04-01"
        assert row["assignee_email"] == "bob@acme.com"
        assert row["assignee_display"] == "Bob Stone"
        # `completedAt` is stored as epoch seconds, not left to a derivation.
        assert row["completed_ts"] == int(
            datetime.fromisoformat("2026-03-20T00:00:00+00:00").timestamp())
        assert json.loads(row["labels"]) == ["cache"]
        assert store.get_container(conn, "linear", "platform")["group_id"] == "platform"
        assert [c["body"] for c in store.doc_comments(conn, "linear", "byo-1")] == ["Rolled out."]
    finally:
        conn.close()
