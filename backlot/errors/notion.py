"""Notion's JSON response media type.

Notion builds its own error envelopes in :mod:`backlot.routers.notion`; this module participates in
the shared vendor dispatch only to put the media type measured on the wire onto every JSON answer.
"""

from __future__ import annotations

PREFIX = "/notion"

#: Measured against api.notion.com: the refusals a caller with no token gets on 2026-09-22, and on
#: 2026-10-15 25 answers (nine 200s, eleven 400s, two 401s, three 404s), `HEAD` and `OPTIONS`
#: among them, with no `Accept` and with four values of it. Every one declared this.
JSON_MEDIA_TYPE = "application/json; charset=utf-8"


def owns(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def http_body(_path: str, _exc, _query=None) -> None:
    """Keep the response body selected by the Notion router."""
    return None


def validation_body(_path: str, _errors) -> None:
    """Keep FastAPI's validation response for paths the Notion router does not shape itself."""
    return None


def json_media_type(_path: str, _status_code: int) -> str:
    """The `content-type` real Notion puts on every JSON answer measured."""
    return JSON_MEDIA_TYPE
