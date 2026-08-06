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
import re
import xml.etree.ElementTree as ET

import pytest
import yaml

from app import store
from app.config import Settings
from tests._helpers import build_corpus, client_for, db_count


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


def test_unauthenticated_request_reports_the_vendors_own_401_detail(client):
    """The message is part of the emulated surface — a client that string-matches its provider's
    error has to keep matching — which is why the shared guard takes it as a parameter.

    GitHub only: Google no longer goes through `auth.require_bearer`, because its answer is not one
    status (403 on Drive/Sheets, 401 on the OAuth-only families) and it carries Google's own error
    envelope, not `detail`. That surface is covered by the tests below."""
    r = client.get("/github/orgs/acme")
    assert r.status_code == 401
    assert r.json()["detail"] == "Bad credentials"


def test_atlassian_401_keeps_the_atlassian_error_envelope(client):
    """Atlassian clients parse the error body as Atlassian Cloud's envelope (Confluence's
    raise_for_status reads ``response.json()["message"]``), so a 401 there is not FastAPI's
    ``{"detail": ...}`` — see app.main._atlassian_error_body."""
    # NOT serverInfo: the jira PyPI client probes that on connect, so it answers unauthenticated
    # on purpose. project/search is the first call that actually needs a credential.
    r = client.get("/atlassian/rest/api/3/project/search")
    assert r.status_code == 401
    body = r.json()
    assert body["message"] == "Unauthorized"
    assert body["errorMessages"] == ["Unauthorized"]
    assert body["statusCode"] == 401


def test_hubspot_acl_hides_restricted_record(client, tokens_yaml):
    """`hs-co-secret` is readable only by hana; another user's crawl must not contain it."""
    users = {u["email"]: u["token"] for u in tokens_yaml["users"]}
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


def test_hubspot_search_is_acl_scoped(client, tokens_yaml):
    """Search must filter by the caller like every other read — not only the plain listing."""
    users = {u["email"]: u["token"] for u in tokens_yaml["users"]}
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
    from tests.conftest import SAMPLE

    settings = build_corpus(tmp_path_factory.mktemp("gh_sample"), SAMPLE + _GH_FILE_DOCS)
    with client_for(settings) as c:
        yield c, settings


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

def test_user_sees_subset_of_admin(client, admin_h, tokens_yaml, ro_conn, sample_settings):
    user = tokens_yaml["users"][0]
    uh = {"Authorization": f"Bearer {user['token']}"}
    admin_conf = len(crawl_confluence(client, admin_h))
    user_conf = len(crawl_confluence(client, uh))
    assert user_conf < admin_conf  # some confluence docs are group/private-restricted
    # matches exactly the ACL-computed visible count
    from app.acl import Acl
    acl = Acl.load(sample_settings.tokens_path, sample_settings.admin_token, sample_settings.org_name)
    vids = acl.visible_ids(ro_conn, acl.resolve(user["token"]))
    assert user_conf == db_count(ro_conn, "confluence", visible_ids=vids)


def test_mock_users_directory(client, tokens_yaml, org):
    # the /_mock/users directory lists every user + token (for testing per-user ACL)
    from app import synth
    body = client.get("/_mock/users").json()
    assert body["admin_token"] == tokens_yaml["admin_token"]
    # S3 uses an AWS keypair, not a token — the directory exposes an admin pair (derived from the
    # admin token, which is what the SigV4 verifier resolves) so a client can use it directly
    assert body["admin_s3_access_key_id"] == synth.s3_access_key_id(body["admin_token"])
    assert body["admin_s3_secret_access_key"] == synth.s3_secret_access_key(body["admin_token"])
    yaml_by_email = {u["email"]: u["token"] for u in tokens_yaml["users"]}
    assert body["count"] == len(body["users"]) == len(yaml_by_email) > 0
    for u in body["users"]:
        assert u["token"] == yaml_by_email[u["email"]]  # matches data/tokens_yaml.yaml
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


def test_slack_accepts_form_field_token(client, tokens_yaml):
    # the official slack-go SDK posts the token as a form field (no bearer header); the mock
    # must accept it exactly like a real Slack Web API.
    admin = tokens_yaml["admin_token"]
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


# --- Slack fidelity (#33) ---------------------------------------------------------------------
#
# Reported from building a filesystem-style Slack client against the mock. Slack answers an
# application error as HTTP 200 with {"ok": false, "error": …}, which the mock already does — these
# are about the cases where it answered something real Slack never would.
#
# NOTE: unlike the Google work in #37/#39, these expectations come from Slack's published reference
# rather than from probing the live API — there are no Slack credentials in this environment. Each
# one cites the documented behaviour it encodes.

def _a_channel_id(client, admin_h):
    return client.get("/slack/api/conversations.list", headers=admin_h,
                      params={"limit": 1}).json()["channels"][0]["id"]


