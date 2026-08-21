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
from graphql import GraphQLError, parse, validate
from starlette.testclient import TestClient

from backlot import auth, pagination, store
from backlot.graphql import engine, mcp_tools
from tests._helpers import selected_fields

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
    return [{"id": r["id"], "title": r["title"]} for r in rows]


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


# --- MCP tool derivation --------------------------------------------------------
# `backlot.graphql.mcp_tools` turns an introspection result into one MCP tool per root Query
# field — how the GraphQL-only sources get the typed toolset the REST sources get from
# `/openapi.json`, which describes nothing here beyond "POST a document".
#
# A second throwaway schema, because these rules need shapes the SDL above has no reason to
# carry: a Relay connection, a nested connection, a self-referential input, a field whose
# argument is required, and object nesting deeper than the depth budget. That the derived
# documents validate against a *vendor's* schema is asserted in the per-source files.

MCP_SDL = """
scalar DateTime

enum Order { asc desc }

input NameComparator { eq: String, contains: String }

input GadgetFilter {
  name: NameComparator
  createdAt: DateTime
  and: [GadgetFilter!]
}

type Query {
  gadget(id: ID!): Gadget!
  gadgets(first: Int, after: String, filter: GadgetFilter, order: Order): GadgetConnection!
  "Loose notes, newest first."
  notes(ids: [ID!], limit: Int): [Note]
}

type GadgetConnection { nodes: [Gadget!]!, pageInfo: PageInfo! }

type PageInfo {
  hasNextPage: Boolean!
  hasPreviousPage: Boolean!
  startCursor: String
  endCursor: String
}

type Gadget {
  id: ID!
  name: String!
  createdAt: DateTime
  owner: Owner
  metrics: Metrics
  parent: Gadget
  tags: [Tag!]
  notes: NoteConnection!
  audit(token: String!): Audit
}

type Tag { id: ID!, label: String }
type Owner { id: ID!, email: String, team: Team, badges: [Tag!] }
type Team { id: ID!, key: String, lead: Owner }
type Metrics { latency: Latency }
type Latency { p95: Float }
type NoteConnection { nodes: [Note!]!, pageInfo: PageInfo! }
type Note { id: ID!, body: String }
type Audit { id: ID! }
"""


def _derive(depth: int = 2, sdl: str = MCP_SDL) -> dict[str, mcp_tools.Tool]:
    payload = engine.Engine(sdl).execute(mcp_tools.INTROSPECTION_QUERY).payload
    return {t.name: t for t in mcp_tools.derive_tools(payload, depth=depth)}


@pytest.fixture(scope="module")
def tools() -> dict[str, mcp_tools.Tool]:
    return _derive()


def test_one_tool_per_root_query_field(tools):
    assert set(tools) == {"gadget", "gadgets", "notes"}


def test_a_tool_asks_for_exactly_its_own_root_field(tools):
    assert selected_fields(tools["gadgets"].document) == {"gadgets"}


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(("gadgets",), id="root"),
        pytest.param(("gadgets", "nodes", "notes"), id="nested"),
    ],
)
def test_a_connection_is_transparent_wherever_it_appears(tools, path):
    assert selected_fields(tools["gadgets"].document, *path) == {"nodes", "pageInfo"}


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(("gadgets", "pageInfo"), id="root"),
        pytest.param(("gadgets", "nodes", "notes", "pageInfo"), id="nested"),
    ],
)
def test_page_info_survives_so_truncation_is_visible(tools, path):
    """A nested page cannot be advanced — there is no argument to carry a cursor into it — so
    `pageInfo` is the only thing standing between a partial answer and a wrong one."""
    assert selected_fields(tools["gadgets"].document, *path) == {
        "hasNextPage",
        "hasPreviousPage",
        "startCursor",
        "endCursor",
    }


@pytest.mark.parametrize(
    "field, why",
    [
        ("parent", "the type is already on the path"),
        ("audit", "its argument is required, so the generator cannot fill it in"),
    ],
)
def test_a_field_the_generator_cannot_fill_in_is_left_out(tools, field, why):
    assert field not in selected_fields(tools["gadgets"].document, "gadgets", "nodes"), why


