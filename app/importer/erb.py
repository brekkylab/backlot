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
import gzip
import hashlib
import io
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
SUPPORTED = ("slack", "gmail", "google_drive", "github", "jira", "confluence", "hubspot",
             "linear", "fireflies")

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

    def write_roster(self, path, settings) -> None:
        """Write the resolved roster as a BYO roster sidecar (see ``byo.load_roster``).

        The directory has to ship WITH a converted corpus, because neither the records nor the
        addresses in them can reconstruct it. Two things would be lost otherwise:

        * display names — ``_slug`` is lossy ("Tomás Rré" -> ``tomas.rre``, "Aisha K. Patel" ->
          ``aisha.k.patel``), so a name cannot be recovered from an email
        * who may authenticate — only the employee directory gets a token (see ``write_tokens``),
          while everyone else the corpus names owns and reads documents without being an account.
          Derived from the corpus alone, every Slack display handle and outside sender would become
          an org account with a working bearer token.

        So ``departments`` carries the authenticating users keyed by their group, and ``contacts``
        everyone else — the same split ``write_tokens`` and ``install`` already make.
        """
        depts: dict[str, list] = {}
        contacts: list[dict] = []
        for email, u in sorted(self.users.items()):
            entry = {"name": u["name"], "email": email}
            if u.get("directory"):
                depts.setdefault(u["group"] or "", []).append(entry)
            elif u["group"]:
                contacts.append({**entry, "group": u["group"]})
            else:
                contacts.append(entry)
        Path(path).write_text(yaml.safe_dump(
            {"org": settings.org_name, "org_domain": settings.org_domain,
             "departments": depts, "contacts": contacts},
            sort_keys=False, allow_unicode=True))


# ---------------------------------------------------------------- ACL derivation
# Sources whose visibility model is the people on the document and nothing wider. A document here
# with no identifiable people is readable by NOBODY (admin still bypasses), and must not fall back
# to an org grant: that would publish a private thread to the entire company. Measured on the bench,
# 3 of ~121k Gmail threads resolve no participant at all — and the org grant was their ONLY grant.
_PARTICIPANTS_ONLY = {"gmail"}


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
    elif source == "fireflies":
        # A meeting recorder is workspace-wide, and the same arithmetic that makes HubSpot
        # org-visible applies: the bench names 1,104 distinct meeting hosts of whom only the ~167
        # in the employee directory can authenticate, so an owner-or-channel scope would leave
        # ~91% of the 10,173 transcripts readable by admin and almost nobody else. Org-visible,
        # on top of the real per-user grants added above for everyone who does resolve.
        add("org", org)
    elif source == "hubspot":
        # A CRM is team-wide, and the object type's group is not a useful scope here: the bench
        # names ~3.3k account owners of whom only the ~167 in the employee directory can
        # authenticate, so both an owner-only and a group scope leave the corpus readable by admin
        # and almost nobody else. Org-visible, like slack.
        add("org", org)
    elif source == "confluence":
        conf = (meta.get("confidentiality") or "internal").lower()
        if conf in ("public", "internal"):
            add("org", org)
        else:  # restricted / confidential
            add("group", group)
    else:  # github / jira / google_drive → container group
        add("group", group)

    if not grants and source not in _PARTICIPANTS_ONLY:
        # Reaching here means the source's scope grant above was a NO-OP — its container has no
        # group (a Drive file with no `team`). Fall back to the org rather than leave the document
        # invisible to every non-admin caller.
        #
        # Written as if/else, not `add("group", group) or add("org", org)`: `add` returns None, so
        # the `or` always evaluated its right-hand side and granted BOTH. That reads as a fallback
        # and behaved as a conjunction.
        if group:
            add("group", group)
        else:
            add("org", org)
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
                # The record's own path within its source, e.g. "all-hands/2025-01-14-x.json".
                # Fireflies needs it: the bench's subdirectories ARE the workspaces its
                # `agents.md` describes, and they become the transcript's channel — the only
                # source whose container lives in the layout rather than in a field. Prefixed
                # with `_` and excluded from HubSpot's property passthrough, so it can never be
                # mistaken for corpus data.
                raw["_erb_path"] = path.relative_to(base).as_posix()
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


# The bench's Drive ``doc_type`` vocabulary -> the mock's Drive subtype vocabulary (the keys
# ``app.routers.google._NATIVE`` recognises as Workspace types). The bench says "doc"/"sheet"/
# "slides", none of which are native keys, so every imported row used to fall back to
# ``application/octet-stream`` — and to the binary ``webViewLink`` shape — leaving nothing in the
# corpus that exercises native-vs-binary handling, ``export`` vs ``alt=media``, or per-type links.
_DRIVE_SUBTYPE = {
    "doc": "document", "document": "document", "gdoc": "document", "notes": "document",
    "memo": "document",
    "sheet": "spreadsheet", "spreadsheet": "spreadsheet", "gsheet": "spreadsheet",
    "slides": "presentation", "slide": "presentation", "deck": "presentation",
    "presentation": "presentation", "gslides": "presentation",
    "folder": "folder",
}


def _ext(name: str | None) -> str | None:
    m = re.search(r"\.([A-Za-z0-9]{1,5})$", (name or "").strip())
    return m.group(1).lower() if m else None


def _drive_type(raw: dict, title: str) -> tuple[str, str | None]:
    """``(subtype, mime_type)`` for a bench Drive row. A recognised ``doc_type`` maps onto a native
    Workspace subtype (the router derives the mimeType from it); anything else is a binary, whose
    type comes from the ``doc_type`` itself when it names a file kind ("pdf") and otherwise from the
    title's or path's extension. A row with no usable type signal is a Doc — the bench's Drive
    corpus is prose, and that beats calling it an opaque blob."""
    key = (raw.get("doc_type") or "").strip().lower()
    if key in _DRIVE_SUBTYPE:
        return _DRIVE_SUBTYPE[key], None
    ext = key if key in _ATT_MIME else (_ext(title) or _ext(raw.get("path") or raw.get("file_path")))
    if ext in _ATT_MIME:
        return ext, _ATT_MIME[ext]
    return "document", None


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


def _resolved(P, values, *, role: str) -> list[str]:
    """Resolve a list of name references, DROPPING the ones that resolve to nobody.

    ``P.resolve`` returns None for a reference that is not a usable identity (a team label, a
    prose fragment). Such a name must not hold a slot in a list of principals: the list is stored
    as the document's collaborators / reviewers, and a null in it is not a person with an unknown
    address — it is not a person. Kept, it also breaks serving outright: `requested_reviewers`
    is rendered per entry into a GitHub Simple User, so a null 500s the pull-request endpoint
    (8 documents in the bench do this). `grants_for` already ignores falsy people, so ACL is
    unchanged either way."""
    return [e for e in (P.resolve(n, role=role) for n in _names(values)) if e]


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
    collabs = _resolved(P, raw.get("collaborators"), role="collaborator")
    # The folder the file claims, and the folder row, are the SAME expression — a doc with no team
    # used to get a folder no `gdrive_folders` row described (group_id is nullable; the row is not
    # optional), so `files.get` on its parent 404'd for a folder holding real files.
    folder = raw.get("drive_area") or group or "drive"
    conn.execute("INSERT OR REPLACE INTO gdrive_folders(folder, group_id) VALUES (?,?)",
                 (folder, group))
    subtype, mime_type = _drive_type(raw, title)
    conn.execute(
        "INSERT OR REPLACE INTO gdrive_files(doc_id, folder, author_email, title, content, "
        "subtype, mime_type, created_ts, updated_ts, collaborators, owner_display) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (dsid, folder, owner_email or f"unknown@{P.org_domain}",
         title, content, subtype, mime_type, (to_epoch(raw.get("created_at")) or synth.epoch(dsid)),
         to_epoch(raw.get("last_modified")), json.dumps(collabs),
         owner))
    return {"owner": owner_email, "people": collabs, "group": group, "confidentiality": None}


