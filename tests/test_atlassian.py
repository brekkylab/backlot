"""Atlassian: Jira issues/JQL and Confluence content/CQL — one router, one file.

One file per router, so a source's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import base64
import json
import re
from urllib.parse import quote

import pytest
import yaml
from starlette.requests import Request

from backlot import store
from backlot.errors import atlassian as errors_atlassian
from tests._helpers import (
    bare_request,
    client_for,
    crawl_confluence,
    crawl_jira,
    db_count,
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
def test_jira_processes_a_credential_it_cannot_resolve_as_anonymous(client, headers):
    """Real Jira does not refuse an unresolvable credential on these routes — it drops the caller
    to anonymous and answers the request. `project/search` is 200 with the projects an anonymous
    caller may see, a bounded `search/jql` is 200 with that query's anonymous view, and an issue
    is Jira's own 404. No document in a Backlot corpus is granted to a principal outside the org,
    so anonymous reaches none of them and each listing comes back empty."""
    projects = client.get("/atlassian/rest/api/3/project/search", headers=headers)
    assert projects.status_code == 200 and projects.json()["values"] == []
    found = client.get(
        "/atlassian/rest/api/3/search/jql", headers=headers, params={"jql": "project = payments"}
    )
    assert found.status_code == 200 and found.json()["issues"] == []
    issue = client.get("/atlassian/rest/api/3/issue/ABC-1", headers=headers)
    assert issue.status_code == 404
    assert issue.json()["errorMessages"] == [
        "Issue does not exist or you do not have permission to see it."
    ]


@pytest.mark.parametrize("path", ["/rest/api/3/field", "/rest/api/3/issueLinkType"])
def test_jira_serves_its_field_metadata_to_an_anonymous_caller(client, path):
    """Both answer 200 to a failed pair on the real sites, so neither is behind the credential."""
    assert client.get(f"/atlassian{path}", headers=FAILED_PAIR).status_code == 200


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

        written = yaml.safe_load(settings.tokens_path.read_text())
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
        # in it, reads both. Under both spellings `_space_container_for_key` resolves: the
        # synthesized key and the name.
        shut = synth.confluence_space_key("secrets")
        ava = {"Authorization": f"Bearer {tokens['ava@acme.com']}"}
        bob = {"Authorization": f"Bearer {tokens['bob@acme.com']}"}
        for spelling in (shut, "secrets"):
            for path in (
                f"/wiki/rest/api/space/{spelling}",
                f"/wiki/rest/api/space/{spelling}?expand=permissions",
            ):
                refused = c.get(f"/atlassian{path}", headers=ava)
                assert refused.status_code == 404, path
                assert refused.json()["message"] == "No space with the given key exists"
                assert c.get(f"/atlassian{path}", headers=bob).status_code == 200, path
        # ... and the roster she is refused names bob against that space.
        space = c.get(
            f"/atlassian/wiki/rest/api/space/{shut}?expand=permissions", headers=bob
        ).json()
        readers = space["permissions"][0]["subjects"]["user"]["results"]
        assert [u["email"] for u in readers] == ["bob@acme.com"]


@pytest.mark.parametrize("headers", UNRESOLVABLE)
def test_jira_404s_a_project_role_an_anonymous_caller_cannot_see(client, headers):
    """A role read is the one Jira route that keeps refusing an anonymous caller, and which of the
    two refusals it gives is decided by the project: on the site where the key names a project
    anonymous can see it is 401 ("You cannot edit the configuration of this project."), and on the
    site where it names nothing it is 404. Anonymous sees no Backlot project, so it is always the
    404 here — the corpus's own project key gets the answer a key naming nothing would."""
    from backlot import synth

    key = synth.jira_project_key("payments")
    for path in (f"/rest/api/3/project/{key}/role", f"/rest/api/3/project/{key}/role/10002"):
        r = client.get(f"/atlassian{path}", headers=headers)
        assert r.status_code == 404, path
        assert r.json()["errorMessages"] == [f"No project could be found with key '{key}'."]


