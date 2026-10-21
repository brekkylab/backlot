"""What Backlot's Slack methods answer, read off a running Backlot as JSON shapes.

The vendor's side of each shape is read off its reference documentation
(:mod:`backlot.fidelity.slack_docs`); this module holds the other side, and the shape both are
reduced to. Backlot runs on ``PROBE_CORPUS`` below rather than the bundled demo corpus, so a finding
reproduces with ``backlot.serve(records=...)`` and does not move when that corpus does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from backlot.fidelity.errors import FidelityError
from backlot.fidelity.findings import BREAKING, GAP, Finding

# A response key that is a FIELD name. Measured 2026-09-22 across every method Backlot serves:
# every key in Slack's answers matches this, except the conversation ids keying `shares.public` and
# `shares.private` on a file. Those are map entries, not fields, and are collapsed to `{}`. The
# reference's own examples key a profile's custom `fields` the same way (`Xf111AAA111`).
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

PROBE_CORPUS: tuple[dict[str, Any], ...] = (
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

# A word only the second record carries.
_QUERY = "drosophila"


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
    The ids are discovered through the API, since Backlot mints them on import."""
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
    return [
        ("api.test", {}),
        ("auth.test", {}),
        ("conversations.list", {"limit": 200}),
        *(("conversations.info", {"channel": c["id"]}) for c in channels),
        *(("conversations.history", {"channel": c["id"], "limit": 200}) for c in channels),
        *(("conversations.members", {"channel": c["id"]}) for c in channels),
        *(("conversations.replies", {"channel": c, "ts": ts}) for c, ts in threads),
        *(
            (method, {"query": _QUERY, "count": 20})
            for method in ("search.all", "search.files", "search.messages")
        ),
        *(("users.info", {"user": p["id"]}) for p in people),
        ("users.list", {"limit": 200}),
    ]


def answers(*, timeout: float = 120.0) -> dict[str, Shape]:
    """This module's entry point: start a Backlot on the probe corpus, and the shape of its answers
    to each method it serves, over every call aimed at that method."""
    import backlot

    # Backlot measuring itself is not anyone's use of it.
    with backlot.serve(records=list(PROBE_CORPUS), telemetry=False) as server:
        call = _Caller(f"{server.base_url}/slack/api", server.token, timeout=timeout)
        shapes: dict[str, list[Shape]] = {}
        for method, arguments in _calls(call):
            shapes.setdefault(method, []).append(shape_of(call(method, **arguments)))
    return {method: Shape.union(s) for method, s in shapes.items()}
