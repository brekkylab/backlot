"""Helpers shared by more than one vendor router.

Deliberately small. Anything that shapes a *response* stays in its own router even where two
vendors look similar — a user object, an error envelope and a page wrapper differ per vendor by
definition, and collapsing them is how a mock starts serving the wrong vendor's shape. What lands
here is plumbing that is genuinely vendor-independent.
"""

from __future__ import annotations

from fastapi import Request


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
