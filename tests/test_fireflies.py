"""Fireflies' meeting-transcript GraphQL API at /fireflies/graphql.

One file per router, so a source's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sqlite3
import time
import tracemalloc

import pytest
from graphql import build_client_schema, parse, validate

from backlot import store
from backlot.graphql import mcp_tools
from tests._helpers import (
    SWEPT_BYTES,
    Chunked,
    WithTheKey,
    client_for,
    corpus_client,
    gql,
    http_row,
    padded_query,
    sent_as,
    served_id,
    tiny_corpus,
)

# --- fireflies: POST /fireflies/graphql -----------------------------------------
# Fireflies has no SDK and no LlamaIndex reader; the vendor's own quickstart is a raw HTTP POST,
# so this IS the client story rather than a fallback for one.


def ff_gql(client, query, headers, **variables):
    body = {"query": query}
    if variables:
        body["variables"] = variables
    return client.post("/fireflies/graphql", json=body, headers=headers)


# The two 500s `backlot.routers.fireflies._auth_failed` answers, as real sends them but for the
# newline real ends each with, which `_auth_failed` leaves off.
_NO_PREFIX = (
    b'{"errors":[{"friendly":true,"code":"auth_failed","message":"Authorization header is missing'
    b" the 'Bearer' prefix. Please use the format: 'Authorization: Bearer <api_key>'. If this"
    b' persists, please contact Fireflies support.","extensions":{"code":"auth_failed"}}]}'
)
_BAD_KEY = (
    b'{"errors":[{"friendly":true,"code":"auth_failed","message":"An error occurred while'
    b" authenticating your request. Please recheck your API key and try again. If this persists,"
    b' please contact Fireflies support.","extensions":{"code":"auth_failed"}}]}'
)
# Values of the header, None for none and `{t}` for a key that resolves, with the 500 Fireflies
# refused each with or None where it served the query.
_FIREFLIES_SPELLINGS = [
    pytest.param("Bearer {t}", None, id="Bearer"),
    pytest.param("bEaReR {t}", None, id="bEaReR"),
    pytest.param("Bearer  {t}", None, id="two spaces"),
    pytest.param(" Bearer {t}", None, id="a space before"),
    pytest.param("Bearer \xa0{t}\xa0", None, id="U+00A0 around the key"),
    pytest.param(None, _BAD_KEY, id="no header"),
    pytest.param("", _BAD_KEY, id="an empty header"),
    pytest.param("Bearer nope", _BAD_KEY, id="Bearer nope"),
    pytest.param("Bearer {t} extra", _BAD_KEY, id="a word after"),
    pytest.param("Bearer {t}x", _BAD_KEY, id="a key that does not resolve"),
    pytest.param("Bearer {t}\x85", _BAD_KEY, id="U+0085 after"),
    pytest.param("Bearer \xa0", _BAD_KEY, id="U+00A0 alone"),
    pytest.param("Bearer \xa0\xa0", _BAD_KEY, id="U+00A0 twice"),
    pytest.param("Bearer\t{t}", _NO_PREFIX, id="a tab"),
    pytest.param("Bearer", _NO_PREFIX, id="Bearer alone"),
    pytest.param("Bearer{t}", _NO_PREFIX, id="no space"),
    pytest.param("Bearer,{t}", _NO_PREFIX, id="a comma"),
    pytest.param("\xa0Bearer {t}", _NO_PREFIX, id="U+00A0 before"),
    pytest.param("{t}", _NO_PREFIX, id="bare"),
    pytest.param("Token {t}", _NO_PREFIX, id="Token"),
]


# `SWEPT_BYTES` in three places of the header: the bytes that served the query, and the 500 every
# other byte drew.
_FIREFLIES_BYTES = {
    "Bearer{x}{t}": ({0x20}, _NO_PREFIX),
    "Bearer {x}{t}": ({0x09, 0x20, 0xA0}, _BAD_KEY),
    "Bearer {t}{x}": ({0x09, 0x20, 0xA0}, _BAD_KEY),
}


def _fireflies_answer(client, tokens_yaml, value: str | None) -> bytes | None:
    """The 500 `Authorization: value` draws, `{t}` the admin key and None for no header, or None
    where it serves the query."""
    headers = {"Content-Type": "application/json"}
    if value is not None:
        headers["Authorization"] = value.replace("{t}", tokens_yaml["admin_token"])
    r = sent_as(
        client, "POST", "/fireflies/graphql", headers, content=b'{"query":"{ __typename }"}'
    )
    if r.status_code == 500:
        return r.content
    assert (r.status_code, r.json()) == (200, {"data": {"__typename": "Query"}})
    return None


@pytest.mark.parametrize("value, refusal", _FIREFLIES_SPELLINGS)
def test_fireflies_reads_a_credential_as_fireflies_does(client, tokens_yaml, value, refusal):
    assert _fireflies_answer(client, tokens_yaml, value) == refusal


def test_fireflies_reads_a_swept_byte_as_fireflies_does(client, tokens_yaml):
    """Each of `SWEPT_BYTES` in each place `_FIREFLIES_BYTES` names draws the answer Fireflies gave
    it."""
    expected = {
        shape.replace("{x}", chr(byte)): None if byte in served else refusal
        for shape, (served, refusal) in _FIREFLIES_BYTES.items()
        for byte in SWEPT_BYTES
    }
    assert {value: _fireflies_answer(client, tokens_yaml, value) for value in expected} == expected


# --- the body and the credential, in the order Fireflies reads them -------------------------------
def _apollo(
    message: bytes, code: bytes = b"BAD_REQUEST", locations: bytes = b"", status: int = 400
) -> WithTheKey:
    """Apollo Server's refusal as Fireflies writes it around ``message``, already JSON-escaped."""
    return WithTheKey(
        b'{"errors":[{"friendly":false,"message":"%s",%s"code":"%s","extensions":{}}]}'
        % (message, locations, code),
        status,
    )


_NO_KEYS = _apollo(b"POST body missing, invalid Content-Type, or JSON object has no keys.")


def _twelve_deep(fields: int, name: str = "l") -> str:
    """A selection of ``fields`` fields twelve deep, ``l0`` on with ``name`` as ``l``, in fields
    Fireflies does not have."""
    selection = " ".join(f"{name}{n}" for n in range(fields))
    return "{ a{b{c{d{e{f{g{h{i{j{k{ %s }}}}}}}}}}} }" % selection


def _query(query: str) -> bytes:
    return json.dumps({"query": query}, separators=(",", ":")).encode()


def _too_deep(query: str, fields: int, name: str = "l", operation: str = "") -> list[bytes]:
    """graphql-depth-limit's error at each field :func:`_twelve_deep` put in ``query``."""
    return [
        b'{"friendly":false,"message":"\'%s\' exceeds maximum operation depth of 10","locations":'
        b'[{"line":1,"column":%d}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}'
        % (operation.encode(), query.index(f" {name}{n} ") + 2)
        for n in range(fields)
    ]


def _validation(*errors: bytes) -> bytes:
    return b'{"errors":[%s]}' % b",".join(errors)


_INTERNAL = (
    b'{"errors":[{"friendly":false,"message":"Internal server error","code":"INTERNAL_SERVER_ERROR",'
    b'"extensions":{}}]}'
)


def _no_field_a(column: int) -> bytes:
    return (
        b'{"friendly":false,"message":"Cannot query field \\"a\\" on type \\"Query\\".","locations":'
        b'[{"line":1,"column":%d}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}' % column
    )


_TOO_MANY_ERRORS = (
    b'{"friendly":false,"message":"Too many validation errors, error limit reached. Validation'
    b' aborted.","code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}'
)
_TWO_FIFTIES = "query A %s query B %s" % (_twelve_deep(50), _twelve_deep(50, "m"))


_BATCHING = _apollo(b"Operation batching disabled.")
_EMPTY = (
    b'{"statusCode":400,"status":false,"code":"http","message":"Body cannot be empty when'
    b' content-type is set to \'application/json\'","errorMessage":"Body cannot be empty when'
    b" content-type is set to 'application/json'\"}"
)
_NOT_JSON = (
    b'{"statusCode":400,"status":false,"code":"http","message":"Body is not valid JSON but'
    b' content-type is set to \'application/json\'","errorMessage":"Body is not valid JSON but'
    b" content-type is set to 'application/json'\"}"
)
_TOO_LARGE = (
    b'{"statusCode":413,"status":false,"code":"http","message":"Request body is too large",'
    b'"errorMessage":"Request body is too large"}'
)
_SIZE_MISMATCH = (
    b'{"statusCode":400,"status":false,"code":"http","message":"Request body size did not match'
    b' Content-Length","errorMessage":"Request body size did not match Content-Length"}'
)
_TYPENAME = b'{"query":"{ __typename }"}'