def test_jira_refuses_a_bearer_it_cannot_read_as_a_connect_token(client):
    """A bearer is not the Basic pair: Jira does not go anonymous for one it cannot resolve, it
    refuses with 403 and a body that is neither API's envelope. A Backlot token is an opaque
    string with no dots, which is the shape that draws this — measured with `usr-…` itself, and
    with `bogustoken123` and `a.b.c`, on both sites on 2026-09-04."""
    r = client.get(
        "/atlassian/rest/api/3/project/search", headers={"Authorization": "Bearer usr-nope"}
    )
    assert r.status_code == 403
    # The whole body, not a subset: this one route answers with a single `error` key, where every
    # other Atlassian error here carries message/statusCode/errorMessages.
    assert r.json() == {"error": "Failed to parse Connect Session Auth Token"}
    # No Seraph header either — that one reports a failed Basic username, and this is not one.
    assert "x-seraph-loginreason" not in r.headers
    # And it is refused ahead of the route: serverInfo needs no credential and still answers 403,
    # which is why the check is not in the caller helper.
    info = client.get(
        "/atlassian/rest/api/3/serverInfo", headers={"Authorization": "Bearer usr-nope"}
    )
    assert info.status_code == 403 and info.json() == {
        "error": errors_atlassian.CONNECT_TOKEN_UNREADABLE
    }


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
    ],
)
def test_jira_reads_an_unrecognised_scheme_as_no_credential(client, header):
    """The 403 above is for a credential the site READ. A spelling it does not read is not a
    credential at all, and the request is the anonymous one — which is how each spelling was told
    apart on the real sites in the first place (403 = read, 200 = not read)."""
    r = client.get("/atlassian/rest/api/3/project/search", headers={"Authorization": header})
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
    outright, with a 403 in its own envelope, whether the credential failed or was never sent."""
    for path in ("/wiki/rest/api/space", "/wiki/rest/api/content/1"):
        r = client.get(f"/atlassian{path}", headers=headers)
        assert r.status_code == 403, path
        assert r.json()["message"] == errors_atlassian.CONFLUENCE_FORBIDDEN
        assert r.json()["statusCode"] == 403


@pytest.mark.parametrize(
    "raw",
    ["nobody@example.com:", ":wrongtoken", ":", "nobody@example.com", "nobody@example.com:a:b"],
)
def test_confluence_answers_401_to_a_basic_credential_it_cannot_parse(client, raw):
    """Confluence's 403 is for a credential it read and rejected. A Basic value that is not one
    non-empty user and one non-empty password separated by a single colon is not one it can read,
    and that is a 401 carrying the site's own OAuth realm — measured on both sites for each shape
    below, and for a value that is not base64 at all. Backlot keeps the Atlassian JSON envelope
    that every other error here carries, where the real 401 is Tomcat's HTML page."""
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
    ``{"detail": ...}`` — see backlot.errors.atlassian."""
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
    assert body["errorMessages"] == [errors_atlassian.CONFLUENCE_FORBIDDEN]
    assert body["statusCode"] == 403


def test_jira_serverinfo_v2_alias_matches_v3(client, admin_h):
    # the `jira` PyPI client (used by llama-index's JiraReader) probes serverInfo under
    # /rest/api/2 on connect; Backlot must serve the same shape as the v3 handler.
    v2 = client.get("/atlassian/rest/api/2/serverInfo", headers=admin_h).json()
    v3 = client.get("/atlassian/rest/api/3/serverInfo", headers=admin_h).json()
    assert v2 == v3
    assert v2["deploymentType"] == "Cloud"


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


def test_confluence_content_filtered_by_space_key(client, admin_h):
    from backlot import synth

    # literal container name (the natural spaceKey value) narrows to that space only
    by_name = client.get(
        "/atlassian/wiki/rest/api/content", headers=admin_h, params={"spaceKey": "handbook"}
    ).json()
    titles = {r["title"] for r in by_name["results"]}
    assert titles == {"Engineering Handbook", "On-call Runbook"}
    assert "Compensation Bands 2026" not in titles

    # the synthesized (hash-suffixed) key resolves to the same space
    synth_key = synth.confluence_space_key("handbook")
    by_synth_key = client.get(
        "/atlassian/wiki/rest/api/content", headers=admin_h, params={"spaceKey": synth_key}
    ).json()
    assert {r["title"] for r in by_synth_key["results"]} == titles

    # an unresolvable spaceKey is strict: zero results, not the unfiltered corpus
    bogus = client.get(
        "/atlassian/wiki/rest/api/content", headers=admin_h, params={"spaceKey": "BOGUS_NOPE"}
    ).json()
    assert bogus["results"] == [] and bogus["size"] == 0

    # no spaceKey at all -> unfiltered (still includes the other space)
    unfiltered = client.get("/atlassian/wiki/rest/api/content", headers=admin_h).json()
    assert "Compensation Bands 2026" in {r["title"] for r in unfiltered["results"]}


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
        params={"cql": 'text~"software" and space=people-ops'},
    ).json()
    assert other_space["results"] == [] and other_space["totalSize"] == 0

    bogus = client.get(
        "/atlassian/wiki/rest/api/search",
        headers=admin_h,
        params={"cql": 'text~"software" and space=BOGUS_NOPE'},
    ).json()
    assert bogus["results"] == [] and bogus["totalSize"] == 0


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


def test_atlassian_errors_use_atlassian_envelope(client):
    # atlassian-python-api's Confluence client does response.json()["message"] on any error, so
    # Backlot must shape /atlassian errors like Cloud does (message + statusCode), not {"detail"}.
    r = client.get("/atlassian/wiki/rest/api/content/999999")  # unauthenticated -> 403
    assert r.status_code == 403
    assert r.json().get("message") and r.json().get("statusCode") == 403
    r2 = client.get(
        "/atlassian/wiki/rest/api/content/search"
    )  # 'search' fails int path validation -> 422
    assert r2.status_code == 422 and "message" in r2.json()
    # non-atlassian paths keep FastAPI's default {"detail"} envelope
    r3 = client.get("/no-such-route")
    assert r3.status_code == 404 and "detail" in r3.json() and "message" not in r3.json()


def test_confluence_spaces_are_paged_not_served_whole(client, admin_h, tokens):
    """Measured against a live Confluence Cloud site on 2026-09-17: `space` reads `limit`/`start`
    and answers a page, with `_links.next`/`.prev` shaped like :func:`confluence_space_links`."""
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
    # none of `limit`/`start`/`expand` — the only parameters this route reads.

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

    # expand rides into next/prev/self too — see confluence_space_links for the ordering.
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
    # An unknown space is the same atlassian-shaped 404 with the expansion and without it, so a
    # space the caller cannot reach cannot be told apart from one that is not there.
    for path in ("/wiki/rest/api/space/NOSUCH", "/wiki/rest/api/space/NOSUCH?expand=permissions"):
        absent = client.get(f"/atlassian{path}", headers=admin_h)
        assert absent.status_code == 404, path
        assert absent.json()["message"] == "No space with the given key exists", path


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


def test_confluence_restrictions_has_update(tmp_path):
    # restrictions/byOperation must return BOTH read and update operations
    import asyncio
    import types

    from backlot.acl import Acl
    from backlot.routers.atlassian import confluence_restrictions

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
        "query_string": b"",
        "app": app,
        "headers": [(b"authorization", f"Bearer {s.admin_token}".encode())],
    }
    result = asyncio.run(confluence_restrictions(cid, Request(scope)))
    assert "read" in result and "update" in result
    assert result["read"]["restrictions"]["user"]["results"]  # the private doc's author


def test_confluence_child_page_and_restriction_match_a_nonexistent_id_for_an_outsider(
    client, tokens, ro_conn
):
    """`_confluence_doc_id` (`:981`) is deliberately unscoped -- of its four callers, `child/
    comment` and `label` already re-check with `store.get_document(..., visible_ids=...)` and 404
    on a miss; `child/page` and `restriction/byOperation` used not to. That let an outsider use
    `child/page`'s 200 as an existence oracle for a page they cannot read, and
    `restriction/byOperation` handed back the READER ROSTER -- emails, account ids, display names
    -- for that same page: data, not just existence. Handled the same way `child/comment`/`label`
    are: a restricted page must be byte-identical, status AND
    body, to a made-up id -- checked here on the actual response bytes, not merely "not 200"."""
    cid = served_id("confluence", "cf-comp")  # people-only
    h = {"Authorization": f"Bearer {tokens['ava@acme.com']}"}  # engineering; cannot see cf-comp
    for path in ("child/page", "restriction/byOperation"):
        hidden = client.get(f"/atlassian/wiki/rest/api/content/{cid}/{path}", headers=h)
        made_up = client.get(f"/atlassian/wiki/rest/api/content/999999999/{path}", headers=h)
        assert hidden.status_code == made_up.status_code == 404
        assert hidden.content == made_up.content


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
        tok = yaml.safe_load(settings.tokens_path.read_text())["admin_token"]
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


@pytest.mark.parametrize("query", ["orderBy=--created", "orderBy=%2B-created", "orderBy="])
def test_jira_refuses_the_spellings_the_real_api_refuses(paged, query):
    """Measured: exactly ONE sigil is stripped, so `--created` leaves `-created`, which is not a
    field — real Jira's own message echoes `-created`, not `--created`. An empty value is refused
    for the same reason: the field is the empty string."""
    client, h = paged
    r = client.get(f"/atlassian/rest/api/3/issue/PAY-7/comment?{query}", headers=h)
    assert r.status_code == 400, r.text


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
        tok = yaml.safe_load(settings.tokens_path.read_text())["admin_token"]
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
    assert d["errors"] == {}
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


# --- how a query parameter is READ: conversion, range, repetition -------------------------

# Measured on brekkylab.atlassian.net, 2026-09-14. The two products refuse a type-conversion
# failure in DIFFERENT envelopes, which is why these cases cannot go through the one
# `errors.atlassian._body` serves: Jira answers RFC 7807 on `application/problem+json`, Confluence
# its own two-key body carrying a raw Java exception string on a bare `application/json`.


@pytest.mark.parametrize(
    "param,value",
    [
        ("maxResults", "abc"),
        ("startAt", "abc"),
        ("maxResults", "1.5"),
        # where Python's own int() reads 10
        ("maxResults", "1_0"),
        # the width is per PARAMETER: `maxResults` is a Java int, `startAt` a Java long, and each
        # is refused just past its own boundary and answered 200 just inside it (below)
        ("maxResults", "2147483648"),
        ("maxResults", "-2147483649"),
        ("maxResults", "9223372036854775807"),
        ("startAt", "9223372036854775808"),
        ("startAt", "-9223372036854775809"),
    ],
)
def test_jira_refuses_an_integer_parameter_it_cannot_convert(paged, param, value):
    client, h = paged
    r = client.get(f"/atlassian/rest/api/3/issue/PAY-7/comment?{param}={value}", headers=h)
    assert r.status_code == 400, r.text
    assert r.headers["content-type"] == "application/problem+json;charset=UTF-8"
    assert r.json() == {
        "type": "about:blank",
        "title": "Bad Request",
        "status": 400,
        "detail": f"Failed to convert '{param}' with value: '{value}'",
        "instance": "/rest/api/3/issue/PAY-7/comment",
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


def test_jira_search_refuses_an_integer_parameter_it_cannot_convert(client, admin_h):
    r = client.get("/atlassian/rest/api/3/search/jql?maxResults=abc", headers=admin_h)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "Failed to convert 'maxResults' with value: 'abc'"
    assert r.json()["instance"] == "/rest/api/3/search/jql"


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
    read. Confluence answers the bare type on every JSON body measured — its 200s, 404s, 400, 403
    and 405. The RFC 7807 refusals keep `application/problem+json`, which the middleware never
    touches. The spelling is pinned here rather than read from the constant, so a reformat into
    GitHub's `application/json; charset=utf-8` is caught in the file a reader of the Jira rule
    opens."""
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
        ("limit=2147483647", 2147483647),
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


