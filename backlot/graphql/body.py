"""A GraphQL POST body read the way the Node servers in front of Linear and Fireflies read it,
where that parts from :func:`json.loads`: :func:`decode` turns the bytes into text, or
:func:`body_parser_text` does as Linear's ``body-parser`` does, and :func:`parse` turns the text
into values. What a vendor says to a body it cannot read is its own: :class:`Unread`,
:func:`strict_violation` and :func:`v8_constant_message` hold Linear's words, and
:func:`prototype_poisoned` names a body Fireflies refuses.
"""

from __future__ import annotations

import json
import json.decoder
import math
import re
import struct
import zlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# `json.decoder`'s whitespace and string scanner, and `json.scanner.NUMBER_RE` under `re.ASCII`: the
# C reader `json.loads` runs reads only the digits 0 to 9, which Python 3.11's `\d` alone does not.
_WHITESPACE = json.decoder.WHITESPACE.match
_NUMBER = re.compile(r"(-?(?:0|[1-9]\d*))(\.\d+)?([eE][-+]?\d+)?", re.ASCII).match
_scanstring = json.decoder.scanstring
_CONSTANTS = ("NaN", "Infinity", "-Infinity")


@dataclass(frozen=True)
class Constant:
    """One of :data:`_CONSTANTS` in a body, and the code point it starts at."""

    token: str
    index: int


class NotJson(ValueError):
    """A body the Node servers do not read as JSON. ``constant`` is the first constant token when
    the reader meets one before anything else it refuses, and None otherwise."""

    def __init__(self, text: str, constant: Constant | None = None):
        super().__init__("not JSON")
        self.text = text
        self.constant = constant


def decode(raw: bytes) -> str:
    """The text a Node server reads from ``raw``: UTF-8, each malformed sequence a U+FFFD, and one
    leading byte-order mark dropped, where ``json.loads`` on bytes guesses UTF-16 or UTF-32 from
    them and refuses a malformed byte. Both vendors refused a UTF-16 body and served a malformed
    byte inside a string on 2026-10-18 (UTC), and two byte-order marks leave the second, which
    Linear quotes back as the token it stopped at and Fireflies refuses. Eleven malformed sequences,
    from a lone continuation byte to a five-byte form, came back inside Linear's messages as as many
    U+FFFD as ``"replace"`` gives here."""
    text = raw.decode("utf-8", "replace")
    return text[1:] if text.startswith("\ufeff") else text


#: The most bytes Linear's ``body-parser`` reads, counted after :func:`_inflate`: api.linear.app
#: served a body of 10485760 bytes and refused one of 10485761 with :class:`Unread`'s 413 on
#: 2026-10-19 (UTC), each sent plain and gzipped.
LINEAR_LIMIT = 10 * 1024 * 1024

# The charsets iconv-lite names with `utf`, as `encodingExists` reads a name: in lower case, without
# a trailing `:` and four digits, and without any character but a letter or a digit.
_ICONV_UTF = frozenset({"utf8", "utf16le", "utf16be", "utf16", "utf32le", "utf32be", "utf32"})
_ICONV_UTF7 = frozenset({"utf7", "utf7imap"})
# The `content-type` package's `PARAM_REGEXP` and `QESC_REGEXP`, as version 1.0.5 has them.
_TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_PARAMETER = re.compile(
    rf'; *({_TOKEN}) *= *("(?:[\x0b\x20\x21\x23-\x5b\x5d-\x7e\x80-\xff]|\\[\x0b\x20-\xff])*"|{_TOKEN}) *'
)
_QUOTED_PAIR = re.compile(r"\\([\x0b\x20-\xff])")


class Unread(Exception):
    """A body Linear's ``body-parser`` refuses before it has the text: the status and the message
    Linear's error handler answers with, and for the 413 an uncompressed body draws, its length.
    """

    def __init__(self, status: int, message: str, length: int | None = None):
        super().__init__(message)
        self.status, self.message, self.length = status, message, length


