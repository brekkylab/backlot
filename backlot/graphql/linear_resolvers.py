"""Bind ``linear.graphql`` to :mod:`backlot.store`.

Resolvers return plain dicts and let graphql-core's default resolver pick the selected keys off
them, so an unasked-for field costs nothing to build. Only the ones taking arguments and hitting
the DB are bound explicitly.

- **Nulls are honest.** The SDL declares everything ``@linear/sdk`` selects, which is more than a
  document corpus can back; anything unbacked resolves to ``null`` / ``[]`` / a documented default
  rather than an invented value.
- **ACL comes from the context**: ``info.context["visible_ids"]`` is threaded into every store
  call, so a resolver never makes an access decision of its own.
- **Cursors are the repo's opaque offset cursor** (``backlot.pagination``) — Linear's are opaque too.
"""

from __future__ import annotations

from graphql import GraphQLError

from backlot import pagination, store, synth
from backlot.graphql.linear_filters import compile_comment_filter, compile_issue_filter

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


def _slice(first, after, last, before) -> tuple[int | None, int, int]:
    """Relay ``first``/``after`` (forward) or ``last``/``before`` (backward) ->
    ``(offset, limit, floor)``.

    ``offset is None`` means "count back from the END of the result set" — the caller resolves it
    with :func:`_from_end` once it knows the total. That case is ``last:`` with no ``before:``,
    and it is why this returns an OPTIONAL offset rather than an int: an absent ``before`` read as
    offset 0 would serve the first n rows to a client asking for the last n.

    ``floor`` is the lower bound a from-the-end offset may not cross, so ``after`` still applies
    when combined with ``last`` (Relay applies ``after`` first, then takes the last n of the rest).
    Asking for both ``first`` and ``last`` is a client bug the spec says to reject."""
    if first is not None and last is not None:
        raise GraphQLError("passing both `first` and `last` is not supported")
    start = pagination.decode_cursor(after) if after else 0
    if last is not None or (before is not None and first is None):
        limit = pagination.clamp_limit(last, PAGE_DEFAULT, PAGE_MAX)
        if before is None:
            return None, limit, start  # resolved against the total by _from_end
        end = pagination.decode_cursor(before)
        return max(start, end - limit), min(limit, max(0, end - start)), start
    return start, pagination.clamp_limit(first, PAGE_DEFAULT, PAGE_MAX), start


def _from_end(offset: int | None, limit: int, floor: int, total: int) -> int:
    """Resolve a from-the-end slice now that the total is known."""
    return max(floor, total - limit) if offset is None else offset