def test_confluence_cql_search_keeps_its_own_lenient_read(client, admin_h):
    """The CQL route is not Spring-bound: a value it cannot convert is a bodiless 404 on real, not
    `content`'s 400 (#216). Serving `content`'s refusal here would trade one divergence for
    another, so it keeps the lenient read — but the NEGATIVE refusal is measured on this route too
    and is shared."""
    ok = client.get("/atlassian/wiki/rest/api/search?cql=type%3Dpage&limit=abc", headers=admin_h)
    assert ok.status_code == 200, ok.text
    neg = client.get("/atlassian/wiki/rest/api/search?cql=type%3Dpage&start=-1", headers=admin_h)
    assert neg.status_code == 400, neg.text
    assert neg.json()["message"] == (
        "java.lang.IllegalArgumentException: start cannot be less than zero"
    )


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
    "query,want",
    [
        # conversion comes first for BOTH parameters, so a bad `start` outranks a negative `limit`
        ("limit=-1&start=abc", 'For input string: "abc"'),
        ("limit=abc&start=-1", 'For input string: "abc"'),
        # and among two negatives it is `start` that gets named
        ("limit=-1&start=-1", "start cannot be less than zero"),
    ],
)
def test_confluence_refuses_the_parameter_real_names_when_both_are_wrong(
    client, admin_h, query, want
):
    r = client.get(f"/atlassian/wiki/rest/api/content?{query}", headers=admin_h)
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


