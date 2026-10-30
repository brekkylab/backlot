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

import calendar
import codecs
import datetime
import json
import math
import re
from collections.abc import Sequence
from decimal import Decimal
from typing import NamedTuple

from backlot.errors import google as gerr


class Enum(NamedTuple):
    """A proto enum: its type URL and its value names in number order."""

    type_url: str
    names: tuple[str, ...]


class Field(NamedTuple):
    """One field of a :class:`Message`. ``kind`` is ``int32``, ``int64``, ``float``, ``double``,
    ``string``, ``bool``, ``enum``, ``message``, ``timestamp`` (a ``google.protobuf.Timestamp``),
    ``rows`` (a repeated ``google.protobuf.ListValue``) or ``any`` (a ``google.protobuf.Value``,
    kept as plain JSON) -- the last two read whole by :meth:`_Writer.rows`; ``wrapper`` marks an
    ``Int32Value``, ``StringValue``, ``BoolValue`` or ``FloatValue`` field, whose scalar lives in a
    ``value`` member that the error path names. A map is a repeated ``message`` field with ``map``
    set, its message the entry (:func:`map_entry`)."""

    json_name: str
    name: str
    kind: str
    wrapper: bool = False
    enum: Enum | None = None
    message: Message | None = None
    repeated: bool = False
    oneof: str | None = None
    map: bool = False


class Message(NamedTuple):
    type_url: str
    fields: Sequence[Field]

    def find(self, name: str) -> Field | None:
        """A field by its JSON name or its proto name, the two spellings real accepts, matched
        exactly (`DataFilters` is an unknown name)."""
        return next((f for f in self.fields if name in (f.json_name, f.name)), None)


def map_entry(holder: str, json_name: str, value: Field, key: str = "string") -> Message:
    """The entry message of a map field of ``holder`` (a type URL): a ``key`` of kind ``key`` and
    ``value``, named as protoc names it. Measured 2026-10-29 on Docs `documents.create`,
    `{"headers": 5}` is refused naming ``google.apps.docs.v1.Document.HeadersEntry``."""
    return Message(
        f"{holder}.{json_name[:1].upper()}{json_name[1:]}Entry",
        (Field("key", "key", key), value._replace(json_name="value", name="value")),
    )


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
_INT64 = (-(2**63), 2**63 - 1)
_EPOCH_DAY = datetime.date(1970, 1, 1).toordinal()


def _str_to_int32(text: str, bounds: tuple[int, int] = _INT32) -> int:
    """``DataPiece::StringToNumber`` over ``safe_strto32``: a leading or trailing SPACE is refused
    outright, while any other ASCII whitespace around the digits is stripped; a sign and leading
    zeros are fine, and nothing but ASCII digits follows them."""
    if text[:1] == " " or text[-1:] == " ":
        raise ConversionError(f'"{text}"')
    body = text.strip(_ASCII_SPACE)
    if not re.fullmatch(r"[+-]?[0-9]+", body):
        raise ConversionError(f'"{text}"')
    number = int(body, 10)
    if not bounds[0] <= number <= bounds[1]:
        raise ConversionError(f'"{text}"')
    return number


def to_int32(piece: tuple[str, object], bounds: tuple[int, int] = _INT32) -> int:
    kind, value = piece
    low, high = bounds
    if kind == "string":
        return _str_to_int32(value, bounds)  # type: ignore[arg-type]
    if kind == "int" and low <= value <= high:  # type: ignore[operator]
        return value  # type: ignore[return-value]
    if kind == "double" and value == int(value) and low <= value <= high:  # type: ignore[arg-type]
        return int(value)  # type: ignore[arg-type]
    raise ConversionError(value_text(piece))


def to_double(piece: tuple[str, object]) -> float:
    """A proto3 JSON ``double``: a number, or a string spelling one, `NaN` and the infinities
    included."""
    kind, value = piece
    if kind in ("int", "double"):
        return float(value)  # type: ignore[arg-type]
    if kind == "string":
        try:
            return float(value)  # type: ignore[arg-type]
        except ValueError:
            pass
    raise ConversionError(value_text(piece))


# The largest finite `float`. Measured 2026-10-29 on a Docs `RgbColor.red`: `3.4e38` is read and
# `1e39`, `-1e39` and `"1e39"` are refused, while `"NaN"`, `"Infinity"` and `1e-50` are read.
_FLOAT_MAX = 3.4028234663852886e38


