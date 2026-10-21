"""Slack is compared against its reference documentation, because it publishes nothing else.

Every other document source here reads a machine-readable description its vendor maintains. Slack
has none: the OpenAPI document it once published is archived, its last content change dated
2020-10-06. What it maintains is the reference at docs.slack.dev, which serves every page as
markdown (``docs.slack.dev/llms.txt``). Markdown is not a schema, so each part is read only where
the page gives it one fixed spelling, measured 2026-10-21 across all 340 method pages:

* the inventory, off the method index: one table row per method, its name linked to its own page;
* a method's arguments, off its ``## Arguments`` section: 1516 lines of one shape, a name, an
  optional type out of six and ``Required`` or ``Optional``, and no bolded line of any other shape;
* what a method answers, off the JSON examples in its ``## Response`` section: 608 code blocks
  that open as JSON, 596 of them JSON as written and 3 more once an elision (``...``,
  ``// ...``) and a trailing comma are taken out, the 9 left all on methods Backlot does not serve;
* the fields of an object such a section links (``reference/objects/user-object.md``), off that
  page's Field/Type/Description tables, which name fields an example leaves out.

A page this cannot read that way is a contract that could not be read, never a divergence.

Methods, not verbs: a page names one ``http_method`` and slack.com answers a method over GET and
POST alike, so a method is one operation here, named as Slack names it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

import httpx

from backlot.fidelity import slack_probe
from backlot.fidelity.errors import FidelityError
from backlot.fidelity.findings import BREAKING, GAP, Finding
from backlot.fidelity.operations import _METHODS
from backlot.fidelity.slack_probe import Shape, diff_shapes, shape_of
from backlot.fidelity.surface import (
    EXTRA,
    IMPLEMENTED,
    MISSING,
    Measurement,
    OperationEntry,
    OperationSurface,
)

# A row of the index: the method's name linked to its own page, whose name the index spells in
# lower case.
_INDEX_ROW = re.compile(
    r"\|\s*\[([A-Za-z0-9._]+)\]\(https://docs\.slack\.dev/reference/methods/\1\.md\)", re.I
)
# A table's separator row, under its header row.
_SEPARATOR = re.compile(r"\|(?:\s*:?-+:?\s*\|)+")
_METHOD_NAME = re.compile(r'^method_name:\s*"?([A-Za-z0-9._]+)"?\s*$', re.M)


def _section(page: str, anchor: str) -> str:
    """The body of the ``## ... {#anchor}`` section, up to the next ``## `` heading."""
    found = re.search(rf"^## [^\n]*\{{#{anchor}\}}\n(.*?)(?=^## |\Z)", page, re.S | re.M)
    return found.group(1) if found else ""


# One argument, as every method page writes it; the type is left out on 194 of the 1516.
_ARGUMENT = re.compile(r"^\*\*`([^`]+)`\*\*(?:`([^`]*)`)?(?:Required|Optional)\s*$", re.M)
_BOLD_NAME = re.compile(r"^\*\*`", re.M)
# An argument's type as JSON names it. `integer` and `number` are one JSON type, as they are in
# `slack_probe.json_type`.
_ARGUMENT_TYPES = {
    "string": "string",
    "boolean": "boolean",
    "integer": "number",
    "number": "number",
    "array": "array",
    "object": "object",
}

# A fenced block, which the pages write on one line between the fences.
_BLOCK = re.compile(r"^```[a-z]*\n(.*?)\n```", re.S | re.M)
_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_ELIDED = re.compile(r"(?://\s*)?\.\.\.\s*,?")
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_OBJECT_LINK = re.compile(r"\((https://docs\.slack\.dev/reference/objects/[a-z-]+\.md)\)")

# A row's name in an object's field table, alone on its line.
_ROW_NAME = re.compile(r"`([a-z][a-z0-9_.]*)`")
# A field table's type cell as JSON names it, first match wins. Measured 2026-10-21 over the 513
# typed rows of the nine object pages: every cell reads as one of these but `Timestamp`, on the
# conversation object's `last_read`, which as Slack's timestamps go may be a string or a number.
# `user_id`, `team_id` and the other backticked id names are Slack's ids, all strings.
_FIELD_TYPES = (
    (re.compile(r"^Array\b|\\?\[\\?\]$"), "array"),
    (re.compile(r"^(String|Enum)\b|^`[a-z_]+`( \(String\))?$|\(String\)$"), "string"),
    (re.compile(r"^(Integer|Int|Number|Unix timestamp)\b"), "number"),
    (re.compile(r"^Boolean\b"), "boolean"),
    (re.compile(r"^(Object|Ref \(|\w+ object$)"), "object"),
)


