"""Microsoft Graph's error envelope.

The stable machine-readable contract is ``error.code``. Microsoft explicitly warns callers not
to depend on the human-readable message (https://learn.microsoft.com/graph/errors).

Every body below was read off graph.microsoft.com on 2026-10-20 and 2026-10-21 (the 401s
anonymously, the rest with app-only and delegated tokens on a Teams tenant). Graph answers an error
in one of two shapes. Most carry ``code``, ``message`` and an ``innerError`` of ``date``,
``request-id`` and ``client-request-id``, with ``code`` and ``message`` ahead of those where the
inner layer reports its own. A request the OData query validator refuses carries an EMPTY ``code``,
a ``details`` list, and the reason again as ``innerError.message``.

"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from fastapi import HTTPException


def owns(path: str) -> bool:
    """Whether this module shapes errors for ``path`` — the question ``backlot.errors`` asks every
    envelope."""
    return path.startswith("/msgraph")


# The id of the request being answered, which Graph sends as the `request-id` header and inside
# every `innerError` — and as `client-request-id` too, unless the caller sent one of its own. Set
# per request by the middleware in `backlot.main`; a fresh one is minted where none is set.
REQUEST_ID: ContextVar[str | None] = ContextVar("msgraph_request_id", default=None)
# The `client-request-id` the caller sent, where it is a GUID; Graph echoes it, and mints none.
CLIENT_REQUEST_ID: ContextVar[str | None] = ContextVar("msgraph_client_request_id", default=None)

# What a 200 JSON body is served as, beside `OData-Version: 4.0`.
JSON_MEDIA_TYPE = (
    "application/json;odata.metadata=minimal;odata.streaming=true;IEEE754Compatible=false;"
    "charset=utf-8"
)

# Sent on every 401, whatever the credential looked like.
WWW_AUTHENTICATE = (
    'Bearer realm="", authorization_uri="https://login.microsoftonline.com/common/oauth2/'
    'authorize", client_id="00000003-0000-0000-c000-000000000000"'
)


class GraphError(HTTPException):
    """An error carrying the ``code`` a client branches on, and what Graph puts beside it: an inner
    code and message where the inner layer reports its own, and the validator's ``details``. The
    message is still the ``detail``, so a plain ``HTTPException`` raised on a Graph path renders
    too — it just falls back to the status's generic code."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        inner: str | None = None,
        inner_message: str | None = None,
        details: bool = False,
        inner_details: bool = False,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.inner = inner
        self.inner_message = inner_message
        self.details = details
        self.inner_details = inner_details


# The code a bare HTTPException on a Graph path renders as, so a route that raises one still
# produces a body a client can branch on rather than a null code.
_GENERIC_CODE = {
    400: "BadRequest",
    401: "InvalidAuthenticationToken",
    403: "Forbidden",
    404: "NotFound",
    405: "BadRequest",
    422: "BadRequest",
}


# --- 401 -------------------------------------------------------------------------------------
# GET /v1.0/me with each credential shape below. A token that resolves to nobody answers the same
# under every spelling — `bearer nope`, `BEARER nope`, `token nope`, `Basic bm9wZTpub3Bl` and a bare
# `nope` all answer as `Bearer nope` — and a valid one is read only after `Bearer` or with no
# scheme (see `auth.msgraph_token`).

JWT_MALFORMED_MESSAGE = "Protocol 'Bearer' failed to validate because The token could not be read."


def _unauthorized(message: str) -> GraphError:
    return GraphError(
        401,
        "InvalidAuthenticationToken",
        message,
        headers={"WWW-Authenticate": WWW_AUTHENTICATE},
    )


def no_credentials() -> GraphError:
    """No Authorization header at all."""
    return _unauthorized("Access token is empty.")


def empty_credentials() -> GraphError:
    """An Authorization header whose token part is blank (``Bearer`` and nothing after it)."""
    return _unauthorized("UnableToParseTokens")


def bad_token() -> GraphError:
    """A token that was presented, is not a JWT, and resolves to nobody."""
    return _unauthorized(JWT_MALFORMED_MESSAGE)


