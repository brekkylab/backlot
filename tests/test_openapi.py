"""Unit tests for backlot.openapi — the MCP-ready OpenAPI derivation served at /_meta/openapi/{source}.

This is app code (not examples), so it's imported and tested directly: the slice/dedupe logic that
lets an OpenAPI→MCP bridge consume Backlot's spec without operationId collisions.
"""

from __future__ import annotations

import re
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

    # HEAD ranks last, so a route serving both still names GET — the id S3's object route has
    # carried since before `head` joined _METHODS, and which must not move.
    both = SimpleNamespace(
        name="object_get", path_format="/s3/{bucket}/{key}", methods={"GET", "HEAD"}
    )
    assert openapi.unique_operation_id(both) == "object_get_s3__bucket___key__get"
    only = SimpleNamespace(name="head_bucket", path_format="/s3/{bucket}", methods={"HEAD"})
    assert openapi.unique_operation_id(only) == "head_bucket_s3__bucket__head"

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
    """Every router advertises its honoured query params through this one helper. Without it there is
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
        openapi.build_mcp_spec(_doc(), "dropbox")


def test_tool_name_inverts_unique_operation_id():
    """The MCP spec names each tool for its route, and gets that name back out of the id
    ``unique_operation_id`` built — for either method of a multi-method route, since FastAPI gives
    both the one id. An id this module did not produce is refused rather than guessed at."""
    from types import SimpleNamespace

    route = SimpleNamespace(
        name="conversations_history",
        path_format="/slack/api/conversations.history",
        methods={"POST", "GET"},
    )
    oid = openapi.unique_operation_id(route)
    assert openapi.tool_name(oid, route.path_format, "get") == "conversations_history"
    assert openapi.tool_name(oid, route.path_format, "post") == "conversations_history"
    with pytest.raises(ValueError, match="not derived from"):
        openapi.tool_name("someone_elses_id", "/slack/api/conversations.history", "get")


def test_bridged_operation_ids_are_the_route_names():
    """What a bridge exposes as a tool name is the operationId, so the served MCP spec carries the
    route's own name (``search_messages``) rather than the path-and-method suffixed form
    (``search_messages_slack_api_search_messages_get``): shorter for a model to read on every call,
    and short enough that an MCP client's 64-character cap never truncates one even under the
    ``<source>_`` namespace ``backlot mcp`` adds. The raw ``/openapi.json`` keeps the long,
    deterministic ids; only the MCP slice renames."""
    warnings.filterwarnings("ignore")
    from backlot.main import app

    spec = app.openapi()
    for source in openapi.SOURCE_PREFIXES:
        for path, item in openapi.build_mcp_spec(spec, source)["paths"].items():
            for method, op in item.items():
                if method not in openapi._METHODS:
                    continue
                oid = op["operationId"]
                raw = spec["paths"][path][method]["operationId"]
                assert oid == openapi.tool_name(raw, path, method), (source, raw, oid)
                assert re.sub(r"\W", "_", path) not in oid, (source, oid)
                assert len(source) + 1 + len(oid) <= 64, (source, oid)


def test_build_mcp_spec_collapses_the_jira_version_aliases():
    """Jira's ``/rest/api/2`` and ``/rest/api/3`` routes are one route under two paths, so under
    route-name ids they share one and ``dedupe_operations`` keeps one — the v3 one, by its
    greatest-path rule. Under the suffixed ids the pair survived as two tools apiece."""
    warnings.filterwarnings("ignore")
    from backlot.main import app

    paths = openapi.build_mcp_spec(app.openapi(), "atlassian")["paths"]
    assert not any("/rest/api/2/" in p for p in paths), sorted(paths)
    assert any(p.endswith("/rest/api/3/issue/{key}") for p in paths), sorted(paths)


def test_build_mcp_spec_resolves_all_real_collisions():
    """Against the real app spec, every bridged source's built spec has unique operationIds
    (the raw /openapi.json carries ~14 duplicates from GET/POST and v2/v3 fidelity aliases)."""
    warnings.filterwarnings("ignore")
    from backlot.main import app

    full = app.openapi()
    for source in openapi.SOURCE_PREFIXES:
        spec = openapi.build_mcp_spec(full, source)  # raises ValueError if a collision survives
        assert spec["paths"], f"{source} sliced to empty"


def test_build_mcp_spec_drops_head_operations():
    """S3 is the only source serving HEAD, and none of it reaches the bridged spec. A HEAD answers
    with headers alone, so a tool built from one returns an empty body on every call. Dropping it
    before the rename is also what keeps S3's object route — GET and HEAD under one operationId —
    from shipping as `object_get` plus an un-renamed `object_get_s3__bucket___key__get`."""
    warnings.filterwarnings("ignore")
    from backlot.main import app

    spec = app.openapi()
    assert "head" in spec["paths"]["/s3/{bucket}/{key}"], "the raw spec still describes HEAD"

    paths = openapi.build_mcp_spec(spec, "s3")["paths"]
    assert not any("head" in item for item in paths.values()), sorted(paths)
    assert sorted(paths["/s3/{bucket}/{key}"]) == ["get"]
    assert paths["/s3/{bucket}/{key}"]["get"]["operationId"] == "object_get"


def test_drop_head_operations_drops_a_path_it_empties():
    """A path whose only operation was HEAD goes with it, rather than lingering as an entry with
    no operation. Synthetic, because no real source has such a path — every ``@router.head``
    shares its path with a GET — so nothing else can cover the guard."""
    spec = {
        "paths": {
            "/only-head": {"head": {"operationId": "h"}},
            "/both": {"head": {"operationId": "b"}, "get": {"operationId": "b"}},
            "/params-only": {"parameters": [], "head": {"operationId": "p"}},
        }
    }
    assert openapi.drop_head_operations(spec)["paths"] == {"/both": {"get": {"operationId": "b"}}}
