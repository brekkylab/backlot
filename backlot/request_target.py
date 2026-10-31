"""What each vendor's front door answers a request whose target holds a raw byte from 0x00 to 0x20
or from 0x7F to 0xFF, other than the LF that ends the line.

:mod:`backlot.protocol` percent-encodes those bytes before uvicorn's parser reads the line and puts
the target as sent on the scope under :data:`SENT`, where :func:`answer` reads it. A front door
either refuses such a target with an answer of its own or reads it as a request the app then
answers: the bytes percent-encoded, or on S3 a path's high bytes as `?` (:func:`_s3`). Which one,
and with what, turns on the kind of each byte (:func:`_kind`), on whether it is in the path or the
query, and on the other bytes beside it. A refusal here carries the front door's status, media
type and body, but for the Atlassian edge's, whose body is a stub (:func:`_atlassian`). Of its
other headers only the Atlassian ones Backlot serves on the same layer's other answers are sent,
not `server`, `CF-RAY` or the S3 ids.

Measured on 2026-10-23 over raw HTTP/1.1 on TLS sockets, with the vendors' credentials but Slides',
S3's and Graph's, on a path no route serves (`…/nope<byte>x`) and a query value (`?b=<byte>x`), with
a cache-buster beside them but on Notion: each kind alone (0x80, 0xE9, 0xFF, `C3 A9`, `E2 82 AC`,
0x01, 0x1F, 0x7F, 0x00, 0x09, 0x0D, 0x20), each ordered pair of `C3 A9`, 0x80, 0x01, 0x00, 0x09,
0x0D and 0x20 in the path and in the query, and eleven pairs split between the two. Every pair was
answered as one of its two bytes alone; the rules below are which one. Jira Cloud answered one pair
both ways: 0x09 and then 0x80 in the path was the 400 0x80 alone gets in one sweep and the tab's
page, as the rules give, in a later one.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse

from fastapi import Request, Response
from starlette.types import Scope

from backlot import auth, errors, utf8_escapes
from backlot.routers import atlassian, s3

#: The scope key :mod:`backlot.protocol` leaves the request target on, as sent, where it
#: percent-encoded a byte of it.
SENT = "raw_request_target"

#: The scope key a rule sets where the front door reads the target as another request and closes
#: the connection after answering it (:func:`_s3`).
CLOSE = "raw_request_target_closes"

#: The scope key :mod:`backlot.protocol` leaves the request target on, as sent, where it holds a
#: `#`. uvicorn's parser reads a `#` as the start of a fragment and leaves it out of the path and
#: the query, with what comes after it, a `?` among that (:func:`printable_answer`).
WITH_A_FRAGMENT = "request_target_with_a_fragment"

#: The scope key :mod:`backlot.protocol` sets where a `?` or a `#` comes after the path in the
#: request target. The scope's `query_string` is empty for a `?` with nothing after it as for none,
#: and holds no `#` (:func:`path_ends_the_target`).
FOLLOWED = "request_target_path_followed"

_RAW = re.compile(rb"[\x00-\x20\x7f-\xff]")


def _parts(line: bytes) -> tuple[bytes, bytes, bytes] | None:
    """The method, the target and the version of ``line``, a request line ending in its LF, or
    ``None`` where it is not `METHOD target HTTP/x.y`.

    The target is what lies between the first space and the last one, so a space inside it is
    one of its bytes, and the version after the last one keeps the CR of a CRLF."""
    method, _, rest = line[:-1].partition(b" ")
    target, _, version = rest.rpartition(b" ")
    if not (method and version.startswith(b"HTTP/")):
        return None
    return method, target, version


def with_a_fragment(line: bytes) -> bytes | None:
    """The target of ``line`` as sent (:func:`_parts`) where it holds a `#`, or ``None``."""
    parts = _parts(line)
    return parts[1] if parts is not None and b"#" in parts[1] else None


def encode_line(line: bytes) -> tuple[bytes, bytes | None]:
    """``line``, a request line ending in its LF, with each byte of :data:`_RAW` in its target
    (:func:`_parts`) percent-encoded, and the target as sent; or ``line`` and ``None`` where the
    target holds none of them or the line is not `METHOD target HTTP/x.y`."""
    parts = _parts(line)
    if parts is None:
        return line, None
    method, sent, version = parts
    encoded = _RAW.sub(lambda m: b"%%%02X" % m[0][0], sent)
    if encoded == sent:
        return line, None
    return b"%s %s %s\n" % (method, encoded, version), sent


def _kind(byte: int) -> str:
    """`nul`, `tab`, `cr` and `space` for 0x00, 0x09, 0x0D and 0x20, `high` for 0x80 to 0xFF, and
    `control` for the rest of :data:`_RAW` a target can hold, an LF ending the line: 0x01 to 0x08,
    0x0B, 0x0C, 0x0E to 0x1F and 0x7F."""
    return {0x00: "nul", 0x09: "tab", 0x0D: "cr", 0x20: "space"}.get(
        byte, "high" if byte >= 0x80 else "control"
    )


def _kinds(part: bytes) -> list[str]:
    """The kind of each raw byte in ``part``, in the order they come."""
    return [_kind(m[0][0]) for m in _RAW.finditer(part)]


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


#: GitHub's answer to every kind, in the path and in the query: 400, `text/html; charset=utf-8`,
#: 1,440 bytes.
GITHUB_PAGE = (
    b'\r\n<html>\r\n  <head>\r\n    <meta content="origin" name="referrer">\r\n'
    b"    <title>Bad request &middot; GitHub</title>\r\n"
    b'    <meta name="viewport" content="width=device-width">\r\n'
    b'    <style type="text/css" media="screen">\r\n      body {\r\n'
    b"        background-color: #f6f8fa;\r\n        color: rgba(0, 0, 0, 0.5);\r\n"
    b"        font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif,"
    b"Apple Color Emoji,Segoe UI Emoji,Segoe UI Symbol;\r\n"
    b"        font-size: 14px;\r\n        line-height: 1.5;\r\n      }\r\n"
    b"      .c { margin: 50px auto; max-width: 600px; text-align: center; padding: 0 24px; }\r\n"
    b"      a { text-decoration: none; }\r\n      a:hover { text-decoration: underline; }\r\n"
    b"      h1 { color: #24292e; line-height: 60px; font-size: 48px; font-weight: 300; "
    b"margin: 0px; }\r\n"
    b"      p { margin: 20px 0 40px; }\r\n      #s { margin-top: 35px; }\r\n      #s a {\r\n"
    b"        color: #666666;\r\n        font-weight: 200;\r\n        font-size: 14px;\r\n"
    b"        margin: 0 10px;\r\n      }\r\n    </style>\r\n  </head>\r\n  <body>\r\n"
    b'    <div class="c">\r\n      <h1>Whoa there!</h1>\r\n'
    b"      <p>You have sent an invalid request. <br><br>\r\n"
    b"        Please do not send this request again.\r\n      </p>\r\n"
    b'      <div id="s">\r\n'
    b'        <a href="https://support.github.com">Contact Support</a> &mdash;\r\n'
    b'        <a href="https://githubstatus.com">GitHub Status</a> &mdash;\r\n'
    b'        <a href="https://twitter.com/githubstatus">@githubstatus</a>\r\n'
    b"      </div>\r\n    </div>\r\n  </body>\r\n</html>\r\n"
)

#: Google's two 400 pages, the same on each of its five hosts, both `text/html; charset=UTF-8`
#: (:func:`_google` says which answers what).
GOOGLE_PAGE_54 = b"<html><title>Error 400 (Bad Request)!!1</title></html>"
GOOGLE_PAGE_1555 = (
    b"<!DOCTYPE html>\n<html lang=en>\n  <meta charset=utf-8>\n"
    b'  <meta name=viewport content="initial-scale=1, minimum-scale=1, width=device-width">\n'
    b"  <title>Error 400 (Bad Request)!!1</title>\n  <style>\n"
    b"    *{margin:0;padding:0}html,code{font:15px/22px arial,sans-serif}html{background:#fff;"
    b"color:#222;padding:15px}body{margin:7% auto 0;max-width:390px;min-height:180px;"
    b"padding:30px 0 15px}* > body{background:url(//www.google.com/images/errors/robot.png) "
    b"100% 5px no-repeat;padding-right:205px}p{margin:11px 0 22px;overflow:hidden}ins{color:#777;"
    b"text-decoration:none}a img{border:0}@media screen and (max-width:772px){body{background:"
    b"none;margin-top:0;max-width:none;padding-right:0}}#logo{background:url(//www.google.com/"
    b"images/branding/googlelogo/1x/googlelogo_color_150x54dp.png) no-repeat;margin-left:-5px}"
    b"@media only screen and (min-resolution:192dpi){#logo{background:url(//www.google.com/"
    b"images/branding/googlelogo/2x/googlelogo_color_150x54dp.png) no-repeat 0% 0%/100% 100%;"
    b"-moz-border-image:url(//www.google.com/images/branding/googlelogo/2x/"
    b"googlelogo_color_150x54dp.png) 0}}@media only screen and (-webkit-min-device-pixel-ratio:2)"
    b"{#logo{background:url(//www.google.com/images/branding/googlelogo/2x/"
    b"googlelogo_color_150x54dp.png) no-repeat;-webkit-background-size:100% 100%}}#logo{display:"
    b"inline-block;height:54px;width:150px}\n  </style>\n"
    b"  <a href=//www.google.com/><span id=logo aria-label=Google></span></a>\n"
    b"  <p><b>400.</b> <ins>That\xe2\x80\x99s an error.</ins>\n"
    b"  <p>Your client has issued a malformed or illegal request.  "
    b"<ins>That\xe2\x80\x99s all we know.</ins>\n"
)

#: The page Cloudflare, in front of Notion, HubSpot, Linear and Fireflies, answers several requests
#: with: `text/html` with no charset, CRLF line ends, 155 bytes.
CLOUDFLARE_PAGE = (
    b"<html>\r\n<head><title>400 Bad Request</title></head>\r\n<body>\r\n"
    b"<center><h1>400 Bad Request</h1></center>\r\n<hr><center>cloudflare</center>\r\n"
    b"</body>\r\n</html>\r\n"
)

#: Graph's 400 pages, 324, 311 and 334 bytes on `text/html; charset=us-ascii` (:func:`_graph` says
#: which of the first two answers what, ``backlot.bracketed_host`` what the third answers).
_GRAPH_PAGE = (
    '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN""http://www.w3.org/TR/html4/strict.dtd">\r\n'
    "<HTML><HEAD><TITLE>Bad Request</TITLE>\r\n"
    '<META HTTP-EQUIV="Content-Type" Content="text/html; charset=us-ascii"></HEAD>\r\n'
    "<BODY><h2>{title}</h2>\r\n<hr><p>HTTP Error 400. {detail}</p>\r\n</BODY></HTML>\r\n"
)
GRAPH_INVALID_URL = _GRAPH_PAGE.format(
    title="Bad Request - Invalid URL", detail="The request URL is invalid."
).encode()
GRAPH_BADLY_FORMED = _GRAPH_PAGE.format(
    title="Bad Request", detail="The request is badly formed."
).encode()
GRAPH_INVALID_HOSTNAME = _GRAPH_PAGE.format(
    title="Bad Request - Invalid Hostname", detail="The request hostname is invalid."
).encode()


def _page(
    body: bytes, media_type: str | None, status: int = 400, close: bool = True, **headers: str
) -> Response:
    """``body`` under exactly ``media_type``: a `text/` type passed as ``media_type`` would gain the
    charset Starlette names on one. ``close`` ends the connection after it: on 2026-10-23 real
    closed it after each front door's refusal here, on api.github.com, slack.com,
    www.googleapis.com, Jira Cloud, api.notion.com, api.linear.app, S3 and Graph, but after
    Cloudflare's empty 400 and Graph's, where a second request on the same connection was answered.
    uvicorn closes it after its own 400 as well."""
    if media_type is not None:
        headers["Content-Type"] = media_type
    if close:
        headers["Connection"] = "close"
    return Response(body, status_code=status, headers=headers)


def _github() -> Response:
    return _page(GITHUB_PAGE, "text/html; charset=utf-8")


def _slack() -> Response:
    """envoy's 400, `text/plain` with no charset, 11 bytes, for every kind in either place."""
    return _page(b"Bad Request", "text/plain")