_FIREFLIES_READS = [
    pytest.param((), padded_query(2**20), None, id="the limit"),
    pytest.param((), padded_query(2**20 + 1), _TOO_LARGE, id="past the limit"),
    pytest.param((), b"{" * (2**20 + 1), _TOO_LARGE, id="past the limit, not JSON"),
    pytest.param((("Content-Encoding", "gzip"),), gzip.compress(_TYPENAME), _NOT_JSON, id="gzip"),
    pytest.param((("Content-Encoding", "gzip"),), _TYPENAME, None, id="gzip over a plain body"),
    pytest.param((("Content-Encoding", "bogus"),), _TYPENAME, None, id="bogus"),
    pytest.param(
        (("Content-Type", "application/json; charset=latin1"),), _TYPENAME, None, id="latin1"
    ),
    pytest.param(
        (("Content-Type", "application/json; charset=utf-nope"),), _TYPENAME, None, id="utf-nope"
    ),
    pytest.param(
        (("Content-Type", "application/json; charset=utf-16"),),
        _TYPENAME.decode().encode("utf-16"),
        _NOT_JSON,
        id="utf-16",
    ),
]
# The headers a body was sent with beyond `Content-Type: application/json`, the body, and Fireflies'
# answer to it with the key: `_EMPTY`, `_NOT_JSON` or `_TOO_LARGE`, its HTTP layer's refusals
# `backlot.routers.fireflies._body_refusal` sends at the status their `statusCode` names, or None
# where it served the query. A `WithTheKey` row is a body that layer passes, which draws one of
# `_auth_failed`'s 500s without the key; its bytes are api.fireflies.ai's without the newline real
# ends them with.
_FIREFLIES_BODIES = [
    pytest.param(b'{"query":"{ __typename }"}', None, id="a query"),
    pytest.param(b"{", _NOT_JSON, id="{"),
    pytest.param(b"", _EMPTY, id="empty"),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"x":NaN}}', _NOT_JSON, id="NaN at the end"
    ),
    pytest.param(b"   ", _NOT_JSON, id="whitespace only"),
    pytest.param(b"\n", _NOT_JSON, id="newline only"),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"__proto__":{"a":1}}}',
        _NOT_JSON,
        id="__proto__ key",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"constructor":{"prototype":{"a":1}}}}',
        _NOT_JSON,
        id="constructor.prototype",
    ),
    pytest.param(
        b'\xff\xfe{\x00"\x00q\x00u\x00e\x00r\x00y\x00"\x00:\x00"\x00{\x00 \x00_\x00_\x00t\x00y\x00p\x00e\x00n\x00a\x00m\x00e\x00 \x00}\x00"\x00}\x00',
        _NOT_JSON,
        id="utf-16 body",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"x":"\xff"}}', None, id="invalid utf-8 in a string"
    ),
    pytest.param(b'\xef\xbb\xbf{"query":"{ __typename }"}', None, id="utf-8 BOM"),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"constructor":null}}', None, id="constructor null"
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"constructor":"x"}}', None, id="constructor string"
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"constructor":[1]}}', None, id="constructor array"
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"constructor":{"a":1}}}',
        None,
        id="constructor object without prototype",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"constructor":{"prototype":null}}}',
        _NOT_JSON,
        id="constructor.prototype null",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"\\u005f_proto__":1}}',
        _NOT_JSON,
        id="__proto__ escaped",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"x":[{"__proto__":1}]}}',
        _NOT_JSON,
        id="__proto__ in an array",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"x":"\\"__proto__\\":1"}}',
        None,
        id="__proto__ in a string value",
    ),
    pytest.param(b'{"__proto__":1,"query":"{ __typename }"}', _NOT_JSON, id="__proto__ top-level"),
    pytest.param(
        b'{"constructor":{"prototype":1},"query":"{ __typename }"}',
        _NOT_JSON,
        id="constructor top-level with prototype",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"prototype":1}}',
        None,
        id="prototype without constructor",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"constructor":{"prototype":1},"constructor":1}}',
        None,
        id="constructor twice, last without prototype",
    ),
    pytest.param(b'\xef\xbb\xbf\xef\xbb\xbf{"query":"{ __typename }"}', _NOT_JSON, id="BOM twice"),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"a":"\xff","x":NaN}}',
        _NOT_JSON,
        id="utf-8 replacement then NaN",
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": ' + b"1" * 4300 + b"}}",
        None,
        id="4300 digits",
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": ' + b"1" * 4301 + b"}}",
        None,
        id="4301 digits",
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": ' + b"1" * 5000 + b"}}",
        None,
        id="5000 digits",
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": -' + b"1" * 4301 + b"}}",
        None,
        id="-4301 digits",
    ),
    pytest.param(b'{"query": "{ __typename }", "variables": {"x": 1e400}}', None, id="1e400"),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": ' + b"[" * 1001 + b"]" * 1001 + b"}}",
        None,
        id="nested 1001",
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": ' + b"[" * 100000 + b"]" * 100000 + b"}}",
        None,
        id="nested 100000",
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": ' + b"[" * 300000 + b"]" * 300000 + b"}}",
        None,
        id="nested 300000",
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": '
        + b'{"a":' * 100000
        + b"1"
        + b"}" * 100000
        + b"}}",
        None,
        id="objects nested 100000",
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": '
        + b"[" * 100000
        + b"NaN"
        + b"]" * 100000
        + b"}}",
        _NOT_JSON,
        id="nested 100000 then NaN",
    ),
    pytest.param(b"\xef\xbb\xbf", _NOT_JSON, id="BOM only"),
    pytest.param(b'{"x":Infinity,"query":"{ __typename }"}', _NOT_JSON, id="Infinity"),
    pytest.param(b'{"x":-Infinity,"query":"{ __typename }"}', _NOT_JSON, id="-Infinity"),
    pytest.param(b"[]", _BATCHING, id="an array"),
    pytest.param(b"null", _NO_KEYS, id="null"),
    pytest.param(b'"x"', _NO_KEYS, id="a string"),
    pytest.param(b"5", _NO_KEYS, id="a number"),
    pytest.param(b"true", _NO_KEYS, id="true"),
    pytest.param(b"{}", _NO_KEYS, id="an empty object"),
    pytest.param(b'[{"query":"{ __typename }"}]', _BATCHING, id="a batch of one query"),
    pytest.param(b'{"query":5}', _apollo(b"GraphQL queries must be strings."), id="a query of 5"),
    pytest.param(
        b'{"query":"{ __typename }","variables":"x"}',
        _apollo(
            b"`variables` in a POST body should be provided as an object, not a recursively "
            b"JSON-encoded string."
        ),
        id="a string of variables",
    ),
    pytest.param(
        b'{"query":"{ __typename }","extensions":5}',
        _apollo(b"`extensions` in a POST body must be an object if provided."),
        id="extensions of 5",
    ),
    pytest.param(
        b'{"query":"{ __typename }","operationName":5}',
        _apollo(b"`operationName` in a POST body must be a string if provided."),
        id="an operationName of 5",
    ),
    pytest.param(
        b'{"query":"{ __typename }","extensions":{"persistedQuery":{"version":1,"sha256Hash":"'
        + b"0" * 64
        + b'"}}}',
        _apollo(b"provided sha does not match query", b"INTERNAL_SERVER_ERROR"),
        id="a persisted query's hash that does not match",
    ),
    pytest.param(
        b'{"extensions":{"persistedQuery":{"version":1,"sha256Hash":"' + b"1" * 64 + b'"}}}',
        _apollo(b"PersistedQueryNotFound", b"PERSISTED_QUERY_NOT_FOUND", status=200),
        id="a persisted query's hash alone",
    ),
    pytest.param(
        b'{"query":null}',
        _apollo(
            b"GraphQL operations must contain a non-empty `query` or a `persistedQuery` extension."
        ),
        id="a query of null",
    ),
    pytest.param(
        b'{"query":"","extensions":{"persistedQuery":{"version":1,"sha256Hash":"'
        + hashlib.sha256(b"").hexdigest().encode()
        + b'"}}}',
        _apollo(
            b"Syntax Error: Unexpected <EOF>.",
            b"GRAPHQL_PARSE_FAILED",
            b'"locations":[{"line":1,"column":1}],',
        ),
        id="a persisted query's hash matching an empty query",
    ),
    pytest.param(
        b'{"query":"{"}',
        _apollo(
            b"Syntax Error: Expected Name, found <EOF>.",
            b"GRAPHQL_PARSE_FAILED",
            b'"locations":[{"line":1,"column":2}],',
        ),
        id="a document that does not parse",
    ),
    pytest.param(
        b'{"query":"{ nosuch }"}',
        _apollo(
            b'Cannot query field \\"nosuch\\" on type \\"Query\\".',
            b"GRAPHQL_VALIDATION_FAILED",
            b'"locations":[{"line":1,"column":3}],',
        ),
        id="a document that does not validate",
    ),
    pytest.param(
        b'{"query":"query A { __typename } query B { __typename }"}',
        _apollo(
            b"Must provide operation name if query contains multiple operations.",
            b"OPERATION_RESOLUTION_FAILURE",
        ),
        id="two operations and no operationName",
    ),
    pytest.param(
        b'{"query":"query($b: Boolean!) { __typename @include(if: $b) }"}',
        _apollo(
            b'Variable \\"$b\\" of required type \\"Boolean!\\" was not provided.',
            b"BAD_USER_INPUT",
            b'"locations":[{"line":1,"column":7}],',
        ),
        id="a variable not provided",
    ),
    pytest.param(
        b'{"query":"query($v: [String!]) { transcripts(participants: $v) { id } }",'
        b'"variables":{"v":[%s]}}' % b",".join([b"true"] * 60),
        WithTheKey(
            _validation(
                *(
                    b'{"friendly":false,"message":"Variable \\"$v\\" got invalid value true at '
                    b'\\"v[%d]\\"; String cannot represent a non string value: true","locations":'
                    b'[{"line":1,"column":7}],"code":"BAD_USER_INPUT","extensions":{}}' % n
                    for n in range(50)
                ),
                b'{"friendly":false,"message":"Too many errors processing variables, error limit '
                b'reached. Execution aborted.","code":"INTERNAL_SERVER_ERROR","extensions":{}}',
            )
        ),
        id="sixty variables refused",
    ),
    pytest.param(
        b'{"query":"{ __typename }","operationName":"Z"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Unknown operation named \\"Z\\".","code":"OPERATION_RESOLUTION_FAILURE","extensions":{}}]}'
        ),
        id="an operationName no operation has",
    ),
    pytest.param(
        b'{"query":"query A { __typename }","operationName":""}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Unknown operation named \\"\\".","code":"OPERATION_RESOLUTION_FAILURE","extensions":{}}]}'
        ),
        id="an empty operationName",
    ),
    pytest.param(
        b'{"query":{"kind":"Document"}}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"GraphQL queries must be strings. It looks like you\'re sending the internal graphql-js representation of a parsed query in your request instead of a request in the GraphQL query language. You can convert an AST to a string using the `print` function from `graphql`, or use a client like `apollo-client` which converts the internal representation to a string for you.","code":"BAD_REQUEST","extensions":{}}]}'
        ),
        id="a parsed document",
    ),
    pytest.param(
        b'{"query":false}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"GraphQL operations must contain a non-empty `query` or a `persistedQuery` extension.","code":"BAD_REQUEST","extensions":{}}]}'
        ),
        id="a query of false",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":"null"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"`variables` in a POST body should be provided as an object, not a recursively JSON-encoded string.","code":"BAD_REQUEST","extensions":{}}]}'
        ),
        id="variables of 'null'",
    ),
    pytest.param(
        b'{"query":"query($b: Boolean!) { __typename @include(if: $b) }","variables":"{\\"b\\":true}"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"`variables` in a POST body should be provided as an object, not a recursively JSON-encoded string.","code":"BAD_REQUEST","extensions":{}}]}'
        ),
        id="variables of a string its operation takes",
    ),
    pytest.param(
        b'{"query":"mutation { __typename }"}',
        WithTheKey(b'{"data":{"__typename":"Mutation"}}', 200),
        id="a mutation of __typename",
    ),
    pytest.param(
        b'{"query":"subscription S { __typename }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Schema is not configured to execute subscription operation.","locations":[{"line":1,"column":1}],"code":"INTERNAL_SERVER_ERROR","extensions":{}}],"data":null}',
            200,
        ),
        id="a subscription of __typename",
    ),
    pytest.param(
        b'{"query":"query($x: String = \\"a\\") { transcript(id: $x) { id } }","variables":{"x":null}}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Argument \\"id\\" of non-null type \\"String!\\" must not be null.","locations":[{"line":1,"column":42}],"path":["transcript"],"code":"INTERNAL_SERVER_ERROR","extensions":{}}],"data":{"transcript":null}}',
            200,
        ),
        id="null for a non-null argument",
    ),
    pytest.param(
        b'{"query":"query($v: Boolean = true) { __typename @include(if: $v) }","variables":{"v":null}}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Argument \\"if\\" of non-null type \\"Boolean!\\" must not be null.","locations":[{"line":1,"column":53}],"code":"INTERNAL_SERVER_ERROR","extensions":{}}],"data":null}',
            200,
        ),
        id="null for a directive's argument at the root",
    ),
    pytest.param(
        b'{"query":"query($x: String = \\"a\\") # a\xc2\x85b\\n{ transcript(id: $x) { id } }","variables":{"x":null}}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Argument \\"id\\" of non-null type \\"String!\\" must not be null.","locations":[{"line":2,"column":18}],"path":["transcript"],"code":"INTERNAL_SERVER_ERROR","extensions":{}}],"data":{"transcript":null}}',
            200,
        ),
        id="null for a non-null argument after U+0085 in a comment",
    ),
    pytest.param(
        b'{"query":"{ ...F }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Internal server error","code":"INTERNAL_SERVER_ERROR","extensions":{}}]}',
            500,
        ),
        id="a fragment not defined",
    ),
    pytest.param(
        b'{"query":"query __proto__ { __typename }"}',
        WithTheKey(_INTERNAL, 500),
        id="an operation named __proto__",
    ),
    pytest.param(
        b'{"query":"query A { __typename } query __proto__ { __typename }","operationName":"A"}',
        WithTheKey(_INTERNAL, 500),
        id="an operation named __proto__ beside the one chosen",
    ),
    pytest.param(
        b'{"query":"query constructor { __typename }"}', None, id="an operation named constructor"
    ),
    pytest.param(
        b'{"query":"query __defineGetter__ { __typename }"}',
        None,
        id="an operation named __defineGetter__",
    ),
    pytest.param(
        b'{"query":"query A { ...__proto__ } fragment __proto__ on Query { __typename }"}',
        None,
        id="a spread of a fragment named __proto__",
    ),
    pytest.param(
        b'{"query":"fragment F on Query { ...F } { ...F }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Internal server error","code":"INTERNAL_SERVER_ERROR","extensions":{}}]}',
            500,
        ),
        id="a fragment that spreads itself",
    ),
    pytest.param(
        b'{"query":"fragment G on Query { ...X } { __typename }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Unknown fragment \\"X\\".","locations":[{"line":1,"column":26}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"Fragment \\"G\\" is never used.","locations":[{"line":1,"column":1}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}]}'
        ),
        id="an undefined spread in a fragment no operation uses",
    ),
    pytest.param(
        b'{"query":"fragment A on Query { ...B } fragment B on Query { ...A } { __typename }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Cannot spread fragment \\"A\\" within itself via \\"B\\".","locations":[{"line":1,"column":23},{"line":1,"column":52}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"Fragment \\"A\\" is never used.","locations":[{"line":1,"column":1}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"Fragment \\"B\\" is never used.","locations":[{"line":1,"column":30}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}]}'
        ),
        id="a cycle among fragments no operation uses",
    ),
    pytest.param(
        b'{"query":"{ ... on Query { ...X } }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Internal server error","code":"INTERNAL_SERVER_ERROR","extensions":{}}]}',
            500,
        ),
        id="an undefined spread in an inline fragment",
    ),
    pytest.param(
        b'{"query":"query A { __typename } query B { ...X }","operationName":"A"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Internal server error","code":"INTERNAL_SERVER_ERROR","extensions":{}}]}',
            500,
        ),
        id="an undefined spread in an operation not chosen",
    ),
    pytest.param(
        b'{"query":"fragment F on Query { ...X } { ...F }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Internal server error","code":"INTERNAL_SERVER_ERROR","extensions":{}}]}',
            500,
        ),
        id="an undefined spread in a fragment an operation uses",
    ),
    pytest.param(
        b'{"query":"fragment F on Query { ...F } { __typename }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Cannot spread fragment \\"F\\" within itself.","locations":[{"line":1,"column":23}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"Fragment \\"F\\" is never used.","locations":[{"line":1,"column":1}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}]}'
        ),
        id="a fragment spreading itself that no operation uses",
    ),
    pytest.param(
        b'{"query":"{ ...X"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Syntax Error: Expected Name, found <EOF>.","locations":[{"line":1,"column":7}],"code":"GRAPHQL_PARSE_FAILED","extensions":{}}]}'
        ),
        id="an undefined spread beside a document that cannot parse",
    ),
    pytest.param(
        b'{"query":"{ __schema { ...X } }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Unknown fragment \\"X\\".","locations":[{"line":1,"column":17}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}]}'
        ),
        id="an undefined spread under __schema",
    ),
    pytest.param(
        b'{"query":"{ __type(name: \\"Query\\") { ...X } }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Unknown fragment \\"X\\".","locations":[{"line":1,"column":30}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}]}'
        ),
        id="an undefined spread under __type",
    ),
    pytest.param(
        b'{"query":"{ __typename { ...X } }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Field \\"__typename\\" must not have a selection since type \\"String!\\" has no subfields.","locations":[{"line":1,"column":14}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"Unknown fragment \\"X\\".","locations":[{"line":1,"column":19}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}]}'
        ),
        id="an undefined spread under __typename",
    ),
    pytest.param(
        b'{"query":"{ ...X } { __typename }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"This anonymous operation must be the only defined operation.","locations":[{"line":1,"column":1}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"Unknown fragment \\"X\\".","locations":[{"line":1,"column":6}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"This anonymous operation must be the only defined operation.","locations":[{"line":1,"column":10}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}]}'
        ),
        id="an undefined spread in an unnamed operation a later one replaces",
    ),
    pytest.param(
        b'{"query":"query A { ...X } query A { __typename }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Unknown fragment \\"X\\".","locations":[{"line":1,"column":14}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"There can be only one operation named \\"A\\".","locations":[{"line":1,"column":7},{"line":1,"column":24}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}]}'
        ),
        id="an undefined spread in an operation a later one of its name replaces",
    ),
    pytest.param(
        b'{"query":"fragment F on Query { transcripts { ...F } } { ...F }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"\'\' exceeds maximum operation depth of 10","locations":[{"line":1,"column":37}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"Cannot spread fragment \\"F\\" within itself.","locations":[{"line":1,"column":37}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"Fragment \\"F\\" cannot be spread here as objects of type \\"Transcript\\" can never be of type \\"Query\\".","locations":[{"line":1,"column":37}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}]}'
        ),
        id="a fragment that spreads itself under a field",
    ),
    pytest.param(
        b'{"query":"{ a{b{c{d{e{f{g{h{i{j{ ...X }}}}}}}}}} }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Internal server error","code":"INTERNAL_SERVER_ERROR","extensions":{}}]}',
            500,
        ),
        id="an undefined spread ten fields deep",
    ),
    pytest.param(
        b'{"query":"{ a{b{c{d{e{f{g{h{i{j{k{ ...X }}}}}}}}}}} }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"\'\' exceeds maximum operation depth of 10","locations":[{"line":1,"column":26}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"Cannot query field \\"a\\" on type \\"Query\\".","locations":[{"line":1,"column":3}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"Unknown fragment \\"X\\".","locations":[{"line":1,"column":29}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}]}'
        ),
        id="an undefined spread eleven fields deep",
    ),
    pytest.param(
        b'{"query":"{ a{b{c{d{e{f{g{h{i{j{k{ l m }}}}}}}}}}} }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"\'\' exceeds maximum operation depth of 10","locations":[{"line":1,"column":26}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"\'\' exceeds maximum operation depth of 10","locations":[{"line":1,"column":28}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"Cannot query field \\"a\\" on type \\"Query\\".","locations":[{"line":1,"column":3}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}]}'
        ),
        id="two fields twelve deep",
    ),
    pytest.param(
        b'{"query":"{ a{b{c{d{e{f{g{h{i{j{k{ l {m} }}}}}}}}}}} }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"\'\' exceeds maximum operation depth of 10","locations":[{"line":1,"column":26}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"Cannot query field \\"a\\" on type \\"Query\\".","locations":[{"line":1,"column":3}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}]}'
        ),
        id="a field twelve deep with a selection",
    ),
    pytest.param(
        b'{"query":"fragment F on Query { ...X } fragment F on Query { __typename } { ...F }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Unknown fragment \\"X\\".","locations":[{"line":1,"column":26}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"There can be only one fragment named \\"F\\".","locations":[{"line":1,"column":10},{"line":1,"column":39}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}]}'
        ),
        id="an undefined spread in the earlier of two fragments of a name",
    ),
    pytest.param(
        b'{"query":"fragment F on Query { __typename } fragment F on Query { ...X } { ...F }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"Internal server error","code":"INTERNAL_SERVER_ERROR","extensions":{}}]}',
            500,
        ),
        id="an undefined spread in the later of two fragments of a name",
    ),
    pytest.param(
        b'{"query":"fragment F on Query { a{b{c{d{e{f{g{h{i{j{k{ l }}}}}}}}}}} } { ...F ...F }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"\'\' exceeds maximum operation depth of 10","locations":[{"line":1,"column":46}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"\'\' exceeds maximum operation depth of 10","locations":[{"line":1,"column":46}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}},{"friendly":false,"message":"Cannot query field \\"a\\" on type \\"Query\\".","locations":[{"line":1,"column":23}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}]}'
        ),
        id="a fragment too deep spread twice",
    ),
    pytest.param(
        _query(_twelve_deep(99)),
        WithTheKey(_validation(*_too_deep(_twelve_deep(99), 99), _no_field_a(3))),
        id="99 fields twelve deep",
    ),
    pytest.param(
        _query(_twelve_deep(100)),
        WithTheKey(_validation(*_too_deep(_twelve_deep(100), 100), _TOO_MANY_ERRORS)),
        id="100 fields twelve deep",
    ),
    pytest.param(
        _query(_twelve_deep(101)),
        WithTheKey(_INTERNAL, 500),
        id="101 fields twelve deep",
    ),
    pytest.param(
        _query(_TWO_FIFTIES),
        WithTheKey(
            _validation(
                *_too_deep(_TWO_FIFTIES, 50, operation="A"),
                *_too_deep(_TWO_FIFTIES, 50, "m", "B"),
                _TOO_MANY_ERRORS,
            )
        ),
        id="50 fields twelve deep in each of two operations",
    ),
    pytest.param(
        _query("query A %s query B %s" % (_twelve_deep(50), _twelve_deep(51, "m"))),
        WithTheKey(_INTERNAL, 500),
        id="50 and 51 fields twelve deep in two operations",
    ),
    pytest.param(
        _query("query A %s" % _twelve_deep(1)),
        WithTheKey(
            _validation(
                *_too_deep("query A %s" % _twelve_deep(1), 1, operation="A"),
                _no_field_a(11),
            )
        ),
        id="a named operation twelve deep",
    ),
    pytest.param(
        b'{"query":"query B { ...X } query A { __typename }"}',
        WithTheKey(_INTERNAL, 500),
        id="an undefined spread in an operation of another name",
    ),
    pytest.param(
        b'{"query":"type Foo { a: Int } { __typename }"}',
        WithTheKey(
            b'{"errors":[{"friendly":false,"message":"The \\"Foo\\" definition is not executable.","locations":[{"line":1,"column":1}],"code":"GRAPHQL_VALIDATION_FAILED","extensions":{}}]}'
        ),
        id="a type definition beside an operation",
    ),
]


