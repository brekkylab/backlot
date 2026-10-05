"""Notion's REST surface: pages, blocks, databases, data sources and search.

One file per router, so a source's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import re

import pytest

from backlot import store, synth
from backlot.routers.notion import DATA_SOURCES_VERSION, PUBLISHED_VERSIONS
from backlot.routers.notion import router as notion_router
from tests._helpers import served_id, tiny_corpus, tok


@pytest.fixture
def notion_h(admin_h):
    """`admin_h` plus a `Notion-Version`, which every route requires the way the real API requires
    it on every request. Tests that are about the header itself build their own headers instead."""
    return {**admin_h, "Notion-Version": DATA_SOURCES_VERSION}


def _notion_routes() -> list[tuple[str, str]]:
    """Every (method, URL) this router serves, with a served id in each path placeholder -- so a
    route added later is swept by the tests below without being listed anywhere by hand.

    Read off the router rather than off ``/openapi.json``, so a route that keeps itself out of the
    schema is swept too: ``include_in_schema=False`` is a thing this repo does (the GraphQL
    sources register their POST that way), and a route invisible to the spec is exactly the one
    that would be left behind by a rule everything else follows."""
    pid = synth.notion_id("nt-runbook")
    return [
        (method, re.sub(r"\{[^}]+\}", pid, route.path))
        for route in notion_router.routes
        for method in sorted(route.methods - {"HEAD", "OPTIONS"})
    ]


# What a Notion refusal carries, measured against api.notion.com on 2026-09-22 with no valid
# credential — which is enough, because that API checks the URL and then the credential before
# anything else.

_NOT_A_BEARER = 'Authorization header must use the format "Bearer <token>".'

_CREDENTIAL_ROWS = [
    # label, the `Authorization` sent, and which of the two 401 messages it gets
    ("no header", None, _NOT_A_BEARER),
    ("no scheme", "nope", _NOT_A_BEARER),
    ("basic", "Basic YTpi", _NOT_A_BEARER),
    ("bearer with no token", "Bearer", _NOT_A_BEARER),
    ("github's legacy scheme", "token {token}", _NOT_A_BEARER),
    ("bearer with two words", "Bearer a b", _NOT_A_BEARER),
    ("Bearer", "Bearer nope", "API token is invalid."),
    ("bearer", "bearer nope", "API token is invalid."),
    ("BEARER", "BEARER nope", "API token is invalid."),
    ("two spaces", "Bearer  nope", "API token is invalid."),
    ("a tab", "Bearer" + chr(9) + "nope", "API token is invalid."),
]


@pytest.mark.parametrize(
    "label, header, message", _CREDENTIAL_ROWS, ids=[r[0] for r in _CREDENTIAL_ROWS]
)
def test_notion_a_credential_it_cannot_use_names_the_format_or_the_token(
    client, admin_h, label, header, message
):
    """Measured: real refuses a header that is not `Bearer <token>` by naming the format, where a
    bearer whose token does not resolve is `API token is invalid.`. The legacy `token <t>` scheme
    GitHub takes is refused here too, and so is a bearer with a second word after its token; the
    scheme is read without its case, and two spaces or a tab between the words count as one."""
    headers = {"Notion-Version": DATA_SOURCES_VERSION}
    if header is not None:
        headers["Authorization"] = header.format(token=admin_h["Authorization"].split()[1])
    r = client.get("/notion/v1/users/me", headers=headers)
    assert r.status_code == 401
    assert r.json()["message"] == message, label


def test_notion_every_refusal_names_itself_by_a_request_id(client, notion_h):
    """Measured: the body carries `request_id` and the response `x-notion-request-id`, one value.
    Three refusal kinds here: the credential's 401, the version's 400 and a page's 404."""
    import uuid

    rows = [
        ("/notion/v1/users/me", {"Notion-Version": DATA_SOURCES_VERSION}),
        ("/notion/v1/users/me", {"Authorization": notion_h["Authorization"]}),
        ("/notion/v1/pages/00000000000000000000000000000000", notion_h),
    ]
    for path, headers in rows:
        r = client.get(path, headers=headers)
        assert r.status_code >= 400, path
        body = r.json()
        assert body["request_id"] == r.headers["x-notion-request-id"], path
        assert uuid.UUID(body["request_id"]).version == 4, path


def test_notion_the_same_request_gets_the_same_id_and_another_a_different_one(client, notion_h):
    """Pins the divergence ``backlot.routers.notion._request_id`` states: one request answers one
    id, and another request a different one."""
    first = client.get("/notion/v1/users/me", headers={"Notion-Version": DATA_SOURCES_VERSION})
    again = client.get("/notion/v1/users/me", headers={"Notion-Version": DATA_SOURCES_VERSION})
    other = client.get("/notion/v1/nonexistent_thing/xyz", headers=notion_h)
    assert first.json()["request_id"] == again.json()["request_id"]
    assert first.json()["request_id"] != other.json()["request_id"]


_ID = "5c6a2821-6bb1-4a7e-b6e1-c50111515c3d"

# URLs Notion does not publish: paths it has no operation on, and paths it publishes only under
# another method
_UNPUBLISHED_ROWS = [
    ("GET", "/notion/v1/nonexistent_thing/xyz"),
    ("GET", "/notion/v1/pages"),
    ("GET", "/notion/foo"),
    ("PUT", f"/notion/v1/pages/{_ID}"),
    ("PATCH", "/notion/v1/pages/"),
    ("DELETE", f"/notion/v1/blocks/{_ID}//"),
    ("GET", "/notion/v1/oauth/token"),
    ("POST", "/notion/v1/oauth/other"),
]


