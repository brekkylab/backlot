"""Assertions that span vendors, and so belong to no single router's file.

Over the conftest SAMPLE corpus (built into a tmp dir — hermetic, so the suite neither depends on
nor crawls whatever ambient import lives in ``data/``): a non-admin's crawl is a strict subset of
the admin's across every source at once, content round-trips byte-for-byte through each vendor's
encoding, and the ``/_meta/*`` affordances behave.

Each vendor's own endpoints live in ``test_<router>.py`` — the per-router crawlers those tests and
these share are in ``tests/_helpers.py``.
"""

from __future__ import annotations

import asyncio
import sqlite3
import urllib.parse

import pytest

from backlot.graphql import body as request_body
from backlot.graphql import v8_json
from tests._helpers import build_corpus, client_for, crawl_confluence, db_count, padded_query


def test_unauthenticated_request_reports_the_vendors_own_401_detail(client):
    """The message is part of the emulated surface — a client that string-matches its vendor's
    error has to keep matching — which is why the shared guard takes it as a parameter.

    GitHub only: Google does not go through `auth.require_bearer`, because its answer is not one
    status (a 403 or a 401 by operation, ``backlot.errors.google.no_credentials``) and it carries
    Google's own error envelope, not `detail`. That surface is covered by ``tests/test_google.py``.

    GitHub carries its own envelope too, and splits the 401 by cause the way real does: a request
    with no usable credential is "Requires authentication", a credential that arrived and did not
    resolve is "Bad credentials"."""
    missing = client.get("/github/orgs/acme")
    assert missing.status_code == 401
    assert missing.json() == {
        "message": "Requires authentication",
        "documentation_url": "https://docs.github.com/rest",
        "status": "401",
    }
    bad = client.get("/github/orgs/acme", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    assert bad.json()["message"] == "Bad credentials"


# --- ACL enforcement over HTTP --------------------------------------------------


def test_user_sees_subset_of_admin(client, admin_h, tokens_yaml, ro_conn, sample_settings):
    user = tokens_yaml["users"][0]
    uh = {"Authorization": f"Bearer {user['token']}"}
    admin_conf = len(crawl_confluence(client, admin_h))
    user_conf = len(crawl_confluence(client, uh))
    assert user_conf < admin_conf  # some confluence docs are group/private-restricted
    # matches exactly the ACL-computed visible count
    from backlot.acl import Acl

    acl = Acl.load(
        sample_settings.tokens_path, sample_settings.admin_token, sample_settings.org_name
    )
    vids = acl.visible_ids(ro_conn, acl.resolve(user["token"]))
    assert user_conf == db_count(ro_conn, "confluence", visible_ids=vids)


def test_meta_users_directory(client, tokens_yaml, org):
    # the /_meta/users directory lists every user + token (for testing per-user ACL)
    from backlot import synth

    body = client.get("/_meta/users").json()
    assert body["admin_token"] == tokens_yaml["admin_token"]
    # S3 uses an AWS keypair, not a token — the directory exposes an admin pair (derived from the
    # admin token, which is what the SigV4 verifier resolves) so a client can use it directly
    assert body["admin_s3_access_key_id"] == synth.s3_access_key_id(body["admin_token"])
    assert body["admin_s3_secret_access_key"] == synth.s3_secret_access_key(body["admin_token"])
    yaml_by_email = {u["email"]: u["token"] for u in tokens_yaml["users"]}
    assert body["count"] == len(body["users"]) == len(yaml_by_email) > 0
    for u in body["users"]:
        assert u["token"] == yaml_by_email[u["email"]]  # matches data/tokens_yaml.yaml
        assert u["name"] and isinstance(u["groups"], list)
        # each user also carries their derived S3 access-key/secret pair
        assert u["s3_access_key_id"] == synth.s3_access_key_id(u["token"])
        assert u["s3_secret_access_key"] == synth.s3_secret_access_key(u["token"])
    # a listed token really is ACL-scoped: it resolves and sees <= what admin sees
    u = body["users"][0]
    admin_repos = client.get(
        f"/github/orgs/{org}/repos", headers={"Authorization": f"Bearer {body['admin_token']}"}
    ).json()
    user_repos = client.get(
        f"/github/orgs/{org}/repos", headers={"Authorization": f"Bearer {u['token']}"}
    ).json()
    assert 0 < len(user_repos) <= len(admin_repos)


def test_unauthenticated_is_rejected(client):
    # Drive's `files.list` takes API keys, so an anonymous GET is an "unregistered caller" -> 403,
    # not 401. A bad token IS 401. Both measured; see tests/test_google.py.
    assert client.get("/drive/v3/files").status_code == 403
    assert (
        client.get("/drive/v3/files", headers={"Authorization": "Bearer nope"}).status_code == 401
    )
    # Atlassian splits: Jira serves the read as an anonymous caller (which reaches no document),
    # Confluence refuses outright with its own 403. Both measured; see tests/test_atlassian.py.
    jira = client.get("/atlassian/rest/api/3/project/search")
    assert jira.status_code == 200 and jira.json()["values"] == []
    assert client.get("/atlassian/wiki/rest/api/space").status_code == 403
    slack = client.post("/slack/api/conversations.list").json()
    assert slack == {"ok": False, "error": "not_authed"}


# --- OpenAPI enrichment: the params each router advertises ---------------------------------
# The routers read query params off the raw request rather than through FastAPI signatures, so
# each has to declare what it honours by hand (openapi.qp). One table rather than a test per
# router: the assertion is identical and only the path and the expected names differ.


_ADVERTISED_PARAMS = [
    ("/github/search/issues", {"q", "page", "per_page"}),
    ("/slack/api/search.messages", {"query", "count", "page"}),
    ("/slack/api/conversations.history", {"channel", "limit", "cursor"}),
    # `user_id` is the path param, asserted here so enrichment cannot drop it
    ("/gmail/v1/users/{user_id}/messages", {"q", "maxResults", "pageToken", "user_id"}),
    ("/drive/v3/files", {"q", "pageSize", "pageToken", "fields"}),
    ("/notion/v1/users", {"start_cursor", "page_size"}),
    ("/atlassian/rest/api/3/search/jql", {"jql", "maxResults", "nextPageToken"}),
    ("/atlassian/wiki/rest/api/search", {"cql"}),
]


def test_router_advertises_the_params_it_honours(client):
    paths = client.get("/openapi.json").json()["paths"]
    unadvertised = {
        path: expected - {p["name"] for p in paths[path]["get"].get("parameters", [])}
        for path, expected in _ADVERTISED_PARAMS
    }
    assert unadvertised == {path: set() for path, _ in _ADVERTISED_PARAMS}


# --- /_meta/openapi/{source}: the MCP-ready spec endpoint ---------------


def test_meta_openapi_spec_endpoint(client):
    gh = client.get("/_meta/openapi/github")
    assert gh.status_code == 200
    ids = [
        op["operationId"]
        for item in gh.json()["paths"].values()
        for m, op in item.items()
        if isinstance(op, dict) and "operationId" in op
    ]
    assert ids and len(ids) == len(set(ids)), (
        "served spec must have unique operationIds (bridge-ready)"
    )
    # s3 is bridged too — the bridge signs each request rather than holding a fixed header
    assert client.get("/_meta/openapi/s3").status_code == 200
    assert client.get("/_meta/openapi/nope").status_code == 404


# --- /health: source_documents beside documents ---------------------------------------------


def _join_warm_thread(main_module) -> None:
    """Wait for the background per-source COUNT(*) cache (see lifespan._warm_caches) to land,
    deterministically — /health's `documents`/`by_source` are None/{} until it does, and a
    fresh-process cold start loses that race against an immediate request 100% of the time (no
    poll budget is safe on a slower CI runner), so the test has to join the thread, not wait on it."""
    warm = main_module.app.state.warm_thread
    warm.join(timeout=10)
    assert not warm.is_alive(), "cache warm-up did not finish within 10s"


def test_health_reports_both_counts(tmp_path):
    """Both numbers, always: the row count alone reads as inflated."""
    from backlot import main as main_module

    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "channel": "incidents",
                "author_email": "bob@acme.com",
                "content": "Anyone seeing 502s?",
                "replies": [{"content": "Looking.", "author_email": "ava@acme.com"}],
            },
        ],
    )
    with client_for(settings, reload=True) as client:
        _join_warm_thread(main_module)
        body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["source_documents"] == 1
    assert body["documents"] == 2


