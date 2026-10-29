"""The answers ``backlot.protocol`` writes for a request httptools refuses
(``backlot.parser_refusals``), each held against the answer real gave the same request, and the
refusals it leaves to uvicorn's 400.

Each request is written byte for byte to the server ``live_server`` starts, so that httptools reads
it as written.
"""

from __future__ import annotations

import hashlib
import re
import socket
import time
import urllib.parse

import pytest

#: Real's answer to each request below (``backlot.parser_refusals`` has the measurement), as its
#: status line and `Content-Type`, each ``None`` where real sent none, the size and SHA-256 of its
#: body once CloudFront's `Request ID` and S3's `RequestId` and `HostId` are emptied, and whether an
#: `x-github-request-id` came with it. The edge page on Jira Cloud is held by its `details` tag,
#: which is what ``backlot.errors.atlassian.edge_page`` keeps of real's.
_REAL = {
    "github page": (
        "HTTP/1.1 400 Bad Request",
        "text/html; charset=utf-8",
        1440,
        "06538877fab513379b1d448af80c1f1a8dcfe237ed50df3bb3824e4b66535873",
        False,
    ),
    "github empty": (
        "HTTP/1.1 400 Bad Request",
        None,
        0,
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        True,
    ),
    "slack 400": (
        "HTTP/1.1 400 Bad Request",
        "text/plain",
        11,
        "7c41b898c5da0cfa4aa049b65ef50248bce9a72d24bef4c723786431921b75aa",
        False,
    ),
    "slack 501": (
        "HTTP/1.1 501 Not Implemented",
        "text/plain",
        15,
        "cc8afc1a45a0ebcda37f1fde9f42d5213f57c92ab6c4172c4c182f666bd71701",
        False,
    ),
    "google 400": (
        "HTTP/1.0 400 Bad Request",
        "text/html; charset=UTF-8",
        54,
        "0fbc74daf7e1cd0368a530244c856bbd18eea886cfce777ee20c13728b1bbcea",
        False,
    ),
    "jira edge": (
        "HTTP/1.1 400 Bad Request",
        "text/html",
        "details",
        "http1.invalid_characters",
        False,
    ),
    "jira 400": (
        "HTTP/1.1 400 Bad Request",
        "text/html",
        859,
        "277e596aea250866337726f97b3ed1b95807a3bc122ea8a215c8b2c6ab4e75ee",
        False,
    ),
    "jira 501": (
        "HTTP/1.1 501 Not Implemented",
        "text/html",
        859,
        "44f9d5df42a60859410f4b17e49bc69ab1fdbb1c718c53d080c1326df0a32d81",
        False,
    ),
    "jira 505 bare": (
        None,
        None,
        859,
        "5a9fb8e548a8fe958e1d7979bf2ebdc0327349c4623bb5dea4b66e18b2228cec",
        False,
    ),
    "cloudflare empty": (
        "HTTP/1.1 400 Bad Request",
        None,
        0,
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        False,
    ),
    "cloudflare 400": (
        "HTTP/1.1 400 Bad Request",
        "text/html",
        155,
        "efca0895b4d88b27a94249f8e7ac0083eff0a4ff3ac37c2841b3f6d7e11c1905",
        False,
    ),
    "cloudflare 501": (
        "HTTP/1.1 501 Not Implemented",
        "text/html",
        163,
        "6e8ee127e655e8244721154eca2daf1973a3c23e431ed5be908eafee826cbf3c",
        False,
    ),
    "cloudflare 403 bare": (
        None,
        None,
        151,
        "eed0b81a2fbdd1c5a9f80705885fc5bbf346ba428a79ff7a13ec8491c6a8e96c",
        False,
    ),
    "cloudflare 400 bare": (
        None,
        None,
        155,
        "efca0895b4d88b27a94249f8e7ac0083eff0a4ff3ac37c2841b3f6d7e11c1905",
        False,
    ),
    "s3 empty": (
        "HTTP/1.1 400 Bad Request",
        None,
        0,
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        False,
    ),
    "s3 505": (
        "HTTP/1.1 505 HTTP Version not supported",
        "application/xml",
        192,
        "2d906d19fdfe52cd78c3c46a400cdc11d80874f88544d2063c5aa1264dbd5037",
        False,
    ),
    "graph 400": (
        "HTTP/1.1 400 Bad Request",
        "text/html; charset=us-ascii",
        311,
        "1e47d302a6bb3d7706f196affb399da5e6d0fe9b589f53cb09d03f5a3677c2b0",
        False,
    ),
    "graph 501": (
        "HTTP/1.1 501 Not Implemented",
        "text/html; charset=us-ascii",
        343,
        "60ea6cb8c598707355381c5bb1d39af603c4d54d0886cbdda6f1ea9993b3cbfd",
        False,
    ),
}

