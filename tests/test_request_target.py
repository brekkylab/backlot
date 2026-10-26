"""The raw bytes ``backlot.request_target`` answers, over a real uvicorn.

A client library percent-encodes such a byte or refuses to send it, and the TestClient would too,
so each request here is written to a socket by hand, to the server ``live_server`` starts with
``backlot serve``. A single-byte row names the vendor host whose measured answer it holds; a row
of `_TARGETS` names its path, measured on the host that path is.
"""

from __future__ import annotations

import re
import select
import socket
import time
import urllib.parse

import pytest

from backlot import errors, request_target

_UTF8, _LONE = b"\xc3\xa9", b"\x80"
_UVICORN = b"Invalid HTTP request received."
_KINDS = {
    "utf-8": _UTF8,
    "lone": _LONE,
    "control": b"\x01",
    "nul": b"\x00",
    "tab": b"\x09",
    "cr": b"\x0d",
    "space": b" ",
    "del": b"\x7f",
    "ff": b"\xff",
}
#: The kinds whose answer is another's: 0x7F is a `control`, 0xFF a lone `high`.
_AS = {"del": "control", "ff": "lone"}

# What the front door answers each kind with, in the path and in a query value. `encoded` is the
# answer to the request with the byte percent-encoded, `?` the one with each high byte in the path
# read as a `?` and the other bytes percent-encoded, and ` closed` after either that answer with the
# connection closed after it.
_GOOGLE = dict.fromkeys(("utf-8", "lone"), "encoded") | {
    "control": "google 54",
    "nul": "google 54",
    "tab": "google 1555",
    "cr": "google 1555",
    "space": "google 1555",
}
_ATLASSIAN = {
    "utf-8": "encoded",
    "lone": "encoded",
    "control": "edge invalid_characters",
    "nul": "cloudfront",
    "tab": "edge codec_error",
    "cr": "cloudfront",
    "space": "encoded",
}
_CLOUDFLARE_PATH = dict.fromkeys(("utf-8", "lone", "control", "tab"), "cloudflare empty") | {
    "nul": "cloudflare page",
    "cr": "cloudflare page",
    "space": "encoded",
}
_CLOUDFLARE_QUERY = _CLOUDFLARE_PATH | {"space": "cloudflare empty"}
_S3_QUERY = {
    "utf-8": "encoded",
    "lone": "encoded",
    "control": "encoded closed",
    "nul": "s3 empty",
    "tab": "s3 empty",
    "cr": "s3 empty",
    "space": "s3 505",
}
_GRAPH_QUERY = dict.fromkeys(("utf-8", "lone"), "encoded") | {
    "control": "graph invalid url",
    "nul": "graph invalid url",
    "tab": "graph badly formed",
    "cr": "graph badly formed",
    "space": "graph badly formed",
}
_MEASURED = {
    # vendor: (path the byte goes under, the path's answers, the query's answers)
    "api.github.com": (
        "/github/",
        dict.fromkeys(_KINDS, "github"),
        dict.fromkeys(_KINDS, "github"),
    ),
    "slack.com": ("/slack/api/", dict.fromkeys(_KINDS, "slack"), dict.fromkeys(_KINDS, "slack")),
    "www.googleapis.com": ("/drive/v3/files/", _GOOGLE, _GOOGLE),
    "sheets.googleapis.com": ("/sheets/v4/spreadsheets/", _GOOGLE, _GOOGLE),
    "Jira Cloud": ("/atlassian/rest/api/3/", _ATLASSIAN, _ATLASSIAN),
    "Confluence Cloud": ("/atlassian/wiki/rest/api/", _ATLASSIAN, _ATLASSIAN),
    "api.notion.com": ("/notion/v1/", _CLOUDFLARE_PATH, _CLOUDFLARE_QUERY),
    "api.hubapi.com": ("/hubspot/crm/v3/objects/contacts/", _CLOUDFLARE_PATH, _CLOUDFLARE_QUERY),
    "api.linear.app": ("/linear/", _CLOUDFLARE_PATH, _CLOUDFLARE_QUERY),
    "api.fireflies.ai": ("/fireflies/", _CLOUDFLARE_PATH, _CLOUDFLARE_QUERY),
    "s3.us-east-1.amazonaws.com": (
        "/s3/no-such-bucket/",
        _S3_QUERY | {"utf-8": "?", "lone": "?", "nul": "s3 empty"},
        _S3_QUERY,
    ),
    "graph.microsoft.com": ("/msgraph/v1.0/", _GRAPH_QUERY | {"lone": "graph empty"}, _GRAPH_QUERY),
}

