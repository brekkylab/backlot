"""HubSpot's CRM v3 surface: object listings, reads, search, batch and associations.

One file per router, so a provider's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

from backlot import store
from tests._helpers import crawl_hubspot, db_count, tiny_corpus


HUBSPOT_OBJECT_TYPES = ("companies", "contacts", "notes")


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
    assert pages == 2  # the cursor branch was actually taken
    assert len(seen) == len(set(seen)) == 3  # every non-archived company exactly once


def test_hubspot_last_page_omits_paging_next(client, admin_h):
    """The termination contract, asserted directly: a page that exhausts the type must not carry
    paging.next. Getting this wrong makes the official SDK's fetch_all loop forever."""
    j = client.get(
        "/hubspot/crm/v3/objects/contacts", headers=admin_h, params={"limit": 100}
    ).json()
    assert j["results"]
    assert "next" not in (j.get("paging") or {})


def test_hubspot_read_one_record(client, admin_h):
    listed = client.get(
        "/hubspot/crm/v3/objects/companies", headers=admin_h, params={"limit": 100}
    ).json()["results"]
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
    assert (
        client.post("/hubspot/crm/v3/objects/widgets/search", headers=admin_h, json={}).status_code
        == 404
    )
    assert (
        client.post(
            "/hubspot/crm/v3/objects/widgets/batch/read", headers=admin_h, json={"inputs": []}
        ).status_code
        == 404
    )


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
    r = client.get(
        "/hubspot/crm/v3/objects/companies", headers=admin_h, params={"after": "0000000000"}
    )
    assert r.status_code == 400


def test_hubspot_missing_record_is_404(client, admin_h):
    assert (
        client.get("/hubspot/crm/v3/objects/companies/999999999999", headers=admin_h).status_code
        == 404
    )


def test_hubspot_unauth_is_401(client):
    assert client.get("/hubspot/crm/v3/objects/companies").status_code == 401


def test_hubspot_acl_hides_restricted_record(client, tokens_yaml):
    """`hs-co-secret` is readable only by hana; another user's crawl must not contain it."""
    users = {u["email"]: u["token"] for u in tokens_yaml["users"]}
    ava_h = {"Authorization": f"Bearer {users['ava@acme.com']}"}
    hana_h = {"Authorization": f"Bearer {users['hana@acme.com']}"}

    def names(h):
        return {r["properties"].get("name") for r in crawl_hubspot(client, h, "companies")}

    assert "Stealth Health Co" not in names(ava_h)
    assert "Stealth Health Co" in names(hana_h)


def test_hubspot_associations_v4(client, admin_h):
    listed = client.get(
        "/hubspot/crm/v3/objects/contacts", headers=admin_h, params={"limit": 100}
    ).json()["results"]
    ava = next(r for r in listed if r["properties"].get("firstname") == "Ava")
    j = client.get(
        f"/hubspot/crm/v4/objects/contacts/{ava['id']}/associations/companies", headers=admin_h
    ).json()
    assert len(j["results"]) == 1
    assoc = j["results"][0]
    assert assoc["toObjectId"].isdigit()
    assert assoc["associationTypes"][0]["category"] == "HUBSPOT_DEFINED"
    assert assoc["associationTypes"][0]["label"] == "Primary"


def test_hubspot_search_filter_groups(client, admin_h):
    """filterGroups combine as OR, filters within a group as AND — over arbitrary properties."""
    body = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "industry", "operator": "EQ", "value": "healthcare"},
                    {"propertyName": "lifecyclestage", "operator": "EQ", "value": "evaluation"},
                ]
            }
        ]
    }
    j = client.post("/hubspot/crm/v3/objects/companies/search", headers=admin_h, json=body).json()
    assert [r["properties"]["name"] for r in j["results"]] == ["Acme Health"]
    assert j["total"] == 1
    # AND within a group: contradicting the second filter drops the row
    body["filterGroups"][0]["filters"][1]["value"] = "qualified"
    assert (
        client.post("/hubspot/crm/v3/objects/companies/search", headers=admin_h, json=body).json()[
            "results"
        ]
        == []
    )
    # OR across groups: two single-filter groups match two different rows
    body = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "lifecyclestage", "operator": "EQ", "value": "evaluation"}
                ]
            },
            {
                "filters": [
                    {"propertyName": "lifecyclestage", "operator": "EQ", "value": "qualified"}
                ]
            },
        ]
    }
    j = client.post("/hubspot/crm/v3/objects/companies/search", headers=admin_h, json=body).json()
    assert {r["properties"]["name"] for r in j["results"]} == {"Acme Health", "Stealth Health Co"}


