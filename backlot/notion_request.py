"""What api.notion.com reads off a request before a route serves it, and the words it refuses one
in.

Three readers, in the order a request meets them. Express's ``body-parser`` reads a JSON body
before anything else is looked at (:func:`read_body`). The validator then reads the query string as
Node's ``qs`` parses it (:func:`query_members`), each value coerced the way the validator coerces
a query value (:func:`coerce`), and each member of the query string and of the body against the
route's schema (:class:`Obj`, :class:`Union` and the nodes beside them), refusing one in the words
:func:`refusal_message` puts together. Every value it quotes back is quoted as :func:`quoted`
quotes it.

Measured against api.notion.com on 2026-10-21 (UTC) with an internal integration's token, under
Notion-Version 2025-09-03 and, where a docstring says so, 2022-06-28.
"""

from __future__ import annotations

import math
import re
import urllib.parse
from dataclasses import dataclass, replace
from typing import Any

from backlot.graphql import body as node_body

#: A member the request did not send, which a refusal names `undefined`.
MISSING = object()

# --------------------------------------------------------------------------- values

#: What JavaScript trims off a string, in `Number()` as in `String.prototype.trim`: the WhiteSpace
#: and LineTerminator code points. In a query value a vertical tab, a form feed, U+2028 and U+FEFF
#: are trimmed; U+0085, U+200B, U+2060 and U+001C are not, and leave the value a string
#: (2026-10-21).
JS_SPACE = "".join(
    map(
        chr,
        (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680, *range(0x2000, 0x200B))
        + (0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF),
    )
)
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+[.]?[0-9]*|[.][0-9]+)(?:[eE][+-]?[0-9]+)?")
_RADIX = {"0x": 16, "0X": 16, "0o": 8, "0O": 8, "0b": 2, "0B": 2}
_RADIX_DIGITS = {16: re.compile("[0-9a-fA-F]+"), 8: re.compile("[0-7]+"), 2: re.compile("[01]+")}


def js_number(text: str) -> float | None:
    """``Number(text)`` where it is finite, or None where JavaScript reads NaN or an infinity: the
    text trimmed of :data:`JS_SPACE`, an empty one zero, a decimal with an optional sign, or a
    hexadecimal, octal or binary literal with no sign, its prefix and digits in either case:
    `0XfF`, `0O17` and `0B11` are 255, 15 and 3, and `0o8`, `0b2` and `0xg` stay strings
    (2026-10-21). Python's ``float`` and ``int`` read an underscore besides, and ``float`` `nan`,
    which JavaScript does not: `0x1_0` stays a string."""
    text = text.strip(JS_SPACE)
    if not text:
        return 0.0
    radix = _RADIX.get(text[:2])
    if radix is not None and _RADIX_DIGITS[radix].fullmatch(text[2:]):
        try:
            value = float(int(text[2:], radix))
        except OverflowError:  # past the largest double, which JavaScript reads as Infinity
            return None
    elif _DECIMAL.fullmatch(text):
        value = float(text)
    else:
        return None
    return value if math.isfinite(value) else None


def coerce(raw: str) -> Any:
    """A query value as the validator reads it: `true` and `false` in any case are booleans, a
    value ``Number()`` reads as a whole number is that number, and anything else stays the string
    it was. Measured on `users?foo=…`, whose refusal quotes what it read: `01`, `1.0`, `5.`,
    `0x10`, `0b11`, an empty value and a space read as 1, 1, 5, 16, 3, 0 and 0, and `1.5`, `.5`,
    `1e-7`, `1e400`, `Infinity`, `-0x10`, `1_0`, `null` and ` true` stay strings."""
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    number = js_number(raw)
    if number is None or number != math.floor(number):
        return raw
    return node_body.Double(number)


