"""Unit tests for backlot.openapi — the MCP-ready OpenAPI derivation served at /_mock/openapi/{source}.

This is app code (not examples), so it's imported and tested directly: the slice/dedupe logic that
lets an OpenAPI→MCP bridge consume the mock's spec without operationId collisions.
"""

from __future__ import annotations

import warnings

import pytest

from backlot import openapi


def test_operation_id_picks_the_method_deterministically():
    """``route.methods`` is a SET, so FastAPI's default ``list(route.methods)[0]`` gives a
    multi-method route a suffix that depends on PYTHONHASHSEED. The method is chosen by
    _METHOD_RANK instead — GET first, the same preference ``dedupe_operations`` applies, so the
    operation that survives the collapse owns the id naming its own method."""
    from types import SimpleNamespace

    route = SimpleNamespace(
        name="conversations_history",
        path_format="/slack/api/conversations.history",
        methods={"POST", "GET"},
    )
    assert (
        openapi.unique_operation_id(route)
        == "conversations_history_slack_api_conversations_history_get"
    )

    # single-method routes are unaffected
    one = SimpleNamespace(name="search", path_format="/notion/v1/search", methods={"POST"})
    assert openapi.unique_operation_id(one) == "search_notion_v1_search_post"

    # a method _METHOD_RANK does not know still yields a stable answer
    odd = SimpleNamespace(name="x", path_format="/x", methods={"TRACE", "OPTIONS"})
    assert openapi.unique_operation_id(odd) == openapi.unique_operation_id(
        SimpleNamespace(name="x", path_format="/x", methods={"OPTIONS", "TRACE"})
    )


def test_served_operation_ids_are_stable_across_processes():
    """The property that actually matters, and the one an in-process test cannot see: a set's
    iteration order is fixed within a process, so this has to compare two interpreters started
    with different PYTHONHASHSEED values. An OpenAPI->MCP bridge keys its tools by operationId, so
    a bridge that caches tool names must not see them change when the server restarts."""
    import json
    import os
    import subprocess
    import sys

    script = (
        "import json, warnings; warnings.filterwarnings('ignore');"
        "from backlot.main import app;"
        "print(json.dumps(sorted("
        "  op['operationId']"
        "  for item in app.openapi()['paths'].values()"
        "  for m, op in item.items() if isinstance(op, dict) and 'operationId' in op)))"
    )
    runs = []
    for seed in ("0", "12345"):
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        assert out.returncode == 0, out.stderr[-2000:]
        runs.append(json.loads(out.stdout))
    assert runs[0] == runs[1], "operationIds differ between two PYTHONHASHSEED values"
    assert runs[0], "no operationIds in the served spec"


def test_qp_builds_an_openapi_query_parameter():
    """Every router advertises its honoured query params through this one helper — there used to be
    five copies of it (``_qp``/``_gqp``/``_aqp``/``_nqp``), two of which omitted ``required``."""
    assert openapi.qp("limit", "integer") == {
        "name": "limit",
        "in": "query",
        "required": False,
        "schema": {"type": "integer"},
    }
    assert openapi.qp("channel", required=True)["required"] is True
    assert openapi.qp("cursor")["schema"] == {"type": "string"}


def test_every_query_param_in_the_served_spec_declares_required():
    """A consequence of the single helper, and the reason it is worth having: the two routers that
    had their own copy emitted no ``required`` key at all, so 15 of the spec's 123 query params
    were shaped differently from the other 108 for no reason."""
    warnings.filterwarnings("ignore")
    from backlot.main import app

    missing = [
        (path, method, q["name"])
        for path, item in app.openapi()["paths"].items()
        for method, op in item.items()
        if isinstance(op, dict)
        for q in op.get("parameters", [])
        if q.get("in") == "query" and "required" not in q
    ]
    assert missing == []


def _doc():
    return {
        "paths": {
            "/github/repos/{owner}/{repo}/issues": {"get": {"operationId": "list_issues"}},
            "/github/search/issues": {"get": {"operationId": "search_issues"}},
            "/notion/v1/search": {"post": {"operationId": "notion_search"}},
        }
    }


def test_slice_keeps_only_prefix():
    out = openapi.slice_spec(_doc(), ["/github"])
    assert set(out["paths"]) == {"/github/repos/{owner}/{repo}/issues", "/github/search/issues"}
    assert "/notion/v1/search" in _doc()["paths"]  # original untouched


def test_slice_empty_raises():
    with pytest.raises(ValueError, match="no paths matched"):
        openapi.slice_spec(_doc(), ["/slack/api"])


def test_dedupe_get_post_same_path_keeps_get():
    spec = {
        "paths": {
            "/slack/api/conversations.history": {
                "get": {"operationId": "x"},
                "post": {"operationId": "x"},
            }
        }
    }
    out = openapi.dedupe_operations(spec)
    assert set(out["paths"]["/slack/api/conversations.history"]) == {"get"}


def test_dedupe_same_id_across_paths_prefers_greater_path():
    # tie-break when one id spans two paths of equal method/params: keep the greater path
    spec = {
        "paths": {
            "/rest/api/2/issue/{key}": {"get": {"operationId": "j"}},
            "/rest/api/3/issue/{key}": {"get": {"operationId": "j"}},
        }
    }
    assert set(openapi.dedupe_operations(spec)["paths"]) == {"/rest/api/3/issue/{key}"}


def test_dedupe_prefers_fewer_path_params():
    spec = {
        "paths": {
            "/batch": {"post": {"operationId": "b"}},
            "/batch/{api}/{version}": {"post": {"operationId": "b"}},
        }
    }
    assert set(openapi.dedupe_operations(spec)["paths"]) == {"/batch"}


def test_build_mcp_spec_rejects_unknown_source():
    with pytest.raises(KeyError):
        openapi.build_mcp_spec(_doc(), "s3")  # SigV4 — intentionally no bridge


def test_every_bridged_operation_id_names_its_own_method():
    """The property a bridge actually consumes. ``dedupe_operations`` keeps the GET of a GET+POST
    route, so the id it carries has to say ``_get`` — otherwise the tool a bridge exposes is a GET
    called ``..._post``. With FastAPI's set-ordered default that was wrong for 14 operations on
    roughly half of all boots."""
    warnings.filterwarnings("ignore")
    from backlot.main import app

    spec = app.openapi()
    wrong = [
        (source, path, method, item[method]["operationId"])
        for source in openapi.SOURCE_PREFIXES
        for path, item in openapi.build_mcp_spec(spec, source)["paths"].items()
        for method in item
        if method in ("get", "post", "put", "delete", "patch")
        and not item[method]["operationId"].endswith("_" + method)
    ]
    assert wrong == []


def test_build_mcp_spec_resolves_all_real_collisions():
    """Against the real app spec, every bridged source's built spec has unique operationIds
    (the raw /openapi.json carries ~14 duplicates from GET/POST and v2/v3 fidelity aliases)."""
    warnings.filterwarnings("ignore")
    from backlot.main import app

    full = app.openapi()
    for source in openapi.SOURCE_PREFIXES:
        spec = openapi.build_mcp_spec(full, source)  # raises ValueError if a collision survives
        assert spec["paths"], f"{source} sliced to empty"
