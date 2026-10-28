"""The error code an answer carries, and the class of a failure.

An error code is the vendor's own identifier where its envelope has one: Slack's ``error``, S3's
``<Code>``, Google's ``reason`` or else its ``status``, Notion's ``code``, HubSpot's ``category``,
Microsoft Graph's ``error.code``, a GraphQL error's ``extensions.code``, or the ``code`` Fireflies
writes beside the message. GitHub, Jira, Confluence, Graph's OData validator and the Linear
resolvers' refusals that carry no code answer with a message only; those messages are Backlot's own
text, so each maps to a fixed slug here and the message itself, which can carry a key, a sha or a
field name from the request, is never sent. A message this table does not know is ``unrecognized``.

The HTTP layer in front of Linear's and Fireflies' GraphQL refuses a body in an envelope of its own,
with no ``errors``. Fireflies' HTTP layer names its code in ``code``. Linear's is Express, which
names one in ``extensions.code`` when its envelope has ``extensions``; Express's ``code`` is the
status as a number, which is not an identifier.

The class is the cause a failed call is put down to, sent as ``backlot.error_class`` on every
failure:

| Class | The call failed because |
|---|---|
| ``unsupported`` | Backlot does not do this yet: the baseline records the operation as ``missing`` or ``refused``, or the GraphQL engine refused a filter |
| ``internal`` | of a Backlot bug: its own failure, or a 5xx other than a vendor's answer (below) |
| ``rate_limit`` | the vendor's rate limit was reached: a 429, or a 403 with ``x-ratelimit-remaining: 0`` as GitHub sends |
| ``auth`` | the credential was missing, wrong, or not allowed: a 401 or 403, or one in ``_AUTH`` |
| ``client`` | the request was wrong: any other failure |

Backlot's own failure is an exception raised instead of an answer, or a field error the GraphQL
engine answers at 200 and marks (``backlot.graphql.engine.Result.raised``). The rules read whether
the failure is Backlot's own, the status, the baseline's support state, the error code and the
``x-ratelimit-remaining`` header, so a code no rule names is classed by its status. A 501 from an
operation Backlot serves is the vendor's own answer (Google's ``notImplemented``), so it is
``client``, and so is a 505, S3's front door's answer to a target holding a space
(``backlot.request_target._s3``), and each 5xx in ``_ANSWERED_5XX``, a vendor's answer to a wrong
request.
"""

from __future__ import annotations

import json
import re

# (pattern on the message, slug), first match wins.
_GITHUB = [
    (r"^Not Found$", "not_found"),
    (r"^Requires authentication$", "requires_authentication"),
    (r"^Bad credentials$", "bad_credentials"),
    (r"^Validation Failed$", "validation_failed"),
    (
        r"^(Only the first 1000 search results|Cannot access beyond the first 1000)",
        "search_result_window",
    ),
    (r"^The sha parameter must be exactly 40 characters", "invalid_sha"),
    (r"^No commit found for SHA: ", "no_commit_for_sha"),
    (r"^No commit found for the ref ", "no_commit_for_ref"),
    (r"^Branch not found$", "branch_not_found"),
    (r"^API rate limit exceeded", "rate_limited"),
    (
        r'^(Bad Request$|The version you specified in the "X-GitHub-Api-Version")',
        "unsupported_api_version",
    ),
]
_JIRA = [
    (r"^Issue does not exist or you do not have permission to see it", "issue_not_found"),
    (r"^No project could be found with (key|id) ", "project_not_found"),
    (r"^The field to order by (must|should) be one of", "invalid_order_by"),
    (r"^Unbounded JQL queries are not allowed", "unbounded_jql"),
    (r"^The maxResults parameter must be between", "invalid_max_results"),
    (r"^The provided nextPageToken is invalid", "invalid_next_page_token"),
    (
        r"^(Invalid request payload|No content to map|There was an error parsing JSON)",
        "invalid_payload",
    ),
    (r"^(Failed to convert |The max number of issues to reconcile is )", "invalid_parameter"),
    (r"^No endpoint ", "no_endpoint"),
    (r"^Content-Type '.*' is not supported", "unsupported_media_type"),
    (r"^Acceptable representations: ", "not_acceptable"),
    (r"^Method '\w+' is not supported", "method_not_allowed"),
    (
        r"^(Client must be authenticated|Failed to parse Connect Session Auth Token)",
        "unauthenticated",
    ),
]
# Confluence prefixes most messages with the Java exception's class, so the rows for those are not
# anchored.
_CONFLUENCE = [
    (r"No content found with id", "content_not_found"),
    (r"No space (found )?with key", "space_not_found"),
    (r"cql query parameter is required", "cql_required"),
    (
        r"Could not parse cql : |Expecting a value for operator |Missing closing parenthesis near "
        r"|Unrecognised clause at : ",
        "invalid_cql",
    ),
    (r"^Unauthorized$", "unauthenticated"),
    (r"^null for uri: ", "no_endpoint"),
    (r"FORBIDDEN|^Current user not permitted to use Confluence", "forbidden"),
    (r"Failed to convert value|cannot be less than zero", "invalid_parameter"),
    (r"Cannot find custom content type", "content_type_not_found"),
]
_GRAPHQL = [(r"^unsupported ", "unsupported_filter")]
# Graph's OData validator answers with an empty `error.code`; its message names the option.
_MSGRAPH = [(r"^The query specified in the URI is not valid\.", "invalid_query_option")]
_S3_CODE = re.compile(rb"<Code>([A-Za-z0-9_.-]{1,64})</Code>")


