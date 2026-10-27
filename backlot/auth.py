"""Auth helpers shared by the vendor routers.

Each vendor carries credentials differently (Slack bearer/form token, Google/GitHub
bearer, Atlassian Basic email:api_token, Linear a scheme-less API key, Microsoft Graph `Bearer`
in any case or no scheme at all). These helpers extract the raw token, resolve it to a :class:`~backlot.acl.Caller` via the app's ACL, and
compute the caller's visible principal set. Error *shaping* (Slack's ``ok:false`` vs a
real 401) stays in the routers.
"""

from __future__ import annotations

import base64
import hmac
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException, Request

from backlot import sigv2, sigv4, sigv4a
from backlot.acl import ANONYMOUS, Acl, Caller


def conn(request: Request) -> sqlite3.Connection:
    return request.app.state.conn


def acl(request: Request) -> Acl:
    return request.app.state.acl


def _authorization(request: Request) -> str | None:
    return request.headers.get("authorization")


def bearer_token(request: Request) -> str | None:
    """Parse ``Authorization: Bearer <t>`` or GitHub's legacy ``token <t>``."""
    hdr = _authorization(request)
    if not hdr:
        return None
    parts = hdr.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
        return parts[1].strip()
    return None


# An `Authorization` value as api.github.com splits it: the scheme, then everything after it.
_GITHUB_SCHEME = re.compile(r"([^ \t,]*)(.*)", re.DOTALL)
# A `Basic` value api.github.com reads as base64: four-character groups, the last of which may be
# two characters and `==` or three and `=`. Written out rather than left to `base64.b64decode`,
# whose `validate=True` reads `abcd=` as base64 on Python 3.11 and refuses it from 3.12 on.
_GITHUB_BASE64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")

# A token `github_credential` reads out of a `Basic` value and hands on even when it resolves to no
# one, so that it is "Bad credentials": real's own shapes, `ghp_`, `gho_`, `ghu_` or `ghs_` and
# exactly 36 ASCII letters or digits, and Backlot's, `usr-` and the 20 hexadecimal digits the
# importers give each user, which stands for a real token here though real serves it as no
# credential.
_GITHUB_TOKEN_SHAPE = re.compile(r"gh[pous]_[A-Za-z0-9]{36}|usr-[0-9a-f]{20}")
# Two more shapes real refuses out of a `Basic` value, but counted, in the window a `Basic` value
# naming no token is counted in: 40 lower-case hexadecimal digits, and one of the four prefixes of
# `_GITHUB_TOKEN_SHAPE` in any other case, then 36 ASCII letters or digits (2026-10-25).
_GITHUB_COUNTED_TOKEN_SHAPE = re.compile(
    r"[0-9a-f]{40}|(?!gh[pous]_)[gG][hH][pPoOuUsS]_[A-Za-z0-9]{36}"
)


def github_credential(request: Request) -> str | None:
    """The credential a GitHub request carries, read the way api.github.com reads ``Authorization``:
    ``None`` for a request it serves as anonymous, and otherwise the string to resolve as a token:
    what follows `Bearer` or `token`, so ``","`` for ``Bearer,``; the token a `Basic` value names;
    or ``""`` for a `Basic` value refused with no token to look up, and for a request carrying more
    than one `Authorization`.

    Measured on `GET /rate_limit` on 2026-10-14 and 2026-10-16, each value on a URL of its own,
    where an anonymous caller is served 200 and a credential that does not resolve is the 401 "Bad
    credentials"::

        Bearer or token, in any case, alone or followed by a space,      401
          a tab or a comma and then anything
        Basic, in any case, alone, followed by a comma, or followed       401
          by a value that is not base64 once its spaces are dropped:
          a, abcde, ====, Zm9vOmJhcg, -_-_, a=bc, abcd=, !!!!, ab<TAB>cd
        Basic, a tab, and a value that is not base64, in a header         200
          with no comma: <TAB>!!!!, <TAB>a, <TAB>abcd=, <TAB><TAB>!!!!
        Basic, a tab, and a comma anywhere after it: <TAB>,!!!!,          401
          <TAB> ,!!!!, <TAB>!!,!!, <TAB>!!!!,
        Basic followed by base64 that names no token: abcd,               200
          Zm9vOmJhcg==, +/+/, ab==, abc=, ab cd, a b c d, <TAB>Zm9v
        any other first word, or none: nope, Digest, OAuth, Bear,         200
          Bearerx, bearer:, Bearer=
        a no-break space or NEL in place of a space: Bearer<NBSP>nope,    200
          Bearer<NEL>, <NBSP>Bearer <t>, Basic<NBSP> and base64

    And with a token that resolves: ``Bearer <t>``, ``bearer <t>``, ``token <t>``, a second space
    or a tab before it are its 200, and so is ``Basic``, then a space or a tab, then the base64 of
    ``x:<t>``, ``nope:<t>``, ``<t>:x-oauth-basic``, ``<t>:`` or ``<t>``; ``Bearer,<t>``,
    ``Bearer ,<t>``, ``Bearer <t> x``, ``Bearer <t>,`` and ``Basic <t>`` are the 401; ``<t>`` alone
    is served as anonymous, and so is ``Basic`` with the base64 of ``<t>:x``, ``<t>:nope``,
    ``<t>:X-OAUTH-BASIC``, ``<t>:x-oauth-basic:`` or ``<t>: ``.

    A `Basic` value naming a token that does not resolve is the 401, with none of the five
    `x-ratelimit-*`, when the token is shaped like one of real's: the base64 of ``x:ghp_<36 A>``,
    of ``x:gho_``, ``x:ghu_`` and ``x:ghs_`` the same, of ``x:ghp_<36 a>``, ``x:ghp_<36 0>``,
    ``gho_<36 A>:``, ``gho_<36 A>:x-oauth-basic`` and ``ghp_<36 A>``. The base64 of
    ``x:gh<c>_<36 A>`` for each other lower-case letter ``<c>``, of ``x:ghp_<35 A>``,
    ``x:ghp_<37 A>``, ``x:ghp_<35 A>_``, ``x:github_pat_<82 A>`` and ``ghp_<36 A>:x`` was the 200
    (2026-10-24 and 2026-10-25).

    Two more shapes are the 401 out of a `Basic` value, but counted, with the five of the window a
    `Basic` value naming no token is counted in (:data:`_GITHUB_COUNTED_TOKEN_SHAPE`): the base64 of
    ``x:<40 a>``, ``x:<40 0>``, ``x:<40 f>``, ``x:0123456789abcdef0123456789abcdef01234567``,
    ``<40 a>``, ``<40 a>:`` and ``<40 a>:x-oauth-basic``, and of ``x:GHP_<36 A>``, ``x:Ghp_<36 A>``,
    ``x:gHp_<36 A>``, ``x:ghP_<36 A>``, ``x:GHO_<36 A>``, ``x:GHS_<36 A>``, ``x:GHU_<36 A>`` and
    ``x:GHP_<36 a>``. ``x:<40 g>``, ``x:<40 A>``, ``x:<40 F>``, ``x:<40 ->``, ``x:<40 _>``, forty of
    ``aB``, ``x:<39 a>``, ``x:<41 a>``, ``x:GHA_<36 A>``, ``x:GHR_<36 A>``, ``x:GHP_<35 A>``,
    ``x:GHP_<37 A>``, ``x:GHP_<35 A>_`` and ``<40 a>:x`` were the 200, and so were
    ``x:gh<U+017F>_<36 A>`` and ``x:GH<U+017F>_<36 A>``, a long s for the `s`, and
    ``x:GHP_<35 A><U+212A>`` and ``x:<39 a><U+212A>``, each ending in the Kelvin sign (2026-10-25
    and 2026-10-26).

    So real splits the scheme off at the first space, tab or comma, reads the password of a `Basic`
    value that decodes, or its user when there is no colon or the password is empty or
    `x-oauth-basic`, and serves one whose token does not resolve as no credential at all, as it does
    an unknown scheme, unless that token is shaped like one of real's (:data:`_GITHUB_TOKEN_SHAPE`,
    which takes Backlot's too, or :data:`_GITHUB_COUNTED_TOKEN_SHAPE`). A `Basic` value that does
    not decode is no credential either where :func:`github_scheme_then_a_tab` holds. A space and a
    tab are the only whitespace it trims: ``Bearer <NBSP>``, ``Bearer <t><NBSP>``,
    ``Bearer <NBSP><t>``, ``Bearer <t><NEL>``, ``Bearer <t><0x1F>``, ``Basic <NBSP>`` and base64,
    and the base64 of ``x:<t>`` followed by a no-break space were each the 401 (2026-10-16).

    More than one `Authorization` header is the 401 whatever each holds, with none of the five
    `x-ratelimit-*`: on `/rate_limit` and `/search/issues`, ``Bearer <t>`` with a second copy, and
    with ``Bearer nope``, ``Digest x`` or an empty value in either order; on `/rate_limit`,
    ``token <t>`` with ``Bearer <t>``, ``Digest x`` with ``Digest y`` and three copies of ``Bearer
    <t>``; on eight operations Backlot does not serve (`GET /zen`, `PATCH /user`, ...), two copies
    of ``Bearer <t>``, ``Digest x`` with ``Digest y`` and ``Bearer<TAB>nope`` with ``Bearer <t>``.
    Two copies of ``Bearer <t>`` answered no version echo on each of those routes and on `/user`,
    `/user/repos` and `/repos/psf/requests`, and five of them moved neither the token's window nor
    the address's (2026-10-17).
    """
    if repeated_authorization(request):
        return ""
    scheme, rest = github_scheme(request)
    if scheme in ("bearer", "token"):
        return rest.strip(" \t")
    if scheme == "basic":
        value = rest.lstrip(" \t").replace(" ", "")
        if not value or not _GITHUB_BASE64.fullmatch(value):
            return None if github_scheme_then_a_tab(request) else ""
        user, _, password = base64.b64decode(value).decode("utf-8", "replace").partition(":")
        token = user if password in ("", "x-oauth-basic") else password
        if (
            acl(request).resolve(token) is not None
            or _GITHUB_TOKEN_SHAPE.fullmatch(token)
            or _GITHUB_COUNTED_TOKEN_SHAPE.fullmatch(token)
        ):
            return token
        return None
    return None


