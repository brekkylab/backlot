"""Bind ``linear.graphql`` to :mod:`app.store`.

Every resolver returns plain dicts and lets graphql-core's default resolver pick the selected
keys off them, so a field the client didn't ask for costs nothing to have built. The four that
*are* bound explicitly (``Team.issues``, ``Issue.comments``, ``Issue.labels``, and the Query
roots) are the ones that take arguments and hit the DB.

Three things are worth knowing before reading further:

- **Nulls are honest.** The SDL declares everything ``@linear/sdk``'s generated documents select,
  which is far more than a document corpus can back. Anything the mock has no data for resolves
  to ``null`` / ``[]`` / a documented default rather than to an invented value. See the SDL
  header for the split.
- **ACL comes from the context, not from here.** ``info.context["visible_ids"]`` is threaded into
  every store call, so a resolver never makes an access decision of its own.
- **Cursors are the repo's opaque offset cursor** (``app.pagination``), the same token every
  other source's page uses; Linear's cursors are opaque to clients too.
"""
from __future__ import annotations

from graphql import GraphQLError

from app import pagination, store, synth
from app.graphql.linear_filters import compile_comment_filter, compile_issue_filter

# Linear's own page defaults: 50 per page, hard-capped at 250.
PAGE_DEFAULT = 50
PAGE_MAX = 250


def _ctx(info):
    return info.context


def _org(info) -> str:
    """The workspace slug, which is what a Linear URL is keyed on."""
    return _ctx(info).get("org") or "org"


def _org_domain(info) -> str:
    return _ctx(info).get("org_domain") or "example.com"


# --- pagination -------------------------------------------------------------------

def _slice(first, after, last, before) -> tuple[int, int, bool]:
    """Relay ``first``/``after`` (forward) or ``last``/``before`` (backward) -> ``(offset, limit,
    backward)``.

    Backward paging is real in Linear, and with an offset cursor it is cheap: ``before`` is the
    offset of the first row the caller has already seen, so the page is the ``last`` rows that
    end just before it. Asking for both directions at once is a client bug, and the spec says to
    reject it rather than guess."""
    if first is not None and last is not None:
        raise GraphQLError("passing both `first` and `last` is not supported")
    if last is not None or (before is not None and first is None):
        limit = pagination.clamp_limit(last, PAGE_DEFAULT, PAGE_MAX)
        end = pagination.decode_cursor(before) if before else None
        offset = max(0, (end - limit)) if end is not None else 0
        if end is not None:
            limit = min(limit, end)
        return offset, limit, True
    return (pagination.decode_cursor(after) if after else 0,
            pagination.clamp_limit(first, PAGE_DEFAULT, PAGE_MAX), False)


def _connection(nodes: list, offset: int, has_next: bool) -> dict:
    """A Relay connection page. ``endCursor`` is the offset the next page starts at, which is
    exactly what ``after`` consumes, so a client can round-trip it without interpreting it.

    ``has_next`` comes from a limit+1 probe, NOT from a COUNT of the whole result set. The
    connection types this schema serves expose no ``totalCount`` — `@linear/sdk`'s fragments do
    not select one — so a COUNT would be a full scan computed only to derive a boolean. On the
    35k-issue bench corpus that doubled the cost of every filtered query (a `labels.some` filter
    went 37ms -> 19ms when the count came out).
    """
    end = offset + len(nodes)
    return {
        "nodes": nodes,
        "pageInfo": {
            "hasNextPage": has_next,
            "hasPreviousPage": offset > 0,
            "startCursor": pagination.encode_cursor(offset) if nodes else None,
            "endCursor": pagination.encode_cursor(end) if nodes else None,
        },
    }


def _page(rows: list, limit: int) -> tuple[list, bool]:
    """Split a limit+1 fetch into (page, has_next). Reading one row past the page is what makes
    ``hasNextPage`` free — the extra row is discarded, never served."""
    return (rows[:limit], True) if len(rows) > limit else (rows, False)


