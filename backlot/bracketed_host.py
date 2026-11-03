"""What each vendor's front door answers a `Host` in brackets that holds no IPv6 address, with a
port after the brackets or none: `[0:0]`, `[x]`, `[1.2.3.4]`, `[v1.x]`.

Starlette builds ``request.url`` from such a `Host` where the brackets hold hex digits or none, a
colon, then hex digits, colons and dots (`[0:0]`), and Python's ``urlsplit`` refuses the URL, so it
cannot be left to the layers that read the URL. :func:`answer` gives the front door's answer, which
``backlot.main.answer_a_host_in_brackets_that_holds_no_ipv6_address`` sends ahead of every
middleware but telemetry's. Where the front door reads the request on, on S3 and on a path no vendor
owns, or refuses its target or method ahead of its `Host` (:func:`_refused_ahead`), the request goes
on with the server's own address as its `Host`, which is what Starlette builds the URL from where
the `Host` does not parse, from 1.7.0.

Measured on 2026-10-30 (UTC) over raw HTTP/1.1 on TLS sockets, on a path each host serves (on S3,
the bucket list or a bucket that does not exist), with the vendors' credentials but Gmail's, the
Docs editors', S3's and Graph's: `[0:0]`, `[0:0]:443`, `[x]`, `[1.2.3.4]`, `[v1.x]`, `[:]` and
`[g::1]`, each answered as :func:`answer` says, `[0:0]` and `[v1.x]` at Google's `/batch/drive/v3`,
and `[0:0]` at GitHub's `/` and after a `User-Agent`. On 2026-10-31 `[0:0]` at Confluence's search
drew the CDN's 403 under a CQL the search parses and under one it refuses, and on 2026-11-02 `[0:0]`
and `[v1.x]` at Confluence's space list and Jira's `serverInfo` drew it beside
`Content-Type: text/plain;charset=`, which draws Confluence's empty 500 under the tenant's own
`Host`. An IPv6 address in brackets (`[::1]`, `[::1]:443`, `[::]`, `[ABCD::1]`, `[::ffff:1.2.3.4]`)
is how a client reaches a server on an IPv6 address, and is served here as any other `Host` is.
Real's front doors answered it as they answered `example.com`, a host they do not serve, but for
two: S3 answered it as it answers its own `Host`, and Graph refused it as it refuses the `Host`s
above, where it served `example.com`.
"""

from __future__ import annotations

import ipaddress
import re

from fastapi import Response
from starlette.types import Scope

from backlot import errors, request_target
from backlot.routers import github

#: A `Host` in brackets, with a port after them or none.
_BRACKETED = re.compile(r"\[(?P<inside>[^\]]*)\](?::[0-9]+)?")

#: What Cloudflare reads as an address in brackets that is not IPv6's (RFC 3986's IPvFuture), a
#: host it does not serve: a lower-case `v`, one hex digit, a `.`, then nothing or runs of letters,
#: digits, `-`, `_` and `:` that single `.`s separate, with a `.` after them or none. It answered
#: any other `Host` in brackets as it answers `[x]`, and so did every other front door `[v1.x]`.
#: Measured on 2026-10-30 (UTC) on Notion with each byte from 0x21 to 0x7E but `]` after `[v1.`,
#: after `[v1.x` and in place of the `.`, and on HubSpot, Linear and Fireflies with `[v1.]`,
#: `[v1..]`, `[vAb.x]`, `[vF.x]`, `[v1.x.y.z]` and `[V1.x]`.
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


#: The kinds of raw byte in the target (``backlot.request_target.kinds_sent``) each front door
#: refuses ahead of the `Host`, as it refuses them under its own
#: (``backlot.request_target.answer``); beside the others it answers the `Host`. GitHub's answer to
#: each kind is its answer to the `Host`, and S3 reads the request on.
_RAW_AHEAD = {
    "slack": {"nul", "tab", "cr", "space", "control", "high"},
    "google": {"nul", "tab", "cr", "space", "control"},
    "graph": {"nul", "tab", "cr", "space", "control"},
    "atlassian": {"nul", "cr"},
    "cloudflare": {"nul", "cr"},
}


