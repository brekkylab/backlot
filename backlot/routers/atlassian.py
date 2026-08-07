"""Mock Atlassian Cloud APIs (read-only): Jira (``/rest/api/3``) and Confluence
(``/wiki/rest/api``). Client base_url: ``http://<host>/atlassian``.

Auth: HTTP Basic ``email:api_token`` (or Bearer). Jira issue descriptions are ADF;
Confluence bodies are storage-format XHTML — matching the real APIs.
"""

from __future__ import annotations

import re
from html import escape

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from backlot import auth, store, synth
from backlot.openapi import qp
from backlot.acl import Caller
from backlot.config import get_settings
from backlot.pagination import confluence_next_link, decode_cursor, next_page_token

router = APIRouter(prefix="/atlassian", tags=["atlassian"])


# --- OpenAPI enrichment (issue #4 bridge) --------------------------------------------------
# jira_search reads params query-or-body (GET+POST) so they're documented with openapi_extra (no
# signature change); confluence params are query-only. Response models use extra="allow" to
# preserve every field. Error paths raise HTTPException (Atlassian-shaped), not filtered here.
# Secondary metadata routes (roles / linktypes / labels / restrictions) are left untyped — the
# bridge still exposes them as tools; they aren't retrieval surfaces.


class _ALoose(BaseModel):
    model_config = ConfigDict(extra="allow")


class JiraServerInfo(_ALoose):
    baseUrl: str
    version: str
    deploymentType: str = "Cloud"


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


_X_JIRA_SEARCH = {
    "parameters": [qp("jql"), qp("maxResults", "integer"), qp("nextPageToken")],
    "requestBody": {
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
        }
    },
}
_P_EXPAND = {"parameters": [qp("expand")]}
_P_CQL = {"parameters": [qp("cql", required=True), qp("limit", "integer"), qp("start", "integer")]}
_P_CONTENT = {
    "parameters": [qp("expand"), qp("spaceKey"), qp("limit", "integer"), qp("start", "integer")]
}


def _require(request: Request) -> Caller:
    return auth.require_basic_or_bearer(request, "Unauthorized")


def _site(request: Request) -> str:
    s = get_settings()
    host = request.headers.get("host") or s.atlassian_site or f"{s.org_name}.atlassian.net"
    return f"{request.url.scheme}://{host}"


# ================================ Jira ==========================================


def _adf(content: str) -> dict:
    paras = [p for p in content.split("\n\n") if p.strip()] or [content]
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paras],
    }


def _jira_container_for_key(conn, token: str) -> str | None:
    """Resolve a JQL project token to its backing container. Matches either the synthesized
    project key (``PAY3F9A2C``, case-insensitive) or the literal container name (e.g.
    ``payments``, case-insensitive) — real Jira project pickers accept both key and name.
    Anything else is unresolvable -> None (callers must treat this as "0 results", never
    silently fall back to the unfiltered corpus)."""
    for r in store.list_containers(conn, "jira"):
        if synth.jira_project_key(r["name"]) == token.upper() or r["name"].lower() == token.lower():
            return r["name"]
    return None


@router.get(
    "/rest/api/2/serverInfo", response_model=JiraServerInfo
)  # jira PyPI client probes this on connect
@router.get("/rest/api/3/serverInfo", response_model=JiraServerInfo)
async def jira_server_info(request: Request):
    site = _site(request)
    return {
        "baseUrl": site,
        "version": "1000.0.0",
        "deploymentType": "Cloud",
        "versionNumbers": [1000, 0, 0],
        "buildNumber": 100000,
        "serverTime": synth.rfc3339_millis(synth.epoch("serverInfo")),
    }


@router.get("/rest/api/3/project/search")
async def jira_project_search(request: Request):
    conn = auth.conn(request)
    _require(request)
    values = []
    for r in store.list_containers(conn, "jira"):
        key = synth.jira_project_key(r["name"])
        values.append(
            {
                "id": str(synth.github_user_id(r["name"])),
                "key": key,
                "name": r["name"],
                "projectTypeKey": "software",
                "simplified": False,
                "style": "classic",
                "isPrivate": False,
                "avatarUrls": synth.avatar_urls("proj:" + key),
                "self": f"{_site(request)}/rest/api/3/project/{key}",
            }
        )
    return {"values": values, "maxResults": 50, "startAt": 0, "total": len(values), "isLast": True}


@router.get("/rest/api/3/project/{key}/role")
async def jira_project_roles(key: str, request: Request):
    return {"Users": f"{_site(request)}/rest/api/3/project/{key}/role/10002"}


