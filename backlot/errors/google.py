"""Google's error envelope, per API family.

`google-api-python-client` reads ``error.message`` to build its ``HttpError``, and real clients
branch on ``error.status`` or ``errors[].reason``. FastAPI's default ``{"detail": …}`` gives them
none of that, so error handling could not be developed or tested against Backlot even though its
status codes were already right.

Everything here was measured against the live Docs / Drive / Gmail / Sheets / Slides APIs. The
envelope is NOT uniform — three families differ in which optional members they carry:

    family                  errors[]           status               no Authorization header
    ------------------------|------------------|---------------------|------------------------
    Drive v3                | always           | auth failures and   | 403 PERMISSION_DENIED
                            |                  | typed values only   |
    Gmail v1                | unless $.xgafv=2 | always              | 401 UNAUTHENTICATED
    Docs v1 / Slides v1     | $.xgafv=1        | always              | 401 UNAUTHENTICATED
    Sheets v4               | $.xgafv=1        | always              | 403 PERMISSION_DENIED

Sheets parts from the other two editor APIs on that last column: measured, a request with no
Authorization header is 403 PERMISSION_DENIED with the unregistered-caller sentence, where Docs
answers 401 UNAUTHENTICATED with the missing-credential one. A present-but-invalid token is 401
UNAUTHENTICATED in every family, which is why a missing header and a bad token are separate
constructors here rather than one "unauthorized".

`errors[]` is what `$.xgafv` selects, and the middle column above is the whole rule
(:func:`has_errors_array`). It is a SYSTEM parameter — a top-level entry of a discovery document's
``parameters``, which every method takes — so it is validated once for the router
(:func:`validate_system_parameters`) and declared once for the document
(:func:`backlot.openapi.google_system_parameters`) rather than route by route. Measured: Drive
carries the array on a `fields` refusal at `2` as well as with no parameter; the LAST repeat is the
value every rule about THIS parameter reads (`2&1` carries it on Sheets where `1&2` does not, `2&0`
on Gmail — a refused value is not a `2` — where `0&2` does not), which is the opposite of the other
system parameters (:func:`first_repeat`); a success body is the same under all of them; and a value
other than `1` or `2` is refused ahead of a bad token, a missing credential and an unparseable range
alike.

`callback` is the second system parameter :func:`validate_system_parameters` checks, and the one
that decides how a body reaches the wire rather than what is in it: see :func:`respond`, which is
also where the indentation and the charset every Google error carries are decided.

Inside `errors[]` the entry follows the constructor that raised it, and each one carries its own
measurement. Measured on Sheets and Docs at `$.xgafv=1`: a typed value the proto layer refuses is
``reason: invalid`` with NO ``domain`` (:func:`invalid_field_value`); every other measured 400 is
``badRequest`` under ``global`` (:func:`invalid_argument`, :func:`bad_field_mask`); a 404 is
``notFound``; a bad token ``authError`` at ``location: Authorization``; an anonymous Sheets request
``forbidden``; an anonymous request on any of the three OAuth-only APIs ``required`` with the short
``Login Required.``. The two editor 400s NOT measured keep whatever their constructor already
renders — ``Invalid gridRange`` is :func:`invalid_argument`, so ``badRequest``, but an Office file
read as a native document is :func:`failed_precondition`, so ``failedPrecondition``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from fastapi import HTTPException, Request, Response

DRIVE, GMAIL, EDITOR = "drive", "gmail", "editor"
# `status` needs no per-family flag: Drive's parameter failures other than a typed value simply do
# not have one, while every Gmail and editor error does, so "the error carries a status" is the
# whole condition.
_PREFIX_FAMILY = (
    ("/drive/v3", DRIVE),
    ("/gmail/v1", GMAIL),
    ("/docs/v1", EDITOR),
    ("/sheets/v4", EDITOR),
    ("/slides/v1", EDITOR),
)

# The long forms Google actually sends. Kept verbatim: a client that matches on the message needs
# the real text, and the short "Invalid Credentials" belongs in `errors[0]`, not at the top.
BAD_TOKEN_MESSAGE = (
    "Request had invalid authentication credentials. Expected OAuth 2 access token, login cookie "
    "or other valid authentication credential. See "
    "https://developers.google.com/identity/sign-in/web/devconsole-project."
)
MISSING_CREDENTIALS_MESSAGE = (
    "Request is missing required authentication credential. Expected OAuth 2 access token, login "
    "cookie or other valid authentication credential. See "
    "https://developers.google.com/identity/sign-in/web/devconsole-project."
)
UNREGISTERED_CALLER_MESSAGE = (
    "Method doesn't allow unregistered callers (callers without established identity). Please use "
    "API Key or other form of API consumer identity to call this API."
)


def family(path: str) -> str | None:
    """Which of the three envelopes a request path takes, or ``None`` for a non-Google route."""
    for prefix, fam in _PREFIX_FAMILY:
        if path.startswith(prefix):
            return fam
    return None


def owns(path: str) -> bool:
    """Whether this module shapes errors for ``path`` — the question ``backlot.errors`` asks every
    envelope. Google is the one vendor where the answer is more than a prefix test, because the
    five API families it serves live under five different ones."""
    return family(path) is not None


class GoogleError(HTTPException):
    """An error carrying everything its envelope needs.

    ``reason``/``location`` populate ``errors[0]`` for the families that send it; ``status`` is the
    canonical code name. ``short`` is a distinct ``errors[0].message``, where Google's top-level
    message is the long form: the bad-token 401 says "Invalid Credentials" and the
    missing-credential 401 says "Login Required."."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        reason: str | None = None,
        location: str | None = None,
        location_type: str = "parameter",
        status: str | None = None,
        short: str | None = None,
        details: list | None = None,
        domain: str | None = "global",
    ):
        super().__init__(status_code=status_code, detail=message)
        self.message = message
        self.reason = reason
        self.location = location
        self.location_type = location_type
        self.status = status
        self.short = short
        self.details = details
        # `errors[0].domain`. Every measured entry says `global` except the proto layer's typed-value
        # refusal, which carries none — so a constructor that renders that one passes ``None``.
        self.domain = domain


