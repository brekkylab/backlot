"""Runtime configuration for the mock server.

All settings are overridable via environment variables (prefix ``BACKLOT_``) so the
server and the offline build scripts read the same values.

Corpus-specific knobs do NOT belong here — this is what every layer reads, and a setting only one
importer uses would put that importer's dataset in front of everyone. A downloading importer keeps
its own settings beside itself (see ``backlot.importer.erb.BenchSettings``), on the same env prefix.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BACKLOT_", env_file=".env", extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _resolve_path_defaults(cls, values):
        """Fill the path defaults from the CURRENT working directory.

        Not plain field defaults: those are evaluated at class-definition time, so the path would
        be frozen to the cwd at import. Not `Path(__file__).parent.parent` either — installed from
        a wheel that is `site-packages`, and a default of `site-packages/data` is never what
        anyone means. `BACKLOT_DATA_DIR` and an explicit kwarg are already present here, so
        `setdefault` leaves them alone.
        """
        if isinstance(values, dict):
            values.setdefault("data_dir", Path("data").resolve())
        return values

    # --- paths --- (default supplied by _resolve_path_defaults above)
    data_dir: Path = Path("data")

    # --- identity / org ---
    # The org name/domain are derived at import time from the dominant email domain in whatever
    # was loaded, via infer_org() below. These are only the last-resort fallback for data that
    # carries no emails; BACKLOT_ORG_NAME / BACKLOT_ORG_DOMAIN override the derivation entirely.
    org_name: str = "example"
    org_domain: str = "example.com"
    # Fallback host for Jira/Confluence ``self`` URLs when a request carries no Host header
    # (SDKs always send one). Empty -> derived from the org name (``<org>.atlassian.net``).
    atlassian_site: str = ""

    # --- auth ---
    # A caller presenting this token bypasses ACL filtering (full crawl / service account).
    admin_token: str = "admin-service-token"
    # If false, any well-formed token is accepted as admin (ACL still exposed, not enforced).
    enforce_acl: bool = True
    # Expose the /_mock/users directory (per-user tokens) so callers can test per-user ACL.
    # It hands out tokens in the clear — fine for a local test mock; set false to disable.
    expose_tokens: bool = True

    # --- pagination defaults ---
    default_page_size: int = 100
    max_page_size: int = 1000

    # --- sqlite read tuning (serving connection; see store.connect_ro) ---
    # Memory-map the DB so reads are served from the OS page cache instead of per-read
    # syscalls — the main lever against the "slow first request after idle" cold-read hit on a
    # large DB. Set >= the DB size to map it fully (SQLite caps to its compile-time max).
    sqlite_mmap_mb: int = 12288  # ~12 GiB, enough to map the largest corpus served so far
    sqlite_cache_mb: int = 256  # SQLite's own page cache
    # Wait (ms) for a lock instead of erroring, so reads ride through an in-place FTS rebuild's
    # commit rather than 500ing; only ever engages during such an out-of-band write.
    sqlite_busy_ms: int = 30000

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mock.sqlite"

    @property
    def tokens_path(self) -> Path:
        return self.data_dir / "tokens.yaml"

    @property
    def credentials_path(self) -> Path:
        return self.data_dir / "credentials.yaml"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def infer_org(emails, settings: Settings) -> tuple[str, str]:
    """Derive ``(org_name, org_domain)`` from the dominant email domain in ``emails`` — so a
    ``@acme.com`` dataset serves as org ``acme`` rather than a hardcoded brand. An explicit
    ``BACKLOT_ORG_NAME`` / ``BACKLOT_ORG_DOMAIN`` env var wins; data with no emails keeps the
    settings fallback. ``org_name`` is the domain's first label (``acme.com`` -> ``acme``)."""
    import os
    from collections import Counter

    name_set = "BACKLOT_ORG_NAME" in os.environ
    domain_set = "BACKLOT_ORG_DOMAIN" in os.environ
    counts: Counter = Counter()
    for e in emails:
        if isinstance(e, str) and "@" in e:
            counts[e.split("@", 1)[1].lower()] += 1

    if domain_set:
        domain = settings.org_domain
    elif counts:
        domain = counts.most_common(1)[0][0]
    else:
        domain = settings.org_domain
    if name_set:
        name = settings.org_name
    elif domain_set or counts:
        name = domain.split(".")[0]
    else:
        name = settings.org_name
    return name, domain