@router.get("/rest/api/3/project/{key}/role/{role_id}")
async def jira_project_role(key: str, role_id: int, request: Request):
    conn = auth.conn(request)
    _require(request)
    container = _jira_container_for_key(conn, key)
    actors = []
    if container:
        c = store.get_container(conn, "jira", container)
        if c and c["group_id"]:
            for m in store.group_members(conn, c["group_id"]):
                actors.append(
                    {
                        "id": synth.github_user_id(m["email"]),
                        "displayName": m["display_name"],
                        "type": "atlassian-user-role-actor",
                        "actorUser": {"accountId": synth.atlassian_account_id(m["email"])},
                    }
                )
    return {"id": role_id, "name": "Users", "actors": actors}


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
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            params.update(await request.json())
        except Exception:
            pass
    jql = str(params.get("jql", ""))
    container = _project_from_jql(conn, jql)
    if container is _JIRA_PROJECT_UNRESOLVED:
        # a project= clause was present but didn't match any project: strict 0 matches, not
        # the unfiltered corpus.
        return {"issues": [], "isLast": True}
    term = _text_from_jql(jql)
    limit = _int(params.get("maxResults"), get_settings().default_page_size)
    offset = decode_cursor(params.get("nextPageToken"))
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
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    doc_id = request.app.state.index["jira"].get(key)
    if doc_id is None:
        raise HTTPException(status_code=404, detail="Issue does not exist")
    row = store.get_document(conn, "jira", doc_id, visible_ids=ids)
    if row is None:
        raise HTTPException(status_code=404, detail="Issue does not exist")
    return _jira_issue(conn, request, row, expand=request.query_params.get("expand", ""))