def _google(path_kinds: list[str], query_kinds: list[str]) -> Response | None:
    """The 54-byte page where a `nul` or a `control` is anywhere, then the 1,555-byte one where a
    `tab`, a `cr` or a `space` is, and a `high` read as its percent-encoding."""
    every = set(path_kinds) | set(query_kinds)
    if every & {"nul", "control"}:
        return _page(GOOGLE_PAGE_54, "text/html; charset=UTF-8")
    if every & {"tab", "cr", "space"}:
        return _page(GOOGLE_PAGE_1555, "text/html; charset=UTF-8")
    return None


#: A `%` with fewer than two hex digits after it.
_BROKEN_ESCAPE = re.compile(rb"%(?![0-9A-Fa-f]{2})")
#: A `%` that ends the path with one hex digit after it or none.
_CUT_ESCAPE = re.compile(rb"%[0-9A-Fa-f]?\Z")


def path_ends_the_target(scope: Scope) -> bool:
    """Whether the path ends the request target as sent, no `?` or `#` coming after it
    (:data:`FOLLOWED`)."""
    return not (scope["query_string"] or scope.get(FOLLOWED))


def cut_escape(path: bytes, ends_the_target: bool) -> bool:
    """Whether ``path``, the vendor's path as sent, is one Cloudflare reads as cut short by the end
    of the target (:data:`_CUT_ESCAPE`) and answers otherwise than each other broken escape, where
    the path ``ends_the_target`` (:func:`path_ends_the_target`). A `?` or a `#` after the `%` is no
    end: on 2026-10-31 (UTC) a `?` there, with a `#` after it or none, and a `#` drew
    :data:`CLOUDFLARE_PAGE` on Linear, Fireflies, Notion and HubSpot
    (``tests/test_request_target.py`` holds the cases)."""
    cut = _CUT_ESCAPE.search(path)
    return ends_the_target and cut is not None and not _broken(path[: cut.start()])


