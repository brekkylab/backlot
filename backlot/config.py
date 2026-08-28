"""Runtime configuration for Backlot server.

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
    # No `atlassian_site` here on purpose: the host in a Jira/Confluence `self` URL comes from the
    # REQUEST's own Host header, falling back to `<org_name>.atlassian.net`. Both rungs are already
    # customizable — per call by the header every SDK sends, and globally by BACKLOT_ORG_NAME — so a
    # third setting could only disagree with the caller about where the caller just reached us.
    # See backlot.routers.atlassian._site.

    # --- auth ---
    # A caller presenting this token bypasses ACL filtering (full crawl / service account).
    admin_token: str = "admin-service-token"
    # Expose the /_meta/users directory (per-user tokens) so callers can test per-user ACL.
    # It hands out tokens in the clear — fine locally; set false to disable.
    expose_tokens: bool = True

    # --- pagination defaults ---
    default_page_size: int = 100
    max_page_size: int = 1000

    # --- sqlite read tuning (serving connection; see store.connect_ro) ---
    # Sized for the corpus most people serve — their own, or the bundled one, which is under a
    # megabyte. A multi-GB corpus wants all three raised, and a deployment that serves one says so
    # explicitly (see the `environment:` block in docker-compose.yml) rather than every laptop
    # inheriting numbers picked for the biggest DB anyone has run here.
    #
    # Memory-map the DB so reads come from the OS page cache instead of a syscall each — the main
    # lever against the "slow first request after idle" cold-read hit. SQLite maps
    # min(this, db size), so a small DB costs only its own size in address space; raise it to at
    # or above the DB size to map a big one fully.
    sqlite_mmap_mb: int = 256
    # SQLite's own page cache, per connection. 64 MiB is a real improvement on SQLite's ~2 MiB
    # default without reserving a quarter gigabyte on a machine serving a 700 KB corpus.
    sqlite_cache_mb: int = 64
    # Wait (ms) for a lock instead of erroring, so a read rides through an out-of-band writer's
    # commit (an in-place `build_fts`) rather than 500ing. Long enough to cover a commit, short
    # enough that a genuinely stuck writer surfaces instead of hanging the client.
    sqlite_busy_ms: int = 5000

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db.sqlite"

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
