"""graphql-js 16's words for the errors graphql-core 3.2 makes with its own.

Linear and Fireflies answered a validation error, a syntax error and a variable's refusal in
graphql-js 16.14.2's words on 2026-10-25 (UTC), and Linear refused an array sent for an input object
as 16.14.2 does, where 16.9.0 reads the array's indices as field names. Of the refusals this module
words, graphql-js 17.0.2 words otherwise each refusal of a variable's value, of an argument's value
as the field runs, of a missing argument, of a ``null`` written for a non-null type, of an input
object written as another kind of value, without a required field or with an unknown one, and of a
literal a custom scalar refuses, unless it refuses with a ``GraphQLError``, and the syntax errors at
``.5`` and ``..``. It also refuses a subscription where the schema has no subscription type as it
validates, in words of its own, adds a refusal of ``@skip`` or ``@include`` at a subscription's root
and, beside the unknown directive, the refusals its rules for ``@defer`` and ``@stream`` make, and
puts a variable's being unused ahead of its type's being unknown or not an input type. That was run
on 2026-10-28 (UTC) over documents for each of 16.14.2's validation rules, lexer and parser errors
and coercions of a value. A vendor's move to 17 shows here as answers that diverge, not as an error.

Linear and Fireflies run graphql-js under Apollo Server, and graphql-core is a port of graphql-js
whose messages differ from it: graphql-core quotes a name, a type, a token or a character with
``'`` where graphql-js quotes it with ``"``, names a directive's location by its value (``query``)
where graphql-js names it ``QUERY``, prints a variable's value with Python's ``repr`` where
graphql-js prints it with its own ``inspect``, words a variable's refusals its own way (``to be a
mapping``), and counts a line at characters graphql-js does not break at.

Here are :func:`requote` for a message, :func:`locations` for where it points, and
:func:`coerce_variable_values` for a variable's refusals, which it makes from the value as the
request sent it, so that no value passes through ``repr``.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable

from graphql import (
    GraphQLEnumType,
    GraphQLError,
    GraphQLInputObjectType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLSchema,
    type_from_ast,
)
from graphql.language import DirectiveLocation, VariableDefinitionNode
from graphql.pyutils import Undefined, suggestion_list

from backlot.graphql import body as request_body

# graphql-js's `inspect` prints ten items of an array and two levels of nesting.
_MAX_ARRAY_LENGTH = 10
_MAX_RECURSIVE_DEPTH = 2
# graphql-js stops coercing variables at the fiftieth error, as `execute` asks it to.
_MAX_ERRORS = 50


def stringify(text: str) -> str:
    """``text`` as ``JSON.stringify`` writes a string (:func:`backlot.graphql.body.node_json`)."""
    return request_body.node_json(text).decode("utf-8")


_INDEX = re.compile(r"0|[1-9][0-9]*")


def _is_array_index(key: str) -> bool:
    return _INDEX.fullmatch(key) is not None and int(key) < 2**32 - 1


def js_keys(obj: dict) -> list[str]:
    """``obj``'s keys in the order JavaScript gives an object's own keys: the array indices in
    ascending order, then the other keys in the order they were first set."""
    indices = sorted((key for key in obj if _is_array_index(key)), key=int)
    return indices + [key for key in obj if not _is_array_index(key)]


def number(value: int | float) -> str:
    """A JSON number as JavaScript's ``String`` prints it."""
    return repr(request_body.Double(value))


def inspect(value: Any) -> str:
    """``value``, a value ``JSON.parse`` could have made, as graphql-js's ``inspect`` prints it."""
    return _format(value, 0)


