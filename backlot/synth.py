"""Deterministic synthesis of structural metadata.

The published dataset only carries ``{doc_id, source_type, title, content}``. Every
structural field a real API returns (ids, timestamps, users, keys, ...) is derived
here from ``sha256(doc_id)`` so responses are stable and self-consistent across calls
and across paginated fetches.

All functions are pure and depend only on their arguments.
"""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime, timezone

BASE_EPOCH = 1_672_531_200  # 2023-01-01T00:00:00Z
TIME_RANGE = 63_072_000  # ~2 years


def _digest(doc_id: str) -> str:
    return hashlib.sha256(doc_id.encode("utf-8")).hexdigest()


def hnum(doc_id: str, start: int = 0, length: int = 8, salt: str = "") -> int:
    """A stable non-negative integer derived from a hex slice of the digest."""
    h = _digest(salt + doc_id) if salt else _digest(doc_id)
    start %= 64
    return int(h[start : start + length] or h[:length], 16)


def pick(doc_id: str, seq, salt: str = ""):
    """Deterministically choose one element of ``seq`` for this doc."""
    seq = list(seq)
    if not seq:
        return None
    return seq[hnum(doc_id, salt=salt) % len(seq)]


# --- timestamps -----------------------------------------------------------------


def epoch(doc_id: str, base: int = BASE_EPOCH, span: int = TIME_RANGE) -> int:
    """Stable unix-second timestamp within [base, base+span)."""
    return base + (hnum(doc_id, 0, 8) % span)


def rfc3339(ts: int) -> str:
    """e.g. 2024-04-05T17:00:00Z (Drive / GitHub / Confluence)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rfc3339_millis(ts: int) -> str:
    """e.g. 2024-04-05T17:00:00.000Z (Confluence version.when)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def jira_datetime(ts: int) -> str:
    """e.g. 2024-04-05T17:00:00.000+0000 (Jira)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def rfc2822(ts: int) -> str:
    """e.g. Fri, 05 Apr 2024 17:00:00 +0000 (Gmail Date header)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


# --- per-vendor identifiers -----------------------------------------------------


def slack_channel_id(channel_name: str) -> str:
    """Stable ``C…`` id keyed on the channel name (shared by all docs in it)."""
    h = _digest("chan:" + channel_name)
    return "C" + h[:10].upper()


def slack_user_id(email: str) -> str:
    h = _digest("user:" + email)
    return "U" + h[:10].upper()


def slack_fmt_ts(epoch_sec: int, key: str) -> str:
    """Format a Slack ts ``<epoch>.<6 digits>`` for a given second, with the
    micro-fraction keyed on ``key`` so every message in a thread shares it."""
    micro = hnum(key, 12, 6) % 1_000_000
    return f"{int(epoch_sec)}.{micro:06d}"


def slack_ts(doc_id: str) -> str:
    """Slack message id == timestamp: ``<epoch>.<6 digits>`` (unique per doc)."""
    return slack_fmt_ts(epoch(doc_id), doc_id)


def slack_thread_ts(root_doc_id: str, seq: int) -> str:
    """ts for a message in a thread: root (seq 0) equals ``slack_ts(root)``; each
    reply is ``seq`` seconds later, so replies sort after the root and share the
    root's ts as their thread_ts."""
    return slack_fmt_ts(epoch(root_doc_id) + int(seq), root_doc_id)


def gmail_id(doc_id: str, salt: str = "msg") -> str:
    """An opaque 16-hex token. Used for attachment ids and Slack's ``client_msg_id``, where the
    value is never parsed — so it deliberately spans the full 64-bit range. A *message* id is
    parsed by Gmail and must not; use ``gmail_message_id``."""
    return hnum(doc_id, salt=salt, length=16).__format__("016x")


# Gmail parses a message id as a signed 64-bit integer, so 2**63 is the ceiling. Measured against
# the live API: `7fffffffffffffff` resolves (404, a real id shape) while `8000000000000000` and
# `ffffffffffffffff` are refused with 400 "Invalid id value". Unmasked, half of any digest-derived
# id lands above the line — 278,278 of the bench corpus's 556,238 messages.
GMAIL_ID_MAX = 2**63


