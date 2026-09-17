"""Notion's REST surface: pages, blocks, databases, data sources and search.

One file per router, so a source's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import re

import pytest

from backlot import store, synth
from backlot.routers.notion import DATA_SOURCES_VERSION
from backlot.routers.notion import router as notion_router
from tests._helpers import served_id, tiny_corpus, tok


@pytest.fixture
def notion_h(admin_h):
    """`admin_h` plus a `Notion-Version`, which every route requires the way the real API requires
    it on every request. Tests that are about the header itself build their own headers instead."""
    return {**admin_h, "Notion-Version": DATA_SOURCES_VERSION}


def _notion_routes() -> list[tuple[str, str]]:
    """Every (method, URL) this router serves, with a served id in each path placeholder -- so a
    route added later is swept by the tests below without being listed anywhere by hand.

    Read off the router rather than off ``/openapi.json``, so a route that keeps itself out of the
    schema is swept too: ``include_in_schema=False`` is a thing this repo does (the GraphQL
    sources register their POST that way), and a route invisible to the spec is exactly the one
    that would be left behind by a rule everything else follows."""
    pid = synth.notion_id("nt-runbook")
    return [
        (method, re.sub(r"\{[^}]+\}", pid, route.path))
        for route in notion_router.routes
        for method in sorted(route.methods - {"HEAD", "OPTIONS"})
    ]


def test_notion_page_retrieve_and_blocks(client, notion_h):
    pid = synth.notion_id("nt-runbook")
    r = client.get(f"/notion/v1/pages/{pid}", headers=notion_h)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "page" and body["id"] == pid
    assert body["properties"]["title"]["title"][0]["plain_text"] == "Notion On-call Runbook"
    assert body["icon"] == {"type": "emoji", "emoji": "📟"}
    ch = client.get(f"/notion/v1/blocks/{pid}/children", headers=notion_h).json()
    text = synth.notion_blocks_to_text(ch["results"])
    assert text == "# On-call\n\nCheck dashboards, roll back, page on-call."


def test_notion_dashless_id_resolves(client, notion_h):
    """Notion accepts a page id dashed, dashless, or in any case -- and all three spellings must
    resolve to the SAME document, not merely 200: a normalization bug in `_norm` could still land
    on a different (or no) row while a bare status-code check stayed green. Mutation-verified: see
    the report -- both the dash-reinsertion and the case-fold in `routers.notion._norm` were
    individually broken and each turned one of these spellings' response into a 404, then
    restored."""
    dashed = synth.notion_id("nt-runbook")
    dashless = dashed.replace("-", "")
    for spelling in (dashless, dashless.upper(), dashed.upper()):
        r = client.get(f"/notion/v1/pages/{spelling}", headers=notion_h)
        assert r.status_code == 200 and r.json()["id"] == dashed


def test_notion_search_and_comments(client, notion_h):
    s = client.post("/notion/v1/search", json={"query": "on-call"}, headers=notion_h).json()
    assert any(r["id"] == synth.notion_id("nt-runbook") for r in s["results"])
    c = client.get(
        "/notion/v1/comments", params={"block_id": synth.notion_id("nt-runbook")}, headers=notion_h
    ).json()
    assert c["results"][0]["rich_text"][0]["plain_text"] == "add rate-limiter step"
    assert c["results"][0]["object"] == "comment"


def test_notion_search_filter_database_only(client, notion_h):
    s = client.post(
        "/notion/v1/search",
        json={"query": "", "filter": {"property": "object", "value": "database"}},
        headers=notion_h,
    ).json()
    assert s["results"] and all(r["object"] == "database" for r in s["results"])
    assert any(r["id"] == synth.notion_id("nt-tasks-db") for r in s["results"])


def test_notion_users(client, notion_h):
    me = client.get("/notion/v1/users/me", headers=notion_h).json()
    assert me["object"] == "user" and me["type"] == "bot"
    lst = client.get("/notion/v1/users", headers=notion_h).json()
    assert lst["results"] and all(u["object"] == "user" for u in lst["results"])
    uid = lst["results"][0]["id"]
    assert client.get(f"/notion/v1/users/{uid}", headers=notion_h).json()["id"] == uid


def test_notion_unauth_is_401(client):
    r = client.get(f"/notion/v1/pages/{synth.notion_id('nt-runbook')}")
    assert r.status_code == 401 and r.json()["code"] == "unauthorized"


def test_notion_acl_hides_group_doc_from_outsider(client, tokens_yaml):
    pid = synth.notion_id("nt-secret")
    outsider = tok(tokens_yaml, "ava@acme.com")  # ava is engineering, not people
    v = {"Notion-Version": DATA_SOURCES_VERSION}
    r = client.get(f"/notion/v1/pages/{pid}", headers={"Authorization": f"Bearer {outsider}", **v})
    assert r.status_code == 404 and r.json()["code"] == "object_not_found"
    # the owner (hana, in people) can see it
    owner = tok(tokens_yaml, "hana@acme.com")
    assert (
        client.get(
            f"/notion/v1/pages/{pid}", headers={"Authorization": f"Bearer {owner}", **v}
        ).status_code
        == 200
    )


def test_notion_database_new_vs_legacy_shape(client, notion_h):
    did = synth.notion_id("nt-tasks-db")
    new = client.get(f"/notion/v1/databases/{did}", headers=notion_h).json()
    assert new["object"] == "database"
    assert new["data_sources"][0]["id"] == synth.notion_data_source_id("nt-tasks-db")
    assert "properties" not in new
    legacy = client.get(
        f"/notion/v1/databases/{did}", headers={**notion_h, "Notion-Version": "2022-06-28"}
    ).json()
    assert "properties" in legacy and "Status" in legacy["properties"]
    assert "data_sources" not in legacy


def test_notion_query_rows_one_path_per_version(client, notion_h):
    """Each version serves one of the two query paths and answers `invalid_request_url` on the
    other, the way real Notion does: 2025-09-03 moved row reads onto data sources and took the
    database path away, so a client cannot reach rows by whichever path it likes."""
    did = synth.notion_id("nt-tasks-db")
    dsid = synth.notion_data_source_id("nt-tasks-db")
    task = synth.notion_id("nt-task-1")
    for version, served, refused in (
        ("2025-09-03", f"data_sources/{dsid}", f"databases/{did}"),
        ("2022-06-28", f"databases/{did}", f"data_sources/{dsid}"),
    ):
        h = {**notion_h, "Notion-Version": version}
        rows = client.post(f"/notion/v1/{served}/query", json={}, headers=h)
        assert rows.status_code == 200, version
        assert any(r["id"] == task for r in rows.json()["results"]), version
        gone = client.post(f"/notion/v1/{refused}/query", json={}, headers=h)
        assert gone.status_code == 400, version
        assert gone.json() == {
            "object": "error",
            "status": 400,
            "code": "invalid_request_url",
            "message": "Invalid request URL.",
        }, version


def test_notion_version_cut_is_the_date_on_both_sides_of_it(client, notion_h):
    """The cut is the 2025-09-03 release date, not an equality test against either version this
    repo's own clients send. Below it: 2022-02-22 predates data sources just as much as 2022-06-28
    does, so it reads rows through the database and sees the schema inline. Above it: 2026-03-11 is
    what Notion publishes as current -- its OpenAPI declares `Notion-Version` with
    `enum: ["2026-03-11"]`, read 2026-09-16 -- and reads them through the data source.

    Both sides, because a comparison narrowed back to equality answers 2025-09-03 and 2022-06-28
    exactly as the date cut does: only a version outside that pair separates them, and the half of
    the rule that names no version at all ("2025-09-03 and later") is the half a client on a
    version Notion has not published yet arrives on."""
    did = synth.notion_id("nt-tasks-db")
    dsid = synth.notion_data_source_id("nt-tasks-db")
    task = synth.notion_id("nt-task-1")
    for version, served, refused, carries, lacks in (
        ("2022-02-22", f"databases/{did}", f"data_sources/{dsid}", "properties", "data_sources"),
        ("2026-03-11", f"data_sources/{dsid}", f"databases/{did}", "data_sources", "properties"),
    ):
        h = {**notion_h, "Notion-Version": version}
        rows = client.post(f"/notion/v1/{served}/query", json={}, headers=h)
        assert rows.status_code == 200, version
        assert any(r["id"] == task for r in rows.json()["results"]), version
        gone = client.post(f"/notion/v1/{refused}/query", json={}, headers=h)
        assert gone.status_code == 400 and gone.json()["code"] == "invalid_request_url", version
        db = client.get(f"/notion/v1/databases/{did}", headers=h).json()
        assert carries in db and lacks not in db, version


def test_notion_every_route_requires_the_version_header(client, admin_h, notion_h):
    """Notion requires `Notion-Version` on every REST request and answers `missing_version`
    without it, so every route here refuses a header-less request rather than reading one on a
    default. Swept off the router rather than listed, so a route added later is held to it too."""
    routes = _notion_routes()
    assert routes, "expected the notion router to have routes"
    for method, url in routes:
        r = client.request(method, url, headers=admin_h, json={} if method == "POST" else None)
        assert r.status_code == 400, (method, url)
        assert r.json() == {
            "object": "error",
            "status": 400,
            "code": "missing_version",
            "message": "Notion-Version header failed validation: Notion-Version header should be "
            "defined, instead was `undefined`.",
        }, (method, url)
    # The refusal comes before the lookup, so it is not something a failed lookup could have
    # produced: the ids swept in above are a page's, and the same URL with a version reaches the
    # lookup and is answered by it.
    url = f"/notion/v1/data_sources/{synth.notion_id('nt-runbook')}"
    assert client.get(url, headers=admin_h).json()["code"] == "missing_version"
    assert client.get(url, headers=notion_h).json()["code"] == "object_not_found"
    # A header with an empty value carries no version, so it is answered as none sent rather than
    # read as a version string -- which would sort below 2025-09-03 and quietly pick the legacy
    # model for a caller that named nothing.
    empty = client.get(url, headers={**admin_h, "Notion-Version": ""})
    assert empty.status_code == 400 and empty.json()["code"] == "missing_version"


def test_notion_credential_is_checked_before_the_version(client):
    """Ordering, measured against api.notion.com on 2026-09-15: an invalid token answered 401 on
    both query paths under 2022-06-28, under 2025-09-03 and with no version header at all, so
    neither version refusal is reachable without a credential that resolves. Held on every route
    for the missing header, and on the query pair for a version the path does not serve, because
    either refusal could be moved above the 401 without another test noticing."""
    bad = {"Authorization": "Bearer nope"}
    for method, url in _notion_routes():
        r = client.request(method, url, headers=bad, json={} if method == "POST" else None)
        assert r.status_code == 401, (method, url)
        assert r.json()["code"] == "unauthorized", (method, url)
    did = synth.notion_id("nt-tasks-db")
    dsid = synth.notion_data_source_id("nt-tasks-db")
    for path, version in (
        (f"databases/{did}", "2025-09-03"),  # the path that version does not serve
        (f"data_sources/{dsid}", "2022-06-28"),
    ):
        r = client.post(
            f"/notion/v1/{path}/query", json={}, headers={**bad, "Notion-Version": version}
        )
        assert r.status_code == 401, (path, version)
        assert r.json()["code"] == "unauthorized", (path, version)


def test_notion_data_source_retrieve(client, notion_h):
    dsid = synth.notion_data_source_id("nt-tasks-db")
    ds = client.get(f"/notion/v1/data_sources/{dsid}", headers=notion_h).json()
    assert ds["object"] == "data_source" and "Status" in ds["properties"]


# --- Notion: typed response schema ---------------------------------------------------------


def test_notion_search_documents_body_param(client):
    op = client.get("/openapi.json").json()["paths"]["/notion/v1/search"]["post"]
    props = op["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "query" in props and "filter" in props


def test_notion_every_route_declares_the_version_header(client):
    """A route that requires the header has to declare it: the spec is the whole of what a
    generated client knows, and `backlot mcp` builds its tools off this one while its bridge sends
    no version of its own (see `tests/test_mcp.py`), so an undeclared requirement would hand an
    agent tools it could not call."""
    paths = client.get("/openapi.json").json()["paths"]
    ops = [
        (method, path, op)
        for path, item in paths.items()
        if path.startswith("/notion/v1")
        for method, op in item.items()
    ]
    assert ops, "expected the notion operations in the spec"
    for method, path, op in ops:
        version = [p for p in op.get("parameters", []) if p["name"] == "Notion-Version"]
        assert len(version) == 1, (method, path)
        assert version[0]["in"] == "header" and version[0]["required"] is True, (method, path)


def test_notion_query_routes_name_the_versions_they_serve(client):
    """The version is the one parameter whose value decides which of the two query routes answers,
    so each says which versions it is the path for rather than repeating the generic sentence --
    a tool description is all an agent has to pick a value from."""
    paths = client.get("/openapi.json").json()["paths"]
    for path, serves, refuses in (
        (
            "/notion/v1/databases/{database_id}/query",
            "versions up to and including 2022-06-28",
            "2025-09-03 and later",
        ),
        (
            "/notion/v1/data_sources/{data_source_id}/query",
            "2025-09-03 and later",
            "versions before it",
        ),
    ):
        params = paths[path]["post"]["parameters"]
        version = next(p for p in params if p["name"] == "Notion-Version")
        assert version["in"] == "header" and version["required"] is True, path
        assert f"serves {serves}" in version["description"], path
        assert refuses in version["description"], path


def test_notion_page_has_typed_response_schema(client):
    op = client.get("/openapi.json").json()["paths"]["/notion/v1/pages/{page_id}"]["get"]
    assert op["responses"]["200"]["content"]["application/json"]["schema"] != {}


def test_notion_responses_unchanged_by_enrichment(client, notion_h):
    res = client.post("/notion/v1/search", json={}, headers=notion_h).json()
    assert res["object"] == "list" and "results" in res
    pages = [r for r in res["results"] if r.get("object") == "page"]
    assert pages, "expected notion pages in search"
    page = client.get(f"/notion/v1/pages/{pages[0]['id']}", headers=notion_h).json()
    for k in ("object", "id", "created_time", "last_edited_time", "properties", "parent", "url"):
        assert k in page, f"notion page missing {k} (fidelity regression)"
    dbs = [r for r in res["results"] if r.get("object") == "database"]
    if dbs:  # version-dependent database shape must survive both header values
        did = dbs[0]["id"]
        legacy = client.get(
            f"/notion/v1/databases/{did}", headers={**notion_h, "Notion-Version": "2022-06-28"}
        ).json()
        default = client.get(
            f"/notion/v1/databases/{did}", headers={**notion_h, "Notion-Version": "2025-09-03"}
        ).json()
        assert "properties" in legacy and "data_sources" in default


# --- Notion ---------------------------------------------------------------------


def _notion_conn(tmp_path):
    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "notion",
                "doc_id": "nf-page",
                "teamspace": "eng",
                "title": "Runbook",
                "content": "# On-call\n\nRoll back and page.",
                "author_email": "ava@acme.com",
                "visibility": "public",
                "icon": "📟",
                "comments": [{"content": "add rate-limiter step", "author_email": "bob@acme.com"}],
            },
            {
                "source_type": "notion",
                "doc_id": "nf-db",
                "subtype": "database",
                "teamspace": "eng",
                "title": "Tasks",
                "content": "Tracker",
                "author_email": "ava@acme.com",
                "visibility": "public",
                "properties": {"Status": {"type": "select"}},
            },
            {
                "source_type": "notion",
                "doc_id": "nf-row",
                "parent": "nf-db",
                "teamspace": "eng",
                "title": "Fix bug",
                "content": "body",
                "author_email": "bob@acme.com",
                "visibility": "public",
                "properties": {"Status": "In Progress"},
            },
        ],
    )
    return store.connect_ro(s.db_path)


def test_notion_page_shape(tmp_path):
    from backlot.routers.notion import _page_obj

    conn = _notion_conn(tmp_path)
    obj = _page_obj(conn, store.get_document(conn, "notion", served_id("notion", "nf-page")))
    assert obj["object"] == "page"
    assert obj["id"] == synth.notion_id("nf-page")
    assert obj["created_by"]["object"] == "user"
    assert obj["parent"] == {"type": "workspace", "workspace": True}
    assert obj["properties"]["title"]["type"] == "title"
    assert obj["properties"]["title"]["title"][0]["plain_text"] == "Runbook"
    assert obj["icon"] == {"type": "emoji", "emoji": "📟"}
    assert obj["url"].startswith("https://www.notion.so/")
    # a database row exposes its property values + a database_id parent
    row = _page_obj(conn, store.get_document(conn, "notion", served_id("notion", "nf-row")))
    assert row["parent"]["type"] == "database_id"
    # ...and it is the database's OWN served id, which `parent_id` already holds. Hashing
    # that column again named a database nothing serves, so every row in every database
    # advertised a parent that 404s.
    assert row["parent"]["database_id"] == served_id("notion", "nf-db")
    assert row["properties"]["Status"]["select"]["name"] == "In Progress"


def test_notion_database_and_data_source_shape(tmp_path):
    from backlot.routers.notion import _data_source_obj, _database_obj

    conn = _notion_conn(tmp_path)
    dbrow = store.get_document(conn, "notion", served_id("notion", "nf-db"))
    new = _database_obj(conn, dbrow, "2025-09-03")
    assert new["object"] == "database"
    assert new["data_sources"][0]["id"] == synth.notion_data_source_id("nf-db")
    assert "properties" not in new
    legacy = _database_obj(conn, dbrow, "2022-06-28")
    assert "data_sources" not in legacy
    assert legacy["properties"]["Status"]["type"] == "select"
    ds = _data_source_obj(conn, dbrow)
    assert ds["object"] == "data_source" and ds["properties"]["title"]["type"] == "title"


def test_notion_user_and_block_shape(tmp_path):
    from backlot.routers.notion import _user_obj

    conn = _notion_conn(tmp_path)
    u = _user_obj(conn, "ava@acme.com")
    assert u["object"] == "user" and u["type"] == "person"
    assert u["person"]["email"] == "ava@acme.com"
    assert u["id"] == synth.notion_user_id("ava@acme.com")
    blocks = synth.notion_blocks("nf-page", "# On-call\n\nRoll back and page.")
    b = blocks[0]
    assert b["object"] == "block" and b["type"] == "heading_1"
    assert b["heading_1"]["rich_text"][0]["plain_text"] == "On-call"