def github_basic_token_counted(request: Request) -> bool:
    """Whether the request's `Authorization` is `Basic` naming a token of
    :data:`_GITHUB_COUNTED_TOKEN_SHAPE` that resolves to no one: the "Bad credentials" 401 real
    counts in the window a `Basic` value naming no token is counted in, carrying its five
    `x-ratelimit-*`. A `Bearer` with the same token is the 401 with none of the five."""
    if repeated_authorization(request) or github_scheme(request)[0] != "basic":
        return False
    token = github_credential(request)
    return (
        bool(token)
        and _GITHUB_COUNTED_TOKEN_SHAPE.fullmatch(token) is not None
        and acl(request).resolve(token) is None
    )


def github_scheme(request: Request) -> tuple[str, str]:
    """The `Authorization` value split by :data:`_GITHUB_SCHEME`, the scheme in lower case."""
    scheme, rest = _GITHUB_SCHEME.fullmatch((_authorization(request) or "").strip(" \t")).groups()
    return scheme.lower(), rest


def github_scheme_then_a_tab(request: Request) -> bool:
    """Whether a tab follows the scheme :func:`github_scheme` splits off, in the one `Authorization`
    header a request carries, holding no comma: the spelling real reads apart from the rest, whose
    rows are in :func:`github_credential`'s table."""
    header = _authorization(request) or ""
    return (
        github_scheme(request)[1].startswith("\t")
        and "," not in header
        and not repeated_authorization(request)
    )


def repeated_authorization(request: Request) -> bool:
    """Whether the request carries more than one `Authorization` header."""
    return len(request.headers.getlist("authorization")) > 1


#: The conditional headers Cloudflare, in front of Notion, HubSpot, Linear and Fireflies,
#: refuses with its 400 page when one of them comes more than once, as it does `Authorization`.
_CLOUDFLARE_REFUSED_HEADERS = (
    "if-none-match",
    "if-match",
    "if-modified-since",
    "if-unmodified-since",
    "if-range",
)


def repeated_cloudflare_refused_header(request: Request) -> bool:
    """Whether the request carries more than once `Authorization` or one of the conditional
    headers Cloudflare refuses (:data:`_CLOUDFLARE_REFUSED_HEADERS`): api.notion.com,
    api.linear.app, api.fireflies.ai and api.hubapi.com answered `Authorization` and each of the
    five, sent twice with the same value, with the 155-byte `400 Bad Request` page on 2026-10-27
    (UTC), where one copy, and `Cache-Control` or `Range` twice, were served."""
    return repeated_authorization(request) or any(
        len(request.headers.getlist(name)) > 1 for name in _CLOUDFLARE_REFUSED_HEADERS
    )


def api_key_token(request: Request) -> str | None:
    """``Authorization`` as Linear reads a personal API key: the whole value, with the spaces and
    tabs around it left off, so a scheme in front is part of the key and fails to resolve.
    api.linear.app answered each latin-1 byte but NUL, LF and CR, put before and after a key that
    resolves, on 2026-10-18 (UTC): a space or a tab served the query, U+00A0 and every other byte
    was the 401, and a control character other than the tab drew an empty 400. A Backlot token
    travels this way; :func:`linear_bearer_token` reads the other spelling."""
    return (_authorization(request) or "").strip(" \t") or None


def linear_bearer_token(request: Request) -> str | None:
    """What follows ``Bearer `` or ``bearer `` in ``Authorization``, where Linear reads an OAuth
    access token; None for any other value.

    With an API key, Linear's answers on 2026-10-18 (UTC) were the 400 for those two spellings and
    the 401 for the 62 other casings of the six letters, as for a tab after the scheme, two
    spaces, and U+00A0 or another latin-1 byte other than a control character between the space and
    the key. A space or a tab after the key is left off and the 400 stays."""
    hdr = (_authorization(request) or "").strip(" \t")
    for scheme in ("Bearer ", "bearer "):
        if hdr.startswith(scheme):
            return hdr[len(scheme) :]
    return None


#: What Linear takes for a personal API key under ``Bearer``: ``lin_api_`` and forty ASCII letters
#: and digits (:func:`linear_api_key_under_bearer`).
_LINEAR_API_KEY = re.compile(r"lin_api_[0-9A-Za-z]{40}")


def linear_api_key_under_bearer(request: Request) -> bool:
    """Whether :func:`linear_bearer_token` holds an API key, which Linear refuses with a 400 before
    it reads the body: a token in :data:`_LINEAR_API_KEY`'s shape, whether or not Linear issued it,
    or one Backlot resolves. A Backlot token is served when sent bare, the spelling of a Linear
    personal API key, so it stands for that user's ``lin_api_`` key.

    On 2026-10-19 (UTC) api.linear.app gave the 400 to ``lin_api_`` and forty ASCII letters or
    digits, whether or not it issued the key, and the 401 to each other length tried (none to
    forty-five, fifty, sixty, sixty-four and a hundred), to any other printable ASCII character put
    first or last of the forty or ``é`` put last, and to ``LIN_API_``, ``lin_oauth_`` and
    ``xlin_api_``."""
    token = linear_bearer_token(request)
    if token is None:
        return False
    return _LINEAR_API_KEY.fullmatch(token) is not None or acl(request).resolve(token) is not None


def fireflies_bearer_token(request: Request) -> str | None:
    """The token Fireflies reads from the Authorization header, or None when it has no bearer
    prefix: the scheme in any case and a space, then the rest with spaces, tabs and U+00A0 left off
    either end. No header, an empty one and the prefix followed by U+00A0 alone give the empty
    token, which resolves to no one.

    Sent to api.fireflies.ai on 2026-10-18 (UTC) after the scheme, after its space and after the
    key, every latin-1 byte other than NUL, LF and CR showed that only a space opens the token, a
    tab or a comma there leaving no prefix; a tab, a space or U+00A0 around the key served the
    query, and U+0085 or another byte there was a key that does not resolve. ``BEARER`` and
    ``bEaReR`` were served, and a U+00A0 before the scheme was no prefix."""
    hdr = (_authorization(request) or "").strip(" \t")
    if not hdr:
        return ""
    if not hdr.lower().startswith("bearer "):
        return None
    return hdr[len("bearer ") :].strip(" \t\xa0")