# Targets the table above does not hold: raw bytes beside each other or beside an escape, both
# orders where the order decides, and paths outside a family's prefixes: (base, target after it,
# answer), each measured on the host whose path the base is.
_TARGETS = [
    ("/drive/v3/files/", b"nope\x09y\x01x", "google 54"),
    ("/drive/v3/files/", b"nope?b=\x01y\x20x", "google 54"),
    ("/drive/v3/files/", b"nope\x80x?b=\x20x", "google 1555"),
    ("/atlassian/rest/api/3/", b"nope\x01y\x00x", "cloudfront"),
    ("/atlassian/rest/api/3/", b"nope\x01y\x20x", "encoded"),
    ("/atlassian/rest/api/3/", b"nope\x20y\x09x", "encoded"),
    ("/atlassian/rest/api/3/", b"nope x?b=\x01x", "edge invalid_characters"),
    ("/atlassian/rest/api/3/", b"nope\x09x?b= x", "encoded"),
    ("/atlassian/rest/api/3/", b"nope?b=\x09y\x01x", "edge invalid_characters"),
    ("/atlassian/rest/api/3/", b"nope?b=\x01y\x20x", "edge invalid_characters"),
    ("/notion/v1/", b"nope\x01y\x20x", "encoded"),
    ("/notion/v1/", b"nope x?b=\x80x", "cloudflare empty"),
    ("/notion/v1/", b"nope\x01x?b= x", "cloudflare empty"),
    ("/notion/v1/", b"nope\x80y\x0dx", "cloudflare page"),
    ("/s3/no-such-bucket/", b"nope\x0dy x", "s3 empty"),
    ("/s3/no-such-bucket/", b"nope\x09y x", "s3 505"),
    ("/s3/no-such-bucket/", b"nope y\x09x", "s3 505"),
    ("/msgraph/v1.0/", b"nope\x01y\x09x", "graph invalid url"),
    ("/msgraph/v1.0/", b"nope\x09y\x01x", "graph badly formed"),
    ("/msgraph/v1.0/", b"nope\x80x?b=\x01x", "graph invalid url"),
    ("/github", b"?b=\x80x", "github"),
    ("/s3/", b"no-such-bucket\xc3\xa9x/nope", "?"),
    ("/s3/", b"no-such-bucket\xc3\xa9\x01x/nope", "? closed"),
    ("/s3/", b"no-such-bucket\x01\xc3\xa9x/nope", "? closed"),
    ("/s3/", b"no-such-bucket\x01\x80x/nope", "? closed"),
    ("/s3/no-such-bucket/", b"nope\x01\x80x", "? closed"),
    # the CDN's own refusals ahead of the edge's, as Backlot answers them encoded
    ("/atlassian/rest/api/3/", b"nope%00x\x01y", "encoded"),
    ("/atlassian/rest/api/3/", b"nope%G1x\ty", "encoded"),
    ("/atlassian/rest/api/3/", b"a/../nope\x01y", "encoded"),
    # and the edge's own `;` and Tomcat's escaped slash behind it
    ("/atlassian/rest/api/3/", b"nope;x\x01y", "edge invalid_characters"),
    ("/atlassian/wiki/rest/api/", b"nope%2Fx\x01y", "edge invalid_characters"),
    # Google's batch mounts, outside its families' prefixes (www.googleapis.com, and
    # gmail.googleapis.com for Gmail's)
    ("/batch/drive/v3", b"\x01x", "google 54"),
    ("/batch/drive/v3", b"?b=\x00x", "google 54"),
    ("/batch", b"?b=\tx", "google 1555"),
    ("/batch/gmail/v1", b"\x01x", "google 54"),
    ("/batch/gmail/v1", b" x", "google 1555"),
    ("/batch/drive/v3", b"x\x80", "encoded"),
]

