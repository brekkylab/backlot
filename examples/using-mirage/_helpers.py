"""Example-runner plumbing shared by the mirage examples.

The base-URL helpers (``slack_base_url``, ``notion_base_url``, ``s3_base_url``) and the
monkeypatchers for Google/GitHub (``point_google_at`` / ``point_github_at``) live in
``backlot.integrations.mirage`` — import them from there. ``serve_or_connect`` lives in
``backlot`` itself. What's left here is plumbing that isn't API at all: FUSE-unavailable
guidance text and the shared-connector wrapper the scripts use for ``--url`` performance.
"""

from __future__ import annotations

import asyncio

__all__ = ["lines", "run_mirage", "FUSE_HELP"]


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
