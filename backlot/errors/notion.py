"""Notion's JSON media type.

Notion's refusals are built by its router (:func:`backlot.routers.notion._error`), not by an
envelope here, so this module shapes no body. What it carries is the one measured fact the router
cannot put on its own answers: the `content-type` real puts on a JSON body. Measured against
api.notion.com on 2026-09-22, each request with its own cache-buster: the 401 for no credential on
`GET /v1/users/me` and on `POST /v1/search`, the 400 for `GET /v1/nonexistent_thing/xyz`, the 400
for `POST /v1/users/me` and the 401 `HEAD /v1/users/me` returns each name
`application/json; charset=utf-8`. Backlot answered FastAPI's bare `application/json` on its 200s,
its `missing_version` 400 and a page's 404 as well, so a client that decodes by the declaration —
`requests`'s `r.text`, which guesses when no charset is named — reads a guess here where the vendor
names UTF-8, and a recorded fixture taken from Backlot does not match one taken from Notion byte
for byte in its headers.
"""

from __future__ import annotations

PREFIX = "/notion"

#: What real's JSON responses declare, 200 and refusal alike, on every Notion route measured.
JSON_MEDIA_TYPE = "application/json; charset=utf-8"


def owns(path: str) -> bool:
    return path == PREFIX or path.startswith(f"{PREFIX}/")


def http_body(path: str, exc, query=None) -> dict | None:
    """No opinion: Notion's refusals are ``JSONResponse`` the router already built, and an
    ``HTTPException`` under ``/notion`` keeps FastAPI's body as it did before this module."""
    return None


def validation_body(path: str, errors) -> None:
    """None: keep FastAPI's own 422 body, as Google's envelope does — a request-validation failure
    on a Notion route is not a shape real is measured to answer through this handler."""
    return None


def json_media_type(path: str, status_code: int) -> str:
    """The `content-type` real puts on a JSON body answered at ``path`` with ``status_code``:
    :data:`JSON_MEDIA_TYPE` on every status measured, 200 and refusal alike (see the module
    docstring). The parameter changes no byte of the body, JSON being UTF-8 by definition; what it
    changes is the header a client or a recorded fixture compares as a string."""
    return JSON_MEDIA_TYPE
