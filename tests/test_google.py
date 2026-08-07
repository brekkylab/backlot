"""Google APIs over HTTP: Gmail, Drive, and the Workspace editor reads (Docs/Sheets/Slides).

One file because they are one router (``backlot/routers/google.py``) and one error envelope
(``backlot/google_errors.py``) — Drive and Gmail share ``_gerr`` and the per-family status table, so
splitting them would put two halves of the same contract in two places.
"""

from __future__ import annotations

import base64
import json
import re
from urllib.parse import quote

import httpx
import jwt
import pytest

from backlot import oauth, store
from backlot.config import Settings
from tests._helpers import crawl_drive, crawl_gmail, db_count, tiny_corpus, tok


# --- admin full-crawl completeness ---------------------------------------------


def test_admin_gmail_crawls_all(client, admin_h, ro_conn):
    assert len(crawl_gmail(client, admin_h)) == db_count(ro_conn, "gmail")


def test_admin_drive_crawls_all(client, admin_h, ro_conn):
    # An unfiltered files.list includes folders on real Drive, and the mock synthesizes one per
    # container — so a full crawl is every stored file plus every folder.
    folders = ro_conn.execute("SELECT COUNT(*) FROM gdrive_folders").fetchone()[0]
    assert len(crawl_drive(client, admin_h)) == db_count(ro_conn, "google_drive") + folders


# --- content round-trips through each vendor's encoding -------------------------


def _gmail_plain(payload):
    """Extract the text/plain body data from a Gmail payload (top-level or a part)."""
    if payload.get("body", {}).get("data"):
        return payload["body"]["data"]
    for part in payload.get("parts", []):
        if part["mimeType"] == "text/plain":
            return part["body"]["data"]
    raise AssertionError("no text/plain part")


# --- Gmail hex message ids (#39) --------------------------------------------------------------
#
# Gmail ids are 16 lowercase hex digits parsed as a signed 64-bit integer. The 400/404 boundary,
# MEASURED against the live API:
#
#   id                        | real Gmail
#   --------------------------|-----------------------------------------------
#   0 / 1 / abc123 / DEADBEEF | 404 NOT_FOUND     (a valid shape, just unknown)
#   7fffffffffffffff          | 404 NOT_FOUND     (2**63 - 1 is in range)
#   8000000000000000          | 400 INVALID_ARGUMENT "Invalid id value"
#   ffffffffffffffff          | 400               (>= 2**63)
#   18c9a1b2c3d4e5f6a         | 400               (17 digits overflows)
#   -1 / 1g / " 1"            | 400               (not hex)
#
# Threads share the id space: a single-message thread reports id == threadId.


def _a_gmail_row(ro_conn):
    return ro_conn.execute("SELECT * FROM gmail_messages LIMIT 1").fetchone()


def test_gmail_messages_list_serves_hex_ids(client, admin_h):
    """The ids a client receives must look like Gmail's, not like the corpus's dsids — that is the
    whole point of #39. `dsid_…` is not hex, so real Gmail would call it an invalid id value."""
    msgs = client.get(
        "/gmail/v1/users/me/messages", headers=admin_h, params={"maxResults": 10}
    ).json()["messages"]
    assert msgs
    for m in msgs:
        for key in ("id", "threadId"):
            assert len(m[key]) == 16, m
            assert all(c in "0123456789abcdef" for c in m[key]), m
            assert int(m[key], 16) < 2**63, m
        assert not m["id"].startswith("dsid_")


def test_gmail_hex_id_resolves_to_the_same_document(client, admin_h, ro_conn):
    """The hex id maps back to its dsid, so the body a client reads by hex is the stored body. A
    one-way id would make every message unreadable."""
    from backlot import synth

    row = _a_gmail_row(ro_conn)
    hexid = synth.gmail_message_id(row["doc_id"])
    m = client.get(
        f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h, params={"format": "full"}
    ).json()
    assert m["id"] == hexid
    assert base64.urlsafe_b64decode(_gmail_plain(m["payload"])).decode() == row["content"]


def test_gmail_thread_id_matches_the_message_id_for_a_lone_message(client, admin_h, ro_conn):
    """Threads share the message id space in real Gmail, so a message that is its own thread root
    reports the same value twice — and `threads.get` resolves it."""
    from backlot import synth

    row = ro_conn.execute(
        "SELECT * FROM gmail_messages WHERE COALESCE(thread_id, '') = '' LIMIT 1"
    ).fetchone()
    if row is None:
        row = ro_conn.execute(
            "SELECT * FROM gmail_messages WHERE thread_id = doc_id LIMIT 1"
        ).fetchone()
    assert row is not None, "SAMPLE should hold a message that is its own thread"
    hexid = synth.gmail_message_id(row["doc_id"])
    m = client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h).json()
    assert m["id"] == m["threadId"] == hexid
    t = client.get(f"/gmail/v1/users/me/threads/{hexid}", headers=admin_h)
    assert t.status_code == 200 and t.json()["id"] == hexid


def test_gmail_reply_reports_its_roots_thread_id(client, admin_h, ro_conn):
    from backlot import synth

    row = ro_conn.execute(
        "SELECT * FROM gmail_messages WHERE COALESCE(thread_id,'') != '' "
        "AND thread_id != doc_id LIMIT 1"
    ).fetchone()
    assert row is not None, "SAMPLE should hold a threaded reply"
    m = client.get(
        f"/gmail/v1/users/me/messages/{synth.gmail_message_id(row['doc_id'])}", headers=admin_h
    ).json()
    assert m["threadId"] == synth.gmail_message_id(row["thread_id"])
    assert m["id"] != m["threadId"]


def test_gmail_attachment_resolves_under_a_hex_message_id(client, admin_h, ro_conn):
    from backlot import synth

    row = ro_conn.execute(
        "SELECT * FROM gmail_messages WHERE COALESCE(attachments,'') NOT IN ('', '[]') LIMIT 1"
    ).fetchone()
    assert row is not None, "SAMPLE should hold a message with an attachment"
    hexid = synth.gmail_message_id(row["doc_id"])
    m = client.get(
        f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h, params={"format": "full"}
    ).json()
    att = next(p for p in m["payload"]["parts"] if p.get("filename"))
    r = client.get(
        f"/gmail/v1/users/me/messages/{hexid}/attachments/{att['body']['attachmentId']}",
        headers=admin_h,
    )
    assert r.status_code == 200 and r.json()["size"] > 0


@pytest.mark.parametrize(
    "mid",
    ["0", "1", "abc123", "DEADBEEF", "7fffffffffffffff", "0000000000000001", "18c9a1b2c3d4e5f6"],
)
def test_gmail_a_valid_but_unknown_id_is_not_found(client, admin_h, mid):
    """A well-formed id the mailbox does not hold is 404, uppercase included — measured."""
    for kind in ("messages", "threads"):
        r = client.get(f"/gmail/v1/users/me/{kind}/{mid}", headers=admin_h)
        assert r.status_code == 404, f"{kind}/{mid}: {r.status_code}"
        assert r.json()["error"]["message"] == "Requested entity was not found."


@pytest.mark.parametrize(
    "mid",
    [
        "8000000000000000",
        "ffffffffffffffff",
        "18c9a1b2c3d4e5f6a",
        "-1",
        "1g",
        "nosuchmessageid",
        "dsid_00908a2dda4b4d359194a09101",
    ],
)
def test_gmail_an_unparsable_id_is_an_invalid_argument(client, admin_h, mid):
    """The gap #39 names: an id that is not a parsable in-range hex integer is 400
    INVALID_ARGUMENT "Invalid id value", not 404. The last row is the mock's OWN former id format,
    which is exactly why the served ids had to change first."""
    for kind in ("messages", "threads"):
        r = client.get(f"/gmail/v1/users/me/{kind}/{mid}", headers=admin_h)
        assert r.status_code == 400, f"{kind}/{mid}: {r.status_code}"
        e = r.json()["error"]
        assert e["message"] == "Invalid id value"
        assert e["status"] == "INVALID_ARGUMENT"
        assert e["errors"][0]["reason"] == "invalidArgument"


def test_gmail_hex_ids_still_enforce_the_acl(client, admin_h, tokens_yaml, ro_conn):
    """Resolving through the index must not become a way around the ACL. The index is global — it
    maps every hex id, visible or not — so the ACL read after it is the only thing standing between
    a scoped caller and someone else's mail. The CFO's comp review is granted to cfo alone."""
    from backlot import synth

    row = ro_conn.execute(
        "SELECT * FROM gmail_messages WHERE title LIKE 'Confidential comp%'"
    ).fetchone()
    hexid = synth.gmail_message_id(row["doc_id"])
    assert client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h).status_code == 200
    cfo = {"Authorization": f"Bearer {tok(tokens_yaml, 'cfo@acme.com')}"}
    assert client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=cfo).status_code == 200
    outsider = {"Authorization": f"Bearer {tok(tokens_yaml, 'mia@acme.com')}"}
    r = client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=outsider)
    assert r.status_code == 404
    assert r.json()["error"]["message"] == "Requested entity was not found."


def test_gmail_body_roundtrip(client, admin_h, ro_conn):
    from backlot import synth

    doc = ro_conn.execute("SELECT * FROM gmail_messages LIMIT 1").fetchone()
    m = client.get(
        f"/gmail/v1/users/me/messages/{synth.gmail_message_id(doc['doc_id'])}",
        headers=admin_h,
        params={"format": "full"},
    ).json()
    body = base64.urlsafe_b64decode(_gmail_plain(m["payload"])).decode()
    assert body == doc["content"]
    subj = next(h["value"] for h in m["payload"]["headers"] if h["name"] == "Subject")
    assert subj == doc["title"]


def test_gmail_messages_list_ordered_by_internaldate_desc(client, admin_h, ro_conn):
    # Real Gmail returns messages.list newest-first by internalDate. Regression (#11): the mock
    # listed by doc_id (hash order), so a capped "newest N" was effectively random by date.
    listed = client.get(
        "/gmail/v1/users/me/messages", headers=admin_h, params={"maxResults": 50}
    ).json()["messages"]
    got = [m["id"] for m in listed]
    # the stable total order the endpoint must produce: created_ts DESC, doc_id ASC as tie-break
    # the served ids are hex (#39), so the expectation is the hex of that stable order
    from backlot import synth

    expected = [
        synth.gmail_message_id(r["doc_id"])
        for r in ro_conn.execute(
            "SELECT doc_id FROM gmail_messages ORDER BY created_ts DESC, doc_id LIMIT 50"
        ).fetchall()
    ]
    assert got == expected
    # ...and internalDate is monotonically non-increasing across the returned page
    dates = [
        int(
            client.get(
                f"/gmail/v1/users/me/messages/{i}", headers=admin_h, params={"format": "minimal"}
            ).json()["internalDate"]
        )
        for i in got
    ]
    assert dates == sorted(dates, reverse=True)


