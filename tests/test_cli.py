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
import os
import re
from pathlib import Path

import pytest

from backlot import cli
from backlot.config import get_settings
from backlot.importer import byo, erb

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# The frame rich draws around an error or a help panel. Dropped because a message WRAPPED inside one
# puts these glyphs between its own words: at 80 columns the panel renders as
#     | ... downloads its own      |
#     | corpus; a path is only ... |
# which collapses to "its own | | corpus" and matches no substring anyone would think to assert.
_BOX = re.compile(r"[─-╿]")


def plain(captured: str) -> str:
    """Rendered CLI output -> the text a reader sees, on one line.

    Assertions have to go through this, for two reasons that both passed locally and failed in CI:

    - rich HIGHLIGHTS option names by styling each fragment separately, so with color on
      `--shard-records` reaches the buffer as
      ``'\\x1b[1;36m-\\x1b[0m\\x1b[1;36m-shard\\x1b[0m\\x1b[1;36m-records\\x1b[0m'`` and the
      contiguous substring is not there. GitHub Actions sets FORCE_COLOR; a dev shell does not.
    - a message longer than the terminal wraps inside its panel, and the frame lands between words.

    Reproduce either with ``FORCE_COLOR=1 COLUMNS=80 pytest``.
    """
    return " ".join(_BOX.sub(" ", _ANSI.sub("", captured)).split())


@pytest.fixture(autouse=True)
def _deterministic_rendering(monkeypatch):
    """Render help and errors the same way here as on a CI runner.

    80 columns on purpose, not a comfortable width: it is what a runner gives, and it is narrow
    enough that rich wraps most messages inside their panel. A wide setting here would hide every
    wrapping bug until CI found it, which is how the frame-between-words case above got through.
    """
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "80")


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
    """`backlot export`'s target — its own entry point, not a flag on `import`."""
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


def test_a_corpus_the_importer_rejects_reports_its_reason_and_exits_1(
    tmp_path, monkeypatch, capsys
):
    """The cli-to-importer seam, with NOTHING patched — the one path every other test here stubs out.

    The importers signal a bad corpus with `raise SystemExit("<message>")`, eleven places in byo.py
    alone. `main()` coercing that payload with `int()` turned the most common failure this tool has
    into `ValueError: invalid literal for int() ... 'line 2: invalid JSON: ...'` — the diagnostic
    still on screen, but as the payload of a traceback. Every routing test patches `byo.run`, so the
    hole was exactly here.
    """
    corpus = tmp_path / "bad.jsonl"
    corpus.write_text('{"source_type": "slack", "id": "x"}\nthis is not json\n')
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "data"))

    assert cli.main(["import", str(corpus)]) == 1
    err = plain(capsys.readouterr().err)
    assert "line 2" in err and "invalid JSON" in err
    assert "Traceback" not in err


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


def test_type_routes_to_the_bench_importer(spy_erb):
    """Importing the bench takes no options at all, so routing is the whole contract."""
    assert cli.main(["import", "--type", "enterpriserag-bench"]) == 0
    assert spy_erb == {}  # called, with nothing to pass


def test_byo_options_reach_the_byo_importer_typed(tmp_path, spy_byo):
    roster = tmp_path / "roster.yaml"
    id_map = tmp_path / "ids.json"
    assert (
        cli.main(
            ["import", "c.jsonl", "--append", "--roster", str(roster), "--id-map", str(id_map)]
        )
        == 0
    )
    assert spy_byo["corpus"] == Path("c.jsonl")  # a Path, not the string
    assert spy_byo["append"] is True
    assert spy_byo["dry_run"] is False
    assert spy_byo["roster"] == roster
    assert spy_byo["id_map"] == id_map


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
    """The message names the values that DO work, not just the one that does not — which is what
    someone reaching for a shorter spelling of `enterpriserag-bench` needs to read."""
    assert cli.main(["import", "-t", "nope"]) == 2
    err = plain(capsys.readouterr().err)
    assert "nope" in err
    for valid in cli.IMPORTER_TYPES:
        assert valid in err, valid


