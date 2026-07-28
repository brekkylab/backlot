import json
import os
import shutil
import sqlite3
import subprocess
import sys
import types
import urllib.request
from pathlib import Path

import certifi
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import pytest
import yaml

from app import store
from app.config import get_settings
from app.importer import erb
from app.importer.erb import Principals, canonical, grants_for

C = erb


# ---------------------------------------------------------------------------
# from test_erb_source.py
# ---------------------------------------------------------------------------

def test_derive_title_content_scalar():
    raw = {"title_field_name": "title", "content_field_names": ["body", "body_addendum"],
           "title": "Doc A", "body": "hello", "body_addendum": "world"}
    title, content = erb.derive_title_content(raw)
    assert title == "Doc A"
    assert "hello" in content and "world" in content


def test_derive_title_content_list_field():
    raw = {"title_field_name": "channel", "content_field_names": ["messages"],
           "channel": "eng-infra", "messages": "Alex: hi\nMaria: yo"}
    title, content = erb.derive_title_content(raw)
    assert title == "eng-infra"
    assert "Alex: hi" in content


def test_supported_sources():
    assert erb.SUPPORTED == ("slack", "gmail", "google_drive", "github", "jira", "confluence",
                             "hubspot", "linear")


def test_erb_sources_are_registered_in_the_store():
    # every source the bench importer loads must have a table/grouping registered
    assert set(erb.SUPPORTED) <= set(store.SOURCE_TABLE)
    for src in erb.SUPPORTED:
        assert store.table(src) and store.grouping_col(src)


# The source directories EnterpriseRAG-Bench ships, with their entry counts in the release
# tarball's generated_data/sources/: slack 285,644 / gmail 121,448 / linear 35,315 /
# google_drive 25,142 / hubspot 15,020 / fireflies 10,182 / github 8,078 / jira 6,126 /
# confluence 5,313. Read these from the tarball: `fetch_generated_data` extracts only SUPPORTED,
# so an extracted generated_data/ dir reflects the importer's coverage, not the bench's contents.
BENCH_SOURCES = {"slack", "gmail", "google_drive", "github", "jira", "confluence",
                 "hubspot", "linear", "fireflies"}


def test_unloaded_bench_sources_are_declared():
    """Linear and Fireflies ship bench data that no loader consumes yet; the set bounds which
    sources may be missing a loader, so adding one to the store forces a decision here."""
    assert set(erb.SUPPORTED) < BENCH_SOURCES
    assert BENCH_SOURCES - set(erb.SUPPORTED) <= {"linear", "fireflies"}


def test_byo_only_sources_have_no_bench_representation():
    """Notion and S3 are the only sources the mock serves that the bench does not ship, so they can
    arrive solely through a BYO corpus."""
    assert set(store.SOURCE_TABLE) - BENCH_SOURCES == {"notion", "s3"}


# ---------------------------------------------------------------------------
# from test_principals.py
# ---------------------------------------------------------------------------

EMPLOYEES = [
    {"name": "Ava Chen", "email": "ava.chen@redwoodinference.com", "dept_slug": "engineering"},
]


def _p():
    return Principals(list(EMPLOYEES), "redwoodinference.com")


def test_canonical_strips_punctuation_and_case():
    assert canonical("Connor O'Brien") == canonical("Connor OBrien") == "connorobrien"
    assert canonical("Ava  Chen") == "avachen"


def test_canonical_drops_middle_initials():
    # 'Aisha K. Patel' and 'Aisha Patel' are the same person; 'Asha Patel' is not
    assert canonical("Aisha K. Patel") == canonical("Aisha Patel") == "aishapatel"
    assert canonical("Asha Patel") == "ashapatel" != "aishapatel"


def test_resolve_directory_match():
    p = _p()
    assert p.resolve("Ava Chen", role="author") == "ava.chen@redwoodinference.com"


def test_resolve_synthesizes_internal_user():
    p = _p()
    email = p.resolve("Maya Chen", role="owner", group_hint="research-applied-ml")
    assert email == "maya.chen@redwoodinference.com"
    assert p.users[email]["group"] == "research-applied-ml"
    assert p.users[email]["external"] is False


def test_resolve_external_parses_email_and_is_not_registered():
    # 'Name <email>' → the real email, deduped by email; never becomes an org principal/user
    p = _p()
    email = p.resolve("Alyssa Chen <alyssa.chen@cascadefg.com>", role="participant_external")
    assert email == "alyssa.chen@cascadefg.com"
    assert email not in p.users  # externals are recipients, not org users


def test_resolve_external_bare_name_offdomain_and_not_registered():
    p = _p()
    email = p.resolve("Dana Ext", role="participant_external")
    assert not email.endswith("@redwoodinference.com")
    assert email not in p.users


def test_resolve_slack_speaker_is_label_not_registered():
    # first-names/bots become display labels only — not org users
    p = _p()
    email = p.resolve("infra-bot", role="slack_participant")
    assert email == "infrabot@redwoodinference.com"  # _slug strips the hyphen
    assert email not in p.users
    assert "alex@redwoodinference.com" == p.resolve("Alex", role="slack_participant")
    assert "alex@redwoodinference.com" not in p.users


def test_resolve_rejects_non_person_junk():
    # a lone single-word token in a name field is not a person → not minted
    p = _p()
    assert p.resolve("Note", role="author") is None
    assert "note@redwoodinference.com" not in p.users


def test_harvest_gmail_email_wins_over_synthesis():
    p = _p()
    rec = ("gmail", "dsid_x", {"title_field_name": "subject", "content_field_names": ["messages"],
            "subject": "s", "messages": ["From: Maya Chen <maya_chen@redwoodinference.com>\nTo: x\n\nhi"]})
    p.harvest_gmail_emails([rec])
    assert p.resolve("Maya Chen", role="author") == "maya_chen@redwoodinference.com"


def test_harvest_skips_alias_header_names():
    # a header alias like 'On-Call (SRE) <oncall@…>' is not a person → not harvested as a user
    p = _p()
    rec = ("gmail", "dsid_y", {"title_field_name": "subject", "content_field_names": ["messages"],
            "subject": "s", "messages": ["From: On-Call (SRE) <oncall@redwoodinference.com>\n\nhi"]})
    p.harvest_gmail_emails([rec])
    assert "oncall@redwoodinference.com" not in p.users