@router.get("/rest/api/2/issue/{key}/comment", response_model=JiraComments)
@router.get("/rest/api/3/issue/{key}/comment", response_model=JiraComments)
async def jira_issue_comments(key: str, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    doc_id = request.app.state.index["jira"].get(key)
    if doc_id is None or store.get_document(conn, "jira", doc_id, visible_ids=ids) is None:
        raise HTTPException(status_code=404, detail="Issue does not exist")
    cs = store.doc_comments(conn, "jira", doc_id)
    site = _site(request)
    return {
        "startAt": 0,
        "maxResults": len(cs),
        "total": len(cs),
        "comments": [_jira_comment(c, site) for c in cs],
    }


@router.get("/rest/api/3/issueLinkType")
async def jira_link_types(request: Request):
    _require(request)
    return {
        "issueLinkTypes": [
            {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
            {"id": "10001", "name": "Relates", "inward": "relates to", "outward": "relates to"},
            {
                "id": "10002",
                "name": "Duplicate",
                "inward": "is duplicated by",
                "outward": "duplicates",
            },
            {"id": "10003", "name": "Cloners", "inward": "is cloned by", "outward": "clones"},
        ]
    }


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
    _require(request)
    return _JIRA_FIELDS


# sentinel: the JQL carried a `project = X` clause that didn't resolve to any known project.
# Distinct from `None` (no project clause at all -> no filter), so callers never silently
# collapse "unresolvable" into "unfiltered" (the fidelity gap found & fixed for Confluence's
# space= handling applies identically to Jira's project= handling).
_JIRA_PROJECT_UNRESOLVED = object()


def _project_from_jql(conn, jql: str):
    m = re.search(r"project\s*=\s*[\"']?([A-Za-z0-9_]+)", jql)
    if not m:
        return None
    container = _jira_container_for_key(conn, m.group(1))
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


def _jira_ref(row, site: str = "") -> dict:
    status = row["status"] or "To Do"
    return {
        "id": str(synth.jira_numeric_id(row["doc_id"])),
        "key": synth.jira_key(row["doc_id"], synth.jira_project_key(row["project"])),
        "self": f"{site}/rest/api/3/issue/{synth.jira_numeric_id(row['doc_id'])}" if site else None,
        "fields": {
            "summary": row["title"],
            "status": {"name": status, "statusCategory": _status_category(status)},
            "priority": {"name": row["priority"] or "Medium"},
            "issuetype": {"name": row["issuetype"] or "Task"},
        },
    }


def _jira_comment(c, site: str = "") -> dict:
    ts = c["created_ts"] or synth.epoch(c["id"])
    actor = _jira_actor(c["author_email"], site)
    return {
        "id": c["id"],
        "self": f"{site}/rest/api/3/issue/comment/{c['id']}" if site else None,
        "author": actor,
        "body": _adf(c["body"]),
        "updateAuthor": actor,
        "created": synth.jira_datetime(ts),
        "updated": synth.jira_datetime(ts),
        "jsdPublic": True,
    }


def _issuetype(name: str, doc_id: str) -> dict:
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
    created = row["created_ts"] or synth.epoch(row["doc_id"])
    updated = row["updated_ts"] or created + 3600
    pkey = synth.jira_project_key(row["project"])
    reporter = _jira_actor(row["reporter_email"] or row["author_email"], site)
    creator = _jira_actor(row["author_email"], site)
    assignee = _jira_actor(row["assignee_email"], site) if row["assignee_email"] else None
    status = row["status"] or "To Do"
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
        "issuetype": _issuetype(row["issuetype"], row["doc_id"]),
        "project": {
            "id": str(synth.github_user_id(row["project"])),
            "key": pkey,
            "name": row["project"],
            "projectTypeKey": "software",
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
        cs = store.doc_comments(conn, "jira", row["doc_id"])
        fields["comment"] = {
            "comments": [_jira_comment(c, site) for c in cs],
            "maxResults": len(cs),
            "total": len(cs),
            "startAt": 0,
        }
        fields["issuelinks"] = store.jcol(row, "issuelinks")
        subs = store.children(conn, "jira", row["doc_id"])
        fields["subtasks"] = [_jira_ref(s, site) for s in subs]
        if row["parent_id"]:
            prow = store.get_document(conn, "jira", row["parent_id"])
            if prow:
                fields["parent"] = _jira_ref(prow, site)
    nid = synth.jira_numeric_id(row["doc_id"])
    issue = {
        "id": str(nid),
        "key": synth.jira_key(row["doc_id"], pkey),
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


def _storage(content: str) -> str:
    """Confluence storage format — XHTML with a leading structured macro, as the real
    editor emits (distinct from the rendered view below)."""
    paras = [p for p in content.split("\n\n") if p.strip()] or [content]
    return "".join(f"<p>{escape(p)}</p>" for p in paras)


def _view(content: str) -> str:
    """Rendered ``view`` HTML — differs from storage (wrapped, ids, no ac: macros), as the
    real API returns a rendered representation, not the storage source."""
    paras = [p for p in content.split("\n\n") if p.strip()] or [content]
    body = "".join(f'<p class="auto-cursor-target">{escape(p)}</p>' for p in paras)
    return f'<div class="contentLayout2"><div class="columnLayout single">{body}</div></div>'


def _export_view(content: str) -> str:
    """Rendered ``export_view`` HTML — the real API's export-oriented rendering (same content
    as ``view``, without editor-only attributes like ``auto-cursor-target``)."""
    paras = [p for p in content.split("\n\n") if p.strip()] or [content]
    body = "".join(f"<p>{escape(p)}</p>" for p in paras)
    return f'<div class="contentLayout2"><div class="columnLayout single">{body}</div></div>'


def _space_container_for_key(conn, space_key: str) -> str | None:
    """Resolve a Confluence ``spaceKey`` to its backing container name. The mock models a space
    by its corpus name, so both the synthesized key (``synth.confluence_space_key(name)``, the
    hash-suffixed value ``/space`` advertises) and the literal container name (e.g. ``"handbook"``,
    a legitimate natural key) resolve. Anything else is unresolvable -> ``None`` (never a silent
    fall-through to "no filter": callers must treat ``None`` as "0 results", not "everything")."""
    for r in store.list_containers(conn, "confluence"):
        if space_key == synth.confluence_space_key(r["name"]) or space_key == r["name"]:
            return r["name"]
    return None


@router.get("/wiki/rest/api/space", response_model=ConfluenceResults)
async def confluence_spaces(request: Request):
    conn = auth.conn(request)
    _require(request)
    results = []
    for r in store.list_containers(conn, "confluence"):
        key = synth.confluence_space_key(r["name"])
        results.append(
            {
                "id": synth.github_user_id(r["name"]),
                "key": key,
                "name": r["name"],
                "type": "global",
                "_links": {"webui": f"/spaces/{key}"},
            }
        )
    return {"results": results, "start": 0, "limit": len(results), "size": len(results)}


@router.get("/wiki/rest/api/space/{key}/permission")
async def confluence_space_permission(key: str, request: Request):
    conn = auth.conn(request)
    _require(request)
    container = _space_container_for_key(conn, key)
    perms = []
    if container:
        emails = store.container_member_emails(conn, "confluence", container)
        if emails is None:
            perms.append(
                {
                    "operation": {"operation": "read", "targetType": "space"},
                    "subjects": {"user": {"results": []}},
                    "anonymousAccess": True,
                }
            )
        else:
            perms.append(
                {
                    "operation": {"operation": "read", "targetType": "space"},
                    "subjects": {
                        "user": {
                            "results": [
                                {"accountId": synth.atlassian_account_id(e), "email": e}
                                for e in sorted(emails)
                            ]
                        }
                    },
                }
            )
    return {"results": perms}


@router.get("/wiki/rest/api/space/{key}", response_model=ConfluencePage, openapi_extra=_P_EXPAND)
async def confluence_space_get(key: str, request: Request):
    """Single-space fetch (atlassian-python-api's ``get_space`` / mcp-atlassian result enrichment).
    404s (Atlassian-shaped) for an unknown key."""
    conn = auth.conn(request)
    _require(request)
    container = _space_container_for_key(conn, key)
    if container is None:
        raise HTTPException(status_code=404, detail="No space with the given key exists")
    space = {
        "id": synth.github_user_id(container),
        "key": key,
        "name": container,
        "type": "global",
        "status": "current",
        "_links": {"webui": f"/spaces/{key}"},
    }
    if "description" in request.query_params.get("expand", ""):
        space["description"] = {"plain": {"value": f"{container} space", "representation": "plain"}}
    return space


@router.get("/wiki/rest/api/search", response_model=ConfluenceResults, openapi_extra=_P_CQL)
async def confluence_cql_search(request: Request):
    """CQL search used by Confluence clients (e.g. mcp-atlassian). We parse the
    `~ "term"` operand and do a keyword search over the ACL-visible corpus."""
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    cql = request.query_params.get("cql", "")
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
    limit = _int(request.query_params.get("limit"), 25)
    start = _int(request.query_params.get("start"), 0)

    # fetch the full ACL-visible match set, filter by the clauses, then paginate — so
    # totalSize reflects the true match count (not just the returned page).
    everything = store.search_documents(conn, term, "confluence", ids, limit=100_000, offset=0)

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
    rows = matched[start : start + limit]
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
                    r["updated_ts"] or r["created_ts"] or synth.epoch(r["doc_id"])
                ),
            }
        )
    links = {"base": f"{_site(request)}/wiki"}
    if start + limit < total:
        params = {"cql": cql, "start": start + limit, "limit": limit}
        links["next"] = "/rest/api/search?" + "&".join(f"{k}={v}" for k, v in params.items())
    return {
        "results": results,
        "start": start,
        "limit": limit,
        "size": len(results),
        "totalSize": total,
        "cqlQuery": cql,
        "searchDuration": 5,
        "_links": links,
    }


@router.get("/wiki/rest/api/content", response_model=ConfluenceResults, openapi_extra=_P_CONTENT)
async def confluence_content_list(request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    expand = request.query_params.get("expand", "")
    space_key = request.query_params.get("spaceKey")
    limit = _int(request.query_params.get("limit"), 25)
    start = _int(request.query_params.get("start"), 0)
    if space_key:
        container = _space_container_for_key(conn, space_key)
        if container is None:
            # spaceKey given but unresolvable: real Confluence returns zero matches, never the
            # unfiltered corpus — do not let this collapse to the "no spaceKey" (container=None) case.
            links = {"base": f"{_site(request)}/wiki"}
            return {"results": [], "start": start, "limit": limit, "size": 0, "_links": links}
    else:
        container = None
    total = store.count_documents(conn, "confluence", container, ids)
    rows = store.list_documents(conn, "confluence", container, ids, limit=limit, offset=start)
    results = [_confluence_page(conn, request, r, expand) for r in rows]
    params = {"type": "page"}
    if space_key:
        params["spaceKey"] = space_key
    if expand:
        params["expand"] = expand
    nxt = confluence_next_link("/wiki/rest/api/content", params, start, limit, len(rows), total)
    links = {"base": f"{_site(request)}/wiki"}
    if nxt:
        links["next"] = nxt
    return {"results": results, "start": start, "limit": limit, "size": len(rows), "_links": links}


@router.get(
    "/wiki/rest/api/content/{content_id}", response_model=ConfluencePage, openapi_extra=_P_EXPAND
)
async def confluence_content_get(content_id: int, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    doc_id = request.app.state.index["confluence"].get(content_id)
    if doc_id is None:
        raise HTTPException(status_code=404, detail="No content found with id")
    row = store.get_document(conn, "confluence", doc_id, visible_ids=ids)
    if row is None:
        raise HTTPException(status_code=404, detail="No content found with id")
    return _confluence_page(conn, request, row, request.query_params.get("expand", "body.storage"))


def _confluence_doc_id(request: Request, content_id: int) -> str | None:
    return request.app.state.index["confluence"].get(content_id)


@router.get(
    "/wiki/rest/api/content/{content_id}/child/page",
    response_model=ConfluenceResults,
    openapi_extra=_P_EXPAND,
)
async def confluence_child_pages(content_id: int, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    doc_id = _confluence_doc_id(request, content_id)
    if doc_id is None:
        raise HTTPException(status_code=404, detail="No content found with id")
    expand = request.query_params.get("expand", "")
    kids = store.children(conn, "confluence", doc_id, visible_ids=ids)
    results = [_confluence_page(conn, request, k, expand) for k in kids]
    return {
        "results": results,
        "start": 0,
        "limit": len(results),
        "size": len(results),
        "_links": {"base": f"{_site(request)}/wiki"},
    }


@router.get("/wiki/rest/api/content/{content_id}/child/comment")
async def confluence_comments(content_id: int, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    doc_id = _confluence_doc_id(request, content_id)
    if doc_id is None or store.get_document(conn, "confluence", doc_id, visible_ids=ids) is None:
        raise HTTPException(status_code=404, detail="No content found with id")
    results = []
    for c in store.doc_comments(conn, "confluence", doc_id):
        ts = c["created_ts"] or synth.epoch(c["id"])
        author = c["author_email"] or "unknown"
        results.append(
            {
                "id": c["id"],
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
                    "by": _conf_user(author),
                    "minorEdit": False,
                    "message": "",
                },
                "extensions": {"location": "footer"},
                "_links": {"webui": f"/spaces/x/pages/{content_id}?focusedCommentId={c['id']}"},
            }
        )
    return {"results": results, "start": 0, "limit": len(results), "size": len(results)}


@router.get("/wiki/rest/api/content/{content_id}/label")
async def confluence_labels(content_id: int, request: Request):
    conn = auth.conn(request)
    caller = _require(request)
    ids = auth.visible_ids(request, caller)
    doc_id = _confluence_doc_id(request, content_id)
    row = store.get_document(conn, "confluence", doc_id, visible_ids=ids) if doc_id else None
    if row is None:
        raise HTTPException(status_code=404, detail="No content found with id")
    labels = store.jcol(row, "labels")
    results = [
        {"prefix": "global", "name": lbl, "id": str(synth.confluence_id(lbl)), "label": lbl}
        for lbl in labels
    ]
    return {"results": results, "start": 0, "limit": 200, "size": len(results)}


@router.get("/wiki/rest/api/content/{content_id}/restriction/byOperation")
async def confluence_restrictions(content_id: int, request: Request):
    conn = auth.conn(request)
    _require(request)
    doc_id = request.app.state.index["confluence"].get(content_id)
    if doc_id is None:
        raise HTTPException(status_code=404, detail="No content found with id")
    emails = store.doc_member_emails(conn, doc_id)
    users = [] if emails is None else [_conf_user(e) for e in sorted(emails)]

    def _op(name):
        return {
            "operation": name,
            "restrictions": {
                "user": {"results": users, "start": 0, "limit": 200, "size": len(users)},
                "group": {"results": [], "start": 0, "limit": 200, "size": 0},
            },
            "_expandable": {"content": f"/rest/api/content/{content_id}"},
        }

    return {"read": _op("read"), "update": _op("update")}


def _conf_user(email: str) -> dict:
    aid = synth.atlassian_account_id(email or "unknown")
    return {
        "type": "known",
        "accountId": aid,
        "accountType": "atlassian",
        "email": email,
        "publicName": (email or "unknown").split("@")[0],
        "displayName": (email or "unknown").split("@")[0].replace(".", " ").title(),
        "profilePicture": {
            "path": f"/wiki/aa-avatar/{aid}",
            "width": 48,
            "height": 48,
            "isDefault": False,
        },
    }


def _confluence_page(conn, request: Request, row, expand: str) -> dict:
    created = row["created_ts"] or synth.epoch(row["doc_id"])
    updated = row["updated_ts"] or created
    cid = synth.confluence_id(row["doc_id"])
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
            "createdBy": _conf_user(author),
            "lastUpdated": {
                "when": synth.rfc3339_millis(updated),
                "by": _conf_user(author),
                "number": vnum,
            },
        }
    if "version" in expand:
        page["version"] = {
            "number": vnum,
            "when": synth.rfc3339_millis(updated),
            "by": _conf_user(author),
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
            pcid = synth.confluence_id(prow["doc_id"])
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


def _int(v, default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except (ValueError, TypeError):
        return default