class DocsTarget(Protocol):
    """What this module needs of a source. Structural, so the registry can import this module
    without this module importing the registry."""

    index_url: str
    mount: tuple[str, ...]


@dataclass(frozen=True)
class Method:
    """One Web API method: its arguments, each with its JSON type or ``""`` where none is stated,
    and the shape of what it answers, where that was read."""

    name: str
    arguments: dict[str, str] = field(default_factory=dict)
    response: Shape | None = None


def fetch_text(url: str, *, timeout: float) -> str:
    """One markdown page, with a failure that says which page and why."""
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        raise FidelityError(f"{url} unreachable: {e}") from e
    if response.status_code != 200:
        raise FidelityError(f"{url} answered {response.status_code}")
    return response.text


def documented_methods(index: str, url: str = "the index") -> dict[str, str]:
    """Every method the index lists, mapped to the URL of its own page.

    Every table row but a header and its separator must read as one, so a change to how the index
    spells a row is refused rather than leaving that method out of the inventory."""
    lines = index.splitlines()
    methods: dict[str, str] = {}
    unread = []
    for at, line in enumerate(lines):
        if not line.startswith("|") or _SEPARATOR.fullmatch(line.strip()):
            continue
        if at + 1 < len(lines) and _SEPARATOR.fullmatch(lines[at + 1].strip()):
            continue
        row = _INDEX_ROW.match(line)
        if row is None:
            unread.append(line)
        else:
            methods[row.group(1)] = (
                f"https://docs.slack.dev/reference/methods/{row.group(1).lower()}.md"
            )
    if unread:
        raise FidelityError(f"{url} has rows this does not read as a method's: {unread}")
    if not methods:
        raise FidelityError(f"{url} lists no methods; is it still Slack's reference index?")
    return methods


def arguments(page: str, url: str = "the page") -> dict[str, str]:
    """The arguments one method page declares, required and optional together, each with its JSON
    type or ``""`` where the page states none.

    Both, because the comparison is about which arguments a caller may send, and a vendor moving an
    argument from required to optional is not surface Backlot is missing."""
    section = _section(page, "arguments")
    found = _ARGUMENT.findall(section)
    if len(found) != len(_BOLD_NAME.findall(section)):
        raise FidelityError(f"{url} has an argument line this does not read")
    out = {}
    for name, stated in found:
        if stated and stated not in _ARGUMENT_TYPES:
            raise FidelityError(f"{url} types {name} as {stated!r}, which this does not read")
        out[name] = _ARGUMENT_TYPES.get(stated, "")
    return out


def loads(text: str) -> Any:
    """A JSON example as the reference writes it: JSON, or JSON once an elision and a trailing
    comma outside every string are taken out. Inside a string both are content — the user object's
    example spells its avatar URLs ``.../..._...__24.jpg``."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    parts, at = [], 0
    for string in _STRING.finditer(text):
        parts += [_ELIDED.sub("", text[at : string.start()]), string.group()]
        at = string.end()
    parts.append(_ELIDED.sub("", text[at:]))
    return json.loads(_TRAILING_COMMA.sub(r"\1", "".join(parts)))


def examples(text: str, url: str = "the page") -> list[Any]:
    """Every JSON value the fenced blocks of ``text`` hold. A block that does not open as JSON is a
    request or a snippet, not an example, and is passed over; one that opens as JSON and does not
    read as it is refused."""
    out = []
    for block in _BLOCK.findall(text):
        if not block.lstrip().startswith(("{", "[")):
            continue
        try:
            out.append(loads(block))
        except json.JSONDecodeError as e:
            raise FidelityError(f"{url} has an example this does not read as JSON: {e}") from e
    return out


def field_type(cell: str) -> str:
    """A field table's type cell as a JSON type, ``""`` for one this does not read."""
    cell = cell.removesuffix(" or null").strip()
    for pattern, json_name in _FIELD_TYPES:
        if pattern.search(cell):
            return json_name
    return ""


