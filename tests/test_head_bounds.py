"""The answers ``backlot.protocol`` gives a request head at its front door's bound
(``backlot.head_bounds``), each held against the answer real gave at that bound, and what it leaves
alone: a head within the bound, the body after a head, and the app, which a head answered at its
bound never reaches.

A head that has not ended cannot be sent through a client library, so each one here goes over a
socket of its own, to the server ``live_server`` starts or, where what reaches the app is the
question, to ``recording_server``.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import select
import socket
import threading
import time
import urllib.parse

import pytest
import uvicorn
from uvicorn.server import ServerState

from backlot import head_bounds, protocol, request_target

#: Real's answer at each bound and to a request line refused ahead of one (``backlot.head_bounds``
#: and ``backlot.request_target.refused_in_the_line`` have the measurements): the status line,
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
    "github 400": (
        "HTTP/1.1 400 Bad Request",
        "text/html; charset=utf-8",
        1440,
        "06538877fab513379b1d448af80c1f1a8dcfe237ed50df3bb3824e4b66535873",
    ),
    "slack 400": (
        "HTTP/1.1 400 Bad Request",
        "text/plain",
        11,
        "7c41b898c5da0cfa4aa049b65ef50248bce9a72d24bef4c723786431921b75aa",
    ),
    "google 400": (
        "HTTP/1.0 400 Bad Request",
        "text/html; charset=UTF-8",
        54,
        "0fbc74daf7e1cd0368a530244c856bbd18eea886cfce777ee20c13728b1bbcea",
    ),
    "jira 400": (
        "HTTP/1.1 400 Bad Request",
        "text/html",
        859,
        "277e596aea250866337726f97b3ed1b95807a3bc122ea8a215c8b2c6ab4e75ee",
    ),
    "cloudflare bad request": (
        "HTTP/1.1 400 Bad Request",
        "text/html",
        155,
        "efca0895b4d88b27a94249f8e7ac0083eff0a4ff3ac37c2841b3f6d7e11c1905",
    ),
    "graph invalid url": (
        "HTTP/1.1 400 Bad Request",
        "text/html; charset=us-ascii",
        324,
        "14c08afc15e276b96c48de6598e86fcc933f3b105a2a18667d395d82c1ea97d5",
    ),
    "graph badly formed": (
        "HTTP/1.1 400 Bad Request",
        "text/html; charset=us-ascii",
        311,
        "1e47d302a6bb3d7706f196affb399da5e6d0fe9b589f53cb09d03f5a3677c2b0",
    ),
}

#: Each front door's bound on a head that has not ended: the prefix real never receives, the path
#: after it, the bytes it reads, whether of one line or of the whole head, and its answer in the
#: request line and in a header.
_BOUNDS = [
    ("/github", "/repos/acme/docs-site", 68_552, False, "github 414", "github 431"),
    ("/slack", "/api/api.test", 61_440, False, "slack", "slack"),
    ("/slack", "", 61_440, False, "slack", "slack"),
    ("", "/drive/v3/files", 65_536, False, "google", "google"),
    ("", "/gmail/v1/users/me/profile", 65_536, False, "google", "google"),
    ("/docs", "/v1/documents/x", 65_536, False, "google", "google"),
    ("/sheets", "/v4/spreadsheets/x", 65_536, False, "google", "google"),
    ("/slides", "/v1/presentations/x", 65_536, False, "google", "google"),
    ("/atlassian", "/rest/api/3/myself", 32_768, True, "jira 414", "jira 494"),
    ("/notion", "/v1/users", 131_072, True, "cloudflare 414", "cloudflare 400"),
    ("/hubspot", "/crm/v3/objects/contacts", 131_072, True, "cloudflare 414", "cloudflare 400"),
    ("/linear", "/graphql", 131_072, True, "cloudflare 414", "cloudflare 400"),
    ("/fireflies", "/graphql", 131_072, True, "cloudflare 414", "cloudflare 400"),
    ("/s3", "/", 16_384, False, "s3", "s3"),
    ("/msgraph", "/v1.0/me", 40_960, False, "graph 414", "graph 400"),
]

#: A request line holding a byte, a broken escape or a `..` the front door refuses, and real's
#: answer where the line reaches the bound and where a header after it does: the refusal where the
#: front door refuses the line ahead of its bound (``backlot.request_target.refused_in_the_line``),
#: its bound's page where it does not. The path is a row of :data:`_BOUNDS` with the bytes after it,
#: in the path or in a query before the test's own, each latin-1, so `\xc3\xa9` is the two bytes
#: of a UTF-8 `é`.
_REFUSED_LINES = [
    ("/github", "/repos/acme/docs-site\t", "github 400", "github 400"),
    ("/github", "/repos/acme/docs-site\xc3\xa9", "github 400", "github 400"),
    ("/github", "/repos/acme/docs-site%zz", "github 414", "github 431"),
    ("/github", "/repos/acme/docs-site?q=\x01x", "github 400", "github 400"),
    ("/github", "/repos/acme/docs-site?pad=" + "a" * 100 + "&q=\x01x", "github 400", "github 400"),
    ("/slack", "/api/api.test\xc3\xa9", "slack", "slack 400"),
    ("/slack", "/api/api.test\x00", "slack", "slack 400"),
    ("/slack", "/api/api.test%zz", "slack", "slack"),
    ("/slack", "/api/api.test?q=\x00x", "slack", "slack 400"),
    ("/slack", "/api/api.test?pad=" + "a" * 100 + "&q=\x00x", "slack", "slack 400"),
    # The LF at the 49,152nd byte of the head real gets, the last of the third piece of
    # ``backlot.head_bounds.PIECE`` bytes, and at the 49,153rd, in the piece that reaches the bound.
    ("/slack", "/api/api.test?q=\x01x&pad=" + "a" * 49_110, "slack", "slack 400"),
    ("/slack", "/api/api.test?q=\x01x&pad=" + "a" * 49_111, "slack", "slack"),
    ("", "/drive/v3/files\x01", "google 400", "google 400"),
    ("", "/drive/v3/files\x00", "google 400", "google 400"),
    ("", "/drive/v3/files\t", "google", "google"),
    ("", "/drive/v3/files\r", "google", "google"),
    ("/atlassian", "/rest/api/3/myself\x00", "jira 400", "jira 400"),
    ("/atlassian", "/rest/api/3/myself\r", "jira 400", "jira 400"),
    ("/atlassian", "/rest/api/3/myself%zz", "jira 414", "jira 400"),
    # A line of 20,000 bytes, which ends in the piece a head's 32,768th byte comes in.
    ("/atlassian", "/rest/api/3/myself%zz?pad=" + "a" * 19_956, "jira 414", "jira 400"),
    ("/atlassian", "/rest/api/3/myself\x01", "jira 414", "jira 494"),
    ("/notion", "/v1/users%zz", "cloudflare 414", "cloudflare bad request"),
    ("/notion", "/v1/users\r", "cloudflare bad request", "cloudflare bad request"),
    ("/notion", "/v1/x%2", "cloudflare 414", "cloudflare bad request"),
    ("/notion", "/v1/users\x01", "cloudflare 414", "cloudflare 400"),
    (
        "/hubspot",
        "/crm/v3/objects/contacts\x00",
        "cloudflare bad request",
        "cloudflare bad request",
    ),
    ("/linear", "/../graphql", "cloudflare 414", "cloudflare bad request"),
    ("/msgraph", "/v1.0/me\t", "graph badly formed", "graph badly formed"),
    ("/msgraph", "/v1.0/me\x01", "graph invalid url", "graph invalid url"),
    ("/msgraph", "/v1.0/me x", "graph badly formed", "graph badly formed"),
    ("/msgraph", "/v1.0/me\xc3\xa9", "graph 414", "graph 400"),
    ("/msgraph", "/v1.0/me?q=\tx", "graph badly formed", "graph badly formed"),
]
_BOUND_OF = {mount: (bound, per_line) for mount, _, bound, per_line, _, _ in _BOUNDS}

#: Ended request lines with no `?`, before a header past the bound, and real's answer: the root of
#: Slack's host, and on Notion a `%2` the end of the target cuts short, the bound's page where the
#: same `%2` before a `?` draws Cloudflare's.
_NO_QUERY = [
    ("/slack", "", "slack"),
    ("/notion", "/v1/x%2", "cloudflare 400"),
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


def _unseen(mount: str, path: str) -> int:
    """The bytes of ``mount`` real never receives: with no ``path`` after it, real gets `/`
    there."""
    return len(mount) - 1 if mount and not path else len(mount)


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


def _short_id(value: object) -> str | None:
    """A path of more than 200 characters named by its first 40 and its length."""
    if isinstance(value, str) and len(value) > 200:
        return f"{value[:40]}...{len(value)}"
    return None


#: A CRLF ahead of the request line, before a header past the bound: the prefix, the path, and
#: whether its two bytes count against the bound, where the bound is the head's; a line bound counts
#: the request line alone. Measured on 2026-11-02 (UTC) as ``backlot.head_bounds`` measured the
#: bounds: with the CRLF, a head two bytes short of the bound was refused on GitHub, Graph and S3
#: and three short waited, and on Slack and Google one byte short waited and the bound's was
#: refused, as on Jira Cloud and Notion.
_BLANK_AHEAD = [
    ("/github", "/repos/acme/docs-site", True),
    ("/slack", "/api/api.test", False),
    ("", "/drive/v3/files", False),
    ("/s3", "/", True),
    ("/msgraph", "/v1.0/me", True),
    ("/atlassian", "/rest/api/3/myself", None),
    ("/notion", "/v1/users", None),
]
_IN_HEADERS = {(mount, path): header for mount, path, *_, header in _BOUNDS}

#: Each case of the test below: the part the bound is reached in, what comes ahead of the request
#: line and whether the bound counts it, the row's prefix and path, what follows the path in the
#: request line, the bound and whether it is a line's, and real's answer. A row of :data:`_NO_QUERY`
#: is a case in a header only, its line ending at the path, and so is a row of :data:`_BLANK_AHEAD`.
_CASES = (
    [
        (part, "", None, mount, path, "?x=", bound, per_line, line if part == "line" else header)
        for mount, path, bound, per_line, line, header in _BOUNDS
        + [(m, p, *_BOUND_OF[m], line, header) for m, p, line, header in _REFUSED_LINES]
        for part in ("line", "header")
    ]
    + [("header", "", None, m, p, "", *_BOUND_OF[m], header) for m, p, header in _NO_QUERY]
    + [
        ("header", "\r\n", counts, m, p, "?x=", *_BOUND_OF[m], _IN_HEADERS[m, p])
        for m, p, counts in _BLANK_AHEAD
    ]
)


@pytest.mark.parametrize(
    "part, lead, counts, mount, path, query, bound, per_line, real", _CASES, ids=_short_id
)
def test_a_head_that_reaches_its_front_doors_bound_is_answered_as_real_answered_it(
    live_server, part, lead, counts, mount, path, query, bound, per_line, real
):
    """One byte short of the bound the head waits for more; the byte that reaches it is answered and
    the connection closed. The first read ends inside the path, before it names a vendor, and the
    second where the line's first bytes have named it, so the rest of the line comes after."""
    base_url, _ = live_server
    line = f"GET {mount}{path}{query}"
    ahead = len(lead) if counts else 0
    if part == "line":
        head = line + "a" * (bound + _unseen(mount, path) - len(line))
    else:
        start = f"{line}{'1' if query else ''} HTTP/1.1\r\nHost: x\r\nX-A: "
        counted = len("X-A: ") if per_line else len(start) - _unseen(mount, path) + ahead
        head = start + "a" * (bound - counted)
    head = lead + head
    named = len(lead) + head_bounds.NAMED_WITHIN
    six = len(lead) + 6
    with _connect(base_url) as c:
        for start, end, wait in ((0, six, 0.2), (six, named, 0.2), (named, -1, 0.5)):
            c.sendall(head[start:end].encode("latin-1"))
            assert not select.select([c], [], [], wait)[0]
        c.sendall(head[-1:].encode("latin-1"))
        answer = _until_closed(c)
    assert _as_real(answer) == _REAL[real]


