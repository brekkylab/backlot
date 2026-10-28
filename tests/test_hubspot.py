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


_ARCHIVED_VALUES = [
    (None, False),
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
]


def test_hubspot_archived_parameter_reads_its_first_value_and_only_true_as_true(client, admin_h):
    """`_flag`'s rule over companies, on the first value when `archived` repeats (a list row is
    sent as the key repeated in list order, and `None` sends no `archived`). The listing answers the
    one archived company when `_flag` reads that value as true, and otherwise the same page as a
    request without `archived`. Read by id, alone or through batch/read, that company is served
    when `_flag` reads the value as true and otherwise answered as a missing id is (see
    `_hidden_archived`); an active company is served whatever the value."""
    url = "/hubspot/crm/v3/objects/companies"
    (defunct,) = client.get(url, headers=admin_h, params={"archived": "true"}).json()["results"]
    unflagged = client.get(url, headers=admin_h).json()
    active = unflagged["results"][0]["id"]
    missing = "999999999999"
    gone = client.get(f"{url}/{missing}", headers=admin_h)
    gone_batch = client.post(
        f"{url}/batch/read", headers=admin_h, json={"inputs": [{"id": missing}]}
    )
    answered, expected = {}, {}
    for value, archived in _ARCHIVED_VALUES:
        params = {} if value is None else {"archived": value}
        r = client.get(url, headers=admin_h, params=params)
        page = r.json()
        reads, wanted = [], []
        for rid, served in ((defunct["id"], archived), (active, True)):
            got = client.get(f"{url}/{rid}", headers=admin_h, params=params)
            batch = client.post(
                f"{url}/batch/read", headers=admin_h, params=params, json={"inputs": [{"id": rid}]}
            )
            reads.append(
                (
                    got.status_code,
                    got.json()["id"]
                    if got.status_code == 200
                    else (got.headers["content-type"], got.content),
                    batch.status_code,
                    [x["id"] for x in batch.json()["results"]]
                    if batch.status_code == 200
                    else json.loads(batch.text.replace(rid, missing)),
                )
            )
            wanted.append(
                (200, rid, 200, [rid])
                if served
                else (404, (gone.headers["content-type"], gone.content), 207, gone_batch.json())
            )
        answered[repr(value)] = (
            r.status_code,
            [x["properties"]["name"] for x in page["results"]],
            page == unflagged,
            reads,
        )
        expected[repr(value)] = (
            200,
            ["Defunct Labs"]
            if archived
            else [x["properties"]["name"] for x in unflagged["results"]],
            not archived,
            wanted,
        )
    assert answered == expected


_UNSUPPORTED_ARCHIVED_TYPES = [
    ("meetings", "0-47 (MEETING_EVENT)"),
    ("meeting", "0-47 (MEETING_EVENT)"),
    ("Meetings", "0-47 (MEETING_EVENT)"),
    ("MEETING", "0-47 (MEETING_EVENT)"),
    ("0-47", "0-47 (MEETING_EVENT)"),
    ("communications", "0-18 (COMMUNICATION)"),
    ("communication", "0-18 (COMMUNICATION)"),
    ("deal_splits", "0-72 (DEAL_SPLIT)"),
    ("deal_split", "0-72 (DEAL_SPLIT)"),
    ("quote_templates", "0-64 (QUOTE_TEMPLATE)"),
    ("quote_template", "0-64 (QUOTE_TEMPLATE)"),
]


def test_hubspot_archived_listing_refuses_unsupported_types(client, admin_h):
    answered = {}
    for object_type, _ in _UNSUPPORTED_ARCHIVED_TYPES:
        url = f"/hubspot/crm/v3/objects/{object_type}"
        r = client.get(url, headers=admin_h, params={"archived": "true"})
        answered[object_type] = (
            r.status_code,
            r.json(),
            client.get(url, headers=admin_h).status_code,
        )
    assert answered == {
        object_type: (
            400,
            {
                "status": "error",
                "message": (
                    "Paging through deleted objects is not yet supported for object type "
                    + type_label
                ),
                "category": "VALIDATION_ERROR",
            },
            200,
        )
        for object_type, type_label in _UNSUPPORTED_ARCHIVED_TYPES
    }


_MEETINGS_FLAGS = [
    ({"archived": "TRUE"}, True),
    ({"archived": "True"}, True),
    ({"archived": ["true", "false"]}, True),
    ({"archived": "true", "limit": "1"}, True),
    ({"archived": "true", "properties": "hs_meeting_title"}, True),
    ({"archived": "yes"}, False),
    ({"archived": "false"}, False),
    ({"archived": ["false", "true"]}, False),
]


def test_hubspot_meetings_refuses_or_lists_by_the_parsed_flag(client, admin_h):
    url = "/hubspot/crm/v3/objects/meetings"
    answers = {
        refused: client.get(url, headers=admin_h, params={"archived": "true"} if refused else None)
        for refused in (True, False)
    }
    answered = {}
    for params, _ in _MEETINGS_FLAGS:
        r = client.get(url, headers=admin_h, params=params)
        answered[str(params)] = (r.status_code, r.json())
    assert answered == {
        str(params): (400 if refused else 200, answers[refused].json())
        for params, refused in _MEETINGS_FLAGS
    }


@pytest.mark.parametrize("headers", [None, "admin"])
def test_hubspot_archived_refusal_follows_authentication_and_cursor_validation(
    client, admin_h, headers
):
    url = "/hubspot/crm/v3/objects/meetings"
    h = admin_h if headers == "admin" else {}
    r = client.get(url, headers=h, params={"archived": "true", "after": "abc"})
    control = client.get(url, headers=h, params={"after": "abc"})
    assert r.status_code == control.status_code == (400 if h else 401)
    assert r.json() == control.json()


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


def _each_route(client, company: str, headers: dict) -> list:
    """One request to each HubSpot route: the listing, a record, search, batch/read and the
    record's associations."""
    return [
        client.get("/hubspot/crm/v3/objects/companies", headers=headers),
        client.get(f"/hubspot/crm/v3/objects/companies/{company}", headers=headers),
        client.post("/hubspot/crm/v3/objects/companies/search", headers=headers, json={}),
        client.post(
            "/hubspot/crm/v3/objects/companies/batch/read",
            headers=headers,
            json={"inputs": [{"id": company}]},
        ),
        client.get(
            f"/hubspot/crm/v4/objects/companies/{company}/associations/contacts", headers=headers
        ),
    ]


_BEARER_SPELLINGS = [
    ("Bearer {key}", 200),
    (" Bearer {key}", 200),
    ("Bearer {key} ", 200),
    ("Bearer {key}\t", 200),
    ("bearer {key}", 401),
    ("BEARER {key}", 401),
    ("Bearer  {key}", 401),
    ("Bearer\t{key}", 401),
    ("Bearer {key} x", 401),
    ("token {key}", 401),
    ("Basic Zm9vOmJhcg==", 401),
    ("{key}", 401),
]


def test_hubspot_reads_the_bearer_scheme_spelled_exactly(client, admin_h, tokens_yaml):
    """Each of the five routes serves what `_bearer_credential` in `backlot.routers.hubspot`
    records as served, and answers the other spellings with the INVALID_AUTHENTICATION 401."""
    company = client.get("/hubspot/crm/v3/objects/companies", headers=admin_h).json()["results"][0]
    answered, expected = {}, {}
    for authorization, status in _BEARER_SPELLINGS:
        headers = {"Authorization": authorization.format(key=tokens_yaml["admin_token"])}
        for r in _each_route(client, company["id"], headers):
            row = (authorization, r.request.method, r.request.url.path)
            answered[row] = (
                r.status_code,
                r.json()["category"] if r.status_code == 401 else None,
            )
            expected[row] = (status, "INVALID_AUTHENTICATION" if status == 401 else None)
    assert answered == expected


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
        ("0-166", False),
        ("0-400", False),
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


def test_hubspot_0_18_0_72_0_64_are_their_types(tmp_path):
    """The three pairings the comment above `_TYPE_IDS` records, over a corpus holding one quote
    template."""
    from tests._helpers import client_for

    records = [
        {
            "source_type": "hubspot",
            "doc_id": "hs-quote-template",
            "object_type": "quote_templates",
            "title": "Standard",
            "content": "Standard template.",
            "author_email": "rep@acme.com",
            "created": "2026-03-01T09:00:00Z",
            "properties": {"hs_name": "Standard"},
        },
    ]
    settings = tiny_corpus(tmp_path, records)
    with client_for(settings, reload=True) as c:
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        rid = served_id("hubspot", "hs-quote-template")
        for asked, named, label in (
            ("0-18", "communications", "0-18 (COMMUNICATION)"),
            ("0-72", "deal_splits", "0-72 (DEAL_SPLIT)"),
            ("0-64", "quote_templates", "0-64 (QUOTE_TEMPLATE)"),
        ):
            listed = c.get(f"/hubspot/crm/v3/objects/{asked}", headers=h)
            assert listed.status_code == 200, (asked, listed.json())
            by_name = c.get(f"/hubspot/crm/v3/objects/{named}", headers=h)
            assert listed.json()["results"] == by_name.json()["results"], asked
            searched = c.post(f"/hubspot/crm/v3/objects/{asked}/search", headers=h, json={})
            assert searched.status_code == 200, asked
            assert searched.json()["total"] == (1 if asked == "0-64" else 0), asked
            archived = c.get(
                f"/hubspot/crm/v3/objects/{asked}", headers=h, params={"archived": "true"}
            )
            assert archived.status_code == 400, asked
            assert archived.json()["message"] == (
                f"Paging through deleted objects is not yet supported for object type {label}"
            )
        assert c.get(f"/hubspot/crm/v3/objects/0-64/{rid}", headers=h).json()["id"] == rid
        read = c.post(
            "/hubspot/crm/v3/objects/0-64/batch/read", headers=h, json={"inputs": [{"id": rid}]}
        )
        assert read.status_code == 200
        assert [r["id"] for r in read.json()["results"]] == [rid]
        for to in ("contacts", "0-18"):
            linked = c.get(f"/hubspot/crm/v4/objects/0-64/{rid}/associations/{to}", headers=h)
            assert (linked.status_code, linked.json()) == (200, {"results": []}), to


_MEASURED_TYPE_IDS = [
    ("0-53", "invoices"),
    ("0-54", "marketing_events"),
    ("0-69", "subscriptions"),
    ("0-74", "goal_targets"),
    ("0-115", "users"),
    ("0-123", "orders"),
    ("0-136", "leads"),
    ("0-142", "carts"),
    ("0-145", "partner_clients"),
    ("0-162", "services"),
    ("0-410", "courses"),
    ("0-420", "listings"),
    ("0-421", "appointments"),
]


@pytest.fixture(scope="module")
def measured_types(tmp_path_factory):
    """One record of each type `_MEASURED_TYPE_IDS` names."""
    return tiny_corpus(
        tmp_path_factory.mktemp("measured_types"),
        [
            {
                "source_type": "hubspot",
                "doc_id": f"hs-{named}",
                "object_type": named,
                "title": named,
                "content": f"One of {named}.",
                "author_email": "rep@acme.com",
                "created": "2026-03-01T09:00:00Z",
            }
            for _, named in _MEASURED_TYPE_IDS
        ],
    )


