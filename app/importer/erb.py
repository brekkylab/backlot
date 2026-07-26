"""Import EnterpriseRAG-Bench (ERB) into the mock DB — the faithful, structured pipeline.

Downloads the bench's structured ``generated_data/`` (real owners/authors/dates/participants/ACL
signals), resolves display names to real emails via the ``Principals`` roster below, loads the six
supported sources into their per-service tables via ``load_structured``, derives per-doc ACL grants
from the real people/scope fields (``grants_for``), and writes ``tokens.yaml`` for the resolved
roster. This is the single ERB importer script — everything it needs (source fetch/parse,
principal resolution, conversation parsing, ACL derivation, and orchestration) lives here.

    python -m app.importer.erb                                   # full corpus: download -> load -> ACL
    python -m app.importer.erb --slice-questions extra_questions.jsonl   # only the docs a slice needs
    python -m app.importer.erb --no-download                     # reuse whatever is already in data/raw
    python -m app.importer.erb --ref some-branch                 # fetch a non-default branch/ref

Only ``curl`` is used to fetch (no ``gh`` / no auth).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import unicodedata
from collections.abc import Iterator
from email.utils import parsedate_to_datetime
from pathlib import Path

import yaml

from app import store, synth
from app.config import get_settings, infer_org

# ---------------------------------------------------------------- constants
SUPPORTED = ("slack", "gmail", "google_drive", "github", "jira", "confluence", "hubspot")

INTERNAL_ROLES = {"owner", "author", "reviewer", "assignee", "reporter",
                  "collaborator", "participant_internal", "mailbox_owner"}
EXTERNAL_ROLES = {"participant_external"}
SLACK_ROLE = "slack_participant"
EXTERNAL_DOMAIN = "external.example"  # placeholder when no counterparty domain is known
_NAME_EMAIL = re.compile(r"([^<>\n,:]+?)\s*<([^>@\s]+@[^>\s]+)>")

_HDR = re.compile(r"^(From|To|Cc|Bcc|Reply-To|Date|Subject|Message-ID):\s*(.*)$")
# US timezone abbreviations the bench uses in some gmail Date headers -> fixed UTC offset (hours).
# DST-labeled variants carry their own offset; bare PT/ET/CT/MT default to standard time.
_TZ = {"UTC": 0, "GMT": 0, "Z": 0, "EST": -5, "EDT": -4, "ET": -5, "CST": -6, "CDT": -5,
       "CT": -6, "MST": -7, "MDT": -6, "MT": -7, "PST": -8, "PDT": -7, "PT": -8}
_ADDR = re.compile(r"<([^>@\s]+@[^>\s]+)>")
_JIRA = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<name>[^:]+?):\s*(?P<body>.*)$", re.DOTALL)
# Slack speaker: 1–3 name-ish words / handles ("Alex", "ops-bot", "Maria L", "IT Help"), an
# optional "(Team)"/"(Role)" label some docs append ("Elena (CFO)", "Asha (FinanceOps)"), then
# ": ". The parenthetical is dropped so only the bare name resolves against the directory.
_SPEAKER = re.compile(
    r"^@?(?P<name>[A-Za-z][\w.'\-]*(?: [A-Za-z0-9][\w.'\-]*){0,2})(?: *\([^)]*\))?: (?P<text>\S.*)$")


# ---------------------------------------------------------------- small helpers
def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def snake(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def canonical(name: str) -> str:
    """Separator/punctuation-agnostic identity key, dropping single-letter tokens (middle
    initials) so variants collapse: 'Connor O'Brien'/'Connor OBrien' -> 'connorobrien',
    'Aisha K. Patel'/'Aisha Patel' -> 'aishapatel'. ('Asha Patel' stays 'ashapatel', distinct.)
    Apostrophes are joined first so a name particle like O'Brien is one token (not a dropped 'o').
    Accents are ASCII-folded (Tomáš -> tomas) so accented and plain spellings collapse together."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()  # á->a, š->s
    s = re.sub(r"['’]", "", s.lower())  # o'brien -> obrien (don't split the O off)
    return "".join(t for t in re.split(r"[^a-z0-9]+", s) if len(t) > 1)


# A name token: starts with a letter (incl. accents), then letters/apostrophe/hyphen/dot only.
_NAME_TOKEN = re.compile(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’.\-]*$")
# Words that mark a value as a team/placeholder/prose fragment, not a person.
_NON_PERSON_WORDS = {"team", "teams", "group", "groups", "all", "everyone", "folks", "redacted",
                     "unknown", "na", "tbd", "via", "support", "bot", "customer", "vendor",
                     "channel", "oncall", "rotation", "admin", "system", "service"}


def _person_like(name: str) -> bool:
    """A name worth minting as a real org user: a genuine 'First Last' (2–4 name tokens).
    Rejects transcript junk, aliases/emails in a name field, team/placeholder names
    ('Customer Success Team'), and parenthetical/prose fragments ('(Aisha Bello, SRE) - Sign-off…'),
    while accepting middle initials ('Aisha K. Patel') and accented/hyphenated names ('Tomás Rré')."""
    if not name or len(name) > 40:
        return False
    if any(ch in name for ch in "@()[]{},:;/\n\t0123456789"):
        return False
    toks = name.split()
    if not (2 <= len(toks) <= 4):
        return False
    if any(t.lower().strip(".") in _NON_PERSON_WORDS for t in toks):
        return False
    return all(_NAME_TOKEN.match(t) for t in toks)


def _parse_named_email(s: str) -> tuple[str, str | None]:
    """'Alyssa Chen <alyssa.chen@x.com>' -> ('Alyssa Chen', 'alyssa.chen@x.com');
    a bare name -> (name, None). Used to dedup external participants by their real email."""
    m = _NAME_EMAIL.search(s or "")
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    return (s or "").strip(), None


def _user_token(email: str) -> str:
    return "usr-" + hashlib.sha256(("tok:" + email).encode()).hexdigest()[:20]


def _slug(name: str) -> str:
    parts = [re.sub(r"[^a-z0-9]+", "", p) for p in (name or "").lower().split()]
    parts = [p for p in parts if p]
    return ".".join(parts) or "user"


def _is_bot(name: str) -> bool:
    n = (name or "").lower()
    return n.endswith("bot") or n.endswith("-bot") or "bot" in n.split()


def _addr(header: str | None) -> str | None:
    if not header:
        return None
    m = _ADDR.search(header)
    return m.group(1).lower() if m else None


def _name(header: str | None) -> str:
    if not header:
        return ""
    return re.sub(r"\s*<[^>]*>", "", header).strip().strip('"')