@pytest.mark.parametrize("mount, path, per_line, read, refused", _ENDED)
def test_a_head_that_ends_is_read_to_the_bound_real_read_it_to(
    live_server, mount, path, per_line, read, refused
):
    """The longest head that ends real read draws the answer the same request draws with `x=1`, and
    one a byte longer real's refusal; on a path no vendor owns, both draw that answer."""
    base_url, _ = live_server

    def head(counted: int) -> bytes:
        start = f"GET {mount}{path}?x=1 HTTP/1.1\r\nHost: x\r\nX-A: "
        fixed = len("X-A: \r") if per_line else len(start) + len("\r\n\r\n") - _unseen(mount, path)
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


def test_the_request_line_of_a_path_no_vendor_owns_is_not_kept():
    """Once its first bytes name no vendor, a request line is not kept for a bound to read: a read
    of 1 MiB after them leaves the line as they were."""
    head = protocol._Head()
    start = b"GET /_meta/openapi" + b"a" * 100
    assert head.read(start) is None
    assert head.read(b"a" * 2**20) is None
    assert bytes(head.request_line) == start


class _Transport(asyncio.Transport):
    """A connection that keeps what the protocol writes to it."""

    def __init__(self) -> None:
        super().__init__()
        self.written = b""
        self.closed = False

    def get_extra_info(self, name, default=None):
        return default

    def write(self, data) -> None:
        self.written += data

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass


