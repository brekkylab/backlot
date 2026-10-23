"""Engine-level tests for the vendor-agnostic GraphQL layer.

Everything here runs against a throwaway SDL that no vendor uses, so these assert the
*engine's* contract (selection, fragments, aliases, variables, directives, introspection,
error envelope, context plumbing) and never a vendor's schema. Per-source endpoint /
search tests belong to the Fireflies and Linear test files.
"""

from __future__ import annotations

import base64
import gzip
import json
import math
import random
import shutil
import struct
import subprocess
import zlib

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from graphql import GraphQLError, GraphQLID, parse, validate
from graphql.type.scalars import coerce_id
from starlette.testclient import TestClient

from backlot import auth, pagination, store
from backlot.graphql import body as request_body
from backlot.graphql import engine, mcp_tools, v8_json
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
    assert (r.request_error, r.raised) == (False, False)
    assert r.payload["data"] == {"boom": None}
    err = r.payload["errors"][0]
    assert err["message"] == "no widget factory today"
    assert err["path"] == ["boom"]


def test_partial_data_survives_alongside_an_error(gql):
    r = gql.execute('{ boom widget(id: "w1") { name } }')
    assert r.payload["data"] == {"boom": None, "widget": {"name": "Gateway"}}
    assert len(r.payload["errors"]) == 1
    assert r.raised is False
    # the body opens with "errors", as the real Fireflies and Linear APIs send it; a response
    # with no error still opens with "data"
    assert list(r.payload) == ["errors", "data"]
    clean = gql.execute('{ widget(id: "w1") { name } }')
    assert (list(clean.payload), clean.raised) == (["data"], False)


def _placed(_root, info):
    raise GraphQLError("refused where it was raised", path=info.path.as_list())


def _crashed(_root, _info):
    raise ValueError("cannot convert float NaN to integer")


@pytest.mark.parametrize(
    "query, variables, raised",
    [
        ("{ placed }", None, False),
        ("{ crashed }", None, True),
        ("{ boom crashed }", None, True),
        # graphql-core completing a value the schema refuses
        ("{ blank }", None, True),
        ("{ count }", None, True),
        ("{ many }", None, True),
        ("{ either { ... on Widget { id } } }", None, True),
        # graphql-core refusing, as it runs the field, an argument the request sent
        ('query W($id: ID = "w1") { widget(id: $id) { name } }', {"id": None}, False),
        (
            'query W($show: Boolean = true) { widget(id: "w1") { name @include(if: $show) } }',
            {"show": None},
            False,
        ),
    ],
)
def test_a_field_error_is_marked_raised_unless_it_refuses_the_request(query, variables, raised):
    gql = engine.Engine(
        SDL.replace(
            "boom: String",
            "boom: String\n  placed: String\n  crashed: String\n  blank: String!\n  count: Int\n"
            "  many: [String]\n  either: Either",
        )
        + "\nunion Either = Widget | Part\n",
        {
            "Query": {
                **RESOLVERS["Query"],
                "placed": _placed,
                "crashed": _crashed,
                "blank": lambda _root, _info: None,
                "count": lambda _root, _info: 2**31,
                "many": lambda _root, _info: 5,
                "either": lambda _root, _info: {"id": "w1"},
            }
        },
    )
    r = gql.execute(query, variables=variables)
    assert (r.request_error, "errors" in r.payload, r.raised) == (False, True, raised)


# --- operation selection --------------------------------------------------------


def test_operation_name_picks_the_operation_to_run(gql):
    q = 'query A { widget(id: "w1") { name } } query B { widget(id: "w2") { name } }'
    r = gql.execute(q, operation_name="B")
    assert r.payload["data"] == {"widget": {"name": "Worker"}}


@pytest.mark.parametrize("variables", [None, {"id": "w1"}], ids=["no variables", "variables"])
def test_ambiguous_operation_without_a_name_is_a_request_error(gql, variables):
    q = 'query A { widget(id: "w1") { name } } query B { widget(id: "w2") { name } }'
    r = gql.execute(q, variables=variables)
    assert r.request_error is True
    assert "data" not in r.payload


# --- context --------------------------------------------------------------------


def test_context_is_visible_to_resolvers(gql):
    r = gql.execute("{ caller }", context={"caller": "ava@acme.com"})
    assert r.payload["data"] == {"caller": "ava@acme.com"}


