"""What a comparison measured, in the shape of the contract it measured, and how a baseline file
spells it.

A path contract is a list of operations; a GraphQL schema is a tree of types, fields and
arguments. A surface holds every element either side has, each with its status, and a baseline
file is that surface with each accepted finding written onto the element it names. Nothing is
recorded twice: the findings a baseline accepts are read back off the elements.

Every finding names exactly one element and no element carries two, which is what lets a finding
be written as fields of the element rather than as a row beside it: an operation is either
missing or extra, a field is missing, extra or of another type, and so on down.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from backlot.fidelity.findings import BREAKING, GAP, Finding

# Both sides have it; only the vendor has it; only Backlot has it; Backlot routes it only to refuse
# it, as it does every S3 write.
IMPLEMENTED, MISSING, EXTRA, REFUSED = "implemented", "missing", "extra", "refused"
_SERVED = (IMPLEMENTED, EXTRA)

_PARAM_KINDS = ("missing_param", "extra_param")
_BATCH_DOCUMENT_KINDS = ("missing_batch_path", "extra_batch_api")
_BATCH_ROUTE_KINDS = ("extra_batch_route",)


def _accepted(f: Finding | None) -> dict[str, str]:
    """The fields an accepted finding adds to the element it names.

    A gap's ``detail`` is not kept: every run regenerates it and a gap is accepted by what it
    names alone. A breaking finding's is kept, because there the detail is what was accepted —
    see ``Baseline.unacknowledged``."""
    if f is None:
        return {}
    d = {"kind": f.kind, "severity": f.severity}
    if f.severity == BREAKING:
        d["detail"] = f.detail
    if f.note:
        d["note"] = f.note
    return d


def _read(raw: Mapping[str, Any], path: str) -> list[Finding]:
    """The finding an element carries, if it carries one."""
    if "kind" not in raw:
        return []
    return [
        Finding(
            raw["kind"],
            raw.get("severity", GAP),
            path,
            raw.get("detail", ""),
            raw.get("note", ""),
        )
    ]


def _unplaced(findings: Iterable[Finding]) -> None:
    left = sorted(f.key for f in findings)
    if left:
        raise ValueError(f"findings naming no element of the measured surface: {left}")


# --------------------------------------------------------------------------- operations


@dataclass(frozen=True)
class OperationEntry:
    """One operation of the vendor's surface, or one Backlot serves that its document does not.

    ``operation`` is spelled as a finding's ``path`` is — ``GET /repos/{}/{}/commits``,
    ``Query.issues`` — so the finding about an operation and the operation name the same string.
    ``id`` is what the vendor calls it (``repos/list-commits``, ``gmail.users.messages.list``),
    empty for a served route the vendor's document does not describe.
    """

    operation: str
    status: str
    id: str = ""


@dataclass(frozen=True)
class BatchDocument:
    """The batch endpoint one discovery document declares, ``""`` where it declares none."""

    document: str
    declared: str
    status: str

    @property
    def path(self) -> str:
        """The path of the finding about it: the document AND the value where nothing answers the
        value, so an accepted move to one shape does not cover a later move to another."""
        return f"{self.document} {self.declared}" if self.status == MISSING else self.document


@dataclass(frozen=True)
class BatchRoute:
    route: str
    status: str


@dataclass(frozen=True)
class BatchSurface:
    """A Google source's batch contract: what each document declares, and which routes answer."""

    documents: tuple[BatchDocument, ...]
    routes: tuple[BatchRoute, ...]

    def to_json(self, findings: Iterable[Finding]) -> dict[str, Any]:
        by_path = {f.path: f for f in findings}
        documents = []
        for d in self.documents:
            entry = {"document": d.document, "declared": d.declared, "status": d.status}
            documents.append(entry | _accepted(by_path.pop(d.path, None)))
        routes = []
        for r in self.routes:
            routes.append(
                {"route": r.route, "status": r.status} | _accepted(by_path.pop(r.route, None))
            )
        _unplaced(by_path.values())
        return {"documents": documents, "routes": routes}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> tuple["BatchSurface", list[Finding]]:
        findings: list[Finding] = []
        documents = []
        for e in raw.get("documents", ()):
            d = BatchDocument(e["document"], e.get("declared", ""), e["status"])
            documents.append(d)
            findings += _read(e, d.path)
        routes = []
        for e in raw.get("routes", ()):
            routes.append(BatchRoute(e["route"], e["status"]))
            findings += _read(e, e["route"])
        return cls(tuple(documents), tuple(routes)), findings