def load_github(conn, dsid, raw, P):
    title, content = _title_content(raw)
    author = raw.get("author", "")
    author_email = P.resolve(author, role="author", group_hint=raw.get("repo")) if author else None
    reviewers = _resolved(P, raw.get("reviewers"), role="reviewer")
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
    reviewers = _resolved(P, raw.get("reviewers"), role="reviewer")
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
                      "created_at", "updated_at", "owner", "notes", "crm_notes",
                      "_erb_path"}  # injected by iter_records, not corpus data


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
        # sort_keys: the stored JSON is CANONICAL, so the same record always yields the same text —
        # otherwise the column depends on the source file's key order, and two importers (or two
        # re-imports of a rewritten file) disagree byte for byte over identical data.
        (doc_id, object_type, author_email, title, content, json.dumps(properties, sort_keys=True),
         None, created_ts, updated_ts, owner_display))


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
    internal = _resolved(P, raw.get("participants_internal"), role="participant_internal")
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
         json.dumps(attachments, sort_keys=True) if attachments else None, root_ts, owner_name))
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
    # A thread_id ONLY when the transcript actually has more than one turn. Real Slack puts
    # `thread_ts` on a message that is part of a thread and leaves it off a standalone post, and the
    # router reads exactly this column to decide (`if not hit["thread_id"]`) — so setting it
    # unconditionally advertised a thread on every single-message doc, and `conversations.replies`
    # answered for a thread with no replies.
    thread_id = dsid if len(turns) > 1 else None
    conn.execute(
        "INSERT OR REPLACE INTO slack_messages(doc_id, channel, author_email, title, content, "
        "thread_id, thread_seq, created_ts, participants) VALUES (?,?,?,?,?,?,?,?,?)",
        (dsid, channel, root_author or f"unknown@{P.org_domain}", "", root_content,
         thread_id, 0, root_ts, json.dumps(participants)))
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


# ---------------------------------------------------------------- linear
# The bench's Linear docs are one ticket per file, with a standard ERB envelope
# (`title_field_name`/`content_field_names`/`dataset_doc_uuid`) plus the metadata its
# `sources/linear/agents.md` documents: key, team, status, priority, created_at, updated_at,
# creator, assignee, and optional project/cycle/estimate/due_date/labels.
#
# Two properties of the real data drive the mapping:
#   * `key` is NOT unique — 22,729 distinct keys across 35,308 docs, one repeated 107 times — so
#     the doc_id stays the dataset uuid and the key becomes `identifier`, which Linear does not
#     require to be globally unique in *our* corpus even though its own product does.
#   * the directory a file sits in disagrees with its own `team` field for ~2,750 docs (and two
#     directories, business-ops/misc-chores, name no team at all). The `team` FIELD is the
#     authority: its three values line up with the ENG/PM/DES identifier prefixes and each maps
#     onto a real directory department, so the ACL group actually has members.

# The bench writes P0-P3; Linear's API has a 0-4 integer scale with 1 the most urgent. Map onto
# the API's scale (as load_hubspot maps onto real HubSpot property names) rather than serving a
# vocabulary no Linear client understands. Labels are accepted too, for a BYO corpus that already
# speaks Linear.
_LINEAR_PRIORITY = {"p0": 1, "p1": 2, "p2": 3, "p3": 4,
                    "urgent": 1, "high": 2, "medium": 3, "low": 4,
                    "none": 0, "no priority": 0}


def linear_priority(value) -> int | None:
    """A bench priority -> Linear's 0-4. Unrecognized text becomes 0 ("No priority"), which is
    what Linear itself stores for an unset priority; a missing value stays None."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if 0 <= int(value) <= 4 else 0
    s = str(value).strip().lower()
    if s.isdigit() and 0 <= int(s) <= 4:
        return int(s)
    return _LINEAR_PRIORITY.get(s, 0)


def _linear_int(value) -> int | None:
    """An estimate: the bench writes it as a numeric string, occasionally as an int, twice as
    null. Anything non-numeric is dropped rather than coerced to 0 — a wrong estimate is worse
    than an absent one."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    try:
        return int(float(s))
    except ValueError:
        return None


def _linear_release(value) -> str | None:
    """8 docs write `release` as a list; Linear attaches an issue to one release name."""
    if isinstance(value, (list, tuple)):
        value = next((x for x in value if x), None)
    s = str(value or "").strip()
    return s or None


def _linear_parent(value) -> str | None:
    """The bench's ``parent_issue`` is a list of keys on 16,813 docs and a bare string on 552.
    Linear has exactly one parent, so take the first."""
    if isinstance(value, (list, tuple)):
        value = next((x for x in value if x), None)
    s = str(value or "").strip()
    return s or None


# The bench writes a dependency as a bare issue key, sometimes with a relation word attached
# ("blocks ENG-123"). Linear's IssueRelation.type vocabulary is blocks | duplicate | related.
_LINEAR_REL_WORDS = (("duplicate", "duplicate"), ("blocked by", "blocks"), ("blocks", "blocks"),
                     ("depends", "blocks"), ("related", "related"))
_LINEAR_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")


def parse_linear_relations(value) -> list[tuple[str, str]]:
    """A bench `dependencies` entry -> ``[(type, key), …]``.

    Defaults to ``related``, which is Linear's own neutral relation, rather than guessing
    ``blocks``: the corpus lists these under a heading that means "depends on" only sometimes, and
    asserting a blocking relationship the data does not state would be inventing a dependency
    graph. A word that IS present is honoured."""
    out = []
    for entry in (value if isinstance(value, (list, tuple)) else [value]):
        text = str(entry or "")
        rel = next((t for word, t in _LINEAR_REL_WORDS if word in text.lower()), "related")
        for key in _LINEAR_KEY.findall(text):
            out.append((rel, key))
    return out


def _linear_url_title(url: str) -> tuple[str, str | None]:
    """`Attachment.title` is non-null in Linear. The bench's `links` are `Label: URL`, its
    `attachments` are bare URLs — so a label is used when present and otherwise derived from the
    last meaningful path segment, never left empty."""
    text = str(url or "").strip()
    m = re.match(r"^(?P<label>[^:]{1,60}):\s*(?P<url>https?://\S+)$", text)
    if m:
        return m.group("url"), m.group("label").strip()
    parts = [p for p in text.rstrip("/").split("/") if p]
    derived = parts[-1] if parts else text
    return text, (derived or None)


def parse_linear_attachments(*values) -> list[dict]:
    """The bench's `links` and `attachments` are both external links, which is exactly Linear's
    `Attachment`. Merged and de-duplicated on url, since a doc can list the same link in both."""
    out, seen = [], set()
    for value in values:
        for entry in (value if isinstance(value, (list, tuple)) else [value]):
            if not entry:
                continue
            url, title = _linear_url_title(entry)
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({"url": url, "title": title or url})
    return out


def _linear_date(value) -> str | None:
    """A `TimelessDate` (`YYYY-MM-DD`) for dueDate — served verbatim, since that is the scalar's
    whole shape. Anything else is dropped."""
    s = str(value or "").strip()
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None


# A Linear comment in the bench is a plain string. Measured across all 165,243 of them, the date
# and the author are carried in a handful of interchangeable shapes:
#     2025-02-18 - Maya Patel: filed the PRD      (dash + name — 60,282, the single most common)
#     2025-02-18 - Created: initial hypothesis    (dash + a LABEL, not a person)
#     2026-03-05 Anjali Rao: updated the criteria (no dash, name)
#     2025-12-18 (Naomi Feldman): include audit   (parenthesised name)
#     2025-02-18 09:15: rolled back               (no name at all — that is a clock)
#     Implementation notes: use heuristics        (undated)
# So the parse is two independent steps rather than a list of whole-line alternatives: peel off
# the date (with an optional dash separator), then try to peel a `Name:` off what remains. Trying
# whole-line patterns in order is what an earlier version did, and because the dash pattern had no
# name group and was tried first, it swallowed the author of 60,282 comments into the body —
# serving `body` as "Maya Patel: filed the PRD" and attributing it to nobody.
_LINEAR_C_DATE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\s*(?:[-–—]\s*)?(?P<rest>.*)$", re.DOTALL)
# The name must START WITH A LETTER. Without that, "2025-02-18 09:15: rolled back" parses as
# author "09" with the body truncated to "15: rolled back" — inventing a person and losing text.
_LINEAR_C_NAME = re.compile(r"^\(?(?P<name>[A-Za-zÀ-ÿ][^:\n()]{0,39}?)\)?:\s*(?P<body>.*)$",
                            re.DOTALL)


def parse_linear_comments(comments) -> list[dict]:
    """Bench comment strings -> ``{date, name, body, body_with_name}``.

    ``body`` has the ``Name:`` prefix removed; ``body_with_name`` keeps it. Both are returned
    because only the caller knows whether the name is a person: the prefix is just as often a
    LABEL ("Created:", "Design review with PM and Accessibility:"), and stripping one of those
    would delete text from the comment. :func:`load_linear` uses ``body`` when the name resolves
    to somebody real and ``body_with_name`` when it doesn't, so nothing is ever lost.
    """
    if isinstance(comments, str):          # 29 docs carry a single string instead of a list
        comments = [comments]
    out = []
    for c in comments or []:
        s = str(c).strip()
        if not s:
            continue
        m = _LINEAR_C_DATE.match(s)
        date, rest = (m.group("date"), m.group("rest")) if m else (None, s)
        n = _LINEAR_C_NAME.match(rest)
        if n:
            out.append({"date": date, "name": n.group("name").strip(),
                        "body": n.group("body").strip(), "body_with_name": rest.strip()})
        else:
            out.append({"date": date, "name": None, "body": rest.strip(),
                        "body_with_name": rest.strip()})
    return out


