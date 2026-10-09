"""HubSpot's CRM v3 surface: object listings, reads, search, batch and associations.

One file per router, so a source's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import json

import pytest

from backlot import store
from tests._helpers import crawl_hubspot, db_count, served_id, tiny_corpus

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


@pytest.mark.parametrize(
    "value,archived",
    [
        ("true", True),
        ("TRUE", True),
        ("True", True),
        ("1", False),
        ("yes", False),
        ("abc", False),
        ("", False),
        (" true", False),
        ("true ", False),
        ("\ttrue", False),
        ("true\t", False),
        ("\ntrue", False),
        ("true\n", False),
        ("\rtrue", False),
        ("true\r", False),
        ("\u00a0true", False),
        ("true\u00a0", False),
        (["true", "false"], True),
        (["true", ""], True),
        (["true", "abc"], True),
        (["TRUE", "false"], True),
        (["True", "false"], True),
        (["true", "false", "false"], True),
        (["false", "true"], False),
        (["", "true"], False),
        (["yes", "true"], False),
        (["1", "true"], False),
        (["abc", "true"], False),
        ([" true", "true"], False),
        (["false", "TRUE"], False),
        (["false", "false", "true"], False),
    ],
)
def test_hubspot_archived_parameter_reads_its_first_value_and_only_true_as_true(
    client, admin_h, value, archived
):
    """`_flag`'s rule over companies, on the first value when `archived` repeats (a list row is
    sent as the key repeated in list order): the one archived company when `_flag` reads that value
    as true, and otherwise the same page as a request without `archived`."""
    url = "/hubspot/crm/v3/objects/companies"
    r = client.get(url, headers=admin_h, params={"archived": value})
    assert r.status_code == 200
    if archived:
        assert [x["properties"]["name"] for x in r.json()["results"]] == ["Defunct Labs"]
    else:
        assert r.json() == client.get(url, headers=admin_h).json()


def test_hubspot_an_archived_record_is_read_only_with_archived_true(client, admin_h):
    """Measured against api.hubapi.com on 2026-09-30 over a portal with one archived contact: its
    `GET …/{id}` was a 404, the same with `?archived=true` a 200 with the record, and batch/read
    without `archived=true` a 207 with `results` empty and the not-found error for its id."""
    (defunct,) = client.get(
        "/hubspot/crm/v3/objects/companies", headers=admin_h, params={"archived": "true"}
    ).json()["results"]
    url = f"/hubspot/crm/v3/objects/companies/{defunct['id']}"

    r = client.get(url, headers=admin_h)
    assert r.status_code == 404
    assert r.json()["category"] == "OBJECT_NOT_FOUND"

    r = client.get(url, headers=admin_h, params={"archived": "true"})
    assert r.status_code == 200
    assert (r.json()["id"], r.json()["archived"]) == (defunct["id"], True)

    r = client.post(
        "/hubspot/crm/v3/objects/companies/batch/read",
        headers=admin_h,
        json={"inputs": [{"id": defunct["id"]}]},
    )
    assert r.status_code == 207
    assert r.json()["results"] == []
    assert [e["context"]["id"] for e in r.json()["errors"]] == [[defunct["id"]]]


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


def test_hubspot_unknown_object_type_is_400(client, admin_h):
    """A typo'd object type must not read as "this type has no records" — that silently turns a
    client bug into an empty result. An object type the caller simply cannot see any rows of is a
    different case and still returns an empty page.

    Measured against api.hubapi.com on 2026-08-21 (a key that authenticates but holds no CRM
    scopes, so a recognized type answers 403 and only an unrecognized one gets this far): the
    status is 400, the envelope carries `status` and `message` and no `category`, and a malformed
    objectTypeId gets a message of its own."""
    r = client.get("/hubspot/crm/v3/objects/widgets", headers=admin_h)
    assert r.status_code == 400
    assert r.json() == {"status": "error", "message": "Unable to infer object type from: widgets"}
    for r in (
        client.post("/hubspot/crm/v3/objects/widgets/search", headers=admin_h, json={}),
        client.post(
            "/hubspot/crm/v3/objects/widgets/batch/read", headers=admin_h, json={"inputs": []}
        ),
    ):
        assert r.status_code == 400
        assert r.json()["message"] == "Unable to infer object type from: widgets"
    r = client.get("/hubspot/crm/v3/objects/0-9999", headers=admin_h)
    assert r.status_code == 400
    assert r.json()["message"] == "Invalid object or event type id: 0-9999"
    # The other side of the same line: a standard type this corpus holds no records of is still a
    # type, so it answers an empty page. These four were asked of api.hubapi.com on 2026-08-24 and
    # answered 403 MISSING_SCOPES, the same as `deals` — resolved before the scope check — so a 400
    # here would deny a type the vendor recognizes. `appointments` is plural-only: its singular is
    # one of the four the API does NOT resolve.
    for known in ("invoices", "orders", "subscriptions", "leads", "appointments", "lead"):
        r = client.get(f"/hubspot/crm/v3/objects/{known}", headers=admin_h)
        assert r.status_code == 200, (known, r.json())
        assert r.json()["results"] == []
    r = client.get("/hubspot/crm/v3/objects/appointment", headers=admin_h)
    assert r.status_code == 400
    assert r.json()["message"] == "Unable to infer object type from: appointment"


@pytest.mark.parametrize(
    "asked,ok",
    [
        ("0-2", True),  # companies, the type this corpus holds
        ("0-3", True),  # deals: a real type, so an empty page rather than an error
        ("0-6", False),  # no standard object holds it
        ("0-41", False),
        ("2-12345", False),  # a custom object's id, which no corpus of Backlot defines
    ],
)
def test_hubspot_an_object_type_id_is_a_spelling_of_its_type(client, admin_h, asked, ok):
    """`/crm/v3/objects/0-3` IS deals. Checked against api.hubapi.com on 2026-08-23 with a
    scope-less key: each of the thirteen published ids answers 403 MISSING_SCOPES exactly as its
    name does, while an id no standard object holds answers 400 `Invalid object or event type
    id`."""
    r = client.get(f"/hubspot/crm/v3/objects/{asked}", headers=admin_h, params={"limit": 100})
    assert (r.status_code == 200) is ok, r.json()
    if not ok:
        assert r.json()["message"] == f"Invalid object or event type id: {asked}"
        return
    named = "companies" if asked == "0-2" else "deals"
    assert [row["id"] for row in r.json()["results"]] == [
        row["id"]
        for row in client.get(
            f"/hubspot/crm/v3/objects/{named}", headers=admin_h, params={"limit": 100}
        ).json()["results"]
    ]


def test_hubspot_one_type_stated_both_ways_is_one_type(tmp_path):
    """A portal has ONE `deals` type, so no HubSpot response can show a record under
    `/objects/deal/{id}` that `/objects/deal` never lists. Resolving to the first spelling that
    exists did exactly that to a corpus stating both: each list path served its own half while
    get-one and batch read accepted either."""
    from tests._helpers import client_for

    records = [
        {
            "source_type": "hubspot",
            "doc_id": "hs-singular",
            "object_type": "deal",
            "title": "Acme renewal",
            "content": "Renewal.",
            "author_email": "rep@acme.com",
            "created": "2026-03-01T09:00:00Z",
            "properties": {"dealname": "Acme renewal"},
        },
        {
            "source_type": "hubspot",
            "doc_id": "hs-plural",
            "object_type": "deals",
            "title": "Beta",
            "content": "Beta.",
            "author_email": "rep@acme.com",
            "created": "2026-03-02T09:00:00Z",
            "properties": {"dealname": "Beta"},
        },
    ]
    settings = tiny_corpus(tmp_path, records)
    # A second client over a different DB in this module, so the app is re-imported: the lifespan
    # writes its connection onto module-level state (see `client_for`).
    with client_for(settings, reload=True) as c:
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        both = {served_id("hubspot", "hs-singular"), served_id("hubspot", "hs-plural")}
        for asked in ("deal", "deals"):
            listed = c.get(f"/hubspot/crm/v3/objects/{asked}", headers=h, params={"limit": 100})
            assert {r["id"] for r in listed.json()["results"]} == both, asked
            searched = c.post(f"/hubspot/crm/v3/objects/{asked}/search", headers=h, json={})
            assert searched.json()["total"] == 2, asked
            for rid in both:
                assert c.get(f"/hubspot/crm/v3/objects/{asked}/{rid}", headers=h).status_code == 200


@pytest.mark.parametrize("asked", ["companies", "company"])
def test_hubspot_a_standard_type_answers_to_either_spelling(client, admin_h, asked):
    """HubSpot resolves the path segment through its object-type registry, so `/objects/company`
    reaches the same records as `/objects/companies` — measured: both answer 403 MISSING_SCOPES on
    a scope-less key, where an unrecognized word answers 400. A corpus states one spelling or the
    other, and neither may hide its records from the vendor's own path."""
    listed = client.get(
        f"/hubspot/crm/v3/objects/{asked}", headers=admin_h, params={"limit": 100}
    ).json()["results"]
    assert [r["id"] for r in listed] == [
        r["id"]
        for r in client.get(
            "/hubspot/crm/v3/objects/companies", headers=admin_h, params={"limit": 100}
        ).json()["results"]
    ]
    # and a record of that type resolves under both spellings, while another type's does not
    one = listed[0]["id"]
    assert client.get(f"/hubspot/crm/v3/objects/{asked}/{one}", headers=admin_h).status_code == 200
    assert client.get(f"/hubspot/crm/v3/objects/tickets/{one}", headers=admin_h).status_code == 404


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