def body_parser_text(raw: bytes, content_type: str | None, content_encoding: str | None) -> str:
    """The text Linear's ``body-parser`` reads from ``raw``, or :class:`Unread`: read off
    ``lib/read.js`` in body-parser 2.2.2, in its order of checks. A charset that does not start
    ``utf-`` is a 415, then an encoding other than ``identity``, ``gzip``, ``deflate`` and ``br``,
    then an uncompressed body past the limit the 413, which ``raw-body`` answers from
    ``Content-Length`` before it reads, then a ``utf-`` charset iconv-lite does not know the 415;
    then :func:`_inflate` reads a compressed body, and the bytes are decoded in the charset's byte
    order (:func:`_byte_order`).

    api.linear.app answered each step on 2026-10-19 (UTC): ``latin1``, ``iso-8859-1``, ``utf8`` and
    ``utf_16le`` were the charset 415, ``x-gzip``, ``bogus`` and ``gzip, gzip`` the encoding's, a
    ``utf-nope`` charset the 415 again, ``latin1`` with ``bogus`` the charset's, ``utf-nope`` with
    ``bogus`` the encoding's and with a body past the limit the 413, as it was sent chunked as well,
    and ``utf-16le``, ``utf-16-le``, ``utf-16be``, ``utf-16`` and ``utf-32`` with and without a
    byte-order mark, ``utf-32le``, ``GZIP``, ``deflate`` and an empty encoding were served. It
    decompressed a ``br`` body, which body-parser 1.x refuses with the encoding's 415, so it runs a
    2.x; such a body is read here as it arrives, and a ``utf-7`` or ``utf-7-imap`` one as UTF-8."""
    charset = _charset(content_type)
    if not charset.startswith("utf-"):
        raise Unread(415, f'unsupported charset "{charset.upper()}"')
    encoding = (content_encoding or "identity").lower()
    if encoding not in ("identity", "gzip", "deflate", "br"):
        raise Unread(415, f'unsupported content encoding "{encoding}"')
    if encoding == "identity" and len(raw) > LINEAR_LIMIT:
        raise Unread(413, "request entity too large", len(raw))
    name = re.sub(r":\d{4}$|[^0-9a-z]", "", charset)
    if name not in _ICONV_UTF | _ICONV_UTF7:
        raise Unread(415, f'unsupported charset "{charset.upper()}"')
    if encoding in ("gzip", "deflate"):
        raw = _inflate(raw, gzip=encoding == "gzip")
    if name in ("utf8", *_ICONV_UTF7):
        return decode(raw)
    width = 2 if name.startswith("utf16") else 4
    order = name[5:] or _byte_order(raw, width)  # past `utf16` or `utf32`
    raw = raw[: len(raw) - len(raw) % width]
    if width == 2:
        text = raw.decode(f"utf-16-{order}", "surrogatepass")
    else:
        points = struct.unpack(f"{'<' if order == 'le' else '>'}{len(raw) // 4}I", raw)
        text = "".join(chr(p) if p <= 0x10FFFF else "\ufffd" for p in points)
        # The units join into a JavaScript string, where a high surrogate then a low one is a pair.
        text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    return text[1:] if text.startswith("\ufeff") else text


def _charset(content_type: str | None) -> str:
    """``getCharset``: the last ``charset`` parameter of ``content_type`` in lower case, or
    ``utf-8`` for none. api.linear.app refused ``charset=utf-8; charset=latin1`` with the
    ``LATIN1`` 415 and served ``charset="utf-8"`` on 2026-10-19 (UTC). A ``Content-Type`` the
    package cannot parse, which ``body-parser`` does not read at all, is read here for what
    parameters it holds."""
    charset = ""
    for name, value in _PARAMETER.findall(content_type or ""):
        if name.lower() == "charset":
            charset = _QUOTED_PAIR.sub(r"\1", value[1:-1]) if value[:1] == '"' else value
    return charset.lower() or "utf-8"