def load_linear(conn, dsid, raw, P):
    title, content = _title_content(raw)
    team = str(raw.get("team") or "engineering")
    group = P.canonical_group(team) or team
    creator = raw.get("creator", "")
    assignee = raw.get("assignee", "")
    creator_email = P.resolve(creator, role="author", group_hint=group) if creator else None
    # "unassigned" is a real value in the bench (11 docs); it is not a person, so it must not
    # become one — leave the assignee null, which is what Linear stores for an unassigned issue.
    assignee_name = assignee if str(assignee).strip().lower() != "unassigned" else ""
    assignee_email = (P.resolve(assignee_name, role="assignee", group_hint=group)
                      if assignee_name else None)
    conn.execute("INSERT OR REPLACE INTO linear_teams(team, group_id) VALUES (?,?)", (team, group))

    identifier = str(raw.get("key") or "").strip() or synth.linear_identifier(
        dsid, synth.linear_team_key(team))
    state = raw.get("status")
    created = to_epoch(raw.get("created_at")) or synth.epoch(dsid)
    updated = to_epoch(raw.get("updated_at"))
    # The bench records no lifecycle timestamps, but a state IS one: Linear sets completedAt the
    # moment an issue enters a completed state and canceledAt when it is canceled, so derive them
    # from the state category + the last-updated clock. Everything else stays NULL rather than
    # being invented.
    state_type = synth.linear_state_type(state)
    ended = updated or created
    conn.execute(
        "INSERT OR REPLACE INTO linear_issues(doc_id, team, author_email, title, content, "
        "identifier, state, priority, estimate, labels, project, cycle, branch_name, due_date, "
        "created_ts, updated_ts, completed_ts, canceled_ts, started_ts, "
        "assignee_email, assignee_display, owner_display, parent_key, release) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (dsid, team, creator_email or f"unknown@{P.org_domain}", title, content,
         identifier, state, linear_priority(raw.get("priority")),
         _linear_int(raw.get("estimate")), json.dumps(_names(raw.get("labels"))),
         raw.get("project"), raw.get("cycle"),
         synth.linear_branch_name(identifier, title, assignee_email),
         _linear_date(raw.get("due_date")), created, updated,
         ended if state_type == "completed" else None,
         ended if state_type == "canceled" else None,
         created if state_type in ("started", "completed") else None,
         assignee_email, assignee_name or None, creator,
         _linear_parent(raw.get("parent_issue")), _linear_release(raw.get("release"))))

    for seq, att in enumerate(parse_linear_attachments(raw.get("links"),
                                                       raw.get("attachments")), start=1):
        conn.execute(
            "INSERT OR REPLACE INTO linear_attachments(id, doc_id, seq, title, url, subtitle, "
            "source_type, created_ts) VALUES (?,?,?,?,?,?,?,?)",
            (f"{dsid}::a{seq}", dsid, seq, att["title"], att["url"], None, None, created))

    prev_ts = created
    for seq, c in enumerate(parse_linear_comments(raw.get("comments")), start=1):
        # A comment author is matched against the EXISTING roster (`display_email`), never
        # minted with `P.resolve` the way an issue's creator/assignee is. The `Name:` segment in
        # a Linear comment is far noisier than Jira's — thousands of distinct strings, many of
        # them labels like "Design review" or "QA sign-off" that `_person_like` happily accepts
        # as a two-token name. Minting those would add people who don't exist to `principals`.
        # An unmatched name leaves the comment unattributed, which is both honest and a legal
        # Linear response (`Comment.user` is nullable) — and its body keeps the prefix, because
        # an unresolved "Created:" is part of the text, not an attribution.
        author = P.display_email(c["name"])[0] if c["name"] else None
        body = c["body"] if author else c["body_with_name"]
        # Comment time, in order of preference: its own date; else one second after the previous
        # comment. MONOTONIC, not `created + seq`: a date-less comment at the end of a dated
        # thread would otherwise land back at the issue's creation date and be served FIRST
        # (`Issue.comments` orders by createdAt, as Linear does). Also never NULL — the column
        # is NOT NULL.
        ts = to_epoch(c["date"]) or (prev_ts + 1)
        prev_ts = max(prev_ts, ts)
        conn.execute(
            "INSERT OR REPLACE INTO linear_comments(id, doc_id, seq, author_email, body, created_ts)"
            " VALUES (?,?,?,?,?,?)",
            (f"{dsid}::c{seq}", dsid, seq, author, body, ts))

    people = [creator_email, assignee_email]
    return {"owner": creator_email, "people": [p for p in people if p], "group": group,
            "confidentiality": None,
            # Consumed by the second pass below, which needs every issue loaded before it can
            # resolve a key to a doc_id.
            "_linear_parent_key": _linear_parent(raw.get("parent_issue")),
            "_linear_relations": parse_linear_relations(raw.get("dependencies"))}


def resolve_linear_references(conn, bundles) -> dict:
    """SECOND PASS: turn the issue KEYS the bench references into doc_ids.

    Has to be a second pass because a parent or a dependency target may be loaded after the issue
    that names it. Has to happen at IMPORT rather than per request because bench identifiers are
    not unique — 45.4% of parent references match more than one issue, and `ENG-314159` alone is
    claimed by 218 children while being the identifier of 107 issues, so a serve-time join on
    `identifier` would invent 23,326 edges from that one key.

    Resolution picks the FIRST match by doc_id, the same rule `store.linear_issue_by_identifier`
    applies, so `Issue.parent` and `Issue.children` are exact inverses rather than two independent
    lookups. A key matching nothing resolves to nothing: a dangling reference must never become a
    relation pointing at an issue that does not exist (the rule `load_hubspot` already applies to
    its `linked_*` stubs).
    """
    key_to_doc: dict[str, str] = {}
    for doc_id, identifier in conn.execute(
            "SELECT doc_id, identifier FROM linear_issues WHERE identifier IS NOT NULL "
            "ORDER BY doc_id"):
        key_to_doc.setdefault(identifier, doc_id)

    stats = {"parents": 0, "parents_dangling": 0, "relations": 0, "relations_dangling": 0}
    for dsid, bundle in bundles.items():
        if bundle.get("_source") != "linear":
            continue
        key = bundle.get("_linear_parent_key")
        if key:
            target = key_to_doc.get(key)
            # An issue is not its own parent; the bench's repeated keys make that reachable.
            if target and target != dsid:
                conn.execute("UPDATE linear_issues SET parent_doc_id = ? WHERE doc_id = ?",
                             (target, dsid))
                stats["parents"] += 1
            else:
                stats["parents_dangling"] += 1
        seen = set()
        for seq, (rel_type, rel_key) in enumerate(bundle.get("_linear_relations") or [], start=1):
            target = key_to_doc.get(rel_key)
            if not target or target == dsid or (rel_type, target) in seen:
                stats["relations_dangling"] += 1
                continue
            seen.add((rel_type, target))
            conn.execute(
                "INSERT OR REPLACE INTO linear_relations(id, from_doc_id, to_doc_id, type, "
                "created_ts) VALUES (?,?,?,?,?)",
                (f"{dsid}::r{seq}", dsid, target, rel_type,
                 conn.execute("SELECT created_ts FROM linear_issues WHERE doc_id = ?",
                              (dsid,)).fetchone()[0]))
            stats["relations"] += 1
    conn.commit()
    return stats


# ---------------------------------------------------------------- fireflies
# The bench's Fireflies docs are meeting transcripts, one per file, with a standard ERB envelope
# plus the metadata its `sources/fireflies/agents.md` documents: meeting_id, recorded_at,
# duration_minutes, call_type, title, redwood_owner/redwood_attendees, customer_company/
# customer_attendees, and optional summary/topics/action_items/next_steps/competitors_mentioned/
# crm_deal_id/transcription_quality.
#
# Four properties of the real data drive the mapping:
#   * the transcript is ONE FLAT TEXT BLOB, not structured per-sentence records. So the sentence
#     rows the API serves are PARSED from it here (620k sentences over 10,173 docs), and
#     `synth.fireflies_transcript_text` is the exact inverse. Only start times are in the data
#     (99.91% of lines); end times are derived (synth.fireflies_fill_times).
#   * the blob is written in six interchangeable line formats — "[00:00] Name:", "00:00 - Name:",
#     "00:00 [Name]:", "(00:00) Name:", "[S00:12] Name (Role):", and un-timestamped "Name:" — and
#     ~7.7% of docs open with an auto-notes preamble whose "Date:"/"Duration:" lines look exactly
#     like speaker lines. Hence one recognizer for all six plus participant gating (below).
#   * NO email addresses appear anywhere in the corpus, so host/organizer/attendee identities are
#     resolved through `Principals` exactly as every other loader does.
#   * `meeting_id` is not unique (10,147 distinct over 10,173 docs), so it becomes `calendar_id`
#     and the API's `id` is synthesized — see the fireflies_transcripts schema.

