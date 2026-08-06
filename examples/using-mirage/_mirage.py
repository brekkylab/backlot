"""Point mirage's connectors at an enterprise-mock server.

Mirage (https://github.com/strukto-ai/mirage) is a virtual filesystem for AI agents: you mount a
SaaS backend and read it with bash-style commands (``ls``, ``cat``, ``grep``, ``find``).

Slack, Notion and S3 take the host as a config field, so they just get pointed at the mock. Google
and GitHub do not — they read it from module-level constants in ``mirage.core.*._client``, which is
why this module monkeypatches (see ``point_google_at`` / ``point_github_at``).

Also re-exports the serve/credential helpers from ``examples/_common/mockserver.py``, so every
example shares one ``--url`` / ``--user`` / ``--token`` behaviour and one local fallback.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common.mockserver import (  # noqa: E402
    google_oauth_user,
    serve_or_connect,
)

# aiohttp caches one verified SSL context at import (`connector._SSL_CONTEXT_VERIFIED`), and on
# macOS it has no CA bundle unless SSL_CERT_FILE was already set — so an HTTPS `--url` fails with
# CERTIFICATE_VERIFY_FAILED if mirage was imported before this module. Load certifi's CAs into that
# cached context too, so the redirect works whatever the import order.
try:  # best-effort; the SSL_CERT_FILE path _mockserver sets still applies if internals change
    import certifi as _certifi
    from aiohttp import connector as _aiohttp_connector

    _aiohttp_connector._SSL_CONTEXT_VERIFIED.load_verify_locations(_certifi.where())
except Exception:  # noqa: BLE001
    pass

__all__ = ["point_google_at", "point_github_at", "slack_base_url", "notion_base_url", "s3_base_url",
           "serve_or_connect", "google_oauth_user", "lines", "run_mirage", "FUSE_HELP"]


def slack_base_url(base_url: str) -> str:
    """The mock's Slack Web API base, for ``SlackConfig(base_url=...)``.

    NOT interchangeable with the same-named helper in ``using-llamaindex-readers``: that one ends in
    a slash because slack_sdk builds URLs as ``base_url + method``. Each client's URL-joining rule
    is its own, which is why these are per-example rather than shared."""
    return f"{base_url.rstrip('/')}/slack/api"


def notion_base_url(base_url: str) -> str:
    """The mock's Notion API base, for ``NotionConfig(base_url=...)``.

    mirage sends ``Notion-Version: 2022-06-28``, which the mock's version-aware router answers with
    the legacy inline-properties / ``databases.query`` shape."""
    return f"{base_url.rstrip('/')}/notion/v1"


def s3_base_url(base_url: str) -> str:
    """The mock's S3 endpoint, for ``S3Config(endpoint_url=...)``. Path-style: the bucket is the
    first path segment under ``/s3`` (S3Config(path_style=True) keeps it out of the hostname)."""
    return f"{base_url.rstrip('/')}/s3"


# Shown when a --fuse run can't mount, so the example exits with guidance instead of a traceback.
FUSE_HELP = (
    "FUSE mount unavailable ({err}).\n"
    "  1. pip install -e '.[mirage]'   (installs mirage-ai[fuse] → mfusepy)\n"
    "  2. install the OS FUSE driver: macFUSE (macOS, https://macfuse.io) or fuse3 (Linux).\n"
    "  Then re-run with --fuse. Without --fuse the example runs in-process (no driver needed)."
)


def lines(text: str) -> list[str]:
    """Split ``ls`` output into entries by line — names can contain spaces, so never ``split()``."""
    return [ln.rstrip() for ln in text.splitlines() if ln.strip()]


def run_mirage(coro):
    """Run a mirage coroutine with HTTP connection reuse.

    mirage opens a fresh ``ClientSession`` — so a new TCP + TLS handshake — for every API call, and
    it makes many (pagination, Gmail's per-message fetch). Against a remote ``--url`` that handshake
    dominated at ~0.85s/call; one shared keep-alive connector cut it ~3x. Harmless locally.
    """
    import aiohttp

    async def _run():
        shared = aiohttp.TCPConnector(limit=32, keepalive_timeout=60, ttl_dns_cache=300)
        original_init = aiohttp.ClientSession.__init__

        def _init(self, *args, **kwargs):  # route every session through the shared connector
            if kwargs.get("connector") is None:
                kwargs["connector"] = shared
                kwargs["connector_owner"] = False  # a per-call session must not close the pool
            original_init(self, *args, **kwargs)

        aiohttp.ClientSession.__init__ = _init
        try:
            return await coro
        finally:
            aiohttp.ClientSession.__init__ = original_init
            await shared.close()

    return asyncio.run(_run())


def _rebind_mirage_constants(source_module: str, overrides: dict[str, str]) -> None:
    """Rebind mirage's hardcoded API-host constants.

    Patches the source module — imported first so it exists — AND every already-imported
    ``mirage.core.*`` that copied a same-named constant by value, which is what makes the redirect
    order-independent. Idempotent.
    """
    importlib.import_module(source_module)
    for mod in list(sys.modules.values()):
        if not getattr(mod, "__name__", "").startswith("mirage.core."):
            continue
        for const, value in overrides.items():
            if hasattr(mod, const):
                setattr(mod, const, value)


def point_google_at(base_url: str) -> None:
    """Redirect mirage's Google connectors (Gmail/Drive/Docs/Sheets/Slides + OAuth) at the mock.

    Google exposes no host config, so the ``_client`` constants are patched directly. The consuming
    submodules import them BY VALUE (``from ..._client import GMAIL_API_BASE``), so both the source
    and every already-imported copy are rebound — that is what makes this order-independent.
    Idempotent; call once, before constructing the Google resources.
    """
    base = base_url.rstrip("/")
    _rebind_mirage_constants("mirage.core.google._client", {
        "TOKEN_URL": f"{base}/oauth2/token",
        "GMAIL_API_BASE": f"{base}/gmail/v1",
        "DRIVE_API_BASE": f"{base}/drive/v3",
        "DOCS_API_BASE": f"{base}/docs/v1",
        "SHEETS_API_BASE": f"{base}/sheets/v4",
        "SLIDES_API_BASE": f"{base}/slides/v1",
    })


def point_github_at(base_url: str) -> None:
    """Redirect mirage's GitHub connector (repo tree/contents/blobs) at the mock.

    One constant, ``_client.API_BASE``, read as a module global at call time — so unlike Google's,
    patching the source alone would do. The sweep is kept for symmetry, in case a future mirage
    version starts copying it by value. Call once, before constructing ``GitHubResource``: its
    constructor already makes HTTP calls (default branch, tree).
    """
    base = base_url.rstrip("/")
    _rebind_mirage_constants("mirage.core.github._client", {"API_BASE": f"{base}/github"})