# --- shared shapes ------------------------------------------------------------------

def _ts(value) -> str | None:
    return synth.rfc3339(value) if value is not None else None


def _user(email: str | None, display: str | None, info) -> dict | None:
    """A ``User``. Linear requires 21 of its fields to be non-null, so each gets a value derived
    from the identity rather than left absent. ``isMe`` is genuinely computed — it is the one
    field whose answer depends on who is asking."""
    if not email and not display:
        return None
    email = email or ""
    name = display or (email.split("@", 1)[0].replace(".", " ").title() if email else "Unknown")
    initials = "".join(p[0].upper() for p in name.split()[:2]) or "?"
    handle = email.split("@", 1)[0] if email else name.lower().replace(" ", "")
    caller = _ctx(info).get("caller_email")
    return {
        "id": synth.linear_user_id(email or name), "name": name, "displayName": handle,
        "email": email, "initials": initials, "url": f"https://linear.app/{_org(info)}/profiles/{handle}",
        "active": True, "isAssignable": True, "guest": False, "admin": False, "owner": False,
        "app": False, "isMentionable": True, "isMe": bool(caller and caller == email),
        "supportsAgentSessions": False, "canAccessAnyPublicTeam": True,
        "createdIssueCount": 0, "avatarBackgroundColor": "#5e6ad2",
        "inviteHash": synth.hnum(email or name, 0, 12).__format__("012x"),
        "createdAt": synth.rfc3339(synth.epoch(email or name)),
        "updatedAt": synth.rfc3339(synth.epoch(email or name)),
        "description": None, "avatarUrl": None, "statusUntilAt": None, "statusEmoji": None,
        "lastSeen": None, "timezone": "Etc/UTC", "disableReason": None, "statusLabel": None,
        "archivedAt": None, "gitHubUserId": None, "title": None, "calendarHash": None,
    }


def _state(name: str | None, team: str, info) -> dict:
    """``WorkflowState`` is non-null on an issue, so a row with no recorded state still gets one —
    "Todo", Linear's own bucket for "created but not begun". States are per-team in Linear, so
    both the id and the back-reference carry the team."""
    name = name or "Todo"
    created = synth.rfc3339(synth.epoch(f"linear-state:{team}:{name}"))
    return {"id": synth.linear_state_id(name, team), "name": name,
            "type": synth.linear_state_type(name), "color": synth.linear_state_color(name),
            "position": 0.0, "description": None,
            "createdAt": created, "updatedAt": created, "archivedAt": None,
            "inheritedFrom": None, "team": _team(team, info)}


def _project(name: str | None, info) -> dict | None:
    """A ``Project``. The corpus knows a project only by name, so the 26 non-null fields the SDK's
    fragment demands take neutral values — empty history arrays, zero progress/scope — rather than
    invented burndown data. `state` is Linear's project state string; "started" is the only claim
    the mock can make about a project it sees issues in."""
    if not name:
        return None
    slug = synth.hnum(name, 0, 8).__format__("08x")
    created = synth.rfc3339(synth.epoch("linear-project:" + name))
    return {
        "id": synth.linear_project_id(name), "name": name, "slugId": slug,
        "url": f"https://linear.app/{_org(info)}/project/{slug}",
        "description": "", "content": None, "color": "#5e6ad2", "icon": None,
        "state": "started", "status": {"id": synth.linear_project_id("status:" + name)},
        "priority": 0.0, "priorityLabel": "No priority",
        "progress": 0.0, "scope": 0.0, "sortOrder": 0.0, "prioritySortOrder": 0.0,
        "labelIds": [], "issueCountHistory": [], "completedIssueCountHistory": [],
        "scopeHistory": [], "completedScopeHistory": [], "inProgressScopeHistory": [],
        "frequencyResolution": "week",
        "slackIssueComments": False, "slackNewIssue": False, "slackIssueStatuses": False,
        "createdAt": created, "updatedAt": created,
        "trashed": None, "archivedAt": None, "autoArchivedAt": None, "canceledAt": None,
        "completedAt": None, "startedAt": None, "healthUpdatedAt": None, "health": None,
        "targetDate": None, "startDate": None, "targetDateResolution": None,
        "startDateResolution": None, "updateRemindersDay": None, "updateRemindersHour": None,
        "updateReminderFrequency": None, "updateReminderFrequencyInWeeks": None,
        "projectUpdateRemindersPausedUntilAt": None,
        "slackChannelId": None, "microsoftTeamsChannelId": None,
        "integrationsSettings": None, "documentContent": None, "syncedWith": None,
        "convertedFromIssue": None, "lastAppliedTemplate": None, "lastUpdate": None,
        "creator": None, "lead": None, "favorite": None,
    }