_FF_CLOCK = r"\d{1,2}:\d{2}(?::\d{2})?"
# A leading timestamp in any form the bench writes, optionally followed by a "-"/"–" separator.
# The optional letter inside the brackets absorbs a quirk the corpus contains ("[S00:12]").
_FF_TS = (rf"(?:\[[A-Za-z]?(?P<b>{_FF_CLOCK})\]|\((?P<p>{_FF_CLOCK})\)|(?P<r>{_FF_CLOCK}))"
          r"\s*(?:[-–—]\s*)?")
# 1-4 name-ish words, optionally bracketed ("[Maya]"), with a trailing "(Role)" / ", Role" that
# is stripped before matching ("Ari (Redwood AE)", "Mark, Sentinel CISO").
_FF_WHO = r"[A-Za-z@][\w.'’\-]*(?: +[A-Za-z0-9][\w.'’\-]*){0,3}"
_FF_UTT = re.compile(rf"^\s*(?:{_FF_TS})?(?:\[(?P<name>{_FF_WHO})\]|(?P<name2>{_FF_WHO}))"
                     rf"(?: *[(,][^)]*\)?)?:[ \t]*(?P<text>.*)$")

# Fireflies' own auto-notes header labels. Each looks exactly like a speaker line
# ("Date: 2025-02-20", "Duration: ~52 minutes"), so none may ever mint a speaker.
_FF_NOT_SPEAKER = {
    "date", "duration", "attendees", "attendees present", "participants", "header",
    "meeting header", "meeting", "meeting date", "meeting title", "meeting start", "title",
    "time", "location", "host", "organizer", "recorded", "recording", "meeting recording",
    "summary", "auto-summary", "summary (auto)", "auto-generated summary", "human summary",
    "topics", "topics covered", "transcript", "transcript body", "action items", "next steps",
    "questions", "notes", "notes on transcription", "agenda", "call type", "start", "end", "note",
}


def _ff_role_stripped(name: str) -> str:
    """'Leah Nguyen - Head of Product' / 'Ana Ruiz, CTO' / 'Ari (Redwood AE)' -> the bare name."""
    s = re.sub(r"\s*\([^)]*\)", "", str(name or ""))
    s = re.sub(r"\s+[-–—]\s+.*$", "", s)
    return re.sub(r"\s*,.*$", "", s).strip()


def fireflies_speaker_map(attendees) -> dict[str, str]:
    """canonical key -> the attendee's clean display name, for every declared attendee AND their
    first name alone, because transcripts overwhelmingly label speakers by first name. Reuses
    :func:`canonical`, so a middle initial collapses too ('Priya S.' resolves to 'Priya Shah')."""
    out: dict[str, str] = {}
    for a in attendees or []:
        clean = _ff_role_stripped(a)
        if not clean:
            continue
        out.setdefault(canonical(clean), clean)
        first = clean.split()[0]
        if len(first) > 1:
            out.setdefault(canonical(first), clean)
    return out


def _ff_resolve_speaker(label: str, pmap: dict[str, str]) -> str | None:
    """A speaker label -> the declared attendee's display name, or None if it names nobody.
    Tries the whole label and then each side of a dash, so a role-prefixed label
    ('Moderator - Alex', 'AE - Priya Shah') still resolves."""
    for cand in [label, *(p.strip() for p in re.split(r"\s+[-–—]\s+", label))]:
        if cand and (key := canonical(_ff_role_stripped(cand))) in pmap:
            return pmap[key]
    return None


def _ff_secs(clock: str) -> float:
    parts = [int(x) for x in clock.split(":")]
    return float(parts[0] * 60 + parts[1] if len(parts) == 2
                 else parts[0] * 3600 + parts[1] * 60 + parts[2])


def parse_fireflies_transcript(text, attendees: list | None = None) -> list[dict]:
    """A flat Fireflies transcript blob -> ``[{speaker_name, text, start_time}]``.

    Mirrors :func:`parse_slack_transcript`: a line only starts a NEW sentence when its speaker is
    a declared attendee, so the auto-notes preamble ("Date: …", "Duration: …") and mid-transcript
    prose (numbered action-item recaps) stay continuation text of the current sentence instead of
    minting fake speakers. When gating recognizes nobody at all — the corpus deliberately contains
    transcripts labeled only "Speaker 1"/"Speaker 2", which ``agents.md`` calls for — it falls back
    to ungated splitting so those meetings still get sentences.
    """
    pmap = fireflies_speaker_map(attendees)
    lines = _unescape(_stringify(text)).split("\n")

    def run(gated: bool) -> list[dict]:
        out: list[list] = []
        cur: list | None = None
        for line in lines:
            m = _FF_UTT.match(line)
            speaker = None
            if m:
                label = m.group("name") or m.group("name2")
                if gated:
                    speaker = _ff_resolve_speaker(label, pmap)
                elif label.strip().lower() not in _FF_NOT_SPEAKER:
                    speaker = label.strip()
            if speaker is not None:
                clock = m.group("b") or m.group("p") or m.group("r")
                cur = [speaker, [m.group("text")], _ff_secs(clock) if clock else None]
                out.append(cur)
            elif cur is not None:
                cur[1].append(line)  # continuation (incl. a non-speaker "phrase: text" line)
        return [{"speaker_name": s, "text": "\n".join(ls).rstrip(), "start_time": t}
                for s, ls, t in out]

    sentences = (run(True) if pmap else []) or run(False)
    if sentences:
        return sentences
    # 17 of the bench's transcripts are prose with no speaker labels at all. They still have to
    # serve their text, and `content` is defined as the sentence concatenation, so the whole body
    # becomes ONE unattributed sentence rather than an empty document. speaker_name stays null,
    # which is what the real API returns when diarization produced no label.
    body = "\n".join(lines).strip()
    return [{"speaker_name": None, "text": body, "start_time": 0.0}] if body else []


def _ff_attendee_names(raw) -> tuple[list[str], list[str]]:
    """(internal Redwood names, external customer names) as the bench declares them."""
    internal = [_ff_role_stripped(n) for n in _names(raw.get("redwood_attendees"))]
    owner = _ff_role_stripped(raw.get("redwood_owner") or "")
    if owner and owner not in internal:
        internal.insert(0, owner)
    external = [_ff_role_stripped(n) for n in _names(raw.get("customer_attendees"))]
    return [n for n in internal if n], [n for n in external if n]


def _ff_summary(raw) -> dict:
    """The bench's auto-notes fields mapped onto the real API's `summary` object.

    They are NOT folded into `content`: the API's own `keyword`/`scope` filter searches `title` and
    `sentences`, so putting summary prose in the sentence text would both break the sentence
    round-trip and make `scope: sentences` match words nobody said.
    """
    def lines(*keys):
        for k in keys:
            v = raw.get(k)
            if isinstance(v, list) and v:
                return [str(x) for x in v if str(x).strip()]
            if isinstance(v, str) and v.strip():
                return [s for s in (ln.strip() for ln in v.split("\n")) if s]
        return []

    overview = raw.get("summary")
    if isinstance(overview, list):
        overview = "\n".join(str(x) for x in overview)
    topics = lines("topics", "topics_covered", "transcript_topics", "Topics")
    actions = lines("action_items", "action_items_auto", "fireflies_action_items")
    return {
        "overview": (str(overview).strip() or None) if overview else None,
        "topics_discussed": topics or None,
        "action_items": actions or None,
        # Fireflies renders action items as one newline-joined string too; both shapes are real.
        "shorthand_bullet": "\n".join(actions) or None,
        "keywords": lines("keywords", "meeting_keywords", "tags", "auto_tags") or None,
        # `next_steps` has no Fireflies field of its own; the product folds next steps into the
        # outline, which is exactly what it is.
        "outline": lines("next_steps", "next_steps_verbose") or None,
        "meeting_type": raw.get("call_type") or None,
    }


# Where the transcript body lives. `transcript` covers 99.1% of the corpus; the rest of this
# list is the long tail of ad-hoc key names the bench also uses, and the `*_continued` keys are
# docs whose body is split across several fields (they are appended, not treated as alternatives).
_FF_BODY_KEYS = ("transcript", "transcript_text", "transcript_body", "full_transcript",
                 "meeting_transcript", "Transcript", "transcript_full", "detailed_transcript",
                 "transcription", "full_transcript_body", "transcript_final", "body_transcript",
                 "body", "content")
_FF_BODY_MORE = ("transcript_continued", "transcript_continued_2", "transcript_continued_3",
                 "additional_transcript", "additional_transcript_part2", "continued_transcript",
                 "tail_transcript")