# How much of a Basic credential a request carries.
BASIC_ABSENT = "absent"  # no header, another scheme, or `Basic` with nothing to decode
BASIC_UNPARSEABLE = "unparseable"  # a value that is not one user and one password
BASIC_PAIR = "pair"  # exactly one non-empty user and one non-empty password


def _basic_value(request: Request) -> str | None:
    """The raw base64 payload of an ``Authorization: Basic`` header, or None for any other."""
    hdr = _authorization(request)
    if not hdr:
        return None
    parts = hdr.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "basic":
        return parts[1]
    return None


def _decoded_basic(request: Request) -> str | None:
    """The decoded ``user:pass`` of a Basic header, ``None`` when there is none to decode and
    ``""`` for a payload that is not base64 — a value that was there and could not be read."""
    value = _basic_value(request)
    if not value:
        return None
    try:
        return base64.b64decode(value).decode("utf-8", "replace")
    except (ValueError, UnicodeDecodeError):
        return ""


def basic_password(request: Request) -> tuple[str | None, str | None]:
    """Parse ``Authorization: Basic base64(user:pass)`` -> (user, pass)."""
    decoded = _decoded_basic(request)
    if not decoded:
        return None, None
    user, _, pw = decoded.partition(":")
    return user, pw


def basic_credential_kind(request: Request) -> str:
    """Which of :data:`BASIC_ABSENT` / :data:`BASIC_UNPARSEABLE` / :data:`BASIC_PAIR` the request
    carries.

    Confluence answers the three differently — a pair it read and rejected is its 403, a value it
    could not read is a 401, and no credential at all is the 403 again — so which one a request
    holds decides which refusal it draws. Measured against ecosystem.atlassian.net and
    brekkylab.atlassian.net on 2026-09-04, which is where the split is observable at all: a single
    colon with both halves non-empty is the pair; an empty user, an empty password, no colon, a
    second colon, or a payload that is not base64 is unparseable; and a missing header, an unknown
    scheme and a `Basic` with nothing after it are no credential.
    """
    if not _basic_value(request):
        return BASIC_ABSENT
    decoded = _decoded_basic(request)
    if not decoded:  # a payload that is there and does not decode
        return BASIC_UNPARSEABLE
    user, _, pw = decoded.partition(":")
    if user and pw and ":" not in pw:
        return BASIC_PAIR
    return BASIC_UNPARSEABLE


def basic_names_a_user(request: Request) -> bool:
    """Whether the request presented a Basic credential naming somebody.

    Jira reports one it could not resolve in ``X-Seraph-LoginReason``, and the header is keyed on
    the username alone: a value carrying a non-empty user before its first colon draws it whatever
    follows, and one with an empty user, or with no colon at all, draws nothing. Measured on both
    sites on 2026-09-04.
    """
    decoded = _decoded_basic(request) or ""
    user, colon, _ = decoded.partition(":")
    return bool(user and colon)


def slack_bearer_token(request: Request) -> str | None:
    """Parse the ``Authorization`` header the way Slack does, which is stricter than
    :func:`bearer_token`: the scheme must be exactly ``Bearer``, followed by one space and the
    token, which keeps whatever follows that space. Spaces and tabs around a header's value are no
    part of it, and two headers are read as one, their values joined by a comma once the blank ones
    are left out, so `Bearer <t>` sent twice is the token `<t>, Bearer <t>`.

    Measured against slack.com (a bogus token is enough — the presented/absent split needs no
    account). ``invalid_auth`` means the header counted as a credential, ``not_authed`` means it
    did not::

        Bearer <t>      invalid_auth      bearer <t>      not_authed
                                          BEARER <t>      not_authed
                                          token <t>       not_authed
                                          Bearer<TAB><t>  not_authed
                                          Bearer<t>       not_authed

    With a user token that resolves, real's `auth.test` answered each row of
    ``tests/test_slack.py::_SLACK_AUTHORIZATION_ROWS`` as that table gives it, the headers sent
    over a raw socket on 2026-10-26 (one header first on 2026-10-20). U+00A0 and U+0085 after the
    token were ``invalid_auth`` there, where ``str.strip()`` takes them off.

    A tab is not a space to Slack, so the generic whitespace split in :func:`bearer_token` is
    wrong here, and so is its case-insensitive scheme match. That function stays permissive because
    GitHub really does accept ``token <t>`` and RFC 7235 really does make the scheme
    case-insensitive; Slack implements neither. Of the five spellings above that Slack refuses,
    sharing it would authenticate four — every one but ``Bearer<t>``, which it does not take
    either — so a client sending ``Authorization: token <xoxb>`` would pass every test here and
    reach nothing in production.
    """
    values = (value.strip(" \t") for value in request.headers.getlist("authorization"))
    hdr = ", ".join(value for value in values if value)
    if not hdr.startswith("Bearer "):
        return None
    return hdr[len("Bearer ") :] or None


def msgraph_token(request: Request) -> tuple[str, str | None]:
    """Parse the ``Authorization`` header the way Microsoft Graph does.

    Graph reads the token after ``Bearer`` in any case, or with no scheme at all, with any run of
    spaces or tabs between. Anything else — another scheme (``token``, ``Basic``), or a word after
    the token — is a token it cannot read, however valid the token itself is (measured with a
    delegated token on graph.microsoft.com, see :mod:`backlot.errors.msgraph`).

    Returns ``(state, token)`` where state is one of:

        "absent"     no Authorization header       -> "Access token is empty."
        "empty"      `Bearer` with nothing after   -> "UnableToParseTokens"
        "unreadable" a spelling Graph cannot read  -> the malformed-token message
        "present"    a token to resolve            -> the malformed-token message if unknown

    Deliberately NOT :func:`bearer_token`: that one returns None for a ``Basic`` header, which
    would report a missing credential where Graph reports a malformed one.
    """
    hdr = _authorization(request)
    if hdr is None:
        return "absent", None
    parts = hdr.split()
    if not parts or (len(parts) == 1 and parts[0].lower() == "bearer"):
        return "empty", None
    if len(parts) == 1:
        return "present", parts[0]
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return "present", parts[1]
    return "unreadable", None


def slack_token(request: Request) -> str | None:
    """The token real Slack reads from a request: the ``token`` form field, then the
    ``Authorization: Bearer`` header (:func:`slack_bearer_token`).

    The form field is where the official slack-go SDK and Slack's own clients put the token; the
    urlencoded or multipart body is read by ``routers.slack.read_post_body``, keeping a field's last
    value. Real serves a bogus header beside a valid form token as the form token, and answers
    ``invalid_auth`` to a valid header beside a bogus one. The last value is the one read
    (``token=<valid>&token=<bogus>`` is ``invalid_auth``), an empty one is no field, and a space
    is a token that resolves to nobody. A ``TOKEN`` form field is no field either. The query
    string is not read here; :func:`slack_query_token_refused` answers for it, and its docstring
    dates the measurement these answers come from."""
    form = getattr(request.state, "_form", None)
    form_field = form.get("token") if form else None
    return form_field or slack_bearer_token(request)


def slack_query_token_refused(request: Request) -> bool:
    """Whether real Slack answers ``invalid_auth`` for the request's ``token`` query parameter.

    Slack's changelog entry "No more tokens in querystrings for newly created apps"
    (docs.slack.dev, November 1, 2020) closes that channel to apps created after February 24,
    2021; Backlot answers as such an app. The user token of an app created on 2026-09-17 drew
    these answers from slack.com/api on 2026-10-11 and 2026-10-13, on each method
    ``routers.slack`` authenticates. The parameter's last value is the one read. A non-empty
    value is refused whatever else the request carries, valid or bogus, unless a ``token`` form
    field takes its place (below); a space or a tab is not empty. An empty one, or ``?token`` with
    no ``=``, is refused beside a token :func:`slack_token` reads, and changes nothing otherwise:
    alone it is ``not_authed``, and so it is beside a header :func:`slack_bearer_token` does not
    read (``bearer <t>``, ``Basic <c>``). A ``token`` form field, an empty one included, takes the
    parameter's place, which is then refused as an empty one is: real answered ``?token=<valid>``
    beside an empty field, urlencoded or a multipart part, with ``not_authed``, and beside it and a
    valid bearer header with ``invalid_auth``, on 2026-10-20. ``?TOKEN=<t>`` is not this parameter.
    """
    if "token" not in request.query_params:
        return False
    form = getattr(request.state, "_form", None)
    if request.query_params["token"] != "" and not (form and "token" in form):
        return True
    return slack_token(request) is not None