def _p_multi():
    employees = [
        {"name": "Ava Chen", "email": "ava.chen@redwoodinference.com", "dept_slug": "engineering"},
        {"name": "Maya Chen", "email": "maya.chen@redwoodinference.com", "dept_slug": "security-compliance"},
        {"name": "Priya Desai", "email": "priya.desai@redwoodinference.com", "dept_slug": "applied-ml-research"},
    ]
    return Principals(employees, "redwoodinference.com")


def test_canonical_group_reconciles_partial_team_label():
    p = _p_multi()
    assert p.canonical_group("security") == "security-compliance"


def test_canonical_group_exact_match():
    p = _p_multi()
    assert p.canonical_group("engineering") == "engineering"


def test_canonical_group_unknown_team_is_its_own_group():
    p = _p_multi()
    assert p.canonical_group("some-unknown-team") == "some-unknown-team"


def test_write_tokens_is_directory_only(tmp_path):
    import types, yaml as _yaml
    p = Principals([{"name": "Ava Chen", "email": "ava.chen@redwoodinference.com",
                     "dept_slug": "engineering"}], "redwoodinference.com")
    p.resolve("Maya Chen", role="owner", group_hint="engineering")   # synthesized, non-directory
    p.resolve("Wei Chen", role="reviewer")                            # synthesized, non-directory
    st = types.SimpleNamespace(tokens_path=tmp_path / "tokens.yaml", org_name="redwood",
                               org_domain="redwoodinference.com", admin_token="admin-service-token")
    p.write_tokens(st)
    d = _yaml.safe_load(st.tokens_path.read_text())
    emails = {u["email"] for u in d["users"]}
    assert emails == {"ava.chen@redwoodinference.com"}   # only the directory employee
    assert "maya.chen@redwoodinference.com" not in emails


def test_canonical_folds_accents():
    assert canonical("Tomáš Novák") == canonical("Tomas Novak") == "tomasnovak"


def test_mint_does_not_clobber_directory_user(tmp_path):
    # an accented/titled directory name whose doc-reference doesn't canonical-match must still
    # keep its directory flag (the colliding mint must not overwrite it) → stays tokened
    import types, yaml as _yaml
    p = Principals([{"name": "Tomáš Novák", "email": "tomas.novak@redwoodinference.com",
                     "dept_slug": "engineering"}], "redwoodinference.com")
    # a doc references the plain spelling; folded canonical now matches → resolves to the dir user
    assert p.resolve("Tomas Novak", role="owner") == "tomas.novak@redwoodinference.com"
    assert p.users["tomas.novak@redwoodinference.com"].get("directory") is True
    st = types.SimpleNamespace(tokens_path=tmp_path / "t.yaml", org_name="redwood",
                               org_domain="redwoodinference.com", admin_token="admin-service-token")
    p.write_tokens(st)
    assert "tomas.novak@redwoodinference.com" in {u["email"] for u in _yaml.safe_load(st.tokens_path.read_text())["users"]}


# ---------------------------------------------------------------------------
# from test_conversations.py
# ---------------------------------------------------------------------------

def test_parse_gmail_thread():
    msgs = ["From: Vivek K <vivek_k@redwoodinference.com>\n"
            "To: Connor O'Brien <connor_obrien@redwoodinference.com>\n"
            "Date: Wed, May 14, 2025 at 9:12 AM PT\nSubject: Beta plan\n\nBody one.",
            "From: Connor O'Brien <connor_obrien@redwoodinference.com>\n"
            "To: Vivek K <vivek_k@redwoodinference.com>\nDate: Wed, May 14, 2025 at 10:00 AM PT\n"
            "Subject: Re: Beta plan\n\nReply two."]
    out = C.parse_gmail_thread(msgs)
    assert len(out) == 2
    assert out[0]["from_email"] == "vivek_k@redwoodinference.com"
    assert out[0]["subject"] == "Beta plan"
    assert "Body one." in out[0]["body"]


def test_to_epoch_parses_bench_date_formats():
    # RFC 2822 email Date header (the bench's gmail format) — the big one: previously unparsed,
    # which left ~96% of gmail with NULL created_ts and a synthesized (fake) served date.
    assert C.to_epoch("Mon, 18 May 2026 09:02:00 -0700") == 1779120120   # 16:02Z
    assert C.to_epoch("Mon, 18 May 2026 10:17:00 -07:00") == 1779124620  # malformed colon offset
    # ISO 8601 with a numeric offset and with a trailing Z
    assert C.to_epoch("2026-05-18T09:02:00-07:00") == 1779120120
    assert C.to_epoch("2028-05-23T09:12:00Z") == 1842685920
    # timezone-ABBREVIATION formats (no numeric offset) — the bench's third gmail date shape
    assert C.to_epoch("2026-08-30 09:12 PDT") == 1788106320   # 16:12Z (PDT = -0700)
    assert C.to_epoch("2026-10-04 09:12 UTC") == 1791105120   # 09:12Z
    assert C.to_epoch("Wed, May 14, 2025 at 9:12 AM PT") == 1747242720  # 17:12Z (PT = -0800)
    # date-only, epoch string, and unparseable
    assert C.to_epoch("2025-11-05") == 1762300800
    assert C.to_epoch("1718326400") == 1718326400
    assert C.to_epoch("not a date") is None


def test_parse_jira_comments():
    out = C.parse_jira_comments(["2026-03-14 Jordan Kim: Filing request.",
                                 "2026-03-15 Priya Desai: On it."])
    assert out[0] == {"date": "2026-03-14", "name": "Jordan Kim", "body": "Filing request."}
    assert out[1]["name"] == "Priya Desai"


def test_parse_slack_transcript():
    out = C.parse_slack_transcript("Alex: hi there\ncontinued line\nMaria: yo\ninfra-bot: ping")
    assert out[0] == ("Alex", "hi there\ncontinued line")
    assert out[1] == ("Maria", "yo")
    assert out[2] == ("infra-bot", "ping")


