"""Slack is compared against its reference documentation, the one contract it maintains.

Every other document source here reads a machine-readable description its vendor maintains. Slack
maintains none: its OpenAPI document is archived, its last content change dated 2020-10-06, and the
copy `api.slack.com` still serves is as old. What it maintains is the reference at docs.slack.dev,
which serves every page as markdown (``docs.slack.dev/llms.txt``). Markdown is not a schema, so each
part is read only where the page gives it one fixed spelling (``docs/fidelity.md``, "Slack is read
off its reference documentation", carries the measurement of each):

* the inventory, off the method index: one table row per method, its name linked to its own page;
* a method's arguments, off its ``## Arguments`` section: one line each, a bolded code name, an
  optional type and ``Required`` or ``Optional``;
* what a method answers, off the JSON examples in its ``## Response`` section;
* the fields of an object such a section links (``reference/objects/user-object.md``), off that
  page's Field/Type/Description tables, which name fields an example leaves out.

A page this cannot read that way is a contract that could not be read, never a divergence.

Methods, not verbs: a page names one ``http_method``, and slack.com answers every method Backlot
serves over GET and POST alike, so a method is one operation here, named as Slack names it.

Backlot's side is ``app.openapi()`` for the arguments, and for the fields what a running Backlot
answers on ``PROBE_CORPUS`` below rather than the bundled demo corpus, so a finding reproduces with
``backlot.serve(records=...)`` and does not move when that corpus does. Both sides' answers are
reduced to a :class:`Shape`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

import httpx

from backlot.fidelity.errors import FidelityError
from backlot.fidelity.findings import BREAKING, GAP, Finding
from backlot.fidelity.operations import _METHODS
from backlot.fidelity.surface import (
    EXTRA,
    IMPLEMENTED,
    MISSING,
    Measurement,
    OperationEntry,
    OperationSurface,
)

# --------------------------------------------------------------------------- shapes

# A response key that is a FIELD name. Measured 2026-09-22 across every method Backlot serves:
# every key in Slack's answers matches this, except the conversation ids keying `shares.public` and
# `shares.private` on a file; and 2026-10-21 over `conversations.history`, the user ids keying a
# huddle message's `room.participants_events`. Those are map entries, not fields, and are collapsed
# to `{}`. The reference's own examples key a profile's custom `fields` the same way
# (`Xf111AAA111`).
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Shape:
    """One or more answers to a method, reduced to what a client can read off them.

    ``fields`` maps a field path to the JSON types seen at it; ``containers`` is every path whose
    members were looked at. The second is what makes an absence readable: a field missing under a
    list one side answered empty is not a divergence, and without recording which containers were
    entered the two cases are indistinguishable.
    """

    fields: dict[str, frozenset[str]]
    containers: frozenset[str]

    @classmethod
    def union(cls, shapes: Iterable["Shape"]) -> "Shape":
        """Several shapes as one: a field any of them carries is present, at every type seen."""
        fields: dict[str, frozenset[str]] = {}
        containers: frozenset[str] = frozenset()
        for shape in shapes:
            for path, types in shape.fields.items():
                fields[path] = fields.get(path, frozenset()) | types
            containers |= shape.containers
        return cls(fields, containers)

    def under(self, prefix: str) -> "Shape":
        """This shape with every path moved under ``prefix``, as an object's own shape is placed
        where a response carries it."""
        if not prefix:
            return self

        def moved(path: str) -> str:
            if not path:
                return prefix
            return f"{prefix}{path}" if path.startswith("[]") else f"{prefix}.{path}"

        return Shape(
            {moved(p): t for p, t in self.fields.items()},
            frozenset(moved(p) for p in self.containers),
        )

    def within(self, root: str) -> "Shape":
        """The part of this shape under ``root``, with ``root`` taken off every path: the inverse
        of :meth:`under`."""
        if not root:
            return self

        def inner(path: str) -> str | None:
            if path == root:
                return ""
            if path.startswith(root + "."):
                return path[len(root) + 1 :]
            if path.startswith(root + "[]"):
                return path[len(root) :]
            return None

        return Shape(
            {p: t for path, t in self.fields.items() if (p := inner(path)) is not None},
            frozenset(p for path in self.containers if (p := inner(path)) is not None),
        )

    def keys(self, container: str) -> set[str]:
        """The field names directly inside ``container``."""
        prefix = f"{container}." if container else ""
        return {
            p[len(prefix) :]
            for p in self.fields
            if p.startswith(prefix) and "." not in p[len(prefix) :] and "[]" not in p[len(prefix) :]
        }


def json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        # int and float are one type to a JSON reader, and Slack's own numbers arrive as both:
        # `tz_offset` is an integer and a file's `timestamp` can be either.
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null"


def shape_of(body: Any, *, empty_object_entered: bool = True) -> Shape:
    """Every field path a JSON value carries, with the JSON types seen at each.

    An array contributes one path for its members rather than one per member, ``<array>[]``, which
    carries the members' own types — the only place a list of ids answered as a list of objects can
    show. A field present on ANY member counts as present: Slack drops fields per object, a
    deactivated member carrying thirteen fewer, and intersecting would report the vendor as missing
    what it serves on every other member.

    An EMPTY ARRAY is never a container: it says only that this answer had no members. An empty
    OBJECT is one where ``empty_object_entered``, which holds for Backlot's own answers: Backlot
    answering a channel's ``properties`` as ``{}`` is a shape that carries no fields, so a field the
    vendor documents under it is a gap. The vendor's examples pass ``False``, because an example's
    ``{}`` is one sample's contents and not a statement that the object never carries a field.
    """
    fields: dict[str, set[str]] = {}
    containers: set[str] = set()

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if node or empty_object_entered:
                containers.add(path)
            for key, value in node.items():
                name = key if _FIELD_NAME.match(key) else "{}"
                child = f"{path}.{name}" if path else name
                fields.setdefault(child, set()).add(json_type(value))
                walk(value, child)
        elif isinstance(node, list) and node:
            members = f"{path}[]"
            containers.add(members)
            for item in node:
                fields.setdefault(members, set()).add(json_type(item))
                walk(item, members)

    walk(body, "")
    return Shape({k: frozenset(v) for k, v in fields.items()}, frozenset(containers))


def _container(path: str) -> str:
    """The path of the object a field sits in — everything before its own name. An array's
    members path is its own container: it exists only where that side's array had members."""
    if path.endswith("[]"):
        return path
    return path.rsplit(".", 1)[0] if "." in path else ""