def _cycle(name: str | None, team: str, info) -> dict | None:
    """A ``Cycle``. ``startsAt``/``endsAt`` are non-null in Linear, and the corpus's cycle names
    are sprint labels ("2025-W08", "Cycle 41") with no dates attached, so the window is derived
    deterministically from the name — stable across calls, and never presented as measured."""
    if not name:
        return None
    if not team:
        team = ""
    start = synth.epoch("linear-cycle:" + name)
    created = synth.rfc3339(start)
    return {
        "id": synth.linear_cycle_id(name, team), "name": name,
        "number": float(synth.linear_issue_number(name) or synth.hnum(name, 0, 4) % 200),
        "startsAt": created, "endsAt": synth.rfc3339(start + 14 * 86400),
        "createdAt": created, "updatedAt": created,
        "progress": 0.0, "issueCountHistory": [], "completedIssueCountHistory": [],
        "scopeHistory": [], "completedScopeHistory": [], "inProgressScopeHistory": [],
        "isActive": False, "isFuture": False, "isPast": False, "isPrevious": False,
        "isNext": False,
        "description": None, "completedAt": None, "autoArchivedAt": None, "archivedAt": None,
        "inheritedFrom": None, "team": _team(team, info),
    }


def _labels(row) -> list[str]:
    return [str(x) for x in store.jcol(row, "labels") if str(x).strip()]


def _label(name: str, ts: str) -> dict:
    return {"id": synth.linear_label_id(name), "name": name, "color": "#bec2c8",
            "isGroup": False, "createdAt": ts, "updatedAt": ts,
            "description": None, "archivedAt": None, "lastAppliedAt": None,
            "inheritedFrom": None, "parent": None, "team": None, "creator": None,
            "retiredBy": None}


def _label_nodes(row) -> list[dict]:
    ts = synth.rfc3339(row["created_ts"])
    return [_label(name, ts) for name in _labels(row)]


def _team_counts(info) -> dict[str, int]:
    """team -> visible issue count, computed at most once per request. ``Team.issueCount`` is
    non-null, so it cannot be left absent; a per-request cache means a page of 50 issues that all
    select ``team { issueCount }`` costs one grouped scan, not fifty COUNT(*)s."""
    ctx = _ctx(info)
    counts = ctx.get("_team_counts")
    if counts is None:
        counts = store.linear_team_issue_counts(ctx["conn"], visible_ids=ctx["visible_ids"])
        ctx["_team_counts"] = counts
    return counts


def resolve_team_issue_count(team, info) -> int:
    return _team_counts(info).get(team["_container"], 0)


