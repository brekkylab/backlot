"""Fireflies' meeting-transcript GraphQL API at /fireflies/graphql.

One file per router, so a provider's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

from backlot import store
from tests._helpers import client_for, corpus_client, gql, tiny_corpus


# --- fireflies: POST /fireflies/graphql -----------------------------------------
# Fireflies has no SDK and no LlamaIndex reader; the vendor's own quickstart is a raw HTTP POST,
# so this IS the client story rather than a fallback for one.


def ff_gql(client, query, headers, **variables):
    body = {"query": query}
    if variables:
        body["variables"] = variables
    return client.post("/fireflies/graphql", json=body, headers=headers)


def test_fireflies_requires_a_bearer_key(client):
    r = client.post("/fireflies/graphql", json={"query": "{ transcripts { id } }"})
    assert r.status_code == 401
    # a GraphQL error envelope, not a framework 403 — clients parse errors[0].message
    assert r.json()["errors"][0]["message"]
    assert "data" not in r.json()
    bad = client.post(
        "/fireflies/graphql",
        json={"query": "{ transcripts { id } }"},
        headers={"Authorization": "Bearer not-a-real-key"},
    )
    assert bad.status_code == 401


def test_fireflies_admin_crawl_sees_every_stored_transcript(client, admin_h, ro_conn):
    r = ff_gql(client, "{ transcripts(limit: 50) { id title } }", admin_h)
    assert r.status_code == 200
    served = r.json()["data"]["transcripts"]
    assert len(served) == store.count_fireflies_transcripts(ro_conn)
    assert (
        len(served) == ro_conn.execute("SELECT COUNT(*) FROM fireflies_transcripts").fetchone()[0]
    )


def test_fireflies_transcript_content_round_trips_through_the_api(client, admin_h, ro_conn):
    """The sentences the API serves must rebuild the stored `content` byte for byte — that is the
    whole point of defining content as the concatenation."""
    from backlot import synth

    r = ff_gql(
        client, "{ transcripts(limit: 50) { id title sentences { speaker_name text } } }", admin_h
    )
    for t in r.json()["data"]["transcripts"]:
        row = store.fireflies_transcript_by_id(ro_conn, t["id"])
        assert synth.fireflies_transcript_text(t["sentences"]) == row["content"]


def test_fireflies_serves_the_documented_metadata_surface(client, admin_h):
    r = ff_gql(
        client,
        """
        { transcripts(limit: 1) {
            id title date dateString duration host_email organizer_email participants
            meeting_link calendar_id cal_id calendar_type channels
            transcript_url audio_url video_url
            user { user_id email name }
            summary { overview keywords action_items outline topics_discussed meeting_type }
            analytics { sentiments { positive_pct neutral_pct negative_pct }
                        speakers { name duration word_count duration_pct } }
            meeting_attendees { displayName email location }
            sentences { index speaker_name speaker_id text raw_text start_time end_time }
        } }""",
        admin_h,
    )
    assert r.status_code == 200 and "errors" not in r.json()
    t = r.json()["data"]["transcripts"][0]
    assert t["id"] and t["title"]
    assert t["date"] and t["dateString"]
    assert t["channels"] and t["transcript_url"] and t["audio_url"] and t["video_url"]
    assert t["sentences"] and t["analytics"]["sentiments"]["positive_pct"] is not None


def test_fireflies_sentence_windows_are_ordered_and_contiguous(client, admin_h):
    r = ff_gql(
        client, "{ transcripts(limit: 50) { sentences { index start_time end_time } } }", admin_h
    )
    for t in r.json()["data"]["transcripts"]:
        sents = t["sentences"]
        assert [s["index"] for s in sents] == list(range(len(sents)))
        for a, b in zip(sents, sents[1:]):
            assert a["start_time"] < a["end_time"] <= b["start_time"]


def test_fireflies_date_is_epoch_millis_matching_the_iso_string(client, admin_h):
    """Fireflies returns `date` as epoch MILLISECONDS — a client that divides by 1000 must land on
    the same instant `dateString` states."""
    import datetime as dt

    r = ff_gql(client, "{ transcripts(limit: 50) { date dateString } }", admin_h)
    for t in r.json()["data"]["transcripts"]:
        parsed = dt.datetime.fromisoformat(t["dateString"].replace("Z", "+00:00"))
        assert t["date"] == parsed.timestamp() * 1000


def test_fireflies_organizer_falls_back_to_the_host(client, admin_h, ro_conn):
    """The column is NULL when a meeting's organizer is its host; the FIELD must still answer,
    because Fireflies itself never returns a null organizer for a hosted meeting."""
    assert (
        ro_conn.execute(
            "SELECT organizer_email FROM fireflies_transcripts WHERE doc_id='ff-discovery'"
        ).fetchone()[0]
        is None
    )
    r = ff_gql(client, "{ transcripts(limit: 50) { host_email organizer_email } }", admin_h)
    served = r.json()["data"]["transcripts"]
    assert all(t["organizer_email"] for t in served)
    assert any(t["organizer_email"] == t["host_email"] for t in served)


def test_fireflies_transcript_by_id_matches_the_listing(client, admin_h):
    listed = ff_gql(client, "{ transcripts(limit: 1) { id title } }", admin_h).json()["data"][
        "transcripts"
    ][0]
    one = ff_gql(
        client, "query($i:String!){ transcript(id:$i) { id title } }", admin_h, i=listed["id"]
    ).json()["data"]["transcript"]
    assert one == listed
    absent = ff_gql(client, '{ transcript(id: "deadbeefdeadbeefdeadbeef") { id } }', admin_h).json()
    assert absent["data"]["transcript"] is None


def test_fireflies_limit_is_clamped_over_http(client, admin_h, ro_conn):
    total = store.count_fireflies_transcripts(ro_conn)
    got = ff_gql(client, "query($l:Int){ transcripts(limit:$l) { id } }", admin_h, l=10_000).json()[
        "data"
    ]["transcripts"]
    assert len(got) == min(50, total)  # clamped to the documented max, not an error


def test_fireflies_skip_pages_without_gaps_or_repeats(client, admin_h, ro_conn):
    total = store.count_fireflies_transcripts(ro_conn)
    walked = []
    for skip in range(total + 1):
        page = ff_gql(
            client, "query($s:Int){ transcripts(limit:1, skip:$s) { id } }", admin_h, s=skip
        ).json()["data"]["transcripts"]
        walked += [t["id"] for t in page]
    assert len(walked) == total == len(set(walked))
    # past the end is an empty list, not an error
    beyond = ff_gql(client, "{ transcripts(limit: 5, skip: 9999) { id } }", admin_h).json()
    assert beyond["data"]["transcripts"] == []


def test_fireflies_keyword_scope_over_http(client, admin_h):
    def titles(**args):
        arglist = ", ".join(f'{k}: "{v}"' for k, v in args.items())
        return {
            t["title"]
            for t in ff_gql(
                client, "{ transcripts(%s, limit: 50) { title } }" % arglist, admin_h
            ).json()["data"]["transcripts"]
        }

    assert titles(keyword="selects", scope="title") == set()
    assert titles(keyword="selects", scope="sentences") == {"April all-hands"}
    assert titles(keyword="selects", scope="all") == {"April all-hands"}
    assert titles(keyword="all-hands", scope="title") == {"April all-hands"}


def test_fireflies_unknown_scope_is_a_field_error_not_a_silent_widening(client, admin_h):
    """Silently searching everything would hide a client's typo. A field error keeps the
    GraphQL-over-HTTP contract: 200 with partial data alongside errors."""
    r = ff_gql(client, '{ transcripts(keyword: "x", scope: "body") { id } }', admin_h)
    assert r.status_code == 200
    body = r.json()
    assert "data" in body  # field error -> data key present
    assert body["data"]["transcripts"] is None
    assert "scope must be one of" in body["errors"][0]["message"]


def test_fireflies_request_errors_are_400_with_no_data_key(client, admin_h):
    """The engine's request/field split: a malformed or invalid document is decided before
    execution, so the response carries no `data` entry at all."""
    for query in ("{ transcripts(", "{ transcripts { nosuchfield } }", "{ }"):
        r = ff_gql(client, query, admin_h)
        assert r.status_code == 400, query
        assert "data" not in r.json(), query
        assert r.json()["errors"], query


def test_fireflies_user_root_answers_for_a_person_only(client, admin_h, tokens_yaml):
    """`user` with no id is the authenticated user. An admin/service token is not a person."""
    assert ff_gql(client, "{ user { user_id email } }", admin_h).json()["data"]["user"] is None
    ava = next(u["token"] for u in tokens_yaml["users"] if u["email"] == "ava@acme.com")
    h = {"Authorization": f"Bearer {ava}"}
    me = ff_gql(client, "{ user { user_id email name } }", h).json()["data"]["user"]
    assert me["email"] == "ava@acme.com" and me["user_id"]
    # the served user_id round-trips back through user(id:)
    again = ff_gql(client, "query($i:String){ user(id:$i) { email } }", h, i=me["user_id"]).json()[
        "data"
    ]["user"]
    assert again["email"] == "ava@acme.com"


def test_fireflies_introspection_describes_the_schema(client, admin_h):
    """There is no OpenAPI entry for this route on purpose, so introspection is how a client
    discovers the surface."""
    r = ff_gql(client, "{ __schema { queryType { fields { name } } } }", admin_h)
    names = {f["name"] for f in r.json()["data"]["__schema"]["queryType"]["fields"]}
    assert {"transcripts", "transcript", "user", "users"} <= names


def test_fireflies_declares_no_mutations(client, admin_h):
    """A read-only mock declares no Mutation type rather than accepting writes and dropping them."""
    r = ff_gql(client, "{ __schema { mutationType { name } } }", admin_h)
    assert r.json()["data"]["__schema"]["mutationType"] is None


def _fireflies_client(tmp_path):
    """Same, over FIREFLIES_CORPUS."""
    return corpus_client(tmp_path, FIREFLIES_CORPUS)


# --- fireflies -------------------------------------------------------------------
# Fireflies' shape differs from every other GraphQL source here in two ways that clients depend
# on: snake_case field names, and offset pagination returning a BARE LIST rather than a Relay
# connection. Both are pinned, because "it's GraphQL" is exactly the assumption that would
# otherwise make someone wrap this in `{ nodes, pageInfo }`.

FIREFLIES_CORPUS = [
    {
        "source_type": "fireflies",
        "doc_id": "ff-f1",
        "channel": "sales-calls",
        "title": "Fidelity discovery call",
        "host_email": "ava@acme.com",
        "host_name": "Ava Chen",
        "organizer_email": "ops@acme.com",
        "duration": 45.0,
        "calendar_id": "cal-fid",
        "created": "2026-04-02T15:00:00Z",
        "visibility": "public",
        "summary": {
            "overview": "Overview text.",
            "topics_discussed": ["latency"],
            "action_items": ["Ava: follow up", "Bob: benchmark"],
            "keywords": ["latency"],
            "meeting_type": "discovery",
        },
        "meeting_attendees": [
            {"displayName": "Ava Chen", "email": "ava@acme.com", "location": None}
        ],
        "sentences": [
            {
                "speaker_name": "Ava Chen",
                "author_email": "ava@acme.com",
                "start_time": 0,
                "text": "Kicking off.",
            },
            {"speaker_name": "Dana Ruiz", "start_time": 20, "text": "Sounds good."},
        ],
    },
]


def _ff(client, settings, query, **variables):
    return gql(
        client, "/fireflies/graphql", query, f"Bearer {settings.admin_token}", **variables
    ).json()


def test_fireflies_accepts_the_vendors_documented_raw_http_post(tmp_path):
    """There is no Fireflies SDK: the vendor's quickstart is curl / requests.post / axios.post /
    Java HttpClient against one endpoint with a Bearer key. That IS the client story, so the exact
    shape those examples send has to work."""
    with _fireflies_client(tmp_path) as (client, settings):
        r = client.post(
            "/fireflies/graphql",
            json={
                "query": "query Transcripts($limit: Int) "
                "{ transcripts(limit: $limit) { id title } }",
                "variables": {"limit": 10},
            },
            headers={
                "Authorization": f"Bearer {settings.admin_token}",
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json()["data"]["transcripts"][0]["title"] == "Fidelity discovery call"


def test_fireflies_transcripts_is_a_bare_list_not_a_relay_connection(tmp_path):
    """Fireflies pages with limit/skip. Wrapping it in `{ nodes, pageInfo }` — the shape every
    other GraphQL source here uses — would break every generated client."""
    with _fireflies_client(tmp_path) as (client, settings):
        got = _ff(client, settings, "{ transcripts(limit: 5) { id } }")
        assert isinstance(got["data"]["transcripts"], list)
        # asking for a connection's fields must be a validation error, i.e. they do not exist
        bad = _ff(client, settings, "{ transcripts { nodes { id } pageInfo { hasNextPage } } }")
        assert "data" not in bad and bad["errors"]


def test_fireflies_field_names_are_snake_case(tmp_path):
    """Fireflies' own convention, not a translation — a camelCase spelling must NOT resolve."""
    with _fireflies_client(tmp_path) as (client, settings):
        ok = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { host_email organizer_email "
            "audio_url video_url transcript_url meeting_link "
            "calendar_type meeting_attendees { displayName } "
            "sentences { speaker_name speaker_id start_time end_time } } }",
        )
        assert "errors" not in ok
        t = ok["data"]["transcripts"][0]
        assert t["host_email"] == "ava@acme.com"
        assert t["organizer_email"] == "ops@acme.com"  # distinct organizer is kept, not coerced
        for camel in ("hostEmail", "audioUrl", "transcriptUrl", "meetingLink"):
            bad = _ff(client, settings, "{ transcripts(limit: 1) { %s } }" % camel)
            assert "data" not in bad and bad["errors"], camel


