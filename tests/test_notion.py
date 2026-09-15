"""Notion's REST surface: pages, blocks, databases, data sources and search.

One file per router, so a source's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

from backlot import store, synth
from tests._helpers import served_id, tiny_corpus, tok


def test_notion_page_retrieve_and_blocks(client, admin_h):
    pid = synth.notion_id("nt-runbook")
    r = client.get(f"/notion/v1/pages/{pid}", headers=admin_h)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "page" and body["id"] == pid
    assert body["properties"]["title"]["title"][0]["plain_text"] == "Notion On-call Runbook"
    assert body["icon"] == {"type": "emoji", "emoji": "📟"}
    ch = client.get(f"/notion/v1/blocks/{pid}/children", headers=admin_h).json()
    text = synth.notion_blocks_to_text(ch["results"])
    assert text == "# On-call\n\nCheck dashboards, roll back, page on-call."


def test_notion_dashless_id_resolves(client, admin_h):
    """Notion accepts a page id dashed, dashless, or in any case -- and all three spellings must
    resolve to the SAME document, not merely 200: a normalization bug in `_norm` could still land
    on a different (or no) row while a bare status-code check stayed green. Mutation-verified: see
    the report -- both the dash-reinsertion and the case-fold in `routers.notion._norm` were
    individually broken and each turned one of these spellings' response into a 404, then
    restored."""
    dashed = synth.notion_id("nt-runbook")
    dashless = dashed.replace("-", "")
    for spelling in (dashless, dashless.upper(), dashed.upper()):
        r = client.get(f"/notion/v1/pages/{spelling}", headers=admin_h)
        assert r.status_code == 200 and r.json()["id"] == dashed


def test_notion_search_and_comments(client, admin_h):
    s = client.post("/notion/v1/search", json={"query": "on-call"}, headers=admin_h).json()
    assert any(r["id"] == synth.notion_id("nt-runbook") for r in s["results"])
    c = client.get(
        "/notion/v1/comments", params={"block_id": synth.notion_id("nt-runbook")}, headers=admin_h
    ).json()
    assert c["results"][0]["rich_text"][0]["plain_text"] == "add rate-limiter step"
    assert c["results"][0]["object"] == "comment"


def test_notion_search_filter_database_only(client, admin_h):
    s = client.post(
        "/notion/v1/search",
        json={"query": "", "filter": {"property": "object", "value": "database"}},
        headers=admin_h,
    ).json()
    assert s["results"] and all(r["object"] == "database" for r in s["results"])
    assert any(r["id"] == synth.notion_id("nt-tasks-db") for r in s["results"])


def test_notion_users(client, admin_h):
    me = client.get("/notion/v1/users/me", headers=admin_h).json()
    assert me["object"] == "user" and me["type"] == "bot"
    lst = client.get("/notion/v1/users", headers=admin_h).json()
    assert lst["results"] and all(u["object"] == "user" for u in lst["results"])
    uid = lst["results"][0]["id"]
    assert client.get(f"/notion/v1/users/{uid}", headers=admin_h).json()["id"] == uid


def test_notion_unauth_is_401(client):
    r = client.get(f"/notion/v1/pages/{synth.notion_id('nt-runbook')}")
    assert r.status_code == 401 and r.json()["code"] == "unauthorized"


def test_notion_acl_hides_group_doc_from_outsider(client, tokens_yaml):
    pid = synth.notion_id("nt-secret")
    outsider = tok(tokens_yaml, "ava@acme.com")  # ava is engineering, not people
    r = client.get(f"/notion/v1/pages/{pid}", headers={"Authorization": f"Bearer {outsider}"})
    assert r.status_code == 404 and r.json()["code"] == "object_not_found"
    # the owner (hana, in people) can see it
    owner = tok(tokens_yaml, "hana@acme.com")
    assert (
        client.get(
            f"/notion/v1/pages/{pid}", headers={"Authorization": f"Bearer {owner}"}
        ).status_code
        == 200
    )


def test_notion_database_new_vs_legacy_shape(client, admin_h):
    did = synth.notion_id("nt-tasks-db")
    new = client.get(f"/notion/v1/databases/{did}", headers=admin_h).json()
    assert new["object"] == "database"
    assert new["data_sources"][0]["id"] == synth.notion_data_source_id("nt-tasks-db")
    assert "properties" not in new
    legacy = client.get(
        f"/notion/v1/databases/{did}", headers={**admin_h, "Notion-Version": "2022-06-28"}
    ).json()
    assert "properties" in legacy and "Status" in legacy["properties"]
    assert "data_sources" not in legacy


def test_notion_query_rows_one_path_per_version(client, admin_h):
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
        h = {**admin_h, "Notion-Version": version}
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


def test_notion_query_version_cut_is_the_date_not_one_legacy_spelling(client, admin_h):
    """A version older than 2022-06-28 predates data sources just as much, so it reads rows
    through the database and sees the schema inline -- the cut is the 2025-09-03 release date,
    not an equality test against the one legacy version Backlot's clients happen to send."""
    did = synth.notion_id("nt-tasks-db")
    dsid = synth.notion_data_source_id("nt-tasks-db")
    h = {**admin_h, "Notion-Version": "2022-02-22"}
    rows = client.post(f"/notion/v1/databases/{did}/query", json={}, headers=h)
    assert rows.status_code == 200
    assert any(r["id"] == synth.notion_id("nt-task-1") for r in rows.json()["results"])
    assert (
        client.post(f"/notion/v1/data_sources/{dsid}/query", json={}, headers=h).status_code == 400
    )
    db = client.get(f"/notion/v1/databases/{did}", headers=h).json()
    assert "properties" in db and "data_sources" not in db