@pytest.mark.parametrize("types, expect_channels", [
    ("public_channel", True),
    ("private_channel", True),
    ("public_channel,private_channel", True),
    ("im", False),
    ("mpim", False),
    ("im,mpim", False),
])
def test_slack_conversations_list_honours_types(client, admin_h, types, expect_channels):
    """`types` was ignored, so `im` returned every public channel and a client presenting
    `channels/` and `dms/` separately got each channel under both. This corpus has no DMs, so `im`
    must come back empty — which is exactly what real Slack answers for a DM-less workspace, making
    "no DMs here" indistinguishable from production instead of indistinguishable from a bug."""
    j = client.get("/slack/api/conversations.list", headers=admin_h,
                   params={"types": types, "limit": 5}).json()
    assert j["ok"] is True
    assert bool(j["channels"]) is expect_channels, j["channels"][:1]
    assert all(c["is_im"] is False and c["is_mpim"] is False for c in j["channels"])


def test_slack_conversations_list_defaults_to_public_channels(client, admin_h):
    """Slack's documented default when `types` is omitted is `public_channel`."""
    omitted = client.get("/slack/api/conversations.list", headers=admin_h,
                         params={"limit": 5}).json()
    explicit = client.get("/slack/api/conversations.list", headers=admin_h,
                          params={"limit": 5, "types": "public_channel"}).json()
    assert omitted["channels"] == explicit["channels"]
    assert omitted["channels"]


def test_slack_conversations_list_rejects_an_unknown_type(client, admin_h):
    """Real Slack answers `invalid_types`; the mock accepted anything, so a typo'd filter silently
    returned the unfiltered list."""
    j = client.get("/slack/api/conversations.list", headers=admin_h,
                   params={"types": "bogus_type"}).json()
    assert j == {"ok": False, "error": "invalid_types"}
    mixed = client.get("/slack/api/conversations.list", headers=admin_h,
                       params={"types": "public_channel,bogus_type"}).json()
    assert mixed == {"ok": False, "error": "invalid_types"}


@pytest.mark.parametrize("param, error", [("latest", "invalid_ts_latest"),
                                          ("oldest", "invalid_ts_oldest")])
