"""Unit tests for the shared credential resolvers in :mod:`backlot.auth`.

The bearer/basic resolvers are covered end-to-end by the per-source endpoint tests; this
file covers the ones with a contract worth pinning on their own: the API-key scheme, whose whole
point is that it must accept a header with *no* auth scheme on it, and Atlassian's Basic, which
authenticates the email and the token as a pair.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backlot import auth


def _request(authorization: str | None = None, app=None) -> Request:
    headers = [(b"authorization", authorization.encode())] if authorization is not None else []
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/x",
            "query_string": b"",
            "headers": headers,
            "app": app,
        }
    )


def _app(acl) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(acl=acl))


# --- require_bearer / atlassian_caller ------------------------------------------


def test_require_bearer_raises_401_carrying_the_vendors_own_detail(acl):
    """The detail string is the VENDOR's: GitHub says "Bad credentials", Google "Invalid
    Credentials", Notion "API token is invalid". A client that string-matches its vendor's error
    has to keep matching, so the message is a parameter rather than something this helper
    invents."""
    with pytest.raises(HTTPException) as e:
        auth.require_bearer(_request(app=_app(acl)), "Bad credentials")
    assert e.value.status_code == 401 and e.value.detail == "Bad credentials"


def test_require_bearer_returns_the_caller_for_a_good_token(acl, tokens):
    caller = auth.require_bearer(
        _request(f"Bearer {tokens['ava@acme.com']}", app=_app(acl)), "nope"
    )
    assert caller.email == "ava@acme.com"


def test_atlassian_caller_accepts_either_scheme(acl, tokens, sample_settings):
    """Atlassian carries Basic email:api_token and also accepts a bearer. Not an OAuth 3LO token
    standing in for Atlassian's: see :func:`auth.atlassian_caller` for why the surface Backlot
    serves reads a bearer as something else."""
    token = tokens["ava@acme.com"]
    basic = base64.b64encode(f"ava@acme.com:{token}".encode()).decode()
    assert auth.atlassian_caller(_request(f"Basic {basic}", app=_app(acl))).email == "ava@acme.com"
    assert auth.atlassian_caller(_request(f"Bearer {token}", app=_app(acl))).email == "ava@acme.com"


def test_atlassian_caller_is_anonymous_when_no_credential_resolves(acl):
    """A credential Atlassian cannot resolve does not refuse the request here: each API decides
    that for itself, and Jira's decision is to serve the anonymous caller."""
    caller = auth.atlassian_caller(_request(app=_app(acl)))
    assert caller.is_anonymous and caller.email is None and not caller.is_admin


@pytest.mark.parametrize(
    "raw, kind",
    [
        ("nobody@example.com:wrongtoken", auth.BASIC_PAIR),
        ("nobody@example.com:", auth.BASIC_UNPARSEABLE),
        (":wrongtoken", auth.BASIC_UNPARSEABLE),
        (":", auth.BASIC_UNPARSEABLE),
        ("nobody@example.com", auth.BASIC_UNPARSEABLE),
        ("nobody@example.com:a:b", auth.BASIC_UNPARSEABLE),
        ("", auth.BASIC_ABSENT),
    ],
)
def test_basic_credential_kind_splits_a_pair_from_a_value_atlassian_cannot_read(raw, kind):
    """Confluence answers a credential it read and rejected differently from one it could not
    read, so the two have to be told apart before either is refused. A pair is exactly one
    non-empty user and one non-empty password around a single colon; a decoded value that is empty
    counts as no credential rather than an unreadable one. Measured on both sites, every shape."""
    value = base64.b64encode(raw.encode()).decode()
    assert auth.basic_credential_kind(_request(f"Basic {value}")) == kind


@pytest.mark.parametrize(
    "header", [None, "Basic", "Basic !!!notbase64!!!", "Bogus xyz", "Bearer t"]
)
def test_basic_credential_kind_reports_no_basic_pair_as_absent_or_unparseable(header):
    """A missing header, an unknown scheme and a bearer carry no Basic credential at all, which is
    the same to Confluence as sending none: it answers those with its 403, not with the 401 a
    value it cannot decode gets. ``Basic`` with nothing after it has nothing to decode either."""
    expected = auth.BASIC_UNPARSEABLE if header == "Basic !!!notbase64!!!" else auth.BASIC_ABSENT
    assert auth.basic_credential_kind(_request(header)) == expected


# --- atlassian_bearer_token: the one spelling the real site recognises ----------


