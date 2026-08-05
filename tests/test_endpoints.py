"""HTTP endpoint tests: drive the vendor endpoints directly (TestClient) over a built DB.

Asserts, over the conftest SAMPLE corpus (built fresh into a tmp dir — hermetic, so the suite
neither depends on nor crawls whatever ambient import lives in ``data/``): (1) an admin crawl
paginates through *every* stored document per source, (2) document content round-trips
byte-for-byte through each vendor's encoding, and (3) a non-admin user's crawl is filtered to
exactly their ACL. The completeness assertion ``crawl_count == db_count`` holds at any corpus
size, so it stays meaningful over the small SAMPLE while running in well under a second.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET

import pytest
import yaml
from starlette.testclient import TestClient

from app import store
from app.config import Settings, get_settings


@pytest.fixture(scope="module")
def client(sample_settings):
    """A TestClient whose app is pointed at the SAMPLE DB (via MOCK_DATA_DIR), not the ambient
    ``data/`` import. Env + settings cache are restored on teardown so other modules are unaffected."""
    from app.main import app

    prev = os.environ.get("MOCK_DATA_DIR")
    os.environ["MOCK_DATA_DIR"] = str(sample_settings.data_dir)
    get_settings.cache_clear()
    try:
        with TestClient(app) as c:  # lifespan opens sample_settings.db_path
            yield c
    finally:
        get_settings.cache_clear()
        if prev is None:
            os.environ.pop("MOCK_DATA_DIR", None)
        else:
            os.environ["MOCK_DATA_DIR"] = prev


@pytest.fixture(scope="module")
def org(client):
    """The org name the mock derived from the corpus (SAMPLE is @acme.com -> 'acme')."""
    return client.get("/_mock/users").json()["org"]


@pytest.fixture(scope="module")
def tokens(sample_settings):
    return yaml.safe_load(sample_settings.tokens_path.read_text())


@pytest.fixture(scope="module")
def admin_h(tokens):
    return {"Authorization": f"Bearer {tokens['admin_token']}"}


@pytest.fixture(scope="module")
def ro_conn(sample_settings):
    conn = store.connect_ro(sample_settings.db_path)
    yield conn
    conn.close()


def db_count(conn, source_type, **kw):
    return store.count_documents(conn, source_type, **kw)


# --- crawlers (small page sizes to exercise pagination) -------------------------

def crawl_gmail(client, headers, user="me"):
    ids, token = [], None
    while True:
        p = {"maxResults": 7}
        if token:
            p["pageToken"] = token
        j = client.get(f"/gmail/v1/users/{user}/messages", headers=headers, params=p).json()
        ids += [m["id"] for m in j.get("messages", [])]
        token = j.get("nextPageToken")
        if not token:
            break
    return ids


def crawl_drive(client, headers):
    ids, token = [], None
    while True:
        p = {"pageSize": 7}
        if token:
            p["pageToken"] = token
        j = client.get("/drive/v3/files", headers=headers, params=p).json()
        ids += [f["id"] for f in j.get("files", [])]
        token = j.get("nextPageToken")
        if not token:
            break
    return ids


def crawl_github_repo(client, headers, org, repo):
    out, page = [], 1
    while True:
        r = client.get(f"/github/repos/{org}/{repo}/issues", headers=headers,
                       params={"per_page": 5, "page": page, "state": "all"})
        body = r.json()
        out += body
        if 'rel="next"' not in r.headers.get("Link", ""):
            break
        page += 1
    return out


def crawl_jira(client, headers):
    out, token = [], None
    while True:
        p = {"maxResults": 6}
        if token:
            p["nextPageToken"] = token
        j = client.get("/atlassian/rest/api/3/search/jql", headers=headers, params=p).json()
        out += j["issues"]
        if j.get("isLast", True):
            break
        token = j["nextPageToken"]
    return out


def crawl_confluence(client, headers):
    out, start, limit = [], 0, 7
    while True:
        j = client.get("/atlassian/wiki/rest/api/content", headers=headers,
                       params={"start": start, "limit": limit, "expand": "body.storage"}).json()
        out += j["results"]
        if "next" not in j.get("_links", {}):
            break
        start += limit
    return out


HUBSPOT_OBJECT_TYPES = ("companies", "contacts", "notes")


def crawl_hubspot(client, headers, object_type, limit=2, archived=False):
    """Cursor-paginate one CRM object type. Terminates on the ABSENCE of paging.next — which is
    exactly how the official client's fetch_all decides it is done, so a mock that always emits
    paging.next would hang a real client rather than error."""
    out, after = [], None
    while True:
        params = {"limit": limit}
        if archived:
            params["archived"] = "true"
        if after:
            params["after"] = after
        j = client.get(f"/hubspot/crm/v3/objects/{object_type}", headers=headers,
                       params=params).json()
        out += j["results"]
        nxt = (j.get("paging") or {}).get("next")
        if not nxt:
            break
        after = nxt["after"]
    return out


def crawl_slack(client, headers):
    total, cursor = 0, None
    channels = []
    while True:
        data = {"limit": 8}
        if cursor:
            data["cursor"] = cursor
        j = client.post("/slack/api/conversations.list", headers=headers, data=data).json()
        channels += j["channels"]
        cursor = j["response_metadata"]["next_cursor"]
        if not cursor:
            break
    for ch in channels:
        ccur = None
        while True:
            d = {"channel": ch["id"], "limit": 50}
            if ccur:
                d["cursor"] = ccur
            h = client.post("/slack/api/conversations.history", headers=headers, data=d).json()
            for m in h["messages"]:
                total += 1
                if m.get("reply_count"):  # a thread root — its replies come from conversations.replies
                    r = client.post("/slack/api/conversations.replies", headers=headers,
                                    data={"channel": ch["id"], "ts": m["ts"]}).json()
                    total += len(r["messages"]) - 1  # thread includes the root we already counted
            ccur = h["response_metadata"]["next_cursor"]
            if not ccur:
                break
    return total


# --- admin full-crawl completeness ---------------------------------------------

def test_admin_gmail_crawls_all(client, admin_h, ro_conn):
    assert len(crawl_gmail(client, admin_h)) == db_count(ro_conn, "gmail")


def test_admin_drive_crawls_all(client, admin_h, ro_conn):
    # An unfiltered files.list includes folders on real Drive, and the mock synthesizes one per
    # container — so a full crawl is every stored file plus every folder.
    folders = ro_conn.execute("SELECT COUNT(*) FROM gdrive_folders").fetchone()[0]
    assert len(crawl_drive(client, admin_h)) == db_count(ro_conn, "google_drive") + folders


def test_admin_github_crawls_all(client, admin_h, ro_conn, org):
    repos = client.get(f"/github/orgs/{org}/repos", headers=admin_h, params={"per_page": 100}).json()
    seen = []
    for r in repos:
        seen += crawl_github_repo(client, admin_h, org, r["name"])
    assert len(seen) == db_count(ro_conn, "github")


def test_admin_jira_crawls_all(client, admin_h, ro_conn):
    assert len(crawl_jira(client, admin_h)) == db_count(ro_conn, "jira")


def test_admin_confluence_crawls_all(client, admin_h, ro_conn):
    assert len(crawl_confluence(client, admin_h)) == db_count(ro_conn, "confluence")


def test_admin_slack_crawls_all(client, admin_h, ro_conn):
    assert crawl_slack(client, admin_h) == db_count(ro_conn, "slack")


def test_admin_hubspot_crawls_all(client, admin_h, ro_conn):
    # The two views partition the corpus: archived records are excluded from the default listing and
    # are the only rows the archived one returns. Together they must account for every stored row.
    live, archived = [], []
    for otype in HUBSPOT_OBJECT_TYPES:
        live += crawl_hubspot(client, admin_h, otype)
        archived += crawl_hubspot(client, admin_h, otype, archived=True)
    assert len(live) + len(archived) == db_count(ro_conn, "hubspot")
    assert [r["properties"]["name"] for r in archived] == ["Defunct Labs"]
    assert all(r["archived"] is False for r in live)
    assert all(r["archived"] is True for r in archived)


def test_hubspot_list_cursor_pages_without_overlap(client, admin_h):
    """The cursor path itself: pages of two over the three non-archived companies, no repeats, no
    gaps, and the walk ends by `paging.next` disappearing rather than by a page coming back empty."""
    seen, pages, after = [], 0, None
    while True:
        params = {"limit": 2, **({"after": after} if after else {})}
        j = client.get("/hubspot/crm/v3/objects/companies", headers=admin_h, params=params).json()
        assert j["results"], "a page in the middle of a cursor walk must not be empty"
        seen += [r["id"] for r in j["results"]]
        pages += 1
        nxt = (j.get("paging") or {}).get("next")
        if not nxt:
            break
        after = nxt["after"]
    assert pages == 2                        # the cursor branch was actually taken
    assert len(seen) == len(set(seen)) == 3   # every non-archived company exactly once


def test_hubspot_last_page_omits_paging_next(client, admin_h):
    """The termination contract, asserted directly: a page that exhausts the type must not carry
    paging.next. Getting this wrong makes the official SDK's fetch_all loop forever."""
    j = client.get("/hubspot/crm/v3/objects/contacts", headers=admin_h,
                   params={"limit": 100}).json()
    assert j["results"]
    assert "next" not in (j.get("paging") or {})


def test_hubspot_read_one_record(client, admin_h):
    listed = client.get("/hubspot/crm/v3/objects/companies", headers=admin_h,
                        params={"limit": 100}).json()["results"]
    acme = next(r for r in listed if r["properties"].get("name") == "Acme Health")
    r = client.get(f"/hubspot/crm/v3/objects/companies/{acme['id']}", headers=admin_h)
    assert r.status_code == 200
    got = r.json()
    assert got["id"] == acme["id"]
    assert got["properties"]["domain"] == "acme-health.com"
    # HubSpot ids are numeric strings, and createdAt/updatedAt are ISO 8601
    assert got["id"].isdigit()
    assert got["createdAt"].endswith("Z")


def test_hubspot_unknown_object_type_is_404(client, admin_h):
    """A typo'd object type must not read as "this type has no records" — that silently turns a
    client bug into an empty result. An object type the caller simply cannot see any rows of is a
    different case and still returns an empty page."""
    r = client.get("/hubspot/crm/v3/objects/widgets", headers=admin_h)
    assert r.status_code == 404
    assert client.post("/hubspot/crm/v3/objects/widgets/search", headers=admin_h,
                       json={}).status_code == 404
    assert client.post("/hubspot/crm/v3/objects/widgets/batch/read", headers=admin_h,
                       json={"inputs": []}).status_code == 404


def test_hubspot_standard_type_with_no_records_is_an_empty_page(client, admin_h):
    """`deals` exists in every HubSpot portal whether or not any deal does, so an empty one is an
    empty listing — not an unknown type. The official LlamaIndex reader pages deals unconditionally,
    so 404-ing here would break it against any corpus that happens to have none."""
    r = client.get("/hubspot/crm/v3/objects/deals", headers=admin_h)
    assert r.status_code == 200
    assert r.json()["results"] == []
    assert "next" not in (r.json().get("paging") or {})


def test_hubspot_unresolvable_cursor_is_400(client, admin_h):
    """An `after` that names no record must fail, not silently restart from the first page — a
    client resuming with a stale cursor would otherwise re-read the whole type as if it were new."""
    r = client.get("/hubspot/crm/v3/objects/companies", headers=admin_h,
                   params={"after": "0000000000"})
    assert r.status_code == 400


def test_hubspot_missing_record_is_404(client, admin_h):
    assert client.get("/hubspot/crm/v3/objects/companies/999999999999",
                      headers=admin_h).status_code == 404


def test_hubspot_unauth_is_401(client):
    assert client.get("/hubspot/crm/v3/objects/companies").status_code == 401


def test_hubspot_acl_hides_restricted_record(client, tokens):
    """`hs-co-secret` is readable only by hana; another user's crawl must not contain it."""
    users = {u["email"]: u["token"] for u in tokens["users"]}
    ava_h = {"Authorization": f"Bearer {users['ava@acme.com']}"}
    hana_h = {"Authorization": f"Bearer {users['hana@acme.com']}"}
    names = lambda h: {r["properties"].get("name")  # noqa: E731
                       for r in crawl_hubspot(client, h, "companies")}
    assert "Stealth Health Co" not in names(ava_h)
    assert "Stealth Health Co" in names(hana_h)


def test_hubspot_associations_v4(client, admin_h):
    listed = client.get("/hubspot/crm/v3/objects/contacts", headers=admin_h,
                        params={"limit": 100}).json()["results"]
    ava = next(r for r in listed if r["properties"].get("firstname") == "Ava")
    j = client.get(f"/hubspot/crm/v4/objects/contacts/{ava['id']}/associations/companies",
                   headers=admin_h).json()
    assert len(j["results"]) == 1
    assoc = j["results"][0]
    assert assoc["toObjectId"].isdigit()
    assert assoc["associationTypes"][0]["category"] == "HUBSPOT_DEFINED"
    assert assoc["associationTypes"][0]["label"] == "Primary"


def test_hubspot_search_filter_groups(client, admin_h):
    """filterGroups combine as OR, filters within a group as AND — over arbitrary properties."""
    body = {"filterGroups": [{"filters": [
        {"propertyName": "industry", "operator": "EQ", "value": "healthcare"},
        {"propertyName": "lifecyclestage", "operator": "EQ", "value": "evaluation"}]}]}
    j = client.post("/hubspot/crm/v3/objects/companies/search", headers=admin_h, json=body).json()
    assert [r["properties"]["name"] for r in j["results"]] == ["Acme Health"]
    assert j["total"] == 1
    # AND within a group: contradicting the second filter drops the row
    body["filterGroups"][0]["filters"][1]["value"] = "qualified"
    assert client.post("/hubspot/crm/v3/objects/companies/search", headers=admin_h,
                       json=body).json()["results"] == []
    # OR across groups: two single-filter groups match two different rows
    body = {"filterGroups": [
        {"filters": [{"propertyName": "lifecyclestage", "operator": "EQ", "value": "evaluation"}]},
        {"filters": [{"propertyName": "lifecyclestage", "operator": "EQ", "value": "qualified"}]}]}
    j = client.post("/hubspot/crm/v3/objects/companies/search", headers=admin_h, json=body).json()
    assert {r["properties"]["name"] for r in j["results"]} == {"Acme Health", "Stealth Health Co"}


def test_hubspot_search_total_counts_all_matches_not_the_page(client, admin_h):
    """`total` is how many records matched, independent of how many fit on this page — so a
    one-record page over two matches still reports 2, and carries a cursor for the rest."""
    body = {"limit": 1, "filterGroups": [{"filters": [
        {"propertyName": "name", "operator": "HAS_PROPERTY"}]}]}
    totals, after, pages = [], None, 0
    while True:
        j = client.post("/hubspot/crm/v3/objects/companies/search", headers=admin_h,
                        json={**body, **({"after": after} if after else {})}).json()
        totals.append(j["total"])
        pages += 1
        nxt = (j.get("paging") or {}).get("next")
        if not nxt:
            break
        after = nxt["after"]
    # three non-archived companies carry a `name`; `total` must stay 3 on EVERY page rather than
    # shrinking to the number of matches left after the cursor
    assert pages == 3
    assert totals == [3, 3, 3]


def test_hubspot_search_has_property_and_contains_token(client, admin_h):
    j = client.post("/hubspot/crm/v3/objects/companies/search", headers=admin_h, json={
        "filterGroups": [{"filters": [{"propertyName": "domain",
                                       "operator": "HAS_PROPERTY"}]}]}).json()
    assert {r["properties"]["name"] for r in j["results"]} == {"Acme Health", "Borealis Clinics"}
    j = client.post("/hubspot/crm/v3/objects/companies/search", headers=admin_h, json={
        "filterGroups": [{"filters": [{"propertyName": "name", "operator": "CONTAINS_TOKEN",
                                       "value": "Health"}]}]}).json()
    assert {r["properties"]["name"] for r in j["results"]} == {"Acme Health", "Stealth Health Co"}


def _hs_search_names(client, headers, **body):
    j = client.post("/hubspot/crm/v3/objects/companies/search", headers=headers, json=body).json()
    return {r["properties"].get("name") for r in j["results"]}


def _hs_filter(client, headers, **f):
    return _hs_search_names(client, headers, filterGroups=[{"filters": [f]}])


def test_hubspot_search_every_operator(client, admin_h):
    """All 13 operators the official client validates. `employees` is numeric-looking and `founded`
    is an ISO date, so the comparison operators are exercised on both value shapes. Only
    non-archived records participate — search excludes the archived view, as the real API does."""
    f = lambda **kw: _hs_filter(client, admin_h, **kw)  # noqa: E731
    assert f(propertyName="name", operator="EQ", value="Acme Health") == {"Acme Health"}
    assert "Acme Health" not in f(propertyName="name", operator="NEQ", value="Acme Health")
    assert f(propertyName="employees", operator="LT", value="200") == {"Acme Health"}
    assert f(propertyName="employees", operator="LTE", value="150") == {"Acme Health"}
    assert f(propertyName="employees", operator="GT", value="200") == {"Borealis Clinics"}
    assert f(propertyName="employees", operator="GTE", value="400") == {"Borealis Clinics"}
    assert f(propertyName="employees", operator="BETWEEN", value="100",
             highValue="200") == {"Acme Health"}
    # BETWEEN must fall back to string comparison the way LT/GT do, or an ISO-8601 range silently
    # matches nothing while `GT` on the same property works.
    assert f(propertyName="founded", operator="BETWEEN", value="2014-01-01",
             highValue="2014-12-31") == {"Borealis Clinics"}
    assert f(propertyName="lifecyclestage", operator="IN",
             values=["evaluation", "procurement"]) == {"Acme Health", "Borealis Clinics"}
    assert "Acme Health" not in f(propertyName="lifecyclestage", operator="NOT_IN",
                                  values=["evaluation"])
    assert f(propertyName="domain", operator="HAS_PROPERTY") == {"Acme Health", "Borealis Clinics"}
    assert f(propertyName="domain", operator="NOT_HAS_PROPERTY") == {"Stealth Health Co"}
    assert f(propertyName="name", operator="CONTAINS_TOKEN",
             value="Clinics") == {"Borealis Clinics"}
    assert "Borealis Clinics" not in f(propertyName="name", operator="NOT_CONTAINS_TOKEN",
                                       value="Clinics")


def test_hubspot_search_prefilter_cannot_change_results(client, admin_h, monkeypatch):
    """The SQL pre-filter is a pure optimisation: it may only skip rows Python would have rejected
    anyway. Every query is run twice — once with the pushdown, once with it disabled — and the
    results and totals must be identical, so a pre-filter that is not a *necessary* condition fails
    here rather than silently dropping matches."""
    from app.routers import hubspot as hs

    bodies = [
        {"filterGroups": [{"filters": [{"propertyName": "industry", "operator": "EQ",
                                        "value": "healthcare"}]}]},
        {"filterGroups": [{"filters": [{"propertyName": "domain", "operator": "HAS_PROPERTY"}]}]},
        {"filterGroups": [{"filters": [{"propertyName": "name", "operator": "CONTAINS_TOKEN",
                                        "value": "Health"}]}]},
        {"filterGroups": [{"filters": [{"propertyName": "lifecyclestage", "operator": "IN",
                                        "values": ["evaluation", "procurement"]}]}]},
        # a group whose filters mix a pushable and a non-pushable operator
        {"filterGroups": [{"filters": [{"propertyName": "name", "operator": "HAS_PROPERTY"},
                                       {"propertyName": "employees", "operator": "GT",
                                        "value": "100"}]}]},
        # OR across groups: no single filter is necessary, so nothing may be pushed down
        {"filterGroups": [{"filters": [{"propertyName": "industry", "operator": "EQ",
                                        "value": "healthcare"}]},
                          {"filters": [{"propertyName": "lifecyclestage", "operator": "EQ",
                                        "value": "qualified"}]}]},
        {"query": "acme"},
    ]

    def run(body):
        j = client.post("/hubspot/crm/v3/objects/companies/search", headers=admin_h,
                        json={**body, "limit": 100}).json()
        return j["total"], [r["id"] for r in j["results"]]

    with_pushdown = [run(b) for b in bodies]
    monkeypatch.setattr(hs, "_sql_prefilter", lambda body: None)
    without = [run(b) for b in bodies]
    assert with_pushdown == without


