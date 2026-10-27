#!/usr/bin/env python3
"""Record what the test suite observes of Backlot, and compare two recordings.

    python scripts/invariance.py record OUT [PYTEST_ARGS...]   # run from the checkout to record
    python scripts/invariance.py compare BASE HEAD --mode code|tests [--calibration BASE2]
    python scripts/invariance.py prose BASE_REF     # Python changed only in comments and docstrings
    python scripts/invariance.py uncovered OUT PATH...   # lines of PATH no test executed in OUT
    python scripts/invariance.py similar PATH...    # functions in PATH alike to one anywhere else
    python scripts/invariance.py verbose PATH...    # comment and docstring blocks in PATH, longest first
    python scripts/invariance.py mutate FILE OLD NEW -- PYTEST_ARGS...   # does a test catch OLD -> NEW?

``record`` runs the suite in the current directory with this file loaded as a pytest plugin, and
writes under OUT:

- ``traffic/``: every request a test sends through a ``TestClient`` and the full response it gets
  (status, headers, body), keyed by the test's node id and the request's position within it.
- ``outcomes/``: each test's outcome.
- ``coverage.json``: line and branch coverage of ``backlot/``.
- ``meta.json``: the run's exit code, wall time and outcome counts.

``compare`` answers for a change that must not alter behaviour:

- ``--mode code``: the change touches ``backlot/`` and leaves ``tests/`` alone, so both runs send
  the same requests. Every response must match part by part — status, each header, each JSON leaf
  and the key order of each object — and every test must end the same way.
- ``--mode tests``: the change touches ``tests/`` and leaves ``backlot/`` alone, so the code is the
  same and only the tests differ. Every line and branch of ``backlot/`` the base run executed must
  still be executed, and every test still present must end the same way.

Two runs of one commit differ: S3 signatures and rate-limit counters follow the clock, the Jira
array-conversion error carries a Java identity hash, and a generated service-account key is random.
``--calibration`` takes a second recording of the base and stops comparing what differs between
the two: a header's digits everywhere (its whole value when more than digits differ), any other
value (the length included) only in the exchange it differed in.

``prose`` answers for a change to comments, docstrings and markdown: each Python file that differs
from BASE_REF parses to the same tree once docstrings are dropped, except a route's or a CLI
command's, which Backlot serves.

Requests a test sends to a ``uvicorn`` subprocess (``live_server``, ``backlot.serve``: the official
SDKs, the MCP servers and most of the S3 tables) are not recorded; those tests are held by their
outcomes, and the subprocess's lines by coverage, which ``pyproject.toml`` has follow it.

The plugin refuses to record when ``backlot`` is imported from anywhere but the checkout being
recorded, so a base run in a worktree that shares another checkout's virtualenv cannot silently
record that checkout's code instead.
"""

from __future__ import annotations

import argparse
import ast
import base64
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ENV_OUT = "BACKLOT_INVARIANCE_OUT"


# --- the pytest plugin ------------------------------------------------------------------------

_out: Path | None = None
_current = "<collection>"
_counts: Counter[str] = Counter()
_traffic_fh = None
_outcome_fh = None


def pytest_configure(config):
    global _out, _traffic_fh, _outcome_fh
    out = os.environ.get(ENV_OUT)
    if not out:
        return
    _out = Path(out)
    import backlot

    root = Path(config.rootpath).resolve()
    imported = Path(backlot.__file__).resolve()
    if not imported.is_relative_to(root):
        raise SystemExit(f"invariance: backlot is imported from {imported}, not from {root}")
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    (_out / "traffic").mkdir(parents=True, exist_ok=True)
    (_out / "outcomes").mkdir(parents=True, exist_ok=True)
    _traffic_fh = open(_out / "traffic" / f"{worker}.jsonl", "w", encoding="utf-8")  # noqa: SIM115
    _outcome_fh = open(_out / "outcomes" / f"{worker}.jsonl", "w", encoding="utf-8")  # noqa: SIM115
    _patch_transport()


def pytest_runtest_logstart(nodeid, location):
    global _current
    _current = nodeid


def pytest_runtest_logreport(report):
    if _outcome_fh is None:
        return
    if report.when == "call" or report.outcome != "passed":
        _outcome_fh.write(
            json.dumps({"test": report.nodeid, "when": report.when, "outcome": report.outcome})
            + "\n"
        )


