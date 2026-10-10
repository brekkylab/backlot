"""Comparing Backlot against the CSDL its vendor publishes.

Its own kind because of what the two sides are. The other document kinds compare paths: a vendor
enumerates one per operation, Backlot's ``app.openapi()`` enumerates its own, and the diff of the
two lists says something. A CSDL enumerates neither. It declares a TYPE SYSTEM: entity types and
their properties, which of those may be null, whether a type is closed to properties it does not
name, and the members of every enum.

So Backlot's side cannot be its own document either. The response models under ``/msgraph`` allow
extra fields and name none of them, deliberately, because a Graph response carries thirty keys and
mirroring that in a Pydantic model would be a second declaration to keep in step. What the
vendor's declaration is compared against is the responses a running server actually sends.

That is not a probe. A probe is for a contract that cannot be read off a document at all, and this
one reads off a document that happens to be richer than any other source's: every finding below
names a property, a type or an enum member the vendor declared, and quotes what it declared about
it. What a probe and this share is only that Backlot's side comes from a running server, which is a
consequence of the vendor's document describing types rather than routes.

Three findings, each classified as BREAKING by Backlot's schema-conformance gate:

- a property served on a CLOSED type that the type does not declare
- a property declared ``Nullable="false"`` served as ``null``
- an enum-typed property carrying a value the enum does not list

These are declaration mismatches, not guaranteed SDK parsing failures. The Python Graph SDK can
retain unknown properties in ``additional_data``; null and enum handling is client-dependent.
The Teams walk samples first pages and inline replies, not every response in a corpus.

A property the document declares and the response omits is NOT a finding. Every one of these APIs
projects: Graph returns eleven of ``user``'s eighty-one properties unless ``$select`` asks for
more, so "declared, not served" is the normal case and reporting it would bury the three above.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol

from backlot.fidelity.errors import FidelityError
from backlot.fidelity.fetch import fetch_text
from backlot.fidelity.findings import BREAKING, Finding

_EDM = "http://docs.oasis-open.org/odata/ns/edm"


@dataclass(frozen=True)
class Declared:
    """One type as the vendor declares it."""

    name: str
    # Property name -> its declared type, walked through BaseType so an inherited property counts
    # as declared. `id` lives on `graph.entity` and nowhere else, so without the walk every entity
    # in every response reports it as invented.
    properties: dict[str, str] = field(default_factory=dict)
    # Names the response may carry that are not properties: a navigation property is served inline
    # by `$expand`, which is a request the vendor documents.
    navigations: frozenset[str] = frozenset()
    # Declared `Nullable="false"`. Read as "not null WHEN RETURNED", never as "always returned".
    required: frozenset[str] = frozenset()
    # An open type may carry properties it does not declare, and two of Graph's own do. Checking
    # them for invented properties would report what the vendor permits.
    open: bool = False


@dataclass(frozen=True)
class Schema:
    """A vendor's whole type system, addressed the way its documents address it."""

    types: dict[str, Declared]
    enums: dict[str, frozenset[str]]

    def declared(self, name: str) -> Declared:
        try:
            return self.types[name]
        except KeyError:
            raise FidelityError(f"{name!r} is not a type this vendor's document declares")

    def members(self, type_ref: str) -> frozenset[str] | None:
        """The members of ``type_ref`` if it names an enum, else None."""
        return self.enums.get(_qualify(type_ref, self._aliases))

    _aliases: Mapping[str, str] = field(default_factory=dict)


def _qualify(ref: str | None, aliases: Mapping[str, str]) -> str | None:
    """``graph.entity`` -> ``microsoft.graph.entity``.

    A ``BaseType`` and a property's ``Type`` are written with the schema's ALIAS, which is what
    makes the inheritance walk terminate anywhere but the leaf if it is not resolved.
    """
    if not ref:
        return None
    head, _, tail = ref.rpartition(".")
    return f"{aliases.get(head, head)}.{tail}" if head else ref


