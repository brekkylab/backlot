"""Load a Bring-Your-Own (BYO) corpus from JSONL into the mock DB.

Serve *any* document set through every vendor API this mock speaks.
Each line is one document:

    {
      "source_type": "confluence",        # required: one of the served source types
                                          #   (slack|gmail|google_drive|github|jira|confluence|
                                          #    notion|s3|hubspot|linear|fireflies)
      "title": "Onboarding guide",         # required except for slack (messages have no title)
      "content": "Full text...",            # required
      "doc_id": "my-123",                  # optional (default: dsid_<sha256(src+title+content)>)
      "space": "handbook",                 # the grouping unit, named per service: slack/fireflies
                                             #   "channel", gmail "mailbox", google_drive "folder",
                                             #   github "repo", jira "project", confluence "space",
                                             #   notion "teamspace", s3 "bucket", hubspot
                                             #   "object_type", linear "team" (default: source_type)
      "group": "people",                   # optional ACL group owning that unit (default: slug(unit))
      "author_email": "ava@acme.com",      # optional author/sender/owner
      "author_groups": ["people","eng"],   # optional groups the author belongs to
      "visibility": "public",              # optional: public|group|private (default: public)
      "readers": ["ava@acme.com","eng"],    # optional explicit reader principals (overrides visibility)
      "subtype": "page",                    # optional: drive document|spreadsheet|presentation;
                                             #   github issue|pull_request; confluence page|blogpost
      "parent": "doc-id-of-parent",         # optional: hierarchy (confluence child page, jira subtask)
      "labels": ["eng","runbook"],          # optional facets -> meta.labels
      "meta": {"issuelinks": [...]},        # optional per-source structured extras (merged into meta JSON)
      "comments": [                         # optional: comments on this doc (jira/confluence/github/drive)
        {"content": "LGTM", "author_email": "rev@acme.com"}
      ],
      "created": "2026-03-01T09:00:00Z",    # optional creation time (epoch seconds or ISO 8601)
      "updated": 1740900000,                # optional modified time (drive/github/jira/confluence)
      "author_name": "Ava Chen",            # optional display name -> the owner's served name
      "replies": [                          # slack only: threaded replies — full messages, not just text
        {"content": "on it", "author_email": "bob@acme.com",
         "reactions": [{"name": "eyes", "count": 1}]}
      ],
      "messages": [                         # gmail only: the thread's later messages
        {"content": "On it.", "author_email": "ava@acme.com", "message_id": "<b@acme>"}
      ]
    }

Child rows are per-source, because a document's child means something different in each API:
slack `replies` are threaded replies, gmail `messages` are further RFC822 messages each with its
own sender and Message-ID, fireflies `sentences` are utterances with a speaker and timing, and
`comments` are a comment API's rows. The record is always the root (seq 0), each child takes the
next sequence number, and children inherit the root's container and ACL.

ACL per doc: `readers` win (an address is a user, anything else a group); else `private` -> author
only, `group` -> the container's group, default -> org-wide. Group membership is the union of each
author's `author_groups` and the group of every container they authored in. Pass ``--roster`` to
state the principals instead of deriving them from the records (:func:`load_roster`), which is how
a converted corpus carries a roster it already knows.

Every record is validated against its per-service JSON Schema first, so a bad corpus never
half-loads; ``--dry-run`` reports problems without touching the DB.

Usage:  backlot import path/to/corpus.jsonl [--append | --dry-run] [--roster r.yaml]
        (or run this module directly: ``python -m backlot.importer.byo`` takes the same flags)
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

import yaml

from backlot import store, synth
from backlot.config import Settings, get_settings, infer_org
from backlot.validation import record_errors


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# The sources whose served id is PROBED, mapped to the record field a corpus states one in.
#
# All four defer their assignment to the end of the load, for one reason: a provided value must
# claim its spelling ahead of every synthesized one, corpus-wide. A record arriving early cannot
# know what a later one will provide, so streaming the probe would let a keyless row take the value
# a later record asks for outright -- failing on the primary key, with a message about a constraint
# rather than about the corpus.
#
# The field is named the way the VENDOR names that id, qualified when the vendor calls it plain
# `id` so it cannot be read as the corpus's own `doc_id` -- the same rule fireflies' own
# `transcript_id` follows.


# source_type -> whether a record of it STATES the identity it is served at, outside the four
# DEFERRED_ID sources (whose stated ids are claimed corpus-wide by their own pass). fireflies
# states one only when the record carries `transcript_id`; an s3 object always does, since
# (bucket, key) IS the object's address and both fields are required.
#
# The distinction is what an --append can be refused on. A synthesized id is a pure function of
# the dataset id, so a re-imported record lands on the value it had and updates its own row; a
# STATED one landing on a value an earlier import already holds is ambiguous between that and a
# second document claiming an id it does not own -- and writing it would replace a document and
# hand its readers to this one.
def _states_own_id(src: str, rec: dict) -> bool:
    if src == "s3":
        return True
    if src == "fireflies":
        # The schema's own spelling only: `meta.transcript_id` is ordinary meta content, stripped
        # before extras are seeded, exactly as `meta.number` is for github.
        return bool(rec.get("transcript_id"))
    return False


DEFERRED_ID = {
    "github": "number",
    "jira": "key",
    "confluence": "content_id",
    "hubspot": "record_id",
}

# The separator between a child row's parent key and its position. `_settle` recomposes the id in
# SQL when the parent moves, so this literal is the one definition both sides read -- pinned by
# test_byo_a_comment_id_follows_its_parents_settled_key.
_CHILD_SEP = "::c"


def _child_id(parent_key, seq: int) -> str:
    """A child row's own id: its parent's SERVED key plus its position in the thread."""
    return f"{parent_key}{_CHILD_SEP}{seq}"


# Distinguishes "no claim on this value" from "claimed by a row whose dataset id is unknown"
# (one carried over from an earlier run, where there is no dataset id left to record). A plain
# `.get()` default of None would conflate the two and let a re-import silently re-claim.
_UNCLAIMED = object()


def _doc_id(rec: dict) -> str:
    if rec.get("doc_id"):
        return str(rec["doc_id"])
    h = hashlib.sha256(
        (rec["source_type"] + rec.get("title", "") + rec["content"]).encode()
    ).hexdigest()
    return "dsid_" + h[:32]


def _user_token(email: str) -> str:
    return "usr-" + hashlib.sha256(("tok:" + email).encode()).hexdigest()[:20]


def _display_name(email: str) -> str:
    return email.split("@")[0].replace(".", " ").replace("_", " ").title()


def _j(v):
    return json.dumps(v, sort_keys=True) if isinstance(v, (list, dict)) else v


def _principal(pid: str) -> tuple[str, str]:
    """A `readers` entry -> ``(principal_type, id)``.

    The shorthand is unchanged — an address is a user, anything else a group — but a `user:` /
    `group:` / `org:` prefix states the type outright. Needed because the shorthand cannot name the
    ORG principal at all, so without a prefix a document that is org-readable *and* names its
    owners has no spelling: `readers` would replace the org grant rather than add to it."""
    for t in ("user", "group", "org"):
        if pid.startswith(t + ":"):
            return t, pid[len(t) + 1 :]
    return ("user", pid) if "@" in pid else ("group", pid)


def _time_given(v) -> bool:
    """Whether the corpus wrote a time in this field at all, as opposed to leaving it
    out. Told apart from an unreadable one, which ``_epoch`` also answers None for: a
    field the author filled in with something this importer cannot read is a typo to
    report, not a default to take."""
    return v is not None and v != ""


def _seconds(n):
    """A number as unix seconds, or None if it is not a second this mock can serve.

    Two ways a number gets here without being a time. ``inf`` and ``nan`` have no
    integer form at all — `json.loads` accepts both as bare literals, so a corpus can
    hand them over and `int()` raises rather than returning anything. And a finite
    second outside `datetime`'s year 1..9999 cannot be rendered: the servers date a row
    through `datetime.fromtimestamp`, so milliseconds written where seconds belong
    imports clean and then raises at SERVE time, well after the import said it worked.
    Asked of the same function the servers use rather than against a hardcoded bound."""
    from datetime import datetime, timezone

    try:
        sec = int(n)
    except (ValueError, OverflowError):  # nan, inf
        return None
    try:
        datetime.fromtimestamp(sec, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return sec


def _epoch_field(v, where, field):
    """A corpus-supplied time, refusing a value that is filled in but unreadable.

    Used wherever an absent time gets a default in its place — a hash of the doc_id, the
    previous message's second, the root's clock plus an hour. Taking that default for a
    typo means the record loads with a time nobody wrote and no way to tell it apart from
    one that was left blank on purpose, which is the whole reason a reply's clock refuses.
    Fields whose absence stores NULL keep plain ``_epoch``: nothing is substituted there,
    so nothing is disguised."""
    sec = _epoch(v)
    if sec is None and _time_given(v):
        raise SystemExit(
            f"{where}: {field} is not a time this importer can read "
            f"(got {v!r}; write epoch seconds or ISO 8601)"
        )
    return sec


def _message_second(stated, default: int) -> int:
    """A child message's second: the one the corpus stated, else the caller's default.

    A plain ``or`` reads the same until the stated second is 0 — 1970-01-01T00:00:00Z, which a
    corpus can legitimately write — and then substitutes the default for a time the author
    actually wrote."""
    return default if stated is None else stated


def _epoch(v):
    """Parse a BYO time (epoch seconds int/float, or ISO 8601 string) -> unix seconds.

    Returns None for a missing/unparseable value, so the router falls back to the
    deterministic synthesized timestamp."""
    if not _time_given(v):
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return _seconds(v)
    from datetime import datetime

    s = str(v).strip().replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        pass
    # Epoch seconds written as a string — `"1770746760"`, or the `<sec>.<frac>` a Slack
    # ts takes, which is the form an `edited.ts` next door is already in. ISO is tried
    # first, so an 8-digit basic-format date stays a date rather than becoming a second.
    try:
        return _seconds(float(s))
    except ValueError:
        return None


def _thread_seconds(where, root_sec, root_written, replies):
    """Every second in a slack thread — the root's and each reply's — resolved together,
    before any of the rows are written. Returns ``(root_second, [reply seconds])``.

    Ordering is judged against clocks the corpus actually supplied. A root with no
    `created` of its own holds a second hashed from its doc_id, which is not a fact
    about the thread, and using it as the anchor made the import turn on that hash: for
    a reply dated inside the synthesizer's own window the hash lands after the reply
    date about half the time, so the same corpus loads or dies depending on the root's
    doc_id, and the refusal quotes a second that appears nowhere in the corpus. When it
    did load, the served thread had a root years before its own reply. Such a root is
    re-grounded on the first reply that carries a clock instead.

    A clockless reply still lands one second after the message before it, so a thread
    that supplies no clocks at all resolves exactly where root+position always put it.
    Re-grounding cannot change an existing corpus either: a reply could not carry
    `created` at all until this branch added it, `replies.items` being closed.
    """
    reps = replies or []
    written = []
    for i, rep in enumerate(reps, start=1):
        v = rep.get("created")
        sec = _epoch(v)
        if sec is None and _time_given(v):
            # A filled-in field this importer cannot read is refused rather than
            # defaulted: taking the default silently reinstates the metronome the field
            # exists to replace, and the second that lands is indistinguishable from
            # what a corpus that never wrote a clock gets.
            raise SystemExit(
                f"{where}: reply {i}: created is not a time this importer can read "
                f"(got {v!r}; write epoch seconds or ISO 8601)"
            )
        written.append(sec)

    if not root_written:
        first = next((i for i, s in enumerate(written) if s is not None), None)
        if first is not None:
            # One second for each clockless reply ahead of it, and one more for the root.
            root_sec = written[first] - (first + 1)

    out = []
    prev, defaulted = root_sec, False
    for i, sec in enumerate(written, start=1):
        if sec is None:
            prev, defaulted = prev + 1, True
            out.append(prev)
            continue
        if sec <= prev:
            if defaulted:
                # The second it collides with is one this importer chose, not one the
                # author wrote, so say that rather than quoting it as a fact about the
                # corpus. Two adjacent seconds cannot both hold a message: a Slack ts is
                # identity as well as clock.
                raise SystemExit(
                    f"{where}: reply {i}: created must be after the message before it "
                    f"(got {reps[i - 1].get('created')!r}), but reply {i - 1} carries no "
                    f"created of its own and took the next free second, {prev}. Give "
                    f"reply {i - 1} a created too, or move this one later."
                )
            raise SystemExit(
                f"{where}: reply {i}: created must be after the message before it "
                f"(got {reps[i - 1].get('created')!r}, the previous message is at {prev})"
            )
        prev, defaulted = sec, False
        out.append(sec)
    return root_sec, out


def _service_columns(
    src,
    ex,
    subtype,
    parent_id,
    seed,
    thread_id,
    seq,
    org_domain,
    created=None,
    updated=None,
    owner_display=None,
) -> dict:
    """Map generic BYO fields (+ meta) to the target service table's own columns.

    ``seed`` is the incoming record's own dataset identifier. It is an INPUT: several sources
    derive a served id from it here, and it is never itself stored. ``parent_id`` and
    ``thread_id`` arrive already RESOLVED to the target's served id — the caller does that, because
    resolving a jira parent needs keys that are only assigned once the whole corpus has been seen.

    ``created``/``updated`` are pre-parsed epoch seconds (or None); slack/gmail carry only
    ``created_ts``.

    ``owner_display`` is the owner's name AS THE CORPUS WROTE IT, which the caller reads from
    whichever field the service names it in (``author_name``, gmail's ``mailbox_owner``, fireflies'
    ``host_name``). Stored rather than derived from the address, because an accented or initialled
    name ("Tomás Rré", "Aisha K. Patel") does not survive the round trip through
    ``<slug>@<domain>``. slack/notion/s3 have no such column — those APIs expose no owner name."""
    if src == "slack":
        # `thread_ts` is deliberately absent here: it is the thread ROOT's `ts`, and that value is
        # only known once the root has been inserted and its ts probed, so the caller sets it.
        return {
            "thread_seq": seq,
            "subtype": subtype or ex.get("subtype"),
            "reactions": _j(ex.get("reactions")),
            "files": _j(ex.get("files")),
            "edited": _j(ex.get("edited")),
            "created_ts": created,
            # Who spoke in this conversation. Slack-only and root-only: it is the thread's
            # cast, not a per-message field, so a reply leaves it NULL.
            "participants": _j(ex.get("participants")),
        }
    if src == "gmail":
        # `thread` names the thread this message belongs to (default: the message's own id), so
        # every message of a multi-message thread shares one thread_id while carrying its own
        # position in `thread_seq`. It holds the ROOT'S SERVED id, resolved by the caller — a
        # gmail id is a pure hash of the seed, so that resolution needs no lookup.
        return {
            "thread_id": thread_id or synth.gmail_message_id(seed),
            "thread_seq": seq,
            "label_ids": _j(ex.get("label_ids")),
            "to_addr": ex.get("to"),
            "cc": ex.get("cc"),
            "bcc": ex.get("bcc"),
            "reply_to": ex.get("reply_to"),
            "message_id": ex.get("message_id"),
            "in_reply_to": ex.get("in_reply_to"),
            "refs": _j(ex.get("references")),
            "attachments": _j(ex.get("attachments")),
            "created_ts": created,
            "body_html": ex.get("html"),
            "owner_display": owner_display,
        }
    if src == "google_drive":
        return {
            "subtype": subtype,
            "mime_type": ex.get("mime_type"),
            "parents": _j(ex.get("parents")),
            "created_ts": created,
            "updated_ts": updated,
            "trashed": (1 if ex.get("trashed") else None),
            "collaborators": _j(ex.get("collaborators")),
            "owner_display": owner_display,
        }
    if src == "github":
        return {
            "kind": subtype or "issue",
            "path": ex.get("path"),
            "number": ex.get("number"),
            "state": ex.get("state"),
            "labels": _j(ex.get("labels")),
            "assignees": _j(ex.get("assignees")),
            "merged_at": ex.get("merged_at"),
            "head_ref": ex.get("head"),
            "base_ref": ex.get("base"),
            "reviews": _j(ex.get("reviews")),
            "reactions": _j(ex.get("reactions")),
            "created_ts": created,
            "updated_ts": updated,
            "closed_ts": _epoch(ex.get("closed_at")),
            "closed_by": ex.get("closed_by"),
            "merged_by": ex.get("merged_by"),
            "milestone": ex.get("milestone"),
            "requested_reviewers": _j(ex.get("requested_reviewers")),
            "changed_paths": _j(ex.get("changed_paths")),
            "owner_display": owner_display,
        }
    if src == "jira":
        return {
            "status": ex.get("status"),
            "issuetype": ex.get("issuetype"),
            "priority": ex.get("priority"),
            "labels": _j(ex.get("labels")),
            "components": _j(ex.get("components")),
            "issuelinks": _j(ex.get("issuelinks")),
            "parent_id": parent_id,
            "changelog": _j(ex.get("changelog")),
            "created_ts": created,
            "updated_ts": updated,
            "assignee_email": ex.get("assignee"),
            "reporter_email": ex.get("reporter"),
            "resolution": ex.get("resolution"),
            "resolution_ts": _epoch(ex.get("resolutiondate")),
            "duedate": ex.get("duedate"),
            "fix_versions": _j(ex.get("fix_versions")),
            # `severity` is a separate axis from `priority` (how bad vs. when to fix) and
            # `squad` is the owning team, which need not be the project's ACL group.
            "severity": ex.get("severity"),
            "squad": ex.get("squad"),
            "key": ex.get("key"),
            "owner_display": owner_display,
        }
    if src == "confluence":
        return {
            # The id the corpus asked for, or None for the deferred pass to fill -- the same
            # provided-or-NULL shape github's `number` and jira's `key` use.
            "id": ex.get("content_id"),
            "subtype": subtype or "page",
            "parent_id": parent_id,
            "labels": _j(ex.get("labels")),
            "created_ts": created,
            "updated_ts": updated,
            "version_number": ex.get("version_number"),
            "version_message": ex.get("version_message"),
            "minor_edit": (1 if ex.get("minor_edit") else None),
            # Confluence's own confidentiality label, free text and stored verbatim rather
            # than forced into an enum: real corpora write "restricted (customer-sensitive)" and
            # "restricted (finance/customer-sensitive)" alongside plain "internal". It is a
            # served label only — ACL still comes from `visibility`/`readers`, so a corpus that
            # wants a restricted page group-scoped says so there too.
            "reviewers": _j(ex.get("reviewers")),
            "confidentiality": ex.get("confidentiality"),
            "owner_team": ex.get("owner_team"),
            "owner_display": owner_display,
        }
    if src == "notion":
        return {
            "subtype": subtype or "page",
            "parent_id": parent_id,
            "properties": _j(ex.get("properties")),
            "icon": ex.get("icon"),
            "cover": ex.get("cover"),
            "created_ts": created,
            "updated_ts": updated,
        }
    if src == "s3":
        return {
            "key": ex.get("key"),
            "subtype": subtype or "STANDARD",
            "content_type": ex.get("content_type") or "text/plain",
            "size": ex.get("size"),
            "created_ts": created,
            "updated_ts": updated,
        }
    if src == "hubspot":
        # The object type is the grouping column, so it is set by the caller like every other
        # container. HubSpot's typed fields stay in one JSON column because search filters may
        # name any property (see backlot/schemas/hubspot.schema.json).
        return {
            "id": ex.get("record_id"),
            "properties": _j(ex.get("properties")),
            "archived": (1 if ex.get("archived") else None),
            "created_ts": created,
            "updated_ts": updated,
            "owner_display": owner_display,
        }
    if src == "linear":
        # Keys are Linear's own (camelCase `branchName`/`dueDate`, `state` not status), so a
        # corpus written against the Linear API needs no renaming. `identifier` and `branchName`
        # are both derivable, so an omitted one is synthesized at serve time rather than stored.
        return {
            "identifier": ex.get("identifier"),
            "state": ex.get("state"),
            "priority": synth.linear_priority(ex.get("priority")),
            "estimate": ex.get("estimate"),
            "labels": _j(ex.get("labels")),
            "project": ex.get("project"),
            "cycle": ex.get("cycle"),
            "branch_name": ex.get("branchName"),
            "due_date": ex.get("dueDate"),
            "created_ts": created,
            "updated_ts": updated,
            "archived_ts": _epoch(ex.get("archivedAt")),
            "auto_archived_ts": _epoch(ex.get("autoArchivedAt")),
            "auto_closed_ts": _epoch(ex.get("autoClosedAt")),
            "canceled_ts": _epoch(ex.get("canceledAt")),
            "completed_ts": _epoch(ex.get("completedAt")),
            "started_ts": _epoch(ex.get("startedAt")),
            "assignee_email": ex.get("assignee"),
            "assignee_display": ex.get("assigneeName"),
            "owner_display": owner_display,
            # `parent` is the generic hierarchy field; for Linear it holds the parent's
            # human identifier (ENG-123), not a doc_id, because that is how Linear itself names
            # a parent.
            "parent_key": parent_id,
            "release": ex.get("release"),
        }
    if src == "fireflies":
        # Keys are the Fireflies API's own, so a corpus written against it needs no renaming.
        # `id` and the three media/web URLs are derived from the record's own dataset id when
        # omitted. A corpus may still provide `transcript_id`, which wins — it is a claim on one
        # spelling, the same way a github `number` is, and the PRIMARY KEY turns a clash between
        # two records into a loud import failure.
        # Read through the registry rather than calling `synth.fireflies_id` here: ID_SEED is
        # where "how is this source's id derived" is answered for every other source, and a second
        # path to the same function is a place for the two to drift.
        tid = ex.get("transcript_id") or store.id_seed("fireflies")(seed)
        return {
            "id": tid,
            "calendar_id": ex.get("calendar_id"),
            "calendar_type": ex.get("calendar_type") or "google_calendar",
            "organizer_email": ex.get("organizer_email"),
            "duration": _ff_minutes(ex.get("duration")),
            "created_ts": created,
            "summary": _j(ex.get("summary")),
            "analytics": _j(ex.get("analytics")),
            "participants": _j(ex.get("participants")),
            "meeting_attendees": _j(ex.get("meeting_attendees")),
            "audio_url": ex.get("audio_url") or synth.fireflies_media_url(tid, "audio"),
            "video_url": ex.get("video_url") or synth.fireflies_media_url(tid, "video"),
            "transcript_url": (ex.get("transcript_url") or synth.fireflies_transcript_url(tid)),
            "meeting_link": ex.get("meeting_link") or synth.fireflies_meeting_link(seed),
            # `host_name` is where a Fireflies record names its owner; the caller resolves
            # which field that is per service, so this branch just takes the value.
            "owner_display": owner_display,
        }
    return {}


def _ff_minutes(value) -> float | None:
    """A Fireflies `duration` in MINUTES, which is the unit the API uses."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _emails(rec: dict):
    """Yield every email in a record (author, readers, child-row authors).

    Drives org inference, so an alias missing here makes a corpus using only that alias fall back to
    the DEFAULT org — which then mis-grants every public doc, since those go to the org principal.
    """
    for key in ("author_email", "host_email"):
        v = rec.get(key)
        if isinstance(v, str) and "@" in v:
            yield v
    for r in rec.get("readers") or []:
        if isinstance(r, str) and "@" in r:
            yield r
    # Every child-row array: `messages[]` is gmail's, and a thread's later messages carry senders
    # the root does not — for a converted mail corpus that is most of the addresses in it.
    for child in (
        (rec.get("comments") or [])
        + (rec.get("sentences") or [])
        + (rec.get("messages") or [])
        + (rec.get("replies") or [])
    ):
        cv = child.get("author_email") if isinstance(child, dict) else None
        if isinstance(cv, str) and "@" in cv:
            yield cv