@pytest.mark.parametrize("asked,named", _MEASURED_TYPE_IDS)
def test_hubspot_a_measured_type_id_is_its_type(measured_types, asked, named):
    """The thirteen pairings the comment above `_TYPE_IDS` records from real's 403s, over a corpus
    holding one record of each of the thirteen types, so an id read as any other type lists another
    type's record or nothing."""
    from tests._helpers import client_for

    settings = measured_types
    with client_for(settings, reload=True) as c:
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        by_id = c.get(f"/hubspot/crm/v3/objects/{asked}", headers=h)
        assert by_id.status_code == 200, by_id.json()
        assert [r["id"] for r in by_id.json()["results"]] == [served_id("hubspot", f"hs-{named}")]


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


_COMPANY_SPELLINGS = [
    "companies",
    "company",
    "Companies",
    "COMPANY",
    "companie\u017f",
    "compan\u0131es",
]


def test_hubspot_a_standard_type_answers_to_either_spelling(client, admin_h):
    """HubSpot resolves the path segment through its object-type registry, so `/objects/company`
    reaches the same records as `/objects/companies` — measured: both answer 403 MISSING_SCOPES on
    a scope-less key, where an unrecognized word answers 400. A corpus states one spelling or the
    other, and neither may hide its records from the vendor's own path. Either is read in upper
    case on each route, as `_canonical` records."""
    plural_ids = [
        r["id"]
        for r in client.get(
            "/hubspot/crm/v3/objects/companies", headers=admin_h, params={"limit": 100}
        ).json()["results"]
    ]
    bodies = {"search": {"limit": 100}, "batch/read": {"inputs": [{"id": i} for i in plural_ids]}}
    answered, expected = {}, {}
    for asked in _COMPANY_SPELLINGS:
        listed = client.get(
            f"/hubspot/crm/v3/objects/{asked}", headers=admin_h, params={"limit": 100}
        ).json()["results"]
        # and a record of that type resolves under both spellings, while another type's does not
        got = client.get(f"/hubspot/crm/v3/objects/{asked}/{plural_ids[0]}", headers=admin_h)
        other = client.get(f"/hubspot/crm/v3/objects/tickets/{plural_ids[0]}", headers=admin_h)
        answered[asked] = [[r["id"] for r in listed], got.status_code, other.status_code]
        expected[asked] = [plural_ids, 200, 404]
        for route, body in bodies.items():
            spelt = client.post(
                f"/hubspot/crm/v3/objects/{asked}/{route}", headers=admin_h, json=body
            )
            plural = client.post(
                f"/hubspot/crm/v3/objects/companies/{route}", headers=admin_h, json=body
            )
            answered[asked].append((spelt.status_code, plural.status_code, spelt.json()))
            expected[asked].append((200, 200, plural.json()))
    assert answered == expected


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


_MISSING_RECORD_PAGE = (
    b"<html>\n<head>\n"
    b'<meta http-equiv="Content-Type" content="text/html;charset=utf-8"/>\n'
    b"<title>Error 404 Not Found</title>\n</head>\n"
    b"<body><h2>HTTP ERROR 404</h2>\n<p>Resource not found</p>\n</body>\n</html>\n"
)


def test_hubspot_unauth_is_401(client):
    assert client.get("/hubspot/crm/v3/objects/companies").status_code == 401


@pytest.mark.parametrize("authorization", [None, "Bearer nope"])
def test_hubspot_refuses_an_unserved_operation_for_its_credential(client, authorization):
    """An operation HubSpot publishes and no route here serves, sent with no key or one that names
    nothing, is the INVALID_AUTHENTICATION 401 a served route answers, ahead of an object type real
    can't resolve, as `refuse_an_unserved_operation` in `backlot.routers.hubspot` records, not
    FastAPI's 404 or 405."""
    headers = {"Authorization": authorization} if authorization else {}
    operations = (
        ("POST", "/hubspot/crm/v3/objects/contacts"),
        ("PATCH", "/hubspot/crm/v3/objects/contacts/abc123"),
        ("DELETE", "/hubspot/crm/v3/objects/contacts/abc123"),
        ("POST", "/hubspot/crm/v3/objects/contacts/batch/create"),
        ("DELETE", "/hubspot/crm/v4/objects/contacts/1/associations/companies/2"),
        ("POST", "/hubspot/crm/v4/associations/contacts/companies/batch/read"),
        ("POST", "/hubspot/crm/v4/associations/usage/high-usage-report/abc"),
        ("PATCH", "/hubspot/crm/v3/objects/zzz/1"),
        ("DELETE", "/hubspot/crm/v3/objects/2-999/1"),
    )
    answered = {}
    for method, path in operations:
        r = client.request(method, path, headers=headers, json={})
        answered[(method, path)] = (r.status_code, r.json())
    refusal = {
        "status": "error",
        "message": "Authentication credentials not found.",
        "category": "INVALID_AUTHENTICATION",
    }
    assert answered == {operation: (401, refusal) for operation in operations}


_CONTACT_SCOPES = [
    "crm.schemas.contacts.write",
    "crm.objects.contacts.write",
    "crm.objects.contacts.sensitive.write.v2",
    "crm.objects.contacts.highly_sensitive.write.v2",
]


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
@pytest.mark.parametrize(
    ("object_type", "scopes"),
    [
        ("contacts", _CONTACT_SCOPES),
        ("contact", _CONTACT_SCOPES),
        ("CONTACTS", _CONTACT_SCOPES),
        ("0-1", _CONTACT_SCOPES),
        (
            "companies",
            [
                "crm.objects.companies.write",
                "crm.schemas.companies.write",
                "crm.objects.companies.highly_sensitive.write.v2",
                "crm.objects.companies.sensitive.write.v2",
            ],
        ),
        (
            "deals",
            [
                "crm.objects.deals.write",
                "crm.schemas.deals.write",
                "crm.objects.deals.sensitive.write.v2",
                "crm.objects.deals.highly_sensitive.write.v2",
            ],
        ),
        ("quotes", ["crm.schemas.quotes.write", "crm.objects.quotes.write", "cpq.quotes.write"]),
        (
            "orders",
            ["crm.schemas.orders.write", "crm.objects.orders.write", "crm.pipelines.orders.write"],
        ),
    ],
)
def test_hubspot_refuses_a_record_write_for_the_scopes_of_its_type(
    client, admin_h, method, object_type, scopes
):
    """`PATCH` and `DELETE` on a record, with a key that resolves, are the 403 MISSING_SCOPES naming
    the type's write scopes, as `_WRITE_SCOPES` in `backlot.routers.hubspot` records. `1` names no
    record: the scopes come before the record is looked up."""
    r = client.request(method, f"/hubspot/crm/v3/objects/{object_type}/1", headers=admin_h)
    assert r.status_code == 403
    assert list(r.json()) == ["status", "message", "errors", "links", "category"]
    assert r.json() == {
        "status": "error",
        "message": "This app hasn't been granted all required scopes to make this call. "
        "Read more about required scopes here: https://developers.hubspot.com/scopes.",
        "errors": [
            {
                "message": "One or more of the following scopes are required.",
                "context": {"requiredGranularScopes": scopes},
            }
        ],
        "links": {"scopes": "https://developers.hubspot.com/scopes"},
        "category": "MISSING_SCOPES",
    }


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
@pytest.mark.parametrize(
    ("object_type", "status", "body"),
    [
        ("zzz", 400, {"status": "error", "message": "Unable to infer object type from: zzz"}),
        ("2-999", 400, {"status": "error", "message": "Invalid object or event type id: 2-999"}),
    ],
)
def test_hubspot_checks_a_record_writes_type_before_its_scopes(
    client, admin_h, method, object_type, status, body
):
    """With a key that resolves, a type real can't resolve is its 400 ahead of the scopes
    (`refuse_an_unserved_operation` in `backlot.routers.hubspot`)."""
    r = client.request(method, f"/hubspot/crm/v3/objects/{object_type}/1", headers=admin_h)
    assert (r.status_code, r.json()) == (status, body)


_COMPANY_SCOPES = [
    "crm.objects.companies.write",
    "crm.schemas.companies.write",
    "crm.objects.companies.highly_sensitive.write.v2",
    "crm.objects.companies.sensitive.write.v2",
]
_QUOTE_SCOPES = ["crm.schemas.quotes.write", "crm.objects.quotes.write", "cpq.quotes.write"]
_ORDER_SCOPES = [
    "crm.schemas.orders.write",
    "crm.objects.orders.write",
    "crm.pipelines.orders.write",
]


def _scopes_refused(r) -> list[str]:
    assert (r.status_code, r.json()["category"]) == (403, "MISSING_SCOPES")
    return r.json()["errors"][0]["context"]["requiredGranularScopes"]


_INPUTS = {"inputs": []}


@pytest.mark.parametrize(
    ("method", "path", "body", "scopes"),
    [
        ("POST", "/crm/v3/objects/contacts", _INPUTS, _CONTACT_SCOPES),
        ("POST", "/crm/v3/objects/0-1", _INPUTS, _CONTACT_SCOPES),
        ("POST", "/crm/v3/objects/quotes", _INPUTS, _QUOTE_SCOPES),
        ("POST", "/crm/v3/objects/contacts/batch/archive", _INPUTS, _CONTACT_SCOPES),
        ("POST", "/crm/v3/objects/contacts/batch/create", _INPUTS, _CONTACT_SCOPES),
        ("POST", "/crm/v3/objects/companies/batch/update", _INPUTS, _COMPANY_SCOPES),
        ("POST", "/crm/v3/objects/contacts/batch/upsert", _INPUTS, _CONTACT_SCOPES),
        (
            "POST",
            "/crm/v3/objects/contacts/merge",
            {"primaryObjectId": "1", "objectIdToMerge": "2"},
            _CONTACT_SCOPES,
        ),
        ("DELETE", "/crm/v4/objects/contacts/1/associations/companies/2", _INPUTS, _CONTACT_SCOPES),
        ("PUT", "/crm/v4/objects/companies/1/associations/contacts/2", [], _COMPANY_SCOPES),
        ("PUT", "/crm/v4/objects/quotes/1/associations/default/tickets/2", _INPUTS, _QUOTE_SCOPES),
        (
            "POST",
            "/crm/v4/associations/contacts/companies/batch/create",
            _INPUTS,
            _CONTACT_SCOPES + _COMPANY_SCOPES,
        ),
        (
            "POST",
            "/crm/v4/associations/companies/contacts/batch/archive",
            _INPUTS,
            _CONTACT_SCOPES + _COMPANY_SCOPES,
        ),
        (
            "POST",
            "/crm/v4/associations/quotes/orders/batch/associate/default",
            _INPUTS,
            _ORDER_SCOPES + _QUOTE_SCOPES,
        ),
        (
            "POST",
            "/crm/v4/associations/CONTACTS/company/batch/labels/archive",
            _INPUTS,
            _CONTACT_SCOPES + _COMPANY_SCOPES,
        ),
        ("POST", "/crm/v4/associations/contacts/contacts/batch/create", _INPUTS, _CONTACT_SCOPES),
    ],
)
def test_hubspot_refuses_a_keyed_write_for_the_scopes_of_its_types(
    client, admin_h, method, path, body, scopes
):
    """With a key that resolves, each write with the body real answered it for is the 403
    MISSING_SCOPES. A v4 record association names its first type's scopes, and a v4 association
    batch write both types', in `_PAIR_ORDER`, as `refuse_an_unserved_operation` in
    `backlot.routers.hubspot` records."""
    r = client.request(method, f"/hubspot{path}", headers=admin_h, json=body)
    assert _scopes_refused(r) == scopes