#: uvicorn's own answer to what httptools refuses.
_UVICORN = (
    "HTTP/1.1 400 Bad Request",
    "text/plain; charset=utf-8",
    30,
    hashlib.sha256(b"Invalid HTTP request received.").hexdigest(),
    False,
)

_BODY = b'{"query":"{ __typename }"}'
_LENGTH = f"Content-Length: {len(_BODY)}"
_JSON = "Content-Type: application/json"

#: Each request below: its method, what follows the path in the request line, its header lines and
#: whether it sends :data:`_BODY`. A `|` in a header line is where the request is split between two
#: reads; `{token}` is the admin's token.
_KINDS = {
    "nul": ("GET", " HTTP/1.1", ["X-Probe: a\x00b"], False),
    "control": ("GET", " HTTP/1.1", ["X-Probe: a\x01b"], False),
    "control in a later read": ("GET", " HTTP/1.1", ["X-Probe: a|\x7fb"], False),
    "nul in a later read": ("GET", " HTTP/1.1", ["X-Probe: a|\x00b"], False),
    "control, credential ahead": (
        "GET",
        " HTTP/1.1",
        ["Authorization: token {token}", "X-Probe: a\x01b"],
        False,
    ),
    "control, credential after": (
        "GET",
        " HTTP/1.1",
        ["X-Probe: a\x01b", "Authorization: token {token}"],
        False,
    ),
    "control in a name": ("GET", " HTTP/1.1", ["X-Pr\x01obe: ab"], False),
    "blank coding": ("POST", " HTTP/1.1", [_JSON, "Transfer-Encoding: ", _LENGTH], True),
    "blank coding, no length": ("POST", " HTTP/1.1", [_JSON, "Transfer-Encoding: "], False),
    "blank coding after a length": (
        "POST",
        " HTTP/1.1",
        [_JSON, _LENGTH, "Transfer-Encoding: "],
        True,
    ),
    "tab coding twice": (
        "POST",
        " HTTP/1.1",
        [_JSON, "Transfer-Encoding: \t", "Transfer-Encoding: \t", _LENGTH],
        True,
    ),
    "identity": ("POST", " HTTP/1.1", [_JSON, "Transfer-Encoding: identity", _LENGTH], True),
    "IDENTITY": ("POST", " HTTP/1.1", [_JSON, "Transfer-Encoding: IDENTITY", _LENGTH], True),
    "identity after a length": (
        "POST",
        " HTTP/1.1",
        [_JSON, _LENGTH, "Transfer-Encoding: identity"],
        True,
    ),
    "nul, then a control byte": (
        "GET",
        " HTTP/1.1",
        ["X-Probe: a\x00b", "X-Probe2: a\x01b"],
        False,
    ),
    "control, then a nul": ("GET", " HTTP/1.1", ["X-Probe2: a\x01b", "X-Probe: a\x00b"], False),
    "blank coding, then a control byte": (
        "POST",
        " HTTP/1.1",
        [_JSON, "Transfer-Encoding: ", "X-Probe: a\x01b", _LENGTH],
        True,
    ),
    "identity, split in its name": (
        "POST",
        " HTTP/1.1",
        [_JSON, "Transfer-Enc|oding: identity", _LENGTH],
        True,
    ),
    "nul, then a length and a blank coding": (
        "POST",
        " HTTP/1.1",
        ["X-Probe: a\x00b", _JSON, _LENGTH, "Transfer-Encoding: "],
        True,
    ),
    "LF, then a length and a blank coding": (
        "POST",
        "/nope\nx HTTP/1.1",
        [_JSON, _LENGTH, "Transfer-Encoding: "],
        True,
    ),
    "identity, no length": ("POST", " HTTP/1.1", [_JSON, "Transfer-Encoding: identity"], False),
    "identity after a length, split in its value": (
        "POST",
        " HTTP/1.1",
        [_JSON, _LENGTH, "Transfer-Encoding: iden|tity"],
        True,
    ),
    "chunked after a length": (
        "POST",
        " HTTP/1.1",
        [_JSON, _LENGTH, "Transfer-Encoding: chunked"],
        True,
    ),
    "chunked beside a length": (
        "POST",
        " HTTP/1.1",
        [_JSON, "Transfer-Encoding: chunked", _LENGTH],
        True,
    ),
    "LF": ("GET", "/nope\nx HTTP/1.1", [], False),
    "LF in the query": ("GET", "?b=\nx HTTP/1.1", [], False),
    "LF on a POST": ("POST", "/nope\nx HTTP/1.1", [_JSON, _LENGTH], True),
    "LF on a HEAD": ("HEAD", "/nope\nx HTTP/1.1", [], False),
}