def _ff_transcript_text(raw) -> str:
    """The transcript body. Falls back to the ERB envelope's own derived content for the 3 docs
    that carry no transcript field at all, so such a meeting still serves its text."""
    first = next((_stringify(raw[k]) for k in _FF_BODY_KEYS if raw.get(k)), "")
    parts = [first] + [_stringify(raw[k]) for k in _FF_BODY_MORE if raw.get(k)]
    body = "\n\n".join(p for p in parts if p)
    return body or derive_title_content(raw)[1]


def _ff_duration(value) -> float | None:
    """Meeting length in MINUTES, which is the unit the Fireflies API's `duration` uses. The bench
    writes it as a string ("72"), an int, or prose ("~64 minutes")."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(m.group()) if m else None


def _ff_speaker_stats(sentences) -> list[dict]:
    """Per-speaker talk time and word counts, computed from the sentences themselves — the only
    part of `analytics` the transcript actually supports (sentiment is not derivable, see
    synth.fireflies_analytics)."""
    agg: dict[str, dict] = {}
    for s in sentences:
        name = s.get("speaker_name") or None
        a = agg.setdefault(name or "", {"name": name, "duration_secs": 0.0, "word_count": 0,
                                        "monologues_count": 0, "longest_monologue": 0.0})
        span = max(0.0, float(s.get("end_time") or 0) - float(s.get("start_time") or 0))
        a["duration_secs"] += span
        a["word_count"] += len((s.get("text") or "").split())
        a["monologues_count"] += 1
        a["longest_monologue"] = max(a["longest_monologue"], span)
    for a in agg.values():
        a["duration_secs"] = round(a["duration_secs"], 2)
        a["longest_monologue"] = round(a["longest_monologue"], 2)
    return list(agg.values())


def _ff_meeting_attendees(raw, internal: list[str], external: list[str], P) -> list[dict]:
    """The API's `meeting_attendees` — {displayName, email, phoneNumber, name, location}. The bench
    names people without emails, so each is resolved the way its side allows: Redwood attendees to
    org identities, customer attendees to external contacts."""
    out = []
    for name, role in [(n, "participant_internal") for n in internal] + \
                      [(n, "participant_external") for n in external]:
        out.append({"displayName": name, "email": P.resolve(name, role=role),
                    "phoneNumber": None, "name": name,
                    "location": raw.get("customer_company") if role in EXTERNAL_ROLES else None})
    return out


def load_fireflies(conn, dsid, raw, P):
    # The bench's subdirectory is the workspace its agents.md describes -> the Fireflies channel.
    # A doc at the source root (11 of them, which agents.md says should not exist) lands in
    # "uncategorized", the label the Fireflies UI itself uses for an ungrouped meeting.
    path = raw.get("_erb_path") or ""
    channel = path.split("/")[0] if "/" in path else "uncategorized"
    title = str(raw.get(raw.get("title_field_name", "title"), "")).strip()

    internal, external = _ff_attendee_names(raw)
    # The channel IS the ACL group, as it is for slack: a Fireflies channel is a workspace
    # grouping, not a department, so it must not be reconciled against the directory's dept slugs
    # (and `call_type` is a meeting kind, not an org unit — it is served as `summary.meeting_type`).
    group = channel
    owner_display = _ff_role_stripped(raw.get("redwood_owner") or "")
    host_email = P.resolve(owner_display, role="owner") if owner_display else None
    # Everyone named on the meeting, resolved the way each kind of reference can be: Redwood
    # attendees are internal identities, customer attendees are external contacts (never
    # principals). Only the internal ones can authenticate, so only they become ACL grants.
    internal_emails = [e for e in (P.resolve(n, role="participant_internal") for n in internal) if e]
    external_emails = [e for e in (P.resolve(n, role="participant_external") for n in external) if e]

    sentences = parse_fireflies_transcript(_ff_transcript_text(raw), internal + external)
    duration = _ff_duration(raw.get("duration_minutes"))
    synth.fireflies_fill_times(sentences, (duration * 60) if duration else None)
    # content is DEFINED as the sentence concatenation, so it and the sentence rows can never drift.
    content = synth.fireflies_transcript_text(sentences)
    created_ts = to_epoch(raw.get("recorded_at")) or synth.epoch(dsid)
    tid = synth.fireflies_id(dsid)

    # speaker_id is a per-MEETING ordinal in Fireflies, assigned by first appearance.
    ordinals: dict[str, int] = {}
    for s in sentences:
        ordinals.setdefault(s["speaker_name"] or "", len(ordinals))

    conn.execute("INSERT OR REPLACE INTO fireflies_channels(channel, group_id) VALUES (?,?)",
                 (channel, group))
    conn.execute(
        "INSERT OR REPLACE INTO fireflies_transcripts(doc_id, channel, author_email, title, "
        "content, transcript_id, calendar_id, calendar_type, organizer_email, duration, "
        "created_ts, summary, analytics, participants, meeting_attendees, audio_url, video_url, "
        "transcript_url, meeting_link, owner_display) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        # sort_keys on every JSON column, for the reason `_hs_insert` gives: the stored text must
        # not depend on the source file's key order, or the same data yields two different values.
        (dsid, channel, host_email or f"unknown@{P.org_domain}", title, content, tid,
         raw.get("meeting_id"), "google_calendar", None, duration, created_ts,
         json.dumps(_ff_summary(raw), sort_keys=True),
         json.dumps(synth.fireflies_analytics(
             dsid, _ff_speaker_stats(sentences), (duration * 60) if duration else None),
             sort_keys=True),
         json.dumps(internal_emails + external_emails),
         json.dumps(_ff_meeting_attendees(raw, internal, external, P), sort_keys=True),
         synth.fireflies_media_url(tid, "audio"), synth.fireflies_media_url(tid, "video"),
         synth.fireflies_transcript_url(tid), synth.fireflies_meeting_link(dsid),
         # NULL, not "", when the meeting names no host: an absent owner is absent, and "" would
         # be served as a display name that is the empty string.
         owner_display or None))

    for seq, s in enumerate(sentences, start=1):
        name = s["speaker_name"] or ""
        conn.execute(
            "INSERT OR REPLACE INTO fireflies_sentences(id, doc_id, seq, author_email, body, "
            "created_ts, reactions, speaker_name, speaker_id, start_time, end_time) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            # A sentence sits on the meeting's own clock (start + its offset), so ordering by time
            # never shuffles a transcript. The speaker resolves to an identity only when the label
            # names a declared attendee — an anonymous "Speaker 3" stays unattributed, as it is.
            (f"{dsid}::s{seq}", dsid, seq,
             P.resolve(name, role="participant_internal") if name in internal else None,
             s["text"], int(created_ts + (s["start_time"] or 0)), None,
             name or None, ordinals.get(name), s["start_time"], s["end_time"]))

    # Like slack/hubspot, the corpus names far more people than can authenticate — only 938 of
    # 10,173 hosts and 3,184 of 27,786 attendee references are in the employee directory — so an
    # owner-or-group scope would leave ~91% of the meetings readable by admin and nobody else.
    # Org-visible, plus a real per-user grant for everyone who does resolve (see grants_for).
    # No `_children`: that list exists so a materialized row which is ACL-filtered BY ITS OWN
    # doc_id carries the parent's grants (a slack reply, a gmail message, a hubspot note — all rows
    # in an ACL-filtered document table). A sentence is not one. `fireflies_sentences` is only ever
    # read through its parent (`store.fireflies_sentences`, and `scope: sentences` searches the
    # transcript's own `content` column with the ACL clause on `fireflies_transcripts`), so a grant
    # naming a sentence id is a row no query looks at — 620k of them over the bench corpus.
    return {"owner": host_email, "people": internal_emails, "group": group,
            "confidentiality": None}


_LOADERS = {"google_drive": load_drive, "github": load_github, "confluence": load_confluence,
            "jira": load_jira, "gmail": load_gmail, "slack": load_slack,
            "hubspot": load_hubspot, "linear": load_linear, "fireflies": load_fireflies}


# ---------------------------------------------------------------- ERB -> BYO-JSONL
# The other half of every ``load_*`` above: the same mapping, written as a BYO record instead of as
# an INSERT. The unified dataset redistributes ERB pre-converted into BYO-JSONL, so there is one
# schema and one importer; this is what converts it.
#
# It lives in this module rather than its own because it must mirror the loaders' decisions
# exactly — mapping choices (a bench `doc_type` onto a Workspace type, a "P1" onto Linear's scale,
# which fields become columns) are made once, in the function right above its converter, and the
# two are edited together. ``tests/test_importer_erb.py`` then diffs the two databases, so a drift
# between them fails a test rather than silently producing a lossy artifact.
#
# Three things this file computes at IMPORT time, which a single record cannot recompute on its
# own, therefore have to be baked into the converted values:
#   * resolved principal emails — a display name becomes a real address via the employee directory,
#     which ships alongside the corpus as a roster (see ``Principals.write_roster``)
#   * the Slack far-future timestamp remap — rank-based over every thread, so it is not a function
#     of one record
#   * identifier -> doc_id resolution for a Linear parent/relation, since bench keys repeat
_LINEAR_KEY_TO_DOC: dict[str, str] = {}


def build_linear_key_index(records) -> dict[str, str]:
    """identifier -> doc_id, FIRST match by doc_id — the same rule
    :func:`resolve_linear_references` applies (and ``store.linear_issue_by_identifier`` after it),
    so a converted artifact resolves a parent or a relation to the same issue a direct import does.
    Bench keys are not unique (5,055 repeat), which is why this is resolved once here and not by a
    serve-time join."""
    out: dict[str, str] = {}
    for _src, dsid, raw in sorted((r for r in records if r[0] == "linear"), key=lambda r: r[1]):
        identifier = str(raw.get("key") or "").strip() or synth.linear_identifier(
            dsid, synth.linear_team_key(str(raw.get("team") or "engineering")))
        out.setdefault(identifier, dsid)
    return out


def _rec(**kw) -> dict:
    """A BYO record with the absent fields dropped — a key set to None and a key left out both load
    as NULL, so the record states only what the source document actually carries."""
    return {k: v for k, v in kw.items() if v is not None}


def _byo_readers(source: str, bundle: dict, org: str) -> list[str] | None:
    """The bundle's ACL as BYO ``readers`` — typed principal ids, so the same grants come out.

    :func:`grants_for` is REUSED rather than re-derived: ACL is the one place where restating the
    rules would be both duplicated and unverifiable per record. ``None`` means "say nothing", which
    is BYO's default of a single org grant — the common case, kept out of the artifact entirely."""
    grants = grants_for(source, {**bundle, "org": org})
    if grants == [("org", org)]:
        return None
    return [f"{t}:{pid}" for t, pid in grants]