# --- request body (GraphQL over HTTP) -------------------------------------------


def test_execute_payload_honours_variables_and_operation_name(gql):
    r = gql.execute_payload(
        {
            "query": 'query A { widget(id: "w1") { name } } '
            "query B($id: ID!) { widget(id: $id) { name } }",
            "variables": {"id": "w2"},
            "operationName": "B",
        }
    )
    assert r.payload["data"] == {"widget": {"name": "Worker"}}


_WIDGETS = b'{"query": "{ widgets { id } }", "variables": {"x": %s}}'


@pytest.mark.parametrize(
    "body, served",
    [
        pytest.param(b"not json", False, id="not JSON"),
        pytest.param(_WIDGETS % b"NaN", False, id="NaN"),
        pytest.param(_WIDGETS % b"-Infinity", False, id="-Infinity"),
        pytest.param('{"query": "{ widgets { id } }"}'.encode("utf-16"), False, id="UTF-16"),
        pytest.param(b"\xef\xbb\xbf" * 2 + _WIDGETS % b"1", False, id="two byte-order marks"),
        pytest.param(_WIDGETS % (b"1" * 4301), True, id="4301 digits"),
        pytest.param(_WIDGETS % (b"[" * 100000 + b"]" * 100000), True, id="nested 100000 deep"),
        pytest.param(
            _WIDGETS % (b"[" * 100000 + "1\u0663".encode() + b"]" * 100000),
            False,
            id="a digit outside 0-9 nested 100000 deep",
        ),
        pytest.param(_WIDGETS % b'"\xff"', True, id="a byte that is not UTF-8"),
        pytest.param(b"\xef\xbb\xbf" + _WIDGETS % b"1", True, id="a byte-order mark"),
    ],
)
def test_a_body_is_read_as_node_reads_it(gql, body, served):
    """A body the reader refuses is :class:`~backlot.graphql.body.NotJson`, and one it reads runs,
    those ``json.loads`` refuses or raises on among them."""
    if served:
        payload = request_body.parse(request_body.decode(body))
        assert gql.execute_payload(payload).payload == {
            "data": {"widgets": [{"id": "w1"}, {"id": "w2"}]}
        }
    else:
        with pytest.raises(request_body.NotJson):
            request_body.parse(request_body.decode(body))


_ECHO = engine.Engine(
    "input F { id: ID, ids: [ID!] } type Query { echo(id: ID!): ID list(ids: [ID!]): [ID!] "
    "f(f: F): [ID] num(n: Float): Float }",
    {
        "Query": {
            "echo": lambda _r, _i, id: id,
            "list": lambda _r, _i, ids: ids,
            "f": lambda _r, _i, f: [f.get("id"), *f.get("ids", [])],
            "num": lambda _r, _i, n: n,
        }
    },
)
_ECHO_FIELDS = {
    "ID!": "echo(id: $x)",
    "[ID!]": "list(ids: $x)",
    "F": "f(f: $x)",
    "Float": "num(n: $x)",
}


