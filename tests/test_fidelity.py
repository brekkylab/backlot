"""API-shape fidelity: assert the new BYO fields + correctness fixes surface in the
vendor response builders exactly as the real APIs shape them.

Each test loads a tiny corpus that exercises one service's new fields, then calls the
router's object builders directly (they take a row/conn, no live socket needed)."""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from xml.etree import ElementTree as ET

from starlette.requests import Request

from app import store
from app.config import Settings
from tests.test_endpoints import _sign_get


def _epoch(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _load(tmp_path, records) -> Settings:
    from app.importer.byo import load
    p = tmp_path / "corpus.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records))
    settings = Settings(data_dir=tmp_path)
    load(p, settings)
    return settings


def _req():
    return Request({"type": "http", "headers": [], "query_string": b"", "scheme": "http",
                    "server": ("mock", 80), "path": "/"})


# --- GitHub ---------------------------------------------------------------------

def test_github_issue_shape(tmp_path):
    from app.routers.github import _issue_obj, _pr_obj
    s = _load(tmp_path, [
        {"source_type": "github", "doc_id": "gh1", "repo": "gw", "title": "Bug", "content": "x",
         "author_email": "a@x.com", "state": "closed", "closed_at": "2026-02-01T00:00:00Z",
         "closed_by": "b@x.com", "assignees": ["a@x.com"], "milestone": "v2",
         "reactions": {"+1": 3, "heart": 1},
         "comments": [{"content": "c", "author_email": "b@x.com", "reactions": {"+1": 1}}]},
        {"source_type": "github", "doc_id": "pr1", "repo": "gw", "title": "PR", "content": "y",
         "author_email": "a@x.com", "subtype": "pull_request", "merged_at": "2026-02-02T00:00:00Z",
         "merged_by": "b@x.com", "requested_reviewers": ["c@x.com"]},
    ])
    conn = store.connect_ro(s.db_path)
    iss = _issue_obj(conn, "org", "gw", store.get_document(conn, "github", "gh1"), "http://m/github")
    # numeric id present and distinct from number (real connectors dedupe on id)
    assert iss["id"] != iss["number"] and isinstance(iss["id"], int)
    assert iss["node_id"]
    # assignee (singular) present alongside assignees[]
    assert iss["assignee"]["login"] == "a" and iss["assignees"][0]["login"] == "a"
    assert iss["closed_at"].startswith("2026-02-01") and iss["closed_by"]["login"] == "b"
    assert iss["milestone"]["title"] == "v2"
    assert iss["state_reason"] == "completed" and iss["author_association"] == "MEMBER"
    # reactions is the full 8-key rollup with total_count
    assert iss["reactions"]["total_count"] == 4 and iss["reactions"]["+1"] == 3
    assert iss["reactions"]["eyes"] == 0

    pr = _pr_obj(conn, "org", "gw", store.get_document(conn, "github", "pr1"), "http://m/github")
    assert pr["merged"] is True and pr["merged_by"]["login"] == "b"
    assert pr["requested_reviewers"][0]["login"] == "c"


def test_github_comment_reactions(tmp_path):
    from app.routers.github import _gh_comment
    s = _load(tmp_path, [
        {"source_type": "github", "doc_id": "gh2", "repo": "gw", "title": "T", "content": "x",
         "comments": [{"content": "hi", "author_email": "a@x.com", "reactions": {"heart": 2}}]},
    ])
    conn = store.connect_ro(s.db_path)
    c = store.doc_comments(conn, "github", "gh2")[0]
    obj = _gh_comment("org", "gw", 1, c, "http://m/github")
    assert obj["reactions"]["heart"] == 2 and obj["node_id"] and obj["url"]
    assert obj["reactions"]["total_count"] == 2


# --- Jira ------------------------------------------------------------------------

def test_jira_status_category_and_fields(tmp_path):
    from app.routers.atlassian import _jira_issue
    s = _load(tmp_path, [
        {"source_type": "jira", "doc_id": "j1", "project": "pay", "title": "T", "content": "c",
         "status": "In Progress", "assignee": "a@x.com", "reporter": "b@x.com",
         "resolution": "Done", "resolutiondate": "2026-03-01T00:00:00Z", "duedate": "2026-04-01",
         "fix_versions": ["1.2.0"]},
        {"source_type": "jira", "doc_id": "j2", "project": "pay", "title": "D", "content": "c",
         "status": "Done"},
    ])
    conn = store.connect_ro(s.db_path)
    f = _jira_issue(conn, _req(), store.get_document(conn, "jira", "j1"))["fields"]
    # the real 3-category model: "In Progress" -> indeterminate (not the old hardcoded "new")
    assert f["status"]["statusCategory"]["key"] == "indeterminate"
    assert f["assignee"]["emailAddress"] == "a@x.com"
    assert f["reporter"]["emailAddress"] == "b@x.com"
    assert f["resolution"]["name"] == "Done" and f["resolutiondate"].startswith("2026-03-01")
    assert f["duedate"] == "2026-04-01" and f["fixVersions"][0]["name"] == "1.2.0"
    # richer actor object
    assert "avatarUrls" in f["assignee"] and f["assignee"]["accountType"] == "atlassian"
    # scaffolds present so probing clients get [] / null, not KeyError
    assert f["attachment"] == [] and f["votes"]["votes"] == 0

    done = _jira_issue(conn, _req(), store.get_document(conn, "jira", "j2"))["fields"]
    assert done["status"]["statusCategory"]["key"] == "done"
    assert done["assignee"] is None  # unassigned by default


# --- Confluence ------------------------------------------------------------------

def test_confluence_body_and_version(tmp_path):
    from app.routers.atlassian import _confluence_page
    s = _load(tmp_path, [
        {"source_type": "confluence", "doc_id": "c1", "space": "hb", "title": "P",
         "content": "para one\n\npara two", "author_email": "a@x.com",
         "created": "2026-01-01T00:00:00Z", "updated": "2026-02-01T00:00:00Z",
         "version_message": "edited", "minor_edit": True, "labels": ["eng"]},
    ])
    conn = store.connect_ro(s.db_path)
    row = store.get_document(conn, "confluence", "c1")
    page = _confluence_page(
        conn, _req(), row,
        "body.storage,body.view,body.export_view,version,metadata.labels,history")
    # storage (XHTML source) and view (rendered) must differ
    assert page["body"]["storage"]["value"] != page["body"]["view"]["value"]
    # export_view (rendered, used by llama-index's ConfluenceReader) carries the same content
    # as view but without editor-only attributes (e.g. no `auto-cursor-target` class)
    assert page["body"]["export_view"]["representation"] == "export_view"
    assert "para one" in page["body"]["export_view"]["value"]
    assert "auto-cursor-target" not in page["body"]["export_view"]["value"]
    # version reflects the update + BYO message/minorEdit; history carries creation
    assert page["version"]["number"] == 2 and page["version"]["message"] == "edited"
    assert page["version"]["minorEdit"] is True
    assert page["history"]["createdDate"].startswith("2026-01-01")
    # labels reachable via expand=metadata.labels on the content object
    assert page["metadata"]["labels"]["results"][0]["name"] == "eng"


def test_confluence_restrictions_has_update(tmp_path):
    # restrictions/byOperation must return BOTH read and update operations
    import asyncio
    import types
    from app import synth
    from app.acl import Acl
    from app.routers.atlassian import confluence_restrictions
    s = _load(tmp_path, [
        {"source_type": "confluence", "doc_id": "c2", "space": "hb", "title": "P",
         "content": "x", "author_email": "a@x.com", "visibility": "private"},
    ])
    cid = synth.confluence_id("c2")
    app = types.SimpleNamespace(state=types.SimpleNamespace(
        conn=store.connect_ro(s.db_path),
        acl=Acl.load(s.tokens_path, s.admin_token, s.org_name),
        index={"confluence": {cid: "c2"}}))
    scope = {"type": "http", "scheme": "http", "server": ("m", 80), "path": "/",
             "query_string": b"", "app": app,
             "headers": [(b"authorization", f"Bearer {s.admin_token}".encode())]}
    result = asyncio.run(confluence_restrictions(cid, Request(scope)))
    assert "read" in result and "update" in result
    assert result["read"]["restrictions"]["user"]["results"]  # the private doc's author


# --- Drive -----------------------------------------------------------------------

def test_drive_permissions_and_trashed(tmp_path):
    from app.routers.google import _drive_permissions, _drive_q_match
    s = _load(tmp_path, [
        {"source_type": "google_drive", "doc_id": "d1", "folder": "mk", "title": "Deck",
         "content": "x", "author_email": "a@x.com", "visibility": "public"},
        {"source_type": "google_drive", "doc_id": "d2", "folder": "mk", "title": "Old",
         "content": "y", "author_email": "a@x.com", "visibility": "group", "group": "mkt",
         "trashed": True},
    ])
    conn = store.connect_ro(s.db_path)
    perms = _drive_permissions(conn, "d1")
    # public share is type "anyone" (not "domain"), and an owner permission exists
    assert any(p["type"] == "anyone" for p in perms)
    assert any(p["role"] == "owner" for p in perms)
    # group-restricted doc surfaces a group-type permission
    gperms = _drive_permissions(conn, "d2")
    assert any(p["type"] == "group" for p in gperms)
    # trashed excluded from a default `q`, included when asked
    d2 = store.get_document(conn, "google_drive", "d2")
    assert _drive_q_match(d2, "trashed = false") is False
    assert _drive_q_match(d2, "trashed = true") is True


def test_drive_size_is_populated_for_docs_editors_files(tmp_path):
    """Google: `size` "is populated for files with binary content stored in Google Drive AND for
    Docs Editors files; it is not populated for shortcuts or folders." The mock set it only in the
    binary branch, so it taught implementors that native Docs have no byte size (issue #23)."""
    from app.routers.google import _drive_file
    s = _load(tmp_path, [
        {"source_type": "google_drive", "doc_id": "n1", "folder": "mk", "title": "Doc",
         "content": "hello there", "author_email": "a@x.com", "subtype": "document"},
        {"source_type": "google_drive", "doc_id": "b1", "folder": "mk", "title": "Scan.pdf",
         "content": "%PDF-1.7", "author_email": "a@x.com", "subtype": "pdf",
         "meta": {"mime_type": "application/pdf"}},
    ])
    conn = store.connect_ro(s.db_path)
    native = _drive_file(conn, store.get_document(conn, "google_drive", "n1"))
    assert native["size"] == str(len("hello there"))
    # checksums and a download link stay binary-only, as they are on real Drive
    assert "md5Checksum" not in native and "webContentLink" not in native
    binary = _drive_file(conn, store.get_document(conn, "google_drive", "b1"))
    assert binary["size"] == str(len("%PDF-1.7")) and binary["md5Checksum"]


# --- Gmail -----------------------------------------------------------------------

def test_gmail_raw_and_headers(tmp_path):
    from app.routers.google import _gmail_message
    s = _load(tmp_path, [
        {"source_type": "gmail", "doc_id": "m1", "mailbox": "ceo", "title": "Hi",
         "content": "body text", "author_email": "ceo@x.com", "bcc": "secret@x.com"},
    ])
    conn = store.connect_ro(s.db_path)
    row = store.get_document(conn, "gmail", "m1")
    # raw format returns the base64url RFC822 message, no parsed payload
    raw = _gmail_message(row, "raw")
    assert "raw" in raw and "payload" not in raw
    import base64
    decoded = base64.urlsafe_b64decode(raw["raw"]).decode()
    assert "Subject: Hi" in decoded and "MIME-Version: 1.0" in decoded
    # Bcc must NOT appear in a fetched message's headers (stripped in transit)
    full = _gmail_message(row, "full")
    names = {h["name"] for h in full["payload"]["headers"]}
    assert "Bcc" not in names and "MIME-Version" in names

    # The declared Content-Type (multipart/alternative here, no attachments) must be backed by a
    # genuinely boundary-delimited body -- not just plain text under a multipart header (invalid
    # MIME real Gmail never produces). Round-trip through Python's own `email` parser: a well-
    # formed message parses with no defects, `is_multipart()` True, and yields the plain-text
    # body back out, matching what a real reader (e.g. llama-index's GmailReader) needs.
    import email
    mime_msg = email.message_from_bytes(base64.urlsafe_b64decode(raw["raw"]))
    assert not mime_msg.defects, f"raw Gmail message is not valid MIME: {mime_msg.defects}"
    assert mime_msg.is_multipart()
    plain_parts = [p for p in mime_msg.get_payload() if p.get_content_type() == "text/plain"]
    assert plain_parts and plain_parts[0].get_payload(decode=True).decode() == "body text"


def test_gmail_raw_with_attachment_is_valid_mime(tmp_path):
    from app.routers.google import _gmail_message
    s = _load(tmp_path, [
        {"source_type": "gmail", "doc_id": "m2", "mailbox": "ceo", "title": "With attachment",
         "content": "see attached", "author_email": "ceo@x.com",
         "attachments": [{"filename": "notes.txt", "mime": "text/plain", "content": "hello"}]},
    ])
    conn = store.connect_ro(s.db_path)
    row = store.get_document(conn, "gmail", "m2")
    raw = _gmail_message(row, "raw")
    import base64, email
    decoded_bytes = base64.urlsafe_b64decode(raw["raw"])
    assert b"Content-Type: multipart/mixed" in decoded_bytes  # top_mime switches with attachments
    mime_msg = email.message_from_bytes(decoded_bytes)
    assert not mime_msg.defects, f"raw Gmail message is not valid MIME: {mime_msg.defects}"
    assert mime_msg.is_multipart()
    filenames = {p.get_filename() for p in mime_msg.get_payload() if p.get_filename()}
    assert "notes.txt" in filenames


# --- Slack -----------------------------------------------------------------------

def test_slack_reply_users_and_num_members(tmp_path):
    from app.routers.slack import _message, _full_channel
    from app import synth
    s = _load(tmp_path, [
        {"source_type": "slack", "doc_id": "s1", "channel": "inc", "content": "root",
         "author_email": "bob@x.com", "visibility": "public",
         "replies": [{"content": "a", "author_email": "ava@x.com"},
                     {"content": "b", "author_email": "cid@x.com"},
                     {"content": "c", "author_email": "ava@x.com"}]},
    ])
    conn = store.connect_ro(s.db_path)
    thread = store.slack_thread(conn, "s1")
    root, first_reply = thread[0], thread[1]
    ru = store.slack_reply_authors(conn, "s1")
    ruids = [synth.slack_user_id(e) for e in ru]
    rootmsg = _message(root, reply_count=3, reply_users=ruids, reply_users_count=len(ru))
    # 3 replies but only 2 distinct repliers -> counts differ (real Slack distinguishes them)
    assert rootmsg["reply_count"] == 3 and rootmsg["reply_users_count"] == 2
    assert len(rootmsg["reply_users"]) == 2
    # a reply carries parent_user_id pointing at the root author
    rep = _message(first_reply, parent_user_id=synth.slack_user_id("bob@x.com"))
    assert rep["parent_user_id"] == synth.slack_user_id("bob@x.com")
    # conversations.list channel object reports a real member count (was hardcoded 0)
    import types
    req = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace()))
    ch = _full_channel(req, conn, "inc")
    assert ch["num_members"] > 0 and ch["creator"] == "USERVICE0"