def _connection(reads: list[bytes]) -> tuple[list[str], list[list[str]], bool]:
    """``reads`` handed to ``backlot.protocol.Protocol`` in this process, a ``data_received`` call
    each and the app's tasks run as far as they go after each, in front of an app that answers each
    request it is handed with an empty 200: each request the app was handed (its method, path, body
    length and whether its target as sent is on the scope), the answers written after each read
    (a status line, or the :data:`_REAL` entry the answer is), and whether the connection closed."""
    seen = []

    async def app(scope, receive, send):
        body = b""
        while (message := await receive())["type"] == "http.request":
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        sent = request_target.SENT in scope
        seen.append(f"{scope['method']} {scope['path']} {len(body)} {sent}")
        headers = [(b"content-length", b"0")]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    loop = asyncio.new_event_loop()
    answers = []
    try:
        config = uvicorn.Config(app, http=protocol.Protocol, lifespan="off", log_config=None)
        state = ServerState()
        served = protocol.Protocol(config, state, {}, _loop=loop)
        transport = _Transport()
        served.connection_made(transport)
        for read in reads:
            before = len(transport.written)
            served.data_received(read)
            for _ in range(100):
                loop.run_until_complete(asyncio.sleep(0))
            written = transport.written[before:]
            starts = [m.start() for m in re.finditer(rb"HTTP/1\.[01] \d{3}", written)]
            answers.append(
                [_named(written[a:b]) for a, b in zip(starts, [*starts[1:], len(written)])]
            )
    finally:
        for task in state.tasks:
            task.cancel()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
    return seen, answers, transport.closed