def gmail_message_id(key: str) -> str:
    """The served id for a Gmail message or thread: 16 lowercase hex digits below ``GMAIL_ID_MAX``.

    Threads share this space, as they do in real Gmail — a thread key is the root message's
    ``doc_id``, so a single-message thread reports the same value for ``id`` and ``threadId``, which
    is exactly what the real API does. That is also why one reverse index resolves both."""
    return f"{hnum(key, salt='msg', length=16) % GMAIL_ID_MAX:016x}"


def drive_folder_id(container: str) -> str:
    return "0A" + _digest("folder:" + container)[:26]


def github_number(doc_id: str) -> int:
    return hnum(doc_id, 0, 8) % 90_000 + 1


def jira_numeric_id(doc_id: str) -> int:
    return 10_000 + hnum(doc_id, 8, 8) % 900_000


def jira_key(doc_id: str, project_key: str) -> str:
    return f"{project_key}-{hnum(doc_id, 16, 6) % 9000 + 1}"


def hubspot_record_id(doc_id: str) -> str:
    """HubSpot record ids are numeric strings (e.g. "5790939450")."""
    return str(1_000_000_000 + hnum(doc_id, 0, 10) % 9_000_000_000)


def hubspot_assoc_type_id(from_type: str, to_type: str) -> int:
    """Association type id for one direction of a type pair. Real HubSpot uses well-known ids per
    direction (contact->company is not company->contact), so this is direction-sensitive too —
    derived from the ordered pair rather than being a shared constant."""
    return hnum(f"{from_type}>{to_type}", 0, 6) % 900 + 1


def confluence_id(doc_id: str) -> int:
    return 100_000 + hnum(doc_id, 24, 8) % 9_000_000


def atlassian_account_id(email: str) -> str:
    return "5b" + _digest("acct:" + email)[:22]


def github_login(email: str) -> str:
    return email.split("@", 1)[0].replace(".", "-")


def github_user_id(email: str) -> int:
    return 1000 + int(_digest("ghid:" + email)[:6], 16) % 9_000_000


def node_id(kind: str, num) -> str:
    """A GitHub-style base64 GraphQL global node id, e.g. ``MDU6SXNzdWUx``.
    Deterministic and opaque — enough for a v4-id-keyed connector to have *a* stable id."""
    return base64.b64encode(f"012:{kind}{num}".encode()).decode().rstrip("=")


def github_avatar(user_id: int) -> str:
    return f"https://avatars.githubusercontent.com/u/{user_id}?v=4"


def avatar_urls(account_id: str) -> dict:
    """Atlassian-style avatarUrls map (four square sizes)."""
    base = f"https://avatar.example.com/{account_id}"
    return {f"{s}x{s}": f"{base}?size={s}" for s in (48, 24, 16, 32)}