def _infer_org(records, settings: Settings) -> tuple[str, str]:
    """Derive (org_name, org_domain) from the corpus's dominant author email domain."""
    return infer_org((e for rec in records for e in _emails(rec)), settings)


def corpus_records(source) -> Iterator[tuple[int, str]]:
    """``(lineno, line)`` over a plain JSONL file, a ``.jsonl.gz``, or a sharded artifact directory.

    A directory is read through its ``manifest.json`` shard by shard, because the point of sharding
    is a corpus too large to hold at once. Line numbers run across the whole artifact, so an error
    message still names one place.

    Split on ``\\n`` ALONE (``newline="\\n"``), for the reason ``jsonl_lines`` documents: U+2028 and
    the vertical tab are ordinary characters inside a JSON string, and universal newlines would tear
    a valid record in half.
    """
    source = Path(source)
    if source.is_dir():
        mf = source / "manifest.json"
        if not mf.exists():
            raise SystemExit(
                f"{mf} not found — pass a JSONL file, a .jsonl.gz, or a sharded artifact directory"
            )
        manifest = json.loads(mf.read_text())
        # The artifact is downloaded, which makes its manifest untrusted input: a shard path has to
        # stay inside the directory it came with.
        paths = []
        for src in sorted(manifest["sources"]):
            for s in manifest["sources"][src]["shards"]:
                p = (source / s["path"]).resolve()
                if not p.is_relative_to(source.resolve()):
                    raise SystemExit(f"manifest names a shard outside {source}: {s['path']}")
                paths.append(p)
    else:
        paths = [source]
    n = 0
    for p in paths:
        opener = gzip.open if p.suffix == ".gz" else open
        with opener(p, "rt", encoding="utf-8", newline="\n") as fh:
            for line in fh:
                n += 1
                yield n, line.rstrip("\n")


def verify_manifest(source) -> list[str]:
    """Problems found checking a sharded artifact against its manifest ([] == intact).

    Size and digest, per shard: a truncated or swapped download has to fail before it half-loads a
    database, which is the same reason ``--dry-run`` exists for a hand-written corpus.
    """
    source = Path(source)
    mf = source / "manifest.json"
    if not mf.exists():
        return [f"{mf} not found"]
    manifest = json.loads(mf.read_text())
    problems = []
    for src in sorted(manifest.get("sources", {})):
        for shard in manifest["sources"][src]["shards"]:
            p = source / shard["path"]
            if not p.exists():
                problems.append(f"{shard['path']}: missing")
                continue
            if p.stat().st_size != shard["bytes"]:
                problems.append(
                    f"{shard['path']}: {p.stat().st_size} bytes, manifest says {shard['bytes']}"
                )
                continue
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != shard["sha256"]:
                problems.append(f"{shard['path']}: sha256 mismatch")
    # The roster is checked too, and not as an afterthought: it is the closed principal set, so it
    # decides who holds a token and what they can see. Importing a directory picks it up on its own,
    # which is exactly why its digest cannot go unverified.
    r = manifest.get("roster")
    if r:
        rp = source / r["path"]
        if not rp.exists():
            problems.append(f"{r['path']}: missing")
        elif rp.stat().st_size != r["bytes"]:
            problems.append(f"{r['path']}: {rp.stat().st_size} bytes, manifest says {r['bytes']}")
        elif hashlib.sha256(rp.read_bytes()).hexdigest() != r["sha256"]:
            problems.append(f"{r['path']}: sha256 mismatch")
    return problems


def _verify_or_die(corpus: Path) -> None:
    """Refuse a sharded artifact that does not match its manifest.

    Shared by ``--dry-run`` and the path that writes a database, because a corpus worth validating
    before a rehearsal is worth validating before the real thing.
    """
    if not corpus.is_dir():
        return
    broken = verify_manifest(corpus)
    if broken:
        print(f"INVALID: {len(broken)} shard problem(s) in {corpus}", file=sys.stderr)
        for b in broken:
            print(f"  {b}", file=sys.stderr)
        raise SystemExit(1)


def load_roster(path) -> dict:
    """Read a roster sidecar — the CLOSED set of principals a corpus's emails refer to.

    By default the roster is derived from the corpus: every ``author_email`` becomes a user with a
    token and a display name guessed from the address. That is right for a hand-written corpus and
    wrong for a converted one, where the people are already known and only some of them are real
    accounts. A roster file states them instead::

        org: redwood                      # optional (default: inferred from the corpus)
        org_domain: redwoodinference.com  # optional
        departments:                      # authenticating users -> a bearer token each
          Engineering:
            - {name: Ava Chen, email: ava.chen@redwoodinference.com}
            - {name: Bo Ryu, email: bo.ryu@redwoodinference.com,
               groups: [proj-checkout-rework, res-emea-support]}
        contacts:                         # principals with NO token (display-only)
          - {name: Zoe Newperson, email: zoe.newperson@redwoodinference.com, group: engineering}

    ``departments`` is exactly the shape of an ``employee_directory.yaml``, so a dataset that
    already ships one works as a roster verbatim; a department name becomes its group id via
    ``slugify``.
    ``contacts`` are people a corpus names who are not accounts — they own and read documents but
    cannot authenticate, the distinction ``tokens.yaml`` draws.

    A person may belong to more than one group — a squad, a compliance register, a region-scoped
    grant — which one department slot cannot say. An entry's ``groups`` list adds those memberships
    on top of the department (or ``group``) one; it never replaces it, so a directory that only
    knows departments and a roster that also states squads produce the same department rows.

    With a roster, `principals`, `group_members` and `tokens.yaml` come from it ALONE: a record's
    `author_email` / `readers` become references into it, and an address absent from it (a Slack
    handle, an outside sender) stays a plain address instead of becoming an account with a token.
    """
    data = yaml.safe_load(Path(path).read_text()) or {}

    def _slugs(raw) -> list[str]:
        """One roster field — ``group:`` or ``groups:`` — in any shape, as group ids.

        The two fields differ only in which membership they NAME, never in how they are
        written, so one reader serves both and neither shape can be a slip. A sequence is
        each of its entries; anything else is a single group, so a scalar (a string, or a
        bare ``2024`` that YAML hands over as an int) is one group rather than a character
        sequence or a ``TypeError``. Slugified once here, so no caller does it twice."""
        items = raw if isinstance(raw, (list, tuple)) else [raw]
        return [s for s in (slugify(str(g)) for g in items if g) if s]

    def _primary(raw) -> str | None:
        # The membership an entry's own `group:` names. A list means its first entry; the rest
        # are not dropped, `_groups` reads the whole field again as extra memberships.
        return next(iter(_slugs(raw)), None)

    def _groups(entry: dict, primary: str | None) -> list[str]:
        # The primary membership first — a department entry's is its department, a contact's is
        # its own `group:` — then everything either field names. dict.fromkeys keeps first
        # occurrence, so a group repeated across the two fields never doubles a row, and a
        # `group:` on a department entry is read rather than silently dropped.
        listed = _slugs(entry.get("group")) + _slugs(entry.get("groups"))
        return list(dict.fromkeys(([primary] if primary else []) + listed))

    users: dict[str, dict] = {}

    def _merge(email: str, name: str, groups: list[str], token: bool, *, stated: bool) -> None:
        # A person may appear more than once — two departments, or a department entry plus a
        # contact carrying extra register memberships. Membership is the UNION: replacing the
        # entry drops the earlier groups, and a `readers: [group:...]` clause then wrongly denies
        # the person it names. A contact never upgrades an account, but it never demotes one.
        #
        # Names do not union, so first-seen-wins is wrong for them: `name` always has a
        # fallback — one derived from the address — and so never looks absent. An entry that
        # states "Tomás Rré" lost to an earlier entry that stated nothing, and the corpus
        # served "Tomas Rre". A stated name wins over a derived one, whichever is seen first;
        # between two stated names the first still wins, as for groups.
        cur = users.get(email)
        if cur is None:
            users[email] = {"name": name, "groups": groups, "token": token, "_stated": stated}
            return
        # No empty-string filter here: both operands came from `_groups`, which drops them
        # already. Inside `_groups` the filter is load-bearing — it catches a name whose slug
        # collapses to "" — and repeating it here only suggested it could still happen.
        cur["groups"] = list(dict.fromkeys(cur["groups"] + groups))
        cur["token"] = cur["token"] or token
        if stated and not cur["_stated"]:
            cur["name"] = name
            cur["_stated"] = True

    for dept, people in (data.get("departments") or {}).items():
        for p in people or []:
            _merge(
                p["email"],
                p.get("name") or _display_name(p["email"]),
                _groups(p, slugify(dept) or None),
                True,
                stated=bool(p.get("name")),
            )
    for p in data.get("contacts") or []:
        _merge(
            p["email"],
            p.get("name") or _display_name(p["email"]),
            _groups(p, _primary(p.get("group"))),
            False,
            stated=bool(p.get("name")),
        )
    for u in users.values():
        u.pop("_stated", None)
    return {"org": data.get("org"), "org_domain": data.get("org_domain"), "users": users}


# --- github: a pull's changeset and its review comments -------------------------------------
#
# Two halves, and they fail differently.
#
# The SHAPE — which subtype may carry the field at all — is a hard error, because an issue has no
# changeset endpoint and no review-comment endpoint, so either field on one is data the mock would
# store and never serve. Stated here rather than as a schema `if`/`then`: expressed that way, both
# rules report as `'subtype' is a required property` at the record root, which names neither the
# field that put the rule in force nor what is unservable about the pairing, and leaves the author
# to find the `if` in the schema. Written in Python the message can say all three.
#
# The REFERENT — whether a declared path names a file the repo actually has — is reported and
# loaded anyway (see `_unresolved_changed_paths`).


def _github_pairing_errors(rec: dict) -> list[str]:
    """Pull-only fields on a record that is not a pull, and a review comment with no anchor.

    Returned rather than raised, so the one rule serves both the load (which refuses) and
    ``--dry-run`` (which reports) — a corpus must never pass the check that exists to spare the
    author a failed import and then fail the import. ``where`` is prefixed by the caller.
    """
    subtype = rec.get("subtype")
    is_pull = subtype == "pull_request"
    msgs = []
    if rec.get("changed_paths") is not None:
        if not is_pull:
            msgs.append(
                f"changed_paths is for subtype='pull_request' only (this is {subtype or 'issue'!r})"
            )
        if not isinstance(rec["changed_paths"], list) or not all(
            isinstance(p, str) for p in rec["changed_paths"]
        ):
            msgs.append("changed_paths must be a list of path strings")
    for c in rec.get("comments") or []:
        # github's line-anchored REVIEW comments are discriminated by `path` (see store.SCHEMA).
        if not isinstance(c, dict) or not any(k in c for k in ("path", "line", "diff_hunk")):
            continue
        if not c.get("path"):
            msgs.append(
                "a review comment needs 'path' — it is what marks the comment as line-anchored; "
                "line/diff_hunk alone would be served by neither endpoint"
            )
        if not is_pull:
            msgs.append(
                f"a review comment is for subtype='pull_request' only (this is "
                f"{subtype or 'issue'!r})"
            )
    return msgs