def resolve_bearer(request: Request) -> Caller | None:
    return acl(request).resolve(bearer_token(request))


def require_bearer(request: Request, detail: str) -> Caller:
    """Resolve a bearer token or raise 401 with the VENDOR's own message.

    ``detail`` is a parameter rather than something this function picks, because the message is
    part of the emulated surface: GitHub says "Bad credentials", Google "Invalid Credentials",
    Atlassian "Unauthorized", and a client that string-matches its vendor's error has to keep
    matching. Each router states its own once (see ``tests/test_endpoints.py``).
    """
    caller = resolve_bearer(request)
    if caller is None:
        raise HTTPException(status_code=401, detail=detail)
    return caller


def atlassian_bearer_token(request: Request) -> str | None:
    """Parse the ``Authorization`` header the way a ``<site>.atlassian.net`` gateway does, which
    is stricter than :func:`bearer_token` and strict differently from :func:`slack_bearer_token`.

    Measured against ecosystem.atlassian.net and brekkylab.atlassian.net on 2026-09-04 (a bogus
    token is enough — a recognised credential is refused with a 403 and an unrecognised one is
    served anonymously, so the answer says which the site read)::

        Bearer <t>      read            bearer <t>      not read
        ' Bearer <t>'   read            BEARER <t>      not read
        Bearer <t>' '   read            token <t>       not read
        Bearer a<TAB>b  read            OAuth <t>       not read
                                        Bearer  <t>     not read
                                        Bearer<TAB><t>  not read
                                        Bearer<t>       not read
                                        Bearer          not read
                                        Bearer a b      not read

    The scheme is case-sensitive and separated from the token by exactly one space, and the token
    holds no space of its own: the `Bearer a b` row above was served anonymously on Jira Cloud on
    2026-10-12, where a tab, a comma or a dot in the token was read and then refused. Any other byte
    is the token's own wherever it stands, a tab, U+0085 and U+00A0 among them, and only a space or
    a tab is trimmed from either end of the header, where Python's ``str.strip`` would take U+0085
    and U+00A0 too. Measured on Jira Cloud on 2026-10-16 with each latin-1 byte but CR, LF and NUL
    in four places: alone after `Bearer `, after `Bearer <t>`, before `Bearer <t>` and between
    `Bearer ` and `<t>`; the control characters but the tab never reach this, the server in front
    answering them with a 400 on real and here. Sharing :func:`bearer_token` would authenticate six
    of those spellings — every "not read" row above but ``OAuth <t>``, ``Bearer<t>`` and the bare
    ``Bearer`` — so a client sending ``Authorization: token <t>`` would pass every test here and
    read nothing in production. Slack reads the double space this refuses as a credential, the
    second space part of a token that then resolves to nobody.
    """
    hdr = (_authorization(request) or "").strip(" \t")
    if not hdr.startswith("Bearer "):
        return None
    rest = hdr[len("Bearer ") :]
    if " " in rest:
        return None
    return rest


def atlassian_bearer_unreadable(request: Request) -> bool:
    """Whether the request carries a bearer the site would read and Backlot cannot resolve.

    A Backlot token is an opaque string with no dots, and that is the shape the gateway reports as
    unreadable — measured with ``usr-<hex>`` itself. A token shaped like a complete signed JWS is
    read and then rejected with a 401 instead; Backlot issues none, and reproducing Atlassian
    Connect's accept boundary would mean inventing the space between the shapes measured, so a
    JWT-shaped bearer takes the 403 here too.
    """
    token = atlassian_bearer_token(request)
    return bool(token) and acl(request).resolve(token) is None


def atlassian_caller(request: Request) -> Caller:
    """The caller for an Atlassian read: Basic ``email:api_token`` or
    :func:`atlassian_bearer_token`, and :data:`backlot.acl.ANONYMOUS` when neither resolves.

    The bearer is NOT an OAuth 3LO token standing in for Atlassian's own. A 3LO token goes to
    ``api.atlassian.com/ex/jira/{cloudid}/…``, which Backlot does not serve; on the
    ``<site>.atlassian.net`` surface it does serve, a bearer is read as a Connect session JWT, and
    an opaque Backlot token is one the gateway cannot read at all — which is the ``403``
    :func:`atlassian_bearer_unreadable` reports.

    No refusal here, unlike :func:`require_bearer`: the two Atlassian APIs disagree about what an
    unresolved credential means. Jira drops the caller to anonymous and answers the request; only
    Confluence refuses. Each router decides for itself, so this reports the identity and nothing
    else.
    """
    bearer = atlassian_bearer_token(request)
    return resolve_basic(request) or acl(request).resolve(bearer) or ANONYMOUS


def resolve_api_key(request: Request) -> Caller | None:
    return acl(request).resolve(api_key_token(request))


def resolve_basic(request: Request) -> Caller | None:
    """Atlassian: the api_token is the password, and the username has to be its own account.

    Both halves, because that is what the real service requires. Measured against a real Atlassian
    Cloud site (``GET /rest/api/3/myself``) with a user API token: ``email:token`` answers 200,
    while an empty password, a wrong password, and a valid token under someone else's address all
    answer 401. Matched case-insensitively, which is also measured: the same token under
    ``AVA.CHEN@…`` and ``Ava.chen@…`` answers 200 as that same account.

    The admin/service token is the one caller with no address — ``Acl.resolve`` gives it
    ``Caller(email=None)`` — so there is nothing to match a username against and any username is
    taken. It has no vendor analogue to be faithful to: it is Backlot's own full-crawl identity,
    and it is what lets an Atlassian client send the placeholder username its config demands.
    """
    user, pw = basic_password(request)
    caller = acl(request).resolve(pw)
    if caller is None:
        return None
    if caller.email is None:
        return caller
    return caller if (user or "").casefold() == caller.email.casefold() else None


def visible_ids(request: Request, caller: Caller) -> set[str] | None:
    return acl(request).visible_ids(conn(request), caller)


@dataclass(frozen=True)
class SigV4Refusal:
    """A credential real S3 refuses: its code, the message it sends and the members after it."""

    code: str
    message: str
    members: tuple[tuple[str, str], ...] = ()


