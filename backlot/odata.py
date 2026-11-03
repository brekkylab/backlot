"""OData ``$filter`` and ``$orderby`` as Microsoft's OData library (ODL) reads them — the library
Microsoft Graph's Teams and directory methods parse their queries with, measured through those
methods on 2026-10-21.

A ``$filter`` is read in three passes, and a refusal comes from the first pass that finds one:

- **Parsing**, left to right: string, number, date, date-time and enum literals, property and
  lambda paths, function calls, ``in`` lists, and the operators by OData's precedence (``or``, then
  ``and``, then the comparisons and ``has`` and ``in``, then ``add``/``sub``, then ``mul``/``div``/
  ``mod``, then ``not``, so ``not a eq b`` is ``(not a) eq b``). ODL counts its recursion: an
  expression is eight levels deep, a parenthesis nests another eight and a ``not`` one more, and
  past 800 the filter is refused.
- **Binding**, against the type the caller describes: a property it does not declare, a function
  or operand of the wrong type, a collection where a single value belongs, and a filter that is
  not a boolean are each refused in ODL's words.
- **What the validator counts** is left to the caller, which knows its limits: :meth:`Filter.nodes`
  is ODL's node count under the weights the caller measured, and the lambda nesting and the
  ``or``/``and`` children are counted too. A comparison ODL binds and .NET cannot run (a boolean
  ordered against another) is :attr:`Filter.deferred`, which the caller raises where the query
  runs.

Keywords, property names and enum members match without regard to ASCII case, as Graph matches
them; ``true``, ``false`` and ``null`` are literals only in lower case. The module knows nothing
about any vendor: a refusal is a :class:`FilterError`, and the router that asked decides how its
vendor words it.
"""

from __future__ import annotations

import decimal
import math
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def ascii_lower(text: str) -> str:
    """``text`` with only its ASCII letters lowered: .NET's ordinal case-insensitive match, under
    which the Kelvin sign is not a ``k`` and a long s is not an ``s``."""
    return text.translate(_ASCII_LOWER)


def is_blank(char: str) -> bool:
    """.NET's ``Char.IsWhiteSpace``: Python's ``isspace`` without the separators U+001C–U+001F."""
    return char.isspace() and not "\x1c" <= char <= "\x1f"


def strip_blanks(text: str) -> str:
    start, end = 0, len(text)
    while start < end and is_blank(text[start]):
        start += 1
    while end > start and is_blank(text[end - 1]):
        end -= 1
    return text[start:end]


class FilterError(ValueError):
    """``kind`` is ``syntax`` or ``type`` (``message`` is ODL's words for it), ``depth`` (the
    recursion limit), ``property`` (``name`` is not a property of the type) or ``enum`` (``name`` is
    not a member of the property's enum, and ``message`` says so)."""

    def __init__(self, kind: str, message: str = "", *, name: str = ""):
        super().__init__(message or f"{kind} {name}")
        self.kind = kind
        self.message = message
        self.name = name


def _syntax(message: str) -> FilterError:
    return FilterError("syntax", message)


def _mistyped(message: str) -> FilterError:
    return FilterError("type", message)


# --- lexing ------------------------------------------------------------------

STRING, BOOLEAN, DATETIME, DATE = "Edm.String", "Edm.Boolean", "Edm.DateTimeOffset", "Edm.Date"
INT32, INT64, DECIMAL = "Edm.Int32", "Edm.Int64", "Edm.Decimal"
SINGLE, DOUBLE = "Edm.Single", "Edm.Double"
_NUMERIC = frozenset({INT32, INT64, DECIMAL, SINGLE, DOUBLE, "Edm.Int16", "Edm.Byte"})

# What ODL reads as one date-time literal, well-formed or not, and what a well-formed one is; a
# literal's digits are ASCII ones.
_DATETIME_LIKE = re.compile(r"\d+-\d+-\d+T[\d:.]*(?:[Zz]|[+-]\d+:\d+)?", re.ASCII)
_DATETIME_FORM = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:\d{2})", re.ASCII
)
_DATE_FORM = re.compile(r"\d{4}-\d{2}-\d{2}(?!\w)", re.ASCII)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", re.ASCII)
_NAME = re.compile(r"[^\W\d]\w*(?:\.[^\W\d]\w*)*")
_QUOTED = re.compile(r"'(?:[^']|'')*'")
_PUNCT = "(),:/"
_DIGITS = "0123456789"
_INT32_MAX, _INT64_MAX = 2**31 - 1, 2**63 - 1
_DECIMAL_MAX = 79228162514264337593543950335


@dataclass(frozen=True)
class _Token:
    kind: str  # name, string, number, datetime, date, enum, punct
    text: str
    start: int
    end: int
    value: Any = None
    type: str | None = None


