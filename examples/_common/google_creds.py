"""Mock-specific Google OAuth glue for the Gmail/Drive examples.

Not general API — a real connector gets its credentials from the Cloud Console / an OAuth
consent screen, not from a mock's ``/_mock/credentials`` endpoint — so this stays under
``examples/`` rather than in ``backlot.testing``. Every script that needs one of these puts
``examples/`` on ``sys.path`` first:

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from _common.google_creds import google_oauth_user
"""

from __future__ import annotations

import json
import urllib.request

__all__ = ["google_oauth_user", "google_service_account_info"]


def google_service_account_info(
    base_url: str, subject: str | None = None
) -> tuple[dict, str | None]:
    """Fetch the mock's service-account key from ``/_mock/credentials`` — the mock-specific glue,
    standing in for the JSON you'd download from the Cloud Console. Returns ``(sa_info, subject)``
    where ``subject`` is the user to impersonate via domain-wide delegation (ACL-filtered to them)
    or None (bare service account → admin, sees everything). The caller turns ``sa_info`` into a
    credential with the official google-auth library (see the examples). ``token_uri`` inside
    ``sa_info`` already points at the mock's ``/oauth2/token``."""
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/_mock/credentials") as r:
        sa = json.load(r)["service_account"]
    if subject:
        print(f"impersonating {subject} → responses are ACL-filtered to that user")
    return sa, subject


def google_oauth_user(base_url: str, user: str | None = None) -> tuple[str, str, str, str]:
    """Mock glue for the authorized-user (3LO) flow. Returns ``(client_id, client_secret,
    refresh_token, token_uri)``: the shared OAuth client's id/secret and ``token_uri`` from
    ``/_mock/credentials``, plus a user's bearer token (from ``/_mock/users`` — ``user`` if given,
    else the first) used as the ``refresh_token``. The caller builds the Credentials with the
    official google-auth library (see gmail.py); the library then refreshes against ``token_uri``
    (the mock's ``/oauth2/token``)."""
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/_mock/credentials") as r:
        creds = json.load(r)
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/_mock/users") as r:
        users = json.load(r)["users"]
    who = (
        next((u for u in users if u["email"] == user), None)
        if user
        else (users[0] if users else None)
    )
    if who is None:
        raise SystemExit(
            f"--user {user!r} not found in /_mock/users" if user else "no users on the mock"
        )
    print(f"authenticating as {who['email']} (authorized_user — client_id/secret + refresh token)")
    client = creds["oauth_client"]
    return client["client_id"], client["client_secret"], who["token"], creds["token_uri"]
