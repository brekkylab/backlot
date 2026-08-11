"""Linear's GraphQL API over HTTP: the served schema, the resolvers, and the filter compiler.

The filter tests were their own file once. They are still their own SECTION below, and the reason
they exist in this shape is worth keeping: a mutation review found 16 of 17 injected faults in
`backlot/graphql/linear_filters.py` surviving the rest of the suite. A wrong filter returns
plausible-looking data rather than an error, so every comparator pins its BOUNDARY (`lte` vs `lt`),
not merely that it filters something.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from backlot import synth
from tests._helpers import build_corpus, client_for, corpus_client, db_count


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


def test_linear_issue_by_uuid_and_by_identifier(client, admin_h):
    by_key = gql(client, '{ issue(id: "ENG-101") { id identifier title } }', admin_h)
    issue = by_key.json()["data"]["issue"]
    assert issue["identifier"] == "ENG-101"
    by_uuid = gql(client, "{ issue(id: %s) { identifier } }" % lit(issue["id"]), admin_h)
    assert by_uuid.json()["data"]["issue"]["identifier"] == "ENG-101"


def test_linear_issue_url_is_the_real_vendor_domain(client, admin_h):
    """Regression: a rename's blind substitution once turned every served `url` field into
    `linear.backlot`. Asserted on the parsed host (no trailing slash) rather than a URL literal,
    because the vulnerable pattern is the literal characters `app` immediately followed by a
    slash — spelling that combination anywhere, even in a comment, makes a repeat of the bug
    rewrite it right alongside the code it guards. A bare `"linear.app"` with nothing appended
    has no slash for the pattern to land on, so it survives. The `"backlot" not in host` half is
    the one that actually matters: a rename can only ever INTRODUCE the mock's own name into a
    vendor domain, never remove it, so no mechanical substitution can turn that assertion from
    failing into passing."""
    issue = gql(client, '{ issue(id: "ENG-101") { url } }', admin_h).json()["data"]["issue"]
    host = urlparse(issue["url"]).netloc
    assert host == "linear.app"
    assert "backlot" not in host


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
    assert (
        gql(client, "{ team(id: %s) { key } }" % lit(key["id"]), admin_h).json()["data"]["team"][
            "key"
        ]
        == "ENG"
    )


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
    assert gql(client, "{ issueLabel(id: %s) { name } }" % lit(label_id), admin_h).json()["data"][
        "issueLabel"
    ]["name"] in {"bug", "gateway"}


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


def test_linear_default_ordering_is_by_creation_not_insertion(client, admin_h):
    """Linear's docs: "By default results are ordered by createdAt field." An absent `orderBy`
    previously fell through to raw insertion order, so `issues(first: n)` returned an arbitrary n
    rather than the first n by creation."""
    q = "{ issues(first: 50%s) { nodes { identifier createdAt } } }"
    default = [
        n["createdAt"] for n in gql(client, q % "", admin_h).json()["data"]["issues"]["nodes"]
    ]
    explicit = [
        n["createdAt"]
        for n in gql(client, q % ", orderBy: createdAt", admin_h).json()["data"]["issues"]["nodes"]
    ]
    assert default == explicit
    assert default == sorted(default), "default ordering must be by creation, ascending"


def test_linear_sort_input_overrides_the_default_ordering(client, admin_h):
    """`orderBy` carries no direction in Linear, so `sort:` is how a client asks for the other
    one — which means it has to actually win over the default."""
    q = "{ issues(first: 50, sort: [{createdAt: {order: Descending}}]) { nodes { createdAt } } }"
    got = [n["createdAt"] for n in gql(client, q, admin_h).json()["data"]["issues"]["nodes"]]
    assert got == sorted(got, reverse=True)


# --- Linear relations / children / attachments / releases (#25) -----------------------


def test_linear_children_is_the_exact_inverse_of_parent(client, admin_h):
    """Linear DEFINES `children` as the inverse of `parent`, so the two must never disagree. They
    are both read off the `parent_doc_id` resolved at import rather than joined on `identifier`,
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


def test_linear_children_is_acl_scoped(client, tokens_yaml):
    """ENG-103 is restricted to hana, so ava cannot even reach it to ask for its children — and
    the children list must never become a way to observe an issue she is denied."""
    ava = {"Authorization": linear_user_token(tokens_yaml, "ava@acme.com")}
    denied = gql(client, '{ issue(id: "ENG-103") { children { nodes { identifier } } } }', ava)
    assert "Entity not found" in denied.json()["errors"][0]["message"]


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