def breaks_an_escape(part: bytes) -> bool:
    """Whether ``part`` holds a `%` two hex digits do not follow (:data:`_BROKEN_ESCAPE`)."""
    return _BROKEN_ESCAPE.search(part) is not None


def refused_path(path: bytes, ends_the_target: bool) -> bool:
    """Whether Cloudflare in front of Linear and Fireflies refuses ``path``, which
    ``ends_the_target`` or not (:func:`cut_escape`), with :data:`CLOUDFLARE_PAGE`: the path holds a
    `..` segment that climbs past the root (:func:`_climbs_past_the_root`), which both hosts refused
    so ahead of a cut escape after it (`/../a%2`, 2026-10-29, UTC), or it holds `%00` or a `%` two
    hex digits do not follow, but where :func:`cut_escape` holds. The page comes ahead of
    Cloudflare's answer to a raw byte: on 2026-10-26 (UTC) both hosts drew it for `/a%zz` with a raw
    `é` before or after it or `\\x01` before it, and for `/a%00` after a raw `é`, and on 2026-10-29
    for `/../a` with a raw `é` or a space after it, where a raw `é` alone drew the empty 400."""
    if _climbs_past_the_root(path):
        return True
    return _broken(path) and not cut_escape(path, ends_the_target)


def _broken(path: bytes) -> bool:
    return b"%00" in path or _BROKEN_ESCAPE.search(path) is not None


