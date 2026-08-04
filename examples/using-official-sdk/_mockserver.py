"""Re-export the shared example mock plumbing — see ``examples/_common/mockserver.py``.

A shim per directory rather than an extra import line in every script: the examples are run
directly (``python examples/using-official-sdk/x.py``), so only their own directory lands on ``sys.path``.
"""
import sys
from pathlib import Path

_EXAMPLES = Path(__file__).resolve().parent.parent
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

from _common.mockserver import (  # noqa: E402,F401
    ROOT,
    TOKEN,
    google_oauth_user,
    google_service_account_info,
    mock_server,
    serve_or_connect,
)
