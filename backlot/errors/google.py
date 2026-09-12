"""Google's error envelope, per API family.

`google-api-python-client` reads ``error.message`` to build its ``HttpError``, and real clients
branch on ``error.status`` or ``errors[].reason``. FastAPI's default ``{"detail": …}`` gives them
none of that, so error handling could not be developed or tested against Backlot even though its
status codes were already right.

Everything here was measured against the live Docs / Drive / Gmail / Sheets / Slides APIs. The
envelope is NOT uniform — three families differ in which optional members they carry:

    family                        errors[]         status                no Authorization header
    ------------------------------|---------------|----------------------|------------------------
    Drive v3                      | always        | auth failures only   | 403 PERMISSION_DENIED
    Gmail v1                      | always        | always               | 401 UNAUTHENTICATED
    Docs v1 / Slides v1           | $.xgafv=1     | always               | 401 UNAUTHENTICATED
    Sheets v4                     | $.xgafv=1     | always               | 403 PERMISSION_DENIED

Sheets parts from the other two editor APIs on that last column: measured, a request with no
Authorization header is 403 PERMISSION_DENIED with the unregistered-caller sentence, where Docs
answers 401 UNAUTHENTICATED with the missing-credential one. A present-but-invalid token is 401
UNAUTHENTICATED in every family, which is why a missing header and a bad token are separate
constructors here rather than one "unauthorized".

`errors[]` is what `$.xgafv` selects for the editor families — `1` adds it, `2` and an absent
parameter leave it off, and the LAST value wins when it is sent twice (measured: `$.xgafv=2&$.xgafv=1`
carries the array, `1&2` does not). A success body is the same either way. `$.xgafv` is a SYSTEM
parameter — a top-level entry of a discovery document's ``parameters``, which every method takes
(read on the Sheets and Drive documents; #172 found it on the rest) — so it is validated once for
the whole router (:func:`validate_system_parameters`) and declared once for the whole document
(:func:`backlot.openapi.google_system_parameters`) rather than route by route. A value other than
`1` or `2` is refused before anything else is read — measured, ahead of a bad token, a missing
credential and an unparseable range alike — with the sentence :func:`bad_system_parameter` carries.

Inside `errors[]` the entry is not uniform, and the difference is which constructor raised it.
Measured on Sheets and Docs at `$.xgafv=1`: a typed value the proto layer refuses — the enum, bool
and int32 "Invalid value at '<field>' (<type>), \"<value>\"" messages — reports ``reason: invalid``
and NO ``domain``; every other 400 measured — an unparseable range, a range past the grid, a bad
``fields`` mask, an unsupported or unknown ``alt``, ``dataFilter.filter must be specified.``, ``Must
specify at least one dataFilter.``, ``No sheet with id``, a non-JSON body — reports ``reason:
badRequest`` with ``domain: global``, and the editor 400s not measured (an Office file read as a
native document, ``Invalid gridRange``) take that entry too; a 404 is ``notFound``, a bad token ``authError`` with ``location:
Authorization``, an anonymous Sheets request ``forbidden``, and an anonymous Docs request
``required`` with the short ``Login Required.`` and the same location. Drive and Gmail carry the
array whatever `$.xgafv` says, with the shapes the tests beside them already pin.
"""

from __future__ import annotations

from typing import Mapping

from fastapi import HTTPException, Request

# Whether a family carries the legacy `errors[]` array whatever `$.xgafv` says; the editor families
# carry it at `$.xgafv=1` only. `status` needs no per-family flag: Drive's
# parameter failures simply do not have one, while every Gmail and editor error does, so "the error
# carries a status" is the whole condition.
DRIVE, GMAIL, EDITOR = "drive", "gmail", "editor"
_FAMILY_HAS_ERRORS = {DRIVE: True, GMAIL: True, EDITOR: False}
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
    canonical code name. ``short`` is a distinct ``errors[0].message`` — only the bad-token 401 uses
    one, where Google's top-level message is the long form and ``errors[0]`` says "Invalid
    Credentials"."""

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


def invalid_field_value(message: str) -> GoogleError:
    """The proto layer's refusal of a typed value — ``Invalid value at '<field>' (<type>),
    "<value>"`` for an enum, a bool or an int32. Measured at `$.xgafv=1`, its `errors[]` entry is
    ``reason: invalid`` and carries no ``domain``, which no other Google error measured does."""
    return GoogleError(400, message, reason="invalid", status="INVALID_ARGUMENT", domain=None)


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


def missing_credentials(path: str = "") -> GoogleError:
    """No Authorization header, on an OAuth-only API (Gmail, Docs, Slides).

    On the editor family the `errors[]` entry (shown at `$.xgafv=1`) is the short ``Login
    Required.`` at ``location: Authorization`` — measured on Docs. Gmail's entry is unmeasured, and
    keeps the long message with no location."""
    if family(path) == EDITOR:
        return GoogleError(
            401,
            MISSING_CREDENTIALS_MESSAGE,
            reason="required",
            location="Authorization",
            location_type="header",
            status="UNAUTHENTICATED",
            short="Login Required.",
        )
    return GoogleError(
        401, MISSING_CREDENTIALS_MESSAGE, reason="required", status="UNAUTHENTICATED"
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
    return missing_credentials(path)


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
    repeat, which is the one real reads."""
    return None if query is None else query.get(XGAFV)


def validate_system_parameters(request: Request) -> None:
    """Refuse a `$.xgafv` other than `1` or `2`, on a Google-family path, before the route runs.

    A router-level dependency, so it is the first thing a request meets: measured, real answers this
    400 ahead of a bad token, a missing credential and an unparseable range. The batch endpoint is
    not a family path and is left alone."""
    if family(request.url.path) is None:
        return
    value = xgafv(request.query_params)
    if value is not None and value not in XGAFV_VALUES:
        raise bad_system_parameter(XGAFV, value)


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
    if _FAMILY_HAS_ERRORS[family(path)] or xgafv(query) == "1":
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
