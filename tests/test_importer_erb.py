import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import pytest
import yaml

from backlot import store, synth
from backlot.config import Settings, get_settings
from backlot.importer import byo, erb
from tests._helpers import client_for
from backlot.importer.erb import Principals, canonical, grants_for

C = erb


# ---------------------------------------------------------------------------
# from test_erb_source.py
# ---------------------------------------------------------------------------


def test_derive_title_content_scalar():
    raw = {
        "title_field_name": "title",
        "content_field_names": ["body", "body_addendum"],
        "title": "Doc A",
        "body": "hello",
        "body_addendum": "world",
    }
    title, content = erb.derive_title_content(raw)
    assert title == "Doc A"
    assert "hello" in content and "world" in content


def test_derive_title_content_list_field():
    raw = {
        "title_field_name": "channel",
        "content_field_names": ["messages"],
        "channel": "eng-infra",
        "messages": "Alex: hi\nMaria: yo",
    }
    title, content = erb.derive_title_content(raw)
    assert title == "eng-infra"
    assert "Alex: hi" in content


def test_supported_sources():
    assert erb.SUPPORTED == (
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
BENCH_SOURCES = {
    "slack",
    "gmail",
    "google_drive",
    "github",
    "jira",
    "confluence",
    "hubspot",
    "linear",
    "fireflies",
}


def test_every_bench_source_has_a_converter():
    """All nine source directories EnterpriseRAG-Bench ships are now loaded — Fireflies was the
    last one. A new bench directory therefore has to be handled here rather than silently
    skipped by `keep_sources`/`iter_records`."""
    assert set(erb.SUPPORTED) == BENCH_SOURCES
    assert set(erb.SUPPORTED) <= set(erb._BYO_CONVERTERS)


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


def test_dump_tokens_returns_the_number_it_actually_wrote(tmp_path):
    """`--tokens-only` prints this count, so it has to be the number of rows in tokens.yaml.
    Only the employee directory gets a token (see Principals.write_tokens); counting every
    resolved principal instead reported 679 for a file holding 167."""
    import shutil

    from backlot.config import Settings

    gen = _write_generated_data(tmp_path)
    data = tmp_path / "tok"
    data.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=data)
    shutil.copy(gen / "employee_directory.yaml", settings.employee_yaml)

    n = erb.dump_tokens(settings, gen)
    written = yaml.safe_load(settings.tokens_path.read_text())["users"]
    assert n == len(written)


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
    rec = (
        "gmail",
        "dsid_x",
        {
            "title_field_name": "subject",
            "content_field_names": ["messages"],
            "subject": "s",
            "messages": ["From: Maya Chen <maya_chen@redwoodinference.com>\nTo: x\n\nhi"],
        },
    )
    p.harvest_gmail_emails([rec])
    assert p.resolve("Maya Chen", role="author") == "maya_chen@redwoodinference.com"


def test_harvest_skips_alias_header_names():
    # a header alias like 'On-Call (SRE) <oncall@…>' is not a person → not harvested as a user
    p = _p()
    rec = (
        "gmail",
        "dsid_y",
        {
            "title_field_name": "subject",
            "content_field_names": ["messages"],
            "subject": "s",
            "messages": ["From: On-Call (SRE) <oncall@redwoodinference.com>\n\nhi"],
        },
    )
    p.harvest_gmail_emails([rec])
    assert "oncall@redwoodinference.com" not in p.users


def _p_multi():
    employees = [
        {"name": "Ava Chen", "email": "ava.chen@redwoodinference.com", "dept_slug": "engineering"},
        {
            "name": "Maya Chen",
            "email": "maya.chen@redwoodinference.com",
            "dept_slug": "security-compliance",
        },
        {
            "name": "Priya Desai",
            "email": "priya.desai@redwoodinference.com",
            "dept_slug": "applied-ml-research",
        },
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
    import types
    import yaml as _yaml

    p = Principals(
        [
            {
                "name": "Ava Chen",
                "email": "ava.chen@redwoodinference.com",
                "dept_slug": "engineering",
            }
        ],
        "redwoodinference.com",
    )
    p.resolve("Maya Chen", role="owner", group_hint="engineering")  # synthesized, non-directory
    p.resolve("Wei Chen", role="reviewer")  # synthesized, non-directory
    st = types.SimpleNamespace(
        tokens_path=tmp_path / "tokens.yaml",
        org_name="redwood",
        org_domain="redwoodinference.com",
        admin_token="admin-service-token",
    )
    p.write_tokens(st)
    d = _yaml.safe_load(st.tokens_path.read_text())
    emails = {u["email"] for u in d["users"]}
    assert emails == {"ava.chen@redwoodinference.com"}  # only the directory employee
    assert "maya.chen@redwoodinference.com" not in emails


def test_canonical_folds_accents():
    assert canonical("Tomáš Novák") == canonical("Tomas Novak") == "tomasnovak"


def test_mint_does_not_clobber_directory_user(tmp_path):
    # an accented/titled directory name whose doc-reference doesn't canonical-match must still
    # keep its directory flag (the colliding mint must not overwrite it) → stays tokened
    import types
    import yaml as _yaml

    p = Principals(
        [
            {
                "name": "Tomáš Novák",
                "email": "tomas.novak@redwoodinference.com",
                "dept_slug": "engineering",
            }
        ],
        "redwoodinference.com",
    )
    # a doc references the plain spelling; folded canonical now matches → resolves to the dir user
    assert p.resolve("Tomas Novak", role="owner") == "tomas.novak@redwoodinference.com"
    assert p.users["tomas.novak@redwoodinference.com"].get("directory") is True
    st = types.SimpleNamespace(
        tokens_path=tmp_path / "t.yaml",
        org_name="redwood",
        org_domain="redwoodinference.com",
        admin_token="admin-service-token",
    )
    p.write_tokens(st)
    assert "tomas.novak@redwoodinference.com" in {
        u["email"] for u in _yaml.safe_load(st.tokens_path.read_text())["users"]
    }


# ---------------------------------------------------------------------------
# from test_conversations.py
# ---------------------------------------------------------------------------


def test_parse_gmail_thread():
    msgs = [
        "From: Vivek K <vivek_k@redwoodinference.com>\n"
        "To: Connor O'Brien <connor_obrien@redwoodinference.com>\n"
        "Date: Wed, May 14, 2025 at 9:12 AM PT\nSubject: Beta plan\n\nBody one.",
        "From: Connor O'Brien <connor_obrien@redwoodinference.com>\n"
        "To: Vivek K <vivek_k@redwoodinference.com>\nDate: Wed, May 14, 2025 at 10:00 AM PT\n"
        "Subject: Re: Beta plan\n\nReply two.",
    ]
    out = C.parse_gmail_thread(msgs)
    assert len(out) == 2
    assert out[0]["from_email"] == "vivek_k@redwoodinference.com"
    assert out[0]["subject"] == "Beta plan"
    assert "Body one." in out[0]["body"]


def test_to_epoch_parses_bench_date_formats():
    # RFC 2822 email Date header (the bench's gmail format) — the big one: previously unparsed,
    # which left ~96% of gmail with NULL created_ts and a synthesized (fake) served date.
    assert C.to_epoch("Mon, 18 May 2026 09:02:00 -0700") == 1779120120  # 16:02Z
    assert C.to_epoch("Mon, 18 May 2026 10:17:00 -07:00") == 1779124620  # malformed colon offset
    # ISO 8601 with a numeric offset and with a trailing Z
    assert C.to_epoch("2026-05-18T09:02:00-07:00") == 1779120120
    assert C.to_epoch("2028-05-23T09:12:00Z") == 1842685920
    # timezone-ABBREVIATION formats (no numeric offset) — the bench's third gmail date shape
    assert C.to_epoch("2026-08-30 09:12 PDT") == 1788106320  # 16:12Z (PDT = -0700)
    assert C.to_epoch("2026-10-04 09:12 UTC") == 1791105120  # 09:12Z
    assert C.to_epoch("Wed, May 14, 2025 at 9:12 AM PT") == 1747242720  # 17:12Z (PT = -0800)
    # date-only, epoch string, and unparseable
    assert C.to_epoch("2025-11-05") == 1762300800
    assert C.to_epoch("1718326400") == 1718326400
    assert C.to_epoch("not a date") is None


def test_parse_jira_comments():
    out = C.parse_jira_comments(
        ["2026-03-14 Jordan Kim: Filing request.", "2026-03-15 Priya Desai: On it."]
    )
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
        ["Alex", "Maria"],
    )
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
        "Elena (CFO): Following up.\nDiego (Eng): thanks\nAsha (FinanceOps): filed it"
    )
    assert out == [("Elena", "Following up."), ("Diego", "thanks"), ("Asha", "filed it")]


# ---------------------------------------------------------------------------
# from test_acl_faithful.py
# ---------------------------------------------------------------------------


def test_drive_grants_owner_collaborators_and_group():
    g = grants_for(
        "google_drive",
        {
            "owner": "a@x.com",
            "people": ["b@x.com"],
            "group": "finance",
            "confidentiality": None,
            "org": "redwood",
        },
    )
    assert ("user", "a@x.com") in g and ("user", "b@x.com") in g
    assert ("group", "finance") in g


def test_gmail_is_private_no_org_or_group():
    g = grants_for(
        "gmail",
        {
            "owner": "a@x.com",
            "people": ["b@x.com", "ext@external.example"],
            "group": "sales",
            "confidentiality": None,
            "org": "redwood",
        },
    )
    assert ("user", "a@x.com") in g
    assert not any(t == "org" or t == "group" for t, _ in g)


def test_confluence_confidentiality_scope():
    pub = grants_for(
        "confluence",
        {
            "owner": "a@x.com",
            "people": [],
            "group": "eng",
            "confidentiality": "public",
            "org": "redwood",
        },
    )
    assert ("org", "redwood") in pub
    restr = grants_for(
        "confluence",
        {
            "owner": "a@x.com",
            "people": [],
            "group": "eng",
            "confidentiality": "restricted",
            "org": "redwood",
        },
    )
    assert ("group", "eng") in restr and ("org", "redwood") not in restr


# ---------------------------------------------------------------------------
# from test_erb_load.py
# ---------------------------------------------------------------------------


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store.SCHEMA)
    return c


def _load_one(conn, src, dsid, raw, P, org="redwood", loader=None):
    """Convert ONE ERB document and insert its row(s) — the per-document slice of a real import.

    `erb.import_structured` is exactly this over a whole corpus: run the source's `_byo_*`
    converter, hand the records to `byo._Loader`. Returns the people bundle `grants_for` reads.
    Pass `loader` to accumulate several documents before resolving their cross-references (a
    Linear parent may be declared before the issue it points at).
    """
    records, bundle = erb._BYO_CONVERTERS[src](dsid, raw, P)
    ldr = loader or byo._Loader(conn, org, P.org_domain)
    for rec in records:
        ldr.add(rec, dsid)
    if loader is None:
        ldr.resolve_cross_references()
        ldr.write_containers()
    return bundle


def test_to_epoch_formats():
    assert erb.to_epoch("2025-09-18") is not None
    assert erb.to_epoch("Wed, May 14, 2025 at 9:12 AM PT") is not None
    assert erb.to_epoch(1710501234) == 1710501234
    assert erb.to_epoch("garbage") is None


def test_drive_owner_is_faithful():
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {
        "title_field_name": "title",
        "content_field_names": ["body"],
        "title": "Model",
        "body": "x",
        "owner": "Maya Chen",
        "collaborators": ["Ethan Park"],
        "team": "research-applied-ml",
        "created_at": "2025-09-18",
        "doc_type": "doc",
    }
    _load_one(conn, "google_drive", "dsid_1", raw, P)
    row = conn.execute(
        "SELECT author_email, owner_display, created_ts FROM gdrive_files WHERE doc_id='dsid_1'"
    ).fetchone()
    assert row["author_email"] == "maya.chen@redwoodinference.com"
    assert row["owner_display"] == "Maya Chen"
    assert row["created_ts"] is not None


def test_drive_doc_type_maps_onto_drive_mime_types():
    """The bench's `doc_type` vocabulary (doc/sheet/slides/pdf) is not the mock's native subtype
    vocabulary, so every imported row used to fall back to `application/octet-stream` — making
    anything that branches on mimeType untestable against the bench corpus (issue #23)."""
    from backlot.routers.google import _drive_file

    conn = _conn()
    P = Principals([], "redwoodinference.com")
    base = {
        "title_field_name": "title",
        "content_field_names": ["body"],
        "body": "x",
        "owner": "Maya Chen",
        "team": "research-applied-ml",
        "created_at": "2025-09-18",
    }
    cases = {
        "doc": "application/vnd.google-apps.document",
        "sheet": "application/vnd.google-apps.spreadsheet",
        "slides": "application/vnd.google-apps.presentation",
        "pdf": "application/pdf",
        None: "application/vnd.google-apps.document",
    }  # unspecified -> a Doc, not a blob
    for i, (doc_type, mime) in enumerate(cases.items()):
        _load_one(
            conn, "google_drive", f"dt_{i}", {**base, "title": f"T{i}", "doc_type": doc_type}, P
        )
        row = store.get_document(conn, "google_drive", f"dt_{i}")
        assert _drive_file(conn, row)["mimeType"] == mime, doc_type