# A method the CDN does not pass on, whose refusal comes ahead of the edge's: (method, base,
# target after it, answer), on Jira Cloud but the one Confluence path.
_CDN_METHODS = [
    (b"PROPFIND", "/atlassian/rest/api/3/", b"nope\x01y", "encoded"),
    (b"PROPFIND", "/atlassian/rest/api/3/", b"nope?b=\x01y", "encoded"),
    (b"PROPFIND", "/atlassian/wiki/rest/api/", b"nope?b=\x01y", "encoded"),
    (b"PROPFIND", "/atlassian/rest/api/3/", b"nope\ty", "encoded"),
    (b"PROPFIND", "/atlassian/rest/api/3/", b"nope y", "encoded"),
    (b"PROPFIND", "/atlassian/rest/api/3/", b"nope\x80y", "encoded"),
    (b"TRACE", "/atlassian/rest/api/3/", b"nope\x01y", "encoded"),
    (b"TRACE", "/atlassian/rest/api/3/", b"nope\ty", "encoded"),
    (b"PROPFIND", "/atlassian/rest/api/3/", b"nope\x00y", "cloudfront"),
    (b"PROPFIND", "/atlassian/rest/api/3/", b"nope\ry", "cloudfront"),
]


def _rows():
    for host, (base, path, query) in _MEASURED.items():
        for kind, sent in _KINDS.items():
            answer = _AS.get(kind, kind)
            ids = (f"{host} path {kind}", f"{host} {kind}")
            yield pytest.param(b"GET", base, b"nope" + sent + b"x", path[answer], 1, b"", id=ids[0])
            yield pytest.param(
                b"GET", base, b"nope?b=" + sent + b"x", query[answer], 1, b"", id=ids[1]
            )
    for base, rest, answer in _TARGETS:
        yield pytest.param(b"GET", base, rest, answer, 1, b"", id=f"{base} {rest!r}")
    for method, base, rest, answer in _CDN_METHODS:
        yield pytest.param(
            method, base, rest, answer, 1, b"", id=f"{method.decode()} {base} {rest!r}"
        )
    # `Authorization` twice, which Cloudflare refuses ahead of the bytes
    for base, sent in (("/linear/", b"\t"), ("/fireflies/", b"\x01"), ("/notion/v1/", b"\t")):
        rest = b"nope" + sent + b"x"
        yield pytest.param(b"GET", base, rest, "cloudflare page", 2, b"", id=f"{base} twice")
    yield pytest.param(
        b"GET", "/hubspot/crm/v3/objects/contacts/", b"nope\tx", "cloudflare page", 2, b""
    )
    yield pytest.param(
        b"GET", "/_meta/", b"nope\x80x", "uvicorn", 1, b"", id="a path no vendor owns"
    )
    # a read sent as a POST naming GET, which Google's API reads behind its front door
    override = b"X-HTTP-Method-Override: GET\r\n"
    for sent, answer in ((b"\x01", "google 54"), (b"\t", "google 1555"), (b"\x80", "encoded")):
        rest = b"?fields=kind&b=" + sent + b"x"
        yield pytest.param(
            b"POST", "/drive/v3/about", rest, answer, 1, override, id=f"override {sent!r}"
        )


def _read(reader) -> tuple[int, dict[str, str], bytes] | None:
    """One answer off ``reader``, by its `Content-Length` or its chunks, or ``None`` where the
    connection closed ahead of it."""
    if not (status_line := reader.readline()):
        return None
    headers = {}
    while (line := reader.readline()) not in (b"\r\n", b""):
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    if headers.get("transfer-encoding") == "chunked":
        body = b""
        while size := int(reader.readline().split(b";")[0], 16):
            body += reader.read(size)
            reader.readline()
        reader.readline()
    else:
        body = reader.read(int(headers.get("content-length", 0)))
    return int(status_line.split()[1]), headers, body


def _exchange(base_url: str, request: bytes) -> tuple[int, dict[str, str], bytes]:
    url = urllib.parse.urlsplit(base_url)
    with socket.create_connection((url.hostname, url.port), timeout=10) as c:
        c.sendall(request)
        return _read(c.makefile("rb"))