def _key(container: str, fallback: str) -> str:
    """A realistic project/space key: word initials, but always >= 2 chars.

    Multi-word containers use initials (``customer-support`` -> ``CS``); single-word ones
    take the first letters (``payments`` -> ``PAY``), since real Jira/Confluence keys — and
    strict clients like mcp-atlassian — reject single-character keys.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", container.lower()) if w]
    initials = "".join(w[0] for w in words).upper()
    if len(initials) >= 2:
        return initials
    if words:
        return words[0][:3].upper()
    return fallback


def jira_project_key(container: str) -> str:
    """A project key unique per container: the readable word-initials prefix (see :func:`_key`)
    plus a short hash of the full name, so distinct projects never collide on the same key (and the
    router's reverse key->project lookup + the derived issue keys stay unambiguous). Deterministic,
    valid Jira shape (uppercase letter start, uppercase alnum)."""
    return _key(container, "PROJ") + _digest(container)[:6].upper()


def confluence_space_key(container: str) -> str:
    """A space key that is unique per container: the readable word-initials prefix (see
    :func:`_key`) plus a short hash of the full name. Initials alone collide — e.g.
    ``eng-serving-runtime`` and ``eng-sre/runbooks`` both reduce to ``ESR`` — which made distinct
    spaces share a key and the router's reverse lookup ambiguous. The 6-hex suffix disambiguates
    (deterministically, so keys stay stable across imports)."""
    return _key(container, "SPACE") + _digest(container)[:6].upper()


# --- Notion --------------------------------------------------------------------
# Notion ids are dashed UUIDs; every page/block/database/data-source/user id is a
# deterministic UUID derived from a namespaced seed. Content is materialized into the
# Notion block tree by notion_blocks() and losslessly recovered by notion_blocks_to_text().


def _uuid_from(seed: str) -> str:
    h = _digest(seed)
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def notion_id(doc_id: str) -> str:
    """Stable dashed-UUID page/database id keyed on the doc_id (reversible via the app index)."""
    return _uuid_from("notion:" + doc_id)


def notion_block_id(doc_id: str, seq: int) -> str:
    return _uuid_from(f"notion-block:{doc_id}:{seq}")


def notion_user_id(email: str) -> str:
    return _uuid_from("notion-user:" + email)


def notion_data_source_id(db_doc_id: str) -> str:
    """The (single) data source id for a database — the 2025-09-03 model's query target."""
    return _uuid_from("notion-ds:" + db_doc_id)


def notion_rich_text(text: str) -> list[dict]:
    """A single-run Notion rich_text array carrying ``text`` verbatim as its plain_text."""
    return [
        {
            "type": "text",
            "text": {"content": text, "link": None},
            "annotations": {
                "bold": False,
                "italic": False,
                "strikethrough": False,
                "underline": False,
                "code": False,
                "color": "default",
            },
            "plain_text": text,
            "href": None,
        }
    ]


# Line prefix each block type carries, so notion_blocks_to_text inverts notion_blocks exactly.
_NOTION_PREFIX = {
    "heading_1": "# ",
    "heading_2": "## ",
    "heading_3": "### ",
    "bulleted_list_item": "- ",
    "numbered_list_item": "1. ",
    "paragraph": "",
}


def notion_blocks(doc_id: str, content: str) -> list[dict]:
    """Parse ``content`` into Notion block objects, one per line.

    Recognizes ``#``/``##``/``###`` headings, ``-``/``*`` bullets, ``N.`` numbered items;
    everything else (incl. blank lines) is a paragraph. Round-trips verbatim for the heading/
    bullet/paragraph forms via :func:`notion_blocks_to_text` (numbered items normalize to ``1. ``,
    as Notion itself does not store the ordinal)."""
    blocks: list[dict] = []
    for i, line in enumerate(content.split("\n")):
        btype, payload = "paragraph", line
        if line.startswith("### "):
            btype, payload = "heading_3", line[4:]
        elif line.startswith("## "):
            btype, payload = "heading_2", line[3:]
        elif line.startswith("# "):
            btype, payload = "heading_1", line[2:]
        elif line[:2] in ("- ", "* "):
            btype, payload = "bulleted_list_item", line[2:]
        elif re.match(r"^\d+\. ", line):
            btype, payload = "numbered_list_item", re.sub(r"^\d+\. ", "", line)
        blocks.append(
            {
                "object": "block",
                "id": notion_block_id(doc_id, i),
                "type": btype,
                "has_children": False,
                "archived": False,
                "in_trash": False,
                btype: {"rich_text": notion_rich_text(payload), "color": "default"},
            }
        )
    return blocks


def notion_blocks_to_text(blocks: list[dict]) -> str:
    """Recover the flat text from a block list (inverse of :func:`notion_blocks`)."""
    out = []
    for b in blocks:
        t = b["type"]
        text = "".join(rt["plain_text"] for rt in b[t].get("rich_text", []))
        out.append(_NOTION_PREFIX.get(t, "") + text)
    return "\n".join(out)


# --- Linear ----------------------------------------------------------------------
# Linear ids are dashed UUIDs (issues, teams, users, workflow states, labels, projects,
# cycles), so every one is a deterministic UUID derived from a namespaced seed — the same
# construction Notion uses above. Human-facing values (the team key, the issue identifier,
# the suggested branch name) follow Linear's own derivation rules instead.


def linear_id(doc_id: str) -> str:
    """Stable dashed-UUID issue id (reversible via the app index)."""
    return _uuid_from("linear:" + doc_id)


def linear_team_id(container: str) -> str:
    return _uuid_from("linear-team:" + container)


def linear_user_id(email: str) -> str:
    return _uuid_from("linear-user:" + email)


def linear_state_id(name: str, team: str = "") -> str:
    """Workflow states are per-TEAM in Linear — ENG's "Done" and DES's "Done" are different
    objects with different ids — so the team is part of the seed."""
    return _uuid_from(f"linear-state:{team}:{name or ''}")


def linear_label_id(name: str) -> str:
    return _uuid_from("linear-label:" + (name or ""))


def linear_project_id(name: str) -> str:
    return _uuid_from("linear-project:" + (name or ""))


def linear_cycle_id(name: str, team: str = "") -> str:
    """Cycles belong to a team, like workflow states."""
    return _uuid_from(f"linear-cycle:{team}:{name or ''}")


def linear_comment_id(comment_row_id: str) -> str:
    return _uuid_from("linear-comment:" + comment_row_id)


def linear_attachment_id(attachment_row_id: str) -> str:
    return _uuid_from("linear-attachment:" + attachment_row_id)


def linear_relation_id(relation_row_id: str) -> str:
    return _uuid_from("linear-relation:" + relation_row_id)


def linear_release_id(name: str) -> str:
    return _uuid_from("linear-release:" + (name or ""))


def linear_team_key(container: str) -> str:
    """A team's short key — the prefix its issue identifiers carry (``ENG-123``).

    NO hash suffix, unlike :func:`jira_project_key` / :func:`confluence_space_key`: the readable
    form reproduces the corpus's own prefixes exactly (``engineering`` -> ``ENG``,
    ``product-management`` -> ``PM``), so a served identifier matches the key written in the issue
    text and in every source that cites it. Two containers CAN collide on one key — the app index
    resolves that to the first team by name, and the team UUID always addresses it exactly."""
    return _key(container, "TEAM")


def linear_identifier(doc_id: str, team_key: str) -> str:
    """A synthesized ``TEAM-123`` identifier, for a corpus that carries no issue key of its own."""
    return f"{team_key}-{hnum(doc_id, 16, 6) % 9000 + 1}"


def linear_issue_number(identifier: str) -> int:
    """``Issue.number`` — the numeric half of the identifier, which is exactly how Linear
    defines it ("the issue's unique number, scoped to the issue's team")."""
    m = re.search(r"(\d+)\s*$", identifier or "")
    return int(m.group(1)) if m else 0


# Linear's priority scale, and the label it shows for each level.
LINEAR_PRIORITY_LABELS = {0: "No priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}


def linear_priority_label(priority) -> str:
    return LINEAR_PRIORITY_LABELS.get(priority if isinstance(priority, int) else 0, "No priority")


# Which of Linear's state *categories* a state name belongs to. Linear groups every workflow
# state into one of these six, and clients branch on the category rather than the name.
_LINEAR_STATE_TYPES = (
    ("triage", ("triage",)),
    ("canceled", ("cancel", "won't do", "wont do", "duplicate", "declined")),
    ("completed", ("done", "complete", "shipped", "closed", "resolved", "merged")),
    ("started", ("progress", "started", "review", "doing", "testing", "qa", "blocked")),
    ("backlog", ("backlog", "icebox", "someday")),
)


def linear_state_type(name: str) -> str:
    """Map a workflow-state name onto Linear's category. Unknown names fall to ``unstarted``,
    which is Linear's own bucket for "created but not begun" (Todo / Planned)."""
    n = (name or "").strip().lower()
    for state_type, needles in _LINEAR_STATE_TYPES:
        if any(needle in n for needle in needles):
            return state_type
    return "unstarted"


_LINEAR_STATE_COLORS = {
    "triage": "#f2994a",
    "backlog": "#bec2c8",
    "unstarted": "#e2e2e2",
    "started": "#f2c94c",
    "completed": "#5e6ad2",
    "canceled": "#95a2b3",
}


def linear_state_color(name: str) -> str:
    return _LINEAR_STATE_COLORS[linear_state_type(name)]


def linear_branch_name(identifier: str, title: str, assignee_email: str | None = None) -> str:
    """Linear's suggested git branch: ``<user>/<identifier>-<slugified title>``, lowercased and
    truncated the way the product does. With no assignee Linear drops the user segment."""
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (title or "").lower())).strip("-")[:40]
    slug = slug.rstrip("-")
    stem = "-".join(p for p in ((identifier or "").lower(), slug) if p)
    user = (assignee_email or "").split("@", 1)[0].replace(".", "").replace("_", "")
    return f"{user}/{stem}" if user else stem