def pytest_unconfigure(config):
    for fh in (_traffic_fh, _outcome_fh):
        if fh is not None:
            fh.close()


def _body(data: bytes) -> dict:
    try:
        return {"text": data.decode()}
    except UnicodeDecodeError:
        return {"b64": base64.b64encode(data).decode()}


def _patch_transport():
    from starlette.testclient import _TestClientTransport

    original = _TestClientTransport.handle_request

    def handle_request(self, request):
        i = _counts[_current]
        _counts[_current] += 1
        record = {
            "test": _current,
            "i": i,
            "request": {
                "method": request.method,
                "url": str(request.url),
                "headers": sorted(request.headers.multi_items()),
                "body": _body(request.read()),
            },
        }
        try:
            response = original(self, request)
        except BaseException as exc:
            record["exception"] = f"{type(exc).__name__}: {exc}"
            _traffic_fh.write(json.dumps(record) + "\n")
            raise
        record["status"] = response.status_code
        record["headers"] = response.headers.multi_items()
        record["body"] = _body(response.read())
        _traffic_fh.write(json.dumps(record) + "\n")
        return response

    _TestClientTransport.handle_request = handle_request


# --- the command line -------------------------------------------------------------------------


def _record(out: Path, pytest_args: list[str]) -> int:
    out = out.resolve()
    if out.exists() and any(out.iterdir()):
        sys.exit(f"invariance: {out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env[ENV_OUT] = str(out)
    env["PYTHONHASHSEED"] = "0"
    env["COVERAGE_FILE"] = str(out / ".coverage")
    here = str(Path(__file__).resolve().parent)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [here, env.get("PYTHONPATH")]))
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        Path(__file__).stem,
        "--cov=backlot",
        "--cov-branch",
        f"--cov-report=json:{out / 'coverage.json'}",
        *pytest_args,
    ]
    start = time.monotonic()
    code = subprocess.call(cmd, env=env)
    meta = {"exit": code, "seconds": round(time.monotonic() - start), **_counts_of(out)}
    (out / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return code


def _counts_of(run: Path) -> dict[str, int]:
    return dict(Counter(o.split()[-1] for o in _outcomes(run).values()))


def _read(directory: Path) -> list[dict]:
    rows = []
    for path in sorted(directory.glob("*.jsonl")):
        rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    return rows


def _outcomes(run: Path) -> dict[str, str]:
    """Each test's outcome: the first phase that did not pass, else its call phase. Rows arrive as
    setup, call, teardown, so a teardown failure overrides a passing call."""
    result: dict[str, str] = {}
    for row in _read(run / "outcomes"):
        if row["when"] == "call":
            result.setdefault(row["test"], row["outcome"])
        elif result.get(row["test"], "passed") == "passed":
            result[row["test"]] = f"{row['when']} {row['outcome']}"
    return result


def _traffic(run: Path) -> dict[tuple[str, int], dict]:
    return {(row["test"], row["i"]): row for row in _read(run / "traffic")}


def _leaves(value, path="$"):
    """Each leaf of a JSON value with its path, plus each object's key order; an empty object or
    list is a leaf of its own."""
    if isinstance(value, dict) and value:
        yield f"{path} keys", list(value)
        for k, v in value.items():
            yield from _leaves(v, f"{path}.{k}")
    elif isinstance(value, list) and value:
        for i, v in enumerate(value):
            yield from _leaves(v, f"{path}[{i}]")
    else:
        yield path, value


def _parts(prefix: str, status, headers: list, body: dict) -> dict:
    view = {f"{prefix} status": status}
    for i, (name, value) in enumerate(headers):
        view[f"{prefix} header {i} {name.lower()}"] = value
    if "b64" in body:
        view[f"{prefix} body"] = body["b64"]
        return view
    try:
        view.update((f"{prefix} body {p}", v) for p, v in list(_leaves(json.loads(body["text"]))))
    except (ValueError, RecursionError):  # not JSON, or nested past what the parser recurses into
        view[f"{prefix} body"] = body["text"]
    return view


def _view(record: dict) -> dict:
    """A request and its response as one flat ``{part: value}``: the method and url, each header,
    each JSON leaf of each body (or the whole body when it is not JSON), and the status."""
    req = record["request"]
    view = _parts("request", f"{req['method']} {req['url']}", req["headers"], req["body"])
    if "exception" in record:
        view["exception"] = record["exception"]
    else:
        view.update(_parts("response", record["status"], record["headers"], record["body"]))
    return view


def _header(part: str) -> str | None:
    """``request header 3 authorization`` -> ``request authorization``; None for any other part."""
    words = part.split(" ", 3)
    return f"{words[0]} {words[3]}" if len(words) == 4 and words[1] == "header" else None


def _digitless(value) -> str:
    return re.sub(r"\d+", "#", str(value))


def _noise(base: dict, calibration: dict) -> tuple[dict[str, bool], dict]:
    """What differs between two runs of one commit. Headers by name, everywhere: True when only
    their digits differ (a clock, a counter), False when more does (a signature). Other parts per
    exchange."""
    names: dict[str, bool] = {}
    parts: dict[tuple[str, int], set[str]] = {}
    for key, record in base.items():
        if key not in calibration:
            continue
        old, new = _view(record), _view(calibration[key])
        for part in set(old) | set(new):
            if old.get(part) != new.get(part):
                # A header's value varies with the clock wherever it varies at all, except the
                # length, which follows the body and varies only where the body does.
                if _header(part) and not part.endswith(" content-length"):
                    digits = _digitless(old.get(part)) == _digitless(new.get(part))
                    names[_header(part)] = names.get(_header(part), True) and digits
                else:
                    parts.setdefault(key, set()).add(part)
    return names, parts


def _masked(view: dict, names: dict[str, bool], parts: set[str]) -> dict:
    # A body that varies varies in length too, though two runs can land on the same length.
    varying = {p.split(" ")[0] for p in parts if p.split(" ")[1:2] == ["body"]}
    masked = {}
    for part, value in view.items():
        if part in parts or (part.endswith(" content-length") and part.split(" ", 1)[0] in varying):
            continue
        digits_only = names.get(_header(part))
        if digits_only is None:
            masked[part] = value
        else:
            masked[part] = _digitless(value) if digits_only else "<varies>"
    return masked


def _files(run: Path) -> dict[str, dict]:
    """``coverage.json``'s files, each named with ``/`` between its parts. Coverage names them with
    the platform's separator, ``\\`` on Windows, where no path from ``git diff`` would match one."""
    files = json.loads((run / "coverage.json").read_text(encoding="utf-8"))["files"]
    return {name.replace("\\", "/"): data for name, data in files.items()}


def _coverage(run: Path) -> dict[str, tuple[set, set]]:
    return {
        name: (set(f["executed_lines"]), {tuple(b) for b in f.get("executed_branches", [])})
        for name, f in _files(run).items()
    }


def _compare(base: Path, head: Path, mode: str, calibration: Path | None, limit: int) -> int:
    problems: list[str] = []

    before, after = _outcomes(base), _outcomes(head)
    for test, outcome in sorted(before.items()):
        if test not in after:
            if mode == "code":
                problems.append(f"test {test} ran on the base and not on the head")
        elif after[test] != outcome:
            problems.append(f"test {test}: {outcome} on the base, {after[test]} on the head")
    for test in sorted(set(after) - set(before)):
        if mode == "code":
            problems.append(f"test {test} ran on the head and not on the base")
        elif after[test] != "passed":
            problems.append(f"test {test} is new and {after[test]}")
    for name, run in (("base", base), ("head", head)):
        meta_file = run / "meta.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
        counts = ", ".join(f"{n} {k}" for k, n in sorted(_counts_of(run).items()))
        print(f"{name}: {counts}" + (f" in {meta['seconds']}s" if "seconds" in meta else ""))

    if mode == "code":
        b, h = _traffic(base), _traffic(head)
        names, parts = _noise(b, _traffic(calibration)) if calibration else ({}, {})
        digits = sorted(n for n, d in names.items() if d)
        whole = sorted(n for n, d in names.items() if not d)
        print(
            f"traffic: {len(b)} responses on the base, {len(h)} on the head; ignored as varying "
            f"between two base runs: the digits of {digits or 'no headers'}, the values of "
            f"{whole or 'no headers'}, and {sum(map(len, parts.values()))} other values in "
            f"{len(parts)} exchanges"
        )
        shown = 0
        for key in sorted(set(b) | set(h)):
            test, i = key
            if key not in b or key not in h:
                problems.append(f"request {i} of {test} was sent by one run only")
                continue
            req = b[key]["request"]
            old = _masked(_view(b[key]), names, parts.get(key, set()))
            new = _masked(_view(h[key]), names, parts.get(key, set()))
            if old != new:
                problems.append(f"{req['method']} {req['url']} in {test} differs")
                if shown < limit:
                    shown += 1
                    for part in sorted(set(old) | set(new), key=str):
                        if old.get(part) != new.get(part):
                            problems.append(
                                f"    {part}: {old.get(part, '<absent>')!r:.200}"
                                f" -> {new.get(part, '<absent>')!r:.200}"
                            )
    else:
        b, h = _coverage(base), _coverage(head)
        lost_lines = lost_branches = 0
        for name, (lines, branches) in sorted(b.items()):
            now_lines, now_branches = h.get(name, (set(), set()))
            missing = sorted(lines - now_lines)
            missing_branches = sorted(branches - now_branches)
            lost_lines += len(missing)
            lost_branches += len(missing_branches)
            if missing:
                problems.append(f"{name}: lines no longer executed: {missing}")
            if missing_branches:
                problems.append(f"{name}: branches no longer taken: {missing_branches}")
        print(f"coverage: {lost_lines} lines and {lost_branches} branches of backlot/ lost")

    for line in problems:
        print(line)
    print("invariant" if not problems else f"{len(problems)} differences")
    return 1 if problems else 0


# A route's docstring is its description in /openapi.json, which the MCP bridge serves as the
# tool's description; a typer command's is its --help.
_SERVED = {"get", "post", "put", "patch", "delete", "head", "options", "api_route", "command"}


def _code_tree(source: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        served = any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in _SERVED
            for d in getattr(node, "decorator_list", [])
        )
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and not served
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.dump(tree)


def _prose(base_ref: str) -> int:
    changed = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AMDR", base_ref, "--", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    problems = list(untracked)
    for name in changed:
        old = subprocess.run(
            ["git", "show", f"{base_ref}:{name}"], capture_output=True, text=True, encoding="utf-8"
        )
        new = Path(name).read_text(encoding="utf-8") if Path(name).exists() else None
        if old.returncode or new is None or _code_tree(old.stdout) != _code_tree(new):
            problems.append(name)
    print(f"{len(changed)} Python files changed, {len(untracked)} added")
    for name in problems:
        print(f"{name}: changed beyond its comments and unserved docstrings")
    print("invariant" if not problems else f"{len(problems)} differences")
    return 1 if problems else 0


def _uncovered(run: Path, paths: list[str]) -> int:
    paths = [p.replace("\\", "/").rstrip("/") for p in paths]
    for name, data in sorted(_files(run).items()):
        if any(name == p or name.startswith(p + "/") for p in paths):
            missing = data["missing_lines"]
            print(f"{name}: {len(missing)} lines no test executes: {missing}")
    return 0


def _python_files(paths: list[Path]) -> list[Path]:
    """The ``.py`` files among ``paths`` and under the directories among them."""
    found = []
    for path in paths:
        found += [path] if path.is_file() else sorted(path.rglob("*.py"))
    return [f for f in found if f.suffix == ".py"]


def _functions(root: Path) -> list[tuple[str, int, str, str]]:
    """Every function under ``root`` that is not a test, as (file, line, name, body without
    docstring), skipping bodies under three lines: those repeat by accident."""
    found = []
    for path in _python_files([root]):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("test_"):
                continue
            body = node.body
            if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]
            source = "\n".join(ast.unparse(statement) for statement in body)
            if source.count("\n") >= 2:
                found.append((str(path), node.lineno, node.name, source))
    return found