# --- constructors: the call site names the KIND of failure, which is what only it knows ---------


def required(param: str, message: str | None = None) -> GoogleError:
    """A parameter the method cannot run without. Google's wording is ``Required parameter: X``,
    except on ``about.get`` where it spells out the sentence — hence the override."""
    return GoogleError(
        400, message or f"Required parameter: {param}", reason="required", location=param
    )


def invalid_parameter(param: str, message: str) -> GoogleError:
    """A parameter whose value names something that does not exist (a mistyped `fields` mask)."""
    return GoogleError(400, message, reason="invalidParameter", location=param)


def invalid_value(param: str, message: str | None = None) -> GoogleError:
    """A parameter whose value is not accepted. Google says only ``Invalid Value``; a caller may
    pass a fuller message where Backlot can explain a refusal Google does not have."""
    return GoogleError(400, message or "Invalid Value", reason="invalid", location=param)


def not_found_file(file_id: str) -> GoogleError:
    """Drive's not-found, which names the id so a batch caller can tell which request failed."""
    return GoogleError(404, f"File not found: {file_id}.", reason="notFound", location="fileId")


def not_found_entity() -> GoogleError:
    """The not-found every API other than Drive gives: no id, no location."""
    return GoogleError(
        404, "Requested entity was not found.", reason="notFound", status="NOT_FOUND"
    )


def not_exportable() -> GoogleError:
    return GoogleError(403, "Export only supports Docs Editors files.", reason="fileNotExportable")


def not_downloadable() -> GoogleError:
    return GoogleError(
        403,
        "Only files with binary content can be downloaded. Use Export with Docs Editors files.",
        reason="fileNotDownloadable",
        location="alt",
    )


def invalid_argument(message: str) -> GoogleError:
    """The editor APIs' generic 400. Its `errors[]` entry, shown at `$.xgafv=1`, is ``badRequest``
    under ``global`` — measured on an unparseable range, a range past the grid, an unsupported
    ``alt``, ``dataFilter.filter must be specified.``, ``No sheet with id``, ``Must specify at least
    one dataFilter.`` and a non-JSON body. A typed value the proto layer refuses is a different
    entry: :func:`invalid_field_value`."""
    return GoogleError(400, message, reason="badRequest", status="INVALID_ARGUMENT")