def to_float(piece: tuple[str, object]) -> float:
    """A proto3 JSON ``float``: a ``double`` that is not finite or fits a float."""
    value = to_double(piece)
    if math.isfinite(value) and abs(value) > _FLOAT_MAX:
        raise ConversionError(value_text(piece))
    return value


# Measured 2026-10-29 and 2026-10-30 on Docs `documents.create`, the refusals below in the order
# they are checked: no `Z` or `±hh:mm` offset under a day at the very end (`2026-10-29`, a
# lower-case `z`, `+0900`, `+24:00`, a final newline); what precedes the zone and the fraction not
# read as `%Y-%m-%dT%H:%M:%S`, the year an int64 but not `-` and zeros, each field after it one or
# two digits in its range, space allowed around the whole (month 13, hour 24, `+2026`, `-0000`, a
# lower-case `t`, no seconds); anything left after the seconds; a day past its month's last (30
# February), or a time, offset not applied, past int64 seconds from the epoch
# (`292277026596-12-04T15:30:08Z`); a fraction, what follows the last `.`, that is not an int32 or
# is `-` and zeros (`1a`, `+1`, `1 `, twelve digits, `-0`); and a time outside 0001 to 9999 UTC once
# the offset is applied, a negative fraction or a fraction past nine digits that is not zero. An
# empty fraction, ten zeros, a year of two or six digits, a second of 60 and
# `10000-01-01T00:00:00+01:00` are read.
_TS_NO_ZONE = (
    "Illegal timestamp format; timestamps must end with 'Z' or have a valid timezone offset."
)
_TS_UNPARSED = "Invalid time format: Failed to parse input"
_TS_TRAILING = "Invalid time format: Illegal trailing data in input string"
_TS_NO_SUCH_DAY = "Invalid time format: Out-of-range field"
_TS_NANOS = "Invalid time format, failed to parse nano seconds"
_TS_PAST_LIMITS = "Timestamp value exceeds limits"
_TS_ZONE = re.compile(r"(?:Z|([+-])([01][0-9]|2[0-3]):([0-5][0-9]))\Z")
_TS_FIELDS = re.compile(
    r"(-?[0-9]+)-([0-9]{1,2})-([0-9]{1,2})T([0-9]{1,2}):([0-9]{1,2}):([0-9]{1,2})"
)
# 0001-01-01T00:00:00Z and 9999-12-31T23:59:59Z, in seconds since the epoch
_TS_SECONDS = (-62_135_596_800, 253_402_300_799)


