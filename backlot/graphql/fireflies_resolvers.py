"""Bind ``fireflies.graphql`` to :mod:`backlot.store`.

Every resolver returns plain dicts and lets graphql-core's default resolver pick the selected
keys off them, so a field the client didn't ask for costs nothing to have built. The ones bound
explicitly are those that take arguments or hit the DB.

Three things are worth knowing before reading further:

- **Pagination is offset-based**, not a Relay connection: `limit`/`skip` straight through to
  SQL. `limit` is CLAMPED to Fireflies' documented maximum of 50 rather than rejected, because
  that is what the real API does — a client asking for 200 gets 50, not an error.
- **ACL comes from the context, not from here.** ``info.context["visible_ids"]`` is threaded
  into every store call, so a resolver never makes an access decision of its own. A transcript
  the caller may not read is indistinguishable from one that does not exist.
- **Nulls are honest.** Anything the corpus cannot back resolves to ``null``; the SDL header
  lists every such field and why.
"""

from __future__ import annotations

import datetime as _dt

from backlot import store, synth

# Fireflies' own page size and its documented hard cap.
PAGE_DEFAULT = 25
PAGE_MAX = 50

# Slots this module keeps on the request context (backlot/routers/fireflies.py builds one dict per
# request, so nothing here outlives the response or crosses a caller). The two lazily-bound fields
# that query — the speaker numbers and a channel's roster — read through them, so a page of
# transcripts costs one statement each rather than one per row.
_PAGE_IDS = "_ff_page_ids"
_SPEAKERS = "_ff_speakers"
_CHANNEL_MEMBERS = "_ff_channel_members"


def _ctx(info):
    return info.context


def clamp_limit(value) -> int:
    """Fireflies documents `limit` as "max 50". It CLAMPS rather than erroring, so a client that
    asks for 200 is served 50. A non-positive or unparseable value falls back to the default.

    Not ``pagination.clamp_limit``, which looks equivalent and is not: it compares the value
    without coercing, so a non-numeric ``limit`` raises TypeError there instead of falling back.
    That path is reachable and pinned (see tests/test_pagination.py)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return PAGE_DEFAULT
    if n <= 0:
        return PAGE_DEFAULT
    return min(n, PAGE_MAX)


def clamp_skip(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def to_epoch_seconds(value) -> int | None:
    """A `DateTime` argument -> unix seconds. Fireflies documents these as ISO 8601 but its own
    `date` field is epoch MILLISECONDS, and clients pass back what they were given, so both are
    accepted (and a bare seconds value too)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        # Anything past ~year 33658 in seconds is milliseconds. Same threshold both directions.
        return int(n / 1000) if abs(n) > 1e11 else int(n)
    s = str(value).strip()
    if not s:
        return None
    if s.lstrip("-").isdigit():
        return to_epoch_seconds(int(s))
    try:
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)).timestamp())


def _millis(ts) -> float | None:
    return None if ts is None else float(int(ts) * 1000)


def _iso(ts) -> str | None:
    if ts is None:
        return None
    return (
        _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    )


def _display_name(email: str | None) -> str | None:
    if not email:
        return None
    local = email.split("@", 1)[0]
    return " ".join(p.capitalize() for p in local.replace("_", ".").split(".") if p) or local


def _user(email: str | None, display: str | None = None) -> dict | None:
    """A workspace user. `user_id` is derived from the address so it is stable and reversible."""
    if not email:
        return None
    return {
        "user_id": synth.fireflies_user_id(email),
        "email": email,
        "name": display or _display_name(email),
        "num_transcripts": None,
        "recent_meeting": None,
        "recent_transcript": None,
        # account/billing state this mock does not model — see the SDL header
        "minutes_consumed": None,
        "is_admin": None,
        "integrations": None,
        "is_calendar_in_sync": None,
        # The mock's ACL groups are a permission mechanism, not Fireflies user-groups, so they are
        # not served under this name. Fireflies serves the empty list, not null.
        "user_groups": [],
    }


