"""Guards on the agent skill and the plugin manifests that distribute it.

`skills/backlot/` is the only copy of the skill; each harness gets a manifest pointing at it. Two
things can rot silently here and nowhere else. A source missing from the skill's `description` is a
skill that never triggers for it — no link is broken and no test elsewhere looks. And a link into a
`docs/` section is only as good as its `#anchor`: `tests/test_docs.py` checks that the *file*
resolves, but drops the fragment (`target.split("#", 1)[0]`), so a renamed heading leaves a link
that resolves to the top of a 300-line page instead of to the answer.

Freshness of the skill's generated routing table is not tested here — `scripts/gen_docs.py --check`
writes both it and docs/supported-sources.md, and `test_docs.py::test_generated_docs_are_current`
already gates that one command.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

from backlot.openapi import SOURCE_PREFIXES
from backlot.validation import SERVICE_SCHEMAS

REPO = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO / "skills" / "backlot"
SKILL = SKILL_DIR / "SKILL.md"

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.M)


def _gen_docs():
    """The generator, loaded by path because scripts/ is not a package.

    Its slug rule is imported rather than reimplemented. A copy of the rule here would agree with
    the generator by construction, which reads as a second opinion and is not one — it would pass
    on exactly the headings the generator gets wrong.
    """
    spec = importlib.util.spec_from_file_location("gen_docs", REPO / "scripts" / "gen_docs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frontmatter(md: Path) -> dict:
    match = _FRONTMATTER_RE.match(md.read_text())
    assert match, f"{md.relative_to(REPO)} has no YAML frontmatter block"
    return yaml.safe_load(match.group(1))


def _skill_markdown() -> list[Path]:
    return sorted(REPO.joinpath("skills").rglob("*.md"))


def _anchors(md: Path) -> set[str]:
    anchor = _gen_docs()._anchor
    return {anchor(h) for h in _HEADING_RE.findall(md.read_text())}


# Every source_type, spelled the way a human names the service. Derived from the schema keys rather
# than listed, so a new source is covered the moment its schema lands.
_SOURCE_WORDS = {source_type: source_type.replace("_", " ") for source_type in SERVICE_SCHEMAS}


def test_skill_frontmatter_matches_its_directory():
    """A skill whose `name` disagrees with its directory does not load."""
    meta = _frontmatter(SKILL)
    assert meta["name"] == SKILL_DIR.name
    assert meta["description"].strip()


@pytest.mark.parametrize("source_type,word", sorted(_SOURCE_WORDS.items()))
def test_skill_description_names_every_source(source_type, word):
    """A request names the service it wants, never Backlot.

    The skill triggers off its `description`, so a source that is not named there is a source the
    skill will not be reached for — invisible to every other check in this repo.
    """
    description = _frontmatter(SKILL)["description"].lower()
    assert word in description, (
        f"{source_type!r} is served but unnamed in the skill's description, so the skill will not "
        f"trigger for it — add {word!r}"
    )


def test_skill_link_anchors_resolve():
    """A `#anchor` into docs/ must name a heading that is still there."""
    broken = []
    for md in _skill_markdown():
        for lineno, line in enumerate(md.read_text().splitlines(), start=1):
            for target in _LINK_RE.findall(line):
                if "#" not in target or target.startswith(("http://", "https://", "mailto:")):
                    continue
                path, _, anchor = target.partition("#")
                page = (md.parent / path).resolve() if path else md
                if not anchor or not page.exists():
                    continue  # the file half is test_docs.py's job
                if anchor not in _anchors(page):
                    broken.append(f"{md.relative_to(REPO)}:{lineno} -> {target}")
    assert not broken, (
        "skill links whose #anchor names no heading in the target:\n  " + "\n  ".join(broken)
    )


# path -> how to read the skill directory out of that manifest. One row per harness: adding a
# harness adds a row here, never a second copy of the skill. Every catalog is read by plugin NAME,
# never by position — a second entry would otherwise silently move which one gets checked.
def _by_name(data: dict, name: str = "backlot") -> dict:
    (plugin,) = [p for p in data["plugins"] if p["name"] == name]
    return plugin


def _claude_marketplace(data: dict) -> str:
    return _by_name(data)["source"]


# manifest -> (how to read its path out, what that path is a path TO). The two marketplaces name a
# plugin root; Codex's plugin.json names the skills directory itself. Both are relative to the
# repository root, not to the manifest's own directory — the shape obra/superpowers and
# hashicorp/agent-skills both ship.
_MANIFESTS = {
    ".claude-plugin/marketplace.json": (_claude_marketplace, "skills/backlot/SKILL.md"),
    ".codex-plugin/plugin.json": (lambda data: data["skills"], "backlot/SKILL.md"),
    ".agents/plugins/marketplace.json": (
        lambda data: _by_name(data)["source"]["url"],
        "skills/backlot/SKILL.md",
    ),
}


@pytest.mark.parametrize(
    "manifest,locate,expected", [(m, *v) for m, v in sorted(_MANIFESTS.items())]
)
def test_every_manifest_points_at_the_real_skill(manifest, locate, expected):
    """Each harness's manifest must resolve to the one skill this repo actually holds."""
    path = REPO / manifest
    assert path.exists(), f"{manifest} is missing"
    root = (REPO / locate(json.loads(path.read_text()))).resolve()
    assert (root / expected).exists(), f"{manifest} points at {root}, which holds no {expected}"