def _one_slack_doc(tmp_path):
    return build_corpus(
        tmp_path,
        [
            {
                "source_type": "slack",
                "channel": "incidents",
                "author_email": "bob@acme.com",
                "content": "Anyone seeing 502s?",
            },
        ],
    )


def test_a_failing_warm_up_is_recorded_instead_of_dying_with_its_thread(tmp_path, monkeypatch):
    """The warm-up runs in a daemon thread, so an exception inside it vanishes with the thread.

    Every cache then stayed None, which each consumer reads as "not warm yet" — the Slack routes fall
    back to per-request queries and keep answering correctly — so nothing 500d, nothing retried, and
    a broken warm-up was indistinguishable from a slow one for the life of the process.
    """
    from backlot import main as main_module
    from backlot import store

    settings = _one_slack_doc(tmp_path)
    # The thread is the only caller of this, so patching it cannot disturb the serving path.
    monkeypatch.setattr(
        store,
        "conversation_member_counts",
        lambda conn, src: (_ for _ in ()).throw(
            sqlite3.OperationalError("no such table: whatever")
        ),
    )
    with client_for(settings, reload=True) as client:
        _join_warm_thread(main_module)
        assert main_module.app.state.warm_error, "the failure was swallowed"
        response = client.get("/health")
        body = response.json()

    assert response.status_code == 200, "a working server must not fail its own healthcheck"
    assert body["status"] == "degraded", body
    # Reported even though the earlier caches DID land: the warm-up fills three in sequence, and
    # `degraded` without the reason is a worse answer than either "ok" or a plain failure.
    assert "no such table" in body["warm_error"], body

    # Still degraded rather than down — the corpus reads correctly through the fallback path.
    admin = {"Authorization": f"Bearer {settings.admin_token}"}
    with client_for(settings) as client:
        listed = client.get("/slack/api/conversations.list", headers=admin).json()
    assert [c["name"] for c in listed["channels"]] == ["incidents"]


