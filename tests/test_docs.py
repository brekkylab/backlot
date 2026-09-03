"""Guards on the repo's own documentation: link integrity, generated-block freshness, coverage.

The endpoint/corpus/auth/config reference lives under docs/ rather than in README.md, which means
the README now carries a table of relative links. A moved or renamed page turns those into 404s
that no other test notices, so the link check below runs over every markdown file in the repo, not
just the README.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from backlot.validation import SERVICE_SCHEMAS

REPO = Path(__file__).resolve().parent.parent

# Markdown inline links and images: [text](target) / ![alt](target). Reference-style links and
# bare autolinks are deliberately not matched — this repo uses neither.
_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)")

# Targets the filesystem cannot answer for.
_EXTERNAL = ("http://", "https://", "mailto:", "#")


def _markdown_files() -> list[Path]:
    """Every markdown file in the repo, minus vendored/generated/ignored trees."""
    skip = {".venv", ".git", "node_modules", "experiments", "dist", "build", "superpowers"}
    return sorted(
        p for p in REPO.rglob("*.md") if not any(part in skip for part in p.relative_to(REPO).parts)
    )


def _relative_targets(md: Path) -> list[tuple[int, str]]:
    """(line number, target) for every link in `md` that should resolve on disk."""
    out = []
    for lineno, line in enumerate(md.read_text().splitlines(), start=1):
        for target in _LINK_RE.findall(line):
            if target.startswith(_EXTERNAL):
                continue
            out.append((lineno, target.split("#", 1)[0]))
    return out


def test_every_relative_link_resolves():
    broken = []
    for md in _markdown_files():
        for lineno, target in _relative_targets(md):
            if not target:  # a pure "#anchor" link, already stripped
                continue
            if not (md.parent / target).exists():
                broken.append(f"{md.relative_to(REPO)}:{lineno} -> {target}")
    assert not broken, "relative links that resolve to nothing:\n  " + "\n  ".join(broken)


# The README is the project's landing page, not its manual: the endpoint, corpus, auth and config
# reference all live under docs/. 180 is a ceiling on a ~140-line target — if a change needs more
# room, the content belongs in docs/ instead.
_README_MAX_LINES = 180

# "eleven sources", "12 SaaS APIs" — a count goes stale the next time a source is added, and the
# generated inventory in docs/supported-sources.md already carries the real one.
_COUNT_RE = re.compile(
    r"\b(?:eleven|twelve|thirteen|fourteen|fifteen|\d{1,2})\s+"
    r"(?:enterprise\s+)?(?:SaaS\s+)?(?:sources|services|APIs|integrations|vendors)\b",
    re.I,
)


def test_readme_stays_short():
    lines = (REPO / "README.md").read_text().splitlines()
    assert len(lines) <= _README_MAX_LINES, (
        f"README.md is {len(lines)} lines (max {_README_MAX_LINES}) — move reference content into "
        "docs/ rather than raising this limit"
    )


def test_readme_states_no_source_count():
    offenders = _COUNT_RE.findall((REPO / "README.md").read_text())
    assert not offenders, (
        f"README.md states a source count ({offenders}) — name a few sources and say "
        '"and more"; the full list is generated into docs/supported-sources.md'
    )


def test_generated_docs_are_current():
    """`scripts/gen_docs.py --check` is the single gate on docs/supported-sources.md going stale.

    Run as a subprocess rather than imported: scripts/ is not a package (it is excluded from the
    wheel by [tool.setuptools.packages.find]), and reaching it would mean bending sys.path to pull
    build tooling into the test process.
    """
    proc = subprocess.run(
        [sys.executable, "scripts/gen_docs.py", "--check"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"{proc.stdout}{proc.stderr}\nrun: python scripts/gen_docs.py"


def test_every_source_type_is_documented():
    """Adding a schema file without documenting the source must fail here.

    A new backlot/schemas/<x>.schema.json makes <x> a source_type the importer accepts, and docs
    that silently omit it start lying. The endpoint reference is the one place the full list lives,
    so it is the one place that has to stay honest.
    """
    doc = (REPO / "docs" / "supported-sources.md").read_text()
    missing = [st for st in sorted(SERVICE_SCHEMAS) if f"`{st}`" not in doc]
    assert not missing, f"source_types with no row in docs/supported-sources.md: {missing}"


def test_documented_graphql_paths_are_mounted(client):
    """The GraphQL sources are the one part of the table `/openapi.json` cannot vouch for.

    Linear and Fireflies register their single POST with ``include_in_schema=False``, so they never
    appear in the spec that scripts/gen_docs.py checks the REST prefixes against. Read the paths
    back out of the generated table and call them: a documented path that 404s is a lie, whichever
    side of it moved.
    """
    doc = (REPO / "docs" / "supported-sources.md").read_text()
    paths = re.findall(r"^\| `\w+` \|[^|]*\| `(/\S*/graphql)` \|", doc, re.M)
    assert paths, "the generated table lists no GraphQL path — did the table's shape change?"

    for path in paths:
        # Unauthenticated, with no query: any status but 404 proves the route is mounted, which is
        # all this asserts. Auth and query behaviour are tested in test_linear.py/test_fireflies.py.
        assert client.post(path, json={}).status_code != 404, (
            f"{path} is documented but not mounted"
        )


def test_configuration_page_names_exactly_the_settings():
    """`docs/configuration.md` claims to be all of them, so it has to be.

    Nothing else compares that page to `Settings`, which is how a removed setting can leave a row
    behind and how the count in its opening sentence can go stale. Both are asserted: the names,
    and the number the prose promises."""
    from backlot.config import Settings

    page = (REPO / "docs" / "configuration.md").read_text()
    expected = {f"BACKLOT_{name.upper()}" for name in Settings.model_fields}
    assert set(re.findall(r"BACKLOT_[A-Z_]+", page)) == expected
    assert len(re.findall(r"^\| `BACKLOT_", page, re.M)) == len(expected)
    stated = re.search(r"There are (\w+), and this page is all of them", page)
    assert stated, "the page no longer says how many settings there are"
    words = "zero one two three four five six seven eight nine ten eleven twelve".split()
    assert stated.group(1) == words[len(expected)], stated.group(1)


def test_env_example_names_only_real_settings():
    """`.env.example` may leave a setting out — it is a starting point, not the reference — but a
    name it lists that `Settings` does not have is a leftover, and a reader who copies the file
    gets a var the server ignores."""
    from backlot.config import Settings

    listed = set(re.findall(r"BACKLOT_[A-Z_]+", (REPO / ".env.example").read_text()))
    real = {f"BACKLOT_{name.upper()}" for name in Settings.model_fields}
    assert listed <= real, sorted(listed - real)