def test_drive_unknown_doc_type_falls_back_to_the_title_extension():
    from backlot.routers.google import _drive_file

    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {
        "title_field_name": "title",
        "content_field_names": ["body"],
        "body": "x",
        "title": "Executed Addendum.pdf",
        "owner": "Maya Chen",
        "doc_type": "scan",
        "team": "research-applied-ml",
        "created_at": "2025-09-18",
    }
    _load_one(conn, "google_drive", "dt_ext", raw, P)
    row = store.get_document(conn, "google_drive", "dt_ext")
    assert _drive_file(conn, row)["mimeType"] == "application/pdf"


def test_jira_assignee_reporter_and_duedate():
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {
        "title_field_name": "summary",
        "content_field_names": ["description"],
        "summary": "S",
        "description": "d",
        "reporter": "Jordan Kim",
        "assignee": "Priya Desai",
        "project": "INT",
        "status": "In Progress",
        "created_at": "2025-11-01",
    }
    _load_one(conn, "jira", "dsid_2", raw, P)
    row = conn.execute(
        "SELECT reporter_email, assignee_email, status FROM jira_issues WHERE doc_id='dsid_2'"
    ).fetchone()
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
    _load_one(conn, "hubspot", "dsid_hs1", HS_RAW, P)
    row = conn.execute("SELECT * FROM hubspot_objects WHERE doc_id='dsid_hs1'").fetchone()
    assert row["object_type"] == "companies"
    assert row["title"] == "Acacia Loop Services"
    assert row["author_email"] == "maya.chen@redwoodinference.com"  # owner (AE), resolved
    assert row["owner_display"] == "Maya Chen"
    props = store.jcol(row, "properties", {})
    assert props["name"] == "Acacia Loop Services"
    assert props["domain"] == "acacia-loop.com"
    assert props["industry"] == "financial_services"
    assert props["lifecyclestage"] == "evaluation"  # stage -> HubSpot's own name
    assert props["account_tier"] == "enterprise"  # no default property -> custom
    assert row["created_ts"] == erb.to_epoch("2025-11-05")
    assert row["updated_ts"] == erb.to_epoch("2026-03-10")


def test_hubspot_notes_materialize_as_note_objects():
    """Real HubSpot models a note as its own object associated with the company, and this repo
    already parses embedded conversations into first-class rows on import — so each `notes` entry
    becomes a `notes` record linked to the company, not just text inside the company body."""
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    _load_one(conn, "hubspot", "dsid_hs1", HS_RAW, P)
    notes = conn.execute(
        "SELECT * FROM hubspot_objects WHERE object_type='notes' ORDER BY doc_id"
    ).fetchall()
    assert len(notes) == 2
    assert notes[0]["content"].startswith("Inbound SMB")
    # API fidelity: a HubSpot note carries its body in hs_note_body
    assert store.jcol(notes[0], "properties", {})["hs_note_body"] == notes[0]["content"]
    # each note is associated with the company, in both directions
    assert [
        r["to_doc_id"] for r in store.hubspot_associations(conn, "dsid_hs1", "notes")
    ] == sorted(n["doc_id"] for n in notes)
    assert [
        r["to_doc_id"] for r in store.hubspot_associations(conn, notes[0]["doc_id"], "companies")
    ] == ["dsid_hs1"]


def test_hubspot_timeline_stays_in_the_company_body():
    """`timeline` is a dated activity log the bench lists in content_field_names — it is the
    company's own text, not a set of note objects, so it must not be materialized."""
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    _load_one(conn, "hubspot", "dsid_hs1", HS_RAW, P)
    company = conn.execute("SELECT content FROM hubspot_objects WHERE doc_id='dsid_hs1'").fetchone()
    assert "inbound signup via marketplace" in company["content"]
    assert (
        conn.execute("SELECT COUNT(*) FROM hubspot_objects WHERE object_type='notes'").fetchone()[0]
        == 2
    )  # only the two `notes`, nothing from `timeline`


def test_hubspot_linked_artifacts_stay_property_stubs():
    """The bench's linked_* arrays are free-text references ("stubs/links" per the dataset's own
    agents.md), not resolvable doc ids — so they stay properties and must never become
    associations pointing at documents that do not exist."""
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    _load_one(conn, "hubspot", "dsid_hs1", HS_RAW, P)
    props = store.jcol(
        conn.execute("SELECT properties FROM hubspot_objects WHERE doc_id='dsid_hs1'").fetchone(),
        "properties",
        {},
    )
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
    bundle = _load_one(conn, "hubspot", "dsid_hs1", HS_RAW, P)
    assert bundle["owner"] == "maya.chen@redwoodinference.com"
    assert set(bundle["people"]) == {
        "ethan.park@redwoodinference.com",
        "priya.desai@redwoodinference.com",
    }
    grants = grants_for("hubspot", {**bundle, "org": "redwood"})
    assert ("user", "maya.chen@redwoodinference.com") in grants


def test_hubspot_is_org_visible():
    """A CRM is team-wide, and the bench names ~3.3k account owners of whom only the ~167 in the
    employee directory can authenticate. Scoping a record to its owner (or to the object type's
    group, whose only members are those same synthesized owners) leaves the corpus visible to admin
    and to almost nobody else — so HubSpot gets an org grant, the way Slack does."""
    bundle = {
        "owner": "maya.chen@redwoodinference.com",
        "people": [],
        "group": "companies",
        "confidentiality": None,
        "org": "redwood",
    }
    grants = grants_for("hubspot", bundle)
    assert ("org", "redwood") in grants
    assert ("group", "companies") not in grants  # the org grant supersedes it
    assert ("user", "maya.chen@redwoodinference.com") in grants  # named people still granted


def test_confluence_restricted_grants_reconciled_directory_group():
    """A doc's team label ("security") must reconcile to the directory's actual dept_slug group
    ("security-compliance") for the ACL grant — not become its own empty group."""
    conn = _conn()
    employees = [
        {
            "name": "Priya Desai",
            "email": "priya.desai@redwoodinference.com",
            "dept_slug": "security-compliance",
        },
    ]
    P = Principals(employees, "redwoodinference.com")
    raw = {
        "title_field_name": "title",
        "content_field_names": ["body"],
        "title": "Sec Policy",
        "body": "x",
        "author": "Priya Desai",
        "owner_team": "security",
        "confidentiality": "restricted",
        "space": "SEC",
        "created_at": "2025-09-18",
    }
    bundle = _load_one(conn, "confluence", "dsid_3", raw, P)
    assert bundle["group"] == "security-compliance"
    grants = grants_for("confluence", {**bundle, "org": "redwood"})
    assert ("group", "security-compliance") in grants
    assert ("group", "security") not in grants


def test_slack_text_variant_not_empty():
    # slack docs whose transcript is in 'text' (title_field_name 'file_name') must still parse
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {
        "title_field_name": "file_name",
        "content_field_names": ["text"],
        "file_name": "1711-foo.json",
        "channel": "partnerships",
        "text": "andrea_p: Heads up on EU regions.\nmike_partner: On it, ETA next week.",
        "participants": ["andrea_p", "mike_partner"],
    }
    _load_one(conn, "slack", "dsid_s1", raw, P)
    rows = conn.execute(
        "SELECT title, content, thread_seq FROM slack_messages WHERE thread_id='dsid_s1' ORDER BY thread_seq"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["title"] == "" and "Heads up" in rows[0]["content"]  # not '*file_name*'
    assert "On it" in rows[1]["content"]


def test_gmail_body_variant_not_empty():
    # gmail docs carrying a single email in 'body' (no 'messages' list) must still get content
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {
        "title_field_name": "subject",
        "content_field_names": ["body"],
        "subject": "Q2 plan",
        "mailbox_owner": "Ceo Person",
        "body": "Here is the Q2 plan draft, please review.",
    }
    _load_one(conn, "gmail", "dsid_g1", raw, P)
    r = conn.execute("SELECT title, content FROM gmail_messages WHERE doc_id='dsid_g1'").fetchone()
    assert r["title"] == "Q2 plan"
    assert "Q2 plan draft" in r["content"]


def test_gmail_thread_attachments_ingested():
    # the bench's thread-level `attachments` (filename strings) must land on the root message
    # so the Gmail API can render them as parts (this is qst_0012's missing data).
    import json as _json

    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {
        "title_field_name": "subject",
        "content_field_names": ["messages"],
        "subject": "Epoch procurement",
        "mailbox_owner": "Irene Choi",
        "attachments": ["Epoch_MSAAttachment_v3.pdf", "redlines_epoch_orderform_20290715.docx"],
        "messages": [
            "From: A <a@x.com>\nTo: B <b@y.com>\nDate: 2029-07-15\nSubject: Epoch procurement\n\nbody"
        ],
    }
    _load_one(conn, "gmail", "dsid_att", raw, P)
    r = conn.execute("SELECT attachments FROM gmail_messages WHERE doc_id='dsid_att'").fetchone()
    atts = _json.loads(r["attachments"])
    assert [a["filename"] for a in atts] == [
        "Epoch_MSAAttachment_v3.pdf",
        "redlines_epoch_orderform_20290715.docx",
    ]
    assert atts[0]["mime"] == "application/pdf"
    assert (
        atts[1]["mime"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    # a doc with no attachments leaves the column NULL (not "[]")
    _load_one(conn, "gmail", "dsid_noatt", {"content_field_names": ["body"], "body": "x"}, P)
    assert (
        conn.execute("SELECT attachments FROM gmail_messages WHERE doc_id='dsid_noatt'").fetchone()[
            0
        ]
        is None
    )


def test_gmail_thread_title_is_doc_level_subject():
    # the doc-level `subject` (the bench's canonical thread subject) must win over the first
    # message's RFC822 "Re: ..." Subject header (qst_0026's dropped-subject bug).
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {
        "title_field_name": "subject",
        "content_field_names": ["messages"],
        "subject": "[P0] Acme Health — retry storm",
        "mailbox_owner": "Sean Gallagher",
        "messages": [
            "From: a@x.com\nSubject: Re: urgent — spikes in 5xx\n\nbody one",
            "From: b@y.com\nSubject: Re: urgent — spikes in 5xx\n\nbody two",
        ],
    }
    _load_one(conn, "gmail", "dsid_subj", raw, P)
    title = conn.execute("SELECT title FROM gmail_messages WHERE doc_id='dsid_subj'").fetchone()[0]
    assert title == "[P0] Acme Health — retry storm"
    # fallback: no doc-level subject -> the message Subject header is used
    raw2 = {
        "title_field_name": "subject",
        "content_field_names": ["messages"],
        "subject": "",
        "mailbox_owner": "X",
        "messages": ["From: a@x.com\nSubject: Real subject\n\nbody"],
    }
    _load_one(conn, "gmail", "dsid_subj2", raw2, P)
    assert (
        conn.execute("SELECT title FROM gmail_messages WHERE doc_id='dsid_subj2'").fetchone()[0]
        == "Real subject"
    )


# ---------------------------------------------------------------------------
# from test_erb_orchestration.py
# ---------------------------------------------------------------------------


def test_acl_bundle_to_grants_drive():
    # a private-ish drive doc: owner + collaborator become user grants + team group
    bundle = {
        "_source": "google_drive",
        "owner": "maya.chen@redwoodinference.com",
        "people": ["ethan.park@redwoodinference.com"],
        "group": "research-applied-ml",
        "confidentiality": None,
    }
    g = grants_for(bundle["_source"], {**bundle, "org": "redwood"})
    assert ("user", "maya.chen@redwoodinference.com") in g
    assert ("group", "research-applied-ml") in g


def test_flat_path_removed():
    # the untrusted flat importer symbols must be gone
    for gone in ("_parse_txt", "_ENTRY_RE", "fetch_slices", "generate_acl", "augment"):
        assert not hasattr(erb, gone), f"{gone} should be removed"


def test_synthesized_users_installed_after_load(tmp_path, monkeypatch):
    """Regression: users synthesized DURING load (owner/collaborator not in the directory) must
    land in principals AND their team group_members — i.e. P.install() runs after the load,
    not before (else they'd get tokens but no principal/group, breaking group-scoped ACL)."""
    data = tmp_path / "data"
    data.mkdir()
    gen = tmp_path / "gen"
    (gen / "sources" / "google_drive").mkdir(parents=True)
    (gen / "employee_directory.yaml").write_text(
        yaml.safe_dump(
            {
                "departments": {
                    "Engineering": [
                        {
                            "name": "Real Dev",
                            "email": "real.dev@redwoodinference.com",
                            "title": "Eng",
                        }
                    ]
                }
            }
        )
    )
    (gen / "sources" / "google_drive" / "d.json").write_text(
        json.dumps(
            {
                "title_field_name": "title",
                "content_field_names": ["body"],
                "dataset_doc_uuid": "dsid_test1",
                "title": "Doc",
                "body": "x",
                "owner": "Zoe Newperson",
                "collaborators": ["Ravi Other"],
                "team": "engineering",
                "created_at": "2025-01-01",
                "confidentiality": "restricted",
            }
        )
    )
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(data))
    get_settings.cache_clear()
    settings = get_settings()
    shutil.copy(gen / "employee_directory.yaml", settings.employee_yaml)
    erb.import_structured(settings, gen)

    c = sqlite3.connect(settings.db_path)
    zoe = "zoe.newperson@redwoodinference.com"
    assert c.execute("SELECT 1 FROM principals WHERE email=?", (zoe,)).fetchone(), (
        "synthesized owner missing from principals"
    )
    assert c.execute(
        "SELECT 1 FROM group_members WHERE group_id='engineering' AND user_id=?", (zoe,)
    ).fetchone(), "synthesized owner missing from its team group_members"
    c.close()
    get_settings.cache_clear()


def test_import_structured_loads_hubspot_source_dir(tmp_path, monkeypatch):
    """End of the wiring: a `sources/hubspot/` dir in generated_data must be walked, loaded, and
    counted by the real import path — not just loadable via load_hubspot() in isolation."""
    data = tmp_path / "data"
    data.mkdir()
    gen = tmp_path / "gen"
    (gen / "sources" / "hubspot").mkdir(parents=True)
    (gen / "employee_directory.yaml").write_text(
        yaml.safe_dump(
            {
                "departments": {
                    "Sales": [
                        {
                            "name": "Maya Chen",
                            "email": "maya.chen@redwoodinference.com",
                            "title": "AE",
                        }
                    ]
                }
            }
        )
    )
    (gen / "sources" / "hubspot" / "company-acacia-loop-services.json").write_text(
        json.dumps({**HS_RAW, "dataset_doc_uuid": "dsid_hs_e2e"})
    )
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(data))
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
    assert (
        c.execute("SELECT COUNT(*) FROM hubspot_objects WHERE object_type='notes'").fetchone()[0]
        == 2
    )
    # `source_documents` is the ERB-level count (this one bench document), not the 3
    # `hubspot_objects` rows `to_byo` materializes for it (company + 2 notes) — the same distinction
    # `byo.load_records`'s own counting makes for a Slack thread's replies, but at a different
    # granularity: `_byo_hubspot` splits one bench document into several TOP-LEVEL BYO records, so
    # counting `byo.load_records`'s `(where, record)` pairs would overcount it as 3.
    assert store.read_meta(c, "source_documents") == "1"
    # the company is ACL-granted, so a non-admin can actually reach it
    assert c.execute("SELECT COUNT(*) FROM doc_acl WHERE doc_id='dsid_hs_e2e'").fetchone()[0] > 0
    c.close()
    get_settings.cache_clear()


def test_import_structured_persists_source_documents_including_excluded(tmp_path, monkeypatch):
    """`source_documents == documents + excluded + failed` (see export_byo's layer). The `+
    excluded` term is the one this task's erb-side fix introduced (`len(records) + len(excluded)`),
    and it was otherwise only exercised in `export_byo`'s manifest.json, never against the database
    `import_structured` actually builds — so a real document plus a deliberately empty-content one
    (which `select_records` drops into `excluded`) has to add up to 2 offered, not 1."""
    data = tmp_path / "data"
    data.mkdir()
    gen = tmp_path / "gen"
    (gen / "sources" / "google_drive").mkdir(parents=True)
    (gen / "employee_directory.yaml").write_text(
        yaml.safe_dump(
            {
                "departments": {
                    "Engineering": [
                        {
                            "name": "Real Dev",
                            "email": "real.dev@redwoodinference.com",
                            "title": "Eng",
                        }
                    ]
                }
            }
        )
    )
    (gen / "sources" / "google_drive" / "real.json").write_text(
        json.dumps(
            {
                "title_field_name": "title",
                "content_field_names": ["body"],
                "dataset_doc_uuid": "dsid_real",
                "title": "Doc",
                "body": "x",
                "owner": "Real Dev",
                "created_at": "2025-01-01",
            }
        )
    )
    # Whitespace-only body -> empty after strip -> select_records drops it into `excluded` rather
    # than yielding it to the converter.
    (gen / "sources" / "google_drive" / "empty.json").write_text(
        json.dumps(
            {
                "title_field_name": "title",
                "content_field_names": ["body"],
                "dataset_doc_uuid": "dsid_empty",
                "title": "Empty",
                "body": "   ",
                "owner": "Real Dev",
                "created_at": "2025-01-01",
            }
        )
    )
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(data))
    get_settings.cache_clear()
    settings = get_settings()
    shutil.copy(gen / "employee_directory.yaml", settings.employee_yaml)
    # dsid_empty isn't in KNOWN_EMPTY_DOCS, so allow_excluded=1 is needed or _resolve_roster refuses.
    res = erb.import_structured(settings, gen, allow_excluded=1)

    assert res["google_drive"] == 1  # only the real document converts and is written
    c = sqlite3.connect(settings.db_path)
    assert store.read_meta(c, "source_documents") == "2"  # 1 written + 1 excluded
    c.close()
    get_settings.cache_clear()


