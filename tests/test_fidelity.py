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
    EXTRA,
    GAP,
    IMPLEMENTED,
    MISSING,
    REFUSED,
    Baseline,
    FidelityError,
    Finding,
    Measurement,
    OperationEntry,
    baseline_path,
    comparisons,
    csdl_diff,
    google_discovery_diff,
    hubspot_catalog,
    msgraph,
    openapi_diff,
    operations,
    s3_probe,
    slack_docs,
)
from backlot.fidelity.comparisons import (
    COMPARISONS,
    CSDL,
    GOOGLE_DISCOVERY,
    GRAPHQL,
    OPENAPI,
    PROBE,
    SLACK,
    UNCOMPARED,
)
from backlot.fidelity.graphql_diff import backlot_schema, diff_schemas, schema_surface
from backlot.fidelity.surface import GraphSurface, OperationSurface, SchemaSurface

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
        (
            "extra_field",
            BREAKING,
            "Transcript.titel",
            VENDOR.replace("title:", "titel(short: Boolean):"),
        ),
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
        (
            "extra_type",
            BREAKING,
            "Invented",
            VENDOR + "\ntype Invented { id(full: Boolean): ID! }\nenum Mood { CALM }",
        ),
        (
            "kind_mismatch",
            BREAKING,
            "Origin",
            VENDOR.replace("union Origin = Meeting | Upload", "type Origin { id: ID! }"),
        ),
        (
            "extra_arg",
            BREAKING,
            "Source.id(raw)",
            VENDOR.replace(
                "interface Source { id: ID! }", "interface Source { id(raw: Boolean): ID! }"
            ),
        ),
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
    measured = _measure(backlot_sdl)
    found = measured.findings
    hits = [f for f in found if f.kind == kind and f.path == path]
    assert hits, f"{kind} at {path} not in {[(f.kind, f.path) for f in found]}"
    assert hits[0].severity == severity
    # Each is written onto the type, field, argument, value or member it names, and read back.
    _, back = SchemaSurface.from_json(measured.surface.to_json(found))
    assert sorted(f.key for f in back) == sorted(f.key for f in found)
    with pytest.raises(ValueError, match="naming no element"):
        measured.surface.to_json([*found, Finding("missing_field", GAP, "Nowhere.field", "")])
    # And what the measurement records Backlot serving is every element Backlot declares, a type
    # of another kind on the vendor's side counted whole.
    ours = build_schema(backlot_sdl)
    whole = {f.path for f in found if f.kind == "kind_mismatch"}
    declared = {p for p in schema_surface(ours, ours).elements() if p.split(".")[0] not in whole}
    served = {p for p, s in measured.surface.elements().items() if s in (IMPLEMENTED, EXTRA)}
    assert served == declared | whole
    # A vendor type is kept only where the vendor's member has one: no enum value or union member.
    assert '"type": "None"' not in json.dumps(measured.surface.to_json(found))


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


def _measure(backlot_sdl: str, vendor_sdl: str = VENDOR) -> Measurement:
    ours, theirs = build_schema(backlot_sdl), build_schema(vendor_sdl)
    return Measurement(diff_schemas(ours, theirs), schema_surface(ours, theirs))


def _baseline(tmp_path, measured, note=""):
    path = tmp_path / "fireflies.json"
    Baseline.empty("fireflies", ("https://api.fireflies.ai/graphql",)).write(
        path, measured.findings, measured="2026-09-01", surface=measured.surface
    )
    if note:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["types"]["Transcript"]["fields"]["title"]["note"] = note
        path.write_text(json.dumps(raw), encoding="utf-8")
    return Baseline.load(path)


def test_a_baseline_silences_what_it_holds_and_surfaces_what_is_new(tmp_path):
    measured = _measure(VENDOR.replace(", title: String", ""))
    known = measured.findings
    baseline = _baseline(tmp_path, measured)
    assert baseline.unacknowledged(known) == []
    regressed = _diff(VENDOR.replace("Float", "Int"))
    assert [f.kind for f in baseline.unacknowledged(regressed)] == ["type_mismatch"]
    assert [f.path for f in baseline.resolved([])] == ["Transcript.title"]
    assert baseline.resolved(known) == []


def test_rewriting_a_baseline_keeps_its_notes_and_takes_the_new_identity(tmp_path):
    """The note sits on the element the finding names, beside what is measured about it."""
    measured = _measure(VENDOR.replace(", title: String", ""))
    baseline = _baseline(tmp_path, measured, note="deliberate: no title in the corpus")
    path = tmp_path / "fireflies.json"
    baseline.identified_as("renamed", ("https://new.invalid",)).write(
        path, measured.findings, measured="2026-10", surface=measured.surface
    )
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["source"] == "renamed"
    assert written["endpoints"] == ["https://new.invalid"]
    assert written["types"]["Transcript"]["fields"]["title"] == {
        "status": MISSING,
        "type": "String",
        "kind": "missing_field",
        "severity": GAP,
        "note": "deliberate: no title in the corpus",
    }


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
    source name, so `measure` runs each and concatenates rather than picking one."""
    calls = []

    def fake(spec, *, timeout=120.0, seen=None):
        calls.append(spec.spec_url)
        return Measurement(
            [Finding("gap", GAP, f"GET {spec.mount[0]}/x", "")], OperationSurface(())
        )

    monkeypatch.setattr(comparisons.openapi_diff, "measure", fake)
    c = comparisons.OpenAPIComparison(
        name="two",
        specs=(
            comparisons.Spec("https://a.invalid/s.json", ("/a",)),
            comparisons.Spec("https://b.invalid/s.json", ("/b",)),
        ),
    )
    found = c.measure().findings
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
    one of those paths would print and count twice.

    The operations they pair join the same way, one entry per operation, and a document whose
    mount serves one outranks a document that only declares it, finding and all; between two that
    both serve it, or both only declare it, the first document's entry stands."""
    shared = _finding("missing_operation", "GET /rest/forge/1/app/properties")

    def fake(spec, *, timeout=120.0, **kw):
        own = spec.mount[0]
        unserved = [_finding("missing_operation", "GET /both")] if own == "/a" else []
        invented = _finding("extra_operation", f"GET {own}/mine", BREAKING)
        return Measurement(
            [shared, _finding("missing_operation", f"GET {own}/own"), *unserved, invented],
            OperationSurface(
                (
                    OperationEntry(shared.path, MISSING, f"forge-props{own}"),
                    OperationEntry("GET /both", IMPLEMENTED if own == "/b" else MISSING, "both"),
                    OperationEntry("GET /served", IMPLEMENTED, f"served{own}"),
                    OperationEntry(invented.path, EXTRA),
                )
            ),
        )

    monkeypatch.setattr(getattr(comparisons, module), "measure", fake)
    c = getattr(comparisons, registry)(
        name="two",
        specs=(
            comparisons.Spec("https://a.invalid/s.json", ("/a",)),
            comparisons.Spec("https://b.invalid/s.json", ("/b",)),
        ),
    )
    measured = c.measure()
    found = measured.findings
    assert len(found) == len({f.key for f in found}), [f.path for f in found]
    assert sorted(f.path for f in found) == [
        "GET /a/mine",
        "GET /a/own",
        "GET /b/mine",
        "GET /b/own",
        "GET /rest/forge/1/app/properties",
    ]
    assert measured.surface.operations == (
        OperationEntry("GET /a/mine", EXTRA),
        OperationEntry("GET /b/mine", EXTRA),
        OperationEntry("GET /both", IMPLEMENTED, "both"),
        OperationEntry("GET /rest/forge/1/app/properties", MISSING, "forge-props/a"),
        OperationEntry("GET /served", IMPLEMENTED, "served/a"),
    )