def _f32(x: float) -> float:
    """``x`` as a .NET ``Single`` holds it."""
    try:
        return struct.unpack("f", struct.pack("f", x))[0]
    except OverflowError:
        return math.copysign(math.inf, x)


def _shortest32(f: float) -> str:
    """The shortest text that reads back as the ``Single`` ``f``, as .NET writes one."""
    for digits in range(1, 10):
        text = f"{f:.{digits}g}"
        if _f32(float(text)) == f:
            return text
    return repr(f)


def _as_decimal(text: str) -> decimal.Decimal | None:
    """``text`` as a .NET ``decimal`` holds it exactly — at most 29 digits, 28 after the point,
    within its range — or None."""
    value = decimal.Decimal(text)
    if value == 0:
        return decimal.Decimal(0)
    _, digits, exponent = value.as_tuple()
    digits = list(digits)
    while digits and digits[-1] == 0 and exponent < 0:
        digits.pop()
        exponent += 1
    if exponent < -28 or len(digits) + max(exponent, 0) > 29 or abs(value) > _DECIMAL_MAX:
        return None
    return value


def _number(text: str) -> tuple[Any, str]:
    """A number literal's value and the type ODL infers for it. An integer is the narrowest integer
    type that holds it, ``Edm.Decimal`` past Int64, and ``Edm.Single`` past that. A fraction or an
    exponent is the first of ``Edm.Single``, ``Edm.Double`` and ``Edm.Decimal`` that holds it
    exactly; one no decimal holds is the first floating type it does not overflow or underflow,
    and ``Edm.Single`` when it fits neither."""
    if "." not in text and "e" not in text and "E" not in text:
        digits = text.lstrip("-").lstrip("0") or "0"
        if len(digits) > 29:
            return _f32(float(text)), SINGLE
        value = -int(digits) if text.startswith("-") else int(digits)
        if -_INT32_MAX - 1 <= value <= _INT32_MAX:
            return value, INT32
        if -_INT64_MAX - 1 <= value <= _INT64_MAX:
            return value, INT64
        if abs(value) <= _DECIMAL_MAX:
            return decimal.Decimal(value), DECIMAL
        return _f32(float(text)), SINGLE
    zero = decimal.Decimal(text) == 0
    double = float(text)
    single = _f32(double)
    fits32 = math.isfinite(single) and (single != 0 or zero)
    fits64 = math.isfinite(double) and (double != 0 or zero)
    exact = _as_decimal(text)
    if exact is not None:
        if fits32 and decimal.Decimal(_shortest32(single)) == exact:
            return single, SINGLE
        if fits64 and decimal.Decimal(repr(double)) == exact:
            return double, DOUBLE
        return exact, DECIMAL
    if fits32:
        return single, SINGLE
    if fits64:
        return double, DOUBLE
    return single, SINGLE


def _datetime(text: str) -> datetime:
    form = (
        f"The DateTimeOffset text '{text}' should be in format "
        "'yyyy-mm-ddThh:mm:ss('.'s+)?(zzzzzz)?' and each field value is within valid range."
    )
    if not _DATETIME_FORM.fullmatch(text):
        raise _syntax(form)
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00" if text[-1] in "Zz" else text)
    except ValueError:
        raise _syntax(form) from None


def _date(text: str) -> datetime | None:
    """A date literal's midnight, or None for text that is not a date, which ODL reads as the number
    in front of it."""
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when.replace(tzinfo=timezone.utc) if when.year else None


def _tokens(expr: str) -> list[_Token]:
    out: list[_Token] = []
    i, n = 0, len(expr)
    while i < n:
        c = expr[i]
        if is_blank(c):
            i += 1
            continue
        if c == "'":
            m = _QUOTED.match(expr, i)
            if m is None:
                raise _syntax(
                    f"There is an unterminated string literal at position {n} in '{expr}'."
                )
            out.append(_Token("string", m.group(), i, m.end(), m.group()[1:-1].replace("''", "'")))
            i = m.end()
            continue
        if c in _DIGITS or (c == "-" and i + 1 < n and expr[i + 1] in _DIGITS):
            if m := _DATETIME_LIKE.match(expr, i):
                out.append(_Token("datetime", m.group(), i, m.end(), _datetime(m.group())))
                i = m.end()
                continue
            if (m := _DATE_FORM.match(expr, i)) and (when := _date(m.group())) is not None:
                out.append(_Token("date", m.group(), i, m.end(), when))
                i = m.end()
                continue
            m = _NUMBER.match(expr, i)
            if m.end() < n and expr[m.end()] == ".":
                raise _syntax(f"A digit was expected at position {m.end() + 1} in '{expr}'.")
            value, kind = _number(m.group())
            out.append(_Token("number", m.group(), i, m.end(), value, kind))
            i = m.end()
            continue
        if m := _NAME.match(expr, i):
            end = m.end()
            # A qualified enum literal: `microsoft.graph.channelMembershipType'private'`.
            if q := _QUOTED.match(expr, end):
                out.append(
                    _Token("enum", expr[i : q.end()], i, q.end(), q.group()[1:-1], m.group())
                )
                i = q.end()
                continue
            out.append(_Token("name", m.group(), i, end))
            i = end
            continue
        if c in _PUNCT:
            out.append(_Token("punct", c, i, i + 1))
            i += 1
            continue
        raise _syntax(f"Syntax error: character '{c}' is not valid at position {i} in '{expr}'.")
    return out