_HEADER_MALFORMED = "The authorization header is malformed; "
_QUERY_CREDENTIAL = "Error parsing the X-Amz-Credential parameter; "
_CREDENTIAL_FORMAT = (
    'the Credential is mal-formed; expecting "<YOUR-AKID>/YYYYMMDD/REGION/SERVICE/aws4_request".'
)
# SigV4a's scope names no region, and real's message for one that is not four parts names none
# either (2026-09-29).
_CREDENTIAL_FORMAT_4A = (
    'the Credential is mal-formed; expecting "<YOUR-AKID>/YYYYMMDD/SERVICE/aws4_request".'
)
_QUERY_PARAMETERS = (
    "X-Amz-Credential",
    "X-Amz-Signature",
    "X-Amz-Date",
    "X-Amz-SignedHeaders",
    "X-Amz-Expires",
)
_SERVER_TIME = "%Y-%m-%dT%H:%M:%SZ"
# The longest `X-Amz-Expires` real takes: 604800 was served and 604801 refused (2026-09-29).
_A_WEEK = 604800
# How far ahead a presign's date may be: real served one five minutes ahead and refused one an hour
# ahead (2026-09-29); the header's own skew window stands in for the boundary between the two.
_NOT_YET_VALID = 900
# A Java long's range, which an `X-Amz-Expires` and a V2 query's `Expires` are read in.
_LONG = 2**63
# The values of `x-amz-content-sha256` real takes besides 64 hex digits of either case, spelt as
# written: `unsigned-payload`, 63 and 65 digits and 64 `g`s were refused with ``_BAD_PAYLOAD_HASH``
# (2026-09-29).
_PAYLOAD_KEYWORDS = frozenset(
    {
        "UNSIGNED-PAYLOAD",
        "STREAMING-UNSIGNED-PAYLOAD-TRAILER",
        "STREAMING-AWS4-HMAC-SHA256-PAYLOAD",
        "STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER",
        "STREAMING-AWS4-ECDSA-P256-SHA256-PAYLOAD",
        "STREAMING-AWS4-ECDSA-P256-SHA256-PAYLOAD-TRAILER",
    }
)
_BAD_PAYLOAD_HASH = (
    "x-amz-content-sha256 must be UNSIGNED-PAYLOAD, STREAMING-UNSIGNED-PAYLOAD-TRAILER, "
    "STREAMING-AWS4-HMAC-SHA256-PAYLOAD, STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER, "
    "STREAMING-AWS4-ECDSA-P256-SHA256-PAYLOAD, STREAMING-AWS4-ECDSA-P256-SHA256-PAYLOAD-TRAILER or "
    "a valid sha256 value."
)
_MISSING_HEADER = "Missing required header for this request: "
_NOT_SIGNED = "There were headers present in the request which were not signed"
_INTERNAL_ERROR = "We encountered an internal error. Please try again."


def _credential_fault(
    credential: str, asymmetric: bool = False
) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    """What real names wrong with a credential scope, and the members it names it with, or ``None``.

    In real's order: the parts, five for SigV4 and four for SigV4a, whose scope names no region;
    the date's format (``_is_credential_date``); the region; the service; the terminal. The region
    is the one this server presents, and real answers any other the way it answered `us-west-2`,
    `eu-west-1`, `US-EAST-1`, `zz` and `us-east-1a` on `s3.us-east-1.amazonaws.com`, naming the
    region sent and the one expected, the latter again as `Region`, and an empty one with a message
    of its own and no member; a region beside a service, a terminal, a scope date or an access key
    that is wrong is refused for the region, and a date of the wrong format beside a wrong or empty
    region for the date (measured 2026-09-29, in the header and in the query alike)."""
    bits = credential.split("/")
    if len(bits) != (4 if asymmetric else 5):
        return (_CREDENTIAL_FORMAT_4A if asymmetric else _CREDENTIAL_FORMAT), ()
    if not _is_credential_date(bits[1]):
        message = (
            f'incorrect date format "{bits[1]}". This date in the credential must be in the format '
            '"yyyyMMdd".'
        )
        return message, ()
    if not asymmetric:
        if not bits[2]:
            return "a non-empty region must be provided in the credential.", ()
        if bits[2] != sigv4.REGION:
            message = f"the region '{bits[2]}' is wrong; expecting '{sigv4.REGION}'"
            return message, (("Region", sigv4.REGION),)
    service, terminal = bits[-2], bits[-1]
    if service != "s3":
        return f'incorrect service "{service}". This endpoint belongs to "s3".', ()
    if terminal != "aws4_request":
        return f'incorrect terminal "{terminal}". This endpoint uses "aws4_request".', ()
    return None


def _is_credential_date(text: str) -> bool:
    """Whether a scope date reads as `yyyyMMdd` does in a strict `java.text.SimpleDateFormat`: four
    digits, two, then the rest of the run, naming a day that exists, anything after the digits
    left unread. `2026092` and `20260929x` were read as dates and then compared with the request's
    as sent, and `20261329`, `20260230`, `2026090`, `20260900`, `2026929`, `202609291`, `2026`,
    `2026-09-29`, `+2026092`, `2026O929` and an empty date were refused for their format
    (2026-09-29)."""
    match = re.match(r"(\d{4})(\d{2})(\d+)", text)
    if match is None:
        return False
    try:
        date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return False
    return True


def _hex_bytes(text: str) -> str:
    return " ".join(f"{b:02x}" for b in text.encode("utf-8"))


_ONE_MECHANISM = (
    "Only one auth mechanism allowed; only the X-Amz-Algorithm query parameter, "
    "Signature query string parameter or the Authorization header should be specified"
)
_NO_SPACE = "Authorization header is invalid -- one and only one ' ' (space) required"
_NO_DATE = "AWS authentication requires a valid Date or x-amz-date header"
_NO_KEY = "The AWS Access Key Id you provided does not exist in our records."
_MISMATCH = (
    "The request signature we calculated does not match the signature you provided. "
    "Check your key and signing method."
)
# A Signature Version 2 query's `Expires` is seconds since the epoch, read in a Java long and taken
# up to an int32's largest: 2147483647 and `+2147483647` were dates and 2147483648 not, nor `1e9` or
# ` 1`, where `01790000000`, `-1`, -2147483649, -9999999999 and -9223372036854775808 were, and
# -9223372036854775809 not (2026-09-29).
_INT32 = 2**31
# The milliseconds real's V2 query multiplies `Expires` into wrap as a Java long's do, and an
# instant before 0000-01-01T00:00:00Z of the proleptic Gregorian calendar is real's 500:
# -62167219200 was an expired request and -62167219201, -70000000000 and -9223372036854775 were each
# the 500, where -9223372036854776, which wraps into the far future, was served and
# -9223372036854775807, which wraps to one second after the epoch, was expired at
# 1970-01-01T00:00:01Z (2026-09-29).
_YEAR_ZERO_MS = -62167219200000
# Real names an instant before 1582-10-15T00:00:00Z in the Julian calendar, as a
# `java.util.GregorianCalendar` does, and a year before the first as its year of the era:
# -62135596800, 0001-01-01 of the proleptic Gregorian calendar, came back as `0001-01-03T00:00:00Z`,
# -30610224000 as `0999-12-27T00:00:00Z` and -9999999999 as `1653-02-10T06:13:21Z` (2026-09-29).
_GREGORIAN_CUTOVER = -12219292800
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def signed_with_v2(request: Request) -> bool:
    """Whether the credential a request carries is a Signature Version 2 one, header or query."""
    scheme = request.headers.get("authorization", "").partition(" ")[0]
    if scheme:
        return scheme.upper() == "AWS"
    return "Signature" in request.query_params and "X-Amz-Algorithm" not in request.query_params


def signed_payload_hash(request: Request) -> str | None:
    """The `x-amz-content-sha256` a V4 or SigV4a signature covers, as sent, or ``None`` for a
    request signed another way, unsigned, or presigned without one. A presign reads it where it is
    sent: one signed over `garbage` as its payload line was served (2026-09-29)."""
    authz = request.headers.get("authorization", "")
    if authz:
        scheme = authz.partition(" ")[0].upper()
        if scheme not in (sigv4.ALGORITHM, sigv4a.ALGORITHM):
            return None
    elif "X-Amz-Algorithm" not in request.query_params:
        return None
    return request.headers.get("x-amz-content-sha256")


def _wire(request: Request) -> tuple[str, str]:
    """The path and query as uvicorn received them. Both halves of what is signed come off the
    wire, not off `request.url`: Starlette rebuilds that URL from the DECODED path, so a key
    containing `%3F` turns into a `?` that splits it — `/q%3Fx.txt` reads back as path `/q` with
    query `x.txt`, a query the client never signed."""
    raw = request.scope.get("raw_path")
    path = raw.decode("ascii") if raw else request.url.path
    return path, request.scope.get("query_string", b"").decode("ascii")


def _skewed(request_time: str, now: datetime) -> SigV4Refusal:
    return SigV4Refusal(
        "RequestTimeTooSkewed",
        "The difference between the request time and the current time is too large.",
        (
            ("RequestTime", request_time),
            ("ServerTime", now.strftime(_SERVER_TIME)),
            ("MaxAllowedSkewMilliseconds", "900000"),
        ),
    )


