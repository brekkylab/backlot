"""GraphQL serving layer.

``engine`` is vendor-agnostic: SDL text + a resolver map in, a GraphQL response envelope
out. Each GraphQL source contributes a ``<source>.graphql`` schema declaration and a
``<source>_resolvers.py`` that maps its fields onto :mod:`backlot.store`; the HTTP endpoint and
its auth scheme live in ``backlot/routers/<source>.py``, matching the per-source prefix
convention used by the REST sources.
"""