@pytest.mark.parametrize("value", [0, -1, 5001, "0", "-1", "5001", None])
def test_jira_search_refuses_a_max_results_outside_the_range_on_post(client, admin_h, value):
    """Measured 2026-09-18: the same range applies to the POST body, and a JSON `null` is not the
    parameter unsent the way it is for `jql` — Jackson reads a null int field as `0`, which fails
    this same check."""
    r = _search_post(client, admin_h, jql="project = payments", maxResults=value)
    assert r.status_code == 400, r.text
    assert r.json() == {"errorMessages": [_MAX_RESULTS_RANGE_MESSAGE], "errors": {}}


@pytest.mark.parametrize("value", [1, 5000, "1", "5000"])
def test_jira_search_serves_a_max_results_at_the_range_bounds_on_post(client, admin_h, value):
    r = _search_post(client, admin_h, jql="project = payments", maxResults=value)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("value", ["abc", True, False])
def test_jira_search_post_refuses_a_max_results_the_vendor_will_not_coerce(client, admin_h, value):
    """Measured 2026-09-18: Jackson refuses a non-numeral string or a boolean with the body-wide
    sentence, naming no parameter."""
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
        ("fieldsByKeys", True),
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
    """Measured: real answers `PUT` with `Allow: POST, GET`. Starlette fills the header from the
    single route that partially matched, so serving the two methods from a route each would
    advertise one of them — which is why the per-method split is made in the served document
    instead (see `openapi.jira_search_placement`)."""
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
    client, admin_h, content_type, named
):
    """Measured: the header decides before the bytes are looked at, so the JSON body sent with each
    of these is never reached."""
    headers = dict(admin_h)
    if content_type is not None:
        headers["Content-Type"] = content_type
    r = client.post(
        "/atlassian/rest/api/3/search/jql",
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
        "instance": "/rest/api/3/search/jql",
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


def test_jira_search_post_ignores_bytes_after_a_complete_body(client, admin_h):
    """Measured: `{"jql": …} junk` is answered 200 — the vendor's parser reads the first value and
    lets the rest go, where a whole-input JSON read would refuse it."""
    r = client.post(
        "/atlassian/rest/api/3/search/jql",
        headers={**admin_h, "Content-Type": "application/json"},
        content=json.dumps({"jql": "project = payments"}) + " trailing",
    )
    assert r.status_code == 200, r.text
    assert r.json()["issues"]


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
    ],
)
def test_jira_refuses_a_wrong_method_as_rfc_7807_naming_the_methods_it_takes(
    client, admin_h, method, path, allow
):
    r = getattr(client, method)(f"/atlassian{path}", headers=admin_h)
    assert r.status_code == 405, r.text
    assert r.headers["content-type"] == errors_atlassian.PROBLEM_JSON
    assert r.headers["allow"] == allow
    assert r.json() == {
        "type": "about:blank",
        "title": "Method Not Allowed",
        "status": 405,
        "detail": f"Method '{method.upper()}' is not supported.",
        "instance": path,
    }


def test_every_jira_route_carries_a_measured_allow(client):
    """The `Allow` table is keyed by route, so a route added later without a row would answer a
    405 naming whatever methods Backlot happens to declare — which is Backlot's truth, not the
    vendor's. This is the reminder to measure one."""
    paths = [
        p for p in client.get("/openapi.json").json()["paths"] if p.startswith("/atlassian/rest")
    ]
    assert paths
    assert [p for p in paths if errors_atlassian.jira_allow(p) is None] == []


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


# --- what answers before, and around, a route (#311, #315, #325) -----------------------------


@pytest.fixture(scope="module")
def keys(client, admin_h) -> dict[str, str]:
    """One served key of each kind, so the sweeps below ask real routes about real resources."""
    space = client.get("/atlassian/wiki/rest/api/space", headers=admin_h).json()["results"][0]
    content = client.get("/atlassian/wiki/rest/api/content", headers=admin_h).json()["results"][0]
    issue = crawl_jira(client, admin_h)[0]
    return {"space": space["key"], "content": str(content["id"]), "issue": issue}


def _routes(keys: dict[str, str]) -> list[str]:
    """Every Atlassian route, addressed at a resource the corpus serves."""
    k, c, i = keys["space"], keys["content"], keys["issue"]
    return [
        "/atlassian/rest/api/2/serverInfo",
        "/atlassian/rest/api/3/serverInfo",
        "/atlassian/rest/api/2/field",
        "/atlassian/rest/api/3/field",
        "/atlassian/rest/api/3/issueLinkType",
        "/atlassian/rest/api/3/project/search",
        f"/atlassian/rest/api/2/issue/{i}",
        f"/atlassian/rest/api/3/issue/{i}",
        f"/atlassian/rest/api/2/issue/{i}/comment",
        f"/atlassian/rest/api/3/issue/{i}/comment",
        "/atlassian/rest/api/2/search/jql?jql=&maxResults=1",
        "/atlassian/rest/api/3/search/jql?jql=&maxResults=1",
        "/atlassian/wiki/rest/api/space",
        f"/atlassian/wiki/rest/api/space/{k}",
        "/atlassian/wiki/rest/api/content",
        f"/atlassian/wiki/rest/api/content/{c}",
        f"/atlassian/wiki/rest/api/content/{c}/child/page",
        f"/atlassian/wiki/rest/api/content/{c}/child/comment",
        f"/atlassian/wiki/rest/api/content/{c}/label",
        f"/atlassian/wiki/rest/api/content/{c}/restriction/byOperation",
        "/atlassian/wiki/rest/api/search?cql=type%3Dpage",
    ]