# Each credential the body table is sent with, and the 500 a body Fireflies reads draws under it.
_BODY_CREDENTIALS = {"key": None, "none": _BAD_KEY, "Bearer nope": _BAD_KEY, "nope": _NO_PREFIX}
# The rows every credential but the key is sent with: one for each `_body_refusal` (`_NOT_JSON` for
# a body that does not parse and for a poisoned one, `_EMPTY`, `_TOO_LARGE`), one it serves, and a
# `WithTheKey` row.
_EACH_WAY_READ = ("{", "__proto__ key", "empty", "past the limit", "a query", "an array")
assert set(_EACH_WAY_READ) <= {row.id for row in (*_FIREFLIES_BODIES, *_FIREFLIES_READS)}


def _fireflies_body_cases() -> list:
    rows = [pytest.param((), *row.values, id=row.id) for row in _FIREFLIES_BODIES]
    rows += _FIREFLIES_READS
    return [
        pytest.param(credential, *row.values, id=f"{row.id}-{credential}")
        for credential in _BODY_CREDENTIALS
        for row in rows
        if credential == "key" or row.id in _EACH_WAY_READ
    ]


@pytest.mark.parametrize("credential,headers,body,answer", _fireflies_body_cases())
def test_fireflies_orders_the_body_and_the_credential(
    client, admin_h, credential, headers, body, answer
):
    """Each row is answered as the comment over `_FIREFLIES_BODIES` says with the key, and no
    credential changes a `_body_refusal` of `_EACH_WAY_READ`."""
    if not any(name == "Content-Type" for name, _ in headers):
        headers = (("Content-Type", "application/json"), *headers)
    if credential != "none":
        key = admin_h["Authorization"] if credential == "key" else credential
        headers = (*headers, ("Authorization", key))
    r = client.post("/fireflies/graphql", content=body, headers=list(headers))
    if answer is not None and not isinstance(answer, WithTheKey):
        assert (r.status_code, r.content) == (json.loads(answer)["statusCode"], answer)
    elif credential != "key":
        assert (r.status_code, r.content) == (500, _BODY_CREDENTIALS[credential])
    elif answer is not None:
        assert (r.status_code, r.content) == (answer.status, answer)
    else:
        assert (r.status_code, r.json()) == (200, {"data": {"__typename": "Query"}})


@pytest.mark.parametrize(
    "last, answer",
    [("__typename", []), ("a{b{c{d{e{f{g{h{i{j{k{ l }}}}}}}}}}}", 500)],
    ids=["served", "an error on each path"],
)
def test_fireflies_reads_a_fragment_once_however_many_spreads_reach_it(last, answer):
    """A document whose first twenty-five fragments each spread the next one twice reaches the
    twenty-sixth by 2**25 paths, and `_depth_limit` reads each fragment once a depth rather than
    once a path, and holds no more than the 101 errors the paths report before it throws."""
    from backlot.routers import fireflies

    document = " ".join(f"fragment F{i} on Query {{ ...F{i + 1} ...F{i + 1} }}" for i in range(25))
    document = parse(document + f" fragment F25 on Query {{ {last} }} {{ ...F0 }}")
    started = time.perf_counter()
    tracemalloc.start()
    try:
        reported = fireflies._depth_limit(document)
        held = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert (reported if isinstance(reported, list) else reported.status) == answer
    assert (time.perf_counter() - started < 1, held < 2**20) == (True, True)


def test_fireflies_admin_crawl_sees_every_stored_transcript(client, admin_h, ro_conn):
    r = ff_gql(client, "{ transcripts(limit: 50) { id title } }", admin_h)
    assert r.status_code == 200
    served = r.json()["data"]["transcripts"]
    assert len(served) == store.count_fireflies_transcripts(ro_conn)
    assert (
        len(served) == ro_conn.execute("SELECT COUNT(*) FROM fireflies_transcripts").fetchone()[0]
    )


def test_fireflies_transcript_content_round_trips_through_the_api(client, admin_h, ro_conn):
    """The sentences the API serves must rebuild the stored `content` byte for byte — that is the
    whole point of defining content as the concatenation."""
    from backlot import synth

    r = ff_gql(
        client, "{ transcripts(limit: 50) { id title sentences { speaker_name text } } }", admin_h
    )
    for t in r.json()["data"]["transcripts"]:
        row = store.fireflies_transcript_by_id(ro_conn, t["id"])
        assert synth.fireflies_transcript_text(t["sentences"]) == row["content"]