def _import_gen(tmp_path, monkeypatch, source: str, filename: str, raw: dict, employees: list):
    """Run the real import over a one-document generated_data tree; returns the built settings."""
    data = tmp_path / "data"
    data.mkdir()
    gen = tmp_path / "gen"
    (gen / "sources" / source).mkdir(parents=True)
    (gen / "employee_directory.yaml").write_text(
        yaml.safe_dump({"departments": {"Team": employees}})
    )
    (gen / "sources" / source / filename).write_text(json.dumps(raw))
    monkeypatch.setenv("BACKLOT_DATA_DIR", str(data))
    get_settings.cache_clear()
    settings = get_settings()
    shutil.copy(gen / "employee_directory.yaml", settings.employee_yaml)
    erb.import_structured(settings, gen)
    return settings


def _granted(conn, doc_id) -> set:
    return {
        (r["principal_type"], r["principal_id"])
        for r in conn.execute("SELECT * FROM doc_acl WHERE doc_id=?", (doc_id,))
    }


def test_materialized_note_rows_inherit_the_company_grants(tmp_path, monkeypatch):
    """A materialized child row is reached through the same ACL-filtered queries as any other doc
    (`_acl_clause` matches per row), so a note with no grants of its own is invisible to every
    non-admin caller — the company would list zero notes."""
    settings = _import_gen(
        tmp_path,
        monkeypatch,
        "hubspot",
        "company-acacia.json",
        {**HS_RAW, "dataset_doc_uuid": "dsid_hs_acl"},
        [{"name": "Maya Chen", "email": "maya.chen@redwoodinference.com", "title": "AE"}],
    )
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    company = _granted(conn, "dsid_hs_acl")
    assert company  # sanity: the parent is granted
    notes = [
        r[0] for r in conn.execute("SELECT doc_id FROM hubspot_objects WHERE object_type='notes'")
    ]
    assert notes
    for n in notes:
        assert _granted(conn, n) == company, f"note {n} does not inherit the company's grants"
    conn.close()
    get_settings.cache_clear()


def test_thread_reply_rows_inherit_the_root_grants(tmp_path, monkeypatch):
    """Same defect on the pre-existing thread loaders: `slack_thread`/`gmail_thread` ACL-filter
    row by row, so ungranted replies silently truncate a thread for non-admin callers."""
    settings = _import_gen(
        tmp_path,
        monkeypatch,
        "slack",
        "1711-foo.json",
        {
            "title_field_name": "file_name",
            "content_field_names": ["text"],
            "dataset_doc_uuid": "dsid_s_acl",
            "file_name": "1711-foo.json",
            "channel": "partnerships",
            "text": "andrea_p: Heads up on EU regions.\nmike_partner: On it, ETA next week.",
            "participants": ["andrea_p", "mike_partner"],
        },
        [{"name": "Andrea Park", "email": "andrea.park@redwoodinference.com", "title": "PM"}],
    )
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    root = _granted(conn, "dsid_s_acl")
    assert root
    replies = [r[0] for r in conn.execute("SELECT doc_id FROM slack_messages WHERE thread_seq > 0")]
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
        "https://raw.githubusercontent.com/onyx-dot-app/EnterpriseRAG-Bench/main/extra_questions.jsonl",
        p,
    )
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


@pytest.mark.skipif(
    os.environ.get("ERB_E2E") != "1",
    reason="set ERB_E2E=1 to run the network-backed faithful-import e2e",
)
def test_qst_0001_owner_is_maya_chen(tmp_path):
    data_dir = tmp_path / "data"
    qfile = Path(tmp_path) / "extra_questions.jsonl"
    _extra_questions(tmp_path)
    env = {**os.environ, "BACKLOT_DATA_DIR": str(data_dir)}
    subprocess.run(
        [sys.executable, "-m", "backlot.importer.erb", "--slice-questions", str(qfile)],
        check=True,
        env=env,
    )
    # dsid_fc36... is qst_0001's expected doc; owner must now be Maya Chen, not a hash pick
    with client_for(Settings(data_dir=data_dir)) as c:
        r = c.get(
            "/drive/v3/files/dsid_fc36d1d60e7e4b4abc7db84629563b7a",
            params={"fields": "owners(displayName)"},
            headers={"Authorization": "Bearer admin-service-token"},
        ).json()
        assert r["owners"][0]["displayName"] == "Maya Chen"


# ---------------------------------------------------------------------------
# Linear: the bench record -> the API-faithful schema
# ---------------------------------------------------------------------------
# The mapping is where the bench and the API disagree, so these assert the translations rather
# than the pass-throughs: P0-P3 -> Linear's 0-4, `status` -> `state`, a state category -> the
# lifecycle timestamps the bench never records, and the three comment shapes.

# A record shaped exactly like the bench's, per `sources/linear/agents.md`.
LINEAR_RAW = {
    "title_field_name": "title",
    "content_field_names": ["description", "comments"],
    "dataset_doc_uuid": "dsid_lin",
    "key": "ENG-49121",
    "team": "engineering",
    "title": "Variant-aware GPU allocation",
    "status": "In Progress",
    "priority": "P1",
    "created_at": "2025-02-18",
    "updated_at": "2025-03-04",
    "creator": "Amaya Chen",
    "assignee": "Diego Martinez",
    "project": "runtime-memory-2025",
    "cycle": "2025-W08",
    "estimate": "5",
    "due_date": "2025-03-15",
    "labels": ["kv-cache", "long-context"],
    "description": "Long-context configs push peak GPU memory into fragile regions.",
    "comments": [
        "2025-02-18 - Created: initial hypothesis captured.",
        "2025-02-20 Diego Martinez: ran baseline traces.",
    ],
}


def _load_linear(raw, dsid="dsid_lin"):
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    _load_one(conn, "linear", dsid, raw, P)
    row = conn.execute("SELECT * FROM linear_issues WHERE doc_id = ?", (dsid,)).fetchone()
    comments = conn.execute(
        "SELECT * FROM linear_comments WHERE doc_id = ? ORDER BY seq", (dsid,)
    ).fetchall()
    return conn, row, comments


def test_linear_maps_the_bench_record_onto_the_api_schema():
    _conn_, row, _c = _load_linear(LINEAR_RAW)
    assert row["team"] == "engineering"
    assert row["identifier"] == "ENG-49121"  # the bench key IS the Linear identifier
    assert row["state"] == "In Progress"  # `status` -> Linear's `state`
    assert row["priority"] == 2  # P1 -> Linear's scale (1 is most urgent)
    assert row["estimate"] == 5  # the bench writes it as a string
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
    P = Principals(
        [
            {"name": "A B", "email": "a.b@x.com", "dept_slug": "engineering"},
            {"name": "C D", "email": "c.d@x.com", "dept_slug": "product"},
            {"name": "E F", "email": "e.f@x.com", "dept_slug": "design-ux"},
        ],
        "x.com",
    )
    assert P.canonical_group("engineering") == "engineering"
    assert P.canonical_group("product-management") == "product"
    assert P.canonical_group("design") == "design-ux"


def test_linear_branch_name_is_derived_when_the_bench_has_none():
    _conn_, row, _c = _load_linear(LINEAR_RAW)
    assert row["branch_name"] == ("diegomartinez/eng-49121-variant-aware-gpu-allocation")


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
    """ "unassigned" is a literal value in the bench (11 docs). Linear stores no assignee for an
    unassigned issue, and minting a user called "unassigned" would pollute the roster."""
    _conn_, row, _c = _load_linear({**LINEAR_RAW, "assignee": "unassigned"})
    assert row["assignee_email"] is None and row["assignee_display"] is None


