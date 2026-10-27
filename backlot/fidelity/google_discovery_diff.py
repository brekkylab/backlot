"""Comparing Backlot against a Google API Discovery document.

Google does not publish OpenAPI. Its own format describes the same thing — which operations exist
and what each accepts — but says it differently enough that sharing a parser with OpenAPI would
mean a parser that understands neither well:

* methods hang off nested ``resources`` rather than off a flat path map;
* a method's ``path`` is relative to ``servicePath`` — empty for Gmail, ``drive/v3/`` for Drive;
* the standard parameters (``fields``, ``alt``, ``prettyPrint``) are declared ONCE at the top of
  the document rather than on each method. Reading only the per-method block reports every one of
  them as surface Backlot invented, which is how this comparison first accused Drive's ``fields``.

A discovery document describes a second contract that is not an operation at all: the batch
endpoint, named in the top-level ``batchPath``. Nothing under ``resources`` declares it, so the
path diff has nothing to pair Backlot's batch routes with, and :func:`batch_measure`
compares the field instead. It reads per SOURCE rather than per document, because Backlot serves
one batch route for several of them.

A third, read where the source names a walk for the document: the messages its methods answer
with, declared under ``schemas``, against what a running Backlot answers (:func:`types_measure`).

Like OpenAPI, these documents are public: no credential, no quota, no account.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Protocol

from backlot.fidelity.errors import FidelityError
from backlot.fidelity.fetch import fetch_json
from backlot.fidelity.findings import BREAKING, GAP, Finding
from backlot.fidelity.operations import (
    Operation,
    canonical,
    diff_operations,
    from_backlot,
    inventory,
)
from backlot.fidelity.surface import (
    EXTRA,
    IMPLEMENTED,
    MISSING,
    BatchDocument,
    BatchRoute,
    BatchSurface,
    Measurement,
    Member,
    OperationSurface,
    SchemaSurface,
    TypeNode,
)


def from_google_discovery(doc: Mapping[str, Any]) -> dict[tuple[str, str], Operation]:
    """Operations declared by a Google API Discovery document.

    Methods hang off nested ``resources`` rather than off a flat path map, and a method's ``path``
    is relative to ``servicePath`` — empty for Gmail, ``drive/v3/`` for Drive — so the two have to
    be joined before anything lines up with what Backlot serves.
    """
    service_path = doc.get("servicePath") or ""
    prefix = str(doc.get("name") or "")
    # Google declares `fields`, `alt`, `prettyPrint` and the rest ONCE at the top of the document,
    # not on each method. Reading only the per-method block reports every one of them as surface
    # Backlot invented, which is how this comparison first accused Drive's `fields` projection.
    common = {
        name
        for name, p in (doc.get("parameters") or {}).items()
        if isinstance(p, dict) and p.get("location") == "query"
    }
    ops: dict[tuple[str, str], Operation] = {}

    def visit(node: Mapping[str, Any]) -> None:
        for method in (node.get("methods") or {}).values():
            if not isinstance(method, dict) or "path" not in method:
                continue
            params = {
                name
                for name, p in (method.get("parameters") or {}).items()
                if isinstance(p, dict) and p.get("location") == "query"
            }
            ref = (method.get("response") or {}).get("$ref")
            o = Operation(
                method.get("httpMethod", "GET").lower(),
                canonical(service_path + method["path"]),
                frozenset(params | common),
                method.get("id") or "",
                f"{prefix}.{ref}" if ref else "",
            )
            ops[o.key] = o
        for child in (node.get("resources") or {}).values():
            if isinstance(child, dict):
                visit(child)

    visit(doc)
    return ops


class SpecTarget(Protocol):
    """What this module needs of ONE document. Structural, so the registry can import this module
    without this module importing the registry."""

    spec_url: str
    mount: tuple[str, ...]
    strip: str


def document_id(doc: Mapping[str, Any], url: str) -> str:
    """What a discovery document calls itself — ``gmail:v1``, ``drive:v3``.

    The document's own name and not the URL it came from, because this is a finding's identity and
    so a baseline key. Google answers one document at more than one URL: measured 2026-09-17,
    ``gmail.googleapis.com/$discovery/rest?version=v1`` and
    ``www.googleapis.com/discovery/v1/apis/gmail/v1/rest`` both answer the document that calls
    itself ``gmail:v1``, so repointing the registry from one to the other must not read as a
    divergence on an API nothing changed about.
    """
    declared = doc.get("id")
    if isinstance(declared, str) and declared:
        return declared
    name, version = doc.get("name"), doc.get("version")
    if isinstance(name, str) and isinstance(version, str) and name and version:
        return f"{name}:{version}"
    return url


def batch_path(doc: Mapping[str, Any]) -> str:
    """The batch endpoint a document declares, canonical, or ``""`` if it declares none.

    Canonical because Backlot's side is: both go through
    :func:`~backlot.fidelity.operations.canonical`, so a slash at either end cannot pass as a
    divergence."""
    declared = doc.get("batchPath")
    return canonical(declared) if isinstance(declared, str) else ""


def answers(route: str, declared: str) -> bool:
    """Whether one of Backlot's batch routes answers a declared batch path.

    Segment by segment, with ``{}`` — what :func:`~backlot.fidelity.operations.canonical` leaves of
    a path parameter — matching any ONE segment. So ``batch/{}/{}`` answers ``batch/drive/v3`` and
    answers neither ``batch`` nor ``batch/drive/v3/files``: a route that swallowed a differing
    number of segments would report a moved ``batchPath`` as covered.
    """
    ours, theirs = route.split("/"), declared.split("/")
    return len(ours) == len(theirs) and all(
        segment == "{}" or segment == other for segment, other in zip(ours, theirs)
    )


def batch_measure(
    batch_mount: Iterable[str], documents: Iterable[tuple[str, Mapping[str, Any]]]
) -> Measurement:
    """Backlot's batch routes against the ``batchPath`` each document declares.

    Per SOURCE, not per document, which is what separates this from the path diff. Measured
    2026-09-17: ``gmail:v1``, ``docs:v1``, ``sheets:v4`` and ``slides:v1`` declare ``batch`` and
    each answers it on its own host, while ``drive:v3`` declares ``batch/drive/v3`` on the shared
    ``www.googleapis.com`` — the ``drive/v3`` is what discriminates it there. Backlot collapses
    those hosts onto one origin, so one route stands in for several documents and the question
    "does any route still answer this" cannot be asked a document at a time.
    """
    routes = [canonical(route) for route in batch_mount]
    out: list[Finding] = []
    declarations: list[BatchDocument] = []
    selected: set[str] = set()
    for url, doc in documents:
        who = document_id(doc, url)
        declared = batch_path(doc)
        if not declared:
            declarations.append(BatchDocument(who, declared, EXTRA))
            out.append(
                Finding(
                    "extra_batch_api",
                    BREAKING,
                    who,
                    "the document declares no batchPath, and Backlot goes on answering batch for "
                    "this API",
                )
            )
            continue
        answering = [route for route in routes if answers(route, declared)]
        declarations.append(BatchDocument(who, declared, IMPLEMENTED if answering else MISSING))
        if not answering:
            # The document AND the value: a gap is acknowledged by identity alone (see
            # `Baseline.unacknowledged`), so keyed on the document, an acknowledged move to one
            # shape would go on covering a later move to another. The path diff has that property
            # for free, since there the moved path IS the identity.
            out.append(
                Finding(
                    "missing_batch_path",
                    GAP,
                    f"{who} {declared}",
                    f"the vendor batches at '{declared}'; no route this source mounts answers "
                    "that shape",
                )
            )
            continue
        selected.update(answering)
    for route in routes:
        if route not in selected:
            out.append(
                Finding(
                    "extra_batch_route",
                    BREAKING,
                    f"/{route}",
                    "Backlot serves it for this source, and no document of it declares a "
                    "batchPath the route answers",
                )
            )
    batch = BatchSurface(
        tuple(sorted(declarations, key=lambda d: d.document)),
        tuple(BatchRoute(f"/{r}", IMPLEMENTED if r in selected else EXTRA) for r in sorted(routes)),
    )
    return Measurement(
        sorted(out, key=lambda f: (f.severity != BREAKING, f.path, f.kind)),
        OperationSurface((), batch),
    )


def measure(
    spec: SpecTarget, *, timeout: float = 120.0, seen: dict[str, dict] | None = None
) -> Measurement:
    """This module's entry point: load both contracts, compare them and pair their operations.

    ONE document. A Google source is often served through several APIs at once — a Drive file is
    also read through Docs, Sheets and Slides — and each publishes its own discovery document with
    its own service path. Those are the caller's to concatenate, and ``seen`` is how that caller
    hands the same documents to :func:`batch_measure` without fetching them twice."""
    from backlot.main import app

    if seen is None:
        seen = {}
    if spec.spec_url not in seen:
        seen[spec.spec_url] = fetch_json(spec.spec_url, timeout=timeout)
    doc = seen[spec.spec_url]
    vendor = from_google_discovery(doc)
    if not vendor:
        raise FidelityError(
            f"{spec.spec_url} declared no operations; is it still a Discovery document?"
        )
    served = from_backlot(app.openapi(), spec.mount, spec.strip)
    return Measurement(diff_operations(served, vendor), inventory(served, vendor))


# --------------------------------------------------------------------------- the response types
#
# A discovery document declares, beside its methods, the messages each one answers with: a method's
# `response` names one of `schemas`, and a schema's `properties` name the messages they hold. Those
# are the `types` of a Google baseline, each keyed by the document's `name` and its own
# (`sheets.Spreadsheet`), since the editor APIs' documents reuse names (`Color`, `Link`).


def _type(prop: Mapping[str, Any], prefix: str) -> str:
    """What a property holds, spelled for a baseline: a message as its qualified name, a list as
    the element in brackets, a map as ``map<string, …>``, and anything else as its JSON type."""
    if "$ref" in prop:
        return f"{prefix}.{prop['$ref']}"
    if prop.get("type") == "array" and isinstance(prop.get("items"), Mapping):
        return f"[{_type(prop['items'], prefix)}]"
    if isinstance(prop.get("additionalProperties"), Mapping):
        return f"map<string, {_type(prop['additionalProperties'], prefix)}>"
    return str(prop.get("type", "any"))


def held(type_: str, types: Mapping[str, Any]) -> str | None:
    """The message a field holds, itself or as the element of a list, or None: a map is a leaf,
    since real refuses a key under one as naming nothing (measured 2026-10-22,
    `sheets.filterViews.criteria.foo`)."""
    name = type_.strip("[]")
    return name if name in types else None


def declared_types(doc: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Every message the document declares, as its fields and what each holds."""
    prefix = str(doc.get("name") or "")
    return {
        f"{prefix}.{name}": {
            field: _type(prop, prefix)
            for field, prop in (schema.get("properties") or {}).items()
            if isinstance(prop, Mapping)
        }
        for name, schema in (doc.get("schemas") or {}).items()
        if isinstance(schema, Mapping)
    }


