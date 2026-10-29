"""The answers ``backlot.protocol`` gives a request head at its front door's bound
(``backlot.head_bounds``), each held against the answer real gave at that bound.

A head that has not ended cannot be sent through a client library, so each one here goes over a
socket of its own to the server ``live_server`` starts.
"""

from __future__ import annotations

import hashlib
import re
import select
import socket
import urllib.parse

import pytest

#: Real's answer at each bound (``backlot.head_bounds`` has the measurement): the status line,
#: `Content-Type` (``None`` where it sent none), and the body's length and SHA-256, with the
#: `Request ID` line of CloudFront's page emptied.
_REAL = {
    "github 414": (
        "HTTP/1.1 414 URI Too Long",
        "text/html",
        110,
        "b60bdb8b0fdb09109c450993650a2ae3f04ab4169410039915e1edd4badb9860",
    ),
    "github 431": (
        "HTTP/1.1 431 Request Header Fields Too Large",
        "text/html",
        105,
        "b1c2bb42cc28d09ad56bfed1d3c2674dcf8f748db59fcbd72575ac8e308ceb15",
    ),
    "slack": (
        "HTTP/1.1 431 Request Header Fields Too Large",
        "text/plain",
        31,
        "56d32f525c4d72892347ae17a6a6db25ba9e88ff2071590b39643a31f6a33c17",
    ),
    "google": (
        "HTTP/1.0 413 Request Entity Too Large",
        "text/html; charset=UTF-8",
        2398,
        "23121050b9f5557057cce44c048d0390cce17c61671d4886c601f377089ef540",
    ),
    "jira 414": (
        "HTTP/1.1 414 Request-URI Too Large",
        "text/html",
        859,
        "081036ab43335282e8b0b52d07d995eb53f3396a87c37cab9622768f3d3194cf",
    ),
    "jira 494": (
        "HTTP/1.1 494 ",
        "text/html",
        859,
        "ac73c6f475394e1e902c08c33708b20caa7300465f81bde1794b65a1185a58ec",
    ),
    "cloudflare 414": (
        "HTTP/1.1 414 Request-URI Too Large",
        "text/html",
        175,
        "c9dafe24dc24cf39ca16172cb377dd858a6f0f2466fdcc906f564de61d108a5a",
    ),
    "cloudflare 400": (
        "HTTP/1.1 400 Bad Request",
        "text/html",
        231,
        "a4ea3faf060301c504a3290a4aaa3a3045dbcd8f3bd43c567c5686d6d35a41e1",
    ),
    "s3": (
        "HTTP/1.1 400 Bad Request",
        None,
        0,
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ),
    "graph 414": (
        "HTTP/1.1 414 Request-URI Too Long",
        "text/html; charset=us-ascii",
        329,
        "ef972000aa66e129b77e955e2cdfe3bc43df42c777cd3973204cafc8c4a37084",
    ),
    "graph 400": (
        "HTTP/1.1 400 Bad Request",
        "text/html; charset=us-ascii",
        346,
        "437f2e5f4e6aef7dae3d4e4894234596f579f2bf85a7a20a8482fdbfafd33c56",
    ),
}

#: Each front door's bound on a head that has not ended: the prefix real never receives, the path
#: after it, the bytes it reads, whether of one line or of the whole head, and its answer in the
#: request line and in a header.
_BOUNDS = [
    ("/github", "/repos/acme/docs-site", 68_552, False, "github 414", "github 431"),
    ("/slack", "/api/api.test", 61_440, False, "slack", "slack"),
    ("", "/drive/v3/files", 65_536, False, "google", "google"),
    ("", "/gmail/v1/users/me/profile", 65_536, False, "google", "google"),
    ("/docs", "/v1/documents/x", 65_536, False, "google", "google"),
    ("/atlassian", "/rest/api/3/myself", 32_768, True, "jira 414", "jira 494"),
    ("/notion", "/v1/users", 131_072, True, "cloudflare 414", "cloudflare 400"),
    ("/hubspot", "/crm/v3/objects/contacts", 131_072, True, "cloudflare 414", "cloudflare 400"),
    ("/linear", "/graphql", 131_072, True, "cloudflare 414", "cloudflare 400"),
    ("/fireflies", "/graphql", 131_072, True, "cloudflare 414", "cloudflare 400"),
    ("/s3", "/", 16_384, False, "s3", "s3"),
    ("/msgraph", "/v1.0/me", 40_960, False, "graph 414", "graph 400"),
]

