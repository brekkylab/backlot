"""ACL resolution + visibility, asserted against the SAMPLE corpus's generated ACL."""

from backlot import store
from tests._helpers import client_for, gql


def _visible(db, acl, token, source):
    ids = acl.visible_ids(db, acl.resolve(token))
    return {r["doc_id"] for r in store.list_documents(db, source, visible_ids=ids, limit=100)}


def test_admin_sees_all_confluence(db, acl):
    assert acl.resolve("admin-service-token").is_admin
    assert _visible(db, acl, "admin-service-token", "confluence") == {
        "cf-handbook",
        "cf-oncall",
        "cf-comp",
    }


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
    assert store.get_document(db, "jira", "jira-private", visible_ids=ids) is None  # hidden
    assert (
        store.get_document(db, "confluence", "cf-handbook", visible_ids=ids) is not None
    )  # public
    assert (
        store.get_document(db, "jira", "jira-private", visible_ids=None) is not None
    )  # admin bypass


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
        "lin-rl",
        "lin-batch",
        "lin-des",
        "lin-secret",
        "lin-blackops",
    }


def test_linear_comments_inherit_the_parent_issues_acl(db, acl, tokens):
    """A comment row has no ACL grant of its own — visibility is the issue's. Without the join in
    `list_linear_comments` a hidden issue's comments would leak through `Query.comments`."""
    from backlot import store as st

    ids = acl.visible_ids(db, acl.resolve(tokens["mia@acme.com"]))
    # mia sees the public issues, so she sees their comments...
    assert st.count_linear_comments(db, doc_id="lin-rl", visible_ids=ids) == 2
    # ...but not a restricted issue's.
    assert st.count_linear_comments(db, doc_id="lin-secret", visible_ids=ids) == 0


def test_linear_team_counts_are_acl_scoped(db, acl, tokens):
    from backlot import store as st

    ava = acl.visible_ids(db, acl.resolve(tokens["ava@acme.com"]))
    assert st.linear_team_issue_counts(db, visible_ids=ava) == {"engineering": 2, "design": 1}
    assert st.linear_team_issue_counts(db, visible_ids=None) == {
        "engineering": 3,
        "design": 1,
        "blackops": 1,
    }


# --- Linear: the by-id relation roots ---------------------------------------------
# `@linear/sdk` resolves relations lazily, so `await issue.project` fires `project(id:)`. Those
# roots read a reverse index built at startup from an UNFILTERED `DISTINCT` over every issue, and
# the entities have no table of their own — a project/cycle/state/label/assignee exists only as a
# column value on some issue. Left unscoped they hand a caller field values off rows they are
# denied, and because the ids are pure functions of the name (backlot/synth.py), they are computable
# offline: an enumerable oracle, not merely a confirmable one.


def _gql(client, query, token):
    return gql(client, "/linear/graphql", query, token).json()


def test_linear_by_id_roots_do_not_leak_entities_off_hidden_issues(sample_settings, tokens):
    """`lin-secret` is granted to hana only. Its state ("Backlog") is shared with nothing else in
    the corpus, so resolving it by id must fail for ava exactly as an absent id would — and must
    still work for hana, proving the id is real and the difference is the ACL."""
    from backlot import synth

    with client_for(sample_settings) as client:
        state_id = synth.linear_state_id("Backlog", "engineering")
        q = '{ workflowState(id: "%s") { name } }' % state_id
        hidden = _gql(client, q, tokens["ava@acme.com"])
        granted = _gql(client, q, tokens["hana@acme.com"])
        assert "data" not in hidden or hidden["data"] is None
        assert "Entity not found" in hidden["errors"][0]["message"]
        assert granted["data"]["workflowState"]["name"] == "Backlog"

        # ...and indistinguishable from an id that genuinely does not exist.
        absent = _gql(
            client,
            '{ workflowState(id: "%s") { name } }'
            % synth.linear_state_id("No Such State", "engineering"),
            tokens["ava@acme.com"],
        )
        assert (
            absent["errors"][0]["message"].split("id=")[0]
            == hidden["errors"][0]["message"].split("id=")[0]
        )


def test_linear_by_id_roots_still_answer_for_visible_entities(sample_settings, tokens):
    """The scoping must not break the SDK: its lazy accessors only fire these for entities hanging
    off an issue it just read, so every one of them has to keep resolving."""
    from backlot import synth

    with client_for(sample_settings) as client:
        ava = tokens["ava@acme.com"]  # can read lin-rl (public)
        assert (
            _gql(
                client,
                '{ project(id: "%s") { name } }' % synth.linear_project_id("runtime-stability"),
                ava,
            )["data"]["project"]["name"]
            == "runtime-stability"
        )
        assert (
            _gql(
                client, '{ issueLabel(id: "%s") { name } }' % synth.linear_label_id("gateway"), ava
            )["data"]["issueLabel"]["name"]
            == "gateway"
        )
        assert (
            _gql(
                client,
                '{ cycle(id: "%s") { name } }' % synth.linear_cycle_id("2025-W08", "engineering"),
                ava,
            )["data"]["cycle"]["name"]
            == "2025-W08"
        )
        assert (
            _gql(
                client,
                '{ workflowState(id: "%s") { name } }'
                % synth.linear_state_id("In Progress", "engineering"),
                ava,
            )["data"]["workflowState"]["name"]
            == "In Progress"
        )
        assert (
            _gql(
                client, '{ user(id: "%s") { email } }' % synth.linear_user_id("bob@acme.com"), ava
            )["data"]["user"]["email"]
            == "bob@acme.com"
        )
        assert _gql(client, '{ team(id: "ENG") { key } }', ava)["data"]["team"]["key"] == "ENG"


