"""Runtime configuration for the mock server.

All settings are overridable via environment variables (prefix ``MOCK_``) so the
server and the offline build scripts read the same values.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCK_", env_file=".env", extra="ignore")

    # --- paths ---
    data_dir: Path = REPO_ROOT / "data"
    # ERB download cache (the bench `generated_data` tarball + its extraction). Pinned to a STABLE
    # location independent of ``data_dir`` so re-imports into a fresh build dir
    # (``MOCK_DATA_DIR=/tmp/... python -m app.importer.erb``) REUSE the already-downloaded JSONs
    # instead of re-fetching from GitHub every time. Override with ``MOCK_RAW_DIR``.
    raw_dir: Path = REPO_ROOT / "data" / "raw"

    # --- identity / org ---
    # The org name/domain are derived at import time from the data's dominant email domain —
    # the BYO corpus (app.importer.byo) or the bench employee directory (app.importer.erb), via
    # infer_org() below. These are only the last-resort fallback for data that carries no emails;
    # MOCK_ORG_NAME / MOCK_ORG_DOMAIN override the derivation entirely.
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
    sqlite_mmap_mb: int = 12288  # ~12 GiB, covers the full augmented corpus
    sqlite_cache_mb: int = 256  # SQLite's own page cache
    # Wait (ms) for a lock instead of erroring, so reads ride through an in-place FTS rebuild's
    # commit rather than 500ing; only ever engages during such an out-of-band write.
    sqlite_busy_ms: int = 30000

    # --- data build ---
    dataset_repo: str = "onyx-dot-app/EnterpriseRAG-Bench"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mock.sqlite"

    @property
    def tokens_path(self) -> Path:
        return self.data_dir / "tokens.yaml"

    @property
    def credentials_path(self) -> Path:
        return self.data_dir / "credentials.yaml"

    @property
    def employee_yaml(self) -> Path:
        return self.data_dir / "employee_directory.yaml"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def infer_org(emails, settings: Settings) -> tuple[str, str]:
    """Derive ``(org_name, org_domain)`` from the dominant email domain in ``emails`` — so a
    ``@acme.com`` dataset serves as org ``acme`` rather than a hardcoded brand. An explicit
    ``MOCK_ORG_NAME`` / ``MOCK_ORG_DOMAIN`` env var wins; data with no emails keeps the
    settings fallback. ``org_name`` is the domain's first label (``acme.com`` -> ``acme``)."""
    import os
    from collections import Counter

    name_set = "MOCK_ORG_NAME" in os.environ
    domain_set = "MOCK_ORG_DOMAIN" in os.environ
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