def _similar(paths: list[str], threshold: float) -> int:
    from difflib import SequenceMatcher

    targets = [f for p in paths for f in _functions(Path(p))]
    pool = _functions(Path("backlot")) + _functions(Path("tests"))
    seen = set()
    for file, line, name, source in targets:
        for other_file, other_line, other_name, other in pool:
            pair = frozenset({(file, line), (other_file, other_line)})
            if len(pair) == 1 or pair in seen:
                continue
            matcher = SequenceMatcher(None, source, other, autojunk=False)
            if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
                continue
            ratio = matcher.ratio()
            if ratio >= threshold:
                seen.add(pair)
                print(
                    f"{ratio:.2f} {file}:{line} {name}  ~  {other_file}:{other_line} {other_name}"
                )
    return 0


def _verbose(paths: list[str], minimum: int) -> int:
    blocks = []
    for path in _python_files([Path(p) for p in paths]):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source)):
            body = getattr(node, "body", None)
            if (
                isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                end = len(lines) if isinstance(node, ast.Module) else node.end_lineno
                code = end - body[0].end_lineno
                blocks.append((body[0].end_lineno - body[0].lineno + 1, path, body[0].lineno, code))
        start = None
        for i, line in enumerate([*lines, ""], start=1):
            if line.lstrip().startswith("#"):
                start = start or i
            elif start:
                blocks.append((i - start, path, start, None))
                start = None
    for length, path, line, code in sorted(blocks, key=lambda b: -b[0]):
        if length >= minimum:
            kind = "comment" if code is None else f"docstring over {code} lines of code"
            print(f"{length:4} {path}:{line} {kind}")
    return 0


def _mutate(file: Path, old: str, new: str, pytest_args: list[str]) -> int:
    """Replace the one occurrence of ``old`` in ``file`` with ``new``, run pytest, and put the file
    back byte for byte, on SIGTERM too. Exits 0 when a test fails, which is what a mutation is for,
    1 when every test passes, and 2 when none ran to a verdict: none selected, or one errored."""
    import signal
    import tempfile
    from xml.etree import ElementTree

    original, before, after = file.read_bytes(), old.encode(), new.encode()
    if original.count(before) != 1:
        sys.exit(f"mutate: {old!r} occurs {original.count(before)} times in {file}, not once")
    previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    file.write_bytes(original.replace(before, after))
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "junit.xml"
        try:
            code = subprocess.call(
                [sys.executable, "-m", "pytest", "-x", f"--junitxml={report}", *pytest_args]
            )
        finally:
            file.write_bytes(original)
            signal.signal(signal.SIGTERM, previous)
        suite = next(ElementTree.parse(report).iter("testsuite")) if report.exists() else None
    tally = {}
    if suite is not None:
        tally = {k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors")}
    if not tally.get("tests") or tally["errors"]:
        print(f"no verdict: pytest exited {code} with {tally or 'no report'}")
        return 2
    print("caught" if tally["failures"] else "missed")
    return 0 if tally["failures"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="run the suite here and record it under OUT")
    rec.add_argument("out", type=Path)
    rec.add_argument("pytest_args", nargs=argparse.REMAINDER)
    cmp = sub.add_parser("compare", help="compare two recordings")
    cmp.add_argument("base", type=Path)
    cmp.add_argument("head", type=Path)
    cmp.add_argument("--mode", choices=["code", "tests"], required=True)
    cmp.add_argument(
        "--calibration",
        type=Path,
        help="a second recording of the base; what differs between the two is not compared",
    )
    cmp.add_argument("--limit", type=int, default=10, help="response diffs to print in full")
    unc = sub.add_parser(
        "uncovered", help="list the lines of PATHs no test executed in a recording"
    )
    unc.add_argument("run", type=Path)
    unc.add_argument("paths", nargs="+")
    sim = sub.add_parser("similar", help="list functions in PATH alike to one anywhere else")
    sim.add_argument("paths", nargs="+")
    sim.add_argument("--threshold", type=float, default=0.8)
    verb = sub.add_parser("verbose", help="list comment and docstring blocks, longest first")
    verb.add_argument("paths", nargs="+")
    verb.add_argument("--min", type=int, default=6, dest="minimum")
    mut = sub.add_parser("mutate", help="apply OLD -> NEW in FILE, run pytest, restore")
    mut.add_argument("file", type=Path)
    mut.add_argument("old")
    mut.add_argument("new")
    mut.add_argument("pytest_args", nargs=argparse.REMAINDER)
    prose = sub.add_parser("prose", help="check Python changed only in comments and docstrings")
    prose.add_argument("base_ref")
    args = parser.parse_args(argv)
    if args.command == "record":
        return _record(args.out, args.pytest_args)
    if args.command == "uncovered":
        return _uncovered(args.run, args.paths)
    if args.command == "similar":
        return _similar(args.paths, args.threshold)
    if args.command == "verbose":
        return _verbose(args.paths, args.minimum)
    if args.command == "mutate":
        rest = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
        return _mutate(args.file, args.old, args.new, rest)
    if args.command == "prose":
        return _prose(args.base_ref)
    return _compare(args.base, args.head, args.mode, args.calibration, args.limit)


if __name__ == "__main__":
    sys.exit(main())
