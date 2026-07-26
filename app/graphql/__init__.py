"""GraphQL serving layer.

``engine`` is vendor-agnostic: SDL text + a resolver map in, a GraphQL response envelope
out. Each GraphQL source contributes a ``<vendor>.graphql`` schema declaration and a
``<vendor>_resolvers.py`` that maps its fields onto :mod:`app.store`; the HTTP endpoint and
its auth scheme live in ``app/routers/<vendor>.py``, matching the per-source prefix
convention used by the REST sources.
"""