# --- Notion ---------------------------------------------------------------------

def _notion_conn(tmp_path):
    s = _load(tmp_path, [
        {"source_type": "notion", "doc_id": "nf-page", "teamspace": "eng", "title": "Runbook",
         "content": "# On-call\n\nRoll back and page.", "author_email": "ava@acme.com",
         "visibility": "public", "icon": "📟",
         "comments": [{"content": "add rate-limiter step", "author_email": "bob@acme.com"}]},
        {"source_type": "notion", "doc_id": "nf-db", "subtype": "database", "teamspace": "eng",
         "title": "Tasks", "content": "Tracker", "author_email": "ava@acme.com",
         "visibility": "public", "properties": {"Status": {"type": "select"}}},
        {"source_type": "notion", "doc_id": "nf-row", "parent": "nf-db", "teamspace": "eng",
         "title": "Fix bug", "content": "body", "author_email": "bob@acme.com",
         "visibility": "public", "properties": {"Status": "In Progress"}},
    ])
    return store.connect_ro(s.db_path)


def test_notion_page_shape(tmp_path):
    from app import synth
    from app.routers.notion import _page_obj
    conn = _notion_conn(tmp_path)
    obj = _page_obj(conn, store.get_document(conn, "notion", "nf-page"))
    assert obj["object"] == "page"
    assert obj["id"] == synth.notion_id("nf-page")
    assert obj["created_by"]["object"] == "user"
    assert obj["parent"] == {"type": "workspace", "workspace": True}
    assert obj["properties"]["title"]["type"] == "title"
    assert obj["properties"]["title"]["title"][0]["plain_text"] == "Runbook"
    assert obj["icon"] == {"type": "emoji", "emoji": "📟"}
    assert obj["url"].startswith("https://www.notion.so/")
    # a database row exposes its property values + a database_id parent
    row = _page_obj(conn, store.get_document(conn, "notion", "nf-row"))
    assert row["parent"]["type"] == "database_id"
    assert row["properties"]["Status"]["select"]["name"] == "In Progress"


