"""Atlassian Cloud's error envelope.

Atlassian clients (atlassian-python-api, which mcp-atlassian uses) parse an error body as Atlassian
Cloud's shape — Confluence's ``raise_for_status`` does ``response.json()["message"]`` — so FastAPI's
default ``{"detail": …}`` turns every error into a cryptic ``KeyError: 'message'`` inside the client
rather than the error Backlot meant to report. Both Jira's ``errorMessages`` list and Confluence's
scalar ``message`` are emitted, since one envelope serves both APIs here.

One envelope, but not the ONLY one: a refusal real answers in a shape measured to differ carries
its own body on an :class:`AtlassianError` and is served verbatim. Reading a query parameter is
where the two products visibly part — see :func:`integer_conversion_failure`.

Nor is the body always JSON. A path neither product serves is Jira's RFC 7807 :func:`no_endpoint`,
Confluence's JAX-RS 404 in whichever of two shapes `Accept` asks for (:func:`jaxrs_not_found`), or
the product's HTML page (:data:`HTML_NOT_FOUND`) — three shapes measured on the same day, each one
what that URL answers rather than what this module would otherwise send.
"""

from __future__ import annotations

import codecs
import http
import json
import re
from xml.sax.saxutils import escape

from fastapi import HTTPException

PREFIX = "/atlassian"
WIKI = f"{PREFIX}/wiki"

# Jira answers a type-conversion failure as RFC 7807, with a charset real puts on it and FastAPI
# does not. Measured on brekkylab.atlassian.net, 2026-09-14.
PROBLEM_JSON = "application/problem+json;charset=UTF-8"

# The two refusals Confluence gives a caller it will not serve, measured against
# ecosystem.atlassian.net and brekkylab.atlassian.net on 2026-09-04. A credential it read and
# rejected, and a request that carried none, both get the 403 below verbatim — the vendor reports
# its own exception class in the message, and a client that logs the body logs that. A Basic value
# it could not read gets a 401 instead, whose real body is Tomcat's HTML page titled "HTTP Status
# 401 - Unauthorized"; that title is the message here, because the envelope this module exists to
# emit is JSON and a client parsing `message` out of the page would find nothing.
CONFLUENCE_FORBIDDEN = (
    "com.atlassian.confluence.mvc.rest.common.exception.StacklessResponseStatusException: "
    '403 FORBIDDEN "Request rejected because caller cannot access Confluence"'
)
CONFLUENCE_UNAUTHORIZED = "Unauthorized"
#: That 403 as real sends it, byte for byte: two members, 190 bytes on 2026-09-30, where the shared
#: envelope a served route renders it in (:func:`http_body`) adds three more.
CONFLUENCE_FORBIDDEN_BODY = json.dumps(
    {"statusCode": 403, "message": CONFLUENCE_FORBIDDEN}, separators=(",", ":")
).encode()

# What a `<site>.atlassian.net` gateway answers a `Bearer` it cannot read as a Connect session
# JWT, on every Jira route — `serverInfo` and `field`, which need no credential, included. One
# `error` key and nothing else: not this module's envelope, and not Confluence's either, so it is
# returned as its own body rather than shaped. Measured on ecosystem.atlassian.net and
# brekkylab.atlassian.net on 2026-09-04.
CONNECT_TOKEN_UNREADABLE = "Failed to parse Connect Session Auth Token"


def connect_token_body() -> dict:
    return {"error": CONNECT_TOKEN_UNREADABLE}


# What Jira answers a request with no credential it resolves — none, a Basic pair it read and
# rejected, an unknown scheme, and on an `OPTIONS` an unreadable bearer too — at an operation that
# will not run anonymously: 401, this line and nothing else, 53 bytes, with
# `WWW-Authenticate: OAuth realm="<the site, percent-encoded>"` and `X-Frame-Options: SAMEORIGIN`.
# Jira's own answer, not the gateway's: it carries what ``backlot.routers.atlassian.vendor_headers``
# puts on Jira's answers and not what that puts on the gateway's own refusals, and an operation's
# media-type check (:func:`refuse_a_media_type`) answers before it. Which operations those are is
# ``backlot.routers.atlassian.unmatched_path``'s to say; measured on Jira Cloud, 2026-09-30. The
# media type follows `Accept` there: `text/html;charset=UTF-8` for `*/*`, the one this server sends
# whatever `Accept` says, `text/plain` with no `Accept` and `application/json` for
# `application/json` (on `myself` and `OPTIONS serverInfo`, the same day).
JIRA_UNAUTHENTICATED = "Client must be authenticated to access this resource."

# Confluence's second anonymous refusal, which a few of its services give instead of
# :data:`CONFLUENCE_FORBIDDEN`: the members in the other order, and on every one measured but the
# Connect module path, `cache-control: no-cache, no-store, must-revalidate` and a 1970 `expires`
# beside it. Measured 2026-09-30; the paths are ``backlot.routers.atlassian``'s.
CONFLUENCE_NOT_PERMITTED_BODY = (
    b'{"message":"Current user not permitted to use Confluence","statusCode":403}'
)


def owns(path: str) -> bool:
    """Whether ``path`` is under the Atlassian mount. The segment has to END there: `/atlassianx`
    is a path of Backlot's own, and answering it in this envelope -- or with the headers
    ``backlot.main.report_atlassian_headers`` adds -- would claim a request neither product saw."""
    return path == PREFIX or path.startswith(f"{PREFIX}/")