def _unsigned(hdrs: dict[str, str], signed_headers: str) -> SigV4Refusal | None:
    """Real's refusal of a request carrying a header it requires to be signed and that is not:
    `Host`, `Content-MD5` and the `x-amz-*` headers but `x-amz-content-sha256`. Each such header
    sent unsigned was refused, in a V4 header, a V4 presign and SigV4a alike, `x-amz-date`,
    `x-amz-region-set`, `x-amz-acl` and `x-amz-meta-*` ones among them, and a `Content-Type`, a
    `Range`, a `Date`, `If-Match`, `If-None-Match`, `If-Modified-Since`, `Cache-Control`,
    `Content-Length`, `Content-Encoding`, `Content-Disposition`, `Content-Language`,
    `x-amzn-trace-id` and an unsigned `x-amz-content-sha256` were not (2026-09-29). They are named
    in ``_hash_set_order``."""
    signed = set(signed_headers.split(";"))
    names = sorted(
        name
        for name in hdrs
        if name not in signed
        and (
            name in ("host", "content-md5")
            or (name.startswith("x-amz-") and name != "x-amz-content-sha256")
        )
    )
    if not names:
        return None
    return SigV4Refusal(
        "AccessDenied", _NOT_SIGNED, (("HeadersNotSigned", ", ".join(_hash_set_order(names))),)
    )


def _hash_set_order(names: list[str]) -> list[str]:
    """``names`` in the order a `java.util.HashSet` they were added to in name order iterates them:
    by the bucket each `String.hashCode` spreads to, over sixteen buckets doubled while the set is
    more than three quarters full, and in name order within a bucket. That order was real's for
    each of the seventeen lists of unsigned headers sent (2026-09-29), seven of which put two names
    in one bucket and three of which held more than twelve."""
    capacity = 16
    while len(names) > capacity * 3 // 4:
        capacity *= 2

    def bucket(name: str) -> int:
        code = 0
        for char in name:
            code = (31 * code + ord(char)) & 0xFFFFFFFF
        return (code ^ (code >> 16)) & (capacity - 1)

    return sorted(sorted(names), key=bucket)


def _region_set_mismatch(region_set: str) -> SigV4Refusal:
    return SigV4Refusal(
        "RegionSetMismatch",
        "The provided X-Amz-Region-Set doesn't match against the requested S3 region.",
        (("RequestedRegion", sigv4.REGION), ("RegionSet", region_set)),
    )


def resolve_sigv4(request: Request) -> tuple[Caller | None, SigV4Refusal | None]:
    """Verify an S3 request's credential: Signature Version 4 or SigV4a (``backlot.sigv4a``) in the
    header or the query (``_resolve_v4_header``, ``_resolve_v4_query``), or Signature Version 2 in
    either (``_resolve_v2_header``, ``_resolve_v2_query``).

    Returns ``(caller, None)`` on a valid signature, ``(ANONYMOUS, None)`` for a request carrying
    no credential at all, and ``(None, refusal)`` otherwise. Real S3 reads an unsigned request as
    an anonymous caller's rather than refusing it, and so does this: what an anonymous caller can
    see is decided where every caller's is, and it can see nothing. The `?annotation` reads refuse
    it instead (``backlot.routers.s3._sigv4_only_refusal``). A V4 presigned request is one carrying
    `X-Amz-Algorithm` and a V2 one a request carrying `Signature`; an `X-Amz-Signature` without the
    first and an `AWSAccessKeyId` without the second are unsigned on real (a public bucket's listing
    answered the one and a bucket's own owner was refused the other as anonymous).

    Each refusal is real's own code, message and members, and they come in real's order: measured
    2026-09-29 against `s3.us-east-1.amazonaws.com`, one fault at a time and, where two could meet,
    the two together. A header beside `X-Amz-Algorithm` or `Signature` in the query is refused
    first, whatever either says, an empty one included, and the two query forms beside each other.
    Real's front ends split on the empty header as they split on `annotationName`, the same
    addresses on the same side of both but for a few that answered either way
    (``backlot.routers.s3._key_selected`` has the proportion, 2026-09-30): most refuse it beside
    either query form, the rest read it as absent, and this refuses it; with no credential in the
    query, every one measured read the request as unsigned. For a header it is then its one space
    and its scheme, matched without case (anything but `AWS4-HMAC-SHA256`, `AWS4-ECDSA-P256-SHA256`
    and V2's `AWS` is `InvalidArgument`, "Unsupported Authorization Type", Bearer and
    `AWS4-HMAC-SHA512` alike), and after those the order each form's own function gives. A mismatch
    names the string this server signed and, for V4 and SigV4a, the canonical request it signed it
    over, as bytes too, the way real names its own. The canonical URI and query are the raw wire
    path and query string (S3 signs the path verbatim, see ``_wire``)."""
    hdrs = {k.lower(): v for k, v in request.headers.items()}
    qs = request.query_params
    now = datetime.now(timezone.utc)
    authz = hdrs.get("authorization", "")
    presigned = "X-Amz-Algorithm" in qs
    v2_query = "Signature" in qs
    argument = (("ArgumentName", "Authorization"), ("ArgumentValue", authz))
    if "authorization" in hdrs and (presigned or v2_query):
        return None, SigV4Refusal("InvalidArgument", _ONE_MECHANISM, argument)
    if authz:
        scheme, space, rest = authz.partition(" ")
        if not space:
            return None, SigV4Refusal("InvalidArgument", _NO_SPACE, argument)
        if scheme.upper() == "AWS":
            return _resolve_v2_header(request, hdrs, rest, argument, now)
        if scheme.upper() not in (sigv4.ALGORITHM, sigv4a.ALGORITHM):
            return None, SigV4Refusal("InvalidArgument", "Unsupported Authorization Type", argument)
        return _resolve_v4_header(request, hdrs, scheme, authz, now)
    if presigned:
        if v2_query:
            # Named without an `ArgumentValue`, there being no header to name (same date).
            return None, SigV4Refusal(
                "InvalidArgument", _ONE_MECHANISM, (("ArgumentName", "Authorization"),)
            )
        return _resolve_v4_query(request, hdrs, now)
    if v2_query:
        return _resolve_v2_query(request, hdrs, now)
    return ANONYMOUS, None


def _resolve_v4_header(
    request: Request, hdrs: dict[str, str], algorithm: str, authz: str, now: datetime
) -> tuple[Caller | None, SigV4Refusal | None]:
    """`Authorization: AWS4-HMAC-SHA256 …` or `AWS4-ECDSA-P256-SHA256 …`, refused as real refuses it
    (measured 2026-09-29).

    In real's order: a missing `x-amz-content-sha256`, then one that is neither a keyword real
    names (``_PAYLOAD_KEYWORDS``, spelt as written) nor 64 hex digits in either case; a missing or
    unreadable date, which is `x-amz-date` when one is sent, readable or not, and otherwise `Date`,
    in any form ``sigv2.parse_date`` reads, an `AccessDenied`; the clock skew, naming the date as
    sent; the three components; the credential scope (``_credential_fault``); a scope date that is
    not the request's in UTC; a header that had to be signed and was not (``_unsigned``); for
    SigV4a, a missing `x-amz-region-set` and then one that does not name this server's region
    (``sigv4a.region_set_matches``); the access key; the signature. The date is signed as sent, and
    a date past the year 9999 is real's 500. The date and everything before it come ahead of every
    fault in the header's body."""
    asymmetric = algorithm.upper() == sigv4a.ALGORITHM
    payload_hash = hdrs.get("x-amz-content-sha256")
    if payload_hash is None:
        message = _MISSING_HEADER + "x-amz-content-sha256"
        return None, SigV4Refusal("InvalidRequest", message)
    if payload_hash not in _PAYLOAD_KEYWORDS and not re.fullmatch(r"[0-9a-fA-F]{64}", payload_hash):
        members = (("ArgumentName", "x-amz-content-sha256"), ("ArgumentValue", payload_hash))
        return None, SigV4Refusal("InvalidArgument", _BAD_PAYLOAD_HASH, members)
    date_line = hdrs["x-amz-date"] if "x-amz-date" in hdrs else hdrs.get("date", "")
    try:
        request_time = sigv2.parse_date(date_line)
    except sigv2.DateOutOfRange:
        return None, SigV4Refusal("InternalError", _INTERNAL_ERROR)
    if request_time is None:
        return None, SigV4Refusal("AccessDenied", _NO_DATE)
    if sigv4.is_skewed(request_time, now):
        return None, _skewed(date_line, now)
    parsed = sigv4.parse_authorization(authz)
    if not parsed:
        message = (
            "the authorization header requires three components: Credential, SignedHeaders, "
            "and Signature."
        )
        return None, SigV4Refusal("AuthorizationHeaderMalformed", _HEADER_MALFORMED + message)
    credential = parsed["credential"]
    fault = _credential_fault(credential, asymmetric)
    if fault:
        message, members = fault
        return None, SigV4Refusal(
            "AuthorizationHeaderMalformed", _HEADER_MALFORMED + message, members
        )
    if credential.split("/")[1] != request_time.strftime("%Y%m%d"):
        message = "Invalid credential date. Date is not the same as X-Amz-Date."
        return None, SigV4Refusal("AuthorizationHeaderMalformed", _HEADER_MALFORMED + message)
    unsigned = _unsigned(hdrs, parsed["signed_headers"])
    if unsigned is not None:
        return None, unsigned
    if asymmetric:
        region_set = hdrs.get("x-amz-region-set")
        if region_set is None:
            message = _MISSING_HEADER + "x-amz-region-set"
            return None, SigV4Refusal("InvalidRequest", message)
        if not sigv4a.region_set_matches(region_set, sigv4.REGION):
            return None, _region_set_mismatch(region_set)
    return _verify_v4(
        request,
        hdrs,
        credential,
        algorithm,
        date_line,
        parsed["signed_headers"],
        parsed["signature"],
        payload_hash,
    )