def invalid_field_value(field: str, message: str) -> GoogleError:
    """The proto layer's refusal of a typed value — ``Invalid value at '<field>' (<type>),
    "<value>"`` for an enum, a bool or an int32. Measured at `$.xgafv=1`, its `errors[]` entry is
    ``reason: invalid`` and carries no ``domain``, which no other Google error measured does."""
    return invalid_field_values([(field, message)])


def invalid_field_values(violations: list[tuple[str, str]]) -> GoogleError:
    """One refusal for every typed value the proto layer could not read, as ``(field, message)``
    pairs in the order to report them.

    Measured 2026-09-23 on Sheets `values.get`, `spreadsheets.get` and `:getByDataFilter` and on
    Drive `files.list`, over query parameters and a JSON body alike: each refused value is a
    ``google.rpc.BadRequest`` field violation in `details`, naming the field the way its message
    does and repeating the message as its description, and a request with several is one 400 whose
    message joins theirs with newlines, in the order `details` lists them. Which order that is,
    is ``routers.google._typed_query``'s."""
    return GoogleError(
        400,
        "\n".join(message for _, message in violations),
        reason="invalid",
        status="INVALID_ARGUMENT",
        domain=None,
        details=[
            {
                "@type": "type.googleapis.com/google.rpc.BadRequest",
                "fieldViolations": [
                    {"field": field, "description": message} for field, message in violations
                ],
            }
        ],
    )


def field_violations(exc: GoogleError) -> list[tuple[str, str]]:
    """The ``(field, message)`` pairs an :func:`invalid_field_values` refusal carries, so a caller
    reading several values can gather every refusal into one."""
    return [(v["field"], v["description"]) for d in exc.details or () for v in d["fieldViolations"]]


def unsupported_conversion() -> GoogleError:
    """`files.export` asked for a format the file's type does not export to. Measured 2026-09-23
    on a spreadsheet: `text/plain`, `bogus/type`, a native Google type, a padded `text/csv ` and an
    empty value each answer this, ``badRequest`` at ``location: convertTo``."""
    return GoogleError(
        400, "The requested conversion is not supported.", reason="badRequest", location="convertTo"
    )


def bad_field_mask(path: str) -> GoogleError:
    """A ``fields`` mask naming something the response has no field for.

    Measured on Sheets: the top-level message is the generic ``Request contains an invalid
    argument.`` and the path that failed is named only inside a ``google.rpc.BadRequest`` detail —
    so a client that wants to know WHICH path was wrong has to read `details`."""
    return GoogleError(
        400,
        "Request contains an invalid argument.",
        reason="badRequest",
        status="INVALID_ARGUMENT",
        details=[
            {
                "@type": "type.googleapis.com/google.rpc.BadRequest",
                "fieldViolations": [
                    {
                        "field": path,
                        "description": (
                            "Error expanding 'fields' parameter. Cannot find matching fields for "
                            f"path '{path}'."
                        ),
                    }
                ],
            }
        ],
    )


def invalid_id_value() -> GoogleError:
    """Gmail's answer to an id it cannot parse — measured: 400 INVALID_ARGUMENT "Invalid id value"
    for a non-hex id or one at/above 2**63, where a well-formed but unknown id is 404 instead."""
    return GoogleError(400, "Invalid id value", reason="invalidArgument", status="INVALID_ARGUMENT")


def failed_precondition(message: str) -> GoogleError:
    """The editor APIs' "right shape, wrong state" 400 — an Office file read as a native doc."""
    return GoogleError(400, message, reason="failedPrecondition", status="FAILED_PRECONDITION")


def bad_token() -> GoogleError:
    """A present-but-invalid bearer: 401 in every family."""
    return GoogleError(
        401,
        BAD_TOKEN_MESSAGE,
        reason="authError",
        location="Authorization",
        location_type="header",
        status="UNAUTHENTICATED",
        short="Invalid Credentials",
    )


def missing_credentials() -> GoogleError:
    """No Authorization header, on an OAuth-only API (Gmail, Docs, Slides).

    One answer for the three: measured 2026-09-14, Gmail, Docs and Slides send the same long
    top-level message and the same `errors[]` entry, the short ``Login Required.`` at ``location:
    Authorization``. Which of them SHOWS that entry still differs — Gmail carries it unless
    `$.xgafv=2`, the editor families only at `1` — but that is `has_errors_array`'s rule, not a
    difference in the error."""
    return GoogleError(
        401,
        MISSING_CREDENTIALS_MESSAGE,
        reason="required",
        location="Authorization",
        location_type="header",
        status="UNAUTHENTICATED",
        short="Login Required.",
    )