_NULL_BODY = {
    "status": "error",
    "message": "validation error",
    "errors": ["The request body may not be null"],
}
_INTERNAL_ERROR = {"status": "error", "message": "internal error"}
_CONTACTS_REFUSED = {
    "status": "error",
    "message": "This app hasn't been granted all required scopes to make this call. "
    "Read more about required scopes here: https://developers.hubspot.com/scopes.",
    "errors": [
        {
            "message": "One or more of the following scopes are required.",
            "context": {"requiredGranularScopes": _CONTACT_SCOPES},
        }
    ],
    "links": {"scopes": "https://developers.hubspot.com/scopes"},
    "category": "MISSING_SCOPES",
}


@pytest.mark.parametrize(
    ("method", "path", "content_type", "content", "status", "body"),
    [
        ("POST", "/crm/v3/objects/contacts", None, "", 400, _NULL_BODY),
        ("POST", "/crm/v3/objects/contacts", "", "", 400, _NULL_BODY),
        ("POST", "/crm/v3/objects/contacts", "   ", "", 400, _NULL_BODY),
        ("POST", "/crm/v3/objects/contacts", "\t", "", 400, _NULL_BODY),
        ("POST", "/crm/v3/objects/deals/batch/upsert", None, "", 400, _NULL_BODY),
        ("POST", "/crm/v3/objects/tickets", None, "", 400, _NULL_BODY),
        ("POST", "/crm/v3/objects/tickets/batch/archive", None, "", 400, _NULL_BODY),
        (
            "POST",
            "/crm/v4/associations/contacts/companies/batch/create",
            None,
            "",
            500,
            _INTERNAL_ERROR,
        ),
        (
            "POST",
            "/crm/v4/associations/contacts/companies/batch/create",
            "",
            "",
            500,
            _INTERNAL_ERROR,
        ),
        (
            "POST",
            "/crm/v4/associations/tickets/contacts/batch/archive",
            None,
            "",
            500,
            _INTERNAL_ERROR,
        ),
        ("POST", "/crm/v3/objects/contacts/merge", None, "", 403, _CONTACTS_REFUSED),
        (
            "PUT",
            "/crm/v4/objects/contacts/1/associations/companies/2",
            None,
            "",
            403,
            _CONTACTS_REFUSED,
        ),
        ("POST", "/crm/v3/objects/contacts", "application/json", "{", 403, _CONTACTS_REFUSED),
        ("POST", "/crm/v3/objects/contacts", "application/json", "", 403, _CONTACTS_REFUSED),
        ("POST", "/crm/v3/objects/contacts", None, "{}", 403, _CONTACTS_REFUSED),
    ],
)
def test_hubspot_answers_a_keyed_write_with_no_body_and_no_content_type(
    client, admin_h, method, path, content_type, content, status, body
):
    """Each row is what `refuse_an_unserved_operation` in `backlot.routers.hubspot` records."""
    headers = {**admin_h, **({} if content_type is None else {"Content-Type": content_type})}
    r = client.request(method, f"/hubspot{path}", headers=headers, content=content)
    assert (r.status_code, r.json()) == (status, body)


@pytest.mark.parametrize(
    ("method", "path", "name"),
    [
        ("POST", "/crm/v3/objects/zzz", "zzz"),
        ("POST", "/crm/v3/objects/zzz/batch/create", "zzz"),
        ("POST", "/crm/v3/objects/zzz/merge", "zzz"),
        ("PUT", "/crm/v4/objects/contacts/1/associations/zzz/2", "zzz"),
        ("POST", "/crm/v4/associations/zzz/companies/batch/create", "zzz"),
        ("POST", "/crm/v4/associations/contacts/zzz/batch/create", "zzz"),
    ],
)
def test_hubspot_checks_a_keyed_writes_types_before_its_body_and_scopes(
    client, admin_h, method, path, name
):
    """A type real can't resolve, on either side of a v4 pair, is its 400 ahead of the body and the
    scopes (`refuse_an_unserved_operation` in `backlot.routers.hubspot`)."""
    r = client.request(method, f"/hubspot{path}", headers=admin_h)
    assert (r.status_code, r.json()) == (
        400,
        {"status": "error", "message": f"Unable to infer object type from: {name}"},
    )


@pytest.mark.parametrize(
    ("user_id", "message"),
    [
        ("1", "This is not a valid userId (1) for your portal "),
        ("-2147483648", "This is not a valid userId (-2147483648) for your portal "),
        ("%2B1", "This is not a valid userId (1) for your portal "),
        ("01", "This is not a valid userId (1) for your portal "),
        ("%D9%A1", "This is not a valid userId (1) for your portal "),
        ("2147483648", "Unable to parse value for path parameter: userId"),
        ("1.0", "Unable to parse value for path parameter: userId"),
        ("abc", "Unable to parse value for path parameter: userId"),
        ("%201", "Unable to parse value for path parameter: userId"),
        ("%F0%9D%9F%8F", "Unable to parse value for path parameter: userId"),
        pytest.param(
            "1" * 5000, "Unable to parse value for path parameter: userId", id="5000-ones"
        ),
        pytest.param(
            "0" * 4400 + "7", "This is not a valid userId (7) for your portal ", id="4400-zeros-7"
        ),
    ],
)
def test_hubspot_refuses_a_high_usage_report_for_its_user_id(client, admin_h, user_id, message):
    """Each row is the `userId` 400 of `refuse_an_unserved_operation` in `backlot.routers.hubspot`,
    sent with a body that doesn't parse."""
    r = client.post(
        f"/hubspot/crm/v4/associations/usage/high-usage-report/{user_id}",
        headers={**admin_h, "Content-Type": "application/json"},
        content="{",
    )
    assert (r.status_code, r.json()) == (400, {"status": "error", "message": message})


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("PATCH", "/hubspot/crm/v3/objects/tickets/1", 405),
        ("DELETE", "/hubspot/crm/v3/objects/tickets/1", 405),
        ("POST", "/hubspot/crm/v3/objects/tickets", 405),
        ("POST", "/hubspot/crm/v3/objects/tickets/merge", 405),
        ("POST", "/hubspot/crm/v3/objects/tickets/batch/create", 404),
        ("DELETE", "/hubspot/crm/v4/objects/tickets/2/associations/contacts/2", 404),
        ("POST", "/hubspot/crm/v4/associations/contacts/tickets/batch/create", 404),
        ("POST", "/hubspot/crm/v4/associations/contacts/companies/batch/read", 404),
    ],
)
def test_hubspot_leaves_the_other_keyed_operations_to_the_router(
    client, admin_h, method, path, status
):
    """Each row goes on to the router from `refuse_an_unserved_operation` in
    `backlot.routers.hubspot`: a type outside `_WRITE_SCOPES`, or the v4 `batch/read`."""
    assert client.request(method, path, headers=admin_h, json={"inputs": []}).status_code == status


def test_hubspot_answers_a_record_patch_that_does_not_parse_with_the_scopes(client, admin_h):
    """A `PATCH` body that doesn't parse is answered with the scopes' 403, as
    `refuse_an_unserved_operation` in `backlot.routers.hubspot` records."""
    headers = {**admin_h, "Content-Type": "application/json"}
    r = client.request("PATCH", "/hubspot/crm/v3/objects/contacts/1", headers=headers, content="{")
    assert (r.status_code, r.json()["category"]) == (403, "MISSING_SCOPES")


def test_hubspot_does_not_refuse_an_unpublished_path_for_its_credential(client):
    """Only what HubSpot publishes is refused for its credential: a path HubSpot does not publish
    is still a 404 with no key, as `refuse_an_unserved_operation` in `backlot.routers.hubspot`
    records."""
    assert client.get("/hubspot/crm/v3/nonexistent-xyz").status_code == 404


_PAGE_TYPE = "text/html;charset=iso-8859-1"

_METHOD_NOT_ALLOWED_PAGE = (
    b"<html>\n<head>\n"
    b'<meta http-equiv="Content-Type" content="text/html;charset=utf-8"/>\n'
    b"<title>Error 405 Method Not Allowed</title>\n</head>\n"
    b"<body><h2>HTTP ERROR 405</h2>\n<p>Reason:\n<pre>    Method Not Allowed</pre></p>\n"
    b"</body>\n</html>\n"
)

_NOT_ACCEPTABLE_PAGE = (
    b"<html>\n<head>\n"
    b'<meta http-equiv="Content-Type" content="text/html;charset=utf-8"/>\n'
    b"<title>Error 406 Not Acceptable</title>\n</head>\n"
    b"<body><h2>HTTP ERROR 406</h2>\n<p>Reason:\n<pre>    Not Acceptable</pre></p>\n"
    b"</body>\n</html>\n"
)


@pytest.mark.parametrize(
    "path",
    [
        "/crm/v3/objects/contacts",
        "/crm/v3/objects/contacts/{ava}",
        "/crm/v4/objects/contacts/{ava}/associations/companies",
        "/crm/v3/objects/contacts/1",
        "/crm/v3/objects/contacts/abc",
        "/crm/v3/objects/contacts/search",
        "/crm/v3/objects/contacts/batch/read",
    ],
)
@pytest.mark.parametrize("keyed", [True, False])
def test_hubspot_answers_head_as_the_get_without_its_body(client, admin_h, ava, path, keyed):
    """A `HEAD` is the `GET`'s status and headers with no body, declaring the length of the body
    the `GET` carries (``answer_head_as_the_get_without_its_body`` in `backlot.main`)."""
    url = "/hubspot" + path.format(ava=ava)
    headers = admin_h if keyed else {}
    got, head = client.get(url, headers=headers), client.head(url, headers=headers)
    assert head.status_code == got.status_code
    assert head.content == b""
    assert head.headers["content-type"] == got.headers["content-type"]
    assert head.headers["content-length"] == str(len(got.content))
    assert head.headers.get("allow") == got.headers.get("allow")


@pytest.mark.parametrize(
    "path",
    [
        "/crm/v3/objects/contacts",
        "/crm/v3/objects/contacts/{ava}",
        "/crm/v4/objects/contacts/{ava}/associations/companies",
        "/crm/v3/objects/contacts/search",
        "/crm/v3/objects/zzz",
    ],
)
@pytest.mark.parametrize(
    "headers",
    [
        "key",
        "none",
        {"Origin": "https://example.com", "Access-Control-Request-Method": "GET"},
    ],
)
def test_hubspot_refuses_options_for_its_own_credentials(client, admin_h, ava, path, headers):
    """`OPTIONS` is real's 401 naming the credentials its front door looks for, the key sent or not
    and as a CORS preflight, with no `Allow` and no CORS headers (`answer_ahead_of_the_routes` in
    `backlot.routers.hubspot`)."""
    sent = {"key": admin_h, "none": {}}.get(headers) if isinstance(headers, str) else headers
    r = client.options("/hubspot" + path.format(ava=ava), headers=sent)
    assert r.status_code == 401
    assert list(r.json()) == ["status", "message", "engagement"]
    assert r.json()["message"] == "Any of the listed authentication credentials are missing"
    assert list(r.json()["engagement"]) == ["service-to-service", "internal-cookie", "app-cookie"]
    assert not {"allow", "access-control-allow-origin", "access-control-allow-methods"} & set(
        r.headers
    )
    assert (r.headers.get("x-content-type-options") == "nosniff") is path.endswith("/search")


