"""Comparing what Backlot serves against what a vendor does.

No network: every comparison here is built from a document or an SDL in this file. The live
surface — credentials, transport, a vendor being down — is exercised by `backlot diff` itself, on
a schedule.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest
from graphql import build_schema

from backlot import cli, sigv4, store
from backlot.fidelity import (
    BREAKING,
    GAP,
    Baseline,
    FidelityError,
    Finding,
    baseline_path,
    comparisons,
    google_batch,
    google_discovery_diff,
    hubspot_catalog,
    openapi_diff,
    operations,
    s3_probe,
)
from backlot.fidelity.comparisons import (
    BATCH_PATH,
    COMPARISONS,
    GOOGLE_DISCOVERY,
    GRAPHQL,
    OPENAPI,
    PROBE,
    UNCOMPARED,
)
from backlot.fidelity.graphql_diff import backlot_schema, diff_schemas

VENDOR = """
type Query { transcripts(limit: Int, keyword: String): [Transcript!], origin: Origin }
type Transcript { id: ID!, title: String, duration: Float, status: Status }
enum Status { DONE, PROCESSING }
interface Source { id: ID! }
type Meeting implements Source { id: ID! }
type Upload implements Source { id: ID! }
union Origin = Meeting | Upload
"""


def _diff(backlot_sdl: str, vendor_sdl: str = VENDOR) -> list[Finding]:
    return diff_schemas(build_schema(backlot_sdl), build_schema(vendor_sdl))


# --------------------------------------------------------------------------- schema diff


@pytest.mark.parametrize(
    "kind,severity,path,backlot_sdl",
    [
        ("extra_field", BREAKING, "Transcript.titel", VENDOR.replace("title:", "titel:")),
        ("type_mismatch", BREAKING, "Transcript.duration", VENDOR.replace("Float", "Int")),
        (
            "extra_arg",
            BREAKING,
            "Query.transcripts(mine)",
            VENDOR.replace("limit: Int", "limit: Int, mine: Boolean"),
        ),
        (
            "arg_type_mismatch",
            BREAKING,
            "Query.transcripts(limit)",
            VENDOR.replace("limit: Int", "limit: String"),
        ),
        ("extra_type", BREAKING, "Invented", VENDOR + "\ntype Invented { id: ID! }"),
        (
            "extra_enum_value",
            BREAKING,
            "Status.CANCELLED",
            VENDOR.replace("PROCESSING", "PROCESSING, CANCELLED"),
        ),
        ("missing_field", GAP, "Transcript.title", VENDOR.replace(", title: String", "")),
        ("missing_arg", GAP, "Query.transcripts(keyword)", VENDOR.replace(", keyword: String", "")),
        (
            "missing_enum_value",
            GAP,
            "Status.PROCESSING",
            VENDOR.replace("DONE, PROCESSING", "DONE"),
        ),
        # Two unions of one name used to match on kind alone, and an interface's own fields were
        # never walked: only the implementing object's copy of them was ever reported.
        ("missing_union_member", GAP, "Origin.Upload", VENDOR.replace(" | Upload", "")),
        (
            "extra_field",
            BREAKING,
            "Source.invented",
            VENDOR.replace("{ id: ID! }", "{ id: ID!, invented: String }"),
        ),
    ],
)
def test_each_divergence_is_found_and_classified(kind, severity, path, backlot_sdl):
    """Run one-way and fields-only against Linear once, this reported a clean schema while ten
    fields and four arguments were missing — all of them on the side it did not walk."""
    found = _diff(backlot_sdl)
    hits = [f for f in found if f.kind == kind and f.path == path]
    assert hits, f"{kind} at {path} not in {[(f.kind, f.path) for f in found]}"
    assert hits[0].severity == severity


def test_identical_schemas_report_nothing_and_breaking_sorts_first():
    assert _diff(VENDOR) == []
    # `zuration` sorts AFTER the gap it creates, so a diff ordered by path alone would put the
    # breaking finding second. Renaming to something alphabetically earlier passes either way.
    ordered = _diff(VENDOR.replace("duration:", "zuration:"))
    assert [f.severity for f in ordered] == [BREAKING, GAP]


def test_a_vendor_type_backlot_never_declares_is_not_reported():
    """Backlot serves a subset; reporting every undeclared vendor type buries the real findings."""
    assert not [f for f in _diff(VENDOR, VENDOR + "\ntype Bite { id: ID! }") if "Bite" in f.path]


def test_the_compared_schema_is_the_sdl_the_server_builds_from():
    assert backlot_schema("fireflies").query_type.fields.keys() >= {"transcripts", "transcript"}


# --------------------------------------------------------------------------- baseline


def _baseline(tmp_path, findings, note=""):
    path = tmp_path / "fireflies.json"
    Baseline.empty("fireflies", ("https://api.fireflies.ai/graphql",)).write(
        path, findings, measured="2026-09-01"
    )
    if note:
        raw = json.loads(path.read_text())
        raw["acknowledged"][0]["note"] = note
        path.write_text(json.dumps(raw))
    return Baseline.load(path)


def test_a_baseline_silences_what_it_holds_and_surfaces_what_is_new(tmp_path):
    known = _diff(VENDOR.replace(", title: String", ""))
    baseline = _baseline(tmp_path, known)
    assert baseline.unacknowledged(known) == []
    regressed = _diff(VENDOR.replace("Float", "Int"))
    assert [f.kind for f in baseline.unacknowledged(regressed)] == ["type_mismatch"]
    assert [f.path for f in baseline.resolved([])] == ["Transcript.title"]


def test_rewriting_a_baseline_keeps_its_notes_and_takes_the_new_identity(tmp_path):
    known = _diff(VENDOR.replace(", title: String", ""))
    baseline = _baseline(tmp_path, known, note="deliberate: no title in the corpus")
    path = tmp_path / "fireflies.json"
    baseline.identified_as("renamed", ("https://new.invalid",)).write(
        path, known, measured="2026-10"
    )
    written = json.loads(path.read_text())
    assert written["source"] == "renamed"
    assert written["endpoints"] == ["https://new.invalid"]
    assert [e.get("note") for e in written["acknowledged"]] == [
        "deliberate: no title in the corpus"
    ]


# --------------------------------------------------------------------------- credentials


@pytest.mark.parametrize(
    "name,overrides,expected",
    [
        ("fireflies", None, "FIREFLIES_API_KEY"),
        ("fireflies", {"token": "x"}, "no credential called 'token'"),
        ("slack", {"api_key": "x"}, "takes no credential"),
        ("s3", {"api_key": "x"}, "takes no credential"),
    ],
)
def test_a_credential_is_resolved_or_refused_by_name(name, overrides, expected, monkeypatch):
    """A single `--token` was accepted by every comparison and did something for two."""
    monkeypatch.delenv("FIREFLIES_API_KEY", raising=False)
    with pytest.raises(FidelityError, match=expected):
        comparisons._resolve_credentials(COMPARISONS[name], overrides)


def test_an_explicit_credential_beats_the_environment(monkeypatch):
    monkeypatch.setenv("FIREFLIES_API_KEY", "from-env")
    assert comparisons._resolve_credentials(COMPARISONS["fireflies"], {"api_key": "x"}) == {
        "api_key": "x"
    }


def test_every_declared_credential_names_an_env_var_and_says_what_it_is():
    for name, comparison in COMPARISONS.items():
        for credential in comparison.credentials:
            assert credential.env.isupper() and credential.what, f"{name}.{credential.name}"


@pytest.mark.parametrize(
    "pairs,expected",
    [(["api_key=abc=="], {"api_key": "abc=="}), (["nonsense"], None), (["=value"], None)],
)
def test_a_credential_pair_splits_on_its_first_equals(pairs, expected):
    if expected is None:
        with pytest.raises(Exception, match="NAME=VALUE"):
            cli._credentials(pairs)
    else:
        assert cli._credentials(pairs) == expected


# --------------------------------------------------------------------------- the registry


def test_a_comparison_with_two_specs_reports_both_documents_findings(monkeypatch):
    """A source published as several documents is several comparisons' worth of surface under one
    source name, so `divergences` runs each and concatenates rather than picking one."""
    calls = []

    def fake(spec, *, timeout=120.0, seen=None):
        calls.append(spec.spec_url)
        return [Finding("gap", GAP, f"GET {spec.mount[0]}/x", "")]

    monkeypatch.setattr(comparisons.openapi_diff, "divergences", fake)
    c = comparisons.OpenAPIComparison(
        name="two",
        specs=(
            comparisons.Spec("https://a.invalid/s.json", ("/a",)),
            comparisons.Spec("https://b.invalid/s.json", ("/b",)),
        ),
    )
    found = c.divergences()
    assert calls == ["https://a.invalid/s.json", "https://b.invalid/s.json"]
    assert [f.path for f in found] == ["GET /a/x", "GET /b/x"]


def _finding(kind, path, severity=GAP):
    return Finding(kind, severity, path, "")


@pytest.mark.parametrize(
    "registry,module",
    [("OpenAPIComparison", "openapi_diff"), ("GoogleDiscoveryComparison", "google_discovery_diff")],
)
def test_a_finding_two_documents_both_declare_is_recorded_once(monkeypatch, registry, module):
    """Two documents of one source can declare the SAME path. Jira's do: 11 paths sit outside
    `/rest/api/{2,3}` -- `/rest/atlassian-connect/1/*`, `/rest/forge/1/*` and one internal worklog
    route -- so both Atlassian documents carry them identically and `missing_operation` reports
    each twice.

    That is not the case the mirrored v2/v3 entries make: `/rest/api/2/attachment/{id}` and
    `/rest/api/3/attachment/{id}` are different paths a client can call, and both belong in the
    baseline. These are one path recorded twice, and a baseline keyed on `kind:path` cannot hold
    the second -- so the file would claim more than it can load, and a genuinely new finding on
    one of those paths would print and count twice."""
    shared = _finding("missing_operation", "GET /rest/forge/1/app/properties")

    def fake(spec, *, timeout=120.0, **kw):
        return [shared, _finding("missing_operation", f"GET {spec.mount[0]}/own")]

    monkeypatch.setattr(getattr(comparisons, module), "divergences", fake)
    c = getattr(comparisons, registry)(
        name="two",
        specs=(
            comparisons.Spec("https://a.invalid/s.json", ("/a",)),
            comparisons.Spec("https://b.invalid/s.json", ("/b",)),
        ),
    )
    found = c.divergences()
    assert len(found) == len({f.key for f in found}), [f.path for f in found]
    assert sorted(f.path for f in found) == [
        "GET /a/own",
        "GET /b/own",
        "GET /rest/forge/1/app/properties",
    ]


def test_findings_from_several_documents_come_back_in_one_order(monkeypatch):
    """`diff_operations` sorts breaking-first-then-path, and concatenating per-document results
    loses that across documents -- which leaves the baseline file in an order `--update-baseline`
    does not reproduce, so the next real change arrives buried in a reordering of the whole file."""

    def fake(spec, *, timeout=120.0, **kw):
        if spec.mount[0] == "/a":
            return [
                _finding("missing_operation", "GET /z"),
                _finding("extra_param", "GET /a?x", BREAKING),
            ]
        return [
            _finding("missing_operation", "GET /b"),
            _finding("extra_param", "GET /b?y", BREAKING),
        ]

    monkeypatch.setattr(comparisons.openapi_diff, "divergences", fake)
    c = comparisons.OpenAPIComparison(
        name="two",
        specs=(
            comparisons.Spec("https://a.invalid/s.json", ("/a",)),
            comparisons.Spec("https://b.invalid/s.json", ("/b",)),
        ),
    )
    found = c.divergences()
    assert [f.severity for f in found] == [BREAKING, BREAKING, GAP, GAP]
    assert [f.path for f in found] == ["GET /a?x", "GET /b?y", "GET /b", "GET /z"]


def test_every_comparison_names_the_documents_it_was_measured_against():
    """`endpoints` is the baseline's identity, and a source can now have more than one document
    behind it -- a single joined string could not tell a source that GAINED a document from one
    whose document moved.

    Each entry must be DISTINCT, which `spec_url` alone does not give: HubSpot's is an index, and
    both of its Specs address it, so naming documents by their fetch URL reported the same string
    twice and identified neither. `Spec.endpoint` qualifies it with what `resolve_url` selects."""
    for name, c in COMPARISONS.items():
        assert isinstance(c.endpoints, tuple) and c.endpoints, name
        assert all(e.startswith("http") for e in c.endpoints), name
        assert len(set(c.endpoints)) == len(c.endpoints), f"{name}: {c.endpoints}"
        # And STABLE across processes: an index-addressed spec renders its resolver into this
        # string, so one rendering as the default `<function … at 0x…>` would write a fresh memory
        # address into the baseline on every run — churning the identity it exists to hold still.
        assert not any(" at 0x" in e for e in c.endpoints), f"{name}: {c.endpoints}"


def test_a_baseline_refuses_one_endpoint_passed_unwrapped(tmp_path):
    """A str is iterable, so `list("https://…")` spreads it one element per character and writes a
    file nothing downstream is shaped wrongly enough to complain about. The registry is guarded in
    both directions already; a hand-built Baseline is the way around those guards.

    All three construction paths, because `load` is the one that turns a FILE into the spread and
    it converts before `__post_init__` can see a string -- so it carries its own check, and the
    two that share a helper are not evidence for the one that does not."""
    with pytest.raises(TypeError, match="tuple of URLs"):
        Baseline.empty("s", "https://one.invalid")
    with pytest.raises(TypeError, match="tuple of URLs"):
        Baseline.empty("s", ()).identified_as("s", "https://one.invalid")

    path = tmp_path / "b.json"
    path.write_text(
        json.dumps(
            {"source": "s", "endpoints": "https://one.invalid", "measured": "", "acknowledged": []}
        )
    )
    with pytest.raises(TypeError, match="tuple of URLs"):
        Baseline.load(path)

    # and the shapes a real file carries still load: a list, an absent key, and the legacy spelling
    for endpoints, want in [(["https://a", "https://b"], ("https://a", "https://b")), (None, ())]:
        raw = {"source": "s", "measured": "", "acknowledged": []}
        if endpoints is not None:
            raw["endpoints"] = endpoints
        path.write_text(json.dumps(raw))
        assert Baseline.load(path).endpoints == want


def test_a_baseline_round_trips_every_endpoint_it_names(tmp_path):
    p = tmp_path / "b.json"
    ends = ("https://a.invalid", "https://b.invalid")
    Baseline.empty("s", ends).write(p, [], measured="2026-01-01")
    assert Baseline.load(p).endpoints == ends
    assert json.loads(p.read_text())["endpoints"] == list(ends)


def test_a_google_source_is_compared_against_every_api_it_is_served_through():
    """A Drive file is also read through Docs, Sheets and Slides. Comparing only Drive left three
    whole API families measured against nothing: a Sheets response shape could be rewritten and
    `backlot diff --source google_drive` would still answer `0 new`."""
    mounts = {m for s in COMPARISONS["google_drive"].specs for m in s.mount}
    assert mounts == {"/drive/v3", "/docs/v1", "/sheets/v4", "/slides/v1"}


def test_hubspot_compares_its_v4_associations_surface_too():
    """Associations are their own API at their own version with their own published document. The
    CRM v3 document does not declare them, so mounting only `crm/v3` left the association read
    compared against nothing."""
    mounts = {m for s in COMPARISONS["hubspot"].specs for m in s.mount}
    assert mounts == {"/hubspot/crm/v3", "/hubspot/crm/v4"}


def test_jira_compares_both_rest_versions_against_their_own_documents():
    """Backlot serves `/rest/api/2` because the clients call it: `atlassian-python-api` hardcodes
    `api_version = "2"` in its Jira constructor and the `jira` PyPI client defaults
    `rest_api_version` to `"2"`, probing `/rest/api/2/serverInfo` on connect.

    Atlassian publishes a document per version — the file naming is
    `swagger[-<apiVersion>].<oasVersion>.json`, so the suffix-less one is the v2 API — and each is
    compared against its own. Measured: comparing the v2 paths against the v3 document instead
    reports all six served operations as surface Backlot invented."""
    by_mount = {s.mount[0]: s.spec_url for s in COMPARISONS["jira"].specs}
    assert set(by_mount) == {"/atlassian/rest/api/2", "/atlassian/rest/api/3"}
    assert by_mount["/atlassian/rest/api/2"].endswith("/swagger.v3.json")
    assert by_mount["/atlassian/rest/api/3"].endswith("/swagger-v3.v3.json")


def test_specs_sharing_an_index_read_it_once_per_run(monkeypatch):
    """HubSpot reaches every one of its documents through one index, so both of its specs address
    the same `spec_url` and `resolve_url` picks the document out of it.

    Measured: that index is 138 KB and answers in ~2.2s, so fetching it per spec spends a whole
    extra vendor round trip on a document already in hand. Memoized per RUN and never wider -- the
    index must be re-read every run, which is why it is not pinned (see `hubspot_catalog`)."""
    INDEX = "https://index.invalid/specs"
    fetched = []

    def fake_fetch(url, *, timeout=120.0):
        fetched.append(url)
        return {"openapi": "3.0.0", "paths": {}} if url != INDEX else {"index": True}

    monkeypatch.setattr(comparisons.openapi_diff, "fetch_json", fake_fetch)
    monkeypatch.setattr(
        comparisons.openapi_diff, "from_openapi", lambda doc: {("get", "/x"): object()}
    )
    monkeypatch.setattr(comparisons.openapi_diff, "from_backlot", lambda *a, **k: {})
    monkeypatch.setattr(comparisons.openapi_diff, "diff_operations", lambda served, vendor: [])

    c = comparisons.OpenAPIComparison(
        name="two",
        specs=(
            comparisons.Spec(INDEX, ("/a",), resolve_url=lambda d: "https://doc.invalid/a.json"),
            comparisons.Spec(INDEX, ("/b",), resolve_url=lambda d: "https://doc.invalid/b.json"),
        ),
    )
    c.divergences()

    assert fetched.count(INDEX) == 1, fetched
    assert sorted(fetched) == sorted(
        [INDEX, "https://doc.invalid/a.json", "https://doc.invalid/b.json"]
    )


def test_the_registry_names_exactly_the_source_types_backlot_serves():
    """A VOCABULARY check, not a coverage one: this says every source_type has some comparison,
    not that every served path is under one. `test_every_served_path_is_compared_or_says_why_not`
    asks the second question, and `google_drive` is why they are different — one source type,
    five vendor APIs.

    Fidelity does not get to invent a source: `store.SOURCE_TABLE` is the canonical list, and
    the same `source_type` a BYO record carries. `google_batch` is the one comparison that is not
    a source at all — Backlot's batch routes stand in for five Google APIs and sit under no
    source's mount — and it is spelled out here rather than excused by a rule, so a second one
    cannot arrive as a typo.

    `cli.FIDELITY_SOURCES` is held to the same list. It exists so `--help` can name the sources
    without importing `backlot.fidelity` on every other command, and it is what a reader is told
    the command takes — a source added to one and not the other leaves `--help` naming a set the
    command does not validate against.
    """
    assert (
        set(COMPARISONS) == set(store.SOURCE_TABLE) | {"google_batch"} == set(cli.FIDELITY_SOURCES)
    )


def test_every_comparison_is_registered_once_as_the_class_its_registry_implies():
    registries = [
        (OPENAPI, comparisons.OpenAPIComparison),
        (GOOGLE_DISCOVERY, comparisons.GoogleDiscoveryComparison),
        (GRAPHQL, comparisons.GraphQLComparison),
        (PROBE, comparisons.ProbeComparison),
        (BATCH_PATH, comparisons.BatchPathComparison),
    ]
    assert sum(len(r) for r, _ in registries) == len(COMPARISONS)
    for registry, expected in registries:
        for name, comparison in registry.items():
            assert isinstance(comparison, expected), f"{name} is a {type(comparison).__name__}"


def test_every_comparison_ships_a_baseline():
    """Shipped so an installed copy can be compared without the repository."""
    for name in COMPARISONS:
        assert Baseline.load(baseline_path(name)).source == name


def test_every_mount_selects_something_backlot_serves():
    """The converse of `test_every_served_path_is_compared_or_says_why_not`, and NOT implied by it.

    That test is one-directional: every served path is under some mount. A mount matching nothing
    escapes it whenever another mount already covers the same paths -- and such a mount diffs an
    EMPTY served surface against a whole vendor document, which reports as nothing but
    `missing_operation` gaps. That looks exactly like an unimplemented surface, so
    `--update-baseline` acknowledges it in bulk and the dead mount compares nothing forever.

    Per SPEC, not per comparison: a source with four documents and one bad mount would otherwise
    pass on the strength of its other three."""
    from backlot.main import app

    served = list(app.openapi()["paths"])
    for name, comparison in COMPARISONS.items():
        for mount in _comparison_mounts(comparison):
            assert any(p.startswith(mount) for p in served), f"{name}: {mount} selects nothing"


def _comparison_mounts(comparison) -> tuple[str, ...]:
    """Every served path prefix a comparison speaks for, whichever kind it is."""
    specs = getattr(comparison, "specs", None)
    if specs is not None:
        return tuple(m for s in specs for m in s.mount)
    return tuple(getattr(comparison, "mount", ()))


def test_every_served_path_is_compared_or_says_why_not():
    """The coverage guarantee the tests beside this one only appeared to give.

    They count SOURCE TYPES, which is the corpus dimension. `google_drive` is one source type
    served through five vendor APIs, so Docs, Sheets and Slides sat compared against nothing while
    both tests passed — a Sheets response shape could be rewritten and `backlot diff` would still
    answer `0 new`. This counts served PATHS, which is what a comparison actually covers.

    Every path lands in exactly one bucket: under some document's mount, probe-compared, or
    declared in `UNCOMPARED` with a reason. A path in none of them is a router someone added
    without asking what checks it.
    """
    from backlot.main import app

    mounts = [m for c in COMPARISONS.values() for m in _comparison_mounts(c)]
    unclassified = [
        p
        for p in sorted(app.openapi()["paths"])
        if not any(p.startswith(m) for m in mounts)
        and not any(p == u or p.startswith(u + "/") for u in UNCOMPARED)
    ]
    assert unclassified == [], (
        "compared against nothing, and not declared in UNCOMPARED: " + ", ".join(unclassified)
    )


def test_no_uncompared_declaration_outlives_its_route():
    """An entry kept after its route is gone is a reason nobody is reading any more.

    And an entry may not sit under a comparison's mount. Nothing else stops one claiming a surface
    that IS compared — the coverage check only asks whether a path is in some bucket, not whether
    it is in the right one — which would leave the list unreadable at face value."""
    from backlot.main import app

    served = list(app.openapi()["paths"])
    mounts = [m for c in COMPARISONS.values() for m in _comparison_mounts(c)]
    for prefix, reason in UNCOMPARED.items():
        assert any(p == prefix or p.startswith(prefix + "/") for p in served), prefix
        assert reason.strip(), prefix
        covered = [m for m in mounts if prefix.startswith(m) or m.startswith(prefix)]
        assert not covered, f"{prefix} is declared uncompared but sits under {covered}"


def test_every_acknowledged_breaking_divergence_carries_its_reasoning():
    """No flag writes one: a breaking entry is a hand edit, and the note is what a reviewer reads
    in the diff instead of a flag on a command nobody kept."""
    for name in COMPARISONS:
        for entry in json.loads(baseline_path(name).read_text())["acknowledged"]:
            if entry["severity"] == BREAKING:
                assert entry.get("note"), f"{name}: {entry['kind']} {entry['path']}"


def test_the_dispatcher_hands_over_for_every_kind_the_registry_holds(monkeypatch):
    """The guard in `divergences` is an isinstance check over a tuple written by hand, so a kind
    added to the registry and not to that tuple is refused on a NIGHTLY run and nowhere else: the
    suite stays green and `backlot diff --source <it>` answers "not a kind of comparison this knows
    how to run"."""
    monkeypatch.setenv("FIREFLIES_API_KEY", "x")
    monkeypatch.setenv("LINEAR_API_KEY", "x")
    for kind in {type(c) for c in COMPARISONS.values()}:
        monkeypatch.setattr(
            kind, "divergences", lambda self, credentials=None, *, timeout=120.0: []
        )
    for name, comparison in COMPARISONS.items():
        assert comparisons.divergences(comparison) == [], name


