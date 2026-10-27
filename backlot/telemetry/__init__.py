"""Anonymous usage telemetry: which vendor operations a running Backlot server is asked for.

docs/telemetry.md is the contract: what is sent, what never is, and how to turn it off.

Nothing here runs at ``import backlot``: the server's lifespan starts the exporter, and the
middleware is installed by ``backlot.main``.
"""

from __future__ import annotations

from backlot.config import Settings

DOCS = "https://github.com/brekkylab/backlot/blob/main/docs/telemetry.md"
# The middleware does not count a request carrying this header, on any route and with any value.
# `backlot mcp` sends it on the introspection it reads to build its Linear and Fireflies tools.
INTERNAL = "X-Backlot-Internal"


def notice(endpoint: str) -> str:
    """What a server that sends telemetry prints to stderr when it starts."""
    return (
        f"Backlot sends anonymous usage counts to {endpoint}:\n"
        "  which vendor operations are called, which ones are asked for that Backlot does not\n"
        "  serve yet, and where answers fail. They decide what we build and fix next, so the\n"
        "  APIs you rely on are the ones that get better first.\n"
        "  Never sent: paths, queries, bodies, tokens, or anything from your corpus.\n"
        "  Turn it off with --disable-telemetry, BACKLOT_TELEMETRY=0 or DO_NOT_TRACK=1.\n"
        f"  Why and what: {DOCS}"
    )


def enabled(settings: Settings) -> bool:
    """Whether this process sends telemetry: on unless ``BACKLOT_TELEMETRY`` is false or
    ``DO_NOT_TRACK``, the convention many developer tools honour, is set to any non-empty value but
    ``0`` or ``false``."""
    return settings.telemetry and settings.do_not_track.strip().lower() in ("", "0", "false")