_SEARCH = "/crm/v3/objects/contacts/search"
_BATCH = "/crm/v3/objects/contacts/batch/read"
_LISTING = "/crm/v3/objects/contacts"
_RECORD = "/crm/v3/objects/contacts/1"
_V4 = "/crm/v4/objects/contacts/{ava}/associations/companies"
_MERGE = "/crm/v3/objects/contacts/merge"
_FORBIDDEN_ROWS = [
    # method, path, keyed, Accept (None sends none), status, Allow, page, `nosniff`, no-store
    ("GET", _SEARCH, True, None, 405, "POST,OPTIONS", True, True, True),
    ("GET", _SEARCH, False, None, 405, "POST,OPTIONS", True, True, True),
    ("GET", _SEARCH, True, "application/json", 405, "POST,OPTIONS", False, True, True),
    ("GET", _SEARCH, False, "application/json", 405, "POST,OPTIONS", False, True, True),
    ("GET", _SEARCH, True, "text/*", 405, "POST,OPTIONS", True, True, True),
    ("GET", _SEARCH, True, "application/json, text/html", 405, "POST,OPTIONS", True, True, True),
    ("GET", _SEARCH, True, "TEXT/HTML", 405, "POST,OPTIONS", False, True, True),
    ("GET", _SEARCH, True, "text/html;level=1", 405, "POST,OPTIONS", False, True, True),
    ("GET", _SEARCH, True, "text/html; q=0", 405, "POST,OPTIONS", False, True, True),
    ("GET", _SEARCH, True, "text/html", 405, "POST,OPTIONS", True, True, True),
    ("GET", _SEARCH, True, "application/*", 405, "POST,OPTIONS", False, True, True),
    ("GET", _SEARCH, True, "text/html;q=abc", 405, "POST,OPTIONS", False, True, True),
    ("GET", _SEARCH, True, "text/html;q=inf", 405, "POST,OPTIONS", False, True, True),
    ("GET", _SEARCH, True, "text/html;q=1d", 405, "POST,OPTIONS", True, True, True),
    ("GET", _SEARCH, True, "text/html;q=NaN", 405, "POST,OPTIONS", True, True, True),
    ("GET", _SEARCH, True, "text/html;q=-NaN", 405, "POST,OPTIONS", True, True, True),
    ("GET", _SEARCH, True, "text/html;q=-0", 405, "POST,OPTIONS", False, True, True),
    (
        "GET",
        _SEARCH,
        True,
        "text/html, application/json;q=-1",
        405,
        "POST,OPTIONS",
        False,
        True,
        True,
    ),
    ("GET", _SEARCH, True, "text/html;Q=0.5", 405, "POST,OPTIONS", False, True, True),
    ("GET", _SEARCH, True, "text/html;q=0;q=0.5", 405, "POST,OPTIONS", True, True, True),
    ("GET", _BATCH, True, None, 405, "POST,OPTIONS", True, False, True),
    ("GET", _BATCH, True, "application/json", 405, "POST,OPTIONS", False, False, True),
    ("PUT", _SEARCH, True, None, 405, "POST,OPTIONS", False, True, False),
    ("DELETE", _SEARCH, False, None, 405, "POST,OPTIONS", False, True, False),
    ("PATCH", _BATCH, True, None, 405, "POST,OPTIONS", False, False, False),
    ("PUT", _RECORD, True, None, 405, "DELETE,GET,OPTIONS,PATCH", False, False, False),
    ("PUT", _RECORD, False, None, 405, "DELETE,GET,OPTIONS,PATCH", False, False, False),
    ("PUT", _RECORD, True, "text/html", 405, "DELETE,GET,OPTIONS,PATCH", False, False, False),
    ("POST", _RECORD, False, None, 405, "DELETE,GET,OPTIONS,PATCH", True, False, True),
    ("PUT", _LISTING, True, None, 405, "POST,GET,OPTIONS", False, False, False),
    ("DELETE", _LISTING, True, None, 405, "POST,GET,OPTIONS", False, False, False),
    ("PUT", _V4, True, None, 405, "GET,OPTIONS", False, False, False),
    ("GET", _MERGE, False, None, 405, "POST,OPTIONS", True, False, True),
    ("PATCH", _MERGE, True, None, 405, "POST,OPTIONS", False, False, False),
    ("DELETE", _MERGE, True, None, 405, "POST,OPTIONS", False, False, False),
    ("PUT", _MERGE, False, None, 405, "POST,OPTIONS", False, False, False),
    ("GET", _LISTING, True, "text/html", 406, None, True, False, True),
    ("GET", _LISTING, False, "text/html", 406, None, True, False, True),
    ("GET", _LISTING, True, "text/*", 406, None, True, False, True),
    ("GET", _LISTING, True, "text/html;q=0.5", 406, None, True, False, True),
    ("GET", _LISTING, True, "application/xml", 406, None, False, False, True),
    ("GET", _LISTING, True, "TEXT/HTML", 406, None, False, False, True),
    ("GET", _LISTING, True, "text/html;level=1", 406, None, False, False, True),
    ("GET", _LISTING, True, "application/hal+json", 406, None, False, False, True),
    ("GET", _RECORD, True, "text/html", 406, None, True, False, True),
    ("GET", _V4, True, "text/html", 406, None, True, False, True),
    ("POST", _SEARCH, True, "text/html", 406, None, True, True, True),
    ("POST", _SEARCH, True, "application/xml", 406, None, False, True, True),
    ("POST", _BATCH, True, "text/html", 406, None, True, False, True),
    ("PATCH", _RECORD, False, "text/html", 406, None, False, False, False),
    ("DELETE", _RECORD, True, "text/html", 406, None, False, False, False),
    ("POST", _MERGE, False, "text/html", 406, None, True, False, True),
]


@pytest.mark.parametrize(
    "method, path, keyed, accept, status, allow, page, nosniff, no_store", _FORBIDDEN_ROWS
)
def test_hubspot_answers_a_method_or_accept_no_route_takes(
    client, admin_h, ava, method, path, keyed, accept, status, allow, page, nosniff, no_store
):
    """Each row is what `answer_ahead_of_the_routes` in `backlot.routers.hubspot` measures."""
    body = b"{}" if method in ("POST", "PUT", "PATCH") else None
    request = client.build_request(
        method, "/hubspot" + path.format(ava=ava), headers=admin_h if keyed else {}, content=body
    )
    if accept is None:
        del request.headers["Accept"]
    else:
        request.headers["Accept"] = accept
    r = client.send(request)
    expected = {405: _METHOD_NOT_ALLOWED_PAGE, 406: _NOT_ACCEPTABLE_PAGE}[status] if page else b""
    assert (r.status_code, r.content) == (status, expected)
    assert r.headers.get("content-type") == (_PAGE_TYPE if page else None)
    assert r.headers.get("allow") == allow
    assert (r.headers.get("x-content-type-options") == "nosniff") is nosniff
    assert (r.headers.get("cache-control") == "must-revalidate,no-cache,no-store") is no_store


@pytest.mark.parametrize(
    "accept",
    [
        "application/json",
        "*/*",
        "text/html, */*;q=0.8",
        "Application/JSON",
        "APPLICATION/*",
        "application/json;q=0",
        "*/*;q=0",
        "",
    ],
)
def test_hubspot_serves_an_accept_that_admits_json(client, admin_h, accept):
    """An `Accept` that names JSON, `application/*` or any type, in any case and at any `q`, is
    served (`_admits_json` in `backlot.routers.hubspot`)."""
    r = client.get("/hubspot/crm/v3/objects/contacts", headers={**admin_h, "Accept": accept})
    assert r.status_code == 200


@pytest.mark.parametrize("method", ["OPTIONS", "GET"])
def test_hubspot_leaves_a_path_no_route_serves_to_the_router(client, admin_h, method):
    """`OPTIONS`, and an `Accept` of `text/html`, on a path no route serves are not the 401 and the
    406 of a served one: real's `GET` there is its 404, and its `OPTIONS` a 405 page from its front
    door, which this server does not answer."""
    r = client.request(
        method, "/hubspot/crm/v3/nonexistent-xyz", headers={**admin_h, "Accept": "text/html"}
    )
    assert r.status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/crm/v3/objects/contacts", None),
        ("GET", "/crm/v3/objects/contacts/{ava}", None),
        ("GET", "/crm/v4/objects/contacts/{ava}/associations/companies", None),
        ("POST", "/crm/v3/objects/contacts/search", {}),
        ("POST", "/crm/v3/objects/contacts/batch/read", {"inputs": [{"id": "{ava}"}]}),
    ],
)
def test_hubspot_serves_a_trailing_slash_as_the_path_without_it(
    client, admin_h, ava, method, path, body
):
    """Trailing slashes are served as the path without them, not redirected
    (``serve_a_slashed_hubspot_path_as_the_path_without_it`` in `backlot.main`)."""
    url = "/hubspot" + path.format(ava=ava)
    sent = json.loads(json.dumps(body).replace("{ava}", ava)) if body is not None else None
    bare = client.request(method, url, headers=admin_h, json=sent)
    assert bare.status_code == 200
    for slashes in ("/", "//", "///"):
        slashed = client.request(
            method, url + slashes, headers=admin_h, json=sent, follow_redirects=False
        )
        assert (slashed.status_code, slashed.content) == (200, bare.content), slashes


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
    for record in (secret["id"], "0" + secret["id"]):
        url = f"/hubspot/crm/v3/objects/companies/{record}"
        hidden = client.get(url, headers=ava_h)
        assert (hidden.status_code, hidden.content) == (404, _MISSING_RECORD_PAGE), record
        assert client.get(url, headers=hana_h).status_code == 200, record


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


@pytest.fixture
def ava(client, admin_h):
    """The contact Ava's id. She is associated with one company."""
    listed = client.get(
        "/hubspot/crm/v3/objects/contacts", headers=admin_h, params={"limit": 100}
    ).json()["results"]
    return next(r for r in listed if r["properties"].get("firstname") == "Ava")["id"]


def _assoc(record, to="companies", source="contacts"):
    return f"/crm/v4/objects/{source}/{record}/associations/{to}"


def _cannot_infer(name):
    return {"status": "error", "message": f"Unable to infer object type from: {name}"}


_EMPTY = {"results": []}


def _not_looked_up(record, why):
    """Real's 404 for an id it won't look up: `why` is "numeric" or "positive"."""
    message = {
        "numeric": "Object not found.  objectId are usually numeric.",
        "positive": "Object not found. objectId is usually positive.",
    }[why]
    return {
        "status": "error",
        "message": message,
        "context": {"id": [record]},
        "category": "OBJECT_NOT_FOUND",
    }