def test_a_kind_of_comparison_the_dispatcher_does_not_know_says_so():
    with pytest.raises(FidelityError, match="not a kind of comparison"):
        comparisons.divergences(object())


def test_the_package_exports_exactly_what_the_command_needs():
    """Re-exporting a submodule's internals invites a caller to assemble a comparison by hand and
    get a different answer than the command does."""
    import backlot.fidelity as package

    surface = {
        "BREAKING",
        "GAP",
        "COMPARISONS",
        "Baseline",
        "CredentialsMissing",
        "FidelityError",
        "Finding",
        "baseline_path",
        "divergences",
    }
    assert set(package.__all__) == surface
    assert all(hasattr(package, name) for name in surface)
    imported = {
        alias.name
        for node in ast.walk(ast.parse(Path(cli.__file__).read_text()))
        if isinstance(node, ast.ImportFrom) and node.module == "backlot.fidelity"
        for alias in node.names
    }
    assert imported, "the command imports nothing from the package; the parse is wrong"
    assert imported <= surface, imported


# --------------------------------------------------------------------------- published specs


A_DISCOVERY_DOC = {
    "servicePath": "drive/v3/",
    "parameters": {"fields": {"location": "query"}, "alt": {"location": "query"}},
    "resources": {
        "files": {
            "methods": {
                "list": {
                    "httpMethod": "GET",
                    "path": "files",
                    "parameters": {"q": {"location": "query"}},
                },
                "get": {"httpMethod": "GET", "path": "files/{fileId}", "parameters": {}},
            },
            "resources": {
                "perms": {
                    "methods": {"list": {"httpMethod": "GET", "path": "files/{fileId}/permissions"}}
                }
            },
        }
    },
}