def test_atlassian_a_head_is_the_get_without_its_body(client, admin_h, keys):
    """Measured on brekkylab.atlassian.net 2026-09-22, a `HEAD` beside each `GET` the same minute
    over all 24 routes: both products answer the `GET`'s status and `content-type` with nothing in
    the body, where FastAPI's ``APIRoute`` answered every one of them 405.

    The length is where the two part, and why ``errors.head_content_length`` exists. Jira declares
    none on either method. Confluence declares the `GET` body's own length on every 200 but
    `search`'s — `space` 1103, `content` 1536, `content/{id}` 3909, `child/comment` 214,
    `child/page` 211, `label` 207, `restriction/byOperation` 801, `space/{key}` 695, each read with
    `curl -I` against the `GET` beside it — and none on the 404, the 405 or the CQL 400.
    """
    for path in _routes(keys):
        got = client.get(path, headers=admin_h)
        head = client.head(path, headers=admin_h)
        assert head.status_code == got.status_code, path
        assert head.content == b"", path
        assert head.headers["content-type"] == got.headers["content-type"], path
        if "/wiki/" in path and not path.startswith("/atlassian/wiki/rest/api/search"):
            assert head.headers["content-length"] == str(len(got.content)), path
        else:
            assert "content-length" not in head.headers, path


def test_atlassian_a_head_on_a_refusal_declares_no_length_either(client, admin_h, keys):
    """The other half of the rule above, on the answers that are not 200s: real declares no length
    on Confluence's 404 for an unknown space, its 405, or Jira's 404 for an unknown issue."""
    for path in (
        "/atlassian/wiki/rest/api/space/NOPESUCH",
        "/atlassian/rest/api/3/issue/NOPE-1",
    ):
        got = client.get(path, headers=admin_h)
        head = client.head(path, headers=admin_h)
        assert head.status_code == got.status_code == 404, path
        assert head.content == b"" and "content-length" not in head.headers, path


@pytest.mark.parametrize(
    "path,allow",
    [
        # measured on brekkylab.atlassian.net 2026-09-22, one OPTIONS per route; the set is real's
        # and the order is one measured spelling, since it varied between two requests
        ("/atlassian/rest/api/3/serverInfo", "GET,HEAD,OPTIONS"),
        ("/atlassian/rest/api/2/field", "POST,GET,HEAD,OPTIONS"),
        ("/atlassian/rest/api/3/issue/NOPE-1", "PUT,GET,HEAD,DELETE,OPTIONS"),
        ("/atlassian/rest/api/3/issue/NOPE-1/comment", "GET,HEAD,POST,OPTIONS"),
        ("/atlassian/rest/api/2/search/jql", "GET,HEAD,POST,OPTIONS"),
        ("/atlassian/rest/api/3/issueLinkType", "POST,GET,HEAD,OPTIONS"),
        # the row that parts from the 405 table: its 405 resolves `/project/{idOrKey}` with
        # `search` read as a key and names five methods, where its OPTIONS names three
        ("/atlassian/rest/api/3/project/search", "GET,HEAD,OPTIONS"),
    ],
)
def test_jira_answers_an_options_with_the_vendors_methods(client, admin_h, path, allow):
    """Real answers 200 with an empty `text/html` body and an `Allow` naming the methods the VENDOR
    serves at that route — `PUT` and `DELETE` on an issue, `POST` on `field` — which Backlot serves
    none of. `Accept-Patch` rides along with an empty value on every one measured."""
    r = client.request("OPTIONS", path, headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == errors_atlassian.JIRA_OPTIONS_MEDIA_TYPE
    assert r.headers["allow"] == allow
    assert r.headers["accept-patch"] == ""
    assert r.content == b""


def test_confluence_answers_an_options_with_a_404_and_search_with_a_204(client, admin_h, keys):
    """Measured 2026-09-22: an `OPTIONS` on a Confluence route is 404 in the `errors` list its 405
    uses, on every route but `search`, which answers 204 naming its three methods."""
    for path in (
        "/atlassian/wiki/rest/api/space",
        f"/atlassian/wiki/rest/api/space/{keys['space']}",
        "/atlassian/wiki/rest/api/content",
        "/atlassian/wiki/rest/api/space/NOPESUCH",
    ):
        r = client.request("OPTIONS", path, headers=admin_h)
        assert r.status_code == 404, path
        assert r.json() == errors_atlassian.CONFLUENCE_OPTIONS_NOT_FOUND, path
    search = client.request("OPTIONS", "/atlassian/wiki/rest/api/search", headers=admin_h)
    assert search.status_code == 204
    assert search.headers["allow"] == errors_atlassian.CONFLUENCE_OPTIONS_204_ALLOW
    assert search.content == b""


def test_every_jira_route_carries_a_measured_options_allow(client):
    """The sibling of ``test_every_jira_route_carries_a_measured_allow``, for the OPTIONS table: a
    route added later without a row would answer an OPTIONS with no `Allow` at all."""
    paths = [
        p for p in client.get("/openapi.json").json()["paths"] if p.startswith("/atlassian/rest")
    ]
    assert paths
    assert [p for p in paths if errors_atlassian.jira_options_allow(p) is None] == []


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE", "OPTIONS", "PATCH"])
@pytest.mark.parametrize(
    "path",
    [
        "/atlassian/rest/api/3/nopesuchroute",
        "/atlassian/rest/api/2/nopesuchroute",
        "/atlassian/rest/api/3/serverInfo/extra",
        "/atlassian/rest/api/3/issue/NOPE-1/nope",
        "/atlassian/rest/api/4/serverInfo",
        "/atlassian/rest/nope/thing",
    ],
)
def test_jira_answers_a_path_it_mounts_nothing_at_as_rfc_7807(client, admin_h, method, path):
    """Measured 2026-09-22 on each of these paths: 404 `application/problem+json` naming the method
    as sent and the vendor path twice. Backlot answered the shared `statusCode` envelope."""
    r = client.request(method, path, headers=admin_h)
    vendor_path = path[len("/atlassian") :]
    assert r.status_code == 404, r.text
    assert r.headers["content-type"] == errors_atlassian.PROBLEM_JSON
    assert r.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": f"No endpoint {method} {vendor_path}.",
        "instance": vendor_path,
    }