def _team(container: str, info) -> dict:
    """A ``Team``. 42 of its fields are non-null in the SDK's fragment; the ones the mock cannot
    know take Linear's own product defaults (cycles off, 2-week duration, estimate scale
    ``notUsed``) rather than zero values that would read as configured."""
    key = synth.linear_team_key(container)
    created = synth.rfc3339(synth.epoch("linear-team:" + container))
    return {
        "id": synth.linear_team_id(container), "key": key, "name": container,
        "displayName": container, "createdAt": created, "updatedAt": created,
        "timezone": "Etc/UTC", "visibility": "public", "private": False,
        "inviteHash": synth.hnum("linear-team:" + container, 0, 12).__format__("012x"),
        "cyclesEnabled": False, "cycleDuration": 2, "cycleCooldownTime": 0, "cycleStartDay": 1.0,
        "cycleIssueAutoAssignCompleted": False, "cycleIssueAutoAssignStarted": False,
        "cycleLockToActive": False, "upcomingCycleCount": 0.0,
        "cycleCalenderUrl": f"https://linear.app/{_org(info)}/team/{key}/cycles.ics",
        "autoArchivePeriod": 6.0, "autoClosePeriod": None, "autoCloseStateId": None,
        "securitySettings": {}, "issueEstimationType": "notUsed", "defaultIssueEstimate": 0.0,
        "issueEstimationExtended": False, "issueEstimationAllowZero": False,
        "inheritIssueEstimation": True, "inheritWorkflowStatuses": False,
        "setIssueSortOrderOnStateChange": "first", "issueSortOrderDefaultToBottom": False,
        "issueOrderingNoPriorityFirst": False, "requirePriorityToLeaveTriage": False,
        "triageEnabled": False, "groupIssueHistory": True, "ledInitiativeCount": 0.0,
        "aiDiscussionSummariesEnabled": False, "aiThreadSummariesEnabled": False,
        "slackIssueComments": False, "slackNewIssue": False, "slackIssueStatuses": False,
        "scimManaged": False, "scimGroupName": None, "icon": None, "color": None,
        "description": None, "archivedAt": None, "retiredAt": None, "allMembersCanJoin": None,
        "autoCloseChildIssues": None, "autoCloseParentIssues": None,
        "defaultTemplateForMembersId": None, "defaultTemplateForNonMembersId": None,
        "_container": container,   # not a schema field: how Team.issues knows what to query
    }


def _issue(row, info) -> dict:
    """One ``linear_issues`` row as an ``Issue``.

    The stubs are deliberate and listed in the SDL header: reactions, SLA timestamps, board /
    sort orders, bot actors and shared access are declared because `@linear/sdk`'s fragment
    selects them, and resolve empty because a document corpus has nothing behind them."""
    identifier = row["identifier"] or synth.linear_identifier(
        row["doc_id"], synth.linear_team_key(row["team"]))
    title = row["title"] or ""
    created = row["created_ts"]
    # updatedAt is non-null in Linear; an issue with no recorded edit reports its creation time,
    # which is what Linear itself shows for a never-edited issue.
    updated = row["updated_ts"] if row["updated_ts"] is not None else created
    return {
        "id": synth.linear_id(row["doc_id"]),
        "identifier": identifier,
        "number": float(synth.linear_issue_number(identifier)),
        "title": title,
        # `content` is the doc's full retrieval text (the bench concatenates description +
        # comments + whatever else its content_field_names names), which is exactly what an
        # issue's markdown description is.
        "description": row["content"],
        "url": synth.linear_url(identifier, title, _org(info)),
        "branchName": row["branch_name"] or synth.linear_branch_name(
            identifier, title, row["assignee_email"]),
        "priority": float(row["priority"] if row["priority"] is not None else 0),
        "priorityLabel": synth.linear_priority_label(row["priority"]),
        "estimate": float(row["estimate"]) if row["estimate"] is not None else None,
        "dueDate": row["due_date"],
        "createdAt": synth.rfc3339(created), "updatedAt": synth.rfc3339(updated),
        "archivedAt": _ts(row["archived_ts"]),
        "autoArchivedAt": _ts(row["auto_archived_ts"]),
        "autoClosedAt": _ts(row["auto_closed_ts"]),
        "canceledAt": _ts(row["canceled_ts"]),
        "completedAt": _ts(row["completed_ts"]),
        "startedAt": _ts(row["started_ts"]),
        "labelIds": [synth.linear_label_id(n) for n in _labels(row)],
        "state": _state(row["state"], row["team"], info),
        "team": _team(row["team"], info),
        "project": _project(row["project"], info),
        "cycle": _cycle(row["cycle"], row["team"], info),
        "creator": _user(row["author_email"], row["owner_display"], info),
        "assignee": _user(row["assignee_email"], row["assignee_display"], info),
        # --- declared by the SDK's fragment, no corpus data behind them -------------
        "trashed": None, "reactionData": {}, "reactions": [], "integrationSourceType": None,
        "previousIdentifiers": [], "customerTicketCount": 0.0, "inheritsSharedAccess": False,
        "boardOrder": 0.0, "sortOrder": 0.0, "prioritySortOrder": 0.0, "subIssueSortOrder": None,
        "startedTriageAt": None, "triagedAt": None, "addedToCycleAt": None,
        "addedToProjectAt": None, "addedToTeamAt": None, "snoozedUntilAt": None,
        "slaStartedAt": None, "slaBreachesAt": None, "slaHighRiskAt": None,
        "slaMediumRiskAt": None, "slaType": None,
        # NOT null, and not a stub by choice: `@linear/sdk` builds `new IssueSharedAccess(...)`
        # unconditionally, so a null here is a TypeError inside the client rather than an empty
        # field. The values are also simply true — nothing in a document corpus is shared with an
        # external viewer — so the honest answer and the working one coincide.
        "sharedAccess": {"isShared": False, "sharedWithCount": 0.0, "sharedWithUsers": [],
                         "viewerHasOnlySharedAccess": False, "disallowedIssueFields": []},
        "delegate": None, "botActor": None, "sourceComment": None,
        "syncedWith": None, "externalUserCreator": None, "asksExternalUserRequester": None,
        "asksRequester": None, "lastAppliedTemplate": None, "parent": None,
        "projectMilestone": None, "recurringIssueTemplate": None, "snoozedBy": None,
        "favorite": None,
        "_row": row,   # not a schema field: how Issue.comments / Issue.labels reach the row
    }


