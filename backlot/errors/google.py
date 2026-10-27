"""Google's error envelope, per API family.

`google-api-python-client` reads ``error.message`` to build its ``HttpError``, and real clients
branch on ``error.status`` or ``errors[].reason``. FastAPI's default ``{"detail": …}`` gives them
none of that, so error handling could not be developed or tested against Backlot even though its
status codes were already right.

Everything here was measured against the live Docs / Drive / Gmail / Sheets / Slides APIs. The
envelope is NOT uniform — three families differ in which optional members they carry:

    family                  errors[]           status
    ------------------------|------------------|-----------------------------------------------
    Drive v3                | always           | auth failures, typed values, `$.xgafv`, the
                            |                  | batch redirect and `Unknown Error.` only
    Gmail v1                | unless $.xgafv=2 | always
    Docs v1 / Slides v1     | $.xgafv=1        | always
    Sheets v4               | $.xgafv=1        | always

A request that carries no credential is refused by operation rather than by family: the 403 of an
unregistered caller on the operations that also take an API key, the 401 of a missing credential on
the rest (:func:`no_credentials`), and on a download the 403 that names the missing API key when no
`Authorization` is sent (:func:`missing_api_key`), or :func:`download_credential_refused`'s 401 when
one is sent that is no credential. One whose credential does not resolve is the 401 of
:func:`bad_token` in every family, which is why a missing credential and a bad token are separate
constructors here rather than one "unauthorized". What counts as a credential is
``routers.google._credential``'s to say.

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
``reason: invalid`` with NO ``domain`` (:func:`invalid_field_value`), and so are a JSON body member
the request message does not have and a JSON body that is not an object
(:func:`invalid_field_values`); a request body that is not JSON is ``parseError``
(:func:`invalid_json`); an Office file read through the API of its own family is
``failedPrecondition`` under ``global`` (:func:`failed_precondition`); every other measured 400 is
``badRequest`` under ``global`` (:func:`invalid_argument`, :func:`bad_field_mask`); a 404 is
``notFound``; a file of the API's own type that the caller cannot see ``forbidden``
(:func:`permission_denied`); a bad token ``authError`` at ``location: Authorization``; an
unregistered caller ``forbidden``; the missing credential ``required`` with the short
``Login Required.``; and the 500 the data-filter reads answer ``backendError``
(:func:`internal_error`). Drive's `Unknown Error.` 500 carries one entry with no members at all
(:func:`unknown_error`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from fastapi import HTTPException, Request, Response

DRIVE, GMAIL, EDITOR = "drive", "gmail", "editor"
# `status` needs no per-family flag: each constructor passes one exactly where the module
# docstring's table says its family carries one, so "the error carries a status" is the whole
# condition.
_PREFIX_FAMILY = (
    ("/drive/v3", DRIVE),
    ("/gmail/v1", GMAIL),
    ("/docs/v1", EDITOR),
    ("/sheets/v4", EDITOR),
    ("/slides/v1", EDITOR),
)
PREFIXES = tuple(prefix for prefix, _ in _PREFIX_FAMILY)

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
MISSING_API_KEY_MESSAGE = "The request is missing a valid API key."
QUERY_TOKEN_MESSAGE = (
    "OAuth token was passed in the query parameter. Please send it in Authorization header instead."
)

# The query parameters real reads an OAuth token from, in the order its front end ranks them
# (``routers.google._query_credential``). A byte-stream read refuses any of them
# (:func:`token_in_query`).
QUERY_TOKENS = ("access_token", "bearer_token", "oauth_token")


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
        empty_entry: bool = False,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.message = message
        self.reason = reason
        self.location = location
        self.location_type = location_type
        self.status = status
        self.short = short
        self.details = details
        # `errors[0].domain`. Every measured entry says `global` except the proto layer's refusals
        # (a typed value, a body member or root the request message cannot take), which carry none —
        # so a constructor that renders one of those passes ``None``.
        self.domain = domain
        # `errors[0]` rendered as `{}`, the entry :func:`unknown_error` carries.
        self.empty_entry = empty_entry


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


def page_token_expired() -> GoogleError:
    """Drive's 403 for a page token it calls expired, which carries no `location`."""
    return GoogleError(
        403,
        "The specified page token has expired, and can no longer be used.",
        reason="pageTokenExpired",
    )


def duplicate_sort_keys() -> GoogleError:
    """Drive's refusal of an ``orderBy`` that names one sort key twice — a 403, not a 400, measured
    against Drive v3 on 2026-10-03 and 2026-10-04."""
    return GoogleError(
        403,
        "The orderBy parameter cannot contain duplicate sort keys.",
        reason="orderByContainsDuplicateSortKeys",
        location="orderBy",
    )


def sorting_not_supported_fulltext() -> GoogleError:
    """Drive's refusal of an ``orderBy`` on queries with fullText terms — a 403, measured against
    Drive v3 on 2026-10-05 and 2026-10-07."""
    return GoogleError(
        403,
        "Sorting is not supported for queries with fullText terms. Results are always in descending relevance order.",
        reason="forbidden",
        location="orderBy",
    )


def not_found_file(file_id: str) -> GoogleError:
    """Drive's not-found, which names the id so a batch caller can tell which request failed."""
    return GoogleError(404, f"File not found: {file_id}.", reason="notFound", location="fileId")