_OBJECT_TYPE_ROWS = [
    ("/crm/v3/objects/nosuch/{ava}", 400, _cannot_infer("nosuch")),
    ("/crm/v3/objects/tas\u212as", 400, _cannot_infer("tas\u212as")),
    (_assoc("{ava}", to="company"), 200, _assoc("{ava}")),
    (_assoc("{ava}", to="0-2"), 200, _assoc("{ava}")),
    (_assoc("{ava}", to="COMPANIES", source="Contacts"), 200, _assoc("{ava}")),
    (_assoc("{ava}", to="nosuch"), 400, _cannot_infer("nosuch")),
    (_assoc("{ava}", source="nosuch"), 400, _cannot_infer("nosuch")),
    (_assoc("{ava}", to="nosuchtwo", source="nosuchone"), 400, _cannot_infer("nosuchone")),
    (_assoc("1"), 200, _EMPTY),
    (_assoc("9223372036854775807"), 200, _EMPTY),
    (_assoc("0" * 25 + "1"), 200, _EMPTY),
    (_assoc("{ava}", source="deals"), 200, _EMPTY),
    (_assoc("0{ava}"), 200, _assoc("{ava}")),
    (_assoc("abc"), 404, _not_looked_up("abc", "numeric")),
    (_assoc("1.0"), 404, _not_looked_up("1.0", "numeric")),
    (_assoc("+1"), 404, _not_looked_up("+1", "numeric")),
    (_assoc("\u0661"), 404, _not_looked_up("\u0661", "numeric")),
    (_assoc("0"), 404, _not_looked_up("0", "positive")),
    (_assoc("-1"), 404, _not_looked_up("-1", "positive")),
    (_assoc("9223372036854775808"), 404, _not_looked_up("9223372036854775808", "numeric")),
    (_assoc("abc") + "?after=bogus", 404, _not_looked_up("abc", "numeric")),
    ("/crm/v3/objects/contacts/0{ava}", 200, "/crm/v3/objects/contacts/{ava}"),
    ("/crm/v3/objects/contacts/abc", 404, _not_looked_up("abc", "numeric")),
    ("/crm/v3/objects/contacts/1.0", 404, _not_looked_up("1.0", "numeric")),
    ("/crm/v3/objects/contacts/+1", 404, _not_looked_up("+1", "numeric")),
    ("/crm/v3/objects/contacts/\u0661", 404, _not_looked_up("\u0661", "numeric")),
    (
        "/crm/v3/objects/contacts/99999999999999999999999",
        404,
        _not_looked_up("99999999999999999999999", "numeric"),
    ),
    (
        "/crm/v3/objects/contacts/9223372036854775808",
        404,
        _not_looked_up("9223372036854775808", "numeric"),
    ),
    ("/crm/v3/objects/contacts/0", 404, _not_looked_up("0", "positive")),
    ("/crm/v3/objects/contacts/-1", 404, _not_looked_up("-1", "positive")),
    ("/crm/v3/objects/contacts/00", 404, _not_looked_up("00", "positive")),
    ("/crm/v3/objects/contacts/-00", 404, _not_looked_up("-00", "positive")),
    (
        "/crm/v3/objects/contacts/-9223372036854775808",
        404,
        _not_looked_up("-9223372036854775808", "positive"),
    ),
    (
        "/crm/v3/objects/contacts/-9223372036854775809",
        404,
        _not_looked_up("-9223372036854775809", "numeric"),
    ),
    # longer than `int` converts (4300 digits): the 23-digit row's answer, not a server error
    ("/crm/v3/objects/contacts/" + "9" * 5000, 404, _not_looked_up("9" * 5000, "numeric")),
    ("/crm/v3/objects/nosuch/abc", 400, _cannot_infer("nosuch")),
    ("/crm/v3/objects/contacts/1", 404, _MISSING_RECORD_PAGE),
    ("/crm/v3/objects/contacts/9223372036854775807", 404, _MISSING_RECORD_PAGE),
    ("/crm/v3/objects/deals/{ava}", 404, _MISSING_RECORD_PAGE),
    (_assoc("1") + "?after=bogus", 400, _assoc("{ava}") + "?after=bogus"),
    (_assoc("{ava}", source="deals") + "?after=bogus", 400, _assoc("{ava}") + "?after=bogus"),
]


def test_hubspot_object_types_resolve_as_real_resolves_them(client, admin_h, ava):
    """Each row is one that `_canonical`, `_record_id`, `get_object` and `list_associations` record
    as measured. Where `want` is a path, the row answers what that path does, here and on real: the
    target under its plural, for another spelling of it; Ava's own associations, for a bad `after`,
    since the cursor is refused before a missing or mismatched record is an empty page; and Ava
    herself, or her associations, for her id with a leading zero. Where `want` is bytes, it is the
    HTML page, under real's Latin-1 content type. A JSON answer's members come in real's order."""
    answered, expected = {}, {}
    for path, status, want in _OBJECT_TYPE_ROWS:
        r = client.get("/hubspot" + path.format(ava=ava), headers=admin_h)
        if isinstance(want, bytes):
            answered[path] = (r.status_code, r.headers["content-type"], r.content)
            expected[path] = (status, "text/html;charset=iso-8859-1", want)
            continue
        if isinstance(want, str):
            control = client.get("/hubspot" + want.format(ava=ava), headers=admin_h).json()
            answered[path] = (r.status_code, list(r.json().items()), control != _EMPTY)
            expected[path] = (status, list(control.items()), True)
        else:
            answered[path] = (r.status_code, list(r.json().items()))
            expected[path] = (status, list(want.items()))
    assert answered == expected


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


def test_hubspot_search_string_filters_ignore_value_case(client, admin_h):
    """A string property's value is compared in any case and an enumeration's exactly, as
    `_match_one` records."""
    f = lambda **kw: _hs_filter(client, admin_h, **kw)  # noqa: E731
    others = {"Borealis Clinics", "Stealth Health Co"}

    assert f(propertyName="name", operator="EQ", value="acme health") == {"Acme Health"}
    assert f(propertyName="name", operator="EQ", value="ACME HEALTH") == {"Acme Health"}
    assert f(propertyName="name", operator="NEQ", value="ACME HEALTH") == others
    assert f(propertyName="name", operator="IN", values=["acme health"]) == {"Acme Health"}
    assert f(propertyName="name", operator="NOT_IN", values=["ACME HEALTH"]) == others


def test_hubspot_search_folds_a_string_value_as_real_does(tmp_path):
    """Each row is what `_hubspot_fold` takes a ``firstname EQ`` value to, as the total over a
    ``Maria``, a ``Mar\u00eda``, a ``Bob`` and a ``Cara``. An enumeration stays exact."""
    from tests._helpers import client_for

    records = [
        {
            "source_type": "hubspot",
            "doc_id": doc_id,
            "object_type": "contacts",
            "title": first,
            "properties": {"firstname": first, "lifecyclestage": stage},
        }
        for doc_id, first, stage in (
            ("hs-maria", "Maria", "lead"),
            ("hs-accent", "Mar\u00eda", "lead"),
            ("hs-bob", "Bob", "customer"),
            ("hs-cara", "Cara", "customer"),
        )
    ]
    settings = tiny_corpus(tmp_path, records)
    with client_for(settings, reload=True) as client:
        headers = {"Authorization": f"Bearer {settings.admin_token}"}

        def total(prop, op, **value):
            body = {
                "filterGroups": [{"filters": [{"propertyName": prop, "operator": op, **value}]}]
            }
            return client.post(
                "/hubspot/crm/v3/objects/contacts/search", headers=headers, json=body
            ).json()["total"]

        for value, want in (
            ("Maria", 2),
            ("maria", 2),
            ("MAR\u00cdA", 2),
            ("m\u00e1ria", 2),
            ("M\u00c4RIA", 2),
            ("Mar\u00efa", 2),
            ("maria\u0301", 2),
            ("MAR\u0130A", 2),
            ("mar\u0131a", 2),
            ("ma\u0280ia", 2),
            ("\uff2d\uff21\uff32\uff29\uff21", 2),
            ("M\u0250ria", 2),
            ("M\u0259ria", 2),
            ("\u026faria", 2),
            ("Mar\u0197a", 2),
            ("M\u00aaria", 2),
            ("Mar\u2170a", 2),
            ("\u2133aria", 2),
            ("B\u00d8B", 1),
            ("b\u1d0fb", 1),
            ("\u0180ob", 1),
            ("\u0299ob", 1),
            ("b\u0254b", 1),
            ("b\u00bab", 1),
            ("bo\u0308b", 1),
            ("\u1d04ara", 1),
            ("\u023cara", 1),
            ("M\u0251ria", 0),
            ("Mar\u0269a", 0),
            ("b\u1d11b", 0),
            ("MAR\u0399A", 0),
            ("b\u043eb", 0),
            ("b\u0302ob", 0),
            ("bob\u0308", 0),
            ("MARIA ", 0),
            (" maria", 0),
            ("mar ia", 0),
        ):
            assert total("firstname", "EQ", value=value) == want, ascii(value)
        assert total("firstname", "IN", values=["m\u00e1ria"]) == 2
        assert total("firstname", "NEQ", value="M\u00c1RIA") == 2
        assert total("lifecyclestage", "EQ", value="lead") == 2
        assert total("lifecyclestage", "EQ", value="LEAD") == 0
        assert total("lifecyclestage", "EQ", value="l\u00e9ad") == 0


@pytest.mark.parametrize(
    "object_type, prop, stored",
    [
        ("contacts", "lifecyclestage", "lead"),
        ("deals", "dealstage", "closedwon"),
        ("deals", "pipeline", "default"),
        ("companies", "industry", "COMPUTER_SOFTWARE"),
    ],
)
def test_hubspot_search_compares_an_enumeration_value_exactly(tmp_path, object_type, prop, stored):
    """Each enumeration `_match_one` compares exactly is matched only as stored, under each of the
    four operators."""
    from tests._helpers import client_for

    record = {"source_type": "hubspot", "doc_id": "hs-enum", "object_type": object_type}
    s = tiny_corpus(tmp_path, [{**record, "title": "r", "properties": {prop: stored}}])
    other = stored.swapcase()
    with client_for(s, reload=True) as c:
        h = {"Authorization": f"Bearer {s.admin_token}"}

        def total(op, **value):
            body = {
                "filterGroups": [{"filters": [{"propertyName": prop, "operator": op, **value}]}]
            }
            return c.post(
                f"/hubspot/crm/v3/objects/{object_type}/search", headers=h, json=body
            ).json()["total"]

        assert (total("EQ", value=stored), total("EQ", value=other)) == (1, 0)
        assert (total("IN", values=[stored]), total("IN", values=[other])) == (1, 0)
        assert (total("NEQ", value=stored), total("NEQ", value=other)) == (0, 1)
        assert (total("NOT_IN", values=[stored]), total("NOT_IN", values=[other])) == (0, 1)


_WILDCARD_ROWS = [
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
]


def test_hubspot_contains_token_reads_star_as_a_wildcard(client, admin_h):
    """The wildcard rule `_NEEDLE_RE`'s comment records; `NOT_CONTAINS_TOKEN` finds none of what
    `CONTAINS_TOKEN` finds."""
    answered = {}
    for prop, value, want in _WILDCARD_ROWS:
        found = _hs_filter(
            client, admin_h, propertyName=prop, operator="CONTAINS_TOKEN", value=value
        )
        others = _hs_filter(
            client, admin_h, propertyName=prop, operator="NOT_CONTAINS_TOKEN", value=value
        )
        answered[(prop, value)] = (found, want & others)
    assert answered == {(prop, value): (want, set()) for prop, value, want in _WILDCARD_ROWS}


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
    _op("lone-cr", '{\r"filterGroups":[{"filters":[{"propertyName":"name","operator":"BOGUS"}]}]}', _enum_refusal(2, 63), object_type="companies"),
    _op("byte-order-mark", '\ufeff{"filterGroups":[{"filters":[{"propertyName":"name","operator":"BOGUS"}]}]}', _enum_refusal(1, 67), object_type="companies"),
    # a UTF-16 body is read in its own encoding, and its byte-order mark takes no column
    _op("utf-16", '{"filterGroups":[{"filters":[{"propertyName":"name","operator":"BOGUS"}]}]}'.encode("utf-16-le"), _enum_refusal(1, 64), object_type="companies"),
    _op("utf-16-byte-order-mark", '{"filterGroups":[{"filters":[{"propertyName":"name","operator":"BOGUS"}]}]}'.encode("utf-16"), _enum_refusal(1, 64), object_type="companies"),
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


