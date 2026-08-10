"""backlot.cli: the `backlot` console script — dispatch, --type routing, and help.

The tests here call ``cli.main`` in-process. What they deliberately do NOT cover is whether the
console script is REGISTERED (`[project.scripts]` in pyproject.toml): an entry point only exists in
an installed distribution, so the wheel-install CI job runs the script itself. Both halves are
needed — a correct `cli.py` behind a missing entry point still leaves `backlot` not found.
"""

from __future__ import annotations

import json

import pytest

from backlot import cli
from backlot.importer import byo, erb


def _record() -> dict:
    return {
        "source_type": "slack",
        "channel": "general",
        "author_email": "ava@acme.com",
        "content": "Serving from the CLI.",
    }


# --- dispatch -------------------------------------------------------------------------------


def test_import_defaults_to_byo_and_loads_the_corpus(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(json.dumps(_record()))
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "data"))

    assert cli.main(["import", str(corpus)]) == 0
    assert (tmp_path / "data" / "mock.sqlite").exists()


def test_import_dry_run_validates_without_writing_a_db(tmp_path, monkeypatch, capsys):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(json.dumps(_record()))
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "data"))

    assert cli.main(["import", str(corpus), "--dry-run"]) == 0
    assert "OK: 1 records valid." in capsys.readouterr().out
    assert not (tmp_path / "data" / "mock.sqlite").exists()


def test_a_dry_run_of_an_invalid_corpus_exits_non_zero(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(json.dumps({"source_type": "slack"}))  # no content
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "data"))

    assert cli.main(["import", str(corpus), "--dry-run"]) == 1


@pytest.mark.parametrize("spelling", ["enterpriserag-bench", "erb"])
def test_type_routes_to_the_bench_importer_with_the_rest_of_the_argv(monkeypatch, spelling):
    """Both spellings reach erb.main, and --type is consumed rather than forwarded to it.

    Forwarding it would make the bench importer reject its own invocation with "unrecognized
    arguments: --type", which is the failure mode a pre-parser exists to avoid.
    """
    seen = {}
    monkeypatch.setattr(erb, "main", lambda argv, prog=None: seen.update(argv=argv, prog=prog) or 0)

    assert cli.main(["import", "--type", spelling, "--no-download", "--ref", "topic"]) == 0
    assert seen["argv"] == ["--no-download", "--ref", "topic"]
    assert seen["prog"] == "backlot import"


def test_the_byo_importer_never_sees_the_type_flag_either(monkeypatch):
    seen = {}
    monkeypatch.setattr(byo, "main", lambda argv, prog=None: seen.update(argv=argv, prog=prog) or 0)

    assert cli.main(["import", "-t", "byo", "corpus.jsonl", "--append"]) == 0
    assert seen["argv"] == ["corpus.jsonl", "--append"]
    assert seen["prog"] == "backlot import"


def test_an_unknown_type_is_a_usage_error():
    with pytest.raises(SystemExit) as e:
        cli.main(["import", "--type", "sharepoint", "corpus.jsonl"])
    assert e.value.code == 2


def test_serve_passes_its_arguments_through_to_uvicorn(monkeypatch):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))

    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "9999", "--log-level", "warning"]) == 0
    assert seen["app"] == "backlot.main:app"  # an import STRING, which --reload requires
    assert (seen["host"], seen["port"], seen["log_level"]) == ("0.0.0.0", 9999, "warning")


def test_serve_defaults_match_uvicorns_own(monkeypatch):
    """`backlot serve` is a shorter spelling of `uvicorn backlot.main:app`, so a bare invocation
    must not quietly change where it binds or whether it trusts X-Forwarded-* headers."""
    import inspect

    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    # Read from uvicorn's own signature, not a copy of its values: a default this repo restated
    # would go stale silently, which is the drift the test exists to catch.
    defaults = inspect.signature(uvicorn.Config.__init__).parameters

    assert cli.main(["serve"]) == 0
    for key in ("host", "port", "reload", "log_level", "proxy_headers", "forwarded_allow_ips"):
        assert seen[key] == defaults[key].default, key


def test_no_proxy_headers_is_the_only_way_to_turn_them_off(monkeypatch):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))

    assert cli.main(["serve", "--no-proxy-headers"]) == 0
    assert seen["proxy_headers"] is False


@pytest.mark.parametrize("argv", [[], ["frobnicate"]])
def test_no_command_or_an_unknown_one_is_a_usage_error(argv):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2


def test_version_reports_the_installed_distribution(capsys):
    from importlib.metadata import version

    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"backlot {version('backlot')}"


# --- help -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["import", "--help"], "--dry-run"),  # byo's own flags, since byo is the default
        (["import", "-t", "erb", "--help"], "--slice-questions"),  # the bench importer's
    ],
)
def test_import_help_shows_the_chosen_importers_flags_and_type_in_one_screen(
    argv, expected, capsys
):
    """One screen, not two: --help is answered against the SELECTED importer's parser with --type
    added to it, so a reader never has to know that two parsers are involved."""
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert expected in out
    assert "--type" in out
    assert out.startswith("usage: backlot import")


def test_an_importer_usage_error_names_the_command_the_user_typed(capsys):
    """`prog` has to reach the importer's parser: without it argparse reports the console script's
    own name ("usage: backlot ..."), which is not a command anyone can retype."""
    with pytest.raises(SystemExit) as e:
        cli.main(["import"])  # byo requires a corpus
    assert e.value.code == 2
    assert capsys.readouterr().err.startswith("usage: backlot import")


def test_running_a_module_directly_still_works(tmp_path, monkeypatch):
    """`python -m backlot.importer.byo` is not deprecated by the CLI — it is the same `main`."""
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(json.dumps(_record()))
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(tmp_path / "data"))

    assert byo.main([str(corpus), "--dry-run"]) == 0


def test_both_importers_expose_a_parser_the_cli_can_render():
    """The CLI builds `backlot import --help` from these, so a parser that moved back inside
    `main` would leave the help screen unable to list that importer's flags."""
    for module in (byo, erb):
        parser = module.build_parser(prog="backlot import")
        assert parser.prog == "backlot import"
        assert parser.format_help()