def unregistered_caller() -> GoogleError:
    """No Authorization header, on an API that also accepts API keys (Drive, Sheets) — so an
    anonymous request is a caller with no established identity rather than a missing credential."""
    return GoogleError(
        403, UNREGISTERED_CALLER_MESSAGE, reason="forbidden", status="PERMISSION_DENIED"
    )


def no_credentials(path: str) -> GoogleError:
    """The right anonymous-request error for this path. Sheets shares the editor ENVELOPE with Docs
    and Slides but not this behaviour, so it is resolved from the path rather than the family."""
    if family(path) == DRIVE or path.startswith("/sheets/v4"):
        return unregistered_caller()
    return missing_credentials()


# --- system parameters ---------------------------------------------------------------------------

XGAFV = "$.xgafv"
XGAFV_VALUES = ("1", "2")


def bad_system_parameter(name: str, value: str) -> GoogleError:
    """A system parameter with a value it does not take. Measured on Sheets and Drive for `$.xgafv`
    at `0`, `3`, `NOPE`, `01` and the empty string: 400 INVALID_ARGUMENT with this sentence, spacing
    included, and on Drive an `errors[]` entry of ``badRequest`` under ``global``."""
    return GoogleError(
        400,
        f"Invalid query parameters. Invalid value '{value}' for system query parameter : {name}",
        reason="badRequest",
        status="INVALID_ARGUMENT",
    )


def xgafv(query: Mapping[str, str] | None) -> str | None:
    """The `$.xgafv` a request sent, or ``None``. Starlette's ``QueryParams.get`` answers the LAST
    repeat, which is the one real reads -- and `$.xgafv` is the one system parameter that works
    that way. Measured 2026-09-15 on Sheets: `1&2` carries no `errors[]` where `2&1` does. Which
    end every other measured parameter is read from is :func:`first_repeat`'s table."""
    return None if query is None else query.get(XGAFV)