def _send(
    base_url: str,
    method: bytes,
    target: bytes,
    admin: str | None,
    times: int = 1,
    header: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    credential = b"Authorization: Bearer %s\r\n" % admin.encode() * times if admin else b""
    request = b"%s %s HTTP/1.1\r\nHost: x\r\n%s%s\r\n" % (method, target, credential, header)
    return _exchange(base_url, request)


def _encoded(target: bytes, question_marks: bool = False) -> bytes:
    path, mark, query = target.partition(b"?")
    if question_marks:
        path = re.sub(rb"[\x80-\xff]", b"%3F", path)
    raw = re.compile(rb"[\x00-\x20\x7f-\xff]")
    path, query = (raw.sub(lambda m: b"%%%02X" % m[0][0], part) for part in (path, query))
    return path + mark + query


#: The answers after which the connection reads another request; it is closed after every other one
#: (``backlot.request_target._page``).
_KEEPS_THE_CONNECTION = {"encoded", "?", "cloudflare empty", "graph empty"}


def _ids_left_out(base: str, body: bytes) -> object:
    """What two answers to one request share: S3's error and Graph's carry ids seeded from the
    request or drawn at random."""
    if base.startswith("/s3/"):
        return re.findall(rb"<(Code|BucketName|Key)>(.*?)</", body)
    if base.startswith("/msgraph/"):
        return re.findall(rb'"code":"(.*?)"', body)
    return body


def _page(answer: str, method: bytes, target: bytes) -> tuple[int, str | None, bytes]:
    if answer == "cloudfront":
        raw_path = _encoded(target).partition(b"?")[0].decode("latin-1")
        page = errors.atlassian.cdn_bad_request(method.decode(), raw_path)
        return 400, "text/html", page.encode()
    if answer.startswith("edge "):
        return 400, "text/html", errors.atlassian.edge_page(f"http1.{answer[5:]}").encode()
    return {
        "github": (400, "text/html; charset=utf-8", request_target.GITHUB_PAGE),
        "slack": (400, "text/plain", b"Bad Request"),
        "google 54": (400, "text/html; charset=UTF-8", request_target.GOOGLE_PAGE_54),
        "google 1555": (400, "text/html; charset=UTF-8", request_target.GOOGLE_PAGE_1555),
        "cloudflare page": (400, "text/html", request_target.CLOUDFLARE_PAGE),
        "cloudflare empty": (400, None, b""),
        "s3 empty": (400, None, b""),
        "graph empty": (400, None, b""),
        "graph invalid url": (400, "text/html; charset=us-ascii", request_target.GRAPH_INVALID_URL),
        "graph badly formed": (
            400,
            "text/html; charset=us-ascii",
            request_target.GRAPH_BADLY_FORMED,
        ),
        "uvicorn": (400, "text/plain; charset=utf-8", _UVICORN),
    }[answer]


@pytest.mark.parametrize("method, base, rest, answer, authorizations, header", list(_rows()))
def test_a_raw_byte_in_the_target_is_answered_as_the_vendors_front_door_answers_it(
    live_server, method, base, rest, answer, authorizations, header
):
    base_url, settings = live_server
    admin = None if base.startswith("/s3/") else settings.admin_token
    target = base.encode() + rest
    status, headers, body = _send(base_url, method, target, admin, authorizations, header)
    assert headers.get("connection") == (None if answer in _KEEPS_THE_CONNECTION else "close")
    reading = answer.removesuffix(" closed")
    if reading in ("encoded", "?"):
        encoded = _encoded(target, question_marks=reading == "?")
        twin = _send(base_url, method, encoded, admin, header=header)
        assert status == twin[0]
        assert _ids_left_out(base, body) == _ids_left_out(base, twin[2])
        return
    if answer == "s3 505":
        assert (status, headers["content-type"]) == (505, "application/xml")
        assert b"<Code>HttpVersionNotSupported</Code>" in body
        return
    want_status, want_type, want_body = _page(answer, method, target)
    assert (status, headers.get("content-type"), body) == (want_status, want_type, want_body)
    # the edge's page carries the edge's two headers beside its own, CloudFront's neither
    edge = {"x-content-type-options": "nosniff", "x-xss-protection": "1; mode=block"}
    if answer.startswith("edge "):
        assert {k: headers.get(k) for k in [*edge, "access-control-allow-origin"]} == edge | {
            "access-control-allow-origin": "*"
        }
    else:
        assert not edge.keys() & headers.keys()


@pytest.mark.parametrize(
    "line, encoded, sent",
    [
        (b"GET /a\x80b HTTP/1.1\r\n", b"GET /a%80b HTTP/1.1\r\n", b"/a\x80b"),
        (b"GET /a\x80b HTTP/1.1\n", b"GET /a%80b HTTP/1.1\n", b"/a\x80b"),
        (b"GET /a b?c d HTTP/1.1\r\n", b"GET /a%20b?c%20d HTTP/1.1\r\n", b"/a b?c d"),
        (b"GET /a\rb HTTP/1.1\r\n", b"GET /a%0Db HTTP/1.1\r\n", b"/a\rb"),
        (b"GET /a%20b HTTP/1.1\r\n", b"GET /a%20b HTTP/1.1\r\n", None),
        (b"GET /a\x80b\r\n", b"GET /a\x80b\r\n", None),
        (b"GET /a\x80b HTTQ/1.1\r\n", b"GET /a\x80b HTTQ/1.1\r\n", None),
        (b" /a\x80b HTTP/1.1\r\n", b" /a\x80b HTTP/1.1\r\n", None),
    ],
)
def test_the_target_is_what_lies_between_the_first_space_and_the_last(line, encoded, sent):
    assert request_target.encode_line(line) == (encoded, sent)


@pytest.mark.parametrize(
    "head, rest, statuses, body, before_the_rest",
    [
        # Cloudflare's empty 400, after which the connection reads the next request
        (b"GET /notion/v1/no", b"pe\tx", [200, 400, 200], b"", False),
        # GitHub's page, after which it is closed
        (b"GET /github/no", b"pe\x80x", [200, 400], request_target.GITHUB_PAGE, False),
        # S3 closes it after the answer it gives a control read as its escape
        (b"GET /s3/no-such-bucket/no", b"pe\x01x", [200, 404], None, False),
        # a method httptools does not know, one past its end, and none: uvicorn's 400 to the
        # first read alone
        (b"hello", b" /github/nope\x80x", [200, 400], _UVICORN, True),
        (b"GETX", b" /github/nope\x80x", [200, 400], _UVICORN, True),
        (b" GET", b" /github/nope\x80x", [200, 400], _UVICORN, True),
    ],
)
def test_a_request_line_in_two_reads_after_another_on_one_connection(
    live_server, head, rest, statuses, body, before_the_rest
):
    """The request line arrives in two reads, after a request answered on the same connection. The
    first read waits for the second where it starts a request line, and is answered alone where it
    does not (``backlot.protocol._starts_a_request_line``)."""
    base_url, settings = live_server
    url = urllib.parse.urlsplit(base_url)
    auth = (
        b"" if b"/s3/" in head else b"Authorization: Bearer %s\r\n" % settings.admin_token.encode()
    )
    with socket.create_connection((url.hostname, url.port), timeout=10) as c:
        reader = c.makefile("rb")
        answers, early = [], []
        for first, second in (
            (b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n", b""),
            (head, rest + b" HTTP/1.1\r\nHost: x\r\n" + auth + b"\r\n"),
            (b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n", b""),
        ):
            try:
                c.sendall(first)
                if second:
                    early.append(bool(select.select([c], [], [], 1)[0]))
                    if not early[-1]:
                        c.sendall(second)
                answer = _read(reader)
            except OSError:  # closed after the answer before
                break
            if answer is None:
                break
            answers.append(answer)
    assert early == [before_the_rest]
    assert [status for status, _, _ in answers] == statuses
    if body is not None:
        assert answers[1][2] == body


@pytest.mark.parametrize(
    "form, answered",
    [
        (b"token={token}", b'"ok":true'),
        (b"GET", b'"error":"invalid_form_data"'),
    ],
)
def test_a_body_in_a_read_of_its_own_reaches_the_route(live_server, form, answered):
    """A read that starts no request is the parser's as it came: a body with no LF in it, sent after
    its headers, is answered, one that reads as the start of a request line too."""
    base_url, settings = live_server
    url = urllib.parse.urlsplit(base_url)
    form = form.replace(b"{token}", settings.admin_token.encode())
    head = (
        b"POST /slack/api/auth.test HTTP/1.1\r\nHost: x\r\nConnection: close\r\n"
        b"Content-Type: application/x-www-form-urlencoded\r\nContent-Length: %d\r\n\r\n" % len(form)
    )
    with socket.create_connection((url.hostname, url.port), timeout=10) as c:
        c.sendall(head)
        time.sleep(0.2)
        c.sendall(form)
        answer = b""
        while chunk := c.recv(65536):
            answer += chunk
    assert answer.startswith(b"HTTP/1.1 200 ")
    assert answered in answer