def test_notion_query_without_a_version_header_is_refused(client, admin_h):
    """Real Notion requires `Notion-Version` on every request; on the query pair Backlot enforces
    it, so a client that forgets the header reaches neither path rather than being served the
    default."""
    did = synth.notion_id("nt-tasks-db")
    dsid = synth.notion_data_source_id("nt-tasks-db")
    for path in (f"databases/{did}", f"data_sources/{dsid}"):
        r = client.post(f"/notion/v1/{path}/query", json={}, headers=admin_h)
        assert r.status_code == 400, path
        assert r.json()["code"] == "missing_version", path
        assert r.json()["object"] == "error" and r.json()["status"] == 400, path


def test_notion_query_credential_is_checked_before_the_version(client):
    """Ordering, measured against api.notion.com on 2026-09-15: an invalid token answers 401 on
    both query paths under 2022-06-28, under 2025-09-03 and with no version header at all, so
    neither version refusal is reachable without a credential that resolves. Asserted because the
    refusals sit after the 401 check and could be moved above it without any other test
    noticing."""
    did = synth.notion_id("nt-tasks-db")
    dsid = synth.notion_data_source_id("nt-tasks-db")
    bad = {"Authorization": "Bearer nope"}
    for path, version in (
        (f"databases/{did}", "2025-09-03"),  # the path that version does not serve
        (f"data_sources/{dsid}", "2022-06-28"),
        (f"databases/{did}", None),  # no version header at all
        (f"data_sources/{dsid}", None),
    ):
        h = bad if version is None else {**bad, "Notion-Version": version}
        r = client.post(f"/notion/v1/{path}/query", json={}, headers=h)
        assert r.status_code == 401, (path, version)
        assert r.json()["code"] == "unauthorized", (path, version)


def test_notion_data_source_retrieve(client, admin_h):
    dsid = synth.notion_data_source_id("nt-tasks-db")
    ds = client.get(f"/notion/v1/data_sources/{dsid}", headers=admin_h).json()
    assert ds["object"] == "data_source" and "Status" in ds["properties"]


# --- Notion: typed response schema ---------------------------------------------------------


def test_notion_search_documents_body_param(client):
    op = client.get("/openapi.json").json()["paths"]["/notion/v1/search"]["post"]
    props = op["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "query" in props and "filter" in props


def test_notion_query_routes_declare_the_version_that_selects_them(client):
    """Each query route declares `Notion-Version` as a required header, naming the versions it is
    the path for. The spec is the whole of what a generated client knows -- `backlot mcp` turns
    this parameter into the tool argument that lets an agent reach either path (see
    `tests/test_mcp.py`), and the bridge sends no version of its own -- so the declaration is
    what makes the route reachable, not decoration on top of it."""
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


def test_notion_responses_unchanged_by_enrichment(client, admin_h):
    res = client.post("/notion/v1/search", json={}, headers=admin_h).json()
    assert res["object"] == "list" and "results" in res
    pages = [r for r in res["results"] if r.get("object") == "page"]
    assert pages, "expected notion pages in search"
    page = client.get(f"/notion/v1/pages/{pages[0]['id']}", headers=admin_h).json()
    for k in ("object", "id", "created_time", "last_edited_time", "properties", "parent", "url"):
        assert k in page, f"notion page missing {k} (fidelity regression)"
    dbs = [r for r in res["results"] if r.get("object") == "database"]
    if dbs:  # version-dependent database shape must survive both header values
        did = dbs[0]["id"]
        legacy = client.get(
            f"/notion/v1/databases/{did}", headers={**admin_h, "Notion-Version": "2022-06-28"}
        ).json()
        default = client.get(
            f"/notion/v1/databases/{did}", headers={**admin_h, "Notion-Version": "2025-09-03"}
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