def stringify(value: Any) -> str:
    """``JSON.stringify(value)`` for what a request can hold: a number written as JavaScript writes
    one (`1e+21`, `1.2345678901234568e+29`), a number that is not finite as `null`, and a string
    through :func:`backlot.graphql.body.node_json`."""
    if value is None or value is True or value is False:
        return {None: "null", True: "true", False: "false"}[value]
    if isinstance(value, float):
        return repr(node_body.Double(value)) if math.isfinite(value) else "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return node_body.node_json(value).decode()
    if isinstance(value, list):
        return "[" + ",".join(stringify(v) for v in value) + "]"
    return "{" + ",".join(f"{stringify(k)}:{stringify(v)}" for k, v in value.items()) + "}"


#: The longest quote a refusal carries whole.
_QUOTE_LIMIT = 55


def quoted(value: Any) -> str:
    """``value`` as a refusal quotes it after `instead was`: ``JSON.stringify``'s text, cut to its
    first 55 UTF-16 units with `...` after them where it is longer, and `undefined` for a member
    not sent. A 54-character value is 56 units quoted and cut, a 53-character one is quoted whole
    (2026-10-04 on `comments?block_id=` and `Notion-Version`, 2026-10-21 on `users?foo=`), and a
    cut can fall between the two halves of a character past U+FFFF, which leaves the first half on
    its own: 53 `x` and U+1F600 came back as the `x`s, `\\ud83d` and `...`, 52 of them with the
    whole character (2026-10-21)."""
    if value is MISSING:
        return "undefined"
    units = stringify(value).encode("utf-16-le", "surrogatepass")
    if len(units) <= 2 * _QUOTE_LIMIT:
        return units.decode("utf-16-le", "surrogatepass")
    return units[: 2 * _QUOTE_LIMIT].decode("utf-16-le", "surrogatepass") + "..."


# --------------------------------------------------------------------------- the query string


@dataclass
class QueryMember:
    """One top-level key of the query string as ``qs`` builds it: ``kind`` is `value` for one
    plain value, `list` for an array and `object` for an object, and ``value`` the first value sent
    under the key, the one a `value` holds."""

    key: str
    kind: str
    value: str


_SEGMENT = re.compile(r"\[[^\[\]]*\]")
#: An index as ``String(index)`` writes one: `[01]`, `[00]`, `[²]` and `[٣]` are objects
#: (2026-10-21).
_INDEX = re.compile(r"0|[1-9][0-9]*")
_MALFORMED_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
#: The highest index ``qs`` reads as an array's (its `arrayLimit`): `foo[20]` is a list and
#: `foo[21]` an object, 2026-10-21.
_ARRAY_LIMIT = 20


def _qs_decode(text: str) -> str:
    """``qs``'s decoder: `+` as a space, then ``decodeURIComponent``, the text as it stands where
    that throws."""
    text = text.replace("+", " ")
    if _MALFORMED_ESCAPE.search(text):
        return text
    try:
        return urllib.parse.unquote_to_bytes(text).decode("utf-8")
    except UnicodeDecodeError:
        return text


def _shape(segments: list[str]) -> str:
    """The kind of value a key's first bracket gives it: `[]`, or an index from 0 to
    :data:`_ARRAY_LIMIT` written as ``String(index)`` writes it, is a list; any other bracket is an
    object."""
    inner = segments[0][1:-1]
    if inner == "":
        return "list"
    return "list" if _INDEX.fullmatch(inner) and int(inner) <= _ARRAY_LIMIT else "object"


#: How ``qs`` merges a key's next value into what it holds: a plain value that meets another value
#: becomes a list, a list that meets an object an object, an object stays one.
_MERGED = {
    ("value", "value"): "list",
    ("value", "list"): "list",
    ("value", "object"): "list",
    ("list", "value"): "list",
    ("list", "list"): "list",
    ("list", "object"): "object",
    ("object", "value"): "object",
    ("object", "list"): "object",
    ("object", "object"): "object",
}


