"""Google's error envelope, per API family.

`google-api-python-client` reads ``error.message`` to build its ``HttpError``, and real clients
branch on ``error.status`` or ``errors[].reason``. FastAPI's default ``{"detail": …}`` gives them
none of that, so error handling could not be developed or tested against this mock even though its
status codes were already right.

Everything here was measured against the live Docs / Drive / Gmail / Sheets / Slides APIs. The
envelope is NOT uniform — three families differ in which optional members they carry:

    family                        errors[]   status                no Authorization header
    ------------------------------|---------|----------------------|------------------------
    Drive v3                      | always  | auth failures only   | 403 PERMISSION_DENIED
    Gmail v1                      | always  | always               | 401 UNAUTHENTICATED
    Docs v1 / Sheets v4 / Slides  | never   | always               | 401 UNAUTHENTICATED

A present-but-invalid bearer token is 401 UNAUTHENTICATED in every family, which is why a missing
header and a bad token are separate constructors here rather than one "unauthorized".
"""

from __future__ import annotations

from fastapi import HTTPException

# Whether a family carries the legacy `errors[]` array. `status` needs no per-family flag: Drive's
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
    """Which envelope a request path takes, or ``None`` for a non-Google route (which keeps
    FastAPI's ``{"detail": …}``)."""
    for prefix, fam in _PREFIX_FAMILY:
        if path.startswith(prefix):
            return fam
    return None


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
    ):
        super().__init__(status_code=status_code, detail=message)
        self.message = message
        self.reason = reason
        self.location = location
        self.location_type = location_type
        self.status = status
        self.short = short


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
    pass a fuller message where the mock can explain a refusal Google does not have."""
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
    """The editor APIs' generic 400."""
    return GoogleError(400, message, reason="invalidArgument", status="INVALID_ARGUMENT")


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
    """No Authorization header, on an OAuth-only API (Gmail, Docs, Slides)."""
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
    return missing_credentials()


def body(path: str, exc: HTTPException) -> dict:
    """Render an exception into its family's envelope.

    A plain ``HTTPException`` raised on a Google path still renders — it just carries no reason —
    so a route that has not been migrated degrades instead of 500ing."""
    message = getattr(exc, "message", None)
    if message is None:
        detail = exc.detail
        message = detail if isinstance(detail, str) else str(detail)
    err: dict = {"code": exc.status_code, "message": message}
    if _FAMILY_HAS_ERRORS[family(path)]:
        entry = {"message": getattr(exc, "short", None) or message, "domain": "global"}
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
    return {"error": err}