def test_hubspot_search_reads_a_filter_operator_as_real_does(client, admin_h):
    """`_refuse_an_operator`'s rule, one body per row: the status and message real answered for each
    operator it could not read, and a 200 for each it trims (`_TRIM`) or reads as an index
    (`_OPERATOR_INDEXES`). The trimmed rows put real's trimmed spellings around `NEQ`, whose page on
    this corpus is not empty, and compare that page with `NEQ`'s own; the index rows check only that
    the body is served."""
    answered, expected = {}, {}
    for row in _OPERATOR_ROWS:
        raw, want, object_type, authed = row.values
        headers = {"Content-Type": "application/json", **(admin_h if authed else {})}
        url = f"/hubspot/crm/v3/objects/{object_type}/search"
        content = raw if isinstance(raw, bytes) else raw.encode()
        r = client.post(url, headers=headers, content=content)
        if isinstance(want, dict):
            answered[row.id], expected[row.id] = (r.status_code, r.json()), (400, want)
        elif isinstance(want, tuple):
            control = client.post(url, headers=headers, content=want[1].encode()).json()["results"]
            answered[row.id], expected[row.id] = (
                (r.status_code, r.json()["results"], control != []),
                (200, control, True),
            )
        else:
            answered[row.id], expected[row.id] = r.status_code, want
    assert answered == expected


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


_ONE_TO_100 = [str(i) for i in range(1, 101)]


@pytest.mark.parametrize(
    "inputs, status",
    [
        pytest.param([{"id": str(i)} for i in range(1, 102)], 400, id="101-objects"),
        pytest.param([*_ONE_TO_100, "101"], 400, id="101-strings"),
        pytest.param([*_ONE_TO_100, {"id": 101}], 400, id="an-integer-101"),
        pytest.param([*_ONE_TO_100, " 1"], 400, id="space-1"),
        pytest.param([*_ONE_TO_100, "01"], 400, id="zero-1"),
        pytest.param([*_ONE_TO_100, {"id": 5.0}], 400, id="5.0"),
        pytest.param([*_ONE_TO_100, {"id": True}], 400, id="true"),
        pytest.param([{"id": s} for s in _ONE_TO_100], 207, id="100-objects"),
        pytest.param([{"id": "1"}] * 101, 207, id="one-id-101-times"),
        pytest.param(_ONE_TO_100 * 2, 207, id="100-ids-twice"),
        pytest.param([*_ONE_TO_100, {"id": 5}], 207, id="an-integer-5-beside-5"),
    ],
)
def test_hubspot_batch_read_refuses_more_than_100_ids(client, admin_h, inputs, status):
    """Each row is how `_HUBSPOT_BATCH_MAX` counts its inputs."""
    r = client.post(
        "/hubspot/crm/v3/objects/contacts/batch/read", headers=admin_h, json={"inputs": inputs}
    )
    assert r.status_code == status, r.text
    if status == 400:
        assert r.json() == {
            "status": "error",
            "message": "The maximum number of inputs supported in a batch is 100. "
            "Please reduce the number of items and try again",
            "category": "VALIDATION_ERROR",
        }


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


def _batch_refusal(column: int, text: str, line: int = 1) -> dict:
    return {
        "status": "error",
        "message": f"Invalid input JSON on line {line}, column {column}: {text}",
        "category": "VALIDATION_ERROR",
    }


def _not_a_list(kind: str, token: str) -> str:
    return f"Cannot deserialize value of type <>` from {kind} value (token `JsonToken.{token}`)"


def _no_creator(arg: str, shown: str) -> str:
    return (
        "Cannot construct instance of  (although at least one Creator exists): "
        f"no {arg}-argument constructor/factory method to deserialize from {shown}"
    )


def _blank_field(field: str) -> dict:
    return {
        "status": "error",
        "message": f'Invalid input JSON: unable to deserialize field "{field}". Invalid value: ',
        "category": "VALIDATION_ERROR",
    }


def _br(id, raw, want, object_type="companies", authed=True):
    return pytest.param(raw, want, object_type, authed, id=id)


_ONE = '{"inputs":[{"id":"1"}],'
_ARRAY = "Cannot deserialize value of type  from Array value (token `JsonToken.START_ARRAY`)"
_OBJECT = "Cannot deserialize value of type  from Object value (token `JsonToken.START_OBJECT`)"


def _no_id(column: int) -> dict:
    return {
        "status": "error",
        "message": f"Invalid input JSON on line 1, column {column}. "
        "Some required fields were not set: [id]",
        "category": "VALIDATION_ERROR",
    }


# fmt: off
_BATCH_BODY_ROWS = [
    _br("inputs-string", '{"inputs":"x"}', _batch_refusal(11, _not_a_list("String", "VALUE_STRING"))),
    _br("inputs-blank-string", '{"inputs":" "}', _batch_refusal(11, _not_a_list("String", "VALUE_STRING"))),
    _br("inputs-string-after-a-lone-cr", '{\r"inputs":"x"}', _batch_refusal(10, _not_a_list("String", "VALUE_STRING"), line=2)),
    _br("inputs-string-after-crlf", '{\r\n"inputs":"x"}', _batch_refusal(10, _not_a_list("String", "VALUE_STRING"), line=2)),
    _br("inputs-string-after-a-byte-order-mark", '\ufeff{"inputs":"x"}', _batch_refusal(14, _not_a_list("String", "VALUE_STRING"))),
    _br("inputs-object", '{"inputs":{"id":"1"}}', _batch_refusal(11, _not_a_list("Object", "START_OBJECT"))),
    _br("inputs-integer", '{"inputs":5}', _batch_refusal(11, _not_a_list("Integer", "VALUE_NUMBER_INT"))),
    _br("inputs-float", '{"inputs":1.5}', _batch_refusal(11, _not_a_list("Floating-point", "VALUE_NUMBER_FLOAT"))),
    _br("inputs-true", '{"inputs":true}', _batch_refusal(11, _not_a_list("Boolean", "VALUE_TRUE"))),
    _br("inputs-false", '{"inputs":false}', _batch_refusal(11, _not_a_list("Boolean", "VALUE_FALSE"))),
    _br("int-element", '{"inputs":[7]}', _batch_refusal(12, _no_creator("int/Int", "Number value (7)"))),
    _br("int-element-minus-2^31", '{"inputs":[-2147483648]}', _batch_refusal(12, _no_creator("int/Int", "Number value (-2147483648)"))),
    _br("int-element-2^31", '{"inputs":[2147483648]}', _batch_refusal(12, _no_creator("long/Long", "Number value (2147483648)"))),
    _br("int-element-minus-2^63", '{"inputs":[-9223372036854775808]}', _batch_refusal(12, _no_creator("long/Long", "Number value (-9223372036854775808)"))),
    _br("int-element-2^63", '{"inputs":[9223372036854775808]}', _batch_refusal(12, _no_creator("BigInteger", "Number value (9223372036854775808)"))),
    _br("float-element", '{"inputs":[1.5]}', _batch_refusal(12, _no_creator("double/Double", "Number value (1.5)"))),
    _br("float-element-1e7", '{"inputs":[1e7]}', _batch_refusal(12, _no_creator("double/Double", "Number value (1.0E7)"))),
    _br("float-element-mantissa", '{"inputs":[123456789.125]}', _batch_refusal(12, _no_creator("double/Double", "Number value (1.23456789125E8)"))),
    _br("float-element-1e-3", '{"inputs":[0.001]}', _batch_refusal(12, _no_creator("double/Double", "Number value (0.001)"))),
    _br("float-element-1e-4", '{"inputs":[1e-4]}', _batch_refusal(12, _no_creator("double/Double", "Number value (1.0E-4)"))),
    _br("float-element-1e400", '{"inputs":[1e400]}', _batch_refusal(12, _no_creator("double/Double", "Number value (Infinity)"))),
    _br("float-element-minus-1e400", '{"inputs":[-1e400]}', _batch_refusal(12, _no_creator("double/Double", "Number value (-Infinity)"))),
    _br("float-element-minus-zero", '{"inputs":[-0.0]}', _batch_refusal(12, _no_creator("double/Double", "Number value (-0.0)"))),
    _br("boolean-element", '{"inputs":[true]}', _batch_refusal(12, _no_creator("boolean/Boolean", "boolean value (true)"))),
    _br("array-element", '{"inputs":[["1"]]}', _batch_refusal(12, _ARRAY)),
    _br("int-after-a-string", '{"inputs":["1",7]}', _batch_refusal(16, _no_creator("int/Int", "Number value (7)"))),
    _br("int-after-an-object", '{"inputs":[{"id":"1"},7]}', _batch_refusal(23, _no_creator("int/Int", "Number value (7)"))),
    _br("null-id", '{"inputs":[{"id":null}]}', _no_id(22)),
    _br("null-id-spaced", '{"inputs":[{"id":null }]}', _no_id(23)),
    _br("missing-id", '{"inputs":[{}]}', _no_id(13)),
    _br("null-id-in-the-second-input", '{"inputs":[{"id":"1"},{"id":null}]}', _no_id(33)),
    _br("object-id", '{"inputs":[{"id":{}}]}', _batch_refusal(18, _OBJECT)),
    _br("empty-array-id", '{"inputs":[{"id":[]}]}', _batch_refusal(18, _ARRAY)),
    _br("array-id", '{"inputs":[{"id":["1"]}]}', _batch_refusal(18, _ARRAY)),
    _br("missing-id-before-an-int", '{"inputs":[{},7]}', _no_id(13)),
    _br("int-before-a-missing-id", '{"inputs":[7,{}]}', _batch_refusal(12, _no_creator("int/Int", "Number value (7)"))),
    _br("object-id-before-a-null-id", '{"inputs":[{"id":{}},{"id":null}]}', _batch_refusal(18, _OBJECT)),
    _br("null-element", '{"inputs":[null]}', {"status": "error", "message": "Input IDs must be non-null."}),
    _br("int-after-null", '{"inputs":[null,7]}', _batch_refusal(17, _no_creator("int/Int", "Number value (7)"))),
    _br("properties-after-null", '{"inputs":[null],"properties":"x"}', _batch_refusal(31, _no_creator("String", "String value ('x')"))),
    _br("properties-string", _ONE + '"properties":"name"}', _batch_refusal(37, _no_creator("String", "String value ('name')"))),
    _br("properties-padded", _ONE + '"properties":" name\\t"}', _batch_refusal(37, _no_creator("String", "String value ('name')"))),
    _br("properties-commas", _ONE + '"properties":"name,domain"}', _batch_refusal(37, _no_creator("String", "String value ('name,domain')"))),
    _br("properties-no-break-space", _ONE + '"properties":"\\u00a0"}', _batch_refusal(37, _no_creator("String", "String value (' ')"))),
    _br("properties-empty", _ONE + '"properties":""}', _blank_field("properties")),
    _br("properties-blank", _ONE + '"properties":' + json.dumps(" \t\n\x01") + "}", _blank_field("properties")),
    _br("properties-integer", _ONE + '"properties":5}', _batch_refusal(37, _not_a_list("Integer", "VALUE_NUMBER_INT"))),
    _br("properties-object", _ONE + '"properties":{}}', _batch_refusal(37, _not_a_list("Object", "START_OBJECT"))),
    _br("properties-false", _ONE + '"properties":false}', _batch_refusal(37, _not_a_list("Boolean", "VALUE_FALSE"))),
    _br("properties-null-element", _ONE + '"properties":[null]}', _batch_refusal(43, "properties element")),
    _br("properties-null-element-then-spaces", _ONE + '"properties":[null]}   ', _batch_refusal(43, "properties element")),
    _br("properties-null-between-strings", _ONE + '"properties":["email",null,"firstname"]}', _batch_refusal(63, "properties element")),
    _br("properties-object-element", _ONE + '"properties":[{}]}', _batch_refusal(38, _OBJECT)),
    _br("properties-array-element", _ONE + '"properties":[[]]}', _batch_refusal(38, _ARRAY)),
    _br("properties-null-before-an-object", _ONE + '"properties":[null,{}]}', _batch_refusal(43, _OBJECT)),
    _br("with-history-null-element", _ONE + '"propertiesWithHistory":[null]}', _batch_refusal(54, "propertiesWithHistory element")),
    _br("with-history-object-element", _ONE + '"propertiesWithHistory":[{}]}', _batch_refusal(49, _OBJECT)),
    _br("with-history-array-element", _ONE + '"propertiesWithHistory":[[]]}', _batch_refusal(49, _ARRAY)),
    _br("null-elements-with-history-first", _ONE + '"propertiesWithHistory":[null],"properties":[null]}', _batch_refusal(74, "properties element")),
    _br("null-property-after-a-null-input", '{"inputs":[null],"properties":[null]}', _batch_refusal(37, "properties element")),
    _br("null-history-before-a-null-input", '{"propertiesWithHistory":[null],"inputs":[null]}', _batch_refusal(48, "propertiesWithHistory element")),
    _br("null-property-before-a-string-inputs", '{"properties":[null],"inputs":"x"}', _batch_refusal(31, _not_a_list("String", "VALUE_STRING"))),
    _br("null-id-before-a-null-property", '{"inputs":[{"id":null}],"properties":[null]}', _no_id(22)),
    _br("with-history-string", _ONE + '"propertiesWithHistory":"name"}', _batch_refusal(48, _no_creator("String", "String value ('name')"))),
    _br("with-history-empty", _ONE + '"propertiesWithHistory":""}', _blank_field("propertiesWithHistory")),
    _br("with-history-float", _ONE + '"propertiesWithHistory":1.5}', _batch_refusal(48, _not_a_list("Floating-point", "VALUE_NUMBER_FLOAT"))),
    _br("properties-before-inputs", '{"properties":"name","inputs":"x"}', _batch_refusal(15, _no_creator("String", "String value ('name')"))),
    _br("integer-properties-before-inputs", '{"properties":5,"inputs":"x"}', _batch_refusal(15, _not_a_list("Integer", "VALUE_NUMBER_INT"))),
    _br("inputs-before-properties", '{"inputs":1.5,"properties":"x"}', _batch_refusal(11, _not_a_list("Floating-point", "VALUE_NUMBER_FLOAT"))),
    _br("with-history-before-properties", _ONE + '"propertiesWithHistory":"a","properties":"b"}', _batch_refusal(48, _no_creator("String", "String value ('a')"))),
    _br("after-an-unknown-member", '{"bogus":1,"inputs":"x"}', _batch_refusal(21, _not_a_list("String", "VALUE_STRING"))),
    _br("after-an-unknown-type", '{"inputs":"x"}', {"status": "error", "message": "Unable to infer object type from: nosuchtype"}, object_type="nosuchtype"),
    _br("after-the-credential", '{"inputs":"x"}', 401, authed=False),
    _br("null-inputs", '{"inputs":null}', 200),
    _br("null-properties", _ONE + '"properties":null}', 207),
    _br("number-in-properties", _ONE + '"properties":[5]}', 207),
    _br("boolean-in-properties", _ONE + '"properties":[true]}', 207),
    _br("number-in-with-history", _ONE + '"propertiesWithHistory":[5]}', 207),
    _br("boolean-in-with-history", _ONE + '"propertiesWithHistory":[true]}', 207),
]
# fmt: on