# --- parsing -----------------------------------------------------------------

_PRECEDENCE = {
    "or": 1,
    "and": 2,
    **dict.fromkeys(("eq", "ne", "gt", "ge", "lt", "le", "has", "in"), 3),
    **dict.fromkeys(("add", "sub"), 4),
    **dict.fromkeys(("mul", "div", "divby", "mod"), 5),
}
_KIND = {
    "eq": "Equal",
    "ne": "NotEqual",
    "gt": "GreaterThan",
    "ge": "GreaterThanOrEqual",
    "lt": "LessThan",
    "le": "LessThanOrEqual",
    "and": "And",
    "or": "Or",
    "add": "Add",
    "sub": "Subtract",
    "mul": "Multiply",
    "div": "Divide",
    "divby": "Divide",
    "mod": "Modulo",
}
_COMPARISONS = frozenset(("eq", "ne", "gt", "ge", "lt", "le"))
_ORDERINGS = frozenset(("gt", "ge", "lt", "le"))
# ODL's recursion limit, in the levels its recursive descent enters.
_DEPTH_LIMIT = 800
_EXPRESSION_LEVELS = 7
_UNEXPECTED_COMMA = (
    "Invalid JSON. An unexpected comma was found in scope 'Array'. A comma is only valid between "
    "properties of an object or between elements of an array."
)
_UNREACHABLE = (
    "An internal error 'TypePromotionUtils_GetFunctionSignatures_Binary_UnreachableCodepath' "
    "occurred."
)


@dataclass
class Node:
    """One node of a parsed filter, for a caller whose service reads more than ODL binds:
    ``kind`` is ``literal``, ``name`` (a property or a lambda's variable), ``path``, ``lambda``
    (``op`` is ``any`` or ``all``), ``call``, ``not``, ``binary`` (a comparison or an arithmetic
    ``op``), ``nary`` (``and`` or ``or``) or ``in``; ``token`` is the text it was read from."""

    kind: str
    token: _Token | None = None
    children: list = field(default_factory=list)
    op: str = ""
    count: int = 0  # the `not`s of a chain
    variable: str | None = None
    items: list[_Token] = field(default_factory=list)