def _byo_confluence(dsid, raw, P):
    title, content = _title_content(raw)
    space = raw.get("space") or "SPACE"
    group = P.canonical_group(raw.get("owner_team")) or space
    author = raw.get("author", "")
    author_email = P.resolve(author, role="author", group_hint=group) if author else None
    reviewers = _resolved(P, raw.get("reviewers"), role="reviewer")
    rec = _rec(source_type="confluence", doc_id=dsid, space=space, title=title, content=content,
               author_email=author_email, author_name=author, subtype="page",
               labels=_names(raw.get("labels")), reviewers=reviewers,
               confidentiality=raw.get("confidentiality"), owner_team=raw.get("owner_team"),
               created=(to_epoch(raw.get("created_at")) or synth.epoch(dsid)),
               updated=to_epoch(raw.get("last_updated")))
    rec["group"] = group
    return [rec], {"owner": author_email, "people": reviewers, "group": group,
                   "confidentiality": raw.get("confidentiality")}


def _byo_drive(dsid, raw, P):
    title, content = _title_content(raw)
    group = P.canonical_group(raw.get("team"))
    owner = raw.get("owner", "")
    owner_email = P.resolve(owner, role="owner", group_hint=group) if owner else None
    collabs = _resolved(P, raw.get("collaborators"), role="collaborator")
    # The MAPPED type, not the bench's own `doc_type` vocabulary: `_drive_type` resolves a native
    # Workspace subtype or a binary's mime, and re-deriving it on load would need `_ATT_MIME` and the
    # title's extension inside `byo.py`. So the converted record carries the resolved pair.
    subtype, mime_type = _drive_type(raw, title)
    rec = _rec(source_type="google_drive", doc_id=dsid,
               folder=(raw.get("drive_area") or group or "drive"), title=title, content=content,
               author_email=owner_email, author_name=owner,
               subtype=subtype, mime_type=mime_type, collaborators=collabs,
               created=(to_epoch(raw.get("created_at")) or synth.epoch(dsid)),
               updated=to_epoch(raw.get("last_modified")))
    # A doc with no team owns no group, and `"group": null` is how BYO says so — inferring one from
    # the folder name would invent a grantable principal the direct import does not have.
    rec["group"] = group
    return [rec], {"owner": owner_email, "people": collabs, "group": group,
                   "confidentiality": None}


def _byo_github(dsid, raw, P):
    title, content = _title_content(raw)
    author = raw.get("author", "")
    author_email = P.resolve(author, role="author", group_hint=raw.get("repo")) if author else None
    reviewers = _resolved(P, raw.get("reviewers"), role="reviewer")
    repo = raw.get("repo") or "repo"
    rec = _rec(source_type="github", doc_id=dsid, repo=repo, title=title, content=content,
               author_email=author_email, author_name=author,
               subtype=("pull_request" if raw.get("pr_number") else "issue"),
               state=raw.get("state"), labels=_names(raw.get("labels")),
               requested_reviewers=reviewers,
               created=(to_epoch(raw.get("created_at")) or synth.epoch(dsid)),
               updated=to_epoch(raw.get("updated_at")))
    rec["group"] = repo
    return [rec], {"owner": author_email, "people": reviewers, "group": repo,
                   "confidentiality": None}


def _byo_jira(dsid, raw, P):
    title, content = _title_content(raw)
    reporter = raw.get("reporter", "")
    assignee = raw.get("assignee", "")
    group = P.canonical_group(raw.get("squad")) or (raw.get("project") or "JIRA")
    reporter_email = P.resolve(reporter, role="reporter", group_hint=group) if reporter else None
    assignee_email = P.resolve(assignee, role="assignee", group_hint=group) if assignee else None
    project = raw.get("project") or "JIRA"
    comments = [
        _rec(id=f"{dsid}::c{seq}", content=c["body"],
             author_email=P.resolve(c["name"], role="author"), created_ts=to_epoch(c["date"]))
        for seq, c in enumerate(parse_jira_comments(raw.get("comments", [])), start=1)]
    rec = _rec(source_type="jira", doc_id=dsid, project=project, title=title, content=content,
               author_email=reporter_email, author_name=reporter, status=raw.get("status"),
               issuetype=raw.get("issue_type"), priority=raw.get("priority"),
               labels=_names(raw.get("labels")), components=_names(raw.get("components")),
               assignee=assignee_email, reporter=reporter_email, severity=raw.get("severity"),
               squad=raw.get("squad"), duedate=raw.get("due_date"),
               comments=(comments or None),
               created=(to_epoch(raw.get("created_at")) or synth.epoch(dsid)),
               updated=to_epoch(raw.get("updated_at")))
    rec["group"] = group
    people = [p for p in (assignee_email, reporter_email) if p]
    return [rec], {"owner": reporter_email, "people": people, "group": group,
                   "confidentiality": None}


def _byo_gmail(dsid, raw, P):
    title, content = _title_content(raw)
    raw_msgs = raw.get("messages")
    msgs = parse_gmail_thread(raw_msgs) if isinstance(raw_msgs, list) and raw_msgs else []
    owner_name = raw.get("mailbox_owner", "")
    mailbox = _slug_mailbox(owner_name) or "inbox"
    owner_email = P.resolve(owner_name, role="mailbox_owner") if owner_name else None
    internal = _resolved(P, raw.get("participants_internal"), role="participant_internal")
    root = msgs[0] if msgs else {}
    attachments = _gmail_attachments(raw)
    root_ts = to_epoch(root.get("date")) or to_epoch(raw.get("first_email_at")) or synth.epoch(dsid)
    # The thread's later messages, each a full message with its own sender/recipients/Message-ID.
    # A date-less one carries the hour-per-position time the loader gives it, since the artifact has
    # to be explicit about a value it computed rather than read.
    messages = [
        _rec(doc_id=f"{dsid}::m{seq}", content=m.get("body", ""),
             author_email=m.get("from_email"), title=(m.get("subject") or title),
             to=m.get("to"), cc=m.get("cc"), message_id=m.get("message_id"),
             created=(to_epoch(m.get("date")) or (root_ts + seq * 3600)))
        for seq, m in enumerate(msgs[1:], start=1)]
    rec = _rec(source_type="gmail", doc_id=dsid, mailbox=mailbox,
               title=(title or root.get("subject") or ""),
               content=(root.get("body") or (content if not msgs else "")),
               author_email=(root.get("from_email") or owner_email),
               mailbox_owner=owner_name, thread=dsid, to=root.get("to"), cc=root.get("cc"),
               message_id=root.get("message_id"), attachments=(attachments or None),
               messages=(messages or None), created=root_ts)
    # A mailbox has no ACL group: a thread is private to its participants, which `readers` states
    # per document (see grants_for).
    rec["group"] = None
    people = [p for p in (owner_email, *internal) if p]
    return [rec], {"owner": owner_email, "people": people, "group": None,
                   "confidentiality": None}