def diff_shapes(method: str, backlot: Shape, documented: Shape) -> list[Finding]:
    """Every divergence between Backlot's answers to one method and the vendor's documented shape.

    Restricted to paths whose container BOTH sides entered: a field documented under a list Backlot
    answered empty is not a gap in Backlot's shape, and Backlot's own fields under an object the
    documentation never looks inside are not surface it invented.
    """
    out: list[Finding] = []
    for path in sorted(set(backlot.fields) - set(documented.fields)):
        if _container(path) in documented.containers:
            out.append(
                Finding(
                    "extra_field",
                    BREAKING,
                    f"{method} {path}",
                    "Backlot serves it; the vendor's reference documents no such field",
                )
            )
    for path in sorted(set(documented.fields) - set(backlot.fields)):
        if _container(path) in backlot.containers:
            out.append(
                Finding(
                    "missing_field",
                    GAP,
                    f"{method} {path}",
                    "the vendor's reference documents it; Backlot does not serve it",
                )
            )
    for path in sorted(set(backlot.fields) & set(documented.fields)):
        # `null` is dropped from both sides first: an example leaving a field null, or a row typed
        # `String or null`, says the field may be absent of a value, not that it has no type.
        mine = backlot.fields[path] - {"null"}
        theirs = documented.fields[path] - {"null"}
        if mine and theirs and not (mine & theirs):
            out.append(
                Finding(
                    "type_mismatch",
                    BREAKING,
                    f"{method} {path}",
                    f"vendor: {'|'.join(sorted(theirs))}, Backlot: {'|'.join(sorted(mine))}",
                )
            )
    return out


# --------------------------------------------------------------------------- Backlot's side

