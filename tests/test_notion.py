"""Notion's REST surface: pages, blocks, databases, data sources and search.

One file per router, so a source's shape assertions live in one place whether they go over HTTP
or call the response builder directly.
"""

from __future__ import annotations

import gzip
import json
import re
import zlib
from urllib.parse import quote

import pytest

from backlot import store, synth
from backlot.routers.notion import DATA_SOURCES_VERSION, PUBLISHED_VERSIONS, weak_etag
from backlot.routers.notion import router as notion_router
from tests._helpers import client_for, sent_as, served_id, tiny_corpus, tok


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


def test_notion_a_credential_it_cannot_use_names_the_format_or_the_token(client, admin_h):
    """Measured: real refuses a header that is not `Bearer <token>` by naming the format, where a
    bearer whose token does not resolve is `API token is invalid.`. The legacy `token <t>` scheme
    GitHub takes is refused here too, and so is a bearer with a second word after its token; the
    scheme is read without its case, and two spaces or a tab between the words count as one."""
    answered = {}
    for label, header, _ in _CREDENTIAL_ROWS:
        headers = {"Notion-Version": DATA_SOURCES_VERSION}
        if header is not None:
            headers["Authorization"] = header.format(token=admin_h["Authorization"].split()[1])
        r = client.get("/notion/v1/users/me", headers=headers)
        body = r.json()
        answered[label] = (r.status_code, body["message"], list(body), body.get("additional_data"))

    def expected(message):
        if message == _NOT_A_BEARER:
            return (401, message, ["object", "status", "code", "message", "request_id"], None)
        members = ["object", "status", "code", "message", "additional_data", "request_id"]
        return (401, message, members, {"reason": "token_invalid"})

    assert answered == {label: expected(message) for label, _, message in _CREDENTIAL_ROWS}


def test_notion_every_answer_names_itself_by_a_request_id(client, notion_h):
    """Measured: the body carries `request_id` as its last member and the response
    `x-notion-request-id`, one value. Three refusal kinds here, the credential's 401, the version's
    400 and a page's 404, and a 200 of each kind: an object, a list and the bot."""
    import uuid

    rows = [
        ("GET", "/notion/v1/users/me", {"Notion-Version": DATA_SOURCES_VERSION}, 401),
        ("GET", "/notion/v1/users/me", {"Authorization": notion_h["Authorization"]}, 400),
        ("GET", "/notion/v1/pages/00000000000000000000000000000000", notion_h, 404),
        ("GET", f"/notion/v1/pages/{synth.notion_id('nt-runbook')}", notion_h, 200),
        ("POST", "/notion/v1/search", notion_h, 200),
        ("GET", "/notion/v1/users/me", notion_h, 200),
    ]
    for method, path, headers, status in rows:
        r = client.request(method, path, headers=headers, json={} if method == "POST" else None)
        assert r.status_code == status, path
        body = r.json()
        assert body["request_id"] == r.headers["x-notion-request-id"], path
        assert list(body)[-1] == "request_id", path
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
    ("GET", f"/notion/v1/ai/skills/{_ID}/upload"),
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
    ("POST", f"/notion/v1/ai/skills/{_ID}/upload"),
]