@pytest.mark.parametrize(
    "argv",
    [
        ["--dry-run"],
        ["--bundled"],
        ["--append"],
        # The ones that take a value have to be given one, or click stops at the missing argument
        # before the refusal this test is about.
        ["--roster", "roster.yaml"],
        ["--id-map", "ids.json"],
    ],
    ids=lambda argv: argv[0],
)
def test_a_byo_option_under_the_bench_type_is_refused(argv, capsys):
    """The bench importer has no options, so every one of `import`'s belongs to BYO. Giving one with
    the bench type is a real flag in the wrong place, worth saying rather than ignoring."""
    assert cli.main(["import", "-t", "enterpriserag-bench", *argv]) == 2
    err = plain(capsys.readouterr().err)
    assert argv[0] in err and "--type" in err


def test_a_corpus_path_under_the_bench_type_is_refused(capsys):
    """A path with the bench type reads as "import this file as the bench", which is not a thing:
    the bench downloads its own corpus."""
    assert cli.main(["import", "-t", "enterpriserag-bench", "some.jsonl"]) == 2
    assert "downloads its own corpus" in plain(capsys.readouterr().err)


def test_a_dry_run_with_an_id_map_is_a_parameter_conflict(tmp_path, capsys):
    """A validation pass assigns no ids, so there is nothing for the manifest to record. It answers
    like every other option conflict in this command — exit 2, with usage and the param named —
    rather than the exit 1 and bare line the importer's own guard raises."""
    assert cli.main(["import", "c.jsonl", "--dry-run", "--id-map", str(tmp_path / "ids.json")]) == 2
    err = plain(capsys.readouterr().err)
    assert "--id-map" in err and "--dry-run assigns none" in err
    assert "Usage" in err


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


def test_export_rejects_a_destination_it_cannot_create(tmp_path, monkeypatch, capsys):
    """Named, and BEFORE the ~1GB fetch. The destination was first touched inside `export_byo`, i.e.
    after the download, so a mistyped path cost the whole wait and then a pathlib traceback."""
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("")
    called = _spy(monkeypatch, erb, "run_export")

    assert cli.main(["export", str(not_a_dir / "out")]) == 2
    err = plain(capsys.readouterr().err)
    assert "cannot create" in err
    assert called == {}, "the exporter must not be reached, so nothing is downloaded"


def test_export_requires_a_destination(capsys):
    assert cli.main(["export"]) == 2
    assert "DIR" in plain(capsys.readouterr().err)


# --- serve ------------------------------------------------------------------------------------


def _write_corpus_schema(path) -> None:
    """A schema-valid corpus with no rows, which is all `serve`'s pre-flight looks at."""
    import sqlite3

    from backlot import store

    conn = sqlite3.connect(path)
    try:
        conn.executescript(store.SCHEMA)
    finally:
        conn.close()


@pytest.fixture
def a_data_dir_with_a_corpus(tmp_path, monkeypatch):
    """A data dir `serve` will accept.

    An empty file sufficed while the pre-flight only checked existence. It reads the schema now,
    which an empty file does not have — and that is the point of the check, so the fixture supplies a
    real schema rather than the check being loosened to admit a placeholder.
    """
    data = tmp_path / "data"
    data.mkdir()
    _write_corpus_schema(data / "mock.sqlite")
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


def test_an_invalid_log_level_is_refused_before_uvicorn_sees_it(
    monkeypatch, a_data_dir_with_a_corpus, capsys
):
    """Left as a free string, a typo travelled into uvicorn's own config and surfaced as
    `KeyError: 'bogus'` from `configure_logging` — a library the caller never named."""
    seen = _spy_uvicorn(monkeypatch)
    assert cli.main(["serve", "--log-level", "bogus"]) == 2
    assert "bogus" in plain(capsys.readouterr().err)
    assert seen == {}, "uvicorn must not be reached at all"


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


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ("random-bytes", "file is not a database"),
        ("truncated", "database disk image is malformed"),
        ("other-database", "not a Backlot corpus"),
    ],
)
def test_serve_refuses_a_corpus_it_cannot_read(damage, reason, tmp_path, monkeypatch, capsys):
    """Existence is not readability. A corrupt file passed the pre-flight, bound the port, and died
    in the lifespan with `sqlite3.DatabaseError` — the failure shape the pre-flight exists to remove.

    The three ways a file at that path is not a corpus, each with the message sqlite or the schema
    check actually produces for it, so the diagnosis and not just the refusal is pinned.
    """
    import sqlite3

    data = tmp_path / "data"
    data.mkdir()
    db = data / "mock.sqlite"
    if damage == "random-bytes":
        db.write_bytes(b"\x00\xff" * 2048)
    elif damage == "truncated":  # what an interrupted copy of a real corpus looks like
        whole = tmp_path / "whole.sqlite"
        _write_corpus_schema(whole)
        db.write_bytes(whole.read_bytes()[:8192])
    else:  # a valid database that is simply not this application's
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE unrelated (x)")
        conn.close()
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(data))

    assert cli.main(["serve"]) == 2
    err = plain(capsys.readouterr().err)
    assert "no usable corpus" in err
    assert reason in err  # names WHY, not just that it failed