def _unresolved_changed_paths(declared, resolves) -> list[tuple[str, str]]:
    """``(where, path)`` for every declared path that names no ``file`` document, in corpus order.

    ``declared`` is one ``(where, repo, paths)`` per pull; ``resolves(repo, path)`` answers whether
    that repo has such a file. One rule for both callers, so what a load reports and what
    ``--dry-run`` reports cannot drift — they differ only in what they can see (a load asks the DB,
    and so finds files from an earlier ``--append``; ``--dry-run`` has the corpus and nothing else).

    Matching is exact on ``(repo, path)`` and ignores ACL, which is what the router does per caller.
    Deliberately so at serve time: a path the corpus named but this caller may not read must behave
    exactly like a typo, or the response reveals which paths exist. Import is not per-caller, so a
    path that resolves for NO caller is the one case it can call out.

    Reported, never refused. A corpus is routinely a SLICE of a repo — the same property that leaves
    a linear `parent` pointing outside it (see `_Loader.resolve_cross_references`) — so refusing
    would make a pull that states the files it really touched unimportable whenever the slice
    stopped short of one of them. And the referent may not have arrived yet: under ``--append`` the
    file document can land in a later shard, or already sit in a DB no ``--dry-run`` can see. So the
    record loads as written and the router drops the path from the changeset, as it drops one the
    caller cannot read.
    """
    out = []
    for where, repo, paths in declared:
        out.extend((where, p) for p in dict.fromkeys(paths) if not resolves(repo, p))
    return out


def _changed_path_count(n: int) -> str:
    return f"{n} path reference{'' if n == 1 else 's'} with no matching `file` document"


def _github_path_refs(where: str, repo: str, rec: dict) -> list[tuple[str, str, list[str]]]:
    """The file paths one github record names: a pull's `changed_paths`, and the anchor of every
    review comment on it.

    Both reach the same report, because both fail the same way. A review comment whose path names
    no file document is served NOWHERE — dropped from `/pulls/{n}/comments` and 404 at
    `/pulls/comments/{id}`, deliberately, so the response cannot reveal which paths exist — and
    unlike a changeset entry it left no trace at import, so the comment simply vanished."""
    refs = []
    if rec.get("changed_paths"):
        refs.append((where, repo, list(rec["changed_paths"])))
    anchors = [
        c["path"]
        for c in rec.get("comments") or []
        if isinstance(c, dict) and c.get("path") and any(k in c for k in ("line", "diff_hunk"))
    ]
    if anchors:
        refs.append((f"{where} (review comment)", repo, anchors))
    return refs


