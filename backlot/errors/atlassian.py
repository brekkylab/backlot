"""Atlassian Cloud's error envelopes.

Atlassian clients (atlassian-python-api, which mcp-atlassian uses) parse an error body as Atlassian
Cloud's shape — Confluence's ``raise_for_status`` does ``response.json()["message"]`` — so FastAPI's
default ``{"detail": …}`` turns every error into a cryptic ``KeyError: 'message'`` inside the client
rather than the error Backlot meant to report. The two products do not share an envelope, and
:func:`http_body` answers each in the one it measures.

Neither envelope is the only shape its product answers in: a refusal real gives in a shape
measured to differ carries its own body on an :class:`AtlassianError` and is served verbatim.
Reading a query parameter is one such refusal, in a different shape on each product — see
:func:`integer_conversion_failure`.

Nor is the body always JSON. A path neither product serves is Jira's RFC 7807 :func:`no_endpoint`,
Confluence's JAX-RS 404 in whichever of two shapes `Accept` asks for (:func:`jaxrs_not_found`), or
the product's HTML page (:data:`HTML_NOT_FOUND`) — three shapes measured on the same day, each one
what that URL answers rather than what this module would otherwise send.
"""

from __future__ import annotations

import base64
import codecs
import hashlib
import html
import json
import math
import re
import struct
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote
from xml.sax.saxutils import escape

from fastapi import HTTPException, Request, Response

from backlot import java_timsort

PREFIX = "/atlassian"
WIKI = f"{PREFIX}/wiki"
_PACKAGE = Path(__file__).resolve().parent.parent

# Jira answers a type-conversion failure as RFC 7807, with a charset real puts on it and FastAPI
# does not. Measured on brekkylab.atlassian.net, 2026-09-14.
PROBLEM_JSON = "application/problem+json;charset=UTF-8"

# The two refusals Confluence gives a caller it will not serve, measured against two Atlassian Cloud
# sites on 2026-09-04. A credential it read and rejected, and a request that carried none, get the
# 403 below verbatim on every served route but `search` (:data:`CONFLUENCE_NOT_PERMITTED_BODY`) —
# the vendor reports its own exception class in the message, and a client that logs the body logs
# that. A Basic value it could not read gets a 401 instead, whose real body is Tomcat's HTML page
# titled "HTTP Status 401 - Unauthorized"; that title is the message here, because the envelope this
# module exists to emit is JSON and a client parsing `message` out of the page would find nothing.
CONFLUENCE_FORBIDDEN = (
    "com.atlassian.confluence.mvc.rest.common.exception.StacklessResponseStatusException: "
    '403 FORBIDDEN "Request rejected because caller cannot access Confluence"'
)
CONFLUENCE_UNAUTHORIZED = "Unauthorized"
#: That 403 as real sends it, byte for byte: two members, 190 bytes on 2026-09-30, the same two a
#: served route's refusal carries (:func:`http_body`).
CONFLUENCE_FORBIDDEN_BODY = json.dumps(
    {"statusCode": 403, "message": CONFLUENCE_FORBIDDEN}, separators=(",", ":")
).encode()

# What a `<site>.atlassian.net` gateway answers a `Bearer` it cannot read as a Connect session
# JWT, on every Jira route — `serverInfo` and `field`, which need no credential, included. One
# `error` key and nothing else: neither Jira's envelope (:func:`_jira_envelope`) nor Confluence's,
# so it is returned as its own body rather than shaped. Measured on two Jira Cloud sites,
# 2026-09-04; the XML it answers instead is :func:`connect_token_answer`.
CONNECT_TOKEN_UNREADABLE = "Failed to parse Connect Session Auth Token"


def connect_token_body() -> dict:
    return {"error": CONNECT_TOKEN_UNREADABLE}


# What Jira answers a request with no credential it resolves — none, a Basic pair it read and
# rejected, an unknown scheme, and on an `OPTIONS` an unreadable bearer too — at an operation that
# will not run anonymously: 401, this line, with
# `WWW-Authenticate: OAuth realm="<the site, percent-encoded>"`. Jira's own answer, not the
# gateway's: it carries what ``backlot.routers.atlassian.vendor_headers`` puts on Jira's answers and
# not what that puts on the gateway's own refusals, and an operation's media-type check
# (:func:`refuse_a_media_type`) and its `Accept` check (:func:`not_acceptable`) answer before it.
# Which operations those are is ``backlot.routers.atlassian._published_answer``'s to say; measured
# on Jira Cloud, 2026-09-30. The type, the encoding and whether the line is sent at all are picked
# from `Accept` (:func:`jira_unauthenticated`).
JIRA_UNAUTHENTICATED = "Client must be authenticated to access this resource."

# Confluence's second anonymous refusal, which a few of its services give instead of
# :data:`CONFLUENCE_FORBIDDEN`: the members in the other order, and on every one measured but the
# Connect module path, `cache-control: no-cache, no-store, must-revalidate` and a 1970 `expires`
# beside it. Measured 2026-09-30; the paths are ``backlot.routers.atlassian``'s.
CONFLUENCE_NOT_PERMITTED = "Current user not permitted to use Confluence"
CONFLUENCE_NOT_PERMITTED_BODY = json.dumps(
    {"message": CONFLUENCE_NOT_PERMITTED, "statusCode": 403}, separators=(",", ":")
).encode()


#: The characters besides ASCII letters and digits whose escape each product decodes in the path a
#: refusal echoes, read off `GET …/nope%XXx` for every byte from `%01` to `%7F`. Jira's 404
#: (2026-10-10 and 2026-10-17) decodes its set and echoes any other escape with its hex in upper
#: case (`%3c` as `%3C`, `%c3%a9` as `%C3%A9`); its conversion 400 and 405 gave the same answer for
#: the eighteen escapes tried on them (2026-10-17). Confluence's `null for uri:` 404 (2026-10-17)
#: decodes RFC 3986's unreserved characters alone and echoes any other escape as it arrived (`%21`
#: as `%21`, `%3c` as `%3c`). Both decode one layer only: `%2541` stays `%2541`. `%3B` and `%5C`,
#: and Confluence's `%2F` and Jira's `%5B` and `%5D`, get another answer than an echo.
_JIRA_ECHO_DECODES = "!$&'()*+,-./:=@_~"
_CONFLUENCE_ECHO_DECODES = "-._~"
_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")


