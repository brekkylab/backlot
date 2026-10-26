"""Atlassian Cloud APIs (read-only): Jira (``/rest/api/3``) and Confluence
(``/wiki/rest/api``). Client base_url: ``http://<host>/atlassian``.

Auth: HTTP Basic ``email:api_token`` (or Bearer). Jira issue descriptions are ADF;
Confluence bodies are storage-format XHTML — matching the real APIs.

Beside the routes, this module answers what the two products answer AROUND them: what they refuse
before a route runs (:func:`_checked_before_the_route`), an `OPTIONS`, a path neither serves and a
method neither declares (:func:`unmatched_path`), and the headers every answer carries
(:func:`vendor_headers`, put on by ``backlot.main.report_atlassian_headers``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Callable
from html.entities import codepoint2name
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Path as PathParameter
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from starlette.convertors import PathConvertor, register_url_convertor
from starlette.routing import Match

from backlot import auth, store, synth
from backlot.acl import Caller
from backlot.config import get_settings
from backlot.errors import atlassian as errors_atlassian
from backlot.openapi import qp
from backlot.pagination import (
    confluence_page_links,
    decode_cursor_or_none,
    next_page_token,
)


async def _checked_before_the_route(request: Request) -> None:
    """What real refuses once a request has reached one of the routes below and before the route
    runs, in the order it checks, measured 2026-10-13 with each pair of them sent wrong together.

    Confluence answers a Basic value it cannot read with its 401 (:func:`_confluence_caller`), and
    then an `Accept` that admits no JSON with its 406 (``errors.atlassian.not_acceptable``), the
    empty one JAX-RS gives on `search` and on the others Tomcat's page or, under a `+json` type
    `Accept` names, the `errors` list, ahead of the anonymous 403, a parameter's 400 and the route's
    own 404. Jira answers a `POST search/jql` body of a type it does not read with its 415
    (:func:`_refuse_a_search_media_type`) and then the 406, ahead of an empty body's 400, an
    unbounded query's 400, a parameter's 400 and the route's own answer. `space/{key}/permission` is
    the one route this asks the 401 alone of: the 405 it answers comes before the 406. On `search`
    a `Content-Type` its JAX-RS service will not read is refused between the 401 and the 406
    (:func:`_refuse_the_search_media_type`).
    """
    endpoint = getattr(request.scope.get("route"), "endpoint", None)
    jaxrs = False
    # the decoded path picks the product only; the refusals echo `_echoed_path` where they echo one
    if errors_atlassian.is_confluence(request.url.path):
        if auth.basic_credential_kind(request) == auth.BASIC_UNPARSEABLE:
            _confluence_caller(request)
        if endpoint is confluence_space_permission:
            return
        jaxrs = endpoint is confluence_cql_search
        if jaxrs:
            _refuse_the_search_media_type(request)
    elif endpoint is jira_search and request.method == "POST":
        _refuse_a_search_media_type(request)
    _refuse_the_accept(
        request,
        errors_atlassian.not_acceptable(
            _echoed_path(request), request.headers.get("accept"), jaxrs=jaxrs
        ),
    )


def _refuse_the_accept(request: Request, refused: errors_atlassian.AtlassianError | None) -> None:
    """Raise ``refused``, the answer to an `Accept` (``errors.atlassian.not_acceptable``), where
    there is one, marking the request so that :func:`vendor_headers` leaves off it what real does:
    the quota and `search`'s cache pair, on the 406 and on the JAX-RS 400 alike (2026-10-13)."""
    if refused is not None:
        request.scope["atlassian_accept_refused"] = True
        raise refused


router = APIRouter(
    prefix="/atlassian", tags=["atlassian"], dependencies=[Depends(_checked_before_the_route)]
)


# --- OpenAPI enrichment --------------------------------------------------
# Parameters are documented with openapi_extra (no signature change); confluence params are
# query-only. Response models use extra="allow" to
# preserve every field. Error paths raise HTTPException (Atlassian-shaped), not filtered here.
# Secondary metadata routes (roles / linktypes / labels / restrictions) are left untyped — the
# bridge still exposes them as tools; they aren't retrieval surfaces.


class _ALoose(BaseModel):
    model_config = ConfigDict(extra="allow")


# Every member `jira_server_info` answers, declared in the order Jira Cloud answers them: the
# response is written in declaration order whatever order the handler's dict has. `serverTime`,
# absent for the anonymous caller, falls back to `None`, which the routes'
# `response_model_exclude_none` leaves out.
class JiraServerInfo(_ALoose):
    baseUrl: str
    displayUrl: str
    displayUrlServicedeskHelpCenter: str
    displayUrlCSMHelpSeeker: str
    displayUrlConfluence: str
    version: str
    versionNumbers: list[int]
    deploymentType: str = "Cloud"
    buildNumber: int
    buildDate: str
    serverTime: str | None = None
    scmInfo: str
    serverTitle: str
    defaultLocale: dict
    serverTimeZone: str


class JiraSearchResult(_ALoose):
    issues: list[dict] = []
    isLast: bool = True


class JiraIssue(_ALoose):
    id: str
    key: str


class JiraComments(_ALoose):
    comments: list[dict] = []
    total: int = 0


class JiraField(_ALoose):
    id: str
    name: str


class ConfluenceResults(_ALoose):
    results: list[dict] = []


class ConfluencePage(_ALoose):
    pass


# The two take the same three parameters in different places: the GET form in the query string, the
# POST form in a `SearchAndReconcileRequestBean` body. Both of Atlassian's documents split them this
# way, down to the schema name, and the live service follows. Both placements are declared here and
# separated per method after FastAPI has built the document, by
# :func:`backlot.openapi.jira_search_placement`, because one ROUTE serves both methods.
_X_JIRA_SEARCH = {
    "parameters": [qp("jql"), qp("maxResults", "integer"), qp("nextPageToken")],
    "requestBody": {
        # Real refuses a POST carrying no body: the missing `Content-Type` is its 415, and the
        # header with an empty body is `No content to map to Object due to end of input`.
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "jql": {"type": "string"},
                        "maxResults": {"type": "integer"},
                        "nextPageToken": {"type": "string"},
                    },
                }
            }
        },
    },
}
_P_EXPAND = {"parameters": [qp("expand")]}
# `expand` is NOT declared here. The endpoint takes one on real Jira, and `expand=renderedBody`
# returns each comment's body as HTML, which Backlot does not produce — and `qp` exists only for
# parameters Backlot honours, because advertising one it ignores makes a client ask for data that
# never arrives. The gap stays where it is legible: `backlot diff --source jira` reports it, and
# the baseline records it as a `missing_param` under that operation's `params`.
_P_JIRA_COMMENTS = {
    "parameters": [qp("startAt", "integer"), qp("maxResults", "integer"), qp("orderBy")]
}
_P_CQL = {
    "parameters": [
        qp("cql", required=True),
        qp("cursor"),
        qp("limit", "integer"),
        qp("start", "integer"),
    ]
}
_P_CONTENT = {
    "parameters": [
        qp("expand"),
        qp("spaceKey"),
        qp("type"),
        qp("title"),
        qp("limit", "integer"),
        qp("start", "integer"),
    ]
}
_P_SPACE = {
    "parameters": [qp("expand"), qp("spaceKey"), qp("limit", "integer"), qp("start", "integer")]
}
# The four listings under `content/{id}`, which read the same pair with their own defaults and
# caps. `child/page` is the one of them this router also reads an `expand` on.
_P_CHILD_PAGE = {"parameters": [qp("expand"), qp("limit", "integer"), qp("start", "integer")]}
_P_CONTENT_CHILD = {"parameters": [qp("limit", "integer"), qp("start", "integer")]}

# The page a comment read serves. Measured against Jira Cloud (2026-09-09) on a real issue,
# which settles what no document states: `maxResults` is CAPPED at 100 as well as defaulted
# to it, and floors UP to 1 (0 and -1 both answer 1). `startAt` floors at 0, and one past the
# end is echoed back unchanged with an empty page rather than refused.
_JIRA_COMMENT_PAGE_MAX = 100
_JIRA_ORDER_FIELD = "created"

# Java's `^([+-]?)\s*(.*)$` under `find`: `\s` is the six ASCII spaces, `.` stops at a line
# terminator, and `$` matches at the end or before one final line terminator (`_jira_order_desc`).
_JIRA_ORDER = re.compile(
    "([+-]?)[ \t\n\x0b\x0c\r]*([^\n\r\x85\u2028\u2029]*)(?:\r\n|[\n\r\x85\u2028\u2029])?\\Z"
)


def _jira_order_desc(raw: str) -> bool:
    """Whether ``orderBy`` asks for the reverse, or Jira's 400 for a field it does not order by.

    Measured against Jira Cloud (2026-09-10). Exactly ONE leading sigil is stripped: `--created`
    keeps a `-created` that is no field, and Jira's own message echoes `-created` rather than what
    was sent. Whitespace around the sigil is ignored (`%20created`, `created%20`, `-%20created`
    are each 200) and the field matches case-insensitively (`Created`, `CREATED`).

    Whitespace matters because a literal `+` in a query string decodes to a space, so `+created` —
    the ascending spelling Atlassian's own document writes — arrives here as `" created"`. Real
    Jira answers 200 to it, so stripping is what matches rather than a special case for `+`.

    Real's answers fit Java reading the value, though Jira does not say so: `String.trim` (every
    character up to U+0020 off both ends, as :func:`_jira_project_order` measured on
    `project/search`), then the pattern `^([+-]?)\\s*(.*)$` (:data:`_JIRA_ORDER`), whose `\\s` is
    ASCII and whose `.` and `$` stop at a line terminator. Measured against Jira Cloud on the
    comment read (2026-10-16 and 2026-10-17): `%01created`, `created%01` and `%01-created` are 200;
    after the sigil `%20`, `%09`, `%0A`, `%0B`, `%0C` and `%0D` go and `%01`, `%1F`, a no-break
    space and an em space stay (`-%01created` names U+0001 and `created`); a no-break space or an em
    space in front stays in the field too; one line terminator at the end goes (`created%C2%85` is
    200) and one anywhere else matches nothing, so the 400 names the value whole, sigil and all
    (`-%C2%85created`, `-cre%0Aated`).

    An EMPTY value is refused, not read as absent: the field it leaves is the empty string, and
    real Jira answers 400 to `?orderBy=`.
    """
    field = raw.strip(_JAVA_TRIMMED)
    desc = False
    if (m := _JIRA_ORDER.match(field)) is not None:
        desc, field = m[1] == "-", m[2]
    if field.casefold() != _JIRA_ORDER_FIELD:
        # Backlot's own sentence, not a transcription: Jira localises this one to the account's
        # language, and the account it was measured against answers in Korean.
        raise HTTPException(
            status_code=400,
            detail=f"The field to order by must be one of [{_JIRA_ORDER_FIELD}]. Instead: {field}",
        )
    return desc


def _jira_caller(request: Request) -> Caller:
    """The caller for a Jira read, anonymous when no credential resolved.

    Jira does not refuse an unresolvable credential on the routes served here: it processes the
    request as an anonymous caller, and says so in ``X-Seraph-LoginReason`` (added for every
    ``/atlassian/rest`` answer in ``backlot.main``). Measured against ecosystem.atlassian.net and
    brekkylab.atlassian.net on 2026-09-04 with a failed ``email:api_token`` pair, with an empty
    password, with a value that is not base64, with an unknown scheme and with no header: each one
    answers `project/search` 200, a bounded `search/jql` 200, and `issue/{key}` 404. Only
    ``GET /rest/api/3/myself``, which Backlot does not serve, answers 401.
    """
    return auth.atlassian_caller(request)


def _confluence_caller(request: Request, *, not_permitted: bool = False) -> Caller:
    """The caller for a Confluence read, refusing where Jira would go anonymous.

    Confluence rejects the request outright: a pair it read and rejected, and a request carrying
    none, are a 403 (which one is the route's, below); a Basic value it could not read is a 401
    naming the site's OAuth realm on every route. Measured on both sites on 2026-09-04, every route
    and every credential shape (see ``auth.basic_credential_kind``).

    ``not_permitted`` picks the other anonymous 403, the one `search` gives
    (``errors.atlassian.CONFLUENCE_NOT_PERMITTED_BODY``), where every other served route gives the
    Stackless 403; measured on Confluence Cloud 2026-09-30 and 2026-10-12.
    """
    caller = auth.atlassian_caller(request)
    if not caller.is_anonymous:
        return caller
    if auth.basic_credential_kind(request) == auth.BASIC_UNPARSEABLE:
        realm = quote(f"{_site(request)}/wiki", safe="")
        raise HTTPException(
            status_code=401,
            detail=errors_atlassian.CONFLUENCE_UNAUTHORIZED,
            headers={"WWW-Authenticate": f'OAuth realm="{realm}"'},
        )
    if not_permitted:
        raise errors_atlassian.AtlassianError(
            403,
            json.loads(errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY),
            media_type="application/json",
        )
    raise HTTPException(status_code=403, detail=errors_atlassian.CONFLUENCE_FORBIDDEN)


def _site(request: Request) -> str:
    """The base of every ``self`` URL Jira/Confluence emit, from the REQUEST first.

    Echoing the caller's own ``Host`` is what makes a returned URL usable: a client reaching
    Backlot through a proxy, a container alias or a tunnel gets links back to the host it actually
    called, not to one this process was configured with. Every SDK sends the header, so the
    ``<org>.atlassian.net`` fallback is only for a hand-rolled HTTP/1.0 request — which is also why
    there is no setting here to override it. The org half is already configurable
    (``BACKLOT_ORG_NAME``).
    """
    s = get_settings()
    host = request.headers.get("host") or f"{s.org_name}.atlassian.net"
    return f"{request.url.scheme}://{host}"


# ================================ Jira ==========================================


def _adf(content: str) -> dict:
    paras = [p for p in content.split("\n\n") if p.strip()] or [content]
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paras],
    }


def _project_key(conn, container: str) -> str:
    """The project key a container serves and navigates by: the prefix its corpus-provided
    issue keys carry (aliased at index build) when the corpus wrote any, else the
    synthesized key. One spelling for the issue prefix, the project payload, JQL and the
    picker — real Jira guarantees an issue key's prefix IS its project's key, and an agent
    that reads PAY-7 out of a document will navigate by PAY."""
    return store.jira_project_key(conn, container) or synth.jira_project_key(container)


def _index_maps(request: Request) -> dict:
    # A bare Request (unit tests build them without an app scope) has no app.state;
    # absence of the maps just means "no aliases", never an error.
    try:
        return getattr(request.app.state, "index", None) or {}
    except KeyError:
        return {}


def _jira_container_for_key(conn, token: str, request: Request | None = None) -> str | None:
    """Resolve a JQL project token to its backing container. Matches the corpus-provided
    key prefix (``PAY``), the synthesized project key (``PAY3F9A2C``, case-insensitive) or
    the literal container name (e.g. ``payments``, case-insensitive) — real Jira project
    pickers accept both key and name. Anything else is unresolvable -> None (callers must
    treat this as "0 results", never silently fall back to the unfiltered corpus)."""
    stored = store.jira_project_by_key(conn, token)
    if stored is not None:
        return stored
    for r in store.list_containers(conn, "jira"):
        if synth.jira_project_key(r["name"]) == token.upper() or r["name"].lower() == token.lower():
            return r["name"]
    return None


def _resolve_jira_key(request: Request, conn, key: str, ids):
    """One issue by its served key or its numeric id, ACL-scoped (see store.jira_by_key and
    store.jira_by_numeric_id).

    The key is matched whole, as it is stored. Resolving it in parts instead — split the key, map
    the prefix to a project through `_jira_container_for_key`, look the suffix up scoped to it —
    lets that function's three-way tolerance into the ISSUE-KEY namespace. The tolerance is a
    deliberate and correct affordance for the JQL project TOKEN, where real Jira pickers accept a
    key OR a name, but here it makes `payments-7` resolve to `PAY-7`'s issue and issue-key lookup
    case-insensitive. Matching the stored key directly has no seam for either to enter."""
    row = store.jira_by_key(conn, key, visible_ids=ids)
    if row is None and key.isascii() and key.isdigit():
        return store.jira_by_numeric_id(conn, key, visible_ids=ids)
    return row


def _require_issue(request: Request, conn, key: str):
    """The issue behind ``key`` the caller can see, or Jira's 404 for it, which reports an issue the
    caller cannot see as one that does not exist."""
    row = _resolve_jira_key(request, conn, key, auth.visible_ids(request, _jira_caller(request)))
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Issue does not exist or you do not have permission to see it.",
        )
    return row


@router.get(
    "/rest/api/2/serverInfo",
    response_model=JiraServerInfo,
    response_model_exclude_none=True,
)  # jira PyPI client probes this on connect
@router.get(
    "/rest/api/3/serverInfo",
    response_model=JiraServerInfo,
    response_model_exclude_none=True,
)
async def jira_server_info(request: Request):
    """The members Jira Cloud answers, the same on v2 and v3 (measured 2026-10-03, 2026-10-05 and
    2026-10-07).

    A signed-in caller gets fifteen, in the order `JiraServerInfo` declares them. The anonymous
    caller `_jira_caller` returns gets all of them but `serverTime`: measured with no
    `Authorization` header, a failed `email:api_token` pair, an empty password, a Basic value that
    is not base64 and an unknown scheme. `serverTime` is milliseconds and a `+HHMM` offset, the form
    of `buildDate` (`synth.jira_datetime`). The offset is the tenant's own and not read from
    `serverTimeZone`: the tenant measured wrote both members with `+0900` while `serverTimeZone`
    answered `Etc/UTC`. This server writes `+0000`. The four display URLs are the site's URL,
    `serverTitle` is `Jira`, and the version and build number are the ones that site served.
    `scmInfo` is a synthesized 40-hex commit id, and `buildDate` a day before `serverTime`, since a
    build precedes the server running it."""
    site = _site(request)
    ts = synth.epoch("serverInfo")
    body = {
        "baseUrl": site,
        "displayUrl": site,
        "displayUrlServicedeskHelpCenter": site,
        "displayUrlCSMHelpSeeker": site,
        "displayUrlConfluence": site,
        "version": "1001.0.0-SNAPSHOT",
        "versionNumbers": [1001, 0, 0],
        "deploymentType": "Cloud",
        "buildNumber": 100294,
        "buildDate": synth.jira_datetime(ts - 86400),
        "serverTime": synth.jira_datetime(ts),
        "scmInfo": hashlib.sha1(b"serverInfo").hexdigest(),
        "serverTitle": "Jira",
        "defaultLocale": {"locale": "en_US"},
        "serverTimeZone": "Etc/UTC",
    }
    if _jira_caller(request).is_anonymous:
        del body["serverTime"]
    return body


def _reachable_projects(conn, ids) -> list:
    """The projects the caller can reach, as container rows.

    A project is listed when the caller can see an issue in it. Backlot's ACL grants per document
    rather than per project, so there is nothing else to read "can browse this project" off.

    Measured for the ANONYMOUS caller, on 2026-09-04: `project/search` answers 200 with an empty
    `values` on a site whose projects are all private (brekkylab.atlassian.net) and with the
    public ones on a site that has them (ecosystem.atlassian.net). The same rule is applied to a
    scoped caller, which is NOT measured — real Jira lists a project by its browse permission, and
    a project can carry one while showing the caller no issue, so a listing there can hold a
    project this one drops. Measuring it needs two accounts and a per-project permission scheme on
    a live site. What the rule does rule out is the unfiltered listing, which handed every project
    to a caller who can open nothing in any of them.
    """
    rows = store.list_containers(conn, "jira")
    if ids is None:
        return rows
    return [r for r in rows if store.has_visible_document(conn, "jira", r["name"], ids)]


# The fields `project/search` sorts by, in the order its 400 names them (Jira Cloud, 2026-10-16).
_JIRA_PROJECT_ORDER_FIELDS = (
    "name",
    "key",
    "owner",
    "category",
    "issueCount",
    "lastIssueUpdatedTime",
    "archivedDate",
    "deletedDate",
)
_JIRA_PROJECT_ORDER_LOWER = frozenset(name.lower() for name in _JIRA_PROJECT_ORDER_FIELDS)
# The four characters past ASCII that Java's `equalsIgnoreCase` takes for an ASCII letter.
_JAVA_ASCII_CASE_ALIASES = str.maketrans(
    {"\u0130": "i", "\u0131": "i", "\u017f": "s", "\u212a": "k"}
)


def _jira_project_order(raw: str) -> None:
    """Jira's 400 when ``project/search`` ``orderBy`` names no field it sorts by.

    Measured against Jira Cloud on 2026-10-16. Only an empty value is read as no `orderBy`. What
    Java's `String.trim` removes (:data:`_JAVA_TRIMMED`) comes off both ends, then one leading
    ``+`` or ``-``, then the trim again: `%01key` and `-%09key` are 200, `%C2%A0key` and
    `%E2%80%83key` are the 400. The field is compared the way `equalsIgnoreCase` compares, so
    `ıssueCount`, `İssueCount` and `iſsueCount` are 200 and `ißueCount` is the 400. The 400 names
    what is left, which a value holding only a sign or whitespace leaves empty: `-bogus` is named
    `bogus`, `--key` `-key`, and `-` or `%20` the empty string.
    """
    if raw == "":
        return
    field = raw.strip(_JAVA_TRIMMED)
    if field[:1] in ("+", "-"):
        field = field[1:].strip(_JAVA_TRIMMED)
    if field.translate(_JAVA_ASCII_CASE_ALIASES).lower() in _JIRA_PROJECT_ORDER_LOWER:
        return
    allowed = ", ".join(_JIRA_PROJECT_ORDER_FIELDS)
    raise errors_atlassian.AtlassianError(
        400,
        {
            "errorMessages": [
                f"The field to order by should be one of [{allowed}]. Instead, it was: {field}."
            ],
            "errors": {},
        },
    )


# The `projectTypeKey` of every project served here.
_JIRA_PROJECT_TYPE = "software"
# What `typeKey` trims from each value: the 25 code points of Unicode's White_Space property, each
# of which kept the project on 2026-10-25 in front of `software` and after it, where U+0001,
# U+001C to U+001F, U+007F, U+00AD, U+180E, U+200B and U+FEFF kept none. `str.strip()` would take
# U+001C to U+001F as well.
_JIRA_TYPE_KEY_SPACE = "\t\n\x0b\x0c\r \x85\xa0\u1680\u2028\u2029\u202f\u205f\u3000" + "".join(
    map(chr, range(0x2000, 0x200B))
)


def _jira_project_kept(request: Request, name: str, key: str, category: int | None) -> bool:
    """Whether ``project/search`` keeps this project under `query`, `keys`, `typeKey` and the
    ``category`` its `categoryId` converted to.

    Measured against Jira Cloud on 2026-10-09 and 2026-10-22, on a site of one project. `query`
    keeps a project whose name or key holds the text, both lower-cased and nothing trimmed: the
    first three letters of the name, a fragment of the key in either case and the name with a
    KELVIN SIGN (U+212A) for its `k` kept that project, where `zzqq`, a space or a tab in front of
    the name's first letters, `bre*` and the key with a LONG S (U+017F) for its `s` kept none.
    `keys` keeps a project whose key is one of the values as sent, so `keys=PA&keys=PB` keeps
    either: the key kept the project, and the key in lower case, with a space in front, `NOPE` and
    `<KEY>,NOPE` kept none. A lone empty `keys=` filters nothing, where `keys=&keys=` kept none.
    `typeKey` is a list split at its commas over every value sent, each trimmed of
    :data:`_JIRA_TYPE_KEY_SPACE` and lower-cased:
    `software`, `Software`, `%20software`, `%C2%A0software`, `software,business` and
    `typeKey=business&typeKey=software` kept the project, `business`, `bogus`, `%01software` and
    `ſoftware` kept none, and an empty value or `,` filtered nothing, all at 200. A project here
    is software and carries no category, so any `categoryId` that converts keeps none
    (`categoryId=99`, `0` and `-1` kept none).
    """
    query = request.query_params.get("query")
    if query:
        lowered = query.lower()
        if lowered not in name.lower() and lowered not in key.lower():
            return False
    keys = request.query_params.getlist("keys")
    if keys and keys != [""] and key not in keys:
        return False
    types = {
        part.strip(_JIRA_TYPE_KEY_SPACE).lower()
        for value in request.query_params.getlist("typeKey")
        for part in value.split(",")
    }
    if types - {""} and _JIRA_PROJECT_TYPE not in types:
        return False
    return category is None


@router.get(
    "/rest/api/3/project/search",
    openapi_extra={
        "parameters": [
            qp("startAt", "integer"),
            qp("maxResults", "integer"),
            qp("query"),
            qp("keys"),
            qp("typeKey"),
            qp("categoryId", "integer"),
        ]
    },
)
async def jira_project_search(request: Request):
    # Jira Cloud, measured 2026-10-16: integer conversion precedes orderBy;
    # startAt is a long floored at zero, and maxResults is clamped to 1..100.
    start = max(0, _int_param(request, "startAt", 0, width=JAVA_LONG))
    limit = min(100, max(1, _int_param(request, "maxResults", 50)))
    # Converted after `startAt` and `maxResults` and before `orderBy` is read, as real converts it.
    category = _int_param(request, "categoryId", None, width=JAVA_LONG)
    # Read for the refusal above. The sort itself is not served, so `orderBy` stays undeclared:
    # `qp` is for a parameter Backlot honours, and advertising this one would offer a client an
    # order the listing never applies. `backlot diff --source jira` keeps the gap.
    raw_order = _str_param(request, "orderBy")
    if raw_order is not None:
        _jira_project_order(raw_order)
    conn = auth.conn(request)
    caller = _jira_caller(request)
    ids = auth.visible_ids(request, caller)
    values = []
    for r in _reachable_projects(conn, ids):
        key = _project_key(conn, r["name"])
        if not _jira_project_kept(request, r["name"], key, category):
            continue
        values.append(
            {
                "id": str(synth.github_user_id(r["name"])),
                "key": key,
                "name": r["name"],
                "projectTypeKey": _JIRA_PROJECT_TYPE,
                "simplified": False,
                "style": "classic",
                "isPrivate": False,
                "avatarUrls": synth.avatar_urls("proj:" + key),
                "self": f"{_site(request)}/rest/api/3/project/{key}",
            }
        )
    total = len(values)
    page = values[start : start + limit]
    answer = {"self": _project_search_link(request, start, limit)}
    if start + len(page) < total:
        answer["nextPage"] = _project_search_link(request, start + limit, limit)
    return answer | {
        "maxResults": limit,
        "startAt": start,
        "total": total,
        "isLast": start + len(page) >= total,
        "values": page,
    }


