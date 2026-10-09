"""Reading a vendor's published document, with a failure that says which vendor and why.

One place, because the distinction it draws matters to every caller: a document that cannot be
fetched or parsed is not a divergence. `backlot diff` exits differently for the two, and a
scheduled run must not file a bug against Backlot because a CDN was down.
"""

from __future__ import annotations

import httpx

from backlot.fidelity.errors import FidelityError


def fetch_text(url: str, *, timeout: float = 120.0) -> str:
    """A vendor document that is not JSON, with the same failure this module's JSON reader gives.

    One vendor publishes its type system as CSDL XML rather than as JSON, and the distinction that
    matters is unchanged: a document that cannot be fetched is not a divergence, so it raises
    :class:`FidelityError` and never reaches a comparison as an empty result.
    """
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        raise FidelityError(f"{url} unreachable: {e}") from e
    if response.status_code != 200:
        raise FidelityError(f"{url} answered {response.status_code}")
    if not response.text.strip():
        raise FidelityError(f"{url} answered an empty body")
    return response.text


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
