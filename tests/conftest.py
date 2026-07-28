"""Shared fixtures: one small in-code corpus, built into a DB once and served on demand.

``SAMPLE`` is the single source of test data — it carries the +α surface (threads, reactions,
comments, attachments, doc types, issue links/subtasks, child pages) that the SDK/MCP tests
exercise, plus public/group/private docs for the ACL tests. It is deliberately independent of
``examples/bring-your-own-corpus/sample_corpus.jsonl`` (which belongs to the BYO example).

- ``db``   — a read-only connection to the built DB.
- ``acl`` / ``tokens`` — the generated ACL and email->token map for that DB.
- ``live_server`` — the same DB served by a real ``uvicorn`` subprocess (the official SDKs and
  the Dockerised MCP server make real HTTP calls, so they need a listening port rather than the
  in-process ``TestClient`` used elsewhere).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

from app import store
from app.acl import Acl
from app.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent

# One corpus for every test. Explicit doc_ids on the docs the ACL tests assert against.
SAMPLE = [
    {"source_type": "confluence", "doc_id": "cf-handbook", "space": "handbook", "group": "engineering",
     "title": "Engineering Handbook", "content": "How we build software: standards, review, on-call.",
     "author_email": "ava@acme.com", "author_groups": ["engineering"], "visibility": "public",
     "labels": ["engineering", "handbook"]},
    {"source_type": "confluence", "doc_id": "cf-oncall", "parent": "cf-handbook", "space": "handbook",
     "group": "engineering", "title": "On-call Runbook",
     "content": "Respond to gateway 502s: check dashboards, roll back, page on-call.",
     "author_email": "ava@acme.com", "author_groups": ["engineering"], "visibility": "public",
     "labels": ["oncall", "runbook"],
     "comments": [{"content": "Add the rate-limiter rollback step.", "author_email": "bob@acme.com"}]},
    {"source_type": "confluence", "doc_id": "cf-comp", "space": "people-ops", "group": "people",
     "title": "Compensation Bands 2026", "content": "Confidential salary bands. People team only.",
     "author_email": "hana@acme.com", "author_groups": ["people"], "visibility": "group"},

    {"source_type": "slack", "channel": "eng-announcements", "group": "engineering",
     "content": "Reminder: production deploy freeze starts Friday.", "author_email": "ava@acme.com",
     "visibility": "public"},
    {"source_type": "slack", "channel": "incidents", "group": "engineering",
     "content": "Anyone else seeing 502s from the gateway?", "author_email": "bob@acme.com",
     "author_groups": ["engineering"], "visibility": "public",
     "reactions": [{"name": "eyes", "count": 2, "users": ["U01", "U02"]}],
     "replies": [{"content": "Yeah, looking now.", "author_email": "ava@acme.com"},
                 {"content": "Rolled back; 502s clearing.", "author_email": "bob@acme.com"}]},
    {"source_type": "slack", "channel": "people-confidential", "group": "people",
     "content": "Confidential people-ops note: Q3 reorg headcount plan.",
     "author_email": "hana@acme.com", "author_groups": ["people"], "visibility": "group"},

    {"source_type": "github", "doc_id": "gh-issue-1", "repo": "gateway", "group": "engineering",
     "title": "Rate limiter drops bursts under 50ms", "content": "Token-bucket refill is off by one tick.",
     "author_email": "bob@acme.com", "author_groups": ["engineering"], "visibility": "public",
     "meta": {"state": "open", "labels": ["bug", "gateway"]},
     "comments": [{"content": "Confirmed with a repro test.", "author_email": "ava@acme.com"}]},
    {"source_type": "github", "doc_id": "gh-pr-1", "repo": "gateway", "group": "engineering",
     "title": "Fix token-bucket refill off-by-one", "content": "Corrects the refill tick; adds a test.",
     "author_email": "bob@acme.com", "author_groups": ["engineering"], "visibility": "public",
     "subtype": "pull_request",
     "meta": {"state": "closed", "merged_at": "2026-02-10T12:00:00Z", "head": "fix/rl", "base": "main",
              "labels": ["bug"],
              "reviews": [{"author_email": "ava@acme.com", "state": "APPROVED", "body": "LGTM."}]},
     "comments": [{"content": "Add a metric for dropped bursts?", "author_email": "ava@acme.com"}]},
    {"source_type": "github", "doc_id": "gh-sec-1", "repo": "vault", "group": "people",
     "title": "Rotate quarterly signing keys", "content": "Track key rotation for the people-ops vault.",
     "author_email": "hana@acme.com", "author_groups": ["people"], "visibility": "group",
     "meta": {"state": "open", "labels": ["security"]}},

    {"source_type": "jira", "doc_id": "jira-sev2", "project": "payments", "group": "payments",
     "title": "SEV2: checkout latency spike", "content": "p95 checkout latency jumped to 2.1s.",
     "author_email": "bob@acme.com", "author_groups": ["payments", "engineering"], "visibility": "group",
     "meta": {"status": "In Progress", "issuetype": "Incident", "priority": "High",
              "issuelinks": [{"id": "1", "type": {"name": "Blocks", "inward": "is blocked by",
                                                  "outward": "blocks"},
                              "outwardIssue": {"key": "PAY-42",
                                               "fields": {"summary": "Right-size the pool",
                                                          "status": {"name": "To Do"},
                                                          "issuetype": {"name": "Task"}}}}]},
     "comments": [{"content": "Rolled back; latency recovering.", "author_email": "ava@acme.com"},
                  {"content": "p95 back to ~240ms.", "author_email": "bob@acme.com"}]},
    {"source_type": "jira", "doc_id": "jira-sub1", "parent": "jira-sev2", "project": "payments",
     "group": "payments", "title": "Write postmortem for the SEV2", "content": "Draft the postmortem.",
     "author_email": "ava@acme.com", "author_groups": ["payments", "engineering"], "visibility": "group",
     "meta": {"issuetype": "Sub-task", "status": "To Do"}},
    {"source_type": "jira", "doc_id": "jira-private", "project": "payments", "group": "payments",
     "title": "Personal task: rotate my API keys", "content": "Private note to self.",
     "author_email": "bob@acme.com", "visibility": "private"},

    {"source_type": "gmail", "mailbox": "ceo", "title": "Q1 board deck draft",
     "content": "Draft narrative for the Q1 board meeting.", "author_email": "ceo@acme.com",
     "readers": ["ceo@acme.com", "ava@acme.com"], "cc": "cfo@acme.com",
     "attachments": [{"filename": "Q1-deck.pdf", "mime": "application/pdf", "size": 2048,
                      "content": "PDF bytes placeholder"}]},
    {"source_type": "gmail", "mailbox": "cfo", "title": "Confidential comp review",
     "content": "Q3 compensation adjustments — do not forward.", "author_email": "cfo@acme.com",
     "readers": ["cfo@acme.com"]},

    {"source_type": "google_drive", "folder": "marketing", "group": "marketing",
     "title": "Brand guidelines v3", "content": "Logo usage, palette, typography.",
     "author_email": "mia@acme.com", "author_groups": ["marketing"], "visibility": "public",
     "subtype": "document"},
    {"source_type": "google_drive", "folder": "finance", "group": "finance", "title": "Q1 Revenue Model",
     "content": "month,revenue\nJan,120000\nFeb,135000", "author_email": "cfo@acme.com",
     "author_groups": ["finance"], "visibility": "group", "subtype": "spreadsheet"},
    {"source_type": "google_drive", "folder": "marketing", "group": "marketing",
     "title": "All-hands Q1 Deck", "content": "Slide 1\n\nSlide 2", "author_email": "mia@acme.com",
     "author_groups": ["marketing"], "visibility": "public", "subtype": "presentation"},
    {"source_type": "google_drive", "folder": "security", "group": "security-compliance",
     "title": "Security Whitepaper.pdf", "content": "%PDF-1.7 placeholder.", "author_email": "sec@acme.com",
     "author_groups": ["security-compliance"], "visibility": "public", "subtype": "pdf",
     "meta": {"mime_type": "application/pdf"}},

    {"source_type": "notion", "doc_id": "nt-runbook", "teamspace": "engineering",
     "group": "engineering", "title": "Notion On-call Runbook",
     "content": "# On-call\n\nCheck dashboards, roll back, page on-call.",
     "author_email": "ava@acme.com", "author_groups": ["engineering"], "visibility": "public",
     "icon": "📟",
     "comments": [{"content": "add rate-limiter step", "author_email": "bob@acme.com"}]},
    {"source_type": "notion", "doc_id": "nt-tasks-db", "subtype": "database",
     "teamspace": "engineering", "group": "engineering", "title": "Eng Tasks",
     "content": "Team task tracker.", "author_email": "ava@acme.com",
     "author_groups": ["engineering"], "visibility": "public",
     "properties": {"Status": {"type": "select"}, "Priority": {"type": "select"}}},
    {"source_type": "notion", "doc_id": "nt-task-1", "parent": "nt-tasks-db",
     "teamspace": "engineering", "group": "engineering", "title": "Fix gateway 502s",
     "content": "Investigate token-bucket refill.", "author_email": "bob@acme.com",
     "author_groups": ["engineering"], "visibility": "public",
     "properties": {"Status": "In Progress", "Priority": "High"}},
    {"source_type": "notion", "doc_id": "nt-secret", "teamspace": "people-ops",
     "group": "people", "title": "Comp planning notes", "content": "Confidential.",
     "author_email": "hana@acme.com", "author_groups": ["people"], "visibility": "group"},

    {"source_type": "s3", "doc_id": "s3-runbook", "bucket": "eng-artifacts",
     "group": "engineering", "key": "runbooks/oncall.md", "title": "On-call Runbook",
     "content": "Check dashboards, roll back, page on-call.", "content_type": "text/markdown",
     "author_email": "ava@acme.com", "author_groups": ["engineering"], "visibility": "public"},
    {"source_type": "s3", "doc_id": "s3-arch", "bucket": "eng-artifacts",
     "group": "engineering", "key": "design/architecture.md", "title": "Architecture",
     "content": "Gateway, workers, and the token bucket.", "content_type": "text/markdown",
     "author_email": "bob@acme.com", "author_groups": ["engineering"], "visibility": "public"},
    {"source_type": "s3", "doc_id": "s3-secret", "bucket": "people-vault", "group": "people",
     "key": "comp/bands.csv", "title": "Comp Bands", "content": "band,min,max\nL5,180,220",
     "content_type": "text/csv", "author_email": "hana@acme.com", "author_groups": ["people"],
     "visibility": "group"},

    # HubSpot: the object type is the container, so these span three of them. The contact and the
    # note are associated with the company (declared once; the loader writes both directions).
    {"source_type": "hubspot", "doc_id": "hs-co-acme", "object_type": "companies",
     "group": "sales", "title": "Acme Health",
     "content": "Acme Health — mid-market healthcare provider evaluating the platform.",
     "author_email": "rep@acme.com", "author_groups": ["sales"], "visibility": "public",
     "properties": {"name": "Acme Health", "domain": "acme-health.com",
                    "industry": "healthcare", "lifecyclestage": "evaluation",
                    "employees": "150", "founded": "2011-03-01"}},
    {"source_type": "hubspot", "doc_id": "hs-c-ava", "object_type": "contacts",
     "group": "sales", "title": "Ava Stone",
     "content": "Ava Stone — VP Platform at Acme Health.",
     "author_email": "rep@acme.com", "author_groups": ["sales"], "visibility": "public",
     "properties": {"firstname": "Ava", "lastname": "Stone", "email": "ava@acme-health.com"},
     "associations": [{"to": "hs-co-acme", "label": "Primary"}]},
    {"source_type": "hubspot", "doc_id": "hs-note-1", "object_type": "notes",
     "group": "sales", "title": "",
     "content": "Security review scheduled; wants EU data residency.",
     "author_email": "rep@acme.com", "author_groups": ["sales"], "visibility": "public",
     "properties": {"hs_note_body": "Security review scheduled; wants EU data residency."},
     "associations": [{"to": "hs-co-acme"}]},
    # Restricted by explicit readers rather than group visibility, so the `companies` container
    # keeps a single owning group while still giving the ACL tests a hidden CRM record.
    {"source_type": "hubspot", "doc_id": "hs-co-secret", "object_type": "companies",
     "group": "sales", "title": "Stealth Health Co",
     "content": "Confidential account under NDA — people team only.",
     "author_email": "hana@acme.com", "author_groups": ["people"],
     "readers": ["hana@acme.com"],
     "properties": {"name": "Stealth Health Co", "lifecyclestage": "qualified"}},
    # Two more companies so a small page size actually produces a cursor: with only two rows a
    # `limit=2` crawl never takes the paging branch, and the cursor path would go untested.
    {"source_type": "hubspot", "doc_id": "hs-co-borealis", "object_type": "companies",
     "group": "sales", "title": "Borealis Clinics",
     "content": "Regional clinic network, procurement stage.", "author_email": "rep@acme.com",
     "author_groups": ["sales"], "visibility": "public",
     "properties": {"name": "Borealis Clinics", "domain": "borealis.example",
                    "industry": "healthcare", "lifecyclestage": "procurement",
                    "employees": "400", "founded": "2014-06-01"}},
    # `archived` is only meaningful if something is archived: this row is excluded from the default
    # listing and is the only row the archived view returns.
    {"source_type": "hubspot", "doc_id": "hs-co-defunct", "object_type": "companies",
     "group": "sales", "title": "Defunct Labs", "content": "Churned; record archived.",
     "author_email": "rep@acme.com", "author_groups": ["sales"], "visibility": "public",
     "archived": True,
     "properties": {"name": "Defunct Labs", "lifecyclestage": "qualified", "employees": "12"}},

    # Linear: the team is the container, so these span two of them. `lin-rl` carries the full
    # surface the LlamaIndex reader dereferences (state/project/labels/creator/assignee/estimate/
    # dueDate/branchName) plus comments; `lin-secret` is the hidden one the ACL tests assert on.
    {"source_type": "linear", "doc_id": "lin-rl", "team": "engineering", "group": "engineering",
     "title": "Rate limiter drops bursts under 50ms",
     "content": "Token-bucket refill is off by one tick under sustained burst load.",
     "author_email": "ava@acme.com", "author_groups": ["engineering"], "visibility": "public",
     "identifier": "ENG-101", "state": "In Progress", "priority": "P1", "estimate": 5,
     "labels": ["bug", "gateway"], "project": "runtime-stability", "cycle": "2025-W08",
     "dueDate": "2026-03-15", "assignee": "bob@acme.com", "assigneeName": "Bob Stone",
     "created": "2026-02-18T00:00:00Z", "updated": "2026-03-04T00:00:00Z",
     "comments": [{"content": "Reproduced with a burst test.", "author_email": "bob@acme.com"},
                  {"content": "Fix is in review.", "author_email": "ava@acme.com"}]},
    {"source_type": "linear", "doc_id": "lin-batch", "team": "engineering",
     "group": "engineering", "title": "Continuous batching stalls after compaction",
     "content": "A 50ms stall when the batcher merges requests right after compaction.",
     "author_email": "bob@acme.com", "author_groups": ["engineering"], "visibility": "public",
     "identifier": "ENG-102", "state": "Done", "priority": "P0", "estimate": 3,
     "labels": ["latency"], "project": "runtime-stability",
     "completedAt": "2026-03-10T00:00:00Z",
     "created": "2026-03-01T00:00:00Z", "updated": "2026-03-10T00:00:00Z"},
    {"source_type": "linear", "doc_id": "lin-des", "team": "design", "group": "design",
     "title": "Revamp field states and selects",
     "content": "Focus rings, hover and disabled states for selects.",
     "author_email": "mia@acme.com", "author_groups": ["design"], "visibility": "public",
     "identifier": "DES-77", "state": "In Review", "priority": "P2", "labels": ["tokens"]},
    # Restricted by explicit readers, so the `engineering` team keeps one owning group while the
    # ACL tests still have a Linear issue that must stay hidden.
    {"source_type": "linear", "doc_id": "lin-secret", "team": "engineering",
     "group": "engineering", "title": "Rotate the signing keys",
     "content": "Key rotation runbook — people team only.", "author_email": "hana@acme.com",
     "author_groups": ["people"], "readers": ["hana@acme.com"],
     "identifier": "ENG-103", "state": "Backlog", "priority": "P3"},
]


def _build(data_dir: Path) -> Settings:
    from app.importer.byo import load

    settings = Settings(data_dir=data_dir)
    corpus = data_dir / "_corpus.jsonl"
    corpus.write_text("\n".join(json.dumps(r) for r in SAMPLE))
    load(corpus, settings)
    return settings


@pytest.fixture
def sample_corpus_path(tmp_path) -> Path:
    """The in-code SAMPLE written to a JSONL tempfile (for corpus-file tests)."""
    p = tmp_path / "sample.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in SAMPLE))
    return p


@pytest.fixture(scope="session")
def sample_settings(tmp_path_factory) -> Settings:
    """Build the SAMPLE corpus into a DB + ACL once for the whole session."""
    return _build(tmp_path_factory.mktemp("sample"))


@pytest.fixture
def db(sample_settings):
    conn = store.connect_ro(sample_settings.db_path)
    yield conn
    conn.close()


@pytest.fixture
def acl(sample_settings) -> Acl:
    return Acl.load(sample_settings.tokens_path, sample_settings.admin_token, sample_settings.org_name)


@pytest.fixture
def tokens(sample_settings) -> dict[str, str]:
    data = yaml.safe_load(sample_settings.tokens_path.read_text())
    return {u["email"]: u["token"] for u in data["users"]}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server(sample_settings):
    """The SAMPLE DB served by a real uvicorn subprocess; yields ``(base_url, settings)``."""
    port = _free_port()
    env = {**os.environ, "MOCK_DATA_DIR": str(sample_settings.data_dir)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=REPO_ROOT, env=env,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=0.5) as r:
                    if r.status == 200:
                        break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        else:
            raise RuntimeError("mock server did not become ready")
        yield base, sample_settings
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
