"""Notion's JSON media type.

Notion's refusals are built by its router (:func:`backlot.routers.notion._error`), not by an
envelope here, so this module shapes no body; it names the charset real puts on a JSON body
(:data:`JSON_MEDIA_TYPE`).

Measured against api.notion.com on 2026-09-22 with no credential, each request with its own
cache-buster: the 401 on `GET /v1/users/me` and on `POST /v1/search`, the 400 for
`GET /v1/nonexistent_thing/xyz` and for `POST /v1/users/me`, and the 401 `HEAD /v1/users/me`
returns. On 2026-10-22 with a token and `Notion-Version: 2025-09-03`: the 200 of each route
``backlot.routers.notion`` serves (`users`, `users/me`, `users/{id}`, `search`, `pages/{id}`,
`blocks/{id}`, `blocks/{id}/children`, `comments`, `data_sources/{id}` and its `query`,
`databases/{id}` and, under `2022-06-28`, its `query`), a `HEAD` 200, and the `missing_version`,
`invalid_request_url`, `validation_error` and `object_not_found` refusals.
"""

from __future__ import annotations

PREFIX = "/notion"

#: The `content-type` of each Notion JSON answer measured, 200 and refusal alike.
JSON_MEDIA_TYPE = "application/json; charset=utf-8"


def owns(path: str) -> bool:
    return path == PREFIX or path.startswith(f"{PREFIX}/")


def http_body(path: str, exc, query=None) -> dict | None:
    """None: Notion's refusals are ``JSONResponse`` the router builds, and an ``HTTPException``
    under ``/notion`` keeps FastAPI's body."""
    return None


def validation_body(path: str, errors) -> None:
    """None: FastAPI's own 422 body is kept, a request-validation failure on a Notion route not
    being a shape real is measured to answer through this handler."""
    return None


def json_media_type(path: str, status_code: int) -> str:
    """:data:`JSON_MEDIA_TYPE`, whatever ``path`` and ``status_code`` (the module docstring has the
    measurement)."""
    return JSON_MEDIA_TYPE
