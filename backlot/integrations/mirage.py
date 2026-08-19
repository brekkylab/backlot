"""Point mirage's connectors at a Backlot server.

Mirage (https://github.com/strukto-ai/mirage) is a virtual filesystem for AI agents: you mount a
SaaS backend and read it with bash-style commands (``ls``, ``cat``, ``grep``, ``find``).

Slack, Notion and S3 take the host as a config field, so they need nothing from here — pass the
mock's URL for that resource straight in (``f"{base_url}/slack/api"``, ``…/notion/v1``, ``…/s3``).
GitHub reads its host from a module-level constant. Google offers a single-host override, but
Backlot needs a distinct prefix for each API. ``point_google_at`` / ``point_github_at`` therefore
rebind the corresponding constants in ``mirage.core.*._client``.
"""

from __future__ import annotations

import importlib
import sys

# aiohttp caches one verified SSL context at import (`connector._SSL_CONTEXT_VERIFIED`), and on
# macOS it has no CA bundle unless SSL_CERT_FILE was already set — so an HTTPS `--url` fails with
# CERTIFICATE_VERIFY_FAILED if mirage was imported before this module. Load certifi's CAs into that
# cached context too, so the redirect works whatever the import order.
try:  # best-effort; the SSL_CERT_FILE path callers set for their own client still applies if internals change
    import certifi as _certifi
    from aiohttp import connector as _aiohttp_connector

    _aiohttp_connector._SSL_CONTEXT_VERIFIED.load_verify_locations(_certifi.where())
except Exception:  # noqa: BLE001
    pass

__all__ = [
    "point_google_at",
    "point_github_at",
]


def _rebind_mirage_constants(source_module: str, overrides: dict[str, str]) -> None:
    """Rebind mirage's hardcoded API-host constants.

    Patches the source module — imported first so it exists — AND every already-imported
    ``mirage.core.*`` that copied a same-named constant by value, which is what makes the redirect
    order-independent. Idempotent.

    Raises if any name in ``overrides`` matched nothing. A renamed constant is the one failure this
    sweep cannot notice on its own: a moved MODULE already raises ``ImportError`` on the line below,
    but ``hasattr`` on a renamed constant is simply False everywhere, and the caller would go on to
    talk to the real vendor with whatever credentials it loaded.
    """
    importlib.import_module(source_module)
    patched: set[str] = set()
    for mod in list(sys.modules.values()):
        if not getattr(mod, "__name__", "").startswith("mirage.core."):
            continue
        for const, value in overrides.items():
            if hasattr(mod, const):
                setattr(mod, const, value)
                patched.add(const)
    if missing := sorted(set(overrides) - patched):
        raise RuntimeError(
            f"{', '.join(missing)} not found in {source_module} or any imported mirage.core.* "
            f"module — mirage's constants moved in an upgrade. Update this shim before the run "
            f"silently reaches the real vendor instead of the mock."
        )


def point_google_at(base_url: str) -> None:
    """Redirect mirage's Google connectors (Gmail/Drive/Docs/Sheets/Slides/Calendar/Forms + OAuth)
    at the mock.

    The constants are patched rather than configured, and the reason is NOT that mirage offers no
    host config -- it does. ``GoogleConfig.api_base`` exists and its own comment calls it a
    "single-host override for every Google API ... used to point backends at a fake server", and
    every base in ``_client`` consults it first. What rules it out here is the SINGLE: with one
    ``api_base``, mirage composes ``docs_base``, ``slides_base`` and ``forms_base`` as
    ``{base}/v1`` alike, and the mock serves docs at ``/docs/v1`` and slides at ``/slides/v1``, so
    three APIs would arrive on one prefix it cannot route apart. Per-constant rebinding is what
    keeps them distinguishable.

    The consuming submodules import them BY VALUE (``from ..._client import GMAIL_API_BASE``), so
    both the source and every already-imported copy are rebound — that is what makes this
    order-independent. Idempotent; call once, before constructing the Google resources.
    """
    base = base_url.rstrip("/")
    _rebind_mirage_constants(
        "mirage.core.google._client",
        {
            "TOKEN_URL": f"{base}/oauth2/token",
            "GMAIL_API_BASE": f"{base}/gmail/v1",
            "DRIVE_API_BASE": f"{base}/drive/v3",
            # Backlot serves no upload route, so bind it to the mock to make a write FAIL there
            # rather than succeed against real Drive.
            "DRIVE_UPLOAD_BASE": f"{base}/upload/drive/v3",
            "DOCS_API_BASE": f"{base}/docs/v1",
            "SHEETS_API_BASE": f"{base}/sheets/v4",
            "SLIDES_API_BASE": f"{base}/slides/v1",
            # Backlot serves neither Calendar nor Forms, so these two are here for the reason
            # DRIVE_UPLOAD_BASE is: mirage 0.0.5 added them, a caller who believed the whole client
            # was redirected would otherwise reach the real Google, and a 404 from the mock is the
            # failure that says so.
            "CALENDAR_API_BASE": f"{base}/calendar/v3",
            "FORMS_API_BASE": f"{base}/forms/v1",
        },
    )


def point_github_at(base_url: str) -> None:
    """Redirect mirage's GitHub connector (repo tree/contents/blobs) at the mock.

    One constant, ``_client.API_BASE``, read as a module global at call time — so unlike Google's,
    patching the source alone would do. The sweep is kept for symmetry, in case a future mirage
    version starts copying it by value. Call once, before constructing ``GitHubResource``: its
    constructor already makes HTTP calls (default branch, tree).
    """
    base = base_url.rstrip("/")
    _rebind_mirage_constants("mirage.core.github._client", {"API_BASE": f"{base}/github"})