def _format(value: Any, depth: int) -> str:
    """``value`` nested ``depth`` arrays or objects deep. graphql-js's ``inspect`` also prints
    ``[Circular]`` for a value that holds itself, which no value ``JSON.parse`` makes does."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return number(value)
    if isinstance(value, str):
        return stringify(value)
    depth += 1
    if isinstance(value, list):
        if not value:
            return "[]"
        if depth > _MAX_RECURSIVE_DEPTH:
            return "[Array]"
        items = [_format(item, depth) for item in value[:_MAX_ARRAY_LENGTH]]
        remaining = len(value) - _MAX_ARRAY_LENGTH
        if remaining == 1:
            items.append("... 1 more item")
        elif remaining > 1:
            items.append(f"... {remaining} more items")
        return "[" + ", ".join(items) + "]"
    if not value:
        return "{}"
    if depth > _MAX_RECURSIVE_DEPTH:
        return "[Object]"
    return "{ " + ", ".join(f"{key}: {_format(value[key], depth)}" for key in js_keys(value)) + " }"


# graphql-js breaks a line at `\r\n`, `\n` or `\r` and nowhere else.
_LINE_BREAK = re.compile(r"\r\n|[\n\r]")


def locations(error: GraphQLError) -> tuple:
    """The ``locations`` graphql-js gives ``error``: each position's line and column as
    graphql-js's ``getLocation`` counts them, the lines broken only at :data:`_LINE_BREAK` and the
    column in UTF-16 code units, where graphql-core's count misses a line that starts at the
    position, adds one at each character Python's ``splitlines`` reads as a break and counts a
    character past U+FFFF once."""
    if not error.positions:
        return ()
    body = error.source.body
    out = []
    for position in error.positions:
        line, start = 1, 0
        for m in _LINE_BREAK.finditer(body):
            if m.start() >= position:
                break
            line, start = line + 1, m.end()
        units = len(body[start:position].encode("utf-16-le", "surrogatepass")) // 2
        out.append({"line": line, "column": units + 1})
    return tuple(out)


def did_you_mean(suggestions: list[str], sub_message: str = "") -> str:
    """graphql-js's ``didYouMean``: up to five suggestions, each in double quotes."""
    if not suggestions:
        return ""
    quoted = [f'"{s}"' for s in suggestions[:5]]
    message = " Did you mean " + (sub_message + " " if sub_message else "")
    if len(quoted) == 1:
        return message + quoted[0] + "?"
    if len(quoted) == 2:
        return message + quoted[0] + " or " + quoted[1] + "?"
    return message + ", ".join(quoted[:-1]) + ", or " + quoted[-1] + "?"


# --- the messages graphql-core writes itself ---------------------------------------------------

# A token or an escape sequence graphql-core quotes at the end of a syntax error: its text runs to
# the last quote, and may hold a quote of its own.
_SYNTAX_TAIL = re.compile(
    r"((?:found|Unexpected) (?:Name|String|BlockString|Int|Float|Comment) |"
    r"Invalid (?:Unicode|character) escape sequence: )'(.*)'\.$",
    re.S,
)
# A character graphql-core quotes with `'` at the end, where graphql-js uses `"`; both write the
# character `"` itself as '"'.
_SYNTAX_CHAR = re.compile(r": '(.)'\.$", re.S)
_PUNCTUATOR = re.compile(r"'(\.\.\.|[!$&(){}\[\]:=@|])'")
# A keyword the parser expected, such as `on`.
_KEYWORD = re.compile(r"Expected '([_A-Za-z][_0-9A-Za-z]*)'")
# The location a directive may not be used on, `fragment definition` here and `FRAGMENT_DEFINITION`
# in graphql-js.
_DIRECTIVE_PLACE = re.compile(r"(may not be used on )(.+)(\.)$")
_PLACE_NAMES = {place.value: place.name for place in DirectiveLocation}
# What a validation or execution message quotes: a name, `$name`, `@name`, `Type.field`, a type
# such as `[Int!]!`, or the `name { ... }` a leaf-selection error suggests.
_QUOTED = re.compile(
    r"'([$@]?[_A-Za-z][_0-9A-Za-z]*(?:[.][_A-Za-z][_0-9A-Za-z]*)?"
    r"|[\[\]!_0-9A-Za-z]+"
    r"|[_A-Za-z][_0-9A-Za-z]* \{ \.\.\. \})'"
)
# A GraphQL string literal ``print_ast`` wrote into a message, which may hold a quote.
_LITERAL = re.compile(r'"""(?:[^"\\]|\\.|"(?!""))*"""|"(?:[^"\\\n]|\\.)*"', re.S)