def schema(document: str) -> Schema:
    """Parse a CSDL document into the declarations a comparison reads.

    Types are keyed by their FULLY QUALIFIED name. ``user``, ``identity`` and ``group`` are each
    declared in more than one of Graph's schema namespaces, so a bare-name registry silently
    answers with whichever came last: it reported eight of `user`'s properties as invented, all of
    them from a security-namespace type of the same name.
    """
    try:
        root = ET.fromstring(document)
    except ET.ParseError as e:
        raise FidelityError(f"vendor document is not parseable XML: {e}") from e

    aliases: dict[str, str] = {}
    raw: dict[str, dict[str, Any]] = {}
    enums: dict[str, frozenset[str]] = {}
    for element in root.iter(f"{{{_EDM}}}Schema"):
        namespace = element.get("Namespace")
        if element.get("Alias"):
            aliases[element.get("Alias")] = namespace
        for kind in ("EntityType", "ComplexType"):
            for declared in element.findall(f"{{{_EDM}}}{kind}"):
                properties = declared.findall(f"{{{_EDM}}}Property")
                raw[f"{namespace}.{declared.get('Name')}"] = {
                    "base": declared.get("BaseType"),
                    "open": declared.get("OpenType") == "true",
                    "properties": {p.get("Name"): p.get("Type") for p in properties},
                    "navigations": {
                        p.get("Name") for p in declared.findall(f"{{{_EDM}}}NavigationProperty")
                    },
                    "required": {p.get("Name") for p in properties if p.get("Nullable") == "false"},
                }
        for enum in element.findall(f"{{{_EDM}}}EnumType"):
            enums[f"{namespace}.{enum.get('Name')}"] = frozenset(
                m.get("Name") for m in enum.findall(f"{{{_EDM}}}Member")
            )
    if not raw:
        raise FidelityError("vendor document declares no types")

    resolved: dict[str, Declared] = {}
    for name in raw:
        properties: dict[str, str] = {}
        navigations: set[str] = set()
        required: set[str] = set()
        is_open = False
        seen: set[str] = set()
        current: str | None = name
        while current in raw and current not in seen:
            seen.add(current)
            entry = raw[current]
            is_open = is_open or entry["open"]
            for key, value in entry["properties"].items():
                properties.setdefault(key, value)
            navigations |= entry["navigations"]
            required |= entry["required"]
            current = _qualify(entry["base"], aliases)
        resolved[name] = Declared(
            name=name,
            properties=properties,
            navigations=frozenset(navigations),
            required=frozenset(required),
            open=is_open,
        )
    return Schema(types=resolved, enums=enums, _aliases=aliases)


def check(schema: Schema, type_name: str, obj: Mapping[str, Any], where: str) -> list[Finding]:
    """Every way one served object diverges from what its type declares.

    ``where`` names the request that produced it, because the property alone does not say which
    endpoint to go and look at.
    """
    declared = schema.declared(type_name)
    short = type_name.split(".")[-1]
    found: list[Finding] = []

    served = {k: v for k, v in obj.items() if not _annotation(k)}
    if not declared.open:
        for key in sorted(set(served) - set(declared.properties) - declared.navigations):
            found.append(
                Finding(
                    kind="undeclared_property",
                    severity=BREAKING,
                    path=f"{short}.{key}",
                    detail=(
                        f"{where} serves {key!r}, which {short} does not declare and which is a "
                        f"closed type: a client generated from the vendor's document has no field "
                        f"to bind it to"
                    ),
                )
            )
    for key in sorted(declared.required & set(served)):
        if served[key] is None:
            found.append(
                Finding(
                    kind="null_where_declared_non_nullable",
                    severity=BREAKING,
                    path=f"{short}.{key}",
                    detail=(
                        f"{where} serves {key!r} as null, which the vendor declares "
                        f'Nullable="false"'
                    ),
                )
            )
    for key, value in sorted(served.items()):
        members = schema.members(declared.properties.get(key))
        if members is not None and isinstance(value, str) and value not in members:
            found.append(
                Finding(
                    kind="value_outside_enum",
                    severity=BREAKING,
                    path=f"{short}.{key}",
                    detail=(
                        f"{where} serves {key!r} as {value!r}, which is not a member the vendor's "
                        f"enum lists ({', '.join(sorted(members))})"
                    ),
                )
            )
    return found


def _annotation(key: str) -> bool:
    """Whether a response key is OData plumbing rather than a property of the entity."""
    return "@odata." in key or key.endswith("@odata.count") or key.endswith("@odata.nextLink")


class CSDLTarget(Protocol):
    """What this module needs of a comparison. Structural, so the registry can import this module
    without this module importing the registry."""

    spec_url: str
    walk: "Callable[[str, str, Schema, float], Iterable[Finding]]"


def divergences(source: CSDLTarget, *, timeout: float = 120.0) -> list[Finding]:
    """This module's entry point: read the vendor's type system, start a server, and ask it.

    Fetching and parsing happen here so every CSDL source shares them; the source supplies only the
    walk that decides which requests to make and which type each answer is.

    The walk is handed ``server.token``, which ``serve()`` measures rather than assumes. A type
    nothing serves is a type nothing checks, so the walk reads as the service account: an
    ACL-filtered caller would leave whole containers unasked and report clean.
    """
    import backlot

    declared = schema(fetch_text(source.spec_url, timeout=timeout))
    with backlot.serve() as server:
        return list(source.walk(server.base_url, server.token, declared, timeout))