def _resolve_v4_query(
    request: Request, hdrs: dict[str, str], now: datetime
) -> tuple[Caller | None, SigV4Refusal | None]:
    """`?X-Amz-Algorithm=…&X-Amz-Credential=…&…`, refused as real refuses it (measured 2026-09-29).

    In real's order: the algorithm, `AWS4-HMAC-SHA256` or `AWS4-ECDSA-P256-SHA256` spelt as written;
    the six parameters; the date; `X-Amz-Expires` as a number (``_long``), as not negative and as a
    week at most; a date not yet valid; the expiry; the credential scope (``_credential_fault``);
    its date; a header that had to be signed and was not (``_unsigned``); for SigV4a, an
    `X-Amz-Region-Set` missing or empty and then one that does not name this server's region; the
    access key; the signature, whose payload line is the `x-amz-content-sha256` the request sends,
    unread, and `UNSIGNED-PAYLOAD` without one."""
    qs = request.query_params
    algorithm = qs["X-Amz-Algorithm"]
    if algorithm not in (sigv4.ALGORITHM, sigv4a.ALGORITHM):
        message = 'X-Amz-Algorithm only supports "AWS4-HMAC-SHA256 and AWS4-ECDSA-P256-SHA256"'
        return None, SigV4Refusal("AuthorizationQueryParametersError", message)
    asymmetric = algorithm == sigv4a.ALGORITHM
    if any(name not in qs for name in _QUERY_PARAMETERS):
        message = (
            "Query-string authentication version 4 requires the X-Amz-Algorithm, "
            "X-Amz-Credential, X-Amz-Signature, X-Amz-Date, X-Amz-SignedHeaders, and "
            "X-Amz-Expires parameters."
        )
        return None, SigV4Refusal("AuthorizationQueryParametersError", message)
    amz_date = qs["X-Amz-Date"]
    request_time = sigv4.parse_amz_date(amz_date)
    if request_time is None:
        message = "X-Amz-Date must be in the ISO8601 Long Format \"yyyyMMdd'T'HHmmss'Z'\""
        return None, SigV4Refusal("AuthorizationQueryParametersError", message)
    expires_in = _long(qs["X-Amz-Expires"])
    if expires_in is None:
        message = "X-Amz-Expires should be a number"
        return None, SigV4Refusal("AuthorizationQueryParametersError", message)
    if expires_in < 0:
        message = "X-Amz-Expires must be non-negative"
        return None, SigV4Refusal("AuthorizationQueryParametersError", message)
    if expires_in > _A_WEEK:
        message = (
            "X-Amz-Expires must be less than a week (in seconds); that is, the given "
            "X-Amz-Expires must be less than 604800 seconds"
        )
        return None, SigV4Refusal("AuthorizationQueryParametersError", message)
    expires_at = (request_time + timedelta(seconds=expires_in)).strftime(_SERVER_TIME)
    if (request_time - now).total_seconds() > _NOT_YET_VALID:
        return None, SigV4Refusal(
            "AccessDenied",
            "Request is not yet valid",
            (
                ("X-Amz-Date", str(int(request_time.timestamp() * 1000))),
                ("Expires", expires_at),
                ("ServerTime", now.strftime(_SERVER_TIME)),
            ),
        )
    if (now - request_time).total_seconds() > expires_in:
        return None, SigV4Refusal(
            "AccessDenied",
            "Request has expired",
            (
                ("X-Amz-Expires", qs["X-Amz-Expires"]),
                ("Expires", expires_at),
                ("ServerTime", now.strftime(_SERVER_TIME)),
            ),
        )
    credential = qs["X-Amz-Credential"]
    fault = _credential_fault(credential, asymmetric)
    if fault:
        message, members = fault
        return None, SigV4Refusal(
            "AuthorizationQueryParametersError", _QUERY_CREDENTIAL + message, members
        )
    if credential.split("/")[1] != amz_date[:8]:
        message = (
            f'Invalid credential date "{credential.split("/")[1]}". This date is not the same '
            f'as X-Amz-Date: "{amz_date[:8]}".'
        )
        return None, SigV4Refusal("AuthorizationQueryParametersError", message)
    unsigned = _unsigned(hdrs, qs["X-Amz-SignedHeaders"])
    if unsigned is not None:
        return None, unsigned
    if asymmetric:
        region_set = qs.get("X-Amz-Region-Set", "")
        if not region_set:
            message = "SigV4a query auth requires a non empty x-amz-region-set parameter."
            return None, SigV4Refusal("AuthorizationQueryParametersError", message)
        if not sigv4a.region_set_matches(region_set, sigv4.REGION):
            return None, _region_set_mismatch(region_set)
    return _verify_v4(
        request,
        hdrs,
        credential,
        algorithm,
        amz_date,
        qs["X-Amz-SignedHeaders"],
        qs["X-Amz-Signature"],
        hdrs.get("x-amz-content-sha256", "UNSIGNED-PAYLOAD"),
    )


def _long(raw: str) -> int | None:
    """An `X-Amz-Expires` as real reads it, as a Java long: a sign, then decimal digits of any
    script. `+300`, `0300`, `٣٠٠` and `３００` were 300, and 9223372036854775807 and
    -9223372036854775808 read as numbers, where ` 300`, `300 `, `3_00`, `3e2`, `0x12c`, `300.0`,
    an empty value, a sign alone, 9223372036854775808 and -9223372036854775809 were not
    (2026-09-29). Leading zeros come off before the value is read, so a run of them never reaches
    `int()`, which refuses past 4300 digits."""
    match = re.fullmatch(r"([+-]?)(\d+)", raw)
    if match is None:
        return None
    digits = "".join(str(int(char)) for char in match[2]).lstrip("0") or "0"
    if len(digits) > 19:
        return None
    value = int(digits) * (-1 if match[1] == "-" else 1)
    return value if -_LONG <= value < _LONG else None


