"""Comparing what Backlot serves against what a vendor does.

No network: every comparison here is built from a document or an SDL in this file. The live
surface — credentials, transport, a vendor being down — is exercised by `backlot diff` itself, on
a schedule.
"""

from __future__ import annotations

import json
import re
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
    google_discovery_diff,
    hubspot_catalog,
    openapi_diff,
    operations,
    s3_probe,
)
from backlot.fidelity.comparisons import COMPARISONS, GOOGLE_DISCOVERY, GRAPHQL, OPENAPI, PROBE
from backlot.fidelity.graphql_diff import backlot_schema, diff_schemas

VENDOR = """
type Query { transcripts(limit: Int, keyword: String): [Transcript!] }
type Transcript { id: ID!, title: String, duration: Float, status: Status }
enum Status { DONE, PROCESSING }
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
    Baseline.empty("fireflies", "https://api.fireflies.ai/graphql").write(
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
    baseline.identified_as("renamed", "https://new.invalid").write(path, known, measured="2026-10")
    written = json.loads(path.read_text())
    assert (written["source"], written["endpoint"]) == ("renamed", "https://new.invalid")
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


def test_the_comparisons_are_exactly_the_sources_backlot_serves():
    """Fidelity does not get to invent a source: `store.SOURCE_TABLE` is the canonical list, and
    the same `source_type` a BYO record carries."""
    assert set(COMPARISONS) == set(store.SOURCE_TABLE)


def test_every_comparison_is_registered_once_as_the_class_its_registry_implies():
    registries = [
        (OPENAPI, comparisons.OpenAPIComparison),
        (GOOGLE_DISCOVERY, comparisons.GoogleDiscoveryComparison),
        (GRAPHQL, comparisons.GraphQLComparison),
        (PROBE, comparisons.ProbeComparison),
    ]
    assert sum(len(r) for r, _ in registries) == len(COMPARISONS)
    for registry, expected in registries:
        for name, comparison in registry.items():
            assert isinstance(comparison, expected), f"{name} is a {type(comparison).__name__}"


def test_every_comparison_ships_a_baseline_and_mounts_something_to_compare():
    """A baseline is shipped so an installed copy can be compared without the repository; a mount
    that matches nothing would compare an empty surface and pass forever."""
    from backlot.main import app

    served = app.openapi()["paths"]
    for name, comparison in COMPARISONS.items():
        assert Baseline.load(baseline_path(name)).source == name
        if comparison in {**OPENAPI, **GOOGLE_DISCOVERY}.values():
            assert any(p.startswith(m) for p in served for m in comparison.mount), name


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
        "FidelityError",
        "Finding",
        "baseline_path",
        "divergences",
    }
    assert set(package.__all__) == surface
    assert all(hasattr(package, name) for name in surface)
    imported = set()
    for line in re.findall(
        r"^\s*from backlot\.fidelity import (.+)$", Path(cli.__file__).read_text(), re.M
    ):
        imported |= {n.split(" as ")[0].strip() for n in line.split(",")}
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
    """Slack's spec starts at /conversations.list so its mount comes off; Google's own document
    already spells drive/v3, so nothing does. Jira and Confluence share /atlassian and must not
    capture each other."""
    served = {
        "paths": dict.fromkeys(
            [
                "/slack/api/conversations.list",
                "/drive/v3/files",
                "/atlassian/rest/api/3/field",
                "/atlassian/wiki/rest/api/space",
            ],
            {"get": {}},
        )
    }

    def mounted(name):
        c = OPENAPI.get(name) or GOOGLE_DISCOVERY[name]
        return {p for _, p in operations.from_backlot(served, c.mount, c.strip)}

    assert mounted("slack") == {"conversations.list"}
    assert mounted("google_drive") == {"drive/v3/files"}
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
    assert {n for n, c in OPENAPI.items() if c.resolve_url} == {"hubspot"}


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
    }
}


def test_the_s3_model_yields_read_operations_keyed_by_what_selects_them():
    found = {o.name: o for o in s3_probe.operations(S3_MODEL)}
    assert "PutBucketAcl" not in found  # a write Backlot refuses is the right answer
    assert (found["GetBucketAcl"].target, found["GetBucketAcl"].query) == ("bucket", "acl")
    assert found["GetObjectTagging"].target == "object"
    assert found["ListBuckets"].query == ""
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


def test_an_unreadable_vendor_exits_two_not_one(monkeypatch, tmp_path, capsys):
    """A scheduled job has to tell "the vendor moved" from "we could not ask", or every outage
    files a fidelity bug."""
    monkeypatch.setenv("FIREFLIES_API_KEY", "test-key")

    def _down(*a, **k):
        raise FidelityError("api.fireflies.ai unreachable")

    monkeypatch.setattr("backlot.fidelity.graphql_diff.real_schema", _down)
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