def _connection(nodes: list, offset: int, has_next: bool) -> dict:
    """A Relay connection page. ``endCursor`` is the offset the next page starts at, which is
    exactly what ``after`` consumes, so a client can round-trip it without interpreting it.

    ``has_next`` comes from a limit+1 probe, NOT from a COUNT of the whole result set. The
    connection types this schema serves expose no ``totalCount`` — `@linear/sdk`'s fragments do
    not select one — so a COUNT would be a full scan run only to derive a boolean, and on the bench
    corpus that doubled the cost of every filtered query.
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


# --- in-memory filters ----------------------------------------------------------------
# `teams`, `users` and `Issue.labels` are served from small in-memory collections (three teams, a
# principal list, an issue's own JSON labels), so their filters are evaluated here rather than
# compiled to SQL the way `IssueFilter` is. They are evaluated AT ALL because the alternative —
# accepting a declared `filter:` and ignoring it — answers a narrowing query with the FULL set,
# which is the silent-wrong-answer this schema promises not to give.


def _match_string(value, spec: dict | None) -> bool:
    """A ``StringComparator`` against one Python value; mirrors the SQL comparator in
    backlot/graphql/linear_filters.py, operator for operator."""
    if not spec:
        return True
    v = "" if value is None else str(value)
    known = (
        "eq",
        "neq",
        "in",
        "nin",
        "contains",
        "containsIgnoreCase",
        "startsWith",
        "endsWith",
        "eqIgnoreCase",
        "neqIgnoreCase",
    )
    for op, raw in spec.items():
        if raw is None:
            continue
        if op not in known:
            raise GraphQLError(f"unsupported comparator {op!r}")
        if op == "eq" and v != raw:
            return False
        if op == "neq" and v == raw:
            return False
        if op == "in" and v not in raw:
            return False
        if op == "nin" and v in raw:
            return False
        if op == "contains" and str(raw) not in v:
            return False
        if op == "containsIgnoreCase" and str(raw).lower() not in v.lower():
            return False
        if op == "startsWith" and not v.startswith(str(raw)):
            return False
        if op == "endsWith" and not v.endswith(str(raw)):
            return False
        if op == "eqIgnoreCase" and v.lower() != str(raw).lower():
            return False
        if op == "neqIgnoreCase" and v.lower() == str(raw).lower():
            return False
    return True


def _match_fields(spec: dict | None, fields: dict) -> bool:
    """A filter object whose keys map to already-computed values, plus ``and`` / ``or``."""
    if not spec:
        return True
    for key, sub in spec.items():
        if sub is None:
            continue
        if key == "and":
            if not all(_match_fields(x, fields) for x in sub):
                return False
        elif key == "or":
            if not any(_match_fields(x, fields) for x in sub):
                return False
        elif key in fields:
            if not _match_string(fields[key], sub):
                return False
        else:
            raise GraphQLError(f"unsupported filter field {key!r}")
    return True


def _match_team(container: str, spec) -> bool:
    return _match_fields(
        spec,
        {
            "name": container,
            "key": synth.linear_team_key(container),
            "id": synth.linear_team_id(container),
        },
    )


def _match_user(row, spec) -> bool:
    email = row["email"]
    return _match_fields(
        spec,
        {
            "email": email,
            "name": row["display_name"],
            "displayName": (email or "").split("@", 1)[0],
            "id": synth.linear_user_id(email),
        },
    )


def _match_label(node: dict, spec) -> bool:
    return _match_fields(spec, {"name": node["name"]})


def _match_attachment(node: dict, spec) -> bool:
    return _match_fields(
        spec,
        {
            "id": node["id"],
            "title": node["title"],
            "url": node["url"],
            "subtitle": node["subtitle"],
            "sourceType": node["sourceType"],
        },
    )


def _match_release(node: dict, spec) -> bool:
    return _match_fields(spec, {"id": node["id"], "name": node["name"], "slugId": node["slugId"]})


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
        "id": synth.linear_user_id(email or name),
        "name": name,
        "displayName": handle,
        "email": email,
        "initials": initials,
        "url": f"https://linear.app/{_org(info)}/profiles/{handle}",
        "active": True,
        "isAssignable": True,
        "guest": False,
        "admin": False,
        "owner": False,
        "app": False,
        "isMentionable": True,
        "isMe": bool(caller and caller == email),
        "supportsAgentSessions": False,
        "canAccessAnyPublicTeam": True,
        "createdIssueCount": 0,
        "avatarBackgroundColor": "#5e6ad2",
        "inviteHash": synth.hnum(email or name, 0, 12).__format__("012x"),
        "createdAt": synth.rfc3339(synth.epoch(email or name)),
        "updatedAt": synth.rfc3339(synth.epoch(email or name)),
        "description": None,
        "avatarUrl": None,
        "statusUntilAt": None,
        "statusEmoji": None,
        "lastSeen": None,
        "timezone": "Etc/UTC",
        "disableReason": None,
        "statusLabel": None,
        "archivedAt": None,
        "gitHubUserId": None,
        "title": None,
        "calendarHash": None,
    }


def _state(name: str | None, team: str, info) -> dict:
    """``WorkflowState`` is non-null on an issue, so a row with no recorded state still gets one —
    "Todo", Linear's own bucket for "created but not begun". States are per-team in Linear, so
    both the id and the back-reference carry the team."""
    name = name or "Todo"
    created = synth.rfc3339(synth.epoch(f"linear-state:{team}:{name}"))
    return {
        "id": synth.linear_state_id(name, team),
        "name": name,
        "type": synth.linear_state_type(name),
        "color": synth.linear_state_color(name),
        "position": 0.0,
        "description": None,
        "createdAt": created,
        "updatedAt": created,
        "archivedAt": None,
        "inheritedFrom": None,
        "team": _team(team, info),
    }


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
        "id": synth.linear_project_id(name),
        "name": name,
        "slugId": slug,
        "url": f"https://linear.app/{_org(info)}/project/{slug}",
        "description": "",
        "content": None,
        "color": "#5e6ad2",
        "icon": None,
        "state": "started",
        "status": {"id": synth.linear_project_id("status:" + name)},
        "priority": 0.0,
        "priorityLabel": "No priority",
        "progress": 0.0,
        "scope": 0.0,
        "sortOrder": 0.0,
        "prioritySortOrder": 0.0,
        "labelIds": [],
        "issueCountHistory": [],
        "completedIssueCountHistory": [],
        "scopeHistory": [],
        "completedScopeHistory": [],
        "inProgressScopeHistory": [],
        "frequencyResolution": "week",
        "slackIssueComments": False,
        "slackNewIssue": False,
        "slackIssueStatuses": False,
        "createdAt": created,
        "updatedAt": created,
        "trashed": None,
        "archivedAt": None,
        "autoArchivedAt": None,
        "canceledAt": None,
        "completedAt": None,
        "startedAt": None,
        "healthUpdatedAt": None,
        "health": None,
        "targetDate": None,
        "startDate": None,
        "targetDateResolution": None,
        "startDateResolution": None,
        "updateRemindersDay": None,
        "updateRemindersHour": None,
        "updateReminderFrequency": None,
        "updateReminderFrequencyInWeeks": None,
        "projectUpdateRemindersPausedUntilAt": None,
        "slackChannelId": None,
        "microsoftTeamsChannelId": None,
        "integrationsSettings": None,
        "documentContent": None,
        "syncedWith": None,
        "convertedFromIssue": None,
        "lastAppliedTemplate": None,
        "lastUpdate": None,
        "creator": None,
        "lead": None,
        "favorite": None,
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
        "id": synth.linear_cycle_id(name, team),
        "name": name,
        "number": float(synth.linear_issue_number(name) or synth.hnum(name, 0, 4) % 200),
        "startsAt": created,
        "endsAt": synth.rfc3339(start + 14 * 86400),
        "createdAt": created,
        "updatedAt": created,
        "progress": 0.0,
        "issueCountHistory": [],
        "completedIssueCountHistory": [],
        "scopeHistory": [],
        "completedScopeHistory": [],
        "inProgressScopeHistory": [],
        "isActive": False,
        "isFuture": False,
        "isPast": False,
        "isPrevious": False,
        "isNext": False,
        "description": None,
        "completedAt": None,
        "autoArchivedAt": None,
        "archivedAt": None,
        "inheritedFrom": None,
        "team": _team(team, info),
    }


def _labels(row) -> list[str]:
    return [str(x) for x in store.jcol(row, "labels") if str(x).strip()]


def _label(name: str, ts: str) -> dict:
    return {
        "id": synth.linear_label_id(name),
        "name": name,
        "color": "#bec2c8",
        "isGroup": False,
        "createdAt": ts,
        "updatedAt": ts,
        "description": None,
        "archivedAt": None,
        "lastAppliedAt": None,
        "inheritedFrom": None,
        "parent": None,
        "team": None,
        "creator": None,
        "retiredBy": None,
    }


def _label_nodes(row) -> list[dict]:
    ts = synth.rfc3339(row["created_ts"])
    return [_label(name, ts) for name in _labels(row)]


def _attachment(row, info) -> dict:
    ts = synth.rfc3339(row["created_ts"])
    return {
        "id": synth.linear_attachment_id(row["id"]),
        "title": row["title"],
        "url": row["url"],
        "subtitle": row["subtitle"],
        "sourceType": row["source_type"],
        "metadata": {},
        "source": None,
        "bodyData": None,
        "groupBySource": False,
        "createdAt": ts,
        "updatedAt": ts,
        "archivedAt": None,
        "creator": None,
        "externalUserCreator": None,
        "originalIssue": None,
        "_doc_id": row["doc_id"],
    }


def _relation(row, info) -> dict:
    ts = synth.rfc3339(row["created_ts"])
    return {
        "id": synth.linear_relation_id(row["id"]),
        "type": row["type"],
        "createdAt": ts,
        "updatedAt": ts,
        "archivedAt": None,
        "_from": row["from_doc_id"],
        "_to": row["to_doc_id"],
    }


def _release(name: str | None, info) -> dict | None:
    """A ``Release``. The corpus knows a release only by name, so the fields Linear declares
    non-null take neutral values — empty progress, zero issueCount — rather than invented
    burndown data, exactly as ``_project`` does."""
    if not name:
        return None
    slug = synth.hnum("linear-release:" + name, 0, 8).__format__("08x")
    created = synth.rfc3339(synth.epoch("linear-release:" + name))
    return {
        "id": synth.linear_release_id(name),
        "name": name,
        "slugId": slug,
        "url": f"https://linear.app/{_org(info)}/release/{slug}",
        "issueCount": 0.0,
        "currentProgress": {},
        "progressHistory": {},
        "createdAt": created,
        "updatedAt": created,
        "description": None,
        "version": None,
        "commitSha": None,
        "trashed": None,
        "startDate": None,
        "targetDate": None,
        "startedAt": None,
        "completedAt": None,
        "canceledAt": None,
        "autoArchivedAt": None,
        "archivedAt": None,
        "releaseNotes": [],
        "stage": None,
        "pipeline": None,
        "creator": None,
    }


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
        "id": synth.linear_team_id(container),
        "key": key,
        "name": container,
        "displayName": container,
        "createdAt": created,
        "updatedAt": created,
        "timezone": "Etc/UTC",
        "visibility": "public",
        "private": False,
        "inviteHash": synth.hnum("linear-team:" + container, 0, 12).__format__("012x"),
        "cyclesEnabled": False,
        "cycleDuration": 2,
        "cycleCooldownTime": 0,
        "cycleStartDay": 1.0,
        "cycleIssueAutoAssignCompleted": False,
        "cycleIssueAutoAssignStarted": False,
        "cycleLockToActive": False,
        "upcomingCycleCount": 0.0,
        "cycleCalenderUrl": f"https://linear.app/{_org(info)}/team/{key}/cycles.ics",
        "autoArchivePeriod": 6.0,
        "autoClosePeriod": None,
        "autoCloseStateId": None,
        "securitySettings": {},
        "issueEstimationType": "notUsed",
        "defaultIssueEstimate": 0.0,
        "issueEstimationExtended": False,
        "issueEstimationAllowZero": False,
        "inheritIssueEstimation": True,
        "inheritWorkflowStatuses": False,
        "setIssueSortOrderOnStateChange": "first",
        "issueSortOrderDefaultToBottom": False,
        "issueOrderingNoPriorityFirst": False,
        "requirePriorityToLeaveTriage": False,
        "triageEnabled": False,
        "groupIssueHistory": True,
        "ledInitiativeCount": 0.0,
        "aiDiscussionSummariesEnabled": False,
        "aiThreadSummariesEnabled": False,
        "slackIssueComments": False,
        "slackNewIssue": False,
        "slackIssueStatuses": False,
        "scimManaged": False,
        "scimGroupName": None,
        "icon": None,
        "color": None,
        "description": None,
        "archivedAt": None,
        "retiredAt": None,
        "allMembersCanJoin": None,
        "autoCloseChildIssues": None,
        "autoCloseParentIssues": None,
        "defaultTemplateForMembersId": None,
        "defaultTemplateForNonMembersId": None,
        "_container": container,  # not a schema field: how Team.issues knows what to query
    }


def _issue(row, info) -> dict:
    """One ``linear_issues`` row as an ``Issue``.

    The stubs are deliberate and listed in the SDL header: reactions, SLA timestamps, board /
    sort orders, bot actors and shared access are declared because `@linear/sdk`'s fragment
    selects them, and resolve empty because a document corpus has nothing behind them."""
    identifier = row["identifier"] or synth.linear_identifier(
        row["doc_id"], synth.linear_team_key(row["team"])
    )
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
        "branchName": row["branch_name"]
        or synth.linear_branch_name(identifier, title, row["assignee_email"]),
        "priority": float(row["priority"] if row["priority"] is not None else 0),
        "priorityLabel": synth.linear_priority_label(row["priority"]),
        "estimate": float(row["estimate"]) if row["estimate"] is not None else None,
        "dueDate": row["due_date"],
        "createdAt": synth.rfc3339(created),
        "updatedAt": synth.rfc3339(updated),
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
        "trashed": None,
        "reactionData": {},
        "reactions": [],
        "integrationSourceType": None,
        "previousIdentifiers": [],
        "customerTicketCount": 0.0,
        "inheritsSharedAccess": False,
        "boardOrder": 0.0,
        "sortOrder": 0.0,
        "prioritySortOrder": 0.0,
        "subIssueSortOrder": None,
        "startedTriageAt": None,
        "triagedAt": None,
        "addedToCycleAt": None,
        "addedToProjectAt": None,
        "addedToTeamAt": None,
        "snoozedUntilAt": None,
        "slaStartedAt": None,
        "slaBreachesAt": None,
        "slaHighRiskAt": None,
        "slaMediumRiskAt": None,
        "slaType": None,
        # NOT null, and not a stub by choice: `@linear/sdk` builds `new IssueSharedAccess(...)`
        # unconditionally, so a null here is a TypeError inside the client rather than an empty
        # field. The values are also simply true — nothing in a document corpus is shared with an
        # external viewer — so the honest answer and the working one coincide.
        "sharedAccess": {
            "isShared": False,
            "sharedWithCount": 0.0,
            "sharedWithUsers": [],
            "viewerHasOnlySharedAccess": False,
            "disallowedIssueFields": [],
        },
        "delegate": None,
        "botActor": None,
        "sourceComment": None,
        "syncedWith": None,
        "externalUserCreator": None,
        "asksExternalUserRequester": None,
        "asksRequester": None,
        "lastAppliedTemplate": None,
        "projectMilestone": None,
        "recurringIssueTemplate": None,
        "snoozedBy": None,
        "favorite": None,
        "_row": row,  # not a schema field: how Issue.comments / Issue.labels reach the row
    }


def _comment(row, info) -> dict:
    ts = synth.rfc3339(row["created_ts"])
    return {
        "id": synth.linear_comment_id(row["id"]),
        "body": row["body"],
        "createdAt": ts,
        "updatedAt": ts,
        "url": f"https://linear.app/{_org(info)}/issue/#comment-{synth.linear_comment_id(row['id'])}",
        "reactionData": {},
        "reactions": [],
        "user": _user(row["author_email"], None, info),
        "issueId": synth.linear_id(row["doc_id"]),
        "quotedText": None,
        "archivedAt": None,
        "editedAt": None,
        "resolvedAt": None,
        "resolvingCommentId": None,
        "documentContentId": None,
        "initiativeId": None,
        "initiativeUpdateId": None,
        "parentId": None,
        "projectId": None,
        "projectUpdateId": None,
        "agentSession": None,
        "botActor": None,
        "resolvingComment": None,
        "documentContent": None,
        "syncedWith": None,
        "externalThread": None,
        "externalUser": None,
        "initiative": None,
        "initiativeUpdate": None,
        "issue": None,
        "parent": None,
        "project": None,
        "projectUpdate": None,
        "resolvingUser": None,
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
            out[k] = {
                op: (
                    [idx.get(str(x), "\x00none") for x in val]
                    if isinstance(val, list)
                    else idx.get(str(val), "\x00none")
                )
                for op, val in v.items()
            }
        elif k in ("and", "or") and isinstance(v, list):
            out[k] = [_resolve_issue_ids(info, s) for s in v]
        else:
            out[k] = v
    return out


def _issue_page(
    info,
    *,
    team=None,
    first=None,
    after=None,
    last=None,
    before=None,
    filter=None,
    orderBy=None,
    sort=None,
    includeArchived=False,
    **_ignored,
) -> dict:
    ctx = _ctx(info)
    conn, visible = ctx["conn"], ctx["visible_ids"]
    offset, limit, floor = _slice(first, after, last, before)
    prefilter = compile_issue_filter(conn, _resolve_issue_ids(info, filter))
    if offset is None:
        # `last:` with no `before:` is the only shape that needs a total, so the COUNT is paid
        # here and not on every page.
        offset = _from_end(
            None,
            limit,
            floor,
            store.count_linear_issues(
                conn, team, visible_ids=visible, prefilter=prefilter, archived=includeArchived
            ),
        )
    rows = store.list_linear_issues(
        conn,
        team,
        visible_ids=visible,
        limit=limit + 1,
        offset=offset,
        order_by=orderBy,
        prefilter=prefilter,
        sort=sort,
        archived=includeArchived,
    )
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
    """``team(id:)`` takes a team UUID or its key (``ENG``).

    Scoped the same way ``teams`` is: a team the caller can see no issue in is not a team they can
    see. Without this the two roots contradict each other — ``teams`` would omit the team while
    ``team(id: "BLA")`` confirmed its existence and name."""
    ctx = _ctx(info)
    container = ctx.get("team_index", {}).get(str(id))
    if (
        container is not None
        and ctx["visible_ids"] is not None
        and not store.linear_team_has_visible(ctx["conn"], container, ctx["visible_ids"])
    ):
        container = None
    if container is None:
        raise GraphQLError(f"Entity not found: Team - Could not find referenced Team. id={id}")
    return _team(container, info)


def resolve_teams(
    _root, info, first=None, after=None, last=None, before=None, filter=None, **_ignored
) -> dict:
    ctx = _ctx(info)
    offset, limit, floor = _slice(first, after, last, before)
    # A team the caller can see no issue in is not a team they can see — same rule the Slack
    # router applies to channels, and it keeps `teams` consistent with what `team.issues` returns.
    # An EXISTS probe per team, NOT the grouped count: `issueCount` is a bound field that only
    # runs when selected, and computing every team's total just to test visibility cost 22ms of
    # ACL-filtered scan on the bench corpus for a question `LIMIT 1` answers.
    names = [
        r["name"]
        for r in store.list_containers(ctx["conn"], "linear")
        if ctx["visible_ids"] is None
        or store.linear_team_has_visible(ctx["conn"], r["name"], ctx["visible_ids"])
    ]
    names = [n for n in names if _match_team(n, filter)]
    offset = _from_end(offset, limit, floor, len(names))
    page = names[offset : offset + limit]
    return _connection([_team(n, info) for n in page], offset, offset + limit < len(names))


def resolve_comments(
    _root, info, first=None, after=None, last=None, before=None, filter=None, **_ignored
) -> dict:
    ctx = _ctx(info)
    conn, visible = ctx["conn"], ctx["visible_ids"]
    offset, limit, floor = _slice(first, after, last, before)
    prefilter = compile_comment_filter(conn, filter)
    if offset is None:
        offset = _from_end(
            None,
            limit,
            floor,
            store.count_linear_comments(conn, visible_ids=visible, prefilter=prefilter),
        )
    rows = store.list_linear_comments(
        conn, visible_ids=visible, limit=limit + 1, offset=offset, prefilter=prefilter
    )
    rows, has_next = _page(rows, limit)
    return _connection([_comment(r, info) for r in rows], offset, has_next)


def resolve_users(
    _root, info, first=None, after=None, last=None, before=None, filter=None, **_ignored
) -> dict:
    ctx = _ctx(info)
    offset, limit, floor = _slice(first, after, last, before)
    rows = [r for r in store.list_users(ctx["conn"]) if _match_user(r, filter)]
    offset = _from_end(offset, limit, floor, len(rows))
    page = rows[offset : offset + limit]
    return _connection(
        [_user(r["email"], r["display_name"], info) for r in page],
        offset,
        offset + limit < len(rows),
    )


# --- by-id roots for the SDK's lazy relation accessors ------------------------------------
# `await issue.state` does NOT read the state off the issue the SDK already has — it fires
# `workflowState(id:)`. Each id is a one-way hash of a name, so each root reads the reverse map
# the app built at startup (see backlot.main._build_index). All five are declared non-null in Linear,
# so a miss is an "Entity not found" error, matching the real API.
#
# THE INDEX IS NOT ACL-SCOPED, so the lookup alone is not enough. It is an unfiltered DISTINCT
# over every issue, and these entities have no table of their own — a project, cycle, workflow
# state, label or assignee exists only as a COLUMN VALUE on some issue. Resolving one without a
# visibility check would hand a caller field values off a row they are denied: the name of a
# project that appears on one hidden issue, or the identity of its assignee (who may not even
# appear in the `users` directory that caller sees). Worse, the ids are pure functions of the
# name in an open-source repo, so they are computable offline and the roots become an ENUMERABLE
# oracle, not merely a confirmable one.
#
# So each root probes visibility the way `teams` already does, and a miss raises the SAME
# "Entity not found" as a genuinely absent id — hidden and absent stay indistinguishable.
# This costs the honest caller nothing: `@linear/sdk`'s lazy accessors only fire these for
# entities hanging off an issue it just successfully read, so the probe always finds that issue.


def _by_id(info, index_key: str, id_value, entity: str, kind: str | None = None):
    ctx = _ctx(info)
    found = ctx.get(index_key, {}).get(str(id_value))
    if (
        found is not None
        and kind is not None
        and not store.linear_entity_has_visible(
            ctx["conn"], kind, found, visible_ids=ctx["visible_ids"]
        )
    ):
        found = None  # exists in the corpus, but not for this caller — answer as if absent
    if found is None:
        raise GraphQLError(
            f"Entity not found: {entity} - Could not find referenced {entity}. id={id_value}"
        )
    return found


def resolve_user(_root, info, id) -> dict:
    email, display = _by_id(info, "user_index", id, "User", kind="user")
    return _user(email, display, info)


def resolve_workflow_state(_root, info, id) -> dict:
    team, name = _by_id(info, "state_index", id, "WorkflowState", kind="state")
    return _state(name, team, info)


def resolve_project(_root, info, id) -> dict:
    return _project(_by_id(info, "project_index", id, "Project", kind="project"), info)


def resolve_cycle(_root, info, id) -> dict:
    team, name = _by_id(info, "cycle_index", id, "Cycle", kind="cycle")
    return _cycle(name, team, info)


def resolve_issue_label(_root, info, id) -> dict:
    name = _by_id(info, "label_index", id, "IssueLabel", kind="label")
    return _label(name, synth.rfc3339(synth.epoch("linear-label:" + name)))


def resolve_release(_root, info, id) -> dict:
    return _release(_by_id(info, "release_index", id, "Release", kind="release"), info)


def resolve_attachment(_root, info, id) -> dict:
    """Attachments live in their own table, so this resolves by row id and scopes on the parent
    issue rather than going through the name-keyed reverse index the other roots use."""
    ctx = _ctx(info)
    row = store.linear_attachment_by_id(ctx["conn"], str(id), visible_ids=ctx["visible_ids"])
    if row is None:
        raise GraphQLError(
            f"Entity not found: Attachment - Could not find referenced Attachment. id={id}"
        )
    return _attachment(row, info)


def resolve_issue_relation(_root, info, id) -> dict:
    ctx = _ctx(info)
    row = store.linear_relation_by_id(ctx["conn"], str(id), visible_ids=ctx["visible_ids"])
    if row is None:
        raise GraphQLError(
            f"Entity not found: IssueRelation - Could not find referenced IssueRelation. id={id}"
        )
    return _relation(row, info)


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


def resolve_issue_comments(
    issue, info, first=None, after=None, last=None, before=None, filter=None, **_ignored
) -> dict:
    ctx = _ctx(info)
    conn, visible = ctx["conn"], ctx["visible_ids"]
    offset, limit, floor = _slice(first, after, last, before)
    doc_id = issue["_row"]["doc_id"]
    prefilter = compile_comment_filter(conn, filter)
    if offset is None:
        offset = _from_end(
            None,
            limit,
            floor,
            store.count_linear_comments(
                conn, doc_id=doc_id, visible_ids=visible, prefilter=prefilter
            ),
        )
    rows = store.list_linear_comments(
        conn,
        doc_id=doc_id,
        visible_ids=visible,
        limit=limit + 1,
        offset=offset,
        prefilter=prefilter,
    )
    rows, has_next = _page(rows, limit)
    return _connection([_comment(r, info) for r in rows], offset, has_next)


def resolve_issue_parent(issue, info):
    """``Issue.parent`` — read off the ``parent_doc_id`` resolved at import, ACL-scoped, so a
    parent the caller cannot read is null rather than a way to confirm it exists.

    Reads the SAME column ``Issue.children`` does, which is the entire point of resolving the key
    once at import: the two directions are exact inverses because they consult one value, not
    because two independent lookups happen to agree. A primary-key lookup, not an identifier one:
    `@linear/sdk`'s Issue fragment selects ``parent { id }`` on every node, so a page would
    otherwise do one indexed-identifier search per row. Bound rather than precomputed in
    :func:`_issue`, so a page pays nothing unless ``parent`` is selected."""
    parent_doc_id = issue["_row"]["parent_doc_id"]
    if not parent_doc_id:
        return None
    ctx = _ctx(info)
    row = store.get_document(ctx["conn"], "linear", parent_doc_id, visible_ids=ctx["visible_ids"])
    return _issue(row, info) if row is not None else None


def resolve_issue_children(
    issue, info, first=None, after=None, last=None, before=None, filter=None, **_ignored
) -> dict:
    """``Issue.children`` — the exact inverse of ``Issue.parent``, read off the ``parent_doc_id``
    resolved at import. Not a join on ``identifier``: bench keys repeat, so that would attach one
    issue's children to every issue sharing its key."""
    ctx = _ctx(info)
    conn, visible = ctx["conn"], ctx["visible_ids"]
    offset, limit, floor = _slice(first, after, last, before)
    doc_id = issue["_row"]["doc_id"]
    prefilter = compile_issue_filter(conn, _resolve_issue_ids(info, filter))
    if offset is None:
        offset = _from_end(
            None,
            limit,
            floor,
            len(
                store.linear_children(
                    conn, doc_id, visible_ids=visible, limit=PAGE_MAX, prefilter=prefilter
                )
            ),
        )
    rows = store.linear_children(
        conn, doc_id, visible_ids=visible, limit=limit + 1, offset=offset, prefilter=prefilter
    )
    rows, has_next = _page(rows, limit)
    return _connection([_issue(r, info) for r in rows], offset, has_next)