class _Loader:
    """Inserts BYO records into an open DB, accumulating the corpus-level state the principal, ACL
    and cross-reference passes need afterwards.

    A class rather than one long loop because a whole corpus and a single document (the unit a
    converting importer feeds in) have to go through ONE implementation or they drift.
    Cross-record work — a
    HubSpot association's target, a Linear parent's identifier — is deferred to
    :meth:`resolve_cross_references`, since the target may arrive on a later record.
    """

    def _assign_github_number(self, doc_id: str, repo: str, taken: dict[str, set[int]]) -> int:
        """A served number for a github row with no corpus-provided one, unique within `repo`
        among the numbers `taken` already holds for it (github's own uniqueness rule — see
        store.ID_SEED's `scope` for github).

        Called only from :meth:`resolve_github_numbers`'s second pass, never from `add`: which
        numbers are already taken in this repo depends on the WHOLE corpus, including rows that
        may not have been loaded yet, so this cannot run while records are still streaming in (see
        that method's docstring).

        Same probe shape as `_assign_confluence_id`/`_assign_hubspot_id`: seeded from the incoming
        record's own id so the same corpus produces the same number, re-seeded a few times to
        spread out, THEN walked
        unconditionally — re-seeding alone only terminates if the hash actually varies with the
        salt, and an unbounded re-seed loop hangs the importer. The walk is BOUNDED: past
        `synth.GITHUB_NUMBER_RANGE` steps every number `synth.github_number` can produce has been
        visited, so `repo` has more non-file rows than the space holds, and returning one anyway
        would break the PRIMARY KEY (repo, number) instead of failing where the problem actually
        is. Reads the range off `synth` rather than a private
        copy of the literal, so raising `synth.github_number`'s own modulus can never silently
        leave this walk still wrapping at the old, smaller one.
        """
        bucket = taken.setdefault(repo, set())
        seed = store.id_seed("github")
        n = int(seed(doc_id))
        for salt in range(1, 9):
            if n not in bucket:
                break
            n = int(seed(f"{doc_id}\x00{salt}"))
        for _ in range(synth.GITHUB_NUMBER_RANGE):
            if n not in bucket:
                return n
            n = n % synth.GITHUB_NUMBER_RANGE + 1
        raise SystemExit(
            f"github: repo {repo!r} has exhausted its {synth.GITHUB_NUMBER_RANGE}-number range; "
            f"no served number is free for {doc_id!r}"
        )

    def _assign_jira_number(self, doc_id: str, project: str, taken: dict[str, set[int]]) -> int:
        """A served key SUFFIX for a jira issue with no corpus-provided key, unique within
        `project` among the suffixes `taken` already holds for it.

        Called only from :meth:`resolve_jira_keys`'s second pass, never from `add`: which
        suffixes are already taken in this project depends on the WHOLE corpus, including rows
        that may not have been loaded yet, so this cannot run while records are still streaming in
        (see that method's docstring).

        Same probe shape as `_assign_github_number`: seeded from the incoming record's own id so
        the same corpus produces the same suffix, re-seeded a few times to spread out, THEN walked
        unconditionally — re-seeding alone only terminates if the hash actually varies with the
        salt, and an unbounded re-seed loop hung the importer once already. The walk is BOUNDED:
        past `synth.JIRA_KEY_NUMBER_RANGE` steps every suffix `synth.jira_key_number` can produce
        has been visited, so `project` has more issues than the space holds, and returning one
        anyway would break the PRIMARY KEY instead of failing where the problem actually is.
        """
        bucket = taken.setdefault(project, set())
        # Seeded from synth directly rather than through store.ID_SEED: jira's served value is
        # the whole KEY, composed from its project's prefix, so it is not a 1-arity seed over the
        # record's own id and has no registry entry (see store.ID_SEED's own comment). Only the
        # SUFFIX is probed here; resolve_jira_keys joins the prefix on.
        seed = synth.jira_key_number
        n = int(seed(doc_id))
        for salt in range(1, 9):
            if n not in bucket:
                break
            n = int(seed(f"{doc_id}\x00{salt}"))
        for _ in range(synth.JIRA_KEY_NUMBER_RANGE):
            if n not in bucket:
                return n
            n = n % synth.JIRA_KEY_NUMBER_RANGE + 1
        raise SystemExit(
            f"jira: project {project!r} has exhausted its {synth.JIRA_KEY_NUMBER_RANGE}-number "
            f"range; no served key is free for {doc_id!r}"
        )

    def _assign_github_comment_id(self, stored_id: str) -> int:
        """The `id` this comment will be served as: unique, 10 digits like real GitHub's, and stable.

        Seeded from the stored id so the same corpus always produces the same ids, then probed with
        a salt until free — a plain hash collides by the birthday bound long before a corpus runs
        out of comments, and a shared id means one comment's `url` returns another's body. A comment
        already in the DB keeps the id it has, so a re-import does not renumber it.

        Same probe shape as `_assign_github_number`/`_assign_jira_number`: re-seed a
        few times to spread out, THEN walk — but the walk is BOUNDED. Past
        `synth.GITHUB_COMMENT_ID_RANGE` steps every id `synth.github_comment_id` can produce has
        been visited, so this corpus has more comments than the space holds, and an unbounded walk
        would spin forever instead of failing where the problem actually is.

        `stored_id` is the SEED — the corpus's own comment id, or one composed from the parent's —
        and is used and discarded. The memo keyed on it lives only for this run.
        """
        if stored_id in self._gh_comment_ids:
            return self._gh_comment_ids[stored_id]
        served = synth.github_comment_id(stored_id)
        # Re-seed a few times, then walk. Re-seeding keeps ids spread out, but it only terminates
        # if the hash actually varies with the salt — walking is what makes termination
        # unconditional, and it cannot spin as long as the range has a free value.
        for salt in range(1, 9):
            if served not in self._gh_ids_taken:
                break
            served = synth.github_comment_id(f"{stored_id}\x00{salt}")
        for _ in range(synth.GITHUB_COMMENT_ID_RANGE):
            if served not in self._gh_ids_taken:
                self._gh_comment_ids[stored_id] = served
                self._gh_ids_taken.add(served)
                return served
            served = (
                synth.GITHUB_COMMENT_ID_MIN
                + (served - synth.GITHUB_COMMENT_ID_MIN + 1) % synth.GITHUB_COMMENT_ID_RANGE
            )
        raise SystemExit(
            f"github: comment ids have exhausted their {synth.GITHUB_COMMENT_ID_RANGE}-value "
            f"range; no served id is free for {stored_id!r}"
        )

    def _assign_confluence_id(self, seed: str, taken: set) -> int:
        """The `id` this page will be served as: unique, matching real Confluence's numeric content
        ids, and stable.

        Seeded from the incoming record's own id so the same corpus always produces the same ids,
        then probed against `taken` until free — a plain hash collides by the birthday bound long
        before a corpus runs out of pages, and a shared id leaves one of the two colliding pages
        unreachable at its own id.

        Called only from :meth:`resolve_probed_ids`, never from `add`: which ids are already taken
        depends on the WHOLE corpus, including the ids a later record may state outright, so this
        cannot run while records are still streaming in.

        Re-seed a few times to spread out, THEN walk — the walk is what makes termination
        unconditional, and it is BOUNDED: past `synth.CONFLUENCE_ID_RANGE` steps every id
        `synth.confluence_id` can produce has been visited, so this corpus has more pages than the
        space holds, and an unbounded walk would spin forever instead of failing where the problem
        actually is.
        """
        seed_fn = store.id_seed("confluence")
        served = seed_fn(seed)
        for salt in range(1, 9):
            if served not in taken:
                break
            served = seed_fn(f"{seed}\x00{salt}")
        for _ in range(synth.CONFLUENCE_ID_RANGE):
            if served not in taken:
                return served
            served = (
                synth.CONFLUENCE_ID_MIN
                + (served - synth.CONFLUENCE_ID_MIN + 1) % synth.CONFLUENCE_ID_RANGE
            )
        raise SystemExit(
            f"confluence: page ids have exhausted their {synth.CONFLUENCE_ID_RANGE}-value range; "
            f"no served id is free for {seed!r}"
        )

    def _assign_hubspot_id(self, seed: str, taken: set) -> str:
        """The `id` this record will be served as: unique, a numeric string matching real HubSpot's,
        and stable. Follows confluence's probe, not gmail's/notion's bare-seed shape:
        `synth.hubspot_record_id`'s 9,000,000,000 values still collide ~16 times at the
        500k-document scale this project generates, and a collision made a record unreachable at
        its own id.

        `taken` holds INTEGERS while the column stores a numeric STRING -- the probe's arithmetic is
        integer, and comparing '1000000000' against 1000000000 would never match. The conversion
        lives here so there is one place for it.
        """
        seed_fn = store.id_seed("hubspot")
        served = int(seed_fn(seed))
        for salt in range(1, 9):
            if served not in taken:
                break
            served = int(seed_fn(f"{seed}\x00{salt}"))
        for _ in range(synth.HUBSPOT_ID_RANGE):
            if served not in taken:
                return str(served)
            served = (
                synth.HUBSPOT_ID_MIN + (served - synth.HUBSPOT_ID_MIN + 1) % synth.HUBSPOT_ID_RANGE
            )
        raise SystemExit(
            f"hubspot: record ids have exhausted their {synth.HUBSPOT_ID_RANGE}-value range; "
            f"no served id is free for {seed!r}"
        )

    def _next_provisional(self, src: str):
        """A placeholder key for a row whose real one is only knowable once the whole corpus has
        been read. Unique by a per-run counter, and drawn from a space no real key can occupy:
        NEGATIVE for a github number (real ones start at 1), `-unassigned-` for a jira key (a real
        one starts with an uppercase project prefix).

        That disjointness is what removed the two-sweep write this replaced. While the value lived
        in a nullable column beside the row's identity, a provider's claim on N could be written
        before the row sitting on N had moved off it, colliding transiently under the unique index
        and aborting an `--append`. A final key can never collide with a provisional one, so the
        intermediate state has nowhere to go wrong.
        """
        self._provisional += 1
        # Typed to the column: an integer key takes a negative number (every real one is
        # positive), a text key a `-unassigned-` string (every real one starts with an uppercase
        # prefix or a digit).
        if store.id_column_type(src, store.id_column(src)) in ("INT", "INTEGER"):
            return -self._provisional
        return f"-unassigned-{self._provisional}"

    @staticmethod
    def _is_provisional(value) -> bool:
        """Whether a stored key is still a placeholder awaiting its deferred assignment."""
        if isinstance(value, int):
            return value < 0
        return str(value).startswith("-unassigned-")

    def _require_provided_id(self, src: str, where: str) -> None:
        """Refuse a record with no id of its own when appending to a source that already holds
        rows.

        A probed key is assigned by walking to the first free value, so re-importing a row draws a
        FRESH one and the row lands a second time. Nothing is left to recognise it by — the
        dataset's identifier is exactly what this change removed — so a re-import would duplicate
        in silence, which is the failure mode the whole change exists to end. Making the corpus
        state the identity turns it back into something checkable: the claim check above then sees
        the value is already taken and says so.

        Only on an append: a fresh import has nothing to be confused with. Every PROBED source is
        covered -- github, jira, confluence and hubspot -- because each has a field to state an
        identity in (see DEFERRED_ID).
        """
        if src in self._appending:
            label = DEFERRED_ID[src]
            raise SystemExit(
                f"{where}: {src} records must carry `{label}` when appending to a corpus that "
                f"already has {src} documents — without one this row cannot be told apart from a "
                f"row already imported, and would be added a second time. Stating one is how an "
                f"--append names a NEW document; there is no update path, so an id an earlier "
                f"import already answers at is refused too, and changing a document that is "
                f"already in means re-importing from scratch. (a fresh import needs no {label}.)"
            )

    def _claim_jira_prefix(self, provided_key, container: str, where: str) -> None:
        """A jira key's prefix is its PROJECT's key, held 1:1 in both directions as real Jira does.
        Distinct from the full-key claim: PAY-1 and PAY-2 are different keys, but in different
        projects they still fight over which project *is* PAY."""
        prefix = str(provided_key).rsplit("-", 1)[0]
        holder = self.jira_prefix_holders.get(prefix)
        if holder is not None and holder != container:
            raise SystemExit(
                f"{where}: key {provided_key!r} carries project key {prefix!r}, "
                f"which project {holder!r} already holds"
            )
        held = self.jira_prefixes.get(container)
        if held is not None and held != prefix:
            raise SystemExit(
                f"{where}: key {provided_key!r} would name project {container!r} "
                f"{prefix!r}, but its keys already name it {held!r}"
            )
        self.jira_prefix_holders[prefix] = container
        self.jira_prefixes[container] = prefix

    def _existing_file_number(self, repo: str, path):
        """The number a github `file` row already holds in this repo, or None for a new file.

        A file is addressed by (repo, path) -- its number exists only so the row is addressable
        under the primary key -- so this is the lookup that recognises a re-imported file, in
        place of the stated id the other github rows are recognised by."""
        if not path:
            return None
        row = self.conn.execute(
            "SELECT number FROM github_items WHERE repo = ? AND path = ? AND kind = 'file'",
            (repo, path),
        ).fetchone()
        return row["number"] if row else None

    def _assign_linear_identifier(self, seed: str, team_key: str) -> str:
        """The `identifier` (`ENG-42`) this issue is served and looked up by, unique within its
        team. Same probe shape as `_assign_confluence_id`: seeded from the record's own id so the
        same corpus produces the same identifier, then walked until free.

        BOUNDED by the size of the space it walks (`synth.LINEAR_ISSUE_NUMBER_RANGE`): past that
        many steps the team holds more keyless issues than numbers, and returning one anyway would
        put two issues on one identifier -- exactly what the walk exists to prevent."""
        taken = self._linear_identifiers.setdefault(team_key, set())
        number = synth.linear_issue_number(synth.linear_identifier(seed, team_key))
        for _ in range(synth.LINEAR_ISSUE_NUMBER_RANGE):
            candidate = f"{team_key}-{number}"
            if candidate not in taken:
                taken.add(candidate)
                return candidate
            number = 1 + number % synth.LINEAR_ISSUE_NUMBER_RANGE
        raise SystemExit(
            f"linear: team {team_key} has exhausted its "
            f"{synth.LINEAR_ISSUE_NUMBER_RANGE}-value identifier range; no identifier is free "
            f"for {seed!r}"
        )

    def _slack_ts(self, channel: str, seed: str, created_ts, author_email=None, body=None):
        """The `ts` a Slack message is served and addressed by, assigned once at import.

        It was computed per request from `(created_ts, thread-root key)`, which COLLIDED: every
        message of a thread hashed the same root key into the same micro-fraction, so two replies
        landing in the same second produced one ts between them and one of the two was reachable
        only at the other's. Assigning it here, probed within the channel, is what makes it an
        identifier rather than a formatting of one.

        No deferred pass, unlike github's number: nothing in a corpus ever provides a ts, so there
        is no provided-beats-synthesized race to settle -- only collisions, which an in-run probe
        settles the moment they happen. The integer part stays the row's `created_ts`, which is
        what `store.slack_messages_at_created_ts` resolves a ts by; only the fraction moves.
        """
        base = int(created_ts) if created_ts is not None else synth.epoch(seed)
        taken = self._slack_ts_taken.setdefault(channel, set())
        preloaded = self._slack_ts_preloaded.get(channel, frozenset())
        # Keyed on the message's OWN seed, where the serve-time version keyed a reply on its
        # thread root — that shared key is precisely what made two replies in one second
        # indistinguishable. A reply still sorts after its root because its `created_ts` is the
        # root's plus its position, which is where thread order actually comes from.
        candidate = synth.slack_fmt_ts(base, seed)
        for salt in range(1, synth.SLACK_TS_FRACTIONS + 1):
            if candidate not in taken:
                taken.add(candidate)
                return candidate
            # Taken by a row that PREDATES this run, in the same channel and second, by the same
            # author, saying the same thing: that is this message coming back, not a collision.
            # Reusing its ts is what makes an --append of an already-imported corpus leave one
            # row -- the probe otherwise walked to a free fraction and imported a duplicate no
            # corpus could opt out of, since nothing in a slack record states a ts. Only against
            # PRELOADED values: two such records in ONE run are two documents, and stay two.
            if candidate in preloaded and self._is_same_slack_message(
                channel, candidate, author_email, body
            ):
                return candidate
            candidate = synth.slack_fmt_ts(base, f"{seed}\x00{salt}")
        raise SystemExit(
            f"slack: channel {channel!r} has more messages in second {base} than the "
            f"{synth.SLACK_TS_FRACTIONS} fractions a ts can hold; no ts is free for {seed!r}"
        )

    def _is_same_slack_message(self, channel: str, ts: str, author_email, body) -> bool:
        """Whether the message already stored at this ts is the one now being imported.

        Author and text, because a slack record states no id: the channel and the second are
        already fixed by the ts itself, so those two are what is left to compare, and two messages
        agreeing on all four are indistinguishable to any client of this mock."""
        row = self.conn.execute(
            "SELECT author_email, content FROM slack_messages WHERE channel = ? AND ts = ?",
            (channel, ts),
        ).fetchone()
        return row is not None and row["author_email"] == author_email and row["content"] == body

    def __init__(
        self, conn, org: str, org_domain: str, *, closed: bool = False, validate: bool = True
    ):
        self.conn = conn
        self.org = org
        self.org_domain = org_domain
        # With a roster the principal set is CLOSED: a record's emails reference it rather than
        # declaring new people (see load_roster).
        self.closed = closed
        # Skipped only for records this repo generated itself — see load_records.
        self.validate = validate
        self.containers = {}  # (source_type, name) -> group_id
        self.users = {}  # email -> display name
        self.groups = set()
        self.memberships = set()  # (group_id, email)
        # Accumulated against the record's DATASET id and resolved through `self.keys` when they
        # are flushed, which happens after the deferred passes have settled every probed key (see
        # load_records). So a grant is never written under a provisional key and never needs
        # rewriting -- the deferred pass moves the document, and the grant simply reads the
        # settled value later.
        self.grants = []  # (source_type, dataset id, principal_type, principal_id)
        self.counts = {}
        self.seen = set()  # (source_type, dataset id)
        # dataset id -> the SERVED key the row was written under, for rows written THIS RUN.
        # Values are tuples, positional against store.id_columns. For github and jira the value
        # starts out PROVISIONAL and is rewritten in place by the deferred pass; for every other
        # source it is final the moment the row lands.
        #
        # In memory, and gone when the load returns: the dataset's identifiers must not outlive
        # the import, which is not the same as forbidding a map while it runs. `seen`,
        # `tracker_ids`, `_confluence_ids` and `_hubspot_ids` are all already this shape.
        self.keys = {}  # (source_type, dataset id) -> tuple(served key), LAST writer wins
        # Two records may share a dataset id. For a pure-hash source they resolve to one key and
        # the upsert leaves the later one, as before; for a PROBED one they take different keys and
        # both rows survive -- so `keys`, which a cross-reference resolves through and which can
        # only name one target, is not enough to find every row still holding a provisional key.
        # These two carry the per-ROW facts instead.
        self._pending = {}  # source_type -> [(provisional key tuple, dataset id)]
        # (source_type, key tuple) -> the dataset id that landed on it, for THIS run. The upsert
        # below is keyed on the served id, so two records whose ids COLLIDE would otherwise merge
        # silently -- one document overwriting another. Two records sharing a DATASET id resolving
        # to one row is the supported case and still upserts; two DIFFERENT ones landing on one id
        # is a collision and raises.
        self._claimed = {}
        self._final = {}  # (source_type, provisional key tuple) -> settled key tuple
        # Provisional keys are handed out from a per-run counter, in a space no real key can
        # occupy: negative for a github number (real ones start at 1), `-unassigned-` for a jira key
        # (a real one starts with an uppercase prefix). So a row can land under the NOT NULL primary
        # key before the claim order is known, and a final value can never collide with a
        # provisional one still in place.
        self._provisional = 0
        # channel -> the ts values already issued in it. A ts is unique within its channel (see
        # store.ID_COLUMNS), so the probe is scoped the same way. Preloaded by seed_tracker_ids so
        # an append cannot hand a new message a ts an existing one already answers at.
        self._slack_ts_taken = {}
        # The subset of the above that a PREVIOUS run wrote, which is the only place a re-imported
        # message can be recognised: a repeat within this run is a second document (see the
        # `repeat` comment in `insert`), but a candidate already held before the run started may
        # be this very message coming back. See `_slack_ts`.
        self._slack_ts_preloaded = {}
        # source_type -> the keys rows written by an EARLIER import already answer at, for the
        # sources a record can state its identity to (see `_states_own_id`). Preloaded by
        # seed_tracker_ids; `_written` is this run's own, so a record repeated within one corpus
        # still upserts rather than colliding with itself.
        self._preexisting = {}
        self._written = set()
        # team key -> the identifiers already issued in it, so a synthesized one is probed rather
        # than hashed into a 9,000-value space and left to collide. Preloaded by seed_tracker_ids.
        self._linear_identifiers = {}
        # The provisional keys of `kind='file'` github rows, so resolve_github_numbers can assign
        # them AFTER every issue and pull — see its phase 2.
        self._github_file_keys = set()
        # jira only, and only until the deferred passes have run: a subtask names its parent by the
        # identifier the corpus gave it, and the KEY that resolves to is not assigned until every
        # record has been seen. `_jira_projects` is the same story for the project a row belongs
        # to, which the assignment probe needs and the provisional key cannot carry.
        # source_type -> {dataset id -> the PARENT's dataset id}. A parent is named by the
        # target's own identifier, and for a deferred source the key that resolves to is not
        # assigned until every record has been seen -- so it is recorded here and written by
        # resolve_parents.
        self._parent_seeds = {}
        self._jira_projects = {}  # dataset id -> project container
        # github comment ids are ASSIGNED rather than hashed at serve time, because a comment's own
        # `url` resolves through one and a hash into any fixed range collides by the birthday bound
        # (~4% at 27k comments) — two comments sharing an id means one comment's url returns the
        # other's body. Seeded from the stored id so it stays stable, probed so it stays unique.
        # Populated by seed_tracker_ids, for the same reason the provided ids there are.
        # In-run only: the seed is the comment's own dataset id, and two mentions of it in one run
        # must resolve to one comment. Across runs there is nothing left to memo -- the served id
        # IS the stored id now, so `_gh_ids_taken` (preloaded) is the whole cross-run fact.
        self._gh_comment_ids = {}  # seed -> served id, for comments assigned THIS run
        self._gh_ids_taken = set()
        # confluence page ids are ASSIGNED rather than hashed at serve time, for the same reason:
        # a hash into synth.confluence_id's 9,000,000 values collides by the birthday bound, and a
        # shared id leaves one page unreachable at its own id. Seeded from the record's own id so
        # it stays stable, probed so it stays unique.
        self._confluence_ids = {}  # seed -> served id, for pages assigned THIS run
        self._confluence_ids_taken = set()
        # hubspot record ids are ASSIGNED rather than hashed at serve time, following confluence's
        # shape rather than gmail's/notion's: synth.hubspot_record_id's 9,000,000,000-value space
        # still collides by the birthday bound at the corpus sizes this project generates
        # (measured: ~16 collisions at 500k documents), and a shared id leaves one record
        # unreachable at its own id. Seeded from the record's own id so it stays stable, probed so
        # it stays unique.
        self._hubspot_ids = {}  # seed -> served id, for records assigned THIS run
        self._hubspot_ids_taken = set()
        # github numbers and jira key suffixes are ASSIGNED like confluence's/hubspot's, but unlike
        # either the assignment cannot happen per-record while the corpus streams: a provided
        # `number`/`key` must claim its spelling ahead of every synthesized one, corpus-wide, and a
        # record arriving early cannot know what a LATER record will provide (see
        # resolve_github_numbers / resolve_jira_keys, which run once every record has been loaded).
        # Keeping a re-imported row on the number it already served is handled at the front instead:
        # an --append must STATE the number/key (see `_require_provided_id`).
        self.fts_ids = {}
        # True once seed_tracker_ids has seen rows already in the DB — i.e. this is an --append
        # onto a corpus that already has documents, which is where a probed source's identity has
        # to be provided rather than synthesized.
        self._appending = set()  # source_types that already have rows
        # HubSpot associations are resolved after the whole corpus is read: a link may name a target
        # that appears on a later line, and an omitted `to_type` is filled in from the target's own
        # object type. dataset id -> object_type, plus the declared links.
        self.hs_types = {}
        self.hs_links = []  # (from dataset id, from_type, declaration)
        # Linear relations name a target by its dataset id and are resolved after the whole corpus
        # is read, since a target may appear on a later line.
        self.lin_links = []
        # Tracker ids the corpus stated, so a second record claiming one is refused. Two records
        # sharing a github number or jira key leaves one of them unreachable at the only id it
        # advertises. The loader is the one place that sees every row, so it is the only place the
        # claim can be checked at all.
        self.tracker_ids = {}  # (source_type, container, id) -> dataset id that claimed it
        # A jira key's prefix is its PROJECT's key, and real Jira holds that 1:1 in both
        # directions: a project has one key, a key names one project. The index can only
        # pick one side of a tie with setdefault — two projects providing `PAY-` keys left
        # `project = PAY` JQL and the role endpoint silently serving only the first, and a
        # project providing `PAY-1` beside `BILL-2` served issue keys whose prefix was not
        # their project's key. Both are corpus shapes only the loader can see and refuse.
        self.jira_prefixes = {}  # container -> prefix
        self.jira_prefix_holders = {}  # prefix -> container
        # A pull's declared changeset, resolved after the whole corpus is read: the `file` document
        # a path names may be on a later line, or in a shard already loaded.
        self.gh_changesets = []  # (where, repo, paths)

    def seed_tracker_ids(self) -> None:
        """Re-read the ids already in the DB, so a claim holds ACROSS runs too.

        A fresh ``_Loader`` per :func:`load_records` sees only the shard it is loading. Without
        this, two shards appended in separate runs could each state ``PAY-7`` and neither would be
        told, leaving one row advertising an id that fetches somebody else. ``--append`` is the
        route straight into that.

        Every stored id is a claim, and reading them is the whole cross-run story: the id a row
        serves IS its primary key. What a claim cannot do is recognise a row — it says "this value
        is taken", never "taken by the document you are about to re-import". That is why an append
        has to state a probed source's identity outright — see
        `_require_provided_id`.

        The jira prefix maps are seeded from the same rows: a later shard bringing `BILL-` keys
        into a project that already answers at `PAY`, or claiming `PAY` for a second project, is
        the same 1:1 violation whether the earlier keys arrived this run or a previous one.
        """
        # Which sources already hold documents. An append into a NON-EMPTY probed source is where
        # a synthesized identity becomes indistinguishable from a re-imported one.
        for src, tbl in store.SOURCE_TABLE.items():
            if self.conn.execute(f"SELECT 1 FROM {tbl} LIMIT 1").fetchone():
                self._appending.add(src)
        # A comment's id is assigned rather than provided, but the claim is the same: an id already
        # issued must not be issued again by a later shard.
        for (cid,) in self.conn.execute("SELECT id FROM github_comments"):
            self._gh_ids_taken.add(cid)
        # Same claim, for confluence pages and hubspot records: an id already issued (this run or a
        # previous one) must not be handed to a second row.
        for (cid,) in self.conn.execute("SELECT id FROM confluence_pages"):
            self._confluence_ids_taken.add(cid)
        for (rid,) in self.conn.execute("SELECT id FROM hubspot_objects"):
            self._hubspot_ids_taken.add(int(rid))
        # Same claim, per channel, for slack message timestamps -- and remembered separately as
        # PRE-EXISTING, which is what lets `_slack_ts` tell a re-imported message from a new one.
        for channel, ts in self.conn.execute("SELECT channel, ts FROM slack_messages"):
            self._slack_ts_taken.setdefault(channel, set()).add(ts)
            self._slack_ts_preloaded.setdefault(channel, set()).add(ts)
        # The keys an earlier import already answers at, for the sources a record states its own
        # identity to -- the claim the four DEFERRED_ID sources get from `tracker_ids` above, for
        # the two that assign no id of their own.
        for src in ("s3", "fireflies"):
            cols = ", ".join(store.id_columns(src))
            self._preexisting[src] = {
                tuple(r) for r in self.conn.execute(f"SELECT {cols} FROM {store.table(src)}")
            }
        # Same claim, per team, for synthesized linear identifiers.
        for team, identifier in self.conn.execute(
            "SELECT team, identifier FROM linear_issues WHERE identifier IS NOT NULL"
        ):
            self._linear_identifiers.setdefault(synth.linear_team_key(str(team)), set()).add(
                identifier
            )
        # Every row carries a key, stated or derived, and for a claim the difference is immaterial:
        # both mean the value is taken. The `dataset id` side of `tracker_ids` is unknowable for a
        # row from an earlier run, so it is recorded as None; the check that reads it only needs to
        # know the claim belongs to a DIFFERENT document than the one now claiming it.
        # All four probed sources, not just github and jira: without confluence's and hubspot's
        # here, a record stating an id an existing row already held went straight through the
        # upsert's `DO UPDATE` and REPLACED that row -- silently losing a document at the id it
        # was reachable by.
        for src in DEFERRED_ID:
            col = store.id_column(src)
            # `scope` is the container a claim is unique within -- a github number is per repo, and
            # nothing else is (see store.ID_COLUMNS), so the rest claim corpus-wide.
            scoped = len(store.id_columns(src)) > 1
            for row in self.conn.execute(
                f"SELECT {col} AS v, {store.grouping_col(src)} AS c FROM {store.table(src)}"
            ):
                if self._is_provisional(row["v"]):
                    continue  # unreachable mid-import, but never claim a placeholder
                scope = str(row["c"]) if scoped else ""
                self.tracker_ids[(src, scope, str(row["v"]))] = None
                if src == "jira":
                    prefix = str(row["v"]).rsplit("-", 1)[0]
                    self.jira_prefixes[str(row["c"])] = prefix
                    self.jira_prefix_holders[prefix] = str(row["c"])

    def add(self, rec: dict, where: str = "record") -> None:
        """Insert one BYO record's row(s). ``where`` names the record in an error message.

        The accumulators are aliased into locals below and only ever mutated in place, so the
        body stays the straight-line per-record mapping it reads as.
        """
        conn, org, org_domain = self.conn, self.org, self.org_domain
        closed, validate, containers = self.closed, self.validate, self.containers
        users, groups, memberships = self.users, self.groups, self.memberships
        grants, counts, seen = self.grants, self.counts, self.seen
        fts_ids, hs_types, hs_links = self.fts_ids, self.hs_types, self.hs_links
        lin_links = self.lin_links
        # Schema pre-validation: source_type/content/title, enums, comment/reply shapes,
        # and unknown-key rejection all come from backlot/schemas/ (see backlot.validation).
        errors = record_errors(rec) if validate else []
        if errors:
            raise SystemExit(f"{where}: " + "; ".join(errors))
        src = rec["source_type"]
        # Not under `validate`: these are pairings no schema states well (see
        # _github_pairing_errors), and a record an importer in this repo generated is no likelier to
        # get a pull-only field onto an issue correctly than a hand-written one.
        if src == "github" and (bad := _github_pairing_errors(rec)):
            raise SystemExit(f"{where}: " + "; ".join(bad))
        # Slack messages have no title; the other five carry a natural one.
        title = rec.get("title") or ""

        # Fireflies: `content` is DEFINED as the sentence concatenation, so the two can never be
        # allowed to disagree. A record that supplies `sentences` has its content derived from
        # them; one that supplies only `content` has its sentences parsed back out of it, so a BYO
        # author can write a plain "Speaker: text" transcript and still get per-sentence rows.
        # Either way the round-trip holds. This runs before `_doc_id`, which hashes the content —
        # so the id covers the transcript either way.
        sentences = None
        if src == "fireflies":
            given = rec.get("sentences")
            if not given and not (rec.get("content") or "").strip():
                # Stated here rather than as a schema `anyOf`, whose error ("is not valid under
                # any of the given schemas") names neither field and so tells the author nothing.
                raise SystemExit(
                    f"{where}: a fireflies record needs 'sentences' or "
                    f"'content' — one of the two IS the transcript"
                )
            if given:
                sentences = [
                    {
                        "speaker_name": s.get("speaker_name") or s.get("speaker"),
                        "text": s.get("text") or s.get("content") or "",
                        "start_time": s.get("start_time"),
                        "end_time": s.get("end_time"),
                        "speaker_id": s.get("speaker_id"),
                        "author_email": s.get("author_email"),
                    }
                    for s in given
                ]
            else:
                # `synth.parse_transcript_text` and not an importer's own parser: it is the declared
                # inverse of the `fireflies_transcript_text` two lines below, so the pair that
                # defines `content` is one module's fixed point rather than two modules agreeing by
                # convention. A record format documents the forms it accepts; salvaging some other
                # dataset's transcript layout is that importer's job, not this loader's.
                sentences = synth.parse_transcript_text(rec.get("content") or "")
            ff_minutes = _ff_minutes(rec.get("duration"))
            synth.fireflies_fill_times(sentences, (ff_minutes * 60) if ff_minutes else None)
            ordinals: dict[str, int] = {}
            for s in sentences:
                ordinals.setdefault(s["speaker_name"] or "", len(ordinals))
                if s.get("speaker_id") is None:
                    s["speaker_id"] = ordinals[s["speaker_name"] or ""]
            rec = {**rec, "content": synth.fireflies_transcript_text(sentences)}

        doc_id = _doc_id(rec)
        # Recorded, not deduplicated: `seen` answers "is this document in the corpus" for the
        # cross-reference resolution further down. Two records sharing a (source, doc_id) are both
        # written, and the row-level upsert (`ON CONFLICT(<served key>) DO UPDATE`, below) leaves the
        # later one — which is what a direct import of the same documents produces. One real corpus
        # has four such pairs (three across sources, one within jira); skipping the repeat instead
        # would keep the earlier document and diverge.
        seen.add((src, doc_id))
        gcol = store.grouping_col(src)
        container = str(rec.get(gcol) or src)  # channel / mailbox / folder / repo / project / space
        # An explicit `"group": null` means the container owns NO ACL group — which is a real
        # state, not a missing value: a Gmail mailbox has no group scope (a thread is private to
        # its participants), so inferring one from the mailbox name would invent a grantable
        # principal. Only an ABSENT `group` falls back to the container slug.
        group = rec["group"] if "group" in rec else (slugify(container) or src)
        group = str(group) if group is not None else None
        containers[(src, container)] = group
        if group:
            groups.add(group)

        def register(email: str | None, name: str | None = None) -> None:
            # With a closed roster the principal set is the sidecar's, so a record's emails are
            # references to it rather than declarations of new people (see `load_roster`).
            if email and not closed:
                users.setdefault(email, name or _display_name(email))
                if group:
                    memberships.add((group, email))

        # `host_email` is Fireflies' own name for the doc's author, accepted so a corpus written
        # against the API needs no renaming (the generic `author_email` still works).
        author = rec.get("author_email") or (rec.get("host_email") if src == "fireflies" else None)
        register(author, rec.get("author_name") or rec.get("host_name"))
        for g in rec.get("author_groups", []):
            if not closed:
                groups.add(g)
                if author:
                    memberships.add((g, author))

        # grant tuples (principal_type, principal_id), shared by the whole thread
        readers = rec.get("readers")
        vis = rec.get("visibility")
        # `is not None`, not truthiness: an explicit `"readers": []` means NOBODY may read this
        # document — admin-only — which is a state a corpus otherwise could not express at all, and
        # the honest reading of an empty list of readers. Falling through to the public default
        # instead would make the most restrictive spelling produce the least restrictive result.
        if readers is not None:
            grant_types = []
            for pid in readers:
                ptype, pval = _principal(pid)
                grant_types.append((ptype, pval))
                if closed:
                    continue
                if ptype == "user":
                    users.setdefault(pval, _display_name(pval))
                elif ptype == "group":
                    groups.add(pval)
        elif vis == "private" and author:
            grant_types = [("user", author)]
        elif vis == "group" and group:
            grant_types = [("group", group)]
        else:
            grant_types = [("org", org)]

        # structured extras: rec.meta merged with convenience top-level keys
        extras = dict(rec.get("meta") or {})
        # A tracker id is read from the field the schema declares it in, and from nowhere
        # else. `meta` is documented free-form, so seeding `extras` from it let
        # `meta: {"number": 3}` claim issue 3 in a repository just as a top-level `number`
        # would — a spelling no schema describes, that shadows a real issue, and that the
        # uniqueness check below would then refuse an import over. Both ids are ordinary
        # `meta` content again: carried through, never promoted to the served column. Read from
        # DEFERRED_ID so a source that gains a stateable id is covered by declaring it there.
        for reserved in (*DEFERRED_ID.values(), "transcript_id"):
            extras.pop(reserved, None)
        for k in (
            "labels",
            "reactions",
            "files",
            "edited",
            "to",
            "cc",
            "bcc",
            "reply_to",
            "message_id",
            "in_reply_to",
            "references",
            "attachments",
            "mime_type",
            "parents",
            "trashed",
            "state",
            "assignees",
            "merged_at",
            "head",
            "base",
            "reviews",
            "status",
            "issuetype",
            "priority",
            "components",
            "issuelinks",
            "label_ids",
            "thread",
            "html",
            "closed_at",
            "closed_by",
            "merged_by",
            "milestone",
            "requested_reviewers",
            "changed_paths",
            "number",
            "content_id",
            "record_id",
            # A transcript's own id and a jira issue's history: both declared by their schema, both
            # read off `extras` — so without them here the schema's spelling was dropped on the
            # floor and only the `meta` one worked. For `transcript_id` that also meant the served
            # id came from a spelling no schema describes, which is what the loop above exists to
            # prevent.
            "transcript_id",
            "changelog",
            "resolution",
            "resolutiondate",
            "duedate",
            "fix_versions",
            "versions",
            "assignee",
            "reporter",
            "minor_edit",
            "version_message",
            "version_number",
            "properties",
            "icon",
            "cover",
            "key",
            "content_type",
            "size",
            "path",
            "archived",
            # confluence confidentiality/ownership, drive collaborators, jira severity/squad,
            # slack participants — the per-service people-and-scope fields
            "reviewers",
            "confidentiality",
            "owner_team",
            "collaborators",
            "severity",
            "squad",
            "participants",
            # Linear (its own field names: `state` not status, camelCase timestamps)
            "identifier",
            "estimate",
            "project",
            "cycle",
            "branchName",
            "dueDate",
            "assigneeName",
            "archivedAt",
            "autoArchivedAt",
            "autoClosedAt",
            "canceledAt",
            "completedAt",
            "startedAt",
            "release",
            "relations",
            "attachments",
            # Fireflies (its own field names, as the GraphQL API returns them)
            "host_email",
            "organizer_email",
            "duration",
            "summary",
            "analytics",
            "participants",
            "meeting_attendees",
            "audio_url",
            "video_url",
            "transcript_url",
            "meeting_link",
            "calendar_id",
            "calendar_type",
        ):
            if k in rec:
                extras[k] = rec[k]
        subtype = rec.get("subtype")
        parent_id = rec.get("parent")
        # created_ts must never be NULL (the server sorts/filters by it; a NULL would need a
        # runtime null-check). Fall back to the same deterministic synth.epoch the server would have
        # synthesized for a missing ts, so the served time is unchanged — just materialized now.
        created_written = _epoch_field(rec.get("created"), where, "created")
        created = synth.epoch(doc_id) if created_written is None else created_written
        updated = _epoch_field(rec.get("updated"), where, "updated")

        replies = rec.get("replies") if src == "slack" else None
        # The ROOT's `ts`, which is what a reply stores as its `thread_ts` — and it is not known
        # until the root has been inserted and probed, so `insert` fills it from here rather than
        # taking it as an argument. A root with replies carries its OWN ts (Slack does too, so
        # `thread_ts == ts` is what marks a root); a standalone message carries NULL.
        slack_thread_ts = None
        # Resolved before the root row is written, because a root whose own clock was
        # synthesized may be re-grounded on the replies that do carry one.
        created, reply_seconds = _thread_seconds(
            where, created, created_written is not None, replies
        )
        # gmail's own child-row array. `replies` stays Slack-only (a Slack reply is a *reply*,
        # with reactions and files); a Gmail thread is a sequence of full RFC822 messages, each
        # with its own sender, recipients and Message-ID, so it gets an array that reads like one
        # — the same per-source choice `sentences` makes for a Fireflies transcript.
        messages = rec.get("messages") if src == "gmail" else None
        # The thread every message of this record belongs to: the record's own `thread` when it
        # names one, else its doc_id — the SAME expression `_service_columns` applies to the root,
        # so a record that opens a thread under an explicit id keeps its messages in it rather than
        # splitting them into a second thread named after the root's doc_id.
        # A gmail id is a pure hash of the seed, so the thread's SERVED id is computable here
        # without waiting for the root row to land.
        gmail_thread = (
            synth.gmail_message_id(rec.get("thread") or doc_id) if src == "gmail" else None
        )
        # The owner's display name as the corpus wrote it, under each service's own name for it:
        # gmail's owner is the MAILBOX's owner (often not the sender of any one message in the
        # thread) and fireflies' is the meeting HOST, where every other source's is the author's.
        owner_display = {
            "gmail": rec.get("mailbox_owner"),
            "fireflies": rec.get("host_name") or rec.get("author_name"),
        }.get(src, rec.get("author_name"))

        if src == "fireflies":
            # Needs the doc_id (analytics is seeded from it), so it can only run here — the
            # sentences themselves were built before `_doc_id`, above.
            secs = (_ff_minutes(rec.get("duration")) or 0) * 60 or None
            extras["analytics"] = extras.get("analytics") or synth.fireflies_analytics(
                doc_id, synth.fireflies_speaker_stats(sentences), secs
            )
            extras["participants"] = extras.get("participants") or [
                e for e in dict.fromkeys(s.get("author_email") for s in sentences) if e
            ]

        def insert(
            did,
            email,
            ttl,
            body,
            seq=0,
            sub=None,
            par=None,
            ex=None,
            cts=None,
            uts=None,
            odisp=None,
        ):
            # A parent is named by the target's dataset id; what gets STORED is the target's
            # served id. notion resolves right here — its id is a pure hash of the seed,
            # so it is the same answer whether the parent has been loaded yet or not. A DEFERRED
            # source cannot: its key is only assigned once the whole corpus has been seen, so the
            # reference is recorded and written by `resolve_parents`. linear's `parent_key` is an
            # IDENTIFIER, not an id, and is left exactly as the corpus wrote it.
            if par is not None and src in ("confluence", "jira"):
                self._parent_seeds.setdefault(src, {})[did] = par
                par = None
            elif par is not None and src == "notion":
                # Resolved inline, unlike confluence's and jira's: a notion id is a pure hash of
                # the dataset id, so it is the same answer whether the parent is in this corpus or
                # was loaded by an earlier run. The reference is still RECORDED, because a hash
                # answers for a parent that does not exist just as readily as for one that does —
                # and notion was the one hierarchy source that stored such a parent without a word,
                # serving a `parent` UUID that 404s. `resolve_parents` checks it.
                self._parent_seeds.setdefault(src, {})[did] = par
                par = synth.notion_id(par)
            cols = _service_columns(
                src, ex or {}, sub, par, did, gmail_thread, seq, org_domain, cts, uts, odisp
            )
            cols.update(author_email=email or f"unknown@{org_domain}", title=ttl, content=body)
            if src == "s3" and cols.get("size") is None:
                cols["size"] = len((body or "").encode("utf-8"))
            cols[gcol] = container
            # ---- the row's own identity -------------------------------------------------
            # Every source's key is assigned HERE, from the incoming record's dataset id as a
            # seed, and the seed is then discarded. What differs per source is only how the
            # candidate is turned into a value that is unique:
            #
            #   pure       gmail, google_drive, notion, linear, fireflies -- the seed IS the id.
            #              Their spaces are wide enough (a full digest, or 2**63) that a collision
            #              is vanishingly unlikely, and the PRIMARY KEY turns one into a loud
            #              import failure rather than a silent shadow.
            #   probed     confluence, hubspot -- their spaces are narrow enough to collide by the
            #              birthday bound at the sizes this project generates, so an in-run memo
            #              plus the preloaded taken-set walks to the first free value.
            #   deferred   github, jira -- a PROVIDED value must claim its spelling ahead of every
            #              synthesized one, corpus-wide, and a record arriving early cannot know
            #              what a later one will provide. They land under a provisional key and
            #              resolve_github_numbers / resolve_jira_keys settle them at the end.
            #   stated     s3 -- the corpus gives (bucket, key) outright; nothing is synthesized.
            #   own pass   slack -- see `_slack_ts`. Not deferred: no corpus ever provides a ts, so
            #              there is no claim to lose a race to, only collisions to probe.
            # The key this dataset id already resolved to in this run, for the sources whose
            # identity is SYNTHESIZED: two records sharing a `doc_id` are one document, and were
            # one row until a probed source started handing the second a fresh id. The
            # sources whose id is a pure hash of the dataset id never needed this -- they land on
            # the same value twice by construction -- and the ones the corpus STATES must not have
            # it, since there the record, not the dataset id, says which document this is.
            repeat = self.keys.get((src, did))
            if src == "linear" and not cols.get("identifier"):
                # MATERIALIZE the identifier the server would otherwise synthesize per request.
                # An id that is served has to be resolvable, and every lookup reads a stored
                # column — so a serve-time-only identifier came back "Entity not found" from
                # `issue(id: "ENG-749")` even though the API had just handed the caller that exact
                # string.
                #
                # Probed, not hashed: `synth.linear_identifier` numbers within 9,000 values per
                # team, which collides by the birthday bound at ~110 keyless issues in one team —
                # and two issues sharing `ENG-2686` leaves one of them unreachable at the only
                # human-facing id it advertises, since `issue(id:)` answers the first. The same
                # probe shape as confluence's and hubspot's, and for the same reason.
                cols["identifier"] = self._assign_linear_identifier(
                    did, synth.linear_team_key(container)
                )
            if src in ("gmail", "google_drive", "notion", "linear"):
                cols["id"] = store.id_seed(src)(did)
            elif src == "slack":
                cols["ts"] = (
                    repeat[-1]
                    if repeat is not None
                    else self._slack_ts(container, did, cols.get("created_ts"), email, body)
                )
                # A reply takes the root's ts (set by the caller once the root landed); a root
                # with replies takes its own; a standalone message has no thread at all.
                cols["thread_ts"] = slack_thread_ts if seq else (cols["ts"] if replies else None)
            elif src == "fireflies":
                pass  # `_service_columns` already set `id`, honouring a provided transcript_id
            elif src == "s3":
                pass  # `_service_columns` already set `key`; `bucket` is the container below
            if src == "notion":
                # A SECOND, unrelated synthesized id for the same row -- the 2025-09-03 API's data
                # source (query target) for a database. Only for a database: real Notion has no
                # data source for a page, and NULL is no claim under the unique index rather than
                # a collision.
                #
                # Written unconditionally (the ternary, not a bare `if`): `names = list(cols)`
                # below feeds the upsert's `DO UPDATE SET col=excluded.col` list, which only clears
                # a column that is IN it. If an earlier import made this row a database and a later
                # one demotes it to a page, an `if`-only assignment would leave `cols` without the
                # key, DO UPDATE would never mention it, and the stale data-source id would keep
                # resolving -- serving a page as a data source, since get_data_source relies on a
                # match here implying subtype='database' (see store.notion_by_data_source_id).
                cols["data_source_id"] = (
                    synth.notion_data_source_id(did) if cols.get("subtype") == "database" else None
                )
            if src == "github" and cols.get("kind") == "file":
                # The schema says a file row's number is ignored, and that stays true: a file is
                # addressed by (repo, path), never by a number. It still needs one to be
                # addressable at all now that (repo, number) is the primary key, so it takes a
                # PROVISIONAL like any keyless row and the deferred pass probes it a real one --
                # after every provided issue/PR number has claimed its spelling, so a file can
                # never take a number a real issue asked for.
                #
                # A file already in the DB keeps the number it holds. That is what makes a file
                # row appendable at all: its identity is (repo, path), which the record states in
                # full, so an --append needs no `number` from the corpus -- and a corpus sharded
                # so a pull's files arrive after the pull (which this importer's own changeset
                # report tells authors to do) was otherwise unloadable past the first shard.
                cols["number"] = self._existing_file_number(container, cols.get("path"))
                file_row = True
            else:
                file_row = False
            if src == "jira":
                self._jira_projects[did] = container
            provisional = False
            # The key this dataset id already resolved to in this run, for the sources whose
            # identity is SYNTHESIZED: two records sharing a `doc_id` are one document, and were
            # one row until a probed source started handing the second a fresh id. The
            # sources whose id is a pure hash of the dataset id never needed this -- they land on
            # the same value twice by construction -- and the ones the corpus STATES must not have
            # it, since there the record, not the dataset id, says which document this is.
            if file_row:
                # No claim and no `_require_provided_id`: a file states its identity in full as
                # (repo, path), and its number was read off the row that path already occupies
                # (or is about to be probed). Running the stated-id claim over it would refuse a
                # re-imported file for holding its own number.
                if cols["number"] is None:
                    cols["number"] = self._next_provisional("github")
                    provisional = True
            elif src in DEFERRED_ID:
                col = store.id_column(src)
                provided = cols.get(col)
                # A provided id is a claim on one spelling, and two records cannot hold the same
                # one: whichever the key gave it to, the other would be unreachable at the only id
                # it advertises. A github number is per repository, a jira key per instance.
                if provided is not None:
                    scope = container if src == "github" else ""
                    claim = (src, scope, str(provided))
                    # Only a DIFFERENT document violates the claim. Two records may share a dataset
                    # id — both are written and the row-level upsert leaves the later one, which is
                    # what a direct import of the same documents produces. A claim carried over
                    # from an earlier run has no dataset id recorded (None), and is a violation for
                    # exactly the same reason: it belongs to a document this one is not.
                    claimed = self.tracker_ids.get(claim, _UNCLAIMED)
                    if claimed is not _UNCLAIMED and claimed != did:
                        label = DEFERRED_ID[src]
                        raise SystemExit(
                            f"{where}: {label} {provided!r} is already claimed by "
                            + (f"{claimed!r}" if claimed is not None else "a previous import")
                            + (f" in repo {scope!r}" if scope else "")
                        )
                    self.tracker_ids[claim] = did
                    if src == "jira":
                        self._claim_jira_prefix(provided, container, where)
                elif repeat is not None:
                    cols[col] = repeat[-1]  # the same document, named twice
                else:
                    self._require_provided_id(src, where)
                    cols[col] = self._next_provisional(src)
                    provisional = True
            # ---- end identity -------------------------------------------------------------
            key = tuple(cols[c] for c in store.id_columns(src))
            claimed_by = self._claimed.get((src, key))
            if claimed_by is not None and claimed_by != did:
                raise SystemExit(
                    f"{where}: this row resolves to {store.id_columns(src)} = {key}, which "
                    f"{claimed_by!r} in this corpus already resolves to. Two documents cannot "
                    "share the one id the API serves them at -- one would be unreachable at it. "
                    "Give one of them a different id, or (if they are the same document) the "
                    "same one."
                )
            if (
                _states_own_id(src, rec)
                and key in self._preexisting.get(src, ())
                and (src, key) not in self._written
            ):
                raise SystemExit(
                    f"{where}: this row states {store.id_columns(src)} = {key}, which a document "
                    "from an EARLIER import already answers at. Whether this record is that "
                    "document coming back or a different one claiming its id cannot be told "
                    "apart from here, and writing it would replace that document and hand its "
                    "readers this one. Re-import the corpus from scratch, or give this record an "
                    "id no imported document holds."
                )
            self._claimed[(src, key)] = did
            self._written.add((src, key))
            names = list(cols)
            # An upsert keyed explicitly on the table's PRIMARY KEY — the id it serves — not a
            # blanket `INSERT OR REPLACE`: two records that resolve to the same key still leave the
            # later one (DO UPDATE), which is what a direct import of the same documents produces
            # (see the `seen.add` comment above) — but a conflict on any OTHER unique index (notion's
            # data_source_id) raises IntegrityError instead of SQLite's REPLACE algorithm silently
            # deleting the row already holding that value. `names` always includes every column
            # `_service_columns` + the additions above set for this src, so DO UPDATE overwrites
            # every one of them, same as OR REPLACE did.
            key_cols = store.id_columns(src)
            update_cols = [n for n in names if n not in key_cols]
            conn.execute(
                f"INSERT INTO {store.table(src)} ({', '.join(names)}) "
                f"VALUES ({', '.join('?' for _ in names)}) "
                f"ON CONFLICT({', '.join(key_cols)}) DO UPDATE SET "
                + ", ".join(f"{n}=excluded.{n}" for n in update_cols),
                [cols[n] for n in names],
            )
            # `keys` answers "what did the record called `did` resolve to", which is what a
            # cross-reference needs and which can only have one answer. `_pending` is per ROW, so
            # the deferred pass reaches every row even when two records shared a dataset id.
            self.keys[(src, did)] = key
            if provisional:
                self._pending.setdefault(src, []).append((key, did))
                if src == "github" and file_row:
                    self._github_file_keys.add(key)
            # Grants and FTS entries name the ROW by the key it landed under -- provisional or
            # not -- and `_settled` translates it when they are flushed, after the deferred passes.
            # Recording the dataset id instead would give two rows that shared one the same grants.
            fts_ids.setdefault(src, []).append(key)
            counts[src] = counts.get(src, 0) + 1
            for pt, pid in grant_types:
                grants.append((src, key, pt, pid))

        insert(
            doc_id,
            author,
            title,
            rec["content"],
            0,
            subtype,
            parent_id,
            extras,
            created,
            updated,
            owner_display,
        )
        if src == "slack":
            # The root has landed, so its ts is settled — every reply below stores it as its
            # `thread_ts`. `store.id_columns("slack")` is (channel, ts), so the ts is the second.
            slack_thread_ts = self.keys[(src, doc_id)][1]

        if src == "linear":
            issue_id = self.keys[(src, doc_id)][0]
            for j, att in enumerate(rec.get("attachments") or [], start=1):
                url = att.get("url") if isinstance(att, dict) else str(att)
                if not url:
                    continue
                title = (
                    (att.get("title") if isinstance(att, dict) else None)
                    or url.rstrip("/").rsplit("/", 1)[-1]
                    or url
                )
                conn.execute(
                    "INSERT OR REPLACE INTO linear_attachments(id, issue_id, seq, title, url, "
                    "subtitle, source_type, created_ts) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        f"{issue_id}::a{j}",
                        issue_id,
                        j,
                        title,
                        url,
                        (att.get("subtitle") if isinstance(att, dict) else None),
                        (att.get("sourceType") if isinstance(att, dict) else None),
                        created,
                    ),
                )
            for a in rec.get("relations") or []:
                # Both ends are still DATASET ids here; resolve_cross_references translates them
                # once every record has been seen, since a target may arrive on a later line.
                lin_links.append((doc_id, a, created))

        if src == "hubspot":
            hs_types[doc_id] = container
            for a in rec.get("associations") or []:
                hs_links.append((doc_id, container, a))

        transcript_id = self.keys[(src, doc_id)][0] if src == "fireflies" else None
        for j, s in enumerate(sentences or [], start=1):
            register(s.get("author_email"), s.get("speaker_name"))
            conn.execute(
                "INSERT OR REPLACE INTO fireflies_sentences(id, transcript_id, seq, author_email, "
                "body, created_ts, reactions, speaker_name, speaker_id, start_time, end_time) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                # A sentence sits on the MEETING's clock plus its own offset, so ordering by time
                # never shuffles a transcript (same reasoning as a comment's created + j above).
                (
                    f"{transcript_id}::s{j}",
                    transcript_id,
                    j,
                    s.get("author_email"),
                    s["text"],
                    int(created + (s.get("start_time") or 0)),
                    None,
                    s.get("speaker_name") or None,
                    s.get("speaker_id"),
                    s.get("start_time"),
                    s.get("end_time"),
                ),
            )

        # Every file path this record names — its changeset and its review-comment anchors — kept
        # for the cross-reference pass, which is the first point that can tell whether each names a
        # `file` document. The pull-only shape rules were checked at the top of this method (see
        # _github_pairing_errors).
        if src == "github":
            self.gh_changesets.extend(_github_path_refs(where, container, rec))

        # comments on the document — only jira/confluence/github expose them (slack uses replies)
        rec_comments = rec.get("comments") or []
        ctable = store.comment_table(src)
        if rec_comments and ctable is None:
            raise SystemExit(f"{where}: comments are not supported for source_type {src!r}")
        # The parent's SERVED key, positional against store.comment_parent_columns — for github
        # that is (repo, number), and the number may still be PROVISIONAL here: the deferred pass
        # rewrites the comment rows alongside the documents they hang off.
        parent_key = self.keys[(src, doc_id)] if rec_comments else None
        prev_c_ts = created
        for j, c in enumerate(rec_comments, start=1):
            body = c.get("body") or c.get("content")
            if not body:
                raise SystemExit(f"{where}: each comment needs 'content'")
            register(c.get("author_email"), c.get("author_name"))
            # A comment with no explicit time follows the PREVIOUS comment, not the doc's clock
            # plus its position. The reason:
            # in a thread that mixes dated and undated comments, `created + j` lands an undated one
            # back at the document's creation time and any consumer ordering by createdAt (Linear's
            # `Issue.comments` does) serves the thread inverted. Monotonic, so it cannot. For an
            # all-undated thread this is exactly `created + j`, as before. Never a hash of the
            # comment's own id, which would scatter one thread across two years.
            c_ts = _epoch_field(c.get("created_ts"), f"{where}: comment {j}", "created_ts")
            # `is None`, not truthiness: 1970-01-01T00:00:00Z parses to 0, and taking the default
            # for it stores a time nobody wrote — the confusion `_epoch_field` exists to prevent.
            if c_ts is None:
                c_ts = prev_c_ts + 1
            prev_c_ts = max(prev_c_ts, c_ts)
            # The corpus's own comment id is a SEED, never stored. For github it seeds the
            # id the API reports, assigned here and probed for uniqueness — a comment's `url`
            # resolves through it, so two comments sharing one means one comment's url returns the
            # other's body. For the other five it seeds a handle those APIs derive their comment id
            # from at serve time; it is composed from the PARENT'S SERVED id, so no part of the
            # dataset's identifier scheme survives in it either.
            seed_cid = c.get("id") or f"{doc_id}::c{j}"
            _cid = (
                self._assign_github_comment_id(seed_cid)
                if src == "github"
                else _child_id(parent_key[-1], j)
            )
            pcols = store.comment_parent_columns(src)
            conn.execute(
                f"INSERT OR REPLACE INTO {ctable}"
                f"(id, {', '.join(pcols)}, seq, author_email, body, created_ts, reactions) "
                f"VALUES ({', '.join('?' for _ in range(len(pcols) + 6))})",
                (
                    _cid,
                    *parent_key,
                    j,
                    c.get("author_email"),
                    body,
                    c_ts,
                    _j(c.get("reactions")),
                ),
            )
            # github's line-anchored REVIEW comments live in the same table, discriminated by
            # `path` (see store.SCHEMA). A second statement rather than widening the shared INSERT
            # above, which serves six tables that have no such columns. `path` is present and this
            # is a pull: _github_pairing_errors settled both before any row was written.
            if src == "github" and any(k in c for k in ("path", "line", "diff_hunk")):
                conn.execute(
                    "UPDATE github_comments SET path = ?, line = ?, diff_hunk = ? WHERE id = ?",
                    (c["path"], c.get("line"), c.get("diff_hunk"), _cid),
                )

        for i, rep in enumerate(replies or [], start=1):
            if not rep.get("content"):
                raise SystemExit(f"{where}: each reply needs 'content'")
            rep_author = rep.get("author_email") or author
            register(rep_author, rep.get("author_name"))
            rep_id = rep.get("doc_id") or (
                "dsid_"
                + hashlib.sha256((doc_id + str(i) + rep["content"]).encode()).hexdigest()[:32]
            )
            seen.add((src, rep_id))
            # A reply is a full message (reactions/files/subtype/edited carry through).
            # Its second was resolved with the rest of the thread's in `_thread_seconds`,
            # which is where the ordering rule and its refusals live.
            rep_cts = reply_seconds[i - 1]
            insert(
                rep_id,
                rep_author,
                rep.get("title") or "",
                rep["content"],
                i,
                sub=rep.get("subtype"),
                ex=rep,
                cts=rep_cts,
            )

        # A gmail thread's later messages. Each is a full message in its own right — sender,
        # To/Cc, Message-ID, body — sharing the root's thread_id and ACL and carrying its position
        # in `thread_seq`, which is what `users.messages.list` / `users.threads.get` page over.
        for i, msg in enumerate(messages or [], start=1):
            # The key is required, its value may be EMPTY: 2.3% of real thread messages are
            # headers with no body (an auto-ack, a bare forward), and dropping those would drop
            # messages from the middle of a thread and renumber the rest.
            if "content" not in msg:
                raise SystemExit(f"{where}: each gmail message needs 'content'")
            # No fallback to the ROOT's author, unlike a slack reply: a thread's messages have
            # different senders by definition, so attributing an unattributed one to whoever
            # opened the thread would invent a sender. It falls through to `unknown@<org_domain>`.
            m_author = msg.get("author_email")
            register(m_author, msg.get("author_name"))
            msg_id = msg.get("doc_id") or f"{doc_id}::m{i}"
            seen.add((src, msg_id))
            # Its own `created` when given, else the root's clock + an hour per position — the
            # spread a real reply chain has, and never NULL.
            insert(
                msg_id,
                m_author,
                msg.get("title") or title,
                msg["content"],
                i,
                # `thread` is forced to the ROOT's thread: a child must never open a thread of
                # its own, or `users.threads.get` would return a one-message thread.
                ex={**msg, "thread": gmail_thread},
                cts=_message_second(
                    _epoch_field(msg.get("created"), f"{where}: message {i}", "created"),
                    created + i * 3600,
                ),
            )

        # Children are written under sequence ids (`::c{j}`, `::s{j}`, `thread_seq`), so a version
        # of a document with FEWER of them overwrote 1..n and left the old tail in place: a
        # transcript re-imported with one sentence served one sentence of content beside three
        # stored ones, breaking this importer's own rule that a transcript's content IS its
        # sentences. Trimmed rather than deleted-and-rewritten, so a child still in place under a
        # provisional parent key is left for `_settle` to move.
        self._trim_children(
            src,
            doc_id,
            # fireflies' child table IS `fireflies_sentences` (see store.COMMENT_TABLE) — a
            # transcript has sentences where the other five have comments.
            len(sentences or []) if src == "fireflies" else len(rec_comments),
            len(replies or []) + len(messages or []),
        )

    def _trim_children(self, src: str, doc_id: str, children: int, thread: int):
        """Drop the children of this document that a PREVIOUS version of it left behind.

        Everything past the count this version wrote, in both shapes a child takes: a row in the
        source's child table, and — for gmail and slack, whose thread members are full documents —
        a row in the source table itself, which takes its ACL grants and its FTS entry with it."""
        key = self.keys.get((src, doc_id))
        if key is None:
            return
        conn = self.conn
        ctable = store.comment_table(src)
        if ctable is not None:
            pcols = " AND ".join(f"{c} = ?" for c in store.comment_parent_columns(src))
            conn.execute(f"DELETE FROM {ctable} WHERE {pcols} AND seq > ?", (*key, children))
        if src in ("gmail", "slack") and thread:
            tcol, tval = ("thread_id", key[0]) if src == "gmail" else ("thread_ts", key[-1])
            scope = "" if src == "gmail" else " AND channel = ?"
            args = (tval,) if src == "gmail" else (tval, key[0])
            stale = [
                tuple(r)
                for r in conn.execute(
                    f"SELECT {', '.join(store.id_columns(src))} FROM {store.table(src)} "
                    f"WHERE {tcol} = ?{scope} AND thread_seq > ?",
                    (*args, thread),
                )
            ]
            if stale:
                where_key = " AND ".join(f"{c} = ?" for c in store.id_columns(src))
                for k in stale:
                    conn.execute(f"DELETE FROM {store.table(src)} WHERE {where_key}", k)
                    conn.execute(f"DELETE FROM {store.acl_table(src)} WHERE {where_key}", k)
                store.fts_add_docs(conn, src, stale)  # delete-then-reinsert: the row is gone

    def write_containers(self) -> None:
        """The per-service grouping rows (``slack_channels``, ``linear_teams``, ``gdrive_folders``,
        …). Deferred to the end of a load rather than written per record: a container's owning
        group is whatever its records agreed on, and the last one wins.

        Linear also gets ``served_id``/``served_key`` here, unconditionally for every row: the
        other two spellings ``team(id:)`` accepts alongside this table's own primary key (the raw
        name). No probe: like gmail/notion's own served ids the raw seed is stored as-is, since a
        collision is not a realistic concern at this digest width (see the schema comment on
        ``idx_linear_teams_served``).

        It must still fail LOUDLY if one happens, which is why the upsert below is keyed explicitly
        on ``team`` rather than being a blanket ``INSERT OR REPLACE`` -- that would resolve a
        ``served_id`` collision by silently deleting the other team's row. ``served_key`` needs no
        such guard; it is not unique to begin with (see ``linear_team_by_served_key``)."""
        for (src, name), group_id in self.containers.items():
            gtable, gcol = store.GROUPING[src]
            if src == "linear":
                self.conn.execute(
                    f"INSERT INTO {gtable}({gcol}, group_id, served_id, served_key) "
                    f"VALUES (?,?,?,?) ON CONFLICT({gcol}) DO UPDATE SET "
                    "group_id=excluded.group_id, served_id=excluded.served_id, "
                    "served_key=excluded.served_key",
                    (name, group_id, synth.linear_team_id(name), synth.linear_team_key(name)),
                )
            elif src == "jira":
                # The project's own key -- the prefix its issue keys carry. `resolve_jira_keys`
                # ran before this (see load_records' ordering) and recorded the prefix it actually
                # used in `jira_prefixes`, including for a project with no provided key at all, so
                # what lands here is always what the corpus is really served under. Upserted on
                # the PK, not `INSERT OR REPLACE`: this table now carries a non-PK UNIQUE index
                # (idx_jira_projects_key) that OR REPLACE would resolve by deleting the other
                # project's row.
                self.conn.execute(
                    f"INSERT INTO {gtable}({gcol}, key, group_id) VALUES (?,?,?) "
                    f"ON CONFLICT({gcol}) DO UPDATE SET key=excluded.key, group_id=excluded.group_id",
                    (name, self.jira_prefixes.get(name) or synth.jira_project_key(name), group_id),
                )
            else:
                self.conn.execute(
                    f"INSERT OR REPLACE INTO {gtable}({gcol}, group_id) VALUES (?,?)",
                    (name, group_id),
                )

    def resolve_cross_references(self) -> None:
        """Resolve the links whose target may only have arrived on a later record.

        Every target is named by the DATASET id the corpus wrote, and what gets stored is the
        target's SERVED id — so each of these is a lookup through `self.keys`, the run's own
        record of what it wrote each document under.

        A target loaded by an EARLIER run cannot be resolved and is refused: the dataset's
        identifiers do not outlive the import, so there is nothing left to match one against.
        Referencing across imports is deliberately not supported, and the error says so rather than
        writing a link that would silently resolve to nothing.
        """
        conn, counts = self.conn, self.counts
        hs_types, hs_links, lin_links = self.hs_types, self.hs_links, self.lin_links

        def resolve(src: str, seed, what: str, whose: str):
            key = self.keys.get((src, seed))
            if key is None:
                raise SystemExit(
                    f"{src} {what} {whose} -> {seed!r}: target not found in this corpus. A link "
                    "names its target by the identifier the corpus gave it, and that identifier "
                    "does not outlive the import — so a target loaded by an EARLIER run can no "
                    "longer be resolved. Load the target and the record that links to it together."
                )
            return key

        # HubSpot associations: one declaration becomes two rows, because real HubSpot exposes a link
        # from both records (with a distinct type id per direction) and a corpus author should not have
        # to write it twice.
        for from_doc, from_type, a in hs_links:
            to_doc = a["to"]
            # The target's EXISTENCE is resolved from what this run wrote, regardless of whether
            # `to_type` was stated explicitly -- an explicit `to_type` names what KIND the target
            # is (the schema's own words: "default: the target record's own object_type"), not a
            # license to link to a record that was never written. Accepting one would write an
            # association `store.hubspot_associations` then returns zero rows for, forever.
            to_key = resolve("hubspot", to_doc, "association", from_doc)
            from_key = resolve("hubspot", from_doc, "association", from_doc)
            to_type = a.get("to_type") or hs_types[to_doc]
            category = a.get("category") or "HUBSPOT_DEFINED"
            label = a.get("label")
            # An explicit type_id applies only to the direction the author declared; the reverse gets
            # its own synthesized id, since the two directions never share one in real HubSpot.
            for f_id, f_type, t_id, t_type, tid in (
                (from_key[0], from_type, to_key[0], to_type, a.get("type_id")),
                (to_key[0], to_type, from_key[0], from_type, None),
            ):
                conn.execute(
                    "INSERT OR REPLACE INTO hubspot_associations(from_id, from_type, to_id, "
                    "to_type, assoc_category, assoc_type_id, label) VALUES (?,?,?,?,?,?,?)",
                    (
                        f_id,
                        f_type,
                        t_id,
                        t_type,
                        category,
                        tid or synth.hubspot_assoc_type_id(f_type, t_type),
                        label,
                    ),
                )

        # Linear parents: `parent` names the target by IDENTIFIER, so it can only be resolved once
        # every issue is loaded. `Issue.children` reads `parent_id`, so without this a corpus would
        # serve `parent` but an empty `children`, and the two directions would disagree.
        if counts.get("linear"):
            key_to_id: dict[str, str] = {}
            for issue_id, ident in conn.execute(
                "SELECT id, identifier FROM linear_issues WHERE identifier IS NOT NULL ORDER BY id"
            ):
                key_to_id.setdefault(ident, issue_id)
            dangling = 0
            for issue_id, pkey in conn.execute(
                "SELECT id, parent_key FROM linear_issues WHERE parent_key IS NOT NULL"
            ).fetchall():
                target = key_to_id.get(pkey)
                if target is None:
                    # A parent names an IDENTIFIER, not a document id, and an identifier that is not
                    # in this corpus is a normal property of a real dataset rather than a corpus
                    # error: a slice of an issue tracker references issues outside it (24.8% of one
                    # real corpus's parent references do). So keep `parent_key` — it is what the
                    # corpus said — and leave `parent_id` NULL, which is exactly what
                    # `Issue.parent` serving null means. `relations` stay strict by contrast: those
                    # name a record outright, so a miss is a typo.
                    dangling += 1
                    continue
                if target != issue_id:
                    conn.execute(
                        "UPDATE linear_issues SET parent_id = ? WHERE id = ?", (target, issue_id)
                    )
            if dangling:
                print(
                    f"  linear: {dangling} parent reference(s) match no issue in this corpus; "
                    f"kept as `parent` with no resolved parent issue",
                    file=sys.stderr,
                )

        # Linear relations: resolve declared targets now that every issue id is known. A target
        # that does not exist is an error rather than a dangling relation, matching hubspot's rule.
        for from_doc, a, created_ts in lin_links:
            to_doc = a["to"]
            from_key = resolve("linear", from_doc, "relation", from_doc)
            to_key = resolve("linear", to_doc, "relation", from_doc)
            conn.execute(
                "INSERT OR REPLACE INTO linear_relations(id, from_id, to_id, type, created_ts)"
                " VALUES (?,?,?,?,?)",
                (
                    a.get("id") or f"{from_key[0]}::r{to_key[0]}",
                    from_key[0],
                    to_key[0],
                    a.get("type") or "related",
                    created_ts,
                ),
            )

        # A pull's declared changeset, against the `file` documents that now exist. Asked of the DB
        # rather than of this run's records, so a path whose file arrived in an earlier `--append`
        # resolves; memoized because a path repeats across the pulls that touched it.
        if self.gh_changesets:
            known: dict[tuple[str, str], bool] = {}

            def _resolves(repo: str, path: str) -> bool:
                if (repo, path) not in known:
                    known[(repo, path)] = (
                        store.get_repo_file(conn, repo, path) is not None
                    )  # ACL-free: see _unresolved_changed_paths
                return known[(repo, path)]

            unresolved = _unresolved_changed_paths(self.gh_changesets, _resolves)
            if unresolved:
                # A count, not the list: a load prints a summary and one line per path would bury
                # it. `--dry-run` names each one, which is where an author goes to fix a corpus.
                print(
                    f"  github: {_changed_path_count(len(unresolved))} in this corpus or the "
                    f"existing DB; each is dropped from the changeset or review comment that "
                    f"names it (`--dry-run` names them)",
                    file=sys.stderr,
                )

    def _settle(self, src: str, final: list) -> None:
        """Move rows in a deferred source from their provisional key to the settled one.

        ``final`` is a list of ``(provisional key tuple, new value)``, one entry per ROW — not per
        dataset id, because two records may share one and, in a probed source, both survive as
        separate rows. Three things move together, and they have to: the document row, every child
        row hanging off it, and the record of what that row is called (``keys``, which a
        cross-reference resolves through, and ``_final``, which the ACL grants and FTS ids are
        translated by when they are flushed).

        No two-sweep dance: a provisional key and a final one can never collide (see
        `_next_provisional`), so a row can be written onto its new value while another row is
        still sitting on its old one.
        """
        col = store.id_column(src)
        tbl = store.table(src)
        ctable = store.comment_table(src)
        # The comment table names the parent by its own spelling of that column, which for the two
        # deferred sources happens to match, but is read from the registry rather than assumed.
        ccols = store.comment_parent_columns(src) if ctable else ()
        where = " AND ".join(f"{c} = ?" for c in store.id_columns(src))
        cwhere = " AND ".join(f"{c} = ?" for c in ccols)
        # A child row's OWN id is composed from its parent's key (see `_child_id`), so it has to
        # move WITH the parent: a comment written while its parent still held a provisional key
        # would otherwise keep `-unassigned-3::c1` as the id the API serves it under. Recomposed in
        # SQL from the row's own `seq` so one statement covers every comment on the parent.
        # github's is exempt: its comment id is an assigned integer that does not mention its
        # parent at all (see `_assign_github_comment_id`).
        recompose = ctable and src != "github"
        for key, value in final:
            if key[-1] == value:
                continue
            scope, settled = key[:-1], (*key[:-1], value)
            self.conn.execute(f"UPDATE {tbl} SET {col} = ? WHERE {where}", (value, *scope, key[-1]))
            if ctable:
                cset = f"{ccols[-1]} = ?" + (
                    f", id = ? || '{_CHILD_SEP}' || seq" if recompose else ""
                )
                cparams = [value, value] if recompose else [value]
                self.conn.execute(
                    f"UPDATE {ctable} SET {cset} WHERE {cwhere}",
                    (*cparams, *scope, key[-1]),
                )
            self._final[(src, key)] = settled
        # `keys` is last-writer-wins per dataset id, so it is repointed by looking up what the
        # value it currently holds became -- never by replaying the list, which would leave it on
        # whichever row happened to come last in `final` rather than last in the corpus.
        for (ksrc, did), key in list(self.keys.items()):
            if ksrc == src and (src, key) in self._final:
                self.keys[(ksrc, did)] = self._final[(src, key)]

    def _settled(self, src: str, key: tuple) -> tuple:
        """The key a row ended up under, given the one it was written with."""
        return self._final.get((src, key), key)

    def resolve_github_numbers(self) -> None:
        """Assign a real `number` to every github row that landed under a provisional one, in two
        phases — corpus-provided numbers claim their spelling FIRST, corpus-wide, and only
        THEN does every remaining row probe.

        This cannot happen inside `add`'s per-record insert, which is why it is here rather than
        there: which numbers a repo's rows without one may take depends on which numbers the
        REST of the repo's rows — including ones not yet loaded — claim outright. A row processed
        early has no way to know that a later record in the same corpus will provide the exact
        number it is about to probe into; running the probe while streaming would let that early
        row win a race a real GitHub numbering scheme never lets it enter.

        Assignment runs in DATASET-ID order, which is what keeps the result independent of the
        order records happened to stream in — the property the old `ORDER BY doc_id` gave, kept
        without storing the value it ordered by.

        `kind='file'` rows are assigned here too, and last is not a place they land by accident:
        every provided issue/PR number has already claimed its spelling in phase 1, so a file can
        only ever take a number no real issue asked for. Its number is never served — a file is
        addressed by (repo, path) — it exists so the row is addressable under the primary key at
        all (see the schema).

        A provided row ALSO answering at its own synthesized spelling, as an alias, is not done on
        purpose: one row holds ONE number, and a provided issue answering at a second, unrequested
        number is not something real GitHub does either — `/issues/<not-this-issue's>` 404s there.
        Closing that gap is the point of the stored key, not a side effect of it.
        """
        if not self.counts.get("github"):
            return
        # Phase 1: every REAL number in the table claims its spelling before anything probes —
        # this run's provided ones, and any a previous run settled. Provisional numbers are
        # negative, so they are exactly the rows still to be assigned.
        taken: dict[str, set[int]] = {}
        for repo, number in self.conn.execute(
            "SELECT repo, number FROM github_items WHERE number >= 0"
        ):
            taken.setdefault(repo, set()).add(int(number))
        # Phase 2: everything else, ISSUES AND PULLS FIRST and only then files, each group in
        # dataset-id order — which is what keeps the result independent of the order records
        # happened to stream in. The provisional key breaks a tie between two records that shared a
        # dataset id, so the order is still total.
        #
        # Files last is what keeps a file's number from displacing an issue's. Provided numbers
        # claimed their spelling in phase 1, but a KEYLESS issue probes here — and sorting the two
        # groups together let a file whose dataset id happened to sort earlier take the number that
        # issue's own seed produces, silently renumbering a real issue to make room for a value no
        # route ever serves.
        final: list = []
        pending = self._pending.get("github", [])
        for key, did in sorted(pending, key=lambda e: (e[0] in self._github_file_keys, e[1], e[0])):
            repo = key[0]
            bucket = taken.setdefault(repo, set())
            candidate = self._assign_github_number(did, repo, taken)
            bucket.add(candidate)
            final.append((key, candidate))
        self._settle("github", final)

    def resolve_jira_keys(self) -> None:
        """Assign a real `key` to every jira row that landed under a provisional one, in the same
        two phases `resolve_github_numbers` uses and for the same reason: a corpus-provided key
        must claim its spelling ahead of every composed one, corpus-wide, and a record arriving
        early cannot know what a later one will provide.

        The whole key is composed here, prefix included, which is only possible at this point:
        the prefix is the PROJECT's key, and which spelling a project answers at is not settled
        until every provided key in the corpus has been seen. Composing it during the stream would
        have written the synthesized prefix onto a project whose later records provide `PAY-7`,
        leaving one project serving two spellings at once.
        """
        if not self.counts.get("jira"):
            return
        # Phase 1: every settled key claims its suffix. A provisional key has no `-N` suffix to
        # read, so those are skipped by the same test that identifies them.
        taken: dict[str, set[int]] = {}
        for project, key in self.conn.execute("SELECT project, key FROM jira_issues"):
            if str(key).startswith("-unassigned-"):
                continue
            taken.setdefault(project, set()).add(int(str(key).rsplit("-", 1)[-1]))
        final: list = []
        for key, did in sorted(self._pending.get("jira", []), key=lambda e: (e[1], e[0])):
            project = self._jira_projects[did]
            bucket = taken.setdefault(project, set())
            candidate = self._assign_jira_number(did, project, taken)
            bucket.add(candidate)
            prefix = self.jira_prefixes.get(project) or synth.jira_project_key(project)
            # Record it: a composed key names its project exactly as a provided one does, and
            # `write_containers` stores this on the project's own row.
            self.jira_prefixes.setdefault(project, prefix)
            final.append((key, f"{prefix}-{candidate}"))
        self._settle("jira", final)

    def write_linear_entities(self) -> None:
        """Store the ids Linear's by-id roots reverse: project, workflow state, label, cycle, user
        and release.

        Each is a DISTINCT value of one `linear_issues` column, hashed to a uuid the SDK asks for
        lazily (`await issue.state` fires a fresh `workflowState(id:)`). The six scans run once
        here, and the app serves them from `linear_entities`.

        Rebuilt whole rather than appended to: the source is a DISTINCT over the live table, so
        after an `--append` the correct contents are a function of every issue now present, not of
        the ones this run happened to add. A row deleted here and re-inserted with the same id is
        the same row -- these ids are pure hashes of a NAME (never probed), so nothing a client
        holds can move.
        """
        if not self.counts.get("linear"):
            return
        distinct = store.linear_distinct_values(self.conn)
        rows: list[tuple] = []
        # (kind, the id function, the rows it applies to) -- `team` and `display` are set only for
        # the kinds whose value has one, matching store.LINEAR_ENTITY_VALUE.
        for email, display in distinct["users"]:
            rows.append(("user", synth.linear_user_id(email), None, email, display))
        for team, name in distinct["states"]:
            rows.append(("state", synth.linear_state_id(name, team), team, name, None))
        for team, name in distinct["cycles"]:
            rows.append(("cycle", synth.linear_cycle_id(name, team), team, name, None))
        for name in distinct["projects"]:
            rows.append(("project", synth.linear_project_id(name), None, name, None))
        for name in distinct["labels"]:
            rows.append(("label", synth.linear_label_id(name), None, name, None))
        for name in distinct["releases"]:
            rows.append(("release", synth.linear_release_id(name), None, name, None))
        self.conn.execute("DELETE FROM linear_entities")
        # `OR REPLACE` on the (kind, served_id) primary key: two DIFFERENT names can hash to one id
        # only by a digest collision, which is vanishingly unlikely -- but the entities are drawn
        # from free-text corpus values, so the import must not abort on one. The loser is simply
        # unreachable by id, exactly as it was when this lived in a dict and `[id] = value` kept the
        # last writer.
        self.conn.executemany(
            "INSERT OR REPLACE INTO linear_entities(kind, served_id, team, name, display) "
            "VALUES (?,?,?,?,?)",
            rows,
        )

    def resolve_probed_ids(self, src: str) -> None:
        """Settle every confluence page / hubspot record that landed under a provisional id.

        The same two phases `resolve_github_numbers` runs, without the two things that make github's
        and jira's own passes special: there is no container to scope the probe to, and nothing to
        compose the value out of. Phase 1 is implicit -- every REAL value in the column is already a
        claim, whether this run's corpus stated it or a previous run assigned it -- and phase 2
        probes the rest in DATASET-ID order, which is what keeps the result independent of the order
        records happened to stream in.
        """
        if not self.counts.get(src):
            return
        col, tbl = store.id_column(src), store.table(src)
        assign = {
            "confluence": self._assign_confluence_id,
            "hubspot": self._assign_hubspot_id,
        }[src]
        # hubspot's probe works in integers (see _assign_hubspot_id); confluence's column already
        # is one.
        cast = int if src == "hubspot" else (lambda v: v)
        taken = {
            cast(v)
            for (v,) in self.conn.execute(f"SELECT {col} FROM {tbl}")
            if not self._is_provisional(v)
        }
        final: list = []
        for key, did in sorted(self._pending.get(src, []), key=lambda e: (e[1], e[0])):
            value = assign(did, taken)
            taken.add(cast(value))
            final.append((key, value))
        self._settle(src, final)

    def resolve_parents(self) -> None:
        """Point every subtask / child page at its parent's settled key.

        Runs after the deferred assignment passes rather than inside
        `resolve_cross_references`, and that ordering is the whole reason it is a separate pass: a
        parent is named by the identifier the corpus gave it, and for a deferred source the key that
        identifier resolves to does not exist until the assignment above has run.

        A parent loaded by an EARLIER run cannot be resolved and is refused -- the identifier
        naming it did not outlive that import, and writing the link anyway would leave `children`
        silently empty for a page that serves a `parent`.
        """
        for src, parents in self._parent_seeds.items():
            col, tbl = store.id_column(src), store.table(src)
            for did, parent_seed in parents.items():
                key = self.keys.get((src, did))
                if key is None:
                    continue  # the row was never written
                if src == "notion":
                    # Already linked (see `insert`); what is left is whether the page it points at
                    # is really there — in this corpus or in the DB an --append is adding to.
                    if store.notion_by_id(self.conn, synth.notion_id(parent_seed)) is None:
                        raise SystemExit(
                            f"notion {key[-1]}: parent {parent_seed!r} names no imported page. The "
                            "page would serve a `parent` id that nothing resolves, and its "
                            "`children` would never list it. Load the parent, or drop the field."
                        )
                    continue
                target = self.keys.get((src, parent_seed))
                if target is None:
                    raise SystemExit(
                        f"{src} {key[-1]}: parent {parent_seed!r} was not imported in this run. A "
                        "parent is named by the identifier the corpus gave it, and that identifier "
                        "does not outlive the import -- so a parent loaded by an EARLIER run can "
                        "no longer be resolved. Load the parent and its child together."
                    )
                self.conn.execute(
                    f"UPDATE {tbl} SET parent_id = ? WHERE {col} = ?", (target[-1], key[-1])
                )


