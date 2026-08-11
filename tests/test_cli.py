"""backlot.cli: the `backlot` console script — dispatch, --type routing, validation and help.

The tests here call ``cli.main`` in-process, which is everything except one thing: whether the
console script is REGISTERED. `[project.scripts]` only takes effect in an installed distribution, so
a correct `cli.py` behind a missing or misspelled entry point still leaves `backlot` not found, and
nothing here would notice. Check that by hand when touching packaging — build a wheel, install it
into a venv outside the checkout, and run `backlot --version` from a directory the source tree
cannot shadow.

Routing is asserted by patching each importer's ``run`` and reading the KEYWORD ARGUMENTS it was
handed. The argparse-era tests could only assert that argv strings were forwarded verbatim, which
said nothing about whether a flag was interpreted correctly — `--shard-records 5` arriving as the
string "5" would have passed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backlot import cli
from backlot.config import get_settings
from backlot.importer import byo, erb


@pytest.fixture(autouse=True)
def _fresh_settings():
    """`get_settings` is lru_cached, so a test that points BACKLOT_DATA_DIR at its own tmp_path is
    otherwise served the FIRST test's directory and writes its corpus there. Clear on the way in and
    again on the way out, the same contract tests/_helpers.py keeps for the HTTP tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _record() -> dict:
    return {
        "source_type": "slack",
        "channel": "general",
        "author_email": "ava@acme.com",
        "content": "Serving from the CLI.",
    }


def _spy(monkeypatch, module) -> dict:
    """Replace ``module.run`` with a recorder that BINDS the call to the real signature.

    Binding matters: a bare ``**kw`` recorder accepts any keyword, so a caller passing
    ``export_byo=`` to a ``run(export_byo_dir=...)`` would be recorded happily here and TypeError
    only in production. ``Signature.bind`` reproduces that TypeError in the test instead.
    """
    import inspect

    real = inspect.signature(module.run)
    seen: dict = {}

    def recorder(*args, **kw):
        bound = real.bind(*args, **kw)
        bound.apply_defaults()
        seen.update(bound.arguments)
        return 0

    monkeypatch.setattr(module, "run", recorder)
    return seen


@pytest.fixture
def spy_byo(monkeypatch):
    """Capture the keyword arguments the BYO importer is driven with."""
    return _spy(monkeypatch, byo)


@pytest.fixture
def spy_erb(monkeypatch):
    return _spy(monkeypatch, erb)


# --- the importers actually run ---------------------------------------------------------------


def test_import_defaults_to_byo_and_loads_the_corpus(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(json.dumps(_record()) + "\n")
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "data"))

    assert cli.main(["import", str(corpus)]) == 0
    assert (tmp_path / "data" / "mock.sqlite").exists()


def test_import_dry_run_validates_without_writing_a_db(tmp_path, monkeypatch, capsys):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(json.dumps(_record()) + "\n")
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "data"))

    assert cli.main(["import", str(corpus), "--dry-run"]) == 0
    assert "OK: 1 records valid." in capsys.readouterr().out
    assert not (tmp_path / "data" / "mock.sqlite").exists()


def test_a_dry_run_of_an_invalid_corpus_exits_non_zero(tmp_path, monkeypatch):
    corpus = tmp_path / "bad.jsonl"
    corpus.write_text(json.dumps({"source_type": "slack"}) + "\n")  # no content
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "data"))

    assert cli.main(["import", str(corpus), "--dry-run"]) == 1


def test_import_bundled_loads_the_corpus_bundled_with_the_package(tmp_path, monkeypatch):
    import sqlite3

    from backlot import store

    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "data"))
    assert cli.main(["import", "--bundled"]) == 0

    conn = sqlite3.connect(tmp_path / "data" / "mock.sqlite")
    try:
        for src in store.SOURCE_TABLE:
            n = conn.execute(f"SELECT COUNT(*) FROM {store.table(src)}").fetchone()[0]
            assert n > 0, src
    finally:
        conn.close()


# --- routing and how options arrive -----------------------------------------------------------


@pytest.mark.parametrize("spelling", ["enterpriserag-bench", "erb"])
def test_type_routes_to_the_bench_importer(spelling, spy_erb):
    assert cli.main(["import", "--type", spelling, "--no-download", "--ref", "topic"]) == 0
    assert spy_erb["no_download"] is True
    assert spy_erb["ref"] == "topic"