#: How a head that ends after a long header meets the bound: the prefix, the path, whether the bytes
#: counted are the header line's, the most of them real read, and its answer to one byte more; on a
#: path no vendor owns, with no bound, a head of 262,144 bytes and ``None``, one byte more being
#: read as well.
_ENDED = [
    ("/slack", "/api/api.test", False, 61_440, "slack"),
    ("", "/drive/v3/files", False, 65_536, "google"),
    ("/s3", "/", False, 16_384, "s3"),
    ("/msgraph", "/v1.0/me", False, 40_959, "graph 400"),
    ("/linear", "/graphql", True, 131_071, "cloudflare 400"),
    ("", "/_meta/nope", False, 262_144, None),
]


def _connect(base_url: str) -> socket.socket:
    url = urllib.parse.urlsplit(base_url)
    return socket.create_connection((url.hostname, url.port), timeout=10)


def _until_closed(c: socket.socket) -> tuple[str, dict[str, str], bytes]:
    """The answer on ``c`` up to the close that follows it: its status line, its headers and its
    body."""
    got = b""
    while chunk := c.recv(65536):
        got += chunk
    head, _, body = got.partition(b"\r\n\r\n")
    status_line, *lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status_line, headers, body


def _as_real(answer: tuple[str, dict[str, str], bytes]) -> tuple[str, str | None, int, str]:
    status_line, headers, body = answer
    assert headers["content-length"] == str(len(body))
    assert headers["connection"] == "close"
    body = re.sub(rb"Request ID: [^\n]*", b"Request ID: ", body)
    return status_line, headers.get("content-type"), len(body), hashlib.sha256(body).hexdigest()


def _status(base_url: str, head: bytes) -> int:
    with _connect(base_url) as c:
        c.sendall(head)
        return int(c.makefile("rb").readline().split()[1])


@pytest.mark.parametrize("part", ["line", "header"])
@pytest.mark.parametrize("mount, path, bound, per_line, in_line, in_headers", _BOUNDS)
def test_a_head_that_reaches_its_front_doors_bound_is_answered_as_real_answered_it(
    live_server, mount, path, bound, per_line, in_line, in_headers, part
):
    """One byte short of the bound the head waits for more; the byte that reaches it is answered
    and the connection closed."""
    base_url, _ = live_server
    line = f"GET {mount}{path}?x="
    if part == "line":
        head = line + "a" * (bound + len(mount) - len(line))
    else:
        start = f"{line}1 HTTP/1.1\r\nHost: x\r\nX-A: "
        counted = len("X-A: ") if per_line else len(start) - len(mount)
        head = start + "a" * (bound - counted)
    with _connect(base_url) as c:
        c.sendall(head[:-1].encode())
        assert not select.select([c], [], [], 0.5)[0]
        c.sendall(head[-1:].encode())
        answer = _until_closed(c)
    assert _as_real(answer) == _REAL[in_line if part == "line" else in_headers]


@pytest.mark.parametrize("mount, path, per_line, read, refused", _ENDED)
def test_a_head_that_ends_is_read_to_the_bound_real_read_it_to(
    live_server, mount, path, per_line, read, refused
):
    """The longest head that ends real read draws the answer the same request draws with `x=1`, and
    one a byte longer real's refusal; on a path no vendor owns, both draw that answer."""
    base_url, _ = live_server

    def head(counted: int) -> bytes:
        start = f"GET {mount}{path}?x=1 HTTP/1.1\r\nHost: x\r\nX-A: "
        fixed = len("X-A: \r") if per_line else len(start) + len("\r\n\r\n") - len(mount)
        return (start + "a" * (counted - fixed) + "\r\n\r\n").encode()

    short = f"GET {mount}{path}?x=1 HTTP/1.1\r\nHost: x\r\n\r\n".encode()
    assert _status(base_url, head(read)) == _status(base_url, short)
    if refused is None:
        assert _status(base_url, head(read + 1)) == _status(base_url, short)
        return
    with _connect(base_url) as c:
        c.sendall(head(read + 1))
        answer = _until_closed(c)
    assert _as_real(answer) == _REAL[refused]


def test_each_request_on_a_connection_is_counted_from_its_own_start(live_server):
    """Two heads of 10,000 bytes on one connection, past S3's bound of 16,384 together: the second
    is read as the first was, and waits for its end like any head within the bound."""
    base_url, _ = live_server
    start = "GET /s3/no-such-bucket/nope HTTP/1.1\r\nHost: x\r\nX-A: "
    value = "a" * (10_000 - len(start) - len("\r\n\r\n") + len("/s3"))
    with _connect(base_url) as c:
        reader = c.makefile("rb")
        c.sendall(f"{start}{value}\r\n\r\n".encode())
        assert reader.readline().startswith(b"HTTP/1.1 404")
        length = 0
        while (line := reader.readline()) not in (b"\r\n", b""):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        reader.read(length)
        c.sendall(f"{start}{value}".encode())
        assert not select.select([c], [], [], 0.5)[0]
        c.sendall(b"\r\n\r\n")
        assert reader.readline().startswith(b"HTTP/1.1 404")