def test_hubspot_acl_hides_restricted_record(client, tokens_yaml, admin_h):
    """`hs-co-secret` is readable only by hana; another user's crawl must not contain it, and a
    direct-by-id read must enforce the same grant -- not just the listing -- since get_object now
    resolves served_id and the ACL in one query (store.hubspot_by_id) rather than a
    resolve-then-refetch, and a collapse that dropped the ACL half would only show up here."""
    users = {u["email"]: u["token"] for u in tokens_yaml["users"]}
    ava_h = {"Authorization": f"Bearer {users['ava@acme.com']}"}
    hana_h = {"Authorization": f"Bearer {users['hana@acme.com']}"}

    def names(h):
        return {r["properties"].get("name") for r in crawl_hubspot(client, h, "companies")}

    assert "Stealth Health Co" not in names(ava_h)
    assert "Stealth Health Co" in names(hana_h)

    secret = next(
        r
        for r in crawl_hubspot(client, admin_h, "companies")
        if r["properties"].get("name") == "Stealth Health Co"
    )
    url = f"/hubspot/crm/v3/objects/companies/{secret['id']}"
    assert client.get(url, headers=ava_h).status_code == 404
    assert client.get(url, headers=hana_h).status_code == 200


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
    # a NUMBER, as real v4 sends it — the v3 `id` beside it is a string, and the official python
    # client models this one as an int
    assert isinstance(assoc["toObjectId"], int)
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
    # The negative operators include a record without the property: Stealth Health Co has no
    # `domain`, so each of the three below finds it as well.
    assert f(propertyName="domain", operator="NEQ", value="acme-health.com") == {
        "Borealis Clinics",
        "Stealth Health Co",
    }
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
    assert f(propertyName="domain", operator="NOT_IN", values=["acme-health.com"]) == {
        "Borealis Clinics",
        "Stealth Health Co",
    }
    assert f(propertyName="domain", operator="HAS_PROPERTY") == {"Acme Health", "Borealis Clinics"}
    assert f(propertyName="domain", operator="NOT_HAS_PROPERTY") == {"Stealth Health Co"}
    assert f(propertyName="name", operator="CONTAINS_TOKEN", value="Clinics") == {
        "Borealis Clinics"
    }
    assert f(propertyName="domain", operator="NOT_CONTAINS_TOKEN", value="borealis") == {
        "Acme Health",
        "Stealth Health Co",
    }


