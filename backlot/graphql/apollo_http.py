"""Apollo Server's HTTP layer, in front of the request pipeline :mod:`backlot.graphql.engine` runs:
what it answers before it reads a document, and the media type of what it answers.

Both vendors run it. :func:`csrf_refusal` is its check that a request was preflighted,
:func:`search_payload` its reading of a GET's query string, :func:`method_refusal` its 405 for a
method it does not serve, :func:`get_content_type_refusal` its 415 for a GET carrying a content
type other than JSON, :func:`response_type` its choice of media type from ``Accept`` and
:data:`NOT_ACCEPTABLE` its 406 when there is none, and :func:`prefers_html` and
:func:`landing_page` its page for a browser. Each refusal but :data:`NOT_ACCEPTABLE` is thrown
before the request pipeline, so it takes whichever media type ``Accept`` allows and
``application/json`` where it allows none: a :class:`~backlot.graphql.engine.Refusal` that is
``thrown``. A refusal the pipeline makes, from a persisted query's to a variable's, is refused for
the ``Accept`` in turn, with :data:`NOT_ACCEPTABLE`.

The code is read off Apollo Server 5.5.1's ``runHttpQuery.ts``, ``preventCsrf.ts`` and
``ApolloServer.ts``, and the libraries they call: ``whatwg-mimetype`` 4.0.0 (:func:`mime_essence`)
and ``negotiator`` 1.0.0 (:func:`preferred_media_type`). ``negotiator`` 1.1.0 parses ``Accept``
anew; api.linear.app and api.fireflies.ai each answered nine values that 1.0.0 and 1.1.0 negotiate
apart as 1.0.0 does on 2026-10-25 (UTC), among them ``application/json;q``, which 1.0.0 refuses.
"""

from __future__ import annotations

import functools
import math
import re
import secrets
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl

from backlot.graphql import body as request_body
from backlot.graphql.engine import BAD_REQUEST, Refusal

#: The methods uvicorn passes on to the application under httptools 0.9.0: 35 of the 49 sent to it
#: on 2026-10-25 (UTC), the IANA registry's, Node's, ``FOO`` and ``get``. It answered the other 14
#: with its own 400 before any route ran. A route that answers whatever method its vendor's server
#: answers takes these.
METHODS = (
    "ACL", "BIND", "CHECKOUT", "CONNECT", "COPY", "DELETE", "GET", "HEAD", "LINK", "LOCK",
    "M-SEARCH", "MERGE", "MKACTIVITY", "MKCALENDAR", "MKCOL", "MOVE", "NOTIFY", "OPTIONS", "PATCH",
    "POST", "PROPFIND", "PROPPATCH", "PURGE", "PUT", "QUERY", "REBIND", "REPORT", "SEARCH", "SOURCE",
    "SUBSCRIBE", "TRACE", "UNBIND", "UNLINK", "UNLOCK", "UNSUBSCRIBE",
)  # fmt: skip

#: The media types Apollo Server answers a single result in, its ``MEDIA_TYPES``.
JSON = "application/json; charset=utf-8"
GRAPHQL_RESPONSE = "application/graphql-response+json; charset=utf-8"
CALLBACK = "application/json; callbackSpec=1.0; charset=utf-8"
_HTML = "text/html"
# The incremental-delivery types `prefersHTML` puts ahead of `text/html`.
_MULTIPART = (
    "multipart/mixed; deferSpec=20220824",
    "multipart/mixed; incrementalSpec=v0.2",
    "multipart/mixed",
)

_JS_SPACE = request_body.JS_SPACE
# A character JavaScript's `.` matches: any but a LineTerminator.
_JS_ANY = r"[^\n\r\u2028\u2029]"
# negotiator's `simpleMediaTypeRegExp`.
_S = re.escape(_JS_SPACE)
_ACCEPT_ENTRY = re.compile(rf"[{_S}]*([^{_S}/;]+)/([^;{_S}]+)[{_S}]*(?:;({_JS_ANY}*))?\Z")
# The literal JavaScript's `parseFloat` reads, past the white space it skips, each of which Python's
# `float` reads alike.
_FLOAT = re.compile(r"[+-]?(?:Infinity|(?:[0-9]+[.]?[0-9]*|[.][0-9]+)(?:[eE][+-]?[0-9]+)?)")


def _js_or(*values: float) -> float:
    """``a || b || ...`` over numbers, as JavaScript reads it: the first that is neither zero nor
    ``NaN``, or else the last."""
    for value in values[:-1]:
        if value and not math.isnan(value):
            return value
    return values[-1]


def _parse_float(text: str) -> float:
    """JavaScript's ``parseFloat``: the longest decimal literal at the start of ``text`` past its
    white space, or ``NaN``."""
    match = _FLOAT.match(text.lstrip(_JS_SPACE))
    return math.nan if match is None else float(match.group())


