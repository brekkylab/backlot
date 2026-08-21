"""Derive MCP tools from a GraphQL introspection result — one tool per root ``Query`` field.

The REST sources reach MCP through ``/openapi.json``: an operation has a fixed request shape and
a fixed response body, so ``FastMCP.from_openapi()`` is the whole bridge. A GraphQL endpoint
describes nothing that way — ``POST /linear/graphql`` is one operation taking an arbitrary
document — which is why Linear and Fireflies had no MCP path at all. This module supplies the
missing description: it reads the schema the endpoint already serves over standard introspection
and produces, for each root field, a tool name, a JSON Schema for its arguments, and a complete
GraphQL document to send.

Everything here is pure — a mapping in, :class:`Tool` objects out. No HTTP, no MCP: the transport
is ``examples/using-mcp-with-agents/_graphql_bridge.py``, which posts ``tool.document`` with the
caller's own credential so the mock's per-token ACL decides what comes back.

**One tool per root field, not one ``graphql(query:)`` passthrough.** A passthrough is three lines
and turns the exercise into "can the model write GraphQL against a schema it has not seen"; none
of the sibling examples work that way, and a typed toolset is what an agent actually meets.

**The selection set is generated, which is the one thing OpenAPI never has to decide.** A REST
operation returns a fixed body; a GraphQL field returns whatever was asked for, so something must
choose. The rules, in full:

* Leaf fields (scalars and enums) are always selected.
* A Relay connection — an object carrying both ``nodes`` and ``pageInfo`` — is **transparent**
  wherever it appears: it expands to ``nodes { … } pageInfo { … }``. Unwrapping one at the root is
  free, so ``issues`` reaches as deep into an ``Issue`` as ``issue`` does; below the root it costs
  a level like any other object field. ``pageInfo`` always rides along, and for a nested
  connection it is load-bearing: there is no cursor to pass into ``Issue.comments``, so
  ``hasNextPage`` is the only thing keeping a truncated answer from reading as a complete one.
  Following them is also what makes an issue's comments reachable at all: ``CommentFilter`` carries
  ``id``/``body``/``createdAt`` and no key for the issue, so the root ``comments`` tool cannot
  stand in for ``Issue.comments``.
* An object field is followed while a **depth budget** remains, and each level spends one. The
  default is 2, which is what Fireflies needs to reach ``transcript.analytics.sentiments`` —
  ``Analytics`` has no leaf fields of its own, so a shallower selection drops the node whole. One
  depth does not suit both schemas, so the launchers choose: Linear runs at 1, because ``Team`` /
  ``Project`` / ``Cycle`` each carry dozens of configuration leaves and a second level takes an
  issue from 516 selected fields to 1,446 for data no agent asks about.
* A type already on the current path is not re-entered, so ``Issue.parent`` (an ``Issue``) stops
  there rather than recursing.
* A field with a required argument is skipped — the generator has no value to supply.
* An object field that ends up selecting nothing is dropped, because an empty selection set is
  not a legal document.

Argument schemas follow the same shape of rule: scalars and enums map directly, and input objects
expand recursively under the same path guard — so ``IssueFilter`` arrives with its real comparator
keys (``{"title": {"containsIgnoreCase": "…"}}``) rather than as an opaque object the model has to
guess at, and its self-referential ``and``/``or`` drop out by the guard rather than a special case.

Custom scalars become strings, which is how GraphQL serializes them absent a stated alternative;
where a vendor accepts more than one spelling (Fireflies' ``DateTime`` takes ISO 8601 or epoch
millis) the argument's own description carries it, and descriptions are copied through.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from graphql import get_introspection_query

#: The standard introspection document, from graphql-core itself rather than hand-written — the
#: bridge sends this and feeds the result straight to :func:`derive_tools`.
INTROSPECTION_QUERY = get_introspection_query(descriptions=True)

#: How many object levels below a root field's own type the generated selection set reaches.
DEFAULT_DEPTH = 2

# GraphQL's built-in scalars, plus the two names both vendors use for a loosely-typed object.
# Anything else is a custom scalar and serializes as a string.
_SCALARS: dict[str, dict[str, Any]] = {
    "Int": {"type": "integer"},
    "Float": {"type": "number"},
    "String": {"type": "string"},
    "Boolean": {"type": "boolean"},
    "ID": {"type": "string"},
    "JSON": {"type": "object"},
    "JSONObject": {"type": "object"},
}


@dataclass(frozen=True)
class Tool:
    """One MCP tool: a root ``Query`` field, its arguments as JSON Schema, and the document to post.

    ``document`` declares every argument as a GraphQL variable, so it is fixed per tool and the
    caller's arguments travel as ``variables``. A variable left unsupplied means the argument was
    not provided at all — which is what the resolvers see — so one document serves every call.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    document: str