def echoed_spelling(raw_path: str, *, confluence: bool) -> str:
    """The path as Jira or Confluence echoes it in a refusal, from the path as it arrived.

    Each escape is decoded or kept as the product's set above says, in one pass, so an escape a
    decoded `%25` would form is not read again. Then a run of slashes is one, a decoded `%2F` among
    them (Jira's `nope%2F%2Fx` comes back `nope/x`, 2026-10-17), and a trailing slash is kept.
    """
    decodes = _CONFLUENCE_ECHO_DECODES if confluence else _JIRA_ECHO_DECODES

    def one(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        if char.isascii() and (char.isalnum() or char in decodes):
            return char
        return match.group(0) if confluence else match.group(0).upper()

    return re.sub("/{2,}", "/", _ESCAPE.sub(one, raw_path))


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
    """A refusal whose body real sends verbatim, rather than in a product envelope
    (:func:`_jira_envelope`, :func:`_confluence_envelope`).

    Carried on the exception the way Google's errors carry their own fields, so the router raises
    one expression and ``backlot.main``'s handler stays a dispatch. ``media_type`` rides along
    because the body and the header are one measurement: Jira's RFC 7807 body arrives on
    ``application/problem+json``, and serving the body under FastAPI's ``application/json`` would
    reproduce half of it.

    A body of ``bytes`` is not JSON at all and goes out as it stands, with no `content-type` but one
    ``headers`` names: JAX-RS's empty refusals are that (:func:`rendered`).
    """

    def __init__(
        self,
        status_code: int,
        body: dict | bytes,
        *,
        media_type: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(
            status_code=status_code,
            detail=(body.get("detail") or body.get("message")) if isinstance(body, dict) else None,
            headers=headers,
        )
        self.body = body
        self.media_type = media_type


def _instance(path: str) -> str:
    """The path real puts in an RFC 7807 ``instance``, which is the vendor's own, not Backlot's."""
    return path[len(PREFIX) :] if path.startswith(PREFIX) else path


#: A `[` or `]` in a Jira path, escaped or bare, which turns two of its RFC 7807 refusals into pages
#: of the web app's, under `Accept: application/json` and `*/*` alike (2026-10-18): the no-endpoint
#: 404 at `nope%5Bx` into the dead-link page (:func:`no_endpoint`), and the conversion 400 at
#: `issue/A%5BB/comment?maxResults=abc` into :data:`_JIRA_NULL_ERROR_PAGE`; the same for `%5D`. At
#: `issue/A%5BB` the issue's own JSON 404 stands. On 2026-10-19 `nope%5bx` and the bare `nope[x`
#: were the dead-link page, and `issue/A[B/comment?maxResults=abc` the `(null)` page. Its 405, 415
#: and 406 are Tomcat's pages instead (:func:`_jira_tomcat_refusal`), whatever the `Accept`:
#: measured on Jira Cloud on 2026-10-21 on `POST issue/A%5BB`, `DELETE issue/A%5BB/comment` and
#: `POST issue/A%5BB/comment` with a `text/plain` body under `application/json`, `*/*`, `text/html`,
#: `application/xml`, `foo` and none, and on `GET issue/A%5BB` under `text/html`, `application/xml`
#: and `foo`, with a credential and without, the page each time, where `foo` leaves the RFC 7807
#: answer at `A-1` with no body (:func:`rendered`). The key spelled `A%5DB`, `A%5bB`, `A[B` or
#: `A]B`, and `/rest/api/2`, answered alike.
_JIRA_BRACKET = re.compile(r"%5[BD]|[\[\]]", re.IGNORECASE)
#: That 400's page: real's is 66,815 bytes on `text/html;charset=utf-8`, titled `(null)`; this is a
#: stub with the status, the media type, the title and the one line it reads.
_JIRA_NULL_ERROR_PAGE = (
    "<!DOCTYPE html><html><head><title>(null)</title></head><body>"
    '<p>Encountered a "null - null" error while loading this page.</p></body></html>'
)


def _jira_tomcat_refusal(status: int, headers: dict[str, str]) -> AtlassianError:
    """Tomcat's page for Jira's ``status`` on a path carrying a bracket (:data:`_JIRA_BRACKET`), on
    :data:`TOMCAT_MEDIA_TYPE` with `Content-Language: en` and ``headers`` beside it: the ones the
    RFC 7807 answer carries, `Allow` on the 405 and `Accept` on the 415 and the 406."""
    return AtlassianError(
        status,
        tomcat_page(status).encode(),
        headers={"Content-Type": TOMCAT_MEDIA_TYPE, "Content-Language": "en", **headers},
    )


def integer_conversion_failure(path: str, name: str, values: list[str]) -> AtlassianError:
    """The 400 either product answers for a query parameter it cannot read as an ``int``, which
    Jira also answers for one it cannot read as a boolean and for a path variable it cannot read as
    a `long` (``backlot.routers.atlassian._role_id``).

    Measured on Jira Cloud and Confluence Cloud, 2026-09-14, on
    `GET /rest/api/3/issue/{key}/comment` and `GET /wiki/rest/api/space`. The two bodies share no
    key: Jira's is RFC 7807 naming the parameter, Confluence's is two keys carrying the Spring
    exception's own ``toString``, which names the value and the type it could not reach but never
    the parameter.

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
    if _JIRA_BRACKET.search(path):
        return AtlassianError(
            400, _JIRA_NULL_ERROR_PAGE.encode(), headers={"Content-Type": JIRA_SITE_HTML_MEDIA_TYPE}
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


def missing_path_variable(path: str, name: str) -> AtlassianError:
    """Jira's 400 for a path variable that is whitespace and nothing else, which Spring reads as
    absent: RFC 7807 saying `Required path variable '<name>' is not present.`, measured 2026-10-13
    on `project/{key}/role/{id}` with `%20`, `%09` and `%E2%80%83`."""
    return AtlassianError(
        400,
        {
            "type": "about:blank",
            "title": "Bad Request",
            "status": 400,
            "detail": f"Required path variable '{name}' is not present.",
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
    cannot parse, which is :func:`cql_not_parsed`.
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


def _service_refusal(status: int, exception: str, message: str) -> AtlassianError:
    """A refusal Confluence's API service layer raises: `statusCode`, the `data` object it always
    carries, and the exception's name before its message, as :func:`start_too_large` records."""
    return AtlassianError(
        status,
        {
            "statusCode": status,
            "data": {"authorized": True, "valid": True, "errors": [], "successful": True},
            "message": f"com.atlassian.confluence.api.service.exceptions.{exception}: {message}",
        },
    )


def cql_not_parsed() -> AtlassianError:
    """The CQL search's refusal of a `cql` it cannot parse (``routers.atlassian._cql_refusal``).
    Measured on Confluence Cloud on 2026-10-22 and 2026-10-25: `"`, `type=page and`, `type=page)`
    and U+1F600 alone each answered this 400 after the caller check, as did `order by created`,
    `not type=page`, `type=page OR NOT type=blogpost`, `type in ()` and `type=page 5`, and a
    character ``routers.atlassian._CQL_TOKEN`` makes a token no clause takes, between two
    letters."""
    return _service_refusal(400, "api.BadRequestException", "Could not parse cql : ")


def cql_expecting_value(operator: str, field: str, got: str | None = None) -> AtlassianError:
    """The CQL search's refusal of an operator with no value after it: `type=` answered
    `Expecting a value for operator '=' after field 'type'`, and `type==page` the same with
    ` but got '='` (Confluence Cloud, 2026-10-22). `title~*` named `~` and `title`, `type in page`
    `in` and ` but got 'page'`, `type not in page` the operator as `notin`, and `type in (page`
    ` but got '('` (2026-10-25)."""
    message = f"Expecting a value for operator '{operator}' after field '{field}'"
    if got is not None:
        message += f" but got '{got}'"
    return _service_refusal(400, "api.BadRequestException", message)


def cql_unrecognised_clause(text: str) -> AtlassianError:
    """The CQL search's refusal of words or quoted values after a whole query, named run together:
    `type=page b` answered `Unrecognised clause at : b`, `title~a b c` `… at : bc` and
    `title~a "b"` `… at : "b"` (Confluence Cloud, 2026-10-25)."""
    return _service_refusal(400, "api.BadRequestException", f"Unrecognised clause at : {text}")


def cql_missing_closing_parenthesis() -> AtlassianError:
    """The CQL search's refusal of a `(` that is never closed: `(type=page` answered
    `Missing closing parenthesis near field (()` (Confluence Cloud, 2026-10-22). Only that one
    query was measured, so its text is answered for every unclosed `(`."""
    return _service_refusal(
        400, "api.BadRequestException", "Missing closing parenthesis near field (()"
    )


def no_space_with_key(space_key: str) -> AtlassianError:
    """`content`'s refusal of a space key naming no space, as sent: the key is not trimmed (`' '` is
    named as a space), and a repeated one is its comma-join, which no key holds. Where it comes
    among the route's other refusals is ``routers.atlassian._CONTENT_TYPES``'s."""
    return _service_refusal(404, "api.NotFoundException", f"No space with key : {space_key}")


def space_not_found(space_key: str) -> AtlassianError:
    """``space/{key}``'s refusal of a key naming no space, with ``expand`` and without: two
    members, the message naming the key as the path decodes it, in the case it was sent in
    (``space/nosuch`` names ``nosuch``, ``space/NO%20SUCH`` names ``NO SUCH``). Measured on
    Confluence Cloud, 2026-10-11 and 2026-10-12. ``content`` refuses the same key with
    :func:`no_space_with_key`.
    """
    return AtlassianError(
        404,
        {
            "statusCode": 404,
            "message": (
                "org.springframework.web.server.ResponseStatusException: "
                f'404 NOT_FOUND "No space found with key : {space_key}"'
            ),
        },
    )


def unknown_content_type(type_value: str) -> AtlassianError:
    """`content`'s refusal of a content type Confluence does not have, named as sent, when neither
    a space nor a title is given. The types it has, and the measurement, are
    ``routers.atlassian._CONTENT_TYPES``'s."""
    return _service_refusal(
        501,
        "unchecked.NotImplementedServiceException",
        f"Cannot find custom content type : {type_value}",
    )


def content_finder_cannot_fetch(content_type: str) -> AtlassianError:
    """`content`'s 501 for `type=comment` and `type=folder`, measured 2026-10-10: the message names
    the type in the plural."""
    return _service_refusal(
        501,
        "unchecked.NotImplementedServiceException",
        f"Cannot fetch {content_type}s with ContentFinder",
    )


#: The six routes under `content/{id}`, each with the sentence real's 404 carries for it and
#: whether the `data` object beside it is the refused shape or the readable one. Measured on
#: Confluence Cloud, 2026-10-16, on ids naming no page (`999999999`, `888888888`, `-1`, `0`,
#: `9223372036854775807`): `content/{id}` and `label` name the id bare and answer the refused
#: shape, the three `child/*` routes name it as a `ContentId` and answer the readable one, and
#: `restriction/byOperation` wraps that `ContentId` in angle brackets.
_CONTENT_NOT_FOUND = {
    "content": (lambda i: f"No content found with id : {i}", False),
    "label": (lambda i: f"No content found with id : {i}", False),
    "child/page": (
        lambda i: f"No parent or not permitted to view content with id : ContentId{{id={i}}}",
        True,
    ),
    "child/comment": (
        lambda i: f"No parent or not permitted to view content with id : ContentId{{id={i}}}",
        True,
    ),
    "child/attachment": (lambda i: f"No content found with id: ContentId{{id={i}}}", True),
    "restriction/byOperation": (
        lambda i: f"No content with id <ContentId{{id={i}}}> can be found",
        False,
    ),
}


def content_not_found(route: str, content_id: int, *, attachment: bool = False) -> AtlassianError:
    """Confluence's 404 for a content id naming no page, in the three members and the two `data`
    shapes :data:`_CONTENT_NOT_FOUND` records.

    The id named is the one READ, never the one sent: `content/05/child/page` names
    `ContentId{id=5}`, because real converts the path variable before it looks the page up
    (:func:`content_id_not_convertible` is what refuses the ones it cannot). A page the caller may
    not read answers this too, so its 404 and a made-up id's differ only in the id each names.

    ``attachment`` is what `content/{id}` and `label` pass for an id sent as `att<long>`, which
    names an attachment, and no corpus record is one. Those two routes answer it with a third shape
    whatever the number, a readable page's included: measured on Confluence Cloud, 2026-10-21,
    `content/att229469` and `content/att229469/label` (229469 a page that serves) and
    `att999999999` on both are `NotFoundException: null` beside `authorized` false and `valid`
    true. The other four routes read the number alone and answer as for the bare id.
    """
    sentence_of, readable = _CONTENT_NOT_FOUND[route]
    if attachment:
        sentence = "null"
        data = {"authorized": False, "valid": True, "errors": [], "successful": False}
    elif readable:
        sentence = sentence_of(content_id)
        data = {"authorized": True, "valid": True, "errors": [], "successful": True}
    else:
        sentence = sentence_of(content_id)
        data = {
            "authorized": False,
            "valid": False,
            "errors": [{"message": {"translation": sentence, "args": []}}],
            "successful": False,
        }
    return AtlassianError(
        404,
        {
            "statusCode": 404,
            "data": data,
            "message": (
                f"com.atlassian.confluence.api.service.exceptions.api.NotFoundException: {sentence}"
            ),
        },
    )


def content_id_not_convertible(value: str) -> AtlassianError:
    """Spring's 400 for a `content/{id}` path variable Confluence's `ContentId` cannot parse, the
    value named as the path decodes it.

    What ends the message depends on the value's first three characters. After `att`, it is the
    `NumberFormatException` of the `long` that should follow, naming what does: `att` names `""`,
    `attabc` names `"abc"`. Otherwise it is `Can't parse as a ContentId`. Measured on Confluence
    Cloud, 2026-10-16 (`abc`, `1.5`, `1e3`, a space, `9223372036854775808`, `-9223372036854775809`,
    `99999999999999999999`) and 2026-10-21 (`att`, `attabc`, `att%205`, `att9223372036854775808`,
    and `ATT999999999` and `Att5`, which end as `abc` does).
    """
    if value.startswith("att"):
        cause = f'java.lang.NumberFormatException: For input string: "{value[3:]}"'
    else:
        cause = (
            "com.atlassian.confluence.api.service.exceptions.api.BadRequestException: "
            "Can't parse as a ContentId"
        )
    return AtlassianError(
        400,
        {
            "statusCode": 400,
            "message": (
                "org.springframework.web.method.annotation."
                "MethodArgumentTypeMismatchException: Failed to convert value of type "
                "'java.lang.String' to required type "
                "'com.atlassian.confluence.api.model.content.id.ContentId'; nested exception is "
                "org.springframework.core.convert.ConversionFailedException: Failed to convert "
                "from type [java.lang.String] to type [@org.springframework.web.bind.annotation."
                f"PathVariable com.atlassian.confluence.api.model.content.id.ContentId] for "
                f"value [{value}]; nested exception is {cause}"
            ),
        },
    )


def not_a_custom_content_type(content_type: str) -> AtlassianError:
    """`content`'s 400 for `type=whiteboard`, `database` and `embed`, measured 2026-10-10: the bare
    exception string, as :func:`negative_not_allowed` is."""
    return AtlassianError(
        400,
        {
            "statusCode": 400,
            "message": (
                f"java.lang.IllegalArgumentException: Type is not a custom content type : "
                f"{content_type}"
            ),
        },
    )


def attachment_without_container() -> AtlassianError:
    """`content`'s 500 for `type=attachment`, which real answers alone, beside a space and beside a
    title alike (2026-10-10): a `NullPointerException` with no `data`."""
    return AtlassianError(
        500,
        {
            "statusCode": 500,
            "message": (
                'java.lang.NullPointerException: Cannot invoke "com.atlassian.confluence.api.model.'
                'content.id.ContentId.asLong()" because "containerId" is null'
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
    return _service_refusal(
        400,
        "api.BadRequestException",
        "Start of this size is no longer supported. If you need to fetch this amount of content, "
        "please use either the search endpoint or get the content by a space at a time.",
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


def label_prefixes_not_converted(parts: list[str]) -> AtlassianError:
    """`label`'s refusal of a `prefix` it cannot convert, naming every element it read
    (``backlot.routers.atlassian._refuse_a_label_prefix`` says which). The body carries the `data`
    object :func:`start_too_large` describes."""
    return AtlassianError(
        400,
        {
            "statusCode": 400,
            "data": {"authorized": True, "valid": True, "errors": [], "successful": True},
            "message": (
                "com.atlassian.confluence.api.service.exceptions.api.BadRequestException: "
                f"Could not convert label prefixes :[{', '.join(parts)}]"
            ),
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


def _load_java_charsets() -> dict[str, str]:
    table = json.loads((_PACKAGE / "data" / "java_charsets.json").read_text(encoding="utf-8"))
    return {
        spelling.lower(): name
        for name, aliases in table["charsets"].items()
        for spelling in (name, *aliases)
    }


#: Every name and alias Java gives a charset, lower-cased, to the charset's own name: the Java
#: runtime's registry, copied in ``backlot/data/java_charsets.json``, since `Charset.forName` is
#: what real asks. Python's registry differs both ways: `u8` and `utf_8` were no charset to real
#: and `x-MacRoman` was one, 2026-10-13.
_JAVA_CHARSETS = _load_java_charsets()


def _java_charset(name: str) -> str | None:
    """The charset Java names ``name``, whatever its case, or None for one it does not know
    (`bogus`, `base64`, `utf-7`)."""
    return _JAVA_CHARSETS.get(name.lower())


def _python_codec(charset: str) -> str:
    """The Python codec that writes the Java charset ``charset`` names, or ASCII where Python has
    none by Java's name for it (`x-MacRoman`), which writes the ASCII line the same."""
    try:
        return codecs.lookup(_java_charset(charset) or charset).name
    except LookupError:
        return "ascii"


def _split_parameters(text: str) -> list[str]:
    """``text`` split at every `;` outside double quotes."""
    parameters, current, quoted = [], "", False
    for ch in text:
        if ch == ";" and not quoted:
            parameters.append(current)
            current = ""
            continue
        quoted = not quoted if ch == '"' else quoted
        current += ch
    parameters.append(current)
    return parameters


def _spring_parameters(listed: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The parameters Spring's `MimeType` keeps of those ``listed``: first one per name as sent,
    with its last value where the name first stood (a `LinkedHashMap`), then one per name in any
    case, under the spelling put last and with its value (a `LinkedCaseInsensitiveMap`), so
    `q=0.5;Q=2;q=0.5` keeps `Q=2`."""
    as_sent: dict[str, str] = {}
    for name, value in listed:
        as_sent[name] = value
    kept: dict[str, str] = {}
    spelled: dict[str, str] = {}
    for name, value in as_sent.items():
        before = spelled.get(name.lower())
        if before is not None and before != name:
            del kept[before]
        spelled[name.lower()] = name
        kept[name] = value
    return list(kept.items())


def _spring_media_type(
    value: str, *, charset_in_any_case: bool, check_q: bool = True
) -> tuple[str, str, list[tuple[str, str]]] | None:
    """``value`` as Spring's `MediaType.parseMediaType` reads it, `(type, subtype, parameters)` with
    the parameters it keeps (:func:`_spring_parameters`), or None where it cannot read it: the type
    is split off at the first `;`, the parameters at every `;` outside double quotes. Measured as
    an `Accept` member and as a `Content-Type` alike.

    Java's `String.trim` takes off only what is at or below a space, so U+0085 or U+00A0 beside a
    type or a parameter is not a token: U+00A0 in front of `application/json` or after
    `application/json;q=0.5` was a 406 on `serverInfo` and `space`, 2026-10-16. A charset is one
    Java's registry names (:func:`_java_charset`). Confluence's Spring services look up a `charset`
    named so in that case only, the last value of it as sent (`charset=bogus;charset=utf-8` is read,
    and `charset=bogus;Charset=utf-8` is not); Jira, ``charset_in_any_case``, looks up the `charset`
    kept, in any case (`Charset=bogus;charset=utf-8` and `charset=bogus;Charset=utf-8` are read,
    `charset=utf-8;Charset=bogus` is not), and reads a charset's name only as a token or quoted
    (`charset=ISO_8859-1:1987`, a name Java gives ISO-8859-1, is not read and
    `charset="ISO_8859-1:1987"` is), 2026-10-16. A `q` is checked where it is kept as `q` and
    must be one :func:`_spring_quality` reads: `q=2;q=0.5` is read and `q=0.5;q=2` is not
    (2026-10-13), and `q=Infinity;Q=0.5` is read, where the `Q` is what is kept (both products,
    2026-10-16). ``check_q`` False leaves that `q` unchecked, for :func:`_spring_quality_reason`.
    """
    full, _, rest = value.partition(";")
    full = full.strip(_JAVA_TRIMMED)
    if full == "*":
        full = "*/*"
    kind, _, subtype = full.partition("/")
    if not (_TOKEN.fullmatch(kind) and _TOKEN.fullmatch(subtype)) or (kind == "*" != subtype):
        return None
    listed = []
    for parameter in _split_parameters(rest):
        name, eq, given = parameter.strip(_JAVA_TRIMMED).partition("=")
        if eq:
            listed.append((name.strip(_JAVA_TRIMMED), given.strip(_JAVA_TRIMMED)))
    as_sent = dict(listed)
    for name, given in as_sent.items():
        is_quoted = len(given) >= 2 and given[0] == given[-1] and given[0] in "\"'"
        if not (_TOKEN.fullmatch(name) and given):
            return None
        spring_charset = name == "charset" and not charset_in_any_case
        if not (is_quoted or spring_charset or _TOKEN.fullmatch(given)):
            return None
        if name == "charset" and not charset_in_any_case and not _java_charset(_unquoted(given)):
            return None
    parameters = _spring_parameters(listed)
    for name, given in parameters:
        if check_q and name == "q" and not _spring_quality(given):
            return None
        is_charset = charset_in_any_case and name.lower() == "charset"
        if is_charset and not _java_charset(_unquoted(given)):
            return None
    return kind.lower(), subtype.lower(), parameters


def _parse_media_type(value: str, *, charset_in_any_case: bool = True) -> str | None:
    """``value`` as Spring reads it (:func:`_spring_media_type`), `type/subtype;name=value` with
    the parameters it keeps, or None where it cannot read it: `application/xml;x=1;X=2` is
    `application/xml;X=2` in the 415 of each product, 2026-10-16."""
    parsed = _spring_media_type(value, charset_in_any_case=charset_in_any_case)
    if parsed is None:
        return None
    kind, subtype, parameters = parsed
    return f"{kind}/{subtype}" + "".join(f";{name}={given}" for name, given in parameters)


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
    path: str,
    headers,
    consumes: tuple[str, ...],
    *,
    body_optional: bool = False,
    charset: str | None = None,
) -> AtlassianError | None:
    """Jira's 415 for a request whose `Content-Type` an operation that ``consumes`` those types does
    not take, or None where it takes it.

    Spring's check, and the first thing the operation does: ahead of the 401 a caller with no
    credential gets (:data:`JIRA_UNAUTHENTICATED`) and of the operation itself for one whose
    credential resolves, and behind the gateway's Connect-token 403. Measured on Jira Cloud on
    2026-09-30, on the operations ``backlot/data/jira_unserved.json`` listed that day:

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
      none of it, though an unreadable one leaves the body out (:func:`rendered`)
    - a path carrying a bracket is Tomcat's 415 page instead (:func:`_jira_tomcat_refusal`)

    Where the operation's entry names a ``charset``, ``detail`` adds `;charset=<it>` to a type read
    without one, as `DOCUMENTED_OTHERWISE` in ``scripts/gen_jira_unserved.py`` records.

    A charset is one Java's registry names (:func:`_java_charset`), whatever the case of the
    parameter's name.
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
        if media_type is None:
            detail = "Could not parse Content-Type."
        else:
            names = [p.split("=", 1)[0].lower() for p in media_type.split(";")[1:]]
            if charset is not None and "charset" not in names:
                media_type = f"{media_type};charset={charset}"
            detail = f"Content-Type '{media_type}' is not supported."
    if _JIRA_BRACKET.search(path):
        return _jira_tomcat_refusal(415, {"Accept": ", ".join(consumes)})
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


#: A name Java's `Charset.forName` looks up: a letter or digit, then letters, digits and `-+:_.`.
#: Any other name is its `IllegalCharsetNameException`, whose message is the name alone.
_JAVA_CHARSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9+:_.-]*")


def _invalid_mime_type(value: str) -> str | None:
    """Spring's ``Invalid mime type "<value>": <reason>`` for a `Content-Type` its
    `MimeTypeUtils.parseMimeType` cannot read, or None for one it reads. The checks run in the order
    Spring 5.3's parser and the `MimeType` it builds run them: the type before the first `;`
    trimmed, then `'mimeType' must not be empty`, `does not contain '/'`,
    `does not contain subtype after '/'`, `wildcard type is legal only in '*/*' (all mime types)`
    and `'type' must not be empty`; the first character of the type, then of the subtype, that is no
    token character; then each parameter with a `=` in the order first named, with the last value
    given it: `'parameter' must not be empty`, `'value' must not be empty`, a name's first character
    that is no token character, and for `charset` the name itself where Java takes no charset by
    that name and `unsupported charset '<name>'` where it knows none, its quotes off, or for another
    parameter its value's first character that is no token character unless the value is quoted.
    Measured on Confluence Cloud on 2026-10-16 and 2026-10-18 with each of those, at
    `POST /wiki/rest/api/space/{id}/permission` with no credential: `;x`, `foo`, `text/`, `*/json`,
    `/plain`, `t ext/plain`, `text//plain`, `text/plain;=x`, `text/plain;a=`, `text/plain;a b=c`,
    `text/plain;a=b c`, `text/plain;a="b`, `text/plain;charset=a b` (named `a b`) and
    `text/plain;charset=nope`, and `text/plain;a b=c;charset=nope` named the space in `a b` first.
    On 2026-10-19 a no-break space (U+00A0) at either end of the header stayed part of it:
    `text/plain` and `text/plain;charset=nope` followed by one were named for it. Not read here: a
    charset Tomcat reads that Java takes no name for, the 500 :func:`refuse_a_confluence_media_type`
    gives first.
    """
    index = value.find(";")
    full = (value if index < 0 else value[:index]).strip(_JAVA_TRIMMED)
    if full == "*":
        full = "*/*"
    kind, slash, subtype = full.partition("/")
    reason = None
    if not full:
        reason = "'mimeType' must not be empty"
    elif not slash:
        reason = "does not contain '/'"
    elif not subtype:
        reason = "does not contain subtype after '/'"
    elif kind == "*" and subtype != "*":
        reason = "wildcard type is legal only in '*/*' (all mime types)"
    elif not kind:
        reason = "'type' must not be empty"
    else:
        parameters: dict[str, str] = {}
        for parameter in _split_parameters(value[index + 1 :]) if index >= 0 else ():
            name, eq, given = parameter.strip(_JAVA_TRIMMED).partition("=")
            if eq:
                parameters[name.strip(_JAVA_TRIMMED)] = given.strip(_JAVA_TRIMMED)
        reason = _token_reason(kind) or _token_reason(subtype)
        for name, given in parameters.items():
            reason = reason or _parameter_reason(name, given)
    return None if reason is None else f'Invalid mime type "{value}": {reason}'


def _parameter_reason(name: str, given: str) -> str | None:
    """Spring's reason for one parameter of the type :func:`_invalid_mime_type` reads, or None."""
    if not name:
        return "'parameter' must not be empty"
    if not given:
        return "'value' must not be empty"
    if (reason := _token_reason(name)) is not None or name != "charset":
        return reason or (_token_reason(given) if _unquoted(given) == given else None)
    charset = _unquoted(given)
    if _JAVA_CHARSET_NAME.fullmatch(charset) is None:
        return charset
    return None if _java_charset(charset) else f"unsupported charset '{charset}'"


def _token_reason(token: str) -> str | None:
    """Spring's reason for the first character of ``token`` that is no token character, or None."""
    for ch in token:
        if _TOKEN.fullmatch(ch) is None:
            return f"Invalid token character '{ch}' in token \"{token}\""
    return None


#: Tomcat's `HttpParser`: a token, and a quoted string, each after any spaces and tabs.
_TOMCAT_TOKEN = re.compile(r"[ \t]*([!#$%&'*+.^_`|~0-9A-Za-z-]*)")
_TOMCAT_QUOTED = re.compile(r'[ \t]*"((?:[^"\\]|\\.)*)"', re.DOTALL)


def _tomcat_charset(value: str) -> str | None:
    """The `charset` Tomcat reads off a `Content-Type` ahead of Spring, its quotes and escapes off,
    or None where it reads none: Tomcat's `MediaType.parseMediaType` (`9.0.98` and `10.1.34` alike)
    takes a type and a subtype that are tokens either side of a `/`, then parameters each after a
    `;`, with spaces and tabs around each, and nothing else, so `foo;charset=` and
    `text/plain;a b=c;charset=` hold none. A name is read in any case, a later one replacing an
    earlier one. A name with no `=` has an empty value, and so has one whose `=` ends the header,
    where one whose `=` meets a `;` has none (`charset=;x=y`)."""
    pos = 0

    def token() -> str | None:
        # `readToken`: the token, empty at the end of the header, None ahead of anything else
        nonlocal pos
        match = _TOMCAT_TOKEN.match(value, pos)
        pos = match.end()
        return match[1] if match[1] or pos == len(value) else None

    def skip(constant: str) -> bool | None:
        # `skipConstant`: whether ``constant`` comes next, None at the end of the header
        nonlocal pos
        while pos < len(value) and value[pos] in " \t":
            pos += 1
        if pos == len(value):
            return None
        next_is_it = value[pos] == constant
        pos += next_is_it
        return next_is_it

    if not token() or skip("/") is not True or not token():
        return None
    charset = None
    found = skip(";")
    while found:
        name, given = token(), ""
        if skip("="):
            quoted = _TOMCAT_QUOTED.match(value, pos)
            if quoted:
                given, pos = re.sub(r"\\(.)", r"\1", quoted[1], flags=re.DOTALL), quoted.end()
            elif value[pos:].lstrip(" \t").startswith('"'):
                given, pos = None, len(value)  # a quote left open reads to the end
            else:
                given = token()
        if name is not None and name.lower() == "charset":
            charset = given
        found = skip(";")
    return None if found is False else charset


def refuse_a_confluence_media_type(headers, consumes: tuple[str, ...]) -> AtlassianError | None:
    """Confluence's 415 for a request whose `Content-Type` an operation that ``consumes`` those
    types does not take, or None where it takes it.

    Spring's `errors` list, the shape of :func:`method_not_allowed`'s Confluence 405, with no
    `Accept` beside it; Tomcat's page for it names ``consumes`` in one (:func:`confluence_refusal`,
    measured 2026-10-13). Measured 2026-10-12 on the attachment writes and
    `POST space/{key}/permission`: an absent or empty `Content-Type` is named `''`, a type is named
    as :func:`_parse_media_type` reads it (`APPLICATION/JSON` as `application/json`,
    `TEXT/Plain;Charset=UTF-8;X=Y` with its parameters kept, `*` as `*/*`). A value Spring's parser
    cannot read is the sentence :func:`_invalid_mime_type` gives, and one whose `q` is then all it
    cannot read is `Invalid media type "<value>": ` and the reason :func:`_spring_quality_reason`
    gives. Each quotes the header without the spaces and tabs around it (` ;x` is named `";x"`,
    2026-10-18). The status and the code are the same. Only a parameter named `charset` in that
    case is a charset to it (`multipart/form-data; boundary=x; Charset=bogus` is read, 2026-10-13).

    Ahead of all of it, a charset Tomcat reads (:func:`_tomcat_charset`) that is no name Java takes
    (:data:`_JAVA_CHARSET_NAME`), an empty one among them, is a 500 with no body and no
    `content-type`, whether the operation takes the type or not. Measured on Confluence Cloud at
    `POST space/{key}/permission` with no credential, `text/plain;charset=` on 2026-10-18 and
    2026-10-19, and on 2026-10-19 `application/json;charset=`, `text/plain;Charset=`,
    `text/plain;charset`, `text/plain;charset=""`, `text/plain;charset=_x` and
    `text/plain;charset=utf-8;charset=` with it, where `text/plain;charset=;x=y` and `foo;charset=`
    were each the 415.
    """
    value = (headers.get("content-type") or "").strip(" \t")
    charset = _tomcat_charset(value)
    if charset is not None and _JAVA_CHARSET_NAME.fullmatch(charset) is None:
        return AtlassianError(500, b"")
    media_type = _parse_media_type(value, charset_in_any_case=False) if value else None
    if media_type is not None and _takes(consumes, media_type):
        return None
    mime = _invalid_mime_type(value) if value else None
    quality = _spring_quality_reason(value)
    if mime is not None:
        reason = mime
    elif quality is not None:
        reason = f'Invalid media type "{value}": {quality}'
    else:
        reason = f"Content type '{media_type or value}' not supported"
    return AtlassianError(
        415,
        {
            "errors": [
                {
                    "status": 415,
                    "code": "UNSUPPORTED_MEDIA_TYPE",
                    "title": f"org.springframework.web.HttpMediaTypeNotSupportedException: {reason}",
                }
            ]
        },
        headers={"Accept": ", ".join(consumes)},
    )


#: The one `Accept` holding a member Jira cannot read that it reads all the same, as
#: `application/json`, byte for byte but for trailing whitespace: measured 2026-10-13 on
#: `serverInfo` and its 401, where the same value respelled (upper-cased, `q=0.90`, `Q=0.9`, a space
#: after a separator, its members reordered, `text/plain` in front) was unreadable, and so was it
#: with U+00A0 in front of it or after it or U+0085 after it (2026-10-16).
_JIRA_READS_AS_JSON = "application/json,*.*;q=0.9"
#: What Java's `Double.parseDouble` reads once it has trimmed the text: a decimal or hexadecimal
#: floating literal, `NaN` or `Infinity`, signed or not, with an optional `f`, `F`, `d` or `D`.
_JAVA_DOUBLE = re.compile(
    r"[+-]?(?:NaN|Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?[fFdD]?"
    r"|0[xX](?:[\da-fA-F]+\.?[\da-fA-F]*|\.[\da-fA-F]+)[pP][+-]?\d+[fFdD]?)"
)
#: The characters `String.trim` takes off either end, which `Double.parseDouble` does first.
_JAVA_TRIMMED = "".join(chr(code) for code in range(0x21))


def _java_double(text: str) -> float | None:
    """``text`` as Java's `Double.parseDouble` reads it, or None where it throws; the hexadecimal
    `0x1p-1` is 0.5."""
    text = text.strip(_JAVA_TRIMMED)
    if not _JAVA_DOUBLE.fullmatch(text):
        return None
    sign = -1.0 if text.startswith("-") else 1.0
    body = text.lstrip("+-").rstrip("fFdD")
    return sign * (float.fromhex(body) if body[:2] in ("0x", "0X") else float(body))


def _java_number_format(text: str) -> str:
    """The message of the `NumberFormatException` `Double.parseDouble` throws for ``text``, one
    :func:`_java_double` does not read: `empty String` for nothing once trimmed, `multiple points`
    for a second `.` in the digits and points that open it after an optional sign, and
    `For input string: "<text trimmed>"` otherwise. Confluence's 415 gave each, `"abc "` trimmed to
    `abc`, on 2026-10-16; OpenJDK 26's message keeps those spaces."""
    text = text.strip(_JAVA_TRIMMED)
    if not text:
        return "empty String"
    unsigned = text[1:] if text[0] in "+-" else text
    if re.match(r"[0-9.]*", unsigned).group().count(".") > 1:
        return "multiple points"
    return f'For input string: "{text}"'


def _unquoted(value: str) -> str:
    """A parameter's value with the quotes around it removed, as Spring reads it."""
    quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
    return value[1:-1] if quoted else value


def _spring_quality(value: str) -> bool:
    """Whether Spring reads ``value`` as a `q`, a :func:`_java_double` between 0 and 1: `0.5`, `.5`,
    `1e0`, `0.5f` and a quoted `"0.5"` it does, and `2`, `-1`, `1.0001`, `NaN` and `abc` it does
    not, measured 2026-10-13 on Jira and on Confluence's Spring services; `0x1p-1` it does, on Jira
    on 2026-10-14."""
    quality = _java_double(_unquoted(value.strip()))
    return quality is not None and 0 <= quality <= 1


def _spring_quality_reason(value: str) -> str | None:
    """Why Spring 5.3's `MediaType.parseMediaType` refuses ``value`` where the `q` it keeps is all
    it cannot read, or None where something else is: the `q` with its quotes off, then
    `Invalid quality value "<q>": should be between 0.0 and 1.0` for a number outside 0 to 1, and
    the message :func:`_java_number_format` gives for one Java cannot read. Measured on
    Confluence's 415 on 2026-10-16: `q=2`, `q="2"` (as `2`), `q=" 2"` (as ` 2`), `q=NaN`,
    `q=0x1.8p0` and `Q=0.5;q=2` are the first; `q=abc`, `q=1.2.3` and `q=""` the second. A
    `charset` Confluence reads or leaves alone keeps the `q` the reason (`charset=ISO_8859-1:1987`,
    `Charset=bogus`). This is None for an empty `q=` and for an unknown `charset` beside a bad `q`,
    where real's reason is the `Invalid mime type` one the empty value or the charset draws."""
    parsed = _spring_media_type(value, charset_in_any_case=False, check_q=False)
    if parsed is None:
        return None
    kept = [given for name, given in parsed[2] if name == "q"]
    if not kept or _spring_quality(kept[0]):
        return None
    quality = _unquoted(kept[0])
    if _java_double(quality) is None:
        return _java_number_format(quality)
    return f'Invalid quality value "{quality}": should be between 0.0 and 1.0'


#: The characters Java's `Character.isWhitespace` is true of in a header's range: a member made of
#: them alone is blank to Spring (`StringUtils.hasText`), where U+0085 and U+00A0 are not.
_JAVA_WHITESPACE = "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f "


def _spring_members(accept: str) -> list[str]:
    """``accept``'s members as Spring's `MimeTypeUtils.tokenize` splits them, at each comma outside
    double quotes, a backslash taking the character after it, with a blank one dropped."""
    members, start, quoted, i = [], 0, False, 0
    while i < len(accept):
        if accept[i] == '"':
            quoted = not quoted
        elif accept[i] == "," and not quoted:
            members.append(accept[start:i])
            start = i + 1
        elif accept[i] == "\\":
            i += 1
        i += 1
    members.append(accept[start:])
    return [member for member in members if member.strip(_JAVA_WHITESPACE)]


def _accept(
    accept: str | None, *, reader: str = "jira"
) -> tuple[list[tuple[str, str, list[tuple[str, str]]]], bool]:
    """The media ranges ``accept`` lists, each `(type, subtype, parameters)` as Jira or, where
    ``reader`` is `spring`, Confluence's Spring services read one (:func:`_spring_media_type`), and
    whether a member they cannot read was among them.

    Members are split at commas outside double quotes (:func:`_spring_members`), so
    `application/json;x="a,b", text/plain` is two (2026-10-16), and a blank one is dropped, so an
    empty header lists nothing, as an absent one does: the two answered alike on every route
    measured 2026-10-13, and a blank member beside others (`,application/json`,
    `application/json,,`) was passed over on Jira and on Confluence's Spring services. A `q` named
    in another case (`Q=2`) is not checked here; it is read as the members are sorted
    (:func:`spring_cannot_sort`) and checked as an answer is written
    (:func:`quality_spring_cannot_copy`). Jira cannot read a header of more than fifty members, its
    sort's limit, where Confluence's Spring services read 51 (2026-10-14).
    """
    members = _spring_members(accept or "")
    ranges, unread = [], reader == "jira" and len(members) > _JIRA_MOST_MEMBERS
    for member in members:
        parsed = _spring_media_type(member, charset_in_any_case=reader == "jira")
        if parsed is None:
            unread = True
            continue
        ranges.append(parsed)
    return ranges, unread


#: The most members a Jira `Accept` may hold (:func:`_accept`).
_JIRA_MOST_MEMBERS = 50


class _UnreadQuality(Exception):
    """A `q` :func:`_java_double` cannot read, met as Spring sorts an `Accept`'s members."""


def _sort_quality(parameters: list[tuple[str, str]]) -> float:
    """A member's `q` as Spring's `MediaType.getQualityValue` reads it as it sorts: the one kept
    (:func:`_spring_parameters`) in any case, unquoted, 1 where there is none."""
    value = next((v for n, v in parameters if n.lower() == "q"), None)
    if value is None:
        return 1.0
    quality = _java_double(_unquoted(value))
    if quality is None:
        raise _UnreadQuality
    return quality


def _double_bits(value: float) -> int:
    """Java's `Double.doubleToLongBits`: one bit pattern for every NaN."""
    return (
        0x7FF8000000000000
        if math.isnan(value)
        else struct.unpack(">q", struct.pack(">d", value))[0]
    )


def _double_compare(d1: float, d2: float) -> int:
    """Java's `Double.compare`: NaN above every value and equal to itself, `-0.0` below `0.0`."""
    if d1 < d2:
        return -1
    if d1 > d2:
        return 1
    bits1, bits2 = _double_bits(d1), _double_bits(d2)
    return (bits1 > bits2) - (bits1 < bits2)


def _sorted_alike(m1, m2) -> int:
    """Spring 5's `MediaType.SPECIFICITY_COMPARATOR` then `QUALITY_VALUE_COMPARATOR` on two
    `(type, subtype, parameters)`: the wildcard type, then the wildcard subtype (`*` or `*+…`),
    and only between two of the same type and subtype the `q` and then how many parameters each
    keeps; a tie there, or two types or subtypes that differ, is decided by `q` alone, each `q`
    compared as `Double.compare` compares them (:func:`_double_compare`)."""

    def wild(member, index):
        return member[index] == "*" or (index == 1 and member[1].startswith("*+"))

    for index in (0, 1):
        if wild(m1, index) != wild(m2, index):
            return 1 if wild(m1, index) else -1
        if m1[index] != m2[index]:
            break
    else:
        by_quality = _double_compare(_sort_quality(m2[2]), _sort_quality(m1[2]))
        if by_quality:
            return by_quality
        return (len(m2[2]) > len(m1[2])) - (len(m2[2]) < len(m1[2]))
    return _double_compare(_sort_quality(m2[2]), _sort_quality(m1[2]))


def spring_cannot_sort(accept: str | None, *, reader: str = "jira") -> bool:
    """Whether Spring fails as it sorts ``accept``'s members, before it picks a handler: a `q` kept
    in another case (`Q=abc`) that :func:`_java_double` cannot read, on a member the sort compares
    by `q`, in a header of two members or more it can read; or, on Confluence's Spring services, a
    comparison that breaks the sort (:class:`backlot.java_timsort.ContractViolation`).

    Which sort runs is read off the answers; Atlassian does not say. Jira's are the sort
    spring-framework runs at `v6.1.14`, which compares every member by `q`; Confluence's Spring
    services' the one it runs at `v5.3.39`, :func:`_sorted_alike` under
    :func:`backlot.java_timsort.java_list_sort`. Measured 2026-10-14 with fourteen headers on each
    of `serverInfo` and `space` and nine on Jira's `OPTIONS`, and 2026-10-16 with ten more on
    `space`. What each answer then becomes is ``backlot.routers.atlassian.spring_sort_failure``.
    """
    ranges, unread = _accept(accept, reader=reader)
    if unread or len(ranges) < 2:
        return False
    try:
        if reader == "jira":
            for _, _, parameters in ranges:
                _sort_quality(parameters)
        else:
            java_timsort.java_list_sort(ranges, _sorted_alike)
    except (_UnreadQuality, java_timsort.ContractViolation):
        return True
    return False


#: A `q` Confluence's JAX-RS services read, Jersey's: `0` or `1`, then a `.` and digits, or the
#: digits after a bare `.`, five characters at most and no more than 1.
_JERSEY_QUALITY = re.compile(r"[01](?:\.\d*)?|\.\d+")


def _jersey_quality(value: str) -> bool:
    return (
        len(value) <= 5
        and _JERSEY_QUALITY.fullmatch(value) is not None
        and not (value[0] == "1" and any(ch != "0" for ch in value[2:]))
    )


def _jaxrs_member(member: str) -> tuple[str, str, list[tuple[str, str]]] | None:
    """One member of an `Accept` as :func:`jersey_media_type` reads it, `*` as `*/*`."""
    head, sep, rest = member.partition(";")
    return jersey_media_type(("*/*" if head.strip(" \t") == "*" else head) + sep + rest)


def _jaxrs_members(accept: str) -> list[str]:
    """``accept``'s members as Jersey reads them, split at each comma outside a quoted string, in
    which a backslash takes the character after it."""
    members, start, quoted, i = [], 0, False, 0
    while i < len(accept):
        if accept[i] == '"':
            quoted = not quoted
        elif accept[i] == "\\" and quoted:
            i += 1
        elif accept[i] == "," and not quoted:
            members.append(accept[start:i])
            start = i + 1
        i += 1
    members.append(accept[start:])
    return members


def _jaxrs_grammar(member: str) -> bool:
    """Whether ``member`` is one Jersey's grammar takes, its `q` aside: a type and subtype, `*`, or
    a token it passes over, then its parameters (:func:`_jaxrs_accept`)."""
    head, _, rest = member.strip(" \t").partition(";")
    if head.strip(" \t") != "*" and "/" not in head:
        return bool(_JERSEY_TOKEN.fullmatch(head.strip(" \t"))) and (
            _jersey_parameters(";" + rest, 0) is not None
        )
    return _jaxrs_member(member.strip(" \t")) is not None


def jaxrs_out_of_bounds(accept: str | None) -> int | None:
    """The character Jersey fails on as it reads ``accept``, or None: the first one past ASCII
    outside a quoted string, where nothing it read before it was already wrong.

    Jersey throws as it reaches such a character, naming its code against a length of 128. U+00A0 in
    front of `application/json`, after `application/json;x=1` or `application/json;foo`, or as a
    member after `foo` was a 500 naming its code (`Index 160 out of bounds for length 128`), where
    inside a quoted value it was read, and after `application/json;foo, ` or
    `application/json;q=2, ` it drew the 400 of the member before it; measured 2026-10-16 on
    `search` with a credential and without one, on its `OPTIONS`, on `audit` and on `POST template`.
    """
    members = _jaxrs_members(accept or "")
    for index, member in enumerate(members):
        if not member.strip(" \t") and index < len(members) - 1:
            return None
        quoted, i, at = False, 0, None
        while i < len(member):
            if member[i] == '"':
                quoted = not quoted
            elif member[i] == "\\" and quoted:
                i += 1
            elif not quoted and ord(member[i]) > 127:
                at = i
                break
            i += 1
        if at is None:
            if _jaxrs_accept(member)[2]:
                return None
            continue
        before = member[:at]
        if not before.strip(" \t") or any(
            _jaxrs_grammar(before + rest) for rest in ("x", "/x", "x/x", "=x", "x=x", ";x=x")
        ):
            return ord(member[at])
        return None
    return None


def jaxrs_out_of_bounds_refusal(accept: str | None) -> AtlassianError | None:
    """Jersey's 500 for an `Accept` it fails on (:func:`jaxrs_out_of_bounds`), or None: a
    `statusCode` and `message` naming the character's code, under `application/json`."""
    code = jaxrs_out_of_bounds(accept)
    if code is None:
        return None
    message = f"java.lang.ArrayIndexOutOfBoundsException: Index {code} out of bounds for length 128"
    body = json.dumps({"statusCode": 500, "message": message}, separators=(",", ":"))
    return AtlassianError(500, body.encode(), headers={"Content-Type": "application/json"})


def _jaxrs_accept(accept: str | None) -> tuple[list[tuple[str, str, float]], bool, bool]:
    """``accept`` as Confluence's JAX-RS services read it: the `(type, subtype, q)` each member
    names, whether a member with no `/` was passed over, and whether the header is one they cannot
    parse.

    Measured 2026-10-13 on `search` with and without a credential: a member with no `/` (`foo`,
    `*.*`) is passed over where it is a token, its parameters read all the same (`foo;x` and
    `"0.5"` cannot be parsed), and `*` is `*/*`; a blank member anywhere but last
    (`,application/json`, `application/json,,`, `application/json, ,text/html`), a member
    :func:`jersey_media_type` cannot read (`application/json;foo`, `;q= 0.5`) and a `q` Jersey does
    not read (`q=2`, `q=1e0`, `q=0.5f`, `q=NaN`, `Q=2`) make a header they cannot parse. A `q` is
    unquoted and the last of several is the one read (`q="0.5"` and `q=2;q=0.5` are read); a
    `charset` is not read at all.
    """
    members = _jaxrs_members(accept or "")
    ranges, passed_over = [], False
    for index, member in enumerate(members):
        member = member.strip(" \t")
        if not member:
            if index < len(members) - 1:
                return [], passed_over, True
            continue
        head, _, rest = member.partition(";")
        if head.strip(" \t") != "*" and "/" not in head:
            if (
                not _JERSEY_TOKEN.fullmatch(head.strip(" \t"))
                or _jersey_parameters(";" + rest, 0) is None
            ):
                return [], passed_over, True
            passed_over = True
            continue
        parsed = _jaxrs_member(member)
        quality = next((v for n, v in reversed(parsed[2]) if n == "q"), "1") if parsed else ""
        if parsed is None or not _jersey_quality(quality):
            return [], passed_over, True
        ranges.append((parsed[0], parsed[1], float(quality)))
    return ranges, passed_over, False


def jaxrs_cannot_parse(accept: str | None) -> bool:
    """Whether Confluence's JAX-RS services cannot parse ``accept`` (:func:`_jaxrs_accept`)."""
    return _jaxrs_accept(accept)[2]


def jaxrs_picks_json(accept: str | None) -> bool:
    """Whether a Confluence JAX-RS 404 (:func:`jaxrs_not_found`) is JSON rather than XML.

    Jersey orders the members by `q`, highest first and `q=0` among them, then by how specific
    each is (`application/json` ahead of `application/*`, and that ahead of `*/*`), then as listed,
    and the first that XML or JSON matches picks it, XML where both do; nothing matching is XML.
    Measured 2026-10-13 on `/wiki/rest/api/nopesuchroute` with 21 values: `application/json;q=0`,
    `application/xml;q=0, application/json`, `text/xml, application/json` and
    `application/*, application/json` are JSON, `application/json;q=0.5, application/xml`,
    `APPLICATION/XML, application/json`, `application/jsonx`, `*/*` and `application/*` are XML.
    """
    candidates = []
    for index, member in enumerate(_jaxrs_members(accept or "")):
        parsed = _jaxrs_member(member.strip(" \t"))
        if parsed is None:
            continue
        kind, subtype, parameters = parsed
        quality = next((v for n, v in reversed(parameters) if n == "q"), "1")
        if not _jersey_quality(quality):
            continue
        rank = 2 if kind == "*" else 1 if subtype == "*" else 0
        candidates.append((-float(quality), rank, index, kind, subtype))
    for *_, kind, subtype in sorted(candidates):
        if kind == "*" or (kind == "application" and subtype in ("*", "xml")):
            return False
        if (kind, subtype) == ("application", "json"):
            return True
    return False


#: What every route of either product answers in, and so what Backlot's 406 names unless an entry's
#: ``produces`` in ``scripts/gen_jira_unserved.py`` names more.
JSON_ONLY = ("application/json",)


def accepts(
    accept: str | None, *, reader: str, produces: tuple[str, ...] = JSON_ONLY
) -> bool | None:
    """Whether ``accept`` admits one of the types an endpoint ``produces``, or None where the
    service cannot read it; ``reader`` is `jira`, `spring` (Confluence's Spring services) or
    `jaxrs` (its JAX-RS ones).

    Measured 2026-10-13 on `serverInfo`, `myself`, an issue, `POST search/jql`, `space`,
    `content/{id}` and `search`: no `Accept`, an empty one, `*`, `*/*`, `application/*` and
    `application/json` in any case and under any `q` (`q=0` included) admit JSON, and so does a
    list holding any of them (`text/html, application/json`, a browser's
    `text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8`), where `text/html`,
    `text/plain`, `text/*`, `text/xml`, `application/xml`, `application/problem+json`,
    `application/hal+json`, `image/*` and `foo/bar` do not; a ``produces`` naming `text/plain` as
    well is admitted the same way (`PRODUCES` in ``scripts/gen_jira_unserved.py``).
    `application/*+json` admits JSON on Jira and the Spring services and not on the JAX-RS ones,
    and `*/json` and `*/*+json` the other way round.

    None is a header the service cannot read: on Jira and the Spring services, one holding a member
    :func:`_accept` cannot read (`foo`, `*.*` and `*/json` among them), wherever it stands, but for
    :data:`_JIRA_READS_AS_JSON`; on the JAX-RS services, one whose every member
    :func:`_jaxrs_accept` passes over. A JAX-RS header that cannot be parsed is the 400
    :func:`not_acceptable` answers before asking this.
    """
    if reader == "jaxrs":
        found, passed_over, _ = _jaxrs_accept(accept)
        if passed_over and not found:
            return None
        ranges = [(kind, subtype, []) for kind, subtype, _ in found]
    else:
        if reader == "jira" and (accept or "").strip(" \t") == _JIRA_READS_AS_JSON:
            accept = "application/json"
        ranges, unread = _accept(accept, reader=reader)
        if unread:
            return None
    if not ranges:
        return True
    for produced in produces:
        produced_kind, produced_subtype = produced.split(";", 1)[0].split("/")
        suffixed = () if reader == "jaxrs" else (f"*+{produced_subtype}",)
        for kind, subtype, _ in ranges:
            if kind == "*" or (
                kind == produced_kind and subtype in ("*", produced_subtype, *suffixed)
            ):
                return True
    return False


def quality_spring_cannot_copy(accept: str | None, *, reader: str = "jira") -> bool:
    """Whether a Spring service fails to write its answer under ``accept``: a member JSON matches
    names its `q` in another case (`Q=2`), the last value of it, in any case, being one
    :func:`_spring_quality` does not read. Spring checks a `q` so named as it copies the member's
    onto the type it writes, and fails there; measured 2026-10-13 with `Q=2`, `Q=abc`, `Q=1.5`,
    `*/*;Q=2`, `application/*;Q=2` and `q=0.5;Q=2`, where `Q=0.5;q=0.5` and
    `text/html;Q=2, application/json` were written. A `q` it cannot read as a number fails sooner
    where it sorts two members or more by it (:func:`spring_cannot_sort`)."""
    ranges, unread = _accept(accept, reader=reader)
    if unread:
        return False
    for kind, subtype, parameters in ranges:
        if kind == "*" or (kind == "application" and subtype in ("*", "json", "*+json")):
            values = [value for name, value in parameters if name.lower() == "q"]
            if values and not _spring_quality(values[-1]):
                return True
    return False


def _json_family(accept: str | None) -> str | None:
    """The first type ``accept`` names whose subtype ends `+json`, which a Spring service writes
    its JSON refusal under where nothing in ``accept`` admits plain JSON
    (:func:`confluence_refusal`)."""
    ranges, _ = _accept(accept, reader="spring")
    return next((f"{k}/{s}" for k, s, _ in ranges if s.endswith("+json")), None)


#: The 406 a Confluence Spring service answers in its `errors` list, under a `+json` type `Accept`
#: names; Tomcat's page is the other shape (:func:`confluence_refusal`).
CONFLUENCE_NOT_ACCEPTABLE = (
    "org.springframework.web.HttpMediaTypeNotAcceptableException: "
    "Could not find acceptable representation"
)


def not_acceptable(
    path: str, accept: str | None, *, jaxrs: bool = False, produces: tuple[str, ...] = JSON_ONLY
) -> AtlassianError | None:
    """The 406 an endpoint at ``path`` answers where ``accept`` admits none of the types it
    ``produces`` (:func:`accepts`), or None.

    Real asks once the request has reached an endpoint, so a 405 for a method the path does not
    take comes first (2026-10-13); where it falls among the endpoint's own refusals is
    ``backlot.routers.atlassian``'s to say.

    - Jira: RFC 7807 under :data:`PROBLEM_JSON`, ``detail`` naming ``produces``
      (`Acceptable representations: [application/json].`) and `Accept` naming them beside it, the
      request's path in ``instance``; on a path carrying a bracket, Tomcat's 406 page instead
      (:func:`_jira_tomcat_refusal`)
    - Confluence's Spring services: the `errors` list :func:`confluence_refusal` renders, Tomcat's
      page for every value measured but a `+json` type
    - its JAX-RS services (``jaxrs``): no body under no `content-type`, and for a header they
      cannot parse (:func:`_jaxrs_accept`) a 400 saying so, under `text/plain`, in that place too
      (2026-10-13; a header naming `text/html` comes back with `text/html,` in front of it there,
      and one whose `q` beside `text/html` cannot be read is a JSON `Internal Server Error` 400,
      neither of which is copied)

    An unreadable `Accept` is refused as well, its body left out (:func:`rendered`).
    """
    if is_confluence(path):
        if jaxrs and jaxrs_cannot_parse(accept):
            out_of_bounds = jaxrs_out_of_bounds_refusal(accept)
            if out_of_bounds is not None:
                return out_of_bounds
            message = f'The HTTP header field "Accept" with value "{accept}" could not be parsed.'
            return AtlassianError(400, message.encode(), headers={"Content-Type": "text/plain"})
        if accepts(accept, reader="jaxrs" if jaxrs else "spring", produces=produces):
            return None
        if jaxrs:
            return AtlassianError(406, b"")
        return AtlassianError(
            406,
            {
                "errors": [
                    {"status": 406, "code": "NOT_ACCEPTABLE", "title": CONFLUENCE_NOT_ACCEPTABLE}
                ]
            },
        )
    if accepts(accept, reader="jira", produces=produces):
        return None
    named = ", ".join(produces)
    if _JIRA_BRACKET.search(path):
        return _jira_tomcat_refusal(406, {"Accept": named})
    return AtlassianError(
        406,
        {
            "type": "about:blank",
            "title": "Not Acceptable",
            "status": 406,
            "detail": f"Acceptable representations: [{named}].",
            "instance": _instance(path),
        },
        media_type=PROBLEM_JSON,
        headers={"Accept": named},
    )


#: The headers a Confluence refusal carries on Tomcat's page and in no other shape: the 405's
#: `Allow` and the 415's `Accept`, measured 2026-10-13 on each shape :func:`confluence_refusal`
#: names.
_PAGE_ONLY_HEADERS = ("allow", "accept")


def confluence_refusal(
    status_code: int, body: bytes, accept: str | None, headers: dict[str, str] | None = None
) -> Response:
    """A refusal a Confluence Spring service answers, in the shape ``accept`` asks for.

    ``body`` under `application/json` where ``accept`` admits JSON (:func:`accepts`, as a Spring
    service reads it), under the first `+json` type it names where it admits none but names one,
    Tomcat's page for the status (:func:`tomcat_page`) where it names neither, and no body under no
    `content-type` where it cannot be read. Measured 2026-10-13 with `application/json`,
    `text/html`, `application/problem+json` or `application/hal+json`, and `foo`, on the 405 (each
    served route under a method it does not take), the 415 (the attachment writes and
    `POST space/{key}/permission`), the 406 (:func:`not_acceptable`) and the anonymous 403 of
    `GET space/{key}/label`.
    """
    kept = {k: v for k, v in (headers or {}).items() if k.lower() not in _PAGE_ONLY_HEADERS}
    takes = accepts(accept, reader="spring")
    if takes is None:
        return Response(b"", status_code=status_code, headers=kept)
    media_type = "application/json" if takes else _json_family(accept)
    if media_type is not None:
        return Response(body, status_code=status_code, headers={**kept, "Content-Type": media_type})
    return Response(
        tomcat_page(status_code),
        status_code=status_code,
        headers={**(headers or {}), "Content-Type": TOMCAT_MEDIA_TYPE, "Content-Language": "en"},
    )


#: A character past the Basic Multilingual Plane, which Confluence's JSON writer reaches only
#: as a pair of UTF-16 surrogates (:func:`_confluence_json`).
_PAST_BMP = re.compile(r"[\U00010000-\U0010FFFF]")


def _confluence_json(body: dict) -> bytes:
    """The JSON bytes of a Confluence refusal or of the CQL search's answer, written the way real
    writes them: compact and unescaped, as ``JSONResponse`` writes them, but for a character past
    U+FFFF, which goes out as two ``\\u`` escapes, its UTF-16 surrogates in upper-case hex, where
    ``JSONResponse`` writes its UTF-8 bytes. Decoded, the two writings are one string, so the
    escapes are made in the dumped text, where such a character can only stand inside a string.

    Measured on Confluence Cloud 2026-10-21: `é` (U+00E9) named in a refused space key went out as
    its UTF-8 bytes on `space/{key}`, `content` and the label and conversion refusals, and U+1F600
    and U+1D7D3 as escapes, `No space found with key : \\uD83D\\uDE00` and
    `for value [\\uD835\\uDFD3]`; on 2026-10-22 `content?type=` with U+1F600 answered
    `Cannot find custom content type : \\uD83D\\uDE00`, and the CQL search's 200 wrote it in
    `cqlQuery` as `\\uD83D\\uDE00` for a `title~` and a `label=` query alike.
    """

    def escapes(match: re.Match[str]) -> str:
        past = ord(match.group()) - 0x10000
        return f"\\u{0xD800 + (past >> 10):04X}\\u{0xDC00 + (past & 0x3FF):04X}"

    written = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return _PAST_BMP.sub(escapes, written).encode()


def rendered(request, status_code: int, body, headers=None) -> Response | None:
    """The response for a refusal whose bytes are not its JSON, or None for the ``JSONResponse``
    around ``body`` every other one gets (``backlot.errors.rendered`` asks).

    A body of ``bytes`` goes out as it stands. A Jira RFC 7807 refusal is an empty
    `text/html;charset=UTF-8` answer, its `Allow` or `Accept` kept, where `Accept` holds a member
    Jira cannot read (:func:`accepts`): `foo` on a 405, the search's 415, the no-endpoint 404 and
    the 406, measured 2026-10-13, which are the ones such a header reaches. Confluence's `errors`
    list, on its 405, 406 and 415, is :func:`confluence_refusal`'s, and its other refusals are
    written by :func:`_confluence_json`, whose escapes past U+FFFF are real's own.
    """
    if isinstance(body, bytes):
        return Response(body, status_code=status_code, headers=headers)
    accept = request.headers.get("accept")
    # the decoded path picks the product only; no rendering here echoes it
    if is_confluence(request.url.path):
        if status_code in (405, 406, 415) and isinstance(body.get("errors"), list):
            encoded = json.dumps(body, separators=(",", ":")).encode()
            return confluence_refusal(status_code, encoded, accept, headers)
        return Response(
            _confluence_json(body),
            status_code=status_code,
            headers=headers,
            media_type="application/json",
        )
    if body.get("type") == "about:blank" and accepts(accept, reader="jira") is None:
        return Response(
            b"",
            status_code=status_code,
            headers={**(headers or {}), "Content-Type": HTML_MEDIA_TYPE},
        )
    return None


#: Jira's page for a 500 its web layer answers where writing an answer failed
#: (:func:`quality_spring_cannot_copy`), `text/html;charset=utf-8`, 1703 bytes: the same page on 23
#: answers measured 2026-10-13 but for a reference of its own to the failure and the response's
#: `atl-traceid` (:func:`jira_oops_page`).
_JIRA_OOPS_PAGE = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
    '<meta http-equiv="X-UA-Compatible" content="IE=edge"><title>'
    "Oops - an error has occurred</title><link type='text/css' rel='stylesheet' href='/static-ass"
    "ets/metal-all.css' media='all'><script src='/static-assets/metal-all.js'></script>"
    '<meta name="decorator" content="none" /></head><body class=" error-page error500">'
    '<script type="text/javascript">document.body.className += " js-enabled";</script>'
    '<div id="page"><header id="header" role="banner"></header><!-- #header -->'
    '<section id="content" role="main"><div class="aui-page-panel">'
    '<div class="aui-page-panel-inner"><section class="aui-page-panel-content lowerContent">'
    '<div id="error-state"><span class="error-type"></span><h1>Something went wrong</h1><p>'
    'Try reloading the page, then check our <a href="https://status.atlassian.com">Statuspage</a>'
    ' for any current outages. If there are no relevant outages, create a <a href="https://suppor'
    't.atlassian.com/contact/">support request</a> so we can help you out.</p><p>'
    "If you create a request, include the following so we can help you as fast as possible:</p>"
    "<ul><li>Error type: <strong>500 - Internal server error</strong></li><li>"
    "Log reference: <strong>{log}</strong></li><li>TraceId: <strong>{trace}</strong></li></ul>"
    "</div></section><!-- .aui-page-panel-content --></div><!-- .aui-page-panel-inner --></div>"
    '<!-- .aui-page-panel --></section><!-- #content --><footer id="footer" role="contentinfo">'
    '<section class="footer-body"><div id="footer-logo">'
    '<a href="http://www.atlassian.com/" rel="nofollow">Atlassian</a></div></section></footer>'
    "<!-- #footer --></div><!-- #page --></body></html>"
)
JIRA_OOPS_MEDIA_TYPE = "text/html;charset=utf-8"


def jira_oops_page(log_reference: str, trace_id: str) -> bytes:
    return _JIRA_OOPS_PAGE.replace("{log}", log_reference).replace("{trace}", trace_id).encode()


#: The `Allow` Jira's 401 to an `OPTIONS` carries where `Accept` holds a member it cannot read, and
#: the one its 500 to an `OPTIONS` carries where `Accept` names a wildcard it has no type for
#: (:func:`jira_unauthenticated`).
_JIRA_UNREAD_ALLOW = "GET, HEAD, POST, PUT, DELETE, OPTIONS, TRACE, PATCH"
_JIRA_NO_TYPE_ALLOW = "GET, HEAD, POST, PUT, DELETE, OPTIONS, TRACE"


def _quality(parameters: list[tuple[str, str]]) -> float:
    given = next((value for name, value in reversed(parameters) if name.lower() == "q"), "1")
    # a `Q` Spring cannot read sorts as any other (the answer is a 500 then,
    # ``backlot.routers.atlassian.spring_sort_failure`` or ``spring_write_failure``)
    return _java_double(_unquoted(given.strip())) if _spring_quality(given) else 1.0


def jira_unauthenticated(
    accept: str | None, method: str
) -> tuple[int, str, bytes | None, dict[str, str]]:
    """``(status, content-type, body, headers)`` for Jira's 401 (:data:`JIRA_UNAUTHENTICATED`), as
    real picks them from ``accept`` and ``method``; the `WWW-Authenticate` every one carries is the
    caller's, and a body of None is Jira's 500 page (:func:`jira_oops_page`), which carries the
    response's own trace id and so is the caller's to fill.

    Measured 2026-10-13 on `OPTIONS serverInfo` with no `Accept` and sixty-six values, the same
    on `GET myself` for each value that admits JSON (the 406 answers the others there first), and
    2026-10-14 on `GET` and `HEAD` of operations that give a caller with no credential no 406
    (`license/approximateLicenseCount`, `app/module/dynamic`) for the two answers that turn on the
    method:

    - a header any member of which Jira cannot read (:func:`_accept`: `foo`, `*.*`, `*/json`,
      `q=2`, `charset=bogus`), wherever it stands: the 401 with no body, under
      `text/html;charset=UTF-8`, with `Allow` naming eight methods on an `OPTIONS` and none on the
      others; but :data:`_JIRA_READS_AS_JSON`, which is `application/json`
    - a bare `*` is passed over, and with nothing else, as with no `Accept` or an empty one:
      `text/plain;charset=UTF-8` (`*;q=0.5` too, and `*, application/json` is `application/json`)
    - `text/html` first found anywhere past the header's start, in that case
      (`application/json, text/html`, `application/json, xtext/html`, `image/*, text/html`; but not
      `text/html;q=0.1, application/json, text/html`, where it is found at the start), or a `*/*`
      anywhere in the list, whatever its `q` (`application/json, */*;q=0.1` and `*/*;q=0` among
      them): `text/html;charset=UTF-8`, and with it `X-Frame-Options: SAMEORIGIN`
    - otherwise the member with the highest `q`, the first of those tied, `q=0` counting as any
      other value does (`application/json;q=0.5, text/plain` and `text/plain, application/json`
      are both `text/plain`): its type lower-cased, `foo/bar` and `image/png` included, its other
      parameters but `q` kept in their order (`application/json;level=1`), a `q` named in another
      case kept as `q` but on a JSON type naming no charset (`application/json;Q=0.5`,
      `text/plain;q=0.5;x=y` for `text/plain;Q=0.5;x=y`), and last `;charset=UTF-8` or the
      charset the member names, spelled as it spells it and unquoted, the line encoded in it
      (UTF-16, `utf-16` or `utf16`, is 108 bytes, big-endian behind a byte-order mark; UTF-32 is
      212, big-endian behind none; `x-MacRoman`, a charset Java knows and Python by another name, is
      53); `text/*` is `text/plain`, `application/*` is `application/json`, and
      `application/*+json`, whatever charset it names, is `application/json;charset=UTF-8`
    - a wildcard under any other type (`image/*`, `audio/*`, `video/*`, `multipart/*`): on an
      `OPTIONS` a 500 with no body, under `text/html;charset=utf-8`, with `Allow` naming seven
      methods, and on the others Jira's 500 page with no `Allow`
    """
    body = JIRA_UNAUTHENTICATED.encode()
    if (accept or "").strip(" \t") == _JIRA_READS_AS_JSON:
        accept = "application/json"
    _, unread = _accept(accept)
    options = method == "OPTIONS"
    if unread:
        return 401, HTML_MEDIA_TYPE, b"", {"Allow": _JIRA_UNREAD_ALLOW} if options else {}
    if (accept or "").find("text/html") > 0:
        return 401, HTML_MEDIA_TYPE, body, {"X-Frame-Options": "SAMEORIGIN"}
    named = [
        m for m in _spring_members(accept or "") if m.split(";", 1)[0].strip(_JAVA_TRIMMED) != "*"
    ]
    ranges, _ = _accept(",".join(named))
    if not ranges:
        return 401, "text/plain;charset=UTF-8", body, {}
    if any(kind == "*" for kind, _, _ in ranges):
        return 401, HTML_MEDIA_TYPE, body, {"X-Frame-Options": "SAMEORIGIN"}
    best = max(range(len(ranges)), key=lambda i: (_quality(ranges[i][2]), -i))
    kind, subtype, parameters = ranges[best]
    if (kind, subtype) == ("application", "*+json"):
        return 401, JIRA_JSON_MEDIA_TYPE, body, {}
    if subtype == "*":
        if kind not in ("text", "application"):
            if not options:
                return 500, JIRA_OOPS_MEDIA_TYPE, None, {}
            return 500, JIRA_OOPS_MEDIA_TYPE, b"", {"Allow": _JIRA_NO_TYPE_ALLOW}
        subtype = "plain" if kind == "text" else "json"
    names_charset = any(n.lower() == "charset" for n, _ in parameters)
    keeps_case = (subtype == "json" or subtype.endswith("+json")) and not names_charset
    kept = [
        ("q" if n.lower() == "q" and not keeps_case else n, v)
        for n, v in parameters
        if n.lower() != "charset" and n != "q"
    ]
    charset = next(
        (_unquoted(v) for n, v in reversed(parameters) if n.lower() == "charset"), "UTF-8"
    )
    codec = _python_codec(charset)
    # Java writes UTF-16 big-endian behind its byte-order mark and UTF-32 big-endian behind none,
    # where Python's codecs write the machine's order and a mark on both
    if codec == "utf-16":
        body = b"\xfe\xff" + JIRA_UNAUTHENTICATED.encode("utf-16-be")
    elif codec == "utf-32":
        body = JIRA_UNAUTHENTICATED.encode("utf-32-be")
    else:
        body = JIRA_UNAUTHENTICATED.encode(codec)
    headers = {"X-Frame-Options": "SAMEORIGIN"} if (kind, subtype) == ("text", "html") else {}
    extra = "".join(f";{n}={v}" for n, v in kept)
    return 401, f"{kind}/{subtype}{extra};charset={charset}", body, headers


def connect_token_answer(accept: str | None) -> tuple[str, bytes]:
    """``(content-type, body)`` for the gateway's Connect-token 403: the 57 bytes of
    `<error>…</error>` under `application/xml` where ``accept`` holds `application/xml` and not
    `application/json`, each found anywhere in it and in any case, and the JSON otherwise. Measured
    2026-10-16 on `serverInfo` with 55 values and on an issue with four: `xapplication/xml` is the
    XML, and `application/xml, application/json` and `text/xml` the JSON.
    """
    named = (accept or "").lower()
    if "application/xml" in named and "application/json" not in named:
        return "application/xml", f"<error>{CONNECT_TOKEN_UNREADABLE}</error>".encode()
    return "application/json", json.dumps(connect_token_body()).encode()


#: A type, a subtype or a parameter's name or bare value, as Jersey's header reader takes one.
_JERSEY_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def jersey_media_type(value: str) -> tuple[str, str, list[tuple[str, str]]] | None:
    """``value`` as Confluence's JAX-RS services read a media type, its type and subtype
    lower-cased beside its parameters, each name lower-cased and each value unquoted, or None where
    they cannot read it.

    Measured 2026-10-13 on `POST template` with 45 `Content-Type` values: `type/subtype` in any
    case, then parameters each after a `;`, where whitespace may stand before a `;` or after it and
    an empty one is skipped (`application/json;;`, `application/json; `), and a parameter is a name,
    a `=` with no whitespace either side, and a value that is a token or a quoted string. A
    parameter with no `=` (`;x`), with nothing after its `=` (`;x=`), with whitespace beside the `=`
    (`;charset =utf-8`) or with an unterminated quote makes the header unreadable, and so does an
    empty header, a comma or anything after the subtype but a parameter. A charset is not looked
    up (`charset=nope` is read). A space or a tab at either end is trimmed, where U+00A0 or U+0085
    there leaves the value unreadable (2026-10-16, each after the type and before it).
    """
    text = value.strip(" \t")
    head = re.match(r"([^/;\s]+)/([^;\s]+)[ \t]*", text)
    if not head or not (_JERSEY_TOKEN.fullmatch(head[1]) and _JERSEY_TOKEN.fullmatch(head[2])):
        return None
    parameters = _jersey_parameters(text, head.end())
    return None if parameters is None else (head[1].lower(), head[2].lower(), parameters)


def _jersey_parameters(text: str, pos: int) -> list[tuple[str, str]] | None:
    """The parameters ``text`` carries from ``pos`` on, by :func:`jersey_media_type`'s grammar."""
    parameters = []
    while pos < len(text):
        if text[pos] != ";":
            return None
        pos += 1
        while pos < len(text) and text[pos] in " \t":
            pos += 1
        if pos == len(text) or text[pos] == ";":
            continue
        name = _JERSEY_TOKEN.match(text, pos)
        if not name or name.end() == len(text) or text[name.end()] != "=":
            return None
        pos = name.end() + 1
        if text.startswith('"', pos):
            close = pos + 1
            while close < len(text) and text[close] != '"':
                close += 1 if text[close] != "\\" else 2
            if close >= len(text):
                return None
            given, pos = text[pos + 1 : close], close + 1
        else:
            bare = _JERSEY_TOKEN.match(text, pos)
            if not bare:
                return None
            given, pos = bare[0], bare.end()
        parameters.append((name[0].lower(), given))
        while pos < len(text) and text[pos] in " \t":
            pos += 1
    return parameters


def jaxrs_media_type_refusal(value: str | None, *, checks_type: bool) -> int | None:
    """The status a Confluence JAX-RS write refuses a `Content-Type` of ``value`` with, or None: 400
    (Tomcat's page, :func:`tomcat_page`) for one :func:`jersey_media_type` cannot read, an
    empty value among them, and where it ``checks_type``, 415 with no body for a type JSON is not
    compatible with in Jersey's sense, a wildcard type or subtype matching anything (`*/*`,
    `application/*` and `*/json` are read as JSON, `text/*` and `application/*+json` are not),
    measured 2026-10-13. An absent `Content-Type` is not refused here; real's `XSRF check failed`
    403 for a `POST` with none is not copied."""
    if value is None:
        return None
    media_type = jersey_media_type(value)
    if media_type is None:
        return 400
    kind, subtype, _ = media_type
    if checks_type and not (kind == "*" or (kind == "application" and subtype in ("*", "json"))):
        return 415
    return None


# The three sentences Jira answers a POST body it cannot turn into an object, measured 2026-09-15.
# Each arrives as `errorMessages` ALONE — no `errors`, where the refusals below it carry one — so
# they go through :class:`AtlassianError` rather than in Jira's envelope (:func:`_jira_envelope`).
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


def properties_null_element(index: int) -> AtlassianError:
    """Jira's 500 for a `search/jql` body whose `properties` list holds a ``null``, measured
    2026-10-10: the message names the first null's index (`["a", null]` names index 1), on the
    same `application/json;charset=UTF-8` as the 400s.
    """
    return AtlassianError(
        500,
        {"message": f"at index {index}", "status-code": 500, "stack-trace": ""},
        media_type=JIRA_JSON_MEDIA_TYPE,
    )


def reconcile_issues_null() -> AtlassianError:
    """Jira's 500 for a `search/jql` body whose `reconcileIssues` is JSON ``null`` rather than
    unsent, measured 2026-10-10: the message is the NPE the operation throws streaming the list,
    verbatim, on the same `application/json;charset=UTF-8` as the 400s.
    """
    return AtlassianError(
        500,
        {
            "message": 'Cannot invoke "java.util.List.stream()" because "reconcileIssues" is null',
            "status-code": 500,
            "stack-trace": "",
        },
        media_type=JIRA_JSON_MEDIA_TYPE,
    )


def reconcile_issues_too_many() -> AtlassianError:
    """Jira's 400 for more than 50 `reconcileIssues`, measured 2026-10-12 on both GET and POST."""
    return AtlassianError(
        400,
        {
            "errorMessages": ["The max number of issues to reconcile is 50."],
            "errors": {},
        },
        media_type=JIRA_JSON_MEDIA_TYPE,
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
    covers. ``path`` is Backlot's, prefix and all, and a trailing slash on it is dropped before
    matching: it is the path a refusal echoes (``backlot.routers.atlassian._echoed_path``), and
    no row above has one."""
    vendor_path = _instance(path).rstrip("/")
    for pattern, methods in _JIRA_ALLOW_PATTERNS:
        if pattern.fullmatch(vendor_path):
            return ", ".join(methods)
    return None


#: The methods each Confluence route here takes, as the `Allow` on its 405's page spells them
#: (which shapes carry that header is :func:`confluence_refusal`'s). Measured 2026-10-13 under
#: `Accept: text/html`, with a method each path does not take; real's order changed from one
#: request to the next (`space` answered `POST, GET` and `GET, POST`), so each row is one spelling
#: it sent.
_CONFLUENCE_ALLOW = (
    ("/wiki/rest/api/space", "GET, POST"),
    ("/wiki/rest/api/space/{key}", "PUT, DELETE, GET"),
    ("/wiki/rest/api/space/{key}/permission", "POST"),
    ("/wiki/rest/api/content", "GET, POST"),
    ("/wiki/rest/api/content/{id}", "DELETE, GET, PUT"),
    ("/wiki/rest/api/content/{id}/child/page", "GET"),
    ("/wiki/rest/api/content/{id}/child/comment", "GET"),
    ("/wiki/rest/api/content/{id}/child/attachment", "GET, POST, PUT"),
    ("/wiki/rest/api/content/{id}/label", "GET, POST, DELETE"),
    ("/wiki/rest/api/content/{id}/restriction/byOperation", "GET"),
)
_CONFLUENCE_ALLOW_PATTERNS = tuple((route_regex(t), allow) for t, allow in _CONFLUENCE_ALLOW)
#: `search` is a JAX-RS service, which answers a method it does not take with no body under no
#: `content-type` whatever `Accept` asks for, and an `Allow` of its own spelling: measured
#: 2026-10-13, `POST search` under `text/html` and under `application/json`.
_CONFLUENCE_SEARCH = "/wiki/rest/api/search"
_CONFLUENCE_SEARCH_ALLOW = "HEAD,GET,OPTIONS"


def method_not_allowed(path: str, method: str) -> AtlassianError:
    """The 405 each product answers for a method it does not serve at ``path``.

    `HEAD` and `OPTIONS` are not among them, because real answers both rather than refusing them:
    a `HEAD` is rewritten to its GET before routing
    (``backlot.main.answer_head_as_the_get_without_its_body``) and an `OPTIONS` is answered by
    ``backlot.routers.atlassian._options_answer``, so neither arrives here.

    Neither product answers this refusal in its :func:`http_body` envelope, and they part in two
    directions — Confluence's `errors` is a LIST carrying no `Allow` (Tomcat's page names one,
    :data:`_CONFLUENCE_ALLOW`, which :func:`confluence_refusal` says an `Accept` draws), Jira's is
    RFC 7807 on `application/problem+json` naming the methods that path takes (:data:`_JIRA_ALLOW`).
    A Jira path carrying a bracket is Tomcat's 405 page instead (:func:`_jira_tomcat_refusal`).
    A client reading `errors[0]["code"]` is the one Confluence's 405 serves; Confluence's
    served-route envelope (:func:`_confluence_envelope`) has no `errors` at all. Confluence's
    `search` is neither (:data:`_CONFLUENCE_SEARCH_ALLOW`).

    ``headers`` of ``{}`` is the empty header set, not "no opinion": real sends no `Allow` on a path
    no row names. Starlette would compute one there from the catch-all
    (``backlot.routers.atlassian.unmatched_path``), which takes every method it lists on every path
    it owns, so what it would advertise is neither the vendor's set nor anything Backlot serves.
    A method outside :data:`SERVED_METHODS` never arrives here: a layer in front of the application
    answers it first (:func:`front_door`).
    """
    if is_confluence(path):
        vendor_path = _instance(path).rstrip("/")
        if vendor_path == _CONFLUENCE_SEARCH:
            return AtlassianError(405, b"", headers={"Allow": _CONFLUENCE_SEARCH_ALLOW})
        allow = next(
            (
                allow
                for pattern, allow in _CONFLUENCE_ALLOW_PATTERNS
                if pattern.fullmatch(vendor_path)
            ),
            None,
        )
        return AtlassianError(
            405,
            {
                "errors": [
                    {
                        "status": 405,
                        "code": "METHOD_NOT_ALLOWED",
                        "title": (
                            "org.springframework.web.HttpRequestMethodNotSupportedException: "
                            f"Request method '{method}' not supported"
                        ),
                    }
                ]
            },
            headers={} if allow is None else {"Allow": allow},
        )
    allow = jira_allow(path)
    if _JIRA_BRACKET.search(path):
        return _jira_tomcat_refusal(405, {} if allow is None else {"Allow": allow})
    return AtlassianError(
        405,
        {
            "type": "about:blank",
            "title": "Method Not Allowed",
            "status": 405,
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
#: headers (:data:`GATEWAY_REFUSED`). Each layer's page is :func:`front_door`'s. Over
#: `backlot.serve()`, uvicorn's parser answers a method it has no name for (`FOO`, `get`) with its
#: own 400 before the application sees it; the ones it names (`CONNECT`, `PROPFIND`, `MKACTIVITY`)
#: reach the rule here.
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


#: The page the CDN answers each of its refusals with, `text/html` with no charset, measured
#: 2026-10-12 on 44 of them: the status in `<H1>`, one line saying why (:data:`CDN_BAD_REQUEST`,
#: :data:`CDN_METHOD_FORBIDDEN` or :data:`CDN_BLOCKED`), and a request id that is the response's
#: own `x-amz-cf-id`, 54 characters of the URL-safe base64 alphabet and `==` on every one.
_CLOUDFRONT_PAGE = (
    '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" '
    '"http://www.w3.org/TR/html4/loose.dtd">\n'
    '<HTML><HEAD><META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=iso-8859-1">\n'
    "<TITLE>ERROR: The request could not be satisfied</TITLE>\n"
    "</HEAD><BODY>\n"
    "<H1>{status} ERROR</H1>\n"
    "<H2>The request could not be satisfied.</H2>\n"
    '<HR noshade size="1px">\n'
    "{reason}\n"
    "We can't connect to the server for this app or website at this time. There might be too much "
    "traffic or a configuration error. Try again later, or contact the app or website owner.\n"
    '<BR clear="all">\n'
    "If you provide content to customers through CloudFront, you can find steps to troubleshoot "
    "and help prevent this error by reviewing the CloudFront documentation.\n"
    '<BR clear="all">\n'
    '<HR noshade size="1px">\n'
    "<PRE>\n"
    "Generated by cloudfront (CloudFront)\n"
    "Request ID: {request_id}\n"
    "</PRE>\n"
    "<ADDRESS>\n"
    "</ADDRESS>\n"
    "</BODY></HTML>"
)
#: Why the CDN refuses `TRACE` and `CONNECT` with 405, a method spelled outside the 403's rule with
#: 400, and a path carrying `%00` or a broken escape (:data:`_BROKEN_ESCAPE`) with 400.
CDN_BAD_REQUEST = "Bad request."
#: A `%` in the path that two hex digits do not follow: `%zz`, `%G0`, `%2g`, `%u005c`, a lone `%`
#: or `%2` at the end, and the first `%` of `%%41`, each the CDN's 400 on both products, measured
#: 2026-10-13. A query string's broken escape passes the CDN.
_BROKEN_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
#: Why it refuses a method by the 403's spelling (:func:`cdn_forbids`).
CDN_METHOD_FORBIDDEN = (
    "This distribution is not configured to allow the HTTP request method that was used for this "
    "request. The distribution supports only cachable requests."
)
#: Why its firewall refuses a path (:func:`waf_blocks`).
CDN_BLOCKED = "Request blocked."

#: What the gateway answers a `PATCH` with: Jira's nginx page and Confluence's openresty one, each
#: `text/html` with no charset and CRLF line ends, measured 2026-10-12.
_GATEWAY_PATCH_PAGE = (
    "<html>\r\n<head><title>{status} {phrase}</title></head>\r\n<body>\r\n"
    "<center><h1>{status} {phrase}</h1></center>\r\n<hr><center>{server}</center>\r\n"
    "</body>\r\n</html>\r\n"
)

#: The page Confluence's Tomcat answers a refusal with where `Accept` takes no JSON and names no
#: `+json` type (:func:`confluence_refusal`), and an encoded slash's or backslash's 400 whatever it
#: takes (:func:`front_door`) and a JAX-RS write's for a `Content-Type` it cannot read
#: (:func:`jaxrs_media_type_refusal`): the status and its reason in the title and in `<h1>` and
#: nothing else, on `text/html;charset=utf-8` with `Content-Language: en`. Measured 2026-10-12 for
#: the 400 and 2026-10-13 for the 403, 405, 406 and 415 (:func:`confluence_refusal`), 431 to 457
#: bytes.
_TOMCAT_PAGE = (
    '<!doctype html><html lang="en"><head><title>HTTP Status {status} – {phrase}</title>'
    '<style type="text/css">body {{font-family:Tahoma,Arial,sans-serif;}} h1, h2, h3, b '
    "{{color:white;background-color:#525D76;}} h1 {{font-size:22px;}} h2 {{font-size:16px;}} h3 "
    "{{font-size:14px;}} p {{font-size:12px;}} a {{color:black;}} .line {{height:1px;"
    "background-color:#525D76;border:none;}}</style></head><body><h1>HTTP Status {status} – "
    "{phrase}</h1></body></html>"
)
_TOMCAT_PHRASES = {
    400: "Bad Request",
    403: "Forbidden",
    405: "Method Not Allowed",
    406: "Not Acceptable",
    415: "Unsupported Media Type",
}


def tomcat_page(status: int) -> str:
    """Tomcat's page for ``status``, one of the five it was measured for."""
    return _TOMCAT_PAGE.format(status=status, phrase=_TOMCAT_PHRASES[status])


TOMCAT_MEDIA_TYPE = "text/html;charset=utf-8"


#: The tag names the CDN's firewall refuses a path for, whatever their case, and the prefixes it
#: refuses every name beginning with (:func:`waf_blocks` carries the measurement). A name runs to
#: whitespace, `/`, `<`, `>` or the end of the path, and a closing tag is refused for `script`
#: alone. `<!doctype` and `<!entity` are prefixes too (`<!DOCTYPEhtml>` and `<!entityx>` refused,
#: `<!doctyp>` and `<!ELEMENT>` served, 2026-10-13).
_WAF_TAGS = frozenset(
    "applet base comment embed frame frameset handler iframe import isindex link listener meta "
    "noscript object script style vmlframe xml xss".split()
)
_WAF_TAG_PREFIXES = ("svg", "xsl", "!doctype", "!entity")
_TAG = re.compile(r"<(/?)([^\t\n\v\f\r /<>]*)")
#: A step up a directory as the firewall reads one in the path: `..` followed by `/` or `\`
#: anywhere, measured 2026-10-12, and a whole segment `..` followed by one or more `;` and a `/`
#: (`/..;/`, `/..;;/`, `/..;%3B/`), measured 2026-10-13. A `..;` inside a segment (`/a..;/`) or
#: with no `/` after its semicolons (`/..;a/`) is not refused here; real answers it further in,
#: with a 400 page, the edge's for the `;` in it (:func:`front_door`).
_PATH_STEP = re.compile(r"\.\.[/\\]|(?<![^/])\.\.;+/")
#: The same in a query parameter's value, where the firewall reads less: `../` anywhere, and `..\`
#: where a `\` or a `/` follows it or the value ends (`?x=..%5C`, `?x=..%5C%5Cy` and
#: `?x=..%5C%2Fy` refused, `?x=..%5Cy` and `?x=..%5C.` served, the whole 0x01-0x7F range swept
#: after the `\`), measured 2026-10-13. A parameter's name is not read (`?..%2F=1` served).
_QUERY_STEP = re.compile(r"\.\./|\.\.\\(?:[\\/]|\Z)")


def _decoded_twice(text: str) -> str:
    """``text`` percent-decoded twice, the depth the firewall was measured to read to
    (2026-10-12)."""
    return unquote(unquote(text))


#: An HTML entity the firewall resolves, which is one terminated by a semicolon: `&lt;` is read
#: and `&lt` (no semicolon) is not, measured 2026-10-12 (`&ltscript` serves where `&lt;script`
#: refuses). Python's :func:`html.unescape` resolves the semicolon-less legacy forms too, so this
#: feeds it only the terminated ones.
_ENTITY = re.compile(r"&(#[0-9]+|#[xX][0-9a-fA-F]+|[0-9A-Za-z]+);")


def _resolve_entities(text: str) -> str:
    """``text`` with its semicolon-terminated HTML entities resolved, as the firewall reads them."""
    return _ENTITY.sub(lambda m: html.unescape(m.group(0)), text)


def waf_blocks(raw_path: str, raw_query: str) -> bool:
    """Whether the CDN's firewall refuses the request, with its 403 (:data:`CDN_BLOCKED`).

    What is copied is the part of it a table can hold, measured on both products 2026-10-12 (the
    query's rule, `..;` and the `<!` prefixes 2026-10-13): a tag
    :data:`_WAF_TAGS` names in the path, read after two percent-decodings and with HTML entities
    resolved, and a step up a directory in the path (:data:`_PATH_STEP`) or in a query parameter's
    value (:data:`_QUERY_STEP`), read after the two percent-decodings alone. The firewall refuses
    more than this table holds; a request it would refuse on other grounds reaches the routes here.
    """
    path = _resolve_entities(_decoded_twice(raw_path))
    for match in _TAG.finditer(path):
        closing, name = match.group(1), match.group(2).lower()
        if closing:
            if name == "script":
                return True
        elif name in _WAF_TAGS or name.startswith(_WAF_TAG_PREFIXES):
            return True
    if _PATH_STEP.search(_decoded_twice(raw_path)):
        return True
    for parameter in raw_query.split("&"):
        name, eq, value = parameter.partition("=")
        if eq and _QUERY_STEP.search(_decoded_twice(value)):
            return True
    return False


class FrontDoor(NamedTuple):
    """An answer a layer in front of the application gives. ``layer`` names whose headers it
    carries, not which layer answered: `cdn` adds no header of its own, `cdn edge` adds the edge's
    two, and `gateway` its two ids beside those, which Confluence's Tomcat 400 carries too
    (measured 2026-10-14)."""

    status: int
    layer: str
    media_type: str
    body: str
    headers: dict[str, str]


def _cloudfront(status: int, reason: str, method: str, raw_path: str) -> str:
    seed = hashlib.sha512(f"cf:{method} {raw_path}".encode()).digest()[:40]
    request_id = base64.urlsafe_b64encode(seed).decode()
    return _CLOUDFRONT_PAGE.format(status=status, reason=reason, request_id=request_id)


def cdn_bad_request(method: str, raw_path: str) -> str:
    """The CDN's 400 page, which :func:`front_door` answers a `%00` in the path with."""
    return _cloudfront(400, CDN_BAD_REQUEST, method, raw_path)


#: What the edge answers a `;` in a path with, JSON or a page as `Accept` picks
#: (:func:`front_door`). The JSON is this body, 28 bytes with its newline, on `application/json`;
#: real's page is 18,943 bytes on `text/html` with no charset,
#: `<meta name="details" content="lua_response">` among them, reading the status and a request id of
#: its own (2026-10-12, 2026-10-18 and 2026-10-19). The stub here keeps the status, the media type
#: and that tag, so a `HEAD` declares the stub's length. Both carry `Access-Control-Allow-Origin: *`
#: beside the edge's two headers, and the gateway's two ids beside those where the path holds the
#: `;` escaped and none bare (2026-10-19: the first on all 192 such answers sent, the ids on the 55
#: with `%3B` or `%3b` alone and on none of the 137 with a bare `;`).
_EDGE_JSON_400 = '{"message":"","status":400}\n'
_EDGE_PAGE_400 = (
    '<!DOCTYPE html><html><head><meta name="details" content="lua_response">'
    "<title>400</title></head><body><p>400</p></body></html>"
)


def edge_page(details: str) -> str:
    """:data:`_EDGE_PAGE_400` naming ``details`` in its tag, the part of the edge's 400 page that
    tells one of its refusals from another (``backlot.request_target`` answers two more of them)."""
    return _EDGE_PAGE_400.replace('content="lua_response"', f'content="{details}"')


def cdn_refusal(method: str, raw_path: str, raw_query: str) -> FrontDoor | None:
    """The CDN's own page for the first three of :func:`front_door`'s checks: `%00` or a broken
    escape in the path, a method it does not pass on and its firewall; or None."""
    if "%00" in raw_path or _BROKEN_ESCAPE.search(raw_path):
        return FrontDoor(
            400, "cdn", "text/html", _cloudfront(400, CDN_BAD_REQUEST, method, raw_path), {}
        )
    if method not in SERVED_METHODS and method not in GATEWAY_REFUSED:
        if method in CDN_405:
            status, layer, reason = 405, "cdn", CDN_BAD_REQUEST
        elif cdn_forbids(method):
            status, layer, reason = 403, "cdn edge", CDN_METHOD_FORBIDDEN
        else:
            status, layer, reason = 400, "cdn", CDN_BAD_REQUEST
        page = _cloudfront(status, reason, method, raw_path)
        return FrontDoor(status, layer, "text/html", page, {})
    if waf_blocks(raw_path, raw_query):
        page = _cloudfront(403, CDN_BLOCKED, method, raw_path)
        return FrontDoor(403, "cdn edge", "text/html", page, {})
    return None


def front_door(
    method: str, raw_path: str, raw_query: str, accept: str | None = None
) -> FrontDoor | None:
    """What answers an `/atlassian` request before either product sees it, or None.

    The checks a request meets, in order, measured 2026-10-12 by sending two of them wrong at once:
    the CDN refuses a path carrying `%00` or a broken escape (:data:`_BROKEN_ESCAPE`, 2026-10-13)
    with its 400, ahead of everything (a `TRACE`, a `PROPFIND` or a tag the firewall names beside
    one is that 400 too); then a method it does not pass on (:data:`SERVED_METHODS`); then its
    firewall (:func:`waf_blocks`), so a `PROPFIND` on a path the firewall refuses is the method's
    403 and a `PATCH` on one is the firewall's; then the edge's 400 for a `;` anywhere in the path,
    bare or as `%3B` (2026-10-19, each spelling: behind `%00`, a `PROPFIND` and the firewall's
    `..;/` or `..%3B/`, ahead of the gateway's `PATCH` on both products and of Tomcat's encoded
    slash and backslash); then the gateway's `PATCH` (:data:`GATEWAY_REFUSED`). The `;` refusal is
    JSON where `Accept` holds `application/json` as spelled, wherever it stands and whatever its `q`
    (`text/html, application/json` and `application/json;q=0, text/html` were JSON,
    `APPLICATION/JSON` the page), and the page (:data:`_EDGE_PAGE_400`) for anything else, none
    included; the same on both products, with an API token and with none, for `GET`, `HEAD`,
    `OPTIONS`, `POST`, `PUT`, `DELETE` and `PATCH`, on served routes, on paths no route matches and
    outside the API mounts (`/browse/A-1;x`, `/wiki/spaces/X;y`, `/x;y`), 2026-10-12 to 2026-10-19.
    Confluence's Tomcat answers an encoded slash anywhere under `/wiki/` with 400, for every method,
    every credential and the `Accept` values `*/*`, `application/json`, `application/xml` and none,
    where Jira decodes one and routes the decoded path; an encoded backslash is the same 400
    (2026-10-13), and on Jira's API the same page under both values measured (2026-10-18).
    `/wiki/spaces/` is the exception, in any case (`/wiki/SPACES/`), the web app's own pages, which
    answered both with 200 (2026-10-13). Each layer's answer is the same whatever the credential,
    and the gateway's Connect-token 403 comes after all of them. Which product a path is the
    gateway's to decide, and it reads the path with each run of slashes as one:
    `PATCH //wiki/rest/api/space` is openresty's 405 (2026-10-13).

    An `Accept` naming `text/html` first draws the edge's own "Page Unavailable" page in place of
    Tomcat's, which is not copied. ``raw_path`` is the path as sent, prefix included, which is
    where an encoded slash and `%00` can still be told from a decoded one.
    """
    door = cdn_refusal(method, raw_path, raw_query)
    if door is not None:
        return door
    collapsed = re.sub(r"/{2,}", "/", raw_path)
    confluence = collapsed == WIKI or collapsed.startswith(f"{WIKI}/")
    encoded = collapsed.lower()
    if ";" in encoded or "%3b" in encoded:
        # the gateway's ids where no `;` is bare (:data:`_EDGE_JSON_400`)
        layer = "cdn edge" if ";" in encoded else "gateway"
        edge = {"Access-Control-Allow-Origin": "*"}
        if accept is not None and "application/json" in accept:
            return FrontDoor(400, layer, "application/json", _EDGE_JSON_400, edge)
        return FrontDoor(400, layer, "text/html", _EDGE_PAGE_400, edge)
    if method in GATEWAY_REFUSED:
        status, phrase, server = (
            (405, "Not Allowed", "openresty") if confluence else (400, "Bad Request", "nginx")
        )
        page = _GATEWAY_PATCH_PAGE.format(status=status, phrase=phrase, server=server)
        return FrontDoor(status, "gateway", "text/html", page, {})
    if (
        collapsed.startswith(f"{WIKI}/")
        and not encoded.startswith(f"{WIKI}/spaces/")
        and ("%2f" in encoded or "%5c" in encoded)
    ) or (serves_the_jira_api(collapsed) and "%5c" in encoded):
        return FrontDoor(
            400, "gateway", TOMCAT_MEDIA_TYPE, tomcat_page(400), {"Content-Language": "en"}
        )
    return None


def no_such_role() -> AtlassianError:
    """Jira's 404 for a project role id that names no role of the project, with an empty
    `errorMessages` where :func:`_jira_envelope` carries the message: 32 bytes on
    `application/json;charset=UTF-8`. Measured on Jira Cloud 2026-10-14 with a credential that can
    read the project's roles, for `-1`, `-0x1`, `11` and a `1` on either side of an em space, each
    read as ``backlot.routers.atlassian._java_long`` reads it."""
    return AtlassianError(404, {"errorMessages": [], "errors": {}}, media_type=JIRA_JSON_MEDIA_TYPE)


def not_a_project_admin(realm: str) -> AtlassianError:
    """Jira's 401 for a project role operation by a caller who does not administer the project: the
    message in :func:`_jira_envelope`, 84 bytes on `application/json;charset=UTF-8`, with
    `WWW-Authenticate: OAuth realm="<realm>"` and no `X-Frame-Options`. ``realm`` is the site,
    percent-encoded. Measured on Jira Cloud 2026-10-15 with a credential that browses the project
    and does not administer it, on its role list, on role ids that name a role, that name none and
    `-1`, and on `POST`, `PUT` and `DELETE` of a role, by the project's key and by its id."""
    return AtlassianError(
        401,
        _jira_envelope("You cannot edit the configuration of this project."),
        media_type=JIRA_JSON_MEDIA_TYPE,
        headers={"WWW-Authenticate": f'OAuth realm="{realm}"'},
    )


def no_endpoint(path: str, method: str) -> AtlassianError:
    """Jira's 404 for a path it mounts no endpoint at, in the RFC 7807 shape its other refusals use.

    Measured on Jira Cloud, 2026-09-22: `/rest/api/3/nopesuchroute`, the same under `/rest/api/2`,
    `/rest/api/4/serverInfo`, `/rest/nope/thing`, and the paths that extend a served route
    (`serverInfo/extra`, `issue/NOPE-1/nope`, `project/search/extra`) each answer this body.
    `detail` names the method as sent -- `GET`, `POST`, `DELETE` and `OPTIONS` each came back in it,
    and `PUT` with no credential on 2026-09-30 -- and both fields carry the vendor path with the
    query string left off. A credential changes nothing; an `Accept` holding a member Jira cannot
    read empties the body (:func:`rendered`). A path carrying a bracket, escaped or bare, is the
    dead-link page instead (:data:`_JIRA_BRACKET`).
    """
    if _JIRA_BRACKET.search(path):
        return AtlassianError(
            404, HTML_NOT_FOUND.encode(), headers={"Content-Type": JIRA_SITE_HTML_MEDIA_TYPE}
        )
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
#: `nope/deeper/still`, `application/json` answers the JSON below, in any case (`APPLICATION/JSON`,
#: 2026-10-13), and absent, `*/*` and `application/xml` the XML, with or without a credential; which
#: a list picks is :func:`jaxrs_picks_json`'s. The message carries the full request URL, query
#: string included.
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

#: What an `OPTIONS` on a Confluence route answers where `Accept` admits JSON: the 404 above in the
#: `errors` list its 405 uses, on every route measured 2026-09-22 but `search`, which answers by
#: `Accept` its own way (``backlot.routers.atlassian._search_options``). What the other values draw
#: is :func:`confluence_options`.
CONFLUENCE_OPTIONS_NOT_FOUND = {
    "errors": [{"status": 404, "code": "NOT_FOUND", "title": "Not Found"}]
}
#: The `Allow` that `OPTIONS` carries where `Accept` cannot be read, measured 2026-10-13.
_CONFLUENCE_UNREAD_OPTIONS_ALLOW = "GET, HEAD, POST, PUT, DELETE, TRACE, OPTIONS, PATCH"


def confluence_options(accept: str | None) -> Response:
    """What an `OPTIONS` on a Confluence route but `search` answers, by ``accept``.

    The 404 (:data:`CONFLUENCE_OPTIONS_NOT_FOUND`) in the shapes :func:`confluence_refusal` gives a
    refusal, but for two: an `Accept` naming neither JSON nor a `+json` type draws Tomcat's 403 page
    in the 404's place, and one that cannot be read the empty 404 with
    :data:`_CONFLUENCE_UNREAD_OPTIONS_ALLOW`. Measured 2026-10-13 on `space` with no `Accept` and
    twenty-six values (and `text/html` with a credential too, the same 403), and 2026-09-30 on nine
    routes with five values each.
    """
    takes = accepts(accept, reader="spring")
    if takes is None:
        return Response(b"", status_code=404, headers={"Allow": _CONFLUENCE_UNREAD_OPTIONS_ALLOW})
    media_type = "application/json" if takes else _json_family(accept)
    if media_type is not None:
        body = json.dumps(CONFLUENCE_OPTIONS_NOT_FOUND, separators=(",", ":")).encode()
        return Response(body, status_code=404, headers={"Content-Type": media_type})
    return Response(
        tomcat_page(403),
        status_code=403,
        headers={"Content-Type": TOMCAT_MEDIA_TYPE, "Content-Language": "en"},
    )


CONFLUENCE_OPTIONS_BY_ACCEPT = "/wiki/rest/api/search"
CONFLUENCE_SEARCH_OPTIONS_ALLOW = "OPTIONS,HEAD,GET"


#: The Confluence route whose 200 and 406 declare no length on a `HEAD`
#: (:func:`head_content_length`).
_CONFLUENCE_HEAD_NO_LENGTH = "/wiki/rest/api/search"


def head_content_length(request: Request, status_code: int) -> bool:
    """Whether real Jira or Confluence puts a `content-length` on a `HEAD` to this request's path
    that answers ``status_code``.

    Measured with `curl -I` beside each `GET` the same minute. Confluence, 2026-09-22: every 200 but
    `search` declares it, to the byte (`space` 1103, `content` 1536, `content/{id}` 3909,
    `child/comment` 214, `child/page` 211, `label` 207, `restriction/byOperation` 801, `space/{key}`
    695), and the 404s, the 405 and the CQL 400 declare none. Jira's answers are chunked and
    declare none on either method (2026-09-22), but for the 401 it refuses a caller with no
    credential with (:data:`JIRA_UNAUTHENTICATED`), which declares its 53 bytes, and the `HEAD`
    beside it declares the same (`myself`, 2026-09-30), and its 500 page (:func:`jira_oops_page`,
    on `serverInfo` and `myself`, 2026-10-13). Confluence's 406 declares its length but
    on `search` (Tomcat's page on `space`, 441 bytes, and nothing on `search`'s empty one,
    2026-10-13).
    """
    path = request.url.path
    if not is_confluence(path):
        return status_code in (401, 500)
    if _instance(path) == _CONFLUENCE_HEAD_NO_LENGTH:
        return False
    return 200 <= status_code < 300 or status_code == 406


def _jira_envelope(message: str) -> dict:
    """Jira's refusal body: the message in an ``errorMessages`` list beside an empty ``errors``, and
    nothing else. Real's `issue/{key}` 404, its project 404 and its `orderBy` 400 each carry these
    two members alone, measured 2026-09-30 and 2026-10-13."""
    return {"errorMessages": [message], "errors": {}}


def _confluence_envelope(status_code: int, message: str) -> dict:
    """Confluence's refusal body: ``statusCode`` and the scalar ``message`` its Stackless 403
    carries, and nothing else, measured 2026-09-30 and 2026-10-13."""
    return {"statusCode": status_code, "message": message}


def http_body(path: str, exc, query=None) -> dict:
    """The refusal body for a served route, in the product's own envelope — Confluence's two members
    (:func:`_confluence_envelope`) under `/wiki`, Jira's two (:func:`_jira_envelope`) elsewhere.
    ``query`` is unused: no Atlassian refusal is selected by a query parameter the way Google's is
    by `$.xgafv`, and it is in the signature so the dispatch in ``__init__`` can treat every module
    alike. A path that is not a route is answered elsewhere and in a shape of its own — see
    :func:`no_endpoint`, :func:`jaxrs_not_found` and :data:`HTML_NOT_FOUND`.

    An :class:`AtlassianError` is the exception and says so by carrying its own body, served as it
    stands."""
    body = getattr(exc, "body", None)
    if body is not None:
        return body
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    if is_confluence(path):
        return _confluence_envelope(exc.status_code, message)
    return _jira_envelope(message)


def validation_body(path: str, errors) -> tuple[int, dict]:
    """A 422 from FastAPI's own request validation, in the product's refusal envelope. The per-field
    detail collapses to one sentence because that is the only part a client reads. Jira's and
    Confluence's routes read their own parameters and path variables, so a route reaches this only
    if it declares one typed."""
    message = "; ".join(e.get("msg", "invalid request") for e in errors) or "Invalid request"
    if is_confluence(path):
        return 422, _confluence_envelope(422, message)
    return 422, _jira_envelope(message)