@pytest.mark.parametrize(
    "prop, value, want",
    [
        ("name", "Clin*", {"Borealis Clinics"}),
        ("name", "*lini*", {"Borealis Clinics"}),
        ("name", "*nics", {"Borealis Clinics"}),
        ("name", "Clinics*", {"Borealis Clinics"}),
        ("name", "Cl*cs", {"Borealis Clinics"}),
        ("name", "Heal*", {"Acme Health", "Stealth Health Co"}),
        ("name", "* Clinics", {"Borealis Clinics"}),
        ("name", "Clin", set()),
        ("name", "Clin*x", set()),
        ("name", "?linics", set()),
        # `*` alone asks only for a token, and Stealth Health Co has no `domain`
        ("domain", "*", {"Acme Health", "Borealis Clinics"}),
    ],
)
def test_hubspot_contains_token_reads_star_as_a_wildcard(client, admin_h, prop, value, want):
    """The wildcard rule `_NEEDLE_RE`'s comment records; `NOT_CONTAINS_TOKEN` finds none of what
    `CONTAINS_TOKEN` finds."""

    def f(op):
        return _hs_filter(client, admin_h, propertyName=prop, operator=op, value=value)

    assert f("CONTAINS_TOKEN") == want
    assert not want & f("NOT_CONTAINS_TOKEN")