def test_linear_synthesizes_an_identifier_when_the_key_is_missing():
    _conn_, row, _c = _load_linear({k: v for k, v in LINEAR_RAW.items() if k != "key"})
    assert row["identifier"].startswith("ENG-")


def test_linear_comment_shapes_are_all_parsed():
    """The shapes measured across all 165,243 bench comments. The date and the name are peeled
    off INDEPENDENTLY — an earlier whole-line-alternatives parse put the dash pattern first, and
    since it had no name group it swallowed the author of 60,282 comments into the body."""
    parsed = erb.parse_linear_comments(
        [
            "2025-02-18 - Maya Patel: Filed initial PRD.",  # dash + name: the most common
            "2025-02-18 - Created: initial hypothesis captured.",  # dash + a LABEL, not a person
            "2026-03-05 Anjali Rao: Updated acceptance criteria.",  # no dash, name
            "2025-12-18 (Naomi Feldman): Include the audit log.",  # parenthesised name
            "Implementation notes: use model heuristics.",  # undated
        ]
    )
    assert [c["date"] for c in parsed] == [
        "2025-02-18",
        "2025-02-18",
        "2026-03-05",
        "2025-12-18",
        None,
    ]
    assert [c["name"] for c in parsed] == [
        "Maya Patel",
        "Created",
        "Anjali Rao",
        "Naomi Feldman",
        "Implementation notes",
    ]
    # `body` drops the prefix, `body_with_name` keeps it — the loader picks per comment, so an
    # unresolvable label like "Created:" never gets deleted from the text.
    assert parsed[0]["body"] == "Filed initial PRD."
    assert parsed[1]["body_with_name"] == "Created: initial hypothesis captured."


def test_linear_comment_author_prefix_is_kept_when_the_name_is_not_a_person():
    """ "Created:" and "Design review:" are labels, not attributions. If they don't resolve to
    somebody, the body must keep them rather than silently losing the words."""
    conn = _conn()
    P = Principals(
        [
            {
                "name": "Maya Patel",
                "email": "maya.patel@redwoodinference.com",
                "dept_slug": "engineering",
            }
        ],
        "redwoodinference.com",
    )
    raw = {
        **LINEAR_RAW,
        "creator": "Maya Patel",
        "assignee": "unassigned",
        "comments": [
            "2025-02-20 - Maya Patel: filed the PRD.",
            "2025-02-21 - Created: initial hypothesis captured.",
        ],
    }
    _load_one(conn, "linear", "dsid_p", raw, P)
    rows = conn.execute(
        "SELECT author_email, body FROM linear_comments WHERE doc_id='dsid_p' ORDER BY seq"
    ).fetchall()
    # resolved -> attributed, and the name is not repeated in the body
    assert rows[0]["author_email"] == "maya.patel@redwoodinference.com"
    assert rows[0]["body"] == "filed the PRD."
    # unresolved -> unattributed, and the text is intact
    assert rows[1]["author_email"] is None
    assert rows[1]["body"] == "Created: initial hypothesis captured."


def test_linear_undated_comment_never_sorts_before_its_dated_neighbours():
    """`Issue.comments` orders by createdAt (as Linear does). Anchoring an undated comment to the
    ISSUE's creation date put a trailing "Next steps:" note at the FRONT of 1,270 real threads."""
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {
        **LINEAR_RAW,
        "created_at": "2025-01-01",
        "comments": ["2025-02-01 - first", "2025-03-01 - second", "Next steps: trailing note"],
    }
    _load_one(conn, "linear", "dsid_m", raw, P)
    rows = conn.execute(
        "SELECT seq, created_ts FROM linear_comments WHERE doc_id='dsid_m' ORDER BY created_ts, seq"
    ).fetchall()
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert rows[2]["created_ts"] == rows[1]["created_ts"] + 1  # one second after its predecessor


def test_linear_parent_issue_is_stored_for_resolution():
    """`Issue.parent` is declared in the SDL and the bench fills `parent_issue` on 46.7% of
    records, so the key has to survive import for the resolver to look it up."""
    conn, row, _c = _load_linear({**LINEAR_RAW, "parent_issue": ["ENG-20297", "ENG-1"]})
    assert row["parent_key"] == "ENG-20297"  # a list -> the first; Linear has one parent
    conn2, row2, _ = _load_linear({**LINEAR_RAW, "parent_issue": "ENG-555"}, dsid="dsid_ps")
    assert row2["parent_key"] == "ENG-555"  # 552 bench docs use a bare string
    conn3, row3, _ = _load_linear(
        {k: v for k, v in LINEAR_RAW.items() if k != "parent_issue"}, dsid="dsid_pn"
    )
    assert row3["parent_key"] is None


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
    raw = {
        **LINEAR_RAW,
        "creator": "Amaya Chen",
        "assignee": "unassigned",
        "comments": [
            "2025-02-20 Amaya Chen: known person, resolved from the issue's creator.",
            "2025-02-21 Design review: a label, not a person.",
        ],
    }
    _load_one(conn, "linear", "dsid_c", raw, P)
    rows = conn.execute(
        "SELECT author_email FROM linear_comments WHERE doc_id='dsid_c' ORDER BY seq"
    ).fetchall()
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
    assert erb.linear_priority(3) == 3  # already Linear's scale
    assert erb.linear_priority("unrecognised") == 0  # Linear's "No priority"
    assert erb.linear_priority(None) is None


def test_linear_grants_flow_through_the_shared_container_path():
    """Linear needs no branch in `grants_for`: its container maps to a group like github/jira."""
    bundle = {"owner": "a@x.com", "people": ["b@x.com"], "group": "engineering", "org": "acme"}
    assert set(grants_for("linear", bundle)) == {
        ("user", "a@x.com"),
        ("user", "b@x.com"),
        ("group", "engineering"),
    }


# --- Linear relations / attachments / release (#25) ---------------------------------


def test_linear_relation_parsing_defaults_to_related():
    """Linear's vocabulary is blocks | duplicate | related. The bench lists a dependency as a bare
    key under a heading that means "depends on" only sometimes, so an unqualified entry becomes
    `related` — asserting `blocks` would invent a dependency graph the data does not state."""
    assert erb.parse_linear_relations(["ENG-31472"]) == [("related", "ENG-31472")]
    assert erb.parse_linear_relations(["blocks ENG-1"]) == [("blocks", "ENG-1")]
    assert erb.parse_linear_relations(["blocked by ENG-2"]) == [("blocks", "ENG-2")]
    assert erb.parse_linear_relations(["duplicate of ENG-3"]) == [("duplicate", "ENG-3")]
    assert erb.parse_linear_relations(["no key here"]) == []
    assert erb.parse_linear_relations("ENG-9") == [("related", "ENG-9")]  # 6 docs use a string


def test_linear_attachment_titles_are_never_empty():
    """`Attachment.title` is non-null in Linear. `links` carry `Label: URL`; `attachments` are
    bare URLs and need a derived title."""
    got = erb.parse_linear_attachments(
        ["Confluence: https://conf.example/a/design"], ["https://figma.example/frames.zip"]
    )
    assert got == [
        {"url": "https://conf.example/a/design", "title": "Confluence"},
        {"url": "https://figma.example/frames.zip", "title": "frames.zip"},
    ]
    assert all(a["title"] for a in got)


def test_linear_attachments_dedupe_across_the_two_bench_fields():
    """A doc can list the same link in both `links` and `attachments`."""
    got = erb.parse_linear_attachments(["https://x.example/a"], ["https://x.example/a"])
    assert len(got) == 1


def test_linear_release_takes_one_name():
    assert erb._linear_release(["runtime-1.19", "other"]) == "runtime-1.19"  # 8 docs use a list
    assert erb._linear_release("console-2025.02") == "console-2025.02"
    assert erb._linear_release(None) is None


def test_linear_second_pass_resolves_keys_and_drops_dangling():
    """The resolution has to be a SECOND pass (a target may load after its referrer) and has to
    happen at import (bench identifiers repeat, so a serve-time join on `identifier` would attach
    one issue's children to every issue sharing its key)."""
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    child = {
        **LINEAR_RAW,
        "key": "ENG-2",
        "parent_issue": ["ENG-1"],
        "dependencies": ["blocks ENG-1", "ENG-NOSUCH"],
    }
    parent = {**LINEAR_RAW, "key": "ENG-1", "parent_issue": None, "dependencies": None}
    # A relation names its target by KEY, and the index that resolves a key is built over the whole
    # corpus before any conversion — the half of the second pass that cannot be per-document.
    erb._precompute_globals([("linear", "d_child", child), ("linear", "d_parent", parent)])
    loader = byo._Loader(conn, "redwood", P.org_domain)
    # The child is loaded BEFORE its parent, which is the case a single pass cannot handle.
    _load_one(conn, "linear", "d_child", child, P, loader=loader)
    _load_one(conn, "linear", "d_parent", parent, P, loader=loader)
    loader.resolve_cross_references()

    row = conn.execute("SELECT parent_doc_id FROM linear_issues WHERE doc_id='d_child'").fetchone()
    assert row["parent_doc_id"] == "d_parent"
    rels = conn.execute("SELECT from_doc_id, to_doc_id, type FROM linear_relations").fetchall()
    # ENG-NOSUCH resolves to nothing and is dropped rather than stored dangling.
    assert [(r["from_doc_id"], r["to_doc_id"], r["type"]) for r in rels] == [
        ("d_child", "d_parent", "blocks")
    ]


def test_linear_second_pass_never_makes_an_issue_its_own_parent():
    """Reachable because bench keys repeat: two issues can share the key an issue names as its
    parent, and one of them can be the issue itself."""
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    raw = {**LINEAR_RAW, "key": "ENG-7", "parent_issue": ["ENG-7"], "dependencies": ["ENG-7"]}
    erb._precompute_globals([("linear", "d_self", raw)])
    _load_one(conn, "linear", "d_self", raw, P)
    assert (
        conn.execute("SELECT parent_doc_id FROM linear_issues WHERE doc_id='d_self'").fetchone()[0]
        is None
    )
    # A self-relation is dropped for the same reason: an issue is not related to itself.
    assert conn.execute("SELECT COUNT(*) FROM linear_relations").fetchone()[0] == 0


def test_linear_loader_stores_release_and_attachments():
    conn, row, _c = _load_linear(
        {
            **LINEAR_RAW,
            "release": "runtime-1.19",
            "links": ["Confluence: https://conf.example/x"],
            "attachments": ["https://figma.example/y.zip"],
        }
    )
    assert row["release"] == "runtime-1.19"
    atts = conn.execute(
        "SELECT title, url FROM linear_attachments WHERE doc_id='dsid_lin' ORDER BY seq"
    ).fetchall()
    assert [(a["title"], a["url"]) for a in atts] == [
        ("Confluence", "https://conf.example/x"),
        ("y.zip", "https://figma.example/y.zip"),
    ]


# ---------------------------------------------------------------------------
# fireflies: the bench -> API-schema mapping
#
# The bench ships a transcript as ONE FLAT TEXT BLOB (not structured per-sentence records), in
# several interchangeable line formats, often behind an auto-notes preamble whose "Date:" /
# "Duration:" lines look exactly like speaker lines. These cover the parse, the exact
# sentence<->content inverse, and the mapping onto the served columns.
# ---------------------------------------------------------------------------

FF_RAW = {
    "dataset_doc_uuid": "dsid_ff1",
    "_erb_path": "sales-calls/2026-04-02-northwind-latency-discovery.json",
    "title_field_name": "title",
    "content_field_names": ["summary", "topics", "action_items", "next_steps", "transcript"],
    "title": "Northwind — latency discovery",
    "meeting_id": "ff-20260402-northwind-001",
    "recorded_at": "2026-04-02T15:00:00Z",
    "duration_minutes": "32",
    "call_type": "discovery",
    "redwood_owner": "Ava Chen - Account Executive",
    "redwood_attendees": ["Ava Chen - Account Executive", "Bob Stone - Solutions Engineer"],
    "customer_company": "Northwind",
    "customer_attendees": ["Dana Ruiz, Head of Platform"],
    "summary": "Northwind wants sub-300ms p95.",
    "topics": ["latency budget", "batching"],
    "action_items": ["Ava: send the batching benchmark"],
    "next_steps": ["Schedule a follow-up"],
    "transcript": (
        "Meeting header:\n"
        "Date: 2026-04-02 15:00 UTC\n"
        "Duration: ~32 minutes\n"
        "\n"
        "[00:00] Ava: thanks for joining, let's start with the latency budget.\n"
        "00:14 - Dana: our p95 sits around 300ms today.\n"
        "00:31 [Bob]: batching is the likely suspect here.\n"
        "We can prove it with the benchmark.\n"
        "(00:52) Ava (Redwood AE): understood, I'll send it over.\n"
    ),
}


