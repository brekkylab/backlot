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

from backlot import auth, errors
from backlot.routers import atlassian, s3

#: The scope key :mod:`backlot.protocol` leaves the request target on, as sent, where it
#: percent-encoded a byte of it.
SENT = "raw_request_target"

#: The scope key a rule sets where the front door reads the target as another request and closes
#: the connection after answering it (:func:`_s3`).
CLOSE = "raw_request_target_closes"

_RAW = re.compile(rb"[\x00-\x20\x7f-\xff]")


def encode_line(line: bytes) -> tuple[bytes, bytes | None]:
    """``line``, a request line ending in its LF, with each byte of :data:`_RAW` in its target
    percent-encoded, and the target as sent; or ``line`` and ``None`` where the target holds none
    of them or the line is not `METHOD target HTTP/x.y`.

    The target is what lies between the first space and the last one, so a space inside it is
    one of its bytes, and the version after the last one keeps the CR of a CRLF."""
    method, _, rest = line[:-1].partition(b" ")
    target, _, version = rest.rpartition(b" ")
    if not (method and version.startswith(b"HTTP/")):
        return line, None
    encoded = _RAW.sub(lambda m: b"%%%02X" % m[0][0], target)
    if encoded == target:
        return line, None
    return b"%s %s %s\n" % (method, encoded, version), target


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


#: The prefix each vendor real serves from its host's root is mounted under here, and the front
#: door :func:`front_door` names for it.
_MOUNTS = (
    ("/github", "github"),
    ("/slack", "slack"),
    (errors.atlassian.PREFIX, "atlassian"),
    ("/notion", "cloudflare"),
    ("/hubspot", "cloudflare"),
    ("/linear", "cloudflare"),
    ("/fireflies", "cloudflare"),
    ("/s3", "s3"),
    ("/msgraph", "graph"),
)

#: The Google families served from a host of their own, whose name is the first segment of the
#: path here: `docs.googleapis.com/v1/documents/…` is `/docs/v1/documents/…`.
_GOOGLE_HOSTS = ("/docs", "/sheets", "/slides")


def front_door(path: str) -> tuple[str, str] | None:
    """The front door a request for ``path`` reaches, `github`, `slack`, `google`, `atlassian`,
    `cloudflare` (in front of Notion, HubSpot, Linear and Fireflies), `s3` or `graph`, and the
    part of ``path`` real never receives: the prefix the vendor is mounted under, or on Google the
    segment naming the host (:data:`_GOOGLE_HOSTS`), empty on Drive and Gmail, whose paths are
    real's own, and on `/batch`. ``None`` for a path of Backlot's own."""
    for prefix, door in _MOUNTS:
        if _under(path, prefix):
            return door, prefix
    if errors.google.owns(path) or _under(path, "/batch"):
        return "google", next((h for h in _GOOGLE_HOSTS if path.startswith(f"{h}/")), "")
    return None


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


def google_page(status: int, reason: str, rest: bytes) -> bytes:
    """Google's error page, `text/html; charset=UTF-8` on each of its five hosts: the status and
    ``reason`` in the title and the first line, then ``rest``, what the page says after it."""
    return (
        b"<!DOCTYPE html>\n<html lang=en>\n  <meta charset=utf-8>\n"
        b'  <meta name=viewport content="initial-scale=1, minimum-scale=1, width=device-width">\n'
        b"  <title>Error %d (%s)!!1</title>\n  <style>\n%s"
        b"  <a href=//www.google.com/><span id=logo aria-label=Google></span></a>\n"
        b"  <p><b>%d.</b> <ins>That\xe2\x80\x99s an error.</ins>\n  <p>%s"
    ) % (status, reason.encode(), _GOOGLE_STYLE, status, rest)


_GOOGLE_STYLE = (
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
)

#: Google's two 400 pages, the same on each of its five hosts, both `text/html; charset=UTF-8`
#: (:func:`_google` says which answers what).
GOOGLE_PAGE_54 = b"<html><title>Error 400 (Bad Request)!!1</title></html>"
GOOGLE_PAGE_1555 = google_page(
    400,
    "Bad Request",
    b"Your client has issued a malformed or illegal request.  "
    b"<ins>That\xe2\x80\x99s all we know.</ins>\n",
)