def _climbs_past_the_root(path: bytes) -> bool:
    """Whether, once each escape in ``path`` is decoded and each run of slashes is one, a `..`
    segment with a segment after it has no segment before it to take away. Both hosts refused
    `/../graphql`, `/x/../../`, `//../graphql`, `/x//../../graphql`, `/./../graphql`,
    `/%2e%2e/graphql`, `/.%2e/graphql` and `/..%2Fgraphql` on 2026-10-29 (UTC), and passed `/..`,
    `/x/../..`, `/x/..%2F..` and `/..?x=1` on to the vendor, and `/..\\graphql`, where a backslash
    is no slash to Cloudflare."""
    segments = re.sub(rb"/{2,}", b"/", urllib.parse.unquote_to_bytes(path)).split(b"/")[1:]
    depth = 0
    for at, segment in enumerate(segments, 1):
        if segment == b".." and depth == 0 and at < len(segments):
            return True
        if segment == b"..":
            depth = max(depth - 1, 0)
        elif segment != b".":
            depth += 1
    return False


def _cloudflare(scope: Scope, path_kinds: list[str], query_kinds: list[str]) -> Response | None:
    """:data:`CLOUDFLARE_PAGE` where a `nul` or a `cr` is anywhere, and where `Authorization` or
    one of the five conditional headers comes more than once
    (``auth.repeated_cloudflare_refused_header``), ahead of the bytes below; then an empty 400
    with no `Content-Type` for any raw byte in the query, and for one in a path that holds no
    `space`; and a path with a `space` in it read as its percent-encoding, whatever else the path
    holds."""
    every = set(path_kinds) | set(query_kinds)
    if every & {"nul", "cr"} or auth.repeated_cloudflare_refused_header(Request(scope)):
        return _page(CLOUDFLARE_PAGE, "text/html")
    if query_kinds or "space" not in path_kinds:
        return _page(b"", None, close=False)
    return None


def _atlassian(scope: Scope, path_kinds: list[str], query_kinds: list[str]) -> Response | None:
    """CloudFront's 400 page where a `nul` or a `cr` is anywhere, ahead of the edge; then, where
    ``errors.atlassian.cdn_refusal`` gives one for the target encoded, the CDN's own page, which
    ``backlot.main.answer_at_the_atlassian_front_door`` answers. The edge reads the query's bytes
    and, where the target holds no `space` at all, the path's: a `control` is its page naming
    `http1.invalid_characters`, and a `tab` where no `control` is, the one naming
    `http1.codec_error`. A `space`, a `high` and a path the edge does not read are read as their
    percent-encoding. Real's edge pages are 18,946 and 18,939 bytes on `text/html` with no charset,
    and the two here are :func:`backlot.errors.atlassian.edge_page`'s. The CDN's refusal of a method
    came ahead of the edge on 2026-10-23: `PROPFIND` was its 403 beside a `control` in the path and
    in the query, a `tab`, a `space` and a `high`, and `TRACE` its 405 beside a `control` and a
    `tab`; beside a `nul` or a `cr`, `PROPFIND` was the 400 for those."""
    every = set(path_kinds) | set(query_kinds)
    raw_path = scope["raw_path"].decode("latin-1")
    if every & {"nul", "cr"}:
        page = errors.atlassian.cdn_bad_request(scope["method"], raw_path)
        return _front_door(scope, "cdn", page, {})
    query = scope["query_string"].decode("latin-1")
    if errors.atlassian.cdn_refusal(scope["method"], raw_path, query) is not None:
        return None
    read = list(query_kinds) if "space" in every else path_kinds + query_kinds
    for kind, details in (("control", "http1.invalid_characters"), ("tab", "http1.codec_error")):
        if kind in read:
            page = errors.atlassian.edge_page(details)
            return _front_door(scope, "cdn edge", page, {"Access-Control-Allow-Origin": "*"})
    return None


def _front_door(scope: Scope, layer: str, page: str, headers: dict[str, str]) -> Response:
    """An Atlassian page from ``layer``, carrying what ``backlot.routers.atlassian.vendor_headers``
    puts on that layer's answers, as ``backlot.main.answer_at_the_atlassian_front_door`` leaves
    them to it."""
    scope["atlassian_front_door"] = layer
    headers |= atlassian.vendor_headers(Request(scope), 400)
    return _page(page.encode(), "text/html", **headers)