def _comment(row, info) -> dict:
    ts = synth.rfc3339(row["created_ts"])
    return {
        "id": synth.linear_comment_id(row["id"]), "body": row["body"],
        "createdAt": ts, "updatedAt": ts,
        "url": f"https://linear.app/{_org(info)}/issue/#comment-{synth.linear_comment_id(row['id'])}",
        "reactionData": {}, "reactions": [],
        "user": _user(row["author_email"], None, info),
        "issueId": synth.linear_id(row["doc_id"]),
        "quotedText": None, "archivedAt": None, "editedAt": None, "resolvedAt": None,
        "resolvingCommentId": None, "documentContentId": None, "initiativeId": None,
        "initiativeUpdateId": None, "parentId": None, "projectId": None, "projectUpdateId": None,
        "agentSession": None, "botActor": None, "resolvingComment": None, "documentContent": None,
        "syncedWith": None, "externalThread": None, "externalUser": None, "initiative": None,
        "initiativeUpdate": None, "issue": None, "parent": None, "project": None,
        "projectUpdate": None, "resolvingUser": None,
    }


# --- Query roots ---------------------------------------------------------------------

def _resolve_issue_ids(info, flt):
    """Rewrite an ``IssueFilter``'s ``id`` comparator from Linear UUIDs to doc_ids, since the
    UUID is derived from the doc_id and only the app index can invert it. An unknown UUID maps
    to a sentinel that matches nothing, so it filters everything out instead of being dropped."""
    if not isinstance(flt, dict):
        return flt
    out = {}
    for k, v in flt.items():
        if k == "id" and isinstance(v, dict):
            idx = info.context.get("index", {})
            out[k] = {op: ([idx.get(str(x), "\x00none") for x in val]
                           if isinstance(val, list) else idx.get(str(val), "\x00none"))
                      for op, val in v.items()}
        elif k in ("and", "or") and isinstance(v, list):
            out[k] = [_resolve_issue_ids(info, s) for s in v]
        else:
            out[k] = v
    return out