def requote(message: str) -> str:
    """A syntax, validation or execution message graphql-core wrote, in graphql-js's words."""
    if message.startswith("Syntax Error: "):
        m = _SYNTAX_TAIL.search(message)
        if m:
            message = message[: m.start(2) - 1] + '"' + m.group(2) + '".'
        else:
            m = _SYNTAX_CHAR.search(message)
            if m:
                message = (
                    message[: m.start()] + f': "{m.group(1)}".' if m.group(1) != '"' else message
                )
        return _KEYWORD.sub(r'Expected "\1"', _PUNCTUATOR.sub(r'"\1"', message))
    place = _DIRECTIVE_PLACE.search(message)
    if place:
        message = message[: place.start(2)] + _PLACE_NAMES[place.group(2)] + place.group(3)
    # The text between the string literals in ``message``, each ``(start, end)``.
    gaps, last = [], 0
    for literal in _LITERAL.finditer(message):
        gaps.append((last, literal.start()))
        last = literal.end()
    gaps.append((last, len(message)))
    # A custom scalar's refusal of a literal follows the first `; ` outside a literal, the one
    # message graphql-core writes with a `; `, in the scalar's own words: Linear's date scalars
    # quote with `'` there (`kind 'IntValue'`).
    cut = next((message.index("; ", a, b) for a, b in gaps if "; " in message[a:b]), len(message))
    head, out, last = message[:cut], [], 0
    for literal in _LITERAL.finditer(head):
        out.append(_QUOTED.sub(r'"\1"', head[last : literal.start()]) + literal.group())
        last = literal.end()
    return "".join(out) + _QUOTED.sub(r'"\1"', head[last:]) + message[cut:]


# --- a variable's refusals, as graphql-js makes them -----------------------------------------


_BUILT_IN = frozenset({"Int", "Float", "String", "Boolean", "ID"})


def _built_in_refusal(name: str, value: Any) -> str | None:
    """The message graphql-js's ``parseValue`` for the built-in scalar ``name`` refuses ``value``
    with, or ``None`` where it takes it."""
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    integral = is_number and float(value).is_integer()
    if name == "Int":
        if not integral:
            return f"Int cannot represent non-integer value: {inspect(value)}"
        if not -(2**31) <= value <= 2**31 - 1:
            return f"Int cannot represent non 32-bit signed integer value: {number(value)}"
    elif name == "Float":
        if not is_number or not math.isfinite(value):
            return f"Float cannot represent non numeric value: {inspect(value)}"
    elif name == "String":
        if not isinstance(value, str):
            return f"String cannot represent a non string value: {inspect(value)}"
    elif name == "Boolean":
        if not isinstance(value, bool):
            return f"Boolean cannot represent a non boolean value: {inspect(value)}"
    elif not (isinstance(value, str) or integral):  # ID, the last of `_BUILT_IN`
        return f"ID cannot represent value: {inspect(value)}"
    return None


def _enum(type_: GraphQLEnumType, value: Any) -> Any:
    names = list(type_.values)
    if not isinstance(value, str):
        shown = inspect(value)
        raise GraphQLError(
            f'Enum "{type_.name}" cannot represent non-string value: {shown}.'
            + did_you_mean(suggestion_list(shown, names), "the enum value")
        )
    if value not in type_.values:
        raise GraphQLError(
            f'Value "{value}" does not exist in "{type_.name}" enum.'
            + did_you_mean(suggestion_list(value, names), "the enum value")
        )
    return value


OnError = Callable[[list, Any, GraphQLError], None]