def _verify_v4(
    request: Request,
    hdrs: dict[str, str],
    credential: str,
    algorithm: str,
    date_line: str,
    signed_headers: str,
    signature: str,
    payload_hash: str,
) -> tuple[Caller | None, SigV4Refusal | None]:
    """The access key and then the signature, SigV4's or SigV4a's by ``algorithm``, the string to
    sign starting with the algorithm as sent and the date line as sent."""
    access_key, scope = credential.split("/", 1)
    resolved = acl(request).resolve_access_key(access_key)
    if resolved is None:
        return None, SigV4Refusal("InvalidAccessKeyId", _NO_KEY, (("AWSAccessKeyId", access_key),))
    caller, secret = resolved
    path, query = _wire(request)
    canonical = sigv4.canonical_request(
        request.method, path, query, hdrs, signed_headers, payload_hash
    )
    if algorithm.upper() == sigv4a.ALGORITHM:
        to_sign = sigv4a.string_to_sign(algorithm, date_line, scope, canonical)
        verified = sigv4a.verify(access_key, secret, to_sign, signature)
    else:
        date_stamp, region = scope.split("/")[:2]
        to_sign = sigv4.string_to_sign(date_line, date_stamp, region, canonical, algorithm)
        verified = hmac.compare_digest(
            sigv4.sign(secret, date_stamp, region, to_sign).encode(), signature.encode()
        )
    if not verified:
        return None, SigV4Refusal(
            "SignatureDoesNotMatch",
            _MISMATCH,
            (
                ("AWSAccessKeyId", access_key),
                ("StringToSign", to_sign),
                ("SignatureProvided", signature),
                ("StringToSignBytes", _hex_bytes(to_sign)),
                ("CanonicalRequest", canonical),
                ("CanonicalRequestBytes", _hex_bytes(canonical)),
            ),
        )
    return caller, None


def _resolve_v2_header(
    request: Request, hdrs: dict[str, str], rest: str, argument: tuple, now: datetime
) -> tuple[Caller | None, SigV4Refusal | None]:
    """`Authorization: AWS <key>:<signature>`, refused as real refuses it (measured 2026-09-29).

    In real's order: a second space after the scheme, which V4's header may carry and this one may
    not; a body that is not one key, one colon and a signature (`garbage`, `<key>:` and `<key>:a:b`
    alike, where an empty key reads as a key); the date; the skew, naming the date as sent; the
    access key; the signature. The date is `x-amz-date` when one is sent, readable or not, and
    otherwise `Date`, in any of the forms ``sigv2.parse_date`` reads, a date past the year 9999
    being real's 500, and the line signed for it is empty beside an `x-amz-date`, which is signed
    among the `x-amz-*` headers instead."""
    if rest.startswith(" "):
        return None, SigV4Refusal("InvalidArgument", _NO_SPACE, argument)
    access_key, colon, signature = rest.partition(":")
    if not colon or not signature or ":" in signature:
        message = "AWS authorization header is invalid.  Expected AwsAccessKeyId:signature"
        return None, SigV4Refusal("InvalidArgument", message, argument)
    if "x-amz-date" in hdrs:
        date, date_line = hdrs["x-amz-date"], ""
    else:
        date = date_line = hdrs.get("date", "")
    try:
        request_time = sigv2.parse_date(date)
    except sigv2.DateOutOfRange:
        return None, SigV4Refusal("InternalError", _INTERNAL_ERROR)
    if request_time is None:
        return None, SigV4Refusal("AccessDenied", _NO_DATE)
    if sigv4.is_skewed(request_time, now):
        return None, _skewed(date, now)
    return _verify_v2(request, hdrs, access_key, signature, date_line)


def _resolve_v2_query(
    request: Request, hdrs: dict[str, str], now: datetime
) -> tuple[Caller | None, SigV4Refusal | None]:
    """`?AWSAccessKeyId=…&Expires=…&Signature=…`, refused as real refuses it (measured 2026-09-29).

    In real's order: `Expires` or `AWSAccessKeyId` missing beside the `Signature`; an `Expires`
    that is not a date (``_INT32``), naming it as sent; one whose milliseconds fall before the year
    zero (``_YEAR_ZERO_MS``), real's 500; the expiry, naming it as a time (``_java_time``); the
    access key; the signature, over a string whose date line is `Expires` as sent. A query has no
    clock skew: one ten years ahead was served."""
    qs = request.query_params
    if "Expires" not in qs or "AWSAccessKeyId" not in qs:
        message = (
            "Query-string authentication requires the Signature, Expires and AWSAccessKeyId "
            "parameters"
        )
        return None, SigV4Refusal("AccessDenied", message)
    raw = qs["Expires"]
    match = re.fullmatch(r"([+-]?)([0-9]+)", raw)
    # Leading zeros come off before the value is read, for the reason ``_long`` gives.
    digits = match[2].lstrip("0") or "0" if match else ""
    expires = int(digits) * (-1 if match[1] == "-" else 1) if match and len(digits) <= 19 else None
    if expires is None or not -_LONG <= expires < _INT32:
        message = f"Invalid date (should be seconds since epoch): {raw}"
        return None, SigV4Refusal("AccessDenied", message)
    milliseconds = (expires * 1000 + _LONG) % (2 * _LONG) - _LONG
    if milliseconds < _YEAR_ZERO_MS:
        return None, SigV4Refusal("InternalError", _INTERNAL_ERROR)
    if now.timestamp() * 1000 > milliseconds:
        return None, SigV4Refusal(
            "AccessDenied",
            "Request has expired",
            (("Expires", _java_time(milliseconds)), ("ServerTime", now.strftime(_SERVER_TIME))),
        )
    return _verify_v2(request, hdrs, qs["AWSAccessKeyId"], qs["Signature"], raw)


def _java_time(milliseconds: int) -> str:
    """An instant as real names a V2 query's `Expires`, Julian before 1582
    (``_GREGORIAN_CUTOVER``)."""
    seconds = milliseconds // 1000
    if seconds >= _GREGORIAN_CUTOVER:
        return (_EPOCH + timedelta(seconds=seconds)).strftime(_SERVER_TIME)
    days, rest = divmod(seconds, 86400)
    shifted = days + 2440588 + 32082
    cycle = (4 * shifted + 3) // 1461
    left = shifted - 1461 * cycle // 4
    month_index = (5 * left + 2) // 153
    day = left - (153 * month_index + 2) // 5 + 1
    month = month_index + 3 - 12 * (month_index // 10)
    year = cycle - 4800 + month_index // 10
    era_year = year if year > 0 else 1 - year
    clock = f"{rest // 3600:02d}:{rest % 3600 // 60:02d}:{rest % 60:02d}"
    return f"{era_year:04d}-{month:02d}-{day:02d}T{clock}Z"


# Where `backlot.routers.s3` is mounted, which real S3 has no counterpart of.
_S3_MOUNT = "/s3"


def _verify_v2(
    request: Request, hdrs: dict[str, str], access_key: str, signature: str, date_line: str
) -> tuple[Caller | None, SigV4Refusal | None]:
    """The access key and then the signature, the members named as real names them for V2: the
    string signed and its bytes, and no canonical request, V2 having none.

    Real signs the path it was sent, which on real is the bucket and the key. Here that path sits
    under ``_S3_MOUNT``, and a client signs either what is under it or all of it: boto3 signs the
    bucket and the key it addresses (its `auth_path`) and for ListBuckets the URL's own path, and a
    signer handed a URL signs that URL's path. So a signature over either verifies, and a mismatch
    names the one real would sign, the path under the mount. boto3's path-style signature for a
    bucket's own operations carries a slash after the bucket that its URL does not, and real
    refused that as a mismatch (2026-09-29), which this is too."""
    resolved = acl(request).resolve_access_key(access_key)
    if resolved is None:
        return None, SigV4Refusal("InvalidAccessKeyId", _NO_KEY, (("AWSAccessKeyId", access_key),))
    caller, secret = resolved
    path, query = _wire(request)
    under = path[len(_S3_MOUNT) :] if path.startswith(_S3_MOUNT) else path
    signed = [
        sigv2.string_to_sign(request.method, hdrs, date_line, candidate, query)
        for candidate in (under or "/", path)
    ]
    to_sign = signed[0]
    if not any(
        hmac.compare_digest(sigv2.sign(secret, candidate).encode(), signature.encode())
        for candidate in signed
    ):
        return None, SigV4Refusal(
            "SignatureDoesNotMatch",
            _MISMATCH,
            (
                ("AWSAccessKeyId", access_key),
                ("StringToSign", to_sign),
                ("SignatureProvided", signature),
                ("StringToSignBytes", _hex_bytes(to_sign)),
            ),
        )
    return caller, None