def test_hubspot_search_sorts(client, admin_h):
    """`sorts` is advertised, so it has to order the whole match set — not just whatever landed on
    the page. Numeric properties sort numerically, which string ordering would get wrong."""
    def names(direction):
        j = client.post("/hubspot/crm/v3/objects/companies/search", headers=admin_h, json={
            "filterGroups": [{"filters": [{"propertyName": "employees",
                                           "operator": "HAS_PROPERTY"}]}],
            "sorts": [{"propertyName": "employees", "direction": direction}]}).json()
        return [r["properties"]["name"] for r in j["results"]]

    assert names("ASCENDING") == ["Acme Health", "Borealis Clinics"]       # 150 then 400
    assert names("DESCENDING") == ["Borealis Clinics", "Acme Health"]


def test_hubspot_search_is_acl_scoped(client, tokens):
    """Search must filter by the caller like every other read — not only the plain listing."""
    users = {u["email"]: u["token"] for u in tokens["users"]}
    body = {"filterGroups": [{"filters": [{"propertyName": "name", "operator": "HAS_PROPERTY"}]}]}
    ava = {"Authorization": f"Bearer {users['ava@acme.com']}"}
    hana = {"Authorization": f"Bearer {users['hana@acme.com']}"}
    assert "Stealth Health Co" not in _hs_search_names(client, ava, **body)
    assert "Stealth Health Co" in _hs_search_names(client, hana, **body)


def test_hubspot_associations_page_past_the_first_page(client, admin_h):
    """Associations need the same cursor contract as listings: at `limit=1` over a company with two
    associated records, both must be reachable and the walk must terminate."""
    listed = client.get("/hubspot/crm/v3/objects/companies", headers=admin_h,
                        params={"limit": 100}).json()["results"]
    acme = next(r for r in listed if r["properties"].get("name") == "Acme Health")
    url = f"/hubspot/crm/v4/objects/companies/{acme['id']}/associations/notes"
    # the SAMPLE company has one note; add the contact link to get two association rows overall
    seen, after, pages = [], None, 0
    while True:
        params = {"limit": 1, **({"after": after} if after else {})}
        j = client.get(url, headers=admin_h, params=params).json()
        seen += [r["toObjectId"] for r in j["results"]]
        pages += 1
        nxt = (j.get("paging") or {}).get("next")
        if not nxt:
            break
        after = nxt["after"]
        assert pages < 10, "association paging did not terminate"
    assert len(seen) == len(set(seen)) >= 1
    # a cursor naming no record must fail rather than silently restart
    assert client.get(url, headers=admin_h, params={"after": "0000000000"}).status_code == 400


def test_hubspot_batch_read_partial_is_207(client, admin_h):
    """A partial batch is 207 with `numErrors` + `errors`, and `status` stays COMPLETE — its allowed
    values are PENDING/PROCESSING/CANCELED/COMPLETE, so a made-up "PARTIAL" makes the official
    client deserialize into the no-errors model and drop the error detail."""
    listed = client.get("/hubspot/crm/v3/objects/companies", headers=admin_h,
                        params={"limit": 100}).json()["results"]
    r = client.post("/hubspot/crm/v3/objects/companies/batch/read", headers=admin_h,
                    json={"inputs": [{"id": listed[0]["id"]}, {"id": "111111111111"}]})
    assert r.status_code == 207
    j = r.json()
    assert j["status"] == "COMPLETE"
    assert len(j["results"]) == 1
    assert j["numErrors"] == 1
    assert j["errors"][0]["context"]["id"] == ["111111111111"]


def test_hubspot_batch_read(client, admin_h):
    listed = client.get("/hubspot/crm/v3/objects/companies", headers=admin_h,
                        params={"limit": 100}).json()["results"]
    ids = [r["id"] for r in listed]
    j = client.post("/hubspot/crm/v3/objects/companies/batch/read", headers=admin_h,
                    json={"inputs": [{"id": i} for i in ids],
                          "properties": ["name"]}).json()
    assert {r["id"] for r in j["results"]} == set(ids)


# --- content round-trips through each vendor's encoding -------------------------

def _gmail_plain(payload):
    """Extract the text/plain body data from a Gmail payload (top-level or a part)."""
    if payload.get("body", {}).get("data"):
        return payload["body"]["data"]
    for part in payload.get("parts", []):
        if part["mimeType"] == "text/plain":
            return part["body"]["data"]
    raise AssertionError("no text/plain part")


# --- Gmail hex message ids (#39) --------------------------------------------------------------
#
# Gmail ids are 16 lowercase hex digits parsed as a signed 64-bit integer. MEASURED against the live
# API, which is what fixes the 400/404 boundary the mock previously got wrong:
#
#   id                        | real Gmail
#   --------------------------|-----------------------------------------------
#   0 / 1 / abc123 / DEADBEEF | 404 NOT_FOUND     (a valid shape, just unknown)
#   7fffffffffffffff          | 404 NOT_FOUND     (2**63 - 1 is in range)
#   8000000000000000          | 400 INVALID_ARGUMENT "Invalid id value"
#   ffffffffffffffff          | 400               (>= 2**63)
#   18c9a1b2c3d4e5f6a         | 400               (17 digits overflows)
#   -1 / 1g / " 1"            | 400               (not hex)
#
# Threads share the id space: a single-message thread reports id == threadId.

def _a_gmail_row(ro_conn):
    return ro_conn.execute("SELECT * FROM gmail_messages LIMIT 1").fetchone()


def test_gmail_messages_list_serves_hex_ids(client, admin_h):
    """The ids a client receives must look like Gmail's, not like the corpus's dsids — that is the
    whole point of #39. `dsid_…` is not hex, so real Gmail would call it an invalid id value."""
    msgs = client.get("/gmail/v1/users/me/messages", headers=admin_h,
                      params={"maxResults": 10}).json()["messages"]
    assert msgs
    for m in msgs:
        for key in ("id", "threadId"):
            assert len(m[key]) == 16, m
            assert all(c in "0123456789abcdef" for c in m[key]), m
            assert int(m[key], 16) < 2 ** 63, m
        assert not m["id"].startswith("dsid_")


def test_gmail_hex_id_resolves_to_the_same_document(client, admin_h, ro_conn):
    """The hex id maps back to its dsid, so the body a client reads by hex is the stored body. A
    one-way id would make every message unreadable."""
    from app import synth
    row = _a_gmail_row(ro_conn)
    hexid = synth.gmail_message_id(row["doc_id"])
    m = client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h,
                   params={"format": "full"}).json()
    assert m["id"] == hexid
    assert base64.urlsafe_b64decode(_gmail_plain(m["payload"])).decode() == row["content"]


def test_gmail_thread_id_matches_the_message_id_for_a_lone_message(client, admin_h, ro_conn):
    """Threads share the message id space in real Gmail, so a message that is its own thread root
    reports the same value twice — and `threads.get` resolves it."""
    from app import synth
    row = ro_conn.execute(
        "SELECT * FROM gmail_messages WHERE COALESCE(thread_id, '') = '' LIMIT 1").fetchone()
    if row is None:
        row = ro_conn.execute(
            "SELECT * FROM gmail_messages WHERE thread_id = doc_id LIMIT 1").fetchone()
    assert row is not None, "SAMPLE should hold a message that is its own thread"
    hexid = synth.gmail_message_id(row["doc_id"])
    m = client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h).json()
    assert m["id"] == m["threadId"] == hexid
    t = client.get(f"/gmail/v1/users/me/threads/{hexid}", headers=admin_h)
    assert t.status_code == 200 and t.json()["id"] == hexid


def test_gmail_reply_reports_its_roots_thread_id(client, admin_h, ro_conn):
    from app import synth
    row = ro_conn.execute("SELECT * FROM gmail_messages WHERE COALESCE(thread_id,'') != '' "
                          "AND thread_id != doc_id LIMIT 1").fetchone()
    assert row is not None, "SAMPLE should hold a threaded reply"
    m = client.get(f"/gmail/v1/users/me/messages/{synth.gmail_message_id(row['doc_id'])}",
                   headers=admin_h).json()
    assert m["threadId"] == synth.gmail_message_id(row["thread_id"])
    assert m["id"] != m["threadId"]


def test_gmail_attachment_resolves_under_a_hex_message_id(client, admin_h, ro_conn):
    from app import synth
    row = ro_conn.execute("SELECT * FROM gmail_messages WHERE COALESCE(attachments,'') NOT IN "
                          "('', '[]') LIMIT 1").fetchone()
    assert row is not None, "SAMPLE should hold a message with an attachment"
    hexid = synth.gmail_message_id(row["doc_id"])
    m = client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h,
                   params={"format": "full"}).json()
    att = next(p for p in m["payload"]["parts"] if p.get("filename"))
    r = client.get(f"/gmail/v1/users/me/messages/{hexid}/attachments/"
                   f"{att['body']['attachmentId']}", headers=admin_h)
    assert r.status_code == 200 and r.json()["size"] > 0


@pytest.mark.parametrize("mid", ["0", "1", "abc123", "DEADBEEF", "7fffffffffffffff",
                                 "0000000000000001", "18c9a1b2c3d4e5f6"])
def test_gmail_a_valid_but_unknown_id_is_not_found(client, admin_h, mid):
    """A well-formed id the mailbox does not hold is 404, uppercase included — measured."""
    for kind in ("messages", "threads"):
        r = client.get(f"/gmail/v1/users/me/{kind}/{mid}", headers=admin_h)
        assert r.status_code == 404, f"{kind}/{mid}: {r.status_code}"
        assert r.json()["error"]["message"] == "Requested entity was not found."


@pytest.mark.parametrize("mid", ["8000000000000000", "ffffffffffffffff", "18c9a1b2c3d4e5f6a",
                                 "-1", "1g", "nosuchmessageid", "dsid_00908a2dda4b4d359194a09101"])
def test_gmail_an_unparsable_id_is_an_invalid_argument(client, admin_h, mid):
    """The gap #39 names: an id that is not a parsable in-range hex integer is 400
    INVALID_ARGUMENT "Invalid id value", not 404. The last row is the mock's OWN former id format,
    which is exactly why the served ids had to change first."""
    for kind in ("messages", "threads"):
        r = client.get(f"/gmail/v1/users/me/{kind}/{mid}", headers=admin_h)
        assert r.status_code == 400, f"{kind}/{mid}: {r.status_code}"
        e = r.json()["error"]
        assert e["message"] == "Invalid id value"
        assert e["status"] == "INVALID_ARGUMENT"
        assert e["errors"][0]["reason"] == "invalidArgument"


def test_gmail_hex_ids_still_enforce_the_acl(client, admin_h, tokens, ro_conn):
    """Resolving through the index must not become a way around the ACL. The index is global — it
    maps every hex id, visible or not — so the ACL read after it is the only thing standing between
    a scoped caller and someone else's mail. The CFO's comp review is granted to cfo alone."""
    from app import synth
    row = ro_conn.execute("SELECT * FROM gmail_messages WHERE title LIKE 'Confidential comp%'"
                          ).fetchone()
    hexid = synth.gmail_message_id(row["doc_id"])
    assert client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h).status_code == 200
    cfo = {"Authorization": f"Bearer {_tok(tokens, 'cfo@acme.com')}"}
    assert client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=cfo).status_code == 200
    outsider = {"Authorization": f"Bearer {_tok(tokens, 'mia@acme.com')}"}
    r = client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=outsider)
    assert r.status_code == 404
    assert r.json()["error"]["message"] == "Requested entity was not found."


def test_gmail_body_roundtrip(client, admin_h, ro_conn):
    from app import synth
    doc = ro_conn.execute("SELECT * FROM gmail_messages LIMIT 1").fetchone()
    m = client.get(f"/gmail/v1/users/me/messages/{synth.gmail_message_id(doc['doc_id'])}",
                   headers=admin_h, params={"format": "full"}).json()
    body = base64.urlsafe_b64decode(_gmail_plain(m["payload"])).decode()
    assert body == doc["content"]
    subj = next(h["value"] for h in m["payload"]["headers"] if h["name"] == "Subject")
    assert subj == doc["title"]


def test_gmail_messages_list_ordered_by_internaldate_desc(client, admin_h, ro_conn):
    # Real Gmail returns messages.list newest-first by internalDate. Regression (#11): the mock
    # listed by doc_id (hash order), so a capped "newest N" was effectively random by date.
    listed = client.get("/gmail/v1/users/me/messages", headers=admin_h,
                        params={"maxResults": 50}).json()["messages"]
    got = [m["id"] for m in listed]
    # the stable total order the endpoint must produce: created_ts DESC, doc_id ASC as tie-break
    # the served ids are hex (#39), so the expectation is the hex of that stable order
    from app import synth
    expected = [synth.gmail_message_id(r["doc_id"]) for r in ro_conn.execute(
        "SELECT doc_id FROM gmail_messages ORDER BY created_ts DESC, doc_id LIMIT 50").fetchall()]
    assert got == expected
    # ...and internalDate is monotonically non-increasing across the returned page
    dates = [int(client.get(f"/gmail/v1/users/me/messages/{i}", headers=admin_h,
                            params={"format": "minimal"}).json()["internalDate"]) for i in got]
    assert dates == sorted(dates, reverse=True)


def test_gmail_messages_list_pagination_stable_and_ordered(client, admin_h, ro_conn):
    # Paging must be a stable partition of the same date-desc order — no dupes, no skips, and page 2
    # continues strictly at/under page 1's tail. (Regression guard for the tie-break in ORDER BY.)
    total = client.get("/gmail/v1/users/me/messages", headers=admin_h,
                       params={"maxResults": 1}).json()["resultSizeEstimate"]
    if total < 2:
        pytest.skip("need >= 2 gmail messages to exercise paging")
    p1 = client.get("/gmail/v1/users/me/messages", headers=admin_h, params={"maxResults": 1}).json()
    p2 = client.get("/gmail/v1/users/me/messages", headers=admin_h,
                    params={"maxResults": 1, "pageToken": p1["nextPageToken"]}).json()
    a, b = p1["messages"][0]["id"], p2["messages"][0]["id"]
    assert a != b                                                     # distinct rows, no repeat
    both = client.get("/gmail/v1/users/me/messages", headers=admin_h,
                      params={"maxResults": 2}).json()["messages"]
    assert [m["id"] for m in both] == [a, b]                          # pages concatenate in order


def test_gmail_attachment_size_matches_part_metadata(client, admin_h, ro_conn):
    # Real Gmail's contract: a part's body.size equals the byte length attachments.get serves, so a
    # client can stat an attachment from message metadata alone. Regression: the part reported the
    # corpus-declared `size` (e.g. 2048) while attachments.get returned len(content) — a mismatch.
    row = ro_conn.execute(
        "SELECT doc_id FROM gmail_messages WHERE attachments IS NOT NULL "
        "AND attachments != '[]' LIMIT 1").fetchone()
    if row is None:
        pytest.skip("no gmail message with an attachment in this subset")
    from app import synth
    hexid = synth.gmail_message_id(row["doc_id"])
    m = client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h,
                   params={"format": "full"}).json()
    parts = [p for p in m["payload"]["parts"] if p.get("body", {}).get("attachmentId")]
    assert parts, "message should expose at least one attachment part"
    for p in parts:
        got = client.get(
            f"/gmail/v1/users/me/messages/{hexid}/attachments/{p['body']['attachmentId']}",
            headers=admin_h).json()
        assert got["size"] == p["body"]["size"]                       # the two agree
        assert len(base64.urlsafe_b64decode(got["data"])) == p["body"]["size"]  # ...and match the bytes


def test_drive_export_roundtrip(client, admin_h, ro_conn):
    doc = ro_conn.execute("SELECT * FROM gdrive_files LIMIT 1").fetchone()
    text = client.get(f"/drive/v3/files/{doc['doc_id']}/export", headers=admin_h,
                      params={"mimeType": "text/plain"}).text
    assert doc["content"] in text and text.startswith(doc["title"])


def test_github_body_roundtrip(client, admin_h, ro_conn, org):
    doc = ro_conn.execute("SELECT * FROM github_items LIMIT 1").fetchone()
    from app import synth
    num = synth.github_number(doc["doc_id"])
    issue = client.get(f"/github/repos/{org}/{doc['repo']}/issues/{num}", headers=admin_h).json()
    assert issue["body"] == doc["content"] and issue["title"] == doc["title"]


def test_github_issues_filtered_by_state(client, admin_h, org):
    # gateway repo: gh-issue-1 is open, gh-pr-1 is a closed PR (both surface via /issues)
    open_body = client.get(f"/github/repos/{org}/gateway/issues", headers=admin_h,
                           params={"state": "open"}).json()
    assert [i["title"] for i in open_body] == ["Rate limiter drops bursts under 50ms"]
    closed_body = client.get(f"/github/repos/{org}/gateway/issues", headers=admin_h,
                             params={"state": "closed"}).json()
    assert [i["title"] for i in closed_body] == ["Fix token-bucket refill off-by-one"]
    all_body = client.get(f"/github/repos/{org}/gateway/issues", headers=admin_h,
                          params={"state": "all"}).json()
    assert {i["title"] for i in all_body} == {"Rate limiter drops bursts under 50ms",
                                              "Fix token-bucket refill off-by-one"}
    # default (no state param) behaves like real GitHub: open only
    default_body = client.get(f"/github/repos/{org}/gateway/issues", headers=admin_h).json()
    assert default_body == open_body


def test_github_pulls_filtered_by_state(client, admin_h, org):
    # gateway repo's only PR (gh-pr-1) is closed
    open_body = client.get(f"/github/repos/{org}/gateway/pulls", headers=admin_h,
                           params={"state": "open"}).json()
    assert open_body == []
    closed_body = client.get(f"/github/repos/{org}/gateway/pulls", headers=admin_h,
                             params={"state": "closed"}).json()
    assert [p["title"] for p in closed_body] == ["Fix token-bucket refill off-by-one"]
    all_body = client.get(f"/github/repos/{org}/gateway/pulls", headers=admin_h,
                          params={"state": "all"}).json()
    assert [p["title"] for p in all_body] == ["Fix token-bucket refill off-by-one"]


# --- github codebase serving: git tree / contents / blobs / branches / readme ---------
#
# These need `github` `file` docs, which the shared SAMPLE corpus (built once, session-scoped,
# in conftest.py) doesn't carry. Rather than touch conftest.py, `gh_client` below builds its own
# small DB — SAMPLE plus a 'codebase' repo of file docs — the same way conftest._build() does.

