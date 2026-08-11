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
import re
from pathlib import Path

import pytest

from backlot import cli
from backlot.config import get_settings
from backlot.importer import byo, erb

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(captured: str) -> str:
    """Rendered CLI output -> the text a reader sees, on one line.

    Assertions have to go through this. rich HIGHLIGHTS option names by styling each fragment
    separately, so with color on, `--shard-records` reaches the buffer as
    ``'\\x1b[1;36m-\\x1b[0m\\x1b[1;36m-shard\\x1b[0m\\x1b[1;36m-records\\x1b[0m'`` and the
    contiguous substring is simply not there. GitHub Actions sets FORCE_COLOR, so a suite that asserts on raw
    output passes locally and fails only in CI — which is exactly what happened. Newlines collapse
    too, so a message wrapped by a narrow terminal still matches.
    """
    return " ".join(_ANSI.sub("", captured).split())


@pytest.fixture(autouse=True)
def _deterministic_rendering(monkeypatch):
    """Render help and errors plainly and at a fixed width, whatever the runner's environment.

    `plain()` above already makes the assertions robust; this makes the OUTPUT deterministic as
    well, so a failure message is readable instead of a wall of escape codes.
    """
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "200")


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


def _spy(monkeypatch, module, name: str = "run") -> dict:
    """Replace ``module.<name>`` with a recorder that BINDS the call to the real signature.

    Binding matters: a bare ``**kw`` recorder accepts any keyword, so a caller passing
    ``export_byo=`` to a ``run(export_byo_dir=...)`` would be recorded happily here and TypeError
    only in production. ``Signature.bind`` reproduces that TypeError in the test instead.
    """
    import inspect

    real = inspect.signature(getattr(module, name))
    seen: dict = {}

    def recorder(*args, **kw):
        bound = real.bind(*args, **kw)
        bound.apply_defaults()
        seen.update(bound.arguments)
        return 0

    monkeypatch.setattr(module, name, recorder)
    return seen


@pytest.fixture
def spy_byo(monkeypatch):
    """Capture the keyword arguments the BYO importer is driven with."""
    return _spy(monkeypatch, byo)


@pytest.fixture
def spy_erb(monkeypatch):
    return _spy(monkeypatch, erb)


@pytest.fixture
def spy_erb_export(monkeypatch):
    """`backlot export`'s target — a separate entry point from the import one it used to be a flag on."""
    return _spy(monkeypatch, erb, "run_export")


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
    assert "OK: 1 records valid." in plain(capsys.readouterr().out)
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
    """Importing the bench takes no options at all, so routing is the whole contract."""
    assert cli.main(["import", "--type", spelling]) == 0
    assert spy_erb == {}  # called, with nothing to pass


def test_byo_options_reach_the_byo_importer_typed(tmp_path, spy_byo):
    roster = tmp_path / "roster.yaml"
    assert cli.main(["import", "c.jsonl", "--append", "--roster", str(roster)]) == 0
    assert spy_byo["corpus"] == Path("c.jsonl")  # a Path, not the string
    assert spy_byo["append"] is True
    assert spy_byo["dry_run"] is False
    assert spy_byo["roster"] == roster


def test_the_bundled_flag_resolves_to_the_packaged_corpus_path(spy_byo):
    from backlot.testing import HELLO_CORPUS

    assert cli.main(["import", "--bundled"]) == 0
    assert spy_byo["corpus"] == HELLO_CORPUS


# --- validation -------------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["import"], ["import", "--bundled", "some.jsonl"]])
def test_a_corpus_path_and_bundled_are_mutually_required(argv, capsys):
    assert cli.main(argv) == 2
    assert "--bundled" in plain(capsys.readouterr().err)


def test_an_unknown_type_is_a_usage_error(capsys):
    assert cli.main(["import", "-t", "nope"]) == 2
    assert "nope" in plain(capsys.readouterr().err)


@pytest.mark.parametrize("flag", ["--dry-run", "--bundled", "--append"])
def test_a_byo_option_under_the_bench_type_is_refused(flag, capsys):
    """The bench importer has no options, so every one of `import`'s belongs to BYO. Giving one with
    `--type erb` is a real flag in the wrong place, which is worth saying rather than ignoring."""
    assert cli.main(["import", "-t", "erb", flag]) == 2
    err = plain(capsys.readouterr().err)
    assert flag in err and "--type" in err


