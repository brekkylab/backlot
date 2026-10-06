"""A JSON request body read the way Google's API front end reads one into its proto message.

The Sheets POST reads (`values:batchGetByDataFilter`, `spreadsheets:getByDataFilter`) take their
request as JSON, and real writes that JSON into the request message with protobuf's proto writer
(`proto_writer.cc`, `datapiece.cc`). Measured 2026-10-04 on `sheets.googleapis.com`, every refusal
the writer below gives is that code's: a value of the wrong kind and a name the message lacks are
named by their proto path (`data_filters[0].grid_range.start_row_index.value`). This module is a
port of the parts those two requests reach, with the option real runs it under, measured: enum names
match whatever the case of their ASCII letters, with `-` read as `_`. The JSON itself is parsed by
:func:`json.loads` (:func:`read`)."""

from __future__ import annotations

import codecs
import json
import math
import re
from typing import NamedTuple

from backlot.errors import google as gerr


class Enum(NamedTuple):
    """A proto enum: its type URL and its value names in number order."""

    type_url: str
    names: tuple[str, ...]


class Field(NamedTuple):
    """One field of a :class:`Message`. ``kind`` is ``int32``, ``string``, ``bool``, ``enum`` or
    ``message``; ``wrapper`` marks an ``Int32Value``/``StringValue`` field, whose scalar lives in a
    ``value`` member that the error path names."""

    json_name: str
    name: str
    kind: str
    wrapper: bool = False
    enum: Enum | None = None
    message: Message | None = None
    repeated: bool = False
    oneof: str | None = None


class Message(NamedTuple):
    type_url: str
    fields: tuple[Field, ...]

    def find(self, name: str) -> Field | None:
        """A field by its JSON name or its proto name, the two spellings real accepts, matched
        exactly (`DataFilters` is an unknown name)."""
        return next((f for f in self.fields if name in (f.json_name, f.name)), None)


class ConversionError(Exception):
    """A value the proto writer cannot convert; ``text`` is how real quotes it back."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


# --- values ------------------------------------------------------------------------------------
#
# A rendered value is ``(kind, value)``: ``string`` (str), ``bool``, ``null``, or a number as
# :func:`_number` reads one, ``int`` or ``double``.


def _dtoa(value: float) -> str:
    """protobuf's ``SimpleDtoa``: 15 significant digits when that round-trips, else 17."""
    text = f"{value:.15g}"
    return text if float(text) == value else f"{value:.17g}"


def value_text(piece: tuple[str, object]) -> str:
    """``DataPiece::ValueAsStringOrDefault``: a string in double quotes with nothing escaped, a
    number as its digits, a boolean as ``true``/``false``."""
    kind, value = piece
    if kind == "string":
        return f'"{value}"'
    if kind == "double":
        return _dtoa(value)  # type: ignore[arg-type]
    if kind == "bool":
        return "true" if value else "false"
    if kind == "null":
        return "null"
    return str(value)


_ASCII_SPACE = " \t\n\v\f\r"
_INT32 = (-(2**31), 2**31 - 1)


def _str_to_int32(text: str) -> int:
    """``DataPiece::StringToNumber`` over ``safe_strto32``: a leading or trailing SPACE is refused
    outright, while any other ASCII whitespace around the digits is stripped; a sign and leading
    zeros are fine, and nothing but ASCII digits follows them."""
    if text[:1] == " " or text[-1:] == " ":
        raise ConversionError(f'"{text}"')
    body = text.strip(_ASCII_SPACE)
    if not re.fullmatch(r"[+-]?[0-9]+", body):
        raise ConversionError(f'"{text}"')
    number = int(body, 10)
    if not _INT32[0] <= number <= _INT32[1]:
        raise ConversionError(f'"{text}"')
    return number