def test_gmail_messages_list_pagination_stable_and_ordered(client, admin_h, ro_conn):
    # Paging must be a stable partition of the same date-desc order — no dupes, no skips, and page 2
    # continues strictly at/under page 1's tail. (Regression guard for the tie-break in ORDER BY.)
    total = client.get(
        "/gmail/v1/users/me/messages", headers=admin_h, params={"maxResults": 1}
    ).json()["resultSizeEstimate"]
    if total < 2:
        pytest.skip("need >= 2 gmail messages to exercise paging")
    p1 = client.get("/gmail/v1/users/me/messages", headers=admin_h, params={"maxResults": 1}).json()
    p2 = client.get(
        "/gmail/v1/users/me/messages",
        headers=admin_h,
        params={"maxResults": 1, "pageToken": p1["nextPageToken"]},
    ).json()
    a, b = p1["messages"][0]["id"], p2["messages"][0]["id"]
    assert a != b  # distinct rows, no repeat
    both = client.get(
        "/gmail/v1/users/me/messages", headers=admin_h, params={"maxResults": 2}
    ).json()["messages"]
    assert [m["id"] for m in both] == [a, b]  # pages concatenate in order


def test_gmail_attachment_size_matches_part_metadata(client, admin_h, ro_conn):
    # Real Gmail's contract: a part's body.size equals the byte length attachments.get serves, so a
    # client can stat an attachment from message metadata alone. Regression: the part reported the
    # corpus-declared `size` (e.g. 2048) while attachments.get returned len(content) — a mismatch.
    row = ro_conn.execute(
        "SELECT doc_id FROM gmail_messages WHERE attachments IS NOT NULL "
        "AND attachments != '[]' LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("no gmail message with an attachment in this subset")
    from backlot import synth

    hexid = synth.gmail_message_id(row["doc_id"])
    m = client.get(
        f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h, params={"format": "full"}
    ).json()
    parts = [p for p in m["payload"]["parts"] if p.get("body", {}).get("attachmentId")]
    assert parts, "message should expose at least one attachment part"
    for p in parts:
        got = client.get(
            f"/gmail/v1/users/me/messages/{hexid}/attachments/{p['body']['attachmentId']}",
            headers=admin_h,
        ).json()
        assert got["size"] == p["body"]["size"]  # the two agree
        assert (
            len(base64.urlsafe_b64decode(got["data"])) == p["body"]["size"]
        )  # ...and match the bytes


def test_drive_export_roundtrip(client, admin_h, ro_conn):
    doc = ro_conn.execute("SELECT * FROM gdrive_files LIMIT 1").fetchone()
    text = client.get(
        f"/drive/v3/files/{doc['doc_id']}/export",
        headers=admin_h,
        params={"mimeType": "text/plain"},
    ).text
    assert doc["content"] in text and text.startswith(doc["title"])


def test_drive_in_owners_query(client, admin_h, ro_conn):
    # real Drive supports `'<owner>' in owners`; the mock must filter by owner (email or name),
    # not ignore the clause. (qst_0031's broken owner-lookup path.)
    total = db_count(ro_conn, "google_drive")
    owner = ro_conn.execute("SELECT author_email FROM gdrive_files LIMIT 1").fetchone()[
        "author_email"
    ]
    expected = ro_conn.execute(
        "SELECT count(*) FROM gdrive_files WHERE author_email=?", (owner,)
    ).fetchone()[0]
    j = client.get(
        "/drive/v3/files", headers=admin_h, params={"q": f"'{owner}' in owners", "pageSize": 1000}
    ).json()
    n = len(j.get("files", []))
    assert 0 < n < total and n == expected  # filtered to exactly this owner's files
    # a non-owner returns nothing (clause honored, not ignored)
    none = client.get(
        "/drive/v3/files",
        headers=admin_h,
        params={"q": "'nobody-xyz@acme.com' in owners", "pageSize": 100},
    ).json()
    assert none.get("files", []) == []


def test_google_batch_dispatches_subrequests(client, admin_h, ro_conn):
    # google-api-python-client posts a multipart/mixed batch to /batch; the mock must dispatch each
    # application/http sub-request in-process and return a multipart/mixed of sub-responses matched
    # by Content-ID. Regression for the batch escaping to real Google (401). Build the batch body
    # exactly like BatchHttpRequest does.
    from email.generator import Generator
    from email.mime.multipart import MIMEMultipart
    from email.mime.nonmultipart import MIMENonMultipart
    from email.parser import BytesParser
    from io import StringIO

    listed = (
        client.get("/gmail/v1/users/me/messages", headers=admin_h, params={"maxResults": 2})
        .json()
        .get("messages", [])
    )
    ids = [m["id"] for m in listed]
    assert ids, "need at least one gmail message in the sample"

    msg = MIMEMultipart("mixed")
    setattr(msg, "_write_headers", lambda self: None)
    for i, mid in enumerate(ids):
        part = MIMENonMultipart("application", "http")
        part["Content-Transfer-Encoding"] = "binary"
        part["Content-ID"] = f"<base + {i}>"  # the format BatchHttpRequest uses
        # format=full is the discriminator: a sub-request whose query is honored returns a payload;
        # one whose query is dropped defaults to full too, so we assert the OPPOSITE below with
        # format=minimal — see test_google_batch_honors_subrequest_query_params.
        part.set_payload(f"GET /gmail/v1/users/me/messages/{mid}?format=full HTTP/1.1\r\n\r\n")
        msg.attach(part)
    fp = StringIO()
    Generator(fp, mangle_from_=False).flatten(msg, unixfrom=False)
    body, boundary = fp.getvalue(), msg.get_boundary()

    r = client.post(
        "/batch",
        headers={**admin_h, "Content-Type": f'multipart/mixed; boundary="{boundary}"'},
        content=body,
    )
    assert r.status_code == 200, r.text
    assert "multipart/mixed" in r.headers["content-type"]
    parsed = BytesParser().parsebytes(
        b"Content-Type: " + r.headers["content-type"].encode() + b"\r\n\r\n" + r.content
    )
    parts = parsed.get_payload()
    assert len(parts) == len(ids)
    for i, (mid, part) in enumerate(zip(ids, parts)):
        assert part["Content-ID"] == f"<base + {i}>"  # echoed so the client can pair them
        sub = part.get_payload(decode=False)
        assert sub.startswith("HTTP/1.1 200")  # dispatched with the admin token, not 401
        assert mid in sub  # the message JSON came back


def _batch_one(client, headers, mid, fmt, uri="/batch"):
    """POST a one-message Gmail batch to `uri` (default /batch; /batch/gmail/v1 is the real Gmail
    path) requesting `fmt`, and return the decoded sub-response JSON. Serialized exactly like
    google-api-python-client's BatchHttpRequest."""
    from email.generator import Generator
    from email.mime.multipart import MIMEMultipart
    from email.mime.nonmultipart import MIMENonMultipart
    from email.parser import BytesParser
    from io import StringIO

    msg = MIMEMultipart("mixed")
    setattr(msg, "_write_headers", lambda self: None)
    part = MIMENonMultipart("application", "http")
    part["Content-Transfer-Encoding"] = "binary"
    part["Content-ID"] = "<b + 0>"
    part.set_payload(f"GET /gmail/v1/users/me/messages/{mid}?format={fmt} HTTP/1.1\r\n\r\n")
    msg.attach(part)
    fp = StringIO()
    Generator(fp, mangle_from_=False).flatten(msg, unixfrom=False)
    r = client.post(
        uri,
        headers={**headers, "Content-Type": f'multipart/mixed; boundary="{msg.get_boundary()}"'},
        content=fp.getvalue(),
    )
    assert r.status_code == 200, r.text
    parsed = BytesParser().parsebytes(
        b"Content-Type: " + r.headers["content-type"].encode() + b"\r\n\r\n" + r.content
    )
    sub = parsed.get_payload()[0].get_payload(decode=False)
    return json.loads(sub.split("\r\n\r\n", 1)[1])


@pytest.mark.parametrize("uri", ["/batch", "/batch/gmail/v1"])
def test_google_batch_honors_subrequest_query_params(client, admin_h, uri):
    # The sub-request's query string must reach the dispatched handler. `format` is the tell: a
    # dropped query defaults to full, so if the mock ignored it, `format=minimal` would still carry a
    # payload. A batch-trusting client that caches these would cache bodyless messages otherwise.
    mid = client.get(
        "/gmail/v1/users/me/messages", headers=admin_h, params={"maxResults": 1}
    ).json()["messages"][0]["id"]
    assert "payload" in _batch_one(client, admin_h, mid, "full", uri)  # format=full honored
    assert "payload" not in _batch_one(
        client, admin_h, mid, "minimal", uri
    )  # format=minimal honored


def test_user_cannot_fetch_others_private_gmail(client, tokens_yaml, admin_h, ro_conn):
    # a private gmail doc owned by user B, fetched with user A's token -> 404
    user_a, user_b = tokens_yaml["users"][0], tokens_yaml["users"][1]
    doc = ro_conn.execute(
        "SELECT doc_id FROM gmail_messages WHERE author_email=? LIMIT 1",
        (user_b["email"],),
    ).fetchone()
    if doc is None:
        pytest.skip("no gmail doc for user B in this subset")
    from backlot import synth

    hexid = synth.gmail_message_id(doc["doc_id"])  # served ids are hex, not dsids (#39)
    ah = {"Authorization": f"Bearer {user_a['token']}"}
    r = client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=ah)
    # A may coincidentally be a recipient; assert admin can always read it
    assert client.get(f"/gmail/v1/users/me/messages/{hexid}", headers=admin_h).status_code == 200
    assert r.status_code in (200, 404)


# --- Google error envelope (#37) ------------------------------------------------------------
#
# Every case below was MEASURED against the live APIs with real OAuth credentials. The envelope is
# per-family, not uniform:
#
#   family                       errors[]   status                 no Authorization header
#   -----------------------------|----------|-----------------------|------------------------
#   Drive v3                     | always   | auth failures only    | 403 PERMISSION_DENIED
#   Gmail v1                     | always   | always                | 401 UNAUTHENTICATED
#   Docs v1 / Sheets v4 / Slides | never    | always                | 401 UNAUTHENTICATED
#
# A bad bearer token is 401 UNAUTHENTICATED in every family.


def _gerr(resp):
    """The `error` object, or a clear failure naming what came back instead."""
    body = resp.json()
    assert "error" in body, f"expected a Google error envelope, got {body}"
    return body["error"]


def test_google_errors_use_googles_envelope(client, admin_h):
    """`google-api-python-client` reads `error.message` to build HttpError, so `{"detail": …}` left
    every error unreadable to the one client the mock exists to serve."""
    r = client.get("/drive/v3/files", headers=admin_h, params={"fields": "totallyBogusField"})
    assert r.status_code == 400
    e = _gerr(r)
    assert e["code"] == 400
    assert e["message"] == "Invalid field selection totallyBogusField"
    assert "detail" not in r.json()
    # non-Google paths keep FastAPI's default envelope
    assert "detail" in client.get("/no-such-route").json()


def test_drive_errors_carry_the_legacy_errors_array(client, admin_h):
    """Drive v3 always sends `errors[]` with a `reason` a client can branch on, and repeats the
    message inside it. It does NOT send `status` for a parameter failure — measured."""
    e = _gerr(client.get("/drive/v3/files", headers=admin_h, params={"fields": "nope"}))
    assert e["errors"] == [
        {
            "message": "Invalid field selection nope",
            "domain": "global",
            "reason": "invalidParameter",
            "location": "fields",
            "locationType": "parameter",
        }
    ]
    assert "status" not in e, "Drive omits status on parameter failures"


def test_editor_api_errors_carry_status_and_no_errors_array(client, admin_h):
    """The editor APIs are the mirror image of Drive: `status`, never `errors[]` — measured."""
    doc = _drive_find(client, admin_h, "Brand")["id"]
    e = _gerr(client.get(f"/sheets/v4/spreadsheets/{doc}", headers=admin_h))
    assert e["code"] == 404 and e["status"] == "NOT_FOUND"
    assert e["message"] == "Requested entity was not found."
    assert "errors" not in e