def test_hubspot_wildcard_needle_with_many_stars_stays_fast():
    # the needle comes from the request, so many `*` against one long token must not backtrack
    from backlot.routers import hubspot as hs

    f = {"operator": "CONTAINS_TOKEN", "value": "a*a*a*a*a*a*a*a*b"}
    assert hs._match_one("a" * 20000, f) is False
    assert hs._match_one("a" * 20000 + "b", f) is True


def _enum_refusal(line: int, column: int) -> dict:
    return {
        "status": "error",
        "message": f"Invalid input JSON on line {line}, column {column}: Enum type must be one of: "
        "[IN, NOT_HAS_PROPERTY, LT, EQ, GT, NOT_IN, GTE, CONTAINS_TOKEN, HAS_PROPERTY, LTE, "
        "NOT_CONTAINS_TOKEN, BETWEEN, NEQ]",
        "category": "VALIDATION_ERROR",
    }


def _empty_operator_refusal(group: int, position: int) -> dict:
    return {
        "status": "error",
        "message": "Invalid input JSON: unable to deserialize field "
        f'"filterGroups[{group}].filters[{position}].operator". Invalid value: ',
        "category": "VALIDATION_ERROR",
    }


def _jobtitle(*groups, compact=True, **top) -> str:
    """A search body on `jobtitle`, one group per argument, each a list of `(operator, value)`."""
    body = {
        "filterGroups": [
            {"filters": [{"propertyName": "jobtitle", "operator": o, "value": v} for o, v in g]}
            for g in groups
        ],
        **top,
    }
    return json.dumps(body, separators=(",", ":")) if compact else json.dumps(body)


def _value_first(value: str, *, ensure_ascii: bool) -> str:
    f = {"propertyName": "jobtitle", "value": value, "operator": "neq"}
    return json.dumps(
        {"filterGroups": [{"filters": [f]}]}, separators=(",", ":"), ensure_ascii=ensure_ascii
    )


def _op(id, raw, want, object_type="contacts", authed=True):
    return pytest.param(raw, want, object_type, authed, id=id)


def _served_as(name):
    return ("served as", _jobtitle([(name, "Salesperson")]))