def test_fireflies_serves_the_documented_metadata_surface(client, admin_h):
    r = ff_gql(
        client,
        """
        { transcripts(limit: 1) {
            id title date dateString duration host_email organizer_email participants
            meeting_link calendar_id cal_id calendar_type privacy is_live
            channels { id title members { user_id email name } is_private created_at }
            speakers { id name }
            transcript_url audio_url video_url
            user { user_id email name }
            summary { overview keywords action_items outline topics_discussed meeting_type }
            analytics { sentiments { positive_pct neutral_pct negative_pct }
                        speakers { speaker_id name duration word_count duration_pct
                                   words_per_minute } }
            meeting_attendees { displayName email location }
            sentences { index speaker_name speaker_id text raw_text start_time end_time
                        ai_filters { text_cleanup } }
        } }""",
        admin_h,
    )
    assert r.status_code == 200 and "errors" not in r.json()
    t = r.json()["data"]["transcripts"][0]
    assert t["id"] and t["title"]
    assert t["date"] and t["dateString"]
    assert t["channels"] and t["transcript_url"] and t["audio_url"] and t["video_url"]
    assert t["sentences"] and t["analytics"]["sentiments"]["positive_pct"] is not None
    s0 = t["sentences"][0]
    assert s0["ai_filters"]["text_cleanup"] == s0["text"]
    # `date` is a bare number, as the vendor declares it — not an ISO string under a custom scalar
    assert isinstance(t["date"], (int, float))
    # the roster carries identity; the talk-time numbers are on analytics.speakers, keyed by the
    # same per-meeting number the sentences use
    assert t["speakers"]
    assert {sp["id"] for sp in t["speakers"]} == {float(s["speaker_id"]) for s in t["sentences"]}
    assert {sp["speaker_id"] for sp in t["analytics"]["speakers"]} == {
        int(sp["id"]) for sp in t["speakers"]
    }
    assert t["is_live"] is False and t["privacy"]
    # `channels.id` IS the channel name, so it can be fed straight back to `channel_id:`
    ch = t["channels"][0]
    assert ch["id"] == ch["title"]
    back = ff_gql(
        client,
        '{ transcripts(channel_id: "%s", limit: 50) { id participants } }' % ch["id"],
        admin_h,
    ).json()["data"]["transcripts"]
    assert t["id"] in {x["id"] for x in back}
    # the roster is who took part in the channel's meetings, not the channel's ACL group — and
    # EXACTLY them: a superset would also be what a roster aggregated without the ACL clause looks
    # like (tests/test_acl.py pins that from the denied caller's side)
    assert {m["email"] for m in ch["members"]} == {e for x in back for e in x["participants"]}
    assert ch["members"]
    assert all(m["user_id"] and m["name"] for m in ch["members"])
    # the channel's own history is not in the corpus
    assert ch["is_private"] is None and ch["created_at"] is None


def test_fireflies_sentence_windows_are_ordered_and_contiguous(client, admin_h):
    r = ff_gql(
        client, "{ transcripts(limit: 50) { sentences { index start_time end_time } } }", admin_h
    )
    for t in r.json()["data"]["transcripts"]:
        sents = t["sentences"]
        assert [s["index"] for s in sents] == list(range(len(sents)))
        for a, b in zip(sents, sents[1:]):
            assert a["start_time"] < a["end_time"] <= b["start_time"]


def test_fireflies_date_is_epoch_millis_matching_the_iso_string(client, admin_h):
    """Fireflies returns `date` as epoch MILLISECONDS — a client that divides by 1000 must land on
    the same instant `dateString` states."""
    import datetime as dt

    r = ff_gql(client, "{ transcripts(limit: 50) { date dateString } }", admin_h)
    for t in r.json()["data"]["transcripts"]:
        parsed = dt.datetime.fromisoformat(t["dateString"].replace("Z", "+00:00"))
        assert t["date"] == parsed.timestamp() * 1000


def test_fireflies_organizer_falls_back_to_the_host(client, admin_h, ro_conn):
    """The column is NULL when a meeting's organizer is its host; the FIELD must still answer,
    because Fireflies itself never returns a null organizer for a hosted meeting."""
    assert (
        ro_conn.execute(
            "SELECT organizer_email FROM fireflies_transcripts WHERE id = ?",
            (served_id("fireflies", "ff-discovery"),),
        ).fetchone()[0]
        is None
    )
    r = ff_gql(client, "{ transcripts(limit: 50) { host_email organizer_email } }", admin_h)
    served = r.json()["data"]["transcripts"]
    assert all(t["organizer_email"] for t in served)
    assert any(t["organizer_email"] == t["host_email"] for t in served)


def test_fireflies_an_unnamed_speaker_still_carries_its_number(client, admin_h):
    """Diarization that produced no label still numbered the speaker, so the roster and the
    analytics have to agree about it rather than one of them dropping the run."""
    t = ff_gql(
        client,
        '{ transcript(id: "%s") { speakers { id name } '
        "analytics { speakers { speaker_id name word_count } } "
        "sentences { speaker_id speaker_name } } }" % served_id("fireflies", "ff-discovery"),
        admin_h,
    ).json()["data"]["transcript"]

    # the fixture's last sentence is "(crosstalk)", transcribed without a speaker label
    assert None in {s["speaker_name"] for s in t["sentences"]}
    # every speaker the sentences number appears in the roster, unnamed one included
    assert {float(s["speaker_id"]) for s in t["sentences"]} == {sp["id"] for sp in t["speakers"]}
    assert None in {sp["name"] for sp in t["speakers"]}
    # and analytics keys on the same numbers, so no entry is left without one
    assert all(sp["speaker_id"] is not None for sp in t["analytics"]["speakers"])
    assert {sp["speaker_id"] for sp in t["analytics"]["speakers"]} == {
        s["speaker_id"] for s in t["sentences"]
    }


def test_fireflies_transcript_by_id_matches_the_listing(client, admin_h):
    listed = ff_gql(client, "{ transcripts(limit: 1) { id title } }", admin_h).json()["data"][
        "transcripts"
    ][0]
    one = ff_gql(
        client, "query($i:String!){ transcript(id:$i) { id title } }", admin_h, i=listed["id"]
    ).json()["data"]["transcript"]
    assert one == listed
    absent = ff_gql(client, '{ transcript(id: "deadbeefdeadbeefdeadbeef") { id } }', admin_h).json()
    assert absent["data"]["transcript"] is None


def test_fireflies_limit_outside_measured_bounds_is_refused_over_http(client, admin_h):
    body = ff_gql(client, "query($l:Int){ transcripts(limit:$l) { id } }", admin_h, l=51).json()
    assert body["data"]["transcripts"] is None
    assert body["errors"][0]["extensions"]["metadata"]["fields"][0]["name"] == "limit"


def test_fireflies_skip_pages_without_gaps_or_repeats(client, admin_h, ro_conn):
    total = store.count_fireflies_transcripts(ro_conn)
    walked = []
    for skip in range(total + 1):
        page = ff_gql(
            client, "query($s:Int){ transcripts(limit:1, skip:$s) { id } }", admin_h, s=skip
        ).json()["data"]["transcripts"]
        walked += [t["id"] for t in page]
    assert len(walked) == total == len(set(walked))
    # past the end is an empty list, not an error
    beyond = ff_gql(client, "{ transcripts(limit: 5, skip: 5000) { id } }", admin_h).json()
    assert beyond["data"]["transcripts"] == []


def test_fireflies_keyword_scope_over_http(client, admin_h):
    def titles(**args):
        arglist = ", ".join(f'{k}: "{v}"' for k, v in args.items())
        return {
            t["title"]
            for t in ff_gql(
                client, "{ transcripts(%s, limit: 50) { title } }" % arglist, admin_h
            ).json()["data"]["transcripts"]
        }

    assert titles(keyword="selects", scope="title") == set()
    assert titles(keyword="selects", scope="sentences") == {"April all-hands"}
    assert titles(keyword="selects", scope="all") == {"April all-hands"}
    assert titles(keyword="all-hands", scope="title") == {"April all-hands"}
    # `title:` narrows on the title by SUBSTRING and takes no scope, which is what separates it
    # from `keyword:` — a partial word still matches.
    assert titles(title="all-hand") == {"April all-hands"}
    assert titles(title="latency") == {"Acme x Northwind — latency discovery"}
    # and it does not reach the sentences the way keyword+scope can
    assert titles(title="selects") == set()


def test_fireflies_date_selects_a_day_and_the_singular_email_filters_narrow(client, admin_h):
    """`date:` is a DAY, not an instant — the live API returns every meeting sharing the calendar
    day of the value passed, so any millisecond within the day has to match. `organizer_email` and
    `participant_email` are the singular forms of the plural filters."""

    def titles(arglist):
        return {
            t["title"]
            for t in ff_gql(
                client, "{ transcripts(%s, limit: 50) { title } }" % arglist, admin_h
            ).json()["data"]["transcripts"]
        }

    # 2026-04-10T16:00:00Z, the all-hands meeting: its own instant, and midnight that day
    assert titles("date: 1775836800000") == {"April all-hands"}
    assert titles("date: 1775779200000") == {"April all-hands"}
    # the millisecond before that midnight is the day before, and the instant in seconds is read
    # as milliseconds, a day in January 1970
    assert titles("date: 1775779199999") == set()
    assert titles("date: 1775836800") == set()
    # the discovery call is a different day, so it is not in either answer
    assert titles("date: 1775142000000") == {"Acme x Northwind — latency discovery"}
    # a day no meeting falls on
    assert titles("date: 1600000000000") == set()
    # `date` narrows AGAINST a range rather than replacing it: a toDate before that day wins
    assert titles("date: 1775836800000, toDate: 1775142000000") == set()

    assert titles('host_email: "ava@acme.com"') == {"Acme x Northwind — latency discovery"}
    assert titles('organizer_email: "ava@acme.com"') == {"Acme x Northwind — latency discovery"}
    assert titles('participant_email: "ava@acme.com"') == {"Acme x Northwind — latency discovery"}
    # upper-cased, the host and organizer addresses find nothing while the participant address
    # still matches, as measured above those two filters in `store._fireflies_where`
    assert titles('host_email: "AVA@ACME.COM"') == set()
    assert titles('organizer_email: "AVA@ACME.COM"') == set()
    assert titles('participant_email: "AVA@ACME.COM"') == {"Acme x Northwind — latency discovery"}
    # `participants` is who the SENTENCES attribute, so a guest-list-only attendee is not one
    assert titles('participant_email: "dana@northwind.example"') == set()
    assert titles('participant_email: "nobody@acme.com"') == set()


def test_fireflies_date_reads_its_day_with_the_fraction_cut_toward_zero(tmp_path):
    """`-0.5` cut toward zero is 0, a millisecond of 1970-01-01, and `-86400000.5` is the midnight
    that opens 1969-12-31; each read as a floor would name the day before."""
    records = [
        {**FIREFLIES_CORPUS[0], "doc_id": f"ff-{day}", "title": day, "created": f"{day}T12:00:00Z"}
        for day in ("1969-12-30", "1969-12-31", "1970-01-01")
    ]
    with corpus_client(tmp_path, records) as (client, settings):

        def titles(date):
            query = "{ transcripts(date: %s) { title } }" % date
            return [t["title"] for t in _ff(client, settings, query)["data"]["transcripts"]]

        assert titles("-0.5") == ["1970-01-01"]
        assert titles("-1") == ["1969-12-31"]
        assert titles("-86400000.5") == ["1969-12-31"]
        assert titles("-86400001") == ["1969-12-30"]


def test_fireflies_unknown_scope_is_a_field_error_not_a_silent_widening(client, admin_h):
    """Silently searching everything would hide a client's typo. A field error keeps the
    GraphQL-over-HTTP contract: 200 with partial data alongside errors."""
    r = ff_gql(client, '{ transcripts(keyword: "x", scope: "body") { id } }', admin_h)
    assert r.status_code == 200
    assert r.content.startswith(b'{"errors":[')
    body = r.json()
    assert "data" in body  # field error -> data key present
    assert body["data"]["transcripts"] is None
    assert body["errors"][0]["code"] == "invalid_arguments"
    # control: a clean response still opens with "data"
    ok = ff_gql(client, '{ transcripts(keyword: "x", scope: "all") { id } }', admin_h)
    assert ok.content.startswith(b'{"data":')


def test_fireflies_request_errors_are_400_with_no_data_key(client, admin_h):
    """The engine's request/field split: a malformed or invalid document is decided before
    execution, so the response carries no `data` entry at all."""
    for query in ("{ transcripts(", "{ transcripts { nosuchfield } }", "{ }"):
        r = ff_gql(client, query, admin_h)
        assert r.status_code == 400, query
        assert "data" not in r.json(), query
        assert r.json()["errors"], query


def test_fireflies_user_root_answers_for_a_person_only(client, admin_h, tokens_yaml, ro_conn):
    """`user` with no id is the authenticated user. An admin/service token is not a person."""
    assert ff_gql(client, "{ user { user_id email } }", admin_h).json()["data"]["user"] is None
    ava = next(u["token"] for u in tokens_yaml["users"] if u["email"] == "ava@acme.com")
    h = {"Authorization": f"Bearer {ava}"}
    me = ff_gql(client, "{ user { user_id email name } }", h).json()["data"]["user"]
    assert me["email"] == "ava@acme.com" and me["user_id"]
    # the served user_id round-trips back through user(id:)
    again = ff_gql(client, "query($i:String){ user(id:$i) { email } }", h, i=me["user_id"]).json()[
        "data"
    ]["user"]
    assert again["email"] == "ava@acme.com"
    # an empty id is read as none, as `resolve_user` records
    for headers, user in ((admin_h, None), (h, {"email": "ava@acme.com"})):
        empty = ff_gql(client, '{ user(id: "") { email } }', headers).json()
        assert empty == {"data": {"user": user}}