def test_findings_from_several_documents_come_back_in_one_order(monkeypatch):
    """`diff_operations` sorts breaking-first-then-path, and concatenating per-document results
    loses that across documents -- which leaves the baseline file in an order `--update-baseline`
    does not reproduce, so the next real change arrives buried in a reordering of the whole file."""

    def fake(spec, *, timeout=120.0, **kw):
        if spec.mount[0] == "/a":
            return Measurement(
                [
                    _finding("missing_operation", "GET /z"),
                    _finding("extra_param", "GET /a?x", BREAKING),
                ],
                OperationSurface(()),
            )
        return Measurement(
            [
                _finding("missing_operation", "GET /b"),
                _finding("extra_param", "GET /b?y", BREAKING),
            ],
            OperationSurface(()),
        )

    monkeypatch.setattr(comparisons.openapi_diff, "measure", fake)
    c = comparisons.OpenAPIComparison(
        name="two",
        specs=(
            comparisons.Spec("https://a.invalid/s.json", ("/a",)),
            comparisons.Spec("https://b.invalid/s.json", ("/b",)),
        ),
    )
    found = c.measure().findings
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
            {"source": "s", "endpoints": "https://one.invalid", "measured": "", "operations": []}
        ),
        encoding="utf-8",
    )
    with pytest.raises(TypeError, match="tuple of URLs"):
        Baseline.load(path)

    # and the shapes a real file carries still load: a list, an absent key, and the legacy spelling
    for endpoints, want in [(["https://a", "https://b"], ("https://a", "https://b")), (None, ())]:
        raw = {"source": "s", "measured": "", "operations": []}
        if endpoints is not None:
            raw["endpoints"] = endpoints
        path.write_text(json.dumps(raw), encoding="utf-8")
        assert Baseline.load(path).endpoints == want


def test_a_baseline_round_trips_every_endpoint_it_names(tmp_path):
    p = tmp_path / "b.json"
    ends = ("https://a.invalid", "https://b.invalid")
    Baseline.empty("s", ends).write(p, [], measured="2026-01-01", surface=OperationSurface(()))
    assert Baseline.load(p).endpoints == ends
    assert json.loads(p.read_text(encoding="utf-8"))["endpoints"] == list(ends)


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


def test_a_google_document_is_read_once_for_every_contract_it_carries(monkeypatch):
    """A discovery document describes operations under `resources`, a batch endpoint in its
    top-level `batchPath` and, for a document a walk reads, the messages its methods answer with
    under `schemas` (`google_discovery_diff.types_measure`), and a Google source reads it once for
    all three."""
    URL = "https://gmail.invalid/rest"
    doc = {
        "id": "gmail:v1",
        "name": "gmail",
        "batchPath": "batch",
        "resources": {
            "users": {
                "methods": {
                    "getProfile": {
                        "id": "gmail.users.getProfile",
                        "path": "gmail/v1/users/{userId}/profile",
                        "httpMethod": "GET",
                        "response": {"$ref": "Profile"},
                    }
                }
            }
        },
        "schemas": {
            "Profile": {
                "properties": {
                    "emailAddress": {"type": "string"},
                    "labels": {"type": "array", "items": {"$ref": "Label"}},
                    "history": {"$ref": "History"},
                    "byKey": {"type": "object", "additionalProperties": {"$ref": "Unreached"}},
                }
            },
            "Label": {"properties": {"id": {"type": "string"}, "name": {"type": "string"}}},
            "History": {"properties": {"id": {"type": "string"}}},
            "Unreached": {"properties": {"x": {"type": "string"}}},
        },
    }
    fetched = []

    def fake_fetch(url, *, timeout=120.0):
        fetched.append(url)
        return doc

    monkeypatch.setattr(google_discovery_diff, "fetch_json", fake_fetch)

    def walk(timeout):
        profile = {"emailAddress": "a@b", "labels": [{"id": "1"}], "nickname": "a"}
        return [("gmail.users.getProfile", profile)]

    c = comparisons.GoogleDiscoveryComparison(
        name="gmail",
        specs=(comparisons.Spec(URL, ("/gmail",), walk=walk),),
        # A route that cannot answer what this document declares, so the batch check has something
        # to say and reading it off the memoized document is what the assertion below proves.
        batch_mount=("/batch/{api}/{version}",),
    )
    measured = c.measure()
    found = measured.findings
    assert fetched == [URL]
    assert [(f.kind, f.path) for f in found if "batch" in f.kind] == [
        ("extra_batch_route", "/batch/{}/{}"),
        ("missing_batch_path", "gmail:v1 batch"),
    ]
    assert [(f.kind, f.path) for f in found if "field" in f.kind] == [
        ("extra_field", "gmail.Profile.nickname"),
        ("missing_field", "gmail.Label.name"),
        ("missing_field", "gmail.Profile.byKey"),
        ("missing_field", "gmail.Profile.history"),
    ]
    written = measured.surface.to_json(found)
    profile = next(e for e in written["operations"] if e.get("id") == "gmail.users.getProfile")
    assert profile["response"] == "gmail.Profile"
    assert "root" not in written
    assert written["types"]["gmail.Profile"]["status"] == "implemented"
    assert written["types"]["gmail.Profile"]["fields"] == {
        "byKey": {
            "status": "missing",
            "type": "map<string, gmail.Unreached>",
            "kind": "missing_field",
            "severity": "gap",
        },
        "emailAddress": {"status": "implemented", "type": "string"},
        "history": {
            "status": "missing",
            "type": "gmail.History",
            "kind": "missing_field",
            "severity": "gap",
        },
        "labels": {"status": "implemented", "type": "[gmail.Label]"},
        "nickname": {
            "status": "extra",
            "kind": "extra_field",
            "severity": "breaking",
            "detail": "Backlot serves it, and the vendor's message declares no such field",
        },
    }
    assert written["types"]["gmail.History"] == {
        "type_kind": "object",
        "status": "missing",
        "fields": {"id": {"status": "missing", "type": "string"}},
    }
    assert "gmail.Unreached" not in written["types"]
    # Each finding has an element to sit on, operations, batch and types alike.
    _, back = GraphSurface.from_json(written)
    assert sorted(f.key for f in back) == sorted(f.key for f in found)


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
    monkeypatch.setattr(
        comparisons.openapi_diff, "inventory", lambda served, vendor, **k: OperationSurface(())
    )

    c = comparisons.OpenAPIComparison(
        name="two",
        specs=(
            comparisons.Spec(INDEX, ("/a",), resolve_url=lambda d: "https://doc.invalid/a.json"),
            comparisons.Spec(INDEX, ("/b",), resolve_url=lambda d: "https://doc.invalid/b.json"),
        ),
    )
    c.measure()

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
        (CSDL, comparisons.CSDLComparison),
        (PROBE, comparisons.ProbeComparison),
        (SLACK, comparisons.SlackComparison),
    ]
    assert sum(len(r) for r, _ in registries) == len(COMPARISONS)
    for registry, expected in registries:
        for name, comparison in registry.items():
            assert isinstance(comparison, expected), f"{name} is a {type(comparison).__name__}"


def test_every_comparison_ships_a_baseline(tmp_path):
    """Shipped so an installed copy can be compared without the repository, and in the form a run
    writes it. A run against the vendor reads only the findings in the file, never an operation's
    status or id, and the routers that refuse an operation Backlot does not serve read only the
    operations recorded `missing` — so a hand edit to either goes unseen unless it is held here."""
    for name, comparison in COMPARISONS.items():
        baseline = Baseline.load(baseline_path(name))
        assert baseline.source == name
        assert baseline.endpoints == comparison.endpoints, name
        # Written back from what it loaded, it is the same file: a spelling `--update-baseline`
        # writes differently from the shipped one would churn the next run's diff.
        out = tmp_path / f"{name}.json"
        acknowledged = baseline.acknowledged.values()
        baseline.write(out, acknowledged, measured=baseline.measured, surface=baseline.surface)
        assert out.read_text(encoding="utf-8") == baseline_path(name).read_text(encoding="utf-8"), (
            name
        )

        entries = baseline.operations
        names = [e.operation for e in entries]
        assert names == sorted(set(names)), f"{name}: one entry per operation, in order"
        surface = baseline.surface
        statuses = {e.status for e in entries}
        if isinstance(surface, GraphSurface):
            statuses |= set(surface.schema.elements().values())
        elif isinstance(surface, SchemaSurface):
            statuses |= set(surface.elements().values())
        elif surface.batch is not None:
            statuses |= {d.status for d in surface.batch.documents}
            statuses |= {r.status for r in surface.batch.routes}
        allowed = {IMPLEMENTED, MISSING, EXTRA}
        if isinstance(comparison, comparisons.ProbeComparison):
            allowed.add(REFUSED)
        assert statuses <= allowed, f"{name}: status {statuses - allowed}"
        # An operation's finding agrees with its status: one Backlot does not serve is `missing`.
        status = {e.operation: e.status for e in entries}
        for f in baseline.acknowledged.values():
            want = {"missing_operation": MISSING, "extra_operation": EXTRA}.get(f.kind)
            assert want in (None, status.get(f.path)), f"{name}: {f.key} on {status.get(f.path)}"
        for e in entries:
            assert bool(e.id) == (e.status != EXTRA), f"{name}: id {e}"


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


