"""Unit tests for the shared credential resolvers in :mod:`app.auth`.

The bearer/basic resolvers are covered end-to-end by the per-source endpoint tests; this
file covers the ones with a contract worth pinning on their own — currently the API-key
scheme, whose whole point is that it must accept a header with *no* auth scheme on it.
"""
from __future__ import annotations

from types import SimpleNamespace

from starlette.requests import Request

from app import auth


def _request(authorization: str | None = None, app=None) -> Request:
    headers = [(b"authorization", authorization.encode())] if authorization is not None else []
    return Request({"type": "http", "method": "POST", "path": "/x", "query_string": b"",
                    "headers": headers, "app": app})


def _app(acl) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(acl=acl))


# --- api_key_token --------------------------------------------------------------

def test_api_key_accepts_a_bare_key():
    assert auth.api_key_token(_request("lin_api_deadbeef")) == "lin_api_deadbeef"


def test_api_key_accepts_a_bearer_prefixed_token():
    assert auth.api_key_token(_request("Bearer lin_oauth_1234")) == "lin_oauth_1234"


def test_api_key_bearer_prefix_is_case_insensitive():
    assert auth.api_key_token(_request("bearer lin_oauth_1234")) == "lin_oauth_1234"


def test_api_key_keeps_an_unrecognised_scheme_verbatim():
    # Linear treats anything that isn't `Bearer <t>` as the key itself, so a stray scheme
    # becomes part of the key and simply fails to resolve — it is not silently stripped.
    assert auth.api_key_token(_request("Basic abc123")) == "Basic abc123"


def test_api_key_is_none_without_a_header():
    assert auth.api_key_token(_request()) is None


def test_api_key_is_none_for_a_blank_header():
    assert auth.api_key_token(_request("   ")) is None


def test_api_key_is_none_for_a_bare_bearer_header():
    assert auth.api_key_token(_request("Bearer")) is None


# --- resolve_api_key ------------------------------------------------------------

def test_resolve_api_key_resolves_a_bare_user_token(acl, tokens):
    caller = auth.resolve_api_key(_request(tokens["ava@acme.com"], app=_app(acl)))
    assert caller is not None
    assert caller.email == "ava@acme.com"
    assert caller.is_admin is False


def test_resolve_api_key_resolves_a_bearer_admin_token(acl, sample_settings):
    caller = auth.resolve_api_key(
        _request(f"Bearer {sample_settings.admin_token}", app=_app(acl)))
    assert caller is not None
    assert caller.is_admin is True


def test_resolve_api_key_rejects_an_unknown_key(acl):
    assert auth.resolve_api_key(_request("lin_api_nope", app=_app(acl))) is None