def _timestamp_error(piece: tuple[str, object]) -> str | None:
    """Why a ``google.protobuf.Timestamp`` refuses ``piece``, or None when it reads it: a string in
    RFC 3339, and nothing but a string."""
    kind, value = piece
    if kind != "string":
        return f"Invalid data type for timestamp, value is {value_text(piece)}"
    zone = _TS_ZONE.search(value)  # type: ignore[arg-type]
    if zone is None:
        return _TS_NO_ZONE
    text, dot, fraction = value[: zone.start()].rpartition(".")  # type: ignore[index]
    if not dot:
        text, fraction = fraction, ""
    text = text.lstrip(_ASCII_SPACE)
    parts = _TS_FIELDS.match(text)
    if parts is None:
        return _TS_UNPARSED
    year, month, day, hour, minute, second = (int(p) for p in parts.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60 and second <= 60):
        return _TS_UNPARSED
    if not _INT64[0] <= year <= _INT64[1] or (parts[1].startswith("-") and not year):
        return _TS_UNPARSED
    if text[parts.end() :].strip(_ASCII_SPACE):
        return _TS_TRAILING
    if day > (29 if month == 2 and calendar.isleap(year) else calendar.mdays[month]):
        return _TS_NO_SUCH_DAY
    cycles, year_in_cycle = divmod(year - 1, 400)  # the calendar repeats every 400 years
    days = cycles * 146_097 + datetime.date(year_in_cycle + 1, month, day).toordinal() - _EPOCH_DAY
    seconds = days * 86_400 + hour * 3600 + minute * 60 + second
    if not _INT64[0] <= seconds <= _INT64[1]:
        return _TS_NO_SUCH_DAY
    if fraction and re.fullmatch(r"-?[0-9]+", fraction) is None:
        return _TS_NANOS
    nanos = int(fraction or 0)
    if not _INT32[0] <= nanos <= _INT32[1] or (fraction.startswith("-") and not nanos):
        return _TS_NANOS
    if nanos < 0 or (len(fraction) > 9 and nanos):
        return _TS_PAST_LIMITS
    if zone[1]:
        seconds -= (int(zone[2]) * 60 + int(zone[3])) * 60 * (-1 if zone[1] == "-" else 1)
    if not _TS_SECONDS[0] <= seconds <= _TS_SECONDS[1]:
        return _TS_PAST_LIMITS
    return None


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

    def __init__(self, parent, message, field, out, *, is_list=False, target=None, is_map=False):
        self.parent = parent
        self.message = message
        self.field = field
        self.out = out
        self.index = 0 if is_list else -1
        # A wrapper opened as an object (`{"value": 1}`) writes its `value` into the message that
        # holds the wrapper field: ``(that message's dict, the field's proto name)``.
        self.target = target
        self.oneofs: set[str] = set()
        # A map is filled as a list of its entries is, each member an entry keyed by its name.
        self.is_map = is_map
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

    def _map_entry(self, top: _Frame, key: str) -> tuple[_Frame, Field]:
        """An entry of the map ``top`` fills, counted on it, and its value field. A key its kind
        cannot read is refused and its value read all the same, measured 2026-10-29 on Sheets'
        `criteria`, whose keys are int32: `{"a": {"condition": 5}}` names `criteria[0].key` and
        then `criteria[0].value.condition`."""
        entry = _Frame(top, top.field.message, top.field, None)
        key_field, value = top.field.message.fields
        if key_field.kind != "string":
            try:
                _convert(key_field, ("string", key))
            except ConversionError as exc:
                at = _Frame(entry, None, key_field, None).location()
                self._invalid_value(at, _type_name(key_field), exc.text)
        return entry, value

    def rows(self, name: str, value) -> bool:
        """Read a ``rows`` field whole, and say whether ``name`` was one.

        Each element is a ``ListValue``, a JSON array whose members may be anything JSON holds;
        what a member may MEAN is the service's question, not the proto layer's. A field that is
        not an array, or an element that is not one, is refused naming the ListValue type,
        measured 2026-10-26 on Sheets `values.update`: `"values": "x"` is ``Invalid value at
        'data.values' (type.googleapis.com/google.protobuf.ListValue), "x"`` and `["x"]` the same
        at `data.values[0]`."""
        if self.invalid_depth or not self.stack or self.stack[-1].is_list or not name:
            return False
        top = self.stack[-1]
        field = top.message.find(name)
        if field is None or field.kind not in ("rows", "any"):
            return False
        if field.kind == "any":
            if value is not None:
                top.out[field.name] = _plain(value)
            return True
        at = _Frame(top, None, field, None).location()
        if value is None:
            return True
        if not isinstance(value, list) or isinstance(value, _Members):
            self._invalid_value(at, _LIST_VALUE, _json_text(value))
            return True
        out = []
        for i, row in enumerate(value):
            if isinstance(row, list) and not isinstance(row, _Members):
                out.append([_plain(v) for v in row])
            elif row is not None:
                self._invalid_value(f"{at}[{i}]", _LIST_VALUE, _json_text(row))
        top.out[field.name] = out
        return True

    def start_object(self, name: str) -> None:
        if self.invalid_depth:
            self.invalid_depth += 1
            return
        if not self.stack:
            self.stack.append(_Frame(None, self.root, None, self.result))
            return
        top = self.stack[-1]
        if top.is_map:
            entry, value = self._map_entry(top, name)
            out: dict = {}
            top.out[name] = out
            self.stack.append(_Frame(entry, value.message, value, out))
            return
        if top.is_list:
            if top.field.kind != "message":
                # measured 2026-10-29 on Sheets `ranges` and Docs `positionedObjectIds`: `[{}]` is
                # named by the list, with the field as its type
                self._invalid_value(
                    top.location(), top.field.name, "Starting an object on a scalar field"
                )
                self.invalid_depth += 1
                return
            out = {}
            top.out.append(out)
            self.stack.append(_Frame(top, top.field.message, top.field, out))
            return
        field = self._lookup(top, name)
        if field is None:
            self.invalid_depth += 1
            return
        if field.kind not in ("message", "timestamp") and not field.wrapper:
            self._invalid_value(top.location(), field.name, "Starting an object on a scalar field")
            self.invalid_depth += 1
            return
        if not self._valid_oneof(top, field, name):
            self.invalid_depth += 1
            return
        if field.kind == "timestamp":  # an object is the message a Timestamp is
            self.stack.append(_Frame(top, _TIMESTAMP, field, top.out.setdefault(field.name, {})))
            return
        if field.map:
            out = top.out.setdefault(field.name, {})
            self.stack.append(_Frame(top, None, field, out, is_list=True, is_map=True))
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
        if top.is_map:
            entry, value = self._map_entry(top, name)
            at = _Frame(entry, None, value, None).location()
            self._invalid_value(at, "Map", f"Cannot have repeated items ('{name}') within a map.")
            self.invalid_depth += 1
            return
        if top.is_list:  # a list inside a list: more elements of the same field
            self.stack.append(_Frame(top, None, top.field, top.out, is_list=True))
            return
        field = self._lookup(top, name)
        if field is None:
            self.invalid_depth += 1
            return
        if field.map:
            self._invalid_value(
                top.location(), "Map", f"Cannot bind a list to map for field '{name}'."
            )
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
        if top.is_list:  # an element of a list or a map; a null one is skipped and not counted
            if piece[0] == "null":
                return
            if top.is_map:
                entry, value = self._map_entry(top, name)
                at = _Frame(entry, None, value, None).location()
                if value.kind == "message":
                    self._invalid_value(at, value.message.type_url, value_text(piece))
                    return
                try:
                    top.out[name] = _convert(value, piece)
                except ConversionError as exc:
                    self._invalid_value(at, _type_name(value), exc.text)
                return
            if top.field.kind != "message":  # a repeated scalar: `ranges` of a batch clear
                try:
                    top.out.append(_convert(top.field, piece))
                except ConversionError as exc:
                    # Only a refused element is counted, measured 2026-10-29 on Sheets `ranges`
                    # and Docs `positionedObjectIds`: `["A1", 5]` names `ranges[0]`, and
                    # `[5, "A1", 6]` names `ranges[0]` and `ranges[1]`.
                    at = _Frame(top, None, top.field, None).location()
                    self._invalid_value(at, _type_name(top.field), exc.text)
                return
            # A frame counts itself on the list it is made in, so it is made for an element only.
            at = _Frame(top, None, top.field, None).location()
            self._invalid_value(at, top.field.message.type_url, value_text(piece))
            return
        field = self._lookup(top, name)
        # A null leaves the field unset, and so does not take its oneof: a null `a1Range` beside a
        # `gridRange` answers the grid range, measured.
        if field is None or piece[0] == "null" or not self._valid_oneof(top, field, name):
            return
        if field.kind == "message":
            at = _Frame(top, None, field, None).location()
            self._invalid_value(at, field.message.type_url, value_text(piece))
            return
        # A repeated scalar takes one value as its one element, named without an index, measured
        # 2026-10-29: `"ranges": 5` is `Invalid value at 'ranges' (TYPE_STRING), 5`.
        element = _Frame(top, None, field, None)
        if field.wrapper:
            element = _Frame(element, None, Field("value", "value", field.kind), None)
        if field.kind == "timestamp":
            # named by the name the body gave it, `createTime` or `create_time`, measured
            reason = _timestamp_error(piece)
            if reason is not None:
                self._invalid_value(
                    element.location(), _TIMESTAMP.type_url, f"Field '{name}', {reason}"
                )
                return
            value = piece[1]
        else:
            try:
                value = _convert(field, piece)
            except ConversionError as exc:
                self._invalid_value(element.location(), _type_name(field), exc.text)
                return
        if top.target is not None:
            holder, held = top.target
            holder[held] = value
        elif field.repeated:
            top.out.setdefault(field.name, []).append(value)
        else:
            top.out[field.name] = value