def _relations_page(issue, info, *, inverse, first, after, last, before) -> dict:
    ctx = _ctx(info)
    conn, visible = ctx["conn"], ctx["visible_ids"]
    offset, limit, floor = _slice(first, after, last, before)
    doc_id = issue["_row"]["doc_id"]
    if offset is None:
        offset = _from_end(
            None,
            limit,
            floor,
            len(
                store.linear_relations(
                    conn, doc_id, inverse=inverse, visible_ids=visible, limit=PAGE_MAX
                )
            ),
        )
    rows = store.linear_relations(
        conn, doc_id, inverse=inverse, visible_ids=visible, limit=limit + 1, offset=offset
    )
    rows, has_next = _page(rows, limit)
    return _connection([_relation(r, info) for r in rows], offset, has_next)


def resolve_issue_relations(
    issue, info, first=None, after=None, last=None, before=None, **_ignored
) -> dict:
    return _relations_page(
        issue, info, inverse=False, first=first, after=after, last=last, before=before
    )


def resolve_issue_inverse_relations(
    issue, info, first=None, after=None, last=None, before=None, **_ignored
) -> dict:
    return _relations_page(
        issue, info, inverse=True, first=first, after=after, last=last, before=before
    )


def resolve_relation_issue(relation, info) -> dict:
    return _issue_by_doc_id(info, relation["_from"])


