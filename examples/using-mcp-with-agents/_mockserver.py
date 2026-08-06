"""Puts ``examples/`` on ``sys.path`` and re-exports ``_common.mockserver``.

Only a shim, and only because the examples are run directly — ``sys.path[0]`` is the script's own
directory, so ``_common`` is not importable without this. One file per directory beats the same
four lines in each of its scripts.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common.mockserver import *  # noqa: E402,F401,F403