_WRAPPER_DEFAULTS = {"int32": 0, "string": "", "bool": False, "float": 0.0}
_WRAPPERS = {
    "int32": "Int32Value",
    "string": "StringValue",
    "bool": "BoolValue",
    "float": "FloatValue",
}
_LIST_VALUE = "type.googleapis.com/google.protobuf.ListValue"
# What an object given for a Timestamp is read as, measured 2026-10-29:
# `{"seconds": 1, "nanos": 2}` is read, `"seconds": "x"` is `TYPE_INT64` and an unknown name is
# refused naming the field.
_TIMESTAMP = Message(
    "type.googleapis.com/google.protobuf.Timestamp",
    (Field("seconds", "seconds", "int64"), Field("nanos", "nanos", "int32")),
)


def _plain(value):
    """A parsed JSON value as plain Python: an object a dict, a number an int or a float."""
    if isinstance(value, _Members):
        return {_text(k): _plain(v) for k, v in value}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, tuple):  # a number, as `_number` read it
        kind, number = value
        if kind == "int" and abs(number) < 2**53:
            return number
        return float(number)
    return _text(value) if isinstance(value, str) else value


def _json_text(value) -> str:
    """How a refusal quotes a value it could not convert: a scalar as :func:`value_text` does."""
    if isinstance(value, list):
        return json.dumps(_plain(value))
    return value_text(_piece(value))