def query_members(query: str) -> dict[str, QueryMember]:
    """The query string's top-level keys as ``qs`` parses them, in the order each first appears.

    A part splits at its first `=` (after `]` when it holds `]=`), a part with none is the key
    with an empty value, and a key that decodes to nothing is dropped (`?=1` is the bare request).
    A repeated key is a list; a key's brackets are read off its decoded text, so `foo%5B%5D` is
    `foo[]`, and `foo[` and `foo]` are keys of their own."""
    members: dict[str, QueryMember] = {}
    for part in query.split("&"):
        cut = part.find("]=")
        cut = part.find("=") if cut == -1 else cut + 1
        key, value = (part, "") if cut == -1 else (part[:cut], part[cut + 1 :])
        key, value = _qs_decode(key), _qs_decode(value)
        first = _SEGMENT.search(key)
        parent = key[: first.start()] if first else key
        segments = _SEGMENT.findall(key)
        if not parent and segments:
            # A key that opens with a bracket names the member inside it, and an index or `[]` the
            # first item of a list, `0`: `[a]=1` is `a` and `[]=1` and `[0]=1` are `0` (2026-10-21).
            inner = segments.pop(0)[1:-1]
            parent = "0" if _shape([f"[{inner}]"]) == "list" else inner
        if not parent:
            continue
        kind = _shape(segments) if segments else "value"
        member = members.get(parent)
        if member is None:
            members[parent] = QueryMember(parent, kind, value)
        else:
            member.kind = _MERGED[member.kind, kind]
    return members


def query_shape_refusal(
    members: dict[str, QueryMember], *, own: dict[str, str | None]
) -> str | None:
    """The message for the first key that is not one value, in the order the keys came, or None.
    Measured on each route this router serves: `foo=1&foo=2`, `foo[]=1` and `foo[0]=1` conflict,
    and `foo[a]=1` is an object, ahead of an unknown key and of a member the route reads
    (`foo[a]=1&page_size=abc` names `foo`). ``own`` maps each key read in its own way to the
    message its reading refuses it with, or None: `filter_properties` takes a list, and
    `filter_properties[x]=a` is refused in its place among the keys, after `foo[a]=1` sent ahead of
    it and before `foo=1&foo=2` sent after it (2026-10-21)."""
    for member in members.values():
        if member.key in own:
            if own[member.key] is not None:
                return own[member.key]
            continue
        if member.kind == "value":
            continue
        if member.kind == "list":
            return f'Conflicting values for "{member.key}" in query parameters.'
        return (
            f'The value of "{member.key}" in query parameters must be a string, number, or boolean.'
        )
    return None


# --------------------------------------------------------------------------- schemas


@dataclass(frozen=True)
class Problem:
    """One thing a member fails: where it is, what it should be, and what it was. ``expected`` is
    `defined` for a member missing, `not present` for one that is not allowed, and otherwise the
    descriptions a value would have had to meet; ``optional`` adds `undefined` to them."""

    path: tuple[str, ...]
    value: Any
    expected: tuple[str, ...]
    optional: bool = False

    def line(self) -> str:
        where = ".".join(self.path).replace(".[", "[")
        described = list(self.expected) + (["`undefined`"] if self.optional else [])
        if len(described) == 1:
            said = described[0]
        elif len(described) == 2:
            said = f"{described[0]} or {described[1]}"
        else:
            said = ", ".join(described[:-1]) + f", or {described[-1]}"
        return f"{where} should be {said}, instead was `{quoted(self.value)}`."


def refusal_message(root: str, problems: list[Problem]) -> str:
    """The `validation_error` message for ``problems`` found under ``root`` (`query` or `body`):
    the one line after `failed validation:`, or each line after `failed validation. Fix one:`."""
    if len(problems) == 1:
        return f"{root} failed validation: {problems[0].line()}"
    return f"{root} failed validation. Fix one:\n" + "\n".join(p.line() for p in problems)


class Node:
    """What a value has to be. :meth:`check` answers the problems it finds, none when the value
    holds."""

    def check(self, value: Any, path: tuple[str, ...]) -> list[Problem]:
        raise NotImplementedError


