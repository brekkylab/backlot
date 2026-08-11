"""Engine-level tests for the vendor-agnostic GraphQL layer.

Everything here runs against a throwaway SDL that no vendor uses, so these assert the
*engine's* contract (selection, fragments, aliases, variables, directives, introspection,
error envelope, context plumbing) and never a vendor's schema. Per-source endpoint /
search tests belong to the Fireflies and Linear test files.
"""

from __future__ import annotations

import json

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from graphql import GraphQLError
from starlette.testclient import TestClient

from backlot import auth, pagination, store
from backlot.graphql import engine

SDL = """
type Query {
  widget(id: ID!): Widget
  widgets(first: Int = 2): [Widget!]!
  caller: String
  boom: String
}

type Widget {
  id: ID!
  name: String!
  size: Int
  parts: [Part!]!
}

type Part {
  id: ID!
  label: String!
}
"""

WIDGETS = {
    "w1": {"id": "w1", "name": "Gateway", "size": 3, "parts": [{"id": "p1", "label": "bucket"}]},
    "w2": {"id": "w2", "name": "Worker", "size": 7, "parts": []},
}


def _boom(_root, _info):
    raise GraphQLError("no widget factory today")


RESOLVERS = {
    "Query": {
        "widget": lambda _root, _info, id: WIDGETS.get(id),
        "widgets": lambda _root, _info, first: list(WIDGETS.values())[:first],
        "caller": lambda _root, info: info.context["caller"],
        "boom": _boom,
    },
}


@pytest.fixture(scope="module")
def gql() -> engine.Engine:
    return engine.Engine(SDL, RESOLVERS)


# --- field selection ------------------------------------------------------------


def test_selection_returns_only_the_requested_fields(gql):
    r = gql.execute('{ widget(id: "w1") { name } }')
    assert r.payload == {"data": {"widget": {"name": "Gateway"}}}
    assert r.request_error is False


def test_nested_selection_walks_into_object_fields(gql):
    r = gql.execute('{ widget(id: "w1") { parts { label } } }')
    assert r.payload["data"] == {"widget": {"parts": [{"label": "bucket"}]}}


def test_argument_default_applies_when_omitted(gql):
    r = gql.execute("{ widgets { id } }")
    assert r.payload["data"] == {"widgets": [{"id": "w1"}, {"id": "w2"}]}


def test_null_result_for_a_nullable_field(gql):
    r = gql.execute('{ widget(id: "nope") { name } }')
    assert r.payload == {"data": {"widget": None}}


# --- aliases / fragments --------------------------------------------------------


def test_aliases_name_each_selection_independently(gql):
    r = gql.execute('{ a: widget(id: "w1") { name } b: widget(id: "w2") { name } }')
    assert r.payload["data"] == {"a": {"name": "Gateway"}, "b": {"name": "Worker"}}


def test_named_fragment_is_spread_into_the_selection(gql):
    r = gql.execute('{ widget(id: "w1") { ...W } } fragment W on Widget { id name }')
    assert r.payload["data"] == {"widget": {"id": "w1", "name": "Gateway"}}


def test_inline_fragment_is_spread_into_the_selection(gql):
    r = gql.execute('{ widget(id: "w1") { ... on Widget { size } } }')
    assert r.payload["data"] == {"widget": {"size": 3}}


# --- variables ------------------------------------------------------------------


def test_variables_are_coerced_and_passed_to_resolvers(gql):
    r = gql.execute("query W($id: ID!) { widget(id: $id) { name } }", variables={"id": "w2"})
    assert r.payload["data"] == {"widget": {"name": "Worker"}}


def test_uncoercible_variable_is_a_request_error(gql):
    r = gql.execute("query W($id: ID!) { widget(id: $id) { name } }", variables={"id": {"x": 1}})
    assert r.request_error is True
    assert "data" not in r.payload
    assert "$id" in r.payload["errors"][0]["message"]


# --- directives -----------------------------------------------------------------


def test_include_directive_drops_the_field_when_false(gql):
    q = 'query W($show: Boolean!) { widget(id: "w1") { name size @include(if: $show) } }'
    r = gql.execute(q, variables={"show": False})
    assert r.payload["data"] == {"widget": {"name": "Gateway"}}


def test_skip_directive_drops_the_field_when_true(gql):
    q = 'query W($hide: Boolean!) { widget(id: "w1") { name size @skip(if: $hide) } }'
    r = gql.execute(q, variables={"hide": True})
    assert r.payload["data"] == {"widget": {"name": "Gateway"}}


# --- introspection --------------------------------------------------------------