AN_OPENAPI_DOC = {
    "paths": {
        "/conversations.list": {
            "get": {"parameters": [{"name": "limit", "in": "query"}, {"$ref": "#/x/cursor"}]},
            "parameters": [{"name": "token", "in": "query"}],
        }
    },
    "x": {"cursor": {"name": "cursor", "in": "query"}},
}


def test_a_discovery_document_joins_its_service_path_and_nests_its_resources():
    found = google_discovery_diff.from_google_discovery(A_DISCOVERY_DOC)
    assert {p for _, p in found} == {
        "drive/v3/files",
        "drive/v3/files/{}",
        "drive/v3/files/{}/permissions",
    }
    # `fields` and `alt` are declared once for the document; reading only the per-method block
    # reported both as surface Backlot invented, which is how this first accused Drive's `fields`.
    assert {"fields", "alt", "q"} <= set(found[("get", "drive/v3/files")].params)


def test_an_openapi_document_follows_refs_and_keeps_path_level_parameters():
    found = openapi_diff.from_openapi(AN_OPENAPI_DOC)
    assert found[("get", "conversations.list")].params == frozenset({"limit", "cursor", "token"})


def test_a_placeholders_name_is_not_a_divergence():
    assert operations.canonical("/users/{userId}/x") == operations.canonical("users/{user_id}/x")