def _split_quoted(text: str, separator: str) -> list[str]:
    """``text`` split at ``separator`` but where the piece so far holds an odd number of ``"``, as
    negotiator's ``splitMediaTypes`` and ``splitParameters`` split."""
    pieces = text.split(separator)
    joined = [pieces[0]]
    for piece in pieces[1:]:
        if joined[-1].count('"') % 2 == 0:
            joined.append(piece)
        else:
            joined[-1] += separator + piece
    return joined


def _media_range(text: str, index: int | None) -> dict | None:
    """negotiator's ``parseMediaType``: the type, subtype, parameters and quality of one entry of
    ``Accept``, read up to the first ``q`` parameter, or ``None`` for one it cannot read."""
    match = _ACCEPT_ENTRY.match(text)
    if match is None:
        return None
    params: dict[str, str] = {}
    q = 1.0
    if match.group(3):
        for parameter in _split_quoted(match.group(3), ";"):
            # A parameter with no `=` has an undefined value there, which reads as "" does.
            key, _, value = parameter.strip(_JS_SPACE).partition("=")
            if value and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]
            if key.lower() == "q":
                q = _parse_float(value)
                break
            params[key.lower()] = value
    return {"type": match.group(1), "subtype": match.group(2), "params": params, "q": q, "i": index}


def _specificity(provided: dict, spec: dict, index: int) -> dict | None:
    """negotiator's ``specify``: how closely the range ``spec`` names the type ``provided``, or
    ``None`` where it does not take it."""
    s = 0
    if spec["type"].lower() == provided["type"].lower():
        s |= 4
    elif spec["type"] != "*":
        return None
    if spec["subtype"].lower() == provided["subtype"].lower():
        s |= 2
    elif spec["subtype"] != "*":
        return None
    if spec["params"]:
        if all(
            value == "*" or value.lower() == provided["params"].get(key, "").lower()
            for key, value in spec["params"].items()
        ):
            s |= 1
        else:
            return None
    return {"i": index, "o": spec["i"], "q": spec["q"], "s": s}


def preferred_media_type(accept: str, provided: tuple[str, ...]) -> str | None:
    """negotiator 1.0.0's ``mediaType``: the type in ``provided`` that ``accept`` ranks first, the
    earlier in ``provided`` among equals, or ``None`` where it takes none of them."""
    ranges = [
        r
        for i, e in enumerate(_split_quoted(accept, ","))
        if (r := _media_range(e.strip(_JS_SPACE), i))
    ]
    priorities = []
    for index, type_ in enumerate(provided):
        target = _media_range(type_, None)
        priority = {"o": -1, "q": 0.0, "s": 0}
        for spec in ranges:
            found = _specificity(target, spec, index)
            if (
                found is not None
                and _js_or(
                    priority["s"] - found["s"],
                    priority["q"] - found["q"],
                    priority["o"] - found["o"],
                )
                < 0
            ):
                priority = found
        priorities.append(priority)
    accepted = [p for p in priorities if p["q"] > 0]
    accepted.sort(
        key=functools.cmp_to_key(
            lambda a, b: _js_or(
                b["q"] - a["q"], b["s"] - a["s"], a["o"] - b["o"], a["i"] - b["i"], 0
            )
        )
    )
    return (
        provided[next(i for i, p in enumerate(priorities) if p is accepted[0])]
        if accepted
        else None
    )


def response_type(accept: str | None) -> str | None:
    """``chooseContentTypeForSingleResultResponse``: the media type Apollo Server answers in under
    ``accept``, :data:`JSON` for none, or ``None`` where it takes none of its three."""
    if not accept:
        return JSON
    return preferred_media_type(accept, (JSON, GRAPHQL_RESPONSE, CALLBACK))


def prefers_html(method: str, accept: str | None) -> bool:
    """``prefersHTML``: whether a GET's ``accept`` puts ``text/html`` ahead of the media types
    Apollo Server answers in, which it lists first so that a tie goes to them."""
    return (
        method == "GET"
        and bool(accept)
        and preferred_media_type(accept, (JSON, GRAPHQL_RESPONSE, *_MULTIPART, _HTML)) == _HTML
    )


#: Apollo Server's 406, thrown once the request pipeline has answered, where ``Accept`` takes none
#: of :func:`response_type`'s types. Its ``http`` extension carries no ``headers``.
NOT_ACCEPTABLE = Refusal(
    "An 'accept' header was provided for this request which does not accept "
    f"{JSON} or {GRAPHQL_RESPONSE}",
    BAD_REQUEST,
    status=406,
    thrown=True,
    http_headers=False,
)

# An HTTP token, as `whatwg-mimetype` reads one.
_TOKEN = re.compile(r"[-!#$%&'*+.^_`|~A-Za-z0-9]+")
_HTTP_SPACE = " \t\n\r"


def mime_essence(value: str) -> str | None:
    """``whatwg-mimetype`` 4.0.0's ``MIMEType.parse(value).essence``: the type and subtype in lower
    case, or ``None`` where it parses none. The parameters cannot make it ``None``."""
    value = value.strip(_HTTP_SPACE)
    type_, _, rest = value.partition("/")
    if not _TOKEN.fullmatch(type_):
        return None
    subtype = rest.partition(";")[0].rstrip(_HTTP_SPACE)
    if not _TOKEN.fullmatch(subtype):
        return None
    return f"{type_}/{subtype}".translate(_ASCII_LOWER)


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