def _ff_load(raw=None, employees=None):
    """Load one fireflies record into an in-memory DB; return (conn, bundle)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store.SCHEMA)
    P = erb.Principals(
        employees
        if employees is not None
        else [
            {"name": "Ava Chen", "email": "ava.chen@acme.com", "dept_slug": "sales"},
            {"name": "Bob Stone", "email": "bob.stone@acme.com", "dept_slug": "engineering"},
        ],
        "acme.com",
    )
    bundle = _load_one(conn, "fireflies", "dsid_ff1", dict(raw or FF_RAW), P)
    return conn, bundle


def test_fireflies_parses_every_timestamp_and_speaker_form():
    sents = erb.parse_fireflies_transcript(
        FF_RAW["transcript"], ["Ava Chen", "Bob Stone", "Dana Ruiz"]
    )
    # "[00:00] Ava:", "00:14 - Dana:", "00:31 [Bob]:", "(00:52) Ava (Redwood AE):" — all four.
    assert [s["speaker_name"] for s in sents] == ["Ava Chen", "Dana Ruiz", "Bob Stone", "Ava Chen"]
    assert [s["start_time"] for s in sents] == [0.0, 14.0, 31.0, 52.0]
    # the un-prefixed line folds into the sentence above it, it does not become its own
    assert sents[2]["text"].endswith("We can prove it with the benchmark.")
    assert len(sents) == 4


def test_fireflies_auto_notes_preamble_never_mints_a_speaker():
    """ "Date: …" / "Duration: …" parse as `Name: text`; gating on the declared attendees is what
    stops them becoming speakers (and inventing 'Date' as a person)."""
    sents = erb.parse_fireflies_transcript(
        FF_RAW["transcript"], ["Ava Chen", "Bob Stone", "Dana Ruiz"]
    )
    assert "Date" not in {s["speaker_name"] for s in sents}
    assert "Duration" not in {s["speaker_name"] for s in sents}
    assert "Meeting header" not in {s["speaker_name"] for s in sents}


def test_fireflies_speaker_resolution_tolerates_first_names_and_initials():
    m = erb.fireflies_speaker_map(["Ava Chen - Account Executive", "Dana Ruiz, Head of Platform"])
    assert erb._ff_resolve_speaker("Ava", m) == "Ava Chen"  # first name only
    assert erb._ff_resolve_speaker("Ava C.", m) == "Ava Chen"  # first + initial
    assert erb._ff_resolve_speaker("Moderator - Ava", m) == "Ava Chen"  # role-prefixed
    assert erb._ff_resolve_speaker("Dana Ruiz", m) == "Dana Ruiz"  # role stripped from the decl
    assert erb._ff_resolve_speaker("Someone Else", m) is None


def test_fireflies_anonymous_speakers_survive_when_nobody_is_recognized():
    """The corpus deliberately contains transcripts labeled only "Speaker 1"/"Speaker 2"
    (agents.md calls for it). Gating would drop every one, so it falls back to ungated."""
    sents = erb.parse_fireflies_transcript(
        "[00:00] Speaker 1: kickoff.\n[00:10] Speaker 2: agreed.\n", ["Ava Chen"]
    )
    assert [s["speaker_name"] for s in sents] == ["Speaker 1", "Speaker 2"]


def test_fireflies_content_is_the_exact_inverse_of_the_sentences():
    conn, _ = _ff_load()
    row = conn.execute("SELECT content FROM fireflies_transcripts").fetchone()
    stored = [
        {"speaker_name": r["speaker_name"], "text": r["body"]}
        for r in conn.execute("SELECT speaker_name, body FROM fireflies_sentences ORDER BY seq")
    ]
    assert synth.fireflies_transcript_text(stored) == row["content"]


def test_fireflies_parse_is_a_fixed_point():
    """parse -> join -> parse must return the same sentences, which is what makes `content` a
    safe definition rather than a second copy that can drift."""
    people = ["Ava Chen", "Bob Stone", "Dana Ruiz"]
    once = erb.parse_fireflies_transcript(FF_RAW["transcript"], people)
    text = synth.fireflies_transcript_text(once)
    twice = erb.parse_fireflies_transcript(text, people)
    assert synth.fireflies_transcript_text(twice) == text
    assert [s["speaker_name"] for s in twice] == [s["speaker_name"] for s in once]


def test_fireflies_summary_notes_are_not_folded_into_the_sentences():
    """The bench lists summary/topics/action_items in content_field_names, but they are auto-notes,
    not speech. Folding them into `content` would make `scope: sentences` match words nobody said
    (and break the round-trip), so they map onto the API's `summary` object instead."""
    conn, _ = _ff_load()
    row = conn.execute("SELECT content, summary FROM fireflies_transcripts").fetchone()
    assert "sub-300ms" not in row["content"]  # the summary prose is NOT in the sentences
    summary = json.loads(row["summary"])
    assert summary["overview"] == "Northwind wants sub-300ms p95."
    assert summary["topics_discussed"] == ["latency budget", "batching"]
    assert summary["action_items"] == ["Ava: send the batching benchmark"]
    assert summary["outline"] == ["Schedule a follow-up"]  # next_steps -> outline
    assert summary["meeting_type"] == "discovery"  # call_type -> meeting_type


def test_fireflies_maps_the_bench_onto_the_api_columns():
    conn, bundle = _ff_load()
    row = conn.execute("SELECT * FROM fireflies_transcripts").fetchone()
    assert row["title"] == "Northwind — latency discovery"
    # the bench subdirectory is the workspace -> the channel, and the channel IS the ACL group
    assert row["channel"] == "sales-calls"
    assert bundle["group"] == "sales-calls"
    assert conn.execute("SELECT group_id FROM fireflies_channels").fetchone()[0] == "sales-calls"
    # host resolves through the directory; the display name keeps the bench's own spelling
    assert row["author_email"] == "ava.chen@acme.com"
    assert row["owner_display"] == "Ava Chen"
    # duration is MINUTES (the API's unit), parsed out of the bench's string
    assert row["duration"] == 32.0
    assert row["created_ts"] == 1775142000  # 2026-04-02T15:00:00Z
    # meeting_id is NOT unique in the corpus, so it becomes calendar_id and `id` is synthesized
    assert row["calendar_id"] == "ff-20260402-northwind-001"
    assert row["transcript_id"] == synth.fireflies_id("dsid_ff1")
    assert row["transcript_url"].endswith(row["transcript_id"])
    assert row["audio_url"] and row["video_url"] and row["meeting_link"]


def test_fireflies_resolves_internal_and_external_attendees_differently():
    """No email appears anywhere in the Fireflies corpus, so identities come from Principals:
    Redwood attendees become org users, customer attendees external contacts (never principals)."""
    conn, bundle = _ff_load()
    attendees = json.loads(
        conn.execute("SELECT meeting_attendees FROM fireflies_transcripts").fetchone()[0]
    )
    by_name = {a["displayName"]: a for a in attendees}
    assert by_name["Ava Chen"]["email"] == "ava.chen@acme.com"
    assert by_name["Dana Ruiz"]["email"].endswith("@external.example")
    assert by_name["Dana Ruiz"]["location"] == "Northwind"  # the customer company
    # only addresses that can authenticate become ACL grants
    assert all(not e.endswith("@external.example") for e in bundle["people"])
    assert "ava.chen@acme.com" in bundle["people"]


def test_fireflies_speaker_ids_are_per_meeting_ordinals():
    conn, _ = _ff_load()
    rows = conn.execute(
        "SELECT speaker_name, speaker_id FROM fireflies_sentences ORDER BY seq"
    ).fetchall()
    assert [r["speaker_id"] for r in rows] == [0, 1, 2, 0]  # Ava reuses her own ordinal
    assert all(isinstance(r["speaker_id"], int) for r in rows)


def test_fireflies_sentences_sit_on_the_meetings_clock():
    conn, _ = _ff_load()
    base = conn.execute("SELECT created_ts FROM fireflies_transcripts").fetchone()[0]
    rows = conn.execute(
        "SELECT created_ts, start_time FROM fireflies_sentences ORDER BY seq"
    ).fetchall()
    assert [r["created_ts"] for r in rows] == [base + int(r["start_time"]) for r in rows]


def test_fireflies_only_resolvable_speakers_get_an_identity():
    conn, _ = _ff_load()
    rows = conn.execute(
        "SELECT speaker_name, author_email FROM fireflies_sentences ORDER BY seq"
    ).fetchall()
    assert rows[0]["author_email"] == "ava.chen@acme.com"  # a declared Redwood attendee
    assert rows[1]["author_email"] is None  # Dana is external, not an identity
    assert rows[1]["speaker_name"] == "Dana Ruiz"  # but her label is still served


def test_fireflies_transcript_without_speaker_labels_still_serves_its_text():
    """17 bench transcripts are prose with no speaker labels. `content` is defined as the sentence
    concatenation, so the body becomes ONE unattributed sentence rather than an empty document."""
    raw = {**FF_RAW, "transcript": "Just prose about the meeting. No labels at all."}
    conn, _ = _ff_load(raw)
    row = conn.execute("SELECT content FROM fireflies_transcripts").fetchone()
    assert row["content"] == "Just prose about the meeting. No labels at all."
    sent = conn.execute("SELECT speaker_name, body FROM fireflies_sentences").fetchone()
    assert sent["speaker_name"] is None  # honest: no label was produced
    assert sent["body"] == row["content"]


def test_fireflies_transcript_missing_entirely_falls_back_to_the_envelope():
    """3 bench documents carry no transcript field at all; the ERB envelope's own derived content
    is used so the meeting is not served empty."""
    raw = {k: v for k, v in FF_RAW.items() if k != "transcript"}
    conn, _ = _ff_load(raw)
    assert conn.execute("SELECT content FROM fireflies_transcripts").fetchone()["content"]


def test_fireflies_is_org_visible_like_slack_and_hubspot():
    """The corpus names 1,104 distinct hosts of whom only the ~167 directory employees can
    authenticate, so an owner-or-channel scope would leave ~91% of transcripts admin-only."""
    grants = erb.grants_for(
        "fireflies",
        {
            "org": "acme",
            "group": "sales-calls",
            "owner": "ava.chen@acme.com",
            "people": ["bob.stone@acme.com"],
        },
    )
    assert ("org", "acme") in grants
    assert ("user", "ava.chen@acme.com") in grants
    assert ("user", "bob.stone@acme.com") in grants
    assert not any(t == "group" for t, _ in grants)


def test_fireflies_external_addresses_never_become_acl_grants():
    grants = erb.grants_for(
        "fireflies",
        {
            "org": "acme",
            "group": "sales-calls",
            "owner": "ava.chen@acme.com",
            "people": ["dana.ruiz@external.example"],
        },
    )
    assert not any(pid.endswith("@external.example") for _, pid in grants)


def test_fireflies_duration_parses_every_shape_the_bench_writes():
    assert erb._ff_duration("72") == 72.0
    assert erb._ff_duration(64) == 64.0
    assert erb._ff_duration("~46 minutes") == 46.0
    assert erb._ff_duration("about 52 min") == 52.0
    assert erb._ff_duration(None) is None
    assert erb._ff_duration("unknown") is None


def test_fireflies_list_valued_transcript_is_joined():
    """125 bench documents carry `transcript` as a LIST of already-split utterances."""
    raw = {**FF_RAW, "transcript": ["[00:00] Ava: first.", "[00:10] Dana: second."]}
    conn, _ = _ff_load(raw)
    rows = conn.execute("SELECT speaker_name FROM fireflies_sentences ORDER BY seq").fetchall()
    assert [r["speaker_name"] for r in rows] == ["Ava Chen", "Dana Ruiz"]


def test_fireflies_escaped_newlines_are_unescaped_before_parsing():
    """Some bench documents carry literal ``\\n`` instead of real newlines; left as-is the whole
    transcript collapses to one line and only the first speaker is ever found."""
    raw = {**FF_RAW, "transcript": "[00:00] Ava: first.\\n[00:10] Dana: second."}
    conn, _ = _ff_load(raw)
    assert conn.execute("SELECT COUNT(*) FROM fireflies_sentences").fetchone()[0] == 2


def test_fireflies_root_level_document_lands_in_uncategorized():
    """11 bench documents sit at the source root, which its own agents.md says should not happen."""
    conn, _ = _ff_load({**FF_RAW, "_erb_path": "2026-04-02-a-meeting.json"})
    assert (
        conn.execute("SELECT channel FROM fireflies_transcripts").fetchone()[0] == "uncategorized"
    )


def test_fireflies_erb_path_is_not_hubspot_property_data():
    """iter_records injects `_erb_path`; it must never leak into a served field."""
    assert "_erb_path" in erb._HS_NOT_A_PROPERTY