def first_repeat(query: Mapping[str, str] | None, name: str) -> str | None:
    """The FIRST repeat of ``name``, or ``None`` when the request does not send it.

    Real reads a repeated query parameter from one end or the other, and no rule divides the two:
    `prettyPrint` and `$.xgafv` are both system parameters, `pageSize` and `majorDimension` both
    method parameters, and in each pair one is read first and the other last. So this is a table,
    one ordered pair per row, each sent both ways round so the answer names the end that was read.
    ``QueryParams.get`` answers the last repeat, so the untyped parameters in the first group are
    read through here and those in the second off ``.get``; a typed one has every repeat parsed by
    ``routers.google._typed_query`` and is read from the end its group names::

        read first          the pair, and what real answers       measured on
        ------------------|--------------------------------------|-------------------------------
        callback          | `cb&dd` calls `cb`                   | Sheets 2026-09-15
        alt               | `media&json` is the `media` refusal  | Sheets 2026-09-15
                          |                                      | Drive files.get 2026-09-17
        fields            | `<mask>&bogus` answers the mask,     | Sheets values.get,
                          | `bogus&<mask>` a 400                 | values:batchGet,
                          |                                      | spreadsheets.get and both
                          |                                      | POSTs; Drive files.list,
                          |                                      | files.get and about;
                          |                                      | 2026-09-23
        prettyPrint       | `false&true` is compact,             | Sheets values.get,
                          | `true&false` indented                | values:batchGet,
                          |                                      | spreadsheets.get and both
                          |                                      | POSTs; 2026-09-23
        q                 | `<folders>&<sheets>` lists folders   | Drive files.list 2026-09-23
        pageSize          | `1&3` is one file                    | Drive files.list 2026-09-23
        pageToken         | `<valid>&BOGUS` is the next page,    | Drive files.list 2026-09-23
                          | `BOGUS&<valid>` a 400                |
        orderBy           | `name&name desc` ascends             | Drive files.list 2026-09-23
        mimeType          | `text/csv&text/tab-separated-values` | Drive files.export 2026-09-23
                          | answers CSV                          |

        read last
        ------------------|--------------------------------------|-------------------------------
        $.xgafv           | `1&2` has no `errors[]`, `2&1` has   | Sheets 2026-09-15
                          |                                      | Gmail 2026-09-22
        majorDimension    | `ROWS&COLUMNS` answers `COLUMNS`     | Sheets values.get 2026-09-23
        valueRenderOption | `FORMULA&FORMATTED_VALUE` answers    | Sheets values.get 2026-09-23
                          | the value, the reverse the formula   |
        includeGridData   | `true&false` answers no grid         | Sheets spreadsheets.get
                          |                                      | 2026-09-22

    An empty first repeat is read as itself rather than skipped, measured 2026-09-23:
    `q=&q=<folders>` is the unfiltered listing, `fields=&fields=id` on Drive `files.get` answers
    ``{}``, and `prettyPrint=&prettyPrint=false` is indented, each what the empty value alone
    answers.

    A second repeat of a parameter read first is not validated, measured 2026-09-23:
    `fields=id&fields=bogus`, `q=<folders>&q=<a clause Drive cannot parse>`,
    `orderBy=name&orderBy=bogus`, `pageToken=<valid>&pageToken=BOGUS` and
    `mimeType=text/csv&mimeType=bogus/type` each answer what their first value alone does, as
    `callback=cb&callback=a b` did on 2026-09-15. `pageSize` is the exception:
    `pageSize=2&pageSize=NOPE` is a 400 whichever end the bad value is at, and so is a bad value at
    either end of `valueRenderOption`, `dateTimeRenderOption`, `includeGridData` or
    `excludeTablesInBandedRanges`, measured the same day, and of `majorDimension`, measured
    2026-09-22.

    Gmail's `q`, `pageToken` and `maxResults` stay on ``.get`` because their end is unmeasured: a
    Gmail list answers 200 only to a scope the measuring credential cannot be granted.
    """
    if query is None:
        return None
    getlist = getattr(query, "getlist", None)
    if getlist is None:
        return query.get(name)
    values = getlist(name)
    return values[0] if values else None


ALT = "alt"
CALLBACK = "callback"
# The characters a JSONP callback name may be built from, quoted from the refusal real writes when
# one is not: "only alphabet, number, '_', '$', '.', '[' and ']' are allowed." Measured character by
# character on Sheets, sending `cb<CH>x` for each of the ASCII punctuation marks and for space, tab,
# newline and `\u00e9`, and `a<ZWSP>b` besides: the seven classes that sentence names are accepted
# and every character sent outside them is refused, so "alphabet" is ASCII letters and nothing
# wider. Position does not matter -- `.cb`, `cb.`, `1bad` and `$` are all accepted, and a
# 2,000-character name is as well.
_CALLBACK_NAME = re.compile(r"[A-Za-z0-9_$.\[\]]+")


def bad_jsonp_callback(name: str) -> GoogleError:
    """A `callback` whose value cannot be a JavaScript name. Measured on Sheets, Drive and Gmail,
    authenticated and anonymous: 400 INVALID_ARGUMENT with this sentence, and its `errors[]` entry
    is ``badRequest`` under ``global`` -- so :func:`invalid_argument`, spelled out here only for the
    message. The refusal still arrives WRAPPED, through the name it just refused."""
    return invalid_argument(
        f"Invalid JSONP callback name: '{name}'; only alphabet, number, '_', '$', '.', '[' and "
        "']' are allowed."
    )


def alt_format(query: Mapping[str, str] | None) -> str:
    """The format `alt` asks for, casefolded, with an absent or empty parameter answering ``""``.

    `alt` is matched without regard to case and an empty `alt=` is no value at all. Measured
    2026-09-17, anonymous on Sheets, Docs, Drive, Gmail and Slides and authenticated on Sheets:
    `alt=JSON`, `alt=Json` and `alt=` each answer the 200 a bare request does, and each is wrapped
    beside a `callback` exactly as `alt=json` is. Reading the value literally answered all three at
    the error status, unwrapped.

    The refusals split on the same measurement, which is why this returns the folded value and the
    caller keeps the sent one: `alt=MEDIA` and `alt=Media` answer ``Unsupported alt type "media"``
    with the format lowercased, while `alt=ZZZ` answers ``Invalid value "ZZZ"`` through the
    spelling it received.
    """
    return (first_repeat(query, ALT) or "").casefold()


