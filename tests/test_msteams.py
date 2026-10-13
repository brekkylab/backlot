"""Microsoft Teams over Microsoft Graph: teams, channels, messages, members and the directory.

One file per router, so a source's shape assertions live in one place. The shapes asserted here
are the ones the vendor's v1.0 reference and its worked examples publish — see the sourcing note
in ``backlot.routers.msteams``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from backlot import store, synth
from tests._helpers import corpus_client, db_count, tok

V1 = "/msgraph/v1.0"


def test_graph_me_serves_the_authenticated_identity(client, admin_h):
    """Regression first observed against upstream: the missing route answered 404."""
    response = client.get(f"{V1}/me", headers=admin_h)
    assert response.status_code == 200, response.text
    assert response.json()["userPrincipalName"]


@pytest.mark.parametrize("visibility", ["public", "private"])
def test_slack_deactivation_does_not_remove_a_teams_member(tmp_path, visibility):
    """The generic membership SQL must preserve Slack's newer deactivation rule only for Slack."""
    from tests._helpers import build_corpus

    email = "ava@acme.com"
    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": source,
                "channel": "shared-name",
                "author_email": email,
                "visibility": visibility,
            }
            for source in ("slack", "msteams")
        ],
    )
    conn = store.connect_rw(settings.db_path)
    try:
        conn.execute("INSERT INTO slack_deactivated_users(email) VALUES (?)", (email,))
        for source, expected in (("slack", []), ("msteams", [email])):
            assert store.conversation_member_emails(conn, source, "shared-name") == expected
            assert store.count_conversation_members(conn, source, "shared-name") == len(expected)
            assert store.conversation_member_counts(conn, source)["shared-name"] == len(expected)
            assert store.conversation_has_author(conn, source, "shared-name", email) == bool(
                expected
            )
            assert store.conversation_membership_violations(conn, source) == []
    finally:
        conn.close()


@pytest.fixture(scope="module")
def team_id(client, admin_h) -> str:
    return client.get(f"{V1}/me/joinedTeams", headers=admin_h).json()["value"][0]["id"]


def _channels(client, headers, team) -> dict[str, str]:
    """displayName -> channel id, for the channels this caller can see."""
    body = client.get(f"{V1}/teams/{team}/channels", headers=headers).json()
    return {c["displayName"]: c["id"] for c in body["value"]}


# --- auth ------------------------------------------------------------------------


# Re-measured 2026-10-08: the three 401 messages and innerError shape below are from live
# graph.microsoft.com: authentication is answered before a tenant is needed, so an unauthenticated
# request gets a real response. See backlot.errors.msgraph for the transcript.


@pytest.mark.parametrize(
    "header,message",
    [
        (None, "Access token is empty."),
        ("Bearer", "UnableToParseTokens"),
        ("Bearer ", "UnableToParseTokens"),
        (
            "Bearer nope",
            "Protocol 'Bearer' failed to validate because The token could not be read.",
        ),
        # The scheme is not checked live: each of these reaches the JWT parser and fails there,
        # so none of them may be reported as a missing credential.
        (
            "bearer nope",
            "Protocol 'Bearer' failed to validate because The token could not be read.",
        ),
        (
            "BEARER nope",
            "Protocol 'Bearer' failed to validate because The token could not be read.",
        ),
        ("token nope", "Protocol 'Bearer' failed to validate because The token could not be read."),
        (
            "Basic bm9wZTpub3Bl",
            "Protocol 'Bearer' failed to validate because The token could not be read.",
        ),
        ("nope", "Protocol 'Bearer' failed to validate because The token could not be read."),
    ],
)
def test_every_way_a_credential_fails_answers_the_message_live_graph_answers(
    client, header, message
):
    r = client.get(f"{V1}/me", headers={"Authorization": header} if header else {})
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "InvalidAuthenticationToken"
    assert err["message"] == message