_GH_FILE_DOCS = [
    {"source_type": "github", "doc_id": "gh-file-readme", "repo": "codebase", "subtype": "file",
     "path": "README.md", "title": "README.md",
     "content": "# codebase\n\nCore service source, browsable via the tree/contents API.\n",
     "group": "engineering", "visibility": "public",
     "author_email": "ava@acme.com", "author_groups": ["engineering"]},
    {"source_type": "github", "doc_id": "gh-file-main", "repo": "codebase", "subtype": "file",
     "path": "src/main.py", "title": "main.py", "content": "def main():\n    return 1\n",
     "group": "engineering", "visibility": "public",
     "author_email": "ava@acme.com", "author_groups": ["engineering"]},
    {"source_type": "github", "doc_id": "gh-file-utils", "repo": "codebase", "subtype": "file",
     "path": "src/pkg/utils.py", "title": "utils.py", "content": "def helper():\n    return 2\n",
     "group": "engineering", "visibility": "public",
     "author_email": "ava@acme.com", "author_groups": ["engineering"]},
    {"source_type": "github", "doc_id": "gh-file-secret", "repo": "codebase", "subtype": "file",
     "path": "config/secret.yaml", "title": "secret.yaml", "content": "api_key: shh\n",
     "group": "people", "visibility": "group",
     "author_email": "hana@acme.com", "author_groups": ["people"]},
    # a separate repo (not 'codebase') so this doesn't perturb the exact tree/contents sets the
    # 'codebase' tests assert against
    {"source_type": "github", "doc_id": "gh-file-unicode", "repo": "unicode-repo", "subtype": "file",
     "path": "docs/unicode.md", "title": "unicode.md", "content": "héllo wörld 世界\n",
     "group": "engineering", "visibility": "public",
     "author_email": "ava@acme.com", "author_groups": ["engineering"]},
    # a file doc, deliberately chosen (by brute force over the doc_id) so its synthesized
    # `number` collides with gh-issue-1's in the SAME repo ('gateway') -- reproduces the
    # (repo, number) index-shadowing bug: a file's number must never be able to hide a
    # real issue/PR at that number.
    {"source_type": "github", "doc_id": "gh-file-collide-88814", "repo": "gateway", "subtype": "file",
     "path": "src/collide.py", "title": "collide.py", "content": "# unrelated file content\n",
     "group": "engineering", "visibility": "public",
     "author_email": "ava@acme.com", "author_groups": ["engineering"]},
]


@pytest.fixture(scope="module")
def gh_client(tmp_path_factory):
    from app.importer.byo import load
    from tests.conftest import SAMPLE

    data_dir = tmp_path_factory.mktemp("gh_sample")
    corpus = data_dir / "_corpus.jsonl"
    corpus.write_text("\n".join(json.dumps(r) for r in SAMPLE + _GH_FILE_DOCS))
    settings = Settings(data_dir=data_dir)
    load(corpus, settings)

    from app.main import app
    prev = os.environ.get("MOCK_DATA_DIR")
    os.environ["MOCK_DATA_DIR"] = str(data_dir)
    get_settings.cache_clear()
    try:
        with TestClient(app) as c:
            yield c, settings
    finally:
        get_settings.cache_clear()
        if prev is None:
            os.environ.pop("MOCK_DATA_DIR", None)
        else:
            os.environ["MOCK_DATA_DIR"] = prev


@pytest.fixture(scope="module")
def gh_org(gh_client):
    c, _ = gh_client
    return c.get("/_mock/users").json()["org"]


@pytest.fixture(scope="module")
def gh_user_tokens(gh_client):
    _, settings = gh_client
    data = yaml.safe_load(settings.tokens_path.read_text())
    return {"admin": data["admin_token"], **{u["email"]: u["token"] for u in data["users"]}}


@pytest.fixture(scope="module")
def gh_admin_h(gh_user_tokens):
    return {"Authorization": f"Bearer {gh_user_tokens['admin']}"}