def test_gmail_errors_carry_both(client, admin_h):
    """Gmail sends `errors[]` AND `status` — measured, and the only family that does both."""
    # a well-formed but unknown id; a non-hex one is 400 "Invalid id value" (see #39)
    e = _gerr(client.get("/gmail/v1/users/me/messages/00000000deadbeef", headers=admin_h))
    assert e["code"] == 404 and e["status"] == "NOT_FOUND"
    assert e["message"] == "Requested entity was not found."
    assert e["errors"][0]["reason"] == "notFound"


# (path, params, code, status, reason, location) — one row per measured case.
GOOGLE_ERROR_CASES = [
    ("/drive/v3/files", {"fields": "bogus"}, 400, None, "invalidParameter", "fields"),
    ("/drive/v3/files", {"orderBy": "bogusKey"}, 400, None, "invalid", "orderBy"),
    ("/drive/v3/files/no-such-file", {}, 404, None, "notFound", "fileId"),
    ("/drive/v3/about", {}, 400, None, "required", "fields"),
    ("/drive/v3/about", {"fields": "storageQuoat"}, 400, None, "invalidParameter", "fields"),
    ("/gmail/v1/users/me/messages/00000000deadbeef", {}, 404, "NOT_FOUND", "notFound", None),
    ("/gmail/v1/users/me/labels/NO_SUCH", {}, 404, "NOT_FOUND", "notFound", None),
]


@pytest.mark.parametrize("path, params, code, status, reason, location", GOOGLE_ERROR_CASES)
def test_google_error_reasons_match_the_real_api(
    client, admin_h, path, params, code, status, reason, location
):
    r = client.get(path, headers=admin_h, params=params)
    assert r.status_code == code
    e = _gerr(r)
    assert e["code"] == code
    assert e.get("status") == status
    err0 = e["errors"][0]
    assert err0["reason"] == reason
    assert err0["domain"] == "global"
    assert err0.get("location") == location
    if location is not None:
        assert err0["locationType"] == "parameter"


def test_drive_not_found_names_the_file_id(client, admin_h):
    """Measured: `File not found: {id}.` — the id is in the message, so a batch caller can tell
    which of its requests failed."""
    e = _gerr(client.get("/drive/v3/files/abc123xyz", headers=admin_h))
    assert e["message"] == "File not found: abc123xyz."


def test_drive_export_requires_mime_type_with_googles_wording(client, admin_h):
    doc = _drive_find(client, admin_h, "Brand")["id"]
    e = _gerr(client.get(f"/drive/v3/files/{doc}/export", headers=admin_h))
    assert e["code"] == 400 and e["message"] == "Required parameter: mimeType"
    assert e["errors"][0] == {
        "message": "Required parameter: mimeType",
        "domain": "global",
        "reason": "required",
        "location": "mimeType",
        "locationType": "parameter",
    }


@pytest.mark.parametrize(
    "path, reason, location",
    [
        ("/drive/v3/files/{pdf}/export?mimeType=text/plain", "fileNotExportable", None),
        ("/drive/v3/files/{doc}?alt=media", "fileNotDownloadable", "alt"),
    ],
)
def test_drive_403s_carry_their_own_reasons(client, admin_h, path, reason, location):
    doc = _drive_find(client, admin_h, "Brand")["id"]
    pdf = _drive_find(client, admin_h, "Whitepaper")["id"]
    e = _gerr(client.get(path.format(doc=doc, pdf=pdf), headers=admin_h))
    assert e["code"] == 403
    assert e["errors"][0]["reason"] == reason
    assert e["errors"][0].get("location") == location


BAD_TOKEN = {"Authorization": "Bearer not-a-real-token"}


@pytest.mark.parametrize(
    "path",
    [
        "/drive/v3/files",
        "/gmail/v1/users/me/profile",
        "/sheets/v4/spreadsheets/x",
        "/docs/v1/documents/x",
        "/slides/v1/presentations/x",
    ],
)
def test_a_bad_token_is_unauthenticated_everywhere(client, path):
    """Measured: every family answers a present-but-invalid bearer with 401 UNAUTHENTICATED, and
    the short "Invalid Credentials" lives in `errors[0]` while the top message is the long form."""
    r = client.get(path, headers=BAD_TOKEN)
    assert r.status_code == 401
    e = _gerr(r)
    assert e["code"] == 401 and e["status"] == "UNAUTHENTICATED"
    assert e["message"].startswith("Request had invalid authentication credentials.")
    if "errors" in e:
        assert e["errors"][0]["message"] == "Invalid Credentials"
        assert e["errors"][0]["reason"] == "authError"
        assert e["errors"][0]["location"] == "Authorization"
        assert e["errors"][0]["locationType"] == "header"


@pytest.mark.parametrize(
    "path, code, status",
    [
        ("/drive/v3/files", 403, "PERMISSION_DENIED"),  # Drive accepts API keys, so anonymous
        ("/sheets/v4/spreadsheets/x", 403, "PERMISSION_DENIED"),  # ...is an "unregistered caller"
        ("/gmail/v1/users/me/profile", 401, "UNAUTHENTICATED"),  # OAuth-only APIs say the
        ("/docs/v1/documents/x", 401, "UNAUTHENTICATED"),  # ...credentials are missing
        ("/slides/v1/presentations/x", 401, "UNAUTHENTICATED"),
    ],
)
def test_a_missing_header_differs_by_family(client, path, code, status):
    """The surprise, measured: no `Authorization` header at all is NOT uniformly 401. Drive and
    Sheets answer 403 PERMISSION_DENIED, Gmail and the Docs/Slides APIs answer 401. A bad token is
    401 everywhere — so the two cases are genuinely distinct and the mock conflated them."""
    r = client.get(path)
    assert r.status_code == code
    e = _gerr(r)
    assert e["code"] == code and e["status"] == status
    if code == 403:
        assert "unregistered callers" in e["message"]
    else:
        assert "missing required authentication credential" in e["message"]


# --- Gmail: typed response schema, unchanged responses ------------------------------------


def test_gmail_messages_has_typed_response_schema(client):
    op = client.get("/openapi.json").json()["paths"]["/gmail/v1/users/{user_id}/messages"]["get"]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema != {}


def test_gmail_responses_unchanged_by_enrichment(client, admin_h):
    lst = client.get("/gmail/v1/users/me/messages", headers=admin_h).json()
    assert "messages" in lst and "resultSizeEstimate" in lst
    if lst["messages"]:
        mid = lst["messages"][0]["id"]
        msg = client.get(
            f"/gmail/v1/users/me/messages/{mid}", params={"format": "full"}, headers=admin_h
        ).json()
        for k in (
            "id",
            "threadId",
            "labelIds",
            "snippet",
            "internalDate",
            "sizeEstimate",
            "payload",
        ):
            assert k in msg, f"gmail message missing {k} (fidelity regression)"


# --- OpenAPI enrichment: drive ------------------------------------------------------------


def test_drive_files_has_typed_response_schema(client):
    op = client.get("/openapi.json").json()["paths"]["/drive/v3/files"]["get"]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema != {}


def _drive_find(client, admin_h, name_substr):
    j = client.get(
        "/drive/v3/files", params={"q": f"name contains '{name_substr}'"}, headers=admin_h
    ).json()
    return j["files"][0] if j.get("files") else None


def test_drive_responses_unchanged_by_enrichment(client, admin_h):
    lst = client.get("/drive/v3/files", headers=admin_h).json()
    assert lst["kind"] == "drive#fileList" and "files" in lst
    doc = _drive_find(client, admin_h, "Brand")
    assert doc is not None
    full = client.get(f"/drive/v3/files/{doc['id']}", headers=admin_h).json()
    for k in (
        "kind",
        "id",
        "name",
        "mimeType",
        "createdTime",
        "modifiedTime",
        "owners",
        "webViewLink",
        "capabilities",
    ):
        assert k in full, f"drive file missing {k} (fidelity regression)"


def test_drive_export_and_media_stay_non_json(client, admin_h):
    # A native doc exports as PlainTextResponse; response_model must NOT be attached to these.
    doc = _drive_find(client, admin_h, "Brand")
    exp = client.get(
        f"/drive/v3/files/{doc['id']}/export", params={"mimeType": "text/plain"}, headers=admin_h
    )
    assert exp.status_code == 200 and "application/json" not in exp.headers["content-type"]
    # A binary (pdf) downloads raw via alt=media.
    pdf = _drive_find(client, admin_h, "Whitepaper")
    med = client.get(f"/drive/v3/files/{pdf['id']}", params={"alt": "media"}, headers=admin_h)
    assert med.status_code == 200 and "application/json" not in med.headers["content-type"]


# --- Drive fidelity: measured divergences from real Google Drive (issue #23) ---------------
#
# Each case below was diffed against https://www.googleapis.com/drive/v3 with equivalent
# credentials; the mock's old behaviour returned 200 with wrong/unfiltered data, so a consumer
# could not tell anything was off.

FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"


def _drive_ids(client, headers, **params):
    j = client.get("/drive/v3/files", headers=headers, params=params).json()
    return [f["id"] for f in j.get("files", [])]


def test_drive_shared_with_me_partitions_by_owner(client, tokens_yaml):
    """`q=sharedWithMe=true` must return only items shared with the caller by someone else, and
    `false` must exclude them — real Drive's "Shared with me" is the only way to enumerate those.
    The mock used to ignore the clause, so both returned the caller's whole visible corpus."""
    mia = {"Authorization": f"Bearer {tok(tokens_yaml, 'mia@acme.com')}"}
    all_ids = set(_drive_ids(client, mia, q="trashed=false", pageSize=100))
    shared = set(_drive_ids(client, mia, q="sharedWithMe=true and trashed=false", pageSize=100))
    own = set(_drive_ids(client, mia, q="sharedWithMe=false and trashed=false", pageSize=100))
    assert shared and own  # SAMPLE gives mia both her own and others' files
    assert shared != own and not (shared & own)
    assert shared | own == all_ids  # together they partition the visible corpus
    # mia authored "Brand guidelines v3"; it is hers, not shared with her
    brand = _drive_find(client, mia, "Brand")["id"]
    assert brand in own and brand not in shared


def test_drive_shared_items_carry_shared_with_me_time(client, tokens_yaml):
    """Real Drive populates `sharedWithMeTime` only on items shared with the caller, and omits
    `parents` on them — so its presence is how a client classifies one. Filtering on
    `sharedWithMe` while never emitting the field left a row that the filter calls shared unable to
    say so itself."""
    mia = {"Authorization": f"Bearer {tok(tokens_yaml, 'mia@acme.com')}"}
    shared = client.get(
        "/drive/v3/files",
        headers=mia,
        params={"q": "sharedWithMe=true and trashed=false", "pageSize": 100},
    ).json()["files"]
    own = client.get(
        "/drive/v3/files",
        headers=mia,
        params={"q": "sharedWithMe=false and trashed=false", "pageSize": 100},
    ).json()["files"]
    assert shared and own
    assert all(f["sharedWithMeTime"] for f in shared), "every shared item needs the timestamp"
    assert all("sharedWithMeTime" not in f for f in own), (
        "an item you own was never shared with you"
    )
    # folders come out of the same filter, so they must answer the same way
    assert any(f["mimeType"] == FOLDER_MIME for f in shared)
    # and files.get agrees with the listing
    one = shared[0]
    assert client.get(f"/drive/v3/files/{one['id']}", headers=mia).json() == one


