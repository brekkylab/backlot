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
from pathlib import Path

import pytest
import yaml

import backlot
from backlot import store
from backlot.acl import Acl
from backlot.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent  # used by test_schema.py to find the BYO example

# One corpus for every test. Explicit doc_ids on the docs the ACL tests assert against.
SAMPLE = [
    {
        "source_type": "confluence",
        "doc_id": "cf-handbook",
        "space": "handbook",
        "group": "engineering",
        "title": "Engineering Handbook",
        "content": "How we build software: standards, review, on-call.",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "labels": ["engineering", "handbook"],
    },
    {
        "source_type": "confluence",
        "doc_id": "cf-oncall",
        "parent": "cf-handbook",
        "space": "handbook",
        "group": "engineering",
        "title": "On-call Runbook",
        "content": "Respond to gateway 502s: check dashboards, roll back, page on-call.",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "labels": ["oncall", "runbook"],
        "comments": [
            {"content": "Add the rate-limiter rollback step.", "author_email": "bob@acme.com"}
        ],
    },
    {
        "source_type": "confluence",
        "doc_id": "cf-comp",
        "space": "people-ops",
        "group": "people",
        "title": "Compensation Bands 2026",
        "content": "Confidential salary bands. People team only.",
        "author_email": "hana@acme.com",
        "author_groups": ["people"],
        "visibility": "group",
    },
    {
        "source_type": "slack",
        "channel": "eng-announcements",
        "group": "engineering",
        "content": "Reminder: production deploy freeze starts Friday.",
        "author_email": "ava@acme.com",
        "visibility": "public",
    },
    {
        "source_type": "slack",
        "channel": "incidents",
        "group": "engineering",
        "content": "Anyone else seeing 502s from the gateway?",
        "author_email": "bob@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "reactions": [{"name": "eyes", "count": 2, "users": ["U01", "U02"]}],
        "replies": [
            {"content": "Yeah, looking now.", "author_email": "ava@acme.com"},
            {"content": "Rolled back; 502s clearing.", "author_email": "bob@acme.com"},
        ],
    },
    {
        "source_type": "slack",
        "channel": "people-confidential",
        "group": "people",
        "content": "Confidential people-ops note: Q3 reorg headcount plan.",
        "author_email": "hana@acme.com",
        "author_groups": ["people"],
        "visibility": "group",
    },
    {
        "source_type": "github",
        "doc_id": "gh-issue-1",
        "repo": "gateway",
        "group": "engineering",
        "title": "Rate limiter drops bursts under 50ms",
        "content": "Token-bucket refill is off by one tick.",
        "author_email": "bob@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "meta": {"state": "open", "labels": ["bug", "gateway"]},
        "comments": [{"content": "Confirmed with a repro test.", "author_email": "ava@acme.com"}],
    },
    {
        "source_type": "github",
        "doc_id": "gh-pr-1",
        "repo": "gateway",
        "group": "engineering",
        "title": "Fix token-bucket refill off-by-one",
        "content": "Corrects the refill tick; adds a test.",
        "author_email": "bob@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "subtype": "pull_request",
        "meta": {
            "state": "closed",
            "merged_at": "2026-02-10T12:00:00Z",
            "head": "fix/rl",
            "base": "main",
            "labels": ["bug"],
            "reviews": [{"author_email": "ava@acme.com", "state": "APPROVED", "body": "LGTM."}],
        },
        "comments": [
            {"content": "Add a metric for dropped bursts?", "author_email": "ava@acme.com"}
        ],
    },
    {
        "source_type": "github",
        "doc_id": "gh-sec-1",
        "repo": "vault",
        "group": "people",
        "title": "Rotate quarterly signing keys",
        "content": "Track key rotation for the people-ops vault.",
        "author_email": "hana@acme.com",
        "author_groups": ["people"],
        "visibility": "group",
        "meta": {"state": "open", "labels": ["security"]},
    },
    {
        "source_type": "jira",
        "doc_id": "jira-sev2",
        "project": "payments",
        "group": "payments",
        "title": "SEV2: checkout latency spike",
        "content": "p95 checkout latency jumped to 2.1s.",
        "author_email": "bob@acme.com",
        "author_groups": ["payments", "engineering"],
        "visibility": "group",
        "meta": {
            "status": "In Progress",
            "issuetype": "Incident",
            "priority": "High",
            "issuelinks": [
                {
                    "id": "1",
                    "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                    "outwardIssue": {
                        "key": "PAY-42",
                        "fields": {
                            "summary": "Right-size the pool",
                            "status": {"name": "To Do"},
                            "issuetype": {"name": "Task"},
                        },
                    },
                }
            ],
        },
        "comments": [
            {"content": "Rolled back; latency recovering.", "author_email": "ava@acme.com"},
            {"content": "p95 back to ~240ms.", "author_email": "bob@acme.com"},
        ],
    },
    {
        "source_type": "jira",
        "doc_id": "jira-sub1",
        "parent": "jira-sev2",
        "project": "payments",
        "group": "payments",
        "title": "Write postmortem for the SEV2",
        "content": "Draft the postmortem.",
        "author_email": "ava@acme.com",
        "author_groups": ["payments", "engineering"],
        "visibility": "group",
        "meta": {"issuetype": "Sub-task", "status": "To Do"},
    },
    {
        "source_type": "jira",
        "doc_id": "jira-private",
        "project": "payments",
        "group": "payments",
        "title": "Personal task: rotate my API keys",
        "content": "Private note to self.",
        "author_email": "bob@acme.com",
        "visibility": "private",
    },
    {
        "source_type": "gmail",
        "mailbox": "ceo",
        "title": "Q1 board deck draft",
        "content": "Draft narrative for the Q1 board meeting.",
        "author_email": "ceo@acme.com",
        "readers": ["ceo@acme.com", "ava@acme.com"],
        "cc": "cfo@acme.com",
        "attachments": [
            {
                "filename": "Q1-deck.pdf",
                "mime": "application/pdf",
                "size": 2048,
                "content": "PDF bytes placeholder",
            }
        ],
    },
    {
        "source_type": "gmail",
        "mailbox": "cfo",
        "title": "Confidential comp review",
        "content": "Q3 compensation adjustments — do not forward.",
        "author_email": "cfo@acme.com",
        "readers": ["cfo@acme.com"],
    },
    # A threaded exchange. Every other gmail doc here is its own thread root, so the reply->root
    # mapping — which `threadId` reports and which the served hex ids have to agree on — had no
    # coverage at all, while the bench corpus is 121,390 threads.
    {
        "source_type": "gmail",
        "doc_id": "gm-thread-root",
        "mailbox": "ava",
        "title": "Gateway retry storm",
        "content": "Seeing repeated 502s from the gateway.",
        "author_email": "ava@acme.com",
        "readers": ["ava@acme.com", "bob@acme.com"],
    },
    {
        "source_type": "gmail",
        "doc_id": "gm-thread-reply",
        "thread": "gm-thread-root",
        "mailbox": "ava",
        "title": "Re: Gateway retry storm",
        "content": "Rolled back the rate limiter; 502s clearing.",
        "author_email": "bob@acme.com",
        "readers": ["ava@acme.com", "bob@acme.com"],
    },
    {
        "source_type": "google_drive",
        "folder": "marketing",
        "group": "marketing",
        "title": "Brand guidelines v3",
        "content": "Logo usage, palette, typography.",
        "author_email": "mia@acme.com",
        "author_groups": ["marketing"],
        "visibility": "public",
        "subtype": "document",
    },
    {
        "source_type": "google_drive",
        "folder": "finance",
        "group": "finance",
        "title": "Q1 Revenue Model",
        "content": "month,revenue\nJan,120000\nFeb,135000",
        "author_email": "cfo@acme.com",
        "author_groups": ["finance"],
        "visibility": "group",
        "subtype": "spreadsheet",
    },
    {
        "source_type": "google_drive",
        "folder": "marketing",
        "group": "marketing",
        "title": "All-hands Q1 Deck",
        "content": "Slide 1\n\nSlide 2",
        "author_email": "mia@acme.com",
        "author_groups": ["marketing"],
        "visibility": "public",
        "subtype": "presentation",
    },
    {
        "source_type": "google_drive",
        "folder": "security",
        "group": "security-compliance",
        "title": "Security Whitepaper.pdf",
        "content": "%PDF-1.7 placeholder.",
        "author_email": "sec@acme.com",
        "author_groups": ["security-compliance"],
        "visibility": "public",
        "subtype": "pdf",
        "meta": {"mime_type": "application/pdf"},
    },
    # A sheet whose content carries a BLANK line, which the real corpus is full of (its spreadsheets
    # are prose). Real Sheets returns an interior blank row as `[]`, not `[""]`, and nothing else in
    # SAMPLE has one — a values read of it used to raise IndexError only against real data.
    {
        "source_type": "google_drive",
        "doc_id": "gd-blankline",
        "folder": "finance",
        "group": "finance",
        "title": "Ledger With Gaps",
        "content": "header\n\nrow after gap\n\n",
        "author_email": "cfo@acme.com",
        "author_groups": ["finance"],
        "visibility": "public",
        "subtype": "spreadsheet",
    },
    # An Office upload, not a native Sheet. Real Google answers an Office file differently from
    # both a native type and a plain binary — only the API owning its family (Sheets, for .xlsx)
    # returns the "must not be an Office file" precondition — so the corpus needs one to test it.
    {
        "source_type": "google_drive",
        "folder": "finance",
        "group": "finance",
        "title": "Budget Rollup.xlsx",
        "content": "binary xlsx placeholder",
        "author_email": "cfo@acme.com",
        "author_groups": ["finance"],
        "visibility": "public",
        "subtype": "xlsx",
        "meta": {"mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    },
    {
        "source_type": "notion",
        "doc_id": "nt-runbook",
        "teamspace": "engineering",
        "group": "engineering",
        "title": "Notion On-call Runbook",
        "content": "# On-call\n\nCheck dashboards, roll back, page on-call.",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "icon": "📟",
        "comments": [{"content": "add rate-limiter step", "author_email": "bob@acme.com"}],
    },
    {
        "source_type": "notion",
        "doc_id": "nt-tasks-db",
        "subtype": "database",
        "teamspace": "engineering",
        "group": "engineering",
        "title": "Eng Tasks",
        "content": "Team task tracker.",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "properties": {"Status": {"type": "select"}, "Priority": {"type": "select"}},
    },
    {
        "source_type": "notion",
        "doc_id": "nt-task-1",
        "parent": "nt-tasks-db",
        "teamspace": "engineering",
        "group": "engineering",
        "title": "Fix gateway 502s",
        "content": "Investigate token-bucket refill.",
        "author_email": "bob@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "properties": {"Status": "In Progress", "Priority": "High"},
    },
    {
        "source_type": "notion",
        "doc_id": "nt-secret",
        "teamspace": "people-ops",
        "group": "people",
        "title": "Comp planning notes",
        "content": "Confidential.",
        "author_email": "hana@acme.com",
        "author_groups": ["people"],
        "visibility": "group",
    },
    {
        "source_type": "s3",
        "doc_id": "s3-runbook",
        "bucket": "eng-artifacts",
        "group": "engineering",
        "key": "runbooks/oncall.md",
        "title": "On-call Runbook",
        "content": "Check dashboards, roll back, page on-call.",
        "content_type": "text/markdown",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
    },
    {
        "source_type": "s3",
        "doc_id": "s3-arch",
        "bucket": "eng-artifacts",
        "group": "engineering",
        "key": "design/architecture.md",
        "title": "Architecture",
        "content": "Gateway, workers, and the token bucket.",
        "content_type": "text/markdown",
        "author_email": "bob@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
    },
    {
        "source_type": "s3",
        "doc_id": "s3-secret",
        "bucket": "people-vault",
        "group": "people",
        "key": "comp/bands.csv",
        "title": "Comp Bands",
        "content": "band,min,max\nL5,180,220",
        "content_type": "text/csv",
        "author_email": "hana@acme.com",
        "author_groups": ["people"],
        "visibility": "group",
    },
    # HubSpot: the object type is the container, so these span three of them. The contact and the
    # note are associated with the company (declared once; the loader writes both directions).
    {
        "source_type": "hubspot",
        "doc_id": "hs-co-acme",
        "object_type": "companies",
        "group": "sales",
        "title": "Acme Health",
        "content": "Acme Health — mid-market healthcare provider evaluating the platform.",
        "author_email": "rep@acme.com",
        "author_groups": ["sales"],
        "visibility": "public",
        "properties": {
            "name": "Acme Health",
            "domain": "acme-health.com",
            "industry": "healthcare",
            "lifecyclestage": "evaluation",
            "employees": "150",
            "founded": "2011-03-01",
        },
    },
    {
        "source_type": "hubspot",
        "doc_id": "hs-c-ava",
        "object_type": "contacts",
        "group": "sales",
        "title": "Ava Stone",
        "content": "Ava Stone — VP Platform at Acme Health.",
        "author_email": "rep@acme.com",
        "author_groups": ["sales"],
        "visibility": "public",
        "properties": {"firstname": "Ava", "lastname": "Stone", "email": "ava@acme-health.com"},
        "associations": [{"to": "hs-co-acme", "label": "Primary"}],
    },
    {
        "source_type": "hubspot",
        "doc_id": "hs-note-1",
        "object_type": "notes",
        "group": "sales",
        "title": "",
        "content": "Security review scheduled; wants EU data residency.",
        "author_email": "rep@acme.com",
        "author_groups": ["sales"],
        "visibility": "public",
        "properties": {"hs_note_body": "Security review scheduled; wants EU data residency."},
        "associations": [{"to": "hs-co-acme"}],
    },
    # Restricted by explicit readers rather than group visibility, so the `companies` container
    # keeps a single owning group while still giving the ACL tests a hidden CRM record.
    {
        "source_type": "hubspot",
        "doc_id": "hs-co-secret",
        "object_type": "companies",
        "group": "sales",
        "title": "Stealth Health Co",
        "content": "Confidential account under NDA — people team only.",
        "author_email": "hana@acme.com",
        "author_groups": ["people"],
        "readers": ["hana@acme.com"],
        "properties": {"name": "Stealth Health Co", "lifecyclestage": "qualified"},
    },
    # Two more companies so a small page size actually produces a cursor: with only two rows a
    # `limit=2` crawl never takes the paging branch, and the cursor path would go untested.
    {
        "source_type": "hubspot",
        "doc_id": "hs-co-borealis",
        "object_type": "companies",
        "group": "sales",
        "title": "Borealis Clinics",
        "content": "Regional clinic network, procurement stage.",
        "author_email": "rep@acme.com",
        "author_groups": ["sales"],
        "visibility": "public",
        "properties": {
            "name": "Borealis Clinics",
            "domain": "borealis.example",
            "industry": "healthcare",
            "lifecyclestage": "procurement",
            "employees": "400",
            "founded": "2014-06-01",
        },
    },
    # `archived` is only meaningful if something is archived: this row is excluded from the default
    # listing and is the only row the archived view returns.
    {
        "source_type": "hubspot",
        "doc_id": "hs-co-defunct",
        "object_type": "companies",
        "group": "sales",
        "title": "Defunct Labs",
        "content": "Churned; record archived.",
        "author_email": "rep@acme.com",
        "author_groups": ["sales"],
        "visibility": "public",
        "archived": True,
        "properties": {"name": "Defunct Labs", "lifecyclestage": "qualified", "employees": "12"},
    },
    # Linear: the team is the container, so these span two of them. `lin-rl` carries the full
    # surface the LlamaIndex reader dereferences (state/project/labels/creator/assignee/estimate/
    # dueDate/branchName) plus comments; `lin-secret` is the hidden one the ACL tests assert on.
    {
        "source_type": "linear",
        "doc_id": "lin-rl",
        "team": "engineering",
        "group": "engineering",
        "title": "Rate limiter drops bursts under 50ms",
        "content": "Token-bucket refill is off by one tick under sustained burst load.",
        "author_email": "ava@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "identifier": "ENG-101",
        "state": "In Progress",
        "priority": "P1",
        "estimate": 5,
        "labels": ["bug", "gateway"],
        "project": "runtime-stability",
        "cycle": "2025-W08",
        "dueDate": "2026-03-15",
        "assignee": "bob@acme.com",
        "assigneeName": "Bob Stone",
        "created": "2026-02-18T00:00:00Z",
        "updated": "2026-03-04T00:00:00Z",
        "comments": [
            {"content": "Reproduced with a burst test.", "author_email": "bob@acme.com"},
            {"content": "Fix is in review.", "author_email": "ava@acme.com"},
        ],
    },
    {
        "source_type": "linear",
        "doc_id": "lin-batch",
        "team": "engineering",
        "group": "engineering",
        "title": "Continuous batching stalls after compaction",
        "content": "A 50ms stall when the batcher merges requests right after compaction.",
        "author_email": "bob@acme.com",
        "author_groups": ["engineering"],
        "visibility": "public",
        "identifier": "ENG-102",
        "state": "Done",
        "priority": "P0",
        "estimate": 3,
        "labels": ["latency"],
        "project": "runtime-stability",
        # Parented to the reader-restricted issue, so `Issue.parent` gets ACL coverage.
        "parent": "ENG-103",
        "release": "runtime-1.19",
        "attachments": [
            "https://ci.acme.test/builds/4821/artifacts.zip",
            {"url": "https://conf.acme.test/design/batching", "title": "Design doc"},
        ],
        # Relates to the RESTRICTED issue, so relation ACL scoping is covered too.
        "relations": [{"to": "lin-rl", "type": "blocks"}, {"to": "lin-secret", "type": "related"}],
        "completedAt": "2026-03-10T00:00:00Z",
        "created": "2026-03-01T00:00:00Z",
        "updated": "2026-03-10T00:00:00Z",
    },
    {
        "source_type": "linear",
        "doc_id": "lin-des",
        "team": "design",
        "group": "design",
        "title": "Revamp field states and selects",
        "content": "Focus rings, hover and disabled states for selects.",
        "author_email": "mia@acme.com",
        "author_groups": ["design"],
        "visibility": "public",
        "identifier": "DES-77",
        "state": "In Review",
        "priority": "P2",
        "labels": ["tokens"],
    },
    # Restricted by explicit readers, so the `engineering` team keeps one owning group while the
    # ACL tests still have a Linear issue that must stay hidden. It carries a project, cycle,
    # labels AND an assignee that appear on NO other issue, so each by-id relation root has an
    # entity only this issue can reach — without that, the ACL tests would only ever exercise the
    # `state` and `creator` predicates and the other four could be broken undetected.
    {
        "source_type": "linear",
        "doc_id": "lin-secret",
        "team": "engineering",
        "group": "engineering",
        "title": "Rotate the signing keys",
        "content": "Key rotation runbook — people team only.",
        "author_email": "hana@acme.com",
        "author_groups": ["people"],
        "readers": ["hana@acme.com"],
        "identifier": "ENG-103",
        "state": "Backlog",
        "priority": "P3",
        "project": "vault-rotation",
        "cycle": "2026-W40-embargo",
        "labels": ["restricted-only"],
        "assignee": "vault.keeper@acme.com",
        "assigneeName": "Vault Keeper",
        "comments": [{"content": "Rotation window agreed.", "author_email": "hana@acme.com"}],
    },
    # A team no ACL-restricted caller can see into, so `teams` and `team(id:)` can be checked for
    # agreement. Only hana is granted it.
    {
        "source_type": "linear",
        "doc_id": "lin-blackops",
        "team": "blackops",
        "group": "people",
        "title": "Sealed programme",
        "content": "Sealed.",
        "author_email": "hana@acme.com",
        "author_groups": ["people"],
        "readers": ["hana@acme.com"],
        "identifier": "BLA-1",
        "state": "Triage",
    },
    # --- fireflies: meeting transcripts -------------------------------------------------
    # Structured `sentences` (the child-row form), so content is DERIVED from them and the
    # round-trip is exercised. Carries a null-speaker sentence, a repeated speaker (so speaker_id
    # ordinals are checked for reuse), summary notes and explicit timings.
    {
        "source_type": "fireflies",
        "doc_id": "ff-discovery",
        "channel": "sales-calls",
        "group": "engineering",
        "title": "Acme x Northwind — latency discovery",
        "host_email": "ava@acme.com",
        "host_name": "Ava Chen",
        "author_groups": ["engineering"],
        "visibility": "public",
        "duration": 32.5,
        "calendar_id": "cal-nw-1",
        "created": "2026-04-02T15:00:00Z",
        "summary": {
            "overview": "Northwind wants sub-300ms p95 on batching.",
            "topics_discussed": ["latency budget", "batching", "pricing"],
            "action_items": ["Ava: send the batching benchmark"],
            "keywords": ["latency", "batching"],
            "meeting_type": "discovery",
        },
        "meeting_attendees": [
            {"displayName": "Ava Chen", "email": "ava@acme.com", "location": None},
            {
                "displayName": "Dana Ruiz",
                "email": "dana@northwind.example",
                "location": "Northwind",
            },
        ],
        "sentences": [
            {
                "speaker_name": "Ava Chen",
                "author_email": "ava@acme.com",
                "start_time": 0,
                "text": "Thanks for joining — let's start with the latency budget.",
            },
            {
                "speaker_name": "Dana Ruiz",
                "start_time": 14,
                "text": "Our p95 sits around 300ms today and batching is the suspect.",
            },
            {
                "speaker_name": "Ava Chen",
                "author_email": "ava@acme.com",
                "start_time": 31,
                "text": "Understood. I'll send the batching benchmark after this.",
            },
            {"speaker_name": None, "start_time": 46, "text": "(crosstalk)"},
        ],
    },
    # Only a `content` body, no `sentences` — the parse-it-back path, including a continuation
    # line that must fold into the sentence above it rather than becoming its own.
    {
        "source_type": "fireflies",
        "doc_id": "ff-allhands",
        "channel": "all-hands",
        "group": "people",
        "title": "April all-hands",
        "author_email": "hana@acme.com",
        "visibility": "public",
        "created": "2026-04-10T16:00:00Z",
        "content": "[00:00] Hana: welcome everyone, quick numbers first.\n"
        "[00:30] Mia: design shipped the new selects.\n"
        "We also cleared the focus-ring backlog.\n"
        "[01:15] Hana: great — that's a wrap.",
    },
    # Restricted by explicit readers, so the ACL tests have a transcript that must stay hidden
    # from everyone but hana (its channel is one no other transcript uses, so a channel_id filter
    # can be checked for leaking too).
    {
        "source_type": "fireflies",
        "doc_id": "ff-secret",
        "channel": "board",
        "group": "people",
        "title": "Board pre-read walkthrough",
        "host_email": "hana@acme.com",
        "readers": ["hana@acme.com"],
        "duration": 61.0,
        "created": "2026-04-15T09:00:00Z",
        "summary": {"overview": "Sealed board pre-read.", "meeting_type": "other"},
        "sentences": [
            {
                "speaker_name": "Hana Ito",
                "author_email": "hana@acme.com",
                "start_time": 0,
                "text": "This one stays in the room.",
            }
        ],
    },
]


def _build(data_dir: Path) -> Settings:
    from backlot.importer.byo import load

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
    return Acl.load(
        sample_settings.tokens_path, sample_settings.admin_token, sample_settings.org_name
    )


@pytest.fixture
def tokens(sample_settings) -> dict[str, str]:
    data = yaml.safe_load(sample_settings.tokens_path.read_text())
    return {u["email"]: u["token"] for u in data["users"]}


@pytest.fixture(scope="module")
def client(sample_settings):
    """A TestClient over the SAMPLE DB, not the ambient ``data/`` import.

    Module-scoped, so each vendor's test file gets its own — a lifespan over SAMPLE costs ~5ms once
    the corpus is built, and sharing one session-wide would collide with the tests that reload
    ``backlot.main`` to serve a different DB."""
    from tests._helpers import client_for

    with client_for(sample_settings) as c:
        yield c


@pytest.fixture(scope="module")
def tokens_yaml(sample_settings) -> dict:
    """``tokens.yaml`` verbatim — ``admin_token`` plus the ``users`` list.

    NOT :func:`tokens`, which is the ``{email: token}`` map. Two shapes, two names: the same
    fixture name meaning different things per file is how a test ends up asserting on the wrong
    one."""
    return yaml.safe_load(sample_settings.tokens_path.read_text())


@pytest.fixture(scope="module")
def admin_h(tokens_yaml) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens_yaml['admin_token']}"}


@pytest.fixture(scope="module")
def org(client) -> str:
    """The org the mock derived from the corpus (SAMPLE is @acme.com -> 'acme')."""
    return client.get("/_mock/users").json()["org"]


@pytest.fixture(scope="module")
def ro_conn(sample_settings):
    """A read-only connection to the SAMPLE DB, module-scoped (cf. the per-test ``db``)."""
    conn = store.connect_ro(sample_settings.db_path)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def live_server(sample_settings):
    """SAMPLE served by a real uvicorn subprocess (via ``backlot.mock_server``); yields
    ``(base_url, settings)`` for the official SDKs and the Dockerised MCP server, which make real
    HTTP calls rather than going through the in-process ``TestClient``.

    A separate build from ``sample_settings``'s own tempdir, but from the identical SAMPLE list —
    doc ids and per-user tokens are deterministic hashes of the record content (see
    ``backlot.importer.byo``), so the two builds agree on everything the tests read from
    ``settings`` (``admin_token``, ``tokens_path``) even though they're different directories.
    """
    with backlot.mock_server(SAMPLE) as m:
        yield m.base_url, sample_settings