PROBE_CORPUS: tuple[dict[str, Any], ...] = (
    {
        # A message with a SUBTYPE, which is the only way the comparison sees the field: a
        # message posted through this API carries none (`slack._writer`), so without a corpus
        # one the probe would report `subtype` as surface Backlot does not serve.
        "source_type": "slack",
        "doc_id": "probe-join",
        "channel": "probe-general",
        "group": "probe",
        "visibility": "public",
        "author_email": "bob@probe.invalid",
        "author_name": "Bob Probe",
        "created": "2026-01-05T09:00:00Z",
        "content": "<@bob> has joined the channel",
        "subtype": "channel_join",
    },
    {
        "source_type": "slack",
        "doc_id": "probe-thread",
        "channel": "probe-general",
        "group": "probe",
        "visibility": "public",
        "author_email": "ava@probe.invalid",
        "author_name": "Ava Probe",
        "created": "2026-01-05T10:00:00Z",
        "content": "release checklist for the probe corpus",
        "reactions": [{"name": "eyes", "users": ["bob@probe.invalid"]}],
        "edited": {"user": "ava@probe.invalid", "ts": "1767607260.000000"},
        # A message carrying a file, because this is the only projection through which Backlot
        # serves one: `search.files` answers an empty match list by construction.
        "files": [
            {
                "id": "F0PROBE0001",
                "name": "checklist.txt",
                "mimetype": "text/plain",
                "title": "checklist",
            }
        ],
        "replies": [
            {
                "content": "checklist reviewed",
                "author_email": "bob@probe.invalid",
                "author_name": "Bob Probe",
                "created": "2026-01-05T10:05:00Z",
            }
        ],
    },
    {
        "source_type": "slack",
        "doc_id": "probe-later",
        "channel": "probe-general",
        "group": "probe",
        "visibility": "public",
        "author_email": "bob@probe.invalid",
        "author_name": "Bob Probe",
        "created": "2026-01-05T11:00:00Z",
        # A second top-level message in this channel, so a page of one has more to follow.
        "content": "release notes go out tomorrow",
    },
    {
        "source_type": "slack",
        "doc_id": "probe-searchable",
        "channel": "probe-random",
        "group": "probe",
        "visibility": "public",
        "author_email": "bob@probe.invalid",
        "author_name": "Bob Probe",
        "created": "2026-01-06T10:00:00Z",
        # A second channel, so a listing has more than one of anything, and the only record
        # carrying the word `_QUERY` searches for.
        "content": "the probe corpus keeps one drosophila here to be searched for",
    },
)

# A word only the last record carries.
_QUERY = "drosophila"