@dataclass(frozen=True)
class Is(Node):
    """A value of one JSON type, ``kind`` being the words a refusal names it by. A boolean is not a
    number here, as it is not in JavaScript."""

    kind: str

    def check(self, value, path):
        holds = {
            "a string": isinstance(value, str),
            "a number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "a boolean": isinstance(value, bool),
            "an object": isinstance(value, dict),
            "an array": isinstance(value, list),
        }[self.kind]
        return [] if holds else [Problem(path, value, (self.kind,))]


@dataclass(frozen=True)
class OneOf(Node):
    """One of these strings, each described as its JSON between backticks, or one of ``unnamed``,
    which a refusal does not list."""

    choices: tuple[str, ...]
    unnamed: tuple[str, ...] = ()

    def check(self, value, path):
        if value in self.choices or value in self.unnamed:
            return []
        return [Problem(path, value, tuple(f'`"{c}"`' for c in self.choices))]


@dataclass(frozen=True)
class Whole(Node):
    """A number with no fraction, `an integer`."""

    def check(self, value, path):
        if math.isfinite(value) and value == math.floor(value):
            return []
        return [Problem(path, value, ("an integer",))]


@dataclass(frozen=True)
class Bound(Node):
    """A number no lower than ``low`` and, where there is one, no higher than ``high``: `≥ `1`` and
    `≤ `100``."""

    low: int
    high: int | None = None

    def check(self, value, path):
        if value < self.low:
            return [Problem(path, value, (f"≥ `{self.low}`",))]
        if self.high is not None and value > self.high:
            return [Problem(path, value, (f"≤ `{self.high}`",))]
        return []


_DASHED_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@dataclass(frozen=True)
class Uuid(Node):
    """A string in a uuid's dashed form, in either case, `a valid uuid`; with ``dashless``, 32
    lower-case hexadecimal digits too. Measured as a cursor's shape on `blocks/{id}/children`,
    `comments` and `search` (a dashless id, braces, a space at either end and a `g` among the
    digits are each refused, an upper-case dashed one passes to the cursor check), and as
    `comments`' `block_id`, which also reads the dashless form in lower case."""

    dashless: bool = False

    def check(self, value, path):
        if _DASHED_UUID.fullmatch(value) or (self.dashless and re.fullmatch("[0-9a-f]{32}", value)):
            return []
        return [Problem(path, value, ("a valid uuid",))]


@dataclass(frozen=True)
class Every(Node):
    """Each of ``nodes`` on the same value in turn, the first problem answering."""

    nodes: tuple[Node, ...]

    def check(self, value, path):
        for node in self.nodes:
            problems = node.check(value, path)
            if problems:
                return problems
        return []


@dataclass(frozen=True)
class Items(Node):
    """An array whose each item is ``item``; the first item with a problem answers."""

    item: Node

    def check(self, value, path):
        if not isinstance(value, list):
            return [Problem(path, value, ("an array",))]
        for i, each in enumerate(value):
            problems = self.item.check(each, (*path, f"[{i}]"))
            if problems:
                return problems
        return []


@dataclass(frozen=True)
class Field:
    """One member of an object: its key, what its value has to be, and whether it must be sent."""

    key: str
    node: Node
    required: bool = False


@dataclass(frozen=True)
class Obj(Node):
    """An object with these members, read in this order: the first one missing or failing answers
    alone, and when every one holds, each member it does not name is `not present`, in the order
    sent, unless ``open``. A member that may go unsent adds `undefined` to what its own value
    should have been."""

    fields: tuple[Field, ...]
    open: bool = False

    def check(self, value, path):
        if not isinstance(value, dict):
            return [Problem(path, value, ("an object",))]
        for f in self.fields:
            got = value.get(f.key, MISSING)
            here = (*path, f.key)
            if got is MISSING:
                if f.required:
                    return [Problem(here, MISSING, ("defined",))]
                continue
            problems = f.node.check(got, here)
            if problems:
                return [
                    replace(p, optional=not f.required) if p.path == here else p for p in problems
                ]
        if self.open:
            return []
        named = {f.key for f in self.fields}
        return [
            Problem((*path, k), v, ("not present",)) for k, v in value.items() if k not in named
        ]


