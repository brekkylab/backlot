"""Settings defaults must not depend on the package living inside a source checkout."""

from __future__ import annotations

from backlot.config import Settings


def test_data_dir_defaults_to_cwd_relative(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKLOT_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert Settings(_env_file=None).data_dir == tmp_path / "data"


def test_shared_settings_carry_no_dataset_specific_knobs():
    """`Settings` is what the server, the routers and the BYO loader all read. A field only one
    importer uses belongs beside that importer (erb.BenchSettings), or every layer ends up
    carrying one dataset's configuration."""
    assert not {"raw_dir", "dataset_repo", "employee_yaml"} & set(dir(Settings))


def test_env_var_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "elsewhere"))
    assert Settings(_env_file=None).data_dir == tmp_path / "elsewhere"