def test_fireflies_an_id_naming_nothing_answers_with_reals_error(client, admin_h, tokens_yaml):
    """Each query names nothing its caller can read and draws the bytes real answered it with, but
    for the newline real ends them with: `data` null beside the error ``_not_in_team`` or
    ``_object_not_found`` builds, its `correlationId` eight letters and digits. A transcript the
    caller cannot see draws what one that does not exist draws, and `hana@acme.com`, who can see
    it, reads it."""
    import re

    not_in_team = (
        '{"errors":[{"friendly":true,"message":"You do not have permissions for this team",'
        '"locations":[{"line":1,"column":3}],"path":["user"],"code":"not_in_team",'
        '"extensions":{"code":"not_in_team","correlationId":"<8>","status":403,"helpUrls":'
        '["https://docs.fireflies.ai/miscellaneous/error-codes#not_in_team"]}}],'
        '"data":{"user":null}}'
    )
    object_not_found = (
        '{"errors":[{"friendly":true,"message":"Transcript not found",'
        '"locations":[{"line":1,"column":3}],"path":["transcript"],"code":"object_not_found",'
        '"extensions":{"code":"object_not_found","correlationId":"<8>","status":404,'
        '"metadata":{"objectType":"Transcript"},"helpUrls":'
        '["https://docs.fireflies.ai/miscellaneous/error-codes#object_not_found"]}}],'
        '"data":{"transcript":null}}'
    )
    tokens = {u["email"]: u["token"] for u in tokens_yaml["users"]}
    listing = "{ transcripts(limit: 50) { id } }"
    every = ff_gql(client, listing, admin_h).json()["data"]["transcripts"]
    ava_h = {"Authorization": f"Bearer {tokens['ava@acme.com']}"}
    seen = ff_gql(client, listing, ava_h).json()["data"]["transcripts"]
    (hidden,) = {t["id"] for t in every} - {t["id"] for t in seen}
    wanted = {
        ("ava@acme.com", '{ user(id: "nope") { user_id } }'): not_in_team,
        ("ava@acme.com", '{ user(id: " ") { user_id } }'): not_in_team,
        ("ava@acme.com", '{ user(id: "nobody@example.com") { user_id } }'): not_in_team,
        ("ava@acme.com", '{ user(id: "ava@acme.com") { user_id } }'): not_in_team,
        ("ava@acme.com", '{ transcript(id: "nope") { id } }'): object_not_found,
        ("ava@acme.com", '{ transcript(id: "%s") { id } }' % hidden): object_not_found,
        ("hana@acme.com", '{ transcript(id: "%s") { id } }' % hidden): (
            '{"data":{"transcript":{"id":"%s"}}}' % hidden
        ),
    }
    answered = {}
    for caller, query in wanted:
        r = ff_gql(client, query, {"Authorization": f"Bearer {tokens[caller]}"})
        assert r.status_code == 200, query
        answered[caller, query] = re.sub(
            r'"correlationId":"[A-Za-z0-9]{8}"', '"correlationId":"<8>"', r.text
        )
    assert answered == wanted


def test_fireflies_introspection_describes_the_schema(client, admin_h):
    """There is no OpenAPI entry for this route on purpose, so introspection is how a client
    discovers the surface."""
    r = ff_gql(
        client,
        "{ __schema { queryType { fields { name } } } "
        '__type(name: "Sentence") { fields { name type { name } } } }',
        admin_h,
    )
    data = r.json()["data"]
    names = {f["name"] for f in data["__schema"]["queryType"]["fields"]}
    assert {"transcripts", "transcript", "user", "users"} <= names
    # a client generated against the vendor schema names these in fragments and deserializers
    sentence = {f["name"]: f["type"]["name"] for f in data["__type"]["fields"]}
    assert sentence["start_time"] == "Float" and sentence["end_time"] == "Float"
    assert sentence["speaker_id"] == "Int"
    assert sentence["ai_filters"] == "AIFilters"


def test_fireflies_mcp_tools_derive_from_the_served_introspection(client, admin_h):
    """The GraphQL→MCP bridge generates its documents from this endpoint's own introspection, so
    each one has to be a document this schema accepts.

    The default depth of 2 is what `analytics.sentiments` needs: `Analytics` carries no leaf
    fields of its own, so a shallower selection would drop the whole node and with it the
    sentiment split and per-speaker talk time the corpus actually computes.
    """
    intro = ff_gql(client, mcp_tools.INTROSPECTION_QUERY, admin_h).json()
    schema = build_client_schema(intro["data"])
    tools = {t.name: t for t in mcp_tools.derive_tools(intro)}

    assert set(tools) == set(schema.query_type.fields)
    for tool in tools.values():
        assert not validate(schema, parse(tool.document)), tool.name
    assert "positive_pct" in tools["transcripts"].document


def test_fireflies_the_hand_written_examples_queries_still_validate(client, admin_h):
    """Fireflies has no SDK, so the example's queries are hand-written and drift silently: nothing
    executes that file, and a schema change here turns it into a 400 nobody sees. Read it as text
    (a test must not import from ``examples/``) and hold every document it sends to the same bar
    the generated MCP documents meet above.
    """
    import re

    from tests.conftest import REPO_ROOT

    text = (REPO_ROOT / "examples/using-official-sdk/fireflies.py").read_text(encoding="utf-8")
    documents = {
        name: body
        for name, body in re.findall(r'^([A-Z_]+) = """(.*?)"""', text, re.S | re.M)
        if "query " in body
    }
    assert documents, "no query literals found -- fix this extractor, not the assertion"

    schema = build_client_schema(
        ff_gql(client, mcp_tools.INTROSPECTION_QUERY, admin_h).json()["data"]
    )
    assert {
        name: [e.message for e in validate(schema, parse(body))]
        for name, body in documents.items()
        if validate(schema, parse(body))
    } == {}


def test_fireflies_declares_no_mutations(client, admin_h):
    """Backlot serves no Fireflies write, so its schema declares no Mutation type rather than
    accepting one and dropping it. A fact about this source, not about the server: Slack takes
    writes."""
    r = ff_gql(client, "{ __schema { mutationType { name } } }", admin_h)
    assert r.json()["data"]["__schema"]["mutationType"] is None


# --- fireflies -------------------------------------------------------------------
# Fireflies' shape differs from every other GraphQL source here in two ways that clients depend
# on: snake_case field names, and offset pagination returning a BARE LIST rather than a Relay
# connection. Both are pinned, because "it's GraphQL" is exactly the assumption that would
# otherwise make someone wrap this in `{ nodes, pageInfo }`.

FIREFLIES_CORPUS = [
    {
        "source_type": "fireflies",
        "doc_id": "ff-f1",
        "channel": "sales-calls",
        "title": "Fidelity discovery call",
        "host_email": "ava@acme.com",
        "host_name": "Ava Chen",
        "organizer_email": "ops@acme.com",
        "duration": 45.0,
        "calendar_id": "cal-fid",
        "created": "2026-04-02T15:00:00Z",
        "visibility": "public",
        "summary": {
            "overview": "Overview text.",
            "topics_discussed": ["latency"],
            "action_items": ["Ava: follow up", "Bob: benchmark"],
            "keywords": ["latency"],
            "meeting_type": "discovery",
        },
        "meeting_attendees": [
            {"displayName": "Ava Chen", "email": "ava@acme.com", "location": None}
        ],
        "sentences": [
            {
                "speaker_name": "Ava Chen",
                "author_email": "ava@acme.com",
                "start_time": 0,
                "text": "Kicking off.",
            },
            {"speaker_name": "Dana Ruiz", "start_time": 20, "text": "Sounds good."},
        ],
    },
]


@pytest.fixture(scope="module")
def fireflies_corpus(tmp_path_factory):
    """FIREFLIES_CORPUS built."""
    return tiny_corpus(tmp_path_factory.mktemp("fireflies_corpus"), FIREFLIES_CORPUS)


def _ff(client, settings, query, **variables):
    return gql(
        client, "/fireflies/graphql", query, f"Bearer {settings.admin_token}", **variables
    ).json()


def test_fireflies_accepts_the_vendors_documented_raw_http_post(fireflies_corpus):
    """There is no Fireflies SDK: the vendor's quickstart is curl / requests.post / axios.post /
    Java HttpClient against one endpoint with a Bearer key. That IS the client story, so the exact
    shape those examples send has to work."""
    settings = fireflies_corpus
    with client_for(settings) as client:
        r = client.post(
            "/fireflies/graphql",
            json={
                "query": "query Transcripts($limit: Int) "
                "{ transcripts(limit: $limit) { id title } }",
                "variables": {"limit": 10},
            },
            headers={
                "Authorization": f"Bearer {settings.admin_token}",
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json()["data"]["transcripts"][0]["title"] == "Fidelity discovery call"


def test_fireflies_transcripts_is_a_bare_list_not_a_relay_connection(fireflies_corpus):
    """Fireflies pages with limit/skip. Wrapping it in `{ nodes, pageInfo }` — the shape every
    other GraphQL source here uses — would break every generated client."""
    settings = fireflies_corpus
    with client_for(settings) as client:
        got = _ff(client, settings, "{ transcripts(limit: 5) { id } }")
        assert isinstance(got["data"]["transcripts"], list)
        # asking for a connection's fields must be a validation error, i.e. they do not exist
        bad = _ff(client, settings, "{ transcripts { nodes { id } pageInfo { hasNextPage } } }")
        assert "data" not in bad and bad["errors"]


def test_fireflies_field_names_are_snake_case(fireflies_corpus):
    """Fireflies' own convention, not a translation — a camelCase spelling must NOT resolve."""
    settings = fireflies_corpus
    with client_for(settings) as client:
        ok = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { host_email organizer_email "
            "audio_url video_url transcript_url meeting_link "
            "calendar_type meeting_attendees { displayName } "
            "sentences { speaker_name speaker_id start_time end_time } } }",
        )
        assert "errors" not in ok
        t = ok["data"]["transcripts"][0]
        assert t["host_email"] == "ava@acme.com"
        assert t["organizer_email"] == "ops@acme.com"  # distinct organizer is kept, not coerced
        for camel in ("hostEmail", "audioUrl", "transcriptUrl", "meetingLink"):
            bad = _ff(client, settings, "{ transcripts(limit: 1) { %s } }" % camel)
            assert "data" not in bad and bad["errors"], camel


def test_fireflies_duration_is_minutes(fireflies_corpus):
    """The API's unit. Serving seconds would make every meeting read as 60x too long."""
    settings = fireflies_corpus
    with client_for(settings) as client:
        t = _ff(client, settings, "{ transcripts(limit: 1) { duration } }")["data"]["transcripts"][
            0
        ]
        assert t["duration"] == 45.0


def test_fireflies_action_items_are_a_newline_joined_string(fireflies_corpus):
    """Fireflies returns `summary.action_items` as ONE string, not a list — a client doing
    `.split("\\n")` on it must not get a JSON array."""
    settings = fireflies_corpus
    with client_for(settings) as client:
        s = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { summary { action_items "
            "topics_discussed keywords overview meeting_type } } }",
        )["data"]["transcripts"][0]["summary"]
        assert s["action_items"] == "Ava: follow up\nBob: benchmark"
        # topics/keywords ARE lists in the real API, so they must stay lists
        assert s["topics_discussed"] == ["latency"]
        assert s["keywords"] == ["latency"]
        assert s["meeting_type"] == "discovery"


def test_fireflies_sentence_times_are_seconds_while_duration_is_minutes(fireflies_corpus):
    """The two units really do differ in the real API; a server that made them agree would look
    tidier and be wrong."""
    settings = fireflies_corpus
    with client_for(settings) as client:
        t = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { duration sentences { start_time end_time } } }",
        )["data"]["transcripts"][0]
        assert t["sentences"][1]["start_time"] == 20.0  # seconds
        assert t["duration"] == 45.0  # minutes
        assert t["sentences"][-1]["end_time"] <= t["duration"] * 60


def test_fireflies_speaker_id_is_an_integer_scoped_to_the_meeting(fireflies_corpus):
    settings = fireflies_corpus
    with client_for(settings) as client:
        sents = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { sentences { index speaker_id speaker_name } } }",
        )["data"]["transcripts"][0]["sentences"]
        assert [s["speaker_id"] for s in sents] == [0, 1]
        assert all(isinstance(s["speaker_id"], int) for s in sents)
        assert [s["index"] for s in sents] == [0, 1]


