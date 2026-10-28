"""Linear's GraphQL API over HTTP: the served schema, the resolvers, and the filter compiler.

The filter tests were their own file once. They are still their own SECTION below, and the reason
they exist in this shape is worth keeping: a mutation review found 16 of 17 injected faults in
`backlot/graphql/linear_filters.py` surviving the rest of the suite. A wrong filter returns
plausible-looking data rather than an error, so every comparator pins its BOUNDARY (`lte` vs `lt`),
not merely that it filters something.
"""

from __future__ import annotations

import gzip
import hashlib
import itertools
import json
import zlib
from datetime import datetime, timezone
from urllib.parse import urlparse

import pytest
from graphql import GraphQLError, build_client_schema, is_enum_type, parse, validate

from backlot import synth
from backlot.fidelity.graphql_diff import backlot_schema
from backlot.graphql import linear_filters, mcp_tools
from backlot.graphql.body import node_json
from tests._helpers import (
    SWEPT_BYTES,
    Chunked,
    WithTheKey,
    build_corpus,
    client_for,
    complete,
    corpus_client,
    db_count,
    http_row,
    padded_query,
    selected_field_count,
    selected_fields,
    sent_as,
    served_id,
)

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


@pytest.fixture(scope="module")
def admin_h(tokens_yaml) -> dict[str, str]:
    """The admin token with no scheme, as ``backlot.auth.api_key_token`` reads it; the shared
    fixture's ``Bearer <token>`` is ``_API_KEY_AS_BEARER``."""
    return {"Authorization": tokens_yaml["admin_token"]}


def linear_user_token(tokens_yaml, email):
    return next(u["token"] for u in tokens_yaml["users"] if u["email"] == email)


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
        for field in (
            "id",
            "title",
            "description",
            "createdAt",
            "updatedAt",
            "archivedAt",
            "autoArchivedAt",
            "autoClosedAt",
            "branchName",
            "canceledAt",
            "completedAt",
            "dueDate",
            "estimate",
        ):
            assert field in issue, field
        assert issue["labels"]["nodes"] is not None
    # Key-presence alone is a TAUTOLOGY: graphql-core always emits a selected field as a key, so
    # the loop above passes even if every value is served as a constant null. Pin the values that
    # must be real, including a lifecycle timestamp that is genuinely populated.
    done = next(i for i in nodes if i["title"] == "Continuous batching stalls after compaction")
    assert done["completedAt"] == "2026-03-10T00:00:00Z"  # not None, not synthesized
    assert done["createdAt"] == "2026-03-01T00:00:00Z"
    assert done["canceledAt"] is None  # Done, so it was never canceled
    by_id = {i["title"]: i for i in nodes}
    rl = by_id["Rate limiter drops bursts under 50ms"]
    assert rl["creator"]["name"] and rl["assignee"]["name"] == "Bob Stone"
    assert rl["state"]["name"] == "In Progress"
    assert rl["project"]["name"] == "runtime-stability"
    assert {label["name"] for label in rl["labels"]["nodes"]} == {"bug", "gateway"}
    assert rl["estimate"] == 5
    assert rl["dueDate"] == "2026-03-15"


def test_linear_issue_by_uuid_and_by_identifier(client, admin_h, ro_conn):
    by_key = gql(client, '{ issue(id: "ENG-101") { id identifier title } }', admin_h)
    issue = by_key.json()["data"]["issue"]
    assert issue["identifier"] == "ENG-101"
    by_uuid = gql(client, "{ issue(id: %s) { identifier } }" % lit(issue["id"]), admin_h)
    assert by_uuid.json()["data"]["issue"]["identifier"] == "ENG-101"


def test_linear_issue_by_identifier_is_case_insensitive(client, admin_h):
    """Linear resolved `bre-1` to `BRE-1` (measured 2026-09-03): the key's case is not part of the
    lookup. The as-written spelling is tried first, so a corpus identifier with a mixed-case suffix
    is still reachable exactly (the suffix rule is the test after this one)."""
    r = gql(client, '{ issue(id: "eng-101") { identifier } }', admin_h).json()
    assert r["data"]["issue"]["identifier"] == "ENG-101"
    miss = gql(client, '{ issue(id: "eng-999") { identifier } }', admin_h).json()
    assert "Entity not found" in miss["errors"][0]["message"]


def test_linear_issue_url_is_the_real_vendor_domain(client, admin_h):
    """A rename's blind substitution can turn every served `url` field into
    `linear.backlot`. Asserted on the parsed host (no trailing slash) rather than a URL literal,
    because the vulnerable pattern is the literal characters `app` immediately followed by a
    slash — spelling that combination anywhere, even in a comment, makes a repeat of the bug
    rewrite it right alongside the code it guards. A bare `"linear.app"` with nothing appended
    has no slash for the pattern to land on, so it survives. The `"backlot" not in host` half is
    the one that actually matters: a rename can only ever INTRODUCE Backlot's own name into a
    vendor domain, never remove it, so no mechanical substitution can turn that assertion from
    failing into passing."""
    issue = gql(client, '{ issue(id: "ENG-101") { url } }', admin_h).json()["data"]["issue"]
    host = urlparse(issue["url"]).netloc
    assert host == "linear.app"
    assert "backlot" not in host


def test_linear_comment_url_is_the_issue_url_with_the_ids_first_eight(client, admin_h):
    """A comment's url is its ISSUE's url with `#comment-` and the first eight characters of
    the comment's id. The measurement is recorded beside `Comment.url`'s construction in
    `linear_resolvers.py`."""
    query = (
        '{ issue(id: "ENG-101") { url comments { nodes { id url } } } '
        "comments(first: 100) { nodes { id url } } }"
    )
    data = gql(client, query, admin_h).json()["data"]
    issue = data["issue"]
    for node in issue["comments"]["nodes"]:
        assert node["url"] == issue["url"] + "#comment-" + node["id"][:8]
    from_comment = issue["comments"]["nodes"][0]
    same = [c for c in data["comments"]["nodes"] if c["id"] == from_comment["id"]]
    assert same, "the issue's comment must also answer from the comments root"
    assert same[0]["url"] == from_comment["url"]


def test_linear_team_resolves_by_key_and_uuid(client, admin_h, ro_conn):
    key = gql(client, '{ team(id: "ENG") { id key name } }', admin_h).json()["data"]["team"]
    assert (key["key"], key["name"]) == ("ENG", "engineering")
    assert (
        gql(client, "{ team(id: %s) { key } }" % lit(key["id"]), admin_h).json()["data"]["team"][
            "key"
        ]
        == "ENG"
    )
    # The container's own raw name is a third, Backlot-only affordance on top of the two real
    # spellings above (key, uuid) -- costs nothing, so it stays alongside them.
    assert (
        gql(client, '{ team(id: "engineering") { key } }', admin_h).json()["data"]["team"]["key"]
        == "ENG"
    )


def test_linear_team_issue_count_excludes_archived_issues(tmp_path):
    """Pins the archived issue ``store.linear_team_issue_counts`` leaves out, beside the listing,
    the `includeArchived: true` listing and the read by id that still serve it."""
    records = [
        {
            **LINEAR_CORPUS[0],
            "doc_id": doc_id,
            "identifier": identifier,
            "team": team,
            "group": team,
            **({"archivedAt": "2026-03-01T00:00:00Z"} if archived else {}),
        }
        for doc_id, identifier, team, archived in [
            ("live", "ENG-1", "engineering", False),
            ("archived", "ENG-2", "engineering", True),
            ("archived-only", "DES-1", "design", True),
        ]
    ]
    with corpus_client(tmp_path, records) as (client, settings):
        headers = {"Authorization": settings.admin_token}
        body = gql(
            client,
            '{ team(id: "ENG") { issueCount issues { nodes { identifier } } } '
            "issues(includeArchived: true) { nodes { identifier } } "
            'archivedTeam: team(id: "DES") { issueCount } }',
            headers,
        ).json()
        assert "errors" not in body, body.get("errors")
        assert body["data"]["team"] == {
            "issueCount": 1,
            "issues": {"nodes": [{"identifier": "ENG-1"}]},
        }
        assert {n["identifier"] for n in body["data"]["issues"]["nodes"]} == {
            "ENG-1",
            "ENG-2",
            "DES-1",
        }
        assert body["data"]["archivedTeam"]["issueCount"] == 0
        archived = gql(client, '{ issue(id: "ENG-2") { identifier archivedAt } }', headers).json()
        assert archived["data"]["issue"]["identifier"] == "ENG-2"
        assert archived["data"]["issue"]["archivedAt"] is not None


def test_linear_team_issue_count_is_the_visible_count(client, admin_h, tokens_yaml):
    """Asserted for BOTH an admin and a restricted caller: as admin alone the count's ACL branch
    never runs, so the assertion would hold with scoping removed entirely."""
    admin = {
        t["key"]: t["issueCount"]
        for t in gql(client, "{ teams { nodes { key issueCount } } }", admin_h).json()["data"][
            "teams"
        ]["nodes"]
    }
    assert admin == {"ENG": 3, "DES": 1, "BLA": 1}
    ava_h = {"Authorization": linear_user_token(tokens_yaml, "ava@acme.com")}
    ava = {
        t["key"]: t["issueCount"]
        for t in gql(client, "{ teams { nodes { key issueCount } } }", ava_h).json()["data"][
            "teams"
        ]["nodes"]
    }
    # ava cannot see lin-secret or the blackops team at all.
    assert ava == {"ENG": 2, "DES": 1}
    # `team(id:)` applies the same scoping directly, not only through the `teams` connection ava's
    # own count above already exercises for the LIST root.
    hidden = gql(client, '{ team(id: "BLA") { key } }', ava_h)
    assert hidden.json()["data"] is None
    assert "Entity not found" in hidden.json()["errors"][0]["message"]
    # So does `teamMembership(id:)`, though ava's membership id there is computable offline.
    bla = synth.linear_team_membership_id("blackops", synth.linear_user_id("ava@acme.com"))
    hidden = gql(client, "{ teamMembership(id: %s) { id } }" % lit(bla), ava_h)
    assert "Entity not found" in hidden.json()["errors"][0]["message"]
    # Membership is the team's group, not every team the caller sees: ava, in engineering, sees
    # design too, and the admin token is no person and in no team.
    q = "{ teams { nodes { key viewerMembership { sortOrder } } } }"
    memberships = {
        who: {
            t["key"]: t["viewerMembership"]
            for t in gql(client, q, h).json()["data"]["teams"]["nodes"]
        }
        for who, h in (("admin", admin_h), ("ava", ava_h))
    }
    assert memberships == {
        "admin": {"BLA": None, "DES": None, "ENG": None},
        "ava": {"DES": None, "ENG": {"sortOrder": 0.0}},
    }


def test_linear_state_type_is_linears_category(client, admin_h):
    r = gql(client, "{ issues { nodes { identifier state { name type } } } }", admin_h)
    types = {n["identifier"]: n["state"]["type"] for n in r.json()["data"]["issues"]["nodes"]}
    assert types == {
        "ENG-101": "started",
        "ENG-102": "completed",
        "DES-77": "started",
        "ENG-103": "backlog",
        "BLA-1": "triage",
    }


_TEAM_LISTS = """query($id: String!) { team(id: $id) {
  createdAt
  cyclesEnabled
  states { nodes { id name type color position createdAt } }
  started: states(filter: {type: {eq: "started"}}) { nodes { name } }
  labels { nodes { name team { id } } }
  bug: labels(filter: {name: {eq: "bug"}}) { nodes { name } }
  projects { nodes { name status { id name type color position indefinite createdAt updatedAt
    archivedAt description team { id } teamId inheritedFrom { id } } } }
  cycles { nodes { name } }
} }"""


def test_linear_team_lists_its_states_labels_projects_and_cycles(client, admin_h, tokens_yaml):
    """A team's states are Linear's six defaults plus every other name its issues are in, newest
    first and then by id, each the object `workflowState(id:)` answers. Its cycles show with the
    team, the hidden issue's included; its projects and the workspace's labels only with an issue
    the caller reads."""
    ava_h = {"Authorization": linear_user_token(tokens_yaml, "ava@acme.com")}

    def lists(key, h):
        r = gql(client, _TEAM_LISTS, h, id=key).json()
        assert "errors" not in r, r["errors"]
        return r["data"]["team"]

    des = lists("DES", admin_h)
    states = {s["name"]: (s["type"], s["color"], s["position"]) for s in des["states"]["nodes"]}
    assert states == {
        "Backlog": ("backlog", "#bec2c8", 0.0),
        "Todo": ("unstarted", "#e2e2e2", 1.0),
        "In Progress": ("started", "#f2c94c", 2.0),
        "In Review": ("started", "#f2c94c", 2.5),
        "Done": ("completed", "#5e6ad2", 3.0),
        "Canceled": ("canceled", "#95a2b3", 4.0),
        "Duplicate": ("duplicate", "#95a2b3", 5.0),
    }
    nodes = des["states"]["nodes"]
    assert {s["createdAt"] for s in nodes} == {des["createdAt"]}
    assert [s["id"] for s in nodes] == sorted((s["id"] for s in nodes), reverse=True)
    for state in nodes:
        q = "{ workflowState(id: %s) { id name type color position createdAt } }"
        assert gql(client, q % lit(state["id"]), admin_h).json()["data"]["workflowState"] == state
    assert {s["name"] for s in des["started"]["nodes"]} == {"In Progress", "In Review"}
    triage = {s["name"]: s["position"] for s in lists("BLA", admin_h)["states"]["nodes"]}
    assert triage["Triage"] == -0.5

    eng = {who: lists("ENG", h) for who, h in (("admin", admin_h), ("ava", ava_h))}
    labels = {who: {n["name"] for n in t["labels"]["nodes"]} for who, t in eng.items()}
    assert labels == {
        "admin": {"bug", "gateway", "latency", "tokens", "restricted-only"},
        "ava": {"bug", "gateway", "latency", "tokens"},
    }
    assert {n["team"] for t in eng.values() for n in t["labels"]["nodes"]} == {None}
    assert eng["ava"]["bug"]["nodes"] == [{"name": "bug"}]
    assert lists("DES", ava_h)["labels"] == eng["ava"]["labels"]
    projects = {who: [n["name"] for n in t["projects"]["nodes"]] for who, t in eng.items()}
    assert sorted(projects["admin"]) == ["runtime-stability", "vault-rotation"]
    assert projects["ava"] == ["runtime-stability"]
    status = eng["ava"]["projects"]["nodes"][0]["status"]
    assert status == {
        "id": synth.linear_project_status_id("started"),
        "name": "In Progress",
        "type": "started",
        "color": "#F2C94C",
        "position": 2.0,
        "indefinite": False,
        "createdAt": status["createdAt"],
        "updatedAt": status["createdAt"],
        "archivedAt": None,
        "description": None,
        "team": None,
        "teamId": None,
        "inheritedFrom": None,
    }
    assert des["projects"]["nodes"] == []
    cycles = {who: {n["name"] for n in t["cycles"]["nodes"]} for who, t in eng.items()}
    assert cycles["admin"] == cycles["ava"] == {"2025-W08", "2026-W40-embargo"}
    # a team with cycles has them on, as real's did once they were turned on
    assert (eng["ava"]["cyclesEnabled"], des["cyclesEnabled"]) == (True, False)


_SEARCH = """query($term: String!, $filter: IssueFilter, $teamId: String, $c: Boolean,
                  $orderBy: PaginationOrderBy) {
  searchIssues(term: $term, filter: $filter, teamId: $teamId, includeComments: $c,
               orderBy: $orderBy) {
    totalCount
    nodes { identifier metadata state { name } }
    archivePayload { archive totalCount databaseVersion includesDependencies }
  }
}"""


@pytest.mark.parametrize(
    "variables, caller, identifiers, total",
    [
        # the identifier in any case, scored 1 and ahead of the word matches
        ({"term": "eng-101"}, "admin", ["ENG-101"], 1),
        # porter-stemmed, as real matched "familiarity" to "familiar"
        ({"term": "bursting"}, "admin", ["ENG-101"], 1),
        # a word's start, as real matched "prob" to "probe"; off the comments, which match "burst"
        ({"term": "burs", "c": False}, "admin", ["ENG-101"], 1),
        # any word matches
        ({"term": "compaction selects"}, "admin", ["DES-77", "ENG-102"], 2),
        # comments are searched unless `includeComments: false`
        ({"term": "reproduced"}, "admin", ["ENG-101"], 1),
        ({"term": "reproduced", "c": False}, "admin", [], 0),
        # `filter` narrows the nodes and not the total
        ({"term": "50ms", "filter": {"number": {"eq": 102}}}, "admin", ["ENG-102"], 2),
        # newest first under `orderBy`
        ({"term": "50ms", "orderBy": "createdAt"}, "admin", ["ENG-102", "ENG-101"], 2),
        ({"term": "50ms", "teamId": synth.linear_team_id("design")}, "admin", [], 0),
        ({"term": "50ms", "teamId": synth.linear_id("no team")}, "admin", [], 0),
        # a team id in upper case, and the nil UUID, both looked up
        (
            {"term": "50ms", "teamId": synth.linear_team_id("engineering").upper()},
            "admin",
            ["ENG-101", "ENG-102"],
            2,
        ),
        ({"term": "50ms", "teamId": "00000000-0000-0000-0000-000000000000"}, "admin", [], 0),
        # an issue the caller cannot read is no match, its comments included
        ({"term": "rotate rotation"}, "admin", ["ENG-103"], 1),
        ({"term": "rotate rotation"}, "ava@acme.com", [], 0),
    ],
)
def test_linear_search_issues(client, admin_h, tokens_yaml, variables, caller, identifiers, total):
    """The answers measured in the docstrings of `store.search_linear_issues` and
    `resolve_search_issues`, over the corpus's own words."""
    h = admin_h if caller == "admin" else {"Authorization": linear_user_token(tokens_yaml, caller)}
    r = gql(client, _SEARCH, h, **variables).json()
    assert "errors" not in r, r["errors"]
    found = r["data"]["searchIssues"]
    nodes = found["nodes"]
    served = [n["identifier"] for n in nodes]
    assert (served if "orderBy" in variables else sorted(served)) == identifiers
    assert found["totalCount"] == total
    assert all(n["state"]["name"] for n in nodes)
    assert all(n["metadata"]["context"] == {} and 0 <= n["metadata"]["score"] <= 1 for n in nodes)
    if variables["term"].upper() in served:
        assert nodes[0]["metadata"]["score"] == 1
    assert found["archivePayload"] == {
        "archive": "[]",
        "totalCount": 0,
        "databaseVersion": 0,
        "includesDependencies": [],
    }


@pytest.mark.parametrize(
    "variables",
    [
        {"term": ""},
        {"term": "x", "teamId": "ENG"},
        {"term": "x", "teamId": "11111111-1111-0111-8111-111111111111"},
    ],
)
def test_linear_search_issues_refuses_what_real_refuses(client, admin_h, variables):
    """An empty term and a team key for `teamId`, each refused as `resolve_search_issues`
    records."""
    r = gql(client, _SEARCH, admin_h, **variables).json()
    assert r["data"] is None
    assert r["errors"][0]["message"] == "Argument Validation Error"


def test_linear_search_ranks_by_relevance_unless_order_by(client, admin_h):
    """ENG-101 holds both words and ENG-102 one, while ENG-102 is the newer: relevance puts
    ENG-101 first and `orderBy` the newest. The identifier alone scores 1."""

    def search(**variables):
        r = gql(client, _SEARCH, admin_h, **variables).json()
        return [
            (n["identifier"], n["metadata"]["score"]) for n in r["data"]["searchIssues"]["nodes"]
        ]

    (first, best), (second, other) = search(term="burst 50ms")
    assert (first, second) == ("ENG-101", "ENG-102") and 1 > best > other > 0
    assert [i for i, _ in search(term="burst 50ms", orderBy="createdAt")] == ["ENG-102", "ENG-101"]
    assert search(term="ENG-102") == [("ENG-102", 1)]


def test_linear_search_finds_an_issue_by_the_spellings_real_reads(tmp_path):
    """The identifier spellings `_linear_search_identifier` records, over one team and a second
    issue numbered the same. A bare number names every visible issue with it; `teamId` still
    narrows that set."""
    titles = {
        2: "Connect your tools",
        19: "Probe children order (parent)",
        21: "Probe children order (child 2)",
    }
    records = [
        {
            "source_type": "linear",
            "doc_id": f"lin-{number}",
            "team": "engineering",
            "group": "engineering",
            "title": title,
            "content": "probe candidate",
            "author_email": "amaya.chen@acme.com",
            "author_groups": ["engineering"],
            "visibility": "public",
            "identifier": f"ENG-{number}",
            "state": "Todo",
            "created": f"2026-09-{number:02d}T00:00:00Z",
        }
        for number, title in titles.items()
    ]
    records.append(
        {
            "source_type": "linear",
            "doc_id": "lin-des-19",
            "team": "design",
            "group": "design",
            "title": "Other nineteen",
            "content": "unrelated",
            "author_email": "mia@acme.com",
            "author_groups": ["design"],
            "visibility": "public",
            "identifier": "DES-19",
            "state": "Todo",
            "created": "2026-09-01T00:00:00Z",
        }
    )
    with corpus_client(tmp_path, records) as (client, settings):
        headers = {"Authorization": settings.admin_token}

        def search(term, **extra):
            body = gql(client, _SEARCH, headers, term=term, **extra).json()
            assert "errors" not in body, body.get("errors")
            return [
                (node["identifier"], node["metadata"]["score"])
                for node in body["data"]["searchIssues"]["nodes"]
            ]

        for term in (
            "ENG-19",
            "eng-19",
            "Eng-19",
            "ENG19",
            "eng19",
            "ENG 19",
            "eng 19",
            "ENG-19.",
            "ENG-19,",
            "ENG-19;",
            "ENG-19!",
            "ENG-19-",
            "ENG-19_",
            "ENG-19\u00e9",
            "ENG-19\u0663",
            "ENG 19.",
            "ENG 19;",
            "   ENG-19",
            "ENG-19   ",
            "  ENG19",
            "ENG 19  ",
            '"ENG-19"',
            "'ENG-19'",
            "\tENG-19",
            "ENG19\t",
            "\xa0ENG19",
            "\ufeffENG-19",
            '"ENG19"',
            '"ENG 19"',
        ):
            assert search(term) == [("ENG-19", 1)], term
        both = [("ENG-19", 1), ("DES-19", 1)]
        for term in ("19", " 19", "19 "):
            assert search(term) == both, term
        assert search("19", teamId=synth.linear_team_id("design")) == [("DES-19", 1)]
        for term in (
            "019",
            "ENG-019",
            "ENG  19",
            "ENG\t19",
            "ENG-19x",
            "ENG 19x",
            "XYZ-19",
            "#19",
            "ENG19.",
            "ENG19;",
            "19.",
            "19,",
            " 19 ",
            "  19",
            "19  ",
            '"19"',
            "\t19",
            "19\xa0",
            "\x85ENG-19",
            "\x1fENG19",
            ".ENG-19",
            "(ENG-19)",
            "`ENG-19`",
            '"ENG-19',
            "'ENG-19\"",
            '" ENG-19"',
            "ENG\u201019",
        ):
            assert search(term) == [], term

        opened = search("ENG-19 probe")
        assert opened[0] == ("ENG-19", 1)
        assert {identifier for identifier, _ in opened} == {"ENG-19", "ENG-2", "ENG-21"}
        assert all(score < 1 for _, score in opened[1:])
        spaced = search("ENG 19 probe")
        assert spaced[0] == ("ENG-19", 1)
        assert {identifier for identifier, _ in spaced} == {"ENG-19", "ENG-2", "ENG-21"}
        assert all(score < 1 for _, score in spaced[1:])

        for term in ("ENG19 probe", "probe ENG-19", "19 probe"):
            found = search(term)
            assert found and all(score < 1 for _, score in found), term
            assert any(identifier == "ENG-19" for identifier, _ in found)

        numbered = search("2")
        assert numbered[0] == ("ENG-2", 1)
        assert numbered[1][0] == "ENG-21" and numbered[1][1] < 1


# The read documents mirage 0.0.7's `LinearVFS` sends (`mirage.core.linear.queries`), trimmed to
# what each selects past what other tests cover.
_MIRAGE_DOCUMENTS = {
    "Teams": "query { teams(first: 50) { nodes { id key name description timezone updatedAt"
    " states { nodes { id name type } } } pageInfo { hasNextPage endCursor } } }",
    "TeamMembers": 'query { team(id: "ENG") { members(first: 50) { nodes { id name displayName'
    " email active admin url updatedAt } pageInfo { hasNextPage endCursor } } } }",
    "TeamProjects": 'query { team(id: "ENG") { projects(first: 50) { nodes { id name description'
    " status { type } url updatedAt lead { id } } pageInfo { hasNextPage endCursor } } } }",
    "TeamCycles": 'query { team(id: "ENG") { cycles(first: 50) { nodes { id name number startsAt'
    " endsAt updatedAt } pageInfo { hasNextPage endCursor } } } }",
    "TeamLabels": 'query { team(id: "ENG") { labels(first: 50) { nodes { id name color }'
    " pageInfo { hasNextPage endCursor } } } }",
    "TeamDocuments": "query($teamId: ID!) { documents(filter: {team: {id: {eq: $teamId}}}, first: 50)"
    " { nodes { id title content url createdAt updatedAt project { id name }"
    " creator { id name email } } pageInfo { hasNextPage endCursor } } }",
    "IssueSearch": 'query { searchIssues(term: "burst", first: 50) { nodes { id identifier title'
    " state { id name } assignee { id displayName email } url } } }",
    "IssueLookup": 'query { issues(filter: {team: {key: {eq: "ENG"}}, number: {eq: 101}}, first: 1)'
    " { nodes { id identifier } } }",
}


@pytest.mark.parametrize("name", list(_MIRAGE_DOCUMENTS))
def test_mirage_linear_documents_are_answered(client, admin_h, name):
    variables = {"teamId": synth.linear_team_id("engineering")} if name == "TeamDocuments" else {}
    r = gql(client, _MIRAGE_DOCUMENTS[name], admin_h, **variables)
    assert r.status_code == 200
    assert "errors" not in r.json(), r.json()["errors"]
    if name == "TeamDocuments":
        # The page `resolve_documents` records a workspace without documents answering.
        assert r.json()["data"]["documents"] == {
            "nodes": [],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    if name == "IssueLookup":
        assert r.json()["data"]["issues"]["nodes"][0]["identifier"] == "ENG-101"


def test_linear_priority_is_linears_numeric_scale(client, admin_h):
    r = gql(client, "{ issues { nodes { identifier priority priorityLabel } } }", admin_h)
    got = {
        n["identifier"]: (n["priority"], n["priorityLabel"])
        for n in r.json()["data"]["issues"]["nodes"]
    }
    # The corpus writes P0-P3; the API serves Linear's own 0-4 scale (1 = most urgent).
    assert got["ENG-102"] == (1, "Urgent")
    assert got["ENG-101"] == (2, "High")
    assert got["DES-77"] == (3, "Medium")
    assert got["ENG-103"] == (4, "Low")


def test_linear_comments_connection_on_an_issue(client, admin_h):
    r = gql(
        client, '{ issue(id: "ENG-101") { comments { nodes { body user { email } } } } }', admin_h
    )
    nodes = r.json()["data"]["issue"]["comments"]["nodes"]
    assert [c["body"] for c in nodes] == ["Reproduced with a burst test.", "Fix is in review."]
    assert nodes[0]["user"]["email"] == "bob@acme.com"


def test_linear_by_id_relation_roots_answer(client, admin_h):
    """`@linear/sdk` resolves relations lazily — `await issue.state` fires `workflowState(id:)`
    rather than reading the value off the issue it already has. Without these roots every
    relation accessor in the SDK fails."""
    issue = gql(
        client,
        '{ issue(id: "ENG-101") { state { id } assignee { id } project { id } '
        "cycle { id } labels { nodes { id } } } }",
        admin_h,
    ).json()["data"]["issue"]
    assert gql(
        client,
        "{ workflowState(id: %s) { name team { key } } }" % lit(issue["state"]["id"]),
        admin_h,
    ).json()["data"]["workflowState"] == {"name": "In Progress", "team": {"key": "ENG"}}
    assert (
        gql(client, "{ user(id: %s) { email } }" % lit(issue["assignee"]["id"]), admin_h).json()[
            "data"
        ]["user"]["email"]
        == "bob@acme.com"
    )
    assert (
        gql(client, "{ project(id: %s) { name } }" % lit(issue["project"]["id"]), admin_h).json()[
            "data"
        ]["project"]["name"]
        == "runtime-stability"
    )
    assert (
        gql(client, "{ cycle(id: %s) { name } }" % lit(issue["cycle"]["id"]), admin_h).json()[
            "data"
        ]["cycle"]["name"]
        == "2025-W08"
    )
    label_id = issue["labels"]["nodes"][0]["id"]
    label = gql(
        client, "{ issueLabel(id: %s) { name isGroup groupType } }" % lit(label_id), admin_h
    ).json()["data"]["issueLabel"]
    assert label["name"] in {"bug", "gateway"}
    # `@linear/sdk@95.1.0`'s `IssueLabel` fragment selects `groupType`; `95.0.0`'s does not.
    assert label["isGroup"] is False
    assert label["groupType"] is None


def _unknown_id(root: str, id_: str, message: str, presentable: str, row: str):
    """A `_LINEAR_BODIES` row: the by-id root ``root`` sent an id it holds nothing for, and Linear's
    error for it with ``message`` and ``presentable`` (`linear_resolvers._not_found`,
    `linear_resolvers._ATTACHMENT`)."""
    body = {"query": "query($x: String!) { %s(id: $x) { id } }" % root, "variables": {"x": id_}}
    error = {
        "message": message,
        "path": [root],
        "locations": [{"line": 1, "column": 22}],
        "extensions": {
            "type": "invalid input",
            "code": "INPUT_ERROR",
            "statusCode": 400,
            "userError": True,
            "userPresentableMessage": presentable,
        },
    }
    answer = node_json({"errors": [error], "data": None})
    return pytest.param(json.dumps(body).encode(), WithTheKey(answer, 200), id=row)


_UNKNOWN_IDS = [
    _unknown_id(
        "issue",
        "00000000-0000-0000-0000-000000000000",
        "Entity not found: Issue",
        "Could not find referenced Issue.",
        "issue 00000000",
    ),
    _unknown_id(
        "issue",
        "NOPE-1",
        "Entity not found: Issue",
        "Could not find referenced Issue.",
        "issue NOPE-1",
    ),
    _unknown_id(
        "team", "nope", "Entity not found: Team", "Could not find referenced Team.", "team nope"
    ),
    _unknown_id(
        "user",
        "00000000-0000-0000-0000-000000000000",
        "Entity not found: User",
        "Could not find referenced User.",
        "user 00000000",
    ),
    _unknown_id(
        "workflowState",
        "nope",
        "Entity not found: WorkflowState",
        "Could not find referenced WorkflowState.",
        "workflowState nope",
    ),
    _unknown_id(
        "project",
        "00000000-0000-0000-0000-000000000000",
        "Entity not found: Project",
        "Could not find referenced Project.",
        "project 00000000",
    ),
    _unknown_id(
        "issueLabel",
        "nope",
        "Entity not found: IssueLabel",
        "Could not find referenced IssueLabel.",
        "issueLabel nope",
    ),
    _unknown_id(
        "cycle",
        "00000000-0000-0000-0000-000000000000",
        "Entity not found: Cycle",
        "Could not find referenced Cycle.",
        "cycle 00000000",
    ),
    _unknown_id(
        "release",
        "nope",
        "Entity not found: Release",
        "Could not find referenced Release.",
        "release nope",
    ),
    _unknown_id(
        "issueRelation",
        "nope",
        "Entity not found: IssueRelation",
        "Could not find referenced IssueRelation.",
        "issueRelation nope",
    ),
    _unknown_id(
        "teamMembership",
        "00000000-0000-0000-0000-000000000000",
        "Entity not found: TeamMembership",
        "Could not find referenced TeamMembership.",
        "teamMembership 00000000",
    ),
    _unknown_id(
        "attachment",
        "00000000-0000-0000-0000-000000000000",
        "Entity not found: Attachment: could not find by url.",
        "Could not find referenced attachment with this url.",
        "attachment 00000000",
    ),
]


def test_linear_workflow_states_are_per_team(client, admin_h):
    """Two teams' identically-named states are different objects in Linear, so their ids differ.
    The corpus has no shared state name, so assert the construction directly instead."""
    assert synth.linear_state_id("Done", "engineering") != synth.linear_state_id("Done", "design")


def test_linear_viewer_reports_the_authenticated_identity(client, tokens_yaml):
    h = {"Authorization": linear_user_token(tokens_yaml, "ava@acme.com")}
    me = gql(client, "{ viewer { email isMe } }", h).json()["data"]["viewer"]
    assert me == {"email": "ava@acme.com", "isMe": True}


def test_linear_content_round_trips_verbatim(client, admin_h, ro_conn):
    """`Issue.description` is the doc's retrieval payload; it must come back byte-for-byte."""
    stored = {
        r["identifier"]: r["content"]
        for r in ro_conn.execute("SELECT identifier, content FROM linear_issues")
    }
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
        page = gql(
            client,
            "{ issues(first: 2%s) { nodes { identifier } "
            "pageInfo { hasNextPage endCursor } } }" % after,
            admin_h,
        ).json()["data"]["issues"]
        seen += [n["identifier"] for n in page["nodes"]]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    assert len(seen) == len(set(seen)) == db_count(ro_conn, "linear")


def test_linear_introspection_reports_the_served_schema(client, admin_h):
    r = gql(
        client,
        "{ __schema { queryType { name } mutationType { name } } "
        '__type(name: "Issue") { fields { name } } }',
        admin_h,
    )
    data = r.json()["data"]
    assert data["__schema"]["queryType"]["name"] == "Query"
    # Read-only: no Mutation root at all, rather than one advertising writes that fail.
    assert data["__schema"]["mutationType"] is None
    names = {f["name"] for f in data["__type"]["fields"]}
    assert {"identifier", "branchName", "estimate", "dueDate", "state", "labels"} <= names


def test_linear_mcp_tools_derive_from_the_served_introspection(client, admin_h):
    """The GraphQL→MCP bridge ships no hand-written queries: it reads this endpoint's own
    introspection and generates one document per root field. Every one has to be a document this
    schema accepts, the selection has to stay bounded, and an issue's discussion has to be in it.

    `examples/using-mcp-with-agents/linear.py` drives the bridge at depth 1, and the ceiling below
    is why: a second level pulls `Issue.cycle.team` and its siblings in with all their
    configuration leaves. The ceiling is well clear at depth 1 and well under a depth-2 selection,
    so if a schema addition pushes it over, check what the new fields cost before raising it.
    """
    intro = gql(client, mcp_tools.INTROSPECTION_QUERY, admin_h).json()
    schema = build_client_schema(intro["data"])
    tools = mcp_tools.derive_tools(intro, depth=1)

    assert {t.name for t in tools} == set(schema.query_type.fields)
    for tool in tools:
        assert not validate(schema, parse(tool.document)), tool.name

    issues = next(t for t in tools if t.name == "issues")
    assert selected_field_count(issues.document) < 600
    # `Issue.comments` is the one nested connection that has to survive: `CommentFilter` carries
    # id/body/createdAt and no key for the issue, so the root `comments` tool cannot stand in for
    # it and an issue's discussion would be unreachable through the whole toolset.
    assert "comments" in selected_fields(issues.document, "issues", "nodes")


def test_linear_the_hand_written_examples_query_still_validates(client, admin_h):
    """The LlamaIndex reader example writes its own document rather than generating one, so a
    schema change can turn it into a 400 that no test sees. Read it as text (a test must not
    import from ``examples/``) and hold it to the bar the generated documents meet above.
    """
    import re

    from tests.conftest import REPO_ROOT

    text = (REPO_ROOT / "examples/using-llamaindex-readers/linear.py").read_text(encoding="utf-8")
    document = re.search(r'^QUERY = """(.*?)"""', text, re.S | re.M)
    assert document, "no QUERY literal found -- fix this extractor, not the assertion"

    schema = build_client_schema(gql(client, mcp_tools.INTROSPECTION_QUERY, admin_h).json()["data"])
    assert [e.message for e in validate(schema, parse(document.group(1)))] == []


def test_linear_malformed_document_is_a_400_with_a_graphql_envelope(client, admin_h):
    r = gql(client, "{ issues(first: }", admin_h)
    assert r.status_code == 400
    body = r.json()
    assert "detail" not in body and "data" not in body
    assert "Syntax Error" in body["errors"][0]["message"]


# --- the body, and where the credential is read against it ---------------------------------------
def _apollo(message: bytes) -> bytes:
    """Apollo Server's refusal of a body as Linear writes it around ``message``, already
    JSON-escaped."""
    return (
        b'{"errors":[{"message":"%s","extensions":{"http":{"status":400,"headers":{}},'
        b'"code":"BAD_REQUEST","type":"graphql error","userError":true}}]}' % message
    )


def _status(answer: bytes) -> int:
    envelope = json.loads(answer)
    if "code" in envelope:
        return envelope["code"]
    if "errors" not in envelope:
        return 200
    return envelope["errors"][0]["extensions"].get("http", {}).get("status", 400)


_NO_KEYS = _apollo(b"POST body missing, invalid Content-Type, or JSON object has no keys.")
_BATCHING = _apollo(b"Operation batching disabled.")
# The headers a body was sent with beyond `Content-Type: application/json`, the body, and the bytes
# Linear answered it with for the key, no credential and `nope` alike, at the status their `code` or
# `extensions.http.status` names, 400 for an error naming neither and 200 for an answer with no
# `errors` (`_status`), or None where Linear served the query with the key and answered 401 without
# one, as `backlot.graphql.body` and `backlot.graphql.v8_json` read them. A `WithTheKey` row draws
# the 401 without the key. Apollo Server's envelopes are api.linear.app's, but for the newline that
# ends each there.
_LINEAR_BODIES = [
    pytest.param(
        b"{",
        b'{"error":"Expected property name or \'}\' in JSON at position 1 (line 1 column 2)","details":{},"code":400}',
        id="{",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"x":NaN}}',
        b'{"error":"Unexpected token \'N\', ...\\"les\\":{\\"x\\":NaN}}\\" is not valid JSON","details":{},"code":400}',
        id="NaN at the end",
    ),
    pytest.param(
        b"5",
        b'{"error":"Unexpected token \'5\', \\"5\\" is not valid JSON","details":{},"code":400}',
        id="top-level 5",
    ),
    pytest.param(
        b'{"x":NaN}',
        b'{"error":"Unexpected token \'N\', \\"{\\"x\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="short object",
    ),
    pytest.param(
        b'{"variables":{"x":NaN},"query":"{ __typename }"}',
        b'{"error":"Unexpected token \'N\', ...\\"les\\":{\\"x\\":NaN},\\"quer\\"... is not valid JSON","details":{},"code":400}',
        id="surround",
    ),
    pytest.param(
        b'{"x":NaN,"query":"{ __typename }"}',
        b'{"error":"Unexpected token \'N\', \\"{\\"x\\":NaN,\\"query\\"... is not valid JSON","details":{},"code":400}',
        id="start",
    ),
    pytest.param(
        b"[NaN]",
        b'{"error":"Unexpected token \'N\', \\"[NaN]\\" is not valid JSON","details":{},"code":400}',
        id="short array",
    ),
    pytest.param(
        b"NaN",
        b'{"error":"Unexpected token \'N\', \\"NaN\\" is not valid JSON","details":{},"code":400}',
        id="top-level NaN",
    ),
    pytest.param(
        b'{"x":Infinity,"query":"{ __typename }"}',
        b'{"error":"Unexpected token \'I\', \\"{\\"x\\":Infinity,\\"\\"... is not valid JSON","details":{},"code":400}',
        id="Infinity",
    ),
    pytest.param(
        b'{"x":-Infinity,"query":"{ __typename }"}',
        b'{"error":"No number after minus sign in JSON at position 6 (line 1 column 7)","details":{},"code":400}',
        id="-Infinity",
    ),
    pytest.param(
        b'{\n  "query":"{ __typename }",\n  "variables": {"x": -Infinity}\n}',
        b'{"error":"No number after minus sign in JSON at position 52 (line 3 column 23)","details":{},"code":400}',
        id="-Infinity multi-line",
    ),
    pytest.param(
        b'{\n  "query":"{ __typename }",\n  "variables": {"x": NaN}\n}',
        b'{"error":"Unexpected token \'N\', ...\\"s\\": {\\"x\\": NaN}\\n}\\" is not valid JSON","details":{},"code":400}',
        id="NaN multi-line",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"e":"\xf0\x9f\x98\x80\xf0\x9f\x98\x80","x":-Infinity}}',
        b'{"error":"No number after minus sign in JSON at position 55 (line 1 column 56)","details":{},"code":400}',
        id="-Infinity after astral",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"e":"a\xf0\x9f\x98\x80\xf0\x9f\x98\x80\xf0\x9f\x98\x80\xf0\x9f\x98\x80","x":NaN}}',
        b'{"error":"Unexpected token \'N\', ...\\"\xf0\x9f\x98\x80\xf0\x9f\x98\x80\\",\\"x\\":NaN}}\\" is not valid JSON","details":{},"code":400}',
        id="NaN window splits a pair",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"x":[1,NaN]}}',
        b'{"error":"Unexpected token \'N\', ...\\"\\":{\\"x\\":[1,NaN]}}\\" is not valid JSON","details":{},"code":400}',
        id="NaN in a list",
    ),
    pytest.param(
        b'{\r\n"query":"{ __typename }",\r\n"variables":{"x":-Infinity}}',
        b'{"error":"No number after minus sign in JSON at position 48 (line 3 column 19)","details":{},"code":400}',
        id="-Infinity after CRLF",
    ),
    pytest.param(
        b'{\r"query":"{ __typename }",\r"variables":{"x":-Infinity}}',
        b'{"error":"No number after minus sign in JSON at position 46 (line 3 column 19)","details":{},"code":400}',
        id="-Infinity after CR",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"e":"a\xe2\x80\xa8b","x":-Infinity}}',
        b'{"error":"No number after minus sign in JSON at position 54 (line 1 column 55)","details":{},"code":400}',
        id="-Infinity after U+2028 in a string",
    ),
    pytest.param(
        b'{"abcdefghijk":NaN}',
        b'{"error":"Unexpected token \'N\', \\"{\\"abcdefghijk\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="len 19",
    ),
    pytest.param(
        b'{"abcdefghijkl":NaN}',
        b'{"error":"Unexpected token \'N\', \\"{\\"abcdefghijkl\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="len 20",
    ),
    pytest.param(
        b'{"abc":[[NaN],1,2,3,4,5,6,7]}',
        b'{"error":"Unexpected token \'N\', \\"{\\"abc\\":[[NaN],1,2,3\\"... is not valid JSON","details":{},"code":400}',
        id="pos 9",
    ),
    pytest.param(
        b'{"abcd":[[NaN],1,2,3,4,5,6,7]}',
        b'{"error":"Unexpected token \'N\', ...\\"{\\"abcd\\":[[NaN],1,2,3\\"... is not valid JSON","details":{},"code":400}',
        id="pos 10",
    ),
    pytest.param(
        b'{"abcdefghijklmnopq":[Infinity]}',
        b'{"error":"Unexpected token \'I\', ...\\"klmnopq\\":[Infinity]}\\" is not valid JSON","details":{},"code":400}',
        id="Infinity window at the end",
    ),
    pytest.param(
        b'{"x":-NaN,"query":"{ __typename }"}',
        b'{"error":"No number after minus sign in JSON at position 6 (line 1 column 7)","details":{},"code":400}',
        id="-NaN",
    ),
    pytest.param(
        b'{"x":Infinityx,"query":"{ __typename }"}',
        b'{"error":"Unexpected token \'I\', \\"{\\"x\\":Infinityx,\\"... is not valid JSON","details":{},"code":400}',
        id="Infinityx",
    ),
    pytest.param(
        b'{"x":NaN,}',
        b'{"error":"Unexpected token \'N\', \\"{\\"x\\":NaN,}\\" is not valid JSON","details":{},"code":400}',
        id="NaN then a later error",
    ),
    pytest.param(
        b'{"abcdefghijklm":NaN}',
        b'{"error":"Unexpected token \'N\', ...\\"fghijklm\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="L21",
    ),
    pytest.param(
        b'{"abcdefghijklmn":NaN}',
        b'{"error":"Unexpected token \'N\', ...\\"ghijklmn\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="L22",
    ),
    pytest.param(
        b'{"abcdefgh":NaN,"z":1}',
        b'{"error":"Unexpected token \'N\', ...\\"abcdefgh\\":NaN,\\"z\\":1}\\" is not valid JSON","details":{},"code":400}',
        id="L22 pos12",
    ),
    pytest.param(
        b'{"e":"\xf0\x9f\x98\x80\xf0\x9f\x98\x80\xf0\x9f\x98\x80","xy":NaN}',
        b'{"error":"Unexpected token \'N\', ...\\"\\ude00\xf0\x9f\x98\x80\\",\\"xy\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="end split low surrogate",
    ),
    pytest.param(
        b'{"x":NaN,"e":"\xf0\x9f\x98\x80\xf0\x9f\x98\x80\xf0\x9f\x98\x80\xf0\x9f\x98\x80"}',
        b'{"error":"Unexpected token \'N\', \\"{\\"x\\":NaN,\\"e\\":\\"\\ud83d\\"... is not valid JSON","details":{},"code":400}',
        id="start split",
    ),
    pytest.param(
        b'{"e":"\xf0\x9f\x98\x80\xf0\x9f\x98\x80\xf0\x9f\x98\x80\xf0\x9f\x98\x80\xf0\x9f\x98\x80x","x":NaN,"f":"\xf0\x9f\x98\x80\xf0\x9f\x98\x80\xf0\x9f\x98\x80\xf0\x9f\x98\x80\xf0\x9f\x98\x80\xf0\x9f\x98\x80"}',
        b'{"error":"Unexpected token \'N\', ...\\"\\ude00\xf0\x9f\x98\x80x\\",\\"x\\":NaN,\\"f\\":\\"\\ud83d\\"... is not valid JSON","details":{},"code":400}',
        id="surround split both",
    ),
    pytest.param(
        b'{\n\r"x":-Infinity}',
        b'{"error":"No number after minus sign in JSON at position 8 (line 3 column 6)","details":{},"code":400}',
        id="LF then CR",
    ),
    pytest.param(
        b'{\t"x":\t-Infinity}',
        b'{"error":"No number after minus sign in JSON at position 8 (line 1 column 9)","details":{},"code":400}',
        id="tab lines",
    ),
    pytest.param(
        b'"abcdefghijklmnopqrstuvwxyz0123456789"',
        b'{"error":"Unexpected token \'\\"\', \\"\\"abcdefghi\\"... is not valid JSON","details":{},"code":400}',
        id="top-level long string",
    ),
    pytest.param(
        b"  \n 5",
        b'{"error":"Unexpected token \'5\', \\"  \\n 5\\" is not valid JSON","details":{},"code":400}',
        id="top-level ws then 5",
    ),
    pytest.param(
        b"-Infinity",
        b'{"error":"Unexpected token \'-\', \\"-Infinity\\" is not valid JSON","details":{},"code":400}',
        id="top-level -Infinity",
    ),
    pytest.param(
        b"Infinity",
        b'{"error":"Unexpected token \'I\', \\"Infinity\\" is not valid JSON","details":{},"code":400}',
        id="top-level Infinity",
    ),
    pytest.param(
        b'{"a":"x\\"y\\\\z","b":NaN}',
        b'{"error":"Unexpected token \'N\', ...\\"y\\\\\\\\z\\",\\"b\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="escaped quote in window",
    ),
    pytest.param(
        b'{"query":"{ __typename }","a":"p\xe2\x80\xa8q","b":NaN}',
        b'{"error":"Unexpected token \'N\', ...\\"\\"p\xe2\x80\xa8q\\",\\"b\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="U+2028 raw in window",
    ),
    pytest.param(
        b'{"query":"{ __typename }","a":"\\n","b":NaN}',
        b'{"error":"Unexpected token \'N\', ...\\":\\"\\\\n\\",\\"b\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="control escape in window",
    ),
    pytest.param(
        b"   ",
        b'{"error":"Unexpected end of JSON input","details":{},"code":400}',
        id="whitespace only",
    ),
    pytest.param(
        b"\n",
        b'{"error":"Unexpected end of JSON input","details":{},"code":400}',
        id="newline only",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"__proto__":{"a":1}}}', None, id="__proto__ key"
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"constructor":{"prototype":{"a":1}}}}',
        None,
        id="constructor.prototype",
    ),
    pytest.param(
        b'\xff\xfe{\x00"\x00q\x00u\x00e\x00r\x00y\x00"\x00:\x00"\x00{\x00 \x00_\x00_\x00t\x00y\x00p\x00e\x00n\x00a\x00m\x00e\x00 \x00}\x00"\x00}\x00',
        b'{"error":"Unexpected token \'\xef\xbf\xbd\', \\"\xef\xbf\xbd\xef\xbf\xbd{\\u0000\\"\\u0000q\\u0000u\\u0000\\"... is not valid JSON","details":{},"code":400}',
        id="utf-16 body",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"x":"\xff"}}', None, id="invalid utf-8 in a string"
    ),
    pytest.param(b'\xef\xbb\xbf{"query":"{ __typename }"}', None, id="utf-8 BOM"),
    pytest.param(
        b'{"a":"\xe2\x82","b":NaN}',
        b'{"error":"Unexpected token \'N\', \\"{\\"a\\":\\"\xef\xbf\xbd\\",\\"b\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="utf8 truncated 3-byte",
    ),
    pytest.param(
        b'{"a":"\xed\xa0\x80","b":NaN}',
        b'{"error":"Unexpected token \'N\', \\"{\\"a\\":\\"\xef\xbf\xbd\xef\xbf\xbd\xef\xbf\xbd\\",\\"b\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="utf8 surrogate",
    ),
    pytest.param(
        b'{"a":"\xf0\x9f\x98","b":NaN}',
        b'{"error":"Unexpected token \'N\', \\"{\\"a\\":\\"\xef\xbf\xbd\\",\\"b\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="utf8 truncated 4-byte",
    ),
    pytest.param(
        b'{"a":"\xc0\x80","b":NaN}',
        b'{"error":"Unexpected token \'N\', \\"{\\"a\\":\\"\xef\xbf\xbd\xef\xbf\xbd\\",\\"b\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="utf8 overlong 2-byte",
    ),
    pytest.param(
        b'{"a":"\xf4\x90\x80\x80","b":NaN}',
        b'{"error":"Unexpected token \'N\', \\"{\\"a\\":\\"\xef\xbf\xbd\xef\xbf\xbd\xef\xbf\xbd\xef\xbf\xbd\\",\\"b\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="utf8 above U+10FFFF",
    ),
    pytest.param(
        b'{"a":"\xff","b":NaN}',
        b'{"error":"Unexpected token \'N\', \\"{\\"a\\":\\"\xef\xbf\xbd\\",\\"b\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="utf8 lone FF",
    ),
    pytest.param(
        b'{"a":"\x80","b":NaN}',
        b'{"error":"Unexpected token \'N\', \\"{\\"a\\":\\"\xef\xbf\xbd\\",\\"b\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="utf8 lone continuation",
    ),
    pytest.param(
        b'{"a":"\xe0\x80\x80","b":NaN}',
        b'{"error":"Unexpected token \'N\', \\"{\\"a\\":\\"\xef\xbf\xbd\xef\xbf\xbd\xef\xbf\xbd\\",\\"b\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="utf8 overlong 3-byte",
    ),
    pytest.param(
        b'{"a":"\xf8\x88\x80\x80\x80","b":NaN}',
        b'{"error":"Unexpected token \'N\', ...\\"\xef\xbf\xbd\xef\xbf\xbd\xef\xbf\xbd\xef\xbf\xbd\\",\\"b\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="utf8 5-byte form",
    ),
    pytest.param(
        b'{"a":"\xe0A","b":NaN}',
        b'{"error":"Unexpected token \'N\', \\"{\\"a\\":\\"\xef\xbf\xbdA\\",\\"b\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="utf8 E0 then ASCII",
    ),
    pytest.param(
        b'{"a":"\xf0\x9fA","b":NaN}',
        b'{"error":"Unexpected token \'N\', \\"{\\"a\\":\\"\xef\xbf\xbdA\\",\\"b\\":NaN}\\" is not valid JSON","details":{},"code":400}',
        id="utf8 F0 9F then ASCII",
    ),
    pytest.param(
        b'\xef\xbb\xbf\xef\xbb\xbf{"query":"{ __typename }"}',
        b'{"error":"Unexpected token \'\xef\xbb\xbf\', \\"\xef\xbb\xbf{\\"query\\":\\"... is not valid JSON","details":{},"code":400}',
        id="BOM twice",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"constructor":null}}', None, id="constructor null"
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": ' + b"1" * 4300 + b"}}",
        None,
        id="4300 digits",
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": ' + b"1" * 4301 + b"}}",
        None,
        id="4301 digits",
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": ' + b"1" * 5000 + b"}}",
        None,
        id="5000 digits",
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": -' + b"1" * 4301 + b"}}",
        None,
        id="-4301 digits",
    ),
    pytest.param(b'{"query": "{ __typename }", "variables": {"x": 1e400}}', None, id="1e400"),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": ' + b"[" * 1001 + b"]" * 1001 + b"}}",
        None,
        id="nested 1001",
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": ' + b"[" * 100000 + b"]" * 100000 + b"}}",
        None,
        id="nested 100000",
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": ' + b"[" * 300000 + b"]" * 300000 + b"}}",
        None,
        id="nested 300000",
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": '
        + b'{"a":' * 100000
        + b"1"
        + b"}" * 100000
        + b"}}",
        None,
        id="objects nested 100000",
    ),
    pytest.param(
        b'{"query": "{ __typename }", "variables": {"x": '
        + b"[" * 100000
        + b"NaN"
        + b"]" * 100000
        + b"}}",
        b'{"error":"Unexpected token \'N\', ...\\"[[[[[[[[[[NaN]]]]]]]\\"... is not valid JSON","details":{},"code":400}',
        id="nested 100000 then NaN",
    ),
    pytest.param(
        b"null",
        b'{"error":"Unexpected token \'n\', \\"null\\" is not valid JSON","details":{},"code":400}',
        id="top-level null",
    ),
    pytest.param(
        b'\xc2\xa0{"query":"{ __typename }"}',
        b'{"error":"Unexpected token \'\xc2\xa0\', \\"\xc2\xa0{\\"query\\":\\"... is not valid JSON","details":{},"code":400}',
        id="U+00A0 then {",
    ),
    pytest.param(
        b'\x0b{"query":"{ __typename }"}',
        b'{"error":"Unexpected token \'\\u000b\', \\"\\u000b{\\"query\\":\\"... is not valid JSON","details":{},"code":400}',
        id="a vertical tab then {",
    ),
    pytest.param(
        b'\x0c{"query":"{ __typename }"}',
        b'{"error":"Unexpected token \'\\f\', \\"\\f{\\"query\\":\\"... is not valid JSON","details":{},"code":400}',
        id="a form feed then {",
    ),
    pytest.param(
        b'\xe3\x80\x80{"query":"{ __typename }"}',
        b'{"error":"Unexpected token \'\xe3\x80\x80\', \\"\xe3\x80\x80{\\"query\\":\\"... is not valid JSON","details":{},"code":400}',
        id="U+3000 then {",
    ),
    pytest.param(
        b'\xe2\x80\xa8{"query":"{ __typename }"}',
        b'{"error":"Unexpected token \'\xe2\x80\xa8\', \\"\xe2\x80\xa8{\\"query\\":\\"... is not valid JSON","details":{},"code":400}',
        id="U+2028 then {",
    ),
    pytest.param(
        b'\x1c{"query":"{ __typename }"}',
        b'{"error":"Unexpected token \'\\u001c\', \\"\\u001c{\\"query\\":\\"... is not valid JSON","details":{},"code":400}',
        id="0x1C then {",
    ),
    pytest.param(
        b'{"e":"\xf0\x9f\x98\x80\xf0\x9f\x98\x80",\n"x":-Infinity}',
        b'{"error":"No number after minus sign in JSON at position 18 (line 2 column 6)","details":{},"code":400}',
        id="-Infinity after two surrogate pairs on the line before",
    ),
    pytest.param(
        b'{"e":"\xf0\x9f\x98\x80",\r\n"f":"\xf0\x9f\x98\x80\xf0\x9f\x98\x80","x":-Infinity}',
        b'{"error":"No number after minus sign in JSON at position 28 (line 2 column 17)","details":{},"code":400}',
        id="-Infinity after surrogate pairs on both lines",
    ),
    pytest.param(
        b'{"query":"{ __typename }","b" 2}',
        b'{"error":"Expected \':\' after property name in JSON at position 30 (line 1 column 31)","details":{},"code":400}',
        id="colon after a later key",
    ),
    pytest.param(
        b'{"query" 2}',
        b'{"error":"Expected \':\' after property name in JSON at position 9 (line 1 column 10)","details":{},"code":400}',
        id="colon after the first key",
    ),
    pytest.param(
        b"[01]",
        b'{"error":"Unexpected number in JSON at position 2 (line 1 column 3)","details":{},"code":400}',
        id="leading zero in an array",
    ),
    pytest.param(
        b'{"a":01}',
        b'{"error":"Unexpected number in JSON at position 6 (line 1 column 7)","details":{},"code":400}',
        id="leading zero in an object",
    ),
    pytest.param(
        b'{"a":{"a":{"a":1,"b" 1}}}',
        b'{"error":"Expected \':\' after property name in JSON at position 21 (line 1 column 22)","details":{},"code":400}',
        id="nested later key no colon",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables"x:{}}',
        b'{"error":"Expected \':\' after property name in JSON at position 37 (line 1 column 38)","details":{},"code":400}',
        id="later key then a letter",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables",:{}}',
        b'{"error":"Expected \':\' after property name in JSON at position 37 (line 1 column 38)","details":{},"code":400}',
        id="later key then a comma",
    ),
    pytest.param(
        b"[", b'{"error":"Unexpected end of JSON input","details":{},"code":400}', id="[ alone"
    ),
    pytest.param(
        b"{ ",
        b'{"error":"Expected property name or \'}\' in JSON at position 2 (line 1 column 3)","details":{},"code":400}',
        id="{ then space",
    ),
    pytest.param(
        b"{} x",
        b'{"error":"Unexpected non-whitespace character after JSON at position 3 (line 1 column 4)","details":{},"code":400}',
        id="{} then x",
    ),
    pytest.param(
        b"[1]]",
        b'{"error":"Unexpected non-whitespace character after JSON at position 3 (line 1 column 4)","details":{},"code":400}',
        id="[1] then ]",
    ),
    pytest.param(
        b'{"query',
        b'{"error":"Unterminated string in JSON at position 7 (line 1 column 8)","details":{},"code":400}',
        id="key unterminated",
    ),
    pytest.param(
        b'{"query"',
        b'{"error":"Expected \':\' after property name in JSON at position 8 (line 1 column 9)","details":{},"code":400}',
        id="key then end",
    ),
    pytest.param(
        b'{"query":',
        b'{"error":"Unexpected end of JSON input","details":{},"code":400}',
        id="key colon end",
    ),
    pytest.param(
        b'{"query": ',
        b'{"error":"Unexpected end of JSON input","details":{},"code":400}',
        id="key colon space end",
    ),
    pytest.param(
        b'{"query":"{ __typename }"',
        b'{"error":"Expected \',\' or \'}\' after property value in JSON at position 25 (line 1 column 26)","details":{},"code":400}',
        id="value then end",
    ),
    pytest.param(
        b'{"query":"{ __typename }" x}',
        b'{"error":"Expected \',\' or \'}\' after property value in JSON at position 26 (line 1 column 27)","details":{},"code":400}',
        id="value then x",
    ),
    pytest.param(
        b'{"query":"{ __typename }",}',
        b'{"error":"Expected double-quoted property name in JSON at position 26 (line 1 column 27)","details":{},"code":400}',
        id="trailing comma object",
    ),
    pytest.param(
        b'{"query":"{ __typename }",1:2}',
        b'{"error":"Expected double-quoted property name in JSON at position 26 (line 1 column 27)","details":{},"code":400}',
        id="comma then number",
    ),
    pytest.param(
        b"{'query':1}",
        b'{"error":"Expected property name or \'}\' in JSON at position 1 (line 1 column 2)","details":{},"code":400}',
        id="single quotes",
    ),
    pytest.param(
        b"{query:1}",
        b'{"error":"Expected property name or \'}\' in JSON at position 1 (line 1 column 2)","details":{},"code":400}',
        id="bare key",
    ),
    pytest.param(
        b"{1:2}",
        b'{"error":"Expected property name or \'}\' in JSON at position 1 (line 1 column 2)","details":{},"code":400}',
        id="first key a number",
    ),
    pytest.param(
        b"[1,]",
        b'{"error":"Unexpected token \']\', \\"[1,]\\" is not valid JSON","details":{},"code":400}',
        id="array trailing comma",
    ),
    pytest.param(
        b"[1 2]",
        b'{"error":"Expected \',\' or \']\' after array element in JSON at position 3 (line 1 column 4)","details":{},"code":400}',
        id="array two values",
    ),
    pytest.param(
        b"[1}",
        b'{"error":"Expected \',\' or \']\' after array element in JSON at position 2 (line 1 column 3)","details":{},"code":400}',
        id="array then }",
    ),
    pytest.param(
        b"[1",
        b'{"error":"Expected \',\' or \']\' after array element in JSON at position 2 (line 1 column 3)","details":{},"code":400}',
        id="array end",
    ),
    pytest.param(
        b"[1,",
        b'{"error":"Unexpected end of JSON input","details":{},"code":400}',
        id="array comma end",
    ),
    pytest.param(
        b'["a" "b"]',
        b'{"error":"Expected \',\' or \']\' after array element in JSON at position 5 (line 1 column 6)","details":{},"code":400}',
        id="array of strings missing comma",
    ),
    pytest.param(
        b'{"query":"{ __typename }',
        b'{"error":"Unterminated string in JSON at position 24 (line 1 column 25)","details":{},"code":400}',
        id="unterminated value string",
    ),
    pytest.param(
        b'{"query":"\\x"}',
        b'{"error":"Bad escaped character in JSON at position 11 (line 1 column 12)","details":{},"code":400}',
        id="bad escape",
    ),
    pytest.param(
        b'{"query":"\\',
        b'{"error":"Unexpected end of JSON input","details":{},"code":400}',
        id="escape at end",
    ),
    pytest.param(
        b'{"query":"\\u12G4"}',
        b'{"error":"Bad Unicode escape in JSON at position 14 (line 1 column 15)","details":{},"code":400}',
        id="bad unicode escape",
    ),
    pytest.param(
        b'{"query":"\\u12',
        b'{"error":"Bad Unicode escape in JSON at position 14 (line 1 column 15)","details":{},"code":400}',
        id="unicode escape at end",
    ),
    pytest.param(
        b'{"query":"a\x01b"}',
        b'{"error":"Bad control character in string literal in JSON at position 11 (line 1 column 12)","details":{},"code":400}',
        id="control char in string",
    ),
    pytest.param(
        b'{"query":"a\tb"}',
        b'{"error":"Bad control character in string literal in JSON at position 11 (line 1 column 12)","details":{},"code":400}',
        id="tab in string",
    ),
    pytest.param(
        b'{"query":"a\nb"}',
        b'{"error":"Bad control character in string literal in JSON at position 11 (line 1 column 12)","details":{},"code":400}',
        id="newline in string",
    ),
    pytest.param(
        b'{"query":"\\\xc3\xa9"}',
        b'{"error":"Bad escaped character in JSON at position 11 (line 1 column 12)","details":{},"code":400}',
        id="escape of a two-byte char",
    ),
    pytest.param(
        b'{"query":"\\\xe4\xb8\x80"}',
        b'{"error":"Unexpected token \'\xe4\xb8\x80\', \\"{\\"query\\":\\"\\\\\xe4\xb8\x80\\"}\\" is not valid JSON","details":{},"code":400}',
        id="escape of a char above 0xFF",
    ),
    pytest.param(
        b'{"x":00}',
        b'{"error":"Unexpected number in JSON at position 6 (line 1 column 7)","details":{},"code":400}',
        id="leading zero",
    ),
    pytest.param(
        b'{"x":-}',
        b'{"error":"No number after minus sign in JSON at position 6 (line 1 column 7)","details":{},"code":400}',
        id="minus alone",
    ),
    pytest.param(
        b'{"x":-a}',
        b'{"error":"No number after minus sign in JSON at position 6 (line 1 column 7)","details":{},"code":400}',
        id="minus then letter",
    ),
    pytest.param(
        b'{"x":1.}',
        b'{"error":"Unterminated fractional number in JSON at position 7 (line 1 column 8)","details":{},"code":400}',
        id="fraction missing",
    ),
    pytest.param(
        b'{"x":1.e5}',
        b'{"error":"Unterminated fractional number in JSON at position 7 (line 1 column 8)","details":{},"code":400}',
        id="fraction then e",
    ),
    pytest.param(
        b'{"x":1e}',
        b'{"error":"Exponent part is missing a number in JSON at position 7 (line 1 column 8)","details":{},"code":400}',
        id="exponent missing",
    ),
    pytest.param(
        b'{"x":1e+}',
        b'{"error":"Exponent part is missing a number in JSON at position 8 (line 1 column 9)","details":{},"code":400}',
        id="exponent sign then no digit",
    ),
    pytest.param(
        b"[1E",
        b'{"error":"Exponent part is missing a number in JSON at position 3 (line 1 column 4)","details":{},"code":400}',
        id="exponent at end",
    ),
    pytest.param(
        b"[1.5.3]",
        b'{"error":"Expected \',\' or \']\' after array element in JSON at position 4 (line 1 column 5)","details":{},"code":400}',
        id="two dots",
    ),
    pytest.param(
        b"[+1]",
        b'{"error":"Unexpected token \'+\', \\"[+1]\\" is not valid JSON","details":{},"code":400}',
        id="plus number",
    ),
    pytest.param(
        b"[0x10]",
        b'{"error":"Expected \',\' or \']\' after array element in JSON at position 2 (line 1 column 3)","details":{},"code":400}',
        id="hex",
    ),
    pytest.param(
        b"[tru]",
        b'{"error":"Unexpected token \']\', \\"[tru]\\" is not valid JSON","details":{},"code":400}',
        id="true misspelt",
    ),
    pytest.param(
        b"[tru5]",
        b'{"error":"Unexpected number in JSON at position 4 (line 1 column 5)","details":{},"code":400}',
        id="true then digit",
    ),
    pytest.param(
        b'[fals"e"]',
        b'{"error":"Unexpected string in JSON at position 5 (line 1 column 6)","details":{},"code":400}',
        id="false then quote",
    ),
    pytest.param(
        b"[nul",
        b'{"error":"Unexpected end of JSON input","details":{},"code":400}',
        id="null at end",
    ),
    pytest.param(
        b"[nulll]",
        b'{"error":"Expected \',\' or \']\' after array element in JSON at position 5 (line 1 column 6)","details":{},"code":400}',
        id="nulll",
    ),
    pytest.param(
        b"[object Object]",
        b'{"error":"\\"[object Object]\\" is not valid JSON","details":{},"code":400}',
        id="object Object",
    ),
    pytest.param(
        b"[undefined]",
        b'{"error":"Unexpected token \'u\', \\"[undefined]\\" is not valid JSON","details":{},"code":400}',
        id="undefined in array",
    ),
    pytest.param(
        b"[:]",
        b'{"error":"Unexpected token \':\', \\"[:]\\" is not valid JSON","details":{},"code":400}',
        id="colon in array",
    ),
    pytest.param(
        b"[,1]",
        b'{"error":"Unexpected token \',\', \\"[,1]\\" is not valid JSON","details":{},"code":400}',
        id="comma first in array",
    ),
    pytest.param(
        b"[}]",
        b'{"error":"Unexpected token \'}\', \\"[}]\\" is not valid JSON","details":{},"code":400}',
        id="close brace in array",
    ),
    pytest.param(
        b"[\xe4\xb8\x80]",
        b'{"error":"Unexpected token \'\xe4\xb8\x80\', \\"[\xe4\xb8\x80]\\" is not valid JSON","details":{},"code":400}',
        id="two-byte token in array",
    ),
    pytest.param(
        b"[\xf0\x9f\x98\x80]",
        b'{"error":"Unexpected token \'\\ud83d\', \\"[\xf0\x9f\x98\x80]\\" is not valid JSON","details":{},"code":400}',
        id="astral token in array",
    ),
    pytest.param(
        b"[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[1,]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]",
        b'{"error":"Unexpected token \']\', ...\\"[[[[[[[[1,]]]]]]]]]]\\"... is not valid JSON","details":{},"code":400}',
        id="deep then bad",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":{"x":[1,2,3,4,5,6,7,8,9,10,]}}',
        b'{"error":"Unexpected token \']\', ...\\",7,8,9,10,]}}\\" is not valid JSON","details":{},"code":400}',
        id="bad after long prefix",
    ),
    pytest.param(
        b'{"\\q":1}',
        b'{"error":"Bad escaped character in JSON at position 3 (line 1 column 4)","details":{},"code":400}',
        id="key with escape bad",
    ),
    pytest.param(
        b'{"12',
        b'{"error":"Unterminated string in JSON at position 4 (line 1 column 5)","details":{},"code":400}',
        id="numeric key unterminated",
    ),
    pytest.param(
        b'{"12x":1,',
        b'{"error":"Expected double-quoted property name in JSON at position 9 (line 1 column 10)","details":{},"code":400}',
        id="numeric key then x",
    ),
    pytest.param(
        b"[123456789.",
        b'{"error":"Unterminated fractional number in JSON at position 11 (line 1 column 12)","details":{},"code":400}',
        id="nine digits then dot end",
    ),
    pytest.param(
        b"[1234567890e]",
        b'{"error":"Exponent part is missing a number in JSON at position 12 (line 1 column 13)","details":{},"code":400}',
        id="ten digits then e",
    ),
    pytest.param(
        b"[-01]",
        b'{"error":"Unexpected number in JSON at position 3 (line 1 column 4)","details":{},"code":400}',
        id="negative zero then digit",
    ),
    pytest.param(
        b"[0-1]",
        b'{"error":"Expected \',\' or \']\' after array element in JSON at position 2 (line 1 column 3)","details":{},"code":400}',
        id="zero then minus",
    ),
    pytest.param(
        b'{ \n\t "query" \n : \n x}',
        b'{"error":"Unexpected token \'x\', ...\\"ry\\" \\n : \\n x}\\" is not valid JSON","details":{},"code":400}',
        id="whitespace then bad",
    ),
    pytest.param(
        b'{\r\n"query":\r\n}',
        b'{"error":"Unexpected token \'}\', \\"{\\r\\n\\"query\\":\\r\\n}\\" is not valid JSON","details":{},"code":400}',
        id="CRLF then bad",
    ),
    pytest.param(b"", _NO_KEYS, id="empty"),
    pytest.param(b"\xef\xbb\xbf", _NO_KEYS, id="a byte-order mark alone"),
    pytest.param(b"{}", _NO_KEYS, id="an empty object"),
    pytest.param(b"[]", _BATCHING, id="an array"),
    pytest.param(b'[{"query":"{ __typename }"}]', _BATCHING, id="a batch of one query"),
    pytest.param(b'{"query":5}', _apollo(b"GraphQL queries must be strings."), id="a query of 5"),
    pytest.param(
        b'{"query":"{ __typename }","variables":"x"}',
        _apollo(
            b"`variables` in a POST body should be provided as an object, not a recursively "
            b"JSON-encoded string."
        ),
        id="a string of variables",
    ),
    pytest.param(
        b'{"query":"{ __typename }","extensions":5}',
        _apollo(b"`extensions` in a POST body must be an object if provided."),
        id="extensions of 5",
    ),
    pytest.param(
        b'{"query":"{ __typename }","operationName":5}',
        _apollo(b"`operationName` in a POST body must be a string if provided."),
        id="an operationName of 5",
    ),
    pytest.param(
        b'{"query":"{ __typename }","extensions":{"persistedQuery":{"version":1,"sha256Hash":"'
        + b"0" * 64
        + b'"}}}',
        b'{"errors":[{"message":"Internal server error","extensions":{"http":{"status":400,'
        b'"headers":{}},"code":"INTERNAL_SERVER_ERROR","type":"internal error","userError":false}}]}',
        id="a persisted query's hash that does not match",
    ),
    pytest.param(
        b'{"query":"{ __typename }","extensions":{"persistedQuery":{"version":2,"sha256Hash":"'
        + b"0" * 64
        + b'"}}}',
        b'{"errors":[{"message":"Internal server error","extensions":{"http":{"status":400,"headers":{}},"code":"INTERNAL_SERVER_ERROR","type":"internal error","userError":false}}]}',
        id="a persisted query's version 2",
    ),
    pytest.param(
        b'{"extensions":{"persistedQuery":{"version":1,"sha256Hash":"' + b"1" * 64 + b'"}}}',
        b'{"errors":[{"message":"PersistedQueryNotFound","extensions":{"http":{"status":200,'
        b'"headers":{}},"code":"PERSISTED_QUERY_NOT_FOUND","type":"graphql error",'
        b'"userError":true}}]}',
        id="a persisted query's hash alone",
    ),
    pytest.param(
        b'{"query":null}',
        b'{"errors":[{"message":"GraphQL operations must contain a non-empty `query` or a '
        b'`persistedQuery` extension.","extensions":{"http":{"status":400,"headers":{}},'
        b'"code":"INVALID_INPUT","type":"invalid input","userError":true}}]}',
        id="a query of null",
    ),
    pytest.param(
        b'{"query":"","extensions":{"persistedQuery":{"version":1,"sha256Hash":"'
        + hashlib.sha256(b"").hexdigest().encode()
        + b'"}}}',
        b'{"errors":[{"message":"Syntax Error: Unexpected <EOF>.","locations":[{"line":1,'
        b'"column":1}],"extensions":{"http":{"status":400,"headers":{}},'
        b'"code":"GRAPHQL_PARSE_FAILED","type":"graphql error","userError":true}}]}',
        id="a persisted query's hash matching an empty query",
    ),
    pytest.param(
        b'{"query":"query __proto__ { __typename }"}', None, id="an operation named __proto__"
    ),
    pytest.param(
        b'{"query":"{"}',
        b'{"errors":[{"message":"Syntax Error: Expected Name, found <EOF>.","locations":[{"line":1,'
        b'"column":2}],"extensions":{"http":{"status":400,"headers":{}},'
        b'"code":"GRAPHQL_PARSE_FAILED","type":"graphql error","userError":true}}]}',
        id="a document that does not parse",
    ),
    pytest.param(
        b'{"query":"{ nosuch }"}',
        b'{"errors":[{"message":"Cannot query field \\"nosuch\\" on type \\"Query\\".",'
        b'"locations":[{"line":1,"column":3}],"extensions":{"http":{"status":400,"headers":{}},'
        b'"code":"GRAPHQL_VALIDATION_FAILED","type":"graphql error","userError":true}}]}',
        id="a document that does not validate",
    ),
    pytest.param(
        b'{"query":"query A { __typename } query B { __typename }"}',
        b'{"errors":[{"message":"operation does not exist","extensions":{"type":"invalid input",'
        b'"code":"INPUT_ERROR","statusCode":400,"userError":true,"userPresentableMessage":"The '
        b'operation does not exist on the query.","http":{"status":400}}}]}',
        id="two operations and no operationName",
    ),
    pytest.param(
        b'{"query":"query($b: Boolean!) { __typename @include(if: $b) }"}',
        WithTheKey(
            b'{"errors":[{"message":"Variable \\"$b\\" of required type \\"Boolean!\\" was not '
            b'provided.","locations":[{"line":1,"column":7}],"extensions":{"code":"BAD_USER_INPUT",'
            b'"type":"graphql error","userError":true}}]}'
        ),
        id="a variable not provided",
    ),
    pytest.param(
        b'{"query":"{ __typename }","operationName":"Z"}',
        b'{"errors":[{"message":"operation does not exist","extensions":{"type":"invalid input","code":"INPUT_ERROR","statusCode":400,"userError":true,"userPresentableMessage":"The operation \'Z\' does not exist on the query.","http":{"status":400}}}]}',
        id="an operationName no operation has",
    ),
    pytest.param(
        b'{"query":"query A { __typename }","operationName":""}',
        b'{"errors":[{"message":"operation does not exist","extensions":{"type":"invalid input","code":"INPUT_ERROR","statusCode":400,"userError":true,"userPresentableMessage":"The operation does not exist on the query.","http":{"status":400}}}]}',
        id="an empty operationName",
    ),
    pytest.param(
        b'{"query":{"kind":"Document"}}',
        b'{"errors":[{"message":"GraphQL queries must be strings. It looks like you\'re sending the internal graphql-js representation of a parsed query in your request instead of a request in the GraphQL query language. You can convert an AST to a string using the `print` function from `graphql`, or use a client like `apollo-client` which converts the internal representation to a string for you.","extensions":{"http":{"status":400,"headers":{}},"code":"BAD_REQUEST","type":"graphql error","userError":true}}]}',
        id="a parsed document",
    ),
    pytest.param(
        b'{"query":false}',
        b'{"errors":[{"message":"GraphQL operations must contain a non-empty `query` or a `persistedQuery` extension.","extensions":{"http":{"status":400,"headers":{}},"code":"INVALID_INPUT","type":"invalid input","userError":true}}]}',
        id="a query of false",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":"[1]"}',
        b'{"errors":[{"message":"`variables` in a POST body must be an object if provided.","extensions":{"http":{"status":400,"headers":{}},"code":"BAD_REQUEST","type":"graphql error","userError":true}}]}',
        id="variables of '[1]'",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":"null"}',
        None,
        id="variables of 'null'",
    ),
    pytest.param(
        b'{"query":"{ __typename }","variables":" {} "}',
        None,
        id="variables of ' {} '",
    ),
    pytest.param(
        b'{"query":"query($b: Boolean!) { __typename @include(if: $b) }","variables":"{\\"b\\":5}"}',
        WithTheKey(
            b'{"errors":[{"message":"Variable \\"$b\\" got invalid value 5; Boolean cannot represent a non boolean value: 5","locations":[{"line":1,"column":7}],"extensions":{"code":"BAD_USER_INPUT","type":"graphql error","userError":true}}]}'
        ),
        id="variables of a string its operation refuses",
    ),
    pytest.param(
        b'{"query":"query($b: Boolean!) { __typename @include(if: $b) }","variables":"{\\"b\\":true}"}',
        None,
        id="variables of a string its operation takes",
    ),
    pytest.param(
        b'{"query":"query($v: [ID!]) { issues(first: 1, filter: {id: {in: $v}}) { nodes { id } } }",'
        b'"variables":{"v":[%s]}}' % b",".join([b"true"] * 60),
        WithTheKey(
            b'{"errors":[%s,{"message":"Internal server error","extensions":{"code":'
            b'"INTERNAL_SERVER_ERROR","type":"internal error","userError":false}}]}'
            % b",".join(
                b'{"message":"Variable \\"$v\\" got invalid value true at \\"v[%d]\\"; ID cannot '
                b'represent value: true","locations":[{"line":1,"column":7}],"extensions":{"code":'
                b'"BAD_USER_INPUT","type":"graphql error","userError":true}}' % n
                for n in range(50)
            )
        ),
        id="sixty variables refused",
    ),
    pytest.param(
        b'{"query":"mutation { __typename }"}',
        WithTheKey(b'{"data":{"__typename":"Mutation"}}', 200),
        id="a mutation of __typename",
    ),
    pytest.param(
        b'{"query":"subscription S { __typename }"}',
        b'{"errors":[{"message":"Subscription \\"S\\" must not select an introspection top level field.","locations":[{"line":1,"column":18}],"extensions":{"http":{"status":400,"headers":{}},"code":"GRAPHQL_VALIDATION_FAILED","type":"graphql error","userError":true}}]}',
        id="a subscription of __typename",
    ),
    pytest.param(
        b'{"query":"query($x: String = \\"a\\") { issue(id: $x) { id } }","variables":{"x":null}}',
        WithTheKey(
            b'{"errors":[{"message":"Internal server error","path":["issue"],"locations":[{"line":1,"column":37}],"extensions":{"code":"INTERNAL_SERVER_ERROR","type":"internal error","userError":false}}],"data":null}',
            200,
        ),
        id="null for a non-null argument",
    ),
    pytest.param(
        b'{"query":"query($v: Boolean = true) { __typename @include(if: $v) }","variables":{"v":null}}',
        WithTheKey(
            b'{"errors":[{"message":"Internal server error","locations":[{"line":1,"column":53}],"extensions":{"code":"INTERNAL_SERVER_ERROR","type":"internal error","userError":false}}],"data":null}',
            200,
        ),
        id="null for a directive's argument at the root",
    ),
    pytest.param(
        b'{"query":"query($x: String = \\"a\\") { team(id: $x) { id } }","variables":{"x":null}}',
        WithTheKey(
            b'{"errors":[{"message":"Internal server error","path":["team"],"locations":[{"line":1,"column":36}],"extensions":{"code":"INTERNAL_SERVER_ERROR","type":"internal error","userError":false}}],"data":null}',
            200,
        ),
        id="null for team's non-null id",
    ),
    pytest.param(
        b'{"query":"query($v: ID = \\"a\\") { issues(filter: {id: {in: [$v]}}) { nodes { id } } }","variables":{"v":null}}',
        WithTheKey(
            b'{"errors":[{"message":"Internal server error","path":["issues"],"locations":[{"line":1,"column":38}],"extensions":{"code":"INTERNAL_SERVER_ERROR","type":"internal error","userError":false}}],"data":null}',
            200,
        ),
        id="null inside a filter's list",
    ),
    pytest.param(
        b'{"query":"query($v: Boolean = true) { viewer { id @include(if: $v) } }","variables":{"v":null}}',
        WithTheKey(
            b'{"errors":[{"message":"Internal server error","path":["viewer"],"locations":[{"line":1,"column":54}],"extensions":{"code":"INTERNAL_SERVER_ERROR","type":"internal error","userError":false}}],"data":null}',
            200,
        ),
        id="null for a directive one level down",
    ),
    pytest.param(
        b'{"query":"query($x: String = \\"a\\") # a\xe2\x80\xa8b\\n{ issue(id: $x) { id } }","variables":{"x":null}}',
        WithTheKey(
            b'{"errors":[{"message":"Internal server error","path":["issue"],"locations":[{"line":2,"column":13}],"extensions":{"code":"INTERNAL_SERVER_ERROR","type":"internal error","userError":false}}],"data":null}',
            200,
        ),
        id="null for a non-null argument after U+2028 in a comment",
    ),
    pytest.param(
        b'{"query":"{ __schema { queryType { name } } }"}',
        b'{"data":{"__schema":{"queryType":{"name":"Query"}}}}',
        id="introspection",
    ),
    pytest.param(
        b'{"query":"{ __type(name: \\"Query\\") { name } }"}',
        b'{"data":{"__type":{"name":"Query"}}}',
        id="__type",
    ),
    pytest.param(
        b'{"query":"{ s: __schema { queryType { name } } }"}',
        b'{"data":{"s":{"queryType":{"name":"Query"}}}}',
        id="an alias of __schema",
    ),
    pytest.param(
        b'{"query":"{ __schema { queryType { name } } __type(name: \\"Issue\\") { name } }"}',
        b'{"data":{"__schema":{"queryType":{"name":"Query"}},"__type":{"name":"Issue"}}}',
        id="__schema and __type",
    ),
    pytest.param(
        b'{"query":"{ ...F } fragment F on Query { __schema { queryType { name } } }"}',
        b'{"data":{"__schema":{"queryType":{"name":"Query"}}}}',
        id="__schema in a fragment",
    ),
    pytest.param(
        b'{"query":"{ ... on Query { __schema { queryType { name } } } }"}',
        b'{"data":{"__schema":{"queryType":{"name":"Query"}}}}',
        id="__schema in an inline fragment",
    ),
    pytest.param(
        b'{"query":"{ __schema @skip(if: false) { queryType { name } } }"}',
        b'{"data":{"__schema":{"queryType":{"name":"Query"}}}}',
        id="__schema under @skip(if: false)",
    ),
    pytest.param(
        b'{"query":"query Q { __schema { queryType { name } } }"}',
        b'{"data":{"__schema":{"queryType":{"name":"Query"}}}}',
        id="a named query of __schema",
    ),
    pytest.param(
        b'{"query":"query A { __schema { queryType { name } } } query B { viewer { id } }","operationName":"A"}',
        b'{"data":{"__schema":{"queryType":{"name":"Query"}}}}',
        id="the introspection operationName chose",
    ),
    pytest.param(
        b'{"query":"query($b: Boolean!) { __schema @include(if: $b) { queryType { name } } }"}',
        b'{"errors":[{"message":"Variable \\"$b\\" of required type \\"Boolean!\\" was not provided.","locations":[{"line":1,"column":7}],"extensions":{"code":"BAD_USER_INPUT","type":"graphql error","userError":true}}]}',
        id="introspection with a variable not provided",
    ),
    pytest.param(
        b'{"query":"{ __typename __schema { queryType { name } } }"}',
        WithTheKey(
            b'{"data":{"__typename":"Query","__schema":{"queryType":{"name":"Query"}}}}', 200
        ),
        id="__typename beside __schema",
    ),
    pytest.param(
        b'{"query":"{ __schema { queryType { name } } viewer @skip(if: true) { id } }"}',
        WithTheKey(b'{"data":{"__schema":{"queryType":{"name":"Query"}}}}', 200),
        id="__schema beside a skipped field",
    ),
    pytest.param(
        b'{"query":"{ viewer @skip(if: true) { id } }"}',
        WithTheKey(b'{"data":{}}', 200),
        id="a skipped field alone",
    ),
    pytest.param(
        b'{"query":"{ ...F } fragment F on Query { __typename }"}',
        None,
        id="__typename in a fragment",
    ),
    pytest.param(
        b'{"query":"{ ... on Query { __typename } }"}',
        None,
        id="__typename in an inline fragment",
    ),
    pytest.param(
        b'{"query":"{ __schema { queryType { name } } ...F } fragment F on Query { __typename }"}',
        WithTheKey(
            b'{"data":{"__schema":{"queryType":{"name":"Query"}},"__typename":"Query"}}', 200
        ),
        id="__schema and __typename in a fragment",
    ),
    pytest.param(
        b'{"query":"{ ... on Query { __schema { queryType { name } } } ...F } fragment F on Query { __typename }"}',
        WithTheKey(
            b'{"data":{"__schema":{"queryType":{"name":"Query"}},"__typename":"Query"}}', 200
        ),
        id="__typename in a fragment beside an inline __schema",
    ),
    pytest.param(
        b'{"query":"{ ...F }"}',
        b'{"errors":[{"message":"Unknown fragment \\"F\\".","locations":[{"line":1,"column":6}],"extensions":{"http":{"status":400,"headers":{}},"code":"GRAPHQL_VALIDATION_FAILED","type":"graphql error","userError":true}}]}',
        id="a fragment not defined",
    ),
    pytest.param(
        b'{"query":"fragment F on Query { ...F } { ...F }"}',
        b'{"errors":[{"message":"Cannot spread fragment \\"F\\" within itself.","locations":[{"line":1,"column":23}],"extensions":{"http":{"status":400,"headers":{}},"code":"GRAPHQL_VALIDATION_FAILED","type":"graphql error","userError":true}}]}',
        id="a fragment that spreads itself",
    ),
    *_UNKNOWN_IDS,
]


_TYPENAME = b'{"query":"{ __typename }"}'
_LIMIT = 10 * 2**20


def _express(status: int, message: bytes) -> bytes:
    """Express's envelope as Linear writes it around ``message``, already JSON-escaped."""
    return b'{"error":"%s","details":{},"code":%d}' % (message, status)


def _charset(name: str) -> tuple:
    return (("Content-Type", f"application/json; charset={name}"),)


_GZIP = (("Content-Encoding", "gzip"),)
_EXPECTED_PROPERTY = _express(
    400, b"Expected property name or '}' in JSON at position 1 (line 1 column 2)"
)
_LATIN1 = _express(415, b'unsupported charset \\"LATIN1\\"')
_HEADER_CHECK = _express(400, b"incorrect header check")
_CUT_SHORT = _express(400, b"unexpected end of file")
_BOGUS = _express(415, b'unsupported content encoding \\"bogus\\"')
_TOO_LARGE = _express(413, b"request entity too large")
_TOO_LARGE_DECLARED = (
    b'{"error":"request entity too large","details":{},"extensions":{"code":"PAYLOAD_TOO_LARGE",'
    b'"type":"payload too large","statusCode":413,"userError":true,"limit":10485760,'
    b'"length":10485761},"code":413}'
)
# Forty ideographs outnumber the ASCII around them, so without a byte-order mark iconv-lite reads
# the body in the order that is wrong for it, and Linear quotes the characters that make.
_IDEOGRAPHS = '{"query":"{ __typename }","x":"' + "\u4e00" * 40 + '"}'
_IDEOGRAPHS_MISREAD = _express(
    400,
    "Unexpected token '\u7b00', \\\"\u7b00\u2200\u7100\u7500\u6500\u7200\u7900\u2200\u3a00"
    '\u2200\\"... is not valid JSON'.encode(),
)
_LINEAR_READS = [
    pytest.param(_GZIP, gzip.compress(_TYPENAME), None, id="gzip"),
    pytest.param((("Content-Encoding", "GZIP"),), gzip.compress(_TYPENAME), None, id="GZIP"),
    pytest.param((("Content-Encoding", "deflate"),), zlib.compress(_TYPENAME), None, id="deflate"),
    pytest.param((("Content-Encoding", ""),), _TYPENAME, None, id="an empty encoding"),
    pytest.param(
        (("Content-Encoding", " gzip "),), gzip.compress(_TYPENAME), None, id="spaces around gzip"
    ),
    pytest.param(
        (("Content-Encoding", "gzip\t"),), gzip.compress(_TYPENAME), None, id="a tab after gzip"
    ),
    pytest.param(
        (("Content-Encoding", "gzip\xa0"),),
        gzip.compress(_TYPENAME),
        _express(415, 'unsupported content encoding \\"gzip\xa0\\"'.encode()),
        id="U+00A0 after gzip",
    ),
    pytest.param(
        (("Content-Encoding", "\xa0gzip"),),
        gzip.compress(_TYPENAME),
        _express(415, 'unsupported content encoding \\"\xa0gzip\\"'.encode()),
        id="U+00A0 before gzip",
    ),
    pytest.param(
        (("Content-Encoding", "gzip\x85"),),
        gzip.compress(_TYPENAME),
        _express(415, 'unsupported content encoding \\"gzip\x85\\"'.encode()),
        id="U+0085 after gzip",
    ),
    pytest.param(
        (("Content-Encoding", "g zip"),),
        gzip.compress(_TYPENAME),
        _express(415, b'unsupported content encoding \\"g zip\\"'),
        id="a space inside",
    ),
    pytest.param(
        (("Content-Encoding", "x-gzip"),),
        gzip.compress(_TYPENAME),
        _express(415, b'unsupported content encoding \\"x-gzip\\"'),
        id="x-gzip",
    ),
    pytest.param((("Content-Encoding", "bogus"),), _TYPENAME, _BOGUS, id="bogus"),
    pytest.param((("Content-Encoding", "BOGUS"),), _TYPENAME, _BOGUS, id="BOGUS"),
    pytest.param(
        (("Content-Encoding", "gzip"), ("Content-Encoding", "gzip")),
        gzip.compress(gzip.compress(_TYPENAME)),
        _express(415, b'unsupported content encoding \\"gzip, gzip\\"'),
        id="gzip twice",
    ),
    pytest.param(
        (("Content-Encoding", ""), ("Content-Encoding", "gzip")),
        gzip.compress(_TYPENAME),
        None,
        id="an empty encoding, then gzip",
    ),
    pytest.param(
        (("Content-Encoding", " "), ("Content-Encoding", "gzip")),
        gzip.compress(_TYPENAME),
        None,
        id="a space, then gzip",
    ),
    pytest.param(
        (("Content-Encoding", ""), ("Content-Encoding", ""), ("Content-Encoding", "gzip")),
        gzip.compress(_TYPENAME),
        None,
        id="two empty encodings, then gzip",
    ),
    pytest.param(
        (("Content-Encoding", ""), ("Content-Encoding", "")),
        _TYPENAME,
        None,
        id="two empty encodings",
    ),
    pytest.param(
        (("Content-Encoding", "gzip"), ("Content-Encoding", "")),
        gzip.compress(_TYPENAME),
        _express(415, b'unsupported content encoding \\"gzip,\\"'),
        id="gzip, then an empty encoding",
    ),
    pytest.param(
        (("Content-Encoding", "gzip"), ("Content-Encoding", " ")),
        gzip.compress(_TYPENAME),
        _express(415, b'unsupported content encoding \\"gzip,\\"'),
        id="gzip, then a space",
    ),
    pytest.param(
        (("Content-Encoding", ""), ("Content-Encoding", "gzip"), ("Content-Encoding", "")),
        gzip.compress(_TYPENAME),
        _express(415, b'unsupported content encoding \\"gzip,\\"'),
        id="gzip between empty encodings",
    ),
    pytest.param(
        (("Content-Encoding", "gzip"), ("Content-Encoding", " "), ("Content-Encoding", "gzip")),
        gzip.compress(_TYPENAME),
        _express(415, b'unsupported content encoding \\"gzip, , gzip\\"'),
        id="gzip, a space, gzip",
    ),
    pytest.param(
        (("Content-Encoding", "bogus"), ("Content-Encoding", "")),
        _TYPENAME,
        _express(415, b'unsupported content encoding \\"bogus,\\"'),
        id="bogus, then an empty encoding",
    ),
    pytest.param(
        (("Content-Encoding", "gzip, "),),
        gzip.compress(_TYPENAME),
        _express(415, b'unsupported content encoding \\"gzip,\\"'),
        id="gzip and a comma and a space",
    ),
    pytest.param(
        (("Content-Encoding", "gzip ,"),),
        gzip.compress(_TYPENAME),
        _express(415, b'unsupported content encoding \\"gzip ,\\"'),
        id="a space before a comma",
    ),
    pytest.param(
        (("Content-Encoding", "gzip,"), ("Content-Encoding", "gzip")),
        gzip.compress(_TYPENAME),
        _express(415, b'unsupported content encoding \\"gzip,, gzip\\"'),
        id="a comma ending an encoding, then gzip",
    ),
    pytest.param(
        (("Content-Encoding", "gzip, identity"),),
        gzip.compress(_TYPENAME),
        _express(415, b'unsupported content encoding \\"gzip, identity\\"'),
        id="gzip, identity",
    ),
    pytest.param(_GZIP, _TYPENAME, _HEADER_CHECK, id="gzip over a plain body"),
    pytest.param(_GZIP, gzip.compress(_TYPENAME) + b"xx", _HEADER_CHECK, id="gzip then xx"),
    pytest.param(_GZIP, gzip.compress(_TYPENAME)[:-5], _CUT_SHORT, id="gzip cut short"),
    pytest.param(_GZIP, b"", _CUT_SHORT, id="gzip over no bytes"),
    pytest.param(_GZIP, gzip.compress(b""), _NO_KEYS, id="gzip of no bytes"),
    pytest.param(_GZIP, gzip.compress(b"{"), _EXPECTED_PROPERTY, id="gzip of {"),
    pytest.param(
        (("Content-Encoding", "deflate"),),
        zlib.compress(_TYPENAME)[2:-4],
        _HEADER_CHECK,
        id="raw deflate",
    ),
    pytest.param(_charset("utf-16le"), _TYPENAME.decode().encode("utf-16-le"), None, id="utf-16le"),
    pytest.param(
        _charset("utf-16-le"), _TYPENAME.decode().encode("utf-16-le"), None, id="utf-16-le"
    ),
    pytest.param(_charset("utf-16be"), _TYPENAME.decode().encode("utf-16-be"), None, id="utf-16be"),
    pytest.param(
        _charset("utf-16"), _TYPENAME.decode().encode("utf-16"), None, id="utf-16, marked"
    ),
    pytest.param(
        _charset("utf-16"), _TYPENAME.decode().encode("utf-16-be"), None, id="utf-16, big-endian"
    ),
    pytest.param(
        _charset("utf-16"), _TYPENAME.decode().encode("utf-16-le"), None, id="utf-16, little-endian"
    ),
    pytest.param(
        _charset("utf-16le"),
        '\ufeff{"query":"{ __typename }"}'.encode("utf-16-le"),
        None,
        id="utf-16le, marked",
    ),
    pytest.param(
        _charset("utf-16le"), _TYPENAME.decode().encode("utf-16-le") + b"x", None, id="odd bytes"
    ),
    pytest.param(
        _charset("utf-16le"),
        b'{"query":"{ __typename }","x":"'.decode().encode("utf-16-le")
        + b"\x00\xd8"
        + b'"}'.decode().encode("utf-16-le"),
        None,
        id="a lone surrogate",
    ),
    pytest.param(
        _charset("utf-32"), _TYPENAME.decode().encode("utf-32"), None, id="utf-32, marked"
    ),
    pytest.param(
        _charset("utf-32"), _TYPENAME.decode().encode("utf-32-be"), None, id="utf-32, big-endian"
    ),
    pytest.param(_charset("utf-32le"), _TYPENAME.decode().encode("utf-32-le"), None, id="utf-32le"),
    pytest.param(_charset("UTF-8"), _TYPENAME, None, id="UTF-8"),
    pytest.param(_charset('"utf-8"'), _TYPENAME, None, id="utf-8 quoted"),
    pytest.param(_charset("utf-7"), _TYPENAME, None, id="utf-7"),
    pytest.param(_charset("utf-7-imap"), _TYPENAME, None, id="utf-7-imap"),
    pytest.param(
        _charset("utf-16le"),
        "5".encode("utf-16-le"),
        _express(400, b"Unexpected token '5', \\\"5\\\" is not valid JSON"),
        id="utf-16le 5",
    ),
    pytest.param(
        _charset("utf-16le"), "{".encode("utf-16-le"), _EXPECTED_PROPERTY, id="utf-16le {"
    ),
    pytest.param(_charset("utf-16le"), b"", _NO_KEYS, id="utf-16le, no bytes"),
    pytest.param(_charset("utf-8; foo=bar"), _TYPENAME, None, id="another parameter"),
    pytest.param(_charset('"utf\\-8"'), _TYPENAME, None, id="a quoted pair"),
    pytest.param(_charset('"lat\\in1"'), _TYPENAME, _LATIN1, id="a quoted pair, latin1"),
    pytest.param(
        (("Content-Type", "application/json; charset = latin1"),),
        _TYPENAME,
        _LATIN1,
        id="spaces around =",
    ),
    pytest.param(_charset('"utf-8:1234"'), _TYPENAME, None, id="a trailing :1234"),
    pytest.param(_charset("utf-32be"), _TYPENAME.decode().encode("utf-32-be"), None, id="utf-32be"),
    pytest.param(
        _charset("utf-32be"),
        b'{"query":"{ __typename }","x":"'.decode().encode("utf-32-be")
        + b"\x00\x11\x00\x00"
        + b'"}'.decode().encode("utf-32-be"),
        None,
        id="past U+10FFFF",
    ),
    pytest.param(
        _charset("utf-16le"), _IDEOGRAPHS.encode("utf-16-le"), None, id="utf-16le, ideographs"
    ),
    pytest.param(
        _charset("utf-16"), _IDEOGRAPHS.encode("utf-16"), None, id="utf-16, marked, ideographs"
    ),
    pytest.param(
        _charset("utf-16"),
        b"\xfe\xff" + _IDEOGRAPHS.encode("utf-16-be"),
        None,
        id="utf-16, marked big-endian, ideographs",
    ),
    pytest.param(
        _charset("utf-16"),
        _IDEOGRAPHS.encode("utf-16-le"),
        _IDEOGRAPHS_MISREAD,
        id="utf-16, little-endian ideographs",
    ),
    pytest.param(
        _charset("utf-16"),
        _IDEOGRAPHS.encode("utf-16-be"),
        _IDEOGRAPHS_MISREAD,
        id="utf-16, big-endian ideographs",
    ),
    pytest.param(_charset("latin1"), _TYPENAME, _LATIN1, id="latin1"),
    pytest.param(
        _charset("iso-8859-1"),
        _TYPENAME,
        _express(415, b'unsupported charset \\"ISO-8859-1\\"'),
        id="iso-8859-1",
    ),
    pytest.param(
        _charset("utf8"), _TYPENAME, _express(415, b'unsupported charset \\"UTF8\\"'), id="utf8"
    ),
    pytest.param(
        _charset("utf-nope"),
        _TYPENAME,
        _express(415, b'unsupported charset \\"UTF-NOPE\\"'),
        id="utf-nope",
    ),
    pytest.param(
        _charset("utf_16le"),
        _TYPENAME.decode().encode("utf-16-le"),
        _express(415, b'unsupported charset \\"UTF_16LE\\"'),
        id="utf_16le",
    ),
    pytest.param(
        (("Content-Type", "Application/JSON; Charset=Latin1"),), _TYPENAME, _LATIN1, id="Charset"
    ),
    pytest.param(_charset("utf-8; charset=latin1"), _TYPENAME, _LATIN1, id="two charsets"),
    pytest.param(
        _charset("latin1") + (("Content-Encoding", "bogus"),),
        _TYPENAME,
        _LATIN1,
        id="latin1, bogus",
    ),
    pytest.param(
        _charset("utf-nope") + (("Content-Encoding", "bogus"),),
        _TYPENAME,
        _BOGUS,
        id="utf-nope, bogus",
    ),
    pytest.param((), padded_query(_LIMIT), None, id="the limit"),
    pytest.param((), padded_query(_LIMIT + 1), _TOO_LARGE_DECLARED, id="past the limit"),
    pytest.param(_GZIP, gzip.compress(padded_query(_LIMIT)), None, id="the limit, gzipped"),
    pytest.param(_GZIP, gzip.compress(padded_query(_LIMIT + 1)), _TOO_LARGE, id="past it, gzipped"),
    pytest.param(
        _GZIP, gzip.compress(padded_query(_LIMIT + 2)), _TOO_LARGE, id="past it by two, gzipped"
    ),
    pytest.param(
        (("Content-Encoding", "deflate"),),
        zlib.compress(padded_query(_LIMIT + 1)),
        _TOO_LARGE,
        id="past it, deflated",
    ),
    pytest.param(_GZIP, b"x" * (_LIMIT + 1), _HEADER_CHECK, id="past it, not gzip"),
    pytest.param((), b"{" * (_LIMIT + 1), _TOO_LARGE_DECLARED, id="past it, not JSON"),
    pytest.param(
        (("Content-Encoding", "bogus"),), padded_query(_LIMIT + 1), _BOGUS, id="past it, bogus"
    ),
    pytest.param(_charset("latin1"), padded_query(_LIMIT + 1), _LATIN1, id="past it, latin1"),
    pytest.param(
        _charset("utf-nope"), padded_query(_LIMIT + 1), _TOO_LARGE_DECLARED, id="past it, utf-nope"
    ),
]


# `Bearer` and a value in a Linear API key's shape that no account holds.
_KEY_SHAPED = "Bearer lin_api_" + "0" * 40
# The rows every credential but the key is sent with: one for each way `_read` refuses a body (V8's
# words, `strict`'s, graphql-js's, a body it cannot decode, an unsupported encoding, an unsupported
# charset, past the limit), one it reads and serves, one for each stage of Apollo Server's that
# refuses a document ahead of the credential (a batch, a query that is not a string, a persisted
# query's hash alone and one that does not match, a parse, a validation, the choice of operation),
# and a `WithTheKey` row.
_EACH_WAY_READ = (
    "{",
    "top-level 5",
    "empty",
    "gzip over a plain body",
    "bogus",
    "latin1",
    "past the limit",
    "__proto__ key",
    "an array",
    "a query of null",
    "a persisted query's hash alone",
    "a persisted query's hash that does not match",
    "a document that does not parse",
    "a document that does not validate",
    "two operations and no operationName",
    "a variable not provided",
)
# The rows whose answer without a key turns on what the document selects
# (`backlot.routers.linear._introspection_only`), each sent with every credential too.
_WHAT_IT_SELECTS = (
    "introspection",
    "__type",
    "an alias of __schema",
    "__schema and __type",
    "__schema in a fragment",
    "__schema in an inline fragment",
    "__schema under @skip(if: false)",
    "a named query of __schema",
    "the introspection operationName chose",
    "introspection with a variable not provided",
    "__typename beside __schema",
    "__schema beside a skipped field",
    "a skipped field alone",
    "__typename in a fragment",
    "__typename in an inline fragment",
    "__schema and __typename in a fragment",
    "__typename in a fragment beside an inline __schema",
)
assert set(_EACH_WAY_READ + _WHAT_IT_SELECTS) <= {
    row.id for row in (*_LINEAR_BODIES, *_LINEAR_READS)
}


def _linear_body_cases() -> list:
    rows = [pytest.param((), *row.values, id=row.id) for row in _LINEAR_BODIES] + _LINEAR_READS
    return [
        pytest.param(credential, *row.values, id=f"{row.id}-{credential}")
        for credential in ("key", "none", "nope", "Bearer key", "Bearer, key-shaped")
        for row in rows
        if credential == "key" or row.id in _EACH_WAY_READ + _WHAT_IT_SELECTS
    ]


@pytest.mark.parametrize("credential,headers,body,answer", _linear_body_cases())
def test_linear_orders_the_body_and_the_credential(
    client, tokens_yaml, credential, headers, body, answer
):
    """Each row answers as the comment over `_LINEAR_BODIES` says with the key, and each row
    `_EACH_WAY_READ` or `_WHAT_IT_SELECTS` names answers so under no credential and `nope` too; an
    API key under `Bearer`, and `_KEY_SHAPED`, are `_API_KEY_AS_BEARER` ahead of each of those
    bodies."""
    admin = tokens_yaml["admin_token"]
    if not any(name == "Content-Type" for name, _ in headers):
        headers = (("Content-Type", "application/json"), *headers)
    if credential != "none":
        key = {"key": admin, "nope": "nope", "Bearer key": f"Bearer {admin}"}.get(
            credential, _KEY_SHAPED
        )
        headers = (*headers, ("Authorization", key))
    r = sent_as(client, "POST", "/linear/graphql", headers, content=body)
    if credential.startswith("Bearer"):
        assert (r.status_code, r.content) == (400, _API_KEY_AS_BEARER)
    elif credential != "key" and (answer is None or isinstance(answer, WithTheKey)):
        assert (r.status_code, r.content) == (401, _UNAUTHENTICATED)
    elif answer is None:
        assert (r.status_code, r.json()) == (200, {"data": {"__typename": "Query"}})
    elif isinstance(answer, WithTheKey):
        assert (r.status_code, r.content) == (answer.status, answer)
    else:
        assert (r.status_code, r.content) == (_status(answer), answer)


def test_linear_parent_resolves_and_is_acl_scoped(client, admin_h, tokens_yaml):
    """`Issue.parent` is declared in the SDL and `@linear/sdk`'s fragment selects `parent { id }`.
    The bench fills `parent_issue` on 46.7% of records, so it must resolve — and it must resolve
    through the ACL, or it becomes another way to confirm a hidden issue exists."""
    # lin-batch (ENG-102) is parented to lin-secret (ENG-103), which only hana can read.
    q = '{ issue(id: "ENG-102") { identifier parent { identifier title } } }'
    as_hana = gql(client, q, {"Authorization": linear_user_token(tokens_yaml, "hana@acme.com")})
    assert as_hana.json()["data"]["issue"]["parent"]["identifier"] == "ENG-103"
    as_ava = gql(client, q, {"Authorization": linear_user_token(tokens_yaml, "ava@acme.com")})
    assert as_ava.json()["data"]["issue"]["parent"] is None  # hidden parent -> null, not a leak
    # admin sees it, confirming the null above is the ACL and not a broken lookup
    assert gql(client, q, admin_h).json()["data"]["issue"]["parent"]["identifier"] == "ENG-103"


def test_linear_issue_without_a_parent_is_null(client, admin_h):
    assert (
        gql(client, '{ issue(id: "ENG-101") { parent { identifier } } }', admin_h).json()["data"][
            "issue"
        ]["parent"]
        is None
    )


@pytest.mark.parametrize(("connection", "args"), [("issues", "first: 50"), ("users", "")])
@pytest.mark.parametrize(
    ("order_by", "field"),
    [
        ("", "createdAt"),
        ("orderBy: createdAt", "createdAt"),
        ("orderBy: updatedAt", "updatedAt"),
    ],
    ids=["default", "createdAt", "updatedAt"],
)
def test_linear_default_and_order_by_are_newest_first(
    client, admin_h, connection, args, order_by, field
):
    """`issues` and `users` are newest first by the field `orderBy` names, and by `createdAt` with
    no `orderBy`, as the comment on `LINEAR_DEFAULT_ORDER_BY` records."""
    joined = ", ".join(a for a in (args, order_by) if a)
    q = "{ %s%s { nodes { createdAt updatedAt } } }" % (connection, f"({joined})" if joined else "")
    stamps = [n[field] for n in gql(client, q, admin_h).json()["data"][connection]["nodes"]]
    assert len(stamps) >= 2
    assert stamps == sorted(stamps, reverse=True)


def test_linear_sort_input_overrides_the_default_ordering(client, admin_h):
    """`orderBy` carries no direction in Linear, so `sort:` is how a client asks for the other
    one — which means it has to actually win over the default."""
    q = "{ issues(first: 50, sort: [{createdAt: {order: Ascending}}]) { nodes { createdAt } } }"
    got = [n["createdAt"] for n in gql(client, q, admin_h).json()["data"]["issues"]["nodes"]]
    assert got == sorted(got)


def test_linear_root_connections_reject_sort_with_order_by(client, admin_h):
    """The refusal and where it falls among the other refusals, as `_reject_sort_with_order_by`
    records them."""
    rejected = {
        "issues": "{ issues(first: 2, sort: [], orderBy: createdAt) { nodes { identifier } } }",
        "users": "{ users(first: 2, sort: [{name: {order: Ascending}}], orderBy: createdAt) { nodes { name } } }",
    }
    for field, query in rejected.items():
        body = gql(client, query, admin_h).json()
        assert body["data"] is None
        assert body["errors"][0]["message"] == "Cannot use both sort and orderBy options"
        assert body["errors"][0]["path"] == [field]
        assert body["errors"][0]["extensions"] == {
            "type": "invalid input",
            "code": "INPUT_ERROR",
            "statusCode": 400,
            "userError": True,
        }

    sort_null = gql(
        client,
        "{ issues(first: 2, sort: null, orderBy: createdAt) { nodes { identifier } } }",
        admin_h,
    ).json()
    assert "errors" not in sort_null
    assert len(sort_null["data"]["issues"]["nodes"]) == 2

    order_by_null = gql(
        client,
        "{ users(first: 2, sort: [{name: {order: Ascending}}], orderBy: null) { nodes { name } } }",
        admin_h,
    ).json()
    assert "errors" not in order_by_null
    assert len(order_by_null["data"]["users"]["nodes"]) == 2

    # Each of these is a `rejected` query plus an argument that is refused without the pair as
    # well, and that refusal is the answer.
    for query, message in (
        (
            '{ issues(first: 2, sort: [], orderBy: createdAt, filter: {id: {eq: "not-a-uuid"}}) '
            "{ nodes { identifier } } }",
            "Argument Validation Error",
        ),
        (
            "{ users(first: 2, last: 2, sort: [{name: {order: Ascending}}], orderBy: createdAt) "
            "{ nodes { name } } }",
            "passing both `first` and `last` is not supported",
        ),
    ):
        assert gql(client, query, admin_h).json()["errors"][0]["message"] == message


# --- Linear relations / children / attachments / releases -----------------------


@pytest.fixture(scope="module")
def two_children(tmp_path_factory):
    """ENG-1 and its two children, ENG-2 created first and edited last, ENG-3 the reverse."""
    return build_corpus(
        tmp_path_factory.mktemp("two_children"),
        [
            {
                "source_type": "linear",
                "doc_id": "parent",
                "team": "engineering",
                "group": "engineering",
                "title": "Parent",
                "content": "p",
                "identifier": "ENG-1",
                "author_email": "ava@acme.com",
                "visibility": "public",
                "created": "2026-01-01T00:00:00Z",
            },
            {
                "source_type": "linear",
                "doc_id": "edited-last",
                "team": "engineering",
                "group": "engineering",
                "title": "Edited last",
                "content": "a",
                "identifier": "ENG-2",
                "author_email": "bob@acme.com",
                "visibility": "public",
                "parent": "ENG-1",
                "created": "2026-01-02T00:00:00Z",
                "updated": "2026-04-01T00:00:00Z",
            },
            {
                "source_type": "linear",
                "doc_id": "created-last",
                "team": "engineering",
                "group": "engineering",
                "title": "Created last",
                "content": "b",
                "identifier": "ENG-3",
                "author_email": "mia@acme.com",
                "visibility": "public",
                "parent": "ENG-1",
                "created": "2026-03-01T00:00:00Z",
                "updated": "2026-02-01T00:00:00Z",
            },
        ],
    )


@pytest.mark.parametrize(
    ("args", "served"),
    [
        ("", ["ENG-3", "ENG-2"]),
        ("(orderBy: createdAt)", ["ENG-3", "ENG-2"]),
        ("(orderBy: updatedAt)", ["ENG-2", "ENG-3"]),
        ("(first: 1)", ["ENG-3"]),
        ("(last: 1)", ["ENG-2"]),
    ],
)
def test_linear_children_are_newest_first(two_children, args, served):
    """Each row is an issue's `children` in the order the comment on `LINEAR_DEFAULT_ORDER_BY`
    records; `ENG-2` was created first and edited last, so the two fields disagree."""
    settings = two_children
    query = '{ issue(id: "ENG-1") { children%s { nodes { identifier } } } }' % args
    with client_for(settings) as client:
        body = gql(client, query, {"Authorization": settings.admin_token}).json()
    assert "errors" not in body, body.get("errors")
    assert [n["identifier"] for n in body["data"]["issue"]["children"]["nodes"]] == served


def test_linear_children_is_the_exact_inverse_of_parent(client, admin_h):
    """Linear DEFINES `children` as the inverse of `parent`, so the two must never disagree. They
    are both read off the `parent_id` resolved at import rather than joined on `identifier`,
    because bench keys repeat — a join would attach one issue's children to every issue sharing
    its key."""
    kids = gql(
        client, '{ issue(id: "ENG-103") { children { nodes { identifier } } } }', admin_h
    ).json()["data"]["issue"]["children"]["nodes"]
    assert [k["identifier"] for k in kids] == ["ENG-102"]
    back = gql(client, '{ issue(id: "ENG-102") { parent { identifier } } }', admin_h).json()[
        "data"
    ]["issue"]["parent"]
    assert back["identifier"] == "ENG-103"


def test_linear_children_is_acl_scoped(client, admin_h, tokens_yaml):
    """ENG-103 is restricted to hana, so ava cannot even reach it to ask for its children — and
    the children list must never become a way to observe an issue she is denied."""
    ava = {"Authorization": linear_user_token(tokens_yaml, "ava@acme.com")}
    denied = gql(client, '{ issue(id: "ENG-103") { children { nodes { identifier } } } }', ava)
    assert "Entity not found" in denied.json()["errors"][0]["message"]
    # Same denial via the OTHER spelling `issue(id:)` accepts: the served UUID. This exercises
    # `linear_by_id`'s own ACL clause rather than `linear_issue_by_identifier`'s (the
    # denial above never reaches the UUID lookup at all, since ava's query never named one).
    uuid = gql(client, '{ issue(id: "ENG-103") { id } }', admin_h).json()["data"]["issue"]["id"]
    denied_by_uuid = gql(client, "{ issue(id: %s) { identifier } }" % lit(uuid), ava)
    assert "Entity not found" in denied_by_uuid.json()["errors"][0]["message"]


def test_linear_relations_and_their_inverse(client, admin_h):
    rels = gql(
        client,
        '{ issue(id: "ENG-102") { relations { nodes { type relatedIssue { identifier } } } } }',
        admin_h,
    ).json()["data"]["issue"]["relations"]["nodes"]
    assert sorted((r["type"], r["relatedIssue"]["identifier"]) for r in rels) == [
        ("blocks", "ENG-101"),
        ("related", "ENG-103"),
    ]
    # the same row read from the other end
    inv = gql(
        client,
        '{ issue(id: "ENG-101") { inverseRelations { nodes { type issue { identifier } } } } }',
        admin_h,
    ).json()["data"]["issue"]
    assert [(r["type"], r["issue"]["identifier"]) for r in inv["inverseRelations"]["nodes"]] == [
        ("blocks", "ENG-102")
    ]


def test_linear_relation_to_a_hidden_issue_is_omitted(client, tokens_yaml, admin_h):
    """A relation is scoped on the FAR end: surfacing one whose counterpart the caller cannot read
    would disclose that issue's existence — the leak class the by-id roots were fixed for."""
    ava = {"Authorization": linear_user_token(tokens_yaml, "ava@acme.com")}
    q = '{ issue(id: "ENG-102") { relations { nodes { relatedIssue { identifier } } } } }'
    seen = [
        r["relatedIssue"]["identifier"]
        for r in gql(client, q, ava).json()["data"]["issue"]["relations"]["nodes"]
    ]
    assert seen == ["ENG-101"], "the relation to the restricted ENG-103 must be omitted"
    # admin sees both, proving the omission is the ACL and not a broken join
    assert len(gql(client, q, admin_h).json()["data"]["issue"]["relations"]["nodes"]) == 2


def test_linear_attachments_from_both_bench_shapes(client, admin_h):
    """`Attachment.title` is non-null in Linear, so a bare URL needs a derived title rather than
    an empty string."""
    nodes = gql(
        client, '{ issue(id: "ENG-102") { attachments { nodes { title url } } } }', admin_h
    ).json()["data"]["issue"]["attachments"]["nodes"]
    got = {n["title"]: n["url"] for n in nodes}
    assert got["Design doc"] == "https://conf.acme.test/design/batching"  # explicit title
    assert got["artifacts.zip"] == "https://ci.acme.test/builds/4821/artifacts.zip"  # derived


def test_linear_attachments_take_no_url_argument_and_filter_by_url_instead(client, admin_h):
    """Linear's `Issue.attachments` has no `url:` argument -- the real API answers
    `Unknown argument "url" on field "Issue.attachments"` (measured 2026-09-03). Backlot accepted one
    and called it Linear's own; a client narrows by url through the filter."""
    r = gql(
        client,
        '{ issue(id: "ENG-102") { attachments(url: "https://conf.acme.test/design/'
        'batching") { nodes { title } } } }',
        admin_h,
    )
    assert r.status_code == 400 and "data" not in r.json()
    assert r.json()["errors"][0]["message"] == (
        'Unknown argument "url" on field "Issue.attachments".'
    )
    one = gql(
        client,
        '{ issue(id: "ENG-102") { attachments(filter: {url: {eq: "https://conf.acme.test/design/'
        'batching"}}) { nodes { title } } } }',
        admin_h,
    )
    assert [n["title"] for n in one.json()["data"]["issue"]["attachments"]["nodes"]] == [
        "Design doc"
    ]
    none = gql(
        client,
        '{ issue(id: "ENG-102") { attachments(filter: {title: {eq: "nope"}}) '
        "{ nodes { title } } } }",
        admin_h,
    )
    assert none.json()["data"]["issue"]["attachments"]["nodes"] == []


def test_linear_releases_and_the_by_id_root(client, admin_h):
    nodes = gql(
        client, '{ issue(id: "ENG-102") { releases { nodes { id name slugId } } } }', admin_h
    ).json()["data"]["issue"]["releases"]["nodes"]
    assert [n["name"] for n in nodes] == ["runtime-1.19"]
    assert (
        gql(client, "{ release(id: %s) { name } }" % lit(nodes[0]["id"]), admin_h).json()["data"][
            "release"
        ]["name"]
        == "runtime-1.19"
    )


def test_linear_release_by_id_is_acl_scoped(client, tokens_yaml):
    """The release only appears on ENG-102, which ava CAN read — so she resolves it. Asserted to
    pin that the scoping is on visibility, not a blanket denial."""
    ava = {"Authorization": linear_user_token(tokens_yaml, "ava@acme.com")}
    got = gql(
        client, "{ release(id: %s) { name } }" % lit(synth.linear_release_id("runtime-1.19")), ava
    )
    assert got.json()["data"]["release"]["name"] == "runtime-1.19"
    absent = gql(
        client, "{ release(id: %s) { name } }" % lit(synth.linear_release_id("nope-9")), ava
    )
    assert "Entity not found" in absent.json()["errors"][0]["message"]


def test_linear_issue_with_no_relations_returns_empty_connections(client, admin_h):
    r = gql(
        client,
        '{ issue(id: "DES-77") { relations { nodes { id } } children { nodes { id } } '
        "attachments { nodes { id } } releases { nodes { id } } } }",
        admin_h,
    ).json()["data"]["issue"]
    assert all(r[k]["nodes"] == [] for k in ("relations", "children", "attachments", "releases"))


def test_linear_parent_and_children_read_the_same_column(client, admin_h, ro_conn):
    """Both directions must consult the resolved `parent_id`, not two independent lookups
    that happen to agree — that is the whole reason the key is resolved once at import.

    Also a performance contract: `@linear/sdk`'s Issue fragment selects `parent { id }` on every
    node, so resolving it by identifier cost ~45ms on a 50-issue page."""
    # `ro_conn` is the SAMPLE db; a fresh get_settings() would follow whatever BACKLOT_DATA_DIR
    # another module last set, which is why this reads the fixture instead.
    row = ro_conn.execute(
        "SELECT id, parent_id, parent_key FROM linear_issues WHERE id = ?",
        (served_id("linear", "lin-batch"),),
    ).fetchone()
    # the import pass resolved the KEY into the parent's own id
    assert row["parent_key"] == "ENG-103"
    assert row["parent_id"] == served_id("linear", "lin-secret")
    served = gql(client, '{ issue(id: "ENG-102") { parent { identifier } } }', admin_h).json()[
        "data"
    ]["issue"]["parent"]
    assert served["identifier"] == "ENG-103"


# --- the filter compiler (backlot/graphql/linear_filters.py) --------------------------------------

CORPUS = [
    {
        "source_type": "linear",
        "doc_id": "f1",
        "team": "engineering",
        "group": "engineering",
        "title": "Alpha gateway",
        "content": "token bucket refill",
        "identifier": "ENG-1",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "state": "In Progress",
        "priority": 1,
        "estimate": 1,
        "labels": ["bug", "gateway"],
        "project": "runtime",
        "cycle": "2026-W01",
        "dueDate": "2026-03-15",
        "assignee": "bob@acme.com",
        "assigneeName": "Bob Stone",
        "created": "2026-01-01T00:00:00Z",
        "comments": [{"content": "first note", "author_email": "bob@acme.com"}],
        # Two attachments with a `sourceType`, for the `SourceTypeComparator` operators. The
        # accented one is what `containsIgnoreCaseAndAccent` is about.
        "attachments": [
            {"url": "https://ci.acme.test/run/1", "title": "CI run", "sourceType": "github"},
            {
                "url": "https://acme.slack.com/archives/C1/p1",
                "title": "Réunion notes",
                "subtitle": "sub",
                "sourceType": "slack-équipe",
            },
            # Neither a subtitle nor a source, for the null rules.
            {"url": "https://acme.test/spec", "title": "Spec"},
        ],
    },
    {
        "source_type": "linear",
        "doc_id": "f2",
        "team": "engineering",
        "group": "engineering",
        "title": "Bravo 100% match_case",
        "content": "x",
        "identifier": "ENG-2",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "state": "Done",
        "priority": 2,
        "estimate": 5,
        "labels": ["bug"],
        "project": "runtime",
        "release": "runtime-1.19",
        "dueDate": "2026-04-01",
        "created": "2026-02-01T00:00:00Z",
        "comments": [{"content": "second note", "author_email": "ava@acme.com"}],
    },
    {
        "source_type": "linear",
        "doc_id": "f3",
        "team": "design",
        "group": "design",
        "title": "Charlie",
        "content": "y",
        "identifier": "DES-1",
        "author_email": "mia@acme.com",
        "author_groups": ["design"],
        "visibility": "public",
        "state": "Canceled",
        "priority": 4,
        "labels": [],
        "created": "2026-03-01T00:00:00Z",
    },
]


@pytest.fixture(scope="module")
def fclient(tmp_path_factory):
    settings = build_corpus(tmp_path_factory.mktemp("linear-filters"), CORPUS)
    with client_for(settings) as c:
        c.__dict__["_admin"] = settings.admin_token
        yield c


def ids(fclient, filter_literal, root="issues") -> list[str]:
    """Identifiers matching an IssueFilter, in a stable order."""
    q = "{ %s(first: 50, filter: %s) { nodes { identifier } } }" % (root, filter_literal)
    body = fclient.post(
        "/linear/graphql", json={"query": q}, headers={"Authorization": fclient.__dict__["_admin"]}
    ).json()
    assert "errors" not in body, body["errors"]
    return sorted(n["identifier"] for n in body["data"][root]["nodes"])


def err(fclient, filter_literal, root="issues") -> str:
    q = "{ %s(first: 50, filter: %s) { nodes { identifier } } }" % (root, filter_literal)
    body = fclient.post(
        "/linear/graphql", json={"query": q}, headers={"Authorization": fclient.__dict__["_admin"]}
    ).json()
    assert "errors" in body, f"expected an error, got {body}"
    return body["errors"][0]["message"]


def post(fclient, query: str, variables: dict | None = None):
    """The raw response, for the tests that assert on the status code and the envelope."""
    body: dict = {"query": query}
    if variables is not None:
        body["variables"] = variables
    return fclient.post(
        "/linear/graphql", json=body, headers={"Authorization": fclient.__dict__["_admin"]}
    )


ALL = ["DES-1", "ENG-1", "ENG-2"]


# --- numeric comparators: each pinned at its boundary -------------------------------


def test_number_comparators_are_pinned_at_their_boundary(fclient):
    assert ids(fclient, "{priority: {eq: 2}}") == ["ENG-2"]
    assert ids(fclient, "{priority: {neq: 2}}") == ["DES-1", "ENG-1"]
    assert ids(fclient, "{priority: {lt: 2}}") == ["ENG-1"]  # excludes 2
    assert ids(fclient, "{priority: {lte: 2}}") == ["ENG-1", "ENG-2"]  # includes 2
    assert ids(fclient, "{priority: {gt: 2}}") == ["DES-1"]  # excludes 2
    assert ids(fclient, "{priority: {gte: 2}}") == ["DES-1", "ENG-2"]  # includes 2
    assert ids(fclient, "{priority: {in: [1, 4]}}") == ["DES-1", "ENG-1"]
    assert ids(fclient, "{priority: {nin: [1, 4]}}") == ["ENG-2"]
    # `number` is the identifier's digits, the lookup mirage sends beside `team.key`.
    assert ids(fclient, "{number: {eq: 1}}") == ["DES-1", "ENG-1"]
    assert ids(fclient, "{number: {gt: 1}}") == ["ENG-2"]
    assert ids(fclient, '{team: {key: {eq: "ENG"}}, number: {eq: 2}}') == ["ENG-2"]
    assert ids(fclient, "{number: {lt: 1}}") == []
    assert ids(fclient, "{number: {eq: null}}") == []
    for spec in ("{eq: 0}", "{gt: 0}", "{lt: 1.5}", "{in: [1.5]}", "{nin: [0]}"):
        r = post(fclient, "{ issues(filter: {number: %s}) { nodes { identifier } } }" % spec)
        assert r.json()["errors"][0]["message"] == "Argument Validation Error", spec


def test_null_comparator_on_a_nullable_number(fclient):
    assert ids(fclient, "{estimate: {null: true}}") == ["DES-1"]
    assert ids(fclient, "{estimate: {null: false}}") == ["ENG-1", "ENG-2"]


def test_neq_drops_null_rows_and_nin_keeps_them_as_linear_does(fclient):
    """Measured 2026-09-03 over issues with a null estimate: `estimate: {neq: 99}` answers none of
    them and `estimate: {nin: [99]}` answers all of them. DES-1 has no estimate."""
    assert ids(fclient, "{estimate: {neq: 5}}") == ["ENG-1"]
    assert ids(fclient, "{estimate: {nin: [5]}}") == ["DES-1", "ENG-1"]
    assert ids(fclient, "{estimate: {neq: 5, null: true}}") == []


def test_negated_relation_filter_keeps_issues_without_the_relation(fclient):
    """Measured 2026-09-03: `project: {name: {neq: "zzz"}}`, `project: {name: {nin: ["zzz"]}}` and
    `assignee: {name: {neq: "zzz"}}` all answer the issues that have no project / assignee. DES-1
    has neither; ENG-2 has a project and no assignee."""
    assert ids(fclient, '{project: {name: {neq: "runtime"}}}') == ["DES-1"]
    assert ids(fclient, '{project: {name: {nin: ["runtime"]}}}') == ["DES-1"]
    assert ids(fclient, '{assignee: {name: {neq: "Bob Stone"}}}') == ["DES-1", "ENG-2"]
    assert ids(fclient, '{assignee: {email: {nin: ["bob@acme.com"]}}}') == ["DES-1", "ENG-2"]


def test_empty_in_list_matches_nothing_and_empty_nin_matches_everything(fclient):
    assert ids(fclient, "{priority: {in: []}}") == []
    assert ids(fclient, "{priority: {nin: []}}") == ALL


# --- the `id` comparator: a served UUID or the human identifier, not a plain column --------


def test_issue_filter_by_id_resolves_both_key_spaces_and_a_bogus_id_matches_nothing(fclient):
    """`IssueFilter.id` accepts the same two spellings `issue(id:)` does: a served UUID, or
    the human identifier. `_resolve_issue_ids` translates a filter's `id` values to issue ids
    before the query runs, so both must resolve -- and a well-shaped value that resolves to
    NEITHER must substitute the sentinel `"\\x00none"`, matching nothing, rather than being dropped
    (which would silently turn the filter into "match everything"). A value of neither shape is
    the vendor's `Argument Validation Error` instead; see the test below."""
    uuid = fclient.post(
        "/linear/graphql",
        json={"query": '{ issue(id: "ENG-1") { id } }'},
        headers={"Authorization": fclient.__dict__["_admin"]},
    ).json()["data"]["issue"]["id"]
    assert ids(fclient, '{id: {eq: "ENG-1"}}') == ["ENG-1"]  # identifier form
    assert ids(fclient, '{id: {eq: "%s"}}' % uuid) == ["ENG-1"]  # served UUID form
    assert ids(fclient, '{id: {eq: "ZZZ-404"}}') == []  # sentinel: matches nothing
    assert ids(fclient, '{id: {eq: "eng-1"}}') == ["ENG-1"]  # case-insensitive, as Linear's is
    assert ids(fclient, '{id: {in: ["%s", "ENG-2", "ZZZ-9"]}}' % uuid) == ["ENG-1", "ENG-2"]


# --- string comparators --------------------------------------------------------------


def test_string_comparators_are_distinct_from_one_another(fclient):
    assert ids(fclient, '{title: {eq: "Charlie"}}') == ["DES-1"]
    assert ids(fclient, '{title: {contains: "gateway"}}') == ["ENG-1"]
    assert ids(fclient, '{title: {startsWith: "Alpha"}}') == ["ENG-1"]
    assert ids(fclient, '{title: {endsWith: "gateway"}}') == ["ENG-1"]
    # startsWith must NOT behave like contains
    assert ids(fclient, '{title: {startsWith: "gateway"}}') == []
    assert ids(fclient, '{title: {containsIgnoreCase: "ALPHA"}}') == ["ENG-1"]
    assert ids(fclient, '{title: {eqIgnoreCase: "charlie"}}') == ["DES-1"]


def test_like_wildcards_in_the_needle_stay_literal(fclient):
    """`%` and `_` are SQL LIKE wildcards. Unescaped, `%` matches everything and `_` any single
    character, so a user-supplied needle would quietly widen the query."""
    assert ids(fclient, '{title: {contains: "100%"}}') == ["ENG-2"]  # literal %, not "match all"
    assert ids(fclient, '{title: {contains: "%"}}') == ["ENG-2"]
    assert ids(fclient, '{title: {contains: "match_case"}}') == ["ENG-2"]
    assert ids(fclient, '{title: {contains: "match-case"}}') == []  # `_` is not a wildcard


# --- dates ---------------------------------------------------------------------------


def test_date_comparators_coerce_iso8601_to_the_stored_epoch(fclient):
    """The column is unix seconds; without coercion every date filter compares a string to an
    integer and silently matches nothing (or everything)."""
    assert ids(fclient, '{createdAt: {gt: "2026-01-15T00:00:00Z"}}') == ["DES-1", "ENG-2"]
    assert ids(fclient, '{createdAt: {lt: "2026-01-15T00:00:00Z"}}') == ["ENG-1"]
    assert ids(fclient, '{createdAt: {gte: "2026-02-01T00:00:00Z"}}') == ["DES-1", "ENG-2"]


@pytest.mark.parametrize(
    "literal",
    ['"not-a-date"', '"1700000000"', '"20210305"', '"P"', '"2021-13-01"', '""'],
    ids=["text", "bare-epoch", "basic-format", "empty-duration", "month-13", "empty"],
)
def test_a_malformed_date_is_the_validation_error_linear_answers(fclient, literal):
    """Measured 2026-09-03: each of these is a 400 from api.linear.app, the error made by the
    `DateTimeOrDuration` scalar itself -- `Expected value of type "DateTimeOrDuration", found "P";
    Unable to parse value 'P' into a valid date` -- with no `data` key. A bare epoch is in the list
    on purpose: an earlier Backlot accepted one, which real Linear never did."""
    r = post(
        fclient, "{ issues(filter: {createdAt: {gt: %s}}) { nodes { identifier } } }" % literal
    )
    assert r.status_code == 400
    body = r.json()
    assert "data" not in body
    assert body["errors"][0]["message"] == (
        f'Expected value of type "DateTimeOrDuration", found {literal}; '
        f"Unable to parse value '{literal[1:-1]}' into a valid date"
    )


def test_a_date_operand_must_be_a_string(fclient):
    """Linear: `Unable to parse literal value of kind 'IntValue'. DateTimeOrDuration supports only
    'StringValue' ones` for a literal, and `... supports only string values` for a variable."""
    r = post(fclient, "{ issues(filter: {createdAt: {gt: 1700000000}}) { nodes { identifier } } }")
    assert r.status_code == 400
    assert "supports only 'StringValue' ones" in r.json()["errors"][0]["message"]
    q = "query($d: DateTimeOrDuration) { issues(filter: {createdAt: {gt: $d}}) { nodes { id } } }"
    r = post(fclient, q, {"d": 1700000000})
    assert r.status_code == 400
    assert r.json()["errors"][0]["message"].startswith('Variable "$d" got invalid value 1700000000')
    assert "supports only string values" in r.json()["errors"][0]["message"]
    r = post(fclient, q, {"d": "not-a-date"})
    assert r.status_code == 400
    assert 'Expected type "DateTimeOrDuration"' in r.json()["errors"][0]["message"]


@pytest.mark.parametrize(
    "literal",
    [
        '"2021-03-05T10"',
        '"PT"',
        '"20210305T100000"',
        '"2021-W10"',
        '"P9999Y"',
        '"-P3000Y"',
        '"P99999999999D"',
    ],
    ids=[
        "hour-only-time",
        "empty-time-duration",
        "basic-with-time",
        "iso-week",
        "years-past-9999",
        "years-before-1",
        "days-overflow",
    ],
)
def test_forms_outside_the_measured_grammar_are_refused(fclient, literal):
    """The first two are measured rejections (2026-09-03: `"2021-03-05T10"` and `"PT"` are 400s from
    api.linear.app). The basic and week forms follow from the measured rejection of `"20210305"`:
    `datetime.fromisoformat` would take them, so they are refused before it sees them. A duration
    that leaves the calendar is not measurable against a vendor that has no such date either; it
    answers the scalar's message rather than the interpreter's ("year must be in 1..9999")."""
    r = post(
        fclient, "{ issues(filter: {createdAt: {gt: %s}}) { nodes { identifier } } }" % literal
    )
    assert r.status_code == 400
    assert r.json()["errors"][0]["message"] == (
        f'Expected value of type "DateTimeOrDuration", found {literal}; '
        f"Unable to parse value '{literal[1:-1]}' into a valid date"
    )
    # The other scalar names itself `TimelessDate` and quotes the value (measured).
    r = post(fclient, "{ issues(filter: {dueDate: {eq: 1}}) { nodes { identifier } } }")
    assert r.status_code == 400
    assert r.json()["errors"][0]["message"].endswith(
        "Unable to parse literal value of kind 'IntValue'. TimelessDate supports only "
        "'StringValue' ones"
    )
    q = "query($d: TimelessDateOrDuration) { issues(filter: {dueDate: {eq: $d}}) { nodes { id } } }"
    r = post(fclient, q, {"d": 1})
    assert r.status_code == 400
    assert r.json()["errors"][0]["message"].endswith(
        "Unable to parse value '1'. TimelessDate supports only string values"
    )


# --- nested object filters -------------------------------------------------------------


def test_nested_filters_on_relations(fclient):
    assert ids(fclient, '{state: {name: {eq: "Done"}}}') == ["ENG-2"]
    assert ids(fclient, '{team: {key: {eq: "DES"}}}') == ["DES-1"]
    assert ids(fclient, '{project: {name: {eq: "runtime"}}}') == ["ENG-1", "ENG-2"]
    assert ids(fclient, '{assignee: {email: {eq: "bob@acme.com"}}}') == ["ENG-1"]
    assert ids(fclient, "{assignee: {null: true}}") == ["DES-1", "ENG-2"]
    assert ids(fclient, "{project: {null: true}}") == ["DES-1"]


def test_derived_state_type_expands_to_the_matching_names(fclient):
    """`state.type` has no column — it is a pure function of the name — so it compiles to an IN
    over the names that satisfy the predicate. A derivation that matched everything would make
    this filter a silent no-op."""
    assert ids(fclient, '{state: {type: {eq: "completed"}}}') == ["ENG-2"]
    assert ids(fclient, '{state: {type: {eq: "canceled"}}}') == ["DES-1"]
    assert ids(fclient, '{state: {type: {eq: "started"}}}') == ["ENG-1"]
    assert ids(fclient, '{state: {type: {in: ["completed", "canceled"]}}}') == ["DES-1", "ENG-2"]


def test_derived_team_key_is_not_the_team_name(fclient):
    assert ids(fclient, '{team: {key: {eq: "ENG"}}}') == ["ENG-1", "ENG-2"]
    assert ids(fclient, '{team: {name: {eq: "engineering"}}}') == ["ENG-1", "ENG-2"]
    assert ids(fclient, '{team: {key: {eq: "engineering"}}}') == []  # key != name


def test_negated_derived_filter_keeps_null_column_rows(fclient):
    """A row with no project cannot BE the excluded project. The column comparator's `neq` says
    so explicitly, and the derived IN-list form has to agree with it."""
    by_name = ids(fclient, '{project: {name: {neq: "runtime"}}}')
    by_id = ids(
        fclient,
        '{project: {id: {neq: "%s"}}}'
        % __import__("backlot.synth", fromlist=["x"]).linear_project_id("runtime"),
    )
    assert by_name == by_id == ["DES-1"]


# --- labels (the JSON column) ------------------------------------------------------------


def test_labels_some_and_every(fclient):
    assert ids(fclient, '{labels: {some: {name: {eq: "gateway"}}}}') == ["ENG-1"]
    assert ids(fclient, '{labels: {some: {name: {eq: "bug"}}}}') == ["ENG-1", "ENG-2"]
    # `every` over a positive predicate needs at least one label on Linear (measured 2026-09-03:
    # `every: {name: {eq: A}}` answered the `[a]` issue alone, not the label-less ones), so DES-1,
    # which has no labels, is out. The full rule is `test_labels_quantifiers_answer_as_linear`.
    assert ids(fclient, '{labels: {every: {name: {eq: "bug"}}}}') == ["ENG-2"]


def test_labels_some_is_not_every(fclient):
    """ENG-1 has bug AND gateway, so `every: bug` must exclude it while `some: bug` includes it."""
    assert "ENG-1" in ids(fclient, '{labels: {some: {name: {eq: "bug"}}}}')
    assert "ENG-1" not in ids(fclient, '{labels: {every: {name: {eq: "bug"}}}}')


def test_nested_and_or_inside_a_labels_filter_is_applied(fclient):
    """An inner and/or that compiled to nothing dropped the WHOLE filter, so a query narrowing to
    a nonexistent label returned the entire corpus."""
    assert ids(fclient, '{labels: {some: {and: [{name: {eq: "nonexistent"}}]}}}') == []
    assert ids(fclient, '{labels: {some: {or: [{name: {eq: "gateway"}}]}}}') == ["ENG-1"]
    assert (
        ids(fclient, '{labels: {some: {and: [{name: {eq: "bug"}}, {name: {eq: "gateway"}}]}}}')
        == []
    )  # one label can't be both
    assert ids(
        fclient, '{labels: {some: {or: [{name: {eq: "bug"}}, {name: {eq: "gateway"}}]}}}'
    ) == ["ENG-1", "ENG-2"]


# --- boolean composition ------------------------------------------------------------------


def test_top_level_and_or_are_not_interchangeable(fclient):
    both = '{and: [{team: {key: {eq: "ENG"}}}, {priority: {eq: 1}}]}'
    either = '{or: [{team: {key: {eq: "ENG"}}}, {priority: {eq: 4}}]}'
    assert ids(fclient, both) == ["ENG-1"]
    assert ids(fclient, either) == ["DES-1", "ENG-1", "ENG-2"]


def test_sibling_keys_are_anded(fclient):
    assert ids(fclient, '{team: {key: {eq: "ENG"}}, priority: {eq: 4}}') == []


def test_or_mixing_a_derived_and_a_column_branch(fclient):
    assert ids(fclient, '{or: [{state: {type: {eq: "canceled"}}}, {priority: {eq: 1}}]}') == [
        "DES-1",
        "ENG-1",
    ]


# --- "declared means implemented" ------------------------------------------------------


def test_an_unsupported_filter_field_is_an_error_not_a_dropped_filter(fclient):
    """The guarantee the module exists to provide: never answer a narrowing query with the full
    set. graphql-core rejects a field the SDL doesn't declare; the compiler rejects one it
    declares but cannot evaluate."""
    q = '{ issues(filter: {nope: {eq: "x"}}) { nodes { identifier } } }'
    r = fclient.post(
        "/linear/graphql", json={"query": q}, headers={"Authorization": fclient.__dict__["_admin"]}
    )
    assert r.status_code == 400
    assert r.json()["errors"][0]["message"] == 'Field "nope" is not defined by type "IssueFilter".'


def test_an_unsupported_comparator_is_an_error(fclient):
    q = '{ issues(filter: {title: {nope: "x"}}) { nodes { identifier } } }'
    r = fclient.post(
        "/linear/graphql", json={"query": q}, headers={"Authorization": fclient.__dict__["_admin"]}
    )
    assert r.status_code == 400


def test_comment_filter_narrows(fclient):
    q = '{ comments(first: 50, filter: {body: {contains: "second"}}) { nodes { body } } }'
    body = fclient.post(
        "/linear/graphql", json={"query": q}, headers={"Authorization": fclient.__dict__["_admin"]}
    ).json()
    assert [n["body"] for n in body["data"]["comments"]["nodes"]] == ["second note"]


def test_comment_filter_by_the_served_id_round_trips(fclient):
    """`Comment.id` is served as a synthesized UUID, so a filter written from one has to be
    translated back to the stored row id or it can never match what the client just read."""
    listed = fclient.post(
        "/linear/graphql",
        json={"query": "{ comments(first: 1) { nodes { id body } } }"},
        headers={"Authorization": fclient.__dict__["_admin"]},
    ).json()
    first = listed["data"]["comments"]["nodes"][0]
    q = '{ comments(first: 50, filter: {id: {eq: "%s"}}) { nodes { body } } }' % first["id"]
    got = fclient.post(
        "/linear/graphql", json={"query": q}, headers={"Authorization": fclient.__dict__["_admin"]}
    ).json()
    assert [n["body"] for n in got["data"]["comments"]["nodes"]] == [first["body"]]


def comment_bodies(fclient, filter_literal, root="comments") -> list[str]:
    """Bodies matching a CommentFilter, in a stable order. ``root`` is the `comments` root or
    `issue(id: …) { comments … }`; the literal may name the served id of ``second note`` as ``%(B)s``."""
    listed = post(fclient, "{ comments(first: 50) { nodes { id body } } }").json()
    served = {n["body"]: n["id"] for n in listed["data"]["comments"]["nodes"]}
    literal = filter_literal % {"B": served["second note"]}
    if root == "comments":
        q = "{ comments(first: 50, filter: %s) { nodes { body } } }" % literal
        path = ("comments",)
    else:
        q = '{ issue(id: "%s") { comments(filter: %s) { nodes { body } } } }' % (root, literal)
        path = ("issue", "comments")
    body = post(fclient, q).json()
    assert "errors" not in body, body["errors"]
    node = body["data"]
    for step in path:
        node = node[step]
    return sorted(n["body"] for n in node["nodes"])


FIRST, SECOND = "first note", "second note"
BOTH = [FIRST, SECOND]
# Every cell is one answer api.linear.app gave over two throwaway comments (`zz-c1` on BRE-1, `zz-c2`
# on BRE-2) in a workspace holding no others, on 2026-09-06 except the `and` row, measured 2026-09-04;
# A is the first comment's body and B the second's id. The fixture's two comments, `first note` on ENG-1 and `second note` on ENG-2, stand in,
# and share the substring `note` where the workspace pair shared `zz-c`.
_COMMENT_OR_CELLS = [
    # the keys of one `or` branch are alternatives; outside an `or`, and under `and`, they AND
    ('{or: [{body: {eq: "first note"}, id: {eq: "%(B)s"}}]}', BOTH),
    ('{body: {eq: "first note"}, id: {eq: "%(B)s"}}', []),
    ('{and: [{body: {eq: "first note"}, id: {eq: "%(B)s"}}]}', []),
    ('{or: [{body: {eq: "first note"}}, {id: {eq: "%(B)s"}}]}', BOTH),
    ('{or: [{body: {contains: "note"}, id: {eq: "%(B)s"}}], body: {eq: "second note"}}', [SECOND]),
    ('{or: [{or: [{body: {eq: "first note"}, id: {eq: "%(B)s"}}]}]}', BOTH),
    ('{and: [{or: [{body: {eq: "first note"}, id: {eq: "%(B)s"}}]}]}', BOTH),
    # a branch with nothing in it is dropped
    ('{or: [{body: {eq: "first note"}}, {}]}', [FIRST]),
    ('{or: [{body: {eq: "first note"}, id: {eq: "%(B)s"}}, {}]}', BOTH),
    ('{or: [{body: {eq: "first note"}, id: null}]}', [FIRST]),
    ('{or: [{body: null}, {id: {eq: "%(B)s"}}]}', [SECOND]),
    ('{or: [{and: []}, {id: {eq: "%(B)s"}}]}', [SECOND]),
    ('{or: [{or: []}, {id: {eq: "%(B)s"}}]}', [SECOND]),
    ('{or: [{}], id: {eq: "%(B)s"}}', [SECOND]),
    ('{and: [{}], id: {eq: "%(B)s"}}', [SECOND]),
    # ... but an `and` / `or` whose every branch is dropped constrains nothing, and so makes an
    # enclosing `or` constrain nothing, while a key beside it and an enclosing `and` still apply
    ('{or: [{or: [{}]}, {id: {eq: "%(B)s"}}]}', BOTH),
    ('{or: [{and: [{}]}, {id: {eq: "%(B)s"}}]}', BOTH),
    ('{or: [{or: [{}]}], id: {eq: "%(B)s"}}', [SECOND]),
    ('{or: [{and: [{}]}], id: {eq: "%(B)s"}}', [SECOND]),
    ('{and: [{or: [{}]}, {id: {eq: "%(B)s"}}]}', [SECOND]),
    # a branch whose key constrains nothing makes the whole `or` constrain nothing; a key beside
    # the `or` still applies, and under `and` such a branch is dropped
    ("{body: {}}", BOTH),
    ("{or: [{body: {}}]}", BOTH),
    ('{or: [{body: {}}, {id: {eq: "%(B)s"}}]}', BOTH),
    ('{or: [{body: {}, id: {eq: "%(B)s"}}]}', BOTH),
    ('{or: [{createdAt: {}}, {id: {eq: "%(B)s"}}]}', BOTH),
    ('{or: [{id: {}}, {body: {eq: "first note"}}]}', BOTH),
    ('{or: [{or: [{body: {}}]}, {id: {eq: "%(B)s"}}]}', BOTH),
    ('{or: [{body: {}}], id: {eq: "%(B)s"}}', [SECOND]),
    ('{or: [{body: {}, id: {eq: "%(B)s"}}], body: {eq: "second note"}}', [SECOND]),
    ('{and: [{body: {}}, {id: {eq: "%(B)s"}}]}', [SECOND]),
    ('{or: [{and: [{body: {}}, {body: {eq: "nosuch"}}]}, {id: {eq: "%(B)s"}}]}', [SECOND]),
]


def test_comment_filter_or_reads_a_branch_as_linear(fclient):
    """Linear's `or` on `CommentFilter` is the `or` `_issue_parts` compiles: the keys of one branch
    are alternatives, a branch with nothing in it is dropped, and a branch whose key constrains
    nothing makes the whole `or` constrain nothing. Compiling a branch as one conjunction, as this
    router used to, answered `{or: [{body: {eq: A}, id: {eq: B}}]}` with no comment where Linear
    answers two, and nothing in the well-formed empty list said so."""
    assert {literal: comment_bodies(fclient, literal) for literal, _ in _COMMENT_OR_CELLS} == dict(
        _COMMENT_OR_CELLS
    )


def test_issue_comments_connection_reads_an_or_branch_the_same_way(fclient):
    """`Issue.comments(filter:)` goes through the same compiler, scoped to the issue: on the real
    API the two-key branch answered the one comment on BRE-1 when asked under `issue(id: BRE-1)`."""
    two_keys = '{or: [{body: {eq: "first note"}, id: {eq: "%(B)s"}}]}'
    assert comment_bodies(fclient, two_keys, root="ENG-1") == [FIRST]
    assert comment_bodies(fclient, two_keys, root="ENG-2") == [SECOND]


def matched_nodes(fclient, query: str, path: tuple[str, ...], field: str) -> list[str]:
    """``field`` of every node at ``path`` (ending at a connection) in the answer to ``query``."""
    body = post(fclient, query).json()
    assert "errors" not in body, body["errors"]
    node = body["data"]
    for step in path:
        node = node[step]
    return sorted(n[field] for n in node["nodes"])


# The routes `_match_fields` evaluates in linear_resolvers.py, each as api.linear.app answered it on
# 2026-09-07 (users: three in the workspace, A the first's name and B the second's email; teams:
# one, `BRE`; labels: BRE-1 carrying `[Bug, Feature]`, `Improvement` nobody's; attachments: two
# throwaway ones on BRE-1, created for the run and deleted after). The fixture stands in: users
# Ava / bob@acme.com / Mia, teams DES and ENG, ENG-1's labels `[bug, gateway]` (`gateway` for
# `Feature`, `nope` for `Improvement`) and ENG-1's three attachments (`CI run` for the first
# throwaway, the Slack url for the second's). Every route reads `or` as `_issue_parts` does: the keys
# of one branch are alternatives, a branch with nothing in it is dropped, and a branch whose key
# constrains nothing makes the whole `or` constrain nothing.
_USERS = ["ava@acme.com", "bob@acme.com", "mia@acme.com"]
_USER_OR_CELLS = [
    ('{or: [{name: {eq: "Ava"}, email: {eq: "bob@acme.com"}}]}', _USERS[:2]),
    ('{name: {eq: "Ava"}, email: {eq: "bob@acme.com"}}', []),
    ('{and: [{name: {eq: "Ava"}, email: {eq: "bob@acme.com"}}]}', []),
    ('{or: [{name: {eq: "Ava"}}, {email: {eq: "bob@acme.com"}}]}', _USERS[:2]),
    (
        '{or: [{name: {eq: "Ava"}, email: {eq: "bob@acme.com"}}], email: {eq: "bob@acme.com"}}',
        [_USERS[1]],
    ),
    ('{or: [{}, {email: {eq: "bob@acme.com"}}]}', [_USERS[1]]),
    ('{or: [{name: null}, {email: {eq: "bob@acme.com"}}]}', [_USERS[1]]),
    ('{or: [{name: {}}, {email: {eq: "bob@acme.com"}}]}', _USERS),
    ('{or: [{name: {}, email: {eq: "bob@acme.com"}}]}', _USERS),
    ("{or: [{}]}", _USERS),
    ("{or: []}", _USERS),
    ("{and: []}", _USERS),
    ("{and: [{}]}", _USERS),
    ('{or: [{or: [{}]}, {email: {eq: "bob@acme.com"}}]}', _USERS),
    ('{or: [{and: []}, {email: {eq: "bob@acme.com"}}]}', [_USERS[1]]),
    ('{and: [{name: {}}, {email: {eq: "bob@acme.com"}}]}', [_USERS[1]]),
]
_TEAMS = ["DES", "ENG"]
_TEAM_OR_CELLS = [
    ('{or: [{name: {eq: "nosuch"}, key: {eq: "ENG"}}]}', ["ENG"]),
    ('{name: {eq: "nosuch"}, key: {eq: "ENG"}}', []),
    ('{or: [{name: {eq: "nosuch"}}, {key: {eq: "ENG"}}]}', ["ENG"]),
    ('{or: [{name: {eq: "engineering"}, key: {eq: "ZZZ"}}]}', ["ENG"]),
    ('{or: [{}, {key: {eq: "nosuch"}}]}', []),
    ('{or: [{name: {}}, {key: {eq: "nosuch"}}]}', _TEAMS),
    ("{or: [{}]}", _TEAMS),
    ("{or: []}", _TEAMS),
]
_ENG1_LABELS = ["bug", "gateway"]
_ISSUE_LABELS_OR_CELLS = [
    ('{or: [{name: {eq: "gateway"}, and: [{name: {eq: "nope"}}]}]}', ["gateway"]),
    ('{or: [{name: {eq: "nope"}, and: [{name: {eq: "gateway"}}]}]}', ["gateway"]),
    ('{and: [{name: {eq: "gateway"}, or: [{name: {eq: "nope"}}]}]}', []),
    ('{name: {eq: "gateway"}, and: [{name: {eq: "nope"}}]}', []),
    ('{or: [{name: {eq: "gateway"}}, {and: [{name: {eq: "nope"}}]}]}', ["gateway"]),
    ('{or: [{}, {name: {eq: "gateway"}}]}', ["gateway"]),
    ('{or: [{name: null}, {name: {eq: "gateway"}}]}', ["gateway"]),
    ('{or: [{name: {}}, {name: {eq: "gateway"}}]}', _ENG1_LABELS),
    ('{or: [{name: {}, and: [{name: {eq: "nope"}}]}]}', _ENG1_LABELS),
    ("{or: [{}]}", _ENG1_LABELS),
    ("{or: []}", _ENG1_LABELS),
    ("{and: [{}]}", _ENG1_LABELS),
    ('{or: [{or: [{}]}, {name: {eq: "gateway"}}]}', _ENG1_LABELS),
    ('{or: [{and: []}, {name: {eq: "gateway"}}]}', ["gateway"]),
    ('{or: [{name: {eq: "gateway"}, and: []}]}', ["gateway"]),
    ('{or: [{name: {eq: "gateway"}, and: [{}]}]}', _ENG1_LABELS),
    ('{and: [{name: {}}, {name: {eq: "gateway"}}]}', ["gateway"]),
    ("{name: {}}", _ENG1_LABELS),
    ("{}", _ENG1_LABELS),
]
_ENG1_ATTACHMENTS = ["CI run", "Réunion notes", "Spec"]
_SLACK = "https://acme.slack.com/archives/C1/p1"
_ATTACHMENT_OR_CELLS = [
    ('{or: [{title: {eq: "CI run"}, url: {eq: "%s"}}]}' % _SLACK, _ENG1_ATTACHMENTS[:2]),
    ('{title: {eq: "CI run"}, url: {eq: "%s"}}' % _SLACK, []),
    ('{and: [{title: {eq: "CI run"}, url: {eq: "%s"}}]}' % _SLACK, []),
    ('{or: [{title: {eq: "CI run"}}, {url: {eq: "%s"}}]}' % _SLACK, _ENG1_ATTACHMENTS[:2]),
    (
        '{or: [{title: {eq: "CI run"}, url: {eq: "%s"}}], url: {eq: "%s"}}' % (_SLACK, _SLACK),
        ["Réunion notes"],
    ),
    ('{or: [{}, {title: {eq: "CI run"}}]}', ["CI run"]),
    ('{or: [{title: null}, {title: {eq: "CI run"}}]}', ["CI run"]),
    ('{or: [{title: {}}, {title: {eq: "CI run"}}]}', _ENG1_ATTACHMENTS),
    ('{or: [{title: {}, url: {eq: "%s"}}]}' % _SLACK, _ENG1_ATTACHMENTS),
    ("{or: [{}]}", _ENG1_ATTACHMENTS),
    ("{or: []}", _ENG1_ATTACHMENTS),
    ("{and: [{}]}", _ENG1_ATTACHMENTS),
    ('{or: [{or: [{}]}, {title: {eq: "CI run"}}]}', _ENG1_ATTACHMENTS),
    ('{or: [{and: []}, {title: {eq: "CI run"}}]}', ["CI run"]),
    ('{and: [{title: {}}, {title: {eq: "CI run"}}]}', ["CI run"]),
]


@pytest.mark.parametrize("literal,expected", _USER_OR_CELLS, ids=[c[0] for c in _USER_OR_CELLS])
def test_users_filter_or_reads_a_branch_as_linear(fclient, literal, expected):
    """`Query.users(filter:)` is evaluated by `_match_fields`, which used to AND the keys of an `or`
    branch: `{or: [{name: {eq: A}, email: {eq: B}}]}` answered no user where Linear answers two."""
    q = "{ users(first: 50, filter: %s) { nodes { email } } }" % literal
    assert matched_nodes(fclient, q, ("users",), "email") == expected


@pytest.mark.parametrize("literal,expected", _TEAM_OR_CELLS, ids=[c[0] for c in _TEAM_OR_CELLS])
def test_teams_filter_or_reads_a_branch_as_linear(fclient, literal, expected):
    """`Query.teams(filter:)` goes through `_match_fields` too, where `TeamFilter` under
    `IssueFilter.team` goes through `_sub_filter`; the two read an `or` branch the same way now."""
    q = "{ teams(first: 50, filter: %s) { nodes { key } } }" % literal
    assert matched_nodes(fclient, q, ("teams",), "key") == expected


@pytest.mark.parametrize(
    "literal,expected", _ISSUE_LABELS_OR_CELLS, ids=[c[0] for c in _ISSUE_LABELS_OR_CELLS]
)
def test_issue_labels_filter_or_reads_a_branch_as_linear(fclient, literal, expected):
    """`Issue.labels(filter:)` is the second evaluator of `IssueLabelFilter`: the same object that
    `labels: {some: …}` compiles through `_label_predicate` is evaluated here by `_match_fields`, and
    the two answered differently on an `or` branch with two keys until both split it."""
    q = '{ issue(id: "ENG-1") { labels(filter: %s) { nodes { name } } } }' % literal
    assert matched_nodes(fclient, q, ("issue", "labels"), "name") == expected


@pytest.mark.parametrize(
    "literal,expected", _ATTACHMENT_OR_CELLS, ids=[c[0] for c in _ATTACHMENT_OR_CELLS]
)
def test_issue_attachments_filter_or_reads_a_branch_as_linear(fclient, literal, expected):
    """`Issue.attachments(filter:)`, the fourth `_match_fields` route, measured over two throwaway
    attachments on BRE-1."""
    q = '{ issue(id: "ENG-1") { attachments(filter: %s) { nodes { title } } } }' % literal
    assert matched_nodes(fclient, q, ("issue", "attachments"), "title") == expected


def test_an_empty_labels_predicate_constrains_nothing_as_it_does_on_linear(fclient):
    """`labels: {}`, `labels: {some: {}}`, `labels: {every: {}}` and `labels: {some: {name: {}}}` each
    answered every issue on api.linear.app (measured 2026-09-03), the label-less ones included: an
    empty predicate is no predicate there, so it compiles to nothing here."""
    everything = ids(fclient, "{}")
    for literal in (
        "{labels: {}}",
        "{labels: {some: {}}}",
        "{labels: {every: {}}}",
        "{labels: {some: {name: {}}}}",
    ):
        assert ids(fclient, literal) == everything, literal


# The labels filter, cell by cell, as api.linear.app answered it over two labelled issues --
# `[zz-a, zz-zzz]` and `[zz-a]`, created for the measurement and deleted after -- beside four with
# no labels. The fixture has the same shape: ENG-1 carries `[bug, gateway]`, ENG-2 `[bug]`, DES-1
# nothing; `bug` stands for `zz-a`, `gateway` for `zz-zzz`, `nope` for a label nobody has.
L2, L1, U = "ENG-1", "ENG-2", "DES-1"
EVERY = {L2, L1, U}
_LABEL_CELLS = [
    # some, positive predicate: EXISTS -- a label-less issue never matches
    ('{some: {name: {eq: "bug"}}}', {L2, L1}),
    ('{some: {name: {eq: "gateway"}}}', {L2}),
    ('{some: {name: {eq: "nope"}}}', set()),
    ('{some: {name: {in: ["bug", "nope"]}}}', {L2, L1}),
    ('{some: {name: {in: ["bug", "gateway"]}}}', {L2, L1}),
    ('{some: {name: {contains: "g"}}}', {L2, L1}),
    ('{some: {name: {contains: ""}}}', {L2, L1}),
    ('{some: {name: {eqIgnoreCase: "BUG"}}}', {L2, L1}),
    ('{some: {or: [{name: {eq: "gateway"}}, {name: {eq: "nope"}}]}}', {L2}),
    # some, negative predicate: no labels, OR a label satisfies it (`[a]` is the one left out)
    ('{some: {name: {neq: "bug"}}}', {L2, U}),
    ('{some: {name: {neq: "gateway"}}}', {L2, L1, U}),
    ('{some: {name: {neq: "nope"}}}', {L2, L1, U}),
    ('{some: {name: {nin: ["bug"]}}}', {L2, U}),
    ('{some: {name: {nin: ["gateway"]}}}', {L2, L1, U}),
    ('{some: {name: {nin: ["nope"]}}}', {L2, L1, U}),
    ('{some: {name: {neqIgnoreCase: "BUG"}}}', {L2, U}),
    ('{some: {and: [{name: {neq: "bug"}}, {name: {neq: "nope"}}]}}', {L2, U}),
    # polarity of a compound: `and` is negative only when every branch is, `or` when any is
    ('{some: {and: [{name: {eq: "bug"}}, {name: {neq: "nope"}}]}}', {L2, L1}),
    ('{some: {and: [{name: {eq: "bug"}}, {name: {neq: "gateway"}}]}}', {L2, L1}),
    ('{some: {name: {eq: "bug", neq: "nope"}}}', {L2, L1}),
    ('{some: {or: [{name: {neq: "bug"}}, {name: {eq: "nope"}}]}}', {L2, U}),
    ('{some: {or: [{name: {eq: "gateway"}}, {name: {neq: "bug"}}]}}', {L2, U}),
    # every, positive predicate: at least one label, and all of them satisfy it
    ('{every: {name: {eq: "bug"}}}', {L1}),
    ('{every: {name: {eq: "gateway"}}}', set()),
    ('{every: {name: {eq: "nope"}}}', set()),
    ('{every: {name: {in: ["bug", "nope"]}}}', {L1}),
    ('{every: {name: {in: ["bug", "gateway"]}}}', {L2, L1}),
    ('{every: {name: {contains: "g"}}}', {L2, L1}),
    ('{every: {name: {contains: ""}}}', {L2, L1}),
    ('{every: {or: [{name: {eq: "gateway"}}, {name: {eq: "nope"}}]}}', set()),
    ('{every: {and: [{name: {eq: "bug"}}, {name: {neq: "nope"}}]}}', {L1}),
    # every, negative predicate: no label fails it, and no labels at all qualifies
    ('{every: {name: {neq: "bug"}}}', {U}),
    ('{every: {name: {neq: "gateway"}}}', {L1, U}),
    ('{every: {name: {neq: "nope"}}}', {L2, L1, U}),
    ('{every: {name: {nin: ["bug"]}}}', {U}),
    ('{every: {name: {nin: ["gateway"]}}}', {L1, U}),
    ('{every: {name: {nin: ["nope"]}}}', {L2, L1, U}),
    ('{every: {name: {neqIgnoreCase: "BUG"}}}', {U}),
    ('{every: {and: [{name: {neq: "bug"}}, {name: {neq: "nope"}}]}}', {U}),
    ('{every: {or: [{name: {eq: "gateway"}}, {name: {neq: "bug"}}]}}', {U}),
    # the collection-level fields
    ("{length: {eq: 0}}", {U}),
    ("{length: {eq: 1}}", {L1}),
    ("{length: {eq: 2}}", {L2}),
    ("{length: {gt: 0}}", {L2, L1}),
    ("{length: {lt: 2}}", {L1, U}),
    ("{length: {neq: 0}}", {L2, L1}),
    ("{length: {in: [0, 2]}}", {L2, U}),
    ("{length: {nin: [0]}}", {L2, L1}),
    ("{length: {gte: 1, lte: 1}}", {L1}),
    ('{or: [{length: {eq: 0}}, {every: {name: {eq: "bug"}}}]}', {L1, U}),
    ('{some: {name: {eq: "bug"}}, every: {name: {eq: "bug"}}}', {L1}),
    ('{name: {eq: "bug"}}', {L2, L1}),
    ("{null: true}", set()),
    ("{null: false}", {L2, L1, U}),
    ("{null: null}", {L2, L1, U}),
    ('{name: {neq: "bug"}}', {L2, U}),
    ('{name: {nin: ["bug"]}}', {L2, U}),
    ('{name: {eq: "bug", neq: "nope"}}', {L2, L1}),
    ('{and: [{some: {name: {eq: "bug"}}}, {length: {eq: 2}}]}', {L2}),
    ("{and: []}", {L2, L1, U}),
    ("{or: []}", {L2, L1, U}),
    ("{or: [{}]}", {L2, L1, U}),
    # a predicate that mixes a comparator with a nested and / or is one AND, polarity included
    ('{some: {name: {neq: "bug"}, and: [{name: {eq: "gateway"}}]}}', {L2}),
    ('{every: {name: {neq: "nope"}, or: [{name: {eq: "bug"}}]}}', {L1}),
    # empty lists: an empty `in` matches no label, an empty `nin` every label
    ("{length: {in: []}}", set()),
    ("{length: {nin: []}}", {L2, L1, U}),
    ("{some: {name: {in: []}}}", set()),
    ("{some: {name: {nin: []}}}", {L2, L1, U}),
    ("{every: {name: {in: []}}}", set()),
    ("{every: {name: {nin: []}}}", {L2, L1, U}),
    # an empty `and` is no predicate; a literally empty `or` is one every label satisfies, so the
    # quantifier still applies and asks for at least one label
    ("{some: {and: []}}", {L2, L1, U}),
    ("{every: {and: []}}", {L2, L1, U}),
    ("{some: {or: []}}", {L2, L1}),
    ("{every: {or: []}}", {L2, L1}),
    ("{some: {or: [{}]}}", {L2, L1, U}),
    ('{some: {or: [], name: {neq: "bug"}}}', {L2}),
    # the keys of the collection filter do not AND: `and`, else `or`, else the first of `length`,
    # `every`, `some`, `name`, whatever the order; `null` is the one that ANDs, and only with those
    ('{some: {name: {eq: "gateway"}}, every: {name: {eq: "bug"}}}', {L1}),
    ('{every: {name: {eq: "bug"}}, some: {name: {eq: "gateway"}}}', {L1}),
    ('{some: {name: {neq: "bug"}}, every: {name: {eq: "bug"}}}', {L1}),
    ('{length: {eq: 1}, some: {name: {eq: "gateway"}}}', {L1}),
    ('{some: {name: {eq: "gateway"}}, length: {eq: 1}}', {L1}),
    ('{length: {eq: 2}, every: {name: {eq: "bug"}}}', {L2}),
    ('{every: {name: {eq: "bug"}}, length: {eq: 2}}', {L2}),
    ('{name: {eq: "gateway"}, some: {name: {eq: "bug"}}}', {L2, L1}),
    ('{some: {name: {eq: "bug"}}, name: {eq: "gateway"}}', {L2, L1}),
    ('{every: {name: {eq: "bug"}}, name: {eq: "gateway"}}', {L1}),
    ('{name: {eq: "gateway"}, every: {name: {eq: "bug"}}}', {L1}),
    ('{length: {eq: 1}, name: {eq: "gateway"}}', {L1}),
    ('{name: {eq: "gateway"}, length: {eq: 1}}', {L1}),
    ('{null: false, some: {name: {eq: "gateway"}}}', {L2}),
    ('{null: true, some: {name: {eq: "gateway"}}}', set()),
    ("{length: {eq: 1}, null: false}", {L1}),
    ("{null: true, length: {eq: 0}}", set()),
    ("{null: null, length: {eq: 0}}", {U}),
    ("{null: false, length: {eq: 99}}", set()),
    ('{or: [{length: {eq: 1}}], some: {name: {eq: "gateway"}}}', {L1}),
    ('{some: {name: {eq: "gateway"}}, or: [{length: {eq: 1}}]}', {L1}),
    ('{and: [{length: {eq: 2}}], every: {name: {eq: "bug"}}}', {L2}),
    ("{or: [{length: {eq: 0}}], length: {eq: 2}}", {U}),
    ("{length: {eq: 2}, or: [{length: {eq: 0}}]}", {U}),
    ("{or: [{length: {eq: 0}}], null: true}", {U}),
    ("{and: [{length: {eq: 2}}], null: true}", {L2}),
    ("{and: [{length: {eq: 2}}], or: [{length: {eq: 1}}]}", {L2}),
    ("{or: [{length: {eq: 1}}], and: [{length: {eq: 2}}]}", {L2}),
    ("{and: [{length: {eq: 1}}], or: [{length: {eq: 2}}]}", {L1}),
    ("{or: [{length: {eq: 2}}], and: [{length: {eq: 1}}]}", {L1}),
    ("{and: [{length: {eq: 2}}], or: [{length: {eq: 1}}], length: {eq: 0}}", {L2}),
    ("{and: [{length: {eq: 2}}, {length: {eq: 1}}]}", set()),
    ("{or: [{length: {eq: 2}}, {length: {eq: 1}}]}", {L2, L1}),
    (
        '{and: [{or: [{length: {eq: 2}}, {length: {eq: 1}}]}, {some: {name: {eq: "bug"}}}]}',
        {L2, L1},
    ),
    # inside `and` a branch that constrains nothing is dropped; inside `or` it makes the whole filter
    # constrain nothing, and so does an `and` / `or` with nothing left in it
    ("{and: [{}, {length: {eq: 2}}]}", {L2}),
    ("{or: [{}, {length: {eq: 2}}]}", EVERY),
    ("{or: [{some: {}}, {length: {eq: 2}}]}", EVERY),
    ("{or: [{null: false}, {length: {eq: 99}}]}", EVERY),
    ("{and: [{}], length: {eq: 2}}", EVERY),
    ("{and: [{length: {eq: 2}}], or: [{}]}", {L2}),
    ("{and: [{}], or: [{length: {eq: 2}}]}", EVERY),
    ("{or: [{length: {eq: 1}}], and: [{}]}", EVERY),
    ("{or: [{length: {eq: 2}}], and: []}", EVERY),
    ("{or: [], length: {eq: 99}}", EVERY),
    ("{and: [], length: {eq: 99}}", EVERY),
    ("{or: [{}], length: {eq: 99}}", EVERY),
    # inside a predicate's `or`: a branch with nothing in it (`{}`, `and: []`) is dropped and the
    # `or` reads as negative, an empty `or` branch is dropped and reads positive, a branch that
    # constrains nothing (`name: {}`, `and: [{}]`) makes the whole predicate constrain nothing
    ('{some: {or: [{}, {name: {eq: "gateway"}}]}}', {L2, U}),
    ('{every: {or: [{}, {name: {eq: "gateway"}}]}}', {U}),
    ('{some: {or: [{}, {}, {name: {eq: "gateway"}}]}}', {L2, U}),
    ('{some: {or: [{and: []}, {name: {eq: "gateway"}}]}}', {L2, U}),
    ('{some: {or: [{}, {name: {neq: "gateway"}}]}}', EVERY),
    ('{every: {or: [{}, {name: {neq: "gateway"}}]}}', {L1, U}),
    ('{every: {and: [{or: [{}, {name: {eq: "gateway"}}]}]}}', {U}),
    ('{some: {or: [{or: []}, {name: {eq: "gateway"}}]}}', {L2}),
    ('{some: {or: [{name: {}}, {name: {eq: "gateway"}}]}}', EVERY),
    ('{every: {or: [{name: {}}, {name: {eq: "gateway"}}]}}', EVERY),
    ('{some: {or: [{and: [{}]}, {name: {eq: "gateway"}}]}}', EVERY),
    ('{some: {and: [{}, {name: {eq: "gateway"}}]}}', {L2}),
    ('{some: {and: [{name: {}}, {name: {eq: "gateway"}}]}}', {L2}),
    ('{some: {and: [{and: []}, {name: {eq: "gateway"}}]}}', {L2}),
    ("{some: {and: [{}]}}", EVERY),
    ("{some: {or: [{and: []}]}}", EVERY),
    ('{some: {name: {eq: "gateway"}, or: [{}]}}', {L2}),
    ('{some: {name: {eq: "gateway"}, or: [{}, {name: {eq: "bug"}}]}}', set()),
    # a null operand: a comparison with null matches no label, a string or list operator with null
    # is a condition every label passes, and the operator keeps its polarity
    ("{some: {name: {eq: null}}}", set()),
    ("{every: {name: {eq: null}}}", set()),
    ("{some: {name: {neq: null}}}", {U}),
    ("{every: {name: {neq: null}}}", {U}),
    ("{some: {name: {contains: null}}}", {L2, L1}),
    ("{every: {name: {contains: null}}}", {L2, L1}),
    ("{some: {name: {startsWith: null}}}", {L2, L1}),
    ("{some: {name: {endsWith: null}}}", {L2, L1}),
    ("{some: {name: {eqIgnoreCase: null}}}", {L2, L1}),
    ("{some: {name: {containsIgnoreCase: null}}}", {L2, L1}),
    ("{some: {name: {in: null}}}", {L2, L1}),
    ("{every: {name: {in: null}}}", {L2, L1}),
    ("{some: {name: {neqIgnoreCase: null}}}", EVERY),
    ("{every: {name: {nin: null}}}", EVERY),
    ('{some: {name: {eq: "bug", contains: null}}}', {L2, L1}),
    ('{some: {or: [{name: {contains: null}}, {name: {eq: "gateway"}}]}}', {L2, L1}),
    ("{length: {eq: null}}", set()),
    ("{length: {neq: null}}", set()),
    ("{length: {eq: 2, lt: null}}", set()),
    # the keys of one `or` branch (measured 2026-09-06 over `[Bug, Feature]` and `[Bug]` beside two
    # label-less issues; `bug` stands for `Bug`, `gateway` for `Feature`, `nope` for `Improvement`,
    # which nobody has). On the collection filter they are NOT alternatives: each branch is one
    # collection filter, read by the precedence above, key order included
    ('{or: [{length: {eq: 1}, some: {name: {eq: "gateway"}}}]}', {L1}),
    ('{or: [{length: {eq: 1}}, {some: {name: {eq: "gateway"}}}]}', {L2, L1}),
    ('{or: [{some: {name: {eq: "gateway"}}, length: {eq: 1}}]}', {L1}),
    ('{or: [{length: {eq: 0}, name: {eq: "gateway"}}]}', {U}),
    ('{or: [{every: {name: {eq: "bug"}}, some: {name: {eq: "gateway"}}}]}', {L1}),
    ('{or: [{some: {name: {eq: "gateway"}}, every: {name: {eq: "bug"}}}]}', {L1}),
    ("{or: [{and: [{length: {eq: 1}}], length: {eq: 2}}]}", {L1}),
    ("{or: [{or: [{length: {eq: 1}}], length: {eq: 2}}]}", {L1}),
    ("{or: [{null: false, length: {eq: 1}}]}", {L1}),
    ("{or: [{null: true, length: {eq: 1}}]}", set()),
    ("{or: [{length: {eq: 1}, null: true}]}", set()),
    ('{or: [{length: {eq: 1}, some: {name: {eq: "gateway"}}}], length: {eq: 0}}', {L1}),
    ('{or: [{length: {eq: 1}, some: {name: {eq: "gateway"}}}, {}]}', EVERY),
    ("{or: [{length: {eq: 1}, some: {}}]}", {L1}),
    ('{or: [{length: {eq: 1}, some: {name: {eq: "nope"}}}]}', {L1}),
    ('{and: [{length: {eq: 1}, some: {name: {eq: "gateway"}}}]}', {L1}),
    ('{and: [{some: {name: {eq: "gateway"}}, length: {eq: 1}}]}', {L1}),
    ('{and: [{length: {eq: 0}, name: {eq: "gateway"}}]}', {U}),
    # on the predicate (`IssueLabelFilter`) they ARE alternatives, as on every other `or`: a label
    # named A or B, where the same object under `and` is a label named both
    ('{some: {or: [{name: {eq: "gateway"}, and: [{name: {eq: "nope"}}]}]}}', {L2}),
    ('{some: {or: [{name: {eq: "nope"}, and: [{name: {eq: "gateway"}}]}]}}', {L2}),
    ('{some: {or: [{name: {eq: "gateway"}}, {and: [{name: {eq: "nope"}}]}]}}', {L2}),
    ('{some: {or: [{name: {eq: "gateway"}, or: [{name: {eq: "nope"}}]}]}}', {L2}),
    (
        '{some: {or: [{name: {eq: "nope"}, or: [{name: {eq: "gateway"}}, {name: {eq: "nope"}}]}]}}',
        {L2},
    ),
    ('{every: {or: [{name: {eq: "bug"}, and: [{name: {eq: "nope"}}]}]}}', {L1}),
    ('{some: {and: [{name: {eq: "gateway"}, or: [{name: {eq: "nope"}}]}]}}', set()),
    ('{some: {and: [{name: {eq: "bug"}, or: [{name: {eq: "gateway"}}]}]}}', set()),
    ('{some: {and: [{name: {eq: "bug"}, or: [{name: {eq: "bug"}}]}]}}', {L2, L1}),
    (
        '{some: {or: [{name: {eq: "nope"}, and: [{name: {eq: "gateway"}}, {name: {eq: "bug"}}]}]}}',
        set(),
    ),
    # the polarity of such a branch is read from the branch as written: negative only when every
    # key is, where the same keys as separate branches read negative when any is
    ('{some: {or: [{name: {neq: "gateway"}, and: [{name: {eq: "nope"}}]}]}}', {L2, L1}),
    ('{some: {or: [{name: {neq: "gateway"}}, {and: [{name: {eq: "nope"}}]}]}}', EVERY),
    ('{some: {or: [{name: {eq: "nope"}, and: [{name: {neq: "gateway"}}]}]}}', {L2, L1}),
    ('{every: {or: [{name: {neq: "gateway"}, and: [{name: {eq: "nope"}}]}]}}', {L1}),
    ('{every: {or: [{name: {neq: "bug"}, and: [{name: {eq: "bug"}}]}]}}', {L2, L1}),
    ('{every: {or: [{name: {neq: "bug"}}, {and: [{name: {eq: "bug"}}]}]}}', EVERY),
    # a key that constrains nothing beside one that does is a predicate every label satisfies (the
    # quantifier still asks for a label), not a vacuous branch; `and: []` / `or: []` beside a key drop
    ('{some: {or: [{name: {}, and: [{name: {eq: "nope"}}]}]}}', {L2, L1}),
    ('{some: {or: [{name: {eq: "nope"}, and: [{}]}]}}', {L2, L1}),
    ('{some: {or: [{name: {eq: "nope"}, or: [{}]}]}}', {L2, L1}),
    ('{some: {or: [{name: {eq: "gateway"}, and: []}]}}', {L2}),
    ('{some: {or: [{name: {eq: "gateway"}, or: []}]}}', {L2}),
    # ... but beside a key that constrains nothing, `and: []` leaves a vacuous branch and `or: []`
    # a predicate every label satisfies
    ("{some: {or: [{name: {}, and: []}]}}", EVERY),
    ("{some: {or: [{name: {}, and: [{}]}]}}", EVERY),
    ('{some: {or: [{name: {}, and: []}, {name: {eq: "gateway"}}]}}', EVERY),
    ("{some: {or: [{name: {}, or: []}]}}", {L2, L1}),
    ('{some: {or: [{name: {}, or: []}, {name: {eq: "gateway"}}]}}', {L2, L1}),
    # and such a branch still carries its polarity, read from the branch as written: with a negative
    # key it is a negative predicate every label satisfies, so the label-less issues qualify too
    ('{some: {or: [{name: {}, and: [{name: {neq: "gateway"}}]}]}}', EVERY),
    ('{some: {or: [{name: {}, or: [{name: {neq: "gateway"}}]}]}}', EVERY),
    ('{every: {or: [{name: {}, and: [{name: {neq: "gateway"}}]}]}}', EVERY),
    ('{every: {or: [{name: {}, or: [{name: {neq: "gateway"}}]}]}}', EVERY),
    ('{some: {or: [{name: {}, and: [{name: {neq: "nope"}}]}]}}', EVERY),
    ('{some: {or: [{name: {neq: "nope"}, and: [{}]}]}}', EVERY),
    ('{some: {or: [{name: {neq: "gateway"}, and: [{}]}]}}', EVERY),
    ('{every: {or: [{name: {neq: "gateway"}, and: [{}]}]}}', EVERY),
    # outside an `or` branch, at the top of a quantifier and in an `and` branch, the keys AND and a
    # key that constrains nothing is not read (measured 2026-09-06, same fixture)
    ('{some: {name: {}, and: [{name: {eq: "nope"}}]}}', set()),
    ('{some: {name: {}, or: [{name: {eq: "nope"}}]}}', set()),
    ('{every: {name: {}, and: [{name: {eq: "bug"}}]}}', {L1}),
    ('{every: {name: {}, or: [{name: {eq: "nope"}}]}}', set()),
    ('{some: {name: {eq: "gateway"}, and: [{}]}}', {L2}),
    ('{every: {name: {eq: "bug"}, and: [{}]}}', {L1}),
    ('{some: {name: {eq: "gateway"}, and: []}}', {L2}),
    ('{some: {name: {eq: "gateway"}, or: []}}', {L2}),
    ('{some: {name: {eq: "nope"}, and: [{}], or: [{}]}}', set()),
    ('{some: {and: [{name: {}, or: [{name: {eq: "nope"}}]}]}}', set()),
    ('{some: {and: [{name: {eq: "gateway"}, and: [{}]}]}}', {L2}),
    ('{some: {and: [{name: {}, and: [{name: {eq: "nope"}}]}, {name: {eq: "bug"}}]}}', set()),
    ('{some: {name: {eq: "bug"}, or: [{name: {eq: "gateway"}}]}}', set()),
    ('{some: {name: {eq: "bug"}, and: [{name: {eq: "gateway"}}]}}', set()),
    ('{every: {name: {eq: "bug"}, or: [{name: {eq: "gateway"}}]}}', set()),
    ('{some: {name: {neq: "gateway"}, or: [{name: {eq: "nope"}}]}}', set()),
    ('{some: {name: {neq: "gateway"}, and: [{name: {eq: "bug"}}]}}', {L2, L1}),
    ('{every: {name: {neq: "gateway"}, or: [{name: {eq: "bug"}}]}}', {L1}),
    # `and: []` and `or: []` drop out of the predicate but not of the polarity: beside a negative key
    # `or: []` (any branch of none) reads positive and `and: []` (every branch of none) keeps the
    # negative reading, in an `or` branch and at the top alike; `or: [{}]` beside a key reads as
    # `and: [{}]` does, a vacuous branch beside a key that constrains nothing (measured 2026-09-07,
    # same fixture)
    ('{some: {or: [{name: {neq: "gateway"}, or: []}]}}', {L2, L1}),
    ('{some: {or: [{name: {neq: "gateway"}, and: []}]}}', EVERY),
    ('{some: {or: [{name: {neq: "gateway"}}]}}', EVERY),
    ('{every: {or: [{name: {neq: "gateway"}, or: []}]}}', {L1}),
    ('{every: {or: [{name: {neq: "gateway"}, and: []}]}}', {L1, U}),
    ('{some: {name: {neq: "gateway"}, or: []}}', {L2, L1}),
    ('{some: {name: {neq: "gateway"}, and: []}}', EVERY),
    ('{every: {name: {neq: "gateway"}, or: []}}', {L1}),
    ('{every: {name: {neq: "gateway"}, and: []}}', {L1, U}),
    ('{some: {or: [{name: {neq: "gateway"}, or: [{}]}]}}', EVERY),
    ("{some: {or: [{name: {}, or: [{}]}]}}", EVERY),
    ('{some: {or: [{name: {}, or: [{}]}, {name: {eq: "gateway"}}]}}', EVERY),
    ("{every: {or: [{name: {}, or: [{}]}]}}", EVERY),
]


def test_labels_quantifiers_answer_as_linear(fclient):
    """Linear pushes a negation outside the quantifier: `some` over a negative predicate is the
    complement of `every` over its positive form, so it answers an issue with no labels; and `every`
    over a positive predicate needs at least one label, so it does not. The textbook EXISTS and
    NOT EXISTS answer the opposite on both counts, which is what #112 measured over label-less
    issues. Each cell here is one measured answer; the mapping to the fixture is above
    `_LABEL_CELLS`."""
    assert {literal: ids(fclient, "{labels: %s}" % literal) for literal, _ in _LABEL_CELLS} == {
        literal: sorted(expected) for literal, expected in _LABEL_CELLS
    }


# The whole `IssueFilter`, where an `or` branch that constrains nothing is the case that matters:
# api.linear.app answers it by making the whole `or` constrain nothing, where dropping the branch
# would narrow the answer to the other branches. Same fixture and stand-ins as `_LABEL_CELLS`;
# `ONE` is a branch that answers ENG-2 alone, `NONE` one that answers nothing.
ONE, NONE = "{labels: {length: {eq: 1}}}", "{labels: {length: {eq: 99}}}"
_ISSUE_CELLS = [
    # a branch with nothing in it is dropped
    ("{or: [{}, %s]}" % ONE, {L1}),
    ("{or: [{and: []}, %s]}" % ONE, {L1}),
    ("{or: [{or: []}, %s]}" % ONE, {L1}),
    ("{or: [{title: null}, %s]}" % ONE, {L1}),
    ("{or: [%s]}" % ONE, {L1}),
    ("{or: []}", EVERY),
    ("{and: []}", EVERY),
    ("{or: [{}]}", EVERY),
    # a branch with a key that constrains nothing makes the whole `or` constrain nothing, its other
    # keys included
    ("{or: [{labels: {}}, %s]}" % ONE, EVERY),
    ("{or: [{labels: {some: {}}}, %s]}" % ONE, EVERY),
    ("{or: [{labels: {every: {}}}, %s]}" % ONE, EVERY),
    ("{or: [{labels: {null: false}}, %s]}" % ONE, EVERY),
    ("{or: [{labels: {and: []}}, %s]}" % ONE, EVERY),
    ("{or: [{labels: {some: {name: {}}}}, %s]}" % ONE, EVERY),
    ("{or: [{labels: {or: [{}, {length: {eq: 99}}]}}, %s]}" % ONE, EVERY),
    ("{or: [{labels: {some: null}}, %s]}" % NONE, EVERY),
    ("{or: [{labels: {some: {name: null}}}, %s]}" % NONE, EVERY),
    ("{or: [{labels: {null: null}}, %s]}" % NONE, EVERY),
    ("{or: [{title: {}}, %s]}" % ONE, EVERY),
    ("{or: [{priority: {}}, %s]}" % NONE, EVERY),
    ("{or: [{estimate: {}}, %s]}" % NONE, EVERY),
    ("{or: [{state: {}}, %s]}" % NONE, EVERY),
    ("{or: [{state: {name: {}}}, %s]}" % NONE, EVERY),
    ('{or: [{labels: {}, title: {eq: "no such title"}}, %s]}' % ONE, EVERY),
    ('{or: [{title: {eq: "no such title"}, labels: {}}, %s]}' % ONE, EVERY),
    ("{or: [{title: {eq: null}, labels: {}}, %s]}" % NONE, EVERY),
    ("{or: [{or: [{labels: {}}, %s]}, %s]}" % (NONE, ONE), EVERY),
    ("{or: [{or: [{}]}, %s]}" % ONE, EVERY),
    ("{or: [{and: [{}]}, %s]}" % NONE, EVERY),
    ("{or: [{and: [{labels: {}}]}, %s]}" % NONE, EVERY),
    # inside an `and`, a branch that constrains nothing is dropped, and it does not make the `and`
    # a branch that constrains nothing
    ("{and: [{}, %s]}" % ONE, {L1}),
    ("{and: [{labels: {}}, %s]}" % ONE, {L1}),
    ("{and: [{or: [{}]}, %s]}" % ONE, {L1}),
    ("{and: [{or: [{}]}, %s]}" % NONE, set()),
    ("{and: [{title: {}}, %s]}" % NONE, set()),
    ("{and: [{title: {contains: null}}, %s]}" % NONE, set()),
    ("{or: [{and: [{labels: {}}, %s]}, %s]}" % (NONE, ONE), {L1}),
    ("{or: [{and: [{}, %s]}, %s]}" % (NONE, NONE), set()),
    ('{labels: {}, title: {eq: "no such title"}}', set()),
    # a labels branch that DOES constrain answers as itself
    ("{or: [%s, %s]}" % (NONE, ONE), {L1}),
    ('{or: [{labels: {some: {name: {eq: "gateway"}}}}, %s]}' % NONE, {L2}),
    ('{or: [{labels: {some: {or: [{}, {name: {eq: "gateway"}}]}}}, %s]}' % NONE, {L2, U}),
    ("{or: [{labels: {or: [{length: {eq: 0}}], length: {eq: 2}}}, %s]}" % NONE, {U}),
    ("{or: [{labels: {some: {name: {contains: null}}}}, %s]}" % NONE, {L2, L1}),
    # a null operand on an issue field: a comparison with null matches nothing, a string or list
    # operator with null is a condition every issue passes -- in an `or` and on its own
    ("{or: [{title: {eq: null}}, %s]}" % ONE, {L1}),
    ("{or: [{title: {eq: null, neq: null}}, %s]}" % NONE, set()),
    ("{or: [{dueDate: {eq: null}}, %s]}" % NONE, set()),
    ("{or: [{labels: {some: {name: {eq: null}}}}, %s]}" % NONE, set()),
    ("{or: [{labels: {length: {eq: null}}}, %s]}" % NONE, set()),
    ("{or: [{title: {contains: null}}, %s]}" % NONE, EVERY),
    ("{or: [{title: {neqIgnoreCase: null}}, %s]}" % NONE, EVERY),
    ("{title: {eq: null}}", set()),
    ("{title: {neq: null}}", set()),
    ("{dueDate: {eq: null}}", set()),
    ("{dueDate: {neq: null}}", set()),
    ("{estimate: {eq: null}}", set()),
    ("{estimate: {gte: null}}", set()),
    ("{priority: {eq: null}}", set()),
    ("{title: {contains: null}}", EVERY),
    ("{title: {in: null}}", EVERY),
    ("{title: {nin: null}}", EVERY),
    ("{title: {startsWith: null}}", EVERY),
    ("{title: {eqIgnoreCase: null}}", EVERY),
    ("{title: {neqIgnoreCase: null}}", EVERY),
    # `null: null` on `null` itself: on a field it reads as `null: true`, beside a sibling too; on a
    # relation and on the label collection it is no key at all, and what is left decides
    ("{dueDate: {null: null}}", {U}),
    ("{estimate: {null: null}}", {U}),
    ("{completedAt: {null: null}}", EVERY),
    ("{canceledAt: {null: null}}", EVERY),
    ("{priority: {null: null}}", set()),
    ("{priority: {null: null, eq: 4}}", set()),
    ("{or: [{priority: {null: null}}, %s]}" % ONE, {L1}),
    ("{or: [{dueDate: {null: null}}, %s]}" % NONE, {U}),
    ("{assignee: {null: null}}", EVERY),
    ("{project: {null: null}}", EVERY),
    ("{creator: {null: null}}", EVERY),
    ('{assignee: {null: null, name: {eq: "Bob Stone"}}}', {L2}),
    ('{assignee: {null: null, name: {eq: "nobody"}}}', set()),
    ("{or: [{assignee: {null: null}}, %s]}" % NONE, EVERY),
    ('{or: [{assignee: {null: null, name: {eq: "nobody"}}}, %s]}' % NONE, set()),
    ("{labels: {null: null}}", EVERY),
    ("{labels: {null: null, length: {eq: 0}}}", {U}),
]


def test_issue_filter_or_answers_a_vacuous_branch_as_linear(fclient):
    """An `or` branch that constrains nothing is not dropped by api.linear.app: it makes the whole
    `or` constrain nothing, so `{or: [{labels: {}}, X]}` answers every issue where X alone answers
    a few. Dropping it, which the empty fragment invites, is a refusal turned into a quietly
    narrower answer. A branch with nothing in it IS dropped, and a null operand is a condition of
    its own. Each cell is one measured answer; the stand-ins are above `_ISSUE_CELLS`."""
    assert {literal: ids(fclient, literal) for literal, _ in _ISSUE_CELLS} == {
        literal: sorted(expected) for literal, expected in _ISSUE_CELLS
    }


# A nullable relation's `null: true` beside other keys. api.linear.app reads nothing else in the
# object: `{null: true, name: {eq: X}}` is the issues without the relation, not none (measured
# 2026-09-04 on `project` with two issues in two throwaway projects beside two in none, and on
# `assignee` with one issue assigned to the viewer beside three unassigned). `and` branches are keys
# of the same object, the object's own `null` wins over its branches', `null: false` does AND, and
# an `or` is the union of its branches. Same fixture: ENG-1 is Bob Stone's and in `runtime`, ENG-2 is
# in `runtime` and unassigned, DES-1 has neither; every issue has a creator.
_RUNTIME = synth.linear_project_id("runtime")
_RELATION_NULL_CELLS = [
    # `null: true` reads nothing else in the object
    ('{assignee: {null: true, name: {eq: "Bob Stone"}}}', {L1, U}),
    ('{assignee: {null: true, name: {eq: "nobody"}}}', {L1, U}),
    ('{assignee: {null: true, name: {neq: "Bob Stone"}}}', {L1, U}),
    ('{assignee: {null: true, email: {eq: "bob@acme.com"}, name: {eq: "Bob Stone"}}}', {L1, U}),
    ('{assignee: {null: true, and: [{name: {eq: "Bob Stone"}}]}}', {L1, U}),
    ('{assignee: {null: true, or: [{name: {eq: "Bob Stone"}}]}}', {L1, U}),
    ('{project: {null: true, name: {eq: "runtime"}}}', {U}),
    ('{project: {null: true, id: {eq: "%s"}}}' % _RUNTIME, {U}),
    # `and` branches are keys of the same object
    ('{assignee: {and: [{null: true}], name: {eq: "Bob Stone"}}}', {L1, U}),
    ('{assignee: {and: [{null: true}, {name: {eq: "Bob Stone"}}]}}', {L1, U}),
    ('{assignee: {and: [{and: [{null: true}]}, {name: {eq: "Bob Stone"}}]}}', {L1, U}),
    ("{assignee: {and: [{null: true}, {null: false}]}}", {L1, U}),
    ("{assignee: {and: [{null: false}, {null: true}]}}", {L1, U}),
    # the object's own `null` wins over its branches'
    ("{assignee: {null: false, and: [{null: true}]}}", {L2}),
    ("{assignee: {null: true, and: [{null: false}]}}", {L1, U}),
    ("{creator: {null: false, and: [{null: true}]}}", EVERY),
    # `null: false` does AND
    ('{assignee: {null: false, name: {eq: "Bob Stone"}}}', {L2}),
    ('{assignee: {null: false, name: {neq: "Bob Stone"}}}', set()),
    ('{assignee: {null: false, name: {eq: "nobody"}}}', set()),
    ('{project: {null: false, name: {eq: "runtime"}}}', {L1, L2}),
    ('{assignee: {null: false, or: [{null: true}, {name: {eq: "Bob Stone"}}]}}', {L2}),
    # an `or` is the union of its branches, each read by this rule
    ('{assignee: {or: [{null: true}, {name: {eq: "Bob Stone"}}]}}', EVERY),
    ('{assignee: {or: [{null: true, name: {eq: "Bob Stone"}}]}}', {L1, U}),
    # two relation filters on the same relation are two filters, ANDed as `IssueFilter` ANDs
    ('{and: [{assignee: {null: true}}, {assignee: {name: {eq: "Bob Stone"}}}]}', set()),
    ('{or: [{assignee: {null: true, name: {eq: "Bob Stone"}}}, %s]}' % NONE, {L1, U}),
    ('{project: {null: true, name: {eq: "runtime"}}, assignee: {null: true}}', {U}),
    ('{project: {null: true, name: {eq: "runtime"}}, assignee: {name: {eq: "Bob Stone"}}}', set()),
]


def test_relation_null_true_reads_nothing_else_in_the_object_as_linear(fclient):
    """`null: true` on a nullable relation is not one condition among the object's keys: Linear
    answers the issues without the relation whatever else the object says, where ANDing the keys
    answers none. `null: false` does AND with them. Each cell is one measured answer; the mapping
    to the fixture is above `_RELATION_NULL_CELLS`."""
    assert {literal: ids(fclient, literal) for literal, _ in _RELATION_NULL_CELLS} == {
        literal: sorted(expected) for literal, expected in _RELATION_NULL_CELLS
    }


# An `or` on a nullable relation, cell by cell, as api.linear.app answered it on 2026-09-04. The
# workspace had four issues: BRE-1 in project `zz-pa` and assigned to the viewer (`nuri`), BRE-2 in
# `zz-pb`, BRE-3 and BRE-4 in no project and unassigned; the projects were created for the
# measurement and deleted after. The fixture below has the same shape, names included, and `nobody`
# is a name no project or user has. 458 filters were measured in eleven rounds, every round from
# the third predicted from the procedure on `_sub_filter` before it was sent; these cells are the
# ones that tell its steps apart, each one a measured answer.
_RELATION_OR_CORPUS = [
    {
        "source_type": "linear",
        "doc_id": "rel1",
        "team": "brekky",
        "group": "brekky",
        "title": "one",
        "content": "x",
        "identifier": "BRE-1",
        "author_email": "ava@acme.com",
        "author_groups": ["brekky"],
        "visibility": "public",
        "project": "zz-pa",
        "assignee": "nuri@acme.com",
        "assigneeName": "nuri",
        "created": "2026-01-01T00:00:00Z",
    },
    {
        "source_type": "linear",
        "doc_id": "rel2",
        "team": "brekky",
        "group": "brekky",
        "title": "two",
        "content": "x",
        "identifier": "BRE-2",
        "author_email": "ava@acme.com",
        "author_groups": ["brekky"],
        "visibility": "public",
        "project": "zz-pb",
        "created": "2026-01-02T00:00:00Z",
    },
    {
        "source_type": "linear",
        "doc_id": "rel3",
        "team": "brekky",
        "group": "brekky",
        "title": "three",
        "content": "x",
        "identifier": "BRE-3",
        "author_email": "ava@acme.com",
        "author_groups": ["brekky"],
        "visibility": "public",
        "created": "2026-01-03T00:00:00Z",
    },
    {
        "source_type": "linear",
        "doc_id": "rel4",
        "team": "brekky",
        "group": "brekky",
        "title": "four",
        "content": "x",
        "identifier": "BRE-4",
        "author_email": "ava@acme.com",
        "author_groups": ["brekky"],
        "visibility": "public",
        "created": "2026-01-04T00:00:00Z",
    },
]
W1, W2, N3, N4 = "BRE-1", "BRE-2", "BRE-3", "BRE-4"
_ZZ_PA, _ZZ_PB = synth.linear_project_id("zz-pa"), synth.linear_project_id("zz-pb")
_RELATION_OR_CELLS = [
    # the keys of one branch are alternatives; outside an `or` the same keys AND
    ("project", '{or: [{name: {eq: "zz-pa"}, id: {eq: "%s"}}]}' % _ZZ_PB, {W1, W2}),
    ("project", '{name: {eq: "zz-pa"}, id: {eq: "%s"}}' % _ZZ_PB, set()),
    ("project", '{or: [{and: [{name: {eq: "zz-pa"}}, {id: {eq: "%s"}}]}]}' % _ZZ_PB, set()),
    (
        "project",
        '{or: [{name: {eq: "zz-pa"}, id: {eq: "%s"}}], name: {eq: "zz-pb"}}' % _ZZ_PB,
        {W2},
    ),
    ("project", '{or: [{null: false, name: {eq: "zz-pa"}, id: {eq: "%s"}}]}' % _ZZ_PB, {W1, W2}),
    ("project", '{or: [{null: false, name: {eq: "nobody"}}]}', set()),
    (
        "project",
        '{or: [{or: [{name: {eq: "zz-pa"}}, {id: {eq: "%s"}}]}, {name: {eq: "nobody"}}]}' % _ZZ_PB,
        {W1, W2},
    ),
    # a branch that says nothing about the related row, beside one that does, adds the issues without the relation
    ("project", '{or: [{null: false}, {name: {eq: "nobody"}}]}', {N3, N4}),
    ("project", '{or: [{}, {name: {eq: "nobody"}}]}', {N3, N4}),
    ("project", '{or: [{null: true}, {name: {eq: "nobody"}}]}', {N3, N4}),
    ("project", '{or: [{and: []}, {name: {eq: "nobody"}}]}', {N3, N4}),
    ("project", '{or: [{null: false}, {name: {eq: "zz-pa"}}]}', {W1, N3, N4}),
    ("project", '{or: [{name: {eq: "zz-pa"}}, {}]}', {W1, N3, N4}),
    ("project", '{or: [{null: true}, {name: {eq: "zz-pa"}}]}', {W1, N3, N4}),
    ("project", '{or: [{name: {eq: "zz-pa"}}, {name: {eq: "nobody"}}]}', {W1}),
    ("project", '{or: [{null: false}, {name: {eq: "nobody"}}, {null: false}]}', {N3, N4}),
    ("project", '{or: [{null: false}, {name: {neq: "zz-pa"}}]}', {W2, N3, N4}),
    # on their own such branches keep their meaning, and `or: []` is the issues with the relation
    ("project", "{or: [{null: false}]}", {W1, W2}),
    ("project", "{or: [{null: false}, {null: false}]}", {W1, W2}),
    ("project", "{or: [{null: true}, {null: true}]}", {N3, N4}),
    ("project", "{or: [{null: true}, {null: false}]}", {W1, W2, N3, N4}),
    ("project", "{or: [{}, {}]}", {W1, W2, N3, N4}),
    ("project", "{or: [{null: false}, {}]}", {W1, W2, N3, N4}),
    ("project", "{or: [{null: true}, {}]}", {W1, W2, N3, N4}),
    ("project", "{or: []}", {W1, W2}),
    ("project", "{}", {W1, W2, N3, N4}),
    ("project", "{and: []}", {W1, W2, N3, N4}),
    ("project", "{or: [{}]}", {W1, W2, N3, N4}),
    # every branch carrying `null: false` is FLAGS deciding false: the relation is present, and the rest reads under that
    ("project", '{or: [{null: false}, {null: false, name: {eq: "nobody"}}]}', set()),
    ("project", '{or: [{null: false}, {null: false, name: {eq: "zz-pa"}}]}', {W1}),
    ("project", '{or: [{}, {null: false, name: {eq: "nobody"}}]}', {N3, N4}),
    ("project", '{or: [{null: true}, {null: false, name: {eq: "nobody"}}]}', {N3, N4}),
    (
        "project",
        '{or: [{null: false}, {null: false, name: {eq: "zz-pa"}}, {name: {eq: "nobody"}}]}',
        {W1, N3, N4},
    ),
    ("project", '{or: [{null: false}, {null: false}, {name: {eq: "zz-pa"}}]}', {W1, N3, N4}),
    ("project", '{and: [{or: [{null: false}, {name: {eq: "nobody"}}]}]}', set()),
    ("project", '{or: [{or: [{null: false}, {name: {eq: "nobody"}}]}]}', set()),
    (
        "project",
        '{or: [{or: [{null: false}, {name: {eq: "nobody"}}]}, {or: [{name: {eq: "zz-pa"}}]}]}',
        {W1, N3, N4},
    ),
    (
        "project",
        '{or: [{or: [{null: false}, {name: {eq: "nobody"}}]}, {null: false, name: {eq: "zz-pa"}}]}',
        {W1},
    ),
    # every branch carrying `null: true` is the issues without, comparators unread; one branch without it reads them all
    ("project", '{or: [{null: true, name: {eq: "zz-pa"}}]}', {N3, N4}),
    (
        "project",
        '{or: [{null: true, name: {eq: "zz-pa"}}, {null: true, name: {eq: "zz-pb"}}]}',
        {N3, N4},
    ),
    ("project", '{or: [{null: true, name: {eq: "zz-pa"}}, {null: true}]}', {N3, N4}),
    ("project", '{or: [{null: true, name: {eq: "zz-pa"}}, {or: [{null: true}]}]}', {N3, N4}),
    (
        "project",
        '{or: [{or: [{null: true}, {name: {eq: "zz-pa"}}]}, {or: [{null: true}, {id: {eq: "%s"}}]}]}'
        % _ZZ_PB,
        {N3, N4},
    ),
    (
        "project",
        '{or: [{null: true, name: {eq: "zz-pa"}}, {name: {eq: "zz-pb"}}]}',
        {W1, W2, N3, N4},
    ),
    ("project", '{or: [{null: true, name: {eq: "zz-pa"}}, {name: {eq: "nobody"}}]}', {W1, N3, N4}),
    ("project", '{or: [{null: true, name: {eq: "zz-pa"}}, {null: false}]}', {W1, N3, N4}),
    (
        "project",
        '{or: [{null: true, name: {eq: "zz-pa"}}, {name: {eq: "nobody"}}, {id: {eq: "%s"}}]}'
        % _ZZ_PB,
        {W1, W2, N3, N4},
    ),
    ("project", '{or: [{null: true, name: {eq: "zz-pa"}}, {}]}', {W1, N3, N4}),
    (
        "project",
        '{or: [{null: true, name: {eq: "zz-pa"}}, {null: false, name: {eq: "nobody"}}]}',
        {W1, N3, N4},
    ),
    # a `null: true` anywhere below is ASSEMBLE's `IS NULL OR rest`; the object's own `null` decides first
    ("project", '{or: [{null: true}], name: {eq: "zz-pa"}}', {N3, N4}),
    ("project", '{or: [{null: true}, {name: {eq: "zz-pb"}}], name: {eq: "zz-pa"}}', {N3, N4}),
    ("project", '{or: [{null: true}, {name: {eq: "zz-pa"}}], id: {eq: "%s"}}' % _ZZ_PB, {N3, N4}),
    ("project", '{or: [{or: [{null: true}]}], name: {eq: "zz-pa"}}', {N3, N4}),
    (
        "project",
        '{and: [{or: [{null: true}, {name: {eq: "nobody"}}]}], name: {eq: "zz-pa"}}',
        {N3, N4},
    ),
    ("project", '{and: [{or: [{null: true}]}], or: [{name: {eq: "zz-pa"}}]}', {N3, N4}),
    ("project", '{and: [{null: true}], or: [{name: {eq: "zz-pa"}}]}', {N3, N4}),
    ("project", '{or: [{null: false}], name: {eq: "zz-pa"}}', {W1}),
    ("project", '{or: [{}], name: {eq: "zz-pa"}}', {W1}),
    ("project", '{or: [{null: false}, {name: {eq: "nobody"}}], name: {eq: "zz-pb"}}', set()),
    ("project", '{and: [{or: [{}, {name: {eq: "nobody"}}]}], name: {eq: "zz-pa"}}', set()),
    ("project", '{null: false, or: [{null: true}, {name: {eq: "zz-pa"}}]}', {W1}),
    ("project", '{null: false, or: [{}, {name: {eq: "zz-pa"}}]}', {W1}),
    ("project", '{null: false, and: [{or: [{null: true}, {name: {eq: "zz-pa"}}]}]}', {W1}),
    ("project", "{or: [{}], null: false}", {W1, W2}),
    ("project", '{null: true, or: [{name: {eq: "zz-pa"}}]}', {N3, N4}),
    # an `or` inside a branch renders its related-row side only; nested `or: []` and `and: []` render nothing
    ("project", '{or: [{or: [{null: true}]}, {name: {eq: "nobody"}}]}', {W1, W2, N3, N4}),
    ("project", '{or: [{or: [{null: false}]}, {name: {eq: "nobody"}}]}', {W1, W2, N3, N4}),
    ("project", '{or: [{and: [{null: false}]}, {name: {eq: "zz-pa"}}]}', {W1, W2, N3, N4}),
    ("project", '{or: [{or: [{}]}, {name: {eq: "nobody"}}]}', {W1, W2, N3, N4}),
    ("project", '{or: [{or: []}, {name: {eq: "nobody"}}]}', set()),
    ("project", '{or: [{or: [{null: true}, {name: {eq: "zz-pa"}}]}]}', {N3, N4}),
    ("project", '{or: [{or: [{}, {name: {eq: "zz-pa"}}]}]}', {W1, N3, N4}),
    ("project", "{or: [{or: []}]}", {W1, W2}),
    ("project", "{or: [{or: [{}]}]}", {W1, W2, N3, N4}),
    ("project", '{or: [{or: [{null: true}, {name: {eq: "zz-pa"}}]}, {null: true}]}', {N3, N4}),
    (
        "project",
        '{or: [{or: [{null: true}], name: {eq: "zz-pa"}}, {name: {eq: "nobody"}}]}',
        {W1, W2, N3, N4},
    ),
    ("project", '{or: [{and: [{name: {eq: "zz-pa"}}, {or: [{null: true}]}]}]}', {N3, N4}),
    ("project", "{or: [{null: true}, {or: []}]}", {W1, W2, N3, N4}),
    ("project", "{or: [{or: []}, {or: []}]}", {W1, W2}),
    (
        "project",
        '{and: [{or: [{}, {name: {eq: "zz-pa"}}]}, {or: [{name: {eq: "zz-pa"}}, {id: {eq: "%s"}}]}]}'
        % _ZZ_PB,
        {W1},
    ),
    # `neq` admits the issues without the relation unless every branch carries `null: false`
    ("project", '{name: {neq: "zz-pa"}}', {W2, N3, N4}),
    ("project", '{or: [{name: {neq: "zz-pa"}}, {}]}', {W2, N3, N4}),
    ("project", '{or: [{null: false, name: {neq: "zz-pa"}}]}', {W2}),
    ("project", '{or: [{null: false}, {null: false, name: {neq: "zz-pa"}}]}', {W2}),
    (
        "project",
        '{or: [{null: false, name: {eq: "zz-pa"}}, {name: {neq: "zz-pa"}}]}',
        {W1, W2, N3, N4},
    ),
    ("project", '{or: [{}, {null: false, name: {neq: "zz-pa"}}]}', {W2, N3, N4}),
    (
        "project",
        '{or: [{and: [{null: false}, {name: {neq: "zz-pa"}}]}, {or: [{null: true}]}]}',
        {W1, W2, N3, N4},
    ),
    # `assignee` takes the same path (the viewer is `nuri`)
    ("assignee", '{or: [{name: {eq: "nuri"}, email: {eq: "nobody"}}]}', {W1}),
    (
        "assignee",
        '{or: [{null: true, name: {eq: "nuri"}}, {email: {eq: "nobody"}}]}',
        {W1, W2, N3, N4},
    ),
    ("assignee", '{or: [{null: true, name: {eq: "nuri"}}]}', {W2, N3, N4}),
    ("assignee", '{or: [{null: false}, {email: {eq: "nobody"}}]}', {W2, N3, N4}),
    ("assignee", '{or: [{}, {name: {eq: "nuri"}}]}', {W1, W2, N3, N4}),
    ("assignee", '{or: [{null: false}, {null: false, name: {eq: "nuri"}}]}', {W1}),
    ("assignee", '{or: [{or: [{null: false}, {email: {eq: "nobody"}}]}]}', set()),
    ("assignee", "{or: []}", {W1}),
    ("assignee", '{or: [{null: false}, {name: {neq: "nuri"}}]}', {W2, N3, N4}),
    ("assignee", '{null: false, or: [{}, {name: {eq: "nuri"}}]}', {W1}),
    # the `IS NULL OR rest` is over the whole object, so a sibling key or a second list still reads
    ("project", '{or: [{null: true}, {name: {eq: "zz-pa"}}], name: {eq: "zz-pa"}}', {W1, N3, N4}),
    (
        "project",
        '{or: [{null: true}, {name: {eq: "zz-pa"}}], id: {eq: "%s"}}' % _ZZ_PA,
        {W1, N3, N4},
    ),
    (
        "project",
        '{and: [{or: [{null: true}, {name: {eq: "zz-pa"}}]}], or: [{name: {eq: "zz-pa"}}]}',
        {W1, N3, N4},
    ),
    (
        "project",
        '{and: [{or: [{null: true}, {name: {eq: "zz-pa"}}]}], or: [{name: {eq: "nobody"}}]}',
        {N3, N4},
    ),
    (
        "project",
        '{and: [{or: [{null: true}, {name: {eq: "zz-pa"}}]}], name: {eq: "zz-pa"}}',
        {W1, N3, N4},
    ),
    (
        "project",
        '{and: [{or: [{null: true}, {name: {eq: "zz-pa"}}]}, {or: [{name: {eq: "zz-pa"}}, {id: {eq: "%s"}}]}]}'
        % (_ZZ_PB,),
        {W1, N3, N4},
    ),
    (
        "project",
        '{and: [{or: [{null: true}, {name: {eq: "zz-pa"}}]}, {or: [{name: {eq: "nobody"}}]}]}',
        {N3, N4},
    ),
    (
        "project",
        '{and: [{or: [{null: false}, {name: {eq: "zz-pa"}}]}], or: [{null: true}, {name: {eq: "zz-pa"}}]}',
        {W1},
    ),
    # FLAGS reads an `and` branch that is an `or` with a bare `{null: false}` and no `null: true` as saying false
    ("project", '{and: [{or: [{null: false}, {name: {neq: "zz-pa"}}]}]}', {W2}),
    ("project", "{and: [{or: [{null: false}, {}]}]}", {W1, W2}),
    ("project", "{and: [{or: [{null: false}, {null: true}]}]}", {W1, W2, N3, N4}),
    (
        "project",
        '{and: [{or: [{null: false}, {null: true}, {name: {eq: "zz-pa"}}]}]}',
        {W1, N3, N4},
    ),
    ("project", '{and: [{or: [{null: false}, {}, {name: {neq: "zz-pa"}}]}]}', {W2}),
    ("project", '{and: [{or: [{null: false}, {or: [{name: {neq: "zz-pa"}}]}]}]}', {W2}),
    (
        "project",
        '{or: [{null: false}, {name: {neq: "zz-pa"}}], and: [{or: [{null: false}, {name: {neq: "zz-pa"}}]}]}',
        {W2},
    ),
    ("project", '{and: [{or: [{}, {name: {neq: "zz-pa"}}]}]}', {W2, N3, N4}),
    ("project", '{and: [{or: [{null: false}, {name: {eq: "zz-pa"}}]}], or: [{}]}', {W1}),
    # a branch that is nothing but comparators with no operator makes the `or` constrain nothing; beside a real key it is TRUE inside its branch
    ("project", "{name: {}}", {W1, W2, N3, N4}),
    ("project", '{or: [{name: {}}, {name: {eq: "nobody"}}]}', {W1, W2, N3, N4}),
    ("project", '{or: [{null: false, name: {}}, {name: {eq: "nobody"}}]}', {W1, W2, N3, N4}),
    ("project", "{or: [{null: false, name: {}}]}", {W1, W2}),
    ("project", '{or: [{name: {}, id: {eq: "%s"}}]}' % (_ZZ_PB,), {W1, W2}),
    ("project", '{or: [{name: {}, id: {eq: "%s"}}, {name: {eq: "nobody"}}]}' % (_ZZ_PB,), {W1, W2}),
    ("project", "{null: false, name: {}}", {W1, W2}),
    ("project", "{or: [{null: true, name: {}}]}", {N3, N4}),
    ("project", '{and: [{or: [{name: {}}, {name: {eq: "nobody"}}]}], name: {eq: "zz-pa"}}', {W1}),
    # `assignee`, the same three
    ("assignee", '{and: [{or: [{null: false}, {email: {neq: "nobody"}}]}]}', {W1}),
    (
        "assignee",
        '{and: [{or: [{null: true}, {name: {eq: "nuri"}}]}], or: [{name: {eq: "nuri"}}]}',
        {W1, W2, N3, N4},
    ),
    ("assignee", '{or: [{null: true}, {name: {eq: "nuri"}}], email: {eq: "nobody"}}', {W2, N3, N4}),
    # the object's own `null` drops every `null` below it before the rest compiles
    ("project", "{null: false, or: [{null: true}]}", {W1, W2}),
    ("project", '{null: false, or: [{null: true, name: {eq: "zz-pa"}}]}', {W1}),
    ("project", "{null: false, and: [{or: [{null: true}]}]}", {W1, W2}),
    ("project", '{null: false, and: [{or: [{null: true}]}], name: {eq: "zz-pa"}}', {W1}),
    # what `and` branches say about `null` merges with true winning: an `or` whose every branch carries `null: true` says true, one with a bare `{null: false}` and no `null: true` says false
    (
        "project",
        '{and: [{or: [{null: false}, {name: {eq: "nobody"}}]}, {or: [{null: true}]}]}',
        {N3, N4},
    ),
    (
        "project",
        '{and: [{or: [{null: false}, {name: {eq: "zz-pa"}}]}, {or: [{null: true}]}], name: {eq: "zz-pa"}}',
        {N3, N4},
    ),
    ("project", '{and: [{or: [{null: false}, {name: {neq: "zz-pa"}}]}, {null: true}]}', {N3, N4}),
    ("project", '{and: [{or: [{null: false}, {name: {neq: "zz-pa"}}]}, {null: false}]}', {W2}),
    (
        "project",
        '{and: [{or: [{null: false}, {name: {neq: "zz-pa"}}]}, {or: [{}, {name: {eq: "zz-pa"}}]}]}',
        set(),
    ),
    (
        "project",
        '{and: [{or: [{null: false}, {name: {neq: "zz-pa"}}]}], or: [{}, {name: {eq: "zz-pa"}}]}',
        set(),
    ),
    ("project", '{and: [{or: [{null: false}, {null: true, name: {eq: "zz-pa"}}]}]}', {W1, N3, N4}),
    ("project", '{and: [{and: [{or: [{null: false}, {name: {neq: "zz-pa"}}]}]}]}', {W2}),
    # the object's own `or` contributes no flag from a bare `{null: false}`: LISTS reads it as the missing-relation alternative, sibling keys or not
    ("project", '{or: [{null: false}, {name: {neq: "zz-pa"}}], name: {neq: "zz-pb"}}', {N3, N4}),
    (
        "project",
        '{and: [{name: {neq: "zz-pb"}}], or: [{null: false}, {name: {neq: "zz-pa"}}]}',
        {N3, N4},
    ),
    (
        "project",
        '{and: [{or: [{null: false}, {name: {neq: "zz-pa"}}]}], name: {neq: "zz-pb"}}',
        set(),
    ),
    ("project", '{or: [{or: [{null: false}, {name: {neq: "zz-pa"}}]}]}', {W2}),
    (
        "project",
        '{or: [{or: [{null: false}, {name: {neq: "zz-pa"}}]}, {name: {eq: "nobody"}}]}',
        {W2, N3, N4},
    ),
]


@pytest.fixture(scope="module")
def rclient(tmp_path_factory):
    """A second client over `_RELATION_OR_CORPUS`. ``reload=True`` because `fclient` is alive for
    the rest of this module: a second lifespan on the same app object would overwrite its state
    (see `client_for`)."""
    settings = build_corpus(tmp_path_factory.mktemp("linear-relation-or"), _RELATION_OR_CORPUS)
    with client_for(settings, reload=True) as c:
        c.__dict__["_admin"] = settings.admin_token
        yield c


def test_relation_or_reads_a_branch_as_linear(rclient):
    """An `or` on a nullable relation is not the union of its branches each read as an object:
    the keys of one branch are alternatives, a branch that says nothing about the related row
    beside one that does adds the issues without the relation, and every branch carrying
    `null: true` is those issues alone. The full rule is on `_sub_filter`; each cell is one
    measured answer, the mapping to the fixture above `_RELATION_OR_CORPUS`."""
    cells = {f"{r}: {literal}": sorted(expected) for r, literal, expected in _RELATION_OR_CELLS}
    assert {
        f"{r}: {literal}": ids(rclient, "{%s: %s}" % (r, literal))
        for r, literal, _ in _RELATION_OR_CELLS
    } == cells


def test_relation_negative_operators_do_not_shadow_the_labels_polarity_rule():
    """`_carries_negative` and `_derived_in` share `_RELATION_NEGATIVE_OPS`, the three operators that
    keep the issues without the relation; the labels polarity rule (`_reads_as_negation`) has its
    own, wider `_NEGATIVE_OPS`. A rebinding of the latter would silently drop `notContains` and its
    three siblings from the labels rule #116 measured, so pin both."""
    assert linear_filters._reads_as_negation({"name": {"notContains": "x"}}) is True
    assert linear_filters._reads_as_negation({"name": {"neq": "x"}}) is True
    assert linear_filters._reads_as_negation({"name": {"eq": "x"}}) is False
    assert set(linear_filters._RELATION_NEGATIVE_OPS) < set(linear_filters._NEGATIVE_OPS)


def test_unmapped_nested_field_is_refused_even_with_no_operator():
    """A nested field this module does not map is refused, not dropped, so the SDL and the compiler
    cannot drift apart unnoticed; an empty comparator (`{}`) or a place inside an `or` does not
    get it past the check."""
    for flt in (
        {"project": {"bogus": {}}},
        {"project": {"or": [{"bogus": {}}]}},
        {"project": {"or": [{"bogus": {}}, {"name": {"eq": "x"}}]}},
        {"assignee": {"and": [{"bogus": {"eq": "x"}}]}},
    ):
        with pytest.raises(GraphQLError, match="unsupported nested filter field 'bogus'"):
            linear_filters._issue_filter(None, flt)


def test_issue_filter_or_reads_the_keys_of_one_branch_as_alternatives(fclient):
    """Linear's `or` ORs the keys of one branch on `IssueFilter` too: `{or: [{number: {eq: 1},
    title: {eq: T2}}]}` answered the issue numbered 1 and the one titled T2, `{and: [{number: {eq:
    1}, title: {eq: T2}}]}` none (measured 2026-09-04; `priority` stands in for `number`, which
    `IssueFilter` here does not carry). Outside an `or` the same two keys AND, as before."""
    two_keys = '{title: {eq: "Alpha gateway"}, priority: {eq: 2}}'
    assert ids(fclient, "{or: [%s]}" % two_keys) == sorted({L2, L1})
    assert ids(fclient, "{and: [%s]}" % two_keys) == []
    assert ids(fclient, two_keys) == []
    assert ids(fclient, '{or: [{title: {eq: "Alpha gateway"}, priority: {eq: 99}}]}') == [L2]


# --- response-shape assertions (were tests/test_fidelity.py) --------------------------------


def _linear_client(tmp_path):
    """``with _linear_client(p) as (client, settings):`` over LINEAR_CORPUS."""
    return corpus_client(tmp_path, LINEAR_CORPUS)


# --- schema drift against api.linear.app (issue #101) --------------------------------------------
# Every type below is what introspection of https://api.linear.app/graphql reported, the oldest on
# 2026-09-03, copied here as literals so the suite runs offline; a literal moves when a later
# measurement finds the vendor has moved. `IntegrationService` is the 2026-10-27 introspection,
# the date its description in `backlot/graphql/linear.graphql` carries.
# `backlot diff --source linear` is the live re-measurement and
# `backlot/fidelity/baseline/linear.json` the record of the gaps it accepts.
# Most entries were a `breaking` finding in #101: Backlot declared the same name at a different
# type, or declared a name the vendor does not have. The behaviour tests after the tables pin that
# the resolvers PRODUCE the corrected types -- a changed declaration on its own is not a fix.

# field path -> the type Linear declares it at
LINEAR_TYPES = {
    # date comparators take DateTimeOrDuration, not DateTime
    **{
        f"DateComparator.{op}": "DateTimeOrDuration"
        for op in ("eq", "neq", "lt", "lte", "gt", "gte")
    },
    **{
        f"NullableDateComparator.{op}": "DateTimeOrDuration"
        for op in ("eq", "neq", "lt", "lte", "gt", "gte")
    },
    # enums that were served as a bare String
    "ExternalEntityInfo.id": "String!",
    "ExternalEntityInfo.service": "ExternalSyncService!",
    "Issue.integrationSourceType": "IntegrationService",
    "IssueSharedAccess.disallowedIssueFields": "[IssueSharedAccessDisallowedField!]!",
    "Project.frequencyResolution": "FrequencyResolutionType!",
    "Project.health": "ProjectUpdateHealthType",
    "Project.startDateResolution": "DateResolutionType",
    "Project.targetDateResolution": "DateResolutionType",
    "Project.updateRemindersDay": "Day",
    "ReleaseNote.generationStatus": "ReleaseNoteGenerationStatus",
    "Team.visibility": "TeamVisibility!",
    # counts typed Int!, not Float!
    "Issue.customerTicketCount": "Int!",
    "IssueSharedAccess.sharedWithCount": "Int!",
    "Project.priority": "Int!",
    "Release.issueCount": "Int!",
    "ReleaseNote.releaseCount": "Int!",
    "Team.issueCount": "Int!",
    "Team.ledInitiativeCount": "Int!",
    "User.createdIssueCount": "Int!",
    # filter and sort inputs at the vendor's comparator type
    "AttachmentFilter.sourceType": "SourceTypeComparator",
    "IssueFilter.creator": "NullableUserFilter",
    "IssueFilter.dueDate": "NullableTimelessDateComparator",
    "IssueFilter.estimate": "EstimateComparator",
    "IssueFilter.id": "IssueIDComparator",
    "NullableProjectFilter.id": "EntityIdentifierIDComparator",
    "UserSortInput.name": "UserNameSort",
    # selected by `@linear/sdk`'s own Team and Project fragments
    "Team.initiativesEnabled": "Boolean!",
    "Team.viewerCanJoin": "Boolean!",
    "Team.viewerCanLeave": "Boolean!",
    "Team.viewerMembership": "TeamMembership",
    "Project.resourceCount": "Int!",
    # non-null where Backlot was nullable
    "Issue.sharedAccess": "IssueSharedAccess!",
    "Release.pipeline": "ReleasePipeline!",
    "Release.stage": "ReleaseStage!",
    "ReleaseNote.pipeline": "ReleasePipeline!",
}
# surface Backlot declared that the vendor has no member for
LINEAR_HAS_NO_FIELD = [
    "IssueFilter.branchName",
    "ReleaseFilter.slugId",
    "UserSortInput.createdAt",
    "UserSortInput.updatedAt",
]
# enum name -> its members, in introspection order
LINEAR_ENUMS = {
    "ExternalSyncService": ["jira", "github", "slack"],
    "IntegrationService": [
        "airbyte",
        "datadog",
        "discord",
        "figma",
        "figmaPlugin",
        "front",
        "github",
        "gong",
        "githubEnterpriseServer",
        "githubCommit",
        "githubImport",
        "githubPersonal",
        "githubCodeAccessPersonal",
        "gitlab",
        "gitlabPersonal",
        "origin",
        "googleCalendarPersonal",
        "googleSheets",
        "intercom",
        "jira",
        "jiraPersonal",
        "launchDarkly",
        "launchDarklyPersonal",
        "loom",
        "notion",
        "opsgenie",
        "pagerDuty",
        "salesforce",
        "slack",
        "slackAsks",
        "asksWeb",
        "slackCustomViewNotifications",
        "slackOrgProjectUpdatesPost",
        "slackOrgInitiativeUpdatesPost",
        "slackPersonal",
        "slackPost",
        "slackProjectPost",
        "slackProjectUpdatesPost",
        "slackInitiativePost",
        "sentry",
        "zendesk",
        "email",
        "mcpServerPersonal",
        "mcpServer",
        "microsoftTeams",
        "microsoftPersonal",
        "microsoftTeamsProjectPost",
    ],  # fmt: skip
    "IssueSharedAccessDisallowedField": ["projectId", "teamId", "cycleId", "projectMilestoneId"],
    "FrequencyResolutionType": ["daily", "weekly"],
    "ProjectUpdateHealthType": ["onTrack", "atRisk", "offTrack"],
    "DateResolutionType": ["month", "quarter", "halfYear", "year"],
    "Day": ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
    "ReleaseNoteGenerationStatus": ["pending", "completed"],
    "TeamVisibility": ["public", "restricted", "private"],
}


@pytest.fixture(scope="module")
def served_schema():
    """The schema Backlot serves, built from the SDL exactly as `backlot diff` builds it."""
    return backlot_schema("linear")


@pytest.mark.parametrize("path,expected", sorted(LINEAR_TYPES.items()))
def test_declared_type_is_the_one_linear_declares(served_schema, path, expected):
    type_name, field = path.split(".")
    assert str(served_schema.type_map[type_name].fields[field].type) == expected


@pytest.mark.parametrize("path", LINEAR_HAS_NO_FIELD)
def test_a_field_linear_does_not_have_is_not_declared(served_schema, path):
    type_name, field = path.split(".")
    assert field not in served_schema.type_map[type_name].fields


def test_attachments_declares_no_url_argument(served_schema):
    assert "url" not in served_schema.type_map["Issue"].fields["attachments"].args


@pytest.mark.parametrize("name,members", sorted(LINEAR_ENUMS.items()))
def test_enum_carries_linears_full_member_list(served_schema, name, members):
    """A partial enum would reject a value the real API serves; `backlot diff` compares the two
    lists member for member, so the declared list is the vendor's whole one."""
    t = served_schema.type_map[name]
    assert is_enum_type(t)
    assert list(t.values) == members


def test_user_name_sort_defaults_nulls_to_last_as_linear_does(fclient):
    fields = post(
        fclient, '{ __type(name: "UserNameSort") { inputFields { name defaultValue } } }'
    ).json()["data"]["__type"]["inputFields"]
    assert {f["name"]: f["defaultValue"] for f in fields} == {"nulls": "last", "order": None}


# --- the resolvers produce the corrected types -------------------------------------------------


def test_counts_are_served_as_json_integers(fclient):
    """`Int!` on the wire is `0`, not `0.0` -- what a real issue answers for `customerTicketCount`
    (measured 2026-09-03). Eight of the nine `Int!` counts are reachable from an issue;
    `ReleaseNote.releaseCount` is not, because no corpus produces a release note."""
    r = post(
        fclient,
        """{ issue(id: "ENG-2") {
            customerTicketCount
            sharedAccess { sharedWithCount }
            project { priority resourceCount }
            team { issueCount ledInitiativeCount }
            creator { createdIssueCount }
            releases { nodes { issueCount } }
        } }""",
    ).json()
    assert "errors" not in r, r.get("errors")
    d = r["data"]["issue"]
    counts = {
        "Issue.customerTicketCount": d["customerTicketCount"],
        "IssueSharedAccess.sharedWithCount": d["sharedAccess"]["sharedWithCount"],
        "Project.priority": d["project"]["priority"],
        "Project.resourceCount": d["project"]["resourceCount"],
        "Team.issueCount": d["team"]["issueCount"],
        "Team.ledInitiativeCount": d["team"]["ledInitiativeCount"],
        "User.createdIssueCount": d["creator"]["createdIssueCount"],
        "Release.issueCount": d["releases"]["nodes"][0]["issueCount"],
    }
    not_int = {k: v for k, v in counts.items() if type(v) is not int}
    assert not not_int, not_int


def test_fields_the_sdk_fragments_select_resolve_to_what_backlot_serves(fclient):
    """All four are non-null, so a declaration with nothing behind it voids the whole result. A
    real team answers `initiativesEnabled: false` (measured 2026-09-07); the admin token is in no
    team, so it may join this one and has none to leave; Backlot serves no project resources, so
    `resourceCount` is a count rather than a stand-in."""
    r = post(
        fclient,
        '{ issue(id: "ENG-2") { team { initiativesEnabled viewerCanJoin viewerCanLeave }'
        " project { resourceCount } } }",
    ).json()
    assert "errors" not in r, r.get("errors")
    d = r["data"]["issue"]
    team = d["team"]
    served = (
        team["initiativesEnabled"],
        team["viewerCanJoin"],
        team["viewerCanLeave"],
        d["project"]["resourceCount"],
    )
    assert served == (False, True, False, 0)


_AGENT_SESSIONS = "query($v: Boolean) { viewer { supportsAgentSessions(forCurrentUser: $v) } }"


@pytest.mark.parametrize(
    "query,variables,expected",
    [
        (
            "{ viewer { supportsAgentSessions } }",
            None,
            {"viewer": {"supportsAgentSessions": False}},
        ),
        (
            "{ viewer { f: supportsAgentSessions(forCurrentUser: false)"
            " t: supportsAgentSessions(forCurrentUser: true) } }",
            None,
            {"viewer": {"f": False, "t": False}},
        ),
        (_AGENT_SESSIONS, {}, {"viewer": {"supportsAgentSessions": False}}),
        (_AGENT_SESSIONS, {"v": False}, {"viewer": {"supportsAgentSessions": False}}),
        (_AGENT_SESSIONS, {"v": True}, {"viewer": {"supportsAgentSessions": False}}),
    ],
    ids=["absent", "literals", "variable omitted", "variable false", "variable true"],
)
def test_supports_agent_sessions_answers_false_under_each_for_current_user(
    fclient, query, variables, expected
):
    """Each row is api.linear.app's answer to the same document and variables, measured when the
    comment in ``_user`` says."""
    r = post(fclient, query, variables)
    assert (r.status_code, r.json()) == (200, {"data": expected})


def test_supports_agent_sessions_declares_linears_default(fclient):
    """Introspection rather than an answer, which is ``false`` under either value; the comment in
    ``_user`` has the vendor's declaration."""
    fields = post(
        fclient,
        '{ __type(name: "User") { fields { name args { name defaultValue'
        " type { kind ofType { name } } } } } }",
    ).json()["data"]["__type"]["fields"]
    (field,) = (f for f in fields if f["name"] == "supportsAgentSessions")
    assert field["args"] == [
        {
            "name": "forCurrentUser",
            "defaultValue": "false",
            "type": {"kind": "NON_NULL", "ofType": {"name": "Boolean"}},
        }
    ]


_MEMBERSHIP = "id createdAt updatedAt archivedAt owner sortOrder user { id } team { id }"


@pytest.mark.parametrize("ava_groups", [[], ["design"]], ids=["one team", "two teams"])
def test_linear_team_membership_is_the_teams_group(tmp_path, ava_groups):
    """The measurements above `_team_groups`. A team's members are its group's, which the roster
    states: bo is in engineering without a single issue, and ava, assigned a design issue, is no
    member of design until the roster puts her in it. ava's membership in a team she is in is the
    one `teamMembership(id:)` finds; in the team she only sees, she has none and may join it."""
    sealed = {
        "source_type": "linear",
        "doc_id": "lin-sealed",
        "team": "blackops",
        "group": "people",
        "title": "Sealed",
        "content": "Sealed.",
        "author_email": "hana@acme.com",
        "readers": ["hana@acme.com"],
        "identifier": "BLA-1",
    }
    design = {
        "source_type": "linear",
        "doc_id": "lin-design",
        "team": "design",
        "group": "design",
        "title": "Select states",
        "content": "Focus rings.",
        "author_email": "mia@acme.com",
        "visibility": "public",
        "identifier": "DES-1",
        "assignee": "ava@acme.com",
    }
    roster = {
        "departments": {
            "Engineering": [
                {"name": "Ava Chen", "email": "ava@acme.com", "groups": ava_groups},
                {"name": "Bo Ryu", "email": "bo@acme.com"},
            ],
            "Design": [{"name": "Mia Park", "email": "mia@acme.com"}],
            "People": [{"name": "Hana Lee", "email": "hana@acme.com"}],
        }
    }
    records = [*LINEAR_CORPUS, sealed, design]
    with corpus_client(tmp_path, records, roster=roster) as (client, settings):
        users = client.get("/_meta/users").json()["users"]
        h = {"Authorization": next(u["token"] for u in users if u["email"] == "ava@acme.com")}
        q = (
            "{ viewer { id createdAt } teams { nodes { key id createdAt viewerCanJoin"
            " viewerCanLeave members { nodes { email createdAt id } }"
            " viewerMembership { %s } } } }" % _MEMBERSHIP
        )
        r = gql(client, q, h).json()
        assert "errors" not in r, r["errors"]
        d = r["data"]
        teams = {t["key"]: t for t in d["teams"]["nodes"]}
        assert list(teams) == ["DES", "ENG"]  # blackops is hidden from ava
        for team in teams.values():
            order = [(m["createdAt"], m["id"]) for m in team["members"]["nodes"]]
            assert order == sorted(order, reverse=True)
        members = {k: {m["email"] for m in t["members"]["nodes"]} for k, t in teams.items()}
        assert members["ENG"] == {"ava@acme.com", "bo@acme.com"}
        in_design = bool(ava_groups)
        assert members["DES"] == {"mia@acme.com"} | ({"ava@acme.com"} if in_design else set())
        mine = ["DES", "ENG"] if in_design else ["ENG"]
        for key, team in teams.items():
            m = team["viewerMembership"]
            assert (team["viewerCanJoin"], team["viewerCanLeave"]) == (
                key not in mine,
                key in mine and len(mine) > 1,
            )
            if key not in mine:
                assert m is None
                other = synth.linear_team_membership_id("design", d["viewer"]["id"])
                miss = gql(client, "{ teamMembership(id: %s) { id } }" % lit(other), h).json()
                assert miss["errors"][0]["message"] == "Entity not found: TeamMembership"
                continue
            assert (m["user"]["id"], m["team"]["id"]) == (d["viewer"]["id"], team["id"])
            assert (m["owner"], m["archivedAt"], m["sortOrder"]) == (False, None, mine.index(key))
            assert (
                m["createdAt"] == m["updatedAt"] == max(team["createdAt"], d["viewer"]["createdAt"])
            )
            by_id = gql(
                client, "{ teamMembership(id: %s) { %s } }" % (lit(m["id"]), _MEMBERSHIP), h
            )
            assert by_id.json()["data"]["teamMembership"] == m
        # Everyone's memberships, by team, by user and in all, newest first, each found by id;
        # hana's in blackops stays out of sight with its team.
        q = (
            "{ teams { nodes { key memberships { nodes { %s } } } }"
            " teamMemberships { nodes { %s } }"
            " users { nodes { email teamMemberships { nodes { id } } teams { nodes { key } }"
            ' eng: teams(filter: {key: {eq: "ENG"}}) { nodes { key } } } } }'
        ) % (_MEMBERSHIP, _MEMBERSHIP)
        d = gql(client, q, h).json()["data"]
        by_team = {t["key"]: t["memberships"]["nodes"] for t in d["teams"]["nodes"]}
        everyone = [m for ms in by_team.values() for m in ms]
        assert len(everyone) == len(members["ENG"]) + len(members["DES"])
        for ms in [*by_team.values(), d["teamMemberships"]["nodes"]]:
            order = [(m["createdAt"], m["id"]) for m in ms]
            assert order == sorted(order, reverse=True)
        assert sorted(m["id"] for m in d["teamMemberships"]["nodes"]) == sorted(
            m["id"] for m in everyone
        )
        for m in everyone:
            for spelt in (m["id"], m["id"].upper()):
                q = "{ teamMembership(id: %s) { %s } }" % (lit(spelt), _MEMBERSHIP)
                assert gql(client, q, h).json()["data"]["teamMembership"] == m
        users = {u["email"]: u for u in d["users"]["nodes"]}
        assert [t["key"] for t in users["ava@acme.com"]["teams"]["nodes"]] == mine
        assert users["ava@acme.com"]["eng"]["nodes"] == [{"key": "ENG"}]
        assert users["hana@acme.com"]["teams"]["nodes"] == []
        admin = {"Authorization": settings.admin_token}
        q = '{ users(filter: {email: {eq: "hana@acme.com"}}) { nodes { teams { nodes { key } } } } }'
        (hana,) = gql(client, q, admin).json()["data"]["users"]["nodes"]
        assert hana["teams"]["nodes"] == [{"key": "BLA"}]
        assert {m["id"] for m in users["ava@acme.com"]["teamMemberships"]["nodes"]} == {
            teams[k]["viewerMembership"]["id"] for k in mine
        }
        q = '{ team(id: "ENG") { membership(userId: %s) { id user { email } } } }'
        for email, member in (("bo@acme.com", True), ("mia@acme.com", False)):
            for user_id in (synth.linear_user_id(email), synth.linear_user_id(email).upper()):
                got = gql(client, q % lit(user_id), h).json()["data"]
                assert (got["team"]["membership"] or {}).get("user") == (
                    {"email": email} if member else None
                )
        refused = gql(client, q % lit("bo@acme.com"), h).json()
        assert refused["data"] == {"team": {"membership": None}}
        assert refused["errors"][0]["message"] == "Internal server error"
        # A lone surrogate is answered as any other unknown id; the raw body carries it as JSON's
        # `\ud800` escape, which a client encoding the string as UTF-8 first cannot send.
        for x in (synth.linear_id("nope"), "a\ud800b"):
            body = {"query": "query($x: String!) { teamMembership(id: $x) { id } }"}
            body["variables"] = {"x": x}
            miss = client.post(
                "/linear/graphql",
                content=json.dumps(body),
                headers={**h, "Content-Type": "application/json"},
            )
            assert miss.status_code == 200
            assert miss.json()["errors"][0]["message"] == "Entity not found: TeamMembership"


def test_enum_fields_serve_a_member_of_the_vendors_enum(fclient):
    """`frequencyResolution` used to be served as "week", which is not a `FrequencyResolutionType`
    member; under the enum declaration that is a serialization error, not a string."""
    r = post(
        fclient,
        """{ issue(id: "ENG-2") {
            integrationSourceType
            team { visibility }
            project { frequencyResolution health startDateResolution targetDateResolution
                      updateRemindersDay }
            sharedAccess { disallowedIssueFields }
        } }""",
    ).json()
    assert "errors" not in r, r.get("errors")
    d = r["data"]["issue"]
    assert d["team"]["visibility"] == "public"
    assert d["project"]["frequencyResolution"] in LINEAR_ENUMS["FrequencyResolutionType"]
    # nullable enums with nothing behind them stay null rather than inventing a member
    assert d["integrationSourceType"] is None
    assert d["project"]["health"] is None
    assert d["project"]["updateRemindersDay"] is None
    assert d["sharedAccess"]["disallowedIssueFields"] == []


def test_release_stage_and_pipeline_are_non_null_and_stable(fclient):
    """`Release.stage` / `Release.pipeline` are `ReleaseStage!` / `ReleasePipeline!` upstream; a
    null would void the whole `release` result under that declaration. Each is an id-only stub
    keyed off the release name, so the two ids differ and the by-id root agrees with the connection."""
    q = '{ issue(id: "ENG-2") { releases { nodes { id stage { id } pipeline { id } } } } }'
    node = post(fclient, q).json()["data"]["issue"]["releases"]["nodes"][0]
    assert node["stage"]["id"] and node["pipeline"]["id"]
    assert node["stage"]["id"] != node["pipeline"]["id"] != node["id"]
    again = post(
        fclient, "{ release(id: %s) { stage { id } pipeline { id } } }" % lit(node["id"])
    ).json()["data"]["release"]
    assert again == {"stage": node["stage"], "pipeline": node["pipeline"]}


@pytest.mark.parametrize(
    "query,message",
    [
        (
            '{ issues(filter: {branchName: {eq: "x"}}) { nodes { id } } }',
            'Field "branchName" is not defined by type "IssueFilter".',
        ),
        (
            '{ issue(id: "ENG-2") { releases(filter: {slugId: {eq: "x"}}) { nodes { id } } } }',
            'Field "slugId" is not defined by type "ReleaseFilter".',
        ),
        (
            "{ users(sort: [{createdAt: {order: Descending}}]) { nodes { id } } }",
            'Field "createdAt" is not defined by type "UserSortInput".',
        ),
        (
            "{ users(sort: [{updatedAt: {order: Descending}}]) { nodes { id } } }",
            'Field "updatedAt" is not defined by type "UserSortInput".',
        ),
    ],
    ids=[
        "IssueFilter.branchName",
        "ReleaseFilter.slugId",
        "UserSortInput.createdAt",
        "UserSortInput.updatedAt",
    ],
)
def test_surface_linear_rejects_is_a_400_here_too(fclient, query, message):
    """Measured 2026-09-03: each of these is a validation error from api.linear.app (`Field
    "branchName" is not defined by type "IssueFilter"`), so accepting it here let a filter compile
    that no client written against Linear could send."""
    r = post(fclient, query)
    assert r.status_code == 400 and "data" not in r.json()
    assert message in [e["message"] for e in r.json()["errors"]]


def test_users_sort_by_name_is_applied_in_both_directions(fclient):
    """`users(sort:)` was declared and ignored. A sort that is accepted and dropped answers a client
    asking for Z->A with A->Z: a wrong result, not an error."""
    q = "{ users(sort: [{name: {order: %s}}]) { nodes { name } } }"
    asc = [n["name"] for n in post(fclient, q % "Ascending").json()["data"]["users"]["nodes"]]
    desc = [n["name"] for n in post(fclient, q % "Descending").json()["data"]["users"]["nodes"]]
    assert len(asc) > 1
    assert asc == sorted(asc)
    assert desc == sorted(asc, reverse=True)


# --- the date operands: DateTimeOrDuration / TimelessDateOrDuration -----------------------------


@pytest.fixture
def now_is_2026_03_01_noon(monkeypatch):
    """Pins the instant a duration is relative to. DES-1 was created at exactly 2026-03-01T00:00Z,
    so `-PT12H` lands on its boundary and `-P1M` between ENG-2 (Feb 1) and DES-1."""
    pinned = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(linear_filters, "_now", lambda: pinned)
    return pinned


def test_duration_operands_are_relative_to_now(fclient, now_is_2026_03_01_noon):
    """Linear: a duration "is added to the current date to create the represented date (e.g
    '-P2W1D' represents the date that was two weeks and 1 day ago)"."""
    assert ids(fclient, '{createdAt: {gt: "-P1M"}}') == ["DES-1"]  # Feb 1 12:00 < Mar 1
    assert ids(fclient, '{createdAt: {lt: "-P1M"}}') == ["ENG-1", "ENG-2"]
    assert ids(fclient, '{createdAt: {gte: "-P2M1D"}}') == ALL  # Dec 31 12:00
    assert ids(fclient, '{createdAt: {gte: "-PT12H"}}') == ["DES-1"]  # exactly its createdAt
    assert ids(fclient, '{createdAt: {gt: "-PT12H"}}') == []
    assert ids(fclient, '{createdAt: {lt: "P1D"}}') == ALL  # a positive duration is the future
    assert ids(fclient, '{createdAt: {gte: "-P1Y2M3W4DT5H6M7S"}}') == ALL  # every component


def test_year_and_month_shortcuts_are_the_first_instant(fclient):
    """Linear: "Accepts shortcuts like `2021` to represent midnight Fri Jan 01 2021"; `2026-02`
    validates too (measured). ENG-1 sits exactly on 2026-01-01T00:00Z."""
    assert ids(fclient, '{createdAt: {gte: "2026"}}') == ALL
    assert ids(fclient, '{createdAt: {gt: "2026"}}') == ["DES-1", "ENG-2"]
    assert ids(fclient, '{createdAt: {gte: "2026-02"}}') == ["DES-1", "ENG-2"]
    assert ids(fclient, '{createdAt: {lt: "2026-02"}}') == ["ENG-1"]


def test_date_comparators_take_in_and_nin(fclient):
    """`in` / `nin` were gaps on both date comparators; the operands go through the same scalar."""
    assert ids(fclient, '{createdAt: {in: ["2026-01-01T00:00:00Z", "2026-03-01"]}}') == [
        "DES-1",
        "ENG-1",
    ]
    assert ids(fclient, '{createdAt: {nin: ["2026-01-01T00:00:00Z"]}}') == ["DES-1", "ENG-2"]
    assert ids(fclient, '{completedAt: {nin: ["2026"]}}') == ALL  # every completed_ts is NULL
    assert ids(fclient, '{completedAt: {neq: "2026"}}') == []
    # `null` on a date comparator carries a Boolean, which must not go through the date scalar.
    assert ids(fclient, "{completedAt: {null: true}}") == ALL
    assert ids(fclient, "{completedAt: {null: false}}") == []


def test_due_date_is_compared_as_a_timeless_date(fclient, now_is_2026_03_01_noon):
    """`IssueFilter.dueDate` is a `NullableTimelessDateComparator` over `TimelessDateOrDuration`:
    a full timestamp is read down to its UTC day, a duration is relative to today, and the column
    is the bare YYYY-MM-DD. Each RULE below is one api.linear.app answered on 2026-09-03 for an
    issue due 2026-03-15 (the module comment in linear_filters.py lists the operands); the corpus
    here is ENG-1 due 2026-03-15, ENG-2 2026-04-01, DES-1 with no due date."""
    assert ids(fclient, '{dueDate: {eq: "2026-03-15"}}') == ["ENG-1"]
    assert ids(fclient, '{dueDate: {eq: "2026-03-15T23:59:59Z"}}') == ["ENG-1"]
    assert ids(fclient, '{dueDate: {eq: "2026-03-16T02:00:00+09:00"}}') == ["ENG-1"]  # 15th UTC
    assert ids(fclient, '{dueDate: {eq: "2026-03-15T23:59:59-05:00"}}') == []  # 16th UTC
    assert ids(fclient, '{dueDate: {gte: "2026-03-15T12:00:00Z"}}') == ["ENG-1", "ENG-2"]
    assert ids(fclient, '{dueDate: {gt: "2026-03-15T00:00:00Z"}}') == ["ENG-2"]
    assert ids(fclient, '{dueDate: {neq: "2026-03-15"}}') == ["ENG-2"]  # null row dropped
    assert ids(fclient, '{dueDate: {lt: "2026-04-01"}}') == ["ENG-1"]
    assert ids(fclient, '{dueDate: {lte: "2026-04-01"}}') == ["ENG-1", "ENG-2"]
    assert ids(fclient, '{dueDate: {gt: "2026-03-15"}}') == ["ENG-2"]
    assert ids(fclient, '{dueDate: {gte: "2026-03"}}') == ["ENG-1", "ENG-2"]  # month shortcut
    assert ids(fclient, '{dueDate: {in: ["2026-03-15", "2026-04-01"]}}') == ["ENG-1", "ENG-2"]
    assert ids(fclient, '{dueDate: {nin: ["2026-03-15"]}}') == ["DES-1", "ENG-2"]  # null kept
    assert ids(fclient, "{dueDate: {null: true}}") == ["DES-1"]
    assert ids(fclient, "{dueDate: {null: false}}") == ["ENG-1", "ENG-2"]
    assert ids(fclient, '{dueDate: {eq: "P14D"}}') == ["ENG-1"]  # 2026-03-01 + 14 days
    assert ids(fclient, '{dueDate: {gte: "P1M"}}') == ["ENG-2"]  # 2026-04-01, inclusive
    assert ids(fclient, '{dueDate: {gt: "P1M"}}') == []
    r = post(fclient, '{ issues(filter: {dueDate: {eq: "nope"}}) { nodes { id } } }')
    assert r.status_code == 400
    assert r.json()["errors"][0]["message"].startswith(
        'Expected value of type "TimelessDateOrDuration", found "nope"; '
    )


# --- the retyped filter inputs ----------------------------------------------------------------


def test_estimate_comparator_nests_and_or(fclient):
    """`EstimateComparator` is the nullable number comparator plus `and` / `or` over plain ones."""
    assert ids(fclient, "{estimate: {or: [{eq: 1}, {eq: 5}]}}") == ["ENG-1", "ENG-2"]
    assert ids(fclient, "{estimate: {and: [{gte: 1}, {lt: 5}]}}") == ["ENG-1"]
    assert ids(fclient, "{estimate: {or: [{null: true}, {gt: 4}]}}") == ["DES-1", "ENG-2"]
    assert ids(fclient, "{estimate: {and: []}}") == ALL  # an empty compound constrains nothing
    assert ids(fclient, "{estimate: {lte: 1, or: [{eq: 1}, {eq: 5}]}}") == ["ENG-1"]  # siblings AND


def test_creator_filter_is_nullable_as_linears(fclient):
    """`IssueFilter.creator` is a `NullableUserFilter` upstream. Every issue here names its creator
    (`author_email` is NOT NULL), so `null: true` is honestly empty and `null: false` everything."""
    assert ids(fclient, "{creator: {null: true}}") == []
    assert ids(fclient, "{creator: {null: false}}") == ALL
    assert ids(fclient, '{creator: {null: false, email: {eq: "mia@acme.com"}}}') == ["DES-1"]


def test_issue_id_and_project_id_comparators_keep_their_operators(fclient):
    """The renamed comparators (`IssueIDComparator`, `EntityIdentifierIDComparator`) carry the same
    four operators; an issue id still resolves either key space, a project id the served UUID."""
    assert ids(fclient, '{id: {in: ["ENG-1", "DES-1"]}}') == ["DES-1", "ENG-1"]
    assert ids(fclient, '{id: {nin: ["ENG-1"]}}') == ["DES-1", "ENG-2"]
    pid = lit(synth.linear_project_id("runtime"))
    assert ids(fclient, "{project: {id: {eq: %s}}}" % pid) == ["ENG-1", "ENG-2"]
    assert ids(fclient, "{project: {id: {neq: %s}}}" % pid) == ["DES-1"]
    # A project value of any shape that matches nothing is an empty page, as measured.
    assert ids(fclient, '{project: {id: {eq: "PROJ-1"}}}') == []
    assert ids(fclient, '{project: {id: {eq: "not-a-uuid"}}}') == []
    assert ids(fclient, '{project: {id: {eq: "11111111-1111-1111-8111-111111111111"}}}') == []


@pytest.mark.parametrize(
    "value",
    [
        "00000000-0000-0000-0000-000000000000",
        "11111111-1111-1111-1111-111111111111",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
    ],
    ids=["nil", "variant-1", "variant-f"],
)
@pytest.mark.parametrize(
    "field", ["id", "project: {id"], ids=["IssueIDComparator", "EntityIdentifierIDComparator"]
)
def test_a_uuid_without_an_rfc4122_variant_is_an_argument_validation_error(fclient, field, value):
    """Measured 2026-09-03 on both comparators: these three answer `Argument Validation Error`
    (code INVALID_INPUT) while v1, v4 and v5 UUIDs with a variant of 8-b are looked up -- the variant
    nibble is what Linear's validator checks, not the version."""
    close = "}" if field == "id" else "}}"
    r = post(
        fclient,
        '{ issues(filter: {%s: {eq: "%s"}%s) { nodes { identifier } } }' % (field, value, close),
    )
    assert r.status_code == 200
    err = r.json()["errors"][0]
    assert err["message"] == "Argument Validation Error"
    assert err["extensions"]["code"] == "INVALID_INPUT"


def test_a_malformed_project_id_is_refused_however_deep_the_filter_nests_it(fclient):
    """`ProjectFilter` takes `and` / `or`, and `_sub_filter` follows them to the same lookup, so a
    UUID refused at `{project: {id: …}}` has to be refused at `{project: {and: [{id: …}]}}` too --
    this branch looked the nested one up and answered an empty page. The issue-id side already
    recursed (`{and: [{id: {eq: "not-a-uuid"}}]}` is refused), so the two sides now agree. The last
    shape pins that `null: true` does not swallow the refusal: `_sub_filter` returns before reading
    the `id`, and the refusal has to happen anyway because `_refuse_malformed_project_ids` runs
    first."""
    nil = "00000000-0000-0000-0000-000000000000"
    for shape in (
        '{project: {and: [{id: {eq: "%s"}}]}}',
        '{project: {or: [{name: {eq: "runtime"}}, {id: {in: ["%s"]}}]}}',
        '{project: {and: [{or: [{id: {neq: "%s"}}]}]}}',
        '{project: {null: true, id: {eq: "%s"}}}',
    ):
        r = post(fclient, "{ issues(filter: %s) { nodes { identifier } } }" % (shape % nil))
        assert r.status_code == 200, shape
        err = r.json()["errors"][0]
        assert err["message"] == "Argument Validation Error", shape
        assert err["extensions"]["code"] == "INVALID_INPUT", shape
    # a well-formed unknown stays an empty page at any depth, as at the top level
    well_formed = "11111111-1111-1111-8111-111111111111"
    assert ids(fclient, '{project: {and: [{id: {eq: "%s"}}]}}' % well_formed) == []
    assert ids(
        fclient, '{project: {or: [{id: {eq: "%s"}}, {name: {eq: "runtime"}}]}}' % well_formed
    ) == [
        "ENG-1",
        "ENG-2",
    ]


def test_an_issue_id_of_neither_shape_is_an_argument_validation_error(fclient):
    """Measured 2026-09-03: `IssueFilter.id: {eq: "not-a-uuid"}` answers `Argument Validation Error`
    with code INVALID_INPUT as a 200 with `errors`, while an unknown but well-shaped identifier
    answers an empty page."""
    r = post(fclient, '{ issues(filter: {id: {eq: "not-a-uuid"}}) { nodes { identifier } } }')
    assert r.status_code == 200
    err = r.json()["errors"][0]
    assert err["message"] == "Argument Validation Error"
    assert err["extensions"]["code"] == "INVALID_INPUT"
    assert ids(fclient, '{id: {eq: "ENG-999"}}') == []
    assert ids(fclient, '{id: {in: ["ENG-1", "ZZZ-1"]}}') == ["ENG-1"]
    # Linear also refuses a non-numeric suffix (`BRE-x`, measured); Backlot does so only for a value
    # that names no issue, because the corpus schema lets a served identifier carry one.
    r = post(fclient, '{ issues(filter: {id: {eq: "ENG-x"}}) { nodes { identifier } } }')
    assert r.json()["errors"][0]["message"] == "Argument Validation Error"
    r = post(
        fclient, '{ issues(filter: {id: {in: ["ENG-1", "not-a-uuid"]}}) { nodes { identifier } } }'
    )
    assert (
        r.json()["errors"][0]["message"] == "Argument Validation Error"
    )  # one bad value fails the list


def test_source_type_comparator_evaluates_every_operator(fclient):
    """`AttachmentFilter.sourceType` is a `SourceTypeComparator`: the string operators plus their
    negations, case-insensitive and accent-insensitive forms. ENG-1 carries `github` and
    `slack-équipe`."""

    def titles(filter_literal):
        q = (
            '{ issue(id: "ENG-1") { attachments(filter: %s) { nodes { title } } } }'
            % filter_literal
        )
        body = post(fclient, q).json()
        assert "errors" not in body, body.get("errors")
        return sorted(n["title"] for n in body["data"]["issue"]["attachments"]["nodes"])

    assert titles('{sourceType: {eq: "github"}}') == ["CI run"]
    assert titles('{sourceType: {notContains: "hub"}}') == ["Réunion notes"]
    assert titles('{sourceType: {notContainsIgnoreCase: "GIT"}}') == ["Réunion notes"]
    assert titles('{sourceType: {startsWithIgnoreCase: "SL"}}') == ["Réunion notes"]
    assert titles('{sourceType: {notStartsWith: "sl"}}') == ["CI run"]
    assert titles('{sourceType: {notEndsWith: "quipe"}}') == ["CI run"]
    assert titles('{sourceType: {containsIgnoreCaseAndAccent: "EQUIPE"}}') == ["Réunion notes"]
    assert titles('{sourceType: {contains: "equipe"}}') == []  # plain contains is exact
    assert titles('{sourceType: {in: ["github", "slack-équipe"]}}') == ["CI run", "Réunion notes"]


def test_attachment_string_fields_follow_linears_null_rules(fclient):
    """Measured 2026-09-03 against an attachment with no subtitle and no source: `subtitle:
    {null: true}` selects it, `nin` keeps it, and `neq` / `neqIgnoreCase` / `notContains` / `eq: ""`
    drop it; its `sourceType` is served as "unknown" and no `sourceType` predicate matches it, `nin`
    included. "Spec" is that attachment here."""

    def titles(filter_literal):
        q = (
            '{ issue(id: "ENG-1") { attachments(filter: %s) { nodes { title } } } }'
            % filter_literal
        )
        body = post(fclient, q).json()
        assert "errors" not in body, body.get("errors")
        return sorted(n["title"] for n in body["data"]["issue"]["attachments"]["nodes"])

    served = post(
        fclient, '{ issue(id: "ENG-1") { attachments { nodes { title subtitle sourceType } } } }'
    )
    spec = next(
        n for n in served.json()["data"]["issue"]["attachments"]["nodes"] if n["title"] == "Spec"
    )
    assert spec == {"title": "Spec", "subtitle": None, "sourceType": "unknown"}
    assert titles("{subtitle: {null: true}}") == ["CI run", "Spec"]
    assert titles("{subtitle: {null: false}}") == ["Réunion notes"]
    assert titles('{subtitle: {neq: "zzz"}}') == ["Réunion notes"]
    assert titles('{subtitle: {neqIgnoreCase: "zzz"}}') == ["Réunion notes"]
    assert titles('{subtitle: {nin: ["zzz"]}}') == ["CI run", "Réunion notes", "Spec"]
    assert titles('{subtitle: {eq: ""}}') == []
    assert titles('{sourceType: {neq: "zzz"}}') == ["CI run", "Réunion notes"]
    assert titles('{sourceType: {nin: ["zzz"]}}') == ["CI run", "Réunion notes"]
    assert titles('{sourceType: {notContains: "zzz"}}') == ["CI run", "Réunion notes"]
    assert titles('{sourceType: {eq: "unknown"}}') == []


# --- Linear -----------------------------------------------------------------------

LINEAR_CORPUS = [
    {
        "source_type": "linear",
        "doc_id": "lin-a",
        "team": "engineering",
        "group": "engineering",
        "title": "Batching stall",
        "content": "A 50ms stall after compaction.",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "identifier": "ENG-9",
        "state": "In Progress",
        "priority": 2,
    },
]


@pytest.fixture(scope="module")
def assigned_and_unassigned(tmp_path_factory):
    """ENG-1 assigned to raj and ENG-2 unassigned, both titled alike."""
    records = [
        {
            "source_type": "linear",
            "doc_id": "assigned",
            "team": "engineering",
            "title": "Rotate the signing keys",
            "identifier": "ENG-1",
            "content": "Keys expire in March.",
            "state": "Todo",
            "author_email": "mia@acme.com",
            "assignee": "raj@acme.com",
            "created": "2026-02-01T09:00:00Z",
        },
        {
            "source_type": "linear",
            "doc_id": "unassigned",
            "team": "engineering",
            "title": "Rotate the signing keys",
            "identifier": "ENG-2",
            "content": "Keys expire in March.",
            "state": "Todo",
            "author_email": "mia@acme.com",
            "created": "2026-02-01T09:00:00Z",
        },
    ]
    return build_corpus(tmp_path_factory.mktemp("assigned_and_unassigned"), records)


@pytest.mark.parametrize(
    "caller, unassigned",
    [
        ("mia@acme.com", "mia/eng-2-rotate-the-signing-keys"),
        # the admin token has no address, so there is no user segment
        ("admin", "eng-2-rotate-the-signing-keys"),
    ],
)
def test_an_unassigned_issue_branch_name_starts_with_the_callers_handle(
    assigned_and_unassigned, caller, unassigned
):
    """An issue with no assignee takes the caller's email local part as its branch prefix.
    An assigned issue keeps the assignee's local part in front of the same slug."""
    settings = assigned_and_unassigned
    with client_for(settings) as client:
        users = client.get("/_meta/users").json()["users"]
        if caller == "admin":
            token = settings.admin_token
        else:
            token = next(u["token"] for u in users if u["email"] == caller)
        body = gql(
            client,
            "{ issues { nodes { identifier branchName } } }",
            {"Authorization": token},
        ).json()
        assert "errors" not in body, body.get("errors")
        got = {n["identifier"]: n["branchName"] for n in body["data"]["issues"]["nodes"]}
        assert got == {"ENG-1": "raj/eng-1-rotate-the-signing-keys", "ENG-2": unassigned}


def test_an_issue_with_no_recorded_edit_is_filtered_on_the_updatedAt_it_serves(tmp_path):
    """`Issue.updatedAt` is non-null in Linear; Backlot serves an issue with no recorded edit at its
    creation time (`COALESCE(updated_ts, created_ts)`), and the sorts already read that expression.
    The filter has to read it too: against the raw column, `eq` missed the value a client had just
    read, `gte` / `lt` left the issue out of every window, and `neq` -- which compares the field
    directly since Linear's non-null `updatedAt` can never drop a row -- dropped it outright."""
    records = [
        complete(
            "linear",
            doc_id="e1",
            identifier="ENG-1",
            title="edited",
            created="2026-01-01T00:00:00Z",
            updated="2026-01-05T00:00:00Z",
        ),
        complete(
            "linear",
            doc_id="e2",
            identifier="ENG-2",
            title="never edited",
            created="2026-02-01T00:00:00Z",
        ),
    ]
    with corpus_client(tmp_path, records) as (client, settings):
        h = {"Authorization": settings.admin_token}

        def ids_for(filter_literal):
            body = gql(
                client, "{ issues(filter: %s) { nodes { identifier } } }" % filter_literal, h
            )
            return sorted(n["identifier"] for n in body.json()["data"]["issues"]["nodes"])

        served = gql(client, "{ issues { nodes { identifier updatedAt } } }", h).json()["data"]
        assert {n["identifier"]: n["updatedAt"] for n in served["issues"]["nodes"]} == {
            "ENG-1": "2026-01-05T00:00:00Z",
            "ENG-2": "2026-02-01T00:00:00Z",
        }
        assert ids_for('{updatedAt: {eq: "2026-02-01T00:00:00Z"}}') == ["ENG-2"]
        assert ids_for('{updatedAt: {neq: "2020-01-01T00:00:00Z"}}') == ["ENG-1", "ENG-2"]
        assert ids_for('{updatedAt: {gte: "2026-01-01T00:00:00Z"}}') == ["ENG-1", "ENG-2"]
        assert ids_for('{updatedAt: {lt: "2030-01-01T00:00:00Z"}}') == ["ENG-1", "ENG-2"]
        assert ids_for('{updatedAt: {gt: "2026-01-05T00:00:00Z"}}') == ["ENG-2"]
        # and the sort agrees with the filter, both reading the served value
        desc = gql(
            client,
            "{ issues(sort: [{updatedAt: {order: Descending}}]) { nodes { identifier } } }",
            h,
        ).json()["data"]["issues"]["nodes"]
        assert [n["identifier"] for n in desc] == ["ENG-2", "ENG-1"]


def test_a_mixed_case_suffix_is_reached_under_a_lower_cased_key(tmp_path):
    """The schema leaves an identifier's suffix opaque, so a corpus may write `ENG-1a`. Folding the
    case of the whole value could not reach it from `eng-1a` -- the probes were `eng-1a` and then
    `ENG-1A`, neither of which is the stored string -- while `issue(id: "ENG-1a")` found it, so one
    issue answered under one spelling of its key and not the other. Linear's identifier has letters
    only in the key, so the key is folded and the suffix is compared as written: `eng-1a` and
    `ENG-1a` are the same issue, `ENG-1A` is a different string and names nothing."""
    records = [
        complete(
            "linear",
            doc_id="m1",
            identifier="ENG-1a",
            title="mixed",
            created="2026-01-01T00:00:00Z",
        )
    ]
    with corpus_client(tmp_path, records) as (client, settings):
        h = {"Authorization": settings.admin_token}
        for spelling in ("ENG-1a", "eng-1a"):
            one = gql(client, '{ issue(id: "%s") { identifier } }' % spelling, h).json()
            assert one["data"]["issue"]["identifier"] == "ENG-1a", spelling
            page = gql(
                client,
                '{ issues(filter: {id: {eq: "%s"}}) { nodes { identifier } } }' % spelling,
                h,
            ).json()
            assert [n["identifier"] for n in page["data"]["issues"]["nodes"]] == ["ENG-1a"], (
                spelling
            )
        miss = gql(client, '{ issue(id: "ENG-1A") { identifier } }', h).json()
        assert "Entity not found" in miss["errors"][0]["message"]


def test_an_unset_priority_filters_and_sorts_as_the_zero_it_is_served_as(tmp_path):
    """`Issue.priority` is non-null in Linear and reads 0 for "no priority"; Backlot serves 0 for a
    NULL column. The filter and the sort have to see that same 0, or `priority: {eq: 0}` misses the
    issue a client just read `priority: 0` from, `nin: [0]` returns it, and `null: true` answers a
    field that is never null."""
    records = [
        complete(
            "linear", doc_id="np", identifier="ENG-1", title="unset", created="2026-01-01T00:00:00Z"
        ),
        complete(
            "linear",
            doc_id="p2",
            identifier="ENG-2",
            title="two",
            priority=2,
            created="2026-01-02T00:00:00Z",
        ),
    ]
    with corpus_client(tmp_path, records) as (client, settings):
        h = {"Authorization": settings.admin_token}

        def ids_for(filter_literal):
            body = gql(
                client, "{ issues(filter: %s) { nodes { identifier } } }" % filter_literal, h
            )
            return sorted(n["identifier"] for n in body.json()["data"]["issues"]["nodes"])

        served = gql(client, "{ issues { nodes { identifier priority } } }", h).json()["data"]
        assert {n["identifier"]: n["priority"] for n in served["issues"]["nodes"]} == {
            "ENG-1": 0,
            "ENG-2": 2,
        }
        assert ids_for("{priority: {eq: 0}}") == ["ENG-1"]
        assert ids_for("{priority: {lt: 1}}") == ["ENG-1"]
        assert ids_for("{priority: {nin: [0]}}") == ["ENG-2"]
        assert ids_for("{priority: {neq: 0}}") == ["ENG-2"]
        assert ids_for("{priority: {null: true}}") == []
        asc = gql(
            client, "{ issues(sort: [{priority: {order: Ascending}}]) { nodes { identifier } } }", h
        ).json()["data"]["issues"]["nodes"]
        assert [n["identifier"] for n in asc] == ["ENG-1", "ENG-2"]


def test_linear_field_error_is_a_200_and_a_syntax_error_is_a_400(tmp_path):
    """Real Linear splits these: a bad document never executed is a 400 with no `data` key, while
    an error raised mid-execution is a 200 carrying `data` alongside `errors`."""
    with _linear_client(tmp_path) as (client, settings):
        h = {"Authorization": settings.admin_token}
        bad = client.post("/linear/graphql", json={"query": "{ issues( }"}, headers=h)
        assert bad.status_code == 400 and "data" not in bad.json()

        missing = client.post(
            "/linear/graphql", json={"query": '{ issue(id: "NOPE-1") { identifier } }'}, headers=h
        )
        assert missing.status_code == 200
        assert "data" in missing.json() and missing.json()["errors"]


_API_KEY_AS_BEARER = (
    b'{"errors":[{"message":"It looks like you\'re trying to use an API key as a Bearer token.'
    b' Remove the Bearer prefix from the Authorization header.","extensions":{"type":"invalid'
    b' input","code":"INPUT_ERROR","statusCode":400,"userError":true}}]}'
)
_UNAUTHENTICATED = (
    b'{"errors":[{"message":"Authentication required, not authenticated","extensions":{"type":"a'
    b'uthentication error","code":"AUTHENTICATION_ERROR","statusCode":401,"userError":true,"user'
    b'PresentableMessage":"You need to authenticate to access this operation.","meta":{},"http":{"status":401}}}]}'
)
# `Authorization` values, `{t}` for an API key that resolves, and the status Linear gave each: 200
# serving the query, 400 being `_API_KEY_AS_BEARER` and 401 `_UNAUTHENTICATED`.
_LINEAR_SPELLINGS = [
    pytest.param("{t}", 200, id="bare"),
    pytest.param(" {t}", 200, id="a space before"),
    pytest.param("{t} ", 200, id="a space after"),
    pytest.param("\xa0{t}", 401, id="U+00A0 before"),
    pytest.param("{t}\xa0", 401, id="U+00A0 after"),
    pytest.param("{t} extra", 401, id="a word after"),
    pytest.param("Bearer {t}", 400, id="Bearer"),
    pytest.param("bearer {t}", 400, id="bearer"),
    pytest.param("Bearer {t} ", 400, id="Bearer, a space after"),
    pytest.param(" Bearer {t}", 400, id="Bearer, a space before"),
    pytest.param("Bearer  {t}", 401, id="Bearer, two spaces"),
    pytest.param("Bearer\t{t}", 401, id="Bearer, a tab"),
    pytest.param("Bearer \t{t}", 401, id="Bearer, a space and a tab"),
    pytest.param("Bearer \xa0{t}", 401, id="Bearer, a space and U+00A0"),
    pytest.param("Bearer{t}", 401, id="Bearer, no space"),
    pytest.param("Bearer,{t}", 401, id="Bearer, a comma"),
    pytest.param("Bearer {t} extra", 401, id="Bearer, a word after"),
    pytest.param("Bearer {t}x", 401, id="Bearer, a key that does not resolve"),
    pytest.param("Bearer lin_api_nope", 401, id="Bearer lin_api_nope"),
    pytest.param(_KEY_SHAPED, 400, id="Bearer, a key's shape Linear never issued"),
    pytest.param("bearer lin_api_" + "0" * 40, 400, id="bearer, a key's shape"),
    pytest.param(
        "Bearer lin_api_" + "aB3" * 13 + "a", 400, id="Bearer, a key's shape in both cases"
    ),
    pytest.param(
        "Bearer lin_api_" + "0" * 39, 401, id="Bearer, a character short of a key's shape"
    ),
    pytest.param("Bearer lin_api_" + "0" * 41, 401, id="Bearer, a character past a key's shape"),
    pytest.param("Bearer lin_api_" + "0" * 39 + "-", 401, id="Bearer, a key's shape ending in -"),
    pytest.param(_KEY_SHAPED + " x", 401, id="Bearer, a key's shape and a word after"),
    pytest.param("Bearer LIN_API_" + "0" * 40, 401, id="Bearer, LIN_API_"),
    pytest.param("Bearer lin_oauth_" + "0" * 40, 401, id="Bearer, lin_oauth_"),
    pytest.param("BEARER lin_api_" + "0" * 40, 401, id="BEARER, a key's shape"),
    pytest.param("lin_api_" + "0" * 40, 401, id="a key's shape alone"),
    pytest.param("Bearer nope", 401, id="Bearer nope"),
    pytest.param("bearer nope", 401, id="bearer nope"),
    pytest.param("Token {t}", 401, id="Token"),
    pytest.param("nope", 401, id="nope"),
    pytest.param("Bearer", 401, id="Bearer alone"),
    pytest.param("Basic Zm9vOmJhcg==", 401, id="Basic"),
]


# `SWEPT_BYTES` in five places of `Authorization`: the statuses other than the 401 every other byte
# drew.
_LINEAR_BYTES = {
    "Bearer{x}{t}": {0x20: 400},
    "Bearer {x}{t}": {},
    "Bearer {t}{x}": {0x09: 400, 0x20: 400},
    "{x}{t}": {0x09: 200, 0x20: 200},
    "{t}{x}": {0x09: 200, 0x20: 200},
}


def _linear_status(client, tokens_yaml, value: str) -> int:
    """The status `Authorization: value` draws, read as the comment over `_LINEAR_SPELLINGS` reads
    it."""
    header = value.replace("{t}", tokens_yaml["admin_token"])
    r = sent_as(
        client,
        "POST",
        "/linear/graphql",
        {"Authorization": header, "Content-Type": "application/json"},
        content=b'{"query":"{ viewer { id } }"}',
    )
    if r.status_code == 200:
        assert r.json()["data"]["viewer"]["id"]
    elif r.status_code == 400:
        assert r.content == _API_KEY_AS_BEARER
    else:
        assert r.content == _UNAUTHENTICATED
    return r.status_code


@pytest.mark.parametrize("value, status", _LINEAR_SPELLINGS)
def test_linear_reads_a_credential_as_linear_does(client, tokens_yaml, value, status):
    assert _linear_status(client, tokens_yaml, value) == status


def test_linear_reads_a_swept_byte_and_a_casing_of_the_scheme_as_linear_does(client, tokens_yaml):
    """Each of `SWEPT_BYTES` in each place `_LINEAR_BYTES` names, and each of the 64 casings of the
    scheme, draws the status Linear gave it."""
    expected = {
        shape.replace("{x}", chr(byte)): answers.get(byte, 401)
        for shape, answers in _LINEAR_BYTES.items()
        for byte in SWEPT_BYTES
    }
    for letters in itertools.product(*[(c, c.upper()) for c in "bearer"]):
        scheme = "".join(letters)
        expected[f"{scheme} {{t}}"] = 400 if scheme in ("Bearer", "bearer") else 401
    assert {value: _linear_status(client, tokens_yaml, value) for value in expected} == expected


def test_linear_issue_asserts_rather_than_re_derive_a_missing_identifier():
    """`_issue` must not fall back to `synth.linear_identifier`: the importer seeds that on the
    DATASET id and the resolver only has the served UUID, so the fallback answered an identifier
    the issue is not reachable by (`PLA-4821` where the row serves `PLA-4442`). Every issue
    carries one by the time it is served, so reaching here without one is a bug upstream —
    the same reasoning as github's `_issue_number`."""
    from backlot.graphql.linear_resolvers import _issue

    with pytest.raises(AssertionError, match="no identifier"):
        _issue({"id": "u-1", "identifier": None, "team": "engineering", "title": "T"}, None)


def test_linear_issue_resolves_first_by_id_when_identifier_repeats(tmp_path):
    """`identifier` is not unique -- 107 issues share one key in a real corpus (see the schema
    comment on `linear_issues.parent_key`) -- so `issue(id: "DUP-1")` deliberately answers the first
    match BY SERVED ID rather than pretending the lookup is unambiguous.
    `store.linear_issue_by_identifier` carries that rule; this pins
    that `resolve_issue`'s new served-id-first stage still falls through to it for an
    identifier -- a served UUID lookup can never itself be ambiguous like this, since `id`
    is UNIQUE."""
    docs = [
        {
            "source_type": "linear",
            "team": "engineering",
            "doc_id": "dup-a",
            "title": "First",
            "content": "x",
            "author_email": "a@acme.com",
            "identifier": "DUP-1",
        },
        {
            "source_type": "linear",
            "team": "engineering",
            "doc_id": "dup-b",
            "title": "Second",
            "content": "x",
            "author_email": "a@acme.com",
            "identifier": "DUP-1",
        },
    ]
    with corpus_client(tmp_path, docs) as (client, settings):
        h = {"Authorization": settings.admin_token}
        r = gql(client, '{ issue(id: "DUP-1") { title } }', h)
        assert r.json()["data"]["issue"]["title"] == "First"


@pytest.mark.parametrize(
    "order",
    [("night-shift", "north-star"), ("north-star", "night-shift")],
    ids=["insertion-agrees-with-name-order", "insertion-disagrees-with-name-order"],
)
def test_linear_team_key_collision_resolves_to_the_first_team_by_name(tmp_path, order):
    """`synth.linear_team_key` is NOT injective: "night-shift" and "north-star" both reduce to
    "NS" (see the schema comment on `linear_teams`). The resolution must keep picking the same
    team every time -- the first by container NAME -- or a key resolving to one team
    silently resolves to a different one after a reimport.

    Parametrized over BOTH insertion orders: with a single fixed order, insertion order and name
    order happen to coincide, so a lookup with no tie-break at all would still pass by accident.
    Reversing insertion order removes that coincidence."""
    assert synth.linear_team_key("night-shift") == synth.linear_team_key("north-star") == "NS"
    docs = [
        {
            "source_type": "linear",
            "team": t,
            "doc_id": f"{t}-1",
            "title": t,
            "content": "x",
            "author_email": "a@acme.com",
        }
        for t in order
    ]
    with corpus_client(tmp_path, docs) as (client, settings):
        h = {"Authorization": settings.admin_token}
        got = gql(client, '{ team(id: "NS") { name } }', h).json()["data"]["team"]
        assert got["name"] == "night-shift"  # "night-shift" < "north-star" by name, either way


def test_linear_team_key_precedes_the_raw_name_affordance(tmp_path):
    """`resolve_team` tries the served KEY before the raw-name affordance, unconditionally (see
    `resolve_team`'s docstring).

    The collision that makes the order observable: a container literally named "ABCD", and a second
    one whose KEY is also "ABCD" (`synth.linear_team_key("alpha-beta-charlie-delta")` takes word
    initials). Resolving both spellings out of one shared lookup would settle it by team-NAME order
    -- "ABCD" sorts before "alpha-beta-charlie-delta", so the literal name would win. It must
    resolve to the KEY's team instead: a real Linear spelling beats the Backlot-only affordance."""
    assert synth.linear_team_key("ABCD") == "ABC"  # its own key -- not a collision with itself
    assert synth.linear_team_key("alpha-beta-charlie-delta") == "ABCD"
    docs = [
        {
            "source_type": "linear",
            "team": t,
            "doc_id": f"{i}",
            "title": t,
            "content": "x",
            "author_email": "a@acme.com",
        }
        for i, t in enumerate(("ABCD", "alpha-beta-charlie-delta"))
    ]
    with corpus_client(tmp_path, docs) as (client, settings):
        h = {"Authorization": settings.admin_token}
        got = gql(client, '{ team(id: "ABCD") { name } }', h).json()["data"]["team"]
        assert got["name"] == "alpha-beta-charlie-delta"


def test_linear_team_uuid_wins_a_raw_name_collision(tmp_path):
    """The served UUID is checked FIRST, so it can never be shadowed by another team's raw-name
    affordance -- even in the constructed case where the two literally collide: a second
    container named exactly the UUID string `synth.linear_team_id` derives for the first one."""
    uuid_of_widgets = synth.linear_team_id("widgets")
    docs = [
        {
            "source_type": "linear",
            "team": t,
            "doc_id": f"{i}",
            "title": t,
            "content": "x",
            "author_email": "a@acme.com",
        }
        for i, t in enumerate(("widgets", uuid_of_widgets))
    ]
    with corpus_client(tmp_path, docs) as (client, settings):
        h = {"Authorization": settings.admin_token}
        got = gql(client, "{ team(id: %s) { name } }" % lit(uuid_of_widgets), h).json()["data"][
            "team"
        ]
        assert got["name"] == "widgets"


def test_a_provided_prefix_is_the_teams_key_on_every_surface(tmp_path):
    """A corpus that writes `ENG-7` into a team the name-derivation would call `PP`
    must be served one spelling: real Linear derives an identifier FROM its team's
    key, so `identifier: "ENG-7"` under `team { key: "PP" }` is the object
    contradicting itself. The key holds on the issue's team object, on `team(id:)`
    lookup, on the teams listing filter, and on the compiled issue filter — and the
    keyless sibling's identifier carries it too."""
    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "linear",
                "doc_id": "ln-a",
                "team": "payments-platform",
                "title": "provides the prefix",
                "content": "x",
                "identifier": "ENG-7",
                "author_email": "ava@acme.com",
                "visibility": "public",
                "created": "2026-02-01T00:00:00Z",
            },
            {
                "source_type": "linear",
                "doc_id": "ln-b",
                "team": "payments-platform",
                "title": "keyless sibling",
                "content": "y",
                "author_email": "ava@acme.com",
                "visibility": "public",
                "created": "2026-02-02T00:00:00Z",
            },
        ],
    )
    with client_for(settings) as c:
        h = {"Authorization": settings.admin_token}
        issue = gql(c, '{ issue(id: "ENG-7") { identifier team { key name } } }', h).json()["data"][
            "issue"
        ]
        assert issue["identifier"] == "ENG-7"
        assert issue["team"] == {"key": "ENG", "name": "payments-platform"}
        team = gql(c, '{ team(id: "ENG") { key name } }', h).json()["data"]["team"]
        assert team == {"key": "ENG", "name": "payments-platform"}
        both = gql(
            c,
            '{ issues(first: 10, filter: {team: {key: {eq: "ENG"}}}) { nodes { identifier } } }',
            h,
        ).json()["data"]["issues"]["nodes"]
        idents = sorted(n["identifier"] for n in both)
        assert len(idents) == 2 and all(i.startswith("ENG-") for i in idents)
        teams = gql(
            c,
            '{ teams(first: 10, filter: {key: {eq: "ENG"}}) { nodes { name } } }',
            h,
        ).json()["data"]["teams"]["nodes"]
        assert [t["name"] for t in teams] == ["payments-platform"]


def _linear_doc(did, team, title, identifier=None, day="01"):
    d = {
        "source_type": "linear",
        "doc_id": did,
        "team": team,
        "title": title,
        "content": "x",
        "author_email": "ava@acme.com",
        "visibility": "public",
        "created": f"2026-02-{day}T00:00:00Z",
    }
    if identifier:
        d["identifier"] = identifier
    return d


def test_a_teams_key_survives_being_nested_under_and_or(tmp_path):
    """`team.key` compiles by expanding the filter's value over the team column's distinct
    names, and the expansion needs the corpus-provided keys to do it. Those were passed to
    the top-level compile only, so one query written two ways disagreed: the flat filter
    found the team at its own key while the same filter under `and` found nothing there and
    everything at the name-derived one it had stopped answering to."""
    settings = build_corpus(
        tmp_path,
        [
            _linear_doc("ln-a", "payments-platform", "states it", identifier="ENG-7"),
            _linear_doc("ln-b", "payments-platform", "keyless sibling", day="02"),
        ],
    )
    with client_for(settings) as c:
        h = {"Authorization": settings.admin_token}

        def idents(flt):
            q = f"{{ issues(first: 10, filter: {flt}) {{ nodes {{ identifier }} }} }}"
            return sorted(n["identifier"] for n in gql(c, q, h).json()["data"]["issues"]["nodes"])

        flat = idents('{team: {key: {eq: "ENG"}}}')
        assert len(flat) == 2
        assert idents('{and: [{team: {key: {eq: "ENG"}}}]}') == flat
        assert idents('{or: [{team: {key: {eq: "ENG"}}}]}') == flat
        assert idents('{team: {and: [{key: {eq: "ENG"}}]}}') == flat
        # and the retired name-derived key answers nowhere, in either spelling
        assert idents('{team: {key: {eq: "PP"}}}') == []
        assert idents('{and: [{team: {key: {eq: "PP"}}}]}') == []


def test_a_stated_key_beats_one_another_team_only_derives_from_its_name(tmp_path):
    """`payments-platform` states ENG; `engineering` merely shortens to it. Both then serve
    `key: "ENG"` — a collision `linear_team_key` has always allowed — but `team(id: "ENG")`
    can return one, and returning the team that never wrote ENG down anywhere made the
    corpus's own spelling unreachable. What a corpus states outranks what Backlot derives,
    the same order the issue and jira indexes resolve a tie in."""
    settings = build_corpus(
        tmp_path,
        [
            _linear_doc("ln-a", "payments-platform", "states ENG", identifier="ENG-7"),
            _linear_doc("ln-b", "engineering", "derives ENG", day="02"),
        ],
    )
    with client_for(settings) as c:
        h = {"Authorization": settings.admin_token}
        assert gql(c, '{ team(id: "ENG") { key name } }', h).json()["data"]["team"] == {
            "key": "ENG",
            "name": "payments-platform",
        }
        # the other team is not lost — its UUID and its own name still address it exactly
        eng_id = synth.linear_team_id("engineering")
        by_uuid = gql(c, f'{{ team(id: "{eng_id}") {{ name }} }}', h).json()["data"]["team"]
        assert by_uuid == {"name": "engineering"}
        by_name = gql(c, '{ team(id: "engineering") { name } }', h).json()["data"]["team"]
        assert by_name == {"name": "engineering"}


def test_a_stated_identifier_outranks_another_teams_derived_one(tmp_path):
    """Two teams reduced to one key can derive the same identifier, and `issue(id:)` resolves
    a repeat to the first row by doc_id. When one of the two WROTE that identifier down, doc_id
    order is the wrong tie-break: the document that asked for the id answered "not found" at
    it. Provided claims its spelling first; a derived one takes what is left."""
    stated = "ENG-8774"
    settings = build_corpus(
        tmp_path,
        [
            # sorts first by doc_id, and derives exactly the identifier the other one states
            _linear_doc(
                next(
                    d
                    for d in (f"aa-{i}" for i in range(200_000))
                    if synth.linear_identifier(d, "ENG") == stated
                ),
                "engineering",
                "derives it",
            ),
            _linear_doc("zz-states-it", "engineering", "states it", identifier=stated, day="02"),
        ],
    )
    with client_for(settings) as c:
        h = {"Authorization": settings.admin_token}
        got = gql(c, f'{{ issue(id: "{stated}") {{ identifier title }} }}', h).json()["data"]
        assert got["issue"]["title"] == "states it"


_BACKWARD_ROOTS = {
    "issues": ("{ issues(%s, orderBy: updatedAt) { %s } }", ["issues"]),
    "team": (
        '{ team(id: "engineering") { issues(%s, orderBy: updatedAt) { %s } } }',
        ["team", "issues"],
    ),
    "users": ("{ users(%s) { %s } }", ["users"]),
    "children": (
        '{ issue(id: "ENG-1") { children(%s, orderBy: updatedAt) { %s } } }',
        ["issue", "children"],
    ),
    "teams": ("{ teams(%s) { %s } }", ["teams"]),
    "comments": ("{ comments(%s) { %s } }", ["comments"]),
    "issue comments": ('{ issue(id: "ENG-1") { comments(%s) { %s } } }', ["issue", "comments"]),
    "labels": ('{ issue(id: "ENG-1") { labels(%s) { %s } } }', ["issue", "labels"]),
    "attachments": ('{ issue(id: "ENG-1") { attachments(%s) { %s } } }', ["issue", "attachments"]),
    "relations": ('{ issue(id: "ENG-1") { relations(%s) { %s } } }', ["issue", "relations"]),
    "inverse relations": (
        '{ issue(id: "ENG-2") { inverseRelations(%s) { %s } } }',
        ["issue", "inverseRelations"],
    ),
    "releases": ('{ issue(id: "ENG-1") { releases(%s) { %s } } }', ["issue", "releases"]),
}


@pytest.fixture(scope="module")
def backward_pages(tmp_path_factory):
    """The corpus `test_linear_backward_pages_name_the_served_nodes` describes."""
    records = [
        {
            "source_type": "linear",
            "doc_id": f"page-{i}",
            "team": "engineering",
            "group": "engineering",
            "identifier": f"ENG-{i}",
            "title": f"Issue {i}",
            "content": "page",
            "author_email": f"user{i}@acme.com",
            "visibility": "public",
            "created": f"2026-01-{i:02}T00:00:00Z",
            "updated": f"2026-02-{i:02}T00:00:00Z",
            **({"parent": "ENG-1"} if i > 1 else {}),
            **(
                {
                    "comments": [
                        {"content": f"note {n}", "created_ts": 1767225600 + n} for n in range(8)
                    ],
                    "labels": [f"label-{n}" for n in range(8)],
                    "attachments": [f"https://acme.test/run/{n}" for n in range(8)],
                    "relations": [{"to": f"page-{n}", "type": "related"} for n in range(2, 9)],
                    "release": "r1",
                }
                if i == 1
                else {"relations": [{"to": "page-2", "type": "related"}]}
                if i > 2
                else {}
            ),
        }
        for i in range(1, 9)
    ] + [
        {
            "source_type": "linear",
            "doc_id": f"squad-{i}",
            "team": f"squad{i}",
            "group": f"squad{i}",
            "title": f"Squad issue {i}",
            "content": "page",
            "author_email": "user1@acme.com",
            "visibility": "public",
            "created": f"2026-03-{i:02}T00:00:00Z",
            "updated": f"2026-04-{i:02}T00:00:00Z",
        }
        for i in range(1, 8)
    ]
    return build_corpus(tmp_path_factory.mktemp("backward_pages"), records)


@pytest.mark.parametrize("root", list(_BACKWARD_ROOTS))
def test_linear_backward_pages_name_the_served_nodes(backward_pages, root):
    """The measurement on ``linear_resolvers._connection`` covers these connections.

    Its before endCursor continues backward; before startCursor overlaps the preceding page.
    Distinct timestamps make position independent of database tie ordering. ENG-1 holds eight
    comments, labels and attachments, a relation to each other issue and one release, every other
    issue a relation to ENG-2, and each team past the first one issue.
    """
    settings = backward_pages
    with client_for(settings) as client:

        def page(args):
            selection = (
                "nodes { id } pageInfo { startCursor endCursor hasPreviousPage hasNextPage }"
            )
            query, path = _BACKWARD_ROOTS[root]
            response = gql(
                client, query % (args, selection), {"Authorization": settings.admin_token}
            )
            assert response.status_code == 200
            body = response.json()
            assert "errors" not in body, body
            result = body["data"]
            for key in path:
                result = result[key]
            return result

        forward = [node["id"] for node in page("first: 50")["nodes"]]
        backward = page("last: 3")

        def got(result):
            return [node["id"] for node in result["nodes"]]

        assert got(backward) == forward[-3:][::-1]

        following = page("last: 3, before: " + lit(backward["pageInfo"]["endCursor"]))
        assert got(following) == forward[-6:-3][::-1]
        overlapping = page("last: 3, before: " + lit(backward["pageInfo"]["startCursor"]))
        assert got(overlapping) == forward[-4:-1][::-1]

        first = page("first: 1")
        assert first["pageInfo"]["startCursor"] == first["pageInfo"]["endCursor"]
        empty = page("last: 3, before: " + lit(first["pageInfo"]["startCursor"]))
        assert empty["nodes"] == []
        assert empty["pageInfo"]["startCursor"] is None
        assert empty["pageInfo"]["endCursor"] is None
        assert got(page("last: 50")) == forward[::-1]

        after = page("first: 1, after: " + lit(first["pageInfo"]["endCursor"]))
        assert after["pageInfo"]["hasPreviousPage"] is False
        assert after["pageInfo"]["hasNextPage"] is (len(forward) > 2)
        before = page("last: 1, before: " + lit(backward["pageInfo"]["endCursor"]))
        assert before["pageInfo"]["hasNextPage"] is False
        assert before["pageInfo"]["hasPreviousPage"] is (len(forward) > 4)

        current = page("first: 3")
        after_first = page("first: 2, after: " + lit(current["pageInfo"]["startCursor"]))
        assert got(after_first) == forward[1:3]
        seen = []
        for _ in range(len(forward) + 1):
            seen.extend(got(current))
            if not current["nodes"]:
                break
            current = page("first: 3, after: " + lit(current["pageInfo"]["endCursor"]))
        assert seen == forward

        seen = []
        for _ in range(len(forward) + 1):
            seen.extend(got(backward))
            if not backward["nodes"]:
                break
            backward = page("last: 3, before: " + lit(backward["pageInfo"]["endCursor"]))
        assert seen == forward[::-1]
        assert len(seen) == len(set(seen))


# --- the load balancer, Express, cors and Apollo Server in front of Linear's GraphQL --------------
# The request, the credential (`_LINEAR_CREDENTIALS`) and the answer api.linear.app gave it, as
# `graphql` records the layers it was measured through: its status, its media type and its body but
# for the newline Apollo Server ends one with, `_TYPENAME_DATA` where it served the query, and none
# for a `HEAD`.
_CSRF = (
    b'{"errors":[{"message":"This operation has been blocked as a potential Cross-Site Request'
    b" Forgery (CSRF). Please either specify a 'content-type' header (with a type that is not one"
    b" of application/x-www-form-urlencoded, multipart/form-data, text/plain) or provide a"
    b" non-empty value for one of the following headers: x-apollo-operation-name,"
    b' apollo-require-preflight\\n","extensions":{"http":{"status":400,"headers":{}},"code":"BAD_R'
    b'EQUEST","type":"graphql error","userError":true}}]}'
)
_ONLY_GET_POST = (
    b'{"errors":[{"message":"Apollo Server supports only GET/POST requests.","extensions":{"http":'
    b'{"status":405,"headers":{}},"code":"BAD_REQUEST","type":"graphql error","userError":true}}]}'
)
_GET_MUTATION = (
    b'{"errors":[{"message":"GET requests only support query operations, not mutation operations"'
    b',"extensions":{"http":{"status":405,"headers":{}},"code":"BAD_REQUEST","type":"graphql erro'
    b'r","userError":true}}]}'
)
_NOT_ACCEPTABLE = (
    b'{"errors":[{"message":"An \'accept\' header was provided for this request which does not'
    b" accept application/json; charset=utf-8 or application/graphql-response+json; charset=utf-8"
    b'","extensions":{"http":{"status":406},"code":"BAD_REQUEST","type":"graphql error","userErr'
    b'or":true}}]}'
)
_NO_QUERY = (
    b'{"errors":[{"message":"GraphQL operations must contain a non-empty `query` or a `persistedQ'
    b'uery` extension.","extensions":{"http":{"status":400,"headers":{}},"code":"INVALID_INPUT","'
    b'type":"invalid input","userError":true}}]}'
)
_TYPENAME_DATA = b'{"data":{"__typename":"Query"}}'
_JSON_UTF8 = "application/json; charset=utf-8"
_GRAPHQL_RESPONSE = "application/graphql-response+json; charset=utf-8"
_CLOUDFLARE_PAGE = (
    b"<html>\r\n<head><title>%s</title></head>\r\n<body>\r\n<center><h1>%s</h1></center>\r\n"
    b"<hr><center>cloudflare</center>\r\n</body>\r\n</html>\r\n"
)
_LINEAR_PREFLIGHT = {
    "access-control-allow-origin": "https://example.com",
    "cache-control": "s-maxage=43200",
    "vary": "Origin, Access-Control-Request-Headers",
    "cache-tag": "preflight",
    "access-control-allow-methods": "GET,HEAD,PUT,PATCH,POST,DELETE,OPTIONS",
    "access-control-expose-headers": "Cache-Control,Content-Language,Content-Length,Content-Type,"
    "Expires,Last-Modified,Pragma,x-request-id,cf-ray,cf-cache-status,Retry-After",
    "access-control-max-age": "86400",
    # Sent no `Access-Control-Request-Headers`, a preflight is answered none.
    "access-control-allow-headers": None,
}
_J = (("Content-Type", "application/json"),)
_P = (("apollo-require-preflight", "1"),)
_ORIGIN = (("Origin", "https://example.com"),)
_PREFLIGHT_ASKED = (*_ORIGIN, ("Access-Control-Request-Method", "POST"))
_Q = "?query=%7B%20__typename%20%7D"


def _search_refusal(message: bytes) -> bytes:
    return (
        b'{"errors":[{"message":"%s","extensions":{"http":{"status":400,"headers":{}},"code":"BAD_REQ'
        b'UEST","type":"graphql error","userError":true}}]}' % message
    )


_LINEAR_HTTP = [
    # Cloudflare, then Linear's load balancer, ahead of everything; then Apollo Server's 405 for
    # each other method.
    http_row(
        "TRACE",
        "",
        (),
        None,
        "key",
        405,
        "text/html",
        _CLOUDFLARE_PAGE % ((b"405 Not Allowed",) * 2),
        id="TRACE",
        answered={"connection": "close"},
    ),
    http_row(
        "CONNECT",
        "",
        (),
        None,
        "key",
        400,
        "text/plain; charset=UTF-8",
        b"error code: 1045\n",
        id="CONNECT",
        answered={"connection": None},
    ),
    http_row(
        "PURGE",
        "",
        (),
        None,
        "key",
        400,
        "text/html; charset=UTF-8",
        _CLOUDFLARE_PAGE % ((b"400 Bad Request",) * 2),
        id="PURGE",
        answered={"connection": None},
    ),
    http_row(
        "TRACE",
        "",
        (),
        None,
        "twice",
        400,
        "text/html",
        _CLOUDFLARE_PAGE % ((b"400 Bad Request",) * 2),
        id="TRACE, Authorization twice",
        answered={"connection": "close"},
    ),
    *(
        http_row(m, "", (*_J, *_P), None, "key", 501, None, b"", id=m)
        for m in ("BIND", "LINK", "M-SEARCH", "MKCALENDAR", "REBIND", "SOURCE", "UNBIND", "UNLINK")
    ),
    *(
        http_row(m, "", (*_J, *_P), None, "key", 405, _JSON_UTF8, _ONLY_GET_POST, id=m)
        for m in (
            "ACL",
            "CHECKOUT",
            "COPY",
            "DELETE",
            "LOCK",
            "MERGE",
            "MKACTIVITY",
            "MKCOL",
            "MOVE",
            "NOTIFY",
            "OPTIONS",
            "PATCH",
            "PROPFIND",
            "PROPPATCH",
            "QUERY",
            "REPORT",
            "SEARCH",
            "SUBSCRIBE",
            "UNLOCK",
            "UNSUBSCRIBE",
        )  # fmt: skip
    ),
    # `cors`, whatever the credential; Apollo Server's CSRF check and 405 without `Origin`.
    *(
        http_row(
            "OPTIONS",
            "",
            _PREFLIGHT_ASKED,
            None,
            c,
            204,
            None,
            b"",
            id=f"preflight, {c}",
            answered=_LINEAR_PREFLIGHT,
        )
        for c in ("key", "none", "Bearer key")
    ),
    http_row(
        "OPTIONS",
        "",
        _ORIGIN,
        None,
        "nope",
        204,
        None,
        b"",
        id="Origin alone",
        answered=_LINEAR_PREFLIGHT,
    ),
    http_row(
        "OPTIONS",
        "",
        (("Origin", "null"), ("Access-Control-Request-Method", "POST")),
        None,
        "key",
        204,
        None,
        b"",
        id="Origin null",
        answered={**_LINEAR_PREFLIGHT, "access-control-allow-origin": "null"},
    ),
    http_row(
        "OPTIONS",
        "",
        (*_ORIGIN, ("Access-Control-Request-Method", "PUT")),
        None,
        "key",
        204,
        None,
        b"",
        id="asked PUT",
        answered=_LINEAR_PREFLIGHT,
    ),
    *(
        http_row(
            "OPTIONS",
            "",
            (*_PREFLIGHT_ASKED, ("Access-Control-Request-Headers", asked)),
            None,
            "key",
            204,
            None,
            b"",
            id=f"asked {asked!r}",
            answered={**_LINEAR_PREFLIGHT, "access-control-allow-headers": allowed},
        )
        for asked, allowed in (
            ("Content-Type, X-Foo", "Content-Type,X-Foo"),
            (" a , b ", "a,b"),
            ("a,,b", "a,,b"),
        )
    ),
    http_row("OPTIONS", "", (), None, "key", 400, _JSON_UTF8, _CSRF, id="OPTIONS, unpreflighted"),
    *(
        http_row(
            "OPTIONS",
            "",
            (("Origin", origin), ("Access-Control-Request-Method", "POST")),
            None,
            c,
            400,
            _JSON_UTF8,
            _CSRF,
            id=f"Origin {origin!r}, {c}",
        )
        for origin in ("", "   ")
        for c in ("key", "none")
    ),
    http_row(
        "OPTIONS",
        "",
        (("Origin", ""), *_J),
        None,
        "key",
        405,
        _JSON_UTF8,
        _ONLY_GET_POST,
        id="Origin '', JSON",
    ),
    http_row(
        "OPTIONS", "", (), None, "Bearer key", 400, _JSON_UTF8, _CSRF, id="OPTIONS, Bearer key"
    ),
    http_row(
        "OPTIONS",
        "",
        (("Access-Control-Request-Method", "POST"),),
        None,
        "key",
        400,
        _JSON_UTF8,
        _CSRF,
        id="OPTIONS, no Origin",
    ),
    http_row("OPTIONS", "", _J, None, "key", 405, _JSON_UTF8, _ONLY_GET_POST, id="OPTIONS, JSON"),
    # `_api_key_as_bearer` ahead of `body-parser`.
    *(
        http_row(m, t, h, b, "Bearer key", 400, _JSON_UTF8, a, id=f"{m}, Bearer key")
        for m, t, h, b, a in (
            ("GET", _Q, (), None, _API_KEY_AS_BEARER),
            ("PUT", "", _J, b"{", _API_KEY_AS_BEARER),
            ("HEAD", _Q, _P, None, b""),
            ("PATCH", "", _J, None, _API_KEY_AS_BEARER),
        )
    ),
    # `body-parser` on a JSON body (`_has_body`), then Apollo Server's CSRF check, then its 405.
    http_row("PUT", "", (*_J, *_P), b"{", "key", 400, _JSON_UTF8, _EXPECTED_PROPERTY, id="PUT {"),
    http_row(
        "OPTIONS", "", (*_J, *_P), b"{", "key", 400, _JSON_UTF8, _EXPECTED_PROPERTY, id="OPTIONS {"
    ),
    http_row(
        "PUT", "", (*_J, *_P), _TYPENAME, "key", 405, _JSON_UTF8, _ONLY_GET_POST, id="PUT, a query"
    ),
    http_row(
        "DELETE", "", _J, _TYPENAME, "none", 405, _JSON_UTF8, _ONLY_GET_POST, id="DELETE, a query"
    ),
    http_row(
        "PUT",
        "",
        (("Content-Type", "text/plain"),),
        _TYPENAME,
        "key",
        400,
        _JSON_UTF8,
        _CSRF,
        id="PUT, text/plain",
    ),
    http_row("DELETE", "", (), None, "nope", 400, _JSON_UTF8, _CSRF, id="DELETE, unpreflighted"),
    # A POST's content type: read as JSON, refused as a request a browser sends unpreflighted, or
    # passed with nothing read.
    *(
        http_row(
            "POST",
            "",
            (("Content-Type", t), *h),
            _TYPENAME,
            "key",
            s,
            _JSON_UTF8,
            a,
            id=f"{t!r}{i}",
        )
        for t, h, s, a, i in (
            ("text/plain", (), 400, _CSRF, ""),
            ("text/plain", _P, 400, _NO_KEYS, ", preflight"),
            ("TEXT/PLAIN;X=Y", (), 400, _CSRF, ""),
            ("application/x-www-form-urlencoded", (), 400, _CSRF, ""),
            ("multipart/form-data; boundary=x", (), 400, _CSRF, ""),
            ("", (), 400, _CSRF, ""),
            ("foo", (), 400, _NO_KEYS, ""),
            ("text/plain, application/json", (), 400, _NO_KEYS, ""),
            ("application/graphql", (), 400, _NO_KEYS, ""),
            ("application/json;", (), 400, _NO_KEYS, ""),
            ("application/json; x=y, text/plain", (), 400, _NO_KEYS, ""),
            ("application/json\t;", (), 400, _NO_KEYS, ""),
            ("Application/JSON", (), 200, _TYPENAME_DATA, ""),
            ("application/json ;charset=utf-8", (), 200, _TYPENAME_DATA, ""),
            ('application/json;x="a;b"', (), 200, _TYPENAME_DATA, ""),
        )
    ),
    http_row("POST", "", (), _TYPENAME, "key", 400, _JSON_UTF8, _CSRF, id="no type"),
    http_row("POST", "", _P, _TYPENAME, "key", 400, _JSON_UTF8, _NO_KEYS, id="no type, preflight"),
    http_row("POST", "", _J, b"", "key", 400, _JSON_UTF8, _NO_KEYS, id="JSON, empty"),
    # A GET: its query string, Apollo Server's 405 for one that is not a query, ahead of the
    # credential.
    http_row("GET", _Q, _P, None, "key", 200, _JSON_UTF8, _TYPENAME_DATA, id="GET"),
    http_row("GET", _Q, (), None, "key", 400, _JSON_UTF8, _CSRF, id="GET, unpreflighted"),
    http_row("GET", _Q, _J, None, "key", 200, _JSON_UTF8, _TYPENAME_DATA, id="GET, JSON"),
    http_row(
        "GET",
        _Q,
        (("Content-Type", "text/plain"), *_P),
        None,
        "key",
        200,
        _JSON_UTF8,
        _TYPENAME_DATA,
        id="GET, text/plain",
    ),
    http_row(
        "GET",
        _Q,
        (("Content-Type", "foo"),),
        None,
        "key",
        200,
        _JSON_UTF8,
        _TYPENAME_DATA,
        id="GET, foo",
    ),
    http_row("GET", _Q, _P, None, "none", 401, _JSON_UTF8, _UNAUTHENTICATED, id="GET, none"),
    http_row(
        "GET",
        "?query=%7B+__typename+%7D",
        _P,
        None,
        "key",
        200,
        _JSON_UTF8,
        _TYPENAME_DATA,
        id="GET, +",
    ),
    http_row("GET", "", _P, None, "key", 400, _JSON_UTF8, _NO_QUERY, id="GET, no query"),
    http_row(
        "GET", "?QUERY" + _Q[6:], _P, None, "key", 400, _JSON_UTF8, _NO_QUERY, id="GET, QUERY"
    ),
    *(
        http_row("GET", t, _P, None, "key", 400, _JSON_UTF8, _search_refusal(m), id=i)
        for t, m, i in (
            (
                _Q + "&query=",
                b"The 'query' search parameter may only be specified once.",
                "query twice",
            ),
            (
                _Q + "&operationName=A&operationName=B",
                b"The 'operationName' search parameter may only be specified once.",
                "operationName twice",
            ),
            (
                _Q + "&variables=nope",
                b"The variables search parameter contains invalid JSON.",
                "variables nope",
            ),
            (
                _Q + "&variables=",
                b"The variables search parameter contains invalid JSON.",
                "variables empty",
            ),
            (
                _Q + "&variables=%5B1%5D",
                b"The variables search parameter should contain a JSON-encoded object.",
                "variables [1]",
            ),
            (
                _Q + "&variables=null",
                b"The variables search parameter should contain a JSON-encoded object.",
                "variables null",
            ),
            (
                _Q + "&extensions=5",
                b"The extensions search parameter should contain a JSON-encoded object.",
                "extensions 5",
            ),
            (
                "?query=%7B&query=%7B&variables=nope",
                b"The 'query' search parameter may only be specified once.",
                "query twice, variables nope",
            ),
            (
                "?operationName=A&operationName=B&query=%7B&query=%7B",
                b"The 'query' search parameter may only be specified once.",
                "operationName twice, query twice",
            ),
            (
                "?operationName=A&operationName=B&variables=nope",
                b"The 'operationName' search parameter may only be specified once.",
                "operationName twice, variables nope",
            ),
            (
                "?variables=nope&extensions=5",
                b"The variables search parameter contains invalid JSON.",
                "variables nope, extensions 5",
            ),
        )
    ),
    *(
        http_row(
            "GET",
            "?query=mutation%20%7B%20__typename%20%7D",
            _P,
            None,
            c,
            405,
            _JSON_UTF8,
            _GET_MUTATION,
            id=f"GET mutation, {c}",
            answered={"allow": "POST"},
        )
        for c in ("key", "none")
    ),
    http_row(
        "GET",
        "?query=query%20A%20%7B%20__typename%20%7D%20mutation%20B%20%7B%20__typename%20%7D"
        "&operationName=A",
        _P,
        None,
        "key",
        200,
        _JSON_UTF8,
        _TYPENAME_DATA,
        id="GET, a query beside a mutation",
    ),
    http_row(
        "HEAD",
        _Q,
        _P,
        None,
        "key",
        405,
        _JSON_UTF8,
        b"",
        id="HEAD",
        answered={"allow": "GET, POST"},
    ),
    http_row(
        "HEAD",
        _Q,
        (("Content-Type", "text/plain"),),
        None,
        "key",
        400,
        _JSON_UTF8,
        b"",
        id="HEAD, text/plain",
    ),
    # `apollo_http.response_type` and its 406, the 401 among the answers it replaces.
    *(
        http_row("POST", "", (*_J, ("Accept", a)), _TYPENAME, "key", s, t, b, id=f"Accept {a!r}")
        for a, s, t, b in (
            ("text/html", 406, _JSON_UTF8, _NOT_ACCEPTABLE),
            ("application/json;q=0", 406, _JSON_UTF8, _NOT_ACCEPTABLE),
            ("application/json;q", 406, _JSON_UTF8, _NOT_ACCEPTABLE),
            ("application/json; charset=latin1", 406, _JSON_UTF8, _NOT_ACCEPTABLE),
            ("", 200, _JSON_UTF8, _TYPENAME_DATA),
            ("*/*", 200, _JSON_UTF8, _TYPENAME_DATA),
            ("application/graphql-response+json", 200, _GRAPHQL_RESPONSE, _TYPENAME_DATA),
            ("application/json;q=0, */*", 200, _GRAPHQL_RESPONSE, _TYPENAME_DATA),
            ("text/html;q=0.9, */*;q=0.1", 200, _JSON_UTF8, _TYPENAME_DATA),
            (
                "application/json; callbackSpec=1.0",
                200,
                "application/json; callbackspec=1.0; charset=utf-8",
                _TYPENAME_DATA,
            ),
        )
    ),
    *(
        http_row("POST", "", (*_J, ("Accept", a)), b, c, s, t, e, id=f"Accept {a!r}, {i}")
        for a, b, c, s, t, e, i in (
            ("text/html", b"{}", "key", 400, _JSON_UTF8, _NO_KEYS, "{}"),
            (
                "application/graphql-response+json",
                b"{}",
                "key",
                400,
                _GRAPHQL_RESPONSE,
                _NO_KEYS,
                "{}",
            ),
            ("text/html", b"[]", "key", 400, _JSON_UTF8, _BATCHING, "[]"),
            (
                "text/html",
                b'{"query":"{"}',
                "key",
                406,
                _JSON_UTF8,
                _NOT_ACCEPTABLE,
                "a parse error",
            ),
            ("text/html", _TYPENAME, "none", 406, _JSON_UTF8, _NOT_ACCEPTABLE, "none"),
            (
                "application/graphql-response+json",
                _TYPENAME,
                "none",
                401,
                _GRAPHQL_RESPONSE,
                _UNAUTHENTICATED,
                "none",
            ),
            ("text/html", b"{", "key", 400, _JSON_UTF8, _EXPECTED_PROPERTY, "{"),
        )
    ),
    http_row(
        "GET",
        _Q,
        (*_P, ("Accept", "text/html")),
        None,
        "key",
        406,
        _JSON_UTF8,
        _NOT_ACCEPTABLE,
        id="GET, Accept text/html",
    ),
    http_row(
        "GET",
        "?query=mutation%20%7B%20__typename%20%7D",
        (*_P, ("Accept", "text/html;level=1")),
        None,
        "key",
        406,
        _JSON_UTF8,
        _NOT_ACCEPTABLE,
        id="GET mutation, Accept text/html;level=1",
    ),
    http_row(
        "POST",
        "",
        (("Content-Type", "text/plain"), ("Accept", "application/graphql-response+json")),
        _TYPENAME,
        "key",
        400,
        _GRAPHQL_RESPONSE,
        _CSRF,
        id="text/plain, Accept graphql-response",
    ),
    http_row(
        "PUT",
        "",
        (*_J, ("Accept", "application/graphql-response+json")),
        _TYPENAME,
        "key",
        405,
        _GRAPHQL_RESPONSE,
        _ONLY_GET_POST,
        id="PUT, Accept graphql-response",
    ),
    http_row(
        "GET",
        _Q,
        (*_P, ("Accept", "text/html, application/graphql-response+json")),
        None,
        "key",
        200,
        _GRAPHQL_RESPONSE,
        _TYPENAME_DATA,
        id="GET, Accept text/html then graphql-response",
    ),
    # `cors` lets any other method carrying `Origin` through; an API key under `Bearer` on DELETE.
    *(
        http_row(m, t, (*h, *_ORIGIN), b, "key", s, _JSON_UTF8, a, id=f"{m}, Origin")
        for m, t, h, b, s, a in (
            ("POST", "", _J, _TYPENAME, 200, _TYPENAME_DATA),
            ("GET", _Q, _P, None, 200, _TYPENAME_DATA),
            ("HEAD", _Q, _P, None, 405, b""),
            ("PUT", "", _J, _TYPENAME, 405, _ONLY_GET_POST),
            ("PATCH", "", _J, _TYPENAME, 405, _ONLY_GET_POST),
            ("DELETE", "", _J, _TYPENAME, 405, _ONLY_GET_POST),
        )
    ),
    http_row(
        "DELETE",
        "",
        (),
        None,
        "Bearer key",
        400,
        _JSON_UTF8,
        _API_KEY_AS_BEARER,
        id="DELETE, Bearer key",
    ),
    http_row(
        "OPTIONS",
        "",
        (
            *_PREFLIGHT_ASKED,
            ("Access-Control-Request-Headers", "a"),
            ("Access-Control-Request-Headers", "b"),
        ),
        None,
        "key",
        204,
        None,
        b"",
        id="asked twice",
        answered={**_LINEAR_PREFLIGHT, "access-control-allow-headers": "a,b"},
    ),
    *(
        http_row(
            "OPTIONS",
            "",
            (*_PREFLIGHT_ASKED, ("Access-Control-Request-Headers", asked)),
            None,
            "key",
            204,
            None,
            b"",
            id=f"asked {asked!r}",
            answered={**_LINEAR_PREFLIGHT, "access-control-allow-headers": allowed},
        )
        for asked, allowed in (
            ("a,  ,b", "a,,b"),
            ("A-B, c_d", "A-B,c_d"),
            ("a\t", "a"),
            ("a b", "a b"),
        )
    ),
    http_row(
        "OPTIONS",
        "",
        (("Origin", " https://example.com\xa0"), ("Access-Control-Request-Method", "POST")),
        None,
        "key",
        204,
        None,
        b"",
        id="Origin with spaces",
        answered={**_LINEAR_PREFLIGHT, "access-control-allow-origin": "https://example.com\xa0"},
    ),
    # Copies of a header: `Content-Type` as `backlot.graphql.body.joined_header` joins them, the
    # rest as `apollo_http.headers_seen` reads them.
    *(
        http_row("POST", "", h, _TYPENAME, "key", s, _JSON_UTF8, a, id=i)
        for h, s, a, i in (
            (
                (*_J, ("Accept", "application/json"), ("Accept", "text/html")),
                200,
                _TYPENAME_DATA,
                "Accept json, html",
            ),
            (
                (*_J, ("Accept", "text/html"), ("Accept", "application/json")),
                200,
                _TYPENAME_DATA,
                "Accept html, json",
            ),
            ((*_J, ("Content-Type", "text/plain")), 400, _NO_KEYS, "Content-Type json, text/plain"),
            ((("Content-Type", "text/plain"), *_J), 400, _NO_KEYS, "Content-Type text/plain, json"),
            ((*_J, ("Content-Type", "")), 400, _NO_KEYS, "Content-Type json, empty"),
            ((("Content-Type", ""), *_J), 200, _TYPENAME_DATA, "Content-Type empty, json"),
            ((*_J, *_J), 400, _NO_KEYS, "Content-Type json twice"),
            (
                (("Content-Type", "text/plain"), ("x-apollo-operation-name", " ")),
                400,
                _CSRF,
                "operation name a space",
            ),
        )
    ),
    *(
        http_row("GET", _Q, h, None, "key", 200, _JSON_UTF8, _TYPENAME_DATA, id=i)
        for h, i in (
            ((("apollo-require-preflight", ""), *_P), "preflight empty, 1"),
            ((*_P, ("apollo-require-preflight", "")), "preflight 1, empty"),
        )
    ),
    # No body and no length under a JSON type, by method (`_has_body`).
    *(
        http_row(
            m,
            t,
            (("Content-Type", "application/json; charset=latin1"), *h),
            None,
            "key",
            s,
            _JSON_UTF8,
            a,
            id=f"{m}, latin1, no body",
        )
        for m, t, h, s, a in (
            ("POST", "", (), 415, _LATIN1),
            ("PUT", "", (), 415, _LATIN1),
            ("PATCH", "", (), 415, _LATIN1),
            ("DELETE", "", (), 415, _LATIN1),
            ("OPTIONS", "", (), 415, _LATIN1),
            ("PROPFIND", "", (), 415, _LATIN1),
            ("GET", "", (), 400, _NO_QUERY),
            ("HEAD", "", (), 405, b""),
        )
    ),
    http_row("POST", "", _J, None, "key", 400, _JSON_UTF8, _NO_KEYS, id="JSON, no body"),
    http_row("POST", "", (), None, "key", 400, _JSON_UTF8, _CSRF, id="no type, no body"),
    http_row(
        "POST", "", _P, None, "key", 400, _JSON_UTF8, _NO_KEYS, id="no type, no body, preflight"
    ),
    http_row(
        "POST",
        "",
        _P,
        Chunked(_TYPENAME),
        "key",
        400,
        _JSON_UTF8,
        _NO_KEYS,
        id="no type, chunked, preflight",
    ),
    http_row(
        "POST",
        "",
        _J,
        Chunked(_TYPENAME),
        "key",
        200,
        _JSON_UTF8,
        _TYPENAME_DATA,
        id="JSON, chunked",
    ),
    http_row(
        "GET",
        "?query=%7B&query=%7B",
        (),
        None,
        "key",
        400,
        _JSON_UTF8,
        _CSRF,
        id="GET, unpreflighted, query twice",
    ),
    http_row(
        "GET",
        _Q + "&variables=%7B%7D&operationName=",
        _P,
        None,
        "key",
        400,
        _JSON_UTF8,
        b'{"errors":[{"message":"operation does not exist","extensions":{"type":"invalid input","code":"INPUT_ERROR",'
        b'"statusCode":400,"userError":true,"userPresentableMessage":"The operation does not exist on the query.",'
        b'"http":{"status":400}}}]}',
        id="GET, variables {}, operationName empty",
    ),
]
_LINEAR_CREDENTIALS = {
    "none": None,
    "nope": "nope",
    "Bearer key": "Bearer {t}",
    "key": "{t}",
    "twice": "{t}",
}


def test_linear_answers_each_method_type_and_accept_as_linear_does(client, tokens_yaml):
    """Each row of `_LINEAR_HTTP` is answered as api.linear.app answered it."""
    answered, expected = {}, {}
    for row in _LINEAR_HTTP:
        method, target, headers, body, credential, status, media_type, answer, named = row.values
        sent = list(headers)
        if _LINEAR_CREDENTIALS[credential] is not None:
            value = _LINEAR_CREDENTIALS[credential].format(t=tokens_yaml["admin_token"])
            sent += [("Authorization", value)] * (2 if credential == "twice" else 1)
        content = body.chunks() if isinstance(body, Chunked) else body
        r = sent_as(client, method, f"/linear/graphql{target}", sent, content=content, bare=True)
        answered[row.id] = (
            r.status_code,
            r.headers.get("content-type"),
            r.content,
            {name: r.headers.get(name) for name in named},
        )
        expected[row.id] = (status, media_type, answer, named)
    assert len(expected) == len(_LINEAR_HTTP)
    assert answered == expected
