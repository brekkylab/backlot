"""Bind ``fireflies.graphql`` to :mod:`backlot.store`.

Every resolver returns plain dicts and lets graphql-core's default resolver pick the selected
keys off them, so a field the client didn't ask for costs nothing to have built. The ones bound
explicitly are those that take arguments or hit the DB.

Three things are worth knowing before reading further:

- **Pagination is offset-based**, not a Relay connection: `limit`/`skip` straight through to
  SQL. The measured validation bounds are checked before a page is read.
- **ACL comes from the context, not from here.** ``info.context["visible_ids"]`` is threaded
  into every store call, so a resolver never makes an access decision of its own. A transcript
  the caller may not read is indistinguishable from one that does not exist.
- **Nulls are honest.** Anything the corpus cannot back resolves to ``null``; the SDL header
  lists every such field and why.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
import secrets
import string

from graphql import GraphQLError

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
    """Normalize an internal page size after the resolver validates supplied arguments.

    Kept separate from ``pagination.clamp_limit``: that helper does not coerce an unparseable
    value. The direct helper behavior is pinned in tests/test_pagination.py.
    """
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
    return synth.utc_datetime(int(ts)).isoformat().replace("+00:00", "Z")


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
        # account/billing state Backlot does not model — see the SDL header
        "minutes_consumed": None,
        "is_admin": None,
        "integrations": None,
        "is_calendar_in_sync": None,
        # Backlot's ACL groups are a permission mechanism, not Fireflies user-groups, so they are
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


# One `invalid_arguments` error names each argument real refuses once, in this order whatever order
# the document sends them in, and one refused twice lists `exclusiveFields` first and takes its
# `message` from it (api.fireflies.ai, 2026-10-21).
_PERSON_FIELDS = (
    ("userId", "user_id"),
    ("mine", "mine"),
    ("organizers", "organizers"),
    ("participants", "participants"),
    ("hostEmail", "host_email"),
    ("organizerEmail", "organizer_email"),
    ("participantEmail", "participant_email"),
)
_PLURAL = ("organizers", "participants")
_EXCLUSIVE = (
    "Only one of the following fields can be provided: "
    "mine, userId, hostEmail, organizerEmail, participantEmail"
)
# The request-context slot for the one `correlationId` every `invalid_arguments` and
# `INTERNAL_SERVER_ERROR` error in a response carries (the two shared it in one response on
# api.fireflies.ai, 2026-10-22), and what it is drawn from.
_CORRELATION_ID = "_ff_correlation_id"
_ALNUM = string.ascii_letters + string.digits
# `date` in milliseconds (api.fireflies.ai, 2026-10-22): real refuses a value past `_DATE_BOUNDS`
# with `max` or `min`, adding `isNumber` for an infinite one, and answers one inside them with
# `_internal_error` when, its fraction cut toward zero, it falls outside `_DATE_SERVED`,
# 0000-01-01T00:00:00.000Z to 9999-12-31T23:59:59.999Z.
_DATE_BOUNDS = (-8_640_000_000_000_000, 8_639_999_999_999_999)
_DATE_SERVED = (-62_167_219_200_000, 253_402_300_799_999)
_DATE_MESSAGE = "date must be a valid timestamp in milliseconds"


def _correlation_id(context) -> str:
    return context.setdefault(_CORRELATION_ID, "".join(secrets.choice(_ALNUM) for _ in range(8)))


def _internal_error(context) -> GraphQLError:
    """Real's ``INTERNAL_SERVER_ERROR``, a field error at 200."""
    return GraphQLError(
        "Something unexpected happened. Please try again",
        extensions={
            "code": "INTERNAL_SERVER_ERROR",
            "correlationId": _correlation_id(context),
            "status": 500,
        },
    )


def _not_in_team(context) -> GraphQLError:
    """Real's answer to a `user` id that names nobody, an email address among them, the caller's
    own too: the null stands and a ``not_in_team`` field error goes beside it, the id read as
    naming someone outside the caller's team (api.fireflies.ai, 2026-10-27)."""
    return GraphQLError(
        "You do not have permissions for this team",
        extensions={
            "code": "not_in_team",
            "correlationId": _correlation_id(context),
            "status": 403,
            "helpUrls": ["https://docs.fireflies.ai/miscellaneous/error-codes#not_in_team"],
        },
    )


def _object_not_found(context) -> GraphQLError:
    """Real's answer to a `transcript` id that names nothing: the null stands and an
    ``object_not_found`` field error goes beside it, the object type in its metadata
    (api.fireflies.ai, 2026-10-27)."""
    return GraphQLError(
        "Transcript not found",
        extensions={
            "code": "object_not_found",
            "correlationId": _correlation_id(context),
            "status": 404,
            "metadata": {"objectType": "Transcript"},
            "helpUrls": ["https://docs.fireflies.ai/miscellaneous/error-codes#object_not_found"],
        },
    )