def test_introspection_reports_the_query_root(gql):
    r = gql.execute("{ __schema { queryType { name } } }")
    assert r.payload["data"]["__schema"]["queryType"]["name"] == "Query"


def test_introspection_reports_a_types_fields(gql):
    r = gql.execute('{ __type(name: "Widget") { fields { name } } }')
    names = {f["name"] for f in r.payload["data"]["__type"]["fields"]}
    assert names == {"id", "name", "size", "parts"}


# --- error envelope -------------------------------------------------------------


def test_malformed_document_returns_a_graphql_error_envelope(gql):
    r = gql.execute("{ widget(id: }")
    assert r.request_error is True
    # A parse failure happens before execution, so the spec says `data` is absent entirely
    # (not `null`) — graphql-core's own ExecutionResult.formatted would emit `"data": None`.
    assert "data" not in r.payload
    err = r.payload["errors"][0]
    assert "Syntax Error" in err["message"]
    assert err["locations"]


def test_unknown_field_returns_a_validation_error(gql):
    r = gql.execute('{ widget(id: "w1") { nope } }')
    assert r.request_error is True
    assert "data" not in r.payload
    # graphql-core quotes identifiers with '' where graphql-js uses ""; assert the substance,
    # which is the part a client acts on.
    msg = r.payload["errors"][0]["message"]
    assert msg.startswith("Cannot query field") and "nope" in msg and "Widget" in msg


def test_resolver_error_nulls_the_field_and_reports_its_path(gql):
    r = gql.execute("{ boom }")
    assert r.request_error is False
    assert r.payload["data"] == {"boom": None}
    err = r.payload["errors"][0]
    assert err["message"] == "no widget factory today"
    assert err["path"] == ["boom"]


def test_partial_data_survives_alongside_an_error(gql):
    r = gql.execute('{ boom widget(id: "w1") { name } }')
    assert r.payload["data"] == {"boom": None, "widget": {"name": "Gateway"}}
    assert len(r.payload["errors"]) == 1


# --- operation selection --------------------------------------------------------


def test_operation_name_picks_the_operation_to_run(gql):
    q = 'query A { widget(id: "w1") { name } } query B { widget(id: "w2") { name } }'
    r = gql.execute(q, operation_name="B")
    assert r.payload["data"] == {"widget": {"name": "Worker"}}


def test_ambiguous_operation_without_a_name_is_a_request_error(gql):
    q = 'query A { widget(id: "w1") { name } } query B { widget(id: "w2") { name } }'
    r = gql.execute(q)
    assert r.request_error is True
    assert "data" not in r.payload


# --- context --------------------------------------------------------------------


def test_context_is_visible_to_resolvers(gql):
    r = gql.execute("{ caller }", context={"caller": "ava@acme.com"})
    assert r.payload["data"] == {"caller": "ava@acme.com"}


# --- request body (GraphQL over HTTP) -------------------------------------------


def test_execute_request_runs_the_body_query(gql):
    body = json.dumps({"query": '{ widget(id: "w1") { name } }'}).encode()
    r = gql.execute_request(body)
    assert r.payload["data"] == {"widget": {"name": "Gateway"}}


def test_execute_request_honours_variables_and_operation_name(gql):
    body = json.dumps(
        {
            "query": 'query A { widget(id: "w1") { name } } '
            "query B($id: ID!) { widget(id: $id) { name } }",
            "variables": {"id": "w2"},
            "operationName": "B",
        }
    ).encode()
    r = gql.execute_request(body)
    assert r.payload["data"] == {"widget": {"name": "Worker"}}


def test_execute_request_rejects_a_non_json_body(gql):
    r = gql.execute_request(b"not json")
    assert r.request_error is True
    assert "data" not in r.payload
    assert r.payload["errors"][0]["message"] == "POST body sent invalid JSON."


def test_execute_request_requires_a_query_string(gql):
    r = gql.execute_request(json.dumps({"variables": {}}).encode())
    assert r.request_error is True
    assert r.payload["errors"][0]["message"] == "Must provide query string."


def test_execute_request_rejects_non_object_variables(gql):
    body = json.dumps({"query": "{ widgets { id } }", "variables": "nope"}).encode()
    r = gql.execute_request(body)
    assert r.request_error is True
    assert r.payload["errors"][0]["message"] == "Variables are invalid JSON."


# --- resolver binding -----------------------------------------------------------


def test_binding_a_resolver_to_an_unknown_field_fails_loudly():
    with pytest.raises(ValueError, match="Query.nosuchfield"):
        engine.Engine(SDL, {"Query": {"nosuchfield": lambda *_: None}})