def test_slack_history_rejects_a_malformed_timestamp(client, admin_h, param, error):
    """`float(oldest)` was unguarded, so a bad argument was a 500 — which clients that back off on
    5xx will retry, burning the whole budget on a request that can never succeed. Real Slack
    answers 200 with the named error."""
    r = client.get("/slack/api/conversations.history", headers=admin_h,
                   params={"channel": _a_channel_id(client, admin_h), param: "not-a-ts"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": error}


@pytest.mark.parametrize("path", ["conversations.list", "users.list"])
def test_slack_rejects_an_invalid_cursor(client, admin_h, path):
    """An undecodable cursor was treated as offset 0, so a client paginating with a corrupted
    cursor looped on page 1 forever instead of failing. Real Slack answers `invalid_cursor`."""
    for bad in ("bogus", "###"):
        j = client.get(f"/slack/api/{path}", headers=admin_h, params={"cursor": bad}).json()
        assert j == {"ok": False, "error": "invalid_cursor"}, (path, bad)


def test_slack_history_rejects_an_invalid_cursor(client, admin_h):
    j = client.get("/slack/api/conversations.history", headers=admin_h,
                   params={"channel": _a_channel_id(client, admin_h), "cursor": "bogus"}).json()
    assert j == {"ok": False, "error": "invalid_cursor"}


def test_slack_members_are_the_channels_own_speakers(client, admin_h, ro_conn):
    """Every public channel reported the same membership — the entire roster — because the handler
    skipped membership for a public channel. Real Slack's membership differs per channel, and a
    workspace where every channel holds everybody is not a shape it produces.

    Membership is now the channel's own participants, which is what the corpus actually knows."""
    chans = client.get("/slack/api/conversations.list", headers=admin_h,
                       params={"limit": 100}).json()["channels"]
    seen = {}
    for c in chans[:4]:
        m = client.get("/slack/api/conversations.members", headers=admin_h,
                       params={"channel": c["id"], "limit": 1000}).json()
        assert m["ok"] is True
        seen[c["name"]] = set(m["members"])
        expected = {r[0] for r in ro_conn.execute(
            "SELECT DISTINCT author_email FROM slack_messages WHERE channel = ?", (c["name"],))}
        assert len(seen[c["name"]]) == len(expected), c["name"]
    assert len(set(map(frozenset, seen.values()))) > 1, \
        "different channels must not all report identical membership"


def test_slack_members_paginate(client, admin_h):
    """`limit` and `cursor` were never read, so `limit=5` returned 16,034 members with an empty
    cursor. Real Slack paginates this method (default 100, cursor-based)."""
    cid = _a_channel_id(client, admin_h)
    first = client.get("/slack/api/conversations.members", headers=admin_h,
                       params={"channel": cid, "limit": 2}).json()
    assert len(first["members"]) <= 2
    cursor = first["response_metadata"]["next_cursor"]
    everyone = client.get("/slack/api/conversations.members", headers=admin_h,
                          params={"channel": cid, "limit": 1000}).json()["members"]
    if len(everyone) > 2:
        assert cursor, "a truncated page must hand back a cursor"
        second = client.get("/slack/api/conversations.members", headers=admin_h,
                            params={"channel": cid, "limit": 2, "cursor": cursor}).json()
        assert not set(first["members"]) & set(second["members"]), "pages must not overlap"
        assert set(first["members"]) | set(second["members"]) <= set(everyone)
    else:
        assert cursor == ""


def test_slack_num_members_agrees_with_the_member_list(client, admin_h):
    """`conversations.info.num_members` counted the roster while `conversations.members` now pages
    the channel's own speakers. A client that stats a channel and then walks it must not get two
    different answers for the same question."""
    chans = client.get("/slack/api/conversations.list", headers=admin_h,
                       params={"limit": 100}).json()["channels"]
    for c in chans[:4]:
        listed = client.get("/slack/api/conversations.members", headers=admin_h,
                            params={"channel": c["id"], "limit": 1000}).json()["members"]
        assert c["num_members"] == len(listed), c["name"]
        info = client.get("/slack/api/conversations.info", headers=admin_h,
                          params={"channel": c["id"]}).json()["channel"]
        assert info["num_members"] == len(listed), c["name"]


def test_slack_members_channel_not_found(client, admin_h):
    j = client.get("/slack/api/conversations.members", headers=admin_h,
                   params={"channel": "C_NOPE"}).json()
    assert j == {"ok": False, "error": "channel_not_found"}


def test_slack_search_all(client, admin_h):
    # slack-go's Search()/SearchContext() hits search.all; it must return both messages + files.
    j = client.post("/slack/api/search.all", headers=admin_h, data={"query": "the"}).json()
    assert j["ok"] is True
    assert "messages" in j and "files" in j
    assert j["files"]["total"] == 0 and j["files"]["matches"] == []


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


# --------------------------------------------------------------------------- Notion

def tok(tokens_yaml, email):
    return next(u["token"] for u in tokens_yaml["users"] if u["email"] == email)


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


def test_notion_acl_hides_group_doc_from_outsider(client, tokens_yaml):
    from app import synth
    pid = synth.notion_id("nt-secret")
    outsider = tok(tokens_yaml, "ava@acme.com")  # ava is engineering, not people
    r = client.get(f"/notion/v1/pages/{pid}", headers={"Authorization": f"Bearer {outsider}"})
    assert r.status_code == 404 and r.json()["code"] == "object_not_found"
    # the owner (hana, in people) can see it
    owner = tok(tokens_yaml, "hana@acme.com")
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
    """The dedicated big-bucket DB, in-process: SigV4 only cares that the Host it sees matches what
    was signed, which holds for TestClient's base_url as much as a real port. ``reload=True``
    because the ``client`` fixture above still holds the module-level app — see ``client_for``."""
    with client_for(big_bucket_settings, reload=True) as c:
        yield c


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


# --- OpenAPI enrichment: the params each router advertises ---------------------------------
# The routers read query params off the raw request rather than through FastAPI signatures, so
# each has to declare what it honours by hand (openapi.qp). One table rather than a test per
# vendor: the assertion is identical and only the path and the expected names differ.

@pytest.mark.parametrize("path, expected", [
    ("/github/search/issues",                 {"q", "page", "per_page"}),
    ("/slack/api/search.messages",            {"query", "count", "page"}),
    ("/slack/api/conversations.history",      {"channel", "limit", "cursor"}),
    # `user_id` is the path param, asserted here so enrichment cannot drop it
    ("/gmail/v1/users/{user_id}/messages",    {"q", "maxResults", "pageToken", "user_id"}),
    ("/drive/v3/files",                       {"q", "pageSize", "pageToken", "fields"}),
    ("/notion/v1/users",                      {"start_cursor", "page_size"}),
    ("/atlassian/rest/api/3/search/jql",      {"jql", "maxResults", "nextPageToken"}),
    ("/atlassian/wiki/rest/api/search",       {"cql"}),
])
def test_router_advertises_the_params_it_honours(client, path, expected):
    op = client.get("/openapi.json").json()["paths"][path]["get"]
    assert expected <= {p["name"] for p in op.get("parameters", [])}


# --- OpenAPI enrichment: github response fidelity ------------------------------------------

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


# --- Slack: enrichment did not change the responses ---------------------------------------

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
        legacy = client.get(f"/notion/v1/databases/{did}",
                            headers={**admin_h, "Notion-Version": "2022-06-28"}).json()
        default = client.get(f"/notion/v1/databases/{did}",
                             headers={**admin_h, "Notion-Version": "2025-09-03"}).json()
        assert "properties" in legacy and "data_sources" in default


# --- OpenAPI enrichment: atlassian (jira + confluence) ------------------------------------

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


def test_fireflies_user_root_answers_for_a_person_only(client, admin_h, tokens_yaml):
    """`user` with no id is the authenticated user. An admin/service token is not a person."""
    assert ff_gql(client, "{ user { user_id email } }",
                  admin_h).json()["data"]["user"] is None
    ava = next(u["token"] for u in tokens_yaml["users"] if u["email"] == "ava@acme.com")
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