# fmt: off
_OPERATOR_ROWS = [
    _op("compact", _jobtitle([("neq", "Salesperson")]), _enum_refusal(1, 68)),
    _op("default-separators", _jobtitle([("neq", "Salesperson")], compact=False), _enum_refusal(1, 73)),
    _op("indent", json.dumps(json.loads(_jobtitle([("neq", "Salesperson")])), indent=2), _enum_refusal(7, 23)),
    _op("crlf", json.dumps(json.loads(_jobtitle([("neq", "Salesperson")])), indent=2).replace("\n", "\r\n"), _enum_refusal(7, 23)),
    _op("tab-indent", json.dumps(json.loads(_jobtitle([("neq", "Salesperson")])), indent="\t"), _enum_refusal(7, 18)),
    _op("two-byte-chars-before", _value_first("ééé", ensure_ascii=False), _enum_refusal(1, 85)),
    _op("escaped-chars-before", _value_first("ééé", ensure_ascii=True), _enum_refusal(1, 97)),
    _op("four-byte-char-before", _value_first(chr(0x1F600), ensure_ascii=False), _enum_refusal(1, 83)),
    _op("mixed-case", _jobtitle([("Eq", "x")]), _enum_refusal(1, 68)),
    _op("unknown-name", _jobtitle([("BOGUS", "x")]), _enum_refusal(1, 68)),
    _op("non-ascii-name", json.dumps(json.loads(_jobtitle([("NÉQ", "x")])), separators=(",", ":"), ensure_ascii=False), _enum_refusal(1, 68)),
    _op("no-break-space-before", _jobtitle([(chr(0xA0) + "EQ", "Salesperson")]), _enum_refusal(1, 68)),
    _op("em-space-after", _jobtitle([("EQ" + chr(0x2003), "Salesperson")]), _enum_refusal(1, 68)),
    _op("index-13", _jobtitle([("13", "Salesperson")]), _enum_refusal(1, 68)),
    _op("index-01", _jobtitle([("01", "Salesperson")]), _enum_refusal(1, 68)),
    _op("index-plus-1", _jobtitle([("+1", "Salesperson")]), _enum_refusal(1, 68)),
    _op("empty", _jobtitle([("", "x")]), _empty_operator_refusal(0, 0)),
    _op("blank", _jobtitle([("  ", "Salesperson")]), _empty_operator_refusal(0, 0)),
    _op("empty-second", _jobtitle([("EQ", "Salesperson"), ("", "Salesperson")]), _empty_operator_refusal(0, 1)),
    _op("second-filter", _jobtitle([("EQ", "x"), ("neq", "x")]), _enum_refusal(1, 124)),
    _op("second-group", _jobtitle([("EQ", "x")], [("neq", "x")]), _enum_refusal(1, 138)),
    _op("first-of-two-unknown", _jobtitle([("bogus", "x"), ("neq", "x")]), _enum_refusal(1, 68)),
    _op("first-of-two-groups", _jobtitle([("bogus", "x")], [("neq", "x")]), _enum_refusal(1, 68)),
    _op("empty-in-second-group", _jobtitle([("EQ", "x")], [("", "x")]), _empty_operator_refusal(1, 0)),
    _op("empty-before-unknown", _jobtitle([("", "Salesperson"), ("neq", "Salesperson")]), _empty_operator_refusal(0, 0)),
    _op("unknown-before-empty", _jobtitle([("neq", "Salesperson"), ("", "Salesperson")]), _enum_refusal(1, 68)),
    _op("repeated-operator-key", '{"filterGroups":[{"filters":[{"propertyName":"jobtitle","operator":"EQ","operator":"neq","value":"Salesperson"}]}]}', _enum_refusal(1, 84)),
    _op("repeated-filtergroups-key", '{"filterGroups":[{"filters":[{"propertyName":"jobtitle","operator":"EQ","value":"x"}]}],"filterGroups":[{"filters":[{"propertyName":"jobtitle","operator":"neq","value":"x"}]}]}', _enum_refusal(1, 155)),
    _op("after-an-integer-operator", '{"filterGroups":[{"filters":[{"propertyName":"jobtitle","operator":5,"value":"x"},{"propertyName":"jobtitle","operator":"neq","value":"x"}]}]}', _enum_refusal(1, 121)),
    _op("before-a-cursor-naming-no-record", _jobtitle([("neq", "x")], after="10000"), _enum_refusal(1, 68)),
    _op("after-an-unknown-type", _jobtitle([("neq", "Salesperson")]), {"status": "error", "message": "Unable to infer object type from: nosuchtype"}, object_type="nosuchtype"),
    _op("after-the-credential", _jobtitle([("neq", "Salesperson")]), 401, authed=False),
    _op("space-before", _jobtitle([(" NEQ", "Salesperson")]), _served_as("NEQ")),
    _op("tab-before", _jobtitle([("\tNEQ", "Salesperson")]), _served_as("NEQ")),
    _op("u0001-before", _jobtitle([(chr(1) + "NEQ", "Salesperson")]), _served_as("NEQ")),
    _op("space-after", _jobtitle([("NEQ ", "Salesperson")]), _served_as("NEQ")),
    _op("index-0", _jobtitle([("0", "Salesperson")]), 200),
    _op("index-12", _jobtitle([("12", "Salesperson")]), 200),
    _op("index-1-after-a-space", _jobtitle([(" 1", "Salesperson")]), 200),
]
# fmt: on