def test_the_mount_comes_off_only_where_the_vendor_does_not_repeat_it():
    """Slack's spec starts at /conversations.list so its mount comes off. Google differs per
    DOCUMENT, which is why `strip` sits on the Spec: Drive's own document spells `drive/v3`, so
    nothing comes off there, while the Sheets document declares an empty `servicePath` and spells
    `v4/spreadsheets/...` itself, so its mount does. Jira and Confluence share /atlassian and must
    not capture each other."""
    served = {
        "paths": dict.fromkeys(
            [
                "/slack/api/conversations.list",
                "/drive/v3/files",
                "/sheets/v4/spreadsheets/{spreadsheet_id}",
                "/atlassian/rest/api/3/field",
                "/atlassian/wiki/rest/api/space",
            ],
            {"get": {}},
        )
    }

    def mounted(name):
        c = OPENAPI.get(name) or GOOGLE_DISCOVERY[name]
        return {
            p
            for spec in c.specs
            for _, p in operations.from_backlot(served, spec.mount, spec.strip)
        }

    assert mounted("slack") == {"conversations.list"}
    assert mounted("google_drive") == {"drive/v3/files", "v4/spreadsheets/{}"}
    assert mounted("jira") == {"rest/api/3/field"}
    assert mounted("confluence") == {"wiki/rest/api/space"}


