"""Vendor-agnostic GraphQL execution: SDL + resolvers -> response envelope.

Knows nothing about any vendor and nothing about :mod:`backlot.store`. A vendor builds one
:class:`Engine` at import time from its SDL and resolver map; its router reads the request body
with :mod:`backlot.graphql.body` and hands it to :meth:`Engine.execute_payload`, along with a
context dict (connection, caller, ``visible_ids``) that every resolver receives as
``info.context``.

Execution is deliberately split into parse -> validate -> execute rather than delegating to
``graphql_sync``, because the three stages produce *different* envelopes. Per the GraphQL
spec, a **request error** (malformed document, failed validation, uncoercible variables) is
detected before execution begins and the response carries no ``data`` entry at all, while a
**field error** raised mid-execution returns partial ``data`` alongside ``errors``. Real
servers make that distinction and clients key off it, so Backlot does too —
``ExecutionResult.formatted`` alone would emit ``"data": null`` for every request error.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from graphql import (
    GraphQLError,
    GraphQLID,
    GraphQLInputObjectType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLScalarType,
    GraphQLSchema,
    ValueNode,
    assert_valid_schema,
    build_schema,
    execute_sync,
    get_operation_ast,
    parse,
    type_from_ast,
    validate,
)

from backlot.graphql import body as request_body

Resolver = Callable[..., Any]


def _ids_as_graphql_js_reads_them(value: Any, type_: Any) -> Any:
    """``value`` for a variable of ``type_``, each :class:`~backlot.graphql.body.Double` holding an
    integer where ``type_`` holds an ``ID`` turned into the ID graphql-js makes of it with
    ``Number::toString``, where graphql-core's ``coerce_id`` writes ``str(int(value))``. graphql
    16.9.0's ``GraphQLID.parseValue`` under Node 24.18.0 read ``1e21``, ``1e300`` and
    ``12345678901234567890`` as ``1e+21``, ``1e+300`` and ``12345678901234567000`` on 2026-10-19
    (UTC), where ``coerce_id`` writes the integer the double holds, digit for digit. graphql
    16.9.0's ``graphqlSync`` read ``1e21`` so as an ``[ID!]`` variable, alone or in a list, and in
    an input object's ``ID`` and ``[ID!]`` fields on 2026-10-20 (UTC).

    They are read here, before graphql-core coerces them, because graphql-core has one ``ID`` scalar
    for every schema in a process, and a process that imports :data:`backlot.main.app` can hold a
    schema of its own."""
    if isinstance(type_, GraphQLNonNull):
        return _ids_as_graphql_js_reads_them(value, type_.of_type)
    if isinstance(type_, GraphQLList):
        if isinstance(value, list):
            return [_ids_as_graphql_js_reads_them(item, type_.of_type) for item in value]
        return _ids_as_graphql_js_reads_them(value, type_.of_type)
    if isinstance(type_, GraphQLInputObjectType):
        if not isinstance(value, dict):
            return value
        fields = type_.fields
        return {
            key: _ids_as_graphql_js_reads_them(item, fields[key].type) if key in fields else item
            for key, item in value.items()
        }
    if type_ is GraphQLID and type(value) is request_body.Double and value.is_integer():
        return repr(value)
    return value


# One custom scalar's coercion: ``parse_value`` takes a variable's JSON value, ``parse_literal`` an
# AST node. graphql-core runs both BEFORE any resolver, so a scalar that rejects an operand turns a
# malformed filter value into a validation error naming the scalar — the envelope the real servers
# produce — rather than into a field error from whichever resolver first tried to use it.
Scalar = Mapping[str, Callable[..., Any]]


@dataclass(frozen=True)
class Result:
    """A GraphQL response body plus how it should be surfaced over HTTP.

    ``request_error`` marks the pre-execution failures above; routers map it to the status
    code their vendor uses (real Linear answers a malformed document with 400, while a
    field error is still a 200 with ``errors``). ``raised`` marks a field error that is Backlot's
    own failure rather than a refusal; :func:`_raised` tells the two apart.
    """

    payload: dict
    request_error: bool = False
    raised: bool = False


def _marking_refusals(fn: Resolver) -> Resolver:
    """The resolver ``fn``, marking each ``GraphQLError`` it raises as a refusal it meant."""

    @functools.wraps(fn)
    def resolve(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except GraphQLError as error:
            error.refusal = True
            raise

    return resolve


def _raised(error: GraphQLError) -> bool:
    """Whether the field error ``error`` is Backlot's own failure: an exception a resolver raised
    that is not a ``GraphQLError``, or one graphql-core raised completing the value a resolver
    returned, such as a null for a non-null field, a non-list for a list or a value its scalar
    cannot serialize. A ``GraphQLError`` a resolver raises (:func:`_marking_refusals` marks it) is a
    refusal, and so is graphql-core's refusal of an argument the request sent, which it makes as it
    runs the field, such as a null variable, declared with a default, where the field or a directive
    on it takes a non-null argument. graphql-core gives that refusal the argument's value as its
    node."""
    original = error.original_error
    if getattr(error, "refusal", False) or getattr(original, "refusal", False):
        return False
    return not (
        isinstance(original, GraphQLError)
        and original.nodes
        and all(isinstance(node, ValueNode) for node in original.nodes)
    )


def _request_error(errors: list[GraphQLError]) -> Result:
    return Result(payload={"errors": [e.formatted for e in errors]}, request_error=True)


def _client_error(message: str) -> Result:
    return _request_error([GraphQLError(message)])


def not_json() -> Result:
    """The request error for a body that is not JSON, in graphql-js's words."""
    return _client_error("POST body sent invalid JSON.")