class _Parser:
    def __init__(self, expr: str):
        self.expr = expr
        self.tokens = _tokens(expr)
        self.i = 0

    def _peek(self, ahead: int = 0) -> _Token | None:
        j = self.i + ahead
        return self.tokens[j] if j < len(self.tokens) else None

    def _at(self, text: str, ahead: int = 0) -> bool:
        t = self._peek(ahead)
        return t is not None and t.kind == "punct" and t.text == text

    def _position(self) -> int:
        t = self._peek()
        return t.start if t is not None else len(self.expr)

    def parse(self) -> Node:
        node = self._expression(1)
        if (extra := self._peek()) is not None:
            raise _syntax(f"Syntax error at position {extra.end} in '{self.expr}'.")
        return node

    def _expression(self, depth: int, floor: int = 1) -> Node:
        """Precedence climbing over the binary levels; ``depth`` is ODL's at the expression."""
        left = self._unary(depth)
        while (t := self._peek()) is not None and t.kind == "name":
            op = ascii_lower(t.text)
            precedence = _PRECEDENCE.get(op)
            if precedence is None or precedence < floor:
                break
            self.i += 1
            if op == "in":
                left = Node("in", t, [left], items=self._in_list(t))
                continue
            right = self._expression(depth, precedence + 1)
            if op in ("and", "or") and left.kind == "nary" and left.op == op:
                left.children.append(right)
            elif op in ("and", "or"):
                left = Node("nary", t, [left, right], op=op)
            else:
                left = Node("binary", t, [left, right], op=op)
        return left

    def _unary(self, depth: int) -> Node:
        nots = 0
        first = self._peek()
        while (t := self._peek()) is not None and t.kind == "name" and ascii_lower(t.text) == "not":
            nots += 1
            self.i += 1
        operand = self._primary(depth + _EXPRESSION_LEVELS + nots)
        return Node("not", first, [operand], count=nots) if nots else operand

    def _primary(self, depth: int) -> Node:
        if depth > _DEPTH_LIMIT:
            raise FilterError("depth", "Recursion depth exceeded allowed limit.")
        t = self._peek()
        if t is None or (t.kind == "punct" and t.text != "("):
            raise _syntax(f"Expression expected at position {self._position()} in '{self.expr}'.")
        self.i += 1
        if t.text == "(" and t.kind == "punct":
            inner = self._expression(depth + 1)
            if not self._at(")"):
                raise _syntax(
                    f"')' or operator expected at position {self._position()} in '{self.expr}'."
                )
            self.i += 1
            return inner
        if t.kind != "name":
            return Node("literal", t)
        if t.text in ("true", "false", "null"):
            return Node("literal", t)
        if self._at("("):
            self.i += 1
            args: list[Node] = []
            if self._at(")"):
                self.i += 1
                return Node("call", t, args)
            while True:
                args.append(self._expression(depth + 1))
                if self._at(","):
                    self.i += 1
                    continue
                if self._at(")"):
                    self.i += 1
                    return Node("call", t, args)
                raise _syntax(
                    f"')' or ',' expected at position {self._position()} in '{self.expr}'."
                )
        if self._at("/"):
            self.i += 1
            segment = self._peek()
            if segment is None or segment.kind != "name":
                raise _syntax(
                    f"Expression expected at position {self._position()} in '{self.expr}'."
                )
            self.i += 1
            if ascii_lower(segment.text) in ("any", "all") and self._at("("):
                return self._lambda(t, segment, depth)
            return Node("path", segment, [Node("name", t)])
        return Node("name", t)

    def _lambda(self, collection: _Token, quantifier: _Token, depth: int) -> Node:
        self.i += 1  # the `(`
        node = Node(
            "lambda", quantifier, [Node("name", collection)], op=ascii_lower(quantifier.text)
        )
        if self._at(")"):
            self.i += 1
            return node
        variable = self._peek()
        if variable is None or variable.kind != "name" or not self._at(":", 1):
            raise _syntax(f"Syntax error at position {self._position()} in '{self.expr}'.")
        self.i += 2
        node.variable = variable.text
        node.children.append(self._expression(depth + 1))
        if not self._at(")"):
            raise _syntax(
                f"')' or operator expected at position {self._position()} in '{self.expr}'."
            )
        self.i += 1
        return node

    def _in_list(self, op: _Token) -> list[_Token]:
        """The parenthesized list ODL reads after ``in`` as a JSON array."""
        if op.text != "in" or not self._at("("):
            raise _syntax(
                "An unexpected 'PrimitiveValue' node was found when reading from the JSON reader. "
                "A 'StartArray' node was expected."
            )
        self.i += 1
        items: list[_Token] = []
        expect_item = True
        while True:
            t = self._peek()
            if t is None:
                raise _syntax(
                    f"')' or operator expected at position {len(self.expr)} in '{self.expr}'."
                )
            self.i += 1
            if t.kind == "punct" and t.text == ")":
                if items and expect_item:
                    raise _syntax(_UNEXPECTED_COMMA)
                return items
            if t.kind == "punct" and t.text == ",":
                if expect_item:
                    raise _syntax(_UNEXPECTED_COMMA)
                expect_item = True
                continue
            if not expect_item:
                raise _syntax(
                    "Invalid JSON. A comma character ',' was expected in scope 'Array'. Every two "
                    "elements in an array and properties of an object must be separated by commas."
                )
            if t.kind == "punct" and t.text == "(":
                raise _syntax("Found an unbalanced bracket expression.")
            items.append(t)
            expect_item = False


# --- binding and evaluation --------------------------------------------------

_FUNCTIONS: dict[str, tuple[tuple[tuple[str, ...], ...], str]] = {
    "startswith": (((STRING, STRING),), BOOLEAN),
    "endswith": (((STRING, STRING),), BOOLEAN),
    "contains": (((STRING, STRING),), BOOLEAN),
    "indexof": (((STRING, STRING),), INT32),
    "concat": (((STRING, STRING),), STRING),
    "tolower": (((STRING,),), STRING),
    "toupper": (((STRING,),), STRING),
    "trim": (((STRING,),), STRING),
    "length": (((STRING,),), INT32),
    "substring": (((STRING, INT32), (STRING, INT32, INT32)), STRING),
}
_CLR = {BOOLEAN: "System.Nullable`1[System.Boolean]", None: "System.Object"}

Evaluator = Callable[[Mapping[str, Any], Mapping[str, Any]], Any]


def _element(collection_type: str) -> str | None:
    m = re.fullmatch(r"Collection\((.+)\)", collection_type or "")
    return m.group(1) if m else None


def _string_order(a: Any, b: Any) -> tuple[Any, Any]:
    """Strings order as .NET's culture comparison orders them — letters before case, a lower-case
    letter before its capital — with null below every string: ``'Ava' gt 'a'`` holds live."""

    def key(s: Any) -> tuple:
        return (s is not None, (s or "").casefold(), (s or "").swapcase())

    return key(a), key(b)