def permission_denied() -> GoogleError:
    """An editor API resolved a file of its own type but the caller cannot read it. Measured on
    Sheets and Docs on 2026-10-06; unlike a missing id, this is a 403 with ``forbidden``."""
    return GoogleError(
        403,
        "The caller does not have permission",
        reason="forbidden",
        status="PERMISSION_DENIED",
    )


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


def supports_all_drives_required() -> GoogleError:
    """`files.list` asked for shared-drive items without saying it supports shared drives. No
    `location` and no `status`, measured 2026-10-04."""
    return GoogleError(
        403,
        "The supportsAllDrives parameter was not set to true.",
        reason="supportsTeamDrivesRequired",
    )


def domain_admin_privilege_required() -> GoogleError:
    """`drives.list` asked for a domain administrator's access, from a Workspace member who is not
    one. No `location` and no `status`, measured 2026-10-06."""
    return GoogleError(
        403,
        "The requesting user does not have the administrator privilege required to list or manage "
        "all shared drives.",
        reason="noListTeamDrivesAdministratorPrivilege",
    )


def abuse_acknowledgment_not_applicable() -> GoogleError:
    """`files.get` acknowledged abuse on a read that downloads nothing. Measured 2026-10-04."""
    return GoogleError(
        403,
        "The acknowledgeAbuse parameter is only applicable for download requests.",
        reason="invalidAbuseAcknowledgment",
        location="acknowledgeAbuse",
    )


def download_redirect(location: str) -> GoogleError:
    """A Drive download inside a batch, redirected rather than answered -- see
    ``routers.google._drive_batch_redirect``. Measured 2026-10-04: a 302 carrying ``Location`` and
    this error body."""
    exc = GoogleError(302, "Unknown Error.", reason="backendError", status="UNKNOWN")
    exc.headers = {"Location": location}
    return exc


def unimplemented() -> GoogleError:
    """A Sheets read inside a batch. Measured 2026-10-04, its `errors[]` entry, shown at
    `$.xgafv=1`, is ``notImplemented`` under ``global``."""
    return GoogleError(
        501,
        "Operation is not implemented, or supported, or enabled.",
        reason="notImplemented",
        status="UNIMPLEMENTED",
    )


def invalid_argument(message: str) -> GoogleError:
    """The editor APIs' generic 400. Its `errors[]` entry, shown at `$.xgafv=1`, is ``badRequest``
    under ``global`` — measured on an unparseable range, a range past the grid, an unsupported
    ``alt``, ``dataFilter.filter must be specified.``, ``No sheet with id``,
    ``Must specify at least one dataFilter.``, and on 2026-10-04 ``No grid with id`` and
    ``GridRange indexes must be >= 0``. A typed value the proto layer refuses is a different entry
    (:func:`invalid_field_value`), and so is :func:`invalid_json`'s."""
    return GoogleError(400, message, reason="badRequest", status="INVALID_ARGUMENT")


def invalid_field_value(field: str, message: str) -> GoogleError:
    """The proto layer's refusal of a typed value — ``Invalid value at '<field>' (<type>),
    "<value>"`` for an enum, a bool or an int32. Measured at `$.xgafv=1`, its `errors[]` entry is
    ``reason: invalid`` and carries no ``domain``."""
    return invalid_field_values([(field, message)])


def invalid_field_values(violations: list[tuple[str | None, str]]) -> GoogleError:
    """One refusal for every typed value the proto layer could not read, as ``(field, message)``
    pairs in the order to report them. A refusal at the root of a JSON body (an unknown top-level
    name, a body that is not an object) has no field, and its violation carries none, measured
    2026-10-04.

    Measured 2026-09-23 on Sheets `values.get`, `spreadsheets.get` and `:getByDataFilter` and on
    Drive `files.list`, over query parameters and a JSON body alike: each refused value is a
    ``google.rpc.BadRequest`` field violation in `details`, naming the field the way its message
    does and repeating the message as its description, and a request with several is one 400 whose
    message joins theirs with newlines, in the order `details` lists them. Which order that is,
    is the caller's: ``routers.google._typed_query``'s for a query string,
    :func:`backlot.protojson.read`'s for a body."""
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
                    {"field": field, "description": message} if field else {"description": message}
                    for field, message in violations
                ],
            }
        ],
    )


def field_violations(exc: GoogleError) -> list[tuple[str | None, str]]:
    """The ``(field, message)`` pairs an :func:`invalid_field_values` refusal carries, so a caller
    reading several values can gather every refusal into one."""
    return [
        (v.get("field"), v["description"]) for d in exc.details or () for v in d["fieldViolations"]
    ]


def invalid_json(message: str) -> GoogleError:
    """A request body that is not JSON (``backlot.protojson.read``). Measured 2026-10-04 on the two
    Sheets data-filter POSTs: ``Invalid JSON payload received.`` and a sentence, no `details`, and
    at `$.xgafv=1` an `errors[]` entry of ``parseError`` under ``global``."""
    return GoogleError(
        400,
        f"Invalid JSON payload received. {message}",
        reason="parseError",
        status="INVALID_ARGUMENT",
    )


def internal_error() -> GoogleError:
    """Real's 500 on the Sheets data-filter reads, measured 2026-10-04: an enum number the proto
    does not declare in some fields, and a `ROW`, `COLUMN` or `SHEET` lookup beside a `spreadsheet`
    location. ``routers.google_sheets._sheets_check_lookup`` and
    ``sheets_values_batch_get_by_data_filter`` list which."""
    return GoogleError(500, "Internal error encountered.", reason="backendError", status="INTERNAL")