def field_tables(page: str) -> list[dict[str, str]]:
    """Each Field/Type/Description table on an object page, in page order: every row's field name
    with its JSON type, ``""`` where the row states none this reads.

    The pages render a table as one cell per line, so a table is its header cells and then rows,
    each opening on a line that holds only the field's name in backticks. A table with no Type
    column (the conversation object's booleans) types nothing, and a type cell that is a sentence
    is that row's description."""
    lines = [line.strip() for line in page.splitlines() if line.strip()]
    tables: list[dict[str, str]] = []
    typed: bool | None = None
    at = 0
    while at < len(lines):
        line = lines[at]
        if line.startswith("#"):
            typed = None
        elif line in ("Field", "Property") and lines[at + 1 : at + 2] in (
            ["Type"],
            ["Description"],
        ):
            typed = lines[at + 1] == "Type"
            tables.append({})
            at += 3 if typed else 2
            continue
        elif typed is not None and (row := _ROW_NAME.fullmatch(line)):
            # The type cell is taken with its row: a backticked id type (`user_id`) is spelled the
            # way a row's name is.
            cell = lines[at + 1] if typed and at + 1 < len(lines) else ""
            tables[-1][row.group(1)] = field_type(cell)
            at += 2 if typed else 1
            continue
        at += 1
    return tables


def _names(table: dict[str, str]) -> set[str]:
    return {name.split(".")[0] for name in table}


def _best(shape: Shape, names: set[str]) -> str | None:
    """The container of ``shape`` whose own fields share the most names with ``names``, the
    shallowest among equals; None where none shares any."""
    scored = [(len(shape.keys(c) & names), -len(c), c) for c in shape.containers]
    best = max(scored, default=(0, 0, ""))
    return best[2] if best[0] else None


def object_shape(page: str, url: str = "the page") -> Shape:
    """An object's documented fields, relative to the object itself: its tables' rows and its
    examples' fields.

    An example wraps the object (the user object's is ``{"user": {...}}``), so each is read from the
    container that shares the most names with the first table, which on every object page names
    the object's own fields. A later table goes where the examples' fields share the most names
    with it: the user object's second table names its ``profile`` fields without the prefix."""
    tables = field_tables(page)
    if not tables:
        raise FidelityError(f"{url} has no field table this reads")
    own = _names(tables[0])
    read = [shape_of(e, empty_object_entered=False) for e in examples(page, url)]
    placed = Shape.union(s.within(root) for s in read if (root := _best(s, own)) is not None)
    shapes = [placed]
    for table in tables:
        prefix = _best(placed, _names(table)) or ""
        fields: dict[str, frozenset[str]] = {}
        containers = {""}
        for name, json_name in table.items():
            fields[name] = frozenset({json_name} if json_name else ())
            parts = name.split(".")
            containers |= {".".join(parts[:i]) for i in range(1, len(parts))}
        shapes.append(Shape(fields, frozenset(containers)).under(prefix))
    return Shape.union(shapes)


def response_shape(page: str, read_object: Callable[[str], Shape], url: str = "the page") -> Shape:
    """What a method answers, as its page documents it: the union of its success examples, with
    each object the ``## Response`` section links placed where the examples carry it.

    Placed at the container sharing the most names with the object's own fields, and only where
    those are at least half of that container's: ``users.list`` carries the user object at
    ``members[]`` and ``users.info`` at ``user``."""
    section = _section(page, "response")
    success = [e for e in examples(section, url) if isinstance(e, dict) and e.get("ok") is True]
    if not success:
        raise FidelityError(f"{url} documents no success response this reads")
    documented = Shape.union(shape_of(e, empty_object_entered=False) for e in success)
    shapes = [documented]
    for link in dict.fromkeys(_OBJECT_LINK.findall(section)):
        obj = read_object(link)
        own = obj.keys("")
        at = _best(documented, own)
        if at is None or 2 * len(documented.keys(at) & own) < len(documented.keys(at)):
            raise FidelityError(f"{url} links {link}, and no example carries that object")
        shapes.append(obj.under(at))
    return Shape.union(shapes)


