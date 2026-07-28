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
    return hnum(doc_id, salt=salt, length=16).__format__("016x")


def drive_file_id(doc_id: str) -> str:
    # Drive ids are opaque; reuse the doc_id so the id is reversible for get/export.
    return doc_id


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
    return [{"type": "text", "text": {"content": text, "link": None},
             "annotations": {"bold": False, "italic": False, "strikethrough": False,
                             "underline": False, "code": False, "color": "default"},
             "plain_text": text, "href": None}]


# Line prefix each block type carries, so notion_blocks_to_text inverts notion_blocks exactly.
_NOTION_PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
                  "bulleted_list_item": "- ", "numbered_list_item": "1. ", "paragraph": ""}


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
        blocks.append({
            "object": "block", "id": notion_block_id(doc_id, i),
            "type": btype, "has_children": False, "archived": False, "in_trash": False,
            btype: {"rich_text": notion_rich_text(payload), "color": "default"},
        })
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


def linear_team_key(container: str) -> str:
    """A team's short key — the prefix its issue identifiers carry (``ENG-123``).

    Deliberately the plain word-initials form with NO hash suffix, unlike
    :func:`jira_project_key` / :func:`confluence_space_key`. Those add one because their keys
    would otherwise collide across containers; here the readable form is worth more, because it
    reproduces the corpus's own prefixes exactly (``engineering`` -> ``ENG``,
    ``product-management`` -> ``PM``, ``design`` -> ``DES``) so a served identifier matches the
    key written in the issue text and in every other source that cites it. Two containers CAN
    collide on one key; the app index resolves a colliding key to the first team by name, and
    the team's UUID always addresses it unambiguously."""
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


_LINEAR_STATE_COLORS = {"triage": "#f2994a", "backlog": "#bec2c8", "unstarted": "#e2e2e2",
                        "started": "#f2c94c", "completed": "#5e6ad2", "canceled": "#95a2b3"}


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


# --- S3 -------------------------------------------------------------------------
# Credentials are derived deterministically from a caller's bearer token so the verifying
# router (app.auth.resolve_sigv4) and the signing clients (examples/tests) agree on the
# access-key/secret pair without any stored keypair. ETag is the real single-part MD5.

_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"      # RFC 4648 base32 alphabet (AK is [A-Z2-7])
_SK_ALPHABET = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")


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