def _issue_page(info, *, team=None, first=None, after=None, last=None, before=None,
                filter=None, orderBy=None, **_ignored) -> dict:
    ctx = _ctx(info)
    conn, visible = ctx["conn"], ctx["visible_ids"]
    offset, limit, _backward = _slice(first, after, last, before)
    prefilter = compile_issue_filter(conn, _resolve_issue_ids(info, filter))
    rows = store.list_linear_issues(conn, team, visible_ids=visible, limit=limit + 1,
                                    offset=offset, order_by=orderBy, prefilter=prefilter)
    rows, has_next = _page(rows, limit)
    return _connection([_issue(r, info) for r in rows], offset, has_next)


def resolve_issues(_root, info, **kwargs) -> dict:
    return _issue_page(info, **kwargs)


def resolve_issue(_root, info, id):
    """``issue(id:)`` takes a UUID or a human identifier, as the real API does. Linear declares
    this non-null, so a miss is an error rather than a null — the same thing the real API does
    ("Entity not found")."""
    ctx = _ctx(info)
    conn, visible = ctx["conn"], ctx["visible_ids"]
    doc_id = ctx.get("index", {}).get(str(id))
    row = store.get_document(conn, "linear", doc_id, visible_ids=visible) if doc_id else None
    if row is None:
        row = store.linear_issue_by_identifier(conn, str(id), visible_ids=visible)
    if row is None:
        raise GraphQLError(f"Entity not found: Issue - Could not find referenced Issue. id={id}")
    return _issue(row, info)


def resolve_team(_root, info, id):
    """``team(id:)`` takes a team UUID or its key (``ENG``)."""
    ctx = _ctx(info)
    container = ctx.get("team_index", {}).get(str(id))
    if container is None:
        raise GraphQLError(f"Entity not found: Team - Could not find referenced Team. id={id}")
    return _team(container, info)


def resolve_teams(_root, info, first=None, after=None, last=None, before=None, **_ignored) -> dict:
    ctx = _ctx(info)
    offset, limit, _b = _slice(first, after, last, before)
    # A team the caller can see no issue in is not a team they can see — same rule the Slack
    # router applies to channels, and it keeps `teams` consistent with what `team.issues` returns.
    # An EXISTS probe per team, NOT the grouped count: `issueCount` is a bound field that only
    # runs when selected, and computing every team's total just to test visibility cost 22ms of
    # ACL-filtered scan on the bench corpus for a question `LIMIT 1` answers.
    names = [r["name"] for r in store.list_containers(ctx["conn"], "linear")
             if ctx["visible_ids"] is None
             or store.linear_team_has_visible(ctx["conn"], r["name"], ctx["visible_ids"])]
    page = names[offset:offset + limit]
    return _connection([_team(n, info) for n in page], offset, offset + limit < len(names))


def resolve_comments(_root, info, first=None, after=None, last=None, before=None,
                     filter=None, **_ignored) -> dict:
    ctx = _ctx(info)
    conn, visible = ctx["conn"], ctx["visible_ids"]
    offset, limit, _b = _slice(first, after, last, before)
    prefilter = compile_comment_filter(conn, filter)
    rows = store.list_linear_comments(conn, visible_ids=visible, limit=limit + 1, offset=offset,
                                      prefilter=prefilter)
    rows, has_next = _page(rows, limit)
    return _connection([_comment(r, info) for r in rows], offset, has_next)


def resolve_users(_root, info, first=None, after=None, last=None, before=None, **_ignored) -> dict:
    ctx = _ctx(info)
    offset, limit, _b = _slice(first, after, last, before)
    rows = store.list_users(ctx["conn"])
    page = rows[offset:offset + limit]
    return _connection([_user(r["email"], r["display_name"], info) for r in page],
                       offset, offset + limit < len(rows))


# --- by-id roots for the SDK's lazy relation accessors ------------------------------------
# `await issue.state` does NOT read the state off the issue the SDK already has — it fires
# `workflowState(id:)`. Each id is a one-way hash of a name, so each root reads the reverse map
# the app built at startup (see app.main._build_index). All five are declared non-null in Linear,
# so a miss is an "Entity not found" error, matching the real API.