# ---------------------------------------------------------------------------
# ERB -> BYO-JSONL -> DB equivalence (#17)
#
# The unified dataset redistributes ERB pre-converted into BYO-JSONL, which only works if
# BYO-JSONL can hold everything the loaders above write. So the acceptance criterion is a DIFF of
# two databases, not a spot check: import ERB directly, convert the same ERB to BYO-JSONL, import
# THAT, and require every table to match row for row. Anything a loader can express and the BYO
# mapping cannot shows up here as a column that differs.
# ---------------------------------------------------------------------------

RT_EMPLOYEES = {
    "Engineering": [
        {"name": "Ava Chen", "email": "ava.chen@redwoodinference.com", "title": "SRE"},
        # accented + a middle initial: `_slug` would mangle both, so a converted artifact cannot
        # recover these display names from the email — the roster sidecar has to carry them.
        {"name": "Tomás Rré", "email": "tomas.rre@redwoodinference.com", "title": "Eng"},
    ],
    "Research & Applied ML": [
        {"name": "Maya Chen", "email": "maya.chen@redwoodinference.com", "title": "RS"},
    ],
}

# One document per source, each carrying the fields that only `erb.py` could express: confluence
# confidentiality/owner_team/reviewers, drive collaborators + a `doc_type`, jira severity/squad,
# a multi-message gmail thread, a multi-turn slack transcript (plus a far-future ts that only the
# rank-based remap can place), hubspot notes, and a linear issue with a parent/relation.
RT_DOCS = {
    "confluence": {
        "restricted.json": {
            "title_field_name": "title",
            "content_field_names": ["body"],
            "dataset_doc_uuid": "dsid_conf_1",
            "title": "Gateway incident runbook",
            "body": "Roll back the gateway, then page the on-call.",
            "space": "ENG",
            "owner_team": "engineering",
            "author": "Ava Chen",
            # 'Zoe Newperson' is not in the directory -> synthesized user, and a reviewer takes no
            # group hint, so it must land in the roster with no group.
            "reviewers": ["Maya Chen", "Zoe Newperson"],
            "confidentiality": "restricted (customer-sensitive)",
            "labels": ["oncall", "runbook"],
            "created_at": "2026-01-05",
            "last_updated": "2026-02-01",
        },
        "internal.json": {
            "title_field_name": "title",
            "content_field_names": ["body"],
            "dataset_doc_uuid": "dsid_conf_2",
            "title": "Handbook",
            "body": "How we work.",
            "space": "HANDBOOK",
            "author": "Maya Chen",
            "confidentiality": "internal",
            "created_at": "2026-01-06",
        },
    },
    "google_drive": {
        "model.json": {
            "title_field_name": "title",
            "content_field_names": ["body"],
            "dataset_doc_uuid": "dsid_drive_1",
            "title": "Q1 revenue model",
            "body": "month,revenue\nJan,120000",
            "team": "Research & Applied ML",
            "drive_area": "research",
            "owner": "Maya Chen",
            "collaborators": ["Ava Chen", "Ravi Other"],
            "doc_type": "sheet",
            "created_at": "2026-01-02",
            "last_modified": "2026-01-09",
        },
        "teamless.json": {
            "title_field_name": "title",
            "content_field_names": ["body"],
            "dataset_doc_uuid": "dsid_drive_2",
            "title": "Scratch",
            "body": "notes",
            "owner": "Ava Chen",
            "doc_type": "doc",
            "created_at": "2026-01-03",
        },
    },
    "jira": {
        "latency.json": {
            "title_field_name": "summary",
            "content_field_names": ["description"],
            "dataset_doc_uuid": "dsid_jira_1",
            "summary": "SEV1: checkout latency spike",
            "description": "p95 checkout latency jumped to 2.1s.",
            "project": "PAY",
            "squad": "engineering",
            "reporter": "Ava Chen",
            "assignee": "Maya Chen",
            "severity": "Sev1",
            "status": "In Progress",
            "issue_type": "Incident",
            "priority": "P1",
            "labels": ["latency"],
            "components": ["gateway"],
            "comments": [
                "2026-01-06 Maya Chen: looking now",
                "2026-01-07 Zoe Newperson: rolled back",
            ],
            "created_at": "2026-01-05",
            "updated_at": "2026-01-08",
            "due_date": "2026-02-01",
        },
    },
    "github": {
        "pr.json": {
            "title_field_name": "title",
            "content_field_names": ["body"],
            "dataset_doc_uuid": "dsid_gh_1",
            "title": "Fix token-bucket refill off-by-one",
            "body": "Corrects the refill tick; adds a test.",
            "repo": "gateway",
            "author": "Ava Chen",
            "reviewers": ["Maya Chen"],
            "state": "closed",
            "labels": ["bug"],
            "pr_number": 42,
            "created_at": "2026-01-03",
            "updated_at": "2026-01-04",
        },
    },
    "gmail": {
        "thread.json": {
            "title_field_name": "subject",
            "content_field_names": ["messages"],
            "dataset_doc_uuid": "dsid_gm_1",
            "subject": "[P0] Acme Health — retry storm",
            "mailbox_owner": "Ava Chen",
            "participants_internal": ["Maya Chen"],
            "attachments": ["postmortem.pdf"],
            "first_email_at": "2026-01-04T09:00:00Z",
            "messages": [
                "From: Ava Chen <ava.chen@redwoodinference.com>\n"
                "To: ops@redwoodinference.com\nCc: maya.chen@redwoodinference.com\n"
                "Date: Mon, 04 Jan 2026 09:00:00 -0800\nSubject: [P0] retry storm\n"
                "Message-ID: <a@redwood>\n\nSeeing 5xx spikes from the gateway.",
                "From: Maya Chen <maya.chen@redwoodinference.com>\n"
                "To: ava.chen@redwoodinference.com\n"
                "Date: Mon, 04 Jan 2026 10:00:00 -0800\nSubject: Re: [P0] retry storm\n"
                "Message-ID: <b@redwood>\n\nOn it — draining the bad pool.",
                # no Date header: its time is the root's clock + an hour per position, which the
                # converted record has to carry explicitly.
                "From: ops-bot@redwoodinference.com\nSubject: Re: [P0] retry storm\n\nAuto-ack.",
            ],
        },
        "single.json": {
            "title_field_name": "subject",
            "content_field_names": ["body"],
            "dataset_doc_uuid": "dsid_gm_2",
            "subject": "Lunch",
            "mailbox_owner": "Maya Chen",
            "body": "Anyone up for lunch?",
        },
    },
    "slack": {
        "thread.json": {
            "title_field_name": "channel",
            "content_field_names": ["messages"],
            "dataset_doc_uuid": "dsid_sl_1",
            "channel": "incidents",
            "participants": ["ava", "maya_r", "infra-bot"],
            "first_message_ts": "1767513600",
            "messages": "ava: Anyone seeing 502s from the gateway?\n"
            "maya_r: Looking now.\ninfra-bot: alert cleared",
        },
        "future.json": {
            "title_field_name": "file_name",
            "content_field_names": ["text"],
            "dataset_doc_uuid": "dsid_sl_2",
            "file_name": "0001-eu.json",
            "channel": "partnerships",
            "participants": ["andrea_p"],
            # beyond the year-2035 cutoff: rank-based and order-preserving, so the remapped value
            # cannot be recomputed from this record alone and has to be baked into the artifact.
            "first_message_ts": "9999999999",
            "text": "andrea_p: EU regions land next week.",
        },
    },
    "hubspot": {"company.json": {**HS_RAW, "dataset_doc_uuid": "dsid_hs_1"}},
    # Fireflies' container comes from the DIRECTORY LAYOUT, not a field, so the subdirectory here
    # is load-bearing: `iter_records` injects `_erb_path` and the first segment is the channel.
    "fireflies": {
        "sales-calls/discovery.json": {
            "title_field_name": "title",
            "content_field_names": ["transcript"],
            "dataset_doc_uuid": "dsid_ff_1",
            "title": "Acacia Loop — discovery",
            "meeting_id": "mtg-4471",
            "recorded_at": "2026-02-19T15:00:00Z",
            "duration_minutes": "42",
            "call_type": "discovery",
            "redwood_owner": "Maya Chen",
            "redwood_attendees": ["Ava Chen"],
            "customer_company": "Acacia Loop Services",
            "customer_attendees": ["Dana Ruiz, CTO"],
            "summary": "Discovery call on latency and KMS.",
            "topics": ["latency", "kms"],
            "action_items": ["Maya: send pricing"],
            # Six line formats and an auto-notes preamble whose "Date:"/"Duration:" lines look
            # exactly like speaker lines — the parse has to agree between the two importers.
            "transcript": "Date: 2026-02-19\nDuration: ~42 minutes\n"
            "[00:00] Maya Chen: Thanks for making time.\n"
            "00:18 - Dana Ruiz: Our p95 sits at 300ms.\n"
            "00:41 [Ava Chen]: We can cut that with the two-tier cache.\n"
            "And the gateway change lands next week.\n"
            "(01:05) Speaker 3: What about KMS?",
        },
        "uncategorized-root.json": {
            "title_field_name": "title",
            "content_field_names": ["transcript"],
            "dataset_doc_uuid": "dsid_ff_2",
            "title": "Untitled sync",
            "recorded_at": "2026-03-01T09:00:00Z",
            # no attendees at all -> ungated parse; and a root-level file -> "uncategorized"
            "transcript": "Speaker 1: quick sync.\nSpeaker 2: agreed.",
        },
    },
    "linear": {
        "child.json": {
            "title_field_name": "title",
            "content_field_names": ["description"],
            "dataset_doc_uuid": "dsid_lin_1",
            "title": "Ship the two-tier cache",
            "description": "Cache the gateway's hot path.",
            "key": "ENG-7",
            "team": "engineering",
            "status": "Done",
            "priority": "P1",
            "creator": "Ava Chen",
            "assignee": "Maya Chen",
            "estimate": "5",
            "labels": ["cache"],
            "project": "gateway",
            "cycle": "Cycle 41",
            "due_date": "2026-04-01",
            "release": "runtime-1.19",
            "created_at": "2026-01-01",
            "updated_at": "2026-03-20",
            "links": ["Design: https://example.com/design"],
            "attachments": ["https://example.com/bench.zip"],
            "comments": ["2026-02-01 - Maya Chen: rolled out to 10%", "Created: initial scope"],
            "parent_issue": ["ENG-8"],
            "dependencies": ["blocks ENG-8"],
        },
        "parent.json": {
            "title_field_name": "title",
            "content_field_names": ["description"],
            "dataset_doc_uuid": "dsid_lin_2",
            "title": "Caching epic",
            "description": "Umbrella.",
            "key": "ENG-8",
            "team": "engineering",
            "status": "In Progress",
            "priority": "P2",
            "creator": "Maya Chen",
            "assignee": "unassigned",
            "created_at": "2026-01-01",
        },
    },
}


def _write_generated_data(root: Path) -> Path:
    """Materialize an ERB ``generated_data/`` tree from RT_DOCS."""
    gen = root / "gen"
    (gen).mkdir(parents=True, exist_ok=True)
    (gen / "employee_directory.yaml").write_text(yaml.safe_dump({"departments": RT_EMPLOYEES}))
    for src, docs in RT_DOCS.items():
        d = gen / "sources" / src
        d.mkdir(parents=True, exist_ok=True)
        for name, raw in docs.items():
            # a name may carry a subdirectory — Fireflies' container IS the layout
            (d / name).parent.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(json.dumps(raw))
    return gen