def test_an_error_is_a_graph_envelope_not_fastapis_detail(client, admin_h, team_id):
    """FastAPI's `{"detail": …}` is a body no Graph client parses: every one of them reads
    `error.code`. A 404 that carried `detail` was indistinguishable from a transport failure."""
    r = client.get(f"{V1}/teams/{team_id}/channels/19:nope@thread.tacv2", headers=admin_h)
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "NotFound"
    assert err["message"] == "No channel found with id 19:nope@thread.tacv2"
    assert err["innerError"]["code"] == "ItemNotFound"


def test_every_error_carries_the_inner_error_real_graph_sends(client):
    """Measured: `innerError` is on every 401, and it carries `date`, `request-id` and
    `client-request-id` — but no `code`. Backlot fills them because it IS the server answering, so
    an id it mints for its own request is the real value rather than a placeholder."""
    err = client.get(f"{V1}/me").json()["error"]
    inner = err["innerError"]
    assert set(inner) == {"date", "request-id", "client-request-id"}
    # The live format carries no timezone suffix and no fractional seconds.
    datetime.strptime(inner["date"], "%Y-%m-%dT%H:%M:%S")
    uuid.UUID(inner["request-id"])
    # A fresh id per request, which is what makes it usable for correlating one.
    again = client.get(f"{V1}/me").json()["error"]["innerError"]
    assert again["request-id"] != inner["request-id"]


# --- identity and the team -------------------------------------------------------


def test_me_answers_the_calling_user(client, tokens_yaml):
    me = client.get(
        f"{V1}/me", headers={"Authorization": f"Bearer {tok(tokens_yaml, 'bob@acme.com')}"}
    ).json()
    assert me["userPrincipalName"] == "bob@acme.com"
    assert me["id"] == synth.msteams_user_id("bob@acme.com")
    # The eleven properties Graph returns when no $select narrows the request — a client generated
    # from the vendor's schema binds to all of them, so a short object is a shape real never sends.
    assert set(me) - {"@odata.context"} == {
        "businessPhones",
        "displayName",
        "givenName",
        "jobTitle",
        "mail",
        "mobilePhone",
        "officeLocation",
        "preferredLanguage",
        "surname",
        "userPrincipalName",
        "id",
    }


def test_select_narrows_an_entity_but_never_drops_its_id(client, admin_h):
    """A client that lists with `$select` and then re-fetches each row has nothing to re-fetch by
    if the projection takes `id` with it, which is why Graph keeps it whatever was asked for."""
    body = client.get(f"{V1}/users", headers=admin_h, params={"$select": "displayName"}).json()
    assert body["value"], "the sample corpus has users"
    for user in body["value"]:
        assert set(user) == {"displayName", "id"}
    assert body["@odata.context"].endswith("#users(displayName)")


def test_joined_teams_is_the_one_team_the_corpus_models(client, admin_h):
    teams = client.get(f"{V1}/me/joinedTeams", headers=admin_h).json()["value"]
    assert len(teams) == 1
    # `joinedTeams` is documented as populating only id, displayName, description, isArchived and
    # tenantId, and returning every other property as null. A client written against that treats
    # the nulls as "call Get team"; filling them in would train it out of a round trip the real API
    # still needs.
    populated = {k for k, v in teams[0].items() if v is not None}
    assert populated == {"id", "displayName", "description", "isArchived", "tenantId"}


def test_a_request_for_another_team_is_not_found(client, admin_h):
    r = client.get(f"{V1}/teams/00000000-0000-0000-0000-000000000000/channels", headers=admin_h)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NotFound"


# --- channels --------------------------------------------------------------------


def test_layout_type_is_null_when_listing_and_set_when_getting_one(client, admin_h, team_id):
    """The vendor documents a known issue: **layoutType returns null when listing all channels**,
    and points at Get channel for it. A client that works around that has to keep having something
    to work around, so the two methods are deliberately allowed to disagree here."""
    listed = _channels(client, admin_h, team_id)
    assert listed, "the sample corpus has Teams channels"
    body = client.get(f"{V1}/teams/{team_id}/channels", headers=admin_h).json()
    assert all(c["layoutType"] is None for c in body["value"])

    one = client.get(f"{V1}/teams/{team_id}/channels/{listed['Platform']}", headers=admin_h).json()
    assert one["layoutType"] == "post"
    assert one["@odata.context"].endswith("/$entity")