#: The one segment under the site that is Jira's REST API. Everything that is neither this nor
#: :data:`WIKI` is the product's own web surface, which answers the HTML page rather than either
#: API's 404: measured 2026-09-22, `/foo`, `/ex/jira/x`, `/restx/api/3/serverInfo` and `/browse/…`
#: are `text/html`, where `/rest` and `/rest/nope/thing` are the RFC 7807 :func:`no_endpoint`.
JIRA_REST = "/rest"


def serves_the_jira_api(path: str) -> bool:
    """Whether ``path`` is under Jira's REST mount, where a path no route serves is RFC 7807."""
    vendor_path = _instance(path)
    return vendor_path == JIRA_REST or vendor_path.startswith(f"{JIRA_REST}/")


def is_confluence(path: str) -> bool:
    """Which of the two products serves ``path``. They answer several things differently — the
    refusal envelope below, and how a query parameter is read — and one predicate keeps the
    callers from drifting apart on what counts as Confluence."""
    return path.startswith(WIKI)


#: What real Jira puts on a JSON body. Measured on a live Jira Cloud site, 2026-09-15 and
#: 2026-09-18: `serverInfo` (with and without a credential), `issueLinkType`, `project/search`, the
#: 404 for an unknown issue under both mounts, the 400 for an unparseable JQL. No space after the
#: semicolon and `UTF-8` upper-case, unlike GitHub's `application/json; charset=utf-8`.
JIRA_JSON_MEDIA_TYPE = "application/json;charset=UTF-8"


def json_media_type(path: str, status_code: int) -> str | None:
    """The `content-type` real puts on a JSON body answered at ``path`` with ``status_code``, or
    ``None`` to keep FastAPI's bare `application/json`.

    Jira names the charset above on every JSON status measured but one: the gateway's 403 for a
    bearer it cannot read as a Connect token (:func:`connect_token_body`) is the bare type,
    measured 2026-09-16 and 2026-09-18 on `serverInfo`, `field` and `project/search`. That 403 is
    the only one this server answers on a Jira path, so the status alone selects it. Confluence
    answers the bare type on every JSON body measured — the 200s, the 404s, the 400, 403 and 405
    (2026-09-15 and 2026-09-18), and the CQL search's 400s and 500 (2026-10-05) — so `/wiki` is
    ``None`` throughout; its 401 is Tomcat's HTML page, which the envelope here does not reproduce.

    The RFC 7807 refusals are not this function's: they ride on :class:`AtlassianError` under
    :data:`PROBLEM_JSON`, and ``main.vendor_json_media_type`` rewrites only a response that is
    exactly `application/json`.
    """
    if is_confluence(path) or status_code == 403:
        return None
    return JIRA_JSON_MEDIA_TYPE


# Java's `Character.isWhitespace` is documented to EXCLUDE the three non-breaking spaces and to
# include the other Unicode space separators. Measured on both products, 2026-09-15: a value
# holding U+00A0, U+2007 or U+202F between its digits is a 400 naming that value, while the same
# value holding U+2003, U+0020, a tab or a newline is thirty-four. Python's own `str.isspace()`
# calls all seven whitespace, so the three are put back by hand.
_JAVA_NON_WHITESPACE = "\u00a0\u2007\u202f"


def strip_java_whitespace(raw: str) -> str:
    """``raw`` with every character Java calls whitespace removed — not trimmed, removed: real
    reads `?maxResults=3%204` as thirty-four and names `?limit=a%20b` as ``"ab"``."""
    return "".join(ch for ch in raw if not ch.isspace() or ch in _JAVA_NON_WHITESPACE)