def test_fireflies_a_page_costs_the_same_queries_as_one_transcript(tmp_path):
    """The three fields that hit the DB are resolved PER TRANSCRIPT — the speaker roster (twice
    over, since `analytics.speakers` reads the same numbers), and a channel's members. Read per
    row, each is a statement per row of a 50-item page; read for the page, each is one.

    Counted rather than described, because nothing else in the suite fails when it doubles."""
    records = [
        {
            **FIREFLIES_CORPUS[0],
            "doc_id": f"ff-page-{i}",
            "title": f"Fidelity discovery call {i}",
            "created": f"2026-04-{i + 2:02d}T15:00:00Z",
        }
        for i in range(5)
    ]
    with corpus_client(tmp_path, records) as (client, settings):
        conn = client.app.state.conn

        def queries(query) -> int:
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            try:
                got = _ff(client, settings, query)
            finally:
                conn.set_trace_callback(None)
            assert "errors" not in got, got
            assert len(got["data"]["transcripts"]) == 5
            return len(statements)

        assert queries("{ transcripts(limit: 5) { id title } }") == 1  # metadata never queries
        assert queries("{ transcripts(limit: 5) { speakers { id name } } }") == 2
        # the second speaker field reads the first one's rows rather than repeating the statement
        assert (
            queries(
                "{ transcripts(limit: 5) { speakers { id } "
                "analytics { speakers { speaker_id } } } }"
            )
            == 2
        )
        # the roster belongs to the CHANNEL, and these five meetings share one
        assert queries("{ transcripts(limit: 5) { channels { id members { email } } } }") == 2


def test_fireflies_stubbed_fields_are_null_not_invented(fireflies_corpus):
    """The SDL declares more than a document corpus can back. Everything unbacked must be null —
    an invented sentiment or classifier flag is worse than an honest gap."""
    settings = fireflies_corpus
    with client_for(settings) as client:
        t = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { apps_preview { outputs { app_id } } "
            "workspace_users shared_with { email } "
            "meeting_attendance { name join_time leave_time } "
            "summary { bullet_gist gist transcript_chapters notes short_overview "
            "          extended_sections { title } } "
            "analytics { categories { questions tasks } } "
            "sentences { ai_filters { text_cleanup task question } } "
            "user { minutes_consumed is_admin integrations is_calendar_in_sync "
            "       user_groups { id } } } }",
        )["data"]["transcripts"][0]
        assert t["meeting_attendance"] is None
        # Fireflies serves the wrapper and the empty lists; only what it nulls is null here
        assert t["apps_preview"] == {"outputs": []}
        assert t["workspace_users"] == [] and t["shared_with"] == []
        assert t["user"]["user_groups"] == []
        assert t["user"]["is_calendar_in_sync"] is None
        assert t["summary"]["bullet_gist"] is None and t["summary"]["gist"] is None
        assert t["summary"]["transcript_chapters"] is None
        assert t["summary"]["notes"] is None and t["summary"]["short_overview"] is None
        assert t["summary"]["extended_sections"] is None
        assert t["analytics"]["categories"]["questions"] is None
        # the object is served; only the classifier flags inside it are null
        assert t["sentences"][0]["ai_filters"]["task"] is None
        assert t["sentences"][0]["ai_filters"]["question"] is None
        assert t["user"]["minutes_consumed"] is None and t["user"]["is_admin"] is None


