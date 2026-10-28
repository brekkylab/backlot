"""Vendor-agnostic GraphQL execution: SDL + resolvers -> response envelope.

Knows nothing about any vendor and nothing about :mod:`backlot.store`. A vendor builds one
:class:`Engine` at import time from its SDL and resolver map; its router reads the request body
with :mod:`backlot.graphql.body` and hands it to :meth:`Engine.execute_payload`, along with a
context dict (connection, caller, ``visible_ids``) that every resolver receives as
``info.context``.

Both vendors serve GraphQL through Apollo Server over graphql-js, so the engine answers as that pair
does: Apollo Server's checks of the body and of a persisted query come first, then graphql-js's
parse, validation and choice of operation (:meth:`Engine.resolve_payload`), then the coercion of
variables and the run itself (:meth:`Engine.run`), each refusal a :class:`Refusal` carrying Apollo
Server's ``code`` and message. A router that reads the credential between the two stages calls them
apart. A refusal made before execution carries no ``data`` entry; an error made as the operation
runs comes back beside ``data``, at 200. Each router turns the refusals into its vendor's envelope.
"""

from __future__ import annotations

import functools
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from graphql import (
    DocumentNode,
    GraphQLError,
    GraphQLID,
    GraphQLInputObjectType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLSyntaxError,
    OperationDefinitionNode,
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
from backlot.graphql import js_words

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


#: Apollo Server's codes for the refusals it and graphql-js make, as api.fireflies.ai carried each
#: on 2026-10-25 (UTC); api.linear.app carried each but ``OPERATION_RESOLUTION_FAILURE``.
BAD_REQUEST = "BAD_REQUEST"
PARSE_FAILED = "GRAPHQL_PARSE_FAILED"
VALIDATION_FAILED = "GRAPHQL_VALIDATION_FAILED"
OPERATION_RESOLUTION_FAILURE = "OPERATION_RESOLUTION_FAILURE"
BAD_USER_INPUT = "BAD_USER_INPUT"
PERSISTED_QUERY_NOT_FOUND = "PERSISTED_QUERY_NOT_FOUND"
INTERNAL_SERVER_ERROR = "INTERNAL_SERVER_ERROR"

#: The errors graphql-js's ``validate`` takes, and graphql-core's: the next one is
#: ``Too many validation errors, error limit reached. Validation aborted.``, and the last.
MAX_VALIDATION_ERRORS = 100


@dataclass(frozen=True)
class Refusal:
    """One request error as Apollo Server answers it: ``message`` as Apollo Server writes it, in
    graphql-js's words where graphql-js made the refusal, the ``locations`` graphql-js gives it,
    Apollo Server's ``code``, the status of the answer and the ``headers`` it sets on it.
    ``thrown`` marks a refusal Apollo Server throws, which ``Accept`` cannot turn into its 406, and
    ``http_headers`` whether the ``http`` extension it carries names its ``headers``, as all but
    the 406's do."""

    message: str
    code: str
    locations: tuple = ()
    status: int = 400
    headers: tuple[tuple[str, str], ...] = ()
    thrown: bool = False
    http_headers: bool = True


@dataclass(frozen=True)
class Result:
    """A GraphQL response body plus how it should be surfaced over HTTP.

    ``refusals`` holds the request errors, made before execution, and ``payload`` then carries no
    ``data``; a router writes them in its vendor's envelope. ``assigned`` holds the index in
    ``payload["errors"]`` of each error graphql-core made as the operation ran (:meth:`Engine.run`).
    ``raised`` marks a field error that is Backlot's own failure rather than a refusal;
    :func:`_raised` tells the two apart.
    """

    payload: dict
    refusals: tuple[Refusal, ...] = ()
    raised: bool = False
    assigned: frozenset[int] = frozenset()

    @property
    def request_error(self) -> bool:
        return bool(self.refusals)

    @property
    def status(self) -> int:
        """The status of the answer: a refusal's, or 200 for an operation that ran."""
        return self.refusals[0].status if self.refusals else 200

    @property
    def headers(self) -> tuple[tuple[str, str], ...]:
        """The headers Apollo Server sets on the answer: a refusal's, or none."""
        return self.refusals[0].headers if self.refusals else ()


@dataclass(frozen=True)
class Operation:
    """What :meth:`Engine.resolve` makes of a request it goes on with: the parsed ``document``, the
    ``operation`` chosen from it, and the ``variables`` and operation ``name`` sent with it."""

    document: DocumentNode
    operation: OperationDefinitionNode
    variables: Any
    name: str | None


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
    on it takes a non-null argument, and its refusal of an operation of a type the schema does not
    have, such as a subscription to Fireflies. graphql-core gives the first the argument's value as
    its node, which the field error keeps, and the second the operation."""
    original = error.original_error
    if getattr(error, "refusal", False) or getattr(original, "refusal", False):
        return False
    return not (
        error.nodes
        and all(isinstance(node, (ValueNode, OperationDefinitionNode)) for node in error.nodes)
    )


def refused(*refusals: Refusal) -> Result:
    """The request errors ``refusals``, in Apollo Server's own envelope: ``message``, the
    ``locations`` where there are any, and ``extensions.code``."""
    errors = []
    for refusal in refusals:
        entry: dict = {"message": refusal.message}
        if refusal.locations:
            entry["locations"] = list(refusal.locations)
        entry["extensions"] = {"code": refusal.code}
        errors.append(entry)
    return Result(payload={"errors": errors}, refusals=refusals)


def _refusal(
    message: str, code: str = BAD_REQUEST, status: int = 400, thrown: bool = False
) -> Result:
    return refused(Refusal(message, code, status=status, thrown=thrown))


def _js_truthy(value: Any) -> bool:
    """Whether JavaScript reads the JSON value ``value`` as true: all but ``null``, ``false``,
    ``""`` and zero, the last of which ``false`` equals in Python."""
    return value not in (None, "", 0)


_NOT_A_STRING = "GraphQL queries must be strings."
# Apollo Server's longer refusal of a `query` that looks like a parsed document,
# `{"kind":"Document"}`: both vendors answered it on 2026-10-25 (UTC).
_A_DOCUMENT = (
    _NOT_A_STRING + " It looks like you're sending the internal graphql-js representation of a "
    "parsed query in your request instead of a request in the GraphQL query language. You can "
    "convert an AST to a string using the `print` function from `graphql`, or use a client like "
    "`apollo-client` which converts the internal representation to a string for you."
)
NO_QUERY = "GraphQL operations must contain a non-empty `query` or a `persistedQuery` extension."
NO_KEYS = "POST body missing, invalid Content-Type, or JSON object has no keys."
RECURSIVELY_ENCODED = (
    "`{}` in a POST body should be provided as an object, not a recursively JSON-encoded string."
)


def body_refusal(payload: Any) -> Result | None:
    """Apollo Server's refusal of a POST body ``payload`` read as JSON, or ``None`` for a body it
    goes on with. The checks run in this order, each first where two apply: an array, an empty
    object or a value that is not an object, a truthy `query` that is not a string, `variables` and
    then `extensions` as a string, then `extensions`, `variables` and `operationName` of the wrong
    type. Both vendors kept that order with two wrong at once on 2026-10-25 (UTC). A ``null``
    member is one not sent."""
    if isinstance(payload, list):
        return _refusal("Operation batching disabled.", thrown=True)
    if not isinstance(payload, dict) or not payload:
        return _refusal(NO_KEYS, thrown=True)
    query = payload.get("query")
    if _js_truthy(query) and not isinstance(query, str):
        document = isinstance(query, dict) and query.get("kind") == "Document"
        return _refusal(_A_DOCUMENT if document else _NOT_A_STRING, thrown=True)
    for name in ("variables", "extensions"):
        if isinstance(payload.get(name), str):
            return _refusal(RECURSIVELY_ENCODED.format(name), thrown=True)
    for name in ("extensions", "variables"):
        if payload.get(name) is not None and not isinstance(payload[name], dict):
            return _refusal(f"`{name}` in a POST body must be an object if provided.", thrown=True)
    name = payload.get("operationName")
    if name is not None and not isinstance(name, str):
        return _refusal("`operationName` in a POST body must be a string if provided.", thrown=True)
    return None


#: Apollo Server's refusals of a persisted query, as api.fireflies.ai answered them on 2026-10-25
#: (UTC).
PERSISTED_QUERY_VERSION = "Unsupported persisted query version"
PERSISTED_QUERY_MISMATCH = "provided sha does not match query"


def _persisted_query(payload: dict) -> Any:
    """The ``persistedQuery`` extension of ``payload``, or ``None`` where it sends none JavaScript
    reads as true."""
    extensions = payload.get("extensions")
    persisted = extensions.get("persistedQuery") if isinstance(extensions, dict) else None
    return persisted if _js_truthy(persisted) else None


def persisted_query_refusal(payload: dict) -> Result | None:
    """Apollo Server's answer to the ``persistedQuery`` extension of ``payload``, or ``None`` for
    a request it goes on with, as a server that keeps no query answers. A version other than the
    number 1 is refused first. Then a hash sent with no ``query``, or with ``null``, ``false`` or
    ``0``, is ``PersistedQueryNotFound`` at 200, as both vendors answered on 2026-10-25 (UTC); once
    a query had been sent with its hash and served, Linear answered its hash alone so to each of 16
    requests, and Fireflies to 15 of 16, serving the query to the other. Then a hash that is not the
    SHA-256 of the query sent, in lower-case hex, is refused before the query is parsed."""
    persisted = _persisted_query(payload)
    if persisted is None:
        return None
    version = persisted.get("version") if isinstance(persisted, dict) else None
    if isinstance(version, bool) or version != 1:
        return _refusal(PERSISTED_QUERY_VERSION, INTERNAL_SERVER_ERROR)
    query = payload.get("query")
    if not isinstance(query, str):
        return _refusal("PersistedQueryNotFound", PERSISTED_QUERY_NOT_FOUND, status=200)
    # Both vendors hashed a query holding U+D800 as the same query with U+FFFD in its place on
    # 2026-10-25 (UTC), the UTF-8 Node writes for a lone surrogate.
    units = query.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    if persisted.get("sha256Hash") != hashlib.sha256(units.encode("utf-8")).hexdigest():
        return _refusal(PERSISTED_QUERY_MISMATCH, INTERNAL_SERVER_ERROR)
    return None


def from_sdl(module_file: str, name: str, resolvers, scalars=None, roots=()) -> "Engine":
    """An :class:`Engine` over the ``<name>.graphql`` sitting beside ``module_file``.

    Each vendor's resolver module keeps its SDL as a sibling file, so this is the one place that
    knows how the two are paired."""
    return Engine(
        (Path(module_file).parent / f"{name}.graphql").read_text(encoding="utf-8"),
        resolvers,
        scalars,
        roots,
    )


def _with_roots(schema: GraphQLSchema, roots: tuple[str, ...]) -> GraphQLSchema:
    """``schema`` with each of ``roots``, ``Mutation`` or ``Subscription``, as a root type that
    has no field: the root types a vendor's schema has and its SDL leaves out because Backlot
    serves none of their fields. A document validated against it is refused a field of one as a
    field the type does not have, and ``__typename`` on one is answered with the type's name.
    graphql-core refuses a schema with an object type of no field, so this one is marked as
    checked; it is only validated against, and run for an operation on a root type the SDL leaves
    out."""
    shadow = GraphQLSchema(
        query=schema.query_type,
        mutation=GraphQLObjectType("Mutation", {}) if "Mutation" in roots else None,
        subscription=GraphQLObjectType("Subscription", {}) if "Subscription" in roots else None,
        types=list(schema.type_map.values()),
        directives=schema.directives,
    )
    shadow._validation_errors = []
    return shadow


class Engine:
    """An executable schema: SDL text bound to a ``{type: {field: resolver}}`` map.

    Resolvers use graphql-core's signature, ``fn(source, info, **arguments)``. Fields left
    unbound fall through to the default resolver, which reads a key off the parent when it
    is a mapping — so a resolver can simply return dicts and let the selection set decide
    what is emitted.

    ``roots`` names the root types :func:`_with_roots` adds.
    """

    def __init__(
        self,
        sdl: str,
        resolvers: Mapping[str, Mapping[str, Resolver]] | None = None,
        scalars: Mapping[str, Scalar] | None = None,
        roots: tuple[str, ...] = (),
    ):
        self.schema: GraphQLSchema = build_schema(sdl)
        # validate() would do this on the first request otherwise, surfacing a vendor's broken
        # SDL as a 500 rather than as an import-time failure.
        assert_valid_schema(self.schema)
        self._checked = _with_roots(self.schema, roots)
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

    def resolve(
        self,
        document: str,
        *,
        variables: Any = None,
        operation_name: str | None = None,
        on_document: Callable[[DocumentNode], Result | list[GraphQLError]] | None = None,
        get: bool = False,
    ) -> Result | Operation:
        """``document`` read as graphql-js reads it under Apollo Server, up to the choice of the
        operation to run: parse, then validation, then the choice of operation, each refused with
        Apollo Server's code; :meth:`run` takes the operation it chose. Sent by ``get``, an
        operation that is not a query is Apollo Server's 405.

        ``on_document`` is a validation rule of the vendor's that reports as graphql-js creates
        it: the :class:`Result` to answer with where it throws, or the errors it reports, which
        come ahead of graphql-js's own and count toward the :data:`MAX_VALIDATION_ERRORS` it
        takes."""
        try:
            doc = parse(document)
        except GraphQLSyntaxError as error:
            return refused(
                Refusal(js_words.requote(error.message), PARSE_FAILED, js_words.locations(error))
            )
        reported = [] if on_document is None else on_document(doc)
        if isinstance(reported, Result):
            return reported
        errors = validate(self._checked, doc, max_errors=MAX_VALIDATION_ERRORS - len(reported))
        if reported or errors:
            return refused(
                *(Refusal(e.message, VALIDATION_FAILED, js_words.locations(e)) for e in reported),
                *(
                    Refusal(js_words.requote(e.message), VALIDATION_FAILED, js_words.locations(e))
                    for e in errors
                ),
            )
        operation = get_operation_ast(doc, operation_name)
        if operation is None:
            message = (
                "Must provide operation name if query contains multiple operations."
                if operation_name is None
                else f'Unknown operation named "{operation_name}".'
            )
            return _refusal(message, OPERATION_RESOLUTION_FAILURE)
        kind = operation.operation.value
        if get and kind != "query":
            return refused(
                Refusal(
                    f"GET requests only support query operations, not {kind} operations",
                    BAD_REQUEST,
                    status=405,
                    headers=(("allow", "POST"),),
                )
            )
        return Operation(doc, operation, variables, operation_name)

    def resolve_payload(
        self,
        payload: Any,
        *,
        on_document: Callable[[DocumentNode], Result | list[GraphQLError]] | None = None,
        get: bool = False,
    ) -> Result | Operation:
        """A GraphQL-over-HTTP POST body (``{query, variables, operationName}``) its router has
        read with :mod:`backlot.graphql.body`, up to the choice of operation: :func:`body_refusal`,
        then :func:`persisted_query_refusal`, then Apollo Server's refusal of a body with no query
        and no ``persistedQuery`` extension, then :meth:`resolve`. A query whose hash a
        ``persistedQuery`` extension matches is parsed however empty it is, as both vendors parsed
        ``""`` on 2026-10-28 (UTC). With ``get``, ``payload`` is a GET's query string as
        :func:`backlot.graphql.apollo_http.search_payload` reads it, which no body check runs on."""
        answer = None if get else body_refusal(payload)
        if answer is None:
            answer = persisted_query_refusal(payload)
        if answer is not None:
            return answer
        query = payload.get("query")
        if not _js_truthy(query) and _persisted_query(payload) is None:
            return _refusal(NO_QUERY)
        return self.resolve(
            query,
            variables=payload.get("variables"),
            operation_name=payload.get("operationName"),
            on_document=on_document,
            get=get,
        )

    def run(self, operation: Operation, *, context: Any = None) -> Result:
        """Coerce the variables of ``operation``, refused with Apollo Server's code, and run it
        with ``context``."""
        variables = operation.variables
        coercion = js_words.coerce_variable_values(
            self.schema, list(operation.operation.variable_definitions), variables
        )
        # Apollo Server gives BAD_USER_INPUT to the refusal of a variable; the one saying there
        # were too many has no variable and keeps the code of an error with none.
        if coercion:
            return refused(
                *(
                    Refusal(
                        e.message,
                        BAD_USER_INPUT if e.nodes else INTERNAL_SERVER_ERROR,
                        js_words.locations(e),
                    )
                    for e in coercion
                )
            )
        if variables:
            types = {
                definition.variable.name.value: type_from_ast(self.schema, definition.type)
                for definition in operation.operation.variable_definitions
            }
            variables = {
                name: _ids_as_graphql_js_reads_them(value, types.get(name))
                for name, value in variables.items()
            }
        # A root type only `self._checked` has runs there, and one neither schema has is
        # graphql-core's refusal.
        on_a_root = getattr(self.schema, f"{operation.operation.operation.value}_type") is None
        result = execute_sync(
            self._checked if on_a_root else self.schema,
            operation.document,
            context_value=context,
            variable_values=variables,
            operation_name=operation.name,
        )
        # api.fireflies.ai and api.linear.app open a response carrying a field error with "errors"
        # (2026-10-21), so the errors come first here. Each error's locations are graphql-js's. An
        # error graphql-core made itself is in graphql-js's words and carries the code Apollo Server
        # gives an error with none, which a router writes in its vendor's envelope; one a resolver
        # raised is the vendor's own.
        payload: dict = {}
        assigned = []
        if result.errors:
            payload["errors"] = []
            for error in result.errors:
                entry = error.formatted
                if "locations" in entry:
                    entry["locations"] = list(js_words.locations(error))
                if _made_by_graphql(error):
                    entry["message"] = js_words.requote(entry["message"])
                    entry.setdefault("extensions", {})["code"] = INTERNAL_SERVER_ERROR
                    assigned.append(len(payload["errors"]))
                payload["errors"].append(entry)
        payload["data"] = result.data
        return Result(
            payload=payload,
            raised=any(_raised(e) for e in result.errors or ()),
            assigned=frozenset(assigned),
        )

    def execute(
        self,
        document: str,
        *,
        variables: Any = None,
        operation_name: str | None = None,
        context: Any = None,
    ) -> Result:
        """:meth:`resolve`, then :meth:`run` the operation it chose."""
        resolved = self.resolve(document, variables=variables, operation_name=operation_name)
        return resolved if isinstance(resolved, Result) else self.run(resolved, context=context)

    def execute_payload(
        self,
        payload: Any,
        *,
        context: Any = None,
        on_document: Callable[[DocumentNode], Result | list[GraphQLError]] | None = None,
        get: bool = False,
    ) -> Result:
        """:meth:`resolve_payload`, then :meth:`run` the operation it chose."""
        resolved = self.resolve_payload(payload, on_document=on_document, get=get)
        return resolved if isinstance(resolved, Result) else self.run(resolved, context=context)


def _made_by_graphql(error: GraphQLError) -> bool:
    """Whether graphql-core made ``error`` itself as the operation ran, as graphql-js would have:
    not a refusal a resolver raised (:func:`_marking_refusals`) and not an exception that is
    Backlot's own failure."""
    if getattr(error, "refusal", False) or getattr(error.original_error, "refusal", False):
        return False
    return error.original_error is None or isinstance(error.original_error, GraphQLError)
