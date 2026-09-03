"""Reading a vendor's published document, with a failure that says which vendor and why.

One place, because the distinction it draws matters to every caller: a document that cannot be
fetched or parsed is not a divergence. `backlot diff` exits differently for the two, and a
scheduled run must not file a bug against Backlot because a CDN was down.
"""

from __future__ import annotations

import httpx

from backlot.fidelity.errors import FidelityError


def fetch_json(url: str, *, timeout: float = 120.0) -> dict:
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        raise FidelityError(f"{url} unreachable: {e}") from e
    if response.status_code != 200:
        raise FidelityError(f"{url} answered {response.status_code}")
    try:
        document = response.json()
    except ValueError as e:
        raise FidelityError(f"{url} is not JSON: {e}") from e
    # A 200 carrying valid JSON that is not an object used to reach the caller and fail deep in a
    # parser as `AttributeError`, which `backlot diff` cannot catch: it left as a traceback with
    # exit 1, the status that files an issue against Backlot for a vendor serving nonsense.
    if not isinstance(document, dict):
        raise FidelityError(f"{url} answered JSON {type(document).__name__}, not an object")
    return document