def test_health_reports_degraded_when_no_counts_ever_landed(tmp_path):
    """The shape a total warm-up failure leaves: no counts, and an error to explain them.

    Asserted on the rendering rather than by breaking the thread, because what a load balancer and
    an operator see is this body — previously `status: "ok"` with `documents: null` forever.
    """
    from backlot import main as main_module

    settings = _one_slack_doc(tmp_path)
    with client_for(settings, reload=True) as client:
        _join_warm_thread(main_module)
        main_module.app.state.doc_counts = None
        main_module.app.state.warm_error = "OperationalError: unable to open database file"
        body = client.get("/health").json()

    assert body["status"] == "degraded", body
    assert body["documents"] is None and body["by_source"] == {}
    assert body["warm_error"] == "OperationalError: unable to open database file"


def test_health_source_documents_is_null_without_the_meta_key(tmp_path):
    """A DB built before the meta table must still answer /health."""
    from backlot import main as main_module
    from backlot import store

    settings = build_corpus(
        tmp_path,
        [
            {
                "source_type": "confluence",
                "space": "h",
                "title": "A",
                "content": "a",
                "author_email": "ava@acme.com",
            },
        ],
    )
    conn = store.connect_rw(settings.db_path)
    conn.execute("DELETE FROM meta WHERE key = 'source_documents'")
    conn.commit()
    conn.close()
    with client_for(settings, reload=True) as client:
        _join_warm_thread(main_module)
        body = client.get("/health").json()
    assert body["source_documents"] is None
    assert body["documents"] == 1


