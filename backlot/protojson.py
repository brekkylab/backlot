"""A JSON request body read the way Google's API front end reads one into its proto message.

The Sheets POST reads (`values:batchGetByDataFilter`, `spreadsheets:getByDataFilter`) take their
request as JSON, and real reads that JSON with protobuf's own streaming parser
(`util/internal/json_stream_parser.cc`) feeding its proto writer (`proto_writer.cc`,
`datapiece.cc`). Measured 2026-10-04 on `sheets.googleapis.com`, every message below and every
leniency is that code's: single-quoted strings, bare keys and trailing commas are read, `1.` is a
number, a syntax error names the 20 bytes either side of where it stopped, and a value of the wrong
kind is named by its proto path (`data_filters[0].grid_range.start_row_index.value`). This module is
a port of the parts those two requests reach, with the options real runs it under, each measured:
invalid UTF-8 is replaced rather than refused, and enum names match whatever the case of their ASCII
letters, with `-` read as `_`.

What real reports, and in which order, follows from feeding the whole body as one chunk and then
finishing (:func:`read`)."""

from __future__ import annotations

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
# A rendered value is ``(kind, value)``: ``string`` (str), ``int`` (a JSON integer that fits int64),
# ``uint`` (one that fits uint64 only), ``double``, ``bool`` or ``null``.


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
    if kind in ("int", "uint") and _INT32[0] <= value <= _INT32[1]:  # type: ignore[operator]
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
    if kind in ("int", "uint", "double") and value in (0, 1):
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
    """The parts of ``ProtoWriter`` and ``ProtoStreamObjectWriter`` the parser drives. Errors go to
    ``errors`` as ``(location or None, message)`` in the order they happen, which is the body's."""

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


# --- the parser --------------------------------------------------------------------------------


class _Cancel(Exception):
    """Parsing stopped for want of more input; the chunk pass leaves it for the finishing pass."""


class _Failure(Exception):
    """A syntax error, with the parser's sentence; ``None`` where real gives none (`_fail`)."""

    def __init__(self, message: str | None):
        super().__init__(message)
        self.message = message


