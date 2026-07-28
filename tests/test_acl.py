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