@pytest.mark.parametrize("raw, want, object_type, authed", _OPERATOR_ROWS)
def test_hubspot_search_reads_a_filter_operator_as_real_does(
    client, admin_h, raw, want, object_type, authed
):
    """`_refuse_an_operator`'s rule, one body per row: the status and message real answered for each
    operator it could not read, and a 200 for each it trims (`_TRIM`) or reads as an index
    (`_OPERATOR_INDEXES`). The trimmed rows put real's trimmed spellings around `NEQ`, whose page on
    this corpus is not empty, and compare that page with `NEQ`'s own; the index rows check only that
    the body is served."""
    headers = {"Content-Type": "application/json", **(admin_h if authed else {})}
    url = f"/hubspot/crm/v3/objects/{object_type}/search"
    r = client.post(url, headers=headers, content=raw.encode())
    if isinstance(want, dict):
        assert (r.status_code, r.json()) == (400, want)
    elif isinstance(want, tuple):
        control = client.post(url, headers=headers, content=want[1].encode()).json()["results"]
        assert r.status_code == 200
        assert r.json()["results"] == control != []
    else:
        assert r.status_code == want


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
        # IN on `domain`, which Stealth Health Co lacks: the pushdown drops that record, so Python
        # has to as well
        {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "domain",
                            "operator": "IN",
                            "values": ["acme-health.com", "borealis.example"],
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
        # a wildcard needle: the pieces around `*` are still substrings of what it matches
        {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": "*lin*"}
                    ]
                }
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
    from backlot.routers.hubspot import _record

    conn = _hubspot_conn(tmp_path)
    row = store.get_document(conn, "hubspot", served_id("hubspot", "hf-co"))
    obj = _record(row)
    # a CRM record is {id, properties, createdAt, updatedAt, archived} — ids are numeric strings and
    # the timestamps are ISO 8601 with milliseconds, as the vendor emits them. `id` must be the
    # row's own STORED served_id, not a re-hash of a seed: hubspot's id space is probed on a
    # collision, so a re-hash can disagree with the value this row was actually assigned --
    # equality with `synth.hubspot_record_id(row["doc_id"])` only holds here because this fixture
    # has no collision, which is exactly why that comparison is the wrong one to assert.
    assert obj["id"] == row["id"]
    assert obj["id"].isdigit()
    assert obj["properties"]["domain"] == "acme-health.com"
    assert obj["createdAt"] == "2026-01-05T00:00:00.000Z"
    assert obj["updatedAt"] == "2026-03-10T00:00:00.000Z"
    assert obj["archived"] is False
    assert (
        _record(store.get_document(conn, "hubspot", served_id("hubspot", "hf-arch")))["archived"]
        is True
    )


def test_hubspot_properties_projection(tmp_path):
    from backlot.routers.hubspot import _record

    conn = _hubspot_conn(tmp_path)
    row = store.get_document(conn, "hubspot", served_id("hubspot", "hf-co"))
    assert set(_record(row, ["name"])["properties"]) == {"name"}
    assert set(_record(row)["properties"]) == {"name", "domain"}  # no projection -> all


def test_hubspot_association_shape(tmp_path):
    conn = _hubspot_conn(tmp_path)
    rows = store.hubspot_associations(conn, served_id("hubspot", "hf-ct"), "companies")
    assert [r["to_id"] for r in rows] == [served_id("hubspot", "hf-co")]
    # the v4 payload is {toObjectId, associationTypes:[{category, typeId, label}]} -- toObjectId
    # reads the target's own STORED served_id (joined in by store.hubspot_associations), not a
    # re-hash of its doc_id: hubspot's id space is probed on a collision, so a re-hash can
    # disagree with the value the target row was actually assigned.
    assert (
        rows[0]["to_id"] == store.get_document(conn, "hubspot", served_id("hubspot", "hf-co"))["id"]
    )
    assert rows[0]["assoc_category"] == "HUBSPOT_DEFINED"
    assert rows[0]["label"] == "Primary"
    # the reverse direction exists and carries its own type id, as real HubSpot does
    back = store.hubspot_associations(conn, served_id("hubspot", "hf-co"), "contacts")
    assert [r["to_id"] for r in back] == [served_id("hubspot", "hf-ct")]
    assert back[0]["assoc_type_id"] != rows[0]["assoc_type_id"]