def linear_url(identifier: str, title: str, org: str = "org") -> str:
    """The issue's web URL. Real Linear is ``https://linear.app/<workspace>/issue/<ID>/<slug>``."""
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (title or "").lower())).strip("-")[:60]
    return f"https://linear.app/{org}/issue/{identifier}/{slug}".rstrip("/")


# --- Fireflies ------------------------------------------------------------------
# The bench ships a transcript as ONE flat text blob with speaker-labeled, timestamped lines —
# not as structured per-sentence records — so the importer parses it (erb.parse_fireflies_
# transcript, which sits with the other conversation parsers) and the concatenation below is the
# EXACT inverse: `content` is DEFINED as fireflies_transcript_text(sentences), so full-text search
# and any RAG consumer read the meeting as one document while the sentence rows stay the single
# source of truth. Verified round-trip-exact over all 10,173 bench transcripts.


def fireflies_transcript_text(sentences) -> str:
    """The stored ``content`` for a transcript: its sentences, one per line, ``Speaker: text``.

    Inverse of :func:`backlot.importer.erb.parse_fireflies_transcript` — re-parsing this text yields
    the same sentences, so the pair is a fixed point (the ``notion_blocks`` /
    ``notion_blocks_to_text`` relationship, same problem, same solution).

    A sentence with an unknown speaker renders bare — the real API leaves ``speaker_name`` null when
    diarization produced no label, and an empty ``": "`` prefix must not become part of the text.
    """
    out = []
    for s in sentences:
        speaker = (s.get("speaker_name") if isinstance(s, dict) else s[0]) or ""
        text = (s.get("text") if isinstance(s, dict) else s[1]) or ""
        out.append(f"{speaker}: {text}" if speaker else text)
    return "\n".join(out)


