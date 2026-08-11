"""The ``backlot`` console script — one entry point for serving and for building ``data/``.

Two commands, each a thin front end over code that already existed:

    backlot serve                       # uvicorn backlot.main:app, with uvicorn's own defaults
    backlot import <corpus.jsonl>       # backlot.importer.byo   (--type byo, the default)
    backlot import --type erb           # backlot.importer.erb

``import`` dispatches on ``--type`` and then hands the REMAINING argv to that importer's own
``main``, so every flag, default and message stays defined in exactly one place — the importer.
Nothing is re-declared here, which is why ``backlot import --dry-run`` and
``python -m backlot.importer.byo --dry-run`` cannot drift apart.

``python -m backlot.importer.{byo,erb}`` still work unchanged; this is a shorter spelling of them,
not a replacement.
"""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import PackageNotFoundError, version

# Imported lazily inside each command, not here: `serve` must not pay for the importers' module
# import (backlot.importer.erb alone is 2,400 lines), and `import` must not pull in uvicorn.

# --type value -> the module implementing it. `erb` is accepted beside the full bench name because
# that is what the module, the tests and every existing doc call it.
IMPORTER_TYPES = ("byo", "enterpriserag-bench", "erb")


def _version() -> str:
    try:
        return version("backlot")
    except PackageNotFoundError:  # a source tree that was never installed
        return "unknown"


def _type_arg(ap: argparse.ArgumentParser) -> None:
    """Declare ``--type`` on ``ap``. Called for the dispatch parser and again for the parser that
    renders ``--help``, so the flag is defined once and both spellings cannot disagree."""
    ap.add_argument(
        "--type",
        "-t",
        dest="corpus_type",
        default="byo",
        choices=IMPORTER_TYPES,
        metavar="{byo,enterpriserag-bench}",
        help="what kind of corpus to import: `byo` (default) reads a BYO-JSONL corpus, a "
        "`.jsonl.gz`, or a sharded artifact directory; `enterpriserag-bench` (alias `erb`) "
        "downloads and imports EnterpriseRAG-Bench. The remaining options are that "
        "importer's own — see `backlot import --type <t> --help`",
    )


def _serve(argv: list[str]) -> int:
    """Run the ASGI app under uvicorn.

    Every default here is uvicorn's own (127.0.0.1:8000, proxy headers on), so this is a shorter
    spelling of `python -m uvicorn backlot.main:app` and not a second set of behaviour to keep in
    step with it. The app is passed as an import STRING because that is what `--reload` requires.
    """
    ap = argparse.ArgumentParser(
        prog="backlot serve",
        description="Serve the mock APIs over the corpus in the data dir (BACKLOT_DATA_DIR). "
        "The corpus has to exist — build one with `backlot import` first.",
    )
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    ap.add_argument("--reload", action="store_true", help="restart on source changes (development)")
    ap.add_argument(
        "--log-level",
        default=None,
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        help="uvicorn log level (default: info)",
    )
    # Behind a TLS-terminating proxy/ALB these two make the app honour X-Forwarded-Proto/Host and
    # emit https self-URLs, which clients that follow returned URLs (PyGithub) need. On by default
    # in uvicorn, so the flag that carries weight is the negative one.
    ap.add_argument(
        "--no-proxy-headers",
        dest="proxy_headers",
        action="store_false",
        help="ignore X-Forwarded-* headers (uvicorn honours them by default)",
    )
    ap.add_argument(
        "--forwarded-allow-ips",
        default=None,
        metavar="IPS",
        help="comma-separated proxy IPs to trust X-Forwarded-* from, or * for any "
        "(default: 127.0.0.1)",
    )
    args = ap.parse_args(argv)

    import uvicorn

    uvicorn.run(
        "backlot.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        proxy_headers=args.proxy_headers,
        forwarded_allow_ips=args.forwarded_allow_ips,
    )
    return 0


def _import(argv: list[str]) -> int:
    """Dispatch to one importer's ``main`` with the rest of the argv untouched."""
    # add_help=False and parse_known_args: everything that is not --type belongs to the importer,
    # including -h, which is answered below against the CHOSEN importer's parser so one help
    # screen shows both --type and that importer's own flags.
    pre = argparse.ArgumentParser(prog="backlot import", add_help=False)
    _type_arg(pre)
    args, rest = pre.parse_known_args(argv)

    if args.corpus_type == "byo":
        from backlot.importer import byo as importer
    else:
        from backlot.importer import erb as importer

    if any(a in ("-h", "--help") for a in rest):
        ap = importer.build_parser(prog="backlot import")
        _type_arg(ap)
        ap.print_help()
        return 0
    return importer.main(rest, prog="backlot import")


COMMANDS = {"serve": _serve, "import": _import}


def _top_parser() -> argparse.ArgumentParser:
    """The parser for ``backlot`` itself — help, --version, and the command list.

    The subcommands are declared for the help listing only; `main` dispatches on argv before this
    parser ever runs, so a command's own flags (`backlot serve --reload`) are never parsed here.
    """
    ap = argparse.ArgumentParser(
        prog="backlot",
        description="Enterprise SaaS read APIs (Slack, Gmail, Drive, GitHub, Jira, Confluence, "
        "Notion, S3, HubSpot, Linear, Fireflies) over your own corpus, with per-document ACLs.",
        epilog="Run `backlot <command> --help` for a command's own options.",
    )
    ap.add_argument("--version", action="version", version=f"backlot {_version()}")
    sub = ap.add_subparsers(dest="command", required=True, metavar="<command>")
    sub.add_parser("serve", help="run the mock API server")
    sub.add_parser("import", help="build the data dir from a corpus (--type byo | erb)")
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in COMMANDS:
        return COMMANDS[argv[0]](argv[1:])
    # No command, an unknown one, or a global flag: let argparse answer it. `--help`/`--version`
    # exit 0 from inside parse_args; anything else is a usage error, which exits 2.
    _top_parser().parse_args(argv)
    return 2  # unreachable: parse_args above always exits when no command was dispatched


if __name__ == "__main__":
    raise SystemExit(main())