def load(
    path: Path, settings: Settings | None = None, reset: bool = True, roster: Path | None = None
) -> dict:
    """Load a BYO-JSONL corpus — a file, a ``.jsonl.gz``, or a sharded directory — into the DB."""

    def _from_file():
        for lineno, line in corpus_records(path):
            line = line.strip()
            if not line:
                continue
            try:
                yield lineno, json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"line {lineno}: invalid JSON: {e}")

    return load_records(_from_file, settings, reset, roster)


def load_records(
    records_factory,
    settings: Settings | None = None,
    reset: bool = True,
    roster: Path | None = None,
    validate: bool = True,
) -> dict:
    """Load already-parsed BYO records into the DB, leaving the previous one in place if it fails.

    A fresh (non-append) load replaces a corpus. Deleting the old DB up front would leave a typo on
    line 40,000 with no corpus at all — an empty schema-only DB beside a tokens.yaml describing the
    one that is gone — so the old file is MOVED aside and moved back if anything raises, a rename
    rather than a copy. An --append is already all-or-nothing (one commit for the whole import).
    """
    settings = settings or get_settings()
    salvage = None
    if reset and settings.db_path.exists():
        salvage = settings.db_path.with_name(settings.db_path.name + ".replaced")
        salvage.unlink(missing_ok=True)
        settings.db_path.rename(salvage)
    try:
        result = _load_records(records_factory, settings, reset, roster, validate)
    except BaseException:
        if salvage is not None:
            settings.db_path.unlink(missing_ok=True)
            salvage.rename(settings.db_path)
        raise
    if salvage is not None:
        salvage.unlink(missing_ok=True)
    return result