def _byte_order(raw: bytes, width: int) -> str:
    """``le`` or ``be`` for a body whose charset names no byte order, as iconv-lite's
    ``detectEncoding`` picks: a byte-order mark first, else which order reads more of the first 100
    units of ``width`` bytes as a character below U+0100 (two bytes) or as one in the first plane,
    less those past U+10FFFF (four), little-endian on a tie."""
    if raw[:width] == (b"\xff\xfe" + b"\x00" * (width - 2)):
        return "le"
    if raw[:width] == (b"\x00" * (width - 2) + b"\xfe\xff"):
        return "be"
    score = 0  # the units read better big-endian, less those read better little-endian
    for i in range(0, min(len(raw) // width, 100) * width, width):
        unit = raw[i : i + width]
        if width == 2:
            score += (unit[0] == 0 and unit[1] != 0) - (unit[0] != 0 and unit[1] == 0)
        else:
            b0, b1, b2, b3 = unit
            score += (b0 == 0 and b1 == 0 and (b2 != 0 or b3 != 0)) - (b0 != 0 or b1 > 0x10)
            score -= ((b0 != 0 or b1 != 0) and b2 == 0 and b3 == 0) - (b3 != 0 or b2 > 0x10)
    return "be" if score > 0 else "le"


def _inflate(raw: bytes, gzip: bool) -> bytes:
    """``raw`` through Node's ``createGunzip`` or ``createInflate``, which ``body-parser`` pipes a
    body through, and :class:`Unread` where that stream fails or yields more than
    :data:`LINEAR_LIMIT` bytes. Node gunzips one member after another while the bytes left over do
    not start with a zero byte, and leaves what follows a ``deflate`` stream unread. The messages
    are zlib's, and ``unexpected end of file`` for a stream cut short."""
    out = bytearray()
    while True:
        inflater = zlib.decompressobj(31 if gzip else 15)
        try:
            out += inflater.decompress(raw, LINEAR_LIMIT + 1 - len(out))
        except zlib.error as exc:
            raise Unread(400, str(exc).rpartition(": ")[2]) from None
        if len(out) > LINEAR_LIMIT:
            raise Unread(413, "request entity too large")
        if not inflater.eof:
            raise Unread(400, "unexpected end of file")
        raw = inflater.unused_data
        if not (gzip and raw[:1].strip(b"\x00")):
            return bytes(out)


class Double(float):
    """A number as the double ``JSON.parse`` reads it, printed as JavaScript's ``String`` prints a
    number, which is how graphql-js quotes a variable's value; graphql-core quotes a float with
    ``repr``. Both vendors quoted ``0.00001``, ``1e20``, ``3000000000.0``, ``1e-7``, ``1e21``,
    ``123456789012345678901234`` and ``-1e400`` back as ``0.00001``, ``100000000000000000000``,
    ``3000000000``, ``1e-7``, ``1e+21``, ``1.2345678901234569e+23`` and ``-Infinity`` on
    2026-10-19 (UTC)."""

    def __repr__(self) -> str:
        if math.isinf(self):
            return "Infinity" if self > 0 else "-Infinity"
        if self == 0:
            return "0"
        sign, digits, exponent = Decimal(float.__repr__(self)).normalize().as_tuple()
        k = len(digits)  # `Number::toString`'s k, n and s
        n = exponent + k
        s = "".join(map(str, digits))
        if k <= n <= 21:
            text = s + "0" * (n - k)
        elif 0 < n <= 21:
            text = f"{s[:n]}.{s[n:]}"
        elif -6 < n <= 0:
            text = "0." + "0" * -n + s
        else:
            text = f"{s[0]}{'.' + s[1:] if k > 1 else ''}e{'+' if n > 0 else '-'}{abs(n - 1)}"
        return "-" * sign + text

    __str__ = __repr__


def _int(digits: str) -> int | float:
    """An integer as the double ``JSON.parse`` reads it as, kept a Python ``int`` below 2**53,
    where each integer is a double that :class:`Double` prints with the digits ``int`` does, and a
    :class:`Double` from there. ``9007199254740993`` came back as ``9007199254740992`` and 308
    nines as ``1e+308``, while 309 nines, 311 ones and 4301 ones came back as ``Infinity``, as the
    float ``1e400`` did, from both vendors on 2026-10-18 (UTC), and ``734946803407896717`` as
    ``734946803407896700`` on 2026-10-19. ``float`` reads a digit string of any length, so the
    4300-digit limit on ``int`` is never reached."""
    double = float(digits)
    return int(double) if abs(double) < 2**53 else Double(double)


class _ConstantRead(Exception):
    """Raised by ``json.loads``'s ``parse_constant`` hook, so the C reader stops at a constant."""


def _refuse_a_constant(token: str):
    raise _ConstantRead(token)


def parse(text: str) -> Any:
    """``text`` as Python values, read as ``JSON.parse`` reads it; :class:`NotJson` otherwise.

    Besides the numbers :func:`_int` and :class:`Double` read, two readings part from
    ``json.loads``, as both vendors answered on 2026-10-18 (UTC): the :data:`_CONSTANTS` are
    refused, where ``json.loads`` reads them as floats, and a body nested 300000 deep is read, where
    ``json.loads`` raises ``RecursionError`` at a depth the interpreter decides. The C reader runs
    first; it stops at a constant without saying where and at that depth, and either hands the
    text to :func:`_parse_iteratively`.

    A text holding a surrogate, which a UTF-16 or UTF-32 body :func:`body_parser_text` decodes can,
    is read through :func:`_joined`. On 2026-10-19 (UTC) Linear quoted a variable sent as U+D83D
    raw and then the escape of U+DE00, or as the escape and then the raw unit, back as the one
    character U+1F600, as it quoted the two escapes, and a key sent the first way as that character
    too."""
    try:
        value = json.loads(
            text, parse_int=_int, parse_float=Double, parse_constant=_refuse_a_constant
        )
    except json.JSONDecodeError:
        raise NotJson(text) from None
    except (_ConstantRead, RecursionError):
        value = _parse_iteratively(text)
    return _joined(value) if _LONE_SURROGATE.search(text) else value


def _join(string: str) -> str:
    """``string`` as the UTF-16 units JavaScript holds it in: a high surrogate followed by a low
    one is the character the two make."""
    return string.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


def _joined(value: Any) -> Any:
    """``value`` with :func:`_join` over each string in it, keys included. Two keys that become one
    keep the first one's place and the last one's value, as ``JSON.parse`` keeps a key it reads
    twice."""
    if type(value) is str:
        return _join(value)
    pending = [value]
    while pending:
        node = pending.pop()
        if type(node) is dict:
            items = list(node.items())
            node.clear()
            for key, item in items:
                node[_join(key)] = _join(item) if type(item) is str else item
            pending.extend(item for item in node.values() if type(item) in (dict, list))
        else:
            for i, item in enumerate(node):
                if type(item) is str:
                    node[i] = _join(item)
                elif type(item) in (dict, list):
                    pending.append(item)
    return value


def _key(text: str, i: int) -> tuple[str, int]:
    """An object key at ``i`` and the index after its colon and the whitespace that follows."""
    if text[i : i + 1] != '"':
        raise NotJson(text)
    key, i = _scanstring(text, i + 1, True)
    i = _WHITESPACE(text, i).end()
    if text[i : i + 1] != ":":
        raise NotJson(text)
    return key, _WHITESPACE(text, i + 1).end()


def _scalar(text: str, i: int) -> tuple[Any, int]:
    """A string, literal or number at ``i``, in ``json.scanner``'s order of tries."""
    if text[i : i + 1] == '"':
        return _scanstring(text, i + 1, True)
    for literal, value in (("null", None), ("true", True), ("false", False)):
        if text.startswith(literal, i):
            return value, i + len(literal)
    number = _NUMBER(text, i)
    if number is not None:
        integer, frac, exp = number.groups()
        if frac or exp:
            return Double(integer + (frac or "") + (exp or "")), number.end()
        return _int(integer), number.end()
    for token in _CONSTANTS:
        if text.startswith(token, i):
            raise NotJson(text, Constant(token, i))
    raise NotJson(text)


def _parse_iteratively(text: str) -> Any:
    """``json.loads(text, parse_int=_int, parse_float=Double)`` over an explicit stack, so no
    nesting is too deep for it, raising :class:`NotJson` where that raises and at the first
    constant token.

    Step for step ``json.decoder``'s grammar: its whitespace, its string and number scanners, and
    two equal keys keeping the last value in the first one's place, as ``dict(pairs)`` does."""
    containers: list[dict | list] = []  # the open objects and arrays, innermost last
    keys: list[str | None] = []  # the key each open object's next value goes under
    i = _WHITESPACE(text, 0).end()
    try:
        while True:
            opener = text[i : i + 1]
            if opener == "{" or opener == "[":
                i = _WHITESPACE(text, i + 1).end()
                if text[i : i + 1] == ("}" if opener == "{" else "]"):
                    value, i = ({} if opener == "{" else []), i + 1
                elif opener == "{":
                    key, i = _key(text, i)
                    containers.append({})
                    keys.append(key)
                    continue
                else:
                    containers.append([])
                    keys.append(None)
                    continue
            else:
                value, i = _scalar(text, i)
            # `value` is complete: hand it to its container, and close each container it completes.
            while True:
                if not containers:
                    if _WHITESPACE(text, i).end() != len(text):
                        raise NotJson(text)
                    return value
                top = containers[-1]
                if type(top) is dict:
                    top[keys[-1]] = value
                    closer = "}"
                else:
                    top.append(value)
                    closer = "]"
                i = _WHITESPACE(text, i).end()
                if text[i : i + 1] == ",":
                    i = _WHITESPACE(text, i + 1).end()
                    if closer == "}":
                        keys[-1], i = _key(text, i)
                    break
                if text[i : i + 1] != closer:
                    raise NotJson(text)
                containers.pop()
                keys.pop()
                value, i = top, i + 1
    except json.JSONDecodeError:
        raise NotJson(text) from None


# V8's JSON.parse quotes the source around the token it stopped at: the whole source when it is
# shorter than _SHORT UTF-16 units, otherwise the _CONTEXT units before the token and the _CONTEXT
# units from it on, cut at the source's ends. Measured on Linear on 2026-10-18 at lengths 19, 20, 21
# and 22 and at positions 5, 9, 10, 18 and the last ten, with a surrogate pair cut on either side.
_CONTEXT = 10
_SHORT = 2 * _CONTEXT + 1


def _units(utf16: bytes, start: int, end: int | None = None) -> str:
    """UTF-16 units ``start`` to ``end`` of a UTF-16-LE source, a pair cut in half kept as the
    lone surrogate JavaScript's ``substring`` leaves."""
    stop = None if end is None else 2 * end
    return utf16[2 * start : stop].decode("utf-16-le", "surrogatepass")


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-le", "surrogatepass")


def _unexpected_token(source: str, position: int) -> str:
    """V8's message for the token at UTF-16 unit ``position`` of ``source``."""
    utf16 = _utf16(source)
    length = len(utf16) // 2
    token = _units(utf16, position, position + 1)
    if length < _SHORT:
        return f"Unexpected token '{token}', \"{source}\" is not valid JSON"
    if position < _CONTEXT:
        quoted = _units(utf16, 0, position + _CONTEXT)
        return f"Unexpected token '{token}', \"{quoted}\"... is not valid JSON"
    if position < length - _CONTEXT:
        quoted = _units(utf16, position - _CONTEXT, position + _CONTEXT)
        return f"Unexpected token '{token}', ...\"{quoted}\"... is not valid JSON"
    quoted = _units(utf16, position - _CONTEXT)
    return f"Unexpected token '{token}', ...\"{quoted}\" is not valid JSON"


def _line_and_column(before: str, position: int) -> tuple[int, int]:
    """The line and column V8 reports for UTF-16 unit ``position``, given the text ``before`` it:
    each ``\\r``, ``\\n`` and ``\\r\\n`` ends a line, the column counting UTF-16 units from 1.
    Measured on Linear on 2026-10-18 after ``\\n``, ``\\r``, ``\\r\\n``, ``\\n\\r`` and a tab, and
    after a U+2028 inside a string, which ends no line."""
    line = 1 + before.count("\n") + before.count("\r") - before.count("\r\n")
    last = max(before.rfind("\n"), before.rfind("\r"))
    start = len(_utf16(before[: last + 1])) // 2
    return line, position - start + 1


def v8_constant_message(text: str, constant: Constant) -> str:
    """What ``JSON.parse(text)`` throws at ``constant``: the unexpected-token message at the ``N``
    or ``I`` it starts with, and for ``-Infinity`` the minus sign's message at the ``I``."""
    position = len(_utf16(text[: constant.index])) // 2
    if constant.token != "-Infinity":
        return _unexpected_token(text, position)
    position += 1
    line, column = _line_and_column(_units(_utf16(text), 0, position), position)
    return (
        f"No number after minus sign in JSON at position {position} (line {line} column {column})"
    )


# body-parser's `firstchar`: the first character that is not one of JSON's four whitespace ones.
_FIRST = re.compile(r"[\x20\x09\x0a\x0d]*([^\x20\x09\x0a\x0d])")


def strict_violation(text: str) -> str | None:
    """What Express's ``body-parser`` says to a non-empty body that does not open with ``{`` or
    ``[``, which its ``strict`` option refuses before ``JSON.parse`` reads anything; None for one
    that does.

    ``createStrictSyntaxError`` asks ``JSON.parse`` about a copy of the body with every unit from
    the first character on turned into ``#``, then puts the body's own text back over each run of
    ``#`` in the message, always from that first character; read off ``lib/types/json.js``, the same
    in body-parser 1.20.3 and 2.2.0, and Linear runs a 2.x (:func:`body_parser_text`). So ``NaN`` is
    ``Unexpected token 'N', "NaN" is not valid JSON`` and a body of whitespace alone is
    ``Unexpected end of JSON input``, as Linear answered on 2026-10-18, along with ``5``,
    ``-Infinity``, a quoted string of 38 units and a body that opens with a UTF-16 byte-order
    mark."""
    first = _FIRST.match(text)
    if first is None:
        return "Unexpected end of JSON input"
    if first.group(1) in "{[":
        return None
    index = first.start(1)  # only ASCII whitespace precedes it, so code points are units here
    utf16 = _utf16(text)
    masked = text[:index] + "#" * (len(utf16) // 2 - index)
    return re.sub(
        "#+",
        lambda run: _units(utf16, index, index + len(run.group())),
        _unexpected_token(masked, index),
    )


def prototype_poisoned(value: Any) -> bool:
    """Whether ``secure-json-parse``, which Fireflies reads its body with, refuses ``value``: an
    object anywhere holding a key ``__proto__``, or a key ``constructor`` whose value is an object
    holding a key ``prototype``.

    Measured on Fireflies on 2026-10-18: ``__proto__`` is refused at the top, inside an array and
    spelt ``\\u005f_proto__``, and served inside a string value; ``constructor`` is refused holding
    ``{"prototype": 1}`` or ``{"prototype": null}``, at the top or nested, and served holding
    ``null``, a string, an array or an object without ``prototype``, and when a second
    ``constructor`` key without one follows it."""
    pending = [value]
    while pending:
        node = pending.pop()
        if type(node) is dict:
            if "__proto__" in node:
                return True
            constructor = node.get("constructor")
            if type(constructor) is dict and "prototype" in constructor:
                return True
            pending.extend(node.values())
        elif type(node) is list:
            pending.extend(node)
    return False


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def node_json(value: Any) -> bytes:
    """``value`` the way ``JSON.stringify`` writes it for Express's ``res.json`` and Apollo Server:
    compact, non-ASCII as itself, and a lone surrogate as a lower-case ``\\u`` escape, which UTF-8
    cannot carry otherwise. Linear quoted half a surrogate pair back so on 2026-10-18. Sent the next
    day as an ``operationName``, in upper case or lower, one half came back so from both vendors,
    and from Linear sent as an unknown field of an input object. A float that is not finite is
    refused, as Starlette's ``JSONResponse`` refuses one."""
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return _LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text).encode("utf-8")