def _s3(scope: Scope, path_kinds: list[str], query_kinds: list[str]) -> Response | None:
    """An empty 400 where a `nul` or a `cr` is anywhere, then real's 505 where a `space` is, then an
    empty 400 again where a `tab` is. A `control` is read as its percent-encoding, and so is a
    `high` in the query; each `high` in the path is read as `?`, as most of real's front ends read
    one. Real's 400 `InvalidBucketName` named a bucket name sent with them, twenty times each on
    2026-10-23: `80` as `?` every time, `C3 A9` as `??` 17 times and `é` 3, `01 C3 A9` as `&#1;??`
    18 times and `&#1;é` 2. Where a `control` is, real closed the connection after its answer that
    day (:data:`CLOSE`): `nope<01>x`, `nope<7F>x`, `nope<1F>x`, `nope<01 80>x` and `?b=<01>x` were
    the 404 `NoSuchBucket` with `Connection: close`, where `nope%01x` and `nope<80>x` were the same
    404 on a connection that answered the next request."""
    every = set(path_kinds) | set(query_kinds)
    if every & {"nul", "cr"}:
        return _page(b"", None)
    if "space" in every:
        sent = scope[SENT]
        request_id = hashlib.sha256(b"s3-505:" + sent).hexdigest()[:16].upper()
        host_id = s3.request_ids(scope["method"], scope["path"], "")[1]
        body = (
            '<?xml version="1.0" encoding="UTF-8"?><Error><Code>HttpVersionNotSupported</Code>'
            "<Message>The HTTP version specified is not supported.</Message>"
            f"<RequestId>{request_id}</RequestId><HostId>{host_id}</HostId></Error>"
        )
        return _page(body.encode(), "application/xml", status=505)
    if "tab" in every:
        return _page(b"", None)
    if "control" in every:
        scope[CLOSE] = True
    if "high" in path_kinds:
        sent_path = scope[SENT].partition(b"?")[0]
        raw_path = _RAW.sub(lambda m: b"%3F" if m[0][0] >= 0x80 else b"%%%02X" % m[0][0], sent_path)
        scope["raw_path"] = raw_path
        scope["path"] = urllib.parse.unquote(raw_path.decode("ascii"))
    return None


def _graph(scope: Scope, path_kinds: list[str], query_kinds: list[str]) -> Response | None:
    """Of a `nul` or a `control` and a `tab`, a `cr` or a `space`, whichever comes first in the
    target picks the page: :data:`GRAPH_INVALID_URL` for the first two, :data:`GRAPH_BADLY_FORMED`
    for the other three. With none of them, a path whose `high` bytes are not UTF-8 is an empty 400,
    and every other `high` is read as its percent-encoding."""
    for kind in path_kinds + query_kinds:
        if kind in ("nul", "control"):
            return _page(GRAPH_INVALID_URL, "text/html; charset=us-ascii")
        if kind in ("tab", "cr", "space"):
            return _page(GRAPH_BADLY_FORMED, "text/html; charset=us-ascii")
    try:
        scope[SENT].partition(b"?")[0].decode("utf-8")
    except UnicodeDecodeError:
        return _page(b"", None, close=False)
    return None


def _drop_the_cut(scope: Scope) -> None:
    """Take the `%` that ends the scope's path, and the hex digit after it, off the path, as
    Cloudflare in front of Linear does where it reads a path with a space in it as its
    percent-encoding (:func:`_cloudflare`): Express's 404 behind it quoted `/a b%2`, `/a b%` and
    `/a b%0` as `/a%20b` on 2026-10-29 (UTC)."""
    cut = _CUT_ESCAPE.search(scope["raw_path"])
    scope["raw_path"] = scope["raw_path"][: cut.start()]
    scope["path"] = scope["path"][: cut.start() - cut.end()]


def answer(scope: Scope) -> Response | None:
    """The answer to a request whose target :mod:`backlot.protocol` encoded, or ``None`` where the
    vendor reads the target as another request, which the scope is left holding.

    Any other path, `/oauth2/token` and `/_meta` among them, gets the 400 uvicorn's own protocol
    answers such a target with."""
    path = scope["path"]
    sent_path, _, sent_query = scope[SENT].partition(b"?")
    in_path, in_query = _kinds(sent_path), _kinds(sent_query)
    if _under(path, "/github"):
        return _github()
    if _under(path, "/slack"):
        return _slack()
    if errors.google.owns(path) or _under(path, "/batch"):
        return _google(in_path, in_query)
    if errors.atlassian.owns(path):
        return _atlassian(scope, in_path, in_query)
    if any(_under(path, p) for p in ("/linear", "/fireflies")):
        own = b"/" + sent_path.partition(b"/")[2].partition(b"/")[2]
        ends = path_ends_the_target(scope)
        if refused_path(own, ends):
            return _page(CLOUDFLARE_PAGE, "text/html")
        refused = _cloudflare(scope, in_path, in_query)
        if refused is None and cut_escape(own, ends):
            _drop_the_cut(scope)
        return refused
    if any(_under(path, p) for p in ("/notion", "/hubspot")):
        # Cloudflare's page for a broken escape or a `%00` comes ahead of the bytes
        # (:func:`printable_answer`)
        own = b"/" + sent_path.partition(b"/")[2].partition(b"/")[2]
        if _broken(own) and not cut_escape(own, path_ends_the_target(scope)):
            return _page(CLOUDFLARE_PAGE, "text/html")
        return _cloudflare(scope, in_path, in_query)
    if errors.s3.owns(path):
        return _s3(scope, in_path, in_query)
    if _under(path, "/msgraph"):
        return _graph(scope, in_path, in_query)
    return _page(b"Invalid HTTP request received.", "text/plain; charset=utf-8")


