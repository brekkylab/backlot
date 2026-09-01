"""Comparing the GraphQL schema Backlot serves against the one its vendor answers introspection with.

The comparison runs in BOTH directions, and over arguments as well as fields. A one-way field-only
diff was run against Linear once and reported a clean result while ten fields and four arguments
were missing, because everything it could have found was on the side it did not walk.

Both schemas arrive as ``GraphQLSchema`` — Backlot's from its own SDL, the vendor's from an
introspection response through ``build_client_schema`` — so the walk below is over graphql-core's
type objects rather than over SDL text. Printed SDL differs by field order and description
wording, neither of which is a divergence.

Findings carry one of two severities:

``breaking``
    Backlot contradicts the vendor: it serves a field or argument the vendor does not have, or
    the same name at a different type. Code written against Backlot compiles and then behaves
    differently in production, which is the failure this project exists to prevent.
``gap``
    The vendor has surface Backlot does not. Backlot serves a deliberate subset, so most of these
    are scope rather than bugs — which is what the baseline is for. A gap on a type Backlot does
    serve is still worth reading: it is where a real client's query fails against Backlot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Protocol

import httpx
from graphql import (
    GraphQLEnumType,
    GraphQLSchema,
    build_client_schema,
    build_schema,
    get_introspection_query,
    is_enum_type,
    is_input_object_type,
    is_object_type,
    is_specified_scalar_type,
)


import backlot.graphql
from backlot.fidelity.errors import FidelityError
from backlot.fidelity.findings import BREAKING, GAP, Finding


def _comparable(schema: GraphQLSchema) -> dict[str, object]:
    """Named types worth comparing: no introspection machinery, no built-in scalars."""
    return {
        name: t
        for name, t in schema.type_map.items()
        if not name.startswith("__") and not is_specified_scalar_type(t)
    }


def _fields(t: object) -> Mapping[str, object]:
    return getattr(t, "fields", {}) or {}


def _diff_args(mock_field, real_field, path: str) -> list[Finding]:
    backlot_args = getattr(mock_field, "args", {}) or {}
    real_args = getattr(real_field, "args", {}) or {}
    out: list[Finding] = []
    for name in sorted(set(backlot_args) - set(real_args)):
        out.append(
            Finding(
                "extra_arg",
                BREAKING,
                f"{path}({name})",
                f"Backlot accepts {name}: {backlot_args[name].type}; the vendor has no such argument",
            )
        )
    for name in sorted(set(real_args) - set(backlot_args)):
        out.append(
            Finding(
                "missing_arg",
                GAP,
                f"{path}({name})",
                f"vendor accepts {name}: {real_args[name].type}; Backlot does not",
            )
        )
    for name in sorted(set(backlot_args) & set(real_args)):
        backlot_t, real_t = str(backlot_args[name].type), str(real_args[name].type)
        if backlot_t != real_t:
            out.append(
                Finding(
                    "arg_type_mismatch",
                    BREAKING,
                    f"{path}({name})",
                    f"vendor: {real_t}, Backlot: {backlot_t}",
                )
            )
    return out


def _diff_object(backlot_t, real_t, name: str) -> list[Finding]:
    backlot_fields, real_fields = _fields(backlot_t), _fields(real_t)
    out: list[Finding] = []
    for f in sorted(set(backlot_fields) - set(real_fields)):
        out.append(
            Finding(
                "extra_field",
                BREAKING,
                f"{name}.{f}",
                f"Backlot serves {f}: {backlot_fields[f].type}; the vendor has no such field",
            )
        )
    for f in sorted(set(real_fields) - set(backlot_fields)):
        out.append(
            Finding(
                "missing_field",
                GAP,
                f"{name}.{f}",
                f"vendor serves {f}: {real_fields[f].type}; Backlot does not",
            )
        )
    for f in sorted(set(backlot_fields) & set(real_fields)):
        mock_f, real_f = backlot_fields[f], real_fields[f]
        backlot_type, real_type = str(mock_f.type), str(real_f.type)
        if backlot_type != real_type:
            out.append(
                Finding(
                    "type_mismatch",
                    BREAKING,
                    f"{name}.{f}",
                    f"vendor: {real_type}, Backlot: {backlot_type}",
                )
            )
        # Input object fields carry no arguments; only object/interface fields do.
        if is_object_type(backlot_t):
            out.extend(_diff_args(mock_f, real_f, f"{name}.{f}"))
    return out


def _diff_enum(backlot_t: GraphQLEnumType, real_t: GraphQLEnumType, name: str) -> list[Finding]:
    out: list[Finding] = []
    for v in sorted(set(backlot_t.values) - set(real_t.values)):
        out.append(
            Finding(
                "extra_enum_value", BREAKING, f"{name}.{v}", "the vendor's enum has no such value"
            )
        )
    for v in sorted(set(real_t.values) - set(backlot_t.values)):
        out.append(
            Finding(
                "missing_enum_value", GAP, f"{name}.{v}", "the vendor's enum carries this value"
            )
        )
    return out


def diff_schemas(backlot: GraphQLSchema, real: GraphQLSchema) -> list[Finding]:
    """Every divergence between two schemas, ordered by severity then path.

    Types the vendor has and Backlot does not are NOT reported one per type. Backlot declares the
    subset it serves, so the vendor's remaining ninety-odd types would bury the findings that are
    about surface Backlot actually claims. They surface where they matter instead: as a
    ``missing_field`` on a type Backlot does serve.
    """
    backlot_types, real_types = _comparable(backlot), _comparable(real)
    out: list[Finding] = []
    for name in sorted(backlot_types):
        backlot_t = backlot_types[name]
        real_t = real_types.get(name)
        if real_t is None:
            out.append(
                Finding("extra_type", BREAKING, name, "the vendor's schema declares no such type")
            )
            continue
        if is_enum_type(backlot_t) and is_enum_type(real_t):
            out.extend(_diff_enum(backlot_t, real_t, name))
        elif (is_object_type(backlot_t) and is_object_type(real_t)) or (
            is_input_object_type(backlot_t) and is_input_object_type(real_t)
        ):
            out.extend(_diff_object(backlot_t, real_t, name))
        elif type(backlot_t) is not type(real_t):
            out.append(
                Finding(
                    "kind_mismatch",
                    BREAKING,
                    name,
                    f"vendor: {type(real_t).__name__}, Backlot: {type(backlot_t).__name__}",
                )
            )
    return sorted(out, key=lambda f: (f.severity != BREAKING, f.path, f.kind))


# --------------------------------------------------------------------------- loading


class GraphQLTarget(Protocol):
    """What this module needs of a source. Structural, so the registry can import this module
    without this module importing the registry."""

    name: str
    endpoint: str

    def authorization(self, resolved: Mapping[str, str]) -> str: ...


def backlot_schema(name: str) -> GraphQLSchema:
    """The schema Backlot serves for a source, built from the SDL the server builds from.

    The same file ``backlot.graphql.engine.from_sdl`` loads at import, so this compares what is
    served without starting a server or importing a corpus.
    """
    sdl = Path(backlot.graphql.__file__).parent / f"{name}.graphql"
    if not sdl.is_file():
        raise FidelityError(f"no SDL for {name!r} at {sdl}")
    return build_schema(sdl.read_text())


def real_schema(endpoint: str, authorization: str, *, timeout: float = 60.0) -> GraphQLSchema:
    """The vendor's own schema, read by introspection.

    Descriptions are not requested: they are prose, they change without the contract changing, and
    a diff that reports reworded documentation is a diff nobody reads twice.
    """
    try:
        response = httpx.post(
            endpoint,
            json={"query": get_introspection_query(descriptions=False)},
            headers={"Authorization": authorization},
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise FidelityError(f"{endpoint} unreachable: {e}") from e
    if response.status_code != 200:
        raise FidelityError(f"{endpoint} answered {response.status_code}")
    body = response.json()
    if body.get("errors"):
        raise FidelityError(f"introspection refused: {body['errors']}")
    if not body.get("data"):
        raise FidelityError("introspection returned no data")
    return build_client_schema(body["data"])


def divergences(
    source: GraphQLTarget,
    credentials: Mapping[str, str] | None = None,
    *,
    timeout: float = 60.0,
) -> list[Finding]:
    """This module's entry point: load both schemas for a source and compare them.

    ``credentials`` arrive already resolved — the registry does that once for every kind, so a
    credential a source does not declare is refused there rather than ignored here.
    """
    authorization = source.authorization(credentials or {})
    return diff_schemas(
        backlot_schema(source.name), real_schema(source.endpoint, authorization, timeout=timeout)
    )