def test_operation_divergences_are_classified_like_schema_ones():
    served = operations.from_backlot(
        {"paths": {"/x/a": {"get": {"parameters": [{"name": "n", "in": "query"}]}}}}, ["/x"], "/x"
    )
    vendor = {
        ("get", "a"): operations.Operation("get", "a", frozenset({"m"})),
        ("get", "b"): operations.Operation("get", "b", frozenset()),
    }
    by_kind = {f.kind: f.severity for f in operations.diff_operations(served, vendor)}
    assert by_kind == {"extra_param": BREAKING, "missing_param": GAP, "missing_operation": GAP}


CATALOG = {
    "results": [
        {
            "name": "Custom Objects",
            "versions": [
                {"version": "2026-03", "openApi": "https://example.invalid/preview"},
                {"version": "3", "openApi": "https://example.invalid/v3"},
            ],
        }
    ]
}


def test_an_index_is_read_on_every_run_rather_than_pinned():
    """Measured 2026-09-01: every past HubSpot release id still serves its own frozen document, so
    a pinned URL never 404s — it reports no drift forever. Only HubSpot needs the indirection."""
    assert hubspot_catalog.entry("Custom Objects", "3")(CATALOG) == "https://example.invalid/v3"
    assert {n for n, c in OPENAPI.items() if any(s.resolve_url for s in c.specs)} == {"hubspot"}


@pytest.mark.parametrize(
    "api,version,expected",
    [("Custom Objects", "99", "publishes no version"), ("Nothing", "3", "no API named")],
)
def test_an_index_miss_says_which_half_was_wrong(api, version, expected):
    with pytest.raises(FidelityError, match=expected):
        hubspot_catalog.entry(api, version)(CATALOG)


# --------------------------------------------------------------------------- S3 probe


S3_MODEL = {
    "operations": {
        "ListObjectsV2": {"http": {"method": "GET", "requestUri": "/{Bucket}?list-type=2"}},
        "GetBucketAcl": {"http": {"method": "GET", "requestUri": "/{Bucket}?acl"}},
        "GetObjectTagging": {"http": {"method": "GET", "requestUri": "/{Bucket}/{Key+}?tagging"}},
        "ListBuckets": {"http": {"method": "GET", "requestUri": "/"}},
        "PutBucketAcl": {"http": {"method": "PUT", "requestUri": "/{Bucket}?acl"}},
        "ListParts": {
            "http": {"method": "GET", "requestUri": "/{Bucket}/{Key+}"},
            "input": {"shape": "ListPartsRequest"},
        },
        "GetObject": {
            "http": {"method": "GET", "requestUri": "/{Bucket}/{Key+}"},
            "input": {"shape": "GetObjectRequest"},
        },
    },
    "shapes": {
        "ListPartsRequest": {
            "required": ["Bucket", "Key", "UploadId"],
            "members": {"UploadId": {"location": "querystring", "locationName": "uploadId"}},
        },
        "GetObjectRequest": {
            "required": ["Bucket", "Key"],
            "members": {"VersionId": {"location": "querystring", "locationName": "versionId"}},
        },
    },
}


def test_the_s3_model_yields_read_operations_keyed_by_what_selects_them():
    found = {o.name: o for o in s3_probe.operations(S3_MODEL)}
    assert "PutBucketAcl" not in found  # a write Backlot refuses is the right answer
    assert (found["GetBucketAcl"].target, found["GetBucketAcl"].query) == ("bucket", "acl")
    assert found["GetObjectTagging"].target == "object"
    assert found["ListBuckets"].query == ""
    # ListParts shares GetObject's `/{Bucket}/{Key+}` and is selected by a REQUIRED querystring
    # member, so read from the URI alone it came out bare, was skipped as "the bare form", and was
    # never asked — while the server answers it with the object body.
    assert found["ListParts"].query == "uploadId"
    assert found["GetObject"].query == ""  # its `versionId` is optional, not another operation
    # `?analytics` is two operations; keyed on the request alone a baseline would silence one.
    shared = {
        "operations": {
            n: {"http": {"method": "GET", "requestUri": "/{Bucket}?analytics"}}
            for n in ("GetBucketAnalyticsConfiguration", "ListBucketAnalyticsConfigurations")
        }
    }
    assert len({str(o) for o in s3_probe.operations(shared)}) == 2


def test_an_operation_answered_with_another_operations_body_is_breaking(monkeypatch):
    """The failure a path diff cannot see: not refused, not implemented, answered 200 with whatever
    the catch-all route returns."""
    listing = '<?xml version="1.0"?><ListBucketResult><Name>b</Name></ListBucketResult>'
    error = '<?xml version="1.0"?><Error><Code>NotImplemented</Code></Error>'

    def fake(method, url, headers=None, timeout=None):
        refused = "tagging" in url
        return httpx.Response(400 if refused else 200, text=error if refused else listing)

    monkeypatch.setattr(s3_probe.httpx, "request", fake)
    found = {
        f.path.split(":")[0]: f
        for f in s3_probe.probe(
            "http://x", "ak", "sk", s3_probe.operations(S3_MODEL), bucket="b", key="k"
        )
    }
    assert (found["GetBucketAcl"].kind, found["GetBucketAcl"].severity) == (
        "silent_fallthrough",
        BREAKING,
    )
    assert (found["GetObjectTagging"].kind, found["GetObjectTagging"].severity) == (
        "missing_operation",
        GAP,
    )


def test_a_server_that_cannot_be_asked_is_not_a_divergence(monkeypatch):
    """The probe's answers come from a server Backlot started, and one that does not answer means
    no comparison ran. Left to escape as a `KeyError` it reaches `backlot diff` as status 1, the
    status a scheduled run files a divergence for."""
    monkeypatch.setattr(s3_probe.httpx, "get", lambda *a, **k: httpx.Response(503, text="warming"))
    with pytest.raises(FidelityError, match="no S3 credentials"):
        s3_probe.run("http://x", {"operations": {}})

    def _boom(*a, **k):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(s3_probe.httpx, "request", _boom)
    with pytest.raises(FidelityError, match="went unanswered"):
        s3_probe.probe("http://x", "ak", "sk", s3_probe.operations(S3_MODEL), bucket="b", key="k")