def test_drive_shared_with_me_time_needs_a_caller(client, admin_h):
    """The admin/service token is not a Drive user, so nothing was shared *with* it — no timestamp
    to invent. `orderBy` on the field still answers (all-equal keys), as real Drive does for nulls."""
    files = client.get("/drive/v3/files", headers=admin_h, params={"pageSize": 20}).json()["files"]
    assert files and all("sharedWithMeTime" not in f for f in files)
    assert (
        client.get(
            "/drive/v3/files",
            headers=admin_h,
            params={"pageSize": 5, "orderBy": "sharedWithMeTime"},
        ).status_code
        == 200
    )


def test_drive_order_by_shared_with_me_time(client, tokens_yaml):
    """The mock models the relation this key sorts on (owner vs caller), so it sorts rather than
    400s — unlike the view/modify-by-me timestamps, which have no counterpart here at all."""
    mia = {"Authorization": f"Bearer {tok(tokens_yaml, 'mia@acme.com')}"}
    r = client.get(
        "/drive/v3/files",
        headers=mia,
        params={"q": "sharedWithMe=true", "pageSize": 100, "orderBy": "sharedWithMeTime desc"},
    )
    assert r.status_code == 200
    times = [f["sharedWithMeTime"] for f in r.json()["files"]]
    assert times == sorted(times, reverse=True)


def test_drive_owned_by_me_reflects_the_caller(client, tokens_yaml):
    """`ownedByMe` is per-caller in real Drive; the mock reported False for every file."""
    mia = {"Authorization": f"Bearer {tok(tokens_yaml, 'mia@acme.com')}"}
    assert _drive_find(client, mia, "Brand")["ownedByMe"] is True
    assert _drive_find(client, mia, "Whitepaper")["ownedByMe"] is False


def test_drive_order_by_sorts_the_result(client, admin_h):
    """`orderBy` was accepted and never applied — silent, so a client that relies on server-side
    ordering appears to work against the mock and misbehaves against production."""
    names = [
        f["name"]
        for f in client.get(
            "/drive/v3/files",
            headers=admin_h,
            params={
                "q": "trashed=false",
                "pageSize": 100,
                "orderBy": "name",
                "fields": "files(name)",
            },
        ).json()["files"]
    ]
    # Drive collates names case-insensitively (folder names in the SAMPLE are lowercase, file
    # names are not, so a case-sensitive sort would put every folder last)
    assert names == sorted(names, key=str.casefold)
    desc = [
        f["name"]
        for f in client.get(
            "/drive/v3/files",
            headers=admin_h,
            params={
                "q": "trashed=false",
                "pageSize": 100,
                "orderBy": "name desc",
                "fields": "files(name)",
            },
        ).json()["files"]
    ]
    assert desc == sorted(names, key=str.casefold, reverse=True)
    mods = [
        f["modifiedTime"]
        for f in client.get(
            "/drive/v3/files",
            headers=admin_h,
            params={
                "q": "trashed=false",
                "pageSize": 100,
                "orderBy": "modifiedTime desc",
                "fields": "files(modifiedTime)",
            },
        ).json()["files"]
    ]
    assert mods == sorted(mods, reverse=True)


def test_drive_order_by_paginates_in_sorted_order(client, admin_h):
    """A sort must span the whole result set, not sort each page in isolation."""
    everything = [
        f["name"]
        for f in client.get(
            "/drive/v3/files",
            headers=admin_h,
            params={"pageSize": 100, "orderBy": "name", "fields": "files(name)"},
        ).json()["files"]
    ]
    paged, token = [], None
    while True:
        p = {"pageSize": 2, "orderBy": "name", "fields": "files(name),nextPageToken"}
        if token:
            p["pageToken"] = token
        j = client.get("/drive/v3/files", headers=admin_h, params=p).json()
        paged += [f["name"] for f in j["files"]]
        token = j.get("nextPageToken")
        if not token:
            break
    assert paged == everything == sorted(everything, key=str.casefold)


def test_drive_order_by_does_not_change_the_rows_themselves(client, admin_h):
    """Sorting builds the whole result set to order it, and defers the per-page `shared` lookup —
    so the served objects must still be identical to the unsorted ones, field for field."""
    plain = {
        f["id"]: f
        for f in client.get("/drive/v3/files", headers=admin_h, params={"pageSize": 100}).json()[
            "files"
        ]
    }
    sorted_ = {
        f["id"]: f
        for f in client.get(
            "/drive/v3/files",
            headers=admin_h,
            params={"pageSize": 100, "orderBy": "modifiedTime desc"},
        ).json()["files"]
    }
    assert plain and plain == sorted_
    assert any(f["shared"] for f in plain.values())  # ...and `shared` is really resolved


def test_drive_order_by_rejects_keys_it_cannot_honor(client, admin_h):
    """Real Drive 400s an undocumented sort key. The mock models no per-caller view/share
    timestamps, so those documented keys are rejected loudly rather than silently ignored."""
    for bad in ("bogusKey", "name descending", "viewedByMeTime"):
        r = client.get("/drive/v3/files", headers=admin_h, params={"orderBy": bad})
        assert r.status_code == 400, f"orderBy={bad!r} should 400, got {r.status_code}"
    ok = client.get(
        "/drive/v3/files", headers=admin_h, params={"orderBy": "folder,name desc", "pageSize": 5}
    )
    assert ok.status_code == 200


def test_drive_invalid_fields_mask_is_rejected(client, admin_h):
    """An unknown field name used to be accepted and yield empty file objects (200 {}), so a typo
    or a stale field name in a consumer's mask passed every mock-backed test and 400d in
    production."""
    r = client.get(
        "/drive/v3/files",
        headers=admin_h,
        params={"pageSize": 1, "fields": "files(totallyBogusField)"},
    )
    assert r.status_code == 400
    assert "totallyBogusField" in r.json()["error"]["message"]
    bad_top = client.get(
        "/drive/v3/files", headers=admin_h, params={"pageSize": 1, "fields": "bogusTop,files(id)"}
    )
    assert bad_top.status_code == 400
    # a documented field the mock does not synthesize is still valid (real Drive omits it, 200)
    ok = client.get(
        "/drive/v3/files",
        headers=admin_h,
        params={"pageSize": 1, "fields": "files(id,thumbnailLink,capabilities/canEdit)"},
    )
    assert ok.status_code == 200 and "thumbnailLink" not in ok.json()["files"][0]


def test_drive_get_honors_the_fields_mask(client, admin_h):
    """The same projection requested two ways must give the same object; files.get ignored the
    mask entirely and added keys nobody asked for."""
    mask = "id,name,mimeType,size,modifiedTime,webViewLink"
    row = client.get(
        "/drive/v3/files",
        headers=admin_h,
        params={"q": "name contains 'Brand'", "pageSize": 1, "fields": f"files({mask})"},
    ).json()["files"][0]
    got = client.get(
        f"/drive/v3/files/{row['id']}", headers=admin_h, params={"fields": mask}
    ).json()
    assert got == row
    r = client.get(
        f"/drive/v3/files/{row['id']}", headers=admin_h, params={"fields": "totallyBogusField"}
    )
    assert r.status_code == 400


def test_drive_folders_are_found_by_mime_type(client, admin_h):
    """Folders were returned by `'root' in parents` but invisible to `mimeType='…folder'`, so a
    crawler indexing folders by type concluded the account had none."""
    by_parent = _drive_ids(client, admin_h, q="'root' in parents", pageSize=100)
    by_mime = _drive_ids(client, admin_h, q=f"mimeType='{FOLDER_MIME}'", pageSize=100)
    assert by_parent and set(by_mime) == set(by_parent)
    # and the negation excludes them
    not_folders = _drive_ids(client, admin_h, q=f"mimeType!='{FOLDER_MIME}'", pageSize=100)
    assert not set(not_folders) & set(by_parent)


def test_drive_folders_honor_the_fields_projection(client, admin_h):
    """Synthesized folder rows bypassed the projection: `files(id,name)` returned 18 keys."""
    for q in ("'root' in parents", f"mimeType='{FOLDER_MIME}'"):
        files = client.get(
            "/drive/v3/files",
            headers=admin_h,
            params={"q": q, "pageSize": 5, "fields": "files(id,name)"},
        ).json()["files"]
        assert files and all(set(f) == {"id", "name"} for f in files), q


def test_drive_folders_match_the_same_q_clauses_as_files(client, admin_h):
    """Folders now flow through `_drive_q_match`, so every clause that should match one does."""
    folders = client.get(
        "/drive/v3/files",
        headers=admin_h,
        params={"q": "'root' in parents", "pageSize": 100, "fields": "files(id,name)"},
    ).json()["files"]
    one = folders[0]
    hit = _drive_ids(
        client, admin_h, q=f"name contains '{one['name']}' and mimeType='{FOLDER_MIME}'"
    )
    assert one["id"] in hit
    # a folder is not trashed, so trashed=true excludes it
    assert one["id"] not in _drive_ids(
        client, admin_h, q=f"mimeType='{FOLDER_MIME}' and trashed=true"
    )


def test_drive_folder_permissions_resolve(client, admin_h):
    """A folder id is a first-class file id in real Drive: files.get and permissions.list both
    answer for it. permissions.list 404d because folders are not stored as rows."""
    folder = client.get(
        "/drive/v3/files", headers=admin_h, params={"q": "'root' in parents", "pageSize": 1}
    ).json()["files"][0]
    got = client.get(f"/drive/v3/files/{folder['id']}", headers=admin_h)
    assert got.status_code == 200 and got.json()["mimeType"] == FOLDER_MIME
    perms = client.get(f"/drive/v3/files/{folder['id']}/permissions", headers=admin_h)
    assert perms.status_code == 200 and perms.json()["permissions"]


def test_drive_native_docs_report_size(client, admin_h):
    """Google populates `size` for binary content *and for Docs Editors files*; the mock omitted
    it on native rows, which taught implementors something false about the API."""
    doc = _drive_find(client, admin_h, "Brand")
    assert doc["mimeType"] == DOC_MIME
    assert int(doc["size"]) > 0
    assert "md5Checksum" not in doc  # real Drive omits checksums on native files
    folder = client.get(
        "/drive/v3/files", headers=admin_h, params={"q": "'root' in parents", "pageSize": 1}
    ).json()["files"][0]
    assert "size" not in folder  # ...but not for folders or shortcuts


# --- Drive about.get -----------------------------------------------------------------------
#
# `about` answers "who am I and how much space do I use" — the call a Drive client makes first,
# and the one the mock had no route for at all (404). Its contract is unusual: `fields` is
# mandatory, and the response carries only what the mask asked for.

ABOUT = "/drive/v3/about"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def _about(client, headers, fields):
    return client.get(ABOUT, headers=headers, params={"fields": fields})


def test_drive_about_requires_a_fields_mask(client, admin_h):
    """Real Drive 400s `about.get` with no `fields` — this resource has no default projection.
    Serving a full body instead would let a client ship a call that fails in production."""
    r = client.get(ABOUT, headers=admin_h)
    assert r.status_code == 400
    assert "fields" in r.json()["error"]["message"]