_GITHUB = ("/github", "/repos/octocat/Hello-World")
_SLACK = ("/slack", "/api/api.test")
_GOOGLE = ("", "/drive/v3/about")
_JIRA = ("/atlassian", "/rest/api/3/myself")
_LINEAR = ("/linear", "/graphql")
_S3 = ("/s3", "/")
_GRAPH = ("/msgraph", "/v1.0/me")

#: Each request real's front door refused, on a path of its vendor's, and real's answer.
_ROWS = [
    (*_GITHUB, "nul", "github page"),
    (*_GITHUB, "control", "github empty"),
    (*_GITHUB, "blank coding", "github page"),
    (*_GITHUB, "blank coding, no length", "github page"),
    (*_GITHUB, "tab coding twice", "github page"),
    (*_GITHUB, "identity", "github page"),
    (*_GITHUB, "IDENTITY", "github page"),
    (*_GITHUB, "identity after a length", "github page"),
    (*_GITHUB, "LF", "github page"),
    (*_GITHUB, "LF in the query", "github page"),
    (*_GITHUB, "LF on a POST", "github page"),
    (*_GITHUB, "nul, then a control byte", "github page"),
    (*_GITHUB, "control, then a nul", "github page"),
    (*_GITHUB, "blank coding, then a control byte", "github page"),
    (*_GITHUB, "nul, then a length and a blank coding", "github page"),
    (*_GITHUB, "LF, then a length and a blank coding", "github page"),
    (*_SLACK, "nul", "slack 400"),
    (*_SLACK, "control", "slack 400"),
    (*_SLACK, "blank coding", "slack 400"),
    (*_SLACK, "blank coding, no length", "slack 501"),
    (*_SLACK, "blank coding after a length", "slack 400"),
    (*_SLACK, "identity", "slack 400"),
    (*_SLACK, "IDENTITY", "slack 400"),
    (*_SLACK, "identity after a length", "slack 400"),
    (*_GOOGLE, "nul", "google 400"),
    (*_GOOGLE, "control", "google 400"),
    (*_GOOGLE, "blank coding", "google 400"),
    (*_GOOGLE, "blank coding, no length", "google 400"),
    (*_GOOGLE, "blank coding after a length", "google 400"),
    (*_JIRA, "control", "jira edge"),
    (*_JIRA, "nul", "jira 400"),
    (*_JIRA, "blank coding", "jira 501"),
    (*_JIRA, "blank coding after a length", "jira 501"),
    (*_JIRA, "tab coding twice", "jira 501"),
    (*_JIRA, "LF", "jira 505 bare"),
    (*_JIRA, "LF on a POST", "jira 505 bare"),
    (*_JIRA, "LF on a HEAD", "jira 505 bare"),
    (*_JIRA, "nul, then a control byte", "jira 400"),
    (*_JIRA, "control, then a nul", "jira 400"),
    (*_JIRA, "blank coding, then a control byte", "jira 501"),
    (*_LINEAR, "nul", "cloudflare 400"),
    (*_LINEAR, "control", "cloudflare empty"),
    (*_LINEAR, "control in a later read", "cloudflare empty"),
    (*_LINEAR, "nul in a later read", "cloudflare 400"),
    (*_LINEAR, "blank coding", "cloudflare 501"),
    (*_LINEAR, "blank coding, no length", "cloudflare 501"),
    (*_LINEAR, "blank coding after a length", "cloudflare 501"),
    (*_LINEAR, "identity", "cloudflare 400"),
    (*_LINEAR, "IDENTITY", "cloudflare 400"),
    (*_LINEAR, "identity after a length", "cloudflare 400"),
    (*_LINEAR, "LF", "cloudflare 403 bare"),
    (*_LINEAR, "LF in the query", "cloudflare 403 bare"),
    (*_LINEAR, "LF on a POST", "cloudflare 400 bare"),
    (*_LINEAR, "LF on a HEAD", "cloudflare 400 bare"),
    (*_LINEAR, "nul, then a control byte", "cloudflare 400"),
    (*_LINEAR, "control, then a nul", "cloudflare 400"),
    (*_LINEAR, "blank coding, then a control byte", "cloudflare 501"),
    (*_LINEAR, "identity, split in its name", "cloudflare 400"),
    ("/notion", "/v1/users", "control", "cloudflare empty"),
    ("/hubspot", "/crm/v3/objects/contacts", "blank coding", "cloudflare 501"),
    ("/fireflies", "/graphql", "identity", "cloudflare 400"),
    (*_S3, "nul", "s3 empty"),
    (*_S3, "blank coding", "s3 empty"),
    (*_S3, "blank coding, no length", "s3 empty"),
    (*_S3, "blank coding after a length", "s3 empty"),
    (*_S3, "LF", "s3 505"),
    (*_S3, "LF on a POST", "s3 505"),
    (*_S3, "nul, then a control byte", "s3 empty"),
    (*_S3, "control, then a nul", "s3 empty"),
    (*_S3, "blank coding, then a control byte", "s3 empty"),
    (*_GRAPH, "nul", "graph 400"),
    (*_GRAPH, "control", "graph 400"),
    (*_GRAPH, "blank coding", "graph 501"),
    (*_GRAPH, "blank coding after a length", "graph 501"),
    (*_GRAPH, "identity", "graph 501"),
    (*_GRAPH, "IDENTITY", "graph 501"),
    (*_GRAPH, "identity after a length", "graph 501"),
]