@pytest.mark.parametrize("method, path", _UNPUBLISHED_ROWS)
def test_notion_a_url_notion_does_not_publish_is_the_url_400(client, notion_h, method, path):
    """Measured: a URL Notion does not publish is 400 `invalid_request_url`, and it answers that
    before it reads the credential or the version — a path outside `/v1` included."""
    for headers in ({}, {"Authorization": "Bearer nope"}, notion_h):
        r = client.request(method, path, headers=headers)
        assert r.status_code == 400, (path, headers)
        assert r.json()["code"] == "invalid_request_url"
        assert r.json()["message"] == "Invalid request URL."


# (method, path) for operations Notion publishes and no route here serves
_PUBLISHED_NOT_SERVED_ROWS = [
    ("PATCH", f"/notion/v1/pages/{_ID}"),
    ("POST", "/notion/v1/pages"),
    ("POST", "/notion/v1/comments"),
    ("GET", f"/notion/v1/comments/{_ID}"),
    ("GET", "/notion/v1/file_uploads"),
    ("GET", f"/notion/v1/pages/{_ID}/properties/title"),
    ("DELETE", f"/notion/v1/blocks/{_ID}"),
    ("DELETE", f"/notion/v1/views/{_ID}/queries/{_ID}"),
    ("PATCH", "/notion/v1/pages/abc"),
    ("PATCH", f"/notion/v1/pages/{_ID}/"),
]