def test_linear_team_by_id_agrees_with_the_teams_listing(sample_settings, tokens):
    """`teams` omits a team the caller sees no issue in; `team(id:)` must not then confirm it.

    Asserts on the team that IS hidden rather than branching on what happens to be listed — an
    earlier version did `if key in listed: ... else: <the real assertion>`, and since the caller
    saw every team the assertion never executed. Deleting `resolve_team`'s visibility check left
    it green."""
    with client_for(sample_settings) as client:
        ava = tokens["ava@acme.com"]  # engineering; `blackops` is granted to hana only
        listed = {
            t["key"]
            for t in _gql(client, "{ teams { nodes { key } } }", ava)["data"]["teams"]["nodes"]
        }
        assert "BLA" not in listed, "precondition: blackops must be hidden from ava"
        assert "ENG" in listed
        hidden = _gql(client, '{ team(id: "BLA") { key name } }', ava)
        assert hidden.get("data") is None
        assert "Entity not found" in hidden["errors"][0]["message"]
        # ...and hana, who is granted it, still gets it — so the above is the ACL, not a break.
        assert (
            _gql(client, '{ team(id: "BLA") { key } }', tokens["hana@acme.com"])["data"]["team"][
                "key"
            ]
            == "BLA"
        )
        # the container-name and UUID spellings are scoped too, not just the key
        assert (
            "Entity not found"
            in _gql(client, '{ team(id: "blackops") { key } }', ava)["errors"][0]["message"]
        )


def test_linear_every_by_id_predicate_is_scoped_not_just_the_dispatch(sample_settings, tokens):
    """Each of the five entity predicates gets its own hidden entity.

    `lin-secret` carries a project, cycle, label and assignee that exist on no other issue, so a
    predicate that matches too broadly (or drops half its condition) is caught here. Previously
    only `state` and `creator` were reachable, and the other four could be broken silently."""
    from backlot import synth

    with client_for(sample_settings) as client:
        ava, hana = tokens["ava@acme.com"], tokens["hana@acme.com"]
        cases = [
            (
                "project",
                '{ project(id: "%s") { name } }' % synth.linear_project_id("vault-rotation"),
            ),
            (
                "cycle",
                '{ cycle(id: "%s") { name } }'
                % synth.linear_cycle_id("2026-W40-embargo", "engineering"),
            ),
            (
                "label",
                '{ issueLabel(id: "%s") { name } }' % synth.linear_label_id("restricted-only"),
            ),
            (
                "assignee",
                '{ user(id: "%s") { email } }' % synth.linear_user_id("vault.keeper@acme.com"),
            ),
            (
                "state",
                '{ workflowState(id: "%s") { name } }'
                % synth.linear_state_id("Backlog", "engineering"),
            ),
        ]
        for kind, query in cases:
            denied, granted = _gql(client, query, ava), _gql(client, query, hana)
            assert "Entity not found" in denied["errors"][0]["message"], f"{kind} leaked to ava"
            assert granted.get("errors") is None, f"{kind} wrongly denied to its reader: {granted}"


def test_linear_hidden_assignee_is_not_nameable_by_id(sample_settings, tokens):
    """The sharpest form: a person who appears ONLY as the assignee of a hidden issue is absent
    from the caller's `users` directory, so `user(id:)` must not name them either."""
    from backlot import synth

    with client_for(sample_settings) as client:
        ava = tokens["ava@acme.com"]
        directory = {
            u["email"]
            for u in _gql(client, "{ users(first: 100) { nodes { email } } }", ava)["data"][
                "users"
            ]["nodes"]
        }
        # hana authors only lin-secret, which ava cannot read.
        visible = {
            n["identifier"]
            for n in _gql(client, "{ issues { nodes { identifier } } }", ava)["data"]["issues"][
                "nodes"
            ]
        }
        assert visible == {"ENG-101", "ENG-102", "DES-77"}  # ENG-103 (lin-secret) is hidden
        got = _gql(
            client, '{ user(id: "%s") { email } }' % synth.linear_user_id("hana@acme.com"), ava
        )
        # `users` is the corpus-wide principal directory (as in real Linear and the Notion
        # router), so hana IS listed there — the point is that the by-id root must not become a
        # SECOND, unscoped way to reach someone, so it agrees with the issue-level ACL instead.
        assert "hana@acme.com" in directory
        assert "Entity not found" in got["errors"][0]["message"]