def foreign_jwt() -> GraphError:
    """A token shaped as a JWT (three base64url segments) that resolves to nobody: it parses, and
    names no application Graph issued it to."""
    return _unauthorized("NoAppIdInToken")


# --- 400 -------------------------------------------------------------------------------------


def bad_request(message: str) -> GraphError:
    return GraphError(400, "BadRequest", message)


def request_bad_request(message: str) -> GraphError:
    """The directory's refusal of a query argument it does not recognise."""
    return GraphError(400, "Request_BadRequest", message)


def unsupported_query(message: str) -> GraphError:
    """The directory's refusal of a query option it parsed and will not run."""
    return GraphError(400, "Request_UnsupportedQuery", message)


def query_option_invalid(reason: str) -> GraphError:
    """The OData validator's refusal: no code, a ``details`` list, and the reason twice."""
    return GraphError(
        400,
        "",
        f"The query specified in the URI is not valid. {reason}",
        inner_message=reason,
        details=True,
    )


def option_not_allowed(option: str) -> GraphError:
    """``$orderby`` and friends on a method whose validator allows none of them."""
    return query_option_invalid(
        f"Query option '{option}' is not allowed. To allow it, set the 'AllowedQueryOptions' "
        "property on EnableQueryAttribute or QueryValidationSettings."
    )


def top_too_large(limit: int, value: str) -> GraphError:
    return query_option_invalid(
        f"The limit of '{limit}' for Top query has been exceeded. The value from the incoming "
        f"request is '{value}'."
    )


def skip_too_large(value: str) -> GraphError:
    return query_option_invalid(
        f"The limit of '2147483647' for Skip query has been exceeded. The value from the incoming "
        f"request is '{value}'."
    )


def invalid_integer(option: str, value: str) -> GraphError:
    """``$top`` or ``$skip`` that is not a non-negative 64-bit integer. A value past 1,024
    characters is echoed as its first 1,021 and ``...``."""
    shown = value if len(value) <= 1024 else value[:1021] + "..."
    return bad_request(
        f"Invalid value '{shown}' for ${option} query option found. The ${option} query option "
        "requires a non-negative integer value."
    )


def empty_option_value() -> GraphError:
    """An empty ``$skiptoken`` on a Teams route, refused before the rest of the request is read."""
    return bad_request("The argument 'rawValue' is null or empty. (Parameter 'rawValue')")


def invalid_count(value: str) -> GraphError:
    return bad_request(f"'{value}' is not a valid count option.")


def headers_invalid() -> GraphError:
    """The directory's refusal of a ``ConsistencyLevel`` it cannot read."""
    return request_bad_request("One or more headers are invalid.")


# What the directory lists as the types a `$format` may name, cut where Graph cuts it.
_SUPPORTED_TYPES = (
    "application/json;odata.metadata=minimal;odata.streaming=true;IEEE754Compatible=false, "
    "application/json;odata.metadata=minimal;odata.streaming=true;IEEE754Compatible=true, "
    "application/json;odata.metadata=minimal;odata.streaming=true, "
    "application/json;odata.metadata=minimal;odata.streaming=false;IEEE754Compatible=false, "
    "application/json;odata.metadata=minimal;odata.streaming=false;IEEE754Compatible=true, "
    "application/json;odata.metadata=minimal;odata.streaming=false, "
    "application/json;odata.metadata=minimal;IEEE754Compatible=false, "
    "application/json;odata.metadata=minimal;IEEE754Compatible=true, "
    "application/json;odata.metadata=minimal, "
    "application/json;odata.metadata=full;odata.streaming=true;IEEE754Compatible=false, "
    "application/json;odata.metadata=full;odata.streaming=true;IEEE754Compatible=true, "
    "application/json;odata.metadata=full;odata.streaming=true, "
    "application/json;odata.metadata=full;odata.streaming=false;IEEE754Compatible=false, "
    "application/json;odata.metadata=full;odata.streaming=false;IEEE754Compatib..."
)