def test_every_served_element_is_one_its_baseline_records_served():
    """With no network: the operations and GraphQL elements read off the app and the SDL are exactly
    the ones the shipped file records Backlot serving, and no served parameter is recorded
    `missing`. One the vendor has, added without rewriting the baseline, stays recorded `missing`,
    which a run against the vendor reports as "acknowledged but no longer diverging".

    S3's served surface is what a running Backlot answers, and that server runs here: only
    botocore's model is fetched over the network, and botocore ships a copy. The operations that
    copy lacks go unchecked."""
    from backlot.main import app

    spec = app.openapi()
    for name, comparison in COMPARISONS.items():
        baseline = Baseline.load(baseline_path(name))
        assert baseline.operations, f"{name}: the baseline names no operations"
        rerun = f"{name}: rerun `backlot diff -s {name} --update-baseline`"
        recorded_ops = {
            e.operation for e in baseline.operations if e.status in (IMPLEMENTED, EXTRA)
        }
        # A probe's and a CSDL walk's served surface is what a running Backlot answers; a CSDL
        # source's operations are still the app's routes under its mount.
        if isinstance(comparison, comparisons.CSDLComparison):
            mine = operations.from_backlot(spec, comparison.mount, comparison.mount[0])
            assert recorded_ops == {str(op) for op in mine.values()}, rerun
            continue
        if isinstance(comparison, comparisons.ProbeComparison):
            continue
        if isinstance(comparison, comparisons.GraphQLComparison):
            sdl = backlot_schema(name)
            mine = schema_surface(sdl, sdl)
            # A type of another kind on the vendor's side is compared as a whole, so the file
            # records none of its members.
            whole = {f.path for f in baseline.acknowledged.values() if f.kind == "kind_mismatch"}
            served = {p for p in mine.elements() if p.split(".")[0] not in whole} | whole
            recorded = baseline.surface.elements()
            assert {p for p, status in recorded.items() if status in (IMPLEMENTED, EXTRA)} == (
                served
            ), rerun
            assert baseline.surface.query == mine.query, rerun
            kinds = {n: t.kind for n, t in baseline.surface.types.items()}
            assert kinds == {n: t.kind for n, t in mine.types.items()}, rerun
            continue
        if isinstance(comparison, comparisons.SlackComparison):
            served_ops = {
                n: set(m.arguments)
                for n, m in slack_docs.from_backlot(spec, comparison.mount).items()
            }
        else:
            served_ops = {
                str(op): op.params
                for s in comparison.specs
                for op in operations.from_backlot(spec, s.mount, s.strip).values()
            }
        assert recorded_ops == set(served_ops), rerun
        served_params = {f"{o}?{p}" for o, params in served_ops.items() for p in params}
        missing = {f.path for f in baseline.acknowledged.values() if f.kind == "missing_param"}
        assert not served_params & missing, rerun
        extra = {f.path for f in baseline.acknowledged.values() if f.kind == "extra_param"}
        assert extra <= served_params, rerun
        assert {p.rpartition("?")[0] for p in missing | extra} <= set(served_ops), rerun
        batch = baseline.surface.batch
        routes = {r.route for r in (batch.routes if batch else ()) if r.status != MISSING}
        assert routes == {"/" + operations.canonical(r) for r in _batch_routes(comparison)}, rerun
        # A walked document's types: what a running Backlot serves, read through the types and
        # responses the file records, is exactly what it records served.
        walks = [s.walk for s in getattr(comparison, "specs", ()) if getattr(s, "walk", None)]
        assert isinstance(baseline.surface, GraphSurface) == bool(walks), rerun
        if walks:
            schema = baseline.surface.schema
            types = {n: {f: m.type for f, m in t.fields.items()} for n, t in schema.types.items()}
            answers = {e.id: e.response for e in baseline.operations if e.response}
            served: dict[str, set[str]] = {}
            for method, body in (read for walk in walks for read in walk(20.0)):
                google_discovery_diff.served_fields(types, answers[method], body, served)
            elements = set(served) | {f"{n}.{f}" for n, fs in served.items() for f in fs}
            recorded = {p for p, st in schema.elements().items() if st in (IMPLEMENTED, EXTRA)}
            assert recorded == elements, rerun

    pytest.importorskip("botocore")
    from botocore.loaders import Loader

    import backlot

    model = Loader().load_service_model("s3", "service-2")
    with backlot.serve() as server:
        measured = PROBE["s3"].run(server.base_url, model, 20.0)
    recorded = {e.operation: e for e in Baseline.load(baseline_path("s3")).operations}
    assert measured.surface.operations
    for e in measured.surface.operations:
        assert recorded.get(e.operation) == e, "s3: rerun `backlot diff -s s3 --update-baseline`"


def test_every_acknowledged_breaking_divergence_carries_its_reasoning():
    """No flag writes one: a breaking entry is a hand edit, and the note is what a reviewer reads
    in the diff instead of a flag on a command nobody kept."""
    for name in COMPARISONS:
        for f in Baseline.load(baseline_path(name)).acknowledged.values():
            if f.severity == BREAKING:
                assert f.note, f"{name}: {f.kind} {f.path}"