# ~150 words/minute is ordinary conversational speech; used only to give a sentence an end_time
# when the transcript's own timestamps don't bound it (the last line of a meeting, or the 0.09%
# of bench sentences that carry no timestamp at all).
_WORDS_PER_SEC = 2.5


# A transcript that opens later than this clearly isn't counting elapsed time from zero — it is
# stamped with the wall clock ("[10:03:12] Ben Carter: …" for a meeting that started at 10:02).
_WALL_CLOCK_FLOOR = 600.0
# With no declared duration to check against, a reading this far past the previous one is a
# garbled hour field, not a real gap (the corpus's transcription noise is deliberate).
_MAX_PLAUSIBLE_GAP = 1800.0


def _fireflies_normalize_readings(sentences, duration_secs: float | None) -> None:
    """Make one transcript's raw timestamp readings a coherent elapsed-time sequence, in place.

    Two things in the corpus break a naive reading, both of which ``agents.md`` sets up: some
    transcripts stamp the WALL CLOCK rather than elapsed offsets, and some carry a garbled hour
    field on a late line ("57:00:12" in a 62-minute meeting). So the readings are rebased onto the
    transcript's own start when they plainly don't begin near zero, and any reading that then lands
    implausibly far ahead is discarded — dropped to ``None``, which makes it inherit the running
    clock rather than tear a 50-hour hole in the meeting.
    """
    readings = [s["start_time"] for s in sentences if s.get("start_time") is not None]
    if not readings:
        return
    base = min(readings)
    if base >= _WALL_CLOCK_FLOOR:  # wall-clock transcript -> rebase onto its own first reading
        for s in sentences:
            if s.get("start_time") is not None:
                s["start_time"] = float(s["start_time"]) - base
    ceiling = float(duration_secs) * 2 if duration_secs else None
    prev = 0.0
    for s in sentences:
        t = s.get("start_time")
        if t is None:
            continue
        t = float(t)
        limit = ceiling if ceiling is not None else prev + _MAX_PLAUSIBLE_GAP
        if t < 0 or t > limit:
            s["start_time"] = None
        else:
            prev = max(prev, t)