def resolve_relation_related_issue(relation, info) -> dict:
    return _issue_by_doc_id(info, relation["_to"])


def _issue_by_doc_id(info, doc_id):
    """Both ends of a relation are declared non-null, and `linear_relations` already ACL-filtered
    on the far end — so this only re-reads the row, and a miss means the ACL changed under us."""
    ctx = _ctx(info)
    row = store.get_document(ctx["conn"], "linear", doc_id, visible_ids=ctx["visible_ids"])
    if row is None:
        raise GraphQLError("Entity not found: Issue - Could not find referenced Issue.")
    return _issue(row, info)


def resolve_issue_attachments(
    issue, info, first=None, after=None, last=None, before=None, url=None, filter=None, **_ignored
) -> dict:
    ctx = _ctx(info)
    conn, visible = ctx["conn"], ctx["visible_ids"]
    offset, limit, floor = _slice(first, after, last, before)
    doc_id = issue["_row"]["doc_id"]
    nodes = [
        _attachment(r, info)
        for r in store.linear_attachments(
            conn, doc_id, visible_ids=visible, limit=PAGE_MAX, url=url
        )
    ]
    nodes = [n for n in nodes if _match_attachment(n, filter)]
    offset = _from_end(offset, limit, floor, len(nodes))
    return _connection(nodes[offset : offset + limit], offset, offset + limit < len(nodes))