def test_hubspot_search_total_counts_all_matches_not_the_page(client, admin_h):
    """`total` is how many records matched, independent of how many fit on this page — so a
    one-record page over two matches still reports 2, and carries a cursor for the rest."""
    body = {
        "limit": 1,
        "filterGroups": [{"filters": [{"propertyName": "name", "operator": "HAS_PROPERTY"}]}],
    }
    totals, after, pages = [], None, 0
    while True:
        j = client.post(
            "/hubspot/crm/v3/objects/companies/search",
            headers=admin_h,
            json={**body, **({"after": after} if after else {})},
        ).json()
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
    j = client.post(
        "/hubspot/crm/v3/objects/companies/search",
        headers=admin_h,
        json={
            "filterGroups": [{"filters": [{"propertyName": "domain", "operator": "HAS_PROPERTY"}]}]
        },
    ).json()
    assert {r["properties"]["name"] for r in j["results"]} == {"Acme Health", "Borealis Clinics"}
    j = client.post(
        "/hubspot/crm/v3/objects/companies/search",
        headers=admin_h,
        json={
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": "Health"}
                    ]
                }
            ]
        },
    ).json()
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
    assert f(propertyName="employees", operator="BETWEEN", value="100", highValue="200") == {
        "Acme Health"
    }
    # BETWEEN must fall back to string comparison the way LT/GT do, or an ISO-8601 range silently
    # matches nothing while `GT` on the same property works.
    assert f(
        propertyName="founded", operator="BETWEEN", value="2014-01-01", highValue="2014-12-31"
    ) == {"Borealis Clinics"}
    assert f(
        propertyName="lifecyclestage", operator="IN", values=["evaluation", "procurement"]
    ) == {"Acme Health", "Borealis Clinics"}
    assert "Acme Health" not in f(
        propertyName="lifecyclestage", operator="NOT_IN", values=["evaluation"]
    )
    assert f(propertyName="domain", operator="HAS_PROPERTY") == {"Acme Health", "Borealis Clinics"}
    assert f(propertyName="domain", operator="NOT_HAS_PROPERTY") == {"Stealth Health Co"}
    assert f(propertyName="name", operator="CONTAINS_TOKEN", value="Clinics") == {
        "Borealis Clinics"
    }
    assert "Borealis Clinics" not in f(
        propertyName="name", operator="NOT_CONTAINS_TOKEN", value="Clinics"
    )


def test_hubspot_search_prefilter_cannot_change_results(client, admin_h, monkeypatch):
    """The SQL pre-filter is a pure optimisation: it may only skip rows Python would have rejected
    anyway. Every query is run twice — once with the pushdown, once with it disabled — and the
    results and totals must be identical, so a pre-filter that is not a *necessary* condition fails
    here rather than silently dropping matches."""
    from backlot.routers import hubspot as hs

    bodies = [
        {
            "filterGroups": [
                {"filters": [{"propertyName": "industry", "operator": "EQ", "value": "healthcare"}]}
            ]
        },
        {"filterGroups": [{"filters": [{"propertyName": "domain", "operator": "HAS_PROPERTY"}]}]},
        {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": "Health"}
                    ]
                }
            ]
        },
        {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "lifecyclestage",
                            "operator": "IN",
                            "values": ["evaluation", "procurement"],
                        }
                    ]
                }
            ]
        },
        # a group whose filters mix a pushable and a non-pushable operator
        {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "name", "operator": "HAS_PROPERTY"},
                        {"propertyName": "employees", "operator": "GT", "value": "100"},
                    ]
                }
            ]
        },
        # OR across groups: no single filter is necessary, so nothing may be pushed down
        {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "industry", "operator": "EQ", "value": "healthcare"}
                    ]
                },
                {
                    "filters": [
                        {"propertyName": "lifecyclestage", "operator": "EQ", "value": "qualified"}
                    ]
                },
            ]
        },
        {"query": "acme"},
    ]

    def run(body):
        j = client.post(
            "/hubspot/crm/v3/objects/companies/search", headers=admin_h, json={**body, "limit": 100}
        ).json()
        return j["total"], [r["id"] for r in j["results"]]

    with_pushdown = [run(b) for b in bodies]
    monkeypatch.setattr(hs, "_sql_prefilter", lambda body: None)
    without = [run(b) for b in bodies]
    assert with_pushdown == without


def test_hubspot_search_sorts(client, admin_h):
    """`sorts` is advertised, so it has to order the whole match set — not just whatever landed on
    the page. Numeric properties sort numerically, which string ordering would get wrong."""

    def names(direction):
        j = client.post(
            "/hubspot/crm/v3/objects/companies/search",
            headers=admin_h,
            json={
                "filterGroups": [
                    {"filters": [{"propertyName": "employees", "operator": "HAS_PROPERTY"}]}
                ],
                "sorts": [{"propertyName": "employees", "direction": direction}],
            },
        ).json()
        return [r["properties"]["name"] for r in j["results"]]

    assert names("ASCENDING") == ["Acme Health", "Borealis Clinics"]  # 150 then 400
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
    listed = client.get(
        "/hubspot/crm/v3/objects/companies", headers=admin_h, params={"limit": 100}
    ).json()["results"]
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
    listed = client.get(
        "/hubspot/crm/v3/objects/companies", headers=admin_h, params={"limit": 100}
    ).json()["results"]
    r = client.post(
        "/hubspot/crm/v3/objects/companies/batch/read",
        headers=admin_h,
        json={"inputs": [{"id": listed[0]["id"]}, {"id": "111111111111"}]},
    )
    assert r.status_code == 207
    j = r.json()
    assert j["status"] == "COMPLETE"
    assert len(j["results"]) == 1
    assert j["numErrors"] == 1
    assert j["errors"][0]["context"]["id"] == ["111111111111"]