#: The refusals uvicorn's 400 answers: where real reads the request on (the module docstring of
#: ``backlot.parser_refusals`` says where), where no rule names the refusal, and on a path no vendor
#: owns.
_UVICORNS = [
    (*_GITHUB, "control, credential ahead"),
    (*_GITHUB, "control, credential after"),
    (*_GITHUB, "blank coding after a length"),
    (*_SLACK, "LF"),
    (*_GOOGLE, "identity"),
    (*_GOOGLE, "LF"),
    (*_JIRA, "identity"),
    (*_S3, "control"),
    (*_S3, "identity"),
    (*_GRAPH, "LF"),
    (*_LINEAR, "chunked beside a length"),
    (*_LINEAR, "chunked after a length"),
    (*_LINEAR, "identity, no length"),
    (*_LINEAR, "identity after a length, split in its value"),
    (*_LINEAR, "control in a name"),
    ("", "/_meta/nope", "control"),
]


def _request(mount: str, path: str, kind: str, token: str) -> list[bytes]:
    """The request ``kind`` names, in the reads it is sent in."""
    method, rest, lines, body = _KINDS[kind]
    lines = [line.format(token=token) for line in ["Host: x", "Connection: close", *lines]]
    raw = f"{method} {mount}{path}{rest}\r\n" + "".join(f"{line}\r\n" for line in lines) + "\r\n"
    return [part.encode("latin-1") for part in raw.split("|")[:-1]] + [
        raw.split("|")[-1].encode("latin-1") + (_BODY if body else b"")
    ]