def test_notion_database_and_data_source_shape(tmp_path):
    from app import synth
    from app.routers.notion import _data_source_obj, _database_obj
    conn = _notion_conn(tmp_path)
    dbrow = store.get_document(conn, "notion", "nf-db")
    new = _database_obj(conn, dbrow, "2025-09-03")
    assert new["object"] == "database"
    assert new["data_sources"][0]["id"] == synth.notion_data_source_id("nf-db")
    assert "properties" not in new
    legacy = _database_obj(conn, dbrow, "2022-06-28")
    assert "data_sources" not in legacy
    assert legacy["properties"]["Status"]["type"] == "select"
    ds = _data_source_obj(conn, dbrow)
    assert ds["object"] == "data_source" and ds["properties"]["title"]["type"] == "title"


def test_notion_user_and_block_shape(tmp_path):
    from app import synth
    from app.routers.notion import _user_obj
    conn = _notion_conn(tmp_path)
    u = _user_obj(conn, "ava@acme.com")
    assert u["object"] == "user" and u["type"] == "person"
    assert u["person"]["email"] == "ava@acme.com"
    assert u["id"] == synth.notion_user_id("ava@acme.com")
    blocks = synth.notion_blocks("nf-page", "# On-call\n\nRoll back and page.")
    b = blocks[0]
    assert b["object"] == "block" and b["type"] == "heading_1"
    assert b["heading_1"]["rich_text"][0]["plain_text"] == "On-call"