# A variable declared `Int` holding a number as the body spells it, and that number as both vendors
# quoted it back, which the docstrings of `backlot.graphql.body._int` and `Double` record.
_NUMBERS = [
    pytest.param("1" * 4301, "Infinity", id="4301 ones"),
    pytest.param("1" * 311, "Infinity", id="311 ones"),
    pytest.param("9" * 309, "Infinity", id="309 nines"),
    pytest.param("1e400", "Infinity", id="1e400"),
    pytest.param("-1e400", "-Infinity", id="-1e400"),
    pytest.param("9" * 308, "1e+308", id="308 nines"),
    pytest.param("9007199254740993", "9007199254740992", id="2**53 + 1"),
    pytest.param("734946803407896717", "734946803407896700", id="past 2**53"),
    pytest.param("123456789012345678901234", "1.2345678901234569e+23", id="24 digits"),
    pytest.param("1e21", "1e+21", id="1e21"),
    pytest.param("1e20", "100000000000000000000", id="1e20"),
    pytest.param("3000000000.0", "3000000000", id="3000000000.0"),
    pytest.param("0.00001", "0.00001", id="0.00001"),
    pytest.param("1e-7", "1e-7", id="1e-7"),
]


@pytest.mark.parametrize(
    "vendor, document",
    [
        ("linear", "query($x: Int) { issues(first: $x) { nodes { id } } }"),
        ("fireflies", "query($x: Int) { transcripts(limit: $x) { id } }"),
    ],
)
def test_a_graphql_vendor_quotes_a_number_as_javascript_prints_it(
    client, tokens_yaml, vendor, document
):
    """The variable's value is read as the double ``JSON.parse`` reads and quoted as JavaScript
    prints it, at both places graphql-js quotes it."""
    admin = tokens_yaml["admin_token"]
    credential = admin if vendor == "linear" else f"Bearer {admin}"
    answered = {}
    for row in _NUMBERS:
        number, quoted = row.values
        body = '{"query":"%s","variables":{"x":%s}}' % (document, number)
        r = client.post(
            f"/{vendor}/graphql",
            content=body.encode(),
            headers={"Authorization": credential, "Content-Type": "application/json"},
        )
        message = (r.json().get("errors") or [{}])[0].get("message", "")
        answered[row.id] = (
            r.status_code,
            f" got invalid value {quoted}; Int cannot represent " in message,
            message.endswith(f" value: {quoted}"),
        )
    assert answered == {row.id: (400, True, True) for row in _NUMBERS}


# A request to Linear or Fireflies that Cloudflare answers itself, `{key}` standing for the vendor's
# own spelling of the admin token, and the status, media type, length, `Connection` and body of the
# answer, as `backlot.main.refuse_cloudflares_methods_on_linear_and_fireflies` and the
# repeated-header middleware it names record them. The last three rows are the control: one header
# reaches the vendor, and a path that only starts with the vendor's name is not the vendor's.
_TYPENAME = b'{"query":"{ __typename }"}'
_CLOUDFLARE = 400
_PAGE = (
    b"<html>\r\n<head><title>%s</title></head>\r\n<body>\r\n<center><h1>%s</h1></center>\r\n"
    b"<hr><center>cloudflare</center>\r\n</body>\r\n</html>\r\n"
)
_TWICE = (_CLOUDFLARE, "text/html", "155", "close", _PAGE % ((b"400 Bad Request",) * 2))
_NOT_ALLOWED = (405, "text/html", "155", "close", _PAGE % ((b"405 Not Allowed",) * 2))
_ERROR_1045 = (400, "text/plain; charset=UTF-8", "17", None, b"error code: 1045\n")
_PURGED = (400, "text/html; charset=UTF-8", "155", None, _PAGE % ((b"400 Bad Request",) * 2))


