"""Notion's JSON response media type.

Notion builds its own error envelopes in :mod:`backlot.routers.notion`; this module participates in
the shared vendor dispatch only to put the media type measured on the wire onto every JSON answer.
"""

from __future__ import annotations

PREFIX = "/notion"

#: Measured against api.notion.com on 2026-09-22: the 200 for ``/v1/users/me``, the credential 401,
#: an unpublished URL's 400, a page's 404 and the corresponding HEAD refusals all declared this.
JSON_MEDIA_TYPE = "application/json; charset=utf-8"


def owns(path: str) -> bool:
    return path.startswith(PREFIX)


def http_body(_path: str, _exc, _query=None) -> None:
    """Keep the response body selected by the Notion router."""
    return None


def validation_body(_path: str, _errors) -> None:
    """Keep FastAPI's validation response for paths the Notion router does not shape itself."""
    return None


def json_media_type(_path: str, _status_code: int) -> str:
    """The `content-type` real Notion puts on every JSON answer measured."""
    return JSON_MEDIA_TYPE