def drive_internal_error() -> GoogleError:
    """Drive's 500 `Internal Error`, which `files.list` answers to an ``orderBy`` naming ``starred``
    second or third, after each of the other ten documented keys and in either direction, while
    ``starred`` first is served. Measured against Drive v3 on 2026-10-04 and 2026-10-07."""
    return GoogleError(500, "Internal Error", reason="internalError")


def unknown_error() -> GoogleError:
    """Drive's 500 `Unknown Error.`, whose `errors[]` entry has no members, for two or more
    `pageSize` values on `permissions.list` whose first is outside 1-100;
    ``routers.google._drive_permissions_page_size`` records the measurement. Real answers the same
    body to a `permissions.list` `pageToken` of `BOGUS`, `0`, a `files.list` token or an issued
    token with `.` added, measured 2026-10-09, which
    ``routers.google._drive_permissions_page_token`` refuses with its 400 instead."""
    return GoogleError(500, "Unknown Error.", status="UNKNOWN", empty_entry=True)


def unsupported_conversion() -> GoogleError:
    """`files.export` asked for a format the file's type does not export to. Measured 2026-09-23
    on a spreadsheet: `text/plain`, `bogus/type`, a native Google type, a padded `text/csv `,
    `text/csv;charset=utf-8` and an empty value each answer this, ``badRequest`` at
    ``location: convertTo``."""
    return GoogleError(
        400, "The requested conversion is not supported.", reason="badRequest", location="convertTo"
    )


def bad_field_mask(mask: str, path: str) -> GoogleError:
    """A ``fields`` mask naming something the response has no field for.

    Measured on Sheets: the top-level message is the generic ``Request contains an invalid
    argument.`` and the path that failed is named only inside a ``google.rpc.BadRequest`` detail —
    so a client that wants to know WHICH path was wrong has to read `details`. The violation's
    `field` is the whole mask, each ``/`` written ``.``: measured 2026-10-22, `spreadsheetId,bogus`
    named `spreadsheetId,bogus` there and `sheets(properties/bogus)` named
    `sheets(properties.bogus)`, the description naming the path alone."""
    return _field_mask_refused(
        mask, f"Error expanding 'fields' parameter. Cannot find matching fields for path '{path}'."
    )


def unbalanced_field_mask(mask: str, unpaired: str) -> GoogleError:
    """:func:`bad_field_mask`'s 400 for a mask that leaves ``unpaired``, ``(`` or ``)``, without its
    pair, the description naming the whole mask: measured 2026-10-22 on Sheets, `sheets(` answered
    `Invalid FieldMask 'sheets('. Cannot find matching ')' for all '('.`, `spreadsheetId)`
    `Cannot find matching '(' for all ')'.`, and `sheets(properties/title` wrote the mask
    `sheets(properties.title` there."""
    pair = ")" if unpaired == "(" else "("
    return _field_mask_refused(
        mask,
        f"Invalid FieldMask '{mask.replace('/', '.')}'. "
        f"Cannot find matching '{pair}' for all '{unpaired}'.",
    )


def _field_mask_refused(mask: str, description: str) -> GoogleError:
    return GoogleError(
        400,
        "Request contains an invalid argument.",
        reason="badRequest",
        status="INVALID_ARGUMENT",
        details=[
            {
                "@type": "type.googleapis.com/google.rpc.BadRequest",
                "fieldViolations": [{"field": mask.replace("/", "."), "description": description}],
            }
        ],
    )


def invalid_page_token() -> GoogleError:
    """Gmail's answer to a page token that does not parse, on both listings: 400 INVALID_ARGUMENT,
    whose `errors[]` entry is `global` / `invalidArgument` and which carries no `details`. Real
    does not check that it issued the token: on 2026-10-09 both listings served `0`, `1` and an
    issued token with its last digit changed or dropped, and refused one with a 21st digit, a
    letter or a leading space. Where it comes among the other refusals is
    ``routers.google._gmail_page``'s."""
    return GoogleError(
        400, "Invalid pageToken", reason="invalidArgument", status="INVALID_ARGUMENT"
    )


def invalid_attachment_token() -> GoogleError:
    """Gmail's answer to an attachment id it does not hold. Measured 2026-09-30 and 2026-10-01:
    400 INVALID_ARGUMENT for a made-up id and for a real one with characters changed, whatever
    message id the path names: one that exists, one that does not, and a non-hex one alike."""
    return GoogleError(
        400, "Invalid attachment token", reason="invalidArgument", status="INVALID_ARGUMENT"
    )


def invalid_id_value() -> GoogleError:
    """Gmail's answer to an id it cannot parse — measured: 400 INVALID_ARGUMENT "Invalid id value"
    for a non-hex id or one at/above 2**63, where a well-formed but unknown id is 404 instead."""
    return GoogleError(400, "Invalid id value", reason="invalidArgument", status="INVALID_ARGUMENT")


