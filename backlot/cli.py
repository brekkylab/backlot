"""The ``backlot`` console script — every command and every option it accepts, declared here.

    backlot serve                       # uvicorn backlot.main:app, with uvicorn's own defaults
    backlot import <corpus.jsonl>       # backlot.importer.byo   (--type byo, the default)
    backlot import --type erb           # backlot.importer.erb, no options of its own
    backlot export out/                 # the bench as a BYO artifact instead of a database
    backlot status                      # what the data dir currently holds

This module is the ONE place the command line is defined. The importers used to declare their own
flags in their own ``argparse`` parsers, which meant ``backlot import --help`` was assembled from
three files and reading this one told you almost nothing. Each importer now exposes plain functions
taking keyword arguments, and the flags that drive them are the parameters below.

``import`` keeps both corpus types behind one command (``--type``) because the bench importer has no
options left to separate: writing an artifact became ``export``, and the rest went. So every option
on ``import`` is BYO's, and one given under ``--type erb`` is refused rather than ignored — see
``_reject_byo_flags_under_bench``.
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Optional

import typer

# The importers and uvicorn are imported inside each command, not here: `serve` must not pay for
# `backlot.importer.erb` (2,400 lines) and `import` must not pull in uvicorn.

# --type value -> the importer it selects. `erb` is accepted beside the full bench name because that
# is what the module, the tests and every existing doc call it.
BYO, BENCH, BENCH_ALIAS = "byo", "enterpriserag-bench", "erb"
IMPORTER_TYPES = (BYO, BENCH, BENCH_ALIAS)

_BYO_PANEL = "BYO corpus options (--type byo)"

app = typer.Typer(
    name="backlot",
    # Bare `backlot` prints the command list instead of a usage error. A CLI whose whole surface is
    # three commands has nothing to gain from making you type `--help` to see them.
    no_args_is_help=True,
    add_completion=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Enterprise SaaS read APIs (Slack, Gmail, Drive, GitHub, Jira, Confluence, Notion, S3, "
    "HubSpot, Linear, Fireflies) over your own corpus, with per-document ACLs.",
    epilog="Run `backlot <command> --help` for a command's own options.",
)


def _version() -> str:
    try:
        return version("backlot")
    except PackageNotFoundError:  # a source tree that was never installed
        return "unknown"


def _use_data_dir(data_dir: Path | None) -> None:
    """Point this run's settings at ``data_dir``.

    Written through the environment and the settings cache rather than by passing a path down every
    call: ``Settings`` reads ``BACKLOT_DATA_DIR`` and ``get_settings`` is ``lru_cache``d, so this is
    the same mechanism the env var itself uses — and the same one the test suite uses to aim a run at
    a tmp dir — instead of a second way for a data dir to arrive.
    """
    if data_dir is None:
        return
    from backlot.config import get_settings

    os.environ["BACKLOT_DATA_DIR"] = str(data_dir)
    get_settings.cache_clear()


DataDir = Annotated[
    Optional[Path],
    typer.Option(
        "--data-dir",
        metavar="DIR",
        help="where the corpus lives (overrides BACKLOT_DATA_DIR) — how you keep several "
        "corpora side by side  [default: ./data]",
        rich_help_panel="Common",
    ),
]


@app.callback(invoke_without_command=True)
def _root(
    version_: Annotated[
        bool,
        typer.Option("--version", help="show the installed version and exit", is_eager=True),
    ] = False,
) -> None:
    if version_:
        typer.echo(f"backlot {_version()}")
        raise typer.Exit()


# --------------------------------------------------------------------------- serve


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="bind address")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="bind port")] = 8000,
    # "--reload" spelled out, not left to typer: a bool option with no explicit name renders as a
    # `--reload/--no-reload` pair, and `--no-reload` is a flag this CLI never had.
    reload: Annotated[
        bool, typer.Option("--reload", help="restart on source changes (development)")
    ] = False,
    log_level: Annotated[
        Optional[str],
        typer.Option(
            help="uvicorn log level: critical|error|warning|info|debug|trace  [default: info]"
        ),
    ] = None,
    # Behind a TLS-terminating proxy/ALB, proxy headers make the app honour X-Forwarded-Proto/Host
    # and emit https self-URLs, which clients that follow returned URLs (PyGithub) need. uvicorn has
    # them on by default, so the flag that carries weight is the negative one.
    proxy_headers: Annotated[
        bool,
        typer.Option(
            "--proxy-headers/--no-proxy-headers",
            help="honour X-Forwarded-* headers (uvicorn's default is on)",
        ),
    ] = True,
    forwarded_allow_ips: Annotated[
        Optional[str],
        typer.Option(
            metavar="IPS",
            help="comma-separated proxy IPs to trust X-Forwarded-* from, or * for any "
            "[default: 127.0.0.1]",
        ),
    ] = None,
    data_dir: DataDir = None,
) -> None:
    """Serve the mock APIs over the corpus in the data dir.

    Every default here is uvicorn's own, so this is a shorter spelling of `python -m uvicorn backlot.main:app` rather than a second set of behaviour to keep in step with it.
    """  # noqa: E501 — one source line per paragraph: typer renders a docstring newline as a line break, so a wrapped paragraph reaches the help screen broken mid-sentence.
    _use_data_dir(data_dir)
    from backlot.config import get_settings

    settings = get_settings()
    # Checked before uvicorn starts: without it the process prints its startup banner, binds the
    # port, and only then fails inside the lifespan on a missing file — which reads as a broken
    # install rather than an empty data dir.
    if not settings.db_path.exists():
        typer.echo(
            f"no corpus in {settings.data_dir} ({settings.db_path.name} is missing).\n"
            f"Build one first:  backlot import --bundled     # the corpus shipped in the package\n"
            f"                  backlot import <corpus.jsonl>",
            err=True,
        )
        raise typer.Exit(2)

    import uvicorn

    # The app is passed as an import STRING because that is what --reload requires.
    uvicorn.run(
        "backlot.main:app",
        host=host,
        port=port,
        reload=reload,
        log_level=log_level,
        proxy_headers=proxy_headers,
        forwarded_allow_ips=forwarded_allow_ips,
    )


# --------------------------------------------------------------------------- import

# The BYO options, and the default that means "not given". Kept as data so the check below names the
# offending flags in the user's own spelling. There is no bench counterpart: importing the bench
# takes no options at all, which is why every one of these is refused under `--type erb`.
_BYO_ONLY: dict[str, object] = {
    "--bundled": False,
    "--append": False,
    "--dry-run": False,
    "--roster": None,
}


def _reject_byo_flags_under_bench(supplied: dict[str, object]) -> None:
    """Fail when a BYO option is given with `--type erb`, which has no options of its own.

    Named explicitly because "unrecognized argument" is not what happened — the flag is real, just
    not for this importer — and because the alternative is ignoring it in silence.
    """
    offenders = [flag for flag, unset in _BYO_ONLY.items() if supplied[flag] != unset]
    if not offenders:
        return
    raise typer.BadParameter(
        f"{', '.join(offenders)} {'belongs' if len(offenders) == 1 else 'belong'} to "
        f"--type {BYO}, and --type {BENCH} is in effect"
    )


@app.command("import")
def import_(
    corpus: Annotated[
        Optional[Path],
        # No rich_help_panel: typer renders arguments in a panel of their own even when it shares a
        # title with an options panel, so naming it _BYO_PANEL printed that heading twice. The help
        # text carries the `--type byo` qualifier instead.
        typer.Argument(
            help="[--type byo] a JSONL corpus file, a .jsonl.gz, or a directory holding "
            "manifest.json + data/<source>/part-*.jsonl.gz shards",
        ),
    ] = None,
    corpus_type: Annotated[
        str,
        typer.Option(
            "--type",
            "-t",
            metavar="{byo,enterpriserag-bench}",
            help="what kind of corpus to import: `byo` reads a BYO-JSONL corpus, a `.jsonl.gz`, or "
            "a sharded artifact directory; `enterpriserag-bench` (alias `erb`) downloads and "
            "imports EnterpriseRAG-Bench",
            rich_help_panel="Common",
        ),
    ] = BYO,
    data_dir: DataDir = None,
    # --- BYO ---------------------------------------------------------------
    bundled: Annotated[
        bool,
        typer.Option(
            "--bundled",
            help="load the hello-world corpus bundled with the package instead of a path of your "
            "own — the one thing an install from a wheel can serve with no data to hand",
            rich_help_panel=_BYO_PANEL,
        ),
    ] = False,
    append: Annotated[
        bool,
        typer.Option(
            "--append",
            help="add to the existing DB instead of resetting",
            rich_help_panel=_BYO_PANEL,
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="validate the corpus only; don't touch the DB",
            rich_help_panel=_BYO_PANEL,
        ),
    ] = False,
    roster: Annotated[
        Optional[Path],
        typer.Option(
            help="roster YAML naming the corpus's principals; with it, principals/groups/tokens "
            "come from the file instead of from the records",
            rich_help_panel=_BYO_PANEL,
        ),
    ] = None,
) -> None:
    """Build the data dir from a corpus.

    Two importers behind one command, chosen with --type. Importing the bench takes no options of its own, so every option below is BYO's, and passing one with `--type erb` is an error rather than silently ignored.
    """  # noqa: E501 — see the note on `serve`: a paragraph must be one source line.
    if corpus_type not in IMPORTER_TYPES:
        raise typer.BadParameter(
            f"{corpus_type!r} is not one of {', '.join(IMPORTER_TYPES)}", param_hint="'--type'"
        )
    _use_data_dir(data_dir)

    if corpus_type != BYO:
        _reject_byo_flags_under_bench(
            {
                "--bundled": bundled,
                "--append": append,
                "--dry-run": dry_run,
                "--roster": roster,
            }
        )
        if corpus is not None:
            raise typer.BadParameter(
                f"--type {BENCH} downloads its own corpus; a path is only for --type {BYO}",
                param_hint="'CORPUS'",
            )
        from backlot.importer import erb

        raise typer.Exit(erb.run())

    if bundled == bool(corpus):
        # Exactly one source. Both mistakes are named in one message because a reader who typed
        # neither needs to learn that --bundled exists, and one who typed both needs to learn they
        # conflict.
        raise typer.BadParameter(
            "give a corpus path or --bundled (the corpus bundled with the package), not both",
            param_hint="'CORPUS'",
        )
    if bundled:
        from backlot.testing import HELLO_CORPUS

        corpus = HELLO_CORPUS

    from backlot.importer import byo

    raise typer.Exit(byo.run(corpus, append=append, dry_run=dry_run, roster=roster))


# --------------------------------------------------------------------------- export


@app.command()
def export(
    out_dir: Annotated[
        Path,
        typer.Argument(
            metavar="DIR",
            help="where to write corpus.jsonl + roster.yaml (or the shards + manifest.json)",
        ),
    ],
    shard_records: Annotated[
        Optional[int],
        typer.Option(
            metavar="N",
            help="write data/<source>/part-*.jsonl.gz shards of N records each plus "
            "manifest.json, instead of one corpus.jsonl — what half a million documents need to "
            "be distributable",
        ),
    ] = None,
    data_dir: DataDir = None,
) -> None:
    """Write EnterpriseRAG-Bench out as a BYO-JSONL artifact instead of a database.

    `backlot import` loads the result to a database equivalent to importing the bench directly. Its own command rather than a flag on `import`, because it writes an artifact and imports nothing.
    """  # noqa: E501 — see the note on `serve`: a paragraph must be one source line.
    if shard_records is not None and shard_records < 1:
        # 0 makes `n >= shard_records` always true: one shard per record, 600k files for the bench,
        # which is the very thing sharding was added to avoid.
        raise typer.BadParameter("must be at least 1", param_hint="'--shard-records'")
    _use_data_dir(data_dir)

    from backlot.importer import erb

    raise typer.Exit(erb.run_export(out_dir, shard_records=shard_records))


# --------------------------------------------------------------------------- status


@app.command()
def status(data_dir: DataDir = None) -> None:
    """Report what the data dir holds: the corpus, its per-source counts, and the roster size.

    Answers "did my import land, and what is in it" without starting a server or opening sqlite by hand. Exits 1 when there is no corpus, so it works as a shell guard.
    """  # noqa: E501 — see the note on `serve`: a paragraph must be one source line.
    _use_data_dir(data_dir)
    from backlot.config import get_settings

    settings = get_settings()
    typer.echo(f"data dir: {settings.data_dir}")
    if not settings.db_path.exists():
        typer.echo(f"corpus:   none ({settings.db_path.name} is missing)")
        typer.echo("Build one with `backlot import --bundled` or `backlot import <corpus.jsonl>`.")
        raise typer.Exit(1)

    from backlot import store

    conn = store.connect_ro(settings.db_path)
    try:
        counts = {
            src: conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            for src, tbl in store.SOURCE_TABLE.items()
        }
        # None on a DB built before the meta table existed; the counts above are rows, and parsing
        # turns one Slack transcript into many message rows, so the two numbers differ by design.
        source_docs = store.read_meta(conn, "source_documents")
    finally:
        conn.close()

    typer.echo(f"corpus:   {settings.db_path} ({settings.db_path.stat().st_size / 1e6:.1f} MB)")
    typer.echo(
        f"documents: {sum(counts.values())} rows"
        + (f" from {source_docs} source documents" if source_docs else "")
    )
    for src, n in sorted(counts.items()):
        if n:
            typer.echo(f"  {src:14s} {n}")
    empty = sorted(src for src, n in counts.items() if not n)
    if empty:
        typer.echo(f"  (no documents: {', '.join(empty)})")

    if settings.tokens_path.exists():
        import yaml

        data = yaml.safe_load(settings.tokens_path.read_text()) or {}
        users = data.get("users") or {}
        typer.echo(
            f"tokens:   {len(users)} in {settings.tokens_path.name}, org {data.get('org', '?')}"
        )
    else:
        typer.echo(f"tokens:   none ({settings.tokens_path.name} is missing)")


def main(argv: list[str] | None = None) -> int:
    """Entry point for the console script, ``python -m backlot``, and the tests.

    Returns an exit code rather than raising, so callers can drive it as a function.

    Runs the app in click's STANDALONE mode and translates the ``SystemExit`` it raises, instead of
    passing ``standalone_mode=False`` and catching the exceptions itself: typer vendors its own copy
    of click (``typer._click``), so ``typer.BadParameter is click.BadParameter`` is False and a
    usage error is not an instance of anything importable from the top-level ``click``. Standalone
    mode is also what formats those errors, so this way the message a user sees is typer's own.

    ``prog_name`` is passed because ``sys.argv[0]`` is the interpreter under ``python -m`` and
    ``-c``, which would otherwise appear in every usage line and help screen.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        app(args=args, prog_name="backlot")
    except SystemExit as e:
        return int(e.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
