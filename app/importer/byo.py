"""Load a Bring-Your-Own (BYO) corpus from JSONL into the mock DB.

Serve *any* document set through the six vendor APIs — not just EnterpriseRAG-Bench.
Each line is one document:

    {
      "source_type": "confluence",        # required: slack|gmail|google_drive|github|jira|confluence
      "title": "Onboarding guide",         # required except for slack (messages have no title)
      "content": "Full text...",            # required
      "doc_id": "my-123",                  # optional (default: dsid_<sha256(src+title+content)>)
      "space": "handbook",                 # the grouping unit, named per service: slack "channel",
                                             #   gmail "mailbox", google_drive "folder", github "repo",
                                             #   jira "project", confluence "space" (default: source_type)
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
      "replies": [                          # slack only: threaded replies — full messages, not just text
        {"content": "on it", "author_email": "bob@acme.com",
         "reactions": [{"name": "eyes", "count": 1}]}
      ]
    }

For slack, a record with a `replies` array becomes a thread: the record is the root
message and each reply is a threaded reply (served via conversations.replies; only the
root shows in conversations.history). Replies inherit the root's container and ACL.

ACL rules per doc: `readers` (emails→users, else groups) win; else `private`→author only;
`group`→the container's group; default→org-wide (everyone). Group membership is the union of
each author's `author_groups` plus the group of every container they authored in — so a
`group`-restricted doc is visible to authors in that container. This one script is the BYO
counterpart to `app.importer.erb` — it builds the DB + ACL + `data/tokens.yaml` from JSONL.

Every record is validated against its per-service JSON Schema before loading (a bad corpus
never half-loads). ``--dry-run`` validates the whole file and reports problems without touching
the DB — there's no separate validate command.

Usage:  python -m app.importer.byo path/to/corpus.jsonl [--append | --dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path  # noqa: F401  (kept for typing/backcompat)

import yaml

from app import store, synth
from app.config import Settings, get_settings, infer_org
from app.validation import record_errors, validate_file


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


def _service_columns(src, ex, subtype, parent_id, doc_id, thread_id, seq, org_domain,
                     created=None, updated=None) -> dict:
    """Map generic BYO fields (+ meta) to the target service table's own columns.

    ``created``/``updated`` are pre-parsed epoch seconds (or None). Services with a
    distinct modified time carry ``updated_ts``; slack/gmail carry only ``created_ts``."""
    if src == "slack":
        return {"thread_id": thread_id, "thread_seq": seq,
                "subtype": subtype or ex.get("subtype"),
                "reactions": _j(ex.get("reactions")), "files": _j(ex.get("files")),
                "edited": _j(ex.get("edited")), "created_ts": created}
    if src == "gmail":
        return {"thread_id": ex.get("thread") or doc_id, "label_ids": _j(ex.get("label_ids")),
                "to_addr": ex.get("to"), "cc": ex.get("cc"), "bcc": ex.get("bcc"),
                "reply_to": ex.get("reply_to"), "message_id": ex.get("message_id"),
                "in_reply_to": ex.get("in_reply_to"), "refs": _j(ex.get("references")),
                "attachments": _j(ex.get("attachments")), "created_ts": created,
                "body_html": ex.get("html")}
    if src == "google_drive":
        return {"subtype": subtype, "mime_type": ex.get("mime_type"), "parents": _j(ex.get("parents")),
                "created_ts": created, "updated_ts": updated,
                "trashed": (1 if ex.get("trashed") else None)}
    if src == "github":
        return {"kind": subtype or "issue", "path": ex.get("path"), "state": ex.get("state"),
                "labels": _j(ex.get("labels")), "assignees": _j(ex.get("assignees")),
                "merged_at": ex.get("merged_at"), "head_ref": ex.get("head"),
                "base_ref": ex.get("base"), "reviews": _j(ex.get("reviews")),
                "reactions": _j(ex.get("reactions")), "created_ts": created, "updated_ts": updated,
                "closed_ts": _epoch(ex.get("closed_at")), "closed_by": ex.get("closed_by"),
                "merged_by": ex.get("merged_by"), "milestone": ex.get("milestone"),
                "requested_reviewers": _j(ex.get("requested_reviewers"))}
    if src == "jira":
        return {"status": ex.get("status"), "issuetype": ex.get("issuetype"),
                "priority": ex.get("priority"), "labels": _j(ex.get("labels")),
                "components": _j(ex.get("components")), "issuelinks": _j(ex.get("issuelinks")),
                "parent_id": parent_id, "changelog": _j(ex.get("changelog")),
                "created_ts": created, "updated_ts": updated,
                "assignee_email": ex.get("assignee"), "reporter_email": ex.get("reporter"),
                "resolution": ex.get("resolution"), "resolution_ts": _epoch(ex.get("resolutiondate")),
                "duedate": ex.get("duedate"), "fix_versions": _j(ex.get("fix_versions"))}
    if src == "confluence":
        return {"subtype": subtype or "page", "parent_id": parent_id, "labels": _j(ex.get("labels")),
                "created_ts": created, "updated_ts": updated,
                "version_number": ex.get("version_number"), "version_message": ex.get("version_message"),
                "minor_edit": (1 if ex.get("minor_edit") else None)}
    if src == "notion":
        return {"subtype": subtype or "page", "parent_id": parent_id,
                "properties": _j(ex.get("properties")), "icon": ex.get("icon"),
                "cover": ex.get("cover"), "created_ts": created, "updated_ts": updated}
    if src == "s3":
        return {"key": ex.get("key"), "subtype": subtype or "STANDARD",
                "content_type": ex.get("content_type") or "text/plain",
                "size": ex.get("size"), "created_ts": created, "updated_ts": updated}
    if src == "hubspot":
        # The object type is the grouping column, so it is set by the caller like every other
        # container. HubSpot's typed fields stay in one JSON column because search filters may
        # name any property (see schemas/hubspot.schema.json).
        return {"properties": _j(ex.get("properties")),
                "archived": (1 if ex.get("archived") else None),
                "created_ts": created, "updated_ts": updated}
    return {}


def _emails(rec: dict):
    """Yield every email that appears in a record (author, readers, comment authors)."""
    v = rec.get("author_email")
    if isinstance(v, str) and "@" in v:
        yield v
    for r in rec.get("readers") or []:
        if isinstance(r, str) and "@" in r:
            yield r
    for c in rec.get("comments") or []:
        cv = c.get("author_email") if isinstance(c, dict) else None
        if isinstance(cv, str) and "@" in cv:
            yield cv


def _infer_org(records: list[dict], settings: Settings) -> tuple[str, str]:
    """Derive (org_name, org_domain) from the corpus's dominant author email domain."""
    return infer_org((e for rec in records for e in _emails(rec)), settings)