def invalid_max_results() -> GoogleError:
    """Gmail's ``Invalid maxResults`` for a `maxResults` of zero or from 2**31 up: 400
    INVALID_ARGUMENT, whose `errors[]` entry is `global` / `invalidArgument` and which carries no
    `details`. The values and the measurement are ``routers.google._gmail_page``'s. A value
    the proto layer cannot read at all is :func:`invalid_field_value`'s shape instead, which names
    the field it refused."""
    return GoogleError(
        400, "Invalid maxResults", reason="invalidArgument", status="INVALID_ARGUMENT"
    )


def failed_precondition(message: str) -> GoogleError:
    """The editor APIs' "right shape, wrong state" 400 — an Office file read as a native doc."""
    return GoogleError(400, message, reason="failedPrecondition", status="FAILED_PRECONDITION")


# The challenge the front end's 401s carry, measured 2026-10-11 on all five hosts, with a credential
# that does not resolve adding `error="invalid_token"` to it (:func:`bad_token`). It rides along on
# a 401 a `callback` wraps at 200 as well. The download server's own 401 names the realm without
# its trailing slash (:func:`download_credential_refused`).
_CHALLENGE = 'Bearer realm="https://accounts.google.com/"'


def bad_token(*, named: bool = True) -> GoogleError:
    """A credential that does not resolve: 401 in every family. ``named`` is whether it named a
    token at all, which the challenge says: measured 2026-10-11, `Bearer nope`, `OAuth nope`,
    `Bearer x <token>` and `Bearer <token>;x` carry `error="invalid_token"` in `WWW-Authenticate`,
    where `Bearer<token>` and `Bearer<TAB><token>`, a scheme with no space after it, carry the bare
    challenge."""
    challenge = f'{_CHALLENGE}, error="invalid_token"' if named else _CHALLENGE
    return GoogleError(
        401,
        BAD_TOKEN_MESSAGE,
        reason="authError",
        location="Authorization",
        location_type="header",
        status="UNAUTHENTICATED",
        short="Invalid Credentials",
        headers={"WWW-Authenticate": challenge},
    )


def missing_credentials(rpc: tuple[str, str] | None = None) -> GoogleError:
    """No credential, on an operation that does not take an API key (:func:`no_credentials`).

    One answer for all of them: measured 2026-09-14, Gmail, Docs and Slides send the same long
    top-level message and the same `errors[]` entry, the short ``Login Required.`` at ``location:
    Authorization``, and measured 2026-09-22 the two Sheets data-filter POSTs send both too. Which
    of them SHOWS that entry still differs — Gmail carries it unless `$.xgafv=2`, the editor
    families only at `1` — but that is `has_errors_array`'s rule, not a difference in the error.

    ``rpc`` is ``(method, service)``, the operation the answer names in a ``CREDENTIALS_MISSING``
    ErrorInfo: measured 2026-10-26, real carries one on every such 401, Gmail's and Docs' included,
    and it is given here for the operations in `_RPC_METHODS`."""
    details = None
    if rpc is not None:
        details = [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "CREDENTIALS_MISSING",
                "domain": "googleapis.com",
                "metadata": {"method": rpc[0], "service": rpc[1]},
            }
        ]
    return GoogleError(
        401,
        MISSING_CREDENTIALS_MESSAGE,
        reason="required",
        location="Authorization",
        location_type="header",
        status="UNAUTHENTICATED",
        short="Login Required.",
        details=details,
        headers={"WWW-Authenticate": _CHALLENGE},
    )


def unregistered_caller() -> GoogleError:
    """No credential where the operation also takes an API key (:func:`no_credentials`). The caller
    has no established identity, rather than missing a credential, and the answer carries no
    `WWW-Authenticate`."""
    return GoogleError(
        403, UNREGISTERED_CALLER_MESSAGE, reason="forbidden", status="PERMISSION_DENIED"
    )


# The operations whose caller with no credential is an unregistered one rather than one missing a
# credential, as (method, path pattern). Measured 2026-10-11 with no `Authorization` on every
# operation served here and on each of the 147 the `gmail` and `google_drive` baselines list as
# `missing_operation`, where `Bearer nope` beside each was the 401 that shows the path is an
# operation: these answered the 403, and every other one the 401. A file id of `generateIds` or
# `generateCseToken` is its own operation, the 401 one; `trash` is the 403 of a file read.
_UNREGISTERED_CALLER_OPERATIONS = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("GET", r"/drive/v3/files"),
        ("GET", r"/drive/v3/files/(?!generateIds$|generateCseToken$)[^/]+"),
        ("GET", r"/drive/v3/files/[^/]+/export"),
        ("GET", r"/drive/v3/files/[^/]+/comments(/[^/]+(/replies(/[^/]+)?)?)?"),
        ("POST", r"/drive/v3/files/[^/]+/comments(/[^/]+/replies)?"),
        ("PATCH", r"/drive/v3/files/[^/]+/comments/[^/]+(/replies/[^/]+)?"),
        ("DELETE", r"/drive/v3/files/[^/]+/comments/[^/]+(/replies/[^/]+)?"),
        ("POST", r"/drive/v3/files/[^/]+/(download|watch)"),
        ("GET", r"/drive/v3/apps"),
        ("GET", r"/drive/v3/operations/[^/]+"),
        ("GET", r"/sheets/v4/spreadsheets/[^/]+"),
        ("GET", r"/sheets/v4/spreadsheets/[^/]+/values/.+"),
        ("GET", r"/sheets/v4/spreadsheets/[^/]+/values:batchGet"),
    )
)


