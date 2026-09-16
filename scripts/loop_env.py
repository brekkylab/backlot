"""Export the agent loop's vendor credentials into a cloud session.

Every parameter under ``/backlot-loop/`` in AWS Systems Manager Parameter Store becomes one
``export NAME=value`` line appended to the file named by ``CLAUDE_ENV_FILE``, which Claude Code
sources for every Bash call of the session. The SessionStart hook in ``.claude/settings.json``
runs this in cloud sessions only, where the environment carries the three AWS variables and
nothing else.

A parameter whose leaf is not an environment-variable name is skipped, and so are
``GITHUB_TOKEN`` and ``GH_TOKEN``: either one replaces the platform's GitHub credential with its
literal value and every ``gh`` call in the session fails.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Iterable

PREFIX = "/backlot-loop/"
_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
_REFUSED = {"GITHUB_TOKEN", "GH_TOKEN"}


def export_lines(parameters: Iterable[dict]) -> list[str]:
    """``export NAME=value`` for each parameter, quoted for bash, in Parameter Store order."""
    lines = []
    for parameter in parameters:
        name = parameter["Name"].removeprefix(PREFIX)
        if not _NAME.fullmatch(name) or name in _REFUSED:
            continue
        lines.append(f"export {name}={shlex.quote(parameter['Value'])}")
    return lines


def fetch(prefix: str, region: str | None) -> list[dict]:
    """Every parameter directly under ``prefix``, decrypted, across every page.

    ``region`` is where the parameters live; ``None`` is the AWS default chain, which the
    measurement bucket's region also drives, so the two are separable when they differ.
    """
    import boto3

    paginator = boto3.client("ssm", region_name=region).get_paginator("get_parameters_by_path")
    pages = paginator.paginate(Path=prefix, Recursive=False, WithDecryption=True)
    return [p for page in pages for p in page["Parameters"]]


def main() -> int:
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        print("loop_env: CLAUDE_ENV_FILE is not set; nothing to write", file=sys.stderr)
        return 0
    region = os.environ.get("AWS_SSM_REGION") or None
    try:
        lines = export_lines(fetch(PREFIX, region))
    except Exception as exc:  # noqa: BLE001 — the hook's stderr is the only place this is seen
        print(f"loop_env: could not read {PREFIX} from Parameter Store: {exc}", file=sys.stderr)
        return 1
    with open(env_file, "a", encoding="utf-8") as handle:
        handle.writelines(line + "\n" for line in lines)
    print(f"loop_env: {len(lines)} variables exported from {PREFIX}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