# ---------------------------------------------------------------- principals
class Principals:
    """Resolve document principal references (display names) to the mock's email-keyed identities.

    The bench names people by display string ("Maya Chen"), inconsistently across sources
    ("Connor O'Brien" vs "Connor OBrien"), and only Gmail headers reveal real emails. This builds
    one canonical identity per person: match the employee directory, harvest real emails from
    Gmail, synthesize a user for unmatched internal references, and keep external participants as
    non-org contacts. Slack first-names/bots are best-effort (documented limitation).
    """

    def __init__(self, employees: list[dict], org_domain: str):
        self.org_domain = org_domain
        self.users: dict[str, dict] = {}      # email -> {name, group, external, is_bot}
        self.groups: set[str] = set()
        self._by_canon: dict[str, str] = {}   # canonical name -> email
        for e in employees:
            self._by_canon[canonical(e["name"])] = e["email"]
            self.users[e["email"]] = {"name": e["name"], "group": e.get("dept_slug"),
                                      "external": False, "is_bot": False, "directory": True}
            if e.get("dept_slug"):
                self.groups.add(e["dept_slug"])

        # team-label -> directory-department reconciliation (doc team labels don't always
        # match the directory's dept_slug verbatim, e.g. "security" vs "security-compliance")
        dept_slugs = [e["dept_slug"] for e in employees if e.get("dept_slug")]
        self._dept_slugs: set[str] = set(dept_slugs)
        token_to_depts: dict[str, set[str]] = {}
        for d in self._dept_slugs:
            for tok in d.split("-"):
                token_to_depts.setdefault(tok, set()).add(d)
        # only unambiguous tokens (appear in exactly one dept_slug) are usable for lookup
        self._token_to_dept: dict[str, str] = {
            tok: next(iter(ds)) for tok, ds in token_to_depts.items() if len(ds) == 1
        }

    @classmethod
    def from_directory(cls, employee_yaml, org_domain: str) -> "Principals":
        data = yaml.safe_load(open(employee_yaml).read())
        emps = []
        for dept, people in (data.get("departments") or {}).items():
            for p in (people or []):
                emps.append({"name": p["name"], "email": p["email"],
                             "dept_slug": slugify(dept)})
        return cls(emps, org_domain)

    def harvest_gmail_emails(self, records) -> None:
        """Record real Name<email> pairs from gmail message headers (real emails win)."""
        for src, _dsid, raw in records:
            if src != "gmail":
                continue
            for msg in raw.get("messages", []) or []:
                for m in _NAME_EMAIL.finditer(str(msg)):
                    name, email = m.group(1).strip(), m.group(2).strip().lower()
                    c = canonical(name)
                    # One canonical identity → one email → one user. If this person's canonical
                    # key is already claimed (by the directory or an earlier header, possibly with
                    # a different dot/underscore email), don't mint a competing duplicate user.
                    # Gate on _person_like so header aliases ('On-Call (SRE) <oncall@…>') don't leak.
                    if (c and _person_like(name) and email.endswith("@" + self.org_domain)
                            and c not in self._by_canon):
                        self._by_canon[c] = email
                        self.users[email] = {"name": name, "group": None,
                                             "external": False, "is_bot": False}

    def canonical_group(self, label: str | None) -> str | None:
        """Reconcile a doc's raw team/owner_team/squad label to the directory's dept_slug group.

        Doc team labels don't always match the directory verbatim (e.g. "security" vs
        "security-compliance"); without this, the ACL group ends up with 0 members.
        """
        if isinstance(label, (list, tuple)):  # some docs carry a multi-valued team field
            label = next((x for x in label if x), None)
        if not label:
            return None
        s = re.sub(r"[^a-z0-9]+", "-", str(label).lower()).strip("-")
        if not s:
            return None
        if s in self._dept_slugs:
            return s
        # prefix either direction: "security" <-> "security-compliance"
        matches = [d for d in self._dept_slugs if d.startswith(s + "-") or s.startswith(d + "-")]
        if len(matches) == 1:
            return matches[0]
        first = s.split("-")[0]
        if first in self._token_to_dept:
            return self._token_to_dept[first]
        return s  # genuine sub-team not in the directory -> its own group

    def resolve(self, name: str, *, role: str, group_hint: str | None = None) -> str | None:
        """Resolve a reference to an address. Only reliable full-name INTERNAL references become
        real org users (registered in self.users → principals/tokens). External participants
        return their parsed email (address only, never registered). Slack speakers return a
        display-label address (never registered — first-names aren't real identities)."""
        name = (name or "").strip()
        if not name:
            return None

        if role in EXTERNAL_ROLES:  # 'Name <email>' → real email, deduped by email; not a principal
            _disp, email = _parse_named_email(name)
            return email or f"{_slug(name)}@{EXTERNAL_DOMAIN}"

        if role == SLACK_ROLE:  # first-name/bot → display label only; Slack docs are org-visible
            return f"{_slug(name)}@{self.org_domain}"

        c = canonical(name)
        if c in self._by_canon:
            email = self._by_canon[c]
            u = self.users.setdefault(email, {"name": name, "group": None,
                                              "external": False, "is_bot": False})
            if group_hint and role in ("owner", "author") and not u["group"]:
                u["group"] = group_hint
                self.groups.add(group_hint)
            return email

        if not c or not _person_like(name):  # transcript/junk single tokens don't become users
            return None

        email = f"{_slug(name)}@{self.org_domain}"
        group = group_hint if (group_hint and role in ("owner", "author")) else None
        self._by_canon.setdefault(c, email)
        # setdefault: if this slug email already exists (e.g. it collides with a directory
        # employee whose accented/titled name didn't canonical-match), keep that entry — never
        # clobber a directory=True user with a synthesized one.
        self.users.setdefault(email, {"name": name, "group": group,
                                      "external": False, "is_bot": False})
        if group:
            self.groups.add(group)
        return self._by_canon[c]

    def display_email(self, name: str) -> tuple[str | None, str]:
        c = canonical(name or "")
        return self._by_canon.get(c), (name or "")

    def install(self, conn, settings) -> None:
        conn.execute("INSERT OR REPLACE INTO principals(id,type,display_name,email) VALUES (?,?,?,?)",
                     (settings.org_name, "org", settings.org_name, None))
        for g in sorted(self.groups):
            conn.execute("INSERT OR REPLACE INTO principals(id,type,display_name,email) VALUES (?,?,?,?)",
                         (g, "group", g, None))
        for email, u in self.users.items():
            ptype = "external" if u["external"] else "user"
            conn.execute("INSERT OR REPLACE INTO principals(id,type,display_name,email) VALUES (?,?,?,?)",
                         (email, ptype, u["name"], email))
            if not u["external"] and u["group"]:
                conn.execute("INSERT OR REPLACE INTO group_members(group_id,user_id) VALUES (?,?)",
                             (u["group"], email))

    def write_tokens(self, settings) -> None:
        # Only the employee directory are authenticating org users (realistic roster). Everyone
        # else the corpus references is display-only: they still appear as owners/authors/grantees
        # on documents (name derived from their email), but get no bearer token / /_mock/users entry.
        users = [{"email": e, "name": u["name"], "token": _user_token(e)}
                 for e, u in self.users.items() if u.get("directory")]
        settings.tokens_path.write_text(yaml.safe_dump(
            {"org": settings.org_name, "org_domain": settings.org_domain,
             "admin_token": settings.admin_token, "users": users}, sort_keys=False))