def _named(answer: bytes) -> str:
    """The :data:`_REAL` entry ``answer`` is, or its status line."""
    head, _, body = answer.partition(b"\r\n\r\n")
    status_line, *lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    if "connection" in headers:
        shape = _as_real((status_line, headers, body))
        return next((name for name, real in _REAL.items() if real == shape), status_line)
    return status_line


def _s3(counted: int, ended: bool = True, method: str = "GET") -> bytes:
    """A head on S3 of which real gets ``counted`` bytes, its `X-A` header padded to that, ended
    with its CRLF and the empty line or not."""
    start = f"{method} /s3/b/k HTTP/1.1\r\nHost: x\r\nX-A: "
    end = "\r\n\r\n" if ended else ""
    return (start + "a" * (counted + len("/s3") - len(start) - len(end)) + end).encode()


_GITHUB = b"GET /github/repos/acme/docs-site HTTP/1.1\r\nHost: x\r\n\r\n"
_GITHUB_LINE = b"GET /github/repos/acme/docs-site?x=" + b"a" * 68_552
_GITHUB_HEADER = b"GET /github/repos/acme/docs-site HTTP/1.1\r\nHost: x\r\nX-A: " + b"a" * 70_000
_CHUNKED = (
    b"POST /s3/b/k HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
    b"6\r\na\r\n\r\nb\r\n0\r\n\r\n"
)
_LENGTH = b"POST /s3/b/k HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\n\r\n\r\nx"

