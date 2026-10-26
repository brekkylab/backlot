"""Unit tests for the shared credential resolvers in :mod:`backlot.auth`.

The bearer/basic resolvers are covered end-to-end by the per-source endpoint tests; this
file covers the ones with a contract worth pinning on their own: the API-key scheme, whose whole
point is that it must accept a header with *no* auth scheme on it, and Atlassian's Basic, which
authenticates the email and the token as a pair.
"""

from __future__ import annotations

import base64
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backlot import auth


def _request(authorization: str | None = None, app=None) -> Request:
    # one byte per character, as a server hands on a header's bytes up to 0xFF
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("latin-1")))
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
    :func:`auth.slack_bearer_token`), which is why that parser is not shared either.
    """
    assert auth.atlassian_bearer_token(_request(header)) == token


@pytest.mark.parametrize(
    "header, token",
    [
        ("Bearer usr-abc123", "usr-abc123"),
        (" Bearer usr-abc123", "usr-abc123"),
        ("Bearer usr-abc123 ", "usr-abc123"),
        ("\tBearer usr-abc123", "usr-abc123"),
        ("Bearer usr-abc123\t", "usr-abc123"),
        ("Bearer  usr-abc123", " usr-abc123"),
        ("Bearer   usr-abc123", "  usr-abc123"),
        ("Bearer \tusr-abc123", "\tusr-abc123"),
        ("Bearer \xa0usr-abc123", "\xa0usr-abc123"),
        ("Bearer usr-abc123\xa0", "usr-abc123\xa0"),
        ("Bearer usr-abc123\x85", "usr-abc123\x85"),
        ("Bearer usr-abc123 x", "usr-abc123 x"),
        ("bearer usr-abc123", None),
        ("BEARER usr-abc123", None),
        ("token usr-abc123", None),
        ("Bearer\tusr-abc123", None),
        ("Bearerusr-abc123", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("Bearer  ", None),
        (None, None),
    ],
)
def test_slack_bearer_token_reads_one_header_as_real_does(header, token):
    """The token :func:`auth.slack_bearer_token` reads from one `Authorization` header, whose rows
    its docstring records; two headers are `test_slack_reads_the_authorization_headers_as_real_does`
    in tests/test_slack.py."""
    assert auth.slack_bearer_token(_request(header)) == token


# --- github_credential: which headers api.github.com reads as a credential -------

_A36 = "A" * 36


@pytest.mark.parametrize(
    "header, token",
    [
        # read as a credential, refused when it does not resolve
        ("Bearer nope", "nope"),
        ("bearer nope", "nope"),
        ("BEARER nope", "nope"),
        ("token nope", "nope"),
        ("Token nope", "nope"),
        ("Bearer  nope", "nope"),
        ("Bearer\tnope", "nope"),
        ("Bearer nope extra", "nope extra"),
        ("Bearer", ""),
        ("bearer", ""),
        ("token", ""),
        ("ToKeN", ""),
        ("Bearer,", ","),
        ("Bearer ,", ","),
        ("Basic", ""),
        ("BaSiC", ""),
        ("Basic,", ""),
        ("Basic a", ""),
        ("Basic abcde", ""),
        ("Basic ====", ""),
        ("Basic Zm9vOmJhcg", ""),
        ("Basic -_-_", ""),
        ("Basic a=bc", ""),
        ("Basic abcd=", ""),
        ("Basic !!!!", ""),
        ("Basic ab\tcd", ""),
        ("Basic abcd ab", ""),
        ("Basic {t}", ""),
        ("Basic\t,!!!!", ""),
        ("Basic\t ,!!!!", ""),
        ("Basic\t!!,!!", ""),
        ("Basic\t!!!!,", ""),
        # a no-break space, NEL or 0x1F beside the token is part of it
        ("Bearer \xa0", "\xa0"),
        ("Bearer \x85", "\x85"),
        ("Bearer {t}\xa0", "{t}\xa0"),
        ("Bearer \xa0{t}", "\xa0{t}"),
        ("Bearer {t}\x85", "{t}\x85"),
        ("Bearer {t}\x1f", "{t}\x1f"),
        ("Basic \xa0b64(x:{t})", ""),
        ("Basic b64(x:{t})\xa0", ""),
        # a token inside a `Basic` value: its password, or its user beside no password or
        # `x-oauth-basic`, after a space or a tab
        ("Basic b64(x:{t})", "{t}"),
        ("Basic b64(nope:{t})", "{t}"),
        ("Basic b64({t}:x-oauth-basic)", "{t}"),
        ("Basic b64({t}:)", "{t}"),
        ("Basic b64({t})", "{t}"),
        ("Basic\tb64(x:{t})", "{t}"),
        ("Basic\t b64(x:{t})", "{t}"),
        ("Basic\tb64({t}:x-oauth-basic)", "{t}"),
        # a token that does not resolve, shaped like one of real's or like a Backlot one
        (f"Basic b64(x:ghp_{_A36})", f"ghp_{_A36}"),
        (f"Basic b64(x:gho_{_A36})", f"gho_{_A36}"),
        (f"Basic b64(x:ghu_{_A36})", f"ghu_{_A36}"),
        (f"Basic b64(x:ghs_{_A36})", f"ghs_{_A36}"),
        (f"Basic b64(x:ghp_{'a' * 36})", f"ghp_{'a' * 36}"),
        (f"Basic b64(x:ghp_{'0' * 36})", f"ghp_{'0' * 36}"),
        (f"Basic b64(gho_{_A36}:)", f"gho_{_A36}"),
        (f"Basic b64(gho_{_A36}:x-oauth-basic)", f"gho_{_A36}"),
        (f"Basic b64(ghp_{_A36})", f"ghp_{_A36}"),
        (f"Basic b64(x:usr-{'0' * 20})", f"usr-{'0' * 20}"),
        # served as no credential at all
        ("Basic abcd", None),
        ("Basic Zm9vOmJhcg==", None),
        ("Basic +/+/", None),
        ("Basic ab==", None),
        ("Basic abc=", None),
        ("Basic ab cd", None),
        ("Basic a b c d", None),
        ("basic abcd", None),
        ("Basic\tZm9v", None),
        ("Basic b64({t}:x)", None),
        ("Basic b64({t}:nope)", None),
        ("Basic b64({t}:X-OAUTH-BASIC)", None),
        ("Basic b64({t}:x-oauth-basic:)", None),
        ("Basic b64({t}: )", None),
        ("Basic\tb64({t}:x)", None),
        (f"Basic b64(x:ghr_{_A36})", None),
        ("Basic b64(x:ghp_)", None),
        (f"Basic b64(x:ghp_{'A' * 35})", None),
        (f"Basic b64(x:ghp_{'A' * 37})", None),
        (f"Basic b64(x:ghp_{'A' * 35}_)", None),
        (f"Basic b64(x:github_pat_{'A' * 82})", None),
        (f"Basic b64(ghp_{_A36}:x)", None),
        (f"Basic b64(x:usr-{'0' * 19}g)", None),
        # no base64 after a tab, and no comma anywhere
        ("Basic\t!!!!", None),
        ("Basic\ta", None),
        ("Basic\tabcd=", None),
        ("Basic\t\t!!!!", None),
        ("BASIC\t!!!!", None),
        # a no-break space or NEL is no separator
        ("Bearer\xa0nope", None),
        ("Bearer\x85nope", None),
        ("Bearer\xa0", None),
        ("Bearer\x85", None),
        ("\xa0Bearer {t}", None),
        ("Basic\xa0b64(x:{t})", None),
        ("nope", None),
        ("Digest nope", None),
        ("OAuth", None),
        ("Bearerx", None),
        ("bearer:", None),
        ("Bearer=", None),
        ("", None),
        (None, None),
    ],
)
def test_github_credential_reads_the_header_as_real_does(acl, tokens, header, token):
    """The rows of :func:`auth.github_credential`'s table, `{t}` a token that resolves and
    `b64(…)` the base64 of what it holds: real served each `{t}` row as that token's caller,
    refused each other row with a token but the `usr-` one, a Backlot token's shape, and served
    each `None` row as anonymous. The token is what the router resolves, so one that is empty or
    holds a comma never names a caller."""
    t = tokens["ava@acme.com"]
    if header is not None:
        header = re.sub(
            r"b64\(([^)]*)\)",
            lambda m: base64.b64encode(m[1].encode()).decode(),
            header.replace("{t}", t),
        )
    expected = token.replace("{t}", t) if token else token
    assert auth.github_credential(_request(header, app=_app(acl))) == expected


# --- api_key_token, linear_bearer_token, fireflies_bearer_token -----------------------
# What each reader takes from one `Authorization` value; the statuses Linear and Fireflies answer
# them with are in tests/test_linear.py and tests/test_fireflies.py.


@pytest.mark.parametrize(
    "header, key, linear_bearer, fireflies",
    [
        ("lin_api_deadbeef", "lin_api_deadbeef", None, None),
        (" \tlin_api_deadbeef\t ", "lin_api_deadbeef", None, None),
        ("lin_api_deadbeef\xa0", "lin_api_deadbeef\xa0", None, None),
        ("Basic abc123", "Basic abc123", None, None),
        ("Bearer lin_oauth_1234", "Bearer lin_oauth_1234", "lin_oauth_1234", "lin_oauth_1234"),
        ("bearer lin_oauth_1234", "bearer lin_oauth_1234", "lin_oauth_1234", "lin_oauth_1234"),
        ("BEARER lin_oauth_1234", "BEARER lin_oauth_1234", None, "lin_oauth_1234"),
        ("Bearer  lin_oauth_1234", "Bearer  lin_oauth_1234", " lin_oauth_1234", "lin_oauth_1234"),
        ("Bearer \xa0x\xa0", "Bearer \xa0x\xa0", "\xa0x\xa0", "x"),
        ("Bearer x\x85", "Bearer x\x85", "x\x85", "x\x85"),
        ("Bearer\tx", "Bearer\tx", None, None),
        ("Bearer", "Bearer", None, None),
        ("Bearer \xa0", "Bearer \xa0", "\xa0", ""),
        ("   ", None, None, ""),
        (None, None, None, ""),
    ],
)
def test_each_reader_takes_from_authorization(header, key, linear_bearer, fireflies):
    request = _request(header)
    assert auth.api_key_token(request) == key
    assert auth.linear_bearer_token(request) == linear_bearer
    assert auth.fireflies_bearer_token(request) == fireflies


# --- resolve_api_key ------------------------------------------------------------


def test_resolve_api_key_resolves_a_bare_user_token(acl, tokens):
    caller = auth.resolve_api_key(_request(tokens["ava@acme.com"], app=_app(acl)))
    assert caller is not None
    assert caller.email == "ava@acme.com"
    assert caller.is_admin is False


def test_resolve_api_key_does_not_read_a_bearer_admin_token(acl, sample_settings):
    assert (
        auth.resolve_api_key(_request(f"Bearer {sample_settings.admin_token}", app=_app(acl)))
        is None
    )


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