@dataclass(frozen=True)
class _Bound:
    type: str | None
    evaluate: Evaluator
    literal: _Token | None = None


class _Binder:
    def __init__(
        self,
        properties: Mapping[str, str],
        enums: Mapping[str, Iterable[str]],
        *,
        fold_case: bool,
        open_type: bool,
        literal_first: bool,
    ):
        self.properties = {ascii_lower(p): (p, t) for p, t in properties.items()}
        self.enums = {name: list(members) for name, members in enums.items()}
        self.fold = fold_case
        self.open = open_type
        self.literal_first = literal_first
        self.deferred: str | None = None
        self.lambda_depth = 0

    # Each bound node is a type and an evaluator over (entity, lambda variables).

    def bind(self, node: Node, scope: Mapping[str, str], lambdas: int = 0) -> _Bound:
        return getattr(self, f"_{node.kind}")(node, scope, lambdas)

    def _literal(self, node, scope, lambdas) -> _Bound:
        t = node.token
        if t.kind == "name":
            value = {"true": True, "false": False, "null": None}[t.text]
            return _Bound(None if value is None else BOOLEAN, lambda e, v: value, t)
        if t.kind == "string":
            return _Bound(STRING, lambda e, v, s=t.value: s, t)
        if t.kind == "number":
            return _Bound(t.type, lambda e, v, n=t.value: n, t)
        if t.kind in ("datetime", "date"):
            return _Bound(DATETIME if t.kind == "datetime" else DATE, lambda e, v, d=t.value: d, t)
        # A qualified enum literal names its type.
        member = self._member(t.type, t.value)
        return _Bound(t.type, lambda e, v: member, t)

    def _member(self, type_name: str, value: str) -> str:
        members = {ascii_lower(m): m for m in self.enums.get(type_name, ())}
        member = members.get(ascii_lower(value))
        if member is None:
            raise FilterError(
                "enum",
                f"The string '{value}' is not a valid enumeration type constant.",
                name=value,
            )
        return member

    def _name(self, node, scope, lambdas) -> _Bound:
        name = node.token.text
        if name in scope:
            return _Bound(scope[name], lambda e, v: v.get(name))
        found = self.properties.get(ascii_lower(name))
        if found is None and self.open:
            return _Bound(None, lambda e, v: e.get(name))
        if found is None:
            raise FilterError("property", name=name)
        prop, type_name = found
        if type_name == DATETIME:
            return _Bound(type_name, lambda e, v: _as_datetime(e.get(prop)))
        return _Bound(type_name, lambda e, v: e.get(prop))

    def _path(self, node, scope, lambdas) -> _Bound:
        # A property of a structured property: the caller describes no structured type's members.
        self._name(node.children[0], scope, lambdas)
        raise FilterError("property", name=node.token.text)

    def _lambda(self, node, scope, lambdas) -> _Bound:
        collection = self._name(node.children[0], scope, lambdas)
        element = _element(collection.type)
        if element is None and not (self.open and collection.type is None):
            raise _mistyped("Any/All may only be used following a collection.")
        self.lambda_depth = max(self.lambda_depth, lambdas + 1)
        test = any if node.op == "any" else all
        if node.variable is None:
            return _Bound(BOOLEAN, lambda e, v: bool(collection.evaluate(e, v) or []))
        body = self.bind(node.children[1], {**scope, node.variable: element}, lambdas + 1)
        if body.type not in (BOOLEAN, None):
            raise _mistyped("The Any/All query expression must evaluate to a single boolean value.")
        var = node.variable

        def evaluate(e, v):
            items = collection.evaluate(e, v) or []
            return test(body.evaluate(e, {**v, var: item}) is True for item in items)

        return _Bound(BOOLEAN, evaluate)

    def _call(self, node, scope, lambdas) -> _Bound:
        name = ascii_lower(node.token.text)
        if name not in _FUNCTIONS:
            raise _mistyped(
                f"An unknown function with name '{node.token.text}' was found. This may also be a "
                "function import or a key lookup on a navigation property, which is not allowed."
            )
        args = [self.bind(a, scope, lambdas) for a in node.children]
        signatures, returns = _FUNCTIONS[name]
        if not any(
            len(sig) == len(args)
            and all(
                a.type in (p, None) or (p == INT32 and a.type == INT64) for a, p in zip(args, sig)
            )
            for sig in signatures
        ):
            considered = "; ".join(
                f"{name}({', '.join(f'{p} Nullable=true' for p in sig)})" for sig in signatures
            )
            raise _mistyped(
                f"No function signature for the function with name '{name}' matches the specified "
                f"arguments. The function signatures considered are: {considered}."
            )
        fns: dict[str, Callable[..., Any]] = {
            "startswith": lambda a, b: a.startswith(b),
            "endswith": lambda a, b: a.endswith(b),
            "contains": lambda a, b: b in a,
            "indexof": lambda a, b: a.find(b),
            "concat": lambda a, b: a + b,
            "tolower": str.lower,
            "toupper": str.upper,
            "trim": str.strip,
            "length": len,
            "substring": _substring,
        }
        fn = fns[name]
        fold = self._fold if returns == BOOLEAN else (lambda x: x)

        def evaluate(e, v):
            values = [fold(a.evaluate(e, v)) for a in args]
            if any(x is None for x in values):
                return None
            try:
                return fn(*values)
            except (TypeError, AttributeError):  # a served value of another type than declared
                return None

        return _Bound(returns, evaluate)

    def _not(self, node, scope, lambdas) -> _Bound:
        operand = self.bind(node.children[0], scope, lambdas)
        if operand.type not in (BOOLEAN, None):
            raise _mistyped(
                "A unary operator with an incompatible type was detected. Found operand type "
                f"'{operand.type}' for operator kind 'Not'."
            )
        flip = node.count % 2 == 1

        def evaluate(e, v):
            value = operand.evaluate(e, v)
            return None if value is None else (not value if flip else bool(value))

        return _Bound(BOOLEAN, evaluate)

    def _nary(self, node, scope, lambdas) -> _Bound:
        items = [self.bind(c, scope, lambdas) for c in node.children]
        so_far = items[0].type
        for item in items[1:]:
            if so_far not in (BOOLEAN, None) or item.type not in (BOOLEAN, None):
                raise _mistyped(
                    "A binary operator with incompatible types was detected. Found operand types "
                    f"'{so_far}' and '{item.type}' for operator kind '{_KIND[node.op]}'."
                )
            so_far = BOOLEAN
        test = all if node.op == "and" else any
        return _Bound(BOOLEAN, lambda e, v: test(i.evaluate(e, v) is True for i in items))

    def _binary(self, node, scope, lambdas) -> _Bound:
        op = node.op
        left, right = (self.bind(c, scope, lambdas) for c in node.children)
        if self.literal_first and op in _COMPARISONS and left.literal and not right.literal:
            left, right = right, left
        if op == "has":
            if left.type not in self.enums:
                raise _mistyped(_UNREACHABLE)
            right = self._against_enum(left.type, right)
            return _Bound(BOOLEAN, lambda e, v: left.evaluate(e, v) == right.evaluate(e, v))
        kind = _KIND[op]
        for side in (left, right):
            if _element(side.type) is not None and op in _COMPARISONS:
                raise _mistyped(
                    f"The operand for a binary operator '{kind}' is not a single value. Binary "
                    "operators require both operands to be single values."
                )
        if left.type in self.enums:
            right = self._against_enum(left.type, right)
        elif right.type in self.enums:
            left = self._against_enum(right.type, left)
        if op not in _COMPARISONS:
            if left.type not in _NUMERIC | {None} or right.type not in _NUMERIC | {None}:
                raise self._incompatible(left.type, right.type, kind)
            types = {left.type, right.type}
            if types & {SINGLE, DOUBLE}:
                result = DOUBLE if DOUBLE in types else SINGLE
                arithmetic, cast = _FLOAT_ARITHMETIC[op], float
            elif DECIMAL in types:
                result = DECIMAL
                arithmetic, cast = _DECIMAL_ARITHMETIC[op], decimal.Decimal
            else:
                result = INT64 if INT64 in types else INT32
                arithmetic, cast = _INTEGER_ARITHMETIC[op], int
            rounded = _f32 if result == SINGLE else (lambda x: x)

            def compute(e, v):
                a, b = left.evaluate(e, v), right.evaluate(e, v)
                if a is None or b is None:
                    return None
                return rounded(arithmetic(cast(a), cast(b)))

            return _Bound(result, compute)
        if not self._comparable(left.type, right.type):
            raise self._incompatible(left.type, right.type, kind)
        if op in _ORDERINGS and BOOLEAN in (left.type, right.type) and self.deferred is None:
            self.deferred = (
                f"The binary operator {kind} is not defined for the types '{_CLR[left.type]}' and "
                f"'{_CLR[right.type]}'."
            )
        strings = STRING in (left.type, right.type)
        enum = left.type if left.type in self.enums else None

        def compare(e, v):
            a, b = self._fold(left.evaluate(e, v)), self._fold(right.evaluate(e, v))
            if enum is not None:
                a, b = (self._enum_rank(enum, x) for x in (a, b))
            if op == "eq":
                return a == b
            if op == "ne":
                return a != b
            if strings:
                a, b = _string_order(a, b)
            elif a is None or b is None:
                return False
            try:
                return {"gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}[op]
            except (TypeError, ArithmeticError):  # a decimal ordered against NaN, or no order
                return False

        return _Bound(BOOLEAN, compare)

    def _in(self, node, scope, lambdas) -> _Bound:
        left = self.bind(node.children[0], scope, lambdas)
        if _element(left.type) is not None:
            raise _mistyped(
                "The left operand for the IN operation is not a single value. IN operations "
                "require the left operand to be a single value and the right operand to be a "
                "collection value."
            )
        if (
            left.type is not None
            and not left.type.startswith("Edm.")
            and left.type not in self.enums
        ):
            raise _mistyped(
                "The property with name '' was found with a value node of type 'PrimitiveValue'; "
                f"however, a resource value of type '{left.type}' was expected."
            )
        values = []
        for item in node.items:
            literal = item.kind != "name" or item.text in ("true", "false", "null")
            if (left.type == STRING and item.kind != "string") or not literal:
                raise _mistyped(f"String item should be single/double quoted: '{item.text}'.")
            if item.kind == "punct":
                raise _syntax("Invalid JSON. A token was not recognized in the JSON content.")
            bound = self._literal(Node("literal", item), scope, lambdas)
            if left.type in self.enums:
                bound = self._against_enum(left.type, bound)
            values.append(self._fold(bound.evaluate({}, {})))
        allowed = set(values)
        return _Bound(BOOLEAN, lambda e, v: self._fold(left.evaluate(e, v)) in allowed)

    def _against_enum(self, enum: str, side: _Bound) -> _Bound:
        """A string literal compared against an enum property is that enum's member; a number is
        its position."""
        if side.literal is not None and side.type == STRING:
            member = self._member(enum, side.literal.value)
            return _Bound(enum, lambda e, v: member, side.literal)
        if side.literal is not None and side.type in (INT32, INT64):
            members = self.enums[enum]
            index = side.literal.value
            member = members[index] if 0 <= index < len(members) else index
            return _Bound(enum, lambda e, v: member, side.literal)
        return side

    def _enum_rank(self, enum: str, value: Any) -> Any:
        members = self.enums[enum]
        return members.index(value) if value in members else value

    def _comparable(self, a: str | None, b: str | None) -> bool:
        if a is None or b is None or a == b:
            return True
        if a in _NUMERIC and b in _NUMERIC:
            return True
        return {a, b} == {DATETIME, DATE}

    def _incompatible(self, a, b, kind) -> FilterError:
        return _mistyped(
            "A binary operator with incompatible types was detected. Found operand types "
            f"'{a}' and '{b}' for operator kind '{kind}'."
        )

    def _fold(self, value: Any) -> Any:
        return value.casefold() if self.fold and isinstance(value, str) else value


class ExecutionError(Exception):
    """A filter that binds and fails as .NET runs it over an entity — a division by zero."""


def _int_divide(a: int, b: int) -> int:
    """C#'s integer division: it truncates toward zero, and zero is no divisor."""
    if b == 0:
        raise ExecutionError()
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def _int_remainder(a: int, b: int) -> int:
    """C#'s ``%``: the remainder takes the dividend's sign."""
    return a - b * _int_divide(a, b)


def _decimal_divide(a: decimal.Decimal, b: decimal.Decimal) -> decimal.Decimal:
    if b == 0:
        raise ExecutionError()
    with decimal.localcontext(prec=28):
        return a / b


def _decimal_remainder(a: decimal.Decimal, b: decimal.Decimal) -> decimal.Decimal:
    if b == 0:
        raise ExecutionError()
    return a % b


def _float_divide(a: float, b: float) -> float:
    """A floating division by zero is an infinity of the signs' product, or NaN for zero or NaN
    over zero."""
    if b == 0:
        if a == 0 or math.isnan(a):
            return math.nan
        return math.copysign(math.inf, a) * math.copysign(1.0, b)
    return a / b


def _float_remainder(a: float, b: float) -> float:
    return math.nan if b == 0 else math.fmod(a, b)


_SUM = {"add": lambda a, b: a + b, "sub": lambda a, b: a - b, "mul": lambda a, b: a * b}
_INTEGER_ARITHMETIC = {**_SUM, "div": _int_divide, "divby": _int_divide, "mod": _int_remainder}
_DECIMAL_ARITHMETIC = {
    **_SUM,
    "div": _decimal_divide,
    "divby": _decimal_divide,
    "mod": _decimal_remainder,
}
_FLOAT_ARITHMETIC = {
    **_SUM,
    "div": _float_divide,
    "divby": _float_divide,
    "mod": _float_remainder,
}


def _substring(text: str, start: int, length: int | None = None) -> str:
    """A negative start reads from the first character, and a negative length takes none."""
    start = max(start, 0)
    return text[start:] if length is None else text[start : start + max(length, 0)]


def _as_datetime(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value


def _count(node: Node, weight: Callable[[str | None], int], binder: _Binder) -> int:
    """ODL's validator node count: every node is one, under the property weights the caller
    measured, and an ``in`` with its list is one in all."""
    if node.kind == "name":
        found = binder.properties.get(ascii_lower(node.token.text))
        return weight(found[1]) if found else 1
    if node.kind == "in":
        return 1
    if node.kind == "not":
        return node.count + _count(node.children[0], weight, binder)
    if node.kind == "nary":
        return len(node.children) - 1 + sum(_count(c, weight, binder) for c in node.children)
    return 1 + sum(_count(c, weight, binder) for c in node.children)


def _clauses(node: Node, op: str) -> int:
    """How many children ``op`` joins across the filter; an ``in`` list's values are ``or``'s."""
    here = len(node.children) if node.kind == "nary" and node.op == op else 0
    if node.kind == "in" and op == "or":
        here = len(node.items)
    return here + sum(_clauses(c, op) for c in node.children if isinstance(c, Node))


class Filter:
    """A bound ``$filter``: call it on an entity's served JSON to test it."""

    def __init__(self, root: Node, bound: _Bound, binder: _Binder):
        self.root = root
        self._bound = bound
        self._binder = binder
        self.lambda_depth = binder.lambda_depth
        self.deferred = binder.deferred

    def __call__(self, entity: Mapping[str, Any]) -> bool:
        """Raises :class:`ExecutionError` where .NET's run of the filter fails."""
        try:
            return self._bound.evaluate(entity, {}) is True
        except RecursionError:  # a comparison chain thousands long; no limit stops it sooner
            return False

    def nodes(self, weight: Callable[[str | None], int]) -> int:
        try:
            return _count(self.root, weight, self._binder)
        except RecursionError:
            return _DEPTH_LIMIT

    def clauses(self, op: str) -> int:
        return _clauses(self.root, op)


def compile_filter(
    expr: str,
    properties: Mapping[str, str],
    *,
    enums: Mapping[str, Iterable[str]] | None = None,
    fold_case: bool = False,
    open_type: bool = False,
    literal_first: bool = False,
) -> Filter:
    """A :class:`Filter`, or :class:`FilterError`.

    ``properties`` maps each name the type declares to its EDM type (``Edm.String``,
    ``Collection(Edm.String)``, an enum's qualified name…); one the response does not carry
    compares as null. ``enums`` lists each enum type's members, in declared order. ``fold_case``
    compares strings without regard to case; ``open_type`` binds a name the type does not declare as
    an untyped property, for a caller that refuses it in words of its own; ``literal_first`` reads
    a comparison with a literal before a property as the property before the literal, operator
    unchanged, as a service that translates the filter rather than evaluating it may.
    """
    root = _Parser(expr).parse()
    binder = _Binder(
        properties,
        enums or {},
        fold_case=fold_case,
        open_type=open_type,
        literal_first=literal_first,
    )
    try:
        bound = binder.bind(root, {})
    except RecursionError:
        # ODL reads `a eq b eq c …` in a loop; binding the tree it builds is what recursion
        # bounds.
        raise FilterError("depth", "Recursion depth exceeded allowed limit.") from None
    if bound.type not in (BOOLEAN, None):
        raise _mistyped("The $filter expression must evaluate to a single boolean value.")
    return Filter(root, bound, binder)


# The ODL node a parsed `$orderby` term binds to, where it is not a property.
_ORDERBY_NODES = {
    "literal": "ConstantNode",
    "call": "SingleValueFunctionCallNode",
    "binary": "BinaryOperatorNode",
    "nary": "BinaryOperatorNode",
    "not": "UnaryOperatorNode",
    "in": "InNode",
    "lambda": "AnyNode",
    "path": "SingleValuePropertyAccessNode",
}


ORDERBY_NODES = frozenset(_ORDERBY_NODES.values())


def parse_orderby(expr: str) -> list[tuple[str, bool]]:
    """``$orderby``'s terms, in order, each with whether it descends. A term is an expression, as
    ODL parses one: a property is its name, and anything else the ODL node it is, such as
    ``ConstantNode``, for the caller to refuse."""
    parser = _Parser(expr)
    terms: list[tuple[str, bool]] = []
    while True:
        term = parser._expression(1)
        name = term.token.text if term.kind == "name" else _ORDERBY_NODES[term.kind]
        nxt = parser._peek()
        descending = False
        if nxt is not None and nxt.kind == "name" and ascii_lower(nxt.text) in ("asc", "desc"):
            descending = ascii_lower(nxt.text) == "desc"
            parser.i += 1
            nxt = parser._peek()
        terms.append((name, descending))
        if nxt is None:
            return terms
        if nxt.kind == "punct" and nxt.text == ",":
            parser.i += 1
            continue
        raise _syntax(f"Syntax error at position {nxt.end} in '{expr}'.")