def test_the_probe_signs_with_the_module_that_verifies_the_signature():
    headers = s3_probe._sign("GET", "http://localhost:8000/s3/b?acl", "AKIAEXAMPLE", "secret")
    parsed = sigv4.parse_authorization(headers["authorization"])
    assert parsed and parsed["signature"] and "x-amz-content-sha256" in parsed["signed_headers"]


def test_a_probe_declares_its_prober_and_the_dispatcher_only_hands_over(monkeypatch):
    """Deriving `backlot.fidelity.{name}_probe` from the name would fail on a nightly run rather
    than here."""
    with pytest.raises(TypeError):
        comparisons.ProbeComparison(name="x", spec_url="https://example.invalid")

    seen = {}

    def fake_run(base_url, model, timeout):
        seen.update(base_url=base_url, model=model, timeout=timeout)
        return []

    monkeypatch.setattr(s3_probe, "fetch_json", lambda url, timeout: {"operations": {}})
    probe = comparisons.ProbeComparison("x", "https://e.invalid", fake_run)
    assert comparisons.divergences(probe, timeout=5) == []
    assert seen["model"] == {"operations": {}} and seen["timeout"] == 5
    assert seen["base_url"].startswith("http")


# --------------------------------------------------------------------------- the batch endpoint


# What each discovery document declared when this was written, measured 2026-09-12. The real
# comparison reads these off the vendor; here they are the vendor's side so the test says what it
# is about, and so the fixture is the thing that goes stale rather than the check.
BATCH_PATHS = {
    "https://gmail.googleapis.com/$discovery/rest?version=v1": ("gmail:v1", "batch"),
    "https://www.googleapis.com/discovery/v1/apis/drive/v3/rest": ("drive:v3", "batch/drive/v3"),
    "https://www.googleapis.com/discovery/v1/apis/docs/v1/rest": ("docs:v1", "batch"),
    "https://www.googleapis.com/discovery/v1/apis/sheets/v4/rest": ("sheets:v4", "batch"),
    "https://www.googleapis.com/discovery/v1/apis/slides/v1/rest": ("slides:v1", "batch"),
}


def _serve_documents(monkeypatch, *declared: dict) -> tuple[comparisons.Spec, ...]:
    """One `Spec` per document passed, at a made-up URL each, fetching back that document."""
    specs = tuple(comparisons.Spec(f"https://doc.invalid/{i}", ()) for i, _ in enumerate(declared))
    by_url = {s.spec_url: d for s, d in zip(specs, declared)}
    monkeypatch.setattr(google_batch, "fetch_json", lambda url, timeout=120.0: by_url[url])
    return specs


@pytest.mark.parametrize(
    "declared,expected",
    [
        ({"batchPath": "batch"}, "batch"),
        ({"batchPath": "batch/drive/v3"}, "batch/drive/v3"),
        # Canonical on both sides, so a slash either end is not a finding on its own.
        ({"batchPath": "/batch"}, "batch"),
        ({"batchPath": "batch/drive/v3/"}, "batch/drive/v3"),
        ({}, ""),
        ({"batchPath": ""}, ""),
        ({"batchPath": None}, ""),
    ],
)
def test_a_batch_path_is_what_the_document_declares_in_its_top_level_field(declared, expected):
    assert google_batch.batch_path(declared) == expected


@pytest.mark.parametrize(
    "route,declared,answered",
    [
        ("batch", "batch", True),
        ("batch/{}/{}", "batch/drive/v3", True),
        ("batch/{}/{}", "batch/gmail/v1", True),
        # Segment counts have to match, or a route swallows a value that moved under it.
        ("batch", "batch/drive/v3", False),
        ("batch/{}/{}", "batch", False),
        ("batch/{}/{}", "batch/drive/v3/files", False),
        # A literal segment is compared literally: only the placeholders are wild.
        ("batch/{}/{}", "upload/drive/v3", False),
    ],
)
def test_a_route_answers_a_batch_path_segment_by_segment(route, declared, answered):
    assert google_batch.answers(route, declared) is answered


@pytest.mark.parametrize(
    "doc,expected",
    [
        ({"id": "gmail:v1", "name": "gmail", "version": "v1"}, "gmail:v1"),
        ({"name": "gmail", "version": "v1"}, "gmail:v1"),
        ({}, "https://doc.invalid/0"),
    ],
)
def test_a_document_is_identified_by_what_it_calls_itself(doc, expected):
    """Not by the URL it was fetched from. Measured 2026-09-12, Google answers the document that
    calls itself `gmail:v1` at both `gmail.googleapis.com/$discovery/rest?version=v1` and
    `www.googleapis.com/discovery/v1/apis/gmail/v1/rest`, so a registry repointed from one to the
    other would otherwise read as a new divergence on an API nothing changed about."""
    assert google_batch.document_id(doc, "https://doc.invalid/0") == expected


def test_every_google_document_declares_a_batch_path_backlot_answers(monkeypatch):
    """The whole comparison over what the five documents declared when this was written. It reads
    every document the Google path diffs read, and Backlot's two route shapes answer all five
    values between them, so there is nothing to report."""
    comparison = comparisons.COMPARISONS["google_batch"]
    # First, so a registry repointed at another URL fails here rather than as a KeyError inside
    # the fetch below.
    assert list(comparison.endpoints) == list(BATCH_PATHS)
    asked = []

    def fake_fetch(url, timeout=120.0):
        asked.append(url)
        name, path = BATCH_PATHS[url]
        return {"id": name, "batchPath": path}

    monkeypatch.setattr(google_batch, "fetch_json", fake_fetch)
    assert google_batch.divergences(comparison) == []
    assert asked == list(BATCH_PATHS)


def test_a_document_that_declares_no_batch_path_is_breaking(monkeypatch):
    """Google dropping batch for one API. Backlot goes on answering `/batch` for it, which is
    surface the vendor's own document no longer describes.

    The other two documents keep both routes answering something, so the only finding is this one.
    """
    specs = _serve_documents(
        monkeypatch,
        {"id": "docs:v1", "batchPath": "batch"},
        {"id": "drive:v3", "batchPath": "batch/drive/v3"},
        {"id": "gmail:v1"},
    )
    found = google_batch.divergences(comparisons.BatchPathComparison(name="x", documents=specs))
    assert [(f.kind, f.severity, f.path) for f in found] == [
        ("extra_batch_api", BREAKING, "gmail:v1")
    ]


def test_a_batch_path_that_moved_is_a_gap_and_leaves_its_route_standing_for_nothing(monkeypatch):
    """A value moving to a THIRD shape — one that is neither `batch` nor `batch/<api>/<version>`.

    Two findings, and they say different things. Backlot does not answer where Drive now batches,
    which is the gap; and `/batch/{api}/{version}` is then a route no document declares a value
    for, which is how a shape Backlot serves for nobody stops being invisible.

    Drive moving to its own host is the OTHER way the second finding arrives, and it arrives
    alone — see the test below.
    """
    specs = _serve_documents(
        monkeypatch,
        {"id": "gmail:v1", "batchPath": "batch"},
        {"id": "drive:v3", "batchPath": "v3/batch"},
    )
    found = google_batch.divergences(comparisons.BatchPathComparison(name="x", documents=specs))
    assert [(f.kind, f.severity, f.path) for f in found] == [
        ("extra_batch_route", BREAKING, "/batch/{}/{}"),
        ("missing_batch_path", GAP, "drive:v3 v3/batch"),
    ]
    assert "v3/batch" in found[1].detail


