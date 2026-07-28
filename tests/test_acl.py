"""ACL resolution + visibility, asserted against the SAMPLE corpus's generated ACL."""
from app import store


def _visible(db, acl, token, source):
    ids = acl.visible_ids(db, acl.resolve(token))
    return {r["doc_id"] for r in store.list_documents(db, source, visible_ids=ids, limit=100)}


def test_admin_sees_all_confluence(db, acl):
    assert acl.resolve("admin-service-token").is_admin
    assert _visible(db, acl, "admin-service-token", "confluence") == {"cf-handbook", "cf-oncall", "cf-comp"}


def test_public_visible_to_everyone(db, acl, tokens):
    # a public page is visible to any user, regardless of group
    assert "cf-handbook" in _visible(db, acl, tokens["ava@acme.com"], "confluence")
    assert "cf-handbook" in _visible(db, acl, tokens["mia@acme.com"], "confluence")


def test_group_restricted_hidden_from_nonmember(db, acl, tokens):
    # ava is in engineering, not 'people' -> cannot see the people-only comp page
    assert _visible(db, acl, tokens["ava@acme.com"], "confluence") == {"cf-handbook", "cf-oncall"}


def test_group_restricted_visible_to_member(db, acl, tokens):
    # hana is in 'people' -> sees the comp page
    assert "cf-comp" in _visible(db, acl, tokens["hana@acme.com"], "confluence")


def test_private_doc_only_its_author(db, acl, tokens):
    assert "jira-private" in _visible(db, acl, tokens["bob@acme.com"], "jira")
    assert "jira-private" not in _visible(db, acl, tokens["ava@acme.com"], "jira")


def test_unknown_token_resolves_to_none(acl):
    assert acl.resolve("nope") is None
    assert acl.resolve(None) is None


def test_forbidden_direct_fetch_is_hidden(db, acl, tokens):
    ids = acl.visible_ids(db, acl.resolve(tokens["ava@acme.com"]))
    assert store.get_document(db, "jira", "jira-private", visible_ids=ids) is None      # hidden
    assert store.get_document(db, "confluence", "cf-handbook", visible_ids=ids) is not None  # public
    assert store.get_document(db, "jira", "jira-private", visible_ids=None) is not None  # admin bypass


def test_admin_visible_ids_is_none(db, acl):
    assert acl.visible_ids(db, acl.resolve("admin-service-token")) is None


# --- Linear ---------------------------------------------------------------------
# Linear's container is the team and its grants come from the shared `grants_for` path, so what
# needs asserting is that the GraphQL layer honours the same filter — including on the comment
# rows, which carry no grant of their own and inherit the parent issue's.

def test_linear_restricted_issue_hidden_from_nonreader(db, acl, tokens):
    assert _visible(db, acl, tokens["ava@acme.com"], "linear") == {"lin-rl", "lin-batch", "lin-des"}


def test_linear_restricted_issue_visible_to_its_reader(db, acl, tokens):
    assert "lin-secret" in _visible(db, acl, tokens["hana@acme.com"], "linear")


def test_linear_admin_sees_every_issue(db, acl):
    assert _visible(db, acl, "admin-service-token", "linear") == {
        "lin-rl", "lin-batch", "lin-des", "lin-secret"}


def test_linear_comments_inherit_the_parent_issues_acl(db, acl, tokens):
    """A comment row has no ACL grant of its own — visibility is the issue's. Without the join in
    `list_linear_comments` a hidden issue's comments would leak through `Query.comments`."""
    from app import store as st
    ids = acl.visible_ids(db, acl.resolve(tokens["mia@acme.com"]))
    # mia sees the public issues, so she sees their comments...
    assert st.count_linear_comments(db, doc_id="lin-rl", visible_ids=ids) == 2
    # ...but not a restricted issue's.
    assert st.count_linear_comments(db, doc_id="lin-secret", visible_ids=ids) == 0


def test_linear_team_counts_are_acl_scoped(db, acl, tokens):
    from app import store as st
    ava = acl.visible_ids(db, acl.resolve(tokens["ava@acme.com"]))
    assert st.linear_team_issue_counts(db, visible_ids=ava) == {"engineering": 2, "design": 1}
    assert st.linear_team_issue_counts(db, visible_ids=None) == {"engineering": 3, "design": 1}


# --- Linear: the by-id relation roots ---------------------------------------------
# `@linear/sdk` resolves relations lazily, so `await issue.project` fires `project(id:)`. Those
# roots read a reverse index built at startup from an UNFILTERED `DISTINCT` over every issue, and
# the entities have no table of their own — a project/cycle/state/label/assignee exists only as a
# column value on some issue. Left unscoped they hand a caller field values off rows they are
# denied, and because the ids are pure functions of the name (app/synth.py), they are computable
# offline: an enumerable oracle, not merely a confirmable one.

def _linear_client(sample_settings):
    import os

    from starlette.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    prev = os.environ.get("MOCK_DATA_DIR")
    os.environ["MOCK_DATA_DIR"] = str(sample_settings.data_dir)
    get_settings.cache_clear()
    client = TestClient(app)
    client.__enter__()

    def close():
        client.__exit__(None, None, None)
        get_settings.cache_clear()
        if prev is None:
            os.environ.pop("MOCK_DATA_DIR", None)
        else:
            os.environ["MOCK_DATA_DIR"] = prev

    return client, close