def fireflies_fill_times(sentences, duration_secs: float | None = None) -> None:
    """Fill each sentence's ``start_time``/``end_time`` in place (seconds).

    The transcript timestamps only the START of a line, and only PERIODICALLY (every 15-60s), so
    consecutive sentences routinely share one clock reading and the last has no end at all. The real
    API serves a contiguous non-overlapping timeline, so each run sharing a reading is spread evenly
    up to the next distinct one — every real timestamp stays the anchor of its run, and no two
    sentences claim the same instant. The final window is a speaking-rate estimate clamped to the
    meeting's duration; a sentence with no reading inherits the running clock.
    """
    if not sentences:
        return
    _fireflies_normalize_readings(sentences, duration_secs)
    clock = 0.0
    for s in sentences:
        if s.get("start_time") is None:
            s["start_time"] = clock
        else:
            s["start_time"] = float(s["start_time"])
        clock = max(clock, s["start_time"])
    n = len(sentences)
    i = 0
    while i < n:
        start = sentences[i]["start_time"]
        j = i + 1
        while j < n and sentences[j]["start_time"] <= start:
            j += 1  # the run of sentences anchored at this same reading
        if j < n:
            window_end = sentences[j]["start_time"]
        else:
            spoken = sum(len((s.get("text") or "").split()) for s in sentences[i:j])
            window_end = start + max(1.0, spoken / _WORDS_PER_SEC)
            if duration_secs and float(duration_secs) > start:
                window_end = min(window_end, float(duration_secs))
        step = (window_end - start) / (j - i)
        for k, s in enumerate(sentences[i:j]):
            s["start_time"] = start + step * k
            s["end_time"] = start + step * (k + 1)
        i = j


def fireflies_id(doc_id: str) -> str:
    """A transcript's API-facing id: the 24-character lowercase hex Fireflies serves.

    Synthesized rather than taken from the bench's ``meeting_id`` because that value is not
    unique and ``transcript(id:)`` looks a meeting up by this one (see the store schema).
    """
    return _digest("fireflies:" + doc_id)[:24]


def fireflies_user_id(email: str) -> str:
    """A workspace user's id. Fireflies' own ids are 24-character hex, like a transcript's; keyed
    on the address so it is stable and reversible through the app's startup index."""
    return _digest("fireflies-user:" + (email or ""))[:24]


# No `fireflies_speaker_id` here on purpose: Fireflies numbers speakers WITHIN one meeting, so an
# ordinal assigned by first appearance (which both importers do) is the whole definition. A hash of
# the name would be stable but would not be an ordinal, and nothing needs one.


def fireflies_transcript_url(transcript_id: str) -> str:
    """The meeting's page in the Fireflies web app — what the API returns as `transcript_url`."""
    return f"https://app.fireflies.ai/view/{transcript_id}"


def fireflies_media_url(transcript_id: str, kind: str) -> str:
    """The `audio_url` / `video_url` the API serves. The mock serves the URLs, not the media."""
    ext = "mp4" if kind == "video" else "mp3"
    return f"https://cdn.fireflies.ai/{kind}/{transcript_id}.{ext}"


def fireflies_meeting_link(doc_id: str) -> str:
    """The conferencing link the meeting was recorded from. Google Meet's code shape (xxx-xxxx-xxx)
    since `calendar_type` is google_calendar for a meeting the bench does not say otherwise about."""
    d = _digest("fireflies-meet:" + doc_id)
    letters = "abcdefghijklmnopqrstuvwxyz"
    code = "".join(letters[int(d[i : i + 2], 16) % 26] for i in range(0, 20, 2))
    return f"https://meet.google.com/{code[:3]}-{code[3:7]}-{code[7:10]}"