#: The reads of one connection, and what the app is handed, the answers written after each read
#: and whether the connection closes: S3's bound is 16,384 bytes of the head and GitHub's 68,552,
#: and the second request starts in a read of its own or in the first one's, its head ending or
#: not, and after a body framed by its length or chunked, a CRLF CRLF in each.
_CONNECTIONS = [
    (
        [_s3(10_000), _s3(10_000, ended=False), b"\r\n\r\n", _s3(16_384, ended=False)],
        ["GET /s3/b/k 0 False"] * 2,
        [["HTTP/1.1 200 OK"], [], ["HTTP/1.1 200 OK"], ["s3"]],
        True,
    ),
    (
        [_s3(10_000) + _s3(10_000)],
        ["GET /s3/b/k 0 False"] * 2,
        [["HTTP/1.1 200 OK"] * 2],
        False,
    ),
    (
        [_s3(10_000) + _s3(16_384)[:30], _s3(16_384)[30:]],
        ["GET /s3/b/k 0 False"] * 2,
        [["HTTP/1.1 200 OK"], ["HTTP/1.1 200 OK"]],
        False,
    ),
    (
        [_s3(100) + _s3(20_000)[:30], _s3(20_000)[30:]],
        ["GET /s3/b/k 0 False"],
        [["HTTP/1.1 200 OK"], ["s3"]],
        True,
    ),
    ([_s3(100) + _s3(20_000)], ["GET /s3/b/k 0 False"], [["HTTP/1.1 200 OK", "s3"]], True),
    (
        [_s3(100) + _s3(20_000, ended=False)],
        ["GET /s3/b/k 0 False"],
        [["HTTP/1.1 200 OK", "s3"]],
        True,
    ),
    (
        [_s3(100) + _s3(100) + _s3(20_000, ended=False)],
        ["GET /s3/b/k 0 False"] * 2,
        [["HTTP/1.1 200 OK"] * 2 + ["s3"]],
        True,
    ),
    (
        [_LENGTH + _s3(20_000, ended=False)],
        ["POST /s3/b/k 5 False"],
        [["HTTP/1.1 200 OK", "s3"]],
        True,
    ),
    (
        [_CHUNKED + _s3(20_000, ended=False)],
        ["POST /s3/b/k 6 False"],
        [["HTTP/1.1 200 OK", "s3"]],
        True,
    ),
    (
        [_CHUNKED[:-1], _CHUNKED[-1:] + _s3(20_000, ended=False)],
        ["POST /s3/b/k 6 False"],
        [[], ["HTTP/1.1 200 OK", "s3"]],
        True,
    ),
    (
        [_GITHUB + _GITHUB_LINE],
        ["GET /github/repos/acme/docs-site 0 False"],
        [["HTTP/1.1 200 OK", "github 414"]],
        True,
    ),
    (
        [_GITHUB + _GITHUB_HEADER[:30], _GITHUB_HEADER[30:] + b"\r\n\r\n"],
        ["GET /github/repos/acme/docs-site 0 False"],
        [["HTTP/1.1 200 OK"], ["github 431"]],
        True,
    ),
    (
        [_GITHUB + b"GET /github/repos/acme/docs-site?q=\x01 HTTP/1.1\r\nHost: x\r\n\r\n"],
        [
            "GET /github/repos/acme/docs-site 0 False",
            "GET /github/repos/acme/docs-site 0 True",
        ],
        [["HTTP/1.1 200 OK"] * 2],
        False,
    ),
]


