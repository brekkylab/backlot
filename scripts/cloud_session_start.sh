#!/bin/bash
# What a cloud session needs before Claude starts working, run by the SessionStart hook in
# .claude/settings.json. A local session exits at once: the developer's own environment is theirs.
#
# The environment's setup script runs outside the clone and is only for provisioning the VM, so
# the project's own install happens here, with `uv`'s cache warmed by that script so it takes
# seconds rather than minutes. The loop's vendor credentials follow, when the session carries the
# AWS key that can read them.
set -eu
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
cd "$CLAUDE_PROJECT_DIR"
uv sync --all-extras --quiet
# The HubSpot reader's own pin is behind llama-index; CI installs it past that pin the same way.
uv pip install --quiet --no-deps "llama-index-readers-hubspot<0.6"
if [ -n "${AWS_ACCESS_KEY_ID:-}" ]; then
  uv run --no-sync python scripts/loop_env.py
fi