def test_a_channel_granted_to_the_org_is_standard_and_a_narrower_one_is_private(
    client, admin_h, team_id
):
    body = client.get(f"{V1}/teams/{team_id}/channels", headers=admin_h).json()
    by_name = {c["displayName"]: c for c in body["value"]}
    assert by_name["Platform"]["membershipType"] == "standard"
    assert by_name["People Confidential"]["membershipType"] == "private"


def test_membership_type_filter_selects_and_an_unsupported_filter_is_refused(
    client, admin_h, team_id
):
    """`$filter` is the one OData parameter where ignoring it is worse than refusing it: a caller
    who asked for a subset and silently got everything has no way to tell."""
    private = client.get(
        f"{V1}/teams/{team_id}/channels",
        headers=admin_h,
        params={"$filter": "membershipType eq 'private'"},
    ).json()
    assert [c["displayName"] for c in private["value"]] == ["People Confidential"]

    refused = client.get(
        f"{V1}/teams/{team_id}/channels",
        headers=admin_h,
        params={"$filter": "displayName eq 'Platform'"},
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "badRequest"


def test_a_channel_the_caller_cannot_read_is_hidden_by_id_as_well_as_from_the_listing(
    client, admin_h, tokens_yaml, team_id
):
    """A channel id is otherwise enough for any authenticated principal to read a private channel's
    name and description and enumerate who is in it — none of which the ACL on its messages allows
    them to know."""
    confidential = _channels(client, admin_h, team_id)["People Confidential"]
    bob = {"Authorization": f"Bearer {tok(tokens_yaml, 'bob@acme.com')}"}
    assert "People Confidential" not in _channels(client, bob, team_id)
    for path in (
        f"{V1}/teams/{team_id}/channels/{confidential}",
        f"{V1}/teams/{team_id}/channels/{confidential}/messages",
        f"{V1}/teams/{team_id}/channels/{confidential}/members",
        f"{V1}/teams/{team_id}/channels/{confidential}/messages/1/replies",
    ):
        assert client.get(path, headers=bob).status_code == 404, path


# --- messages --------------------------------------------------------------------


def test_a_messages_id_is_its_created_time_in_milliseconds_and_its_etag(client, admin_h, team_id):
    """Backlot's synthetic ids encode milliseconds; the imported-message etag reuses the id.
    This tests the emulator convention, not a guarantee about Microsoft's opaque ids."""
    incidents = _channels(client, admin_h, team_id)["Incidents"]
    messages = client.get(
        f"{V1}/teams/{team_id}/channels/{incidents}/messages", headers=admin_h
    ).json()["value"]
    assert messages
    for m in messages:
        assert m["etag"] == m["id"]
        stamp = datetime.strptime(m["createdDateTime"], "%Y-%m-%dT%H:%M:%S.%f%z")
        assert round(stamp.replace(tzinfo=timezone.utc).timestamp() * 1000) == int(m["id"])


def test_a_channel_listing_returns_roots_only_and_replies_carry_their_parent(
    client, admin_h, team_id
):
    """ "The list of messages (without the replies)" — a reply reaches a client through the replies
    relationship, and it names its root in `replyToId` rather than repeating the thread."""
    incidents = _channels(client, admin_h, team_id)["Incidents"]
    roots = client.get(
        f"{V1}/teams/{team_id}/channels/{incidents}/messages", headers=admin_h
    ).json()["value"]
    assert all(m["replyToId"] is None for m in roots)

    root = next(m for m in roots if "502s" in m["body"]["content"])
    replies = client.get(
        f"{V1}/teams/{team_id}/channels/{incidents}/messages/{root['id']}/replies",
        headers=admin_h,
    ).json()
    assert replies["@odata.count"] == 2
    assert all(r["replyToId"] == root["id"] for r in replies["value"])
    # Newest first, which is the order the vendor's own example returns (Reply3, Reply2, Reply1).
    assert [r["id"] for r in replies["value"]] == sorted(
        (r["id"] for r in replies["value"]), reverse=True
    )


def test_expand_replies_folds_a_thread_into_one_response(client, admin_h, team_id):
    incidents = _channels(client, admin_h, team_id)["Incidents"]
    body = client.get(
        f"{V1}/teams/{team_id}/channels/{incidents}/messages",
        headers=admin_h,
        params={"$expand": "replies"},
    ).json()
    root = next(m for m in body["value"] if "502s" in m["body"]["content"])
    assert root["replies@odata.count"] == 2
    assert {r["body"]["content"] for r in root["replies"]} == {
        "Yeah, looking now.",
        "<p>Rolled back; 502s clearing.</p>",
    }
    # Without $expand the key is absent entirely, not present and empty — a client testing for it
    # is asking whether it expanded, not whether the thread has replies.
    plain = client.get(
        f"{V1}/teams/{team_id}/channels/{incidents}/messages", headers=admin_h
    ).json()
    assert all("replies" not in m for m in plain["value"])


def test_a_message_carries_the_per_message_fields_the_corpus_stated(client, admin_h, team_id):
    incidents = _channels(client, admin_h, team_id)["Incidents"]
    roots = client.get(
        f"{V1}/teams/{team_id}/channels/{incidents}/messages",
        headers=admin_h,
        params={"$expand": "replies"},
    ).json()["value"]
    root = next(m for m in roots if "502s" in m["body"]["content"])
    assert root["importance"] == "high"
    # Graph's metadata declares `reactionType`, `createdDateTime` and `user` non-nullable on a
    # chatMessageReaction, so a corpus states an ADDRESS and the router assembles the rest. Passing
    # the corpus's object straight through let a reaction with no `user` out — a shape the real
    # service cannot send.
    assert root["reactions"] == [
        {
            "reactionType": "like",
            "createdDateTime": "2026-02-10T18:01:00.000Z",
            "user": {
                "application": None,
                "device": None,
                "user": {
                    "id": synth.msteams_user_id("ava@acme.com"),
                    "displayName": "Ava",
                    "userIdentityType": "aadUser",
                },
            },
        }
    ]
    assert root["body"]["contentType"] == "text"
    assert root["from"]["user"]["userIdentityType"] == "aadUser"
    assert root["channelIdentity"] == {"teamId": team_id, "channelId": incidents}
    assert root["chatId"] is None  # a channel message is not a chat message

    edited = next(r for r in root["replies"] if r["body"]["contentType"] == "html")
    assert edited["lastEditedDateTime"] is not None
    assert edited["lastModifiedDateTime"] == edited["lastEditedDateTime"]
    unedited = next(r for r in root["replies"] if r["body"]["contentType"] == "text")
    assert unedited["lastEditedDateTime"] is None
    assert unedited["lastModifiedDateTime"] == unedited["createdDateTime"]


def test_get_message_resolves_a_reply_as_well_as_a_root(client, admin_h, team_id):
    """A client that got its id from a `replyToId` or a search hit passes a reply's id here, and
    Graph resolves this route against the whole channel."""
    incidents = _channels(client, admin_h, team_id)["Incidents"]
    base = f"{V1}/teams/{team_id}/channels/{incidents}/messages"
    root = next(
        m
        for m in client.get(base, headers=admin_h).json()["value"]
        if "502s" in m["body"]["content"]
    )
    reply = client.get(f"{base}/{root['id']}/replies", headers=admin_h).json()["value"][0]
    fetched = client.get(f"{base}/{reply['id']}", headers=admin_h).json()
    assert fetched["id"] == reply["id"] and fetched["replyToId"] == root["id"]
    assert client.get(f"{base}/9999999999999", headers=admin_h).status_code == 404


# --- pagination ------------------------------------------------------------------


def test_next_link_pages_a_channel_and_stops(client, admin_h, team_id, ro_conn):
    """The nextLink is absolute and carries the state a client needs; Graph omits the key entirely
    on the last page rather than sending an empty one."""
    incidents = _channels(client, admin_h, team_id)["Incidents"]
    url = f"{V1}/teams/{team_id}/channels/{incidents}/messages?$top=1"
    seen, pages = [], 0
    while url and pages < 10:
        body = client.get(url, headers=admin_h).json()
        seen += [m["id"] for m in body["value"]]
        url = body.get("@odata.nextLink")
        pages += 1
    assert url is None, "paging did not terminate"
    assert len(seen) == len(set(seen)), "a page repeated a message"
    total = store.count_msteams_top_level(ro_conn, "Incidents", None)
    assert len(seen) == total


def test_a_crawl_reaches_every_stored_message(client, admin_h, team_id, ro_conn):
    """Roots come from the channel listing and replies from the relationship, so the two together
    have to account for every row — a message reachable by neither is one no client can read."""
    found = set()
    for channel_id in _channels(client, admin_h, team_id).values():
        base = f"{V1}/teams/{team_id}/channels/{channel_id}/messages"
        url = f"{base}?$top=50"
        while url:
            body = client.get(url, headers=admin_h).json()
            for m in body["value"]:
                found.add(m["id"])
                replies = client.get(f"{base}/{m['id']}/replies", headers=admin_h).json()
                found.update(r["id"] for r in replies["value"])
            url = body.get("@odata.nextLink")
    assert len(found) == db_count(ro_conn, "msteams")


# --- members and the directory ---------------------------------------------------


def test_channel_members_are_its_speakers_and_carry_the_polymorphic_type(client, admin_h, team_id):
    """`conversationMember` is a polymorphic base, and a client deserializing the collection picks
    its concrete type from `@odata.type` — without it the SDKs cannot construct the object."""
    incidents = _channels(client, admin_h, team_id)["Incidents"]
    body = client.get(f"{V1}/teams/{team_id}/channels/{incidents}/members", headers=admin_h).json()
    assert {m["email"] for m in body["value"]} == {"ava@acme.com", "bob@acme.com"}
    for m in body["value"]:
        assert m["@odata.type"] == "#microsoft.graph.aadUserConversationMember"
        assert m["userId"] == synth.msteams_user_id(m["email"])
        assert m["roles"] == []
    assert body["@odata.count"] == len(body["value"])


def test_a_member_id_is_the_teams_and_users_ids_the_way_graph_encodes_it(client, admin_h, team_id):
    """Microsoft says to treat the id as opaque, and nothing here parses it — but a real one
    decodes to `<teamId>##<userId>`, and a value that does not is a difference someone diffing a
    recorded response has to explain."""
    import base64

    incidents = _channels(client, admin_h, team_id)["Incidents"]
    member = client.get(
        f"{V1}/teams/{team_id}/channels/{incidents}/members", headers=admin_h
    ).json()["value"][0]
    assert base64.b64decode(member["id"]).decode() == f"{team_id}##{member['userId']}"


def test_a_user_resolves_by_object_id_and_by_principal_name(client, admin_h):
    by_upn = client.get(f"{V1}/users/ava@acme.com", headers=admin_h).json()
    by_id = client.get(f"{V1}/users/{by_upn['id']}", headers=admin_h).json()
    assert by_id == by_upn

    missing = client.get(f"{V1}/users/nobody@acme.com", headers=admin_h)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "Request_ResourceNotFound"
    assert "nobody@acme.com" in missing.json()["error"]["message"]


# Placed after every `client`-fixture test in this module: each opens a SECOND app over a different
# DB via `corpus_client`, which overwrites the module-scoped fixture's shared `app.state` (see
# `tests._helpers.client_for`'s docstring).


def test_a_channel_is_ordered_by_its_reply_chains_newest_activity(tmp_path):
    """Graph sorts a channel "by the last modified date of the entire reply chain, including both
    the root channel message and its replies" — so an old post someone answered today outranks a
    newer post nobody has touched. Ordering by the root's own clock alone gets this backwards."""
    records = [
        {
            "source_type": "msteams",
            "doc_id": "old-but-active",
            "channel": "Platform",
            "content": "Old post.",
            "author_email": "ava@x.com",
            "created": "2026-01-01T09:00:00Z",
            "replies": [
                {
                    "content": "Still going.",
                    "author_email": "bo@x.com",
                    "created": "2026-03-01T09:00:00Z",
                }
            ],
        },
        {
            "source_type": "msteams",
            "doc_id": "newer-but-quiet",
            "channel": "Platform",
            "content": "Newer post.",
            "author_email": "bo@x.com",
            "created": "2026-02-01T09:00:00Z",
        },
    ]
    with corpus_client(tmp_path, records) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=h).json()["value"][0]["id"]
        channel = _channels(client, h, team)["Platform"]
        bodies = [
            m["body"]["content"]
            for m in client.get(f"{V1}/teams/{team}/channels/{channel}/messages", headers=h).json()[
                "value"
            ]
        ]
    assert bodies == ["Old post.", "Newer post."]


