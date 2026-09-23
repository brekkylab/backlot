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
    CredentialsMissing,
    FidelityError,
    Finding,
    baseline_path,
    comparisons,
    google_discovery_diff,
    hubspot_catalog,
    openapi_diff,
    operations,
    s3_probe,
    slack_docs,
    slack_probe,
)
from backlot.fidelity.comparisons import (
    COMPARISONS,
    GOOGLE_DISCOVERY,
    GRAPHQL,
    OPENAPI,
    PROBE,
    SLACK,
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
    """A single `--token` was accepted by all eleven comparisons and did something for two."""
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
    # And the batch endpoint each of those four APIs declares, which no `mount` can select: it is
    # a top-level field rather than an operation, so a mount over it would hand both routes to the
    # path diff to report as surface Backlot invented. Measured 2026-09-17: Docs, Sheets and
    # Slides declare `batch` at the root of their own hosts and Drive `batch/drive/v3` on the
    # shared `www.googleapis.com`, so this source needs both shapes and Gmail's needs one.
    assert COMPARISONS["google_drive"].batch_mount == ("/batch", "/batch/{api}/{version}")
    assert COMPARISONS["gmail"].batch_mount == ("/batch",)


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


def test_a_google_document_is_read_once_for_both_the_contracts_it_carries(monkeypatch):
    """A discovery document describes operations under `resources` AND a batch endpoint in its
    top-level `batchPath`, and a Google source reads it once for both."""
    URL = "https://gmail.invalid/rest"
    doc = {
        "id": "gmail:v1",
        "batchPath": "batch",
        "resources": {
            "users": {
                "methods": {
                    "getProfile": {"path": "gmail/v1/users/{userId}/profile", "httpMethod": "GET"}
                }
            }
        },
    }
    fetched = []

    def fake_fetch(url, *, timeout=120.0):
        fetched.append(url)
        return doc

    monkeypatch.setattr(google_discovery_diff, "fetch_json", fake_fetch)
    c = comparisons.GoogleDiscoveryComparison(
        name="gmail",
        specs=(comparisons.Spec(URL, ("/gmail",)),),
        # A route that cannot answer what this document declares, so the batch check has something
        # to say and reading it off the memoized document is what the assertion below proves.
        batch_mount=("/batch/{api}/{version}",),
    )
    found = c.divergences()
    assert fetched == [URL]
    assert [(f.kind, f.path) for f in found if "batch" in f.kind] == [
        ("extra_batch_route", "/batch/{}/{}"),
        ("missing_batch_path", "gmail:v1 batch"),
    ]


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
    the same `source_type` a BYO record carries.

    `cli.FIDELITY_SOURCES` is held to the same list. It exists so `--help` can name the sources
    without importing `backlot.fidelity` on every other command, and it is what a reader is told
    the command takes — a source added to one and not the other leaves `--help` naming a set the
    command does not validate against.
    """
    assert set(COMPARISONS) == set(store.SOURCE_TABLE) == set(cli.FIDELITY_SOURCES)


def test_every_comparison_is_registered_once_as_the_class_its_registry_implies():
    registries = [
        (OPENAPI, comparisons.OpenAPIComparison),
        (GOOGLE_DISCOVERY, comparisons.GoogleDiscoveryComparison),
        (GRAPHQL, comparisons.GraphQLComparison),
        (PROBE, comparisons.ProbeComparison),
        (SLACK, comparisons.SlackComparison),
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
        # A batch route is matched EXACTLY where a mount is matched by prefix: `/batch` would
        # otherwise stand in for `/batch/{api}/{version}`, and a source could go on mounting a
        # batch route Backlot stopped serving.
        for route in _batch_routes(comparison):
            assert route in served, f"{name}: {route} is not served"
        # And a Google source names at least one. `batch_mount` defaults to empty and the batch
        # check is skipped when it is, so a third Google source registered without one would have
        # every document's `batchPath` go unread while every test above it passed -- the test
        # beside this one pins the two sources that exist rather than the rule.
        if isinstance(comparison, comparisons.GoogleDiscoveryComparison):
            assert comparison.batch_mount, f"{name}: a Google source names no batch route"


def _comparison_mounts(comparison) -> tuple[str, ...]:
    """Every served path prefix a comparison speaks for, whichever kind it is."""
    specs = getattr(comparison, "specs", None)
    if specs is not None:
        return tuple(m for s in specs for m in s.mount)
    return tuple(getattr(comparison, "mount", ()))


def _batch_routes(comparison) -> tuple[str, ...]:
    """The batch routes a comparison speaks for. Apart from the mounts above because these are
    whole paths and not prefixes, and because the path diff must never be handed one."""
    return tuple(getattr(comparison, "batch_mount", ()))


def test_every_served_path_is_compared_or_says_why_not():
    """The coverage guarantee the tests beside this one only appeared to give.

    They count SOURCE TYPES, which is the corpus dimension. `google_drive` is one source type
    served through five vendor APIs, so Docs, Sheets and Slides sat compared against nothing while
    both tests passed — a Sheets response shape could be rewritten and `backlot diff` would still
    answer `0 new`. This counts served PATHS, which is what a comparison actually covers.

    Every path lands in exactly one bucket: under some document's mount, named as a source's batch
    route, probe-compared, or declared in `UNCOMPARED` with a reason. A path in none of them is a
    router someone added without asking what checks it.
    """
    from backlot.main import app

    mounts = [m for c in COMPARISONS.values() for m in _comparison_mounts(c)]
    batch = {r for c in COMPARISONS.values() for r in _batch_routes(c)}
    unclassified = [
        p
        for p in sorted(app.openapi()["paths"])
        if not any(p.startswith(m) for m in mounts)
        and p not in batch
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
    mounts += [r for c in COMPARISONS.values() for r in _batch_routes(c)]
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
    """Google differs per DOCUMENT, which is why `strip` sits on the Spec: Drive's own document
    spells `drive/v3`, so nothing comes off there, while the Sheets document declares an empty
    `servicePath` and spells `v4/spreadsheets/...` itself, so its mount does. Jira and Confluence
    share /atlassian and must not capture each other."""
    served = {
        "paths": dict.fromkeys(
            [
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


GOOGLE_BATCH_MOUNT = ("/batch", "/batch/{api}/{version}")


def _batch_docs(declared: tuple[str | None, ...], spelled: str = "id") -> list[tuple[str, dict]]:
    """Four documents shaped like `google_drive`'s and in its order, each declaring the batchPath
    given -- `None` declaring none.

    `spelled` is how a document says which API it is. Google's carry an `id`; the other two are
    what a finding falls back to when one does not, and they are reached here rather than through
    `document_id` because the identity a baseline holds is the finding's path."""
    docs = []
    for who, d in zip(("drive:v3", "docs:v1", "sheets:v4", "slides:v1"), declared):
        name, version = who.split(":")
        says = {"id": {"id": who}, "name and version": {"name": name, "version": version}}
        docs.append(
            (
                f"https://{name}.invalid/rest",
                {**says.get(spelled, {}), **({"batchPath": d} if d else {})},
            )
        )
    return docs


SHORTER = ("batch/v3", "batch", "batch", "batch")


@pytest.mark.parametrize(
    "declared,spelled,expected",
    [
        pytest.param(
            ("batch/drive/v3", "batch", "batch", "batch"), "id", [], id="measured-2026-09-17"
        ),
        pytest.param(
            ("batch", "batch", "batch", "batch"),
            "id",
            [("extra_batch_route", BREAKING, "/batch/{}/{}")],
            id="drive-moves-to-its-own-host",
        ),
        pytest.param(
            ("batch/drive/v3", None, "batch", "batch"),
            "id",
            [("extra_batch_api", BREAKING, "docs:v1")],
            id="one-api-drops-batch",
        ),
        pytest.param(
            SHORTER,
            "id",
            [
                ("extra_batch_route", BREAKING, "/batch/{}/{}"),
                ("missing_batch_path", GAP, "drive:v3 batch/v3"),
            ],
            id="a-value-shorter-than-the-route",
        ),
        pytest.param(
            ("batch/drive/v3/files", "batch", "batch", "batch"),
            "id",
            [
                ("extra_batch_route", BREAKING, "/batch/{}/{}"),
                ("missing_batch_path", GAP, "drive:v3 batch/drive/v3/files"),
            ],
            id="a-value-longer-than-the-route",
        ),
        pytest.param(
            ("/batch/drive/v3/", "/batch/", "batch", "batch"), "id", [], id="slashes-at-either-end"
        ),
        pytest.param(
            SHORTER,
            "name and version",
            [
                ("extra_batch_route", BREAKING, "/batch/{}/{}"),
                ("missing_batch_path", GAP, "drive:v3 batch/v3"),
            ],
            id="a-document-carrying-no-id",
        ),
        pytest.param(
            SHORTER,
            "nothing",
            [
                ("extra_batch_route", BREAKING, "/batch/{}/{}"),
                ("missing_batch_path", GAP, "https://drive.invalid/rest batch/v3"),
            ],
            id="a-document-that-names-itself-nowhere",
        ),
    ],
)
def test_batch_path_divergences_name_the_document_and_the_route(declared, spelled, expected):
    """Both directions, and a value that moved reports as both.

    The gap carries the VALUE as well as the document, because a gap is acknowledged by identity
    alone: keyed on the document, an acknowledged move to one shape would go on covering a later
    move to another.

    `one-api-drops-batch` is where the two directions come apart: `/batch` is still selected by the
    two documents left, so only the API is reported and the route is not.

    The two length cases are the matcher's segment count. A route that swallowed a differing
    number of segments would report either move as covered."""
    found = google_discovery_diff.batch_divergences(
        GOOGLE_BATCH_MOUNT, _batch_docs(declared, spelled)
    )
    assert [(f.kind, f.severity, f.path) for f in found] == expected


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


def test_two_bodies_under_one_root_element_are_told_apart_by_their_children(monkeypatch):
    """#188: both listings are `200 <ListBucketResult>`, so status and root alone cannot tell
    ListObjectsV2 from the body a bare bucket GET answers with — which is why V2 had to be
    acknowledged in the baseline to keep the comparison quiet. The child elements separate them:
    V2 carries KeyCount where V1 carries Marker. An operation the catch-all really does answer
    still matches on all three, which this model's `?acl` is here to show."""
    v1 = '<?xml version="1.0"?><ListBucketResult><Name>b</Name><Marker></Marker></ListBucketResult>'
    v2 = '<?xml version="1.0"?><ListBucketResult><Name>b</Name><KeyCount>1</KeyCount></ListBucketResult>'

    def fake(method, url, headers=None, timeout=None):
        return httpx.Response(200, text=v2 if "list-type=2" in url else v1)

    monkeypatch.setattr(s3_probe.httpx, "request", fake)
    found = {
        f.path.split(":")[0]: f
        for f in s3_probe.probe(
            "http://x", "ak", "sk", s3_probe.operations(S3_MODEL), bucket="b", key="k"
        )
    }
    assert "ListObjectsV2" not in found
    assert found["GetBucketAcl"].kind == "silent_fallthrough"


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


# --------------------------------------------------------------------------- slack

# One row of the reference index and one method page, as docs.slack.dev serves them: the index is
# a markdown table of links, and a page carries YAML frontmatter and one line per argument. Both
# abbreviated to the parts the two parsers read.
DOCS_INDEX = """---
title: "Methods"
---

| Name | Description |
|------|-------------|
| [users.list](https://docs.slack.dev/reference/methods/users.list.md) | Lists all users. |
| [chat.postMessage](https://docs.slack.dev/reference/methods/chat.postmessage.md) | Sends one. |
"""

DOCS_PAGE = """---
method_name: "users.list"
http_method: "GET"
---

## Arguments {#arguments}

### Required arguments

**`token`**`string`Required

Authentication token bearing required scopes.

### Optional arguments

**`limit`**`number`Optional

The maximum number of items to return.

**`team_id`**Optional

encoded team id to list users in

## Usage info {#usage-info}

**`not_an_argument`**`string`Required
"""


def test_the_slack_reference_is_read_off_its_index_and_its_pages():
    """The index is the whole inventory; a page is read only for a method Backlot serves.

    `team_id` is written in the shape 187 of the 1442 arguments measured 2026-09-22 share — a name
    and `Optional` with no type between them — and 34 of those 187 are `team_id` itself; it parses
    as an argument like the rest. The bolded line under `## Usage info` is not one: the section
    boundary is what keeps a page's prose out of its argument list."""
    assert slack_docs.documented_methods(DOCS_INDEX) == {
        "users.list": "https://docs.slack.dev/reference/methods/users.list.md",
        "chat.postMessage": "https://docs.slack.dev/reference/methods/chat.postmessage.md",
    }
    assert slack_docs.arguments(DOCS_PAGE) == {"token", "limit", "team_id"}


def test_a_slack_method_is_one_comparison_unit_over_both_verbs():
    """Slack's Web API is RPC over POST and most methods answer GET too, while the documentation
    names one verb. Measured against slack.com 2026-09-22, all twelve methods Backlot serves answer
    200/ok over both — so the verb is not a contract here, and reading the two verbs separately
    pairs one served method with two units and calls the undocumented one surface Backlot
    invented."""
    served = {
        "paths": {
            "/slack/api/users.list": {
                "get": {"parameters": [{"name": "limit", "in": "query"}]},
                "post": {"parameters": [{"name": "cursor", "in": "query"}]},
            },
            "/gmail/v1/users/{user_id}/messages": {"get": {}},
        }
    }
    assert slack_docs.from_backlot(served, ("/slack/api",)) == {
        "users.list": slack_docs.Method("users.list", frozenset({"limit", "cursor"}))
    }


def test_slack_method_divergences_are_classified_like_operation_ones():
    served = {"a": slack_docs.Method("a", frozenset({"x", "y"})), "c": slack_docs.Method("c", ())}
    real = {"a": slack_docs.Method("a", frozenset({"x", "z"})), "b": slack_docs.Method("b", ())}
    found = {(f.kind, f.path): f.severity for f in slack_docs.diff_methods(served, real)}
    assert found == {
        ("extra_operation", "c"): BREAKING,
        ("missing_operation", "b"): GAP,
        ("extra_param", "a?y"): BREAKING,
        ("missing_param", "a?z"): GAP,
    }


def test_a_reference_page_for_another_method_is_refused(monkeypatch):
    """The index and the page are separate fetches. A redirect answering a different method would
    otherwise be read as this method's argument list, which is a quieter wrong answer than a
    failure — the arguments would simply not be the ones compared."""
    monkeypatch.setattr(
        slack_docs,
        "_fetch_text",
        lambda url, timeout: DOCS_INDEX if url.endswith("methods.md") else DOCS_PAGE,
    )
    monkeypatch.setattr(
        slack_docs, "from_backlot", lambda spec, mount: {"chat.postMessage": object()}
    )
    with pytest.raises(FidelityError, match="states method_name users.list, not chat.postMessage"):
        slack_docs.divergences(COMPARISONS["slack"])


def test_a_response_is_reduced_to_the_paths_a_client_can_read():
    """An array contributes one path for its members, not one per member, carrying their types,
    and a field on ANY member counts as present — Slack drops fields per object, so intersecting
    would report the vendor as missing what it serves on every other one.

    A key that is not a field name is a map entry and collapses to `{}`. Measured 2026-09-22, the
    only ones Slack answers with are the conversation ids keying a file's `shares.public` and
    `shares.private`; left alone they write one path per channel a file was shared into."""
    shape = slack_probe.shape_of(
        {
            "ok": True,
            "members": [{"id": "U1", "profile": {"email": "a@b.c"}}, {"id": "U2", "deleted": True}],
            "shares": {"public": {"C0AAA": [{"ts": "1.0"}]}},
            "matches": [],
        }
    )
    assert set(shape.fields) == {
        "ok",
        "members",
        "members[]",
        "members[].id",
        "members[].profile",
        "members[].profile.email",
        "members[].deleted",
        "shares",
        "shares.public",
        "shares.public.{}",
        "shares.public.{}[]",
        "shares.public.{}[].ts",
        "matches",
    }
    assert shape.fields["members[].id"] == frozenset({"string"})
    assert shape.fields["members[]"] == frozenset({"object"})
    assert shape.fields["ok"] == frozenset({"boolean"})
    # `matches` was answered empty, so nothing under it was looked at and it is not a container.
    assert "matches[]" not in shape.containers and "members[]" in shape.containers


def test_an_empty_list_hides_its_members_and_an_empty_object_does_not():
    """A list with no elements is nothing to look at; an object with no keys is a shape that was
    looked at. So the first suppresses — Backlot's `search.files` serves no matches by
    construction, and a workspace can answer a search with none, and reported either way round
    every field of the other side's match becomes a finding — while the second reports, which is
    what surfaced the 31 gaps under a channel's `properties` that Backlot answers as `{}`."""
    populated = slack_probe.shape_of({"matches": [{"id": "F1", "name": "x"}]})
    empty = slack_probe.shape_of({"matches": []})
    assert slack_probe.diff_shapes("search.files", populated, empty) == []
    assert slack_probe.diff_shapes("search.files", empty, populated) == []
    assert slack_probe.diff_shapes(
        "conversations.list",
        slack_probe.shape_of({"properties": {}}),
        slack_probe.shape_of({"properties": {"use_case": "welcome"}}),
    ) == [
        Finding(
            "missing_field",
            GAP,
            "conversations.list properties.use_case",
            "the real API serves it; Backlot does not",
        )
    ]
    # And where both sides DID look inside it, the difference is reported in both directions.
    other = slack_probe.shape_of({"matches": [{"id": "F1", "user": "U1"}]})
    assert {
        (f.kind, f.path): f.severity for f in slack_probe.diff_shapes("m", populated, other)
    } == {
        ("extra_field", "m matches[].name"): BREAKING,
        ("missing_field", "m matches[].user"): GAP,
    }


def test_a_type_mismatch_is_two_sides_sharing_no_type_but_null():
    """A field this workspace happens to leave null and this corpus fills in is one field at one
    type, whichever side left it null. Reported as a mismatch, the comparison becomes a report on
    two sets of contents. A field seen at two types on one side matches the other side's one.

    An array's members are compared the same way: a list of ids answered as a list of objects is a
    mismatch at `ids[]`, and the objects' own fields are surface the real API has no container for.
    """
    assert slack_probe.diff_shapes(
        "m",
        slack_probe.shape_of(
            {"a": "s", "b": 1, "c": None, "t": [{"v": 1}, {"v": "1"}], "ids": [{"id": "U1"}]}
        ),
        slack_probe.shape_of({"a": None, "b": "1", "c": "s", "t": [{"v": "1"}], "ids": ["U1"]}),
    ) == [
        Finding(
            "extra_field",
            BREAKING,
            "m ids[].id",
            "Backlot serves it; the real API's answer has no such field",
        ),
        Finding("type_mismatch", BREAKING, "m b", "real: string, Backlot: number"),
        Finding("type_mismatch", BREAKING, "m ids[]", "real: string, Backlot: object"),
    ]


@pytest.mark.parametrize(
    "ask, answer, message",
    [
        (
            "probe",
            httpx.Response(200, json={"ok": False, "error": "x"}),
            "answered .x., so users.list",
        ),
        ("probe", httpx.Response(502, text="<html>bad gateway</html>"), "answered 502, not JSON"),
        ("probe", httpx.ConnectError("no route to host"), "went unanswered"),
        ("docs", httpx.Response(404, text="not found"), "answered 404"),
        ("docs", httpx.ConnectError("no route to host"), "unreachable"),
    ],
    ids=["probe-not-ok", "probe-not-json", "probe-unreachable", "docs-404", "docs-unreachable"],
)
def test_a_method_that_could_not_be_called_is_not_a_divergence(ask, answer, message, monkeypatch):
    """Slack answers HTTP 200 with `ok: false`, so a scope the token lacks would otherwise be read
    as a response shape with one field in it — and reported as the vendor missing everything else.
    Anything else that escapes either module reaches `backlot diff` as status 1, the status a
    scheduled run files a divergence for."""

    def get(*a, **k):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(httpx, "get", get)
    with pytest.raises(FidelityError, match=message):
        if ask == "probe":
            slack_probe._Caller("https://slack.invalid/api", "t", pace=0.0, timeout=1.0)(
                "users.list"
            )
        else:
            slack_docs._fetch_text("https://docs.slack.invalid/methods.md", timeout=1.0)


def _stub(answers: dict, calls: list[str] | None = None):
    """A caller answering from `answers`, where a callable answer is given the call's arguments."""

    def call(method, **arguments):
        if calls is not None:
            calls.append(method)
        answer = answers[method]
        return answer(**arguments) if callable(answer) else answer

    return call


def test_only_the_real_side_must_be_an_admin_and_each_side_is_compared_over_every_object():
    """Backlot's side of the probe is always its admin/service token, which serves `has_2fa` on
    every person. A real caller who is not an admin gets it only on their own member object
    (measured 2026-09-23), a caller-identity difference the shape comparison would read as Backlot
    inventing the field on every other member — so a non-admin real token is refused before
    Backlot is asked anything, as this repository's own misconfiguration (`CredentialsMissing`),
    and Backlot's own side is never asked who it is.

    Past that, `users.info` is asked about every person on each side and read as one shape, so a
    field one person carries is present whichever person a listing puts first."""
    real = {
        "auth.test": {"ok": True, "user_id": "U1"},
        "users.info": {"ok": True, "user": {"id": "U1", "is_admin": False}},
    }
    backlot_calls: list[str] = []
    backlot = _stub({}, backlot_calls)
    with pytest.raises(CredentialsMissing, match="not a workspace admin"):
        slack_probe.probe(backlot, _stub(real))
    assert backlot_calls == []

    people = {"U1": {"id": "U1", "is_admin": True}, "U2": {"id": "U2", "start_date": "2026-01-05"}}
    common = {
        "users.list": {"ok": True, "members": [{"id": "U1"}, {"id": "U2"}]},
        "conversations.list": {"ok": True, "channels": [{"id": "C1"}]},
        "conversations.history": {
            "ok": True,
            "messages": [{"ts": "1.0", "reply_count": 1, "text": "zebra"}],
        },
        "search.messages": {"ok": True, "messages": {"matches": [{}]}},
        **{
            m: {"ok": True}
            for m in (
                "api.test",
                "conversations.info",
                "conversations.members",
                "conversations.replies",
                "search.all",
                "search.files",
            )
        },
    }
    real.update(common, **{"users.info": lambda user: {"ok": True, "user": people[user]}})
    ours = {
        **common,
        "auth.test": {"ok": True, "user_id": "U1"},
        "users.info": lambda user: {"ok": True, "user": {"id": user}},
    }
    assert slack_probe.probe(_stub(ours, backlot_calls), _stub(real)) == [
        Finding(
            "missing_field",
            GAP,
            "users.info user.is_admin",
            "the real API serves it; Backlot does not",
        ),
        Finding(
            "missing_field",
            GAP,
            "users.info user.start_date",
            "the real API serves it; Backlot does not",
        ),
    ]
    assert backlot_calls[0] == "users.list"


def test_a_workspace_with_nothing_to_sample_cannot_be_probed():
    """Not a clean comparison: everything every call but `api.test` and `auth.test` needs is
    discovered through the API itself, so a workspace holding no thread leaves
    `conversations.replies` with nothing to ask about, and one whose search finds none of its own
    words leaves a match's fields compared against nothing — which is the whole of what the three
    search methods add over their envelopes."""
    answers = {
        "conversations.list": {"ok": True, "channels": [{"id": "C1", "is_member": True}]},
        "conversations.history": {"ok": True, "messages": [{"ts": "1.0", "text": "<@U1> bullet"}]},
        "users.list": {"ok": True, "members": [{"id": "U1"}]},
        "search.messages": {"ok": True, "messages": {"matches": []}},
    }
    call = _stub(answers)
    with pytest.raises(FidelityError, match="message with replies"):
        slack_probe.discover(call)

    answers["conversations.history"]["messages"][0]["reply_count"] = 1
    with pytest.raises(FidelityError, match="candidate words"):
        slack_probe.discover(call)

    answers["users.list"]["members"] = [{"id": "B1", "is_bot": True}, {"id": "USLACKBOT"}]
    answers["users.list"]["members"] += [{"id": "U0", "deleted": True}, {"id": "U1"}]
    answers["search.messages"]["messages"]["matches"] = [{}]
    answers["conversations.history"]["messages"].append({"ts": "2.0", "text": "alphabet"})
    sample = slack_probe.discover(call)
    assert (sample.users, sample.queries) == (("U1",), ("bullet", "alphabet"))
    answers["users.list"]["members"] = answers["users.list"]["members"][:3]
    with pytest.raises(FidelityError, match="no active person"):
        slack_probe.discover(call)

    # One word per message, the messages carrying the most fields first, and within one the
    # longest word, out of text with Slack's markup taken off: with the markup left in,
    # `<@U0BCVV6G3M1>` would parse as an 11-character word; with the sort merely alphabetical,
    # "ants" would come before "zebra"; in history order, "kiwi" would be drawn from the second.
    assert slack_probe._searchable_words(
        [
            {"ts": "1", "text": "zebra kiwi"},
            {"ts": "2", "files": [], "text": "<@U0BCVV6G3M1> zebra ants"},
        ]
    ) == ["zebra", "kiwi"]


def test_the_probe_corpus_answers_every_shape_the_probe_compares(tmp_path):
    """The Backlot half of the probe is only as good as the corpus under it, and nothing else holds
    that corpus to what the comparison needs. A corpus that stopped producing a thread would leave
    `discover` raising, which `backlot diff` reports as a contract it could not read — exit 2, and
    the source uncompared night after night with nothing saying so.

    `search.files` is the one method asked for here without a populated collection: Backlot serves
    no file matches by construction, so its envelope is all this comparison can reach and a file
    object is read off `conversations.history` instead.
    """
    from tests._helpers import client_for, tiny_corpus

    settings = tiny_corpus(tmp_path, list(slack_probe.PROBE_CORPUS))
    with client_for(settings, reload=True) as client:
        headers = {"Authorization": f"Bearer {settings.admin_token}"}

        def call(method, **arguments):
            body = client.get(f"/slack/api/{method}", headers=headers, params=arguments).json()
            assert body.get("ok"), (method, body)
            return body

        calls = slack_probe._calls(slack_probe.discover(call, "drosophila"))
        bodies = [(m, call(m, **a)) for m, a in calls]
        answers = dict(bodies)
        served = slack_docs.from_backlot(client.app.openapi(), COMPARISONS["slack"].mount)

    # Every method Backlot serves is asked, so a route added later is compared rather than left
    # to the documentation's request surface alone.
    assert set(answers) == set(served)
    asked = [m for m, _ in calls]
    methods = ("conversations.info", "conversations.history", "users.info")
    assert {m: asked.count(m) for m in methods} == dict.fromkeys(methods, 2)
    assert len(answers["conversations.list"]["channels"]) == 2
    assert len(answers["users.list"]["members"]) >= 2
    assert answers["conversations.members"]["members"]
    assert answers["users.info"]["user"]["id"]
    assert answers["search.messages"]["messages"]["matches"]
    assert answers["search.all"]["messages"]["matches"]
    assert answers["search.files"]["files"]["matches"] == []
    assert len(answers["conversations.replies"]["messages"]) == 2
    history = [
        m for method, body in bodies if method == "conversations.history" for m in body["messages"]
    ]
    carried = {
        key
        for message in history
        for key in ("edited", "reactions", "files", "reply_count")
        if key in message
    }
    assert carried == {"edited", "reactions", "files", "reply_count"}


def test_the_two_slack_contracts_are_reported_as_one(monkeypatch):
    """One source, two vendor sides, one baseline. Both endpoints are recorded so a reader of the
    file can see which of the two a finding came from."""
    docs = [Finding("missing_operation", GAP, "chat.postMessage", "d")]
    probed = [Finding("missing_field", GAP, "users.list cache_ts", "d")]
    monkeypatch.setattr(slack_docs, "divergences", lambda source, timeout: docs)
    monkeypatch.setattr(slack_probe, "divergences", lambda source, creds, timeout: probed)
    comparison = COMPARISONS["slack"]
    assert comparison.endpoints == (comparison.docs_url, comparison.live_url)
    assert comparison.divergences({"user_token": "t"}) == docs + probed


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
    """Its own status because it is not a vendor outage: reported as one and left green, the three
    sources that declare a credential would go uncompared night after night."""
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