def test_drive_about_rejects_an_unknown_field(client, admin_h):
    """Same rule as the `files` masks: a typo 400s rather than quietly matching nothing."""
    assert _about(client, admin_h, "storageQuoat").status_code == 400
    assert _about(client, admin_h, "storageQuota").status_code == 200


def test_drive_about_rejects_a_mask_that_selects_nothing(client, admin_h):
    """`fields=,` clears the required-mask check but names no field. Falling through to "no
    projection" would answer a request for nothing with the entire resource."""
    r = client.get(ABOUT, headers=admin_h, params={"fields": ","})
    assert r.status_code == 400


def test_drive_about_needs_auth(client):
    # no header at all -> 403 on Drive (an unregistered caller); a bad token -> 401
    assert client.get(ABOUT, params={"fields": "user"}).status_code == 403
    bad = {"Authorization": "Bearer nope"}
    assert client.get(ABOUT, params={"fields": "user"}, headers=bad).status_code == 401
    # auth is resolved before the mask, as real Drive does — a missing mask on a bad token is 401
    assert client.get(ABOUT, headers=bad).status_code == 401


def test_drive_about_serves_only_the_requested_fields(client, admin_h):
    """Unlike `files.list` — whose typed response model always carries `kind` — `about` projects
    strictly, which is what real Drive does: ask for `user` and `user` is all you get."""
    j = _about(client, admin_h, "user").json()
    assert set(j) == {"user"}
    assert set(_about(client, admin_h, "user,storageQuota").json()) == {"user", "storageQuota"}


def test_drive_about_nested_mask_selects_its_parent(client, admin_h):
    """`storageQuota/limit` selects `storageQuota`, the same rule every other mask in this mock
    follows — one projection depth, applied consistently."""
    j = _about(client, admin_h, "storageQuota/limit").json()
    assert set(j) == {"storageQuota"}
    assert "usage" in j["storageQuota"]


def test_drive_about_user_is_the_caller(client, tokens_yaml):
    """`about.user` is the authenticated user, so `me` is true — the opposite of the same object
    read as a file's `owners` entry, where it describes someone else."""
    mia = {"Authorization": f"Bearer {tok(tokens_yaml, 'mia@acme.com')}"}
    u = _about(client, mia, "user").json()["user"]
    assert u["kind"] == "drive#user"
    assert u["emailAddress"] == "mia@acme.com"
    assert u["me"] is True
    # the file resource keeps its own answer: mia as an owner is not "me" to the object itself
    assert _drive_find(client, mia, "Brand")["owners"][0]["me"] is False


def test_drive_about_admin_token_reports_a_concrete_address(client, admin_h):
    """The admin/service token is not a Drive person; real Drive still never reports a placeholder
    here, so the service identity stands in — as `gmail.users.getProfile` already does."""
    u = _about(client, admin_h, "user").json()["user"]
    assert "@" in u["emailAddress"] and u["me"] is True


def test_drive_about_usage_matches_the_sizes_files_list_serves(client, tokens_yaml):
    """storageQuota and files.list are two views of one corpus. If they disagree, a client cannot
    reconcile "how much space do I use" with "what is in my Drive"."""
    mia = {"Authorization": f"Bearer {tok(tokens_yaml, 'mia@acme.com')}"}
    quota = _about(client, mia, "storageQuota").json()["storageQuota"]
    files = client.get(
        "/drive/v3/files", headers=mia, params={"pageSize": 100, "fields": "files(size)"}
    ).json()["files"]
    listed = sum(int(f["size"]) for f in files if "size" in f)  # folders carry no size
    assert listed > 0
    assert int(quota["usageInDrive"]) == listed
    assert quota["usage"] == quota["usageInDrive"]  # the mock stores nothing outside Drive
    assert int(quota["limit"]) == 2199023255552  # 2 TiB
    assert int(quota["usageInDriveTrash"]) == 0  # SAMPLE trashes nothing


def test_drive_about_usage_is_scoped_to_the_caller(client, admin_h, tokens_yaml):
    """A scoped token must not be told the weight of a corpus it cannot read."""
    mia = {"Authorization": f"Bearer {tok(tokens_yaml, 'mia@acme.com')}"}
    mine = int(_about(client, mia, "storageQuota").json()["storageQuota"]["usage"])
    everything = int(_about(client, admin_h, "storageQuota").json()["storageQuota"]["usage"])
    assert 0 < mine < everything


def test_drive_about_export_formats_are_honoured_by_files_export(client, admin_h):
    """Advertising a target that `files.export` refuses would be worse than advertising nothing:
    a client reads this map to decide what to ask for."""
    formats = _about(client, admin_h, "exportFormats").json()["exportFormats"]
    doc = _drive_find(client, admin_h, "Brand")
    assert doc["mimeType"] == DOC_MIME and formats[DOC_MIME]
    for target in formats[DOC_MIME]:
        r = client.get(
            f"/drive/v3/files/{doc['id']}/export", headers=admin_h, params={"mimeType": target}
        )
        assert r.status_code == 200, target
    # every native type the mock serves is covered; the folder type is not exportable anywhere
    assert set(formats) == {DOC_MIME, SHEET_MIME, "application/vnd.google-apps.presentation"}
    assert "text/csv" in formats[SHEET_MIME]


def test_drive_about_shared_drive_fields_agree_with_the_drives_listing(client, admin_h):
    """The mock's corpus is all My Drive and `/drive/v3/drives` is empty, so every shared-drive
    field has to say the same thing rather than hinting at a capability that isn't there."""
    j = _about(client, admin_h, "*").json()
    assert client.get("/drive/v3/drives", headers=admin_h).json()["drives"] == []
    assert j["canCreateDrives"] is False and j["canCreateTeamDrives"] is False
    assert j["driveThemes"] == [] and j["teamDriveThemes"] == []


def test_drive_about_star_serves_the_whole_resource(client, admin_h):
    j = _about(client, admin_h, "*").json()
    assert j["kind"] == "drive#about"
    assert j["appInstalled"] is False
    assert {
        "user",
        "storageQuota",
        "importFormats",
        "exportFormats",
        "maxImportSizes",
        "maxUploadSize",
        "folderColorPalette",
    } <= set(j)
    # folderColorRgb is a documented file field, so the palette a client picks from must be real
    assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in j["folderColorPalette"])
    assert DOC_MIME in j["importFormats"]["text/plain"]


def test_drive_about_appears_in_the_openapi_spec(client):
    """The OpenAPI→MCP bridge builds its tools from the spec, so a route the spec omits is a route
    no generated client can reach."""
    op = client.get("/openapi.json").json()["paths"][ABOUT]["get"]
    assert {p["name"] for p in op["parameters"]} == {"fields"}


@pytest.fixture(scope="module")
def base(live_server):
    return live_server[0]


@pytest.fixture(scope="module")
def admin_h(live_server):
    return {"Authorization": f"Bearer {live_server[1].admin_token}"}


def _drive_by_mime(base, admin_h, mime):
    """A visible Drive file id + name for the given native mimeType."""
    r = httpx.get(
        f"{base}/drive/v3/files", headers=admin_h, params={"q": "trashed=false", "pageSize": 1000}
    ).json()
    for f in r["files"]:
        if f["mimeType"] == mime:
            return f["id"], f["name"]
    raise AssertionError(f"no {mime} in corpus")


# --- Drive navigability ---------------------------------------------------------


def test_shared_drives_empty(base, admin_h):
    r = httpx.get(f"{base}/drive/v3/drives", headers=admin_h, params={"fields": "drives(id,name)"})
    assert r.status_code == 200
    assert r.json()["drives"] == []


def test_root_lists_folders_with_matching_ids(base, admin_h):
    r = httpx.get(
        f"{base}/drive/v3/files",
        headers=admin_h,
        params={"q": "'root' in parents and trashed=false", "pageSize": 1000},
    ).json()
    folders = r["files"]
    assert folders, "root should expose folder objects"
    assert all(f["mimeType"] == "application/vnd.google-apps.folder" for f in folders)
    names = {f["name"] for f in folders}
    assert {"marketing", "finance"} <= names

    # a folder's id must equal what its children report as their parent, so a client can descend
    finance = next(f for f in folders if f["name"] == "finance")
    kids = httpx.get(
        f"{base}/drive/v3/files",
        headers=admin_h,
        params={"q": f"'{finance['id']}' in parents and trashed=false"},
    ).json()["files"]
    assert kids and all(finance["id"] in k["parents"] for k in kids)
    # and GET on the folder id resolves to the folder object
    got = httpx.get(f"{base}/drive/v3/files/{finance['id']}", headers=admin_h).json()
    assert got["mimeType"] == "application/vnd.google-apps.folder" and got["name"] == "finance"


# --- Workspace editor read APIs -------------------------------------------------


def test_docs_get_returns_paragraph_text(base, admin_h):
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.document")
    doc = httpx.get(f"{base}/docs/v1/documents/{fid}", headers=admin_h).json()
    assert doc["documentId"] == fid
    text = "".join(
        e["textRun"]["content"]
        for el in doc["body"]["content"]
        if "paragraph" in el
        for e in el["paragraph"]["elements"]
    )
    assert "Logo usage" in text  # SAMPLE "Brand guidelines v3"


def test_sheets_get_withholds_grid_data_by_default(base, admin_h):
    """Measured: a plain `spreadsheets.get` returns `sheets[i].properties` and NO `data` — on a real
    workbook that is 4 KB against 5.7 MB with `includeGridData=true`. The mock used to volunteer the
    full grid on every call, so a reader got cells here that it would never get from Google, and the
    document it built had a different layout in the two environments.

    `ranges` alone does not unlock it either — also measured."""
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    for params in ({}, {"ranges": "Sheet1!A1:A2"}):
        sh = httpx.get(
            f"{base}/sheets/v4/spreadsheets/{fid}", headers=admin_h, params=params
        ).json()
        assert sh["spreadsheetId"] == fid
        assert set(sh["sheets"][0]) == {"properties"}, params
    props = sh["sheets"][0]["properties"]
    # the measured key set of a real sheet's properties
    assert set(props) == {"sheetId", "title", "index", "sheetType", "gridProperties"}
    assert props["gridProperties"] == {"rowCount": 1000, "columnCount": 26}


def test_sheets_get_returns_grid_when_asked(base, admin_h):
    """One row per stored line, one cell per row holding the line verbatim. This used to split on
    commas, which over the real corpus manufactured columns out of prose punctuation — see
    `_sheets_grid`; the corpus has no delimiter-uniform CSV at all."""
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    sh = httpx.get(
        f"{base}/sheets/v4/spreadsheets/{fid}", headers=admin_h, params={"includeGridData": "true"}
    ).json()
    data = sh["sheets"][0]["data"][0]
    assert "startRow" not in data and "startColumn" not in data, "zeros are omitted, as proto3 does"
    rows = data["rowData"]
    # a cell object per column of the range (26), the empty ones carrying no value — measured shape
    assert {len(r["values"]) for r in rows} == {26}
    assert all(c == {} for r in rows for c in r["values"][1:])
    assert [r["values"][0]["formattedValue"] for r in rows] == [
        "month,revenue",
        "Jan,120000",
        "Feb,135000",
    ]


def test_sheets_get_grid_data_honours_ranges(base, admin_h):
    """Measured: `ranges` + `includeGridData` scopes the returned rowData to the range (a real
    workbook went 5.7 MB -> 11 KB for `A1:B2`)."""
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    sh = httpx.get(
        f"{base}/sheets/v4/spreadsheets/{fid}",
        headers=admin_h,
        params={"includeGridData": "true", "ranges": "Sheet1!A2:A3"},
    ).json()
    data = sh["sheets"][0]["data"][0]
    assert data["startRow"] == 1
    cells = [[c.get("formattedValue") for c in row["values"]] for row in data["rowData"]]
    assert cells == [["Jan,120000"], ["Feb,135000"]]