def jsonp_callback(request: Request) -> str | None:
    """The `callback` this request is answered through, or ``None`` for a plain JSON body.

    The REQUEST, not its query alone, because the method decides too: JSONP is what a `<script>`
    element fetches, and a `<script>` element issues a GET. Measured on Sheets, a `callback` on
    `values:batchGetByDataFilter` and on `spreadsheets:getByDataFilter` is ignored outright -- no
    wrap on a success, none on an error, and a name that a GET would be refused for is not even
    looked at -- where the same POST honours `$.xgafv` and `prettyPrint`. So GET is the whole of
    where this parameter applies.

    Two values that look like a callback are not one either. An empty `callback=` is absent:
    measured, it answers the plain body at the real status, success and error alike. So is any
    `alt` NAMING A FORMAT other than `json` -- which spellings do name that format is
    :func:`alt_format`'s half, and `JSON` and an empty `alt=` are among them. Measured on Sheets,
    `alt=media`, `alt=proto` and `alt=zzz` each answer their own 400 unwrapped, even when
    `callback` is itself unparseable, so an `alt` whose format the API cannot render takes the
    request out of the JSONP path along with the JSON one.
    That suppression is not Sheets' own: measured 2026-09-16, `alt=media` and `alt=zzz` beside a
    `callback` answer unwrapped on Drive, Gmail, Docs and Slides too, which is why `alt` is read
    for every family here rather than only where ``routers.google._sheets_respond`` refuses the
    value. Refusing it is still Sheets-only, and the four families that accept a format they cannot
    render where real answers a 400 are a gap of their own.

    Which `alt` counts as JSON is :func:`alt_format`'s question, not this one's -- `alt=JSON` and
    `alt=` are the JSON the default spells, and answering them unwrapped is the divergence that
    reading the value literally here used to produce.

    Both are read as :func:`first_repeat`, not off ``QueryParams.get``: real answers a repeated
    `callback` through the first name and a repeated `alt` through the first format.
    """
    if request.method != "GET":
        return None
    query = request.query_params
    alt = alt_format(query)
    if alt and alt != "json":
        return None
    return first_repeat(query, CALLBACK) or None


def validate_system_parameters(request: Request) -> None:
    """Refuse a `$.xgafv` other than `1` or `2`, or a `callback` that cannot be a JavaScript name,
    on a Google-family path, before the route runs.

    A router-level dependency, so it is the first thing a request meets: measured, real answers the
    `$.xgafv` 400 ahead of a bad token, a missing credential and an unparseable range, and the
    `callback` 400 ahead of the same three and of a mistyped `fields` mask. `$.xgafv` goes first
    because it beats `callback` too -- measured, `callback=a b&$.xgafv=9` answers the `$.xgafv`
    sentence, wrapped through the very name the other check would have refused. The batch endpoint
    is not a family path and is left alone.
    """
    if family(request.url.path) is None:
        return
    # That this ran at all is what :func:`rendered` needs to know, and only this call can say so:
    # a ROUTER dependency runs once a route has matched, so an unrouted family path reaches the
    # renderer with a `callback` nothing has looked at.
    request.state.google_system_parameters_checked = True
    value = xgafv(request.query_params)
    if value is not None and value not in XGAFV_VALUES:
        raise bad_system_parameter(XGAFV, value)
    callback = jsonp_callback(request)
    if callback is not None and not _CALLBACK_NAME.fullmatch(callback):
        raise bad_jsonp_callback(callback)


def has_errors_array(fam: str, value: str | None) -> bool:
    """Whether a family's error carries `errors[]` under the `$.xgafv` the request sent.

    Three rules, one per family, each measured. Drive always carries it. Gmail carries it unless
    the value is `2`, so an absent parameter and a value Google refuses both keep it. The editor
    families carry it only at `1`."""
    if fam == DRIVE:
        return True
    if fam == GMAIL:
        return value != "2"
    return value == "1"