def derive_tools(introspection: Mapping[str, Any], *, depth: int = DEFAULT_DEPTH) -> list[Tool]:
    """Turn an introspection response into one :class:`Tool` per root ``Query`` field.

    ``introspection`` is the endpoint's reply to :data:`INTROSPECTION_QUERY`, with or without its
    ``data`` envelope. Tools come back in schema order, which is the order the SDL declares.
    """
    body = introspection.get("data", introspection)
    schema = body["__schema"]
    types = {t["name"]: t for t in schema["types"]}
    root = types[schema["queryType"]["name"]]

    tools = []
    for field in root["fields"]:
        if field["name"].startswith("__"):
            continue  # introspection meta-fields are not part of the served surface
        tool = _tool(field, types, depth)
        if tool is not None:
            tools.append(tool)
    return tools


# --- one field -------------------------------------------------------------------------


def _tool(field: Mapping[str, Any], types: dict[str, Any], depth: int) -> Tool | None:
    named = _named(field["type"])
    selection = _root_selection(named, types, depth)
    # A field whose type yields no selectable field cannot be asked for at all: the document
    # would carry an empty selection set, which does not parse. Nothing in either vendor's
    # schema hits this; it is here so a schema that does loses one tool rather than every tool.
    if selection is None and named["kind"] == "OBJECT":
        return None

    args = field["args"]
    properties, required = {}, []
    for arg in args:
        schema = _json_schema(arg["type"], types, frozenset())
        if schema is None:
            # Nothing describable in it, so the tool cannot offer the argument — and leaving a
            # REQUIRED one out means a document the schema rejects, which is worse than one tool
            # fewer. Neither vendor's schema reaches this; a wider one might.
            if _is_required(arg):
                return None
            continue
        if arg.get("description"):
            schema = {**schema, "description": arg["description"]}
        properties[arg["name"]] = schema
        if _is_required(arg):
            required.append(arg["name"])

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        input_schema["required"] = required

    return Tool(
        name=field["name"],
        description=_description(field, depth),
        input_schema=input_schema,
        document=_document(field["name"], [a for a in args if a["name"] in properties], selection),
    )


def _description(field: Mapping[str, Any], depth: int) -> str:
    """The field's own description, plus what the generated selection set did."""
    own = (field.get("description") or "").strip()
    returns = f"Returns `{_type_str(field['type'])}`"
    named = _named(field["type"])
    if named["kind"] == "OBJECT":
        returns += f", with its fields selected automatically to a depth of {depth}"
    return f"{own}\n\n{returns}.".strip()


def _document(name: str, args: list[Mapping[str, Any]], selection: str | None) -> str:
    """``query <name>($a: T, …) { <name>(a: $a, …) <selection> }``."""
    header = name
    if args:
        header += "(" + ", ".join(f"${a['name']}: {_type_str(a['type'])}" for a in args) + ")"
    call = name
    if args:
        call += "(" + ", ".join(f"{a['name']}: ${a['name']}" for a in args) + ")"
    body = f"{call} {selection}" if selection else call
    return f"query {header} {{\n  {body}\n}}\n"


# --- selection sets --------------------------------------------------------------------


def _root_selection(named: Mapping[str, Any], types: dict[str, Any], depth: int) -> str | None:
    """The selection set for a root field's own type. The whole budget goes to the type itself:
    unwrapping a connection at the root is free, so ``issues`` reaches as deep into an ``Issue``
    as ``issue`` does."""
    if named["kind"] != "OBJECT":
        return None  # a scalar or enum field takes no selection set
    return _field_selection(named["name"], types, depth, frozenset(), 1)