def unsupported_format(value: str) -> GraphError:
    """The directory's ``$format`` naming something other than JSON: a bare word is not a media
    type, and a media type is not one it serves."""
    if "/" not in value:
        message = f"The MIME type '{value}' is invalid or unspecified."
    else:
        message = (
            "A supported MIME type could not be found that matches the acceptable MIME types for "
            f"the request. The supported type(s) '{_SUPPORTED_TYPES}' do not match any of the "
            f"acceptable MIME types '{value}'."
        )
    return GraphError(415, "Request_BadRequest", message)


def unknown_property(name: str, type_name: str) -> GraphError:
    """``$select`` or ``$expand`` naming a property the type does not declare."""
    return bad_request(
        f"Parsing OData Select and Expand failed: Could not find a property named '{name}' on "
        f"type 'microsoft.graph.{type_name}'."
    )


def unknown_segment(segment: str) -> GraphError:
    """A path whose first segment names nothing the service exposes."""
    return bad_request(f"Resource not found for the segment '{segment}'.")


def invalid_version(version: str) -> GraphError:
    return GraphError(404, "ResourceNotFound", f"Invalid version: {version}")


def write_without_content_type() -> GraphError:
    """A ``POST``, ``PUT`` or ``PATCH`` with no ``Content-Type``, refused before its path is."""
    return bad_request(
        "Write requests (excluding DELETE) must contain the Content-Type header declaration."
    )


def write_not_json() -> GraphError:
    return bad_request("Entity only allows writes with a JSON Content-Type header.")


def post_not_allowed() -> GraphError:
    return GraphError(
        405, "Request_BadRequest", "Specified HTTP method is not allowed for the request target."
    )


# --- 403, 404, 410, 502 ----------------------------------------------------------------------


def forbidden(
    message: str,
    *,
    inner: str | None = None,
    inner_message: str | None = None,
    inner_details: bool = False,
) -> GraphError:
    return GraphError(
        403,
        "Forbidden",
        message,
        inner=inner,
        inner_message=inner_message,
        inner_details=inner_details,
    )


def not_the_caller() -> GraphError:
    """``/users/{id}/joinedTeams`` for someone else, to a caller who is no administrator."""
    message = "Invalid UserId. UserId does not match Access token UserObjectId"
    return forbidden(message, inner="AccessDenied", inner_message=message, inner_details=True)


def not_in_roster(user_id: str, channel_id: str) -> GraphError:
    """A private channel the caller is not in, read as a channel or for its members."""
    return forbidden(
        "InsufficientPrivileges",
        inner="1",
        inner_message=(
            f"AclCheckFailed-The initiator 8:orgid:{user_id} is not a member of the roster "
            f"92:{channel_id.removeprefix('19:')} in the generic thread {channel_id}"
        ),
    )


def not_found(
    message: str,
    *,
    inner: str | None = None,
    inner_message: str | None = None,
    inner_details: bool = False,
):
    return GraphError(
        404,
        "NotFound",
        message,
        inner=inner,
        inner_message=inner_message,
        inner_details=inner_details,
    )


def item_not_found(message: str) -> GraphError:
    """A 404 whose inner layer reports ``ItemNotFound`` in the same words, with a ``details``
    list."""
    return not_found(message, inner="ItemNotFound", inner_message=message, inner_details=True)


def no_such_team(team_id: str) -> GraphError:
    """``/teams/{id}`` for a team id that names no team."""
    return item_not_found(f"No team found with Group Id {team_id}")


def no_such_person(user_id: str) -> GraphError:
    """``/users/{id}/joinedTeams`` for an address the tenant does not have."""
    return not_found(f"Failed to find user with id '{user_id}' in the tenant")