# The RPC each operation's missing-credential 401 names, by its HTTP method and path, measured
# 2026-10-26 on `sheets.googleapis.com`, and on 2026-10-27 for the last three, which are not served
# and answer this 401 all the same.
_SHEETS_RPC = "google.apps.sheets.v4.SpreadsheetsService"
_RPC_METHODS = tuple(
    (method, re.compile(pattern), (f"{_SHEETS_RPC}.{name}", "sheets.googleapis.com"))
    for method, pattern, name in (
        ("POST", r"/sheets/v4/spreadsheets", "CreateSpreadsheet"),
        (
            "POST",
            r"/sheets/v4/spreadsheets/[^/]+/sheets/[^/]+:copyTo",
            "CopySheetToAnotherSpreadsheet",
        ),
        (
            "POST",
            r"/sheets/v4/spreadsheets/[^/]+/values:batchUpdateByDataFilter",
            "BatchUpdateValuesByDataFilter",
        ),
        (
            "POST",
            r"/sheets/v4/spreadsheets/[^/]+/values:batchClearByDataFilter",
            "BatchClearValuesByDataFilter",
        ),
        ("POST", r"/sheets/v4/spreadsheets/[^/]+/values/.+:clear", "ClearValues"),
        ("POST", r"/sheets/v4/spreadsheets/[^/]+/values/.+:append", "AppendValues"),
        ("PUT", r"/sheets/v4/spreadsheets/[^/]+/values/.+", "UpdateValues"),
        ("POST", r"/sheets/v4/spreadsheets/[^/]+/values:batchUpdate", "BatchUpdateValues"),
        ("POST", r"/sheets/v4/spreadsheets/[^/]+/values:batchClear", "BatchClearValues"),
        (
            "POST",
            r"/sheets/v4/spreadsheets/[^/]+/values:batchGetByDataFilter",
            "BatchGetValuesByDataFilter",
        ),
        ("POST", r"/sheets/v4/spreadsheets/[^/]+:getByDataFilter", "GetSpreadsheetByDataFilter"),
        ("POST", r"/sheets/v4/spreadsheets/[^/]+:batchUpdate", "BatchUpdateSpreadsheet"),
        (
            "POST",
            r"/sheets/v4/spreadsheets/[^/]+/developerMetadata:search",
            "SearchDeveloperMetadata",
        ),
        (
            "GET",
            r"/sheets/v4/spreadsheets/[^/]+/developerMetadata/[^/]+",
            "GetDeveloperMetadata",
        ),
    )
)


def no_credentials(path: str, method: str) -> GoogleError:
    """What a request with no credential gets on this operation: :func:`unregistered_caller` on the
    ones in `_UNREGISTERED_CALLER_OPERATIONS`, :func:`missing_credentials` on the rest, Gmail, Docs
    and Slides whole among them.

    A byte-stream read with no credential has two answers of its own, :func:`missing_api_key` with
    no `Authorization` and :func:`download_credential_refused` with one that is no credential, which
    the route asks for by name.
    """
    if any(m == method and p.fullmatch(path) for m, p in _UNREGISTERED_CALLER_OPERATIONS):
        return unregistered_caller()
    rpc = next((r for m, p, r in _RPC_METHODS if m == method and p.fullmatch(path)), None)
    return missing_credentials(rpc)


def delegation_denied(email: str) -> GoogleError:
    """Gmail's answer to a user's token asking for another address's mailbox, naming the caller's
    own address. Measured 2026-10-11 with a Workspace member's `gmail.readonly` token, on every
    Gmail route served here, for an address in the member's domain and one outside it. Which
    `userId` is the caller's own, and which refusals come before it, is
    ``routers.google._refuse_another_mailbox``'s."""
    return GoogleError(
        403, f"Delegation denied for {email}", reason="forbidden", status="PERMISSION_DENIED"
    )


def invalid_user_id() -> GoogleError:
    """:func:`delegation_denied` for a `userId` that is not an address at all: `bob`, `@`, a local
    part with nothing after its `@` and a domain with nothing before it each answered this sentence
    on 2026-10-11, where `a%40b.com` was an address. Which `userId` gets it here is
    ``routers.google._refuse_another_mailbox``'s."""
    return GoogleError(
        403,
        "Invalid user id specified in request/Delegation denied",
        reason="forbidden",
        status="PERMISSION_DENIED",
    )


def refuse_download(request: Request) -> None:
    """The two refusals a byte-stream read makes before it reads its parameters: a token named in
    the query (:func:`token_in_query`), then a `callback` that cannot be called, which is real's 503
    `Backend Error`.

    A `callback` on a download is not JSONP. Measured 2026-10-07 on `files.get?alt=media` and
    `files.export`, against a PDF and a Doc: a name a script can call is ignored when the download
    succeeds (200, the bytes) and turns EVERY other answer into the same 503, while a name that
    cannot be called is that 503 even where the download would have succeeded. So this raises the
    503 for the uncallable name and leaves ``google_download_live`` on the request for the renderer,
    which turns whatever else the request raises into the same 503 (:func:`rendered`).

    Whether a request is a download is `routers.google._drive_download`'s question, read off the
    route, and the route calls this once it is true: an export asking for `alt=json` and a file
    whose id is literally `export` are ordinary reads to real (measured 2026-10-07), so neither
    reaches here. A credential that does not resolve (``routers.google._credential``) answers its
    own 401 ahead of this, which is where real puts it.
    """
    if names_a_query_token(request.query_params):
        raise token_in_query()
    callback = first_repeat(request.query_params, CALLBACK) or ""
    if not callback:
        return
    request.state.google_download_live = True
    if not callable_jsonp_name(callback):
        raise backend_error()


