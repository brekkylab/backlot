"""Import EnterpriseRAG-Bench (ERB) into the mock DB — the faithful, structured pipeline.

Downloads the bench's ``generated_data/``, resolves display names to real emails via
``Principals``, converts each document to BYO record(s) (``to_byo``) and loads those, deriving
per-doc ACL grants from the real people/scope fields (``grants_for``). Everything the import needs
— fetch, parse, principal resolution, ACL derivation, orchestration — lives in this one module.

    python -m backlot.importer.erb                                   # full corpus: download -> load -> ACL
    python -m backlot.importer.erb --slice-questions extra_questions.jsonl   # only the docs a slice needs
    python -m backlot.importer.erb --no-download                     # reuse whatever is already in data/raw
    python -m backlot.importer.erb --ref some-branch                 # fetch a non-default branch/ref

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

from backlot import store, synth
from backlot.config import get_settings, infer_org
from backlot.importer import byo

# Safe at module level despite byo needing this module back: every one of byo's imports from
# here is function-local, so neither module is half-built when the other is first touched.

# ---------------------------------------------------------------- constants
SUPPORTED = (
    "slack",
    "gmail",
    "google_drive",
    "github",
    "jira",
    "confluence",
    "hubspot",
    "linear",
    "fireflies",
)

INTERNAL_ROLES = {
    "owner",
    "author",
    "reviewer",
    "assignee",
    "reporter",
    "collaborator",
    "participant_internal",
    "mailbox_owner",
}
EXTERNAL_ROLES = {"participant_external"}
SLACK_ROLE = "slack_participant"
EXTERNAL_DOMAIN = "external.example"  # placeholder when no counterparty domain is known
_NAME_EMAIL = re.compile(r"([^<>\n,:]+?)\s*<([^>@\s]+@[^>\s]+)>")

_HDR = re.compile(r"^(From|To|Cc|Bcc|Reply-To|Date|Subject|Message-ID):\s*(.*)$")
# US timezone abbreviations the bench uses in some gmail Date headers -> fixed UTC offset (hours).
# DST-labeled variants carry their own offset; bare PT/ET/CT/MT default to standard time.
_TZ = {
    "UTC": 0,
    "GMT": 0,
    "Z": 0,
    "EST": -5,
    "EDT": -4,
    "ET": -5,
    "CST": -6,
    "CDT": -5,
    "CT": -6,
    "MST": -7,
    "MDT": -6,
    "MT": -7,
    "PST": -8,
    "PDT": -7,
    "PT": -8,
}
_ADDR = re.compile(r"<([^>@\s]+@[^>\s]+)>")
_JIRA = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<name>[^:]+?):\s*(?P<body>.*)$", re.DOTALL)
# Slack speaker: 1–3 name-ish words / handles ("Alex", "ops-bot", "Maria L", "IT Help"), an
# optional "(Team)"/"(Role)" label some docs append ("Elena (CFO)", "Asha (FinanceOps)"), then
# ": ". The parenthetical is dropped so only the bare name resolves against the directory.
_SPEAKER = re.compile(
    r"^@?(?P<name>[A-Za-z][\w.'\-]*(?: [A-Za-z0-9][\w.'\-]*){0,2})(?: *\([^)]*\))?: (?P<text>\S.*)$"
)


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
_NON_PERSON_WORDS = {
    "team",
    "teams",
    "group",
    "groups",
    "all",
    "everyone",
    "folks",
    "redacted",
    "unknown",
    "na",
    "tbd",
    "via",
    "support",
    "bot",
    "customer",
    "vendor",
    "channel",
    "oncall",
    "rotation",
    "admin",
    "system",
    "service",
}


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

    The bench names people by display string, inconsistently across sources ("Connor O'Brien" vs
    "Connor OBrien"), and only Gmail headers reveal real addresses. Builds one canonical identity
    per person: match the employee directory, harvest emails from Gmail, synthesize a user for an
    unmatched internal reference. Slack first-names and bots are best-effort.
    """

    def __init__(self, employees: list[dict], org_domain: str):
        self.org_domain = org_domain
        self.users: dict[str, dict] = {}  # email -> {name, group, directory?}
        self.groups: set[str] = set()
        self._by_canon: dict[str, str] = {}  # canonical name -> email
        for e in employees:
            self._by_canon[canonical(e["name"])] = e["email"]
            self.users[e["email"]] = {
                "name": e["name"],
                "group": e.get("dept_slug"),
                "directory": True,
            }
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
            for p in people or []:
                emps.append({"name": p["name"], "email": p["email"], "dept_slug": slugify(dept)})
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
                    if (
                        c
                        and _person_like(name)
                        and email.endswith("@" + self.org_domain)
                        and c not in self._by_canon
                    ):
                        self._by_canon[c] = email
                        self.users[email] = {"name": name, "group": None}

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
            u = self.users.setdefault(email, {"name": name, "group": None})
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
        self.users.setdefault(email, {"name": name, "group": group})
        if group:
            self.groups.add(group)
        return self._by_canon[c]

    def display_email(self, name: str) -> tuple[str | None, str]:
        c = canonical(name or "")
        return self._by_canon.get(c), (name or "")

    def install(self, conn, settings) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO principals(id,type,display_name,email) VALUES (?,?,?,?)",
            (settings.org_name, "org", settings.org_name, None),
        )
        for g in sorted(self.groups):
            conn.execute(
                "INSERT OR REPLACE INTO principals(id,type,display_name,email) VALUES (?,?,?,?)",
                (g, "group", g, None),
            )
        for email, u in self.users.items():
            conn.execute(
                "INSERT OR REPLACE INTO principals(id,type,display_name,email) VALUES (?,?,?,?)",
                (email, "user", u["name"], email),
            )
            if u["group"]:
                conn.execute(
                    "INSERT OR REPLACE INTO group_members(group_id,user_id) VALUES (?,?)",
                    (u["group"], email),
                )

    def write_tokens(self, settings) -> None:
        # Only the employee directory are authenticating org users (realistic roster). Everyone
        # else the corpus references is display-only: they still appear as owners/authors/grantees
        # on documents (name derived from their email), but get no bearer token / /_mock/users entry.
        users = [
            {"email": e, "name": u["name"], "token": _user_token(e)}
            for e, u in self.users.items()
            if u.get("directory")
        ]
        settings.tokens_path.write_text(
            yaml.safe_dump(
                {
                    "org": settings.org_name,
                    "org_domain": settings.org_domain,
                    "admin_token": settings.admin_token,
                    "users": users,
                },
                sort_keys=False,
            )
        )

    def write_roster(self, path, settings) -> None:
        """Write the resolved roster as a BYO roster sidecar (see ``byo.load_roster``).

        Has to ship WITH a converted corpus, because the records cannot reconstruct it: ``_slug`` is
        lossy so a display name is unrecoverable from an address, and only the employee directory
        may authenticate — derived from the corpus alone, every Slack handle and outside sender
        would become an org account with a working token. ``departments`` is the authenticating
        users keyed by group, ``contacts`` everyone else, the same split ``install`` makes.
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
        Path(path).write_text(
            yaml.safe_dump(
                {
                    "org": settings.org_name,
                    "org_domain": settings.org_domain,
                    "departments": depts,
                    "contacts": contacts,
                },
                sort_keys=False,
                allow_unicode=True,
            )
        )


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
        # The scope grant above was a NO-OP — the container has no group (a Drive file with no `team`).
        # Fall back to the org rather than leave the doc invisible to every non-admin.
        # if/else, NOT `add(...) or add(...)`: `add` returns None, so the `or` grants both.
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


def iter_records(
    sources_dir: Path, sources: tuple[str, ...] = SUPPORTED
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


SNAPSHOT_FILE = ".erb-source.json"


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
                rest == p or rest.startswith(p + "/") for p in keep_sources
            )
            if not keep:
                continue
            dest = out / rest
            if m.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(m) as fsrc:
                    dest.write_bytes(fsrc.read())
    # Which bench this is. A ref name will not do it: `main` has moved past the commit that added
    # generated_data, and the one tag (v1.0.0) predates that commit, so neither pins the data. The
    # tarball's digest does, and it is recorded here because this is the only moment the bytes that
    # produced this directory are still identifiable.
    (out / SNAPSHOT_FILE).write_text(
        json.dumps(
            {
                "repo": repo,
                "ref": ref,
                "tarball_sha256": _sha256(tar_path),
                "tarball_bytes": tar_path.stat().st_size,
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    return out


def read_snapshot(gen_dir) -> dict | None:
    """What ``fetch_generated_data`` recorded about the tarball this directory came from, if any.
    Absent for a tree assembled by hand, so a caller treats it as unknown rather than a mismatch."""
    path = Path(gen_dir) / SNAPSHOT_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


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
        out.append(
            {
                "from_name": _name(hdrs.get("From")),
                "from_email": _addr(hdrs.get("From")),
                "to": hdrs.get("To"),
                "cc": hdrs.get("Cc"),
                "date": hdrs.get("Date"),
                "subject": hdrs.get("Subject"),
                "message_id": hdrs.get("Message-ID"),
                "body": "\n".join(lines[body_start:]).strip(),
            }
        )
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
    "csv": "text/csv",
    "txt": "text/plain",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "zip": "application/zip",
    "json": "application/json",
}


# The bench's Drive ``doc_type`` vocabulary -> the mock's Drive subtype vocabulary (the keys
# ``backlot.routers.google._NATIVE`` recognises as Workspace types). The bench says "doc"/"sheet"/
# "slides", none of which are native keys — unmapped, every row falls back to
# ``application/octet-stream`` and the binary ``webViewLink`` shape, leaving nothing in the corpus
# that exercises native-vs-binary handling, ``export`` vs ``alt=media``, or per-type links.
_DRIVE_SUBTYPE = {
    "doc": "document",
    "document": "document",
    "gdoc": "document",
    "notes": "document",
    "memo": "document",
    "sheet": "spreadsheet",
    "spreadsheet": "spreadsheet",
    "gsheet": "spreadsheet",
    "slides": "presentation",
    "slide": "presentation",
    "deck": "presentation",
    "presentation": "presentation",
    "gslides": "presentation",
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
    ext = (
        key if key in _ATT_MIME else (_ext(title) or _ext(raw.get("path") or raw.get("file_path")))
    )
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
            entry = {
                "filename": name,
                "mime": a.get("mime") or a.get("mimeType"),
                "size": a.get("size"),
            }
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
            out.append(
                {
                    "date": m.group("date"),
                    "name": m.group("name").strip(),
                    "body": m.group("body").strip(),
                }
            )
    return out


def _canon_speaker(s: str) -> str:
    """Canonicalize a speaker/participant name for matching: drop a trailing team label and any
    non-alphanumerics. 'ben.jones (Acme)' / 'Ben Jones' -> 'benjones'; 'api-monitor-bot' ->
    'apimonitorbot'."""
    s = re.sub(r"\s*\([^)]*\)", "", str(s))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_slack_transcript(
    messages: str, participants: list | None = None
) -> list[tuple[str, str]]:
    """Slack ``messages`` is ONE concatenated ``Speaker: text`` transcript. When ``participants`` is
    given, a line only starts a NEW turn if its speaker matches a known participant; otherwise it's
    body text of the current turn. This stops sentence fragments / section headers ("A couple
    followups:", "What I did:") from being mis-parsed as speakers and minting fake authors."""
    # canon -> the participant's clean display name (team label stripped); used both to gate turns
    # and to normalize the speaker to the participant's canonical identity, so transcript variants
    # ("a lex", "Ana Customs") collapse onto the real participant ("alex", "ana_customs") instead of
    # minting variant-duplicate authors.
    pmap: dict[str, str] = {}
    for p in participants or []:
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
        core = s[: mnum.start()]
    elif mabbr and mabbr.group(1) in _TZ:
        off = _dt.timedelta(hours=_TZ[mabbr.group(1)])
        core = s[: mabbr.start()]
    else:
        core = s
    core = re.sub(r"^[A-Za-z]{3},\s*", "", core.strip()).replace(" at ", " ")
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d",
        "%b %d, %Y %I:%M %p",
        "%b %d, %Y %H:%M",
        "%b %d, %Y",
    ):
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

    ``P.resolve`` returns None for a reference that is not a usable identity (a team label, a prose
    fragment), and such a name must not hold a slot in a list of principals — a null there is not a
    person with an unknown address. It also breaks serving: ``requested_reviewers`` is rendered per
    entry into a GitHub Simple User, so a null 500s the pull-request endpoint."""
    return [e for e in (P.resolve(n, role=role) for n in _names(values)) if e]


def _title_content(raw):
    return derive_title_content(raw)


def _slug_mailbox(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


# The bench's HubSpot docs are denormalized company (account) records — there are no contact/deal
# objects in the corpus. These are the fields with a real HubSpot company property to map onto;
# everything else becomes a custom property, which is what an actual portal looks like (a mix of
# HubSpot defaults and portal-specific fields). We map the bench onto the mock's API-shaped schema
# rather than storing ERB's shape, exactly as the drive/github converters do for their sources.
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
_HS_NOT_A_PROPERTY = {
    "title_field_name",
    "content_field_names",
    "dataset_doc_uuid",
    "created_at",
    "updated_at",
    "owner",
    "notes",
    "crm_notes",
    "_erb_path",
}  # injected by iter_records, not corpus data


def _hs_notes(raw) -> list[str]:
    """The bench's CRM notes: usually a list of undated fragments, sometimes a single string.
    (`timeline` is a *dated activity log* the bench lists in content_field_names — it is the
    company's own body text, not a set of note objects, so it is deliberately not included.)"""
    for key in ("notes", "crm_notes"):
        v = raw.get(key)
        if isinstance(v, list):
            out = [str(n) for n in v if str(n).strip()]
            if out:  # an empty list must not mask a populated `crm_notes`
                return out
        elif isinstance(v, str) and v.strip():
            return [v]
    return []


# ~35% of Slack `first_message_ts` values are the bench's opaque far-future "ordering keys", not
# calendar dates — valid ts up to year 2286. Served verbatim they render absurdly and blow up
# mirage's per-day FS layout. Remap ONLY the out-of-range roots (year > 2035), order-preserving,
# into a compact window continuing the real timeline just after the newest in-range thread;
# in-range values stay untouched so the realistic majority keeps its cross-source coherence (a
# Slack thread and the Jira ticket it cites stay aligned). Slack-only.
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
    return {
        dsid: start + (rank * _SLACK_TS_REMAP_SPAN // max(1, n - 1))
        for rank, (ts, dsid) in enumerate(future)
    }


# ---------------------------------------------------------------- linear
# One ticket per file. Two properties of the real data drive the mapping:
#   * `key` is NOT unique (one repeats 107 times), so the doc_id stays the dataset uuid and the
#     key becomes `identifier` — which our corpus therefore does not treat as unique.
#   * the directory a file sits in disagrees with its own `team` field for ~2,750 docs, and two
#     directories name no team at all. The `team` FIELD is the authority: its values line up with
#     the ENG/PM/DES identifier prefixes and each maps onto a real directory department, so the
#     ACL group actually has members.

# The bench writes P0-P3; Linear's API has a 0-4 integer scale with 1 the most urgent. Map onto
# the API's scale (as the hubspot converter maps onto real HubSpot property names) rather than serving a
# vocabulary no Linear client understands. Labels are accepted too, for a BYO corpus that already
# speaks Linear.
_LINEAR_PRIORITY = {
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
_LINEAR_REL_WORDS = (
    ("duplicate", "duplicate"),
    ("blocked by", "blocks"),
    ("blocks", "blocks"),
    ("depends", "blocks"),
    ("related", "related"),
)
_LINEAR_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")


def parse_linear_relations(value) -> list[tuple[str, str]]:
    """A bench `dependencies` entry -> ``[(type, key), …]``.

    Defaults to ``related``, which is Linear's own neutral relation, rather than guessing
    ``blocks``: the corpus lists these under a heading that means "depends on" only sometimes, and
    asserting a blocking relationship the data does not state would be inventing a dependency
    graph. A word that IS present is honoured."""
    out = []
    for entry in value if isinstance(value, (list, tuple)) else [value]:
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
        for entry in value if isinstance(value, (list, tuple)) else [value]:
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


# A bench Linear comment is a plain string, and the date and author come in interchangeable shapes:
#     2025-02-18 - Maya Patel: filed the PRD      (dash + name — the most common)
#     2025-02-18 - Created: initial hypothesis    (dash + a LABEL, not a person)
#     2026-03-05 Anjali Rao: updated the criteria (no dash, name)
#     2025-12-18 (Naomi Feldman): include audit   (parenthesised name)
#     2025-02-18 09:15: rolled back               (no name at all — that is a clock)
#     Implementation notes: use heuristics        (undated)
# Hence two independent steps, NOT a list of whole-line alternatives: peel the date (with its
# optional dash), then try to peel a `Name:` off the remainder. Ordered whole-line patterns
# silently swallow the author into the body whenever an earlier one lacks a name group.
_LINEAR_C_DATE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\s*(?:[-–—]\s*)?(?P<rest>.*)$", re.DOTALL)
# The name must START WITH A LETTER. Without that, "2025-02-18 09:15: rolled back" parses as
# author "09" with the body truncated to "15: rolled back" — inventing a person and losing text.
_LINEAR_C_NAME = re.compile(
    r"^\(?(?P<name>[A-Za-zÀ-ÿ][^:\n()]{0,39}?)\)?:\s*(?P<body>.*)$", re.DOTALL
)


def parse_linear_comments(comments) -> list[dict]:
    """Bench comment strings -> ``{date, name, body, body_with_name}``.

    Both bodies, because only the caller knows whether the ``Name:`` prefix is a person or a LABEL
    ("Created:", "Design review with PM and Accessibility:") whose removal would delete text.
    :func:`_byo_linear` takes ``body`` when the name resolves to somebody and ``body_with_name``
    when it does not.
    """
    if isinstance(comments, str):  # 29 docs carry a single string instead of a list
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
            out.append(
                {
                    "date": date,
                    "name": n.group("name").strip(),
                    "body": n.group("body").strip(),
                    "body_with_name": rest.strip(),
                }
            )
        else:
            out.append(
                {"date": date, "name": None, "body": rest.strip(), "body_with_name": rest.strip()}
            )
    return out


# ---------------------------------------------------------------- fireflies
# One meeting transcript per file. Four properties of the real data drive the mapping:
#   * the transcript is ONE FLAT TEXT BLOB, so the per-sentence rows the API serves are PARSED
#     from it here and `synth.fireflies_transcript_text` is the exact inverse. Only start times
#     are in the data; end times are derived (synth.fireflies_fill_times).
#   * the blob uses six interchangeable line formats ("[00:00] Name:", "00:00 - Name:",
#     "00:00 [Name]:", "(00:00) Name:", "[S00:12] Name (Role):", bare "Name:") and some docs open
#     with an auto-notes preamble whose "Date:"/"Duration:" lines look exactly like speaker lines.
#     Hence one recognizer for all six, plus participant gating.
#   * NO email addresses appear anywhere, so identities resolve through `Principals`.
#   * `meeting_id` is not unique, so it becomes `calendar_id` and the API's `id` is synthesized.

_FF_CLOCK = r"\d{1,2}:\d{2}(?::\d{2})?"
# A leading timestamp in any form the bench writes, optionally followed by a "-"/"–" separator.
# The optional letter inside the brackets absorbs a quirk the corpus contains ("[S00:12]").
_FF_TS = (
    rf"(?:\[[A-Za-z]?(?P<b>{_FF_CLOCK})\]|\((?P<p>{_FF_CLOCK})\)|(?P<r>{_FF_CLOCK}))"
    r"\s*(?:[-–—]\s*)?"
)
# 1-4 name-ish words, optionally bracketed ("[Maya]"), with a trailing "(Role)" / ", Role" that
# is stripped before matching ("Ari (Redwood AE)", "Mark, Sentinel CISO").
_FF_WHO = r"[A-Za-z@][\w.'’\-]*(?: +[A-Za-z0-9][\w.'’\-]*){0,3}"
_FF_UTT = re.compile(
    rf"^\s*(?:{_FF_TS})?(?:\[(?P<name>{_FF_WHO})\]|(?P<name2>{_FF_WHO}))"
    rf"(?: *[(,][^)]*\)?)?:[ \t]*(?P<text>.*)$"
)

# Fireflies' own auto-notes header labels. Each looks exactly like a speaker line
# ("Date: 2025-02-20", "Duration: ~52 minutes"), so none may ever mint a speaker.
_FF_NOT_SPEAKER = {
    "date",
    "duration",
    "attendees",
    "attendees present",
    "participants",
    "header",
    "meeting header",
    "meeting",
    "meeting date",
    "meeting title",
    "meeting start",
    "title",
    "time",
    "location",
    "host",
    "organizer",
    "recorded",
    "recording",
    "meeting recording",
    "summary",
    "auto-summary",
    "summary (auto)",
    "auto-generated summary",
    "human summary",
    "topics",
    "topics covered",
    "transcript",
    "transcript body",
    "action items",
    "next steps",
    "questions",
    "notes",
    "notes on transcription",
    "agenda",
    "call type",
    "start",
    "end",
    "note",
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
    return float(
        parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]
    )


def parse_fireflies_transcript(text, attendees: list | None = None) -> list[dict]:
    """A flat Fireflies transcript blob -> ``[{speaker_name, text, start_time}]``.

    Mirrors :func:`parse_slack_transcript`: a line starts a NEW sentence only when its speaker is a
    declared attendee, so the auto-notes preamble and mid-transcript prose stay continuation text
    instead of minting fake speakers. Falls back to ungated splitting when it recognizes nobody at
    all — the corpus deliberately contains transcripts labelled only "Speaker 1"/"Speaker 2".
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
        return [
            {"speaker_name": s, "text": "\n".join(ls).rstrip(), "start_time": t} for s, ls, t in out
        ]

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
_FF_BODY_KEYS = (
    "transcript",
    "transcript_text",
    "transcript_body",
    "full_transcript",
    "meeting_transcript",
    "Transcript",
    "transcript_full",
    "detailed_transcript",
    "transcription",
    "full_transcript_body",
    "transcript_final",
    "body_transcript",
    "body",
    "content",
)
_FF_BODY_MORE = (
    "transcript_continued",
    "transcript_continued_2",
    "transcript_continued_3",
    "additional_transcript",
    "additional_transcript_part2",
    "continued_transcript",
    "tail_transcript",
)


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


def _ff_meeting_attendees(raw, internal: list[str], external: list[str], P) -> list[dict]:
    """The API's `meeting_attendees` — {displayName, email, phoneNumber, name, location}. The bench
    names people without emails, so each is resolved the way its side allows: Redwood attendees to
    org identities, customer attendees to external contacts."""
    out = []
    for name, role in [(n, "participant_internal") for n in internal] + [
        (n, "participant_external") for n in external
    ]:
        out.append(
            {
                "displayName": name,
                "email": P.resolve(name, role=role),
                "phoneNumber": None,
                "name": name,
                "location": raw.get("customer_company") if role in EXTERNAL_ROLES else None,
            }
        )
    return out


# ---------------------------------------------------------------- ERB -> BYO records
# THE mapping, one function per source. Both consumers go through here — `import_structured`
# hands the records to the loader, `export_byo` writes the same ones to JSONL — so a mapping
# decision is made exactly once and a direct import cannot disagree with the artifact.
#
# Three things a single record cannot recompute for itself, so they are baked into its values
# (see `_precompute_globals` for the two that need a view of the whole corpus):
#   * resolved principal emails, which ship beside the corpus as a roster
#   * the Slack far-future timestamp remap, which is rank-based over every thread
#   * identifier -> doc_id for a Linear parent/relation, since bench keys repeat
_LINEAR_KEY_TO_DOC: dict[str, str] = {}


def build_linear_key_index(records) -> dict[str, str]:
    """identifier -> doc_id, FIRST match by doc_id — the rule
    ``byo._Loader.resolve_cross_references`` and ``store.linear_issue_by_identifier`` also apply.
    Bench keys are not unique, which is why this is resolved once here and never by a serve-time
    join."""
    out: dict[str, str] = {}
    for _src, dsid, raw in sorted((r for r in records if r[0] == "linear"), key=lambda r: r[1]):
        identifier = str(raw.get("key") or "").strip() or synth.linear_identifier(
            dsid, synth.linear_team_key(str(raw.get("team") or "engineering"))
        )
        out.setdefault(identifier, dsid)
    return out


def _precompute_globals(records) -> None:
    """The two things a single record cannot compute for itself, both needed before the first
    conversion: the Slack far-future timestamp remap (rank-based over every thread) and the
    Linear identifier -> doc_id index (bench keys repeat, so a parent or relation has to be
    resolved once, here, rather than by a serve-time join)."""
    _SLACK_TS_REMAP.clear()
    _SLACK_TS_REMAP.update(build_slack_ts_remap(records))
    _LINEAR_KEY_TO_DOC.clear()
    _LINEAR_KEY_TO_DOC.update(build_linear_key_index(records))


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
    rec = _rec(
        source_type="confluence",
        doc_id=dsid,
        space=space,
        title=title,
        content=content,
        author_email=author_email,
        author_name=author,
        subtype="page",
        labels=_names(raw.get("labels")),
        reviewers=reviewers,
        confidentiality=raw.get("confidentiality"),
        owner_team=raw.get("owner_team"),
        created=(to_epoch(raw.get("created_at")) or synth.epoch(dsid)),
        updated=to_epoch(raw.get("last_updated")),
    )
    rec["group"] = group
    return [rec], {
        "owner": author_email,
        "people": reviewers,
        "group": group,
        "confidentiality": raw.get("confidentiality"),
    }


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
    rec = _rec(
        source_type="google_drive",
        doc_id=dsid,
        folder=(raw.get("drive_area") or group or "drive"),
        title=title,
        content=content,
        author_email=owner_email,
        author_name=owner,
        subtype=subtype,
        mime_type=mime_type,
        collaborators=collabs,
        created=(to_epoch(raw.get("created_at")) or synth.epoch(dsid)),
        updated=to_epoch(raw.get("last_modified")),
    )
    # A doc with no team owns no group, and `"group": null` is how BYO says so — inferring one from
    # the folder name would invent a grantable principal the direct import does not have.
    rec["group"] = group
    return [rec], {"owner": owner_email, "people": collabs, "group": group, "confidentiality": None}


def _byo_github(dsid, raw, P):
    title, content = _title_content(raw)
    author = raw.get("author", "")
    author_email = P.resolve(author, role="author", group_hint=raw.get("repo")) if author else None
    reviewers = _resolved(P, raw.get("reviewers"), role="reviewer")
    repo = raw.get("repo") or "repo"
    rec = _rec(
        source_type="github",
        doc_id=dsid,
        repo=repo,
        title=title,
        content=content,
        author_email=author_email,
        author_name=author,
        subtype=("pull_request" if raw.get("pr_number") else "issue"),
        state=raw.get("state"),
        labels=_names(raw.get("labels")),
        requested_reviewers=reviewers,
        created=(to_epoch(raw.get("created_at")) or synth.epoch(dsid)),
        updated=to_epoch(raw.get("updated_at")),
    )
    rec["group"] = repo
    return [rec], {
        "owner": author_email,
        "people": reviewers,
        "group": repo,
        "confidentiality": None,
    }


def _byo_jira(dsid, raw, P):
    title, content = _title_content(raw)
    reporter = raw.get("reporter", "")
    assignee = raw.get("assignee", "")
    group = P.canonical_group(raw.get("squad")) or (raw.get("project") or "JIRA")
    reporter_email = P.resolve(reporter, role="reporter", group_hint=group) if reporter else None
    assignee_email = P.resolve(assignee, role="assignee", group_hint=group) if assignee else None
    project = raw.get("project") or "JIRA"
    comments = [
        _rec(
            id=f"{dsid}::c{seq}",
            content=c["body"],
            author_email=P.resolve(c["name"], role="author"),
            created_ts=to_epoch(c["date"]),
        )
        for seq, c in enumerate(parse_jira_comments(raw.get("comments", [])), start=1)
    ]
    rec = _rec(
        source_type="jira",
        doc_id=dsid,
        project=project,
        title=title,
        content=content,
        author_email=reporter_email,
        author_name=reporter,
        status=raw.get("status"),
        issuetype=raw.get("issue_type"),
        priority=raw.get("priority"),
        labels=_names(raw.get("labels")),
        components=_names(raw.get("components")),
        assignee=assignee_email,
        reporter=reporter_email,
        severity=raw.get("severity"),
        squad=raw.get("squad"),
        duedate=raw.get("due_date"),
        comments=(comments or None),
        created=(to_epoch(raw.get("created_at")) or synth.epoch(dsid)),
        updated=to_epoch(raw.get("updated_at")),
    )
    rec["group"] = group
    people = [p for p in (assignee_email, reporter_email) if p]
    return [rec], {
        "owner": reporter_email,
        "people": people,
        "group": group,
        "confidentiality": None,
    }


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
        _rec(
            doc_id=f"{dsid}::m{seq}",
            content=m.get("body", ""),
            author_email=m.get("from_email"),
            title=(m.get("subject") or title),
            to=m.get("to"),
            cc=m.get("cc"),
            message_id=m.get("message_id"),
            created=(to_epoch(m.get("date")) or (root_ts + seq * 3600)),
        )
        for seq, m in enumerate(msgs[1:], start=1)
    ]
    rec = _rec(
        source_type="gmail",
        doc_id=dsid,
        mailbox=mailbox,
        title=(title or root.get("subject") or ""),
        content=(root.get("body") or (content if not msgs else "")),
        author_email=(root.get("from_email") or owner_email),
        mailbox_owner=owner_name,
        thread=dsid,
        to=root.get("to"),
        cc=root.get("cc"),
        message_id=root.get("message_id"),
        attachments=(attachments or None),
        messages=(messages or None),
        created=root_ts,
    )
    # A mailbox has no ACL group: a thread is private to its participants, which `readers` states
    # per document (see grants_for).
    rec["group"] = None
    people = [p for p in (owner_email, *internal) if p]
    return [rec], {"owner": owner_email, "people": people, "group": None, "confidentiality": None}


def _byo_slack(dsid, raw, P):
    channel = raw.get("channel") or "general"
    _title, content = _title_content(raw)
    participants = _names(raw.get("participants"))
    turns = parse_slack_transcript(content, participants)
    root_author = P.resolve(turns[0][0], role="slack_participant") if turns else None
    root_ts = (
        _SLACK_TS_REMAP.get(dsid) or to_epoch(raw.get("first_message_ts")) or synth.epoch(dsid)
    )
    replies = [
        _rec(
            doc_id=f"{dsid}::m{seq}",
            content=text,
            author_email=P.resolve(spk, role="slack_participant"),
        )
        for seq, (spk, text) in enumerate(turns[1:], start=1)
    ]
    rec = _rec(
        source_type="slack",
        doc_id=dsid,
        channel=channel,
        content=(turns[0][1] if turns else content),
        author_email=root_author,
        participants=participants,
        replies=(replies or None),
        # The remapped value, NOT the source `first_message_ts`: the remap is rank-based over
        # every slack thread, so it cannot be recomputed from this record.
        created=root_ts,
    )
    rec["group"] = channel
    # Slack speakers are display labels rather than org identities, so the doc is org-visible and
    # `people` stays empty — same as the loader's bundle.
    return [rec], {"owner": root_author, "people": [], "group": channel, "confidentiality": None}


def _byo_hubspot(dsid, raw, P):
    title, content = _title_content(raw)
    object_type = group = "companies"
    owner = raw.get("owner", "")
    owner_email = P.resolve(owner, role="owner", group_hint=group) if owner else None
    people = [
        P.resolve(n, role="collaborator")
        for n in (raw.get("se_assigned"), raw.get("csm_assigned"))
        if n
    ]
    props = {_HS_PROPERTY.get(k, k): v for k, v in raw.items() if k not in _HS_NOT_A_PROPERTY}
    created = to_epoch(raw.get("created_at")) or synth.epoch(dsid)
    # A note is its own CRM object associated with the company, so it converts to its own record —
    # which is exactly how a BYO author would write one.
    notes, links = [], []
    for i, body in enumerate(_hs_notes(raw), start=1):
        note_id = f"{dsid}::n{i}"
        links.append({"to": note_id, "to_type": "notes"})
        note = _rec(
            source_type="hubspot",
            doc_id=note_id,
            object_type="notes",
            title="",
            content=body,
            author_email=owner_email,
            properties={"hs_note_body": body, "hs_timestamp": synth.rfc3339(created + i)},
            created=created + i,
        )
        # The notes object type hangs off the COMPANY's group, not off its own name.
        note["group"] = group
        notes.append(note)
    rec = _rec(
        source_type="hubspot",
        doc_id=dsid,
        object_type=object_type,
        title=title,
        content=content,
        author_email=owner_email,
        author_name=owner,
        properties=props,
        associations=(links or None),
        created=created,
        updated=to_epoch(raw.get("updated_at")),
    )
    rec["group"] = group
    return [rec, *notes], {
        "owner": owner_email,
        "people": [p for p in people if p],
        "group": group,
        "confidentiality": None,
    }


def _byo_linear(dsid, raw, P):
    title, content = _title_content(raw)
    team = str(raw.get("team") or "engineering")
    group = P.canonical_group(team) or team
    creator = raw.get("creator", "")
    assignee = raw.get("assignee", "")
    creator_email = P.resolve(creator, role="author", group_hint=group) if creator else None
    assignee_name = assignee if str(assignee).strip().lower() != "unassigned" else ""
    assignee_email = (
        P.resolve(assignee_name, role="assignee", group_hint=group) if assignee_name else None
    )
    identifier = str(raw.get("key") or "").strip() or synth.linear_identifier(
        dsid, synth.linear_team_key(team)
    )
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
        comments.append(
            _rec(
                id=f"{dsid}::c{seq}",
                content=(c["body"] if author else c["body_with_name"]),
                author_email=author,
                created_ts=ts,
            )
        )
    # A relation names its target by doc_id in BYO, so the bench's issue KEY is resolved here —
    # dropping a dangling, self- or duplicate reference, and keeping the original position in the
    # id so a re-conversion of the same corpus agrees row for row.
    relations, seen = [], set()
    for seq, (rel_type, rel_key) in enumerate(
        parse_linear_relations(raw.get("dependencies")), start=1
    ):
        target = _LINEAR_KEY_TO_DOC.get(rel_key)
        if not target or target == dsid or (rel_type, target) in seen:
            continue
        seen.add((rel_type, target))
        relations.append({"id": f"{dsid}::r{seq}", "to": target, "type": rel_type})

    rec = _rec(
        source_type="linear",
        doc_id=dsid,
        team=team,
        title=title,
        content=content,
        author_email=creator_email,
        author_name=creator,
        identifier=identifier,
        state=state,
        priority=linear_priority(raw.get("priority")),
        estimate=_linear_int(raw.get("estimate")),
        labels=_names(raw.get("labels")),
        project=raw.get("project"),
        cycle=raw.get("cycle"),
        branchName=synth.linear_branch_name(identifier, title, assignee_email),
        dueDate=_linear_date(raw.get("due_date")),
        assignee=assignee_email,
        assigneeName=(assignee_name or None),
        # The parent's own identifier, as the corpus wrote it — BYO resolves it to a doc_id
        # on load with the same first-match rule.
        parent=_linear_parent(raw.get("parent_issue")),
        release=_linear_release(raw.get("release")),
        completedAt=(ended if state_type == "completed" else None),
        canceledAt=(ended if state_type == "canceled" else None),
        startedAt=(created if state_type in ("started", "completed") else None),
        attachments=(parse_linear_attachments(raw.get("links"), raw.get("attachments")) or None),
        comments=(comments or None),
        relations=(relations or None),
        created=created,
        updated=updated,
    )
    rec["group"] = group
    people = [p for p in (creator_email, assignee_email) if p]
    return [rec], {
        "owner": creator_email,
        "people": people,
        "group": group,
        "confidentiality": None,
    }


def _byo_fireflies(dsid, raw, P):
    path = raw.get("_erb_path") or ""
    channel = path.split("/")[0] if "/" in path else "uncategorized"
    title = str(raw.get(raw.get("title_field_name", "title"), "")).strip()
    internal, external = _ff_attendee_names(raw)
    group = channel
    owner_display = _ff_role_stripped(raw.get("redwood_owner") or "")
    host_email = P.resolve(owner_display, role="owner") if owner_display else None
    internal_emails = [
        e for e in (P.resolve(n, role="participant_internal") for n in internal) if e
    ]
    external_emails = [
        e for e in (P.resolve(n, role="participant_external") for n in external) if e
    ]

    # Parsed HERE (the parse needs attendee fields the record does not carry) but deliberately NOT
    # timed here: `synth.fireflies_fill_times` REWRITES start_time, spreading a run of sentences that
    # share one clock reading across its window, so feeding its output back in would change the run
    # structure and produce a different timeline. The record carries the readings as transcribed.
    # `content` is omitted for the same reason — it is DEFINED as the sentence concatenation, so
    # emitting it would double the artifact's largest field and could drift.
    sentences = parse_fireflies_transcript(_ff_transcript_text(raw), internal + external)
    ordinals: dict[str, int] = {}
    for s in sentences:
        ordinals.setdefault(s["speaker_name"] or "", len(ordinals))
    byo_sentences = [
        _rec(
            text=s["text"],
            speaker_name=s["speaker_name"],
            speaker_id=ordinals.get(s["speaker_name"] or ""),
            start_time=s["start_time"],
            # A speaker resolves to an identity only when the label names a DECLARED INTERNAL
            # attendee — an anonymous "Speaker 3" or a customer stays unattributed, as the loader
            # leaves it.
            author_email=(
                P.resolve(s["speaker_name"], role="participant_internal")
                if s["speaker_name"] in internal
                else None
            ),
        )
        for s in sentences
    ]
    duration = _ff_duration(raw.get("duration_minutes"))

    rec = _rec(
        source_type="fireflies",
        doc_id=dsid,
        channel=channel,
        title=title,
        host_email=host_email,
        host_name=(owner_display or None),
        calendar_id=raw.get("meeting_id"),
        calendar_type="google_calendar",
        duration=duration,
        summary=_ff_summary(raw),
        # `participants` is the attendee roster (internal + external), NOT the set of
        # speakers the loader would fall back to — a customer who never spoke is still a
        # participant — so it has to be stated.
        participants=(internal_emails + external_emails),
        meeting_attendees=_ff_meeting_attendees(raw, internal, external, P),
        sentences=byo_sentences,
        created=(to_epoch(raw.get("recorded_at")) or synth.epoch(dsid)),
    )
    rec["group"] = group
    # `transcript_id`, the three media/web URLs, `meeting_link` and `analytics` are all derived
    # from the doc_id and the sentences by the very same synth functions on load, so they are left
    # out rather than restated — unlike the values above, nothing about them needs a global view.
    return [rec], {
        "owner": host_email,
        "people": internal_emails,
        "group": group,
        "confidentiality": None,
    }


_BYO_CONVERTERS = {
    "google_drive": _byo_drive,
    "github": _byo_github,
    "confluence": _byo_confluence,
    "jira": _byo_jira,
    "gmail": _byo_gmail,
    "slack": _byo_slack,
    "hubspot": _byo_hubspot,
    "linear": _byo_linear,
    "fireflies": _byo_fireflies,
}


def to_byo(src: str, dsid: str, raw: dict, P: "Principals", org: str) -> list[dict]:
    """One ERB document -> the BYO record(s) it maps to.

    More than one when a child is a first-class document (a HubSpot company plus its notes); a Slack
    thread's replies and a Gmail thread's later messages ride along inside the root record instead,
    because that is how BYO models a thread.
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
        self._open: dict[str, tuple] = {}  # source -> (path, fh, records_written)
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
        fh = io.TextIOWrapper(gzip.GzipFile(path, "wb", compresslevel=9, mtime=0), encoding="utf-8")
        self._open[src] = (path, fh, 0)
        return self._open[src]

    def _close_shard(self, src: str, path: Path, fh, n: int) -> None:
        fh.close()
        self._open.pop(src, None)
        self.shards.setdefault(src, []).append(
            {
                "path": str(path.relative_to(self.out_dir)),
                "records": n,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )

    def close(self, *, counts: dict, documents: int, layer: dict | None = None) -> None:
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
            "sources": {
                src: {
                    "documents": counts.get(src, 0),
                    "records": sum(s["records"] for s in shards),
                    "shards": shards,
                }
                for src, shards in sorted(self.shards.items())
            },
        }
        if roster.exists():
            manifest["roster"] = {
                "path": "roster.yaml",
                "sha256": _sha256(roster),
                "bytes": roster.stat().st_size,
            }
        if layer is not None:
            # Keyed per layer rather than flat: a tool that folds another layer in rewrites the
            # top-level `documents` to the combined total, so a flat `source_documents` beside it
            # would describe a whole the converted layer is only part of.
            manifest["layers"] = {"converted": layer}
        (self.out_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=1, sort_keys=True) + "\n"
        )


def export_byo(
    settings, gen_dir, out_dir, *, question_ids=None, shard_records=None, allow_excluded=0
) -> dict:
    """Convert ERB to a BYO-JSONL artifact: ``corpus.jsonl`` + ``roster.yaml``, or per-source gzip
    shards plus ``manifest.json`` when ``shard_records`` is set (what the full 512k-document corpus
    needs to be distributable).

    The counterpart of :func:`import_structured`: the same records and the same principal
    resolution, written out instead of loaded. ``tests/test_importer_erb.py`` holds the two to
    producing equivalent databases.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    P, records, excluded = _resolve_roster(
        settings, gen_dir, question_ids=question_ids, allow_excluded=allow_excluded
    )
    _precompute_globals(records)
    # The same resolve-then-convert order import_structured uses, and for the same reason: without
    # it the artifact's content depends on which document was read first, and it stops matching a
    # direct import of the same tree.
    _populate_principals(records, P, settings)
    counts: dict[str, int] = {s: 0 for s in SUPPORTED}
    failures: list[tuple[str, str, str]] = []
    writer = _ByoWriter(out_dir, shard_records)
    for i, (src, dsid, raw) in enumerate(records, 1):
        try:
            for rec in to_byo(src, dsid, raw, P, settings.org_name):
                writer.write(src, rec)
            counts[src] += 1
        except Exception as e:  # one bad doc must not sink the conversion
            failures.append((dsid, src, repr(e)))
        if i % 25000 == 0:
            print(
                f"  converted {i}/{len(records)} ({len(failures)} skipped)",
                file=sys.stderr,
                flush=True,
            )
    if failures:
        print(
            f"  WARNING: skipped {len(failures)} docs. First few: {failures[:5]}",
            file=sys.stderr,
            flush=True,
        )
    P.write_roster(out_dir / "roster.yaml", settings)
    # Successes, not attempts — `documents` is what was written, so it cannot contradict its own
    # per-source sum when a document is skipped. `source_documents` is what the bench offered, so the
    # layer states its own arithmetic: source_documents == documents + excluded + failed, and a
    # consumer holding a short count can see what is missing without leaving the artifact.
    layer = {
        "description": "EnterpriseRAG-Bench, redistributed as BYO-JSONL (MIT, onyx-dot-app)",
        "source_documents": len(records) + len(excluded),
        "documents": sum(counts.values()),
        "excluded": excluded,
        "failed": [{"doc_id": d, "source": s, "error": e} for d, s, e in failures],
    }
    snapshot = read_snapshot(gen_dir)
    if snapshot:
        layer["snapshot"] = snapshot
    writer.close(counts=counts, documents=sum(counts.values()), layer=layer)
    return counts


def parse_employees(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text())
    employees: list[dict] = []
    for dept, people in data.get("departments", {}).items():
        for p in people or []:
            employees.append(
                {
                    "name": p["name"],
                    "email": p["email"],
                    "title": p.get("title", ""),
                    "department": dept,
                    "dept_slug": slugify(dept),
                    "mailbox": snake(p["name"]),
                }
            )
    return employees


# ---------------------------------------------------------------- orchestration
KNOWN_EMPTY_DOCS = {
    # A slack thread whose `messages` is "" while its metadata asserts six participants and two
    # timestamps 55 seconds apart: damaged input rather than a thread that is empty by design, and a
    # thread with zero messages is a state the real API cannot produce. Three more documents carry
    # the same mismatch with 3, 5 and 9 characters of body, which is why the rule below tests for
    # empty rather than for some minimum length — the lengths run 0, 3, 5, 9 and then thirty-one
    # more under fifty, so a cutoff would be a number with nothing in the data behind it.
    "dsid_33cbedf0709949fd9416c8c864a86cf2",
}


def select_records(
    gen_dir: Path, question_ids: set[str] | None = None, excluded: list | None = None
):
    """Yield ``(source_type, dsid, raw_json)`` records under ``gen_dir/sources``, or only those
    whose ``dsid`` is in ``question_ids``. A selected doc's container is NOT expanded to its
    siblings, so a sliced corpus can leave a container sparse.

    An empty-content document is dropped HERE rather than in any one consumer — dropping it in only
    one is what let a direct import accept a document the converted artifact then rejected against
    the BYO schema. Pass a list as ``excluded`` to collect them; each entry names the record via
    ``_erb_path``, so "which one?" is a lookup rather than a rescan of the raw bench.
    """
    for src, dsid, raw in iter_records(gen_dir / "sources"):
        if question_ids is not None and dsid not in question_ids:
            continue
        if not (derive_title_content(raw)[1] or "").strip():
            if excluded is not None:
                excluded.append(
                    {
                        "source": src,
                        "doc_id": dsid,
                        "path": raw.get("_erb_path", ""),
                        # The mechanical rule, not a diagnosis: the same damage also
                        # produces near-empty bodies that this test does not catch.
                        "reason": "content empty after strip",
                    }
                )
            continue
        yield src, dsid, raw


def _resolve_roster(settings, gen_dir, *, question_ids=None, allow_excluded=0):
    """Shared prefix: build Principals, materialize records, harvest emails.

    Returns ``(P, records, excluded)``. Every consumer of the corpus goes through here, so this is
    also where an exclusion ``KNOWN_EMPTY_DOCS`` does not name stops the run.
    """
    emails = [e["email"] for e in parse_employees(settings.employee_yaml)]
    settings.org_name, settings.org_domain = infer_org(emails, settings)
    P = Principals.from_directory(settings.employee_yaml, settings.org_domain)
    records, excluded = [], []
    for rec in select_records(gen_dir, question_ids, excluded):
        records.append(rec)
        if len(records) % 25000 == 0:
            print(f"  materialized {len(records)} records...", file=sys.stderr, flush=True)
    # An exclusion this file does not declare means the input changed: `generated_data` has had one
    # commit ever, so the same bench has to yield the same set. Refusing costs a flag; accepting is
    # how a document goes missing with a line on stderr as the only record of it. Nothing caps the
    # list because this gate bounds how long it can get.
    undeclared = [e for e in excluded if e["doc_id"] not in KNOWN_EMPTY_DOCS]
    if len(undeclared) > allow_excluded:
        print(
            f"{len(undeclared)} document(s) excluded that KNOWN_EMPTY_DOCS does not name:",
            file=sys.stderr,
        )
        for e in undeclared:
            print(f"  {e['source']}/{e['path']}  ({e['doc_id']}) — {e['reason']}", file=sys.stderr)
        print(
            f"Read them, then declare them or pass --allow-excluded {len(undeclared)}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if excluded:
        print(
            "  excluded " + ", ".join(f"{e['source']}/{e['path']}" for e in excluded),
            file=sys.stderr,
            flush=True,
        )
    print(f"  materialized {len(records)} records; loading...", file=sys.stderr, flush=True)
    # (Gmail-header email harvesting was dropped: it scanned every message body — minutes of CPU —
    # for marginal value under the directory-only roster. Message senders come straight from the
    # parsed From: headers, and principals still dedupe by canonical name.)
    return P, records, excluded


def _convert_all(records, P, settings, counts: dict, failures: list):
    """Every ERB document as the BYO record(s) it maps to, as ``(where, record)`` pairs.

    A generator function rather than a list: at bench scale this is 788k records, and the loader
    streams them. ``counts`` (documents per source) and ``failures`` are RESET on each call —
    ``byo.load_records`` iterates twice (org inference, then the load), so accumulating would
    double every tally.
    """
    counts.clear()
    counts.update({s: 0 for s in SUPPORTED})
    failures.clear()
    for i, (src, dsid, raw) in enumerate(records, 1):
        try:
            converted = to_byo(src, dsid, raw, P, settings.org_name)
        except Exception as e:  # one bad doc must not sink the import
            failures.append((dsid, src, repr(e)))
            continue
        for rec in converted:
            yield f"{src}/{dsid}", rec
        counts[src] += 1
        if i % 25000 == 0:
            print(
                f"  converted {i}/{len(records)} ({len(failures)} skipped)",
                file=sys.stderr,
                flush=True,
            )


def _populate_principals(records, P, settings) -> None:
    """Run every document through the converter once and throw the records away.

    ``P.resolve`` LEARNS — it harvests real addresses out of Gmail headers and dedupes people by
    canonical name — so converting in a single pass made the output depend on document ORDER: a name
    unresolvable when its own document was converted resolved once a later one introduced it, giving
    a different ``doc_acl``. Resolving everything first means every conversion sees the finished
    directory, which is also what lets a direct import and the exported artifact agree.
    """
    for _ in _convert_all(records, P, settings, {}, []):
        pass


def dump_tokens(settings, gen_dir, *, question_ids=None, allow_excluded=0) -> int:
    """Resolve principals and write ``tokens.yaml`` WITHOUT building the DB — a fast roster preview.
    Returns the tokened-user count. Runs the real converter and discards its records, so the roster
    matches a full import exactly."""
    P, records, _ = _resolve_roster(
        settings, gen_dir, question_ids=question_ids, allow_excluded=allow_excluded
    )
    _precompute_globals(records)
    _populate_principals(records, P, settings)
    P.write_tokens(settings)
    # The same filter write_tokens applies — only the employee directory gets a token, so counting
    # every resolved principal reported four times the rows the file actually holds.
    return sum(1 for u in P.users.values() if u.get("directory"))


def import_structured(settings, gen_dir, *, question_ids=None, allow_excluded=0) -> dict:
    """Build the DB from an ERB ``generated_data`` tree.

    Each document is converted to BYO record(s) and those are loaded — the same path the
    redistributed artifact takes, where ``export_byo`` writes the identical records to JSONL. One
    mapping per source, so a direct import and the artifact cannot disagree.
    """
    P, records, excluded = _resolve_roster(
        settings, gen_dir, question_ids=question_ids, allow_excluded=allow_excluded
    )
    _precompute_globals(records)
    if _SLACK_TS_REMAP:
        print(
            f"  slack: remapped {len(_SLACK_TS_REMAP)} future-dated threads into a realistic "
            f"window (order-preserving)",
            file=sys.stderr,
            flush=True,
        )
    counts: dict[str, int] = {}
    failures: list[tuple[str, str, str]] = []

    # Resolve first, load second (see _populate_principals): the roster can only be written once
    # every document has been through P, and the load pass then converts against the finished
    # directory. `byo.load_records` reads the roster as the CLOSED principal set, which is what
    # keeps a Slack display handle or an outside sender from becoming an org account with a working
    # token. It is the same sidecar a converted artifact ships, so a direct import leaves the same
    # file behind.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    roster_path = settings.data_dir / "roster.yaml"
    _populate_principals(records, P, settings)
    P.write_roster(roster_path, settings)

    # validate=False: these records come from `to_byo`, i.e. from code the schemas describe, and
    # `test_erb_to_byo_output_validates_against_the_byo_schemas` already holds it to them.
    byo.load_records(
        lambda: _convert_all(records, P, settings, counts, failures),
        settings,
        reset=True,
        roster=roster_path,
        validate=False,
    )
    if failures:
        print(
            f"  WARNING: skipped {len(failures)} docs. First few: {failures[:5]}",
            file=sys.stderr,
            flush=True,
        )

    # `byo.load_records` counted `source_documents` at its own granularity — one per BYO record
    # `_convert_all` handed it — which overcounts here: `to_byo` can split ONE ERB document into
    # several top-level BYO records (a HubSpot company plus its notes, see `_byo_hubspot`), unlike a
    # hand-written BYO corpus where a document's children (replies/comments) always ride inside a
    # single record. The number this importer offers is `len(records) + len(excluded)` — the same
    # arithmetic `export_byo`'s `layer` documents (`source_documents == documents + excluded +
    # failed`) — so it is corrected here, after the load that wrote the wrong one.
    conn = store.connect_rw(settings.db_path)
    store.write_meta(conn, "source_documents", len(records) + len(excluded))
    conn.close()
    return counts


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Import EnterpriseRAG-Bench (faithful, structured) into the mock DB."
    )
    ap.add_argument(
        "--slice-questions",
        type=Path,
        default=None,
        help="only import docs referenced (expected_doc_ids) by this questions JSONL",
    )
    ap.add_argument(
        "--ref", default="main", help="EnterpriseRAG-Bench branch/ref to fetch (default: main)"
    )
    ap.add_argument(
        "--no-download",
        action="store_true",
        help="reuse cached data/raw/generated_data; skip fetching",
    )
    ap.add_argument(
        "--tokens-only",
        action="store_true",
        help="resolve the roster and write tokens.yaml WITHOUT building the DB (fast)",
    )
    ap.add_argument(
        "--export-byo",
        type=Path,
        default=None,
        metavar="DIR",
        help="write a BYO-JSONL artifact into DIR instead of building the DB: "
        "corpus.jsonl + roster.yaml, or shards + manifest.json with "
        "--shard-records; `backlot.importer.byo` loads either to an equivalent DB",
    )
    ap.add_argument(
        "--shard-records",
        type=int,
        default=None,
        metavar="N",
        help="with --export-byo: write data/<source>/part-*.jsonl.gz shards of N "
        "records each plus manifest.json, instead of one corpus.jsonl",
    )
    ap.add_argument(
        "--allow-excluded",
        type=int,
        default=0,
        metavar="N",
        help="proceed with up to N empty-content documents that KNOWN_EMPTY_DOCS does "
        "not name (default 0: any undeclared exclusion stops the run)",
    )
    args = ap.parse_args(argv)
    if args.shard_records is not None and args.shard_records < 1:
        # 0 makes `n >= shard_records` always true: one shard per record, 600k files for the bench,
        # which is the very thing sharding was added to avoid.
        ap.error("--shard-records must be at least 1")
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
        counts = export_byo(
            settings,
            gen_dir,
            args.export_byo,
            question_ids=question_ids,
            shard_records=args.shard_records,
            allow_excluded=args.allow_excluded,
        )
        dest = (
            f"{args.export_byo}/data/<source>/part-*.jsonl.gz + manifest.json"
            if args.shard_records
            else f"{args.export_byo}/corpus.jsonl"
        )
        print(f"Converted {sum(counts.values())} documents -> {dest}")
        for src, n in counts.items():
            print(f"  {src:14s} {n}")
        print(
            f"Roster -> {args.export_byo}/roster.yaml "
            f"(org {settings.org_name}, domain {settings.org_domain})"
        )
        print(
            f"Load it with: python -m backlot.importer.byo {args.export_byo}/corpus.jsonl "
            f"--roster {args.export_byo}/roster.yaml"
        )
        return 0

    if args.tokens_only:
        n = dump_tokens(
            settings, gen_dir, question_ids=question_ids, allow_excluded=args.allow_excluded
        )
        print(f"Wrote {n} users to {settings.tokens_path} (roster only; no DB built)")
        print(f"Org: {settings.org_name} ({settings.org_domain})")
        return 0

    counts = import_structured(
        settings, gen_dir, question_ids=question_ids, allow_excluded=args.allow_excluded
    )
    print(f"Loaded {sum(counts.values())} documents into {settings.db_path}")
    for src, n in counts.items():
        print(f"  {src:14s} {n}")
    print(f"Org: {settings.org_name} ({settings.org_domain}) · tokens -> {settings.tokens_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