@pytest.mark.parametrize("method, path", _PUBLISHED_NOT_SERVED_ROWS)
def test_notion_an_operation_notion_publishes_answers_the_credential(
    client, notion_h, method, path
):
    """Measured: an operation Notion publishes answers the credential's two 401s whether or not
    Backlot serves it, with `abc` in an id's place and with one trailing slash, where the same
    path under a method Notion does not publish for it is the URL's 400 (the rows above).
    A token and a version that clear both get the URL's 400 here, as Backlot has no operation to
    run: the gap the baseline's `missing` operation acknowledges."""
    body = {} if method in ("POST", "PATCH") else None
    bare = client.request(method, path, json=body)
    assert bare.status_code == 401 and bare.json()["message"] == _NOT_A_BEARER, path
    wrong = client.request(method, path, json=body, headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401 and wrong.json()["message"] == "API token is invalid.", path
    assert wrong.json()["additional_data"] == {"reason": "token_invalid"}, path
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


def test_notion_json_carries_the_charset_and_the_headers_real_sends(client, notion_h):
    """Pins ``backlot.errors.notion.json_media_type``, ``backlot.routers.notion.weak_etag`` and
    ``backlot.routers.notion.APPLICATION_HEADERS`` on 200s and refusals, a `HEAD` among them: each
    carries the charset; each but the framework's 405 for `TRACE` the `ETag` over its body, a `HEAD`
    its `GET`'s; and each but that 405 and a refusal of the body reader
    (``backlot.routers.notion.front_door_body``) the headers, which a body that reader leaves unread
    gets too. The OpenAPI document and the app's own routes keep `application/json` and carry none
    of the headers."""
    # two answers real sent, kept whole, the second's length counted in bytes, not characters
    for body, etag in (
        (
            b'{"object":"error","status":401,"code":"unauthorized","message":"Authorization header'
            b' must use the format \\"Bearer <token>\\".","request_id":"675f0ca5-96f1-43d8-bd09-'
            b'ac2433e235e9"}',
            'W/"b2-r883nfyX+MtL2hM795u3RMqBGaA"',
        ),
        (
            '{"object":"error","status":400,"code":"validation_error","message":"body failed '
            "validation: body.query should be a string or `undefined`, instead was "
            '`[\\"é한\\"]`.","request_id":"d45596aa-6b18-4338-a9c3-32f8256e710c"}'.encode(),
            'W/"da-AeKifXGEFVtPdfWp2X1QNdbc2FE"',
        ),
    ):
        assert weak_etag(body) == etag
    utf8 = "application/json; charset=utf-8"
    real = {
        "content-security-policy": "default-src 'none'",
        "referrer-policy": "strict-origin-when-cross-origin",
        "x-content-type-options": "nosniff",
        "x-dns-prefetch-control": "off",
        "x-download-options": "noopen",
        "x-frame-options": "SAMEORIGIN",
        "x-permitted-cross-domain-policies": "none",
        "x-xss-protection": "0",
    }
    none = dict.fromkeys(real)
    # each with `etag` True for the tag over the body, None for no tag
    app, refused, bare = {**real, "etag": True}, {**none, "etag": True}, {**none, "etag": None}
    versioned = {"Notion-Version": DATA_SOURCES_VERSION}
    rows = [
        ("GET", "/notion/v1/users/me", notion_h, None, 200, app),
        ("POST", "/notion/v1/search", {**notion_h, **_JSON}, b"{}", 200, app),
        ("GET", f"/notion/v1/pages/{synth.notion_id('nt-runbook')}", notion_h, None, 200, app),
        ("GET", "/notion/v1/users/me", versioned, None, 401, app),
        ("POST", "/notion/v1/search", {**versioned, **_JSON}, b"{}", 401, app),
        (
            "GET",
            "/notion/v1/users/me",
            {"Authorization": notion_h["Authorization"]},
            None,
            400,
            app,
        ),
        ("GET", "/notion/v1/nonexistent_thing/xyz", notion_h, None, 400, app),
        ("GET", "/notion/v1/pages/00000000000000000000000000000000", notion_h, None, 404, app),
        ("POST", "/notion/v1/oauth/token", {**versioned, **_JSON}, b"{}", 401, app),
        # the framework's 405 for a method no route takes, which real's front door answers itself
        ("TRACE", "/notion/v1/users/me", notion_h, None, 405, bare),
        # the body reader's refusals, and a body it does not read
        ("POST", "/notion/v1/search", {**notion_h, **_JSON}, b"{", 400, refused),
        (
            "POST",
            "/notion/v1/search",
            {**notion_h, "Content-Type": "application/json; charset=latin1"},
            b"{}",
            415,
            refused,
        ),
        (
            "POST",
            "/notion/v1/search",
            {**notion_h, **_JSON},
            b'{"x":"' + b"a" * 600_000 + b'"}',
            413,
            refused,
        ),
        ("POST", "/notion/v1/search", {**notion_h, "Content-Type": "text/plain"}, b"{", 200, app),
    ]
    for method, path, headers, body, status, sent in rows:
        r = client.request(method, path, headers=headers, content=body)
        got = {name: r.headers.get(name) for name in real}
        got["etag"] = r.headers.get("etag") and r.headers["etag"] == weak_etag(r.content)
        assert (r.status_code, r.headers["content-type"], got) == (status, utf8, sent), (path, body)
    head = client.head("/notion/v1/users/me", headers=versioned)
    got = {name: head.headers.get(name) for name in real}
    get = client.get("/notion/v1/users/me", headers=versioned)
    got["etag"] = head.headers.get("etag") and head.headers["etag"] == weak_etag(get.content)
    assert (head.status_code, head.headers["content-type"], got) == (401, utf8, app)
    # the contract keeps the bare type, and so do the app's own routes, with none of the headers
    op = client.get("/openapi.json").json()["paths"]["/notion/v1/users/me"]["get"]
    assert list(op["responses"]["200"]["content"]) == ["application/json"]
    for path in ("/openapi.json", "/health", "/notionx/v1/users/me"):
        r = client.get(path)
        got = {name: r.headers.get(name) for name in bare}
        assert (r.headers["content-type"], got) == ("application/json", bare), path
    # the vendor root is the framework's redirect, with no JSON body, and real's 302 has no tag
    root = client.get("/notion", headers=notion_h, follow_redirects=False)
    assert (root.status_code, root.headers.get("etag")) == (307, None)


def test_notion_answers_an_if_none_match_of_a_star_as_real_does(client, notion_h):
    """Each request below is answered as ``backlot.routers.notion.not_modified`` records real
    answering it: with the 304 ``backlot.main.report_notion_etag`` makes of the answer it stands
    for, or with the status it gets without `If-None-Match`."""
    me, page = "/notion/v1/users/me", f"/notion/v1/pages/{synth.notion_id('nt-runbook')}"
    stands_for = {}
    for path in (me, page):
        headers = dict(client.get(path, headers=notion_h).headers)
        del headers["content-type"], headers["content-length"]
        stands_for[path] = (304, b"", headers)
    star = ("If-None-Match", "*")
    wanted = {
        ("GET", me, (star,)): stands_for[me],
        ("HEAD", me, (star,)): stands_for[me],
        ("GET", page, (star,)): stands_for[page],
        ("GET", me, (("If-None-Match", " \t* "),)): stands_for[me],
        ("GET", me, (star, ("If-Modified-Since", ""))): stands_for[me],
        ("GET", me, (star, ("If-Modified-Since", "  "))): stands_for[me],
        ("GET", me, (star, ("Cache-Control", "NO-CACHE"))): stands_for[me],
        ("GET", me, (star, ("Cache-Control", "max-age=0"))): stands_for[me],
        ("GET", me, (star, ("If-Modified-Since", "Fri, 01 Jan 2100 00:00:00 GMT"))): 200,
        ("GET", me, (star, ("If-Modified-Since", "\xa0"))): 200,
        ("GET", me, (star, ("Cache-Control", "max-age=0, no-cache"))): 200,
        ("GET", me, (star, ("Cache-Control", "max-age=0"), ("Cache-Control", "no-cache"))): 200,
        ("GET", me, (star, ("Cache-Control", "xno-cache"))): stands_for[me],
        ("GET", me, (star, ("Cache-Control", "\xa0no-cache"))): 200,
        ("GET", me, (star, ("Cache-Control", 'no-cache="set-cookie"'))): stands_for[me],
        ("GET", me, (("If-None-Match", '"*"'),)): 200,
        ("GET", me, (("If-None-Match", 'W/"0-x", *'),)): 200,
        ("GET", me, (("If-None-Match", "\xa0*"),)): 200,
        ("GET", me, (("If-None-Match", "*\xa0"),)): 200,
        # real's tag is new on each answer, so the one an answer carried matches no later one
        ("GET", me, (("If-None-Match", stands_for[me][2]["etag"]),)): 200,
        ("GET", "/notion/v1/pages/00000000000000000000000000000000", (star,)): 404,
        ("GET", "/notion/v1/nonexistent_thing/xyz", (star,)): 400,
        ("POST", "/notion/v1/search", (star, *_JSON.items())): 200,
    }
    answered = {}
    for method, path, sent in wanted:
        body = b"{}" if method == "POST" else None
        r = sent_as(client, method, path, [*notion_h.items(), *sent], content=body)
        answer = (304, r.content, dict(r.headers)) if r.status_code == 304 else r.status_code
        answered[method, path, sent] = answer
    assert answered == wanted


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
_NOT_A_STRING_ID = "query failed validation: query.block_id should be a string, instead was "

# label, who asks, the query string, and what comes back: the status, then a refusal's code and
# message, or the text of each comment in a list
_COMMENTS_QUERY_ROWS = [
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
    ("a number", "admin", "?block_id=5", 400, "validation_error", _NOT_A_STRING_ID + "`5`."),
    (
        "an exponent",
        "admin",
        "?block_id=1e3",
        400,
        "validation_error",
        _NOT_A_STRING_ID + "`1000`.",
    ),
    ("true", "admin", "?block_id=true", 400, "validation_error", _NOT_A_STRING_ID + "`true`."),
    ("null", "admin", "?block_id=null", 400, "validation_error", _NOT_A_UUID + '`"null"`.'),
    (
        "dashless",
        "admin",
        "?block_id=" + _RUNBOOK.replace("-", ""),
        200,
        None,
        ["add rate-limiter step"],
    ),
    (
        "dashless in upper case",
        "admin",
        "?block_id=" + _RUNBOOK.replace("-", "").upper(),
        400,
        "validation_error",
        _NOT_A_UUID + '`"' + _RUNBOOK.replace("-", "").upper() + '"`.',
    ),
    (
        "a cursor on a block's list",
        "admin",
        f"?block_id={synth.notion_block_id(_RUNBOOK, 0)}&start_cursor={synth.notion_block_id(_RUNBOOK, 0)}",
        400,
        "validation_error",
        f"The start_cursor provided is invalid: {synth.notion_block_id(_RUNBOOK, 0)}",
    ),
    (
        "54 a's, cut",
        "admin",
        "?block_id=" + "a" * 54,
        400,
        "validation_error",
        _NOT_A_UUID + '`"' + "a" * 54 + "...`.",
    ),
    (
        "twice",
        "admin",
        f"?block_id={_RUNBOOK}&block_id={_RUNBOOK}",
        400,
        "validation_error",
        'Conflicting values for "block_id" in query parameters.',
    ),
    (
        "a key it does not read",
        "admin",
        f"?block_id={_RUNBOOK}&foo=1",
        200,
        None,
        ["add rate-limiter step"],
    ),
    (
        "a key that is an object",
        "admin",
        f"?block_id={_RUNBOOK}&foo[a]=1",
        400,
        "validation_error",
        'The value of "foo" in query parameters must be a string, number, or boolean.',
    ),
    (
        "page_size 0",
        "admin",
        f"?block_id={_RUNBOOK}&page_size=0",
        400,
        "validation_error",
        "query failed validation: query.page_size should be ≥ `1` or `undefined`, instead was `0`.",
    ),
    (
        "page_size empty",
        "admin",
        f"?block_id={_RUNBOOK}&page_size=",
        400,
        "validation_error",
        "query failed validation: query.page_size should be ≥ `1` or `undefined`, instead was `0`.",
    ),
    (
        "page_size 1e3",
        "admin",
        f"?block_id={_RUNBOOK}&page_size=1e3",
        400,
        "validation_error",
        "query failed validation: query.page_size should be ≤ `100` or `undefined`, instead was "
        "`1000`.",
    ),
    (
        "page_size 5.0",
        "admin",
        f"?block_id={_RUNBOOK}&page_size=5.0",
        200,
        None,
        ["add rate-limiter step"],
    ),
    (
        "page_size 1.5",
        "admin",
        f"?block_id={_RUNBOOK}&page_size=1.5",
        400,
        "validation_error",
        'query failed validation: query.page_size should be a number or `undefined`, instead was `"1.5"`.',
    ),
    (
        "a cursor that is a number, ahead of the page size",
        "admin",
        f"?block_id={_RUNBOOK}&page_size=abc&start_cursor=5",
        400,
        "validation_error",
        "query failed validation: query.start_cursor should be a string or `undefined`, instead was "
        "`5`.",
    ),
    (
        "a cursor that is not a uuid, ahead of the page size",
        "admin",
        f"?block_id={_RUNBOOK}&page_size=abc&start_cursor=bogus",
        400,
        "validation_error",
        "query failed validation: query.start_cursor should be a valid uuid or `undefined`, instead "
        'was `"bogus"`.',
    ),
    (
        "block_id ahead of the cursor",
        "admin",
        "?block_id=5&start_cursor=5",
        400,
        "validation_error",
        _NOT_A_STRING_ID + "`5`.",
    ),
    (
        "a cursor no comment has",
        "admin",
        f"?block_id={_RUNBOOK}&start_cursor={_ZERO}",
        400,
        "validation_error",
        f"The start_cursor provided is invalid: {_ZERO}",
    ),
    (
        "a cursor on no block, after the lookup",
        "admin",
        f"?block_id={_ZERO}&start_cursor={_ZERO}",
        404,
        "object_not_found",
        f"Could not find block with ID: {_ZERO}.",
    ),
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
    _COMMENTS_QUERY_ROWS,
    ids=[r[0] for r in _COMMENTS_QUERY_ROWS],
)
def test_notion_comments_reads_its_query_string_as_real_does(
    client, notion_h, tokens, label, who, query, status, code, expected
):
    """Real's answer to each query string, read in ``notion_request.COMMENTS_QUERY``'s order: a
    uuid that names nothing is 404 `object_not_found`, a database's id 403 `restricted_resource`,
    and a page's or a block's id the comment list, empty for a block. A block is found only on a
    page the token can see, so `nt-secret`'s splits the way that page does."""
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

_NEW, _OLD = DATA_SOURCES_VERSION, "2022-06-28"
_FIX_ONE = "body failed validation. Fix one:\n"
_IN_TRASH = "body.filter.in_trash should be defined, instead was `undefined`."
_SORT_EITHER = (
    'body.sort should specify either `{ "property": "relevance" }` or '
    '`{ "timestamp": "last_edited_time", "direction": "ascending" | "descending" }`.'
)
_ASC_DESC = '`"ascending"`, `"descending"`'

# label, body, the Notion-Version sent (None for none), status, error code, message
_SEARCH_BODY_TYPE_ROWS = [
    ("neither member", {}, _NEW, 200, None, None),
    ("string query", {"query": "on-call"}, _NEW, 200, None, None),
    ("object filter", {"filter": {"property": "object", "value": "page"}}, _NEW, 200, None, None),
    ("numeric query", {"query": 5}, _NEW, 400, "validation_error", _NOT_A_STRING + "`5`."),
    ("list query", {"query": ["x"]}, _NEW, 400, "validation_error", _NOT_A_STRING + '`["x"]`.'),
    ("null query", {"query": None}, _NEW, 400, "validation_error", _NOT_A_STRING + "`null`."),
    (
        "object query",
        {"query": {"a": 1, "b": [1, 2]}},
        _NEW,
        400,
        "validation_error",
        _NOT_A_STRING + '`{"a":1,"b":[1,2]}`.',
    ),
    (
        "string filter",
        {"filter": "page"},
        _NEW,
        400,
        "validation_error",
        _NOT_AN_OBJECT + '`"page"`.',
    ),
    ("non-ascii filter", {"filter": "é"}, _NEW, 400, "validation_error", _NOT_AN_OBJECT + '`"é"`.'),
    (
        "list filter",
        {"filter": ["page"]},
        _NEW,
        400,
        "validation_error",
        _NOT_AN_OBJECT + '`["page"]`.',
    ),
    ("null filter", {"filter": None}, _NEW, 400, "validation_error", _NOT_AN_OBJECT + "`null`."),
    (
        "both, filter first",
        {"filter": "page", "query": 5},
        _NEW,
        400,
        "validation_error",
        _NOT_A_STRING + "`5`.",
    ),
    ("no version", {"query": 5}, None, 400, "missing_version", _NO_VERSION),
    # `sort`, its schema and then what it serves
    ("relevance", {"sort": {"property": "relevance"}}, _NEW, 200, None, None),
    (
        "last edited, ascending",
        {"sort": {"timestamp": "last_edited_time", "direction": "ascending"}},
        _NEW,
        200,
        None,
        None,
    ),
    (
        "sort null",
        {"sort": None},
        _NEW,
        400,
        "validation_error",
        "body failed validation: body.sort should be an object or `undefined`, instead was `null`.",
    ),
    ("sort empty", {"sort": {}}, _NEW, 400, "validation_error", _SORT_EITHER),
    (
        "relevance with a timestamp",
        {"sort": {"property": "relevance", "timestamp": "last_edited_time"}},
        _NEW,
        400,
        "validation_error",
        'body.sort with `{ "property": "relevance" }` cannot include `timestamp` or `direction`.',
    ),
    (
        "a property not relevance, with a direction",
        {"sort": {"property": "x", "direction": "ascending"}},
        _NEW,
        400,
        "validation_error",
        'body.sort.property should be "relevance", instead was `x`.',
    ),
    (
        "a property not relevance, with the timestamp",
        {"sort": {"property": "x", "timestamp": "last_edited_time"}},
        _NEW,
        400,
        "validation_error",
        'body.sort.property should be "relevance", instead was `x`.',
    ),
    (
        "another timestamp, and no direction",
        {"sort": {"timestamp": "created_time"}},
        _NEW,
        400,
        "validation_error",
        'body.sort.timestamp should be "last_edited_time" when sorting by timestamp.',
    ),
    (
        "relevance with a timestamp it does not take",
        {"sort": {"property": "relevance", "timestamp": "x"}},
        _NEW,
        400,
        "validation_error",
        'body.sort with `{ "property": "relevance" }` cannot include `timestamp` or `direction`.',
    ),
    (
        "relevance with a direction",
        {"sort": {"property": "relevance", "direction": "ascending"}},
        _NEW,
        400,
        "validation_error",
        'body.sort with `{ "property": "relevance" }` cannot include `timestamp` or `direction`.',
    ),
    (
        "relevance with a direction real has not",
        {"sort": {"property": "relevance", "direction": "up"}},
        _NEW,
        400,
        "validation_error",
        _FIX_ONE
        + "body.sort.timestamp should be defined, instead was `undefined`.\nbody.sort.direction should be not present, "
        + _ASC_DESC
        + ', or `undefined`, instead was `"up"`.',
    ),
    (
        "a property not relevance",
        {"sort": {"property": "x"}},
        _NEW,
        400,
        "validation_error",
        'body.sort.property should be "relevance", instead was `x`.',
    ),
    (
        "a timestamp alone",
        {"sort": {"timestamp": "last_edited_time"}},
        _NEW,
        400,
        "validation_error",
        'body.sort.direction should be defined when body.sort.timestamp is "last_edited_time".',
    ),
    (
        "a direction real has not",
        {"sort": {"timestamp": "last_edited_time", "direction": "up"}},
        _NEW,
        400,
        "validation_error",
        _FIX_ONE
        + "body.sort.direction should be "
        + _ASC_DESC
        + ', or `undefined`, instead was `"up"`.\nbody.sort.property should be defined, instead was `undefined`.',
    ),
    (
        "created time",
        {"sort": {"timestamp": "created_time", "direction": "ascending"}},
        _NEW,
        400,
        "validation_error",
        'body.sort.timestamp should be "last_edited_time" when sorting by timestamp.',
    ),
    (
        "a direction alone",
        {"sort": {"direction": "up"}},
        _NEW,
        400,
        "validation_error",
        _FIX_ONE
        + "body.sort.timestamp should be defined, instead was `undefined`.\nbody.sort.property should be defined, instead was `undefined`.\nbody.sort.direction should be "
        + _ASC_DESC
        + ', or `undefined`, instead was `"up"`.',
    ),
    (
        "all three, a direction real has not",
        {"sort": {"timestamp": "last_edited_time", "direction": "up", "property": "relevance"}},
        _NEW,
        400,
        "validation_error",
        _FIX_ONE
        + "body.sort.direction should be "
        + _ASC_DESC
        + ', not present, or `undefined`, instead was `"up"`.\nbody.sort.timestamp should be not present, instead was `"last_edited_time"`.',
    ),
    (
        "a timestamp that is a number",
        {"sort": {"timestamp": 1}},
        _NEW,
        400,
        "validation_error",
        _FIX_ONE
        + 'body.sort.timestamp should be `"last_edited_time"`, a string, or `undefined`, instead was `1`.\nbody.sort.property should be defined, instead was `undefined`.',
    ),
    (
        "a member sort does not take",
        {"sort": {"timestamp": "last_edited_time", "direction": "ascending", "foo": 1}},
        _NEW,
        400,
        "validation_error",
        _FIX_ONE
        + "body.sort.foo should be not present, instead was `1`.\nbody.sort.property should be defined, instead was `undefined`.",
    ),
    (
        "a created time with a member sort does not take",
        {"sort": {"timestamp": "created_time", "foo": 1}},
        _NEW,
        400,
        "validation_error",
        _FIX_ONE
        + 'body.sort.timestamp should be `"last_edited_time"`, instead was `"created_time"`.\nbody.sort.property should be defined, instead was `undefined`.\nbody.sort.foo should be not present, instead was `1`.',
    ),
    # `filter`
    ("pages", {"filter": {"property": "object", "value": "page"}}, _NEW, 200, None, None),
    ("the trash", {"filter": {"in_trash": True}}, _NEW, 200, None, None),
    (
        "an empty filter",
        {"filter": {}},
        _NEW,
        400,
        "validation_error",
        _FIX_ONE
        + "body.filter.property should be defined, instead was `undefined`."
        + "\n"
        + _IN_TRASH,
    ),
    (
        "a value alone",
        {"filter": {"value": "page"}},
        _NEW,
        400,
        "validation_error",
        _FIX_ONE
        + "body.filter.property should be defined, instead was `undefined`."
        + "\n"
        + _IN_TRASH,
    ),
    (
        "database, which 2025-09-03 does not read",
        {"filter": {"property": "object", "value": "database"}},
        _NEW,
        400,
        "validation_error",
        _FIX_ONE
        + 'body.filter.value should be `"page"` or `"data_source"`, instead was `"database"`.\n'
        + _IN_TRASH,
    ),
    (
        "database, which 2022-06-28 serves",
        {"filter": {"property": "object", "value": "database"}},
        _OLD,
        200,
        None,
        None,
    ),
    (
        "database beside the trash, quoted as sent",
        {"filter": {"in_trash": False, "value": "database"}},
        _OLD,
        400,
        "validation_error",
        _FIX_ONE
        + "body.filter.property should be defined, instead was `undefined`."
        + '\nbody.filter.value should be not present, instead was `"database"`.',
    ),
    (
        "another property",
        {"filter": {"property": "type", "value": "page"}},
        _NEW,
        400,
        "validation_error",
        _FIX_ONE + 'body.filter.property should be `"object"`, instead was `"type"`.\n' + _IN_TRASH,
    ),
    (
        "another property beside the trash",
        {"filter": {"property": "type", "in_trash": True}},
        _NEW,
        400,
        "validation_error",
        'body failed validation: body.filter.property should be `"object"` or not present, instead was `"type"`.',
    ),
    (
        "a trash that is not a boolean",
        {"filter": {"property": "object", "value": "page", "in_trash": "x"}},
        _NEW,
        400,
        "validation_error",
        'body failed validation: body.filter.in_trash should be a boolean or `undefined`, instead was `"x"`.',
    ),
    (
        "all of it and a member more",
        {"filter": {"property": "object", "value": "page", "in_trash": True, "foo": 1}},
        _NEW,
        400,
        "validation_error",
        _FIX_ONE
        + 'body.filter.foo should be not present, instead was `1`.\nbody.filter.property should be not present, instead was `"object"`.\nbody.filter.value should be not present, instead was `"page"`.',
    ),
    # the members in the order they are read, whatever order they are sent in
    (
        "sort ahead of query",
        {"query": 1, "sort": "x"},
        _NEW,
        400,
        "validation_error",
        'body failed validation: body.sort should be an object or `undefined`, instead was `"x"`.',
    ),
    (
        "query ahead of a sort the schema admits",
        {"sort": {"timestamp": "created_time", "direction": "ascending"}, "query": 1},
        _NEW,
        400,
        "validation_error",
        _NOT_A_STRING + "`1`.",
    ),
    (
        "query ahead of the cursor",
        {"start_cursor": 5, "query": 1},
        _NEW,
        400,
        "validation_error",
        _NOT_A_STRING + "`1`.",
    ),
    (
        "the cursor ahead of the page size",
        {"page_size": "x", "start_cursor": 5},
        _NEW,
        400,
        "validation_error",
        "body failed validation: body.start_cursor should be a string or `undefined`, instead was `5`.",
    ),
    (
        "a cursor not a uuid ahead of the page size",
        {"page_size": "x", "start_cursor": "bogus"},
        _NEW,
        400,
        "validation_error",
        'body failed validation: body.start_cursor should be a valid uuid or `undefined`, instead was `"bogus"`.',
    ),
    (
        "the page size ahead of the filter",
        {"filter": 1, "page_size": "x"},
        _NEW,
        400,
        "validation_error",
        'body failed validation: body.page_size should be a number or `undefined`, instead was `"x"`.',
    ),
    (
        "the filter ahead of a member search does not take",
        {"foo": 1, "filter": 1},
        _NEW,
        400,
        "validation_error",
        _NOT_AN_OBJECT + "`1`.",
    ),
    (
        "two members search does not take",
        {"bar": 2, "foo": 1},
        _NEW,
        400,
        "validation_error",
        _FIX_ONE
        + "body.bar should be not present, instead was `2`.\nbody.foo should be not present, instead was `1`.",
    ),
    (
        "a page size that is null",
        {"page_size": None},
        _NEW,
        400,
        "validation_error",
        "body failed validation: body.page_size should be a number or `undefined`, instead was `null`.",
    ),
    (
        "a value cut at 55 units",
        {"foo": "x" * 60},
        _NEW,
        400,
        "validation_error",
        'body failed validation: body.foo should be not present, instead was `"'
        + "x" * 54
        + "...`.",
    ),
]


def test_notion_search_reads_its_body_as_real_does(client, admin_h, monkeypatch):
    """Real's answer to each body, read by ``notion_request.search_body`` and then
    ``search_sort_refusal``, the version ahead of both. A refusal comes ahead of
    ``auth.visible_ids``, and a 200 runs it."""
    from backlot import auth

    looked_up = []
    see = auth.visible_ids
    monkeypatch.setattr(auth, "visible_ids", lambda *a, **k: looked_up.append(a) or see(*a, **k))
    answered, wanted = {}, {}
    for label, body, version, status, code, message in _SEARCH_BODY_TYPE_ROWS:
        looked_up.clear()
        headers = admin_h if version is None else {**admin_h, "Notion-Version": version}
        r = client.post("/notion/v1/search", json=body, headers=headers)
        payload = r.json()
        if code is None:
            answered[label] = (r.status_code, bool(looked_up), payload["object"])
            wanted[label] = (status, True, "list")
        else:
            echoed = payload.get("request_id") == r.headers["x-notion-request-id"]
            answered[label] = (
                r.status_code,
                bool(looked_up),
                [payload.get(k) for k in ("object", "status", "code", "message")],
                echoed,
            )
            wanted[label] = (status, False, ["error", status, code, message], True)
    assert answered == wanted


_SEARCHED_DATABASES = [
    ("2025-09-03", "", "data_source", "data_source"),
    ("2026-03-11", "", "data_source", "data_source"),
    ("2022-06-28", "", "data_source", "database"),
    ("2022-06-28", "", "database", "database"),
    ("2025-09-03", "", None, "data_source"),
    ("2026-03-11", "", None, "data_source"),
    ("2022-06-28", "", None, "database"),
    ("2025-09-03", "Eng Tasks", None, "data_source"),
]


def test_notion_search_lists_the_databases_in_the_versions_shape(client, admin_h):
    """Each search lists the databases as the comment in `search` records, each in the shape its
    own `GET` answers, and beside the pages when it sends neither a filter nor a query."""
    answered, expected = {}, {}
    for version, query, value, kind in _SEARCHED_DATABASES:
        h = {**admin_h, "Notion-Version": version}
        body = {"query": query} | (
            {"filter": {"property": "object", "value": value}} if value else {}
        )
        s = client.post("/notion/v1/search", json=body, headers=h).json()
        listed = [r for r in s["results"] if r["object"] != "page"]
        as_kind = synth.notion_data_source_id if kind == "data_source" else synth.notion_id
        served_id = as_kind("nt-tasks-db")
        got = next((r for r in listed if r["id"] == served_id), None)
        fetched = client.get(f"/notion/v1/{kind}s/{served_id}", headers=h).json()
        # `request_id` names the response, not the object: it rides the list body in a search and
        # the object's own body in the `GET`, so it is dropped from the latter before comparing
        # shapes.
        fetched.pop("request_id", None)
        row = (version, query, value)
        answered[row] = ({r["object"] for r in listed}, len(listed) < len(s["results"]), got)
        expected[row] = ({kind}, body == {"query": ""}, fetched)
    assert answered == expected


@pytest.mark.parametrize(
    "route, query, row_keys",
    [
        ("query", "", {"title", "Status", "Priority"}),
        ("query", "?filter_properties=Status", {"Status"}),
        ("query", "?filter_properties=title", {"title"}),
        ("query", "?filter_properties[]=Status&filter_properties[]=title", {"Status", "title"}),
        ("query", "?filter_properties=", set()),
        ("search", "", {"title", "Status", "Priority"}),
        ("search", "?filter_properties=Status", {"Status"}),
        ("search", "?filter_properties=title", {"title"}),
        ("search", "?filter_properties=Status&filter_properties=Priority", {"Status", "Priority"}),
        ("search", "?filter_properties=nope", set()),
        ("search", "?filter_properties=", set()),
        ("search", "?filter_properties[]=Status", {"Status"}),
        ("search", "?filter_properties%5B%5D=Status", {"Status"}),
        (
            "search",
            "?filter_properties[20]=Status&filter_properties=Priority",
            {"Status", "Priority"},
        ),
        ("search", "?filter_properties=Status&filter_properties[x]=Priority", {"Status"}),
        ("search", "?filter_properties=Status&filter_properties[0][x]=Priority", {"Status"}),
        ("search", "?filter_properties[0][x]=Status", set()),
        ("search", "?filter_properties[0]=Status&filter_properties[0]=Priority", set()),
        ("search", "?filter_properties[x]=Status", None),
        ("search", "?filter_properties[21]=Status", None),
        ("search", "?filter_properties[01]=Status", None),
        ("search", "?filter_properties[]=Status&filter_properties[x]=Priority", None),
        ("search", "?filter_properties[x]=Status&filter_properties=Priority", None),
    ],
)
def test_notion_filter_properties_keeps_only_the_named_row_properties(
    client, notion_h, route, query, row_keys
):
    """The properties a row of `search` or of a data source query comes back with; ``row_keys`` of
    ``None`` is real's 400, answered ahead of the body's own error."""
    if route == "search":
        path, body = "search", {"query": "", "filter": {"property": "object", "value": "page"}}
    else:
        path, body = f"data_sources/{synth.notion_data_source_id('nt-tasks-db')}/query", {}
    r = client.post(
        f"/notion/v1/{path}{query}",
        json=body if row_keys is not None else {**body, "query": 5},
        headers=notion_h,
    )
    if row_keys is None:
        assert (r.status_code, r.json()["code"], r.json()["message"]) == (
            400,
            "validation_error",
            'The value of "filter_properties" in query parameters must be a string or list of '
            "strings",
        )
        # behind the credential and the version
        anon = {"Notion-Version": notion_h["Notion-Version"]}
        r = client.post(f"/notion/v1/search{query}", json=body, headers=anon)
        assert (r.status_code, r.json()["code"]) == (401, "unauthorized")
        unversioned = {"Authorization": notion_h["Authorization"]}
        r = client.post(f"/notion/v1/search{query}", json=body, headers=unversioned)
        assert (r.status_code, r.json()["code"]) == (400, "missing_version")
        return
    results = r.json()["results"]
    row = next(r for r in results if r["id"] == synth.notion_id("nt-task-1"))
    assert set(row["properties"]) == row_keys
    if route == "search":
        # A row outside a database is the workspace page and keeps its title.
        outside = next(r for r in results if r["parent"]["type"] == "workspace")
        assert "title" in outside["properties"]


def test_notion_users(client, notion_h):
    me = client.get("/notion/v1/users/me", headers=notion_h).json()
    assert me["object"] == "user" and me["type"] == "bot"
    lst = client.get("/notion/v1/users", headers=notion_h).json()
    assert lst["results"] and all(u["object"] == "user" for u in lst["results"])
    person = next(u for u in lst["results"] if u.get("type") == "person")
    assert person["person"]["email_verified"] is True
    uid = person["id"]
    retrieved = client.get(f"/notion/v1/users/{uid}", headers=notion_h).json()
    assert retrieved["id"] == uid and retrieved["person"]["email_verified"] is True


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
    # The value comes back as every refusal quotes one (``notion_request.quoted``).
    for sent, quoted in (
        ('a"b', '"a\\"b"'),
        ("x" * 53, '"' + "x" * 53 + '"'),
        ("x" * 60, '"' + "x" * 54 + "..."),
    ):
        r = client.get("/notion/v1/users/me", headers={**admin_h, "Notion-Version": sent})
        assert r.json()["message"].endswith(f"instead was `{quoted}`."), sent
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


_QUERY_MEMBERS = ["sorts", "filter", "start_cursor", "page_size", "result_type"]


_BODY_ROUTE_MEMBERS = [
    ("/notion/v1/search", ["query", "filter", "sort", "start_cursor", "page_size"]),
    ("/notion/v1/data_sources/{data_source_id}/query", _QUERY_MEMBERS),
    ("/notion/v1/databases/{database_id}/query", _QUERY_MEMBERS),
]


def test_notion_a_body_route_declares_the_members_it_reads(client):
    paths = client.get("/openapi.json").json()["paths"]
    declared = {}
    for path, _ in _BODY_ROUTE_MEMBERS:
        op = paths[path]["post"]
        declared[path] = list(
            op["requestBody"]["content"]["application/json"]["schema"]["properties"]
        )
    assert declared == dict(_BODY_ROUTE_MEMBERS)


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


@pytest.fixture(scope="module")
def notion_conn(tmp_path_factory):
    """A read-only connection to a page with a comment, a database and one row of it."""
    s = tiny_corpus(
        tmp_path_factory.mktemp("notion_conn"),
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
    conn = store.connect_ro(s.db_path)
    yield conn
    conn.close()


def test_notion_page_shape(notion_conn):
    from backlot.routers.notion import _page_obj

    conn = notion_conn
    obj = _page_obj(conn, store.get_document(conn, "notion", served_id("notion", "nf-page")))
    assert obj["object"] == "page"
    assert obj["id"] == synth.notion_id("nf-page")
    assert obj["created_by"]["object"] == "user"
    assert obj["parent"] == {"type": "workspace", "workspace": True}
    assert obj["properties"]["title"]["type"] == "title"
    assert obj["properties"]["title"]["title"][0]["plain_text"] == "Runbook"
    assert obj["icon"] == {"type": "emoji", "emoji": "📟"}
    assert obj["url"] == "https://app.notion.com/p/" + obj["id"].replace("-", "")
    # a database row exposes its property values + a database_id parent
    row = _page_obj(conn, store.get_document(conn, "notion", served_id("notion", "nf-row")))
    assert row["parent"]["type"] == "database_id"
    # ...and it is the database's OWN served id, which `parent_id` already holds. Hashing
    # that column again named a database nothing serves, so every row in every database
    # advertised a parent that 404s.
    assert row["parent"]["database_id"] == served_id("notion", "nf-db")
    assert row["properties"]["Status"]["select"]["name"] == "In Progress"


def test_notion_database_and_data_source_shape(notion_conn):
    from backlot.routers.notion import _data_source_obj, _database_obj

    conn = notion_conn
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


def test_notion_user_and_block_shape(notion_conn):
    from backlot.routers.notion import _user_obj

    conn = notion_conn
    u = _user_obj(conn, "ava@acme.com")
    assert u["object"] == "user" and u["type"] == "person"
    assert list(u["person"]) == ["email", "email_verified"]
    assert u["person"]["email"] == "ava@acme.com" and u["person"]["email_verified"] is True
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


# --- the members real serves ----------------------------------------------------
# The measurements are recorded, with their date, in backlot/routers/notion.py: the partial user
# on `_partial_user`, the version-typed parent on `_parent_field`, each object's members and their
# order on the function that renders it, the URL on `_object_url` and the 200's request id on
# `_answer`.

_PAGE_MEMBERS = [
    "object",
    "id",
    "created_time",
    "last_edited_time",
    "created_by",
    "last_edited_by",
    "cover",
    "icon",
    "parent",
    "in_trash",
    "is_archived",
    "is_locked",
    "properties",
    "url",
    "public_url",
    "archived",
]
_BLOCK_MEMBERS = [
    "object",
    "id",
    "parent",
    "created_time",
    "last_edited_time",
    "created_by",
    "last_edited_by",
    "has_children",
    "in_trash",
    "type",
    "<type>",
    "archived",
]
_DATABASE_MEMBERS = {
    "2025-09-03": [
        "object",
        "id",
        "title",
        "description",
        "parent",
        "is_inline",
        "database_type",
        "in_trash",
        "is_locked",
        "created_time",
        "last_edited_time",
        "data_sources",
        "icon",
        "cover",
        "url",
        "public_url",
        "archived",
    ],
    "2022-06-28": [
        "object",
        "id",
        "cover",
        "icon",
        "created_time",
        "created_by",
        "last_edited_by",
        "last_edited_time",
        "title",
        "description",
        "is_inline",
        "database_type",
        "properties",
        "parent",
        "url",
        "public_url",
        "in_trash",
        "archived",
    ],
}
_DATA_SOURCE_MEMBERS = [
    *_DATABASE_MEMBERS["2022-06-28"][:14],
    "database_parent",
    *_DATABASE_MEMBERS["2022-06-28"][14:],
]
_RUNBOOK_ID = synth.notion_id("nt-runbook")
_ROW_ID = synth.notion_id("nt-task-1")
_TASKS_DB = synth.notion_id("nt-tasks-db")
_TASKS_DS = synth.notion_data_source_id("nt-tasks-db")

_PERSON_MEMBERS = ["object", "id", "name", "avatar_url", "type", "person"]

_MEMBER_ROWS = [
    # label, method, path, version, the object read off the body, real's members in real's order;
    # the admin token calls each but the last, which a person's token calls
    ("page", "GET", f"pages/{_RUNBOOK_ID}", "2025-09-03", None, _PAGE_MEMBERS),
    ("page 2022", "GET", f"pages/{_RUNBOOK_ID}", "2022-06-28", None, _PAGE_MEMBERS),
    ("row", "GET", f"pages/{_ROW_ID}", "2025-09-03", None, _PAGE_MEMBERS),
    ("searched page", "POST", "search", "2025-09-03", "results", _PAGE_MEMBERS),
    (
        "queried row",
        "POST",
        f"data_sources/{_TASKS_DS}/query",
        "2025-09-03",
        "results",
        _PAGE_MEMBERS,
    ),
    ("page's block", "GET", f"blocks/{_RUNBOOK_ID}", "2025-09-03", None, _BLOCK_MEMBERS),
    ("row's block 2022", "GET", f"blocks/{_ROW_ID}", "2022-06-28", None, _BLOCK_MEMBERS),
    (
        "child block",
        "GET",
        f"blocks/{_RUNBOOK_ID}/children",
        "2025-09-03",
        "results",
        _BLOCK_MEMBERS,
    ),
    (
        "database",
        "GET",
        f"databases/{_TASKS_DB}",
        "2025-09-03",
        None,
        _DATABASE_MEMBERS["2025-09-03"],
    ),
    (
        "database 2022",
        "GET",
        f"databases/{_TASKS_DB}",
        "2022-06-28",
        None,
        _DATABASE_MEMBERS["2022-06-28"],
    ),
    ("data source", "GET", f"data_sources/{_TASKS_DS}", "2025-09-03", None, _DATA_SOURCE_MEMBERS),
    (
        "bot",
        "GET",
        "users/me",
        "2025-09-03",
        None,
        ["object", "id", "name", "avatar_url", "type", "bot"],
    ),
    (
        "person",
        "GET",
        f"users/{synth.notion_user_id('ava@acme.com')}",
        "2025-09-03",
        None,
        _PERSON_MEMBERS,
    ),
    ("listed person", "GET", "users", "2025-09-03", "results", _PERSON_MEMBERS),
    ("me as a person", "GET", "users/me", "2025-09-03", None, _PERSON_MEMBERS),
]


@pytest.mark.parametrize(
    "label, method, path, version, listed, members",
    _MEMBER_ROWS,
    ids=[row[0] for row in _MEMBER_ROWS],
)
def test_notion_an_object_carries_reals_members_in_reals_order(
    client, admin_h, tokens_yaml, label, method, path, version, listed, members
):
    h = {**admin_h, "Notion-Version": version}
    if label == "me as a person":
        h["Authorization"] = f"Bearer {tok(tokens_yaml, 'bob@acme.com')}"
    body = {"filter": {"property": "object", "value": "page"}} if path == "search" else {}
    r = client.request(
        method, f"/notion/v1/{path}", headers=h, json=body if method == "POST" else None
    )
    assert r.status_code == 200, r.text
    got = r.json()[listed][0] if listed else r.json()
    expected = [got.get("type") if m == "<type>" else m for m in members]
    assert list(got) == (expected if listed else [*expected, "request_id"])


_PARENT_RECORDS = [
    {
        "source_type": "notion",
        "doc_id": "nf-parent",
        "title": "Parent",
        "content": "body",
        "author_email": "ava@acme.com",
        "visibility": "public",
        "created": "2026-03-01T08:15:00Z",
        "updated": "2026-03-02T09:30:00Z",
    },
    {
        "source_type": "notion",
        "doc_id": "nf-child",
        "parent": "nf-parent",
        "title": "Child",
        "content": "body",
        "author_email": "ava@acme.com",
        "visibility": "public",
    },
    {
        "source_type": "notion",
        "doc_id": "nf-db",
        "subtype": "database",
        "title": "Tasks",
        "content": "Tracker",
        "author_email": "ava@acme.com",
        "visibility": "public",
        "properties": {"Status": {"type": "select"}},
        "created": "2026-03-03T10:00:00Z",
        "updated": "2026-03-04T11:45:00Z",
    },
    {
        "source_type": "notion",
        "doc_id": "nf-row",
        "parent": "nf-db",
        "title": "Fix bug",
        "content": "body",
        "author_email": "bob@acme.com",
        "visibility": "public",
        "properties": {"Status": "Open"},
    },
]


@pytest.fixture(scope="module")
def parent_corpus(tmp_path_factory):
    """`_PARENT_RECORDS` built."""
    return tiny_corpus(tmp_path_factory.mktemp("parent_corpus"), _PARENT_RECORDS)


_PARENT_ROWS = [
    # label, the record, version, its parent on the page and on its block alike
    ("page under a page", "nf-child", "2025-09-03", {"type": "page_id", "page_id": "nf-parent"}),
    (
        "page under a page 2022",
        "nf-child",
        "2022-06-28",
        {"type": "page_id", "page_id": "nf-parent"},
    ),
    (
        "row",
        "nf-row",
        "2025-09-03",
        {"type": "data_source_id", "data_source_id": "ds:nf-db", "database_id": "nf-db"},
    ),
    ("row 2022", "nf-row", "2022-06-28", {"type": "database_id", "database_id": "nf-db"}),
    ("top-level page", "nf-parent", "2025-09-03", {"type": "workspace", "workspace": True}),
]


def test_notion_a_page_and_its_block_name_the_parent_the_version_reads(parent_corpus):
    def served(value):
        if value.startswith("ds:"):
            return synth.notion_data_source_id(value[3:])
        return served_id("notion", value)

    settings = parent_corpus
    with client_for(settings) as client:
        for label, doc, version, parent in _PARENT_ROWS:
            h = {"Authorization": f"Bearer {settings.admin_token}", "Notion-Version": version}
            expected = {k: served(v) if k.endswith("_id") else v for k, v in parent.items()}
            for route in ("pages", "blocks"):
                got = client.get(f"/notion/v1/{route}/{served_id('notion', doc)}", headers=h)
                assert got.json()["parent"] == expected, (label, route)


def test_notion_an_author_is_the_partial_user(client, notion_h):
    ava = {"object": "user", "id": synth.notion_user_id("ava@acme.com")}
    bob = {"object": "user", "id": synth.notion_user_id("bob@acme.com")}
    legacy = {**notion_h, "Notion-Version": "2022-06-28"}
    answers = [
        (ava, client.get(f"/notion/v1/pages/{_RUNBOOK_ID}", headers=notion_h).json()),
        (bob, client.get(f"/notion/v1/pages/{_ROW_ID}", headers=notion_h).json()),
        (ava, client.get(f"/notion/v1/blocks/{_RUNBOOK_ID}", headers=notion_h).json()),
        (
            ava,
            client.get(f"/notion/v1/blocks/{_RUNBOOK_ID}/children", headers=notion_h).json()[
                "results"
            ][0],
        ),
        (ava, client.get(f"/notion/v1/databases/{_TASKS_DB}", headers=legacy).json()),
        (ava, client.get(f"/notion/v1/data_sources/{_TASKS_DS}", headers=notion_h).json()),
    ]
    for author, obj in answers:
        assert obj["created_by"] == author and obj["last_edited_by"] == author, obj["object"]


def test_notion_url_is_the_app_host_and_a_data_source_names_its_databases(client, notion_h):
    def url(served):
        return "https://app.notion.com/p/" + served.replace("-", "")

    page = client.get(f"/notion/v1/pages/{_RUNBOOK_ID}", headers=notion_h).json()
    database = client.get(f"/notion/v1/databases/{_TASKS_DB}", headers=notion_h).json()
    source = client.get(f"/notion/v1/data_sources/{_TASKS_DS}", headers=notion_h).json()
    assert page["url"] == url(_RUNBOOK_ID)
    assert database["url"] == url(_TASKS_DB)
    assert source["url"] == url(_TASKS_DB)
    assert source["parent"] == {"type": "database_id", "database_id": _TASKS_DB}
    assert (
        source["database_parent"] == database["parent"] == {"type": "workspace", "workspace": True}
    )


def test_notion_a_page_its_blocks_and_a_data_source_answer_millisecond_timestamps(parent_corpus):
    settings = parent_corpus
    with client_for(settings) as client:
        h = {"Authorization": f"Bearer {settings.admin_token}", "Notion-Version": "2025-09-03"}
        page = served_id("notion", "nf-parent")
        answers = [
            client.get(f"/notion/v1/pages/{page}", headers=h).json(),
            client.get(f"/notion/v1/blocks/{page}", headers=h).json(),
            client.get(f"/notion/v1/blocks/{page}/children", headers=h).json()["results"][0],
        ]
        for obj in answers:
            assert obj["created_time"] == "2026-03-01T08:15:00.000Z", obj["object"]
            assert obj["last_edited_time"] == "2026-03-02T09:30:00.000Z", obj["object"]
        source = client.get(
            f"/notion/v1/data_sources/{synth.notion_data_source_id('nf-db')}", headers=h
        ).json()
        assert source["created_time"] == "2026-03-03T10:00:00.000Z"
        assert source["last_edited_time"] == "2026-03-04T11:45:00.000Z"


def test_notion_search_and_query_trailers_follow_the_version(client, admin_h):
    def post(h, path="/notion/v1/search", body=None):
        return client.post(path, json=body or {}, headers=h).json()

    new = post({**admin_h, "Notion-Version": "2025-09-03"})
    assert new["type"] == "page_or_data_source" and "page_or_data_source" in new
    old = post({**admin_h, "Notion-Version": "2022-06-28"})
    assert old["type"] == "page_or_database" and "page_or_database" in old
    dsid = synth.notion_data_source_id("nt-tasks-db")
    new_rows = post(
        {**admin_h, "Notion-Version": "2025-09-03"}, f"/notion/v1/data_sources/{dsid}/query"
    )
    assert new_rows["type"] == "page_or_data_source" and "page_or_data_source" in new_rows
    dbid = synth.notion_id("nt-tasks-db")
    old_rows = post(
        {**admin_h, "Notion-Version": "2022-06-28"}, f"/notion/v1/databases/{dbid}/query"
    )
    assert old_rows["type"] == "page_or_database" and "page_or_database" in old_rows


def test_notion_bot_names_its_workspace(client, notion_h):
    import uuid

    bot = client.get("/notion/v1/users/me", headers=notion_h).json()["bot"]
    assert list(bot) == ["owner", "workspace_name", "workspace_id", "workspace_limits"]
    assert uuid.UUID(bot["workspace_id"]).version == 4
    assert bot["workspace_limits"] == {"max_file_upload_size_in_bytes": 5242880}


# --- what a request carries, ahead of what it asks for -----------------------------------------
#
# Each row is api.notion.com's answer to the request under 2025-09-03 (and 2022-06-28 for the
# database query): status, then the message of a refusal, or the members a body Express refuses
# carries, or None for a 200. `{page}`, `{ds}`, `{db}` and `{user}` are a page, the
# data source, its database and a user the corpus serves, and `{rand}` a uuid naming nothing.

_RAND = "11111111-2222-4333-8444-555555555555"
_FIX = "query failed validation. Fix one:\n"
_BODY_FIX = "body failed validation. Fix one:\n"


def _absent(where: str, key: str, value: str) -> str:
    return f"{where} failed validation: {where}.{key} should be not present, instead was `{value}`."


def _foo(value: str) -> str:
    return _absent("query", "foo", value)


_CONFLICT = 'Conflicting values for "{}" in query parameters.'
_OBJECT = 'The value of "{}" in query parameters must be a string, number, or boolean.'
_NOT_A_NUMBER = (
    "query failed validation: query.page_size should be a number or `undefined`, instead was "
)
_NOT_A_CURSOR = (
    "query failed validation: query.start_cursor should be a string or `undefined`, instead was "
)
_NOT_A_UUID_CURSOR = "query failed validation: query.start_cursor should be a valid uuid or `undefined`, instead was "
_INVALID = "The start_cursor provided is invalid: "
_NOT_A_LIST = (
    'The value of "filter_properties" in query parameters must be a string or list of strings'
)
_UNKNOWN_API = {
    "isNotionError": False,
    "name": "UnknownApiError",
    "message": "Something went wrong.",
}
_TOO_LARGE = {
    "isNotionError": False,
    "name": "PayloadTooLargeError",
    "message": "Request body too large.",
    "code": 413,
}
_INVALID_JSON = "Error parsing JSON body."
_JSON = {"Content-Type": "application/json"}
_LONE_HIGH = chr(0xD83D)
_EMOJI = "%F0%9F%98%80"

#: What a data source query's filter is refused with when it names none of the filter types: the
#: lines, at the top or inside a compound, in the order real listed them.
_FILTER_TYPES = [
    "title",
    "rich_text",
    "number",
    "checkbox",
    "select",
    "multi_select",
    "status",
    "date",
    "people",
    "files",
    "url",
    "email",
    "phone_number",
    "relation",
    "created_by",
    "created_time",
    "last_edited_by",
    "last_edited_time",
    "formula",
    "unique_id",
    "rollup",
    "verification",
]


def _defined(where: str, keys: list[str]) -> str:
    return _BODY_FIX + "\n".join(
        f"{where}.{k} should be defined, instead was `undefined`." for k in keys
    )


_TOP = ["or", "and", *_FILTER_TYPES]
_INSIDE = [*_FILTER_TYPES, "or", "and"]
_TIMESTAMP_INSIDE = [
    *(("property" if k == "created_time" else k) for k in _FILTER_TYPES),
    "timestamp",
    "or",
    "and",
]
_Q = "/data_sources/{ds}/query"

# label, method, path under /notion/v1 (or `/notion`, the root itself), query string, what the
# body is (a dict sent as JSON, bytes or a str sent as they stand, None for no body), headers
# beyond the token and the version (None drops one; a list of pairs to send one twice), each sent
# byte for byte, status, and what is expected; a 404's words are their own gap, so a 404 row
# names only its status
_REQUEST_ROWS = [
    # the query string as `qs` parses it, and each value as the validator coerces it
    ("unknown key", "GET", "/users", "foo=1", None, {}, 400, _foo("1")),
    ("a string", "GET", "/users", "foo=abc", None, {}, 400, _foo('"abc"')),
    ("empty", "GET", "/users", "foo=", None, {}, 400, _foo("0")),
    ("no equals", "GET", "/users", "foo", None, {}, 400, _foo("0")),
    ("a space", "GET", "/users", "foo=%20", None, {}, 400, _foo("0")),
    ("zero-padded", "GET", "/users", "foo=01", None, {}, 400, _foo("1")),
    ("a point zero", "GET", "/users", "foo=1.0", None, {}, 400, _foo("1")),
    ("a fraction", "GET", "/users", "foo=1.5", None, {}, 400, _foo('"1.5"')),
    ("a fraction alone", "GET", "/users", "foo=.5", None, {}, 400, _foo('".5"')),
    ("TRUE", "GET", "/users", "foo=TRUE", None, {}, 400, _foo("true")),
    ("False", "GET", "/users", "foo=False", None, {}, 400, _foo("false")),
    ("true after a space", "GET", "/users", "foo=%20true", None, {}, 400, _foo('" true"')),
    ("null", "GET", "/users", "foo=null", None, {}, 400, _foo('"null"')),
    ("1e21", "GET", "/users", "foo=1e21", None, {}, 400, _foo("1e+21")),
    ("1e20", "GET", "/users", "foo=1e20", None, {}, 400, _foo("100000000000000000000")),
    ("1e-7", "GET", "/users", "foo=1e-7", None, {}, 400, _foo('"1e-7"')),
    ("1e400", "GET", "/users", "foo=1e400", None, {}, 400, _foo('"1e400"')),
    ("Infinity", "GET", "/users", "foo=Infinity", None, {}, 400, _foo('"Infinity"')),
    ("hexadecimal", "GET", "/users", "foo=0x10", None, {}, 400, _foo("16")),
    ("signed hexadecimal", "GET", "/users", "foo=-0x10", None, {}, 400, _foo('"-0x10"')),
    ("binary", "GET", "/users", "foo=0b11", None, {}, 400, _foo("3")),
    ("a hexadecimal prefix alone", "GET", "/users", "foo=0x", None, {}, 400, _foo('"0x"')),
    ("a hexadecimal prefix and no digit", "GET", "/users", "foo=0xg", None, {}, 400, _foo('"0xg"')),
    ("hexadecimal in upper case", "GET", "/users", "foo=0xFF", None, {}, 400, _foo("255")),
    (
        "hexadecimal under an upper-case prefix",
        "GET",
        "/users",
        "foo=0XfF",
        None,
        {},
        400,
        _foo("255"),
    ),
    ("octal", "GET", "/users", "foo=0o17", None, {}, 400, _foo("15")),
    ("octal under an upper-case prefix", "GET", "/users", "foo=0O17", None, {}, 400, _foo("15")),
    ("octal with an 8", "GET", "/users", "foo=0o8", None, {}, 400, _foo('"0o8"')),
    ("binary under an upper-case prefix", "GET", "/users", "foo=0B11", None, {}, 400, _foo("3")),
    ("binary with a 2", "GET", "/users", "foo=0b2", None, {}, 400, _foo('"0b2"')),
    ("hexadecimal after a space", "GET", "/users", "foo=%200x10", None, {}, 400, _foo("16")),
    ("hexadecimal before a space", "GET", "/users", "foo=0x10%20", None, {}, 400, _foo("16")),
    (
        "hexadecimal with a space inside",
        "GET",
        "/users",
        "foo=0x%2010",
        None,
        {},
        400,
        _foo('"0x 10"'),
    ),
    (
        "hexadecimal with an underscore",
        "GET",
        "/users",
        "foo=0x1_0",
        None,
        {},
        400,
        _foo('"0x1_0"'),
    ),
    (
        "a hexadecimal past every double",
        "GET",
        "/users",
        "foo=0x" + "f" * 260,
        None,
        {},
        400,
        _foo('"0x' + "f" * 52 + "..."),
    ),
    (
        "a hexadecimal below the largest double",
        "GET",
        "/users",
        "foo=0x" + "f" * 255,
        None,
        {},
        400,
        _foo("1.1235582092889474e+307"),
    ),
    ("an underscore", "GET", "/users", "foo=1_0", None, {}, 400, _foo('"1_0"')),
    ("minus zero", "GET", "/users", "foo=-0", None, {}, 400, _foo("0")),
    ("a plus, read as a space", "GET", "/users", "foo=+1", None, {}, 400, _foo("1")),
    ("an escaped plus", "GET", "/users", "foo=%2B.5", None, {}, 400, _foo('"+.5"')),
    (
        "past 2**53",
        "GET",
        "/users",
        "foo=9007199254740993",
        None,
        {},
        400,
        _foo("9007199254740992"),
    ),
    (
        "thirty digits",
        "GET",
        "/users",
        "foo=123456789012345678901234567890",
        None,
        {},
        400,
        _foo("1.2345678901234568e+29"),
    ),
    ("a no-break space trimmed", "GET", "/users", "foo=%C2%A01", None, {}, 400, _foo("1")),
    ("U+0085 kept", "GET", "/users", "foo=%C2%851", None, {}, 400, _foo('"' + chr(0x85) + '1"')),
    ("U+001C kept", "GET", "/users", "foo=%1C1", None, {}, 400, _foo('"\\u001c1"')),
    *[
        (f"U+{cp:04X} trimmed", "GET", "/users", f"foo={quote(chr(cp))}1", None, {}, 400, _foo("1"))
        for cp in (
            0x09,
            0x0B,
            0x0C,
            0x0D,
            0x1680,
            0x2000,
            0x2003,
            0x200A,
            0x2028,
            0x2029,
            0x202F,
            0x205F,
            0x3000,
            0xFEFF,
        )
    ],
    ("a line feed after, trimmed", "GET", "/users", "foo=1%0A", None, {}, 400, _foo("1")),
    ("a carriage return after, trimmed", "GET", "/users", "foo=1%0D", None, {}, 400, _foo("1")),
    (
        "U+200B kept",
        "GET",
        "/users",
        "foo=1%E2%80%8B",
        None,
        {},
        400,
        _foo('"1' + chr(0x200B) + '"'),
    ),
    (
        "U+2060 kept",
        "GET",
        "/users",
        "foo=%E2%81%A01",
        None,
        {},
        400,
        _foo('"' + chr(0x2060) + '1"'),
    ),
    ("a quote", "GET", "/users", "foo=x%22y", None, {}, 400, _foo('"x\\"y"')),
    ("a bad escape", "GET", "/users", "foo=%ZZ", None, {}, 400, _foo('"%ZZ"')),
    (
        "a bad escape, then a good one",
        "GET",
        "/users",
        "foo=%zz%41",
        None,
        {},
        400,
        _foo('"%zz%41"'),
    ),
    (
        "a good escape, then a bad one",
        "GET",
        "/users",
        "foo=%41%zz",
        None,
        {},
        400,
        _foo('"%41%zz"'),
    ),
    ("an escape", "GET", "/users", "foo=%41", None, {}, 400, _foo('"A"')),
    ("an escape that is not UTF-8", "GET", "/users", "foo=%FF", None, {}, 400, _foo('"%FF"')),
    ("54 x, cut", "GET", "/users", "foo=" + "x" * 54, None, {}, 400, _foo('"' + "x" * 54 + "...")),
    (
        "53 x and U+1F600, cut inside it",
        "GET",
        "/users",
        "foo=" + "x" * 53 + _EMOJI,
        None,
        {},
        400,
        _foo('"' + "x" * 53 + _LONE_HIGH + "..."),
    ),
    (
        "52 x and U+1F600, cut after it",
        "GET",
        "/users",
        "foo=" + "x" * 52 + _EMOJI,
        None,
        {},
        400,
        _foo('"' + "x" * 52 + chr(0x1F600) + "..."),
    ),
    (
        "two unknown keys",
        "GET",
        "/users",
        "bar=2&foo=1",
        None,
        {},
        400,
        _FIX
        + "query.bar should be not present, instead was `2`.\nquery.foo should be not present, instead was `1`.",
    ),
    (
        "a key in another case",
        "GET",
        "/users",
        "foo=1&FOO=2",
        None,
        {},
        400,
        _FIX
        + "query.foo should be not present, instead was `1`.\nquery.FOO should be not present, instead was `2`.",
    ),
    (
        "a long key, whole",
        "GET",
        "/users",
        "k" * 70 + "=1",
        None,
        {},
        400,
        _absent("query", "k" * 70, "1"),
    ),
    ("repeated", "GET", "/users", "foo=1&foo=1", None, {}, 400, _CONFLICT.format("foo")),
    ("[]", "GET", "/users", "foo[]=1", None, {}, 400, _CONFLICT.format("foo")),
    ("[20]", "GET", "/users", "foo[20]=1", None, {}, 400, _CONFLICT.format("foo")),
    ("[21]", "GET", "/users", "foo[21]=1", None, {}, 400, _OBJECT.format("foo")),
    ("[-1]", "GET", "/users", "foo[-1]=1", None, {}, 400, _OBJECT.format("foo")),
    ("[a]", "GET", "/users", "foo[a]=1", None, {}, 400, _OBJECT.format("foo")),
    (
        "an equals sign inside the bracket",
        "GET",
        "/users",
        "foo[a=b]=1",
        None,
        {},
        400,
        _OBJECT.format("foo"),
    ),
    ("a closing bracket after", "GET", "/users", "foo[a]]=1", None, {}, 400, _OBJECT.format("foo")),
    (
        "a bracket inside a bracket",
        "GET",
        "/users",
        "foo[a[b]]=1",
        None,
        {},
        400,
        _OBJECT.format("foo[a"),
    ),
    ("[01]", "GET", "/users", "foo[01]=1", None, {}, 400, _OBJECT.format("foo")),
    ("[00]", "GET", "/users", "foo[00]=1", None, {}, 400, _OBJECT.format("foo")),
    ("a superscript digit", "GET", "/users", "foo[%C2%B2]=1", None, {}, 400, _OBJECT.format("foo")),
    (
        "an Arabic-Indic digit",
        "GET",
        "/users",
        "foo[%D9%A3]=1",
        None,
        {},
        400,
        _OBJECT.format("foo"),
    ),
    ("a value, then []", "GET", "/users", "foo=1&foo[]=2", None, {}, 400, _CONFLICT.format("foo")),
    ("[], then a value", "GET", "/users", "foo[]=1&foo=2", None, {}, 400, _CONFLICT.format("foo")),
    ("[] twice", "GET", "/users", "foo[]=1&foo[]=2", None, {}, 400, _CONFLICT.format("foo")),
    (
        "[0], then a value",
        "GET",
        "/users",
        "foo[0]=1&foo=2",
        None,
        {},
        400,
        _CONFLICT.format("foo"),
    ),
    ("[a], then []", "GET", "/users", "foo[a]=1&foo[]=2", None, {}, 400, _OBJECT.format("foo")),
    ("two objects", "GET", "/users", "foo[a]=1&foo[b]=2", None, {}, 400, _OBJECT.format("foo")),
    ("an empty part", "GET", "/users", "&&foo=1", None, {}, 400, _foo("1")),
    ("escaped []", "GET", "/users", "foo%5B%5D=1", None, {}, 400, _CONFLICT.format("foo")),
    ("an open bracket", "GET", "/users", "foo[=1", None, {}, 400, _absent("query", "foo[", "1")),
    (
        "a key that is a bracket",
        "GET",
        "/users",
        "[a]=1",
        None,
        {},
        400,
        _absent("query", "a", "1"),
    ),
    ("a key that is []", "GET", "/users", "[]=1", None, {}, 400, _absent("query", "0", "1")),
    ("no key", "GET", "/users", "=1", None, {}, 200, None),
    (
        "a value then an object",
        "GET",
        "/users",
        "foo=1&foo[a]=2",
        None,
        {},
        400,
        _CONFLICT.format("foo"),
    ),
    (
        "an object then a value",
        "GET",
        "/users",
        "foo[a]=2&foo=1",
        None,
        {},
        400,
        _OBJECT.format("foo"),
    ),
    (
        "a list then an object",
        "GET",
        "/users",
        "foo[]=1&foo[a]=2",
        None,
        {},
        400,
        _OBJECT.format("foo"),
    ),
    (
        "the first key's shape",
        "GET",
        "/users",
        "bar[a]=1&foo=1&foo=2",
        None,
        {},
        400,
        _OBJECT.format("bar"),
    ),
    (
        "the first key's shape, the other way",
        "GET",
        "/users",
        "foo=1&foo=2&bar[a]=1",
        None,
        {},
        400,
        _CONFLICT.format("foo"),
    ),
    (
        "a shape ahead of a member",
        "GET",
        "/users",
        "foo[a]=1&page_size=abc",
        None,
        {},
        400,
        _OBJECT.format("foo"),
    ),
    # `users`
    (
        "page size not a number",
        "GET",
        "/users",
        "page_size=abc",
        None,
        {},
        400,
        _NOT_A_NUMBER + '`"abc"`.',
    ),
    ("page size true", "GET", "/users", "page_size=true", None, {}, 400, _NOT_A_NUMBER + "`true`."),
    (
        "page size 1e309",
        "GET",
        "/users",
        "page_size=1e309",
        None,
        {},
        400,
        _NOT_A_NUMBER + '`"1e309"`.',
    ),
    (
        "page size ahead of an unknown key",
        "GET",
        "/users",
        "foo=1&page_size=abc",
        None,
        {},
        400,
        _NOT_A_NUMBER + '`"abc"`.',
    ),
    (
        "cursor ahead of page size",
        "GET",
        "/users",
        "page_size=abc&start_cursor=5",
        None,
        {},
        400,
        _NOT_A_CURSOR + "`5`.",
    ),
    ("cursor empty", "GET", "/users", "start_cursor=", None, {}, 400, _NOT_A_CURSOR + "`0`."),
    (
        "cursor of 32 digits",
        "GET",
        "/users",
        "start_cursor=11111111222243338444555555555555",
        None,
        {},
        400,
        _NOT_A_CURSOR + "`1.111111122224334e+31`.",
    ),
    (
        "unknown key ahead of a cursor naming nothing",
        "GET",
        "/users",
        "start_cursor=bogus&foo=1",
        None,
        {},
        400,
        _foo("1"),
    ),
    (
        "cursor naming nothing",
        "GET",
        "/users",
        "start_cursor=bogus",
        None,
        {},
        400,
        _INVALID + "bogus",
    ),
    ("cursor null", "GET", "/users", "start_cursor=null", None, {}, 400, _INVALID + "null"),
    (
        "a long cursor, whole",
        "GET",
        "/users",
        "start_cursor=" + "x" * 70,
        None,
        {},
        400,
        _INVALID + "x" * 70,
    ),
    (
        "cursor naming nothing ahead of -2",
        "GET",
        "/users",
        "page_size=-2&start_cursor=bogus",
        None,
        {},
        400,
        _INVALID + "bogus",
    ),
    (
        "page size -2",
        "GET",
        "/users",
        "page_size=-2",
        None,
        {},
        503,
        "Public API service is temporarily unavailable, please try again later.",
    ),
    # `blocks/{id}/children`
    (
        "children: a cursor of a space, a number",
        "GET",
        "/blocks/{page}/children",
        "start_cursor=%20",
        None,
        {},
        400,
        _NOT_A_CURSOR + "`0`.",
    ),
    (
        "children: a cursor naming a block, in upper case",
        "GET",
        "/blocks/{page}/children",
        "start_cursor=" + synth.notion_block_id(_RUNBOOK, 0).upper(),
        None,
        {},
        400,
        _INVALID + synth.notion_block_id(_RUNBOOK, 0).upper(),
    ),
    (
        "children: a cursor not a uuid",
        "GET",
        "/blocks/{page}/children",
        "start_cursor=bogus",
        None,
        {},
        400,
        _NOT_A_UUID_CURSOR + '`"bogus"`.',
    ),
    (
        "children: a cursor without its dashes",
        "GET",
        "/blocks/{page}/children",
        "start_cursor=11111111222243338444555555555abc",
        None,
        {},
        400,
        _NOT_A_UUID_CURSOR + '`"11111111222243338444555555555abc"`.',
    ),
    (
        "children: the cursor ahead of the page size",
        "GET",
        "/blocks/{page}/children",
        "page_size=abc&start_cursor=bogus",
        None,
        {},
        400,
        _NOT_A_UUID_CURSOR + '`"bogus"`.',
    ),
    (
        "children: the cursor's shape ahead of an unknown key",
        "GET",
        "/blocks/{page}/children",
        "foo=1&start_cursor=bogus",
        None,
        {},
        400,
        _NOT_A_UUID_CURSOR + '`"bogus"`.',
    ),
    (
        "children: a cursor naming nothing",
        "GET",
        "/blocks/{page}/children",
        "start_cursor={rand}",
        None,
        {},
        400,
        _INVALID + _RAND,
    ),
    (
        "children: a cursor in upper case",
        "GET",
        "/blocks/{page}/children",
        "start_cursor=" + _RAND.upper(),
        None,
        {},
        400,
        _INVALID + _RAND.upper(),
    ),
    (
        "children: no block, a cursor not a uuid",
        "GET",
        "/blocks/{rand}/children",
        "start_cursor=bogus",
        None,
        {},
        400,
        _NOT_A_UUID_CURSOR + '`"bogus"`.',
    ),
    (
        "children: no block, an unknown key",
        "GET",
        "/blocks/{rand}/children",
        "foo=1",
        None,
        {},
        400,
        _foo("1"),
    ),
    (
        "children: no block, a uuid cursor",
        "GET",
        "/blocks/{rand}/children",
        "start_cursor={rand}",
        None,
        {},
        404,
        None,
    ),
    (
        "children: no block, page size -1",
        "GET",
        "/blocks/{rand}/children",
        "page_size=-1",
        None,
        {},
        404,
        None,
    ),
    # the routes that read no query member, and the page, which serves one
    (
        "block",
        "GET",
        "/blocks/{page}",
        "foo=1&bar=2",
        None,
        {},
        400,
        _FIX
        + "query.foo should be not present, instead was `1`.\nquery.bar should be not present, instead was `2`.",
    ),
    (
        "me: a page size",
        "GET",
        "/users/me",
        "page_size=1",
        None,
        {},
        400,
        _absent("query", "page_size", "1"),
    ),
    (
        "me: filter_properties, a list",
        "GET",
        "/users/me",
        "filter_properties=title",
        None,
        {},
        400,
        _absent("query", "filter_properties", '["title"]'),
    ),
    (
        "block: filter_properties twice, one list",
        "GET",
        "/blocks/{page}",
        "filter_properties=a&filter_properties=b",
        None,
        {},
        400,
        _absent("query", "filter_properties", '["a","b"]'),
    ),
    (
        "block: filter_properties[], a list",
        "GET",
        "/blocks/{page}",
        "filter_properties[]=a",
        None,
        {},
        400,
        _absent("query", "filter_properties", '["a"]'),
    ),
    (
        "block: filter_properties as an object",
        "GET",
        "/blocks/{page}",
        "filter_properties[x]=a",
        None,
        {},
        400,
        _NOT_A_LIST,
    ),
    (
        "block: an object, then filter_properties as one",
        "GET",
        "/blocks/{page}",
        "foo[a]=1&filter_properties[x]=a",
        None,
        {},
        400,
        _OBJECT.format("foo"),
    ),
    (
        "block: filter_properties as an object, then an object",
        "GET",
        "/blocks/{page}",
        "filter_properties[x]=a&foo[a]=1",
        None,
        {},
        400,
        _NOT_A_LIST,
    ),
    (
        "search: an unknown key, then filter_properties as an object",
        "POST",
        "/search",
        "foo=1&filter_properties[x]=a",
        {},
        _JSON,
        400,
        _NOT_A_LIST,
    ),
    (
        "search: a repeated key, then filter_properties as an object",
        "POST",
        "/search",
        "foo=1&foo=2&filter_properties[x]=a",
        {},
        _JSON,
        400,
        _CONFLICT.format("foo"),
    ),
    (
        "search: filter_properties as an object, then a repeated key",
        "POST",
        "/search",
        "filter_properties[x]=a&foo=1&foo=2",
        {},
        _JSON,
        400,
        _NOT_A_LIST,
    ),
    (
        "search: filter_properties as an object, and a body it refuses",
        "POST",
        "/search",
        "filter_properties[x]=a",
        {"page_size": "x"},
        _JSON,
        400,
        _NOT_A_LIST,
    ),
    (
        "query: an unknown key, then filter_properties as an object",
        "POST",
        _Q,
        "foo=1&filter_properties[x]=a",
        {},
        _JSON,
        400,
        _NOT_A_LIST,
    ),
    (
        "query: filter_properties as an object, and a body it refuses",
        "POST",
        _Q,
        "filter_properties[x]=a",
        {"page_size": "x"},
        _JSON,
        400,
        _NOT_A_LIST,
    ),
    (
        "query: filter_properties[], then an object",
        "POST",
        _Q,
        "filter_properties[]=a&foo[a]=1",
        {},
        _JSON,
        400,
        _OBJECT.format("foo"),
    ),
    (
        "page: a page size, then filter_properties as an object",
        "GET",
        "/pages/{page}",
        "page_size=x&filter_properties[x]=a",
        None,
        {},
        400,
        _NOT_A_LIST,
    ),
    ("user", "GET", "/users/{user}", "foo=1", None, {}, 400, _foo("1")),
    ("database", "GET", "/databases/{db}", "foo=1", None, {}, 400, _foo("1")),
    ("data source", "GET", "/data_sources/{ds}", "foo=1", None, {}, 400, _foo("1")),
    ("page: an unknown key", "GET", "/pages/{page}", "foo=1&bar=2", None, {}, 200, None),
    (
        "page: repeated",
        "GET",
        "/pages/{page}",
        "foo=1&foo=2",
        None,
        {},
        400,
        _CONFLICT.format("foo"),
    ),
    ("page: an object", "GET", "/pages/{page}", "foo[a]=1", None, {}, 400, _OBJECT.format("foo")),
    ("search: an unknown key", "POST", "/search", "foo=1", {}, _JSON, 400, _foo("1")),
    (
        "search: a page size in the query string",
        "POST",
        "/search",
        "page_size=1",
        {},
        _JSON,
        400,
        _absent("query", "page_size", "1"),
    ),
    (
        "search: the query string ahead of the body",
        "POST",
        "/search",
        "foo=1",
        {"page_size": "x"},
        _JSON,
        400,
        _foo("1"),
    ),
    # a body on a `GET`
    (
        "a body member on users",
        "GET",
        "/users",
        "",
        b'{"a":1}',
        _JSON,
        400,
        _absent("body", "a", "1"),
    ),
    (
        "a page size in a GET's body",
        "GET",
        "/users/me",
        "",
        b'{"page_size":1}',
        _JSON,
        400,
        _absent("body", "page_size", "1"),
    ),
    (
        "a GET's array body",
        "GET",
        "/pages/{page}",
        "",
        b"[]",
        _JSON,
        400,
        "body failed validation: body should be an object, instead was `[]`.",
    ),
    (
        "a GET's body after its query string",
        "GET",
        "/users",
        "foo=1",
        b'{"a":1}',
        _JSON,
        400,
        _foo("1"),
    ),
    (
        "a GET's body ahead of the lookup",
        "GET",
        "/blocks/{rand}/children",
        "",
        b'{"a":1}',
        _JSON,
        400,
        _absent("body", "a", "1"),
    ),
    (
        "a GET's body after the credential",
        "GET",
        "/users",
        "",
        b'{"a":1}',
        {**_JSON, "Authorization": None},
        401,
        'Authorization header must use the format "Bearer <token>".',
    ),
    # the body as Express reads it, ahead of everything
    ("invalid JSON", "POST", "/search", "", b"{", _JSON, 400, _INVALID_JSON),
    (
        "invalid JSON with no credential",
        "POST",
        "/search",
        "",
        b"{",
        {**_JSON, "Authorization": None},
        400,
        _INVALID_JSON,
    ),
    (
        "invalid JSON with a token that does not resolve",
        "POST",
        "/search",
        "",
        b"{",
        {**_JSON, "Authorization": "Bearer ntn_bogus"},
        400,
        _INVALID_JSON,
    ),
    (
        "invalid JSON with no version",
        "POST",
        "/search",
        "",
        b"{",
        {**_JSON, "Notion-Version": None},
        400,
        _INVALID_JSON,
    ),
    (
        "invalid JSON with a version not published",
        "POST",
        "/search",
        "",
        b"{",
        {**_JSON, "Notion-Version": "banana"},
        400,
        _INVALID_JSON,
    ),
    (
        "invalid JSON ahead of the query string",
        "POST",
        "/search",
        "foo=1&foo=2",
        b"{",
        _JSON,
        400,
        _INVALID_JSON,
    ),
    (
        "invalid JSON on a path no route serves",
        "POST",
        "/nope",
        "",
        b"{",
        _JSON,
        400,
        _INVALID_JSON,
    ),
    ("invalid JSON on a GET", "GET", "/users", "", b"{", _JSON, 400, _INVALID_JSON),
    (
        "invalid JSON ahead of an unknown data source",
        "POST",
        "/data_sources/{rand}/query",
        "",
        b"{",
        _JSON,
        400,
        _INVALID_JSON,
    ),
    (
        "an array with no credential is the credential's",
        "POST",
        "/search",
        "",
        b"[]",
        {**_JSON, "Authorization": None},
        401,
        'Authorization header must use the format "Bearer <token>".',
    ),
    ("a scalar", "POST", "/search", "", b"null", _JSON, 400, _INVALID_JSON),
    ("a string", "POST", "/search", "", b'"x"', _JSON, 400, _INVALID_JSON),
    ("spaces", "POST", "/search", "", b"   ", _JSON, 400, _INVALID_JSON),
    ("NaN", "POST", "/search", "", b'{"page_size":NaN}', _JSON, 400, _INVALID_JSON),
    ("a trailing comma", "POST", "/search", "", b'{"foo":1,}', _JSON, 400, _INVALID_JSON),
    ("text after it", "POST", "/search", "", b"{} x", _JSON, 400, _INVALID_JSON),
    ("empty", "POST", "/search", "", b"", _JSON, 200, None),
    ("a byte-order mark", "POST", "/search", "", "﻿{}".encode(), _JSON, 200, None),
    (
        "an array",
        "POST",
        "/search",
        "",
        b"[1]",
        _JSON,
        400,
        "body failed validation: body should be an object, instead was `[1]`.",
    ),
    (
        "__proto__ is a member",
        "POST",
        "/search",
        "",
        b'{"__proto__":{"a":1}}',
        _JSON,
        400,
        _absent("body", "__proto__", '{"a":1}'),
    ),
    (
        "a member repeated, the last read",
        "POST",
        "/search",
        "",
        b'{"page_size":1,"page_size":"x"}',
        _JSON,
        400,
        'body failed validation: body.page_size should be a number or `undefined`, instead was `"x"`.',
    ),
    (
        "a parameter",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {"Content-Type": "application/json; foo=bar"},
        400,
        _absent("body", "foo", "1"),
    ),
    (
        "in another case",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {"Content-Type": "Application/Json; Charset=UTF-8"},
        400,
        _absent("body", "foo", "1"),
    ),
    (
        "text/plain is not read",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {"Content-Type": "text/plain"},
        200,
        None,
    ),
    (
        "text/plain invalid is not read",
        "POST",
        "/search",
        "",
        b"{",
        {"Content-Type": "text/plain"},
        200,
        None,
    ),
    ("no type is not read", "POST", "/search", "", b'{"foo":1}', {}, 200, None),
    (
        "merge-patch is not read",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {"Content-Type": "application/merge-patch+json"},
        200,
        None,
    ),
    (
        "text/json is not read",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {"Content-Type": "text/json"},
        200,
        None,
    ),
    (
        "a type that does not parse is not read",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {"Content-Type": "application/json;"},
        200,
        None,
    ),
    (
        "an empty charset is not read",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {"Content-Type": "application/json; charset="},
        200,
        None,
    ),
    (
        "latin1",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {"Content-Type": "application/json; charset=latin1"},
        415,
        _UNKNOWN_API,
    ),
    (
        "utf8",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {"Content-Type": "application/json; charset=utf8"},
        415,
        _UNKNOWN_API,
    ),
    (
        "utf-nope",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {"Content-Type": "application/json; charset=utf-nope"},
        415,
        _UNKNOWN_API,
    ),
    (
        "latin1 with no credential",
        "POST",
        "/search",
        "",
        b"{}",
        {"Content-Type": "application/json; charset=latin1", "Authorization": None},
        415,
        _UNKNOWN_API,
    ),
    (
        "latin1 on text/plain is not read",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {"Content-Type": "text/plain; charset=latin1"},
        200,
        None,
    ),
    (
        "UTF-8 read as utf-16le",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {"Content-Type": "application/json;charset=utf-16le"},
        400,
        _INVALID_JSON,
    ),
    (
        "utf-16le with its mark",
        "POST",
        "/search",
        "",
        "﻿".encode("utf-16-le") + '{"foo":1}'.encode("utf-16-le"),
        {"Content-Type": "application/json; charset=utf-16le"},
        400,
        _absent("body", "foo", "1"),
    ),
    (
        "gzip",
        "POST",
        "/search",
        "",
        gzip.compress(b'{"foo":1}'),
        {**_JSON, "Content-Encoding": "gzip"},
        400,
        _absent("body", "foo", "1"),
    ),
    (
        "deflate",
        "POST",
        "/search",
        "",
        zlib.compress(b'{"foo":1}'),
        {**_JSON, "Content-Encoding": "deflate"},
        400,
        _absent("body", "foo", "1"),
    ),
    (
        "an encoding not read",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {**_JSON, "Content-Encoding": "bogus"},
        415,
        _UNKNOWN_API,
    ),
    (
        "gzip that is not",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {**_JSON, "Content-Encoding": "gzip"},
        400,
        _UNKNOWN_API,
    ),
    (
        "br, which this parser does not read",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {**_JSON, "Content-Encoding": "br"},
        415,
        _UNKNOWN_API,
    ),
    (
        "an empty encoding, then gzip",
        "POST",
        "/search",
        "",
        gzip.compress(b'{"foo":1}'),
        [
            ("Content-Type", "application/json"),
            ("Content-Encoding", ""),
            ("Content-Encoding", "gzip"),
        ],
        400,
        _absent("body", "foo", "1"),
    ),
    (
        "gzip, then an empty encoding",
        "POST",
        "/search",
        "",
        gzip.compress(b'{"foo":1}'),
        [
            ("Content-Type", "application/json"),
            ("Content-Encoding", "gzip"),
            ("Content-Encoding", ""),
        ],
        415,
        _UNKNOWN_API,
    ),
    (
        "identity twice",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        [
            ("Content-Type", "application/json"),
            ("Content-Encoding", "identity"),
            ("Content-Encoding", "identity"),
        ],
        415,
        _UNKNOWN_API,
    ),
    (
        "gzip between a space and a tab",
        "POST",
        "/search",
        "",
        gzip.compress(b'{"foo":1}'),
        {**_JSON, "Content-Encoding": " gzip\t"},
        400,
        _absent("body", "foo", "1"),
    ),
    (
        "an empty type, then JSON",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        [("Content-Type", ""), ("Content-Type", "application/json")],
        400,
        _absent("body", "foo", "1"),
    ),
    (
        "JSON, then an empty type",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        [("Content-Type", "application/json"), ("Content-Type", "")],
        200,
        None,
    ),
    ("an empty type", "POST", "/search", "", b'{"foo":1}', {"Content-Type": ""}, 200, None),
    (
        "utf-16be",
        "POST",
        "/search",
        "",
        '{"foo":1}'.encode("utf-16-be"),
        {"Content-Type": "application/json; charset=utf-16be"},
        400,
        _absent("body", "foo", "1"),
    ),
    (
        "a charset it does not know, past the limit",
        "POST",
        "/search",
        "",
        b'{"query":"' + b"a" * 600000 + b'"}',
        {"Content-Type": "application/json; charset=utf-nope"},
        413,
        _TOO_LARGE,
    ),
    (
        "a list of a small number, quoted",
        "POST",
        "/search",
        "",
        b'{"query":[0.000001]}',
        _JSON,
        400,
        _NOT_A_STRING + "`[0.000001]`.",
    ),
    (
        "an object of numbers, quoted",
        "POST",
        "/search",
        "",
        b'{"query":{"a":1e-7,"b":[1.5e300]}}',
        _JSON,
        400,
        _NOT_A_STRING + '`{"a":1e-7,"b":[1.5e+300]}`.',
    ),
    (
        "a list of large and signed numbers, quoted",
        "POST",
        "/search",
        "",
        b'{"query":[1e16,1e21,-0.0,5]}',
        _JSON,
        400,
        _NOT_A_STRING + "`[10000000000000000,1e+21,0,5]`.",
    ),
    (
        "page: no credential, and a key that is an object",
        "GET",
        "/pages/{page}",
        "foo[a]=1",
        None,
        {"Authorization": None},
        401,
        _NOT_A_BEARER,
    ),
    (
        "page: no version, and a key that is an object",
        "GET",
        "/pages/{page}",
        "foo[a]=1",
        None,
        {"Notion-Version": None},
        400,
        _NO_VERSION,
    ),
    (
        "children: no credential, and a cursor not a uuid",
        "GET",
        "/blocks/{page}/children",
        "start_cursor=bogus",
        None,
        {"Authorization": None},
        401,
        _NOT_A_BEARER,
    ),
    (
        "children: no version, and a cursor not a uuid",
        "GET",
        "/blocks/{page}/children",
        "start_cursor=bogus",
        None,
        {"Notion-Version": None},
        400,
        _NO_VERSION,
    ),
    (
        "gzip inflating past the limit",
        "POST",
        "/search",
        "",
        gzip.compress(b'{"query":"' + b"a" * 520000 + b'"}'),
        {**_JSON, "Content-Encoding": "gzip"},
        413,
        _TOO_LARGE,
    ),
    (
        "two types are not read",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        [("Content-Type", "application/json"), ("Content-Type", "text/plain")],
        200,
        None,
    ),
    (
        "two types the other way round are not read",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        [("Content-Type", "text/plain"), ("Content-Type", "application/json")],
        200,
        None,
    ),
    (
        "one type twice is not read",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        [("Content-Type", "application/json"), ("Content-Type", "application/json")],
        200,
        None,
    ),
    (
        "a space and a tab after the type",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {"Content-Type": "application/json \t"},
        400,
        _absent("body", "foo", "1"),
    ),
    (
        "a no-break space after the type",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {"Content-Type": "application/json\xa0"},
        200,
        None,
    ),
    (
        "0x85 after the type",
        "POST",
        "/search",
        "",
        b'{"foo":1}',
        {"Content-Type": "application/json\x85"},
        200,
        None,
    ),
    ("the root, invalid JSON", "POST", "/notion", "", b"{", _JSON, 400, _INVALID_JSON),
    (
        "the root, a charset it cannot read",
        "POST",
        "/notion",
        "",
        b"{",
        {"Content-Type": "application/json; charset=latin1"},
        415,
        _UNKNOWN_API,
    ),
    (
        "512000 bytes",
        "POST",
        "/search",
        "",
        b'{"query":"' + b"a" * 511988 + b'"}',
        _JSON,
        200,
        None,
    ),
    (
        "512001 bytes",
        "POST",
        "/search",
        "",
        b'{"query":"' + b"a" * 511989 + b'"}',
        _JSON,
        413,
        _TOO_LARGE,
    ),
    (
        "512001 bytes with no credential",
        "POST",
        "/search",
        "",
        b'{"query":"' + b"a" * 511989 + b'"}',
        {**_JSON, "Authorization": None},
        413,
        _TOO_LARGE,
    ),
    # a data source query's query string and body, ahead of its lookup
    ("query: an unknown key", "POST", _Q, "foo=1", {}, _JSON, 400, _foo("1")),
    (
        "query: a cursor in the query string",
        "POST",
        _Q,
        "start_cursor=x",
        {},
        _JSON,
        400,
        _absent("query", "start_cursor", '"x"'),
    ),
    (
        "query: an unknown key ahead of an unknown data source",
        "POST",
        "/data_sources/{rand}/query",
        "foo=1",
        {},
        _JSON,
        400,
        _foo("1"),
    ),
    (
        "query: a member it does not take",
        "POST",
        _Q,
        "",
        {"foo": 1},
        _JSON,
        400,
        _absent("body", "foo", "1"),
    ),
    (
        "query: in_trash, which search takes",
        "POST",
        _Q,
        "",
        {"in_trash": True},
        _JSON,
        400,
        _absent("body", "in_trash", "true"),
    ),
    (
        "query: page size 0",
        "POST",
        _Q,
        "",
        {"page_size": 0},
        _JSON,
        400,
        "body failed validation: body.page_size should be ≥ `1` or `undefined`, instead was `0`.",
    ),
    (
        "query: page size -1",
        "POST",
        _Q,
        "",
        {"page_size": -1},
        _JSON,
        400,
        "body failed validation: body.page_size should be ≥ `1` or `undefined`, instead was `-1`.",
    ),
    (
        "query: page size 1.5",
        "POST",
        _Q,
        "",
        {"page_size": 1.5},
        _JSON,
        400,
        "body failed validation: body.page_size should be an integer or `undefined`, instead was `1.5`.",
    ),
    (
        "query: page size 1e400",
        "POST",
        _Q,
        "",
        b'{"page_size":1e400}',
        _JSON,
        400,
        "body failed validation: body.page_size should be an integer or `undefined`, instead was `null`.",
    ),
    (
        "query: page size a string",
        "POST",
        _Q,
        "",
        {"page_size": "5"},
        _JSON,
        400,
        'body failed validation: body.page_size should be a number or `undefined`, instead was `"5"`.',
    ),
    (
        "query: page size true",
        "POST",
        _Q,
        "",
        {"page_size": True},
        _JSON,
        400,
        "body failed validation: body.page_size should be a number or `undefined`, instead was `true`.",
    ),
    (
        "query: a result type it does not know",
        "POST",
        _Q,
        "",
        {"result_type": "bogus"},
        _JSON,
        400,
        'body failed validation: body.result_type should be `"page"`, `"data_source"`, or `undefined`, instead was `"bogus"`.',
    ),
    (
        "query: a filter not an object",
        "POST",
        _Q,
        "",
        {"filter": "x"},
        _JSON,
        400,
        'body failed validation: body.filter should be an object or `undefined`, instead was `"x"`.',
    ),
    (
        "query: an empty filter",
        "POST",
        _Q,
        "",
        {"filter": {}},
        _JSON,
        400,
        _defined("body.filter", _TOP),
    ),
    (
        "query: a filter of a property alone",
        "POST",
        _Q,
        "",
        {"filter": {"property": "Nope"}},
        _JSON,
        400,
        _defined("body.filter", _TOP),
    ),
    (
        "query: an empty condition in a compound",
        "POST",
        _Q,
        "",
        {"filter": {"and": [{}]}},
        _JSON,
        400,
        _defined("body.filter.and[0]", _INSIDE),
    ),
    (
        "query: an empty condition two compounds deep",
        "POST",
        _Q,
        "",
        {"filter": {"and": [{"or": [{}]}]}},
        _JSON,
        400,
        _defined("body.filter.and[0].or[0]", _FILTER_TYPES),
    ),
    (
        "query: a timestamp's filter with neither name in a compound",
        "POST",
        _Q,
        "",
        {"filter": {"and": [{"created_time": {"after": "2020-01-01"}}]}},
        _JSON,
        400,
        _defined("body.filter.and[0]", _TIMESTAMP_INSIDE),
    ),
    (
        "query: a compound not an array",
        "POST",
        _Q,
        "",
        {"filter": {"and": "x"}},
        _JSON,
        400,
        _defined("body.filter", _TOP).replace(
            "body.filter.and should be defined, instead was `undefined`.",
            'body.filter.and should be an array, instead was `"x"`.',
        ),
    ),
    (
        "query: a condition not an object",
        "POST",
        _Q,
        "",
        {"filter": {"property": 1, "number": "x"}},
        _JSON,
        400,
        _defined("body.filter", _TOP).replace(
            "body.filter.number should be defined, instead was `undefined`.",
            'body.filter.number should be an object, instead was `"x"`.',
        ),
    ),
    (
        "query: a property that is a number",
        "POST",
        _Q,
        "",
        {"filter": {"property": 1, "number": {"equals": 1}}},
        _JSON,
        400,
        _defined("body.filter", _TOP).replace(
            "body.filter.number should be defined, instead was `undefined`.",
            "body.filter.property should be a string, instead was `1`.",
        ),
    ),
    (
        "query: two filter types",
        "POST",
        _Q,
        "",
        {"filter": {"title": {"equals": "x"}, "number": {"equals": 1}, "property": "Priority"}},
        _JSON,
        400,
        _defined("body.filter", _TOP)
        .replace(
            "body.filter.title should be defined, instead was `undefined`.",
            'body.filter.number should be not present, instead was `{"equals":1}`.',
        )
        .replace(
            "body.filter.number should be defined, instead was `undefined`.",
            'body.filter.title should be not present, instead was `{"equals":"x"}`.',
        ),
    ),
    (
        "query: a compound's own property is read by nothing",
        "POST",
        _Q,
        "",
        {"filter": {"and": [], "property": "Nope"}},
        _JSON,
        200,
        None,
    ),
    (
        "query: sorts not an array",
        "POST",
        _Q,
        "",
        {"sorts": "x"},
        _JSON,
        400,
        'body failed validation: body.sorts should be an array or `undefined`, instead was `"x"`.',
    ),
    (
        "query: a sort not an object",
        "POST",
        _Q,
        "",
        {"sorts": ["x"]},
        _JSON,
        400,
        'body failed validation: body.sorts[0] should be an object, instead was `"x"`.',
    ),
    (
        "query: the second sort not an object",
        "POST",
        _Q,
        "",
        {"sorts": [{"property": "Priority", "direction": "ascending"}, "x"]},
        _JSON,
        400,
        'body failed validation: body.sorts[1] should be an object, instead was `"x"`.',
    ),
    (
        "query: an empty sort, the first named",
        "POST",
        _Q,
        "",
        {"sorts": [{}, {}]},
        _JSON,
        400,
        _BODY_FIX
        + "body.sorts[0].property should be defined, instead was `undefined`.\nbody.sorts[0].timestamp should be defined, instead was `undefined`.",
    ),
    (
        "query: a sort with no direction",
        "POST",
        _Q,
        "",
        {"sorts": [{"property": "Nope"}]},
        _JSON,
        400,
        _BODY_FIX
        + "body.sorts[0].direction should be defined, instead was `undefined`.\nbody.sorts[0].timestamp should be defined, instead was `undefined`.",
    ),
    (
        "query: a timestamp sort with no direction",
        "POST",
        _Q,
        "",
        {"sorts": [{"timestamp": "created_time"}]},
        _JSON,
        400,
        _BODY_FIX
        + "body.sorts[0].property should be defined, instead was `undefined`.\nbody.sorts[0].direction should be defined, instead was `undefined`.",
    ),
    (
        "query: a timestamp it does not sort by",
        "POST",
        _Q,
        "",
        {"sorts": [{"timestamp": "bogus", "direction": "ascending"}]},
        _JSON,
        400,
        _BODY_FIX
        + 'body.sorts[0].property should be defined, instead was `undefined`.\nbody.sorts[0].timestamp should be `"created_time"` or `"last_edited_time"`, instead was `"bogus"`.',
    ),
    (
        "query: a direction it does not have",
        "POST",
        _Q,
        "",
        {"sorts": [{"property": "Priority", "direction": "up"}]},
        _JSON,
        400,
        _BODY_FIX
        + 'body.sorts[0].direction should be `"ascending"` or `"descending"`, instead was `"up"`.\nbody.sorts[0].timestamp should be defined, instead was `undefined`.',
    ),
    (
        "query: a member beside a sort",
        "POST",
        _Q,
        "",
        {"sorts": [{"property": "Priority", "direction": "ascending", "foo": 1}]},
        _JSON,
        200,
        None,
    ),
    (
        "query: a sort by a timestamp",
        "POST",
        _Q,
        "",
        {"sorts": [{"timestamp": "created_time", "direction": "ascending"}]},
        _JSON,
        200,
        None,
    ),
    (
        "query: a filter on a property it has",
        "POST",
        _Q,
        "",
        {"filter": {"property": "Priority", "select": {"equals": "x"}}},
        _JSON,
        200,
        None,
    ),
    (
        "query under 2022-06-28, with a key it refuses",
        "POST",
        _Q,
        "foo=1",
        {},
        {**_JSON, "Notion-Version": "2022-06-28"},
        400,
        "Invalid request URL.",
    ),
    (
        "query under 2022-06-28, with a body it refuses",
        "POST",
        _Q,
        "",
        {"page_size": "x"},
        {**_JSON, "Notion-Version": "2022-06-28"},
        400,
        "Invalid request URL.",
    ),
    (
        "a database query under 2025-09-03, with a key it refuses",
        "POST",
        "/databases/{db}/query",
        "foo=1",
        {},
        _JSON,
        400,
        "Invalid request URL.",
    ),
    (
        "a data source under 2022-06-28, with a key it refuses",
        "GET",
        "/data_sources/{ds}",
        "foo=1",
        None,
        {"Notion-Version": "2022-06-28"},
        400,
        "Invalid request URL.",
    ),
    (
        "query: sorts ahead of filter",
        "POST",
        _Q,
        "",
        {"filter": "x", "sorts": "x"},
        _JSON,
        400,
        'body failed validation: body.sorts should be an array or `undefined`, instead was `"x"`.',
    ),
    (
        "query: filter ahead of the page size",
        "POST",
        _Q,
        "",
        {"page_size": "x", "filter": "x"},
        _JSON,
        400,
        'body failed validation: body.filter should be an object or `undefined`, instead was `"x"`.',
    ),
    (
        "query: the cursor ahead of the page size",
        "POST",
        _Q,
        "",
        {"page_size": "x", "start_cursor": 5},
        _JSON,
        400,
        "body failed validation: body.start_cursor should be a string or `undefined`, instead was `5`.",
    ),
    (
        "query: the page size ahead of a cursor naming nothing",
        "POST",
        _Q,
        "",
        {"start_cursor": "bogus", "page_size": "x"},
        _JSON,
        400,
        'body failed validation: body.page_size should be a number or `undefined`, instead was `"x"`.',
    ),
    (
        "query: a body's schema ahead of the lookup",
        "POST",
        "/data_sources/{rand}/query",
        "",
        {"sorts": [{}]},
        _JSON,
        400,
        _BODY_FIX
        + "body.sorts[0].property should be defined, instead was `undefined`.\nbody.sorts[0].timestamp should be defined, instead was `undefined`.",
    ),
    (
        "query: a cursor after the lookup",
        "POST",
        "/data_sources/{rand}/query",
        "",
        {"start_cursor": "bogus"},
        _JSON,
        404,
        None,
    ),
    (
        "query: a property after the lookup",
        "POST",
        "/data_sources/{rand}/query",
        "",
        {"filter": {"property": "Nope", "title": {"equals": "x"}}},
        _JSON,
        404,
        None,
    ),
    (
        "query: filter_properties after the lookup",
        "POST",
        "/data_sources/{rand}/query",
        "filter_properties=x",
        {},
        _JSON,
        404,
        None,
    ),
    # after the lookup
    (
        "query: a sort on a property it does not have",
        "POST",
        _Q,
        "",
        {"sorts": [{"property": "Nope", "direction": "ascending"}]},
        _JSON,
        400,
        "Could not find sort property with name or id: Nope",
    ),
    (
        "query: a filter on a property it does not have",
        "POST",
        _Q,
        "",
        {"filter": {"property": "Nope", "title": {"equals": "x"}}},
        _JSON,
        400,
        "Could not find property with name or id: Nope",
    ),
    (
        "query: the sort's property ahead of the filter's",
        "POST",
        _Q,
        "",
        {
            "sorts": [{"property": "Nope", "direction": "ascending"}],
            "filter": {"property": "Nope2", "title": {"equals": "x"}},
        },
        _JSON,
        400,
        "Could not find sort property with name or id: Nope",
    ),
    (
        "query: a property under `or`",
        "POST",
        _Q,
        "",
        {"filter": {"or": [{"property": "Nope", "number": {"equals": 1}}]}},
        _JSON,
        400,
        "Could not find property with name or id: Nope",
    ),
    (
        "query: `and` ahead of `or`",
        "POST",
        _Q,
        "",
        {
            "filter": {
                "or": [{"property": "Nope2", "title": {"equals": "y"}}],
                "and": [{"property": "Nope", "title": {"equals": "x"}}],
            }
        },
        _JSON,
        400,
        "Could not find property with name or id: Nope",
    ),
    (
        "query: the filter's property ahead of the cursor",
        "POST",
        _Q,
        "",
        {"start_cursor": "bogus", "filter": {"property": "Nope", "title": {"equals": "x"}}},
        _JSON,
        400,
        "Could not find property with name or id: Nope",
    ),
    (
        "query: a cursor naming nothing",
        "POST",
        _Q,
        "",
        {"start_cursor": "bogus"},
        _JSON,
        400,
        _INVALID + "bogus",
    ),
    (
        "query: the cursor ahead of filter_properties",
        "POST",
        _Q,
        "filter_properties=x",
        {"start_cursor": "bogus"},
        _JSON,
        400,
        _INVALID + "bogus",
    ),
    (
        "query: filter_properties naming nothing",
        "POST",
        _Q,
        "filter_properties=title&filter_properties=Nope",
        {},
        _JSON,
        400,
        "The schema for the data source with the following ID is malformed: {ds}. The provided `filter_properties` contains an invalid attribute: Nope.",
    ),
    (
        "query: no rows of a data source's",
        "POST",
        _Q,
        "",
        {"result_type": "data_source", "start_cursor": "bogus"},
        _JSON,
        400,
        _INVALID + "bogus",
    ),
    # 2022-06-28's database query reads the same body
    (
        "database query: page size 1.5",
        "POST",
        "/databases/{db}/query",
        "",
        {"page_size": 1.5},
        {**_JSON, "Notion-Version": "2022-06-28"},
        400,
        "body failed validation: body.page_size should be an integer or `undefined`, instead was `1.5`.",
    ),
    (
        "database query: an unknown key",
        "POST",
        "/databases/{db}/query",
        "foo=1",
        {},
        {**_JSON, "Notion-Version": "2022-06-28"},
        400,
        _foo("1"),
    ),
    (
        "database query: filter_properties naming nothing",
        "POST",
        "/databases/{db}/query",
        "filter_properties=x",
        {},
        {**_JSON, "Notion-Version": "2022-06-28"},
        400,
        "The schema for the data source with the following ID is malformed: {ds}. The provided `filter_properties` contains an invalid attribute: x.",
    ),
]


def test_notion_reads_a_request_as_real_does(client, notion_h):
    """Real's answer to each request: the body Express reads first (``notion_request.read_body``),
    then the credential, the version and whether the version mounts the route, then the query
    string and the body against the route's schema, then what names a property or a row."""
    ids = {
        "page": _RUNBOOK,
        "ds": synth.notion_data_source_id("nt-tasks-db"),
        "db": synth.notion_id("nt-tasks-db"),
        "user": client.get("/notion/v1/users", headers=notion_h).json()["results"][0]["id"],
        "rand": _RAND,
    }
    answered, wanted = {}, {}
    for label, method, path, query, send, headers, status, expected in _REQUEST_ROWS:
        if isinstance(headers, list):  # a header sent more than once
            sent = [*notion_h.items(), *headers]
        else:
            sent = {**notion_h, **{k: v for k, v in headers.items() if v is not None}}
            for k, v in headers.items():
                if v is None:
                    sent.pop(k, None)
        full = path if path == "/notion" else f"/notion/v1{path.format(**ids)}"
        url = full + (f"?{query.format(**ids)}" if query else "")
        content = json.dumps(send).encode() if isinstance(send, dict) else send
        r = sent_as(client, method, url, sent, content)
        body = r.json()
        row = (method, path, label)
        if isinstance(expected, dict):
            members = ["isNotionError", "errorId", *(k for k in expected if k != "isNotionError")]
            answered[row] = (r.status_code, list(body), {k: body.get(k) for k in expected})
            wanted[row] = (status, members, expected)
        elif isinstance(expected, str):
            answered[row] = (r.status_code, body.get("message"))
            wanted[row] = (status, expected.replace("{ds}", ids["ds"]))
        else:
            answered[row], wanted[row] = r.status_code, status
    assert answered == wanted


# --- where a page starts, and how long it is ---------------------------------------------------

_PAGING_RECORDS = [
    {
        "source_type": "notion",
        "doc_id": "np-page",
        "title": "Paged",
        "content": "one\n\ntwo\n\nthree",
        "author_email": "ava@acme.com",
        "visibility": "public",
        "comments": [
            {"body": "first", "author_email": "ava@acme.com"},
            {"body": "second", "author_email": "bob@acme.com"},
            {"body": "third", "author_email": "carol@acme.com"},
        ],
    },
    {
        "source_type": "notion",
        "doc_id": "np-db",
        "subtype": "database",
        "title": "Rows",
        "content": "Tracker",
        "author_email": "ava@acme.com",
        "visibility": "public",
        "properties": {"Status": {"type": "select"}},
    },
    *(
        {
            "source_type": "notion",
            "doc_id": f"np-row-{n}",
            "parent": "np-db",
            "title": f"Row {n}",
            "content": "body",
            "author_email": "ava@acme.com",
            "visibility": "public",
            "properties": {"Status": "Open"},
        }
        for n in (1, 2, 3)
    ),
]


@pytest.fixture(scope="module")
def paging_corpus(tmp_path_factory):
    """`_PAGING_RECORDS` built."""
    return tiny_corpus(tmp_path_factory.mktemp("paging_corpus"), _PAGING_RECORDS)


def _lister(route: str):
    """How to ask one list for a page: a function of the client, the headers and the paging members,
    answering the response."""
    page = synth.notion_id("np-page")
    ds = synth.notion_data_source_id("np-db")
    if route == "users":
        return lambda c, h, m: c.get("/notion/v1/users", params=m, headers=h)
    if route == "children":
        return lambda c, h, m: c.get(f"/notion/v1/blocks/{page}/children", params=m, headers=h)
    if route == "comments":
        return lambda c, h, m: c.get(
            "/notion/v1/comments", params={"block_id": page, **m}, headers=h
        )
    if route == "search":
        return lambda c, h, m: c.post("/notion/v1/search", json=m, headers=h)
    return lambda c, h, m: c.post(f"/notion/v1/data_sources/{ds}/query", json=m, headers=h)


@pytest.mark.parametrize("route", ["users", "children", "comments", "search", "query"])
def test_notion_a_cursor_starts_the_page_at_the_item_it_names(paging_corpus, route):
    """The page a cursor asks for starts with the item it names, and the cursor after a page is the
    next item's id (``routers.notion._start``). A cursor naming nothing is refused, `comments` with
    the three members real adds there."""
    ask = _lister(route)
    settings = paging_corpus
    with client_for(settings) as client:
        h = {
            "Authorization": f"Bearer {settings.admin_token}",
            "Notion-Version": DATA_SOURCES_VERSION,
        }
        ids = [x["id"] for x in ask(client, h, {}).json()["results"]]
        assert len(ids) >= 3, ids
        first = ask(client, h, {"page_size": 1}).json()
        assert [x["id"] for x in first["results"]] == ids[:1]
        assert first["has_more"] is True
        assert first["next_cursor"].endswith(ids[1])  # the query's names a session first
        second = ask(client, h, {"page_size": 1, "start_cursor": ids[1]}).json()
        assert [x["id"] for x in second["results"]] == ids[1:2]
        assert second["next_cursor"] == ids[2]
        refused = ask(client, h, {"start_cursor": "11111111-2222-4333-8444-555555555555"})
        assert refused.status_code == 400, refused.text
        assert refused.json()["message"] == (
            "The start_cursor provided is invalid: 11111111-2222-4333-8444-555555555555"
        )
        extra = refused.json().get("additional_data")
        assert extra == (
            {
                "tool_error_code": "invalid_cursor",
                "tool_error_class": "validation",
                "tool_error_retryable": "false",
            }
            if route == "comments"
            else None
        )


@pytest.mark.parametrize(
    "route, members, shown",
    [
        # the route, the members sent (`{2nd}` the second item's id), and the results shown: a
        # count, `all` of them, or which of the empty pages ``routers.notion._paged`` answers
        ("users", {"page_size": -1}, "empty"),
        ("users", {"page_size": -1, "start_cursor": "{2nd}"}, "empty at the 2nd"),
        ("users", {"page_size": "-1.0"}, "empty"),
        ("users", {"page_size": 0}, "all"),
        ("users", {"page_size": "-0"}, "all"),
        ("users", {"page_size": 101}, "all"),
        ("users", {"page_size": "0x2"}, 2),
        ("children", {"page_size": -5}, "empty"),
        ("children", {"page_size": " "}, "all"),
        ("search", {"page_size": 0.5}, "empty"),
        ("search", {"page_size": -0.5}, "empty"),
        ("search", {"page_size": 1.5}, 1),
        ("search", {"page_size": 2.5}, 2),
        ("search", {"page_size": 0}, "all"),
        ("search", {"page_size": 1e308}, "all"),
        ("search", {"filter": {"in_trash": True}}, 0),
        ("search", {"filter": {"in_trash": False}, "page_size": 2}, 2),
        ("query", {"page_size": 101}, "all"),
        ("query", {"result_type": "data_source"}, 0),
        ("search", {"query": "zzzqqq", "page_size": -1}, "nothing"),
    ],
)
def test_notion_a_page_is_as_long_as_real_reads_its_page_size(paging_corpus, route, members, shown):
    """Real's page for each `page_size`, as ``routers.notion._size`` reads it; the trash holds
    nothing here, and a data source holds no data source."""
    ask = _lister(route)
    settings = paging_corpus
    with client_for(settings) as client:
        h = {
            "Authorization": f"Bearer {settings.admin_token}",
            "Notion-Version": DATA_SOURCES_VERSION,
        }
        everything = [x["id"] for x in ask(client, h, {}).json()["results"]]
        sent = {k: everything[1] if v == "{2nd}" else v for k, v in members.items()}
        r = ask(client, h, sent)
        assert r.status_code == 200, r.text
        page = r.json()
        if shown in ("empty", "empty at the 2nd", "nothing"):
            named = {"empty": everything[:1], "empty at the 2nd": everything[1:2]}.get(
                shown, [None]
            )
            assert page["results"] == []
            assert (page["next_cursor"], page["has_more"]) == (named[0], shown != "nothing")
        else:
            assert [x["id"] for x in page["results"]] == everything[
                : len(everything) if shown == "all" else shown
            ]


def test_notion_data_source_query_reads_its_cursor_as_real_does(paging_corpus):
    """Each spelling of a data source query's cursor, as ``routers.notion._row_start`` reads it:
    where the page starts, or what the refusal names, and the cursor after it."""
    ds = synth.notion_data_source_id("np-db")
    settings = paging_corpus
    with client_for(settings) as client:
        h = {
            "Authorization": f"Bearer {settings.admin_token}",
            "Notion-Version": DATA_SOURCES_VERSION,
        }

        def ask(cursor):
            body = {"page_size": 1} if cursor is None else {"page_size": 1, "start_cursor": cursor}
            return client.post(f"/notion/v1/data_sources/{ds}/query", json=body, headers=h)

        first = ask(None).json()
        rows = [
            r["id"]
            for r in client.post(f"/notion/v1/data_sources/{ds}/query", json={}, headers=h).json()[
                "results"
            ]
        ]
        issued = first["next_cursor"].split(":")[1]
        assert first["next_cursor"] == f"s:{issued}:{rows[1]}"
        dashless = rows[1].replace("-", "")
        cases = [
            # the cursor, then the row the page starts at and the cursor after it, or the refusal's
            # name for the cursor
            ("", (0, f"s:{issued}:{rows[1]}")),
            (rows[1], (1, rows[2])),
            (f"s:{issued}:{rows[1]}", (1, f"s:{issued}:{rows[2]}")),
            (f"s:{issued}:{rows[2]}", (2, None)),
            (f"s:{issued}:{dashless}", dashless),
            (f"s:{issued}:y", "y"),
            (f"s:fresh:{dashless}", (1, f"s:fresh:{rows[2]}")),
            (f"s:fresh:{rows[1].upper()}", (0, f"s:fresh:{rows[1]}")),
            (f"s:fresh:{dashless.upper()}", (0, f"s:fresh:{rows[1]}")),
            ("s:fresh:y", (0, f"s:fresh:{rows[1]}")),
            (f"s:fresh:b:{rows[1]}", (0, f"s:fresh:{rows[1]}")),
            ("s:", "s:"),
            ("s:x:", "s:x:"),
            (f"s::{rows[1]}", f"s::{rows[1]}"),
            (f"S:x:{rows[1]}", f"S:x:{rows[1]}"),
            (dashless, dashless),
            (rows[1].upper(), rows[1].upper()),
        ]
        for cursor, want in cases:
            r = ask(cursor)
            if isinstance(want, str):
                assert r.status_code == 400, (cursor, r.text)
                assert r.json()["message"] == f"The start_cursor provided is invalid: {want}"
            else:
                assert r.status_code == 200, (cursor, r.text)
                page = r.json()
                assert ([x["id"] for x in page["results"]], page["next_cursor"]) == (
                    [rows[want[0]]],
                    want[1],
                ), cursor


def test_notion_a_cursor_naming_a_page_the_caller_cannot_see_names_nothing(
    client, notion_h, tokens
):
    """A search cursor naming a page the caller cannot see is refused as a cursor naming nothing,
    so a refusal tells an outsider nothing the page's absence from the results did not. The admin's
    page starting at it is the control."""
    seen = client.post("/notion/v1/search", json={"start_cursor": _SECRET}, headers=notion_h)
    assert seen.status_code == 200 and seen.json()["results"][0]["id"] == _SECRET
    outsider = {**notion_h, "Authorization": f"Bearer {tokens['ava@acme.com']}"}
    hidden = client.post("/notion/v1/search", json={"start_cursor": _SECRET}, headers=outsider)
    made_up = client.post("/notion/v1/search", json={"start_cursor": _RAND}, headers=outsider)
    assert hidden.status_code == made_up.status_code == 400
    assert hidden.json()["message"].replace(_SECRET, _RAND) == made_up.json()["message"]


@pytest.mark.parametrize("value", ["page", "data_source", None])
def test_notion_search_fills_a_page_of_the_object_type_it_filters_to(
    paging_corpus, monkeypatch, value
):
    """A page of one object type, or of any with no filter, holds the next results and names the
    one after it, an empty page names the first, and a cursor starts at its result wherever that
    sits in the corpus. With the window ``routers.notion._OVERFETCH`` sizes cut to the page and one
    more, the corpus's one database and four pages leave at least one of the two filters short of a
    page in the first window, so the search has to read on, and the last result lies past it."""
    from backlot.routers import notion as notion_routes

    monkeypatch.setattr(notion_routes, "_OVERFETCH", 1)
    settings = paging_corpus
    with client_for(settings) as client:
        h = {
            "Authorization": f"Bearer {settings.admin_token}",
            "Notion-Version": DATA_SOURCES_VERSION,
        }
        flt = {"filter": {"property": "object", "value": value}} if value else {}
        every = client.post("/notion/v1/search", json=flt, headers=h).json()["results"]
        assert {x["object"] for x in every} == ({value} if value else {"page", "data_source"})
        ids = [x["id"] for x in every]
        page = client.post("/notion/v1/search", json={**flt, "page_size": 1}, headers=h).json()
        assert [x["id"] for x in page["results"]] == ids[:1]
        assert page["next_cursor"] == (ids[1] if len(ids) > 1 else None)
        empty = client.post("/notion/v1/search", json={**flt, "page_size": -1}, headers=h).json()
        assert (empty["results"], empty["next_cursor"]) == ([], ids[0])
        last = client.post(
            "/notion/v1/search",
            json={**flt, "page_size": 1, "start_cursor": ids[-1]},
            headers=h,
        ).json()
        assert ([x["id"] for x in last["results"]], last["next_cursor"]) == (ids[-1:], None)


@pytest.mark.parametrize(
    "version, by, starts",
    [
        # the version, which id the cursor names the database by, and whether the page starts there
        ("2022-06-28", "database", True),
        ("2022-06-28", "data_source", True),
        (DATA_SOURCES_VERSION, "data_source", True),
        (DATA_SOURCES_VERSION, "database", False),
    ],
)
def test_notion_search_starts_at_a_database_by_the_ids_its_version_reads(
    paging_corpus, version, by, starts
):
    """Real's search starts at a database named by the id each version lists it under, and under
    2022-06-28 by its data source's too."""
    db, ds = synth.notion_id("np-db"), synth.notion_data_source_id("np-db")
    listed = ds if version == DATA_SOURCES_VERSION else db
    settings = paging_corpus
    with client_for(settings) as client:
        h = {"Authorization": f"Bearer {settings.admin_token}", "Notion-Version": version}
        body = {"start_cursor": db if by == "database" else ds, "page_size": 1}
        r = client.post("/notion/v1/search", json=body, headers=h)
        if starts:
            assert r.status_code == 200, r.text
            assert r.json()["results"][0]["id"] == listed
        else:
            assert r.status_code == 400 and r.json()["message"].endswith(db)


_JS_TRIMMED = [
    # a `query` of each, and whether real answered every result or none
    ("\n", True),
    ("\t", True),
    (" ", True),
    ("\u00a0", True),
    ("\u3000", True),
    ("\u2028", True),
    ("\ufeff", True),
    ("\x1c", False),
    ("\x85", False),
    ("\u200b", False),
]


def test_notion_search_trims_its_query_as_javascript_does(client, notion_h):
    """A `query` of nothing but what JavaScript trims (``notion_request.JS_SPACE``) is no query
    at all, and anything else is searched for, which finds nothing here."""
    everything = client.post("/notion/v1/search", json={}, headers=notion_h).json()["results"]
    answered = {}
    for text, _ in _JS_TRIMMED:
        r = client.post("/notion/v1/search", json={"query": text}, headers=notion_h)
        answered[text] = (r.status_code, r.json().get("results"))
    assert answered == {text: (200, everything if every else []) for text, every in _JS_TRIMMED}
