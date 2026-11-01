"""Where each vendor's front door stops reading a request head that has not ended, and what it
answers there. :mod:`backlot.protocol` reads each head against the :class:`Bound` that :func:`bound`
gives for the path its request line names.

A front door bounds either the whole head, every byte of it that has arrived, or each line of it,
the bytes before the line's LF with the CR among them (:attr:`Bound.per_line`). Once that many have
arrived it answers and closes the connection, with a page that turns on whether the bound was
reached in the request line or in the headers, or with its refusal of a request line it refuses
ahead of the bound (:meth:`Bound.refused`). A head whose last byte, the LF that ends it, is the
bound's last gets past a head bound unless :attr:`Bound.refuses_its_end`; a line bound counts no LF,
so a line holding that many bytes is refused whether its LF follows or not. The answer here is the
front door's status line, `Content-Type` and body, with `Content-Length` and `Connection: close`,
and none of the front door's other headers (`Server`, `Date`, `Via`, `Cache-Control`,
`Referrer-Policy`, `X-Cache`, `X-Amz-Cf-Pop` and the request ids).

Bytes are counted as the client sent them but for the prefix a path starts with here and real never
receives (``backlot.request_target.front_door``): the head real gets is that much shorter. The rest
of the head is counted as sent, `Host` and all. A path no vendor owns has no bound.

Measured on 2026-10-29 with each request written to a TLS socket by hand and cache-busted: each sent
exactly N bytes and then waited for an answer (six seconds, four on api.github.com, which closes a
head it has had no byte of for about five), and N was bisected for a request line with no LF
(`GET <path>?_=<r>&x=aaa…`) and for a header with none (`X-A: aaa…` after a complete request line,
`Host`, `User-Agent` and `Connection`). A 1,000-byte header line before `X-A` moved the bound of
Jira Cloud and of the four behind Cloudflare by 1,000, which makes theirs line bounds. The same
requests with their LF and the end of the head, bisected the same day, showed how five of the bounds
take a head that ends: on Slack, Google and S3 one of the bound's length is read and one byte longer
is refused, on Graph one of the bound's length is refused, and on Cloudflare a request line of
131,071 bytes before its LF is read and one of 131,072 refused, and a header line the same on Linear
and Fireflies. GitHub's and Jira Cloud's ended heads met a lower bound first; GitHub's bound is
taken to read one as Slack's does. A body after a head that ended, 50,000 bytes on Graph and 140,000
on Notion, drew the status line and `Content-Type` a body of ten bytes drew.
"""

from __future__ import annotations

import dataclasses
import re
import urllib.parse

from backlot import errors, request_target

#: How much of the request line :func:`bound` is given, unless the line ends sooner: the longest
#: method httptools reads (`UNSUBSCRIBE`), a space and the longest prefix a vendor is named by
#: (``backlot.request_target.front_door``) fit in it.
NAMED_WITHIN = 64


@dataclasses.dataclass(frozen=True)
class Bound:
    """One front door's bound on a head and its two answers."""

    #: The bytes the front door reads, of the head or of a line, before it answers.
    limit: int
    #: Whether :attr:`limit` bounds each line rather than the whole head.
    per_line: bool
    #: Whether a head of :attr:`limit` bytes is refused where its last byte ends it.
    refuses_its_end: bool
    #: The answer to a request line that reaches the bound.
    in_line: bytes
    #: The answer to a header that reaches it.
    in_headers: bytes
    #: The bytes of the request line real never receives (``front_door``'s prefix).
    mount: int = 0
    #: The front door, as ``backlot.request_target.front_door`` names it.
    door: str = ""

    def answer(self, size: int, line: int, in_request_line: bool, ended: bool) -> bytes | None:
        """The answer to a head of which ``size`` bytes have arrived, ``line`` of them in the line
        they end in and before its LF, ``ended`` where the last is the LF that ends the head; or
        ``None`` while it is within the bound."""
        if self.per_line:
            read = line - self.mount if in_request_line else line
        elif ended and not self.refuses_its_end:
            return None
        else:
            read = size - self.mount
        if read < self.limit:
            return None
        return self.in_line if in_request_line else self.in_headers

    def refused(self, line: bytes, ended: bool) -> bytes | None:
        """The front door's refusal of the request line ``line``, the bytes before its LF, where it
        answers that ahead of the bound (``backlot.request_target.refused_in_the_line``), ``ended``
        where the LF came; else ``None``."""
        refused = request_target.refused_in_the_line(self.door, line, ended)
        return None if refused is None else _answer(*refused)


def _answer(status_line: str, media_type: str | None, body: bytes) -> bytes:
    """``body`` as a front door sends it here (the module docstring says which headers)."""
    head = f"{status_line}\r\n"
    if media_type is not None:
        head += f"Content-Type: {media_type}\r\n"
    return f"{head}Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body


#: GitHub's two pages, `text/html` with no charset.
GITHUB_PAGE_414 = (
    b"<html><body><h1>414 URI Too Long</h1>\n"
    b"The URI provided was too long for the server to process.\n</body></html>\n"
)
GITHUB_PAGE_431 = (
    b"<html><body><h1>431 Request Header Fields Too Large</h1>\n"
    b"Request Header Fields Too Large.\n</body></html>\n"
)