def to_int32(piece: tuple[str, object]) -> int:
    kind, value = piece
    if kind == "string":
        return _str_to_int32(value)  # type: ignore[arg-type]
    if kind == "int" and _INT32[0] <= value <= _INT32[1]:  # type: ignore[operator]
        return value  # type: ignore[return-value]
    if kind == "double" and value == int(value) and _INT32[0] <= value <= _INT32[1]:  # type: ignore[arg-type]
        return int(value)  # type: ignore[arg-type]
    raise ConversionError(value_text(piece))


def to_string(piece: tuple[str, object]) -> str:
    if piece[0] == "string":
        return piece[1]  # type: ignore[return-value]
    raise ConversionError(value_text(piece))


_TRUE = frozenset({"true", "t", "yes", "y", "1"})
_FALSE = frozenset({"false", "f", "no", "n", "0"})


def to_bool(piece: tuple[str, object]) -> bool:
    """A string by ``safe_strtob``'s words, whatever the case of its ASCII letters: `yeſ`, which is
    `yes` once `ſ` (U+017F) is case-folded outside ASCII, is refused, measured 2026-10-05. A number
    is read too, which v3.21's ``DataPiece::ToBool`` does not do but real does: measured 2026-10-04
    on `includeGridData`, the JSON numbers `1`, `0` and `1.0` answer and `2` is refused."""
    kind, value = piece
    if kind == "bool":
        return value  # type: ignore[return-value]
    if kind == "string":
        folded = "".join(c.lower() if c.isascii() else c for c in value)  # type: ignore[union-attr]
        if folded in _TRUE or folded in _FALSE:
            return folded in _TRUE
    if kind in ("int", "double") and value in (0, 1):
        return bool(value)
    raise ConversionError(value_text(piece))


def enum_from_string(text: str, enum: Enum) -> int:
    """``DataPiece::ToEnum`` for a string: the name, then a decimal number the enum declares, then
    the name with its ASCII letters upper-cased and `-` read as `_` (real runs with case-insensitive
    enum parsing and without lower-camel names: `rows` and `dimension-unspecified` answer,
    `dimensionUnspecified` is refused, and so is `rowſ`, whose `ſ` upper-cases to `S` outside ASCII,
    measured 2026-10-05). The query string's enums are read the same way."""
    if text in enum.names:
        return enum.names.index(text)
    try:
        number = _str_to_int32(text)
    except ConversionError:
        number = None
    if number is not None and 0 <= number < len(enum.names):
        return number
    normalized = "".join(c.upper() if c.isascii() else c for c in text).replace("-", "_")
    if normalized in enum.names:
        return enum.names.index(normalized)
    raise ConversionError(f'"{text}"')


def to_enum(piece: tuple[str, object], enum: Enum) -> int:
    """A string as :func:`enum_from_string` reads one; a number as an int32 whether or not the enum
    declares it, which is what lets an out-of-range number reach the service."""
    if piece[0] == "string":
        return enum_from_string(piece[1], enum)  # type: ignore[arg-type]
    return to_int32(piece)


# --- the proto writer --------------------------------------------------------------------------