@dataclass(frozen=True)
class OperationSurface:
    """The operations of a contract that enumerates them: a published document's paths, or the
    operations of a service model a probe asks a running server about.

    Parameters appear only where they diverge, under the operation they belong to: a parameter is
    not something a vendor names on its own, and listing the matching ones would only repeat the
    served routes' own declarations."""

    operations: tuple[OperationEntry, ...]
    batch: BatchSurface | None = None

    @classmethod
    def joined(cls, parts: Iterable["OperationSurface"]) -> "OperationSurface":
        """Several contracts' surfaces as one source's, one entry per operation.

        Two documents of one source can declare the same operation — Jira's both carry the 11
        paths outside ``/rest/api/{2,3}`` — and only one of them may be the document whose mount
        serves it, so a served entry wins over a ``missing`` one for the same operation."""
        seen: dict[str, OperationEntry] = {}
        batch = None
        for part in parts:
            batch = part.batch or batch
            for e in part.operations:
                known = seen.get(e.operation)
                if known is None or (known.status == MISSING and e.status != MISSING):
                    seen[e.operation] = e
        return cls(tuple(seen[k] for k in sorted(seen)), batch)

    def to_json(self, findings: Iterable[Finding]) -> dict[str, Any]:
        own: dict[str, Finding] = {}
        params: dict[str, dict[str, Finding]] = defaultdict(dict)
        batch: list[Finding] = []
        for f in findings:
            if f.kind in _PARAM_KINDS:
                operation, _, name = f.path.rpartition("?")
                params[operation][name] = f
            elif f.kind in _BATCH_DOCUMENT_KINDS + _BATCH_ROUTE_KINDS:
                batch.append(f)
            else:
                own[f.path] = f
        entries = []
        for e in self.operations:
            entry: dict[str, Any] = {"operation": e.operation}
            if e.id:
                entry["id"] = e.id
            entry["status"] = e.status
            entry |= _accepted(own.pop(e.operation, None))
            if diverging := params.pop(e.operation, None):
                entry["params"] = {n: _accepted(diverging[n]) for n in sorted(diverging)}
            entries.append(entry)
        _unplaced([*own.values(), *(f for p in params.values() for f in p.values())])
        out: dict[str, Any] = {"operations": entries}
        if self.batch is not None:
            out["batch"] = self.batch.to_json(batch)
        else:
            _unplaced(batch)
        return out

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> tuple["OperationSurface", list[Finding]]:
        findings: list[Finding] = []
        entries = []
        for e in raw["operations"]:
            entries.append(OperationEntry(e["operation"], e["status"], e.get("id", "")))
            findings += _read(e, e["operation"])
            for name, p in (e.get("params") or {}).items():
                findings += _read(p, f"{e['operation']}?{name}")
        batch = None
        if "batch" in raw:
            batch, more = BatchSurface.from_json(raw["batch"])
            findings += more
        return cls(tuple(entries), batch), findings


# --------------------------------------------------------------------------- GraphQL schema


@dataclass(frozen=True)
class Member:
    """A field, an argument, an enum value or a union member.

    ``type`` is the vendor's, kept where only the vendor has the member: that is the one fact about
    it the served SDL cannot answer offline. ``args`` are a field's, compared where both sides have
    the field and listed as Backlot's own where only Backlot does."""

    status: str
    type: str = ""
    args: dict[str, "Member"] = field(default_factory=dict)