def _gql(client, query, token):
    return client.post("/linear/graphql", json={"query": query},
                       headers={"Authorization": token}).json()


def test_linear_by_id_roots_do_not_leak_entities_off_hidden_issues(sample_settings, tokens):
    """`lin-secret` is granted to hana only. Its state ("Backlog") is shared with nothing else in
    the corpus, so resolving it by id must fail for ava exactly as an absent id would — and must
    still work for hana, proving the id is real and the difference is the ACL."""
    from app import synth

    client, close = _linear_client(sample_settings)
    try:
        state_id = synth.linear_state_id("Backlog", "engineering")
        q = '{ workflowState(id: "%s") { name } }' % state_id
        hidden = _gql(client, q, tokens["ava@acme.com"])
        granted = _gql(client, q, tokens["hana@acme.com"])
        assert "data" not in hidden or hidden["data"] is None
        assert "Entity not found" in hidden["errors"][0]["message"]
        assert granted["data"]["workflowState"]["name"] == "Backlog"

        # ...and indistinguishable from an id that genuinely does not exist.
        absent = _gql(client, '{ workflowState(id: "%s") { name } }'
                      % synth.linear_state_id("No Such State", "engineering"),
                      tokens["ava@acme.com"])
        assert absent["errors"][0]["message"].split("id=")[0] == \
            hidden["errors"][0]["message"].split("id=")[0]
    finally:
        close()


def test_linear_by_id_roots_still_answer_for_visible_entities(sample_settings, tokens):
    """The scoping must not break the SDK: its lazy accessors only fire these for entities hanging
    off an issue it just read, so every one of them has to keep resolving."""
    from app import synth

    client, close = _linear_client(sample_settings)
    try:
        ava = tokens["ava@acme.com"]        # can read lin-rl (public)
        assert _gql(client, '{ project(id: "%s") { name } }'
                    % synth.linear_project_id("runtime-stability"),
                    ava)["data"]["project"]["name"] == "runtime-stability"
        assert _gql(client, '{ issueLabel(id: "%s") { name } }' % synth.linear_label_id("gateway"),
                    ava)["data"]["issueLabel"]["name"] == "gateway"
        assert _gql(client, '{ cycle(id: "%s") { name } }'
                    % synth.linear_cycle_id("2025-W08", "engineering"),
                    ava)["data"]["cycle"]["name"] == "2025-W08"
        assert _gql(client, '{ workflowState(id: "%s") { name } }'
                    % synth.linear_state_id("In Progress", "engineering"),
                    ava)["data"]["workflowState"]["name"] == "In Progress"
        assert _gql(client, '{ user(id: "%s") { email } }' % synth.linear_user_id("bob@acme.com"),
                    ava)["data"]["user"]["email"] == "bob@acme.com"
        assert _gql(client, '{ team(id: "ENG") { key } }', ava)["data"]["team"]["key"] == "ENG"
    finally:
        close()


def test_linear_team_by_id_agrees_with_the_teams_listing(sample_settings, tokens):
    """`teams` omits a team the caller sees no issue in; `team(id:)` must not then confirm it.
    mia is in `marketing` — she authors nothing in Linear, so she sees no team at all."""
    client, close = _linear_client(sample_settings)
    try:
        mia = tokens["mia@acme.com"]
        listed = {t["key"] for t in _gql(client, "{ teams { nodes { key } } }",
                                         mia)["data"]["teams"]["nodes"]}
        for key in ("ENG", "DES"):
            byid = _gql(client, '{ team(id: "%s") { key } }' % key, mia)
            if key in listed:
                assert byid["data"]["team"]["key"] == key
            else:
                assert "Entity not found" in byid["errors"][0]["message"], \
                    f"team(id: {key!r}) confirmed a team `teams` hid"
    finally:
        close()


def test_linear_hidden_assignee_is_not_nameable_by_id(sample_settings, tokens):
    """The sharpest form: a person who appears ONLY as the assignee of a hidden issue is absent
    from the caller's `users` directory, so `user(id:)` must not name them either."""
    from app import synth

    client, close = _linear_client(sample_settings)
    try:
        ava = tokens["ava@acme.com"]
        directory = {u["email"] for u in _gql(client, "{ users(first: 100) { nodes { email } } }",
                                              ava)["data"]["users"]["nodes"]}
        # hana authors only lin-secret, which ava cannot read.
        visible = {n["identifier"] for n in _gql(client, "{ issues { nodes { identifier } } }",
                                                 ava)["data"]["issues"]["nodes"]}
        assert visible == {"ENG-101", "ENG-102", "DES-77"}   # ENG-103 (lin-secret) is hidden
        got = _gql(client, '{ user(id: "%s") { email } }' % synth.linear_user_id("hana@acme.com"),
                   ava)
        # `users` is the corpus-wide principal directory (as in real Linear and the Notion
        # router), so hana IS listed there — the point is that the by-id root must not become a
        # SECOND, unscoped way to reach someone, so it agrees with the issue-level ACL instead.
        assert "hana@acme.com" in directory
        assert "Entity not found" in got["errors"][0]["message"]
    finally:
        close()
