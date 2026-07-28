"""The Linear filter compiler (`app/graphql/linear_filters.py`).

Its own file because it is a self-contained translation — filter input in, SQL fragment out — and
because a mutation review found 16 of 17 injected faults surviving the rest of the suite. A wrong
filter is the worst kind of defect this mock can have: it returns plausible-looking data rather
than an error, so every comparator gets an assertion that pins its BOUNDARY (`lte` vs `lt`), not
just that it filters something.

Driven through the real GraphQL endpoint rather than by calling the compiler, so the SDL, the
resolver plumbing and the SQL are all covered by the same assertion.
"""
from __future__ import annotations

import json
import os

import pytest
from starlette.testclient import TestClient

from app.config import Settings, get_settings

# priority/estimate are chosen so `lte: 2` and `lt: 2` differ, and dates so `gte`/`gt` differ.
CORPUS = [
    {"source_type": "linear", "doc_id": "f1", "team": "engineering", "group": "engineering",
     "title": "Alpha gateway", "content": "token bucket refill", "identifier": "ENG-1",
     "author_email": "ava@acme.com", "author_groups": ["engineering"], "visibility": "public",
     "state": "In Progress", "priority": 1, "estimate": 1, "labels": ["bug", "gateway"],
     "project": "runtime", "cycle": "2026-W01", "assignee": "bob@acme.com",
     "assigneeName": "Bob Stone", "created": "2026-01-01T00:00:00Z",
     "comments": [{"content": "first note", "author_email": "bob@acme.com"}]},
    {"source_type": "linear", "doc_id": "f2", "team": "engineering", "group": "engineering",
     "title": "Bravo 100% match_case", "content": "x", "identifier": "ENG-2",
     "author_email": "ava@acme.com", "author_groups": ["engineering"], "visibility": "public",
     "state": "Done", "priority": 2, "estimate": 5, "labels": ["bug"],
     "project": "runtime", "created": "2026-02-01T00:00:00Z",
     "comments": [{"content": "second note", "author_email": "ava@acme.com"}]},
    {"source_type": "linear", "doc_id": "f3", "team": "design", "group": "design",
     "title": "Charlie", "content": "y", "identifier": "DES-1",
     "author_email": "mia@acme.com", "author_groups": ["design"], "visibility": "public",
     "state": "Canceled", "priority": 4, "labels": [], "created": "2026-03-01T00:00:00Z"},
]