def _dump_db(path) -> dict[str, list]:
    """Every servable table as sorted row tuples, so two DBs can be compared table by table.

    Excludes ``meta``: it holds build-PROVENANCE facts, not servable content, and
    ``source_documents`` in particular counts a different unit for the two round-trip sides on
    purpose — a HubSpot company's notes are sub-parts of ONE bench document to a direct import, but
    independently-addressable top-level documents to a BYO corpus that was exported and re-loaded
    (see ``_byo_hubspot``). Forcing them equal would mean one side counting wrong.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'docs_fts%' AND name NOT LIKE 'sqlite_%' AND name != 'meta' "
                "ORDER BY name"
            )
        ]
        out = {}
        for t in tables:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})")]
            out[t] = sorted(
                (tuple(r[c] for c in cols) for r in conn.execute(f"SELECT * FROM {t}")), key=repr
            )
        return out
    finally:
        conn.close()


def _import_erb_directly(gen: Path, data_dir: Path):
    from backlot.config import Settings

    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=data_dir)
    shutil.copy(gen / "employee_directory.yaml", settings.employee_yaml)
    erb.import_structured(settings, gen)
    return settings


def _import_via_byo(gen: Path, data_dir: Path, out_dir: Path):
    from backlot.config import Settings
    from backlot.importer import byo

    data_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=data_dir)
    shutil.copy(gen / "employee_directory.yaml", settings.employee_yaml)
    erb.export_byo(settings, gen, out_dir)
    byo.load(out_dir / "corpus.jsonl", settings, roster=out_dir / "roster.yaml")
    return settings


def test_erb_to_byo_round_trip_builds_an_equivalent_database(tmp_path):
    """ERB -> BYO-JSONL -> DB must be indistinguishable from ERB -> DB, table by table, including
    doc_acl, principals, group_members and every per-service column.

    Both paths now share one mapping (`to_byo`), so this no longer guards two implementations
    against drift. What it still guards is the SERIALIZATION: that writing the converted records
    out as JSONL and reading them back is lossless — the encoding, the sharding, the manifest and
    the roster sidecar all round-trip."""
    gen = _write_generated_data(tmp_path)
    direct = _import_erb_directly(gen, tmp_path / "direct")
    viabyo = _import_via_byo(gen, tmp_path / "viabyo", tmp_path / "artifact")

    a, b = _dump_db(direct.db_path), _dump_db(viabyo.db_path)
    assert set(a) == set(b), "table sets differ"
    for t in sorted(a):
        assert a[t] == b[t], (
            f"table {t} differs\n  only in direct: {[r for r in a[t] if r not in b[t]]}\n"
            f"  only via byo:  {[r for r in b[t] if r not in a[t]]}"
        )

    # meta.source_documents is excluded from _dump_db on purpose (see its docstring) — the two
    # sides count a different unit — so it is not silently left unchecked; it gets its own pinned
    # assertion instead. RT_DOCS holds 15 bench documents (2 confluence + 2 google_drive + 1 jira +
    # 1 github + 2 gmail + 2 slack + 1 hubspot + 2 fireflies + 2 linear), none excluded (every one
    # has real content), so:
    #   direct:  source_documents = 15 documents + 0 excluded = 15
    # Only `_byo_hubspot` fans a bench document out into more than one top-level BYO record (the
    # company plus its 2 notes in HS_RAW); every other converter returns exactly one. So the
    # exported-and-reloaded corpus holds 15 - 1 + 3 = 17 top-level BYO documents, and `byo.load()`
    # counts one per document at ITS OWN granularity (a JSONL line):
    #   via byo: source_documents = 17
    conn_direct, conn_viabyo = sqlite3.connect(direct.db_path), sqlite3.connect(viabyo.db_path)
    assert store.read_meta(conn_direct, "source_documents") == "15"
    assert store.read_meta(conn_viabyo, "source_documents") == "17"
    conn_direct.close()
    conn_viabyo.close()


def test_erb_to_byo_round_trip_writes_the_same_tokens(tmp_path):
    """`tokens.yaml` is the roster a caller authenticates with, so the converted artifact has to
    reproduce it exactly — including the rule that only the employee directory gets a token."""
    gen = _write_generated_data(tmp_path)
    direct = _import_erb_directly(gen, tmp_path / "direct")
    viabyo = _import_via_byo(gen, tmp_path / "viabyo", tmp_path / "artifact")

    ta = yaml.safe_load(direct.tokens_path.read_text())
    tb = yaml.safe_load(viabyo.tokens_path.read_text())
    assert ta["org"] == tb["org"] and ta["org_domain"] == tb["org_domain"]
    key = lambda us: sorted((u["email"], u["name"], u["token"]) for u in us)  # noqa: E731
    assert key(ta["users"]) == key(tb["users"])


def test_erb_to_byo_output_validates_against_the_byo_schemas(tmp_path):
    """--dry-run has to still catch a bad corpus, so the converted artifact must pass the very
    same validator a hand-written corpus does — no private back door into the loader."""
    from backlot.validation import validate_file
    from backlot.config import Settings

    gen = _write_generated_data(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    settings = Settings(data_dir=data)
    shutil.copy(gen / "employee_directory.yaml", settings.employee_yaml)
    out = tmp_path / "artifact"
    out.mkdir()
    erb.export_byo(settings, gen, out)
    assert validate_file(out / "corpus.jsonl") == []


# ---------------------------------------------------------------------------
# fidelity fixes the round-trip exposed
# ---------------------------------------------------------------------------


def test_byo_drive_subtypes_are_all_accepted_by_the_schema():
    """`_drive_type` is the mock's Drive subtype vocabulary (#23), and a converted record has to
    carry its output — so the BYO drive schema must accept every value it can produce, or an
    artifact fails validation on a file type the importer itself created."""
    from backlot.validation import record_errors

    for doc_type, title in (
        ("doc", "Runbook"),
        ("sheet", "Model"),
        ("slides", "Deck"),
        ("pdf", "MSA"),
        ("folder", "Deals"),
        (None, "Notes"),
        (None, "redlines.docx"),
        (None, "export.csv"),
        (None, "logo.png"),
    ):
        raw = {
            "title_field_name": "title",
            "content_field_names": ["body"],
            "title": title,
            "body": "x",
            **({"doc_type": doc_type} if doc_type else {}),
        }
        subtype, mime_type = erb._drive_type(raw, title)
        errs = record_errors(
            {
                "source_type": "google_drive",
                "folder": "f",
                "title": title,
                "content": "x",
                "subtype": subtype,
                **({"mime_type": mime_type} if mime_type else {}),
            }
        )
        assert errs == [], f"{doc_type or title} -> subtype {subtype!r}: {errs}"


def test_drive_folder_row_exists_even_without_a_team():
    """A file's folder and its folder row are the same expression: a doc with no team used to be
    filed in a folder `gdrive_folders` had no row for (group_id is nullable; the row is not)."""
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    _load_one(
        conn,
        "google_drive",
        "dsid_nt",
        {
            "title_field_name": "title",
            "content_field_names": ["body"],
            "title": "Scratch",
            "body": "x",
            "owner": "Ava Chen",
        },
        P,
    )
    folder = conn.execute("SELECT folder FROM gdrive_files WHERE doc_id='dsid_nt'").fetchone()[0]
    row = conn.execute("SELECT * FROM gdrive_folders WHERE folder=?", (folder,)).fetchone()
    assert row is not None and row["group_id"] is None


def test_unresolvable_principals_are_dropped_not_stored_as_nulls():
    """`P.resolve` returns None for a reference that is not a person. Such a name must not hold a
    slot in a list of principals — `requested_reviewers` is rendered per entry into a GitHub user,
    so a null 500s the pull-request endpoint (8 bench documents carry one)."""
    from backlot.routers.github import _gh_user

    with pytest.raises(AttributeError):
        _gh_user(None)  # the crash the null caused

    conn = _conn()
    P = Principals(
        [
            {
                "name": "Ava Chen",
                "email": "ava.chen@redwoodinference.com",
                "dept_slug": "engineering",
            }
        ],
        "redwoodinference.com",
    )
    raw = {
        "title_field_name": "title",
        "content_field_names": ["body"],
        "title": "PR",
        "body": "x",
        "repo": "gateway",
        "author": "Ava Chen",
        # 'Customer Success Team' is a team label, not a person -> resolves to nobody
        "reviewers": ["Ava Chen", "Customer Success Team"],
    }
    _load_one(conn, "github", "dsid_rv", raw, P)
    stored = json.loads(
        conn.execute(
            "SELECT requested_reviewers FROM github_items WHERE doc_id='dsid_rv'"
        ).fetchone()[0]
    )
    assert stored == ["ava.chen@redwoodinference.com"]
    assert None not in stored


def test_slack_thread_id_only_when_the_transcript_has_replies():
    """Real Slack puts `thread_ts` on a message that is part of a thread and leaves it off a
    standalone post — and the router reads this column to decide."""
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    single = {
        "title_field_name": "file_name",
        "content_field_names": ["text"],
        "file_name": "f.json",
        "channel": "partnerships",
        "participants": ["andrea_p"],
        "text": "andrea_p: EU regions land next week.",
    }
    _load_one(conn, "slack", "dsid_one", single, P)
    assert (
        conn.execute("SELECT thread_id FROM slack_messages WHERE doc_id='dsid_one'").fetchone()[0]
        is None
    )
    threaded = {
        **single,
        "participants": ["andrea_p", "mike_p"],
        "text": "andrea_p: EU regions?\nmike_p: next week.",
    }
    _load_one(conn, "slack", "dsid_two", threaded, P)
    assert (
        conn.execute("SELECT thread_id FROM slack_messages WHERE doc_id='dsid_two'").fetchone()[0]
        == "dsid_two"
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM slack_messages WHERE thread_id='dsid_two'").fetchone()[0]
        == 2
    )


def test_hubspot_properties_are_stored_as_canonical_json():
    """The stored JSON must not depend on the source file's key order, or two importers (or two
    re-imports of a rewritten file) disagree byte for byte over identical data."""
    conn = _conn()
    P = Principals([], "redwoodinference.com")
    _load_one(conn, "hubspot", "dsid_a", {**HS_RAW}, P)
    _load_one(conn, "hubspot", "dsid_b", {k: HS_RAW[k] for k in reversed(list(HS_RAW))}, P)
    a, b = (
        conn.execute("SELECT properties FROM hubspot_objects WHERE doc_id=?", (d,)).fetchone()[0]
        for d in ("dsid_a", "dsid_b")
    )
    assert a == b


def test_export_byo_writes_a_roster_carrying_names_and_who_may_authenticate(tmp_path):
    """The roster is the half of a converted artifact the records cannot hold: `_slug` is lossy, so
    a display name is unrecoverable from an email, and only the directory may authenticate."""
    P = Principals(
        [
            {
                "name": "Tomás Rré",
                "email": "tomas.rre@redwoodinference.com",
                "dept_slug": "engineering",
            }
        ],
        "redwoodinference.com",
    )
    # a name resolved during load that is NOT in the directory
    P.resolve("Zoe Newperson", role="owner", group_hint="research-applied-ml")
    P.resolve("Ravi Other", role="collaborator")

    from backlot.config import Settings

    settings = Settings(data_dir=tmp_path, org_name="redwood", org_domain="redwoodinference.com")
    out = tmp_path / "roster.yaml"
    P.write_roster(out, settings)
    roster = yaml.safe_load(out.read_text())

    assert roster["org"] == "redwood" and roster["org_domain"] == "redwoodinference.com"
    # the directory user keeps its accented name and sits under its group
    assert roster["departments"] == {
        "engineering": [{"name": "Tomás Rré", "email": "tomas.rre@redwoodinference.com"}]
    }
    contacts = {c["email"]: c for c in roster["contacts"]}
    assert contacts["zoe.newperson@redwoodinference.com"]["group"] == "research-applied-ml"
    # a collaborator takes no group hint, so it has none
    assert "group" not in contacts["ravi.other@redwoodinference.com"]

    # ...and byo reads exactly this back
    from backlot.importer.byo import load_roster

    parsed = load_roster(out)
    assert parsed["users"]["tomas.rre@redwoodinference.com"] == {
        "name": "Tomás Rré",
        "group": "engineering",
        "token": True,
    }
    assert parsed["users"]["ravi.other@redwoodinference.com"]["token"] is False


def test_conversion_does_not_depend_on_document_order():
    """`Principals` LEARNS: `resolve` registers a person the first time a document names them, and
    `display_email` only finds someone already registered. So converting in a single pass made the
    OUTPUT depend on which document was read first — here, whether a comment by "Nadia Weber" is
    attributed to her or left with the name as a text prefix, depending on whether the issue she
    authored happened to be converted before the issue she commented on.

    Measured on a 2,555-document bench slice, that order sensitivity moved 27 doc_acl grants and
    one comment attribution. `_populate_principals` resolves everything before anything is
    converted, so the corpus converts to the same records either way."""
    from backlot.config import Settings

    settings = Settings(data_dir=Path("/nonexistent"))
    settings.org_name, settings.org_domain = "redwood", "redwoodinference.com"

    # `commented_on` names Nadia only inside a comment (display_email — finds her only if she is
    # already known); `authored` is where resolve() actually registers her.
    commented_on = (
        "linear",
        "d_commented",
        {
            **LINEAR_RAW,
            "key": "ENG-1",
            "creator": "Amaya Chen",
            "comments": ["2025-02-20 Nadia Weber: ran the baseline traces."],
        },
    )
    authored = (
        "linear",
        "d_authored",
        {**LINEAR_RAW, "key": "ENG-2", "creator": "Nadia Weber", "comments": []},
    )

    def convert(order):
        P = Principals([], "redwoodinference.com")
        erb._precompute_globals(order)
        erb._populate_principals(order, P, settings)
        return sorted(
            json.dumps(rec, sort_keys=True)
            for src, dsid, raw in order
            for rec in erb.to_byo(src, dsid, raw, P, settings.org_name)
        )

    forward = convert([commented_on, authored])
    assert forward == convert([authored, commented_on])
    # ...and the order-independent answer is the resolved one, not the unresolved one: the comment
    # is attributed and its body no longer carries the name as a prefix.
    comment = [c for rec in map(json.loads, forward) for c in rec.get("comments", [])][0]
    assert comment["author_email"] == "nadia.weber@redwoodinference.com"
    assert comment["content"] == "ran the baseline traces."


def test_every_supported_source_has_a_byo_converter_and_a_round_trip_fixture():
    """The converter fails SOFT — `export_byo` logs and skips a doc it cannot convert — so a source
    added to `SUPPORTED` without a converter would silently drop every one of its documents from the
    artifact instead of erroring. Same for the fixture: the equivalence diff above only covers what
    the tree contains, so a source missing from RT_DOCS is a source the diff never checks."""
    assert set(erb._BYO_CONVERTERS) == set(erb.SUPPORTED)
    assert set(RT_DOCS) == set(erb.SUPPORTED)


def test_export_byo_converts_every_document_it_was_given(tmp_path):
    """The counts `export_byo` returns must account for every record, per source — the guard on the
    soft failure above actually firing."""
    from backlot.config import Settings

    gen = _write_generated_data(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    settings = Settings(data_dir=data)
    shutil.copy(gen / "employee_directory.yaml", settings.employee_yaml)
    out = tmp_path / "artifact"
    counts = erb.export_byo(settings, gen, out)
    assert counts == {src: len(docs) for src, docs in RT_DOCS.items()}
    # and the artifact holds at least one record per document (hubspot notes add more)
    assert sum(1 for line in (out / "corpus.jsonl").read_text().split("\n") if line.strip()) >= sum(
        counts.values()
    )


def test_grants_for_fallback_is_a_fallback_not_a_conjunction():
    """`add("group", group) or add("org", org)` read as "group else org" and behaved as "both":
    `add` returns None, so the right-hand side always ran."""
    g = grants_for("gmail", {"org": "acme", "group": "eng", "owner": None, "people": []})
    assert ("group", "eng") not in g and ("org", "acme") not in g
    # the live fallback — a Drive file whose container has no group is org-visible, not invisible
    assert grants_for(
        "google_drive", {"org": "acme", "group": None, "owner": None, "people": []}
    ) == [("org", "acme")]
    # ...and one that HAS a group gets exactly that, never the org too
    assert grants_for(
        "google_drive", {"org": "acme", "group": "eng", "owner": None, "people": []}
    ) == [("group", "eng")]


def test_a_gmail_thread_with_no_participants_is_granted_to_nobody():
    """Gmail's model is "private to the participants" — so a thread that resolved none of them has
    nobody to grant to, and an org fallback would publish a private thread to the whole company.
    3 of the bench's ~121k threads land here, and the org grant was their ONLY grant."""
    assert grants_for("gmail", {"org": "acme", "group": None, "owner": None, "people": []}) == []
    # a thread that DOES name someone still grants to them
    assert grants_for(
        "gmail", {"org": "acme", "group": None, "owner": "ava@acme.com", "people": ["bob@acme.com"]}
    ) == [("user", "ava@acme.com"), ("user", "bob@acme.com")]
    # and no other source loses its scope
    for src in ("slack", "hubspot", "fireflies"):
        assert ("org", "acme") in grants_for(
            src, {"org": "acme", "group": "c", "owner": None, "people": []}
        ), src