def test_parse_slack_transcript_gates_on_participants():
    # a message-body line "A couple followups: ..." must NOT become a speaker (it's not a
    # participant) — it stays as body of the current turn, so no fake author is minted.
    out = C.parse_slack_transcript(
        "Alex: hey team\nA couple followups: can we warn on whitespace?\nMaria: sure",
        ["Alex", "Maria"])
    assert [s for s, _ in out] == ["Alex", "Maria"]
    assert "A couple followups: can we warn on whitespace?" in out[0][1]  # merged into Alex
    # participant match is tolerant of team labels / formatting, and the speaker is normalized to
    # the participant's canonical name: "Ben Jones" -> "ben.jones" (from "ben.jones (Acme)").
    out2 = C.parse_slack_transcript("Ben Jones: hi\nrandom note: x", ["ben.jones (Acme)"])
    assert [s for s, _ in out2] == ["ben.jones"] and "random note: x" in out2[0][1]
    # transcript variants collapse onto one participant identity (no variant-duplicate authors)
    out3 = C.parse_slack_transcript("Alex: a\nA lex: b\nMaria: c", ["alex", "maria"])
    assert [s for s, _ in out3] == ["alex", "alex", "maria"]


def test_parse_gmail_thread_handles_escaped_newlines():
    # some docs double-escape newlines (literal '\n'); body must still be extracted
    msg = "From: A <a@x.com>\\nTo: B <b@x.com>\\nDate: 2024-01-01\\nSubject: Hi\\n\\nThe body text."
    out = C.parse_gmail_thread([msg])
    assert len(out) == 1
    assert out[0]["from_email"] == "a@x.com" and out[0]["subject"] == "Hi"
    assert "The body text." in out[0]["body"] and out[0]["body"] != ""


def test_parse_slack_transcript_handles_escaped_newlines():
    out = C.parse_slack_transcript("alex: hi there\\nmaria: yo back")
    assert out == [("alex", "hi there"), ("maria", "yo back")]


def test_parse_slack_transcript_speaker_with_parenthetical_team():
    # Some bench docs label speakers "Name (Team):" — each turn must still split per speaker,
    # the parenthetical dropped so the name resolves against the directory.
    out = C.parse_slack_transcript(
        "Elena (CFO): Following up.\nDiego (Eng): thanks\nAsha (FinanceOps): filed it")
    assert out == [("Elena", "Following up."), ("Diego", "thanks"),
                   ("Asha", "filed it")]


# ---------------------------------------------------------------------------
# from test_acl_faithful.py
# ---------------------------------------------------------------------------

def test_drive_grants_owner_collaborators_and_group():
    g = grants_for("google_drive", {"owner": "a@x.com", "people": ["b@x.com"],
                                    "group": "finance", "confidentiality": None, "org": "redwood"})
    assert ("user", "a@x.com") in g and ("user", "b@x.com") in g
    assert ("group", "finance") in g


def test_gmail_is_private_no_org_or_group():
    g = grants_for("gmail", {"owner": "a@x.com", "people": ["b@x.com", "ext@external.example"],
                             "group": "sales", "confidentiality": None, "org": "redwood"})
    assert ("user", "a@x.com") in g
    assert not any(t == "org" or t == "group" for t, _ in g)


def test_confluence_confidentiality_scope():
    pub = grants_for("confluence", {"owner": "a@x.com", "people": [], "group": "eng",
                                    "confidentiality": "public", "org": "redwood"})
    assert ("org", "redwood") in pub
    restr = grants_for("confluence", {"owner": "a@x.com", "people": [], "group": "eng",
                                      "confidentiality": "restricted", "org": "redwood"})
    assert ("group", "eng") in restr and ("org", "redwood") not in restr


# ---------------------------------------------------------------------------
# from test_erb_load.py
# ---------------------------------------------------------------------------

def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store.SCHEMA)
    return c


def test_to_epoch_formats():
    assert erb.to_epoch("2025-09-18") is not None
    assert erb.to_epoch("Wed, May 14, 2025 at 9:12 AM PT") is not None
    assert erb.to_epoch(1710501234) == 1710501234
    assert erb.to_epoch("garbage") is None


def test_drive_owner_is_faithful():
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {"title_field_name": "title", "content_field_names": ["body"], "title": "Model",
           "body": "x", "owner": "Maya Chen", "collaborators": ["Ethan Park"],
           "team": "research-applied-ml", "created_at": "2025-09-18", "doc_type": "doc"}
    erb.load_drive(conn, "dsid_1", raw, P)
    row = conn.execute("SELECT author_email, owner_display, created_ts FROM gdrive_files WHERE doc_id='dsid_1'").fetchone()
    assert row["author_email"] == "maya.chen@redwoodinference.com"
    assert row["owner_display"] == "Maya Chen"
    assert row["created_ts"] is not None


def test_jira_assignee_reporter_and_duedate():
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {"title_field_name": "summary", "content_field_names": ["description"],
           "summary": "S", "description": "d", "reporter": "Jordan Kim", "assignee": "Priya Desai",
           "project": "INT", "status": "In Progress", "created_at": "2025-11-01"}
    erb.load_jira(conn, "dsid_2", raw, P)
    row = conn.execute("SELECT reporter_email, assignee_email, status FROM jira_issues WHERE doc_id='dsid_2'").fetchone()
    assert row["reporter_email"] == "jordan.kim@redwoodinference.com"
    assert row["assignee_email"] == "priya.desai@redwoodinference.com"
    assert row["status"] == "In Progress"