def test_fireflies_duration_is_minutes(tmp_path):
    """The API's unit. Serving seconds would make every meeting read as 60x too long."""
    with _fireflies_client(tmp_path) as (client, settings):
        t = _ff(client, settings, "{ transcripts(limit: 1) { duration } }")["data"]["transcripts"][
            0
        ]
        assert t["duration"] == 45.0


def test_fireflies_action_items_are_a_newline_joined_string(tmp_path):
    """Fireflies returns `summary.action_items` as ONE string, not a list — a client doing
    `.split("\\n")` on it must not get a JSON array."""
    with _fireflies_client(tmp_path) as (client, settings):
        s = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { summary { action_items "
            "topics_discussed keywords overview meeting_type } } }",
        )["data"]["transcripts"][0]["summary"]
        assert s["action_items"] == "Ava: follow up\nBob: benchmark"
        # topics/keywords ARE lists in the real API, so they must stay lists
        assert s["topics_discussed"] == ["latency"]
        assert s["keywords"] == ["latency"]
        assert s["meeting_type"] == "discovery"


def test_fireflies_sentence_times_are_seconds_while_duration_is_minutes(tmp_path):
    """The two units really do differ in the real API; a mock that made them agree would look
    tidier and be wrong."""
    with _fireflies_client(tmp_path) as (client, settings):
        t = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { duration sentences { start_time end_time } } }",
        )["data"]["transcripts"][0]
        assert t["sentences"][1]["start_time"] == 20.0  # seconds
        assert t["duration"] == 45.0  # minutes
        assert t["sentences"][-1]["end_time"] <= t["duration"] * 60