@dataclass(frozen=True)
class Union(Node):
    """A value that holds against any one of ``branches``. When none holds, what each branch says
    is put together: the branches whose problems lie deepest answer, and their problems at one
    path become one problem naming everything any of them expected, in the order they came, with
    `undefined` last when any of them was optional. Each union below was measured so, its refusal
    listing the paths in the order its branches meet them."""

    branches: tuple[Node, ...]

    def check(self, value, path):
        found = [branch.check(value, path) for branch in self.branches]
        if not all(found):
            return []
        deepest = max(len(p.path) for problems in found for p in problems)
        merged: dict[tuple[str, ...], Problem] = {}
        for problems in found:
            if max(len(p.path) for p in problems) != deepest:
                continue
            for p in problems:
                held = merged.get(p.path)
                if held is None:
                    merged[p.path] = p
                    continue
                expected = held.expected + tuple(e for e in p.expected if e not in held.expected)
                merged[p.path] = replace(
                    held, expected=expected, optional=held.optional or p.optional
                )
        return list(merged.values())


class Anything(Node):
    """Any object, where the parts of it a refusal could name are not modelled here."""

    def check(self, value, path):
        return [] if isinstance(value, dict) else [Problem(path, value, ("an object",))]


# --------------------------------------------------------------------------- the routes' schemas

_STRING = Is("a string")
_NUMBER = Is("a number")
_DIRECTION = OneOf(("ascending", "descending"))

#: `users`: a cursor that is any string, and a page size that is any number.
USERS_QUERY = Obj((Field("start_cursor", _STRING), Field("page_size", _NUMBER)))
#: `blocks/{id}/children`: a cursor in a uuid's shape.
CHILDREN_QUERY = Obj((Field("start_cursor", Every((_STRING, Uuid()))), Field("page_size", _NUMBER)))
# `pages/{id}` and `comments` serve a key they do not name (`foo=1&bar=2` is 200 on each), where
# every other route refuses it (2026-10-21), so their schemas are `open`.
#: `pages/{id}`.
PAGE_QUERY = Obj((), open=True)
#: `comments`, which reads `block_id` first.
COMMENTS_QUERY = Obj(
    (
        Field("block_id", Every((_STRING, Uuid(dashless=True))), required=True),
        Field("start_cursor", Every((_STRING, Uuid()))),
        Field("page_size", Every((_NUMBER, Bound(low=1, high=100)))),
    ),
    open=True,
)
#: A route's query string or body where it reads no member, and refuses each one sent. Every
#: `GET` refuses a body's members this way, after its query string and before its lookup: `{"a":1}`
#: is `body.a should be not present` on each of the eight, an unknown page or block included
#: (2026-10-21).
NOTHING = Obj(())

#: `search`'s `sort`: by a timestamp, by relevance, or any of the three members as a string, in the
#: order the refusals list their lines (`{"direction":"up"}` names `timestamp`, then `property`,
#: then `direction`). What the third admits, :func:`search_sort_refusal` refuses after the schema.
_SEARCH_SORT = Union(
    (
        Obj(
            (Field("timestamp", OneOf(("last_edited_time",)), True), Field("direction", _DIRECTION))
        ),
        Obj((Field("property", OneOf(("relevance",)), True),)),
        Obj(
            (
                Field("property", _STRING),
                Field("timestamp", _STRING),
                Field("direction", _DIRECTION),
            )
        ),
    )
)


def _search_filter(legacy: bool) -> Union:
    """`search`'s `filter`: an object type, or the trash alone. 2022-06-28 also serves `database`,
    which it does not list when it refuses a value: `{"value":"bogus"}` names `"page"` or
    `"data_source"` under both versions, and `{"in_trash":false,"value":"database"}` quotes
    `"database"` back (2026-10-21)."""
    value = OneOf(("page", "data_source"), ("database",) if legacy else ())
    return Union(
        (
            Obj(
                (
                    Field("property", OneOf(("object",)), True),
                    Field("value", value, True),
                    Field("in_trash", Is("a boolean")),
                )
            ),
            Obj((Field("in_trash", Is("a boolean"), True),)),
        )
    )