def _field_selection(
    type_name: str, types: dict[str, Any], budget: int, path: frozenset[str], indent: int
) -> str | None:
    """One object type's selection set, with a connection expanded transparently.

    ``pageInfo`` rides along wherever the connection appears. A nested page cannot be advanced —
    the generator has no cursor to pass and nothing to pass it to — so ``hasNextPage`` is the
    only thing that keeps a truncated answer from reading as a complete one.
    """
    connection = _connection(types[type_name])
    if connection is None:
        return _selection(type_name, types, budget, path | {type_name}, indent)
    node, page_type = connection
    if node in path:
        return None  # e.g. `Issue.children`, a connection back to an Issue already on the path
    nodes = _selection(node, types, budget, path | {node}, indent + 1)
    if nodes is None:
        return None
    page = _selection(page_type, types, 0, frozenset(), indent + 1)
    return _block([f"nodes {nodes}", f"pageInfo {page}"], indent)


def _selection(
    type_name: str, types: dict[str, Any], budget: int, path: frozenset[str], indent: int = 1
) -> str | None:
    parts = []
    for field in types[type_name]["fields"]:
        named = _named(field["type"])
        if named["kind"] in ("SCALAR", "ENUM"):
            parts.append(field["name"])
            continue
        # A union or interface needs a type condition per member, which is a selection the
        # caller would have to author; unions appear only on stub fields in either schema.
        if named["kind"] != "OBJECT":
            continue
        if any(_is_required(arg) for arg in field["args"]):
            continue
        if named["name"] in path or budget <= 0:
            continue
        sub = _field_selection(named["name"], types, budget - 1, path, indent + 1)
        if sub is not None:
            parts.append(f"{field['name']} {sub}")
    return _block(parts, indent) if parts else None


def _block(parts: list[str], indent: int) -> str:
    pad = "  " * indent
    return "{\n" + "".join(f"{pad}  {p}\n" for p in parts) + pad + "}"


def _connection(type_: Mapping[str, Any]) -> tuple[str, str] | None:
    """A Relay connection's ``(node type, page-info type)``, or ``None`` if it is not one.

    Carrying both ``nodes`` and ``pageInfo`` is the whole test, and both type names are read off
    those fields: a schema is free to call its page type something other than ``PageInfo``.
    """
    fields = {f["name"]: f for f in (type_.get("fields") or ())}
    if not {"nodes", "pageInfo"} <= set(fields):
        return None
    return _named(fields["nodes"]["type"])["name"], _named(fields["pageInfo"]["type"])["name"]


# --- arguments -------------------------------------------------------------------------


def _json_schema(
    ref: Mapping[str, Any], types: dict[str, Any], path: frozenset[str]
) -> dict[str, Any] | None:
    kind = ref["kind"]
    if kind == "NON_NULL":
        return _json_schema(ref["ofType"], types, path)
    if kind == "LIST":
        items = _json_schema(ref["ofType"], types, path)
        return None if items is None else {"type": "array", "items": items}
    if kind == "SCALAR":
        return dict(_SCALARS.get(ref["name"], {"type": "string"}))
    if kind == "ENUM":
        return {"type": "string", "enum": [v["name"] for v in types[ref["name"]]["enumValues"]]}
    if kind == "INPUT_OBJECT":
        return _input_object(ref["name"], types, path)
    return None  # an object type cannot be an argument


def _input_object(name: str, types: dict[str, Any], path: frozenset[str]) -> dict[str, Any] | None:
    if name in path:
        return None  # e.g. IssueFilter.and: [IssueFilter!] — the same guard as the output side
    properties, required = {}, []
    for field in types[name]["inputFields"]:
        schema = _json_schema(field["type"], types, path | {name})
        if schema is None:
            continue
        if field.get("description"):
            schema = {**schema, "description": field["description"]}
        properties[field["name"]] = schema
        if _is_required(field):
            required.append(field["name"])
    if not properties:
        return None
    out: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        out["required"] = required
    return out


def _is_required(arg: Mapping[str, Any]) -> bool:
    return arg["type"]["kind"] == "NON_NULL" and arg.get("defaultValue") is None


def _named(ref: Mapping[str, Any]) -> Mapping[str, Any]:
    """Strip the ``NON_NULL``/``LIST`` wrappers off a type reference."""
    while ref.get("ofType"):
        ref = ref["ofType"]
    return ref


def _type_str(ref: Mapping[str, Any]) -> str:
    if ref["kind"] == "NON_NULL":
        return _type_str(ref["ofType"]) + "!"
    if ref["kind"] == "LIST":
        return "[" + _type_str(ref["ofType"]) + "]"
    return ref["name"]