def names_a_query_token(query: Mapping[str, str]) -> bool:
    """Whether ``query`` names one of the `QUERY_TOKENS` in any case, which is what
    :func:`token_in_query` refuses."""
    return any(name.lower() in QUERY_TOKENS for name in query)


def token_in_query() -> GoogleError:
    """A byte-stream read whose query names an OAuth token: 400 under `badRequest`, beside a header
    credential that resolves as beside none, and whatever the value but one the query is read as a
    token that does not resolve, which answers that token's 401 first (the route's credential check
    runs ahead of this). The names are :func:`names_a_query_token`'s; a name that only resembles one
    is left to the download's other refusals.

    Measured 2026-10-13 on `files.export`, and on `files.get?alt=media` with an empty value and
    inside a batch; ``tests/test_google.py::_DOWNLOAD_QUERY_TOKEN_ROWS`` holds the cases.
    """
    return invalid_argument(QUERY_TOKEN_MESSAGE)


def download_credential_refused() -> GoogleError:
    """A byte-stream read whose `Authorization` real's front end does not read as a credential
    (``routers.google._credential``), refused by the server that holds the bytes: 401 with the short
    ``Invalid Credentials`` as its message, no ``status``, and a challenge naming the realm without
    its trailing slash.

    Measured 2026-10-11 on `files.get?alt=media` and `files.export`, each sent on its own:
    `bearer nope`, `Basic YWJjOmRlZg==`, a bare `Bearer` and `nope` each answered this, and on
    2026-10-13 an `Authorization` whose value is empty did too, where no header at all is the
    missing API key's 403 and `Bearer nope` the front end's own 401. Its place among the download's
    refusals is :func:`missing_api_key`'s to say."""
    return GoogleError(
        401,
        "Invalid Credentials",
        reason="authError",
        location="Authorization",
        location_type="header",
        headers={"WWW-Authenticate": 'Bearer realm="https://accounts.google.com"'},
    )


def missing_api_key() -> GoogleError:
    """No `Authorization` on a byte-stream read. Measured 2026-10-04 on `files.export` and
    2026-10-05 on `files.get?alt=media`, on an existing file and on a missing id alike: 403 with
    this sentence and `reason: forbidden`, and NO ``status`` -- where the same route without the
    download answers :func:`unregistered_caller`'s sentence with `PERMISSION_DENIED`.

    Real answers it last of the refusals a download makes, which is why the route raises it after
    the download's parameters rather than at the top. Measured 2026-10-07, the order a byte-stream
    read answers in is: `$.xgafv` 400, a credential that does not resolve 401, a token named in the
    query 400 (:func:`token_in_query`, 2026-10-13), `callback` 503, a mistyped parameter or an
    absent `mimeType` 400, this 403 (or, for a header that is no credential,
    :func:`download_credential_refused`), then the lookup."""
    return GoogleError(403, MISSING_API_KEY_MESSAGE, reason="forbidden")


# Real's 503 body for a download carrying a `callback`, to the byte, measured 2026-10-07 over every
# error a download answered beside one: the `errors[]` entry is written INLINE, and the body ends
# with the closing brace and no trailing newline. `respond`'s serializer writes the array expanded
# and ends with a newline, so this one body is a literal rather than a rendering of `backend_error`.
DOWNLOAD_BACKEND_ERROR_BODY = (
    "{\n"
    '  "error": {\n'
    '    "code": 503,\n'
    '    "message": "Backend Error",\n'
    '    "errors": [{\n'
    '      "message": "Backend Error",\n'
    '      "domain": "global",\n'
    '      "reason": "backendError"\n'
    "    }]\n"
    "  }\n"
    "}"
)


def backend_error() -> GoogleError:
    """A `callback` that cannot be called, on a byte-stream read where JSONP does not apply: 503
    `Backend Error`, in the script's content type, unwrapped.

    Measured 2026-10-04 on `files.export` and 2026-10-05 on `files.get?alt=media` for the uncallable
    name, and 2026-10-07 that every other error on a download carrying a `callback` -- a missing id,
    a Docs file read with `alt=media`, a mistyped `supportsAllDrives`, an absent `mimeType`, no
    credential -- becomes this same 503. The body is :data:`DOWNLOAD_BACKEND_ERROR_BODY`, which
    :func:`backend_error_response` writes literally; an ordinary read beside an uncallable name is
    the wrapped 400 at 200 instead (:func:`bad_jsonp_callback`).
    """
    return GoogleError(503, "Backend Error", reason="backendError")


def backend_error_response(headers: Mapping[str, str] | None = None) -> Response:
    """The whole answer a download carrying a `callback` gets when it fails: 503,
    `text/javascript; charset=UTF-8`, and :data:`DOWNLOAD_BACKEND_ERROR_BODY` byte for byte."""
    return Response(
        DOWNLOAD_BACKEND_ERROR_BODY,
        status_code=503,
        media_type="text/javascript; charset=UTF-8",
        headers=headers,
    )


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
    that way. The pair that measured it, and the end every other measured parameter is read from,
    are :func:`first_repeat`'s table."""
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
                          | `&bad` is what an empty token gets,  | Drive permissions.list and
                          | `bad&` a 400                         | drives.list 2026-10-05
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
        maxResults        | `1&3` is 3, `3&1` is 1               | Gmail messages.list and
                          |                                      | threads.list 2026-10-08
        pageToken         | `bad&<valid>` is the next page,      | Gmail messages.list and
                          | `<valid>&bad` a 400                  | threads.list 2026-10-09

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

    Gmail's `q` stays on ``.get`` because which end real reads it from is unmeasured.
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