# --- fireflies ------------------------------------------------------------------
# `ff-secret` is granted to hana only, and it is the sole transcript in the `board` channel, so
# both the unfiltered list and every filter have to agree about hiding it.


def _ff_gql(client, query, token, **variables):
    return gql(client, "/fireflies/graphql", query, f"Bearer {token}", **variables).json()


def test_fireflies_store_reads_are_acl_scoped(db, acl, tokens):
    assert "ff-secret" in _visible(db, acl, "admin-service-token", "fireflies")  # admin
    assert "ff-secret" in _visible(db, acl, tokens["hana@acme.com"], "fireflies")  # granted
    assert "ff-secret" not in _visible(db, acl, tokens["ava@acme.com"], "fireflies")
    # the org-visible transcripts are readable by both
    for email in ("hana@acme.com", "ava@acme.com"):
        assert {"ff-discovery", "ff-allhands"} <= _visible(db, acl, tokens[email], "fireflies")


def test_fireflies_transcripts_list_hides_denied_meetings(sample_settings, tokens):
    with client_for(sample_settings) as client:
        q = "{ transcripts(limit: 50) { title } }"
        ava = _ff_gql(client, q, tokens["ava@acme.com"])["data"]["transcripts"]
        hana = _ff_gql(client, q, tokens["hana@acme.com"])["data"]["transcripts"]
        assert "Board pre-read walkthrough" not in [t["title"] for t in ava]
        assert "Board pre-read walkthrough" in [t["title"] for t in hana]


def test_fireflies_transcript_by_id_denies_rather_than_reveals(sample_settings, tokens):
    """A transcript the caller may not read must be indistinguishable from one that does not
    exist — the id is a pure function of the doc_id (backlot/synth.py), so it is computable offline
    and a different error would confirm the meeting exists."""
    from backlot import synth

    with client_for(sample_settings) as client:
        q = "query($i:String!){ transcript(id:$i) { title } }"
        tid = synth.fireflies_id("ff-secret")
        assert _ff_gql(client, q, tokens["ava@acme.com"], i=tid)["data"]["transcript"] is None
        granted = _ff_gql(client, q, tokens["hana@acme.com"], i=tid)["data"]["transcript"]
        assert granted["title"] == "Board pre-read walkthrough"  # the id IS real
        # an absent id looks exactly the same to the denied caller
        assert (
            _ff_gql(client, q, tokens["ava@acme.com"], i="deadbeefdeadbeefdeadbeef")["data"][
                "transcript"
            ]
            is None
        )


def test_fireflies_filters_do_not_leak_a_denied_meeting(sample_settings, tokens):
    """Every narrowing argument goes through the same ACL clause: a filter that a hidden
    transcript is the ONLY match for must return nothing, not the hidden row."""
    with client_for(sample_settings) as client:
        for args in (
            'channel_id: "board"',  # its channel alone
            'host_email: "hana@acme.com"',  # its host
            'keyword: "stays in the room", scope: "sentences"',  # its own sentence
            'keyword: "Board pre-read", scope: "title"',
        ):
            q = "{ transcripts(%s, limit: 50) { title } }" % args
            ava = _ff_gql(client, q, tokens["ava@acme.com"])["data"]["transcripts"]
            assert "Board pre-read walkthrough" not in [t["title"] for t in ava], args
            hana = _ff_gql(client, q, tokens["hana@acme.com"])["data"]["transcripts"]
            assert "Board pre-read walkthrough" in [t["title"] for t in hana], args


def test_fireflies_sentences_of_a_denied_meeting_are_unreachable(sample_settings, tokens):
    """Sentences are fetched off a transcript the caller was already cleared for, so denying the
    parent is what protects them — this pins that there is no second path to the text."""
    with client_for(sample_settings) as client:
        q = "{ transcripts(limit: 50) { sentences { text } } }"
        ava = _ff_gql(client, q, tokens["ava@acme.com"])["data"]["transcripts"]
        said = [s["text"] for t in ava for s in t["sentences"]]
        assert not any("stays in the room" in s for s in said)


def test_fireflies_mine_is_scoped_to_the_calling_user(sample_settings, tokens):
    """`mine` means the caller's OWN meetings. The caller's address is the only identity the
    server can vouch for, so it must never widen to everyone's."""
    with client_for(sample_settings) as client:
        q = "{ transcripts(mine: true, limit: 50) { title host_email } }"
        ava = _ff_gql(client, q, tokens["ava@acme.com"])["data"]["transcripts"]
        assert [t["title"] for t in ava] == ["Acme x Northwind — latency discovery"]
        hana = _ff_gql(client, q, tokens["hana@acme.com"])["data"]["transcripts"]
        assert {t["host_email"] for t in hana} == {"hana@acme.com"}


def test_fireflies_mine_returns_nothing_for_a_token_that_is_not_a_person(sample_settings):
    """An admin/service token has no user, so "my meetings" is empty rather than all of them."""
    with client_for(sample_settings) as client:
        got = _ff_gql(
            client, "{ transcripts(mine: true, limit: 50) { title } }", sample_settings.admin_token
        )
        assert got["data"]["transcripts"] == []