@pytest.mark.parametrize(
    "depth, path, expected",
    [
        # A leaf is always selected; an object is followed while the budget lasts.
        (2, ("gadgets", "nodes"), {"id", "name", "createdAt", "owner", "metrics", "notes"}),
        (2, ("gadgets", "nodes", "owner"), {"id", "email", "team"}),
        # `Team.lead` is a third object level, one past a budget of 2.
        (2, ("gadgets", "nodes", "owner", "team"), {"id", "key"}),
        (2, ("gadgets", "nodes", "metrics", "latency"), {"p95"}),
        # At budget 1 `Owner` keeps its leaves but loses `team` — and `metrics` goes with it,
        # because `Metrics` is all objects, so it would select nothing and an empty selection
        # set is not a legal document.
        (1, ("gadgets", "nodes"), {"id", "name", "createdAt", "owner", "notes"}),
        (1, ("gadgets", "nodes", "owner"), {"id", "email"}),
        # A connection spends a level like any other object, and its nodes get what is left.
        (1, ("gadgets", "nodes", "notes", "nodes"), {"id", "body"}),
    ],
)
def test_the_depth_budget_bounds_the_selection(depth, path, expected):
    assert selected_fields(_derive(depth)["gadgets"].document, *path) == expected


def test_every_argument_becomes_a_variable(tools):
    declared = parse(tools["gadgets"].document).definitions[0].variable_definitions
    assert {v.variable.name.value for v in declared} == {"first", "after", "filter", "order"}


def test_a_required_argument_is_required_in_the_input_schema(tools):
    assert tools["gadget"].input_schema["required"] == ["id"]
    assert "required" not in tools["gadgets"].input_schema


def test_scalar_and_enum_arguments_map_to_json_schema(tools):
    props = tools["gadgets"].input_schema["properties"]
    assert props["first"]["type"] == "integer"
    assert props["after"]["type"] == "string"
    assert props["order"]["enum"] == ["asc", "desc"]


def test_a_list_argument_is_an_array_of_its_item_type(tools):
    assert tools["notes"].input_schema["properties"]["ids"] == {
        "type": "array",
        "items": {"type": "string"},
    }


def test_an_input_object_expands_under_the_same_path_guard(tools):
    filt = tools["gadgets"].input_schema["properties"]["filter"]
    assert filt["type"] == "object"
    # nested input objects expand, so the comparator keys are visible rather than guessed
    assert set(filt["properties"]["name"]["properties"]) == {"eq", "contains"}
    assert filt["properties"]["createdAt"]["type"] == "string"  # a custom scalar is a string
    assert "and" not in filt["properties"]  # [GadgetFilter!], already on the path


def test_a_bare_list_of_objects_is_selected_for_a_single_result(tools):
    """`gadget(id:)` returns one row, so its own lists are bounded by that one row."""
    assert "tags" in selected_fields(tools["gadget"].document, "gadget")
    assert selected_fields(tools["gadget"].document, "gadget", "tags") == {"id", "label"}


@pytest.mark.parametrize(
    "path, field",
    [
        pytest.param(("gadgets", "nodes"), "tags", id="directly"),
        # through a single-valued object: `owner` is one, but it is one PER ROW
        pytest.param(("gadgets", "nodes", "owner"), "badges", id="through-a-single-object"),
    ],
)
def test_a_bare_list_of_objects_is_dropped_inside_a_repeated_result(tools, path, field):
    """Rows x their own lists is a product with no page and no bound — the cut the hand-written
    `examples/using-official-sdk/fireflies.py` makes too, selecting `sentences` only for a single
    transcript. The by-id tool still serves the field in full."""
    assert field not in selected_fields(tools["gadgets"].document, *path)


def test_a_connection_survives_inside_a_repeated_result(tools):
    """A connection is bounded by its own page and says so through `pageInfo`, so the size rule
    above does not apply to it."""
    assert "notes" in selected_fields(tools["gadgets"].document, "gadgets", "nodes")


@pytest.mark.parametrize(
    "tool_name, expected",
    [
        pytest.param("gadgets", ["after", "first"], id="relay"),
        pytest.param("notes", ["limit"], id="offset"),
        pytest.param("gadget", [], id="single-result-has-none"),
    ],
)
def test_a_many_row_tools_description_names_its_paging_arguments(tools, tool_name, expected):
    """Without one the server applies its own default page rather than returning everything. The
    arguments are in the input schema, but a vendor need not describe them, so the tool says it."""
    description = tools[tool_name].description
    for name in expected:
        assert f"`{name}`" in description
    if not expected:
        assert "paging" not in description


