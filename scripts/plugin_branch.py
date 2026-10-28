"""Build the tree the ``plugin`` branch carries, which the Claude Code and Codex marketplaces install.

The tree holds the two plugin manifests, every file they name, the plugin's own README, and the
license: the skill as it was at a release, without the package, its tests or its lock file. Only
files git tracks are copied. Run by ``.github/workflows/plugin-branch.yml``.

- Both manifests get ``--version``. Codex reinstalls a cached plugin only when that string changes
  (openai/codex ``core-plugins/src/loader.rs``, 2026-10-20), so it has to move with every release.
- A Markdown link in the skill that points elsewhere in the repository becomes a GitHub URL at
  ``--ref``, since the branch does not carry what it points at.

``python scripts/plugin_branch.py --version 1.2.3 --ref v1.2.3 <out>`` writes the tree to ``<out>``.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFESTS = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json")
# Repository file -> where the branch carries it.
CARRIED = {"plugin/README.md": "README.md", "LICENSE": "LICENSE", "NOTICE.md": "NOTICE.md"}
GITHUB = "https://github.com/brekkylab/backlot"

_VERSION = re.compile(r'"version": "[^"]*"')
_PLAIN_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]*")
_LINK = re.compile(r"\]\((?!https?://|mailto:|#)([^)\s#]+)(#[^)\s]*)?\)")


def _tracked(path: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--", path],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    files = [f for f in out.split("\0") if f]
    if not files:
        raise SystemExit(f"{path} names nothing git tracks")
    return files


def _named_paths(value) -> list[str]:
    """Every ``./``-relative path a manifest names, anywhere in it."""
    if isinstance(value, dict):
        return [p for v in value.values() for p in _named_paths(v)]
    if isinstance(value, list):
        return [p for v in value for p in _named_paths(v)]
    if isinstance(value, str) and value.startswith("./"):
        return [value[2:].rstrip("/")]
    return []


def _absolute_links(text: str, source: Path, ref: str, tree: set[str]) -> str:
    """Point each relative link that leaves ``tree`` at its file on GitHub at ``ref``."""

    def rewrite(m: re.Match) -> str:
        target = (source.parent / m[1]).resolve().relative_to(REPO).as_posix()
        if target in tree or any(f.startswith(f"{target}/") for f in tree):
            return m[0]
        if not (REPO / target).exists():
            raise SystemExit(f"{source.relative_to(REPO)} links to {m[1]}, which does not exist")
        kind = "tree" if (REPO / target).is_dir() else "blob"
        return f"]({GITHUB}/{kind}/{ref}/{target}{m[2] or ''})"

    return _LINK.sub(rewrite, text)


def build(version: str, ref: str, out: Path) -> None:
    if not _PLAIN_VERSION.fullmatch(version):
        raise SystemExit(f"{version!r} is not a version")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty")

    files: dict[str, str] = {}  # branch path -> repository path
    for manifest in MANIFESTS:
        files[manifest] = manifest
        for named in _named_paths(json.loads((REPO / manifest).read_text(encoding="utf-8"))):
            files.update({f: f for f in _tracked(named)})
    files.update({dst: src for src, dst in CARRIED.items()})

    tree = set(files)
    for dst, src in sorted(files.items()):
        target = out / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        if dst in MANIFESTS:
            text, n = _VERSION.subn(
                f'"version": "{version}"', (REPO / src).read_text(encoding="utf-8")
            )
            if n != 1:
                raise SystemExit(f"{src} carries {n} version fields, not one")
            target.write_text(text, encoding="utf-8")
        elif dst.startswith("skills/") and dst.endswith(".md"):
            text = (REPO / src).read_text(encoding="utf-8")
            target.write_text(_absolute_links(text, REPO / src, ref, tree), encoding="utf-8")
        else:
            shutil.copyfile(REPO / src, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True, help="the version both manifests carry")
    parser.add_argument("--ref", required=True, help="the git ref the skill's links point at")
    parser.add_argument("out", type=Path, help="an empty or missing directory to write")
    args = parser.parse_args(argv)
    build(args.version, args.ref, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