class AtlassianError(HTTPException):
    """A refusal whose body real sends verbatim, rather than through :func:`_body`.

    Carried on the exception the way Google's errors carry their own fields, so the router raises
    one expression and ``backlot.main``'s handler stays a dispatch. ``media_type`` rides along
    because the body and the header are one measurement: Jira's RFC 7807 body arrives on
    ``application/problem+json``, and serving the body under FastAPI's ``application/json`` would
    reproduce half of it.
    """

    def __init__(
        self,
        status_code: int,
        body: dict,
        *,
        media_type: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(
            status_code=status_code,
            detail=body.get("detail") or body.get("message"),
            headers=headers,
        )
        self.body = body
        self.media_type = media_type


def _instance(path: str) -> str:
    """The path real puts in an RFC 7807 ``instance``, which is the vendor's own, not Backlot's."""
    return path[len(PREFIX) :] if path.startswith(PREFIX) else path


def integer_conversion_failure(path: str, name: str, values: list[str]) -> AtlassianError:
    """The 400 either product answers for a query parameter it cannot read as an ``int``.

    Measured on brekkylab.atlassian.net, 2026-09-14, on `GET /rest/api/3/issue/{key}/comment` and
    `GET /wiki/rest/api/space`. The two bodies share no key: Jira's is RFC 7807 naming the
    parameter, Confluence's is two keys carrying the Spring exception's own ``toString``, which
    names the value and the type it could not reach but never the parameter.

    ``values`` is every value the query string carried for ``name``, because what real reports for
    a REPEATED parameter is the whole array rather than the one value it tried: Confluence renders
    it as the comma-join, Jira as a Java array's ``toString``. The identity hash in Jira's differs
    per request on real, so a Python ``id`` stands in — equally arbitrary, equally not a promise.

    The products also disagree on whether the value they name is the one that ARRIVED: Jira echoes
    it whitespace and all (`?maxResults=%20abc%20` names `' abc '`), Confluence names it with the
    whitespace REMOVED — the same cleaning it converts by, so `?limit=a%20b` names `"ab"` and a
    whitespace-only value names `""`. The cleaning is per value and the join comes after, which is
    the only order that matches all three of `?limit=%20abc%20&limit=2` naming `"abc,2"`,
    `?limit=abc&limit=%202%20` naming `"abc,2"` and `?limit=%20&limit=2` naming `",2"`.
    """
    if is_confluence(path):
        shown = ",".join(strip_java_whitespace(v) for v in values)
        java_type = "java.lang.String" if len(values) == 1 else "java.lang.String[]"
        return AtlassianError(
            400,
            {
                "statusCode": 400,
                "message": (
                    "org.springframework.web.method.annotation."
                    "MethodArgumentTypeMismatchException: Failed to convert value of type "
                    f"'{java_type}' to required type 'int'; nested exception is "
                    f'java.lang.NumberFormatException: For input string: "{shown}"'
                ),
            },
        )
    # Masked to 32 bits: `Integer.toHexString(hashCode())` is at most eight digits, where CPython's
    # `id` is a heap address and renders nine here — which would also put a live address on the wire.
    shown = values[0] if len(values) == 1 else f"[Ljava.lang.String;@{id(values) & 0xFFFFFFFF:x}"
    return AtlassianError(
        400,
        {
            "type": "about:blank",
            "title": "Bad Request",
            "status": 400,
            "detail": f"Failed to convert '{name}' with value: '{shown}'",
            "instance": _instance(path),
        },
        media_type=PROBLEM_JSON,
    )


def cql_required() -> AtlassianError:
    """The CQL search's refusal of a request whose first `cql` is absent or empty.

    Measured 2026-10-05: no `cql`, `?cql=` and a bare `?cql` are this 400, and so is
    `?CQL=type=page`, the name being case-sensitive. A repeated `cql` is read from its first value:
    `?cql=&cql=type=page` is this 400 and `?cql=type=page&cql=` a 200. It comes after the 404 for a
    `limit` or `start` real cannot convert (``backlot.routers.atlassian._cql_page_param``) and ahead
    of the negative `limit`/`start` refusal: `?cql=&limit=abc` is that 404 (measured 2026-10-06) and
    `?limit=-1` with no `cql` is this 400. The body carries the `data` object
    :func:`start_too_large` describes. A whitespace-only `cql` (`%20`, `%09`) is not this refusal:
    real answers it after the negative one with `Could not parse cql : `, its message for a CQL it
    cannot parse, and this server does not check CQL syntax.
    """
    return AtlassianError(
        400,
        {
            "statusCode": 400,
            "data": {"authorized": True, "valid": True, "errors": [], "successful": True},
            "message": (
                "com.atlassian.confluence.api.service.exceptions.api.BadRequestException: "
                "cql query parameter is required"
            ),
        },
    )


def start_too_large() -> AtlassianError:
    """`content`'s refusal of a `start` above 100000, which `space` does not share.

    Measured 2026-09-22: `content?start=100001` is a 400 whose body carries the `data` object
    Confluence puts on the refusals its API service layer raises, where the conversion 400 and the
    negative 400 above carry `statusCode` and `message` alone. `start=100000` is a 200, so the
    bound is inclusive.
    """
    return AtlassianError(
        400,
        {
            "statusCode": 400,
            "data": {"authorized": True, "valid": True, "errors": [], "successful": True},
            "message": (
                "com.atlassian.confluence.api.service.exceptions.api.BadRequestException: "
                "Start of this size is no longer supported. If you need to fetch this amount of "
                "content, please use either the search endpoint or get the content by a space at "
                "a time."
            ),
        },
    )


def search_cursor_refused(*, failed: bool = False) -> AtlassianError:
    """The CQL search's refusal of a `cursor` it cannot page by
    (``backlot.routers.atlassian._cql_search_after`` says which): a 400, or with ``failed`` a 500.
    Both bodies name the search service behind the route, which is where the token is read."""
    status = 500 if failed else 400
    failure = (
        "There was an error returned from XP-Search Aggregator API: HTTP/1.1 500 Internal Server "
        "Error"
        if failed
        else "There was an illegal request passed to XP-Search Aggregator API : HTTP/1.1 400 Bad "
        "Request"
    )
    scale = "com.atlassian.confluence.api.service.exceptions.scale.SSStatusCodeException"
    return AtlassianError(
        status,
        {
            "statusCode": status,
            "message": (
                f"{scale}: CQL was parsed but the search manager was unable to execute the search. "
                f"Error message: {scale}: {failure}"
            ),
        },
    )


def search_next_out_of_range() -> AtlassianError:
    """The CQL search's refusal of a page that answers `next` where `start` plus the rows served,
    or plus one on a page that served none, passes Java's `int`.

    Measured 2026-10-04 on nine matches: `?limit=2&start=2147483646` and `?limit=0&start=2147483647`
    are this 400, `?limit=1&start=2147483646` and `?limit=0&start=2147483646` are served, and so is
    `?start=2147483647`, which serves all nine and answers no `next`. The body carries the `data`
    member :func:`start_too_large`'s does.
    """
    return AtlassianError(
        400,
        {
            "statusCode": 400,
            "data": {"authorized": True, "valid": True, "errors": [], "successful": True},
            "message": (
                "com.atlassian.confluence.api.service.exceptions.api.BadRequestException: CQL was "
                "parsed but the search manager was unable to execute the search. Error message: "
                "java.lang.IllegalArgumentException"
            ),
        },
    )


def negative_not_allowed(name: str) -> AtlassianError:
    """Confluence's refusal of a negative ``limit`` or ``start``, measured 2026-09-14 on `content`
    and `space` and 2026-09-23 on the three listings under `content/{id}`. Jira does NOT share
    it — a negative there clamps to the floor and answers 200 — so this is Confluence's alone,
    and its body is the bare exception string again."""
    return AtlassianError(
        400,
        {
            "statusCode": 400,
            "message": f"java.lang.IllegalArgumentException: {name} cannot be less than zero",
        },
    )


def zero_limit_not_allowed() -> AtlassianError:
    """`label`'s refusal of `?limit=0`, which no other Confluence listing shares.

    Measured 2026-09-22 with a cache-buster on each request: `content`, `space`, the CQL search,
    `child/page`, `child/comment` and `child/attachment` all answer `?limit=0` with an empty page
    at 200, and `content/{id}/label` alone answers this 400. `?limit=1` and `?limit=2` are 200
    there, so it is the zero it refuses rather than a small page. The message is the bare exception
    string with no name in it, where the negative refusal beside it names the parameter.
    """
    return AtlassianError(
        400, {"statusCode": 400, "message": "java.lang.IllegalArgumentException: null"}
    )


def unsupported_media_type(path: str, content_type: str | None) -> AtlassianError:
    """Jira's 415 for a POST body it will not read, measured 2026-09-15 on `POST search/jql`.

    RFC 7807 again, the same five keys and the same media type as
    :func:`integer_conversion_failure`. The ``detail`` names the type that arrived, and a request
    carrying no ``Content-Type`` at all is named ``'null'`` — the literal string, which is the
    header's absence rendered by a Java formatter rather than a JSON null.
    """
    return AtlassianError(
        415,
        {
            "type": "about:blank",
            "title": "Unsupported Media Type",
            "status": 415,
            "detail": f"Content-Type '{content_type or 'null'}' is not supported.",
            "instance": _instance(path),
        },
        media_type=PROBLEM_JSON,
    )


#: A token, as Spring's `MimeType` checks one: ASCII, no control character, no separator.
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def _parse_media_type(value: str) -> str | None:
    """``value`` as Spring's `MimeTypeUtils.parseMimeType` reads it, `type/subtype;name=value`, or
    None where it cannot read it; the type is split off at the first `;`, the parameters at every
    `;` outside double quotes."""
    full, _, rest = value.partition(";")
    full = full.strip()
    if full == "*":
        full = "*/*"
    kind, _, subtype = full.partition("/")
    if not (_TOKEN.fullmatch(kind) and _TOKEN.fullmatch(subtype)) or (kind == "*" != subtype):
        return None
    parsed = f"{kind.lower()}/{subtype.lower()}"
    parameters, current, quoted = [], "", False
    for ch in rest:
        if ch == ";" and not quoted:
            parameters.append(current)
            current = ""
            continue
        quoted = not quoted if ch == '"' else quoted
        current += ch
    parameters.append(current)
    for parameter in parameters:
        name, eq, given = parameter.strip().partition("=")
        if not eq:
            continue
        name, given = name.strip(), given.strip()
        is_quoted = len(given) >= 2 and given[0] == given[-1] and given[0] in "\"'"
        if not _TOKEN.fullmatch(name) or not (is_quoted or _TOKEN.fullmatch(given)):
            return None
        if name.lower() == "charset":
            try:
                codecs.lookup(given[1:-1] if is_quoted else given)
            except LookupError:
                return None
        parsed += f";{name}={given}"
    return parsed


def _takes(consumes: tuple[str, ...], media_type: str) -> bool:
    kind, subtype = media_type.split(";", 1)[0].split("/")
    for taken in consumes:
        taken_kind, _, taken_subtype = taken.partition("/")
        if taken_kind == "*" or (taken_kind == kind and taken_subtype in ("*", subtype)):
            return True
    return False


def _has_body(headers) -> bool:
    """Whether Spring counts the request as carrying a body: a `Transfer-Encoding`, or a
    `Content-Length` other than `0`."""
    length = (headers.get("content-length") or "").strip()
    return bool((headers.get("transfer-encoding") or "").strip()) or (length not in ("", "0"))


def refuse_a_media_type(
    path: str, headers, consumes: tuple[str, ...], *, body_optional: bool = False
) -> AtlassianError | None:
    """Jira's 415 for a request whose `Content-Type` an operation that ``consumes`` those types does
    not take, or None where it takes it.

    Spring's check, and the first thing the operation does: ahead of the 401 a caller with no
    credential gets (:data:`JIRA_UNAUTHENTICATED`) and of the operation itself for one whose
    credential resolves, and behind the gateway's Connect-token 403. Measured on Jira Cloud on
    2026-09-30, on the operations ``backlot/data/jira_unserved.json`` lists:

    - the media type is compared without its parameters and whatever its case, and a type only a
      wildcard would cover (`application/*`, `*/*`, `*`) is refused where the operation does not
      take `*/*`
    - an absent `Content-Type` and an empty one are named `'null'`, and the check refuses both
      unless the operation takes `*/*`
    - where the body is optional, a request that carries none (:func:`_has_body`) is not checked
    - a value Spring cannot read (:func:`_parse_media_type`) — `foo`, `application/json,text/plain`,
      an unquoted space in a parameter, an unknown charset (`charset=nope`, `Charset=nope`) — is
      `Could not parse Content-Type.`, where `charset=latin1` and a quoted `charset="utf-8"` are
      read, and a parameter with no `=` is left out
    - ``detail`` names the type as read: type and subtype lower-cased and each parameter as sent
      after a bare `;`, `TEXT/Plain;Charset=UTF-8;X=Y` as `text/plain;Charset=UTF-8;X=Y`
    - `Accept` names ``consumes``, joined by `, `, on every 415; `Accept` on the request changes
      none of it

    A charset is looked up in Python's codec registry rather than Java's, which agree on the ones
    above.
    """
    if body_optional and not _has_body(headers):
        return None
    value = headers.get("content-type")
    if not value:
        if "*/*" in consumes:
            return None
        detail = "Content-Type 'null' is not supported."
    else:
        media_type = _parse_media_type(value)
        if media_type is not None and _takes(consumes, media_type):
            return None
        detail = (
            "Could not parse Content-Type."
            if media_type is None
            else f"Content-Type '{media_type}' is not supported."
        )
    return AtlassianError(
        415,
        {
            "type": "about:blank",
            "title": "Unsupported Media Type",
            "status": 415,
            "detail": detail,
            "instance": _instance(path),
        },
        media_type=PROBLEM_JSON,
        headers={"Accept": ", ".join(consumes)},
    )


# The three sentences Jira answers a POST body it cannot turn into an object, measured 2026-09-15.
# Each arrives as `errorMessages` ALONE — no `errors`, where the refusals below it carry one — so
# they go through :class:`AtlassianError` rather than :func:`_body`.
BODY_EMPTY = "No content to map to Object due to end of input"
BODY_UNPARSEABLE = "There was an error parsing JSON. Check that your request body is valid."
BODY_NOT_AN_OBJECT = "Invalid request payload. Refer to the REST API documentation and try again."


def body_not_read(message: str) -> AtlassianError:
    """A 400 for a body that did not deserialize. ``message`` is one of the three above."""
    return AtlassianError(400, {"errorMessages": [message]})


def failed_to_read_request(path: str) -> AtlassianError:
    """Jira's RFC 7807 refusal for a `search/jql` body its reader will not read (see
    `routers.atlassian._jira_search_body` for which). Measured 2026-10-06 on
    `POST /rest/api/3/search/jql`, in `application/problem+json` where the other body refusals use
    the `errorMessages` envelope.
    """
    return AtlassianError(
        400,
        {
            "type": "about:blank",
            "title": "Bad Request",
            "status": 400,
            "detail": "Failed to read request",
            "instance": _instance(path),
        },
        media_type=PROBLEM_JSON,
    )


def unbounded_jql() -> AtlassianError:
    """Jira's 400 for `search/jql` given no `jql` at all — on GET or POST, measured 2026-09-16
    against `brekkylab.atlassian.net`. The sentence is Backlot's own: real answers in the
    account's language, as it does the `nextPageToken` and `orderBy` refusals.
    """
    return AtlassianError(
        400,
        {
            "errorMessages": [
                "Unbounded JQL queries are not allowed here. Add a search restriction to the query."
            ],
            "errors": {},
        },
    )


def max_results_out_of_range() -> AtlassianError:
    """Jira's 400 for `search/jql`'s `maxResults` outside 1-5000, on both methods and both
    placements — the query string and the POST body. Measured 2026-09-18 against Jira Cloud:
    `0`, `-1` and `5001` are all refused, `1` and `5000` both answered 200.

    The sentence is Backlot's own, not a transcription, as with :func:`unbounded_jql` and
    :func:`bad_page_token`: real localises this one too, to the same account language.
    """
    return AtlassianError(
        400,
        {
            "errorMessages": ["The maxResults parameter must be between 1 and 5,000."],
            "errors": {},
        },
    )


def bad_page_token() -> AtlassianError:
    """Jira's 400 for a ``nextPageToken`` it cannot decode, measured 2026-09-16 on both methods —
    the query string's token and the body's are refused alike.

    The sentence is Backlot's own, not a transcription: real localises this one to the account's
    language, as it does the ``orderBy`` refusal (see ``routers.atlassian._jira_order_desc``,
    measured against the same account). The envelope is reproduced, the wording is not.
    """
    return AtlassianError(
        400,
        {
            "errorMessages": ["The provided nextPageToken is invalid or has expired."],
            "errors": {},
        },
    )


# What real answers in `Allow` on a Jira 405, per route. Measured on brekkylab.atlassian.net,
# 2026-09-17, by sending a method the vendor defines on no route of that path. Both Jira mounts were
# measured and agreed, which is why one row binds `{version}` to both: `PUT /rest/api/2/search/jql`
# answers `GET, POST` and `POST /rest/api/2/serverInfo` answers `GET`, the sets their `/3` spellings
# answer. The set is the vendor's rather than this server's, so a 405 answered here for a write the
# vendor serves carries that method in its own `Allow`.
#
# A SET rather than a string: real's order varies per RESPONSE, so no order reproduces it. Three
# `PUT /rest/api/3/search/jql` in a row answered `POST, GET`, `GET, POST` and `POST, GET`; two
# `POST /rest/api/3/issue/{key}` answered `DELETE, GET, PUT` and `PUT, GET, DELETE`. The order below
# is this module's choice and the only part of the header that is not a measurement.
#
# Two rows part from Jira's own `swagger-v3.v3.json`, which declares the same set as the measurement
# for every other route here. `project/search`: the document declares `GET` alone and real answers
# the three methods `/rest/api/3/project/{projectIdOrKey}` takes, so `search` binds as a project key
# there and the measurement is what the row states. `project/{key}/role/{id}`: a `POST` with an
# unknown key is that route's 404 rather than a 405, so no 405 could be measured and its row is the
# document's four methods alone.
_JIRA_ALLOW = (
    ("/rest/api/{version}/serverInfo", ("GET",)),
    ("/rest/api/{version}/field", ("GET", "POST")),
    ("/rest/api/{version}/issue/{key}", ("GET", "PUT", "DELETE")),
    ("/rest/api/{version}/issue/{key}/comment", ("GET", "POST")),
    ("/rest/api/{version}/search/jql", ("GET", "POST")),
    ("/rest/api/{version}/issueLinkType", ("GET", "POST")),
    ("/rest/api/{version}/project/search", ("GET", "PUT", "DELETE")),
    ("/rest/api/{version}/project/{key}/role", ("GET",)),
    ("/rest/api/{version}/project/{key}/role/{id}", ("GET", "POST", "PUT", "DELETE")),
)


def route_regex(template: str) -> re.Pattern[str]:
    """``template`` with `{version}` bound to the two Jira mounts and every other placeholder to one
    path segment. Read with ``fullmatch`` below, so `/issue/{key}` never matches
    `/issue/{key}/comment`."""
    segments = [
        "[23]" if seg == "{version}" else "[^/]+" if seg.startswith("{") else re.escape(seg)
        for seg in template.split("/")
    ]
    return re.compile("/".join(segments) + "$")


_JIRA_ALLOW_PATTERNS = tuple((route_regex(t), methods) for t, methods in _JIRA_ALLOW)


def jira_allow(path: str) -> str | None:
    """The `Allow` real sends on a 405 at ``path``, or ``None`` for a Jira route no row above
    covers. ``path`` is Backlot's, prefix and all.

    A trailing slash is stripped before matching: the path a refusal echoes may keep one
    (``backlot.routers.atlassian._echoed_path``, measured 2026-10-10), where the route tables do not.
    """
    vendor_path = _instance(path).rstrip("/")
    for pattern, methods in _JIRA_ALLOW_PATTERNS:
        if pattern.fullmatch(vendor_path):
            return ", ".join(methods)
    return None


def method_not_allowed(path: str, method: str) -> AtlassianError:
    """The 405 each product answers for a method it does not serve at ``path``.

    `HEAD` and `OPTIONS` are not among them, because real answers both rather than refusing them:
    a `HEAD` is rewritten to its GET before routing
    (``backlot.main.answer_head_as_the_get_without_its_body``) and an `OPTIONS` is answered by
    ``backlot.routers.atlassian._options_answer``, so neither arrives here.

    The one refusal the shared envelope :func:`http_body` gets wrong for both products, in two
    different directions — Confluence's `errors` is a LIST carrying no `Allow`, Jira's is RFC 7807
    on `application/problem+json` naming the methods that path takes (the table above). A client
    reading `errors[0]["code"]` is the one this costs: the shared envelope has an `errors` OBJECT,
    so that read raises against Backlot and works against real Confluence.

    ``headers`` of ``{}`` is the empty header set, not "no opinion": real sends no `Allow` on the
    Confluence side, nor for a method a layer in front of the application refuses, on a served route
    or not (:data:`SERVED_METHODS`). Starlette would compute one there from the catch-all
    (``backlot.routers.atlassian.unmatched_path``), which takes every method it lists on every path
    it owns, so what it would advertise is neither the vendor's set nor anything Backlot serves.
    The front door answers Jira's `PATCH` with 400, and a method the CDN refuses by its spelling
    with 403 or 400, rather than 405 (:data:`SERVED_METHODS`), which :func:`_refused_status`
    carries.
    """
    status = _refused_status(path, method)
    if is_confluence(path):
        return AtlassianError(
            status,
            {
                "errors": [
                    {
                        "status": status,
                        "code": http.HTTPStatus(status).name,
                        "title": (
                            "org.springframework.web.HttpRequestMethodNotSupportedException: "
                            f"Request method '{method}' not supported"
                        ),
                    }
                ]
            },
            headers={},
        )
    allow = jira_allow(path) if method in SERVED_METHODS else None
    return AtlassianError(
        status,
        {
            "type": "about:blank",
            "title": http.HTTPStatus(status).phrase,
            "status": status,
            "detail": f"Method '{method}' is not supported.",
            "instance": _instance(path),
        },
        media_type=PROBLEM_JSON,
        headers={} if allow is None else {"Allow": allow},
    )


# What real Jira names in `Allow` on an `OPTIONS`, which is NOT the set it names on a 405 above.
# Measured on Jira Cloud, 2026-09-22, one request per route: eight of the nine rows are
# that route's 405 set plus `HEAD` and `OPTIONS`, and `project/search` is the exception — its 405
# resolves `/project/{projectIdOrKey}` with `search` read as a key, where its `OPTIONS` reaches the
# search route itself and names three methods rather than five. Real spells the list without spaces
# after the commas, where its 405 `Allow` has them, and the order of the set varied between two
# requests, so the order below is one measured spelling and not a sequence a client can rely on.
_JIRA_OPTIONS_ALLOW = (
    ("/rest/api/{version}/serverInfo", "GET,HEAD,OPTIONS"),
    ("/rest/api/{version}/field", "POST,GET,HEAD,OPTIONS"),
    ("/rest/api/{version}/issue/{key}", "PUT,GET,HEAD,DELETE,OPTIONS"),
    ("/rest/api/{version}/issue/{key}/comment", "GET,HEAD,POST,OPTIONS"),
    ("/rest/api/{version}/search/jql", "GET,HEAD,POST,OPTIONS"),
    ("/rest/api/{version}/issueLinkType", "POST,GET,HEAD,OPTIONS"),
    ("/rest/api/{version}/project/search", "GET,HEAD,OPTIONS"),
    ("/rest/api/{version}/project/{key}/role", "GET,HEAD,OPTIONS"),
    ("/rest/api/{version}/project/{key}/role/{id}", "DELETE,POST,PUT,GET,HEAD,OPTIONS"),
)

_JIRA_OPTIONS_PATTERNS = tuple((route_regex(t), allow) for t, allow in _JIRA_OPTIONS_ALLOW)

#: The `content-type` real sends on a Jira `OPTIONS`, over an empty body.
JIRA_OPTIONS_MEDIA_TYPE = "text/html;charset=UTF-8"


def jira_options_allow(path: str) -> str | None:
    """The `Allow` real sends on an `OPTIONS` at ``path``, or ``None`` for a path no row covers."""
    vendor_path = _instance(path)
    for pattern, allow in _JIRA_OPTIONS_PATTERNS:
        if pattern.fullmatch(vendor_path):
            return allow
    return None


#: The methods the application behind the gateway ever sees. Two layers in front of it refuse the
#: rest, each with its own HTML page and no `Allow`, on a served route and on an unserved path
#: alike: measured 2026-09-30 with the 40 methods of the IANA registry on `serverInfo`, `space` and
#: `nopesuchroute` under both mounts, and with `AB` followed by each of the 94 ASCII characters
#: from `!` to `~` on `serverInfo` and `space` (and `TRACE` on 2026-09-22 too). The CDN
#: (`server: CloudFront`) answers `TRACE` and `CONNECT` with 405 and neither
#: `x-content-type-options` nor `x-xss-protection` (:data:`CDN_405`). Any other method it answers
#: by spelling (:func:`cdn_forbids`): one to nine characters of `A`-`Z`, `-` and `_` — `PROPFIND`,
#: `QUERY`, `M-SEARCH`, `ABCDEFGHI` — with 403 and those two alone, and the other spellings measured
#: with 400 and neither — ten characters or more (`MKACTIVITY`, `VERSION-CONTROL`), a lower-case
#: letter (`get`, `Get`), a digit (`FOO1`) or any other punctuation (`G.T`). The gateway behind it
#: (`server: AtlassianEdge`) answers a `PATCH` — Jira's nginx with 400 on `nopesuchroute`,
#: `serverInfo` and an issue, Confluence's openresty with 405 on `space` and `nopesuchroute` — and
#: puts its two ids, `nosniff` and `x-xss-protection` on that refusal and none of the application's
#: headers (:data:`GATEWAY_REFUSED`). So a method outside this set gets no vendor `Allow` below and
#: none of the application's headers in ``backlot.routers.atlassian.vendor_headers``; the body it
#: gets here is still this module's JSON, where real's is that HTML page. Over `backlot.serve()`,
#: uvicorn's parser answers a method it has no name for (`FOO`, `get`) with its own 400 before the
#: application sees it; the ones it names (`CONNECT`, `PROPFIND`, `MKACTIVITY`) reach the rule here.
SERVED_METHODS = ("GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD")

#: The methods the gateway refuses itself, rather than the CDN in front of it.
GATEWAY_REFUSED = ("PATCH",)

#: The methods the CDN refuses with 405, where it refuses the others by their spelling.
CDN_405 = ("TRACE", "CONNECT")

_CDN_403_SPELLING = re.compile(r"[A-Z_-]{1,9}")


def cdn_forbids(method: str) -> bool:
    """Whether the CDN answers ``method``, one it does not pass on, with its 403 and the edge's two
    headers rather than its 405 or its 400 (:data:`SERVED_METHODS` has the measurement)."""
    return method not in CDN_405 and _CDN_403_SPELLING.fullmatch(method) is not None


def _refused_status(path: str, method: str) -> int:
    """The status of the refusal for ``method``: the application's 405 for one it sees, and the
    front door's for one it does not, as :data:`SERVED_METHODS` measures them."""
    if method in SERVED_METHODS or method in CDN_405:
        return 405
    if method in GATEWAY_REFUSED:
        return 405 if is_confluence(path) else 400
    return 403 if cdn_forbids(method) else 400


def no_endpoint(path: str, method: str) -> AtlassianError:
    """Jira's 404 for a path it mounts no endpoint at, in the RFC 7807 shape its other refusals use.

    Measured on Jira Cloud, 2026-09-22: `/rest/api/3/nopesuchroute`, the same under `/rest/api/2`,
    `/rest/api/4/serverInfo`, `/rest/nope/thing`, and the paths that extend a served route
    (`serverInfo/extra`, `issue/NOPE-1/nope`, `project/search/extra`) each answer this body.
    `detail` names the method as sent -- `GET`, `POST`, `DELETE` and `OPTIONS` each came back in it,
    and `PUT` with no credential on 2026-09-30 -- and both fields carry the vendor path with the
    query string left off. A credential changes nothing, and neither does `Accept`.
    """
    vendor_path = _instance(path)
    return AtlassianError(
        404,
        {
            "type": "about:blank",
            "title": "Not Found",
            "status": 404,
            "detail": f"No endpoint {method} {vendor_path}.",
            "instance": vendor_path,
        },
        media_type=PROBLEM_JSON,
    )


#: Confluence's JAX-RS 404 for a segment under `/wiki/rest/api/` that no resource claims. The shape
#: follows `Accept` rather than the credential: measured 2026-09-22 on `nopesuchroute` and
#: `nope/deeper/still`, `Accept: application/json` answers the JSON below and every other value --
#: absent, `*/*`, `application/xml` -- answers the XML, with or without a credential. The message
#: carries the full request URL, query string included.
JAXRS_XML_MEDIA_TYPE = "application/xml"
JAXRS_JSON_MEDIA_TYPE = "application/json"
_JAXRS_MESSAGE = "null for uri: {url}"


def jaxrs_not_found(url: str, *, as_json: bool) -> tuple[str, str]:
    """``(media_type, body)`` for that 404, in whichever of the two shapes ``Accept`` asks for.

    The XML escapes what XML requires and nothing else, which is what real does: a path carrying
    `&` came back `&amp;` and one carrying `'` came back unescaped (2026-09-22). A path carrying
    `<script>`, encoded or not, never reaches the application there -- the gateway answers its own
    403 HTML page -- so that one is unmeasurable rather than measured.
    """
    message = _JAXRS_MESSAGE.format(url=url)
    if as_json:
        return JAXRS_JSON_MEDIA_TYPE, json.dumps(
            {"message": message, "status-code": 404}, separators=(",", ":")
        )
    return JAXRS_XML_MEDIA_TYPE, (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<status><status-code>404</status-code><message>{escape(message)}</message></status>"
    )


#: Confluence answers the product's own HTML page, not the API 404, for a path that extends a
#: resource it serves (which ones is ``backlot.routers.atlassian._CONFLUENCE_HTML_RESOURCES``) and
#: for `/wiki/rest/nope` outside the API mount, with `Accept: application/json` and without it
#: alike, measured 2026-09-22. Real's body is a ~30KB build-specific shell whose script tags name
#: the deploy; this is a stub with the status and the media type, which is the part a client
#: branches on.
HTML_MEDIA_TYPE = "text/html;charset=UTF-8"
#: Jira's own site page for a path it serves nothing at — `/foo`, `/ex/jira/x`, `/restx/api/3/…`,
#: titled "Oops, you've found a dead link." — spells the charset in lower case, measured 2026-09-30.
JIRA_SITE_HTML_MEDIA_TYPE = "text/html;charset=utf-8"
HTML_NOT_FOUND = (
    "<!DOCTYPE html><html><head><title>Not Found</title></head>"
    "<body><p>This page could not be found.</p></body></html>"
)

#: What an `OPTIONS` on a Confluence route answers: the 404 above in the `errors` list its 405 uses,
#: on every route measured 2026-09-22 but `search`, which answers by `Accept`
#: (``backlot.routers.atlassian._search_options``). That is for `*/*`, `application/json` or no
#: `Accept`; `text/html` or `application/xml` is a 403 HTML page there instead, measured on each of
#: those routes 2026-09-30, which this server does not answer.
CONFLUENCE_OPTIONS_NOT_FOUND = {
    "errors": [{"status": 404, "code": "NOT_FOUND", "title": "Not Found"}]
}
CONFLUENCE_OPTIONS_BY_ACCEPT = "/wiki/rest/api/search"
CONFLUENCE_SEARCH_OPTIONS_ALLOW = "OPTIONS,HEAD,GET"


#: The Confluence route whose 200 declares no length on a `HEAD` (:func:`head_content_length`).
_CONFLUENCE_HEAD_NO_LENGTH = "/wiki/rest/api/search"


def head_content_length(path: str, status_code: int) -> bool:
    """Whether a `HEAD` at ``path`` declares the length of the body its `GET` would have carried,
    as real does.

    Measured with `curl -I` beside each `GET` the same minute. Confluence, 2026-09-22: every 200 but
    `search` declares it, to the byte (`space` 1103, `content` 1536, `content/{id}` 3909,
    `child/comment` 214, `child/page` 211, `label` 207, `restriction/byOperation` 801, `space/{key}`
    695), and the 404s, the 405 and the CQL 400 declare none. Jira's answers are chunked and
    declare none on either method (2026-09-22), but for the 401 it refuses a caller with no
    credential with (:data:`JIRA_UNAUTHENTICATED`), which declares its 53 bytes, and the `HEAD`
    beside it declares the same (`myself`, 2026-09-30).
    """
    if not is_confluence(path):
        return status_code == 401
    if _instance(path) == _CONFLUENCE_HEAD_NO_LENGTH:
        return False
    return 200 <= status_code < 300


def _body(status_code: int, detail) -> dict:
    try:
        reason = http.HTTPStatus(status_code).phrase
    except ValueError:
        reason = "Error"
    message = detail if isinstance(detail, str) else str(detail)
    return {
        "statusCode": status_code,
        "message": message,
        "reason": reason,
        "errorMessages": [message],
        "errors": {},
    }


def http_body(path: str, exc, query=None) -> dict:
    """``path`` and ``query`` are both unused: one envelope covers every Atlassian ROUTE, unlike
    Google's per-family split, and no Atlassian refusal is selected by a query parameter the way
    Google's is by `$.xgafv`. They are in the signature so the dispatch in ``__init__`` can treat
    every module alike. A path that is not a route is answered elsewhere and in a shape of its own
    — see :func:`no_endpoint`, :func:`jaxrs_not_found` and :data:`HTML_NOT_FOUND`.

    An :class:`AtlassianError` is the exception to the one envelope and says so by carrying its
    own body, which is served as it stands."""
    body = getattr(exc, "body", None)
    return body if body is not None else _body(exc.status_code, exc.detail)


def validation_body(path: str, errors) -> tuple[int, dict]:
    """A 422 from FastAPI's own request validation, in the same envelope. The per-field detail
    collapses to one sentence because that is the only part a client reads.

    ``path`` is unused, as in :func:`http_body`: one envelope covers every Atlassian route."""
    message = "; ".join(e.get("msg", "invalid request") for e in errors) or "Invalid request"
    return 422, _body(422, message)