#: An escaped byte from 0x00 to 0x1F, or 0x7F, its hex digits in either case.
_ESCAPED_CONTROL = re.compile(rb"%(?:[01][0-9A-Fa-f]|7[Ff])")
#: The prefixes under which Google's front door refuses a `%00`: on each of the four hosts of their
#: own, the API's; on www.googleapis.com, which Drive and the batch endpoint share, `/drive/v3/` and
#: `/batch/`. On 2026-10-30 (UTC) the four refused `%00` right after the prefix (`/gmail/v1%00x`,
#: `/v4%00x`, `/v1%00x`) and outside it (`/gmail%00x`, `/nope%00x`), where Backlot routes nothing,
#: and www.googleapis.com answered `/drive/v3%00x`, `/drive%00x`, `/batch%00x` and `/nope%00x` with
#: its 404 page and refused `/drive/v2/nope%00x`, under a version Backlot does not serve.
_GOOGLE_NUL = ("/gmail/v1", "/docs/v1", "/sheets/v4", "/slides/v1", "/drive/v3/", "/batch/")
#: The C1 controls Graph's front door refuses in a path once it has read the path's escapes.
_GRAPH_REFUSED = frozenset("\x81\x8d\x8f\x90\x9d")


def _graph_refuses(raw_path: bytes) -> bool:
    """Whether Graph's front door refuses ``raw_path``: one holding a character of
    :data:`_GRAPH_REFUSED` once its escapes are read, as UTF-8 where they decode to a string without
    U+10FFFE or U+10FFFF and byte by byte otherwise. Measured on 2026-10-30 (UTC) for each escape
    from `%80` to `%FF` alone, for `%C2` before each of `%80` to `%A0`, for sequences cut short,
    overlong or past U+10FFFF, and for U+FFFE, U+FFFF, U+10FFFD, U+10FFFE and U+10FFFF sent as
    UTF-8; a query holding `%80`, `%81`, `%ED%A0%80`, `%C3%A9` or `%F4%90%80%80` was read on."""
    raw = urllib.parse.unquote_to_bytes(raw_path)
    text = raw.decode("utf-8") if utf8_escapes.decodes(raw_path) else raw.decode("latin-1")
    if "\U0010fffe" in text or "\U0010ffff" in text:
        text = raw.decode("latin-1")
    return not _GRAPH_REFUSED.isdisjoint(text)


def escape_answer(scope: Scope) -> Response | None:
    """The front door's answer to a path that holds `%00`, or on Graph any escape of
    :data:`_ESCAPED_CONTROL` or one :func:`_graph_refuses`, or ``None`` where it reads the path on
    as the vendor's.

    slack.com's empty 400; Google's 1,555-byte page where :data:`_GOOGLE_NUL` says, on a connection
    that answers the next request; Cloudflare's page in front of Notion and HubSpot; S3's empty 400,
    whose status line real words `400 Invalid URI`; and Graph's page naming the URL invalid.
    Measured on 2026-10-30 (UTC) over raw HTTP/1.1 on TLS sockets, with the vendors' credentials
    but Gmail's, the Docs editors', S3's and Graph's, for each escape from `%00` to `%1F` and `%7F`
    at a path no route serves (`…/nope<escape>x`), and for `%00`, `%0A` and `%0D` at the end of one
    a route serves: each of the others was read as the path's other bytes are, on every host but
    Graph, and Graph refused the lower-case spellings `%0a`, `%0b`, `%0d`, `%1f` and `%7f` too. A
    `%00` in the query was read as its other bytes are on each of those hosts but Confluence, which
    answered Tomcat's 400 page. Jira Cloud's and Confluence's front door answers a `%00` in the
    path with the CDN's page (``backlot.errors.atlassian.cdn_refusal``), Linear's and Fireflies'
    with Cloudflare's (:func:`refused_path`), and api.github.com read it as any other byte."""
    path, sent = scope["path"], scope["raw_path"]
    if _under(path, "/msgraph"):
        if _ESCAPED_CONTROL.search(sent) is None and not _graph_refuses(sent):
            return None
        return _page(GRAPH_INVALID_URL, "text/html; charset=us-ascii")
    if b"%00" not in sent:
        return None
    if _under(path, "/slack"):
        return _page(b"", None)
    if path.startswith(_GOOGLE_NUL):
        return _page(GOOGLE_PAGE_1555, "text/html; charset=UTF-8", close=False)
    if any(_under(path, p) for p in ("/notion", "/hubspot")):
        return _page(CLOUDFLARE_PAGE, "text/html")
    if errors.s3.owns(path):
        return _page(b"", None)
    return None


