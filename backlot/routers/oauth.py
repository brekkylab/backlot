"""Mock OAuth 2.0 token endpoint (``POST /oauth2/token``).

Turns a Google-style client credential into a bearer access token the rest of the mock already
understands (the user's ``usr-`` token): a ``refresh_token`` grant (authorized-user flow) or a
signed service-account JWT assertion (``jwt-bearer`` grant, with the ``sub`` claim selecting the
impersonated user under domain-wide delegation). A bare service account with no ``sub`` maps to
the admin/service token — a full-crawl identity, the pragmatic mock stand-in for a service
principal. See :mod:`backlot.oauth` for how the credentials are generated and verified.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backlot.oauth import JWT_BEARER_GRANT

router = APIRouter(tags=["oauth"])


def _err(detail: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": detail}, status_code=status)


@router.post("/oauth2/token")
async def token(request: Request):
    oauth = getattr(request.app.state, "oauth", None)
    form = dict(await request.form())
    grant = form.get("grant_type", "")
    acl = request.app.state.acl

    if oauth is None:
        return _err("temporarily_unavailable: no mock credentials configured")

    if grant == "refresh_token":
        # the refresh_token IS the user's bearer token (from /_mock/users) — validate it
        # resolves to someone and hand it straight back as the access token.
        rt = form.get("refresh_token")
        access = rt if acl.resolve(rt) is not None else None
    elif grant in (JWT_BEARER_GRANT, "assertion"):
        result = oauth.verify_assertion(form.get("assertion"))
        if result is None:
            return _err("invalid_grant")
        if isinstance(result, tuple):  # bare service account (no sub) → service/crawl identity
            access = acl.admin_token
        else:
            access = acl.email_to_token().get(result)
            if access is None:  # sub not a known corpus user
                return _err("invalid_grant: unknown subject")
    else:
        return _err("unsupported_grant_type")

    if not access:
        return _err("invalid_grant")
    # static access token (the caller's bearer token); expiry is cosmetic — a re-refresh just
    # returns the same token, so a long-lived crawl never breaks.
    return JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": 3599,
            "scope": form.get("scope", ""),
        },
        headers={"Cache-Control": "no-store"},
    )