def _project_search_link(request: Request, start: int, limit: int) -> str:
    """`project/search`'s `self` at ``start``, and its `nextPage` at the page after: every parameter
    the request sent but `startAt` and `maxResults`, each value of a repeated one, by its name and
    value as sent (`a%2Bb`, `a+b`, `b%6Fgus`; a bare `bogus` as `bogus=`, and `=x` as `x=`), then
    `maxResults` and `startAt` as read. Real lists them in the order a `java.util.HashMap` holding
    them in that order iterates: by the bucket each name's `String.hashCode` spreads to, over
    sixteen buckets doubled while the map is more than three quarters full, and in the order put
    within a bucket (`maxResults=1&Zeta=1&alpha=2` links `Zeta=1&maxResults=1&alpha=2&startAt=0`).
    Measured 2026-10-17 on a site of one project and on ecosystem.atlassian.net (100 projects,
    anonymous), where `nextPage` came with every page but the last."""
    params: dict[str, list[str]] = {}
    for piece in request.scope["query_string"].decode("latin-1").split("&"):
        name, _, value = piece.partition("=")
        if not name:
            name, value = value, ""
        if name and name not in ("startAt", "maxResults"):
            params.setdefault(name, []).append(value)
    params["maxResults"], params["startAt"] = [str(limit)], [str(start)]
    capacity = 16
    while len(params) > capacity * 3 // 4:
        capacity *= 2

    def bucket(name: str) -> int:
        code = 0
        for char in name:
            code = (31 * code + ord(char)) & 0xFFFFFFFF
        return (code ^ (code >> 16)) & (capacity - 1)

    query = "&".join(
        f"{name}={value}" for name in sorted(params, key=bucket) for value in params[name]
    )
    return f"{_site(request)}/rest/api/3/project/search?{query}"


def _jira_project_id(container: str) -> str:
    """A project's id: the one `project/search` lists and an issue's `fields.project.id` carries."""
    return str(synth.github_user_id(container))


def _require_project(request: Request, conn, key: str) -> str:
    """The container behind a project key or id the caller can reach, or Jira's own 404 for it.

    An unreachable project is reported as an absent one, the way `issue/{key}` reports an issue
    the caller cannot see.

    The segment is an `id` when it is one to eighteen ASCII digits with no leading zero, and a
    `key` otherwise; the 404 calls it by that word, and an id finds the project
    (:func:`_jira_project_id`) as its key does. Measured against Jira Cloud on 2026-10-14, with and
    without a credential, on `role` and `role/10002`: `1`, `10002` and `999999999999999999` are
    ids; `0`, `007`, `-1`, `1000000000000000000` (nineteen digits, still a Java `long`) and `1２` (a
    full-width `２`) are keys. A credentialled caller got the same answer from a project's id as from
    its key, on `role`, `role/10002` and `POST role/99999`, on 2026-10-14 and 2026-10-15.

    A key is the project's own as written: on 2026-10-17 the key in lower case or with only its
    first letter in upper case, and the project's name in either case, were each the 404 by key,
    with and without a credential, on `role` and `role/10002`.
    """
    ids = auth.visible_ids(request, _jira_caller(request))
    kind = "id" if re.fullmatch(r"[1-9][0-9]{0,17}", key) else "key"
    rows = store.list_containers(conn, "jira")
    if kind == "id":
        container = next((r["name"] for r in rows if _jira_project_id(r["name"]) == key), None)
    else:
        container = next((r["name"] for r in rows if _project_key(conn, r["name"]) == key), None)
    # The `ids is None` short-circuit mirrors `github._repo_visible`, where it is load-bearing: a
    # `subtype: repo` record creates a container holding no document, and the admin keeps the repo
    # as soon as that record exists. No Jira record can do that — `project` is required on every
    # one of them and none omits the issue — so here it only keeps the two readers alike.
    if container is not None and (
        ids is None or store.has_visible_document(conn, "jira", container, ids)
    ):
        return container
    raise HTTPException(status_code=404, detail=f"No project could be found with {kind} '{key}'.")


def _require_project_admin(request: Request, conn, key: str) -> str:
    """:func:`_require_project`, then the 401 ``errors.atlassian.not_a_project_admin`` describes,
    for every caller but the admin.

    The 401 comes after the role id's 400 (:func:`_role_id`) and the project's 404, and ahead of
    whatever the role id names: on 2026-10-15 real gave the 400 for `abc`, `0xİ` and a blank id
    ahead of it. Anonymous sees no project here, so it always gets the 404. Where anonymous can see
    the project, real's 401 lists a second message after the one ``not_a_project_admin`` carries,
    `You are not authorized to perform this operation.  Please log in.`, 152 bytes in all: a Jira
    Cloud site whose project `AA` is public answered it on `role` and `role/10002` by the key and by
    the id, and on `role/99999` by the key, on 2026-10-16.
    """
    container = _require_project(request, conn, key)
    if not _jira_caller(request).is_admin:
        raise errors_atlassian.not_a_project_admin(quote(_site(request), safe=""))
    return container


#: The id of the one role a project has here.
_JIRA_ROLE = 10002

#: The one role's description, this server's own: the vendor's document gives "A project role that
#: represents developers in a project" for its `Developers`, and a site's roles are its own.
_JIRA_ROLE_DESCRIPTION = "A project role that represents users in a project"


def _jira_role_link(request: Request, container: str) -> str:
    """The one role's `self`, by the project's id where the vendor's document shows its key, however
    the request names the project. Measured with an administrator's credential for a project keyed
    `SCRUM`: real's role list linked `…/project/10000/role/10005` on 2026-10-14, and its read of
    `project/SCRUM/role/10005` gave that same `self` on 2026-10-16."""
    return f"{_site(request)}/rest/api/3/project/{_jira_project_id(container)}/role/{_JIRA_ROLE}"


@router.get("/rest/api/3/project/{key}/role")
async def jira_project_roles(key: str, request: Request):
    container = _require_project_admin(request, auth.conn(request), key)
    return {"Users": _jira_role_link(request, container)}


def _java_digit(ch: str, radix: int) -> int | None:
    """``ch`` as Java's `Character.digit` reads it in ``radix``: a decimal digit of any script in
    the Basic Multilingual Plane (`١`, `１`), and in hexadecimal `a` to `f` in either case,
    full-width ones included (`Ａ`); a character past U+FFFF is two `char`s to Java, neither of them
    a digit."""
    if ord(ch) > 0xFFFF:
        return None
    decimal = unicodedata.decimal(ch, None)
    if decimal is not None:
        return decimal
    for first in ("a", "A", "\uff41", "\uff21"):
        if radix == 16 and ord(first) <= ord(ch) <= ord(first) + 5:
            return ord(ch) - ord(first) + 10
    return None


def _java_long(raw: str) -> int | None:
    """``raw`` as Spring reads a `long` path variable, or None where it cannot: whitespace removed
    (``errors.atlassian.strip_java_whitespace``), then hexadecimal after `0x`, `0X` or `#` with an
    optional `-` in front, otherwise decimal with an optional sign, each digit as
    :func:`_java_digit` reads it, within a `long`. Measured on `project/{key}/role/{id}` 2026-10-13:
    `-%231`, `0x１` and a 1 behind 4400 zeros read, `+0x1` and `0x-1` do not."""
    text = errors_atlassian.strip_java_whitespace(raw)
    negative = text.startswith("-")
    rest = text[1:] if negative else text
    if rest.startswith(("0x", "0X", "#")):
        radix, digits = 16, rest[1:] if rest.startswith("#") else rest[2:]
    else:
        radix, digits = 10, (text[1:] if text.startswith(("+", "-")) else text)
    if not digits:
        return None
    value = 0
    for ch in digits:
        digit = _java_digit(ch, radix)
        if digit is None:
            return None
        value = value * radix + digit
        if value > 2**63:
            return None
    value = -value if negative else value
    return value if -(2**63) <= value < 2**63 else None


def _parse_long(raw: str) -> int | None:
    """``raw`` as Java's `Long.parseLong` reads it, or None where it throws: an optional sign, then
    decimal digits as :func:`_java_digit` reads them, within a `long`. Nothing is trimmed and there
    is no hexadecimal, unlike :func:`_java_long`."""
    negative = raw.startswith("-")
    digits = raw[1:] if raw[:1] in ("+", "-") else raw
    if not digits:
        return None
    value = 0
    for ch in digits:
        digit = _java_digit(ch, 10)
        if digit is None:
            return None
        value = value * 10 + digit
    value = -value if negative else value
    return value if -(2**63) <= value < 2**63 else None


def _content_id(raw: str) -> tuple[int, bool]:
    """The ``content_id`` path variable as Confluence's `ContentId` reads it, and whether it was
    sent as an attachment's, or the 400 it answers.

    An id is :func:`_parse_long` of the value, or of what follows a leading `att`, which marks an
    attachment's (:func:`backlot.errors.atlassian.content_not_found` says what that changes).
    Measured on Confluence Cloud, 2026-10-16 and 2026-10-21: `05`, `٥` and `att5` are 5, and
    `0x10`, a space beside the digits, `Att5` and `attabc` are the 400.

    Every route under `content/{id}` reads it in its body rather than declaring it in the
    signature: Spring converts the path variable after the credential's 403 and before the route's
    own parameter refusals (`content/abc/child/page?limit=abc`, `?limit=-1` and
    `content/abc/label?prefix=bogus` are this 400), and a declared type is checked before the
    route runs.
    """
    attachment = raw.startswith("att")
    content_id = _parse_long(raw[3:] if attachment else raw)
    if content_id is None:
        raise errors_atlassian.content_id_not_convertible(raw)
    return content_id, attachment


def _role_id(request: Request, raw: str) -> int:
    """A project role's id as real reads it (:func:`_java_long`), or the 400 it answers: one naming
    the variable as missing where removing the whitespace leaves nothing
    (``errors.atlassian.missing_path_variable``) and one naming the value it could not convert
    otherwise. Real answers either ahead of the project's 404 and, on a write, behind its 415, for
    every caller (2026-10-13)."""
    if errors_atlassian.strip_java_whitespace(raw) == "":
        raise errors_atlassian.missing_path_variable(_echoed_path(request), "id")
    role = _java_long(raw)
    if role is None:
        raise errors_atlassian.integer_conversion_failure(_echoed_path(request), "id", [raw])
    return role


@router.get("/rest/api/3/project/{key}/role/{role_id}")
async def jira_project_role(
    key: str, request: Request, role_id: str = PathParameter(json_schema_extra={"type": "integer"})
):
    """One project role, its id read as :func:`_role_id` reads it; the schema still declares the
    integer the vendor's document does. An id other than :data:`_JIRA_ROLE` names no role of the
    project (``errors.atlassian.no_such_role``).

    The role carries the six members real's did for an administrator on 2026-10-14: `self`
    (:func:`_jira_role_link`), `name`, `id`, `description`, `actors` and a `scope` naming the
    project by its id alone. An actor's `id` counts up from 10000, as real's user actors' did
    (10000, 10002, …); here it is the member's place in the role."""
    role = _role_id(request, role_id)
    conn = auth.conn(request)
    container = _require_project_admin(request, conn, key)
    if role != _JIRA_ROLE:
        raise errors_atlassian.no_such_role()
    members = store.group_members(conn, store.get_container(conn, "jira", container)["group_id"])
    actors = [
        {
            "id": 10000 + n,
            "displayName": m["display_name"],
            "type": "atlassian-user-role-actor",
            "actorUser": {"accountId": synth.atlassian_account_id(m["email"])},
        }
        for n, m in enumerate(members)
    ]
    return {
        "self": _jira_role_link(request, container),
        "name": "Users",
        "id": role,
        "description": _JIRA_ROLE_DESCRIPTION,
        "actors": actors,
        "scope": {"type": "PROJECT", "project": {"id": _jira_project_id(container)}},
    }


# The `SearchAndReconcileRequestBean` fields real's POST form accepts, measured 2026-09-18 against
# Jira Cloud (`failFast` 2026-10-12): `fields`, `fieldsByKeys`, `failFast`, `expand`, `properties`
# and `reconcileIssues` are all read without a refusal, alongside the three Backlot itself acts on.
# Backlot implements none of the six, the same gap `_P_EXPAND`'s comment notes for the comment
# read, but they must stay off the unknown-property refusal below or that refusal would catch a
# client using them.
_JIRA_SEARCH_BODY_KEYS = frozenset(
    {
        "jql",
        "maxResults",
        "nextPageToken",
        "fields",
        "fieldsByKeys",
        "failFast",
        "expand",
        "properties",
        "reconcileIssues",
    }
)
# search/jql's own maxResults range on both methods; see errors_atlassian.max_results_out_of_range
# for the measurement. Unlike a type-conversion failure, this range is not either parser's (Jackson
# reads the body, Spring binds the query string) — it belongs to the operation itself, which is why
# it applies identically to both placements.
_JIRA_SEARCH_MAX_RESULTS_RANGE = (1, 5000)


def _jira_max_results_was_sent(request: Request) -> bool:
    """Whether the GET form's `maxResults` is one real would attempt to convert at all, mirroring
    the cases :func:`_int_param` itself folds into "absent": no parameter, an empty value, or one
    that is whitespace after Java's own whitespace stripping. `jira_search` uses this to keep
    Backlot's own default — never something the caller sent — off the 1-5000 range check: a
    deployment's `default_page_size` is not a value the vendor ever validated.
    """
    values = request.query_params.getlist("maxResults")
    if not values or values[0] == "":
        return False
    return errors_atlassian.strip_java_whitespace(values[0]) != ""


def _jira_search_max_results(value) -> int:
    """A POST body's `maxResults`, once the caller has established the key is present, coerced the
    way Jackson coerces it into the bean's int field. Presence, not this function, is what decides
    whether the 1-5000 range in :data:`_JIRA_SEARCH_MAX_RESULTS_RANGE` applies at all — see
    :func:`_jira_max_results_was_sent` for why the GET form needs its own version of that same
    question.

    A JSON `null` is NOT absence here as it is for `jql`'s own null handling in this endpoint:
    Jackson reads a null int field as `0`, which then fails the range check like any other
    out-of-bounds value (measured 2026-09-18, `{"maxResults": null}` draws the same refusal as
    `{"maxResults": 0}`).

    A digit string and a float are read fine — `"5"` as `5`, `1.5` truncated to `1` — where a
    non-numeral string and a boolean are refused with the body-wide sentence a body Jackson cannot
    deserialize at all gets (:data:`errors_atlassian.BODY_NOT_AN_OBJECT`): `bool` is checked before
    `int`/`float` because Python's `int` is their common base class.

    A number must also fit the bean's Java `int`. 2147483647 and -2147483648 bind and reach the
    1-5000 range check, where 2147483648, -2147483649, 2147483647.5, -2147483648.5 and the infinite
    float `1e400` reads as are the body-wide sentence (measured 2026-10-06, the fractions
    2026-10-09). A float is compared with the bounds before it is truncated, since
    `int(2147483647.5)` is in range.
    """
    if isinstance(value, bool):
        raise errors_atlassian.body_not_read(errors_atlassian.BODY_NOT_AN_OBJECT)
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        n = value
    elif isinstance(value, str):
        try:
            n = int(value)
        except ValueError:
            raise errors_atlassian.body_not_read(errors_atlassian.BODY_NOT_AN_OBJECT) from None
    else:
        raise errors_atlassian.body_not_read(errors_atlassian.BODY_NOT_AN_OBJECT)
    if not -(2**31) <= n <= 2**31 - 1:
        raise errors_atlassian.body_not_read(errors_atlassian.BODY_NOT_AN_OBJECT)
    return int(n)


_JIRA_BOOLEAN_TEXTS = frozenset({"", "null", "true", "True", "TRUE", "false", "False", "FALSE"})


def _jira_bean_boolean_ok(value) -> bool:
    """Whether a `fieldsByKeys` or a `failFast` binds to the bean's boolean. Jira Cloud bound
    `fieldsByKeys` this way on 2026-10-10 and 2026-10-11, and the two alike, value for value, on
    2026-10-13. `null`, a boolean and any integer bind. A string binds when, trimmed (see
    :data:`_JAVA_TRIMMED`), it is empty, `null`, or `true` or `false` in lower, title or upper case:
    `" TRUE "`, `"False"`, `""` and `"null"` bind, where `"tRuE"`, `"NULL"`, `"1"`, `"yes"` and a
    `true` behind a no-break space do not. A float, a list and an object do not bind.
    """
    if value is None or isinstance(value, int):
        return True
    if isinstance(value, str):
        return value.strip(_JAVA_TRIMMED) in _JIRA_BOOLEAN_TEXTS
    return False


