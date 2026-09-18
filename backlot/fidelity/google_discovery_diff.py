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
path diff has nothing to pair Backlot's batch routes with, and :func:`batch_divergences`
compares the field instead. It reads per SOURCE rather than per document, because Backlot serves
one batch route for several of them.

Like OpenAPI, these documents are public: no credential, no quota, no account.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Protocol

from backlot.fidelity.errors import FidelityError
from backlot.fidelity.fetch import fetch_json
from backlot.fidelity.findings import BREAKING, GAP, Finding
from backlot.fidelity.operations import Operation, canonical, diff_operations, from_backlot


def from_google_discovery(doc: Mapping[str, Any]) -> dict[tuple[str, str], Operation]:
    """Operations declared by a Google API Discovery document.

    Methods hang off nested ``resources`` rather than off a flat path map, and a method's ``path``
    is relative to ``servicePath`` — empty for Gmail, ``drive/v3/`` for Drive — so the two have to
    be joined before anything lines up with what Backlot serves.
    """
    service_path = doc.get("servicePath") or ""
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
            o = Operation(
                method.get("httpMethod", "GET").lower(),
                canonical(service_path + method["path"]),
                frozenset(params | common),
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


def batch_divergences(
    batch_mount: Iterable[str], documents: Iterable[tuple[str, Mapping[str, Any]]]
) -> list[Finding]:
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
    selected: set[str] = set()
    for url, doc in documents:
        who = document_id(doc, url)
        declared = batch_path(doc)
        if not declared:
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
    return sorted(out, key=lambda f: (f.severity != BREAKING, f.path, f.kind))


def divergences(
    spec: SpecTarget, *, timeout: float = 120.0, seen: dict[str, dict] | None = None
) -> list[Finding]:
    """This module's entry point: load both contracts and compare them.

    ONE document. A Google source is often served through several APIs at once — a Drive file is
    also read through Docs, Sheets and Slides — and each publishes its own discovery document with
    its own service path. Those are the caller's to concatenate, and ``seen`` is how that caller
    hands the same documents to :func:`batch_divergences` without fetching them twice."""
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
    return diff_operations(served, vendor)