def _load_records(
    records_factory,
    settings: Settings,
    reset: bool,
    roster: Path | None,
    validate: bool,
) -> dict:
    """The load itself. See :func:`load_records`, which is this plus the replace-safely dance.

    ``records_factory`` returns a FRESH iterator of ``(where, record)`` pairs and may be called
    twice — the org has to be inferred from every author's address before the first grant is
    written, so a corpus is re-read rather than held in memory. ``where`` names the record in an
    error message. ``validate=False`` skips the JSON Schema check, and is only for records an
    importer in this repo generated itself; a corpus from OUTSIDE always validates, which is why
    ``load`` does not expose the flag.
    """
    conn = store.connect_rw(settings.db_path)

    # infer the org from the corpus (dominant email domain) before building any grants,
    # since public docs are granted to the org principal — see _infer_org. Read in its own pass,
    # keeping only the emails: a sharded corpus is streamed rather than held in memory.
    def _scanned():
        """Just what `_emails` reads, one record at a time.

        `infer_org` consumes the addresses exactly once (backlot/config.py), so nothing needs to be held:
        yielding keeps the memory constant where a list would build one dict per document plus one
        per child row — millions of them on a large corpus, in a pass whose whole point is to stream.
        """
        for _no, _rec in records_factory():
            yield {
                **{k: _rec[k] for k in ("author_email", "host_email", "readers") if k in _rec},
                **{
                    c: [
                        {"author_email": r.get("author_email")}
                        for r in (_rec.get(c) or [])
                        if isinstance(r, dict)
                    ]
                    for c in ("comments", "sentences", "messages", "replies")
                    if c in _rec
                },
            }

    roster_data = load_roster(roster) if roster else None
    closed = roster_data is not None
    # A roster states the org rather than leaving it to be guessed from the dominant author domain
    # — which a converted corpus can get wrong, since its documents also carry outside senders and
    # display-only handles. When it states BOTH, the inference pass is pure cost: every value it
    # would produce is about to be overwritten. Skipping it also halves the passes an in-memory
    # caller makes over a corpus it generates on the fly.
    stated = bool(roster_data and roster_data.get("org") and roster_data.get("org_domain"))
    if stated:
        org_name, org_domain = roster_data["org"], roster_data["org_domain"]
    else:
        org_name, org_domain = _infer_org(_scanned(), settings)
        if closed:
            org_name = roster_data.get("org") or org_name
            org_domain = roster_data.get("org_domain") or org_domain
    if not reset:
        row = conn.execute("SELECT id FROM principals WHERE type='org' LIMIT 1").fetchone()
        if row:
            org_name = row[0]
    org = org_name

    loader = _Loader(conn, org, org_domain, closed=closed, validate=validate)
    if not reset:
        loader.seed_tracker_ids()
    source_docs = 0
    for lineno, rec in records_factory():
        source_docs += 1
        loader.add(rec, f"line {lineno}")
    # Order matters. The two deferred passes settle every provisional key FIRST, so that
    # everything downstream — the jira parent links, the cross-reference targets, the ACL grants
    # and the FTS ids, all of which name a document by the dataset id it came in under — resolves
    # through `loader.keys` after it holds final values rather than provisional ones.
    loader.resolve_github_numbers()
    loader.resolve_jira_keys()
    loader.resolve_probed_ids("confluence")
    loader.resolve_probed_ids("hubspot")
    loader.resolve_parents()
    loader.resolve_cross_references()
    # After the linear rows are final: its contents are a DISTINCT over them.
    loader.write_linear_entities()
    users, groups = loader.users, loader.groups
    memberships, grants = loader.memberships, loader.grants
    counts, fts_ids = loader.counts, loader.fts_ids

    if closed:
        # The roster IS the principal set: users, the groups they belong to, and the memberships
        # between them. Nothing the records referenced adds to it.
        users = {e: u["name"] for e, u in roster_data["users"].items()}
        groups = {g for u in roster_data["users"].values() for g in u["groups"]}
        memberships = {(g, e) for e, u in roster_data["users"].items() for g in u["groups"]}

    # principals: org, groups, users
    conn.execute("INSERT OR REPLACE INTO principals VALUES (?,?,?,?)", (org, "org", org, None))
    for g in groups:
        conn.execute("INSERT OR REPLACE INTO principals VALUES (?,?,?,?)", (g, "group", g, None))
    for email, name in users.items():
        conn.execute(
            "INSERT OR REPLACE INTO principals VALUES (?,?,?,?)", (email, "user", name, email)
        )
    # Every USER principal, unconditionally -- no org/group row ever reaches this loop, so there
    # is no NULL case to get wrong (see the schema comment on fireflies_users). Keyed
    # explicitly on `email` (this table's PRIMARY KEY), not a blanket `INSERT OR REPLACE`: a
    # `served_id` collision between two DIFFERENT emails must raise through the unique index
    # rather than have `OR REPLACE` silently delete the other email's row out from under it (the
    # same hazard the doc tables' own upsert avoids -- see `insert`'s comment on it).
    for email in users:
        conn.execute(
            "INSERT INTO fireflies_users(email, served_id) VALUES (?,?) "
            "ON CONFLICT(email) DO UPDATE SET served_id=excluded.served_id",
            (email, synth.fireflies_user_id(email)),
        )
    loader.write_containers()
    for g, email in memberships:
        conn.execute("INSERT OR REPLACE INTO group_members VALUES (?,?)", (g, email))
    # A grant names its document by the SERVED id, in the same columns the document table is
    # keyed on (see store.ACL_TABLE) — resolved here, once every deferred key has settled, rather
    # than written during the stream under a value that was still provisional.
    #
    # A row's grants REPLACE whatever it had, rather than adding to them: a document rewritten by
    # a later record (two records sharing a dataset id, or a re-import landing on the same
    # synthesized id) serves the later record's content, so serving the earlier record's readers
    # beside it would let a reader the corpus dropped keep reading a document it no longer names.
    # Append-only grants could widen a reader set and never narrow it, and nothing in a corpus
    # could take a reader back.
    regranted = set()
    for source_type, written_key, ptype, pid in grants:
        key = loader._settled(source_type, written_key)
        if (source_type, key) not in regranted:
            regranted.add((source_type, key))
            where = " AND ".join(f"{c} = ?" for c in store.id_columns(source_type))
            conn.execute(f"DELETE FROM {store.acl_table(source_type)} WHERE {where}", key)
        conn.execute(
            f"INSERT OR IGNORE INTO {store.acl_table(source_type)} "
            f"VALUES ({', '.join('?' for _ in range(len(key) + 2))})",
            (*key, ptype, pid),
        )
    conn.commit()
    if reset:
        store.build_fts(conn)  # full-text index for search (search.messages / confluence CQL)
    else:
        for s, ids in fts_ids.items():
            # Same resolution as the grants above: these were accumulated as dataset ids and are
            # translated to served keys now that the deferred passes have settled them.
            store.fts_add_docs(conn, s, [loader._settled(s, k) for k in ids])

    # Every principal is a document owner/reader; only some are ACCOUNTS. Without a roster the two
    # sets coincide (a corpus's authors are its users); with one, `contacts` are principals with no
    # token, so they show as owners and grantees but never authenticate.
    tokened = (
        users
        if not closed
        else {e: u["name"] for e, u in roster_data["users"].items() if u["token"]}
    )
    users_rows = {e: {"email": e, "name": n, "token": _user_token(e)} for e, n in tokened.items()}
    token_org, token_domain = org_name, org_domain
    token_admin = settings.admin_token
    if not reset and settings.tokens_path.exists():
        prev = yaml.safe_load(settings.tokens_path.read_text()) or {}
        token_org = prev.get("org", token_org)
        token_domain = prev.get("org_domain", token_domain)
        token_admin = prev.get("admin_token", settings.admin_token)
        merged = {u["email"]: u for u in prev.get("users", [])}
        for e, row in users_rows.items():
            merged.setdefault(e, row)
        users_rows = merged
    tokens = {
        "org": token_org,
        "org_domain": token_domain,
        "admin_token": token_admin,
        "users": [users_rows[k] for k in sorted(users_rows)],
    }
    settings.tokens_path.write_text(yaml.safe_dump(tokens, sort_keys=False))
    from backlot import oauth

    oauth.generate(settings, org=org_name)

    # `source_documents` is what the corpus OFFERED, not COUNT(*) of the rows it produced: faithful
    # parsing promotes structure (a slack thread's replies, a gmail thread's later messages) into
    # extra rows within the SAME source document, so the count is one per `records_factory()` item,
    # taken from the load pass above rather than the earlier org-inference pass (which would double
    # it — see the docstring). On append the prior value already reflects earlier loads.
    prior = 0 if reset else int(store.read_meta(conn, "source_documents") or 0)
    store.write_meta(conn, "source_documents", prior + source_docs)
    conn.close()
    return {
        "counts": counts,
        "users": len(users),
        "groups": len(groups),
        "org": org_name,
        "org_domain": org_domain,
        "total": sum(counts.values()),
    }