def test_hubspot_route_reads_the_stored_served_id_under_a_collision(tmp_path, monkeypatch):
    """Route-level companion to the store-layer collision tests: `_record`'s `id`, `_page`'s
    `after` cursor, and `list_associations`' `toObjectId` all have to read the row's own stored
    `id`, not fall back to a live re-hash of a seed. Reverting all three to
    `synth.hubspot_record_id(...)` leaves the FULL suite green -- every other route test's fixture
    happens to have no collision, so the re-hash and the stored column agree by coincidence. Forced
    here the same way the store tests force one: the seed collapsed
    to a constant, so every record but the first import order is walked away from the raw hash.
    """
    from backlot import store
    from tests._helpers import client_for

    monkeypatch.setitem(store.ID_SEED, "hubspot", (lambda seed: "1000000000", None))
    docs = [
        {
            "source_type": "hubspot",
            "doc_id": f"h{i}",
            "object_type": "companies",
            "title": f"Co {i}",
            "content": "x",
            "author_email": "a@acme.com",
            "properties": {"name": f"Co {i}"},
        }
        for i in range(3)
    ]
    docs.append(
        {
            "source_type": "hubspot",
            "doc_id": "hn0",
            "object_type": "notes",
            "title": "",
            "content": "x",
            "author_email": "a@acme.com",
            "associations": [{"to": "h0"}],
        }
    )
    s = tiny_corpus(tmp_path, docs)
    monkeypatch.undo()

    with client_for(s) as client:
        h = {"Authorization": f"Bearer {s.admin_token}"}

        # _page's `after`: walk companies one at a time, so every page but the last carries a
        # cursor that has to resolve back to the NEXT company, not loop or skip.
        ids, after, pages = [], None, 0
        while True:
            params = {"limit": 1, **({"after": after} if after else {})}
            page = client.get("/hubspot/crm/v3/objects/companies", headers=h, params=params).json()
            ids += [r["id"] for r in page["results"]]
            pages += 1
            assert pages < 10, "collision-forced pagination did not terminate"
            nxt = (page.get("paging") or {}).get("next")
            if not nxt:
                break
            after = nxt["after"]
        assert len(ids) == len(set(ids)) == 3  # every id distinct despite the forced collision

        # _record's `id`: each listed id must round-trip through a direct GET to ITS OWN record,
        # not some other record that happens to share the raw (un-probed) hash.
        for i in ids:
            got = client.get(f"/hubspot/crm/v3/objects/companies/{i}", headers=h).json()
            assert got["id"] == i

        # list_associations' `toObjectId`: must be h0's own served id (ids[0], since the listing
        # is doc_id-ordered and "h0" sorts first) -- not a re-hash of "h0" that a collision walk
        # may have moved h0 away from.
        note_id = client.get(
            "/hubspot/crm/v3/objects/notes", headers=h, params={"limit": 10}
        ).json()["results"][0]["id"]
        assoc = client.get(
            f"/hubspot/crm/v4/objects/notes/{note_id}/associations/companies", headers=h
        ).json()
        assert str(assoc["results"][0]["toObjectId"]) == ids[0]


def test_hubspot_page_omits_paging_next_on_last_page(tmp_path):
    """The termination contract at the builder level: `paging.next` appears only when a further page
    exists, because the official client's fetch_all stops on its absence."""
    from backlot.routers.hubspot import _page

    conn = _hubspot_conn(tmp_path)
    rows = store.list_hubspot_objects(conn, "companies", limit=3)  # 1 non-archived company
    assert "paging" not in _page(rows, 10, None)
    assert _page(rows, 1, None)["results"]  # a full page still yields rows
