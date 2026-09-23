"""Slack is compared against its reference documentation, because it publishes nothing else.

Every other document source here reads a machine-readable description its vendor maintains. Slack
has none: the OpenAPI 2.0 document it once published is archived, its last content change dated
2020-10-06, and nothing replaced it. ``docs/fidelity.md``, "Slack is documented and asked, never read off a spec", carries
that measurement.

What Slack does maintain is the reference itself. ``docs/fidelity.md``, "Operations and arguments
come from the reference documentation", carries the measurement of its shape; this module is what
reads the one-line argument shape and ``method_name``/``http_method`` frontmatter that measurement
found.

Methods, not verbs. ``docs/fidelity.md``, "Methods are compared, not verbs", carries the
measurement for why. A method is identified here by its own name, the way Slack names it.

Response bodies are not read here — the documentation cannot state them, which is measured in
:mod:`backlot.fidelity.slack_probe`, the module that reads that layer instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

import httpx

from backlot.fidelity.errors import FidelityError
from backlot.fidelity.findings import BREAKING, GAP, Finding
from backlot.fidelity.operations import _METHODS

# A row of the reference index: the method's name linked to its own page.
_INDEX_ROW = re.compile(r"^\|\s*\[([A-Za-z0-9._]+)\]\((https://[^)]+\.md)\)", re.M)
# One argument, as every method page writes it: **`name`**`type`Required, with the type omitted on
# 187 of the 1442 measured 2026-09-22 (`**`team_id`**Optional`, 34 of them).
_ARGUMENT = re.compile(r"^\*\*`([^`]+)`\*\*(?:`[^`]*`)?(?:Required|Optional)\s*$", re.M)
_ARGUMENTS_SECTION = re.compile(r"^## Arguments \{#arguments\}\n(.*?)(?=^## |\Z)", re.S | re.M)
_METHOD_NAME = re.compile(r'^method_name:\s*"?([A-Za-z0-9._]+)"?\s*$', re.M)


class DocsTarget(Protocol):
    """What this module needs of a source. Structural, so the registry can import this module
    without this module importing the registry."""

    docs_url: str
    mount: tuple[str, ...]


@dataclass(frozen=True)
class Method:
    """One Web API method, reduced to what the documentation states about its request surface."""

    name: str
    arguments: frozenset[str]


def _fetch_text(url: str, *, timeout: float) -> str:
    """One markdown page, with a failure that says which page and why.

    Not :func:`backlot.fidelity.fetch.fetch_json` — the vendor side here is markdown, and that
    helper exists to insist the answer is a JSON object.
    """
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        raise FidelityError(f"{url} unreachable: {e}") from e
    if response.status_code != 200:
        raise FidelityError(f"{url} answered {response.status_code}")
    return response.text


def documented_methods(index: str) -> dict[str, str]:
    """Every method the reference index lists, mapped to the URL of its own page."""
    return {name: url for name, url in _INDEX_ROW.findall(index)}


def arguments(page: str) -> frozenset[str]:
    """The arguments one method page declares, required and optional together.

    Both, because the comparison is about which arguments a caller may send, and a vendor moving an
    argument from required to optional is not surface Backlot is missing.
    """
    section = _ARGUMENTS_SECTION.search(page)
    if not section:
        return frozenset()
    return frozenset(_ARGUMENT.findall(section.group(1)))


def from_backlot(spec: dict, mount: tuple[str, ...]) -> dict[str, Method]:
    """The Slack methods Backlot serves, taken from the app's own ``/openapi.json``.

    One :class:`Method` per method rather than per verb, and its arguments are the UNION over the
    verbs it answers: Backlot answers every one of them over GET and POST from a single
    ``api_route``, so the two carry identical parameters and a per-verb reading would report each
    method twice.
    """
    out: dict[str, Method] = {}
    for path, item in (spec.get("paths") or {}).items():
        if not any(path.startswith(m) for m in mount):
            continue
        name = path.rsplit("/", 1)[-1]
        params: set[str] = set()
        for verb in _METHODS:
            operation = item.get(verb)
            if not isinstance(operation, dict):
                continue
            params |= {
                p["name"]
                for p in operation.get("parameters") or ()
                if p.get("in") == "query" and p.get("name")
            }
        out[name] = Method(name, frozenset(params))
    return out


def diff_methods(backlot: dict[str, Method], real: dict[str, Method]) -> list[Finding]:
    """Every divergence in the request surface, both directions, methods and arguments."""
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
            Finding("missing_operation", GAP, name, "the vendor serves it; Backlot does not")
        )
    for name in sorted(set(backlot) & set(real)):
        mine, theirs = backlot[name], real[name]
        for argument in sorted(mine.arguments - theirs.arguments):
            out.append(
                Finding(
                    "extra_param",
                    BREAKING,
                    f"{name}?{argument}",
                    "Backlot accepts it; the vendor's reference documents no such argument",
                )
            )
        for argument in sorted(theirs.arguments - mine.arguments):
            out.append(
                Finding(
                    "missing_param",
                    GAP,
                    f"{name}?{argument}",
                    "the vendor accepts it; Backlot does not",
                )
            )
    return sorted(out, key=lambda f: (f.severity != BREAKING, f.path, f.kind))


def divergences(source: DocsTarget, *, timeout: float = 120.0) -> list[Finding]:
    """This module's entry point: read both request surfaces and compare them.

    The index is what the whole inventory is read from, and a method's own page is fetched only
    when Backlot serves it — the arguments of a method Backlot does not serve say nothing that its
    absence has not already said, and reading all 324 pages would spend 324 vendor round trips and
    3.0 MB to learn it.
    """
    from backlot.main import app

    pages = documented_methods(_fetch_text(source.docs_url, timeout=timeout))
    if not pages:
        raise FidelityError(
            f"{source.docs_url} listed no methods; is it still Slack's reference index?"
        )
    served = from_backlot(app.openapi(), source.mount)
    # Every documented method, its arguments left empty until a page is read for it. A method
    # Backlot does not serve is already a `missing_operation`, and an argument list under it would
    # be compared against nothing.
    real = {name: Method(name, frozenset()) for name in pages}
    for name in sorted(set(served) & set(real)):
        page = _fetch_text(pages[name], timeout=timeout)
        stated = _METHOD_NAME.search(page)
        # The index and the page must agree on which method this is. They are separate pages, and
        # a redirect that silently answers a different one would otherwise be read as this method's
        # argument list.
        if not stated or stated.group(1) != name:
            raise FidelityError(
                f"{pages[name]} states method_name "
                f"{stated.group(1) if stated else '(none)'}, not {name}"
            )
        real[name] = Method(name, arguments(page))
    return diff_methods(served, real)