# validator.js's patterns, read as JavaScript reads them: `\s` is JavaScript's (no \x1c-\x1f or
# \x85, and a BOM), and a range ending at \uffff takes either half of a surrogate pair.
_JS_SPACE = "\t\n\x0b\x0c\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_WIDE = "\u00a1-\ud7ff\uf900-\ufdcf\ufdf0-\uffef"
_LOCAL_PART = re.compile(f"[a-zA-Z0-9!#$%&'*+\\-/=?^_`{{|}}~{_WIDE}]+")
_QUOTED_LOCAL = re.compile(
    f"(?:[{_JS_SPACE}\\x01-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f!#-\\[\\]-~\u00a0{_WIDE}]"
    f"|\\\\[\\x01-\\x09\\x0b\\x0c\\x0d-\\x7f\u00a0{_WIDE}])*"
)
_TLD = re.compile(
    "[a-zA-Z\u00a1-\u00a8\u00aa-\ud7ff\uf900-\ufdcf\ufdf0-\uffef]{2,}|[xX][nN][a-zA-Z0-9-]{2,}"
)
_LABEL = re.compile("[a-zA-Z0-9_\u00a1-\uffff-]+")


def _is_email(value: str) -> bool:
    """validator.js 13.15.15's ``isEmail`` with its default options, which answered as
    api.fireflies.ai's ``IsEmailOnly`` did for each address sent on 2026-10-21. The address is read
    as JavaScript reads it, in UTF-16 code units, but for the local part's and domain's byte limits,
    which count UTF-8."""
    raw = value.encode("utf-16-le", "surrogatepass")
    s = "".join(chr(int.from_bytes(raw[i : i + 2], "little")) for i in range(0, len(raw), 2))
    user, _, domain = s.rpartition("@")
    if len(s) > 254 or _utf8_len(user) > 64 or _utf8_len(domain) > 254:
        return False
    labels = domain.split(".")
    if (
        len(labels) < 2
        or not _TLD.fullmatch(labels[-1])
        or re.search(f"[{_JS_SPACE}]", labels[-1])
        or not all(
            len(p) <= 63
            and _LABEL.fullmatch(p)
            and not re.search("[_\uff01-\uff5e]", p)
            and not p.startswith("-")
            and not p.endswith("-")
            for p in labels
        )
    ):
        return False
    if user[:1] == '"' and user[-1:] == '"':
        return bool(_QUOTED_LOCAL.fullmatch(user[1:-1]))
    return all(_LOCAL_PART.fullmatch(p) for p in user.split("."))


def _utf8_len(units: str) -> int:
    text = units.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    return len(text.encode("utf-8", "surrogatepass"))


def _not_a_js_date(value) -> bool:
    """Whether real refuses ``value`` as a date. On 2026-10-21 api.fireflies.ai refused the strings
    V8's ``new Date()`` reads as an Invalid Date and no number or boolean. This tells the two kinds
    of string that need no date parser: one with no ASCII digit, which V8 never reads, and one of
    ASCII digits only, which it reads as a year when it has four and otherwise by value. Any other
    string is left to :func:`to_epoch_seconds`."""
    if not isinstance(value, str):
        return False
    if not re.search("[0-9]", value):
        return True
    if not re.fullmatch("[0-9]+", value) or len(value) == 4:
        return False
    digits = value.lstrip("0")
    return len(digits) > 6 or 13 <= int(digits or "0") <= 31 or int(digits or "0") > 275760


_SCOPE_ENUM = "scope must be one of the following values: title, sentences, all"
_SCOPE_NEEDS_KEYWORD = "keyword is required when scope is provided"