def _auth(*values: str) -> tuple:
    return tuple(("Authorization", value) for value in values)


_ETAG_TWICE = (("If-None-Match", '"x"'),) * 2
_CLOUDFLARE_FIRST = [
    pytest.param("POST", "/graphql", _TYPENAME, _auth("{key}", "{key}"), _TWICE, id="two keys"),
    pytest.param("POST", "/graphql", _TYPENAME, _auth("{key}", "nope"), _TWICE, id="a key, nope"),
    pytest.param("POST", "/graphql", _TYPENAME, _auth("nope", "{key}"), _TWICE, id="nope, a key"),
    pytest.param("POST", "/graphql", _TYPENAME, _auth("{key}", ""), _TWICE, id="a key, empty"),
    pytest.param("POST", "/graphql", _TYPENAME, _auth("", ""), _TWICE, id="two empty"),
    pytest.param("POST", "/graphql", _TYPENAME, _auth("{key}") * 3, _TWICE, id="three keys"),
    pytest.param("POST", "/graphql", b"", _auth("{key}", "nope"), _TWICE, id="no bytes"),
    pytest.param("POST", "/graphql", b"{", _auth("{key}", "nope"), _TWICE, id="{"),
    pytest.param("POST", "/graphql", None, _auth("{key}", "nope"), _TWICE, id="past the limit"),
    pytest.param("GET", "/graphql", b"", _auth("{key}", "nope"), _TWICE, id="GET"),
    pytest.param("HEAD", "/graphql", b"", _auth("{key}", "nope"), _TWICE, id="HEAD"),
    pytest.param("POST", "/nosuch", _TYPENAME, _auth("{key}", "nope"), _TWICE, id="another path"),
    pytest.param("POST", "", _TYPENAME, _auth("{key}", "nope"), _TWICE, id="the root"),
    *(
        pytest.param(m, path, b"", _auth("{key}"), answer, id=f"{m} {path or '/'}")
        for m, answer in (("TRACE", _NOT_ALLOWED), ("CONNECT", _ERROR_1045), ("PURGE", _PURGED))
        for path in ("/graphql/", "/nosuch", "")
    ),
    *(
        pytest.param(m, "/graphql", b"", _auth("{key}", "{key}"), _TWICE, id=f"{m}, two keys")
        for m in ("CONNECT", "PURGE")
    ),
    *(
        pytest.param(
            m, "/graphql", b"", (*_auth("{key}"), *_ETAG_TWICE), _TWICE, id=f"{m}, ETag twice"
        )
        for m in ("TRACE", "CONNECT", "PURGE")
    ),
    pytest.param("POST", "/graphql", _TYPENAME, _auth("{key}"), 200, id="one key"),
    pytest.param("POST", "x/graphql", _TYPENAME, _auth("{key}", "nope"), 404, id="a longer name"),
    pytest.param("TRACE", "x/graphql", b"", _auth("{key}"), 404, id="TRACE, a longer name"),
]


@pytest.mark.parametrize("vendor", ["linear", "fireflies"])
def test_cloudflare_answers_ahead_of_linear_and_fireflies(client, tokens_yaml, vendor):
    admin = tokens_yaml["admin_token"]
    key = admin if vendor == "linear" else f"Bearer {admin}"
    answered, expected = {}, {}
    for row in _CLOUDFLARE_FIRST:
        method, path, body, sent, answer = row.values
        if body is None:
            body = padded_query({"linear": 10 * 2**20, "fireflies": 2**20}[vendor] + 1)
        headers = [("Content-Type", "application/json")]
        headers += [(name, value.format(key=key)) for name, value in sent]
        r = client.request(method, f"/{vendor}{path}", content=body, headers=headers)
        if isinstance(answer, int):
            answered[row.id] = (
                r.status_code,
                b"cloudflare" in r.content,
                "connection" in r.headers,
                r.json() if r.status_code == 200 else None,
            )
            expected[row.id] = (
                answer,
                False,
                False,
                {"data": {"__typename": "Query"}} if answer == 200 else None,
            )
            continue
        answered[row.id] = (
            r.status_code,
            r.headers.get("content-type"),
            r.headers.get("content-length"),
            r.headers.get("connection"),
            r.content,
        )
        expected[row.id] = (*answer[:4], b"" if method == "HEAD" else answer[4])
    assert answered == expected