def resolve_attachment_issue(attachment, info) -> dict:
    return _issue_by_doc_id(info, attachment["_doc_id"])


def resolve_issue_releases(
    issue, info, first=None, after=None, last=None, before=None, filter=None, **_ignored
) -> dict:
    """An issue names at most one release in the corpus, but Linear models it as a connection."""
    offset, limit, floor = _slice(first, after, last, before)
    rel = _release(issue["_row"]["release"], info)
    nodes = [n for n in ([rel] if rel else []) if _match_release(n, filter)]
    offset = _from_end(offset, limit, floor, len(nodes))
    return _connection(nodes[offset : offset + limit], offset, offset + limit < len(nodes))


def resolve_issue_labels(
    issue, info, first=None, after=None, last=None, before=None, filter=None, **_ignored
) -> dict:
    """Labels are a JSON column on the issue, so the whole set is already in hand; the page is a
    slice of it rather than another query."""
    offset, limit, floor = _slice(first, after, last, before)
    nodes = [n for n in _label_nodes(issue["_row"]) if _match_label(n, filter)]
    offset = _from_end(offset, limit, floor, len(nodes))
    return _connection(nodes[offset : offset + limit], offset, offset + limit < len(nodes))


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
        "release": resolve_release,
        "attachment": resolve_attachment,
        "issueRelation": resolve_issue_relation,
    },
    "Team": {"issues": resolve_team_issues, "issueCount": resolve_team_issue_count},
    "Issue": {
        "comments": resolve_issue_comments,
        "labels": resolve_issue_labels,
        "parent": resolve_issue_parent,
        "children": resolve_issue_children,
        "relations": resolve_issue_relations,
        "inverseRelations": resolve_issue_inverse_relations,
        "attachments": resolve_issue_attachments,
        "releases": resolve_issue_releases,
    },
    "IssueRelation": {
        "issue": resolve_relation_issue,
        "relatedIssue": resolve_relation_related_issue,
    },
    "Attachment": {"issue": resolve_attachment_issue},
}


def build_engine():
    """The Linear engine, over the SDL beside this module."""
    from backlot.graphql import engine

    return engine.from_sdl(__file__, "linear", RESOLVERS)


__all__ = ["RESOLVERS", "build_engine", "PAGE_DEFAULT", "PAGE_MAX"]