def callable_jsonp_name(name: str) -> bool:
    """Whether `name` is a callback a script can call, which is the regex :func:`bad_jsonp_callback`
    refuses: ASCII letters and digits plus ``_``, ``$``, ``.``, ``[`` and ``]``, at any position.

    Public because :func:`validate_system_parameters` and :func:`refuse_download` ask the same
    question of the same name, and answer it differently: the wrapped 400 of
    :func:`bad_jsonp_callback` on an ordinary read, :func:`backend_error`'s 503 on a download.
    """
    return _CALLBACK_NAME.fullmatch(name) is not None


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
    looked at -- where the same POST honours `$.xgafv` and `prettyPrint`. So GET is the only
    method this parameter applies to. A `HEAD` (``request.state.answering_head``) is not GET here
    either: measured 2026-10-11 on Drive's `files.list` and Gmail's `profile`, a callable name and
    an uncallable one each answered a `HEAD` with 200 `application/json; charset=UTF-8`, where the
    GET beside it was the script, and a `HEAD` that failed beside one kept its own status — no
    credential's and a bad one's, and a missing file's 404 — where its GET was the script at 200.

    Two values that look like a callback are not one either. An empty `callback=` is absent:
    measured, it answers the plain body at the real status, success and error alike. So is any
    `alt` NAMING A FORMAT other than `json` -- which spellings do name that format is
    :func:`alt_format`'s half, and `JSON` and an empty `alt=` are among them. Measured on Sheets,
    `alt=media`, `alt=proto` and `alt=zzz` each answer their own 400 unwrapped, even when
    `callback` is itself unparseable, so an `alt` whose format the API cannot render takes the
    request out of the JSONP path along with the JSON one.
    That suppression is not Sheets' own: measured 2026-09-16, `alt=media` and `alt=zzz` beside a
    `callback` answer unwrapped on Drive, Gmail, Docs and Slides too, which is why `alt` is read
    for every family here rather than only where ``routers.google_sheets._sheets_respond`` refuses
    the value. Refusing it is still Sheets-only, and the four families that accept a format they
    cannot render where real answers a 400 are a gap of their own.

    A Drive byte-stream read is out of the JSONP path for a third reason, and one this function is
    not asked to decide: a `callback` on a download is neither refused nor wrapped by the JSONP
    path but is real's 503 on any failure (:func:`refuse_download`). Which requests those are is
    ``routers.google._drive_download``'s question, read off the route, and the dependency that
    would call this for one passes ``callback=False`` instead.

    Which `alt` counts as JSON is :func:`alt_format`'s question, not this one's -- `alt=JSON` and
    `alt=` are the JSON the default spells, and answering them unwrapped is the divergence that
    reading the value literally here used to produce.

    Both are read as :func:`first_repeat`, not off ``QueryParams.get``: real answers a repeated
    `callback` through the first name and a repeated `alt` through the first format.
    """
    if request.method != "GET" or getattr(request.state, "answering_head", False):
        return None
    query = request.query_params
    alt = alt_format(query)
    if alt and alt != "json":
        return None
    return first_repeat(query, CALLBACK) or None


def validate_system_parameters(request: Request, *, callback: bool = True) -> None:
    """Refuse a `$.xgafv` other than `1` or `2`, or a `callback` that cannot be a JavaScript name,
    on a Google-family path, before the route runs.

    A router-level dependency, so it is the first thing a request meets: measured, real answers the
    `$.xgafv` 400 ahead of a bad token, a missing credential and an unparseable range, and the
    `callback` 400 ahead of the same three and of a mistyped `fields` mask. `$.xgafv` goes first
    because it beats `callback` too -- measured, `callback=a b&$.xgafv=9` answers the `$.xgafv`
    sentence, wrapped through the very name the other check would have refused. The batch endpoint
    is not a family path and is left alone.

    ``callback=False`` leaves `callback` alone: it is not checked, and no refusal, the `$.xgafv` one
    included, is wrapped through it. The caller decides when, since which requests real exempts is a
    question about the route -- a POST, and a Drive download, which answers an uncallable name with
    :func:`backend_error`'s 503 rather than this 400 (see ``routers.google._system_parameters``).
    """
    if family(request.url.path) is None:
        return
    # Whether `callback` was checked is what :func:`rendered` needs to know, and only this call can
    # say so: a ROUTER dependency runs once a route has matched, so an unrouted family path reaches
    # the renderer with a `callback` nothing has looked at.
    request.state.google_system_parameters_checked = callback
    value = xgafv(request.query_params)
    if value is not None and value not in XGAFV_VALUES:
        raise bad_system_parameter(XGAFV, value)
    if not callback:
        return
    name = jsonp_callback(request)
    if name is not None and not callable_jsonp_name(name):
        raise bad_jsonp_callback(name)


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
        err["errors"] = [{} if getattr(exc, "empty_entry", False) else entry]
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

    This is NOT the path a Drive download carrying a `callback` is rendered through: that answer is
    always real's 503 in the script's type with no call around it, and it is
    :func:`backend_error_response` that writes it (see :func:`rendered`).
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
    parameter reaches the success path only (``routers.google_sheets._sheets_respond``) and nothing
    here reads it.

    Wrapped only where ``validate_system_parameters`` checked `callback`, which is where a route
    matched and did not exempt it. That is a ROUTER dependency, so a family path with NO route --
    `/sheets/v4/nope` -- reaches this having been refused nothing, and `callback=a b` there would be
    answered by calling `a b`. Real answers such a path from its front end as HTML, measured
    2026-09-16 with a `callback` and without: 400 on Sheets, Docs and Slides, 404 on Drive and
    Gmail. So JSONP is not its shape there under any name, and the plain body is the nearer of the
    two answers Backlot can give.

    Where the check DID run the name needs no second look, and the body being wrapped may BE its
    refusal -- that is real's own answer, measured the same day: `callback=evil);alert(1);//` on a
    Sheets read comes back 200 calling that very name, escaped the way :func:`_escaped_name`
    escapes one.

    A request :func:`refuse_download` marked ``google_download_live`` is answered here instead, and
    whatever it raised becomes real's 503 -- measured 2026-10-07, EVERY error a Drive download
    answers beside a `callback` is the same 503 `Backend Error`, in the script's type and
    unwrapped, including a missing id's 404 and a mistyped parameter's 400. The marker is set once
    the route has passed real's two earlier refusals, so a `$.xgafv` 400 and a bad `Bearer` 401,
    which the marker is not set for, keep their own shape.

    It takes the whole request because :func:`jsonp_callback` reads the method as well as the query.
    """
    if getattr(request.state, "google_download_live", False):
        return backend_error_response(headers)
    checked = getattr(request.state, "google_system_parameters_checked", False)
    callback = jsonp_callback(request) if checked else None
    return respond(body, callback=callback, status_code=status_code, headers=headers)