# --- HubSpot: bench company records mapped onto the mock's CRM schema -------------
# Shapes below mirror real bench records (data/raw/generated_data/sources/hubspot): `notes` is a
# list of undated CRM fragments, `timeline` is a dated activity log, and the `linked_*` arrays are
# free-text stubs pointing at other sources rather than resolvable document ids.
HS_RAW = {
    "title_field_name": "company_name",
    "content_field_names": ["next_step", "blockers", "timeline", "notes"],
    "company_id": "hub-00013452",
    "company_name": "Acacia Loop Services",
    "company_domain": "acacia-loop.com",
    "stage": "evaluation",
    "owner": "Maya Chen",
    "se_assigned": "Ethan Park",
    "csm_assigned": "Priya Desai",
    "created_at": "2025-11-05",
    "updated_at": "2026-03-10",
    "account_tier": "enterprise",
    "industry": "financial_services",
    "employee_count_range": "1000+",
    "hq_region": "eu",
    "next_step": "Finalize SLA + capacity-sizing workshop",
    "blockers": ["legal review of KMS/HSM integration"],
    "timeline": ["2026-02-18 - inbound signup via marketplace"],
    "notes": [
        "Inbound SMB — most traffic originates from US West customers",
        "Customer complaint: chat replies lag by ~1s+, wants <300ms median",
    ],
    "linked_fireflies": ["ff_2026-02-19_abbeygate_intro"],
    "linked_gmail_threads": ["gthread_1A9B2C_abbeygate_costs"],
    "linked_drive_docs": ["drive:/deals/abbeygate/pricing_deck_v3.pdf"],
    "linked_support_tickets": ["RINF-7421"],
}


def test_hubspot_company_maps_to_crm_properties():
    """The bench's denormalized company record is mapped onto the mock's HubSpot-API-shaped
    schema — not stored in ERB's own shape. Fields with a real HubSpot company property take that
    name; the rest stay as custom properties, which is what a real portal looks like."""
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    erb.load_hubspot(conn, "dsid_hs1", HS_RAW, P)
    row = conn.execute("SELECT * FROM hubspot_objects WHERE doc_id='dsid_hs1'").fetchone()
    assert row["object_type"] == "companies"
    assert row["title"] == "Acacia Loop Services"
    assert row["author_email"] == "maya.chen@redwoodinference.com"   # owner (AE), resolved
    assert row["owner_display"] == "Maya Chen"
    props = store.jcol(row, "properties", {})
    assert props["name"] == "Acacia Loop Services"
    assert props["domain"] == "acacia-loop.com"
    assert props["industry"] == "financial_services"
    assert props["lifecyclestage"] == "evaluation"                    # stage -> HubSpot's own name
    assert props["account_tier"] == "enterprise"                      # no default property -> custom
    assert row["created_ts"] == erb.to_epoch("2025-11-05")
    assert row["updated_ts"] == erb.to_epoch("2026-03-10")


def test_hubspot_notes_materialize_as_note_objects():
    """Real HubSpot models a note as its own object associated with the company, and this repo
    already parses embedded conversations into first-class rows on import — so each `notes` entry
    becomes a `notes` record linked to the company, not just text inside the company body."""
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    erb.load_hubspot(conn, "dsid_hs1", HS_RAW, P)
    notes = conn.execute("SELECT * FROM hubspot_objects WHERE object_type='notes' "
                         "ORDER BY doc_id").fetchall()
    assert len(notes) == 2
    assert notes[0]["content"].startswith("Inbound SMB")
    # API fidelity: a HubSpot note carries its body in hs_note_body
    assert store.jcol(notes[0], "properties", {})["hs_note_body"] == notes[0]["content"]
    # each note is associated with the company, in both directions
    assert [r["to_doc_id"] for r in store.hubspot_associations(conn, "dsid_hs1", "notes")] \
        == sorted(n["doc_id"] for n in notes)
    assert [r["to_doc_id"] for r in
            store.hubspot_associations(conn, notes[0]["doc_id"], "companies")] == ["dsid_hs1"]


def test_hubspot_timeline_stays_in_the_company_body():
    """`timeline` is a dated activity log the bench lists in content_field_names — it is the
    company's own text, not a set of note objects, so it must not be materialized."""
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    erb.load_hubspot(conn, "dsid_hs1", HS_RAW, P)
    company = conn.execute("SELECT content FROM hubspot_objects WHERE doc_id='dsid_hs1'").fetchone()
    assert "inbound signup via marketplace" in company["content"]
    assert conn.execute("SELECT COUNT(*) FROM hubspot_objects WHERE object_type='notes'"
                        ).fetchone()[0] == 2      # only the two `notes`, nothing from `timeline`


def test_hubspot_linked_artifacts_stay_property_stubs():
    """The bench's linked_* arrays are free-text references ("stubs/links" per the dataset's own
    agents.md), not resolvable doc ids — so they stay properties and must never become
    associations pointing at documents that do not exist."""
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    erb.load_hubspot(conn, "dsid_hs1", HS_RAW, P)
    props = store.jcol(
        conn.execute("SELECT properties FROM hubspot_objects WHERE doc_id='dsid_hs1'").fetchone(),
        "properties", {})
    assert props["linked_fireflies"] == ["ff_2026-02-19_abbeygate_intro"]
    assert props["linked_support_tickets"] == ["RINF-7421"]
    # the only associations are company <-> its own notes
    to_types = {r["to_type"] for r in conn.execute("SELECT to_type FROM hubspot_associations")}
    assert to_types == {"notes", "companies"}


def test_hubspot_bundle_names_owner_se_and_csm():
    """The AE owns the account; the SE and CSM are the other real people on it, so they belong in
    the ACL bundle the same way reviewers/collaborators do for other sources."""
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    bundle = erb.load_hubspot(conn, "dsid_hs1", HS_RAW, P)
    assert bundle["owner"] == "maya.chen@redwoodinference.com"
    assert set(bundle["people"]) == {"ethan.park@redwoodinference.com",
                                     "priya.desai@redwoodinference.com"}
    grants = grants_for("hubspot", {**bundle, "org": "redwood"})
    assert ("user", "maya.chen@redwoodinference.com") in grants


def test_hubspot_is_org_visible():
    """A CRM is team-wide, and the bench names ~3.3k account owners of whom only the ~167 in the
    employee directory can authenticate. Scoping a record to its owner (or to the object type's
    group, whose only members are those same synthesized owners) leaves the corpus visible to admin
    and to almost nobody else — so HubSpot gets an org grant, the way Slack does."""
    bundle = {"owner": "maya.chen@redwoodinference.com", "people": [], "group": "companies",
              "confidentiality": None, "org": "redwood"}
    grants = grants_for("hubspot", bundle)
    assert ("org", "redwood") in grants
    assert ("group", "companies") not in grants          # the org grant supersedes it
    assert ("user", "maya.chen@redwoodinference.com") in grants   # named people still granted


