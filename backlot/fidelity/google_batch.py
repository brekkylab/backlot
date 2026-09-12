"""Google's batch endpoint is a document FIELD, so it is compared as one.

Every discovery document Backlot compares against names a batch endpoint in its top-level
``batchPath``: measured 2026-09-12, ``gmail:v1``, ``docs:v1``, ``sheets:v4`` and ``slides:v1`` say
``batch``, and ``drive:v3`` says ``batch/drive/v3``. None of them declares that endpoint as a
method, and a ``methods`` entry is the only thing ``google_discovery_diff.from_google_discovery``
builds an operation out of — so a path diff has nothing on the vendor's side to pair Backlot's two
batch routes with, and the endpoint sat outside the coverage check as a result.

The methods that DO carry the word are a different thing: ``messages.batchModify``,
``spreadsheets.values.batchGet`` and the rest are single requests that happen to act on several
items, and they are declared as operations and compared as operations already. Measured across the
five documents, no declared path is a ``batchPath`` value.

Nor does it belong to one source's mount. Each API answers batch on its own host —
``gmail.googleapis.com/batch``, ``sheets.googleapis.com/batch`` — while Drive sits on the shared
``www.googleapis.com``, which is what the ``drive/v3`` in its value discriminates. Backlot collapses
those hosts onto one origin, so ``/batch`` stands in for Gmail's, Docs', Sheets' and Slides' at once
and ``/batch/{api}/{version}`` for Drive's. Pairing either route with any one document would assert
a correspondence that is not one to one, which is why this is its own kind of comparison rather
than a mount added to a source that already has one.

What it asserts is narrow, and in both directions:

* a document that declares no ``batchPath`` is reported, because Backlot goes on answering batch
  for an API whose own document no longer says it has one;
* a declared value no Backlot route answers is reported, which is what a value moving to a third
  shape looks like — identified by the document and the value together, so that acknowledging one
  move does not go on covering the next;
* a Backlot route no declared value selects is reported, which is what Drive moving to its own host
  would look like — ``/batch/{api}/{version}`` would go on being served while standing for nothing.

The three overlap on a large enough change: a vendor dropping batch everywhere reports each
document AND each route. That is deliberate, and they say different things — one API lost its batch
endpoint, versus this route now stands for no API at all — and which of them is true is what a
reader of the report needs to know.

Paths, not operations. ``batchPath`` names a path and no method, so what is compared on Backlot's
side is the paths under its batch mount rather than the ``(method, path)`` pairs the two path diffs
compare in.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from backlot.fidelity.fetch import fetch_json
from backlot.fidelity.findings import BREAKING, GAP, Finding
from backlot.fidelity.operations import canonical, from_backlot


class DocumentTarget(Protocol):
    """What this module needs of ONE document: where to fetch it. Structural, so the registry can
    import this module without this module importing the registry."""

    spec_url: str


class BatchTarget(Protocol):
    """What this module needs of the comparison: the documents to read the field out of, and the
    served prefix whose paths are the other side of it."""

    documents: tuple[DocumentTarget, ...]
    mount: tuple[str, ...]


def document_id(doc: Mapping[str, Any], url: str) -> str:
    """What a discovery document calls itself — ``gmail:v1``, ``drive:v3``.

    The document's own name rather than the URL it was fetched from, because this is a finding's
    identity and so a baseline key. Google serves one document at more than one URL: measured
    2026-09-12, ``gmail.googleapis.com/$discovery/rest?version=v1`` and
    ``www.googleapis.com/discovery/v1/apis/gmail/v1/rest`` both answer the document that calls
    itself ``gmail:v1``. Repointing the registry from one to the other must not read as a new
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
    """The batch endpoint a discovery document declares, canonical, or ``""`` if it declares none.

    Canonical because Backlot's side is: the two go through the same
    :func:`~backlot.fidelity.operations.canonical`, so a slash at either end cannot pass as a
    divergence.
    """
    declared = doc.get("batchPath")
    return canonical(declared) if isinstance(declared, str) else ""


def answers(template: str, path: str) -> bool:
    """Whether one of Backlot's batch routes answers a declared batch path.

    Segment by segment, with ``{}`` — what :func:`~backlot.fidelity.operations.canonical` leaves of
    a path parameter — matching any ONE segment. So ``batch/{}/{}`` answers ``batch/drive/v3`` and
    answers neither ``batch`` nor ``batch/drive/v3/files``: a route that swallowed a differing
    number of segments would report a moved ``batchPath`` as covered.
    """
    route, declared = template.split("/"), path.split("/")
    return len(route) == len(declared) and all(
        segment == "{}" or segment == theirs for segment, theirs in zip(route, declared)
    )


def divergences(comparison: BatchTarget, *, timeout: float = 120.0) -> list[Finding]:
    """This module's entry point: read the field out of every document, and compare.

    Every document the Google path diffs already read, which is where the list comes from rather
    than a second copy of it in the registry — see ``comparisons._documents_of``.
    """
    from backlot.main import app

    served = sorted({op.path for op in from_backlot(app.openapi(), comparison.mount).values()})
    out: list[Finding] = []
    selected: set[str] = set()
    for document in comparison.documents:
        doc = fetch_json(document.spec_url, timeout=timeout)
        who = document_id(doc, document.spec_url)
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
        answering = [route for route in served if answers(route, declared)]
        if not answering:
            # The document AND the value, because a gap is acknowledged by identity alone (see
            # `Baseline.unacknowledged`): keyed on the document, an acknowledged move to `v3/batch`
            # would go on covering a later move to anything else. The path diffs have the same
            # property for free, since there the moved path IS the identity.
            out.append(
                Finding(
                    "missing_batch_path",
                    GAP,
                    f"{who} {declared}",
                    f"the vendor batches at '{declared}'; no Backlot route answers that shape",
                )
            )
            continue
        selected.update(answering)
    for route in served:
        if route not in selected:
            out.append(
                Finding(
                    "extra_batch_route",
                    BREAKING,
                    f"/{route}",
                    "Backlot serves it, and no compared document declares a batchPath it answers",
                )
            )
    return sorted(out, key=lambda f: (f.severity != BREAKING, f.path, f.kind))