def run(
    corpus: Path, *, append: bool = False, dry_run: bool = False, roster: Path | None = None
) -> int:
    """Load ``corpus`` into the mock DB (or, with ``dry_run``, only validate it).

    Takes keyword arguments rather than an argv list: the command line that reaches this lives in
    ``backlot.cli``, which is where every flag and its help text is declared. Nothing here parses
    arguments, so there is no second place for a default to drift.
    """
    corpus = Path(corpus)

    if dry_run:
        # A sharded artifact is checked against its manifest first: a truncated download is a
        # different failure from a bad record, and saying so is cheaper than a schema error.
        _verify_or_die(corpus)
        problems, n = [], 0
        # Enough to resolve a declared changeset afterwards: which paths each repo has, and which
        # pull declared what. Paths only — a record's content is never held, so this stays flat in a
        # pass whose whole point is to stream a corpus too large to load.
        gh_files: dict[str, set[str]] = {}
        gh_changesets: list[tuple[str, str, list[str]]] = []
        for lineno, line in corpus_records(corpus):
            line = line.strip()
            if not line:
                continue
            n += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                problems.append((lineno, f"invalid JSON: {e}"))
                continue
            errs = record_errors(rec)
            if isinstance(rec, dict) and rec.get("source_type") == "github":
                errs += _github_pairing_errors(rec)
                # Only from a record that is otherwise sound: its own `path` may be the thing that
                # is wrong, and an unresolved reference to it would then blame the pull for a typo
                # one line up.
                if not errs:
                    repo = str(rec.get("repo") or "github")  # as _Loader.add derives it
                    if rec.get("subtype") == "file" and rec.get("path"):
                        gh_files.setdefault(repo, set()).add(rec["path"])
                    else:
                        gh_changesets.extend(_github_path_refs(f"line {lineno}", repo, rec))
            problems.extend((lineno, m) for m in errs)
        unresolved = _unresolved_changed_paths(
            gh_changesets, lambda repo, path: path in gh_files.get(repo, ())
        )
        if unresolved:
            # Reported, and the exit code left alone: a slice of a repo is a legitimate corpus, so
            # this cannot gate one (see _unresolved_changed_paths). Named one per line, because
            # finding the typo is the whole point of the report.
            print(
                f"NOTE: {_changed_path_count(len(unresolved))} in {corpus}; each is dropped "
                f"from the changeset or review comment that names it",
                file=sys.stderr,
            )
            for where, path in unresolved:
                print(f"  {where}: {path}", file=sys.stderr)
        if not problems:
            print(f"OK: {n} records valid.")
            return 0
        print(f"INVALID: {len(problems)} problem(s) in {corpus}", file=sys.stderr)
        for lineno, msg in problems:
            print(f"  line {lineno}: {msg}", file=sys.stderr)
        return 1

    # The same check `--dry-run` makes, on the path that actually writes a database. A shard that is
    # short but validly terminated — what a resumed or re-uploaded download looks like — otherwise
    # loads as a quietly incomplete corpus and reports success. Measured at 0.67 s of sha256 over a
    # 1.0 GB artifact, in front of an import that writes 14 GB.
    _verify_or_die(corpus)

    settings = get_settings()
    # A sharded artifact ships its own roster beside the manifest, so importing one takes the
    # directory and nothing else; `--roster` still wins when it is given.
    if roster is None and corpus.is_dir() and (corpus / "roster.yaml").exists():
        roster = corpus / "roster.yaml"
        print(f"using the artifact's own roster: {roster}", file=sys.stderr)
    res = load(corpus, settings, reset=not append, roster=roster)
    print(f"Loaded {res['total']} documents into {settings.db_path}")
    for src, n in sorted(res["counts"].items()):
        print(f"  {src:14s} {n}")
    print(f"Principals: {res['users']} users, {res['groups']} groups")
    print(f"Tokens written to {settings.tokens_path}")
    return 0


if __name__ == "__main__":
    # `python -m backlot.importer.byo <flags>` is `backlot import <flags>`, re-entered through the
    # CLI so the flags are parsed by the one parser that declares them. Kept because CONTRIBUTING
    # documents this spelling and it is what a source checkout without the console script has.
    from backlot.cli import BYO, module_main

    raise SystemExit(module_main(BYO, sys.argv[1:]))