class _Frame:
    """One open element, as ``ProtoWriter::ProtoElement`` keeps it: a message, or a list of a
    repeated field. ``field`` is the field it fills (``None`` at the root) and ``index`` counts a
    list's elements so far (``-1`` for a message). ``out`` is what it fills: a dict keyed by proto
    field name for a message (a key is present once its field was set, default value or not), a
    list for a repeated field."""

    def __init__(self, parent, message, field, out, *, is_list=False, target=None):
        self.parent = parent
        self.message = message
        self.field = field
        self.out = out
        self.index = 0 if is_list else -1
        # A wrapper opened as an object (`{"value": 1}`) writes its `value` into the message that
        # holds the wrapper field: ``(that message's dict, the field's proto name)``.
        self.target = target
        self.oneofs: set[str] = set()
        # An element of a repeated field counts itself on the list it sits in.
        if not is_list and field is not None and field.repeated and parent and parent.index >= 0:
            parent.index += 1

    @property
    def is_list(self) -> bool:
        return self.index >= 0

    def location(self) -> str:
        """``ProtoElement::ToString``: each field's proto name from the root down, left out where
        a list holds an element of its own field, and after a list the count of its elements so
        far less one, in brackets. Measured 2026-10-04 on nested lists: `[{...}, [{"a1Range": 5}]]`
        names `data_filters[0][0].a1_range`, `[[{"a1Range": 5}]]` names `data_filters[0].a1_range`
        and `[[{...}], {"a1Range": 5}]` names `data_filters[0].a1_range`."""
        chain = []
        frame = self
        while frame.parent is not None:
            chain.append(frame)
            frame = frame.parent
        loc = ""
        for frame in reversed(chain):
            if not frame.field.repeated or frame.parent.field is not frame.field:
                loc = f"{loc}.{frame.field.name}" if loc else frame.field.name
            if frame.field.repeated and frame.index > 0:
                loc += f"[{frame.index - 1}]"
        return loc


class _Writer:
    """The parts of ``ProtoWriter`` and ``ProtoStreamObjectWriter`` that :func:`_walk` drives.
    Errors go to ``errors`` as ``(location or None, message)`` in the order they happen, which is
    the body's."""

    def __init__(self, root: Message):
        self.root = root
        self.result: dict = {}
        self.stack: list[_Frame] = []
        self.invalid_depth = 0
        self.errors: list[tuple[str | None, str]] = []

    def _invalid_name(self, location: str, name: str, message: str) -> None:
        at = f" at '{location}'" if location else ""
        self.errors.append(
            (
                location or None,
                f'Invalid JSON payload received. Unknown name "{name}"{at}: {message}',
            )
        )

    def _invalid_value(self, location: str, type_name: str, value: str) -> None:
        at = f" at '{location}'" if location else ""
        self.errors.append((location or None, f"Invalid value{at} ({type_name}), {value}"))

    def _lookup(self, top: _Frame, name: str) -> Field | None:
        """``ProtoWriter::Lookup``. An empty name inside an element of a repeated field names that
        field again, and anywhere else is refused, measured 2026-10-04: `{"dataFilters": [{"": 1}]}`
        is `Invalid value at 'data_filters[0]'` for a `DataFilter`, and `"": 1` at the root is
        `Proto fields must have a name.` An object under such a name is written by real into the
        element as the nested message's protobuf encoding, which comes back as an unparseable
        `a1Range`; this reads it as a nested element instead, which selects nothing."""
        if not name:
            if top.field is not None and top.field.repeated:
                return top.field
            self._invalid_name(top.location(), name, "Proto fields must have a name.")
            return None
        field = top.message.find(name)
        if field is None:
            self._invalid_name(top.location(), name, "Cannot find field.")
        return field

    def _valid_oneof(self, top: _Frame, field: Field, name: str) -> bool:
        if field.oneof is None:
            return True
        if field.oneof in top.oneofs:
            self._invalid_value(
                top.location(),
                "oneof",
                f"oneof field '{field.oneof}' is already set. Cannot set '{name}'",
            )
            return False
        top.oneofs.add(field.oneof)
        return True

    def start_object(self, name: str) -> None:
        if self.invalid_depth:
            self.invalid_depth += 1
            return
        if not self.stack:
            self.stack.append(_Frame(None, self.root, None, self.result))
            return
        top = self.stack[-1]
        if top.is_list:
            out: dict = {}
            top.out.append(out)
            self.stack.append(_Frame(top, top.field.message, top.field, out))
            return
        field = self._lookup(top, name)
        if field is None:
            self.invalid_depth += 1
            return
        if field.kind != "message" and not field.wrapper:
            self._invalid_value(top.location(), field.name, "Starting an object on a scalar field")
            self.invalid_depth += 1
            return
        if not self._valid_oneof(top, field, name):
            self.invalid_depth += 1
            return
        if field.wrapper:
            top.out.setdefault(field.name, _WRAPPER_DEFAULTS[field.kind])
            self.stack.append(_Frame(top, _wrapper(field), field, {}, target=(top.out, field.name)))
            return
        if field.repeated:
            # An object where a list belongs is read as the list's one element, named without
            # an index (`data_filters.a1_range`), measured.
            out = {}
            top.out.setdefault(field.name, []).append(out)
            self.stack.append(_Frame(top, field.message, field, out))
            return
        self.stack.append(_Frame(top, field.message, field, top.out.setdefault(field.name, {})))

    def end_object(self) -> None:
        if self.invalid_depth:
            self.invalid_depth -= 1
        elif self.stack:
            self.stack.pop()

    def start_list(self, name: str) -> None:
        if self.invalid_depth:
            self.invalid_depth += 1
            return
        if not self.stack:
            self._invalid_name("", name, "Root element must be a message.")
            self.invalid_depth += 1
            return
        top = self.stack[-1]
        if top.is_list:  # a list inside a list: more elements of the same field
            self.stack.append(_Frame(top, None, top.field, top.out, is_list=True))
            return
        field = self._lookup(top, name)
        if field is None:
            self.invalid_depth += 1
            return
        if not field.repeated:
            self._invalid_name(
                top.location(), name, "Proto field is not repeating, cannot start list."
            )
            self.invalid_depth += 1
            return
        out = top.out.setdefault(field.name, [])
        self.stack.append(_Frame(top, None, field, out, is_list=True))

    def end_list(self) -> None:
        self.end_object()

    def render(self, name: str, piece: tuple[str, object]) -> None:
        if self.invalid_depth:
            return
        if not self.stack:
            # A bare `null` too, measured 2026-10-04 on both reads.
            self._invalid_name("", name, "Root element must be a message.")
            return
        top = self.stack[-1]
        if top.is_list:  # an element of a list
            if piece[0] != "null":
                at = _Frame(top, None, top.field, None).location()
                self._invalid_value(at, top.field.message.type_url, value_text(piece))
            return
        field = self._lookup(top, name)
        # A null leaves the field unset, and so does not take its oneof: a null `a1Range` beside a
        # `gridRange` answers the grid range, measured.
        if field is None or piece[0] == "null" or not self._valid_oneof(top, field, name):
            return
        if field.kind == "message" or field.repeated:
            at = _Frame(top, None, field, None).location()
            self._invalid_value(at, field.message.type_url, value_text(piece))
            return
        element = _Frame(top, None, field, None)
        if field.wrapper:
            element = _Frame(element, None, Field("value", "value", field.kind), None)
        try:
            value = _convert(field, piece)
        except ConversionError as exc:
            self._invalid_value(element.location(), _type_name(field), exc.text)
            return
        if top.target is not None:
            holder, held = top.target
            holder[held] = value
        else:
            top.out[field.name] = value