# ---------------------------------------------------------------- ACL derivation
def grants_for(source: str, meta: dict) -> list[tuple[str, str]]:
    """Derive a document's ACL grants from its real people + scope signals — no random assignment.

    Grant read to everyone named on the doc (owner/author/collaborators/reviewers/assignee/
    reporter/participants), plus a scope grant from the source's visibility model: Confluence
    confidentiality, Gmail thread-privacy, or the container's group. Admin/service token still
    bypasses at query time.
    """
    org = meta.get("org")
    group = meta.get("group")
    grants: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(t: str, pid: str | None):
        if pid and (t, pid) not in seen:
            seen.add((t, pid))
            grants.append((t, pid))

    # per-user grants (owner + named people); external addresses can't authenticate → skip as ACL
    people = [meta.get("owner"), *meta.get("people", [])]
    for e in people:
        if e and not e.endswith("@external.example") and "@external." not in e:
            add("user", e)

    if source == "gmail":
        pass  # private to participants — no org/group scope
    elif source == "slack":
        add("org", org)  # channel privacy isn't recoverable from first-names → org-visible
    elif source == "confluence":
        conf = (meta.get("confidentiality") or "internal").lower()
        if conf in ("public", "internal"):
            add("org", org)
        else:  # restricted / confidential
            add("group", group)
    else:  # github / jira / google_drive → container group
        add("group", group)

    if not grants:  # never leave a doc ungranted
        add("group", group) or add("org", org)
    return grants


# ---------------------------------------------------------------- source fetch + parse
def _unescape(s: str) -> str:
    """Some source docs double-escape newlines/tabs (a literal ``\\n`` instead of a real newline).
    Left as-is, header/transcript parsing collapses to one line and bodies come out empty."""
    if "\\n" in s or "\\t" in s:
        return s.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    return s


def _stringify(v) -> str:
    """A content field is either a string or a list (gmail/jira/slack conversation)."""
    if isinstance(v, list):
        return "\n\n".join(_unescape(str(x)) for x in v)
    return "" if v is None else _unescape(str(v))


def derive_title_content(raw: dict) -> tuple[str, str]:
    title = str(raw.get(raw.get("title_field_name", "title"), "")).strip()
    parts = [_stringify(raw.get(f)) for f in raw.get("content_field_names", ["content"])]
    return title, "\n\n".join(p for p in parts if p).strip()


def iter_records(sources_dir: Path, sources: tuple[str, ...] = SUPPORTED
                 ) -> Iterator[tuple[str, str, dict]]:
    for src in sources:
        base = sources_dir / src
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.json")):
            try:
                raw = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            dsid = raw.get("dataset_doc_uuid")
            if dsid:
                yield src, dsid, raw


def fetch_generated_data(settings, *, ref: str = "main") -> Path:
    """Download + extract generated_data (sources for SUPPORTED + employee_directory.yaml).
    Returns the extracted ``generated_data`` directory. Cached under settings.raw_dir."""
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    out = settings.raw_dir / "generated_data"
    if (out / "employee_directory.yaml").exists():
        return out
    repo = settings.dataset_repo
    url = f"https://codeload.github.com/{repo}/tar.gz/refs/heads/{ref}"
    tar_path = settings.raw_dir / f"erb-{ref}.tar.gz"
    if not tar_path.exists():
        print(f"downloading {url}", file=sys.stderr)
        subprocess.run(["curl", "-fsSL", url, "-o", str(tar_path)], check=True)
    keep_sources = {f"sources/{s}" for s in SUPPORTED}
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path) as tf:
        for m in tf.getmembers():
            # member path: <repo>-<ref>/generated_data/<rest>
            parts = m.name.split("/", 2)
            if len(parts) < 3 or parts[1] != "generated_data":
                continue
            rest = parts[2]  # e.g. "sources/gmail/x.json" or "employee_directory.yaml"
            keep = rest == "employee_directory.yaml" or any(
                rest == p or rest.startswith(p + "/") for p in keep_sources)
            if not keep:
                continue
            dest = out / rest
            if m.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(m) as fsrc:
                    dest.write_bytes(fsrc.read())
    return out


def parse_gmail_thread(messages: list[str]) -> list[dict]:
    """Gmail ``messages`` is a list of RFC822-ish strings (real From/To/Cc/Date + body)."""
    out = []
    for msg in messages or []:
        lines = _unescape(str(msg)).split("\n")  # some docs use literal \n instead of newlines
        hdrs: dict[str, str] = {}
        body_start = len(lines)
        for i, line in enumerate(lines):
            m = _HDR.match(line)
            if m:
                hdrs.setdefault(m.group(1), m.group(2).strip())
            elif line.strip() == "" and hdrs:
                body_start = i + 1
                break
            elif hdrs:
                body_start = i
                break
        out.append({
            "from_name": _name(hdrs.get("From")), "from_email": _addr(hdrs.get("From")),
            "to": hdrs.get("To"), "cc": hdrs.get("Cc"), "date": hdrs.get("Date"),
            "subject": hdrs.get("Subject"), "message_id": hdrs.get("Message-ID"),
            "body": "\n".join(lines[body_start:]).strip(),
        })
    return out


# Filename-extension -> MIME, so the Gmail API's attachment parts carry a realistic type.
_ATT_MIME = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "csv": "text/csv", "txt": "text/plain", "png": "image/png", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "zip": "application/zip", "json": "application/json",
}