def test_byo_options_reach_the_byo_importer_typed(tmp_path, spy_byo):
    roster = tmp_path / "roster.yaml"
    assert cli.main(["import", "c.jsonl", "--append", "--roster", str(roster)]) == 0
    assert spy_byo["corpus"] == Path("c.jsonl")  # a Path, not the string
    assert spy_byo["append"] is True
    assert spy_byo["dry_run"] is False
    assert spy_byo["roster"] == roster


def test_bench_numeric_options_arrive_as_ints(tmp_path, spy_erb):
    out = tmp_path / "out"
    argv = ["import", "-t", "erb", "--export-byo", str(out), "--shard-records", "50000"]
    assert cli.main([*argv, "--allow-excluded", "3"]) == 0
    assert spy_erb["shard_records"] == 50000  # not "50000"
    assert spy_erb["allow_excluded"] == 3
    assert spy_erb["export_byo_dir"] == out


def test_the_bundled_flag_resolves_to_the_packaged_corpus_path(spy_byo):
    from backlot.testing import HELLO_CORPUS

    assert cli.main(["import", "--bundled"]) == 0
    assert spy_byo["corpus"] == HELLO_CORPUS


# --- validation -------------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["import"], ["import", "--bundled", "some.jsonl"]])
def test_a_corpus_path_and_bundled_are_mutually_required(argv, capsys):
    assert cli.main(argv) == 2
    assert "--bundled" in capsys.readouterr().err


def test_an_unknown_type_is_a_usage_error(capsys):
    assert cli.main(["import", "-t", "nope"]) == 2
    assert "nope" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "flag"),
    [
        (["import", "-t", "erb", "--dry-run"], "--dry-run"),
        (["import", "-t", "erb", "--bundled"], "--bundled"),
        (["import", "-t", "erb", "--append"], "--append"),
        (["import", "c.jsonl", "--no-download"], "--no-download"),
        (["import", "c.jsonl", "--tokens-only"], "--tokens-only"),
        (["import", "c.jsonl", "--allow-excluded", "2"], "--allow-excluded"),
    ],
)
def test_an_option_belonging_to_the_other_importer_is_refused(argv, flag, capsys):
    """One command accepting both importers' options makes these typable. argparse could not reach
    this case — the two parsers were disjoint — and the flag would otherwise be dropped in silence.
    """
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert flag in err and "--type" in err


def test_shard_records_must_be_at_least_one(capsys):
    """0 makes `n >= shard_records` always true: one shard per record, 600k files for the bench."""
    assert cli.main(["import", "-t", "erb", "--export-byo", "out", "--shard-records", "0"]) == 2
    assert "at least 1" in capsys.readouterr().err


def test_shard_records_without_export_byo_is_refused(capsys):
    """Silently ignored before: a sharded export was asked for and a database got built instead."""
    assert cli.main(["import", "-t", "erb", "--shard-records", "5"]) == 2
    assert "--export-byo" in capsys.readouterr().err


# --- serve ------------------------------------------------------------------------------------


@pytest.fixture
def a_data_dir_with_a_corpus(tmp_path, monkeypatch):
    """`serve` refuses to start without a corpus, so its tests need one to exist."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "mock.sqlite").write_bytes(b"")
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(data))
    return data


def _spy_uvicorn(monkeypatch) -> dict:
    import uvicorn

    seen: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    return seen


def _uvicorn_defaults() -> dict:
    """uvicorn.run's own defaults, read BEFORE the spy replaces the function — otherwise the
    signature under inspection is the spy's."""
    import inspect

    import uvicorn

    return inspect.signature(uvicorn.run).parameters


def test_serve_passes_its_arguments_through_to_uvicorn(monkeypatch, a_data_dir_with_a_corpus):
    seen = _spy_uvicorn(monkeypatch)
    argv = ["serve", "--host", "0.0.0.0", "--port", "9999", "--log-level", "warning"]
    assert cli.main(argv) == 0
    assert seen["app"] == "backlot.main:app"  # an import STRING, which --reload requires
    assert (seen["host"], seen["port"], seen["log_level"]) == ("0.0.0.0", 9999, "warning")


