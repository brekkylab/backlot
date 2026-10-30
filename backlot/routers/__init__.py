"""Helpers shared by more than one vendor router.

Deliberately small. Anything that shapes a *response* stays in its own router even where two
vendors look similar — a user object, an error envelope and a page wrapper differ per vendor by
definition, and collapsing them is how an emulator serves the wrong vendor's shape. What lands
here is plumbing that is genuinely vendor-independent.
"""

from __future__ import annotations

import functools
import inspect
import re

import fastapi.routing
import starlette.routing
from fastapi import Request
from starlette.types import Scope


def _to_the_end_of_the_path(compile_path):
    """``compile_path`` with each route's pattern ending at the end of the path, `\\Z`, and with a
    `path` parameter taking a newline as it takes any other character.

    Starlette ends a pattern in `$`, which Python also matches just before a newline that ends the
    string, and its `path` parameter's `.*` stops at a newline. Real reads a `%0A` in a path as any
    other byte but on Slack, which drops one at the end (``slack.drop_the_end``): on 2026-10-19
    (UTC) a path a route here serves, with `%0A` after it, was the vendor's answer to a path no
    route matches on Jira, GitHub, Notion, Linear, Drive and Gmail, or where a route's parameter
    takes the newline, that route's answer: Jira's `project/search%0A` its 404 for the project key
    `search` and a newline, Notion's `users/me%0A` its 400 for a user id that is no UUID;
    Confluence's was a 404 too. On 2026-10-30 one such path on each of those hosts drew the same
    answer with `%0D` in place of the `%0A`, and Confluence's was its web app's page titled
    `Page Not Found`, both ways. Patched here, ahead of every vendor router, because each route is
    compiled when its router is built, and the copy FastAPI makes of it when the app includes the
    router is compiled again (``fastapi.routing``'s own name for the function)."""

    @functools.wraps(compile_path)
    def compile_to_the_end(path: str):
        regex, path_format, convertors = compile_path(path)
        pattern = regex.pattern.removesuffix("$") + r"\Z"
        return re.compile(pattern, re.DOTALL), path_format, convertors

    return compile_to_the_end


starlette.routing.compile_path = fastapi.routing.compile_path = _to_the_end_of_the_path(
    inspect.unwrap(starlette.routing.compile_path)
)


def sent_path(scope: Scope) -> str:
    """The path a client sends the vendor's own host: this request's as it arrived, escapes and
    all, with the first segment, which names the vendor here, taken off. A first segment with no
    slash after it leaves `/`."""
    raw = (scope.get("raw_path") or scope["path"].encode()).decode("latin-1")
    return "/" + raw.partition("/")[2].partition("/")[2]


async def json_body(request: Request) -> dict:
    """The request's JSON object, or ``{}`` for an absent, blank or non-object body.

    Several POST endpoints take an all-optional body (HubSpot's search, Notion's query) and their
    real APIs accept an empty one, so a malformed body reads as "no parameters" rather than
    becoming a 422 the vendor would never send.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — empty/invalid body → treat as no params
        return {}
    return body if isinstance(body, dict) else {}
