"""Load a Bring-Your-Own (BYO) corpus from JSONL into the mock DB.

Serve *any* document set through all eleven vendor APIs — not just EnterpriseRAG-Bench.
Each line is one document:

    {
      "source_type": "confluence",        # required: one of the eleven served sources
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

Usage:  python -m backlot.importer.byo path/to/corpus.jsonl [--append | --dry-run] [--roster r.yaml]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path  # noqa: F401  (kept for typing/backcompat)

import yaml

from backlot import store, synth
from backlot.config import Settings, get_settings, infer_org
from backlot.validation import record_errors


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


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
    ORG principal at all, so a document that is org-readable *and* names its owners had no
    spelling: `readers` replaced the org grant instead of adding to it."""
    for t in ("user", "group", "org"):
        if pid.startswith(t + ":"):
            return t, pid[len(t) + 1 :]
    return ("user", pid) if "@" in pid else ("group", pid)


def _epoch(v):
    """Parse a BYO time (epoch seconds int/float, or ISO 8601 string) -> unix seconds.

    Returns None for a missing/unparseable value, so the router falls back to the
    deterministic synthesized timestamp."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    from datetime import datetime

    s = str(v).strip().replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


def _service_columns(
    src,
    ex,
    subtype,
    parent_id,
    doc_id,
    thread_id,
    seq,
    org_domain,
    created=None,
    updated=None,
    owner_display=None,
) -> dict:
    """Map generic BYO fields (+ meta) to the target service table's own columns.

    ``created``/``updated`` are pre-parsed epoch seconds (or None); slack/gmail carry only
    ``created_ts``.

    ``owner_display`` is the owner's name AS THE CORPUS WROTE IT, which the caller reads from
    whichever field the service names it in (``author_name``, gmail's ``mailbox_owner``, fireflies'
    ``host_name``). Stored rather than derived from the address, because an accented or initialled
    name ("Tomás Rré", "Aisha K. Patel") does not survive the round trip through
    ``<slug>@<domain>``. slack/notion/s3 have no such column — those APIs expose no owner name."""
    if src == "slack":
        return {
            "thread_id": thread_id,
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
        # `thread` names the thread this message belongs to (default: the doc's own id), so every
        # message of a multi-message thread shares one thread_id while carrying its own position
        # in `thread_seq`.
        return {
            "thread_id": ex.get("thread") or doc_id,
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
            "owner_display": owner_display,
        }
    if src == "confluence":
        return {
            "subtype": subtype or "page",
            "parent_id": parent_id,
            "labels": _j(ex.get("labels")),
            "created_ts": created,
            "updated_ts": updated,
            "version_number": ex.get("version_number"),
            "version_message": ex.get("version_message"),
            "minor_edit": (1 if ex.get("minor_edit") else None),
            # Confluence's own confidentiality label, free text and stored verbatim rather
            # than forced into an enum: the bench writes "restricted (customer-sensitive)" and
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
        from backlot.importer.erb import linear_priority

        return {
            "identifier": ex.get("identifier"),
            "state": ex.get("state"),
            "priority": linear_priority(ex.get("priority")),
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
            # human identifier (ENG-123), not a doc_id, because that is how Linear and the
            # bench both name a parent.
            "parent_key": parent_id,
            "release": ex.get("release"),
        }
    if src == "fireflies":
        # Keys are the Fireflies API's own, so a corpus written against it needs no renaming.
        # `transcript_id` and the three media/web URLs are derived from the doc_id when omitted,
        # and MATERIALIZED here rather than synthesized per request for the same reason Linear's
        # `identifier` is: `transcript(id:)` has to resolve the id the API just handed the caller,
        # and the app's reverse index is built from stored columns.
        tid = ex.get("transcript_id") or synth.fireflies_id(doc_id)
        return {
            "transcript_id": tid,
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
            "meeting_link": ex.get("meeting_link") or synth.fireflies_meeting_link(doc_id),
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
        contacts:                         # principals with NO token (display-only)
          - {name: Zoe Newperson, email: zoe.newperson@redwoodinference.com, group: engineering}

    ``departments`` is exactly the shape of the bench's ``employee_directory.yaml``, so that file
    works as a roster verbatim; a department name becomes its group id via ``slugify``.
    ``contacts`` are people a corpus names who are not accounts — they own and read documents but
    cannot authenticate, the distinction ``tokens.yaml`` draws.

    With a roster, `principals`, `group_members` and `tokens.yaml` come from it ALONE: a record's
    `author_email` / `readers` become references into it, and an address absent from it (a Slack
    handle, an outside sender) stays a plain address instead of becoming an account with a token.
    """
    data = yaml.safe_load(Path(path).read_text()) or {}
    users: dict[str, dict] = {}
    for dept, people in (data.get("departments") or {}).items():
        for p in people or []:
            users[p["email"]] = {
                "name": p.get("name") or _display_name(p["email"]),
                "group": slugify(dept) or None,
                "token": True,
            }
    for p in data.get("contacts") or []:
        # A contact never upgrades an account: `departments` is the authenticating roster, so a
        # duplicated email keeps its token rather than losing it to listing order.
        users.setdefault(
            p["email"],
            {
                "name": p.get("name") or _display_name(p["email"]),
                "group": (slugify(p["group"]) if p.get("group") else None),
                "token": False,
            },
        )
    return {"org": data.get("org"), "org_domain": data.get("org_domain"), "users": users}