def _validate_transcripts(arguments):
    """Raise real's ``invalid_arguments`` error for the dates, page bounds, scope/keyword and
    person filters it refuses.

    ``mine``, ``user_id`` and the three singular addresses collide once two of them hold a value
    (``false``, ``null`` and ``""`` do not), and the error then names each of them that was sent,
    ``false`` and ``""`` included. ``organizers`` and ``participants`` collide with any one that
    holds a value, ``[]`` included, and the error names only the plural for that. A ``scope`` that
    holds a value (``""`` does not) needs a ``keyword`` holding a character outside ``_JS_SPACE``,
    and a ``scope`` spelled other than ``title``, ``sentences`` or ``all`` is refused, ``""``
    included; real lists ``scope`` ahead of the other arguments it refuses (api.fireflies.ai,
    2026-10-21 and 2026-10-22)."""
    fields = {}

    def add(name, kind, message):
        field = fields.setdefault(name, {"name": name, "message": message, "constraints": []})
        field["constraints"].append({"type": kind, "message": message})

    scope, keyword = arguments["scope"], arguments["keyword"]
    if scope and re.fullmatch(f"[{_JS_SPACE}]*", keyword or ""):
        add("scope", "conditionalRequired", _SCOPE_NEEDS_KEYWORD)
    if store.fireflies_scope_columns(scope) is None:
        add("scope", "isEnum", _SCOPE_ENUM)
    date = arguments["date"]
    if date is not None:
        if date > _DATE_BOUNDS[1]:
            add("date", "max", _DATE_MESSAGE)
        elif date < _DATE_BOUNDS[0]:
            add("date", "min", _DATE_MESSAGE)
        if math.isinf(date):
            add("date", "isNumber", "date must be a number conforming to the specified constraints")
    for name in ("fromDate", "toDate"):
        if _not_a_js_date(arguments[name]):
            add(name, "isDate", f"{name} must be a Date instance")
    limit, skip = arguments["limit"], arguments["skip"]
    if limit is not None:
        if limit <= 0:
            add("limit", "isPositive", "limit must be a positive number")
        elif limit > PAGE_MAX:
            add("limit", "max", "limit must not be greater than 50")
    if skip is not None:
        if skip < 0:
            add("skip", "min", "skip must not be less than 0")
        elif skip > 5000:
            add("skip", "max", "skip must not be greater than 5000")
    held = [key for name, key in _PERSON_FIELDS if name not in _PLURAL and arguments[key]]
    for name, key in _PERSON_FIELDS:
        value = arguments[key]
        if value is None:
            continue
        if name in _PLURAL:
            if held:
                add(name, "exclusiveFields", f"{_EXCLUSIVE}, {name}.")
            if not all(map(_is_email, value)):
                add(name, "IsEmailOnly", f"{name} must be a valid email address")
            continue
        if len(held) > 1:
            add(name, "exclusiveFields", f"{_EXCLUSIVE}.")
        if key.endswith("_email") and value and not _is_email(value):
            add(name, "IsEmailOnly", f"{name} must be a valid email address")
    if fields:
        raise GraphQLError(
            "Invalid argument(s) were provided",
            extensions={
                "code": "invalid_arguments",
                "correlationId": _correlation_id(arguments["info"].context),
                "status": 400,
                "metadata": {"fields": list(fields.values())},
                "helpUrls": [
                    "https://docs.fireflies.ai/miscellaneous/error-codes#invalid_arguments"
                ],
            },
        )


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
    _validate_transcripts(locals())
    ctx = _ctx(info)

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
    # Validation above leaves at most one singular person constraint. The plural-only pair
    # remains valid: `organizers` is any-of, `participants` is all-of.
    organizers = [*(organizers or []), *([organizer_email] if organizer_email else [])]
    participants = [*(participants or []), *([participant_email] if participant_email else [])]

    from_ts = to_epoch_seconds(fromDate)
    to_ts = to_epoch_seconds(toDate)
    if date is not None:
        ms = math.trunc(date)
        if not _DATE_SERVED[0] <= ms <= _DATE_SERVED[1]:
            raise _internal_error(ctx)
        # `date` selects a DAY, not an instant: the real API returns every meeting sharing the
        # calendar day of the value passed, and that day is UTC — a value anywhere in the day
        # before or after returns nothing, whichever zone the caller is in. Narrowed against any
        # fromDate/toDate already given. The value is milliseconds whatever its size: real answered
        # a transcript's date divided by 1000 with no transcripts, and the millisecond before a
        # midnight with the day before (2026-10-07). The day is read from `ms`, the value the year
        # check above reads.
        day = ms // 1000
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
    if row is None:
        raise _object_not_found(ctx)
    return _transcript(row, info)


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
    if not id:
        # No id, or an empty one (api.fireflies.ai, 2026-10-27) -> the authenticated user,
        # Fireflies' `user` default. An admin/service token is not a person, so it has no user to
        # return.
        email = ctx.get("caller_email")
        return _user(email) if email else None
    email = _email_for_user_id(ctx, id)
    if not email:
        raise _not_in_team(ctx)
    row = store.get_user(ctx["conn"], email)
    return _user(email, row["display_name"] if row else None)


def resolve_users(_root, info, **_ignored):
    """The Fireflies WORKSPACE's users — the people with an account, i.e. the authenticating
    roster, not every person the corpus happens to name.

    The real `users` query takes no pagination arguments, because a real workspace has tens or
    hundreds of members. Backlot's `principals` table is much broader: every internal reference
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


# Fireflies' one other root type, which its introspection named beside `Query` with no
# subscription type when measured on 2026-10-25 (UTC). Backlot serves no mutation.
ROOTS = ("Mutation",)


def build_engine():
    """The Fireflies engine, over the SDL beside this module."""
    from backlot.graphql import engine

    return engine.from_sdl(__file__, "fireflies", RESOLVERS, roots=ROOTS)


__all__ = [
    "RESOLVERS",
    "build_engine",
    "PAGE_DEFAULT",
    "PAGE_MAX",
    "clamp_limit",
    "clamp_skip",
    "to_epoch_seconds",
]