def test_each_request_on_a_connection_is_counted_from_its_own_start():
    """Each request's head is counted from its own first byte, whether it starts a read or starts in
    the read the request before it ends in, after a head or after a body. A head within its bound
    waits for its end; a head that reaches it is answered after each answer ahead of it, and the
    app is never handed it."""
    got = [_connection(reads) for reads, *_ in _CONNECTIONS]
    assert got == [tuple(expected) for _, *expected in _CONNECTIONS]


#: A body longer than the bound after a head that ends, on Graph, whose bound refuses a head at its
#: end, and on Notion, behind a line bound: the prefix, the path and the body's length, as
#: ``backlot.head_bounds`` has them measured.
_BODIES = [
    ("/msgraph", "/v1.0/me", 50_000),
    ("/notion", "/v1/search", 140_000),
]


@pytest.mark.parametrize("mount, path, size", _BODIES)
def test_a_body_after_the_head_is_not_counted(live_server, mount, path, size):
    """The long body is answered as the ten-byte one, by status line and `Content-Type`; the CR and
    the LF that end the head come in two reads, the body with the LF."""
    base_url, _ = live_server

    def answer(length: int) -> tuple[bytes, bytes | None]:
        head = (
            f"POST {mount}{path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            f"Content-Length: {length}\r\n\r"
        )
        with _connect(base_url) as c:
            c.sendall(head.encode())
            assert not select.select([c], [], [], 0.2)[0]
            c.sendall(b"\n" + b"a" * length)
            reader = c.makefile("rb")
            status_line = reader.readline()
            while (line := reader.readline()) not in (b"\r\n", b""):
                if line.lower().startswith(b"content-type:"):
                    return status_line, line
            return status_line, None

    assert answer(size) == answer(10)


@pytest.fixture(scope="module")
def recording_server():
    """``backlot.protocol.Protocol`` served in this process in front of an app that keeps the path
    of each request it is handed and answers 200."""
    seen: list[str] = []

    async def app(scope, receive, send):
        if scope["type"] == "http":
            seen.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    config = uvicorn.Config(app, port=0, http=protocol.Protocol, lifespan="off", log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", seen
    server.should_exit = True
    thread.join()


def test_a_head_answered_at_its_bound_never_reaches_the_app(recording_server):
    """A head that ends at Graph's bound is refused in the read that ends it, and the app runs only
    the request sent after it."""
    base_url, seen = recording_server
    seen.clear()
    start = "GET /msgraph/v1.0/me HTTP/1.1\r\nHost: x\r\nX-A: "
    head = start + "a" * (40_960 + len("/msgraph") - len(start) - len("\r\n\r\n")) + "\r\n\r\n"
    with _connect(base_url) as c:
        c.sendall(head.encode())
        answer = _until_closed(c)
    assert _as_real(answer) == _REAL["graph 400"]
    assert _status(base_url, b"GET /msgraph/v1.0/me HTTP/1.1\r\nHost: x\r\n\r\n") == 200
    assert seen == ["/msgraph/v1.0/me"]