def test_a_value_that_moves_again_is_not_covered_by_the_first_move_s_acknowledgement(monkeypatch):
    """A gap is acknowledged by identity alone — `Baseline.unacknowledged` reads a gap's detail
    for nothing — so the value has to be part of the identity. Keyed on the document only, the
    entry `--update-baseline` wrote for `v3/batch` would answer for `v4/batch` too, and the second
    move would pass as `0 new`."""
    (first,) = [
        f
        for f in google_batch.divergences(
            comparisons.BatchPathComparison(
                name="x",
                documents=_serve_documents(
                    monkeypatch, {"id": "drive:v3", "batchPath": "v3/batch"}
                ),
            )
        )
        if f.kind == "missing_batch_path"
    ]
    acknowledged = Baseline("x", (), "", {first.key: first})
    (second,) = [
        f
        for f in google_batch.divergences(
            comparisons.BatchPathComparison(
                name="x",
                documents=_serve_documents(
                    monkeypatch, {"id": "drive:v3", "batchPath": "v4/batch"}
                ),
            )
        )
        if f.kind == "missing_batch_path"
    ]
    assert acknowledged.unacknowledged([first]) == []
    assert acknowledged.unacknowledged([second]) == [second]


def test_a_route_no_document_declares_any_more_is_breaking_on_its_own(monkeypatch):
    """Drive moving to its own host, which is the case `docs/fidelity.md` names for this finding.

    Its value would then stop carrying the `drive/v3` discriminator and read `batch`, like the
    four APIs already on their own hosts. Every value is answered, so there is no gap — and
    `/batch/{api}/{version}` is left standing for nothing, which is the whole finding.
    """
    specs = _serve_documents(
        monkeypatch,
        *(
            {"id": name, "batchPath": "batch"}
            for name in ("gmail:v1", "drive:v3", "docs:v1", "sheets:v4", "slides:v1")
        ),
    )
    found = google_batch.divergences(comparisons.BatchPathComparison(name="x", documents=specs))
    assert [(f.kind, f.severity, f.path) for f in found] == [
        ("extra_batch_route", BREAKING, "/batch/{}/{}")
    ]


def test_two_documents_that_call_themselves_the_same_thing_report_once(monkeypatch):
    """Google answers the document that calls itself `gmail:v1` at two URLs, so a registry holding
    both reads one document twice.

    The module reports what it read, once per document; the comparison passes that through
    `_one_report`, which is where every kind's report is keyed. A baseline keyed on `kind:path`
    cannot hold the second copy, so leaving it in makes the file claim more than it can load."""
    specs = _serve_documents(
        monkeypatch,
        {"id": "gmail:v1"},
        {"id": "gmail:v1"},
        {"id": "drive:v3", "batchPath": "batch/drive/v3"},
        {"id": "docs:v1", "batchPath": "batch"},
    )
    comparison = comparisons.BatchPathComparison(name="x", documents=specs)
    assert [f.path for f in google_batch.divergences(comparison)] == ["gmail:v1", "gmail:v1"]
    assert [f.path for f in comparison.divergences()] == ["gmail:v1"]


def test_two_comparisons_over_one_document_read_it_once():
    """A source's Specs may address one document more than once — HubSpot's two both address its
    API index — and here, where no resolver picks a different document out of it, that is one
    document twice: a vendor round trip per run spent re-reading what is already in hand. What a
    repeat does to the report is the keying above, not this."""
    shared = "https://doc.invalid/shared"
    compared = [
        comparisons.GoogleDiscoveryComparison(name="a", specs=(comparisons.Spec(shared, ("/a",)),)),
        comparisons.GoogleDiscoveryComparison(
            name="b",
            specs=(
                comparisons.Spec(shared, ("/b",)),
                comparisons.Spec("https://doc.invalid/own", ()),
            ),
        ),
    ]
    assert [d.spec_url for d in comparisons._documents_of(compared)] == [
        shared,
        "https://doc.invalid/own",
    ]


def test_the_batch_comparison_reads_the_documents_the_google_path_diffs_read():
    """Taken from `GOOGLE_DISCOVERY` rather than listed a second time. A second list would go on
    reading a document the registry had repointed, and a sixth Google API would have its
    `batchPath` unchecked until someone remembered the list existed."""
    assert {d.spec_url for d in COMPARISONS["google_batch"].documents} == {
        s.spec_url for c in GOOGLE_DISCOVERY.values() for s in c.specs
    }


def test_the_batch_routes_backlot_serves_are_the_two_shapes_google_documents():
    """Backlot's side of the comparison, and the reason there are two of it. Every API answers
    batch on its own host, which Backlot collapses onto one origin: `/batch` stands in for the four
    whose document says `batch`, and `/batch/{api}/{version}` for Drive, which sits on the shared
    `www.googleapis.com` and discriminates."""
    from backlot.main import app

    comparison = COMPARISONS["google_batch"]
    served = {op.path for op in operations.from_backlot(app.openapi(), comparison.mount).values()}
    assert served == {"batch", "batch/{}/{}"}


# --------------------------------------------------------------------------- the command


@pytest.fixture
def _vendor(monkeypatch):
    def _serve(sdl: str = VENDOR):
        monkeypatch.setenv("FIREFLIES_API_KEY", "test-key")
        # Patched in the module that owns them, not on the package re-exports: patching those
        # leaves the command calling the vendor for real.
        monkeypatch.setattr(
            "backlot.fidelity.graphql_diff.real_schema", lambda *a, **k: build_schema(sdl)
        )
        monkeypatch.setattr(
            "backlot.fidelity.graphql_diff.backlot_schema", lambda name: build_schema(VENDOR)
        )

    return _serve


def _both(sdl=VENDOR):
    return sdl.replace("Float", "Int").replace("status: Status", "status: Status, summary: String")


def test_update_baseline_records_gaps_and_leaves_breaking_findings_live(_vendor, tmp_path, capsys):
    """Muting a breaking finding with the flag that records a deliberate gap is how an alarm stops
    being read; the gaps are still written, or the bug stays buried under them every run."""
    _vendor(_both())
    args = ["diff", "-s", "fireflies", "--baseline-dir", str(tmp_path)]
    assert cli.main([*args, "--update-baseline"]) == 1
    acknowledged = json.loads((tmp_path / "fireflies.json").read_text())["acknowledged"]
    assert [(f["severity"], f["path"]) for f in acknowledged] == [(GAP, "Transcript.summary")]
    assert "type_mismatch" in capsys.readouterr().err