def test_fireflies_speaker_id_is_an_integer_scoped_to_the_meeting(tmp_path):
    with _fireflies_client(tmp_path) as (client, settings):
        sents = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { sentences { index speaker_id speaker_name } } }",
        )["data"]["transcripts"][0]["sentences"]
        assert [s["speaker_id"] for s in sents] == [0, 1]
        assert all(isinstance(s["speaker_id"], int) for s in sents)
        assert [s["index"] for s in sents] == [0, 1]


def test_fireflies_stubbed_fields_are_null_not_invented(tmp_path):
    """The SDL declares more than a document corpus can back. Everything unbacked must be null —
    an invented sentiment or classifier flag is worse than an honest gap."""
    with _fireflies_client(tmp_path) as (client, settings):
        t = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { apps_preview "
            "meeting_attendance { email joinedAt duration } "
            "summary { bullet_gist gist transcript_chapters } "
            "analytics { categories { questions tasks } } "
            "sentences { ai_filters { task question } } "
            "user { minutes_consumed is_admin integrations } } }",
        )["data"]["transcripts"][0]
        assert t["meeting_attendance"] is None and t["apps_preview"] is None
        assert t["summary"]["bullet_gist"] is None and t["summary"]["gist"] is None
        assert t["summary"]["transcript_chapters"] is None
        assert t["analytics"]["categories"]["questions"] is None
        assert t["sentences"][0]["ai_filters"] is None
        assert t["user"]["minutes_consumed"] is None and t["user"]["is_admin"] is None


