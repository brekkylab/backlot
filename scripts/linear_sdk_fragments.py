#!/usr/bin/env python3
"""Check @linear/sdk's generated fragments against the Linear schema Backlot serves.

    python scripts/linear_sdk_fragments.py BUNDLE [--schema backlot/graphql/linear.graphql]

BUNDLE is the SDK's `dist/index.mjs`, where each generated fragment is GraphQL source in a string.
For each ``fragment X on T`` there, where the schema declares ``T`` with a field besides ``id``,
every field ``X`` selects at its top level has to be declared on ``T``: a field it is not is a
``Cannot query field`` the SDK meets as soon as it sends a document holding that fragment. A
fragment on a type the schema does not declare, or declares with ``id`` alone, is left out, since
Backlot does not serve that type in full.

`@linear/sdk` 95.1.0's `IssueLabel` fragment selects `groupType`, which 95.0.0's does not, and
against a schema without it the Linear example fails with `Cannot query field 'groupType' on type
'IssueLabel'`; an example pinned to 95.0.0 does not.

Prints how many fragments the bundle holds and how many were checked, then one line per fragment
selecting an undeclared field. Exit 0 when none does, 1 when one does, 2 when the bundle cannot be
read or holds no fragment, which a moved bundle or a changed string format would look like.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from graphql import GraphQLInterfaceType, GraphQLObjectType, build_schema, parse
from graphql.language import FieldNode

SCHEMA = Path(__file__).resolve().parent.parent / "backlot" / "graphql" / "linear.graphql"
_FRAGMENT = re.compile(r"fragment (\w+) on (\w+)\s*\{")


def fragments(source: str) -> dict[str, tuple[str, list[str]]]:
    """Each fragment in ``source``, by name: the type it is on and the fields its top level
    selects. The bundle writes a document as a string literal, so its escaped newlines and quotes
    are read back first."""
    text = source.replace("\\n", "\n").replace('\\"', '"')
    found: dict[str, tuple[str, list[str]]] = {}
    for m in _FRAGMENT.finditer(text):
        depth, end = 0, m.end() - 1
        while True:
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        definition = parse(text[m.start() : end + 1]).definitions[0]
        selected = sorted(
            {s.name.value for s in definition.selection_set.selections if isinstance(s, FieldNode)}
        )
        found[m.group(1)] = (m.group(2), selected)
    return found


def undeclared(found: dict, schema_source: str) -> tuple[int, list[tuple[str, str, list[str]]]]:
    """How many fragments are on a type the schema serves beyond ``id``, and each of those that
    selects a field the type does not declare, with the fields."""
    schema = build_schema(schema_source)
    checked, hits = 0, []
    for name, (on, selected) in sorted(found.items()):
        served = schema.type_map.get(on)
        if not isinstance(served, (GraphQLObjectType, GraphQLInterfaceType)):
            continue
        if set(served.fields) <= {"id"}:
            continue
        checked += 1
        missing = [f for f in selected if f not in served.fields and f != "__typename"]
        if missing:
            hits.append((name, on, missing))
    return checked, hits


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--schema", type=Path, default=SCHEMA)
    args = ap.parse_args(argv)
    try:
        source = args.bundle.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read {args.bundle}: {exc}")
        return 2
    found = fragments(source)
    if not found:
        print(f"{args.bundle} holds no `fragment … on …`; the bundle's layout may have changed")
        return 2
    checked, hits = undeclared(found, args.schema.read_text(encoding="utf-8"))
    print(f"{len(found)} fragments, {checked} on a type the schema serves beyond `id`")
    for name, on, missing in hits:
        print(f"fragment {name} on {on} selects what {on} does not declare: {', '.join(missing)}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