#: The page Cloudflare, in front of Notion, HubSpot, Linear and Fireflies, answers several requests
#: with: `text/html` with no charset, CRLF line ends, 155 bytes.
CLOUDFLARE_PAGE = (
    b"<html>\r\n<head><title>400 Bad Request</title></head>\r\n<body>\r\n"
    b"<center><h1>400 Bad Request</h1></center>\r\n<hr><center>cloudflare</center>\r\n"
    b"</body>\r\n</html>\r\n"
)


def graph_page(status: int, name: str, title: str, detail: str) -> bytes:
    """A page of Graph's front end, `text/html; charset=us-ascii`: ``name`` in the head's title and
    ``title`` over the line `HTTP Error <status>. <detail>`."""
    return (
        '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN""http://www.w3.org/TR/html4/strict.dtd">'
        f"\r\n<HTML><HEAD><TITLE>{name}</TITLE>\r\n"
        '<META HTTP-EQUIV="Content-Type" Content="text/html; charset=us-ascii"></HEAD>\r\n'
        f"<BODY><h2>{title}</h2>\r\n<hr><p>HTTP Error {status}. {detail}</p>\r\n</BODY></HTML>\r\n"
    ).encode()


#: Graph's two 400 pages, 324 and 311 bytes (:func:`_graph` says which answers what).
GRAPH_INVALID_URL = graph_page(
    400, "Bad Request", "Bad Request - Invalid URL", "The request URL is invalid."
)
GRAPH_BADLY_FORMED = graph_page(400, "Bad Request", "Bad Request", "The request is badly formed.")


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


def cut_escape(path: bytes, query: bytes | None) -> bool:
    """Whether ``path``, the vendor's path as sent, with ``query`` after a `?` or ``None`` where
    none came, is one Cloudflare in front of Linear and Fireflies reads as cut short by the end of
    the target (:data:`_CUT_ESCAPE`). ``backlot.main.refuse_a_cut_escape_on_linear_and_fireflies``
    answers it, and :func:`refused_path` each other broken escape."""
    cut = _CUT_ESCAPE.search(path)
    return query is None and cut is not None and not _broken(path[: cut.start()])


def refused_path(path: bytes, query: bytes | None) -> bool:
    """Whether Cloudflare in front of Linear and Fireflies refuses ``path`` with ``query`` after it,
    ``None`` where no `?` came, with :data:`CLOUDFLARE_PAGE`: the path holds a `..` segment that
    climbs past the root (:func:`_climbs_past_the_root`), which both hosts refused so ahead of a cut
    escape after it (`/../a%2`, 2026-10-29, UTC), or it holds `%00` or a `%` two hex digits do not
    follow, but where :func:`cut_escape` holds. The page comes ahead of Cloudflare's answer to a raw
    byte: on 2026-10-26 (UTC) both hosts drew it for `/a%zz` with a raw `é` before or after it or
    `\\x01` before it, and for `/a%00` after a raw `é`, and on 2026-10-29 for `/../a` with a raw `é`
    or a space after it, where a raw `é` alone drew the empty 400."""
    if _climbs_past_the_root(path):
        return True
    return _broken(path) and not cut_escape(path, query)


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
    if (page := _graph_page(path_kinds + query_kinds)) is not None:
        return _page(page, "text/html; charset=us-ascii")
    try:
        scope[SENT].partition(b"?")[0].decode("utf-8")
    except UnicodeDecodeError:
        return _page(b"", None, close=False)
    return None


def _graph_page(kinds: list[str]) -> bytes | None:
    """Graph's page for a target whose raw bytes are of ``kinds``, in the order they come."""
    for kind in kinds:
        if kind in ("nul", "control"):
            return GRAPH_INVALID_URL
        if kind in ("tab", "cr", "space"):
            return GRAPH_BADLY_FORMED
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
    door, prefix = front_door(scope["path"]) or (None, None)
    sent_path, mark, sent_query = scope[SENT].partition(b"?")
    in_path, in_query = _kinds(sent_path), _kinds(sent_query)
    if door == "github":
        return _github()
    if door == "slack":
        return _slack()
    if door == "google":
        return _google(in_path, in_query)
    if door == "atlassian":
        return _atlassian(scope, in_path, in_query)
    if door == "cloudflare" and prefix in ("/linear", "/fireflies"):
        own = b"/" + sent_path.partition(b"/")[2].partition(b"/")[2]
        query = sent_query if mark else None
        if refused_path(own, query):
            return _page(CLOUDFLARE_PAGE, "text/html")
        refused = _cloudflare(scope, in_path, in_query)
        if refused is None and cut_escape(own, query):
            _drop_the_cut(scope)
        return refused
    if door == "cloudflare":
        return _cloudflare(scope, in_path, in_query)
    if door == "s3":
        return _s3(scope, in_path, in_query)
    if door == "graph":
        return _graph(scope, in_path, in_query)
    return _page(b"Invalid HTTP request received.", "text/plain; charset=utf-8")


