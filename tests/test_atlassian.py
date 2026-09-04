"""Atlassian: Jira issues/JQL and Confluence content/CQL — one router, one file.

One file per router, so a source's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

from starlette.requests import Request
import base64
import re

import pytest

from backlot import store
from backlot.errors import atlassian as errors_atlassian
from tests._helpers import (
    bare_request,
    client_for,
    crawl_confluence,
    crawl_jira,
    db_count,
    tiny_corpus,
    served_id,
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


def test_jira_lists_only_the_projects_the_caller_can_open(tmp_path):
    """The anonymous listing is empty because a project is listed when the caller can see an issue
    in it, and that rule is not anonymous-only: a scoped caller who can open nothing in a project
    does not get it either. Backlot grants per document, so the projects have to be read off the
    issues — listing every one of them to everybody was how an empty anonymous listing could not
    be told from a full one."""
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
    ]
    settings = tiny_corpus(tmp_path, corpus)
    with client_for(settings, reload=True) as c:
        import yaml

        written = yaml.safe_load(settings.tokens_path.read_text())
        tokens = {u["email"]: u["token"] for u in written["users"]}
        tokens["admin"] = written["admin_token"]

        def keys(headers):
            listing = c.get("/atlassian/rest/api/3/project/search", headers=headers).json()
            return sorted(p["name"] for p in listing["values"])

        assert keys({"Authorization": f"Bearer {tokens['admin']}"}) == ["payments", "secrets"]
        assert keys({"Authorization": f"Bearer {tokens['bob@acme.com']}"}) == [
            "payments",
            "secrets",
        ]
        assert keys({"Authorization": f"Bearer {tokens['ava@acme.com']}"}) == ["payments"]
        assert keys({}) == []


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
    assert body["message"] == errors_atlassian.CONFLUENCE_FORBIDDEN
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

    # no project clause at all -> unfiltered (same three issues here, since payments is the
    # only Jira project in the SAMPLE corpus -- the earlier assertions are what prove filtering,
    # not this equality)
    unfiltered = client.get("/atlassian/rest/api/3/search/jql", headers=admin_h).json()
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


def test_confluence_single_space_get(client, admin_h):
    spaces = client.get("/atlassian/wiki/rest/api/space", headers=admin_h).json()["results"]
    assert spaces
    key = spaces[0]["key"]
    r = client.get(f"/atlassian/wiki/rest/api/space/{key}", headers=admin_h)
    assert r.status_code == 200 and r.json()["key"] == key and r.json()["name"] == spaces[0]["name"]
    # unknown space -> clean atlassian-shaped 404
    r2 = client.get("/atlassian/wiki/rest/api/space/NOSUCH", headers=admin_h)
    assert r2.status_code == 404 and "message" in r2.json()


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
    search = client.get("/atlassian/rest/api/3/search/jql", headers=admin_h).json()
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