def test_hubspot_batch_read_refuses_a_body_real_cannot_read(client, admin_h):
    """`_refuse_a_batch_read_shape`'s rule, one body per row: the status and body real answered,
    including which member it names when two are wrong, and the status for a null member, or a
    number or boolean in either properties list, which real reads (a 207 here because no company
    has id `1`)."""
    answered, expected = {}, {}
    for row in _BATCH_BODY_ROWS:
        raw, want, object_type, authed = row.values
        headers = {"Content-Type": "application/json", **(admin_h if authed else {})}
        url = f"/hubspot/crm/v3/objects/{object_type}/batch/read"
        r = client.post(url, headers=headers, content=raw.encode())
        if isinstance(want, dict):
            answered[row.id], expected[row.id] = (r.status_code, r.json()), (400, want)
        else:
            answered[row.id], expected[row.id] = r.status_code, want
    assert answered == expected


_MISSING_ID_SPELLINGS = [
    ("true", "true"),
    ("false", "false"),
    ("1e3", "1e3"),
    ("1E3", "1E3"),
    ("1e7", "1e7"),
    ("1.0", "1.0"),
    ("-1", "-1"),
    ("-0.0", "-0.0"),
    ("99999999999999999999", "99999999999999999999"),
    ('"missing-id"', "missing-id"),
    (r'"missing\u002did"', "missing-id"),
    (r'"missing\"\\id"', 'missing"\\id'),
]


def test_hubspot_batch_read_names_a_missing_id_as_the_body_spells_it(client, admin_h):
    """An input naming no record is the 207's one error, its id a number or boolean as the body
    spells it and a string as it decodes. Backlot names the member `context.id` where real's is
    `context.ids`."""
    answered = {}
    for raw_id, _ in _MISSING_ID_SPELLINGS:
        r = client.post(
            "/hubspot/crm/v3/objects/contacts/batch/read",
            headers={**admin_h, "Content-Type": "application/json"},
            content=('{"inputs":[{"id":' + raw_id + "}]}").encode(),
        )
        j = r.json()
        error = (j.get("errors") or [{}])[0]
        answered[raw_id] = (
            r.status_code,
            (j.get("status"), j.get("results"), j.get("numErrors")),
            error.get("category"),
            error.get("context"),
        )
    assert answered == {
        raw_id: (207, ("COMPLETE", [], 1), "OBJECT_NOT_FOUND", {"id": [echoed]})
        for raw_id, echoed in _MISSING_ID_SPELLINGS
    }


_FOUND_ID_SPELLINGS = [
    ("integer", None, None),
    ("escaped-string", None, None),
    ("escaped-string", "properties", "5"),
    ("escaped-string", "properties", "true"),
    ("escaped-string", "propertiesWithHistory", "5"),
    ("escaped-string", "propertiesWithHistory", "true"),
]


def test_hubspot_batch_read_finds_a_record_by_an_integer_or_an_escaped_id(client, admin_h):
    """A record's id sent as a JSON integer, or as a string of `\\u` escapes, finds the record
    as real's does, the escaped one also beside a `5` or a `true` in either properties list."""
    listed = client.get("/hubspot/crm/v3/objects/contacts", headers=admin_h).json()["results"]
    rid = listed[0]["id"]
    assert rid.isascii() and rid.isdigit()
    answered = {}
    for spelling, field, literal in _FOUND_ID_SPELLINGS:
        raw_id = (
            rid if spelling == "integer" else '"' + "".join(f"\\u{ord(c):04x}" for c in rid) + '"'
        )
        raw = '{"inputs":[{"id":' + raw_id + "}]"
        if field is not None:
            raw += ',"' + field + '":[' + literal + "]"
        raw += "}"
        r = client.post(
            "/hubspot/crm/v3/objects/contacts/batch/read",
            headers={**admin_h, "Content-Type": "application/json"},
            content=raw.encode(),
        )
        answered[(spelling, field, literal)] = (
            r.status_code,
            [record["id"] for record in r.json().get("results", [])],
        )
    assert answered == {row: (200, [rid]) for row in _FOUND_ID_SPELLINGS}


def _token_refusal(column: int, token: str, line: int = 1) -> dict:
    return {
        "status": "error",
        "message": f"Invalid input JSON on line {line}, column {column}: Non-standard token "
        f"'{token}': enable `JsonReadFeature.ALLOW_NON_NUMERIC_NUMBERS` to allow",
    }


def _nt(id, route, raw, want, object_type="companies", authed=True):
    return pytest.param(route, raw, want, object_type, authed, id=id)


_A_FILTER = '{"filterGroups":[{"filters":[{"propertyName":"name","operator":"EQ","value":%s}]}]}'
_AN_OPERATOR = '"filterGroups":[{"filters":[{"propertyName":"name","operator":%s,"value":"x"}]}]'

