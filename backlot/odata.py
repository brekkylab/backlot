"""The subset of an OData ``$filter`` Microsoft Graph's Teams and directory methods were measured
to evaluate: comparisons (``eq ne gt ge lt le``), ``in``, ``startswith``/``endswith``/``contains``,
``and``/``or``/``not`` and parentheses, over string, boolean, null and number literals. Property
names and enum members are matched without regard to case, as Graph matches them.

The parser knows nothing about any vendor. A refusal is a :class:`FilterError` naming what was
wrong, and the router that asked turns it into its own vendor's words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

_TOKEN = re.compile(
    r"\s*(?:(?P<string>'(?:[^']|'')*')|(?P<number>-?\d+(?:\.\d+)?)|(?P<word>[A-Za-z_][\w.]*)"
    r"|(?P<punct>[(),]))"
)
_COMPARE = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a is not None and b is not None and a > b,
    "ge": lambda a, b: a is not None and b is not None and a >= b,
    "lt": lambda a, b: a is not None and b is not None and a < b,
    "le": lambda a, b: a is not None and b is not None and a <= b,
}
_FUNCTIONS = {
    "startswith": lambda a, b: isinstance(a, str) and isinstance(b, str) and a.startswith(b),
    "endswith": lambda a, b: isinstance(a, str) and isinstance(b, str) and a.endswith(b),
    "contains": lambda a, b: isinstance(a, str) and isinstance(b, str) and b in a,
}
_LITERALS = {"true": True, "false": False, "null": None}


class FilterError(ValueError):
    """``kind`` is ``expected`` (an operand is missing at ``position``), ``syntax`` (something else
    is wrong at ``position``), ``property`` (``name`` is not a property of the type), ``function``
    or ``operator`` (``name`` is one this caller refuses) or ``enum`` (``name`` is not a member of
    the property's enum)."""

    def __init__(self, kind: str, *, position: int = 0, name: str = ""):
        super().__init__(f"{kind} {name or position}")
        self.kind = kind
        self.position = position
        self.name = name


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    position: int


def _tokens(expr: str) -> list[_Token]:
    out, i = [], 0
    while i < len(expr):
        if expr[i:].strip() == "":
            break
        m = _TOKEN.match(expr, i)
        if m is None:
            raise FilterError("syntax", position=i + len(expr[i:]) - len(expr[i:].lstrip()))
        kind = m.lastgroup
        out.append(_Token(kind, m.group(kind), m.start(kind)))
        i = m.end()
    return out


Predicate = Callable[[Mapping[str, Any]], bool]
_Operand = tuple[Callable[[Mapping[str, Any]], Any], str | None]


class _Parser:
    def __init__(
        self,
        expr: str,
        properties: Iterable[str],
        *,
        fold_case: bool,
        enums: Mapping[str, Iterable[str]],
        refuse: Iterable[str],
    ):
        self.expr = expr
        self.tokens = _tokens(expr)
        self.i = 0
        self.properties = {p.casefold(): p for p in properties}
        self.fold = fold_case
        self.enums = {k: {m.casefold(): m for m in v} for k, v in enums.items()}
        self.refuse = set(refuse)

    def _peek(self) -> _Token | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _take(self) -> _Token:
        token = self._peek()
        if token is None:
            raise FilterError("expected", position=len(self.expr))
        self.i += 1
        return token

    def _expect(self, text: str) -> None:
        token = self._take()
        if token.text != text:
            raise FilterError("syntax", position=token.position)

    def parse(self) -> Predicate:
        predicate = self._or()
        if (extra := self._peek()) is not None:
            raise FilterError("syntax", position=extra.position)
        return predicate

    def _or(self) -> Predicate:
        left = self._and()
        while (t := self._peek()) is not None and t.text == "or":
            self.i += 1
            right = self._and()
            left = (lambda a, b: lambda e: a(e) or b(e))(left, right)
        return left

    def _and(self) -> Predicate:
        left = self._unary()
        while (t := self._peek()) is not None and t.text == "and":
            self.i += 1
            right = self._unary()
            left = (lambda a, b: lambda e: a(e) and b(e))(left, right)
        return left

    def _unary(self) -> Predicate:
        token = self._peek()
        if token is not None and token.text == "not":
            if "not" in self.refuse:
                raise FilterError("operator", name="not")
            self.i += 1
            inner = self._unary()
            return lambda e: not inner(e)
        if token is not None and token.text == "(":
            self.i += 1
            inner = self._or()
            self._expect(")")
            return inner
        if token is not None and token.kind == "word" and token.text.lower() in _FUNCTIONS:
            name = token.text.lower()
            if name in self.refuse:
                raise FilterError("function", name=name)
            self.i += 1
            self._expect("(")
            left, prop = self._operand()
            self._expect(",")
            right, _ = self._operand(enum_of=prop)
            self._expect(")")
            fn = _FUNCTIONS[name]
            return lambda e: fn(self._fold(left(e)), self._fold(right(e)))
        left, prop = self._operand()
        op = self._take()
        if op.text == "in":
            self._expect("(")
            values = [self._operand(enum_of=prop)[0]]
            while (t := self._peek()) is not None and t.text == ",":
                self.i += 1
                values.append(self._operand(enum_of=prop)[0])
            self._expect(")")
            return lambda e: self._fold(left(e)) in {self._fold(v(e)) for v in values}
        if op.text not in _COMPARE:
            raise FilterError("syntax", position=op.position)
        if op.text in self.refuse:
            raise FilterError("operator", name=op.text)
        right, _ = self._operand(enum_of=prop)
        compare = _COMPARE[op.text]
        return lambda e: compare(self._fold(left(e)), self._fold(right(e)))

    def _operand(self, enum_of: str | None = None) -> _Operand:
        """One side of a comparison, and the declared property it names (or None). A string literal
        compared against an enum property is read as that enum's member, in the declared case."""
        token = self._take()
        if token.kind == "string":
            value = token.text[1:-1].replace("''", "'")
            if enum_of is not None and enum_of in self.enums:
                member = self.enums[enum_of].get(value.casefold())
                if member is None:
                    raise FilterError("enum", name=value)
                value = member
            return (lambda e: value), None
        if token.kind == "number":
            number = float(token.text) if "." in token.text else int(token.text)
            return (lambda e: number), None
        if token.kind == "word":
            if token.text in _LITERALS:
                literal = _LITERALS[token.text]
                return (lambda e: literal), None
            name = self.properties.get(token.text.casefold())
            if name is None:
                raise FilterError("property", name=token.text)
            return (lambda e: e.get(name)), name
        raise FilterError("syntax", position=token.position)

    def _fold(self, value: Any) -> Any:
        return value.casefold() if self.fold and isinstance(value, str) else value


def compile_filter(
    expr: str,
    properties: Iterable[str],
    *,
    fold_case: bool = False,
    enums: Mapping[str, Iterable[str]] | None = None,
    refuse: Iterable[str] = (),
) -> Predicate:
    """A predicate over an entity's served JSON, or :class:`FilterError`.

    ``properties`` are the names the type declares (one the response does not carry compares as
    null); ``fold_case`` compares strings case-insensitively; ``enums`` names the properties whose
    string literal must be a member; ``refuse`` names functions (``contains``…) and operators
    (``ne``, ``not``) this caller does not evaluate.
    """
    return _Parser(expr, properties, fold_case=fold_case, enums=enums or {}, refuse=refuse).parse()