def test_hubspot_batch_read(client, admin_h):
    listed = client.get(
        "/hubspot/crm/v3/objects/companies", headers=admin_h, params={"limit": 100}
    ).json()["results"]
    ids = [r["id"] for r in listed]
    j = client.post(
        "/hubspot/crm/v3/objects/companies/batch/read",
        headers=admin_h,
        json={"inputs": [{"id": i} for i in ids], "properties": ["name"]},
    ).json()
    assert {r["id"] for r in j["results"]} == set(ids)


# --- HubSpot ---------------------------------------------------------------------


def _hubspot_conn(tmp_path):
    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "hubspot",
                "doc_id": "hf-co",
                "object_type": "companies",
                "title": "Acme Health",
                "content": "Mid-market provider.",
                "author_email": "rep@acme.com",
                "visibility": "public",
                "created": "2026-01-05T00:00:00Z",
                "updated": "2026-03-10T00:00:00Z",
                "properties": {"name": "Acme Health", "domain": "acme-health.com"},
            },
            {
                "source_type": "hubspot",
                "doc_id": "hf-ct",
                "object_type": "contacts",
                "title": "Ava",
                "content": "VP Platform.",
                "author_email": "rep@acme.com",
                "visibility": "public",
                "properties": {"firstname": "Ava"},
                "associations": [{"to": "hf-co", "label": "Primary"}],
            },
            {
                "source_type": "hubspot",
                "doc_id": "hf-arch",
                "object_type": "companies",
                "title": "Defunct",
                "content": "Churned.",
                "author_email": "rep@acme.com",
                "visibility": "public",
                "archived": True,
                "properties": {"name": "Defunct"},
            },
        ],
    )
    return store.connect_ro(s.db_path)


def test_hubspot_record_shape(tmp_path):
    from backlot import synth
    from backlot.routers.hubspot import _record

    conn = _hubspot_conn(tmp_path)
    obj = _record(store.get_document(conn, "hubspot", "hf-co"))
    # a CRM record is {id, properties, createdAt, updatedAt, archived} — ids are numeric strings and
    # the timestamps are ISO 8601 with milliseconds, as the vendor emits them
    assert obj["id"] == synth.hubspot_record_id("hf-co")
    assert obj["id"].isdigit()
    assert obj["properties"]["domain"] == "acme-health.com"
    assert obj["createdAt"] == "2026-01-05T00:00:00.000Z"
    assert obj["updatedAt"] == "2026-03-10T00:00:00.000Z"
    assert obj["archived"] is False
    assert _record(store.get_document(conn, "hubspot", "hf-arch"))["archived"] is True


def test_hubspot_properties_projection(tmp_path):
    from backlot.routers.hubspot import _record

    conn = _hubspot_conn(tmp_path)
    row = store.get_document(conn, "hubspot", "hf-co")
    assert set(_record(row, ["name"])["properties"]) == {"name"}
    assert set(_record(row)["properties"]) == {"name", "domain"}  # no projection -> all


def test_hubspot_association_shape(tmp_path):
    from backlot import synth

    conn = _hubspot_conn(tmp_path)
    rows = store.hubspot_associations(conn, "hf-ct", "companies")
    assert [r["to_doc_id"] for r in rows] == ["hf-co"]
    # the v4 payload is {toObjectId, associationTypes:[{category, typeId, label}]}
    assert synth.hubspot_record_id(rows[0]["to_doc_id"]).isdigit()
    assert rows[0]["assoc_category"] == "HUBSPOT_DEFINED"
    assert rows[0]["label"] == "Primary"
    # the reverse direction exists and carries its own type id, as real HubSpot does
    back = store.hubspot_associations(conn, "hf-co", "contacts")
    assert [r["to_doc_id"] for r in back] == ["hf-ct"]
    assert back[0]["assoc_type_id"] != rows[0]["assoc_type_id"]


def test_hubspot_page_omits_paging_next_on_last_page(tmp_path):
    """The termination contract at the builder level: `paging.next` appears only when a further page
    exists, because the official client's fetch_all stops on its absence."""
    from backlot.routers.hubspot import _page

    conn = _hubspot_conn(tmp_path)
    rows = store.list_hubspot_objects(conn, "companies", limit=3)  # 1 non-archived company
    assert "paging" not in _page(rows, 10, None)
    assert _page(rows, 1, None)["results"]  # a full page still yields rows