# The same page on each vendor Cloudflare fronts, `{key}` as above, for each header it refuses
# sent more than once. The controls: one copy reaches the vendor, and `Cache-Control` or `Range`
# twice reaches it too. The one-copy `If-None-Match` sends a tag, not `*`: api.notion.com answers
# a single `*` on `GET /v1/users/me` with a 304, where these controls assert the 200.
_CLOUDFLARE_REQUESTS = [
    ("notion", "GET", "/v1/users/me", None, (("Notion-Version", "2025-09-03"),)),
    ("linear", "POST", "/graphql", _TYPENAME, (("Content-Type", "application/json"),)),
    ("fireflies", "POST", "/graphql", _TYPENAME, (("Content-Type", "application/json"),)),
    ("hubspot", "GET", "/crm/v3/objects/contacts?limit=1", None, ()),
]
_CLOUDFLARE_REFUSED = [
    ("Authorization", "{key}"),
    ("If-None-Match", '"x"'),
    ("If-Match", '"x"'),
    ("If-Modified-Since", "Wed, 01 Jan 2025 00:00:00 GMT"),
    ("If-Unmodified-Since", "Wed, 01 Jan 2025 00:00:00 GMT"),
    ("If-Range", '"x"'),
]
_CLOUDFLARE_SERVED_TWICE = [
    ("Cache-Control", "no-cache"),
    ("Range", "bytes=0-99"),
]


def test_cloudflare_refuses_a_repeated_header(client, tokens_yaml):
    admin = tokens_yaml["admin_token"]
    page = (
        b"<html>\r\n<head><title>400 Bad Request</title></head>\r\n<body>\r\n"
        b"<center><h1>400 Bad Request</h1></center>\r\n<hr><center>cloudflare</center>\r\n"
        b"</body>\r\n</html>\r\n"
    )
    answered, expected = {}, {}
    for vendor, method, path, body, extra in _CLOUDFLARE_REQUESTS:
        key = admin if vendor == "linear" else f"Bearer {admin}"
        for name, value in _CLOUDFLARE_REFUSED:
            for copies, status in ((2, _CLOUDFLARE), (1, 200)):
                headers = list(extra)
                headers += [(name, value.format(key=key)) for _ in range(copies)]
                if name != "Authorization":
                    headers.append(("Authorization", key))
                r = client.request(method, f"/{vendor}{path}", content=body, headers=headers)
                label = f"{vendor} {name} x{copies}"
                if status == _CLOUDFLARE:
                    answered[label] = (
                        r.status_code,
                        r.headers.get("content-type"),
                        r.headers.get("content-length"),
                        r.headers.get("connection"),
                        r.content,
                    )
                    expected[label] = (400, "text/html", "155", "close", page)
                else:
                    answered[label] = (r.status_code, page in r.content)
                    expected[label] = (200, False)
        for name, value in _CLOUDFLARE_SERVED_TWICE:
            headers = list(extra) + [(name, value)] * 2 + [("Authorization", key)]
            r = client.request(method, f"/{vendor}{path}", content=body, headers=headers)
            label = f"{vendor} {name} twice"
            answered[label] = (r.status_code, page in r.content)
            expected[label] = (200, False)
    assert answered == expected


