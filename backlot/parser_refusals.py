"""The front doors' answers to a request that httptools, uvicorn's parser, refuses, where the front
door refuses it as well. :class:`backlot.protocol.Protocol` names the refusal in a
:class:`Refused` and writes :func:`answer`'s bytes in place of uvicorn's 400, which reads
`Invalid HTTP request received.`; where :func:`answer` gives ``None`` that 400 stands.

Each answer is written by :func:`backlot.head_bounds.written`, or is a body alone where the front
door sent no status line; of a front door's other headers only GitHub's request id is sent.
:func:`answer` gives ``None`` where real reads the request on to the API, which answers it: S3
`control`; GitHub `control` beside an `Authorization`, with a 400 reading `Bad Request` and the
API's headers where the credential resolves and a 401 reading `Bad credentials` where it does not;
Google, Jira Cloud and S3 `identity`; and Slack `versionless`, answered on `HTTP/1.0`. It gives
``None`` too for Google's and Graph's `versionless`, which each answered by the method (Google's 404
to a `GET` names the target), and for GitHub's `blank` after a `Content-Length`, where real closed
the connection with no answer.

Measured on 2026-10-29 over raw HTTP/1.1 on TLS sockets, one connection per request and a
cache-buster in each query, on api.github.com, slack.com, www.googleapis.com, Jira Cloud,
api.notion.com, api.linear.app, api.fireflies.ai, api.hubapi.com, S3 in us-east-1 and
graph.microsoft.com, each with no credential: a header value holding each byte of `nul` and
`control` (:data:`Cause`) between two letters; a `POST` whose `Transfer-Encoding` was empty, a space
or a tab, once or twice, ahead of a `Content-Length`, after one or with none; `identity` and
`IDENTITY` ahead of a `Content-Length` and `identity` after one; an LF in a path no route serves
(`…/nope<LF>x HTTP/1.1`) on `GET`, `POST`, `HEAD`, `PUT`, `DELETE`, `PATCH` and `OPTIONS`, and in a
query value (`?b=<LF>x`) on `GET`; and a 0x00 and a 0x01 in two header values, and an empty
`Transfer-Encoding` and a 0x01 in another header's value, each pair in either order. On
api.github.com a `control` byte was sent with a token ahead of the header and after it, and with one
that does not resolve, and a 0x00 and an LF each ahead of a `Content-Length` and an empty
`Transfer-Encoding`. The four Cloudflare hosts answered each alike.
"""

from __future__ import annotations

import urllib.parse
from typing import Literal, NamedTuple

from backlot import errors, head_bounds, request_target
from backlot.routers import github

#: Why httptools refused a request: a header value holding 0x00 (`nul`) or another byte it refuses
#: there as `Invalid header value char`, 0x01 to 0x1F but the tab, the LF and the CR, and 0x7F
#: (`control`), a `Transfer-Encoding` that is empty or only spaces and tabs (`blank`), one that is
#: `identity` beside a `Content-Length` (`identity`), and a request line an LF in the target ends,
#: which leaves it with no version (`versionless`).
Cause = Literal["nul", "control", "blank", "identity", "versionless"]


class Refused(NamedTuple):
    """A request httptools refused, as far as it had arrived when it did."""

    cause: Cause
    #: The method and the target as httptools read them.
    method: str
    target: bytes
    #: The name of each header line, lowercased.
    names: frozenset[bytes]
    #: Whether its `Transfer-Encoding` came after a `Content-Length`.
    coding_after_length: bool


_GITHUB = "text/html; charset=utf-8"
_GOOGLE = "text/html; charset=UTF-8"
_BAD_REQUEST = "HTTP/1.1 400 Bad Request"
_NOT_IMPLEMENTED = "HTTP/1.1 501 Not Implemented"

#: A 400 with no body and no `Content-Type`.
EMPTY_400 = head_bounds.written(_BAD_REQUEST, None, b"")
#: Cloudflare's 501 page, `text/html` with no charset.
CLOUDFLARE_PAGE_501 = (
    b"<html>\r\n<head><title>501 Not Implemented</title></head>\r\n<body>\r\n"
    b"<center><h1>501 Not Implemented</h1></center>\r\n<hr><center>cloudflare</center>\r\n"
    b"</body>\r\n</html>\r\n"
)
#: The page Cloudflare sends a `GET` with no version, with no status line ahead of it.
CLOUDFLARE_PAGE_403 = (
    b"<html>\r\n<head><title>403 Forbidden</title></head>\r\n<body>\r\n"
    b"<center><h1>403 Forbidden</h1></center>\r\n<hr><center>cloudflare</center>\r\n"
    b"</body>\r\n</html>\r\n"
)

#: Graph's 501 page, 343 bytes.
GRAPH_CODING_NOT_SUPPORTED = request_target.graph_page(
    501,
    "Not Implemented",
    "Not Implemented",
    "The request transfer encoding type is not supported.",
)


