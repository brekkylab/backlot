"""What V8's ``JSON.parse`` throws for a text it cannot read, which Linear answers a body with.

:func:`error` walks the text as ``src/json/json-parser.cc`` does in V8 13.6.233.17, Node 24's, and
words the first problem with its message templates (``src/common/message-template.h``). Node 22, 23
and 25 run V8 12.4.254.21, 12.9.202.28 and 14.1.146.11, which have the same templates and stop at
the same place in each text the tests try; Linear does not say which it runs. What it answered on
2026-10-18 (UTC) puts it after V8 11.8: to ``{"query":"{ __typename }","b" 2}`` it said ``Expected
':' after property name in JSON at position 30 (line 1 column 31)``, as Node 22 does, where V8
11.8.172.17 says ``Unexpected number in JSON at position 30 (line 1 column 31)``. Positions and
columns count UTF-16 units, as V8's do.
"""

from __future__ import annotations

import re

# V8's token classes (`GetOneCharJsonToken`) for the characters that are not whitespace, which is
# skipped before a token is read; any other character is ILLEGAL.
_EOS, _NUMBER, _STRING, _LBRACE, _RBRACE, _LBRACK, _RBRACK = range(7)
_TRUE, _FALSE, _NULL, _COLON, _COMMA, _ILLEGAL = range(7, 13)
_TOKENS = {'"': _STRING, "-": _NUMBER, "[": _LBRACK, "{": _LBRACE, "]": _RBRACK, "}": _RBRACE}
_TOKENS.update({"t": _TRUE, "f": _FALSE, "n": _NULL, ":": _COLON, ",": _COMMA})
_TOKENS.update(dict.fromkeys("0123456789", _NUMBER))
_LITERALS = {_TRUE: "true", _FALSE: "false", _NULL: "null"}

# `GetJsonScanFlags`: what a backslash may be followed by, besides the `u` that opens a `\u` escape.
_ESCAPES = frozenset('"\\/bfnrt')
_DIGITS = frozenset("0123456789")
_HEX = frozenset("0123456789abcdefABCDEF")
_SKIP_WHITESPACE = re.compile(r"[ \t\r\n]*").match
_SKIP_DIGITS = re.compile(r"[0-9]*").match
# The characters `ScanJsonString` reads past: all but a quote, a backslash and U+0000 to U+001F.
_SKIP_STRING = re.compile(r'[^"\\\x00-\x1f]*').match

# Each template that ends "at position % (line % column %)" in V8, without that ending, which
# _message adds.
_EXPECTED_PROPERTY = "Expected property name or '}' in JSON"
_EXPECTED_QUOTED_PROPERTY = "Expected double-quoted property name in JSON"
_EXPECTED_COLON = "Expected ':' after property name in JSON"
_EXPECTED_COMMA_OR_BRACE = "Expected ',' or '}' after property value in JSON"
_EXPECTED_COMMA_OR_BRACKET = "Expected ',' or ']' after array element in JSON"
_TRAILING = "Unexpected non-whitespace character after JSON"
_UNTERMINATED_STRING = "Unterminated string in JSON"
_BAD_ESCAPE = "Bad escaped character in JSON"
_BAD_UNICODE_ESCAPE = "Bad Unicode escape in JSON"
_BAD_CONTROL = "Bad control character in string literal in JSON"
_NO_NUMBER_AFTER_MINUS = "No number after minus sign in JSON"
_UNTERMINATED_FRACTION = "Unterminated fractional number in JSON"
_MISSING_EXPONENT = "Exponent part is missing a number in JSON"
_BY_TOKEN = {_NUMBER: "Unexpected number in JSON", _STRING: "Unexpected string in JSON"}

# `GetErrorMessageWithEllipses`: the whole source when it is shorter than _SHORT UTF-16 units,
# otherwise the _CONTEXT units before the token and the _CONTEXT units from it on, cut at the
# source's ends. Measured on Linear on 2026-10-18 at lengths 19, 20, 21 and 22 and at positions 5,
# 9, 10, 18 and the last ten, with a surrogate pair cut on either side.
_CONTEXT = 10
_SHORT = 2 * _CONTEXT + 1
# `IsSpecialString`: a whole source V8 quotes back without naming a token.
_SPECIAL = frozenset({"[object Object]", "undefined", "Infinity", "NaN"})