@pytest.mark.parametrize(
    "type_, value, answer",
    [
        pytest.param("ID!", "1e21", {"data": {"echo": "1e+21"}}, id="1e21"),
        pytest.param("ID!", "1e300", {"data": {"echo": "1e+300"}}, id="1e300"),
        pytest.param(
            "ID!", "734946803407896717", {"data": {"echo": "734946803407896700"}}, id="past 2**53"
        ),
        pytest.param(
            "ID!",
            "12345678901234567890",
            {"data": {"echo": "12345678901234567000"}},
            id="20 digits",
        ),
        pytest.param(
            "ID!", "9007199254740993", {"data": {"echo": "9007199254740992"}}, id="2**53 + 1"
        ),
        pytest.param("ID!", "5", {"data": {"echo": "5"}}, id="5"),
        pytest.param("ID!", "-0", {"data": {"echo": "0"}}, id="-0"),
        pytest.param("ID!", "1.0", {"data": {"echo": "1"}}, id="1.0"),
        pytest.param("ID!", '"w1"', {"data": {"echo": "w1"}}, id="a string"),
        pytest.param(
            "ID!",
            "1.5",
            {
                "errors": [
                    {
                        "message": "Variable '$x' got invalid value 1.5; "
                        "ID cannot represent value: 1.5",
                        "locations": [{"line": 1, "column": 7}],
                    }
                ]
            },
            id="1.5",
        ),
        pytest.param(
            "[ID!]", '[1e21, 5, "w1"]', {"data": {"list": ["1e+21", "5", "w1"]}}, id="IDs in a list"
        ),
        pytest.param("[ID!]", "1e21", {"data": {"list": ["1e+21"]}}, id="one ID for a list"),
        pytest.param(
            "F",
            '{"id": 1e21, "ids": [1e300, 5]}',
            {"data": {"f": ["1e+21", "1e+300", "5"]}},
            id="IDs in an input object",
        ),
        pytest.param(
            "F",
            '{"nope": 1e21}',
            {
                "errors": [
                    {
                        "message": "Variable '$x' got invalid value {'nope': 1e+21}; "
                        "Field 'nope' is not defined by type 'F'.",
                        "locations": [{"line": 1, "column": 7}],
                    }
                ]
            },
            id="a field the input object does not have",
        ),
        pytest.param(
            "F",
            "5",
            {
                "errors": [
                    {
                        "message": "Variable '$x' got invalid value 5; "
                        "Expected type 'F' to be a mapping.",
                        "locations": [{"line": 1, "column": 7}],
                    }
                ]
            },
            id="a number for an input object",
        ),
        pytest.param("Float", "1e21", {"data": {"num": 1e21}}, id="a Float"),
    ],
)
def test_a_variable_is_read_as_graphql_js_reads_it(type_, value, answer):
    """Each value is read by :func:`backlot.graphql.engine._ids_as_graphql_js_reads_them`."""
    query = "query($x: %s) { %s }" % (type_, _ECHO_FIELDS[type_])
    body = '{"query": "%s", "variables": {"x": %s}}' % (query, value)
    assert _ECHO.execute_payload(request_body.parse(body)).payload == answer


def test_graphql_cores_id_scalar_is_left_as_graphql_core_has_it():
    assert GraphQLID.parse_value is coerce_id


def _fuzz_texts(seed: int, count: int):
    """``count`` JSON texts, constant tokens among their values, and as many again with one
    character of each deleted, doubled or replaced."""
    rng = random.Random(seed)
    pieces = ["0", "-0", "7", "-12", "3.5", "-0.25e3", "1E+2", "2e-3", "1" * 40, "1" * 4301]
    strings = ['""', '"a"', '"\\u00e9\\n"', '"\\ud83d\\ude00"', '"\\ud800"', '"x\\"y"', '"\u2028"']
    constants = ["NaN", "Infinity", "-Infinity"]
    ws = ["", " ", "\n", "\t", "\r\n"]

    def value(out: list, depth: int):
        roll = rng.random()
        if depth < 6 and roll < 0.2:
            out.append("[" + rng.choice(ws))
            for k in range(rng.randrange(4)):
                if k:
                    out.append(rng.choice(ws) + "," + rng.choice(ws))
                value(out, depth + 1)
            out.append(rng.choice(ws) + "]")
        elif depth < 6 and roll < 0.4:
            out.append("{" + rng.choice(ws))
            for k in range(rng.randrange(4)):
                if k:
                    out.append(",")
                out.append(
                    rng.choice(['"k"', '"a"', '"__proto__"', '"\\u006b"']) + rng.choice(ws) + ":"
                )
                value(out, depth + 1)
            out.append("}")
        elif roll < 0.45:
            out.append(rng.choice(constants))
        else:
            out.append(rng.choice(pieces + strings + ["true", "false", "null"]))

    for _ in range(count):
        out: list = [rng.choice(ws)]
        value(out, 0)
        out.append(rng.choice(ws))
        text = "".join(out)
        yield text
        i = rng.randrange(len(text))
        yield rng.choice(
            [
                text[:i] + text[i + 1 :],
                text[:i] + text[i] + text[i:],
                text[:i] + rng.choice('{}[],:"-.e0 x\u0663') + text[i + 1 :],
            ]
        )


def _refuse(token):
    raise json.JSONDecodeError(f"{token} is refused", token, 0)


def _outcome_of_json_loads(text: str):
    try:
        value = json.loads(
            text,
            parse_int=request_body._int,
            parse_float=request_body.Double,
            parse_constant=_refuse,
        )
        return repr(value), json.dumps(value)
    except json.JSONDecodeError:
        return "refused"