def test_confluence_restricted_grants_reconciled_directory_group():
    """A doc's team label ("security") must reconcile to the directory's actual dept_slug group
    ("security-compliance") for the ACL grant — not become its own empty group."""
    conn = _conn()
    employees = [
        {"name": "Priya Desai", "email": "priya.desai@redwoodinference.com",
         "dept_slug": "security-compliance"},
    ]
    P = Principals(employees, "redwoodinference.com")
    raw = {"title_field_name": "title", "content_field_names": ["body"], "title": "Sec Policy",
           "body": "x", "author": "Priya Desai", "owner_team": "security",
           "confidentiality": "restricted", "space": "SEC", "created_at": "2025-09-18"}
    bundle = erb.load_confluence(conn, "dsid_3", raw, P)
    assert bundle["group"] == "security-compliance"
    grants = grants_for("confluence", {**bundle, "org": "redwood"})
    assert ("group", "security-compliance") in grants
    assert ("group", "security") not in grants


def test_slack_text_variant_not_empty():
    # slack docs whose transcript is in 'text' (title_field_name 'file_name') must still parse
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {"title_field_name": "file_name", "content_field_names": ["text"],
           "file_name": "1711-foo.json", "channel": "partnerships",
           "text": "andrea_p: Heads up on EU regions.\nmike_partner: On it, ETA next week.",
           "participants": ["andrea_p", "mike_partner"]}
    erb.load_slack(conn, "dsid_s1", raw, P)
    rows = conn.execute("SELECT title, content, thread_seq FROM slack_messages WHERE thread_id='dsid_s1' ORDER BY thread_seq").fetchall()
    assert len(rows) == 2
    assert rows[0]["title"] == "" and "Heads up" in rows[0]["content"]  # not '*file_name*'
    assert "On it" in rows[1]["content"]


def test_gmail_body_variant_not_empty():
    # gmail docs carrying a single email in 'body' (no 'messages' list) must still get content
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {"title_field_name": "subject", "content_field_names": ["body"],
           "subject": "Q2 plan", "mailbox_owner": "Ceo Person",
           "body": "Here is the Q2 plan draft, please review."}
    erb.load_gmail(conn, "dsid_g1", raw, P)
    r = conn.execute("SELECT title, content FROM gmail_messages WHERE doc_id='dsid_g1'").fetchone()
    assert r["title"] == "Q2 plan"
    assert "Q2 plan draft" in r["content"]


