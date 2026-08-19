"""ACL resolution + visibility, asserted against the SAMPLE corpus's generated ACL."""

import yaml

from backlot import store
from tests._helpers import client_for, gql


def _visible(db, acl, token, source):
    """The SERVED keys this token can see in one source — the same tuples `keys` hands back, so an
    assertion names a document by its corpus id and compares what the API actually addresses."""
    ids = acl.visible_ids(db, acl.resolve(token))
    cols = store.id_columns(source)
    return {
        tuple(r[c] for c in cols)
        for r in store.list_documents(db, source, visible_ids=ids, limit=100)
    }


def test_admin_sees_all_confluence(db, acl, keys):
    assert acl.resolve("admin-service-token").is_admin
    assert _visible(db, acl, "admin-service-token", "confluence") == {
        keys["cf-handbook"],
        keys["cf-oncall"],
        keys["cf-comp"],
    }


def test_public_visible_to_everyone(db, acl, tokens, keys):
    # a public page is visible to any user, regardless of group
    assert keys["cf-handbook"] in _visible(db, acl, tokens["ava@acme.com"], "confluence")
    assert keys["cf-handbook"] in _visible(db, acl, tokens["mia@acme.com"], "confluence")


def test_group_restricted_hidden_from_nonmember(db, acl, tokens, keys):
    # ava is in engineering, not 'people' -> cannot see the people-only comp page
    assert _visible(db, acl, tokens["ava@acme.com"], "confluence") == {
        keys["cf-handbook"],
        keys["cf-oncall"],
    }


def test_group_restricted_visible_to_member(db, acl, tokens, keys):
    # hana is in 'people' -> sees the comp page
    assert keys["cf-comp"] in _visible(db, acl, tokens["hana@acme.com"], "confluence")


def test_private_doc_only_its_author(db, acl, tokens, keys):
    assert keys["jira-private"] in _visible(db, acl, tokens["bob@acme.com"], "jira")
    assert keys["jira-private"] not in _visible(db, acl, tokens["ava@acme.com"], "jira")


def test_unknown_token_resolves_to_none(acl):
    assert acl.resolve("nope") is None
    assert acl.resolve(None) is None


def test_forbidden_direct_fetch_is_hidden(db, acl, tokens, keys):
    ids = acl.visible_ids(db, acl.resolve(tokens["ava@acme.com"]))
    priv, pub = keys["jira-private"], keys["cf-handbook"]
    assert store.get_document(db, "jira", *priv, visible_ids=ids) is None  # hidden
    assert store.get_document(db, "confluence", *pub, visible_ids=ids) is not None  # public
    assert store.get_document(db, "jira", *priv, visible_ids=None) is not None  # admin bypass


def test_admin_visible_ids_is_none(db, acl):
    assert acl.visible_ids(db, acl.resolve("admin-service-token")) is None


# --- Linear ---------------------------------------------------------------------
# Linear's container is the team and its grants come from the shared `grants_for` path, so what
# needs asserting is that the GraphQL layer honours the same filter — including on the comment
# rows, which carry no grant of their own and inherit the parent issue's.


def test_linear_restricted_issue_hidden_from_nonreader(db, acl, tokens, keys):
    assert _visible(db, acl, tokens["ava@acme.com"], "linear") == {
        keys["lin-rl"],
        keys["lin-batch"],
        keys["lin-des"],
    }


def test_linear_restricted_issue_visible_to_its_reader(db, acl, tokens, keys):
    assert keys["lin-secret"] in _visible(db, acl, tokens["hana@acme.com"], "linear")


def test_linear_admin_sees_every_issue(db, acl, keys):
    assert _visible(db, acl, "admin-service-token", "linear") == {
        keys["lin-rl"],
        keys["lin-batch"],
        keys["lin-des"],
        keys["lin-secret"],
        keys["lin-blackops"],
    }