def test_fireflies_analytics_sentiments_sum_to_one_hundred(fireflies_corpus):
    """Synthesized, never derived from the text — but it still has to be internally coherent, or a
    consumer charting it gets nonsense."""
    settings = fireflies_corpus
    with client_for(settings) as client:
        s = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { analytics { sentiments "
            "{ positive_pct neutral_pct negative_pct } } } }",
        )["data"]["transcripts"][0]["analytics"]["sentiments"]
        assert round(s["positive_pct"] + s["neutral_pct"] + s["negative_pct"]) == 100
        assert all(v >= 0 for v in s.values())


def test_fireflies_speaker_analytics_are_computed_from_the_sentences(fireflies_corpus):
    """Talk time and word counts ARE derivable from the transcript, so unlike sentiment they are
    real rather than synthesized."""
    settings = fireflies_corpus
    with client_for(settings) as client:
        t = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { analytics { speakers "
            "{ name duration word_count duration_pct } } "
            "sentences { speaker_name text start_time end_time } } }",
        )["data"]["transcripts"][0]
        by_name = {s["name"]: s for s in t["analytics"]["speakers"]}
        assert set(by_name) == {"Ava Chen", "Dana Ruiz"}
        for sent in t["sentences"]:
            spoken = len(sent["text"].split())
            assert by_name[sent["speaker_name"]]["word_count"] >= spoken
        assert by_name["Ava Chen"]["duration"] == 20.0  # 0 -> 20, its own window


def test_fireflies_speaker_shares_sum_to_one_hundred(fireflies_corpus):
    """`duration_pct` shares out the TALK TIME, not the declared meeting length: a corpus
    transcript often does not span its whole meeting, and dividing by the declared length emits
    shares summing to ~4%, which reads as a bug in anything that charts them."""
    settings = fireflies_corpus
    with client_for(settings) as client:
        speakers = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { duration analytics "
            "{ speakers { duration duration_pct } } } }",
        )["data"]["transcripts"][0]["analytics"]["speakers"]
        assert round(sum(s["duration_pct"] for s in speakers)) == 100
        # and the talk time really is far short of the declared 45-minute meeting
        assert sum(s["duration"] for s in speakers) < 45 * 60


def test_fireflies_no_openapi_entry_for_the_graphql_route(fireflies_corpus):
    """Describing one POST that accepts an arbitrary query tells an OpenAPI->MCP bridge nothing,
    so the route is deliberately absent from the document (and from SOURCE_PREFIXES)."""
    from backlot import openapi

    settings = fireflies_corpus
    with client_for(settings) as client:
        spec = client.get("/openapi.json").json()
        assert not [p for p in spec["paths"] if p.startswith("/fireflies")]
        assert "fireflies" not in openapi.SOURCE_PREFIXES


def test_fireflies_users_is_the_workspace_roster_not_every_named_person(tmp_path, monkeypatch):
    """`users` must be the people with an ACCOUNT. Backlot's principals table registers every
    internal reference across every source — 16,034 on the deployed bench corpus, of whom 327 have
    a token — so serving all of them would be wrong (they have no Fireflies account) AND a 1.6 MB
    unpaginated response. The real query takes no pagination args, so scoping is what bounds it.

    `user(id:)` must still resolve a display-only principal, or a transcript whose host never had
    an account would serve `user: null`.
    """

    from backlot import store, synth

    settings = tiny_corpus(tmp_path, FIREFLIES_CORPUS)
    # A principal the corpus names but who has no token — what an ERB append creates. `served_id`
    # -> email is written AT IMPORT now, not rebuilt from `principals` on every boot, so a
    # principal inserted straight into the table — bypassing byo's own principal-writing loop —
    # needs its own `fireflies_users` row too, exactly as a real out-of-band write would have to
    # supply one.
    conn = store.connect_rw(settings.db_path)
    conn.execute(
        "INSERT OR REPLACE INTO principals(id, type, display_name, email) VALUES (?,?,?,?)",
        ("ghost@acme.com", "user", "Ghost Person", "ghost@acme.com"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO fireflies_users(email, served_id) VALUES (?,?)",
        ("ghost@acme.com", synth.fireflies_user_id("ghost@acme.com")),
    )
    conn.commit()
    conn.close()

    with client_for(settings) as client:
        emails = {u["email"] for u in _ff(client, settings, "{ users { email } }")["data"]["users"]}
        assert "ghost@acme.com" not in emails, "a tokenless principal is not a workspace member"
        assert "ava@acme.com" in emails  # the corpus's real, tokened host
        assert emails <= set(_roster_emails(settings))

        # ...but the display-only person is still addressable by id
        got = _ff(
            client,
            settings,
            "query($i:String){ user(id:$i) { email name } }",
            i=synth.fireflies_user_id("ghost@acme.com"),
        )["data"]["user"]
        assert got["email"] == "ghost@acme.com" and got["name"] == "Ghost Person"

    # No probe: `synth.fireflies_user_id` draws from a 96-bit digest, so the raw seed is
    # stored as-is -- a forced collision between two DIFFERENT users must fail the import loudly
    # through `idx_fireflies_user_served`, not silently let one user's row overwrite the other's.
    # A fresh two-host corpus, not FIREFLIES_CORPUS: that fixture's only ACCOUNT is ava (organizer
    # ops@acme.com never becomes a `type='user'` principal), so it has just one row to collide.
    monkeypatch.setattr(synth, "fireflies_user_id", lambda email: "dup-user-id")
    collide_docs = [
        {
            "source_type": "fireflies",
            "doc_id": f"ff-collide-{i}",
            "channel": "general",
            "title": "t",
            "host_email": e,
            "duration": 1.0,
            "created": "2026-01-01T00:00:00Z",
            "sentences": [{"speaker_name": "x", "author_email": e, "start_time": 0, "text": "hi"}],
        }
        for i, e in enumerate(("one@acme.com", "two@acme.com"))
    ]
    with pytest.raises(sqlite3.IntegrityError):
        tiny_corpus(tmp_path / "collide", collide_docs)


def _roster_emails(settings):
    import yaml

    data = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8")) or {}
    return [u["email"] for u in data.get("users", [])]


# --- Fastify, @fastify/cors and Apollo Server in front of Fireflies' GraphQL ----------------------
# Each row: a request, its credential (`_FIREFLIES_CREDENTIALS`), and api.fireflies.ai's answer to
# it, as `graphql` records the layers it was measured through: status, media type and body less
# the newline Apollo Server ends one with, `_TYPENAME_DATA` where the query was served, nothing for
# a `HEAD`.
_CSRF = _apollo(
    b"This operation has been blocked as a potential Cross-Site Request Forgery (CSRF). Please"
    b" either specify a 'content-type' header (with a type that is not one of"
    b" application/x-www-form-urlencoded, multipart/form-data, text/plain) or provide a non-empty"
    b" value for one of the following headers: x-apollo-operation-name, apollo-require-preflight"
    b"\\n"
)
_ONLY_GET_POST = _apollo(b"Apollo Server supports only GET/POST requests.")
_NOT_ACCEPTABLE = _apollo(
    b"An 'accept' header was provided for this request which does not accept application/json;"
    b" charset=utf-8 or application/graphql-response+json; charset=utf-8"
)
_GET_CONTENT_TYPE = _apollo(
    b"GET requests may not have a content-type header other than application/json."
)
_UNSUPPORTED = (
    b'{"statusCode":415,"status":false,"code":"http","message":"Unsupported Media Type",'
    b'"errorMessage":"Unsupported Media Type"}'
)
_TYPENAME_DATA = b'{"data":{"__typename":"Query"}}'
_JSON_UTF8 = "application/json; charset=utf-8"
_GRAPHQL_RESPONSE = "application/graphql-response+json; charset=utf-8"
_CLOUDFLARE_PAGE = (
    b"<html>\r\n<head><title>%s</title></head>\r\n<body>\r\n<center><h1>%s</h1></center>\r\n"
    b"<hr><center>cloudflare</center>\r\n</body>\r\n</html>\r\n"
)
_FIREFLIES_PREFLIGHT = {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET,HEAD,POST",
    "vary": "Access-Control-Request-Headers",
    # A preflight asking for no header names gets none back.
    "access-control-allow-headers": None,
}
_J = (("Content-Type", "application/json"),)
_P = (("apollo-require-preflight", "1"),)
_PREFLIGHT_ASKED = (("Origin", "https://example.com"), ("Access-Control-Request-Method", "POST"))
_Q = "?query=%7B%20__typename%20%7D"


_PAST_THE_LIMIT = b"x" * (2**20 + 10)
_LANDING_PAGE = object()  # Apollo Server's page for a browser, `landing_page`'s


def _not_found(method: str, target: str) -> bytes:
    message = b"Cannot %s /graphql%s" % (method.encode(), target.encode())
    return (
        b'{"statusCode":404,"status":false,"code":"not_found","message":"%s","errorMessage":"%s"}'
        % (message, message)
    )


_FIREFLIES_HTTP = [
    # Cloudflare, ahead of everything.
    http_row(
        "TRACE",
        "",
        (),
        None,
        "key",
        405,
        "text/html",
        _CLOUDFLARE_PAGE % ((b"405 Not Allowed",) * 2),
        id="TRACE",
        answered={"connection": "close"},
    ),
    http_row(
        "CONNECT",
        "",
        (),
        None,
        "key",
        400,
        "text/plain; charset=UTF-8",
        b"error code: 1045\n",
        id="CONNECT",
        answered={"connection": None},
    ),
    http_row(
        "PURGE",
        "",
        (),
        None,
        "key",
        400,
        "text/html; charset=UTF-8",
        _CLOUDFLARE_PAGE % ((b"400 Bad Request",) * 2),
        id="PURGE",
        answered={"connection": None},
    ),
    http_row(
        "TRACE",
        "",
        (),
        None,
        "twice",
        400,
        "text/html",
        _CLOUDFLARE_PAGE % ((b"400 Bad Request",) * 2),
        id="TRACE, Authorization twice",
        answered={"connection": "close"},
    ),
    # `@fastify/cors`, whatever the credential and the body.
    *(
        http_row(
            "OPTIONS",
            "",
            _PREFLIGHT_ASKED,
            None,
            c,
            204,
            None,
            b"",
            id=f"preflight, {c}",
            answered=_FIREFLIES_PREFLIGHT,
        )
        for c in ("key", "none", "Bearer nope")
    ),
    http_row(
        "OPTIONS",
        "",
        (("Origin", "null"), ("Access-Control-Request-Method", "POST")),
        None,
        "key",
        204,
        None,
        b"",
        id="Origin null",
        answered=_FIREFLIES_PREFLIGHT,
    ),
    http_row(
        "OPTIONS",
        "",
        (("Origin", "https://example.com"), ("Access-Control-Request-Method", "PUT")),
        None,
        "key",
        204,
        None,
        b"",
        id="asked PUT",
        answered=_FIREFLIES_PREFLIGHT,
    ),
    *(
        http_row(
            "OPTIONS",
            "",
            (*_PREFLIGHT_ASKED, ("Access-Control-Request-Headers", asked)),
            None,
            "key",
            204,
            None,
            b"",
            id=f"asked {asked!r}",
            answered={**_FIREFLIES_PREFLIGHT, "access-control-allow-headers": allowed},
        )
        for asked, allowed in (
            ("Content-Type, X-Foo", "Content-Type, X-Foo"),
            (" a , b ", "a , b"),
            ("a,,b", "a,,b"),
        )
    ),
    *(
        http_row(
            "OPTIONS",
            "",
            h,
            b,
            c,
            400,
            "text/plain",
            b"Invalid Preflight Request",
            id=f"OPTIONS {i}, {c}",
        )
        for h, b, c, i in (
            ((), None, "key", ""),
            ((), None, "none", ""),
            ((), None, "Bearer nope", ""),
            ((("Origin", "https://example.com"),), None, "key", "Origin alone"),
            ((("Access-Control-Request-Method", "POST"),), None, "key", "no Origin"),
            ((("Origin", ""), ("Access-Control-Request-Method", "POST")), None, "key", "Origin ''"),
            (
                (("Origin", "  "), ("Access-Control-Request-Method", "POST")),
                None,
                "key",
                "Origin '  '",
            ),
            (
                (("Origin", "https://example.com"), ("Access-Control-Request-Method", "")),
                None,
                "key",
                "Access-Control-Request-Method ''",
            ),
            ((*_J, *_P), b"{", "key", "{"),
        )
    ),
    # `_not_found`, after `_fastify_read`.
    http_row(
        "ACL",
        "?_=271823726",
        (*_J, *_P),
        None,
        "key",
        404,
        _JSON_UTF8,
        b'{"statusCode":404,"status":false,"code":"not_found","message":"Cannot ACL /graphql?_=27182'
        b'3726","errorMessage":"Cannot ACL /graphql?_=271823726"}',
        id="ACL",
    ),
    *(
        http_row(m, "?_=1", (*_J, *_P), None, "key", 404, _JSON_UTF8, _not_found(m, "?_=1"), id=m)
        for m in (
            "BIND",
            "CHECKOUT",
            "COPY",
            "LINK",
            "LOCK",
            "M-SEARCH",
            "MERGE",
            "MKACTIVITY",
            "MKCALENDAR",
            "MKCOL",
            "MOVE",
            "NOTIFY",
            "PROPFIND",
            "PROPPATCH",
            "QUERY",
            "REBIND",
            "REPORT",
            "SEARCH",
            "SOURCE",
            "SUBSCRIBE",
            "UNBIND",
            "UNLINK",
            "UNLOCK",
            "UNSUBSCRIBE",
        )  # fmt: skip
    ),
    http_row(
        "PROPFIND",
        "?x=%7B%20y&z&a=b+c&_=1",
        _J,
        None,
        "key",
        404,
        _JSON_UTF8,
        _not_found("PROPFIND", "?x=%7B%20y&z&a=b+c&_=1"),
        id="PROPFIND, a query string",
    ),
    *(
        http_row(m, "", h, b, c, s, _JSON_UTF8, a, id=f"{m} {i}")
        for m, h, b, c, s, a, i in (
            ("PUT", (*_J, *_P), _TYPENAME, "key", 404, _not_found("PUT", ""), "a query"),
            ("DELETE", _J, _TYPENAME, "none", 404, _not_found("DELETE", ""), "a query"),
            ("PUT", (), _TYPENAME, "key", 404, _not_found("PUT", ""), "no type"),
            (
                "PATCH",
                (("Content-Type", "application/x-www-form-urlencoded"),),
                b"query=%7B%7D",
                "key",
                404,
                _not_found("PATCH", ""),
                "a form",
            ),
            (
                "DELETE",
                (("Content-Type", "text/plain"),),
                b"",
                "key",
                404,
                _not_found("DELETE", ""),
                "text/plain",
            ),
            ("PUT", (*_J, *_P), b"{", "key", 400, _NOT_JSON, "{"),
            ("PATCH", (*_J, *_P), None, "key", 400, _EMPTY, "empty"),
            ("PUT", (("Content-Type", "a/b/c"),), _TYPENAME, "key", 415, _UNSUPPORTED, "a/b/c"),
            ("PUT", (("Content-Type", ""),), _TYPENAME, "key", 415, _UNSUPPORTED, "an empty type"),
            ("PUT", (("Content-Type", "text/plain"),), b"\xe9", "key", 400, _SIZE_MISMATCH, "0xE9"),
            (
                "PUT",
                (("Content-Type", "text/plain"),),
                _PAST_THE_LIMIT,
                "key",
                413,
                _TOO_LARGE,
                "past the limit",
            ),
        )
    ),
    # A POST's content type: a 415 for one Fastify does not read; CSRF for one a browser sends
    # unpreflighted; the CSRF check ahead of the credential.
    *(
        http_row("POST", "", (("Content-Type", t), *h), b, c, s, _JSON_UTF8, a, id=f"{t!r}{i}")
        for t, h, b, c, s, a, i in (
            ("text/plain", (), _TYPENAME, "key", 400, _CSRF, ""),
            ("text/plain", (), _TYPENAME, "none", 400, _CSRF, ", none"),
            ("text/plain", _P, _TYPENAME, "key", 400, _NO_KEYS, ", preflight"),
            ("text/plain", _P, b"\xe9", "key", 400, _SIZE_MISMATCH, ", preflight, 0xE9"),
            ("text/plain", (), b"\xe9", "key", 400, _SIZE_MISMATCH, ", 0xE9"),
            ("text/plain; charset=latin1", _P, b"\xe9", "key", 400, _SIZE_MISMATCH, ", 0xE9"),
            ("text/plain", _P, "\u00e9".encode(), "key", 400, _NO_KEYS, ", preflight, U+00E9"),
            (
                "application/x-www-form-urlencoded",
                _P,
                b"query=%7B__typename%7D&x=\xe9",
                "key",
                200,
                b'{"data":{"__typename":"Query"}}',
                ", preflight, 0xE9",
            ),
            ("TEXT/PLAIN;X=Y", (), _TYPENAME, "key", 400, _CSRF, ""),
            ("application/x-www-form-urlencoded", (), _TYPENAME, "key", 400, _CSRF, ""),
            ("", (), _TYPENAME, "key", 415, _UNSUPPORTED, ""),
            ("foo", (), _TYPENAME, "key", 415, _UNSUPPORTED, ""),
            ("text /plain", (), _TYPENAME, "key", 415, _UNSUPPORTED, ""),
            ("multipart/form-data; boundary=x", (), _TYPENAME, "key", 415, _UNSUPPORTED, ""),
            ("application/graphql", (), _TYPENAME, "key", 415, _UNSUPPORTED, ""),
            (
                "application/graphql",
                (),
                _PAST_THE_LIMIT,
                "key",
                415,
                _UNSUPPORTED,
                ", past the limit",
            ),
            ("application/json ,", (), _TYPENAME, "key", 415, _UNSUPPORTED, ""),
            ("text/plain", (), _PAST_THE_LIMIT, "key", 413, _TOO_LARGE, ", past the limit"),
            (
                "text/plain",
                _P,
                b"\xe9" * 2**20 + b"\xe9",
                "key",
                413,
                _TOO_LARGE,
                ", past the limit, 0xE9",
            ),
            (
                "application/x-www-form-urlencoded",
                (),
                _PAST_THE_LIMIT,
                "key",
                413,
                _TOO_LARGE,
                ", past the limit",
            ),
            ("application/json;", (), _TYPENAME, "key", 200, _TYPENAME_DATA, ""),
            ("application/json\t;", (), _TYPENAME, "key", 200, _TYPENAME_DATA, ""),
            ("Application/JSON", (), _TYPENAME, "key", 200, _TYPENAME_DATA, ""),
            (
                "application/x-www-form-urlencoded",
                _P,
                b"query=%7B%20__typename%20%7D",
                "key",
                200,
                _TYPENAME_DATA,
                ", a form",
            ),
            (
                "application/x-www-form-urlencoded",
                _P,
                b"query=%7B%20__typename%20%7D&query=x",
                "key",
                400,
                _apollo(b"GraphQL queries must be strings."),
                ", query twice",
            ),
            (
                "application/x-www-form-urlencoded",
                _P,
                b"query=%7B%20__typename%20%7D&variables=%7B%7D",
                "key",
                400,
                _apollo(
                    b"`variables` in a POST body should be provided as an object, not a recursively JSON-encoded string."
                ),
                ", variables",
            ),
            ("application/x-www-form-urlencoded", _P, b"", "key", 400, _NO_KEYS, ", empty"),
            ("text/plain", _P, b"", "key", 400, _NO_KEYS, ", empty"),
        )
    ),
    http_row("POST", "", (), _TYPENAME, "key", 415, _JSON_UTF8, _UNSUPPORTED, id="no type"),
    http_row(
        "POST", "", _P, _TYPENAME, "key", 415, _JSON_UTF8, _UNSUPPORTED, id="no type, preflight"
    ),
    http_row("POST", "", (), b"", "key", 400, _JSON_UTF8, _CSRF, id="no type, empty"),
    http_row("POST", "", _P, b"", "key", 400, _JSON_UTF8, _NO_KEYS, id="no type, empty, preflight"),
    # Apollo Server's page for a browser, ahead of the CSRF check and the credential.
    *(
        http_row("GET", t, (*h, ("Accept", a)), None, c, s, ty, e, id=f"GET, Accept {a!r}{i}")
        for t, h, a, c, s, ty, e, i in (
            ("", (), "text/html", "none", 200, "text/html", _LANDING_PAGE, ", none"),
            (_Q, _P, "text/html,application/json", "key", 200, "text/html", _LANDING_PAGE, ""),
            (_Q, _P, "text/*", "key", 200, "text/html", _LANDING_PAGE, ""),
            (_Q, _P, "text/html;q=0.5;charset=utf-8", "key", 200, "text/html", _LANDING_PAGE, ""),
            (
                _Q,
                _P,
                "application/json;q=0.5, text/html;q=0.6",
                "key",
                200,
                "text/html",
                _LANDING_PAGE,
                "",
            ),
            (_Q, _P, "text/html;level=1", "key", 406, _JSON_UTF8, _NOT_ACCEPTABLE, ""),
            (_Q, _P, "text/html;q", "key", 406, _JSON_UTF8, _NOT_ACCEPTABLE, ""),
            (
                _Q,
                _P,
                "text/html;q=0.9, application/json",
                "key",
                200,
                _JSON_UTF8,
                _TYPENAME_DATA,
                "",
            ),
        )
    ),
    # A GET, its content type and its query string read after the credential.
    http_row("GET", _Q, _P, None, "key", 200, _JSON_UTF8, _TYPENAME_DATA, id="GET"),
    http_row("GET", _Q, (), None, "key", 400, _JSON_UTF8, _CSRF, id="GET, unpreflighted"),
    http_row("GET", _Q, _J, None, "key", 200, _JSON_UTF8, _TYPENAME_DATA, id="GET, JSON"),
    *(
        http_row(
            "GET", _Q, (("Content-Type", t), *h), None, c, s, _JSON_UTF8, a, id=f"GET, {t!r}{i}"
        )
        for t, h, c, s, a, i in (
            ("text/plain", _P, "key", 415, _GET_CONTENT_TYPE, ""),
            ("foo", (), "key", 415, _GET_CONTENT_TYPE, ""),
            ("foo", _P, "none", 500, _BAD_KEY, ", none"),
            ("application/graphql-response+json", _P, "key", 415, _GET_CONTENT_TYPE, ""),
            ("", _P, "key", 200, _TYPENAME_DATA, ""),
            ("application/json; charset=latin1", _P, "key", 200, _TYPENAME_DATA, ""),
        )
    ),
    http_row(
        "GET",
        "?query=%7B&query=%7B",
        (("Content-Type", "text/plain"), *_P),
        None,
        "key",
        415,
        _JSON_UTF8,
        _GET_CONTENT_TYPE,
        id="GET, text/plain, query twice",
    ),
    *(
        http_row("GET", t, _P, None, "key", 400, _JSON_UTF8, _apollo(m), id=i)
        for t, m, i in (
            (
                _Q + "&query=",
                b"The 'query' search parameter may only be specified once.",
                "query twice",
            ),
            (
                _Q + "&variables=nope",
                b"The variables search parameter contains invalid JSON.",
                "variables nope",
            ),
            (
                _Q + "&variables=null",
                b"The variables search parameter should contain a JSON-encoded object.",
                "variables null",
            ),
            (
                "?query=%7B&query=%7B&variables=nope",
                b"The 'query' search parameter may only be specified once.",
                "query twice, variables nope",
            ),
            (
                "?operationName=A&operationName=B&variables=nope",
                b"The 'operationName' search parameter may only be specified once.",
                "operationName twice, variables nope",
            ),
            (
                "?variables=nope&extensions=5",
                b"The variables search parameter contains invalid JSON.",
                "variables nope, extensions 5",
            ),
        )
    ),
    *(
        http_row(
            "GET",
            f"?query={op}%20%7B%20__typename%20%7D",
            _P,
            None,
            c,
            s,
            _JSON_UTF8,
            a,
            id=f"GET {op}, {c}",
            **h,
        )
        for op, c, s, a, h in (
            (
                "mutation",
                "key",
                405,
                _apollo(
                    b"GET requests only support query operations, not mutation operations",
                    status=405,
                ),
                {"answered": {"allow": "POST"}},
            ),
            (
                "subscription",
                "key",
                405,
                _apollo(
                    b"GET requests only support query operations, not subscription operations",
                    status=405,
                ),
                {"answered": {"allow": "POST"}},
            ),
            ("mutation", "none", 500, _BAD_KEY, {}),
        )
    ),
    http_row(
        "HEAD",
        _Q,
        _P,
        None,
        "key",
        405,
        _JSON_UTF8,
        b"",
        id="HEAD",
        answered={"allow": "GET, POST"},
    ),
    http_row(
        "HEAD",
        _Q,
        (*_P, ("Accept", "text/html")),
        None,
        "key",
        405,
        _JSON_UTF8,
        b"",
        id="HEAD, Accept text/html",
    ),
    http_row("HEAD", _Q, _P, None, "none", 500, _JSON_UTF8, b"", id="HEAD, none"),
    http_row(
        "HEAD",
        _Q,
        (("Content-Type", "text/plain"),),
        None,
        "key",
        400,
        _JSON_UTF8,
        b"",
        id="HEAD, text/plain",
    ),
    # `apollo_http.response_type` and its 406, the credential's 500 among the thrown refusals.
    *(
        http_row("POST", "", (*_J, ("Accept", a)), b, c, s, t, e, id=f"Accept {a!r}{i}")
        for a, b, c, s, t, e, i in (
            ("text/html", _TYPENAME, "key", 406, _JSON_UTF8, _NOT_ACCEPTABLE, ""),
            ("application/json;q=0", _TYPENAME, "key", 406, _JSON_UTF8, _NOT_ACCEPTABLE, ""),
            ("application/json;q", _TYPENAME, "key", 406, _JSON_UTF8, _NOT_ACCEPTABLE, ""),
            (
                "application/graphql-response+json",
                _TYPENAME,
                "key",
                200,
                _GRAPHQL_RESPONSE,
                _TYPENAME_DATA,
                "",
            ),
            (
                "application/json; callbackSpec=1.0",
                _TYPENAME,
                "key",
                200,
                "application/json; callbackSpec=1.0; charset=utf-8",
                _TYPENAME_DATA,
                "",
            ),
            ("text/html", b"{}", "key", 400, _JSON_UTF8, _NO_KEYS, ", {}"),
            (
                "application/graphql-response+json",
                b"{}",
                "key",
                400,
                _GRAPHQL_RESPONSE,
                _NO_KEYS,
                ", {}",
            ),
            (
                "text/html",
                b'{"query":"{"}',
                "key",
                406,
                _JSON_UTF8,
                _NOT_ACCEPTABLE,
                ", a parse error",
            ),
            ("text/html", _TYPENAME, "none", 500, _JSON_UTF8, _BAD_KEY, ", none"),
            (
                "application/graphql-response+json",
                _TYPENAME,
                "none",
                500,
                _GRAPHQL_RESPONSE,
                _BAD_KEY,
                ", none",
            ),
            ("text/html", b"{", "key", 400, _JSON_UTF8, _NOT_JSON, ", {"),
        )
    ),
    http_row(
        "POST",
        "",
        (("Content-Type", "text/plain"), ("Accept", "application/graphql-response+json")),
        _TYPENAME,
        "key",
        400,
        _GRAPHQL_RESPONSE,
        _CSRF,
        id="text/plain, Accept graphql-response",
    ),
    http_row(
        "PUT",
        "",
        (*_J, ("Accept", "application/graphql-response+json")),
        _TYPENAME,
        "key",
        404,
        _JSON_UTF8,
        _not_found("PUT", ""),
        id="PUT, Accept graphql-response",
    ),
    # Fastify reads the body of a DELETE before its 404, and leaves a type it has no parser for
    # unread there.
    http_row("DELETE", "", (*_J, *_P), b"{", "key", 400, _JSON_UTF8, _NOT_JSON, id="DELETE {"),
    http_row(
        "PUT",
        "",
        (("Content-Type", "application/graphql"), *_P),
        _TYPENAME,
        "key",
        404,
        _JSON_UTF8,
        _not_found("PUT", ""),
        id="PUT application/graphql",
    ),
    http_row(
        "OPTIONS",
        "",
        (
            *_PREFLIGHT_ASKED,
            ("Access-Control-Request-Headers", "a"),
            ("Access-Control-Request-Headers", "b"),
        ),
        None,
        "key",
        204,
        None,
        b"",
        id="asked twice",
        answered={**_FIREFLIES_PREFLIGHT, "access-control-allow-headers": "a, b"},
    ),
    *(
        http_row(
            "OPTIONS",
            "",
            (*_PREFLIGHT_ASKED, ("Access-Control-Request-Headers", asked)),
            None,
            "key",
            204,
            None,
            b"",
            id=f"asked {asked!r}",
            answered={**_FIREFLIES_PREFLIGHT, "access-control-allow-headers": allowed},
        )
        for asked, allowed in (
            ("a\xa0b", "a\xa0b"),
            ("a b", "a b"),
            ("a\x85b", "a\x85b"),
            ("a\tb", "a\tb"),
            ("a\xa0", "a\xa0"),
            ("\xa0a", "\xa0a"),
            ("a\x85", "a\x85"),
            ("a\t", "a"),
            ("a,  ,b", "a,  ,b"),
            ("A-B, c_d", "A-B, c_d"),
        )
    ),
    # Copies of a header, read as `apollo_http.headers_seen` reads them.
    *(
        http_row("POST", "", h, _TYPENAME, "key", s, _JSON_UTF8, a, id=i)
        for h, s, a, i in (
            (
                (*_J, ("Accept", "application/json"), ("Accept", "text/html")),
                200,
                _TYPENAME_DATA,
                "Accept json, html",
            ),
            (
                (*_J, ("Accept", "text/html"), ("Accept", "application/json")),
                200,
                _TYPENAME_DATA,
                "Accept html, json",
            ),
            (
                (*_J, ("Content-Type", "text/plain")),
                200,
                _TYPENAME_DATA,
                "Content-Type json, text/plain",
            ),
            ((("Content-Type", "text/plain"), *_J), 400, _CSRF, "Content-Type text/plain, json"),
            ((*_J, ("Content-Type", "")), 200, _TYPENAME_DATA, "Content-Type json, empty"),
            ((("Content-Type", ""), *_J), 415, _UNSUPPORTED, "Content-Type empty, json"),
            ((*_J, *_J), 200, _TYPENAME_DATA, "Content-Type json twice"),
            (
                (("Content-Type", "text/plain"), ("x-apollo-operation-name", " ")),
                400,
                _CSRF,
                "operation name a space",
            ),
        )
    ),
    *(
        http_row("GET", _Q, h, None, "key", 200, _JSON_UTF8, _TYPENAME_DATA, id=i)
        for h, i in (
            ((("apollo-require-preflight", ""), *_P), "preflight empty, 1"),
            ((*_P, ("apollo-require-preflight", "")), "preflight 1, empty"),
        )
    ),
    # Each method under a JSON type, with no body and no length.
    *(
        http_row(
            m,
            t,
            (("Content-Type", "application/json; charset=latin1"),),
            None,
            "key",
            s,
            ty,
            a,
            id=f"{m}, latin1, no body",
        )
        for m, t, s, ty, a in (
            ("POST", "", 400, _JSON_UTF8, _EMPTY),
            ("PUT", "", 400, _JSON_UTF8, _EMPTY),
            ("PATCH", "", 400, _JSON_UTF8, _EMPTY),
            ("DELETE", "", 400, _JSON_UTF8, _EMPTY),
            ("OPTIONS", "", 400, "text/plain", b"Invalid Preflight Request"),
            ("PROPFIND", "", 404, _JSON_UTF8, _not_found("PROPFIND", "")),
            (
                "GET",
                "",
                400,
                _JSON_UTF8,
                _apollo(
                    b"GraphQL operations must contain a non-empty `query` or a `persistedQuery` extension."
                ),
            ),
            ("HEAD", "", 405, _JSON_UTF8, b""),
        )
    ),
    http_row("POST", "", (), None, "key", 400, _JSON_UTF8, _CSRF, id="no type, no body"),
    http_row(
        "POST", "", _P, None, "key", 400, _JSON_UTF8, _NO_KEYS, id="no type, no body, preflight"
    ),
    http_row(
        "POST",
        "",
        _P,
        Chunked(_TYPENAME),
        "key",
        415,
        _JSON_UTF8,
        _UNSUPPORTED,
        id="no type, chunked, preflight",
    ),
    http_row(
        "POST",
        "",
        _J,
        Chunked(_TYPENAME),
        "key",
        200,
        _JSON_UTF8,
        _TYPENAME_DATA,
        id="JSON, chunked",
    ),
    http_row(
        "GET",
        "?query=%7B&query=%7B",
        (),
        None,
        "key",
        400,
        _JSON_UTF8,
        _CSRF,
        id="GET, unpreflighted, query twice",
    ),
    http_row(
        "GET",
        _Q + "&variables=%7B%7D&operationName=",
        _P,
        None,
        "key",
        400,
        _JSON_UTF8,
        _apollo(b'Unknown operation named \\"\\".', b"OPERATION_RESOLUTION_FAILURE"),
        id="GET, variables {}, operationName empty",
    ),
    *(
        http_row(
            m,
            t,
            (*h, ("Origin", "https://example.com")),
            b,
            "key",
            s,
            _JSON_UTF8,
            a,
            id=f"{m}, Origin",
        )
        for m, t, h, b, s, a in (
            ("POST", "", _J, _TYPENAME, 200, _TYPENAME_DATA),
            ("GET", _Q, _P, None, 200, _TYPENAME_DATA),
            ("HEAD", _Q, _P, None, 405, b""),
            ("PUT", "", _J, _TYPENAME, 404, _not_found("PUT", "")),
        )
    ),
]
_FIREFLIES_CREDENTIALS = {
    "none": None,
    "Bearer nope": "Bearer nope",
    "key": "Bearer {t}",
    "twice": "Bearer {t}",
}


def _fireflies_body(r, answer):
    """``r``'s body, or `_LANDING_PAGE` where ``answer`` is and ``r`` is Apollo Server's page: one
    nonce of 64 hex digits, 3,522 bytes and its title."""
    if answer is not _LANDING_PAGE:
        return r.content
    page = r.text
    nonces = set(re.findall(r"nonce-([0-9a-f]+)", page))
    whole = len(page.encode()) == 3522 and "<title>Apollo Server</title>" in page
    return _LANDING_PAGE if len(nonces) == 1 and len(nonces.pop()) == 64 and whole else r.content


def test_fireflies_answers_each_method_type_and_accept_as_fireflies_does(client, tokens_yaml):
    """Each row of `_FIREFLIES_HTTP` is answered as api.fireflies.ai answered it."""
    answered, expected = {}, {}
    for row in _FIREFLIES_HTTP:
        method, target, headers, body, credential, status, media_type, answer, named = row.values
        sent = list(headers)
        if _FIREFLIES_CREDENTIALS[credential] is not None:
            value = _FIREFLIES_CREDENTIALS[credential].format(t=tokens_yaml["admin_token"])
            sent += [("Authorization", value)] * (2 if credential == "twice" else 1)
        content = body.chunks() if isinstance(body, Chunked) else body
        r = sent_as(client, method, f"/fireflies/graphql{target}", sent, content=content, bare=True)
        answered[row.id] = (
            r.status_code,
            r.headers.get("content-type"),
            _fireflies_body(r, answer),
            {name: r.headers.get(name) for name in named},
        )
        expected[row.id] = (status, media_type, answer, named)
    assert len(expected) == len(_FIREFLIES_HTTP)
    assert answered == expected
