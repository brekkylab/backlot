"""HubSpot addresses each published document by release, so its URL has to be looked up.

Every other vendor Backlot compares against publishes its document at a URL that stays put. HubSpot
publishes an INDEX of its APIs, and each version of each one names the release its document is
currently at — ``…/public/api/spec/v2/specs/release/22154/version/3``.

Pinning the resolved URL is not the simplification it looks like. Measured on 2026-09-01, all three
releases the index named for Custom Objects still serve, each returning its own frozen document:
release 22154 answers nine paths as ``v3``, 75278 answers eleven as ``2026-03``, 165137 answers
thirteen. So a pinned URL never 404s to tell you it has gone stale — it goes on serving the document
frozen on the day it was pinned, and the comparison reports no drift forever. That is the one
outcome this command exists to prevent, so the index is re-read on every run.

This lives apart from :mod:`backlot.fidelity.openapi_diff` because none of it is about OpenAPI. That
module reads a document; which document to read is one vendor's private arrangement.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from backlot.fidelity.errors import FidelityError


def _entry_url(catalog: Mapping[str, Any], api: str, version: str) -> str:
    for entry in catalog.get("results") or ():
        if entry.get("name") != api:
            continue
        for published in entry.get("versions") or ():
            if str(published.get("version")) == version and published.get("openApi"):
                return published["openApi"]
        # Named the API but not the version: say which half was wrong, because the two are fixed in
        # different places — a version is retired by the vendor, a name is a typo in the registry.
        raise FidelityError(f"{api!r} publishes no version {version!r}")
    raise FidelityError(f"no API named {api!r} in the catalog")


def entry(api: str, version: str) -> Callable[[Mapping[str, Any]], str]:
    """A ``resolve_url`` hook that picks one API's version out of HubSpot's index."""

    def resolve(catalog: Mapping[str, Any]) -> str:
        return _entry_url(catalog, api, version)

    return resolve
