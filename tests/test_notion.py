"""Notion's REST surface: pages, blocks, databases, data sources and search.

One file per router, so a provider's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

from backlot import store, synth
from tests._helpers import tiny_corpus, tok


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
    pid = synth.notion_id("nt-runbook").replace("-", "")
    assert client.get(f"/notion/v1/pages/{pid}", headers=admin_h).status_code == 200


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


def test_notion_query_rows_both_paths(client, admin_h):
    did = synth.notion_id("nt-tasks-db")
    dsid = synth.notion_data_source_id("nt-tasks-db")
    rows_new = client.post(f"/notion/v1/data_sources/{dsid}/query", json={}, headers=admin_h).json()
    assert any(r["id"] == synth.notion_id("nt-task-1") for r in rows_new["results"])
    rows_legacy = client.post(
        f"/notion/v1/databases/{did}/query",
        json={},
        headers={**admin_h, "Notion-Version": "2022-06-28"},
    ).json()
    assert any(r["id"] == synth.notion_id("nt-task-1") for r in rows_legacy["results"])


def test_notion_data_source_retrieve(client, admin_h):
    dsid = synth.notion_data_source_id("nt-tasks-db")
    ds = client.get(f"/notion/v1/data_sources/{dsid}", headers=admin_h).json()
    assert ds["object"] == "data_source" and "Status" in ds["properties"]


# --- Notion: typed response schema ---------------------------------------------------------


def test_notion_search_documents_body_param(client):
    op = client.get("/openapi.json").json()["paths"]["/notion/v1/search"]["post"]
    props = op["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "query" in props and "filter" in props


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
    obj = _page_obj(conn, store.get_document(conn, "notion", "nf-page"))
    assert obj["object"] == "page"
    assert obj["id"] == synth.notion_id("nf-page")
    assert obj["created_by"]["object"] == "user"
    assert obj["parent"] == {"type": "workspace", "workspace": True}
    assert obj["properties"]["title"]["type"] == "title"
    assert obj["properties"]["title"]["title"][0]["plain_text"] == "Runbook"
    assert obj["icon"] == {"type": "emoji", "emoji": "📟"}
    assert obj["url"].startswith("https://www.notion.so/")
    # a database row exposes its property values + a database_id parent
    row = _page_obj(conn, store.get_document(conn, "notion", "nf-row"))
    assert row["parent"]["type"] == "database_id"
    assert row["properties"]["Status"]["select"]["name"] == "In Progress"


def test_notion_database_and_data_source_shape(tmp_path):
    from backlot.routers.notion import _data_source_obj, _database_obj

    conn = _notion_conn(tmp_path)
    dbrow = store.get_document(conn, "notion", "nf-db")
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
