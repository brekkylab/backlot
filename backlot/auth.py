"""Auth helpers shared by the vendor routers.

Each vendor carries credentials differently (Slack bearer/query token, Google/GitHub
bearer, Atlassian Basic email:api_token, Linear a scheme-less API key). These helpers
extract the raw token, resolve it to a :class:`~backlot.acl.Caller` via the app's ACL, and
compute the caller's visible principal set. Error *shaping* (Slack's ``ok:false`` vs a
real 401) stays in the routers.
"""

from __future__ import annotations

import base64
import hmac
import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException, Request

from backlot import sigv4
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


def api_key_token(request: Request) -> str | None:
    """Parse ``Authorization: <key>`` — with or without a ``Bearer`` prefix.

    Linear's GraphQL API carries a personal API key as the bare header value
    (``Authorization: lin_api_...``, no scheme) and an OAuth access token as
    ``Bearer <token>``, accepting both on the same header, so this accepts both too.
    Anything that is not a ``Bearer`` prefix is returned verbatim rather than having its
    first word stripped: to the real API the whole header value *is* the key, so a stray
    scheme fails to resolve instead of being quietly discarded.
    """
    hdr = (_authorization(request) or "").strip()
    if not hdr:
        return None
    parts = hdr.split(None, 1)
    if parts[0].lower() == "bearer":
        return parts[1].strip() or None if len(parts) == 2 else None
    return hdr


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
    :func:`bearer_token`: the scheme must be exactly ``Bearer``, separated from the token by one or
    more SPACES.

    Measured against slack.com (a bogus token is enough — the presented/absent split needs no
    account). ``invalid_auth`` means the header counted as a credential, ``not_authed`` means it
    did not::

        Bearer <t>      invalid_auth      bearer <t>      not_authed
        Bearer  <t>     invalid_auth      BEARER <t>      not_authed
        ' Bearer <t>'   invalid_auth      token <t>       not_authed
        Bearer <t>' '   invalid_auth      Bearer<TAB><t>  not_authed
                                          Bearer<t>       not_authed

    A tab is not a space to Slack, so the generic whitespace split in :func:`bearer_token` is
    wrong here, and so is its case-insensitive scheme match. That function stays permissive because
    GitHub really does accept ``token <t>`` and RFC 7235 really does make the scheme
    case-insensitive; Slack implements neither. Of the five spellings above that Slack refuses,
    sharing it would authenticate four — every one but ``Bearer<t>``, which it does not take
    either — so a client sending ``Authorization: token <xoxb>`` would pass every test here and
    reach nothing in production.
    """
    hdr = (_authorization(request) or "").strip()
    if not hdr.startswith("Bearer "):
        return None
    return hdr[len("Bearer ") :].strip() or None


def slack_token(request: Request) -> str | None:
    """Slack accepts the token as a bearer header, query param, or form field. The official
    slack-go SDK (and Slack's own clients) post it as the ``token`` form field, so fall back to
    the form stashed on ``request.state._form`` by the slack-form middleware.

    Both names are case-sensitive too: ``?TOKEN=`` and a ``TOKEN`` form field are ``not_authed``
    live, which the exact-key lookups below already answer."""
    form = getattr(request.state, "_form", None)
    form_field = form.get("token") if form else None
    return slack_bearer_token(request) or request.query_params.get("token") or form_field


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
                                        OAuth <t>       not read
                                        Bearer  <t>     not read
                                        Bearer<TAB><t>  not read
                                        Bearer<t>       not read
                                        Bearer          not read

    The scheme is case-sensitive and separated from the token by exactly one space. Sharing
    :func:`bearer_token` would authenticate five of those spellings — every "not read" row above
    but ``OAuth <t>``, ``Bearer<t>`` and the bare ``Bearer`` — so a client sending
    ``Authorization: token <t>`` would pass every test here and read nothing in production. Slack
    refuses a different set: it takes the double space this refuses.

    The leading ``strip`` is what serves the ``Bearer <t>' '`` row, so the token needs no second
    one: nothing with trailing whitespace survives to reach it.
    """
    hdr = (_authorization(request) or "").strip()
    if not hdr.startswith("Bearer "):
        return None
    rest = hdr[len("Bearer ") :]
    if not rest or rest[0].isspace():
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


def resolve_sigv4(request: Request) -> tuple[Caller | None, str | None]:
    """Verify an S3 SigV4 request (header or presigned-query auth).

    Returns ``(caller, None)`` on a valid signature, else ``(None, <S3 error code>)`` — one of
    ``MissingSecurityHeader`` / ``AuthorizationHeaderMalformed`` / ``InvalidAccessKeyId`` /
    ``RequestTimeTooSkewed`` / ``AccessDenied`` / ``SignatureDoesNotMatch``. Real S3's check
    order is parse -> resolve access key -> time validity -> signature match, so a bogus access
    key is reported before any time error, and a stale-but-correctly-signed request is reported
    as a time error rather than a signature mismatch. The region is taken from the client's own
    credential scope, so any region validates. The canonical URI and query are the raw wire path
    and query string (S3 signs the path verbatim)."""
    hdrs = {k.lower(): v for k, v in request.headers.items()}
    qs = request.query_params
    authz = hdrs.get("authorization", "")
    presigned = False
    if authz.startswith(sigv4.ALGORITHM):
        parsed = sigv4.parse_authorization(authz)
        if not parsed:
            return None, "AuthorizationHeaderMalformed"
        cred = sigv4.split_credential(parsed["credential"])
        signed_headers, signature = parsed["signed_headers"], parsed["signature"]
        amz_date = hdrs.get("x-amz-date", "")
        payload_hash = hdrs.get("x-amz-content-sha256", "UNSIGNED-PAYLOAD")
    elif qs.get("X-Amz-Signature"):
        presigned = True
        cred = sigv4.split_credential(qs.get("X-Amz-Credential", ""))
        signed_headers = qs.get("X-Amz-SignedHeaders", "host")
        signature = qs["X-Amz-Signature"]
        amz_date = qs.get("X-Amz-Date", "")
        payload_hash = "UNSIGNED-PAYLOAD"
    else:
        return None, "MissingSecurityHeader"
    if not cred:
        return None, "AuthorizationHeaderMalformed"
    access_key, date_stamp, region = cred
    resolved = acl(request).resolve_access_key(access_key)
    if resolved is None:
        return None, "InvalidAccessKeyId"
    caller, secret = resolved
    request_time = sigv4.parse_amz_date(amz_date)
    if request_time is None:
        return None, "AuthorizationHeaderMalformed"
    now = datetime.now(timezone.utc)
    if presigned:
        try:
            expires_in = int(qs.get("X-Amz-Expires", ""))
        except ValueError:
            return None, "AuthorizationHeaderMalformed"
        if (now - request_time).total_seconds() > expires_in:
            return None, "AccessDenied"
    elif sigv4.is_skewed(request_time, now):
        return None, "RequestTimeTooSkewed"
    # Both halves of the canonical request come off the wire, not off `request.url`: Starlette
    # rebuilds that URL from the DECODED path, so a key containing `%3F` turns into a `?` that
    # splits it — `/q%3Fx.txt` reads back as path `/q` with query `x.txt`, a query the client never
    # signed. `raw_path` and `query_string` are the bytes uvicorn received.
    raw = request.scope.get("raw_path")
    path = raw.decode("ascii") if raw else request.url.path
    query = request.scope.get("query_string", b"").decode("ascii")
    expected = sigv4.expected_signature(
        secret,
        request.method,
        path,
        query,
        hdrs,
        signed_headers,
        payload_hash,
        amz_date,
        date_stamp,
        region,
    )
    if not hmac.compare_digest(expected, signature):
        return None, "SignatureDoesNotMatch"
    return caller, None