def test_jira_no_endpoint_names_the_path_without_its_query(client, admin_h):
    """Real's `detail` and `instance` carry the path alone: the same request with a query string
    answered the same two fields."""
    plain = client.get("/atlassian/rest/api/3/nopesuchroute", headers=admin_h).json()
    with_query = client.get(
        "/atlassian/rest/api/3/nopesuchroute", headers=admin_h, params={"a": "1", "b": "2"}
    ).json()
    assert with_query == plain
    assert "?" not in plain["detail"] and "?" not in plain["instance"]


@pytest.mark.parametrize(
    "accept,media_type",
    [
        ("application/json", errors_atlassian.JAXRS_JSON_MEDIA_TYPE),
        ("*/*", errors_atlassian.JAXRS_XML_MEDIA_TYPE),
        ("application/xml", errors_atlassian.JAXRS_XML_MEDIA_TYPE),
    ],
)
def test_confluence_answers_an_unclaimed_segment_in_the_shape_accept_asks_for(
    client, admin_h, accept, media_type
):
    """Measured 2026-09-22: the shape follows `Accept` and not the credential — `application/json`
    by name answers JSON, and an absent header, `*/*` and `application/xml` each answer the XML
    document. The message carries the full request URL, query string included."""
    r = client.get(
        "/atlassian/wiki/rest/api/nopesuchroute",
        headers={**admin_h, "Accept": accept},
        params={"a": "1"},
    )
    assert r.status_code == 404, r.text
    assert r.headers["content-type"] == media_type
    assert "/wiki/rest/api/nopesuchroute?a=1" in r.text
    if media_type == errors_atlassian.JAXRS_JSON_MEDIA_TYPE:
        assert r.json()["status-code"] == 404
        assert r.json()["message"].startswith("null for uri: http")
    else:
        assert r.text.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><status>')
        assert "<status-code>404</status-code>" in r.text


@pytest.mark.parametrize(
    "path",
    [
        "/atlassian/wiki/rest/api/space/{space}/nope",
        "/atlassian/wiki/rest/api/space/NOPESUCH/deeper",
        "/atlassian/wiki/rest/api/content/{content}/nope",
        "/atlassian/wiki/rest/api/content/nope/deeper",
        "/atlassian/wiki/rest/nope",
        "/atlassian/wiki/nope",
    ],
)
def test_confluence_answers_the_products_page_below_a_resource_it_serves(
    client, admin_h, keys, path
):
    """Measured 2026-09-22: a path below `space/` or `content/`, and anything under `/wiki` outside
    the API mount, is the product's HTML 404 rather than the API's. Real's body is a ~30KB
    build-specific shell, so this serves the status and the media type with a stub."""
    r = client.get(path.format(**keys), headers=admin_h)
    assert r.status_code == 404, r.text
    assert r.headers["content-type"] == errors_atlassian.HTML_MEDIA_TYPE
    assert r.text == errors_atlassian.HTML_NOT_FOUND


def test_atlassian_a_method_a_route_does_not_declare_still_gets_the_405(client, admin_h):
    """The control for the catch-all: it takes every method on every path under `/atlassian`, so
    the 405s measured for both products have to survive it rather than turn into its 404."""
    jira = client.post("/atlassian/rest/api/3/serverInfo", headers=admin_h)
    assert jira.status_code == 405 and jira.headers["allow"] == "GET"
    conf = client.post("/atlassian/wiki/rest/api/space", headers=admin_h)
    assert conf.status_code == 405 and "allow" not in conf.headers
    assert conf.json()["errors"][0]["code"] == "METHOD_NOT_ALLOWED"


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def test_atlassian_every_answer_names_the_request(client, admin_h, keys):
    """Measured 2026-09-22 over 78 responses: both products put `atl-request-id` and `atl-traceid`
    on every answer, the second being the first without its dashes, and Jira adds a 32-hex
    `x-arequestid`. Backlot answered four headers and none of them an identifier.

    Real mints a new value per response; this one is derived from the request, the divergence
    ``backlot.routers.atlassian.request_ids`` states and this pins — a corpus served twice answers
    the same id, and two different requests do not share one.
    """
    answers = {
        "jira 200": client.get("/atlassian/rest/api/3/serverInfo", headers=admin_h),
        "jira 404": client.get("/atlassian/rest/api/3/nopesuchroute", headers=admin_h),
        "jira 405": client.post("/atlassian/rest/api/3/serverInfo", headers=admin_h),
        "jira options": client.request(
            "OPTIONS", "/atlassian/rest/api/3/serverInfo", headers=admin_h
        ),
        "confluence 200": client.get("/atlassian/wiki/rest/api/space", headers=admin_h),
        "confluence 404": client.get("/atlassian/wiki/rest/api/space/NOPESUCH", headers=admin_h),
        "confluence options": client.request(
            "OPTIONS", "/atlassian/wiki/rest/api/space", headers=admin_h
        ),
    }
    for label, r in answers.items():
        request_id = r.headers["atl-request-id"]
        assert _UUID.fullmatch(request_id), (label, request_id)
        assert r.headers["atl-traceid"] == request_id.replace("-", ""), label
        if label.startswith("jira"):
            assert re.fullmatch(r"[0-9a-f]{32}", r.headers["x-arequestid"]), label
        else:
            assert "x-arequestid" not in r.headers, label
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