def _refused_ahead(scope: Scope) -> bool:
    """Whether the front door refuses the request's target or method ahead of its `Host`, as it
    refuses them under its own: a raw byte of :data:`_RAW_AHEAD`; a target past GitHub's bound
    (``backlot.routers.github.target_past_its_bound``); a `%00` on Slack, Notion and HubSpot
    (``backlot.request_target.escape_answer``); in front of Jira and Confluence the CDN's 400s and
    405s (``backlot.errors.atlassian.cdn_refusal``), where its 403s come after the `Host`'s; and on
    Linear and Fireflies a path Cloudflare refuses (``backlot.request_target.refused_path``), one
    the end cuts in a `%` (``backlot.request_target.cut_escape``) and `TRACE`.

    Measured on 2026-11-03 (UTC) beside `[0:0]` and `[v1.x]`, most beside `[x]` too, and beside the
    door's own `Host`. Each front door answered a raw byte of each kind in the path as the table
    :data:`_RAW_AHEAD` says, and so did Slack, Drive, Jira, Notion, Linear and Graph one after the
    path's `?`; GitHub answered a target of 8,214 bytes with its 414. Jira and Confluence answered
    `%00`, `%zz`, `TRACE` and `CONNECT`, and Jira `MKCALENDAR` and `UNSUBSCRIBE`, as they did under
    the tenant's `Host`, and `PROPFIND`, `PATCH`, `/..;/`, a `%3Cscript%3E` segment and a `;` with
    the CDN's 403 for the `Host`. Linear and Fireflies answered `%00`, `%zz`, `%2` at the end,
    `/../graphql` and `TRACE` as under their own `Host`, and `CONNECT` and `PURGE` as the `Host`
    says. GitHub and Graph answered the `Host` beside `%00`, Slack beside a broken escape, and each
    host but S3 beside `%0A`, `%80`, a raw `\\` and a raw `"`."""
    path, method, sent = scope["path"], scope["method"], scope["raw_path"]
    door = (request_target.front_door(path) or (None, None))[0]
    if request_target.kinds_sent(scope) & _RAW_AHEAD.get(door, set()):
        return True
    if door == "github":
        return github.target_past_its_bound(scope)
    if _under(path, "/slack") or any(_under(path, p) for p in ("/notion", "/hubspot")):
        return b"%00" in sent
    if errors.atlassian.owns(path):
        query = scope["query_string"].decode("latin-1")
        refusal = errors.atlassian.cdn_refusal(method, sent.decode("latin-1"), query)
        return refusal is not None and refusal.status != 403
    if any(_under(path, p) for p in ("/linear", "/fireflies")):
        own = b"/" + sent.partition(b"/")[2].partition(b"/")[2]
        query_or_none = scope["query_string"] or None
        return (
            method == "TRACE"
            or request_target.refused_path(own, query_or_none)
            or request_target.cut_escape(own, query_or_none)
        )
    return False


def answer(scope: Scope) -> Response | None:
    """The front door's answer to a request whose `Host` :func:`holds_no_address`, or ``None`` where
    it reads the request on as one with any other `Host` or refuses its target or method first
    (:func:`_refused_ahead`).

    GitHub's 400 page, closing the connection; Slack's 404 with no body and no `Content-Type`; the
    CDN's 403 page in front of Jira and Confluence; Google's 1,555-byte 400 page on each of its
    hosts; on Notion, HubSpot, Linear and Fireflies, Cloudflare's 400 page, closing the connection,
    or for an IPvFuture (:data:`_IPVFUTURE`) its 403 page; Graph's page naming the hostname invalid,
    closing the connection. S3 answered each request as it answers the same request with its own
    `Host`."""
    path = scope["path"]
    if _refused_ahead(scope):
        return None
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