def test_linear_attachments_url_argument_and_filter(client, admin_h):
    one = gql(
        client,
        '{ issue(id: "ENG-102") { attachments(url: "https://conf.acme.test/design/'
        'batching") { nodes { title } } } }',
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
    """Both directions must consult the resolved `parent_doc_id`, not two independent lookups
    that happen to agree — that is the whole reason the key is resolved once at import.

    Also a performance contract: `@linear/sdk`'s Issue fragment selects `parent { id }` on every
    node, so resolving it by identifier cost ~45ms on a 50-issue page."""
    # `ro_conn` is the SAMPLE db; a fresh get_settings() would follow whatever BACKLOT_DATA_DIR
    # another module last set, which is why this reads the fixture instead.
    row = ro_conn.execute(
        "SELECT doc_id, parent_doc_id, parent_key FROM linear_issues WHERE doc_id = 'lin-batch'"
    ).fetchone()
    # the import pass resolved the KEY into a doc_id
    assert row["parent_key"] == "ENG-103"
    assert row["parent_doc_id"] == "lin-secret"
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
        "assignee": "bob@acme.com",
        "assigneeName": "Bob Stone",
        "created": "2026-01-01T00:00:00Z",
        "comments": [{"content": "first note", "author_email": "bob@acme.com"}],
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


def test_null_comparator_on_a_nullable_number(fclient):
    assert ids(fclient, "{estimate: {null: true}}") == ["DES-1"]
    assert ids(fclient, "{estimate: {null: false}}") == ["ENG-1", "ENG-2"]


def test_neq_keeps_rows_whose_column_is_null(fclient):
    """NULL never equals anything, so a bare `<> ?` would drop the rows a caller asking for
    "not X" expects to see."""
    assert "DES-1" in ids(fclient, "{estimate: {neq: 5}}")


def test_empty_in_list_matches_nothing_and_empty_nin_matches_everything(fclient):
    assert ids(fclient, "{priority: {in: []}}") == []
    assert ids(fclient, "{priority: {nin: []}}") == ALL


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


def test_a_malformed_date_is_an_error_not_a_silent_mismatch(fclient):
    assert "ISO-8601" in err(fclient, '{createdAt: {gt: "not-a-date"}}')


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
    # `every` also holds for an issue with no labels, as Linear's collection filters do
    assert ids(fclient, '{labels: {every: {name: {eq: "bug"}}}}') == ["DES-1", "ENG-2"]


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
    assert "not defined by type 'IssueFilter'" in r.json()["errors"][0]["message"]


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


def test_an_empty_labels_predicate_is_an_error_not_a_no_op(fclient):
    """`labels: {some: {}}` constrains nothing. Compiling it to an empty fragment would drop the
    whole filter and answer with the full corpus, so it is rejected instead."""
    assert "must constrain something" in err(fclient, "{labels: {some: {}}}")
    assert "needs `some` or `every`" in err(fclient, "{labels: {}}")


# --- response-shape assertions (were tests/test_fidelity.py) --------------------------------


def _linear_client(tmp_path):
    """``with _linear_client(p) as (client, settings):`` over LINEAR_CORPUS."""
    return corpus_client(tmp_path, LINEAR_CORPUS)


# --- Linear -----------------------------------------------------------------------
# Linear's auth is the one shape no other source in this repo uses: the personal API key is the
# BARE `Authorization` value with no scheme, while an OAuth access token is `Bearer <token>`, and
# the real API accepts both on the same header. Getting this wrong is silent — a stripped-scheme
# parse would accept `Bearer <key>` and reject the bare key that every real Linear client sends.

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


def _linear_identifiers(client, authorization):
    """``authorization`` verbatim, not a Bearer-wrapped token — these tests assert on the scheme."""
    r = gql(client, "{ issues { nodes { identifier } } }", {"Authorization": authorization})
    return r.status_code, r.json()


def test_linear_accepts_a_bare_api_key_with_no_scheme(tmp_path):
    """What `LinearReader` and `@linear/sdk` both send: `Authorization: <key>`, no prefix."""
    with _linear_client(tmp_path) as (client, settings):
        status, body = _linear_identifiers(client, settings.admin_token)
        assert status == 200
        assert [n["identifier"] for n in body["data"]["issues"]["nodes"]] == ["ENG-9"]


def test_linear_accepts_a_bearer_oauth_token(tmp_path):
    """The OAuth shape, on the same header."""
    with _linear_client(tmp_path) as (client, settings):
        status, body = _linear_identifiers(client, f"Bearer {settings.admin_token}")
        assert status == 200
        assert [n["identifier"] for n in body["data"]["issues"]["nodes"]] == ["ENG-9"]


def test_linear_rejects_a_stray_scheme_rather_than_stripping_it(tmp_path):
    """To the real API the WHOLE header value is the key, so `Token <key>` is simply a wrong key —
    not a key with a scheme to discard. Stripping the first word would authenticate a credential
    the real API refuses."""
    with _linear_client(tmp_path) as (client, settings):
        assert _linear_identifiers(client, f"Token {settings.admin_token}")[0] == 401


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