def test_linear_comments_inherit_the_parent_issues_acl(db, acl, tokens, keys):
    """A comment row has no ACL grant of its own — visibility is the issue's. Without the join in
    `list_linear_comments` a hidden issue's comments would leak through `Query.comments`."""
    from backlot import store as st

    ids = acl.visible_ids(db, acl.resolve(tokens["mia@acme.com"]))
    # mia sees the public issues, so she sees their comments...
    assert st.count_linear_comments(db, issue_id=keys["lin-rl"][0], visible_ids=ids) == 2
    # ...but not a restricted issue's.
    assert st.count_linear_comments(db, issue_id=keys["lin-secret"][0], visible_ids=ids) == 0


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
# roots read `linear_entities`, an UNFILTERED `DISTINCT` over every issue, because the entities have
# no table of their own — a project/cycle/state/label/assignee exists only as a column value on
# some issue. Left unscoped they hand a caller field values off rows they are
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


def test_fireflies_store_reads_are_acl_scoped(db, acl, tokens, keys):
    secret = keys["ff-secret"]
    assert secret in _visible(db, acl, "admin-service-token", "fireflies")  # admin
    assert secret in _visible(db, acl, tokens["hana@acme.com"], "fireflies")  # granted
    assert secret not in _visible(db, acl, tokens["ava@acme.com"], "fireflies")
    # the org-visible transcripts are readable by both
    for email in ("hana@acme.com", "ava@acme.com"):
        assert {keys["ff-discovery"], keys["ff-allhands"]} <= _visible(
            db, acl, tokens[email], "fireflies"
        )


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