class _Refused(Exception):
    """Where V8 stops: the code point, the token class there, and the template if V8 names one."""

    def __init__(self, at: int, token: int, template: str | None = None):
        super().__init__(template)
        self.at, self.token, self.template = at, token, template


def utf16(text: str) -> bytes:
    return text.encode("utf-16-le", "surrogatepass")


def units(utf16_bytes: bytes, start: int, end: int | None = None) -> str:
    """UTF-16 units ``start`` to ``end`` of a UTF-16-LE source, a pair cut in half kept as the
    lone surrogate JavaScript's ``substring`` leaves."""
    stop = None if end is None else 2 * end
    return utf16_bytes[2 * start : stop].decode("utf-16-le", "surrogatepass")


def error(text: str) -> str | None:
    """The message ``JSON.parse(text)`` throws, or None for a text it reads.

    The walk makes V8's checks in V8's order and keeps the open containers in a list rather than on
    the call stack, so it reads any depth, as V8 does by turning to its iterative parse when its
    recursive one runs low on stack. Linear answered a body stopping at each refusal the walk
    raises with that refusal's message on 2026-10-18 (UTC)."""
    try:
        _walk(text)
    except _Refused as refused:
        return _message(text, refused)
    return None


def _message(text: str, refused: _Refused) -> str:
    """`ReportUnexpectedToken`'s words for where the walk stopped."""
    template = refused.template or _BY_TOKEN.get(refused.token)
    if template is None and refused.token == _EOS:
        return "Unexpected end of JSON input"
    position = len(utf16(text[: refused.at])) // 2
    if template is None:
        if text in _SPECIAL:
            return f'"{text}" is not valid JSON'
        return _unexpected_token(text, position)
    line, column = _line_and_column(text[: refused.at], position)
    return f"{template} at position {position} (line {line} column {column})"


def _unexpected_token(source: str, position: int) -> str:
    """V8's message for the token at UTF-16 unit ``position`` of ``source``, quoting the source
    around it."""
    encoded = utf16(source)
    length = len(encoded) // 2
    token = units(encoded, position, position + 1)
    if length < _SHORT:
        return f"Unexpected token '{token}', \"{source}\" is not valid JSON"
    if position < _CONTEXT:
        quoted = units(encoded, 0, position + _CONTEXT)
        return f"Unexpected token '{token}', \"{quoted}\"... is not valid JSON"
    if position < length - _CONTEXT:
        quoted = units(encoded, position - _CONTEXT, position + _CONTEXT)
        return f"Unexpected token '{token}', ...\"{quoted}\"... is not valid JSON"
    quoted = units(encoded, position - _CONTEXT)
    return f"Unexpected token '{token}', ...\"{quoted}\" is not valid JSON"


def _line_and_column(before: str, position: int) -> tuple[int, int]:
    """The line and column V8 reports for UTF-16 unit ``position``, given the text ``before`` it:
    each ``\\r``, ``\\n`` and ``\\r\\n`` ends a line, the column counting UTF-16 units from 1.
    Measured on Linear on 2026-10-18 after ``\\n``, ``\\r``, ``\\r\\n``, ``\\n\\r`` and a tab, and
    after a U+2028 inside a string, which ends no line."""
    line = 1 + before.count("\n") + before.count("\r") - before.count("\r\n")
    last = max(before.rfind("\n"), before.rfind("\r"))
    start = len(utf16(before[: last + 1])) // 2
    return line, position - start + 1


def _token(text: str, i: int) -> int:
    return _TOKENS.get(text[i], _ILLEGAL) if i < len(text) else _EOS


def _expect(text: str, i: int, wanted: int, template: str) -> int:
    """`ExpectNext`: past the whitespace at ``i``, ``wanted`` or a refusal in ``template``'s words;
    the index after it."""
    i = _SKIP_WHITESPACE(text, i).end()
    if _token(text, i) != wanted:
        raise _Refused(i, _token(text, i), template)
    return i + 1


