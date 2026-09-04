"""Linear's GraphQL API over HTTP: the served schema, the resolvers, and the filter compiler.

The filter tests were their own file once. They are still their own SECTION below, and the reason
they exist in this shape is worth keeping: a mutation review found 16 of 17 injected faults in
`backlot/graphql/linear_filters.py` surviving the rest of the suite. A wrong filter returns
plausible-looking data rather than an error, so every comparator pins its BOUNDARY (`lte` vs `lt`),
not merely that it filters something.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlparse

import pytest
from graphql import build_client_schema, is_enum_type, parse, validate

from backlot import synth
from backlot.fidelity.graphql_diff import backlot_schema
from backlot.graphql import linear_filters, mcp_tools
from tests._helpers import (
    build_corpus,
    complete,
    client_for,
    corpus_client,
    db_count,
    selected_field_count,
    selected_fields,
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


def test_linear_missing_issue_is_a_field_error_not_a_400(client, admin_h):
    """Linear declares `issue` non-null, so a miss nulls `data` and reports an error — but the
    request itself was fine, so the status stays 200."""
    r = gql(client, '{ issue(id: "NOPE-1") { identifier } }', admin_h)
    assert r.status_code == 200
    assert r.json()["data"] is None
    assert "Entity not found" in r.json()["errors"][0]["message"]


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

    text = (REPO_ROOT / "examples/using-llamaindex-readers/linear.py").read_text()
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


# --- Linear relations / children / attachments / releases -----------------------


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
        "Unknown argument 'url' on field 'Issue.attachments'."
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
    message = body["errors"][0]["message"]
    assert message.startswith("Expected value of type 'DateTimeOrDuration', found ")
    assert message.endswith("into a valid date")


def test_a_date_operand_must_be_a_string(fclient):
    """Linear: `Unable to parse literal value of kind 'IntValue'. DateTimeOrDuration supports only
    'StringValue' ones` for a literal, and `... supports only string values` for a variable."""
    r = post(fclient, "{ issues(filter: {createdAt: {gt: 1700000000}}) { nodes { identifier } } }")
    assert r.status_code == 400
    assert "supports only 'StringValue' ones" in r.json()["errors"][0]["message"]
    q = "query($d: DateTimeOrDuration) { issues(filter: {createdAt: {gt: $d}}) { nodes { id } } }"
    r = post(fclient, q, {"d": 1700000000})
    assert r.status_code == 400
    assert r.json()["errors"][0]["message"].startswith("Variable '$d' got invalid value 1700000000")
    assert "supports only string values" in r.json()["errors"][0]["message"]
    r = post(fclient, q, {"d": "not-a-date"})
    assert r.status_code == 400
    assert "Expected type 'DateTimeOrDuration'" in r.json()["errors"][0]["message"]


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
    message = r.json()["errors"][0]["message"]
    assert message.startswith("Expected value of type 'DateTimeOrDuration', found ")
    assert message.endswith("into a valid date"), message
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
]


@pytest.mark.parametrize("literal,expected", _LABEL_CELLS, ids=[c[0] for c in _LABEL_CELLS])
def test_labels_quantifiers_answer_as_linear(fclient, literal, expected):
    """Linear pushes a negation outside the quantifier: `some` over a negative predicate is the
    complement of `every` over its positive form, so it answers an issue with no labels; and `every`
    over a positive predicate needs at least one label, so it does not. The textbook EXISTS and
    NOT EXISTS answer the opposite on both counts, which is what #112 measured over label-less
    issues. Each cell here is one measured answer; the mapping to the fixture is above
    `_LABEL_CELLS`."""
    assert ids(fclient, "{labels: %s}" % literal) == sorted(expected)


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


@pytest.mark.parametrize("literal,expected", _ISSUE_CELLS, ids=[c[0] for c in _ISSUE_CELLS])
def test_issue_filter_or_answers_a_vacuous_branch_as_linear(fclient, literal, expected):
    """An `or` branch that constrains nothing is not dropped by api.linear.app: it makes the whole
    `or` constrain nothing, so `{or: [{labels: {}}, X]}` answers every issue where X alone answers
    a few. Dropping it, which the empty fragment invites, is a refusal turned into a quietly
    narrower answer. A branch with nothing in it IS dropped, and a null operand is a condition of
    its own. Each cell is one measured answer; the stand-ins are above `_ISSUE_CELLS`."""
    assert ids(fclient, literal) == sorted(expected)


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


@pytest.mark.parametrize(
    "literal,expected", _RELATION_NULL_CELLS, ids=[c[0] for c in _RELATION_NULL_CELLS]
)
def test_relation_null_true_reads_nothing_else_in_the_object_as_linear(fclient, literal, expected):
    """`null: true` on a nullable relation is not one condition among the object's keys: Linear
    answers the issues without the relation whatever else the object says, where ANDing the keys
    answers none. `null: false` does AND with them. Each cell is one measured answer; the mapping
    to the fixture is above `_RELATION_NULL_CELLS`."""
    assert ids(fclient, literal) == sorted(expected)


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
    # the added rows vanish when the comparator branch carries `null: false`, or when the `or` is not the object's own
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
    # a `null: true` below an object with a comparator key of its own, or in an `and` branch beside its own `or`, is the issues without; the object's own `null` wins
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
    # a `null: true` anywhere below the object adds the issues without the relation to whatever the REST of the object answers, so a sibling key or a second group still reads
    ("project", '{or: [{null: true}, {name: {eq: "zz-pa"}}], name: {eq: "zz-pa"}}', {W1, N3, N4}),
    (
        "project",
        '{or: [{null: true}, {name: {eq: "zz-pa"}}], id: {eq: "%s"}}' % (_ZZ_PA,),
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
    # a bare `{null: false}` in an `or` lifted from an `and` branch requires the relation for the whole object, unless that `or` carries a `null: true`
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
    # the object's own `or` is not an `and` branch: its bare `{null: false}` is the missing-relation alternative even beside a sibling key
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
    (
        "project",
        '{or: [{null: false}, {name: {neq: "zz-pa"}}], and: [{or: [{null: false}, {name: {neq: "zz-pa"}}]}]}',
        {W2},
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


@pytest.mark.parametrize(
    "relation,literal,expected",
    _RELATION_OR_CELLS,
    ids=[f"{r}: {c}" for r, c, _ in _RELATION_OR_CELLS],
)
def test_relation_or_reads_a_branch_as_linear(rclient, relation, literal, expected):
    """An `or` on a nullable relation is not the union of its branches each read as an object:
    the keys of one branch are alternatives, a branch that says nothing about the related row
    beside one that does adds the issues without the relation, and every branch carrying
    `null: true` is those issues alone. The full rule is on `_sub_filter`; each cell is one
    measured answer, the mapping to the fixture above `_RELATION_OR_CORPUS`."""
    assert ids(rclient, "{%s: %s}" % (relation, literal)) == sorted(expected)


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
# Every type below is what introspection of https://api.linear.app/graphql reported on 2026-09-03,
# copied here as literals so the suite runs offline; `backlot diff --source linear` is the live
# re-measurement and `backlot/fidelity/baseline/linear.json` the record of the gaps it accepts.
# Each entry was a `breaking` finding in #101: Backlot declared the same name at a different type,
# or declared a name the vendor does not have. The behaviour tests after the tables pin that the
# resolvers PRODUCE the corrected types -- a changed declaration on its own is not a fix.

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
    (measured 2026-09-03). Seven of the eight retyped counts are reachable from an issue;
    `ReleaseNote.releaseCount` is not, because no corpus produces a release note."""
    r = post(
        fclient,
        """{ issue(id: "ENG-2") {
            customerTicketCount
            sharedAccess { sharedWithCount }
            project { priority }
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
        "Team.issueCount": d["team"]["issueCount"],
        "Team.ledInitiativeCount": d["team"]["ledInitiativeCount"],
        "User.createdIssueCount": d["creator"]["createdIssueCount"],
        "Release.issueCount": d["releases"]["nodes"][0]["issueCount"],
    }
    not_int = {k: v for k, v in counts.items() if type(v) is not int}
    assert not not_int, not_int


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
            "Field 'branchName' is not defined by type 'IssueFilter'.",
        ),
        (
            '{ issue(id: "ENG-2") { releases(filter: {slugId: {eq: "x"}}) { nodes { id } } } }',
            "Field 'slugId' is not defined by type 'ReleaseFilter'.",
        ),
        (
            "{ users(sort: [{createdAt: {order: Descending}}]) { nodes { id } } }",
            "Field 'createdAt' is not defined by type 'UserSortInput'.",
        ),
        (
            "{ users(sort: [{updatedAt: {order: Descending}}]) { nodes { id } } }",
            "Field 'updatedAt' is not defined by type 'UserSortInput'.",
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
        "Expected value of type 'TimelessDateOrDuration', found "
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
    is UNIQUE. Placed after every `client`-fixture test in this module, not beside its sibling
    `issue(id:)` tests above: it opens a SECOND app over a different DB via `corpus_client`,
    which overwrites the module-scoped `client` fixture's shared `app.state` (see
    `tests._helpers.client_for`'s docstring) -- fine here because nothing after it needs `client`."""
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