def load(path: Path, settings: Settings | None = None, reset: bool = True) -> dict:
    settings = settings or get_settings()
    if reset and settings.db_path.exists():
        settings.db_path.unlink()
    conn = store.connect_rw(settings.db_path)

    lines = Path(path).read_text().splitlines()
    # infer the org from the corpus (dominant email domain) before building any grants,
    # since public docs are granted to the org principal — see _infer_org.
    _scan = []
    for _ln in lines:
        _ln = _ln.strip()
        if _ln:
            try:
                _scan.append(json.loads(_ln))
            except json.JSONDecodeError:
                pass  # malformed lines are reported precisely in the main loop below
    org_name, org_domain = _infer_org(_scan, settings)
    if not reset:
        row = conn.execute("SELECT id FROM principals WHERE type='org' LIMIT 1").fetchone()
        if row:
            org_name = row[0]
    org = org_name

    containers: dict[tuple[str, str], str] = {}   # (source_type, name) -> group_id
    users: dict[str, str] = {}                    # email -> display name
    groups: set[str] = set()
    memberships: set[tuple[str, str]] = set()      # (group_id, email)
    grants: list[tuple[str, str, str]] = []        # (doc_id, principal_type, principal_id)
    counts: dict[str, int] = {}
    seen: set[str] = set()
    fts_ids: dict[str, list[str]] = {}
    # HubSpot associations are resolved after the whole corpus is read: a link may name a target
    # that appears on a later line, and an omitted `to_type` is filled in from the target's own
    # object type. doc_id -> object_type, plus the declared links.
    hs_types: dict[str, str] = {}
    hs_links: list[tuple[str, str, dict]] = []      # (from_doc_id, from_type, declaration)

    for lineno, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise SystemExit(f"line {lineno}: invalid JSON: {e}")
        # Schema pre-validation: source_type/content/title, enums, comment/reply shapes,
        # and unknown-key rejection all come from schemas/ (see app.validation).
        errors = record_errors(rec)
        if errors:
            raise SystemExit(f"line {lineno}: " + "; ".join(errors))
        src = rec["source_type"]
        # Slack messages have no title; the other five carry a natural one.
        title = rec.get("title") or ""

        doc_id = _doc_id(rec)
        if doc_id in seen:
            continue
        seen.add(doc_id)
        gcol = store.grouping_col(src)
        container = str(rec.get(gcol) or src)   # channel / mailbox / folder / repo / project / space
        group = str(rec.get("group") or slugify(container) or src)
        containers[(src, container)] = group
        groups.add(group)

        def register(email: str | None, name: str | None = None) -> None:
            if email:
                users.setdefault(email, name or _display_name(email))
                memberships.add((group, email))

        author = rec.get("author_email")
        register(author, rec.get("author_name"))
        for g in rec.get("author_groups", []):
            groups.add(g)
            if author:
                memberships.add((g, author))

        # grant tuples (principal_type, principal_id), shared by the whole thread
        readers = rec.get("readers")
        vis = rec.get("visibility")
        if readers:
            grant_types = []
            for pid in readers:
                if "@" in pid:
                    grant_types.append(("user", pid))
                    users.setdefault(pid, _display_name(pid))
                else:
                    grant_types.append(("group", pid))
                    groups.add(pid)
        elif vis == "private" and author:
            grant_types = [("user", author)]
        elif vis == "group":
            grant_types = [("group", group)]
        else:
            grant_types = [("org", org)]

        # structured extras: rec.meta merged with convenience top-level keys
        extras = dict(rec.get("meta") or {})
        for k in ("labels", "reactions", "files", "edited", "to", "cc", "bcc", "reply_to",
                  "message_id", "in_reply_to", "references", "attachments", "mime_type",
                  "parents", "trashed", "state", "assignees", "merged_at", "head", "base", "reviews",
                  "status", "issuetype", "priority", "components", "issuelinks",
                  "label_ids", "thread", "html", "closed_at", "closed_by", "merged_by", "milestone",
                  "requested_reviewers", "resolution", "resolutiondate", "duedate",
                  "fix_versions", "versions", "assignee", "reporter", "minor_edit",
                  "version_message", "version_number", "properties", "icon", "cover",
                  "key", "content_type", "size", "path", "archived"):
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

        def insert(did, email, ttl, body, seq=0, sub=None, par=None, ex=None, cts=None, uts=None):
            cols = _service_columns(src, ex or {}, sub, par, did, thread_id, seq,
                                    org_domain, cts, uts)
            cols.update(doc_id=did, author_email=email or f"unknown@{org_domain}",
                        title=ttl, content=body)
            if src == "s3" and cols.get("size") is None:
                cols["size"] = len((body or "").encode("utf-8"))
            cols[gcol] = container
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

        insert(doc_id, author, title, rec["content"], 0, subtype, parent_id, extras, created, updated)

        if src == "hubspot":
            hs_types[doc_id] = container
            for a in rec.get("associations") or []:
                hs_links.append((doc_id, container, a))

        # comments on the document — only jira/confluence/github expose them (slack uses replies)
        rec_comments = rec.get("comments") or []
        ctable = store.comment_table(src)
        if rec_comments and ctable is None:
            raise SystemExit(f"line {lineno}: comments are not supported for source_type {src!r}")
        for j, c in enumerate(rec_comments, start=1):
            body = c.get("body") or c.get("content")
            if not body:
                raise SystemExit(f"line {lineno}: each comment needs 'content'")
            register(c.get("author_email"), c.get("author_name"))
            conn.execute(
                f"INSERT OR REPLACE INTO {ctable}"
                "(id, doc_id, seq, author_email, body, created_ts, reactions) VALUES (?,?,?,?,?,?,?)",
                (cid := c.get("id") or f"{doc_id}::c{j}", doc_id, j, c.get("author_email"), body,
                 _epoch(c.get("created_ts")) or synth.epoch(cid), _j(c.get("reactions"))),
            )

        for i, rep in enumerate(replies or [], start=1):
            if not rep.get("content"):
                raise SystemExit(f"line {lineno}: each reply needs 'content'")
            rep_author = rep.get("author_email") or author
            register(rep_author, rep.get("author_name"))
            rep_id = rep.get("doc_id") or (
                "dsid_" + hashlib.sha256((doc_id + str(i) + rep["content"]).encode()).hexdigest()[:32])
            if rep_id in seen:
                continue
            seen.add(rep_id)
            # A reply is a full message (reactions/files/subtype/edited carry through);
            # its time is the root's + its position so the thread stays ordered (created is now
            # always set, so a reply ts is never NULL).
            rep_cts = created + i
            insert(rep_id, rep_author, rep.get("title") or "", rep["content"], i,
                   sub=rep.get("subtype"), ex=rep, cts=rep_cts)

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
            row = conn.execute("SELECT object_type FROM hubspot_objects WHERE doc_id = ?",
                               (to_doc,)).fetchone()
            to_type = row["object_type"] if row else None
        if to_type is None:
            raise SystemExit(
                f"hubspot association {from_doc} -> {to_doc}: target not found in this corpus or "
                f"the existing DB; add the target record or set 'to_type' on the association")
        category = a.get("category") or "HUBSPOT_DEFINED"
        label = a.get("label")
        # An explicit type_id applies only to the direction the author declared; the reverse gets
        # its own synthesized id, since the two directions never share one in real HubSpot.
        for f_doc, f_type, t_doc, t_type, tid in (
                (from_doc, from_type, to_doc, to_type, a.get("type_id")),
                (to_doc, to_type, from_doc, from_type, None)):
            conn.execute(
                "INSERT OR REPLACE INTO hubspot_associations(from_doc_id, from_type, to_doc_id, "
                "to_type, assoc_category, assoc_type_id, label) VALUES (?,?,?,?,?,?,?)",
                (f_doc, f_type, t_doc, t_type, category,
                 tid or synth.hubspot_assoc_type_id(f_type, t_type), label))

    # principals: org, groups, users
    conn.execute("INSERT OR REPLACE INTO principals VALUES (?,?,?,?)", (org, "org", org, None))
    for g in groups:
        conn.execute("INSERT OR REPLACE INTO principals VALUES (?,?,?,?)", (g, "group", g, None))
    for email, name in users.items():
        conn.execute("INSERT OR REPLACE INTO principals VALUES (?,?,?,?)", (email, "user", name, email))
    for src, name in {(s, n) for (s, n) in containers}:
        gtable, gcol = store.GROUPING[src]
        conn.execute(f"INSERT OR REPLACE INTO {gtable}({gcol}, group_id) VALUES (?,?)",
                     (name, containers[(src, name)]))
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

    users_rows = {e: {"email": e, "name": n, "token": _user_token(e)} for e, n in users.items()}
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
    tokens = {"org": token_org, "org_domain": token_domain,
              "admin_token": token_admin,
              "users": [users_rows[k] for k in sorted(users_rows)]}
    settings.tokens_path.write_text(yaml.safe_dump(tokens, sort_keys=False))
    from app import oauth
    oauth.generate(settings, org=org_name)
    conn.close()
    return {"counts": counts, "users": len(users), "groups": len(groups),
            "org": org_name, "org_domain": org_domain, "total": sum(counts.values())}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Import (load) a BYO JSONL corpus into the mock DB.")
    ap.add_argument("corpus", help="path to a JSONL corpus file")
    ap.add_argument("--append", action="store_true", help="add to the existing DB instead of resetting")
    ap.add_argument("--dry-run", action="store_true", help="validate the corpus only; don't touch the DB")
    args = ap.parse_args(argv)
    corpus = Path(args.corpus)

    if args.dry_run:
        problems = validate_file(corpus)
        if not problems:
            n = sum(1 for line in corpus.read_text().splitlines() if line.strip())
            print(f"OK: {n} records valid.")
            return 0
        print(f"INVALID: {len(problems)} problem(s) in {corpus}", file=sys.stderr)
        for lineno, msg in problems:
            print(f"  line {lineno}: {msg}", file=sys.stderr)
        return 1

    settings = get_settings()
    res = load(corpus, settings, reset=not args.append)
    print(f"Loaded {res['total']} documents into {settings.db_path}")
    for src, n in sorted(res["counts"].items()):
        print(f"  {src:14s} {n}")
    print(f"Principals: {res['users']} users, {res['groups']} groups")
    print(f"Tokens written to {settings.tokens_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