def from_sdl(module_file: str, name: str, resolvers, scalars=None) -> "Engine":
    """An :class:`Engine` over the ``<name>.graphql`` sitting beside ``module_file``.

    Each vendor's resolver module keeps its SDL as a sibling file, so this is the one place that
    knows how the two are paired."""
    return Engine(
        (Path(module_file).parent / f"{name}.graphql").read_text(encoding="utf-8"),
        resolvers,
        scalars,
    )


class Engine:
    """An executable schema: SDL text bound to a ``{type: {field: resolver}}`` map.

    Resolvers use graphql-core's signature, ``fn(source, info, **arguments)``. Fields left
    unbound fall through to the default resolver, which reads a key off the parent when it
    is a mapping — so a resolver can simply return dicts and let the selection set decide
    what is emitted.
    """

    def __init__(
        self,
        sdl: str,
        resolvers: Mapping[str, Mapping[str, Resolver]] | None = None,
        scalars: Mapping[str, Scalar] | None = None,
    ):
        self.schema: GraphQLSchema = build_schema(sdl)
        # validate() would do this on the first request otherwise, surfacing a vendor's broken
        # SDL as a 500 rather than as an import-time failure.
        assert_valid_schema(self.schema)
        for scalar_name, coercion in (scalars or {}).items():
            scalar = self.schema.type_map.get(scalar_name)
            # Same rule as an unknown resolver target below: a misspelt scalar would leave the SDL's
            # pass-through coercion in place and the operand check silently absent.
            if not isinstance(scalar, GraphQLScalarType):
                raise ValueError(f"scalar map references unknown scalar {scalar_name}")
            for hook, fn in coercion.items():
                if hook not in ("serialize", "parse_value", "parse_literal"):
                    raise ValueError(f"scalar {scalar_name} has no coercion hook {hook!r}")
                setattr(scalar, hook, fn)
        for type_name, fields in (resolvers or {}).items():
            gql_type = self.schema.type_map.get(type_name)
            # A typo in a resolver map would otherwise bind nothing and fail silently at
            # request time, so refuse to build the schema at all.
            if gql_type is None or not hasattr(gql_type, "fields"):
                raise ValueError(f"resolver map references unknown type {type_name}")
            for field_name, fn in fields.items():
                field = gql_type.fields.get(field_name)
                if field is None:
                    raise ValueError(
                        f"resolver map references unknown field {type_name}.{field_name}"
                    )
                field.resolve = _marking_refusals(fn)

    def execute(
        self,
        document: str,
        *,
        variables: dict | None = None,
        operation_name: str | None = None,
        context: Any = None,
    ) -> Result:
        try:
            doc = parse(document)
        except GraphQLError as exc:  # GraphQLSyntaxError
            return _request_error([exc])
        errors = validate(self.schema, doc)
        if errors:
            return _request_error(list(errors))
        operation = get_operation_ast(doc, operation_name)
        if variables and operation is not None:
            types = {
                definition.variable.name.value: type_from_ast(self.schema, definition.type)
                for definition in operation.variable_definitions
            }
            variables = {
                name: _ids_as_graphql_js_reads_them(value, types.get(name))
                for name, value in variables.items()
            }
        result = execute_sync(
            self.schema,
            doc,
            context_value=context,
            variable_values=variables,
            operation_name=operation_name,
        )
        # Variable coercion and operation selection fail *inside* execute_sync but still
        # before any field runs. Those errors carry no ``path`` (nothing was resolved), which
        # is what separates them from a non-null field error that propagated up to the root.
        if result.data is None and result.errors and all(e.path is None for e in result.errors):
            return _request_error(list(result.errors))
        # graphql-core's ExecutionResult.formatted writes "data" first; api.fireflies.ai and
        # api.linear.app open a response carrying a field error with "errors" (2026-10-21), so flip
        # the two members here.
        return Result(
            payload=dict(reversed(list(result.formatted.items()))),
            raised=any(_raised(e) for e in result.errors or ()),
        )

    def execute_payload(self, payload: Any, *, context: Any = None) -> Result:
        """Run a GraphQL-over-HTTP POST body (``{query, variables, operationName}``) its router has
        read with :mod:`backlot.graphql.body`. A body of the wrong shape comes back as a GraphQL
        error envelope in graphql-js's words rather than a framework-level validation error."""
        if not isinstance(payload, dict):
            return not_json()
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            return _client_error("Must provide query string.")
        variables = payload.get("variables")
        if variables is not None and not isinstance(variables, dict):
            return _client_error("Variables are invalid JSON.")
        operation_name = payload.get("operationName")
        if operation_name is not None and not isinstance(operation_name, str):
            return _client_error("Must provide operation name as a string.")
        return self.execute(
            query, variables=variables, operation_name=operation_name, context=context
        )