_WRAPPER_DEFAULTS = {"int32": 0, "string": ""}


def _wrapper(field: Field) -> Message:
    """The wrapper message a ``{...}`` opens: one ``value`` member of the field's own kind."""
    return Message(
        "type.googleapis.com/google.protobuf."
        + ("Int32Value" if field.kind == "int32" else "StringValue"),
        (Field("value", "value", field.kind),),
    )


def _type_name(field: Field) -> str:
    if field.kind == "enum":
        return field.enum.type_url
    return f"TYPE_{field.kind.upper()}"


def _convert(field: Field, piece: tuple[str, object]):
    if field.kind == "int32":
        return to_int32(piece)
    if field.kind == "string":
        return to_string(piece)
    if field.kind == "bool":
        return to_bool(piece)
    return to_enum(piece, field.enum)


# --- reading the body ----------------------------------------------------------------------------


class _Members(list):
    """An object's members as ``(name, value)`` pairs in body order, a repeated name kept."""


def _number(text: str, *, integer: bool) -> tuple[str, object]:
    """A JSON number as ``JsonStreamParser`` reads it: an integer from int64's least to uint64's
    greatest is an ``int``, and any other number a ``double``, which past a double's range is
    refused. Measured 2026-10-06, `-9223372036854775808` and `18446744073709551615` are quoted back
    as sent and the integers just past them as doubles."""
    if integer and -(2**63) <= (value := int(text)) < 2**64:
        return ("int", value)
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(text)
    return ("double", number)