@pytest.mark.parametrize(
    "vendor, reader, body",
    [
        pytest.param("linear", (request_body, "decode"), _TYPENAME, id="linear"),
        pytest.param("fireflies", (request_body, "decode"), _TYPENAME, id="fireflies"),
        pytest.param("linear", (v8_json, "error"), b"{", id="linear, V8's message"),
    ],
)
def test_a_graphql_vendor_reads_the_body_in_a_worker_thread(
    client, monkeypatch, vendor, reader, body
):
    """The router hands the body to its reader through ``run_in_threadpool``, so the reader runs
    where no event loop does and the loop serves other requests while it reads."""
    module, name = reader
    where = []
    original = getattr(module, name)

    def recording(*args):
        try:
            asyncio.get_running_loop()
            where.append("the event loop")
        except RuntimeError:
            where.append("a worker thread")
        return original(*args)

    monkeypatch.setattr(module, name, recording)
    client.post(f"/{vendor}/graphql", content=body, headers={"Content-Type": "application/json"})
    assert where == ["a worker thread"]


# Half a surrogate pair where a message quotes the client's text without escaping it: the `\u`
# escape sent, the text the message names, and the bytes that text is on the wire. A pair is the
# control, the one character it makes.
_ISSUE_FILTER = "query($f: IssueFilter) { issues(filter: $f) { nodes { id } } }"
_TWO_OPERATIONS = "query A { __typename } query B { __typename }"
_HIGH, _LOW = chr(0xD83D), chr(0xDC00)
_LONE_SURROGATES = [
    pytest.param("linear", "key", "\\ud83d", _HIGH, b"\\ud83d", id="Linear, a key, the high half"),
    pytest.param(
        "linear",
        "key",
        "\\uD83D",
        _HIGH,
        b"\\ud83d",
        id="Linear, a key, the high half in upper case",
    ),
    pytest.param("linear", "key", "\\udc00", _LOW, b"\\udc00", id="Linear, a key, the low half"),
    pytest.param(
        "linear",
        "key",
        "\\udc00\\ud83d",
        _LOW + _HIGH,
        b"\\udc00\\ud83d",
        id="Linear, a key, low then high",
    ),
    pytest.param(
        "linear", "operationName", "\\ud83d", _HIGH, b"\\ud83d", id="Linear, an operationName"
    ),
    pytest.param(
        "fireflies", "operationName", "\\ud83d", _HIGH, b"\\ud83d", id="Fireflies, an operationName"
    ),
    pytest.param(
        "fireflies",
        "operationName",
        "\\uDC00",
        _LOW,
        b"\\udc00",
        id="Fireflies, the low half in upper case",
    ),
    pytest.param(
        "linear",
        "key",
        "\\ud83d" + "\\ude00",
        chr(0x1F600),
        chr(0x1F600).encode(),
        id="Linear, a key, a pair",
    ),
]