def _java_double(x: float) -> str:
    """Java's ``Double.toString``, which the measured `number_value: 1.0` is: shortest digits,
    plain from 1e-3 up to 1e7 and always with a decimal (`1.0`), else ``<d.ddd>E<n>`` (`1.0E10`)."""
    if x == 0:
        return "-0.0" if math.copysign(1, x) < 0 else "0.0"
    if 1e-3 <= abs(x) < 1e7:
        return repr(x)
    d = Decimal(repr(abs(x)))
    exp = d.adjusted()
    mantissa = str(d.scaleb(-exp).normalize())
    if "." not in mantissa:
        mantissa += ".0"
    return f"{'-' if x < 0 else ''}{mantissa}E{exp}"


def _text_escape(value: str) -> str:
    r"""A string as protobuf-java's ``TextFormat.escapeBytes`` writes it: ``\\``, ``\"``, ``\'``
    and the C control escapes by name, and every other byte outside printable ASCII, a UTF-8
    continuation byte included, as three octal digits."""
    out = []
    for byte in value.encode("utf-8"):
        ch = chr(byte)
        if ch in _TEXT_ESCAPES:
            out.append(_TEXT_ESCAPES[ch])
        elif 0x20 <= byte < 0x7F:
            out.append(ch)
        else:
            out.append(f"\\{byte:03o}")
    return "".join(out)


_TEXT_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "'": "\\'",
    "\a": "\\a",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\v": "\\v",
}


def _value_lines(value, indent: str) -> list[str]:
    """A ``google.protobuf.Value`` in protobuf text format, one line each."""
    if value is None:
        return [f"{indent}null_value: NULL_VALUE"]
    if isinstance(value, bool):
        return [f"{indent}bool_value: {'true' if value else 'false'}"]
    if isinstance(value, (int, float)):
        return [f"{indent}number_value: {_java_double(float(value))}"]
    if isinstance(value, str):
        return [f'{indent}string_value: "{_text_escape(value)}"']
    if isinstance(value, dict):
        return [f"{indent}struct_value {{", *_struct_lines(value, indent + "  "), f"{indent}}}"]
    return [f"{indent}list_value {{", *_list_lines(value, indent + "  "), f"{indent}}}"]


def _struct_lines(value: dict, indent: str) -> list[str]:
    lines = []
    for k, v in value.items():
        lines += [
            f"{indent}fields {{",
            f'{indent}  key: "{_text_escape(k)}"',
            f"{indent}  value {{",
        ]
        lines += _value_lines(v, indent + "    ")
        lines += [f"{indent}  }}", f"{indent}}}"]
    return lines


def _list_lines(value: list, indent: str) -> list[str]:
    lines = []
    for v in value:
        lines += [f"{indent}values {{", *_value_lines(v, indent + "  "), f"{indent}}}"]
    return lines


def value_debug(value) -> str:
    r"""How Sheets quotes a cell value it cannot store: its ``Value`` kind, a tab, and the message
    in protobuf text format. Measured 2026-10-26: `{"a": 1}` is ``struct_value \t {\n  fields
    {\n    key: "a"\n    value {\n      number_value: 1.0\n    }\n  }\n}\n`` and `[1, 2]`
    ``list_value \t {\n  values {\n    number_value: 1.0\n  }\n  values {\n    number_value:
    2.0\n  }\n}\n``."""
    if isinstance(value, dict):
        kind, body = "struct_value", _struct_lines(value, "  ")
    else:
        kind, body = "list_value", _list_lines(value, "  ")
    return f"{kind} \t " + "\n".join(["{", *body, "}"]) + "\n"


def _wrapper(field: Field) -> Message:
    """The wrapper message a ``{...}`` opens: one ``value`` member of the field's own kind."""
    return Message(
        f"type.googleapis.com/google.protobuf.{_WRAPPERS[field.kind]}",
        (Field("value", "value", field.kind),),
    )


