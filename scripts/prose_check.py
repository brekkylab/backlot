"""Two checks on the comment and docstring lines a change adds to Python files.

    python scripts/prose_check.py [BASE]      # BASE defaults to origin/main

The change is the working tree against the merge base of BASE and HEAD, so committed, uncommitted
and untracked files are all read. What it reports:

- An issue or pull request number in a comment or docstring, a hash and two to five digits.
  Merged, the number points at a change rather than at what the code does. The comment says what
  was measured and when, and the number goes in the pull request's description. A number in a
  sentence that names another repository as `owner/repo` is that repository's, and is left alone.
- A date on a comment or docstring line under `tests/` that the same change also writes into a
  comment or docstring outside it. The code's prose is the measurement's home, and the test points
  at it rather than dating it again. A date that code outside `tests/` uses as a value, in a string
  literal, is a version such as an API's rather than a measurement's, and is left alone.

The first is the pull request template's third checkbox, the second is "Said once" in
`.claude/agents/prose-reviewer.md`. With `--github` it prints each finding as a GitHub warning on
its line and exits 0; without it, it exits 1 when it finds anything.
"""

from __future__ import annotations

import argparse
import ast
import io
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path

#: Up to five digits, since six are a colour (`#000000`), and not after `&`, an entity (`&#123;`).
NUMBER = re.compile(r"(?<!&)#\d{2,5}\b")
#: Two path segments, each starting with a letter, with nothing either side that would make them
#: part of a longer path: `psf/requests`, not `backlot/routers/github.py` or `…/merge`.
REPOSITORY = re.compile(r"(?<![\w./{}…-])[A-Za-z][\w.-]*/[A-Za-z][\w.-]*(?![\w/{}])")
#: A last segment ending in one of these makes the two segments a file, `botocore/handlers.py`.
FILE = re.compile(r"\.(?:py|pyi|md|json|jsonl|toml|ya?ml|txt|cfg|ini|sh|js|mjs|ts|tsx|html)$")
#: First segments that make two segments a media type, `application/json`, rather than a repository.
MEDIA_TYPES = frozenset(
    {"application", "audio", "font", "image", "message", "model", "multipart", "text", "video"}
)
DATE = re.compile(r"\b20\d\d-\d\d-\d\d\b")
#: The end of a sentence, or of a paragraph: `issue_numbers` joins a block's lines with spaces and
#: writes a blank line as a newline.
SENTENCE_END = re.compile(r"[.!?]\s+|\n")
#: String tokens. Python 3.12 splits an f-string into parts, and its literal text is the middle.
STRINGS = {tokenize.STRING} | (
    {tokenize.FSTRING_MIDDLE} if hasattr(tokenize, "FSTRING_MIDDLE") else set()
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def added_lines(diff: str) -> dict[str, set[int]]:
    """The lines a `git diff -U0` adds, by the path they are added to."""
    files: dict[str, set[int]] = {}
    path, line = None, 0
    for text in diff.splitlines():
        if text.startswith("+++ "):
            path = text[6:] if text.startswith("+++ b/") else None
            if path:
                files.setdefault(path, set())
        elif text.startswith("@@") and path:
            line = int(re.search(r"\+(\d+)", text).group(1))
        elif path and text.startswith("+"):
            files[path].add(line)
            line += 1
    return files


def changed(root: Path, base: str) -> dict[str, set[int]]:
    """The added lines of every Python file the change touches, untracked files whole."""
    merge_base = git(root, "merge-base", base, "HEAD").strip()
    files = added_lines(
        git(
            root,
            "diff",
            "-U0",
            "--no-color",
            "--no-ext-diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            merge_base,
            "--",
            "*.py",
        )
    )
    for path in git(root, "ls-files", "--others", "--exclude-standard", "--", "*.py").splitlines():
        lines = (root / path).read_text(encoding="utf-8").count("\n") + 1
        files[path] = set(range(1, lines + 1))
    return files


def docstrings(src: str) -> list[tuple[int, int]]:
    """The first and last line of each module, class and function docstring in SRC."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                spans.append((first.lineno, first.end_lineno))
    return spans


def blocks(src: str) -> list[list[tuple[int, str]]]:
    """SRC's prose as runs of `(line, text)`: a docstring, consecutive whole-line comments, or a
    comment after code. A comment's text is what follows its `#` or `#:`."""
    lines = src.split("\n")
    out = [[(n, lines[n - 1]) for n in range(a, b + 1)] for a, b in docstrings(src)]
    comments: list[tuple[int, str, bool]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                own_line = not lines[row - 1][:col].strip()
                comments.append((row, re.sub(r"^#:?", "", tok.string), own_line))
    except (tokenize.TokenError, SyntaxError):
        comments = [
            (n, re.sub(r"^\s*#:?", "", t), True)
            for n, t in enumerate(lines, 1)
            if t.lstrip().startswith("#")
        ]
    run: list[tuple[int, str]] = []
    for row, text, own_line in comments:
        if run and (not own_line or row != run[-1][0] + 1):
            out.append(run)
            run = []
        if own_line:
            run.append((row, text))
        else:
            out.append([(row, text)])
    if run:
        out.append(run)
    return out


def sentence_at(text: str, at: int) -> str:
    start = max((m.end() for m in SENTENCE_END.finditer(text, 0, at)), default=0)
    end = SENTENCE_END.search(text, at)
    return text[start : end.start() if end else len(text)]


def names_repository(sentence: str, local: frozenset[str]) -> bool:
    """Whether SENTENCE names a repository as `owner/repo` other than this one, and other than a
    file, a path under LOCAL (the names at this repository's root), a media type, `and/or` or two
    upper-case words such as `GET/POST`."""
    for m in REPOSITORY.finditer(sentence):
        owner, repo = m.group().split("/", 1)
        if not (
            FILE.search(repo)
            or owner in local
            or owner.lower() in MEDIA_TYPES
            or m.group().lower() in {"and/or", "brekkylab/backlot"}
            or (owner.isupper() and repo.isupper())
        ):
            return True
    return False


def issue_numbers(
    src: str, added: set[int], local: frozenset[str] = frozenset()
) -> list[tuple[int, str]]:
    """Check 1 on one file: an issue number on an added prose line, outside a sentence naming a
    repository."""
    found = []
    for block in blocks(src):
        text, starts = "", []
        for n, line in block:
            starts.append((len(text), n))
            text += (line.strip() or "\n") + " "
        for m in NUMBER.finditer(text):
            n = max(start for start in starts if start[0] <= m.start())[1]
            if n in added and not names_repository(sentence_at(text, m.start()), local):
                found.append(
                    (
                        n,
                        f"{m.group()} names an issue or pull request: say what was measured and "
                        f"when, and put {m.group()} in the pull request's description",
                    )
                )
    return found


def prose_dates(src: str, added: set[int]) -> dict[int, set[str]]:
    """The dates on SRC's added prose lines, by line."""
    out: dict[int, set[str]] = {}
    for block in blocks(src):
        for n, line in block:
            if n in added and (dates := set(DATE.findall(line))):
                out.setdefault(n, set()).update(dates)
    return out


def value_dates(sources: dict[str, str]) -> set[str]:
    """Dates in the string literals of SOURCES that are not docstrings."""
    found: set[str] = set()
    for src in sources.values():
        spans = docstrings(src)
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type in STRINGS and not any(a <= tok.start[0] <= b for a, b in spans):
                    found.update(DATE.findall(tok.string))
        except (tokenize.TokenError, SyntaxError):
            continue
    return found


def redated_tests(
    sources: dict[str, str], added: dict[str, set[int]], values: set[str]
) -> list[tuple[str, int, str]]:
    """Check 2: a date on an added test prose line that an added code prose line also carries."""
    homes: dict[str, list[str]] = {}
    for path in sorted(added):
        if not path.startswith("tests/"):
            for n, dates in sorted(prose_dates(sources[path], added[path]).items()):
                for d in dates - values:
                    homes.setdefault(d, []).append(f"{path}:{n}")
    found = []
    for path in sorted(added):
        if path.startswith("tests/"):
            for n, dates in sorted(prose_dates(sources[path], added[path]).items()):
                for d in sorted(dates & homes.keys()):
                    found.append(
                        (
                            path,
                            n,
                            f"{d} dates a measurement {', '.join(homes[d][:3])} records: "
                            "point at it rather than dating it again",
                        )
                    )
    return found


def check(root: Path, base: str) -> list[tuple[str, int, str]]:
    added = changed(root, base)
    sources = {p: (root / p).read_text(encoding="utf-8") for p in added if (root / p).is_file()}
    added = {p: lines for p, lines in added.items() if p in sources}
    local = frozenset(entry.name for entry in root.iterdir())
    found = [
        (p, n, msg) for p in sorted(added) for n, msg in issue_numbers(sources[p], added[p], local)
    ]
    if any(path.startswith("tests/") for path in added):
        code = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py")
        values = value_dates(
            {
                p: (root / p).read_text(encoding="utf-8")
                for p in code.splitlines()
                if not p.startswith("tests/") and (root / p).is_file()
            }
        )
        found += redated_tests(sources, added, values)
    return sorted(found)


def _escape(value: str, prop: bool = False) -> str:
    """A workflow command's escaping: `%`, CR and LF everywhere, and `:` and `,` in a property."""
    value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return value.replace(":", "%3A").replace(",", "%2C") if prop else value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("base", nargs="?", default="origin/main")
    parser.add_argument("--github", action="store_true", help="print GitHub warnings and exit 0")
    args = parser.parse_args(argv)
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    found = check(root, args.base)
    for path, n, msg in found:
        if args.github:
            print(f"::warning file={_escape(path, True)},line={n},title=prose::{_escape(msg)}")
        else:
            print(f"{path}:{n}: {msg}")
    if args.github and found and (summary := os.environ.get("GITHUB_STEP_SUMMARY")):
        with open(summary, "a", encoding="utf-8") as out:
            out.write("### Prose checks\n\n")
            out.writelines(f"- `{path}:{n}`: {msg}\n" for path, n, msg in found)
    return 1 if found and not args.github else 0


if __name__ == "__main__":
    sys.exit(main())