def test_grants_are_written_to_their_own_source_table(tmp_path):
    """A grant belongs to one source's document. Two documents sharing a doc_id across sources
    must not share a grant — a union of the two makes a public page make a restricted
    drive file readable by anyone in the org."""
    from tests._helpers import build_corpus

    s = build_corpus(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "space": "handbook",
                "doc_id": "shared-1",
                "title": "Public page",
                "content": "anyone may read this",
                "author_email": "ava@acme.com",
                "visibility": "public",
                "group": "engineering",
                "author_groups": ["engineering"],
            },
            {
                "source_type": "google_drive",
                "folder": "vault",
                "doc_id": "shared-1",
                "title": "Secret sheet",
                "content": "hana only",
                "author_email": "hana@acme.com",
                "readers": ["hana@acme.com"],
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    # Each source's grants are read through its OWN table, keyed on its own served id -- the two
    # documents shared a corpus id and share nothing after import.
    page = conn.execute("SELECT id FROM confluence_pages").fetchone()["id"]
    file_id = conn.execute("SELECT id FROM gdrive_files").fetchone()["id"]
    conf = {
        r["principal_id"]
        for r in conn.execute("SELECT principal_id FROM confluence_acl WHERE id = ?", (page,))
    }
    drive = {
        r["principal_id"]
        for r in conn.execute("SELECT principal_id FROM google_drive_acl WHERE id = ?", (file_id,))
    }
    assert "hana@acme.com" in drive and "acme" not in drive
    assert "acme" in conf
    conn.close()


def test_a_shared_doc_id_does_not_share_visibility(tmp_path):
    """The defect the per-source ACL tables exist to remove: bob is in no granted group, and the
    drive file is readable only by hana. A confluence page sharing its corpus id must not grant
    the org. The two never even resolve to the same id -- each source assigns its own -- so this
    pins the property from both directions: separate tables AND separate id spaces."""
    from tests._helpers import build_corpus

    s = build_corpus(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "space": "handbook",
                "doc_id": "shared-1",
                "title": "Public page",
                "content": "anyone may read this",
                "author_email": "ava@acme.com",
                "visibility": "public",
                "group": "engineering",
                "author_groups": ["engineering"],
            },
            {
                "source_type": "google_drive",
                "folder": "vault",
                "doc_id": "shared-1",
                "title": "Secret sheet",
                "content": "hana only",
                "author_email": "hana@acme.com",
                "readers": ["hana@acme.com"],
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    bob = {"acme", "bob@acme.com", *store.user_group_ids(conn, "bob@acme.com")}
    titles = [r["title"] for r in store.list_documents(conn, "google_drive", None, bob, limit=10)]
    assert titles == []  # bob sees no drive document at all
    hana = {"acme", "hana@acme.com", *store.user_group_ids(conn, "hana@acme.com")}
    titles = [r["title"] for r in store.list_documents(conn, "google_drive", None, hana, limit=10)]
    assert titles == ["Secret sheet"]
    # doc_grants/docs_with_grants answer for ONE source's document: the drive row is hana-only
    # even though the confluence row that shared its corpus id is org-public.
    page = conn.execute("SELECT id FROM confluence_pages").fetchone()["id"]
    file_id = conn.execute("SELECT id FROM gdrive_files").fetchone()["id"]
    drive = {
        (r["principal_type"], r["principal_id"])
        for r in store.doc_grants(conn, "google_drive", file_id)
    }
    assert drive == {("user", "hana@acme.com")}
    assert store.docs_with_grants(conn, "google_drive", [file_id]) == {file_id}
    conf = {
        (r["principal_type"], r["principal_id"]) for r in store.doc_grants(conn, "confluence", page)
    }
    assert ("org", "acme") in conf
    conn.close()

    # Asserted at the ROUTER layer too, with a real HTTP request carrying a non-admin bearer token:
    # ava is a member of the org the public confluence grant names but holds no grant of her own on
    # the drive file, and a scoping that read the two sources' grants together would let her
    # through on the strength of the confluence one alone.
    toks = yaml.safe_load(s.tokens_path.read_text())
    ava_token = next(u["token"] for u in toks["users"] if u["email"] == "ava@acme.com")
    hana_token = next(u["token"] for u in toks["users"] if u["email"] == "hana@acme.com")
    with client_for(s) as client:
        r = client.get("/drive/v3/files", headers={"Authorization": f"Bearer {ava_token}"})
        assert [f["name"] for f in r.json()["files"]] == []
        r = client.get("/drive/v3/files", headers={"Authorization": f"Bearer {hana_token}"})
        assert "Secret sheet" in {f["name"] for f in r.json()["files"]}


def test_slack_channels_for_principals_reads_its_own_acl_table(tmp_path):
    """The principal-indexed lookup ``conversations.list`` falls back to while the channel-ACL
    cache is cold (see ``routers/slack.py``'s ``else`` branch). It must answer from ``slack_acl``,
    not some other source's table — a doc_id shared with a github item, granted to a DIFFERENT
    principal there, would leak (or hide) a channel if the query read the wrong table."""
    from tests._helpers import build_corpus

    s = build_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "channel": "eng-private",
                "doc_id": "collide-1",
                "content": "restricted to engineering",
                "author_email": "ava@acme.com",
                "readers": ["engineering"],
            },
            {
                "source_type": "github",
                "repo": "unrelated-repo",
                "doc_id": "collide-1",
                "title": "Unrelated issue",
                "content": "granted to a different group entirely",
                "author_email": "bob@acme.com",
                "readers": ["design"],
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    # granted via slack_acl -> the channel is found
    assert store.slack_channels_for_principals(conn, ["engineering"]) == {"eng-private"}
    # granted only in github_acl (same doc_id, different table) -> must NOT surface the channel
    assert store.slack_channels_for_principals(conn, ["design"]) == set()
    conn.close()


def test_container_has_public_reads_its_own_acl_table(tmp_path):
    """A cross-source doc_id collision must not leak a grant from one source's ACL into another
    source's ``container_has_public`` answer — the container-level analogue of the doc-id bug this
    plan exists to fix. A slack channel and a github repo share a doc_id here; the slack message is
    group-restricted and the github item is public, so the two sources must disagree."""
    from tests._helpers import build_corpus

    s = build_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "channel": "shared-name",
                "doc_id": "collide-2",
                "content": "restricted to engineering",
                "author_email": "ava@acme.com",
                "visibility": "group",
                "group": "engineering",
                "author_groups": ["engineering"],
            },
            {
                "source_type": "github",
                "repo": "collide-repo",
                "doc_id": "collide-2",
                "title": "Public issue",
                "content": "anyone may read this",
                "author_email": "bob@acme.com",
                "visibility": "public",
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    assert store.container_has_public(conn, "github", "collide-repo") is True
    assert store.container_has_public(conn, "slack", "shared-name") is False
    conn.close()