def test_a_breaking_finding_acknowledged_by_hand_survives_a_rewrite(_vendor, tmp_path):
    """No flag acknowledges one; a hand edit does, and a rewrite must not undo it."""
    _vendor(VENDOR.replace("Float", "Int"))
    args = ["diff", "-s", "fireflies", "--baseline-dir", str(tmp_path)]
    path = tmp_path / "fireflies.json"
    assert cli.main([*args, "--update-baseline"]) == 1

    edited = json.loads(path.read_text())
    edited["acknowledged"].append(
        {
            "kind": "type_mismatch",
            "severity": BREAKING,
            "path": "Transcript.duration",
            "detail": "vendor: Int, Backlot: Float",
            "note": "deliberate",
        }
    )
    path.write_text(json.dumps(edited))
    assert cli.main(args) == 0
    assert cli.main([*args, "--update-baseline"]) == 0
    kept = [e for e in json.loads(path.read_text())["acknowledged"] if e["severity"] == BREAKING]
    assert [e["note"] for e in kept] == ["deliberate"]

    # The vendor moves again. The note was written about `vendor: Int`, and it says nothing about a
    # vendor now serving String, so the acknowledgement stops covering what is reported — going on
    # silencing it is the drift in March this command exists to catch. The entry stays in the file
    # exactly as written, because deleting a reviewer's reasoning is not what a rewrite does.
    _vendor(VENDOR.replace("Float", "String"))
    assert cli.main(args) == 1
    assert cli.main([*args, "--update-baseline"]) == 1
    kept = [e for e in json.loads(path.read_text())["acknowledged"] if e["severity"] == BREAKING]
    assert [(e["detail"], e["note"]) for e in kept] == [
        ("vendor: Int, Backlot: Float", "deliberate")
    ]


@pytest.mark.parametrize(
    "reply",
    [None, (500, "nope"), (200, "<html>just a moment…</html>"), (200, {"data": {"__schema": {}}})],
    ids=["unreachable", "server-error", "not-json", "not-an-introspection-result"],
)
def test_an_unreadable_vendor_exits_two_not_one(reply, monkeypatch, tmp_path, capsys):
    """A scheduled job has to tell "the vendor moved" from "we could not ask", or every outage
    files a fidelity bug against Backlot.

    A 200 is not proof the vendor answered. A CDN challenge page and a proxy that lost its upstream
    both come back 200 carrying something that is not a schema, and left to escape as a `ValueError`
    the command dies on a traceback with status 1 — which is the status that means "the schemas
    disagree".
    """
    monkeypatch.setenv("FIREFLIES_API_KEY", "test-key")

    def _post(*a, **k):
        if reply is None:
            raise httpx.ConnectError("no route to host")
        status, body = reply
        kw = {"json": body} if isinstance(body, dict) else {"text": body}
        return httpx.Response(status, **kw)

    monkeypatch.setattr("backlot.fidelity.graphql_diff.httpx.post", _post)
    assert cli.main(["diff", "-s", "fireflies", "--baseline-dir", str(tmp_path)]) == 2
    assert "could not read" in capsys.readouterr().err


def test_an_unknown_source_names_the_ones_that_exist(capsys):
    assert cli.main(["diff", "-s", "nosuchvendor"]) == 2
    assert "fireflies" in capsys.readouterr().err


def test_the_human_form_groups_by_severity_and_says_what_to_do(_vendor, tmp_path, capsys):
    _vendor(_both())
    cli.main(["diff", "-s", "fireflies", "--baseline-dir", str(tmp_path)])
    out = capsys.readouterr()
    assert "breaking (1)" in out.err and "gap (1)" in out.err
    assert "--update-baseline" in out.err
    # Escape codes in a redirected run would land in a log file or a CI transcript.
    assert "\x1b[" not in out.out + out.err


def test_a_clean_run_says_so(_vendor, tmp_path, capsys):
    _vendor()
    assert cli.main(["diff", "-s", "fireflies", "--baseline-dir", str(tmp_path)]) == 0
    assert "nothing new" in capsys.readouterr().out


def test_a_long_detail_is_wrapped(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "80")
    cli._echo_findings("x", [Finding("k", BREAKING, "p", "x " * 120)], "red")
    assert max(len(line) for line in capsys.readouterr().err.splitlines()) <= 100


def test_json_output_is_the_only_thing_on_stdout_and_is_never_styled(_vendor, tmp_path, capsys):
    """It has to parse, including where CI forces colour on."""
    _vendor(VENDOR.replace("Float", "Int"))
    code = cli.main(["diff", "-s", "fireflies", "--baseline-dir", str(tmp_path), "--json"])
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert code == 1 and out.err == "" and "\x1b[" not in out.out
    assert payload["source"] == "fireflies"
    assert [f["kind"] for f in payload["new"]] == ["type_mismatch"]
    assert payload["total"] == len(payload["new"])


def test_json_output_says_what_a_baseline_write_did(_vendor, tmp_path, capsys):
    _vendor()
    cli.main(
        ["diff", "-s", "fireflies", "--baseline-dir", str(tmp_path), "--update-baseline", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["baseline"].endswith("fireflies.json") and payload["unacknowledged"] == []


def test_a_document_that_is_json_but_not_an_object_is_not_a_divergence(monkeypatch, tmp_path):
    """A 200 carrying valid non-object JSON used to fail deep in a parser as `AttributeError`,
    which the command cannot catch: it left as a traceback with status 1, the status that means
    the contracts disagree and files an issue against Backlot."""
    for payload in (["not", "an", "object"], "just a string", 42):
        monkeypatch.setattr(
            "backlot.fidelity.fetch.httpx.get", lambda *a, **k: httpx.Response(200, json=payload)
        )
        with pytest.raises(FidelityError, match="not an object"):
            comparisons.divergences(COMPARISONS["notion"])
        assert cli.main(["diff", "-s", "notion", "--baseline-dir", str(tmp_path)]) == 2


def test_a_credential_nobody_set_exits_three_not_two(monkeypatch, tmp_path, capsys):
    """Its own status because it is not a vendor outage: reported as one and left green, the two
    sources whose contract is introspection would go uncompared night after night."""
    monkeypatch.delenv("FIREFLIES_API_KEY", raising=False)
    assert cli.main(["diff", "-s", "fireflies", "--baseline-dir", str(tmp_path)]) == 3
    assert "FIREFLIES_API_KEY" in capsys.readouterr().err


def test_the_probe_asks_with_the_timeout_it_was_given(monkeypatch):
    """`timeout` reached the three set-up reads but not the probe requests, so the ones that
    matter ran on this module's own default whatever the caller asked for."""
    seen = []
    listing = '<?xml version="1.0"?><ListBucketResult><Name>b</Name><Contents><Key>k</Key></Contents></ListBucketResult>'

    def fake_get(url, headers=None, timeout=None):
        if "_meta/users" in url:
            creds = {"admin_s3_access_key_id": "AKIA", "admin_s3_secret_access_key": "sk"}
            return httpx.Response(200, json=creds)
        return httpx.Response(200, text=listing)

    def fake_request(method, url, headers=None, timeout=None):
        seen.append(timeout)
        return httpx.Response(200, text=listing)

    monkeypatch.setattr(s3_probe.httpx, "get", fake_get)
    monkeypatch.setattr(s3_probe.httpx, "request", fake_request)
    s3_probe.run("http://x", S3_MODEL, timeout=99.0)
    assert seen and set(seen) == {99.0}