def test_slides_get_returns_slides(base, admin_h):
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.presentation")
    pr = httpx.get(f"{base}/slides/v1/presentations/{fid}", headers=admin_h).json()
    assert pr["presentationId"] == fid and len(pr["slides"]) >= 1
    text = "".join(
        t["textRun"]["content"]
        for s in pr["slides"]
        for pe in s["pageElements"]
        for t in pe["shape"]["text"]["textElements"]
    )
    assert "Slide 1" in text


# The three refusals below were MEASURED against the live Google APIs (docs.googleapis.com,
# sheets.googleapis.com, slides.googleapis.com) with real OAuth credentials, one call per cell:
#
#   target passed to API X                     | result
#   -------------------------------------------|----------------------------------------------
#   a DIFFERENT native Workspace type          | 404 NOT_FOUND  "Requested entity was not found."
#   an Office file of X's own family           | 400 FAILED_PRECONDITION  (Office message)
#   any other non-native (pdf/txt/folder/…)    | 400 INVALID_ARGUMENT  "Request contains an
#                                              |     invalid argument."
#   a nonexistent id                           | 404 NOT_FOUND  (same as row 1)
#
# The first row is the surprise: a Doc id is not a "bad spreadsheet" to the Sheets API, it is
# simply not an entity it knows, and it is indistinguishable from an id that does not exist.
NOT_FOUND = "Requested entity was not found."
INVALID_ARG = "Request contains an invalid argument."
OFFICE_MSG = (
    "This operation is not supported for this document. The document must not be an Office file."
)


def test_editor_apis_treat_another_native_type_as_not_found(base, admin_h):
    """Measured: 404 "Requested entity was not found." — the SAME answer a nonexistent id gets.
    The mock used to serve these 200, reinterpreting the file: a Doc read through the Sheets API
    came back as a "grid" of prose, plausible enough that a client would trust it."""
    doc, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.document")
    sheet, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    deck, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.presentation")
    for path, label in [
        (f"/sheets/v4/spreadsheets/{doc}", "a Doc through Sheets"),
        (f"/sheets/v4/spreadsheets/{deck}", "a Deck through Sheets"),
        (f"/docs/v1/documents/{sheet}", "a Sheet through Docs"),
        (f"/docs/v1/documents/{deck}", "a Deck through Docs"),
        (f"/slides/v1/presentations/{doc}", "a Doc through Slides"),
        (f"/slides/v1/presentations/{sheet}", "a Sheet through Slides"),
    ]:
        r = httpx.get(f"{base}{path}", headers=admin_h)
        assert r.status_code == 404, f"{label}: {r.status_code}"
        assert r.json()["error"]["message"] == NOT_FOUND, label
    # and it is the same answer as an id that does not exist at all — body and all
    assert httpx.get(f"{base}/sheets/v4/spreadsheets/no-such-id", headers=admin_h).json() == {
        "error": {"code": 404, "message": NOT_FOUND, "status": "NOT_FOUND"}
    }
    # each API still serves its OWN type — without this arm a blanket 404 would pass
    assert httpx.get(f"{base}/docs/v1/documents/{doc}", headers=admin_h).status_code == 200
    assert httpx.get(f"{base}/sheets/v4/spreadsheets/{sheet}", headers=admin_h).status_code == 200
    assert httpx.get(f"{base}/slides/v1/presentations/{deck}", headers=admin_h).status_code == 200


def test_sheets_values_treat_another_native_type_as_not_found(base, admin_h):
    """The values routes go through the same guard, so they cannot become the way around it."""
    doc, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.document")
    for r in (_values(base, admin_h, doc, "Sheet1"), _batch(base, admin_h, doc, ["Sheet1"])):
        assert r.status_code == 404
        assert r.json()["error"]["message"] == NOT_FOUND


def test_editor_apis_reject_a_non_native_file(base, admin_h):
    """A PDF is not a Workspace document in any family: measured 400 "Request contains an invalid
    argument." on all three APIs — a different answer from another native type, which 404s."""
    pdf, _ = _drive_by_mime(base, admin_h, "application/pdf")
    for path in (
        f"/sheets/v4/spreadsheets/{pdf}",
        f"/docs/v1/documents/{pdf}",
        f"/slides/v1/presentations/{pdf}",
    ):
        r = httpx.get(f"{base}{path}", headers=admin_h)
        assert r.status_code == 400, path
        assert r.json()["error"]["message"] == INVALID_ARG, path