def _sentence(row, index: int) -> dict:
    return {
        "index": index,
        "speaker_name": row["speaker_name"],
        "speaker_id": row["speaker_id"],
        "text": row["body"],
        # The corpus carries one text form, so the "raw" form is the same string rather than
        # a second, invented one.
        "raw_text": row["body"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        # Fireflies returns the object even when nothing classified: `text_cleanup` carries the
        # cleaned text, and the classifier flags are null.
        "ai_filters": {
            "text_cleanup": row["body"],
            "task": None,
            "pricing": None,
            "metric": None,
            "question": None,
            "date_and_time": None,
            "sentiment": None,
        },
    }


def _summary(row) -> dict:
    """The stored `summary` JSON, filled out to the API's full Summary shape."""
    s = store.jcol(row, "summary") or {}
    action_items = s.get("action_items")
    if isinstance(action_items, list):
        # Fireflies returns action_items as ONE newline-joined string, not a list.
        action_items = "\n".join(str(x) for x in action_items)
    outline = s.get("outline")
    if isinstance(outline, list):
        outline = "\n".join(str(x) for x in outline)
    return {
        "keywords": s.get("keywords"),
        "action_items": action_items,
        "outline": outline,
        "overview": s.get("overview"),
        "shorthand_bullet": s.get("shorthand_bullet"),
        "short_summary": s.get("short_summary") or s.get("overview"),
        "topics_discussed": s.get("topics_discussed"),
        "meeting_type": s.get("meeting_type"),
        # further LLM renderings of the same notes — see the SDL header
        "bullet_gist": None,
        "gist": None,
        "transcript_chapters": None,
        "notes": None,
        "short_overview": None,
        "extended_sections": None,
    }


def _words_per_minute(word_count, duration_secs) -> float | None:
    """Words per minute over this speaker's own talk time. Null rather than a division error when
    the corpus gives a speaker no measurable time."""
    if word_count is None or not duration_secs:
        return None
    return round(float(word_count) * 60.0 / float(duration_secs), 2)


def _analytics_speakers(row, speakers: list) -> list[dict]:
    """`analytics.speakers` is Fireflies' AnalyticsSpeaker: talk-time statistics keyed by the same
    per-meeting speaker number the sentences carry. `speakers` is the roster those numbers come
    from, joined to the stored statistics by NAME — the only key the analytics JSON has, since
    synth.fireflies_speaker_stats aggregates by it.

    So this is one entry per LABEL where `Transcript.speakers` is one per number: numbers sharing a
    label — several runs diarization left unlabelled, or one name it used for two of them — are ONE
    statistics entry however many numbers they cover, served under one of those numbers, while the
    roster keeps every number. Nothing in the stored analytics can separate them."""
    numbers = {s["speaker_name"]: s["speaker_id"] for s in speakers}
    out = []
    for sp in (store.jcol(row, "analytics") or {}).get("speakers") or []:
        name = sp.get("name")
        duration = sp.get("duration")
        out.append(
            {
                "speaker_id": numbers.get(name),
                "name": name,
                "duration": duration,
                "word_count": sp.get("word_count"),
                "longest_monologue": sp.get("longest_monologue"),
                "monologues_count": sp.get("monologues_count"),
                "filler_words": sp.get("filler_words"),
                "questions": sp.get("questions"),
                "duration_pct": sp.get("duration_pct"),
                "words_per_minute": _words_per_minute(sp.get("word_count"), duration),
            }
        )
    return out


def _analytics(row) -> dict:
    a = store.jcol(row, "analytics") or {}
    return {
        "_row": row,
        "sentiments": a.get("sentiments"),
        # bound explicitly (see RESOLVERS): the speaker numbers live in fireflies_sentences
        # classifier buckets — see the SDL header
        "categories": a.get("categories"),
    }


def _channel(name: str) -> dict:
    """One Channel. `id` IS the channel name rather than a minted opaque id, because
    `transcripts(channel_id:)` selects on the name — a client must be able to feed `channels { id }`
    straight back in."""
    return {
        "_channel": name,
        "id": name,
        "title": name,
        # the corpus states which channel a meeting is in, not the channel's own history
        "created_at": None,
        "updated_at": None,
        "created_by": None,
        "is_private": None,
    }


def _transcript(row, info) -> dict:
    """One transcript as a dict. `_row` is carried along so the field resolvers that need another
    query (sentences) can reach the transcript id without a second lookup."""
    host = row["author_email"]
    return {
        "_row": row,
        "id": row["id"],
        "title": row["title"] or None,
        "date": _millis(row["created_ts"]),
        "dateString": _iso(row["created_ts"]),
        "duration": row["duration"],
        "host_email": host,
        # Fireflies falls back to the host when a meeting has no distinct organizer, which is the
        # common case; a NULL column must not surface as a null field the client then can't use.
        "organizer_email": row["organizer_email"] or host,
        "participants": store.jcol(row, "participants", []) or [],
        "user": _user(host, row["owner_display"]),
        "fireflies_users": [
            e
            for e in (store.jcol(row, "participants", []) or [])
            if isinstance(e, str) and "@external." not in e
        ],
        # workspace membership and link-sharing state — see the SDL header. Fireflies serves the
        # empty list for both, not null.
        "workspace_users": [],
        "shared_with": [],
        # The corpus is finished recordings, so no meeting is ever mid-transcription.
        "is_live": False,
        "privacy": "link",
        "meeting_link": row["meeting_link"],
        "calendar_id": row["calendar_id"],
        "cal_id": row["calendar_id"],
        "calendar_type": row["calendar_type"],
        "channels": [_channel(row["channel"])] if row["channel"] else [],
        "transcript_url": row["transcript_url"],
        "audio_url": row["audio_url"],
        "video_url": row["video_url"],
        "summary": _summary(row),
        "analytics": _analytics(row),
        "meeting_attendees": store.jcol(row, "meeting_attendees", []) or [],
        # who was INVITED is in the corpus; who dialed in and for how long is not
        "meeting_attendance": None,
        "meeting_info": {
            "fred_joined": True,
            "silent_meeting": False,
            "summary_status": "processed",
        },
        # Fireflies serves the wrapper with an empty `outputs`, not null — see the SDL header.
        "apps_preview": {"outputs": []},
    }


# --- Query roots ------------------------------------------------------------------


def resolve_transcripts(
    _root,
    info,
    keyword=None,
    scope=None,
    fromDate=None,
    toDate=None,
    host_email=None,
    organizers=None,
    organizer_email=None,
    participants=None,
    participant_email=None,
    title=None,
    date=None,
    user_id=None,
    mine=None,
    channel_id=None,
    limit=None,
    skip=None,
    **_ignored,
):
    ctx = _ctx(info)
    if store.fireflies_scope_columns(scope) is None:
        # An unknown `scope` is a client mistake, and silently searching everything would hide it.
        # A field error (partial data + errors), which is what the real API returns.
        raise ValueError(f"scope must be one of title, sentences, all — got {scope!r}")

    hosts = [host_email] if host_email else []
    # `mine` and `user_id` both mean "restrict to this user's own meetings". The caller's own
    # address is the only identity the server can vouch for, so an admin token (no caller_email)
    # asking for `mine` gets nothing rather than everything — the same reasoning as Linear's
    # `viewer`.
    caller = ctx.get("caller_email")
    if mine:
        hosts.append(caller or "\x00none")
    if user_id:
        hosts.append(_email_for_user_id(ctx, user_id) or "\x00none")
    # Several host constraints that disagree can match nothing; that is the honest answer.
    if len({h.lower() for h in hosts}) > 1:
        return []

    # The singular forms are aliases of the plural filters, so they combine the same way:
    # `organizers` is any-of, `participants` is all-of.
    organizers = [*(organizers or []), *([organizer_email] if organizer_email else [])]
    participants = [*(participants or []), *([participant_email] if participant_email else [])]

    from_ts = to_epoch_seconds(fromDate)
    to_ts = to_epoch_seconds(toDate)
    if date is not None:
        # `date` selects a DAY, not an instant: the real API returns every meeting sharing the
        # calendar day of the value passed, and that day is UTC — a value anywhere in the day
        # before or after returns nothing, whichever zone the caller is in. Narrowed against any
        # fromDate/toDate already given.
        day = to_epoch_seconds(date)
        if day is None:
            return []
        start = day - (day % 86400)
        from_ts = start if from_ts is None else max(from_ts, start)
        end = start + 86399
        to_ts = end if to_ts is None else min(to_ts, end)

    rows = store.list_fireflies_transcripts(
        ctx["conn"],
        channel=channel_id,
        host_email=hosts[0] if hosts else None,
        organizers=organizers or None,
        participants=participants or None,
        title=title,
        from_ts=from_ts,
        to_ts=to_ts,
        keyword=keyword,
        scope=scope,
        visible_ids=ctx.get("visible_ids"),
        limit=clamp_limit(limit),
        offset=clamp_skip(skip),
    )
    # The page's ids, for the batched speaker load. Accumulated rather than assigned: one document
    # may select `transcripts` twice under aliases, and a second page must not drop the first's.
    ctx.setdefault(_PAGE_IDS, []).extend(r["id"] for r in rows)
    return [_transcript(r, info) for r in rows]


def _email_for_user_id(ctx, user_id):
    """Reverse a served `user_id` to its address — a unique-indexed column lookup."""
    return store.fireflies_user_by_served_id(ctx["conn"], user_id)


def resolve_transcript(_root, info, id):
    ctx = _ctx(info)
    row = store.fireflies_transcript_by_id(ctx["conn"], id, ctx.get("visible_ids"))
    return _transcript(row, info) if row else None


def resolve_transcript_sentences(transcript, info, **_ignored):
    """Sentences are a separate table, so this is the one Transcript field that queries. Bound
    explicitly for that reason — selecting only metadata never touches fireflies_sentences."""
    row = transcript["_row"]
    rows = store.fireflies_sentences(_ctx(info)["conn"], row["id"])
    return [_sentence(r, i) for i, r in enumerate(rows)]


def _speakers(info, transcript_id) -> list:
    """This transcript's roster rows, read once per request and loaded for the whole page at once.

    Two fields want them — `Transcript.speakers` and `analytics.speakers` — and both are resolved
    per transcript, so the unmemoised read is one statement per row of the page and two when both
    are selected. The page's ids are registered by resolve_transcripts, so the first field that
    asks loads all of them in one statement; a `transcript(id:)` root registers none and loads the
    one it has.
    """
    ctx = _ctx(info)
    cache = ctx.setdefault(_SPEAKERS, {})
    if transcript_id not in cache:
        want = [i for i in ctx.get(_PAGE_IDS) or () if i not in cache]
        if transcript_id not in want:
            want.append(transcript_id)
        cache.update(store.fireflies_speakers(ctx["conn"], want))
    return cache[transcript_id]


def resolve_transcript_speakers(transcript, info, **_ignored):
    """Fireflies' `Transcript.speakers` is identity only — name and the per-meeting number. Both
    live in fireflies_sentences, so this queries; selecting metadata alone never does."""
    return [
        {"id": float(s["speaker_id"]), "name": s["speaker_name"]}
        for s in _speakers(info, transcript["_row"]["id"])
    ]


def resolve_analytics_speakers(analytics, info, **_ignored):
    """`analytics.speakers` carries the stored talk-time statistics, joined to the same speaker
    numbers `Transcript.speakers` reports."""
    row = analytics["_row"]
    return _analytics_speakers(row, _speakers(info, row["id"]))


def resolve_channel_members(channel, info, **_ignored):
    """A channel's roster: everyone who took part in a meeting in the channel THIS CALLER can read.
    Bound rather than built eagerly: `channels { id title }` is the common selection and must not
    query.

    Cached on the channel name for the request, because a page is usually one channel's meetings
    and the roster is a property of the channel, not of the transcript it was reached through —
    ``visible_ids`` is fixed for the request, so two transcripts in one channel have the same
    answer."""
    ctx = _ctx(info)
    cache = ctx.setdefault(_CHANNEL_MEMBERS, {})
    name = channel["_channel"]
    if name not in cache:
        cache[name] = [
            {
                "user_id": synth.fireflies_user_id(r["email"]),
                "email": r["email"],
                "name": r["display_name"] or _display_name(r["email"]),
            }
            for r in store.fireflies_channel_members(ctx["conn"], name, ctx.get("visible_ids"))
            if r["email"]
        ]
    return cache[name]


def resolve_user(_root, info, id=None):
    ctx = _ctx(info)
    if id is None:
        # No id -> the authenticated user, Fireflies' `user` default. An admin/service token is
        # not a person, so it has no user to return.
        email = ctx.get("caller_email")
        return _user(email) if email else None
    email = _email_for_user_id(ctx, id) or (id if "@" in str(id) else None)
    if not email:
        return None
    row = store.get_user(ctx["conn"], email)
    return _user(email, row["display_name"] if row else None)


def resolve_users(_root, info, **_ignored):
    """The Fireflies WORKSPACE's users — the people with an account, i.e. the authenticating
    roster, not every person the corpus happens to name.

    The real `users` query takes no pagination arguments, because a real workspace has tens or
    hundreds of members. The mock's `principals` table is much broader: every internal reference
    across every source is registered there (16,034 of them on the largest deployed corpus, of whom
    only 327 have a token). Serving all of those as workspace members would be both wrong — they
    have no Fireflies account — and a 1.6 MB unpaginated response, the same hazard Slack's
    `users.list` documents. Scoping to the roster is what bounds it, rather than inventing `limit`
    arguments the vendor's schema does not have.

    `user(id:)` deliberately still resolves ANY principal: a transcript's host is a real reference
    even when that person never had an account, so `Transcript.user` must not come back null.
    """
    ctx = _ctx(info)
    roster = ctx.get("roster")
    rows = store.list_users(ctx["conn"])
    if roster is not None:
        rows = [r for r in rows if r["email"] in roster]
    return [_user(r["email"], r["display_name"]) for r in rows]


RESOLVERS = {
    "Query": {
        "transcripts": resolve_transcripts,
        "transcript": resolve_transcript,
        "user": resolve_user,
        "users": resolve_users,
    },
    "Transcript": {
        "sentences": resolve_transcript_sentences,
        "speakers": resolve_transcript_speakers,
    },
    "MeetingAnalytics": {"speakers": resolve_analytics_speakers},
    "Channel": {"members": resolve_channel_members},
}


def build_engine():
    """The Fireflies engine, over the SDL beside this module."""
    from backlot.graphql import engine

    return engine.from_sdl(__file__, "fireflies", RESOLVERS)


__all__ = [
    "RESOLVERS",
    "build_engine",
    "PAGE_DEFAULT",
    "PAGE_MAX",
    "clamp_limit",
    "clamp_skip",
    "to_epoch_seconds",
]