def test_the_iterative_reader_reads_what_json_loads_reads():
    """Thousands of texts, valid and broken: the reader :func:`backlot.graphql.body.parse` falls
    back to answers as ``json.loads`` does, value for value and type for type, and refuses a text
    holding a constant token."""
    checked = 0
    for text in _fuzz_texts(seed=20261018, count=3000):
        try:
            value = request_body._parse_iteratively(text)
            got = repr(value), json.dumps(value)
        except request_body.NotJson:
            got = "refused"
        assert got == _outcome_of_json_loads(text), text
        checked += 1
    assert checked == 6000


# Characters a mutation of `_v8_texts` puts in: each token, each escape letter, what may follow a
# number's digits, the letters of the literals, `é`, `一` and an emoji on either side of U+FFFF,
# U+2028, a byte-order mark, control characters, some of the whitespace Python's `str.isspace` knows
# and JSON does not, a digit outside 0 to 9, and two characters V8 has no use for.
_V8_ALPHABET = list('{}[],:"\\/-+.eE0123456789 \t\r\ntrufalsenbx#') + [
    "\u00e9",
    "\u4e00",
    "\U0001f600",
    "\u2028",
    "\ufeff",
    "\x00",
    "\x0b",
    "\x0c",
    "\x1f",
    "\x7f",
    "\u00a0",
    "\u0663",
]


def _v8_texts():
    """The texts the V8 tests read: :func:`_fuzz_texts`'s and three mutations of each of them,
    every prefix of four texts, a token V8 refuses between two strings of 0 to 12 of ``x``, of
    ``é`` or of an emoji two UTF-16 units long, a refusal after 0 to 3 repetitions of ``\\n``,
    ``\\r``, ``\\r\\n`` or ``\\n\\r``, the four sources V8 quotes back whole, ``NaN`` with a space
    before or after it and ``{}x``, and a text nested 100000 deep."""
    rng = random.Random(20261019)
    for text in _fuzz_texts(seed=20261019, count=1500):
        yield text
        for _ in range(3):
            mutated = text
            for _ in range(rng.randint(1, 3)):
                i = rng.randrange(len(mutated) + 1)
                c = rng.choice(_V8_ALPHABET)
                mutated = rng.choice(
                    [
                        mutated[:i] + mutated[i + 1 :],
                        mutated[:i] + c + mutated[i:],
                        mutated[:i] + c + mutated[i + 1 :],
                        mutated[:i],
                    ]
                )
            yield mutated
    for whole in (
        '{"query":"{ __typename }","variables":{"x":[1,-2.5e3,true,null,"\\u00e9\\n"]}}',
        '[{"a":"\U0001f600"},0,-0,0.5,1E+2,false]',
        '{"k":{"k":{"k":[[]]}},"\\"":"\\/"}',
        '"\\b\\f\\n\\r\\t\\u12aB\\uabcd\\uefAB\\uCDEF\\u4567\\u89c0"',
    ):
        for end in range(len(whole) + 1):
            yield whole[:end]
    for filler in ("x", "\u00e9", "\U0001f600"):
        for before in range(13):
            for after in range(13):
                yield '["' + filler * before + '",#,"' + filler * after + '"]'
    for newline in ("\n", "\r", "\r\n", "\n\r"):
        for lines in range(4):
            yield "[1," + (newline + '"\U0001f600\u2028",') * lines + " 2 3]"
    yield from ("[object Object]", "undefined", "Infinity", "NaN", " NaN", "NaN ", "{}x")
    yield "[" * 100000 + "1,}" + "]" * 100000


def test_v8_json_refuses_what_the_reader_refuses():
    """:func:`backlot.graphql.v8_json.error` refuses a text exactly when
    :func:`backlot.graphql.body.parse` does, so the two readers the Linear router runs one after
    the other never disagree on whether a body is JSON."""
    for text in _v8_texts():
        try:
            request_body.parse(text)
            read = True
        except request_body.NotJson:
            read = False
        assert (v8_json.error(text) is None) == read, text