@pytest.mark.parametrize("vendor, where, sent, named, wire", _LONE_SURROGATES)
def test_a_graphql_vendor_writes_a_lone_surrogate_as_an_escape(
    client, tokens_yaml, vendor, where, sent, named, wire
):
    """The answer is the request error, its message naming the text and its bytes carrying it as
    `backlot.graphql.body.node_json` writes it."""
    admin = tokens_yaml["admin_token"]
    if where == "key":
        body = '{"query":"%s","variables":{"f":{"%s":1}}}' % (_ISSUE_FILTER, sent)
        quoted, member = f'"{named}" is not defined by type', "message"
    elif vendor == "linear":
        body = '{"query":"%s","operationName":"%s"}' % (_TWO_OPERATIONS, sent)
        quoted, member = f"The operation '{named}' does not", "userPresentableMessage"
    else:
        body = '{"query":"%s","operationName":"%s"}' % (_TWO_OPERATIONS, sent)
        quoted, member = f'operation named "{named}"', "message"
    credential = admin if vendor == "linear" else f"Bearer {admin}"
    r = client.post(
        f"/{vendor}/graphql",
        content=body.encode(),
        headers={"Authorization": credential, "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    error = r.json()["errors"][0]
    assert quoted in error.get("extensions", {}).get(member, error.get(member))
    assert wire in r.content


# The middlewares of `backlot.main` a request runs, outermost first: its own vendor's, and none of
# another's. The Drive `HEAD` and the Slack `POST` each have a row beside their vendor's `GET`, for
# the middlewares only that method runs.
_ADMIN = "Bearer {admin}"
_RUNS = [
    pytest.param("GET", "/slack/api/auth.test", {}, [], id="slack"),
    pytest.param(
        "POST",
        "/slack/api/auth.test",
        {"Content-Type": "application/x-www-form-urlencoded"},
        ["warn_slack_post_charset", "parse_slack_form"],
        id="slack POST",
    ),
    pytest.param(
        "GET",
        "/notion/v1/users/me",
        {"Notion-Version": "2022-06-28"},
        [
            "vendor_json_media_type",
            "report_notion_etag",
            "read_a_notion_body_as_express_does",
            "report_notion_headers",
        ],
        id="notion",
    ),
    pytest.param(
        "GET",
        "/github/rate_limit",
        {},
        [
            "vendor_json_media_type",
            "put_a_request_id_on_github_answers",
            "report_github_rate_limit",
            "echo_github_api_version",
        ],
        id="github",
    ),
    pytest.param(
        "GET",
        "/atlassian/rest/api/3/serverInfo",
        {},
        [
            "vendor_json_media_type",
            "report_atlassian_headers",
            "normalise_the_slashes_in_an_atlassian_path",
        ],
        id="jira",
    ),
    pytest.param("GET", "/drive/v3/about", {}, [], id="drive"),
    pytest.param(
        "HEAD", "/drive/v3/about", {}, ["answer_head_as_the_get_without_its_body"], id="drive HEAD"
    ),
    pytest.param("GET", "/s3/", {}, ["answer_s3_with_request_ids"], id="s3"),
    pytest.param("GET", "/hubspot/crm/v3/objects/contacts", {}, [], id="hubspot"),
    pytest.param("POST", "/linear/graphql", {}, [], id="linear"),
    pytest.param(
        "GET", "/msgraph/v1.0/me", {}, ["read_a_graph_request_the_way_graph_does"], id="teams"
    ),
    pytest.param("GET", "/health", {}, [], id="health"),
]


@pytest.mark.parametrize("method, path, headers, ran", _RUNS)
def test_a_request_runs_its_own_vendors_middlewares_and_no_others(
    client, tokens_yaml, monkeypatch, method, path, headers, ran
):
    # the class the client's app was built with: `tests._helpers.client_for` reloads
    # `backlot.main`, which defines `_Layer` again for each app
    layer = next(m.cls for m in client.app.user_middleware if m.cls.__name__ == "_Layer")
    seen = []
    call = layer.__call__

    async def watched(self, scope, receive, send):
        if scope["type"] == "http" and self.runs_on(scope):
            seen.append(self.layer.dispatch_func.__name__)
        await call(self, scope, receive, send)

    monkeypatch.setattr(layer, "__call__", watched)
    admin = {"Authorization": _ADMIN.format(admin=tokens_yaml["admin_token"])}
    client.request(method, path, headers={**admin, **headers})
    assert seen == ran


@pytest.mark.parametrize(
    "path, host",
    [
        ("/github/x", b"testserver"),
        ("/github/x%3Fy/", b"testserver"),
        ("/github/x%23y/", b"testserver"),
        ("/git%09hub/x", b"testserver"),
        ("/notion%0D", b"testserver"),
        ("/s3%0A/b", b"testserver"),
        ("/github/x", b"[::1]:8000"),
        ("/github/x", b"[0:0]"),
    ],
)
def test_url_path_is_the_path_request_url_reads(path, host):
    from starlette.requests import Request

    from backlot import main

    raw = path.encode()
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": urllib.parse.unquote(path),
        "raw_path": raw,
        "query_string": b"q=1",
        "headers": [(b"host", host)],
    }

    def read(path_of):
        try:
            return path_of(scope)
        except ValueError as error:  # `[0:0]`, which `urlsplit` refuses
            return type(error)

    assert read(main._url_path) == read(lambda scope: Request(scope).url.path)