def http_body(path: str, exc: HTTPException, query: Mapping[str, str] | None = None) -> dict:
    """Render an exception into its family's envelope.

    The EXCEPTION, not its ``detail``: a :class:`GoogleError` carries the reason / location / status
    as attributes on itself, and reading them off the detail string would silently flatten every
    error to a bare message. A plain ``HTTPException`` raised on a Google path still renders — it
    just carries no reason — so a route that has not been migrated degrades instead of 500ing.

    ``query`` is the request's, for the one thing the envelope reads off it: `$.xgafv=1` puts
    `errors[]` on an editor-family error.
    """
    message = getattr(exc, "message", None)
    if message is None:
        detail = exc.detail
        message = detail if isinstance(detail, str) else str(detail)
    err: dict = {"code": exc.status_code, "message": message}
    if has_errors_array(family(path), xgafv(query)):
        entry = {"message": getattr(exc, "short", None) or message}
        domain = getattr(exc, "domain", "global")
        if domain:
            entry["domain"] = domain
        reason = getattr(exc, "reason", None)
        if reason:
            entry["reason"] = reason
        location = getattr(exc, "location", None)
        if location:
            entry["location"] = location
            entry["locationType"] = getattr(exc, "location_type", "parameter")
        err["errors"] = [entry]
    status = getattr(exc, "status", None)
    if status:
        err["status"] = status
    # `details` carries the google.rpc payloads a few failures add under the message — measured on
    # a mistyped Sheets `fields` mask, whose BadRequest names the path that could not be expanded.
    # After `status`, which is the order the measured bodies come back in.
    details = getattr(exc, "details", None)
    if details:
        err["details"] = details
    return {"error": err}


def validation_body(path: str, errors) -> None:
    """None: keep FastAPI's own 422 body. A bad parameter on a Google route is refused by the router
    with a :class:`GoogleError`, so FastAPI's validator is not the path that reports it."""
    return None


# --- how a Google body reaches the wire ---------------------------------------------------------


# The characters real's serializer writes as `\\uXXXX` rather than as themselves, beyond the ones
# JSON requires of every serializer. Measured 2026-09-17 by sending each of the 1,112,063
# codepoints a query string carries -- every one but the surrogates and U+0000, which the front end
# hands back as the literal `%00` rather than decoding -- through the `alt` echo on Sheets in 1,013
# requests, and reading which came back escaped. 209 do, and these are the 176 of them that
# ``json.dumps`` leaves alone -- it writes the other 33 itself, U+0001-U+001F and the two
# characters JSON reserves.
#
# The set cannot be written as a category test. It is the format category as Unicode 4.0 drew it:
# U+17B4 and U+17B5 are in it though they have been Mn since 4.1, and U+061C, U+0604 and U+180E are
# out of it though each is Cf today. The line and paragraph separators come with it. Everything
# else stays as it is -- letters, emoji, NBSP, U+3000, `&` and `'` among them.
_ALSO_ESCAPED = (
    (0x003C, 0x003C),
    (0x003E, 0x003E),
    (0x007F, 0x009F),
    (0x00AD, 0x00AD),
    (0x0600, 0x0603),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x17B4, 0x17B5),
    (0x200B, 0x200F),
    (0x2028, 0x202E),
    (0x2060, 0x2064),
    (0x206A, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)

_ALSO_ESCAPED_RE = re.compile(
    "[%s]" % "".join(f"{chr(lo)}-{chr(hi)}" if lo != hi else chr(lo) for lo, hi in _ALSO_ESCAPED)
)


def _escape(match: "re.Match[str]") -> str:
    """One character as the `\\uXXXX` real writes, in the surrogate pair it writes above the BMP."""
    cp = ord(match.group())
    if cp <= 0xFFFF:
        return "\\u%04x" % cp
    cp -= 0x10000
    return "\\u%04x\\u%04x" % (0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF))