def served_fields(
    types: Mapping[str, Mapping[str, str]], type_name: str, value: Any, out: dict[str, set[str]]
) -> None:
    """Add to ``out`` each field ``value`` carries, under the message it is, through every message
    it holds: what one answer of type ``type_name`` serves."""
    if isinstance(value, list):
        for item in value:
            served_fields(types, type_name, item, out)
        return
    if not isinstance(value, Mapping):
        return
    fields = types.get(type_name, {})
    seen = out.setdefault(type_name, set())
    for name, item in value.items():
        seen.add(name)
        inner = held(fields[name], types) if name in fields else None
        if inner is not None:
            served_fields(types, inner, item, out)


class TypesTarget(Protocol):
    """What :func:`types_measure` needs of one document's comparison."""

    spec_url: str
    walk: Callable[[float], list[tuple[str, Any]]] | None


def types_measure(spec: TypesTarget, doc: Mapping[str, Any], *, timeout: float) -> Measurement:
    """The messages the methods a walk asked answer with, and every message they reach, measured
    against what a running Backlot served, with the statuses ``docs/fidelity.md`` gives a Google
    source's `types`.

    Every one of them is listed whether or not Backlot serves it, where a GraphQL source lists none
    of the vendor's types Backlot does not declare: a `fields` mask is read against the whole of
    each (`backlot.routers.google_sheets._gmask_select`), so the baseline has to carry it offline.
    A field the vendor does not declare is `extra`, and breaking."""
    types = declared_types(doc)
    answers = {o.id: o.response for o in from_google_discovery(doc).values() if o.response}
    walked: list[tuple[str, Any]] = spec.walk(timeout) if spec.walk else []
    served: dict[str, set[str]] = {}
    for method, body in walked:
        if method not in answers:
            raise FidelityError(f"{spec.spec_url} declares no response for {method!r}")
        served_fields(types, answers[method], body, served)
    reached: set[str] = set()
    todo = sorted({answers[m] for m, _ in walked})
    while todo:
        name = todo.pop()
        if name in reached or name not in types:
            continue
        reached.add(name)
        todo += [h for t in types[name].values() if (h := held(t, types))]
    findings: list[Finding] = []
    nodes: dict[str, TypeNode] = {}
    for name in sorted(reached):
        carried = served.get(name)
        fields: dict[str, Member] = {}
        for field, type_ in types[name].items():
            if carried is None:
                fields[field] = Member(MISSING, type_)
            elif field in carried:
                fields[field] = Member(IMPLEMENTED, type_)
            else:
                fields[field] = Member(MISSING, type_)
                findings.append(
                    Finding(
                        "missing_field",
                        GAP,
                        f"{name}.{field}",
                        f"the vendor declares it ({type_}); no answer the walk read carried it",
                    )
                )
        for field in sorted((carried or set()) - set(types[name])):
            fields[field] = Member(EXTRA)
            findings.append(
                Finding(
                    "extra_field",
                    BREAKING,
                    f"{name}.{field}",
                    "Backlot serves it, and the vendor's message declares no such field",
                )
            )
        nodes[name] = TypeNode("object", IMPLEMENTED if carried is not None else MISSING, fields)
    return Measurement(
        sorted(findings, key=lambda f: (f.severity != BREAKING, f.path, f.kind)),
        SchemaSurface("", nodes),
    )