def test_gmail_thread_attachments_ingested():
    # the bench's thread-level `attachments` (filename strings) must land on the root message
    # so the Gmail API can render them as parts (this is qst_0012's missing data).
    import json as _json
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {"title_field_name": "subject", "content_field_names": ["messages"],
           "subject": "Epoch procurement", "mailbox_owner": "Irene Choi",
           "attachments": ["Epoch_MSAAttachment_v3.pdf", "redlines_epoch_orderform_20290715.docx"],
           "messages": ["From: A <a@x.com>\nTo: B <b@y.com>\nDate: 2029-07-15\nSubject: Epoch procurement\n\nbody"]}
    erb.load_gmail(conn, "dsid_att", raw, P)
    r = conn.execute("SELECT attachments FROM gmail_messages WHERE doc_id='dsid_att'").fetchone()
    atts = _json.loads(r["attachments"])
    assert [a["filename"] for a in atts] == ["Epoch_MSAAttachment_v3.pdf",
                                             "redlines_epoch_orderform_20290715.docx"]
    assert atts[0]["mime"] == "application/pdf"
    assert atts[1]["mime"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    # a doc with no attachments leaves the column NULL (not "[]")
    erb.load_gmail(conn, "dsid_noatt", {"content_field_names": ["body"], "body": "x"}, P)
    assert conn.execute("SELECT attachments FROM gmail_messages WHERE doc_id='dsid_noatt'").fetchone()[0] is None


def test_gmail_thread_title_is_doc_level_subject():
    # the doc-level `subject` (the bench's canonical thread subject) must win over the first
    # message's RFC822 "Re: ..." Subject header (qst_0026's dropped-subject bug).
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {"title_field_name": "subject", "content_field_names": ["messages"],
           "subject": "[P0] Acme Health — retry storm", "mailbox_owner": "Sean Gallagher",
           "messages": ["From: a@x.com\nSubject: Re: urgent — spikes in 5xx\n\nbody one",
                        "From: b@y.com\nSubject: Re: urgent — spikes in 5xx\n\nbody two"]}
    erb.load_gmail(conn, "dsid_subj", raw, P)
    title = conn.execute("SELECT title FROM gmail_messages WHERE doc_id='dsid_subj'").fetchone()[0]
    assert title == "[P0] Acme Health — retry storm"
    # fallback: no doc-level subject -> the message Subject header is used
    raw2 = {"title_field_name": "subject", "content_field_names": ["messages"], "subject": "",
            "mailbox_owner": "X", "messages": ["From: a@x.com\nSubject: Real subject\n\nbody"]}
    erb.load_gmail(conn, "dsid_subj2", raw2, P)
    assert conn.execute("SELECT title FROM gmail_messages WHERE doc_id='dsid_subj2'").fetchone()[0] == "Real subject"


# ---------------------------------------------------------------------------
# from test_erb_orchestration.py
# ---------------------------------------------------------------------------

def test_acl_bundle_to_grants_drive():
    # a private-ish drive doc: owner + collaborator become user grants + team group
    bundle = {"_source": "google_drive", "owner": "maya.chen@redwoodinference.com",
              "people": ["ethan.park@redwoodinference.com"], "group": "research-applied-ml",
              "confidentiality": None}
    g = grants_for(bundle["_source"], {**bundle, "org": "redwood"})
    assert ("user", "maya.chen@redwoodinference.com") in g
    assert ("group", "research-applied-ml") in g


def test_flat_path_removed():
    # the untrusted flat importer symbols must be gone
    for gone in ("_parse_txt", "_ENTRY_RE", "fetch_slices", "generate_acl", "augment"):
        assert not hasattr(erb, gone), f"{gone} should be removed"


def test_synthesized_users_installed_after_load(tmp_path, monkeypatch):
    """Regression: users synthesized DURING load (owner/collaborator not in the directory) must
    land in principals AND their team group_members — i.e. P.install() runs after load_structured,
    not before (else they'd get tokens but no principal/group, breaking group-scoped ACL)."""
    data = tmp_path / "data"; data.mkdir()
    gen = tmp_path / "gen"; (gen / "sources" / "google_drive").mkdir(parents=True)
    (gen / "employee_directory.yaml").write_text(yaml.safe_dump({"departments": {"Engineering": [
        {"name": "Real Dev", "email": "real.dev@redwoodinference.com", "title": "Eng"}]}}))
    (gen / "sources" / "google_drive" / "d.json").write_text(json.dumps({
        "title_field_name": "title", "content_field_names": ["body"],
        "dataset_doc_uuid": "dsid_test1", "title": "Doc", "body": "x",
        "owner": "Zoe Newperson", "collaborators": ["Ravi Other"], "team": "engineering",
        "created_at": "2025-01-01", "confidentiality": "restricted"}))
    monkeypatch.setenv("MOCK_DATA_DIR", str(data))
    get_settings.cache_clear()
    settings = get_settings()
    shutil.copy(gen / "employee_directory.yaml", settings.employee_yaml)
    erb.import_structured(settings, gen)

    c = sqlite3.connect(settings.db_path)
    zoe = "zoe.newperson@redwoodinference.com"
    assert c.execute("SELECT 1 FROM principals WHERE email=?", (zoe,)).fetchone(), \
        "synthesized owner missing from principals"
    assert c.execute("SELECT 1 FROM group_members WHERE group_id='engineering' AND user_id=?",
                     (zoe,)).fetchone(), "synthesized owner missing from its team group_members"
    c.close()
    get_settings.cache_clear()


def test_import_structured_loads_hubspot_source_dir(tmp_path, monkeypatch):
    """End of the wiring: a `sources/hubspot/` dir in generated_data must be walked, loaded, and
    counted by the real import path — not just loadable via load_hubspot() in isolation."""
    data = tmp_path / "data"; data.mkdir()
    gen = tmp_path / "gen"; (gen / "sources" / "hubspot").mkdir(parents=True)
    (gen / "employee_directory.yaml").write_text(yaml.safe_dump({"departments": {"Sales": [
        {"name": "Maya Chen", "email": "maya.chen@redwoodinference.com", "title": "AE"}]}}))
    (gen / "sources" / "hubspot" / "company-acacia-loop-services.json").write_text(
        json.dumps({**HS_RAW, "dataset_doc_uuid": "dsid_hs_e2e"}))
    monkeypatch.setenv("MOCK_DATA_DIR", str(data))
    get_settings.cache_clear()
    settings = get_settings()
    shutil.copy(gen / "employee_directory.yaml", settings.employee_yaml)
    res = erb.import_structured(settings, gen)

    # import_structured returns the per-source counts directly; a company counts once even though
    # it also materializes note rows (same as a gmail thread counting once).
    assert res["hubspot"] == 1
    c = sqlite3.connect(settings.db_path)
    c.row_factory = sqlite3.Row
    company = c.execute("SELECT * FROM hubspot_objects WHERE object_type='companies'").fetchone()
    assert company["doc_id"] == "dsid_hs_e2e"
    assert company["author_email"] == "maya.chen@redwoodinference.com"
    assert c.execute("SELECT COUNT(*) FROM hubspot_objects WHERE object_type='notes'"
                     ).fetchone()[0] == 2
    # the company is ACL-granted, so a non-admin can actually reach it
    assert c.execute("SELECT COUNT(*) FROM doc_acl WHERE doc_id='dsid_hs_e2e'").fetchone()[0] > 0
    c.close()
    get_settings.cache_clear()


def _import_gen(tmp_path, monkeypatch, source: str, filename: str, raw: dict, employees: list):
    """Run the real import over a one-document generated_data tree; returns the built settings."""
    data = tmp_path / "data"; data.mkdir()
    gen = tmp_path / "gen"; (gen / "sources" / source).mkdir(parents=True)
    (gen / "employee_directory.yaml").write_text(
        yaml.safe_dump({"departments": {"Team": employees}}))
    (gen / "sources" / source / filename).write_text(json.dumps(raw))
    monkeypatch.setenv("MOCK_DATA_DIR", str(data))
    get_settings.cache_clear()
    settings = get_settings()
    shutil.copy(gen / "employee_directory.yaml", settings.employee_yaml)
    erb.import_structured(settings, gen)
    return settings


def _granted(conn, doc_id) -> set:
    return {(r["principal_type"], r["principal_id"])
            for r in conn.execute("SELECT * FROM doc_acl WHERE doc_id=?", (doc_id,))}


def test_materialized_note_rows_inherit_the_company_grants(tmp_path, monkeypatch):
    """A materialized child row is reached through the same ACL-filtered queries as any other doc
    (`_acl_clause` matches per row), so a note with no grants of its own is invisible to every
    non-admin caller — the company would list zero notes."""
    settings = _import_gen(
        tmp_path, monkeypatch, "hubspot", "company-acacia.json",
        {**HS_RAW, "dataset_doc_uuid": "dsid_hs_acl"},
        [{"name": "Maya Chen", "email": "maya.chen@redwoodinference.com", "title": "AE"}])
    conn = sqlite3.connect(settings.db_path); conn.row_factory = sqlite3.Row
    company = _granted(conn, "dsid_hs_acl")
    assert company                                     # sanity: the parent is granted
    notes = [r[0] for r in conn.execute(
        "SELECT doc_id FROM hubspot_objects WHERE object_type='notes'")]
    assert notes
    for n in notes:
        assert _granted(conn, n) == company, f"note {n} does not inherit the company's grants"
    conn.close()
    get_settings.cache_clear()


def test_thread_reply_rows_inherit_the_root_grants(tmp_path, monkeypatch):
    """Same defect on the pre-existing thread loaders: `slack_thread`/`gmail_thread` ACL-filter
    row by row, so ungranted replies silently truncate a thread for non-admin callers."""
    settings = _import_gen(
        tmp_path, monkeypatch, "slack", "1711-foo.json",
        {"title_field_name": "file_name", "content_field_names": ["text"],
         "dataset_doc_uuid": "dsid_s_acl", "file_name": "1711-foo.json", "channel": "partnerships",
         "text": "andrea_p: Heads up on EU regions.\nmike_partner: On it, ETA next week.",
         "participants": ["andrea_p", "mike_partner"]},
        [{"name": "Andrea Park", "email": "andrea.park@redwoodinference.com", "title": "PM"}])
    conn = sqlite3.connect(settings.db_path); conn.row_factory = sqlite3.Row
    root = _granted(conn, "dsid_s_acl")
    assert root
    replies = [r[0] for r in conn.execute(
        "SELECT doc_id FROM slack_messages WHERE thread_seq > 0")]
    assert replies
    for rid in replies:
        assert _granted(conn, rid) == root, f"reply {rid} does not inherit the root's grants"
    conn.close()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# from test_faithful_e2e.py
# ---------------------------------------------------------------------------

def _extra_questions(tmp):
    p = Path(tmp) / "extra_questions.jsonl"
    urllib.request.urlretrieve(
        "https://raw.githubusercontent.com/onyx-dot-app/EnterpriseRAG-Bench/main/extra_questions.jsonl", p)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


@pytest.mark.skipif(os.environ.get("ERB_E2E") != "1",
                    reason="set ERB_E2E=1 to run the network-backed faithful-import e2e")
def test_qst_0001_owner_is_maya_chen(tmp_path):
    data_dir = tmp_path / "data"
    qfile = Path(tmp_path) / "extra_questions.jsonl"
    _extra_questions(tmp_path)
    env = {**os.environ, "MOCK_DATA_DIR": str(data_dir)}
    subprocess.run([sys.executable, "-m", "app.importer.erb", "--slice-questions", str(qfile)],
                   check=True, env=env)
    # dsid_fc36... is qst_0001's expected doc; owner must now be Maya Chen, not a hash pick
    from starlette.testclient import TestClient
    os.environ["MOCK_DATA_DIR"] = str(data_dir)
    from app.main import app
    with TestClient(app) as c:
        r = c.get("/drive/v3/files/dsid_fc36d1d60e7e4b4abc7db84629563b7a",
                  params={"fields": "owners(displayName)"},
                  headers={"Authorization": "Bearer admin-service-token"}).json()
        assert r["owners"][0]["displayName"] == "Maya Chen"


# ---------------------------------------------------------------------------
# Linear: the bench record -> the API-faithful schema
# ---------------------------------------------------------------------------
# The mapping is where the bench and the API disagree, so these assert the translations rather
# than the pass-throughs: P0-P3 -> Linear's 0-4, `status` -> `state`, a state category -> the
# lifecycle timestamps the bench never records, and the three comment shapes.

# A record shaped exactly like the bench's, per `sources/linear/agents.md`.
LINEAR_RAW = {
    "title_field_name": "title", "content_field_names": ["description", "comments"],
    "dataset_doc_uuid": "dsid_lin", "key": "ENG-49121", "team": "engineering",
    "title": "Variant-aware GPU allocation", "status": "In Progress", "priority": "P1",
    "created_at": "2025-02-18", "updated_at": "2025-03-04",
    "creator": "Amaya Chen", "assignee": "Diego Martinez",
    "project": "runtime-memory-2025", "cycle": "2025-W08", "estimate": "5",
    "due_date": "2025-03-15", "labels": ["kv-cache", "long-context"],
    "description": "Long-context configs push peak GPU memory into fragile regions.",
    "comments": ["2025-02-18 - Created: initial hypothesis captured.",
                 "2025-02-20 Diego Martinez: ran baseline traces."],
}


def _load_linear(raw, dsid="dsid_lin"):
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    erb.load_linear(conn, dsid, raw, P)
    row = conn.execute("SELECT * FROM linear_issues WHERE doc_id = ?", (dsid,)).fetchone()
    comments = conn.execute(
        "SELECT * FROM linear_comments WHERE doc_id = ? ORDER BY seq", (dsid,)).fetchall()
    return conn, row, comments


def test_linear_maps_the_bench_record_onto_the_api_schema():
    _conn_, row, _c = _load_linear(LINEAR_RAW)
    assert row["team"] == "engineering"
    assert row["identifier"] == "ENG-49121"      # the bench key IS the Linear identifier
    assert row["state"] == "In Progress"          # `status` -> Linear's `state`
    assert row["priority"] == 2                   # P1 -> Linear's scale (1 is most urgent)
    assert row["estimate"] == 5                   # the bench writes it as a string
    assert row["project"] == "runtime-memory-2025"
    assert row["cycle"] == "2025-W08"
    assert row["due_date"] == "2025-03-15"
    assert json.loads(row["labels"]) == ["kv-cache", "long-context"]
    assert row["author_email"] == "amaya.chen@redwoodinference.com"
    assert row["owner_display"] == "Amaya Chen"
    assert row["assignee_email"] == "diego.martinez@redwoodinference.com"
    assert row["assignee_display"] == "Diego Martinez"
    assert row["title"] == "Variant-aware GPU allocation"
    assert "fragile regions" in row["content"]


def test_linear_container_is_the_team_field_not_the_directory():
    """~2,750 bench files sit in a directory that disagrees with their own `team`, and two
    directories (business-ops, misc-chores) name no team at all. The field is the authority."""
    conn, row, _c = _load_linear({**LINEAR_RAW, "team": "design"})
    assert row["team"] == "design"
    assert conn.execute("SELECT team FROM linear_teams").fetchone()["team"] == "design"


def test_linear_team_maps_onto_a_real_directory_department():
    """The ACL group has to have members, so the three bench teams must reconcile to dept slugs."""
    P = Principals([{"name": "A B", "email": "a.b@x.com", "dept_slug": "engineering"},
                    {"name": "C D", "email": "c.d@x.com", "dept_slug": "product"},
                    {"name": "E F", "email": "e.f@x.com", "dept_slug": "design-ux"}], "x.com")
    assert P.canonical_group("engineering") == "engineering"
    assert P.canonical_group("product-management") == "product"
    assert P.canonical_group("design") == "design-ux"


def test_linear_branch_name_is_derived_when_the_bench_has_none():
    _conn_, row, _c = _load_linear(LINEAR_RAW)
    assert row["branch_name"] == (
        "diegomartinez/eng-49121-variant-aware-gpu-allocation")


def test_linear_completed_timestamp_derives_from_the_state_category():
    """The bench records no lifecycle timestamps, but a state IS one: Linear sets completedAt the
    moment an issue enters a completed state."""
    _conn_, row, _c = _load_linear({**LINEAR_RAW, "status": "Done"})
    assert row["completed_ts"] == erb.to_epoch("2025-03-04")
    assert row["canceled_ts"] is None


def test_linear_canceled_timestamp_derives_from_the_state_category():
    _conn_, row, _c = _load_linear({**LINEAR_RAW, "status": "Canceled"})
    assert row["canceled_ts"] == erb.to_epoch("2025-03-04")
    assert row["completed_ts"] is None


def test_linear_open_issue_has_no_lifecycle_timestamps():
    _conn_, row, _c = _load_linear({**LINEAR_RAW, "status": "Backlog"})
    assert row["completed_ts"] is None and row["canceled_ts"] is None
    assert row["archived_ts"] is None and row["auto_closed_ts"] is None


def test_linear_unassigned_is_not_turned_into_a_person():
    """"unassigned" is a literal value in the bench (11 docs). Linear stores no assignee for an
    unassigned issue, and minting a user called "unassigned" would pollute the roster."""
    _conn_, row, _c = _load_linear({**LINEAR_RAW, "assignee": "unassigned"})
    assert row["assignee_email"] is None and row["assignee_display"] is None


def test_linear_synthesizes_an_identifier_when_the_key_is_missing():
    _conn_, row, _c = _load_linear({k: v for k, v in LINEAR_RAW.items() if k != "key"})
    assert row["identifier"].startswith("ENG-")


def test_linear_comment_shapes_are_all_parsed():
    """The three shapes measured across all 165,223 bench comments."""
    parsed = erb.parse_linear_comments([
        "2025-02-18 - Created: initial hypothesis captured.",       # 54.6%
        "2026-03-05 Anjali Rao: Updated acceptance criteria.",       # 38.0%
        "2025-12-18 (Naomi Feldman): Include the audit log.",        # the parenthesised variant
        "Implementation notes: use model heuristics.",               # 7.2% undated
    ])
    assert [c["date"] for c in parsed] == ["2025-02-18", "2026-03-05", "2025-12-18", None]
    assert [c["name"] for c in parsed] == [None, "Anjali Rao", "Naomi Feldman", None]
    assert parsed[0]["body"] == "Created: initial hypothesis captured."
    assert parsed[3]["body"] == "Implementation notes: use model heuristics."


def test_linear_comment_clock_prefix_is_not_read_as_an_author():
    """`2025-02-18 09:15: rolled back` must not parse as author "09" with the body truncated to
    "15: rolled back" — that both invents a person and loses text."""
    parsed = erb.parse_linear_comments(["2025-02-18 09:15: rolled back"])
    assert parsed[0]["name"] is None
    assert parsed[0]["body"] == "09:15: rolled back"


def test_linear_comment_string_instead_of_a_list_is_tolerated():
    """29 bench docs carry `comments` as a bare string."""
    assert len(erb.parse_linear_comments("2025-02-18 - one note")) == 1


def test_linear_comments_become_rows_with_real_dates():
    _conn_, _row, comments = _load_linear(LINEAR_RAW)
    assert [c["seq"] for c in comments] == [1, 2]
    assert comments[0]["created_ts"] == erb.to_epoch("2025-02-18")
    assert comments[1]["created_ts"] == erb.to_epoch("2025-02-20")


def test_linear_comment_author_is_matched_never_minted():
    """The `Name:` segment is far noisier than Jira's — 16,108 distinct strings, mostly labels
    like "Design review" that `_person_like` would happily accept. A comment therefore matches
    against the EXISTING roster and stays unattributed otherwise."""
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {**LINEAR_RAW, "creator": "Amaya Chen", "assignee": "unassigned",
           "comments": ["2025-02-20 Amaya Chen: known person, resolved from the issue's creator.",
                        "2025-02-21 Design review: a label, not a person."]}
    erb.load_linear(conn, "dsid_c", raw, P)
    rows = conn.execute(
        "SELECT author_email FROM linear_comments WHERE doc_id='dsid_c' ORDER BY seq").fetchall()
    assert rows[0]["author_email"] == "amaya.chen@redwoodinference.com"
    assert rows[1]["author_email"] is None
    assert "design.review@redwoodinference.com" not in P.users


def test_linear_undated_comment_stays_on_the_issues_clock():
    """created_ts is NOT NULL, and a random per-comment time would shuffle the thread."""
    _conn_, row, comments = _load_linear({**LINEAR_RAW, "comments": ["no date here", "nor here"]})
    assert [c["created_ts"] for c in comments] == [row["created_ts"] + 1, row["created_ts"] + 2]


def test_linear_priority_normalisation():
    assert [erb.linear_priority(v) for v in ("P0", "P1", "P2", "P3")] == [1, 2, 3, 4]
    assert [erb.linear_priority(v) for v in ("Urgent", "High", "Medium", "Low")] == [1, 2, 3, 4]
    assert erb.linear_priority(3) == 3                 # already Linear's scale
    assert erb.linear_priority("unrecognised") == 0    # Linear's "No priority"
    assert erb.linear_priority(None) is None


def test_linear_grants_flow_through_the_shared_container_path():
    """Linear needs no branch in `grants_for`: its container maps to a group like github/jira."""
    bundle = {"owner": "a@x.com", "people": ["b@x.com"], "group": "engineering", "org": "acme"}
    assert set(grants_for("linear", bundle)) == {
        ("user", "a@x.com"), ("user", "b@x.com"), ("group", "engineering")}