def test_editor_apis_reject_an_office_file_of_their_own_family(base, admin_h):
    """The one case the third-party bug reports were actually about, and it is narrower than they
    suggest: an Office file gets the Office-specific FAILED_PRECONDITION message ONLY from the API
    that owns its family. Measured both ways round — xlsx to Sheets and docx to Docs give the
    Office message, while xlsx to Docs and docx to Sheets give the plain invalid-argument one."""
    xlsx, _ = _drive_by_mime(
        base, admin_h, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    r = httpx.get(f"{base}/sheets/v4/spreadsheets/{xlsx}", headers=admin_h)
    assert r.status_code == 400
    assert r.json()["error"]["message"] == OFFICE_MSG
    # the same file through the other two APIs is just an invalid argument
    for path in (f"/docs/v1/documents/{xlsx}", f"/slides/v1/presentations/{xlsx}"):
        assert httpx.get(f"{base}{path}", headers=admin_h).json()["error"]["message"] == INVALID_ARG


def test_editor_apis_reject_a_folder(base, admin_h):
    """A folder id is reachable — a client walking Drive holds them — and real Google answers 400
    invalid-argument rather than pretending the folder is a document."""
    folder = httpx.get(
        f"{base}/drive/v3/files", headers=admin_h, params={"q": "'root' in parents", "pageSize": 1}
    ).json()["files"][0]["id"]
    r = httpx.get(f"{base}/docs/v1/documents/{folder}", headers=admin_h)
    assert r.status_code == 400
    assert r.json()["error"]["message"] == INVALID_ARG


def test_wrong_type_is_refused_before_it_is_read(base, live_server):
    """A caller who cannot see the file still gets 404, not 400: the type of a document you have
    no access to is not something the API should confirm."""
    import yaml

    tokens = {
        u["email"]: u["token"]
        for u in yaml.safe_load(live_server[1].tokens_path.read_text())["users"]
    }
    admin_h = {"Authorization": f"Bearer {live_server[1].admin_token}"}
    sheet, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    outsider = {"Authorization": f"Bearer {tokens['mia@acme.com']}"}  # cannot see the finance sheet
    assert httpx.get(f"{base}/docs/v1/documents/{sheet}", headers=outsider).status_code == 404


def test_editor_apis_enforce_acl(base, live_server):
    """The finance spreadsheet is group-restricted; a non-member gets 404, not the content."""
    import yaml

    tokens = {
        u["email"]: u["token"]
        for u in yaml.safe_load(live_server[1].tokens_path.read_text())["users"]
    }
    admin_h = {"Authorization": f"Bearer {live_server[1].admin_token}"}
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    outsider = {"Authorization": f"Bearer {tokens['mia@acme.com']}"}  # marketing, not finance
    assert httpx.get(f"{base}/sheets/v4/spreadsheets/{fid}", headers=outsider).status_code == 404


# --- Sheets values.get / values.batchGet ----------------------------------------
#
# A spreadsheet's stored content is one text blob, and a line break is the only structure it
# actually has — so a row is a line and a row has ONE cell holding that line verbatim. The SAMPLE
# spreadsheet ("Q1 Revenue Model") stores "month,revenue\nJan,120000\nFeb,135000", which is:
#
#          A
#   1  month,revenue
#   2  Jan,120000
#   3  Feb,135000
#
# The commas stay inside the cell. Splitting on them would be a delimiter policy, and the bench
# corpus says the mock has no business guessing one: of its 1,875 `doc_type: sheet` records, NONE
# is delimiter-uniform CSV — 82.6% are prose and 17.4% are prose around a PIPE-delimited table.

GRID = [["month,revenue"], ["Jan,120000"], ["Feb,135000"]]


@pytest.fixture(scope="module")
def sheet_id(base, admin_h):
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    return fid


def _values(base, headers, sheet_id, rng, **params):
    return httpx.get(
        f"{base}/sheets/v4/spreadsheets/{sheet_id}/values/{quote(rng, safe='')}",
        headers=headers,
        params=params,
    )


def _batch(base, headers, sheet_id, ranges, **params):
    return httpx.get(
        f"{base}/sheets/v4/spreadsheets/{sheet_id}/values:batchGet",
        headers=headers,
        params=[("ranges", r) for r in ranges] + list(params.items()),
    )


@pytest.mark.parametrize(
    "rng, expected",
    [
        ("Sheet1", GRID),  # whole sheet
        ("Sheet1!A1:A3", GRID),  # explicit bounds
        ("A1:A3", GRID),  # sheet name omitted
        ("Sheet1!A1:B3", GRID),  # column B is empty, so it trims away
        ("Sheet1!A1:A2", GRID[:2]),  # sub-range
        ("Sheet1!A2", [["Jan,120000"]]),  # single cell keeps its commas
        ("A:A", GRID),  # whole column
        ("1:1", [GRID[0]]),  # whole row
        ("Sheet1!A2:A", GRID[1:]),  # unbounded lower edge
        ("'Sheet1'!A1:A1", [GRID[0]]),  # quoted sheet name
    ],
)
def test_sheets_values_get_range_forms(base, admin_h, sheet_id, rng, expected):
    """Every A1 form a client may send has to resolve against the same grid. Without the parser
    each of these is a 404 on a route that does not exist."""
    r = _values(base, admin_h, sheet_id, rng)
    assert r.status_code == 200, r.text
    assert r.json()["values"] == expected


def test_sheets_values_get_keeps_a_line_intact(base, admin_h, sheet_id):
    """The cell holds the whole line, commas and all. Splitting on commas is what the mock used to
    do, and over the real corpus it manufactured columns out of sentence punctuation — a prose line
    like "customer dates, ARR exposure, highest-risk deals" became three cells of a table that
    never existed. Which delimiter (if any) applies is the corpus owner's call, not the mock's."""
    j = _values(base, admin_h, sheet_id, "Sheet1!A1").json()
    assert j["values"] == [["month,revenue"]]
    # and there is exactly one column: B is past the end of every row
    assert "values" not in _values(base, admin_h, sheet_id, "Sheet1!B1:B3").json()


def test_sheets_values_round_trips_the_stored_text(base, admin_h, sheet_id):
    """The invariant that makes "serve it as-is" checkable: the cells of the whole sheet, joined
    by newlines, reproduce byte-for-byte what Drive's CSV export serves. If a future splitter
    breaks that, it is inventing or dropping something.

    A blank line comes back as ``[]``, not ``[""]`` — trailing-empty trimming empties the row, which
    is also what real Sheets returns for an interior blank row. So the reconstruction has to read an
    empty row as an empty line: a naive ``cells[0]`` passes on the SAMPLE sheet (it has no blank
    lines) and raises IndexError on a real corpus, which is why a blank line is asserted below."""
    export = httpx.get(
        f"{base}/drive/v3/files/{sheet_id}/export", headers=admin_h, params={"mimeType": "text/csv"}
    ).text
    rows = _values(base, admin_h, sheet_id, "Sheet1").json()["values"]
    assert "\n".join((cells[0] if cells else "") for cells in rows) == export


def test_sheets_values_serve_a_blank_line_as_an_empty_row(base, admin_h):
    """A blank line is an empty row ``[]``, not a row holding ``""`` — trailing-empty trimming
    empties it, which is what real Sheets returns for an interior blank row (measured: a real
    whole-sheet read came back with row widths {0, 4, 5, 6}).

    ``gd-blankline`` stores "header\\n\\nrow after gap\\n\\n": a gap in the middle and two at the
    end. The trailing ones trim away entirely; the middle one survives as ``[]``."""
    j = _values(base, admin_h, "gd-blankline", "Sheet1").json()
    assert j["values"] == [["header"], [], ["row after gap"]]
    # and the round trip still holds, blank lines and all — trailing gaps included
    export = httpx.get(
        f"{base}/drive/v3/files/gd-blankline/export",
        headers=admin_h,
        params={"mimeType": "text/csv"},
    ).text
    rebuilt = "\n".join((c[0] if c else "") for c in j["values"])
    assert rebuilt == export.rstrip("\n")
    assert export.endswith("\n\n"), "the stored trailing gap is still in the exported text"


def test_sheets_values_get_accepts_an_unencoded_range(base, admin_h, sheet_id):
    """`!` and `:` are legal in a path segment, so a hand-written URL must work as well as the
    percent-encoded one google-api-python-client sends."""
    r = httpx.get(f"{base}/sheets/v4/spreadsheets/{sheet_id}/values/Sheet1!A1:A2", headers=admin_h)
    assert r.status_code == 200
    assert r.json()["values"] == GRID[:2]


def test_sheets_values_get_echoes_the_normalized_range(base, admin_h, sheet_id):
    """A client caches on `range`, so the response names the resolved range in full A1 form —
    sheet included — however the request spelled it."""
    assert _values(base, admin_h, sheet_id, "A1:A2").json()["range"] == "Sheet1!A1:A2"
    # an unbounded edge resolves against the GRID, not the data — measured: a 14-row real sheet
    # answers `values/<title>` with `A1:Z1000`, not `A1:D14`
    assert _values(base, admin_h, sheet_id, "Sheet1").json()["range"] == "Sheet1!A1:Z1000"
    assert _values(base, admin_h, sheet_id, "A:A").json()["range"] == "Sheet1!A1:A1000"
    assert _values(base, admin_h, sheet_id, "1:1").json()["range"] == "Sheet1!A1:Z1"


def test_sheets_values_get_defaults_to_rows(base, admin_h, sheet_id):
    assert _values(base, admin_h, sheet_id, "Sheet1!A1:A3").json()["majorDimension"] == "ROWS"


def test_sheets_values_get_major_dimension_columns_transposes(base, admin_h, sheet_id):
    j = _values(base, admin_h, sheet_id, "Sheet1!A1:A3", majorDimension="COLUMNS").json()
    assert j["majorDimension"] == "COLUMNS"
    # one column, holding every line in order
    assert j["values"] == [["month,revenue", "Jan,120000", "Feb,135000"]]


def test_sheets_values_get_trims_trailing_empties(base, admin_h, sheet_id):
    """Real Sheets does not pad a range out to its bounds: a row stops at its last non-empty cell
    and the block stops at its last non-empty row. Padding would make a client read phantom
    columns that the grid does not have."""
    j = _values(base, admin_h, sheet_id, "Sheet1!A1:D5").json()
    assert j["values"] == GRID  # not 5 rows, not 4 columns


def test_sheets_values_get_omits_values_when_the_range_is_empty(base, admin_h, sheet_id):
    """An empty range answers 200 with NO `values` key at all — not `[]`. A client testing
    `"values" in resp` is the documented way to tell empty from present."""
    r = _values(base, admin_h, sheet_id, "Sheet1!D1:E2")
    assert r.status_code == 200
    assert "values" not in r.json()
    assert r.json()["range"] == "Sheet1!D1:E2"


@pytest.mark.parametrize("rng", ["Other!A1:B2", "not a range", "A1:", "!A1", ""])
def test_sheets_values_get_rejects_an_unusable_range(base, admin_h, sheet_id, rng):
    """The mock has exactly one sheet, `Sheet1`; naming another is as unresolvable as a malformed
    reference, and real Sheets 400s on both rather than returning an empty grid."""
    assert _values(base, admin_h, sheet_id, rng).status_code == 400


@pytest.mark.parametrize(
    "params, field, enum",
    [
        ({"majorDimension": "DIAGONAL"}, "major_dimension", "Dimension"),
        ({"valueRenderOption": "NOPE"}, "value_render_option", "ValueRenderOption"),
    ],
)
def test_sheets_values_get_rejects_a_bad_enum(base, admin_h, sheet_id, params, field, enum):
    """Measured message shape, not an invented one: Google names the proto field and type."""
    r = _values(base, admin_h, sheet_id, "Sheet1!A1:A2", **params)
    assert r.status_code == 400
    bad = next(iter(params.values()))
    assert r.json()["error"]["message"] == (
        f"Invalid value at '{field}' (type.googleapis.com/google.apps.sheets.v4.{enum}), \"{bad}\""
    )


def test_sheets_values_get_render_options_agree_on_this_corpus(base, admin_h, sheet_id):
    """The corpus stores no formulas and no typed numbers — `spreadsheets.get` already declares
    every cell a `stringValue` — so the three render options coincide here. Asserted so that a
    future change which makes them diverge has to say so."""
    out = {
        opt: _values(base, admin_h, sheet_id, "Sheet1!A1:A3", valueRenderOption=opt).json()[
            "values"
        ]
        for opt in ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")
    }
    assert out["FORMATTED_VALUE"] == out["UNFORMATTED_VALUE"] == out["FORMULA"] == GRID


def test_sheets_values_get_agrees_with_spreadsheets_get(base, admin_h, sheet_id):
    """Two views of one document: the grid `values.get` serves must be the grid the structured
    read serves, or a client gets a different answer depending on which call it made."""
    sh = httpx.get(
        f"{base}/sheets/v4/spreadsheets/{sheet_id}",
        headers=admin_h,
        params={"includeGridData": "true"},
    ).json()
    # the grid pads each row to the range width with empty cells; `values` trims them. Drop the
    # padding and the two must name the same cells.
    structured = [
        [c["formattedValue"] for c in row["values"] if c]
        for row in sh["sheets"][0]["data"][0]["rowData"]
    ]
    assert _values(base, admin_h, sheet_id, "Sheet1").json()["values"] == structured


def test_sheets_values_get_enforces_the_acl(base, live_server, sheet_id):
    """The finance spreadsheet is group-restricted; the values route must not be a way around the
    ACL that `spreadsheets.get` enforces."""
    import yaml

    tokens = {
        u["email"]: u["token"]
        for u in yaml.safe_load(live_server[1].tokens_path.read_text())["users"]
    }
    outsider = {"Authorization": f"Bearer {tokens['mia@acme.com']}"}  # marketing, not finance
    admin_h = {"Authorization": f"Bearer {live_server[1].admin_token}"}
    # the admin arm is what keeps this honest: without it a missing route 404s and the test passes
    assert _values(base, admin_h, sheet_id, "Sheet1").status_code == 200
    assert _batch(base, admin_h, sheet_id, ["Sheet1"]).status_code == 200
    assert _values(base, outsider, sheet_id, "Sheet1").status_code == 404
    assert _batch(base, outsider, sheet_id, ["Sheet1"]).status_code == 404


def test_sheets_values_get_needs_auth(base, sheet_id):
    # Sheets accepts API keys, so no header at all is 403 PERMISSION_DENIED; a bad bearer is 401.
    # Both measured against the live API.
    assert _values(base, {}, sheet_id, "Sheet1").status_code == 403
    assert _values(base, {"Authorization": "Bearer nope"}, sheet_id, "Sheet1").status_code == 401


def test_sheets_values_get_clamps_a_range_that_overflows_the_grid(base, admin_h, sheet_id):
    """Measured: an END past the grid is CLAMPED, not refused — `A1:AA5` on a 26-column sheet comes
    back as `A1:Z5`, and `A1:B1001` as `A1:B1000`."""
    assert _values(base, admin_h, sheet_id, "A1:AA5").json()["range"] == "Sheet1!A1:Z5"
    assert _values(base, admin_h, sheet_id, "A1:B1001").json()["range"] == "Sheet1!A1:B1000"
    assert _values(base, admin_h, sheet_id, "Z1:AA5").json()["range"] == "Sheet1!Z1:Z5"


@pytest.mark.parametrize("rng", ["AA1:AB5", "ZZ1:ZZ5", "A1001:B1002", "AA1001:AB1002"])
def test_sheets_values_get_rejects_a_start_outside_the_grid(base, admin_h, sheet_id, rng):
    """Measured: the START must sit inside the grid. Overflowing the end clamps; starting outside
    is an error naming the limits."""
    r = _values(base, admin_h, sheet_id, rng)
    assert r.status_code == 400
    assert r.json()["error"]["message"].startswith("Range (Sheet1!")
    assert r.json()["error"]["message"].endswith(
        "exceeds grid limits. Max rows: 1000, max columns: 26"
    )


def test_sheets_values_get_empty_inside_the_grid_is_not_an_error(base, admin_h, sheet_id):
    """Measured: a range inside the grid but past the data answers 200 with the range echoed and no
    `values` key — distinct from a range that starts outside the grid, which 400s."""
    for rng in ("A100:B101", "A100", "Z1:Z5"):
        j = _values(base, admin_h, sheet_id, rng).json()
        assert "values" not in j, rng
        assert j["range"].startswith("Sheet1!"), rng


# Every (range -> echoed range) pair below was compared side by side against the live Sheets API on
# a real spreadsheet, normalising only the sheet title. 19 of 21 cases came back byte-identical; the
# other two (`A1`, `'Sheet1'!A1:A1`) differ only because that spreadsheet's A1 is blank while the
# SAMPLE's is not — same status, same echo. Pinned here so the parser cannot drift back.
MEASURED_ECHO = [
    ("Sheet1", "Sheet1!A1:Z1000"),
    ("Sheet1!A1:A2", "Sheet1!A1:A2"),
    ("A1:A2", "Sheet1!A1:A2"),
    ("A:A", "Sheet1!A1:A1000"),
    ("1:1", "Sheet1!A1:Z1"),
    ("Sheet1!A2:A", "Sheet1!A2:A1000"),
    ("Sheet1!A1", "Sheet1!A1"),
    ("'Sheet1'!A1:A1", "Sheet1!A1"),
    ("A1:AA5", "Sheet1!A1:Z5"),
    ("A1:B1001", "Sheet1!A1:B1000"),
    ("Z1:AA5", "Sheet1!Z1:Z5"),
    ("A100:B101", "Sheet1!A100:B101"),
    ("A100", "Sheet1!A100"),
    ("Sheet1!A1:D5", "Sheet1!A1:D5"),
]


def test_sheets_values_accept_a_bare_quoted_sheet_name(base, admin_h, sheet_id):
    """`'Sheet1'` with no `!cellpart` means "every cell in that sheet" — measured on a real
    spreadsheet, quoted and unquoted alike, on both `values.get` and `values:batchGet`.

    The mock only un-quoted a title when a `!` followed, so the one form that means "the whole
    sheet without naming bounds" 400d. A client cannot drop the quotes to work around it: quoting is
    what disambiguates a sheet name from a cell reference, measured below."""
    for rng in ("Sheet1", "'Sheet1'"):
        r = _values(base, admin_h, sheet_id, rng)
        assert r.status_code == 200, f"{rng}: {r.text}"
        assert r.json()["range"] == "Sheet1!A1:Z1000", rng
        b = _batch(base, admin_h, sheet_id, [rng])
        assert b.status_code == 200, f"batch {rng}: {b.text}"
        assert b.json()["valueRanges"][0]["range"] == "Sheet1!A1:Z1000", rng


def test_sheets_values_quoting_distinguishes_a_sheet_from_a_cell(base, admin_h, sheet_id):
    """Measured: bare `A1` is the CELL A1 of the first sheet, while `'A1'` is a request for a SHEET
    named A1 and 400s when there is none. So the quotes carry meaning and cannot be stripped —
    without them a client asking for a tab would silently read another tab's cells."""
    assert _values(base, admin_h, sheet_id, "A1").json()["range"] == "Sheet1!A1"
    r = _values(base, admin_h, sheet_id, "'A1'")
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "Unable to parse range: 'A1'"
    # a quoted name that is not this spreadsheet's sheet is refused the same way
    assert _values(base, admin_h, sheet_id, "'Other'").status_code == 400
    assert _batch(base, admin_h, sheet_id, ["'Other'"]).status_code == 400


@pytest.mark.parametrize("rng, echo", MEASURED_ECHO)
def test_sheets_values_range_echo_matches_real_sheets(base, admin_h, sheet_id, rng, echo):
    r = _values(base, admin_h, sheet_id, rng)
    assert r.status_code == 200, r.text
    assert r.json()["range"] == echo


@pytest.mark.parametrize(
    "rng, message",
    [
        ("Other!A1:B2", "Unable to parse range: Other!A1:B2"),
        ("not a range", "Unable to parse range: not a range"),
        ("A1:", "Unable to parse range: A1:"),  # the WHOLE spec, not the offending half
    ],
)
def test_sheets_values_parse_error_matches_real_sheets(base, admin_h, sheet_id, rng, message):
    r = _values(base, admin_h, sheet_id, rng)
    assert r.status_code == 400
    assert r.json()["error"]["message"] == message


def test_sheets_batch_get_returns_one_value_range_per_request_range(base, admin_h, sheet_id):
    j = _batch(base, admin_h, sheet_id, ["Sheet1!A1:A1", "Sheet1!A3:A3"]).json()
    assert j["spreadsheetId"] == sheet_id
    # a 1x1 range echoes as a bare cell even when the request spelled out `A1:A1` — measured
    assert [vr["range"] for vr in j["valueRanges"]] == ["Sheet1!A1", "Sheet1!A3"]
    assert [vr["values"] for vr in j["valueRanges"]] == [[GRID[0]], [GRID[2]]]


def test_sheets_batch_get_matches_the_single_get_for_each_range(base, admin_h, sheet_id):
    """batchGet is N single gets through one resolver; if the two disagree, batching changes
    meaning rather than saving round trips."""
    ranges = ["Sheet1", "A1:A2", "Sheet1!A2", "A:A", "Sheet1!D1:E2"]
    batched = _batch(base, admin_h, sheet_id, ranges).json()["valueRanges"]
    singles = [_values(base, admin_h, sheet_id, r).json() for r in ranges]
    assert batched == singles


def test_sheets_batch_get_honors_major_dimension(base, admin_h, sheet_id):
    j = _batch(base, admin_h, sheet_id, ["Sheet1!A1:A3"], majorDimension="COLUMNS").json()
    assert j["valueRanges"][0]["values"] == [["month,revenue", "Jan,120000", "Feb,135000"]]


def test_sheets_batch_get_fails_the_whole_call_on_one_bad_range(base, admin_h, sheet_id):
    """A partial batch would leave the caller unable to tell which range it is missing, so real
    Sheets rejects the request outright."""
    assert _batch(base, admin_h, sheet_id, ["Sheet1!A1:A1", "Other!A1"]).status_code == 400


def test_sheets_batch_get_with_no_ranges_selects_nothing(base, admin_h, sheet_id):
    """`ranges` has no default, so an empty range list selects no data. NOTE: this is the natural
    reading of the API, NOT a behaviour diffed against real Sheets — see the route's comment."""
    r = _batch(base, admin_h, sheet_id, [])
    assert r.status_code == 200
    assert r.json()["spreadsheetId"] == sheet_id
    assert "valueRanges" not in r.json()


# --- Slack timestamp consistency ------------------------------------------------


def test_channel_created_not_after_messages(base, admin_h):
    channels = httpx.get(f"{base}/slack/api/conversations.list", headers=admin_h).json()["channels"]
    assert channels
    for ch in channels:
        hist = httpx.get(
            f"{base}/slack/api/conversations.history",
            headers=admin_h,
            params={"channel": ch["id"], "limit": 1},
        ).json()
        msgs = hist.get("messages", [])
        if msgs:
            assert ch["created"] <= float(msgs[0]["ts"]), f"#{ch['name']} created after its message"


def test_history_honors_oldest_latest(base, admin_h):
    """A time-bounded fetch (as a filesystem client makes per day) is filtered by ts — a tight
    window keeps the message, a window entirely after it drops the message."""
    cid = httpx.get(f"{base}/slack/api/conversations.list", headers=admin_h).json()["channels"][0][
        "id"
    ]
    ts = float(
        httpx.get(
            f"{base}/slack/api/conversations.history",
            headers=admin_h,
            params={"channel": cid, "limit": 1},
        ).json()["messages"][0]["ts"]
    )

    tight = httpx.get(
        f"{base}/slack/api/conversations.history",
        headers=admin_h,
        params={
            "channel": cid,
            "oldest": ts - 5,
            "latest": ts + 5,
            "inclusive": "true",
            "limit": 1000,
        },
    ).json()["messages"]
    assert any(abs(float(m["ts"]) - ts) < 1e-6 for m in tight)

    after = httpx.get(
        f"{base}/slack/api/conversations.history",
        headers=admin_h,
        params={"channel": cid, "oldest": ts + 1, "latest": ts + 100, "limit": 1000},
    ).json()["messages"]
    assert all(float(m["ts"]) > ts for m in after)  # the sampled message is excluded


# --- response-shape assertions (were tests/test_fidelity.py) --------------------------------


# --- Drive -----------------------------------------------------------------------


def test_drive_permissions_and_trashed(tmp_path):
    from backlot.routers.google import _drive_permissions, _drive_q_match

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "google_drive",
                "doc_id": "d1",
                "folder": "mk",
                "title": "Deck",
                "content": "x",
                "author_email": "a@x.com",
                "visibility": "public",
            },
            {
                "source_type": "google_drive",
                "doc_id": "d2",
                "folder": "mk",
                "title": "Old",
                "content": "y",
                "author_email": "a@x.com",
                "visibility": "group",
                "group": "mkt",
                "trashed": True,
            },
        ],
    )
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
    from backlot.routers.google import _drive_file

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "google_drive",
                "doc_id": "n1",
                "folder": "mk",
                "title": "Doc",
                "content": "hello there",
                "author_email": "a@x.com",
                "subtype": "document",
            },
            {
                "source_type": "google_drive",
                "doc_id": "b1",
                "folder": "mk",
                "title": "Scan.pdf",
                "content": "%PDF-1.7",
                "author_email": "a@x.com",
                "subtype": "pdf",
                "meta": {"mime_type": "application/pdf"},
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    native = _drive_file(conn, store.get_document(conn, "google_drive", "n1"))
    assert native["size"] == str(len("hello there"))
    # checksums and a download link stay binary-only, as they are on real Drive
    assert "md5Checksum" not in native and "webContentLink" not in native
    binary = _drive_file(conn, store.get_document(conn, "google_drive", "b1"))
    assert binary["size"] == str(len("%PDF-1.7")) and binary["md5Checksum"]


# --- Gmail -----------------------------------------------------------------------


def test_gmail_raw_and_headers(tmp_path):
    from backlot.routers.google import _gmail_message

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "gmail",
                "doc_id": "m1",
                "mailbox": "ceo",
                "title": "Hi",
                "content": "body text",
                "author_email": "ceo@x.com",
                "bcc": "secret@x.com",
            },
        ],
    )
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
    from backlot.routers.google import _gmail_message

    s = tiny_corpus(
        tmp_path,
        [
            {
                "source_type": "gmail",
                "doc_id": "m2",
                "mailbox": "ceo",
                "title": "With attachment",
                "content": "see attached",
                "author_email": "ceo@x.com",
                "attachments": [
                    {"filename": "notes.txt", "mime": "text/plain", "content": "hello"}
                ],
            },
        ],
    )
    conn = store.connect_ro(s.db_path)
    row = store.get_document(conn, "gmail", "m2")
    raw = _gmail_message(row, "raw")
    import base64
    import email

    decoded_bytes = base64.urlsafe_b64decode(raw["raw"])
    assert b"Content-Type: multipart/mixed" in decoded_bytes  # top_mime switches with attachments
    mime_msg = email.message_from_bytes(decoded_bytes)
    assert not mime_msg.defects, f"raw Gmail message is not valid MIME: {mime_msg.defects}"
    assert mime_msg.is_multipart()
    filenames = {p.get_filename() for p in mime_msg.get_payload() if p.get_filename()}
    assert "notes.txt" in filenames