class _Caller:
    """Backlot's Slack Web API, asked with the admin/service token."""

    def __init__(self, base_url: str, token: str, *, timeout: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def __call__(self, method: str, **arguments: Any) -> dict:
        url = f"{self._base_url}/{method}"
        try:
            response = httpx.get(
                url,
                params=arguments,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout,
            )
            body = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise FidelityError(f"{url} could not be read: {e}") from e
        if not isinstance(body, dict) or not body.get("ok"):
            raise FidelityError(f"{url} answered {body!r}, so {method} could not be compared")
        return body


def _calls(call: _Caller) -> list[tuple[str, dict[str, Any]]]:
    """Every method Backlot serves, with its arguments, aimed at what the corpus holds: a method
    once per channel, thread or person.

    Every one rather than one of each, because Backlot drops fields per object as Slack does: a
    channel with no thread answers no `reply_count`, and a person without a file no file fields.
    The ids are discovered through the API, since Backlot mints them on import. And with the
    arguments the reference's own examples were sent where an answer echoes them: `api.test`'s
    carries the `foo` it was sent, `conversations.history`'s the `latest`, and a page with more to
    follow its `response_metadata`.

    A write method is asked like any other, and what it needs to name is written here first: a
    message to edit and react to, and a second one for `chat.delete`, so each call in the list
    names something that is there when the list runs. The writes land in this throwaway server's
    overlay, so the corpus is unchanged and the reads above have already been taken."""
    channels = call("conversations.list", limit=200).get("channels") or []
    people = call("users.list", limit=200).get("members") or []
    threads = [
        (channel["id"], message["ts"])
        for channel in channels
        for message in call("conversations.history", channel=channel["id"], limit=200).get(
            "messages"
        )
        or ()
        if message.get("reply_count")
    ]
    if not channels or not people or not threads:
        raise FidelityError(
            "the probe corpus served no channel, person or thread, so nothing can be compared"
        )
    room = channels[0]["id"]
    edited = call("chat.postMessage", channel=room, text="a message to edit")["ts"]
    call("reactions.add", channel=room, timestamp=edited, name="eyes")
    doomed = call("chat.postMessage", channel=room, text="a message to delete")["ts"]
    return [
        ("api.test", {"foo": "bar"}),
        ("auth.test", {}),
        ("conversations.list", {"limit": 200}),
        *(("conversations.info", {"channel": c["id"]}) for c in channels),
        *(("conversations.history", {"channel": c["id"], "limit": 200}) for c in channels),
        *(
            ("conversations.history", {"channel": c["id"], "limit": 1, "latest": "9999999999.0"})
            for c in channels
        ),
        *(("conversations.members", {"channel": c["id"]}) for c in channels),
        *(("conversations.replies", {"channel": c, "ts": ts}) for c, ts in threads),
        *(
            (method, {"query": _QUERY, "count": 20})
            for method in ("search.all", "search.files", "search.messages")
        ),
        *(("users.info", {"user": p["id"]}) for p in people),
        ("users.list", {"limit": 200}),
        ("chat.postMessage", {"channel": room, "text": "posted by the probe"}),
        ("chat.postMessage", {"channel": room, "text": "a reply", "thread_ts": edited}),
        ("chat.postEphemeral", {"channel": room, "user": people[0]["id"], "text": "seen once"}),
        ("chat.update", {"channel": room, "ts": edited, "text": "edited by the probe"}),
        ("chat.getPermalink", {"channel": room, "message_ts": edited}),
        ("reactions.add", {"channel": room, "timestamp": edited, "name": "tada"}),
        ("reactions.get", {"channel": room, "timestamp": edited}),
        ("reactions.list", {"limit": 200}),
        ("reactions.remove", {"channel": room, "timestamp": edited, "name": "tada"}),
        ("chat.delete", {"channel": room, "ts": doomed}),
    ]


def answers(*, timeout: float = 120.0) -> dict[str, Shape]:
    """Start a Backlot on the probe corpus, and the shape of its answers to each method it serves,
    over every call aimed at that method."""
    import backlot

    # Backlot measuring itself is not anyone's use of it.
    with backlot.serve(records=list(PROBE_CORPUS), telemetry=False) as server:
        call = _Caller(f"{server.base_url}/slack/api", server.token, timeout=timeout)
        shapes: dict[str, list[Shape]] = {}
        for method, arguments in _calls(call):
            shapes.setdefault(method, []).append(shape_of(call(method, **arguments)))
    return {method: Shape.union(s) for method, s in shapes.items()}


# --------------------------------------------------------------------------- the reference

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


# One argument, as every method page writes it, its type left out on some; and a bolded code name
# anywhere on a line, which must be one of those.
_ARGUMENT = re.compile(r"^\*\*`([^`]+)`\*\*(?:`([^`]*)`)?(?:Required|Optional)\s*$", re.M)
_BOLD_NAME = re.compile(r"\*\*`[^`\n]+`\*\*")
# An argument's type as JSON names it. `integer` and `number` are one JSON type, as they are in
# `json_type`.
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

#: Where a method puts an object its ``## Response`` links and none of its examples carries, by
#: ``(method, the object page's file name)``. A page that documents one answer shape per item type
#: and exemplifies one of them leaves the others' objects with no container to be read off, and
#: guessing one would place a whole object wrong — so each entry here is measured, not inferred.
#:
#: ``reactions.get`` is the one method that needs it: its page links the file object twice and
#: every success example is the message variant. Measured against slack.com on 2026-10-21,
#: ``reactions.get`` aimed at a file answers ``{"ok": true, "type": "file", "file": {…}}`` and no
#: ``channel``, putting the object at ``file`` as ``reactions.list`` puts it at ``items[].file``.
_OBJECT_AT = {("reactions.get", "file-object.md"): "file"}

# A row's name in an object's field table, alone on its line.
_ROW_NAME = re.compile(r"`([a-z][a-z0-9_.]*)`")
# A field table's type cell as JSON names it, first match wins; a cell none reads types nothing.
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
    argument from required to optional is not surface Backlot is missing.

    A page that reads as taking none is refused: every page this reads is one Backlot serves, and
    each of those takes arguments, so none read means the section moved or was respelled."""
    section = _section(page, "arguments")
    found = _ARGUMENT.findall(section)
    if len(found) != len(_BOLD_NAME.findall(section)):
        raise FidelityError(f"{url} has an argument line this does not read")
    if not found:
        raise FidelityError(f"{url} has no `## Arguments` section this reads")
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

    def repaired(between: str) -> str:
        return _TRAILING_COMMA.sub(r"\1", _ELIDED.sub("", between))

    parts, at = [], 0
    for string in _STRING.finditer(text):
        parts += [repaired(text[at : string.start()]), string.group()]
        at = string.end()
    parts.append(repaired(text[at:]))
    return json.loads("".join(parts))


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
    method = url.rsplit("/", 1)[-1].removesuffix(".md")
    shapes = [documented]
    for link in dict.fromkeys(_OBJECT_LINK.findall(section)):
        obj = read_object(link)
        own = obj.keys("")
        at = _best(documented, own)
        if at is None or 2 * len(documented.keys(at) & own) < len(documented.keys(at)):
            at = _OBJECT_AT.get((method, link.rsplit("/", 1)[-1]))
            if at is None:
                raise FidelityError(f"{url} links {link}, and no example carries that object")
        shapes.append(obj.under(at))
    return Shape.union(shapes)


# --------------------------------------------------------------------------- the comparison


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
    answered = answers(timeout=timeout)
    backlot = {name: Method(name, m.arguments, answered.get(name)) for name, m in served.items()}
    return Measurement(diff_methods(backlot, real), inventory(backlot, real))
