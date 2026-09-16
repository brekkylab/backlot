"""scripts/loop_env.py turns the loop's Parameter Store entries into the session's environment."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("loop_env", REPO / "scripts" / "loop_env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_lines_quote_for_bash_and_refuse_what_is_not_a_variable(tmp_path):
    loop_env = _load()
    lines = loop_env.export_lines(
        [
            {"Name": "/backlot-loop/SLACK_USER_TOKEN", "Value": "xoxp-1 it's $HOME `x`"},
            {"Name": "/backlot-loop/not-a-name", "Value": "x"},
            {"Name": "/backlot-loop/GITHUB_TOKEN", "Value": "x"},
            {"Name": "/backlot-loop/GH_TOKEN", "Value": "x"},
        ]
    )
    assert [line.split("=", 1)[0] for line in lines] == ["export SLACK_USER_TOKEN"]
    # The line is for bash to source, so bash is what decides whether the quoting held.
    env_file = tmp_path / "env"
    env_file.write_text("\n".join(lines) + "\n")
    out = subprocess.run(
        ["bash", "-c", f'source "{env_file}" && printf %s "$SLACK_USER_TOKEN"'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout == "xoxp-1 it's $HOME `x`"


def test_main_appends_to_the_env_file_and_does_nothing_without_one(tmp_path, monkeypatch):
    loop_env = _load()
    monkeypatch.setattr(
        loop_env, "fetch", lambda prefix: [{"Name": prefix + "LINEAR_API_KEY", "Value": "lin_k"}]
    )
    monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
    assert loop_env.main() == 0

    env_file = tmp_path / "env"
    env_file.write_text("export A=1\n")
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
    assert loop_env.main() == 0
    assert env_file.read_text() == "export A=1\nexport LINEAR_API_KEY=lin_k\n"