def from_backlot(spec: dict, mount: tuple[str, ...]) -> dict[str, Method]:
    """The Slack methods Backlot serves and their arguments, from the app's own ``/openapi.json``.

    One :class:`Method` per method rather than per verb, its arguments the union over the verbs it
    answers: Backlot answers every one over GET and POST from a single ``api_route``."""
    out: dict[str, Method] = {}
    for path, item in (spec.get("paths") or {}).items():
        if not any(path.startswith(m) for m in mount):
            continue
        name = path.rsplit("/", 1)[-1]
        params: dict[str, str] = {}
        for verb in _METHODS:
            operation = item.get(verb)
            if not isinstance(operation, dict):
                continue
            for p in operation.get("parameters") or ():
                if p.get("in") == "query" and p.get("name"):
                    declared = (p.get("schema") or {}).get("type", "")
                    params[p["name"]] = _ARGUMENT_TYPES.get(declared, "")
        out[name] = Method(name, params)
    return out


def diff_methods(backlot: dict[str, Method], real: dict[str, Method]) -> list[Finding]:
    """Every divergence, both directions: methods, their arguments and their answers."""
    out: list[Finding] = []
    for name in sorted(set(backlot) - set(real)):
        out.append(
            Finding(
                "extra_operation",
                BREAKING,
                name,
                "the vendor's reference documents no such method",
            )
        )
    for name in sorted(set(real) - set(backlot)):
        out.append(
            Finding(
                "missing_operation", GAP, name, "the vendor documents it; Backlot does not serve it"
            )
        )
    for name in sorted(set(backlot) & set(real)):
        mine, theirs = backlot[name], real[name]
        for argument in sorted(set(mine.arguments) - set(theirs.arguments)):
            out.append(
                Finding(
                    "extra_param",
                    BREAKING,
                    f"{name}?{argument}",
                    "Backlot accepts it; the vendor's reference documents no such argument",
                )
            )
        for argument in sorted(set(theirs.arguments) - set(mine.arguments)):
            out.append(
                Finding(
                    "missing_param",
                    GAP,
                    f"{name}?{argument}",
                    "the vendor's reference documents it; Backlot does not accept it",
                )
            )
        for argument in sorted(set(mine.arguments) & set(theirs.arguments)):
            ours, documented = mine.arguments[argument], theirs.arguments[argument]
            if ours and documented and ours != documented:
                out.append(
                    Finding(
                        "param_type_mismatch",
                        BREAKING,
                        f"{name}?{argument}",
                        f"vendor: {documented}, Backlot: {ours}",
                    )
                )
        if mine.response is not None and theirs.response is not None:
            out += diff_shapes(name, mine.response, theirs.response)
    return sorted(out, key=lambda f: (f.severity != BREAKING, f.path, f.kind))


def inventory(backlot: Iterable[str], real: Iterable[str]) -> OperationSurface:
    """One entry per method either side has, named as Slack names it; a method Backlot alone has
    carries no vendor id."""
    backlot, real = set(backlot), set(real)
    entries = []
    for name in sorted(backlot | real):
        if name not in backlot:
            status = MISSING
        else:
            status = IMPLEMENTED if name in real else EXTRA
        entries.append(OperationEntry(name, status, name if name in real else ""))
    return OperationSurface(tuple(entries))


def measure(source: DocsTarget, *, timeout: float = 120.0) -> Measurement:
    """This module's entry point: read both sides and compare them.

    The index is the whole inventory, and a method's own page is read only where Backlot serves it:
    the arguments and answers of a method Backlot does not serve say nothing its absence has not
    already said. An object page is read once however many methods link it."""
    from backlot.main import app

    pages = documented_methods(fetch_text(source.index_url, timeout=timeout), source.index_url)
    served = from_backlot(app.openapi(), source.mount)
    objects: dict[str, Shape] = {}

    def read_object(url: str) -> Shape:
        if url not in objects:
            objects[url] = object_shape(fetch_text(url, timeout=timeout), url)
        return objects[url]

    real = {name: Method(name) for name in pages}
    for name in sorted(set(served) & set(real)):
        page = fetch_text(pages[name], timeout=timeout)
        stated = _METHOD_NAME.search(page)
        # The index and the page must agree on which method this is: a redirect that answered
        # another method's page would otherwise be read as this one's.
        if not stated or stated.group(1) != name:
            raise FidelityError(
                f"{pages[name]} states method_name "
                f"{stated.group(1) if stated else '(none)'}, not {name}"
            )
        real[name] = Method(
            name, arguments(page, pages[name]), response_shape(page, read_object, pages[name])
        )
    answered = slack_probe.answers(timeout=timeout)
    backlot = {name: Method(name, m.arguments, answered.get(name)) for name, m in served.items()}
    return Measurement(diff_methods(backlot, real), inventory(backlot, real))