def _gmail_attachments(raw: dict) -> list[dict]:
    """Normalize a gmail doc's thread-level ``attachments`` into the {filename, mime, size}
    shape the Gmail router serves (payload parts + download endpoint). The bench lists them as
    bare filename strings; some docs may already use dicts — pass those through, filling gaps."""
    out = []
    for a in raw.get("attachments") or []:
        if isinstance(a, dict):
            name = a.get("filename") or a.get("name") or ""
            entry = {"filename": name, "mime": a.get("mime") or a.get("mimeType"),
                     "size": a.get("size")}
        else:
            name, entry = str(a), {"filename": str(a), "mime": None, "size": None}
        if not name:
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        entry["mime"] = entry["mime"] or _ATT_MIME.get(ext, "application/octet-stream")
        entry["size"] = entry["size"] or 1024
        out.append(entry)
    return out


def parse_jira_comments(comments: list[str]) -> list[dict]:
    """Jira ``comments`` is a list of ``YYYY-MM-DD Name: text``."""
    out = []
    for c in comments or []:
        m = _JIRA.match(str(c).strip())
        if m:
            out.append({"date": m.group("date"), "name": m.group("name").strip(),
                        "body": m.group("body").strip()})
    return out


def _canon_speaker(s: str) -> str:
    """Canonicalize a speaker/participant name for matching: drop a trailing team label and any
    non-alphanumerics. 'ben.jones (Acme)' / 'Ben Jones' -> 'benjones'; 'api-monitor-bot' ->
    'apimonitorbot'."""
    s = re.sub(r"\s*\([^)]*\)", "", str(s))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_slack_transcript(messages: str, participants: list | None = None) -> list[tuple[str, str]]:
    """Slack ``messages`` is ONE concatenated ``Speaker: text`` transcript. When ``participants`` is
    given, a line only starts a NEW turn if its speaker matches a known participant; otherwise it's
    body text of the current turn. This stops sentence fragments / section headers ("A couple
    followups:", "What I did:") from being mis-parsed as speakers and minting fake authors."""
    # canon -> the participant's clean display name (team label stripped); used both to gate turns
    # and to normalize the speaker to the participant's canonical identity, so transcript variants
    # ("a lex", "Ana Customs") collapse onto the real participant ("alex", "ana_customs") instead of
    # minting variant-duplicate authors.
    pmap: dict[str, str] = {}
    for p in (participants or []):
        pmap.setdefault(_canon_speaker(p), re.sub(r"\s*\([^)]*\)", "", str(p)).strip())
    pset = set(pmap)
    msgs: list[list] = []
    in_fence = False
    cur: list | None = None
    for line in _unescape(str(messages)).split("\n"):
        m = None if in_fence else _SPEAKER.match(line)
        # a real turn only when the name is a known participant (or we have no participant list to
        # gate on, or nothing to append to yet — the root line)
        if m and (not pset or cur is None or _canon_speaker(m.group("name")) in pset):
            name = pmap.get(_canon_speaker(m.group("name")), m.group("name"))
            cur = [name, [m.group("text")]]
            msgs.append(cur)
        elif cur is not None:
            cur[1].append(line)  # continuation (incl. a non-participant "phrase: text" line)
        if line.count("```") % 2 == 1:
            in_fence = not in_fence
    return [(spk, "\n".join(ls).rstrip()) for spk, ls in msgs]