def test_serve_defaults_match_uvicorns_own(monkeypatch, a_data_dir_with_a_corpus):
    """The point of `backlot serve` is being a shorter spelling of `python -m uvicorn`, not a second
    set of defaults to keep in step with it."""
    defaults = _uvicorn_defaults()  # before the spy, or this inspects the spy
    seen = _spy_uvicorn(monkeypatch)
    assert cli.main(["serve"]) == 0
    for key in ("host", "port", "proxy_headers", "reload"):
        assert seen[key] == defaults[key].default, key


def test_no_proxy_headers_is_the_only_way_to_turn_them_off(monkeypatch, a_data_dir_with_a_corpus):
    seen = _spy_uvicorn(monkeypatch)
    assert cli.main(["serve", "--no-proxy-headers"]) == 0
    assert seen["proxy_headers"] is False


def test_serve_refuses_to_start_without_a_corpus(tmp_path, monkeypatch, capsys):
    """Without this check uvicorn binds the port and prints its banner, then the lifespan dies on a
    missing file — which reads as a broken install rather than an empty data dir."""
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "empty"))
    assert cli.main(["serve"]) == 2
    err = capsys.readouterr().err
    assert "no corpus" in err
    assert "backlot import --bundled" in err  # the way out, not just the complaint


# --- --data-dir -------------------------------------------------------------------------------


def test_data_dir_overrides_the_environment(tmp_path, monkeypatch):
    """The flag has to win over BACKLOT_DATA_DIR, or the two ways to say it would disagree."""
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "from-env"))
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(json.dumps(_record()) + "\n")

    assert cli.main(["import", str(corpus), "--data-dir", str(tmp_path / "from-flag")]) == 0
    assert (tmp_path / "from-flag" / "mock.sqlite").exists()
    assert not (tmp_path / "from-env").exists()


# --- status -----------------------------------------------------------------------------------


def test_status_reports_an_empty_data_dir_and_exits_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "empty"))
    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out
    assert "none" in out
    assert "backlot import" in out  # tells you how to fix it


def test_status_reports_the_loaded_corpus(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "data"))
    assert cli.main(["import", "--bundled"]) == 0
    capsys.readouterr()

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "slack" in out and "fireflies" in out
    assert "tokens" in out


# --- top level --------------------------------------------------------------------------------


def test_an_unknown_command_is_a_usage_error(capsys):
    assert cli.main(["frobnicate"]) == 2
    assert "frobnicate" in capsys.readouterr().err


def test_no_arguments_prints_the_command_list(capsys):
    """Exit stays 2 — a bare invocation is still a usage error — but the output is the help screen
    rather than a one-line complaint, so the commands are discoverable without `--help`."""
    assert cli.main([]) == 2
    out = capsys.readouterr().out
    for command in ("serve", "import", "status"):
        assert command in out


def test_version_reports_the_installed_distribution(capsys):
    from importlib.metadata import version

    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"backlot {version('backlot')}"


def test_help_shows_every_command(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for command in ("serve", "import", "status"):
        assert command in out


def test_import_help_shows_both_importers_options_in_one_screen(capsys):
    """The reason this refactor exists: every option `import` accepts is declared in cli.py and
    visible at once, grouped by the importer it drives. Before, the bench options were unreachable
    from the help text until you already knew to pass `-t erb`."""
    assert cli.main(["import", "--help"]) == 0
    out = capsys.readouterr().out
    for byo_option in ("--bundled", "--append", "--dry-run", "--roster"):
        assert byo_option in out, byo_option
    for bench_option in ("--slice-questions", "--export-byo", "--shard-records", "--tokens-only"):
        assert bench_option in out, bench_option
    assert "BYO corpus" in out and "EnterpriseRAG-Bench" in out  # the two panels


def test_the_module_spellings_re_enter_the_same_cli(monkeypatch, spy_byo):
    """`python -m backlot.importer.byo` is documented in CONTRIBUTING. It now dispatches through the
    CLI rather than parsing its own flags, so there is no second parser to drift."""
    assert cli.main(["import", "c.jsonl", "--append"]) == 0
    assert spy_byo["append"] is True