#: Apache's 400 page in front of Slack's API: `text/html; charset=iso-8859-1`, 226 bytes.
SLACK_APACHE_PAGE = (
    b'<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">\n<html><head>\n'
    b"<title>400 Bad Request</title>\n</head><body>\n<h1>Bad Request</h1>\n"
    b"<p>Your browser sent a request that this server could not understand.<br />\n</p>\n"
    b"</body></html>\n"
)

#: Jetty's 400 page behind HubSpot's front door: `text/html;charset=iso-8859-1`, 215 bytes.
HUBSPOT_JETTY_PAGE = (
    b'<html>\n<head>\n<meta http-equiv="Content-Type" content="text/html;charset=utf-8"/>\n'
    b"<title>Error 400 Bad Request</title>\n</head>\n<body><h2>HTTP ERROR 400</h2>\n"
    b"<p>Reason:\n<pre>    Bad Request</pre></p>\n</body>\n</html>\n"
)

#: HubSpot's own 400 page. Real's is 19,237 bytes of `text/html; charset=utf-8`, most of them a
#: style sheet and an inline image; the stub here keeps its title, its words and its status code.
HUBSPOT_ERROR_PAGE = (
    b"<!DOCTYPE html> <html lang=en><meta charset=utf-8><title>Error</title>"
    b"<header><h4>Something went wrong</h4><p>There was an error loading the page.</p></header>"
    b"<div class=status-code>400</div>\n"
)

#: The printable characters but `%` that Graph's front door refuses in a path.
_GRAPH_REFUSED_PRINTABLE = re.compile(rb'["#<>]')
#: The path under which HubSpot's Jetty reads a `{` and a `;` (:func:`printable_answer`,
#: :func:`printable_reading`).
_HUBSPOT_OBJECTS = b"/hubspot/crm/v3/objects/"
#: The mounts under which a `\` in the path is read as a `/` (:func:`printable_reading`).
_BACKSLASH_MOUNTS = (b"/atlassian/", b"/drive/", b"/gmail/", b"/docs/", b"/sheets/", b"/slides/")


def printable_answer(scope: Scope) -> Response | None:
    """The front door's answer to a target holding a `#` (:data:`WITH_A_FRAGMENT`), a `%` two hex
    digits do not follow (:data:`_BROKEN_ESCAPE`), or on Graph and HubSpot another printable
    character it refuses, or ``None`` where it reads the target on.

    On api.github.com its page for a `#` anywhere, and on slack.com envoy's empty 400; then on
    slack.com Apache's page (:data:`SLACK_APACHE_PAGE`) for a broken escape in the path; on Graph
    :data:`GRAPH_INVALID_URL` for one, or for a `"`, a `#`, a `<` or a `>`, in the path; on S3 an
    empty 400 for one in the path, which reads a `#` as one of its characters, and whose status line
    real words `400 Invalid URI: EOF` where the path ends fewer than two characters after the `%`
    and `400 Invalid URI: isHexDigit` otherwise; on Notion and HubSpot Cloudflare's page for one in
    the path, but where the end of the target cuts it short (:func:`cut_escape`), which HubSpot
    answers with :data:`HUBSPOT_ERROR_PAGE` and Notion reads on (``notion.undecodable_url``); and on
    HubSpot Jetty's page for a `{` or a `}` in a path under `/crm/v3/objects/`.

    Measured on 2026-10-31 (UTC) over raw HTTP/1.1 on TLS sockets, with the vendors' credentials but
    S3's and Graph's, for each byte from 0x21 to 0x7E sent as itself and as its escape at a path no
    route serves (`…/nope<byte>x`) and in a query value (`?b=<byte>x`): each broken escape and each
    character here was refused in the path and read on in the query, but on api.github.com and
    slack.com, where a `#` was refused in either (api.github.com's answer to a broken escape in the
    query is ``backlot.main.refuse_a_broken_escape_in_a_github_query``'s). Then each spelling of a
    broken escape, a `#` and a broken escape in either order, some of them beside `%00` or a raw
    byte, and on slack.com, Notion and HubSpot some of them with no credential, and on
    api.github.com a `#` with none, each of which drew the answer it drew with one;
    ``tests/test_request_target.py`` holds the cases. Notion reads nothing past a `#`, and slack.com
    answers `%00` beside a broken escape with envoy's empty 400 (:func:`escape_answer`). On Notion
    and HubSpot, Cloudflare's page for a broken escape or a `%00` comes ahead of its answer to a raw
    byte (:func:`answer`), but for a `%` or a `%4` that ends the target after one, and on HubSpot a
    `{` beside one, which draw the raw byte's empty 400; slack.com answers a raw `\\x01` ahead of a
    `#` or a broken escape beside it, Graph gives a `\\x01` the page it gives a `"`, and S3 reads a
    raw byte on. The answers that do not close the connection are Apache's, Jetty's and HubSpot's
    own: each of those was followed by an answer to a second request on the same connection, and
    none of the others."""
    path, raw_path = scope["path"], scope["raw_path"]
    sent = scope.get(WITH_A_FRAGMENT)
    sent_path = raw_path if sent is None else sent.partition(b"?")[0]
    if _under(path, "/github"):
        return _github() if sent is not None else None
    if _under(path, "/slack"):
        if sent is not None:
            return _page(b"", None)
        if _BROKEN_ESCAPE.search(raw_path):
            return _page(SLACK_APACHE_PAGE, "text/html; charset=iso-8859-1", close=False)
        return None
    if _under(path, "/msgraph"):
        if _BROKEN_ESCAPE.search(sent_path) or _GRAPH_REFUSED_PRINTABLE.search(sent_path):
            return _page(GRAPH_INVALID_URL, "text/html; charset=us-ascii")
        return None
    if errors.s3.owns(path):
        return _page(b"", None) if _BROKEN_ESCAPE.search(sent_path) else None
    if any(_under(path, p) for p in ("/notion", "/hubspot")):
        own = b"/" + raw_path.partition(b"/")[2].partition(b"/")[2]
        if cut_escape(own, path_ends_the_target(scope)):
            if _under(path, "/notion"):
                return None
            return _page(HUBSPOT_ERROR_PAGE, "text/html; charset=utf-8", close=False)
        if _BROKEN_ESCAPE.search(own):
            return _page(CLOUDFLARE_PAGE, "text/html")
        if raw_path.startswith(_HUBSPOT_OBJECTS) and (b"{" in raw_path or b"}" in raw_path):
            return _page(HUBSPOT_JETTY_PAGE, "text/html;charset=iso-8859-1", close=False)
    return None