def test_a_system_event_message_carries_no_sender(tmp_path):
    """Measured against the vendor's own example: `from` is null beside
    `messageType: systemEventMessage`, because Teams generated the notice rather than a person."""
    with corpus_client(
        tmp_path,
        [
            {
                "source_type": "msteams",
                "channel": "Platform",
                "message_type": "systemEventMessage",
                "content": "<systemEventMessage/>",
                "content_type": "html",
                "author_email": "ava@x.com",
                "created": "2026-02-01T09:00:00Z",
            }
        ],
    ) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=h).json()["value"][0]["id"]
        channel = _channels(client, h, team)["Platform"]
        m = client.get(f"{V1}/teams/{team}/channels/{channel}/messages", headers=h).json()["value"][
            0
        ]
    assert m["messageType"] == "systemEventMessage"
    assert m["from"] is None


def test_a_sender_id_from_a_message_resolves_at_the_users_route(tmp_path):
    """An id the API hands out has to resolve. A reply's sender may be a bot or anyone else the
    corpus names, and `from.user.id` is the only handle a client has for them — a `/users/{id}`
    that came back not-found for it would be Backlot advertising an id it will not answer."""
    with corpus_client(
        tmp_path,
        [
            {
                "source_type": "msteams",
                "channel": "Platform",
                "content": "Nightly build passed.",
                "author_email": "ava@x.com",
                "created": "2026-02-01T09:00:00Z",
                "replies": [
                    {
                        "content": "Artifacts uploaded.",
                        "author_email": "buildbot@x.com",
                        "created": "2026-02-01T09:00:30Z",
                    }
                ],
            }
        ],
    ) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=h).json()["value"][0]["id"]
        channel = _channels(client, h, team)["Platform"]
        base = f"{V1}/teams/{team}/channels/{channel}/messages"
        root = client.get(base, headers=h).json()["value"][0]
        reply = client.get(f"{base}/{root['id']}/replies", headers=h).json()["value"][0]
        resolved = client.get(f"{V1}/users/{reply['from']['user']['id']}", headers=h)

    assert resolved.status_code == 200
    assert resolved.json()["userPrincipalName"] == "buildbot@x.com"


def test_a_reaction_without_its_own_time_takes_the_messages(tmp_path):
    with corpus_client(
        tmp_path,
        [
            {
                "source_type": "msteams",
                "channel": "Platform",
                "content": "Ship it.",
                "author_email": "ava@x.com",
                "created": "2026-02-01T09:00:00Z",
                "reactions": [{"reactionType": "heart", "user": "bo@x.com"}],
            }
        ],
    ) as (client, settings):
        h = {"Authorization": f"Bearer {settings.admin_token}"}
        team = client.get(f"{V1}/me/joinedTeams", headers=h).json()["value"][0]["id"]
        channel = _channels(client, h, team)["Platform"]
        m = client.get(f"{V1}/teams/{team}/channels/{channel}/messages", headers=h).json()["value"][
            0
        ]

    reaction = m["reactions"][0]
    assert reaction["createdDateTime"] == m["createdDateTime"]
    assert reaction["user"]["user"]["id"] == synth.msteams_user_id("bo@x.com")