def no_such_member_of(user_id: str) -> GraphError:
    """``/users/{id}/joinedTeams`` for an object id the directory does not have: the failure of the
    group lookup behind it, which carries that lookup's own response headers and ids."""
    backend = uuid.uuid4()
    when = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    return item_not_found(
        "Failed to execute MsGraph backend request GetUserGroupsS2SRequest. Workload "
        f"MsGraph_DirectoryServices. Request Url: https://graph.microsoft.com/edu/users/{user_id}"
        "/memberOf/$/microsoft.graph.group?$filter=groupTypes/any(c:c+eq+'Unified')&$top=999, "
        "Request Method: GET, Response Status Code: NotFound, Response Headers: "
        f"Strict-Transport-Security: max-age=31536000\r\nrequest-id: {backend}\r\n"
        f"client-request-id: {backend}\r\nx-ms-resource-unit: 2\r\nDate: {when}\r\n, Reason "
        f"Phrase: Not Found, Request ID {uuid.uuid4().hex.upper()}."
    )


def unsupported_path() -> GraphError:
    """A path under a known resource that names no API."""
    return not_found("Requested API is not supported. Please check the path.")


def team_not_found(team_id: str) -> GraphError:
    """``/teams/{id}/channels`` for a team id that names no team."""
    message = f"No threadId found for TeamId {team_id}"
    return not_found(message, inner="Unknown", inner_message=message)


def channel_not_found(channel_id: str) -> GraphError:
    """A channel id the team does not have, read as a channel (its own route, or ``/members``)."""
    return not_found(
        "NotFound",
        inner="1",
        inner_message=f"LocationLookupFailed-Location lookup failed for thread {channel_id}",
    )


def channel_gone() -> GraphError:
    """A channel id the team does not have, read for its messages."""
    return GraphError(410, "Gone", "UnknownError")


def message_not_found() -> GraphError:
    return not_found("UnknownError")


def user_not_found(user_id: str) -> GraphError:
    """The directory's own 404, which names the id it could not resolve."""
    return GraphError(
        404,
        "Request_ResourceNotFound",
        f"Resource '{user_id}' does not exist or one of its queried reference-property objects "
        "are not present.",
    )


def execution_failed() -> GraphError:
    """A filter that binds and that .NET fails to run, such as a division by zero."""
    return GraphError(500, "InternalServerError", "Failed to execute request.")


def backend_failure() -> GraphError:
    """What ``/replies`` answers a ``$skiptoken`` it cannot decode."""
    return GraphError(502, "BadGateway", "Failed to execute backend request.")


def _inner(exc) -> dict:
    """The ``innerError`` real Graph sends on every error.

    ``date`` has no timezone suffix and no fractional seconds live ("2026-09-02T11:24:27"), so it
    is formatted rather than handed to ``isoformat()``. The two ids are the ones the response's
    headers carry: equal, unless the caller sent a GUID ``client-request-id`` of its own.
    """
    body: dict = {}
    code = getattr(exc, "inner", None)
    message = getattr(exc, "inner_message", None)
    if code:
        body["code"] = code
    if message:
        body["message"] = message
    if getattr(exc, "inner_details", False):
        body["details"] = []
    request_id = REQUEST_ID.get() or str(uuid.uuid4())
    body["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    body["request-id"] = request_id
    body["client-request-id"] = CLIENT_REQUEST_ID.get() or request_id
    return body


def http_body(path: str, exc: HTTPException, query=None) -> dict:
    detail = exc.detail
    message = detail if isinstance(detail, str) else str(detail)
    code = getattr(exc, "code", None)
    error: dict = {
        "code": code if code is not None else _GENERIC_CODE.get(exc.status_code, "UnknownError"),
        "message": message,
    }
    if getattr(exc, "details", False):
        error["details"] = []
    error["innerError"] = _inner(exc)
    return {"error": error}


def validation_body(path: str, errors) -> tuple[int, dict]:
    """The answer to a request FastAPI's own validator rejected.

    **400, not 422.** Graph has no 422 for a read: a parameter it cannot use is a ``BadRequest``,
    and answering 422 would send a client down a branch the real service never takes.

    The routes here take their parameters off the raw request rather than through typed signatures,
    so nothing should reach the validator — but a body is shaped rather than left as
    ``{"detail": …}`` so that if one ever does, a Graph client still sees a Graph error.
    """
    return 400, http_body(path, bad_request("The request is malformed or incorrect."))