def refused_in_the_line(door: str, line: bytes, ended: bool) -> tuple[str, str, bytes] | None:
    """The status line, media type and body the front door ``door`` (:func:`front_door`'s name)
    refuses the request line ``line``, the bytes before its LF, with ahead of its head bound, or
    ``None`` where the bound's page answers; ``ended`` is whether the LF came, so that a header
    rather than the line reached the bound; where the LF is itself the byte that reaches the bound,
    which real was not measured at, the line has ended. The target of a line that ended lies between
    its first space and its last, as :func:`encode_line` reads it, and of one that has not, after
    its first.

    A front door refuses a raw byte in the target as it reads the line: GitHub one of any kind,
    Google a `nul` or a `control`, Jira Cloud's CDN and Cloudflare a `nul` or a `cr`, and Graph one
    of any kind but a `high` (:func:`_graph_page`). Once the line has ended, Slack refuses a raw
    byte of any kind, Jira Cloud's CDN a path :func:`_broken` holds, and Cloudflare, on each of its
    four hosts, a path :func:`refused_path` holds. Each answer is the one real gave the same line
    with a short head.

    Measured on 2026-11-01 (UTC) over raw HTTP/1.1 on TLS, each request cache-busted, on the paths
    of ``backlot.head_bounds``' measurement: one byte of each kind, with a byte after it, in the
    path and in the query, and `%zz` in the path, each sent with a short head, before a header past
    the door's bound with no LF, and with the query run past the bound with no LF; on Jira Cloud,
    Notion, HubSpot and Linear also `%00`, `/a%zzb`, a `..` that climbs past the root, one that does
    not, and a `%2` or `%` that ends the target, on Linear and Fireflies `/a%zz` and `/../graphql`,
    and on Cloudflare's four hosts a `%2` or `%` before the `?`, with a short head and before the
    long header; and on GitHub, Slack, Google, Jira Cloud, Notion, HubSpot, Linear and Graph a byte
    each refuses 100 bytes into the query, which drew the answer the byte draws at the query's
    start. Every long request the rules here do not name drew the bound's page but
    `/../rest/api/3/myself` on Jira Cloud, which drew the CDN's 400 as it did with a short head; S3
    answered each with an empty 400, which its bound answers too."""
    method, _, rest = line.partition(b" ")
    target = rest.rpartition(b" ")[0] if ended else rest
    path, mark, query = target.partition(b"?")
    kinds = _kinds(path) + _kinds(query)
    every = set(kinds)
    bad_request = "HTTP/1.1 400 Bad Request"
    if door == "github" and kinds:
        return bad_request, "text/html; charset=utf-8", GITHUB_PAGE
    if door == "slack" and kinds and ended:
        return bad_request, "text/plain", b"Bad Request"
    if door == "google" and every & {"nul", "control"}:
        return "HTTP/1.0 400 Bad Request", "text/html; charset=UTF-8", GOOGLE_PAGE_54
    if door == "graph" and (page := _graph_page(kinds)) is not None:
        return bad_request, "text/html; charset=us-ascii", page
    if door == "atlassian" and (every & {"nul", "cr"} or ended and _broken(path)):
        raw_path = _RAW.sub(lambda m: b"%%%02X" % m[0][0], path).decode("latin-1")
        page = errors.atlassian.cdn_bad_request(method.decode("latin-1"), raw_path)
        return bad_request, "text/html", page.encode()
    if door == "cloudflare":
        own = b"/" + path.partition(b"/")[2].partition(b"/")[2]
        if every & {"nul", "cr"} or ended and refused_path(own, query if mark else None):
            return bad_request, "text/html", CLOUDFLARE_PAGE
    return None