def _walk(text: str) -> None:
    """Raise :class:`_Refused` where V8 stops reading ``text``. Walking code points rather than
    UTF-16 units stops at the same place: every character V8 tests for is below U+0100, so it
    matches neither a code point above U+FFFF nor the first of the two units V8 reads that code
    point as."""
    containers: list[int] = []  # _LBRACE or _LBRACK for each open container, innermost last
    i = 0
    while True:
        # A value starts here.
        i = _SKIP_WHITESPACE(text, i).end()
        token = _token(text, i)
        if token == _LBRACE or token == _LBRACK:
            closer = _RBRACE if token == _LBRACE else _RBRACK
            j = _SKIP_WHITESPACE(text, i + 1).end()
            if _token(text, j) == closer:
                i = j + 1
            elif token == _LBRACE:
                i = _string(text, _expect(text, j, _STRING, _EXPECTED_PROPERTY))
                i = _expect(text, i, _COLON, _EXPECTED_COLON)
                containers.append(token)
                continue
            else:
                containers.append(token)
                i = j
                continue
        elif token == _STRING:
            i = _string(text, i + 1)
        elif token == _NUMBER:
            i = _number(text, i)
        elif token in _LITERALS:
            i = _literal(text, i, _LITERALS[token])
        else:
            raise _Refused(i, token)
        # The value is complete: close each container it completes.
        while True:
            i = _SKIP_WHITESPACE(text, i).end()
            if not containers:
                if i < len(text):
                    raise _Refused(i, _token(text, i), _TRAILING)
                return
            if _token(text, i) == _COMMA:
                i += 1
                if containers[-1] == _LBRACE:
                    i = _string(text, _expect(text, i, _STRING, _EXPECTED_QUOTED_PROPERTY))
                    i = _expect(text, i, _COLON, _EXPECTED_COLON)
                break
            if containers[-1] == _LBRACE:
                closer, template = _RBRACE, _EXPECTED_COMMA_OR_BRACE
            else:
                closer, template = _RBRACK, _EXPECTED_COMMA_OR_BRACKET
            if _token(text, i) != closer:
                raise _Refused(i, _token(text, i), template)
            containers.pop()
            i += 1


def _string(text: str, i: int) -> int:
    """`ScanJsonString` from the character after the opening quote; the index after the closing
    one."""
    while True:
        i = _SKIP_STRING(text, i).end()
        if i == len(text):
            raise _Refused(i, _ILLEGAL, _UNTERMINATED_STRING)
        if text[i] == '"':
            return i + 1
        if text[i] != "\\":
            raise _Refused(i, _ILLEGAL, _BAD_CONTROL)
        i += 1
        if i == len(text) or ord(text[i]) > 0xFF:
            raise _Refused(i, _token(text, i))
        if text[i] == "u":
            for _ in range(4):
                i += 1
                if i == len(text) or text[i] not in _HEX:
                    raise _Refused(i, _ILLEGAL, _BAD_UNICODE_ESCAPE)
        elif text[i] not in _ESCAPES:
            raise _Refused(i, _ILLEGAL, _BAD_ESCAPE)
        i += 1


def _number(text: str, i: int) -> int:
    """`ParseJsonNumberAsDoubleOrSmi` from a ``-`` or a digit; the index after the number. V8 also
    returns early after an integer's digits, to read it as a small integer, from where the checks
    below find nothing to refuse."""

    def at(k: int) -> str:
        return text[k] if k < len(text) else ""

    if text[i] == "-":
        i += 1
    if at(i) == "0":
        i += 1
        if at(i) in _DIGITS:
            raise _Refused(i, _NUMBER)
    else:
        start = i
        i = _SKIP_DIGITS(text, i).end()
        if i == start:
            raise _Refused(i, _ILLEGAL, _NO_NUMBER_AFTER_MINUS)
    if at(i) == ".":
        i += 1
        if at(i) not in _DIGITS:
            raise _Refused(i, _ILLEGAL, _UNTERMINATED_FRACTION)
        i = _SKIP_DIGITS(text, i).end()
    if at(i) in ("e", "E"):
        i += 1
        if at(i) in ("+", "-"):
            i += 1
        if at(i) not in _DIGITS:
            raise _Refused(i, _ILLEGAL, _MISSING_EXPONENT)
        i = _SKIP_DIGITS(text, i).end()
    return i


def _literal(text: str, i: int, word: str) -> int:
    """`ScanLiteral`: ``word`` at ``i``, its first letter already matched; the index after it."""
    for k in range(i + 1, i + len(word)):
        if k == len(text):
            raise _Refused(k, _EOS)
        if text[k] != word[k - i]:
            raise _Refused(k, _token(text, k))
    return i + len(word)