def _byo_slack(dsid, raw, P):
    channel = raw.get("channel") or "general"
    _title, content = _title_content(raw)
    participants = _names(raw.get("participants"))
    turns = parse_slack_transcript(content, participants)
    root_author = P.resolve(turns[0][0], role="slack_participant") if turns else None
    root_ts = _SLACK_TS_REMAP.get(dsid) or to_epoch(raw.get("first_message_ts")) or synth.epoch(dsid)
    replies = [_rec(doc_id=f"{dsid}::m{seq}", content=text,
                    author_email=P.resolve(spk, role="slack_participant"))
               for seq, (spk, text) in enumerate(turns[1:], start=1)]
    rec = _rec(source_type="slack", doc_id=dsid, channel=channel,
               content=(turns[0][1] if turns else content), author_email=root_author,
               participants=participants, replies=(replies or None),
               # The remapped value, NOT the source `first_message_ts`: the remap is rank-based over
               # every slack thread, so it cannot be recomputed from this record.
               created=root_ts)
    rec["group"] = channel
    # Slack speakers are display labels rather than org identities, so the doc is org-visible and
    # `people` stays empty — same as the loader's bundle.
    return [rec], {"owner": root_author, "people": [], "group": channel, "confidentiality": None}


def _byo_hubspot(dsid, raw, P):
    title, content = _title_content(raw)
    object_type = group = "companies"
    owner = raw.get("owner", "")
    owner_email = P.resolve(owner, role="owner", group_hint=group) if owner else None
    people = [P.resolve(n, role="collaborator")
              for n in (raw.get("se_assigned"), raw.get("csm_assigned")) if n]
    props = {_HS_PROPERTY.get(k, k): v for k, v in raw.items() if k not in _HS_NOT_A_PROPERTY}
    created = to_epoch(raw.get("created_at")) or synth.epoch(dsid)
    # A note is its own CRM object associated with the company, so it converts to its own record —
    # which is exactly how a BYO author would write one.
    notes, links = [], []
    for i, body in enumerate(_hs_notes(raw), start=1):
        note_id = f"{dsid}::n{i}"
        links.append({"to": note_id, "to_type": "notes"})
        note = _rec(source_type="hubspot", doc_id=note_id, object_type="notes", title="",
                    content=body, author_email=owner_email,
                    properties={"hs_note_body": body,
                                "hs_timestamp": synth.rfc3339(created + i)},
                    created=created + i)
        # The notes object type hangs off the COMPANY's group, not off its own name.
        note["group"] = group
        notes.append(note)
    rec = _rec(source_type="hubspot", doc_id=dsid, object_type=object_type, title=title,
               content=content, author_email=owner_email, author_name=owner, properties=props,
               associations=(links or None), created=created,
               updated=to_epoch(raw.get("updated_at")))
    rec["group"] = group
    return [rec, *notes], {"owner": owner_email, "people": [p for p in people if p],
                           "group": group, "confidentiality": None}


def _byo_linear(dsid, raw, P):
    title, content = _title_content(raw)
    team = str(raw.get("team") or "engineering")
    group = P.canonical_group(team) or team
    creator = raw.get("creator", "")
    assignee = raw.get("assignee", "")
    creator_email = P.resolve(creator, role="author", group_hint=group) if creator else None
    assignee_name = assignee if str(assignee).strip().lower() != "unassigned" else ""
    assignee_email = (P.resolve(assignee_name, role="assignee", group_hint=group)
                      if assignee_name else None)
    identifier = str(raw.get("key") or "").strip() or synth.linear_identifier(
        dsid, synth.linear_team_key(team))
    state = raw.get("status")
    created = to_epoch(raw.get("created_at")) or synth.epoch(dsid)
    updated = to_epoch(raw.get("updated_at"))
    state_type = synth.linear_state_type(state)
    ended = updated or created

    comments, prev_ts = [], created
    for seq, c in enumerate(parse_linear_comments(raw.get("comments")), start=1):
        author = P.display_email(c["name"])[0] if c["name"] else None
        ts = to_epoch(c["date"]) or (prev_ts + 1)
        prev_ts = max(prev_ts, ts)
        comments.append(_rec(id=f"{dsid}::c{seq}",
                             content=(c["body"] if author else c["body_with_name"]),
                             author_email=author, created_ts=ts))
    # A relation names its target by doc_id in BYO, so the bench's issue KEY is resolved here —
    # dropping a dangling, self- or duplicate reference exactly as `resolve_linear_references`
    # does, and keeping the original position in the id so the two agree row for row.
    relations, seen = [], set()
    for seq, (rel_type, rel_key) in enumerate(
            parse_linear_relations(raw.get("dependencies")), start=1):
        target = _LINEAR_KEY_TO_DOC.get(rel_key)
        if not target or target == dsid or (rel_type, target) in seen:
            continue
        seen.add((rel_type, target))
        relations.append({"id": f"{dsid}::r{seq}", "to": target, "type": rel_type})

    rec = _rec(source_type="linear", doc_id=dsid, team=team, title=title, content=content,
               author_email=creator_email, author_name=creator, identifier=identifier,
               state=state, priority=linear_priority(raw.get("priority")),
               estimate=_linear_int(raw.get("estimate")), labels=_names(raw.get("labels")),
               project=raw.get("project"), cycle=raw.get("cycle"),
               branchName=synth.linear_branch_name(identifier, title, assignee_email),
               dueDate=_linear_date(raw.get("due_date")),
               assignee=assignee_email, assigneeName=(assignee_name or None),
               # The parent's own identifier, as the corpus wrote it — BYO resolves it to a doc_id
               # on load with the same first-match rule.
               parent=_linear_parent(raw.get("parent_issue")),
               release=_linear_release(raw.get("release")),
               completedAt=(ended if state_type == "completed" else None),
               canceledAt=(ended if state_type == "canceled" else None),
               startedAt=(created if state_type in ("started", "completed") else None),
               attachments=(parse_linear_attachments(raw.get("links"),
                                                    raw.get("attachments")) or None),
               comments=(comments or None), relations=(relations or None),
               created=created, updated=updated)
    rec["group"] = group
    people = [p for p in (creator_email, assignee_email) if p]
    return [rec], {"owner": creator_email, "people": people, "group": group,
                   "confidentiality": None}


def _byo_fireflies(dsid, raw, P):
    path = raw.get("_erb_path") or ""
    channel = path.split("/")[0] if "/" in path else "uncategorized"
    title = str(raw.get(raw.get("title_field_name", "title"), "")).strip()
    internal, external = _ff_attendee_names(raw)
    group = channel
    owner_display = _ff_role_stripped(raw.get("redwood_owner") or "")
    host_email = P.resolve(owner_display, role="owner") if owner_display else None
    internal_emails = [e for e in (P.resolve(n, role="participant_internal") for n in internal) if e]
    external_emails = [e for e in (P.resolve(n, role="participant_external") for n in external) if e]

    # The transcript is parsed HERE — that parse needs the attendee list to gate speakers, and
    # gets it from bench fields the record does not carry — but it is deliberately NOT timed here.
    # `synth.fireflies_fill_times` REWRITES start_time (it spreads a run of sentences sharing one
    # periodic clock reading across its window), so feeding its output back in changes the run
    # structure and the timeline comes out different: the record carries the READINGS as
    # transcribed, and the loader derives the timeline from them exactly as the loader here does.
    # `content` is left out for the same reason — it is *defined* as the sentence concatenation and
    # is rebuilt by the same `synth.fireflies_transcript_text`, so emitting it would double the
    # artifact's largest field and create a second copy that could drift.
    sentences = parse_fireflies_transcript(_ff_transcript_text(raw), internal + external)
    ordinals: dict[str, int] = {}
    for s in sentences:
        ordinals.setdefault(s["speaker_name"] or "", len(ordinals))
    byo_sentences = [
        _rec(text=s["text"], speaker_name=s["speaker_name"],
             speaker_id=ordinals.get(s["speaker_name"] or ""), start_time=s["start_time"],
             # A speaker resolves to an identity only when the label names a DECLARED INTERNAL
             # attendee — an anonymous "Speaker 3" or a customer stays unattributed, as the loader
             # leaves it.
             author_email=(P.resolve(s["speaker_name"], role="participant_internal")
                           if s["speaker_name"] in internal else None))
        for s in sentences]
    duration = _ff_duration(raw.get("duration_minutes"))

    rec = _rec(source_type="fireflies", doc_id=dsid, channel=channel, title=title,
               host_email=host_email, host_name=(owner_display or None),
               calendar_id=raw.get("meeting_id"), calendar_type="google_calendar",
               duration=duration, summary=_ff_summary(raw),
               # `participants` is the attendee roster (internal + external), NOT the set of
               # speakers the loader would fall back to — a customer who never spoke is still a
               # participant — so it has to be stated.
               participants=(internal_emails + external_emails),
               meeting_attendees=_ff_meeting_attendees(raw, internal, external, P),
               sentences=byo_sentences,
               created=(to_epoch(raw.get("recorded_at")) or synth.epoch(dsid)))
    rec["group"] = group
    # `transcript_id`, the three media/web URLs, `meeting_link` and `analytics` are all derived
    # from the doc_id and the sentences by the very same synth functions on load, so they are left
    # out rather than restated — unlike the values above, nothing about them needs a global view.
    return [rec], {"owner": host_email, "people": internal_emails, "group": group,
                   "confidentiality": None}