def _jira_reconcile_issue_ok(value) -> bool:
    """Whether one `reconcileIssues` element binds to the bean's Java `Long`, as measured against
    Jira Cloud on 2026-10-10 and 2026-10-11. A null binds, as a null `Long`, where a bare null
    `reconcileIssues` does not (see `jira_search`). An integer binds from -2**63 up to 2**63 - 1. A
    float binds from -2**63 up to 2**63, both ends included, and is truncated: `1.5`, `9.2e18` and
    `9223372036854775807.0` (2**63 as a double) bind, where `9.3e18`, `1e19` and the next double
    past either end do not. A string binds when, trimmed (see :data:`_JAVA_TRIMMED`), it is empty,
    `null`, or at most 1000 characters of a sign and decimal digits, in any script up to U+FFFF,
    naming an integer in range: `" 1"`, `"+1"`, `"٥"` and a `1` behind 999 zeros bind, where
    `"1.5"`, `"1_000"`, `"0x10"`, `"𝟓"`, a `1` behind 1000 zeros and a `1` behind a no-break space
    do not. A boolean, a list and an object do not bind.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        return -(2**63) <= value <= 2**63
    if isinstance(value, str):
        text = value.strip(_JAVA_TRIMMED)
        if text in ("", "null"):
            return True
        if len(text) > 1000 or not re.fullmatch(r"[+-]?\d+", text) or max(map(ord, text)) > 0xFFFF:
            return False
        value = int(text)
    return isinstance(value, int) and -(2**63) <= value < 2**63


@router.api_route(
    "/rest/api/2/search/jql",
    methods=["GET", "POST"],  # atlassian-python-api uses v2
    response_model=JiraSearchResult,
    openapi_extra=_X_JIRA_SEARCH,
)
@router.api_route(
    "/rest/api/3/search/jql",
    methods=["GET", "POST"],
    response_model=JiraSearchResult,
    openapi_extra=_X_JIRA_SEARCH,
)
async def jira_search(request: Request):
    conn = auth.conn(request)
    caller = _jira_caller(request)
    ids = auth.visible_ids(request, caller)
    default_size = get_settings().default_page_size
    if request.method == "POST":
        body = await _jira_search_body(request)
        if not _JIRA_SEARCH_BODY_KEYS.issuperset(body):
            # The body deserializes into a fixed bean, and a property it does not declare is a
            # failure, not surplus (measured 2026-09-18, `{"bogus": 1}` and `{"startAt": 0}` —
            # `startAt` belongs to the older `search` operation, not this one). Same sentence a
            # body Jackson cannot deserialize at all gets, below.
            raise errors_atlassian.body_not_read(errors_atlassian.BODY_NOT_AN_OBJECT)
        raw_jql = body.get("jql")
        raw_token = body.get("nextPageToken")
        if isinstance(raw_jql, (list, dict)) or isinstance(raw_token, (list, dict)):
            # Measured 2026-09-18 on both fields: a `jql` or `nextPageToken` shaped as a list or an
            # object is the same refusal as an unknown field, where a scalar (a number, a bool)
            # instead reaches its own handling below — the JQL parser for `jql`, which
            # `_str_param`'s docstring already documents as lenient here, and the token decoder for
            # `nextPageToken`.
            raise errors_atlassian.body_not_read(errors_atlassian.BODY_NOT_AN_OBJECT)
        raw_fields = body.get("fields")
        if raw_fields is not None and (
            not isinstance(raw_fields, list)
            or any(isinstance(field, (list, dict)) for field in raw_fields)
        ):
            # `fields` is declared by the request bean even though Backlot does not otherwise use
            # it. Real Jira still binds its value: a list of scalars is served (`["id"]`, `[1]`,
            # `[null]`, `[true]`), while an object, a bare string, a nested list or a list holding
            # an object is a body-wide invalid-payload refusal (measured 2026-10-06; the lists of
            # scalars and the bare string 2026-10-09).
            raise errors_atlassian.body_not_read(errors_atlassian.BODY_NOT_AN_OBJECT)
        # The other five members Backlot does not act on still have their values bound by the bean
        # (measured 2026-10-10 and 2026-10-11, `failFast` 2026-10-13): `expand` takes a scalar,
        # `properties` a list of scalars, `reconcileIssues` a list of what
        # `_jira_reconcile_issue_ok` accepts, and `fieldsByKeys` and `failFast` what
        # `_jira_bean_boolean_ok` accepts. Anything else is the body-wide refusal, which real gives
        # ahead of the 500s a null `properties` element and a null `reconcileIssues` draw further
        # down.
        if isinstance(body.get("expand"), (list, dict)):
            raise errors_atlassian.body_not_read(errors_atlassian.BODY_NOT_AN_OBJECT)
        if not _jira_bean_boolean_ok(body.get("fieldsByKeys")):
            raise errors_atlassian.body_not_read(errors_atlassian.BODY_NOT_AN_OBJECT)
        if not _jira_bean_boolean_ok(body.get("failFast")):
            raise errors_atlassian.body_not_read(errors_atlassian.BODY_NOT_AN_OBJECT)
        raw_properties = body.get("properties")
        if raw_properties is not None and (
            not isinstance(raw_properties, list)
            or any(isinstance(prop, (list, dict)) for prop in raw_properties)
        ):
            raise errors_atlassian.body_not_read(errors_atlassian.BODY_NOT_AN_OBJECT)
        raw_reconcile = body.get("reconcileIssues")
        if raw_reconcile is not None and (
            not isinstance(raw_reconcile, list)
            or not all(_jira_reconcile_issue_ok(issue) for issue in raw_reconcile)
        ):
            raise errors_atlassian.body_not_read(errors_atlassian.BODY_NOT_AN_OBJECT)
        # A JSON null is the parameter unsent, not the string "None": real answers
        # `{"jql": null}` with the same unbounded-JQL refusal it gives `{}` (measured 2026-09-16).
        jql = "" if raw_jql is None else str(raw_jql)
        max_results_sent = "maxResults" in body
        limit = _jira_search_max_results(body["maxResults"]) if max_results_sent else default_size
        if raw_properties is not None and None in raw_properties:
            # Real answers this after the binding refusals above (measured beside `maxResults`,
            # `reconcileIssues`, `expand`, `jql` and an unknown member) and ahead of every refusal
            # below (measured 2026-10-11).
            raise errors_atlassian.properties_null_element(raw_properties.index(None))
        token = raw_token
        reconcile_null = "reconcileIssues" in body and raw_reconcile is None
        reconcile_count = len(raw_reconcile) if isinstance(raw_reconcile, list) else 0
    else:
        jql = _str_param(request, "jql") or ""
        max_results_sent = _jira_max_results_was_sent(request)
        limit = _int_param(request, "maxResults", default_size)
        # Each conversion is refused ahead of the page token, the range and the unbounded JQL:
        # `maxResults` first, then `fieldsByKeys`, then `failFast`, then `reconcileIssues`,
        # whichever order the query sends them in (measured 2026-10-12).
        _bool_param(request, "fieldsByKeys")
        _bool_param(request, "failFast")
        _jira_long_list_param(request, "reconcileIssues")
        token = _str_param(request, "nextPageToken")
        reconcile_null = False
        reconcile_count = len(request.query_params.getlist("reconcileIssues"))
    # An undecodable token is refused before the project clause is resolved (measured
    # 2026-09-16; see test_jira_search_refuses_a_page_token_it_cannot_decode).
    offset = decode_cursor_or_none(None if token is None else str(token))
    if offset is None:
        raise errors_atlassian.bad_page_token()
    if reconcile_count > 50:
        # Real counts every value it was sent, a blank one on GET and a null element on POST
        # included, and answers this after the page token and ahead of the range and the unbounded
        # JQL (measured 2026-10-12, the null elements 2026-10-13).
        raise errors_atlassian.reconcile_issues_too_many()
    if max_results_sent and not (
        _JIRA_SEARCH_MAX_RESULTS_RANGE[0] <= limit <= _JIRA_SEARCH_MAX_RESULTS_RANGE[1]
    ):
        # Checked after the page token and before the unbounded-JQL refusal, both measured too
        # (2026-09-18).
        raise errors_atlassian.max_results_out_of_range()
    # No `jql` at all is refused rather than answered as the unfiltered corpus (measured
    # 2026-09-16; see test_jira_search_refuses_no_jql_at_all).
    if not jql.strip():
        raise errors_atlassian.unbounded_jql()
    if reconcile_null:
        # Real answers this after the page token, the range and the JQL, and before it looks up
        # the project (measured 2026-10-11).
        raise errors_atlassian.reconcile_issues_null()
    container = _project_from_jql(conn, jql, request)
    if container is _JIRA_PROJECT_UNRESOLVED:
        # a project= clause was present but didn't match any project: strict 0 matches, not
        # the unfiltered corpus.
        return {"issues": [], "isLast": True}
    term = _text_from_jql(jql)
    if term:  # text ~ / summary ~ / description ~ → full-text search (FTS), scoped to project
        total = store.count_search(conn, term, "jira", ids, container=container)
        rows = store.search_documents(
            conn, term, "jira", ids, limit=limit, offset=offset, container=container
        )
    else:
        total = store.count_documents(conn, "jira", container, ids)
        rows = store.list_documents(conn, "jira", container, ids, limit=limit, offset=offset)
    issues = [_jira_issue(conn, request, r, fields_only=True) for r in rows]
    token = next_page_token(offset, len(rows), total)
    return {
        "issues": issues,
        "isLast": token is None,
        **({"nextPageToken": token} if token else {}),
    }


@router.get(
    "/rest/api/2/issue/{key}",  # atlassian-python-api uses v2 for issue fetch
    response_model=JiraIssue,
    openapi_extra=_P_EXPAND,
)
@router.get("/rest/api/3/issue/{key}", response_model=JiraIssue, openapi_extra=_P_EXPAND)
async def jira_get_issue(key: str, request: Request):
    conn = auth.conn(request)
    row = _require_issue(request, conn, key)
    return _jira_issue(conn, request, row, expand=_str_param(request, "expand", ""))


@router.get(
    "/rest/api/2/issue/{key}/comment",
    response_model=JiraComments,
    openapi_extra=_P_JIRA_COMMENTS,
)
@router.get(
    "/rest/api/3/issue/{key}/comment",
    response_model=JiraComments,
    openapi_extra=_P_JIRA_COMMENTS,
)
async def jira_issue_comments(key: str, request: Request):
    """One PAGE of an issue's comments.

    `total` counts the whole collection while `startAt` and `maxResults` describe the slice, which
    is what makes the envelope a page rather than a restatement of its own length.

    Ordering is by `orderBy`, which real Jira accepts as `created` with an optional single leading
    `+`/`-`, case-insensitively and ignoring whitespace — see :func:`_jira_order_desc` — and
    answers 400 for anything else. Reproduced, because accepting one silently would serve corpus
    order to a client that asked for something else with nothing in the response to say the sort
    was dropped — and would pass here while failing against Jira. Its own message is localised to
    the account's language, so the wording is not reproduced, only the refusal.
    """
    # BEFORE the issue is resolved. Measured 2026-09-10: a bad `orderBy` on a key that does
    # not exist is 400 on real Jira where the same key without the parameter is 404, so the
    # parameter is checked first. It separates nothing a caller could not already tell — the
    # 400 is identical for a key that exists, one hidden from the caller, and one that never
    # existed.
    #
    # An ABSENT `orderBy` is "not asked" and leaves the corpus's own order alone. What real Jira
    # returns without one is not established: its REST intro says responses are "listed in
    # ascending order by default", but that is a general statement its own operations contradict
    # (project classification: "If not provided, values will not be sorted"), and the site
    # available for measuring had no issue carrying a comment. Sorting on that would be picking a
    # default, not reproducing one.
    #
    # The integers are read here too, and BEFORE `orderBy`, because the binder outranks both. All
    # measured 2026-09-15: `?maxResults=abc` on a key that does not exist is the conversion 400
    # where the same key alone is 404; `?maxResults=abc&orderBy=bogus` is the conversion 400 in
    # either query order; and `?startAt=abc&maxResults=xyz` names `startAt` in either order, which
    # is the handler signature's order rather than the URL's, so `startAt` is read first.
    #
    # `startAt` is the one parameter measured to take a Java LONG rather than an int.
    start = max(0, _int_param(request, "startAt", 0, width=JAVA_LONG))
    limit = min(
        _JIRA_COMMENT_PAGE_MAX,
        max(1, _int_param(request, "maxResults", _JIRA_COMMENT_PAGE_MAX)),
    )
    raw_order = _str_param(request, "orderBy")
    desc = _jira_order_desc(raw_order) if raw_order is not None else None

    conn = auth.conn(request)
    row = _require_issue(request, conn, key)

    cs = store.doc_comments(conn, "jira", row["key"])
    if desc is not None:
        # Sorted for BOTH directions, not only the descending one: `store.doc_comments` orders by
        # `seq`, the comment's position in the corpus record, and a corpus is free to list
        # comments in an order its own timestamps contradict. Passing those rows through for
        # `created` would accept the parameter and apply nothing.
        #
        # The whole collection, before the slice below — sorting a page instead would answer
        # correctly only while `startAt` is 0.
        #
        # `seq` breaks a tie, so two comments written in the same second keep a stable order
        # instead of one that depends on the rows coming back the same way twice.
        cs = sorted(cs, key=lambda c: (c["created_ts"], c["seq"]), reverse=desc)
    site = _site(request)
    return {
        "startAt": start,
        "maxResults": limit,
        "total": len(cs),
        "comments": [_jira_comment(c, site) for c in cs[start : start + limit]],
    }


@router.get("/rest/api/3/issueLinkType")
async def jira_link_types(request: Request):
    # No credential check: real Jira answers this to an anonymous caller (measured 2026-09-04).
    # The four default names, IDs and self links were measured on one Jira Cloud tenant
    # on 2026-10-09; this mirrors that default set, not tenant-customized link types.
    site = _site(request)
    types = {
        "issueLinkTypes": [
            {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
            {"id": "10001", "name": "Cloners", "inward": "is cloned by", "outward": "clones"},
            {
                "id": "10002",
                "name": "Duplicate",
                "inward": "is duplicated by",
                "outward": "duplicates",
            },
            {"id": "10003", "name": "Relates", "inward": "relates to", "outward": "relates to"},
        ]
    }

    for item in types["issueLinkTypes"]:
        item["self"] = f"{site}/rest/api/3/issueLinkType/{item['id']}"
    return types


# The standard system fields, used by clients (e.g. mcp-atlassian) for field/epic discovery.
_JIRA_FIELDS = [
    {
        "id": "summary",
        "key": "summary",
        "name": "Summary",
        "custom": False,
        "navigable": True,
        "searchable": True,
        "schema": {"type": "string", "system": "summary"},
    },
    {
        "id": "description",
        "key": "description",
        "name": "Description",
        "custom": False,
        "navigable": True,
        "searchable": True,
        "schema": {"type": "string", "system": "description"},
    },
    {
        "id": "status",
        "key": "status",
        "name": "Status",
        "custom": False,
        "navigable": True,
        "searchable": True,
        "schema": {"type": "status", "system": "status"},
    },
    {
        "id": "issuetype",
        "key": "issuetype",
        "name": "Issue Type",
        "custom": False,
        "navigable": True,
        "searchable": True,
        "schema": {"type": "issuetype", "system": "issuetype"},
    },
    {
        "id": "priority",
        "key": "priority",
        "name": "Priority",
        "custom": False,
        "navigable": True,
        "searchable": True,
        "schema": {"type": "priority", "system": "priority"},
    },
    {
        "id": "labels",
        "key": "labels",
        "name": "Labels",
        "custom": False,
        "navigable": True,
        "searchable": True,
        "schema": {"type": "array", "items": "string", "system": "labels"},
    },
    {
        "id": "assignee",
        "key": "assignee",
        "name": "Assignee",
        "custom": False,
        "navigable": True,
        "searchable": True,
        "schema": {"type": "user", "system": "assignee"},
    },
    {
        "id": "reporter",
        "key": "reporter",
        "name": "Reporter",
        "custom": False,
        "navigable": True,
        "searchable": True,
        "schema": {"type": "user", "system": "reporter"},
    },
    {
        "id": "created",
        "key": "created",
        "name": "Created",
        "custom": False,
        "navigable": True,
        "searchable": True,
        "schema": {"type": "datetime", "system": "created"},
    },
    {
        "id": "updated",
        "key": "updated",
        "name": "Updated",
        "custom": False,
        "navigable": True,
        "searchable": True,
        "schema": {"type": "datetime", "system": "updated"},
    },
    {
        "id": "project",
        "key": "project",
        "name": "Project",
        "custom": False,
        "navigable": True,
        "searchable": True,
        "schema": {"type": "project", "system": "project"},
    },
    {
        "id": "comment",
        "key": "comment",
        "name": "Comment",
        "custom": False,
        "navigable": False,
        "searchable": True,
        "schema": {"type": "comments-page", "system": "comment"},
    },
    {
        "id": "issuelinks",
        "key": "issuelinks",
        "name": "Linked Issues",
        "custom": False,
        "navigable": True,
        "searchable": True,
        "schema": {"type": "array", "items": "issuelinks", "system": "issuelinks"},
    },
    {
        "id": "parent",
        "key": "parent",
        "name": "Parent",
        "custom": False,
        "navigable": True,
        "searchable": False,
        "schema": {"type": "issuelink", "system": "parent"},
    },
    {
        "id": "subtasks",
        "key": "subtasks",
        "name": "Sub-Tasks",
        "custom": False,
        "navigable": True,
        "searchable": False,
        "schema": {"type": "array", "items": "issuelinks", "system": "subtasks"},
    },
]


@router.get(
    "/rest/api/2/field", response_model=list[JiraField]
)  # atlassian-python-api / mcp-atlassian field discovery
@router.get("/rest/api/3/field", response_model=list[JiraField])
async def jira_fields(request: Request):
    # No credential check: real Jira answers this to an anonymous caller (measured 2026-09-04).
    return _JIRA_FIELDS


# sentinel: the JQL carried a `project = X` clause that didn't resolve to any known project.
# Distinct from `None` (no project clause at all -> no filter), so callers never silently
# collapse "unresolvable" into "unfiltered" (the fidelity gap found & fixed for Confluence's
# space= handling applies identically to Jira's project= handling).
_JIRA_PROJECT_UNRESOLVED = object()


def _project_from_jql(conn, jql: str, request: Request | None = None):
    m = re.search(r"project\s*=\s*[\"']?([A-Za-z0-9_]+)", jql)
    if not m:
        return None
    container = _jira_container_for_key(conn, m.group(1), request)
    return container if container is not None else _JIRA_PROJECT_UNRESOLVED


def _text_from_jql(jql: str) -> str | None:
    """Extract the search term from a ``text ~``/``summary ~``/``description ~`` JQL clause
    (the `~` "contains" operator). Returns None when the JQL carries no text predicate."""
    m = (
        re.search(r'\b(?:text|summary|description)\s*~\s*"([^"]+)"', jql)
        or re.search(r"\b(?:text|summary|description)\s*~\s*'([^']+)'", jql)
        or re.search(r"\b(?:text|summary|description)\s*~\s*([^\s()]+)", jql)
    )
    return m.group(1).strip() if m else None


# Jira Cloud has exactly three status categories; a status name maps to one of them.
_STATUS_CATEGORY = {
    "to do": (2, "new", "blue-gray", "To Do"),
    "open": (2, "new", "blue-gray", "To Do"),
    "backlog": (2, "new", "blue-gray", "To Do"),
    "selected for development": (2, "new", "blue-gray", "To Do"),
    "reopened": (2, "new", "blue-gray", "To Do"),
    "new": (2, "new", "blue-gray", "To Do"),
    "in progress": (4, "indeterminate", "yellow", "In Progress"),
    "in review": (4, "indeterminate", "yellow", "In Progress"),
    "in development": (4, "indeterminate", "yellow", "In Progress"),
    "blocked": (4, "indeterminate", "yellow", "In Progress"),
    "done": (3, "done", "green", "Done"),
    "closed": (3, "done", "green", "Done"),
    "resolved": (3, "done", "green", "Done"),
    "complete": (3, "done", "green", "Done"),
}


def _status_category(status: str) -> dict:
    cid, key, color, name = _STATUS_CATEGORY.get(
        (status or "").strip().lower(), (2, "new", "blue-gray", "To Do")
    )
    return {"id": cid, "key": key, "colorName": color, "name": name}


def _jira_actor(email: str, site: str = "") -> dict:
    email = email or "unknown"
    display = email.split("@")[0].replace(".", " ").replace("_", " ").title()
    aid = synth.atlassian_account_id(email)
    return {
        "accountId": aid,
        "accountType": "atlassian",
        "active": True,
        "displayName": display,
        "emailAddress": email,
        "avatarUrls": synth.avatar_urls(aid),
        "timeZone": "UTC",
        "self": f"{site}/rest/api/3/user?accountId={aid}" if site else None,
    }


def _issue_key(request: Request, row) -> str:
    """The key this issue answers to — its own stored `key` column, whole.

    Nothing is composed here. The prefix and the suffix are joined at import by
    `resolve_jira_keys`, at the one moment the project's prefix is settled for the whole corpus, so
    a stated key and a derived one are the same kind of value by the time they reach here.

    Asserted, not defensively re-derived: every jira row gets a key at import (`resolve_jira_keys`
    raises rather than leave one NULL), so reaching this with a NULL one is a bug upstream. A silent
    re-derive would serve a PROBED row's synthesized suffix instead of failing where the problem
    is."""
    assert row["key"] is not None, "jira: a row reached the serializer with no key"
    return row["key"]


def _jira_ref(request: Request, row, site: str = "") -> dict:
    status = row["status"]
    return {
        "id": row["numeric_id"],
        "key": _issue_key(request, row),
        "self": f"{site}/rest/api/3/issue/{row['numeric_id']}" if site else None,
        "fields": {
            "summary": row["title"],
            "status": {"name": status, "statusCategory": _status_category(status)},
            "priority": {"name": row["priority"] or "Medium"},
            "issuetype": {"name": row["issuetype"]},
        },
    }


def _jira_comment(c, site: str = "") -> dict:
    ts = c["created_ts"] if c["created_ts"] is not None else synth.epoch(str(c["id"]))
    actor = _jira_actor(c["author_email"], site)
    cid = synth.atlassian_comment_id(c["id"])
    return {
        "id": cid,
        "self": f"{site}/rest/api/3/issue/comment/{cid}" if site else None,
        "author": actor,
        "body": _adf(c["body"]),
        "updateAuthor": actor,
        "created": synth.jira_datetime(ts),
        "updated": synth.jira_datetime(ts),
        "jsdPublic": True,
    }


def _issuetype(name: str, seed: str) -> dict:
    name = name or "Task"
    subtask = name.lower() in ("sub-task", "subtask")
    return {
        "id": str(synth.github_user_id("itype:" + name)),
        "name": name,
        "subtask": subtask,
        "hierarchyLevel": -1 if subtask else 0,
        "iconUrl": f"https://jira.example.com/issuetype/{name.lower().replace(' ', '-')}.png",
    }


def _jira_issue(conn, request: Request, row, expand: str = "", fields_only: bool = False) -> dict:
    site = _site(request)
    # `is not None`: an issue dated 1970-01-01T00:00:00Z stores 0 and must serve it.
    created = row["created_ts"] if row["created_ts"] is not None else synth.epoch(row["key"])
    updated = row["updated_ts"] if row["updated_ts"] is not None else created + 3600
    pkey = _project_key(conn, row["project"])
    reporter = _jira_actor(row["reporter_email"] or row["author_email"], site)
    creator = _jira_actor(row["author_email"], site)
    assignee = _jira_actor(row["assignee_email"], site) if row["assignee_email"] else None
    status = row["status"]
    resolution = (
        None
        if not row["resolution"]
        else {
            "id": str(synth.github_user_id("res:" + row["resolution"])),
            "name": row["resolution"],
            "description": "",
        }
    )
    fields = {
        "summary": row["title"],
        "description": _adf(row["content"]),
        "issuetype": _issuetype(row["issuetype"], row["key"]),
        "project": {
            "id": str(synth.github_user_id(row["project"])),
            "key": pkey,
            "name": row["project"],
            "projectTypeKey": _JIRA_PROJECT_TYPE,
            "simplified": False,
            "self": f"{site}/rest/api/3/project/{pkey}",
            "avatarUrls": synth.avatar_urls("proj:" + pkey),
        },
        "status": {
            "id": str(synth.github_user_id("status:" + status)),
            "name": status,
            "statusCategory": _status_category(status),
        },
        "priority": {
            "id": str(synth.github_user_id("prio:" + (row["priority"] or "Medium"))),
            "name": row["priority"] or "Medium",
            "iconUrl": f"{site}/images/icons/priorities/{(row['priority'] or 'medium').lower()}.svg",
        },
        "labels": store.jcol(row, "labels"),
        "components": [
            {
                "id": str(synth.github_user_id("comp:" + c)),
                "name": c,
                "self": f"{site}/rest/api/3/component/{synth.github_user_id('comp:' + c)}",
            }
            for c in store.jcol(row, "components")
        ],
        "created": synth.jira_datetime(created),
        "updated": synth.jira_datetime(updated),
        "creator": creator,
        "reporter": reporter,
        "assignee": assignee,
        "resolution": resolution,
        "resolutiondate": synth.jira_datetime(row["resolution_ts"])
        if row["resolution_ts"]
        else None,
        "duedate": row["duedate"],
        "fixVersions": [
            {"id": str(synth.github_user_id("ver:" + v)), "name": v, "released": False}
            for v in store.jcol(row, "fix_versions")
        ],
        "versions": [],
        "attachment": [],
        "votes": {"votes": 0, "hasVoted": False},
        "watches": {"watchCount": 0, "isWatching": False},
        "timetracking": {},
    }
    if not fields_only:
        cs = store.doc_comments(conn, "jira", row["key"])
        fields["comment"] = {
            "comments": [_jira_comment(c, site) for c in cs],
            "maxResults": len(cs),
            "total": len(cs),
            "startAt": 0,
        }
        fields["issuelinks"] = store.jcol(row, "issuelinks")
        subs = store.children(conn, "jira", row["key"])
        fields["subtasks"] = [_jira_ref(request, s, site) for s in subs]
        if row["parent_id"]:
            prow = store.get_document(conn, "jira", row["parent_id"])
            if prow:
                fields["parent"] = _jira_ref(request, prow, site)
    nid = row["numeric_id"]
    issue = {
        "id": str(nid),
        "key": _issue_key(request, row),
        "self": f"{site}/rest/api/3/issue/{nid}",
        "fields": fields,
    }
    if not fields_only and "changelog" in (expand or ""):
        hist = store.jcol(row, "changelog")
        issue["changelog"] = {
            "startAt": 0,
            "maxResults": len(hist),
            "total": len(hist),
            "histories": hist,
        }
    return issue


# ============================== Confluence ======================================


#: How a body's characters are written: storage writes each character HTML 4 names
#: (`codepoint2name`) as that entity but `Š` and `š`, view and export_view write only `"`, `&`, `<`
#: and `>` as entities, and every other character measured is itself in all three, `'` among them.
#: Measured against Confluence Cloud on pages on 2026-10-23, and with twelve of those characters on
#: a comment and a blog post on 2026-10-25.
_CONFLUENCE_STORAGE_ENTITIES = {
    cp: f"&{name};" for cp, name in codepoint2name.items() if cp not in (0x160, 0x161)
}
_CONFLUENCE_RENDERED_ENTITIES = str.maketrans(
    {'"': "&quot;", "&": "&amp;", "<": "&lt;", ">": "&gt;"}
)


def _storage(content: str) -> str:
    """Confluence storage format — XHTML with a leading structured macro, as the real
    editor emits (distinct from the rendered view below)."""
    paras = [p for p in content.split("\n\n") if p.strip()] or [content]
    return "".join(f"<p>{p.translate(_CONFLUENCE_STORAGE_ENTITIES)}</p>" for p in paras)


def _view(content: str) -> str:
    """Rendered ``view`` HTML — differs from storage (wrapped, ids, no ac: macros), as the
    real API returns a rendered representation, not the storage source."""
    paras = [p for p in content.split("\n\n") if p.strip()] or [content]
    body = "".join(
        f'<p class="auto-cursor-target">{p.translate(_CONFLUENCE_RENDERED_ENTITIES)}</p>'
        for p in paras
    )
    return f'<div class="contentLayout2"><div class="columnLayout single">{body}</div></div>'


def _export_view(content: str) -> str:
    """Rendered ``export_view`` HTML — the real API's export-oriented rendering (same content
    as ``view``, without editor-only attributes like ``auto-cursor-target``)."""
    paras = [p for p in content.split("\n\n") if p.strip()] or [content]
    body = "".join(f"<p>{p.translate(_CONFLUENCE_RENDERED_ENTITIES)}</p>" for p in paras)
    return f'<div class="contentLayout2"><div class="columnLayout single">{body}</div></div>'


def _space_container_for_key(conn, space_key: str) -> str | None:
    """Resolve a Confluence ``spaceKey`` to its backing container name. Backlot models a space by
    its corpus name, so both the synthesized key (``synth.confluence_space_key(name)``, the
    hash-suffixed value ``/space`` advertises) and the literal container name (e.g. ``"handbook"``,
    a legitimate natural key) resolve. Anything else is unresolvable -> ``None``, which no caller
    reads as "no filter": a CQL search matches nothing, `content` 404s and `space` leaves it out.
    Case does not decide whether a key resolves: real reads a key whatever its case (measured
    2026-10-10, `space/mfs` and `space/MfS` answering the space `space/MFS` does), and the name half
    is folded too because a natural key the corpus names may be typed in any case. The fold is
    `str.lower`: real answers `space/MFſ` and `content?spaceKey=MFſ` with the 404 a key naming no
    space gets (measured 2026-10-11), and `str.casefold` would read that `ſ` as `s`. An exact
    spelling is tried first, so two spaces whose names differ only in case each keep their own
    name."""
    names = [r["name"] for r in store.list_containers(conn, "confluence")]
    for fold in (str, str.lower):
        for name in names:
            if fold(space_key) in (fold(synth.confluence_space_key(name)), fold(name)):
                return name
    return None


def _content_space(conn, ids, space_key: str) -> str:
    """The container `content?spaceKey=` filters by, or :func:`errors_atlassian.no_space_with_key`.

    A space the caller reaches no page in is refused the same way as a key naming nothing, as
    :func:`_require_space` refuses it on the space reads, so the listing does not confirm that the
    space exists. Unmeasured for a scoped caller, see :func:`_reachable_spaces`. A repeated
    `spaceKey` is one value with a comma in it (:func:`_str_param`), which no key holds."""
    container = _space_container_for_key(conn, space_key)
    if container is not None and (
        ids is None or store.has_visible_document(conn, "confluence", container, ids)
    ):
        return container
    raise errors_atlassian.no_space_with_key(space_key)


def _reachable_spaces(conn, ids) -> list:
    """The spaces the caller can reach, as container rows — `_reachable_projects` for Confluence.

    A space is listed when the caller can read a page in it: the ACL grants per document, so there
    is nothing else to read "can view this space" off. NOT measured — Confluence 403s an anonymous
    request before resolving a space, and a scoped measurement needs two accounts and a space
    permission scheme on a live site. Real Confluence lists by space permission, which can grant
    `read` while showing the caller no page, so a real listing can hold a space this one drops.
    """
    rows = store.list_containers(conn, "confluence")
    if ids is None:
        return rows
    return [r for r in rows if store.has_visible_document(conn, "confluence", r["name"], ids)]


def _space_keys(request: Request) -> list[str] | None:
    """The keys `space?spaceKey=` narrows the listing to, or ``None`` for no narrowing.

    Read the way Spring binds a list parameter, measured 2026-10-12 on Confluence Cloud. One value
    is split at each comma and each piece trimmed as Java's `String.trim()` trims: `spaceKey=A,B`
    names two spaces, `%20A` and `%09A` are `A`, and a no-break space is kept, so `%C2%A0A` names
    nothing. Two or more values are taken as sent, neither split nor trimmed:
    `spaceKey=NOSUCH&spaceKey=%20A` lists nothing, and `spaceKey=NOSUCH&spaceKey=A,B` lists nothing
    either. An empty value alone is no narrowing at all, while a piece that trims to nothing names
    no space, so `spaceKey=%20` and `spaceKey=%2C` list nothing.
    """
    values = request.query_params.getlist("spaceKey")
    if len(values) == 1:
        values = [v.strip(_JAVA_TRIMMED) for v in values[0].split(",")] if values[0] else []
    return values or None


def _require_space(request: Request, conn, key: str) -> str:
    """The container behind a space key the caller can reach — `_require_project` for Confluence.

    Both space reads answer an unreachable key with the 404 a key naming nothing gets
    (:func:`errors_atlassian.space_not_found`, which names only the key the caller sent), so neither
    confirms the space exists — worth withholding because ``?expand=permissions`` names a principal
    granted a space the caller cannot open. Unmeasured for a scoped caller, see
    :func:`_reachable_spaces`.
    """
    ids = auth.visible_ids(request, _confluence_caller(request))
    container = _space_container_for_key(conn, key)
    # The `ids is None` short-circuit is load-bearing only on GitHub (see `_require_project`'s
    # note): `confluence.schema.json` requires `space` on every record, so no space exists
    # without a page, and here it keeps the readers of the two APIs alike.
    if container is not None and (
        ids is None or store.has_visible_document(conn, "confluence", container, ids)
    ):
        return container
    raise errors_atlassian.space_not_found(key)


# The thirteen keys real names under `_expandable` on a space, in real's own order. Measured on
# brekkylab.atlassian.net, 2026-09-16, on a global space and two personal ones, which agreed: four
# of the keys carry a path and nine are the empty string. `homepage` is one of the four there, and
# is empty here — a corpus names no home page for a space, and a space without one is not something
# the measurement could produce, so the empty string is this server's gap rather than a value real
# was seen to send.
def _space_expandable(key: str) -> dict:
    return {
        "settings": f"/rest/api/space/{key}/settings",
        "metadata": "",
        "identifiers": "",
        "roles": "",
        "icon": "",
        "typeSettings": "",
        "description": "",
        "history": "",
        "operations": "",
        "lookAndFeel": f"/rest/api/settings/lookandfeel?spaceKey={key}",
        "permissions": "",
        "theme": f"/rest/api/space/{key}/theme",
        "homepage": "",
    }


def _space_permissions(request: Request, conn, container: str) -> list[dict]:
    """The space permission roster, as `?expand=permissions` answers it.

    One entry per GRANT, which is real's unit: of the 120 entries on the space measured
    (brekkylab.atlassian.net, 2026-09-17) 81 carry a subject and every one of those is a single
    user, while the other 39 carry no `subjects` key at all. So the roster's length tracks grants
    rather than membership, and `permissions[i].subjects.user.results[0]` names one principal. A
    `group` or `org` grant is an entry with no `subjects` here for the same reason real leaves those
    collapsed — no group subject appeared under this expansion on the space measured.

    One operation, `read`/`space`, because it is the only one a corpus states: the ACL says who can
    read a document and nothing at all about who may administer the space or delete a comment. Real
    answered 26 operations there, five of them `read`/`space`; inventing the other 25 would put a
    permission model on the wire that the corpus never licensed.

    ``anonymousAccess`` is False whatever the grant, because no grant a corpus can write says "the
    public": an org grant is every MEMBER and not every visitor (`store._expand_grants` returns
    ``None`` for `principal_type == "org"`), and ``_confluence_caller`` refuses an anonymous caller
    before a space is resolved at all. Real's was False on every entry measured.

    The order is this function's own — real's was not measured — and it is sorted so that two reads
    of the same space agree.
    """
    entries = []
    for g in sorted(store.container_grants(conn, "confluence", container), key=tuple):
        ptype, pid = g["principal_type"], g["principal_id"]
        entry = {"id": synth.confluence_id(f"perm:{container}:read:{ptype}:{pid}")}
        if ptype == "user":
            entry["subjects"] = {
                "user": {"results": [_conf_user(pid, _site(request))], "size": 1},
                "_expandable": {"group": ""},
            }
        entry["operation"] = {"operation": "read", "targetType": "space"}
        entry["anonymousAccess"] = False
        entry["unlicensedAccess"] = False
        entries.append(entry)
    return entries


def _space_description(container: str, subs: set[str]) -> dict:
    """A space description, as the sub-properties in ``subs`` select it.

    Measured on brekkylab.atlassian.net, 2026-09-17: the bare `expand=description` carries NO value,
    only `{"_expandable": {"view": "", "plain": ""}}`. The value arrives for `description.plain` or
    `description.view`, each leaving the other spelling in a nested `_expandable`, and asking for
    both leaves no `_expandable` at all. A client that reads `description.plain.value` off the bare
    spelling gets nothing from real, so serving it there would answer a body real does not send.

    The two renderings differ only in `representation` here, because a corpus states one description
    text and real's was empty on the space measured.
    """
    out = {
        sub: {"value": f"{container} space", "representation": sub, "embeddedContent": []}
        for sub in ("plain", "view")
        if sub in subs
    }
    rest = {sub: "" for sub in ("view", "plain") if sub not in subs}
    if rest:
        out["_expandable"] = rest
    return out


def _space(request: Request, conn, container: str, expand: str, *, listed: bool) -> dict:
    """One space, as both reads render it.

    ``listed`` is the one difference real draws between them, and it is in `_links`: a space inside
    the listing carries `webui` and `self` alone, where the single read carries `context`,
    `collection` and `base` beside them. Measured 2026-09-16 on the same site, the same minute.

    An expansion real serves is REMOVED from `_expandable` once it is served, which is how a client
    tells an expansion it asked for and got from one it asked for and did not.
    """
    key = synth.confluence_space_key(container)
    site = _site(request)
    space = {
        "id": synth.github_user_id(container),
        "ari": (
            f"ari:cloud:confluence:{synth.atlassian_cloud_id(get_settings().org_name)}"
            f":space/{synth.github_user_id(container)}"
        ),
        "key": key,
        "alias": key,
        "name": container,
        "type": "global",
        "status": "current",
        "_expandable": _space_expandable(key),
    }
    # A term names its property before the first dot and its sub-property after: real answers
    # `expand=description.plain` and `expand=permissions.bogus` with the property expanded, and
    # ignores a term naming no property (`descriptions`, `bogus`) rather than refusing it. Measured
    # on brekkylab.atlassian.net, 2026-09-17, on `space/{key}`.
    wanted: dict[str, set[str]] = {}
    for term in (expand or "").split(","):
        head, _, sub = term.strip().partition(".")
        if head:
            wanted.setdefault(head, set()).update([sub] if sub else [])
    if "description" in wanted:
        space["description"] = _space_description(container, wanted["description"])
        space["_expandable"].pop("description", None)
    if "permissions" in wanted:
        space["permissions"] = _space_permissions(request, conn, container)
        space["_expandable"].pop("permissions", None)
    links = {"webui": f"/spaces/{key}", "self": f"{site}/wiki/rest/api/space/{key}"}
    if not listed:
        links = {
            "context": "/wiki",
            "self": links["self"],
            "collection": "/rest/api/space",
            "webui": links["webui"],
            "base": f"{site}/wiki",
        }
    space["_links"] = links
    return space


@router.get("/wiki/rest/api/space", response_model=ConfluenceResults, openapi_extra=_P_SPACE)
async def confluence_spaces(request: Request):
    """Paged the way `content` is (`?limit`/`?start`, both through `_confluence_page_params`), and
    answering the `_links` every paged listing answers (:func:`_confluence_envelope`). `expand` is
    applied per space through :func:`_space` and carried into `next`/`prev`/`self` too.

    `limit` is capped at 1000, as real caps it.

    `spaceKey` narrows the listing to the spaces it names (:func:`_space_keys`), each resolved as
    every other reader resolves a key (:func:`_space_container_for_key`), and `total`, `next` and
    `prev` count the narrowed listing. Measured 2026-10-12 on Confluence Cloud: the spaces come back
    in the listing's own order whatever order the keys were sent in, a space named twice is listed
    once, and a key naming no space is no refusal, so `spaceKey=NOSUCH` is a 200 with no results
    where `content?spaceKey=NOSUCH` is a 404. The narrowing applies to the spaces the caller
    reaches, so a key naming a space they reach no page in is left out as a key naming nothing is.
    """
    conn = auth.conn(request)
    ids = auth.visible_ids(request, _confluence_caller(request))
    limit, start = _confluence_page_params(request, cap=1000)
    expand = _str_param(request, "expand", "") or ""
    # store.list_containers orders by name; real's own order is none of name, key or id (measured
    # 2026-09-17).
    reachable = _reachable_spaces(conn, ids)
    keys = _space_keys(request)
    if keys is not None:
        named = {_space_container_for_key(conn, key) for key in keys}
        reachable = [r for r in reachable if r["name"] in named]
    total = len(reachable)
    results = [
        _space(request, conn, r["name"], expand, listed=True)
        for r in reachable[start : start + limit]
    ]
    links = _confluence_envelope(
        request,
        "/rest/api/space",
        start=start,
        limit=limit,
        size=len(results),
        total=total,
        head=("spaceKey",),
    )
    return {
        "results": results,
        "start": start,
        "limit": limit,
        "size": len(results),
        "_links": links,
    }


@router.get("/wiki/rest/api/space/{key}/permission", include_in_schema=False)
async def confluence_space_permission(key: str, request: Request):
    """Real refuses a `GET` here, so Backlot refuses one too, and the roster is served where real
    serves it: `space/{key}?expand=permissions`.

    A route rather than nothing at all, because the path having no handler is a 404 and real's
    answer is a 405 — the vendor's own document declares one operation here and it is a `POST`
    (add a space permission), a write no source in Backlot serves. ``include_in_schema=False``
    keeps the refusal off ``app.openapi()``, which is what `backlot diff` compares: the 405 is a
    fact about the wire, and a `GET` operation on this path is a declaration the vendor's document
    does not make.

    A `POST` here is the published write, which ``_published_answer`` answers as real does before
    it runs (its 415, then the 406 for an `Accept` admitting no JSON, then the caller's refusal,
    :data:`_CONFLUENCE_CONSUMES`).

    ``key`` is unused and declared because the path carries it: the refusal comes before any lookup
    on real, where `GET space/NOSUCHSPACE/permission` answers the same 405 as a key that names a
    space (measured 2026-09-16), so resolving one here would only be able to disagree.
    """
    raise errors_atlassian.method_not_allowed(request.url.path, request.method)


@router.get("/wiki/rest/api/space/{key}", response_model=ConfluencePage, openapi_extra=_P_EXPAND)
async def confluence_space_get(key: str, request: Request):
    """Single-space fetch (atlassian-python-api's ``get_space`` / mcp-atlassian result enrichment).
    404s (Atlassian-shaped) for a key naming no space the caller can reach (:func:`_require_space`)."""
    conn = auth.conn(request)
    container = _require_space(request, conn, key)
    return _space(request, conn, container, _str_param(request, "expand", "") or "", listed=False)


@router.get("/wiki/rest/api/search", response_model=ConfluenceResults, openapi_extra=_P_CQL)
async def confluence_cql_search(request: Request):
    """CQL search used by Confluence clients (e.g. mcp-atlassian). Four clauses are read: a `~`
    operand is a keyword search over the ACL-visible corpus, and `space`, `type` and `label` filter
    its matches, or the whole ACL-visible corpus when there is no `~`. Measured 2026-10-05 with no
    `~`: real's `type=page` is every page on the site and `space=<KEY> and type=page` that space's
    pages; its `space=<KEY>` also holds the space's attachments, comments and the space itself,
    where this server answers the space's pages and blogposts. A CQL holding none of the four names
    only fields this route does not read, and answers no row. A `Content-Type` it will not read is
    refused before this runs (:func:`_refuse_the_search_media_type`)."""
    conn = auth.conn(request)
    caller = _confluence_caller(request, not_permitted=True)
    ids = auth.visible_ids(request, caller)
    # Not `_confluence_page_params`: see `_cql_page_param`.
    limit = _cql_page_param(request, "limit", 25)
    start = _cql_page_param(request, "start", 0)
    if limit is None or start is None:
        return Response(status_code=404)
    # the first value, between the 404 and the negative check: see `errors_atlassian.cql_required`
    cqls = request.query_params.getlist("cql")
    if not cqls or not cqls[0]:
        raise errors_atlassian.cql_required()
    cql = cqls[0]
    m = re.search(r'(?:text|title)\s*~\s*"?([^"~]+)"?', cql) or re.search(r'~\s*"?([^"~]+)"?', cql)
    term = m.group(1).strip() if m else ""
    # honor the common structured CQL clauses: space / type / label
    ms = re.search(r'space(?:\.key)?\s*=\s*"?([A-Za-z0-9_-]+)"?', cql)
    space_key = ms.group(1) if ms else None
    space_unresolvable = False
    container = None
    if space_key:
        container = _space_container_for_key(conn, space_key)
        if container is None:
            # unresolvable space=/space.key= clause: strict 0 matches, not the unfiltered corpus.
            space_unresolvable = True
    mt = re.search(r'type\s*=\s*"?(page|blogpost|comment)"?', cql)
    want_type = mt.group(1) if mt else None
    ml = re.search(r'label\s*(?:=|in)\s*"?([^")\s]+)"?', cql)
    want_label = ml.group(1) if ml else None
    _refuse_negative_page_params(limit, start)
    tokens = _cql_tokens(cql)
    refusal = _cql_refusal(tokens)
    if refusal is not None:
        raise refusal
    # An empty `cursor` is none, and of a repeated one the first is read, measured 2026-10-04.
    sent_cursor = (request.query_params.getlist("cursor") or [None])[0]
    after = _cql_search_after(sent_cursor) if sent_cursor else None

    # fetch the full ACL-visible match set, filter by the clauses, then paginate — so
    # totalSize reflects the true match count (not just the returned page).
    if term:
        everything = store.search_documents(conn, term, "confluence", ids, limit=100_000, offset=0)
    elif space_key or want_type or want_label:
        everything = store.list_documents(
            conn, "confluence", container=None, visible_ids=ids, limit=100_000, offset=0
        )
    else:
        everything = []

    def _match(r) -> bool:
        if space_unresolvable:
            return False
        if container and r["space"] != container:
            return False
        if want_type and (r["subtype"] or "page") != want_type:
            return False
        if want_label and want_label not in store.jcol(r, "labels"):
            return False
        return True

    matched = [r for r in everything if _match(r)]
    total = len(matched)
    position = _cql_position(matched, after[0]) if after else 0
    rows = matched[position : position + limit]
    reached = position + max(len(rows), 1)
    if reached < total and start + max(len(rows), 1) > 2**31 - 1:
        raise errors_atlassian.search_next_out_of_range()
    results = []
    for r in rows:
        page = _confluence_page(conn, request, r, "version,space")
        results.append(
            {
                "content": page,
                "title": r["title"],
                "excerpt": r["content"][:200],
                "url": page["_links"]["webui"],
                "entityType": "content",
                "lastModified": synth.rfc3339_millis(
                    _confluence_ts(r["updated_ts"], r["created_ts"], r["id"])
                ),
            }
        )
    links = _confluence_envelope(
        request,
        "/rest/api/search",
        start=start,
        limit=limit,
        size=len(results),
        total=total,
        cursor=_cql_cursor(rows, matched, position),
        sent_cursor=sent_cursor,
        reached=reached,
    )
    return Response(
        errors_atlassian._confluence_json(
            {
                "results": results,
                "start": start,
                "limit": limit,
                "size": len(results),
                "totalSize": total,
                "cqlQuery": _cql_written_back(tokens),
                "searchDuration": 5,
                "_links": links,
            }
        ),
        media_type="application/json",
    )


@router.get("/wiki/rest/api/content", response_model=ConfluenceResults, openapi_extra=_P_CONTENT)
async def confluence_content_list(request: Request):
    conn = auth.conn(request)
    caller = _confluence_caller(request)
    ids = auth.visible_ids(request, caller)
    expand = _str_param(request, "expand", "") or ""
    # The refusals in the order `_CONTENT_TYPES` records. An empty `type` is no type, and a
    # repeated one is a single value with a comma in it, which names no type.
    limit = _int_param(request, "limit", 25)
    start = _int_param(request, "start", 0)
    content_type = _str_param(request, "type") or "page"
    answer = _CONTENT_TYPES.get(content_type)
    if answer is not None and start > _CONTENT_START_BOUND:
        raise errors_atlassian.start_too_large()
    space_key = _str_param(request, "spaceKey")
    container = _content_space(conn, ids, space_key) if space_key else None
    if answer == "unfetchable":
        raise errors_atlassian.content_finder_cannot_fetch(content_type)
    _refuse_negative_page_params(limit, start)
    limit = min(limit, 1000)
    if answer == "not_custom":
        raise errors_atlassian.not_a_custom_content_type(content_type)
    if answer == "no_container":
        raise errors_atlassian.attachment_without_container()
    # Measured on a Confluence Cloud tenant on 2026-10-03 and 2026-10-05: `title` answered the page
    # with that title whether spelled as stored, in lower case or in upper case, with or without
    # `spaceKey`, with no `next` when asked for one page at a time. A title no page has answered
    # `size: 0`, and so did the title's first word, the title with a space before or after it, a
    # space alone, and a repeated `title` in either order (`_str_param` joins it with a comma); an
    # empty `title` filters nothing.
    title = _str_param(request, "title") or None
    if answer is None:
        # A type Confluence does not have: refused on its own, and beside a space or a title the
        # filter that matches nothing.
        if container is None and title is None:
            raise errors_atlassian.unknown_content_type(content_type)
        total, rows = 0, []
    else:
        subtype = content_type if _str_param(request, "type") else None
        total = store.count_documents(
            conn, "confluence", container, ids, title=title, subtype=subtype
        )
        rows = store.list_documents(
            conn,
            "confluence",
            container,
            ids,
            limit=limit,
            offset=start,
            title=title,
            subtype=subtype,
        )
    results = [_confluence_page(conn, request, r, expand) for r in rows]
    links = _confluence_envelope(
        request, "/rest/api/content", start=start, limit=limit, size=len(rows), total=total
    )
    return {"results": results, "start": start, "limit": limit, "size": len(rows), "_links": links}


@router.get(
    "/wiki/rest/api/content/{content_id}", response_model=ConfluencePage, openapi_extra=_P_EXPAND
)
async def confluence_content_get(content_id: str, request: Request):
    conn = auth.conn(request)
    caller = _confluence_caller(request)
    content_id, attachment = _content_id(content_id)
    ids = auth.visible_ids(request, caller)
    row = store.confluence_by_id(conn, content_id, visible_ids=ids)
    if row is None or attachment:
        raise errors_atlassian.content_not_found("content", content_id, attachment=attachment)
    return _confluence_page(conn, request, row, _str_param(request, "expand", "body.storage"))


@router.get(
    "/wiki/rest/api/content/{content_id}/child/page",
    response_model=ConfluenceResults,
    openapi_extra=_P_CHILD_PAGE,
)
async def confluence_child_pages(content_id: str, request: Request):
    conn = auth.conn(request)
    caller = _confluence_caller(request)
    content_id, _ = _content_id(content_id)
    limit, start = _confluence_page_params(request)
    ids = auth.visible_ids(request, caller)
    if store.get_document(conn, "confluence", content_id, visible_ids=ids) is None:
        raise errors_atlassian.content_not_found("child/page", content_id)
    expand = _str_param(request, "expand", "") or ""
    kids = store.children(conn, "confluence", content_id, visible_ids=ids)
    page = kids[start : start + limit]
    results = [_confluence_page(conn, request, k, expand) for k in page]
    return {
        "results": results,
        "start": start,
        "limit": limit,
        "size": len(results),
        "_links": _confluence_envelope(
            request,
            f"/rest/api/content/{content_id}/child/page",
            start=start,
            limit=limit,
            size=len(results),
            total=len(kids),
        ),
    }


@router.get("/wiki/rest/api/content/{content_id}/child/comment", openapi_extra=_P_CONTENT_CHILD)
async def confluence_comments(content_id: str, request: Request):
    conn = auth.conn(request)
    caller = _confluence_caller(request)
    content_id, _ = _content_id(content_id)
    limit, start = _confluence_page_params(request, cap=1000)
    ids = auth.visible_ids(request, caller)
    if store.get_document(conn, "confluence", content_id, visible_ids=ids) is None:
        raise errors_atlassian.content_not_found("child/comment", content_id)
    comments = store.doc_comments(conn, "confluence", content_id)
    results = []
    for c in comments[start : start + limit]:
        ts = c["created_ts"] if c["created_ts"] is not None else synth.epoch(str(c["id"]))
        author = c["author_email"] or "unknown"
        cid = synth.atlassian_comment_id(c["id"])
        results.append(
            {
                "id": cid,
                "type": "comment",
                "status": "current",
                "title": f"Re: {content_id}",
                "body": {
                    "storage": {"value": _storage(c["body"]), "representation": "storage"},
                    "view": {"value": _view(c["body"]), "representation": "view"},
                },
                "version": {
                    "number": 1,
                    "when": synth.rfc3339_millis(ts),
                    "by": _conf_user(author, _site(request)),
                    "minorEdit": False,
                    "message": "",
                },
                "extensions": {"location": "footer"},
                "_links": {"webui": f"/spaces/x/pages/{content_id}?focusedCommentId={cid}"},
            }
        )
    return {
        "results": results,
        "start": start,
        "limit": limit,
        "size": len(results),
        "_links": _confluence_envelope(
            request,
            f"/rest/api/content/{content_id}/child/comment",
            start=start,
            limit=limit,
            size=len(results),
            total=len(comments),
        ),
    }


@router.get("/wiki/rest/api/content/{content_id}/child/attachment", openapi_extra=_P_CONTENT_CHILD)
async def confluence_attachments(content_id: str, request: Request):
    """A page's attachments, which is the empty page for every page: a corpus record states no
    attachment, and this is the page real answers for a page holding none.
    """
    conn = auth.conn(request)
    caller = _confluence_caller(request)
    content_id, _ = _content_id(content_id)
    limit, start = _confluence_page_params(request, default=50)
    ids = auth.visible_ids(request, caller)
    if store.get_document(conn, "confluence", content_id, visible_ids=ids) is None:
        raise errors_atlassian.content_not_found("child/attachment", content_id)
    return {
        "results": [],
        "start": start,
        "limit": limit,
        "size": 0,
        "_links": _confluence_envelope(
            request,
            f"/rest/api/content/{content_id}/child/attachment",
            start=start,
            limit=limit,
            size=0,
            total=0,
        ),
    }


# The label prefixes real reads, in this case only (Confluence Cloud, 2026-10-14).
_LABEL_PREFIXES = frozenset({"global", "my", "team", "system"})


def _refuse_a_label_prefix(request: Request) -> None:
    """Confluence's 400 for a `prefix` it cannot convert, measured on `content/{id}/label` on
    2026-10-09 and 2026-10-14.

    Every `prefix` value is read, each split at its commas and each element trimmed as Java's
    `String.trim` trims (`%09global` is `global`); a single empty `prefix` is the parameter unsent.
    Any element outside :data:`_LABEL_PREFIXES` is the refusal, which names every element in the
    order sent: `?prefix=global&prefix=bogus` names `[global, bogus]`, `?prefix=global,` names
    `[global, ]` and `?prefix=%20` names `[]`."""
    values = request.query_params.getlist("prefix")
    if values in ([], [""]):
        return
    parts = [part.strip(_JAVA_TRIMMED) for value in values for part in value.split(",")]
    if not _LABEL_PREFIXES.issuperset(parts):
        raise errors_atlassian.label_prefixes_not_converted(parts)


@router.get("/wiki/rest/api/content/{content_id}/label", openapi_extra=_P_CONTENT_CHILD)
async def confluence_labels(content_id: str, request: Request):
    conn = auth.conn(request)
    caller = _confluence_caller(request)
    content_id, attachment = _content_id(content_id)
    # Real converts `limit` and `start`, then reads `prefix`, before it looks the page up, and
    # refuses a negative or zero one after it: on an id naming nothing, `?limit=abc` and
    # `?prefix=bogus` are their 400s and `?limit=-1` and `?limit=0` the 404 (Confluence Cloud,
    # 2026-10-14, 2026-10-16 and 2026-10-21).
    _int_param(request, "limit", 200)
    _int_param(request, "start", 0)
    _refuse_a_label_prefix(request)
    ids = auth.visible_ids(request, caller)
    row = store.get_document(conn, "confluence", content_id, visible_ids=ids)
    if row is None or attachment:
        raise errors_atlassian.content_not_found("label", content_id, attachment=attachment)
    limit, start = _confluence_page_params(request, default=200, cap=200, refuse_zero=True)
    labels = store.jcol(row, "labels")
    results = [
        {"prefix": "global", "name": lbl, "id": str(synth.confluence_id(lbl)), "label": lbl}
        for lbl in labels[start : start + limit]
    ]
    return {
        "results": results,
        "start": start,
        "limit": limit,
        "size": len(results),
        "_links": _confluence_envelope(
            request,
            f"/rest/api/content/{content_id}/label",
            start=start,
            limit=limit,
            size=len(results),
            total=len(labels),
        ),
    }


@router.get("/wiki/rest/api/content/{content_id}/restriction/byOperation")
async def confluence_restrictions(content_id: str, request: Request):
    conn = auth.conn(request)
    caller = _confluence_caller(request)
    content_id, _ = _content_id(content_id)
    ids = auth.visible_ids(request, caller)
    if store.get_document(conn, "confluence", content_id, visible_ids=ids) is None:
        raise errors_atlassian.content_not_found("restriction/byOperation", content_id)
    emails = store.doc_member_emails(conn, "confluence", content_id)
    site = _site(request)
    users = [] if emails is None else [_conf_user(e, site) for e in sorted(emails)]
    path = f"{site}/wiki/rest/api/content/{content_id}/restriction/byOperation"
    content = f"/rest/api/content/{content_id}"
    lists = {
        "user": {"results": users, "start": 0, "limit": 200, "size": len(users)},
        "group": {"results": [], "start": 0, "limit": 200, "size": 0},
    }
    named = _restriction_expansions(request)

    # Real's members in real's order (Confluence Cloud, 2026-10-09, 2026-10-14 and 2026-10-17):
    # each operation links to itself under this path, and the answer ends with the links a listing
    # carries. With no `expand`, both operations come with both lists. Once `expand` names
    # something, an operation it does not name is listed under the answer's `_expandable`, read
    # ahead of update whatever order `expand` names them in. A named operation comes with its
    # `restrictions` only when `expand` names them, and then with the lists it names, user ahead of
    # group, and the others under the `restrictions` object's own `_expandable`.
    def _op(name):
        op = {"operation": name}
        paths = None if named is None else named[name]
        if paths is None or any(p[:1] == ["restrictions"] for p in paths):
            kinds = [
                k
                for k in lists
                if paths is None or any(p[:2] == ["restrictions", k] for p in paths)
            ]
            op["restrictions"] = {k: lists[k] for k in kinds}
            if unnamed := [k for k in lists if k not in kinds]:
                op["restrictions"]["_expandable"] = dict.fromkeys(unnamed, "")
            op["_expandable"] = {"content": content}
        else:
            op["_expandable"] = {"restrictions": "", "content": content}
        op["_links"] = {"self": f"{path}/{name}"}
        return op

    answer = {name: _op(name) for name in _RESTRICTION_OPERATIONS if named is None or name in named}
    if unnamed := [name for name in _RESTRICTION_OPERATIONS if name not in answer]:
        answer["_expandable"] = dict.fromkeys(unnamed, "")
    answer["_links"] = {"self": path, "base": f"{site}/wiki", "context": "/wiki"}
    return answer


#: The operations `restriction/byOperation` answers, in the order real lists them.
_RESTRICTION_OPERATIONS = ("read", "update")


def _restriction_expansions(request: Request) -> dict[str, list[list[str]]] | None:
    """What `expand` names under each operation of `restriction/byOperation`: for each operation
    it names, the dotted segments after the operation's name, one list per item; None when no
    `expand` value holds an item, which real answers as it answers no `expand`.

    Measured on Confluence Cloud, 2026-10-17: real joins repeated `expand` parameters, splits each
    value at `,` and each item at `.`, skips empty items (`read,`, `,read`, `read,,update`), and
    compares each segment as sent. `READ`, ` read`, `read ` and `.read` name no operation;
    `read.restrictions.USER`, `read..restrictions` and `read.Restrictions.user` name no list; a
    segment past the list (`read.restrictions.user.bogus`) is ignored. `expand=` answers as no
    `expand` does, and `expand=%20` names nothing. `read.content` expands the page into the
    operation, which is not modelled here: it answers as `read` does."""
    items = [i for value in request.query_params.getlist("expand") for i in value.split(",") if i]
    if not items:
        return None
    named: dict[str, list[list[str]]] = {}
    for item in items:
        operation, *rest = item.split(".")
        if operation in _RESTRICTION_OPERATIONS:
            named.setdefault(operation, []).append(rest)
    return named


def _conf_user(email: str, site: str) -> dict:
    """The user object every Confluence read carries.

    One helper for all of them because real sends ONE object: the space roster's subject, a page's
    `version.by` and `history.createdBy`, and a comment's own two are the same thirteen keys in the
    same order, measured on brekkylab.atlassian.net, 2026-09-17, on `space/{key}?expand=permissions`,
    `content?expand=version` and `content/{id}/child/comment?expand=version,history`.

    Four of the thirteen are constants on the site measured and a corpus states nothing that could
    vary them: `isExternalCollaborator`, `isGuest`, `accountStatus` and `_expandable`. `locale` is
    real's ACCOUNT language setting (`"ko"` there, and it is what the error messages follow), which
    is a property of the reader rather than of the corpus, so it is one value here.

    ``site`` is the caller's own base (:func:`_site`) because `_links.self` is an address a client
    follows — the shape `github._gh_user` takes `_api_base(request)` for.
    """
    aid = synth.atlassian_account_id(email or "unknown")
    name = (email or "unknown").split("@")[0]
    return {
        "type": "known",
        "accountId": aid,
        "accountType": "atlassian",
        "email": email,
        "publicName": name,
        "profilePicture": {
            "path": f"/wiki/aa-avatar/{aid}",
            "width": 48,
            "height": 48,
            "isDefault": False,
        },
        "displayName": name.replace(".", " ").title(),
        "isExternalCollaborator": False,
        "isGuest": False,
        "locale": "en",
        "accountStatus": "active",
        "_expandable": {"operations": "", "personalSpace": ""},
        "_links": {"self": f"{site}/wiki/rest/api/user?accountId={aid}"},
    }


def _confluence_ts(updated_ts, created_ts, cid) -> int:
    """A page's last-modified second: its own, else its creation second, else one seeded from its
    id. One helper for the two places that need it, because the CQL result and the page body must
    date the same page the same way — and because reaching for a jira column here (`key`) raised
    IndexError on the search route while the page route was fine.

    `is not None` at each step: 1970-01-01T00:00:00Z stores as 0, and a page that HAS a second
    must serve it rather than a seeded one."""
    if updated_ts is not None:
        return updated_ts
    if created_ts is not None:
        return created_ts
    return synth.epoch(str(cid))


def _confluence_page(conn, request: Request, row, expand: str) -> dict:
    created = row["created_ts"] if row["created_ts"] is not None else synth.epoch(str(row["id"]))
    updated = row["updated_ts"] if row["updated_ts"] is not None else created
    cid = row["id"]
    key = synth.confluence_space_key(row["space"])
    author = row["author_email"]
    ctype = row["subtype"] or "page"  # page | blogpost
    # version number: BYO override, else 2 if the page was updated after creation, else 1
    vnum = row["version_number"] or (2 if row["updated_ts"] and row["updated_ts"] != created else 1)
    webui = f"/spaces/{key}/{ctype}s/{cid}"
    page = {
        "id": str(cid),
        "type": ctype,
        "status": "current",
        "title": row["title"],
        "space": {
            "id": synth.github_user_id(row["space"]),
            "key": key,
            "name": row["space"],
            "type": "global",
            "_links": {"webui": f"/spaces/{key}"},
        },
        "_links": {
            "webui": webui,
            "tinyui": f"/x/{cid}",
            "editui": f"/pages/resumedraft.action?draftId={cid}",
            "self": f"{_site(request)}/wiki/rest/api/content/{cid}",
        },
        "_expandable": {
            "childTypes": "",
            "container": f"/rest/api/space/{key}",
            "metadata": "",
            "operations": "",
            "restrictions": "",
            "history": f"/rest/api/content/{cid}/history",
            "ancestors": "",
            "body": "",
            "version": "",
            "descendants": "",
        },
    }
    if "history" in expand or "version" in expand:
        page["history"] = {
            "latest": True,
            "createdDate": synth.rfc3339_millis(created),
            "createdBy": _conf_user(author, _site(request)),
            "lastUpdated": {
                "when": synth.rfc3339_millis(updated),
                "by": _conf_user(author, _site(request)),
                "number": vnum,
            },
        }
    if "version" in expand:
        page["version"] = {
            "number": vnum,
            "when": synth.rfc3339_millis(updated),
            "by": _conf_user(author, _site(request)),
            "minorEdit": bool(row["minor_edit"]),
            "message": row["version_message"] or "",
        }
    if "body.storage" in expand:
        page.setdefault("body", {})["storage"] = {
            "value": _storage(row["content"]),
            "representation": "storage",
        }
    if "body.view" in expand:
        page.setdefault("body", {})["view"] = {
            "value": _view(row["content"]),
            "representation": "view",
        }
    if "body.export_view" in expand:
        page.setdefault("body", {})["export_view"] = {
            "value": _export_view(row["content"]),
            "representation": "export_view",
        }
    if "body.atlas_doc_format" in expand:
        import json as _json

        page.setdefault("body", {})["atlas_doc_format"] = {
            "value": _json.dumps(_adf(row["content"])),
            "representation": "atlas_doc_format",
        }
    if "metadata.labels" in expand or "metadata" in expand:
        labels = store.jcol(row, "labels")
        page["metadata"] = {
            "labels": {
                "results": [
                    {
                        "prefix": "global",
                        "name": lbl,
                        "id": str(synth.confluence_id(lbl)),
                        "label": lbl,
                    }
                    for lbl in labels
                ],
                "start": 0,
                "limit": 200,
                "size": len(labels),
            }
        }
    if "ancestors" in expand:
        ancestors, pid = [], row["parent_id"]
        while pid:
            prow = store.get_document(conn, "confluence", pid)
            if prow is None:
                break
            pcid = prow["id"]
            ancestors.insert(
                0,
                {
                    "id": str(pcid),
                    "type": prow["subtype"] or "page",
                    "status": "current",
                    "title": prow["title"],
                    "_links": {"webui": f"/spaces/{key}/pages/{pcid}"},
                },
            )
            pid = prow["parent_id"]
        page["ancestors"] = ancestors
    return page


# What real converts a query parameter with. Measured on brekkylab.atlassian.net, 2026-09-14,
# against Jira's comment read and Confluence's space listing, which agree on every case:
#
#   ?maxResults=          the default, as though unsent -- an empty value is not a failure
#   ?maxResults=%2B3      3, so a leading sign is read (a RAW `+` decodes to a space and reaches
#                         the binder as ` 3`, which reads 3 by the whitespace removal below)
#   ?maxResults=%203%20   3, and ?maxResults=3%204 is 34 -- whitespace is REMOVED, not trimmed
#   ?maxResults=<U+0663>  3, so the digits are Unicode's, not ASCII's
#   ?maxResults=1_0       400 -- where Python's own int() reads 10
#   ?maxResults=1.5       400
#
# Python's `int` agrees with all of it but three things — the underscore, the internal whitespace,
# and the non-breaking spaces `strip_java_whitespace` exists for — so those three are handled here
# and the rest is left to `int` rather than restated as a pattern that would then have to be kept
# in step with it.
#
# The products part on TWO cases, both of them a value that cleans away to nothing:
#
#   - whitespace and nothing else. Jira reads it as absent and answers 200 with the default
#     (`?maxResults=%20` and `?maxResults=%09` both); Confluence cleans it to the empty string and
#     fails to convert THAT, reporting `For input string: ""`. A genuinely empty `?limit=` is the
#     default on both, so it is the whitespace, not the emptiness, that separates them.
#   - an empty FIRST value of a repeated parameter. Jira takes the default (`?startAt=&startAt=5`);
#     Confluence refuses, naming `",5"`. See :func:`_int_param`.
#
# The WIDTH is per parameter, not per product, and measured one parameter at a time: Jira's
# `startAt` takes a Java long (`2147483648` and `9223372036854775807` are both echoed back, and
# only past a long is it refused), while its `maxResults` and both Confluence parameters are a Java
# int. Reading one width off another is what put a 400 on `?startAt=-2147483649`, which real
# answers 200.
JAVA_INT = (-(2**31), 2**31 - 1)
JAVA_LONG = (-(2**63), 2**63 - 1)


def _int_param(
    request: Request, name: str, default: int, *, width: tuple[int, int] = JAVA_INT
) -> int:
    """One query parameter as an int, refused the way real refuses it.

    The FIRST value when the parameter repeats, not the last: `?startAt=3&startAt=5` is 3 on both
    products, where Starlette's ``QueryParams.get`` returns 5. A client that appends to a URL
    rather than replacing in it — a retry layer adding `startAt` to a URL that already carries one
    is the ordinary way — was being served real's other page under a 200.

    A value that converts to nothing is where the products part, and they part twice. A LONE empty
    value is the default on both (`?limit=` and `?maxResults=` are each 200). A first value that is
    empty or whitespace-only is the default on Jira (`?startAt=&startAt=5` and `?maxResults=%20`
    are 200 with it) and, on Confluence, the default only when the parameter does not repeat:
    `?limit=&limit=5` is a 400 naming `",5"`, the array with an empty element in front.
    """
    values = request.query_params.getlist(name)
    if not values:
        return default
    confluence = errors_atlassian.is_confluence(request.url.path)
    if values[0] == "" and (len(values) == 1 or not confluence):
        return default
    raw = values[0]
    cleaned = errors_atlassian.strip_java_whitespace(raw)
    if cleaned == "" and not confluence:
        return default  # Jira alone reads a whitespace-only value as absent
    try:
        if "_" in cleaned:
            raise ValueError(cleaned)
        if any(ch.isspace() for ch in cleaned):
            # Only the three non-breaking spaces survive the cleaning, and Java throws on one
            # WHEREVER it sits, while Python's `int()` strips a leading or trailing one itself
            # (`int('\xa03')` is 3). Without this, only the interior spelling was refused.
            raise ValueError(cleaned)
        n = int(cleaned)
    except ValueError:
        raise errors_atlassian.integer_conversion_failure(
            _echoed_path(request), name, values
        ) from None
    if not width[0] <= n <= width[1]:
        # Python's int is unbounded, so a page size computed from a timestamp or a byte count
        # flowed into the query where real answered 400.
        raise errors_atlassian.integer_conversion_failure(_echoed_path(request), name, values)
    return n


# The words `_bool_param` reads, and the value each one stands for.
_JIRA_QUERY_BOOLEANS = {
    "true": True,
    "on": True,
    "yes": True,
    "1": True,
    "false": False,
    "off": False,
    "no": False,
    "0": False,
}


def _bool_param(request: Request, name: str, default: bool | None = None) -> bool | None:
    """One query parameter as a boolean, read the way Jira reads `search/jql`'s `fieldsByKeys` and
    `failFast` (Jira Cloud, 2026-10-12 and 2026-10-13).

    The first value is the one read. What Java's `String.trim` removes comes off it, every character
    up to U+0020 at either end (`%01true` is read), and no character past U+0020 does (`%C2%A0true`
    and `%E2%80%80true` are refused). What is left matches a key of :data:`_JIRA_QUERY_BOOLEANS` in
    any case, compared the way Java's `equalsIgnoreCase` compares, which also reads `ſ` (U+017F) as
    `s`. A lone empty value is the default. A value the trim empties is refused (`%20`, `%01`), and
    so is an empty first value of a repeated parameter (`?fieldsByKeys=&fieldsByKeys=yes`).
    """
    values = request.query_params.getlist(name)
    if not values or values == [""]:
        return default
    word = values[0].strip(_JAVA_TRIMMED).lower().replace("\u017f", "s")
    if word not in _JIRA_QUERY_BOOLEANS:
        raise errors_atlassian.integer_conversion_failure(_echoed_path(request), name, values)
    return _JIRA_QUERY_BOOLEANS[word]


def _jira_long_list_param(request: Request, name: str) -> list[int]:
    """Every value of a repeated query parameter as a Java `long`, as Jira converts the
    `reconcileIssues` of a `search/jql` GET (Jira Cloud, 2026-10-12).

    `?reconcileIssues=1&reconcileIssues=2` is served, and a value that will not convert is refused
    wherever it sits: `1&a` names the array as `a&1` does, where :func:`_int_param` reads the first
    value alone. A value :func:`errors_atlassian.strip_java_whitespace` empties is passed over, as
    in `?reconcileIssues=%20&reconcileIssues=1`. The rest convert by :func:`_int_param`'s rules, so
    `1,2` is refused rather than split.
    """
    values = request.query_params.getlist(name)
    numbers = []
    for raw in values:
        cleaned = errors_atlassian.strip_java_whitespace(raw)
        if cleaned == "":
            continue
        try:
            if "_" in cleaned or any(ch.isspace() for ch in cleaned):
                raise ValueError(cleaned)
            n = int(cleaned)
        except ValueError:
            n = None
        if n is None or not JAVA_LONG[0] <= n <= JAVA_LONG[1]:
            raise errors_atlassian.integer_conversion_failure(_echoed_path(request), name, values)
        numbers.append(n)
    return numbers


def _reject_json_constant(value: str):
    """A `parse_constant` hook. Python's decoder reads `NaN`, `Infinity` and `-Infinity`, which real
    refuses as malformed JSON (measured 2026-10-06, `-Infinity` 2026-10-09), so raising here sends
    them to the parse error `_jira_search_body` answers a body it cannot parse with.
    """
    raise ValueError(value)


class _JiraOversizedNumber:
    """A number literal of more than 1000 digits, left unconverted for the body checks to refuse."""

    def __init__(self, raw: str):
        self.raw = raw


def _jira_json_number(read, oversized: list[_JiraOversizedNumber]):
    """A `parse_int` / `parse_float` hook that counts a literal's digits before `read` sees it, and
    returns a literal past 1000 as a `_JiraOversizedNumber` it also appends to `oversized`.

    Real counts every digit, the fraction's and the exponent's included, and reads a literal of up
    to 1000. As `maxResults`, `1.` and 998 digits then `e1` is served, while `1.` and 999 then
    `e1`, `0.` and 999 zeros then `1`, and 1000 digits then `.5` get `Failed to read request`, as
    integers of 1001, 4300, 4301 and 5000 digits do (Jira Cloud, 2026-10-06 and 2026-10-09).
    Counting here also keeps a literal past Python's own 4300-digit `int` limit away from `int()`.
    Which refusal such a literal draws depends on where it sits; see :func:`_jira_search_body`.
    """

    def parse(raw: str):
        if sum(ch.isdigit() for ch in raw) > 1000:
            number = _JiraOversizedNumber(raw)
            oversized.append(number)
            return number
        return read(raw)

    return parse


def _refuse_a_search_media_type(request: Request) -> None:
    """Raises ``errors.atlassian.unsupported_media_type`` for a `POST search/jql` whose
    `Content-Type` is not JSON.

    Measured 2026-09-15. The `Content-Type` is checked first and on its own: a perfectly good JSON
    body sent without the header is the same 415 as one sent with `text/plain`, so the header
    decides before the bytes are looked at. The match is on the media type alone, case-insensitively
    and ignoring parameters — `APPLICATION/JSON` and `application/json; charset=utf-8` are both
    read, `*/*` and `application/xml` are not. :func:`_checked_before_the_route` has its place.
    """
    content_type = request.headers.get("content-type")
    if (content_type or "").split(";")[0].strip().lower() != "application/json":
        raise errors_atlassian.unsupported_media_type(_echoed_path(request), content_type)


async def _jira_search_body(request: Request) -> dict:
    """The `search/jql` request body, or the refusal real gives for one it will not read.

    Measured 2026-09-15. Its `Content-Type` has been read by then
    (:func:`_refuse_a_search_media_type`); the body itself real sorts into three sentences (a number
    too long to read aside, below), and the boundaries between them are not where a JSON parser
    would draw them:

    - a body of zero length, and a literal `null`, are "no content"
    - bytes that do not parse are a parse error — but a body that is only WHITESPACE is not, it is
      the not-an-object sentence, which is why emptiness here means length and not `strip()`
    - JSON that parses to anything but an object — `[]`, `5`, `"x"`, `true` — is not-an-object

    Trailing bytes after a complete value are IGNORED rather than refused: `{"jql": …} junk` is
    answered 200. That is what `raw_decode` reproduces and `json.loads` would not, reading the
    first value and letting the rest go.

    The leading bytes are the other side of that and are NOT ignored so freely. JSON's whitespace is
    the four ASCII ones, so a non-breaking space in front of the object is the parse error, where
    `str.lstrip()` would skip it and read the object behind it. Bytes that are not UTF-8 are the
    not-an-object sentence, where `errors="replace"` would repair them into U+FFFD and parse.

    Python's decoder is constrained in three places where it reads differently from real. `NaN` and
    `Infinity` are the parse error (see :func:`_reject_json_constant`). A number literal past 1000
    digits (see :func:`_jira_json_number`) is real's problem+json `Failed to read request` when it
    is the body or the value of a member, and the not-an-object sentence when it sits inside a list
    or an object. Both hold whichever member it is, an undeclared one included (measured on each
    member `_JIRA_SEARCH_BODY_KEYS` declares, on `bogus`, and on a body of the number alone or in a
    list, 2026-10-09 and 2026-10-10). Nesting deep enough to exhaust Python's reader is the
    not-an-object sentence, the refusal real gives such a body at every depth measured.

    Real reads the members in order and answers the first one it cannot read or bind, so
    `{"bogus": 1, "jql": <1001 digits>}` is the not-an-object sentence there, and the other way
    round it is `Failed to read request` (2026-10-10). This reader refuses an oversized literal
    before it looks at any member, whatever the order.
    """
    raw = await request.body()
    if not raw:
        raise errors_atlassian.body_not_read(errors_atlassian.BODY_EMPTY)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise errors_atlassian.body_not_read(errors_atlassian.BODY_NOT_AN_OBJECT) from None
    oversized: list[_JiraOversizedNumber] = []
    try:
        parsed, _end = json.JSONDecoder(
            parse_constant=_reject_json_constant,
            parse_int=_jira_json_number(int, oversized),
            parse_float=_jira_json_number(float, oversized),
        ).raw_decode(text.lstrip(" \t\n\r"))
    except RecursionError:
        # Real refuses a `fields` nested 998 to 300000 deep as a payload it cannot bind (measured
        # 2026-10-06), so the depth Python's reader gives out at must not change the answer.
        raise errors_atlassian.body_not_read(errors_atlassian.BODY_NOT_AN_OBJECT) from None
    except ValueError:
        message = (
            errors_atlassian.BODY_NOT_AN_OBJECT
            if not raw.strip()
            else errors_atlassian.BODY_UNPARSEABLE
        )
        raise errors_atlassian.body_not_read(message) from None
    if parsed is None:
        raise errors_atlassian.body_not_read(errors_atlassian.BODY_EMPTY)
    members = parsed.values() if isinstance(parsed, dict) else [parsed]
    if any(isinstance(value, _JiraOversizedNumber) for value in members):
        raise errors_atlassian.failed_to_read_request(_echoed_path(request))
    if oversized or not isinstance(parsed, dict):
        raise errors_atlassian.body_not_read(errors_atlassian.BODY_NOT_AN_OBJECT)
    return parsed


def _str_param(request: Request, name: str, default: str | None = None) -> str | None:
    """One query parameter as a string, comma-joined when it repeats.

    Measured on three parameters across both products: Jira's `?orderBy=bogus&orderBy=created` is
    refused naming `bogus,created` in either order, its repeated `jql` is refused with a parse error
    at the character the comma lands on, and Confluence's `?spaceKey=NOPE1&spaceKey=NOPE2` is a 404
    naming `NOPE1,NOPE2`. So a repeated string is one value with a comma in it, and reading the last
    alone answers 200 to whichever ordering happens to put a valid spelling second.

    The join is what is reproduced here, not every refusal that follows from it: a repeated `jql`
    reaches `_project_from_jql`, which is lenient where real's JQL parser is not, so Backlot answers
    200 to a joined query real refuses. That leniency is the parser's and predates this.
    """
    values = request.query_params.getlist(name)
    return ",".join(values) if values else default


def _confluence_page_params(
    request: Request,
    *,
    default: int = 25,
    cap: int | None = None,
    refuse_zero: bool = False,
) -> tuple[int, int]:
    """Confluence's `limit` and `start`, which refuse a negative where Jira's clamp one.

    Measured on the five routes that call it, `space` on 2026-09-14, `child/page`, `child/comment`
    and `label` on 2026-09-23 and `child/attachment` on 2026-10-10: `?limit=-1` and `?start=-1` are
    400. Unclamped, a listing would hand them to SQLite, which reads a negative LIMIT as no limit at
    all and so would answer `?limit=-1` with the whole collection. `content` reads the pair itself,
    since it puts refusals of its own between these (``_CONTENT_TYPES``).

    The three `child/*` listings call it before they look the page up, so on an id naming nothing
    its refusals come ahead of the 404: `?limit=abc` and `?limit=-1` are its 400s there. Measured
    on `child/attachment` 2026-10-10 and on `child/page` and `child/comment` 2026-10-16. `label`
    converts the pair before its lookup and calls this after it (:func:`confluence_labels`).

    Order is measured too, because both parameters can be wrong at once. Conversion comes first for
    BOTH — `?limit=-1&start=abc` is the conversion failure about `abc`, not the negative about
    `limit` — and among two negatives `start` is the one named: `?limit=-1&start=-1` reports
    `start cannot be less than zero`.

    The CQL search reads its own pair (:func:`_cql_page_param`), JAX-RS-bound rather than Spring's,
    and shares :func:`_refuse_negative_page_params`, which is measured on that route too.

    ``refuse_zero`` is `label`'s alone: it answers `?limit=0` with a 400 where every other listing
    answers an empty page (:func:`backlot.errors.atlassian.zero_limit_not_allowed`). It is reached
    after both refusals above, measured 2026-09-23: `?limit=0&start=abc` is the conversion failure
    and `?limit=0&start=-1` the negative about `start`.

    ``default`` and ``cap`` are per route, measured 2026-09-22 on a live site: `content`, `space`
    and `child/comment` cap `limit` at 1000, `label` defaults to 200 and caps there, `child/page`
    defaults to 25 and caps nowhere (`?limit=1001` is echoed), `child/attachment` defaults to 50
    and caps nowhere, and the CQL search caps nowhere either. A value above the cap is answered
    with the cap rather than refused, so a client asking for more than real serves gets real's page
    size back.
    """
    limit = _int_param(request, "limit", default)
    start = _int_param(request, "start", 0)
    _refuse_negative_page_params(limit, start)
    if refuse_zero and limit == 0:
        raise errors_atlassian.zero_limit_not_allowed()
    if cap is not None:
        limit = min(limit, cap)
    return limit, start


# What Java's `String.trim()` removes: every character up to and including the space.
_JAVA_TRIMMED = "".join(map(chr, range(0x21)))


# CQL as real's reader splits it, measured on Confluence Cloud on 2026-10-22 and 2026-10-25, the
# second time with `title~a<c>b` and `title~a<c>` for each ASCII punctuation character `<c>`: a
# quoted value in either quotes, in which a backslash keeps the next character; an operator, a
# bracket or a comma; a date such as `2026-01-01` and a number, both in ASCII digits; and a word of
# ASCII letters, digits and `_`, dotted as `space.key` is. A control character, one past ASCII and
# those in `DROPPED` are left out as a space is: `title ~ probe*` is `title ~ probe`, `title~a#b`
# reads `a` and then `b`, and on 2026-10-26 `title~` and U+0663 (an Arabic-Indic three) or U+FF11 (a
# fullwidth one) had no value. Each other character is a token no clause takes, so `label=a-b` and
# `title~a:b` cannot be parsed.
_CQL_TOKEN = re.compile(
    r"""(?P<QUOTED>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
    r"|(?P<OP>!=|!~|>=|<=|=|~|>|<)"
    r"|(?P<PUNCT>[(),])"
    r"|(?P<DATE>[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r"|(?P<NUMBER>-[0-9]+|[0-9]+(?![A-Za-z_.]))"
    r"|(?P<WORD>[A-Za-z0-9_]+(?:[.][A-Za-z0-9_]+)*)"
    r"|(?P<DROPPED>[\s#$%&*+;?@^`{|}\x00-\x1f\x7f]|[^\x00-\x7f])"
    r"|(?P<OTHER>.)",
    re.DOTALL,
)
_CQL_OPERATORS = frozenset(("!=", "!~", ">=", "<=", "=", "~", ">", "<"))
_CQL_KEYWORDS = frozenset(("and", "or", "not", "in", "order", "by", "asc", "desc"))
_CQL_VALUES = frozenset(("WORD", "QUOTED", "NUMBER", "DATE"))


def _cql_tokens(cql: str) -> list[tuple[str, str]]:
    """The tokens of ``cql`` as ``(kind, text)``, those `_CQL_TOKEN` leaves out dropped."""
    return [(m.lastgroup, m.group()) for m in _CQL_TOKEN.finditer(cql) if m.lastgroup != "DROPPED"]


def _cql_written_back(tokens: list[tuple[str, str]]) -> str:
    """``cqlQuery`` as real writes the query back: its tokens one space apart, in the case and the
    quotes they were sent in, and each value of `space.key` in single quotes, in place of its own
    (Confluence Cloud, 2026-10-22 and 2026-10-25)."""
    out, key = [], False
    for i, (kind, text) in enumerate(tokens):
        before = tokens[i - 1] if i else (None, None)
        if kind == "OP" or kind == "WORD" and text.lower() == "in":
            field = i - 1 - (kind == "WORD" and i > 1 and before[1].lower() == "not")
            key = field >= 0 and tokens[field][1].lower() == "space.key"
        elif kind in ("WORD", "QUOTED") and key and (before[0] == "OP" or before[1] in "(,"):
            text = f"'{text[1:-1]}'" if kind == "QUOTED" else f"'{text}'"
        elif before[1] == ")" or kind == "WORD" and text.lower() in ("and", "or"):
            key = False
        out.append(text)
    return " ".join(out)


def _cql_refusal(tokens: list[tuple[str, str]]):
    """The 400 real answers a CQL it cannot read with, or None for one this server reads, by where
    the reading stops: ``errors.atlassian.cql_not_parsed`` at no clause at all, a token no clause
    takes, a `not` anywhere but straight after `and`, `order by` with no clause before it, a list
    that is empty or ends in a comma, and anything left after `order by`; ``cql_expecting_value`` at
    an operator followed by nothing or by a token that is no value, and at an `in` list never
    closed; ``cql_missing_closing_parenthesis`` at a `(` never closed; and
    ``cql_unrecognised_clause`` at words or quoted values left after a whole query. Each one's
    docstring gives the queries that answered it. A field CQL does not have, a field with no
    operator, and an operator or a value the field does not take (`foo`, `type`, `type<page`,
    `type=PAGE`) are refused on real with messages of their own, and answered at 200 here."""
    at = 0

    def peek(offset=0):
        return tokens[at + offset] if at + offset < len(tokens) else (None, None)

    def keyword(word, offset=0):
        kind, text = peek(offset)
        return kind == "WORD" and text.lower() == word

    class _Stop(Exception):
        def __init__(self, refusal):
            self.refusal = refusal

    def not_parsed():
        return _Stop(errors_atlassian.cql_not_parsed())

    def value(operator, field):
        nonlocal at
        kind, text = peek()
        if kind is None:
            raise _Stop(errors_atlassian.cql_expecting_value(operator, field))
        if kind == "NUMBER" and text.startswith("-"):  # `id = -1`, `created > -1d`
            raise not_parsed()
        if kind not in _CQL_VALUES or text.lower() in _CQL_KEYWORDS:
            raise _Stop(errors_atlassian.cql_expecting_value(operator, field, text))
        at += 1
        if peek() == ("PUNCT", "("):  # a function: now("-1d"), currentUser()
            at += 1
            while peek() != ("PUNCT", ")"):
                if peek()[0] is None:
                    raise not_parsed()
                at += 1
            at += 1

    def clause():
        nonlocal at
        if peek() == ("PUNCT", "("):
            at += 1
            sequence()
            if peek() != ("PUNCT", ")"):
                raise _Stop(errors_atlassian.cql_missing_closing_parenthesis())
            at += 1
            return
        kind, field = peek()
        if kind != "WORD" or field.lower() in _CQL_KEYWORDS:
            raise not_parsed()
        at += 1
        negated = keyword("not") and keyword("in", 1)
        if negated or keyword("in"):
            operator = "".join(text for _, text in tokens[at : at + 1 + negated])
            at += 1 + negated
            if peek() != ("PUNCT", "("):
                raise _Stop(errors_atlassian.cql_expecting_value(operator, field, peek()[1]))
            at += 1
            while True:
                if peek()[0] is None:
                    raise _Stop(errors_atlassian.cql_expecting_value(operator, field, "("))
                if peek()[0] not in _CQL_VALUES:
                    raise not_parsed()
                value(operator, field)
                if peek() != ("PUNCT", ","):
                    break
                at += 1
            if peek()[0] is None:
                raise _Stop(errors_atlassian.cql_expecting_value(operator, field, "("))
            if peek() != ("PUNCT", ")"):
                raise not_parsed()
            at += 1
            return
        if peek()[0] != "OP":
            raise _Stop(None)  # a field and no operator: refused on real with a message of its own
        operator = peek()[1]
        at += 1
        value(operator, field)

    def sequence():
        nonlocal at
        clause()
        while keyword("and") or keyword("or"):
            conjunction = peek()[1].lower()
            at += 1
            if conjunction == "and" and keyword("not"):
                at += 1
            if peek()[0] is None:
                raise not_parsed()
            clause()

    try:
        if not tokens:
            raise not_parsed()
        sequence()
        rest = tokens[at:]
        if rest and not (keyword("order") and keyword("by", 1)):
            if all(kind in ("WORD", "QUOTED") for kind, _ in rest) and not any(
                text.lower() in _CQL_KEYWORDS for _, text in rest
            ):
                raise _Stop(
                    errors_atlassian.cql_unrecognised_clause("".join(text for _, text in rest))
                )
            raise not_parsed()
        if rest:
            at += 2
            while True:
                kind, text = peek()
                if kind != "WORD" or text.lower() in _CQL_KEYWORDS:
                    raise not_parsed()
                at += 1
                if keyword("asc") or keyword("desc"):
                    at += 1
                if peek() != ("PUNCT", ","):
                    break
                at += 1
            if at < len(tokens):
                raise not_parsed()
    except _Stop as stop:
        return stop.refusal
    return None


def _cql_page_param(request: Request, name: str, default: int) -> int | None:
    """The CQL search's `limit` or `start`, read the way its JAX-RS binding reads an `int`, or
    ``None`` where real answers the 404 with no body and no `content-type` a `@QueryParam` it
    cannot convert gets.

    Measured 2026-10-04 on a live site, with an `Accept` that takes JSON: the first of a repeated
    parameter is the one read (`limit=5&limit=abc` is five, `limit=abc&limit=5` the 404); an empty
    value or one `trim()` empties (a space, a tab, a newline, and on 2026-10-05 `%01` and `%1F`
    alone) is the default; anything else is `Integer.valueOf` of the value as sent, so a sign and
    leading zeros pass and a Unicode decimal digit counts (`٥` and `５` are five) unless it lies
    past U+FFFF, which Java reads as two halves of a surrogate pair (`𝟓` is the 404), where a space
    beside the digits, `1.5`, `1_0`, `0x10`, `1e3` and a value outside `int` are the 404 too. The
    404 comes after the credential's refusal and before every other one the route makes. A NUL
    never gets this far: Tomcat answers it with an HTML 400 page.
    """
    values = request.query_params.getlist(name)
    if not values or not values[0].strip(_JAVA_TRIMMED):
        return default
    if not re.fullmatch(r"[+-]?\d+", values[0]) or max(map(ord, values[0])) > 0xFFFF:
        return None
    value = int(values[0])
    return value if -(2**31) <= value < 2**31 else None


def _cql_search_after(token: str) -> list | None:
    """What a sent `cursor` says the page goes on after: a list holding the one sort value it
    carries, ``None`` for a token that reads as no cursor, or real's refusal of a token it cannot
    page by (:func:`backlot.errors.atlassian.search_cursor_refused`).

    Measured 2026-10-04 and 2026-10-05: a token is `_t_` and `_h_` around two base64 parts, either
    alphabet, each read as the first JSON value in it, leading whitespace skipped and whatever
    follows ignored, and real's own hold the sort value in a list and then `[]`. The 400 answers a
    token without `_t_` or `_h_`, a part that is not base64 or holds no JSON value, `NaN`,
    `Infinity` or `-Infinity` in either part, an `_h_` that is not a list or holds `null`, a list or
    an object, and a `_t_` that is neither a list nor `null` or holds two values. Past those, a
    `_t_` of `null` is no cursor, and the 500 answers an empty list or a list or an object as the
    value: `_t_` `[]` with `_h_` `[null]` is the 400 and with `_h_` `[1]` the 500. A string, a
    number, `true`, `false` and `null` as the value are served (:func:`_cql_position`). Both
    refusals come after the route's of `limit` and `start`: `?limit=abc&cursor=abc` is the 404 and
    `?limit=-1&cursor=abc` the negative 400.
    """
    parts = re.fullmatch(r"_t_(.*)_h_(.*)", token, re.S)
    first, then = (_cql_token_part(part) for part in parts.groups()) if parts else (_NO_JSON,) * 2
    if (
        not isinstance(then, list)
        or any(each is None or isinstance(each, (list, dict)) for each in then)
        or not (first is None or isinstance(first, list) and len(first) <= 1)
    ):
        raise errors_atlassian.search_cursor_refused()
    if first is not None and (not first or isinstance(first[0], (list, dict))):
        raise errors_atlassian.search_cursor_refused(failed=True)
    return first


# What `_cql_token_part` answers for a part holding no JSON value it reads.
_NO_JSON = object()


def _refuse_a_json_constant(name: str):
    """Refuses the three constants Python's JSON reads and real's does not
    (:func:`_cql_search_after`)."""
    raise ValueError(name)


def _cql_token_part(part: str):
    """One part of a CQL search cursor read as :func:`_cql_search_after` says real reads it, or
    ``_NO_JSON``, which a part nesting deeper than Python's JSON reader recurses gets as well."""
    standard = part.replace("-", "+").replace("_", "/") + "=" * (-len(part) % 4)
    decoder = json.JSONDecoder(parse_constant=_refuse_a_json_constant)
    try:
        raw = base64.b64decode(standard, validate=True)
        return decoder.raw_decode(raw.decode("utf-8").lstrip(" \t\n\r"))[0]
    except (ValueError, binascii.Error, RecursionError):
        return _NO_JSON


def _cql_position(matched: list, sort_value) -> int:
    """Where in ``matched`` the CQL search's page starts, given the value a sent cursor carries
    (:func:`_cql_search_after`): after the row whose id follows the tab a string value opens with,
    after every row for `null`, and at the first match for any other value.

    Measured 2026-10-04 on a nine-page site: `start` is echoed and advanced in the links but never
    positions the page, so `?limit=1&start=100001` serves the first match, and a cursor naming the
    second match serves the third onwards at any `start`. Real's own value is `"\\t<id> …"`, and it
    serves the rows whose value sorts after the one sent, in the descending order of those values it
    answers in, which puts a number, `true`, `false` and `null` where the summary above does. This
    server's matches are in an order of its own, so a string naming none of them starts at the first
    match, where real may answer an empty page or one from the middle (`"\\t3"` started at the sixth
    of nine).
    """
    if sort_value is None:
        return len(matched)
    named = re.match(r"\t(\S+)", sort_value) if isinstance(sort_value, str) else None
    for i, row in enumerate(matched if named else ()):
        if str(row["id"]) == named.group(1):
            return i + 1
    return 0


# The `start` past which `content` answers `start_too_large`. The same `start` is a 200 on the
# neighbours: an empty page on `space` and on `child/page`, and on the CQL search the page `start`
# does not position (:func:`_cql_position`).
_CONTENT_START_BOUND = 100_000
# The types `content?type=` knows, and what each answers. Measured on a Confluence Cloud site on
# 2026-10-10, each type alone, beside a known space, an unknown one and a title, and beside a
# `limit` or `start` below zero and a `start` past `_CONTENT_START_BOUND`, matched as sent (`Page`,
# `Blogpost` and `COMMENT` are no type). After the conversion 400, a known type refuses a `start`
# past the bound, where a type Confluence does not have never reads it; then an unknown space is the
# 404; then `comment` and `folder` are `content_finder_cannot_fetch`'s 501; then a negative `limit`
# or `start` is the 400; then `whiteboard`, `database` and `embed` are `not_a_custom_content_type`'s
# 400 and `attachment` is `attachment_without_container`'s 500, beside a space or a title as alone.
# A type not in this table is `unknown_content_type`'s 501 alone, and an empty page beside a known
# space or a title, whatever `start` is.
_CONTENT_TYPES = {
    "page": "listed",
    "blogpost": "listed",
    "comment": "unfetchable",
    "folder": "unfetchable",
    "whiteboard": "not_custom",
    "database": "not_custom",
    "embed": "not_custom",
    "attachment": "no_container",
}


def _cql_cursor(served: list, matched: list, position: int) -> str | None:
    """The `cursor` real's CQL search carries on `next`: a token naming the last row the page
    served, or on a page that served none the row it would have started with
    (:func:`_cql_position`).

    Measured 2026-09-23 on a nine-page site: the token names the second match at `limit=2` and the
    fifth at `limit=5`, and moves with each `next` followed. Measured 2026-10-04, an empty page
    names the first match when no cursor was sent, whatever `start` says, and the row after the sent
    one's when one was: `?limit=0` with a cursor naming the second match names the third. Real's
    token carries the row's sort value (:func:`_cql_search_after`); this builds one of its own from
    the row's id, so a client sees a token shaped like real's, and real reads this one back as it
    reads its own.
    """
    row = served[-1] if served else (matched[position] if position < len(matched) else None)
    if row is None:
        return None
    payload = base64.b64encode(f'["\\t{row["id"]}"]'.encode()).decode("ascii")
    return quote(f"_t_{payload}_h_W10=", safe="")


def _confluence_carried(
    request: Request,
    *,
    first: tuple[str, ...] = ("expand",),
    own: tuple[str, ...] = (),
    head: tuple[str, ...] = (),
) -> tuple[str, str, str]:
    """The request's other parameters, split into what leads everything else in a page link, what
    leads `limit`/`start` and what trails `start`.

    Measured 2026-09-17 and 2026-09-22: `expand` leads `limit`/`start`, and on `prev` it leads the
    marker as well. Where a name real does not read lands is a Java map's iteration order rather
    than a rule — `bogus` after the marker, `zebra` ahead of it, `nonce` and `cql` after `start`,
    each on its own request — so the names in ``first`` lead and everything else trails in the
    order the caller sent it, which reproduces the `cql` and `nonce` cases and not the other two.

    ``head`` names what a route's links carry ahead of all of that, the marker and `expand`
    included: `space` carries `spaceKey` there, on `next`, `prev` and `self` alike, whichever order
    it was sent in beside `expand` (measured 2026-10-12).

    ``own`` names what the route writes into its links itself, so it is not carried as well.

    Each parameter is carried as the request spelt it, name and value, from the raw query string.
    Measured on `content` and `space` on 2026-10-15: a bare or escaped comma, a lower-case `%2c`,
    `+`, `%7E`, `~`, `;` and an escaped name (`%65xpand`) came back as sent, a name sent without
    `=` came back with one, and an empty part or name was dropped. The names compared here are the
    spelt ones too, as real's are: `l%69mit=1` is read as `limit` and still carried, and
    `%65xpand` and `sp%61ceKey`, read as `expand` and `spaceKey`, land where other names do
    (measured 2026-10-16).
    """
    front, lead, trail = [], [], []
    for part in request.scope["query_string"].decode("latin-1").split("&"):
        name, _, value = part.partition("=")
        if not name or name in ("limit", "start", "next", "prev", *own):
            continue
        (front if name in head else lead if name in first else trail).append(f"{name}={value}")
    return (
        "&".join(front) + "&" if front else "",
        "&".join(lead) + "&" if lead else "",
        "&".join(trail),
    )


def _confluence_envelope(
    request: Request,
    route: str,
    *,
    start: int,
    limit: int,
    size: int,
    total: int,
    cursor: str | None = None,
    sent_cursor: str | None = None,
    reached: int | None = None,
    head: tuple[str, ...] = (),
) -> dict:
    """`_links` as every paged Confluence listing answers it: `base`, `context` and `self` on every
    page, plus `next`/`prev` from :func:`backlot.pagination.confluence_page_links`.

    Measured 2026-09-22 on `content`, `space`, the CQL `search`, `child/page`, `child/comment` and
    `label`, and 2026-10-10 on `child/attachment`: all three keys ride every page, `context` is the
    product's own prefix and `self` is the request's URL with `limit`, `start` and the two markers
    removed and every other parameter kept — a cache-buster sent with the request comes back inside
    `self`.

    ``cursor``, ``sent_cursor`` and ``reached`` are the CQL search's: the token this page's `next`
    carries, the one the request brought, and where among the matches the page ends, as
    :func:`backlot.pagination.confluence_page_links` counts it. Measured 2026-09-23 following `next`
    three hops at `limit=0`, `1` and `2`: the sent one is not carried the way other parameters are,
    so `self` holds no cursor and `next` the new one alone, and `prev` is where it goes back out.

    ``head`` is passed to :func:`_confluence_carried`, which says where those parameters ride.
    """
    front, lead, trail = _confluence_carried(
        request, own=() if sent_cursor is None else ("cursor",), head=head
    )
    query = "&".join(p for p in (front.rstrip("&"), lead.rstrip("&"), trail) if p)
    links = {
        "base": f"{_site(request)}/wiki",
        "context": "/wiki",
        "self": f"{_site(request)}/wiki{route}" + (f"?{query}" if query else ""),
    }
    sent = quote(sent_cursor, safe="") if sent_cursor else None
    links.update(
        confluence_page_links(
            route, start, limit, size, total, lead, trail, cursor, sent, reached, head=front
        )
    )
    return links


def _refuse_negative_page_params(limit: int, start: int) -> None:
    """Confluence's refusal of a negative page parameter, shared by every listing measured to give
    it. `start` is checked first because it is the one real names when both are negative."""
    for name, value in (("start", start), ("limit", limit)):
        if value < 0:
            raise errors_atlassian.negative_not_allowed(name)


# ======================== what answers before, and around, a route ==========================

#: Every method the catch-all below takes: the methods the application behind the real gateway ever
#: sees (``errors.atlassian.SERVED_METHODS``) but `HEAD`, which
#: ``backlot.main.answer_head_as_the_get_without_its_body`` turns into its GET before routing, so
#: that none reaches a route. Every method outside `SERVED_METHODS` is left off because a layer in
#: front of the application refuses it, which ``backlot.main.answer_at_the_atlassian_front_door``
#: answers before routing (``errors.atlassian.front_door``).
_UNMATCHED_METHODS = [m for m in errors_atlassian.SERVED_METHODS if m != "HEAD"]

#: The Confluence resources whose unmatched sub-paths real answers with the product's HTML page
#: rather than the API's 404, measured 2026-09-22. `space/MFS/nope`, `space/nope/deeper`,
#: `content/65851/nope`, `content/65851/child/page/nope` and `content/nope/deeper` are the page;
#: `search/nope`, `settings/nope`, `audit/nope` and a first segment no resource claims
#: (`nopesuchroute`, `nope/deeper/still`) are the JAX-RS 404. The split is which Java service owns
#: the prefix, not whether the resource exists — a space key no site has is the page too — so the
#: two families Backlot serves are named here and everything else takes the API's own 404.
_CONFLUENCE_HTML_RESOURCES = ("space", "content")

unmatched_router = APIRouter(prefix="/atlassian", include_in_schema=False)


class _EveryPath(PathConvertor):
    """Starlette's `path`, a newline included: its `.*` stops at one, so `GET /rest/api/3/nope%0Ax`
    would miss the catch-all, where real answers it as any path no route matches (2026-10-18)."""

    regex = "(?s:.*)"


register_url_convertor("every_path", _EveryPath())

_PACKAGE = Path(__file__).resolve().parent.parent


def _operations(rows) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """(method, vendor-path pattern) for each `METHOD /path` row, each `{}` one path segment."""
    operations = []
    for row in rows:
        method, path = row.split(" ", 1)
        pattern = "[^/]+".join(re.escape(part) for part in path.split("{}"))
        operations.append((method, re.compile(pattern)))
    return tuple(operations)


def _published_not_served(source: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """The operations the vendor publishes and no route here serves: the baseline's ``missing``
    operations, read off the file as ``backlot.published`` reads them, so an operation the vendor
    adds reaches :func:`unmatched_path` once the baseline acknowledges it; a route added here wins
    over :func:`unmatched_path` whatever the file says."""
    baseline = _PACKAGE / "fidelity" / "baseline" / f"{source}.json"
    entries = json.loads(baseline.read_text(encoding="utf-8"))["operations"]
    return _operations(e["operation"] for e in entries if e["status"] == "missing")


_JIRA_PUBLISHED = _published_not_served("jira")
_CONFLUENCE_PUBLISHED = _published_not_served("confluence")
#: What real answers before each Jira operation of those runs, operation by operation;
#: ``scripts/gen_jira_unserved.py`` writes the file from Jira's documents and says how that was
#: measured.
_JIRA_UNSERVED = json.loads((_PACKAGE / "data" / "jira_unserved.json").read_text(encoding="utf-8"))
_JIRA_ENTRIES = {**_JIRA_UNSERVED["refused"], **_JIRA_UNSERVED["run"]}
_JIRA_OPERATIONS = tuple(
    (row, method, pattern)
    for row, (method, pattern) in zip(_JIRA_ENTRIES, _operations(_JIRA_ENTRIES), strict=True)
)
#: The operations at a served route's path whose own lookup real runs next: an issue's `PUT` and
#: `DELETE` and its comments' `POST` reach :func:`_require_issue`, and a project role's three writes
#: reach :func:`_require_project_admin`, so a key naming nothing the caller sees is that resource's
#: 404. Measured on Jira Cloud 2026-10-12 (the issue's under `/rest/api/2` 2026-09-30),
#: credentialled and not.
_JIRA_ISSUE_WRITES = frozenset(
    f"{method} /rest/api/{version}/issue/{{}}{tail}"
    for version in ("2", "3")
    for method, tail in (("PUT", ""), ("DELETE", ""), ("POST", "/comment"))
)
_JIRA_ROLE_WRITES = frozenset(
    f"{method} /rest/api/3/project/{{}}/role/{{}}" for method in ("POST", "PUT", "DELETE")
)


def _jira_operation(method: str, vendor_path: str) -> str | None:
    """The operation of :data:`_JIRA_ENTRIES` a request names, or None.

    Of the ones its method and path match, the one with the fewest placeholders: `PUT issue/archive`
    is an operation of its own that `PUT issue/{}` also matches, and real answers it as itself (a
    400 for an empty body, 2026-10-12, where the issue's would be its 404).
    """
    matches = [
        row
        for row, verb, pattern in _JIRA_OPERATIONS
        if verb == method and pattern.fullmatch(vendor_path)
    ]
    return min(matches, key=lambda row: row.count("{}"), default=None)


_CONFLUENCE_NOT_PERMITTED_GETS = (
    "api/audit",
    "api/audit/export",
    "api/audit/retention",
    "api/audit/since",
    "api/content-states",
    "api/longtask",
    "api/longtask/{}",
    "api/relation/{}/from/{}/{}/to/{}",
    "api/relation/{}/from/{}/{}/to/{}/{}",
    "api/relation/{}/to/{}/{}/from/{}",
    "api/search/user",
    "api/settings/lookandfeel",
    "api/template/blueprint",
    "api/template/page",
    "api/template/{}",
    "api/user/watch/content/{}",
    "api/user/watch/label/{}",
    "api/user/watch/space/{}",
    "atlassian-connect/1/app/module/dynamic",
)

#: The Confluence writes its JAX-RS services answer, as opposed to its Spring ones: measured
#: 2026-10-13 with no credential on each of the 65 writes the Confluence baseline lists as
#: `missing_operation`, these 23 answered a JSON body or none with
#: ``errors.atlassian.CONFLUENCE_NOT_PERMITTED_BODY`` (but the two that ran,
#: :data:`_CONFLUENCE_RUN_ANONYMOUSLY`), another type with an empty 415 (but the three under
#: `settings/lookandfeel`, :data:`_CONFLUENCE_JAXRS_UNTYPED`), and a `Content-Type` Jersey cannot
#: read (`foo`) with Tomcat's 400 page, the two that ran included
#: (``errors.atlassian.jaxrs_media_type_refusal`` has which values those are).
_CONFLUENCE_JAXRS_WRITES = tuple(
    f"{method} /wiki/rest/{path}"
    for method, path in (
        ("POST", "api/audit"),
        ("PUT", "api/audit/retention"),
        ("POST", "api/content/archive"),
        ("POST", "api/content/blueprint/instance/{}"),
        ("PUT", "api/content/blueprint/instance/{}"),
        ("POST", "api/contentbody/convert/async/bulk/tasks"),
        ("POST", "api/contentbody/convert/async/{}"),
        ("PUT", "api/relation/{}/from/{}/{}/to/{}/{}"),
        ("DELETE", "api/relation/{}/from/{}/{}/to/{}/{}"),
        ("PUT", "api/settings/lookandfeel"),
        ("POST", "api/settings/lookandfeel/custom"),
        ("DELETE", "api/settings/lookandfeel/custom"),
        ("POST", "api/template"),
        ("PUT", "api/template"),
        ("DELETE", "api/template/{}"),
        *(
            (method, f"api/user/watch/{kind}/{{}}")
            for kind in ("content", "label", "space")
            for method in ("POST", "DELETE")
        ),
        ("POST", "atlassian-connect/1/app/module/dynamic"),
        ("DELETE", "atlassian-connect/1/app/module/dynamic"),
    )
)
_CONFLUENCE_JAXRS = _operations(_CONFLUENCE_JAXRS_WRITES)
_CONFLUENCE_JAXRS_UNTYPED = _operations(
    row for row in _CONFLUENCE_JAXRS_WRITES if "/settings/lookandfeel" in row
)
#: The JAX-RS GETs of :data:`_CONFLUENCE_NOT_PERMITTED_GETS` and the two `contentbody/convert`
#: reads, which read a request's `Content-Type` the way their writes do: measured 2026-10-19 on the
#: CQL search and on each of the first but one, a type JSON is not compatible with is the empty 415
#: ahead of the caller check and ahead of the operation's own 400, and a header Jersey cannot read
#: is Tomcat's 400 page, each with no credential and with one. The one is `settings/lookandfeel`,
#: untyped on its GET as on its writes (:data:`_CONFLUENCE_JAXRS_UNTYPED`): any type reaches the
#: caller check, and only the header Jersey cannot read is refused.
_CONFLUENCE_JAXRS_GETS = _operations(
    [
        *(f"GET /wiki/rest/{path}" for path in _CONFLUENCE_NOT_PERMITTED_GETS),
        # measured 2026-10-22: `text/plain` was the empty 415 and `foo` Tomcat's 400 page on both,
        # where no `Content-Type` was a 400 and a 200 with no credential
        "GET /wiki/rest/api/contentbody/convert/async/{}",
        "GET /wiki/rest/api/contentbody/convert/async/bulk/tasks",
    ]
)
_CONFLUENCE_JAXRS_GETS_UNTYPED = _operations(["GET /wiki/rest/api/settings/lookandfeel"])
#: The two of them that ran with no credential: `[]` at 200 and a 404 for an id naming nothing.
_CONFLUENCE_JAXRS_RUN = tuple(row for row in _CONFLUENCE_JAXRS_WRITES if "/contentbody/" in row)

#: The Confluence services whose refusal of a caller with no credential is
#: ``errors.atlassian.CONFLUENCE_NOT_PERMITTED_BODY`` rather than the one every other operation
#: gives. Measured 2026-09-30 with no credential on each of the 63 GETs the Confluence baseline
#: lists as `missing_operation`: these nineteen (:data:`_CONFLUENCE_NOT_PERMITTED_GETS`) answered
#: that body, 42 the other, and two ran (:data:`_CONFLUENCE_RUN_ANONYMOUSLY`); and the JAX-RS
#: writes (:data:`_CONFLUENCE_JAXRS_WRITES`) answered it too. No field of the vendor's document
#: separates the two refusals, so the services are named.
_CONFLUENCE_NOT_PERMITTED = _operations(
    [
        *(f"GET /wiki/rest/{path}" for path in _CONFLUENCE_NOT_PERMITTED_GETS),
        *(row for row in _CONFLUENCE_JAXRS_WRITES if row not in _CONFLUENCE_JAXRS_RUN),
    ]
)
_CONFLUENCE_CONNECT_MODULES = "/wiki/rest/atlassian-connect/1/app/module/dynamic"
#: Confluence operations real serves that its document does not publish, so the baseline has no
#: entry for them: each refused a caller with no credential the way the published ones do. The
#: reads answered 200 with one, on 2026-09-30; the three writes, which a 405 here names in its
#: `Allow` (``errors.atlassian.method_not_allowed``), answered the refusal with a JSON body, a
#: `text/plain` one and none alike on 2026-10-13, and were sent with no credential alone.
_CONFLUENCE_OUTSIDE_THE_DOCUMENT = _operations(
    (
        *(
            f"GET /wiki/rest/api/{path}"
            for path in (
                "content/{}/child",
                "content/{}/history",
                "content/{}/property",
                "content/{}/version",
                "space/{}/content",
                "space/{}/property",
            )
        ),
        "POST /wiki/rest/api/content",
        "PUT /wiki/rest/api/content/{}",
        "DELETE /wiki/rest/api/content/{}",
    )
)
#: The two GETs the same sweep found running with no credential, a 200 with `[]` and a 400 for an
#: id that does not match the service's pattern, and the two JAX-RS writes beside them.
_CONFLUENCE_RUN_ANONYMOUSLY = _operations(
    (
        "GET /wiki/rest/api/contentbody/convert/async/bulk/tasks",
        "GET /wiki/rest/api/contentbody/convert/async/{}",
        *_CONFLUENCE_JAXRS_RUN,
    )
)
#: The Confluence operations a Spring service checks the request's media type for ahead of the
#: caller, whoever it is: (method, pattern, the types taken). With no credential, `Bearer nope` and
#: a Basic pair it rejects, a body of another type, or none, is the 415 (``errors.atlassian`` names
#: how it reads the type) and a body of that type the refusal :func:`_confluence_refusal` gives,
#: measured 2026-10-12 on the attachment writes and `space/{key}/permission`, and 2026-10-13 on each
#: of the 65 writes the baseline lists: these sixteen answered no body and a `text/plain` one with
#: the 415, the JAX-RS ones (:data:`_CONFLUENCE_JAXRS_WRITES`) their own way, and the other 26 the
#: caller's refusal either way.
_CONFLUENCE_CONSUMES = tuple(
    (method, pattern, consumes)
    for rows, consumes in (
        (
            (
                "POST /wiki/rest/api/content/{}/child/attachment",
                "PUT /wiki/rest/api/content/{}/child/attachment",
                "POST /wiki/rest/api/content/{}/child/attachment/{}/data",
            ),
            ("multipart/form-data",),
        ),
        (
            (
                "POST /wiki/rest/api/content/{}/pagehierarchy/copy",
                "POST /wiki/rest/api/content/{}/permission/check",
                "POST /wiki/rest/api/content/{}/restriction",
                "PUT /wiki/rest/api/content/{}/restriction",
                "PUT /wiki/rest/api/content/{}/state",
                "POST /wiki/rest/api/content/{}/version",
                "POST /wiki/rest/api/group",
                "POST /wiki/rest/api/group/userByGroupId",
                "POST /wiki/rest/api/space/{}/permission",
                "POST /wiki/rest/api/space/{}/permission/custom-content",
                "PUT /wiki/rest/api/space/{}/theme",
                "POST /wiki/rest/api/user/{}/property/{}",
                "PUT /wiki/rest/api/user/{}/property/{}",
            ),
            ("application/json",),
        ),
    )
    for method, pattern in _operations(rows)
)


#: The Confluence operation that answers an `Accept` admitting no JSON with its anonymous 403, in
#: the shape that `Accept` asks for (``errors.atlassian.confluence_refusal``), rather than the 406,
#: under each method it is published for, measured 2026-10-13. Of the 69 GETs swept the same day,
#: the 63 the baseline lists and the six of :data:`_CONFLUENCE_OUTSIDE_THE_DOCUMENT`, this one did,
#: the 21 JAX-RS ones :data:`_CONFLUENCE_NOT_PERMITTED` and :data:`_CONFLUENCE_RUN_ANONYMOUSLY` name
#: answered the 406 with no body, and the other 47 Tomcat's page.
_CONFLUENCE_REFUSED_BY_ACCEPT = _operations(
    f"{method} /wiki/rest/api/space/{{}}/label" for method in ("GET", "POST", "DELETE")
)


def _is(operations, method: str | None, vendor_path: str) -> bool:
    """Whether ``vendor_path`` is one of ``operations``, for ``method`` or, as None, any method."""
    return any(
        (method is None or verb == method) and pattern.fullmatch(vendor_path)
        for verb, pattern in operations
    )


def _jira_oops_page(request: Request) -> bytes:
    """Jira's 500 page for this request (``errors.atlassian.jira_oops_page``), carrying the trace id
    its headers carry."""
    trace = request_ids(request)["atl-traceid"]
    return errors_atlassian.jira_oops_page(synth._uuid_from(f"oops:{trace}"), trace)


def _jira_unauthenticated(request: Request) -> Response:
    """Jira's 401 for an operation that will not run for this caller, as real spells it under the
    request's `Accept` (``errors.atlassian.jira_unauthenticated``); the realm is the site,
    percent-encoded."""
    realm = quote(_site(request), safe="")
    status, media_type, body, headers = errors_atlassian.jira_unauthenticated(
        request.headers.get("accept"), request.method
    )
    if body is None:
        body = _jira_oops_page(request)
    return Response(
        body,
        status_code=status,
        headers={
            "Content-Type": media_type,
            "WWW-Authenticate": f'OAuth realm="{realm}"',
            **headers,
        },
    )


def _jira_media_refusal(
    request: Request, operation: str | None
) -> errors_atlassian.AtlassianError | None:
    """The 415 ``operation`` answers for a `Content-Type` it does not take, where its entry says
    what it takes (``errors.atlassian.refuse_a_media_type``), or None."""
    entry = _JIRA_ENTRIES.get(operation) if operation is not None else None
    if entry is None or "consumes" not in entry:
        return None
    return errors_atlassian.refuse_a_media_type(
        _echoed_path(request),
        request.headers,
        tuple(entry["consumes"]),
        body_optional=entry.get("body") == "optional",
        charset=entry.get("charset"),
    )


def _confluence_refusal(request: Request, vendor_path: str) -> Response | None:
    """Confluence's refusal of a caller with no credential it resolves on an operation it
    publishes and no route here serves, or None where the operation runs for that caller.

    The refusal a served route gives (:func:`_confluence_caller`), in real's two members, on every
    operation but the services named in :data:`_CONFLUENCE_NOT_PERMITTED`, and on writes too:
    `DELETE` on a content id's label and `POST` on its copy, each naming nothing, answered it with
    no credential on 2026-09-30. A caller whose credential resolves gets what an unserved path
    gets, the gap the baseline entry acknowledges.
    """
    if _is(_CONFLUENCE_RUN_ANONYMOUSLY, request.method, vendor_path):
        return None
    if not auth.atlassian_caller(request).is_anonymous:
        return None
    if _is(_CONFLUENCE_REFUSED_BY_ACCEPT, request.method, vendor_path):
        return errors_atlassian.confluence_refusal(
            403, errors_atlassian.CONFLUENCE_FORBIDDEN_BODY, request.headers.get("accept")
        )
    if _is(_CONFLUENCE_NOT_PERMITTED, request.method, vendor_path):
        headers = {}
        if vendor_path != _CONFLUENCE_CONNECT_MODULES:
            headers = {
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Expires": "Thu, 01 Jan 1970 00:00:00 GMT",
            }
        return Response(
            errors_atlassian.CONFLUENCE_NOT_PERMITTED_BODY,
            status_code=403,
            media_type="application/json",
            headers=headers,
        )
    if auth.basic_credential_kind(request) == auth.BASIC_UNPARSEABLE:
        _confluence_caller(request)  # its 401
    return Response(
        errors_atlassian.CONFLUENCE_FORBIDDEN_BODY, status_code=403, media_type="application/json"
    )


#: The Confluence operations that answer in types other than JSON, and which: `audit/export`, whose
#: `Accept` of `application/zip` or `text/csv` reached the caller's refusal with no credential on
#: 2026-10-14 where `application/json`, `text/html` and `application/octet-stream` drew the empty
#: 406. None of the other 136 operations the baseline lists or
#: :data:`_CONFLUENCE_OUTSIDE_THE_DOCUMENT` names drew a 406 for `application/json` that day.
_CONFLUENCE_PRODUCES = tuple(
    (method, pattern, ("application/zip", "text/csv"))
    for method, pattern in _operations(["GET /wiki/rest/api/audit/export"])
)


def _jaxrs_media_refusal(request: Request, *, checks_type: bool) -> Response | None:
    """What a Confluence JAX-RS service answers a `Content-Type` it will not read, or None:
    Tomcat's 400 page for one Jersey cannot read, or an empty 415 with no `content-type` where
    the service checks the type, as ``errors.atlassian.jaxrs_media_type_refusal`` decides, and the
    400 page for the header sent with more than one value that is not blank; blank ones are left
    out, and the header sent blank alone reads as an empty value. Measured with no credential on
    2026-10-22: the header twice, as `application/json` twice, `text/plain` twice or the two
    together, was the 400 page on `POST template`, on the `audit`, `settings/lookandfeel` and CQL
    search GETs and on `/wiki/rest/api/nosuch`, where the Spring `GET content/{id}` and
    `POST content` gave the 403 they give to one. On `audit` an empty, space or tab value beside
    `application/json` was its 403 and beside `text/plain` its 415, an empty one beside two
    `application/json` the 400 page, and two empty ones beside one `application/json` the 403; two
    empty ones alone were the 400 page there, on `POST template` and on `/wiki/rest/api/nosuch`."""
    values = request.headers.getlist("content-type")
    read = [value for value in values if value.strip(" \t")] or values[:1]
    if len(read) > 1:
        status = 400
    else:
        status = errors_atlassian.jaxrs_media_type_refusal(
            read[0] if read else None, checks_type=checks_type
        )
    if status == 400:
        return Response(
            errors_atlassian.tomcat_page(400),
            status_code=400,
            media_type=errors_atlassian.TOMCAT_MEDIA_TYPE,
            headers={"Content-Language": "en"},
        )
    return None if status is None else Response(status_code=status)


def _refuse_the_search_media_type(request: Request) -> None:
    """Raise what the CQL search answers a `Content-Type` it will not read
    (:func:`_jaxrs_media_refusal`): Tomcat's 400 page, or the empty 415 unless `Accept` is one
    Jersey cannot parse, whose 400 comes first. Measured on Confluence Cloud on 2026-10-22 with a
    credential: `text/plain` under `Accept: application/xml` was the 415 and under
    `Accept: application/json;q=2` the `Accept` 400, and `foo` under either the 400 page."""
    refused = _jaxrs_media_refusal(request, checks_type=True)
    if refused is None or (
        refused.status_code == 415
        and errors_atlassian.jaxrs_cannot_parse(request.headers.get("accept"))
    ):
        return
    raise errors_atlassian.AtlassianError(
        refused.status_code, bytes(refused.body), headers=dict(refused.headers)
    )


def _confluence_jaxrs_media_refusal(request: Request, vendor_path: str) -> Response | None:
    """What a Confluence JAX-RS write (:data:`_CONFLUENCE_JAXRS_WRITES`) or GET
    (:data:`_CONFLUENCE_JAXRS_GETS`) answers a `Content-Type` it will not read, or None, as
    :func:`_jaxrs_media_refusal` gives it: the services that check the type are all but the
    untyped ones of each kind (:data:`_CONFLUENCE_JAXRS_UNTYPED`,
    :data:`_CONFLUENCE_JAXRS_GETS_UNTYPED`)."""
    if _is(_CONFLUENCE_JAXRS, request.method, vendor_path):
        checks_type = not _is(_CONFLUENCE_JAXRS_UNTYPED, request.method, vendor_path)
    elif _is(_CONFLUENCE_JAXRS_GETS, request.method, vendor_path):
        checks_type = not _is(_CONFLUENCE_JAXRS_GETS_UNTYPED, request.method, vendor_path)
    else:
        return None
    return _jaxrs_media_refusal(request, checks_type=checks_type)


def _published_answer(request: Request, vendor_path: str) -> Response | None:
    """What real answers before an operation it publishes at ``vendor_path`` and no route here
    serves runs for this caller, or None where it would run, which is where Backlot has nothing to
    give but the gap the baseline's `missing` operation acknowledges.

    Jira checks the request's media type where the operation does, for any caller the gateway lets
    through (:func:`_jira_media_refusal`), then `Accept` (``errors.atlassian.not_acceptable``, asked
    of no caller where the operation's entry says ``"accept": "unchecked"``, which was measured with
    no credential), then refuses a caller with no credential it resolves where the operation will
    not run anonymously, and any `OPTIONS` on one (:func:`_jira_unauthenticated`), each measured
    2026-09-30 and the `Accept` check's place 2026-10-13; where the operation runs and is one of
    :data:`_JIRA_ISSUE_WRITES` or :data:`_JIRA_ROLE_WRITES`, its lookup of a key that names nothing
    the caller can see is its 404, and a role write's is followed by the 401
    :func:`_require_project_admin` gives. The operation is the one :func:`_jira_operation` names.

    Confluence answers a Basic value it cannot read with Tomcat's 401 first, then checks the media
    type where the operation does (:data:`_CONFLUENCE_CONSUMES`, and its JAX-RS operations their
    own way, :func:`_confluence_jaxrs_media_refusal`), then `Accept` (on every operation but
    :data:`_CONFLUENCE_REFUSED_BY_ACCEPT`, and with no body on the JAX-RS ones), then refuses a
    caller with no credential it resolves on every operation but :data:`_CONFLUENCE_RUN_ANONYMOUSLY`
    (:func:`_confluence_refusal`); the 401's place was measured 2026-10-12 with an undecodable value
    and with one holding no colon, each with a body of a type the operation does not take, and the
    `Accept` check's 2026-10-13.
    """
    method = request.method
    # `request.url.path` here classifies the path by product and mount only; nothing echoes it (the
    # refusals below carry no path, and `_echoed_path` reads the raw spelling where one does)
    if errors_atlassian.is_confluence(request.url.path):
        if method == "OPTIONS" or not (
            _is(_CONFLUENCE_PUBLISHED, method, vendor_path)
            or _is(_CONFLUENCE_OUTSIDE_THE_DOCUMENT, method, vendor_path)
        ):
            return None
        if auth.basic_credential_kind(request) == auth.BASIC_UNPARSEABLE:
            _confluence_caller(request)  # Tomcat's 401, ahead of the media-type check too
        for verb, pattern, consumes in _CONFLUENCE_CONSUMES:
            if verb == method and pattern.fullmatch(vendor_path):
                refused = errors_atlassian.refuse_a_confluence_media_type(request.headers, consumes)
                if refused is not None:
                    raise refused
        accept = request.headers.get("accept")
        jaxrs = _confluence_jaxrs_media_refusal(request, vendor_path)
        # Jersey parses `Accept` after Tomcat reads the body's type and before it matches it: a
        # header it cannot parse is its 400 ahead of the empty 415 (2026-10-14)
        if jaxrs is not None and not (
            jaxrs.status_code == 415 and errors_atlassian.jaxrs_cannot_parse(accept)
        ):
            return jaxrs
        if not _is(_CONFLUENCE_REFUSED_BY_ACCEPT, method, vendor_path):
            produces = next(
                (
                    made
                    for verb, pattern, made in _CONFLUENCE_PRODUCES
                    if verb == method and pattern.fullmatch(vendor_path)
                ),
                errors_atlassian.JSON_ONLY,
            )
            _refuse_the_accept(
                request,
                errors_atlassian.not_acceptable(
                    request.url.path,
                    accept,
                    jaxrs=_is(_CONFLUENCE_NOT_PERMITTED, method, vendor_path)
                    or _is(_CONFLUENCE_RUN_ANONYMOUSLY, method, vendor_path),
                    produces=produces,
                ),
            )
        return _confluence_refusal(request, vendor_path)
    if not errors_atlassian.serves_the_jira_api(request.url.path):
        return None
    operation = _jira_operation(method, vendor_path)
    refused = _jira_media_refusal(request, operation)
    if refused is not None:
        raise refused
    entry = _JIRA_ENTRIES.get(operation) if operation is not None else None
    if entry is not None and entry.get("accept") != "unchecked":
        _refuse_the_accept(
            request,
            errors_atlassian.not_acceptable(
                _echoed_path(request),
                request.headers.get("accept"),
                produces=tuple(entry.get("produces", errors_atlassian.JSON_ONLY)),
            ),
        )
    if operation in _JIRA_ROLE_WRITES:
        _role_id(request, vendor_path.split("/")[7])
    if auth.atlassian_caller(request).is_anonymous and (
        operation in _JIRA_UNSERVED["refused"]
        or (method == "OPTIONS" and _is(_JIRA_PUBLISHED, None, vendor_path))
    ):
        return _jira_unauthenticated(request)
    if operation in _JIRA_ISSUE_WRITES:
        _require_issue(request, auth.conn(request), vendor_path.split("/")[5])
    elif operation in _JIRA_ROLE_WRITES:
        _require_project_admin(request, auth.conn(request), vendor_path.split("/")[5])
    return None


#: The start of the `orderBy` refusal (:func:`_jira_order_desc`), which real writes as it writes an
#: answer, where its other refusals are written by what refused them.
_JIRA_ORDER_REFUSAL = "The field to order by must be one of ["


def spring_write_failure(request: Request, status: int, headers, body: bytes) -> Response | None:
    """What real answers where a Spring service fails to write an answer under the request's
    `Accept` (``errors.atlassian.quality_spring_cannot_copy``), given the answer this server built,
    or None where real answers as it would have. Measured 2026-10-13 under `application/json;Q=2`
    on every route here and on each refusal below.

    Jira: a JSON answer that succeeded, the 401 it answers a caller with no credential, and the
    `orderBy` refusal are its 500 page (``errors.atlassian.jira_oops_page``; an `OPTIONS` gets the
    500 with no body and seven methods in `Allow` its 401 picker gives a wildcard it has no type
    for). Its other refusals are written as they would have been.

    Confluence: a JSON answer that succeeded is a 400 with no body; a refusal Spring raised itself
    (the `errors` list of a 405 or a 415, a `org.springframework` exception, the anonymous 403) is
    Tomcat's page for its status, as an `Accept` taking no JSON draws it, or the product's HTML
    404 page for a 404; a refusal the API's own service layer raised is its status with no body; an
    `OPTIONS` is Tomcat's 403 page. What :func:`_confluence_not_found` answers is not Spring's to
    write, an `OPTIONS` included: the product's page stands under `Q=2` and under
    `application/json;q=0.5;Q=2;q=0.5`, and so does the JAX-RS 404 under that second header, which
    Jersey reads (JSON, and XML under `*/*;q=0.5;Q=2;q=0.5`), where under `application/json;Q=2`,
    which Jersey cannot read, it is the empty 500 that function gives (2026-10-16). `search` reads
    the `q` itself (``errors.atlassian._jaxrs_accept``), and the 401 for a Basic value Confluence
    cannot read is Tomcat's page either way.
    """
    media_type = headers.get("content-type") or ""
    is_json = media_type.startswith("application/json")
    # the decoded path picks the product, the service and the page's headers; nothing echoes it
    if not errors_atlassian.is_confluence(request.url.path):
        gate = status == 401 and "www-authenticate" in headers
        refusal = status == 400 and is_json and _JIRA_ORDER_REFUSAL.encode() in body
        if not (gate or refusal or (200 <= status < 300 and is_json)):
            return None
        kept = {"WWW-Authenticate": headers["www-authenticate"]} if gate else {}
        media = errors_atlassian.JIRA_OOPS_MEDIA_TYPE
        if request.method == "OPTIONS":
            allow = {"Allow": errors_atlassian._JIRA_NO_TYPE_ALLOW}
            return Response(b"", status_code=500, media_type=media, headers={**kept, **allow})
        return Response(_jira_oops_page(request), status_code=500, media_type=media, headers=kept)
    vendor_path = _vendor_path(request.url.path)
    if vendor_path.startswith("/wiki/rest/api/search"):
        return None
    if request.scope.get("atlassian_outside_spring"):
        return None
    if request.method == "OPTIONS":
        return errors_atlassian.confluence_refusal(403, b"", "text/html")
    if not is_json or status == 401:
        return None
    if 200 <= status < 300:
        return Response(b"", status_code=400)
    refused = json.loads(body)
    message = refused.get("message", "") if isinstance(refused, dict) else ""
    spring = isinstance(refused.get("errors"), list) or message.startswith(
        ("org.springframework.", "com.atlassian.confluence.mvc.rest.common.exception.")
    )
    if not spring:
        return Response(b"", status_code=status)
    if status == 404:
        return Response(
            errors_atlassian.HTML_NOT_FOUND, 404, media_type=errors_atlassian.HTML_MEDIA_TYPE
        )
    page_headers = {}
    if status == 405:
        page_headers = dict(
            errors_atlassian.method_not_allowed(request.url.path, request.method).headers
        )
    elif status == 415:
        for verb, pattern, consumes in _CONFLUENCE_CONSUMES:
            if verb == request.method and pattern.fullmatch(vendor_path):
                page_headers = {"Accept": ", ".join(consumes)}
    return errors_atlassian.confluence_refusal(status, b"", "text/html", page_headers)


def spring_sort_failure(request: Request, status: int, headers, body: bytes) -> Response | None:
    """The answer that replaces this server's where Spring fails as it sorts the request's `Accept`
    (``errors.atlassian.spring_cannot_sort``), or None where what answers comes before Spring.
    Measured 2026-10-14 with `application/json;Q=abc, text/plain` and its kin on each answer below.

    Spring sorts before it picks a handler, so every answer it would give is replaced, the 404s,
    405s, 406s and 415s among them: on Jira by its 500 page (``errors.atlassian.jira_oops_page``)
    with no `WWW-Authenticate`, an `OPTIONS` by the empty 500 and seven methods in `Allow`, whatever
    the credential; on Confluence by an empty 500 with no `content-type`. What comes before Spring
    stands: the gateway's Connect-token 403, Tomcat's 401 for an unreadable Basic value, and on
    Confluence's JAX-RS services Jersey's own reading of the header, its 400
    (``errors.atlassian.not_acceptable``) or the 404's 500.
    """
    if not errors_atlassian.is_confluence(request.url.path):
        if status == 403 and auth.atlassian_bearer_unreadable(request):
            return None
        media = errors_atlassian.JIRA_OOPS_MEDIA_TYPE
        if request.method == "OPTIONS":
            allow = {"Allow": errors_atlassian._JIRA_NO_TYPE_ALLOW}
            return Response(b"", status_code=500, media_type=media, headers=allow)
        return Response(_jira_oops_page(request), status_code=500, media_type=media)
    jersey = status == 400 and (headers.get("content-type") or "").startswith("text/plain")
    if status in (401, 500) or jersey:
        return None
    return Response(b"", status_code=500)


def _vendor_path(path: str) -> str:
    return (
        path[len(errors_atlassian.PREFIX) :] if path.startswith(errors_atlassian.PREFIX) else path
    )


def _echoed_path(request: Request) -> str:
    """The path a refusal names, which is not always the one that was routed.

    Real collapses an interior run of slashes in what it echoes and keeps a trailing one, where
    routing ignores both, and keeps some escapes as sent, where routing decodes every one
    (``errors.atlassian.echoed_spelling``). Measured 2026-10-10 on Jira's 415, conversion 400 and
    405, each of which keeps the trailing slash in `instance`.
    """
    return request.scope.get("atlassian_echo_path", request.url.path)


def _wants_json(request: Request) -> bool:
    """Whether Confluence's JAX-RS 404 is JSON (``errors.atlassian.jaxrs_picks_json``)."""
    return errors_atlassian.jaxrs_picks_json(request.headers.get("accept"))


def _confluence_serves_html(vendor_path: str) -> bool:
    """Whether real answers this unmatched Confluence path with the product's page.

    Anything under `/wiki` that is not under `/wiki/rest/api/` is the page (`/wiki/rest/nope`,
    `/wiki/nope`, and `/wiki/rest/api` itself, where `/wiki/rest/api/` with the slash is the API's
    404). Under the API mount it is the page only below one of :data:`_CONFLUENCE_HTML_RESOURCES`.
    """
    api = f"{errors_atlassian.WIKI[len(errors_atlassian.PREFIX) :]}/rest/api"
    if not vendor_path.startswith(f"{api}/"):
        return True
    rest = vendor_path[len(api) + 1 :]
    head, _, tail = rest.partition("/")
    return bool(tail) and head in _CONFLUENCE_HTML_RESOURCES


def _confluence_not_found(request: Request) -> Response:
    """Confluence's answer for a path it serves nothing at: the page, or JAX-RS's own 404. Neither
    is Spring's to write, which the request is marked with for :func:`spring_write_failure`."""
    request.scope["atlassian_outside_spring"] = True
    vendor_path = _vendor_path(_echoed_path(request))
    if _confluence_serves_html(vendor_path):
        return Response(
            errors_atlassian.HTML_NOT_FOUND,
            status_code=404,
            media_type=errors_atlassian.HTML_MEDIA_TYPE,
        )
    refused = _jaxrs_media_refusal(request, checks_type=False)
    if refused is not None:
        # ahead of this 404 and of the 500 below, measured 2026-10-19 on `/wiki/rest/api/nosuch`
        # with `foo`, `application/json;x=` and an empty value and 2026-10-22 with `foo` under
        # `Accept: application/json;q=2`, where a type Jersey reads leaves both alone; the page
        # above answered `foo` as it answers no type
        return refused
    if errors_atlassian.jaxrs_cannot_parse(request.headers.get("accept")):
        # Jersey fails to write its 404 under an `Accept` it cannot parse: an empty 500 with no
        # `content-type`, measured 2026-10-14 on `nopesuchroute` and `nope/deeper` with `q=2`, a
        # blank member and `;foo`, with a credential and without one, and 2026-10-16 with U+00A0
        return Response(b"", status_code=500)
    url = f"{_site(request)}{vendor_path}"
    # the query as it arrived, for the reason `normalise_the_slashes_in_an_atlassian_path` reads
    # the path from the scope
    if query := request.scope.get("query_string", b"").decode("latin-1"):
        url = f"{url}?{query}"
    media_type, body = errors_atlassian.jaxrs_not_found(url, as_json=_wants_json(request))
    # JAX-RS's 404 says `no-transform` in both shapes and the page says nothing, measured 2026-09-30
    # with a credential and without one
    return Response(
        body, status_code=404, media_type=media_type, headers={"Cache-Control": "no-transform"}
    )


def _options_answer(request: Request) -> Response:
    """What an `OPTIONS` on a path one of the routes above serves answers.

    The two products split again. Jira answers 200 with an empty `text/html` body, an empty
    `Accept-Patch` and an `Allow` naming the VENDOR's methods for that route — `PUT` and `DELETE`
    on an issue, `POST` on `field` — which Backlot serves none of: the header describes the
    endpoint a client is asking about, so it is copied rather than derived from what this server
    happens to implement (`errors.atlassian.jira_options_allow`). Confluence answers 404 in the
    `errors` list its 405 uses, by `Accept` (``errors.atlassian.CONFLUENCE_OPTIONS_NOT_FOUND``,
    ``errors.atlassian.confluence_options``), but on `search` (:func:`_search_options`). Measured on
    Atlassian Cloud over every route here, `child/attachment` on 2026-10-10 and the others on
    2026-09-22.

    Jira's 200 is for a caller whose credential resolves. Anyone else — no credential, the Basic
    pair it rejects, an unknown scheme, and here an unreadable bearer too, which a `GET` draws the
    Connect-token 403 with — gets Jira's 401 (:func:`_jira_unauthenticated`), measured
    2026-09-30 on `serverInfo` and an issue with each of those four. Confluence's answer does not
    depend on the credential.
    """
    path = request.url.path
    if errors_atlassian.is_confluence(path):
        if _vendor_path(path) == errors_atlassian.CONFLUENCE_OPTIONS_BY_ACCEPT:
            return _search_options(request)
        return errors_atlassian.confluence_options(request.headers.get("accept"))
    if auth.atlassian_caller(request).is_anonymous:
        return _jira_unauthenticated(request)
    allow = errors_atlassian.jira_options_allow(path)
    # present with nothing after the colon, on every Jira `OPTIONS` measured: empty is the value
    headers = {"Accept-Patch": ""}
    if allow is not None:
        headers["Allow"] = allow
    return Response(
        b"", status_code=200, media_type=errors_atlassian.JIRA_OPTIONS_MEDIA_TYPE, headers=headers
    )


#: The WADL Jersey writes for `search`, with the site where real's names its own; read once.
_SEARCH_WADL = (_PACKAGE / "data" / "confluence_search_options.wadl").read_text(encoding="utf-8")


def _search_options(request: Request) -> Response:
    """Confluence's `search` answers an `OPTIONS` the way JAX-RS does, by what `Accept` asks for.

    Measured 2026-09-30, the same with a credential and without one: `*/*` and `application/xml`
    are 200 `application/xml` with the resource's WADL (3016 bytes on the site, the same bytes on
    every one of six), a request with no `Accept` header at all is the same document as
    `application/vnd.sun.wadl+xml`, and `application/json` and `text/html` are 204 with no body.
    All name the three methods in `Allow`. A header Jersey cannot parse is its 400
    (``errors.atlassian._jaxrs_accept``; `application/json;Q=2`, 2026-10-13).
    """
    headers = {"Allow": errors_atlassian.CONFLUENCE_SEARCH_OPTIONS_ALLOW}
    accept = request.headers.get("accept")
    # the decoded path picks the product only; the 400 echoes the header, not the path
    unparsed = errors_atlassian.not_acceptable(request.url.path, accept, jaxrs=True)
    if unparsed is not None and unparsed.status_code in (400, 500):
        raise unparsed
    if accept is None:
        media_type = "application/vnd.sun.wadl+xml"
    elif "*/*" in accept or "application/xml" in accept:
        media_type = "application/xml"
    else:
        return Response(status_code=204, headers=headers)
    body = _SEARCH_WADL.replace("{site}", _site(request))
    return Response(body, status_code=200, headers={**headers, "Content-Type": media_type})


#: A stub for the Jira web app's own pages, which real serves at `/browse/…` as a client-side shell
#: whose script tags name the deploy; the status and the media type are what this copies.
_JIRA_WEB_PAGE = "<!DOCTYPE html><html><head><title>Jira</title></head><body></body></html>"


def _site_surface(request: Request) -> Response:
    """What the site answers outside both API mounts, measured on 2026-09-30.

    The root is a 302: to `/login.jsp?os_destination=<the URL asked for>` for a caller with no
    credential and to `/jira/for-you` for one whose credential resolves, the query carried along.
    `/browse` is the Jira web app: 200, `text/html;charset=UTF-8` at `/browse` itself, a 55-byte
    `text/html` shell at `/browse/`, and below it `text/html` for a caller with no credential and
    `text/html; charset=utf-8` for one — a key that names nothing included. Everything else is
    Jira's own not-found page (``errors.atlassian.JIRA_SITE_HTML_MEDIA_TYPE``), with a credential
    and without one. What is not copied: a caller whose credential resolves is redirected from
    `/browse` to a project it last looked at, which Backlot has no record of, so it gets the
    anonymous page.
    """
    echoed = _vendor_path(_echoed_path(request))
    site = _site(request)
    query = f"?{request.url.query}" if request.url.query else ""
    anonymous = auth.atlassian_caller(request).is_anonymous
    if echoed == "/":
        if anonymous:
            destination = quote(f"{site}/{query}", safe="")
            return Response(
                status_code=302,
                headers={"Location": f"{site}/login.jsp?os_destination={destination}"},
            )
        return Response(status_code=302, headers={"Location": f"{site}/jira/for-you{query}"})
    if echoed == "/browse":
        return Response(_JIRA_WEB_PAGE, media_type=errors_atlassian.HTML_MEDIA_TYPE)
    if echoed.startswith("/browse/"):
        if echoed == "/browse/" or anonymous:
            media_type = "text/html"
        else:
            media_type = "text/html; charset=utf-8"
        return Response(_JIRA_WEB_PAGE, headers={"Content-Type": media_type})
    return Response(
        errors_atlassian.HTML_NOT_FOUND,
        status_code=404,
        media_type=errors_atlassian.JIRA_SITE_HTML_MEDIA_TYPE,
    )


@unmatched_router.api_route("/{rest:every_path}", methods=_UNMATCHED_METHODS)
async def unmatched_path(request: Request, rest: str) -> Response:
    """What either product answers for a path no route above serves, and for a method they do not.

    Mounted after `router`, so a request one of those routes answers never arrives here; what does
    is a path with no route at all, or a method a route does not declare, which Starlette would
    otherwise refuse before any vendor code ran.

    A path with no route is Jira's RFC 7807 `No endpoint <METHOD> <path>.` (see
    `errors.atlassian.no_endpoint`) or Confluence's own pair of shapes (see
    :func:`_confluence_not_found`), and an `OPTIONS` on such a path is that same answer — measured
    on both products 2026-09-22. A method a route does not declare is the 405 each product answers
    (`errors.atlassian.method_not_allowed`, measured too), raised here because this route takes
    every method on every path it owns and so receives that request; `OPTIONS` is the exception,
    and :func:`_options_answer` is what real gives it.

    Those are the answers where the vendor publishes nothing for the method at the path. An
    operation it does publish is looked at first, at a served route's path as at any other, since
    real reaches it whether or not Backlot serves it (:func:`_published_answer`); a caller it would
    run for gets the answer above. Outside both API mounts the site is its web app
    (:func:`_site_surface`).
    """
    # the path as routed, not `request.url.path`: parsing the URL again drops a decoded newline or
    # tab and ends the path at a decoded `#`, so `myse%0Alf` would be looked up as `myself`
    path = request.scope["path"]
    vendor_path = _vendor_path(path)
    served = _some_atlassian_route_matches(request)
    if served and request.method == "OPTIONS":
        return _options_answer(request)
    published = _published_answer(request, vendor_path)
    if published is not None:
        return published
    if served:
        raise errors_atlassian.method_not_allowed(_echoed_path(request), request.method)
    if errors_atlassian.is_confluence(path):
        if (
            auth.atlassian_caller(request).is_anonymous
            and vendor_path.startswith("/wiki/")
            and not vendor_path.startswith("/wiki/rest")
        ):
            # the web app, not an API: a caller with no credential is sent to log in (`/wiki/nope`,
            # measured 2026-09-30; with a credential it is the page below), `dest-url` quoting the
            # path as `null for uri:` spells it and the query as sent (`/wiki/nope%3Fx?a=1` gives
            # `%2Fwiki%2Fnope%253Fx%3Fa%3D1`, 2026-10-17)
            query = request.scope.get("query_string", b"").decode("latin-1")
            asked = quote(
                f"{_vendor_path(_echoed_path(request))}{'?' + query if query else ''}", safe=""
            )
            return Response(
                status_code=302,
                headers={
                    "Location": f"{_site(request)}/login?application=confluence&dest-url={asked}"
                },
            )
        return _confluence_not_found(request)
    if errors_atlassian.serves_the_jira_api(path):
        # on a path Jira publishes nothing at, the URL is what answers
        raise errors_atlassian.no_endpoint(_echoed_path(request), request.method)
    return _site_surface(request)


def _some_atlassian_route_matches(request: Request) -> bool:
    """Whether a route other than this catch-all matches the path, whatever its method.

    Asked of :data:`router` rather than of the app, which is both narrower and the only way to ask
    it: the catch-all matches every path under `/atlassian`, so scanning the app would always say
    yes, and the app's list holds the included ROUTER rather than its routes, so the catch-all
    cannot be filtered out of it by endpoint either.

    `Match.PARTIAL` counts, which is the point: a path that matches a route whose methods do not
    is exactly the request the 405 above answers.
    """
    return any(route.matches(request.scope)[0] is not Match.NONE for route in router.routes)


def _a_route_answers(request: Request) -> bool:
    """Whether a route above takes this request's method at its path, a `HEAD` read as its GET.

    ``Match.FULL`` only: an `OPTIONS` or a method a route does not take is answered around the
    route (see :func:`unmatched_path`), and a path no route matches by the catch-all.
    """
    method = "GET" if request.method == "HEAD" else request.method
    scope = {**request.scope, "method": method}
    return any(route.matches(scope)[0] is Match.FULL for route in router.routes)


# ============================== the headers real puts on every answer ========================


#: The two ids both products put on every answer, and Jira's third. Real mints a new value per
#: response; these are derived from the request, the choice this repository makes for a synthesised
#: id (as S3's two are, in `backlot.routers.s3.request_ids`), so that a corpus served twice answers
#: the same id and a test can assert one. Measured on Atlassian Cloud 2026-09-22 over 78
#: responses: `atl-request-id` is a UUID, `atl-traceid` is that same 32 hex WITHOUT the dashes, and
#: Jira's `x-arequestid` is 32 hex of its own.
def request_ids(request: Request) -> dict[str, str]:
    """`atl-request-id` and `atl-traceid`, plus `x-arequestid` where the request is Jira's."""
    seed = f"atl:{request.method} {request.url.path}?{request.url.query}"
    request_id = synth._uuid_from(seed)
    ids = {"atl-request-id": request_id, "atl-traceid": request_id.replace("-", "")}
    if not errors_atlassian.is_confluence(request.url.path):
        ids["x-arequestid"] = synth._digest("arequestid:" + seed)[:32]
    return ids


#: Jira's burst quota. Measured 2026-09-22 and 2026-09-30: `x-ratelimit-limit` is 350 on a `GET` of
#: every route here but two, 400 on `issue/{key}` and 500 on `project/{key}/role/{id}`, and 200 on a
#: `POST` to `search/jql`; the policy's `q` is 100, 150, 200 and 100 against those four limits,
#: always with `w=1`. A `HEAD` and an `OPTIONS` read a quota of their own, `q` and
#: `x-ratelimit-limit` both 1000000000000, on `serverInfo`, `field`, `search/jql`, `project/search`
#: and an issue alike.
_JIRA_BURST_POLICY = "jira-burst-based"
_JIRA_BURST_WINDOW = 1
_JIRA_BURST_DEFAULT = (100, 350)
_JIRA_BURST_BUCKETS = (
    ("GET", "/rest/api/{version}/issue/{key}", (150, 400)),
    ("GET", "/rest/api/{version}/project/{key}/role/{id}", (200, 500)),
    ("POST", "/rest/api/{version}/search/jql", (100, 200)),
)
_JIRA_BURST_PATTERNS = tuple(
    (method, errors_atlassian.route_regex(t), bucket) for method, t, bucket in _JIRA_BURST_BUCKETS
)
_JIRA_UNMETERED = (1000000000000, 1000000000000)
_JIRA_MOUNT = re.compile(r"/rest/api/[23]/")


def _jira_burst_bucket(method: str, path: str) -> tuple[int, int]:
    if method in ("HEAD", "OPTIONS"):
        return _JIRA_UNMETERED
    vendor_path = _vendor_path(path)
    for verb, pattern, bucket in _JIRA_BURST_PATTERNS:
        if verb == method and pattern.fullmatch(vendor_path):
            return bucket
    return _JIRA_BURST_DEFAULT


def _route_template(request: Request) -> str:
    """The template of the route at this request's path, either Jira mount read as one."""
    for route in router.routes:
        if route.matches(request.scope)[0] is not Match.NONE:
            return _JIRA_MOUNT.sub("/rest/api/{version}/", route.path, count=1)
    return request.url.path


class JiraBurstWindows:
    """What a caller has spent of a burst quota in the current second, per method and route.

    A window counts one method on one route template, `{version}` read as either mount and a path
    parameter as any value. Measured 2026-09-30 with five requests of each pair sent together:
    `serverInfo` and `field` each read 349…345, where the `/2` and `/3` spellings of `serverInfo`
    read 349…340 between them, and two issue keys read 399…390, as did an issue key beside one that
    does not exist; a `HEAD` and an `OPTIONS` on `serverInfo` each counted their own, and three
    `HEAD`s left the `GET` after them at 349. `remaining` stops at zero and nothing is refused: no
    429 was measured, and a mock that invents one fails a suite for pacing it never asked for —
    the same line ``backlot.routers.github.RateLimitWindows`` draws, whose shape this follows.
    ``clock`` is `time.time` unless a test hands in another.
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._windows: dict[tuple[str, str, str], list[int]] = {}

    def count(self, key: tuple[str, str, str], limit: int) -> int:
        """`remaining` after counting one more request in ``key``'s window against ``limit``."""
        now = int(self.clock())
        window = self._windows.get(key)
        if window is None or now >= window[0] + _JIRA_BURST_WINDOW:
            window = self._windows[key] = [now, 0]
        window[1] += 1
        return max(limit - window[1], 0)


def _burst_windows(app) -> JiraBurstWindows:
    windows = getattr(app.state, "jira_burst_windows", None)
    if windows is None:
        windows = app.state.jira_burst_windows = JiraBurstWindows()
    return windows


def rate_limit_headers(request: Request, caller: Caller) -> dict[str, str]:
    """Jira's four, for a caller whose credential resolved, counted in its window.

    An anonymous request carries none of them, and neither does the 404 for a path Jira mounts no
    endpoint at (both measured 2026-09-22) or a 405 (`POST serverInfo`, `PUT search/jql`,
    `POST issue/{key}`, measured 2026-09-30), so this is asked only where real answers them.
    """
    q, limit = _jira_burst_bucket(request.method, request.url.path)
    key = (caller.email or "anonymous", request.method, _route_template(request))
    remaining = _burst_windows(request.app).count(key, limit)
    return {
        "ratelimit": f'"{_JIRA_BURST_POLICY}";r={remaining};t={_JIRA_BURST_WINDOW}',
        "ratelimit-policy": f'"{_JIRA_BURST_POLICY}";q={q};w={_JIRA_BURST_WINDOW}',
        "x-ratelimit-limit": str(limit),
        "x-ratelimit-remaining": str(remaining),
    }


#: Confluence says its v1 REST API is deprecated, in three headers, on the answers the content and
#: space services give — including their 404s. Measured 2026-09-22: `content`, `content/{id}`,
#: `child/comment`, `child/page`, `label`, `space`, `space/{key}` and the 404s for an unknown space
#: and an unknown content id all carry them, and so do `child/attachment`, its 400 and an unknown
#: id's 404, measured 2026-10-10; `search`, `restriction/byOperation`, the 405 at
#: `space/{key}/permission`, the 403 an anonymous request gets and an `OPTIONS` on `space`,
#: `space/{key}`, an unknown space and `permission` carry none, and neither does the 406 an `Accept`
#: admitting no JSON draws (`content/{id}`, an unknown id and `space`, 2026-10-13). Nor does an
#: answer the catch-all gives, measured 2026-09-30 over twenty of them on ten paths: the JAX-RS 404
#: in both shapes and the product's HTML page, under `space/` and `content/` and outside the API
#: mount. The dates are real's own, a removal date that has already passed.
CONFLUENCE_DEPRECATION = {
    "deprecation": "Wed, 1 Mar 2023 00:00:00 GMT",
    "link": (
        "<https://developer.atlassian.com/cloud/confluence/changelog/#CHANGE-864>; "
        'rel="deprecation"'
    ),
    "warning": '299 - "Deprecated API, will be removed on Mon, 31 Mar 2025 00:00:00 GMT"',
}
_NO_DEPRECATION = ("/wiki/rest/api/search", "/wiki/rest/api/content/{id}/restriction/byOperation")
_NO_DEPRECATION_PATTERNS = tuple(
    errors_atlassian.route_regex(t)
    for t in (*_NO_DEPRECATION, "/wiki/rest/api/space/{key}/permission")
)


def sends_deprecation(path: str, status_code: int) -> bool:
    """Whether this Confluence answer carries the deprecation trio."""
    if not errors_atlassian.is_confluence(path):
        return False
    if status_code in (401, 403, 406):
        return False
    vendor_path = _vendor_path(path)
    return not any(pattern.fullmatch(vendor_path) for pattern in _NO_DEPRECATION_PATTERNS)


#: What the edge puts on every answer that passes it, a gateway's own refusal included, and on the
#: CDN's 403s, where its 405 and its 400 carry neither (``errors.atlassian.SERVED_METHODS`` has
#: those): on all 78 of 2026-09-22, on the Connect-token 403 and a `PATCH` measured 2026-09-30,
#: and on the firewall's 403 and Tomcat's 400 for an encoded slash measured 2026-10-12.
_EDGE = {"x-content-type-options": "nosniff", "x-xss-protection": "1; mode=block"}


def _gateway_headers(request: Request) -> dict[str, str]:
    """What the gateway in front of either product puts on a refusal it gives itself: the two ids
    both products carry and :data:`_EDGE`, without Jira's `x-arequestid`."""
    ids = {k: v for k, v in request_ids(request).items() if k != "x-arequestid"}
    return {**ids, **_EDGE}


def vendor_headers(request: Request, status_code: int) -> dict[str, str]:
    """Everything real puts on an `/atlassian` answer that is not the body's own.

    Both products: the two ids and :data:`_EDGE`. Jira adds `x-arequestid`, `timing-allow-origin`
    and a `cache-control`, its API's or the web app's page by page, and once a credential resolves
    the caller's own account id, with the rate-limit four where a route answers or an `OPTIONS` asks
    at its path. An answer given in front of the application carries what
    ``errors.atlassian.FrontDoor`` names for its layer, and the gateway's Connect-token 403 what
    that names for the gateway. Confluence: the millisecond clock it stamps every answer with, the
    deprecation trio where the v1 services send it, and the search's `cache-control` and `expires`.
    Measured on Atlassian Cloud 2026-09-22 and 2026-09-30, the search's pair 2026-10-07; what is
    deliberately not here is in `backlot.main.report_atlassian_headers`.
    """
    path = request.url.path
    layer = request.scope.get("atlassian_front_door")
    if layer is not None:
        # An answer given in front of the application never reaches what stamps the rest
        # (``errors.atlassian.FrontDoor`` names what each layer adds).
        if layer == "gateway":
            return _gateway_headers(request)
        return dict(_EDGE) if layer == "cdn edge" else {}
    headers = {**request_ids(request), **_EDGE}
    if errors_atlassian.is_confluence(path):
        # The search's GET/HEAD 200, 400, 403 and 404 carry the pair; Tomcat's 401 and the
        # refusal of an `Accept` do not (2026-10-13).
        if (
            request.method in ("GET", "HEAD")
            and status_code != 401
            and not request.scope.get("atlassian_accept_refused")
            and _vendor_path(path) == "/wiki/rest/api/search"
        ):
            headers["cache-control"] = "no-cache, no-store, must-revalidate"
            headers["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
        headers["x-confluence-request-time"] = str(int(time.time() * 1000))
        # the notice rides on what a v1 service answers: a route's own answer, not one given
        # around it
        if sends_deprecation(path, status_code) and _a_route_answers(request):
            headers.update(CONFLUENCE_DEPRECATION)
        return headers
    if status_code == 403 and auth.atlassian_bearer_unreadable(request):
        # The Connect-token 403 is the gateway's
        # (``backlot.main.refuse_a_bearer_jira_cannot_read``): real puts the two ids and
        # :data:`_EDGE` on it and none of Jira's own, measured 2026-09-30 on fifteen of them.
        return _gateway_headers(request)
    headers["timing-allow-origin"] = "*"
    if errors_atlassian.serves_the_jira_api(path):
        headers["cache-control"] = "no-cache, no-store, no-transform"
    else:
        # The web app's own caching, measured 2026-09-30: `/browse` says
        # `no-cache, no-store, must-revalidate`, `/browse/…` is a static shell served without Jira's
        # request id or `timing-allow-origin` and says `no-store, max-age=0, stale-if-error=0`, and
        # the root and the not-found page say nothing.
        echoed = _vendor_path(_echoed_path(request))
        if echoed == "/browse":
            headers["cache-control"] = "no-cache, no-store, must-revalidate"
        elif echoed.startswith("/browse/"):
            headers["cache-control"] = "no-store, max-age=0, stale-if-error=0"
            del headers["x-arequestid"], headers["timing-allow-origin"]
    caller = auth.atlassian_caller(request)
    if caller.is_anonymous:
        return headers
    # The admin/service token resolves to a caller with no address; `"unknown"` is what the rest
    # of this module seeds an account id from for one (see :func:`_conf_user`).
    headers["x-aaccountid"] = synth.atlassian_account_id(caller.email or "unknown")
    # the quota rides on what a route answers, and on an `OPTIONS` at its path; a 405 carries none,
    # and neither does the 406 a route refuses an `Accept` with (`POST search/jql`, 2026-10-13)
    if not request.scope.get("atlassian_accept_refused") and (
        _a_route_answers(request)
        or (request.method == "OPTIONS" and _some_atlassian_route_matches(request))
    ):
        headers.update(rate_limit_headers(request, caller))
    return headers