def test_a_connection_is_recognised_by_shape_not_by_type_name():
    """Carrying both `nodes` and `pageInfo` is the whole test, and the page type's name is read
    off the field — nothing requires a schema to spell it `PageInfo`."""
    tools = _derive(sdl=MCP_SDL.replace("PageInfo", "Cursorish"))
    assert selected_fields(tools["gadgets"].document, "gadgets") == {"nodes", "pageInfo"}
    assert "hasNextPage" in selected_fields(tools["gadgets"].document, "gadgets", "pageInfo")


def test_a_tool_is_dropped_when_a_required_argument_cannot_be_described():
    """`Opaque` has nothing describable in it, so there is no schema to offer for `bag` — and
    emitting the tool anyway would send a document missing a required argument."""
    sdl = MCP_SDL.replace("notes(ids: [ID!], limit: Int)", "notes(bag: Opaque!, limit: Int)")
    assert "notes" not in _derive(sdl=sdl + "\ninput Opaque { self: Opaque }\n")


@pytest.mark.parametrize(
    "field, extra",
    [
        pytest.param("node: Node", "interface Node { id: ID! }", id="interface"),
        pytest.param("thing: Thing", "union Thing = Note", id="union"),
    ],
)
def test_a_tool_is_dropped_when_its_type_needs_a_selection_the_generator_cannot_write(field, extra):
    """An interface or union needs a type condition per member. Nothing here writes one, so the
    field cannot be served — and serving it anyway means a document with no selection set."""
    sdl = MCP_SDL.replace("type Query {", "type Query {\n  " + field) + "\n" + extra + "\n"
    assert {"node", "thing"}.isdisjoint(_derive(sdl=sdl))


def test_an_argument_with_a_default_is_optional_in_the_document_too():
    """A NON_NULL argument that carries a default is optional to the caller, so its variable is
    declared nullable — GraphQL allows that at a defaulted argument, and `$x: T!` would validate
    and then refuse the call at execution."""
    sdl = MCP_SDL.replace(
        "notes(ids: [ID!], limit: Int)", 'notes(cursor: String! = "x", limit: Int)'
    )
    tool = _derive(sdl=sdl)["notes"]
    assert "required" not in tool.input_schema
    declared = parse(tool.document).definitions[0].variable_definitions
    assert {v.variable.name.value: v.type.kind for v in declared} == {
        "cursor": "named_type",  # not `non_null_type`
        "limit": "named_type",
    }
    result = engine.Engine(sdl).execute(tool.document, variables={"limit": 1})
    assert result.request_error is False


def test_an_unusable_connection_falls_back_to_the_plain_object_path():
    """A connection whose page type has nothing selectable is not usable as one, so it is treated
    as an ordinary object rather than emitting a `pageInfo` with no selection set."""
    sdl = MCP_SDL.replace(
        """type PageInfo {
  hasNextPage: Boolean!
  hasPreviousPage: Boolean!
  startCursor: String
  endCursor: String
}""",
        "type PageInfo { deeper: Deeper }\ntype Deeper { deepest: Deepest }\ntype Deepest { x: Int }",
    )
    tools = _derive(sdl=sdl)
    assert not validate(engine.Engine(sdl).schema, parse(tools["gadgets"].document))
    assert "pageInfo" not in selected_fields(tools["gadgets"].document, "gadgets")


def test_an_error_envelope_is_reported_rather_than_crashing():
    """`{"errors": […], "data": null}` is what an endpoint sends when the credential is wrong.
    Reading `__schema` off that null is a TypeError that says nothing about why."""
    envelope = {"data": None, "errors": [{"message": "Invalid API key"}]}
    with pytest.raises(ValueError, match="Invalid API key"):
        mcp_tools.derive_tools(envelope)


def test_a_derived_document_validates_against_the_schema(tools):
    schema = engine.Engine(MCP_SDL).schema
    for tool in tools.values():
        assert not validate(schema, parse(tool.document)), tool.name


def test_a_tool_carries_its_fields_description(tools):
    assert "Loose notes, newest first." in tools["notes"].description