class _Loader:
    """Inserts BYO records into an open DB, accumulating the corpus-level state the principal, ACL
    and cross-reference passes need afterwards.

    A class rather than one long loop because a whole corpus and a single document (the ERB
    importer's unit) have to go through ONE implementation or they drift. Cross-record work — a
    HubSpot association's target, a Linear parent's identifier — is deferred to
    :meth:`resolve_cross_references`, since the target may arrive on a later record.
    """

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
        self.grants = []  # (doc_id, principal_type, principal_id)
        self.counts = {}
        self.seen = set()  # (source_type, doc_id)
        self.fts_ids = {}
        # HubSpot associations are resolved after the whole corpus is read: a link may name a target
        # that appears on a later line, and an omitted `to_type` is filled in from the target's own
        # object type. doc_id -> object_type, plus the declared links.
        self.hs_types = {}
        self.hs_links = []  # (from_doc_id, from_type, declaration)
        # Linear relations name a target by doc_id and are resolved after the whole corpus is read,
        # since a target may appear on a later line — the same second pass the ERB importer runs.
        self.lin_links = []

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
        # Slack messages have no title; the other five carry a natural one.
        title = rec.get("title") or ""

        # Fireflies: `content` is DEFINED as the sentence concatenation, so the two can never be
        # allowed to disagree. A record that supplies `sentences` has its content derived from
        # them; one that supplies only `content` has its sentences parsed back out of it, so a BYO
        # author can write a plain "Speaker: text" transcript and still get per-sentence rows.
        # Either way the round-trip holds, exactly as it does for an ERB import. This runs before
        # `_doc_id`, which hashes the content — so the id covers the transcript either way.
        sentences = None
        if src == "fireflies":
            # The same parser the ERB loader uses, so a BYO transcript and a bench transcript are
            # stored identically.
            from backlot.importer.erb import parse_fireflies_transcript

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
                sentences = parse_fireflies_transcript(rec.get("content") or "")
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
        # written, and the row-level `INSERT OR REPLACE` leaves the later one — which is what a
        # direct ERB import of the same documents produces. The bench has four such pairs (three
        # across sources, one within jira); skipping the repeat instead would keep the earlier
        # document and diverge.
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
        created = _epoch(rec.get("created"))
        if created is None:
            created = synth.epoch(doc_id)
        updated = _epoch(rec.get("updated"))

        replies = rec.get("replies") if src == "slack" else None
        thread_id = doc_id if replies else None
        # gmail's own child-row array. `replies` stays Slack-only (a Slack reply is a *reply*,
        # with reactions and files); a Gmail thread is a sequence of full RFC822 messages, each
        # with its own sender, recipients and Message-ID, so it gets an array that reads like one
        # — the same per-source choice `sentences` makes for a Fireflies transcript (#15).
        messages = rec.get("messages") if src == "gmail" else None
        # The thread every message of this record belongs to: the record's own `thread` when it
        # names one, else its doc_id — the SAME expression `_service_columns` applies to the root,
        # so a record that opens a thread under an explicit id keeps its messages in it rather than
        # splitting them into a second thread named after the root's doc_id.
        gmail_thread = (rec.get("thread") or doc_id) if src == "gmail" else None
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
            from backlot.importer.erb import _ff_speaker_stats

            secs = (_ff_minutes(rec.get("duration")) or 0) * 60 or None
            extras["analytics"] = extras.get("analytics") or synth.fireflies_analytics(
                doc_id, _ff_speaker_stats(sentences), secs
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
            cols = _service_columns(
                src, ex or {}, sub, par, did, thread_id, seq, org_domain, cts, uts, odisp
            )
            cols.update(
                doc_id=did, author_email=email or f"unknown@{org_domain}", title=ttl, content=body
            )
            if src == "s3" and cols.get("size") is None:
                cols["size"] = len((body or "").encode("utf-8"))
            cols[gcol] = container
            if src == "linear" and not cols.get("identifier"):
                # MATERIALIZE the identifier the server would otherwise synthesize per request.
                # Same reasoning as created_ts above: an id that is served has to be resolvable,
                # and the app's reverse index is built from stored columns — so a serve-time-only
                # identifier came back "Entity not found" from `issue(id: "ENG-749")` even though
                # the API had just handed the caller that exact string. Deterministic, so the
                # served value is unchanged; it is just written down now.
                cols["identifier"] = synth.linear_identifier(did, synth.linear_team_key(container))
            names = list(cols)
            conn.execute(
                f"INSERT OR REPLACE INTO {store.table(src)} ({', '.join(names)}) "
                f"VALUES ({', '.join('?' for _ in names)})",
                [cols[n] for n in names],
            )
            fts_ids.setdefault(src, []).append(did)
            counts[src] = counts.get(src, 0) + 1
            for pt, pid in grant_types:
                grants.append((did, pt, pid))

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

        if src == "linear":
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
                    "INSERT OR REPLACE INTO linear_attachments(id, doc_id, seq, title, url, "
                    "subtitle, source_type, created_ts) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        f"{doc_id}::a{j}",
                        doc_id,
                        j,
                        title,
                        url,
                        (att.get("subtitle") if isinstance(att, dict) else None),
                        (att.get("sourceType") if isinstance(att, dict) else None),
                        created,
                    ),
                )
            for a in rec.get("relations") or []:
                lin_links.append((doc_id, a, created))

        if src == "hubspot":
            hs_types[doc_id] = container
            for a in rec.get("associations") or []:
                hs_links.append((doc_id, container, a))

        for j, s in enumerate(sentences or [], start=1):
            register(s.get("author_email"), s.get("speaker_name"))
            conn.execute(
                "INSERT OR REPLACE INTO fireflies_sentences(id, doc_id, seq, author_email, body, "
                "created_ts, reactions, speaker_name, speaker_id, start_time, end_time) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                # A sentence sits on the MEETING's clock plus its own offset, so ordering by time
                # never shuffles a transcript (same reasoning as a comment's created + j above).
                (
                    f"{doc_id}::s{j}",
                    doc_id,
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

        # comments on the document — only jira/confluence/github expose them (slack uses replies)
        rec_comments = rec.get("comments") or []
        ctable = store.comment_table(src)
        if rec_comments and ctable is None:
            raise SystemExit(f"{where}: comments are not supported for source_type {src!r}")
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
            c_ts = _epoch(c.get("created_ts")) or (prev_c_ts + 1)
            prev_c_ts = max(prev_c_ts, c_ts)
            conn.execute(
                f"INSERT OR REPLACE INTO {ctable}"
                "(id, doc_id, seq, author_email, body, created_ts, reactions) VALUES (?,?,?,?,?,?,?)",
                (
                    _cid := c.get("id") or f"{doc_id}::c{j}",
                    doc_id,
                    j,
                    c.get("author_email"),
                    body,
                    c_ts,
                    _j(c.get("reactions")),
                ),
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
            # A reply is a full message (reactions/files/subtype/edited carry through);
            # its time is the root's + its position so the thread stays ordered (created is now
            # always set, so a reply ts is never NULL).
            rep_cts = created + i
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
                cts=_epoch(msg.get("created")) or (created + i * 3600),
            )

    def write_containers(self) -> None:
        """The per-service grouping rows (``slack_channels``, ``linear_teams``, ``gdrive_folders``,
        …). Deferred to the end of a load rather than written per record: a container's owning
        group is whatever its records agreed on, and the last one wins."""
        for (src, name), group_id in self.containers.items():
            gtable, gcol = store.GROUPING[src]
            self.conn.execute(
                f"INSERT OR REPLACE INTO {gtable}({gcol}, group_id) VALUES (?,?)", (name, group_id)
            )

    def resolve_cross_references(self) -> None:
        """Resolve the links whose target may only have arrived on a later record."""
        conn, counts, seen = self.conn, self.counts, self.seen
        hs_types, hs_links, lin_links = self.hs_types, self.hs_links, self.lin_links
        # HubSpot associations: one declaration becomes two rows, because real HubSpot exposes a link
        # from both records (with a distinct type id per direction) and a corpus author should not have
        # to write it twice.
        for from_doc, from_type, a in hs_links:
            to_doc = a["to"]
            to_type = a.get("to_type") or hs_types.get(to_doc)
            if to_type is None:
                # `--append` loads one file at a time, so a target already in the DB is not in
                # `hs_types`. Fall back to the stored row before giving up, or appending a contact to a
                # previously-loaded company would fail for a link that is perfectly resolvable.
                row = conn.execute(
                    "SELECT object_type FROM hubspot_objects WHERE doc_id = ?", (to_doc,)
                ).fetchone()
                to_type = row["object_type"] if row else None
            if to_type is None:
                raise SystemExit(
                    f"hubspot association {from_doc} -> {to_doc}: target not found in this corpus or "
                    f"the existing DB; add the target record or set 'to_type' on the association"
                )
            category = a.get("category") or "HUBSPOT_DEFINED"
            label = a.get("label")
            # An explicit type_id applies only to the direction the author declared; the reverse gets
            # its own synthesized id, since the two directions never share one in real HubSpot.
            for f_doc, f_type, t_doc, t_type, tid in (
                (from_doc, from_type, to_doc, to_type, a.get("type_id")),
                (to_doc, to_type, from_doc, from_type, None),
            ):
                conn.execute(
                    "INSERT OR REPLACE INTO hubspot_associations(from_doc_id, from_type, to_doc_id, "
                    "to_type, assoc_category, assoc_type_id, label) VALUES (?,?,?,?,?,?,?)",
                    (
                        f_doc,
                        f_type,
                        t_doc,
                        t_type,
                        category,
                        tid or synth.hubspot_assoc_type_id(f_type, t_type),
                        label,
                    ),
                )

        # Linear parents: `parent` names the target by IDENTIFIER, so it can only be resolved once
        # every issue is loaded. `Issue.children` reads `parent_doc_id`, so without this a corpus would
        # serve `parent` but an empty `children`, and the two directions would disagree.
        if counts.get("linear"):
            key_to_doc: dict[str, str] = {}
            for did, ident in conn.execute(
                "SELECT doc_id, identifier FROM linear_issues WHERE identifier IS NOT NULL "
                "ORDER BY doc_id"
            ):
                key_to_doc.setdefault(ident, did)
            dangling = 0
            for did, pkey in conn.execute(
                "SELECT doc_id, parent_key FROM linear_issues WHERE parent_key IS NOT NULL"
            ).fetchall():
                target = key_to_doc.get(pkey)
                if target is None:
                    # A parent names an IDENTIFIER, not a doc_id, and an identifier that is not in this
                    # corpus is a normal property of a real dataset rather than a corpus error: a slice
                    # of an issue tracker references issues outside it (24.8% of the bench's parent
                    # references do). So keep `parent_key` — it is what the corpus said — and leave
                    # `parent_doc_id` NULL, which is exactly what `Issue.parent` serving null means.
                    # `relations` stay strict by contrast: those name a doc_id, so a miss is a typo.
                    dangling += 1
                    continue
                if target != did:
                    conn.execute(
                        "UPDATE linear_issues SET parent_doc_id = ? WHERE doc_id = ?", (target, did)
                    )
            if dangling:
                print(
                    f"  linear: {dangling} parent reference(s) match no issue in this corpus; "
                    f"kept as `parent` with no resolved parent issue",
                    file=sys.stderr,
                )

        # Linear relations: resolve declared targets now that every doc_id is known. A target that
        # does not exist is an error rather than a dangling relation, matching the hubspot rule.
        for from_doc, a, created_ts in lin_links:
            to_doc = a["to"]
            if ("linear", to_doc) not in seen and not conn.execute(
                "SELECT 1 FROM linear_issues WHERE doc_id = ?", (to_doc,)
            ).fetchone():
                raise SystemExit(
                    f"linear relation {from_doc} -> {to_doc}: target not found in this corpus or the "
                    f"existing DB; add the target issue or drop the relation"
                )
            conn.execute(
                "INSERT OR REPLACE INTO linear_relations(id, from_doc_id, to_doc_id, type, created_ts)"
                " VALUES (?,?,?,?,?)",
                (
                    a.get("id") or f"{from_doc}::r{to_doc}",
                    from_doc,
                    to_doc,
                    a.get("type") or "related",
                    created_ts,
                ),
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
    """Load already-parsed BYO records into the DB. ``load`` is this over a JSONL file.

    ``records_factory`` returns a FRESH iterator of ``(where, record)`` pairs and may be called
    twice — the org has to be inferred from every author's address before the first grant is
    written, so a corpus is re-read rather than held in memory. ``where`` names the record in an
    error message. ``validate=False`` skips the JSON Schema check, and is only for records this repo
    generated itself (``erb.to_byo``); a corpus from OUTSIDE always validates, which is why
    ``load`` does not expose the flag.
    """
    settings = settings or get_settings()
    if reset and settings.db_path.exists():
        settings.db_path.unlink()
    conn = store.connect_rw(settings.db_path)

    # infer the org from the corpus (dominant email domain) before building any grants,
    # since public docs are granted to the org principal — see _infer_org. Read in its own pass,
    # keeping only the emails: a sharded corpus is streamed rather than held in memory.
    def _scanned():
        """Just what `_emails` reads, one record at a time.

        `infer_org` consumes the addresses exactly once (backlot/config.py), so nothing needs to be held:
        yielding keeps the memory constant where a list would build one dict per document plus one
        per child row — millions of them at bench scale, in a pass whose whole point is to stream.
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
    # caller makes over a corpus it generates on the fly (see erb.import_structured).
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
    source_docs = 0
    for lineno, rec in records_factory():
        source_docs += 1
        loader.add(rec, f"line {lineno}")
    loader.resolve_cross_references()
    users, groups = loader.users, loader.groups
    memberships, grants = loader.memberships, loader.grants
    counts, fts_ids = loader.counts, loader.fts_ids

    if closed:
        # The roster IS the principal set: users, the groups they belong to, and the memberships
        # between them. Nothing the records referenced adds to it.
        users = {e: u["name"] for e, u in roster_data["users"].items()}
        groups = {u["group"] for u in roster_data["users"].values() if u["group"]}
        memberships = {(u["group"], e) for e, u in roster_data["users"].items() if u["group"]}

    # principals: org, groups, users
    conn.execute("INSERT OR REPLACE INTO principals VALUES (?,?,?,?)", (org, "org", org, None))
    for g in groups:
        conn.execute("INSERT OR REPLACE INTO principals VALUES (?,?,?,?)", (g, "group", g, None))
    for email, name in users.items():
        conn.execute(
            "INSERT OR REPLACE INTO principals VALUES (?,?,?,?)", (email, "user", name, email)
        )
    loader.write_containers()
    for g, email in memberships:
        conn.execute("INSERT OR REPLACE INTO group_members VALUES (?,?)", (g, email))
    for doc_id, ptype, pid in grants:
        conn.execute("INSERT OR REPLACE INTO doc_acl VALUES (?,?,?)", (doc_id, ptype, pid))
    conn.commit()
    if reset:
        store.build_fts(conn)  # full-text index for search (search.messages / confluence CQL)
    else:
        for s, ids in fts_ids.items():
            store.fts_add_docs(conn, s, ids)

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


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Import (load) a BYO JSONL corpus into the mock DB.")
    ap.add_argument(
        "corpus",
        help="a JSONL corpus file, a .jsonl.gz, or a directory holding "
        "manifest.json + data/<source>/part-*.jsonl.gz shards",
    )
    ap.add_argument(
        "--append", action="store_true", help="add to the existing DB instead of resetting"
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="validate the corpus only; don't touch the DB"
    )
    ap.add_argument(
        "--roster",
        type=Path,
        default=None,
        help="roster YAML naming the corpus's principals (see load_roster); with it, "
        "principals/groups/tokens come from the file instead of from the records",
    )
    args = ap.parse_args(argv)
    corpus = Path(args.corpus)

    if args.dry_run:
        # A sharded artifact is checked against its manifest first: a truncated download is a
        # different failure from a bad record, and saying so is cheaper than a schema error.
        _verify_or_die(corpus)
        problems, n = [], 0
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
            problems.extend((lineno, m) for m in record_errors(rec))
        if not problems:
            print(f"OK: {n} records valid.")
            return 0
        print(f"INVALID: {len(problems)} problem(s) in {corpus}", file=sys.stderr)
        for lineno, msg in problems:
            print(f"  line {lineno}: {msg}", file=sys.stderr)
        return 1

    # The same check `--dry-run` makes, on the path that actually writes a database. A shard that is
    # short but validly terminated — what a resumed or re-uploaded download looks like — otherwise
    # loads as a quietly incomplete corpus and reports success. Measured at 0.67 s of sha256 over the
    # 1.0 GB bench artifact, in front of an import that writes 14 GB.
    _verify_or_die(corpus)

    settings = get_settings()
    # A sharded artifact ships its own roster beside the manifest, so importing one takes the
    # directory and nothing else; `--roster` still wins when it is given.
    roster = args.roster
    if roster is None and corpus.is_dir() and (corpus / "roster.yaml").exists():
        roster = corpus / "roster.yaml"
        print(f"using the artifact's own roster: {roster}", file=sys.stderr)
    res = load(corpus, settings, reset=not args.append, roster=roster)
    print(f"Loaded {res['total']} documents into {settings.db_path}")
    for src, n in sorted(res["counts"].items()):
        print(f"  {src:14s} {n}")
    print(f"Principals: {res['users']} users, {res['groups']} groups")
    print(f"Tokens written to {settings.tokens_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