_VALUE, _OBJ_MID, _ENTRY, _ENTRY_MID, _ARRAY_VALUE, _ARRAY_MID = range(6)
(
    _BEGIN_STRING,
    _BEGIN_NUMBER,
    _BEGIN_TRUE,
    _BEGIN_FALSE,
    _BEGIN_NULL,
    _BEGIN_OBJECT,
    _END_OBJECT,
    _BEGIN_ARRAY,
    _END_ARRAY,
    _ENTRY_SEPARATOR,
    _VALUE_SEPARATOR,
    _BEGIN_KEY,
    _UNKNOWN,
) = range(13)
_SPACE = b" \t\n\v\f\r"
_ESCAPES = {ord("b"): b"\b", ord("f"): b"\f", ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t"}
_ESCAPES[ord("v")] = b"\v"


def _is_letter(byte: int) -> bool:
    return chr(byte).isascii() and (chr(byte).isalpha() or byte in b"_$")


def _valid_utf8_prefix(data: bytes) -> int:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return exc.start
    return len(data)


def _replace_invalid(data: bytes) -> bytes:
    """``ReplaceInvalidCodePoints`` with real's replacement, a space per invalid byte."""
    out = bytearray()
    while data:
        n = _valid_utf8_prefix(data)
        out += data[:n]
        if n == len(data):
            break
        out += b" "
        data = data[n + 1 :]
    return bytes(out)


def _char_len(data: bytes, at: int) -> int:
    """``UTF8FirstLetterNumBytes``."""
    lead = data[at]
    size = 1 if lead < 0xC0 else 2 if lead < 0xE0 else 3 if lead < 0xF0 else 4
    return min(size, len(data) - at)


class _Parser:
    """``JsonStreamParser``, line for line where it matters: the parse stack, cancelling when a
    token may continue past the end of the chunk, and ``ReportFailure``'s context window."""

    def __init__(self, writer: _Writer):
        self.w = writer
        self.stack = [_VALUE]
        self.leftover = b""
        self.json = b""
        self.p = 0
        self.key: bytes | None = None
        self.string_open = 0
        self.parsed = bytearray()
        self.finishing = False

    # -- driving

    def parse(self, chunk: bytes) -> None:
        n = _valid_utf8_prefix(chunk)
        if n > 0:
            try:
                self._parse_chunk(chunk[:n])
            finally:
                self.leftover += chunk[n:]
        else:
            self.leftover = chunk

    def _parse_chunk(self, chunk: bytes) -> None:
        self.json, self.p, self.finishing = chunk, 0, False
        self._run()
        self._skip_space()
        if self.p == len(self.json):
            self.leftover = b""
        elif not self.stack:
            self._fail("Parsing terminated before end of input.")
        else:
            self.leftover = self.json[self.p :]

    def finish(self) -> None:
        if not self.stack and not self.leftover:
            return
        self.json, self.p, self.finishing = _replace_invalid(self.leftover), 0, True
        self._run()
        self._skip_space()
        if self.p < len(self.json):
            self._fail("Parsing terminated before end of input.")

    def _run(self) -> None:
        handlers = {
            _VALUE: self._value,
            _OBJ_MID: self._object_mid,
            _ENTRY: self._entry,
            _ENTRY_MID: self._entry_mid,
            _ARRAY_VALUE: self._array_value,
            _ARRAY_MID: self._array_mid,
        }
        while self.stack:
            kind = self.stack.pop()
            token = self._next_token() if self.string_open == 0 else _BEGIN_STRING
            try:
                handlers[kind](token)
            except _Cancel:
                if self.finishing:
                    raise
                self.stack.append(kind)
                return

    # -- reporting

    def _fail(self, message: str):
        """``ReportFailure``: the sentence, the 20 bytes either side of where parsing stopped, and a
        caret under it. Where that window cuts a character in two, real answers the generic
        ``Request contains an invalid argument.`` instead, measured 2026-10-04 at both ends of the
        window (the parser's message is then not valid UTF-8)."""
        begin = max(self.p - 20, 0)
        end = min(self.p + 20, len(self.json))
        try:
            segment = self.json[begin:end].decode("utf-8")
        except UnicodeDecodeError:
            raise _Failure(None) from None
        raise _Failure(f"{message}\n{segment}\n{' ' * (self.p - begin)}^")

    def _unknown(self, message: str):
        if not self.finishing:
            raise _Cancel
        if self.p == len(self.json):
            self._fail(f"Unexpected end of string. {message}")
        self._fail(message)

    # -- tokens

    def _skip_space(self) -> None:
        while self.p < len(self.json) and self.json[self.p] in _SPACE:
            self.p += 1

    def _advance(self) -> None:
        self.p += _char_len(self.json, self.p)

    def _next_token(self) -> int:
        self._skip_space()
        rest = self.json[self.p :]
        if not rest:
            return _UNKNOWN
        c = rest[0]
        if c in b"\"'":
            return _BEGIN_STRING
        if c == ord("-") or 0x30 <= c <= 0x39:
            return _BEGIN_NUMBER
        for word, token in (
            (b"true", _BEGIN_TRUE),
            (b"false", _BEGIN_FALSE),
            (b"null", _BEGIN_NULL),
        ):
            if rest.startswith(word):
                return token
        single = {
            ord("{"): _BEGIN_OBJECT,
            ord("}"): _END_OBJECT,
            ord("["): _BEGIN_ARRAY,
            ord("]"): _END_ARRAY,
            ord(":"): _ENTRY_SEPARATOR,
            ord(","): _VALUE_SEPARATOR,
        }
        if c in single:
            return single[c]
        return _BEGIN_KEY if _is_letter(c) else _UNKNOWN

    def _take_key(self) -> str:
        key = (self.key or b"").decode("utf-8", "replace")
        self.key = None
        return key

    # -- values

    def _value(self, token: int) -> None:
        if token == _BEGIN_OBJECT:
            self._advance()
            self.w.start_object(self._take_key())
            self.stack.append(_ENTRY)
        elif token == _BEGIN_ARRAY:
            self._advance()
            self.w.start_list(self._take_key())
            self.stack.append(_ARRAY_VALUE)
        elif token == _BEGIN_STRING:
            self._string()
            self.w.render(self._take_key(), ("string", self._decoded()))
        elif token == _BEGIN_NUMBER:
            self._number()
        elif token in (_BEGIN_TRUE, _BEGIN_FALSE, _BEGIN_NULL):
            word = {_BEGIN_TRUE: b"true", _BEGIN_FALSE: b"false", _BEGIN_NULL: b"null"}[token]
            piece = ("null", None) if token == _BEGIN_NULL else ("bool", token == _BEGIN_TRUE)
            self.w.render(self._take_key(), piece)
            self.p += len(word)
        elif token == _UNKNOWN:
            self._unknown("Expected a value.")
        else:
            # `fals` at the end of a chunk may yet be `false`, so a short leftover waits.
            if not self.finishing and len(self.json) - self.p < len(b"false"):
                raise _Cancel
            self._fail("Unexpected token.")

    def _decoded(self) -> str:
        raw, self.parsed = bytes(self.parsed), bytearray()
        return raw.decode("utf-8", "replace")

    def _string(self) -> None:
        if self.string_open == 0:
            self.string_open = self.json[self.p]
            self.p += 1
        while self.p < len(self.json):
            c = self.json[self.p]
            if c == ord("\\"):
                if len(self.json) - self.p == 1:
                    if not self.finishing:
                        raise _Cancel
                    self._fail("Closing quote expected in string.")
                if self.json[self.p + 1] == ord("u"):
                    self._unicode_escape()
                    continue
                nxt = self.json[self.p + 1]
                self.parsed += _ESCAPES.get(nxt, bytes([nxt]))
                self.p += 2
                continue
            if c == self.string_open:
                self.string_open = 0
                self.p += 1
                return
            step = _char_len(self.json, self.p)
            self.parsed += self.json[self.p : self.p + step]
            self.p += step
        if not self.finishing:
            raise _Cancel
        self.string_open = 0
        self._fail("Closing quote expected in string.")

    def _unicode_escape(self) -> None:
        rest = self.json[self.p :]
        if len(rest) < 6:
            if not self.finishing:
                raise _Cancel
            self._fail("Illegal hex string.")
        digits = rest[2:6]
        if not all(chr(d) in "0123456789abcdefABCDEF" for d in digits):
            self._fail("Invalid escape sequence.")
        code = int(digits, 16)
        if 0xD800 <= code <= 0xDBFF:
            if len(rest) < 12:
                if not self.finishing:
                    raise _Cancel
            elif rest[6:8] == b"\\u":
                low_digits = rest[8:12]
                if not all(chr(d) in "0123456789abcdefABCDEF" for d in low_digits):
                    self._fail("Invalid escape sequence.")
                low = int(low_digits, 16)
                if 0xDC00 <= low <= 0xDFFF:
                    code = (((code & 0x3FF) << 10) | (low & 0x3FF)) + 0x10000
                    self.p += 6
        # A surrogate left unpaired reads as U+FFFD, one each, measured 2026-10-04: `"\ud800A"` is
        # the string `\ufffdA`.
        self.parsed += "\ufffd".encode() if 0xD800 <= code <= 0xDFFF else chr(code).encode()
        self.p += 6

    def _number(self) -> None:
        rest = self.json[self.p :]
        index, floating = 0, False
        while index < len(rest):
            c = chr(rest[index])
            if "0" <= c <= "9":
                pass
            elif c in ".eE":
                floating = True
            elif c not in "+-x":
                break
            index += 1
        if index == len(rest) and not self.finishing:
            raise _Cancel
        text = rest[:index].decode("ascii")
        if floating:
            piece = self._double(text)
        elif not text.startswith("-"):
            if len(text) >= 2 and text[0] == "0":
                self._fail("Octal/hex numbers are not valid JSON values.")
            piece = (
                ("uint", int(text))
                if re.fullmatch(r"[0-9]+", text) and int(text) < 2**64
                else self._double(text)
            )
        else:
            if len(text) >= 3 and text[1] == "0":
                self._fail("Octal/hex numbers are not valid JSON values.")
            piece = (
                ("int", int(text))
                if re.fullmatch(r"-[0-9]+", text) and int(text) >= -(2**63)
                else self._double(text)
            )
        self.p += index
        self.w.render(self._take_key(), piece)

    def _double(self, text: str) -> tuple[str, float]:
        try:
            value = float.fromhex(text) if "x" in text.lower() else float(text)
        except ValueError:
            self._fail("Unable to parse number.")
        if not math.isfinite(value):
            self._fail("Number exceeds the range of double.")
        return ("double", value)

    # -- objects and arrays

    def _object_mid(self, token: int) -> None:
        if token == _UNKNOWN:
            self._unknown("Expected , or } after key:value pair.")
        if token == _END_OBJECT:
            self._advance()
            self.w.end_object()
        elif token == _VALUE_SEPARATOR:
            self._advance()
            self.stack.append(_ENTRY)
        else:
            self._fail("Expected , or } after key:value pair.")

    def _entry(self, token: int) -> None:
        if token == _UNKNOWN:
            self._unknown("Expected an object key or }.")
        if token == _END_OBJECT:  # a trailing comma is allowed
            self.w.end_object()
            self._advance()
            return
        if token == _BEGIN_STRING:
            self._string()
            self.key = bytes(self.parsed)
            self.parsed = bytearray()
        elif token in (_BEGIN_KEY, _BEGIN_NULL, _BEGIN_TRUE, _BEGIN_FALSE):
            self._bare_key()
            if token != _BEGIN_KEY and self.key in (b"null", b"true", b"false"):
                self._fail("Expected an object key or }.")
        else:
            self._fail("Expected an object key or }.")
        self.stack += [_OBJ_MID, _ENTRY_MID]

    def _bare_key(self) -> None:
        start = self.p
        end = start
        while end < len(self.json) and (
            _is_letter(self.json[end]) or 0x30 <= self.json[end] <= 0x39
        ):
            end += 1
        if not self.finishing and end == len(self.json):
            raise _Cancel
        self.key, self.p = self.json[start:end], end

    def _entry_mid(self, token: int) -> None:
        if token == _UNKNOWN:
            self._unknown("Expected : between key:value pair.")
        if token != _ENTRY_SEPARATOR:
            self._fail("Expected : between key:value pair.")
        self._advance()
        self.stack.append(_VALUE)

    def _array_value(self, token: int) -> None:
        if token == _UNKNOWN:
            self._unknown("Expected a value or ] within an array.")
        if token == _END_ARRAY:
            self.w.end_list()
            self._advance()
            return
        self.stack.append(_ARRAY_MID)
        try:
            self._value(token)
        except _Cancel:
            self.stack.pop()
            raise

    def _array_mid(self, token: int) -> None:
        if token == _UNKNOWN:
            self._unknown("Expected , or ] after array value.")
        if token == _END_ARRAY:
            self.w.end_list()
            self._advance()
        elif token == _VALUE_SEPARATOR:
            self._advance()
            self.stack.append(_ARRAY_VALUE)
        else:
            self._fail("Expected , or ] after array value.")


_BOM = b"\xef\xbb\xbf"


def read(body: bytes, message: Message) -> dict:
    """``body`` read into ``message``, as a dict keyed by proto field name, or the 400 real gives.

    Real feeds the body to the parser as one chunk and then finishes it, and reports in this order,
    measured 2026-10-04 with a body that fails at two stages at once: a syntax error the chunk pass
    hits (`{"dataFilters": "abc" "x"}` is the syntax error), then whatever the writer has refused by
    then (`{"dataFilters": "abc"` with no closing brace is the `DataFilter` refusal), then a syntax
    error the finishing pass hits (`{"bogus": 1` is `Unexpected end of string`: the `1` might have
    gone on, so it was left for that pass, and its field refused only there), then the writer's
    refusals from that pass. The writer's refusals are one 400 naming each, in body order.

    An empty body is an empty message, and a byte-order mark in front is skipped, both measured."""
    if body.startswith(_BOM):
        body = body[len(_BOM) :]
    if not body:
        return {}
    writer = _Writer(message)
    parser = _Parser(writer)
    for step in (lambda: parser.parse(body), parser.finish):
        try:
            step()
        except _Failure as failure:
            if failure.message is None:
                raise gerr.invalid_argument("Request contains an invalid argument.") from None
            raise gerr.invalid_json(failure.message) from None
        if writer.errors:
            raise gerr.invalid_field_values(writer.errors)
    return writer.result