@pytest.mark.parametrize("method, path", _PUBLISHED_NOT_SERVED_ROWS)
def test_notion_an_operation_notion_publishes_answers_the_credential(
    client, notion_h, method, path
):
    """Measured: an operation Notion publishes answers the credential's two 401s whether or not
    Backlot serves it, with `abc` in an id's place and with one trailing slash, where the same
    path under a method Notion does not publish for it is the URL's 400 (the rows above).
    A token and a version that clear both get the URL's 400 here, as Backlot has no operation to
    run: the gap the baseline's `missing_operation` row acknowledges."""
    body = {} if method in ("POST", "PATCH") else None
    bare = client.request(method, path, json=body)
    assert bare.status_code == 401 and bare.json()["message"] == _NOT_A_BEARER, path
    wrong = client.request(method, path, json=body, headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401 and wrong.json()["message"] == "API token is invalid.", path
    cleared = client.request(method, path, json=body, headers=notion_h)
    assert cleared.status_code == 400 and cleared.json()["code"] == "invalid_request_url", path


@pytest.mark.parametrize("operation", ["token", "introspect", "revoke"])
def test_notion_an_oauth_client_endpoint_refuses_as_a_client(client, notion_h, operation):
    """Measured: the three endpoints Notion puts behind Basic auth refuse a request as an unknown
    client, `{"error":"invalid_client","request_id":…}` at 401 with `WWW-Authenticate`, rather than
    in the envelope the bearer routes use — with no header, a bearer and a Basic credential alike.
    The admin token is refused the same way, as Backlot registers no OAuth client."""
    basic = {"Authorization": "Basic YTpi"}
    for headers in ({}, {"Authorization": "Bearer nope"}, basic, notion_h):
        r = client.post(f"/notion/v1/oauth/{operation}", json={}, headers=headers)
        assert r.status_code == 401, headers
        request_id = r.headers["x-notion-request-id"]
        assert r.json() == {"error": "invalid_client", "request_id": request_id}, headers
        assert r.headers["www-authenticate"] == 'Basic realm="OAuth"', headers


_WRONG_METHOD_ROWS = [
    ("GET", "/notion/v1/search", 400),
    ("GET", "/notion/v1/pages", 400),
    ("DELETE", "/notion/v1/users/me", 400),
    ("PUT", "/notion/v1/users/me", 400),
    ("PATCH", "/notion/v1/users/me", 400),
    ("OPTIONS", "/notion/v1/users/me", 400),
    ("TRACE", "/notion/v1/users/me", 405),
    ("TRACE", "/notion/v1/nope", 405),
]


@pytest.mark.parametrize("method, path, status", _WRONG_METHOD_ROWS)
def test_notion_a_method_a_route_does_not_answer(client, notion_h, method, path, status):
    """Measured: the method is part of the URL Notion checks. `GET` on the two POST routes and
    `DELETE`, `PUT`, `PATCH` and `OPTIONS` on a GET route are each `invalid_request_url` at 400,
    not a 405 — so the catch-all takes those methods rather than the ones the routes declare.
    `TRACE` is refused with Cloudflare's own `405 Not Allowed` page before the API reads the URL,
    so the catch-all leaves it off and it is the framework's 405."""
    r = client.request(method, path, headers=notion_h)
    assert r.status_code == status, (method, path)
    if status == 400:
        assert r.json()["code"] == "invalid_request_url"


def test_notion_one_trailing_slash_is_the_path_without_it_and_two_is_not(client, notion_h):
    """Measured: `GET /v1/users/me/` and `POST /v1/search/` answer what the slash-free spelling
    answers, where `GET /v1/users/me//` is the URL 400 — exactly one slash is dropped."""
    plain = client.get("/notion/v1/users/me", headers=notion_h)
    slashed = client.get("/notion/v1/users/me/", headers=notion_h, follow_redirects=False)
    assert slashed.status_code == 200 and slashed.json() == plain.json()
    searched = client.post("/notion/v1/search/", json={}, headers=notion_h, follow_redirects=False)
    assert searched.status_code == 200
    assert searched.json() == client.post("/notion/v1/search", json={}, headers=notion_h).json()
    twice = client.get("/notion/v1/users/me//", headers=notion_h, follow_redirects=False)
    assert twice.status_code == 400 and twice.json()["code"] == "invalid_request_url"
    root = client.get("/notion/", headers=notion_h, follow_redirects=False)
    assert root.status_code == 400 and root.json()["code"] == "invalid_request_url"
    bare = client.get("/notion", headers=notion_h, follow_redirects=False)
    assert bare.status_code == 307 and bare.headers["location"].endswith("/notion/")


def test_notion_a_slashed_path_is_acl_scoped_like_the_path_without_it(client, admin_h, tokens):
    """The slash rewrite reaches the corpus routes, so the spelling it produces has to be scoped
    the way the spelling it replaces is: a page a user token cannot see is that token's 404 with
    the slash as without it, and the page it can see is the same object either way."""
    version = {"Notion-Version": DATA_SOURCES_VERSION}
    admin = {**admin_h, **version}
    user_token = sorted(tokens.values())[0]
    user = {"Authorization": f"Bearer {user_token}", **version}
    everything = {
        r["id"] for r in client.post("/notion/v1/search", json={}, headers=admin).json()["results"]
    }
    theirs = {
        r["id"] for r in client.post("/notion/v1/search", json={}, headers=user).json()["results"]
    }
    hidden = sorted(everything - theirs)
    assert hidden and theirs, (len(everything), len(theirs))
    for page in (hidden[0], sorted(theirs)[0]):
        plain = client.get(f"/notion/v1/pages/{page}", headers=user)
        slashed = client.get(f"/notion/v1/pages/{page}/", headers=user, follow_redirects=False)
        assert slashed.status_code == plain.status_code, page
        assert slashed.json() == plain.json(), page
    assert client.get(f"/notion/v1/pages/{hidden[0]}/", headers=user).status_code == 404
    assert client.get(f"/notion/v1/pages/{hidden[0]}/", headers=admin).status_code == 200


def test_notion_a_head_is_the_get_without_its_body(client, notion_h):
    """Measured with `curl -I` beside each `GET` the same minute: `HEAD /v1/users/me` is the
    credential's 401 and `HEAD /v1/nonexistent_thing/xyz` the URL's 400, each carrying the GET
    body's own `content-length` and `content-type` (178 and 145 bytes) and nothing in the body;
    `HEAD /v1/comments/{id}`, an operation Backlot does not serve, was the credential's 401 at 178
    on 2026-09-28."""
    from starlette.testclient import TestClient

    rows = [
        ("/notion/v1/users/me", {"Notion-Version": DATA_SOURCES_VERSION}),
        ("/notion/v1/nonexistent_thing/xyz", notion_h),
        ("/notion/v1/users/me", notion_h),
        (f"/notion/v1/comments/{_ID}", {}),
        ("/notion/v1/users/me/", notion_h),
    ]
    sent = []

    async def recording(scope, receive, send):
        async def record(message):
            sent.append(message)
            await send(message)

        await client.app(scope, receive, record)

    statuses = []
    for path, headers in rows:
        got = client.get(path.rstrip("/"), headers=headers)
        head = client.head(path, headers=headers, follow_redirects=False)
        assert head.status_code == got.status_code, path
        assert head.headers["content-length"] == str(len(got.content)), path
        assert head.headers["content-type"] == got.headers["content-type"], path
        statuses.append(head.status_code)
        # The test client drops a HEAD body itself, so what the app sends is read at the ASGI
        # layer. No `with`: a second lifespan would overwrite the state `client` started.
        sent.clear()
        TestClient(recording).head(path, headers=headers, follow_redirects=False)
        bodies = [m.get("body", b"") for m in sent if m["type"] == "http.response.body"]
        assert b"".join(bodies) == b"", path
    assert statuses == [401, 400, 200, 401, 200]


def test_notion_page_retrieve_and_blocks(client, notion_h):
    pid = synth.notion_id("nt-runbook")
    r = client.get(f"/notion/v1/pages/{pid}", headers=notion_h)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "page" and body["id"] == pid
    assert body["properties"]["title"]["title"][0]["plain_text"] == "Notion On-call Runbook"
    assert body["icon"] == {"type": "emoji", "emoji": "📟"}
    ch = client.get(f"/notion/v1/blocks/{pid}/children", headers=notion_h).json()
    text = synth.notion_blocks_to_text(ch["results"])
    assert text == "# On-call\n\nCheck dashboards, roll back, page on-call."


def test_notion_dashless_id_resolves(client, notion_h):
    """Notion accepts a page id dashed, dashless, or in any case -- and all three spellings must
    resolve to the SAME document, not merely 200: a normalization bug in `_norm` could still land
    on a different (or no) row while a bare status-code check stayed green. Mutation-verified: see
    the report -- both the dash-reinsertion and the case-fold in `routers.notion._norm` were
    individually broken and each turned one of these spellings' response into a 404, then
    restored."""
    dashed = synth.notion_id("nt-runbook")
    dashless = dashed.replace("-", "")
    for spelling in (dashless, dashless.upper(), dashed.upper()):
        r = client.get(f"/notion/v1/pages/{spelling}", headers=notion_h)
        assert r.status_code == 200 and r.json()["id"] == dashed


def test_notion_search_and_comments(client, notion_h):
    s = client.post("/notion/v1/search", json={"query": "on-call"}, headers=notion_h).json()
    assert any(r["id"] == synth.notion_id("nt-runbook") for r in s["results"])
    c = client.get(
        "/notion/v1/comments", params={"block_id": synth.notion_id("nt-runbook")}, headers=notion_h
    ).json()
    assert c["results"][0]["rich_text"][0]["plain_text"] == "add rate-limiter step"
    assert c["results"][0]["object"] == "comment"


_RUNBOOK = synth.notion_id("nt-runbook")
_SECRET = synth.notion_id("nt-secret")
_ZERO = "00000000-0000-0000-0000-000000000000"
_NOT_A_UUID = "query failed validation: query.block_id should be a valid uuid, instead was "

# label, who asks, the query string, and what comes back: the status, then a refusal's code and
# message, or the text of each comment in a list
_COMMENTS_BLOCK_ID_ROWS = [
    (
        "absent",
        "admin",
        "",
        400,
        "validation_error",
        "query failed validation: query.block_id should be defined, instead was `undefined`.",
    ),
    (
        "empty",
        "admin",
        "?block_id=",
        400,
        "validation_error",
        "query failed validation: query.block_id should be a string, instead was `0`.",
    ),
    ("not a uuid", "admin", "?block_id=nope", 400, "validation_error", _NOT_A_UUID + '`"nope"`.'),
    (
        "braced",
        "admin",
        f"?block_id=%7B{_ZERO}%7D",
        400,
        "validation_error",
        _NOT_A_UUID + f'`"{{{_ZERO}}}"`.',
    ),
    ("a quote", "admin", "?block_id=a%22b", 400, "validation_error", _NOT_A_UUID + '`"a\\"b"`.'),
    (
        "no such block",
        "admin",
        f"?block_id={_ZERO}",
        404,
        "object_not_found",
        f"Could not find block with ID: {_ZERO}.",
    ),
    (
        "a database",
        "admin",
        f"?block_id={synth.notion_id('nt-tasks-db')}",
        403,
        "restricted_resource",
        "This object is managed by Notion and isn’t accessible via MCP",
    ),
    ("a page", "admin", f"?block_id={_RUNBOOK}", 200, None, ["add rate-limiter step"]),
    ("a block", "admin", f"?block_id={synth.notion_block_id(_RUNBOOK, 0)}", 200, None, []),
    (
        "a hidden page's block",
        "ava@acme.com",
        f"?block_id={synth.notion_block_id(_SECRET, 0)}",
        404,
        "object_not_found",
        f"Could not find block with ID: {synth.notion_block_id(_SECRET, 0)}.",
    ),
    (
        "the same block, seen",
        "hana@acme.com",
        f"?block_id={synth.notion_block_id(_SECRET, 0)}",
        200,
        None,
        [],
    ),
]


@pytest.mark.parametrize(
    "label, who, query, status, code, expected",
    _COMMENTS_BLOCK_ID_ROWS,
    ids=[r[0] for r in _COMMENTS_BLOCK_ID_ROWS],
)
def test_notion_comments_answers_a_block_id_the_way_real_does(
    client, notion_h, tokens, label, who, query, status, code, expected
):
    """Measured against api.notion.com on 2026-10-01 and 2026-10-02 with `Notion-Version:
    2025-09-03`: a `block_id` that is absent, empty or not a uuid is 400 `validation_error`, with
    the value quoted as JSON; a uuid that names nothing is 404 `object_not_found`; a database's id
    is 403 `restricted_resource`; and a page's or a block's id is the comment list, empty for a
    block. A block is found only on a page the token can see, so `nt-secret`'s splits the way
    that page does."""
    headers = notion_h if who == "admin" else {**notion_h, "Authorization": f"Bearer {tokens[who]}"}
    r = client.get(f"/notion/v1/comments{query}", headers=headers)
    assert r.status_code == status, r.text
    body = r.json()
    if code is None:
        assert [c["rich_text"][0]["plain_text"] for c in body["results"]] == expected
    else:
        assert (body["object"], body["status"], body["code"]) == ("error", status, code)
        assert body["message"] == expected


_NOT_A_STRING = "body failed validation: body.query should be a string or `undefined`, instead was "
_NOT_AN_OBJECT = (
    "body failed validation: body.filter should be an object or `undefined`, instead was "
)
_NO_VERSION = (
    "Notion-Version header failed validation: Notion-Version header should be defined, "
    "instead was `undefined`."
)

# label, body, whether to send Notion-Version, status, error code, message
_SEARCH_BODY_TYPE_ROWS = [
    ("neither member", {}, True, 200, None, None),
    ("string query", {"query": "on-call"}, True, 200, None, None),
    ("object filter", {"filter": {"property": "object", "value": "page"}}, True, 200, None, None),
    ("numeric query", {"query": 5}, True, 400, "validation_error", _NOT_A_STRING + "`5`."),
    ("list query", {"query": ["x"]}, True, 400, "validation_error", _NOT_A_STRING + '`["x"]`.'),
    ("null query", {"query": None}, True, 400, "validation_error", _NOT_A_STRING + "`null`."),
    (
        "object query",
        {"query": {"a": 1, "b": [1, 2]}},
        True,
        400,
        "validation_error",
        _NOT_A_STRING + '`{"a":1,"b":[1,2]}`.',
    ),
    (
        "string filter",
        {"filter": "page"},
        True,
        400,
        "validation_error",
        _NOT_AN_OBJECT + '`"page"`.',
    ),
    ("non-ascii filter", {"filter": "é"}, True, 400, "validation_error", _NOT_AN_OBJECT + '`"é"`.'),
    (
        "list filter",
        {"filter": ["page"]},
        True,
        400,
        "validation_error",
        _NOT_AN_OBJECT + '`["page"]`.',
    ),
    ("null filter", {"filter": None}, True, 400, "validation_error", _NOT_AN_OBJECT + "`null`."),
    (
        "both, filter first",
        {"filter": "page", "query": 5},
        True,
        400,
        "validation_error",
        _NOT_A_STRING + "`5`.",
    ),
    ("no version", {"query": 5}, False, 400, "missing_version", _NO_VERSION),
]


@pytest.mark.parametrize(
    "label, body, versioned, status, code, message",
    _SEARCH_BODY_TYPE_ROWS,
    ids=[row[0] for row in _SEARCH_BODY_TYPE_ROWS],
)
def test_notion_search_reads_query_and_filter_by_their_types_like_real(
    client, admin_h, notion_h, label, body, versioned, status, code, message
):
    """The rule the comment in ``search`` states: a string query, a filter object and an
    omitted member are accepted; query errors precede filter errors, and version comes first."""
    r = client.post("/notion/v1/search", json=body, headers=notion_h if versioned else admin_h)
    assert r.status_code == status, (label, r.text)
    payload = r.json()
    if code is None:
        assert payload["object"] == "list"
    else:
        assert (payload["object"], payload["status"], payload["code"]) == ("error", status, code)
        assert payload["message"] == message
        assert payload["request_id"] == r.headers["x-notion-request-id"]


def test_notion_search_refuses_wrong_types_before_acl_lookup(client, notion_h, monkeypatch):
    from backlot.routers import notion

    def unexpected_acl_lookup(*args, **kwargs):
        raise AssertionError("A refused search must not look up visible documents")

    monkeypatch.setattr(notion.auth, "visible_ids", unexpected_acl_lookup)
    response = client.post("/notion/v1/search", json={"query": 5}, headers=notion_h)
    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"


def test_notion_search_filter_database_only(client, notion_h):
    s = client.post(
        "/notion/v1/search",
        json={"query": "", "filter": {"property": "object", "value": "database"}},
        headers=notion_h,
    ).json()
    assert s["results"] and all(r["object"] == "database" for r in s["results"])
    assert any(r["id"] == synth.notion_id("nt-tasks-db") for r in s["results"])


def test_notion_users(client, notion_h):
    me = client.get("/notion/v1/users/me", headers=notion_h).json()
    assert me["object"] == "user" and me["type"] == "bot"
    lst = client.get("/notion/v1/users", headers=notion_h).json()
    assert lst["results"] and all(u["object"] == "user" for u in lst["results"])
    uid = lst["results"][0]["id"]
    assert client.get(f"/notion/v1/users/{uid}", headers=notion_h).json()["id"] == uid


def test_notion_unauth_is_401(client):
    r = client.get(f"/notion/v1/pages/{synth.notion_id('nt-runbook')}")
    assert r.status_code == 401 and r.json()["code"] == "unauthorized"
    assert r.json()["message"] == _NOT_A_BEARER


def test_notion_acl_hides_group_doc_from_outsider(client, tokens_yaml):
    pid = synth.notion_id("nt-secret")
    outsider = tok(tokens_yaml, "ava@acme.com")  # ava is engineering, not people
    v = {"Notion-Version": DATA_SOURCES_VERSION}
    r = client.get(f"/notion/v1/pages/{pid}", headers={"Authorization": f"Bearer {outsider}", **v})
    assert r.status_code == 404 and r.json()["code"] == "object_not_found"
    # the owner (hana, in people) can see it
    owner = tok(tokens_yaml, "hana@acme.com")
    assert (
        client.get(
            f"/notion/v1/pages/{pid}", headers={"Authorization": f"Bearer {owner}", **v}
        ).status_code
        == 200
    )


def test_notion_database_new_vs_legacy_shape(client, notion_h):
    did = synth.notion_id("nt-tasks-db")
    new = client.get(f"/notion/v1/databases/{did}", headers=notion_h).json()
    assert new["object"] == "database"
    assert new["data_sources"][0]["id"] == synth.notion_data_source_id("nt-tasks-db")
    assert "properties" not in new
    legacy = client.get(
        f"/notion/v1/databases/{did}", headers={**notion_h, "Notion-Version": "2022-06-28"}
    ).json()
    assert "properties" in legacy and "Status" in legacy["properties"]
    assert "data_sources" not in legacy


def test_notion_query_rows_one_path_per_version(client, notion_h):
    """Each version serves one of the two query paths and answers `invalid_request_url` on the
    other, the way real Notion does: 2025-09-03 moved row reads onto data sources and took the
    database path away, so a client cannot reach rows by whichever path it likes."""
    did = synth.notion_id("nt-tasks-db")
    dsid = synth.notion_data_source_id("nt-tasks-db")
    task = synth.notion_id("nt-task-1")
    for version, served, refused in (
        ("2025-09-03", f"data_sources/{dsid}", f"databases/{did}"),
        ("2022-06-28", f"databases/{did}", f"data_sources/{dsid}"),
    ):
        h = {**notion_h, "Notion-Version": version}
        rows = client.post(f"/notion/v1/{served}/query", json={}, headers=h)
        assert rows.status_code == 200, version
        assert any(r["id"] == task for r in rows.json()["results"]), version
        gone = client.post(f"/notion/v1/{refused}/query", json={}, headers=h)
        assert gone.status_code == 400, version
        assert gone.json() == {
            "request_id": gone.headers["x-notion-request-id"],
            "object": "error",
            "status": 400,
            "code": "invalid_request_url",
            "message": "Invalid request URL.",
        }, version


def test_notion_version_cut_is_the_date_on_both_sides_of_it(client, notion_h):
    """The cut is the 2025-09-03 release date, not an equality test against either version this
    repo's own clients send. Below it: 2022-02-22 predates data sources just as much as 2022-06-28
    does, so it reads rows through the database and sees the schema inline. Above it: 2026-03-11 is
    what Notion publishes as current -- its OpenAPI declares `Notion-Version` with
    `enum: ["2026-03-11"]`, read 2026-09-16 -- and reads them through the data source.

    Both sides, because a comparison narrowed back to equality answers 2025-09-03 and 2022-06-28
    exactly as the date cut does: only a version outside that pair separates them, and the half of
    the rule that names no version at all ("2025-09-03 and later") is the half a client on a
    version Notion has not published yet arrives on."""
    did = synth.notion_id("nt-tasks-db")
    dsid = synth.notion_data_source_id("nt-tasks-db")
    task = synth.notion_id("nt-task-1")
    for version, served, refused, carries, lacks in (
        ("2022-02-22", f"databases/{did}", f"data_sources/{dsid}", "properties", "data_sources"),
        ("2026-03-11", f"data_sources/{dsid}", f"databases/{did}", "data_sources", "properties"),
    ):
        h = {**notion_h, "Notion-Version": version}
        rows = client.post(f"/notion/v1/{served}/query", json={}, headers=h)
        assert rows.status_code == 200, version
        assert any(r["id"] == task for r in rows.json()["results"]), version
        gone = client.post(f"/notion/v1/{refused}/query", json={}, headers=h)
        assert gone.status_code == 400 and gone.json()["code"] == "invalid_request_url", version
        db = client.get(f"/notion/v1/databases/{did}", headers=h).json()
        assert carries in db and lacks not in db, version


def test_notion_every_route_requires_the_version_header(client, admin_h, notion_h):
    """Notion requires `Notion-Version` on every REST request and answers `missing_version`
    without it, so every route here refuses a header-less request rather than reading one on a
    default. Swept off the router rather than listed, so a route added later is held to it too."""
    routes = _notion_routes()
    assert routes, "expected the notion router to have routes"
    for method, url in routes:
        r = client.request(method, url, headers=admin_h, json={} if method == "POST" else None)
        assert r.status_code == 400, (method, url)
        assert r.json() == {
            "request_id": r.headers["x-notion-request-id"],
            "object": "error",
            "status": 400,
            "code": "missing_version",
            "message": "Notion-Version header failed validation: Notion-Version header should be "
            "defined, instead was `undefined`.",
        }, (method, url)
    # The refusal comes before the lookup, so it is not something a failed lookup could have
    # produced: the ids swept in above are a page's, and the same URL with a version reaches the
    # lookup and is answered by it.
    url = f"/notion/v1/data_sources/{synth.notion_id('nt-runbook')}"
    assert client.get(url, headers=admin_h).json()["code"] == "missing_version"
    assert client.get(url, headers=notion_h).json()["code"] == "object_not_found"
    # An empty header carries a value rather than no header, and real answers it by enumerating
    # what it publishes rather than with the `undefined` sentence (measured 2026-09-17). Either
    # way it is refused, which is what keeps it from sorting below 2025-09-03 and quietly picking
    # the legacy model for a caller that named nothing.
    empty = client.get(url, headers={**admin_h, "Notion-Version": ""})
    assert empty.status_code == 400 and empty.json()["code"] == "missing_version"
    assert empty.json()["message"].endswith('instead was `""`.')


def test_notion_refuses_a_version_it_does_not_publish(client, admin_h):
    """A version Notion does not publish is refused rather than sorted: `2024-01-01` would fall
    below the 2025-09-03 cut and hand a client the legacy database model for a version that never
    existed. Measured against api.notion.com on 2026-09-17 with an integration token -- `banana`,
    `2024-01-01` and `2099-01-01` are each answered 400 `missing_version` with the enumeration of
    the seven versions it publishes, and that refusal is where this list of seven comes from.
    Swept off the router, so a route added later is held to it too."""
    message = (
        "Notion-Version header failed validation: Notion-Version header should be "
        '`"2021-05-11"`, `"2021-05-13"`, `"2021-08-16"`, `"2022-02-22"`, `"2022-06-28"`, '
        '`"2025-09-03"`, or `"2026-03-11"`, instead was `"2024-01-01"`.'
    )
    for method, url in _notion_routes():
        r = client.request(
            method,
            url,
            headers={**admin_h, "Notion-Version": "2024-01-01"},
            json={} if method == "POST" else None,
        )
        assert r.status_code == 400, (method, url)
        assert r.json() == {
            "request_id": r.headers["x-notion-request-id"],
            "object": "error",
            "status": 400,
            "code": "missing_version",
            "message": message,
        }, (method, url)
    # The seven pass, so what is refused above is the value and not the check: each reads a page,
    # a route every version mounts.
    page = f"/notion/v1/pages/{synth.notion_id('nt-runbook')}"
    for version in PUBLISHED_VERSIONS:
        r = client.get(page, headers={**admin_h, "Notion-Version": version})
        assert r.status_code == 200, version


def test_notion_credential_is_checked_before_the_version(client):
    """Ordering, measured against api.notion.com on 2026-09-15: an invalid token answered 401 on
    both query paths under 2022-06-28, under 2025-09-03 and with no version header at all, so
    neither version refusal is reachable without a credential that resolves. Held on every route
    for the missing header, and on the query pair for a version the path does not serve, because
    either refusal could be moved above the 401 without another test noticing."""
    bad = {"Authorization": "Bearer nope"}
    for method, url in _notion_routes():
        r = client.request(method, url, headers=bad, json={} if method == "POST" else None)
        assert r.status_code == 401, (method, url)
        assert r.json()["code"] == "unauthorized", (method, url)
    did = synth.notion_id("nt-tasks-db")
    dsid = synth.notion_data_source_id("nt-tasks-db")
    for path, version in (
        (f"databases/{did}", "2025-09-03"),  # the path that version does not serve
        (f"data_sources/{dsid}", "2022-06-28"),
    ):
        r = client.post(
            f"/notion/v1/{path}/query", json={}, headers={**bad, "Notion-Version": version}
        )
        assert r.status_code == 401, (path, version)
        assert r.json()["code"] == "unauthorized", (path, version)


def test_notion_data_source_retrieve_is_mounted_only_above_the_cut(client, notion_h):
    """`GET /data_sources/{id}` is gated on the version the way the query pair is: a version older
    than the split mounts no data source route at all, so it answers `invalid_request_url` rather
    than the object. Measured against api.notion.com on 2026-09-17 with an id that exists and is
    not a data source: 2021-05-11 and 2022-06-28 refuse, 2025-09-03 and 2026-03-11 answer
    `object_not_found`, which is the route answering. `GET /databases/{id}` is the control -- it is
    mounted under all seven, so what is refused below the cut is this route and not the request."""
    dsid = synth.notion_data_source_id("nt-tasks-db")
    did = synth.notion_id("nt-tasks-db")
    for version in ("2025-09-03", "2026-03-11"):
        ds = client.get(
            f"/notion/v1/data_sources/{dsid}", headers={**notion_h, "Notion-Version": version}
        )
        assert ds.status_code == 200, version
        body = ds.json()
        assert body["object"] == "data_source" and "Status" in body["properties"], version
    for version in ("2021-05-11", "2022-06-28"):
        h = {**notion_h, "Notion-Version": version}
        gone = client.get(f"/notion/v1/data_sources/{dsid}", headers=h)
        assert gone.status_code == 400, version
        assert gone.json() == {
            "request_id": gone.headers["x-notion-request-id"],
            "object": "error",
            "status": 400,
            "code": "invalid_request_url",
            "message": "Invalid request URL.",
        }, version
        assert client.get(f"/notion/v1/databases/{did}", headers=h).status_code == 200, version


# --- Notion: typed response schema ---------------------------------------------------------


def test_notion_search_documents_body_param(client):
    op = client.get("/openapi.json").json()["paths"]["/notion/v1/search"]["post"]
    props = op["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "query" in props and "filter" in props


def test_notion_every_route_declares_the_version_header_it_enforces(client):
    """A route that requires the header has to declare it: the spec is the whole of what a
    generated client knows, and `backlot mcp` builds its tools off this one while its bridge sends
    no version of its own (see `tests/test_mcp.py`), so an undeclared requirement would hand an
    agent tools it could not call. Declared as the seven versions the route accepts rather than as
    a string, so a generated client is handed the set the route keeps."""
    paths = client.get("/openapi.json").json()["paths"]
    ops = [
        (method, path, op)
        for path, item in paths.items()
        if path.startswith("/notion/v1")
        for method, op in item.items()
    ]
    assert ops, "expected the notion operations in the spec"
    for method, path, op in ops:
        version = [p for p in op.get("parameters", []) if p["name"] == "Notion-Version"]
        assert len(version) == 1, (method, path)
        assert version[0]["in"] == "header" and version[0]["required"] is True, (method, path)
        assert version[0]["schema"] == {
            "type": "string",
            "enum": list(PUBLISHED_VERSIONS),
        }, (method, path)


def test_notion_query_routes_name_the_versions_they_serve(client):
    """The version is the one parameter whose value decides which of the two query routes answers,
    so each says which versions it is the path for rather than repeating the generic sentence --
    a tool description is all an agent has to pick a value from."""
    paths = client.get("/openapi.json").json()["paths"]
    for path, serves, refuses in (
        (
            "/notion/v1/databases/{database_id}/query",
            "versions up to and including 2022-06-28",
            "2025-09-03 and later",
        ),
        (
            "/notion/v1/data_sources/{data_source_id}/query",
            "2025-09-03 and later",
            "versions before it",
        ),
    ):
        params = paths[path]["post"]["parameters"]
        version = next(p for p in params if p["name"] == "Notion-Version")
        assert version["in"] == "header" and version["required"] is True, path
        assert f"serves {serves}" in version["description"], path
        assert refuses in version["description"], path


def test_notion_page_has_typed_response_schema(client):
    op = client.get("/openapi.json").json()["paths"]["/notion/v1/pages/{page_id}"]["get"]
    assert op["responses"]["200"]["content"]["application/json"]["schema"] != {}


def test_notion_responses_unchanged_by_enrichment(client, notion_h):
    res = client.post("/notion/v1/search", json={}, headers=notion_h).json()
    assert res["object"] == "list" and "results" in res
    pages = [r for r in res["results"] if r.get("object") == "page"]
    assert pages, "expected notion pages in search"
    page = client.get(f"/notion/v1/pages/{pages[0]['id']}", headers=notion_h).json()
    for k in ("object", "id", "created_time", "last_edited_time", "properties", "parent", "url"):
        assert k in page, f"notion page missing {k} (fidelity regression)"
    dbs = [r for r in res["results"] if r.get("object") == "database"]
    if dbs:  # version-dependent database shape must survive both header values
        did = dbs[0]["id"]
        legacy = client.get(
            f"/notion/v1/databases/{did}", headers={**notion_h, "Notion-Version": "2022-06-28"}
        ).json()
        default = client.get(
            f"/notion/v1/databases/{did}", headers={**notion_h, "Notion-Version": "2025-09-03"}
        ).json()
        assert "properties" in legacy and "data_sources" in default


# --- Notion ---------------------------------------------------------------------


def _notion_conn(tmp_path):
    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "notion",
                "doc_id": "nf-page",
                "teamspace": "eng",
                "title": "Runbook",
                "content": "# On-call\n\nRoll back and page.",
                "author_email": "ava@acme.com",
                "visibility": "public",
                "icon": "📟",
                "comments": [{"content": "add rate-limiter step", "author_email": "bob@acme.com"}],
            },
            {
                "source_type": "notion",
                "doc_id": "nf-db",
                "subtype": "database",
                "teamspace": "eng",
                "title": "Tasks",
                "content": "Tracker",
                "author_email": "ava@acme.com",
                "visibility": "public",
                "properties": {"Status": {"type": "select"}},
            },
            {
                "source_type": "notion",
                "doc_id": "nf-row",
                "parent": "nf-db",
                "teamspace": "eng",
                "title": "Fix bug",
                "content": "body",
                "author_email": "bob@acme.com",
                "visibility": "public",
                "properties": {"Status": "In Progress"},
            },
        ],
    )
    return store.connect_ro(s.db_path)