@pytest.mark.parametrize(
    "path,policy,limit",
    [
        # every row measured 2026-09-22, reading the four headers off that route's own answer
        ("/atlassian/rest/api/3/serverInfo", 100, 350),
        ("/atlassian/rest/api/3/project/search", 100, 350),
        ("/atlassian/rest/api/3/issue/NOPE-1", 150, 400),
        ("/atlassian/rest/api/3/project/ENG/role/10002", 200, 500),
    ],
)
def test_jira_reports_the_burst_quota_of_the_route(
    client, admin_h, frozen_burst, path, policy, limit
):
    """Real's quota is per route: 350 on most, 400 on an issue, 500 on a project role. `remaining`
    counts down inside the window the policy names — a burst of six read 349…344 — and a second
    route under the same limit shares the count."""
    first = client.get(path, headers=admin_h)
    assert first.headers["x-ratelimit-limit"] == str(limit)
    assert first.headers["x-ratelimit-remaining"] == str(limit - 1)
    assert first.headers["ratelimit"] == f'"jira-burst-based";r={limit - 1};t=1'
    assert first.headers["ratelimit-policy"] == f'"jira-burst-based";q={policy};w=1'
    again = client.get(path, headers=admin_h)
    assert again.headers["x-ratelimit-remaining"] == str(limit - 2)


def test_jira_burst_quota_is_shared_by_the_routes_under_one_limit(client, admin_h, frozen_burst):
    """Measured: `field` and `serverInfo` sit in the 350 bucket and counted down together, where
    `issue/{key}` has a bucket of its own."""
    client.get("/atlassian/rest/api/3/serverInfo", headers=admin_h)
    field = client.get("/atlassian/rest/api/3/field", headers=admin_h)
    assert field.headers["x-ratelimit-remaining"] == "348"
    issue = client.get("/atlassian/rest/api/3/issue/NOPE-1", headers=admin_h)
    assert issue.headers["x-ratelimit-remaining"] == "399"


@pytest.mark.parametrize("headers", UNRESOLVABLE)
def test_jira_sends_no_quota_and_no_account_id_to_an_anonymous_caller(client, headers):
    """Measured 2026-09-22: an anonymous request carries the two ids, `x-arequestid` and the
    `cache-control`, and none of the five that describe the caller or its quota."""
    r = client.get("/atlassian/rest/api/3/serverInfo", headers=headers)
    assert r.status_code == 200
    assert r.headers["atl-request-id"] and r.headers["x-arequestid"]
    assert r.headers["cache-control"] == "no-cache, no-store, no-transform"
    for absent in (
        "x-aaccountid",
        "ratelimit",
        "ratelimit-policy",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
    ):
        assert absent not in r.headers, absent


def test_jira_sends_no_quota_on_a_path_it_mounts_nothing_at(client, admin_h):
    """The other half of that rule: the no-endpoint 404 carried the ids and the account id but none
    of the rate-limit four, measured on `/rest/api/3/nopesuchroute` with a credential."""
    r = client.get("/atlassian/rest/api/3/nopesuchroute", headers=admin_h)
    assert r.status_code == 404
    assert r.headers["atl-request-id"] and r.headers["x-aaccountid"]
    assert "x-ratelimit-limit" not in r.headers


def test_jira_echoes_the_callers_own_account_id(client, tokens):
    """Real echoes the caller's account id on every Jira answer once the credential resolves; here
    it is the same id the corpus serves that user under (``synth.atlassian_account_id``)."""
    from backlot import synth

    email, token = sorted(tokens.items())[0]
    r = client.get("/atlassian/rest/api/3/serverInfo", headers={"Authorization": f"Bearer {token}"})
    assert r.headers["x-aaccountid"] == synth.atlassian_account_id(email)


def test_confluence_says_its_v1_rest_api_is_deprecated(client, admin_h, keys):
    """Measured 2026-09-22: the content and space services send the three headers on every answer
    they give, their 404s included, and `search`, `restriction/byOperation` and the 405 at
    `space/{key}/permission` send none. The dates are real's own, a removal date already past."""
    carries = [
        "/atlassian/wiki/rest/api/space",
        f"/atlassian/wiki/rest/api/space/{keys['space']}",
        "/atlassian/wiki/rest/api/content",
        f"/atlassian/wiki/rest/api/content/{keys['content']}/child/page",
        f"/atlassian/wiki/rest/api/content/{keys['content']}/label",
        "/atlassian/wiki/rest/api/space/NOPESUCH",
    ]
    for path in carries:
        r = client.get(path, headers=admin_h)
        assert r.headers["deprecation"] == "Wed, 1 Mar 2023 00:00:00 GMT", path
        assert r.headers["warning"].startswith('299 - "Deprecated API'), path
        assert 'rel="deprecation"' in r.headers["link"], path
    for path in (
        "/atlassian/wiki/rest/api/search?cql=type%3Dpage",
        f"/atlassian/wiki/rest/api/content/{keys['content']}/restriction/byOperation",
    ):
        assert "deprecation" not in client.get(path, headers=admin_h).headers, path