def test_a_kind_of_comparison_the_dispatcher_does_not_know_says_so():
    with pytest.raises(FidelityError, match="not a kind of comparison"):
        comparisons.measure(object())


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
        "EXTRA",
        "IMPLEMENTED",
        "MISSING",
        "REFUSED",
        "Measurement",
        "OperationEntry",
        "baseline_path",
        "measure",
    }
    assert set(package.__all__) == surface
    assert all(hasattr(package, name) for name in surface)
    imported = {
        alias.name
        for node in ast.walk(ast.parse(Path(cli.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module == "backlot.fidelity"
        for alias in node.names
    }
    assert imported, "the command imports nothing from the package; the parse is wrong"
    assert imported <= surface, imported


# --------------------------------------------------------------------------- published specs


A_DISCOVERY_DOC = {
    "name": "drive",
    "servicePath": "drive/v3/",
    "parameters": {"fields": {"location": "query"}, "alt": {"location": "query"}},
    "resources": {
        "files": {
            "methods": {
                "list": {
                    "id": "drive.files.list",
                    "httpMethod": "GET",
                    "path": "files",
                    "parameters": {"q": {"location": "query"}},
                    "response": {"$ref": "FileList"},
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
            "get": {
                "operationId": "conversations_list",
                "parameters": [{"name": "limit", "in": "query"}, {"$ref": "#/x/cursor"}],
            },
            "post": {},
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
    # The method's own `id` is the vendor's name for it; a method declaring none has no name.
    assert found[("get", "drive/v3/files")].id == "drive.files.list"
    assert found[("get", "drive/v3/files/{}")].id == ""
    # And its `response` the message it answers with, keyed as the baseline's `types` are.
    assert found[("get", "drive/v3/files")].response == "drive.FileList"
    assert found[("get", "drive/v3/files/{}")].response == ""


def test_an_openapi_document_follows_refs_and_keeps_path_level_parameters():
    found = openapi_diff.from_openapi(AN_OPENAPI_DOC)
    assert found[("get", "conversations.list")].params == frozenset({"limit", "cursor", "token"})
    assert found[("get", "conversations.list")].id == "conversations_list"
    assert found[("post", "conversations.list")].id == ""


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


def test_operation_divergences_are_classified_paired_and_written_like_schema_ones():
    served = operations.from_backlot(
        {"paths": {"/x/a": {"get": {"parameters": [{"name": "n", "in": "query"}]}}}}, ["/x"], "/x"
    )
    vendor = {
        ("get", "a"): operations.Operation("get", "a", frozenset({"m"}), "get-a"),
        ("get", "b"): operations.Operation("get", "b", frozenset(), "get-b"),
    }
    by_kind = {f.kind: f.severity for f in operations.diff_operations(served, vendor)}
    assert by_kind == {"extra_param": BREAKING, "missing_param": GAP, "missing_operation": GAP}

    # The operations pair the same way: each is on both sides, the vendor's only or Backlot's only,
    # named as the vendor's document names it, and a served route with no path item in the
    # document has no name.
    served[("post", "a")] = operations.Operation("post", "a", frozenset())
    surface = operations.inventory(served, vendor)
    assert surface.operations == (
        OperationEntry("GET /a", IMPLEMENTED, "get-a"),
        OperationEntry("GET /b", MISSING, "get-b"),
        OperationEntry("POST /a", EXTRA, ""),
    )

    # Each finding is written onto the operation it names, a parameter's and an answer's field's
    # under its operation, and read back off it; a gap's detail is regenerated by every run and is
    # not kept. An operation's name can hold a space, so a field is what follows the whole of it.
    findings = operations.diff_operations(served, vendor) + [
        Finding("missing_field", GAP, "GET /a user.profile", ""),
        Finding("param_type_mismatch", BREAKING, "GET /b?k", "vendor: string, Backlot: number"),
        Finding("type_mismatch", BREAKING, "GET /b user.id", "vendor: string, Backlot: number"),
    ]
    written = surface.to_json(findings)
    assert written["operations"][0]["params"] == {
        "m": {"kind": "missing_param", "severity": GAP},
        "n": {
            "kind": "extra_param",
            "severity": BREAKING,
            "detail": "Backlot accepts it; the vendor's spec declares no such parameter",
        },
    }
    assert written["operations"][0]["fields"] == {
        "user.profile": {"kind": "missing_field", "severity": GAP}
    }
    assert written["operations"][1] == {
        "operation": "GET /b",
        "id": "get-b",
        "status": MISSING,
        "kind": "missing_operation",
        "severity": GAP,
        "params": {
            "k": {
                "kind": "param_type_mismatch",
                "severity": BREAKING,
                "detail": "vendor: string, Backlot: number",
            }
        },
        "fields": {
            "user.id": {
                "kind": "type_mismatch",
                "severity": BREAKING,
                "detail": "vendor: string, Backlot: number",
            }
        },
    }
    read, back = OperationSurface.from_json(written)
    assert read == surface
    assert {(f.kind, f.path) for f in back} == {(f.kind, f.path) for f in findings}

    # An element holds one finding and a finding names one element, and an operation is one entry,
    # so writing refuses a second finding on an element or one no element carries, and reading
    # refuses an operation listed twice, rather than dropping any of them.
    with pytest.raises(ValueError, match="two findings name one element"):
        surface.to_json([*findings, Finding("extra_operation", BREAKING, "GET /b", "")])
    with pytest.raises(ValueError, match="naming no element"):
        surface.to_json([*findings, Finding("missing_operation", GAP, "GET /c", "")])
    with pytest.raises(ValueError, match="naming no element"):
        surface.to_json([Finding("extra_batch_route", BREAKING, "/batch", "")])
    with pytest.raises(ValueError, match="listed twice"):
        OperationSurface.from_json(
            {"operations": [*written["operations"], written["operations"][1]]}
        )


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
    measured = google_discovery_diff.batch_measure(
        GOOGLE_BATCH_MOUNT, _batch_docs(declared, spelled)
    )
    assert [(f.kind, f.severity, f.path) for f in measured.findings] == expected
    # Every one is written onto the document or the route it names, and read back off it.
    _, back = OperationSurface.from_json(measured.surface.to_json(measured.findings))
    assert sorted(f.key for f in back) == sorted(f.key for f in measured.findings)
    stray = Finding("extra_batch_route", BREAKING, "/nowhere", "")
    with pytest.raises(ValueError, match="naming no element"):
        measured.surface.to_json([*measured.findings, stray])
    # And each element has the status its finding gives it, `implemented` where none names it.
    kinds = {f.path: f.kind for f in measured.findings}
    status = {"missing_batch_path": MISSING, "extra_batch_api": EXTRA, "extra_batch_route": EXTRA}
    elements = [(d.path, d.status) for d in measured.surface.batch.documents]
    elements += [(r.route, r.status) for r in measured.surface.batch.routes]
    for path, got in elements:
        assert got == status.get(kinds.get(path), IMPLEMENTED), path


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
        "GetBucketCors": {"http": {"method": "GET", "requestUri": "/{Bucket}?cors"}},
        "GetObjectTagging": {"http": {"method": "GET", "requestUri": "/{Bucket}/{Key+}?tagging"}},
        "ListBuckets": {"http": {"method": "GET", "requestUri": "/"}},
        "ListDirectoryBuckets": {
            "http": {"method": "GET", "requestUri": "/"},
            "staticContextParams": {"UseS3ExpressControlEndpoint": {"value": True}},
        },
        # The flag sits on bucket operations too: botocore sends PutBucketAcl to the control
        # endpoint for a directory bucket (`--x-s3`) and to the S3 endpoint for any other.
        "PutBucketAcl": {
            "http": {"method": "PUT", "requestUri": "/{Bucket}?acl"},
            "staticContextParams": {"UseS3ExpressControlEndpoint": {"value": True}},
        },
        "WriteGetObjectResponse": {
            "http": {"method": "POST", "requestUri": "/WriteGetObjectResponse"},
            "staticContextParams": {"UseObjectLambdaEndpoint": {"value": True}},
        },
        "ListParts": {
            "http": {"method": "GET", "requestUri": "/{Bucket}/{Key+}"},
            "input": {"shape": "ListPartsRequest"},
        },
        # A flag set false, and one not spelled as an object, send it nowhere else.
        "GetServiceFlaggedOff": {
            "http": {"method": "GET", "requestUri": "/"},
            "staticContextParams": {
                "UseS3ExpressControlEndpoint": {"value": False},
                "UseObjectLambdaEndpoint": True,
            },
        },
        # A word of its own, sent to the S3 endpoint: asked at that word.
        "GetWord": {"http": {"method": "GET", "requestUri": "/Word"}},
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


def test_the_s3_model_yields_its_operations_keyed_by_what_selects_them():
    found = {o.name: o for o in s3_probe.operations(S3_MODEL)}
    # A write is asked too, as one Backlot must refuse.
    assert found["GetBucketAcl"].reads and not found["PutBucketAcl"].reads
    assert s3_probe.S3Operation("HeadObject", "HEAD", "object", "").reads
    # The one path naming a word of its own rather than a bucket or a key is named by that word.
    assert str(found["WriteGetObjectResponse"]) == (
        "WriteGetObjectResponse: POST /WriteGetObjectResponse"
    )
    # Every other is named by the bucket, the object or the service root it is asked at.
    assert str(found["GetBucketAcl"]) == "GetBucketAcl: GET /{bucket}?acl"
    # An operation carrying both flags is sent where the Object Lambda one says.
    both = {
        "UseObjectLambdaEndpoint": {"value": True},
        "UseS3ExpressControlEndpoint": {"value": True},
    }
    assert s3_probe._elsewhere({"staticContextParams": both}, "service") == (
        "the S3 Object Lambda endpoint"
    )
    assert (found["GetBucketAcl"].target, found["GetBucketAcl"].query) == ("bucket", "acl")
    assert found["GetObjectTagging"].target == "object"
    assert found["ListBuckets"].query == ""
    # ListParts shares GetObject's `/{Bucket}/{Key+}` and is selected by a REQUIRED querystring
    # member: read from the URI alone it comes out bare, where the fallthrough cannot be told
    # apart, while the server answers it with the object body.
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


_LISTING = '<?xml version="1.0"?><ListBucketResult><Name>b</Name></ListBucketResult>'


def _s3_error(code: str) -> str:
    return f"<Error><Code>{code}</Code></Error>"


@pytest.mark.parametrize(
    "answers, expected",
    [
        # The failure a path diff cannot see: not refused, not implemented, answered 200 with
        # whatever the catch-all route returns; `NotImplemented`, the refusal; and a write
        # answered as though it ran.
        (
            {"acl": (200, _LISTING), "tagging": (400, _s3_error("NotImplemented"))},
            {
                "GetBucketAcl": (MISSING, "silent_fallthrough", BREAKING),
                "GetObjectTagging": (MISSING, "missing_operation", GAP),
                "PutBucketAcl": (IMPLEMENTED, "answered_write", BREAKING),
                "ListBuckets": (IMPLEMENTED, None),
                "GetServiceFlaggedOff": (IMPLEMENTED, None),
                # Each is served only at another endpoint, and not asked.
                "ListDirectoryBuckets": (MISSING, "missing_operation", GAP),
                "WriteGetObjectResponse": (MISSING, "missing_operation", GAP),
            },
        ),
        # The error real gives an operation is its answer, as real's `NoSuchCORSConfiguration` is
        # for a bucket nobody configured; the same status with `NotImplemented` is still the
        # refusal.
        (
            {
                "cors": (404, _s3_error("NoSuchCORSConfiguration")),
                "tagging": (404, _s3_error("NotImplemented")),
            },
            {
                "GetBucketCors": (IMPLEMENTED, None),
                "GetObjectTagging": (MISSING, "missing_operation", GAP),
            },
        ),
        # Any other error is one real does not give: a 405 where real's is a 200, and a 404 of
        # another code where real's is the 404 above.
        (
            {
                "acl": (405, _s3_error("MethodNotAllowed")),
                "cors": (404, _s3_error("NoSuchBucket")),
            },
            {
                "GetBucketAcl": (IMPLEMENTED, "unexpected_error", BREAKING),
                "GetBucketCors": (IMPLEMENTED, "unexpected_error", BREAKING),
                "PutBucketAcl": (REFUSED, None),
            },
        ),
        # `NotImplemented` is the refusal only under an error status.
        (
            {"tagging": (200, _s3_error("NotImplemented"))},
            {"GetObjectTagging": (IMPLEMENTED, None)},
        ),
    ],
    ids=[
        "another-operations-body",
        "a-vendors-own-error",
        "an-error-real-does-not-give",
        "a-success-naming-the-refusal",
    ],
)
def test_an_operation_is_judged_by_what_the_server_answers_it_with(monkeypatch, answers, expected):
    asked = set()

    def fake(method, url, headers=None, timeout=None):
        asked.add((method, url))
        for selector, (status, text) in answers.items():
            if f"?{selector}" in url:
                return httpx.Response(status, text=text)
        return httpx.Response(200, text=_LISTING)

    monkeypatch.setattr(s3_probe.httpx, "request", fake)
    measured = s3_probe.probe(
        "http://x", "ak", "sk", s3_probe.operations(S3_MODEL), bucket="b", key="k"
    )
    # Each is asked at its bucket or its object, and one served only at another endpoint is not.
    assert {("GET", "http://x/s3/b?acl"), ("GET", "http://x/s3/b/k?tagging")} <= asked
    assert ("GET", "http://x/s3/Word") in asked
    assert not {m for m, u in asked if m == "POST"}
    found = {f.path.split(":")[0]: f for f in measured.findings}
    statuses = {e.id: e.status for e in measured.surface.operations}
    for operation, (status, *finding) in expected.items():
        got = found.get(operation)
        assert statuses[operation] == status, operation
        assert ([None] if got is None else [got.kind, got.severity]) == finding, operation
    _, back = OperationSurface.from_json(measured.surface.to_json(measured.findings))
    assert sorted(f.key for f in back) == sorted(f.key for f in measured.findings)


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
        ).findings
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
        return Measurement([], OperationSurface(()))

    monkeypatch.setattr(s3_probe, "fetch_json", lambda url, timeout: {"operations": {}})
    probe = comparisons.ProbeComparison("x", "https://e.invalid", fake_run)
    assert comparisons.measure(probe, timeout=5) == Measurement([], OperationSurface(()))
    assert seen["model"] == {"operations": {}} and seen["timeout"] == 5
    assert seen["base_url"].startswith("http")


# --------------------------------------------------------------------------- CSDL


# A CSDL small enough to read, carrying every feature the parser has to get right: an alias
# (`g.`), inheritance through `BaseType`, an open type beside a closed one, an enum, a navigation
# property, `Nullable="false"`, and a type name declared in TWO namespaces.
CSDL_DOC = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0">
  <edmx:DataServices>
    <Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="vendor.api" Alias="g">
      <EnumType Name="importance">
        <Member Name="normal" Value="0"/>
        <Member Name="high" Value="1"/>
      </EnumType>
      <EntityType Name="entity" Abstract="true">
        <Property Name="id" Type="Edm.String" Nullable="false"/>
      </EntityType>
      <EntityType Name="message" BaseType="g.entity">
        <Property Name="body" Type="Edm.String" Nullable="false"/>
        <Property Name="importance" Type="g.importance"/>
        <NavigationProperty Name="replies" Type="Collection(g.message)"/>
      </EntityType>
      <EntityType Name="person" BaseType="g.entity" OpenType="true">
        <Property Name="mail" Type="Edm.String"/>
      </EntityType>
    </Schema>
    <Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="vendor.security">
      <EntityType Name="person">
        <Property Name="riskLevel" Type="Edm.String"/>
      </EntityType>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""


@pytest.fixture(scope="module")
def csdl():
    return csdl_diff.schema(CSDL_DOC)


def test_a_csdl_type_carries_what_it_inherits_and_is_keyed_by_its_namespace(csdl):
    """`id` is declared once, on an abstract base, so without the walk through `BaseType` every
    entity in every response reports it as invented.

    And the two `person` types are why the key is fully qualified: keyed by bare name, one
    namespace silently answers for the other, and every property of the one that lost is reported
    as undeclared.
    """
    message = csdl.declared("vendor.api.message")
    assert set(message.properties) == {"id", "body", "importance"}
    assert message.navigations == frozenset({"replies"})
    assert message.required == frozenset({"id", "body"})
    assert not message.open

    assert csdl.declared("vendor.api.person").open  # OpenType is inherited-or-own, not overridden
    assert set(csdl.declared("vendor.security.person").properties) == {"riskLevel"}
    assert csdl.members("g.importance") == frozenset({"normal", "high"})
    assert csdl.members("Edm.String") is None


@pytest.mark.parametrize(
    "type_name, obj, expected",
    [
        # A closed type has no field to bind an undeclared property to.
        (
            "vendor.api.message",
            {"id": "1", "invented": 1},
            ["undeclared_property:message.invented"],
        ),
        # An open one is declared to accept them, so reporting it would report what the vendor
        # permits. Two of Graph's own Teams types are open.
        ("vendor.api.person", {"id": "1", "invented": 1}, []),
        # `$expand` serves a navigation property inline, which is a request the vendor documents.
        ("vendor.api.message", {"id": "1", "replies": []}, []),
        (
            "vendor.api.message",
            {"id": "1", "body": None},
            ["null_where_declared_non_nullable:message.body"],
        ),
        # `Nullable="false"` means "not null WHEN RETURNED", never "always returned": these APIs
        # project, so a declared property the response omits is the normal case.
        ("vendor.api.message", {"id": "1"}, []),
        (
            "vendor.api.message",
            {"id": "1", "importance": "URGENT"},
            ["value_outside_enum:message.importance"],
        ),
        ("vendor.api.message", {"id": "1", "importance": "high"}, []),
        # OData plumbing is not a property of the entity.
        ("vendor.api.message", {"id": "1", "@odata.context": "x", "replies@odata.count": 2}, []),
    ],
)
def test_what_a_served_object_is_and_is_not_held_to(csdl, type_name, obj, expected):
    found = csdl_diff.check(csdl, type_name, obj, "GET /messages")
    assert [f"{f.kind}:{f.path}" for f in found] == expected
    assert all(f.severity == BREAKING for f in found)
    assert all("GET /messages" in f.detail for f in found)


def test_a_type_the_walk_names_and_the_vendor_does_not_is_not_a_divergence(csdl):
    """A renamed vendor type has to stop the run. Reported as a finding it would read as Backlot
    inventing a property, and the baseline would then acknowledge a name that no longer exists."""
    with pytest.raises(FidelityError, match="not a type this vendor's document declares"):
        csdl_diff.check(csdl, "vendor.api.chat", {"id": "1"}, "GET /chats")

    with pytest.raises(FidelityError, match="not parseable XML"):
        csdl_diff.schema("<Edmx")
    with pytest.raises(FidelityError, match="declares no types"):
        csdl_diff.schema('<?xml version="1.0"?><Edmx/>')


def test_a_csdl_source_declares_its_walk_and_the_dispatcher_only_hands_over(monkeypatch):
    """Same handover as a probe's, and for the same reason: which request answers with which
    declared type is knowledge no document carries, so the source names it and a rename is
    something a linter catches rather than a nightly ImportError."""
    with pytest.raises(TypeError):
        comparisons.CSDLComparison(name="x", spec_url="https://example.invalid")

    seen = {}

    measured = Measurement([], SchemaSurface("GraphService", {}))

    def fake_walk(base_url, token, schema, timeout):
        seen.update(base_url=base_url, token=token, timeout=timeout, types=len(schema.types))
        return measured

    monkeypatch.setattr(csdl_diff, "fetch_text", lambda url, timeout: CSDL_DOC)
    source = comparisons.CSDLComparison("x", "https://e.invalid", fake_walk)
    assert comparisons.measure(source, timeout=5) is measured
    assert seen["timeout"] == 5 and seen["types"] == 4
    assert seen["base_url"].startswith("http")
    # The token is `serve()`'s MEASURED one, not a re-fetch of `/_meta/users`: a type nothing
    # serves is a type nothing checks, and an ACL-filtered caller would leave whole channels
    # unasked and report clean.
    assert seen["token"]


def test_the_teams_walk_asks_as_the_service_account_and_reaches_every_type_it_names():
    """The walk is the only thing that decides what gets compared, so a request it stops making is
    coverage that silently disappears. `backlot.serve()`'s own corpus is what it is asked against.
    """
    import backlot

    with backlot.serve() as server:
        with httpx.Client(
            base_url=server.base_url,
            timeout=30,
            headers={"Authorization": f"Bearer {server.token}"},
        ) as client:
            reached = {t for t, _, _ in msgraph._objects(client)}
        anonymous = httpx.get(f"{server.base_url}{msgraph.V1}/me")
    assert anonymous.status_code == 401  # the walk's answers are the authenticated ones

    named = {
        f"microsoft.graph.{n}"
        for n in (
            "user",
            "team",
            "channel",
            "aadUserConversationMember",
            "chatMessage",
            "itemBody",
            "channelIdentity",
            "chatMessageFromIdentitySet",
            "teamworkUserIdentity",
            "chatMessageReaction",
            "chatMessageReactionIdentitySet",
            "chatMessageAttachment",
            "chatMessageMention",
            "chatMessageMentionedIdentitySet",
        )
    }
    assert named <= reached


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


def test_update_baseline_records_gaps_and_leaves_breaking_findings_live(
    _vendor, tmp_path, capsys, monkeypatch
):
    """Muting a breaking finding with the flag that records a deliberate gap is how an alarm stops
    being read; the gaps are still written, or the bug stays buried under them every run.

    It writes them onto the schema it measured, every root field either side has among it, so
    the file can say which vendor operation a query asked for without asking the vendor."""
    _vendor(_both().replace("origin: Origin", "meetings: String"))
    args = ["diff", "-s", "fireflies", "--baseline-dir", str(tmp_path)]
    assert Baseline.empty("fireflies").operations is None  # no run has written one yet
    assert cli.main([*args, "--update-baseline"]) == 1
    path = tmp_path / "fireflies.json"
    baseline = Baseline.load(path)
    assert sorted((f.severity, f.path) for f in baseline.acknowledged.values()) == [
        (GAP, "Query.meetings"),
        (GAP, "Transcript.summary"),
    ]
    assert baseline.operations == (
        OperationEntry("Query.meetings", MISSING, "meetings"),
        OperationEntry("Query.origin", EXTRA),
        OperationEntry("Query.transcripts", IMPLEMENTED, "transcripts"),
    )
    assert SchemaSurface("Query", {}).operations == ()  # a file that lost its root type
    # The breaking finding is measured and left unaccepted: the element is there, bare.
    assert json.loads(path.read_text(encoding="utf-8"))["types"]["Transcript"]["fields"][
        "duration"
    ] == {"status": IMPLEMENTED}
    assert "type_mismatch" in capsys.readouterr().err

    # A measurement with a finding no element of its surface carries cannot be written: exit 3,
    # and the file is left as it was.
    before = path.read_text(encoding="utf-8")
    lost = Measurement([Finding("missing_field", GAP, "Nowhere.x", "")], SchemaSurface("Query", {}))
    monkeypatch.setattr("backlot.fidelity.measure", lambda *a, **k: lost)
    assert cli.main([*args, "--update-baseline"]) == 3
    assert path.read_text(encoding="utf-8") == before

    # Nor can a file the command cannot open or cannot write.
    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")

    for step in ("load", "write"):
        with monkeypatch.context() as m:
            m.setattr(Baseline, step, denied)
            assert cli.main([*args, "--update-baseline"]) == 3


@pytest.mark.parametrize(
    "written, refused",
    [
        # The list a file held before its findings were written onto elements, alone or left
        # beside a surface by a merge.
        (
            {"acknowledged": [{"kind": "missing_field", "severity": GAP, "path": "Query.x"}]},
            "under `acknowledged`",
        ),
        (
            {
                "types": {},
                "acknowledged": [{"kind": "missing_field", "severity": GAP, "path": "Query.x"}],
            },
            "under `acknowledged`",
        ),
        # A note put on the type rather than on the field it is about.
        (
            {
                "types": {
                    "Transcript": {"type_kind": "OBJECT", "status": "implemented", "note": "x"}
                }
            },
            "no `kind`",
        ),
        # A breaking acknowledgement spelled in another case.
        (
            {
                "types": {
                    "Transcript": {
                        "type_kind": "OBJECT",
                        "status": "implemented",
                        "kind": "extra_type",
                        "severity": "BREAKING",
                    }
                }
            },
            "`severity` is",
        ),
        # A severity, or a detail, put on the type with no `kind` beside it.
        (
            {
                "types": {
                    "Transcript": {"type_kind": "OBJECT", "status": "implemented", "severity": GAP}
                }
            },
            "no `kind`",
        ),
        (
            {
                "types": {
                    "Transcript": {"type_kind": "OBJECT", "status": "implemented", "detail": "x"}
                }
            },
            "no `kind`",
        ),
        # An element missing a key the loader reads, and keys of the wrong shape.
        ({"types": {"Transcript": {"status": "implemented"}}}, "type_kind"),
        ({"endpoints": "https://api.fireflies.ai/graphql"}, "not one URL"),
        ({"types": []}, "has no attribute"),
    ],
    ids=[
        "the-list-format",
        "the-list-beside-a-surface",
        "a-note-with-no-kind",
        "a-severity-in-another-case",
        "a-severity-with-no-kind",
        "a-detail-with-no-kind",
        "a-type-with-no-kind",
        "endpoints-a-string",
        "types-a-list",
    ],
)
def test_a_baseline_the_command_cannot_read_is_refused_not_emptied(
    written, refused, _vendor, tmp_path, capsys
):
    """Read as written, each holds a note or an acknowledgement the loader would not see, which
    `--update-baseline` would drop without a word, or is shaped so the loader cannot read it.
    Exit 3, as for a credential: the file is this repository's to fix."""
    _vendor()
    path = tmp_path / "fireflies.json"
    raw = {"source": "fireflies", "root": {"query": "Query"}, **written}
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    with pytest.raises((ValueError, KeyError, TypeError, AttributeError), match=refused):
        Baseline.load(path)
    args = ["diff", "-s", "fireflies", "--baseline-dir", str(tmp_path), "--update-baseline"]
    assert cli.main(args) == 3
    err = capsys.readouterr().err
    assert "the baseline cannot be used" in err
    assert ("it has no" in err) == (refused == "type_kind")
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("typed", [BREAKING, GAP])
def test_a_breaking_finding_acknowledged_by_hand_survives_a_rewrite(typed, _vendor, tmp_path):
    """No flag acknowledges one; a hand edit does, and a rewrite must not undo it. Whether it is
    breaking is the measurement's to say, so a hand edit typing it a gap is kept whole too."""
    _vendor(VENDOR.replace("Float", "Int"))
    args = ["diff", "-s", "fireflies", "--baseline-dir", str(tmp_path)]
    path = tmp_path / "fireflies.json"
    assert cli.main([*args, "--update-baseline"]) == 1

    def duration():
        return json.loads(path.read_text(encoding="utf-8"))["types"]["Transcript"]["fields"][
            "duration"
        ]

    edited = json.loads(path.read_text(encoding="utf-8"))
    edited["types"]["Transcript"]["fields"]["duration"] |= {
        "kind": "type_mismatch",
        "severity": typed,
        "detail": "vendor: Int, Backlot: Float",
        "note": "deliberate",
    }
    path.write_text(json.dumps(edited), encoding="utf-8")
    assert cli.main(args) == 0

    # The vendor moves again. The note was written about `vendor: Int`, and it says nothing about a
    # vendor now serving String, so the acknowledgement stops covering what is reported — going on
    # silencing it is the drift in March this command exists to catch. The entry stays in the file
    # as written, because deleting a reviewer's reasoning is not what a rewrite does.
    _vendor(VENDOR.replace("Float", "String"))
    assert cli.main(args) == 1
    assert cli.main([*args, "--update-baseline"]) == 1
    kept = duration()
    assert (kept["detail"], kept["note"]) == ("vendor: Int, Backlot: Float", "deliberate")
    assert kept["severity"] == BREAKING

    # And with the vendor back where the note was written, the rewrite keeps it acknowledged.
    _vendor(VENDOR.replace("Float", "Int"))
    assert cli.main(args) == 0
    assert cli.main([*args, "--update-baseline"]) == 0
    assert duration()["note"] == "deliberate"


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


def test_a_clean_run_says_so_and_a_stale_baseline_fails(_vendor, tmp_path, capsys):
    """An entry for a divergence the vendor no longer has fails the run as a new one does, in both
    forms, or a scheduled run stays green and files nothing about it."""
    _vendor()
    args = ["diff", "-s", "fireflies", "--baseline-dir", str(tmp_path)]
    assert cli.main(args) == 0
    assert "nothing new" in capsys.readouterr().out
    assert cli.main([*args, "--json"]) == 0
    capsys.readouterr()

    _vendor(VENDOR.replace("status: Status", "status: Status, summary: String"))
    assert cli.main([*args, "--update-baseline"]) == 0
    _vendor()
    capsys.readouterr()
    assert cli.main(args) == 1
    out = capsys.readouterr().out
    assert "no longer diverging (1)" in out and "Transcript.summary" in out
    assert "nothing new" not in out
    assert cli.main([*args, "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["new"] == []
    assert [f["path"] for f in payload["resolved"]] == ["Transcript.summary"]


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
            comparisons.measure(COMPARISONS["notion"])
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


# --------------------------------------------------------------------------- slack

# The reference as docs.slack.dev serves it, abbreviated to the parts `slack_docs` reads: an index
# that is a markdown table of links, a method page with frontmatter, one line per argument and its
# examples on one line between fences, and an object page whose tables render one cell per line.
SLACK_INDEX = """---
title: "Methods"
---

| Name | Description |
|------|-------------|
| [users.info](https://docs.slack.dev/reference/methods/users.info.md) | Gets one user. |
| [users.list](https://docs.slack.dev/reference/methods/users.list.md) | Lists all users. |
| [chat.postMessage](https://docs.slack.dev/reference/methods/chat.postmessage.md) | Sends one. |
"""

SLACK_PAGE = """---
method_name: "users.list"
http_method: "GET"
---

## Arguments {#arguments}

### Required arguments

**`token`**`string`Required

Authentication token bearing required scopes.

### Optional arguments

**`limit`**`number`Optional

**`team_id`**Optional

## Usage info {#usage-info}

**`not_an_argument`**`string`Required

```
GET /api/users.list?limit=1
```

## Response {#response}

#### Typical success response

```
{  "ok": true,  "members": [    {      "id": "W012A3CDE",      "name": "spengler",      "profile": {        "image_24": "https://a.slack-edge.com/.../..._24.jpg",        "fields": {"Xf111AAA111": {"value": "x"}}      },      "is_admin": true    },  ],  // ...  "cache_ts": 1498777272}
```

#### Typical error response

```
{  "ok": false,  "error": "invalid_cursor"}
```

Returns a list of [user objects](https://docs.slack.dev/reference/objects/user-object.md).

## Errors {#errors}
"""

SLACK_OBJECT = """---
title: "User object"
---

## Example {#example}

```
{    "user": {        "id": "U123ABC456",        "name": "sherlock",        "profile": {            "real_name": "Sherlock Holmes"        }    }}
```

```
{    "unrelated": {        "q": 1    }}
```

## Common fields {#fields}

Field

Type

Description

`deleted`

Boolean

This user has been deactivated.

`id`

String

Identifier for this workspace user.

`enterprise_user.teams`

String\\[\\]

An array of workspace IDs.

`tz_offset`

Integer or null

Number of seconds to offset UTC time by.

`team_id`

`team_id`

The workspace this user belongs to.

`who_can_share_contact_card`

Contact card sharing permission setting.

## Profile fields {#profile}

Field

Type

Description

`real_name`

String

The user's first and last name.

`status_emoji_display_info`

Array of objects

The emoji shown beside a status.

## Flags {#flags}

Property

Description

`is_bot`

Whether the user is a bot.

## File types {#types}

`pdf`

A PDF.
"""

OBJECT_URL = "https://docs.slack.dev/reference/objects/user-object.md"


def test_the_slack_reference_is_read_off_its_index_and_its_pages():
    """The index is the whole inventory, and a row it does not read is refused rather than dropped
    from it. An argument is read only inside its section, its type as JSON names it, and one with
    no type at all (`team_id`) is still an argument. A section that moved, or an argument line
    respelled, is refused rather than read as a method taking fewer arguments."""
    assert slack_docs.documented_methods(SLACK_INDEX) == {
        "users.info": "https://docs.slack.dev/reference/methods/users.info.md",
        "users.list": "https://docs.slack.dev/reference/methods/users.list.md",
        "chat.postMessage": "https://docs.slack.dev/reference/methods/chat.postmessage.md",
    }
    with pytest.raises(FidelityError, match="does not read as a method"):
        slack_docs.documented_methods(SLACK_INDEX + "| chat.update | Updates one. |\n")
    with pytest.raises(FidelityError, match="lists no methods"):
        slack_docs.documented_methods("| Name |\n|---|\n")
    assert slack_docs.arguments(SLACK_PAGE) == {"token": "string", "limit": "number", "team_id": ""}
    for respelled, refusal in [
        (SLACK_PAGE.replace("`number`", "`float`"), "types limit"),
        (SLACK_PAGE.replace("`number`Optional", "`number`Deprecated"), "argument line"),
        (SLACK_PAGE.replace("**`limit`**", "- **`limit`**"), "argument line"),
        (SLACK_PAGE.replace("{#arguments}", "{#parameters}"), "no `## Arguments` section"),
    ]:
        with pytest.raises(FidelityError, match=refusal):
            slack_docs.arguments(respelled)


def test_a_slack_response_is_its_examples_with_each_linked_object_placed_where_they_carry_it():
    """Read off success examples alone, an elision and a trailing comma taken out only outside a
    string, a request and an error example passed over, and an example's map keys collapsed to
    `{}`. The user object's first table is the object's own fields, read from where an example
    carries them, and an example sharing none of them is not the object. A later table goes under
    the container whose fields it shares (`profile`), or the object's own where none does; a table
    with no Type column types nothing, and a heading ends a table. The object goes where an example
    carries the container sharing most of its fields (`members[]`), and only where those are at
    least half of that container's."""
    obj = slack_docs.object_shape(SLACK_OBJECT)
    assert obj.fields["profile.status_emoji_display_info"] == {"array"}
    assert obj.fields["enterprise_user.teams"] == {"array"}
    assert obj.fields["tz_offset"] == {"number"}
    assert obj.fields["team_id"] == {"string"}
    assert obj.fields["who_can_share_contact_card"] == frozenset()
    assert obj.fields["is_bot"] == frozenset()
    assert not {"pdf", "unrelated", "q", "user"} & set(obj.fields)
    assert {"", "profile", "enterprise_user"} <= obj.containers

    shape = slack_docs.response_shape(SLACK_PAGE, {OBJECT_URL: obj}.__getitem__)
    assert shape.fields["members[].profile.image_24"] == {"string"}
    assert "members[].profile.fields.{}.value" in shape.fields
    assert shape.fields["members[].deleted"] == {"boolean"}
    assert shape.fields["members[].profile.real_name"] == {"string"}
    assert "error" not in shape.fields and "user.id" not in shape.fields
    assert obj.under("elsewhere").within("elsewhere") == obj
    assert slack_docs.loads('{"text": "a, }", "n": [1, ...],}') == {"text": "a, }", "n": [1]}

    with pytest.raises(FidelityError, match="does not read as JSON"):
        slack_docs.response_shape(SLACK_PAGE.replace('"ok": true,', '"ok": true'), {}.get)
    # One shared field out of the four `members[]` carries is not the object's container.
    for unplaced in ({"x": 1, "y": 2}, {"id": "U1"}):
        with pytest.raises(FidelityError, match="no example carries that object"):
            slack_docs.response_shape(
                SLACK_PAGE, {OBJECT_URL: slack_docs.shape_of(unplaced)}.__getitem__
            )


def test_a_json_shape_keeps_types_and_enters_only_what_it_looked_inside():
    """A boolean is told apart before a number, since a bool is an int to Python. An empty array
    is never a container; an empty object is where Backlot answers it, and not in an example."""
    shape = slack_docs.shape_of({"a": True, "b": 1, "c": "s", "d": None, "e": [], "f": {}})
    assert {k: set(v) for k, v in shape.fields.items()} == {
        "a": {"boolean"},
        "b": {"number"},
        "c": {"string"},
        "d": {"null"},
        "e": {"array"},
        "f": {"object"},
    }
    assert shape.containers == {"", "f"}
    assert slack_docs.shape_of({"f": {}}, empty_object_entered=False).containers == {""}


def test_slack_divergences_are_classified_like_operation_ones():
    """A method is one unit over both verbs, its arguments the union over them. Integer and number
    are one JSON type, and an argument or a field one side states no type for is no type mismatch;
    a field Backlot answers under a list the documentation never looks inside is not one it
    invented, and `null` beside a type is not another type."""
    served = slack_docs.from_backlot(
        {
            "paths": {
                "/slack/api/users.list": {
                    "get": {
                        "parameters": [
                            {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                            {"name": "cursor", "in": "query", "schema": {"type": "string"}},
                        ]
                    },
                    "post": {"parameters": [{"name": "x", "in": "query"}]},
                }
            }
        },
        ("/slack/api",),
    )
    assert served["users.list"].arguments == {"limit": "number", "cursor": "string", "x": ""}

    ours = slack_docs.shape_of(
        {
            "members": [{"id": "U1", "tz": 3, "bot": {"a": 1}, "name": None, "locale": "en"}],
            "cache_ts": "x",
        }
    )
    documented = slack_docs.Shape.union(
        [
            slack_docs.shape_of(
                {
                    "members": [{"id": "U1", "tz": "Asia/Seoul", "bot": {}, "name": "a"}],
                    "extra": [{"y": 1}],
                },
                empty_object_entered=False,
            ),
            slack_docs.Shape({"members[].locale": frozenset()}, frozenset()),
        ]
    )
    backlot = {
        "users.list": slack_docs.Method(
            "users.list", {"limit": "number", "cursor": "array", "x": "", "y": "string"}, ours
        ),
        "a.b": slack_docs.Method("a.b"),
    }
    real = {
        "users.list": slack_docs.Method(
            "users.list",
            {"limit": "number", "cursor": "string", "x": "string", "y": "", "team_id": ""},
            documented,
        ),
        "c.d": slack_docs.Method("c.d"),
    }
    found = {(f.kind, f.path): f.severity for f in slack_docs.diff_methods(backlot, real)}
    assert found == {
        ("extra_operation", "a.b"): BREAKING,
        ("missing_operation", "c.d"): GAP,
        ("missing_param", "users.list?team_id"): GAP,
        ("param_type_mismatch", "users.list?cursor"): BREAKING,
        ("extra_field", "users.list cache_ts"): BREAKING,
        ("missing_field", "users.list extra"): GAP,
        ("type_mismatch", "users.list members[].tz"): BREAKING,
    }
    assert slack_docs.inventory(backlot, real).operations == (
        OperationEntry("a.b", EXTRA, ""),
        OperationEntry("c.d", MISSING, "c.d"),
        OperationEntry("users.list", IMPLEMENTED, "users.list"),
    )


def test_slack_is_measured_off_the_reference_with_each_page_read_once(monkeypatch):
    """Only a served method's page is read, and an object page once however many methods link it.
    A page stating another method's name, or none, is refused: the index and the page must
    agree."""
    fetched = []
    pages = {
        "https://docs.slack.dev/reference/methods.md": SLACK_INDEX,
        "https://docs.slack.dev/reference/methods/users.info.md": SLACK_PAGE.replace(
            '"users.list"', '"users.info"', 1
        ),
        "https://docs.slack.dev/reference/methods/users.list.md": SLACK_PAGE,
        OBJECT_URL: SLACK_OBJECT,
    }

    def fetch(url, *, timeout):
        fetched.append(url)
        return pages[url]

    monkeypatch.setattr(slack_docs, "fetch_text", fetch)
    monkeypatch.setattr(
        slack_docs,
        "from_backlot",
        lambda spec, mount: {
            name: slack_docs.Method(name, {"limit": "number"})
            for name in ("users.info", "users.list")
        },
    )
    monkeypatch.setattr(
        slack_docs,
        "answers",
        lambda *, timeout: {
            name: slack_docs.shape_of({"ok": True, "members": []})
            for name in ("users.info", "users.list")
        },
    )
    measured = slack_docs.measure(COMPARISONS["slack"])
    assert sorted(fetched) == sorted(pages)
    assert {f.path for f in measured.findings if f.kind == "missing_param"} == {
        f"{name}?{argument}"
        for name in ("users.info", "users.list")
        for argument in ("token", "team_id")
    }
    assert "missing_field:users.list cache_ts" in {f.key for f in measured.findings}

    for stated, refusal in [
        ('method_name: "users.info"', "states method_name users.info, not users.list"),
        ("", r"states method_name \(none\), not users.list"),
    ]:
        pages["https://docs.slack.dev/reference/methods/users.list.md"] = SLACK_PAGE.replace(
            'method_name: "users.list"', stated
        )
        with pytest.raises(FidelityError, match=refusal):
            slack_docs.measure(COMPARISONS["slack"])


@pytest.mark.parametrize(
    "answer, read",
    [
        (httpx.Response(404), lambda: slack_docs.fetch_text("https://x.invalid/a.md", timeout=1)),
        (
            httpx.ConnectError("no route to host"),
            lambda: slack_docs.fetch_text("https://x.invalid/a.md", timeout=1),
        ),
        (
            httpx.Response(200, json={"ok": False, "error": "invalid_auth"}),
            lambda: slack_docs._Caller("https://x.invalid/api", "t", timeout=1)("api.test"),
        ),
        *(
            (
                httpx.Response(200, json={"ok": True, **served}),
                lambda: slack_docs._calls(
                    slack_docs._Caller("https://x.invalid/api", "t", timeout=1)
                ),
            )
            for served in (
                {},
                {"channels": [{"id": "C1"}], "messages": [{"ts": "1", "reply_count": 1}]},
                {"channels": [{"id": "C1"}], "members": [{"id": "U1"}], "messages": [{"ts": "1"}]},
            )
        ),
    ],
    ids=[
        "page-not-found",
        "unreachable",
        "backlot-refused",
        "corpus-serves-nothing",
        "corpus-serves-no-person",
        "corpus-serves-no-thread",
    ],
)
def test_a_slack_contract_that_cannot_be_read_is_not_a_divergence(answer, read, monkeypatch):
    """Exit 2, never a finding: a page that is not there, and a Backlot that refuses a call or
    serves nothing to aim one at."""

    def get(*a, **k):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(slack_docs.httpx, "get", get)
    with pytest.raises(FidelityError):
        read()


def test_the_probe_corpus_answers_every_method_backlot_serves():
    """Backlot's side is the running server on a corpus the module supplies, asked every method
    the reference is compared on, over every channel, thread and person, so a message's thread,
    reaction, edit and file fields are each answered somewhere. The baseline's field entries hold
    to it with no network, as its arguments are held to the app's own declarations: a field it
    acknowledges missing is one Backlot does not answer, and one it acknowledges extra is one
    Backlot does."""
    from backlot.main import app

    answered = slack_docs.answers(timeout=20.0)
    assert set(answered) == set(slack_docs.from_backlot(app.openapi(), ("/slack/api",)))
    history = answered["conversations.history"].fields
    for field in ("reply_count", "reactions", "edited", "files[].id"):
        assert f"messages[].{field}" in history, field
    # And with the arguments the reference's examples were sent, where an answer echoes them.
    assert {"latest", "response_metadata"} <= set(history)
    assert "args.foo" in answered["api.test"].fields

    rerun = "slack: rerun `backlot diff -s slack --update-baseline`"
    for f in Baseline.load(baseline_path("slack")).acknowledged.values():
        if f.kind in ("missing_field", "extra_field"):
            method, _, path = f.path.partition(" ")
            assert (path in answered[method].fields) == (f.kind == "extra_field"), (f.key, rerun)