# --- HubSpot ---------------------------------------------------------------------

def _hubspot_conn(tmp_path):
    s = _load(tmp_path, [
        {"source_type": "hubspot", "doc_id": "hf-co", "object_type": "companies",
         "title": "Acme Health", "content": "Mid-market provider.", "author_email": "rep@acme.com",
         "visibility": "public", "created": "2026-01-05T00:00:00Z",
         "updated": "2026-03-10T00:00:00Z",
         "properties": {"name": "Acme Health", "domain": "acme-health.com"}},
        {"source_type": "hubspot", "doc_id": "hf-ct", "object_type": "contacts", "title": "Ava",
         "content": "VP Platform.", "author_email": "rep@acme.com", "visibility": "public",
         "properties": {"firstname": "Ava"},
         "associations": [{"to": "hf-co", "label": "Primary"}]},
        {"source_type": "hubspot", "doc_id": "hf-arch", "object_type": "companies",
         "title": "Defunct", "content": "Churned.", "author_email": "rep@acme.com",
         "visibility": "public", "archived": True, "properties": {"name": "Defunct"}},
    ])
    return store.connect_ro(s.db_path)


def test_hubspot_record_shape(tmp_path):
    from app import synth
    from app.routers.hubspot import _record
    conn = _hubspot_conn(tmp_path)
    obj = _record(store.get_document(conn, "hubspot", "hf-co"))
    # a CRM record is {id, properties, createdAt, updatedAt, archived} — ids are numeric strings and
    # the timestamps are ISO 8601 with milliseconds, as the vendor emits them
    assert obj["id"] == synth.hubspot_record_id("hf-co")
    assert obj["id"].isdigit()
    assert obj["properties"]["domain"] == "acme-health.com"
    assert obj["createdAt"] == "2026-01-05T00:00:00.000Z"
    assert obj["updatedAt"] == "2026-03-10T00:00:00.000Z"
    assert obj["archived"] is False
    assert _record(store.get_document(conn, "hubspot", "hf-arch"))["archived"] is True