def test_binding_a_resolver_to_an_unknown_type_fails_loudly():
    with pytest.raises(ValueError, match="NoSuchType"):
        engine.Engine(SDL, {"NoSuchType": {"x": lambda *_: None}})


def test_an_unsound_schema_fails_at_construction_not_at_request_time():
    # A schema with no Query root builds happily and would otherwise raise from inside the
    # first request; a vendor's broken SDL should break at import instead. graphql-core
    # reports schema faults as TypeError (build_schema does the same for an unknown type).
    with pytest.raises(TypeError, match="Query root type must be provided"):
        engine.Engine("type Widget { id: ID }")


# --- mounted over HTTP ----------------------------------------------------------
# A throwaway vendor ("acme") wired the way a real GraphQL source will be: an APIRouter with
# a prefix, the API-key auth resolver, and the caller's visible_ids injected into the
# resolver context so the resolver reaches store.py's existing ACL path with no per-source
# ACL code. Proves the seams line up; the vendor schemas themselves ship with their issues.

ACME_SDL = """
type Query {
  documents(source: String!, limit: Int): [Document!]!
}

type Document {
  id: ID!
  title: String!
}
"""


def _resolve_documents(_root, info, source, limit=None):
    ctx = info.context
    rows = store.list_documents(
        ctx["conn"],
        source,
        visible_ids=ctx["visible_ids"],
        limit=pagination.clamp_limit(limit, 10, 50),
    )
    return [{"id": r["doc_id"], "title": r["title"]} for r in rows]


ACME = engine.Engine(ACME_SDL, {"Query": {"documents": _resolve_documents}})


@pytest.fixture
def acme_client(db, acl):
    app = FastAPI()
    app.state.conn = db
    app.state.acl = acl
    router = APIRouter(prefix="/acme")

    @router.post("/graphql")
    async def graphql_endpoint(request: Request):
        caller = auth.resolve_api_key(request)
        if caller is None:
            return JSONResponse(
                {"errors": [{"message": "Authentication required"}]}, status_code=401
            )
        context = {"conn": auth.conn(request), "visible_ids": auth.visible_ids(request, caller)}
        result = ACME.execute_request(await request.body(), context=context)
        return JSONResponse(result.payload, status_code=400 if result.request_error else 200)

    app.include_router(router)
    return TestClient(app)


def _titles(response) -> set[str]:
    return {d["title"] for d in response.json()["data"]["documents"]}


QUERY = '{ documents(source: "confluence") { id title } }'


def test_mounted_endpoint_answers_a_post(acme_client, sample_settings):
    r = acme_client.post(
        "/acme/graphql",
        json={"query": QUERY},
        headers={"Authorization": sample_settings.admin_token},
    )
    assert r.status_code == 200
    assert _titles(r) == {"Engineering Handbook", "On-call Runbook", "Compensation Bands 2026"}


def test_mounted_endpoint_rejects_a_missing_credential(acme_client):
    r = acme_client.post("/acme/graphql", json={"query": QUERY})
    assert r.status_code == 401


def test_mounted_endpoint_returns_a_graphql_envelope_for_a_malformed_document(
    acme_client, sample_settings
):
    r = acme_client.post(
        "/acme/graphql",
        json={"query": "{ documents(source: }"},
        headers={"Authorization": sample_settings.admin_token},
    )
    assert r.status_code == 400
    body = r.json()
    # The failure mode this guards against: FastAPI answering with its own
    # ``{"detail": [...]}`` 422 instead of a GraphQL error envelope.
    assert "detail" not in body
    assert "data" not in body
    assert "Syntax Error" in body["errors"][0]["message"]


def test_mounted_endpoint_returns_a_graphql_envelope_for_a_non_json_body(
    acme_client, sample_settings
):
    r = acme_client.post(
        "/acme/graphql",
        content=b"not json",
        headers={"Authorization": sample_settings.admin_token, "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["errors"][0]["message"] == "POST body sent invalid JSON."


def test_acl_filters_results_through_the_resolver_context(acme_client, tokens):
    """Same query, two callers: each sees only what the ACL grants them."""
    ava = acme_client.post(
        "/acme/graphql", json={"query": QUERY}, headers={"Authorization": tokens["ava@acme.com"]}
    )
    hana = acme_client.post(
        "/acme/graphql", json={"query": QUERY}, headers={"Authorization": tokens["hana@acme.com"]}
    )
    # cf-comp is visibility=group on `people`; ava is engineering, hana is people.
    assert _titles(ava) == {"Engineering Handbook", "On-call Runbook"}
    assert "Compensation Bands 2026" in _titles(hana)