def coerce_input_value(value: Any, type_: Any, on_error: OnError, path: tuple = ()) -> None:
    """Report each refusal graphql-js's ``coerceInputValue`` makes of ``value`` as a ``type_``, to
    ``on_error(path, invalid_value, error)``, in the order graphql-js makes them."""
    if isinstance(type_, GraphQLNonNull):
        if value is None:
            on_error(
                list(path),
                value,
                GraphQLError(f'Expected non-nullable type "{type_}" not to be null.'),
            )
        else:
            coerce_input_value(value, type_.of_type, on_error, path)
        return
    if value is None:
        return
    if isinstance(type_, GraphQLList):
        if isinstance(value, list):
            for index, item in enumerate(value):
                coerce_input_value(item, type_.of_type, on_error, (*path, index))
        else:
            coerce_input_value(value, type_.of_type, on_error, path)
        return
    if isinstance(type_, GraphQLInputObjectType):
        if not isinstance(value, dict):
            on_error(
                list(path),
                value,
                GraphQLError(f'Expected type "{type_.name}" to be an object.'),
            )
            return
        fields = type_.fields
        for name, field in fields.items():
            if name not in value:
                if field.default_value is Undefined and isinstance(field.type, GraphQLNonNull):
                    on_error(
                        list(path),
                        value,
                        GraphQLError(
                            f'Field "{name}" of required type "{field.type}" was not provided.'
                        ),
                    )
                continue
            coerce_input_value(value[name], field.type, on_error, (*path, name))
        for name in js_keys(value):
            if name not in fields:
                on_error(
                    list(path),
                    value,
                    GraphQLError(
                        f'Field "{name}" is not defined by type "{type_.name}".'
                        + did_you_mean(suggestion_list(name, list(fields)))
                    ),
                )
        return
    if isinstance(type_, GraphQLEnumType):
        try:
            _enum(type_, value)
        except GraphQLError as error:
            on_error(list(path), value, error)
        return
    if type_.name in _BUILT_IN:
        message = _built_in_refusal(type_.name, value)
        if message is not None:
            on_error(list(path), value, GraphQLError(message))
        return
    try:
        type_.parse_value(value)
    # A custom scalar's refusal, a ValueError from Linear's, as graphql-js wraps a thrown Error.
    except Exception as error:
        on_error(
            list(path),
            value,
            GraphQLError(f'Expected type "{type_.name}". {error}', original_error=error),
        )


def _print_path(path: list) -> str:
    return "".join(f"[{key}]" if isinstance(key, int) else f".{key}" for key in path)


class _TooMany(Exception):
    pass


def coerce_variable_values(
    schema: GraphQLSchema, definitions: list[VariableDefinitionNode], inputs: Any
) -> list[GraphQLError]:
    """The refusals graphql-js's ``getVariableValues`` makes of the variables ``inputs`` for a
    validated operation declaring ``definitions``, as ``execute`` asks it: at most fifty, then one
    saying so. An empty list is a request whose variables graphql-js coerces."""
    inputs = inputs or {}
    errors: list[GraphQLError] = []

    def report(error: GraphQLError) -> None:
        if len(errors) >= _MAX_ERRORS:
            raise _TooMany
        errors.append(error)

    try:
        for definition in definitions:
            name = definition.variable.name.value
            type_ = type_from_ast(schema, definition.type)
            if name not in inputs:
                if definition.default_value is None and isinstance(type_, GraphQLNonNull):
                    report(
                        GraphQLError(
                            f'Variable "${name}" of required type "{type_}" was not provided.',
                            definition,
                        )
                    )
                continue
            value = inputs[name]
            if value is None and isinstance(type_, GraphQLNonNull):
                report(
                    GraphQLError(
                        f'Variable "${name}" of non-null type "{type_}" must not be null.',
                        definition,
                    )
                )
                continue

            def on_error(path: list, invalid: Any, error: GraphQLError) -> None:
                prefix = f'Variable "${name}" got invalid value {inspect(invalid)}'
                if path:
                    prefix += f' at "{name}{_print_path(path)}"'
                report(GraphQLError(f"{prefix}; {error.message}", definition))

            coerce_input_value(value, type_, on_error)
    except _TooMany:
        errors.append(
            GraphQLError(
                "Too many errors processing variables, error limit reached. Execution aborted."
            )
        )
    return errors