def _refuse_constant(name: str):
    raise ValueError(name)


# Python keeps a `\ud800` escape that no low surrogate follows as that code point.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _text(value: str) -> str:
    """A string with each unpaired surrogate as U+FFFD, measured 2026-10-04: `"\\ud800A"` is
    U+FFFD and `A`."""
    return _LONE_SURROGATE.sub("\ufffd", value)


def _piece(value) -> tuple[str, object]:
    if isinstance(value, str):
        return ("string", _text(value))
    if isinstance(value, bool):
        return ("bool", value)
    if value is None:
        return ("null", None)
    return value  # a number, already read by `_number`


def _walk(writer: _Writer, tree) -> None:
    """``tree`` fed to ``writer`` element by element in body order, the calls real's parser makes
    on its writer."""
    pending = [(iter([("", tree)]), None)]
    while pending:
        entry = next(pending[-1][0], None)
        if entry is None:
            _, end = pending.pop()
            if end is not None:
                end()
            continue
        name, value = entry
        if isinstance(value, _Members):
            writer.start_object(name)
            pending.append((((_text(k), v) for k, v in value), writer.end_object))
        elif isinstance(value, list):
            writer.start_list(name)
            pending.append(((("", v) for v in value), writer.end_list))
        else:
            writer.render(name, _piece(value))


def _space_per_byte(exc: UnicodeDecodeError) -> tuple[str, int]:
    """``ReplaceInvalidCodePoints`` with real's replacement, a space per invalid byte, measured
    2026-10-06."""
    return " ", exc.start + 1


codecs.register_error("backlot.protojson.space", _space_per_byte)

_BOM = b"\xef\xbb\xbf"


def read(body: bytes, message: Message) -> dict:
    """``body`` read into ``message``, as a dict keyed by proto field name, or a 400.

    The body is parsed whole and then fed to the writer, whose refusals are one 400 naming each, in
    body order. Invalid UTF-8 is replaced rather than refused (:func:`_space_per_byte`), an empty
    body is an empty message, and a byte-order mark in front is skipped, all measured 2026-10-04.

    A body that is not JSON is refused with ``Unexpected token.``, and so are `NaN`, `Infinity` and
    a number past a double's range. Where real refuses one of these, it gives its parser's own
    sentence and up to 20 bytes either side of where it stopped,
    ``Request contains an invalid argument.`` where those bytes would split a character, or, for a
    body cut short, the refusal of a value read before the cut. Measured 2026-10-04, real's parser
    also reads single quotes, bare keys, a trailing comma, `1.` and an escape JSON lacks, all
    refused here; and measured 2026-10-06, it refuses a root object holding lists nested 100 deep
    (``Message too deep. Max recursion depth reached in array``), which is read here."""
    if body.startswith(_BOM):
        body = body[len(_BOM) :]
    if not body:
        return {}
    try:
        tree = json.loads(
            body.decode("utf-8", "backlot.protojson.space"),
            object_pairs_hook=_Members,
            parse_int=lambda text: _number(text, integer=True),
            parse_float=lambda text: _number(text, integer=False),
            parse_constant=_refuse_constant,
            strict=False,
        )
    except (ValueError, RecursionError):  # the latter for a body nested past Python's stack
        raise gerr.invalid_json("Unexpected token.") from None
    writer = _Writer(message)
    _walk(writer, tree)
    if writer.errors:
        raise gerr.invalid_field_values(writer.errors)
    return writer.result