def search_body(legacy: bool) -> Obj:
    """`search`'s body under 2022-06-28 (``legacy``) or a later version, its members in the order
    the refusals read them: `{"query":1,"sort":"x"}` is the `sort` refusal and
    `{"sort":{"timestamp":"created_time"},"query":1}` the `query` one, since that `sort` holds
    against the schema."""
    return Obj(
        (
            Field("sort", _SEARCH_SORT),
            Field("query", _STRING),
            Field("start_cursor", Every((_STRING, Uuid()))),
            Field("page_size", _NUMBER),
            Field("filter", _search_filter(legacy)),
        )
    )


#: The filter types a data source query's `filter` reads by a `property`, in the order its refusal
#: lists them.
_PROPERTY_FILTERS = (
    "title",
    "rich_text",
    "number",
    "checkbox",
    "select",
    "multi_select",
    "status",
    "date",
    "people",
    "files",
    "url",
    "email",
    "phone_number",
    "relation",
    "created_by",
    "created_time",
    "last_edited_by",
    "last_edited_time",
    "formula",
    "unique_id",
    "rollup",
    "verification",
)


def _condition_branches() -> tuple[Node, ...]:
    """One branch per filter type, each its condition object and the `property` it reads, then the
    two timestamps read by `timestamp`. A condition object's own members are not checked here."""
    by_property = tuple(
        Obj((Field(key, Anything(), True), Field("property", _STRING, True)))
        for key in _PROPERTY_FILTERS
    )
    by_timestamp = tuple(
        Obj((Field(key, Anything(), True), Field("timestamp", _STRING, True)))
        for key in ("created_time", "last_edited_time")
    )
    return by_property + by_timestamp


def _compounds(element: Node) -> tuple[Obj, ...]:
    """`or` and `and`, each an array of ``element``; another member beside one is not refused
    (`{"and":[],"or":[]}` is served)."""
    return tuple(Obj((Field(key, Items(element), True),), open=True) for key in ("or", "and"))


#: A condition two compounds deep, where the compounds are not among the branches
#: (`{"and":[{"or":[{}]}]}` names `title` … `verification` and stops).
_INNERMOST = Union(_condition_branches())
#: A condition inside a top-level compound: the filter types, then the two compounds.
_INNER = Union(_condition_branches() + _compounds(_INNERMOST))
#: A data source query's `filter`: the two compounds first at the top, last inside one, as the
#: refusals list them (`{}` names `or`, `and`, `title` …; `{"and":[{}]}` names `title` … `and`).
_QUERY_FILTER = Union(_compounds(_INNER) + _condition_branches())
_QUERY_SORT = Union(
    (
        Obj((Field("property", _STRING, True), Field("direction", _DIRECTION, True)), open=True),
        Obj(
            (
                Field("timestamp", OneOf(("created_time", "last_edited_time")), True),
                Field("direction", _DIRECTION, True),
            ),
            open=True,
        ),
    )
)
#: A data source query's body, and 2022-06-28's database query's: `{"sorts":"x","filter":"x"}` is
#: the `sorts` refusal, `{"filter":"x","page_size":"x"}` the `filter` one and
#: `{"page_size":"x","start_cursor":5}` the `start_cursor` one.
QUERY_BODY = Obj(
    (
        Field("sorts", Items(_QUERY_SORT)),
        Field("filter", _QUERY_FILTER),
        Field("start_cursor", _STRING),
        Field("page_size", Every((_NUMBER, Whole(), Bound(low=1)))),
        Field("result_type", OneOf(("page", "data_source"))),
    )
)