def test_hubspot_properties_projection(tmp_path):
    from app.routers.hubspot import _record
    conn = _hubspot_conn(tmp_path)
    row = store.get_document(conn, "hubspot", "hf-co")
    assert set(_record(row, ["name"])["properties"]) == {"name"}
    assert set(_record(row)["properties"]) == {"name", "domain"}     # no projection -> all


def test_hubspot_association_shape(tmp_path):
    from app import synth
    conn = _hubspot_conn(tmp_path)
    rows = store.hubspot_associations(conn, "hf-ct", "companies")
    assert [r["to_doc_id"] for r in rows] == ["hf-co"]
    # the v4 payload is {toObjectId, associationTypes:[{category, typeId, label}]}
    assert synth.hubspot_record_id(rows[0]["to_doc_id"]).isdigit()
    assert rows[0]["assoc_category"] == "HUBSPOT_DEFINED"
    assert rows[0]["label"] == "Primary"
    # the reverse direction exists and carries its own type id, as real HubSpot does
    back = store.hubspot_associations(conn, "hf-co", "contacts")
    assert [r["to_doc_id"] for r in back] == ["hf-ct"]
    assert back[0]["assoc_type_id"] != rows[0]["assoc_type_id"]


def test_hubspot_page_omits_paging_next_on_last_page(tmp_path):
    """The termination contract at the builder level: `paging.next` appears only when a further page
    exists, because the official client's fetch_all stops on its absence."""
    from app.routers.hubspot import _page
    conn = _hubspot_conn(tmp_path)
    rows = store.list_hubspot_objects(conn, "companies", limit=3)   # 1 non-archived company
    assert "paging" not in _page(rows, 10, None)
    assert _page(rows, 1, None)["results"]                          # a full page still yields rows


# --- S3 --------------------------------------------------------------------------

NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _get_xml(base_url, path, token):
    url, headers = _sign_get(base_url, path, token)
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
        return ET.fromstring(r.read())


def test_list_buckets_xml_shape(live_server):
    base_url, settings = live_server
    root = _get_xml(base_url, "/s3/", settings.admin_token)
    assert root.tag == f"{NS}ListAllMyBucketsResult"
    assert root.find(f"{NS}Owner/{NS}ID") is not None
    names = {b.findtext(f"{NS}Name") for b in root.iter(f"{NS}Bucket")}
    assert "eng-artifacts" in names


def test_list_objects_v2_xml_shape(live_server):
    base_url, settings = live_server
    root = _get_xml(base_url, "/s3/eng-artifacts?list-type=2", settings.admin_token)
    assert root.tag == f"{NS}ListBucketResult"
    assert root.findtext(f"{NS}Name") == "eng-artifacts"
    assert root.findtext(f"{NS}IsTruncated") in ("true", "false")
    c = next(root.iter(f"{NS}Contents"))
    assert c.findtext(f"{NS}Key") and c.findtext(f"{NS}ETag").startswith('"')
    assert c.findtext(f"{NS}LastModified").endswith("Z")


def test_list_objects_v2_delimiter_common_prefixes(live_server):
    base_url, settings = live_server
    root = _get_xml(base_url, "/s3/eng-artifacts?list-type=2&delimiter=/", settings.admin_token)
    prefixes = {cp.findtext(f"{NS}Prefix") for cp in root.iter(f"{NS}CommonPrefixes")}
    assert {"runbooks/", "design/"} <= prefixes


# --- Linear -----------------------------------------------------------------------
# Linear's auth is the one shape no other source in this repo uses: the personal API key is the
# BARE `Authorization` value with no scheme, while an OAuth access token is `Bearer <token>`, and
# the real API accepts both on the same header. Getting this wrong is silent — a stripped-scheme
# parse would accept `Bearer <key>` and reject the bare key that every real Linear client sends.

LINEAR_CORPUS = [
    {"source_type": "linear", "doc_id": "lin-a", "team": "engineering", "group": "engineering",
     "title": "Batching stall", "content": "A 50ms stall after compaction.",
     "author_email": "ava@acme.com", "author_groups": ["engineering"], "visibility": "public",
     "identifier": "ENG-9", "state": "In Progress", "priority": 2},
]


def _linear_client(tmp_path):
    import os

    from starlette.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    settings = _load(tmp_path, LINEAR_CORPUS)
    prev = os.environ.get("MOCK_DATA_DIR")
    os.environ["MOCK_DATA_DIR"] = str(settings.data_dir)
    get_settings.cache_clear()
    client = TestClient(app)
    client.__enter__()

    def close():
        client.__exit__(None, None, None)
        get_settings.cache_clear()
        if prev is None:
            os.environ.pop("MOCK_DATA_DIR", None)
        else:
            os.environ["MOCK_DATA_DIR"] = prev

    return client, settings, close


def _linear_identifiers(client, authorization):
    r = client.post("/linear/graphql", json={"query": "{ issues { nodes { identifier } } }"},
                    headers={"Authorization": authorization})
    return r.status_code, r.json()


