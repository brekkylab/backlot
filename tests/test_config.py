"""Settings defaults must not depend on the package living inside a source checkout."""

from __future__ import annotations

from backlot.config import Settings


def test_data_dir_defaults_to_cwd_relative(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKLOT_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert Settings(_env_file=None).data_dir == tmp_path / "data"


def test_raw_dir_defaults_under_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKLOT_RAW_DIR", raising=False)
    monkeypatch.delenv("BACKLOT_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert Settings(_env_file=None).raw_dir == tmp_path / "data" / "raw"


def test_env_var_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "elsewhere"))
    assert Settings(_env_file=None).data_dir == tmp_path / "elsewhere"
