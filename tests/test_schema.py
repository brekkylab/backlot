"""BYO corpus JSON Schema validation (schemas/ + app.validation)."""
import json

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
