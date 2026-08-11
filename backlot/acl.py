"""Runtime ACL: resolve a caller token to an identity and compute what it may see.

Principal ids are globally unique across types (org name, group slugs, user emails),
so a document is visible to a caller iff any of the doc's ACL ``principal_id`` values
is in the caller's principal set: ``{org} ∪ {their groups} ∪ {their own email}``.
An admin/service token bypasses filtering entirely (``visible_ids`` -> ``None``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

from backlot import store, synth


@dataclass(frozen=True)
class Caller:
    email: str | None  # None for admin/service account
    is_admin: bool


class Acl:
    def __init__(self, token_to_email: dict[str, str], admin_token: str, org_name: str):
        self._tokens = token_to_email
        self._admin_token = admin_token
        self.org_name = org_name

        # Derived S3 (SigV4) credentials: access-key-id -> (Caller, secret-access-key). Every
        # bearer token (users + the admin/service token) gets a deterministic keypair via synth,
        # so a signed S3 request resolves to the same identity a bearer token would.
        self._access_keys: dict[str, tuple[Caller, str]] = {}
        self._access_keys[synth.s3_access_key_id(admin_token)] = (
            Caller(email=None, is_admin=True),
            synth.s3_secret_access_key(admin_token),
        )
        for token, email in token_to_email.items():
            self._access_keys[synth.s3_access_key_id(token)] = (
                Caller(email=email, is_admin=False),
                synth.s3_secret_access_key(token),
            )

    @property
    def admin_token(self) -> str:
        return self._admin_token

    def email_to_token(self) -> dict[str, str]:
        """Inverse of the token map (each user has exactly one token)."""
        return {email: token for token, email in self._tokens.items()}

    @classmethod
    def load(cls, tokens_path: Path, admin_token: str, org_name: str) -> "Acl":
        token_to_email: dict[str, str] = {}
        if tokens_path.exists():
            data = yaml.safe_load(tokens_path.read_text()) or {}
            for entry in data.get("users", []):
                if entry.get("token") and entry.get("email"):
                    token_to_email[entry["token"]] = entry["email"]
            # tokens.yaml may override the admin token and the org (BYO derives it from the corpus)
            admin_token = data.get("admin_token", admin_token)
            org_name = data.get("org", org_name)
        return cls(token_to_email, admin_token, org_name)

    def resolve(self, token: str | None) -> Caller | None:
        """Return the Caller for a raw token, or None if the token is unknown."""
        if not token:
            return None
        if token == self._admin_token:
            return Caller(email=None, is_admin=True)
        email = self._tokens.get(token)
        if email is None:
            return None
        return Caller(email=email, is_admin=False)

    def resolve_access_key(self, access_key: str | None) -> tuple[Caller, str] | None:
        """Resolve a SigV4 access-key-id to ``(Caller, secret_access_key)``, or None if unknown."""
        if not access_key:
            return None
        return self._access_keys.get(access_key)

    def visible_ids(self, conn: sqlite3.Connection, caller: Caller) -> set[str] | None:
        if caller.is_admin:
            return None
        ids = {self.org_name, caller.email}
        ids.update(store.user_group_ids(conn, caller.email))
        return ids