def _github(refused: Refused, path: str, query: str) -> bytes | None:
    """Its 400 page, but for `control` from a request that sent no `Authorization`,
    :data:`EMPTY_400` carrying the request id."""
    if refused.cause == "control":
        if b"authorization" in refused.names:
            return None
        request_id = github.request_id(refused.method, path, query)
        return head_bounds.written(_BAD_REQUEST, None, b"", {github.REQUEST_ID_HEADER: request_id})
    if refused.cause == "blank" and refused.coding_after_length:
        return None
    return head_bounds.written(_BAD_REQUEST, _GITHUB, request_target.GITHUB_PAGE)


def _slack(refused: Refused) -> bytes | None:
    """The 400 :mod:`backlot.request_target` answers on Slack, but a 501 for `blank` from a request
    that sent no `Content-Length`."""
    if refused.cause == "versionless":
        return None
    if refused.cause == "blank" and b"content-length" not in refused.names:
        return head_bounds.written(_NOT_IMPLEMENTED, "text/plain", b"Not Implemented")
    return head_bounds.written(_BAD_REQUEST, "text/plain", b"Bad Request")


def _google(refused: Refused) -> bytes | None:
    """The 54-byte 400 page on `HTTP/1.0`."""
    if refused.cause in ("identity", "versionless"):
        return None
    return head_bounds.written("HTTP/1.0 400 Bad Request", _GOOGLE, request_target.GOOGLE_PAGE_54)


def _atlassian(refused: Refused, raw_path: str) -> bytes | None:
    """The edge's page naming `http1.invalid_characters` for `control`, and the CDN's page, 400 for
    `nul`, 501 for `blank` and 505 with no status line for `versionless`."""
    if refused.cause == "control":
        page = errors.atlassian.edge_page("http1.invalid_characters")
        return head_bounds.written(_BAD_REQUEST, "text/html", page.encode())
    for cause, status, status_line in (
        ("nul", 400, _BAD_REQUEST),
        ("blank", 501, _NOT_IMPLEMENTED),
        ("versionless", 505, None),
    ):
        if refused.cause == cause:
            page = errors.atlassian.cdn_page(status, refused.method, raw_path).encode()
            if status_line is None:
                return page
            return head_bounds.written(status_line, "text/html", page)
    return None


def _cloudflare(refused: Refused) -> bytes:
    """:data:`EMPTY_400` for `control`, :data:`CLOUDFLARE_PAGE_501` for `blank`, and
    ``backlot.request_target.CLOUDFLARE_PAGE`` for the rest; for `versionless` with no status line,
    and :data:`CLOUDFLARE_PAGE_403` in its place on a `GET`."""
    if refused.cause == "control":
        return EMPTY_400
    if refused.cause == "blank":
        return head_bounds.written(_NOT_IMPLEMENTED, "text/html", CLOUDFLARE_PAGE_501)
    if refused.cause == "versionless":
        return CLOUDFLARE_PAGE_403 if refused.method == "GET" else request_target.CLOUDFLARE_PAGE
    return head_bounds.written(_BAD_REQUEST, "text/html", request_target.CLOUDFLARE_PAGE)


def _s3(refused: Refused, path: str) -> bytes | None:
    """:data:`EMPTY_400` for `nul` and `blank`, and the 505 `HttpVersionNotSupported` for
    `versionless`."""
    if refused.cause in ("nul", "blank"):
        return EMPTY_400
    if refused.cause == "versionless":
        body = request_target.s3_version_not_supported(refused.method, path, refused.target)
        return head_bounds.written(
            "HTTP/1.1 505 HTTP Version not supported", "application/xml", body
        )
    return None


def _graph(refused: Refused) -> bytes | None:
    """``backlot.request_target.GRAPH_BADLY_FORMED`` for a byte in a header value, and
    :data:`GRAPH_CODING_NOT_SUPPORTED` for a `Transfer-Encoding`."""
    if refused.cause in ("nul", "control"):
        page = request_target.GRAPH_BADLY_FORMED
        return head_bounds.written(_BAD_REQUEST, head_bounds.GRAPH_TYPE, page)
    if refused.cause in ("blank", "identity"):
        page = GRAPH_CODING_NOT_SUPPORTED
        return head_bounds.written(_NOT_IMPLEMENTED, head_bounds.GRAPH_TYPE, page)
    return None


def answer(refused: Refused) -> bytes | None:
    """What the front door the path in ``refused.target`` names writes, or ``None`` where it reads
    the request on (the module docstring says where) or no vendor owns the path."""
    raw_path, _, raw_query = refused.target.partition(b"?")
    path = urllib.parse.unquote(raw_path.decode("latin-1"))
    door = (request_target.front_door(path) or (None, ""))[0]
    if door == "github":
        return _github(refused, path, raw_query.decode("latin-1"))
    if door == "slack":
        return _slack(refused)
    if door == "google":
        return _google(refused)
    if door == "atlassian":
        return _atlassian(refused, raw_path.decode("latin-1"))
    if door == "cloudflare":
        return _cloudflare(refused)
    if door == "s3":
        return _s3(refused, path)
    if door == "graph":
        return _graph(refused)
    return None