def test_claude_plugin_manifest_names_the_plugin():
    """The catalog entry and the plugin definition have to agree on the name."""
    catalog = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["name"] in {p["name"] for p in catalog["plugins"]}


def test_the_two_plugin_manifests_agree_on_version():
    """One plugin, two harnesses: a bump that lands in one manifest and not the other is a lie.

    Neither can be derived from the package — setuptools-scm reports a dev version off the commit
    count in a checkout, which is not a version a plugin catalog can show — so they are written by
    hand, and the realistic failure is bumping one and forgetting the other.
    """
    claude = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
    codex = json.loads((REPO / ".codex-plugin" / "plugin.json").read_text())
    assert claude["version"] == codex["version"]


# Backlot's own endpoints, as opposed to the vendor prefixes gen_docs.py already checks. Matched by
# the whole underscore namespace rather than by one literal prefix: a pattern that recognises only
# the prefix in use stops matching the moment that prefix is renamed, which is the moment it has to
# fire.
_SEGMENT = r"(?:[\w-]+|<\w+>)"
_OWN_ENDPOINT_RE = re.compile(rf"/_{_SEGMENT}(?:/{_SEGMENT})*|/health\b")
_PARAM_RE = re.compile(r"<\w+>|\{\w+\}")


def _path_shape(path: str) -> str:
    """A path with every parameter segment collapsed, so prose may name its own placeholder.

    The skill writes `<key>` where FastAPI's route says `{source}`; what has to match is the route,
    not the word chosen to stand in for a value.
    """
    return _PARAM_RE.sub("{}", path)


def test_every_backlot_endpoint_the_skill_names_is_mounted():
    """The skill quotes Backlot's own endpoints as bare paths, which no link check can see.

    Rename that surface and the skill goes on telling an agent to call something that 404s, while
    every other test in this repo stays green.
    """
    from backlot.main import app

    served = {_path_shape(p) for p in app.openapi()["paths"]}
    missing = {p for md in _skill_markdown() for p in _OWN_ENDPOINT_RE.findall(md.read_text())}
    missing = {p for p in missing if _path_shape(p) not in served}
    assert not missing, f"endpoints the skill names but the app does not serve: {sorted(missing)}"


def test_every_backlot_attribute_the_skill_names_exists():
    """The same blind spot on the Python side: a renamed export leaves the snippet uncompilable."""
    import backlot

    names = set()
    for md in _skill_markdown():
        names.update(re.findall(r"\bbacklot\.(\w+)", md.read_text()))
    missing = sorted(n for n in names if not hasattr(backlot, n))
    assert not missing, f"attributes the skill names but `backlot` does not export: {missing}"


# Headings whose GitHub anchor is not guessable from the words alone: an identifier's underscores
# survive, emphasis delimiters do not, and punctuation is dropped. Written out rather than computed,
# so this is an independent statement of the rule instead of a second copy of it.
_ANCHOR_CASES = [
    ("Slack — `Bearer`", "slack--bearer"),
    (
        "Gmail, Google Drive, Docs, Sheets, Slides — `Bearer`",
        "gmail-google-drive-docs-sheets-slides--bearer",
    ),
    (
        "Documentation rules (`tests/test_docs.py` enforces all of these)",
        "documentation-rules-teststest_docspy-enforces-all-of-these",
    ),
    ("Amazon S3 — `SigV4` (`s3_access_key_id`)", "amazon-s3--sigv4-s3_access_key_id"),
    ("**Bold** and _italic_", "bold-and-italic"),
]


@pytest.mark.parametrize("heading,expected", _ANCHOR_CASES)
def test_the_anchor_rule_matches_github(heading, expected):
    """The generator's slug rule, checked against GitHub's, on the cases where they could differ.

    An underscore is a word character, so GitHub keeps the ones inside `test_docs.py` while dropping
    the pair that delimits `_italic_`. Getting that backwards writes a link that resolves to the top
    of the page instead of to the section, which no link check can see.
    """
    assert _gen_docs()._anchor(heading) == expected


def test_the_routing_table_names_every_openapi_slice_key_and_no_others():
    """`/_meta/openapi/` is keyed on the MCP bridge's names, not on `source_type`.

    Six of the eleven source_types 404 at that endpoint, so the routing table carries the real key
    per source. Asserted as a set on both sides rather than row by row: a row-wise check would need
    to know which key belongs to which source, which is the generator's own derivation, and a test
    that repeats it can only agree with it. Comparing the sets catches both ways it can go wrong —
    a key invented here, and a served key the derivation quietly drops to `—`.
    """
    rows = re.findall(r"^\| `(\w+)` \|.*\| (`\w+`|—) \|$", SKILL.read_text(), re.M)
    assert rows, "the generated routing table has no rows — did its shape change?"

    named = {cell.strip("`") for _, cell in rows if cell != "—"}
    assert named == set(SOURCE_PREFIXES), (
        f"routing table names slices {sorted(named)}, /_meta/openapi/ accepts "
        f"{sorted(SOURCE_PREFIXES)}"
    )