@dataclass(frozen=True)
class TypeNode:
    kind: str
    status: str
    fields: dict[str, Member] = field(default_factory=dict)
    values: dict[str, Member] = field(default_factory=dict)
    members: dict[str, Member] = field(default_factory=dict)


@dataclass(frozen=True)
class SchemaSurface:
    """The types Backlot declares, each with every field, argument, value and member either side
    has under it.

    The vendor's types Backlot does not declare are not listed, as no finding names them: a
    vendor type matters where a type Backlot does declare reaches it, and there it shows as the
    ``type`` of a missing member. The root query type's fields are this contract's operations."""

    query: str
    types: dict[str, TypeNode]

    @property
    def operations(self) -> tuple[OperationEntry, ...]:
        root = self.types.get(self.query)
        if root is None:
            return ()
        return tuple(
            OperationEntry(f"{self.query}.{name}", m.status, "" if m.status == EXTRA else name)
            for name, m in sorted(root.fields.items())
        )

    def elements(self) -> dict[str, str]:
        """Every element's finding path, with its status."""
        out: dict[str, str] = {}
        for name, t in self.types.items():
            out[name] = t.status
            for group in (t.fields, t.values, t.members):
                for member, m in group.items():
                    out[f"{name}.{member}"] = m.status
                    for arg, a in m.args.items():
                        out[f"{name}.{member}({arg})"] = a.status
        return out

    def to_json(self, findings: Iterable[Finding]) -> dict[str, Any]:
        by_path = {f.path: f for f in findings}

        def member(path: str, m: Member) -> dict[str, Any]:
            d: dict[str, Any] = {"status": m.status}
            if m.type:
                d["type"] = m.type
            d |= _accepted(by_path.pop(path, None))
            if m.args:
                d["args"] = {a: member(f"{path}({a})", m.args[a]) for a in sorted(m.args)}
            return d

        types: dict[str, Any] = {}
        for name in sorted(self.types):
            t = self.types[name]
            # `type_kind` rather than introspection's `kind`, which names an accepted finding here.
            d: dict[str, Any] = {"type_kind": t.kind, "status": t.status}
            d |= _accepted(by_path.pop(name, None))
            for group, members in (
                ("fields", t.fields),
                ("values", t.values),
                ("members", t.members),
            ):
                if members:
                    d[group] = {n: member(f"{name}.{n}", members[n]) for n in sorted(members)}
            types[name] = d
        _unplaced(by_path.values())
        return {"root": {"query": self.query}, "types": types}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> tuple["SchemaSurface", list[Finding]]:
        findings: list[Finding] = []

        def member(path: str, e: Mapping[str, Any]) -> Member:
            findings.extend(_read(e, path))
            args = {a: member(f"{path}({a})", v) for a, v in (e.get("args") or {}).items()}
            return Member(e["status"], e.get("type", ""), args)

        types = {}
        for name, e in raw["types"].items():
            findings.extend(_read(e, name))
            groups = {
                group: {n: member(f"{name}.{n}", v) for n, v in (e.get(group) or {}).items()}
                for group in ("fields", "values", "members")
            }
            types[name] = TypeNode(e["type_kind"], e["status"], **groups)
        return cls(raw["root"]["query"], types), findings


Surface = OperationSurface | SchemaSurface


def surface_from_json(raw: Mapping[str, Any]) -> tuple[Surface | None, list[Finding]]:
    """The surface a baseline file holds, and the findings written onto it."""
    if "types" in raw:
        return SchemaSurface.from_json(raw)
    if "operations" in raw:
        return OperationSurface.from_json(raw)
    return None, []


@dataclass(frozen=True)
class Measurement:
    """What one run of a comparison read: its findings, and the surface they are about."""

    findings: list[Finding]
    surface: Surface | None = None
