"""What each vendor's front door answers a `Host` in brackets that holds no IPv6 address, with a
port after the brackets or none: `[0:0]`, `[x]`, `[1.2.3.4]`, `[v1.x]`.

Starlette builds ``request.url`` from such a `Host` where the brackets hold only hex digits, colons
and dots, and Python's ``urlsplit`` refuses the URL, so it cannot be left to the layers that read
the URL. :func:`answer` gives the front door's answer, which
``backlot.main.answer_a_host_in_brackets_that_holds_no_ipv6_address`` sends ahead of every
middleware but telemetry's. Where the front door reads the request on, on S3 and on a path no vendor
owns, the request goes on with the server's own address as its `Host`, which is what Starlette
builds the URL from where the `Host` does not parse, from 1.7.0.

Measured on 2026-10-30 (UTC) over raw HTTP/1.1 on TLS sockets, on a path each host serves (on S3,
the bucket list or a bucket that does not exist), with the vendors' credentials but Gmail's, the
Docs editors', S3's and Graph's: `[0:0]`, `[0:0]:443`, `[x]`, `[1.2.3.4]`, `[v1.x]`, `[:]` and
`[g::1]`, each answered as :func:`answer` says, `[0:0]` and `[v1.x]` at Google's `/batch/drive/v3`,
and `[0:0]` at GitHub's `/` and after a `User-Agent`. An IPv6 address in brackets (`[::1]`,
`[::1]:443`, `[::]`, `[ABCD::1]`, `[::ffff:1.2.3.4]`) is how a client reaches a server on an IPv6
address, and is served here as any other `Host` is. Real's front doors answered it as they answered
`example.com`, a host they do not serve, but for two: S3 answered it as it answers its own `Host`,
and Graph refused it as it refuses the `Host`s above, where it served `example.com`.
"""

from __future__ import annotations

import ipaddress
import re

from fastapi import Response
from starlette.types import Scope

from backlot import errors, request_target

#: A `Host` in brackets, with a port after them or none.
_BRACKETED = re.compile(r"\[(?P<inside>[^\]]*)\](?::[0-9]+)?")

#: What Cloudflare reads as an address in brackets that is not IPv6's (RFC 3986's IPvFuture), a
#: host it does not serve: a lower-case `v`, one hex digit, a `.`, then letters, digits, `-`, `_`
#: and `:` in runs one `.` parts, with a `.` after them or none. It answered any other `Host` in
#: brackets as it answers `[x]`, and so did every other front door `[v1.x]`. Measured on Notion with
#: each byte from 0x21 to 0x7E but `]` after `[v1.`, after `[v1.x` and in place of the `.`, and on
#: HubSpot, Linear and Fireflies with `[v1.]`, `[v1..]`, `[vAb.x]`, `[vF.x]`, `[v1.x.y.z]` and
#: `[V1.x]`.
_IPVFUTURE = re.compile(r"v[0-9A-Fa-f]\.(?:[-0-9:A-Z_a-z]+(?:\.[-0-9:A-Z_a-z]+)*\.?)?")

#: Cloudflare's answer to a host it does not serve: `text/html` with no charset, 151 bytes.
CLOUDFLARE_FORBIDDEN = (
    b"<html>\r\n<head><title>403 Forbidden</title></head>\r\n<body>\r\n"
    b"<center><h1>403 Forbidden</h1></center>\r\n<hr><center>cloudflare</center>\r\n"
    b"</body>\r\n</html>\r\n"
)


def _host(scope: Scope) -> str | None:
    """The first `Host` the request carries, the one Starlette reads, or ``None``."""
    for name, value in scope["headers"]:
        if name == b"host":
            return value.decode("latin-1")
    return None


def _inside(scope: Scope) -> str | None:
    """What the brackets of a `Host` in brackets hold where it is no IPv6 address, or ``None``."""
    host = _host(scope)
    match = _BRACKETED.fullmatch(host) if host is not None else None
    if match is None:
        return None
    try:
        ipaddress.IPv6Address(match["inside"])
    except ValueError:
        return match["inside"]
    return None


def holds_no_address(scope: Scope) -> bool:
    """Whether the request's `Host` is in brackets that hold no IPv6 address."""
    return _inside(scope) is not None


def _page(status: int, body: bytes, media_type: str | None, close: bool) -> Response:
    """``body`` under exactly ``media_type``, none where it is ``None``, and `Connection: close`
    where ``close``: real closed the connection after the answers here that say so, and read a
    second request on it after the others (2026-10-30, a `Host` of `[0:0]` and of `[v1.x]` sent with
    no `Connection` header)."""
    headers = {"Content-Type": media_type} if media_type is not None else {}
    if close:
        headers["Connection"] = "close"
    return Response(body, status_code=status, headers=headers)


def answer(scope: Scope) -> Response | None:
    """The front door's answer to a request whose `Host` :func:`holds_no_address`, or ``None`` where
    it reads the request on as one with any other `Host`.

    GitHub's 400 page, closing the connection; Slack's 404 with no body and no `Content-Type`; the
    CDN's 403 page in front of Jira and Confluence; Google's 1,555-byte 400 page on each of its
    hosts; on Notion, HubSpot, Linear and Fireflies, Cloudflare's 400 page, closing the connection,
    or for an IPvFuture (:data:`_IPVFUTURE`) its 403 page; Graph's page naming the hostname invalid,
    closing the connection. S3 answered each request as it answers the same request with its own
    `Host`."""
    path = scope["path"]
    if _under(path, "/github"):
        return _page(400, request_target.GITHUB_PAGE, "text/html; charset=utf-8", close=True)
    if _under(path, "/slack"):
        return _page(404, b"", None, close=False)
    if errors.atlassian.owns(path):
        raw_path = scope["raw_path"].decode("latin-1")
        page = errors.atlassian.cdn_refused_host(scope["method"], raw_path)
        return _page(403, page.encode(), "text/html", close=False)
    if errors.google.owns(path) or _under(path, "/batch"):
        return _page(400, request_target.GOOGLE_PAGE_1555, "text/html; charset=UTF-8", close=False)
    if any(_under(path, p) for p in ("/notion", "/hubspot", "/linear", "/fireflies")):
        if _IPVFUTURE.fullmatch(_inside(scope) or ""):
            return _page(403, CLOUDFLARE_FORBIDDEN, "text/html", close=False)
        return _page(400, request_target.CLOUDFLARE_PAGE, "text/html", close=True)
    if _under(path, "/msgraph"):
        page = request_target.GRAPH_INVALID_HOSTNAME
        return _page(400, page, "text/html; charset=us-ascii", close=True)
    return None


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")