def to_epoch(value) -> int | None:
    """Parse a bench date/time to unix seconds; None if unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    # ISO 8601, incl. a trailing Z and +/-HH:MM offsets — the bench's gmail Date headers use
    # "2026-05-18T09:02:00-07:00" and "...Z"; a naive value is treated as UTC.
    try:
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int((dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)).timestamp())
    except ValueError:
        pass
    # RFC 2822 email Date header ("Mon, 18 May 2026 09:02:00 -0700"). Tolerate a malformed
    # "-07:00" colon offset (seen in the bench) by normalizing it to "-0700" first. Without this,
    # ~96% of gmail messages failed to parse -> NULL created_ts -> a synthesized (fake) served date.
    try:
        dt = parsedate_to_datetime(re.sub(r"([+-]\d{2}):(\d{2})\b", r"\1\2", s))
        if dt is not None:
            return int((dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)).timestamp())
    except (ValueError, TypeError):
        pass
    # Human/mixed formats the parsers above reject: a trailing timezone as either a numeric offset
    # ("...at 9:12 AM -07:00" / "-0700") OR a 2-4 letter abbreviation ("2026-08-30 09:12 PDT",
    # "... 09:12 PM PT", "Wed, May 14, 2025 at 9:12 AM PT"). Split off the tz, then parse the rest.
    off = _dt.timedelta(0)
    mnum = re.search(r"\s([+-]\d{2}):?(\d{2})$", s)
    mabbr = re.search(r"\s([A-Z]{2,4})$", s)
    if mnum:
        sign = 1 if mnum.group(1)[0] == "+" else -1
        off = _dt.timedelta(minutes=sign * (abs(int(mnum.group(1))) * 60 + int(mnum.group(2))))
        core = s[:mnum.start()]
    elif mabbr and mabbr.group(1) in _TZ:
        off = _dt.timedelta(hours=_TZ[mabbr.group(1)])
        core = s[:mabbr.start()]
    else:
        core = s
    core = re.sub(r"^[A-Za-z]{3},\s*", "", core.strip()).replace(" at ", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p", "%Y-%m-%d",
                "%b %d, %Y %I:%M %p", "%b %d, %Y %H:%M", "%b %d, %Y"):
        try:
            base = _dt.datetime.strptime(core, fmt)
            return int(base.replace(tzinfo=_dt.timezone(off)).timestamp())
        except ValueError:
            pass
    return None


def _names(v):
    """Normalize a principals field that may be a list or a single string."""
    if v is None:
        return []
    return [x for x in (v if isinstance(v, list) else [v]) if x]


def _title_content(raw):
    return derive_title_content(raw)


def _slug_mailbox(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


# ---------------------------------------------------------------- loaders
def load_drive(conn, dsid, raw, P):
    title, content = _title_content(raw)
    group = P.canonical_group(raw.get("team"))
    owner = raw.get("owner", "")
    owner_email = P.resolve(owner, role="owner", group_hint=group) if owner else None
    collabs = [P.resolve(n, role="collaborator") for n in _names(raw.get("collaborators"))]
    if group:
        conn.execute("INSERT OR REPLACE INTO gdrive_folders(folder, group_id) VALUES (?,?)",
                     (raw.get("drive_area") or group, group))
    conn.execute(
        "INSERT OR REPLACE INTO gdrive_files(doc_id, folder, author_email, title, content, "
        "subtype, created_ts, updated_ts, collaborators, owner_display) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (dsid, raw.get("drive_area") or group or "drive", owner_email or f"unknown@{P.org_domain}",
         title, content, raw.get("doc_type"), (to_epoch(raw.get("created_at")) or synth.epoch(dsid)),
         to_epoch(raw.get("last_modified")), json.dumps(collabs),
         owner))
    return {"owner": owner_email, "people": collabs, "group": group, "confidentiality": None}


def load_github(conn, dsid, raw, P):
    title, content = _title_content(raw)
    author = raw.get("author", "")
    author_email = P.resolve(author, role="author", group_hint=raw.get("repo")) if author else None
    reviewers = [P.resolve(n, role="reviewer") for n in _names(raw.get("reviewers"))]
    repo = raw.get("repo") or "repo"
    conn.execute("INSERT OR REPLACE INTO github_repos(repo, group_id) VALUES (?,?)", (repo, repo))
    conn.execute(
        "INSERT OR REPLACE INTO github_items(doc_id, repo, author_email, title, content, kind, "
        "state, labels, created_ts, updated_ts, requested_reviewers, owner_display) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (dsid, repo, author_email or f"unknown@{P.org_domain}", title, content,
         "pull_request" if raw.get("pr_number") else "issue", raw.get("state"),
         json.dumps(_names(raw.get("labels"))), (to_epoch(raw.get("created_at")) or synth.epoch(dsid)),
         to_epoch(raw.get("updated_at")), json.dumps(reviewers), author))
    return {"owner": author_email, "people": reviewers, "group": repo, "confidentiality": None}


def load_confluence(conn, dsid, raw, P):
    title, content = _title_content(raw)
    space = raw.get("space") or "SPACE"
    group = P.canonical_group(raw.get("owner_team")) or space
    author = raw.get("author", "")
    author_email = P.resolve(author, role="author", group_hint=group) if author else None
    reviewers = [P.resolve(n, role="reviewer") for n in _names(raw.get("reviewers"))]
    conn.execute("INSERT OR REPLACE INTO confluence_spaces(space, group_id) VALUES (?,?)", (space, group))
    conn.execute(
        "INSERT OR REPLACE INTO confluence_pages(doc_id, space, author_email, title, content, "
        "subtype, labels, created_ts, updated_ts, reviewers, confidentiality, owner_team, owner_display) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (dsid, space, author_email or f"unknown@{P.org_domain}", title, content, "page",
         json.dumps(_names(raw.get("labels"))), (to_epoch(raw.get("created_at")) or synth.epoch(dsid)),
         to_epoch(raw.get("last_updated")), json.dumps(reviewers), raw.get("confidentiality"),
         raw.get("owner_team"), author))
    return {"owner": author_email, "people": reviewers, "group": group,
            "confidentiality": raw.get("confidentiality")}


def load_jira(conn, dsid, raw, P):
    title, content = _title_content(raw)
    reporter = raw.get("reporter", "")
    assignee = raw.get("assignee", "")
    group = P.canonical_group(raw.get("squad")) or (raw.get("project") or "JIRA")
    reporter_email = P.resolve(reporter, role="reporter", group_hint=group) if reporter else None
    assignee_email = P.resolve(assignee, role="assignee", group_hint=group) if assignee else None
    project = raw.get("project") or "JIRA"
    conn.execute("INSERT OR REPLACE INTO jira_projects(project, group_id) VALUES (?,?)", (project, group))
    conn.execute(
        "INSERT OR REPLACE INTO jira_issues(doc_id, project, author_email, title, content, status, "
        "issuetype, priority, labels, components, created_ts, updated_ts, assignee_email, "
        "reporter_email, severity, squad, duedate, owner_display) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (dsid, project, reporter_email or f"unknown@{P.org_domain}", title, content,
         raw.get("status"), raw.get("issue_type"), raw.get("priority"),
         json.dumps(_names(raw.get("labels"))), json.dumps(_names(raw.get("components"))),
         (to_epoch(raw.get("created_at")) or synth.epoch(dsid)), to_epoch(raw.get("updated_at")), assignee_email,
         reporter_email, raw.get("severity"), raw.get("squad"), raw.get("due_date"), reporter))
    # comments
    for seq, c in enumerate(parse_jira_comments(raw.get("comments", [])), start=1):
        conn.execute("INSERT OR REPLACE INTO jira_comments(id, doc_id, seq, author_email, body, created_ts)"
                     " VALUES (?,?,?,?,?,?)",
                     (f"{dsid}::c{seq}", dsid, seq, P.resolve(c["name"], role="author"),
                      c["body"], to_epoch(c["date"])))
    people = [assignee_email, reporter_email]
    return {"owner": reporter_email, "people": [p for p in people if p], "group": group,
            "confidentiality": None}


# The bench's HubSpot docs are denormalized company (account) records — there are no contact/deal
# objects in the corpus. These are the fields with a real HubSpot company property to map onto;
# everything else becomes a custom property, which is what an actual portal looks like (a mix of
# HubSpot defaults and portal-specific fields). We map the bench onto the mock's API-shaped schema
# rather than storing ERB's shape, exactly as load_drive/load_github do for their sources.
_HS_PROPERTY = {
    "company_name": "name",
    "company_domain": "domain",
    "industry": "industry",
    "stage": "lifecyclestage",
}
# Excluded from `properties`: ERB's own envelope keys (see derive_title_content), the two dates that
# become columns, the owner (which becomes author_email + owner_display), and the notes that become
# their own rows. `se_assigned` / `csm_assigned` are deliberately NOT excluded — they feed the ACL
# bundle *and* stay properties, since a real portal exposes the SE and CSM as fields on the record.
_HS_NOT_A_PROPERTY = {"title_field_name", "content_field_names", "dataset_doc_uuid",
                      "created_at", "updated_at", "owner", "notes", "crm_notes"}


def _hs_notes(raw) -> list[str]:
    """The bench's CRM notes: usually a list of undated fragments, sometimes a single string.
    (`timeline` is a *dated activity log* the bench lists in content_field_names — it is the
    company's own body text, not a set of note objects, so it is deliberately not included.)"""
    for key in ("notes", "crm_notes"):
        v = raw.get(key)
        if isinstance(v, list):
            out = [str(n) for n in v if str(n).strip()]
            if out:              # an empty list must not mask a populated `crm_notes`
                return out
        elif isinstance(v, str) and v.strip():
            return [v]
    return []


def _hs_insert(conn, doc_id, object_type, author_email, title, content, properties,
               created_ts, updated_ts=None, owner_display=None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO hubspot_objects(doc_id, object_type, author_email, title, content, "
        "properties, archived, created_ts, updated_ts, owner_display) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (doc_id, object_type, author_email, title, content, json.dumps(properties), None,
         created_ts, updated_ts, owner_display))


def _hs_associate(conn, a_doc, a_type, b_doc, b_type, label=None) -> None:
    """Link two records in both directions — real HubSpot exposes an association from either end,
    with a distinct type id per direction."""
    for f_doc, f_type, t_doc, t_type in ((a_doc, a_type, b_doc, b_type),
                                         (b_doc, b_type, a_doc, a_type)):
        conn.execute(
            "INSERT OR REPLACE INTO hubspot_associations(from_doc_id, from_type, to_doc_id, "
            "to_type, assoc_category, assoc_type_id, label) VALUES (?,?,?,?,?,?,?)",
            (f_doc, f_type, t_doc, t_type, "HUBSPOT_DEFINED",
             synth.hubspot_assoc_type_id(f_type, t_type), label))


def load_hubspot(conn, dsid, raw, P):
    title, content = _title_content(raw)
    object_type = "companies"
    group = object_type
    owner = raw.get("owner", "")
    owner_email = P.resolve(owner, role="owner", group_hint=group) if owner else None
    # The AE owns the account; the SE and CSM are the other real people on it, so they enter the
    # ACL bundle the way reviewers/collaborators do elsewhere.
    people = [P.resolve(n, role="collaborator")
              for n in (raw.get("se_assigned"), raw.get("csm_assigned")) if n]
    for otype in (object_type, "notes"):
        conn.execute("INSERT OR REPLACE INTO hubspot_object_types(object_type, group_id) "
                     "VALUES (?,?)", (otype, group))

    # The linked_* arrays stay here as free-text stubs: the dataset documents them as
    # "references to linked artifacts as stubs/links", so they name no resolvable doc_id and must
    # never become associations pointing at documents that do not exist.
    props = {_HS_PROPERTY.get(k, k): v for k, v in raw.items() if k not in _HS_NOT_A_PROPERTY}
    created = to_epoch(raw.get("created_at")) or synth.epoch(dsid)
    author = owner_email or f"unknown@{P.org_domain}"
    _hs_insert(conn, dsid, object_type, author, title, content, props, created,
               to_epoch(raw.get("updated_at")), owner)

    # A HubSpot note is its own object associated with the company, and this repo already parses
    # embedded conversations into first-class rows on import (Slack transcripts -> threads, Jira
    # comments -> comment rows). Notes carry no date in the bench, so their time is the company's
    # clock plus position: deterministic, ordered, and never NULL.
    children: list[str] = []
    for i, body in enumerate(_hs_notes(raw), start=1):
        note_id = f"{dsid}::n{i}"
        children.append(note_id)
        # A note has no title in HubSpot; its body lives in hs_note_body, mirrored into `content`
        # so full-text search and any RAG consumer see it.
        _hs_insert(conn, note_id, "notes", author, "", body,
                   {"hs_note_body": body, "hs_timestamp": synth.rfc3339(created + i)}, created + i)
        _hs_associate(conn, dsid, object_type, note_id, "notes")

    return {"owner": owner_email, "people": [p for p in people if p], "group": group,
            "confidentiality": None, "_children": children}


def load_gmail(conn, dsid, raw, P):
    title, content = _title_content(raw)
    # 'messages' is a list of RFC822 emails (a thread); some docs instead carry a single email
    # in 'body'/'content' (content_field_names points there). Parse the list when present, else
    # fall back to the derived single-email content so those docs aren't left empty.
    raw_msgs = raw.get("messages")
    msgs = parse_gmail_thread(raw_msgs) if isinstance(raw_msgs, list) and raw_msgs else []
    owner_name = raw.get("mailbox_owner", "")
    mailbox = _slug_mailbox(owner_name) or "inbox"
    owner_email = P.resolve(owner_name, role="mailbox_owner") if owner_name else None
    internal = [P.resolve(n, role="participant_internal") for n in _names(raw.get("participants_internal"))]
    # external participants stay as recipient addresses on the thread's To/Cc headers (parsed
    # above); they are not org principals, so they never enter the ACL `people` set.
    conn.execute("INSERT OR REPLACE INTO gmail_mailboxes(mailbox, group_id) VALUES (?,?)",
                 (mailbox, None))
    root = msgs[0] if msgs else {}
    # The bench carries a thread-level `attachments` list (filenames); the Gmail router already
    # renders these as payload parts + a download endpoint + `has:attachment` search once the
    # column is populated. Attach them to the thread root (thread_seq 0).
    attachments = _gmail_attachments(raw)
    # Root time: its own Date, then the doc-level first_email_at, then a deterministic synthesized
    # base (synth.epoch — the same value the server already synthesizes for a NULL, so no served
    # date changes) so created_ts is never NULL. Two SEPARATE to_epoch calls, not
    # `to_epoch(A or B)`, which would pass an unparseable-but-truthy A and never reach B.
    root_ts = to_epoch(root.get("date")) or to_epoch(raw.get("first_email_at")) or synth.epoch(dsid)
    conn.execute(
        "INSERT OR REPLACE INTO gmail_messages(doc_id, mailbox, author_email, title, content, "
        "thread_id, thread_seq, to_addr, cc, message_id, attachments, created_ts, owner_display) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        # Prefer the doc-level thread subject (title_field_name -> `subject`, the bench's canonical
        # thread subject) over the first message's RFC822 Subject header, which is often a "Re: ..."
        # reply subject and loses the thread's distinctive title (e.g. "[P0] Acme Health — ...").
        (dsid, mailbox, root.get("from_email") or owner_email or f"unknown@{P.org_domain}",
         title or root.get("subject") or "", root.get("body") or (content if not msgs else ""),
         dsid, 0, root.get("to"), root.get("cc"), root.get("message_id"),
         json.dumps(attachments) if attachments else None, root_ts, owner_name))
    for seq, m in enumerate(msgs[1:], start=1):
        # a reply's own Date when present, else the root's clock + an hour per position (matches the
        # server's historical hour-spread) so a date-less reply is thread-coherent and never NULL.
        conn.execute(
            "INSERT OR REPLACE INTO gmail_messages(doc_id, mailbox, author_email, title, content, "
            "thread_id, thread_seq, to_addr, cc, message_id, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"{dsid}::m{seq}", mailbox, m.get("from_email") or f"unknown@{P.org_domain}",
             m.get("subject") or title, m.get("body", ""), dsid, seq, m.get("to"), m.get("cc"),
             m.get("message_id"), to_epoch(m.get("date")) or (root_ts + seq * 3600)))
    people = [owner_email, *internal]
    return {"owner": owner_email, "people": [p for p in people if p], "group": None,
            "confidentiality": None, "_extra_rows": len(msgs) - 1 if msgs else 0,
            "_children": [f"{dsid}::m{seq}" for seq in range(1, len(msgs))]}


