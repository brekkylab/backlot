"""Atlassian: Jira issues/JQL and Confluence content/CQL — one router, one file.

One file per router, so a source's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx
import pytest
import yaml
from starlette.requests import Request

from backlot import store, synth
from backlot.errors import atlassian as errors_atlassian
from backlot.routers import atlassian as routers_atlassian
from tests._helpers import (
    bare_request,
    client_for,
    crawl_confluence,
    crawl_jira,
    db_count,
    sent_as,
    served_id,
    tiny_corpus,
)


def test_admin_jira_crawls_all(client, admin_h, ro_conn):
    assert len(crawl_jira(client, admin_h)) == db_count(ro_conn, "jira")


def test_admin_confluence_crawls_all(client, admin_h, ro_conn):
    assert len(crawl_confluence(client, admin_h)) == db_count(ro_conn, "confluence")


def _basic(raw: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(raw.encode()).decode()}


FAILED_PAIR = _basic("nobody@example.com:wrongtoken")

# Measured against ecosystem.atlassian.net (a site with public projects) and
# brekkylab.atlassian.net (one without) on 2026-09-04, with `nobody@example.com:wrongtoken`,
# with an empty password, with a value that is not base64, with an unknown scheme, and with no
# Authorization header at all. Every shape below answered the same on both sites.
UNRESOLVABLE = [
    pytest.param(FAILED_PAIR, id="failed-pair"),
    pytest.param({}, id="no-credential"),
    pytest.param({"Authorization": "Bogus xyz"}, id="unknown-scheme"),
]


@pytest.mark.parametrize("headers", UNRESOLVABLE)
def test_jira_processes_a_credential_it_cannot_resolve_as_anonymous(client, admin_h, headers):
    """Real Jira does not refuse an unresolvable credential on these routes — it drops the caller
    to anonymous and answers the request. `project/search` is 200 with the projects an anonymous
    caller may see, a bounded `search/jql` is 200 with that query's anonymous view, and an issue
    is Jira's own 404. No document in a Backlot corpus is granted to a principal outside the org,
    so anonymous reaches none of them and each listing comes back empty. `serverInfo` is 200 with
    the signed-in body less `serverTime`, on v2 and v3 (see `jira_server_info`)."""
    projects = client.get("/atlassian/rest/api/3/project/search", headers=headers)
    assert projects.status_code == 200 and projects.json()["values"] == []
    found = client.get(
        "/atlassian/rest/api/3/search/jql", headers=headers, params={"jql": "project = payments"}
    )
    assert found.status_code == 200 and found.json()["issues"] == []
    issue = client.get("/atlassian/rest/api/3/issue/ABC-1", headers=headers)
    assert issue.status_code == 404
    # Jira's own envelope, two members and no more (`errors.atlassian.http_body`)
    assert issue.json() == {
        "errorMessages": ["Issue does not exist or you do not have permission to see it."],
        "errors": {},
    }
    for ver in ("2", "3"):
        info = client.get(f"/atlassian/rest/api/{ver}/serverInfo", headers=headers)
        signed = client.get(f"/atlassian/rest/api/{ver}/serverInfo", headers=admin_h).json()
        assert info.status_code == 200
        assert list(info.json().items()) == [(k, v) for k, v in signed.items() if k != "serverTime"]


@pytest.mark.parametrize("path", ["/rest/api/3/field", "/rest/api/3/issueLinkType"])
def test_jira_serves_its_field_metadata_to_an_anonymous_caller(client, path):
    """Both answer 200 to a failed pair on the real sites, so neither is behind the credential."""
    assert client.get(f"/atlassian{path}", headers=FAILED_PAIR).status_code == 200


@pytest.mark.parametrize("host", [None, "jira.example.test"])
def test_jira_link_types_match_measured_metadata(client, admin_h, host):
    """The default four types and self links measured beside `jira_link_types`, members in real's
    order, for a caller whose credential resolves."""
    headers = {**admin_h, "Host": host} if host else admin_h
    response = client.get("/atlassian/rest/api/3/issueLinkType", headers=headers)
    assert response.status_code == 200
    base = f"http://{host or 'testserver'}/rest/api/3/issueLinkType"
    types = response.json()["issueLinkTypes"]
    assert [list(item.items()) for item in types] == [
        [("id", i), ("name", n), ("inward", a), ("outward", b), ("self", f"{base}/{i}")]
        for i, n, a, b in [
            ("10000", "Blocks", "is blocked by", "blocks"),
            ("10001", "Cloners", "is cloned by", "clones"),
            ("10002", "Duplicate", "is duplicated by", "duplicates"),
            ("10003", "Relates", "relates to", "relates to"),
        ]
    ]


def test_atlassian_lists_only_the_containers_the_caller_can_open(tmp_path):
    """The anonymous listing is empty because a project is listed when the caller can see an issue
    in it, and that rule is not anonymous-only: a scoped caller who can open nothing in a project
    does not get it either. Backlot grants per document, so the projects have to be read off the
    issues — listing every one of them to everybody was how an empty anonymous listing could not
    be told from a full one. A Confluence space is the same container under another vendor's name,
    so one corpus carries the split in both."""
    corpus = [
        {
            "source_type": "jira",
            "doc_id": "j-open",
            "project": "payments",
            "title": "Gateway 502s",
            "content": "Body.",
            "author_email": "ava@acme.com",
            "visibility": "public",
        },
        {
            "source_type": "jira",
            "doc_id": "j-shut",
            "project": "secrets",
            "title": "Rotation",
            "content": "Body.",
            "author_email": "bob@acme.com",
            "visibility": "private",
        },
        {
            "source_type": "confluence",
            "doc_id": "c-open",
            "space": "engineering",
            "title": "Runbook",
            "content": "Body.",
            "author_email": "ava@acme.com",
            "visibility": "public",
        },
        {
            "source_type": "confluence",
            "doc_id": "c-shut",
            "space": "secrets",
            "title": "Key rotation",
            "content": "Body.",
            "author_email": "bob@acme.com",
            "visibility": "private",
        },
    ]
    settings = tiny_corpus(tmp_path, corpus)
    with client_for(settings, reload=True) as c:
        import yaml

        from backlot import synth

        written = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))
        tokens = {u["email"]: u["token"] for u in written["users"]}
        tokens["admin"] = written["admin_token"]

        def keys(headers):
            listing = c.get("/atlassian/rest/api/3/project/search", headers=headers).json()
            return sorted(p["name"] for p in listing["values"])

        def spaces(headers):
            listing = c.get("/atlassian/wiki/rest/api/space", headers=headers).json()
            return sorted(s["name"] for s in listing["results"])

        assert keys({"Authorization": f"Bearer {tokens['admin']}"}) == ["payments", "secrets"]
        assert keys({"Authorization": f"Bearer {tokens['bob@acme.com']}"}) == [
            "payments",
            "secrets",
        ]
        assert keys({"Authorization": f"Bearer {tokens['ava@acme.com']}"}) == ["payments"]
        assert keys({}) == []

        assert spaces({"Authorization": f"Bearer {tokens['admin']}"}) == ["engineering", "secrets"]
        assert spaces({"Authorization": f"Bearer {tokens['bob@acme.com']}"}) == [
            "engineering",
            "secrets",
        ]
        assert spaces({"Authorization": f"Bearer {tokens['ava@acme.com']}"}) == ["engineering"]
        # No anonymous row: `_confluence_caller` refuses before a space is resolved.

        # The space ava reaches no page in is absent on the space read with the roster asked for and
        # without it, as an unopenable project is on `project/{key}/role`. Bob, who authored the page
        # in it, reads both, under the spellings `_space_container_for_key` resolves: the
        # synthesized key and the name, each in upper or lower case. The key with `ſ` for its `S` is
        # no spelling of it, and both callers get the 404.
        shut = synth.confluence_space_key("secrets")
        ava = {"Authorization": f"Bearer {tokens['ava@acme.com']}"}
        bob = {"Authorization": f"Bearer {tokens['bob@acme.com']}"}
        for spelling, bob_reads in (
            (shut, True),
            (shut.lower(), True),
            ("secrets", True),
            ("SECRETS", True),
            (shut.replace("S", "ſ"), False),
        ):
            for path in (
                f"/wiki/rest/api/space/{spelling}",
                f"/wiki/rest/api/space/{spelling}?expand=permissions",
            ):
                refused = c.get(f"/atlassian{path}", headers=ava)
                assert refused.status_code == 404, path
                assert refused.json() == {
                    "statusCode": 404,
                    "message": (
                        "org.springframework.web.server.ResponseStatusException: "
                        f'404 NOT_FOUND "No space found with key : {spelling}"'
                    ),
                }
                read = c.get(f"/atlassian{path}", headers=bob)
                assert read.status_code == (200 if bob_reads else 404), path
        # ... and the roster she is refused names bob against that space.
        space = c.get(
            f"/atlassian/wiki/rest/api/space/{shut}?expand=permissions", headers=bob
        ).json()
        readers = space["permissions"][0]["subjects"]["user"]["results"]
        assert [u["email"] for u in readers] == ["bob@acme.com"]

        # A write at a served route's path looks its key up as the caller sees it: ava's `DELETE` on
        # the issue she cannot open is its 404, on the one she can the 405 Backlot answers a write
        # real would run, and the admin's the 405 on both; a project role's write looks the project
        # up the same way, by its key or its id, and then refuses her the 401 for not administering
        # it (``backlot.routers.atlassian._published_answer``), as the role's read does.
        admin = {"Authorization": f"Bearer {tokens['admin']}"}
        projects = {name: synth.jira_project_key(name) for name in ("payments", "secrets")}
        ids = {name: synth.github_user_id(name) for name in ("payments", "secrets")}
        found = c.get(
            "/atlassian/rest/api/3/search/jql",
            params={"jql": f"project in ({', '.join(projects.values())})", "fields": "summary"},
            headers=admin,
        ).json()["issues"]
        issues = {i["fields"]["summary"]: i["key"] for i in found}
        for method, path, ava_status, admin_status in (
            ("DELETE", f"issue/{issues['Gateway 502s']}", 405, 405),
            ("DELETE", f"issue/{issues['Rotation']}", 404, 405),
            ("DELETE", f"project/{projects['payments']}/role/10002", 401, 405),
            ("DELETE", f"project/{projects['secrets']}/role/10002", 404, 405),
            ("DELETE", f"project/{ids['payments']}/role/10002", 401, 405),
            ("DELETE", f"project/{ids['secrets']}/role/10002", 404, 405),
            ("GET", f"project/{ids['payments']}/role/10002", 401, 200),
            ("GET", f"project/{ids['secrets']}/role/10002", 404, 200),
        ):
            for headers, status in ((ava, ava_status), (admin, admin_status)):
                r = c.request(method, f"/atlassian/rest/api/3/{path}", headers=headers)
                assert r.status_code == status, (method, path, r.text)


@pytest.mark.parametrize(
    "order_by, refused",
    [
        ("bogus", "bogus"),
        ("key,name", "key,name"),
        ("-bogus", "bogus"),
        ("--key", "-key"),
        ("\t-bogus ", "bogus"),
        ("-", ""),
        (" ", ""),
        ("\xa0key", "\xa0key"),
        ("-\xa0key", "\xa0key"),
        ("i\xdfueCount", "i\xdfueCount"),
        ("key", None),
        ("-key", None),
        ("+name", None),
        ("KEY", None),
        (" key", None),
        ("\x01key", None),
        ("- \tkey", None),
        ("\u0131ssueCount", None),
        ("\u0130ssueCount", None),
        ("i\u017fsueCount", None),
        ("", None),
    ],
)
def test_jira_project_search_refuses_an_order_by_naming_no_field(
    client, admin_h, order_by, refused
):
    """`project/search` refuses an `orderBy` naming no field it sorts by, with the body
    `_jira_project_order` records, and answers the listing for each spelling it reads as a field
    and for an empty value."""
    listing = client.get("/atlassian/rest/api/3/project/search", headers=admin_h)
    r = client.get(
        "/atlassian/rest/api/3/project/search",
        params={"orderBy": order_by},
        headers=admin_h,
    )
    if refused is None:
        assert r.status_code == 200, r.text
        # the links carry the query as sent (`_project_search_link`), so they are left out
        unlinked = ("self", "nextPage")
        assert {k: v for k, v in r.json().items() if k not in unlinked} == {
            k: v for k, v in listing.json().items() if k not in unlinked
        }
        return
    assert r.status_code == 400, r.text
    assert r.json() == {
        "errorMessages": [
            "The field to order by should be one of [name, key, owner, category, issueCount, "
            "lastIssueUpdatedTime, archivedDate, deletedDate]. Instead, it was: "
            f"{refused}."
        ],
        "errors": {},
    }


def test_jira_project_search_filters_by_query_keys_type_and_category(tmp_path):
    """`project/search` applies `query`, `keys`, `typeKey` and `categoryId`, so a value that
    matches no project is an empty page (`_jira_project_kept`)."""
    settings = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "jira",
                "doc_id": f"issue-{name}",
                "project": name,
                "title": name,
                "content": "Issue",
                "author_email": "a@x.com",
                "visibility": "public",
            }
            for name in ("scrum", "orders")
        ],
    )
    with client_for(settings, reload=True) as client:
        admin_h = {"Authorization": f"Bearer {settings.admin_token}"}
        listing = client.get("/atlassian/rest/api/3/project/search", headers=admin_h)
        assert listing.status_code == 200, listing.text
        projects = listing.json()["values"]
        assert len(projects) == 2
        one, other = projects[0], projects[1]

        def keys_of(params):
            response = client.get(
                "/atlassian/rest/api/3/project/search", params=params, headers=admin_h
            )
            assert response.status_code == 200, response.text
            body = response.json()
            found = [project["key"] for project in body["values"]]
            assert body["total"] == len(found)
            return found

        all_keys = [project["key"] for project in projects]
        assert keys_of({}) == all_keys
        assert keys_of({"query": "zzqq"}) == []
        assert keys_of({"query": one["name"][:3].lower()}) == [one["key"]]
        assert keys_of({"query": one["name"][:3].upper()}) == [one["key"]]
        fragment = next(
            one["key"][i : i + 4]
            for i in range(len(one["key"]) - 3)
            if one["key"][i : i + 4].lower() not in one["name"].lower()
            and one["key"][i : i + 4].lower() not in other["name"].lower()
            and one["key"][i : i + 4].lower() not in other["key"].lower()
        )
        assert keys_of({"query": fragment}) == [one["key"]]
        name_only = next(
            one["name"][i : i + 3]
            for i in range(len(one["name"]) - 2)
            if one["name"][i : i + 3].lower() not in one["key"].lower()
            and one["name"][i : i + 3].lower() not in other["name"].lower()
            and one["name"][i : i + 3].lower() not in other["key"].lower()
        )
        assert keys_of({"query": name_only}) == [one["key"]]
        assert keys_of({"keys": one["key"]}) == [one["key"]]
        assert keys_of({"keys": one["key"].lower()}) == []
        assert keys_of({"keys": "NOPE"}) == []
        assert keys_of([("keys", one["key"]), ("keys", other["key"])]) == all_keys
        assert keys_of({"typeKey": "software"}) == all_keys
        assert keys_of({"typeKey": "bogus"}) == []
        assert keys_of({"categoryId": "99"}) == []
        with_s = next(project for project in projects if "s" in project["name"].lower())
        assert keys_of({"query": with_s["name"].replace("s", "\u017f")}) == []
        assert keys_of({"keys": ""}) == all_keys
        assert keys_of([("keys", ""), ("keys", "")]) == []
        assert keys_of({"typeKey": "Software"}) == all_keys
        assert keys_of({"typeKey": " software"}) == all_keys
        assert keys_of({"typeKey": "\u3000software\x85"}) == all_keys
        assert keys_of({"typeKey": "\x1fsoftware"}) == []
        assert keys_of({"typeKey": "business,software"}) == all_keys
        assert keys_of([("typeKey", "software"), ("typeKey", "business")]) == all_keys
        assert keys_of([("typeKey", "business"), ("typeKey", "software")]) == all_keys
        assert keys_of({"typeKey": "business"}) == []
        assert keys_of({"typeKey": ","}) == all_keys
        assert keys_of({"categoryId": " "}) == all_keys
        assert keys_of({"categoryId": "-1"}) == []


@pytest.mark.parametrize(
    "query, start, limit",
    [
        ({}, 0, 50),
        ({"startAt": "1"}, 1, 50),
        ({"startAt": "1", "maxResults": "1"}, 1, 1),
        ({"startAt": "-1"}, 0, 50),
        ({"startAt": "99999999999"}, 99999999999, 50),
        ({"maxResults": "0"}, 0, 1),
        ({"maxResults": "-1"}, 0, 1),
        ({"maxResults": "1"}, 0, 1),
        ({"maxResults": " 1"}, 0, 1),
        ({"maxResults": "101"}, 0, 100),
        ({"maxResults": "5000"}, 0, 100),
    ],
)
def test_jira_project_search_pages(tmp_path, query, start, limit):
    """The project pagination measurement recorded in `jira_project_search` governs each page."""
    settings = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "jira",
                "doc_id": f"issue-{i}",
                "project": name,
                "key": f"{name.upper()}-1",
                "title": name,
                "content": "Issue",
                "author_email": "a@x.com",
                "visibility": "public",
            }
            for i, name in enumerate(("payments", "orders"))
        ],
    )
    with client_for(settings, reload=True) as client:
        admin_h = {"Authorization": f"Bearer {settings.admin_token}"}
        url = "/atlassian/rest/api/3/project/search"
        full = client.get(url, headers=admin_h).json()["values"]
        assert len(full) > 1
        response = client.get(url, params=query, headers=admin_h)
        assert response.status_code == 200, response.text
        page = full[start : start + limit]
        body = response.json()
        last = start + len(page) >= len(full)
        assert list(body) == ["self", *([] if last else ["nextPage"]), *_PAGE_MEMBERS]
        assert {k: body[k] for k in _PAGE_MEMBERS} == {
            "values": page,
            "startAt": start,
            "maxResults": limit,
            "total": len(full),
            "isLast": last,
        }
        assert body["self"].endswith(f"/project/search?maxResults={limit}&startAt={start}")
        if not last:
            assert body["nextPage"].endswith(f"?maxResults={limit}&startAt={start + limit}")


_PAGE_MEMBERS = ["maxResults", "startAt", "total", "isLast", "values"]


@pytest.mark.parametrize(
    "query, link",
    [
        ("maxResults=1&bogus=a%2Bb", "bogus=a%2Bb&maxResults=1&startAt=0"),
        ("maxResults=1&bogus=a+b", "bogus=a+b&maxResults=1&startAt=0"),
        ("maxResults=1&b%6Fgus=x", "b%6Fgus=x&maxResults=1&startAt=0"),
        ("maxResults=1&bogus", "bogus=&maxResults=1&startAt=0"),
        ("maxResults=1&bogus=1&bogus=2", "bogus=1&bogus=2&maxResults=1&startAt=0"),
        ("maxResults=1&Zeta=1&alpha=2", "Zeta=1&maxResults=1&alpha=2&startAt=0"),
        ("maxResults=1&keys=A&keys=B", "keys=A&keys=B&maxResults=1&startAt=0"),
        ("maxResults=1&=x", "maxResults=1&x=&startAt=0"),
        ("maxResults=1&startAt=0&startAt=5", "maxResults=1&startAt=0"),
        ("startAt=%2B1&maxResults=1", "maxResults=1&startAt=1"),
        # fifteen names spread over thirty-two buckets, twelve over sixteen
        (
            "maxResults=1&qmokc=1&qscln=1&qmlde=1&qklik=1&qatpo=1&qazyu=1&qzuet=1&quimu=1&qkiux=1&qdktb=1&qolft=1&qqlew=1&qbwqu=1",
            "qmokc=1&qazyu=1&qdktb=1&quimu=1&qatpo=1&qzuet=1&qbwqu=1&qscln=1&qolft=1&qqlew=1&maxResults=1&qkiux=1&qmlde=1&qklik=1&startAt=0",
        ),
        (
            "maxResults=1&qmokc=1&qscln=1&qmlde=1&qklik=1&qatpo=1&qazyu=1&qzuet=1&quimu=1&qkiux=1&qdktb=1",
            "qmokc=1&qazyu=1&qdktb=1&quimu=1&qatpo=1&maxResults=1&qkiux=1&qmlde=1&qzuet=1&qscln=1&qklik=1&startAt=0",
        ),
    ],
)
def test_jira_project_search_links_itself_with_what_it_was_sent(client, admin_h, query, link):
    """Each row is the query real's `self` carried for the same query, in the order
    `_project_search_link` gives."""
    r = client.get(f"/atlassian/rest/api/3/project/search?{query}", headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["self"].split("?", 1)[1] == link


@pytest.mark.parametrize(
    "query, name, value",
    [
        ([("startAt", "abc")], "startAt", "abc"),
        ([("startAt", "1.5")], "startAt", "1.5"),
        ([("maxResults", "abc")], "maxResults", "abc"),
        ([("startAt", "abc"), ("orderBy", "bogus")], "startAt", "abc"),
        ([("orderBy", "bogus"), ("startAt", "abc")], "startAt", "abc"),
        ([("maxResults", "abc"), ("orderBy", "bogus")], "maxResults", "abc"),
        ([("orderBy", "bogus"), ("maxResults", "abc")], "maxResults", "abc"),
        ([("startAt", "abc"), ("maxResults", "xyz")], "startAt", "abc"),
        ([("maxResults", "xyz"), ("startAt", "abc")], "startAt", "abc"),
        ([("categoryId", "abc")], "categoryId", "abc"),
        ([("categoryId", "1.5")], "categoryId", "1.5"),
        ([("orderBy", "bogus"), ("categoryId", "abc")], "categoryId", "abc"),
        ([("categoryId", "abc"), ("startAt", "x")], "startAt", "x"),
        ([("categoryId", "abc"), ("maxResults", "x")], "maxResults", "x"),
    ],
)
def test_jira_project_search_integer_conversion_precedes_order(client, admin_h, query, name, value):
    """The measured integer refusal precedes orderBy validation in either URL order."""
    response = client.get("/atlassian/rest/api/3/project/search", params=query, headers=admin_h)
    assert response.status_code == 400, response.text
    assert response.headers["content-type"] == "application/problem+json;charset=UTF-8"
    assert response.json() == {
        "type": "about:blank",
        "title": "Bad Request",
        "status": 400,
        "detail": f"Failed to convert '{name}' with value: '{value}'",
        "instance": "/rest/api/3/project/search",
    }


@pytest.mark.parametrize("headers", [*UNRESOLVABLE, "admin"])
@pytest.mark.parametrize(
    "segment,kind,found",
    [
        pytest.param(synth.jira_project_key("payments"), "key", True, id="corpus-key"),
        pytest.param(str(synth.github_user_id("payments")), "id", True, id="corpus-id"),
        pytest.param("NOSUCHKEY", "key", False, id="no-such-key"),
        pytest.param("1", "id", False, id="one-digit"),
        pytest.param("999999999999999999", "id", False, id="eighteen-digits"),
        pytest.param("1000000000000000000", "key", False, id="nineteen-digits"),
        pytest.param("0", "key", False, id="zero"),
        pytest.param("007", "key", False, id="leading-zero"),
        pytest.param("1２", "key", False, id="full-width-digit"),
        pytest.param(synth.jira_project_key("payments").lower(), "key", False, id="key-lower"),
        pytest.param(
            synth.jira_project_key("payments").capitalize(), "key", False, id="key-capitalised"
        ),
        pytest.param("payments", "key", False, id="project-name"),
        pytest.param("PAYMENTS", "key", False, id="project-name-upper"),
    ],
)
def test_jira_role_routes_find_a_project_by_its_key_as_written_or_its_id(
    client, admin_h, headers, segment, kind, found
):
    """A role read is the one Jira route that keeps refusing an anonymous caller, and which of the
    two refusals it gives is decided by the project: on the site where the key names a project
    anonymous can see it is 401, and on the site where it names nothing it is 404. Anonymous sees no
    Backlot project, so it is always the 404 here — the corpus's own project, by its key or its id,
    gets the answer a segment naming nothing would. The admin is the control: it reads the role by
    the project's key and its id, and gets the same 404 for every other segment. The 404 calls the
    segment an id or a key by the rule `_require_project` records."""
    if headers == "admin":
        headers = admin_h
    else:
        found = False
    for path in (
        f"/rest/api/3/project/{segment}/role",
        f"/rest/api/3/project/{segment}/role/10002",
    ):
        r = client.get(f"/atlassian{path}", headers=headers)
        if found:
            assert r.status_code == 200, path
            continue
        assert r.status_code == 404, path
        assert r.json() == {
            "errorMessages": [f"No project could be found with {kind} '{segment}'."],
            "errors": {},
        }, path


def test_jira_refuses_a_bearer_it_cannot_read_as_a_connect_token(client):
    """A bearer is not the Basic pair: Jira does not go anonymous for one it cannot resolve, it
    refuses with 403 and a body that is neither API's envelope. A Backlot token is an opaque
    string with no dots, which is the shape that draws this — measured with `usr-…` itself, and
    with `bogustoken123` and `a.b.c`, on both sites on 2026-09-04."""
    r = client.get(
        "/atlassian/rest/api/3/project/search", headers={"Authorization": "Bearer usr-nope"}
    )
    assert r.status_code == 403
    # The whole body, not a subset: this one route answers with a single `error` key, where a Jira
    # refusal in its own envelope (``errors.atlassian._jira_envelope``) carries `errorMessages` and
    # `errors`. Byte for byte, spelled as ``backlot.main.refuse_a_bearer_jira_cannot_read`` says
    # real spells it.
    assert r.content == b'{"error": "Failed to parse Connect Session Auth Token"}'
    # No Seraph header either — that one reports a failed Basic username, and this is not one.
    assert "x-seraph-loginreason" not in r.headers
    # The gateway's own headers and none of Jira's (``backlot.routers.atlassian._gateway_headers``).
    assert r.headers["atl-traceid"] == r.headers["atl-request-id"].replace("-", "")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-xss-protection"] == "1; mode=block"
    for absent in (
        "x-arequestid",
        "cache-control",
        "timing-allow-origin",
        "x-aaccountid",
        "x-ratelimit-limit",
    ):
        assert absent not in r.headers, absent
    # And it is refused ahead of the route: serverInfo needs no credential and still answers 403,
    # which is why the check is not in the caller helper. The tokens are spellings
    # ``backlot.auth.atlassian_bearer_token`` reads.
    for token in (
        *("usr-nope", "a\tb", "a,b", "a.b", "\tusr-nope", "\t\tusr-nope"),
        *("\x85a", "\xa0a", "\x85", "\xa0", "\xa0\xa0"),
    ):
        info = sent_as(
            client, "GET", "/atlassian/rest/api/3/serverInfo", {"Authorization": f"Bearer {token}"}
        )
        assert info.status_code == 403 and info.json() == {
            "error": errors_atlassian.CONNECT_TOKEN_UNREADABLE
        }, token


def test_jira_answers_a_jwt_shaped_bearer_with_the_403_too(client):
    """The one disclosed divergence, pinned so it cannot drift into an accident. A bearer shaped
    like a complete signed JWS is READ by the real gateway and then rejected — `401 text/html`
    "Client must be authenticated to access this resource." on both sites — where every incomplete
    shape draws the 403. Backlot issues no JWT-shaped token, and reproducing Atlassian Connect's
    accept boundary would mean inventing the space between the shapes measured, so this answers the
    403. If `auth.atlassian_bearer_unreadable` ever grows a shape check, this is what says so."""
    jws = "eyJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJ4In0.c2ln"
    r = client.get(
        "/atlassian/rest/api/3/project/search", headers={"Authorization": f"Bearer {jws}"}
    )
    assert r.status_code == 403
    assert r.json() == {"error": errors_atlassian.CONNECT_TOKEN_UNREADABLE}
    # Confluence answers a JWT-shaped bearer with its own 403, which real does too.
    conf = client.get("/atlassian/wiki/rest/api/space", headers={"Authorization": f"Bearer {jws}"})
    assert conf.status_code == 403
    assert conf.json()["message"] == errors_atlassian.CONFLUENCE_FORBIDDEN


@pytest.mark.parametrize(
    "header",
    [
        "bearer usr-nope",
        "BEARER usr-nope",
        "token usr-nope",
        "OAuth usr-nope",
        "Bearer  usr-nope",
        "Bearer\tusr-nope",
        "Bearer",
        # a space in the token, which the gateway does not read past
        "Bearer a b",
        "Bearer \t usr-nope",
        "Bearer a  b",
        "Bearer a b c",
        # the header's ends, as ``backlot.auth.atlassian_bearer_token`` trims them
        "Bearer \t",
        "\x85Bearer usr-nope",
        "\xa0Bearer usr-nope",
    ],
)
def test_jira_reads_an_unrecognised_scheme_as_no_credential(client, header):
    """The 403 above is for a credential the site READ. A spelling it does not read is not a
    credential at all, and the request is the anonymous one — which is how each spelling was told
    apart on the real sites in the first place (403 = read, 200 = not read)."""
    r = sent_as(client, "GET", "/atlassian/rest/api/3/project/search", {"Authorization": header})
    assert r.status_code == 200 and r.json()["values"] == []


def test_atlassian_still_authenticates_the_bearer_spelling_it_does_read(client, tokens_yaml):
    """The strict parser must not cost the working credential: `Bearer <token>` is what
    mcp-atlassian sends for an admin, and both APIs answer it."""
    h = {"Authorization": f"Bearer {tokens_yaml['admin_token']}"}
    assert client.get("/atlassian/rest/api/3/project/search", headers=h).json()["values"]
    assert client.get("/atlassian/wiki/rest/api/space", headers=h).status_code == 200


def test_confluence_refuses_an_unreadable_bearer_with_its_own_403_not_jiras(client):
    """Confluence answers a bearer it cannot resolve with the same 403 envelope it gives a failed
    pair — the refusal is keyed on the credential failing, not on which scheme carried it. Jira's
    single-key Connect body does not appear on this side. Measured on both sites, for an opaque
    token and for a JWT-shaped one."""
    r = client.get("/atlassian/wiki/rest/api/space", headers={"Authorization": "Bearer usr-nope"})
    assert r.status_code == 403
    assert r.json()["message"] == errors_atlassian.CONFLUENCE_FORBIDDEN


@pytest.mark.parametrize("headers", UNRESOLVABLE)
def test_confluence_refuses_an_unresolvable_credential_with_its_own_403(client, headers):
    """Confluence does not process an anonymous caller the way Jira does: it rejects the request
    outright, with a 403 in its own envelope, whether the credential failed or was never sent. The
    body is real's to the byte, two members; `search` gives the other anonymous 403, in its own
    words (``backlot.routers.atlassian._confluence_caller``)."""
    forbidden, not_permitted = (
        errors_atlassian.CONFLUENCE_FORBIDDEN_BODY,
        errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,
    )
    for path, body in (
        ("/wiki/rest/api/space", forbidden),
        ("/wiki/rest/api/space/ENG", forbidden),
        ("/wiki/rest/api/content/1", forbidden),
        ("/wiki/rest/api/content/1/label", forbidden),
        ("/wiki/rest/api/content/1/child/attachment?limit=abc", forbidden),
        ("/wiki/rest/api/search?cql=type%3Dpage", not_permitted),
    ):
        r = client.get(f"/atlassian{path}", headers=headers)
        assert r.status_code == 403, path
        assert r.headers["content-type"] == "application/json", path
        assert r.content == body, path


@pytest.mark.parametrize(
    "raw",
    ["nobody@example.com:", ":wrongtoken", ":", "nobody@example.com", "nobody@example.com:a:b"],
)
def test_confluence_answers_401_to_a_basic_credential_it_cannot_parse(client, raw):
    """Confluence's 403 is for a credential it read and rejected. A Basic value that is not one
    non-empty user and one non-empty password separated by a single colon is not one it can read,
    and that is a 401 carrying the site's own OAuth realm — measured on both sites for each shape
    below, and for a value that is not base64 at all. Backlot answers it in Confluence's JSON
    envelope (`errors.atlassian.http_body`), where the real 401 is Tomcat's HTML page."""
    r = client.get("/atlassian/wiki/rest/api/space", headers=_basic(raw))
    assert r.status_code == 401
    # The literal, not the constant: comparing the body against the same constant the router
    # emits passes whatever that constant says, and its value is the measured part — the title of
    # the Tomcat page real answers with, kept because this envelope is JSON where that page is not.
    assert r.json()["message"] == "Unauthorized"
    assert r.headers["www-authenticate"] == 'OAuth realm="http%3A%2F%2Ftestserver%2Fwiki"'


def test_jira_reports_a_failed_credential_in_the_seraph_header(client):
    """Jira answers a failed credential anonymously but still says one was presented, on every
    response including the 200s. The header is keyed on the username: a value carrying a non-empty
    user before its first colon gets it, and one without a colon, or with an empty user, does not
    — neither does a request that sent no credential at all."""
    carries = client.get("/atlassian/rest/api/3/project/search", headers=FAILED_PAIR)
    assert carries.headers["x-seraph-loginreason"] == "AUTHENTICATED_FAILED"
    for headers in ({}, _basic(":wrongtoken"), _basic("nobody@example.com")):
        answer = client.get("/atlassian/rest/api/3/project/search", headers=headers)
        assert "x-seraph-loginreason" not in answer.headers
    # Confluence carries no Seraph header on any of its answers.
    refused = client.get("/atlassian/wiki/rest/api/space", headers=FAILED_PAIR)
    assert "x-seraph-loginreason" not in refused.headers


def test_atlassian_error_keeps_the_atlassian_error_envelope(client):
    """Atlassian clients parse the error body as Atlassian Cloud's envelope (Confluence's
    raise_for_status reads ``response.json()["message"]``), so an error there is not FastAPI's
    ``{"detail": ...}`` — see backlot.errors.atlassian. Confluence's 403 carries the two members
    ``errors.atlassian.CONFLUENCE_FORBIDDEN_BODY`` records and no more."""
    r = client.get("/atlassian/wiki/rest/api/space")
    assert r.status_code == 403
    body = r.json()
    # Once against the vendor's own wording rather than against the constant, for the reason given
    # in test_confluence_answers_401_to_a_basic_credential_it_cannot_parse; the other sites compare
    # the constant, which pins that the router reaches for the right one.
    assert body["message"] == (
        "com.atlassian.confluence.mvc.rest.common.exception.StacklessResponseStatusException: "
        '403 FORBIDDEN "Request rejected because caller cannot access Confluence"'
    )
    assert sorted(body) == ["message", "statusCode"]
    assert body["statusCode"] == 403


def test_jira_serverinfo_answers_reals_fifteen_members_on_v2_and_v3(client, admin_h):
    # the `jira` PyPI client (used by llama-index's JiraReader) probes serverInfo under
    # /rest/api/2 on connect, so v2 serves the v3 handler's answer; the members are the ones
    # `jira_server_info` records.
    v2 = client.get("/atlassian/rest/api/2/serverInfo", headers=admin_h).json()
    v3 = client.get("/atlassian/rest/api/3/serverInfo", headers=admin_h).json()
    assert list(v2.items()) == list(v3.items())
    assert list(v3) == [
        "baseUrl",
        "displayUrl",
        "displayUrlServicedeskHelpCenter",
        "displayUrlCSMHelpSeeker",
        "displayUrlConfluence",
        "version",
        "versionNumbers",
        "deploymentType",
        "buildNumber",
        "buildDate",
        "serverTime",
        "scmInfo",
        "serverTitle",
        "defaultLocale",
        "serverTimeZone",
    ]
    site = v3["baseUrl"]
    synthesized = {k: v3.pop(k) for k in ("buildDate", "serverTime", "scmInfo")}
    assert v3 == {
        "baseUrl": site,
        "displayUrl": site,
        "displayUrlServicedeskHelpCenter": site,
        "displayUrlCSMHelpSeeker": site,
        "displayUrlConfluence": site,
        "version": "1001.0.0-SNAPSHOT",
        "versionNumbers": [1001, 0, 0],
        "deploymentType": "Cloud",
        "buildNumber": 100294,
        "serverTitle": "Jira",
        "defaultLocale": {"locale": "en_US"},
        "serverTimeZone": "Etc/UTC",
    }
    assert re.fullmatch(r"[0-9a-f]{40}", synthesized["scmInfo"])
    built = synthesized["buildDate"]
    stamp = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d{4}"
    assert re.fullmatch(stamp, built)
    assert re.fullmatch(stamp, synthesized["serverTime"])
    served = datetime.strptime(synthesized["serverTime"], "%Y-%m-%dT%H:%M:%S.%f%z")
    assert datetime.strptime(built, "%Y-%m-%dT%H:%M:%S.%f%z") < served


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

    # "payments" carries no provided key in the SAMPLE corpus, so its served issue-key prefix IS
    # the synthesized one above -- the served spelling resolves...
    served_key = by_key["issues"][0]["key"]
    assert served_key.startswith(synth_key + "-")
    assert (
        client.get(f"/atlassian/rest/api/3/issue/{served_key}", headers=admin_h).status_code == 200
    )
    # ...but the literal container NAME as an issue-key prefix does not, even though it resolves
    # perfectly well as a JQL project TOKEN just above: `_jira_container_for_key`'s three-way
    # tolerance (provided prefix / synthesized key / literal name) is a deliberate affordance for
    # the project token, where real Jira's own pickers accept any of the three. Reusing it for
    # ISSUE-KEY resolution would give every project two extra namespaces to answer at. Real Jira
    # 404s
    # `/issue/payments-7` (the container's bare name, not its key) exactly like this.
    suffix = served_key.rsplit("-", 1)[1]
    aliased = client.get(f"/atlassian/rest/api/3/issue/payments-{suffix}", headers=admin_h)
    assert aliased.status_code == 404

    # an unresolvable project is strict: zero results, not the unfiltered corpus
    bogus = client.get(
        "/atlassian/rest/api/3/search/jql", headers=admin_h, params={"jql": "project = BOGUS_NOPE"}
    ).json()
    assert bogus["issues"] == [] and bogus["isLast"] is True

    # a jql with no project clause at all -> unfiltered (same three issues here, since payments
    # is the only Jira project in the SAMPLE corpus -- the earlier assertions are what prove
    # filtering, not this equality). It still has to RESTRICT, or real refuses it: an empty jql,
    # and an `ORDER BY` alone, are both the unbounded refusal -- see
    # test_jira_search_refuses_no_jql_at_all.
    unfiltered = client.get(
        "/atlassian/rest/api/3/search/jql", headers=admin_h, params={"jql": "project is not EMPTY"}
    ).json()
    assert {i["fields"]["summary"] for i in unfiltered["issues"]} == titles


def _service(status, exception, message):
    data = {"authorized": True, "valid": True, "errors": [], "successful": True}
    return status, {
        "statusCode": status,
        "data": data,
        "message": f"{_SERVICE}.{exception}: {message}",
    }


_SERVICE = "com.atlassian.confluence.api.service.exceptions"
_NOT_IMPLEMENTED = "unchecked.NotImplementedServiceException"


def _no_space(key):
    return _service(404, "api.NotFoundException", f"No space with key : {key}")


def _no_type(name):
    return _service(501, _NOT_IMPLEMENTED, f"Cannot find custom content type : {name}")


def _bare(status, message):
    return status, {"statusCode": status, "message": message}


_START = _service(
    400,
    "api.BadRequestException",
    "Start of this size is no longer supported. If you need to fetch this amount of content, "
    "please use either the search endpoint or get the content by a space at a time.",
)
_NEGATIVE_LIMIT = _bare(400, "java.lang.IllegalArgumentException: limit cannot be less than zero")
_NEGATIVE_START = _bare(400, "java.lang.IllegalArgumentException: start cannot be less than zero")
_COMMENTS = _service(501, _NOT_IMPLEMENTED, "Cannot fetch comments with ContentFinder")
_FOLDERS = _service(501, _NOT_IMPLEMENTED, "Cannot fetch folders with ContentFinder")
_ATTACHMENT = _bare(
    500,
    'java.lang.NullPointerException: Cannot invoke "com.atlassian.confluence.api.model.content.id.'
    'ContentId.asLong()" because "containerId" is null',
)


def _not_custom(name):
    return _bare(
        400, f"java.lang.IllegalArgumentException: Type is not a custom content type : {name}"
    )


# The rows `_CONTENT_TYPES` records, each a request real answered with the site's space key where
# this has `handbook` and a page's title where it has `{title}`. "listed" is the page listing,
# "empty" a page with no results.
# fmt: off
_CONTENT_SPACE_AND_TYPE = [
    ([("spaceKey", "NOPE")], _no_space("NOPE")),
    ([("spaceKey", "NOPE1"), ("spaceKey", "NOPE2")], _no_space("NOPE1,NOPE2")),
    ([("spaceKey", "handbook"), ("spaceKey", "handbook")], _no_space("handbook,handbook")),
    ([("spaceKey", "handbook,NOPE")], _no_space("handbook,NOPE")),
    ([("spaceKey", ""), ("spaceKey", "handbook")], _no_space(",handbook")),
    ([("spaceKey", " ")], _no_space(" ")),
    ([("spaceKey", "")], "listed"),
    ([("spaceKey", "NOPE"), ("limit", "-1")], _no_space("NOPE")),
    ([("spaceKey", "NOPE"), ("start", "-1")], _no_space("NOPE")),
    ([("spaceKey", "NOPE"), ("start", "100001")], _START),
    ([("spaceKey", "NOPE"), ("limit", "-1"), ("start", "100001")], _START),
    ([("spaceKey", "handbook"), ("limit", "-1")], _NEGATIVE_LIMIT),
    ([("type", "bogus")], _no_type("bogus")),
    ([("type", "Page")], _no_type("Page")),
    ([("type", "COMMENT")], _no_type("COMMENT")),
    ([("type", " ")], _no_type(" ")),
    ([("type", "page"), ("type", "bogus")], _no_type("page,bogus")),
    ([("type", "page"), ("type", "page")], _no_type("page,page")),
    ([("type", ""), ("type", "page")], _no_type(",page")),
    ([("type", "")], "listed"),
    ([("type", "page")], "listed"),
    ([("type", "blogpost")], "empty"),
    ([("type", "bogus"), ("limit", "-1")], _NEGATIVE_LIMIT),
    ([("type", "bogus"), ("start", "-1")], _NEGATIVE_START),
    ([("type", "bogus"), ("start", "100001")], _no_type("bogus")),
    ([("spaceKey", "NOPE"), ("type", "bogus")], _no_space("NOPE")),
    ([("type", "bogus"), ("spaceKey", "NOPE")], _no_space("NOPE")),
    ([("spaceKey", "NOPE"), ("type", "bogus"), ("start", "100001")], _no_space("NOPE")),
    ([("spaceKey", "handbook"), ("type", "bogus")], "empty"),
    ([("spaceKey", "handbook"), ("type", "bogus"), ("start", "100001")], "empty"),
    ([("spaceKey", "handbook"), ("type", "bogus"), ("limit", "-1")], _NEGATIVE_LIMIT),
    ([("spaceKey", "handbook"), ("type", "bogus"), ("start", "-1")], _NEGATIVE_START),
    ([("title", "{title}"), ("type", "bogus")], "empty"),
    ([("title", "zzqq"), ("type", "bogus")], "empty"),
    ([("title", ""), ("type", "bogus")], _no_type("bogus")),
    ([("title", "{title}"), ("type", "bogus"), ("start", "100001")], "empty"),
    ([("title", "{title}"), ("type", "bogus"), ("limit", "-1")], _NEGATIVE_LIMIT),
    ([("title", "zzqq"), ("spaceKey", "NOPE")], _no_space("NOPE")),
    ([("type", "page"), ("limit", "-1")], _NEGATIVE_LIMIT),
    ([("type", "page"), ("start", "100001")], _START),
    ([("type", "blogpost"), ("start", "100001")], _START),
    ([("title", "{title}"), ("type", "page")], "listed"),
    ([("title", "{title}"), ("type", "blogpost")], "empty"),
    ([("type", "comment")], _COMMENTS),
    ([("type", "comment"), ("limit", "-1")], _COMMENTS),
    ([("type", "comment"), ("start", "-1")], _COMMENTS),
    ([("type", "comment"), ("start", "100001")], _START),
    ([("spaceKey", "NOPE"), ("type", "comment")], _no_space("NOPE")),
    ([("spaceKey", "handbook"), ("type", "comment")], _COMMENTS),
    ([("title", "{title}"), ("type", "comment")], _COMMENTS),
    ([("type", "folder")], _FOLDERS),
    ([("type", "folder"), ("limit", "-1")], _FOLDERS),
    ([("spaceKey", "NOPE"), ("type", "folder"), ("start", "100001")], _START),
    ([("type", "whiteboard")], _not_custom("whiteboard")),
    ([("type", "database")], _not_custom("database")),
    ([("type", "embed")], _not_custom("embed")),
    ([("type", "whiteboard"), ("limit", "-1")], _NEGATIVE_LIMIT),
    ([("type", "whiteboard"), ("start", "-1")], _NEGATIVE_START),
    ([("type", "whiteboard"), ("start", "100001")], _START),
    ([("spaceKey", "NOPE"), ("type", "whiteboard")], _no_space("NOPE")),
    ([("spaceKey", "handbook"), ("type", "whiteboard")], _not_custom("whiteboard")),
    ([("title", "{title}"), ("type", "whiteboard")], _not_custom("whiteboard")),
    ([("type", "attachment")], _ATTACHMENT),
    ([("type", "attachment"), ("limit", "-1")], _NEGATIVE_LIMIT),
    ([("type", "attachment"), ("start", "100001")], _START),
    ([("spaceKey", "NOPE"), ("type", "attachment")], _no_space("NOPE")),
    ([("spaceKey", "handbook"), ("type", "attachment")], _ATTACHMENT),
    ([("title", "{title}"), ("type", "attachment")], _ATTACHMENT),
]
# fmt: on


@pytest.mark.parametrize("query, want", _CONTENT_SPACE_AND_TYPE)
def test_confluence_content_answers_a_space_key_and_a_type_as_real_does(
    client, admin_h, query, want
):
    """The rows `_CONTENT_SPACE_AND_TYPE` records, one request each, compared with real's status
    and body; a listing row checks the page listing's own fields instead."""
    query = [(k, v.format(title="Engineering Handbook")) for k, v in query]
    r = client.get("/atlassian/wiki/rest/api/content", headers=admin_h, params=query)
    if want == "listed":
        assert r.status_code == 200, r.text
        assert {x["type"] for x in r.json()["results"]} == {"page"}
    elif want == "empty":
        assert r.status_code == 200, r.text
        assert (r.json()["results"], r.json()["size"]) == ([], 0)
    else:
        assert (r.status_code, r.json()) == want


@pytest.mark.parametrize(
    "caller, space, want",
    [
        ("admin", "handbook", ["Engineering Handbook", "On-call Runbook"]),
        ("admin", "{handbook}", ["Engineering Handbook", "On-call Runbook"]),
        ("admin", "people-ops", ["Compensation Bands 2026"]),
        ("admin", "{people-ops}", ["Compensation Bands 2026"]),
        ("admin", "{handbook lower}", ["Engineering Handbook", "On-call Runbook"]),
        ("admin", "HANDBOOK", ["Engineering Handbook", "On-call Runbook"]),
        ("admin", "people-opſ", 404),
        ("admin", "", ["Compensation Bands 2026", "Engineering Handbook", "On-call Runbook"]),
        ("ava@acme.com", "handbook", ["Engineering Handbook", "On-call Runbook"]),
        ("ava@acme.com", "people-ops", 404),
        ("ava@acme.com", "{people-ops}", 404),
        ("ava@acme.com", "{people-ops lower}", 404),
        ("ava@acme.com", "PEOPLE-OPS", 404),
    ],
)
def test_confluence_content_filtered_by_space_key(client, admin_h, tokens, caller, space, want):
    """A space key narrows the listing to that space under both spellings `_space_container_for_key`
    resolves, the name and `{the synthesized key}`, each in upper or lower case (`people-opſ` is no
    spelling of `people-ops`), and an empty one narrows nothing. ava reaches no page in
    `people-ops`, so to her its key is the 404 a key naming nothing gets (`_content_space`), in
    upper or lower case."""
    from backlot import synth

    keys = {n: synth.confluence_space_key(n) for n in ("handbook", "people-ops")}
    key = space.format(**keys, **{f"{n} lower": k.lower() for n, k in keys.items()})
    h = admin_h if caller == "admin" else {"Authorization": f"Bearer {tokens[caller]}"}
    r = client.get("/atlassian/wiki/rest/api/content", headers=h, params={"spaceKey": key})
    if want == 404:
        assert (r.status_code, r.json()) == _no_space(key)
    else:
        assert sorted(x["title"] for x in r.json()["results"]) == want


def test_confluence_space_names_that_differ_only_in_case_stay_two_spaces(tmp_path):
    """A corpus may name two spaces `Ops` and `ops`. An exact spelling is resolved before a folded
    one (`_space_container_for_key`), so each name answers its own space on the three readers of a
    key, as each synthesized key sent in lower case does, and a name no other space shares answers
    in another case too."""
    from backlot import synth

    corpus = [
        {
            "source_type": "confluence",
            "doc_id": doc_id,
            "space": space,
            "title": title,
            "content": "Body.",
            "author_email": "ava@acme.com",
            "visibility": "public",
        }
        for doc_id, space, title in (
            ("o-up", "Ops", "Upper"),
            ("o-low", "ops", "Lower"),
            ("l", "Ledger", "Books"),
        )
    ]
    settings = tiny_corpus(tmp_path, corpus)
    wiki = "/atlassian/wiki/rest/api"
    with client_for(settings, reload=True) as c:
        token = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))["admin_token"]
        admin = {"Authorization": f"Bearer {token}"}
        for sent, name, title in (
            ("Ops", "Ops", "Upper"),
            ("ops", "ops", "Lower"),
            (synth.confluence_space_key("Ops").lower(), "Ops", "Upper"),
            (synth.confluence_space_key("ops").lower(), "ops", "Lower"),
            ("lEDGER", "Ledger", "Books"),
        ):
            space = c.get(f"{wiki}/space/{sent}", headers=admin).json()
            assert (space["name"], space["key"]) == (name, synth.confluence_space_key(name)), sent
            for url, params in (
                (f"{wiki}/content", {"spaceKey": sent}),
                (f"{wiki}/search", {"cql": f"space={sent}"}),
            ):
                titles = [
                    x["title"] for x in c.get(url, headers=admin, params=params).json()["results"]
                ]
                assert titles == [title], (url, sent)


def test_confluence_content_filtered_by_title(client, admin_h, tokens):
    """The rule the comment in `confluence_content_list` records, over the caller's own pages."""
    url = "/atlassian/wiki/rest/api/content"

    def titles(headers, **params):
        body = client.get(url, headers=headers, params=params).json()
        assert body["size"] == len(body["results"])
        return [r["title"] for r in body["results"]]

    for sent in ("On-call Runbook", "on-call runbook", "ON-CALL RUNBOOK"):
        assert titles(admin_h, title=sent, spaceKey="handbook") == ["On-call Runbook"], sent
        assert titles(admin_h, title=sent) == ["On-call Runbook"], sent
    for sent in ("zzqq-no-such-page", "On-call", " On-call Runbook", "On-call Runbook ", " "):
        assert titles(admin_h, title=sent, spaceKey="handbook") == [], sent
        assert titles(admin_h, title=sent) == [], sent
    assert titles(admin_h, title=["On-call Runbook", "zzqq-nope"]) == []
    assert titles(admin_h, title=["zzqq-nope", "On-call Runbook"]) == []
    assert len(titles(admin_h, title="")) > 1
    # the total behind `_links.next` counts the matching pages, not the space's
    one = client.get(url, headers=admin_h, params={"title": "On-call Runbook", "limit": 1}).json()
    assert one["size"] == 1 and "next" not in one["_links"]
    assert "next" in client.get(url, headers=admin_h, params={"limit": 1}).json()["_links"]
    # the title of a page the caller cannot see matches nothing for that caller
    comp = "Compensation Bands 2026"
    assert titles({"Authorization": f"Bearer {tokens['hana@acme.com']}"}, title=comp) == [comp]
    assert titles({"Authorization": f"Bearer {tokens['ava@acme.com']}"}, title=comp) == []


def test_atlassian_comment_ids_are_numeric_on_the_wire(tmp_path):
    """The stored id composes the parent's key with the comment's position (`PAY-7::c1`) — this is
    Backlot's own bookkeeping. Real Jira and Confluence report numeric strings, and both the `self`
    link and Confluence's `focusedCommentId` carry the value, so the internal scheme leaked into
    three places a client reads."""
    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "jira",
                "doc_id": "j-c",
                "project": "payments",
                "title": "T",
                "content": "c",
                "author_email": "a@x.com",
                "visibility": "public",
                "key": "PAY-7",
                "comments": [{"content": "hi", "author_email": "b@x.com"}],
            },
            {
                "source_type": "confluence",
                "doc_id": "cf-c",
                "space": "handbook",
                "title": "P",
                "content": "c",
                "author_email": "a@x.com",
                "visibility": "public",
                "comments": [{"content": "hi", "author_email": "b@x.com"}],
            },
        ],
    )
    with client_for(s, reload=True) as c:
        h = {"Authorization": f"Bearer {s.admin_token}"}
        (jc,) = c.get("/atlassian/rest/api/3/issue/PAY-7/comment", headers=h).json()["comments"]
        assert jc["id"].isdigit() and jc["self"].endswith(f"/comment/{jc['id']}")
        page = served_id("confluence", "cf-c")
        (cc,) = c.get(f"/atlassian/wiki/rest/api/content/{page}/child/comment", headers=h).json()[
            "results"
        ]
        assert cc["id"].isdigit()
        assert cc["_links"]["webui"].endswith(f"focusedCommentId={cc['id']}")


def test_confluence_dates_an_epoch_zero_page_on_both_routes(tmp_path):
    """1970-01-01T00:00:00Z stores as 0, and both routes that date a page must serve it.

    The CQL result read a `key` column off a confluence row — jira's spelling — which raised
    IndexError and 500ed the whole search whenever a page had no timestamps to short-circuit on.
    One helper now dates a page for both, so the body and the search hit cannot disagree."""
    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "doc_id": "cf-zero",
                "space": "handbook",
                "title": "Epoch",
                "content": "a page dated at the epoch",
                "author_email": "a@x.com",
                "visibility": "public",
                "created": 0,
            }
        ],
    )
    with client_for(s, reload=True) as c:
        h = {"Authorization": f"Bearer {s.admin_token}"}
        hit = c.get("/atlassian/wiki/rest/api/search", headers=h, params={"cql": 'text~"epoch"'})
        assert hit.status_code == 200
        (result,) = hit.json()["results"]
        assert result["lastModified"].startswith("1970-01-01T00:00:00")
        page = c.get(
            f"/atlassian/wiki/rest/api/content/{served_id('confluence', 'cf-zero')}",
            headers=h,
            params={"expand": "history"},
        ).json()
        assert page["history"]["createdDate"].startswith("1970-01-01T00:00:00")


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
        params={"cql": 'text~"software" and space="people-ops"'},
    ).json()
    assert other_space["results"] == [] and other_space["totalSize"] == 0

    bogus = client.get(
        "/atlassian/wiki/rest/api/search",
        headers=admin_h,
        params={"cql": 'text~"software" and space=BOGUS_NOPE'},
    ).json()
    assert bogus["results"] == [] and bogus["totalSize"] == 0


_CQL_REQUIRED = {
    "statusCode": 400,
    "data": {"authorized": True, "valid": True, "errors": [], "successful": True},
    "message": (
        "com.atlassian.confluence.api.service.exceptions.api.BadRequestException: "
        "cql query parameter is required"
    ),
}
_LIMIT_BELOW_ZERO = {
    "statusCode": 400,
    "message": "java.lang.IllegalArgumentException: limit cannot be less than zero",
}


@pytest.mark.parametrize(
    "query, status, body",
    [
        ("", 400, _CQL_REQUIRED),
        ("cql=", 400, _CQL_REQUIRED),
        ("cql", 400, _CQL_REQUIRED),
        ("CQL=type%3Dpage", 400, _CQL_REQUIRED),
        ("cql=&cql=type%3Dpage", 400, _CQL_REQUIRED),
        ("cql=type%3Dpage&cql=", 200, None),
        ("limit=-1", 400, _CQL_REQUIRED),
        ("cql=type%3Dpage&limit=-1", 400, _LIMIT_BELOW_ZERO),
        ("cql=%20&limit=-1", 400, _LIMIT_BELOW_ZERO),
        ("cql=&limit=abc", 404, None),
        ("cql=&start=abc", 404, None),
    ],
)
def test_confluence_cql_search_refuses_a_request_with_no_first_cql(
    client, admin_h, query, status, body
):
    """The requests :func:`backlot.errors.atlassian.cql_required` records, beside the 404 that comes
    ahead of it and the requests that pass it on to the negative refusal or to the search."""
    r = client.get(f"/atlassian/wiki/rest/api/search?{query}", headers=admin_h)
    assert r.status_code == status, r.text
    if body is not None:
        assert r.json() == body


def test_confluence_cql_search_reads_its_content_type_as_jaxrs_does(client, admin_h):
    """Pins the search's `Content-Type` refusal (``_refuse_the_search_media_type`` and
    ``_CONFLUENCE_JAXRS_GETS`` in ``backlot.routers.atlassian``), with no credential and with one,
    against `Accept` and on a `HEAD`."""
    url = "/atlassian/wiki/rest/api/search?cql=type%3Dpage"
    for credential in ({}, admin_h):
        r = client.get(url, headers={**credential, "Content-Type": "text/plain"})
        assert r.status_code == 415 and r.content == b"", r.text
        assert r.headers.get("content-type") is None
        r = client.get(url, headers={**credential, "Content-Type": "foo"})
        assert r.status_code == 400, r.text
        assert r.content == errors_atlassian.tomcat_page(400).encode()
        assert r.headers["content-type"] == errors_atlassian.TOMCAT_MEDIA_TYPE
    # ahead of the operation's own 400 for a `cql` that names nothing, and on a `HEAD`
    r = client.get(
        "/atlassian/wiki/rest/api/search", headers={**admin_h, "Content-Type": "text/plain"}
    )
    assert r.status_code == 415, r.text
    r = client.head(url, headers={"Content-Type": "text/plain"})
    assert r.status_code == 415, r.text
    r = client.get(url, headers={**admin_h, "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    twice = [*admin_h.items(), ("Content-Type", _JSON_TYPE), ("Content-Type", _JSON_TYPE)]
    r = sent_as(client, "GET", url, twice)
    assert r.status_code == 400 and r.content == errors_atlassian.tomcat_page(400).encode()
    # ahead of the 406, and the 415 behind the 400 for an `Accept` Jersey cannot parse
    for accept, ctype, status, ctype_out in [
        ("application/xml", "text/plain", 415, None),
        ("application/xml", "foo", 400, errors_atlassian.TOMCAT_MEDIA_TYPE),
        ("application/json;q=2", "foo", 400, errors_atlassian.TOMCAT_MEDIA_TYPE),
        ("application/json;q=2", "text/plain", 400, "text/plain"),
        ("application/xml", "application/json", 406, None),
    ]:
        r = client.get(url, headers={**admin_h, "Accept": accept, "Content-Type": ctype})
        assert (r.status_code, r.headers.get("content-type")) == (status, ctype_out), r.text
    # where a Spring GET answers either type as it answers none
    for ctype in ("text/plain", "foo"):
        r = client.get("/atlassian/wiki/rest/api/content/1", headers={"Content-Type": ctype})
        assert r.status_code == 403, r.text


def test_confluence_cql_search_with_no_tilde_selects_by_its_clauses(tmp_path):
    """Pins what :func:`backlot.routers.atlassian.confluence_cql_search` says a CQL with no `~`
    clause answers, for the admin and for a caller one page is hidden from, and which value of a
    repeated `cql` is searched.

    Not SAMPLE: it holds pages alone, so a `type` clause would select the same rows as no clause."""
    from backlot import synth

    settings = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "doc_id": "q-guide",
                "space": "eng",
                "title": "Guide",
                "content": "Body.",
                "author_email": "ava@acme.com",
                "visibility": "public",
                "labels": ["runbook"],
            },
            {
                "source_type": "confluence",
                "doc_id": "q-post",
                "space": "eng",
                "subtype": "blogpost",
                "title": "Post",
                "content": "Body.",
                "author_email": "ava@acme.com",
                "visibility": "public",
            },
            {
                "source_type": "confluence",
                "doc_id": "q-shut",
                "space": "ops",
                "title": "Shut",
                "content": "Body.",
                "author_email": "bob@acme.com",
                "visibility": "private",
            },
        ],
    )
    eng, ops = synth.confluence_space_key("eng"), synth.confluence_space_key("ops")
    rows = [
        # cql, the titles the admin is served, the titles ava is served. A list goes out as a
        # repeated `cql`, read from its first value as `cql_required` records.
        ("type=page", {"Guide", "Shut"}, {"Guide"}),
        ("type=blogpost", {"Post"}, {"Post"}),
        (f"space={eng}", {"Guide", "Post"}, {"Guide", "Post"}),
        (f"space={eng} and type=page", {"Guide"}, {"Guide"}),
        (f"space={eng.lower()}", {"Guide", "Post"}, {"Guide", "Post"}),
        (f'space="{ops.lower()}"', {"Shut"}, set()),
        ("space=OPS", {"Shut"}, set()),
        ("label=runbook", {"Guide"}, {"Guide"}),
        ("space=NOPE", set(), set()),
        ('title="Guide"', set(), set()),
        (["type=page", f"space={eng}"], {"Guide", "Shut"}, {"Guide"}),
    ]
    with client_for(settings, reload=True) as c:
        written = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))
        tokens = {u["email"]: u["token"] for u in written["users"]}
        callers = {
            "admin": {"Authorization": f"Bearer {written['admin_token']}"},
            "ava": {"Authorization": f"Bearer {tokens['ava@acme.com']}"},
        }
        for cql, admin, ava in rows:
            for who, want in (("admin", admin), ("ava", ava)):
                r = c.get(
                    "/atlassian/wiki/rest/api/search", headers=callers[who], params={"cql": cql}
                ).json()
                assert {x["title"] for x in r["results"]} == want, (cql, who)
                assert r["totalSize"] == len(want), (cql, who)


def test_confluence_storage_roundtrip(client, admin_h, ro_conn):
    doc = ro_conn.execute("SELECT * FROM confluence_pages LIMIT 1").fetchone()
    cid = doc["id"]
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


_ENTITY_TEXT = "'\"\u00e9\u2014\u0160\u0161\u00a0\u2603\u2329&<>"
_ENTITY_STORAGE = "'&quot;&eacute;&mdash;\u0160\u0161&nbsp;\u2603&lang;&amp;&lt;&gt;"
_ENTITY_RENDERED = "'&quot;\u00e9\u2014\u0160\u0161\u00a0\u2603\u2329&amp;&lt;&gt;"
_LAYOUT = '<div class="contentLayout2"><div class="columnLayout single">{}</div></div>'


@pytest.mark.parametrize(
    "route, representation, value",
    [
        ("", "storage", f"<p>{_ENTITY_STORAGE}</p>"),
        ("", "view", _LAYOUT.format(f'<p class="auto-cursor-target">{_ENTITY_RENDERED}</p>')),
        ("", "export_view", _LAYOUT.format(f"<p>{_ENTITY_RENDERED}</p>")),
        ("/child/comment", "storage", f"<p>{_ENTITY_STORAGE}</p>"),
        (
            "/child/comment",
            "view",
            _LAYOUT.format(f'<p class="auto-cursor-target">{_ENTITY_RENDERED}</p>'),
        ),
    ],
)
def test_confluence_a_body_is_written_with_each_representations_entities(
    tmp_path, route, representation, value
):
    """A page's body and its comment's, holding characters
    `backlot.routers.atlassian._CONFLUENCE_STORAGE_ENTITIES` records real writing."""
    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "doc_id": "entities",
                "space": "eng",
                "title": "P",
                "content": _ENTITY_TEXT,
                "author_email": "a@x.com",
                "visibility": "public",
                "comments": [{"content": _ENTITY_TEXT, "author_email": "b@x.com"}],
            }
        ],
    )
    page = served_id("confluence", "entities")
    with client_for(s, reload=True) as c:
        r = c.get(
            f"/atlassian/wiki/rest/api/content/{page}{route}",
            headers={"Authorization": f"Bearer {s.admin_token}"},
            params={"expand": f"body.{representation}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()["results"][0]["body"] if route else r.json()["body"]
    assert body[representation]["value"] == value


def test_atlassian_errors_use_atlassian_envelope(client, admin_h):
    # atlassian-python-api's Confluence client does response.json()["message"] on any error, so
    # Backlot must shape /atlassian errors like Cloud does, not {"detail"}: each product's own two
    # members (`errors.atlassian.http_body`).
    r = client.get("/atlassian/wiki/rest/api/content/999999")  # unauthenticated -> 403
    assert r.status_code == 403
    assert sorted(r.json()) == ["message", "statusCode"] and r.json()["statusCode"] == 403
    r2 = client.get(
        "/atlassian/wiki/rest/api/content/abc", headers=admin_h
    )  # an id `ContentId` cannot read -> Spring's 400
    assert r2.status_code == 400 and sorted(r2.json()) == ["message", "statusCode"]
    # non-atlassian paths keep FastAPI's default {"detail"} envelope
    r3 = client.get("/no-such-route")
    assert r3.status_code == 404 and "detail" in r3.json() and "message" not in r3.json()


def test_confluence_spaces_are_paged_not_served_whole(client, admin_h, tokens):
    """Measured against a live Confluence Cloud site on 2026-09-17: `space` reads `limit`/`start`
    and answers a page, with `_links.next`/`.prev` shaped like
    :func:`backlot.pagination.confluence_page_links`."""
    unpaged = client.get("/atlassian/wiki/rest/api/space", headers=admin_h).json()
    names = [s["name"] for s in unpaged["results"]]
    assert names == [
        "handbook",
        "people-ops",
    ]  # the order every slice below relies on (see confluence_spaces's own comment on why)
    assert unpaged["start"] == 0 and unpaged["limit"] == 25 and unpaged["size"] == 2
    assert unpaged["_links"] == {
        "base": "http://testserver/wiki",
        "context": "/wiki",
        "self": "http://testserver/wiki/rest/api/space",
    }  # a full page: no next, no prev. `self` carries no query string for a request that sends
    # none of `limit`/`start`/`expand`/`spaceKey` — the only parameters this route reads.

    # `_site` echoes the caller's own Host, not a fixed org name — proved here since this route's
    # `base`/`self` are the only place that claim goes untested.
    via_alias = client.get(
        "/atlassian/wiki/rest/api/space", headers={**admin_h, "Host": "example.test"}
    ).json()
    assert via_alias["_links"]["base"] == "http://example.test/wiki"
    assert via_alias["_links"]["self"] == "http://example.test/wiki/rest/api/space"

    first = client.get("/atlassian/wiki/rest/api/space?limit=1", headers=admin_h).json()
    assert [s["name"] for s in first["results"]] == ["handbook"]
    assert (first["start"], first["limit"], first["size"]) == (0, 1, 1)
    assert first["_links"]["next"] == "/rest/api/space?next=true&limit=1&start=1"
    assert "prev" not in first["_links"]

    second = client.get("/atlassian/wiki/rest/api/space?start=1", headers=admin_h).json()
    assert [s["name"] for s in second["results"]] == ["people-ops"]
    assert (second["start"], second["limit"], second["size"]) == (1, 25, 1)
    assert second["_links"]["prev"] == "/rest/api/space?prev=true&limit=1&start=0"
    assert "next" not in second["_links"]

    # expand rides into next/prev/self too — see confluence_page_links for the ordering.
    expanded_first = client.get(
        "/atlassian/wiki/rest/api/space?limit=1&expand=description", headers=admin_h
    ).json()
    # a bare `description` carries no value (see `_space_description`'s own docstring) — its
    # `_expandable` moving under the per-space entry is enough to prove `expand` reached `_space`.
    assert expanded_first["results"][0]["description"] == {"_expandable": {"view": "", "plain": ""}}
    assert (
        expanded_first["_links"]["next"]
        == "/rest/api/space?next=true&expand=description&limit=1&start=1"
    )
    assert (
        expanded_first["_links"]["self"]
        == "http://testserver/wiki/rest/api/space?expand=description"
    )

    expanded_second = client.get(
        "/atlassian/wiki/rest/api/space?start=1&limit=1&expand=description", headers=admin_h
    ).json()
    assert (
        expanded_second["_links"]["prev"]
        == "/rest/api/space?expand=description&prev=true&limit=1&start=0"
    )

    # a negative or unconvertible value is refused the same way `content` refuses it — shared
    # through `_confluence_page_params` — proved once here rather than the whole matrix again.
    negative = client.get("/atlassian/wiki/rest/api/space?limit=-1", headers=admin_h)
    assert negative.status_code == 400
    assert (
        negative.json()["message"]
        == "java.lang.IllegalArgumentException: limit cannot be less than zero"
    )

    # ACL-scoped: `total` (and so where `next`/`prev` land) is the caller's own reachable set, not
    # the corpus's. ava reaches only "handbook", so her one-row page carries neither link.
    ava_h = {"Authorization": f"Bearer {tokens['ava@acme.com']}"}
    scoped = client.get("/atlassian/wiki/rest/api/space?limit=1", headers=ava_h).json()
    assert [s["name"] for s in scoped["results"]] == ["handbook"]
    assert "next" not in scoped["_links"] and "prev" not in scoped["_links"]


# The keys of the bundled corpus's two spaces, which a row below names as `{hb}` and `{po}`
_SPACE_KEYS = {
    "hb": synth.confluence_space_key("handbook"),
    "po": synth.confluence_space_key("people-ops"),
    "po_lower": synth.confluence_space_key("people-ops").lower(),
}

# What `space?spaceKey=` lists, as `_space_keys` and `confluence_spaces` record real reading it:
# the query, whose token asks, the spaces listed, and whether the page answers a `next`. ava
# reaches `handbook` alone.
# fmt: off
_SPACE_KEY_ROWS = [
    ([("spaceKey", "{po}")], "admin", ["people-ops"], False),
    ([("spaceKey", "{po_lower}")], "admin", ["people-ops"], False),
    ([("spaceKey", "NOSUCH")], "admin", [], False),
    ([("spaceKey", "{po}"), ("spaceKey", "{hb}")], "admin", ["handbook", "people-ops"], False),
    ([("spaceKey", "{po}"), ("spaceKey", "{po}")], "admin", ["people-ops"], False),
    ([("spaceKey", "{po},NOSUCH")], "admin", ["people-ops"], False),
    ([("spaceKey", "{po},{hb}")], "admin", ["handbook", "people-ops"], False),
    ([("spaceKey", " {po}\t")], "admin", ["people-ops"], False),
    ([("spaceKey", "\u00a0{po}")], "admin", [], False),
    ([("spaceKey", "NOSUCH"), ("spaceKey", " {po}")], "admin", [], False),
    ([("spaceKey", "NOSUCH"), ("spaceKey", "{po},{hb}")], "admin", [], False),
    ([("spaceKey", "{po}"), ("spaceKey", "")], "admin", ["people-ops"], False),
    ([("spaceKey", "")], "admin", ["handbook", "people-ops"], False),
    ([("spaceKey", " ")], "admin", [], False),
    ([("spaceKey", ",")], "admin", [], False),
    ([("spaceKey", "{hb}"), ("limit", "1")], "admin", ["handbook"], False),
    ([("spaceKey", "{po},{hb}"), ("limit", "1")], "admin", ["handbook"], True),
    ([("spaceKey", "{po}")], "ava", [], False),
    ([("spaceKey", "{po},{hb}"), ("limit", "1")], "ava", ["handbook"], False),
]
# fmt: on


@pytest.mark.parametrize("params, caller, names, has_next", _SPACE_KEY_ROWS)
def test_confluence_space_listing_lists_the_spaces_space_key_names(
    client, admin_h, tokens, params, caller, names, has_next
):
    """Over the bundled corpus, for an admin and for ava, the scoped token the narrowing must not
    widen; the measurement behind each row is in :func:`backlot.routers.atlassian._space_keys` and
    :func:`backlot.routers.atlassian.confluence_spaces`."""
    headers = (
        admin_h if caller == "admin" else {"Authorization": f"Bearer {tokens['ava@acme.com']}"}
    )
    sent = [(name, value.format(**_SPACE_KEYS)) for name, value in params]
    r = client.get("/atlassian/wiki/rest/api/space", params=sent, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["name"] for s in body["results"]] == names
    assert (body["start"], body["size"]) == (0, len(names))
    assert ("next" in body["_links"]) is has_next


def test_confluence_space_listing_declares_the_parameters_it_reads(client):
    """The served spec names `spaceKey` beside the page pair and `expand`, so a generated client
    can find the narrowing."""
    op = client.app.openapi()["paths"]["/atlassian/wiki/rest/api/space"]["get"]
    assert {p["name"] for p in op["parameters"]} == {"expand", "spaceKey", "limit", "start"}


def test_confluence_single_space_get(client, admin_h):
    spaces = client.get("/atlassian/wiki/rest/api/space", headers=admin_h).json()["results"]
    assert spaces
    key = spaces[0]["key"]
    r = client.get(f"/atlassian/wiki/rest/api/space/{key}", headers=admin_h)
    assert r.status_code == 200 and r.json()["key"] == key and r.json()["name"] == spaces[0]["name"]
    # the reader roster is the admin's to read, under the expansion real answers it with
    perm = client.get(f"/atlassian/wiki/rest/api/space/{key}?expand=permissions", headers=admin_h)
    assert perm.status_code == 200
    assert perm.json()["permissions"][0]["operation"] == {
        "operation": "read",
        "targetType": "space",
    }
    # No space has these keys, and each is refused naming it as `errors_atlassian.space_not_found`
    # records, with the expansion and without it.
    for sent, named in (
        ("NOSUCH", "NOSUCH"),
        ("nosuch?expand=permissions", "nosuch"),
        ("%20NOSUCH", " NOSUCH"),
        ("NO%3FSUCH", "NO?SUCH"),
    ):
        absent = client.get(f"/atlassian/wiki/rest/api/space/{sent}", headers=admin_h)
        assert absent.status_code == 404, sent
        assert absent.json() == {
            "statusCode": 404,
            "message": (
                "org.springframework.web.server.ResponseStatusException: "
                f'404 NOT_FOUND "No space found with key : {named}"'
            ),
        }, sent


def test_confluence_writes_a_character_past_the_bmp_as_surrogate_escapes(client, admin_h):
    """Pins the bytes ``errors.atlassian._confluence_json`` writes, on the raw body, since
    ``.json()`` decodes both writings to one string."""
    emoji = "%F0%9F%98%80"  # U+1F600
    for path, status, written in (
        (
            f"/atlassian/wiki/rest/api/space/{emoji}",
            404,
            rb'No space found with key : \uD83D\uDE00\""',
        ),
        (
            f"/atlassian/wiki/rest/api/content?spaceKey={emoji}",
            404,
            rb"No space with key : \uD83D\uDE00",
        ),
        (
            f"/atlassian/wiki/rest/api/content?limit={emoji}",
            400,
            rb"For input string: \"\uD83D\uDE00\"",
        ),
        (
            f"/atlassian/wiki/rest/api/content/229469/label?prefix={emoji}",
            400,
            rb"label prefixes :[\uD83D\uDE00]",
        ),
        (
            f"/atlassian/wiki/rest/api/content?type={emoji}",
            501,
            rb"Cannot find custom content type : \uD83D\uDE00",
        ),
        (
            "/atlassian/wiki/rest/api/content/%F0%9D%9F%93/child/page",
            400,
            rb"for value [\uD835\uDFD3]",
        ),
        (
            f"/atlassian/wiki/rest/api/search?cql=title~%22{emoji}x%22&limit=1",
            200,
            rb'\"\uD83D\uDE00x\""',
        ),
        (
            f"/atlassian/wiki/rest/api/search?cql=label%3D%22{emoji}x%22",
            200,
            rb'\"\uD83D\uDE00x\""',
        ),
    ):
        answer = client.get(path, headers=admin_h)
        assert answer.status_code == status, path
        assert written in answer.content, path
        # and never the character's own UTF-8 bytes
        assert "\U0001f600".encode() not in answer.content, path
        assert "\U0001d7d3".encode() not in answer.content, path
    # A character inside the plane is written as its UTF-8 bytes, unescaped.
    inside = client.get("/atlassian/wiki/rest/api/space/%C3%A9", headers=admin_h)
    assert inside.status_code == 404
    assert "é".encode() in inside.content


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
    search = client.get(
        "/atlassian/rest/api/3/search/jql", headers=admin_h, params={"jql": "project is not EMPTY"}
    ).json()
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


def test_jira_issue_key_asserts_rather_than_re_derive_a_null_key():
    """`_issue_key` must not fall back to re-deriving a key from a NULL one: a PROBED row (one whose
    served value came from a walk, not a pure hash) would advertise a key nobody stored, unreachable
    at its own url. An assertion is strictly better: every jira row gets a key at import
    (`resolve_jira_keys` raises rather than leave one NULL), so reaching here with one is a bug
    upstream, and failing loudly
    beats silently serving the wrong key."""
    from backlot.routers.atlassian import _issue_key

    with pytest.raises(AssertionError, match="no key"):
        _issue_key(bare_request(), {"key": None, "project": "x"})


def _jira_row(conn, title: str):
    """The jira row a fixture record with this title became.

    A jira key is assigned across the whole corpus, so unlike a hashed id it cannot be
    computed from the record's own identifier — which does not survive the import anyway. The row
    is found by something the fixture can still see, as any other client would have to."""
    return conn.execute("SELECT * FROM jira_issues WHERE title = ?", (title,)).fetchone()


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
    f = _jira_issue(conn, bare_request(), _jira_row(conn, "T"))["fields"]
    # the real 3-category model: "In Progress" -> indeterminate (not the old hardcoded "new")
    assert f["status"]["statusCategory"]["key"] == "indeterminate"
    assert f["assignee"]["emailAddress"] == "a@x.com"
    assert f["reporter"]["emailAddress"] == "b@x.com"
    # `reporter` is optional, as it is in Jira -- "not required by default, and users can leave it
    # empty" -- and an issue stating none reports its author, which is what makes the field
    # optional rather than missing.
    d = _jira_issue(conn, bare_request(), _jira_row(conn, "D"))["fields"]
    assert d["reporter"]["emailAddress"] == "ava@acme.com"
    assert f["resolution"]["name"] == "Done" and f["resolutiondate"].startswith("2026-03-01")
    assert f["duedate"] == "2026-04-01" and f["fixVersions"][0]["name"] == "1.2.0"
    # richer actor object
    assert "avatarUrls" in f["assignee"] and f["assignee"]["accountType"] == "atlassian"
    # scaffolds present so probing clients get [] / null, not KeyError
    assert f["attachment"] == [] and f["votes"]["votes"] == 0

    done = _jira_issue(conn, bare_request(), _jira_row(conn, "D"))["fields"]
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
    row = store.get_document(conn, "confluence", served_id("confluence", "c1"))
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


def _members(node: dict) -> str:
    """An answer's member names in order, nested down to each restriction list."""
    return " ".join(
        f"{k}{{{_members(v)}}}"
        if isinstance(v, dict) and k not in ("user", "group", "_links")
        else k
        for k, v in node.items()
    )


_BOTH_LISTS = "restrictions{user group} _expandable{content} _links"
_NO_LISTS = "_expandable{restrictions content} _links"


@pytest.mark.parametrize(
    "query, members",
    [
        ("", f"read{{operation {_BOTH_LISTS}}} update{{operation {_BOTH_LISTS}}} _links"),
        ("expand=", f"read{{operation {_BOTH_LISTS}}} update{{operation {_BOTH_LISTS}}} _links"),
        ("expand=read", f"read{{operation {_NO_LISTS}}} _expandable{{update}} _links"),
        ("expand=update", f"update{{operation {_NO_LISTS}}} _expandable{{read}} _links"),
        (
            "expand=update,read",
            f"read{{operation {_NO_LISTS}}} update{{operation {_NO_LISTS}}} _links",
        ),
        (
            "expand=update&expand=read",
            f"read{{operation {_NO_LISTS}}} update{{operation {_NO_LISTS}}} _links",
        ),
        ("expand=&expand=read", f"read{{operation {_NO_LISTS}}} _expandable{{update}} _links"),
        ("expand=,read,,", f"read{{operation {_NO_LISTS}}} _expandable{{update}} _links"),
        ("expand=read.bogus", f"read{{operation {_NO_LISTS}}} _expandable{{update}} _links"),
        (
            "expand=read..restrictions",
            f"read{{operation {_NO_LISTS}}} _expandable{{update}} _links",
        ),
        (
            "expand=read.restrictions.user",
            "read{operation restrictions{user _expandable{group}} _expandable{content} _links}"
            " _expandable{update} _links",
        ),
        (
            "expand=read.restrictions.user.bogus",
            "read{operation restrictions{user _expandable{group}} _expandable{content} _links}"
            " _expandable{update} _links",
        ),
        (
            "expand=update.restrictions.group",
            "update{operation restrictions{group _expandable{user}} _expandable{content} _links}"
            " _expandable{read} _links",
        ),
        (
            "expand=read.restrictions",
            "read{operation restrictions{_expandable{user group}} _expandable{content} _links}"
            " _expandable{update} _links",
        ),
        (
            "expand=read.restrictions.USER",
            "read{operation restrictions{_expandable{user group}} _expandable{content} _links}"
            " _expandable{update} _links",
        ),
        (
            "expand=read.restrictions.group,read.restrictions.user",
            f"read{{operation {_BOTH_LISTS}}} _expandable{{update}} _links",
        ),
        (
            "expand=read.restrictions.user,update",
            "read{operation restrictions{user _expandable{group}} _expandable{content} _links}"
            f" update{{operation {_NO_LISTS}}} _links",
        ),
        ("expand=bogus", "_expandable{read update} _links"),
        ("expand=READ", "_expandable{read update} _links"),
        ("expand=%20read", "_expandable{read update} _links"),
        ("expand=.read", "_expandable{read update} _links"),
        ("expand=%20", "_expandable{read update} _links"),
    ],
)
def test_confluence_restrictions_answer_what_expand_names_in_real_shape(tmp_path, query, members):
    """Each row is real's answer to the same query, member names and order, as measured where
    `_restriction_expansions` and the comment in `confluence_restrictions` say. Every operation
    served links to itself, and the answer ends with real's links."""
    import asyncio
    import types

    from backlot.acl import Acl
    from backlot.routers.atlassian import _site, confluence_restrictions

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
    conn = store.connect_ro(s.db_path)
    cid = served_id("confluence", "c2")
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            conn=conn,
            acl=Acl.load(s.tokens_path, s.admin_token, s.org_name),
        )
    )
    scope = {
        "type": "http",
        "scheme": "http",
        "server": ("m", 80),
        "path": "/",
        "query_string": query.encode(),
        "app": app,
        "headers": [(b"authorization", f"Bearer {s.admin_token}".encode())],
    }
    request = Request(scope)
    result = asyncio.run(confluence_restrictions(str(cid), request))
    assert _members(result) == members
    path = f"{_site(request)}/wiki/rest/api/content/{cid}/restriction/byOperation"
    assert result["_links"] == {"self": path, "base": f"{_site(request)}/wiki", "context": "/wiki"}
    for op in ("read", "update"):
        if op in result:
            assert result[op]["_links"] == {"self": f"{path}/{op}"}
            if "user" in result[op].get("restrictions", {}):
                assert result[op]["restrictions"]["user"]["results"]  # the private doc's author


@pytest.mark.parametrize("spelt", ["{}", "0{}", "att{}"])
@pytest.mark.parametrize("route", ["child/page", "child/attachment", "restriction/byOperation"])
def test_confluence_a_page_an_outsider_cannot_read_is_a_made_up_id(
    client, admin_h, tokens, route, spelt
):
    """A page the caller cannot read answers what a made-up id answers, status and body byte for
    byte but for the id each 404 names, which is the number the caller sent, so neither a
    listing's 200 nor `restriction/byOperation`'s reader roster tells an outsider the page is
    there, whichever of these spellings names it. The admin's 200 on the same URL is the
    control."""
    cid = served_id("confluence", "cf-comp")  # people-only
    path = f"/atlassian/wiki/rest/api/content/{spelt.format(cid)}/{route}"
    served = client.get(path, headers=admin_h)
    assert served.status_code == 200, served.text

    h = {"Authorization": f"Bearer {tokens['ava@acme.com']}"}  # engineering; cannot see cf-comp
    hidden = client.get(path, headers=h)
    made_up = client.get(
        f"/atlassian/wiki/rest/api/content/{spelt.format(999999999)}/{route}", headers=h
    )
    assert hidden.status_code == made_up.status_code == 404
    assert str(cid).encode() in hidden.content
    assert hidden.content.replace(str(cid).encode(), b"999999999") == made_up.content


# --- Confluence content/{id}: the id as `ContentId` reads it, and the 404 of one naming nothing --

#: Spring's 400 for an id `ContentId` cannot read, up to the value it names.
_CONTENT_ID_400_HEAD = (
    "org.springframework.web.method.annotation.MethodArgumentTypeMismatchException: Failed to "
    "convert value of type 'java.lang.String' to required type "
    "'com.atlassian.confluence.api.model.content.id.ContentId'; nested exception is "
    "org.springframework.core.convert.ConversionFailedException: Failed to convert from type "
    "[java.lang.String] to type [@org.springframework.web.bind.annotation.PathVariable "
    "com.atlassian.confluence.api.model.content.id.ContentId] for value "
)
_CANNOT_PARSE = (
    "com.atlassian.confluence.api.service.exceptions.api.BadRequestException: "
    "Can't parse as a ContentId"
)


def _number_format(rest):
    return f'java.lang.NumberFormatException: For input string: "{rest}"'


@pytest.mark.parametrize(
    "sent, read",
    [
        # sent on `child/page`, and the id real named in its 404
        ("05", 5),
        ("+5", 5),
        ("%2B5", 5),
        ("0000000000000000000000000005", 5),
        ("-0", 0),
        ("%D9%A5", 5),
        ("%EF%BC%95", 5),
        ("9223372036854775807", 2**63 - 1),
        ("-9223372036854775808", -(2**63)),
        ("att5", 5),
        ("att05", 5),
        ("att+5", 5),
        ("att-5", -5),
        ("att%D9%A5", 5),
        # or the value its 400 named, and how the message ended
        ("abc", ("abc", _CANNOT_PARSE)),
        ("1.5", ("1.5", _CANNOT_PARSE)),
        ("1e3", ("1e3", _CANNOT_PARSE)),
        ("0x10", ("0x10", _CANNOT_PARSE)),
        ("1_0", ("1_0", _CANNOT_PARSE)),
        ("5.", ("5.", _CANNOT_PARSE)),
        (".5", (".5", _CANNOT_PARSE)),
        ("+", ("+", _CANNOT_PARSE)),
        ("-", ("-", _CANNOT_PARSE)),
        ("--5", ("--5", _CANNOT_PARSE)),
        ("+-5", ("+-5", _CANNOT_PARSE)),
        ("%20", (" ", _CANNOT_PARSE)),
        ("%205", (" 5", _CANNOT_PARSE)),
        ("5%09", ("5" + chr(9), _CANNOT_PARSE)),
        ("%F0%9D%9F%93", (chr(0x1D7D3), _CANNOT_PARSE)),
        ("9223372036854775808", ("9223372036854775808", _CANNOT_PARSE)),
        ("-9223372036854775809", ("-9223372036854775809", _CANNOT_PARSE)),
        ("99999999999999999999", ("99999999999999999999", _CANNOT_PARSE)),
        ("search", ("search", _CANNOT_PARSE)),
        ("ATT999999999", ("ATT999999999", _CANNOT_PARSE)),
        ("Att5", ("Att5", _CANNOT_PARSE)),
        ("xatt5", ("xatt5", _CANNOT_PARSE)),
        ("att", ("att", _number_format(""))),
        ("attabc", ("attabc", _number_format("abc"))),
        ("att%205", ("att 5", _number_format(" 5"))),
        (
            "att9223372036854775808",
            ("att9223372036854775808", _number_format("9223372036854775808")),
        ),
    ],
)
def test_confluence_reads_a_content_id_as_contentid_does(client, admin_h, sent, read):
    """``read`` is the id real named in its 404, or the value its 400 named and the end of that
    400's message."""
    r = client.get(f"/atlassian/wiki/rest/api/content/{sent}/child/page", headers=admin_h)
    if isinstance(read, int):
        assert r.status_code == 404, r.text
        assert r.json()["message"].endswith(f" : ContentId{{id={read}}}")
    else:
        value, cause = read
        assert r.status_code == 400, r.text
        message = f"{_CONTENT_ID_400_HEAD}[{value}]; nested exception is {cause}"
        assert r.json() == {"statusCode": 400, "message": message}


@pytest.mark.parametrize("route", ["", "/label", "/child/comment", "/restriction/byOperation"])
@pytest.mark.parametrize("sent", ["abc", "attabc"])
def test_confluence_reads_the_content_id_alike_on_every_route(client, admin_h, sent, route):
    """The same 400 for the same id on each route under `content/{id}`, `child/page`'s being the
    control."""
    api = "/atlassian/wiki/rest/api/content"
    r = client.get(f"{api}/{sent}{route}", headers=admin_h)
    assert r.status_code == 400, r.text
    assert r.content == client.get(f"{api}/{sent}/child/page", headers=admin_h).content


_SERVED_PAGE = "{page}"


@pytest.mark.parametrize(
    "path, sentence, data, length",
    [
        # under `content/`; the sentence after `NotFoundException: `; which `data`; real's bytes
        ("999999999", "No content found with id : 999999999", "refused", 287),
        ("999999999/label", "No content found with id : 999999999", "refused", 287),
        (
            "999999999/child/page",
            "No parent or not permitted to view content with id : ContentId{id=999999999}",
            "readable",
            248,
        ),
        (
            "999999999/child/comment",
            "No parent or not permitted to view content with id : ContentId{id=999999999}",
            "readable",
            248,
        ),
        (
            "999999999/child/attachment",
            "No content found with id: ContentId{id=999999999}",
            "readable",
            221,
        ),
        (
            "999999999/restriction/byOperation",
            "No content with id <ContentId{id=999999999}> can be found",
            "refused",
            329,
        ),
        # an attachment's id
        ("att999999999", "null", "attachment", 178),
        ("att999999999/label", "null", "attachment", 178),
        (f"att{_SERVED_PAGE}", "null", "attachment", 178),
        (f"att{_SERVED_PAGE}/label", "null", "attachment", 178),
        (
            "att999999999/child/page",
            "No parent or not permitted to view content with id : ContentId{id=999999999}",
            "readable",
            248,
        ),
        (
            "att999999999/child/attachment",
            "No content found with id: ContentId{id=999999999}",
            "readable",
            221,
        ),
        (
            "att999999999/restriction/byOperation",
            "No content with id <ContentId{id=999999999}> can be found",
            "refused",
            329,
        ),
        # what `label` refuses only after its lookup, which an id naming nothing never reaches
        ("999999999/label?limit=-1", "No content found with id : 999999999", "refused", 287),
        ("999999999/label?limit=0", "No content found with id : 999999999", "refused", 287),
        ("999999999/label?start=-1", "No content found with id : 999999999", "refused", 287),
        (f"att{_SERVED_PAGE}/label?limit=-1", "null", "attachment", 178),
        (f"att{_SERVED_PAGE}/label?limit=0", "null", "attachment", 178),
        # with nothing wrong but the id, each listing looks it up
        (
            "999999999/child/page?limit=0",
            "No parent or not permitted to view content with id : ContentId{id=999999999}",
            "readable",
            248,
        ),
        (
            "999999999/child/comment?limit=0",
            "No parent or not permitted to view content with id : ContentId{id=999999999}",
            "readable",
            248,
        ),
        (
            "999999999/child/attachment?limit=0",
            "No content found with id: ContentId{id=999999999}",
            "readable",
            221,
        ),
    ],
)
def test_confluence_answers_an_id_naming_nothing_with_reals_404(
    client, admin_h, path, sentence, data, length
):
    """Each row is real's 404 to the request named, in its three members in its order."""
    page = served_id("confluence", "cf-comp")
    r = client.get(f"/atlassian/wiki/rest/api/content/{path.format(page=page)}", headers=admin_h)
    shape = {
        "readable": {"authorized": True, "valid": True, "errors": [], "successful": True},
        "refused": {
            "authorized": False,
            "valid": False,
            "errors": [{"message": {"translation": sentence, "args": []}}],
            "successful": False,
        },
        "attachment": {"authorized": False, "valid": True, "errors": [], "successful": False},
    }[data]
    want = {
        "statusCode": 404,
        "data": shape,
        "message": f"com.atlassian.confluence.api.service.exceptions.api.NotFoundException: {sentence}",
    }
    assert r.status_code == 404, r.text
    assert r.content == json.dumps(want, separators=(",", ":")).encode()
    assert len(r.content) == length


@pytest.mark.parametrize(
    "path", ["abc", "abc/child/page", "att5/child/page", "abc/label?limit=abc"]
)
def test_confluence_reads_the_content_id_after_the_credential(client, path):
    """With no `Authorization`, real's 403 comes ahead of the id's 400 and the parameter's."""
    r = client.get(f"/atlassian/wiki/rest/api/content/{path}")
    assert r.status_code == 403, r.text
    assert "caller cannot access Confluence" in r.json()["message"]


# --- Jira: a page of comments is a page -------------------------------------------------------
#
# Measured against Jira Cloud on an issue with no comments, which settles every one of these:
# parameter validation runs before the issue is resolved. The dates differ and say which instance
# state each claim reflects — 2026-09-09 for the defaults, the clamps, the caps and `created` /
# `+created` / `-created` against `bogus`; 2026-09-10 for the rest of the `orderBy` grammar (one
# leading sigil, whitespace ignored, field case-insensitive), for the empty value being refused
# rather than read as absent, and for the 400 arriving before the 404.
#
# What `-created` does to a non-empty list comes from Atlassian's document rather than from a
# call, and the no-`orderBy` case from neither — see that test's own docstring.

# The corpus lists these OUT of chronological order on purpose: `comment 1` is the newest and
# `comment 7` the oldest. A fixture whose array order matches its clock cannot tell a real sort
# from `store.doc_comments`' `ORDER BY seq`, so every ascending assertion below would pass against
# an implementation that ignores `orderBy` altogether.
_COMMENTS = [
    {"content": f"comment {i}", "author_email": "b@x.com", "created_ts": 1770000000 + (8 - i) * 60}
    for i in range(1, 8)
]


@pytest.fixture(scope="module")
def paged(tmp_path_factory):
    """One issue with seven comments, served."""
    settings = tiny_corpus(
        tmp_path_factory.mktemp("paged"),
        [
            {
                "source_type": "jira",
                "doc_id": "j-page",
                "project": "payments",
                "title": "T",
                "content": "c",
                "author_email": "a@x.com",
                "visibility": "public",
                "key": "PAY-7",
                "comments": _COMMENTS,
            }
        ],
    )
    with client_for(settings, reload=True) as client:
        tok = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))["admin_token"]
        yield client, {"Authorization": f"Bearer {tok}"}


def _page(paged, **params):
    client, h = paged
    r = client.get("/atlassian/rest/api/3/issue/PAY-7/comment", headers=h, params=params)
    return r, r.json()


def _bodies(d):
    return [c["body"]["content"][0]["content"][0]["text"] for c in d["comments"]]


def test_jira_comments_page_on_start_at_and_max_results(paged):
    """The endpoint declares both, and the envelope has always claimed to be a page. Serving the
    whole collection labelled `startAt: 0` hands a client asking for page two the contents of page
    one, with nothing in the response to say so."""
    r, d = _page(paged, startAt=3, maxResults=2)
    assert r.status_code == 200
    assert (d["startAt"], d["maxResults"], d["total"]) == (3, 2, 7)
    assert _bodies(d) == ["comment 4", "comment 5"]


def test_jira_comments_default_to_the_first_hundred(paged):
    """Measured: with no parameters real Jira echoes `maxResults: 100`, not the size of the
    collection."""
    _r, d = _page(paged)
    assert (d["startAt"], d["maxResults"], d["total"]) == (0, 100, 7)
    assert len(d["comments"]) == 7


@pytest.mark.parametrize(
    "params,want",
    [
        # measured: a negative offset floors at 0, one past the end is echoed back unchanged
        ({"startAt": -1}, (0, 100, 7)),
        ({"startAt": 100}, (100, 100, 7)),
        # measured: maxResults floors UP to 1 -- 0 and -1 both answer 1, not 0
        ({"maxResults": 0}, (0, 1, 7)),
        ({"maxResults": -1}, (0, 1, 7)),
        # measured: capped at 100, which is the documented default AND the maximum
        ({"maxResults": 100000}, (0, 100, 7)),
    ],
)
def test_jira_comment_paging_clamps_the_way_the_real_api_does(paged, params, want):
    _r, d = _page(paged, **params)
    assert (d["startAt"], d["maxResults"], d["total"]) == want


def test_jira_comments_past_the_end_are_an_empty_page_not_an_error(paged):
    _r, d = _page(paged, startAt=100)
    assert d["comments"] == []


@pytest.mark.parametrize(
    "order,want",
    [
        # `comment 7` is the OLDEST in this corpus and `comment 1` the newest, so an ascending
        # sort has to reorder the rows rather than pass them through.
        ("created", ["comment 7", "comment 6"]),
        ("+created", ["comment 7", "comment 6"]),
        ("-created", ["comment 1", "comment 2"]),
    ],
)
def test_jira_comments_order_by_created(paged, order, want):
    _r, d = _page(paged, orderBy=order, maxResults=2)
    assert _bodies(d) == want


def test_jira_comments_without_order_by_keep_the_order_the_corpus_states(paged):
    """What real Jira returns with no `orderBy` is NOT measured: the site available for measuring
    had no issue carrying a comment, and creating one is a write to a live instance. So the corpus
    keeps whatever order it stated, rather than this inventing a default sort.

    The fixture lists its comments newest-first, so a default sort would be visible here."""
    _r, d = _page(paged, maxResults=3)
    assert _bodies(d) == ["comment 1", "comment 2", "comment 3"]


@pytest.mark.parametrize(
    "query,want",
    [
        # Atlassian's own document writes the ascending form as `+created`, and a literal `+` in a
        # query string decodes to a space. Real Jira answers 200 to every one of these.
        ("orderBy=+created", ["comment 7", "comment 6"]),
        ("orderBy=%2Bcreated", ["comment 7", "comment 6"]),
        ("orderBy=%20created", ["comment 7", "comment 6"]),
        ("orderBy=created%20", ["comment 7", "comment 6"]),
        ("orderBy=Created", ["comment 7", "comment 6"]),
        ("orderBy=CREATED", ["comment 7", "comment 6"]),
        ("orderBy=-Created", ["comment 1", "comment 2"]),
        ("orderBy=-%20created", ["comment 1", "comment 2"]),
        # `_jira_order_desc` reads the rest, as real did.
        ("orderBy=%01created", ["comment 7", "comment 6"]),
        ("orderBy=created%01", ["comment 7", "comment 6"]),
        ("orderBy=%01-created", ["comment 1", "comment 2"]),
        ("orderBy=%0Bcreated", ["comment 7", "comment 6"]),
        ("orderBy=-%0Bcreated", ["comment 1", "comment 2"]),
        ("orderBy=-%0D%0Acreated", ["comment 1", "comment 2"]),
        ("orderBy=created%C2%85", ["comment 7", "comment 6"]),
    ],
)
def test_jira_order_by_takes_the_spellings_the_real_api_takes(paged, query, want):
    """Sent as a RAW query string, not through `params=`, which would percent-encode the `+` and
    never exercise the spelling Atlassian's document actually writes.

    Measured against Jira Cloud: one leading `+` or `-` is the direction sigil, whitespace around
    it is ignored, and the field is matched case-insensitively."""
    client, h = paged
    d = client.get(f"/atlassian/rest/api/3/issue/PAY-7/comment?{query}&maxResults=2", headers=h)
    assert d.status_code == 200, d.text
    assert _bodies(d.json()) == want


@pytest.mark.parametrize(
    "query, named",
    [
        ("orderBy=--created", "-created"),
        ("orderBy=%2B-created", "-created"),
        ("orderBy=", ""),
        ("orderBy=%C2%A0created", "\xa0created"),
        ("orderBy=%E2%80%83created", " created"),
        ("orderBy=-%01created", "\x01created"),
        ("orderBy=-%1Fcreated", "\x1fcreated"),
        ("orderBy=-%C2%A0created", "\xa0created"),
        ("orderBy=-%C2%85created", "-\x85created"),
        ("orderBy=-cre%0Aated", "-cre\nated"),
        ("orderBy=created%20%C2%85", "created "),
        ("orderBy=created%C2%85%C2%85", "created\x85\x85"),
    ],
)
def test_jira_refuses_the_spellings_the_real_api_refuses(paged, query, named):
    """Each row names the field real's 400 named, as `_jira_order_desc` reads the value; the
    sentence around it is Backlot's own."""
    client, h = paged
    r = client.get(f"/atlassian/rest/api/3/issue/PAY-7/comment?{query}", headers=h)
    assert r.status_code == 400, r.text
    assert r.json()["errorMessages"][0].endswith(f"Instead: {named}")


def test_jira_validates_order_by_before_resolving_the_issue(paged):
    """Measured: `GET /issue/NOPE-1/comment?orderBy=bogus` is 400 on real Jira while the same key
    without the parameter is 404, so the parameter is validated first.

    It leaks nothing: the 400 is identical whether the key exists, is hidden, or was never a key,
    so it separates none of the three."""
    client, h = paged
    assert client.get("/atlassian/rest/api/3/issue/NOPE-1/comment", headers=h).status_code == 404
    r = client.get("/atlassian/rest/api/3/issue/NOPE-1/comment?orderBy=bogus", headers=h)
    assert r.status_code == 400


def test_jira_sorts_the_whole_collection_before_slicing_it(paged):
    """A sort applied to the page instead of the collection passes every case that leaves `startAt`
    at 0, so the two are only told apart off the first page."""
    _r, d = _page(paged, orderBy="-created", startAt=2, maxResults=2)
    assert _bodies(d) == ["comment 3", "comment 4"]


def test_jira_orders_comments_sharing_a_timestamp_by_seq(tmp_path):
    """The tie-break the router promises. Two comments written in the same second come back in
    `seq` order ascending and reversed descending, rather than in whatever order the rows arrive
    in twice."""
    settings = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "jira",
                "doc_id": "j-tie",
                "project": "payments",
                "title": "T",
                "content": "c",
                "author_email": "a@x.com",
                "visibility": "public",
                "key": "PAY-9",
                "comments": [
                    {"content": "same A", "author_email": "b@x.com", "created_ts": 1770000000},
                    {"content": "same B", "author_email": "b@x.com", "created_ts": 1770000000},
                ],
            }
        ],
    )
    with client_for(settings, reload=True) as client:
        tok = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))["admin_token"]
        h = {"Authorization": f"Bearer {tok}"}
        url = "/atlassian/rest/api/3/issue/PAY-9/comment"
        asc = client.get(url, headers=h, params={"orderBy": "created"}).json()
        desc = client.get(url, headers=h, params={"orderBy": "-created"}).json()
        assert _bodies(asc) == ["same A", "same B"]
        assert _bodies(desc) == ["same B", "same A"]


@pytest.mark.parametrize("order", ["bogus", "updated", "-updated"])
def test_jira_refuses_an_order_by_field_that_is_not_created(paged, order):
    """Measured: real Jira answers 400 for any field but `created`. Accepting one silently would
    serve corpus order to a client that asked for something else, with nothing in the response
    saying the sort was dropped -- and would pass here while failing against Jira.

    Only the status and the envelope are reproduced. Jira's own message is localised to the
    account's language, so its wording is not portable."""
    r, d = _page(paged, orderBy=order)
    assert r.status_code == 400
    assert sorted(d) == ["errorMessages", "errors"] and d["errors"] == {}
    # The message names the field AFTER the direction sigil is stripped, which is what real Jira
    # echoes: `--created` there reports `-created`, not what was sent.
    assert d["errorMessages"][0].endswith(f"Instead: {order.lstrip('-+')}")
    assert "[created]" in d["errorMessages"][0]


def test_jira_comment_paging_is_declared_so_a_client_can_discover_it(paged):
    client, _h = paged
    op = client.app.openapi()["paths"]["/atlassian/rest/api/3/issue/{key}/comment"]["get"]
    declared = {p["name"] for p in op["parameters"]}
    assert {"startAt", "maxResults", "orderBy"} <= declared
    # `expand` is deliberately absent: real Jira takes one, Backlot does not honour it, and `qp`
    # is for parameters Backlot honours. `backlot diff --source jira` is where that gap is read.
    assert "expand" not in declared


# --- a project role's id: a `long` path variable, read before the project ---------------------

_MISSING_ID = "Required path variable 'id' is not present."
_NOT_ADMIN = b'{"errorMessages":["You cannot edit the configuration of this project."],"errors":{}}'
_NO_SUCH_ROLE = b'{"errorMessages":[],"errors":{}}'


def _unconverted(role_id: str) -> str:
    return f"Failed to convert 'id' with value: '{unquote(role_id)}'"


@pytest.mark.parametrize(
    "method,role_id,caller,body,status,answer",
    [
        # what a `long` path variable reads (``backlot.routers.atlassian._java_long``): the project
        # lookup comes next, the 404 a caller who sees no project gets
        *(
            ("GET", role_id, "none", None, 404, None)
            for role_id in (
                "-1",
                "+1",
                "01",
                "%201",
                "1%20",
                "%E2%80%83%201",
                "0x1",
                "0x01",
                "-0x1",
                "0X1",
                "-%231",
                "%D9%A3",
                "%EF%BC%91",
                "0x%EF%BC%91",
                "0x%EF%BC%A1",
                "9223372036854775807",
                "-9223372036854775808",
                "0x7fffffffffffffff",
                "-0x8000000000000000",
                "0" * 4400 + "1",
            )
        ),
        # what it does not, the 400 naming the value
        *(
            ("GET", role_id, "none", None, 400, _unconverted(role_id))
            for role_id in (
                "abc",
                "1.5",
                "1.0",
                "1e3",
                "1_0",
                "+",
                "-",
                "--1",
                "+-1",
                "+0x1",
                "+%231",
                "0x",
                "0x-1",
                "0x1g",
                "00x1",
                "%C2%A01",
                "1%C2%A0",
                "%F0%9D%9F%8F",
                "99999999999999999999",
                "9223372036854775808",
                "-9223372036854775809",
                "0x8000000000000000",
                "0x%C4%B0",
            )
        ),
        # whitespace alone, which leaves the variable missing
        ("GET", "%20", "none", None, 400, _MISSING_ID),
        ("GET", "%E2%80%83", "none", None, 400, _MISSING_ID),
        # a caller who sees the project gets the same 400, the project's one role for its id
        # however it is spelled, and the 404 for any other id read
        ("GET", "abc", "admin", None, 400, _unconverted("abc")),
        ("GET", "10002", "admin", None, 200, 10002),
        ("GET", "0x2712", "admin", None, 200, 10002),
        ("GET", "-0x1", "admin", None, 404, _NO_SUCH_ROLE),
        ("GET", "1%E2%80%831", "admin", None, 404, _NO_SUCH_ROLE),
        # the writes read it behind their 415 and ahead of the project
        ("DELETE", "abc", "none", None, 400, _unconverted("abc")),
        ("DELETE", "%20", "none", None, 400, _MISSING_ID),
        ("POST", "abc", "none", "{}", 400, _unconverted("abc")),
        ("PUT", "abc", "none", "{}", 400, _unconverted("abc")),
        ("POST", "abc", "admin", "{}", 400, _unconverted("abc")),
        ("PUT", "abc", "none", None, 415, "Content-Type 'null' is not supported."),
        ("DELETE", "-0x1", "none", None, 404, None),
        # a caller who sees the project and does not administer it: the same 400s, then the 401
        # whatever role the id names, on the reads and the writes alike
        ("GET", "abc", "ava", None, 400, _unconverted("abc")),
        ("GET", "%20", "ava", None, 400, _MISSING_ID),
        ("POST", "abc", "ava", "{}", 400, _unconverted("abc")),
        ("GET", "10002", "ava", None, 401, _NOT_ADMIN),
        ("GET", "-1", "ava", None, 401, _NOT_ADMIN),
        ("GET", "99999", "ava", None, 401, _NOT_ADMIN),
        ("POST", "99999", "ava", "{}", 401, _NOT_ADMIN),
        ("PUT", "99999", "ava", "{}", 401, _NOT_ADMIN),
        ("DELETE", "99999", "ava", None, 401, _NOT_ADMIN),
        # an `Accept` that admits no JSON is the 406, ahead of the id's 400 and of the 401
        ("GET", "10002", "ava-html", None, 406, None),
        ("GET", "abc", "ava-html", None, 406, None),
    ],
)
def test_jira_reads_a_role_id_as_a_long_before_the_project(
    client, admin_h, tokens, method, role_id, caller, body, status, answer
):
    """Each row as ``backlot.routers.atlassian._role_id`` reads the id, which real answered the same
    with and without a credential; the project is one the anonymous caller cannot see and the admin
    and ava can, ava does not administer it (``errors.atlassian.not_a_project_admin``), and an id
    the project has no role for is ``errors.atlassian.no_such_role``."""
    key = synth.jira_project_key("payments")
    headers = {
        "admin": dict(admin_h),
        "ava": {"Authorization": f"Bearer {tokens['ava@acme.com']}"},
        "ava-html": {"Authorization": f"Bearer {tokens['ava@acme.com']}", "Accept": "text/html"},
        "none": {},
    }[caller]
    if body is not None:
        headers["Content-Type"] = "application/json"
    r = client.request(
        method, f"/atlassian/rest/api/3/project/{key}/role/{role_id}", headers=headers, content=body
    )
    assert r.status_code == status, r.text
    if status in (400, 415):
        assert r.headers["content-type"] == "application/problem+json;charset=UTF-8"
        assert r.json()["detail"] == answer
        if "%" not in role_id:
            assert r.json()["instance"] == f"/rest/api/3/project/{key}/role/{role_id}"
    elif status == 200:
        assert r.json()["id"] == answer
    elif status == 406:
        assert r.headers["content-type"] == errors_atlassian.PROBLEM_JSON
    elif answer is not None:
        assert r.headers["content-type"] == "application/json;charset=UTF-8"
        assert r.content == answer
        realm = 'OAuth realm="http%3A%2F%2Ftestserver"' if status == 401 else None
        assert r.headers.get("www-authenticate") == realm
    else:
        assert r.json()["errorMessages"] == [f"No project could be found with key '{key}'."]


def test_jira_role_declares_its_id_as_the_integer_the_document_does(client):
    """The route reads the id as text so that it can answer real's 400s itself; the schema keeps
    the integer Jira's document declares."""
    op = client.app.openapi()["paths"]["/atlassian/rest/api/3/project/{key}/role/{role_id}"]["get"]
    (role_id,) = [p for p in op["parameters"] if p["name"] == "role_id"]
    assert role_id["schema"]["type"] == "integer"


def test_jira_role_list_links_the_role_its_read_answers(client, admin_h):
    """The list names one role and links it by the project's id
    (``backlot.routers.atlassian._jira_project_id``) and the role id
    (``backlot.routers.atlassian._JIRA_ROLE``) the role read answers 200 for, in the shape
    ``backlot.routers.atlassian.jira_project_role`` gives and with the project group's members as
    its actors. The read by the project's key answers the same body, its `self` the link by id. An
    id no project has is the project's 404. A function of its own because it follows a link the
    list emits."""
    key = synth.jira_project_key("payments")
    projects = client.get("/atlassian/rest/api/3/project/search", headers=admin_h).json()
    (project_id,) = [p["id"] for p in projects["values"] if p["key"] == key]
    listed = client.get(f"/atlassian/rest/api/3/project/{key}/role", headers=admin_h).json()
    assert list(listed) == ["Users"]
    path = urlsplit(listed["Users"]).path
    assert path == f"/rest/api/3/project/{project_id}/role/10002"
    role = client.get(f"/atlassian{path}", headers=admin_h).json()
    assert list(role) == ["self", "name", "id", "description", "actors", "scope"]
    assert role["self"] == listed["Users"]
    assert (role["id"], role["name"]) == (10002, "Users")
    assert role["description"]
    assert role["scope"] == {"type": "PROJECT", "project": {"id": project_id}}
    actors = role["actors"]
    assert [actor["id"] for actor in actors] == list(range(10000, 10000 + len(actors)))
    assert len(actors) > 1
    assert {actor["type"] for actor in actors} == {"atlassian-user-role-actor"}
    by_key = client.get(f"/atlassian/rest/api/3/project/{key}/role/10002", headers=admin_h).json()
    assert by_key["self"] == listed["Users"]
    assert by_key == role
    other = str(int(project_id) + 1)
    r = client.get(f"/atlassian/rest/api/3/project/{other}/role", headers=admin_h)
    assert (r.status_code, r.json()["errorMessages"]) == (
        404,
        [f"No project could be found with id '{other}'."],
    )


# --- how a query parameter is READ: conversion, range, repetition -------------------------

# The two products refuse a type-conversion failure in DIFFERENT envelopes, as measured at
# ``errors.atlassian.integer_conversion_failure``: Jira answers RFC 7807 on
# `application/problem+json`, Confluence its own two members
# (``errors.atlassian._confluence_envelope``) carrying a raw Java exception string on a bare
# `application/json`.


# the path a refusal echoes, as `_echoed_path` gives it
@pytest.mark.parametrize("slash", ["", "/"])
@pytest.mark.parametrize(
    "param,value,key",
    [
        ("maxResults", "abc", "PAY-7"),
        ("startAt", "abc", "PAY-7"),
        ("maxResults", "1.5", "PAY-7"),
        # where Python's own int() reads 10
        ("maxResults", "1_0", "PAY-7"),
        # the width is per PARAMETER: `maxResults` is a Java int, `startAt` a Java long, and each
        # is refused just past its own boundary and answered 200 just inside it (below)
        ("maxResults", "2147483648", "PAY-7"),
        ("maxResults", "-2147483649", "PAY-7"),
        ("maxResults", "9223372036854775807", "PAY-7"),
        ("startAt", "9223372036854775808", "PAY-7"),
        ("startAt", "-9223372036854775809", "PAY-7"),
        # escapes in the key ``errors.atlassian.echoed_spelling`` keeps as sent
        ("maxResults", "abc", "A%20B"),
        ("maxResults", "abc", "A%3FB"),
        ("maxResults", "abc", "A%23B"),
    ],
)
def test_jira_refuses_an_integer_parameter_it_cannot_convert(paged, param, value, key, slash):
    client, h = paged
    r = client.get(f"/atlassian/rest/api/3/issue/{key}/comment{slash}?{param}={value}", headers=h)
    assert r.status_code == 400, r.text
    assert r.headers["content-type"] == "application/problem+json;charset=UTF-8"
    assert r.json() == {
        "type": "about:blank",
        "title": "Bad Request",
        "status": 400,
        "detail": f"Failed to convert '{param}' with value: '{value}'",
        "instance": f"/rest/api/3/issue/{key}/comment{slash}",
    }


@pytest.mark.parametrize(
    "query,want",
    [
        # an EMPTY value is not a conversion failure on either product -- it reads as absent
        ("maxResults=", (0, 100)),
        # a leading `+` and surrounding whitespace are both accepted, which is narrower than
        # "any decimal integer" and is what Python's own `int()` accepts
        ("maxResults=%2B3", (0, 3)),
        ("maxResults=%203%20", (0, 3)),
        # internal whitespace is REMOVED, not just trimmed -- `3 4` is thirty-four
        ("maxResults=3%204", (0, 34)),
        # Unicode digits, not ASCII's: U+0663 is ARABIC-INDIC DIGIT THREE
        ("maxResults=%D9%A3", (0, 3)),
        # whitespace and nothing else reads as absent HERE -- Confluence refuses the same value,
        # see test_confluence_refuses_a_whitespace_only_value_where_jira_reads_it_as_absent
        ("maxResults=%20", (0, 100)),
        ("maxResults=%09", (0, 100)),
        # just inside each parameter's own width
        ("maxResults=2147483647", (0, 100)),
        ("startAt=9223372036854775807", (9223372036854775807, 100)),
        ("startAt=-9223372036854775808", (0, 100)),
    ],
)
def test_jira_takes_the_integer_spellings_the_real_api_takes(paged, query, want):
    client, h = paged
    r = client.get(f"/atlassian/rest/api/3/issue/PAY-7/comment?{query}", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["startAt"], d["maxResults"]) == want


@pytest.mark.parametrize(
    "query,want",
    [
        # an integer takes the FIRST value and ignores the rest -- not the last, which is what
        # Starlette's `QueryParams.get` returns
        ("startAt=3&startAt=5", (3, 100)),
        ("startAt=5&startAt=abc", (5, 100)),
        ("maxResults=1&maxResults=2", (0, 1)),
        ("startAt=&startAt=5", (0, 100)),
    ],
)
def test_jira_reads_the_first_of_a_repeated_integer_parameter(paged, query, want):
    client, h = paged
    r = client.get(f"/atlassian/rest/api/3/issue/PAY-7/comment?{query}", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["startAt"], d["maxResults"]) == want


def test_jira_reports_the_whole_array_when_the_first_repeated_value_will_not_convert(paged):
    """Measured: `?startAt=abc&startAt=5` is refused and the value real names is the ARRAY, as a
    Java `toString` — `'[Ljava.lang.String;@5edeadf5'`. The trailing identity hash differs per
    request, so the shape is reproduced and the hash is not a promise."""
    client, h = paged
    r = client.get("/atlassian/rest/api/3/issue/PAY-7/comment?startAt=abc&startAt=5", headers=h)
    assert r.status_code == 400, r.text
    assert re.fullmatch(
        r"Failed to convert 'startAt' with value: '\[Ljava\.lang\.String;@[0-9a-f]+'",
        r.json()["detail"],
    ), r.json()["detail"]


@pytest.mark.parametrize(
    "query", ["orderBy=bogus&orderBy=created", "orderBy=created&orderBy=bogus"]
)
def test_jira_comma_joins_a_repeated_string_parameter_before_validating_it(paged, query):
    """Measured: a repeated STRING parameter is joined with a comma and validated as one string, so
    `bogus,created` is refused in BOTH orders — where reading the last value alone answers 200 to
    whichever ordering puts the valid spelling second."""
    client, h = paged
    r = client.get(f"/atlassian/rest/api/3/issue/PAY-7/comment?{query}", headers=h)
    assert r.status_code == 400, r.text
    assert "bogus" in r.json()["errorMessages"][0]


@pytest.mark.parametrize("slash", ["", "/"])
@pytest.mark.parametrize(
    "param,value",
    [("maxResults", "abc"), ("fieldsByKeys", "x"), ("failFast", "x"), ("reconcileIssues", "a")],
)
def test_jira_search_refuses_a_parameter_it_cannot_convert(client, admin_h, slash, param, value):
    r = client.get(f"/atlassian/rest/api/3/search/jql{slash}?{param}={value}", headers=admin_h)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == f"Failed to convert '{param}' with value: '{value}'"
    assert r.json()["instance"] == f"/rest/api/3/search/jql{slash}"


_ANY_PROJECT = "jql=project%20is%20not%20EMPTY"
_BOUNDED = f"{_ANY_PROJECT}&maxResults=1"
_RECONCILE_TOO_MANY = {
    "errorMessages": ["The max number of issues to reconcile is 50."],
    "errors": {},
}


def _ids(n: int) -> str:
    """``n`` distinct `reconcileIssues` values, as a query string."""
    return "&".join(f"reconcileIssues={10000 + i}" for i in range(n))


@pytest.mark.parametrize(
    "query,named",
    [
        # fieldsByKeys: the first value, Java's trim off it, matched in any case
        (f"{_BOUNDED}&fieldsByKeys=true", None),
        (f"{_BOUNDED}&fieldsByKeys=TRUE", None),
        (f"{_BOUNDED}&fieldsByKeys=False", None),
        (f"{_BOUNDED}&fieldsByKeys=yes", None),
        (f"{_BOUNDED}&fieldsByKeys=ON", None),
        (f"{_BOUNDED}&fieldsByKeys=no", None),
        (f"{_BOUNDED}&fieldsByKeys=off", None),
        (f"{_BOUNDED}&fieldsByKeys=1", None),
        (f"{_BOUNDED}&fieldsByKeys=0", None),
        (f"{_BOUNDED}&fieldsByKeys=", None),
        (f"{_BOUNDED}&fieldsByKeys=%20true", None),
        (f"{_BOUNDED}&fieldsByKeys=true%20", None),
        (f"{_BOUNDED}&fieldsByKeys=%01true", None),
        (f"{_BOUNDED}&fieldsByKeys=true%1F", None),
        (f"{_BOUNDED}&fieldsByKeys=ye%C5%BF", None),
        (f"{_BOUNDED}&fieldsByKeys=yes&fieldsByKeys=x", None),
        (f"{_BOUNDED}&fieldsByKeys=yes&fieldsByKeys=%20", None),
        (f"{_BOUNDED}&fieldsByKeys=x", ("fieldsByKeys", "x")),
        (f"{_BOUNDED}&fieldsByKeys=null", ("fieldsByKeys", "null")),
        (f"{_BOUNDED}&fieldsByKeys=2", ("fieldsByKeys", "2")),
        (f"{_BOUNDED}&fieldsByKeys=y", ("fieldsByKeys", "y")),
        (f"{_BOUNDED}&fieldsByKeys=n", ("fieldsByKeys", "n")),
        (f"{_BOUNDED}&fieldsByKeys=%20", ("fieldsByKeys", " ")),
        (f"{_BOUNDED}&fieldsByKeys=%01", ("fieldsByKeys", "\x01")),
        (f"{_BOUNDED}&fieldsByKeys=tr%20ue", ("fieldsByKeys", "tr ue")),
        (f"{_BOUNDED}&fieldsByKeys=%C2%A0true", ("fieldsByKeys", "\xa0true")),
        (f"{_BOUNDED}&fieldsByKeys=%C2%85true", ("fieldsByKeys", "\x85true")),
        (f"{_BOUNDED}&fieldsByKeys=%E2%80%80true", ("fieldsByKeys", "\u2000true")),
        (f"{_BOUNDED}&fieldsByKeys=o%EF%AC%80", ("fieldsByKeys", "o\ufb00")),
        (f"{_BOUNDED}&fieldsByKeys=x&fieldsByKeys=yes", ("fieldsByKeys", None)),
        (f"{_BOUNDED}&fieldsByKeys=&fieldsByKeys=yes", ("fieldsByKeys", None)),
        # failFast: read by the same rule
        (f"{_BOUNDED}&failFast=TRUE", None),
        (f"{_BOUNDED}&failFast=", None),
        (f"{_BOUNDED}&failFast=%00true", None),
        (f"{_BOUNDED}&failFast=ye%C5%BF", None),
        (f"{_BOUNDED}&failFast=yes&failFast=x", None),
        (f"{_BOUNDED}&failFast=x", ("failFast", "x")),
        (f"{_BOUNDED}&failFast=null", ("failFast", "null")),
        (f"{_BOUNDED}&failFast=2", ("failFast", "2")),
        (f"{_BOUNDED}&failFast=%20", ("failFast", " ")),
        (f"{_BOUNDED}&failFast=%C2%A0true", ("failFast", "\xa0true")),
        (f"{_BOUNDED}&failFast=o%EF%AC%80", ("failFast", "o\ufb00")),
        (f"{_BOUNDED}&failFast=x&failFast=yes", ("failFast", None)),
        (f"{_BOUNDED}&failFast=&failFast=yes", ("failFast", None)),
        # reconcileIssues: every value, the blank ones passed over
        (f"{_BOUNDED}&reconcileIssues=1", None),
        (f"{_BOUNDED}&reconcileIssues=-1", None),
        (f"{_BOUNDED}&reconcileIssues=%2B1", None),
        (f"{_BOUNDED}&reconcileIssues=%201", None),
        (f"{_BOUNDED}&reconcileIssues=1%20", None),
        (f"{_BOUNDED}&reconcileIssues=1%202", None),
        (f"{_BOUNDED}&reconcileIssues=%D9%A1", None),
        (f"{_BOUNDED}&reconcileIssues=9223372036854775807", None),
        (f"{_BOUNDED}&reconcileIssues=-9223372036854775808", None),
        (f"{_BOUNDED}&reconcileIssues=", None),
        (f"{_BOUNDED}&reconcileIssues=%20", None),
        (f"{_BOUNDED}&reconcileIssues=1&reconcileIssues=2", None),
        (f"{_BOUNDED}&reconcileIssues=&reconcileIssues=1", None),
        (f"{_BOUNDED}&reconcileIssues=%20&reconcileIssues=1", None),
        (f"{_BOUNDED}&reconcileIssues=a", ("reconcileIssues", "a")),
        (f"{_BOUNDED}&reconcileIssues=1.5", ("reconcileIssues", "1.5")),
        (f"{_BOUNDED}&reconcileIssues=1%2C2", ("reconcileIssues", "1,2")),
        (f"{_BOUNDED}&reconcileIssues=1_0", ("reconcileIssues", "1_0")),
        (f"{_BOUNDED}&reconcileIssues=%C2%A01", ("reconcileIssues", "\xa01")),
        (f"{_BOUNDED}&reconcileIssues=%011", ("reconcileIssues", "\x011")),
        (
            f"{_BOUNDED}&reconcileIssues=9223372036854775808",
            ("reconcileIssues", "9223372036854775808"),
        ),
        (
            f"{_BOUNDED}&reconcileIssues=-9223372036854775809",
            ("reconcileIssues", "-9223372036854775809"),
        ),
        (f"{_BOUNDED}&reconcileIssues=1&reconcileIssues=a", ("reconcileIssues", None)),
        (f"{_BOUNDED}&reconcileIssues=a&reconcileIssues=1", ("reconcileIssues", None)),
        (
            f"{_BOUNDED}&reconcileIssues=1&reconcileIssues=9223372036854775808",
            ("reconcileIssues", None),
        ),
        # two refusals at once: the one real names
        (f"{_ANY_PROJECT}&maxResults=abc&fieldsByKeys=x", ("maxResults", "abc")),
        (f"{_ANY_PROJECT}&fieldsByKeys=x&maxResults=abc", ("maxResults", "abc")),
        (f"{_ANY_PROJECT}&reconcileIssues=a&maxResults=abc", ("maxResults", "abc")),
        (f"{_BOUNDED}&reconcileIssues=a&fieldsByKeys=x", ("fieldsByKeys", "x")),
        (f"{_ANY_PROJECT}&fieldsByKeys=x&nextPageToken=bogus", ("fieldsByKeys", "x")),
        (f"{_ANY_PROJECT}&reconcileIssues=a&maxResults=0", ("reconcileIssues", "a")),
        (f"{_ANY_PROJECT}&maxResults=abc&failFast=x", ("maxResults", "abc")),
        (f"{_ANY_PROJECT}&failFast=x&maxResults=abc", ("maxResults", "abc")),
        (f"{_BOUNDED}&failFast=x&fieldsByKeys=x", ("fieldsByKeys", "x")),
        (f"{_BOUNDED}&failFast=x&reconcileIssues=a", ("failFast", "x")),
        (f"{_BOUNDED}&reconcileIssues=a&failFast=x", ("failFast", "x")),
        (f"{_ANY_PROJECT}&failFast=x&nextPageToken=bogus", ("failFast", "x")),
        (f"{_ANY_PROJECT}&failFast=x&maxResults=0", ("failFast", "x")),
        ("failFast=x", ("failFast", "x")),
        # the cap: more than 50 values, a blank one counted, after the conversions and the page
        # token and before the range and the unbounded JQL
        (f"{_BOUNDED}&{_ids(50)}", None),
        (f"{_BOUNDED}&{_ids(49)}&reconcileIssues=", None),
        (f"{_BOUNDED}&{_ids(51)}", _RECONCILE_TOO_MANY),
        (f"{_BOUNDED}&" + "&".join(["reconcileIssues=1"] * 51), _RECONCILE_TOO_MANY),
        (f"{_BOUNDED}&{_ids(50)}&reconcileIssues=", _RECONCILE_TOO_MANY),
        (f"{_BOUNDED}&{_ids(50)}&reconcileIssues=%20", _RECONCILE_TOO_MANY),
        (f"{_BOUNDED}&{_ids(50)}&reconcileIssues=a", ("reconcileIssues", None)),
        (f"{_BOUNDED}&fieldsByKeys=x&{_ids(51)}", ("fieldsByKeys", "x")),
        (f"{_BOUNDED}&failFast=x&{_ids(51)}", ("failFast", "x")),
        (f"{_BOUNDED}&failFast=yes&{_ids(51)}", _RECONCILE_TOO_MANY),
        (f"{_ANY_PROJECT}&maxResults=abc&{_ids(51)}", ("maxResults", "abc")),
        (f"{_ANY_PROJECT}&nextPageToken=bogus&{_ids(51)}", errors_atlassian.bad_page_token().body),
        (f"{_ANY_PROJECT}&maxResults=0&{_ids(51)}", _RECONCILE_TOO_MANY),
        (_ids(51), _RECONCILE_TOO_MANY),
    ],
)
def test_jira_search_get_reads_fields_by_keys_fail_fast_and_reconcile_issues(
    client, admin_h, query, named
):
    """Real's answers to the GET form, for the rules `_bool_param` and `_jira_long_list_param`
    describe and the cap on `reconcileIssues`: ``named`` is the parameter a 400 names and the value
    it names as sent, or ``None`` where the parameter repeats and the 400 names the array (see
    `test_jira_reports_the_whole_array_when_the_first_repeated_value_will_not_convert`), or the
    whole body of a 400 that names no parameter. A row without one is served."""
    r = client.get(f"/atlassian/rest/api/3/search/jql?{query}", headers=admin_h)
    if named is None:
        assert r.status_code == 200, r.text
        return
    if isinstance(named, dict):
        assert (r.status_code, r.json()) == (400, named), r.text
        assert r.headers["content-type"] == "application/json;charset=UTF-8"
        return
    param, shown = named
    assert r.status_code == 400, r.text
    assert r.headers["content-type"] == "application/problem+json;charset=UTF-8"
    body = r.json()
    detail = body.pop("detail")
    if shown is None:
        assert re.fullmatch(
            rf"Failed to convert '{param}' with value: '\[Ljava\.lang\.String;@[0-9a-f]+'", detail
        ), detail
    else:
        assert detail == f"Failed to convert '{param}' with value: '{shown}'"
    assert body == {
        "type": "about:blank",
        "title": "Bad Request",
        "status": 400,
        "instance": "/rest/api/3/search/jql",
    }


@pytest.mark.parametrize("param", ["limit", "start"])
def test_confluence_refuses_an_integer_parameter_it_cannot_convert(client, admin_h, param):
    """Confluence's own envelope, not Jira's: two keys, a bare `application/json`, and the raw
    Java exception string real puts in `message`."""
    r = client.get(f"/atlassian/wiki/rest/api/content?{param}=abc", headers=admin_h)
    assert r.status_code == 400, r.text
    assert r.headers["content-type"] == "application/json"
    assert r.json() == {
        "statusCode": 400,
        "message": (
            "org.springframework.web.method.annotation.MethodArgumentTypeMismatchException: "
            "Failed to convert value of type 'java.lang.String' to required type 'int'; "
            'nested exception is java.lang.NumberFormatException: For input string: "abc"'
        ),
    }


def test_jira_json_carries_the_charset_real_sends_and_confluence_does_not(client, admin_h):
    """Measured on a live Atlassian Cloud site, 2026-09-15 and 2026-09-18: Jira answers
    `application/json;charset=UTF-8` on its 200s, a 404 under either mount and a plain 400, and the
    bare `application/json` on the one 403 this server answers, the gateway's for a bearer it cannot
    read. Confluence answers the bare type on every JSON body measured, whose statuses and dates
    ``errors.atlassian.json_media_type`` lists. The RFC 7807 refusals keep
    `application/problem+json`, which the middleware never touches. The spelling is pinned here
    rather than read from the constant, so a reformat into GitHub's
    `application/json; charset=utf-8` is caught in the file a reader of the Jira rule opens."""
    jira, bare = "application/json;charset=UTF-8", "application/json"
    assert errors_atlassian.JIRA_JSON_MEDIA_TYPE == jira
    unreadable = {"Authorization": "Bearer usr-nope"}
    cells = [
        ("/atlassian/rest/api/3/serverInfo", admin_h, 200, jira),
        ("/atlassian/rest/api/2/serverInfo", {}, 200, jira),
        ("/atlassian/rest/api/3/project/search", admin_h, 200, jira),
        ("/atlassian/rest/api/3/issue/NOPE-999999", admin_h, 404, jira),
        ("/atlassian/rest/api/2/issue/NOPE-999999", admin_h, 404, jira),
        ("/atlassian/rest/api/3/issue/PAY-7/comment?orderBy=bogus", admin_h, 400, jira),
        (
            "/atlassian/rest/api/3/search/jql?maxResults=abc",
            admin_h,
            400,
            errors_atlassian.PROBLEM_JSON,
        ),
        ("/atlassian/rest/api/3/serverInfo", unreadable, 403, bare),
        ("/atlassian/rest/api/3/project/search", unreadable, 403, bare),
        ("/atlassian/wiki/rest/api/space", admin_h, 200, bare),
        ("/atlassian/wiki/rest/api/search?cql=type=page", admin_h, 200, bare),
        ("/atlassian/wiki/rest/api/space/NOPESUCHSPACE", admin_h, 404, bare),
        ("/atlassian/wiki/rest/api/content/999999999", admin_h, 404, bare),
        ("/atlassian/wiki/rest/api/space", {}, 403, bare),
    ]
    for path, headers, status, ctype in cells:
        r = client.get(path, headers=headers)
        assert (r.status_code, r.headers["content-type"]) == (status, ctype), path
    # the OpenAPI document still keys the JSON body as `application/json`, as real's own spec does:
    # the charset is on the wire, not in the contract `backlot diff` compares
    op = client.get("/openapi.json").json()["paths"]["/atlassian/rest/api/3/serverInfo"]["get"]
    assert list(op["responses"]["200"]["content"]) == ["application/json"]


def test_confluence_names_the_comma_join_when_a_repeated_value_will_not_convert(client, admin_h):
    """Where Jira renders the array as a Java `toString`, Confluence renders it as the comma-join
    and reports the array's own type."""
    r = client.get("/atlassian/wiki/rest/api/content?limit=abc&limit=2", headers=admin_h)
    assert r.status_code == 400, r.text
    assert "'java.lang.String[]'" in r.json()["message"]
    assert 'For input string: "abc,2"' in r.json()["message"]


def test_confluence_refuses_a_negative_limit_where_jira_clamps_one(client, admin_h):
    """The products disagree and both are measured: Jira answers 200 with `startAt: 0` for
    `?startAt=-5`, Confluence refuses. Unclamped, `-1` reaches SQLite, which reads a negative
    `LIMIT` as NO limit — so the answer was the whole collection, the opposite of the ask."""
    r = client.get("/atlassian/wiki/rest/api/content?limit=-1", headers=admin_h)
    assert r.status_code == 400, r.text
    assert r.json() == {
        "statusCode": 400,
        "message": "java.lang.IllegalArgumentException: limit cannot be less than zero",
    }


# The bounds and the envelope every paged Confluence listing answers, measured 2026-09-22 against a
# live Cloud site whose `content` listing holds 9 items, `space` 3, and one page 2 children.

_CAPS = [
    ("content", None, 1001, 1000),
    ("content", None, 2147483647, 1000),
    ("space", None, 1001, 1000),
    ("space", None, 5000, 1000),
    ("child/comment", "comment", 1001, 1000),
    ("label", "label", 1001, 200),
    ("child/page", "child", 1001, 1001),
    ("child/attachment", "attachment", 1001, 1001),
]


@pytest.mark.parametrize("route, kind, sent, want", _CAPS, ids=[f"{r[0]}-{r[2]}" for r in _CAPS])
def test_confluence_caps_limit_where_real_caps_it(client, admin_h, route, kind, sent, want):
    """Each cap :func:`backlot.routers.atlassian._confluence_page_params` records, on its own
    route. A value above the cap is answered with the cap, not refused."""
    if kind is None:
        path = f"/atlassian/wiki/rest/api/{route}"
    else:
        cid = client.get("/atlassian/wiki/rest/api/content?limit=1", headers=admin_h).json()[
            "results"
        ][0]["id"]
        path = f"/atlassian/wiki/rest/api/content/{cid}/{route}"
    r = client.get(f"{path}?limit={sent}", headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["limit"] == want


_DEFAULTS = [
    ("content", 25),
    ("space", 25),
    ("child/page", 25),
    ("child/comment", 25),
    ("child/attachment", 50),
    ("label", 200),
]


@pytest.mark.parametrize("route, want", _DEFAULTS)
def test_confluence_each_listing_answers_its_own_default_limit(client, admin_h, route, want):
    """Measured: the `limit` each listing answers when none is sent."""
    api = "/atlassian/wiki/rest/api"
    if route in ("content", "space"):
        path = f"{api}/{route}"
    else:
        cid = client.get(f"{api}/content?limit=1", headers=admin_h).json()["results"][0]["id"]
        path = f"{api}/content/{cid}/{route}"
    assert client.get(path, headers=admin_h).json()["limit"] == want


def test_confluence_content_refuses_a_start_above_the_bound_where_space_serves_one(client, admin_h):
    """Measured: `content?start=100001` is a 400 carrying the `data` object Confluence's service
    layer adds, `start=100000` a 200 with an empty page, and `space?start=100001` a 200 — the bound
    is `content`'s alone."""
    api = "/atlassian/wiki/rest/api"
    ok = client.get(f"{api}/content?start=100000&limit=1", headers=admin_h)
    assert ok.status_code == 200 and ok.json()["size"] == 0
    refused = client.get(f"{api}/content?start=100001&limit=1", headers=admin_h)
    assert refused.status_code == 400
    body = refused.json()
    assert sorted(body) == ["data", "message", "statusCode"]
    assert body["data"] == {"authorized": True, "valid": True, "errors": [], "successful": True}
    assert "Start of this size is no longer supported" in body["message"]
    assert client.get(f"{api}/space?start=100001", headers=admin_h).status_code == 200


_PAGED = ["content", "space", "child/page", "child/comment", "child/attachment", "label"]


@pytest.mark.parametrize("route", _PAGED)
def test_confluence_every_paged_listing_answers_base_context_and_self(client, admin_h, route):
    """Measured: all three ride every page, `context` is `/wiki`, and `self` is the request's URL
    with `limit`, `start` and the markers removed and every other parameter kept."""
    api = "/atlassian/wiki/rest/api"
    if route in ("content", "space"):
        path = f"{api}/{route}"
    else:
        cid = client.get(f"{api}/content?limit=1", headers=admin_h).json()["results"][0]["id"]
        path = f"{api}/content/{cid}/{route}"
    links = client.get(f"{path}?limit=1&start=0&bogus=x", headers=admin_h).json()["_links"]
    assert links["context"] == "/wiki"
    assert links["base"].endswith("/wiki")
    assert links["self"].endswith(path.replace("/atlassian", "") + "?bogus=x"), links["self"]


@pytest.mark.parametrize(
    "route, zero_refused",
    [
        ("content", None),
        ("child/page", None),
        ("child/comment", None),
        ("label", "java.lang.IllegalArgumentException: null"),
    ],
)
def test_confluence_listings_read_limit_and_start(client, admin_h, route, zero_refused):
    """Measured: each reads both, so `?limit=0` is an empty page whose `next` names the page it is
    on, or on `label` the 400 of :func:`backlot.errors.atlassian.zero_limit_not_allowed`; `?start=1`
    carries a `prev`; and a `start` past the collection answers a `prev` and no `next`."""
    api = "/atlassian/wiki/rest/api"
    path = f"{api}/content"
    if route != "content":
        holder = None
        for page in client.get(f"{api}/content?limit=100", headers=admin_h).json()["results"]:
            if client.get(f"{api}/content/{page['id']}/{route}", headers=admin_h).json()["size"]:
                holder = page["id"]
                break
        assert holder, f"the bundled corpus holds no {route} row to page"
        path = f"{api}/content/{holder}/{route}"
    zero = client.get(f"{path}?limit=0", headers=admin_h)
    if zero_refused:
        assert zero.status_code == 400
        assert zero.json() == {"statusCode": 400, "message": zero_refused}
    else:
        assert zero.status_code == 200, zero.text
        assert zero.json()["limit"] == 0 and zero.json()["size"] == 0
        assert zero.json()["_links"]["next"].endswith("next=true&limit=0&start=0")
    second = client.get(f"{path}?limit=1&start=1", headers=admin_h)
    assert second.status_code == 200, second.text
    assert second.json()["start"] == 1 and second.json()["limit"] == 1
    assert second.json()["_links"]["prev"].endswith("prev=true&limit=1&start=0")
    past = client.get(f"{path}?limit=1&start=99999", headers=admin_h).json()["_links"]
    assert "next" not in past and "prev" in past


# (the request after `/wiki/rest/api`, the page link it answers, that link, the `self` after
# `/wiki/rest/api`), with `{hb}` and `{po}` read from `_SPACE_KEYS`
_CARRIED_ROWS = [
    (
        "/content?limit=1&bogus=x",
        "next",
        "/rest/api/content?next=true&limit=1&start=1&bogus=x",
        "/content?bogus=x",
    ),
    (
        "/space?spaceKey={po}&start=1",
        "prev",
        "/rest/api/space?spaceKey={po}&prev=true&limit=1&start=0",
        "/space?spaceKey={po}",
    ),
    (
        "/space?expand=description&spaceKey={po},{hb}&limit=1",
        "next",
        "/rest/api/space?spaceKey={po},{hb}&next=true&expand=description&limit=1&start=1",
        "/space?spaceKey={po},{hb}&expand=description",
    ),
    (
        "/space?spaceKey={po}&spaceKey={hb}&expand=description&limit=1&start=1",
        "prev",
        "/rest/api/space?spaceKey={po}&spaceKey={hb}&expand=description&prev=true&limit=1&start=0",
        "/space?spaceKey={po}&spaceKey={hb}&expand=description",
    ),
    (
        "/content?expand=version,space&limit=1",
        "next",
        "/rest/api/content?next=true&expand=version,space&limit=1&start=1",
        "/content?expand=version,space",
    ),
    (
        "/content?expand=version%2cspace&limit=1",
        "next",
        "/rest/api/content?next=true&expand=version%2cspace&limit=1&start=1",
        "/content?expand=version%2cspace",
    ),
    (
        "/space?expand=version%2Cspace&limit=1&start=1",
        "prev",
        "/rest/api/space?expand=version%2Cspace&prev=true&limit=1&start=0",
        "/space?expand=version%2Cspace",
    ),
    (
        "/content?limit=1&bogus=a+b,%7E~;&flag&&=x",
        "next",
        "/rest/api/content?next=true&limit=1&start=1&bogus=a+b,%7E~;&flag=",
        "/content?bogus=a+b,%7E~;&flag=",
    ),
    (
        "/space?l%69mit=1",
        "next",
        "/rest/api/space?next=true&limit=1&start=1&l%69mit=1",
        "/space?l%69mit=1",
    ),
    (
        "/space?limit=1&next=true&start=1",
        "prev",
        "/rest/api/space?prev=true&limit=1&start=0",
        "/space",
    ),
    (
        "/space?limit=1&start=1&prev=x",
        "prev",
        "/rest/api/space?prev=true&limit=1&start=0",
        "/space",
    ),
    (
        "/content?%65xpand=version,space&limit=1",
        "next",
        "/rest/api/content?next=true&limit=1&start=1&%65xpand=version,space",
        "/content?%65xpand=version,space",
    ),
    (
        "/content?limit=1&bogus=a=b",
        "next",
        "/rest/api/content?next=true&limit=1&start=1&bogus=a=b",
        "/content?bogus=a=b",
    ),
]


@pytest.mark.parametrize("request_path, which, link, self_path", _CARRIED_ROWS)
def test_confluence_a_carried_parameter_is_spelt_and_placed_as_measured(
    client, admin_h, request_path, which, link, self_path
):
    """Each row's links as :func:`backlot.routers.atlassian._confluence_carried` spells and places
    their parameters."""
    api = "/atlassian/wiki/rest/api"
    links = client.get(api + request_path.format(**_SPACE_KEYS), headers=admin_h).json()["_links"]
    assert links[which] == link.format(**_SPACE_KEYS)
    assert links["self"] == "http://testserver/wiki/rest/api" + self_path.format(**_SPACE_KEYS)


def test_confluence_label_serves_one_of_two_labels_and_a_next(client, admin_h):
    """`label` is the one listing the bundled corpus holds two rows for, so it is where a page
    smaller than the collection can be read: one row, and a `next` to the second."""
    api = "/atlassian/wiki/rest/api"
    holder = next(
        page["id"]
        for page in client.get(f"{api}/content?limit=100", headers=admin_h).json()["results"]
        if client.get(f"{api}/content/{page['id']}/label", headers=admin_h).json()["size"] > 1
    )
    page = client.get(f"{api}/content/{holder}/label?limit=1&start=0", headers=admin_h).json()
    assert page["size"] == 1 and page["limit"] == 1
    assert page["_links"]["next"].endswith("next=true&limit=1&start=1")


@pytest.mark.parametrize(
    "query, refused",
    [
        *[(f"prefix={p}", None) for p in ("global", "my", "team", "system", "", "%20global")],
        *[(f"prefix={p}", None) for p in ("%09global", "global,my", "my,team,system,global")],
        ("prefix=bogus", ["bogus"]),
        ("prefix=GLOBAL", ["GLOBAL"]),
        ("prefix=%20bogus", ["bogus"]),
        ("prefix=%20", [""]),
        ("prefix=global,bogus", ["global", "bogus"]),
        ("prefix=global%20,%20bogus", ["global", "bogus"]),
        ("prefix=global,", ["global", ""]),
        ("prefix=global&prefix=bogus", ["global", "bogus"]),
        ("prefix=bogus&prefix=global", ["bogus", "global"]),
        ("prefix=&prefix=bogus", ["", "bogus"]),
        # where the refusal sits among the others, as the comment in `confluence_labels` records
        ("prefix=bogus&limit=0", ["bogus"]),
        ("prefix=bogus&limit=-1&start=-1", ["bogus"]),
        ("prefix=bogus&limit=abc", "limit=abc"),
        ("prefix=bogus&start=abc", "start=abc"),
    ],
)
def test_confluence_label_reads_its_prefix_as_real_does(client, admin_h, query, refused):
    """Real's answers to `content/{id}/label?prefix=…`, the rule `_refuse_a_label_prefix`
    records: ``refused`` is the elements the 400 names, ``None`` a 200, or a query whose answer this
    one is."""
    api = "/atlassian/wiki/rest/api"
    cid = client.get(f"{api}/content?limit=1", headers=admin_h).json()["results"][0]["id"]
    r = client.get(f"{api}/content/{cid}/label?{query}", headers=admin_h)
    if refused is None:
        assert r.status_code == 200, r.text
    elif isinstance(refused, str):
        twin = client.get(f"{api}/content/{cid}/label?{refused}", headers=admin_h)
        assert (r.status_code, r.json()) == (400, twin.json())
    else:
        want = errors_atlassian.label_prefixes_not_converted(refused)
        assert (r.status_code, r.json()) == (400, want.body)


# (query, start, limit, prev) for a page holding no attachment, which every corpus page is
_ATTACHMENT_PAGES = [
    ("", 0, 50, None),
    ("limit=0", 0, 0, None),
    ("start=1", 1, 50, "prev=true&limit=1&start=0"),
    ("limit=0&start=1", 1, 0, "prev=true&limit=0&start=1"),
    ("start=100001", 100001, 50, "prev=true&limit=50&start=99951"),
]


@pytest.mark.parametrize("query, start, limit, prev", _ATTACHMENT_PAGES)
def test_confluence_child_attachment_is_the_empty_page_from_any_start(
    client, admin_h, query, start, limit, prev
):
    """Measured: no rows and no `next` at any `start`, a `prev` from any `start` past 0, and no
    bound on `start` of the kind `content` has."""
    api = "/atlassian/wiki/rest/api"
    cid = client.get(f"{api}/content?limit=1", headers=admin_h).json()["results"][0]["id"]
    r = client.get(f"{api}/content/{cid}/child/attachment?{query}", headers=admin_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["results"], body["size"], body["start"], body["limit"]) == ([], 0, start, limit)
    assert "next" not in body["_links"]
    if prev is None:
        assert "prev" not in body["_links"]
    else:
        assert body["_links"]["prev"].endswith(f"/child/attachment?{prev}")


@pytest.mark.parametrize("route", ["child/page", "child/comment", "child/attachment", "label"])
def test_confluence_child_listings_refuse_a_limit_they_cannot_convert(client, admin_h, route):
    """Measured: each answers Spring's two-key 400, the same body `content` gives."""
    api = "/atlassian/wiki/rest/api"
    cid = client.get(f"{api}/content?limit=1", headers=admin_h).json()["results"][0]["id"]
    r = client.get(f"{api}/content/{cid}/{route}?limit=abc", headers=admin_h)
    assert r.status_code == 400
    assert sorted(r.json()) == ["message", "statusCode"]
    assert "MethodArgumentTypeMismatchException" in r.json()["message"]


@pytest.fixture(scope="module")
def searchable(tmp_path_factory):
    """Four Confluence pages one CQL term matches, so a page of two leaves rows behind it: the
    bundled corpus matches two at most, where a page served ahead of the last one needs three."""
    settings = tiny_corpus(
        tmp_path_factory.mktemp("searchable"),
        [
            {
                "source_type": "confluence",
                "doc_id": f"cf-rollback-{n}",
                "space": "handbook",
                "title": f"Rollback {n}",
                "content": "rollback steps",
                "author_email": "a@x.com",
                "visibility": "public",
            }
            for n in range(4)
        ],
    )
    with client_for(settings, reload=True) as client:
        tok = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))["admin_token"]
        yield client, {"Authorization": f"Bearer {tok}"}


_CQL = '/atlassian/wiki/rest/api/search?cql=text~"rollback"'


def _cursors(link: str) -> list[str]:
    return re.findall(r"cursor=([^&]*)", link)


def _named_row(token: str) -> str:
    """The content id a cursor names, read back out of its base64 payload."""
    payload = re.fullmatch(r"_t_(.+)_h_W10=", unquote(token)).group(1)
    return json.loads(base64.b64decode(payload))[0].strip()


def _token(
    row_id: str, *, real: bool = False, tab: bool = True, first: str = "{}", then: str = "[]"
) -> str:
    """A cursor naming ``row_id``, as this server spells one, or as real does, whose sort value goes
    on past the id; with ``tab=False``, the id alone, which names no row. ``first`` is the `_t_`
    part's text, `{}` standing for the list holding that value, and ``then`` the `_h_` part's."""
    value = ("\t" if tab else "") + row_id + (" mG3e:Saf>3LVt*JA@Ok1 cp" if real else "")
    t, h = (
        base64.b64encode(part.encode()).decode()
        for part in (first.format(json.dumps([value])), then)
    )
    return quote(f"_t_{t}_h_{h}", safe="")


_SCALE = "com.atlassian.confluence.api.service.exceptions.scale.SSStatusCodeException"
_REFUSED, _FAILED = (
    f"{_SCALE}: CQL was parsed but the search manager was unable to execute the search. Error "
    f"message: {_SCALE}: {failure}"
    for failure in (
        "There was an illegal request passed to XP-Search Aggregator API : HTTP/1.1 400 Bad Request",
        "There was an error returned from XP-Search Aggregator API: HTTP/1.1 500 Internal Server "
        "Error",
    )
)

_NEXT_PAST_INT = errors_atlassian.search_next_out_of_range().body

# The CQL search's page over `searchable`'s four matches, by the rules `_cql_position`,
# `_cql_cursor`, `_cql_search_after`, `errors_atlassian.search_next_out_of_range` and the comment on
# `cursor` in `confluence_cql_search` record: the query after `_CQL`, where `{cN}` is a cursor this
# server spells naming match N, `{real1}` real's spelling of the one naming match 1 and `{bare1}`
# match 1's id with no tab in front, and the other `{…1}` are the cursor naming match 1 with the
# change the test's `tokens` spells out; then the matches served, or `(status, message)` for a
# refusal (the whole body where it carries more, `None` for none); then the match `next`'s cursor
# names, or `None` for no `next`.
# fmt: off
_CQL_PAGE_ROWS = [
    ("&limit=2", [0, 1], 1),
    ("&limit=0&start=3", [], 0),
    ("&limit=1&start=3", [0], 0),
    ("&limit=2&start=100001", [0, 1], 1),
    ("&limit=2&cursor={c1}", [2, 3], None),
    ("&limit=2&start=100001&cursor={c1}", [2, 3], None),
    ("&limit=2&cursor={real1}", [2, 3], None),
    ("&limit=2&cursor={bare1}", [0, 1], 1),
    ("&limit=1&cursor={c0}", [1], 1),
    ("&limit=0&cursor={c1}", [], 2),
    ("&limit=0&cursor={c2}", [], None),
    ("&limit=2&cursor={c3}", [], None),
    ("&limit=2&cursor=", [0, 1], 1),
    ("&limit=2&cursor", [0, 1], 1),
    ("&limit=2&cursor=&cursor={c1}", [0, 1], 1),
    ("&limit=2&cursor={c1}&cursor=", [2, 3], None),
    ("&limit=1&cursor={c1}&cursor={c2}", [2], 2),
    ("&limit=2&cursor=abc", (400, _REFUSED), None),
    ("&limit=2&cursor=_t_WyJcdDEiXQ%3D%3D", (400, _REFUSED), None),
    ("&limit=2&cursor=WyJcdDEiXQ%3D%3D_h_W10%3D", (400, _REFUSED), None),
    ("&limit=2&cursor=_t_WyJcdDEiXQ%3D%3D_h_abc", (400, _REFUSED), None),
    ("&limit=2&cursor=_t_%21%21%21_h_W10%3D", (400, _REFUSED), None),
    ("&limit=2&cursor=_t_aGVsbG8%3D_h_W10%3D", (400, _REFUSED), None),
    ("&limit=2&cursor=_t_WyJcdDEiLCAiXHQyIl0%3D_h_W10%3D", (400, _REFUSED), None),
    ("&limit=2&cursor=_t_W10%3D_h_W10%3D", (500, _FAILED), None),
    ("&limit=3&cursor=_t_W3t9XQ%3D%3D_h_W10%3D", (500, _FAILED), None),
    ("&limit=3&cursor=_t_W1siXHQxIl1d_h_W10%3D", (500, _FAILED), None),
    ("&limit=3&cursor=_t_WzNd_h_W10%3D", [0, 1, 2], 2),
    ("&limit=3&cursor=_t_W3RydWVd_h_W10%3D", [0, 1, 2], 2),
    ("&limit=3&cursor=_t_W251bGxd_h_W10%3D", [], None),
    ("&limit=2&cursor=_t_WyJcdH5-fiJd_h_W10%3D", [0, 1], 1),
    ("&limit=2&cursor={lead1}", [2, 3], None),
    ("&limit=2&cursor={trail1}", [2, 3], None),
    ("&limit=2&cursor={pair1}", [2, 3], None),
    ("&limit=2&cursor={null1}", (400, _REFUSED), None),
    ("&limit=2&cursor={list1}", (400, _REFUSED), None),
    ("&limit=2&cursor={bare_null1}", (400, _REFUSED), None),
    ("&limit=2&cursor={inf1}", (400, _REFUSED), None),
    ("&limit=2&cursor={dot1}", (400, _REFUSED), None),
    ("&limit=2&cursor=_t_W05hTl0%3D_h_W10%3D", (400, _REFUSED), None),
    ("&limit=2&cursor=_t_Wy1JbmZpbml0eV0%3D_h_W10%3D", (400, _REFUSED), None),
    ("&limit=2&cursor=_t_bnVsbA%3D%3D_h_W10%3D", [0, 1], 1),
    ("&limit=0&cursor=_t_bnVsbA%3D%3D_h_W10%3D", [], 0),
    ("&limit=2&cursor=_t_bnVsbA%3D%3D_h_W251bGxd", (400, _REFUSED), None),
    ("&limit=2&cursor=_t_W10%3D_h_W251bGxd", (400, _REFUSED), None),
    ("&limit=2&cursor=_t_W10geA%3D%3D_h_W10%3D", (500, _FAILED), None),
    ("&limit=1&start=2147483646", [0], 0),
    ("&limit=2&start=2147483646", (400, _NEXT_PAST_INT), None),
    ("&limit=0&start=2147483646", [], 0),
    ("&limit=0&start=2147483647", (400, _NEXT_PAST_INT), None),
    ("&limit=4&start=2147483647", [0, 1, 2, 3], None),
    ("&limit=1&start=2147483647&cursor={c2}", [3], None),
    ("&limit=1&start=2147483647&cursor={c1}", (400, _NEXT_PAST_INT), None),
    ("&limit=-1&cursor=abc", (400, 'java.lang.IllegalArgumentException: limit cannot be less than zero'), None),
    ("&limit=abc&cursor=abc", (404, None), None),
]
# fmt: on


@pytest.mark.parametrize("query, answer, next_names", _CQL_PAGE_ROWS)
def test_confluence_cql_pages_by_the_cursor_it_is_sent(searchable, query, answer, next_names):
    client, h = searchable
    matches = client.get(f"{_CQL}&limit=4", headers=h).json()["results"]
    ids = [r["content"]["id"] for r in matches]
    tokens = {f"c{n}": _token(row) for n, row in enumerate(ids)} | {
        "real1": _token(ids[1], real=True),
        "bare1": _token(ids[1], tab=False),
        "lead1": _token(ids[1], first=" {}"),
        "trail1": _token(ids[1], first="{} x"),
        "pair1": _token(ids[1], then="[1,2]"),
        "null1": _token(ids[1], then="[null]"),
        "list1": _token(ids[1], then="[[]]"),
        "bare_null1": _token(ids[1], then="null"),
        "inf1": _token(ids[1], then="[Infinity]"),
    }
    # a `.` among the first part's base64 characters
    tokens["dot1"] = tokens["c1"][:7] + "." + tokens["c1"][7:]
    r = client.get(_CQL + query.format(**tokens), headers=h)
    if isinstance(answer, tuple):
        status, message = answer
        assert r.status_code == status, r.text
        if message is None:
            assert (r.content, r.headers.get("content-type")) == (b"", None)
            return
        assert r.headers["content-type"] == "application/json"
        if isinstance(message, dict):
            assert r.json() == message
        else:
            assert r.json() == {"statusCode": status, "message": message}
        return
    page = r.json()
    assert [row["content"]["id"] for row in page["results"]] == [ids[n] for n in answer]
    sent = re.search(r"start=(\d+)", query)
    assert (page["totalSize"], page["start"]) == (4, int(sent[1]) if sent else 0)
    nxt = page["_links"].get("next")
    assert (ids.index(_named_row(_cursors(nxt)[0])) if nxt else None) == next_names
    assert "cursor=" not in page["_links"]["self"] and len(_cursors(nxt or "")) == bool(nxt)


def test_confluence_cql_cursor_nested_deeper_than_the_json_reader_recurses_is_the_400():
    """A cursor part of lists nested over a million deep is refused with the 400 a part holding no
    JSON value gets (:func:`backlot.routers.atlassian._cql_token_part`). Called directly: a token
    that deep runs past the longest URL httpx sends."""
    from backlot.routers.atlassian import _cql_search_after

    # `[3]`, then `[` and `]` three at a time in base64
    token = "_t_WzNd_h_" + "W1tb" * 333_334 + "XV1d" * 333_334
    with pytest.raises(errors_atlassian.AtlassianError) as refused:
        _cql_search_after(token)
    assert refused.value.body == errors_atlassian.search_cursor_refused().body


def test_confluence_cql_cursor_finds_out_no_page_the_caller_cannot_see(tmp_path):
    """A cursor is positioned among the caller's own matches, so one naming a page a scoped token
    cannot see answers as no cursor does, where the admin is served the rows after it. The pages
    say the term three times, twice and once, so the hidden one ranks between the two seen."""
    page = {"source_type": "confluence", "space": "handbook", "author_email": "ava@acme.com"}
    seen_pages = {"author_groups": ["engineering"], "visibility": "public"}
    records = [
        {**page, **seen_pages, "doc_id": "cf-rollout-3", "title": "Rollout three",
         "content": "rollout rollout rollout steps"},
        {**page, **seen_pages, "doc_id": "cf-rollout-1", "title": "Rollout one",
         "content": "rollout steps"},
        {**page, "doc_id": "cf-rollout-pay", "space": "people-ops", "title": "Rollout pay",
         "content": "rollout rollout steps", "author_email": "hana@acme.com",
         "author_groups": ["people"], "visibility": "group"},
    ]  # fmt: skip
    settings = tiny_corpus(tmp_path, records)
    users = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))
    with client_for(settings, reload=True) as client:
        admin_h = {"Authorization": f"Bearer {users['admin_token']}"}
        ava = next(u["token"] for u in users["users"] if u["email"] == "ava@acme.com")
        ava_h = {"Authorization": f"Bearer {ava}"}
        q = '/atlassian/wiki/rest/api/search?cql=text~"rollout"&limit=50'

        def served(headers: dict, cursor: str = "") -> list:
            sent = f"&cursor={_token(cursor)}" if cursor else ""
            return [
                r["content"]["id"] for r in client.get(q + sent, headers=headers).json()["results"]
            ]

        every, seen = served(admin_h), served(ava_h)
        hidden = next(row for row in every if row not in seen)
        assert len(seen) == 2 and every.index(hidden) == 1
        assert served(ava_h, hidden) == seen
        assert served(admin_h, hidden) == every[2:]


def test_confluence_cql_links_carry_one_cursor_and_prev_the_one_sent(searchable):
    """Measured 2026-09-23 following `next` three hops at `limit=0`, `1` and `2`: `next` carries
    the new cursor alone, `self` none, and `prev` leads with the one the request sent, at `start=0`
    too. With a cursor sent, `prev`'s own `limit` is the request's rather than the rows skipped.
    At `limit=1` each hop moves the cursor one match on, and at `limit=0` too, where no page serves
    a row."""
    client, h = searchable
    ids = [r["content"]["id"] for r in client.get(f"{_CQL}&limit=4", headers=h).json()["results"]]
    for limit in (0, 1):
        page = client.get(f"{_CQL}&limit={limit}", headers=h).json()
        nxt = page["_links"]["next"]
        assert re.match(rf"/rest/api/search\?next=true&cursor=[^&]+&limit={limit}&start=", nxt)
        assert "cursor=" not in page["_links"]["self"]
        named, served = [], []
        for _hop in range(3):
            sent = _cursors(page["_links"]["next"])
            assert len(sent) == 1, page["_links"]["next"]
            named.append(_named_row(sent[0]))
            page = client.get("/atlassian/wiki" + page["_links"]["next"], headers=h).json()
            served += [r["content"]["id"] for r in page["results"]]
            links = page["_links"]
            assert "cursor=" not in links["self"]
            assert links["prev"].startswith(f"/rest/api/search?cursor={sent[0]}&prev=true&")
        assert (named, served) == (ids[:3], ids[1:4] if limit else [])
    token = _cursors(client.get(f"{_CQL}&limit=1", headers=h).json()["_links"]["next"])[0]
    with_cursor = client.get(f"{_CQL}&cursor={token}&limit=3&start=1", headers=h).json()
    assert with_cursor["_links"]["prev"].startswith(
        f"/rest/api/search?cursor={token}&prev=true&limit=3&start=0&"
    )
    without = client.get(f"{_CQL}&limit=3&start=1", headers=h).json()
    assert without["_links"]["prev"].startswith("/rest/api/search?prev=true&limit=1&start=0&")


def test_confluence_reads_the_first_of_a_repeated_integer_parameter(client, admin_h):
    r = client.get("/atlassian/wiki/rest/api/content?limit=1&limit=25", headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["limit"] == 1


@pytest.mark.parametrize(
    "query,want_limit",
    [
        ("limit=", 25),
        ("limit=%2B3", 3),
        ("limit=%203%20", 3),
        ("limit=3%204", 34),
        ("limit=%D9%A3", 3),
        # Java's `int` ceiling converts, and `content` then answers its own cap for it.
        ("limit=2147483647", 1000),
    ],
)
def test_confluence_takes_the_integer_spellings_the_real_api_takes(
    client, admin_h, query, want_limit
):
    """The conversion rules are shared with Jira, and were pinned only on Jira: giving both
    `_confluence_page_params` reads `width=JAVA_LONG` left this file green."""
    r = client.get(f"/atlassian/wiki/rest/api/content?{query}", headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["limit"] == want_limit


@pytest.mark.parametrize(
    "value", ["1.5", "1_0", "2147483648", "-2147483649", "9223372036854775807"]
)
def test_confluence_refuses_the_integer_values_the_real_api_refuses(client, admin_h, value):
    """`limit` is a Java int on this route, where Jira's `startAt` is a long."""
    r = client.get(f"/atlassian/wiki/rest/api/content?limit={value}", headers=admin_h)
    assert r.status_code == 400, r.text


@pytest.mark.parametrize("path", ["/rest/api/3/issue/PAY-7/comment", "/wiki/rest/api/content"])
def test_atlassian_reads_javas_whitespace_set_not_pythons(client, admin_h, paged, path):
    """Measured: Java's `Character.isWhitespace` excludes the three non-breaking spaces, so a value
    holding one is a 400 on both products WHEREVER it sits, where U+2003, a newline and a tab are
    removed and the value converts.

    The spelling axis is not decoration. Python's `str.split()` calls all six whitespace, which read
    the non-breaking ones as thirty-four; and once they survive the cleaning, Python's own `int()`
    strips a LEADING or TRAILING one before converting (`int('\\xa03')` is 3), so only the interior
    spelling reached a refusal. Three characters by four placements catches both."""
    c, h = paged if path.startswith("/rest") else (client, admin_h)
    name = "maxResults" if path.startswith("/rest") else "limit"
    placements = ("3{s}4", "{s}3", "3{s}", "{s}3{s}")
    for nbsp in ("%C2%A0", "%E2%80%87", "%E2%80%AF"):
        for placement in placements:
            value = placement.format(s=nbsp)
            r = c.get(f"/atlassian{path}?{name}={value}", headers=h)
            assert r.status_code == 400, f"{value}: {r.text}"
    for space in ("%E2%80%83", "%0A", "%09"):
        for placement in placements:
            value = placement.format(s=space)
            ok = c.get(f"/atlassian{path}?{name}={value}", headers=h)
            assert ok.status_code == 200, f"{value}: {ok.text}"


def test_confluence_refuses_an_empty_first_value_only_when_the_parameter_repeats(client, admin_h):
    """Measured: `?limit=&limit=5` is a 400 naming `",5"` — the array with an empty element in
    front — where a lone `?limit=` is the default. Jira reads `?startAt=&startAt=5` as the default,
    so this is Confluence's alone."""
    r = client.get("/atlassian/wiki/rest/api/content?limit=&limit=5", headers=admin_h)
    assert r.status_code == 400, r.text
    assert 'For input string: ",5"' in r.json()["message"]


@pytest.mark.parametrize(
    "query,want",
    [
        # cleaning is per value and the join comes after -- stripping the join gives "abc ,2"
        ("limit=%20abc%20&limit=2", '"abc,2"'),
        ("limit=abc&limit=%202%20", '"abc,2"'),
        ("limit=%20&limit=2", '",2"'),
        # and a single value is whitespace REMOVAL, not a trim
        ("limit=a%20b", '"ab"'),
    ],
)
def test_confluence_names_each_value_cleaned_before_joining_them(client, admin_h, query, want):
    r = client.get(f"/atlassian/wiki/rest/api/content?{query}", headers=admin_h)
    assert r.status_code == 400, r.text
    assert f"For input string: {want}" in r.json()["message"]


@pytest.mark.parametrize(
    "query,name",
    [
        ("maxResults=abc", "maxResults"),
        ("startAt=abc", "startAt"),
        ("maxResults=abc&orderBy=bogus", "maxResults"),
        ("orderBy=bogus&maxResults=abc", "maxResults"),
        # `startAt` is named in either URL order: the handler signature's order, not the URL's
        ("startAt=abc&maxResults=xyz", "startAt"),
        ("maxResults=xyz&startAt=abc", "startAt"),
    ],
)
def test_jira_refuses_an_unconvertible_parameter_before_resolving_the_issue(paged, query, name):
    """Measured 2026-09-15: the binder outranks both the 404 and the `orderBy` check.
    `?maxResults=abc` on a key that does not exist is the conversion 400 where the same key alone
    is 404, and it wins over a bad `orderBy` in either query order."""
    client, h = paged
    for key in ("PAY-7", "NOPE-99999"):
        r = client.get(f"/atlassian/rest/api/3/issue/{key}/comment?{query}", headers=h)
        assert r.status_code == 400, f"{key}: {r.text}"
        assert (
            r.json()["detail"]
            == f"Failed to convert '{name}' with value: '{query.split(f'{name}=')[1].split('&')[0]}'"
        )
    # ... where the same key with no parameter at all is still the 404
    assert (
        client.get("/atlassian/rest/api/3/issue/NOPE-99999/comment", headers=h).status_code == 404
    )


# The CQL search's `limit` and `start`, each request as real answered it (`_cql_page_param`): the
# query, then the status and the answer — on a 200 the `limit` and `start` echoed, on a 400 the
# message, on a 404 nothing, since that one has no body and no `content-type`.
# fmt: off
_CQL_INT_ROWS = [
    ("cql=type%3Dpage&limit=abc", 404, None),
    ("cql=type%3Dpage&limit=1.5", 404, None),
    ("cql=type%3Dpage&limit=1_0", 404, None),
    ("cql=type%3Dpage&limit=2147483648", 404, None),
    ("cql=type%3Dpage&limit=-2147483649", 404, None),
    ("cql=type%3Dpage&limit=0x10", 404, None),
    ("cql=type%3Dpage&limit=1e3", 404, None),
    ("cql=type%3Dpage&limit=%205", 404, None),
    ("cql=type%3Dpage&limit=5%20", 404, None),
    ("cql=type%3Dpage&limit=%095%09", 404, None),
    ("cql=type%3Dpage&limit=5%0A", 404, None),
    ("cql=type%3Dpage&limit=%2B", 404, None),
    ("cql=type%3Dpage&limit=-", 404, None),
    ("cql=type%3Dpage&limit=%E2%81%B5", 404, None),
    ("cql=type%3Dpage&limit=%EF%BC%8B5", 404, None),
    ("cql=type%3Dpage&limit=%C2%A0", 404, None),
    ("cql=type%3Dpage&limit=%F0%9D%9F%93", 404, None),
    ("cql=type%3Dpage&limit=%F0%91%81%AB", 404, None),
    ("cql=type%3Dpage&start=%F0%9D%9F%93&limit=1", 404, None),
    ("cql=type%3Dpage&start=abc&limit=1", 404, None),
    ("cql=type%3Dpage&start=1_0&limit=1", 404, None),
    ("cql=type%3Dpage&start=2147483648&limit=1", 404, None),
    ("cql=type%3Dpage&start=%201&limit=1", 404, None),
    ("cql=type%3Dpage&start=%C2%A0&limit=1", 404, None),
    ("cql=type%3Dpage&start=%2B&limit=1", 404, None),
    ("cql=type%3Dpage&limit=abc&limit=5", 404, None),
    ("cql=type%3Dpage&limit=abc&start=-1", 404, None),
    ("cql=type%3Dpage&limit=-1&start=abc", 404, None),
    ("limit=abc", 404, None),
    ("cql=space%3DNOPE&limit=abc", 404, None),
    ("cql=type%3Dpage&limit=2147483647", 200, (2147483647, 0)),
    ("cql=type%3Dpage&limit=%2B5", 200, (5, 0)),
    ("cql=type%3Dpage&limit=05", 200, (5, 0)),
    ("cql=type%3Dpage&limit=0002", 200, (2, 0)),
    ("cql=type%3Dpage&limit=-0", 200, (0, 0)),
    ("cql=type%3Dpage&limit=0", 200, (0, 0)),
    ("cql=type%3Dpage&limit=%20", 200, (25, 0)),
    ("cql=type%3Dpage&limit=", 200, (25, 0)),
    ("cql=type%3Dpage&limit=%09", 200, (25, 0)),
    ("cql=type%3Dpage&limit=%0A", 200, (25, 0)),
    ("cql=type%3Dpage&limit=%0D%0A", 200, (25, 0)),
    ("cql=type%3Dpage&limit=%01", 200, (25, 0)),
    ("cql=type%3Dpage&limit=%1F", 200, (25, 0)),
    ("cql=type%3Dpage&limit=%EF%BC%95", 200, (5, 0)),
    ("cql=type%3Dpage&limit=%D9%A5", 200, (5, 0)),
    ("cql=type%3Dpage&limit=%D9%A1%D9%A2", 200, (12, 0)),
    ("cql=type%3Dpage&limit=1%D9%A2", 200, (12, 0)),
    ("cql=type%3Dpage&limit=%E0%A5%A8", 200, (2, 0)),
    ("cql=type%3Dpage&start=%2B1&limit=1", 200, (1, 1)),
    ("cql=type%3Dpage&start=&limit=1", 200, (1, 0)),
    ("cql=type%3Dpage&start=%20&limit=1", 200, (1, 0)),
    ("cql=type%3Dpage&start=-0&limit=1", 200, (1, 0)),
    ("cql=type%3Dpage&start=%EF%BC%91&limit=1", 200, (1, 1)),
    ("cql=type%3Dpage&limit=5&limit=abc", 200, (5, 0)),
    ("cql=type%3Dpage&limit=&limit=5", 200, (25, 0)),
    ("cql=type%3Dpage&limit=5&limit=2", 200, (5, 0)),
    ("cql=type%3Dpage&start=1&start=2&limit=1", 200, (1, 1)),
    ("cql=type%3Dpage&limit=-1", 400, 'java.lang.IllegalArgumentException: limit cannot be less than zero'),
    ("cql=type%3Dpage&start=-1&limit=1", 400, 'java.lang.IllegalArgumentException: start cannot be less than zero'),
    ("cql=type%3Dpage&limit=-2147483648", 400, 'java.lang.IllegalArgumentException: limit cannot be less than zero'),
    ("cql=type%3Dpage&start=-2147483648&limit=1", 400, 'java.lang.IllegalArgumentException: start cannot be less than zero'),
]
# fmt: on


@pytest.mark.parametrize("query, status, answer", _CQL_INT_ROWS)
def test_confluence_cql_search_reads_limit_and_start_as_jaxrs_does(
    client, admin_h, query, status, answer
):
    r = client.get(f"/atlassian/wiki/rest/api/search?{query}", headers=admin_h)
    assert r.status_code == status, r.text
    if status == 404:
        assert (r.content, r.headers.get("content-type"), r.headers["content-length"]) == (
            b"",
            None,
            "0",
        )
    elif status == 400:
        assert r.json() == {"statusCode": 400, "message": answer}
    else:
        assert (r.json()["limit"], r.json()["start"]) == answer


def test_confluence_refuses_a_whitespace_only_value_where_jira_reads_it_as_absent(client, admin_h):
    """The one case the two products part on. Jira answers 200 with the default; Confluence trims
    to the empty string and fails to convert that, naming `""`. A genuinely empty `?limit=` is the
    default on both, so it is the whitespace rather than the emptiness that separates them."""
    r = client.get("/atlassian/wiki/rest/api/content?limit=%20", headers=admin_h)
    assert r.status_code == 400, r.text
    assert 'For input string: ""' in r.json()["message"]
    assert client.get("/atlassian/wiki/rest/api/content?limit=", headers=admin_h).status_code == 200


def test_confluence_names_the_trimmed_value_where_jira_names_the_raw_one(client, admin_h, paged):
    """Measured on `?…=%20abc%20`: Jira's `detail` keeps the spaces, Confluence's `message` does
    not. The same value, rendered two ways by the product that refused it."""
    conf = client.get("/atlassian/wiki/rest/api/content?limit=%20abc%20", headers=admin_h)
    assert conf.status_code == 400, conf.text
    assert 'For input string: "abc"' in conf.json()["message"]

    jira_client, h = paged
    jira = jira_client.get(
        "/atlassian/rest/api/3/issue/PAY-7/comment?maxResults=%20abc%20", headers=h
    )
    assert jira.status_code == 400, jira.text
    assert jira.json()["detail"] == "Failed to convert 'maxResults' with value: ' abc '"


@pytest.mark.parametrize(
    "route,query,want",
    [
        # conversion comes first for BOTH parameters, so a bad `start` outranks a negative `limit`
        ("content", "limit=-1&start=abc", 'For input string: "abc"'),
        ("content", "limit=abc&start=-1", 'For input string: "abc"'),
        # and among two negatives it is `start` that gets named
        ("content", "limit=-1&start=-1", "start cannot be less than zero"),
        # `content`'s `start` bound comes after conversion, and ahead of both the negative and an
        # unknown `spaceKey`
        ("content", "limit=abc&start=100001", "MethodArgumentTypeMismatchException"),
        ("content", "limit=-1&start=100001", "Start of this size is no longer supported"),
        ("content", "spaceKey=NOPE&start=100001", "Start of this size is no longer supported"),
        # `label`'s zero-`limit` refusal comes after conversion and after the negative
        ("label", "limit=0&start=abc", 'For input string: "abc"'),
        ("label", "limit=0&start=-1", "start cannot be less than zero"),
        # on an id naming nothing, the three `child/*` listings refuse both parameters ahead of the
        # 404, and `label` its conversion and its `prefix`
        ("content/999999999/child/attachment", "limit=abc", 'For input string: "abc"'),
        ("content/999999999/child/attachment", "limit=-1", "limit cannot be less than zero"),
        *[
            (f"content/999999999/{route}", query, want)
            for route in ("child/page", "child/comment")
            for query, want in (
                ("limit=abc", 'For input string: "abc"'),
                ("start=abc", 'For input string: "abc"'),
                ("limit=-1", "limit cannot be less than zero"),
                ("start=-1", "start cannot be less than zero"),
                ("limit=0&start=-1", "start cannot be less than zero"),
            )
        ],
        ("content/999999999/label", "limit=abc", 'For input string: "abc"'),
        ("content/999999999/label", "start=abc", 'For input string: "abc"'),
        ("content/999999999/label", "prefix=bogus", "Could not convert label prefixes :[bogus]"),
        ("content/999999999/label", "limit=-1&prefix=bogus", "label prefixes :[bogus]"),
        ("content/999999999/label", "prefix=bogus&limit=0", "label prefixes :[bogus]"),
        ("content/999999999/label", "prefix=bogus&limit=abc", 'For input string: "abc"'),
        ("content/att999999999/child/page", "limit=-1", "limit cannot be less than zero"),
        # an id `ContentId` cannot read comes ahead of each of them
        ("content/abc/child/page", "limit=abc", "for value [abc]"),
        ("content/abc/child/page", "limit=-1", "for value [abc]"),
        ("content/abc/child/comment", "limit=-1", "for value [abc]"),
        ("content/abc/child/attachment", "limit=-1", "for value [abc]"),
        ("content/abc/label", "prefix=bogus", "for value [abc]"),
        ("content/99999999999999999999/label", "limit=abc", "for value [99999999999999999999]"),
        ("content/attabc/label", "limit=abc", "for value [attabc]"),
    ],
)
def test_confluence_refuses_the_parameter_real_names_when_both_are_wrong(
    client, admin_h, route, query, want
):
    api = "/atlassian/wiki/rest/api"
    if route == "label":
        cid = client.get(f"{api}/content?limit=1", headers=admin_h).json()["results"][0]["id"]
        route = f"content/{cid}/label"
    r = client.get(f"{api}/{route}?{query}", headers=admin_h)
    assert r.status_code == 400, r.text
    assert want in r.json()["message"]


def test_jira_search_on_post_does_not_read_the_query_string_at_all(client, admin_h):
    """Measured: the query string is not read on POST, so a malformed parameter there cannot
    refuse the request — the GET rules do not reach this method."""
    for body in ({"jql": "project = payments", "maxResults": 1}, {"jql": "project = payments"}):
        r = client.post(
            "/atlassian/rest/api/3/search/jql?maxResults=abc&startAt=abc",
            headers=admin_h,
            json=body,
        )
        assert r.status_code == 200, r.text


# --- search/jql: one parameter, two places, one of them per method ------------------------


def _search_post(client, headers, query="", **body):
    return client.post(f"/atlassian/rest/api/3/search/jql{query}", headers=headers, json=body)


def test_jira_search_post_takes_its_parameters_from_the_body_and_get_from_the_query(
    client, admin_h
):
    """Measured 2026-09-15 across the four placements."""
    page1 = _search_post(client, admin_h, jql="project = payments", maxResults=1)
    assert page1.status_code == 200, page1.text
    token = page1.json()["nextPageToken"]
    first = page1.json()["issues"][0]["key"]

    # the body is read
    in_body = _search_post(
        client, admin_h, jql="project = payments", maxResults=1, nextPageToken=token
    )
    assert in_body.json()["issues"][0]["key"] != first

    # the query string is not, on this method
    in_query = _search_post(
        client, admin_h, query=f"?nextPageToken={token}", jql="project = payments", maxResults=1
    )
    assert in_query.json()["issues"][0]["key"] == first

    # ... and a body that is silent falls back to the default, not to the query string
    ignored = _search_post(client, admin_h, query="?maxResults=1", jql="project = payments")
    assert len(ignored.json()["issues"]) > 1

    # while the GET form reads exactly the parameters the POST form ignores
    got = client.get(
        f"/atlassian/rest/api/3/search/jql?jql=project+%3D+payments&maxResults=1&nextPageToken={token}",
        headers=admin_h,
    )
    assert got.status_code == 200, got.text
    assert got.json()["issues"][0]["key"] == in_body.json()["issues"][0]["key"]


@pytest.mark.parametrize("method", ["get", "post"])
def test_jira_search_refuses_no_jql_at_all(client, admin_h, method):
    """Measured 2026-09-16, on both methods: no `jql` anywhere Backlot reads it for that method is
    the same 400 real gives, not the unfiltered corpus. A POST's own query string carrying a `jql`
    still draws it, because the body is the only place POST reads one."""
    if method == "get":
        r = client.get("/atlassian/rest/api/3/search/jql?maxResults=5", headers=admin_h)
    else:
        r = _search_post(client, admin_h, query="?jql=project+%3D+payments&maxResults=1")
    assert r.status_code == 400, r.text
    assert r.json() == {
        "errorMessages": [
            "Unbounded JQL queries are not allowed here. Add a search restriction to the query."
        ],
        "errors": {},
    }


def test_jira_search_reads_a_null_jql_as_one_that_was_not_sent(client, admin_h):
    """Measured 2026-09-16: `{"jql": null}` draws the same unbounded-JQL refusal as `{}`. Read
    through a bare `str()` it is the string "None", which restricts nothing and reaches the whole
    visible corpus — the answer this refusal exists to replace."""
    r = client.post(
        "/atlassian/rest/api/3/search/jql",
        headers={**admin_h, "Content-Type": "application/json"},
        content='{"jql": null}',
    )
    assert r.status_code == 400, r.text
    assert r.json()["errorMessages"] == [
        "Unbounded JQL queries are not allowed here. Add a search restriction to the query."
    ]


_MAX_RESULTS_RANGE_MESSAGE = "The maxResults parameter must be between 1 and 5,000."


@pytest.mark.parametrize("value", [0, -1, 5001])
def test_jira_search_refuses_a_max_results_outside_the_range_on_get(client, admin_h, value):
    """Measured 2026-09-18 against Jira Cloud: real refuses `maxResults` outside 1-5000 on both
    methods."""
    r = client.get(
        "/atlassian/rest/api/3/search/jql",
        headers=admin_h,
        params={"jql": "project = payments", "maxResults": value},
    )
    assert r.status_code == 400, r.text
    assert r.json() == {"errorMessages": [_MAX_RESULTS_RANGE_MESSAGE], "errors": {}}


@pytest.mark.parametrize("value", [1, 5000])
def test_jira_search_serves_a_max_results_at_the_range_bounds_on_get(client, admin_h, value):
    r = client.get(
        "/atlassian/rest/api/3/search/jql",
        headers=admin_h,
        params={"jql": "project = payments", "maxResults": value},
    )
    assert r.status_code == 200, r.text


def test_jira_search_max_results_range_loses_to_a_bad_page_token_on_get(client, admin_h):
    """Measured: the token is decoded first, so a request wrong on both counts gets the token's
    refusal rather than the range's."""
    r = client.get(
        "/atlassian/rest/api/3/search/jql",
        headers=admin_h,
        params={"jql": "project = payments", "maxResults": -1, "nextPageToken": "not-a-token"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["errorMessages"] == ["The provided nextPageToken is invalid or has expired."]


def test_jira_search_max_results_range_wins_over_the_unbounded_jql_refusal_on_get(client, admin_h):
    """Measured: an out-of-range `maxResults` with no `jql` at all is the range refusal, not the
    unbounded one."""
    r = client.get("/atlassian/rest/api/3/search/jql", headers=admin_h, params={"maxResults": -1})
    assert r.status_code == 400, r.text
    assert r.json()["errorMessages"] == [_MAX_RESULTS_RANGE_MESSAGE]


@pytest.mark.parametrize("query", ["", "maxResults=", "maxResults=%20"])
def test_jira_search_default_page_size_is_never_checked_against_the_range(
    tmp_path, monkeypatch, query
):
    """`default_page_size` is Backlot's own setting, not a value the vendor ever validated, so a
    deployment that misconfigures it outside 1-5000 must not turn a request that sent no
    `maxResults` at all into a refusal — real never checks its own default against that range.
    An empty or whitespace-only value is the same "not really sent" case on real (see
    `_int_param`), so it gets the same exemption."""
    monkeypatch.setenv("BACKLOT_DEFAULT_PAGE_SIZE", "0")
    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "jira",
                "doc_id": "j-default",
                "project": "payments",
                "title": "T",
                "content": "c",
                "author_email": "a@x.com",
                "visibility": "public",
            }
        ],
    )
    with client_for(s, reload=True) as c:
        h = {"Authorization": f"Bearer {s.admin_token}"}
        get_r = c.get(
            f"/atlassian/rest/api/3/search/jql?jql=project+%3D+payments&{query}", headers=h
        )
        assert get_r.status_code == 200, get_r.text
        post_r = c.post(
            "/atlassian/rest/api/3/search/jql", headers=h, json={"jql": "project = payments"}
        )
        assert post_r.status_code == 200, post_r.text


@pytest.mark.parametrize("value", [0, -1, 5001, "0", "-1", "5001", None, 2147483647, -2147483648])
def test_jira_search_refuses_a_max_results_outside_the_range_on_post(client, admin_h, value):
    """Measured 2026-09-18: the same range applies to the POST body, and a JSON `null` is not the
    parameter unsent the way it is for `jql` — Jackson reads a null int field as `0`, which fails
    this same check. Both ends of Java's `int` bind and reach it too; see
    `_jira_search_max_results`."""
    r = _search_post(client, admin_h, jql="project = payments", maxResults=value)
    assert r.status_code == 400, r.text
    assert r.json() == {"errorMessages": [_MAX_RESULTS_RANGE_MESSAGE], "errors": {}}


@pytest.mark.parametrize("value", [1, 5000, "1", "5000"])
def test_jira_search_serves_a_max_results_at_the_range_bounds_on_post(client, admin_h, value):
    r = _search_post(client, admin_h, jql="project = payments", maxResults=value)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "value",
    [
        "abc",
        True,
        False,
        2147483648,
        -2147483649,
        2147483647.5,
        -2147483648.5,
        int("1" * 1000),
        -int("1" * 1000),
    ],
)
def test_jira_search_post_refuses_a_max_results_the_vendor_will_not_coerce(client, admin_h, value):
    """Measured 2026-09-18: Jackson refuses a non-numeral string or a boolean with the body-wide
    sentence, naming no parameter. A number past Java's `int` draws the same sentence, a fraction
    past it included (see `_jira_search_max_results`), up to the 1000 digits real still reads (see
    `_jira_json_number`)."""
    r = _search_post(client, admin_h, jql="project = payments", maxResults=value)
    assert r.status_code == 400, r.text
    assert r.json() == {"errorMessages": [errors_atlassian.BODY_NOT_AN_OBJECT]}


@pytest.mark.parametrize("value,want_len", [("5", 3), (1.5, 1)])
def test_jira_search_post_coerces_a_max_results_the_vendor_coerces(
    client, admin_h, value, want_len
):
    """Measured: a digit string is read as the number it names — `"5"` serves the whole
    three-issue project here — and a float truncates towards zero, so `1.5` serves one issue,
    where a non-numeral string and a boolean are refused instead."""
    r = _search_post(client, admin_h, jql="project = payments", maxResults=value)
    assert r.status_code == 200, r.text
    assert len(r.json()["issues"]) == want_len


@pytest.mark.parametrize("bogus", [{"startAt": 0}, {"bogus": 1}])
def test_jira_search_post_refuses_an_unknown_body_field(client, admin_h, bogus):
    """Measured 2026-09-18: an undeclared body property is refused, not ignored."""
    r = _search_post(client, admin_h, jql="project = payments", **bogus)
    assert r.status_code == 400, r.text
    assert r.json() == {"errorMessages": [errors_atlassian.BODY_NOT_AN_OBJECT]}


@pytest.mark.parametrize(
    "field,value",
    [
        ("fields", ["summary"]),
        ("fields", [1, None, True]),
        ("fieldsByKeys", True),
        ("failFast", True),
        ("expand", "names"),
        ("properties", ["prop1"]),
        ("reconcileIssues", [1, 2]),
    ],
)
def test_jira_search_post_accepts_every_field_the_vendors_bean_declares(
    client, admin_h, field, value
):
    """Measured 2026-09-18: real's `SearchAndReconcileRequestBean` also takes these, so the
    unknown-field refusal above must not catch them even though Backlot acts on none of them."""
    r = _search_post(client, admin_h, jql="project = payments", **{field: value})
    assert r.status_code == 200, r.text


def _bean(members: str, jql: str | None = '"project = payments"') -> str:
    """A `search/jql` POST body: `jql` first unless ``jql`` is None, then ``members`` as written."""
    return "{" + ", ".join(m for m in (jql and f'"jql": {jql}', members) if m) + "}"


_BEAN_REFUSED = {"errorMessages": [errors_atlassian.BODY_NOT_AN_OBJECT]}
_RECONCILE_NULL = {
    "message": 'Cannot invoke "java.util.List.stream()" because "reconcileIssues" is null',
    "status-code": 500,
    "stack-trace": "",
}


def _properties_null(index: int) -> dict:
    return {"message": f"at index {index}", "status-code": 500, "stack-trace": ""}


# What real answers for a value of each of the five bean members other than `fields` that Backlot
# does not act on, alone and beside a second refusal: the rules are `_jira_bean_boolean_ok`,
# `_jira_reconcile_issue_ok`, the cap on `reconcileIssues` and where `jira_search` raises the two
# 500s. `None` is a 200. The one value of each that
# `test_jira_search_post_accepts_every_field_the_vendors_bean_declares` serves is left to that test.
_BEAN_MEMBER_ROWS = [
    *[(_bean(f'"expand": {v}'), None) for v in ('"bogus"', '""', "1", "1.5", "true", "null")],
    *[(_bean(f'"expand": {v}'), _BEAN_REFUSED) for v in ("[]", '["names"]', "[1]", "{}")],
    *[
        (_bean(f'"{member}": {v}'), None)
        for member in ("fieldsByKeys", "failFast")
        for v in (
            *("false", "null", "0", "2", "-1", "9223372036854775808"),
            *('"true"', '"True"', '"TRUE"', '"false"', '"False"', '"FALSE"', '" TRUE "'),
            *('""', '" "', '"null"'),
            json.dumps(chr(1) + "true"),
        )
    ],
    *[
        (_bean(f'"{member}": {v}'), _BEAN_REFUSED)
        for member in ("fieldsByKeys", "failFast")
        for v in (
            *('"yes"', '"x"', '"tRuE"', '"NULL"', '"1"', '"\\u00a0true"'),
            *("1.5", "1.0", "[]", "{}"),
        )
    ],
    *[
        (_bean(f'"properties": {v}'), None)
        for v in ("[1]", "[1.5]", "[true]", '[""]', "[]", "null")
    ],
    *[
        (_bean(f'"properties": {v}'), _BEAN_REFUSED)
        for v in ('"prop1"', '""', "1", "{}", "[[1]]", "[{}]", "[null, [1]]", "[{}, null]")
    ],
    *[
        (_bean(f'"properties": {v}'), _properties_null(i))
        for v, i in (("[null]", 0), ('["a", null]', 1), ('["a", "b", null, null]', 2))
    ],
    *[
        (_bean(f'"reconcileIssues": {v}'), None)
        for v in (
            *("[]", "[null]", "[1, null, 2]", "[-1]", "[-9223372036854775808]"),
            *("[1.5]", "[1e2]", "[9.2e18]", "[-9.2e18]"),
            *("[9223372036854775807.0]", "[-9223372036854775808.0]", '["%s1"]' % ("0" * 999)),
            *('["1"]', '[" 1"]', '["+1"]', '["01"]', '["\\u0661"]', '["\\u0665"]'),
            *('["9223372036854775807"]',),
            *('[""]', '[" "]', '["null"]', json.dumps([chr(1) + "1"])),
        )
    ],
    *[
        (_bean(f'"reconcileIssues": {v}'), _BEAN_REFUSED)
        for v in (
            *('["a"]', "[true]", "1", '"1"', "{}", "[[1]]", '[null, "a"]'),
            *("[9223372036854775808]", "[-9223372036854775809]", "[9.3e18]", "[-1e19]"),
            *('["1.5"]', '["1e2"]', '["0x10"]', '["1_000"]', '["\\u00a01"]'),
            *('["9223372036854775808"]', '["%s1"]' % ("0" * 1000), '["\\ud835\\udfd3"]'),
            *("[9223372036854777856.0]", "[-9223372036854777856.0]"),
        )
    ],
    (_bean('"reconcileIssues": null'), _RECONCILE_NULL),
    # The two 500s beside a second refusal.
    (_bean('"reconcileIssues": null', jql=None), errors_atlassian.unbounded_jql().body),
    (
        _bean('"reconcileIssues": null, "maxResults": 0'),
        errors_atlassian.max_results_out_of_range().body,
    ),
    (
        _bean('"maxResults": 0, "reconcileIssues": null'),
        errors_atlassian.max_results_out_of_range().body,
    ),
    (
        _bean('"reconcileIssues": null, "nextPageToken": "bogus"'),
        errors_atlassian.bad_page_token().body,
    ),
    (_bean('"reconcileIssues": null, "maxResults": "abc"'), _BEAN_REFUSED),
    (_bean('"maxResults": "abc", "reconcileIssues": null'), _BEAN_REFUSED),
    (_bean('"reconcileIssues": null, "bogus": 1'), _BEAN_REFUSED),
    (_bean('"reconcileIssues": null', jql='"project = ZZZNOPE999"'), _RECONCILE_NULL),
    (_bean('"properties": [null], "reconcileIssues": ["a"]'), _BEAN_REFUSED),
    (_bean('"reconcileIssues": ["a"], "properties": [null]'), _BEAN_REFUSED),
    (_bean('"properties": [null], "maxResults": "abc"'), _BEAN_REFUSED),
    (_bean('"maxResults": "abc", "properties": [null]'), _BEAN_REFUSED),
    (_bean('"properties": [null], "expand": []'), _BEAN_REFUSED),
    (_bean('"properties": [null], "bogus": 1'), _BEAN_REFUSED),
    *[
        (raw, _properties_null(0))
        for raw in (
            _bean('"properties": [null]', jql=None),
            _bean('"properties": [null], "maxResults": 0'),
            _bean('"properties": [null], "nextPageToken": "bogus"'),
            _bean('"properties": [null], "reconcileIssues": null'),
            _bean('"reconcileIssues": null, "properties": [null]'),
        )
    ],
    (
        _bean(f'"reconcileIssues": {[10000 + i for i in range(50)]}'),
        None,
    ),
    (
        _bean(f'"reconcileIssues": {[10000 + i for i in range(51)]}'),
        _RECONCILE_TOO_MANY,
    ),
    (
        _bean(f'"reconcileIssues": {[10000 + i for i in range(51)]}', jql=None),
        _RECONCILE_TOO_MANY,
    ),
    (
        _bean(f'"reconcileIssues": {[10000 + i for i in range(51)]}, "maxResults": 0'),
        _RECONCILE_TOO_MANY,
    ),
    (
        _bean(f'"reconcileIssues": {[10000 + i for i in range(51)]}, "nextPageToken": "bogus"'),
        errors_atlassian.bad_page_token().body,
    ),
    # A null or an empty element counts toward the cap; a 500 and a binding refusal come first.
    *[
        (_bean('"reconcileIssues": ' + json.dumps(ids)), want)
        for ids, want in (
            ([10000 + i for i in range(50)] + [None], _RECONCILE_TOO_MANY),
            ([10000 + i for i in range(50)] + [""], _RECONCILE_TOO_MANY),
            ([None] * 50, None),
            ([None] * 51, _RECONCILE_TOO_MANY),
        )
    ],
    (
        _bean(f'"reconcileIssues": {[10000 + i for i in range(51)]}, "properties": [null]'),
        _properties_null(0),
    ),
    (_bean(f'"reconcileIssues": {[10000 + i for i in range(51)]}, "failFast": "x"'), _BEAN_REFUSED),
]


@pytest.mark.parametrize("raw,want", _BEAN_MEMBER_ROWS)
def test_jira_search_post_binds_the_members_it_does_not_act_on_as_real_binds_them(
    client, admin_h, raw, want
):
    r = client.post(
        "/atlassian/rest/api/3/search/jql",
        headers={**admin_h, "Content-Type": "application/json"},
        content=raw,
    )
    if want is None:
        assert r.status_code == 200, r.text
    else:
        assert (r.status_code, r.json()) == (want.get("status-code", 400), want), r.text
        assert r.headers["content-type"] == "application/json;charset=UTF-8"


def test_jira_search_post_refuses_a_jql_shaped_as_a_list(client, admin_h):
    """Measured 2026-09-18: `{"jql": [...]}` draws the same not-an-object refusal as an unknown
    field, where a scalar `jql` (a number, say) is coerced to a string and reaches the (lenient)
    JQL parser instead."""
    r = _search_post(client, admin_h, jql=["order by created"])
    assert r.status_code == 400, r.text
    assert r.json() == {"errorMessages": [errors_atlassian.BODY_NOT_AN_OBJECT]}


@pytest.mark.parametrize("value", [["abc"], {"a": 1}])
def test_jira_search_post_refuses_a_next_page_token_shaped_as_a_list_or_object(
    client, admin_h, value
):
    """Measured 2026-09-18: the same not-an-object refusal `jql` draws as a list applies to
    `nextPageToken` too, where a scalar (an int, a bool) is coerced to a string and reaches the
    token decoder instead, drawing `bad_page_token`."""
    r = _search_post(client, admin_h, jql="project = payments", nextPageToken=value)
    assert r.status_code == 400, r.text
    assert r.json() == {"errorMessages": [errors_atlassian.BODY_NOT_AN_OBJECT]}


@pytest.mark.parametrize(
    "raw,message",
    [
        # JSON's whitespace is the four ASCII ones, so a non-breaking space in front is not skipped
        (
            "\xa0{}",
            "There was an error parsing JSON. Check that your request body is valid.",
        ),
        # and bytes that are not UTF-8 are not repaired into U+FFFD and then parsed
        (
            b'{"jql": "project = payments\xff"}',
            "Invalid request payload. Refer to the REST API documentation and try again.",
        ),
    ],
)
def test_jira_search_post_refuses_the_leading_bytes_real_refuses(client, admin_h, raw, message):
    """Bytes TRAILING a complete value are ignored; the leading ones are not ignored so freely, and
    both boundaries are measured rather than taken from a JSON reader's own defaults."""
    r = client.post(
        "/atlassian/rest/api/3/search/jql",
        headers={**admin_h, "Content-Type": "application/json"},
        content=raw if isinstance(raw, bytes) else raw.encode(),
    )
    assert r.status_code == 400, r.text
    assert r.json() == {"errorMessages": [message]}


def test_jira_search_advertises_both_its_methods_when_it_refuses_a_third(client, admin_h):
    """Measured: real answers `PUT` with `Allow: POST, GET`. The header is the measured table's
    (`errors.atlassian.jira_allow`), which is keyed by the path whatever route serves it."""
    for version in ("2", "3"):
        r = client.request("PUT", f"/atlassian/rest/api/{version}/search/jql", headers=admin_h)
        assert r.status_code == 405, r.text
        assert set(r.headers["allow"].replace(" ", "").split(",")) == {"GET", "POST"}


def test_jira_search_declares_each_placement_on_the_method_that_reads_it(client):
    """The served spec has to make the split discoverable, or a generated client keeps sending a
    POST cursor in the query string and never sees why it does not page."""
    paths = client.app.openapi()["paths"]["/atlassian/rest/api/3/search/jql"]
    assert {p["name"] for p in paths["get"]["parameters"]} == {
        "jql",
        "maxResults",
        "nextPageToken",
    }
    assert "requestBody" not in paths["get"]
    assert "parameters" not in paths["post"] or paths["post"]["parameters"] == []
    assert paths["post"]["requestBody"]["required"] is True


@pytest.mark.parametrize("slash", ["", "/"])
@pytest.mark.parametrize(
    "content_type,named",
    [
        (None, "null"),
        ("text/plain", "text/plain"),
        ("application/xml", "application/xml"),
        ("*/*", "*/*"),
    ],
)
def test_jira_search_post_refuses_a_media_type_it_does_not_read(
    client, admin_h, content_type, named, slash
):
    """Measured: the header decides before the bytes are looked at, so the JSON body sent with each
    of these is never reached."""
    headers = dict(admin_h)
    if content_type is not None:
        headers["Content-Type"] = content_type
    r = client.post(
        f"/atlassian/rest/api/3/search/jql{slash}",
        headers=headers,
        content=json.dumps({"jql": "project = payments"}),
    )
    assert r.status_code == 415, r.text
    assert r.headers["content-type"] == "application/problem+json;charset=UTF-8"
    assert r.json() == {
        "type": "about:blank",
        "title": "Unsupported Media Type",
        "status": 415,
        "detail": f"Content-Type '{named}' is not supported.",
        "instance": f"/rest/api/3/search/jql{slash}",
    }


@pytest.mark.parametrize(
    "content_type", ["application/json", "APPLICATION/JSON", "application/json; charset=utf-8"]
)
def test_jira_search_post_reads_the_media_type_the_way_real_matches_it(
    client, admin_h, content_type
):
    """Case-insensitive, and parameters are ignored — all three are read on real."""
    r = client.post(
        "/atlassian/rest/api/3/search/jql",
        headers={**admin_h, "Content-Type": content_type},
        content=json.dumps({"jql": "project = payments"}),
    )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "raw,message",
    [
        # "no content" is about LENGTH, not emptiness after stripping
        ("", "No content to map to Object due to end of input"),
        ("null", "No content to map to Object due to end of input"),
        ("{", "There was an error parsing JSON. Check that your request body is valid."),
        ("[]", "Invalid request payload. Refer to the REST API documentation and try again."),
        # a body that parses to anything but an object, whatever that anything is
        ("5", "Invalid request payload. Refer to the REST API documentation and try again."),
        ('"x"', "Invalid request payload. Refer to the REST API documentation and try again."),
        ("true", "Invalid request payload. Refer to the REST API documentation and try again."),
        # whitespace alone is NOT the parse error a JSON reader would raise, and not "no content"
        ("   ", "Invalid request payload. Refer to the REST API documentation and try again."),
        # constants Python's reader takes and real's does not (see `_reject_json_constant`)
        *[
            (
                '{"jql": "project = payments", "maxResults": %s}' % value,
                errors_atlassian.BODY_UNPARSEABLE,
            )
            for value in ("NaN", "Infinity", "-Infinity")
        ],
        # `1e400` reads as an infinite float, past Java's `int` (see `_jira_search_max_results`)
        *[
            (
                '{"jql": "project = payments", "maxResults": %s}' % value,
                errors_atlassian.BODY_NOT_AN_OBJECT,
            )
            for value in ("1e400", "-1e400")
        ],
        # a `fields` that is not a list of scalars (see `jira_search`)
        *[
            (
                json.dumps({"jql": "project = payments", "fields": fields}),
                errors_atlassian.BODY_NOT_AN_OBJECT,
            )
            for fields in ([["id"]], [{}], {}, "id")
        ],
        # nested past the recursion limit of Python 3.11's reader (999) and of 3.13's (100000),
        # so each row pins the answer on one side of a limit that moves with the Python
        *[
            pytest.param(
                '{"jql": "project = payments", "fields": %s"id"%s}' % ("[" * depth, "]" * depth),
                errors_atlassian.BODY_NOT_AN_OBJECT,
                id=f"fields-nested-{depth}",
            )
            for depth in (999, 100_000)
        ],
        # a number past 1000 digits inside a list or an object, whichever member holds it (see
        # `_jira_search_body`); as the body or a member's value it is
        # `test_jira_search_post_refuses_a_number_past_1000_digits_as_failed_read`
        *[
            pytest.param(raw, errors_atlassian.BODY_NOT_AN_OBJECT, id=name)
            for name, raw in [
                ("fields-list", '{"jql": "project = payments", "fields": [%s]}' % ("1" * 1001)),
                (
                    "properties-list",
                    '{"jql": "project = payments", "properties": [%s]}' % ("1" * 5000),
                ),
                (
                    "reconcileIssues-nested",
                    '{"jql": "project = payments", "reconcileIssues": [[%s]]}' % ("1" * 1001),
                ),
                (
                    "expand-object",
                    '{"jql": "project = payments", "expand": {"a": %s}}' % ("1" * 1001),
                ),
                ("body-list", "[%s]" % ("1" * 1001)),
            ]
        ],
    ],
)
def test_jira_search_post_refuses_a_body_it_cannot_turn_into_an_object(
    client, admin_h, raw, message
):
    """Three sentences, measured."""
    r = client.post(
        "/atlassian/rest/api/3/search/jql",
        headers={**admin_h, "Content-Type": "application/json"},
        content=raw,
    )
    assert r.status_code == 400, r.text
    assert r.json() == {"errorMessages": [message]}


@pytest.mark.parametrize("tail", [" trailing", " " + "[" * 1001])
def test_jira_search_post_ignores_bytes_after_a_complete_body(client, admin_h, tail):
    """Measured: `{"jql": …} junk` is answered 200 — the vendor's parser reads the first value and
    lets the rest go, where a whole-input JSON read would refuse it."""
    r = client.post(
        "/atlassian/rest/api/3/search/jql",
        headers={**admin_h, "Content-Type": "application/json"},
        content=json.dumps({"jql": "project = payments"}) + tail,
    )
    assert r.status_code == 200, r.text
    assert r.json()["issues"]


_SEARCH_V3 = "/rest/api/3/search/jql"


@pytest.mark.parametrize(
    "path,raw,echoed",
    [
        pytest.param(_SEARCH_V3, body, _SEARCH_V3, id=name)
        for name, body in [
            ("maxResults-1001", '{"jql": "x", "maxResults": %s}' % ("1" * 1001)),
            ("maxResults-negative-1001", '{"jql": "x", "maxResults": -%s}' % ("1" * 1001)),
            ("maxResults-4301", '{"jql": "x", "maxResults": %s}' % ("1" * 4301)),
            ("maxResults-5000", '{"jql": "x", "maxResults": %s}' % ("1" * 5000)),
            ("maxResults-1000-then-fraction", '{"jql": "x", "maxResults": %s.5}' % ("1" * 1000)),
            ("maxResults-exponent", '{"jql": "x", "maxResults": 1.%se1}' % ("1" * 999)),
            ("jql", '{"jql": %s}' % ("1" * 1001)),
            ("nextPageToken", '{"jql": "x", "nextPageToken": %s}' % ("1" * 1001)),
            ("expand", '{"jql": "x", "expand": %s}' % ("1" * 5000)),
            ("fieldsByKeys", '{"jql": "x", "fieldsByKeys": %s}' % ("1" * 1001)),
            ("properties", '{"jql": "x", "properties": %s}' % ("1" * 1001)),
            ("reconcileIssues", '{"jql": "x", "reconcileIssues": %s}' % ("1" * 1001)),
            ("fields", '{"jql": "x", "fields": %s}' % ("1" * 1001)),
            ("undeclared-member", '{"jql": "x", "bogus": %s}' % ("1" * 1001)),
            ("body-1001", "1" * 1001),
            ("body-5000", "1" * 5000),
        ]
    ]
    + [
        # the path a refusal echoes, as `_echoed_path` gives it
        pytest.param(path, '{"jql": %s}' % ("1" * 1001), echoed, id=path)
        for path, echoed in [
            ("/rest/api/3/search/jql/", "/rest/api/3/search/jql/"),
            ("/rest/api/3//search/jql", _SEARCH_V3),
            ("/rest/api/2/search/jql", "/rest/api/2/search/jql"),
        ]
    ],
)
def test_jira_search_post_refuses_a_number_past_1000_digits_as_failed_read(
    client, admin_h, path, raw, echoed
):
    """A number literal past 1000 digits, the fraction's and exponent's counted, is real's
    problem+json refusal as the body or as any member's value (see `_jira_search_body`); inside a
    list or an object it is a row of
    `test_jira_search_post_refuses_a_body_it_cannot_turn_into_an_object`."""
    r = client.post(
        f"/atlassian{path}",
        headers={**admin_h, "Content-Type": "application/json"},
        content=raw,
    )
    assert r.status_code == 400, r.text
    assert r.headers["content-type"] == "application/problem+json;charset=UTF-8"
    assert r.json() == {
        "type": "about:blank",
        "title": "Bad Request",
        "status": 400,
        "detail": "Failed to read request",
        "instance": echoed,
    }


def test_jira_search_post_with_no_body_at_all_is_the_media_type_refusal(client, admin_h):
    """No body at all is the media-type refusal: the query string
    `POST search/jql?jql=…&maxResults=1` carries is never reached, because the missing header
    refuses the request first."""
    r = client.post(
        "/atlassian/rest/api/3/search/jql?jql=project+%3D+payments&maxResults=1", headers=admin_h
    )
    assert r.status_code == 415, r.text
    assert r.json()["detail"] == "Content-Type 'null' is not supported."


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("project", ["payments", "ZZZNOPE999"])
def test_jira_search_refuses_a_page_token_it_cannot_decode(client, admin_h, method, project):
    """Measured on both methods, identically, and unaffected by whether the JQL's project
    resolves: 2026-09-16, `project = ZZZNOPE999` (matching no project) with a bogus
    `nextPageToken` still draws the 400 on real, not the unresolved-project's empty page."""
    jql = f"project = {project}"
    if method == "get":
        r = client.get(
            f"/atlassian/rest/api/3/search/jql?jql={quote(jql)}&nextPageToken=BOGUS",
            headers=admin_h,
        )
    else:
        r = _search_post(client, admin_h, jql=jql, nextPageToken="BOGUS")
    assert r.status_code == 400, r.text
    assert r.json()["errors"] == {}
    assert "nextPageToken" in r.json()["errorMessages"][0]


def test_jira_search_still_pages_on_a_token_it_issued(client, admin_h):
    """The refusal above must not catch the tokens Backlot hands out, on either method."""
    first = client.get(
        "/atlassian/rest/api/3/search/jql?jql=project+%3D+payments&maxResults=1", headers=admin_h
    ).json()
    assert (
        client.get(
            f"/atlassian/rest/api/3/search/jql?jql=project+%3D+payments&maxResults=1"
            f"&nextPageToken={first['nextPageToken']}",
            headers=admin_h,
        ).json()["issues"][0]["key"]
        != first["issues"][0]["key"]
    )


# --- a wrong method: two products, two shapes (#233) ---------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/wiki/rest/api/space/{key}/permission"),
        ("put", "/wiki/rest/api/content"),
    ],
)
def test_confluence_refuses_a_wrong_method_with_springs_errors_list_and_no_allow(
    client, admin_h, method, path
):
    """Both requests measured on brekkylab.atlassian.net, 2026-09-16. The shape and what it costs a
    client are in ``errors.atlassian.method_not_allowed``; this holds it on the wire, including the
    title naming the method that arrived."""
    key = client.get("/atlassian/wiki/rest/api/space", headers=admin_h).json()["results"][0]["key"]
    r = getattr(client, method)(f"/atlassian{path}".format(key=key), headers=admin_h)
    assert r.status_code == 405, r.text
    assert r.headers["content-type"] == "application/json"
    assert "allow" not in r.headers
    assert r.json() == {
        "errors": [
            {
                "status": 405,
                "code": "METHOD_NOT_ALLOWED",
                "title": (
                    "org.springframework.web.HttpRequestMethodNotSupportedException: "
                    f"Request method '{method.upper()}' not supported"
                ),
            }
        ]
    }


@pytest.mark.parametrize("slash", ["", "/"])
@pytest.mark.parametrize(
    "method,path,allow",
    [
        # every row measured on brekkylab.atlassian.net, 2026-09-16, by sending a method the vendor
        # defines on no route of that path; real's ORDER varies per response, so only the set is
        # real's and the order below is `_JIRA_ALLOW`'s
        ("post", "/rest/api/3/serverInfo", "GET"),
        ("put", "/rest/api/3/field", "GET, POST"),
        ("post", "/rest/api/3/issue/PAY-1", "GET, PUT, DELETE"),
        ("put", "/rest/api/3/issue/PAY-1/comment", "GET, POST"),
        ("put", "/rest/api/2/search/jql", "GET, POST"),
        # the row the vendor's own document disagrees with: it declares GET alone on `project/search`
        ("post", "/rest/api/3/project/search", "GET, PUT, DELETE"),
        # an escape in the path, echoed as ``errors.atlassian.echoed_spelling`` spells it
        ("post", "/rest/api/3/issue/A%20B", "GET, PUT, DELETE"),
        ("post", "/rest/api/3/issue/A%23B", "GET, PUT, DELETE"),
        ("post", "/rest/api/3/issue/A%3FB", "GET, PUT, DELETE"),
    ],
)
def test_jira_refuses_a_wrong_method_as_rfc_7807_naming_the_methods_it_takes(
    client, admin_h, method, path, allow, slash
):
    r = getattr(client, method)(f"/atlassian{path}{slash}", headers=admin_h)
    assert r.status_code == 405, r.text
    assert r.headers["content-type"] == errors_atlassian.PROBLEM_JSON
    assert r.headers["allow"] == allow
    assert r.json() == {
        "type": "about:blank",
        "title": "Method Not Allowed",
        "status": 405,
        "detail": f"Method '{method.upper()}' is not supported.",
        "instance": f"{path}{slash}",
    }


@pytest.mark.parametrize(
    "table",
    [errors_atlassian.jira_allow, errors_atlassian.jira_options_allow],
    ids=["405", "options"],
)
def test_every_jira_route_carries_a_measured_allow(client, table):
    """The `Allow` tables, the 405's and the OPTIONS', are keyed by route, so a route added later
    without a row would answer a 405 naming whatever methods Backlot happens to declare — which is
    Backlot's truth, not the vendor's — and an OPTIONS with no `Allow` at all. This is the reminder
    to measure one."""
    paths = [
        p for p in client.get("/openapi.json").json()["paths"] if p.startswith("/atlassian/rest")
    ]
    assert paths
    assert [p for p in paths if table(p) is None] == []


# --- the space reads (#234) -----------------------------------------------------------------


def test_confluence_space_reads_carry_the_identifiers_and_links_real_sends(client, admin_h):
    """Measured 2026-09-16 on a global space and two personal ones. The two reads agree on every
    field but `_links`: inside the listing a space carries `webui` and `self` alone, where the
    single read carries `context`, `collection` and `base` beside them."""
    from backlot import synth
    from backlot.config import get_settings

    listed = client.get("/atlassian/wiki/rest/api/space", headers=admin_h).json()["results"][0]
    key = listed["key"]
    single = client.get(f"/atlassian/wiki/rest/api/space/{key}", headers=admin_h).json()
    cloud = synth.atlassian_cloud_id(get_settings().org_name)

    for space in (listed, single):
        assert space["ari"] == f"ari:cloud:confluence:{cloud}:space/{space['id']}"
        assert space["alias"] == space["key"]
        assert space["status"] == "current"
        assert len(space["_expandable"]) == 13
        assert space["_expandable"]["permissions"] == ""
        assert space["_expandable"]["theme"] == f"/rest/api/space/{key}/theme"

    assert sorted(listed["_links"]) == ["self", "webui"]
    assert sorted(single["_links"]) == ["base", "collection", "context", "self", "webui"]
    assert single["_links"]["collection"] == "/rest/api/space"
    assert single["_links"]["webui"] == f"/spaces/{key}"


def test_confluence_expands_permissions_with_one_entry_per_grant(tmp_path):
    """Real's unit is the grant, not the space — see ``_space_permissions`` for the counts behind
    that. A user grant is a single-user subject; a group or an org grant carries no `subjects` key
    at all, which is the shape real gives a principal it leaves collapsed. Its own corpus because
    the shared one carries no user grant on a Confluence space, so neither shape could be told from
    the other there."""
    corpus = [
        {
            "source_type": "confluence",
            "space": "open",
            "title": "Open",
            "content": "Body.",
            "author_email": "ava@acme.com",
            "visibility": "public",
        },
        {
            "source_type": "confluence",
            "space": "eng-only",
            "title": "Group",
            "content": "Body.",
            "author_email": "ava@acme.com",
            "author_groups": ["engineering"],
            "visibility": "group",
        },
        {
            "source_type": "confluence",
            "space": "shared-private",
            "title": "Bob's",
            "content": "Body.",
            "author_email": "bob@acme.com",
            "visibility": "private",
        },
        {
            "source_type": "confluence",
            "space": "shared-private",
            "title": "Ava's",
            "content": "Body.",
            "author_email": "ava@acme.com",
            "visibility": "private",
        },
    ]
    settings = tiny_corpus(tmp_path, corpus)
    with client_for(settings, reload=True) as c:
        admin = {"Authorization": f"Bearer {settings.admin_token}"}
        spaces = c.get("/atlassian/wiki/rest/api/space?expand=permissions", headers=admin).json()
        by_name = {s["name"]: s["permissions"] for s in spaces["results"]}
        assert sorted(by_name) == ["eng-only", "open", "shared-private"]

        for name in ("open", "eng-only"):
            (entry,) = by_name[name]
            assert "subjects" not in entry, name
            assert entry["operation"] == {"operation": "read", "targetType": "space"}
            # an org grant is every MEMBER, not every visitor, and an anonymous caller is refused
            # before a space resolves — so neither flag is ever True here
            assert entry["anonymousAccess"] is False
            assert entry["unlicensedAccess"] is False

        # two user grants on one space are two entries, which is what makes the id a per-grant id
        private = by_name["shared-private"]
        assert len(private) == 2
        assert len({e["id"] for e in private}) == 2
        named = []
        for entry in private:
            users = entry["subjects"]["user"]
            assert users["size"] == len(users["results"]) == 1
            assert entry["subjects"]["_expandable"] == {"group": ""}
            named.append(users["results"][0]["email"])
        assert sorted(named) == ["ava@acme.com", "bob@acme.com"]

        ids = [e["id"] for perms in by_name.values() for e in perms]
        assert len(ids) == len(set(ids))

        # the subject is the object every Confluence read carries: measured 2026-09-17, real sends
        # the same thirteen keys for a roster subject, a page's `version.by` and its `createdBy`
        page = c.get(
            "/atlassian/wiki/rest/api/content?limit=1&expand=version,history", headers=admin
        ).json()["results"][0]
        subject = private[0]["subjects"]["user"]["results"][0]
        assert list(subject) == [
            "type",
            "accountId",
            "accountType",
            "email",
            "publicName",
            "profilePicture",
            "displayName",
            "isExternalCollaborator",
            "isGuest",
            "locale",
            "accountStatus",
            "_expandable",
            "_links",
        ]
        assert subject["accountStatus"] == "active"
        assert subject["isGuest"] is False and subject["isExternalCollaborator"] is False
        assert subject["_expandable"] == {"operations": "", "personalSpace": ""}
        assert subject["_links"]["self"] == (
            f"http://testserver/wiki/rest/api/user?accountId={subject['accountId']}"
        )
        for user in (page["version"]["by"], page["history"]["createdBy"]):
            assert list(user) == list(subject)


def test_confluence_both_space_reads_answer_the_same_roster(client, admin_h):
    """An expansion that is served leaves `_expandable`, which is how a client tells an expansion it
    got from one it asked for and did not, and the listing's entry is the single read's."""
    listed = client.get(
        "/atlassian/wiki/rest/api/space?expand=permissions", headers=admin_h
    ).json()["results"][0]
    single = client.get(
        f"/atlassian/wiki/rest/api/space/{listed['key']}?expand=permissions", headers=admin_h
    ).json()
    for space in (listed, single):
        assert space["permissions"]
        assert "permissions" not in space["_expandable"]
        assert len(space["_expandable"]) == 12
    assert single["permissions"] == listed["permissions"]


@pytest.mark.parametrize(
    "expand,description,permissions",
    [
        ("description", True, False),
        ("description.plain", True, False),
        ("description.view,permissions", True, True),
        ("permissions.bogus", False, True),
        ("descriptions", False, False),
        ("bogus", False, False),
        ("", False, False),
    ],
)
def test_confluence_expands_the_property_the_first_dotted_segment_names(
    client, admin_h, expand, description, permissions
):
    """Measured 2026-09-17 on `space/{key}`: the property is named by the FIRST dotted segment of
    each term, so `description.plain` and `permissions.bogus` both expand, and a term naming no
    property is ignored rather than refused — `expand=bogus` answers 200 with `_expandable` whole,
    as an absent `expand` does.

    Both reads take the expansion, so both are asserted: they share `_space`.
    """
    listed = client.get(f"/atlassian/wiki/rest/api/space?expand={expand}", headers=admin_h).json()[
        "results"
    ][0]
    single = client.get(
        f"/atlassian/wiki/rest/api/space/{listed['key']}?expand={expand}", headers=admin_h
    ).json()
    for space in (listed, single):
        assert ("description" in space) is description
        assert ("permissions" in space) is permissions
        assert ("description" in space["_expandable"]) is not description
        assert ("permissions" in space["_expandable"]) is not permissions


@pytest.mark.parametrize(
    "expand,served,expandable",
    [
        ("description", (), {"view": "", "plain": ""}),
        ("description.plain", ("plain",), {"view": ""}),
        ("description.view", ("view",), {"plain": ""}),
        ("description.plain,description.view", ("plain", "view"), None),
    ],
)
def test_confluence_description_carries_a_value_only_for_the_rendering_asked_for(
    client, admin_h, expand, served, expandable
):
    """Measured 2026-09-17 on `space/{key}`: the bare term carries no value at all, `.plain` and
    `.view` each carry one and leave the other in a nested `_expandable`, and asking for both leaves
    no `_expandable`. A client reading `description.plain.value` off the bare term gets nothing from
    real, so answering one there would be a body real does not send."""
    listed = client.get(f"/atlassian/wiki/rest/api/space?expand={expand}", headers=admin_h).json()[
        "results"
    ][0]
    single = client.get(
        f"/atlassian/wiki/rest/api/space/{listed['key']}?expand={expand}", headers=admin_h
    ).json()
    for space in (listed, single):
        got = space["description"]
        assert tuple(k for k in got if k != "_expandable") == served
        for rendering in served:
            assert got[rendering] == {
                "value": f"{space['name']} space",
                "representation": rendering,
                "embeddedContent": [],
            }
        assert got.get("_expandable") == expandable


def test_confluence_refuses_the_permission_read_before_it_resolves_the_key(client, admin_h):
    """`GET space/NOSUCHSPACE/permission` is the same 405 as the key that names a space, measured
    2026-09-16 — the method is refused ahead of the lookup, so this route never reports whether a
    space exists. The space read itself still does, with its 404."""
    absent = client.get("/atlassian/wiki/rest/api/space/NOSUCHSPACE/permission", headers=admin_h)
    known = client.get("/atlassian/wiki/rest/api/space", headers=admin_h).json()["results"][0]
    present = client.get(
        f"/atlassian/wiki/rest/api/space/{known['key']}/permission", headers=admin_h
    )
    assert absent.status_code == present.status_code == 405
    assert absent.json() == present.json()
    assert "allow" not in absent.headers


# --- what answers before, and around, a route ------------------------------------------------


@pytest.fixture(scope="module")
def keys(client, admin_h) -> dict[str, str]:
    """One served key of each kind, so the sweeps below ask real routes about real resources."""
    from backlot import synth

    space = client.get("/atlassian/wiki/rest/api/space", headers=admin_h).json()["results"][0]
    content = client.get("/atlassian/wiki/rest/api/content", headers=admin_h).json()["results"][0]
    issue = crawl_jira(client, admin_h)[0]
    return {
        "space": space["key"],
        "content": str(content["id"]),
        "issue": issue,
        "project": synth.jira_project_key("payments"),
    }


def _routes(keys: dict[str, str]) -> list[str]:
    """Every Atlassian route, addressed at a resource the corpus serves."""
    k, c, i, p = keys["space"], keys["content"], keys["issue"], keys["project"]
    return [
        "/atlassian/rest/api/2/serverInfo",
        "/atlassian/rest/api/3/serverInfo",
        "/atlassian/rest/api/2/field",
        "/atlassian/rest/api/3/field",
        "/atlassian/rest/api/3/issueLinkType",
        "/atlassian/rest/api/3/project/search",
        f"/atlassian/rest/api/3/project/{p}/role",
        f"/atlassian/rest/api/3/project/{p}/role/10002",
        f"/atlassian/rest/api/2/issue/{i}",
        f"/atlassian/rest/api/3/issue/{i}",
        f"/atlassian/rest/api/2/issue/{i}/comment",
        f"/atlassian/rest/api/3/issue/{i}/comment",
        "/atlassian/rest/api/2/search/jql?jql=&maxResults=1",
        "/atlassian/rest/api/3/search/jql?jql=&maxResults=1",
        "/atlassian/wiki/rest/api/space",
        f"/atlassian/wiki/rest/api/space/{k}",
        f"/atlassian/wiki/rest/api/space/{k}/permission",
        "/atlassian/wiki/rest/api/content",
        f"/atlassian/wiki/rest/api/content/{c}",
        f"/atlassian/wiki/rest/api/content/{c}/child/page",
        f"/atlassian/wiki/rest/api/content/{c}/child/comment",
        f"/atlassian/wiki/rest/api/content/{c}/child/attachment",
        f"/atlassian/wiki/rest/api/content/{c}/label",
        f"/atlassian/wiki/rest/api/content/{c}/restriction/byOperation",
        "/atlassian/wiki/rest/api/search?cql=type%3Dpage",
    ]


def test_atlassian_a_head_is_the_get_without_its_body(client, admin_h, keys):
    """Pins the rule the comment on ``backlot.main._HEAD_IS_THE_GET_WITHOUT_ITS_BODY`` records for
    both products, on each route, on the 404s it names and on a slashed spelling
    (``backlot.main.normalise_the_slashes_in_an_atlassian_path``).

    The length is where the two part, and which answers declare one is
    ``errors.atlassian.head_content_length``, measured there.

    The test client drops a `HEAD` body itself, so what the app sends is read at the ASGI layer.
    """
    from starlette.testclient import TestClient

    sent = []

    async def recording(scope, receive, send):
        async def record(message):
            sent.append(message)
            await send(message)

        await client.app(scope, receive, record)

    rows = [
        *((path, admin_h) for path in _routes(keys)),
        ("/atlassian/wiki/rest/api/space/NOPESUCH", admin_h),
        ("/atlassian/rest/api/3/issue/NOPE-1", admin_h),
        # slashed spellings, answered as the slash-free ones
        # (``backlot.main.normalise_the_slashes_in_an_atlassian_path``)
        ("/atlassian/wiki/rest/api/search/?cql=type%3Dpage", admin_h),
        ("/atlassian//wiki/rest/api/space", admin_h),
        ("/atlassian/rest/api/3/serverInfo/", admin_h),
        # the gateway's Connect-token 403 (``backlot.main.refuse_a_bearer_jira_cannot_read``)
        ("/atlassian/rest/api/3/serverInfo", {"Authorization": "Bearer nope"}),
        # and its 401, which declares its 53 bytes (``errors.atlassian.head_content_length``)
        ("/atlassian/rest/api/3/myself", {}),
    ]
    statuses = []
    for path, headers in rows:
        got = client.get(path, headers=headers)
        head = client.head(path, headers=headers)
        assert head.status_code == got.status_code, path
        assert head.headers["content-type"] == got.headers["content-type"], path
        search = path.startswith("/atlassian/wiki/rest/api/search")
        confluence_200 = "/wiki/" in path and got.status_code == 200 and not search
        if confluence_200 or ("/wiki/" not in path and got.status_code == 401):
            assert head.headers["content-length"] == str(len(got.content)), path
        else:
            assert "content-length" not in head.headers, path
        statuses.append(head.status_code)
        # No `with`: a second lifespan would overwrite the state `client` started.
        sent.clear()
        TestClient(recording).head(path, headers=headers)
        bodies = [m.get("body", b"") for m in sent if m["type"] == "http.response.body"]
        assert b"".join(bodies) == b"", path
    assert statuses[-7:] == [404, 404, 200, 200, 200, 403, 401]


@pytest.mark.parametrize(
    "path,allow",
    [
        # the `Allow` ``errors.atlassian._JIRA_OPTIONS_ALLOW`` copies, route by route
        ("/atlassian/rest/api/3/serverInfo", "GET,HEAD,OPTIONS"),
        # its slashed spelling answers the route's own
        ("/atlassian/rest/api/3/serverInfo/", "GET,HEAD,OPTIONS"),
        ("/atlassian/rest/api/2/field", "POST,GET,HEAD,OPTIONS"),
        ("/atlassian/rest/api/3/issue/NOPE-1", "PUT,GET,HEAD,DELETE,OPTIONS"),
        ("/atlassian/rest/api/3/issue/NOPE-1/comment", "GET,HEAD,POST,OPTIONS"),
        ("/atlassian/rest/api/2/search/jql", "GET,HEAD,POST,OPTIONS"),
        ("/atlassian/rest/api/3/issueLinkType", "POST,GET,HEAD,OPTIONS"),
        ("/atlassian/rest/api/3/project/NOPE/role", "GET,HEAD,OPTIONS"),
        ("/atlassian/rest/api/3/project/NOPE/role/10002", "DELETE,POST,PUT,GET,HEAD,OPTIONS"),
        # the row that parts from the 405 table (see ``errors.atlassian._JIRA_OPTIONS_ALLOW``)
        ("/atlassian/rest/api/3/project/search", "GET,HEAD,OPTIONS"),
    ],
)
def test_jira_answers_an_options_with_the_vendors_methods(client, admin_h, path, allow):
    """Pins the Jira half of ``backlot.routers.atlassian._options_answer``: 200, an empty
    `text/html` body and `Accept-Patch`, and the vendor's `Allow` for the route."""
    r = client.request("OPTIONS", path, headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == errors_atlassian.JIRA_OPTIONS_MEDIA_TYPE
    assert r.headers["allow"] == allow
    assert r.headers["accept-patch"] == ""
    assert r.content == b""


@pytest.mark.parametrize(
    "path,accept,status,media_type",
    [
        ("/atlassian/wiki/rest/api/space", "*/*", 404, "application/json"),
        ("/atlassian/wiki/rest/api/space/{space}", "*/*", 404, "application/json"),
        ("/atlassian/wiki/rest/api/content", "*/*", 404, "application/json"),
        ("/atlassian/wiki/rest/api/space/NOPESUCH", "*/*", 404, "application/json"),
        # what the other values draw (``errors.atlassian.confluence_options``)
        ("/atlassian/wiki/rest/api/space", None, 404, "application/json"),
        ("/atlassian/wiki/rest/api/space", "*", 404, "application/json"),
        ("/atlassian/wiki/rest/api/space", "application/hal+json", 404, "application/hal+json"),
        ("/atlassian/wiki/rest/api/space", "text/html", 403, errors_atlassian.TOMCAT_MEDIA_TYPE),
        (
            "/atlassian/wiki/rest/api/space",
            "application/xml",
            403,
            errors_atlassian.TOMCAT_MEDIA_TYPE,
        ),
        ("/atlassian/wiki/rest/api/content", "text/plain", 403, errors_atlassian.TOMCAT_MEDIA_TYPE),
        ("/atlassian/wiki/rest/api/space", "foo", 404, None),
        ("/atlassian/wiki/rest/api/space", "application/json,*.*;q=0.9", 404, None),
        ("/atlassian/wiki/rest/api/search", "*/*", 200, "application/xml"),
        ("/atlassian/wiki/rest/api/search", "application/xml", 200, "application/xml"),
        ("/atlassian/wiki/rest/api/search", None, 200, "application/vnd.sun.wadl+xml"),
        ("/atlassian/wiki/rest/api/search", "application/json", 204, None),
        ("/atlassian/wiki/rest/api/search", "text/html", 204, None),
    ],
)
def test_confluence_answers_an_options_by_route_and_accept(
    client, admin_h, keys, path, accept, status, media_type
):
    """Pins the Confluence half of ``backlot.routers.atlassian._options_answer``: on each route but
    `search` what ``errors.atlassian.confluence_options`` answers each `Accept`, and on `search`
    what ``backlot.routers.atlassian._search_options`` answers it, its three methods every time.
    The WADL is real's, with this server's origin in its two URLs."""
    request = client.build_request("OPTIONS", path.format(**keys), headers=admin_h)
    if accept is None:
        del request.headers["accept"]
    else:
        request.headers["accept"] = accept
    r = client.send(request)
    assert r.status_code == status, r.text
    if status == 403:
        assert r.headers["content-type"] == media_type
        assert r.text == errors_atlassian.tomcat_page(403) and len(r.content) == 431
        return
    if status == 404 and media_type is None:
        assert r.content == b"" and "content-type" not in r.headers
        assert r.headers["allow"] == "GET, HEAD, POST, PUT, DELETE, TRACE, OPTIONS, PATCH"
        return
    if status == 404:
        assert r.headers["content-type"] == media_type
        assert json.loads(r.content) == errors_atlassian.CONFLUENCE_OPTIONS_NOT_FOUND
        return
    assert r.headers["allow"] == errors_atlassian.CONFLUENCE_SEARCH_OPTIONS_ALLOW
    if status == 204:
        assert r.content == b"" and "content-type" not in r.headers
        return
    assert r.headers["content-type"] == media_type
    assert r.text.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><application')
    assert 'resources base="http://testserver/wiki/rest/api/"' in r.text
    assert len(r.content) == 3016 - 2 * 31 + 2 * len("http://testserver")


_NO_ENDPOINT_PATHS = [
    "/atlassian/rest/api/3/nopesuchroute",
    "/atlassian/rest/api/2/nopesuchroute",
    "/atlassian/rest/api/3/serverInfo/extra",
    "/atlassian/rest/api/3/issue/NOPE-1/nope",
    "/atlassian/rest/api/4/serverInfo",
    "/atlassian/rest/nope/thing",
]


@pytest.mark.parametrize(
    "method,path,echoed",
    [
        *[
            (method, path, path.removeprefix("/atlassian"))
            for path in _NO_ENDPOINT_PATHS
            for method in ("GET", "POST", "DELETE", "OPTIONS")
        ],
        # and `PUT` (a `PATCH` is the front door's, see `_ANSWERS`)
        ("PUT", "/atlassian/rest/api/3/nopesuchroute", "/rest/api/3/nopesuchroute"),
        # what a refusal echoes: no query string (``errors.atlassian.no_endpoint``), and the slashes
        # ``backlot.main.normalise_the_slashes_in_an_atlassian_path`` describes
        ("GET", "/atlassian/rest/api/3/nopesuchroute?a=1&b=2", "/rest/api/3/nopesuchroute"),
        ("GET", "/atlassian/rest/api/3/nopesuchroute/", "/rest/api/3/nopesuchroute/"),
        ("GET", "/atlassian/rest/api/3//nopesuchroute", "/rest/api/3/nopesuchroute"),
        # an escape ``errors.atlassian.echoed_spelling`` keeps: as sent, hex in upper case, one
        # layer only
        ("GET", "/atlassian/rest/api/3/nope%20x", "/rest/api/3/nope%20x"),
        ("GET", "/atlassian/rest/api/3/nope%3cx", "/rest/api/3/nope%3Cx"),
        ("GET", "/atlassian/rest/api/3/nope%2541", "/rest/api/3/nope%2541"),
        ("GET", "/atlassian/rest/api/3/nope%c3%a9", "/rest/api/3/nope%C3%A9"),
        ("GET", "/atlassian/rest/api/3/nope%3Fx", "/rest/api/3/nope%3Fx"),
        ("GET", "/atlassian/rest/api/3/nope%23x", "/rest/api/3/nope%23x"),
        ("GET", "/atlassian/rest/api/3/nope%09x", "/rest/api/3/nope%09x"),
        ("GET", "/atlassian/rest/api/3/nope%0Dx", "/rest/api/3/nope%0Dx"),
        ("GET", "/atlassian/rest/api/3/nope%0Ax", "/rest/api/3/nope%0Ax"),
        # beside the escaped brackets, which draw a page instead (the bracket test below)
        ("GET", "/atlassian/rest/api/3/nope%5Ex", "/rest/api/3/nope%5Ex"),
        # and ones it decodes
        ("GET", "/atlassian/rest/api/3/nope%2dx", "/rest/api/3/nope-x"),
        ("GET", "/atlassian/rest/api/3/nope%7ex", "/rest/api/3/nope~x"),
        ("GET", "/atlassian/rest/api/3/nope%21x", "/rest/api/3/nope!x"),
        ("GET", "/atlassian/rest/api/3/nope%41x", "/rest/api/3/nopeAx"),
        # an escape beside the slash rules above, and a decoded `%2F` among them
        ("GET", "/atlassian/rest/api/3//nope%20x/", "/rest/api/3/nope%20x/"),
        ("GET", "/atlassian/rest/api/3/nope%2F%2Fx", "/rest/api/3/nope/x"),
    ],
)
def test_jira_answers_a_path_it_mounts_nothing_at_as_rfc_7807(
    client, admin_h, method, path, echoed
):
    """Pins the body ``errors.atlassian.no_endpoint`` describes, on each path and method it
    names."""
    r = client.request(method, path, headers=admin_h)
    assert r.status_code == 404, r.text
    assert r.headers["content-type"] == errors_atlassian.PROBLEM_JSON
    assert r.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": f"No endpoint {method} {echoed}.",
        "instance": echoed,
    }


#: The stub of Jira's `(null)` error page: its title and the one line real's reads.
_JIRA_NULL_PAGE = (
    "<!DOCTYPE html><html><head><title>(null)</title></head><body>"
    '<p>Encountered a "null - null" error while loading this page.</p></body></html>'
)


@pytest.mark.parametrize("accept", ["application/json", "*/*"])
@pytest.mark.parametrize(
    "vendor_path,status,page",
    [
        # the dead-link page, the one the site answers `/foo` with
        ("/rest/api/3/nope%5Bx", 404, "/foo"),
        ("/rest/api/3/nope%5Dx", 404, "/foo"),
        ("/rest/api/3/issue/A%5BB/comment?maxResults=abc", 400, _JIRA_NULL_PAGE),
        ("/rest/api/3/issue/A%5DB/comment?maxResults=abc", 400, _JIRA_NULL_PAGE),
        # in lower case, and bare
        ("/rest/api/3/nope%5bx", 404, "/foo"),
        ("/rest/api/3/nope[x", 404, "/foo"),
        ("/rest/api/3/issue/A[B/comment?maxResults=abc", 400, _JIRA_NULL_PAGE),
        # the issue's own 404 is not one of the two
        ("/rest/api/3/issue/A%5BB", 404, None),
        ("/rest/api/3/issue/A%5DB", 404, None),
    ],
)
def test_jira_answers_a_bracket_with_a_page_of_its_web_app(
    client, admin_h, vendor_path, status, page, accept
):
    """Pins the two refusals ``errors.atlassian._JIRA_BRACKET`` names, under both `Accept` values
    measured."""
    r = client.get(f"/atlassian{vendor_path}", headers={**admin_h, "Accept": accept})
    assert r.status_code == status, r.text
    if page is None:
        assert r.headers["content-type"] == "application/json;charset=UTF-8"
        assert sorted(r.json()) == ["errorMessages", "errors"]
        return
    if page == "/foo":
        page = client.get("/atlassian/foo", headers=admin_h).text
    assert r.headers["content-type"] == "text/html;charset=utf-8"
    assert r.text == page


_CREDENTIALS = {
    "none": {},
    "failed-pair": FAILED_PAIR,
    "unknown-scheme": {"Authorization": "Bogus xyz"},
    "unreadable-bearer": {"Authorization": "Bearer nope"},
}


@pytest.mark.parametrize(
    "method,path,credential,status",
    [
        # an operation Jira publishes and no route here serves, which will not run anonymously
        ("GET", "/atlassian/rest/api/3/myself", "none", 401),
        ("GET", "/atlassian/rest/api/3/myself", "failed-pair", 401),
        ("GET", "/atlassian/rest/api/3/myself", "unknown-scheme", 401),
        ("DELETE", "/atlassian/rest/api/3/screens/999999999", "none", 401),
        ("GET", "/atlassian/rest/api/2/policies/project/NOPE/rules", "none", 401),
        # the controls: one that runs anonymously on real, where Backlot has no operation to run,
        # and the same refused path with a credential, both the baseline's gap; and a bearer it
        # cannot read on a GET, which is the Connect-token 403 before any of this
        ("GET", "/atlassian/rest/api/3/dashboard", "none", 404),
        ("GET", "/atlassian/rest/api/3/myself", "admin", 404),
        ("GET", "/atlassian/rest/api/2/policies/project/NOPE/rules", "admin", 404),
        ("GET", "/atlassian/rest/api/3/myself", "unreadable-bearer", 403),
        # an `OPTIONS` on a path Jira publishes something at, served here or not
        ("OPTIONS", "/atlassian/rest/api/3/serverInfo", "none", 401),
        ("OPTIONS", "/atlassian/rest/api/3/serverInfo", "failed-pair", 401),
        ("OPTIONS", "/atlassian/rest/api/3/serverInfo", "unknown-scheme", 401),
        ("OPTIONS", "/atlassian/rest/api/3/serverInfo", "unreadable-bearer", 401),
        ("OPTIONS", "/atlassian/rest/api/3/issue/NOPE-1", "none", 401),
        ("OPTIONS", "/atlassian/rest/api/3/dashboard", "none", 401),
        ("OPTIONS", "/atlassian/rest/api/3/serverInfo", "admin", 200),
        # and on a path it publishes nothing at, the URL's 404 whatever the credential, a decoded
        # newline, tab or `#` in a published path's spelling included
        ("OPTIONS", "/atlassian/rest/api/3/nopesuchroute", "none", 404),
        ("OPTIONS", "/atlassian/rest/api/3/nopesuchroute", "unreadable-bearer", 404),
        ("GET", "/atlassian/rest/api/3/myse%0Alf", "none", 404),
        ("GET", "/atlassian/rest/api/3/my%09self", "none", 404),
        ("GET", "/atlassian/rest/api/3/myself%23x", "none", 404),
    ],
)
def test_jira_refuses_what_it_will_not_run_for_a_caller_it_cannot_name(
    client, admin_h, method, path, credential, status
):
    """Pins the 401 the comment on ``errors.atlassian.JIRA_UNAUTHENTICATED`` describes, in the
    `text/html` it has for the test client's `Accept: */*`: on the operations
    ``backlot/data/jira_unserved.json`` refuses and on an `OPTIONS` wherever Jira publishes
    something, for each credential that comment names. Where Jira publishes nothing, the URL's 404
    answers first."""
    headers = admin_h if credential == "admin" else _CREDENTIALS[credential]
    r = client.request(method, path, headers=headers)
    assert r.status_code == status, r.text
    if status == 404:
        # the URL's 404, naming the path as sent (``errors.atlassian.no_endpoint``)
        echoed = path.removeprefix("/atlassian")
        assert r.json()["detail"] == f"No endpoint {method} {echoed}."
        assert r.json()["instance"] == echoed
    if status != 401:
        assert "www-authenticate" not in r.headers
        return
    assert r.content == b"Client must be authenticated to access this resource."
    assert r.headers["content-type"] == "text/html;charset=UTF-8"
    assert r.headers["www-authenticate"] == 'OAuth realm="http%3A%2F%2Ftestserver"'
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert r.headers["cache-control"] == "no-cache, no-store, no-transform"
    for name in ("x-arequestid", "atl-request-id", "timing-allow-origin", "x-xss-protection"):
        assert name in r.headers, name
    assert "x-aaccountid" not in r.headers
    if credential == "failed-pair":
        assert r.headers["x-seraph-loginreason"] == "AUTHENTICATED_FAILED"


_BANNER = "/atlassian/rest/api/3/announcementBanner"
_PLAN = "/atlassian/rest/api/3/plans/plan/1"
_UI = "/atlassian/rest/api/3/uiModifications/1"
_REMOVE_TEMPLATE = "/atlassian/rest/api/3/project-template/remove-template"
_SERVICE_REGISTRY = "/atlassian/rest/atlassian-connect/1/service-registry?serviceIds=1"
_COLUMNS = "/atlassian/rest/api/2/settings/columns"
_FILTER_COLUMNS = "/atlassian/rest/api/3/filter/999/columns"
_FIELD = "/atlassian/rest/api/3/field"
_ATTACHMENTS = "/atlassian/rest/api/3/issue/NOPE-999999/attachments"
_AVATAR = "/atlassian/rest/api/3/issuetype/999/avatar2"
_PREFERENCES = "/atlassian/rest/api/3/mypreferences"
_JSON_OR_TEXT = "application/json, text/plain"
_AGENTS = "/atlassian/rest/api/3/agents/bulk/invoke/batch"
_POLICIES = "/atlassian/rest/api/3/policies/project/NOPE/policies"
#: a `Content-Type` Spring cannot read, which the 415 names no type for
_UNREAD = object()
_JSON = "application/json"


@pytest.mark.parametrize(
    "method,path,credential,content_type,body,status,named,accept",
    [
        # an operation no route serves, which takes JSON and needs a body
        ("PUT", _BANNER, "none", None, None, 415, "null", _JSON),
        ("PUT", _BANNER, "none", "", "{}", 415, "null", _JSON),
        (
            "PUT",
            _BANNER,
            "none",
            "TEXT/Plain;Charset=UTF-8;X=Y",
            "x",
            415,
            "text/plain;Charset=UTF-8;X=Y",
            _JSON,
        ),
        ("PUT", _BANNER, "none", "application/*", "{}", 415, "application/*", _JSON),
        ("PUT", _BANNER, "none", "*", "{}", 415, "*/*", _JSON),
        ("PUT", _BANNER, "none", "application/json;charset=nope", "{}", 415, _UNREAD, _JSON),
        ("PUT", _BANNER, "none", "application/json;a=b c", "{}", 415, _UNREAD, _JSON),
        ("PUT", _BANNER, "none", "text/plain", "", 415, "text/plain", _JSON),
        ("PUT", f"{_BANNER}/", "none", "text/plain", "x", 415, "text/plain", _JSON),
        ("PUT", f"{_BANNER}/", "none", _JSON, "{}", 401, None, None),
        ("PUT", _BANNER, "none", _JSON, "{}", 401, None, None),
        ("PUT", _BANNER, "none", _JSON, "", 401, None, None),
        ("PUT", _BANNER, "none", "application/json;charset=latin1", "{}", 401, None, None),
        ("PUT", _BANNER, "none", 'application/json;charset="utf-8"', "{}", 401, None, None),
        ("PUT", _BANNER, "none", "application/json;x", "{}", 401, None, None),
        # a charset Java does not name is no charset, whatever its case, and one it names is read
        ("PUT", _BANNER, "none", "application/json;charset=u8", "{}", 415, _UNREAD, _JSON),
        ("PUT", _BANNER, "none", "application/json;Charset=u8", "{}", 415, _UNREAD, _JSON),
        ("PUT", _BANNER, "none", "application/json;charset=x-MacRoman", "{}", 401, None, None),
        # the same check on the bulk agent invocation, a POST
        ("POST", _AGENTS, "none", "text/plain", "x", 415, "text/plain", _JSON),
        ("POST", _AGENTS, "admin", "text/plain", "x", 415, "text/plain", _JSON),
        ("POST", _AGENTS, "none", None, None, 415, "null", _JSON),
        ("POST", _AGENTS, "none", _JSON, "{}", 401, None, None),
        ("POST", _AGENTS, "admin", _JSON, "{}", 404, None, None),
        # an `OPTIONS` is not checked, and an unreadable bearer is refused before the check
        ("OPTIONS", _BANNER, "none", "text/plain", "x", 401, None, None),
        ("PUT", _BANNER, "unreadable-bearer", "text/plain", "x", 403, None, None),
        # one that takes JSON Patch, which refuses JSON whatever the credential
        ("PUT", _PLAN, "none", _JSON, "{}", 415, _JSON, "application/json-patch+json"),
        ("PUT", _PLAN, "admin", _JSON, "{}", 415, _JSON, "application/json-patch+json"),
        ("PUT", _PLAN, "failed-pair", _JSON, "{}", 415, _JSON, "application/json-patch+json"),
        ("PUT", _PLAN, "unknown-scheme", _JSON, "{}", 415, _JSON, "application/json-patch+json"),
        ("PUT", _PLAN, "none", "application/json-patch+json", "[]", 401, None, None),
        ("PUT", _PLAN, "none", "APPLICATION/JSON-PATCH+JSON; Charset=UTF-8", "[]", 401, None, None),
        ("PUT", _PLAN, "admin", "application/json-patch+json", "[]", 404, None, None),
        # one whose body is optional, which checks only a request that carries one
        ("PUT", _UI, "none", None, None, 401, None, None),
        ("PUT", _UI, "none", "", None, 401, None, None),
        ("PUT", _UI, "none", "text/plain", "", 401, None, None),
        ("PUT", _UI, "none", None, "{}", 415, "null", _JSON),
        ("PUT", _UI, "none", "text/plain", "x", 415, "text/plain", _JSON),
        # one documented with a body that checks nothing, and one documented with none
        ("POST", "/atlassian/rest/api/3/plans/plan", "none", "foo", "x", 401, None, None),
        (
            "DELETE",
            "/atlassian/rest/api/3/plans/plan/1/team/atlassian/2",
            "none",
            "foo",
            "x",
            401,
            None,
            None,
        ),
        # one documented with a JSON body that checks the type only behind the 401
        ("PUT", _POLICIES, "none", "text/plain", "x", 401, None, None),
        ("PUT", _POLICIES, "none", "foo", "x", 401, None, None),
        ("PUT", _POLICIES, "none", None, None, 401, None, None),
        # two the documents give no body that check for JSON all the same, a GET among them
        ("DELETE", _REMOVE_TEMPLATE, "none", None, None, 415, "null", _JSON),
        ("DELETE", _REMOVE_TEMPLATE, "none", _JSON, "{}", 401, None, None),
        ("GET", _SERVICE_REGISTRY, "none", None, None, 415, "null", _JSON),
        ("GET", _SERVICE_REGISTRY, "none", _JSON, None, 401, None, None),
        # one that takes `*/*`, which refuses only what does not parse, and one that takes two
        ("PUT", _COLUMNS, "none", None, None, 401, None, None),
        ("PUT", _COLUMNS, "none", "image/png", "x", 401, None, None),
        ("PUT", _COLUMNS, "none", "foo", "x", 415, _UNREAD, "multipart/form-data, */*"),
        (
            "PUT",
            _PREFERENCES,
            "none",
            "application/xml",
            "x",
            415,
            "application/xml",
            "application/json, text/plain",
        ),
        ("PUT", _PREFERENCES, "none", "text/plain", "x", 401, None, None),
        # the parameters kept (``errors.atlassian._spring_media_type``)
        *(
            ("PUT", _PREFERENCES, "none", content_type, "x", status, named, accept)
            for content_type, status, named, accept in (
                ("application/xml;x=1;X=2", 415, "application/xml;X=2", _JSON_OR_TEXT),
                ("application/xml;q=1;Q=2", 415, "application/xml;Q=2", _JSON_OR_TEXT),
                (
                    "application/xml;charset=bogus;Charset=utf-8",
                    415,
                    "application/xml;Charset=utf-8",
                )
                + (_JSON_OR_TEXT,),
                ("application/json;charset=nope;charset=utf-8", 401, None, None),
                ("application/json;Charset=nope;charset=utf-8", 401, None, None),
                ("application/json;charset=utf-8;Charset=nope", 415, _UNREAD, _JSON_OR_TEXT),
                ("application/json;q=2", 415, _UNREAD, _JSON_OR_TEXT),
                ("application/json\xa0", 415, _UNREAD, _JSON_OR_TEXT),
            )
        ),
        # one whose 415 adds a charset the request did not send, and keeps one it did
        # (`DOCUMENTED_OTHERWISE` in ``scripts/gen_jira_unserved.py``)
        ("PUT", _FILTER_COLUMNS, "none", "text/plain", "x", 415)
        + ("text/plain;charset=UTF-8", "application/json, application/yaml, application/*+json"),
        ("PUT", _FILTER_COLUMNS, "none", "text/plain;charset=ISO-8859-1", "x", 415)
        + (
            "text/plain;charset=ISO-8859-1",
            "application/json, application/yaml, application/*+json",
        ),
        # its body is optional, so one with none is not checked
        ("PUT", _FILTER_COLUMNS, "none", None, None, 404, None, None),
        # one that takes JSON alone, one multipart alone, and one `*/*`
        ("POST", _FIELD, "none", "text/plain", "x", 415, "text/plain", _JSON),
        ("POST", _FIELD, "none", _JSON, "{}", 405, None, None),
        ("POST", _ATTACHMENTS, "none", "text/plain", "x", 415, "text/plain", "multipart/form-data"),
        ("POST", _ATTACHMENTS, "none", "multipart/form-data; boundary=x", "--x--", 404, None, None),
        ("POST", _AVATAR, "none", "foo", "x", 415, _UNREAD, "*/*"),
        ("POST", _AVATAR, "none", "image/png", "x", 404, None, None),
    ],
)
def test_jira_checks_the_media_type_an_operation_takes_before_anything_else(
    client, admin_h, method, path, credential, content_type, body, status, named, accept
):
    """Pins the 415 ``errors.atlassian.refuse_a_media_type`` describes, on the operations
    ``backlot/data/jira_unserved.json`` says check one. Each row's control is the same URL
    answering past the check: the 401 a caller with no credential gets, the 404 an operation no
    route serves gives a caller whose credential resolves, or, on one real runs for this caller,
    what no route here answers it: the 405 of a method a route at the path does not take, or the
    404 of a path none serves."""
    headers = dict(admin_h if credential == "admin" else _CREDENTIALS[credential])
    if content_type is not None:
        headers["Content-Type"] = content_type.encode("latin-1")
    r = _send_as_latin1(client, client.build_request(method, path, headers=headers, content=body))
    assert r.status_code == status, r.text
    if status != 415:
        assert "accept" not in r.headers
        return
    assert r.headers["content-type"] == errors_atlassian.PROBLEM_JSON
    assert r.headers["accept"] == accept
    assert r.json() == {
        "type": "about:blank",
        "title": "Unsupported Media Type",
        "status": 415,
        "detail": (
            "Could not parse Content-Type."
            if named is _UNREAD
            else f"Content-Type '{named}' is not supported."
        ),
        "instance": path.split("?")[0].removeprefix("/atlassian"),
    }


def test_jira_unserved_agrees_with_the_baseline_and_the_script(monkeypatch):
    """``backlot/data/jira_unserved.json`` sorts each `missing` operation of the Jira baseline
    into `refused` or `run` and holds nothing else, so one the file has lost, or one the baseline
    has gained or dropped since the file was written, fails here: the reminder to rerun
    ``scripts/gen_jira_unserved.py``. A route added for one of them is `implemented` in the baseline
    and the file would otherwise refuse a served operation's path for a method no route takes. Which
    list an operation belongs in is read off Jira's documents, which that script's `--check`
    compares on the schedule in `.github/workflows/fidelity.yml`. An entry's accept, produces and
    charset, which `ACCEPT_UNCHECKED`, `PRODUCES` and `DOCUMENTED_OTHERWISE` set whatever the
    documents say, are compared as well, so a name added to or dropped from one of those three
    without a rerun fails here too."""
    import runpy

    import backlot
    from backlot.fidelity.comparisons import baseline_path

    entries = json.loads(baseline_path("jira").read_text(encoding="utf-8"))["operations"]
    unserved = [e["operation"] for e in entries if e["status"] == "missing"]
    table = Path(backlot.__file__).resolve().parent / "data" / "jira_unserved.json"
    content = json.loads(table.read_text(encoding="utf-8"))
    assert sorted([*content["refused"], *content["run"]]) == sorted(unserved)
    # And the script reads the same operations off the baseline: given documents declaring
    # nothing, it refuses every one of them.
    monkeypatch.setattr("backlot.fidelity.fetch.fetch_json", lambda url, **kw: {"paths": {}})
    script = runpy.run_path(str(table.parents[2] / "scripts" / "gen_jira_unserved.py"))
    built = script["build"]()["refused"]
    assert sorted(built) == sorted(unserved)
    held = {**content["refused"], **content["run"]}
    for field in ("accept", "produces", "charset"):
        written = {op: entry[field] for op, entry in built.items() if field in entry}
        assert written == {op: entry[field] for op, entry in held.items() if field in entry}, field


@pytest.mark.parametrize(
    "path,accept,shape,echoed",
    [
        ("/atlassian/wiki/rest/api/nopesuchroute?a=1", "application/json", "json", "?a=1"),
        ("/atlassian/wiki/rest/api/nopesuchroute?a=1", "*/*", "xml", "?a=1"),
        ("/atlassian/wiki/rest/api/nopesuchroute?a=1", "application/xml", "xml", "?a=1"),
        (
            "/atlassian/wiki/rest/api/nopesuchroute?a=1",
            "application/xml;q=0.5;x=1, application/json",
            "json",
            "?a=1",
        ),
        ("/atlassian/wiki/rest/api/nopesuchroute?a=1", None, "xml", "?a=1"),
        # a trailing slash is kept and an interior run collapsed, as in Jira's `detail`
        ("/atlassian/wiki/rest/api/nopesuchroute/", "application/json", "json", "nopesuchroute/"),
        (
            "/atlassian/wiki/rest/api//nopesuchroute",
            "application/json",
            "json",
            "/api/nopesuchroute",
        ),
        # what ``errors.atlassian.jaxrs_not_found`` escapes in the XML; the JSON escapes neither
        ("/atlassian/wiki/rest/api/nope&x'quote", None, "xml", "nope&amp;x'quote"),
        ("/atlassian/wiki/rest/api/nope&x'quote", "application/json", "json", "nope&x'quote"),
        # the name is read whatever its case
        ("/atlassian/wiki/rest/api/nopesuchroute", "APPLICATION/JSON", "json", "nopesuchroute"),
        # by `q`, `q=0` among them, then the more specific, then the first listed, XML before JSON
        *(
            ("/atlassian/wiki/rest/api/nopesuchroute", accept, shape, "nopesuchroute")
            for accept, shape in (
                ("application/json;q=0", "json"),
                ("application/xml;q=0, application/json", "json"),
                ("application/json;q=0.5, application/xml", "xml"),
                ("application/xml, application/json", "xml"),
                ("APPLICATION/XML, application/json", "xml"),
                ("application/*, application/json", "json"),
                ("application/*", "xml"),
                ("text/xml, application/json", "json"),
                ("foo, application/json", "json"),
                ("application/jsonx", "xml"),
                ("application/json-patch+json", "xml"),
                ("application/*, application/json;q=0.5", "xml"),
                ("*/*, application/json;q=0.5", "xml"),
                ("*/json", "xml"),
                # a `Q` Spring would fail to copy, where Jersey reads the last `q`
                ("application/json;q=0.5;Q=2;q=0.5", "json"),
                ("*/*;q=0.5;Q=2;q=0.5", "xml"),
            )
        ),
        # the escapes ``errors.atlassian.echoed_spelling`` keeps and decodes, in `null for uri:`,
        # by Confluence's own set
        ("/atlassian/wiki/rest/api/nope%20x", "application/json", "json", "nope%20x"),
        ("/atlassian/wiki/rest/api/nope%21x", "application/json", "json", "nope%21x"),
        ("/atlassian/wiki/rest/api/nope%3cx", "application/json", "json", "nope%3cx"),
        ("/atlassian/wiki/rest/api/nope%3Fx", "application/json", "json", "nope%3Fx"),
        ("/atlassian/wiki/rest/api/nope%23x", "application/json", "json", "nope%23x"),
        ("/atlassian/wiki/rest/api/nope%2Dx", "application/json", "json", "nope-x"),
        ("/atlassian/wiki/rest/api/nope%0Ax", "application/json", "json", "nope%0Ax"),
        ("/atlassian/wiki/rest/api/nope%0Ax", "*/*", "xml", "nope%0Ax"),
        # an escaped `?` beside a real query: the query is the one sent, after the path's escape
        ("/atlassian/wiki/rest/api/nope%3Fx?_=1", "application/json", "json", "nope%3Fx?_=1"),
    ],
)
def test_confluence_answers_an_unclaimed_segment_in_the_shape_accept_asks_for(
    client, admin_h, path, accept, shape, echoed
):
    """Pins the JAX-RS 404 the comment on ``errors.atlassian.JAXRS_XML_MEDIA_TYPE`` describes,
    `Accept` by `Accept`."""
    headers = admin_h if accept is None else {**admin_h, "Accept": accept}
    r = client.get(path, headers=headers)
    assert r.status_code == 404, r.text
    assert r.headers["cache-control"] == "no-transform"
    if shape == "json":
        assert r.headers["content-type"] == errors_atlassian.JAXRS_JSON_MEDIA_TYPE
        assert r.json()["status-code"] == 404
        assert r.json()["message"].startswith("null for uri: http")
        assert r.json()["message"].endswith(echoed)
    else:
        assert r.headers["content-type"] == errors_atlassian.JAXRS_XML_MEDIA_TYPE
        assert r.text.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><status>')
        assert "<status-code>404</status-code>" in r.text
        assert f"{echoed}</message>" in r.text


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def _spelled(client, method):
    """A client whose requests reach the app under ``method`` as spelled, which the test client's
    transport would upper-case. No `with`: a second lifespan would overwrite the state `client`
    started."""
    from starlette.testclient import TestClient

    async def app(scope, receive, send):
        await client.app({**scope, "method": method}, receive, send)

    return TestClient(app)


_ANSWERS = [
    # (label, method, path, and for a method refused in front of the application its status and
    # which layer refused it: the CDN, which adds nothing to its 405 and its 400 and the edge's two
    # headers to its 403, or the gateway, which adds its two ids beside those)
    ("jira 200", "GET", "/atlassian/rest/api/3/serverInfo", None),
    ("jira 404", "GET", "/atlassian/rest/api/3/nopesuchroute", None),
    ("jira 405", "POST", "/atlassian/rest/api/3/serverInfo", None),
    ("jira options", "OPTIONS", "/atlassian/rest/api/3/serverInfo", None),
    ("confluence 200", "GET", "/atlassian/wiki/rest/api/space", None),
    ("confluence 404", "GET", "/atlassian/wiki/rest/api/space/NOPESUCH", None),
    ("confluence options", "OPTIONS", "/atlassian/wiki/rest/api/space", None),
    ("jira trace", "TRACE", "/atlassian/rest/api/3/serverInfo", (405, "cdn")),
    ("jira trace, unserved", "TRACE", "/atlassian/rest/api/3/nopesuchroute", (405, "cdn")),
    ("confluence trace", "TRACE", "/atlassian/wiki/rest/api/space", (405, "cdn")),
    ("jira connect", "CONNECT", "/atlassian/rest/api/3/serverInfo", (405, "cdn")),
    (
        "confluence connect, unserved",
        "CONNECT",
        "/atlassian/wiki/rest/api/nopesuchroute",
        (405, "cdn"),
    ),
    ("jira propfind", "PROPFIND", "/atlassian/rest/api/3/serverInfo", (403, "cdn edge")),
    ("jira link, unserved", "LINK", "/atlassian/rest/api/3/nopesuchroute", (403, "cdn edge")),
    ("confluence search", "SEARCH", "/atlassian/wiki/rest/api/space", (403, "cdn edge")),
    ("jira hyphen", "M-SEARCH", "/atlassian/rest/api/3/serverInfo", (403, "cdn edge")),
    ("confluence underscore", "FOO_BAR", "/atlassian/wiki/rest/api/space", (403, "cdn edge")),
    ("confluence nine letters", "ABCDEFGHI", "/atlassian/wiki/rest/api/space", (403, "cdn edge")),
    ("jira ten letters", "ABCDEFGHIJ", "/atlassian/rest/api/3/serverInfo", (400, "cdn")),
    ("confluence ten, registered", "MKACTIVITY", "/atlassian/wiki/rest/api/space", (400, "cdn")),
    ("jira lower case", "get", "/atlassian/rest/api/3/serverInfo", (400, "cdn")),
    (
        "confluence mixed case, unserved",
        "Get",
        "/atlassian/wiki/rest/api/nopesuchroute",
        (400, "cdn"),
    ),
    ("jira digit, unserved", "FOO1", "/atlassian/rest/api/3/nopesuchroute", (400, "cdn")),
    ("confluence full stop", "G.T", "/atlassian/wiki/rest/api/space", (400, "cdn")),
    ("jira patch", "PATCH", "/atlassian/rest/api/3/serverInfo", (400, "gateway")),
    ("jira patch, unserved", "PATCH", "/atlassian/rest/api/3/nopesuchroute", (400, "gateway")),
    ("confluence patch", "PATCH", "/atlassian/wiki/rest/api/space", (405, "gateway")),
    (
        "confluence patch, unserved",
        "PATCH",
        "/atlassian/wiki/rest/api/nopesuchroute",
        (405, "gateway"),
    ),
]


def test_atlassian_headers_ride_every_answer_but_the_front_doors(client, admin_h):
    """Pins the headers ``backlot.routers.atlassian.vendor_headers`` puts on every answer the
    application gives: the ids ``backlot.routers.atlassian.request_ids`` derives,
    ``backlot.routers.atlassian._EDGE``, and Jira's `timing-allow-origin` or Confluence's
    millisecond clock.

    Real mints a new value per response; this one is derived from the request, the divergence
    ``backlot.routers.atlassian.request_ids`` states and this pins — a corpus served twice answers
    the same id, and two different requests do not share one.

    A method the CDN (`server: CloudFront`) or the gateway behind it (`server: AtlassianEdge`)
    refuses before the application gets that layer's status and headers and no `Allow`, measured
    at ``errors.atlassian.SERVED_METHODS``; each such row names the layer. Those methods are left
    off the catch-all's methods for that reason, and their rows hold that what answers one here
    advertises nothing: Starlette would otherwise name the methods the catch-all takes.
    """
    answers = {}
    for label, method, path, refused in _ANSWERS:
        r = _spelled(client, method).request("GET", path, headers=admin_h)
        if refused is not None:
            status, layer = refused
            assert r.status_code == status, (label, r.text)
            assert "allow" not in r.headers, label
            for name in ("atl-request-id", "atl-traceid"):
                assert (name in r.headers) == (layer == "gateway"), (label, name)
            for name in ("x-content-type-options", "x-xss-protection"):
                assert (name in r.headers) == (layer != "cdn"), (label, name)
            for absent in (
                "x-arequestid",
                "cache-control",
                "timing-allow-origin",
                "x-ratelimit-limit",
                "deprecation",
                "x-confluence-request-time",
            ):
                assert absent not in r.headers, (label, absent)
            continue
        answers[label] = r
        request_id = r.headers["atl-request-id"]
        assert _UUID.fullmatch(request_id), (label, request_id)
        assert r.headers["atl-traceid"] == request_id.replace("-", ""), label
        assert r.headers["x-content-type-options"] == "nosniff", label
        assert r.headers["x-xss-protection"] == "1; mode=block", label
        if label.startswith("jira"):
            assert re.fullmatch(r"[0-9a-f]{32}", r.headers["x-arequestid"]), label
            assert r.headers["timing-allow-origin"] == "*", label
            assert "x-confluence-request-time" not in r.headers, label
        else:
            assert "x-arequestid" not in r.headers, label
            assert "timing-allow-origin" not in r.headers, label
            assert re.fullmatch(r"\d{13}", r.headers["x-confluence-request-time"]), label
    again = client.get("/atlassian/rest/api/3/serverInfo", headers=admin_h)
    assert again.headers["atl-request-id"] == answers["jira 200"].headers["atl-request-id"]
    assert (
        answers["jira 200"].headers["atl-request-id"]
        != answers["jira 404"].headers["atl-request-id"]
    )


@pytest.fixture
def frozen_burst(client):
    """The burst windows on a clock that does not move, so counting is the only thing that does."""
    from backlot.routers.atlassian import JiraBurstWindows

    app = client.app
    before = getattr(app.state, "jira_burst_windows", None)
    app.state.jira_burst_windows = JiraBurstWindows(clock=lambda: 1_000.0)
    yield
    app.state.jira_burst_windows = before


_UNMETERED = 1000000000000
_JQL = "/atlassian/rest/api/3/search/jql"


@pytest.mark.parametrize(
    "method,path,policy,limit",
    [
        ("GET", "/atlassian/rest/api/3/serverInfo", 100, 350),
        ("GET", "/atlassian/rest/api/3/project/search", 100, 350),
        ("GET", "/atlassian/rest/api/3/issue/NOPE-1", 150, 400),
        ("GET", "/atlassian/rest/api/3/project/ENG/role/10002", 200, 500),
        ("POST", _JQL, 100, 200),
        ("HEAD", "/atlassian/rest/api/3/serverInfo", _UNMETERED, _UNMETERED),
        ("HEAD", "/atlassian/rest/api/3/issue/NOPE-1", _UNMETERED, _UNMETERED),
        ("OPTIONS", "/atlassian/rest/api/3/serverInfo", _UNMETERED, _UNMETERED),
        ("OPTIONS", "/atlassian/rest/api/3/issue/NOPE-1", _UNMETERED, _UNMETERED),
    ],
)
def test_jira_reports_the_burst_quota_of_the_route(
    client, admin_h, frozen_burst, method, path, policy, limit
):
    """Real's quota is per method and route (``backlot.routers.atlassian._JIRA_BURST_BUCKETS``,
    where the measurement is), and `remaining` counts down inside the window the policy names."""
    body = {"jql": "project = ENG"} if method == "POST" else None
    first = client.request(method, path, headers=admin_h, json=body)
    assert first.headers["x-ratelimit-limit"] == str(limit)
    assert first.headers["x-ratelimit-remaining"] == str(limit - 1)
    assert first.headers["ratelimit"] == f'"jira-burst-based";r={limit - 1};t=1'
    assert first.headers["ratelimit-policy"] == f'"jira-burst-based";q={policy};w=1'
    again = client.request(method, path, headers=admin_h, json=body)
    assert again.headers["x-ratelimit-remaining"] == str(limit - 2)


@pytest.mark.parametrize(
    "before,last,remaining",
    [
        # a window is one method on one route template
        # (``backlot.routers.atlassian.JiraBurstWindows``)
        pytest.param(
            ("GET", "/rest/api/3/serverInfo"), ("GET", "/rest/api/3/field"), 349, id="two-routes"
        ),
        pytest.param(
            ("GET", "/rest/api/3/serverInfo"),
            ("GET", "/rest/api/2/serverInfo"),
            348,
            id="two-mounts",
        ),
        pytest.param(
            ("GET", "/rest/api/3/issue/NOPE-1"),
            ("GET", "/rest/api/3/issue/NOPE-2"),
            398,
            id="two-keys",
        ),
        pytest.param(
            ("GET", "/rest/api/3/search/jql"),
            ("POST", "/rest/api/3/search/jql"),
            199,
            id="two-methods",
        ),
        pytest.param(
            ("HEAD", "/rest/api/3/serverInfo"),
            ("GET", "/rest/api/3/serverInfo"),
            349,
            id="head-then-get",
        ),
        pytest.param(
            ("OPTIONS", "/rest/api/3/serverInfo"),
            ("GET", "/rest/api/3/serverInfo"),
            349,
            id="options-then-get",
        ),
        pytest.param(
            ("HEAD", "/rest/api/3/serverInfo"),
            ("HEAD", "/rest/api/2/serverInfo"),
            _UNMETERED - 2,
            id="head-two-mounts",
        ),
        pytest.param(
            ("HEAD", "/rest/api/3/serverInfo"),
            ("OPTIONS", "/rest/api/3/serverInfo"),
            _UNMETERED - 1,
            id="head-then-options",
        ),
    ],
)
def test_jira_counts_a_burst_per_method_and_route(
    client, admin_h, frozen_burst, before, last, remaining
):
    """What one request leaves of the next one's window: ``remaining`` is what ``last`` reads after
    ``before``, both sent with one credential in the same second."""
    for method, path in (before, last):
        body = {"jql": "project = ENG"} if method == "POST" else None
        r = client.request(method, f"/atlassian{path}", headers=admin_h, json=body)
    assert r.headers["x-ratelimit-remaining"] == str(remaining)


@pytest.mark.parametrize(
    "credential,method,path,status,quota",
    [
        *[
            pytest.param(
                p.values[0], "GET", "/atlassian/rest/api/3/serverInfo", 200, False, id=p.id
            )
            for p in UNRESOLVABLE
        ],
        # where no route answers, the caller is named and nothing is counted
        # (``backlot.routers.atlassian.vendor_headers``)
        pytest.param(
            "admin",
            "GET",
            "/atlassian/rest/api/3/nopesuchroute",
            404,
            False,
            id="admin-no-endpoint",
        ),
        pytest.param(
            "admin", "HEAD", "/atlassian/rest/api/3/nopesuchroute", 404, False, id="admin-head-404"
        ),
        pytest.param(
            "admin",
            "OPTIONS",
            "/atlassian/rest/api/3/nopesuchroute",
            404,
            False,
            id="admin-options-404",
        ),
        pytest.param(
            "admin", "POST", "/atlassian/rest/api/3/serverInfo", 405, False, id="admin-405"
        ),
        pytest.param(
            "admin", "DELETE", "/atlassian/rest/api/3/serverInfo", 405, False, id="admin-delete-405"
        ),
        pytest.param("admin", "PUT", _PLAN, 415, False, id="admin-unserved-415"),
        pytest.param({}, "PUT", _PLAN, 415, False, id="anonymous-unserved-415"),
        pytest.param(
            "admin", "HEAD", "/atlassian/rest/api/3/serverInfo", 200, True, id="admin-head"
        ),
        pytest.param("user", "GET", "/atlassian/rest/api/3/serverInfo", 200, True, id="user"),
    ],
)
def test_jira_names_the_caller_and_counts_its_quota_once_a_credential_resolves(
    client, admin_h, tokens, credential, method, path, status, quota
):
    """Pins what ``backlot.routers.atlassian.vendor_headers`` puts on a Jira answer by caller: the
    two ids, `x-arequestid` and `cache-control` for everyone, and for a credential that resolves the
    account id the corpus serves that user under (``synth.atlassian_account_id``), with the quota
    four where a route answered. The admin token, which has no address, gets the one seeded from
    `"unknown"`."""
    from backlot import synth

    if credential == "admin":
        headers, account = admin_h, synth.atlassian_account_id("unknown")
    elif credential == "user":
        email, token = sorted(tokens.items())[0]
        headers = {"Authorization": f"Bearer {token}"}
        account = synth.atlassian_account_id(email)
    else:
        headers, account = credential, None
    r = client.request(method, path, headers=headers)
    assert r.status_code == status
    assert r.headers["atl-request-id"] and r.headers["x-arequestid"]
    assert r.headers["cache-control"] == "no-cache, no-store, no-transform"
    assert r.headers.get("x-aaccountid") == account
    for name in ("ratelimit", "ratelimit-policy", "x-ratelimit-limit", "x-ratelimit-remaining"):
        assert (name in r.headers) == quota, name


@pytest.mark.parametrize(
    "method,path,anonymous,carries",
    [
        ("GET", "/atlassian/wiki/rest/api/space", False, True),
        ("GET", "/atlassian/wiki/rest/api/space/{space}", False, True),
        ("GET", "/atlassian/wiki/rest/api/content", False, True),
        ("GET", "/atlassian/wiki/rest/api/content/{content}/child/page", False, True),
        ("GET", "/atlassian/wiki/rest/api/content/{content}/label", False, True),
        ("GET", "/atlassian/wiki/rest/api/content/{content}/child/attachment", False, True),
        ("GET", "/atlassian/wiki/rest/api/content/999999999/child/attachment", False, True),
        ("GET", "/atlassian/wiki/rest/api/space/NOPESUCH", False, True),
        ("HEAD", "/atlassian/wiki/rest/api/space", False, True),
        ("GET", "/atlassian/wiki/rest/api/search?cql=type%3Dpage", False, False),
        ("GET", "/atlassian/wiki/rest/api/content/{content}/restriction/byOperation", False, False),
        ("GET", "/atlassian/wiki/rest/api/space/{space}/permission", False, False),
        # the 403 an anonymous request gets never reached the service that stamps them
        ("GET", "/atlassian/wiki/rest/api/space", True, False),
        # an `OPTIONS` is answered around the route, and a path no route serves by the catch-all
        ("OPTIONS", "/atlassian/wiki/rest/api/space", False, False),
        ("OPTIONS", "/atlassian/wiki/rest/api/space/{space}", False, False),
        ("GET", "/atlassian/wiki/rest/api/nopesuchroute", False, False),
        ("GET", "/atlassian/wiki/rest/api/space/{space}/nope", False, False),
        ("GET", "/atlassian/wiki/nope", False, False),
    ],
)
def test_confluence_says_its_v1_rest_api_is_deprecated(
    client, admin_h, keys, method, path, anonymous, carries
):
    """Pins where the comment on ``backlot.routers.atlassian.CONFLUENCE_DEPRECATION`` says the three
    headers ride and where it says they do not, the ids and the clock riding on both."""
    r = client.request(method, path.format(**keys), headers={} if anonymous else admin_h)
    if anonymous:
        assert r.status_code == 403
    assert r.headers["atl-request-id"] and r.headers["x-confluence-request-time"]
    if carries:
        assert r.headers["deprecation"] == "Wed, 1 Mar 2023 00:00:00 GMT"
        assert r.headers["warning"].startswith('299 - "Deprecated API')
        assert 'rel="deprecation"' in r.headers["link"]
    else:
        assert "deprecation" not in r.headers and "warning" not in r.headers
        assert 'rel="deprecation"' not in r.headers.get("link", "")


@pytest.mark.parametrize(
    "path",
    [
        "/atlassian/rest/api/3/serverInfo/",
        "/atlassian/rest/api/3/serverInfo//",
        "/atlassian/rest/api/3//serverInfo",
        "/atlassian/rest/api/2/field/",
        "/atlassian/wiki/rest/api/space/",
        "/atlassian/wiki/rest/api/content/",
    ],
)
def test_atlassian_serves_a_path_the_gateway_normalises(client, admin_h, path):
    """Pins the routing ``backlot.main.normalise_the_slashes_in_an_atlassian_path`` describes: each
    spelling answers what the canonical one answers."""
    canonical = re.sub("/{2,}", "/", path).rstrip("/")
    served = client.get(path, headers=admin_h, follow_redirects=False)
    assert served.status_code == 200, served.text
    assert served.json() == client.get(canonical, headers=admin_h).json()


@pytest.mark.parametrize(
    "method,path,anonymous,refusal",
    [
        ("GET", "/atlassian/wiki/rest/api/user/current", True, "forbidden"),
        ("GET", "/atlassian/wiki/rest/api/content/{content}/restriction", True, "forbidden"),
        ("DELETE", "/atlassian/wiki/rest/api/content/{content}/label/nope", True, "forbidden"),
        # served on real, published in no document
        ("GET", "/atlassian/wiki/rest/api/content/{content}/history", True, "forbidden"),
        ("GET", "/atlassian/wiki/rest/api/space/{space}/content", True, "forbidden"),
        ("GET", "/atlassian/wiki/rest/api/audit", True, "not-permitted"),
        ("GET", "/atlassian/wiki/rest/api/template/blueprint", True, "not-permitted"),
        (
            "GET",
            "/atlassian/wiki/rest/atlassian-connect/1/app/module/dynamic",
            True,
            "not-permitted",
        ),
        # the controls: one real runs with no credential at all, and a caller whose credential
        # resolves (``backlot.routers.atlassian._confluence_refusal``)
        ("GET", "/atlassian/wiki/rest/api/contentbody/convert/async/bulk/tasks", True, None),
        ("GET", "/atlassian/wiki/rest/api/user/current", False, None),
        ("GET", "/atlassian/wiki/rest/api/content/{content}/history", False, None),
    ],
)
def test_confluence_refuses_an_operation_it_publishes_to_a_caller_it_cannot_name(
    client, admin_h, keys, method, path, anonymous, refusal
):
    """Pins the anonymous refusals ``backlot.routers.atlassian._confluence_refusal`` gives:
    ``errors.atlassian.CONFLUENCE_FORBIDDEN_BODY``, or on the services
    ``backlot.routers.atlassian._CONFLUENCE_NOT_PERMITTED`` names
    ``errors.atlassian.CONFLUENCE_NOT_PERMITTED_BODY`` with the headers its comment names."""
    r = client.request(method, path.format(**keys), headers={} if anonymous else admin_h)
    if refusal is None:
        assert r.status_code == 404, r.text
        return
    assert r.status_code == 403
    assert r.headers["content-type"] == "application/json"
    if refusal == "forbidden":
        assert r.content == errors_atlassian.CONFLUENCE_FORBIDDEN_BODY
        assert len(r.content) == 190
        assert "expires" not in r.headers
        return
    assert (
        r.content == b'{"message":"Current user not permitted to use Confluence","statusCode":403}'
    )
    if path.endswith("/app/module/dynamic"):
        assert "expires" not in r.headers and "cache-control" not in r.headers
    else:
        assert r.headers["cache-control"] == "no-cache, no-store, must-revalidate"
        assert r.headers["expires"] == "Thu, 01 Jan 1970 00:00:00 GMT"


_PROBLEM = errors_atlassian.PROBLEM_JSON
_JAXRS_XML = errors_atlassian.JAXRS_XML_MEDIA_TYPE
_PAGE = errors_atlassian.HTML_MEDIA_TYPE
_JIRA_PAGE = errors_atlassian.JIRA_SITE_HTML_MEDIA_TYPE


@pytest.mark.parametrize(
    "path,anonymous,status,media_type,location,cache",
    [
        ("/atlassian/rest", False, 404, _PROBLEM, None, "no-cache, no-store, no-transform"),
        (
            "/atlassian/rest/nope/thing",
            False,
            404,
            _PROBLEM,
            None,
            "no-cache, no-store, no-transform",
        ),
        ("/atlassian/wiki/rest/api/nopesuchroute", False, 404, _JAXRS_XML, None, "no-transform"),
        ("/atlassian/wiki/rest/nope", False, 404, _PAGE, None, None),
        ("/atlassian/wiki/rest/nope", True, 404, _PAGE, None, None),
        # below a resource the API serves, the product's page rather than the API's 404
        ("/atlassian/wiki/rest/api/space/{space}/nope", False, 404, _PAGE, None, None),
        ("/atlassian/wiki/rest/api/space/NOPESUCH/deeper", False, 404, _PAGE, None, None),
        ("/atlassian/wiki/rest/api/content/{content}/nope", False, 404, _PAGE, None, None),
        ("/atlassian/wiki/rest/api/content/{content}/nope", True, 404, _PAGE, None, None),
        ("/atlassian/wiki/rest/api/content/nope/deeper", False, 404, _PAGE, None, None),
        # the Confluence web app sends a caller with no credential to log in
        ("/atlassian/wiki/nope", False, 404, _PAGE, None, None),
        (
            "/atlassian/wiki/nope",
            True,
            302,
            None,
            "{site}/login?application=confluence&dest-url=%2Fwiki%2Fnope",
            None,
        ),
        # an escape in the path, and an escaped `?` beside a query (``unmatched_path``)
        (
            "/atlassian/wiki/nope%20x",
            True,
            302,
            None,
            "{site}/login?application=confluence&dest-url=%2Fwiki%2Fnope%2520x",
            None,
        ),
        (
            "/atlassian/wiki/nope%3Fx?a=1",
            True,
            302,
            None,
            "{site}/login?application=confluence&dest-url=%2Fwiki%2Fnope%253Fx%3Fa%3D1",
            None,
        ),
        # Jira's own not-found page, its charset in lower case
        ("/atlassian/foo", False, 404, _JIRA_PAGE, None, None),
        ("/atlassian/foo", True, 404, _JIRA_PAGE, None, None),
        ("/atlassian/restx/api/3/serverInfo", False, 404, _JIRA_PAGE, None, None),
        ("/atlassian/ex/jira/nope/rest/api/3/issue/NOPE-1", False, 404, _JIRA_PAGE, None, None),
        # the Jira web app
        (
            "/atlassian/browse",
            True,
            200,
            "text/html;charset=UTF-8",
            None,
            "no-cache, no-store, must-revalidate",
        ),
        (
            "/atlassian/browse/",
            True,
            200,
            "text/html",
            None,
            "no-store, max-age=0, stale-if-error=0",
        ),
        (
            "/atlassian/browse/",
            False,
            200,
            "text/html",
            None,
            "no-store, max-age=0, stale-if-error=0",
        ),
        (
            "/atlassian/browse/NOPE-1",
            True,
            200,
            "text/html",
            None,
            "no-store, max-age=0, stale-if-error=0",
        ),
        (
            "/atlassian/browse/NOPE-1",
            False,
            200,
            "text/html; charset=utf-8",
            None,
            "no-store, max-age=0, stale-if-error=0",
        ),
        # an encoded backslash in the key included, which is Tomcat's 400 under Jira's API
        # (``errors.atlassian.front_door``) and not here
        (
            "/atlassian/browse/A%5C1",
            True,
            200,
            "text/html",
            None,
            "no-store, max-age=0, stale-if-error=0",
        ),
        # The mount itself keeps its slash, since stripped it would be a path no route matches and
        # Starlette's slash redirect would send the client back to the spelling it asked for; it
        # is the site's root, which redirects.
        (
            "/atlassian/",
            True,
            302,
            None,
            "{site}/login.jsp?os_destination=http%3A%2F%2Ftestserver%2F",
            None,
        ),
        ("/atlassian/", False, 302, None, "{site}/jira/for-you", None),
        # Backlot's own paths that only start like the mount: FastAPI's 404 and none of the
        # products' headers, where a prefix test without the boundary would answer them as Jira.
        ("/atlassianx", False, 404, "backlot", None, None),
        ("/atlassianx/rest/api/3/serverInfo", False, 404, "backlot", None, None),
    ],
)
def test_atlassian_answers_by_which_mount_the_path_is_under(
    client, admin_h, keys, path, anonymous, status, media_type, location, cache
):
    """Pins the answer each mount gives: Jira's RFC 7807 under ``errors.atlassian.JIRA_REST``, the
    JAX-RS 404 under Confluence's API mount, a product's page below the resources
    ``backlot.routers.atlassian._CONFLUENCE_HTML_RESOURCES`` names, the Confluence web app's login
    redirect for a caller with no credential, and outside both mounts what
    ``backlot.routers.atlassian._site_surface`` answers. The pages are stubs with real's status and
    media type, and the caching is what ``backlot.routers.atlassian.vendor_headers`` and
    ``backlot.routers.atlassian._confluence_not_found`` say. So the mount decides the shape, not
    `is_confluence` alone."""
    r = client.get(
        path.format(**keys), headers={} if anonymous else admin_h, follow_redirects=False
    )
    assert r.status_code == status, r.text
    if media_type == "backlot":
        assert r.json() == {"detail": "Not Found"}
        assert "atl-request-id" not in r.headers
        return
    assert "atl-request-id" in r.headers
    assert r.headers.get("cache-control") == cache
    # the web app's static shell is served without Jira's request id
    assert ("x-arequestid" in r.headers) == (
        not path.startswith(("/atlassian/wiki", "/atlassian/browse/"))
    )
    if location is not None:
        assert r.headers["location"] == location.format(site="http://testserver")
        assert r.content == b"" and "content-type" not in r.headers
        return
    assert r.headers["content-type"] == media_type
    if status == 404 and media_type in (_PAGE, _JIRA_PAGE):
        assert r.text == errors_atlassian.HTML_NOT_FOUND


@pytest.mark.parametrize("version", [2, 3])
def test_jira_numeric_ids_resolve_exactly_and_preserve_acl(client, admin_h, tokens, version):
    """An issue's numeric id reads as its key does, for the admin and each scoped token, and with a
    leading zero is a 404 (see store.jira_by_numeric_id).
    """
    issues = client.get(
        "/atlassian/rest/api/3/search/jql?jql=project+%3D+payments&maxResults=50", headers=admin_h
    ).json()["issues"]
    assert issues
    for issue in issues:
        path = f"/atlassian/rest/api/{version}/issue/"
        key, numeric = issue["key"], issue["id"]
        for suffix in ["", "/comment"]:
            by_key = client.get(path + key + suffix, headers=admin_h)
            by_id = client.get(path + numeric + suffix, headers=admin_h)
            assert by_id.status_code == by_key.status_code == 200
            assert by_id.json() == by_key.json()
            assert client.get(path + "0" + numeric + suffix, headers=admin_h).status_code == 404
            for token in tokens.values():
                h = {"Authorization": "Bearer " + token}
                keyed = client.get(path + key + suffix, headers=h)
                numbered = client.get(path + numeric + suffix, headers=h)
                assert numbered.status_code == keyed.status_code
                assert numbered.json() == keyed.json()


@pytest.mark.parametrize("version", [2, 3])
def test_jira_issue_ids_that_hash_alike_each_read_back_their_own_issue(tmp_path, version):
    """PAY-1425 and PAY-2172 share a numeric seed; each served id must read its own issue, and the
    subtask and parent entries that point at them carry those ids."""
    keys = ("PAY-1425", "PAY-2172")
    corpus = [
        {
            "source_type": "jira",
            "doc_id": key,
            "key": key,
            "project": "payments",
            "title": key,
            "content": "Body.",
            "author_email": "ava@acme.com",
            "created": "2026-01-01T00:00:00Z",
            "issuetype": "Task",
            "status": "To Do",
            **({"parent": "PAY-1425"} if key == "PAY-2172" else {}),
        }
        for key in keys
    ]
    settings = tiny_corpus(tmp_path, corpus)
    admin = yaml.safe_load(settings.tokens_path.read_text(encoding="utf-8"))["admin_token"]
    with client_for(settings, reload=True) as c:
        h = {"Authorization": f"Bearer {admin}"}
        path = f"/atlassian/rest/api/{version}/issue/"
        ids = {}
        for key in keys:
            by_key = c.get(path + key, headers=h)
            assert by_key.status_code == 200
            ids[key] = by_key.json()["id"]
            by_id = c.get(path + ids[key], headers=h)
            assert by_id.status_code == 200 and by_id.json()["key"] == key
            assert by_id.json() == by_key.json()
            assert (
                c.get(path + ids[key] + "/comment", headers=h).json()
                == c.get(path + key + "/comment", headers=h).json()
            )
        assert len(set(ids.values())) == len(keys)
        parent = c.get(path + ids["PAY-1425"], headers=h).json()["fields"]
        assert [s["id"] for s in parent["subtasks"]] == [ids["PAY-2172"]]
        assert parent["subtasks"][0]["self"].endswith("/issue/" + ids["PAY-2172"])
        child = c.get(path + ids["PAY-2172"], headers=h).json()["fields"]
        assert child["parent"]["id"] == ids["PAY-1425"]


_SEARCH = "/atlassian/wiki/rest/api/search?cql=type%3Dpage"


@pytest.mark.parametrize(
    "method,path,credential,status,cached",
    [
        ("GET", _SEARCH + "&limit=1", "admin", 200, True),
        ("HEAD", _SEARCH + "&limit=1", "admin", 200, True),
        ("GET", _SEARCH + "&limit=-1", "admin", 400, True),
        ("GET", _SEARCH + "&limit=abc", "admin", 404, True),
        ("HEAD", _SEARCH + "&start=abc", "admin", 404, True),
        ("GET", _SEARCH + "&limit=2&cursor=abc", "admin", 400, True),
        ("GET", _SEARCH + "&limit=1", None, 403, True),
        ("HEAD", _SEARCH + "&limit=1", None, 403, True),
        ("GET", _SEARCH + "&limit=1", "Basic !!!", 401, False),
        ("HEAD", _SEARCH + "&limit=1", "Basic !!!", 401, False),
        ("POST", _SEARCH + "&limit=1", "admin", 405, False),
        ("OPTIONS", _SEARCH + "&limit=1", "admin", 200, False),
        ("GET", "/atlassian/wiki/rest/api/content?limit=1", "admin", 200, False),
    ],
)
def test_confluence_search_carries_the_cache_pair_where_real_does(
    client, admin_h, method, path, credential, status, cached
):
    """Pins where ``backlot.routers.atlassian.vendor_headers`` puts the search's `cache-control`
    and `expires`, each row's status and headers as Confluence Cloud answered it."""
    headers = {"admin": admin_h, None: {}}.get(credential, {"Authorization": credential})
    r = client.request(method, path, headers=headers)
    assert r.status_code == status, r.text
    if cached:
        assert r.headers["cache-control"] == "no-cache, no-store, must-revalidate"
        assert r.headers["expires"] == "Thu, 01 Jan 1970 00:00:00 GMT"
    else:
        assert "cache-control" not in r.headers and "expires" not in r.headers


# --- the front door's own refusals: a page before either product ----------------------------

#: The pages the layers in front of both products send (``errors.atlassian`` dates each), each as
#: its length in bytes and the SHA-256 of its text with the `Request ID:` line read as
#: `Request ID: <id>`: the one line two of CloudFront's answers differ by, a value seeded from the
#: method and path here and random on real.
_CLOUDFRONT_405 = (915, "367a52dd1b2cf9f4508bbc97dd06390705cba5cf43446e0b57bae8e36aeabd2c")
_CLOUDFRONT_METHOD_403 = (1053, "5dd68faa0cbf2eb7f73764c74991458af12c48974ee1abbe35e55303c9e76207")
_CLOUDFRONT_400 = (915, "73a1d9f5d62fcebdc292a776e609c11154344817ddfa6d4d1fa2b753bdc6f8b6")
_CLOUDFRONT_BLOCKED = (919, "9d105cf52ff43a5280d20ee1f0fdfac202ef44d9d70b25857d2a7b6ad3a59e45")
_NGINX_400 = (150, "487ad0d2cf075f4328a1adf57ef428759ad4e2c873a8ebd2ad9653990829c9cf")
_OPENRESTY_405 = (154, "4a327e30d348f9975e50913c72d1bdb067f31f726722326c17c6af8d7f04b3a6")
_TOMCAT_400 = (435, "141d0eee920f04eae74509434b49e8c473014166b90ec31e6f03596a782bf89f")
_EDGE_400 = (126, "8266721d920027d1f639bfc978f9293bd845ffd458017961400c519328e2c328")
_EDGE_PAGE = "text/html"
_TOMCAT_PAGE = "text/html;charset=utf-8"


@pytest.mark.parametrize(
    "method,vendor_path,status,page,media_type",
    [
        # the CDN: `TRACE` is its 405, a method spelled as the 403's rule spells one its 403, any
        # other its 400, and a `%00` or a broken escape its 400 ahead of the method and the firewall
        ("TRACE", "/rest/api/3/serverInfo", 405, _CLOUDFRONT_405, _EDGE_PAGE),
        ("PROPFIND", "/rest/api/3/serverInfo", 403, _CLOUDFRONT_METHOD_403, _EDGE_PAGE),
        ("MKACTIVITY", "/rest/api/3/serverInfo", 400, _CLOUDFRONT_400, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope%00", 400, _CLOUDFRONT_400, _EDGE_PAGE),
        ("TRACE", "/rest/api/3/nope%00", 400, _CLOUDFRONT_400, _EDGE_PAGE),
        ("PATCH", "/rest/api/3/nope%00", 400, _CLOUDFRONT_400, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope%zz", 400, _CLOUDFRONT_400, _EDGE_PAGE),
        ("GET", "/rest/api/3/serverInfo%2", 400, _CLOUDFRONT_400, _EDGE_PAGE),
        ("GET", "/wiki/rest/api/space/%", 400, _CLOUDFRONT_400, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope%%41", 400, _CLOUDFRONT_400, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope%u005c", 400, _CLOUDFRONT_400, _EDGE_PAGE),
        ("TRACE", "/rest/api/3/nope%zz", 400, _CLOUDFRONT_400, _EDGE_PAGE),
        ("PROPFIND", "/rest/api/3/nope%zz", 400, _CLOUDFRONT_400, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope<script>%zz", 400, _CLOUDFRONT_400, _EDGE_PAGE),
        # its firewall (`errors.atlassian.waf_blocks`): a tag, and a step up a directory
        ("GET", "/rest/api/3/nope<script>", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/wiki/rest/api/nope%3Cscript%3E", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/wiki/rest/api/space/A<script>", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope%253Cscript%253E", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope&lt;script&gt;", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope</script>", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope<svg>", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope<svgx>", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope<xslt>", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope<!doctypex>", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope<!ENTITY>", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope..%2F..%2Fetc%2Fpasswd", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope..%5Cy", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope/..;/x", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope/..%3B/x", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope/..;;;;/x", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope/..;%3B/x", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/serverInfo?x=..%2Fy", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/serverInfo?x=..%5C", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/serverInfo?x=..%5C&y=1", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/serverInfo?x=..%5C%5Cy", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/rest/api/3/serverInfo?x=..%5C%2Fy", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        # the method's refusal before the firewall's, the firewall's before the gateway's `PATCH`
        ("PROPFIND", "/rest/api/3/nope<script>", 403, _CLOUDFRONT_METHOD_403, _EDGE_PAGE),
        ("PATCH", "/rest/api/3/nope<script>", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        # the gateway's `PATCH`, which picks the product with each run of slashes read as one and
        # comes before Tomcat
        ("PATCH", "/rest/api/3/serverInfo", 400, _NGINX_400, _EDGE_PAGE),
        ("PATCH", "/wiki/rest/api/space", 405, _OPENRESTY_405, _EDGE_PAGE),
        ("PATCH", "/wiki", 405, _OPENRESTY_405, _EDGE_PAGE),
        ("PATCH", "//wiki/rest/api/space", 405, _OPENRESTY_405, _EDGE_PAGE),
        ("PATCH", "/wiki/rest/api/space/NO%2FSUCH", 405, _OPENRESTY_405, _EDGE_PAGE),
        # Confluence's Tomcat: an encoded slash or backslash anywhere under `/wiki/` but `spaces/`
        ("GET", "/wiki/rest/api/space/NO%2FSUCH", 400, _TOMCAT_400, _TOMCAT_PAGE),
        ("POST", "/wiki/rest/api/space/NO%2FSUCH", 400, _TOMCAT_400, _TOMCAT_PAGE),
        ("GET", "/wiki/rest/api/space/NO%2fSUCH", 400, _TOMCAT_400, _TOMCAT_PAGE),
        ("GET", "/wiki/rest/api/space/NO%5CSUCH", 400, _TOMCAT_400, _TOMCAT_PAGE),
        ("GET", "//wiki/rest/api/space/NO%2FSUCH", 400, _TOMCAT_400, _TOMCAT_PAGE),
        ("GET", "/wiki/rest/api/nope%2Fthing", 400, _TOMCAT_400, _TOMCAT_PAGE),
        ("GET", "/wiki/rest/api/space%2F", 400, _TOMCAT_400, _TOMCAT_PAGE),
        ("GET", "/wiki/display/A%2FB", 400, _TOMCAT_400, _TOMCAT_PAGE),
        # and on Jira's API an encoded backslash, at a path no route matches and at served ones
        ("GET", "/rest/api/3/nope%5Cx", 400, _TOMCAT_400, _TOMCAT_PAGE),
        ("GET", "/rest/api/3/issue/A%5CB", 400, _TOMCAT_400, _TOMCAT_PAGE),
        ("GET", "/rest/api/3/issue/A%5CB/comment?maxResults=abc", 400, _TOMCAT_400, _TOMCAT_PAGE),
        # the edge's 400 for a `;`, bare or escaped in either case, wherever it stands, a `..;` the
        # firewall lets through included (the shape `Accept` picks is
        # `test_atlassian_edge_refuses_a_semicolon_in_the_shape_accept_picks`)
        ("GET", "/rest/api/3/nope%3Bx", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope%3bx", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope;x", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope;x%3By", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/rest/api/3/issue/A%3BB", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/rest/api/3/issue/A;B", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/rest/api/3/issue/A%3BB/comment?maxResults=abc", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/wiki/rest/api/nope%3Bx", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/wiki/rest/api/nope;x", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/wiki/rest/api/space/NO;SUCH", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/wiki/rest/api/space/NO%3BSUCH", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/wiki/rest/api/content/1;2", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/rest/api/3/nopeA..;/x", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/rest/api/3/nope/..;a/x", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/browse/A-1;x", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/x;y", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/wiki/x;y", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/wiki/spaces/X;y", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/wiki/rest/api;x", 400, _EDGE_400, _EDGE_PAGE),
        ("PATCH", "/rest/api/3/issue/A;B", 400, _EDGE_400, _EDGE_PAGE),
        ("PATCH", "/wiki/rest/api/space;x", 400, _EDGE_400, _EDGE_PAGE),
        # and where it stands among the other layers' refusals (``errors.atlassian.front_door``)
        ("GET", "/rest/api/3/nope%00;x", 400, _CLOUDFRONT_400, _EDGE_PAGE),
        ("PROPFIND", "/rest/api/3/nope;x", 403, _CLOUDFRONT_METHOD_403, _EDGE_PAGE),
        ("GET", "/rest/api/3/..;/nope;x", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("GET", "/wiki/rest/api/space/A%2FB;x", 400, _EDGE_400, _EDGE_PAGE),
        ("GET", "/rest/api/3/issue/A%5CB;x", 400, _EDGE_400, _EDGE_PAGE),
        # a `HEAD` is the page's status and length with no body
        ("HEAD", "/rest/api/3/nope<script>", 403, _CLOUDFRONT_BLOCKED, _EDGE_PAGE),
        ("HEAD", "/wiki/rest/api/space/NO%2FSUCH", 400, _TOMCAT_400, _TOMCAT_PAGE),
        ("HEAD", "/rest/api/3/nope;x", 400, _EDGE_400, _EDGE_PAGE),
        # what none of them refuses reaches the product: a tag the firewall does not name, a
        # closing tag but `script`'s, a name short of `!doctype`, a tag in the query, a `..\` there
        # that neither a `\` nor a `/` nor the end of the value follows, a `;` there, a step in a
        # parameter's name, a broken escape in the query, Jira's decoded slash and the web app's
        # `spaces/` in any case
        ("GET", "/rest/api/3/nope<img>", 404, None, errors_atlassian.PROBLEM_JSON),
        ("GET", "/rest/api/3/nope</style>", 404, None, errors_atlassian.PROBLEM_JSON),
        ("GET", "/rest/api/3/nope<!doctyp>", 404, None, errors_atlassian.PROBLEM_JSON),
        ("GET", "/rest/api/3/nope<!ELEMENT>", 404, None, errors_atlassian.PROBLEM_JSON),
        (
            "GET",
            "/rest/api/3/serverInfo?q=<script>",
            200,
            None,
            errors_atlassian.JIRA_JSON_MEDIA_TYPE,
        ),
        (
            "GET",
            "/rest/api/3/serverInfo?x=..%5Cy",
            200,
            None,
            errors_atlassian.JIRA_JSON_MEDIA_TYPE,
        ),
        ("GET", "/rest/api/3/serverInfo?x=..%3B", 200, None, errors_atlassian.JIRA_JSON_MEDIA_TYPE),
        (
            "GET",
            "/rest/api/3/serverInfo?x=..%5C.",
            200,
            None,
            errors_atlassian.JIRA_JSON_MEDIA_TYPE,
        ),
        ("GET", "/rest/api/3/serverInfo?..%2F=1", 200, None, errors_atlassian.JIRA_JSON_MEDIA_TYPE),
        ("GET", "/rest/api/3/serverInfo?x=%zz", 200, None, errors_atlassian.JIRA_JSON_MEDIA_TYPE),
        ("GET", "/rest/api/3/issue/A%2FB", 404, None, errors_atlassian.PROBLEM_JSON),
        # the web app answers `spaces/` itself (real's single-page app at 200, which is not copied)
        ("GET", "/wiki/spaces/NO%2FSUCH", 404, None, "text/html;charset=UTF-8"),
        ("GET", "/wiki/SPACES/NO%2FSUCH", 404, None, "text/html;charset=UTF-8"),
    ],
)
def test_atlassian_front_door_answers_before_either_product(
    client, admin_h, method, vendor_path, status, page, media_type
):
    """Each refusal is the page real sent, held here as its length and digest, with the headers its
    layer carries (``errors.atlassian.FrontDoor``). The edge's `;` page is the stub
    ``errors.atlassian._EDGE_PAGE_400`` describes. Which layer gives a refusal and in what order is
    ``errors.atlassian.front_door``'s, which carries the measurement. The rows with no page reach
    the product. The credential does not change which layer answers, so an admin request is enough
    to show a served route does not reach its handler."""
    path, _, query = vendor_path.partition("?")
    answer = errors_atlassian.front_door(method, f"/atlassian{path}", query)
    r = _spelled(client, method).request("GET", f"/atlassian{vendor_path}", headers=admin_h)
    assert r.status_code == status, r.text
    assert r.headers["content-type"] == media_type
    if page is None:
        assert answer is None
        return
    length, digest = page
    assert "allow" not in r.headers
    assert ("content-language" in r.headers) == (media_type == _TOMCAT_PAGE)
    # the gateway's two ids, which the edge's 400 carries too where no `;` is bare
    ids = page in (_NGINX_400, _OPENRESTY_405, _TOMCAT_400) or (
        page == _EDGE_400 and ";" not in vendor_path
    )
    for name in ("atl-request-id", "atl-traceid"):
        assert (name in r.headers) == ids, name
    for name in ("x-content-type-options", "x-xss-protection"):
        assert (name in r.headers) == (page not in (_CLOUDFRONT_405, _CLOUDFRONT_400)), name
    assert r.headers.get("access-control-allow-origin") == ("*" if page == _EDGE_400 else None)
    if method == "HEAD":
        assert r.content == b"" and r.headers["content-length"] == str(length)
        return
    assert len(r.content) == length
    assert re.search(r"Request ID: [A-Za-z0-9_-]{54}==\n", r.text) or "Request ID" not in r.text
    normalised = re.sub(r"Request ID: \S+", "Request ID: <id>", r.text)
    assert hashlib.sha256(normalised.encode()).hexdigest() == digest


_EDGE_JSON = b'{"message":"","status":400}\n'
_EDGE_STUB = (
    b'<!DOCTYPE html><html><head><meta name="details" content="lua_response">'
    b"<title>400</title></head><body><p>400</p></body></html>"
)


@pytest.mark.parametrize(
    "vendor_path", ["/rest/api/3/issue/A;B", "/rest/api/3/nope%3Bx", "/wiki/rest/api/space/NO;SUCH"]
)
@pytest.mark.parametrize(
    "accept,media_type,body",
    [
        ("application/json", "application/json", _EDGE_JSON),
        ("application/json, text/html", "application/json", _EDGE_JSON),
        ("text/html, application/json", "application/json", _EDGE_JSON),
        ("application/json;q=0, text/html", "application/json", _EDGE_JSON),
        ("APPLICATION/JSON", "text/html", _EDGE_STUB),
        ("application/*, application/xml", "text/html", _EDGE_STUB),
        ("text/html", "text/html", _EDGE_STUB),
        ("*/*", "text/html", _EDGE_STUB),
    ],
)
def test_atlassian_edge_refuses_a_semicolon_in_the_shape_accept_picks(
    client, admin_h, vendor_path, accept, media_type, body
):
    """The edge's 400 for a `;` (``errors.atlassian.front_door``), in the shape `Accept` picks, with
    no charset on either product."""
    r = client.get(f"/atlassian{vendor_path}", headers={**admin_h, "Accept": accept})
    assert r.status_code == 400
    assert r.headers["content-type"] == media_type
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.content == body


@pytest.mark.parametrize(
    "method,path",
    [
        ("TRACE", "/github/user"),
        ("PROPFIND", "/notion/v1/users"),
        ("GET", "/slack/api/auth.test%00"),
        ("PATCH", "/s3/"),
    ],
)
def test_atlassian_front_door_answers_under_atlassian_alone(client, admin_h, method, path):
    """A request another vendor's mount takes that the front door would refuse under `/atlassian`
    gets no page of CloudFront's or the gateway's: the middleware
    (``backlot.main.answer_at_the_atlassian_front_door``) passes any path it does not own."""
    r = _spelled(client, method).request("GET", path, headers=admin_h)
    for layer in ("CloudFront", "nginx", "openresty"):
        assert layer not in r.text, (layer, r.status_code)


# --- a write on a served path: the operation's own answer, not a 405 -------------------------


@pytest.mark.parametrize(
    "method,vendor_path,body,status",
    [
        # an issue write is the issue's 404, and with no body the 415 the operation's media-type
        # check gives first; a route here takes only GET, so the method reaches `unmatched_path`
        ("DELETE", "/rest/api/3/issue/NOPE-999999", "{}", 404),
        ("DELETE", "/rest/api/2/issue/NOPE-999999", "{}", 404),
        ("PUT", "/rest/api/3/issue/NOPE-999999", "{}", 404),
        ("PUT", "/rest/api/3/issue/NOPE-999999", None, 415),
        ("POST", "/rest/api/3/issue/NOPE-999999/comment", "{}", 404),
        ("POST", "/rest/api/3/issue/NOPE-999999/comment", None, 415),
        # a project role write at a key that names nothing is the project's 404
        ("POST", "/rest/api/3/project/NOPESUCHZZ/role/999", "{}", 404),
        ("PUT", "/rest/api/3/project/NOPESUCHZZ/role/999", None, 415),
        ("PUT", "/rest/api/3/project/NOPESUCHZZ/role/999", "{}", 404),
        ("DELETE", "/rest/api/3/project/NOPESUCHZZ/role/999", None, 404),
        # `issue/archive`'s own write, which real runs (a 400) and Backlot does not
        ("PUT", "/rest/api/3/issue/archive", "{}", 405),
    ],
)
def test_jira_runs_a_write_on_a_served_path_where_a_route_takes_only_get(
    client, method, vendor_path, body, status
):
    """The same path's `GET` is served (the `test_admin_*` crawls exercise it); the write differs
    only by method and reaches the operation real reaches (``_JIRA_ISSUE_WRITES``,
    ``_JIRA_ROLE_WRITES`` and ``_jira_operation`` in ``backlot.routers.atlassian`` carry the
    measurement). A method no operation is published for is the 405
    ``errors.atlassian.method_not_allowed`` answers, which
    `test_jira_refuses_a_wrong_method_as_rfc_7807_naming_the_methods_it_takes` pins. No
    credential."""
    kwargs = {"content": body, "headers": {"Content-Type": "application/json"}} if body else {}
    r = client.request(method, f"/atlassian{vendor_path}", **kwargs)
    assert r.status_code == status, r.text
    if status == 415:
        assert r.headers["content-type"] == errors_atlassian.PROBLEM_JSON
        assert r.json()["detail"] == "Content-Type 'null' is not supported."
    elif status == 405:
        assert r.headers["content-type"] == errors_atlassian.PROBLEM_JSON
        assert r.json()["detail"] == f"Method '{method}' is not supported."
    else:
        assert sorted(r.json()) == ["errorMessages", "errors"]  # `errors.atlassian.http_body`


_SPRING_415 = "org.springframework.web.HttpMediaTypeNotSupportedException: "
_OUT_OF_RANGE = 'Invalid quality value "{}": should be between 0.0 and 1.0'
_JSON_TYPE = "application/json"


@pytest.mark.parametrize(
    "method,vendor_path,authorization,ctype,status,body",
    [
        # a Spring write that checks its `Content-Type` first: its 415, naming the type as read,
        # then the caller's refusal for the type it takes
        ("POST", "/wiki/rest/api/content/abc123/child/attachment", None, None, 415, "''"),
        ("POST", "/wiki/rest/api/content/abc123/child/attachment", None, "", 415, "''"),
        ("POST", "/wiki/rest/api/content/abc123/child/attachment", None, "text/plain", 415)
        + ("'text/plain'",),
        ("POST", "/wiki/rest/api/content/abc123/child/attachment", None, "APPLICATION/JSON", 415)
        + ("'application/json'",),
        (
            "POST",
            "/wiki/rest/api/content/abc123/child/attachment",
            None,
            "TEXT/Plain;Charset=UTF-8;X=Y",
            415,
            "'text/plain;Charset=UTF-8;X=Y'",
        ),
        ("POST", "/wiki/rest/api/content/abc123/child/attachment", None, "foo", 415)
        + ("Invalid mime type \"foo\": does not contain '/'",),
        # a type Spring's parser cannot read, named for its reason
        # (``errors.atlassian._invalid_mime_type``), and `*` and `text/plain;a`, which it reads
        *(
            ("POST", "/wiki/rest/api/space/abc123/permission", None, ctype, 415, reason)
            for ctype, reason in (
                (
                    "text/plain;charset=nope",
                    "Invalid mime type \"text/plain;charset=nope\": unsupported charset 'nope'",
                ),
                (
                    "text/",
                    "Invalid mime type \"text/\": does not contain subtype after '/'",
                ),
                (
                    "a/b c",
                    'Invalid mime type "a/b c": Invalid token character \' \' in token "b c"',
                ),
                (";x", "Invalid mime type \";x\": 'mimeType' must not be empty"),
                (" ;x", "Invalid mime type \";x\": 'mimeType' must not be empty"),
                (
                    "text/plain\xa0",
                    'Invalid mime type "text/plain\xa0": '
                    "Invalid token character '\xa0' in token \"plain\xa0\"",
                ),
                (
                    "text/plain;charset=nope\xa0",
                    'Invalid mime type "text/plain;charset=nope\xa0": nope\xa0',
                ),
                (
                    "*/json",
                    'Invalid mime type "*/json": '
                    "wildcard type is legal only in '*/*' (all mime types)",
                ),
                ("*", "'*/*'"),
                ("/plain", "Invalid mime type \"/plain\": 'type' must not be empty"),
                (
                    "text//plain",
                    'Invalid mime type "text//plain": Invalid token character \'/\' in token "/plain"',
                ),
                (
                    "text/plain;=x",
                    "Invalid mime type \"text/plain;=x\": 'parameter' must not be empty",
                ),
                ("text/plain;a=", "Invalid mime type \"text/plain;a=\": 'value' must not be empty"),
                (
                    "text/plain;a b=c;charset=nope",
                    'Invalid mime type "text/plain;a b=c;charset=nope": '
                    "Invalid token character ' ' in token \"a b\"",
                ),
                (
                    "text/plain;a=b c",
                    'Invalid mime type "text/plain;a=b c": Invalid token character \' \' in token "b c"',
                ),
                (
                    'text/plain;a="b',
                    'Invalid mime type "text/plain;a="b": Invalid token character \'"\' in token ""b"',
                ),
                ("text/plain;charset=a b", 'Invalid mime type "text/plain;charset=a b": a b'),
                (
                    "t x/p y",
                    'Invalid mime type "t x/p y": Invalid token character \' \' in token "t x"',
                ),
                (
                    "text/pl ain;charset=nope",
                    'Invalid mime type "text/pl ain;charset=nope": '
                    "Invalid token character ' ' in token \"pl ain\"",
                ),
                (
                    "text/plain;=",
                    "Invalid mime type \"text/plain;=\": 'parameter' must not be empty",
                ),
                ("text/plain;a", "'text/plain'"),
                (
                    "text/plain; charset = nope ",
                    "Invalid mime type \"text/plain; charset = nope\": unsupported charset 'nope'",
                ),
            )
        ),
        # the charset Tomcat reads first (``errors.atlassian.refuse_a_confluence_media_type``)
        *(
            ("POST", "/wiki/rest/api/space/abc123/permission", None, ctype, status, body)
            for ctype, status, body in (
                ("text/plain;charset=", 500, b""),
                ("application/json;charset=", 500, b""),
                ("application/json; charset=", 500, b""),
                ("text/plain;Charset=", 500, b""),
                ("text/plain;charset", 500, b""),
                ('text/plain;charset=""', 500, b""),
                ("text/plain;charset=utf-8;charset=", 500, b""),
                ("application/json;charset=utf-8;Charset=", 500, b""),
                ("text/plain;charset=_x", 500, b""),
                ('text/plain;charset="a b"', 500, b""),
                ("text/plain; charset =", 500, b""),
                ('text/plain;charset;a="b', 500, b""),
                ("application/json;charset=;charset=utf-8", 403, None),
                ("text/plain;charset=;charset=utf-8", 415, "'text/plain;charset=utf-8'"),
                (
                    "text/plain;charset=;x=y",
                    415,
                    "Invalid mime type \"text/plain;charset=;x=y\": 'value' must not be empty",
                ),
                (
                    "text/plain;charset= ;x=y",
                    415,
                    "Invalid mime type \"text/plain;charset= ;x=y\": 'value' must not be empty",
                ),
                ("foo;charset=", 415, "Invalid mime type \"foo;charset=\": does not contain '/'"),
                (";charset=", 415, "Invalid mime type \";charset=\": 'mimeType' must not be empty"),
                (
                    "text/plain;a b=c;charset=",
                    415,
                    'Invalid mime type "text/plain;a b=c;charset=": '
                    "Invalid token character ' ' in token \"a b\"",
                ),
                (
                    "text/plain;charset=_x;a b=c",
                    415,
                    'Invalid mime type "text/plain;charset=_x;a b=c": _x',
                ),
                (
                    'text/plain;charset="nope"',
                    415,
                    'Invalid mime type "text/plain;charset="nope"": unsupported charset \'nope\'',
                ),
            )
        ),
        # the parameters kept (``errors.atlassian._spring_media_type``)
        *(
            ("POST", "/wiki/rest/api/content/abc123/child/attachment", None, ctype, 415, named)
            for ctype, named in (
                ("application/json;charset=nope;charset=utf-8", "'application/json;charset=utf-8'"),
                ("application/json;Charset=nope;charset=utf-8", "'application/json;charset=utf-8'"),
                ("application/json;charset=utf-8;Charset=nope", "'application/json;Charset=nope'"),
                ("application/xml;x=1;X=2", "'application/xml;X=2'"),
                ("application/xml;Q=2", "'application/xml;Q=2'"),
                ("application/xml;q=2;Q=0.5", "'application/xml;Q=0.5'"),
                ("application/xml;q=1d", "'application/xml;q=1d'"),
                ("application/xml;q=-0", "'application/xml;q=-0'"),
            )
        ),
        # a `q` Spring cannot read, the one thing wrong with the type: why, in Spring's words and
        # Java's (``errors.atlassian._spring_quality_reason``)
        *(
            ("POST", "/wiki/rest/api/space/abc123/permission", None, ctype, 415)
            + (f'Invalid media type "{ctype}": {reason}',)
            for ctype, reason in (
                ("application/json;q=2", _OUT_OF_RANGE.format("2")),
                ('application/xml;q="2"', _OUT_OF_RANGE.format("2")),
                ('application/xml;q=" 2"', _OUT_OF_RANGE.format(" 2")),
                ("application/xml;q=NaN", _OUT_OF_RANGE.format("NaN")),
                ("application/xml;q=0x1.8p0", _OUT_OF_RANGE.format("0x1.8p0")),
                ("application/xml;Q=0.5;q=2", _OUT_OF_RANGE.format("2")),
                ("application/xml;Charset=bogus;q=2", _OUT_OF_RANGE.format("2")),
                ("application/xml;charset=ISO_8859-1:1987;q=2", _OUT_OF_RANGE.format("2")),
                ("application/xml;q=abc", 'For input string: "abc"'),
                ('application/xml;q="abc "', 'For input string: "abc"'),
                ("application/xml;q=-", 'For input string: "-"'),
                ("application/xml;q=0x1.8", 'For input string: "0x1.8"'),
                ("application/xml;q=1.2e3.4", 'For input string: "1.2e3.4"'),
                ("application/xml;q=1.2.3", "multiple points"),
                ("application/xml;q=.5.5", "multiple points"),
                ("application/xml;q=-..", "multiple points"),
                ('application/xml;q=""', "empty String"),
            )
        ),
        (
            "POST",
            "/wiki/rest/api/content/abc123/child/attachment",
            None,
            "multipart/form-data;boundary=x;q=2",
            415,
            'Invalid media type "multipart/form-data;boundary=x;q=2": ' + _OUT_OF_RANGE.format("2"),
        ),
        # a charset Java does not name, in the case Spring checks, and one named in another case
        (
            "POST",
            "/wiki/rest/api/content/abc123/child/attachment",
            None,
            "multipart/form-data; boundary=x; charset=u8",
            415,
            'Invalid mime type "multipart/form-data; boundary=x; charset=u8": '
            "unsupported charset 'u8'",
        ),
        (
            "POST",
            "/wiki/rest/api/content/abc123/child/attachment",
            None,
            "multipart/form-data; boundary=x; Charset=bogus",
            403,
            errors_atlassian.CONFLUENCE_FORBIDDEN_BODY,
        ),
        (
            "POST",
            "/wiki/rest/api/content/abc123/child/attachment",
            None,
            "multipart/form-data; boundary=x",
            403,
            errors_atlassian.CONFLUENCE_FORBIDDEN_BODY,
        ),
        ("POST", "/wiki/rest/api/content/abc123/child/attachment/1/data", None, _JSON_TYPE, 415)
        + ("'application/json'",),
        ("PUT", "/wiki/rest/api/content/abc123/state", None, None, 415, "''"),
        ("PUT", "/wiki/rest/api/content/abc123/state", None, "text/plain", 415, "'text/plain'"),
        ("PUT", "/wiki/rest/api/content/abc123/state", None, _JSON_TYPE, 403)
        + (errors_atlassian.CONFLUENCE_FORBIDDEN_BODY,),
        ("PUT", "/wiki/rest/api/space/abc123/theme", None, "text/plain", 415, "'text/plain'"),
        ("POST", "/wiki/rest/api/space/abc123/permission", None, "text/plain", 415, "'text/plain'"),
        ("POST", "/wiki/rest/api/space/abc123/permission", None, _JSON_TYPE, 403)
        + (errors_atlassian.CONFLUENCE_FORBIDDEN_BODY,),
        # Tomcat's 401 for a Basic value it cannot read comes before the 415
        ("POST", "/wiki/rest/api/content/abc123/child/attachment", "Basic !!!", _JSON_TYPE, 401)
        + (None,),
        # a Spring write that checks nothing first refuses the caller with or without a body
        ("DELETE", "/wiki/rest/api/space/abc123", None, None, 403)
        + (errors_atlassian.CONFLUENCE_FORBIDDEN_BODY,),
        ("POST", "/wiki/rest/api/content/abc123/label", None, _JSON_TYPE, 403)
        + (errors_atlassian.CONFLUENCE_FORBIDDEN_BODY,),
        # a JAX-RS write: Tomcat's page for a type it cannot read, an empty 415 for one other than
        # JSON (but `settings/lookandfeel`'s), and the not-permitted refusal for JSON or none
        ("POST", "/wiki/rest/api/content/archive", None, None, 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("POST", "/wiki/rest/api/content/archive", None, _JSON_TYPE, 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("POST", "/wiki/rest/api/content/archive", None, "text/plain", 415, b""),
        ("POST", "/wiki/rest/api/template", None, "application/xml", 415, b""),
        ("POST", "/wiki/rest/api/content/archive", None, "foo", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        # Jersey's own reading of a type: a wildcard JSON matches is JSON, a charset is not looked
        # up and an empty parameter is skipped, and a parameter with no name, no `=`, an empty value
        # or space beside the `=` is unreadable
        ("POST", "/wiki/rest/api/template", None, "*/*", 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("POST", "/wiki/rest/api/template", None, "application/*", 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("POST", "/wiki/rest/api/template", None, "*/json", 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("POST", "/wiki/rest/api/template", None, "application/json;charset=nope", 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("POST", "/wiki/rest/api/template", None, 'application/json; ;x="a;b"', 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("POST", "/wiki/rest/api/template", None, "text/*", 415, b""),
        ("POST", "/wiki/rest/api/template", None, "application/*+json", 415, b""),
        ("POST", "/wiki/rest/api/template", None, "application/json;;", 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("POST", "/wiki/rest/api/template", None, 'application/json;x="a\\"b"', 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("POST", "/wiki/rest/api/template", None, "a@b/json", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("POST", "/wiki/rest/api/template", None, "application/json;=x", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("POST", "/wiki/rest/api/template", None, "application/json;x", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("POST", "/wiki/rest/api/template", None, "application/json;x=", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("POST", "/wiki/rest/api/template", None, "application/json;charset =utf-8", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("POST", "/wiki/rest/api/template", None, "application/json;charset= utf-8", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("DELETE", "/wiki/rest/api/user/watch/space/NOPESUCHZZ", None, "", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("POST", "/wiki/rest/api/template", None, 'application/json;x="a', 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("POST", "/wiki/rest/api/template", None, "application/json/x", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("POST", "/wiki/rest/api/template", None, "a/b c", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("POST", "/wiki/rest/api/template", None, "application/json;x?y", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("POST", "/wiki/rest/api/template", None, "application/json;x=1 ;y=2", 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("POST", "/wiki/rest/api/template", None, "application/json;x=1 ;y", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        # the value's ends, as ``backlot.errors.atlassian.jersey_media_type`` trims them
        ("POST", "/wiki/rest/api/template", None, "application/json\xa0", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("POST", "/wiki/rest/api/template", None, "\x85application/json", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("POST", "/wiki/rest/api/template", None, "application/json\t", 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        # the two that ran: a type Jersey cannot read is its 400 too, JSON reaches the operation,
        # which real runs and Backlot does not serve
        ("POST", "/wiki/rest/api/contentbody/convert/async/1", None, "application/json;x", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("POST", "/wiki/rest/api/contentbody/convert/async/1", None, _JSON_TYPE, 404, None),
        ("POST", "/wiki/rest/api/contentbody/convert/async/bulk/tasks", None, "text/plain", 415)
        + (b"",),
        ("PUT", "/wiki/rest/api/settings/lookandfeel", None, "text/plain", 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("PUT", "/wiki/rest/api/settings/lookandfeel", None, "foo", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("DELETE", "/wiki/rest/api/relation/1/from/user/a/to/content/2", None, None, 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        # each operation the four lists in ``backlot.routers.atlassian`` name, with no credential:
        # the nineteen GETs and the JAX-RS writes but the two that run, the not-permitted 403, the
        # Spring writes that check a type, the 415 for `text/plain`, and the GETs real serves
        # outside the document, the Stackless 403
        *(
            ("GET", f"/wiki/rest/{path}", None, None, 403)
            + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,)
            for path in (
                "api/audit",
                "api/audit/export",
                "api/audit/retention",
                "api/audit/since",
                "api/content-states",
                "api/longtask",
                "api/longtask/1",
                "api/relation/1/from/1/1/to/1",
                "api/relation/1/from/1/1/to/1/1",
                "api/relation/1/to/1/1/from/1",
                "api/search/user",
                "api/settings/lookandfeel",
                "api/template/blueprint",
                "api/template/page",
                "api/template/1",
                "api/user/watch/content/1",
                "api/user/watch/label/1",
                "api/user/watch/space/1",
                "atlassian-connect/1/app/module/dynamic",
            )
        ),
        # the same GETs reading a `Content-Type` (``_CONFLUENCE_JAXRS_GETS``)
        *(
            ("GET", f"/wiki/rest/{path}", None, "text/plain", 415, b"")
            for path in (
                "api/audit",
                "api/audit/export",
                "api/audit/retention",
                "api/audit/since",
                "api/content-states",
                "api/longtask",
                "api/longtask/1",
                "api/relation/1/from/1/1/to/1",
                "api/relation/1/from/1/1/to/1/1",
                "api/relation/1/to/1/1/from/1",
                "api/search/user",
                "api/template/blueprint",
                "api/template/page",
                "api/template/1",
                "api/user/watch/content/1",
                "api/user/watch/label/1",
                "api/user/watch/space/1",
                "atlassian-connect/1/app/module/dynamic",
            )
        ),
        ("GET", "/wiki/rest/api/settings/lookandfeel", None, "text/plain", 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("GET", "/wiki/rest/api/audit", None, "application/*+json", 415, b""),
        ("GET", "/wiki/rest/api/audit", None, "application/json;charset=_x", 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("GET", "/wiki/rest/api/audit", None, "foo", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("GET", "/wiki/rest/api/audit", None, "application/json;x=", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("GET", "/wiki/rest/api/settings/lookandfeel", None, "foo", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        # a path no service serves: the header Jersey cannot read is the 400 page ahead of
        # its 404, and a type it can read leaves that 404 alone
        ("GET", "/wiki/rest/api/nosuch", None, "foo", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("GET", "/wiki/rest/api/nosuch", None, "", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("GET", "/wiki/rest/api/nosuch", None, "text/plain", 404, None),
        # the two `contentbody/convert` reads, which ``_CONFLUENCE_JAXRS_GETS`` names as well
        ("GET", "/wiki/rest/api/contentbody/convert/async/1", None, "text/plain", 415, b""),
        ("GET", "/wiki/rest/api/contentbody/convert/async/bulk/tasks", None, "application/xml")
        + (415, b""),
        ("GET", "/wiki/rest/api/contentbody/convert/async/1", None, "foo", 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        # the header sent twice (``_jaxrs_media_refusal``), and a Spring GET and write as controls
        *(
            ("GET" if path else "POST", path or "/wiki/rest/api/template", None, twice, 400)
            + (errors_atlassian.tomcat_page(400).encode(),)
            for path, twice in (
                ("", (_JSON_TYPE, _JSON_TYPE)),
                ("/wiki/rest/api/audit", (_JSON_TYPE, "text/plain")),
                ("/wiki/rest/api/settings/lookandfeel", ("text/plain", "text/plain")),
                ("/wiki/rest/api/nosuch", (_JSON_TYPE, _JSON_TYPE)),
            )
        ),
        ("GET", "/wiki/rest/api/content/1", None, (_JSON_TYPE, "text/plain"), 403)
        + (errors_atlassian.CONFLUENCE_FORBIDDEN_BODY,),
        # a blank value beside one is left out, and blank ones alone read as an empty value
        ("GET", "/wiki/rest/api/audit", None, ("", _JSON_TYPE), 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("GET", "/wiki/rest/api/audit", None, (_JSON_TYPE, " "), 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("GET", "/wiki/rest/api/audit", None, ("\t", "text/plain"), 415, b""),
        ("GET", "/wiki/rest/api/audit", None, ("", _JSON_TYPE, _JSON_TYPE), 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("GET", "/wiki/rest/api/audit", None, ("", "", _JSON_TYPE), 403)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,),
        ("GET", "/wiki/rest/api/nosuch", None, ("", ""), 400)
        + (errors_atlassian.tomcat_page(400).encode(),),
        ("POST", "/wiki/rest/api/content", None, (_JSON_TYPE, "text/plain"), 403)
        + (errors_atlassian.CONFLUENCE_FORBIDDEN_BODY,),
        *(
            (method, path, None, None, 403, errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY)
            for method, path in (
                ("POST", "/wiki/rest/api/audit"),
                ("PUT", "/wiki/rest/api/audit/retention"),
                ("POST", "/wiki/rest/api/content/archive"),
                ("POST", "/wiki/rest/api/content/blueprint/instance/1"),
                ("PUT", "/wiki/rest/api/content/blueprint/instance/1"),
                ("PUT", "/wiki/rest/api/relation/1/from/1/1/to/1/1"),
                ("DELETE", "/wiki/rest/api/relation/1/from/1/1/to/1/1"),
                ("PUT", "/wiki/rest/api/settings/lookandfeel"),
                ("POST", "/wiki/rest/api/settings/lookandfeel/custom"),
                ("DELETE", "/wiki/rest/api/settings/lookandfeel/custom"),
                ("POST", "/wiki/rest/api/template"),
                ("PUT", "/wiki/rest/api/template"),
                ("DELETE", "/wiki/rest/api/template/1"),
                ("POST", "/wiki/rest/api/user/watch/content/1"),
                ("DELETE", "/wiki/rest/api/user/watch/content/1"),
                ("POST", "/wiki/rest/api/user/watch/label/1"),
                ("DELETE", "/wiki/rest/api/user/watch/label/1"),
                ("POST", "/wiki/rest/api/user/watch/space/1"),
                ("DELETE", "/wiki/rest/api/user/watch/space/1"),
                ("POST", "/wiki/rest/atlassian-connect/1/app/module/dynamic"),
                ("DELETE", "/wiki/rest/atlassian-connect/1/app/module/dynamic"),
            )
        ),
        *(
            (method, path, None, "text/plain", 415, "'text/plain'")
            for method, path in (
                ("POST", "/wiki/rest/api/content/1/child/attachment"),
                ("PUT", "/wiki/rest/api/content/1/child/attachment"),
                ("POST", "/wiki/rest/api/content/1/child/attachment/1/data"),
                ("POST", "/wiki/rest/api/content/1/pagehierarchy/copy"),
                ("POST", "/wiki/rest/api/content/1/permission/check"),
                ("POST", "/wiki/rest/api/content/1/restriction"),
                ("PUT", "/wiki/rest/api/content/1/restriction"),
                ("PUT", "/wiki/rest/api/content/1/state"),
                ("POST", "/wiki/rest/api/content/1/version"),
                ("POST", "/wiki/rest/api/group"),
                ("POST", "/wiki/rest/api/group/userByGroupId"),
                ("POST", "/wiki/rest/api/space/1/permission"),
                ("POST", "/wiki/rest/api/space/1/permission/custom-content"),
                ("PUT", "/wiki/rest/api/space/1/theme"),
                ("POST", "/wiki/rest/api/user/1/property/1"),
                ("PUT", "/wiki/rest/api/user/1/property/1"),
            )
        ),
        *(
            ("GET", f"/wiki/rest/api/{path}", None, None, 403)
            + (errors_atlassian.CONFLUENCE_FORBIDDEN_BODY,)
            for path in (
                "content/1/child",
                "content/1/history",
                "content/1/property",
                "content/1/version",
                "space/ENG/content",
                "space/ENG/property",
            )
        ),
        # an `OPTIONS` asks for no operation and is refused by none of these
        ("OPTIONS", "/wiki/rest/api/content/1/label", None, None, 404, None),
    ],
)
def test_confluence_answers_an_operation_before_it_runs(
    client, method, vendor_path, authorization, ctype, status, body
):
    """With no credential real refuses each operation these rows name, published or served outside
    the document, before it runs, in the order and shape ``_published_answer``,
    ``_CONFLUENCE_CONSUMES``, ``_CONFLUENCE_JAXRS_WRITES``, ``_CONFLUENCE_NOT_PERMITTED_GETS`` and
    ``_CONFLUENCE_OUTSIDE_THE_DOCUMENT`` in ``backlot.routers.atlassian`` carry the measurement for.
    A 415's ``body`` is the reason Spring gives; the 401 is asserted by status alone, its body being
    JSON here where real's is Tomcat's page. An `OPTIONS` at a published write's path is the 404
    real answers it, not a refusal, and `/wiki/rest/api/nosuch`, at which no operation is, the 404
    or the 400 page ``_confluence_not_found`` gives. A ``ctype`` of two values sends the header
    twice."""
    ctypes = () if ctype is None else (ctype,) if isinstance(ctype, str) else ctype
    headers = [("Content-Type", value) for value in ctypes]
    if authorization is not None:
        headers.append(("Authorization", authorization))
    r = sent_as(
        client, method, f"/atlassian{vendor_path}", headers, content=b"x" if ctype else None
    )
    assert r.status_code == status, r.text
    if isinstance(body, str):
        reason = body if body.startswith("Invalid") else f"Content type {body} not supported"
        assert r.json() == {
            "errors": [
                {"status": 415, "code": "UNSUPPORTED_MEDIA_TYPE", "title": _SPRING_415 + reason}
            ]
        }
    elif body is not None:
        assert r.content == body
        assert r.headers.get("content-type") == (
            None
            if body == b""
            else errors_atlassian.TOMCAT_MEDIA_TYPE
            if status == 400
            else _JSON_TYPE
        )


@pytest.mark.parametrize(
    "path, ctypes, accept, status, body",
    [
        # a path whose answer is the page reads no `Content-Type`
        ("/wiki/rest/api/space/ENG/nope", ("foo",), None, 404, errors_atlassian.HTML_NOT_FOUND),
        ("/wiki/rest/api/space/ENG/nope", (_JSON_TYPE, _JSON_TYPE), None, 404)
        + (errors_atlassian.HTML_NOT_FOUND,),
        # elsewhere the 400 page comes ahead of the empty 500 for an `Accept` Jersey cannot parse
        ("/wiki/rest/api/nosuch", ("foo",), "application/json;q=2", 400)
        + (errors_atlassian.tomcat_page(400),),
        ("/wiki/rest/api/nosuch", (_JSON_TYPE, _JSON_TYPE), "application/json;q=2", 400)
        + (errors_atlassian.tomcat_page(400),),
        ("/wiki/rest/api/nosuch", ("text/plain",), "application/json;q=2", 500, ""),
    ],
)
def test_confluence_unserved_path_reads_its_content_type_where_real_does(
    client, path, ctypes, accept, status, body
):
    """Pins where ``_confluence_not_found`` reads a `Content-Type` among its other answers."""
    headers = [("Content-Type", value) for value in ctypes]
    if accept is not None:
        headers.append(("Accept", accept))
    r = sent_as(client, "GET", f"/atlassian{path}", headers)
    assert (r.status_code, r.text) == (status, body)


# --- what `Accept` asks for: the 406, and the shapes a refusal takes from it --------------------

_HTML = "text/html"
_JSON = "application/json"
_PROBLEM = errors_atlassian.PROBLEM_JSON
_JIRA_JSON = errors_atlassian.JIRA_JSON_MEDIA_TYPE
_TOMCAT = errors_atlassian.TOMCAT_MEDIA_TYPE
_UNREAD_BESIDE_JSON = "application/json,*.*;q=0.9"
_BROWSER = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_INFO = "/atlassian/rest/api/3/serverInfo"
_SEARCH = "/atlassian/rest/api/3/search/jql"
_ISSUE_NOPE = "/atlassian/rest/api/3/issue/NOPE-1"
_ISSUE_BRACKET = "/atlassian/rest/api/3/issue/A%5BB"
_SPACES = "/atlassian/wiki/rest/api/space"
_ATTACH = "/atlassian/wiki/rest/api/content/1/child/attachment"
_LABEL = "/atlassian/wiki/rest/api/space/NOPESUCH/label"
_CQL = "/atlassian/wiki/rest/api/search?cql=type%3Dpage"
_LICENSE = "/atlassian/rest/api/3/license/approximateLicenseCount"
#: Tomcat's page by status (``errors.atlassian.tomcat_page``): as long as real's was, and the title
#: real's carried in its `<title>` and its `<h1>`.
_TOMCAT_PAGES = {
    400: (435, "HTTP Status 400 – Bad Request"),
    403: (431, "HTTP Status 403 – Forbidden"),
    405: (449, "HTTP Status 405 – Method Not Allowed"),
    406: (441, "HTTP Status 406 – Not Acceptable"),
    415: (457, "HTTP Status 415 – Unsupported Media Type"),
}
_PRESENT = ...

_EMPTY_HTML = errors_atlassian.HTML_MEDIA_TYPE
_COLUMNS_PRODUCE = (
    "application/json;charset=UTF-8, application/json, application/*+json, application/yaml, "
    "application/xml, text/xml, application/*+xml"
)


#: Jira's 500 page as real sent it, its length and the SHA-256 of its text with the log reference
#: and the trace id read as `<log>` and `<trace>` (``errors.atlassian.jira_oops_page``).
_JIRA_OOPS = (1703, "8f3cc6f7eac5d1b3d98f513cfc5094be6465a48d66717d8b409787c6c7a8d114")
_Q2 = "application/json;Q=2"
#: Two members, one with a `q` Spring cannot read as it sorts them
_SORT_FAILS = "application/json;Q=abc, text/plain"
#: Headers of more than 31 members, where Java's `List.sort` merges runs (``backlot.java_timsort``):
#: two that Confluence's Spring services sort without reading their `Q=abc`, one whose comparisons
#: break the sort's contract, one whose `Q=abc` they read, and one with no `Q` whose comparisons
#: break the contract because `application/*+json` sorts as a wildcard subtype, each picked by
#: running Spring's own sort in a JVM
_SORTED_33 = ", ".join(
    (
        "application/xml;q=0.9",
        "application/json",
        "application/xml;q=0",
        "application/xml;q=0",
        "text/plain;q=0.9",
        "application/json;q=0",
        "text/*",
        "application/json;q=0.5",
        "text/*;q=0.1",
        "application/xml;q=0",
        "image/png;q=0.1",
        "*/*;q=0.5",
        "application/xml;q=0.5",
        "*/*;q=0",
        "application/xml;q=0",
        "application/*;q=0.9",
        "text/*;q=0.9",
        "application/*;q=0.1",
        "text/*;q=1",
        "*/*",
        "image/png;q=0",
        "*/*;q=0.9",
        "application/xml;q=0",
        "application/*;Q=abc",
        "text/plain;q=0.1",
        "text/*;q=0.5",
        "*/*;q=0",
        "application/xml;q=0.9",
        "text/plain;q=1",
        "text/*;q=0.9",
        "text/html",
        "application/xml;q=1",
        "application/json;q=0.9",
    )
)
_SORTED_34 = ", ".join(
    (
        "text/*;q=0.9",
        "text/html;q=0",
        "application/*;q=0.1",
        "text/*;q=1",
        "image/png;q=0.9",
        "application/json;q=0.1",
        "*/*;q=0.1",
        "application/xml;q=0.1",
        "text/*;q=1",
        "text/*;q=0.5",
        "application/xml;q=0.9",
        "application/xml;q=0",
        "text/plain;q=1",
        "application/*;q=1",
        "text/plain;q=0",
        "image/png;q=0.9",
        "text/html;q=0.1",
        "application/xml;q=0",
        "image/png;q=0.9",
        "text/html;q=1",
        "*/*;q=0.5",
        "application/json;q=0.5",
        "*/*",
        "application/xml",
        "*/*;q=0.9",
        "text/html;q=0.1",
        "image/png;q=0.1",
        "application/*;q=1",
        "image/png;q=0.5",
        "text/plain;q=0.9",
        "application/*;q=0.5",
        "application/xml;q=0.5",
        "application/*;Q=abc",
        "image/png;q=1",
    )
)
_BREAKS_THE_SORT_33 = ", ".join(
    (
        "application/xml;q=0.5",
        "text/plain",
        "image/png;q=0.9",
        "text/*;q=0",
        "application/xml;q=1",
        "text/plain;q=0",
        "application/json;q=0.1",
        "text/plain;q=0.1",
        "text/html;q=1",
        "text/*;q=0",
        "application/json;q=0",
        "image/png;q=1",
        "image/png;q=0",
        "image/png;q=1",
        "application/json;q=0.9",
        "application/json;q=0.1",
        "application/json;q=0.9",
        "*/*;Q=abc",
        "application/json;q=0",
        "text/plain;q=1",
        "application/*;q=1",
        "text/*;q=0.1",
        "text/plain;q=0",
        "text/*;q=0",
        "image/png;q=0",
        "application/*;q=1",
        "application/*;q=0.1",
        "application/*",
        "application/json",
        "application/xml;q=0.1",
        "text/*",
        "text/*;q=0.5",
        "text/plain;q=1",
    )
)
_READS_THE_Q_33 = ", ".join(
    (
        "application/*;q=0.9",
        "application/json;Q=abc",
        "*/*;q=1",
        "text/*;q=0",
        "image/png;q=0",
        "text/plain;q=0.9",
        "text/plain;q=0.9",
        "text/*;q=1",
        "image/png;q=1",
        "text/*;q=0.1",
        "text/html;q=1",
        "image/png;q=0.9",
        "*/*;q=0",
        "*/*;q=1",
        "application/json;q=0.5",
        "text/html;q=0.9",
        "application/xml",
        "text/*;q=0.1",
        "application/xml;q=1",
        "text/html;q=0.1",
        "application/xml;q=0.5",
        "text/plain",
        "text/*;q=0.1",
        "application/json;q=0.9",
        "application/*;q=0.1",
        "text/*;q=0.9",
        "application/*;q=0.9",
        "text/plain;q=0.1",
        "application/json;q=0.1",
        "*/*;q=0",
        "application/*;q=0.1",
        "application/json;q=0.5",
        "application/*;q=0.5",
    )
)
_WILD_SUFFIX_32 = ", ".join(
    (
        "text/plain;q=0",
        "application/json",
        "application/xml;q=0.5",
        "text/plain;q=0.9",
        "application/json;q=0.5",
        "application/json",
        "application/*+json",
        "text/plain;q=0",
        "application/*+json",
        "application/xml;q=0.9",
        "application/*+json",
        "application/*+json",
        "application/json;q=0.9",
        "application/*;q=0",
        "text/plain",
        "application/json;q=0",
        "application/*+json;q=0.5",
        "application/*+json;q=0.5",
        "application/xml",
        "application/*+json;q=0",
        "application/xml",
        "*/*;q=0.9",
        "application/json",
        "application/*+json;q=0.5",
        "text/plain;q=0",
        "text/plain",
        "application/json;q=0",
        "text/plain",
        "application/*;q=0",
        "text/plain;q=0",
        "application/*+json",
        "text/plain",
    )
)


def _MEMBERS(count: int) -> str:
    """An `Accept` of ``count`` members, the last `application/json`."""
    return ", ".join([*(f"a/b{i}" for i in range(count - 1)), "application/json"])


_OOPS_MEDIA = "text/html;charset=utf-8"
_LABELS = "/atlassian/wiki/rest/api/content/{content}/label"


def _unparsed(accept: str) -> bytes:
    """The 400 a Confluence JAX-RS service answers an `Accept` it cannot parse with."""
    return f'The HTTP header field "Accept" with value "{accept}" could not be parsed.'.encode()


def _send_as_latin1(client, request):
    """``request``, built by the TestClient with its default headers, sent with each header value's
    bytes as they are, which :func:`tests._helpers.sent_as` says why the TestClient does not."""
    if all(value.isascii() for value in request.headers.values()):
        return client.send(request)

    async def send():
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport) as raw:
            again = raw.build_request(
                request.method,
                str(request.url),
                headers=request.headers.raw,
                content=request.read(),
            )
            return await raw.send(again)

    return asyncio.run(send())


def _out_of_bounds(code: int) -> bytes:
    """The 500 a Confluence JAX-RS service answers an `Accept` it fails on, naming the ``code`` of
    the character it fails on."""
    message = f"java.lang.ArrayIndexOutOfBoundsException: Index {code} out of bounds for length 128"
    return f'{{"statusCode":500,"message":"{message}"}}'.encode()


_ACCEPT_ROWS = [
    # (method, path, credential, Accept, (content-type, body) sent, status, content-type answered,
    # body: bytes, a shape below, or None to leave it unread, and headers: a value, `_PRESENT`, or
    # None for absent)
    #
    # -- Jira: a served route, the 406 and the values that pass it
    (
        "GET",
        _INFO,
        "none",
        _HTML,
        None,
        406,
        _PROBLEM,
        b'{"type":"about:blank","title":"Not Acceptable","status":406,"detail":"Acceptable '
        b'representations: [application/json].","instance":"/rest/api/3/serverInfo"}',
        {"accept": "application/json", "x-ratelimit-limit": None},
    ),
    ("GET", _INFO, "none", _JSON, None, 200, _JIRA_JSON, None, {"accept": None}),
    ("GET", _INFO, "none", "TEXT/HTML", None, 406, _PROBLEM, "problem", {}),
    ("GET", _INFO, "none", "text/xml", None, 406, _PROBLEM, "problem", {}),
    ("GET", _INFO, "none", "application/problem+json", None, 406, _PROBLEM, "problem", {}),
    ("GET", _INFO, "none", "text/*", None, 406, _PROBLEM, "problem", {}),
    ("GET", _INFO, "none", "image/*", None, 406, _PROBLEM, "problem", {}),
    ("GET", _INFO, "none", "application/*", None, 200, _JIRA_JSON, None, {}),
    ("GET", "/atlassian/rest/api/2/serverInfo", "none", _HTML, None, 406, _PROBLEM, "problem", {}),
    ("GET", _INFO, "none", None, None, 200, _JIRA_JSON, None, {}),
    ("GET", _INFO, "none", "", None, 200, _JIRA_JSON, None, {}),
    ("GET", _INFO, "none", "*", None, 200, _JIRA_JSON, None, {}),
    ("GET", _INFO, "none", "*/*;q=0", None, 200, _JIRA_JSON, None, {}),
    ("GET", _INFO, "none", "application/*+json", None, 200, _JIRA_JSON, None, {}),
    ("GET", _INFO, "none", "application/json;q=0, text/html", None, 200, _JIRA_JSON, None, {}),
    ("GET", _INFO, "none", _BROWSER, None, 200, _JIRA_JSON, None, {}),
    ("GET", _INFO, "none", _UNREAD_BESIDE_JSON, None, 200, _JIRA_JSON, None, {}),
    ("GET", _INFO, "none", 'application/json;q="0.5"', None, 200, _JIRA_JSON, None, {}),
    ("GET", _INFO, "none", "application/json;q=0.5f", None, 200, _JIRA_JSON, None, {}),
    ("GET", _INFO, "none", "application/json;q=0.5;q=2", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json;charset=u8", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json;charset=undefined", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json;Charset=bogus", None, 406, _EMPTY_HTML, b"", {}),
    # `foo` and `*.*`: the 406 with no body and its `Accept` kept
    (
        "GET",
        _INFO,
        "none",
        "foo",
        None,
        406,
        errors_atlassian.HTML_MEDIA_TYPE,
        b"",
        {"accept": "application/json"},
    ),
    ("GET", _INFO, "none", "*.*", None, 406, errors_atlassian.HTML_MEDIA_TYPE, b"", {}),
    # a caller whose credential resolves gets its account id and no quota on the 406
    ("GET", _INFO, "admin", _HTML, None, 406, _PROBLEM, "problem")
    + ({"x-aaccountid": _PRESENT, "x-ratelimit-limit": None},),
    ("GET", _INFO, "admin", _JSON, None, 200, _JIRA_JSON, None, {"x-ratelimit-limit": _PRESENT}),
    ("HEAD", _INFO, "none", _HTML, None, 406, _PROBLEM, b"", {"content-length": None}),
    # -- Jira: what comes before the 406, and what comes after it
    ("GET", _INFO, "unreadable-bearer", _HTML, None, 403, _JSON, None, {}),
    ("DELETE", _INFO, "none", _HTML, None, 405, _PROBLEM, None, {"allow": "GET"}),
    ("DELETE", _INFO, "none", "foo", None, 405, errors_atlassian.HTML_MEDIA_TYPE, b"", {}),
    ("POST", _SEARCH, "admin", _HTML, ("text/plain", b"x"), 415, _PROBLEM, None, {}),
    (
        "POST",
        _SEARCH,
        "admin",
        "foo",
        ("text/plain", b"x"),
        415,
        "text/html;charset=UTF-8",
        b"",
        {},
    ),
    ("POST", _SEARCH, "admin", _HTML, (_JSON, b""), 406, _PROBLEM, "problem", {}),
    ("POST", _SEARCH, "admin", _JSON, (_JSON, b""), 400, _JIRA_JSON, None, {}),
    ("GET", _ISSUE_NOPE, "admin", _HTML, None, 406, _PROBLEM, "problem", {}),
    ("GET", _ISSUE_NOPE, "admin", _JSON, None, 404, _JIRA_JSON, None, {}),
    ("GET", "/atlassian/rest/api/3/issue/NOPE-1/comment?startAt=x", "none", _HTML, None, 406)
    + (_PROBLEM, None, {}),
    ("GET", "/atlassian/rest/api/3/issue/NOPE-1/comment?startAt=x", "none", _JSON, None, 400)
    + (_PROBLEM, None, {}),
    ("GET", "/atlassian/rest/api/3/nopesuchroute", "none", _HTML, None, 404, _PROBLEM, None, {}),
    ("GET", "/atlassian/rest/api/3/nopesuchroute", "none", "foo", None, 404)
    + (errors_atlassian.HTML_MEDIA_TYPE, b"", {}),
    # -- Jira: a bracket in the path turns the 405, the 415 and the 406 into Tomcat's pages, under
    # an `Accept` that empties the RFC 7807 one too (``errors.atlassian._JIRA_BRACKET``)
    ("POST", _ISSUE_NOPE, "none", _JSON, None, 405, _PROBLEM, None)
    + ({"allow": "GET, PUT, DELETE", "content-language": None},),
    ("POST", _ISSUE_BRACKET, "none", _JSON, None, 405, _TOMCAT, "page")
    + ({"allow": "GET, PUT, DELETE", "content-language": "en"},),
    ("POST", _ISSUE_BRACKET, "none", "foo", None, 405, _TOMCAT, "page", {}),
    ("POST", _ISSUE_BRACKET, "none", None, None, 405, _TOMCAT, "page", {}),
    ("DELETE", f"{_ISSUE_BRACKET}/comment", "admin", "*/*", None, 405, _TOMCAT, "page")
    + ({"allow": "GET, POST"},),
    ("POST", f"{_ISSUE_NOPE}/comment", "none", _JSON, ("text/plain", b"x"), 415, _PROBLEM, None)
    + ({"accept": "application/json", "content-language": None},),
    ("POST", f"{_ISSUE_BRACKET}/comment", "none", _JSON, ("text/plain", b"x"), 415, _TOMCAT)
    + ("page", {"accept": "application/json", "content-language": "en"}),
    ("POST", f"{_ISSUE_BRACKET}/comment", "none", "foo", ("text/plain", b"x"), 415, _TOMCAT)
    + ("page", {}),
    ("POST", f"{_ISSUE_BRACKET}/comment", "none", _JSON, (_JSON, b"{}"), 404, _JIRA_JSON, None, {}),
    ("GET", _ISSUE_BRACKET, "none", _HTML, None, 406, _TOMCAT, "page")
    + ({"accept": "application/json", "content-language": "en"},),
    ("GET", _ISSUE_BRACKET, "none", "foo", None, 406, _TOMCAT, "page", {}),
    # the key spelled with `]`, in lower case and bare, and under `/rest/api/2`
    ("POST", "/atlassian/rest/api/3/issue/A%5DB", "none", _JSON, None, 405, _TOMCAT, "page", {}),
    ("POST", "/atlassian/rest/api/3/issue/A%5bB", "none", _JSON, None, 405, _TOMCAT, "page", {}),
    ("POST", "/atlassian/rest/api/3/issue/A[B", "none", _JSON, None, 405, _TOMCAT, "page", {}),
    ("GET", "/atlassian/rest/api/3/issue/A]B", "none", _HTML, None, 406, _TOMCAT, "page", {}),
    ("POST", "/atlassian/rest/api/2/issue/A%5BB", "none", _JSON, None, 405, _TOMCAT, "page", {}),
    # -- Jira: a published operation with no route of its own (``_published_answer``)
    ("GET", "/atlassian/rest/api/3/myself", "none", _HTML, None, 406, _PROBLEM, "problem")
    + ({"www-authenticate": None},),
    ("GET", "/atlassian/rest/api/3/myself", "none", _JSON, None, 401)
    + ("application/json;charset=UTF-8", None, {"www-authenticate": _PRESENT}),
    ("PUT", _ISSUE_NOPE, "none", _HTML, (_JSON, b"{}"), 406, _PROBLEM, "problem", {}),
    ("PUT", _ISSUE_NOPE, "none", _JSON, (_JSON, b"{}"), 404, _JIRA_JSON, None, {}),
    ("PUT", _ISSUE_NOPE, "none", _HTML, None, 415, _PROBLEM, None, {"accept": "application/json"}),
    ("POST", "/atlassian/rest/api/3/issue", "none", _HTML, ("text/plain", b"x"), 415, _PROBLEM)
    + (None, {}),
    ("POST", "/atlassian/rest/api/3/issue", "none", _HTML, None, 406, _PROBLEM, "problem", {}),
    ("GET", "/atlassian/rest/api/3/mypreferences", "none", "text/plain", None, 401)
    + ("text/plain;charset=UTF-8", None, {}),
    ("GET", "/atlassian/rest/api/3/mypreferences", "none", _HTML, None, 406, _PROBLEM)
    + ("problem, text/plain", {"accept": "application/json, text/plain"}),
    ("GET", "/atlassian/rest/api/2/mypreferences", "none", _HTML, None, 406, _PROBLEM)
    + ("problem, text/plain", {}),
    ("GET", "/atlassian/rest/api/3/license/approximateLicenseCount", "none", _HTML, None, 401)
    + ("text/html;charset=UTF-8", None, {}),
    ("GET", "/atlassian/rest/api/3/invalid-jqls-access-log", "none", _HTML, None, 401)
    + ("text/html;charset=UTF-8", None, {"www-authenticate": _PRESENT}),
    ("GET", "/atlassian/rest/api/2/invalid-jqls-access-log", "none", _HTML, None, 401)
    + ("text/html;charset=UTF-8", None, {"www-authenticate": _PRESENT}),
    ("OPTIONS", _INFO, "none", _HTML, None, 401, "text/html;charset=UTF-8", None, {}),
    # -- Confluence: a Spring route's 406 in each of its shapes, and the values that pass it
    ("GET", _SPACES, "none", _HTML, None, 406, _TOMCAT, "page", {"content-language": "en"}),
    ("GET", _SPACES, "none", "application/problem+json", None, 406)
    + ("application/problem+json", "errors", {}),
    ("GET", _SPACES, "none", "application/hal+json", None, 406)
    + ("application/hal+json", "errors", {}),
    ("GET", _SPACES, "none", "foo", None, 406, None, b"", {}),
    ("GET", _SPACES, "none", _UNREAD_BESIDE_JSON, None, 406, None, b"", {}),
    ("GET", _SPACES, "none", "application/*+json", None, 403, _JSON, None, {}),
    ("GET", _SPACES, "none", None, None, 403, _JSON, None, {}),
    ("GET", _SPACES, "unparseable-basic", _HTML, None, 401, _JSON, None)
    + ({"deprecation": None, "warning": None},),
    ("GET", f"{_SPACES}/{{space}}/permission", "unparseable-basic", _HTML, None, 401, _JSON)
    + (None, {}),
    ("GET", f"{_SPACES}?limit=x", "admin", _HTML, None, 406, _TOMCAT, "page", {}),
    ("GET", f"{_SPACES}?limit=x", "admin", _JSON, None, 400, _JSON, None, {}),
    ("GET", f"{_LABELS}?prefix=bogus", "admin", _HTML, None, 406, _TOMCAT, "page", {}),
    ("GET", f"{_LABELS}?prefix=bogus", "admin", _JSON, None, 400, _JSON, None, {}),
    ("GET", f"{_SPACES}/NOPESUCH", "admin", _HTML, None, 406, _TOMCAT, "page")
    + ({"deprecation": None},),
    *(
        ("GET", _SPACES, "admin", accept, None, 200, _JSON, None, {})
        for accept in (
            'application/json;q="0.5"',
            "application/json;q=2;q=0.5",
            "application/json;Charset=bogus",
            "application/json;charset=UTF-8;Charset=bogus",
        )
    ),
    *(
        ("GET", _SPACES, "admin", accept, None, 406, None, b"", {})
        for accept in (
            "application/json;q=0.5;q=2",
            "application/json;charset=bogus;Charset=UTF-8",
            "application/json;charset=u8",
            "application/json;charset=undefined",
        )
    ),
    ("GET", f"{_SPACES}/NOPESUCH", "admin", _JSON, None, 404, _JSON, None)
    + ({"deprecation": _PRESENT},),
    ("HEAD", _SPACES, "none", _HTML, None, 406, _TOMCAT, b"", {"content-length": "441"}),
    # -- Confluence: a JAX-RS service's 406, and the ones the catch-all answers
    ("GET", _CQL, "none", _HTML, None, 406, None, b"", {"expires": None}),
    ("GET", _CQL, "none", _UNREAD_BESIDE_JSON, None, 403, _JSON, None, {"expires": _PRESENT}),
    ("GET", "/atlassian/wiki/rest/api/audit", "none", _HTML, None, 406, None, b"", {}),
    ("GET", "/atlassian/wiki/rest/api/audit", "none", _JSON, None, 403, _JSON, None, {}),
    ("GET", "/atlassian/wiki/rest/api/contentbody/convert/async/1", "none", _HTML, None, 406)
    + (None, b"", {}),
    ("GET", "/atlassian/wiki/rest/api/content/1/history", "none", _HTML, None, 406, _TOMCAT)
    + ("page", {}),
    ("PUT", "/atlassian/wiki/rest/api/content/1", "none", _HTML, (_JSON, b"{}"), 406, _TOMCAT)
    + ("page", {}),
    ("PUT", "/atlassian/wiki/rest/api/content/1", "none", _JSON, (_JSON, b"{}"), 403, _JSON)
    + (errors_atlassian.CONFLUENCE_FORBIDDEN_BODY, {}),
    ("POST", "/atlassian/wiki/rest/api/content", "none", _JSON, ("text/plain", b"x"), 403, _JSON)
    + (errors_atlassian.CONFLUENCE_FORBIDDEN_BODY, {}),
    ("DELETE", "/atlassian/wiki/rest/api/content/1", "none", _JSON, None, 403, _JSON)
    + (errors_atlassian.CONFLUENCE_FORBIDDEN_BODY, {}),
    # the one operation whose anonymous refusal takes the shape `Accept` asks for in its place
    ("GET", _LABEL, "none", _HTML, None, 403, _TOMCAT, "page", {}),
    (
        "GET",
        _LABEL,
        "none",
        _JSON,
        None,
        403,
        _JSON,
        errors_atlassian.CONFLUENCE_FORBIDDEN_BODY,
        {},
    ),
    ("GET", _LABEL, "none", "application/problem+json", None, 403, "application/problem+json")
    + (errors_atlassian.CONFLUENCE_FORBIDDEN_BODY, {}),
    ("GET", _LABEL, "none", "foo", None, 403, None, b"", {}),
    # -- Confluence: a 405 and a 415 in the shape `Accept` asks for
    ("DELETE", _SPACES, "none", _HTML, None, 405, _TOMCAT, "page", {"allow": "GET, POST"}),
    ("DELETE", _SPACES, "none", _JSON, None, 405, _JSON, None, {"allow": None}),
    ("DELETE", _SPACES, "none", "application/problem+json", None, 405)
    + ("application/problem+json", None, {"allow": None}),
    ("DELETE", _SPACES, "none", "foo", None, 405, None, b"", {"allow": None}),
    ("GET", f"{_SPACES}/{{space}}/permission", "admin", _HTML, None, 405, _TOMCAT, "page")
    + ({"allow": "POST"},),
    # each route's `Allow` on the page (``errors.atlassian._CONFLUENCE_ALLOW``)
    ("POST", f"{_SPACES}/{{space}}", "none", _HTML, None, 405, _TOMCAT, "page")
    + ({"allow": "PUT, DELETE, GET"},),
    ("DELETE", "/atlassian/wiki/rest/api/content", "none", _HTML, None, 405, _TOMCAT, "page")
    + ({"allow": "GET, POST"},),
    ("POST", "/atlassian/wiki/rest/api/content/1", "none", _HTML, None, 405, _TOMCAT, "page")
    + ({"allow": "DELETE, GET, PUT"},),
    ("PUT", "/atlassian/wiki/rest/api/content/1/child/page", "none", _HTML, None, 405, _TOMCAT)
    + ("page", {"allow": "GET"}),
    ("PUT", "/atlassian/wiki/rest/api/content/1/child/comment", "none", _HTML, None, 405, _TOMCAT)
    + ("page", {"allow": "GET"}),
    ("DELETE", _ATTACH, "none", _HTML, None, 405, _TOMCAT, "page", {"allow": "GET, POST, PUT"}),
    ("PUT", "/atlassian/wiki/rest/api/content/1/label", "none", _HTML, None, 405, _TOMCAT, "page")
    + ({"allow": "GET, POST, DELETE"},),
    ("POST", "/atlassian/wiki/rest/api/content/1/restriction/byOperation", "none", _HTML, None)
    + (405, _TOMCAT, "page", {"allow": "GET"}),
    ("POST", _ATTACH, "none", _HTML, ("text/plain", b"x"), 415, _TOMCAT, "page")
    + ({"accept": "multipart/form-data"},),
    ("POST", _ATTACH, "none", _JSON, ("text/plain", b"x"), 415, _JSON, None, {"accept": None}),
    ("POST", _ATTACH, "none", "application/hal+json", ("text/plain", b"x"), 415)
    + ("application/hal+json", None, {"accept": None}),
    ("POST", _ATTACH, "none", "foo", ("text/plain", b"x"), 415, None, b"", {"accept": None}),
    # `search`'s own 405, which no Accept changes
    ("POST", "/atlassian/wiki/rest/api/search", "none", _JSON, None, 405, None, b"")
    + ({"allow": "HEAD,GET,OPTIONS"},),
    ("POST", "/atlassian/wiki/rest/api/search", "none", _HTML, None, 405, None, b"")
    + ({"allow": "HEAD,GET,OPTIONS"},),
    # -- a member Jira or a Spring service cannot read (``errors.atlassian.accepts``)
    ("GET", _INFO, "none", "foo, application/json", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "foo, application/json;Q=2", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json, foo", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json,*.*", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "*/json", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json;q=2", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json;q=1e0", None, 200, _JIRA_JSON, None, {}),
    ("GET", _INFO, "none", "application/json;q=\"0.5'", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json;q=10.51", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json;charset=bogus", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json;charset=base64", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json,,", None, 200, _JIRA_JSON, None, {}),
    ("GET", "/atlassian/rest/api/3/issue/{issue[key]}", "admin", "application/json;q=2", None, 406)
    + (_EMPTY_HTML, b"", {}),
    (
        "GET",
        "/atlassian/rest/api/3/issue/{issue[key]}",
        "admin",
        _JSON,
        None,
        200,
        _JIRA_JSON,
        None,
        {},
    ),
    ("GET", "/atlassian/rest/api/3/myself", "none", "application/json;charset=base64", None, 406)
    + (_EMPTY_HTML, b"", {}),
    ("GET", _SPACES, "none", "foo, application/json", None, 406, None, b"", {}),
    ("GET", _INFO, "none", "\xa0application/json", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json;x@y=1", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json;=1", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json,*.*;q=0.9\xa0", None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json;charset=utf-8;Charset=bogus", None, 406)
    + (_EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", "application/json;charset=ISO_8859-1:1987", None, 406, _EMPTY_HTML, b"")
    + ({},),
    ("GET", _SPACES, "none", "application/json;q=2", None, 406, None, b"", {}),
    ("GET", _SPACES, "admin", "\xa0application/json", None, 406, None, b"", {}),
    ("GET", _SPACES, "admin", "application/json;x@y=1", None, 406, None, b"", {}),
    ("GET", _SPACES, "admin", "application/json;=1", None, 406, None, b"", {}),
    ("GET", _SPACES, "admin", "application/json;x=a@b", None, 406, None, b"", {}),
    ("GET", _SPACES, "admin", "text/plain;charset=ISO_8859-1:1987, application/json", None, 200)
    + (_JSON, None, {}),
    ("GET", _SPACES, "admin", "application/json;charset=bogus;Charset=utf-8", None, 406, None, b"")
    + ({},),
    ("GET", _SPACES, "admin", 'text/plain;q="abc";Q=0.5, application/json', None, 200, _JSON, None)
    + ({},),
    ("GET", _SPACES, "none", "*/json", None, 406, None, b"", {}),
    ("GET", _SPACES, "none", "application/json,,", None, 403, _JSON, None, {}),
    (
        "GET",
        "/atlassian/wiki/rest/api/content/{content}",
        "admin",
        "application/json;charset=utf-7",
        None,
        406,
        None,
        b"",
        {},
    ),
    ("GET", "/atlassian/wiki/rest/api/content/{content}", "admin", _JSON, None, 200, _JSON, None)
    + ({},),
    # -- Confluence's JAX-RS services read it their own way (``errors.atlassian._jaxrs_accept``)
    ("GET", _CQL, "admin", "application/*+json", None, 406, None, b"", {}),
    ("GET", _CQL, "admin", "*/json", None, 200, _JSON, None, {}),
    # no `Accept` and an empty one admit JSON there as anywhere
    ("GET", _CQL, "admin", None, None, 200, _JSON, None, {}),
    ("GET", _CQL, "admin", "", None, 200, _JSON, None, {}),
    ("GET", _CQL, "none", "", None, 403, _JSON, None, {}),
    ("GET", _CQL, "none", "*/*+json", None, 403, _JSON, None, {}),
    ("GET", _CQL, "none", "foo, application/json", None, 403, _JSON, None, {}),
    ("GET", _CQL, "none", "foo, *.*", None, 406, None, b"", {}),
    ("GET", _CQL, "admin", 'application/json;x="a,b", text/plain', None, 200, _JSON, None, {}),
    ("GET", _CQL, "admin", 'application/json;x="\xa0"', None, 200, _JSON, None, {}),
    # a character past ASCII outside a quoted string (``errors.atlassian.jaxrs_out_of_bounds``),
    # the code of the one Jersey fails on
    *(
        (method, path, credential, accept, sent, 500, _JSON, _out_of_bounds(code), {})
        for method, path, credential, accept, sent, code in (
            ("GET", _CQL, "admin", "\xa0application/json", None, 160),
            ("GET", _CQL, "none", "\xa0application/json", None, 160),
            ("GET", _CQL, "admin", "application/json;foo\xa0", None, 160),
            ("GET", _CQL, "admin", "foo, \xa0", None, 160),
            ("GET", _CQL, "admin", "\x80application/json", None, 128),
            ("GET", _CQL, "admin", "application/json, \x85\xa0", None, 133),
            ("GET", _CQL, "admin", "foo;\xa0", None, 160),
            ("GET", _CQL, "admin", "*\xa0", None, 160),
            ("GET", _CQL, "admin", "\xa0*", None, 160),
            ("GET", _CQL, "admin", "*;\xa0", None, 160),
            ("GET", _CQL, "admin", "\xa0, application/json", None, 160),
            ("GET", _CQL, "admin", 'application/json;x="\xa0", \x85', None, 133),
            ("GET", _CQL, "admin", 'application/json;x="\\"\xa0", \x85', None, 133),
            ("GET", _CQL, "admin", 'application/json;x="a"\xa0', None, 160),
            (
                "OPTIONS",
                "/atlassian/wiki/rest/api/search",
                "admin",
                "\xa0application/json",
                None,
                160,
            ),
            ("GET", "/atlassian/wiki/rest/api/audit", "none", "\xa0application/json", None, 160),
            ("POST", "/atlassian/wiki/rest/api/template", "none", "\xa0application/json")
            + (("text/plain", b"x"), 160),
        )
    ),
    *(
        ("GET", _CQL, "admin", accept, None, 400, "text/plain", _unparsed(accept), {})
        for accept in (
            "application/json;foo, \xa0",
            "application/json;q=2, \xa0",
            "ap@p\xa0lication/json",
            "ap@p\xa0, \xa0",
            "foo@\xa0",
            "foo;@\xa0",
            "application/json, ,\xa0",
            'application/json;x="\xa0", application/json;foo',
            'application/json;x="\\"\xa0", application/json;foo',
        )
    ),
    ("GET", _CQL, "admin", 'application/json;x="a\\",b", text/plain', None, 200, _JSON, None, {}),
    ("GET", f"{_SPACES}/MFS/nope", "none", "\xa0application/json", None, 404)
    + (errors_atlassian.HTML_MEDIA_TYPE, errors_atlassian.HTML_NOT_FOUND.encode(), {}),
    ("GET", "/atlassian/wiki/rest/api/nopesuchroute", "none", "\xa0application/json", None, 500)
    + (None, b"", {"cache-control": None}),
    ("GET", "/atlassian/wiki/rest/api/nopesuchroute", "none", None, None, 404, "application/xml")
    + (None, {}),
    # Jersey under a header it cannot parse: the 404's 500, and a write's 400 ahead of its 415
    # (``backlot.routers.atlassian._confluence_not_found``, ``_published_answer``)
    *(
        ("GET", "/atlassian/wiki/rest/api/nopesuchroute", "none", accept, None, 500, None, b"")
        + ({"cache-control": None},)
        for accept in (
            "application/json;q=2",
            "application/json,,text/html",
            "application/json;foo",
        )
    ),
    ("GET", "/atlassian/wiki/rest/api/nope/deeper", "admin", "application/json;q=2", None, 500)
    + (None, b"", {}),
    ("POST", "/atlassian/wiki/rest/api/template", "none", "application/json;q=2")
    + (("text/plain", b"x"), 400, "text/plain", _unparsed("application/json;q=2"), {}),
    ("POST", "/atlassian/wiki/rest/api/content/archive", "none", "application/json;q=2")
    + (("text/plain", b"x"), 400, "text/plain", _unparsed("application/json;q=2"), {}),
    ("POST", "/atlassian/wiki/rest/api/template", "none", "application/json;q=2", ("foo", b"x"))
    + (400, _TOMCAT, "page", {}),
    # an operation that answers in other types than JSON (``_CONFLUENCE_PRODUCES``)
    ("GET", "/atlassian/wiki/rest/api/audit/export", "none", _JSON, None, 406, None, b"", {}),
    ("GET", "/atlassian/wiki/rest/api/audit/export", "none", "application/octet-stream", None)
    + (406, None, b"", {}),
    *(
        ("GET", "/atlassian/wiki/rest/api/audit/export", "none", accept, None, 403, _JSON)
        + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY, {})
        for accept in ("application/zip", "text/csv", "*/*")
    ),
    ("GET", _CQL, "admin", "application/json;charset=utf-7", None, 200, _JSON, None, {}),
    ("GET", _CQL, "none", "application/json,", None, 403, _JSON, None, {}),
    ("GET", _CQL, "none", "*;q=0.5", None, 403, _JSON, None, {}),
    ("GET", _CQL, "none", "application/json;q=.5", None, 403, _JSON, None, {}),
    (
        "GET",
        _CQL,
        "none",
        "application/json,,",
        None,
        400,
        "text/plain",
        _unparsed("application/json,,"),
    )
    + ({"expires": None},),
    (
        "GET",
        _CQL,
        "admin",
        ",application/json",
        None,
        400,
        "text/plain",
        _unparsed(",application/json"),
    )
    + ({"cache-control": None},),
    ("GET", _CQL, "admin", "application/json;q=2", None, 400, "text/plain")
    + (_unparsed("application/json;q=2"), {}),
    *(
        ("GET", _CQL, "admin", accept, None, 400, "text/plain", _unparsed(accept), {})
        for accept in (
            "application/json;foo",
            "application/json;q= 0.5",
            "application/json;q=0.5;q=2",
            "application/json;Q=2",
            "application/json;q=1.0000",
            "application/json;q=1.5",
            "application/json;q=1.01",
            "application/json;q=0.5f",
            "foo;x, application/json",
        )
    ),
    *(
        ("GET", _CQL, "admin", accept, None, 200, _JSON, None, {})
        for accept in (
            'application/json;q="0.5"',
            "application/json;q=2;q=0.5",
            "application/json; q=0.5",
        )
    ),
    ("DELETE", "/atlassian/wiki/rest/api/template/1", "none", '"0.5"', None, 400, "text/plain")
    + (_unparsed('"0.5"'), {}),
    ("HEAD", _CQL, "none", _HTML, None, 406, None, b"", {"content-length": None}),
    (
        "GET",
        "/atlassian/wiki/rest/api/audit",
        "none",
        "application/*+json",
        None,
        406,
        None,
        b"",
        {},
    ),
    ("GET", "/atlassian/wiki/rest/api/audit", "none", "*/json", None, 403, _JSON, None, {}),
    # a JAX-RS write: its 415 before the 406, its 406 with no body, and the refusal past both
    ("POST", "/atlassian/wiki/rest/api/template", "none", _HTML, ("text/plain", b"x"), 415, None)
    + (b"", {}),
    (
        "POST",
        "/atlassian/wiki/rest/api/template",
        "none",
        _HTML,
        (_JSON, b"{}"),
        406,
        None,
        b"",
        {},
    ),
    ("POST", "/atlassian/wiki/rest/api/template", "none", _JSON, (_JSON, b"{}"), 403, _JSON)
    + (errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY, {}),
    # `space/{key}/label`'s writes answer as its GET does
    ("POST", _LABEL, "none", _HTML, (_JSON, b"{}"), 403, _TOMCAT, "page", {}),
    ("DELETE", _LABEL, "none", _HTML, None, 403, _TOMCAT, "page", {}),
    # -- a Jira write that checks no `Accept` (its entry's ``accept``), beside one that does
    ("DELETE", "/atlassian/rest/api/3/projectCategory/1", "none", _HTML, None, 401, _EMPTY_HTML)
    + (None, {}),
    ("PUT", "/atlassian/rest/api/3/plans/plan/1", "none", _HTML)
    + (("application/json-patch+json", b"{}"), 401, _EMPTY_HTML, None, {}),
    (
        "DELETE",
        "/atlassian/rest/api/3/screens/1",
        "none",
        _HTML,
        None,
        406,
        _PROBLEM,
        "problem",
        {},
    ),
    # a write whose 406 names more than JSON (its entry's ``produces``)
    ("PUT", "/atlassian/rest/api/3/mypreferences", "none", _HTML, (_JSON, b"{}"), 406, _PROBLEM)
    + ("problem, text/plain", {"accept": "application/json, text/plain"}),
    ("DELETE", "/atlassian/rest/api/3/filter/1/columns", "none", _HTML, None, 406, _PROBLEM, None)
    + ({"accept": _COLUMNS_PRODUCE},),
    ("DELETE", "/atlassian/rest/api/2/filter/1/columns", "none", _HTML, None, 406, _PROBLEM, None)
    + ({"accept": _COLUMNS_PRODUCE},),
    # -- a `q` named in another case that Spring cannot read
    # (``backlot.routers.atlassian.spring_write_failure``), on Jira
    *(
        ("GET", path, credential, accept, None, 500, _OOPS_MEDIA, "oops", {})
        for path, credential, accept in (
            (_INFO, "none", _Q2),
            (_INFO, "none", "application/json;Q=abc"),
            (_INFO, "none", "*/*;Q=2"),
            (_INFO, "none", "application/*;Q=2"),
            (_INFO, "none", "application/*+json;Q=2"),
            (_INFO, "none", "application/json;q=0.5;Q=2"),
            (_INFO, "admin", "application/json;q=0.5;Q=2;q=0.5"),
            ("/atlassian/rest/api/3/issue/{issue[key]}/comment?orderBy=bogus", "admin", _Q2),
            ("/atlassian/rest/api/3/search/jql?jql=project%3D{project}", "admin", _Q2),
        )
    ),
    ("GET", "/atlassian/rest/api/3/myself", "none", _Q2, None, 500, _OOPS_MEDIA, "oops")
    + ({"www-authenticate": _PRESENT},),
    ("GET", "/atlassian/rest/api/3/myself", "none", "application/json;q=0.5;Q=2;q=0.5", None)
    + (500, _OOPS_MEDIA, "oops", {"www-authenticate": _PRESENT}),
    ("HEAD", _INFO, "none", _Q2, None, 500, _OOPS_MEDIA, b"", {"content-length": "1703"}),
    ("OPTIONS", _INFO, "none", _Q2, None, 500, _OOPS_MEDIA, b"")
    + ({"allow": "GET, HEAD, POST, PUT, DELETE, OPTIONS, TRACE", "www-authenticate": _PRESENT},),
    ("GET", _INFO, "none", "application/json;Q=0.5;q=0.5", None, 200, _JIRA_JSON, None, {}),
    ("GET", _INFO, "none", "text/html;Q=2, application/json", None, 200, _JIRA_JSON, None, {}),
    ("GET", _INFO, "none", "text/json;Q=2, application/json", None, 200, _JIRA_JSON, None, {}),
    ("OPTIONS", _INFO, "admin", _Q2, None, 200, errors_atlassian.JIRA_OPTIONS_MEDIA_TYPE, b"")
    + ({"allow": "GET,HEAD,OPTIONS"},),
    # and not on another source's path
    ("GET", "/health", "none", _Q2, None, 200, _JSON, None, {}),
    ("GET", _ISSUE_NOPE, "none", _Q2, None, 404, _JIRA_JSON, None, {}),
    ("GET", _SEARCH, "admin", _Q2, None, 400, _JIRA_JSON, None, {}),
    ("POST", _INFO, "none", _Q2, None, 405, _PROBLEM, None, {}),
    # and on Confluence
    ("GET", _SPACES, "admin", _Q2, None, 400, None, b"", {"content-length": "0"}),
    ("HEAD", _SPACES, "admin", _Q2, None, 400, None, b"", {"content-length": None}),
    ("GET", "/atlassian/wiki/rest/api/content/{content}", "admin", _Q2, None, 400, None, b"", {}),
    ("GET", _SPACES, "none", _Q2, None, 403, _TOMCAT, "page", {}),
    ("GET", f"{_SPACES}?limit=abc", "admin", _Q2, None, 400, _TOMCAT, "page", {}),
    ("GET", f"{_SPACES}?limit=-1", "admin", _Q2, None, 400, None, b"", {}),
    ("GET", "/atlassian/wiki/rest/api/content?type=bogus", "admin", _Q2, None, 501, None, b"", {}),
    ("DELETE", _SPACES, "admin", _Q2, None, 405, _TOMCAT, "page", {"allow": "GET, POST"}),
    ("POST", _ATTACH, "admin", _Q2, ("text/plain", b"x"), 415, _TOMCAT, "page")
    + ({"accept": "multipart/form-data"},),
    ("POST", _ATTACH, "none", _Q2, ("multipart/form-data; boundary=x", b"x"), 403, _TOMCAT)
    + ("page", {"accept": None}),
    ("PUT", "/atlassian/wiki/rest/api/content/1/state", "none", _Q2, ("text/plain", b"x"), 415)
    + (_TOMCAT, "page", {"accept": "application/json"}),
    ("GET", _SPACES, "admin", "application/json;Q=2;Charset=bogus", None, 400, None, b"", {}),
    ("GET", _SPACES, "admin", "application/json;q=0.5;Q=2;q=0.5", None, 400, None, b"", {}),
    ("OPTIONS", _SPACES, "admin", _Q2, None, 403, _TOMCAT, "page", {}),
    ("GET", f"{_SPACES}/NOPESUCH", "admin", _Q2, None, 404, errors_atlassian.HTML_MEDIA_TYPE)
    + (errors_atlassian.HTML_NOT_FOUND.encode(), {}),
    ("GET", "/atlassian/wiki/rest/api/nopesuchroute", "admin", _Q2, None, 500, None, b"", {}),
    # a run of slashes is one before the product is picked (``backlot.main._spring_failure``)
    ("GET", "/atlassian//wiki/rest/api/space", "admin", _Q2, None, 400, None, b"", {}),
    ("GET", "/atlassian//wiki/rest/api/content", "admin", _Q2, None, 400, None, b"", {}),
    ("OPTIONS", "/atlassian//wiki/rest/api/space", "admin", _Q2, None, 403, _TOMCAT, "page", {}),
    ("GET", "/atlassian//wiki/rest/api/space", "admin", _SORTED_33, None, 400, None, b"", {}),
    # Jersey writes its own 404, an `OPTIONS` of it too, under a `Q` Spring would fail to copy
    (
        "OPTIONS",
        "/atlassian/wiki/rest/api/nopesuchroute",
        "admin",
        "application/json;q=0.5;Q=2;q=0.5",
    )
    + (None, 404, errors_atlassian.JAXRS_JSON_MEDIA_TYPE, None, {}),
    ("OPTIONS", "/atlassian/wiki/rest/api/nopesuchroute", "admin", _Q2, None, 500, None, b"", {}),
    # the product's page, which Spring does not write either
    *(
        (
            method,
            f"{_SPACES}/{{space}}/nope",
            "admin",
            accept,
            None,
            404,
            errors_atlassian.HTML_MEDIA_TYPE,
        )
        + (errors_atlassian.HTML_NOT_FOUND.encode(), {})
        for method in ("GET", "OPTIONS")
        for accept in (_Q2, "application/json;q=0.5;Q=2;q=0.5")
    ),
    # the service layer's own 404, and a JSON member that copies its `q` beside one Spring skips
    ("GET", "/atlassian/wiki/rest/api/content/999999999", "admin", _Q2, None, 404, None, b"", {}),
    ("GET", _SPACES, "admin", "application/xml;Q=2, application/json", None, 200, _JSON, None, {}),
    # a `charset` Spring finds no quotes around, the one at its start being unmatched
    ("GET", _SPACES, "admin", 'application/json;charset="utf-8x', None, 406, None, b"", {}),
    # a credential that resolves gets what an unserved path gets on `space/{key}/label`, where
    # real runs it; the 403 in `Accept`'s shape is the anonymous caller's alone
    (
        "GET",
        f"{_SPACES}/{{space}}/label",
        "admin",
        None,
        None,
        404,
        errors_atlassian.HTML_MEDIA_TYPE,
    )
    + (errors_atlassian.HTML_NOT_FOUND.encode(), {}),
    # Confluence answers a bearer it cannot read with its own 403, stamped as its answers are
    ("GET", _SPACES, "unreadable-bearer", None, None, 403, _JSON, None)
    + ({"x-confluence-request-time": _PRESENT},),
    ("GET", _SPACES, "admin", "text/html;Q=2", None, 406, _TOMCAT, "page", {}),
    ("HEAD", f"{_SPACES}/NOPESUCH", "admin", _Q2, None, 404, errors_atlassian.HTML_MEDIA_TYPE, b"")
    + ({"content-length": None},),
    ("OPTIONS", _CQL, "admin", _Q2, None, 400, "text/plain", _unparsed(_Q2), {}),
    ("GET", _SPACES, "unparseable-basic", _Q2, None, 401, _JSON, None, {}),
    ("OPTIONS", "/atlassian/wiki/rest/api/nope<script>", "admin", _Q2, None, 403, "text/html", None)
    + ({"content-length": "919"},),
    ("GET", _CQL, "admin", 'application/json;x="a;b"', None, 200, _JSON, None, {}),
    # -- an `Accept` Spring fails to sort (``errors.atlassian.spring_cannot_sort``,
    # ``backlot.routers.atlassian.spring_sort_failure``)
    *(
        (
            method,
            path,
            credential,
            accept,
            sent,
            500,
            _OOPS_MEDIA,
            "oops",
            {"www-authenticate": None},
        )
        for method, path, credential, accept, sent in (
            ("GET", _INFO, "none", _SORT_FAILS, None),
            ("GET", _INFO, "none", "text/plain;Q=abc, application/json", None),
            ("GET", _INFO, "none", "text/plain;Q=abc, text/html;q=0.5", None),
            ("GET", _INFO, "none", "*;Q=abc, application/json", None),
            ("GET", _INFO, "none", "application/json, application/json;Q=abc", None),
            ("GET", _INFO, "none", 'application/json;Q="abc", text/plain', None),
            ("GET", "/atlassian/rest/api/3/myself", "none", _SORT_FAILS, None),
            ("GET", "/atlassian/rest/api/3/myself", "admin", _SORT_FAILS, None),
            ("GET", "/atlassian/rest/api/3/nopesuchroute", "none", _SORT_FAILS, None),
            ("GET", _ISSUE_NOPE, "none", _SORT_FAILS, None),
            ("POST", _INFO, "none", _SORT_FAILS, None),
            ("PUT", "/atlassian/rest/api/3/announcementBanner", "none", _SORT_FAILS)
            + (("text/plain", b"x"),),
            ("GET", _LICENSE, "none", _SORT_FAILS, None),
        )
    ),
    ("HEAD", _INFO, "none", _SORT_FAILS, None, 500, _OOPS_MEDIA, b"", {"content-length": "1703"}),
    *(
        ("OPTIONS", _INFO, credential, accept, None, 500, _OOPS_MEDIA, b"")
        + ({"allow": "GET, HEAD, POST, PUT, DELETE, OPTIONS, TRACE", "www-authenticate": None},)
        for credential, accept in (
            ("none", _SORT_FAILS),
            ("admin", _SORT_FAILS),
            ("unreadable-bearer", _SORT_FAILS),
            ("none", "*;Q=abc, application/json"),
            ("none", "text/plain, text/html, application/json;Q=abc"),
        )
    ),
    # a header Spring sorts: a lone member, `NaN`, a hexadecimal `q`
    ("GET", "/atlassian/rest/api/3/myself", "none", "application/json;Q=abc", None, 500)
    + (_OOPS_MEDIA, "oops", {"www-authenticate": _PRESENT}),
    ("GET", "/atlassian/rest/api/3/myself", "none", "application/json;Q=abc,", None, 500)
    + (_OOPS_MEDIA, "oops", {"www-authenticate": _PRESENT}),
    ("OPTIONS", _INFO, "none", "application/json;Q=NaN, text/plain", None, 500, _OOPS_MEDIA, b"")
    + ({"www-authenticate": _PRESENT},),
    ("OPTIONS", _INFO, "none", "application/json;Q=0x1p-1, text/plain", None, 401)
    + ("text/plain;charset=UTF-8", errors_atlassian.JIRA_UNAUTHENTICATED.encode(), {}),
    ("OPTIONS", _INFO, "none", "application/json;Q=0X1p-1, text/plain", None, 401)
    + ("text/plain;charset=UTF-8", errors_atlassian.JIRA_UNAUTHENTICATED.encode(), {}),
    ("GET", _INFO, "none", "foo, application/json;Q=abc, text/plain", None, 406, _EMPTY_HTML, b"")
    + ({},),
    ("GET", _INFO, "none", "application/json;q=-1", None, 406, _EMPTY_HTML, b"", {}),
    # the member limit (``errors.atlassian._accept``)
    ("GET", _INFO, "none", _MEMBERS(51), None, 406, _EMPTY_HTML, b"", {}),
    ("GET", _INFO, "none", _MEMBERS(50), None, 200, _JIRA_JSON, None, {}),
    ("OPTIONS", _INFO, "none", _MEMBERS(51), None, 401, _EMPTY_HTML, b"")
    + ({"allow": "GET, HEAD, POST, PUT, DELETE, OPTIONS, TRACE, PATCH"},),
    ("GET", _SPACES, "admin", _MEMBERS(51), None, 200, _JSON, None, {}),
    # and on Confluence, by its own sort (``errors.atlassian.spring_cannot_sort``)
    *(
        (method, path, credential, accept, None, 500, None, b"", {})
        for method, path, credential, accept in (
            ("GET", _SPACES, "admin", _SORT_FAILS),
            ("GET", _SPACES, "none", _SORT_FAILS),
            ("GET", _SPACES, "admin", "text/plain;Q=abc, application/json"),
            ("GET", _SPACES, "admin", "*/*;Q=abc, */*"),
            ("GET", _SPACES, "admin", "a/b, text/plain;Q=abc, application/json"),
            ("GET", f"{_SPACES}/NOPESUCH", "admin", _SORT_FAILS),
            ("DELETE", _SPACES, "admin", _SORT_FAILS),
            ("OPTIONS", _SPACES, "admin", _SORT_FAILS),
            ("GET", "/atlassian/wiki/rest/api/nopesuchroute", "none", _SORT_FAILS),
        )
    ),
    ("GET", f"{_SPACES}?limit=abc", "admin", _SORT_FAILS, None, 500, None, b"", {}),
    # three and four members, where which pairs Java's sort compares decides it
    *(
        ("GET", _SPACES, "admin", accept, None, 500, None, b"", {})
        for accept in (
            "application/json, application/json, application/json;Q=abc",
            "application/json;Q=abc, */*, application/json",
            "text/plain;Q=abc, application/*+json",
        )
    ),
    *(
        ("GET", _SPACES, "admin", accept, None, 400, None, b"", {})
        for accept in (
            "*;Q=abc, application/json",
            "application/json, */*;Q=abc",
            "application/json;Q=abc, application/*",
            "*/*;Q=abc, text/*",
            "application/json, */*;Q=abc, application/json",
            "application/json, text/plain, application/json, application/*;Q=abc",
            "application/*;Q=abc, application/json;q=0.5, application/json, text/plain",
            "application/json, application/*;Q=abc, application/json;q=0.5, text/plain",
        )
    ),
    # the comparisons ``errors.atlassian._sorted_alike`` makes, and past 31 members the sort
    # ``backlot.java_timsort`` runs
    *(
        ("GET", _SPACES, "admin", accept, None, status, None, b"", {})
        for accept, status in (
            ("text/plain;Q=abc, text/*;Q=-0, application/json;q=0", 500),
            ("application/*;Q=abc, application/json, text/plain;Q=NaN", 400),
            ("application/json;Q=abc, application/*, text/plain;Q=NaN", 500),
            ("text/plain, application/json;Q=NaN, application/*;Q=abc", 500),
            (
                "application/json;x=1, application/json;x=1;x=2, application/*;Q=abc, text/html;Q=2",
                500,
            ),
            (
                "application/json;x=1, application/json;x=1;X=2, application/*;Q=abc, text/html;Q=2",
                500,
            ),
            (_SORTED_33, 400),
            (_SORTED_34, 400),
            (_BREAKS_THE_SORT_33, 500),
            (_READS_THE_Q_33, 500),
            (_WILD_SUFFIX_32, 500),
            ("text/*;Q=abc, text/html;Q=-2, application/json;Q=-1", 400),
            ("application/json;Q=-2, text/plain;Q=-1, text/*;Q=abc", 500),
            ("application/json;Q=-1, text/*;Q=-2, text/html;Q=abc", 500),
        )
    ),
    ("GET", "/atlassian/wiki/rest/api/audit", "none", _SORT_FAILS, None, 400, "text/plain")
    + (_unparsed(_SORT_FAILS), {}),
    ("GET", _SPACES, "unparseable-basic", _SORT_FAILS, None, 401, _JSON, None, {}),
]

_ACCEPT_CREDENTIALS = {
    "none": {},
    "unreadable-bearer": {"Authorization": "Bearer nope"},
    "unparseable-basic": {"Authorization": "Basic !!!"},
}


def _accept_body(shape, status: int, vendor_path: str) -> bytes:
    """The body a row of :data:`_ACCEPT_ROWS` names by its shape."""
    if shape == "page":
        return errors_atlassian.tomcat_page(status).encode()
    if shape == "errors":
        return json.dumps(
            {
                "errors": [
                    {
                        "status": 406,
                        "code": "NOT_ACCEPTABLE",
                        "title": errors_atlassian.CONFLUENCE_NOT_ACCEPTABLE,
                    }
                ]
            },
            separators=(",", ":"),
        ).encode()
    named = "application/json" + (", text/plain" if shape == "problem, text/plain" else "")
    return json.dumps(
        {
            "type": "about:blank",
            "title": "Not Acceptable",
            "status": 406,
            "detail": f"Acceptable representations: [{named}].",
            "instance": vendor_path,
        },
        separators=(",", ":"),
    ).encode()


#: Jira's 401 under each `Accept` (``errors.atlassian.jira_unauthenticated``), on
#: `OPTIONS serverInfo` and, for the `GET` rows, `GET myself` with no credential: (method, Accept,
#: status, content-type, the body as ``_LINE``, ``_UTF16_LINE`` or none, headers beside the realm).
_LINE = errors_atlassian.JIRA_UNAUTHENTICATED.encode()
_UTF16_LINE = b"\xfe\xff" + errors_atlassian.JIRA_UNAUTHENTICATED.encode("utf-16-be")
_UTF32_LINE = errors_atlassian.JIRA_UNAUTHENTICATED.encode("utf-32-be")
_UNREAD_ALLOW = {"allow": "GET, HEAD, POST, PUT, DELETE, OPTIONS, TRACE, PATCH"}
_JIRA_401 = [
    ("OPTIONS", None, 401, "text/plain;charset=UTF-8", _LINE, {"x-frame-options": None}),
    ("OPTIONS", "", 401, "text/plain;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "*", 401, "text/plain;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "*;q=0.5", 401, "text/plain;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "*, application/json", 401, "application/json;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "application/json, *", 401, "application/json;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "text/plain, *", 401, "text/plain;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "*/*", 401, "text/html;charset=UTF-8", _LINE, {"x-frame-options": "SAMEORIGIN"}),
    ("OPTIONS", "*/*;q=0", 401, "text/html;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "application/json, */*;q=0.1", 401, "text/html;charset=UTF-8", _LINE, {}),
    ("OPTIONS", _BROWSER, 401, "text/html;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "text/html", 401, "text/html;charset=UTF-8", _LINE, {"x-frame-options": _PRESENT}),
    ("OPTIONS", "TEXT/HTML", 401, "text/html;charset=UTF-8", _LINE, {}),
    ("OPTIONS", _JSON, 401, "application/json;charset=UTF-8", _LINE, {"x-frame-options": None}),
    ("OPTIONS", "APPLICATION/JSON", 401, "application/json;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "application/*", 401, "application/json;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "application/json;q=0", 401, "application/json;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "application/json;q=1e0", 401, "application/json;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "application/json,", 401, "application/json;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "application/json;charset=utf-8", 401, "application/json;charset=utf-8", _LINE, {}),
    ("OPTIONS", 'application/json;charset="utf-8"', 401, "application/json;charset=utf-8", _LINE)
    + ({},),
    ("OPTIONS", "application/json;charset=ISO-8859-1", 401)
    + ("application/json;charset=ISO-8859-1", _LINE, {}),
    ("OPTIONS", "application/json, text/plain", 401, "application/json;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "text/plain, application/json", 401, "text/plain;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "application/json;q=0.5, text/plain", 401, "text/plain;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "application/json, image/*", 401, "application/json;charset=UTF-8", _LINE, {}),
    ("OPTIONS", _UNREAD_BESIDE_JSON, 401, "application/json;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "text/plain", 401, "text/plain;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "text/*", 401, "text/plain;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "text/xml", 401, "text/xml;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "application/xml", 401, "application/xml;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "application/hal+json", 401, "application/hal+json;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "foo/bar", 401, "foo/bar;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "image/png", 401, "image/png;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "text/plain;charset=utf-16", 401, "text/plain;charset=utf-16", _UTF16_LINE, {}),
    ("OPTIONS", "application/json;charset=utf16", 401, "application/json;charset=utf16")
    + (_UTF16_LINE, {}),
    ("OPTIONS", "application/json;charset=utf-32", 401, "application/json;charset=utf-32")
    + (_UTF32_LINE, {}),
    ("OPTIONS", "application/json;charset=x-MacRoman", 401)
    + ("application/json;charset=x-MacRoman", _LINE, {}),
    ("OPTIONS", "application/*+json;charset=utf-16", 401, "application/json;charset=UTF-8", _LINE)
    + ({},),
    ("OPTIONS", "application/*+json;q=0.5, text/plain", 401, "text/plain;charset=UTF-8", _LINE)
    + ({},),
    ("OPTIONS", "application/json;level=1", 401, "application/json;level=1;charset=UTF-8", _LINE)
    + ({},),
    ("OPTIONS", "text/plain;x=y", 401, "text/plain;x=y;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "application/json;q=0.5", 401, "application/json;charset=UTF-8", _LINE, {}),
    ("OPTIONS", 'application/json;q="0.5"', 401, "application/json;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "application/json;q=2;q=0.5", 401, "application/json;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "text/htm, application/json", 401, "text/htm;charset=UTF-8", _LINE, {}),
    ("OPTIONS", 'application/json;x="a;b"', 401, 'application/json;x="a;b";charset=UTF-8', _LINE)
    + ({},),
    ("OPTIONS", "text/plain;x=0.1, application/json;q=0.5", 401, "text/plain;x=0.1;charset=UTF-8")
    + (_LINE, {}),
    ("OPTIONS", "application/json;charset=UnicodeBig", 401, "application/json;charset=UnicodeBig")
    + (_UTF16_LINE, {}),
    # a `q` named in another case, in the type
    *(
        ("OPTIONS", accept, 401, media_type, _UTF16_LINE if "utf-16" in accept else _LINE, {})
        for accept, media_type in (
            ("application/json;Q=0.5", "application/json;Q=0.5;charset=UTF-8"),
            ("application/json;level=1;Q=0.5", "application/json;level=1;Q=0.5;charset=UTF-8"),
            ("application/hal+json;Q=0.5", "application/hal+json;Q=0.5;charset=UTF-8"),
            ("application/json;q=0.5;Q=0.5", "application/json;Q=0.5;charset=UTF-8"),
            ("application/json;charset=utf-16;Q=0.5", "application/json;q=0.5;charset=utf-16"),
            ("text/plain;Q=0.5;x=y", "text/plain;q=0.5;x=y;charset=UTF-8"),
            ("application/xml;Q=0.5", "application/xml;q=0.5;charset=UTF-8"),
            ("application/json;charset=utf-8;level=1", "application/json;level=1;charset=utf-8"),
            ("application/json;level=1;q=0.5", "application/json;level=1;charset=UTF-8"),
        )
    ),
    ("OPTIONS", "text/plain;q=0.1;q=1, application/json", 401, "text/plain;charset=UTF-8", _LINE)
    + ({},),
    # the parameters kept, and members split outside quotes (``errors.atlassian._accept``)
    *(
        ("GET", accept, 401, media_type, _LINE, {})
        for accept, media_type in (
            ("application/json;q=Infinity;Q=0.5", "application/json;Q=0.5;charset=UTF-8"),
            ('text/plain;q="abc";Q=0.5, application/json', "application/json;charset=UTF-8"),
            ('application/json;x="a,b", text/plain', 'application/json;x="a,b";charset=UTF-8'),
            (
                'application/json;x="a\\",b", text/plain',
                'application/json;x="a\\",b";charset=UTF-8',
            ),
            ("application/json;Charset=bogus;charset=utf-8", "application/json;charset=utf-8"),
            ("application/json;charset=bogus;Charset=utf-8", "application/json;charset=utf-8"),
            (
                'application/json;charset="ISO_8859-1:1987"',
                "application/json;charset=ISO_8859-1:1987",
            ),
        )
    ),
    ("OPTIONS", "text/plain;q=1;q=0.1, application/json", 401, "application/json;charset=UTF-8")
    + (_LINE, {}),
    # `text/html` first found past the header's start is the page's type, ahead of the `q` order
    ("OPTIONS", "application/json, text/html", 401, "text/html;charset=UTF-8", _LINE)
    + ({"x-frame-options": "SAMEORIGIN"},),
    ("OPTIONS", "application/json, text/html;charset=utf-16", 401, "text/html;charset=UTF-8")
    + (_LINE, {}),
    ("OPTIONS", "image/*, text/html", 401, "text/html;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "text/html;q=0.1, application/json, text/html", 401)
    + ("application/json;charset=UTF-8", _LINE, {"x-frame-options": None}),
    ("OPTIONS", "application/json,TEXT/HTML", 401, "application/json;charset=UTF-8", _LINE, {}),
    ("OPTIONS", "application/json, foo, text/html", 401, _EMPTY_HTML, b"", _UNREAD_ALLOW),
    (
        "OPTIONS",
        "image/*",
        500,
        "text/html;charset=utf-8",
        b"",
        {"allow": "GET, HEAD, POST, PUT, DELETE, OPTIONS, TRACE"},
    ),
    ("OPTIONS", "audio/*", 500, "text/html;charset=utf-8", b"", {}),
    # a member Jira cannot read
    ("OPTIONS", "foo", 401, _EMPTY_HTML, b"", {**_UNREAD_ALLOW, "x-frame-options": None}),
    ("OPTIONS", "*.*", 401, _EMPTY_HTML, b"", {}),
    ("OPTIONS", "*/json", 401, _EMPTY_HTML, b"", {}),
    ("OPTIONS", "foo, application/json", 401, _EMPTY_HTML, b"", _UNREAD_ALLOW),
    ("OPTIONS", "application/json, foo", 401, _EMPTY_HTML, b"", _UNREAD_ALLOW),
    ("OPTIONS", "application/json,*.*", 401, _EMPTY_HTML, b"", _UNREAD_ALLOW),
    ("OPTIONS", "application/json;q=2", 401, _EMPTY_HTML, b"", _UNREAD_ALLOW),
    ("OPTIONS", "text/html;q=abc, application/json", 401, _EMPTY_HTML, b"", _UNREAD_ALLOW),
    ("OPTIONS", "application/json;charset=bogus", 401, _EMPTY_HTML, b"", _UNREAD_ALLOW),
    ("OPTIONS", "application/json;charset=base64", 401, _EMPTY_HTML, b"", _UNREAD_ALLOW),
    ("OPTIONS", "application/json;charset=rot13", 401, _EMPTY_HTML, b"", _UNREAD_ALLOW),
    ("OPTIONS", "application/json;charset=u8", 401, _EMPTY_HTML, b"", _UNREAD_ALLOW),
    ("OPTIONS", "application/json;Charset=bogus", 401, _EMPTY_HTML, b"", _UNREAD_ALLOW),
    ("OPTIONS", "\xa0application/json", 401, _EMPTY_HTML, b"", _UNREAD_ALLOW),
    ("OPTIONS", "application/json,*.*;q=0.9\xa0", 401, _EMPTY_HTML, b"", _UNREAD_ALLOW),
    # a GET whose `Accept` admits JSON passes the 406 and reaches the same choice
    ("GET", "text/plain, application/json", 401, "text/plain;charset=UTF-8", _LINE, {}),
    ("GET", "application/json, */*;q=0.1", 401, "text/html;charset=UTF-8", _LINE, {}),
    ("GET", "application/json, text/html", 401, "text/html;charset=UTF-8", _LINE, {}),
]
#: The gateway's Connect-token 403 under each `Accept` (``errors.atlassian.connect_token_answer``),
#: the headers and the route-independence being
#: :func:`test_jira_refuses_a_bearer_it_cannot_read_as_a_connect_token`'s.
_CONNECT_XML = b"<error>Failed to parse Connect Session Auth Token</error>"
_CONNECT_JSON = b'{"error": "Failed to parse Connect Session Auth Token"}'
_CONNECT = [
    (None, False),
    ("application/xml", True),
    ("APPLICATION/XML", True),
    ("application/xml;q=0", True),
    (_BROWSER, True),
    ("text/xml", False),
    ("application/json, application/xml", False),
    (_HTML, False),
    ("foo", False),
    # each found anywhere in the header, JSON over XML wherever the two stand
    ("xapplication/xml", True),
    ("APPLICATION/XMLX", True),
    ("text/html;x=application/xml", True),
    ("*/*, application/xml", True),
    ("\x85application/xml", True),
    ("application/xml\xa0", True),
    ("application/xml, application/json", False),
    ("application/xml, xapplication/jsonx", False),
    ("application/json;x=application/xml", False),
    ("Application/Json, application/xml", False),
    ("application/xhtml+xml", False),
    ("application/x-xml", False),
]
_ACCEPT_ROWS += [
    *(
        (
            method,
            _INFO if method == "OPTIONS" else "/atlassian/rest/api/3/myself",
            "none",
            accept,
            None,
            status,
            media_type,
            body,
            {"www-authenticate": 'OAuth realm="http%3A%2F%2Ftestserver"', **headers},
        )
        for method, accept, status, media_type, body, headers in _JIRA_401
    ),
    *(
        (
            "GET",
            _INFO,
            "unreadable-bearer",
            accept,
            None,
            403,
            "application/xml" if xml else _JSON,
            _CONNECT_XML if xml else _CONNECT_JSON,
            {},
        )
        for accept, xml in _CONNECT
    ),
    # the gateway's Connect-token 403 comes before Spring sorts `Accept`
    ("GET", _INFO, "unreadable-bearer", _SORT_FAILS, None, 403, _JSON, _CONNECT_JSON, {}),
    # the same choice on `_LICENSE` by GET and HEAD (``errors.atlassian.jira_unauthenticated``)
    ("GET", _LICENSE, "none", "foo", None, 401, _EMPTY_HTML, b"")
    + ({"allow": None, "www-authenticate": _PRESENT},),
    ("HEAD", _LICENSE, "none", "*.*", None, 401, _EMPTY_HTML, b"", {"allow": None}),
    # a `Q=2` beside a member Jira cannot read fails nothing there either
    ("GET", _LICENSE, "none", "foo, application/json;Q=2", None, 401, _EMPTY_HTML, b"", {}),
    ("GET", _LICENSE, "none", "image/*", None, 500, _OOPS_MEDIA, "oops")
    + ({"allow": None, "www-authenticate": _PRESENT},),
]


@pytest.mark.parametrize(
    "method,path,credential,accept,sent,status,media_type,body,headers", _ACCEPT_ROWS
)
def test_atlassian_answers_an_accept_where_and_as_real_does(
    client, admin_h, keys, method, path, credential, accept, sent, status, media_type, body, headers
):
    """Pins the 406 (``errors.atlassian.not_acceptable``) where each product answers it, the
    refusals that come before it and after it
    (``backlot.routers.atlassian._checked_before_the_route`` and
    ``backlot.routers.atlassian._published_answer``), and the shape a refusal takes from
    `Accept` (``errors.atlassian.rendered``, ``errors.atlassian.confluence_refusal``)."""
    path = path.format(**keys)
    sent_headers = dict(admin_h if credential == "admin" else _ACCEPT_CREDENTIALS[credential])
    if accept is not None:
        sent_headers["accept"] = accept.encode("latin-1")
    request = client.build_request(
        method, path, headers=sent_headers, content=None if sent is None else sent[1]
    )
    if accept is None:
        del request.headers["accept"]
    if sent is not None:
        request.headers["content-type"] = sent[0]
    r = _send_as_latin1(client, request)
    assert r.status_code == status, r.text
    assert r.headers.get("content-type") == media_type
    if body == "oops":
        trace = r.headers["atl-traceid"]
        assert f"TraceId: <strong>{trace}</strong>" in r.text
        page = re.sub(
            r"Log reference: <strong>[0-9a-f-]{36}</strong>",
            "Log reference: <strong><log></strong>",
            r.text,
        )
        page = page.replace(f"<strong>{trace}</strong>", "<strong><trace></strong>")
        assert (len(r.content), hashlib.sha256(page.encode()).hexdigest()) == _JIRA_OOPS
        body = None
    if isinstance(body, str):
        body = _accept_body(body, status, path.split("?")[0][len("/atlassian") :])
        if status in _TOMCAT_PAGES and media_type == _TOMCAT:
            length, title = _TOMCAT_PAGES[status]
            assert len(body) == length
            head = f'<!doctype html><html lang="en"><head><title>{title}</title>'
            assert body.startswith(head.encode())
            assert body.endswith(f"<body><h1>{title}</h1></body></html>".encode())
    if body is not None:
        assert r.content == body
    for name, value in headers.items():
        if value is None:
            assert name not in r.headers, name
        elif value is _PRESENT:
            assert name in r.headers, name
        else:
            assert r.headers[name] == value, name


def _accept_unchecked() -> list[str]:
    """The operations whose entry says ``"accept": "unchecked"``."""
    return sorted(
        op
        for group in ("refused", "run")
        for op, entry in routers_atlassian._JIRA_UNSERVED[group].items()
        if entry.get("accept") == "unchecked"
    )


@pytest.mark.parametrize("operation", _accept_unchecked())
def test_jira_operations_that_answer_an_accept_with_no_406(client, operation):
    """Pins each entry `ACCEPT_UNCHECKED` in ``scripts/gen_jira_unserved.py`` marks: none gives a
    caller with no credential the 406 for `Accept: text/html`, with a body of a type it takes. The
    refused ones answer their 401 under that type; the avatar views, which real runs, get what any
    operation real runs gets here."""
    entry = routers_atlassian._JIRA_ENTRIES[operation]
    method, template = operation.split(" ", 1)
    headers = {"Accept": _HTML}
    content = None
    if entry.get("consumes") and entry.get("body") != "optional":
        headers["Content-Type"], content = entry["consumes"][0], b"{}"
    r = client.request(
        method, f"/atlassian{template.replace('{}', '1')}", headers=headers, content=content
    )
    assert r.status_code != 406, r.text
    if operation in routers_atlassian._JIRA_UNSERVED["refused"]:
        assert r.status_code == 401
        assert r.headers["content-type"] == "text/html;charset=UTF-8"


_CQL_PREFIX = "com.atlassian.confluence.api.service.exceptions.api.BadRequestException: "
_CQL_NOT_PARSED = "Could not parse cql : "
# What real answered each `cql` with on Confluence Cloud, as `_CQL_TOKEN`, `_cql_written_back`
# and `_cql_refusal` in `backlot.routers.atlassian` record it: the `cqlQuery` of a 200, or the
# message of a 400 after `_CQL_PREFIX`.
_CQL_PARSE_ROWS = [
    *[
        (cql, 200, written)
        for cql, written in [
            ("type=page", "type = page"),
            ("type=page  and  space=handbook", "type = page and space = handbook"),
            ("TYPE=page", "TYPE = page"),
            ("type=page AND space=handbook", "type = page AND space = handbook"),
            ("type in (page,blogpost)", "type in ( page , blogpost )"),
            ("type NOT IN (page,blogpost)", "type NOT IN ( page , blogpost )"),
            ("type=page and (space=A or space=B)", "type = page and ( space = A or space = B )"),
            ("((type=page))", "( ( type = page ) )"),
            ("type!=page", "type != page"),
            ("title!~x", "title !~ x"),
            ("text~probe", "text ~ probe"),
            ('title~"a b"', 'title ~ "a b"'),
            ("title='x'", "title = 'x'"),
            ('title~"a\\"b"', 'title ~ "a\\"b"'),
            ("title~'a\\'b'", "title ~ 'a\\'b'"),
            ('title~"日本"', 'title ~ "日本"'),
            ("space.key=handbook", "space.key = 'handbook'"),
            ('space.key="handbook"', "space.key = 'handbook'"),
            ("SPACE.KEY=handbook", "SPACE.KEY = 'handbook'"),
            ('space.key != "handbook"', "space.key != 'handbook'"),
            ('space.key in ("handbook", handbook)', "space.key in ( 'handbook' , 'handbook' )"),
            ('space = "handbook"', 'space = "handbook"'),
            ('created>now("-1d")', 'created > now ( "-1d" )'),
            ("created>startOfDay()", "created > startOfDay ( )"),
            ("created >= 2026-01-01 and type=page", "created >= 2026-01-01 and type = page"),
            ("id in (1,2)", "id in ( 1 , 2 )"),
            ("label=a.b", "label = a.b"),
            ("label=x order by created desc", "label = x order by created desc"),
            (
                "type=page ORDER BY title asc, created desc",
                "type = page ORDER BY title asc , created desc",
            ),
            ("type=page and not space=A", "type = page and not space = A"),
            ("type=page\tand\nspace=A", "type = page and space = A"),
            ("title ~ probe*", "title ~ probe"),
            ("title ~ *probe", "title ~ probe"),
            *[(f"title~a{c}", "title ~ a") for c in "#$%&*+;?@^`{|}"],
        ]
    ],
    *[
        (cql, 400, message)
        for cql, message in [
            ('"', _CQL_NOT_PARSED),
            ("   ", _CQL_NOT_PARSED),
            ("\U0001f600", _CQL_NOT_PARSED),
            ("type=page and", _CQL_NOT_PARSED),
            ("type=page or", _CQL_NOT_PARSED),
            ("type=page)", _CQL_NOT_PARSED),
            ("order by created", _CQL_NOT_PARSED),
            ("not type=page", _CQL_NOT_PARSED),
            ("type=page OR NOT type=blogpost", _CQL_NOT_PARSED),
            ("type=page and (not space=A)", _CQL_NOT_PARSED),
            ("type in ()", _CQL_NOT_PARSED),
            ("type in (page,)", _CQL_NOT_PARSED),
            ("label in (a b)", _CQL_NOT_PARSED),
            ("type=page order by", _CQL_NOT_PARSED),
            ("type=page order by created b", _CQL_NOT_PARSED),
            ("type=page b and type=page", _CQL_NOT_PARSED),
            ("type=page 5", _CQL_NOT_PARSED),
            ("id = -1", _CQL_NOT_PARSED),
            ("created > -1d", _CQL_NOT_PARSED),
            ("title~a..b", _CQL_NOT_PARSED),
            ("title~a.", _CQL_NOT_PARSED),
            ("created>\u0662\u0660\u0662\u0666-01-01", _CQL_NOT_PARSED),
            *[(f"title~a{c}b", _CQL_NOT_PARSED) for c in "!\"'(),-/:<=>[\\]~"],
            *[
                (f"title~a{c}b", "Unrecognised clause at : b")
                for c in "#$%&*+;?@^`{|}\x01\x7f" + "é\u0663\U0001f600"
            ],
            ("title~a b c", "Unrecognised clause at : bc"),
            ('title~a "b"', 'Unrecognised clause at : "b"'),
            ("title ~ 'it''s'", "Unrecognised clause at : 's'"),
            ("type in (page) b", "Unrecognised clause at : b"),
            ("type=", "Expecting a value for operator '=' after field 'type'"),
            ("title~", "Expecting a value for operator '~' after field 'title'"),
            ("text~*", "Expecting a value for operator '~' after field 'text'"),
            ("title~é", "Expecting a value for operator '~' after field 'title'"),
            ("title~\u0663", "Expecting a value for operator '~' after field 'title'"),
            ("title~\uff11", "Expecting a value for operator '~' after field 'title'"),
            ("type==page", "Expecting a value for operator '=' after field 'type' but got '='"),
            ("space=~abc", "Expecting a value for operator '=' after field 'space' but got '~'"),
            ("label=.a", "Expecting a value for operator '=' after field 'label' but got '.'"),
            ("label=-x", "Expecting a value for operator '=' after field 'label' but got '-'"),
            (
                "type in page",
                "Expecting a value for operator 'in' after field 'type' but got 'page'",
            ),
            (
                "type not in page",
                "Expecting a value for operator 'notin' after field 'type' but got 'page'",
            ),
            (
                "type NOT IN (page",
                "Expecting a value for operator 'NOTIN' after field 'type' but got '('",
            ),
            ("(type=page", "Missing closing parenthesis near field (()"),
        ]
    ],
]


@pytest.mark.parametrize("cql, status, answer", _CQL_PARSE_ROWS)
def test_confluence_cql_search_reads_the_query_as_real_does(client, admin_h, cql, status, answer):
    """The rows `_CQL_PARSE_ROWS` records, each refusal with the `data` object of a service
    refusal."""
    r = client.get("/atlassian/wiki/rest/api/search", headers=admin_h, params={"cql": cql})
    assert r.status_code == status, r.text
    if status == 200:
        assert r.json()["cqlQuery"] == answer
    else:
        assert r.json() == {
            "statusCode": 400,
            "data": {"authorized": True, "valid": True, "errors": [], "successful": True},
            "message": _CQL_PREFIX + answer,
        }