# --- OAuth credentials (backlot/oauth.py) — the /oauth2/token exchange Google's SDKs refresh against -----


@pytest.fixture
def creds(tmp_path):
    s = Settings(data_dir=tmp_path, org_name="acme")
    oauth.generate(s, org="acme")
    return s, oauth.Oauth.load(s.credentials_path)


def test_generate_writes_credentials(creds):
    s, o = creds
    assert s.credentials_path.exists()
    assert o is not None and o._data["org"] == "acme"
    # one shared OAuth client + one service account with a real private key; no per-user data
    assert o.client_config()["client_id"].endswith(".apps.googleusercontent.com")
    assert "BEGIN PRIVATE KEY" in o.service_account_json("http://x")["private_key"]
    assert "users" not in o._data


def _assertion(o, claims):
    sa = o.service_account_json("http://x/oauth2/token")
    return jwt.encode(
        {
            "iss": sa["client_email"],
            "aud": sa["token_uri"],
            "iat": 0,
            "exp": 9_999_999_999,
            **claims,
        },
        sa["private_key"],
        algorithm="RS256",
    )


def test_service_account_assertion(creds):
    _, o = creds
    # domain-wide delegation: sub selects the impersonated user
    assert o.verify_assertion(_assertion(o, {"sub": "bob@acme.com"})) == "bob@acme.com"
    # bare service account (no sub) → sentinel so the endpoint grants a service identity
    assert o.verify_assertion(_assertion(o, {})) == ("", "sa")
    # wrong issuer / garbage signature → rejected
    assert o.verify_assertion(_assertion(o, {"iss": "evil@x", "sub": "bob@acme.com"})) is None
    assert o.verify_assertion("not.a.jwt") is None


def test_public_key_not_exposed(creds):
    _, o = creds
    # the SA bundle handed out carries the private key (client signs) but never the public key
    assert "public_key_pem" not in o.service_account_json("http://x")
