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


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/github/search/issues", {"q", "page", "per_page"}),
        ("/slack/api/search.messages", {"query", "count", "page"}),
        ("/slack/api/conversations.history", {"channel", "limit", "cursor"}),
        # `user_id` is the path param, asserted here so enrichment cannot drop it
        ("/gmail/v1/users/{user_id}/messages", {"q", "maxResults", "pageToken", "user_id"}),
        ("/drive/v3/files", {"q", "pageSize", "pageToken", "fields"}),
        ("/notion/v1/users", {"start_cursor", "page_size"}),
        ("/atlassian/rest/api/3/search/jql", {"jql", "maxResults", "nextPageToken"}),
        ("/atlassian/wiki/rest/api/search", {"cql"}),
    ],
)
def test_router_advertises_the_params_it_honours(client, path, expected):
    op = client.get("/openapi.json").json()["paths"][path]["get"]
    assert expected <= {p["name"] for p in op.get("parameters", [])}


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
@pytest.mark.parametrize("number, quoted", _NUMBERS)
def test_a_graphql_vendor_quotes_a_number_as_javascript_prints_it(
    client, tokens_yaml, vendor, document, number, quoted
):
    """The variable's value is read as the double ``JSON.parse`` reads and quoted as JavaScript
    prints it, at both places graphql-js quotes it."""
    admin = tokens_yaml["admin_token"]
    credential = admin if vendor == "linear" else f"Bearer {admin}"
    body = '{"query":"%s","variables":{"x":%s}}' % (document, number)
    r = client.post(
        f"/{vendor}/graphql", content=body.encode(), headers={"Authorization": credential}
    )
    assert r.status_code == 400
    message = r.json()["errors"][0]["message"]
    assert f" got invalid value {quoted}; Int cannot represent " in message
    assert message.endswith(f" value: {quoted}")


# A request to Linear or Fireflies with `Authorization` sent more than once, `{key}` standing for
# the vendor's own spelling of the admin token, each answered with the 400 page
# `backlot.main.refuse_a_repeated_authorization_on_linear_and_fireflies` records. The last three
# rows are the control: one header reaches the vendor, and a path that only starts with the
# vendor's name is not the vendor's.
_TYPENAME = b'{"query":"{ __typename }"}'
_CLOUDFLARE = 400
_REPEATED = [
    pytest.param("POST", "/graphql", _TYPENAME, ("{key}", "{key}"), _CLOUDFLARE, id="two keys"),
    pytest.param("POST", "/graphql", _TYPENAME, ("{key}", "nope"), _CLOUDFLARE, id="a key, nope"),
    pytest.param("POST", "/graphql", _TYPENAME, ("nope", "{key}"), _CLOUDFLARE, id="nope, a key"),
    pytest.param("POST", "/graphql", _TYPENAME, ("{key}", ""), _CLOUDFLARE, id="a key, empty"),
    pytest.param("POST", "/graphql", _TYPENAME, ("", ""), _CLOUDFLARE, id="two empty"),
    pytest.param("POST", "/graphql", _TYPENAME, ("{key}",) * 3, _CLOUDFLARE, id="three keys"),
    pytest.param("POST", "/graphql", b"", ("{key}", "nope"), _CLOUDFLARE, id="no bytes"),
    pytest.param("POST", "/graphql", b"{", ("{key}", "nope"), _CLOUDFLARE, id="{"),
    pytest.param("POST", "/graphql", None, ("{key}", "nope"), _CLOUDFLARE, id="past the limit"),
    pytest.param("GET", "/graphql", b"", ("{key}", "nope"), _CLOUDFLARE, id="GET"),
    pytest.param("HEAD", "/graphql", b"", ("{key}", "nope"), _CLOUDFLARE, id="HEAD"),
    pytest.param("POST", "/nosuch", _TYPENAME, ("{key}", "nope"), _CLOUDFLARE, id="another path"),
    pytest.param("POST", "", _TYPENAME, ("{key}", "nope"), _CLOUDFLARE, id="the root"),
    pytest.param("POST", "/graphql", _TYPENAME, ("{key}",), 200, id="one key"),
    pytest.param("POST", "x/graphql", _TYPENAME, ("{key}", "nope"), 404, id="a longer name"),
]


@pytest.mark.parametrize("vendor", ["linear", "fireflies"])
@pytest.mark.parametrize("method, path, body, values, status", _REPEATED)
def test_cloudflare_refuses_a_repeated_authorization(
    client, tokens_yaml, vendor, method, path, body, values, status
):
    admin = tokens_yaml["admin_token"]
    key = admin if vendor == "linear" else f"Bearer {admin}"
    if body is None:
        body = padded_query({"linear": 10 * 2**20, "fireflies": 2**20}[vendor] + 1)
    headers = [("Content-Type", "application/json")]
    headers += [("Authorization", value.format(key=key)) for value in values]
    r = client.request(method, f"/{vendor}{path}", content=body, headers=headers)
    assert r.status_code == status
    page = (
        b"<html>\r\n<head><title>400 Bad Request</title></head>\r\n<body>\r\n"
        b"<center><h1>400 Bad Request</h1></center>\r\n<hr><center>cloudflare</center>\r\n"
        b"</body>\r\n</html>\r\n"
    )
    if status != _CLOUDFLARE:
        assert page not in r.content
        assert "connection" not in r.headers
        if status == 200:
            assert r.json() == {"data": {"__typename": "Query"}}
        return
    assert (r.headers["content-type"], r.headers["content-length"]) == ("text/html", "155")
    assert r.headers["connection"] == "close"
    if method != "HEAD":
        assert r.content == page


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


# Half a surrogate pair where graphql-core quotes the client's text without escaping it: the `\u`
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
        quoted = f"'{named}' is not defined by type"
    else:
        body = '{"query":"%s","operationName":"%s"}' % (_TWO_OPERATIONS, sent)
        quoted = f"operation named '{named}'"
    credential = admin if vendor == "linear" else f"Bearer {admin}"
    r = client.post(
        f"/{vendor}/graphql",
        content=body.encode(),
        headers={"Authorization": credential, "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert quoted in r.json()["errors"][0]["message"]
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
        ["vendor_json_media_type", "read_a_notion_body_as_express_does"],
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