def printable_reading(scope: Scope) -> dict[str, object] | None:
    """The scope keys a front door reads otherwise where the target holds one of these printable
    characters, each with the value it reads, or ``None`` where it reads none of them otherwise:

    - a `\\` in a path under `/atlassian/` or a Google mount (:data:`_BACKSLASH_MOUNTS`) is a `/`,
      where `%5C` stays a backslash;
    - a `+` in a path under `/drive/v3/` is a space, where `%2B` stays a plus; in the path alone,
      because ``backlot.routers.google._drive_batch_redirect`` reads the raw path as sent;
    - in a path under HubSpot's `/crm/v3/objects/`, a `;` and what comes after it up to the next
      `/` are no part of the path, where `%3B` is;
    - on S3, a `#` in the path is one of its characters, and the query begins at the `?` after it;
    - on HubSpot, a query holding a broken escape (:data:`_BROKEN_ESCAPE`) comes with no
      credential.

    Measured on 2026-10-31 (UTC) over raw HTTP/1.1 on TLS sockets, with the sweep
    :func:`printable_answer` names and then the spellings
    ``tests/test_request_target.py::_PRINTABLE`` holds, each answered as the request it reads as: a
    `\\` on Jira, Confluence, Drive, Gmail, Docs, Sheets and Slides; a `+` on Drive's files, their
    permissions, comments and export, and a shared drive; a `;` on HubSpot's contacts and their
    list, where `objects;x/contacts/nope` was the gateway's 404 page; a `#` on S3's buckets; and a
    broken escape in a name or a value of HubSpot's query, on `contacts`, `contacts/nope` and
    `owners`."""
    path, raw = scope["path"], scope["raw_path"]
    reading: dict[str, object] = {}
    sent = scope.get(WITH_A_FRAGMENT)
    if sent is not None and errors.s3.owns(path) and b"#" in sent.partition(b"?")[0]:
        raw, _, reading["query_string"] = sent.partition(b"?")
    slashed = raw.replace(b"\\", b"/")
    if slashed.startswith(_BACKSLASH_MOUNTS):
        raw = slashed
    if raw.startswith(_HUBSPOT_OBJECTS):
        segments = raw.removeprefix(_HUBSPOT_OBJECTS).split(b"/")
        raw = _HUBSPOT_OBJECTS + b"/".join(s.partition(b";")[0] for s in segments)
    if raw != scope["raw_path"]:
        reading["raw_path"] = raw
        reading["path"] = urllib.parse.unquote(raw.decode("latin-1"))
    if raw.startswith(b"/drive/v3/") and b"+" in raw:
        reading["path"] = urllib.parse.unquote(raw.replace(b"+", b" ").decode("latin-1"))
    if _under(path, "/hubspot") and _BROKEN_ESCAPE.search(scope["query_string"]):
        headers = [(name, value) for name, value in scope["headers"] if name != b"authorization"]
        if len(headers) < len(scope["headers"]):
            reading["headers"] = headers
    return reading or None