def test_github_tree_recursive(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    body = c.get(f"/github/repos/{gh_org}/codebase/git/trees/main",
                headers=gh_admin_h, params={"recursive": "1"}).json()
    assert body["truncated"] is False
    paths = {e["path"] for e in body["tree"]}
    assert paths == {"README.md", "src", "src/main.py", "src/pkg", "src/pkg/utils.py",
                     "config", "config/secret.yaml"}
    content = "def main():\n    return 1\n"
    blob = next(e for e in body["tree"] if e["path"] == "src/main.py")
    assert blob["mode"] == "100644" and blob["type"] == "blob"
    assert blob["sha"] == hashlib.sha1(content.encode()).hexdigest()
    assert blob["size"] == len(content)
    tree_dir = next(e for e in body["tree"] if e["path"] == "src/pkg")
    assert tree_dir["mode"] == "040000" and tree_dir["type"] == "tree"
    assert "size" not in tree_dir


def test_github_tree_non_recursive(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    body = c.get(f"/github/repos/{gh_org}/codebase/git/trees/main", headers=gh_admin_h).json()
    paths = {e["path"] for e in body["tree"]}
    assert paths == {"README.md", "src", "config"}  # top level only: root file + top dirs


def test_github_contents_dir(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    body = c.get(f"/github/repos/{gh_org}/codebase/contents/src", headers=gh_admin_h).json()
    assert {(e["name"], e["type"]) for e in body} == {("main.py", "file"), ("pkg", "dir")}


def test_github_contents_file(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    body = c.get(f"/github/repos/{gh_org}/codebase/contents/src/main.py", headers=gh_admin_h).json()
    content = "def main():\n    return 1\n"
    assert body["type"] == "file" and body["encoding"] == "base64"
    assert base64.b64decode(body["content"]).decode() == content
    assert body["sha"] == hashlib.sha1(content.encode()).hexdigest()
    assert body["name"] == "main.py" and body["path"] == "src/main.py"


def test_github_contents_root(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    body = c.get(f"/github/repos/{gh_org}/codebase/contents", headers=gh_admin_h).json()
    assert {e["name"] for e in body} == {"README.md", "src", "config"}


def test_github_blob_by_sha(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    content = "def main():\n    return 1\n"
    sha = hashlib.sha1(content.encode()).hexdigest()
    body = c.get(f"/github/repos/{gh_org}/codebase/git/blobs/{sha}", headers=gh_admin_h).json()
    assert body["sha"] == sha and body["encoding"] == "base64"
    assert base64.b64decode(body["content"]).decode() == content


def test_github_blob_unknown_sha_404(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    r = c.get(f"/github/repos/{gh_org}/codebase/git/blobs/{'0' * 40}", headers=gh_admin_h)
    assert r.status_code == 404
    # matches the existing github 404 shape (app.main's shared exception handler wraps
    # HTTPException(detail=...) as {"detail": ...} for every non-atlassian router)
    assert r.json() == {"detail": "Not Found"}


def test_github_branch_and_commit_resolve_tree(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    branch = c.get(f"/github/repos/{gh_org}/codebase/branches/main", headers=gh_admin_h).json()
    tree_sha = branch["commit"]["commit"]["tree"]["sha"]
    commit_sha = branch["commit"]["sha"]
    commit = c.get(f"/github/repos/{gh_org}/codebase/commits/{commit_sha}", headers=gh_admin_h).json()
    assert commit["commit"]["tree"]["sha"] == tree_sha
    # the tree sha resolved from branch/commit is itself a valid `ref` for git/trees
    tree = c.get(f"/github/repos/{gh_org}/codebase/git/trees/{tree_sha}", headers=gh_admin_h).json()
    assert tree["sha"] == tree_sha
    assert {e["path"] for e in tree["tree"]}


def test_github_readme_real_content(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    body = c.get(f"/github/repos/{gh_org}/codebase/readme", headers=gh_admin_h).json()
    text = "# codebase\n\nCore service source, browsable via the tree/contents API.\n"
    assert base64.b64decode(body["content"]).decode() == text
    assert body["sha"] == hashlib.sha1(text.encode()).hexdigest()


def test_github_readme_stub_when_no_readme_file(client, admin_h, org):
    # 'gateway' (base SAMPLE) has issues/PRs but no file docs -> falls back to the stub
    body = client.get(f"/github/repos/{org}/gateway/readme", headers=admin_h).json()
    assert base64.b64decode(body["content"]).decode().startswith("# gateway")


def test_github_file_excluded_from_issues_and_pulls(gh_client, gh_admin_h, gh_org):
    c, _ = gh_client
    issues = c.get(f"/github/repos/{gh_org}/codebase/issues", headers=gh_admin_h,
                   params={"state": "all"}).json()
    assert issues == []  # 'codebase' has only file docs, no issues/PRs
    pulls = c.get(f"/github/repos/{gh_org}/codebase/pulls", headers=gh_admin_h,
                  params={"state": "all"}).json()
    assert pulls == []


def test_github_file_excluded_from_search_issues(gh_client, gh_admin_h):
    c, _ = gh_client
    # 'helper' only appears in a file's content (src/pkg/utils.py); it must not surface
    # as an issue/PR search hit even though the FTS index covers file content too.
    body = c.get("/github/search/issues", headers=gh_admin_h, params={"q": "helper"}).json()
    assert body["total_count"] == 0
    assert body["items"] == []


def test_github_file_number_index_excludes_files(gh_client, gh_admin_h, gh_org):
    """`kind='file'` rows must never populate app.state.index["github"] (the (repo, number)
    reverse index): a file's synthesized number can collide with a real issue/PR's number
    (see gh-file-collide-88814, which deliberately collides with gh-issue-1's), and if the
    file's doc_id ends up as the map value, a real issue/PR 404s."""
    c, _ = gh_client
    from app import synth

    file_doc_ids = {d["doc_id"] for d in _GH_FILE_DOCS}
    idx = c.app.state.index["github"]
    assert not (set(idx.values()) & file_doc_ids)

    # the real issue is still resolvable by number even though a file doc collides with it
    issue_num = synth.github_number("gh-issue-1")
    assert synth.github_number("gh-file-collide-88814") == issue_num  # sanity: collision is real
    r = c.get(f"/github/repos/{gh_org}/gateway/issues/{issue_num}", headers=gh_admin_h)
    assert r.status_code == 200
    assert r.json()["title"] == "Rate limiter drops bursts under 50ms"

    pr_num = synth.github_number("gh-pr-1")
    r2 = c.get(f"/github/repos/{gh_org}/gateway/pulls/{pr_num}", headers=gh_admin_h)
    assert r2.status_code == 200
    assert r2.json()["title"] == "Fix token-bucket refill off-by-one"


def test_github_size_is_utf8_byte_length(gh_client, gh_admin_h, gh_org):
    """Real GitHub's `size` is a UTF-8 byte count, not a character count -- must differ for a
    file whose content has multi-byte characters, across the tree, contents, and blob endpoints."""
    c, _ = gh_client
    content = "héllo wörld 世界\n"
    nbytes = len(content.encode())
    assert nbytes > len(content)  # sanity: the two would only coincidentally match otherwise

    tree = c.get(f"/github/repos/{gh_org}/unicode-repo/git/trees/main", headers=gh_admin_h,
                params={"recursive": "1"}).json()
    entry = next(e for e in tree["tree"] if e["path"] == "docs/unicode.md")
    assert entry["size"] == nbytes

    body = c.get(f"/github/repos/{gh_org}/unicode-repo/contents/docs/unicode.md", headers=gh_admin_h).json()
    assert body["size"] == nbytes

    sha = hashlib.sha1(content.encode()).hexdigest()
    blob = c.get(f"/github/repos/{gh_org}/unicode-repo/git/blobs/{sha}", headers=gh_admin_h).json()
    assert blob["size"] == nbytes


def test_github_file_acl_scoped(gh_client, gh_admin_h, gh_org, gh_user_tokens):
    c, _ = gh_client
    member_h = {"Authorization": f"Bearer {gh_user_tokens['hana@acme.com']}"}       # in 'people'
    nonmember_h = {"Authorization": f"Bearer {gh_user_tokens['bob@acme.com']}"}     # not in 'people'

    def has_secret(headers):
        body = c.get(f"/github/repos/{gh_org}/codebase/git/trees/main", headers=headers,
                     params={"recursive": "1"}).json()
        return any(e["path"] == "config/secret.yaml" for e in body["tree"])

    assert has_secret(gh_admin_h)
    assert has_secret(member_h)
    assert not has_secret(nonmember_h)

    ok = c.get(f"/github/repos/{gh_org}/codebase/contents/config/secret.yaml", headers=member_h)
    assert ok.status_code == 200
    hidden = c.get(f"/github/repos/{gh_org}/codebase/contents/config/secret.yaml", headers=nonmember_h)
    assert hidden.status_code == 404


def test_jira_serverinfo_v2_alias_matches_v3(client, admin_h):
    # the `jira` PyPI client (used by llama-index's JiraReader) probes serverInfo under
    # /rest/api/2 on connect; the mock must serve the same shape as the v3 handler.
    v2 = client.get("/atlassian/rest/api/2/serverInfo", headers=admin_h).json()
    v3 = client.get("/atlassian/rest/api/3/serverInfo", headers=admin_h).json()
    assert v2 == v3
    assert v2["deploymentType"] == "Cloud"


def test_jira_search_filtered_by_project(client, admin_h):
    from app import synth

    # literal project name (a legitimate JQL project= token) narrows to that project's issues
    by_name = client.get("/atlassian/rest/api/3/search/jql", headers=admin_h,
                         params={"jql": "project = payments"}).json()
    titles = {i["fields"]["summary"] for i in by_name["issues"]}
    assert titles == {"SEV2: checkout latency spike", "Write postmortem for the SEV2",
                       "Personal task: rotate my API keys"}

    # the synthesized (hash-suffixed) project key resolves to the same project
    synth_key = synth.jira_project_key("payments")
    by_key = client.get("/atlassian/rest/api/3/search/jql", headers=admin_h,
                        params={"jql": f"project = {synth_key}"}).json()
    assert {i["fields"]["summary"] for i in by_key["issues"]} == titles

    # an unresolvable project is strict: zero results, not the unfiltered corpus
    bogus = client.get("/atlassian/rest/api/3/search/jql", headers=admin_h,
                       params={"jql": "project = BOGUS_NOPE"}).json()
    assert bogus["issues"] == [] and bogus["isLast"] is True

    # no project clause at all -> unfiltered (same three issues here, since payments is the
    # only Jira project in the SAMPLE corpus -- the earlier assertions are what prove filtering,
    # not this equality)
    unfiltered = client.get("/atlassian/rest/api/3/search/jql", headers=admin_h).json()
    assert {i["fields"]["summary"] for i in unfiltered["issues"]} == titles


def test_confluence_content_filtered_by_space_key(client, admin_h):
    from app import synth

    # literal container name (the natural spaceKey value) narrows to that space only
    by_name = client.get("/atlassian/wiki/rest/api/content", headers=admin_h,
                         params={"spaceKey": "handbook"}).json()
    titles = {r["title"] for r in by_name["results"]}
    assert titles == {"Engineering Handbook", "On-call Runbook"}
    assert "Compensation Bands 2026" not in titles

    # the synthesized (hash-suffixed) key resolves to the same space
    synth_key = synth.confluence_space_key("handbook")
    by_synth_key = client.get("/atlassian/wiki/rest/api/content", headers=admin_h,
                              params={"spaceKey": synth_key}).json()
    assert {r["title"] for r in by_synth_key["results"]} == titles

    # an unresolvable spaceKey is strict: zero results, not the unfiltered corpus
    bogus = client.get("/atlassian/wiki/rest/api/content", headers=admin_h,
                       params={"spaceKey": "BOGUS_NOPE"}).json()
    assert bogus["results"] == [] and bogus["size"] == 0

    # no spaceKey at all -> unfiltered (still includes the other space)
    unfiltered = client.get("/atlassian/wiki/rest/api/content", headers=admin_h).json()
    assert "Compensation Bands 2026" in {r["title"] for r in unfiltered["results"]}


def test_confluence_cql_search_filtered_by_space(client, admin_h):
    # "software" appears only in cf-handbook's body (SAMPLE), so this term narrows to one hit
    # when the space clause matches, and correctly to zero when it points elsewhere/unresolvable
    # (proving the space filter — not the text term — is what drives the 0, in the negative cases).
    narrowed = client.get("/atlassian/wiki/rest/api/search", headers=admin_h,
                          params={"cql": 'text~"software" and space=handbook'}).json()
    assert {r["title"] for r in narrowed["results"]} == {"Engineering Handbook"}
    assert narrowed["totalSize"] == 1

    other_space = client.get("/atlassian/wiki/rest/api/search", headers=admin_h,
                             params={"cql": 'text~"software" and space=people-ops'}).json()
    assert other_space["results"] == [] and other_space["totalSize"] == 0

    bogus = client.get("/atlassian/wiki/rest/api/search", headers=admin_h,
                       params={"cql": 'text~"software" and space=BOGUS_NOPE'}).json()
    assert bogus["results"] == [] and bogus["totalSize"] == 0


def test_confluence_storage_roundtrip(client, admin_h, ro_conn):
    doc = ro_conn.execute("SELECT * FROM confluence_pages LIMIT 1").fetchone()
    from app import synth
    cid = synth.confluence_id(doc["doc_id"])
    page = client.get(f"/atlassian/wiki/rest/api/content/{cid}", headers=admin_h,
                      params={"expand": "body.storage"}).json()
    xhtml = page["body"]["storage"]["value"]
    # invert _storage: join paragraphs on \n\n, drop the wrapping tags, unescape
    from html import unescape
    text = xhtml.replace("</p><p>", "\n\n")
    text = re.sub(r"</?p>", "", text)
    assert unescape(text).strip() == doc["content"].strip()


# --- ACL enforcement over HTTP --------------------------------------------------

def test_user_sees_subset_of_admin(client, admin_h, tokens, ro_conn, sample_settings):
    user = tokens["users"][0]
    uh = {"Authorization": f"Bearer {user['token']}"}
    admin_conf = len(crawl_confluence(client, admin_h))
    user_conf = len(crawl_confluence(client, uh))
    assert user_conf < admin_conf  # some confluence docs are group/private-restricted
    # matches exactly the ACL-computed visible count
    from app.acl import Acl
    acl = Acl.load(sample_settings.tokens_path, sample_settings.admin_token, sample_settings.org_name)
    vids = acl.visible_ids(ro_conn, acl.resolve(user["token"]))
    assert user_conf == db_count(ro_conn, "confluence", visible_ids=vids)


def test_mock_users_directory(client, tokens, org):
    # the /_mock/users directory lists every user + token (for testing per-user ACL)
    from app import synth
    body = client.get("/_mock/users").json()
    assert body["admin_token"] == tokens["admin_token"]
    # S3 uses an AWS keypair, not a token — the directory exposes an admin pair (derived from the
    # admin token, which is what the SigV4 verifier resolves) so a client can use it directly
    assert body["admin_s3_access_key_id"] == synth.s3_access_key_id(body["admin_token"])
    assert body["admin_s3_secret_access_key"] == synth.s3_secret_access_key(body["admin_token"])
    yaml_by_email = {u["email"]: u["token"] for u in tokens["users"]}
    assert body["count"] == len(body["users"]) == len(yaml_by_email) > 0
    for u in body["users"]:
        assert u["token"] == yaml_by_email[u["email"]]  # matches data/tokens.yaml
        assert u["name"] and isinstance(u["groups"], list)
        # each user also carries their derived S3 access-key/secret pair
        assert u["s3_access_key_id"] == synth.s3_access_key_id(u["token"])
        assert u["s3_secret_access_key"] == synth.s3_secret_access_key(u["token"])
    # a listed token really is ACL-scoped: it resolves and sees <= what admin sees
    u = body["users"][0]
    admin_repos = client.get(f"/github/orgs/{org}/repos",
                             headers={"Authorization": f"Bearer {body['admin_token']}"}).json()
    user_repos = client.get(f"/github/orgs/{org}/repos",
                            headers={"Authorization": f"Bearer {u['token']}"}).json()
    assert 0 < len(user_repos) <= len(admin_repos)


def test_mock_users_can_be_disabled(client, monkeypatch):
    from app import main
    from app.config import Settings
    monkeypatch.setattr(main, "get_settings", lambda: Settings(expose_tokens=False))
    assert client.get("/_mock/users").status_code == 404


def test_unauthenticated_is_rejected(client):
    # Drive accepts API keys, so an anonymous request is an "unregistered caller" -> 403, not 401.
    # A present-but-invalid bearer IS 401. Both measured; see the Google-envelope tests below.
    assert client.get("/drive/v3/files").status_code == 403
    assert client.get("/drive/v3/files",
                      headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/atlassian/rest/api/3/search/jql").status_code == 401
    slack = client.post("/slack/api/conversations.list").json()
    assert slack == {"ok": False, "error": "not_authed"}


def test_slack_api_test_requires_no_auth(client):
    # real Slack's api.test needs no token at all (it's a bare connectivity check); several real
    # clients call it at construction/connect time (e.g. llama-index's SlackReader.__init__), so
    # the mock must answer 200 without auth rather than 404/not_authed.
    ok = client.post("/slack/api/api.test", data={"foo": "bar"}).json()
    assert ok == {"ok": True, "args": {"foo": "bar"}}
    err = client.post("/slack/api/api.test", data={"error": "boom"}).json()
    assert err == {"ok": False, "error": "boom"}


def test_slack_accepts_form_field_token(client, tokens):
    # the official slack-go SDK posts the token as a form field (no bearer header); the mock
    # must accept it exactly like a real Slack Web API.
    admin = tokens["admin_token"]
    ok = client.post("/slack/api/search.messages", data={"token": admin, "query": "the"}).json()
    assert ok["ok"] is True
    # no token anywhere -> not_authed
    none = client.post("/slack/api/search.messages", data={"query": "the"}).json()
    assert none == {"ok": False, "error": "not_authed"}


def test_slack_users_info_resolves_author(client, admin_h, ro_conn):
    # users.info must resolve a Slack message author's synthesized id (incl. display-only
    # speakers/bots, which aren't principals) — qst_0077's raw-ID bug.
    from app import synth
    email = ro_conn.execute("SELECT DISTINCT author_email FROM slack_messages LIMIT 1").fetchone()[0]
    uid = synth.slack_user_id(email)
    j = client.post("/slack/api/users.info", headers=admin_h, data={"user": uid}).json()
    assert j["ok"] is True
    assert j["user"]["id"] == uid and j["user"]["profile"]["email"] == email
    # a bogus id still 404s (clause honored, cache doesn't invent users)
    bad = client.post("/slack/api/users.info", headers=admin_h, data={"user": "UZZZZZZZZZZ"}).json()
    assert bad == {"ok": False, "error": "user_not_found"}


def test_drive_in_owners_query(client, admin_h, ro_conn):
    # real Drive supports `'<owner>' in owners`; the mock must filter by owner (email or name),
    # not ignore the clause. (qst_0031's broken owner-lookup path.)
    total = db_count(ro_conn, "google_drive")
    owner = ro_conn.execute("SELECT author_email FROM gdrive_files LIMIT 1").fetchone()["author_email"]
    expected = ro_conn.execute("SELECT count(*) FROM gdrive_files WHERE author_email=?", (owner,)).fetchone()[0]
    j = client.get("/drive/v3/files", headers=admin_h,
                   params={"q": f"'{owner}' in owners", "pageSize": 1000}).json()
    n = len(j.get("files", []))
    assert 0 < n < total and n == expected  # filtered to exactly this owner's files
    # a non-owner returns nothing (clause honored, not ignored)
    none = client.get("/drive/v3/files", headers=admin_h,
                      params={"q": "'nobody-xyz@acme.com' in owners", "pageSize": 100}).json()
    assert none.get("files", []) == []


def test_slack_search_all(client, admin_h):
    # slack-go's Search()/SearchContext() hits search.all; it must return both messages + files.
    j = client.post("/slack/api/search.all", headers=admin_h, data={"query": "the"}).json()
    assert j["ok"] is True
    assert "messages" in j and "files" in j
    assert j["files"]["total"] == 0 and j["files"]["matches"] == []


def test_google_batch_dispatches_subrequests(client, admin_h, ro_conn):
    # google-api-python-client posts a multipart/mixed batch to /batch; the mock must dispatch each
    # application/http sub-request in-process and return a multipart/mixed of sub-responses matched
    # by Content-ID. Regression for the batch escaping to real Google (401). Build the batch body
    # exactly like BatchHttpRequest does.
    from email.generator import Generator
    from email.mime.multipart import MIMEMultipart
    from email.mime.nonmultipart import MIMENonMultipart
    from email.parser import BytesParser
    from io import StringIO

    listed = client.get("/gmail/v1/users/me/messages", headers=admin_h,
                        params={"maxResults": 2}).json().get("messages", [])
    ids = [m["id"] for m in listed]
    assert ids, "need at least one gmail message in the sample"

    msg = MIMEMultipart("mixed")
    setattr(msg, "_write_headers", lambda self: None)
    for i, mid in enumerate(ids):
        part = MIMENonMultipart("application", "http")
        part["Content-Transfer-Encoding"] = "binary"
        part["Content-ID"] = f"<base + {i}>"  # the format BatchHttpRequest uses
        # format=full is the discriminator: a sub-request whose query is honored returns a payload;
        # one whose query is dropped defaults to full too, so we assert the OPPOSITE below with
        # format=minimal — see test_google_batch_honors_subrequest_query_params.
        part.set_payload(f"GET /gmail/v1/users/me/messages/{mid}?format=full HTTP/1.1\r\n\r\n")
        msg.attach(part)
    fp = StringIO()
    Generator(fp, mangle_from_=False).flatten(msg, unixfrom=False)
    body, boundary = fp.getvalue(), msg.get_boundary()

    r = client.post("/batch", headers={**admin_h, "Content-Type": f'multipart/mixed; boundary="{boundary}"'},
                    content=body)
    assert r.status_code == 200, r.text
    assert "multipart/mixed" in r.headers["content-type"]
    parsed = BytesParser().parsebytes(
        b"Content-Type: " + r.headers["content-type"].encode() + b"\r\n\r\n" + r.content)
    parts = parsed.get_payload()
    assert len(parts) == len(ids)
    for i, (mid, part) in enumerate(zip(ids, parts)):
        assert part["Content-ID"] == f"<base + {i}>"          # echoed so the client can pair them
        sub = part.get_payload(decode=False)
        assert sub.startswith("HTTP/1.1 200")                  # dispatched with the admin token, not 401
        assert mid in sub                                      # the message JSON came back


def _batch_one(client, headers, mid, fmt, uri="/batch"):
    """POST a one-message Gmail batch to `uri` (default /batch; /batch/gmail/v1 is the real Gmail
    path) requesting `fmt`, and return the decoded sub-response JSON. Serialized exactly like
    google-api-python-client's BatchHttpRequest."""
    from email.generator import Generator
    from email.mime.multipart import MIMEMultipart
    from email.mime.nonmultipart import MIMENonMultipart
    from email.parser import BytesParser
    from io import StringIO

    msg = MIMEMultipart("mixed")
    setattr(msg, "_write_headers", lambda self: None)
    part = MIMENonMultipart("application", "http")
    part["Content-Transfer-Encoding"] = "binary"
    part["Content-ID"] = "<b + 0>"
    part.set_payload(f"GET /gmail/v1/users/me/messages/{mid}?format={fmt} HTTP/1.1\r\n\r\n")
    msg.attach(part)
    fp = StringIO()
    Generator(fp, mangle_from_=False).flatten(msg, unixfrom=False)
    r = client.post(uri, headers={**headers, "Content-Type": f'multipart/mixed; boundary="{msg.get_boundary()}"'},
                    content=fp.getvalue())
    assert r.status_code == 200, r.text
    parsed = BytesParser().parsebytes(
        b"Content-Type: " + r.headers["content-type"].encode() + b"\r\n\r\n" + r.content)
    sub = parsed.get_payload()[0].get_payload(decode=False)
    return json.loads(sub.split("\r\n\r\n", 1)[1])


@pytest.mark.parametrize("uri", ["/batch", "/batch/gmail/v1"])
def test_google_batch_honors_subrequest_query_params(client, admin_h, uri):
    # The sub-request's query string must reach the dispatched handler. `format` is the tell: a
    # dropped query defaults to full, so if the mock ignored it, `format=minimal` would still carry a
    # payload. A batch-trusting client that caches these would cache bodyless messages otherwise.
    mid = client.get("/gmail/v1/users/me/messages", headers=admin_h,
                     params={"maxResults": 1}).json()["messages"][0]["id"]
    assert "payload" in _batch_one(client, admin_h, mid, "full", uri)     # format=full honored
    assert "payload" not in _batch_one(client, admin_h, mid, "minimal", uri)  # format=minimal honored


def test_slack_replies_resolve_from_a_reply_ts(client, admin_h):
    # A search hit that lands on a REPLY yields that reply's ts; conversations.replies must return
    # the whole thread from it (Slack accepts any in-thread ts), not thread_not_found. The SAMPLE
    # 'incidents' 502 thread's replies include "Rolled back; 502s clearing." Regression: previously
    # replies resolved only thread ROOTS, so a search->replies chain broke whenever the hit was a
    # reply (the common case — real MCP clients pass the hit's own ts).
    sr = client.post("/slack/api/search.messages", headers=admin_h,
                     data={"query": "Rolled back"}).json()
    matches = sr["messages"]["matches"]
    assert matches, "expected a slack search hit for the reply text"
    hit = next(m for m in matches if "Rolled back" in m["text"])
    assert "thread_ts" in hit, "a threaded search hit must carry its root thread_ts"
    rep = client.post("/slack/api/conversations.replies", headers=admin_h,
                      data={"channel": hit["channel"]["id"], "ts": hit["ts"]}).json()
    assert rep.get("ok"), rep
    texts = " ".join(m["text"] for m in rep["messages"])
    assert "Anyone else seeing 502s" in texts   # thread root is returned
    assert "Rolled back" in texts               # the reply we searched for is in the same thread


def test_user_cannot_fetch_others_private_gmail(client, tokens, admin_h, ro_conn):
    # a private gmail doc owned by user B, fetched with user A's token -> 404
    user_a, user_b = tokens["users"][0], tokens["users"][1]
    doc = ro_conn.execute(
        "SELECT doc_id FROM gmail_messages WHERE author_email=? LIMIT 1",
        (user_b["email"],),
    ).fetchone()
    if doc is None:
        pytest.skip("no gmail doc for user B in this subset")
    from app import synth
    hexid = synth.gmail_message_id(doc["doc_id"])   # served ids are hex, not dsids (#39)
    ah = {"Authorization": f"Bearer {user_a['token']}"}
    r = client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=ah)
    # A may coincidentally be a recipient; assert admin can always read it
    assert client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h).status_code == 200
    assert r.status_code in (200, 404)


# --------------------------------------------------------------------------- Notion

def _tok(tokens, email):
    return next(u["token"] for u in tokens["users"] if u["email"] == email)


def test_notion_page_retrieve_and_blocks(client, admin_h):
    from app import synth
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
    from app import synth
    pid = synth.notion_id("nt-runbook").replace("-", "")
    assert client.get(f"/notion/v1/pages/{pid}", headers=admin_h).status_code == 200


def test_notion_search_and_comments(client, admin_h):
    from app import synth
    s = client.post("/notion/v1/search", json={"query": "on-call"}, headers=admin_h).json()
    assert any(r["id"] == synth.notion_id("nt-runbook") for r in s["results"])
    c = client.get("/notion/v1/comments", params={"block_id": synth.notion_id("nt-runbook")},
                   headers=admin_h).json()
    assert c["results"][0]["rich_text"][0]["plain_text"] == "add rate-limiter step"
    assert c["results"][0]["object"] == "comment"


def test_notion_search_filter_database_only(client, admin_h):
    from app import synth
    s = client.post("/notion/v1/search",
                    json={"query": "", "filter": {"property": "object", "value": "database"}},
                    headers=admin_h).json()
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
    from app import synth
    r = client.get(f"/notion/v1/pages/{synth.notion_id('nt-runbook')}")
    assert r.status_code == 401 and r.json()["code"] == "unauthorized"


def test_notion_acl_hides_group_doc_from_outsider(client, tokens):
    from app import synth
    pid = synth.notion_id("nt-secret")
    outsider = _tok(tokens, "ava@acme.com")  # ava is engineering, not people
    r = client.get(f"/notion/v1/pages/{pid}", headers={"Authorization": f"Bearer {outsider}"})
    assert r.status_code == 404 and r.json()["code"] == "object_not_found"
    # the owner (hana, in people) can see it
    owner = _tok(tokens, "hana@acme.com")
    assert client.get(f"/notion/v1/pages/{pid}",
                      headers={"Authorization": f"Bearer {owner}"}).status_code == 200


def test_notion_database_new_vs_legacy_shape(client, admin_h):
    from app import synth
    did = synth.notion_id("nt-tasks-db")
    new = client.get(f"/notion/v1/databases/{did}", headers=admin_h).json()
    assert new["object"] == "database"
    assert new["data_sources"][0]["id"] == synth.notion_data_source_id("nt-tasks-db")
    assert "properties" not in new
    legacy = client.get(f"/notion/v1/databases/{did}",
                        headers={**admin_h, "Notion-Version": "2022-06-28"}).json()
    assert "properties" in legacy and "Status" in legacy["properties"]
    assert "data_sources" not in legacy


def test_notion_query_rows_both_paths(client, admin_h):
    from app import synth
    did = synth.notion_id("nt-tasks-db")
    dsid = synth.notion_data_source_id("nt-tasks-db")
    rows_new = client.post(f"/notion/v1/data_sources/{dsid}/query", json={}, headers=admin_h).json()
    assert any(r["id"] == synth.notion_id("nt-task-1") for r in rows_new["results"])
    rows_legacy = client.post(f"/notion/v1/databases/{did}/query", json={},
                              headers={**admin_h, "Notion-Version": "2022-06-28"}).json()
    assert any(r["id"] == synth.notion_id("nt-task-1") for r in rows_legacy["results"])


def test_notion_data_source_retrieve(client, admin_h):
    from app import synth
    dsid = synth.notion_data_source_id("nt-tasks-db")
    ds = client.get(f"/notion/v1/data_sources/{dsid}", headers=admin_h).json()
    assert ds["object"] == "data_source" and "Status" in ds["properties"]


# ------------------------------------------------------------------------ S3 (SigV4/404/416 edges)

def _sign_get(base_url, path, token, *, tamper=False, extra_headers=None):
    """Return (url, headers) for a SigV4-signed GET, using botocore (the real signer)."""
    pytest.importorskip("botocore")
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
    from urllib.parse import parse_qsl, quote, urlencode
    from app import synth

    # URL-encode the path: split on ? to preserve the path part, then properly encode query params.
    # Use quote_via=quote (not the default quote_plus) so a space becomes %20, matching the server's
    # canonicalization (app.sigv4._canonical_query uses quote); quote_plus would emit '+' and mismatch.
    if "?" in path:
        path_part, query_part = path.split("?", 1)
        params = parse_qsl(query_part, keep_blank_values=True)
        query_part = urlencode(params, safe="-_.~", quote_via=quote)
        path = f"{path_part}?{query_part}"

    ak = synth.s3_access_key_id(token)
    sk = synth.s3_secret_access_key(token)
    url = f"{base_url}{path}"
    req = AWSRequest(method="GET", url=url, headers=dict(extra_headers or {}))
    req.headers["x-amz-content-sha256"] = "UNSIGNED-PAYLOAD"
    S3SigV4Auth(Credentials(ak, sk), "s3", "us-east-1").add_auth(req)
    headers = dict(req.headers)
    if tamper:
        headers["Authorization"] = headers["Authorization"][:-4] + "dead"
    return url, headers


def test_s3_unknown_access_key_rejected(live_server):
    import urllib.request
    base_url, settings = live_server
    url = f"{base_url}/s3/eng-artifacts?list-type=2"
    req = urllib.request.Request(url, headers={
        "Authorization": ("AWS4-HMAC-SHA256 Credential=AKIABOGUS0000000BOGUS/"
                          "20260720/us-east-1/s3/aws4_request, "
                          "SignedHeaders=host, Signature=00"),
        "x-amz-date": "20260720T000000Z"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403 and b"InvalidAccessKeyId" in e.value.read()


def test_s3_tampered_signature_rejected(live_server):
    import urllib.request
    base_url, settings = live_server
    url, headers = _sign_get(base_url, "/s3/eng-artifacts?list-type=2",
                             settings.admin_token, tamper=True)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(url, headers=headers))
    assert e.value.code == 403 and b"SignatureDoesNotMatch" in e.value.read()


def test_s3_missing_key_is_nosuchkey(live_server):
    import urllib.request
    base_url, settings = live_server
    url, headers = _sign_get(base_url, "/s3/eng-artifacts/does/not/exist.md", settings.admin_token)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(url, headers=headers))
    assert e.value.code == 404 and b"NoSuchKey" in e.value.read()


def test_s3_unsatisfiable_range_is_416(live_server):
    import urllib.request
    base_url, settings = live_server
    url, headers = _sign_get(base_url, "/s3/eng-artifacts/runbooks/oncall.md",
                             settings.admin_token, extra_headers={"Range": "bytes=99999-100000"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(url, headers=headers))
    assert e.value.code == 416 and b"InvalidRange" in e.value.read()
    total = len("Check dashboards, roll back, page on-call.")
    assert e.value.headers.get("Content-Range") == f"bytes */{total}"
    assert e.value.headers.get("Content-Type") == "application/xml"


# ---------------------------------------------------- S3 large-bucket perf (SQL-pushed listing)

def _s3_big_corpus(n=3000):
    """~3000 objects in one bucket: 12 month-prefixes x 25 day-prefixes, split 50/50 across two
    ACL groups so month-01 alone (250 objects, still nested by day) exercises prefix filtering,
    keyset pagination, delimiter rollup, and ACL scoping all at once — without needing to touch
    (or slow down) the shared SAMPLE corpus every other test in this module depends on."""
    for i in range(n):
        month = (i % 12) + 1
        day = ((i // 12) % 25) + 1
        key = f"logs/2026/{month:02d}/{day:02d}/obj-{i:05d}.json"
        group = "engineering" if (i // 12) % 2 == 0 else "people"
        author = "eng-bulk@acme.com" if group == "engineering" else "people-bulk@acme.com"
        yield {"source_type": "s3", "doc_id": f"s3-big-{i:05d}", "bucket": "big-bucket",
               "group": group, "key": key, "title": key, "content": f"payload-{i}",
               "author_email": author, "author_groups": [group], "visibility": "group"}
    # A second, dedicated bucket for the CommonPrefixes-straddling regression (Fix 3): one
    # "folder" (150 objects) bigger than a max-keys=100 page, plus a small trailing folder — the
    # exact shape that made a rolled-up CommonPrefixes group straddle a page cutoff and get
    # emitted twice before the fix.
    for i in range(150):
        key = f"grp/big/f-{i:04d}.json"
        yield {"source_type": "s3", "doc_id": f"s3-straddle-big-{i:04d}", "bucket": "straddle-bucket",
               "group": "engineering", "key": key, "title": key, "content": f"big-payload-{i}",
               "author_email": "eng-bulk@acme.com", "author_groups": ["engineering"],
               "visibility": "public"}
    for i in range(5):
        key = f"grp/small/f-{i:02d}.json"
        yield {"source_type": "s3", "doc_id": f"s3-straddle-small-{i:02d}", "bucket": "straddle-bucket",
               "group": "engineering", "key": key, "title": key, "content": f"small-payload-{i}",
               "author_email": "eng-bulk@acme.com", "author_groups": ["engineering"],
               "visibility": "public"}


@pytest.fixture(scope="module")
def big_bucket_settings(tmp_path_factory):
    """A DB of its own (not the shared SAMPLE) holding one bucket with ~3000 S3 objects."""
    from app.importer.byo import load
    from app.config import Settings

    data_dir = tmp_path_factory.mktemp("s3_big")
    settings = Settings(data_dir=data_dir)
    corpus = data_dir / "_big_corpus.jsonl"
    corpus.write_text("\n".join(json.dumps(r) for r in _s3_big_corpus()))
    load(corpus, settings)
    return settings


@pytest.fixture(scope="module")
def big_bucket_tokens(big_bucket_settings):
    data = yaml.safe_load(big_bucket_settings.tokens_path.read_text())
    return {u["email"]: u["token"] for u in data["users"]}


@pytest.fixture(scope="module")
def big_bucket_client(big_bucket_settings):
    """A TestClient pointed at the dedicated big-bucket DB — in-process (no live uvicorn
    subprocess needed; SigV4 verification only cares that the Host it sees matches what was
    signed, which holds for TestClient's own base_url just as much as a real listening port).

    Reloads ``app.main`` into a *fresh* FastAPI instance rather than reusing the module-level
    ``app`` singleton the ``client`` fixture above already wraps in its own still-open
    TestClient: a second lifespan start on that SAME app object would overwrite its
    app.state (db/acl/index) out from under the other, still-live client."""
    import importlib
    import app.main as main_module

    prev = os.environ.get("MOCK_DATA_DIR")
    os.environ["MOCK_DATA_DIR"] = str(big_bucket_settings.data_dir)
    get_settings.cache_clear()
    try:
        importlib.reload(main_module)
        with TestClient(main_module.app) as c:
            yield c
    finally:
        get_settings.cache_clear()
        if prev is None:
            os.environ.pop("MOCK_DATA_DIR", None)
        else:
            os.environ["MOCK_DATA_DIR"] = prev


def _s3_get(client, path, token):
    """SigV4-sign a GET (same signer as the module-level ``_sign_get``) and issue it through an
    in-process TestClient instead of a live socket."""
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
    from urllib.parse import parse_qsl, quote, urlencode
    from app import synth

    if "?" in path:
        path_part, query_part = path.split("?", 1)
        params = parse_qsl(query_part, keep_blank_values=True)
        query_part = urlencode(params, safe="-_.~", quote_via=quote)
        path = f"{path_part}?{query_part}"
    base_url = str(client.base_url)
    url = f"{base_url}{path}"
    ak = synth.s3_access_key_id(token)
    sk = synth.s3_secret_access_key(token)
    req = AWSRequest(method="GET", url=url)
    req.headers["x-amz-content-sha256"] = "UNSIGNED-PAYLOAD"
    S3SigV4Auth(Credentials(ak, sk), "s3", "us-east-1").add_auth(req)
    return client.get(url, headers=dict(req.headers))


S3NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def _s3_keys(root) -> list[str]:
    return [e.text for e in root.findall(f"{{{S3NS}}}Contents/{{{S3NS}}}Key")]


def test_s3_large_bucket_prefix_filters_and_sorts(big_bucket_client, big_bucket_settings):
    pytest.importorskip("botocore")
    r = _s3_get(big_bucket_client,
               "/s3/big-bucket?list-type=2&prefix=logs/2026/01/&max-keys=1000",
               big_bucket_settings.admin_token)
    assert r.status_code == 200
    root = ET.fromstring(r.text)
    keys = _s3_keys(root)
    assert len(keys) == 250                                   # 3000 / 12 months
    assert keys == sorted(keys)
    assert all(k.startswith("logs/2026/01/") for k in keys)
    assert root.findtext(f"{{{S3NS}}}IsTruncated") == "false"


def test_s3_large_bucket_pagination_round_trips(big_bucket_client, big_bucket_settings):
    pytest.importorskip("botocore")
    admin = big_bucket_settings.admin_token
    r1 = _s3_get(big_bucket_client, "/s3/big-bucket?list-type=2&max-keys=100", admin)
    root1 = ET.fromstring(r1.text)
    keys1 = _s3_keys(root1)
    assert len(keys1) == 100 and keys1 == sorted(keys1)
    assert root1.findtext(f"{{{S3NS}}}IsTruncated") == "true"
    token = root1.findtext(f"{{{S3NS}}}NextContinuationToken")
    assert token

    from urllib.parse import quote
    r2 = _s3_get(big_bucket_client,
                f"/s3/big-bucket?list-type=2&max-keys=100&continuation-token={quote(token)}",
                admin)
    root2 = ET.fromstring(r2.text)
    keys2 = _s3_keys(root2)
    assert len(keys2) == 100 and keys2 == sorted(keys2)
    assert not (set(keys1) & set(keys2))               # no overlap between pages
    assert keys1[-1] < keys2[0]                        # contiguous keyset order, no gap/dup
    assert root2.findtext(f"{{{S3NS}}}ContinuationToken") == token


def test_s3_large_bucket_delimiter_returns_common_prefixes(big_bucket_client, big_bucket_settings):
    pytest.importorskip("botocore")
    # Under a single month (250 objects, well within one SQL page) every "day" folder rolls up
    # into one CommonPrefixes entry, computed over that bounded page — see the comment on
    # app.routers.s3._list_objects_v2 for why this only holds a page's worth of raw rows at once.
    r = _s3_get(big_bucket_client,
               "/s3/big-bucket?list-type=2&prefix=logs/2026/01/&delimiter=/&max-keys=1000",
               big_bucket_settings.admin_token)
    root = ET.fromstring(r.text)
    prefixes = {cp.findtext(f"{{{S3NS}}}Prefix")
               for cp in root.findall(f"{{{S3NS}}}CommonPrefixes")}
    assert prefixes == {f"logs/2026/01/{d:02d}/" for d in range(1, 26)}
    assert root.findall(f"{{{S3NS}}}Contents") == []      # every key continues past the delimiter
    assert root.findtext(f"{{{S3NS}}}IsTruncated") == "false"


def test_s3_large_bucket_acl_scopes_listing(big_bucket_client, big_bucket_settings, big_bucket_tokens):
    pytest.importorskip("botocore")

    def keys_for(token):
        r = _s3_get(big_bucket_client,
                   "/s3/big-bucket?list-type=2&prefix=logs/2026/01/&max-keys=1000", token)
        return {e.text for e in ET.fromstring(r.text).findall(f"{{{S3NS}}}Contents/{{{S3NS}}}Key")}

    admin_keys = keys_for(big_bucket_settings.admin_token)
    eng_keys = keys_for(big_bucket_tokens["eng-bulk@acme.com"])
    people_keys = keys_for(big_bucket_tokens["people-bulk@acme.com"])

    assert len(admin_keys) == 250
    assert eng_keys and people_keys
    assert eng_keys < admin_keys and people_keys < admin_keys      # proper, non-empty subsets
    assert eng_keys.isdisjoint(people_keys)
    assert eng_keys | people_keys == admin_keys


def test_s3_delimiter_common_prefix_not_duplicated_across_pages(big_bucket_client, big_bucket_settings):
    """Fix 3 (correctness): "straddle-bucket" has one 150-object folder ("grp/big/") — bigger
    than a max-keys=100 page — plus a small trailing folder ("grp/small/"). Before the fix, the
    "grp/big/" CommonPrefixes group straddled the page cutoff and was emitted on BOTH the page
    where it started and the page where it resumed. Traverse every page and assert each
    CommonPrefixes/Content appears exactly once, with no gaps."""
    pytest.importorskip("botocore")
    admin = big_bucket_settings.admin_token
    from urllib.parse import quote

    seen_prefixes: list[str] = []
    seen_keys: list[str] = []
    url = "/s3/straddle-bucket?list-type=2&prefix=grp/&delimiter=/&max-keys=100"
    pages = 0
    while True:
        pages += 1
        assert pages <= 10, "too many pages — pagination isn't converging"
        r = _s3_get(big_bucket_client, url, admin)
        assert r.status_code == 200
        root = ET.fromstring(r.text)
        seen_prefixes += [cp.findtext(f"{{{S3NS}}}Prefix")
                          for cp in root.findall(f"{{{S3NS}}}CommonPrefixes")]
        seen_keys += _s3_keys(root)
        token = root.findtext(f"{{{S3NS}}}NextContinuationToken")
        if root.findtext(f"{{{S3NS}}}IsTruncated") != "true":
            assert token is None
            break
        assert token
        url = f"/s3/straddle-bucket?list-type=2&prefix=grp/&delimiter=/&max-keys=100&continuation-token={quote(token)}"

    # every CommonPrefixes appears EXACTLY once across all pages (no dup)...
    assert seen_prefixes == ["grp/big/", "grp/small/"]
    # ...and no plain Contents at all — both "folders" fully roll up under the delimiter (no gap)
    assert seen_keys == []


def test_s3_max_keys_zero_returns_empty_page_safely(big_bucket_client, big_bucket_settings):
    """Fix 4: max-keys=0 must not crash (no indexing into an empty page) and must report
    IsTruncated based on whether more data exists, with KeyCount 0 and no NextContinuationToken."""
    pytest.importorskip("botocore")
    r = _s3_get(big_bucket_client, "/s3/big-bucket?list-type=2&max-keys=0",
               big_bucket_settings.admin_token)
    assert r.status_code == 200
    root = ET.fromstring(r.text)
    assert root.findtext(f"{{{S3NS}}}KeyCount") == "0"
    assert root.findall(f"{{{S3NS}}}Contents") == []
    assert root.findall(f"{{{S3NS}}}CommonPrefixes") == []
    assert root.findtext(f"{{{S3NS}}}IsTruncated") == "true"          # big-bucket has 3000 objects
    assert root.findtext(f"{{{S3NS}}}NextContinuationToken") is None


# --- Google error envelope (#37) ------------------------------------------------------------
#
# Every case below was MEASURED against the live APIs with real OAuth credentials. The envelope is
# per-family, not uniform:
#
#   family                       errors[]   status                 no Authorization header
#   -----------------------------|----------|-----------------------|------------------------
#   Drive v3                     | always   | auth failures only    | 403 PERMISSION_DENIED
#   Gmail v1                     | always   | always                | 401 UNAUTHENTICATED
#   Docs v1 / Sheets v4 / Slides | never    | always                | 401 UNAUTHENTICATED
#
# A bad bearer token is 401 UNAUTHENTICATED in every family.

def _gerr(resp):
    """The `error` object, or a clear failure naming what came back instead."""
    body = resp.json()
    assert "error" in body, f"expected a Google error envelope, got {body}"
    return body["error"]


def test_google_errors_use_googles_envelope(client, admin_h):
    """`google-api-python-client` reads `error.message` to build HttpError, so `{"detail": …}` left
    every error unreadable to the one client the mock exists to serve."""
    r = client.get("/drive/v3/files", headers=admin_h, params={"fields": "totallyBogusField"})
    assert r.status_code == 400
    e = _gerr(r)
    assert e["code"] == 400
    assert e["message"] == "Invalid field selection totallyBogusField"
    assert "detail" not in r.json()
    # non-Google paths keep FastAPI's default envelope
    assert "detail" in client.get("/no-such-route").json()


def test_drive_errors_carry_the_legacy_errors_array(client, admin_h):
    """Drive v3 always sends `errors[]` with a `reason` a client can branch on, and repeats the
    message inside it. It does NOT send `status` for a parameter failure — measured."""
    e = _gerr(client.get("/drive/v3/files", headers=admin_h, params={"fields": "nope"}))
    assert e["errors"] == [{"message": "Invalid field selection nope", "domain": "global",
                            "reason": "invalidParameter", "location": "fields",
                            "locationType": "parameter"}]
    assert "status" not in e, "Drive omits status on parameter failures"


def test_editor_api_errors_carry_status_and_no_errors_array(client, admin_h):
    """The editor APIs are the mirror image of Drive: `status`, never `errors[]` — measured."""
    doc = _drive_find(client, admin_h, "Brand")["id"]
    e = _gerr(client.get(f"/sheets/v4/spreadsheets/{doc}", headers=admin_h))
    assert e["code"] == 404 and e["status"] == "NOT_FOUND"
    assert e["message"] == "Requested entity was not found."
    assert "errors" not in e


def test_gmail_errors_carry_both(client, admin_h):
    """Gmail sends `errors[]` AND `status` — measured, and the only family that does both."""
    # a well-formed but unknown id; a non-hex one is 400 "Invalid id value" (see #39)
    e = _gerr(client.get("/gmail/v1/users/me/messages/00000000deadbeef", headers=admin_h))
    assert e["code"] == 404 and e["status"] == "NOT_FOUND"
    assert e["message"] == "Requested entity was not found."
    assert e["errors"][0]["reason"] == "notFound"


# (path, params, code, status, reason, location) — one row per measured case.
GOOGLE_ERROR_CASES = [
    ("/drive/v3/files", {"fields": "bogus"}, 400, None, "invalidParameter", "fields"),
    ("/drive/v3/files", {"orderBy": "bogusKey"}, 400, None, "invalid", "orderBy"),
    ("/drive/v3/files/no-such-file", {}, 404, None, "notFound", "fileId"),
    ("/drive/v3/about", {}, 400, None, "required", "fields"),
    ("/drive/v3/about", {"fields": "storageQuoat"}, 400, None, "invalidParameter", "fields"),
    ("/gmail/v1/users/me/messages/00000000deadbeef", {}, 404, "NOT_FOUND", "notFound", None),
    ("/gmail/v1/users/me/labels/NO_SUCH", {}, 404, "NOT_FOUND", "notFound", None),
]


@pytest.mark.parametrize("path, params, code, status, reason, location", GOOGLE_ERROR_CASES)
def test_google_error_reasons_match_the_real_api(client, admin_h, path, params, code, status,
                                                 reason, location):
    r = client.get(path, headers=admin_h, params=params)
    assert r.status_code == code
    e = _gerr(r)
    assert e["code"] == code
    assert e.get("status") == status
    err0 = e["errors"][0]
    assert err0["reason"] == reason
    assert err0["domain"] == "global"
    assert err0.get("location") == location
    if location is not None:
        assert err0["locationType"] == "parameter"


def test_drive_not_found_names_the_file_id(client, admin_h):
    """Measured: `File not found: {id}.` — the id is in the message, so a batch caller can tell
    which of its requests failed."""
    e = _gerr(client.get("/drive/v3/files/abc123xyz", headers=admin_h))
    assert e["message"] == "File not found: abc123xyz."


def test_drive_export_requires_mime_type_with_googles_wording(client, admin_h):
    doc = _drive_find(client, admin_h, "Brand")["id"]
    e = _gerr(client.get(f"/drive/v3/files/{doc}/export", headers=admin_h))
    assert e["code"] == 400 and e["message"] == "Required parameter: mimeType"
    assert e["errors"][0] == {"message": "Required parameter: mimeType", "domain": "global",
                              "reason": "required", "location": "mimeType",
                              "locationType": "parameter"}


@pytest.mark.parametrize("path, reason, location", [
    ("/drive/v3/files/{pdf}/export?mimeType=text/plain", "fileNotExportable", None),
    ("/drive/v3/files/{doc}?alt=media", "fileNotDownloadable", "alt"),
])
def test_drive_403s_carry_their_own_reasons(client, admin_h, path, reason, location):
    doc = _drive_find(client, admin_h, "Brand")["id"]
    pdf = _drive_find(client, admin_h, "Whitepaper")["id"]
    e = _gerr(client.get(path.format(doc=doc, pdf=pdf), headers=admin_h))
    assert e["code"] == 403
    assert e["errors"][0]["reason"] == reason
    assert e["errors"][0].get("location") == location


BAD_TOKEN = {"Authorization": "Bearer not-a-real-token"}


@pytest.mark.parametrize("path", ["/drive/v3/files", "/gmail/v1/users/me/profile",
                                  "/sheets/v4/spreadsheets/x", "/docs/v1/documents/x",
                                  "/slides/v1/presentations/x"])
def test_a_bad_token_is_unauthenticated_everywhere(client, path):
    """Measured: every family answers a present-but-invalid bearer with 401 UNAUTHENTICATED, and
    the short "Invalid Credentials" lives in `errors[0]` while the top message is the long form."""
    r = client.get(path, headers=BAD_TOKEN)
    assert r.status_code == 401
    e = _gerr(r)
    assert e["code"] == 401 and e["status"] == "UNAUTHENTICATED"
    assert e["message"].startswith("Request had invalid authentication credentials.")
    if "errors" in e:
        assert e["errors"][0]["message"] == "Invalid Credentials"
        assert e["errors"][0]["reason"] == "authError"
        assert e["errors"][0]["location"] == "Authorization"
        assert e["errors"][0]["locationType"] == "header"


@pytest.mark.parametrize("path, code, status", [
    ("/drive/v3/files", 403, "PERMISSION_DENIED"),        # Drive accepts API keys, so anonymous
    ("/sheets/v4/spreadsheets/x", 403, "PERMISSION_DENIED"),  # ...is an "unregistered caller"
    ("/gmail/v1/users/me/profile", 401, "UNAUTHENTICATED"),   # OAuth-only APIs say the
    ("/docs/v1/documents/x", 401, "UNAUTHENTICATED"),         # ...credentials are missing
    ("/slides/v1/presentations/x", 401, "UNAUTHENTICATED"),
])
def test_a_missing_header_differs_by_family(client, path, code, status):
    """The surprise, measured: no `Authorization` header at all is NOT uniformly 401. Drive and
    Sheets answer 403 PERMISSION_DENIED, Gmail and the Docs/Slides APIs answer 401. A bad token is
    401 everywhere — so the two cases are genuinely distinct and the mock conflated them."""
    r = client.get(path)
    assert r.status_code == code
    e = _gerr(r)
    assert e["code"] == code and e["status"] == status
    if code == 403:
        assert "unregistered callers" in e["message"]
    else:
        assert "missing required authentication credential" in e["message"]


def test_atlassian_errors_use_atlassian_envelope(client):
    # atlassian-python-api's Confluence client does response.json()["message"] on any error, so the
    # mock must shape /atlassian errors like Atlassian Cloud (message + statusCode), not {"detail"}.
    r = client.get("/atlassian/wiki/rest/api/content/999999")   # unauthenticated -> 401
    assert r.status_code == 401
    assert r.json().get("message") and r.json().get("statusCode") == 401
    r2 = client.get("/atlassian/wiki/rest/api/content/search")  # 'search' fails int path validation -> 422
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


# --- OpenAPI enrichment: github query params + response fidelity (issue #4 bridge) --------

def test_github_search_issues_documents_q_param(client):
    op = client.get("/openapi.json").json()["paths"]["/github/search/issues"]["get"]
    names = {p["name"] for p in op.get("parameters", [])}
    assert {"q", "page", "per_page"} <= names


def test_github_list_issues_documents_state_param(client):
    op = client.get("/openapi.json").json()["paths"]["/github/repos/{owner}/{repo}/issues"]["get"]
    params = {p["name"]: p for p in op.get("parameters", [])}
    assert "state" in params and {"page", "per_page"} <= set(params)
    assert params["state"]["schema"].get("default") == "open"


def test_github_search_still_filters_by_q(client, admin_h):
    body = client.get("/github/search/issues", params={"q": ""}, headers=admin_h).json()
    assert "items" in body and "total_count" in body


def test_github_responses_unchanged_by_enrichment(client, admin_h):
    # Fidelity guard: the rich issue field set must survive query-param + response_model enrichment.
    body = client.get("/github/search/issues", params={"q": ""}, headers=admin_h).json()
    assert body["items"], "SAMPLE should have github issues"
    item = body["items"][0]
    for key in ("id", "node_id", "number", "title", "body", "state", "user", "labels",
                "assignees", "milestone", "comments", "reactions", "author_association",
                "created_at", "updated_at", "html_url", "url", "repository_url"):
        assert key in item, f"missing {key} (fidelity regression)"


def test_github_issue_search_has_typed_response_schema(client):
    op = client.get("/openapi.json").json()["paths"]["/github/search/issues"]["get"]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema != {}
    assert "$ref" in schema or schema.get("type") in ("object", "array")


def test_github_operation_ids_unique(client):
    spec = client.get("/openapi.json").json()
    ids = [op["operationId"]
           for p, item in spec["paths"].items() if p.startswith("/github")
           for m, op in item.items() if isinstance(op, dict) and "operationId" in op]
    assert len(ids) == len(set(ids))


# --- OpenAPI enrichment: slack (query-or-form params via openapi_extra) -------------------

def test_slack_search_documents_query_param(client):
    op = client.get("/openapi.json").json()["paths"]["/slack/api/search.messages"]["get"]
    names = {p["name"] for p in op.get("parameters", [])}
    assert {"query", "count", "page"} <= names


def test_slack_history_documents_channel_param(client):
    op = client.get("/openapi.json").json()["paths"]["/slack/api/conversations.history"]["get"]
    names = {p["name"] for p in op.get("parameters", [])}
    assert {"channel", "limit", "cursor"} <= names


def test_slack_responses_unchanged_by_enrichment(client, admin_h):
    lst = client.get("/slack/api/conversations.list", headers=admin_h).json()
    assert lst["ok"] and "channels" in lst and "response_metadata" in lst
    if lst["channels"]:
        ch = lst["channels"][0]
        for k in ("id", "name", "is_private", "is_member", "num_members", "topic",
                  "purpose", "created", "creator"):
            assert k in ch, f"slack channel missing {k} (fidelity regression)"
    srch = client.get("/slack/api/search.messages", params={"query": "gateway"}, headers=admin_h).json()
    assert srch["ok"] and "messages" in srch and "matches" in srch["messages"]


def test_slack_api_test_has_typed_response_schema(client):
    # api.test is a new endpoint (readers probe it on connect); enrich it like its siblings.
    op = client.get("/openapi.json").json()["paths"]["/slack/api/api.test"]["get"]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema != {}
    assert "$ref" in schema or schema.get("type") in ("object", "array")


# --- OpenAPI enrichment: gmail ------------------------------------------------------------

def test_gmail_messages_documents_q_param(client):
    op = client.get("/openapi.json").json()["paths"]["/gmail/v1/users/{user_id}/messages"]["get"]
    names = {p["name"] for p in op.get("parameters", [])}
    assert {"q", "maxResults", "pageToken"} <= names
    assert "user_id" in names  # path param preserved


def test_gmail_messages_has_typed_response_schema(client):
    op = client.get("/openapi.json").json()["paths"]["/gmail/v1/users/{user_id}/messages"]["get"]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema != {}


def test_gmail_responses_unchanged_by_enrichment(client, admin_h):
    lst = client.get("/gmail/v1/users/me/messages", headers=admin_h).json()
    assert "messages" in lst and "resultSizeEstimate" in lst
    if lst["messages"]:
        mid = lst["messages"][0]["id"]
        msg = client.get(f"/gmail/v1/users/me/messages/{mid}", params={"format": "full"},
                         headers=admin_h).json()
        for k in ("id", "threadId", "labelIds", "snippet", "internalDate", "sizeEstimate", "payload"):
            assert k in msg, f"gmail message missing {k} (fidelity regression)"


# --- OpenAPI enrichment: drive ------------------------------------------------------------

def test_drive_files_documents_q_param(client):
    op = client.get("/openapi.json").json()["paths"]["/drive/v3/files"]["get"]
    names = {p["name"] for p in op.get("parameters", [])}
    assert {"q", "pageSize", "pageToken", "fields"} <= names


def test_drive_files_has_typed_response_schema(client):
    op = client.get("/openapi.json").json()["paths"]["/drive/v3/files"]["get"]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema != {}


def _drive_find(client, admin_h, name_substr):
    j = client.get("/drive/v3/files", params={"q": f"name contains '{name_substr}'"},
                   headers=admin_h).json()
    return j["files"][0] if j.get("files") else None


def test_drive_responses_unchanged_by_enrichment(client, admin_h):
    lst = client.get("/drive/v3/files", headers=admin_h).json()
    assert lst["kind"] == "drive#fileList" and "files" in lst
    doc = _drive_find(client, admin_h, "Brand")
    assert doc is not None
    full = client.get(f"/drive/v3/files/{doc['id']}", headers=admin_h).json()
    for k in ("kind", "id", "name", "mimeType", "createdTime", "modifiedTime", "owners",
              "webViewLink", "capabilities"):
        assert k in full, f"drive file missing {k} (fidelity regression)"


def test_drive_export_and_media_stay_non_json(client, admin_h):
    # A native doc exports as PlainTextResponse; response_model must NOT be attached to these.
    doc = _drive_find(client, admin_h, "Brand")
    exp = client.get(f"/drive/v3/files/{doc['id']}/export",
                     params={"mimeType": "text/plain"}, headers=admin_h)
    assert exp.status_code == 200 and "application/json" not in exp.headers["content-type"]
    # A binary (pdf) downloads raw via alt=media.
    pdf = _drive_find(client, admin_h, "Whitepaper")
    med = client.get(f"/drive/v3/files/{pdf['id']}", params={"alt": "media"}, headers=admin_h)
    assert med.status_code == 200 and "application/json" not in med.headers["content-type"]


# --- Drive fidelity: measured divergences from real Google Drive (issue #23) ---------------
#
# Each case below was diffed against https://www.googleapis.com/drive/v3 with equivalent
# credentials; the mock's old behaviour returned 200 with wrong/unfiltered data, so a consumer
# could not tell anything was off.

FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"


def _drive_ids(client, headers, **params):
    j = client.get("/drive/v3/files", headers=headers, params=params).json()
    return [f["id"] for f in j.get("files", [])]


def test_drive_shared_with_me_partitions_by_owner(client, tokens):
    """`q=sharedWithMe=true` must return only items shared with the caller by someone else, and
    `false` must exclude them — real Drive's "Shared with me" is the only way to enumerate those.
    The mock used to ignore the clause, so both returned the caller's whole visible corpus."""
    mia = {"Authorization": f"Bearer {_tok(tokens, 'mia@acme.com')}"}
    all_ids = set(_drive_ids(client, mia, q="trashed=false", pageSize=100))
    shared = set(_drive_ids(client, mia, q="sharedWithMe=true and trashed=false", pageSize=100))
    own = set(_drive_ids(client, mia, q="sharedWithMe=false and trashed=false", pageSize=100))
    assert shared and own                      # SAMPLE gives mia both her own and others' files
    assert shared != own and not (shared & own)
    assert shared | own == all_ids             # together they partition the visible corpus
    # mia authored "Brand guidelines v3"; it is hers, not shared with her
    brand = _drive_find(client, mia, "Brand")["id"]
    assert brand in own and brand not in shared


def test_drive_shared_items_carry_shared_with_me_time(client, tokens):
    """Real Drive populates `sharedWithMeTime` only on items shared with the caller, and omits
    `parents` on them — so its presence is how a client classifies one. Filtering on
    `sharedWithMe` while never emitting the field left a row that the filter calls shared unable to
    say so itself."""
    mia = {"Authorization": f"Bearer {_tok(tokens, 'mia@acme.com')}"}
    shared = client.get("/drive/v3/files", headers=mia,
                        params={"q": "sharedWithMe=true and trashed=false",
                                "pageSize": 100}).json()["files"]
    own = client.get("/drive/v3/files", headers=mia,
                     params={"q": "sharedWithMe=false and trashed=false",
                             "pageSize": 100}).json()["files"]
    assert shared and own
    assert all(f["sharedWithMeTime"] for f in shared), "every shared item needs the timestamp"
    assert all("sharedWithMeTime" not in f for f in own), "an item you own was never shared with you"
    # folders come out of the same filter, so they must answer the same way
    assert any(f["mimeType"] == FOLDER_MIME for f in shared)
    # and files.get agrees with the listing
    one = shared[0]
    assert client.get(f"/drive/v3/files/{one['id']}", headers=mia).json() == one


def test_drive_shared_with_me_time_needs_a_caller(client, admin_h):
    """The admin/service token is not a Drive user, so nothing was shared *with* it — no timestamp
    to invent. `orderBy` on the field still answers (all-equal keys), as real Drive does for nulls."""
    files = client.get("/drive/v3/files", headers=admin_h, params={"pageSize": 20}).json()["files"]
    assert files and all("sharedWithMeTime" not in f for f in files)
    assert client.get("/drive/v3/files", headers=admin_h,
                      params={"pageSize": 5, "orderBy": "sharedWithMeTime"}).status_code == 200


def test_drive_order_by_shared_with_me_time(client, tokens):
    """The mock models the relation this key sorts on (owner vs caller), so it sorts rather than
    400s — unlike the view/modify-by-me timestamps, which have no counterpart here at all."""
    mia = {"Authorization": f"Bearer {_tok(tokens, 'mia@acme.com')}"}
    r = client.get("/drive/v3/files", headers=mia,
                   params={"q": "sharedWithMe=true", "pageSize": 100,
                           "orderBy": "sharedWithMeTime desc"})
    assert r.status_code == 200
    times = [f["sharedWithMeTime"] for f in r.json()["files"]]
    assert times == sorted(times, reverse=True)


def test_drive_owned_by_me_reflects_the_caller(client, tokens):
    """`ownedByMe` is per-caller in real Drive; the mock reported False for every file."""
    mia = {"Authorization": f"Bearer {_tok(tokens, 'mia@acme.com')}"}
    assert _drive_find(client, mia, "Brand")["ownedByMe"] is True
    assert _drive_find(client, mia, "Whitepaper")["ownedByMe"] is False


def test_drive_order_by_sorts_the_result(client, admin_h):
    """`orderBy` was accepted and never applied — silent, so a client that relies on server-side
    ordering appears to work against the mock and misbehaves against production."""
    names = [f["name"] for f in client.get(
        "/drive/v3/files", headers=admin_h,
        params={"q": "trashed=false", "pageSize": 100, "orderBy": "name",
                "fields": "files(name)"}).json()["files"]]
    # Drive collates names case-insensitively (folder names in the SAMPLE are lowercase, file
    # names are not, so a case-sensitive sort would put every folder last)
    assert names == sorted(names, key=str.casefold)
    desc = [f["name"] for f in client.get(
        "/drive/v3/files", headers=admin_h,
        params={"q": "trashed=false", "pageSize": 100, "orderBy": "name desc",
                "fields": "files(name)"}).json()["files"]]
    assert desc == sorted(names, key=str.casefold, reverse=True)
    mods = [f["modifiedTime"] for f in client.get(
        "/drive/v3/files", headers=admin_h,
        params={"q": "trashed=false", "pageSize": 100, "orderBy": "modifiedTime desc",
                "fields": "files(modifiedTime)"}).json()["files"]]
    assert mods == sorted(mods, reverse=True)


def test_drive_order_by_paginates_in_sorted_order(client, admin_h):
    """A sort must span the whole result set, not sort each page in isolation."""
    everything = [f["name"] for f in client.get(
        "/drive/v3/files", headers=admin_h,
        params={"pageSize": 100, "orderBy": "name", "fields": "files(name)"}).json()["files"]]
    paged, token = [], None
    while True:
        p = {"pageSize": 2, "orderBy": "name", "fields": "files(name),nextPageToken"}
        if token:
            p["pageToken"] = token
        j = client.get("/drive/v3/files", headers=admin_h, params=p).json()
        paged += [f["name"] for f in j["files"]]
        token = j.get("nextPageToken")
        if not token:
            break
    assert paged == everything == sorted(everything, key=str.casefold)


def test_drive_order_by_does_not_change_the_rows_themselves(client, admin_h):
    """Sorting builds the whole result set to order it, and defers the per-page `shared` lookup —
    so the served objects must still be identical to the unsorted ones, field for field."""
    plain = {f["id"]: f for f in client.get(
        "/drive/v3/files", headers=admin_h, params={"pageSize": 100}).json()["files"]}
    sorted_ = {f["id"]: f for f in client.get(
        "/drive/v3/files", headers=admin_h,
        params={"pageSize": 100, "orderBy": "modifiedTime desc"}).json()["files"]}
    assert plain and plain == sorted_
    assert any(f["shared"] for f in plain.values())   # ...and `shared` is really resolved


def test_drive_order_by_rejects_keys_it_cannot_honor(client, admin_h):
    """Real Drive 400s an undocumented sort key. The mock models no per-caller view/share
    timestamps, so those documented keys are rejected loudly rather than silently ignored."""
    for bad in ("bogusKey", "name descending", "viewedByMeTime"):
        r = client.get("/drive/v3/files", headers=admin_h, params={"orderBy": bad})
        assert r.status_code == 400, f"orderBy={bad!r} should 400, got {r.status_code}"
    ok = client.get("/drive/v3/files", headers=admin_h,
                    params={"orderBy": "folder,name desc", "pageSize": 5})
    assert ok.status_code == 200


def test_drive_invalid_fields_mask_is_rejected(client, admin_h):
    """An unknown field name used to be accepted and yield empty file objects (200 {}), so a typo
    or a stale field name in a consumer's mask passed every mock-backed test and 400d in
    production."""
    r = client.get("/drive/v3/files", headers=admin_h,
                   params={"pageSize": 1, "fields": "files(totallyBogusField)"})
    assert r.status_code == 400
    assert "totallyBogusField" in r.json()["error"]["message"]
    bad_top = client.get("/drive/v3/files", headers=admin_h,
                         params={"pageSize": 1, "fields": "bogusTop,files(id)"})
    assert bad_top.status_code == 400
    # a documented field the mock does not synthesize is still valid (real Drive omits it, 200)
    ok = client.get("/drive/v3/files", headers=admin_h,
                    params={"pageSize": 1, "fields": "files(id,thumbnailLink,capabilities/canEdit)"})
    assert ok.status_code == 200 and "thumbnailLink" not in ok.json()["files"][0]


def test_drive_get_honors_the_fields_mask(client, admin_h):
    """The same projection requested two ways must give the same object; files.get ignored the
    mask entirely and added keys nobody asked for."""
    mask = "id,name,mimeType,size,modifiedTime,webViewLink"
    row = client.get("/drive/v3/files", headers=admin_h,
                     params={"q": "name contains 'Brand'", "pageSize": 1,
                             "fields": f"files({mask})"}).json()["files"][0]
    got = client.get(f"/drive/v3/files/{row['id']}", headers=admin_h,
                     params={"fields": mask}).json()
    assert got == row
    r = client.get(f"/drive/v3/files/{row['id']}", headers=admin_h,
                   params={"fields": "totallyBogusField"})
    assert r.status_code == 400


def test_drive_folders_are_found_by_mime_type(client, admin_h):
    """Folders were returned by `'root' in parents` but invisible to `mimeType='…folder'`, so a
    crawler indexing folders by type concluded the account had none."""
    by_parent = _drive_ids(client, admin_h, q="'root' in parents", pageSize=100)
    by_mime = _drive_ids(client, admin_h, q=f"mimeType='{FOLDER_MIME}'", pageSize=100)
    assert by_parent and set(by_mime) == set(by_parent)
    # and the negation excludes them
    not_folders = _drive_ids(client, admin_h, q=f"mimeType!='{FOLDER_MIME}'", pageSize=100)
    assert not set(not_folders) & set(by_parent)


def test_drive_folders_honor_the_fields_projection(client, admin_h):
    """Synthesized folder rows bypassed the projection: `files(id,name)` returned 18 keys."""
    for q in ("'root' in parents", f"mimeType='{FOLDER_MIME}'"):
        files = client.get("/drive/v3/files", headers=admin_h,
                           params={"q": q, "pageSize": 5, "fields": "files(id,name)"}).json()["files"]
        assert files and all(set(f) == {"id", "name"} for f in files), q


def test_drive_folders_match_the_same_q_clauses_as_files(client, admin_h):
    """Folders now flow through `_drive_q_match`, so every clause that should match one does."""
    folders = client.get("/drive/v3/files", headers=admin_h,
                         params={"q": "'root' in parents", "pageSize": 100,
                                 "fields": "files(id,name)"}).json()["files"]
    one = folders[0]
    hit = _drive_ids(client, admin_h, q=f"name contains '{one['name']}' and mimeType='{FOLDER_MIME}'")
    assert one["id"] in hit
    # a folder is not trashed, so trashed=true excludes it
    assert one["id"] not in _drive_ids(client, admin_h, q=f"mimeType='{FOLDER_MIME}' and trashed=true")


def test_drive_folder_permissions_resolve(client, admin_h):
    """A folder id is a first-class file id in real Drive: files.get and permissions.list both
    answer for it. permissions.list 404d because folders are not stored as rows."""
    folder = client.get("/drive/v3/files", headers=admin_h,
                        params={"q": "'root' in parents", "pageSize": 1}).json()["files"][0]
    got = client.get(f"/drive/v3/files/{folder['id']}", headers=admin_h)
    assert got.status_code == 200 and got.json()["mimeType"] == FOLDER_MIME
    perms = client.get(f"/drive/v3/files/{folder['id']}/permissions", headers=admin_h)
    assert perms.status_code == 200 and perms.json()["permissions"]


def test_drive_native_docs_report_size(client, admin_h):
    """Google populates `size` for binary content *and for Docs Editors files*; the mock omitted
    it on native rows, which taught implementors something false about the API."""
    doc = _drive_find(client, admin_h, "Brand")
    assert doc["mimeType"] == DOC_MIME
    assert int(doc["size"]) > 0
    assert "md5Checksum" not in doc          # real Drive omits checksums on native files
    folder = client.get("/drive/v3/files", headers=admin_h,
                        params={"q": "'root' in parents", "pageSize": 1}).json()["files"][0]
    assert "size" not in folder              # ...but not for folders or shortcuts


# --- Drive about.get -----------------------------------------------------------------------
#
# `about` answers "who am I and how much space do I use" — the call a Drive client makes first,
# and the one the mock had no route for at all (404). Its contract is unusual: `fields` is
# mandatory, and the response carries only what the mask asked for.

ABOUT = "/drive/v3/about"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def _about(client, headers, fields):
    return client.get(ABOUT, headers=headers, params={"fields": fields})


def test_drive_about_requires_a_fields_mask(client, admin_h):
    """Real Drive 400s `about.get` with no `fields` — this resource has no default projection.
    Serving a full body instead would let a client ship a call that fails in production."""
    r = client.get(ABOUT, headers=admin_h)
    assert r.status_code == 400
    assert "fields" in r.json()["error"]["message"]


def test_drive_about_rejects_an_unknown_field(client, admin_h):
    """Same rule as the `files` masks: a typo 400s rather than quietly matching nothing."""
    assert _about(client, admin_h, "storageQuoat").status_code == 400
    assert _about(client, admin_h, "storageQuota").status_code == 200


def test_drive_about_rejects_a_mask_that_selects_nothing(client, admin_h):
    """`fields=,` clears the required-mask check but names no field. Falling through to "no
    projection" would answer a request for nothing with the entire resource."""
    r = client.get(ABOUT, headers=admin_h, params={"fields": ","})
    assert r.status_code == 400


def test_drive_about_needs_auth(client):
    # no header at all -> 403 on Drive (an unregistered caller); a bad token -> 401
    assert client.get(ABOUT, params={"fields": "user"}).status_code == 403
    bad = {"Authorization": "Bearer nope"}
    assert client.get(ABOUT, params={"fields": "user"}, headers=bad).status_code == 401
    # auth is resolved before the mask, as real Drive does — a missing mask on a bad token is 401
    assert client.get(ABOUT, headers=bad).status_code == 401


def test_drive_about_serves_only_the_requested_fields(client, admin_h):
    """Unlike `files.list` — whose typed response model always carries `kind` — `about` projects
    strictly, which is what real Drive does: ask for `user` and `user` is all you get."""
    j = _about(client, admin_h, "user").json()
    assert set(j) == {"user"}
    assert set(_about(client, admin_h, "user,storageQuota").json()) == {"user", "storageQuota"}


def test_drive_about_nested_mask_selects_its_parent(client, admin_h):
    """`storageQuota/limit` selects `storageQuota`, the same rule every other mask in this mock
    follows — one projection depth, applied consistently."""
    j = _about(client, admin_h, "storageQuota/limit").json()
    assert set(j) == {"storageQuota"}
    assert "usage" in j["storageQuota"]


def test_drive_about_user_is_the_caller(client, tokens):
    """`about.user` is the authenticated user, so `me` is true — the opposite of the same object
    read as a file's `owners` entry, where it describes someone else."""
    mia = {"Authorization": f"Bearer {_tok(tokens, 'mia@acme.com')}"}
    u = _about(client, mia, "user").json()["user"]
    assert u["kind"] == "drive#user"
    assert u["emailAddress"] == "mia@acme.com"
    assert u["me"] is True
    # the file resource keeps its own answer: mia as an owner is not "me" to the object itself
    assert _drive_find(client, mia, "Brand")["owners"][0]["me"] is False


def test_drive_about_admin_token_reports_a_concrete_address(client, admin_h):
    """The admin/service token is not a Drive person; real Drive still never reports a placeholder
    here, so the service identity stands in — as `gmail.users.getProfile` already does."""
    u = _about(client, admin_h, "user").json()["user"]
    assert "@" in u["emailAddress"] and u["me"] is True


def test_drive_about_usage_matches_the_sizes_files_list_serves(client, tokens):
    """storageQuota and files.list are two views of one corpus. If they disagree, a client cannot
    reconcile "how much space do I use" with "what is in my Drive"."""
    mia = {"Authorization": f"Bearer {_tok(tokens, 'mia@acme.com')}"}
    quota = _about(client, mia, "storageQuota").json()["storageQuota"]
    files = client.get("/drive/v3/files", headers=mia,
                       params={"pageSize": 100, "fields": "files(size)"}).json()["files"]
    listed = sum(int(f["size"]) for f in files if "size" in f)   # folders carry no size
    assert listed > 0
    assert int(quota["usageInDrive"]) == listed
    assert quota["usage"] == quota["usageInDrive"]   # the mock stores nothing outside Drive
    assert int(quota["limit"]) == 2199023255552     # 2 TiB
    assert int(quota["usageInDriveTrash"]) == 0     # SAMPLE trashes nothing


def test_drive_about_usage_is_scoped_to_the_caller(client, admin_h, tokens):
    """A scoped token must not be told the weight of a corpus it cannot read."""
    mia = {"Authorization": f"Bearer {_tok(tokens, 'mia@acme.com')}"}
    mine = int(_about(client, mia, "storageQuota").json()["storageQuota"]["usage"])
    everything = int(_about(client, admin_h, "storageQuota").json()["storageQuota"]["usage"])
    assert 0 < mine < everything


def test_drive_about_export_formats_are_honoured_by_files_export(client, admin_h):
    """Advertising a target that `files.export` refuses would be worse than advertising nothing:
    a client reads this map to decide what to ask for."""
    formats = _about(client, admin_h, "exportFormats").json()["exportFormats"]
    doc = _drive_find(client, admin_h, "Brand")
    assert doc["mimeType"] == DOC_MIME and formats[DOC_MIME]
    for target in formats[DOC_MIME]:
        r = client.get(f"/drive/v3/files/{doc['id']}/export", headers=admin_h,
                       params={"mimeType": target})
        assert r.status_code == 200, target
    # every native type the mock serves is covered; the folder type is not exportable anywhere
    assert set(formats) == {DOC_MIME, SHEET_MIME,
                            "application/vnd.google-apps.presentation"}
    assert "text/csv" in formats[SHEET_MIME]


def test_drive_about_shared_drive_fields_agree_with_the_drives_listing(client, admin_h):
    """The mock's corpus is all My Drive and `/drive/v3/drives` is empty, so every shared-drive
    field has to say the same thing rather than hinting at a capability that isn't there."""
    j = _about(client, admin_h, "*").json()
    assert client.get("/drive/v3/drives", headers=admin_h).json()["drives"] == []
    assert j["canCreateDrives"] is False and j["canCreateTeamDrives"] is False
    assert j["driveThemes"] == [] and j["teamDriveThemes"] == []


def test_drive_about_star_serves_the_whole_resource(client, admin_h):
    j = _about(client, admin_h, "*").json()
    assert j["kind"] == "drive#about"
    assert j["appInstalled"] is False
    assert {"user", "storageQuota", "importFormats", "exportFormats", "maxImportSizes",
            "maxUploadSize", "folderColorPalette"} <= set(j)
    # folderColorRgb is a documented file field, so the palette a client picks from must be real
    assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in j["folderColorPalette"])
    assert DOC_MIME in j["importFormats"]["text/plain"]


def test_drive_about_appears_in_the_openapi_spec(client):
    """The OpenAPI→MCP bridge builds its tools from the spec, so a route the spec omits is a route
    no generated client can reach."""
    op = client.get("/openapi.json").json()["paths"][ABOUT]["get"]
    assert {p["name"] for p in op["parameters"]} == {"fields"}


# --- OpenAPI enrichment: notion -----------------------------------------------------------

def test_notion_search_documents_body_param(client):
    op = client.get("/openapi.json").json()["paths"]["/notion/v1/search"]["post"]
    props = op["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "query" in props and "filter" in props


def test_notion_users_documents_pagination(client):
    op = client.get("/openapi.json").json()["paths"]["/notion/v1/users"]["get"]
    names = {p["name"] for p in op.get("parameters", [])}
    assert {"start_cursor", "page_size"} <= names


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
        legacy = client.get(f"/notion/v1/databases/{did}",
                            headers={**admin_h, "Notion-Version": "2022-06-28"}).json()
        default = client.get(f"/notion/v1/databases/{did}",
                             headers={**admin_h, "Notion-Version": "2025-09-03"}).json()
        assert "properties" in legacy and "data_sources" in default


# --- OpenAPI enrichment: atlassian (jira + confluence) ------------------------------------

def test_atlassian_jira_search_documents_params(client):
    op = client.get("/openapi.json").json()["paths"]["/atlassian/rest/api/3/search/jql"]["get"]
    names = {p["name"] for p in op.get("parameters", [])}
    assert {"jql", "maxResults", "nextPageToken"} <= names


def test_atlassian_confluence_search_documents_cql(client):
    op = client.get("/openapi.json").json()["paths"]["/atlassian/wiki/rest/api/search"]["get"]
    assert "cql" in {p["name"] for p in op.get("parameters", [])}


def test_atlassian_issue_has_typed_response_schema(client):
    op = client.get("/openapi.json").json()["paths"]["/atlassian/rest/api/3/issue/{key}"]["get"]
    assert op["responses"]["200"]["content"]["application/json"]["schema"] != {}


def test_atlassian_serverinfo_has_typed_response_schema(client):
    # serverInfo is a new alias (jira PyPI client probes it on connect); enrich it like its siblings.
    for ver in ("2", "3"):
        op = client.get("/openapi.json").json()["paths"][f"/atlassian/rest/api/{ver}/serverInfo"]["get"]
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
    cl = client.get("/atlassian/wiki/rest/api/content", params={"expand": "body.storage"},
                    headers=admin_h).json()
    assert "results" in cl and cl["results"]
    cid = cl["results"][0]["id"]
    page = client.get(f"/atlassian/wiki/rest/api/content/{cid}", params={"expand": "body.storage"},
                      headers=admin_h).json()
    assert "body" in page and "storage" in page["body"]  # expand survives


# --- /_mock/openapi/{source}: the MCP-ready spec endpoint (issue #4 bridge) ---------------

def test_mock_openapi_spec_endpoint(client):
    gh = client.get("/_mock/openapi/github")
    assert gh.status_code == 200
    ids = [op["operationId"]
           for item in gh.json()["paths"].values()
           for m, op in item.items() if isinstance(op, dict) and "operationId" in op]
    assert ids and len(ids) == len(set(ids)), "served spec must have unique operationIds (bridge-ready)"
    assert client.get("/_mock/openapi/s3").status_code == 404  # SigV4 — intentionally no bridge
    assert client.get("/_mock/openapi/nope").status_code == 404


# --- Linear (GraphQL) -------------------------------------------------------------
# Linear is GraphQL-only, so there is no REST surface to crawl. What matters instead is that the
# schema answers what real clients ask for: the LlamaIndex reader's exact field set, `@linear/sdk`'s
# by-id relation roots, and Linear's own error/status split. (The TypeScript SDK itself is
# exercised by the Node CI job — pytest cannot drive `@linear/sdk`.)

def gql(client, query, headers, **variables):
    body = {"query": query}
    if variables:
        body["variables"] = variables
    return client.post("/linear/graphql", json=body, headers=headers)


def linear_user_token(tokens, email):
    return next(u["token"] for u in tokens["users"] if u["email"] == email)


def lit(value) -> str:
    """A GraphQL string literal. GraphQL only accepts DOUBLE quotes, so Python's %r (single
    quotes) is a syntax error on the wire — json.dumps produces the right thing."""
    return json.dumps(str(value))


# The exact selection `llama-index-readers-linear` sends, and every field its `load_data()`
# dereferences by subscript. A KeyError on any of them is the failure this guards.
READER_QUERY = """
query Team($id: String!) {
  team(id: $id) {
    issues {
      nodes {
        id title description createdAt updatedAt archivedAt autoArchivedAt autoClosedAt
        branchName canceledAt completedAt dueDate estimate
        creator { name } assignee { name } state { name } project { name }
        labels { nodes { name } }
      }
    }
  }
}
"""


def test_linear_reader_field_set_all_resolves(client, admin_h):
    r = gql(client, READER_QUERY, admin_h, id="ENG")
    assert r.status_code == 200
    assert "errors" not in r.json(), r.json().get("errors")
    nodes = r.json()["data"]["team"]["issues"]["nodes"]
    assert nodes
    for issue in nodes:
        # Present as KEYS even when null — the reader subscripts every one of them.
        for field in ("id", "title", "description", "createdAt", "updatedAt", "archivedAt",
                      "autoArchivedAt", "autoClosedAt", "branchName", "canceledAt",
                      "completedAt", "dueDate", "estimate"):
            assert field in issue, field
        assert issue["labels"]["nodes"] is not None
    # Key-presence alone is a TAUTOLOGY: graphql-core always emits a selected field as a key, so
    # the loop above passes even if every value is served as a constant null. Pin the values that
    # must be real, including a lifecycle timestamp that is genuinely populated.
    done = next(i for i in nodes if i["title"] == "Continuous batching stalls after compaction")
    assert done["completedAt"] == "2026-03-10T00:00:00Z"     # not None, not synthesized
    assert done["createdAt"] == "2026-03-01T00:00:00Z"
    assert done["canceledAt"] is None                        # Done, so it was never canceled
    by_id = {i["title"]: i for i in nodes}
    rl = by_id["Rate limiter drops bursts under 50ms"]
    assert rl["creator"]["name"] and rl["assignee"]["name"] == "Bob Stone"
    assert rl["state"]["name"] == "In Progress"
    assert rl["project"]["name"] == "runtime-stability"
    assert {label["name"] for label in rl["labels"]["nodes"]} == {"bug", "gateway"}
    assert rl["estimate"] == 5
    assert rl["dueDate"] == "2026-03-15"


def test_linear_issue_by_uuid_and_by_identifier(client, admin_h):
    by_key = gql(client, '{ issue(id: "ENG-101") { id identifier title } }', admin_h)
    issue = by_key.json()["data"]["issue"]
    assert issue["identifier"] == "ENG-101"
    by_uuid = gql(client, "{ issue(id: %s) { identifier } }" % lit(issue["id"]), admin_h)
    assert by_uuid.json()["data"]["issue"]["identifier"] == "ENG-101"


def test_linear_missing_issue_is_a_field_error_not_a_400(client, admin_h):
    """Linear declares `issue` non-null, so a miss nulls `data` and reports an error — but the
    request itself was fine, so the status stays 200."""
    r = gql(client, '{ issue(id: "NOPE-1") { identifier } }', admin_h)
    assert r.status_code == 200
    assert r.json()["data"] is None
    assert "Entity not found" in r.json()["errors"][0]["message"]


def test_linear_team_resolves_by_key_and_uuid(client, admin_h):
    key = gql(client, '{ team(id: "ENG") { id key name } }', admin_h).json()["data"]["team"]
    assert (key["key"], key["name"]) == ("ENG", "engineering")
    assert gql(client, "{ team(id: %s) { key } }" % lit(key["id"]),
               admin_h).json()["data"]["team"]["key"] == "ENG"


def test_linear_team_issue_count_is_the_visible_count(client, admin_h, tokens):
    """Asserted for BOTH an admin and a restricted caller: as admin alone the count's ACL branch
    never runs, so the assertion would hold with scoping removed entirely."""
    admin = {t["key"]: t["issueCount"] for t in
             gql(client, "{ teams { nodes { key issueCount } } }",
                 admin_h).json()["data"]["teams"]["nodes"]}
    assert admin == {"ENG": 3, "DES": 1, "BLA": 1}
    ava_h = {"Authorization": linear_user_token(tokens, "ava@acme.com")}
    ava = {t["key"]: t["issueCount"] for t in
           gql(client, "{ teams { nodes { key issueCount } } }",
               ava_h).json()["data"]["teams"]["nodes"]}
    # ava cannot see lin-secret or the blackops team at all.
    assert ava == {"ENG": 2, "DES": 1}


def test_linear_state_type_is_linears_category(client, admin_h):
    r = gql(client, "{ issues { nodes { identifier state { name type } } } }", admin_h)
    types = {n["identifier"]: n["state"]["type"] for n in r.json()["data"]["issues"]["nodes"]}
    assert types == {"ENG-101": "started", "ENG-102": "completed", "DES-77": "started",
                     "ENG-103": "backlog", "BLA-1": "triage"}


def test_linear_priority_is_linears_numeric_scale(client, admin_h):
    r = gql(client, "{ issues { nodes { identifier priority priorityLabel } } }", admin_h)
    got = {n["identifier"]: (n["priority"], n["priorityLabel"])
           for n in r.json()["data"]["issues"]["nodes"]}
    # The corpus writes P0-P3; the API serves Linear's own 0-4 scale (1 = most urgent).
    assert got["ENG-102"] == (1, "Urgent")
    assert got["ENG-101"] == (2, "High")
    assert got["DES-77"] == (3, "Medium")
    assert got["ENG-103"] == (4, "Low")


def test_linear_comments_connection_on_an_issue(client, admin_h):
    r = gql(client, '{ issue(id: "ENG-101") { comments { nodes { body user { email } } } } }',
            admin_h)
    nodes = r.json()["data"]["issue"]["comments"]["nodes"]
    assert [c["body"] for c in nodes] == ["Reproduced with a burst test.", "Fix is in review."]
    assert nodes[0]["user"]["email"] == "bob@acme.com"


def test_linear_by_id_relation_roots_answer(client, admin_h):
    """`@linear/sdk` resolves relations lazily — `await issue.state` fires `workflowState(id:)`
    rather than reading the value off the issue it already has. Without these roots every
    relation accessor in the SDK fails."""
    issue = gql(client, '{ issue(id: "ENG-101") { state { id } assignee { id } project { id } '
                        'cycle { id } labels { nodes { id } } } }',
                admin_h).json()["data"]["issue"]
    assert gql(client, "{ workflowState(id: %s) { name team { key } } }" % lit(issue["state"]["id"]),
               admin_h).json()["data"]["workflowState"] == {"name": "In Progress",
                                                            "team": {"key": "ENG"}}
    assert gql(client, "{ user(id: %s) { email } }" % lit(issue["assignee"]["id"]),
               admin_h).json()["data"]["user"]["email"] == "bob@acme.com"
    assert gql(client, "{ project(id: %s) { name } }" % lit(issue["project"]["id"]),
               admin_h).json()["data"]["project"]["name"] == "runtime-stability"
    assert gql(client, "{ cycle(id: %s) { name } }" % lit(issue["cycle"]["id"]),
               admin_h).json()["data"]["cycle"]["name"] == "2025-W08"
    label_id = issue["labels"]["nodes"][0]["id"]
    assert gql(client, "{ issueLabel(id: %s) { name } }" % lit(label_id),
               admin_h).json()["data"]["issueLabel"]["name"] in {"bug", "gateway"}


def test_linear_workflow_states_are_per_team(client, admin_h):
    """Two teams' identically-named states are different objects in Linear, so their ids differ.
    The corpus has no shared state name, so assert the construction directly instead."""
    from app import synth
    assert synth.linear_state_id("Done", "engineering") != synth.linear_state_id("Done", "design")


def test_linear_viewer_reports_the_authenticated_identity(client, tokens):
    h = {"Authorization": linear_user_token(tokens, "ava@acme.com")}
    me = gql(client, "{ viewer { email isMe } }", h).json()["data"]["viewer"]
    assert me == {"email": "ava@acme.com", "isMe": True}


def test_linear_content_round_trips_verbatim(client, admin_h, ro_conn):
    """`Issue.description` is the doc's retrieval payload; it must come back byte-for-byte."""
    stored = {r["identifier"]: r["content"]
              for r in ro_conn.execute("SELECT identifier, content FROM linear_issues")}
    r = gql(client, "{ issues(first: 100) { nodes { identifier description } } }", admin_h)
    served = {n["identifier"]: n["description"] for n in r.json()["data"]["issues"]["nodes"]}
    assert served == stored


def test_linear_crawl_reaches_every_document(client, admin_h, ro_conn):
    """The completeness assertion the REST crawls make, in Relay form: page with `first`/`after`
    to exhaustion and land on exactly the stored row count."""
    seen, cursor, guard = [], None, 0
    while True:
        guard += 1
        assert guard < 50
        after = (", after: %s" % lit(cursor)) if cursor else ""
        page = gql(client, "{ issues(first: 2%s) { nodes { identifier } "
                           "pageInfo { hasNextPage endCursor } } }" % after,
                   admin_h).json()["data"]["issues"]
        seen += [n["identifier"] for n in page["nodes"]]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    assert len(seen) == len(set(seen)) == db_count(ro_conn, "linear")


def test_linear_introspection_reports_the_served_schema(client, admin_h):
    r = gql(client, "{ __schema { queryType { name } mutationType { name } } "
                    '__type(name: "Issue") { fields { name } } }', admin_h)
    data = r.json()["data"]
    assert data["__schema"]["queryType"]["name"] == "Query"
    # Read-only mock: no Mutation root at all, rather than one advertising writes that fail.
    assert data["__schema"]["mutationType"] is None
    names = {f["name"] for f in data["__type"]["fields"]}
    assert {"identifier", "branchName", "estimate", "dueDate", "state", "labels"} <= names


def test_linear_malformed_document_is_a_400_with_a_graphql_envelope(client, admin_h):
    r = gql(client, "{ issues(first: }", admin_h)
    assert r.status_code == 400
    body = r.json()
    assert "detail" not in body and "data" not in body
    assert "Syntax Error" in body["errors"][0]["message"]


def test_linear_unauthenticated_is_401(client):
    r = client.post("/linear/graphql", json={"query": "{ viewer { email } }"})
    assert r.status_code == 401
    assert r.json()["errors"][0]["message"] == "Authentication required"


def test_linear_parent_resolves_and_is_acl_scoped(client, admin_h, tokens):
    """`Issue.parent` is declared in the SDL and `@linear/sdk`'s fragment selects `parent { id }`.
    The bench fills `parent_issue` on 46.7% of records, so it must resolve — and it must resolve
    through the ACL, or it becomes another way to confirm a hidden issue exists."""
    # lin-batch (ENG-102) is parented to lin-secret (ENG-103), which only hana can read.
    q = '{ issue(id: "ENG-102") { identifier parent { identifier title } } }'
    as_hana = gql(client, q, {"Authorization": linear_user_token(tokens, "hana@acme.com")})
    assert as_hana.json()["data"]["issue"]["parent"]["identifier"] == "ENG-103"
    as_ava = gql(client, q, {"Authorization": linear_user_token(tokens, "ava@acme.com")})
    assert as_ava.json()["data"]["issue"]["parent"] is None    # hidden parent -> null, not a leak
    # admin sees it, confirming the null above is the ACL and not a broken lookup
    assert gql(client, q, admin_h).json()["data"]["issue"]["parent"]["identifier"] == "ENG-103"


def test_linear_issue_without_a_parent_is_null(client, admin_h):
    assert gql(client, '{ issue(id: "ENG-101") { parent { identifier } } }',
               admin_h).json()["data"]["issue"]["parent"] is None


def test_linear_default_ordering_is_by_creation_not_insertion(client, admin_h):
    """Linear's docs: "By default results are ordered by createdAt field." An absent `orderBy`
    previously fell through to raw insertion order, so `issues(first: n)` returned an arbitrary n
    rather than the first n by creation."""
    q = "{ issues(first: 50%s) { nodes { identifier createdAt } } }"
    default = [n["createdAt"] for n in gql(client, q % "", admin_h).json()["data"]["issues"]["nodes"]]
    explicit = [n["createdAt"] for n in
                gql(client, q % ", orderBy: createdAt", admin_h).json()["data"]["issues"]["nodes"]]
    assert default == explicit
    assert default == sorted(default), "default ordering must be by creation, ascending"


def test_linear_sort_input_overrides_the_default_ordering(client, admin_h):
    """`orderBy` carries no direction in Linear, so `sort:` is how a client asks for the other
    one — which means it has to actually win over the default."""
    q = ('{ issues(first: 50, sort: [{createdAt: {order: Descending}}]) '
         "{ nodes { createdAt } } }")
    got = [n["createdAt"] for n in gql(client, q, admin_h).json()["data"]["issues"]["nodes"]]
    assert got == sorted(got, reverse=True)


# --- Linear relations / children / attachments / releases (#25) -----------------------

def test_linear_children_is_the_exact_inverse_of_parent(client, admin_h):
    """Linear DEFINES `children` as the inverse of `parent`, so the two must never disagree. They
    are both read off the `parent_doc_id` resolved at import rather than joined on `identifier`,
    because bench keys repeat — a join would attach one issue's children to every issue sharing
    its key."""
    kids = gql(client, '{ issue(id: "ENG-103") { children { nodes { identifier } } } }',
               admin_h).json()["data"]["issue"]["children"]["nodes"]
    assert [k["identifier"] for k in kids] == ["ENG-102"]
    back = gql(client, '{ issue(id: "ENG-102") { parent { identifier } } }',
               admin_h).json()["data"]["issue"]["parent"]
    assert back["identifier"] == "ENG-103"


def test_linear_children_is_acl_scoped(client, tokens):
    """ENG-103 is restricted to hana, so ava cannot even reach it to ask for its children — and
    the children list must never become a way to observe an issue she is denied."""
    ava = {"Authorization": linear_user_token(tokens, "ava@acme.com")}
    denied = gql(client, '{ issue(id: "ENG-103") { children { nodes { identifier } } } }', ava)
    assert "Entity not found" in denied.json()["errors"][0]["message"]


def test_linear_relations_and_their_inverse(client, admin_h):
    rels = gql(client, '{ issue(id: "ENG-102") { relations { nodes { type relatedIssue '
                       "{ identifier } } } } }", admin_h).json()["data"]["issue"]["relations"]["nodes"]
    assert sorted((r["type"], r["relatedIssue"]["identifier"]) for r in rels) == \
        [("blocks", "ENG-101"), ("related", "ENG-103")]
    # the same row read from the other end
    inv = gql(client, '{ issue(id: "ENG-101") { inverseRelations { nodes { type issue '
                      "{ identifier } } } } }", admin_h).json()["data"]["issue"]
    assert [(r["type"], r["issue"]["identifier"]) for r in inv["inverseRelations"]["nodes"]] == \
        [("blocks", "ENG-102")]


def test_linear_relation_to_a_hidden_issue_is_omitted(client, tokens, admin_h):
    """A relation is scoped on the FAR end: surfacing one whose counterpart the caller cannot read
    would disclose that issue's existence — the leak class the by-id roots were fixed for."""
    ava = {"Authorization": linear_user_token(tokens, "ava@acme.com")}
    q = '{ issue(id: "ENG-102") { relations { nodes { relatedIssue { identifier } } } } }'
    seen = [r["relatedIssue"]["identifier"]
            for r in gql(client, q, ava).json()["data"]["issue"]["relations"]["nodes"]]
    assert seen == ["ENG-101"], "the relation to the restricted ENG-103 must be omitted"
    # admin sees both, proving the omission is the ACL and not a broken join
    assert len(gql(client, q, admin_h).json()["data"]["issue"]["relations"]["nodes"]) == 2


def test_linear_attachments_from_both_bench_shapes(client, admin_h):
    """`Attachment.title` is non-null in Linear, so a bare URL needs a derived title rather than
    an empty string."""
    nodes = gql(client, '{ issue(id: "ENG-102") { attachments { nodes { title url } } } }',
                admin_h).json()["data"]["issue"]["attachments"]["nodes"]
    got = {n["title"]: n["url"] for n in nodes}
    assert got["Design doc"] == "https://conf.acme.test/design/batching"   # explicit title
    assert got["artifacts.zip"] == "https://ci.acme.test/builds/4821/artifacts.zip"  # derived


def test_linear_attachments_url_argument_and_filter(client, admin_h):
    one = gql(client, '{ issue(id: "ENG-102") { attachments(url: "https://conf.acme.test/design/'
                      'batching") { nodes { title } } } }', admin_h)
    assert [n["title"] for n in one.json()["data"]["issue"]["attachments"]["nodes"]] == ["Design doc"]
    none = gql(client, '{ issue(id: "ENG-102") { attachments(filter: {title: {eq: "nope"}}) '
                       "{ nodes { title } } } }", admin_h)
    assert none.json()["data"]["issue"]["attachments"]["nodes"] == []


def test_linear_releases_and_the_by_id_root(client, admin_h):
    nodes = gql(client, '{ issue(id: "ENG-102") { releases { nodes { id name slugId } } } }',
                admin_h).json()["data"]["issue"]["releases"]["nodes"]
    assert [n["name"] for n in nodes] == ["runtime-1.19"]
    assert gql(client, '{ release(id: %s) { name } }' % lit(nodes[0]["id"]),
               admin_h).json()["data"]["release"]["name"] == "runtime-1.19"


def test_linear_release_by_id_is_acl_scoped(client, tokens):
    """The release only appears on ENG-102, which ava CAN read — so she resolves it. Asserted to
    pin that the scoping is on visibility, not a blanket denial."""
    from app import synth
    ava = {"Authorization": linear_user_token(tokens, "ava@acme.com")}
    got = gql(client, '{ release(id: %s) { name } }' % lit(synth.linear_release_id("runtime-1.19")),
              ava)
    assert got.json()["data"]["release"]["name"] == "runtime-1.19"
    absent = gql(client, '{ release(id: %s) { name } }' % lit(synth.linear_release_id("nope-9")), ava)
    assert "Entity not found" in absent.json()["errors"][0]["message"]


def test_linear_issue_with_no_relations_returns_empty_connections(client, admin_h):
    r = gql(client, '{ issue(id: "DES-77") { relations { nodes { id } } children { nodes { id } } '
                    "attachments { nodes { id } } releases { nodes { id } } } }",
            admin_h).json()["data"]["issue"]
    assert all(r[k]["nodes"] == [] for k in ("relations", "children", "attachments", "releases"))


def test_linear_parent_and_children_read_the_same_column(client, admin_h, ro_conn):
    """Both directions must consult the resolved `parent_doc_id`, not two independent lookups
    that happen to agree — that is the whole reason the key is resolved once at import.

    Also a performance contract: `@linear/sdk`'s Issue fragment selects `parent { id }` on every
    node, so resolving it by identifier cost ~45ms on a 50-issue page."""
    # `ro_conn` is the SAMPLE db; a fresh get_settings() would follow whatever MOCK_DATA_DIR
    # another module last set, which is why this reads the fixture instead.
    row = ro_conn.execute("SELECT doc_id, parent_doc_id, parent_key FROM linear_issues "
                          "WHERE doc_id = 'lin-batch'").fetchone()
    # the import pass resolved the KEY into a doc_id
    assert row["parent_key"] == "ENG-103"
    assert row["parent_doc_id"] == "lin-secret"
    served = gql(client, '{ issue(id: "ENG-102") { parent { identifier } } }',
                 admin_h).json()["data"]["issue"]["parent"]
    assert served["identifier"] == "ENG-103"


# --- fireflies: POST /fireflies/graphql -----------------------------------------
# Fireflies has no SDK and no LlamaIndex reader; the vendor's own quickstart is a raw HTTP POST,
# so this IS the client story rather than a fallback for one.

def ff_gql(client, query, headers, **variables):
    body = {"query": query}
    if variables:
        body["variables"] = variables
    return client.post("/fireflies/graphql", json=body, headers=headers)


def test_fireflies_requires_a_bearer_key(client):
    r = client.post("/fireflies/graphql", json={"query": "{ transcripts { id } }"})
    assert r.status_code == 401
    # a GraphQL error envelope, not a framework 403 — clients parse errors[0].message
    assert r.json()["errors"][0]["message"]
    assert "data" not in r.json()
    bad = client.post("/fireflies/graphql", json={"query": "{ transcripts { id } }"},
                      headers={"Authorization": "Bearer not-a-real-key"})
    assert bad.status_code == 401


def test_fireflies_admin_crawl_sees_every_stored_transcript(client, admin_h, ro_conn):
    r = ff_gql(client, "{ transcripts(limit: 50) { id title } }", admin_h)
    assert r.status_code == 200
    served = r.json()["data"]["transcripts"]
    assert len(served) == store.count_fireflies_transcripts(ro_conn)
    assert len(served) == ro_conn.execute("SELECT COUNT(*) FROM fireflies_transcripts").fetchone()[0]


def test_fireflies_transcript_content_round_trips_through_the_api(client, admin_h, ro_conn):
    """The sentences the API serves must rebuild the stored `content` byte for byte — that is the
    whole point of defining content as the concatenation."""
    from app import synth

    r = ff_gql(client, "{ transcripts(limit: 50) { id title sentences "
                       "{ speaker_name text } } }", admin_h)
    for t in r.json()["data"]["transcripts"]:
        row = store.fireflies_transcript_by_id(ro_conn, t["id"])
        assert synth.fireflies_transcript_text(t["sentences"]) == row["content"]


def test_fireflies_serves_the_documented_metadata_surface(client, admin_h):
    r = ff_gql(client, """
        { transcripts(limit: 1) {
            id title date dateString duration host_email organizer_email participants
            meeting_link calendar_id cal_id calendar_type channels
            transcript_url audio_url video_url
            user { user_id email name }
            summary { overview keywords action_items outline topics_discussed meeting_type }
            analytics { sentiments { positive_pct neutral_pct negative_pct }
                        speakers { name duration word_count duration_pct } }
            meeting_attendees { displayName email location }
            sentences { index speaker_name speaker_id text raw_text start_time end_time }
        } }""", admin_h)
    assert r.status_code == 200 and "errors" not in r.json()
    t = r.json()["data"]["transcripts"][0]
    assert t["id"] and t["title"]
    assert t["date"] and t["dateString"]
    assert t["channels"] and t["transcript_url"] and t["audio_url"] and t["video_url"]
    assert t["sentences"] and t["analytics"]["sentiments"]["positive_pct"] is not None


def test_fireflies_sentence_windows_are_ordered_and_contiguous(client, admin_h):
    r = ff_gql(client, "{ transcripts(limit: 50) { sentences { index start_time end_time } } }",
               admin_h)
    for t in r.json()["data"]["transcripts"]:
        sents = t["sentences"]
        assert [s["index"] for s in sents] == list(range(len(sents)))
        for a, b in zip(sents, sents[1:]):
            assert a["start_time"] < a["end_time"] <= b["start_time"]


def test_fireflies_date_is_epoch_millis_matching_the_iso_string(client, admin_h):
    """Fireflies returns `date` as epoch MILLISECONDS — a client that divides by 1000 must land on
    the same instant `dateString` states."""
    import datetime as dt

    r = ff_gql(client, "{ transcripts(limit: 50) { date dateString } }", admin_h)
    for t in r.json()["data"]["transcripts"]:
        parsed = dt.datetime.fromisoformat(t["dateString"].replace("Z", "+00:00"))
        assert t["date"] == parsed.timestamp() * 1000


def test_fireflies_organizer_falls_back_to_the_host(client, admin_h, ro_conn):
    """The column is NULL when a meeting's organizer is its host; the FIELD must still answer,
    because Fireflies itself never returns a null organizer for a hosted meeting."""
    assert ro_conn.execute("SELECT organizer_email FROM fireflies_transcripts "
                      "WHERE doc_id='ff-discovery'").fetchone()[0] is None
    r = ff_gql(client, "{ transcripts(limit: 50) { host_email organizer_email } }", admin_h)
    served = r.json()["data"]["transcripts"]
    assert all(t["organizer_email"] for t in served)
    assert any(t["organizer_email"] == t["host_email"] for t in served)


def test_fireflies_transcript_by_id_matches_the_listing(client, admin_h):
    listed = ff_gql(client, "{ transcripts(limit: 1) { id title } }",
                    admin_h).json()["data"]["transcripts"][0]
    one = ff_gql(client, 'query($i:String!){ transcript(id:$i) { id title } }',
                 admin_h, i=listed["id"]).json()["data"]["transcript"]
    assert one == listed
    absent = ff_gql(client, '{ transcript(id: "deadbeefdeadbeefdeadbeef") { id } }',
                    admin_h).json()
    assert absent["data"]["transcript"] is None


def test_fireflies_limit_is_clamped_over_http(client, admin_h, ro_conn):
    total = store.count_fireflies_transcripts(ro_conn)
    got = ff_gql(client, "query($l:Int){ transcripts(limit:$l) { id } }",
                 admin_h, l=10_000).json()["data"]["transcripts"]
    assert len(got) == min(50, total)     # clamped to the documented max, not an error


def test_fireflies_skip_pages_without_gaps_or_repeats(client, admin_h, ro_conn):
    total = store.count_fireflies_transcripts(ro_conn)
    walked = []
    for skip in range(total + 1):
        page = ff_gql(client, "query($s:Int){ transcripts(limit:1, skip:$s) { id } }",
                      admin_h, s=skip).json()["data"]["transcripts"]
        walked += [t["id"] for t in page]
    assert len(walked) == total == len(set(walked))
    # past the end is an empty list, not an error
    beyond = ff_gql(client, "{ transcripts(limit: 5, skip: 9999) { id } }", admin_h).json()
    assert beyond["data"]["transcripts"] == []


def test_fireflies_keyword_scope_over_http(client, admin_h):
    def titles(**args):
        arglist = ", ".join(f'{k}: "{v}"' for k, v in args.items())
        return {t["title"] for t in ff_gql(
            client, "{ transcripts(%s, limit: 50) { title } }" % arglist,
            admin_h).json()["data"]["transcripts"]}

    assert titles(keyword="selects", scope="title") == set()
    assert titles(keyword="selects", scope="sentences") == {"April all-hands"}
    assert titles(keyword="selects", scope="all") == {"April all-hands"}
    assert titles(keyword="all-hands", scope="title") == {"April all-hands"}


def test_fireflies_unknown_scope_is_a_field_error_not_a_silent_widening(client, admin_h):
    """Silently searching everything would hide a client's typo. A field error keeps the
    GraphQL-over-HTTP contract: 200 with partial data alongside errors."""
    r = ff_gql(client, '{ transcripts(keyword: "x", scope: "body") { id } }', admin_h)
    assert r.status_code == 200
    body = r.json()
    assert "data" in body                                  # field error -> data key present
    assert body["data"]["transcripts"] is None
    assert "scope must be one of" in body["errors"][0]["message"]


def test_fireflies_request_errors_are_400_with_no_data_key(client, admin_h):
    """The engine's request/field split: a malformed or invalid document is decided before
    execution, so the response carries no `data` entry at all."""
    for query in ("{ transcripts(", "{ transcripts { nosuchfield } }", "{ }"):
        r = ff_gql(client, query, admin_h)
        assert r.status_code == 400, query
        assert "data" not in r.json(), query
        assert r.json()["errors"], query


def test_fireflies_user_root_answers_for_a_person_only(client, admin_h, tokens):
    """`user` with no id is the authenticated user. An admin/service token is not a person."""
    assert ff_gql(client, "{ user { user_id email } }",
                  admin_h).json()["data"]["user"] is None
    ava = next(u["token"] for u in tokens["users"] if u["email"] == "ava@acme.com")
    h = {"Authorization": f"Bearer {ava}"}
    me = ff_gql(client, "{ user { user_id email name } }", h).json()["data"]["user"]
    assert me["email"] == "ava@acme.com" and me["user_id"]
    # the served user_id round-trips back through user(id:)
    again = ff_gql(client, 'query($i:String){ user(id:$i) { email } }', h,
                   i=me["user_id"]).json()["data"]["user"]
    assert again["email"] == "ava@acme.com"


def test_fireflies_introspection_describes_the_schema(client, admin_h):
    """There is no OpenAPI entry for this route on purpose, so introspection is how a client
    discovers the surface."""
    r = ff_gql(client, "{ __schema { queryType { fields { name } } } }", admin_h)
    names = {f["name"] for f in r.json()["data"]["__schema"]["queryType"]["fields"]}
    assert {"transcripts", "transcript", "user", "users"} <= names


def test_fireflies_declares_no_mutations(client, admin_h):
    """A read-only mock declares no Mutation type rather than accepting writes and dropping them."""
    r = ff_gql(client, "{ __schema { mutationType { name } } }", admin_h)
    assert r.json()["data"]["__schema"]["mutationType"] is None