def test_notion_page_shape(tmp_path):
    from backlot.routers.notion import _page_obj

    conn = _notion_conn(tmp_path)
    obj = _page_obj(conn, store.get_document(conn, "notion", served_id("notion", "nf-page")))
    assert obj["object"] == "page"
    assert obj["id"] == synth.notion_id("nf-page")
    assert obj["created_by"]["object"] == "user"
    assert obj["parent"] == {"type": "workspace", "workspace": True}
    assert obj["properties"]["title"]["type"] == "title"
    assert obj["properties"]["title"]["title"][0]["plain_text"] == "Runbook"
    assert obj["icon"] == {"type": "emoji", "emoji": "📟"}
    assert obj["url"].startswith("https://www.notion.so/")
    # a database row exposes its property values + a database_id parent
    row = _page_obj(conn, store.get_document(conn, "notion", served_id("notion", "nf-row")))
    assert row["parent"]["type"] == "database_id"
    # ...and it is the database's OWN served id, which `parent_id` already holds. Hashing
    # that column again named a database nothing serves, so every row in every database
    # advertised a parent that 404s.
    assert row["parent"]["database_id"] == served_id("notion", "nf-db")
    assert row["properties"]["Status"]["select"]["name"] == "In Progress"


def test_notion_database_and_data_source_shape(tmp_path):
    from backlot.routers.notion import _data_source_obj, _database_obj

    conn = _notion_conn(tmp_path)
    dbrow = store.get_document(conn, "notion", served_id("notion", "nf-db"))
    new = _database_obj(conn, dbrow, "2025-09-03")
    assert new["object"] == "database"
    assert new["data_sources"][0]["id"] == synth.notion_data_source_id("nf-db")
    assert "properties" not in new
    legacy = _database_obj(conn, dbrow, "2022-06-28")
    assert "data_sources" not in legacy
    assert legacy["properties"]["Status"]["type"] == "select"
    ds = _data_source_obj(conn, dbrow)
    assert ds["object"] == "data_source" and ds["properties"]["title"]["type"] == "title"