def test_confluence_sends_no_deprecation_notice_to_a_caller_it_refuses(client):
    """Measured: the 403 an anonymous request gets carries the ids and the clock, and none of the
    three — it never reached the service that stamps them."""
    r = client.get("/atlassian/wiki/rest/api/space")
    assert r.status_code == 403
    assert r.headers["atl-request-id"] and r.headers["x-confluence-request-time"]
    assert "deprecation" not in r.headers and "warning" not in r.headers


def test_confluence_stamps_a_millisecond_clock_and_jira_does_not(client, admin_h):
    """`x-confluence-request-time` is a 13-digit millisecond epoch on every Confluence answer
    measured, and no Jira answer carries one."""
    conf = client.get("/atlassian/wiki/rest/api/space", headers=admin_h)
    assert re.fullmatch(r"\d{13}", conf.headers["x-confluence-request-time"])
    jira = client.get("/atlassian/rest/api/3/serverInfo", headers=admin_h)
    assert "x-confluence-request-time" not in jira.headers


def test_atlassian_answers_carry_nosniff(client, admin_h):
    """`x-content-type-options: nosniff` is on every answer either product gives."""
    for path in ("/atlassian/rest/api/3/serverInfo", "/atlassian/wiki/rest/api/space"):
        assert client.get(path, headers=admin_h).headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "path",
    [
        "/atlassian/rest/api/3/serverInfo",
        "/atlassian/rest/api/3/nopesuchroute",
        "/atlassian/wiki/rest/api/space",
    ],
)
def test_atlassian_a_method_the_front_door_refuses_carries_none_of_the_headers(
    client, admin_h, path
):
    """The control for the catch-all's method list and for the header middleware. Real refuses a
    `TRACE` with 405 and an invented method with 403, the gateway's own HTML page, carrying no
    `Allow` and none of the ids, quota or deprecation headers the application adds — on a served
    route and on an unserved path alike, measured 2026-09-22. `TRACE` is left off the catch-all's
    methods for that reason, so what answers it here is a 405 too, and this holds that the 405
    advertises nothing: Starlette would otherwise name the seven methods the catch-all takes."""
    r = client.request("TRACE", path, headers=admin_h)
    assert r.status_code == 405, r.text
    assert "allow" not in r.headers
    for absent in (
        "atl-request-id",
        "atl-traceid",
        "x-arequestid",
        "x-ratelimit-limit",
        "deprecation",
        "x-confluence-request-time",
    ):
        assert absent not in r.headers, absent


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
    """Measured 2026-09-22: a trailing slash, a doubled one and a run in the middle each answer
    what the canonical spelling answers, on both products. Starlette redirected the first two with
    a 307 and, once the catch-all was mounted, would have refused every one of them outright."""
    canonical = re.sub("/{2,}", "/", path).rstrip("/")
    served = client.get(path, headers=admin_h, follow_redirects=False)
    assert served.status_code == 200, served.text
    assert served.json() == client.get(canonical, headers=admin_h).json()


def test_atlassian_a_head_and_an_options_follow_the_same_normalisation(client, admin_h):
    """Measured on the slashed spelling: the `HEAD` is the `GET`'s 200 with no body and the
    `OPTIONS` is the route's own `Allow`, rather than the answer for a path nothing serves."""
    head = client.head("/atlassian/rest/api/3/serverInfo/", headers=admin_h)
    assert head.status_code == 200 and head.content == b""
    options = client.request("OPTIONS", "/atlassian/rest/api/3/serverInfo/", headers=admin_h)
    assert options.status_code == 200 and options.headers["allow"] == "GET,HEAD,OPTIONS"


def test_atlassian_a_refusal_keeps_a_trailing_slash_and_collapses_an_interior_run(client, admin_h):
    """What a refusal ECHOES is normalised differently from what routing reads. Measured: real
    answers `No endpoint GET /rest/api/3/nopesuchroute/.` for the trailing slash — the slash kept
    in both `detail` and `instance` — and `/rest/api/3/nopesuchroute` for `/api/3//nopesuchroute`,
    the interior run collapsed. Confluence's `null for uri:` message draws the same two lines."""
    trailing = client.get("/atlassian/rest/api/3/nopesuchroute/", headers=admin_h).json()
    assert trailing["detail"] == "No endpoint GET /rest/api/3/nopesuchroute/."
    assert trailing["instance"] == "/rest/api/3/nopesuchroute/"
    interior = client.get("/atlassian/rest/api/3//nopesuchroute", headers=admin_h).json()
    assert interior["detail"] == "No endpoint GET /rest/api/3/nopesuchroute."
    assert interior["instance"] == "/rest/api/3/nopesuchroute"
    json_headers = {**admin_h, "Accept": "application/json"}
    conf_trailing = client.get("/atlassian/wiki/rest/api/nopesuchroute/", headers=json_headers)
    assert conf_trailing.json()["message"].endswith("/wiki/rest/api/nopesuchroute/")
    conf_interior = client.get("/atlassian/wiki/rest/api//nopesuchroute", headers=json_headers)
    assert conf_interior.json()["message"].endswith("/wiki/rest/api/nopesuchroute")


def test_atlassian_the_mount_root_answers_rather_than_redirecting_to_itself(client, admin_h):
    """`/atlassian/` is the mount rather than a path under it. Stripping its slash would leave a
    path no route matches, and Starlette's slash redirect would send the client back to the
    spelling it just asked for — so it keeps its slash and reaches the catch-all."""
    r = client.get("/atlassian/", headers=admin_h, follow_redirects=False)
    assert r.status_code == 404
    assert r.json()["detail"] == "No endpoint GET /."