# What `JSON.parse` throws for each text on stdin, `null` for one it reads.
_NODE_JSON_PARSE = """
const texts = JSON.parse(require("fs").readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(texts.map((text) => {
  try { JSON.parse(text); return null; } catch (e) { return e.message; }
})));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_v8_json_words_each_refusal_as_node_does():
    """Node's own ``JSON.parse`` is the oracle: :func:`backlot.graphql.v8_json.error` says what it
    says, word for word, to every one of :func:`_v8_texts`."""
    texts = list(_v8_texts())
    run = subprocess.run(
        ["node", "-e", _NODE_JSON_PARSE],
        input=json.dumps(texts),
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    node = json.loads(run.stdout)
    assert len(node) == len(texts)
    for text, expected in zip(texts, node, strict=True):
        assert v8_json.error(text) == expected, text


def _numbers():
    """Texts of numbers: doubles from random bit patterns, mantissas at each power of ten from 1e-30
    to 1e30, an integer of each length up to thirty digits, zeros and the two infinities."""
    rng = random.Random(20261019)
    for _ in range(5000):
        double = struct.unpack("<d", rng.getrandbits(64).to_bytes(8, "little"))[0]
        if math.isfinite(double):
            yield repr(double)
    for exponent in range(-30, 31):
        for mantissa in ("1", "1.5", "123456789", "9.999999999999999"):
            yield f"{mantissa}e{exponent}"
    for digits in range(1, 31):
        yield str(rng.randrange(10 ** (digits - 1), 10**digits))
    yield from ("0", "-0", "-0.0", "1e400", "-1e400")


# What Node prints for the number in each text on stdin.
_NODE_STRING = """
const texts = JSON.parse(require("fs").readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(texts.map((text) => String(JSON.parse(text)))));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_number_is_read_and_printed_as_node_does():
    """Node is the oracle: :func:`backlot.graphql.body.parse` reads each of :func:`_numbers` as a
    value whose ``repr`` is what ``String(JSON.parse(text))`` prints."""
    texts = list(_numbers())
    run = subprocess.run(
        ["node", "-e", _NODE_STRING],
        input=json.dumps(texts),
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    for text, expected in zip(texts, json.loads(run.stdout), strict=True):
        assert repr(request_body.parse(text)) == expected, text


def _surrogate_texts():
    """Texts holding U+D83D and U+DE00, each raw or as its escape, in either order: in a string,
    beside each alone in an array, as a key, as a key the escaped pair follows as a key too, as a
    value, and in an array and an object inside an array and an object."""
    for high in (chr(0xD83D), "\\ud83d"):
        for low in (chr(0xDE00), "\\ude00"):
            for pair in (high + low, low + high):
                yield f'"{pair}"'
                yield f'["a{pair}b", "{high}", "{low}"]'
                yield f'{{"{pair}": 1}}'
                yield f'{{"{pair}": 1, "x": 2, "\\ud83d\\ude00": [3, "{pair}"]}}'
                yield f'{{"k": "{pair}"}}'
                yield f'[["{pair}"], {{"{pair}": "{pair}"}}]'
                yield f'{{"a": {{"{pair}": [1]}}}}'


# What Node gives back for each text on stdin: what JSON.parse reads, as JSON.stringify writes it.
_NODE_STRINGIFY = """
const texts = JSON.parse(require("fs").readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(texts.map((text) => JSON.stringify(JSON.parse(text)))));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_surrogate_pair_is_read_as_node_reads_it():
    """What :func:`backlot.graphql.body.parse` makes of each of :func:`_surrogate_texts`, written
    by :func:`backlot.graphql.body.node_json`, is what Node makes of it."""
    texts = list(_surrogate_texts())
    run = subprocess.run(
        ["node", "-e", _NODE_STRINGIFY],
        input=json.dumps(texts),
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    for text, expected in zip(texts, json.loads(run.stdout), strict=True):
        assert request_body.node_json(request_body.parse(text)).decode() == expected, text


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), request_body.Double("-inf")],
    ids=["nan", "inf", "a Double holding -inf"],
)
def test_node_json_refuses_a_float_that_is_not_finite(value):
    with pytest.raises(ValueError):
        request_body.node_json({"x": value})


def _compressed():
    """Whole gzip and deflate streams, each also cut short, with a bit flipped, and followed by
    bytes that start another member or none, and the bytes of no stream, as (gzip?, bytes)."""
    rng = random.Random(20261019)
    tails = (b"\x00", b"\x00x", b"x", b"xx", b"\x1f", b"\x1f\x8b")
    tails += (gzip.compress(b"t"), zlib.compress(b"t"))
    for _ in range(100):
        payload = rng.randbytes(rng.randrange(300))
        for gz in (True, False):
            whole = gzip.compress(payload) if gz else zlib.compress(payload)
            flipped = bytearray(whole)
            flipped[rng.randrange(len(whole))] ^= 1 << rng.randrange(8)
            yield from ((gz, whole), (gz, whole[: rng.randrange(len(whole))]), (gz, bytes(flipped)))
            yield from ((gz, whole + tail) for tail in tails)
    yield from ((True, b""), (False, b""), (True, b"x"), (False, zlib.compress(b"t")[2:-4]))


# What Node's zlib stream yields from each case on stdin, or the error it throws.
_NODE_INFLATE = """
const zlib = require("zlib");
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
(async () => {
  const out = [];
  for (const [gzip, base64] of cases) {
    out.push(await new Promise((resolve) => {
      const stream = gzip ? zlib.createGunzip() : zlib.createInflate();
      const chunks = [];
      stream.on("data", (chunk) => chunks.push(chunk));
      stream.on("end", () => resolve(Buffer.concat(chunks).toString("base64")));
      stream.on("error", (error) => resolve({ error: error.message }));
      stream.end(Buffer.from(base64, "base64"));
    }));
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_compressed_body_is_inflated_as_node_inflates_it():
    """Node's zlib streams are the oracle: for each of :func:`_compressed`,
    :func:`backlot.graphql.body._inflate` yields the same bytes or fails with the same message."""
    cases = list(_compressed())
    run = subprocess.run(
        ["node", "-e", _NODE_INFLATE],
        input=json.dumps([[gz, base64.b64encode(raw).decode()] for gz, raw in cases]),
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    for (gz, raw), expected in zip(cases, json.loads(run.stdout), strict=True):
        try:
            got = base64.b64encode(request_body._inflate(raw, gz)).decode()
        except request_body.Unread as unread:
            got = {"error": unread.message}
        assert got == expected, (gz, raw)


# Bodies with no byte-order mark, as hex, and the order iconv-lite 0.7.2 reads each in
# (`backlot.graphql.body._byte_order`): a row for each test its heuristic makes of a unit, where the
# two orders read the body differently.
_BYTE_ORDERS = [
    pytest.param(2, "0111", "le"),
    pytest.param(2, "ff4e000041ff41114eff", "le"),
    pytest.param(2, "0000101100411041fe01", "be"),
    pytest.param(2, "10fe0001014e", "be"),
    pytest.param(4, "00fe414e0000ff001100fe11410001fe01ff0100ff11ff01", "be"),
    pytest.param(4, "fffe1041014e11410000004111fe010011ff1041", "be"),
    pytest.param(4, "00fe014e104101fffe000100410000fe", "le"),
    pytest.param(4, "ffff10000041ffff0011fe10", "le"),
    pytest.param(4, "114efe4e101001fffe101110ff41ff001110011041ff1000", "le"),
    pytest.param(4, "0000010141014e4e11000000414e4e01fe110101", "le"),
    pytest.param(4, "0001ff10000100004e411141ffff41414141114e014e1001", "le"),
    pytest.param(4, "001000ff000000000101fefe104100014e00ff41", "be"),
    pytest.param(4, "41fe4efe000101fffe41ff11fe104e1100fffe00", "be"),
    pytest.param(4, "001000114141fe4e004e41004e4101ff104e0141", "be"),
    pytest.param(4, "0010414efe4eff1011ff014e11004e4e", "be"),
    pytest.param(4, "000000004100000000000041", "le"),
]


@pytest.mark.parametrize("width, hex_bytes, order", _BYTE_ORDERS)
def test_a_body_with_no_byte_order_mark_is_read_in_the_order_iconv_lite_picks(
    width, hex_bytes, order
):
    assert request_body._byte_order(bytes.fromhex(hex_bytes), width) == order


def test_execute_payload_requires_a_query_string(gql):
    r = gql.execute_payload({"variables": {}})
    assert r.request_error is True
    assert r.payload["errors"][0]["message"] == "Must provide query string."


def test_execute_payload_rejects_non_object_variables(gql):
    r = gql.execute_payload({"query": "{ widgets { id } }", "variables": "nope"})
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
# A throwaway vendor ("acme") wired with the parts every GraphQL source here shares: an APIRouter
# with a prefix, the API-key auth resolver, and the caller's visible_ids injected into the resolver
# context so the resolver reaches store.py's existing ACL path with no per-source ACL code. Proves
# the seams line up; the vendor schemas themselves ship with their issues.

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
        try:
            payload = request_body.parse(request_body.decode(await request.body()))
        except request_body.NotJson:
            result = engine.not_json()
        else:
            result = ACME.execute_payload(payload, context=context)
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
  gadgets(first: Int, after: String, last: Int, before: String, filter: GadgetFilter, order: Order): GadgetConnection!
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
  checksum(algorithm: String!): String
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
        ("checksum", "a required argument is unfillable on a leaf field too"),
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
    assert {v.variable.name.value for v in declared} == {
        "first",
        "after",
        "last",
        "before",
        "filter",
        "order",
    }


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


@pytest.mark.parametrize(
    "tool_name, arguments, expected",
    [
        # nothing about paging said, so bound it — this is the call an agent actually makes
        pytest.param("gadgets", {}, {"first": 10}, id="relay-unpaged"),
        pytest.param("notes", {}, {"limit": 10}, id="offset-unpaged"),
        pytest.param(
            "gadgets",
            {"filter": {"name": {"eq": "x"}}},
            {"filter": ..., "first": 10},
            id="filtered",
        ),
        # the caller's own size wins, whichever direction it pages in
        pytest.param("gadgets", {"first": 50}, {"first": 50}, id="own-size"),
        pytest.param("gadgets", {"last": 5}, {"last": 5}, id="backward-size"),
        # a forward cursor still gets a size, so page 2 is bounded like page 1
        pytest.param("gadgets", {"after": "c"}, {"after": "c", "first": 10}, id="next-page"),
        # `before` alone means "the page ending here"; adding `first` would override it
        pytest.param("gadgets", {"before": "c"}, {"before": "c"}, id="backward-cursor"),
        # a single-result tool has no page to bound
        pytest.param("gadget", {"id": "g1"}, {"id": "g1"}, id="single-result"),
    ],
)
def test_a_page_size_is_supplied_only_when_the_caller_named_none(
    tools, tool_name, arguments, expected
):
    """An unpaged list call otherwise returns the server's default page, which the measured agent
    never asks to narrow — it filters instead. The default cannot be a variable default in the
    document, because that would also apply to a `last`-only call and Relay rejects `first` and
    `last` together."""
    got = mcp_tools.with_page_default(tools[tool_name], arguments)
    assert set(got) == set(expected)
    assert {k: v for k, v in got.items() if expected[k] is not ...} == {
        k: v for k, v in expected.items() if v is not ...
    }


def test_the_page_default_is_stated_in_the_description(tools):
    assert "10" in tools["gadgets"].description
    assert "10" not in tools["gadget"].description


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


_PAGE_INFO = """type PageInfo {
  hasNextPage: Boolean!
  hasPreviousPage: Boolean!
  startCursor: String
  endCursor: String
}"""
_GADGET_CONNECTION = "type GadgetConnection { nodes: [Gadget!]!, pageInfo: PageInfo! }"


@pytest.mark.parametrize(
    "old, new",
    [
        pytest.param(
            _PAGE_INFO,
            "type PageInfo { deeper: Deeper }\ntype Deeper { deepest: Deepest }\ntype Deepest { x: Int }",
            id="the-page-type-selects-nothing",
        ),
        pytest.param(
            _GADGET_CONNECTION,
            "type GadgetConnection { nodes: [String!]!, pageInfo: PageInfo! }",
            id="nodes-is-a-scalar",
        ),
        pytest.param(
            _GADGET_CONNECTION,
            "type GadgetConnection { nodes: [Gadget!]!, pageInfo: String }",
            id="page-info-is-a-scalar",
        ),
    ],
)
def test_a_connection_shape_that_cannot_be_served_falls_back_to_the_plain_path(old, new):
    """Carrying `nodes` and `pageInfo` by name is not enough to be served as a connection: the
    page type may select nothing, and either name may point at a scalar, which holds no selection
    at all. Each of those is an ordinary object field instead — the tool loses its expansion, and
    the derivation keeps every tool it had."""
    sdl = MCP_SDL.replace(old, new)
    tools = _derive(sdl=sdl)
    assert set(tools) == {"gadget", "gadgets", "notes"}
    assert not validate(engine.Engine(sdl).schema, parse(tools["gadgets"].document))


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