def test_export_byo_shards_are_verifiable_and_reproducible(tmp_path):
    """Sharded output has to be checkable from the manifest alone and byte-identical across runs —
    a dataset consumer verifies a download without the corpus it came from, and gzip's default
    header would put the current time in every shard."""
    import gzip as _gzip
    import json as _json
    from backlot.config import Settings

    gen = _write_generated_data(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    settings = Settings(data_dir=data)
    shutil.copy(gen / "employee_directory.yaml", settings.employee_yaml)

    out = tmp_path / "sharded"
    counts = erb.export_byo(settings, gen, out, shard_records=2)
    manifest = _json.loads((out / "manifest.json").read_text())

    # every shard the manifest names exists, and its recorded size and digest match the file
    seen = 0
    for src, info in manifest["sources"].items():
        assert info["documents"] == counts[src]
        for shard in info["shards"]:
            p = out / shard["path"]
            assert p.exists() and p.stat().st_size == shard["bytes"]
            assert erb._sha256(p) == shard["sha256"]
            lines = [x for x in _gzip.open(p, "rt").read().split("\n") if x.strip()]
            assert len(lines) == shard["records"] <= 2
            assert all(_json.loads(x)["source_type"] == src for x in lines)
            seen += len(lines)
    assert seen == manifest["records"] > 0
    assert manifest["roster"]["sha256"] == erb._sha256(out / "roster.yaml")
    assert not (out / "corpus.jsonl").exists()  # sharded mode writes no single file

    # a second conversion of the same input reproduces the same digests
    again = erb.export_byo(settings, gen, tmp_path / "sharded2", shard_records=2)
    assert again == counts
    m2 = _json.loads((tmp_path / "sharded2" / "manifest.json").read_text())
    assert [s["sha256"] for i in m2["sources"].values() for s in i["shards"]] == [
        s["sha256"] for i in manifest["sources"].values() for s in i["shards"]
    ]


def test_select_records_drops_a_document_with_no_content(tmp_path):
    """The bench ships a slack thread whose `messages` is "". A direct import accepted it and the
    converted artifact then failed its own BYO schema (`content: '' should be non-empty`), so both
    paths have to make the same call — which they do only if the record source drops it."""
    gen = _write_generated_data(tmp_path)
    empty = gen / "sources" / "slack" / "general" / "empty-thread.json"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text(
        json.dumps(
            {
                "channel": "general",
                "messages": "",
                "participants": ["Ava Chen"],
                "title_field_name": "channel",
                "content_field_names": ["messages"],
                "dataset_doc_uuid": "dsid_empty_thread",
            }
        )
    )
    ids = {dsid for _src, dsid, _raw in erb.select_records(gen)}
    assert "dsid_empty_thread" not in ids
    # and a document that does carry content is still yielded from the same directory
    assert any(dsid.startswith("dsid_sl") for dsid in ids)


def _with_empty_thread(tmp_path, dsid="dsid_empty_thread"):
    """A generated_data tree plus one slack thread whose `messages` is "", as the bench ships."""
    gen = _write_generated_data(tmp_path)
    empty = gen / "sources" / "slack" / "general" / "empty-thread.json"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text(
        json.dumps(
            {
                "channel": "general",
                "messages": "",
                "participants": ["Ava Chen"],
                "title_field_name": "channel",
                "content_field_names": ["messages"],
                "dataset_doc_uuid": dsid,
            }
        )
    )
    return gen


def _settings_for(tmp_path, gen):
    from backlot.config import Settings

    data = tmp_path / "data"
    data.mkdir()
    settings = Settings(data_dir=data)
    shutil.copy(gen / "employee_directory.yaml", settings.employee_yaml)
    return settings


def test_an_undeclared_empty_document_stops_the_export(tmp_path, capsys):
    """`generated_data` has had one commit ever, so the same bench has to yield the same exclusions.
    One the code does not declare means the input changed, and the run stops naming it rather than
    dropping a document behind a line on stderr."""
    gen = _with_empty_thread(tmp_path)
    settings = _settings_for(tmp_path, gen)
    with pytest.raises(SystemExit):
        erb.export_byo(settings, gen, tmp_path / "out", shard_records=2)
    err = capsys.readouterr().err
    assert "general/empty-thread.json" in err and "dsid_empty_thread" in err
    assert "--allow-excluded 1" in err  # and says how to proceed once someone has looked


def test_a_declared_exclusion_is_recorded_by_identity_and_the_layer_adds_up(tmp_path, monkeypatch):
    """A count cannot be resolved back to a document without rescanning the raw bench, so the
    manifest names what went — and states the total it came out of, because a consumer holding a
    short count should not have to leave the artifact to learn whether anything is missing."""
    gen = _with_empty_thread(tmp_path)
    monkeypatch.setattr(erb, "KNOWN_EMPTY_DOCS", {"dsid_empty_thread"})
    settings = _settings_for(tmp_path, gen)
    out = tmp_path / "out"
    erb.export_byo(settings, gen, out, shard_records=2)

    layer = json.loads((out / "manifest.json").read_text())["layers"]["converted"]
    assert layer["excluded"] == [
        {
            "source": "slack",
            "doc_id": "dsid_empty_thread",
            "path": "general/empty-thread.json",
            "reason": "content empty after strip",
        }
    ]
    assert layer["source_documents"] == (
        layer["documents"] + len(layer["excluded"]) + len(layer["failed"])
    )


def test_the_snapshot_the_data_came_from_reaches_the_manifest(tmp_path):
    """Neither ref nor tag pins this data — `main` moved past the commit that added generated_data
    and the one tag predates it — so the artifact carries the tarball digest instead."""
    gen = _with_empty_thread(tmp_path)
    monkey = {
        "repo": "onyx-dot-app/EnterpriseRAG-Bench",
        "ref": "main",
        "tarball_sha256": "0" * 64,
        "tarball_bytes": 1000142917,
    }
    assert erb.read_snapshot(gen) is None  # a hand-assembled tree records nothing
    (gen / erb.SNAPSHOT_FILE).write_text(json.dumps(monkey))
    assert erb.read_snapshot(gen) == monkey

    settings = _settings_for(tmp_path, gen)
    out = tmp_path / "out"
    erb.export_byo(settings, gen, out, shard_records=2, allow_excluded=1)
    assert (
        json.loads((out / "manifest.json").read_text())["layers"]["converted"]["snapshot"] == monkey
    )


def test_round_trip_survives_two_documents_sharing_a_doc_id(tmp_path):
    """Four bench documents share a `dataset_doc_uuid` with another: three across sources (a drive
    file that is also a confluence page, plus a hubspot and a jira one) and two jira issues sharing
    one. Within a source the row is overwritten, so both importers must keep the LAST record; across
    sources each has its own table, so both survive and BOTH containers' ACL groups must be granted.
    Resolving either of those differently is enough to make the full-corpus round-trip diverge."""
    gen = _write_generated_data(tmp_path)
    jira_first = json.loads(sorted((gen / "sources" / "jira").glob("*.json"))[0].read_text())
    dsid = jira_first["dataset_doc_uuid"]
    # `zz-` so it sorts last: iter_records walks the source in path order. The title lives in
    # whichever field `title_field_name` names, so that is the one to change.
    (gen / "sources" / "jira" / "zz-repeat.json").write_text(
        json.dumps(
            {
                **jira_first,
                jira_first["title_field_name"]: "Repeated id, later record",
                "status": "Resolved",
            }
        )
    )
    conf_first = json.loads(sorted((gen / "sources" / "confluence").glob("*.json"))[0].read_text())
    (gen / "sources" / "confluence" / "zz-shared.json").write_text(
        json.dumps(
            {
                **conf_first,
                "dataset_doc_uuid": dsid,
                conf_first["title_field_name"]: "Same id, under confluence",
            }
        )
    )

    direct = _import_erb_directly(gen, tmp_path / "direct")
    viabyo = _import_via_byo(gen, tmp_path / "viabyo", tmp_path / "artifact")

    for label, settings in (("direct", direct), ("via byo", viabyo)):
        conn = sqlite3.connect(settings.db_path)
        assert conn.execute("SELECT title FROM jira_issues WHERE doc_id=?", (dsid,)).fetchone() == (
            "Repeated id, later record",
        ), f"{label} kept the earlier of the two jira records"
        assert conn.execute(
            "SELECT title FROM confluence_pages WHERE doc_id=?", (dsid,)
        ).fetchone() == ("Same id, under confluence",), (
            f"{label} lost the confluence document to the jira one that shares its id"
        )
        conn.close()

    a, b = _dump_db(direct.db_path), _dump_db(viabyo.db_path)
    for t in sorted(a):
        assert a[t] == b[t], (
            f"table {t} differs\n  only in direct: {[r for r in a[t] if r not in b[t]]}\n"
            f"  only via byo:  {[r for r in b[t] if r not in a[t]]}"
        )

    # Same pinned check as test_erb_to_byo_round_trip_builds_an_equivalent_database, adjusted for
    # the 2 extra bench FILES this test adds on top of RT_DOCS's 15 (zz-repeat.json, zz-shared.json
    # — both duplicate an existing doc_id, but `select_records` counts by file, not by doc_id, so
    # both still add to the offered total; neither is excluded and neither is hubspot, so they add
    # 1 each to both sides):
    #   direct:  source_documents = 17 documents + 0 excluded = 17
    #   via byo: source_documents = 17 - 1 (hubspot company) + 3 (company + 2 notes) = 19
    conn_direct, conn_viabyo = sqlite3.connect(direct.db_path), sqlite3.connect(viabyo.db_path)
    assert store.read_meta(conn_direct, "source_documents") == "17"
    assert store.read_meta(conn_viabyo, "source_documents") == "19"
    conn_direct.close()
    conn_viabyo.close()