# Slack source `first_message_ts`: ~35% are the bench's opaque far-future "ordering keys" (valid
# 10-digit ts up to year 2286, plus one corrupt 12-digit record at year 8632) — NOT real calendar
# dates. Served verbatim they render as absurd dates AND blow up mirage's per-day FS layout (a
# channel's 90-day window lands in the far future). Remap ONLY the out-of-range roots (year > 2035),
# order-preserving, into a compact window that continues the real timeline just after the newest
# in-range thread; in-range ts stay untouched so the realistic majority keeps its cross-source
# temporal coherence (a Slack thread and the Jira ticket it cites stay aligned). Slack-only: every
# other source already sits in 2022-2035.
_SLACK_TS_CUTOFF = int(_dt.datetime(2035, 1, 1, tzinfo=_dt.timezone.utc).timestamp())
_SLACK_TS_REMAP_SPAN = 8 * 365 * 86400
_SLACK_TS_REMAP: dict[str, int] = {}


def build_slack_ts_remap(records) -> dict[str, int]:
    """dsid -> remapped root ts for slack threads whose source ts is beyond _SLACK_TS_CUTOFF.
    Rank-based (order-preserving, robust to outliers like the lone year-8632 record): the future
    roots are spread evenly across [newest_in_range, +SPAN], so their relative order is kept while
    the absolute values become plausible near-future dates."""
    in_range_max = _SLACK_TS_CUTOFF
    future: list[tuple[int, str]] = []
    for src, dsid, raw in records:
        if src != "slack":
            continue
        ts = to_epoch(raw.get("first_message_ts"))
        if ts is None:
            continue
        if ts > _SLACK_TS_CUTOFF:
            future.append((ts, dsid))
        elif ts > in_range_max:
            in_range_max = ts
    future.sort()
    n = len(future)
    start = in_range_max + 60  # seamless continuation, just after the newest real thread
    return {dsid: start + (rank * _SLACK_TS_REMAP_SPAN // max(1, n - 1))
            for rank, (ts, dsid) in enumerate(future)}


def load_slack(conn, dsid, raw, P):
    channel = raw.get("channel") or "general"
    # The transcript lives in whatever field content_field_names points at — 'messages' for
    # threaded docs, 'text' for single-post docs (whose title_field_name is 'file_name'!). Use
    # the derived content, never raw['messages'] (which is null for the 'text' variant) and never
    # the derived title (which can be a filename). This is the fix for docs that were rendering
    # as "*<channel/filename>*" with empty bodies.
    _title, content = _title_content(raw)
    participants = _names(raw.get("participants"))
    # gate speaker-splitting on the declared participants so message-body lines like
    # "A couple followups: ..." aren't mis-parsed as new speakers (fake authors).
    turns = parse_slack_transcript(content, participants)
    conn.execute("INSERT OR REPLACE INTO slack_channels(channel, group_id) VALUES (?,?)",
                 (channel, channel))
    root_author = P.resolve(turns[0][0], role="slack_participant") if turns else None
    root_content = turns[0][1] if turns else content  # keep the raw text if it isn't a transcript
    # Real first_message_ts (a unix-epoch string) when present, else a deterministic synthesized
    # base (synth.epoch — the same value the server already synthesizes for a NULL, so no served
    # date changes) so the column is never NULL. The bench leaves ~0.1% of slack docs date-less.
    root_ts = _SLACK_TS_REMAP.get(dsid) or to_epoch(raw.get("first_message_ts")) or synth.epoch(dsid)
    conn.execute(
        "INSERT OR REPLACE INTO slack_messages(doc_id, channel, author_email, title, content, "
        "thread_id, thread_seq, created_ts, participants) VALUES (?,?,?,?,?,?,?,?,?)",
        (dsid, channel, root_author or f"unknown@{P.org_domain}", "", root_content,
         dsid, 0, root_ts, json.dumps(participants)))
    for seq, (spk, text) in enumerate(turns[1:], start=1):
        # Each reply sits on the SAME clock as its root (root_ts + seq), so a thread is temporally
        # coherent and never NULL.
        conn.execute(
            "INSERT OR REPLACE INTO slack_messages(doc_id, channel, author_email, title, content, "
            "thread_id, thread_seq, created_ts) VALUES (?,?,?,?,?,?,?,?)",
            (f"{dsid}::m{seq}", channel, P.resolve(spk, role="slack_participant") or f"unknown@{P.org_domain}",
             "", text, dsid, seq, root_ts + seq))
    # Slack speakers are display labels, not org identities; the doc is org-visible (see
    # grants_for), so no per-user ACL grants — `people` stays empty.
    return {"owner": root_author, "people": [], "group": channel, "confidentiality": None,
            "_children": [f"{dsid}::m{seq}" for seq in range(1, len(turns))]}


_LOADERS = {"google_drive": load_drive, "github": load_github, "confluence": load_confluence,
            "jira": load_jira, "gmail": load_gmail, "slack": load_slack,
            "hubspot": load_hubspot}


def load_structured(conn, records, P, settings) -> dict:
    """Insert every record's doc row(s); return {dsid: people_bundle} for the ACL step + counts.

    Resilient per-doc: a single malformed record (e.g. an unexpected field shape) is logged and
    skipped rather than aborting the whole import. Commits in batches so a crash can't roll back
    the entire corpus and the write transaction/journal stays bounded."""
    bundles = {}
    counts = {s: 0 for s in SUPPORTED}
    failures: list[tuple[str, str, str]] = []
    # Precompute the slack future-date remap (needs a global view of all slack roots) before the
    # per-doc loop; load_slack reads it via the _SLACK_TS_REMAP module global.
    _SLACK_TS_REMAP.clear()
    _SLACK_TS_REMAP.update(build_slack_ts_remap(records))
    if _SLACK_TS_REMAP:
        print(f"  slack: remapped {len(_SLACK_TS_REMAP)} future-dated threads into a realistic "
              f"window (order-preserving)", file=sys.stderr, flush=True)
    total = len(records)
    for i, (src, dsid, raw) in enumerate(records, 1):
        try:
            bundle = _LOADERS[src](conn, dsid, raw, P)
            bundle["_source"] = src
            bundles[dsid] = bundle
            counts[src] += 1
        except Exception as e:  # one bad doc must not sink the import
            failures.append((dsid, src, repr(e)))
        if i % 5000 == 0:
            conn.commit()
            print(f"  loaded {i}/{total} ({len(failures)} skipped)", file=sys.stderr, flush=True)
    conn.commit()
    if failures:
        print(f"  WARNING: skipped {len(failures)} docs. First few: {failures[:5]}",
              file=sys.stderr, flush=True)
    return {"bundles": bundles, "counts": counts, "failures": failures}


def parse_employees(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text())
    employees: list[dict] = []
    for dept, people in data.get("departments", {}).items():
        for p in people or []:
            employees.append({
                "name": p["name"], "email": p["email"], "title": p.get("title", ""),
                "department": dept, "dept_slug": slugify(dept), "mailbox": snake(p["name"]),
            })
    return employees


# ---------------------------------------------------------------- orchestration
def select_records(gen_dir: Path, question_ids: set[str] | None = None):
    """Yield ``(source_type, dsid, raw_json)`` records under ``gen_dir/sources``.

    If ``question_ids`` is None, every record is yielded. Otherwise only records whose ``dsid``
    is in ``question_ids`` are yielded. This is a deliberate simplification of the plan's fuller
    interface (which also pulls in every other record sharing a selected doc's container/thread,
    so containers aren't left empty) — that container-expansion is NOT needed for validation, so
    it is skipped here.
    """
    for src, dsid, raw in iter_records(gen_dir / "sources"):
        if question_ids is None or dsid in question_ids:
            yield src, dsid, raw


class _NullConn:
    """A no-op DB connection: lets ``load_structured`` drive the loaders (whose ``P.resolve``
    calls build the roster) without paying for any inserts — used by the fast tokens-only path."""
    def execute(self, *a, **k):
        return None

    def commit(self, *a, **k):
        return None


def _resolve_roster(settings, gen_dir, *, question_ids=None):
    """Shared prefix: build Principals, materialize records, harvest emails. Returns (P, records)."""
    emails = [e["email"] for e in parse_employees(settings.employee_yaml)]
    settings.org_name, settings.org_domain = infer_org(emails, settings)
    P = Principals.from_directory(settings.employee_yaml, settings.org_domain)
    records = []
    for rec in select_records(gen_dir, question_ids):
        records.append(rec)
        if len(records) % 25000 == 0:
            print(f"  materialized {len(records)} records...", file=sys.stderr, flush=True)
    print(f"  materialized {len(records)} records; loading...", file=sys.stderr, flush=True)
    # (Gmail-header email harvesting was dropped: it scanned every message body — minutes of CPU —
    # for marginal value under the directory-only roster. Message senders come straight from the
    # parsed From: headers, and principals still dedupe by canonical name.)
    return P, records


def dump_tokens(settings, gen_dir, *, question_ids=None) -> int:
    """Resolve principals over the corpus and write ``tokens.yaml`` WITHOUT building the DB — a
    fast roster preview (skips the row inserts + FTS build). Returns the tokened-user count.
    Uses the real loaders via a no-op connection, so the roster matches a full import exactly."""
    P, records = _resolve_roster(settings, gen_dir, question_ids=question_ids)
    load_structured(_NullConn(), records, P, settings)  # resolve-only; inserts are no-ops
    P.write_tokens(settings)
    return sum(1 for u in P.users.values() if not u["external"] and not u["is_bot"])


def import_structured(settings, gen_dir, *, question_ids=None) -> dict:
    P, records = _resolve_roster(settings, gen_dir, question_ids=question_ids)

    if settings.db_path.exists():
        settings.db_path.unlink()
    conn = store.connect_rw(settings.db_path)
    result = load_structured(conn, records, P, settings)
    # Install principals AFTER load: the loaders synthesize users via P.resolve() during load, so
    # installing earlier would omit every synthesized user (and their group membership) from the
    # principals/group_members tables while they still get tokens — breaking group-scoped ACL.
    P.install(conn, settings)
    for dsid, bundle in result["bundles"].items():
        # A loader may materialize child rows from one bench doc — a Slack transcript's turns, a
        # Gmail thread's messages, a HubSpot company's notes. Those rows are reached through the
        # same per-row ACL filter as any other doc (store._acl_clause matches on each row's
        # doc_id), so they must carry the parent's grants: without them a non-admin caller sees a
        # silently truncated thread or an empty note list, while admin sees everything.
        docs = [dsid, *bundle.get("_children", [])]
        for ptype, pid in grants_for(bundle["_source"], {**bundle, "org": settings.org_name}):
            for doc in docs:
                conn.execute("INSERT OR REPLACE INTO doc_acl(doc_id, principal_type, principal_id)"
                             " VALUES (?,?,?)", (doc, ptype, pid))
    conn.commit()
    P.write_tokens(settings)
    store.build_fts(conn)
    conn.close()
    from app import oauth
    oauth.generate(settings)
    return result["counts"]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Import EnterpriseRAG-Bench (faithful, structured) into the mock DB.")
    ap.add_argument("--slice-questions", type=Path, default=None,
                    help="only import docs referenced (expected_doc_ids) by this questions JSONL")
    ap.add_argument("--ref", default="main",
                    help="EnterpriseRAG-Bench branch/ref to fetch (default: main)")
    ap.add_argument("--no-download", action="store_true",
                    help="reuse cached data/raw/generated_data; skip fetching")
    ap.add_argument("--tokens-only", action="store_true",
                    help="resolve the roster and write tokens.yaml WITHOUT building the DB (fast)")
    args = ap.parse_args(argv)
    settings = get_settings()

    if args.no_download:
        gen_dir = settings.raw_dir / "generated_data"
    else:
        gen_dir = fetch_generated_data(settings, ref=args.ref)

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(gen_dir / "employee_directory.yaml", settings.employee_yaml)

    question_ids = None
    if args.slice_questions:
        question_ids = set()
        for line in args.slice_questions.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            question_ids.update(json.loads(line).get("expected_doc_ids", []))

    if args.tokens_only:
        n = dump_tokens(settings, gen_dir, question_ids=question_ids)
        print(f"Wrote {n} users to {settings.tokens_path} (roster only; no DB built)")
        print(f"Org: {settings.org_name} ({settings.org_domain})")
        return 0

    counts = import_structured(settings, gen_dir, question_ids=question_ids)
    print(f"Loaded {sum(counts.values())} documents into {settings.db_path}")
    for src, n in counts.items():
        print(f"  {src:14s} {n}")
    print(f"Org: {settings.org_name} ({settings.org_domain}) · tokens -> {settings.tokens_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