_BYO_CONVERTERS = {"google_drive": _byo_drive, "github": _byo_github,
                   "confluence": _byo_confluence, "jira": _byo_jira, "gmail": _byo_gmail,
                   "slack": _byo_slack, "hubspot": _byo_hubspot, "linear": _byo_linear,
                   "fireflies": _byo_fireflies}


def to_byo(src: str, dsid: str, raw: dict, P: "Principals", org: str) -> list[dict]:
    """One ERB document -> the BYO record(s) that load to the same rows ``_LOADERS[src]`` writes.

    More than one record when the loader materializes children as first-class documents (a HubSpot
    company plus its notes); a Slack thread's replies and a Gmail thread's later messages instead
    ride along inside the root record, because that is how BYO models a thread.

    Resolves principals through ``P`` in the same order the loader does, so the roster this leaves
    behind — and therefore ``tokens.yaml`` — is the roster a direct import produces.
    """
    records, bundle = _BYO_CONVERTERS[src](dsid, raw, P)
    readers = _byo_readers(src, bundle, org)
    if readers is not None:
        for rec in records:
            rec["readers"] = readers
    return records


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class _ByoWriter:
    """Writes converted records as one plain file, or as per-source gzip shards + a manifest.

    The full corpus is ~788k records / ~1 GB gzipped, which is neither a file a host wants nor one
    a consumer can fetch selectively — so records go to ``data/<source>/part-NNNNN.jsonl.gz`` and a
    caller who wants a single source pulls one folder. The manifest records each shard's record
    count, byte size and SHA-256 so a download can be verified without re-reading the corpus.

    Shards are gzipped with ``mtime=0``: the same input has to produce the same checksums, and
    gzip's default header carries the current time.
    """

    def __init__(self, out_dir: Path, shard_records: int | None):
        self.out_dir = Path(out_dir)
        self.shard_records = shard_records
        self.shards: dict[str, list[dict]] = {}
        self._open: dict[str, tuple] = {}          # source -> (path, fh, records_written)
        self._plain = None
        if shard_records is None:
            self._plain = open(self.out_dir / "corpus.jsonl", "w")

    def write(self, src: str, rec: dict) -> None:
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        if self._plain is not None:
            self._plain.write(line)
            return
        path, fh, n = self._open.get(src) or self._new_shard(src)
        fh.write(line)
        n += 1
        if n >= self.shard_records:
            self._close_shard(src, path, fh, n)
        else:
            self._open[src] = (path, fh, n)

    def _new_shard(self, src: str) -> tuple:
        d = self.out_dir / "data" / src
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"part-{len(self.shards.get(src, [])):05d}.jsonl.gz"
        # GzipFile, not gzip.open: only the former takes `mtime`, and the default header would
        # stamp the current time into every shard and change its digest run to run.
        fh = io.TextIOWrapper(gzip.GzipFile(path, "wb", compresslevel=9, mtime=0),
                              encoding="utf-8")
        self._open[src] = (path, fh, 0)
        return self._open[src]

    def _close_shard(self, src: str, path: Path, fh, n: int) -> None:
        fh.close()
        self._open.pop(src, None)
        self.shards.setdefault(src, []).append({
            "path": str(path.relative_to(self.out_dir)), "records": n,
            "bytes": path.stat().st_size, "sha256": _sha256(path)})

    def close(self, *, counts: dict, documents: int) -> None:
        if self._plain is not None:
            self._plain.close()
            return
        for src, (path, fh, n) in list(self._open.items()):
            self._close_shard(src, path, fh, n)
        roster = self.out_dir / "roster.yaml"
        manifest = {
            "schema": 1,
            "documents": documents,
            "records": sum(s["records"] for v in self.shards.values() for s in v),
            "shard_records": self.shard_records,
            "sources": {src: {"documents": counts.get(src, 0),
                              "records": sum(s["records"] for s in shards),
                              "shards": shards}
                        for src, shards in sorted(self.shards.items())},
        }
        if roster.exists():
            manifest["roster"] = {"path": "roster.yaml", "sha256": _sha256(roster),
                                  "bytes": roster.stat().st_size}
        (self.out_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=1, sort_keys=True) + "\n")


def export_byo(settings, gen_dir, out_dir, *, question_ids=None, shard_records=None) -> dict:
    """Convert ERB to a BYO-JSONL artifact: ``corpus.jsonl`` + ``roster.yaml``.

    The counterpart of :func:`import_structured` — same records, same principal resolution, same
    global precomputation, but written as a corpus ``app.importer.byo`` imports rather than
    straight into a DB. ``tests/test_importer_erb.py`` requires the two to produce equivalent
    databases. With ``shard_records`` set it writes per-source gzip shards plus a ``manifest.json``
    instead of one plain file — what the full 512k-document corpus needs to be distributable.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    P, records = _resolve_roster(settings, gen_dir, question_ids=question_ids)
    # Both global precomputations, before any record is converted — same as load_structured.
    _SLACK_TS_REMAP.clear()
    _SLACK_TS_REMAP.update(build_slack_ts_remap(records))
    _LINEAR_KEY_TO_DOC.clear()
    _LINEAR_KEY_TO_DOC.update(build_linear_key_index(records))
    counts: dict[str, int] = {s: 0 for s in SUPPORTED}
    failures: list[tuple[str, str, str]] = []
    writer = _ByoWriter(out_dir, shard_records)
    for i, (src, dsid, raw) in enumerate(records, 1):
        try:
            for rec in to_byo(src, dsid, raw, P, settings.org_name):
                writer.write(src, rec)
            counts[src] += 1
        except Exception as e:  # one bad doc must not sink the conversion (as in load_structured)
            failures.append((dsid, src, repr(e)))
        if i % 25000 == 0:
            print(f"  converted {i}/{len(records)} ({len(failures)} skipped)",
                  file=sys.stderr, flush=True)
    if failures:
        print(f"  WARNING: skipped {len(failures)} docs. First few: {failures[:5]}",
              file=sys.stderr, flush=True)
    P.write_roster(out_dir / "roster.yaml", settings)
    writer.close(counts=counts, documents=len(records))
    return counts


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
    if counts.get("linear"):
        # Needs every issue present, so it cannot run inside the per-doc loop above.
        rstats = resolve_linear_references(conn, bundles)
        print(f"  linear: resolved {rstats['parents']} parents "
              f"({rstats['parents_dangling']} dangling) and {rstats['relations']} relations "
              f"({rstats['relations_dangling']} dangling)", file=sys.stderr, flush=True)
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
    skipped_empty = 0
    for src, dsid, raw in iter_records(gen_dir / "sources"):
        if question_ids is not None and dsid not in question_ids:
            continue
        # The bench ships one document with no content at all (a slack thread whose `messages` is
        # ""). There is nothing to serve for it, and a thread with zero messages is a state the real
        # API cannot produce — so it is dropped here, where every consumer of the corpus sees the
        # same decision. Dropping it in only one of them is what made a converted artifact fail the
        # BYO schema (`content: '' should be non-empty`) while a direct import accepted it.
        if not (derive_title_content(raw)[1] or "").strip():
            skipped_empty += 1
            continue
        yield src, dsid, raw
    if skipped_empty:
        print(f"  skipped {skipped_empty} document(s) with empty content", file=sys.stderr,
              flush=True)


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
    ap.add_argument("--export-byo", type=Path, default=None, metavar="DIR",
                    help="write a BYO-JSONL artifact (corpus.jsonl + roster.yaml) into DIR instead "
                         "of building the DB; `app.importer.byo` loads it to an equivalent DB")
    ap.add_argument("--shard-records", type=int, default=None, metavar="N",
                    help="with --export-byo: write data/<source>/part-*.jsonl.gz shards of N "
                         "records each plus manifest.json, instead of one corpus.jsonl")
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

    if args.export_byo:
        counts = export_byo(settings, gen_dir, args.export_byo, question_ids=question_ids,
                            shard_records=args.shard_records)
        print(f"Converted {sum(counts.values())} documents -> {args.export_byo}/corpus.jsonl")
        for src, n in counts.items():
            print(f"  {src:14s} {n}")
        print(f"Roster -> {args.export_byo}/roster.yaml "
              f"(org {settings.org_name}, domain {settings.org_domain})")
        print(f"Load it with: python -m app.importer.byo {args.export_byo}/corpus.jsonl "
              f"--roster {args.export_byo}/roster.yaml")
        return 0

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