@pytest.mark.parametrize(
    "header, token",
    [
        ("Bearer usr-abc123", "usr-abc123"),
        (" Bearer usr-abc123", "usr-abc123"),  # leading space is stripped
        ("Bearer usr-abc123 ", "usr-abc123"),  # so is trailing
        ("bearer usr-abc123", None),  # the scheme is case-SENSITIVE here
        ("BEARER usr-abc123", None),
        ("token usr-abc123", None),  # GitHub's spelling, which Atlassian does not take
        ("OAuth usr-abc123", None),
        ("Bearer  usr-abc123", None),  # exactly one space
        ("Bearer\tusr-abc123", None),  # a tab is not a space
        ("Bearerusr-abc123", None),
        ("Bearer", None),
        ("Bearer ", None),
        (None, None),
    ],
)
def test_atlassian_bearer_token_takes_only_the_spelling_the_real_site_reads(header, token):
    """Which spellings count as a bearer credential, measured against ecosystem.atlassian.net and
    brekkylab.atlassian.net on 2026-09-04 on a Jira route: a recognised one is refused (403), and
    an unrecognised one is processed anonymously (200), so the answer says which the site read.
    Both sites agreed on every row.

    Not :func:`auth.bearer_token`, which is deliberately permissive because GitHub really does
    accept ``token <t>`` and RFC 7235 really does make the scheme case-insensitive. Atlassian
    implements neither, and sharing the permissive parser would authenticate five spellings the
    real site serves anonymously — so a client sending ``Authorization: bearer <t>`` would pass
    every test here and read nothing in production. Slack is strict in its own way (see
    :func:`auth.slack_bearer_token`); it accepts the double space Atlassian refuses, which is why
    that parser is not shared either.
    """
    assert auth.atlassian_bearer_token(_request(header)) == token


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
    caller = auth.resolve_api_key(_request(f"Bearer {sample_settings.admin_token}", app=_app(acl)))
    assert caller is not None
    assert caller.is_admin is True


def test_resolve_api_key_rejects_an_unknown_key(acl):
    assert auth.resolve_api_key(_request("lin_api_nope", app=_app(acl))) is None


# --- resolve_basic: the pair, not either half -------------------------------------
# Measured against a real Atlassian Cloud site (brekkylab.atlassian.net,
# GET /rest/api/3/myself) with a user API token:
#
#   email + real token      -> 200, authenticated as that account
#   email + empty password  -> 401
#   email + wrong password  -> 401
#   unknown email + token   -> 401
#
# So neither half authenticates on its own: the password must resolve AND the username must be
# that account's own address. The one case with no vendor analogue is Backlot's admin/service
# token, which resolves to `Caller(email=None)` — there is no address to match it against, so it
# is accepted under any username.


def _basic(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def test_resolve_basic_accepts_the_token_with_its_own_email(acl, tokens):
    caller = auth.resolve_basic(
        _request(_basic("ava@acme.com", tokens["ava@acme.com"]), app=_app(acl))
    )
    assert caller is not None and caller.email == "ava@acme.com"


@pytest.mark.parametrize(
    "user, pw_key, needle",
    [
        ("ava@acme.com", None, "an empty password"),
        ("ava@acme.com", "wrong", "a password that resolves to nobody"),
        ("nobody@example.invalid", "ava@acme.com", "a username that is not the token's account"),
    ],
    ids=["empty-password", "wrong-password", "mismatched-username"],
)
def test_resolve_basic_refuses_a_half_credential(acl, tokens, user, pw_key, needle):
    pw = "" if pw_key is None else ("not-a-token" if pw_key == "wrong" else tokens[pw_key])
    assert auth.resolve_basic(_request(_basic(user, pw), app=_app(acl))) is None, needle


def test_resolve_basic_matches_the_email_case_insensitively(acl, tokens):
    """Atlassian treats an address case-insensitively, so a username copied in the shape a page
    displays it still pairs with its own token."""
    caller = auth.resolve_basic(
        _request(_basic("AVA@ACME.COM", tokens["ava@acme.com"]), app=_app(acl))
    )
    assert caller is not None and caller.email == "ava@acme.com"


def test_resolve_basic_takes_the_admin_token_under_any_username(acl, sample_settings):
    """The service token is Backlot's own identity and resolves to no address, so there is nothing
    to match a username against — the placeholder every Atlassian client has to send goes through."""
    caller = auth.resolve_basic(
        _request(_basic("svc@example.com", sample_settings.admin_token), app=_app(acl))
    )
    assert caller is not None and caller.is_admin is True