@pytest.fixture(scope="module")
def fclient(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("linear-filters")
    settings = Settings(data_dir=data_dir)
    corpus = data_dir / "c.jsonl"
    corpus.write_text("\n".join(json.dumps(r) for r in CORPUS))
    from app.importer.byo import load
    load(corpus, settings)

    from app.main import app
    prev = os.environ.get("MOCK_DATA_DIR")
    os.environ["MOCK_DATA_DIR"] = str(data_dir)
    get_settings.cache_clear()
    try:
        with TestClient(app) as c:
            c.__dict__["_admin"] = settings.admin_token
            yield c
    finally:
        get_settings.cache_clear()
        if prev is None:
            os.environ.pop("MOCK_DATA_DIR", None)
        else:
            os.environ["MOCK_DATA_DIR"] = prev


def ids(fclient, filter_literal, root="issues") -> list[str]:
    """Identifiers matching an IssueFilter, in a stable order."""
    q = "{ %s(first: 50, filter: %s) { nodes { identifier } } }" % (root, filter_literal)
    body = fclient.post("/linear/graphql", json={"query": q},
                        headers={"Authorization": fclient.__dict__["_admin"]}).json()
    assert "errors" not in body, body["errors"]
    return sorted(n["identifier"] for n in body["data"][root]["nodes"])


def err(fclient, filter_literal, root="issues") -> str:
    q = "{ %s(first: 50, filter: %s) { nodes { identifier } } }" % (root, filter_literal)
    body = fclient.post("/linear/graphql", json={"query": q},
                        headers={"Authorization": fclient.__dict__["_admin"]}).json()
    assert "errors" in body, f"expected an error, got {body}"
    return body["errors"][0]["message"]


ALL = ["DES-1", "ENG-1", "ENG-2"]


# --- numeric comparators: each pinned at its boundary -------------------------------

def test_number_comparators_are_pinned_at_their_boundary(fclient):
    assert ids(fclient, "{priority: {eq: 2}}") == ["ENG-2"]
    assert ids(fclient, "{priority: {neq: 2}}") == ["DES-1", "ENG-1"]
    assert ids(fclient, "{priority: {lt: 2}}") == ["ENG-1"]          # excludes 2
    assert ids(fclient, "{priority: {lte: 2}}") == ["ENG-1", "ENG-2"]  # includes 2
    assert ids(fclient, "{priority: {gt: 2}}") == ["DES-1"]          # excludes 2
    assert ids(fclient, "{priority: {gte: 2}}") == ["DES-1", "ENG-2"]  # includes 2
    assert ids(fclient, "{priority: {in: [1, 4]}}") == ["DES-1", "ENG-1"]
    assert ids(fclient, "{priority: {nin: [1, 4]}}") == ["ENG-2"]


def test_null_comparator_on_a_nullable_number(fclient):
    assert ids(fclient, "{estimate: {null: true}}") == ["DES-1"]
    assert ids(fclient, "{estimate: {null: false}}") == ["ENG-1", "ENG-2"]


def test_neq_keeps_rows_whose_column_is_null(fclient):
    """NULL never equals anything, so a bare `<> ?` would drop the rows a caller asking for
    "not X" expects to see."""
    assert "DES-1" in ids(fclient, '{estimate: {neq: 5}}')


def test_empty_in_list_matches_nothing_and_empty_nin_matches_everything(fclient):
    assert ids(fclient, "{priority: {in: []}}") == []
    assert ids(fclient, "{priority: {nin: []}}") == ALL


# --- string comparators --------------------------------------------------------------

def test_string_comparators_are_distinct_from_one_another(fclient):
    assert ids(fclient, '{title: {eq: "Charlie"}}') == ["DES-1"]
    assert ids(fclient, '{title: {contains: "gateway"}}') == ["ENG-1"]
    assert ids(fclient, '{title: {startsWith: "Alpha"}}') == ["ENG-1"]
    assert ids(fclient, '{title: {endsWith: "gateway"}}') == ["ENG-1"]
    # startsWith must NOT behave like contains
    assert ids(fclient, '{title: {startsWith: "gateway"}}') == []
    assert ids(fclient, '{title: {containsIgnoreCase: "ALPHA"}}') == ["ENG-1"]
    assert ids(fclient, '{title: {eqIgnoreCase: "charlie"}}') == ["DES-1"]


def test_like_wildcards_in_the_needle_stay_literal(fclient):
    """`%` and `_` are SQL LIKE wildcards. Unescaped, `%` matches everything and `_` any single
    character, so a user-supplied needle would quietly widen the query."""
    assert ids(fclient, '{title: {contains: "100%"}}') == ["ENG-2"]     # literal %, not "match all"
    assert ids(fclient, '{title: {contains: "%"}}') == ["ENG-2"]
    assert ids(fclient, '{title: {contains: "match_case"}}') == ["ENG-2"]
    assert ids(fclient, '{title: {contains: "match-case"}}') == []      # `_` is not a wildcard


# --- dates ---------------------------------------------------------------------------

def test_date_comparators_coerce_iso8601_to_the_stored_epoch(fclient):
    """The column is unix seconds; without coercion every date filter compares a string to an
    integer and silently matches nothing (or everything)."""
    assert ids(fclient, '{createdAt: {gt: "2026-01-15T00:00:00Z"}}') == ["DES-1", "ENG-2"]
    assert ids(fclient, '{createdAt: {lt: "2026-01-15T00:00:00Z"}}') == ["ENG-1"]
    assert ids(fclient, '{createdAt: {gte: "2026-02-01T00:00:00Z"}}') == ["DES-1", "ENG-2"]


def test_a_malformed_date_is_an_error_not_a_silent_mismatch(fclient):
    assert "ISO-8601" in err(fclient, '{createdAt: {gt: "not-a-date"}}')


# --- nested object filters -------------------------------------------------------------

def test_nested_filters_on_relations(fclient):
    assert ids(fclient, '{state: {name: {eq: "Done"}}}') == ["ENG-2"]
    assert ids(fclient, '{team: {key: {eq: "DES"}}}') == ["DES-1"]
    assert ids(fclient, '{project: {name: {eq: "runtime"}}}') == ["ENG-1", "ENG-2"]
    assert ids(fclient, '{assignee: {email: {eq: "bob@acme.com"}}}') == ["ENG-1"]
    assert ids(fclient, "{assignee: {null: true}}") == ["DES-1", "ENG-2"]
    assert ids(fclient, "{project: {null: true}}") == ["DES-1"]


def test_derived_state_type_expands_to_the_matching_names(fclient):
    """`state.type` has no column — it is a pure function of the name — so it compiles to an IN
    over the names that satisfy the predicate. A derivation that matched everything would make
    this filter a silent no-op."""
    assert ids(fclient, '{state: {type: {eq: "completed"}}}') == ["ENG-2"]
    assert ids(fclient, '{state: {type: {eq: "canceled"}}}') == ["DES-1"]
    assert ids(fclient, '{state: {type: {eq: "started"}}}') == ["ENG-1"]
    assert ids(fclient, '{state: {type: {in: ["completed", "canceled"]}}}') == ["DES-1", "ENG-2"]


def test_derived_team_key_is_not_the_team_name(fclient):
    assert ids(fclient, '{team: {key: {eq: "ENG"}}}') == ["ENG-1", "ENG-2"]
    assert ids(fclient, '{team: {name: {eq: "engineering"}}}') == ["ENG-1", "ENG-2"]
    assert ids(fclient, '{team: {key: {eq: "engineering"}}}') == []    # key != name


def test_negated_derived_filter_keeps_null_column_rows(fclient):
    """A row with no project cannot BE the excluded project. The column comparator's `neq` says
    so explicitly, and the derived IN-list form has to agree with it."""
    by_name = ids(fclient, '{project: {name: {neq: "runtime"}}}')
    by_id = ids(fclient, '{project: {id: {neq: "%s"}}}'
                % __import__("app.synth", fromlist=["x"]).linear_project_id("runtime"))
    assert by_name == by_id == ["DES-1"]


# --- labels (the JSON column) ------------------------------------------------------------

def test_labels_some_and_every(fclient):
    assert ids(fclient, '{labels: {some: {name: {eq: "gateway"}}}}') == ["ENG-1"]
    assert ids(fclient, '{labels: {some: {name: {eq: "bug"}}}}') == ["ENG-1", "ENG-2"]
    # `every` also holds for an issue with no labels, as Linear's collection filters do
    assert ids(fclient, '{labels: {every: {name: {eq: "bug"}}}}') == ["DES-1", "ENG-2"]


def test_labels_some_is_not_every(fclient):
    """ENG-1 has bug AND gateway, so `every: bug` must exclude it while `some: bug` includes it."""
    assert "ENG-1" in ids(fclient, '{labels: {some: {name: {eq: "bug"}}}}')
    assert "ENG-1" not in ids(fclient, '{labels: {every: {name: {eq: "bug"}}}}')


def test_nested_and_or_inside_a_labels_filter_is_applied(fclient):
    """An inner and/or that compiled to nothing dropped the WHOLE filter, so a query narrowing to
    a nonexistent label returned the entire corpus."""
    assert ids(fclient, '{labels: {some: {and: [{name: {eq: "nonexistent"}}]}}}') == []
    assert ids(fclient, '{labels: {some: {or: [{name: {eq: "gateway"}}]}}}') == ["ENG-1"]
    assert ids(fclient, '{labels: {some: {and: [{name: {eq: "bug"}}, '
                        '{name: {eq: "gateway"}}]}}}') == []   # one label can't be both
    assert ids(fclient, '{labels: {some: {or: [{name: {eq: "bug"}}, '
                        '{name: {eq: "gateway"}}]}}}') == ["ENG-1", "ENG-2"]


# --- boolean composition ------------------------------------------------------------------

def test_top_level_and_or_are_not_interchangeable(fclient):
    both = '{and: [{team: {key: {eq: "ENG"}}}, {priority: {eq: 1}}]}'
    either = '{or: [{team: {key: {eq: "ENG"}}}, {priority: {eq: 4}}]}'
    assert ids(fclient, both) == ["ENG-1"]
    assert ids(fclient, either) == ["DES-1", "ENG-1", "ENG-2"]


def test_sibling_keys_are_anded(fclient):
    assert ids(fclient, '{team: {key: {eq: "ENG"}}, priority: {eq: 4}}') == []


def test_or_mixing_a_derived_and_a_column_branch(fclient):
    assert ids(fclient, '{or: [{state: {type: {eq: "canceled"}}}, {priority: {eq: 1}}]}') == \
        ["DES-1", "ENG-1"]


# --- "declared means implemented" ------------------------------------------------------

def test_an_unsupported_filter_field_is_an_error_not_a_dropped_filter(fclient):
    """The guarantee the module exists to provide: never answer a narrowing query with the full
    set. graphql-core rejects a field the SDL doesn't declare; the compiler rejects one it
    declares but cannot evaluate."""
    q = '{ issues(filter: {nope: {eq: "x"}}) { nodes { identifier } } }'
    r = fclient.post("/linear/graphql", json={"query": q},
                     headers={"Authorization": fclient.__dict__["_admin"]})
    assert r.status_code == 400
    assert "not defined by type 'IssueFilter'" in r.json()["errors"][0]["message"]


def test_an_unsupported_comparator_is_an_error(fclient):
    q = '{ issues(filter: {title: {nope: "x"}}) { nodes { identifier } } }'
    r = fclient.post("/linear/graphql", json={"query": q},
                     headers={"Authorization": fclient.__dict__["_admin"]})
    assert r.status_code == 400


def test_comment_filter_narrows(fclient):
    q = '{ comments(first: 50, filter: {body: {contains: "second"}}) { nodes { body } } }'
    body = fclient.post("/linear/graphql", json={"query": q},
                        headers={"Authorization": fclient.__dict__["_admin"]}).json()
    assert [n["body"] for n in body["data"]["comments"]["nodes"]] == ["second note"]


def test_comment_filter_by_the_served_id_round_trips(fclient):
    """`Comment.id` is served as a synthesized UUID, so a filter written from one has to be
    translated back to the stored row id or it can never match what the client just read."""
    listed = fclient.post("/linear/graphql",
                          json={"query": "{ comments(first: 1) { nodes { id body } } }"},
                          headers={"Authorization": fclient.__dict__["_admin"]}).json()
    first = listed["data"]["comments"]["nodes"][0]
    q = '{ comments(first: 50, filter: {id: {eq: "%s"}}) { nodes { body } } }' % first["id"]
    got = fclient.post("/linear/graphql", json={"query": q},
                       headers={"Authorization": fclient.__dict__["_admin"]}).json()
    assert [n["body"] for n in got["data"]["comments"]["nodes"]] == [first["body"]]