def _slug(message: object, table: list[tuple[str, str]]) -> str:
    if not isinstance(message, str):
        return "unrecognized"
    for pattern, slug in table:
        if re.search(pattern, message):
            return slug
    return "unrecognized"


def error_code(source: str, status: int, body: bytes) -> str:
    """The error code of an answer, ``""`` for an answer that is not an error or carries none."""
    if source == "s3":
        m = _S3_CODE.search(body) if status >= 400 else None
        return m.group(1).decode() if m else ""
    try:
        doc = json.loads(body)
    except ValueError:
        # Jira answers a missing credential with the bare sentence as text; its HTML pages (the
        # `(null)` 400, the dead-link 404) carry no message.
        text = body[:512].decode("utf-8", "replace").strip()
        return _slug(text, _JIRA) if source == "jira" and text and not text.startswith("<") else ""
    if not isinstance(doc, dict):
        return ""
    if source == "slack":
        return (
            doc.get("error", "")
            if doc.get("ok") is False and isinstance(doc.get("error"), str)
            else ""
        )
    if source in ("linear", "fireflies"):
        errors = doc.get("errors")
        if errors is None and status >= 400:
            extensions = doc.get("extensions")
            code = extensions.get("code") if isinstance(extensions, dict) else doc.get("code")
            return code if isinstance(code, str) else ""
        if not errors or not isinstance(errors, list) or not isinstance(errors[0], dict):
            return ""
        extensions = errors[0].get("extensions")
        code = extensions.get("code") if isinstance(extensions, dict) else None
        if not code:
            code = errors[0].get("code")
        if isinstance(code, str) and code:
            return code
        return _slug(errors[0].get("message"), _GRAPHQL)
    if status < 400:
        return ""
    if source == "github":
        return _slug(doc.get("message"), _GITHUB) if "message" in doc else ""
    if source in ("gmail", "google_drive"):
        error = doc.get("error")
        if not isinstance(error, dict):
            return ""
        # Docs, Sheets and Slides send no `errors[].reason`, only the canonical `status`.
        entries = error.get("errors")
        first = entries[0] if isinstance(entries, list) and entries else None
        reason = first.get("reason") if isinstance(first, dict) else None
        if isinstance(reason, str):
            return reason
        return error["status"] if isinstance(error.get("status"), str) else ""
    if source == "notion":
        return doc["code"] if isinstance(doc.get("code"), str) else ""
    if source == "msteams":
        error = doc.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("code"), str):
            return ""
        return error["code"] or _slug(error.get("message"), _MSGRAPH)
    if source == "hubspot":
        return doc["category"] if isinstance(doc.get("category"), str) else ""
    if source == "jira":
        messages = doc.get("errorMessages") or [
            doc.get("detail") or doc.get("message") or doc.get("error")
        ]
        return _slug(messages[0] if messages else None, _JIRA) if any(messages) else ""
    if source == "confluence":
        message = doc.get("message") or doc.get("detail")
        return _slug(message, _CONFLUENCE) if message else ""
    return ""


# The auth errors a vendor answers at a status other than 401 and 403: Slack's at 200, and
# Fireflies' at 500 (`backlot.routers.fireflies._auth_failed`).
_AUTH = frozenset(
    {("slack", "account_inactive"), ("slack", "invalid_auth"), ("slack", "not_authed")}
    | {("fireflies", "auth_failed")}
)
# Each 5xx a vendor answers a wrong request with, by source and error code, and Backlot answers too:
# Fireflies' 500 for a document graphql-depth-limit throws on
# (`backlot.routers.fireflies._depth_limit`).
_ANSWERED_5XX = frozenset({("fireflies", "INTERNAL_SERVER_ERROR")})


def error_class(
    source: str, support: str, status: int, code: str, spent: bool, raised: bool = False
) -> str:
    """The class of a failed call. ``spent`` is whether it answered ``x-ratelimit-remaining: 0``,
    ``raised`` whether the failure is Backlot's own, as the module docstring has it."""
    if raised:
        return "internal"
    if support in ("missing", "refused") or code == "unsupported_filter":
        return "unsupported"
    if status == 429 or (status == 403 and spent):
        return "rate_limit"
    if status in (401, 403) or (source, code) in _AUTH:
        return "auth"
    if status >= 500 and status not in (501, 505) and (source, code) not in _ANSWERED_5XX:
        return "internal"
    return "client"