def test_linear_accepts_a_bare_api_key_with_no_scheme(tmp_path):
    """What `LinearReader` and `@linear/sdk` both send: `Authorization: <key>`, no prefix."""
    client, settings, close = _linear_client(tmp_path)
    try:
        status, body = _linear_identifiers(client, settings.admin_token)
        assert status == 200
        assert [n["identifier"] for n in body["data"]["issues"]["nodes"]] == ["ENG-9"]
    finally:
        close()


def test_linear_accepts_a_bearer_oauth_token(tmp_path):
    """The OAuth shape, on the same header."""
    client, settings, close = _linear_client(tmp_path)
    try:
        status, body = _linear_identifiers(client, f"Bearer {settings.admin_token}")
        assert status == 200
        assert [n["identifier"] for n in body["data"]["issues"]["nodes"]] == ["ENG-9"]
    finally:
        close()


def test_linear_rejects_a_stray_scheme_rather_than_stripping_it(tmp_path):
    """To the real API the WHOLE header value is the key, so `Token <key>` is simply a wrong key —
    not a key with a scheme to discard. Stripping the first word would authenticate a credential
    the real API refuses."""
    client, settings, close = _linear_client(tmp_path)
    try:
        assert _linear_identifiers(client, f"Token {settings.admin_token}")[0] == 401
    finally:
        close()


def test_linear_field_error_is_a_200_and_a_syntax_error_is_a_400(tmp_path):
    """Real Linear splits these: a bad document never executed is a 400 with no `data` key, while
    an error raised mid-execution is a 200 carrying `data` alongside `errors`."""
    client, settings, close = _linear_client(tmp_path)
    try:
        h = {"Authorization": settings.admin_token}
        bad = client.post("/linear/graphql", json={"query": "{ issues( }"}, headers=h)
        assert bad.status_code == 400 and "data" not in bad.json()

        missing = client.post("/linear/graphql",
                              json={"query": '{ issue(id: "NOPE-1") { identifier } }'}, headers=h)
        assert missing.status_code == 200
        assert "data" in missing.json() and missing.json()["errors"]
    finally:
        close()


# --- fireflies -------------------------------------------------------------------
# Fireflies' shape differs from every other GraphQL source here in two ways that clients depend
# on: snake_case field names, and offset pagination returning a BARE LIST rather than a Relay
# connection. Both are pinned, because "it's GraphQL" is exactly the assumption that would
# otherwise make someone wrap this in `{ nodes, pageInfo }`.

FIREFLIES_CORPUS = [
    {"source_type": "fireflies", "doc_id": "ff-f1", "channel": "sales-calls",
     "title": "Fidelity discovery call", "host_email": "ava@acme.com", "host_name": "Ava Chen",
     "organizer_email": "ops@acme.com", "duration": 45.0, "calendar_id": "cal-fid",
     "created": "2026-04-02T15:00:00Z", "visibility": "public",
     "summary": {"overview": "Overview text.", "topics_discussed": ["latency"],
                 "action_items": ["Ava: follow up", "Bob: benchmark"],
                 "keywords": ["latency"], "meeting_type": "discovery"},
     "meeting_attendees": [{"displayName": "Ava Chen", "email": "ava@acme.com",
                            "location": None}],
     "sentences": [
         {"speaker_name": "Ava Chen", "author_email": "ava@acme.com", "start_time": 0,
          "text": "Kicking off."},
         {"speaker_name": "Dana Ruiz", "start_time": 20, "text": "Sounds good."}]},
]


def _fireflies_client(tmp_path):
    import os

    from starlette.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    settings = _load(tmp_path, FIREFLIES_CORPUS)
    prev = os.environ.get("MOCK_DATA_DIR")
    os.environ["MOCK_DATA_DIR"] = str(settings.data_dir)
    get_settings.cache_clear()
    client = TestClient(app)
    client.__enter__()

    def close():
        client.__exit__(None, None, None)
        get_settings.cache_clear()
        if prev is None:
            os.environ.pop("MOCK_DATA_DIR", None)
        else:
            os.environ["MOCK_DATA_DIR"] = prev

    return client, settings, close


def _ff(client, settings, query, **variables):
    body = {"query": query}
    if variables:
        body["variables"] = variables
    return client.post("/fireflies/graphql", json=body,
                       headers={"Authorization": f"Bearer {settings.admin_token}"}).json()


def test_fireflies_accepts_the_vendors_documented_raw_http_post(tmp_path):
    """There is no Fireflies SDK: the vendor's quickstart is curl / requests.post / axios.post /
    Java HttpClient against one endpoint with a Bearer key. That IS the client story, so the exact
    shape those examples send has to work."""
    client, settings, close = _fireflies_client(tmp_path)
    try:
        r = client.post(
            "/fireflies/graphql",
            json={"query": "query Transcripts($limit: Int) "
                           "{ transcripts(limit: $limit) { id title } }",
                  "variables": {"limit": 10}},
            headers={"Authorization": f"Bearer {settings.admin_token}",
                     "Content-Type": "application/json"})
        assert r.status_code == 200
        assert r.json()["data"]["transcripts"][0]["title"] == "Fidelity discovery call"
    finally:
        close()