# --- a `HEAD` -----------------------------------------------------------------------------------


def refuses_head(path: str) -> bool:
    """Whether a `HEAD` on ``path`` is :func:`head_refusal`: on any path of the editor family,
    routed or not, where Drive and Gmail answer one as the GET
    (``backlot.main._HEAD_IS_THE_GET_WITHOUT_ITS_BODY``).

    A prefix test with no boundary, as :func:`family`'s is, because the host refuses the method past
    the version segment as well: `HEAD /v4x` and `/v4x/spreadsheets/abc` on Sheets, `/v1x` on Docs
    and `/v1x/presentations/abc` on Slides each answered the 400 too, measured 2026-10-11."""
    return family(path) == EDITOR


def head_refusal() -> Response:
    """What the Docs, Sheets and Slides hosts answer a `HEAD` with: 400, `text/html; charset=utf-8`,
    `content-length: 0` and no body, ahead of the credential and of the system parameter alike.
    Measured 2026-10-11 on routed paths, an unrouted one and the root, beside an unknown `$.xgafv`
    and with a trailing slash, with a Workspace member's token, a bad one and none.

    Sheets answers some with its host's HTML 404 page instead, and not by class of credential. On
    2026-10-13 one Workspace spreadsheet's id got the 404 on 32 of 32, whoever asked; across 20
    other Sheets paths two gcloud-issued tokens (that member's and a consumer account's) got the 400
    on 160 of 160, while a bad token and no credential got the 404 on 28 of 160; that member's
    token from the OAuth 2.0 Playground got the 400 on 35 of 35 across seven of those paths, and
    that member's gcloud token got the 400 on 18 of 18 across six other spreadsheets the member can
    open. Nor does Sheets go by whether the caller is a domain administrator: that member is not
    one, like every caller modelled here, and another Workspace member who is not one either got the
    404 on each made-up Sheets path it tried, with `drive`, `gmail.readonly` or both, but the 400 on
    a spreadsheet that exists, while that domain's administrator got the 400 on both through the
    same OAuth client. What picks the page was not found, so every caller gets the 400 here."""
    return Response(status_code=400, media_type="text/html")


def head_content_length(request: Request, status_code: int) -> bool:
    """True only where the GET a `HEAD` runs as is a Drive download's 200.

    Measured 2026-10-11 under `Accept-Encoding: identity` on every Drive and Gmail route served
    here, each `HEAD` beside its GET: the `HEAD` answered the GET's status and `content-type` with
    no body — the 200s; a missing file's, label's and message's 404; an unparseable id's, a bad page
    size's and an unknown `$.xgafv`'s 400; the 401s and 403s a missing or bad credential gets; and
    on a download the missing API key's 403 and an uncallable name's 503. Every answer but a
    download's was chunked, with no `content-length` on either method. A download's 200 declares one
    on the `HEAD` as on the GET — `files.export` to `text/plain` and to `text/html` and
    `files.get?alt=media` on a PDF, 16, 798 and 587 bytes — while a download that fails declares a
    length on its GET (`File not found`, an unsupported `mimeType`, an absent one, a Docs file read
    with `alt=media`, no credential, the 503) and none on its `HEAD`.

    ``request.state.google_download`` is set by ``routers.google._system_parameters``."""
    return status_code == 200 and getattr(request.state, "google_download", False)