# Fireflies' own sentiment buckets and the analytics envelope shape. Out of scope to COMPUTE
# (the issue is explicit: analytics is served from stored or synthesized values, never derived
# from the text), so a transcript with no stored analytics gets a deterministic, self-consistent
# one: the three sentiment shares sum to 100 and the per-speaker durations sum to the meeting.
def fireflies_analytics(
    doc_id: str, speakers: list[dict] | None = None, duration_secs: float | None = None
) -> dict:
    """The `analytics` object: sentiments, per-speaker talk time, and categories.

    ``duration_pct`` is each speaker's share of the TALK TIME, not of the meeting's declared
    length. In real Fireflies the two are near-identical (a transcript covers its whole meeting)
    and the shares sum to ~100. A corpus transcript often does not span its declared duration —
    the bench's own timestamps stop early on most meetings — so dividing by the declared length
    would emit a set of shares summing to 4%, which reads as a bug in every consumer that charts
    it. Sharing out the talk time keeps the field's meaning and its arithmetic.
    """
    pos = 20 + hnum(doc_id, salt="ff-pos") % 51  # 20-70
    neg = hnum(doc_id, salt="ff-neg") % max(1, 101 - pos - 10)
    neutral = 100 - pos - neg
    total = float(sum(s.get("duration_secs") or 0.0 for s in speakers or []))
    spk = []
    for s in speakers or []:
        share = s.get("duration_secs")
        spk.append(
            {
                "name": s.get("name"),
                "duration": round(float(share), 2) if share is not None else None,
                "word_count": s.get("word_count"),
                "longest_monologue": s.get("longest_monologue"),
                "monologues_count": s.get("monologues_count"),
                "filler_words": s.get("filler_words"),
                "questions": s.get("questions"),
                "duration_pct": (
                    round(float(share) / total * 100, 2) if share is not None and total else None
                ),
            }
        )
    return {
        "sentiments": {"positive_pct": pos, "neutral_pct": neutral, "negative_pct": neg},
        "speakers": spk,
        "categories": {"questions": None, "date_times": None, "metrics": None, "tasks": None},
    }


# --- S3 -------------------------------------------------------------------------
# Credentials are derived deterministically from a caller's bearer token so the verifying
# router (backlot.auth.resolve_sigv4) and the signing clients (examples/tests) agree on the
# access-key/secret pair without any stored keypair. ETag is the real single-part MD5.

_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"  # RFC 4648 base32 alphabet (AK is [A-Z2-7])
_SK_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _base_n(hex_digest: str, alphabet: str, length: int) -> str:
    n = int(hex_digest, 16)
    base = len(alphabet)
    out = []
    for _ in range(length):
        n, rem = divmod(n, base)
        out.append(alphabet[rem])
    return "".join(out)


def s3_access_key_id(token: str) -> str:
    """A stable ``AKIA``-prefixed 20-char access key id for a bearer token."""
    body = _base_n(_digest("s3-ak:" + token), _B32, 16)
    return "AKIA" + body


def s3_secret_access_key(token: str) -> str:
    """A stable 40-char secret access key for a bearer token."""
    d = _digest("s3-sk:" + token) + _digest("s3-sk2:" + token)
    return _base_n(d, _SK_ALPHABET, 40)


def s3_etag(doc_id: str, content: str) -> str:
    """The quoted MD5 hex ETag S3 returns for a single-part object (MD5 of the body)."""
    return '"' + hashlib.md5(content.encode("utf-8")).hexdigest() + '"'


def s3_iso(ts: int) -> str:
    """S3 ListObjectsV2 LastModified, e.g. 2024-04-05T17:00:00.000Z."""
    return rfc3339_millis(ts)


def s3_http_date(ts: int) -> str:
    """The Last-Modified response header, RFC 1123: Fri, 05 Apr 2024 17:00:00 GMT."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