def test_fireflies_transcripts_is_a_bare_list_not_a_relay_connection(tmp_path):
    """Fireflies pages with limit/skip. Wrapping it in `{ nodes, pageInfo }` — the shape every
    other GraphQL source here uses — would break every generated client."""
    client, settings, close = _fireflies_client(tmp_path)
    try:
        got = _ff(client, settings, "{ transcripts(limit: 5) { id } }")
        assert isinstance(got["data"]["transcripts"], list)
        # asking for a connection's fields must be a validation error, i.e. they do not exist
        bad = _ff(client, settings, "{ transcripts { nodes { id } pageInfo { hasNextPage } } }")
        assert "data" not in bad and bad["errors"]
    finally:
        close()


def test_fireflies_field_names_are_snake_case(tmp_path):
    """Fireflies' own convention, not a translation — a camelCase spelling must NOT resolve."""
    client, settings, close = _fireflies_client(tmp_path)
    try:
        ok = _ff(client, settings, "{ transcripts(limit: 1) { host_email organizer_email "
                                   "audio_url video_url transcript_url meeting_link "
                                   "calendar_type meeting_attendees { displayName } "
                                   "sentences { speaker_name speaker_id start_time end_time } } }")
        assert "errors" not in ok
        t = ok["data"]["transcripts"][0]
        assert t["host_email"] == "ava@acme.com"
        assert t["organizer_email"] == "ops@acme.com"    # distinct organizer is kept, not coerced
        for camel in ("hostEmail", "audioUrl", "transcriptUrl", "meetingLink"):
            bad = _ff(client, settings, "{ transcripts(limit: 1) { %s } }" % camel)
            assert "data" not in bad and bad["errors"], camel
    finally:
        close()


def test_fireflies_duration_is_minutes(tmp_path):
    """The API's unit. Serving seconds would make every meeting read as 60x too long."""
    client, settings, close = _fireflies_client(tmp_path)
    try:
        t = _ff(client, settings, "{ transcripts(limit: 1) { duration } }")["data"]["transcripts"][0]
        assert t["duration"] == 45.0
    finally:
        close()


def test_fireflies_action_items_are_a_newline_joined_string(tmp_path):
    """Fireflies returns `summary.action_items` as ONE string, not a list — a client doing
    `.split("\\n")` on it must not get a JSON array."""
    client, settings, close = _fireflies_client(tmp_path)
    try:
        s = _ff(client, settings, "{ transcripts(limit: 1) { summary { action_items "
                                  "topics_discussed keywords overview meeting_type } } }"
                )["data"]["transcripts"][0]["summary"]
        assert s["action_items"] == "Ava: follow up\nBob: benchmark"
        # topics/keywords ARE lists in the real API, so they must stay lists
        assert s["topics_discussed"] == ["latency"]
        assert s["keywords"] == ["latency"]
        assert s["meeting_type"] == "discovery"
    finally:
        close()


def test_fireflies_sentence_times_are_seconds_while_duration_is_minutes(tmp_path):
    """The two units really do differ in the real API; a mock that made them agree would look
    tidier and be wrong."""
    client, settings, close = _fireflies_client(tmp_path)
    try:
        t = _ff(client, settings, "{ transcripts(limit: 1) { duration "
                                  "sentences { start_time end_time } } }"
                )["data"]["transcripts"][0]
        assert t["sentences"][1]["start_time"] == 20.0            # seconds
        assert t["duration"] == 45.0                              # minutes
        assert t["sentences"][-1]["end_time"] <= t["duration"] * 60
    finally:
        close()


def test_fireflies_speaker_id_is_an_integer_scoped_to_the_meeting(tmp_path):
    client, settings, close = _fireflies_client(tmp_path)
    try:
        sents = _ff(client, settings, "{ transcripts(limit: 1) { sentences "
                                      "{ index speaker_id speaker_name } } }"
                    )["data"]["transcripts"][0]["sentences"]
        assert [s["speaker_id"] for s in sents] == [0, 1]
        assert all(isinstance(s["speaker_id"], int) for s in sents)
        assert [s["index"] for s in sents] == [0, 1]
    finally:
        close()


def test_fireflies_stubbed_fields_are_null_not_invented(tmp_path):
    """The SDL declares more than a document corpus can back. Everything unbacked must be null —
    an invented sentiment or classifier flag is worse than an honest gap."""
    client, settings, close = _fireflies_client(tmp_path)
    try:
        t = _ff(client, settings, "{ transcripts(limit: 1) { apps_preview "
                                  "meeting_attendance { email joinedAt duration } "
                                  "summary { bullet_gist gist transcript_chapters } "
                                  "analytics { categories { questions tasks } } "
                                  "sentences { ai_filters { task question } } "
                                  "user { minutes_consumed is_admin integrations } } }"
                )["data"]["transcripts"][0]
        assert t["meeting_attendance"] is None and t["apps_preview"] is None
        assert t["summary"]["bullet_gist"] is None and t["summary"]["gist"] is None
        assert t["summary"]["transcript_chapters"] is None
        assert t["analytics"]["categories"]["questions"] is None
        assert t["sentences"][0]["ai_filters"] is None
        assert t["user"]["minutes_consumed"] is None and t["user"]["is_admin"] is None
    finally:
        close()


