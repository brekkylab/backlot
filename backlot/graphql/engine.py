"""Vendor-agnostic GraphQL execution: SDL + resolvers -> response envelope.

Knows nothing about any vendor and nothing about :mod:`backlot.store`. A vendor builds one
:class:`Engine` at import time from its SDL and resolver map; its router hands raw request
bodies to :meth:`Engine.execute_request` along with a context dict (connection, caller,
``visible_ids``) that every resolver receives as ``info.context``.

Execution is deliberately split into parse -> validate -> execute rather than delegating to
``graphql_sync``, because the three stages produce *different* envelopes. Per the GraphQL
spec, a **request error** (malformed document, failed validation, uncoercible variables) is
detected before execution begins and the response carries no ``data`` entry at all, while a
**field error** raised mid-execution returns partial ``data`` alongside ``errors``. Real
servers make that distinction and clients key off it, so the mock does too —
``ExecutionResult.formatted`` alone would emit ``"data": null`` for every request error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from graphql import (
    GraphQLError,
    GraphQLSchema,
    assert_valid_schema,
    build_schema,
    execute_sync,
    parse,
    validate,
)

Resolver = Callable[..., Any]


@dataclass(frozen=True)
class Result:
    """A GraphQL response body plus how it should be surfaced over HTTP.

    ``request_error`` marks the pre-execution failures above; routers map it to the status
    code their vendor uses (real Linear answers a malformed document with 400, while a
    field error is still a 200 with ``errors``).
    """

    payload: dict
    request_error: bool = False


def _request_error(errors: list[GraphQLError]) -> Result:
    return Result(payload={"errors": [e.formatted for e in errors]}, request_error=True)


def _client_error(message: str) -> Result:
    return _request_error([GraphQLError(message)])


def from_sdl(module_file: str, name: str, resolvers) -> "Engine":
    """An :class:`Engine` over the ``<name>.graphql`` sitting beside ``module_file``.

    Each vendor's resolver module keeps its SDL as a sibling file, so this is the one place that
    knows how the two are paired."""
    return Engine((Path(module_file).parent / f"{name}.graphql").read_text(), resolvers)


class Engine:
    """An executable schema: SDL text bound to a ``{type: {field: resolver}}`` map.

    Resolvers use graphql-core's signature, ``fn(source, info, **arguments)``. Fields left
    unbound fall through to the default resolver, which reads a key off the parent when it
    is a mapping — so a resolver can simply return dicts and let the selection set decide
    what is emitted.
    """

    def __init__(self, sdl: str, resolvers: Mapping[str, Mapping[str, Resolver]] | None = None):
        self.schema: GraphQLSchema = build_schema(sdl)
        # validate() would do this on the first request otherwise, surfacing a vendor's broken
        # SDL as a 500 rather than as an import-time failure.
        assert_valid_schema(self.schema)
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
                field.resolve = fn

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
        return Result(payload=result.formatted)

    def execute_request(self, body: bytes | str, *, context: Any = None) -> Result:
        """Run a GraphQL-over-HTTP POST body (``{query, variables, operationName}``).

        Body problems come back as a GraphQL error envelope rather than a framework-level
        validation error, because that is what a real GraphQL server returns and what
        generated clients parse. Messages match graphql-js, which is what the vendors run.
        """
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _client_error("POST body sent invalid JSON.")
        if not isinstance(payload, dict):
            return _client_error("POST body sent invalid JSON.")
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