def _escaped(text: str) -> str:
    """Serialized JSON with the characters real escapes and ``json.dumps`` does not.

    ``json.dumps`` already writes U+0000-U+001F, `"` and `\\` the way real does, down to the short
    forms -- measured, a value carrying U+0008 through U+000D comes back as `\\b\\t\\n\\u000b\\f\\r`,
    which is Python's spelling exactly. What it leaves raw and real does not is :data:`_ALSO_ESCAPED`.

    Applied to the serialized text rather than to the values inside it, which is safe for exactly
    this set: none of these characters is part of JSON's grammar, so every one of them the text
    holds is already inside a string. U+0000-U+001F could not be handled here for that reason --
    the newlines an indented body is laid out with are the same character.

    It reaches the plain body as much as the wrapped one and a success as much as an error:
    measured, a Sheets cell holding ``<b>&'x`` comes back with the brackets escaped and `&`, `'`
    and the letters raw, and an error echoing an unparseable range spelled the same way matches it.
    So this is the serializer's rule, not the JSONP path's.
    """
    return _ALSO_ESCAPED_RE.sub(_escape, text)


def _escaped_name(name: str) -> str:
    """A callback name the way real writes it into ``// API callback\\n<name>(``.

    The wrapper needs more of the rule than the body does: a name is not serialized JSON, so
    nothing has escaped its quotes, backslashes and C0 controls yet. ``json.dumps`` does that half
    -- measured, `cb<TAB>x` is called through `cb\\tx` and `cb<BACKSLASH>x` through `cb\\\\x` --
    and :func:`_escaped` does the rest.

    Real refuses every one of these names, since the character set it accepts is ASCII letters,
    digits and ``_$.[]``. The refusal still arrives wrapped, through the very name it refuses, so
    the escaping is what the client reads.
    """
    return _escaped(json.dumps(name, ensure_ascii=False)[1:-1])


def respond(
    body: dict,
    *,
    compact: bool = False,
    callback: str | None = None,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """One Google body on the wire, rendered the way real renders it.

    Measured to the byte: compact puts no space after `:` or `,` and ends without a newline, while
    the indented form is two spaces deep and DOES end with one, and the plain type names a charset.

    A `callback` makes the answer a script rather than a body: measured across Sheets, Docs, Drive,
    Gmail and Slides, authenticated and anonymous, a 400, a 401, a 403 and a 404 each came back
    **200** with `text/javascript; charset=UTF-8` and the body inside ``// API callback\\ncb(…);``.
    The status the caller would have seen survives only inside `error.code`, which is the point of
    JSONP: a browser loading the answer through a `<script>` element can read neither a status nor
    a body that did not arrive as JavaScript.
    """
    text = _escaped(
        json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        if compact
        else json.dumps(body, ensure_ascii=False, indent=2) + "\n"
    )
    if callback is None:
        return Response(
            text,
            status_code=status_code,
            media_type="application/json; charset=UTF-8",
            headers=headers,
        )
    return Response(
        f"// API callback\n{_escaped_name(callback)}({text});",
        status_code=200,
        media_type="text/javascript; charset=UTF-8",
        headers=headers,
    )


def rendered(
    request: Request,
    status_code: int,
    body: dict,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """The whole response for a Google error, which real renders exactly as it renders a success.

    Indented whatever `prettyPrint` says -- measured on Sheets, Docs, Drive, Gmail and Slides, an
    error came back two-space indented with no parameter, with `prettyPrint=false` and with
    `prettyPrint=true` alike, where a success under `prettyPrint=false` is compact. So the
    parameter reaches the success path only (``routers.google._sheets_respond``) and nothing here
    reads it.

    Wrapped only where ``validate_system_parameters`` ran, which is where a route matched. That is a
    ROUTER dependency, so a family path with NO route -- `/sheets/v4/nope` -- reaches this having
    been refused nothing, and `callback=a b` there would be answered by calling `a b`. Real answers
    such a path from its front end as HTML, measured 2026-09-16 with a `callback` and without: 400
    on Sheets, Docs and Slides, 404 on Drive and Gmail. So JSONP is not its shape there under any
    name, and the plain body is the nearer of the two answers Backlot can give.

    Where the check DID run the name needs no second look, and the body being wrapped may BE its
    refusal -- that is real's own answer, measured the same day: `callback=evil);alert(1);//` on a
    Sheets read comes back 200 calling that very name, escaped the way :func:`_escaped_name`
    escapes one.

    It takes the whole request because :func:`jsonp_callback` reads the method as well as the query.
    """
    checked = getattr(request.state, "google_system_parameters_checked", False)
    callback = jsonp_callback(request) if checked else None
    return respond(body, callback=callback, status_code=status_code, headers=headers)