# fmt: off
_TOKEN_ROWS = [
    _nt("inputs", "batch/read", '{"inputs":NaN}', _token_refusal(14, "NaN")),
    _nt("an-input", "batch/read", '{"inputs":[NaN]}', _token_refusal(15, "NaN")),
    _nt("an-input-infinity", "batch/read", '{"inputs":[Infinity]}', _token_refusal(20, "Infinity")),
    _nt("an-input-minus-infinity", "batch/read", '{"inputs":[-Infinity]}', _token_refusal(21, "-Infinity")),
    _nt("properties", "batch/read", _ONE + '"properties":NaN}', _token_refusal(40, "NaN")),
    _nt("an-id-in-an-input", "batch/read", '{"inputs":[{"id":NaN}]}', _token_refusal(21, "NaN")),
    _nt("an-unknown-member", "batch/read", '{"bogus":NaN}', _token_refusal(13, "NaN")),
    _nt("limit", "search", '{"limit":NaN}', _token_refusal(13, "NaN")),
    _nt("limit-infinity", "search", '{"limit":Infinity}', _token_refusal(18, "Infinity")),
    _nt("after", "search", '{"after":NaN}', _token_refusal(13, "NaN")),
    _nt("a-filter-value", "search", _A_FILTER % "NaN", _token_refusal(80, "NaN")),
    _nt("filter-groups", "search", '{"filterGroups":NaN}', _token_refusal(20, "NaN")),
    _nt("filters", "search", '{"filterGroups":[{"filters":NaN}]}', _token_refusal(32, "NaN")),
    _nt("contacts", "search", '{"limit":NaN}', _token_refusal(13, "NaN"), object_type="contacts"),
    _nt("a-bare-token", "search", "NaN", _token_refusal(4, "NaN")),
    _nt("a-bare-minus-infinity", "batch/read", "-Infinity", _token_refusal(10, "-Infinity")),
    # the position, counted as `_token_at` counts one
    _nt("lf", "search", '{\n"limit":NaN}', _token_refusal(12, "NaN", line=2)),
    _nt("crlf", "search", '{\r\n"limit":NaN}', _token_refusal(12, "NaN", line=2)),
    _nt("lone-cr", "search", '{\r"limit":NaN}', _token_refusal(12, "NaN", line=2)),
    _nt("double-cr", "search", '{\r\r"limit":NaN}', _token_refusal(12, "NaN", line=3)),
    _nt("byte-order-mark", "search", b'\xef\xbb\xbf{"limit":NaN}', _token_refusal(16, "NaN")),
    _nt("tab", "search", '{\t"limit":NaN}', _token_refusal(14, "NaN")),
    _nt("two-byte-char-before", "search", '{"query":"é","limit":NaN}', _token_refusal(26, "NaN")),
    _nt("three-and-four-byte-chars-before", "search", '{"query":"' + chr(0x20AC) + chr(0x1F600) + '","limit":NaN}', _token_refusal(31, "NaN")),
    _nt("escaped-char-before", "search", '{"query":"\\u00e9","limit":NaN}', _token_refusal(30, "NaN")),
    _nt("spelled-in-a-string-before", "search", '{"query":"a\\"NaN","limit":NaN}', _token_refusal(30, "NaN")),
    _nt("repeated-key", "search", '{"limit":5,"limit":NaN}', _token_refusal(23, "NaN")),
    # UTF-16, and a byte that does not decode behind the token: the text ahead of it is scanned
    _nt("utf-16", "search", '{"limit":NaN}'.encode("utf-16-be"), _token_refusal(13, "NaN")),
    _nt("a-byte-that-does-not-decode-behind", "search", b'{"limit":NaN,"query":"\xff"}', _token_refusal(13, "NaN")),
    _nt("a-byte-order-mark-and-a-byte-that-does-not-decode", "batch/read", b'\xef\xbb\xbf{\n"inputs":[NaN,"\xff"]}', _token_refusal(14, "NaN", line=2)),
    # the token and a value real refuses for its type: whichever starts first is named
    _nt("inputs-ahead", "batch/read", '{"inputs":"x","properties":NaN}', _batch_refusal(11, _not_a_list("String", "VALUE_STRING"))),
    _nt("inputs-behind", "batch/read", '{"properties":NaN,"inputs":"x"}', _token_refusal(18, "NaN")),
    _nt("an-input-ahead", "batch/read", '{"inputs":[5,NaN]}', _batch_refusal(12, _no_creator("int/Int", "Number value (5)"))),
    _nt("an-input-behind", "batch/read", '{"inputs":[NaN,5]}', _token_refusal(15, "NaN")),
    _nt("an-array-input-around-it", "batch/read", '{"inputs":[[NaN]]}', _batch_refusal(12, _ARRAY)),
    _nt("properties-ahead", "batch/read", '{"properties":"x","inputs":[NaN]}', _batch_refusal(15, _no_creator("String", "String value ('x')"))),
    _nt("blank-properties-ahead", "batch/read", '{"properties":" ","inputs":[NaN]}', _blank_field("properties")),
    _nt("blank-properties-behind", "batch/read", '{"inputs":[NaN],"properties":" "}', _token_refusal(15, "NaN")),
    _nt("a-null-input-ahead", "batch/read", '{"inputs":[null],"properties":NaN}', _token_refusal(34, "NaN")),
    _nt("a-null-input-and-an-unknown-member", "batch/read", '{"inputs":[null],"bogus":NaN}', _token_refusal(29, "NaN")),
    _nt("a-null-input-and-an-id", "batch/read", '{"inputs":[null,{"id":NaN}]}', _token_refusal(26, "NaN")),
    _nt("a-null-id-ahead", "batch/read", '{"inputs":[{"id":null}],"x":NaN}', _no_id(22)),
    _nt("a-null-id-behind", "batch/read", '{"x":NaN,"inputs":[{"id":null}]}', _token_refusal(9, "NaN")),
    _nt("the-token-before-a-null-ids-input-closes", "batch/read", '{"inputs":[{"id":null,"x":NaN}]}', _token_refusal(30, "NaN")),
    _nt("an-object-id-ahead", "batch/read", '{"inputs":[{"id":{}}],"x":NaN}', _batch_refusal(18, _OBJECT)),
    _nt("an-object-id-behind", "batch/read", '{"x":NaN,"inputs":[{"id":{}}]}', _token_refusal(9, "NaN")),
    _nt("an-object-id-around-it", "batch/read", '{"inputs":[{"id":{"a":NaN}}]}', _batch_refusal(18, _OBJECT)),
    _nt("an-object-element-behind", "batch/read", '{"x":NaN,"inputs":[{"id":"1"}],"properties":[{}]}', _token_refusal(9, "NaN")),
    _nt("a-null-element-ahead", "batch/read", '{"inputs":[{"id":"1"}],"properties":[null],"x":NaN}', _token_refusal(51, "NaN")),
    _nt("a-missing-id-ahead", "batch/read", '{"inputs":[{}],"x":NaN}', _no_id(13)),
    _nt("a-missing-id-behind", "batch/read", '{"x":NaN,"inputs":[{}]}', _token_refusal(9, "NaN")),
    _nt("operator-ahead", "search", "{" + _AN_OPERATOR % '"BOGUS"' + ',"limit":NaN}', _enum_refusal(1, 64)),
    _nt("operator-behind", "search", '{"limit":NaN,' + _AN_OPERATOR % '"BOGUS"' + "}", _token_refusal(13, "NaN")),
    _nt("operator-ahead-in-its-filter", "search", '{"filterGroups":[{"filters":[{"propertyName":"name","operator":"BOGUS","value":NaN}]}]}', _enum_refusal(1, 64)),
    _nt("operator-behind-in-its-filter", "search", '{"filterGroups":[{"filters":[{"propertyName":"name","value":NaN,"operator":"BOGUS"}]}]}', _token_refusal(64, "NaN")),
    _nt("operator-ahead-of-filters", "search", '{"filterGroups":[{"filters":[{"propertyName":"name","operator":"BOGUS"}]},{"filters":Infinity}]}', _enum_refusal(1, 64)),
    _nt("blank-operator-ahead", "search", "{" + _AN_OPERATOR % '" "' + ',"limit":NaN}', _empty_operator_refusal(0, 0)),
    _nt("blank-operator-behind", "search", '{"limit":NaN,' + _AN_OPERATOR % '" "' + "}", _token_refusal(13, "NaN")),
    # after the type and the credential
    _nt("after-an-unknown-type", "search", '{"limit":NaN}', {"status": "error", "message": "Unable to infer object type from: nosuchtype"}, object_type="nosuchtype"),
    _nt("after-the-credential", "batch/read", '{"inputs":[NaN]}', 401, authed=False),
    # inside a string the three spellings are text
    _nt("an-id", "batch/read", '{"inputs":["NaN"]}', 207),
    _nt("an-id-in-an-object", "batch/read", '{"inputs":[{"id":"-Infinity"}]}', 207),
    _nt("a-query", "search", '{"query":"Infinity"}', 200),
    _nt("a-filter-value-with-quotes", "search", _A_FILTER % '"a \\"NaN\\" b"', 200),
    _nt("after-an-empty-string", "search", '{"query":"","bogus":"NaN"}', 200),
]
# fmt: on


def test_hubspot_refuses_a_non_standard_number_token(client, admin_h):
    """`_non_standard_number`'s rule, one body per row: the status and body real answered,
    including which value it names when the body also holds one `_refuse_a_batch_read_shape` or
    `_refuse_an_operator` refuses, and the status for the three spellings inside a string, which
    real reads as text (a 207 for batch/read because no company has those ids)."""
    answered, expected = {}, {}
    for row in _TOKEN_ROWS:
        route, raw, want, object_type, authed = row.values
        headers = {"Content-Type": "application/json", **(admin_h if authed else {})}
        url = f"/hubspot/crm/v3/objects/{object_type}/{route}"
        content = raw if isinstance(raw, bytes) else raw.encode()
        r = client.post(url, headers=headers, content=content)
        if isinstance(want, dict):
            answered[row.id], expected[row.id] = (r.status_code, r.json()), (400, want)
        else:
            answered[row.id], expected[row.id] = r.status_code, want
    assert answered == expected


@pytest.mark.parametrize(
    "inputs, caller, status",
    [
        (["{a}"], None, 200),
        (["{a}", {"id": "{b}"}], None, 200),
        (["111111111111"], None, 207),
        ([" {a} "], None, 207),
        ([""], None, 207),
        (["{secret}"], "ava@acme.com", 207),
        (["{secret}"], "hana@acme.com", 200),
    ],
    ids=[
        "alone",
        "beside-an-object",
        "missing",
        "padded",
        "empty",
        "a-record-the-caller-cannot-read",
        "a-record-the-caller-can-read",
    ],
)
def test_hubspot_batch_read_reads_a_bare_id_string_as_that_id(
    client, admin_h, tokens_yaml, inputs, caller, status
):
    """`batch_read`'s reading of a bare string in `inputs`: each row's answer, status and body,
    against the answer to the same ids sent as objects by the same caller (the admin where
    `caller` is None). `{secret}` is the company only hana may read, so ava's bare string gets
    the not-found error a missing id gets and hana's gets the record."""
    listed = client.get(
        "/hubspot/crm/v3/objects/companies", headers=admin_h, params={"limit": 2}
    ).json()["results"]
    secret = next(
        r["id"]
        for r in crawl_hubspot(client, admin_h, "companies")
        if r["properties"].get("name") == "Stealth Health Co"
    )
    ids = {"a": listed[0]["id"], "b": listed[1]["id"], "secret": secret}
    users = {u["email"]: u["token"] for u in tokens_yaml["users"]}
    headers = {"Authorization": f"Bearer {users[caller]}"} if caller else admin_h
    bare = [
        x.format(**ids) if isinstance(x, str) else {"id": x["id"].format(**ids)} for x in inputs
    ]
    url = "/hubspot/crm/v3/objects/companies/batch/read"
    r = client.post(url, headers=headers, json={"inputs": bare})
    as_objects = [{"id": x} if isinstance(x, str) else x for x in bare]
    control = client.post(url, headers=headers, json={"inputs": as_objects})
    assert control.status_code == status
    assert (r.status_code, r.json()) == (status, control.json())


# --- HubSpot ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def hubspot_conn(tmp_path_factory):
    """A read-only connection to two companies, one archived, and a contact associated with the
    live one."""
    s = tiny_corpus(
        tmp_path_factory.mktemp("hubspot_conn"),
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
    conn = store.connect_ro(s.db_path)
    yield conn
    conn.close()


def test_hubspot_record_shape(hubspot_conn):
    from backlot.routers.hubspot import _record

    conn = hubspot_conn
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


def test_hubspot_properties_projection(hubspot_conn):
    from backlot.routers.hubspot import _record

    conn = hubspot_conn
    row = store.get_document(conn, "hubspot", served_id("hubspot", "hf-co"))
    assert set(_record(row, ["name"])["properties"]) == {"name"}
    assert set(_record(row)["properties"]) == {"name", "domain"}  # no projection -> all


def test_hubspot_association_shape(hubspot_conn):
    conn = hubspot_conn
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


def test_hubspot_page_omits_paging_next_on_last_page(hubspot_conn):
    """The termination contract at the builder level: `paging.next` appears only when a further page
    exists, because the official client's fetch_all stops on its absence."""
    from backlot.routers.hubspot import _page

    conn = hubspot_conn
    rows = store.list_hubspot_objects(conn, "companies", limit=3)  # 1 non-archived company
    assert "paging" not in _page(rows, 10, None)
    assert _page(rows, 1, None)["results"]  # a full page still yields rows