def test_notion_user_and_block_shape(tmp_path):
    from backlot.routers.notion import _user_obj

    conn = _notion_conn(tmp_path)
    u = _user_obj(conn, "ava@acme.com")
    assert u["object"] == "user" and u["type"] == "person"
    assert u["person"]["email"] == "ava@acme.com"
    assert u["id"] == synth.notion_user_id("ava@acme.com")
    blocks = synth.notion_blocks("nf-page", "# On-call\n\nRoll back and page.")
    b = blocks[0]
    assert b["object"] == "block" and b["type"] == "heading_1"
    assert b["heading_1"]["rich_text"][0]["plain_text"] == "On-call"


@pytest.mark.parametrize("version", ["2022-06-28", "2025-09-03"])
def test_database_blocks_have_no_children(client, admin_h, version):
    """A database's block has no children and lists none, beside a page that has both (see
    `get_block_children`)."""
    h = {**admin_h, "Notion-Version": version}
    bid = synth.notion_id("nt-tasks-db")
    block = client.get(f"/notion/v1/blocks/{bid}", headers=h)
    assert block.status_code == 200
    assert block.json()["type"] == "child_database"
    assert block.json()["has_children"] is False
    children = client.get(f"/notion/v1/blocks/{bid}/children", headers=h)
    assert children.status_code == 200
    assert children.json()["results"] == []
    assert children.json()["next_cursor"] is None
    assert children.json()["has_more"] is False
    pid = synth.notion_id("nt-runbook")
    assert client.get(f"/notion/v1/blocks/{pid}", headers=h).json()["has_children"] is True
    assert client.get(f"/notion/v1/blocks/{pid}/children", headers=h).json()["results"]
