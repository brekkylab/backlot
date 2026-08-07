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
from backlot.acl import Acl, Caller


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


def basic_password(request: Request) -> tuple[str | None, str | None]:
    """Parse ``Authorization: Basic base64(user:pass)`` -> (user, pass)."""
    hdr = _authorization(request)
    if not hdr:
        return None, None
    parts = hdr.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "basic":
        try:
            decoded = base64.b64decode(parts[1]).decode("utf-8", "replace")
            user, _, pw = decoded.partition(":")
            return user, pw
        except (ValueError, UnicodeDecodeError):
            return None, None
    return None, None


def slack_token(request: Request) -> str | None:
    """Slack accepts the token as a bearer header, query param, or form field. The official
    slack-go SDK (and Slack's own clients) post it as the ``token`` form field, so fall back to
    the form stashed on ``request.state._form`` by the slack-form middleware."""
    form = getattr(request.state, "_form", None)
    form_field = form.get("token") if form else None
    return bearer_token(request) or request.query_params.get("token") or form_field


def resolve_bearer(request: Request) -> Caller | None:
    return acl(request).resolve(bearer_token(request))


def require_bearer(request: Request, detail: str) -> Caller:
    """Resolve a bearer token or raise 401 with the VENDOR's own message.

    ``detail`` is a parameter rather than something this function picks, because the message is
    part of the emulated surface: GitHub says "Bad credentials", Google "Invalid Credentials",
    Atlassian "Unauthorized", and a client that string-matches its provider's error has to keep
    matching. Each router states its own once (see ``tests/test_endpoints.py``).
    """
    caller = resolve_bearer(request)
    if caller is None:
        raise HTTPException(status_code=401, detail=detail)
    return caller


def require_basic_or_bearer(request: Request, detail: str) -> Caller:
    """Same, for Atlassian: it carries Basic ``email:api_token`` and also accepts a bearer OAuth
    token, so both are tried before refusing."""
    caller = resolve_basic(request) or resolve_bearer(request)
    if caller is None:
        raise HTTPException(status_code=401, detail=detail)
    return caller


def resolve_api_key(request: Request) -> Caller | None:
    return acl(request).resolve(api_key_token(request))


def resolve_basic(request: Request) -> Caller | None:
    """Atlassian: resolve by the api_token (password); fall back to the username email."""
    a = acl(request)
    user, pw = basic_password(request)
    caller = a.resolve(pw)
    if caller is not None:
        return caller
    # allow username=email as an identity shortcut (mock convenience)
    if user and "@" in user:
        from backlot import store

        if store.get_user(conn(request), user):
            return Caller(email=user, is_admin=False)
    return None


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
    credential scope, so any region validates. The canonical URI is the raw wire path (S3 signs
    it verbatim)."""
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
    raw = request.scope.get("raw_path")
    path = raw.decode("ascii") if raw else request.url.path
    expected = sigv4.expected_signature(
        secret,
        request.method,
        path,
        request.url.query,
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
