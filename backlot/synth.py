"""Deterministic synthesis of structural metadata.

A corpus may carry no more than ``{doc_id, source_type, title, content}``. Every
structural field a real API returns (ids, timestamps, users, keys, ...) is derived
here from ``sha256(seed)`` so responses are stable and self-consistent across calls
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


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def hnum(seed: str, start: int = 0, length: int = 8, salt: str = "") -> int:
    """A stable non-negative integer derived from a hex slice of the digest."""
    h = _digest(salt + seed) if salt else _digest(seed)
    start %= 64
    return int(h[start : start + length] or h[:length], 16)


def pick(seed: str, seq, salt: str = ""):
    """Deterministically choose one element of ``seq`` for this doc."""
    seq = list(seq)
    if not seq:
        return None
    return seq[hnum(seed, salt=salt) % len(seq)]


# --- timestamps -----------------------------------------------------------------


def epoch(seed: str, base: int = BASE_EPOCH, span: int = TIME_RANGE) -> int:
    """Stable unix-second timestamp within [base, base+span)."""
    return base + (hnum(seed, 0, 8) % span)


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


def gmail_id(seed: str, salt: str = "msg") -> str:
    """An opaque 16-hex token. Used for attachment ids and Slack's ``client_msg_id``, where the
    value is never parsed — so it deliberately spans the full 64-bit range. A *message* id is
    parsed by Gmail and must not; use ``gmail_message_id``."""
    return hnum(seed, salt=salt, length=16).__format__("016x")


# Gmail parses a message id as a signed 64-bit integer, so 2**63 is the ceiling. Measured against
# the live API: `7fffffffffffffff` resolves (404, a real id shape) while `8000000000000000` and
# `ffffffffffffffff` are refused with 400 "Invalid id value". Unmasked, half of any digest-derived
# id lands above the line — measured, 278,278 of one corpus's 556,238 messages.
GMAIL_ID_MAX = 2**63


def gmail_message_id(key: str) -> str:
    """The served id for a Gmail message or thread: 16 lowercase hex digits below ``GMAIL_ID_MAX``.

    Threads share this space, as they do in real Gmail — a thread key is the root message's
    ``seed``, so a single-message thread reports the same value for ``id`` and ``threadId``, which
    is exactly what the real API does. That is also why the stored ``served_id`` column and a
    re-hash of the thread key resolve both: a message's own id is a column read
    (``store.gmail_by_served_id``), while its ``threadId`` re-derives this same function over
    ``thread_id or seed`` rather than reading the root row (see ``routers.google._gmail_ids``)."""
    return f"{hnum(key, salt='msg', length=16) % GMAIL_ID_MAX:016x}"


def drive_folder_id(container: str) -> str:
    return "0A" + _digest("folder:" + container)[:26]


def gdrive_file_id(seed: str) -> str:
    """A Drive file's served id (#51, task 12): 33 characters, base64url alphabet
    (``[A-Za-z0-9_-]``), starting with ``1`` — the shape of a modern real Drive file id.
    Reversible via the stored ``gdrive_files.served_id`` column (see
    ``store.gdrive_by_served_id``), not hashed at serve time.

    Derived from the RAW DIGEST BYTES, unlike ``drive_folder_id``'s hex slice: hex spans only 16
    of the 64 available symbols, so it reads noticeably unlike a real id. 24 digest bytes
    base64url-encode to exactly 32 characters with no padding (24 * 8 / 6 = 32 evenly), so
    ``"1"`` plus that is 33 characters — matching a real file id's length exactly.

    A separate id space from ``drive_folder_id``: every folder id starts ``0A``, every file id
    starts ``1``, so the two can never collide even though a folder id is also a valid
    ``files.get`` argument on real Drive (see ``routers.google._drive_folder_name_by_id``)."""
    digest = hashlib.sha256(("gdrive-file:" + seed).encode("utf-8")).digest()
    return "1" + base64.urlsafe_b64encode(digest[:24]).decode("ascii")


# The size of the per-repo space a github issue/PR number is drawn from (1..GITHUB_NUMBER_RANGE).
# Exported (not a private literal inside github_number) so importer.byo's probe walk and
# exhaustion check -- both bounded to "every number this function can produce" -- read this SAME
# constant rather than an uncoupled copy: bumping it here immediately widens both without a
# second edit to find and keep in sync.
GITHUB_NUMBER_RANGE = 90_000


def github_number(seed: str) -> int:
    return hnum(seed, 0, 8) % GITHUB_NUMBER_RANGE + 1


GITHUB_COMMENT_ID_MIN = 1_000_000_000
GITHUB_COMMENT_ID_RANGE = 9_000_000_000


def github_comment_id(comment_id: str) -> int:
    """The SEED for a comment's own id — 10 digits, as real GitHub's are.

    Only a seed: the id served is assigned at import and probed for uniqueness from here (see
    ``backlot.importer.byo``). A hash alone cannot be relied on, because a comment's `url` resolves
    through its id and any fixed range collides by the birthday bound long before a corpus runs out
    of comments. Wide anyway, so the probe almost never has to run.
    """
    return GITHUB_COMMENT_ID_MIN + hnum(comment_id, 0, 12) % GITHUB_COMMENT_ID_RANGE


def jira_numeric_id(seed: str) -> int:
    return 10_000 + hnum(seed, 8, 8) % 900_000


# The size of the per-project space `jira_key_number` draws from (1..JIRA_KEY_NUMBER_RANGE). A
# project with more issues than this has run out of that space, which is a corpus-scale problem
# and not something a silent duplicate should paper over — see importer.byo's
# `_assign_jira_number`. Exported (not a private literal inside `jira_key_number`) for the same
# reason `GITHUB_NUMBER_RANGE` is: the importer's probe walk and exhaustion check both read this
# SAME constant rather than an uncoupled copy.
JIRA_KEY_NUMBER_RANGE = 9_000


def jira_key_number(seed: str) -> int:
    """The numeric suffix of `jira_key(seed, project_key)`, split out so the two cannot drift.

    A key's PREFIX is a fact about the container (its project), not this row — a corpus's own key
    always wins (see importer.byo) — but the SUFFIX is a fact about the row alone, and is what a
    served id can be assigned and probed on within one project (see store.SERVED_ID): the prefix
    is under-constrained (two containers may share one — a corpus-provided prefix is checked 1:1
    against its project in importer.byo, but `jira_project_key`'s OWN digest can still collide
    with an unrelated project's provided one at ~1/16.7M odds; see its own docstring), so a probe
    scoped by project must never include it.
    """
    return hnum(seed, 16, 6) % JIRA_KEY_NUMBER_RANGE + 1


def jira_key(seed: str, project_key: str) -> str:
    return f"{project_key}-{jira_key_number(seed)}"


HUBSPOT_ID_MIN = 1_000_000_000
HUBSPOT_ID_RANGE = 9_000_000_000


def hubspot_record_id(seed: str) -> str:
    """The SEED for a record's own id -- a numeric string (e.g. "5790939450"), as real HubSpot's
    are -- see ``backlot.importer.byo._Loader._assign_hubspot_id`` for the assignment that actually
    resolves it to a served, unique value.

    Only a seed, unlike confluence_id's neighbours gmail/notion: this space is 9,000,000,000
    values, wide enough to look safe, but measured at a corpus this project actually generates
    (500k documents) it still collides ~16 times by the birthday bound -- so it gets confluence's
    probe, not gmail's/notion's bare-seed shape (#51)."""
    return str(HUBSPOT_ID_MIN + hnum(seed, 0, 10) % HUBSPOT_ID_RANGE)


def hubspot_assoc_type_id(from_type: str, to_type: str) -> int:
    """Association type id for one direction of a type pair. Real HubSpot uses well-known ids per
    direction (contact->company is not company->contact), so this is direction-sensitive too —
    derived from the ordered pair rather than being a shared constant."""
    return hnum(f"{from_type}>{to_type}", 0, 6) % 900 + 1


CONFLUENCE_ID_MIN = 100_000
CONFLUENCE_ID_RANGE = 9_000_000


def confluence_id(seed: str) -> int:
    """The SEED for a page's own id — see ``backlot.importer.byo._Loader._assign_confluence_id``
    for the assignment that actually resolves it to a served, unique value."""
    return CONFLUENCE_ID_MIN + hnum(seed, 24, 8) % CONFLUENCE_ID_RANGE


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
    """A project key meant to be unique per container: the readable word-initials prefix (see
    :func:`_key`) plus a short hash of the full name, so two SYNTHESIZED keys practically never
    collide with each other (the router's reverse key->project lookup + the derived issue keys
    stay unambiguous). Deterministic, valid Jira shape (uppercase letter start, uppercase alnum).

    NOT guaranteed unique against a corpus-PROVIDED prefix, though: a project with no provided
    keys of its own is never checked against `importer.byo`'s 1:1 prefix<->project enforcement
    (that only runs for prefixes a corpus actually writes), so this digest could in principle
    equal another project's provided prefix — or, symmetrically, another KEYLESS project's own
    digest, at the identical ~1/16.7M order (6 hex digits). See the residual documented on
    `store.py`'s `idx_jira_served` schema comment and `importer.byo`'s `resolve_jira_numbers`."""
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


def notion_id(seed: str) -> str:
    """Stable dashed-UUID page/database id keyed on the seed (reversible via the stored
    notion_pages.served_id column -- see store.notion_by_served_id)."""
    return _uuid_from("notion:" + seed)


def notion_block_id(seed: str, seq: int) -> str:
    return _uuid_from(f"notion-block:{seed}:{seq}")


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


def notion_blocks(seed: str, content: str) -> list[dict]:
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
                "id": notion_block_id(seed, i),
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


def linear_id(seed: str) -> str:
    """Stable dashed-UUID issue id (reversible via the app index)."""
    return _uuid_from("linear:" + seed)


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


def linear_identifier(seed: str, team_key: str) -> str:
    """A synthesized ``TEAM-123`` identifier, for a corpus that carries no issue key of its own."""
    return f"{team_key}-{hnum(seed, 16, 6) % 9000 + 1}"


def linear_issue_number(identifier: str) -> int:
    """``Issue.number`` — the numeric half of the identifier, which is exactly how Linear
    defines it ("the issue's unique number, scoped to the issue's team")."""
    m = re.search(r"(\d+)\s*$", identifier or "")
    return int(m.group(1)) if m else 0


# Linear's priority scale, and the label it shows for each level.
LINEAR_PRIORITY_LABELS = {0: "No priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}


def linear_priority_label(priority) -> str:
    return LINEAR_PRIORITY_LABELS.get(priority if isinstance(priority, int) else 0, "No priority")


# The spellings a corpus may use for a priority, mapped onto the scale above. `P0`-`P3` is included
# because it is what issue trackers outside Linear write, and the BYO schema accepts it.
_LINEAR_PRIORITY_VALUES = {
    "p0": 1,
    "p1": 2,
    "p2": 3,
    "p3": 4,
    "urgent": 1,
    "high": 2,
    "medium": 3,
    "low": 4,
    "none": 0,
    "no priority": 0,
}


def linear_priority(value) -> int | None:
    """Normalize a corpus's priority onto Linear's 0-4 scale.

    Unrecognized text becomes 0 ("No priority"), which is what Linear itself stores for an unset
    priority; a missing value stays None. Lives here, beside the labels it is the inverse of, so
    both importers normalize identically without either depending on the other.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if 0 <= int(value) <= 4 else 0
    s = str(value).strip().lower()
    if s.isdigit() and 0 <= int(s) <= 4:
        return int(s)
    return _LINEAR_PRIORITY_VALUES.get(s, 0)


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
# A corpus may carry a transcript as ONE flat text blob with speaker-labeled, timestamped lines
# rather than as structured per-sentence records, in which case the importer parses it (each
# importer keeps its own parser, with the rest of its conversation parsing) and the text below is the
# EXACT inverse: `content` is DEFINED as fireflies_transcript_text(sentences), so full-text search
# and any RAG consumer read the meeting as one document while the sentence rows stay the single
# source of truth. Verified round-trip-exact over all 10,173 transcripts of a real corpus.


# A speaker label: 1-4 name-ish words, optionally bracketed ("[Maya]"), with a trailing "(Role)" or
# ", Role" stripped off ("Ari (Redwood AE)"). A line whose prefix is not this shape is continuation
# text, which is what keeps a sentence containing a colon from minting a speaker.
_SPEAKER = r"[A-Za-z@][\w.'’\-]*(?: +[A-Za-z0-9][\w.'’\-]*){0,3}"
_CLOCK = r"\d{1,2}:\d{2}(?::\d{2})?"
# An optional leading timestamp in the forms transcripts conventionally use — "[00:12] ", "(00:12) ",
# "00:12 - " — then the speaker, then the colon.
_TRANSCRIPT_LINE = re.compile(
    rf"^\s*(?:(?:\[(?P<b>{_CLOCK})\]|\((?P<p>{_CLOCK})\)|(?P<r>{_CLOCK}))\s*(?:[-–—]\s*)?)?"
    rf"(?:\[(?P<name>{_SPEAKER})\]|(?P<name2>{_SPEAKER}))(?: *[(,][^)]*\)?)?:[ \t]*(?P<text>.*)$"
)


def _clock_secs(clock: str) -> float:
    p = [int(x) for x in clock.split(":")]
    return float(p[0] * 60 + p[1] if len(p) == 2 else p[0] * 3600 + p[1] * 60 + p[2])


def parse_transcript_text(text: str) -> list[dict]:
    """A ``Speaker: text`` transcript body -> ``[{speaker_name, text, start_time}]``.

    The inverse of :func:`fireflies_transcript_text`, and defined as such: `content` is DEFINED as
    the concatenation of the sentences, so whichever of the two a corpus supplies, the pair has to
    agree. A line that names no speaker continues the sentence above it (and a body that opens with
    one becomes an unattributed sentence rather than being dropped, or re-deriving the text would
    lose it). A leading clock, if present, becomes ``start_time``.

    Only the conventions the record format documents are recognized. An importer whose source
    dataset writes transcripts some other way parses them itself — see
    ``backlot.importer.erb.parse_fireflies_transcript``, which adds that dataset's own line formats
    and its auto-notes preamble, neither of which belongs in a shared inverse.
    """
    if not (text or "").strip():
        return []
    out: list[dict] = []
    for line in text.split("\n"):
        m = _TRANSCRIPT_LINE.match(line)
        # A `scheme://` is not a speaker: the label pattern happily matches "see https" in
        # "see https://example.com", which would split a sentence at a URL and mint a speaker
        # nobody said. The text after a real speaker's colon never opens with a slash pair.
        if m and m.group("text").startswith("//"):
            m = None
        if m:
            clock = m.group("b") or m.group("p") or m.group("r")
            out.append(
                {
                    "speaker_name": (m.group("name") or m.group("name2")).strip(),
                    "text": m.group("text"),
                    "start_time": _clock_secs(clock) if clock else None,
                }
            )
        elif out:
            out[-1]["text"] += "\n" + line
        else:
            out.append({"speaker_name": None, "text": line, "start_time": None})
    for s in out:
        s["text"] = s["text"].rstrip()
    return out


def fireflies_transcript_text(sentences) -> str:
    """The stored ``content`` for a transcript: its sentences, one per line, ``Speaker: text``.

    Inverse of an importer's transcript parser — re-parsing this text yields
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
# of real sentences measured to carry no timestamp at all).
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


def fireflies_id(seed: str) -> str:
    """A transcript's API-facing id: the 24-character lowercase hex Fireflies serves.

    Synthesized rather than reused from a corpus's own meeting id, which is not required to be
    unique — ``transcript(id:)`` looks a meeting up by this one (see the store schema).
    """
    return _digest("fireflies:" + seed)[:24]


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


def fireflies_meeting_link(seed: str) -> str:
    """The conferencing link the meeting was recorded from. Google Meet's code shape (xxx-xxxx-xxx)
    since `calendar_type` is google_calendar for a meeting the corpus does not say otherwise about."""
    d = _digest("fireflies-meet:" + seed)
    letters = "abcdefghijklmnopqrstuvwxyz"
    code = "".join(letters[int(d[i : i + 2], 16) % 26] for i in range(0, 20, 2))
    return f"https://meet.google.com/{code[:3]}-{code[3:7]}-{code[7:10]}"


def fireflies_speaker_stats(sentences) -> list[dict]:
    """Per-speaker talk time and word counts, computed from the sentences themselves — the only
    part of `analytics` a transcript actually supports (sentiment is not derivable, see
    :func:`fireflies_analytics`, which consumes this). Shared by both importers."""
    agg: dict[str, dict] = {}
    for s in sentences:
        name = s.get("speaker_name") or None
        a = agg.setdefault(
            name or "",
            {
                "name": name,
                "duration_secs": 0.0,
                "word_count": 0,
                "monologues_count": 0,
                "longest_monologue": 0.0,
            },
        )
        span = max(0.0, float(s.get("end_time") or 0) - float(s.get("start_time") or 0))
        a["duration_secs"] += span
        a["word_count"] += len((s.get("text") or "").split())
        a["monologues_count"] += 1
        a["longest_monologue"] = max(a["longest_monologue"], span)
    for a in agg.values():
        a["duration_secs"] = round(a["duration_secs"], 2)
        a["longest_monologue"] = round(a["longest_monologue"], 2)
    return list(agg.values())


# Fireflies' own sentiment buckets and the analytics envelope shape. Out of scope to COMPUTE
# (the issue is explicit: analytics is served from stored or synthesized values, never derived
# from the text), so a transcript with no stored analytics gets a deterministic, self-consistent
# one: the three sentiment shares sum to 100 and the per-speaker durations sum to the meeting.
def fireflies_analytics(
    seed: str, speakers: list[dict] | None = None, duration_secs: float | None = None
) -> dict:
    """The `analytics` object: sentiments, per-speaker talk time, and categories.

    ``duration_pct`` is each speaker's share of the TALK TIME, not of the meeting's declared
    length. In real Fireflies the two are near-identical (a transcript covers its whole meeting)
    and the shares sum to ~100. A corpus transcript often does not span its declared duration —
    real transcripts' timestamps stop early on most meetings — so dividing by the declared length
    would emit a set of shares summing to 4%, which reads as a bug in every consumer that charts
    it. Sharing out the talk time keeps the field's meaning and its arithmetic.
    """
    pos = 20 + hnum(seed, salt="ff-pos") % 51  # 20-70
    neg = hnum(seed, salt="ff-neg") % max(1, 101 - pos - 10)
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


def s3_etag(seed: str, content: str) -> str:
    """The quoted MD5 hex ETag S3 returns for a single-part object (MD5 of the body)."""
    return '"' + hashlib.md5(content.encode("utf-8")).hexdigest() + '"'


def s3_iso(ts: int) -> str:
    """S3 ListObjectsV2 LastModified, e.g. 2024-04-05T17:00:00.000Z."""
    return rfc3339_millis(ts)


def s3_http_date(ts: int) -> str:
    """The Last-Modified response header, RFC 1123: Fri, 05 Apr 2024 17:00:00 GMT."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