def test_fireflies_analytics_sentiments_sum_to_one_hundred(tmp_path):
    """Synthesized, never derived from the text — but it still has to be internally coherent, or a
    consumer charting it gets nonsense."""
    client, settings, close = _fireflies_client(tmp_path)
    try:
        s = _ff(client, settings, "{ transcripts(limit: 1) { analytics { sentiments "
                                  "{ positive_pct neutral_pct negative_pct } } } }"
                )["data"]["transcripts"][0]["analytics"]["sentiments"]
        assert round(s["positive_pct"] + s["neutral_pct"] + s["negative_pct"]) == 100
        assert all(v >= 0 for v in s.values())
    finally:
        close()


def test_fireflies_speaker_analytics_are_computed_from_the_sentences(tmp_path):
    """Talk time and word counts ARE derivable from the transcript, so unlike sentiment they are
    real rather than synthesized."""
    client, settings, close = _fireflies_client(tmp_path)
    try:
        t = _ff(client, settings, "{ transcripts(limit: 1) { analytics { speakers "
                                  "{ name duration word_count duration_pct } } "
                                  "sentences { speaker_name text start_time end_time } } }"
                )["data"]["transcripts"][0]
        by_name = {s["name"]: s for s in t["analytics"]["speakers"]}
        assert set(by_name) == {"Ava Chen", "Dana Ruiz"}
        for sent in t["sentences"]:
            spoken = len(sent["text"].split())
            assert by_name[sent["speaker_name"]]["word_count"] >= spoken
        assert by_name["Ava Chen"]["duration"] == 20.0     # 0 -> 20, its own window
    finally:
        close()


def test_fireflies_speaker_shares_sum_to_one_hundred(tmp_path):
    """`duration_pct` shares out the TALK TIME, not the declared meeting length: a corpus
    transcript often does not span its whole meeting, and dividing by the declared length emits
    shares summing to ~4%, which reads as a bug in anything that charts them."""
    client, settings, close = _fireflies_client(tmp_path)
    try:
        speakers = _ff(client, settings, "{ transcripts(limit: 1) { duration analytics "
                                         "{ speakers { duration duration_pct } } } }"
                       )["data"]["transcripts"][0]["analytics"]["speakers"]
        assert round(sum(s["duration_pct"] for s in speakers)) == 100
        # and the talk time really is far short of the declared 45-minute meeting
        assert sum(s["duration"] for s in speakers) < 45 * 60
    finally:
        close()


def test_fireflies_no_openapi_entry_for_the_graphql_route(tmp_path):
    """Describing one POST that accepts an arbitrary query tells an OpenAPI->MCP bridge nothing,
    so the route is deliberately absent from the document (and from SOURCE_PREFIXES)."""
    from app import openapi

    client, settings, close = _fireflies_client(tmp_path)
    try:
        spec = client.get("/openapi.json").json()
        assert not [p for p in spec["paths"] if p.startswith("/fireflies")]
        assert "fireflies" not in openapi.SOURCE_PREFIXES
    finally:
        close()


def test_fireflies_users_is_the_workspace_roster_not_every_named_person(tmp_path):
    """`users` must be the people with an ACCOUNT. The mock's principals table registers every
    internal reference across every source — 16,034 on the deployed bench corpus, of whom 327 have
    a token — so serving all of them would be wrong (they have no Fireflies account) AND a 1.6 MB
    unpaginated response. The real query takes no pagination args, so scoping is what bounds it.

    `user(id:)` must still resolve a display-only principal, or a transcript whose host never had
    an account would serve `user: null`.
    """
    import os

    from starlette.testclient import TestClient

    from app import store, synth
    from app.config import get_settings
    from app.main import app

    settings = _load(tmp_path, FIREFLIES_CORPUS)
    # A principal the corpus names but who has no token — what an ERB append creates. Inserted
    # BEFORE startup because the user_id -> email index is built in the lifespan, exactly as
    # Linear's by-id indexes are.
    conn = store.connect_rw(settings.db_path)
    conn.execute("INSERT OR REPLACE INTO principals(id, type, display_name, email) "
                 "VALUES (?,?,?,?)",
                 ("ghost@acme.com", "user", "Ghost Person", "ghost@acme.com"))
    conn.commit()
    conn.close()

    prev = os.environ.get("MOCK_DATA_DIR")
    os.environ["MOCK_DATA_DIR"] = str(settings.data_dir)
    get_settings.cache_clear()
    client = TestClient(app)
    client.__enter__()

    def close():
        client.__exit__(None, None, None)
        get_settings.cache_clear()
        if prev is None:
            os.environ.pop("MOCK_DATA_DIR", None)
        else:
            os.environ["MOCK_DATA_DIR"] = prev

    try:
        emails = {u["email"] for u in _ff(client, settings, "{ users { email } }")["data"]["users"]}
        assert "ghost@acme.com" not in emails, "a tokenless principal is not a workspace member"
        assert "ava@acme.com" in emails                    # the corpus's real, tokened host
        assert emails <= set(_roster_emails(settings))

        # ...but the display-only person is still addressable by id
        got = _ff(client, settings, 'query($i:String){ user(id:$i) { email name } }',
                  i=synth.fireflies_user_id("ghost@acme.com"))["data"]["user"]
        assert got["email"] == "ghost@acme.com" and got["name"] == "Ghost Person"
    finally:
        close()


def _roster_emails(settings):
    import yaml
    data = yaml.safe_load(settings.tokens_path.read_text()) or {}
    return [u["email"] for u in data.get("users", [])]