def test_fireflies_analytics_sentiments_sum_to_one_hundred(tmp_path):
    """Synthesized, never derived from the text — but it still has to be internally coherent, or a
    consumer charting it gets nonsense."""
    with _fireflies_client(tmp_path) as (client, settings):
        s = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { analytics { sentiments "
            "{ positive_pct neutral_pct negative_pct } } } }",
        )["data"]["transcripts"][0]["analytics"]["sentiments"]
        assert round(s["positive_pct"] + s["neutral_pct"] + s["negative_pct"]) == 100
        assert all(v >= 0 for v in s.values())


def test_fireflies_speaker_analytics_are_computed_from_the_sentences(tmp_path):
    """Talk time and word counts ARE derivable from the transcript, so unlike sentiment they are
    real rather than synthesized."""
    with _fireflies_client(tmp_path) as (client, settings):
        t = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { analytics { speakers "
            "{ name duration word_count duration_pct } } "
            "sentences { speaker_name text start_time end_time } } }",
        )["data"]["transcripts"][0]
        by_name = {s["name"]: s for s in t["analytics"]["speakers"]}
        assert set(by_name) == {"Ava Chen", "Dana Ruiz"}
        for sent in t["sentences"]:
            spoken = len(sent["text"].split())
            assert by_name[sent["speaker_name"]]["word_count"] >= spoken
        assert by_name["Ava Chen"]["duration"] == 20.0  # 0 -> 20, its own window


def test_fireflies_speaker_shares_sum_to_one_hundred(tmp_path):
    """`duration_pct` shares out the TALK TIME, not the declared meeting length: a corpus
    transcript often does not span its whole meeting, and dividing by the declared length emits
    shares summing to ~4%, which reads as a bug in anything that charts them."""
    with _fireflies_client(tmp_path) as (client, settings):
        speakers = _ff(
            client,
            settings,
            "{ transcripts(limit: 1) { duration analytics "
            "{ speakers { duration duration_pct } } } }",
        )["data"]["transcripts"][0]["analytics"]["speakers"]
        assert round(sum(s["duration_pct"] for s in speakers)) == 100
        # and the talk time really is far short of the declared 45-minute meeting
        assert sum(s["duration"] for s in speakers) < 45 * 60


def test_fireflies_no_openapi_entry_for_the_graphql_route(tmp_path):
    """Describing one POST that accepts an arbitrary query tells an OpenAPI->MCP bridge nothing,
    so the route is deliberately absent from the document (and from SOURCE_PREFIXES)."""
    from backlot import openapi

    with _fireflies_client(tmp_path) as (client, settings):
        spec = client.get("/openapi.json").json()
        assert not [p for p in spec["paths"] if p.startswith("/fireflies")]
        assert "fireflies" not in openapi.SOURCE_PREFIXES


def test_fireflies_users_is_the_workspace_roster_not_every_named_person(tmp_path):
    """`users` must be the people with an ACCOUNT. The mock's principals table registers every
    internal reference across every source — 16,034 on the deployed bench corpus, of whom 327 have
    a token — so serving all of them would be wrong (they have no Fireflies account) AND a 1.6 MB
    unpaginated response. The real query takes no pagination args, so scoping is what bounds it.

    `user(id:)` must still resolve a display-only principal, or a transcript whose host never had
    an account would serve `user: null`.
    """

    from backlot import store, synth

    settings = tiny_corpus(tmp_path, FIREFLIES_CORPUS)
    # A principal the corpus names but who has no token — what an ERB append creates. Inserted
    # BEFORE startup because the user_id -> email index is built in the lifespan, exactly as
    # Linear's by-id indexes are.
    conn = store.connect_rw(settings.db_path)
    conn.execute(
        "INSERT OR REPLACE INTO principals(id, type, display_name, email) VALUES (?,?,?,?)",
        ("ghost@acme.com", "user", "Ghost Person", "ghost@acme.com"),
    )
    conn.commit()
    conn.close()

    with client_for(settings) as client:
        emails = {u["email"] for u in _ff(client, settings, "{ users { email } }")["data"]["users"]}
        assert "ghost@acme.com" not in emails, "a tokenless principal is not a workspace member"
        assert "ava@acme.com" in emails  # the corpus's real, tokened host
        assert emails <= set(_roster_emails(settings))

        # ...but the display-only person is still addressable by id
        got = _ff(
            client,
            settings,
            "query($i:String){ user(id:$i) { email name } }",
            i=synth.fireflies_user_id("ghost@acme.com"),
        )["data"]["user"]
        assert got["email"] == "ghost@acme.com" and got["name"] == "Ghost Person"


def _roster_emails(settings):
    import yaml

    data = yaml.safe_load(settings.tokens_path.read_text()) or {}
    return [u["email"] for u in data.get("users", [])]
