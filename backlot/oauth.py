"""Mock OAuth credentials for the Google-style client-configuration flow.

Real Gmail/Drive connectors rarely hold a raw access token — they carry an OAuth **client
config**: either an ``authorized_user`` bundle (client_id/secret + a per-user refresh_token)
or a **service account** JSON (a private key used to mint a signed JWT and impersonate a user
via domain-wide delegation). This module lets those configs work against the mock unmodified:

- :func:`generate` synthesizes, at import time, one mock OAuth client and one org service
  account (a real RSA keypair), written to ``credentials.yaml``. There is **no** per-user data:
  a user's refresh_token is simply their existing bearer token (from ``tokens.yaml`` /
  ``/_mock/users``), so the token endpoint's refresh grant just validates it and hands it back.
- :class:`Oauth` is the runtime side: it exposes the client config and verifies a service-account
  JWT assertion (RS256, honoring the ``sub`` impersonation claim).

The mock token endpoint (``POST /oauth2/token``) turns either grant into that user's bearer
token, so the rest of auth/ACL is unchanged. ``token_uri`` in every bundle points back at the
mock, so the client's refresh / JWT-bearer call lands here rather than at Google.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

# NOTE: pyjwt + cryptography (pyjwt[crypto]) are imported lazily inside the functions that need
# them, not at module load. The server bind-mounts backlot/ over an image whose deps are baked in, so
# a code update can outrun a dependency install; keeping these imports lazy means the app still
# boots and the refresh_token flow (no crypto needed) still works even if the dep is missing —
# only service-account JWT verification / key generation require it.

JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"


def _h(*parts: str) -> str:
    return hashlib.sha256(("oauth:" + ":".join(parts)).encode()).hexdigest()


def generate(settings, org: str | None = None) -> dict:
    """Build (and persist to ``credentials.yaml``) the mock's OAuth credentials: one OAuth client
    and one org service account with a freshly generated RSA keypair. No per-user data — refresh
    tokens are the users' existing bearer tokens. Deterministic except the RSA key, which is
    generated once per import and stored."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    org = org or settings.org_name
    client_id = _h("client", org)[:32] + ".apps.googleusercontent.com"
    client_secret = "GOCSPX-" + _h("secret", org)[:28]
    sa_email = f"enterprise-mock@{org}.iam.gserviceaccount.com"

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )

    creds = {
        "org": org,
        "oauth_client": {"client_id": client_id, "client_secret": client_secret},
        "service_account": {
            "client_email": sa_email,
            "client_id": _h("said", org)[:21],
            "private_key_id": _h("pkid", org)[:40],
            "private_key": private_pem,
            "public_key_pem": public_pem,  # server-side only; never exposed via /_mock/credentials
        },
    }
    settings.credentials_path.write_text(yaml.safe_dump(creds, sort_keys=False))
    return creds


class Oauth:
    """Runtime resolver loaded from ``credentials.yaml`` (None if absent)."""

    def __init__(self, data: dict):
        self._data = data
        self._sa = data.get("service_account", {})

    @classmethod
    def load(cls, path: Path) -> "Oauth | None":
        if not Path(path).exists():
            return None
        data = yaml.safe_load(Path(path).read_text()) or {}
        if not data.get("oauth_client"):
            return None
        return cls(data)

    def client_config(self) -> dict:
        """The single OAuth client (client_id/secret) shared by all users."""
        return dict(self._data["oauth_client"])

    @property
    def client_email(self) -> str | None:
        """The service account's email (``enterprise-mock@<org>.iam.gserviceaccount.com``)."""
        return self._sa.get("client_email")

    def verify_assertion(self, assertion: str | None) -> str | tuple[None, str] | None:
        """Verify a service-account JWT (RS256) with the stored public key. Returns the ``sub``
        (impersonated user) when present, the sentinel ``("", "sa")`` for a bare SA (no
        delegation) so the caller can grant a service-account/crawl identity, or None if the
        assertion is invalid."""
        if not assertion or not self._sa.get("public_key_pem"):
            return None
        try:
            import jwt
        except (
            ImportError
        ):  # pyjwt[crypto] not installed — SA flow unavailable, refresh still works
            return None
        try:
            claims = jwt.decode(
                assertion,
                self._sa["public_key_pem"],
                algorithms=["RS256"],
                options={"verify_aud": False},
            )
        except jwt.PyJWTError:
            return None
        if claims.get("iss") != self._sa.get("client_email"):
            return None
        sub = claims.get("sub")
        return sub if sub else ("", "sa")

    def service_account_json(self, token_uri: str) -> dict:
        """The service_account.json a client would download (private key included; the public
        key kept server-side for verification is never exposed)."""
        sa = self._sa
        return {
            "type": "service_account",
            "project_id": self._data["org"],
            "private_key_id": sa["private_key_id"],
            "private_key": sa["private_key"],
            "client_email": sa["client_email"],
            "client_id": sa["client_id"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": token_uri,
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        }


if __name__ == "__main__":
    # Regenerate credentials.yaml (client + service account) — for a deployed DB built before this
    # feature, so no full re-import is needed. Run after restart to load it. Reads only the org
    # from tokens.yaml; refresh tokens are the users' existing bearer tokens, nothing to store.
    from backlot.config import get_settings

    s = get_settings()
    org = (
        (yaml.safe_load(s.tokens_path.read_text()) or {}).get("org")
        if s.tokens_path.exists()
        else None
    )
    generate(s, org=org)
    print(f"wrote {s.credentials_path} (org={org or s.org_name})")