def search_sort_refusal(sort: dict) -> str | None:
    """What `search` says, after the schema, to a `sort` the schema admits and the route does not
    serve, or None. The words carry no `failed validation`, and the property comes back unquoted
    (`instead was `x``), as real wrote it on 2026-10-21."""
    if not sort:
        return (
            'body.sort should specify either `{ "property": "relevance" }` or '
            '`{ "timestamp": "last_edited_time", "direction": "ascending" | "descending" }`.'
        )
    if "property" in sort:
        if sort["property"] != "relevance":
            return f'body.sort.property should be "relevance", instead was `{sort["property"]}`.'
        if "timestamp" in sort or "direction" in sort:
            return (
                'body.sort with `{ "property": "relevance" }` cannot include `timestamp` or '
                "`direction`."
            )
        return None
    if sort.get("timestamp") != "last_edited_time":
        return 'body.sort.timestamp should be "last_edited_time" when sorting by timestamp.'
    if "direction" not in sort:
        return (
            'body.sort.direction should be defined when body.sort.timestamp is "last_edited_time".'
        )
    return None


# --------------------------------------------------------------------------- the body


#: The most bytes `body-parser` reads from a Notion request, its `500kb`: a body of 512000 bytes
#: was served and one of 512001 refused, 2026-10-21.
NOTION_LIMIT = 500 * 1024


def is_json(content_type: str) -> bool:
    """Whether `body-parser`'s JSON reader reads a body sent under ``content_type``: the
    ``content-type`` package must parse the header, and its type be `application/json` in any case.
    `application/json;`, an empty `charset=` and `application/json, text/plain` do not parse, and
    `text/json`, `application/merge-patch+json` and `*/*` are other types, so each of those bodies
    goes unread (2026-10-21). Spaces and tabs around the type are read past, and a no-break space
    (0xA0) or 0x85 there is not."""
    head, semicolon, rest = content_type.partition(";")
    if head.strip(" \t").lower() != "application/json":
        return False
    rest = semicolon + rest
    at = 0
    while at < len(rest):
        found = node_body.PARAMETER.match(rest, at)
        if found is None:
            return False
        at = found.end()
    return True


@dataclass(frozen=True)
class Unreadable:
    """A body `body-parser` refuses: the status, and the `name` and `message` Notion's error
    handler answers with, beside an `errorId` and no `request_id`. A 413 carries a `code` too."""

    status: int
    name: str
    message: str


def read_body(raw: bytes, content_type: str, content_encoding: str) -> Any:
    """The body a Notion route reads, or the :class:`Unreadable` or `invalid_json` refusal Express
    answers it with before the route, the credential and the version are looked at.

    Only a JSON body is read (:func:`is_json`); any other is no body at all, whatever it holds. A
    JSON one goes through :func:`backlot.graphql.body.body_parser_text` with Notion's limit, and
    each refusal it makes is Express's: `latin1`, `utf8` and `utf-nope` charsets and a `bogus` or
    `br` encoding drew the 415, a body one byte past the limit and a gzip body inflating past it the
    413, and a gzip body that was not one the 400. An empty body is no members, and a body that does
    not open with `{` or `[`, or that ``JSON.parse`` refuses, is `invalid_json` (a JSON scalar, a
    body of spaces, `{"a":1,}`, `NaN`, a UTF-8 body sent as `utf-16le`). `{` drew `invalid_json`
    with no credential, a token that does not resolve, no `Notion-Version`, one it does not publish,
    on a path no route serves, on `/` and on a `GET`, and `latin1`, `bogus` and a body of 1 MB drew
    theirs with no credential (2026-10-21). Returns ``"invalid_json"`` for that refusal, since its
    words are the router's."""
    if not is_json(content_type):
        return {}
    try:
        text = node_body.body_parser_text(
            raw, content_type, content_encoding, limit=NOTION_LIMIT, brotli=False
        )
    except node_body.Unread as unread:
        if unread.status == 413:
            return Unreadable(413, "PayloadTooLargeError", "Request body too large.")
        return Unreadable(unread.status, "UnknownApiError", "Something went wrong.")
    if not text:
        return {}
    if node_body.strict_violation(text) is not None:
        return "invalid_json"
    try:
        return node_body.parse(text)
    except node_body.NotJson:
        return "invalid_json"