_NOT_PREFLIGHTED = ("application/x-www-form-urlencoded", "multipart/form-data", "text/plain")
_PREFLIGHT_HEADERS = ("x-apollo-operation-name", "apollo-require-preflight")
CSRF = (
    "This operation has been blocked as a potential Cross-Site Request Forgery (CSRF). Please "
    "either specify a 'content-type' header (with a type that is not one of "
    f"{', '.join(_NOT_PREFLIGHTED)}) or provide a non-empty value for one of the following "
    f"headers: {', '.join(_PREFLIGHT_HEADERS)}\n"
)


def headers_seen(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    """The request headers as Apollo Server reads them behind Node, from the ASGI ``raw`` list:
    each name in lower case and each value without the spaces and tabs at its ends, the values of
    a name sent more than once joined with ``", "`` but ``content-type``'s, of which Node keeps the
    first, and a header with no value left out, as each vendor answered on 2026-10-25 (UTC). Linear
    reads `Content-Type` on its own (``backlot.routers.linear``)."""
    seen: dict[str, str] = {}
    for name, value in raw:
        key, text = name.decode("latin-1").lower(), value.decode("latin-1").strip(" \t")
        if key in seen:
            if key != "content-type":
                seen[key] = f"{seen[key]}, {text}"
        else:
            seen[key] = text
    return {key: value for key, value in seen.items() if value}


def csrf_refusal(headers: Mapping[str, str]) -> Refusal | None:
    """``preventCsrf``: the 400 for a request that may not have been preflighted, one with no
    content type or one ``whatwg-mimetype`` reads as a type a browser sends without a preflight,
    and none of :data:`_PREFLIGHT_HEADERS` with a value; ``None`` for a request it lets through,
    a content type ``whatwg-mimetype`` cannot parse among them."""
    content_type = headers.get("content-type")
    if content_type is not None and mime_essence(content_type) not in _NOT_PREFLIGHTED:
        return None
    if any(headers.get(name) for name in _PREFLIGHT_HEADERS):
        return None
    return Refusal(CSRF, BAD_REQUEST, thrown=True)


def method_refusal() -> Refusal:
    """The 405 Apollo Server answers a method other than GET and POST with."""
    return Refusal(
        "Apollo Server supports only GET/POST requests.",
        BAD_REQUEST,
        status=405,
        headers=(("allow", "GET, POST"),),
        thrown=True,
    )


def get_content_type_refusal(headers: Mapping[str, str]) -> Refusal | None:
    """Apollo Server 5.5's 415 for a GET whose content type ``whatwg-mimetype`` does not read as
    ``application/json``, or ``None``. Apollo Server 5.4 and before make no such check."""
    content_type = headers.get("content-type")
    if content_type is None or mime_essence(content_type) == "application/json":
        return None
    return Refusal(
        "GET requests may not have a content-type header other than application/json.",
        BAD_REQUEST,
        status=415,
        thrown=True,
    )


def search_payload(query_string: bytes) -> dict | Refusal:
    """A GET's request as Apollo Server reads it off the query string, with ``URLSearchParams``:
    ``query``, ``operationName``, ``variables`` and ``extensions``, each at most once, the last two
    read with ``JSON.parse`` as objects; or the refusal of the first that is not, in that order."""
    params: dict[str, list[str]] = {}
    for name, value in parse_qsl(
        query_string.decode("latin-1"), keep_blank_values=True, errors="replace"
    ):
        params.setdefault(name, []).append(value)
    payload: dict[str, Any] = {}
    for name in ("query", "operationName", "variables", "extensions"):
        values = params.get(name, [])
        if len(values) > 1:
            return Refusal(
                f"The '{name}' search parameter may only be specified once.",
                BAD_REQUEST,
                thrown=True,
            )
        if not values:
            continue
        value: Any = values[0]
        if name in ("variables", "extensions"):
            try:
                value = request_body.parse(value)
            except request_body.NotJson:
                return Refusal(
                    f"The {name} search parameter contains invalid JSON.", BAD_REQUEST, thrown=True
                )
            if not isinstance(value, dict):
                return Refusal(
                    f"The {name} search parameter should contain a JSON-encoded object.",
                    BAD_REQUEST,
                    thrown=True,
                )
        payload[name] = value
    return payload


_LANDING_PAGE = (Path(__file__).parent / "apollo_landing_page.html").read_text(encoding="utf-8")


def landing_page() -> str:
    """Apollo Server 5.5.1's page for a browser, the embedded Sandbox: the page api.fireflies.ai
    answered each of twelve GETs preferring ``text/html`` with on 2026-10-25 (UTC), the same 3522
    bytes each time but the nonce, 64 hexadecimal digits of its own each time."""
    return _LANDING_PAGE.replace("NONCE", secrets.token_hex(32))