#: Google's page, the same on www, gmail, docs, sheets and slides.googleapis.com.
GOOGLE_PAGE_413 = request_target.google_page(
    413,
    "Request Entity Too Large",
    b"Your client issued a request that was too large.\n <script>\n  (function() { var c=function"
    b'(a,d,b){a=a+"=deleted; path="+d;b!=null&&(a+="; domain="+b);document.cookie=a+"; expires=Thu,'
    b' 01 Jan 1970 00:00:00 GMT"};var g=function(a){var d=e,b=location.hostname;c(d,a,null);'
    b'c(d,a,b);for(var f=0;;){f=b.indexOf(".",f+1);if(f<0)break;c(d,a,b.substring(f+1))}};var h;'
    b"if(unescape(encodeURI(document.cookie)).length>4E3){for(var k=document.cookie.split"
    b'(";"),l=[],m=0;m<k.length;m++){var n=k[m].match(/^\\s*([^=]+)/);n&&l.push(n[1])}for(var p=0;'
    b'p<l.length;p++){var q=void 0,e=l[p];g("/");for(var r=location.pathname,t=0;;){t=r.indexOf'
    b'("/",t+1);if(t<0)break;q=r.substring(0,t);g(q);g(q+"/")}r.charAt(r.length-1)!="/"&&(g(r),'
    b'g(r+"/"))}h=!0}else h=!1;\nh&&setTimeout(function(){if(history.replaceState){var a=location'
    b'.href;history.replaceState(null,"","/");location.replace(a)}},1E3); })();\n\n</script>\n'
    b" <ins>That\xe2\x80\x99s all we know.</ins>\n",
)

#: Cloudflare's two pages, for the `cloudflare` door of ``backlot.request_target.front_door``,
#: `text/html` with no charset.
CLOUDFLARE_PAGE_414 = (
    b"<html>\r\n<head><title>414 Request-URI Too Large</title></head>\r\n<body>\r\n"
    b"<center><h1>414 Request-URI Too Large</h1></center>\r\n<hr><center>cloudflare</center>\r\n"
    b"</body>\r\n</html>\r\n"
)
CLOUDFLARE_PAGE_HEADER = (
    b"<html>\r\n<head><title>400 Request Header Or Cookie Too Large</title></head>\r\n<body>\r\n"
    b"<center><h1>400 Bad Request</h1></center>\r\n"
    b"<center>Request Header Or Cookie Too Large</center>\r\n"
    b"<hr><center>cloudflare</center>\r\n</body>\r\n</html>\r\n"
)

_GRAPH = "text/html; charset=us-ascii"
_SLACK_431 = _answer(
    "HTTP/1.1 431 Request Header Fields Too Large", "text/plain", b"Request Header Fields Too Large"
)
_GOOGLE_413 = _answer(
    "HTTP/1.0 413 Request Entity Too Large", "text/html; charset=UTF-8", GOOGLE_PAGE_413
)
_S3_400 = _answer("HTTP/1.1 400 Bad Request", None, b"")

#: Each front door's bound but Jira Cloud's, whose page names the request (:func:`bound`).
_BOUNDS = {
    "github": Bound(
        68_552,
        per_line=False,
        refuses_its_end=False,
        in_line=_answer("HTTP/1.1 414 URI Too Long", "text/html", GITHUB_PAGE_414),
        in_headers=_answer(
            "HTTP/1.1 431 Request Header Fields Too Large", "text/html", GITHUB_PAGE_431
        ),
    ),
    "slack": Bound(
        61_440,
        per_line=False,
        refuses_its_end=False,
        in_line=_SLACK_431,
        in_headers=_SLACK_431,
    ),
    "google": Bound(
        65_536,
        per_line=False,
        refuses_its_end=False,
        in_line=_GOOGLE_413,
        in_headers=_GOOGLE_413,
    ),
    "cloudflare": Bound(
        131_072,
        per_line=True,
        refuses_its_end=False,
        in_line=_answer("HTTP/1.1 414 Request-URI Too Large", "text/html", CLOUDFLARE_PAGE_414),
        in_headers=_answer("HTTP/1.1 400 Bad Request", "text/html", CLOUDFLARE_PAGE_HEADER),
    ),
    "s3": Bound(
        16_384,
        per_line=False,
        refuses_its_end=False,
        in_line=_S3_400,
        in_headers=_S3_400,
    ),
    "graph": Bound(
        40_960,
        per_line=False,
        refuses_its_end=True,
        in_line=_answer(
            "HTTP/1.1 414 Request-URI Too Long",
            _GRAPH,
            request_target.graph_page(
                414, "Request URL Too Long", "Request URL Too Long", "The request URL is too long."
            ),
        ),
        in_headers=_answer(
            "HTTP/1.1 400 Bad Request",
            _GRAPH,
            request_target.graph_page(
                400,
                "Bad Request",
                "Bad Request - Request Too Long",
                "The size of the request headers is too long.",
            ),
        ),
    ),
}


def bound(start: bytes) -> Bound | None:
    """The bound on a head whose request line starts ``start``, read off the path in it, or
    ``None`` where no vendor owns the path. Jira Cloud's is a line bound of 32,768 with the CDN's
    page, 414 in the request line and 494 in the headers (``errors.atlassian.cdn_too_long``)."""
    method, _, target = start.partition(b" ")
    path = re.split(rb"[? ]", target, maxsplit=1)[0].decode("latin-1")
    named = request_target.front_door(urllib.parse.unquote(path))
    if named is None:
        return None
    door, prefix = named
    if door == "atlassian":
        line, headers = (
            _answer(
                status_line,
                "text/html",
                errors.atlassian.cdn_too_long(status, method.decode("latin-1"), path).encode(),
            )
            for status, status_line in (
                (414, "HTTP/1.1 414 Request-URI Too Large"),
                (494, "HTTP/1.1 494 "),
            )
        )
        return Bound(32_768, True, False, line, headers, len(prefix), door)
    return dataclasses.replace(_BOUNDS[door], mount=len(prefix), door=door)