def _answer(base_url: str, reads: list[bytes]) -> bytes:
    url = urllib.parse.urlsplit(base_url)
    with socket.create_connection((url.hostname, url.port), timeout=10) as c:
        for i, read in enumerate(reads):
            if i:
                time.sleep(0.2)
            c.sendall(read)
        got = b""
        while chunk := c.recv(65536):
            got += chunk
    return got


def _shape(answer: bytes) -> tuple:
    """``answer`` in the shape of :data:`_REAL`'s entries."""
    status_line = content_type = None
    request_id = False
    body = answer
    if answer.startswith(b"HTTP/"):
        head, _, body = answer.partition(b"\r\n\r\n")
        status_line, *lines = head.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        assert headers["content-length"] == str(len(body))
        assert headers["connection"] == "close"
        content_type = headers.get("content-type")
        request_id = "x-github-request-id" in headers
    if details := re.search(rb'<meta name="details" content="([^"]*)"', body):
        return status_line, content_type, "details", details[1].decode(), request_id
    body = re.sub(rb"Request ID: [^\n]*", b"Request ID: ", body)
    body = re.sub(
        rb"<RequestId>[^<]*</RequestId><HostId>[^<]*</HostId>",
        b"<RequestId></RequestId><HostId></HostId>",
        body,
    )
    return status_line, content_type, len(body), hashlib.sha256(body).hexdigest(), request_id


@pytest.mark.parametrize("mount, path, kind, real", _ROWS)
def test_a_request_httptools_refuses_is_answered_as_its_front_door_answered_it(
    live_server, mount, path, kind, real
):
    base_url, settings = live_server
    answer = _answer(base_url, _request(mount, path, kind, settings.admin_token))
    assert _shape(answer) == _REAL[real]


@pytest.mark.parametrize("byte", [*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F])
def test_each_byte_httptools_refuses_in_a_value_is_answered_by_its_kind(live_server, byte):
    """0x00 draws Cloudflare's page and each other byte its empty 400, on Linear as on the four
    hosts behind Cloudflare, which answered each byte alike."""
    base_url, _ = live_server
    raw = f"GET /linear/graphql HTTP/1.1\r\nHost: x\r\nX-Probe: a{chr(byte)}b\r\n\r\n"
    answer = _answer(base_url, [raw.encode("latin-1")])
    assert _shape(answer) == _REAL["cloudflare 400" if byte == 0 else "cloudflare empty"]


@pytest.mark.parametrize("mount, path, kind", _UVICORNS)
def test_a_refusal_no_rule_answers_keeps_uvicorns_400(live_server, mount, path, kind):
    base_url, settings = live_server
    answer = _answer(base_url, _request(mount, path, kind, settings.admin_token))
    assert _shape(answer) == _UVICORN


def test_a_refused_request_in_the_read_that_ends_the_one_before_it_keeps_uvicorns_400(
    live_server,
):
    """The protocol has read none of the second request's head, which rides in the read that ends
    the first, so the `Content-Length` it sends is not seen and Slack's 501 for a blank coding with
    none is not written."""
    base_url, _ = live_server
    first = b"GET /slack/api/api.test HTTP/1.1\r\nHost: x\r\n\r\n"
    second = b"POST /slack/api/api.test HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: \r\n" + (
        f"{_LENGTH}\r\n\r\n".encode() + _BODY
    )
    answer = _answer(base_url, [first + second])
    assert _shape(answer[answer.index(b"HTTP/1.1 400") :]) == _UVICORN
