#!/usr/bin/env python3
"""Check @linear/sdk's generated fragments against the Linear schema Backlot serves.

    python scripts/linear_sdk_fragments.py BUNDLE [--schema backlot/graphql/linear.graphql]

BUNDLE is the SDK's `dist/index.mjs`, where each generated fragment is GraphQL source in a template
literal (``new TypedDocumentString(`…`)``), its newlines as they are. For each ``fragment X on T``
there, where the schema declares ``T`` with a field besides ``id``, every field ``X`` selects at its
top level has to be declared on ``T``: a field it is not is a ``Cannot query field`` the SDK meets
as soon as it sends a document holding that fragment. A fragment on a type the schema does not
declare, or declares with ``id`` alone, is left out, since Backlot does not serve that type in full.

Prints how many fragments the bundle holds and how many were checked, then one line per fragment
selecting an undeclared field. Exit 0 when none does, 1 when one does, 2 when the bundle or the
schema cannot be read, or the bundle holds no fragment or one that does not parse, which a moved
bundle or a changed string format would look like. Run by its path, it exits 2 on any other
exception too, the traceback on stderr, where Python would exit 1, the code of a fragment named.
"""

from __future__ import annotations

import argparse
import re
import sys
import traceback
from pathlib import Path

from graphql import GraphQLError, GraphQLInterfaceType, GraphQLObjectType, build_schema, parse
from graphql.language import FieldNode

SCHEMA = Path(__file__).resolve().parent.parent / "backlot" / "graphql" / "linear.graphql"
_NAME = r"[_A-Za-z][_0-9A-Za-z]*"  # a GraphQL Name
_FRAGMENT = re.compile(rf"fragment ({_NAME}) on ({_NAME})[ \t\n\r,]*\{{")


def fragments(source: str) -> dict[str, tuple[str, list[str]]]:
    """Each fragment in ``source``, by name: the type it is on and the fields its top level
    selects. ``ValueError`` for a fragment whose braces never close or that does not parse."""
    found: dict[str, tuple[str, list[str]]] = {}
    for m in _FRAGMENT.finditer(source):
        depth, end = 0, m.end() - 1
        while end < len(source):
            if source[end] == "{":
                depth += 1
            elif source[end] == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        else:
            raise ValueError(f"fragment {m.group(1)}'s braces never close")
        try:
            definition = parse(source[m.start() : end + 1]).definitions[0]
        except GraphQLError as exc:
            raise ValueError(
                f"fragment {m.group(1)} does not parse: {exc.message.rstrip('.')}"
            ) from None
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
    texts = []
    for path in (args.bundle, args.schema):
        try:
            texts.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            print(f"cannot read {path}: {exc}")
            return 2
    source, schema_source = texts
    try:
        found = fragments(source)
    except ValueError as exc:
        print(f"{args.bundle}: {exc}; the bundle's layout may have changed")
        return 2
    if not found:
        print(f"{args.bundle} holds no `fragment … on …`; the bundle's layout may have changed")
        return 2
    checked, hits = undeclared(found, schema_source)
    print(f"fragments: {len(found)}, checked: {checked}")
    for name, on, missing in hits:
        print(f"fragment {name} on {on} selects what {on} does not declare: {', '.join(missing)}")
    return 1 if hits else 0


if __name__ == "__main__":
    try:
        status = main(sys.argv[1:])
    except Exception:
        traceback.print_exc()
        status = 2
    sys.exit(status)
