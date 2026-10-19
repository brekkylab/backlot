"""A GraphQL POST body read the way the Node servers in front of Linear and Fireflies read it,
where that parts from :func:`json.loads`: :func:`decode` turns the bytes into text and
:func:`parse` the text into values. What a vendor says to a body it cannot read is its own:
:func:`strict_violation` and :func:`v8_constant_message` write Linear's words, and
:func:`prototype_poisoned` names a body Fireflies refuses.
"""

from __future__ import annotations

import json
import json.decoder
import math
import re
from dataclasses import dataclass
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
    that is what stopped the reader, and None when Python's reader refuses the body too."""

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


def _int(digits: str) -> int | float:
    """An integer as the double ``JSON.parse`` reads it as, kept a Python ``int`` unless that
    double is infinite. Both vendors echoed a variable declared ``Int`` this way on 2026-10-18:
    ``9007199254740993`` came back as ``9007199254740992`` and 308 nines as ``1e+308``, while 309
    nines, 311 ones and 4301 ones came back as ``Infinity``, as the float ``1e400`` did. ``float``
    reads a digit string of any length, so the 4300-digit limit on ``int`` is never reached."""
    double = float(digits)
    return double if math.isinf(double) else int(double)


class _ConstantRead(Exception):
    """Raised by ``json.loads``'s ``parse_constant`` hook, so the C reader stops at a constant."""


def _refuse_a_constant(token: str):
    raise _ConstantRead(token)


def parse(text: str) -> Any:
    """``text`` as Python values, read as ``JSON.parse`` reads it; :class:`NotJson` otherwise.

    Besides :func:`_int`'s integers, two readings part from ``json.loads``, as both vendors
    answered on 2026-10-18 (UTC): the :data:`_CONSTANTS` are refused, where ``json.loads`` reads
    them as floats, and a body nested 300000 deep is read, where ``json.loads``
    raises ``RecursionError`` at a depth the interpreter decides. The C reader runs first; it stops
    at a constant without saying where and at that depth, and either hands the text to
    :func:`_parse_iteratively`."""
    try:
        return json.loads(text, parse_int=_int, parse_constant=_refuse_a_constant)
    except json.JSONDecodeError:
        raise NotJson(text) from None
    except (_ConstantRead, RecursionError):
        return _parse_iteratively(text)


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
            return float(integer + (frac or "") + (exp or "")), number.end()
        return _int(integer), number.end()
    for token in _CONSTANTS:
        if text.startswith(token, i):
            raise NotJson(text, Constant(token, i))
    raise NotJson(text)


def _parse_iteratively(text: str) -> Any:
    """``json.loads(text, parse_int=_int)`` over an explicit stack, so no nesting is too deep for
    it, raising :class:`NotJson` where that raises and at the first constant token.

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
# shorter than _SHORT UTF-16 units, otherwise _CONTEXT units on each side, cut at the source's ends.
# Measured on Linear on 2026-10-18 at lengths 19, 20, 21 and 22 and at positions 5, 9, 10, 18 and
# the last ten, with a surrogate pair cut on either side.
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
    in body-parser 1.20.3 and 2.2.0, as the vendor does not say which it runs. So ``NaN`` is
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
    """``value`` the way Express's ``res.json`` writes it: compact, non-ASCII as itself, and a lone
    surrogate as a lower-case ``\\u`` escape, which is how Linear quoted half a surrogate pair back
    on 2026-10-18 and what UTF-8 cannot carry otherwise."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return _LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text).encode("utf-8")