def _type_name(field: Field) -> str:
    if field.kind == "enum":
        return field.enum.type_url
    return f"TYPE_{field.kind.upper()}"


def _convert(field: Field, piece: tuple[str, object]):
    if field.kind == "int32":
        return to_int32(piece)
    if field.kind == "int64":
        return to_int32(piece, _INT64)
    if field.kind == "float":
        return to_float(piece)
    if field.kind == "double":
        return to_double(piece)
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
        if writer.rows(name, value):
            continue
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

# How deep real's parser opens objects and lists, the root counted. Measured 2026-10-29 on Docs
# `documents.create`: a root holding 99 nested lists or objects is read and 100 is refused, an
# object as ``…reached for key '<the name it is under>'`` (`''` in a list) and a list as
# ``…reached in array``, alone whatever else the body holds.
_MAX_DEPTH = 100
_TOO_DEEP = "Message too deep. Max recursion depth reached"
_ROOT_NOT_A_MESSAGE = (
    'Invalid JSON payload received. Unknown name "": Root element must be a message.'
)


def _too_deep(tree) -> str | None:
    """The refusal of the first object or list in body order that opens past :data:`_MAX_DEPTH`."""
    pending = [(tree, "", 1)]
    while pending:
        value, key, depth = pending.pop()
        if isinstance(value, _Members):
            if depth > _MAX_DEPTH:
                return f"{_TOO_DEEP} for key '{key}'"
            pending += [(v, _text(k), depth + 1) for k, v in reversed(value)]
        elif isinstance(value, list):
            if depth > _MAX_DEPTH:
                return f"{_TOO_DEEP} in array"
            pending += [(v, "", depth + 1) for v in reversed(value)]
    return None


def _too_deep_text(text: str) -> str | None:
    """:func:`_too_deep` over text nested past what :func:`json.loads` can parse."""
    opened: list[str] = []
    key = last = ""
    i = 0
    while i < len(text):
        c = text[i]
        if c == '"':
            j = i + 1
            while j < len(text) and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            last, i = text[i : j + 1], j + 1
            continue
        if c == ":":
            try:
                key = _text(json.loads(last))
            except ValueError:
                key = ""
        elif c in "{[":
            if len(opened) >= _MAX_DEPTH:
                if c == "[":
                    return f"{_TOO_DEEP} in array"
                return f"{_TOO_DEEP} for key '{key if opened[-1] == '{' else ''}'"
            opened.append(c)
        elif c in "}]" and opened:
            opened.pop()
        i += 1
    return None


def whole(exc: gerr.GoogleError) -> bool:
    """Whether a refusal :func:`read` gave is of the body as a whole: JSON it could not parse, or a
    root that is not an object. Measured 2026-10-29 on Docs `documents.create` and Sheets
    `values:batchGetByDataFilter`, real answers one alone, with no query refusal beside it."""
    return exc.reason == "parseError" or gerr.field_violations(exc) == [(None, _ROOT_NOT_A_MESSAGE)]


def read(body: bytes, message: Message, *, field: str | None = None) -> dict:
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
    refused here. A body nested past :data:`_MAX_DEPTH` is refused as real refuses it."""
    if body.startswith(_BOM):
        body = body[len(_BOM) :]
    if not body:
        return {}
    text = body.decode("utf-8", "backlot.protojson.space")
    try:
        tree = json.loads(
            text,
            object_pairs_hook=_Members,
            parse_int=lambda text: _number(text, integer=True),
            parse_float=lambda text: _number(text, integer=False),
            parse_constant=_refuse_constant,
            strict=False,
        )
    except ValueError:
        raise gerr.invalid_json("Unexpected token.") from None
    except RecursionError:  # a body nested past Python's stack
        raise gerr.invalid_json(_too_deep_text(text) or "Unexpected token.") from None
    if (deep := _too_deep(tree)) is not None:
        raise gerr.invalid_json(deep)
    if field is not None and isinstance(tree, _Members):
        # A request whose body is one of its fields (`body: "data"` in the discovery document):
        # real names that field in every refusal, `Unknown name "bogus" at 'data'`.
        tree = _Members([(field, tree)])
    writer = _Writer(message)
    _walk(writer, tree)
    if writer.errors:
        raise gerr.invalid_field_values(writer.errors)
    return writer.result