def test_a_corpus_path_under_the_bench_type_is_refused(capsys):
    """`import -t erb some.jsonl` reads as "import this file as the bench", which is not a thing:
    the bench downloads its own corpus."""
    assert cli.main(["import", "-t", "erb", "some.jsonl"]) == 2
    assert "downloads its own corpus" in plain(capsys.readouterr().err)


def test_shard_records_must_be_at_least_one(capsys):
    """0 makes `n >= shard_records` always true: one shard per record, 600k files for the bench."""
    assert cli.main(["export", "out", "--shard-records", "0"]) == 2
    assert "at least 1" in plain(capsys.readouterr().err)


# --- export -----------------------------------------------------------------------------------


def test_export_routes_to_the_bench_exporter(tmp_path, spy_erb_export):
    out = tmp_path / "artifact"
    assert cli.main(["export", str(out), "--shard-records", "50000"]) == 0
    assert spy_erb_export["out_dir"] == out
    assert spy_erb_export["shard_records"] == 50000  # an int, not "50000"


def test_export_without_sharding_writes_one_corpus_file(tmp_path, spy_erb_export):
    out = tmp_path / "artifact"
    assert cli.main(["export", str(out)]) == 0
    assert spy_erb_export["shard_records"] is None


def test_export_requires_a_destination(capsys):
    assert cli.main(["export"]) == 2
    assert "DIR" in plain(capsys.readouterr().err)


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
    err = plain(capsys.readouterr().err)
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
    out = plain(capsys.readouterr().out)
    assert "none" in out
    assert "backlot import" in out  # tells you how to fix it


def test_status_reports_the_loaded_corpus(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "data"))
    assert cli.main(["import", "--bundled"]) == 0
    capsys.readouterr()

    assert cli.main(["status"]) == 0
    out = plain(capsys.readouterr().out)
    assert "slack" in out and "fireflies" in out
    assert "tokens" in out


# --- top level --------------------------------------------------------------------------------


def test_an_unknown_command_is_a_usage_error(capsys):
    assert cli.main(["frobnicate"]) == 2
    assert "frobnicate" in plain(capsys.readouterr().err)


def test_no_arguments_prints_the_command_list(capsys):
    """Exit stays 2 — a bare invocation is still a usage error — but the output is the help screen
    rather than a one-line complaint, so the commands are discoverable without `--help`."""
    assert cli.main([]) == 2
    out = plain(capsys.readouterr().out)
    for command in ("serve", "import", "status"):
        assert command in out


def test_version_reports_the_installed_distribution(capsys):
    from importlib.metadata import version

    assert cli.main(["--version"]) == 0
    assert plain(capsys.readouterr().out) == f"backlot {version('backlot')}"


def test_help_shows_every_command(capsys):
    assert cli.main(["--help"]) == 0
    out = plain(capsys.readouterr().out)
    for command in ("serve", "import", "status"):
        assert command in out


def test_import_help_shows_every_option_it_accepts(capsys):
    """The reason this refactor exists: every option `import` accepts is declared in cli.py and
    visible at once. Before, they were split across two importers' argparse parsers."""
    assert cli.main(["import", "--help"]) == 0
    out = plain(capsys.readouterr().out)
    for option in ("--type", "--data-dir", "--bundled", "--append", "--dry-run", "--roster"):
        assert option in out, option
    assert "BYO corpus" in out  # the panel that says which importer they belong to


@pytest.mark.parametrize(
    "not_an_option", ["--slice-questions", "--tokens-only", "--export-byo", "--allow-excluded"]
)
def test_import_rejects_options_it_does_not_have(not_an_option, capsys):
    """None of these exist. A stale invocation carrying one must fail loudly — being accepted and
    ignored is how a caller believes they asked for something they did not get."""
    assert cli.main(["import", "-t", "erb", not_an_option, "x"]) == 2
    assert "No such option" in plain(capsys.readouterr().err)


def test_the_module_spellings_re_enter_the_same_cli(monkeypatch, spy_byo):
    """`python -m backlot.importer.byo` is documented in CONTRIBUTING. It now dispatches through the
    CLI rather than parsing its own flags, so there is no second parser to drift."""
    assert cli.main(["import", "c.jsonl", "--append"]) == 0
    assert spy_byo["append"] is True
