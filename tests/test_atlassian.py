"""Atlassian: Jira issues/JQL and Confluence content/CQL — one router, one file.

One file per router, so a provider's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

from starlette.requests import Request
import re


from backlot import store
from tests._helpers import bare_request, crawl_confluence, crawl_jira, db_count, tiny_corpus


def test_admin_jira_crawls_all(client, admin_h, ro_conn):
    assert len(crawl_jira(client, admin_h)) == db_count(ro_conn, "jira")


def test_admin_confluence_crawls_all(client, admin_h, ro_conn):
    assert len(crawl_confluence(client, admin_h)) == db_count(ro_conn, "confluence")


def test_atlassian_401_keeps_the_atlassian_error_envelope(client):
    """Atlassian clients parse the error body as Atlassian Cloud's envelope (Confluence's
    raise_for_status reads ``response.json()["message"]``), so a 401 there is not FastAPI's
    ``{"detail": ...}`` — see backlot.main._atlassian_error_body."""
    # NOT serverInfo: the jira PyPI client probes that on connect, so it answers unauthenticated
    # on purpose. project/search is the first call that actually needs a credential.
    r = client.get("/atlassian/rest/api/3/project/search")
    assert r.status_code == 401
    body = r.json()
    assert body["message"] == "Unauthorized"
    assert body["errorMessages"] == ["Unauthorized"]
    assert body["statusCode"] == 401


def test_jira_serverinfo_v2_alias_matches_v3(client, admin_h):
    # the `jira` PyPI client (used by llama-index's JiraReader) probes serverInfo under
    # /rest/api/2 on connect; the mock must serve the same shape as the v3 handler.
    v2 = client.get("/atlassian/rest/api/2/serverInfo", headers=admin_h).json()
    v3 = client.get("/atlassian/rest/api/3/serverInfo", headers=admin_h).json()
    assert v2 == v3
    assert v2["deploymentType"] == "Cloud"


def test_jira_search_filtered_by_project(client, admin_h):
    from backlot import synth

    # literal project name (a legitimate JQL project= token) narrows to that project's issues
    by_name = client.get(
        "/atlassian/rest/api/3/search/jql", headers=admin_h, params={"jql": "project = payments"}
    ).json()
    titles = {i["fields"]["summary"] for i in by_name["issues"]}
    assert titles == {
        "SEV2: checkout latency spike",
        "Write postmortem for the SEV2",
        "Personal task: rotate my API keys",
    }

    # the synthesized (hash-suffixed) project key resolves to the same project
    synth_key = synth.jira_project_key("payments")
    by_key = client.get(
        "/atlassian/rest/api/3/search/jql",
        headers=admin_h,
        params={"jql": f"project = {synth_key}"},
    ).json()
    assert {i["fields"]["summary"] for i in by_key["issues"]} == titles

    # an unresolvable project is strict: zero results, not the unfiltered corpus
    bogus = client.get(
        "/atlassian/rest/api/3/search/jql", headers=admin_h, params={"jql": "project = BOGUS_NOPE"}
    ).json()
    assert bogus["issues"] == [] and bogus["isLast"] is True

    # no project clause at all -> unfiltered (same three issues here, since payments is the
    # only Jira project in the SAMPLE corpus -- the earlier assertions are what prove filtering,
    # not this equality)
    unfiltered = client.get("/atlassian/rest/api/3/search/jql", headers=admin_h).json()
    assert {i["fields"]["summary"] for i in unfiltered["issues"]} == titles


def test_confluence_content_filtered_by_space_key(client, admin_h):
    from backlot import synth

    # literal container name (the natural spaceKey value) narrows to that space only
    by_name = client.get(
        "/atlassian/wiki/rest/api/content", headers=admin_h, params={"spaceKey": "handbook"}
    ).json()
    titles = {r["title"] for r in by_name["results"]}
    assert titles == {"Engineering Handbook", "On-call Runbook"}
    assert "Compensation Bands 2026" not in titles

    # the synthesized (hash-suffixed) key resolves to the same space
    synth_key = synth.confluence_space_key("handbook")
    by_synth_key = client.get(
        "/atlassian/wiki/rest/api/content", headers=admin_h, params={"spaceKey": synth_key}
    ).json()
    assert {r["title"] for r in by_synth_key["results"]} == titles

    # an unresolvable spaceKey is strict: zero results, not the unfiltered corpus
    bogus = client.get(
        "/atlassian/wiki/rest/api/content", headers=admin_h, params={"spaceKey": "BOGUS_NOPE"}
    ).json()
    assert bogus["results"] == [] and bogus["size"] == 0

    # no spaceKey at all -> unfiltered (still includes the other space)
    unfiltered = client.get("/atlassian/wiki/rest/api/content", headers=admin_h).json()
    assert "Compensation Bands 2026" in {r["title"] for r in unfiltered["results"]}


def test_confluence_cql_search_filtered_by_space(client, admin_h):
    # "software" appears only in cf-handbook's body (SAMPLE), so this term narrows to one hit
    # when the space clause matches, and correctly to zero when it points elsewhere/unresolvable
    # (proving the space filter — not the text term — is what drives the 0, in the negative cases).
    narrowed = client.get(
        "/atlassian/wiki/rest/api/search",
        headers=admin_h,
        params={"cql": 'text~"software" and space=handbook'},
    ).json()
    assert {r["title"] for r in narrowed["results"]} == {"Engineering Handbook"}
    assert narrowed["totalSize"] == 1

    other_space = client.get(
        "/atlassian/wiki/rest/api/search",
        headers=admin_h,
        params={"cql": 'text~"software" and space=people-ops'},
    ).json()
    assert other_space["results"] == [] and other_space["totalSize"] == 0

    bogus = client.get(
        "/atlassian/wiki/rest/api/search",
        headers=admin_h,
        params={"cql": 'text~"software" and space=BOGUS_NOPE'},
    ).json()
    assert bogus["results"] == [] and bogus["totalSize"] == 0


def test_confluence_storage_roundtrip(client, admin_h, ro_conn):
    doc = ro_conn.execute("SELECT * FROM confluence_pages LIMIT 1").fetchone()
    from backlot import synth

    cid = synth.confluence_id(doc["doc_id"])
    page = client.get(
        f"/atlassian/wiki/rest/api/content/{cid}",
        headers=admin_h,
        params={"expand": "body.storage"},
    ).json()
    xhtml = page["body"]["storage"]["value"]
    # invert _storage: join paragraphs on \n\n, drop the wrapping tags, unescape
    from html import unescape

    text = xhtml.replace("</p><p>", "\n\n")
    text = re.sub(r"</?p>", "", text)
    assert unescape(text).strip() == doc["content"].strip()


def test_atlassian_errors_use_atlassian_envelope(client):
    # atlassian-python-api's Confluence client does response.json()["message"] on any error, so the
    # mock must shape /atlassian errors like Atlassian Cloud (message + statusCode), not {"detail"}.
    r = client.get("/atlassian/wiki/rest/api/content/999999")  # unauthenticated -> 401
    assert r.status_code == 401
    assert r.json().get("message") and r.json().get("statusCode") == 401
    r2 = client.get(
        "/atlassian/wiki/rest/api/content/search"
    )  # 'search' fails int path validation -> 422
    assert r2.status_code == 422 and "message" in r2.json()
    # non-atlassian paths keep FastAPI's default {"detail"} envelope
    r3 = client.get("/no-such-route")
    assert r3.status_code == 404 and "detail" in r3.json() and "message" not in r3.json()


def test_confluence_single_space_get(client, admin_h):
    spaces = client.get("/atlassian/wiki/rest/api/space", headers=admin_h).json()["results"]
    assert spaces
    key = spaces[0]["key"]
    r = client.get(f"/atlassian/wiki/rest/api/space/{key}", headers=admin_h)
    assert r.status_code == 200 and r.json()["key"] == key and r.json()["name"] == spaces[0]["name"]
    # unknown space -> clean atlassian-shaped 404
    r2 = client.get("/atlassian/wiki/rest/api/space/NOSUCH", headers=admin_h)
    assert r2.status_code == 404 and "message" in r2.json()


# --- OpenAPI enrichment: atlassian (jira + confluence) ------------------------------------


def test_atlassian_issue_has_typed_response_schema(client):
    op = client.get("/openapi.json").json()["paths"]["/atlassian/rest/api/3/issue/{key}"]["get"]
    assert op["responses"]["200"]["content"]["application/json"]["schema"] != {}


def test_atlassian_serverinfo_has_typed_response_schema(client):
    # serverInfo is a new alias (jira PyPI client probes it on connect); enrich it like its siblings.
    for ver in ("2", "3"):
        op = client.get("/openapi.json").json()["paths"][f"/atlassian/rest/api/{ver}/serverInfo"][
            "get"
        ]
        schema = op["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema != {}
        assert "$ref" in schema or schema.get("type") in ("object", "array")


def test_atlassian_responses_unchanged_by_enrichment(client, admin_h):
    search = client.get("/atlassian/rest/api/3/search/jql", headers=admin_h).json()
    assert "issues" in search and "isLast" in search and search["issues"]
    key = search["issues"][0]["key"]
    issue = client.get(f"/atlassian/rest/api/3/issue/{key}", headers=admin_h).json()
    for k in ("id", "key", "self", "fields"):
        assert k in issue, f"jira issue missing {k} (fidelity regression)"
    assert "summary" in issue["fields"] and "status" in issue["fields"]
    cl = client.get(
        "/atlassian/wiki/rest/api/content", params={"expand": "body.storage"}, headers=admin_h
    ).json()
    assert "results" in cl and cl["results"]
    cid = cl["results"][0]["id"]
    page = client.get(
        f"/atlassian/wiki/rest/api/content/{cid}",
        params={"expand": "body.storage"},
        headers=admin_h,
    ).json()
    assert "body" in page and "storage" in page["body"]  # expand survives


# --- Jira ------------------------------------------------------------------------


def test_jira_status_category_and_fields(tmp_path):
    from backlot.routers.atlassian import _jira_issue

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "jira",
                "doc_id": "j1",
                "project": "pay",
                "title": "T",
                "content": "c",
                "status": "In Progress",
                "assignee": "a@x.com",
                "reporter": "b@x.com",
                "resolution": "Done",
                "resolutiondate": "2026-03-01T00:00:00Z",
                "duedate": "2026-04-01",
                "fix_versions": ["1.2.0"],
            },
            {
                "source_type": "jira",
                "doc_id": "j2",
                "project": "pay",
                "title": "D",
                "content": "c",
                "status": "Done",
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    f = _jira_issue(conn, bare_request(), store.get_document(conn, "jira", "j1"))["fields"]
    # the real 3-category model: "In Progress" -> indeterminate (not the old hardcoded "new")
    assert f["status"]["statusCategory"]["key"] == "indeterminate"
    assert f["assignee"]["emailAddress"] == "a@x.com"
    assert f["reporter"]["emailAddress"] == "b@x.com"
    assert f["resolution"]["name"] == "Done" and f["resolutiondate"].startswith("2026-03-01")
    assert f["duedate"] == "2026-04-01" and f["fixVersions"][0]["name"] == "1.2.0"
    # richer actor object
    assert "avatarUrls" in f["assignee"] and f["assignee"]["accountType"] == "atlassian"
    # scaffolds present so probing clients get [] / null, not KeyError
    assert f["attachment"] == [] and f["votes"]["votes"] == 0

    done = _jira_issue(conn, bare_request(), store.get_document(conn, "jira", "j2"))["fields"]
    assert done["status"]["statusCategory"]["key"] == "done"
    assert done["assignee"] is None  # unassigned by default


# --- Confluence ------------------------------------------------------------------


def test_confluence_body_and_version(tmp_path):
    from backlot.routers.atlassian import _confluence_page

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "doc_id": "c1",
                "space": "hb",
                "title": "P",
                "content": "para one\n\npara two",
                "author_email": "a@x.com",
                "created": "2026-01-01T00:00:00Z",
                "updated": "2026-02-01T00:00:00Z",
                "version_message": "edited",
                "minor_edit": True,
                "labels": ["eng"],
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    row = store.get_document(conn, "confluence", "c1")
    page = _confluence_page(
        conn,
        bare_request(),
        row,
        "body.storage,body.view,body.export_view,version,metadata.labels,history",
    )
    # storage (XHTML source) and view (rendered) must differ
    assert page["body"]["storage"]["value"] != page["body"]["view"]["value"]
    # export_view (rendered, used by llama-index's ConfluenceReader) carries the same content
    # as view but without editor-only attributes (e.g. no `auto-cursor-target` class)
    assert page["body"]["export_view"]["representation"] == "export_view"
    assert "para one" in page["body"]["export_view"]["value"]
    assert "auto-cursor-target" not in page["body"]["export_view"]["value"]
    # version reflects the update + BYO message/minorEdit; history carries creation
    assert page["version"]["number"] == 2 and page["version"]["message"] == "edited"
    assert page["version"]["minorEdit"] is True
    assert page["history"]["createdDate"].startswith("2026-01-01")
    # labels reachable via expand=metadata.labels on the content object
    assert page["metadata"]["labels"]["results"][0]["name"] == "eng"


def test_confluence_restrictions_has_update(tmp_path):
    # restrictions/byOperation must return BOTH read and update operations
    import asyncio
    import types
    from backlot import synth
    from backlot.acl import Acl
    from backlot.routers.atlassian import confluence_restrictions

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "doc_id": "c2",
                "space": "hb",
                "title": "P",
                "content": "x",
                "author_email": "a@x.com",
                "visibility": "private",
            },
        ],
    )
    cid = synth.confluence_id("c2")
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            conn=store.connect_ro(s.db_path),
            acl=Acl.load(s.tokens_path, s.admin_token, s.org_name),
            index={"confluence": {cid: "c2"}},
        )
    )
    scope = {
        "type": "http",
        "scheme": "http",
        "server": ("m", 80),
        "path": "/",
        "query_string": b"",
        "app": app,
        "headers": [(b"authorization", f"Bearer {s.admin_token}".encode())],
    }
    result = asyncio.run(confluence_restrictions(cid, Request(scope)))
    assert "read" in result and "update" in result
    assert result["read"]["restrictions"]["user"]["results"]  # the private doc's author
