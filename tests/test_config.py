"""Settings defaults must not depend on the package living inside a source checkout, and
`settings_put_back` hands back on exit the settings `get_settings` holds on entry."""

from __future__ import annotations

import pytest

from backlot.config import Settings, get_settings
from tests._helpers import settings_put_back


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


@pytest.mark.parametrize(
    "read_on_entry,expected", [(False, ("exit", 100)), (True, ("entry", 7))], ids=["unread", "read"]
)
def test_settings_put_back_hands_back_what_get_settings_held_on_entry(
    monkeypatch, read_on_entry, expected
):
    monkeypatch.setenv("BACKLOT_ORG_NAME", "entry")
    monkeypatch.setenv("BACKLOT_DEFAULT_PAGE_SIZE", "7")
    get_settings.cache_clear()
    try:
        if read_on_entry:
            get_settings()
        with settings_put_back():
            get_settings.cache_clear()
            get_settings().org_name = "inside"
            monkeypatch.setenv("BACKLOT_ORG_NAME", "exit")
            monkeypatch.delenv("BACKLOT_DEFAULT_PAGE_SIZE")
        settings = get_settings()
        assert (settings.org_name, settings.default_page_size) == expected
    finally:
        get_settings.cache_clear()