def test_serve_refuses_to_start_without_a_corpus(tmp_path, monkeypatch, capsys):
    """Without this check uvicorn binds the port and prints its banner, then the lifespan dies on a
    missing file — which reads as a broken install rather than an empty data dir."""
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "empty"))
    assert cli.main(["serve"]) == 2
    err = plain(capsys.readouterr().err)
    assert "no usable corpus" in err
    assert "mock.sqlite is missing" in err  # the empty-dir case, distinct from a corrupt one
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


def test_a_rejected_command_does_not_apply_its_data_dir(tmp_path, monkeypatch):
    """`--data-dir` writes the environment and clears the settings cache, so applying it before the
    usage checks left a rejected call having changed where the NEXT one would read from — visible
    in-process, which is how `cli.main` is used from tests and from `python -m backlot`."""
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "keep-me"))

    # rejected: a BYO option under the bench type
    assert (
        cli.main(["import", "-t", "enterpriserag-bench", "--dry-run", "--data-dir", "/nope"]) == 2
    )
    assert os.environ["BACKLOT_DATA_DIR"] == str(tmp_path / "keep-me")

    # rejected: neither a path nor --bundled
    assert cli.main(["import", "--data-dir", "/nope"]) == 2
    assert os.environ["BACKLOT_DATA_DIR"] == str(tmp_path / "keep-me")


# --- status -----------------------------------------------------------------------------------


def test_status_reports_an_empty_data_dir_and_exits_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "empty"))
    assert cli.main(["status"]) == 1
    out = plain(capsys.readouterr().out)
    assert "none" in out
    assert "backlot import" in out  # tells you how to fix it


def test_status_reports_an_unreadable_corpus_rather_than_raising(tmp_path, monkeypatch, capsys):
    """A file at the corpus path that is not a corpus — a truncated copy, an interrupted import.
    Diagnosing the data dir is this command's entire job, so it cannot answer with sqlite's
    traceback."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "mock.sqlite").write_bytes(b"not a database")
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(data))

    assert cli.main(["status"]) == 1
    err = plain(capsys.readouterr().err)
    assert "unreadable" in err
    assert "backlot import" in err  # and how to get out of it


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
    for option in (
        "--type",
        "--data-dir",
        "--bundled",
        "--append",
        "--dry-run",
        "--roster",
        "--id-map",
    ):
        assert option in out, option
    assert "BYO corpus" in out  # the panel that says which importer they belong to


@pytest.mark.parametrize(
    "not_an_option", ["--slice-questions", "--tokens-only", "--export-byo", "--allow-excluded"]
)
def test_import_rejects_options_it_does_not_have(not_an_option, capsys):
    """None of these exist. A stale invocation carrying one must fail loudly — being accepted and
    ignored is how a caller believes they asked for something they did not get."""
    assert cli.main(["import", "-t", "enterpriserag-bench", not_an_option, "x"]) == 2
    assert "No such option" in plain(capsys.readouterr().err)


def test_the_module_spellings_re_enter_the_same_cli(monkeypatch, spy_byo):
    """`python -m backlot.importer.byo` is documented in CONTRIBUTING. It now dispatches through the
    CLI rather than parsing its own flags, so there is no second parser to drift."""
    assert cli.main(["import", "c.jsonl", "--append"]) == 0
    assert spy_byo["append"] is True
