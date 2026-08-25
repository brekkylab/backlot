"""GraphQL serving layer.

``engine`` is vendor-agnostic: SDL text + a resolver map in, a GraphQL response envelope
out. Each GraphQL source contributes a ``<source>.graphql`` schema declaration and a
``<source>_resolvers.py`` that maps its fields onto :mod:`backlot.store`; the HTTP endpoint and
its auth scheme live in ``backlot/routers/<source>.py``, matching the per-source prefix
convention used by the REST sources.

``mcp_tools`` is vendor-agnostic too, and is the GraphQL answer to :mod:`backlot.openapi`: it turns
an introspection result into one MCP tool per root ``Query`` field, so a GraphQL-only source can be
reached by an agent at all. ``examples/using-mcp-with-agents/_graphql_bridge.py`` is its transport.
"""