def _by_id(info, index_key: str, id_value, entity: str):
    found = _ctx(info).get(index_key, {}).get(str(id_value))
    if found is None:
        raise GraphQLError(
            f"Entity not found: {entity} - Could not find referenced {entity}. id={id_value}")
    return found


def resolve_user(_root, info, id) -> dict:
    email, display = _by_id(info, "user_index", id, "User")
    return _user(email, display, info)


def resolve_workflow_state(_root, info, id) -> dict:
    team, name = _by_id(info, "state_index", id, "WorkflowState")
    return _state(name, team, info)


def resolve_project(_root, info, id) -> dict:
    return _project(_by_id(info, "project_index", id, "Project"), info)


def resolve_cycle(_root, info, id) -> dict:
    team, name = _by_id(info, "cycle_index", id, "Cycle")
    return _cycle(name, team, info)


def resolve_issue_label(_root, info, id) -> dict:
    name = _by_id(info, "label_index", id, "IssueLabel")
    return _label(name, synth.rfc3339(synth.epoch("linear-label:" + name)))


def resolve_viewer(_root, info) -> dict:
    """The authenticated identity. The admin/service token is not a person in the corpus, so it
    reports as an app user — true, and it keeps the non-null contract."""
    ctx = _ctx(info)
    email = ctx.get("caller_email")
    if not email:
        who = _user("service@" + _org_domain(info), "Service Account", info)
        who["app"] = True
        who["admin"] = True
        return who
    row = store.get_user(ctx["conn"], email)
    return _user(email, row["display_name"] if row else None, info)


# --- relation fields that take arguments ------------------------------------------------

def resolve_team_issues(team, info, **kwargs) -> dict:
    return _issue_page(info, team=team["_container"], **kwargs)


def resolve_issue_comments(issue, info, first=None, after=None, last=None, before=None,
                           filter=None, **_ignored) -> dict:
    ctx = _ctx(info)
    conn, visible = ctx["conn"], ctx["visible_ids"]
    offset, limit, _b = _slice(first, after, last, before)
    doc_id = issue["_row"]["doc_id"]
    prefilter = compile_comment_filter(conn, filter)
    rows = store.list_linear_comments(conn, doc_id=doc_id, visible_ids=visible, limit=limit + 1,
                                      offset=offset, prefilter=prefilter)
    rows, has_next = _page(rows, limit)
    return _connection([_comment(r, info) for r in rows], offset, has_next)


def resolve_issue_labels(issue, info, first=None, after=None, last=None, before=None,
                         **_ignored) -> dict:
    """Labels are a JSON column on the issue, so the whole set is already in hand; the page is a
    slice of it rather than another query."""
    offset, limit, _b = _slice(first, after, last, before)
    nodes = _label_nodes(issue["_row"])
    return _connection(nodes[offset:offset + limit], offset, offset + limit < len(nodes))


RESOLVERS = {
    "Query": {
        "issue": resolve_issue,
        "issues": resolve_issues,
        "team": resolve_team,
        "teams": resolve_teams,
        "comments": resolve_comments,
        "users": resolve_users,
        "viewer": resolve_viewer,
        "user": resolve_user,
        "workflowState": resolve_workflow_state,
        "project": resolve_project,
        "issueLabel": resolve_issue_label,
        "cycle": resolve_cycle,
    },
    "Team": {"issues": resolve_team_issues, "issueCount": resolve_team_issue_count},
    "Issue": {"comments": resolve_issue_comments, "labels": resolve_issue_labels},
}


def build_engine():
    """Construct the Linear engine from the SDL next to this module."""
    from pathlib import Path

    from app.graphql import engine

    sdl = (Path(__file__).parent / "linear.graphql").read_text()
    return engine.Engine(sdl, RESOLVERS)

__all__ = ["RESOLVERS", "build_engine", "PAGE_DEFAULT", "PAGE_MAX"]
